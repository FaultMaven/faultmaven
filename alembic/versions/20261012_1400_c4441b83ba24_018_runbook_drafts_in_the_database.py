"""018_runbook_drafts_in_the_database

A runbook draft's text lives in its row: ``conversion_drafts.content``
(``TEXT NOT NULL``) replaces ``conversion_drafts.file_path`` (#1950).

Until now a draft's text existed only as a file under the cwd-relative
``data/knowledge/`` of the pod that wrote it. The Cloud API runs as two replicas
with no volumes, so the file was missing on the other replica and gone after
every rollout: ``get_conversion`` served the draft with no content, ``verify``
failed with a 500, and a scan discarded every draft whose file that pod could
not see. The text now has one durable home that every replica reads, as a
published runbook's already does in ``knowledge_items.content``.

How each row gets its text
--------------------------

In order, each step filling only rows the steps before it left empty:

1. **Verified drafts** take the text of the runbook they were published as:
   ``knowledge_items.content`` through ``knowledge_item_id``. That text is what
   verify published and what search serves, frontmatter included.
2. **Other live drafts** (not discarded) take the text of the file their
   ``file_path`` names, read relative to the working directory the migration
   runs in and only from inside ``data/knowledge`` there. Standalone runs the
   migration at startup from the application root, where those files are. A
   path outside that tree, a missing file and an unreadable one all leave the
   row empty. This includes a VERIFIED draft whose published item was deleted
   (``knowledge_item_id`` cleared by the foreign key): step 1 cannot fill it,
   and with no file it is discarded by step 3 — the runbook it was published as
   is already gone.
3. **Live drafts still empty are DISCARDED**: their text is gone, and a draft
   with no text cannot be shown, edited or verified. A case conversion whose
   last live draft this discards releases its ``conversion_jobs.live_case_id``
   in the same statement order the application uses
   (``release_live_case_key_if_drained``), so that case can be converted
   again. The ids and the count are logged.
4. **Discarded drafts** get ``''``. Discard is a soft delete; nothing reads a
   discarded draft's text.

Then ``content`` becomes ``NOT NULL`` and ``file_path`` is dropped.

What users see: on Cloud, where the migration Job has no access to any pod's
disk, every unverified draft whose file lived on a pod is discarded — those
files were already lost by the rollouts before this one. Verified runbooks are
untouched (their text was always in ``knowledge_items``), and their draft rows
keep their text. A case whose only draft is discarded can be converted again.
On standalone, drafts whose files are in place keep them as their text.

The standalone guard
--------------------

On SQLite, a migration run from somewhere other than the application root
would find no ``data/knowledge`` and discard every unverified draft. When step
3 would discard a draft and ``data/knowledge`` does not exist in the working
directory, the upgrade REFUSES before changing anything, naming the directory
it looked in. Run it from the application root (the app's own startup run
does), or discard those drafts first. The guard is keyed on the DIALECT, not on
the deployment: PostgreSQL has none, because Cloud's migration Job never had
those files, by construction. A self-hosted single-tenant deployment on
PostgreSQL that runs this migration by hand from the wrong working directory is
therefore not protected; its normal path, the app's own startup migration, runs
from the application root and reads the files.

Offline (``--sql``)
-------------------

Step 2 reads files, which a generated script cannot do.

* **SQLite refuses to render** (``step (b) reads draft files; standalone
  migrates online from the app root``): SQLite is standalone, whose drafts'
  text is in those files, and a script would discard every one.
* **PostgreSQL renders**, because its migration Job never sees those files
  anyway (the online run finds none either). The script's FIRST statement is a
  SQL comment warning that it discards every live draft whose text is not
  already in the database (not published); a comment marks where step 2 would
  run, and steps 1, 3 and 4 follow as SQL.

Dialects
--------

PostgreSQL alters the table in place. The data steps run under
``SET LOCAL row_security = off`` (restored to ``DEFAULT`` after), the choice and
reasoning of revisions 006-008 and 013: they are tenant-wide by construction,
and a role the policy would filter raises rather than settling one enterprise's
rows.

SQLite cannot alter a column's nullability, so the table is rebuilt
(``batch_alter_table`` from a frozen copy of the table as 008 left it plus the
column this revision adds, ``recreate="always"``). No table references
``conversion_drafts`` by foreign key and it has no triggers (008's reasoning,
pinned by its test); its indexes, the partial unique
``(enterprise_id, runbook_id)`` among them, come back with the frozen
definition.

``downgrade()``
---------------

Re-adds ``file_path`` as a NULLABLE ``VARCHAR(500)`` and drops ``content``.
Nothing is restored: the text cannot be written back to files the parent
revision would look for, so every row's ``file_path`` is NULL, and the drafts
this revision discarded stay discarded.

Revision ID: c4441b83ba24
Revises: f95a5b3a58eb
Create Date: 2026-10-12 14:00:00
"""

import logging
from pathlib import Path
from typing import Optional, Sequence, Union

import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c4441b83ba24"  # pragma: allowlist secret
down_revision: Union[str, Sequence[str], None] = "f95a5b3a58eb"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_log = logging.getLogger("alembic.runtime.migration")

#: The runbook directory, relative to the working directory: where the
#: application wrote draft files (``utils.runbook_id.KNOWLEDGE_ROOT`` at the
#: parent revision). Frozen here, as migrations are history.
KNOWLEDGE_DIR = Path("data/knowledge")

#: Step 1: a verified draft's text is the runbook it was published as.
FILL_VERIFIED_FROM_KNOWLEDGE_ITEMS = text(
    "UPDATE conversion_drafts SET content = ("
    "SELECT knowledge_items.content FROM knowledge_items "
    "WHERE knowledge_items.item_id = conversion_drafts.knowledge_item_id"
    ") WHERE status = 'verified' AND content IS NULL "
    "AND knowledge_item_id IS NOT NULL"
)
#: Step 2's rows: live, still empty.
SELECT_LIVE_EMPTY = text(
    "SELECT id, file_path FROM conversion_drafts "
    "WHERE status <> 'discarded' AND content IS NULL ORDER BY id"
)
FILL_ONE = text("UPDATE conversion_drafts SET content = :content WHERE id = :id")
#: Step 3's rows, by id, for the log.
SELECT_TO_DISCARD = text(
    "SELECT id FROM conversion_drafts "
    "WHERE status <> 'discarded' AND content IS NULL ORDER BY id"
)
#: Step 3, first half: release the live-case key of every job that is about to
#: lose a draft and keeps no live draft with text. Before the discard, so the
#: rows it keys on are still recognisable.
RELEASE_LIVE_CASE_KEYS = text(
    "UPDATE conversion_jobs SET live_case_id = NULL "
    "WHERE live_case_id IS NOT NULL "
    "AND id IN (SELECT conversion_id FROM conversion_drafts "
    "WHERE status <> 'discarded' AND content IS NULL) "
    "AND NOT EXISTS (SELECT 1 FROM conversion_drafts AS kept "
    "WHERE kept.conversion_id = conversion_jobs.id "
    "AND kept.status <> 'discarded' AND kept.content IS NOT NULL)"
)
#: Step 3, second half.
DISCARD_EMPTY_LIVE = text(
    "UPDATE conversion_drafts SET status = 'discarded' "
    "WHERE status <> 'discarded' AND content IS NULL"
)
#: Step 4.
EMPTY_THE_REST = text("UPDATE conversion_drafts SET content = '' WHERE content IS NULL")
#: The standalone guard's count, taken before anything changes: the live drafts
#: step 1 cannot fill. With no ``data/knowledge``, step 2 fills none either.
COUNT_LIVE_WITHOUT_PUBLISHED_TEXT = text(
    "SELECT COUNT(*) FROM conversion_drafts WHERE status <> 'discarded' "
    "AND NOT (status = 'verified' AND EXISTS ("
    "SELECT 1 FROM knowledge_items "
    "WHERE knowledge_items.item_id = conversion_drafts.knowledge_item_id))"
)

#: The first statement of an offline (PostgreSQL) script.
OFFLINE_WARNING = (
    "-- WARNING (018): this script cannot read draft files. It DISCARDS every "
    "live runbook draft whose text is not already in the database (every "
    "unverified draft, and every verified one whose published item is gone). "
    "Run the migration online from the application root to keep drafts whose "
    "files are in place"
)

ROW_SECURITY_OFF = "SET LOCAL row_security = off"
ROW_SECURITY_RESTORED = "SET LOCAL row_security TO DEFAULT"

_TAGS_ARRAY = sa.Text().with_variant(
    postgresql.ARRAY(sa.String(length=50)), "postgresql"
)


def _frozen_conversion_drafts() -> sa.Table:
    """``conversion_drafts`` as 008 left it, plus the nullable ``content`` this
    revision adds before the rebuild.

    Frozen here because migrations are history: the ORM moves on, this does not.
    No revision between 008 and this one alters the table.
    """
    meta = sa.MetaData()
    table = sa.Table(
        "conversion_drafts",
        meta,
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("enterprise_id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=True),
        sa.Column("conversion_id", sa.String(length=36), nullable=False),
        sa.Column("knowledge_item_id", sa.String(length=36), nullable=True),
        sa.Column("verified_by", sa.String(length=36), nullable=True),
        sa.Column("runbook_id", sa.String(length=100), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("file_path", sa.String(length=500), nullable=False),
        sa.Column(
            "status", sa.String(length=20), server_default="draft", nullable=False
        ),
        sa.Column(
            "source_type",
            sa.String(length=20),
            server_default="document",
            nullable=False,
        ),
        sa.Column(
            "document_type",
            sa.String(length=50),
            server_default="runbook",
            nullable=True,
        ),
        sa.Column("domain", sa.String(length=50), nullable=True),
        sa.Column("service", sa.String(length=100), nullable=True),
        sa.Column("severity", sa.String(length=20), nullable=True),
        sa.Column("tags", _TAGS_ARRAY, nullable=True),
        sa.Column(
            "validation_passed", sa.Boolean(), server_default="1", nullable=False
        ),
        sa.Column("validation_errors", sa.JSON(), nullable=True),
        sa.Column("validation_warnings", sa.JSON(), nullable=True),
        sa.Column("quality_score", sa.Numeric(precision=5, scale=1), nullable=True),
        sa.Column("quality_details", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("content", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "severity IS NULL OR severity IN "
            "('critical', 'high', 'medium', 'low', 'info')",
            name="conversion_drafts_severity_check",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'verified', 'discarded')",
            name="conversion_drafts_status_check",
        ),
        sa.ForeignKeyConstraint(
            ["conversion_id"], ["conversion_jobs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["enterprise_id"], ["enterprises.enterprise_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["knowledge_item_id"], ["knowledge_items.item_id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.organization_id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["verified_by"], ["users.user_id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("conversion_id", "enterprise_id", "organization_id"):
        sa.Index(f"ix_conversion_drafts_{column}", table.c[column])
    sa.Index("ix_conversion_drafts_tags", table.c.tags, postgresql_using="gin")
    sa.Index(
        "uq_conversion_drafts_enterprise_runbook_id",
        table.c.enterprise_id,
        table.c.runbook_id,
        unique=True,
        sqlite_where=sa.text("status <> 'discarded'"),
        postgresql_where=sa.text("status <> 'discarded'"),
    )
    return table


def _read_draft_file(file_path: Optional[str], knowledge_dir: Path) -> Optional[str]:
    """The text of the file ``file_path`` names, or ``None``.

    Read only from strictly inside ``knowledge_dir`` once both sides are
    resolved (symlinks included), the containment rule the application applied
    to every draft path: a row's path is a database value, and this migration
    must not read an arbitrary file into a row a client is served.
    """
    if not file_path:
        return None
    try:
        root = knowledge_dir.resolve()
        candidate = Path(file_path).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if candidate == root or not candidate.is_relative_to(root):
        return None
    try:
        return candidate.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _refuse_if_standalone_would_lose_drafts() -> None:
    """SQLite, online: refuse before anything changes when no ``data/knowledge``
    is here to read and step 3 would discard a draft."""
    context = op.get_context()
    if context.as_sql or context.dialect.name != "sqlite":
        return
    if KNOWLEDGE_DIR.is_dir():
        return
    at_risk = op.get_bind().execute(COUNT_LIVE_WITHOUT_PUBLISHED_TEXT).scalar()
    if at_risk:
        raise RuntimeError(
            f"018 refused: {at_risk} live runbook draft(s) keep their text only "
            f"in files under {KNOWLEDGE_DIR}/, which does not exist in the "
            f"working directory ({Path.cwd()}). The migration reads those files "
            "relative to where it runs: run it from the application root (the "
            "app's own startup run does), or discard those drafts first. Nothing "
            "was changed."
        )


def _fill_from_files() -> None:
    """Step 2: live, still-empty rows take their file's text."""
    if op.get_context().as_sql:
        op.execute(
            "-- 018 step 2 (each live draft's text from its file under "
            "data/knowledge) reads files and cannot run offline; drafts it would "
            "fill are discarded by step 3 below"
        )
        return
    bind = op.get_bind()
    filled = 0
    for draft_id, file_path in bind.execute(SELECT_LIVE_EMPTY).fetchall():
        content = _read_draft_file(file_path, KNOWLEDGE_DIR)
        if content is None:
            continue
        bind.execute(FILL_ONE, {"content": content, "id": draft_id})
        filled += 1
    _log.info("018: filled %d draft(s) from their files", filled)


def _execute_counted(statement, label: str) -> None:
    """Run one data statement; log its row count when there is a connection."""
    if op.get_context().as_sql:
        op.execute(statement)
        return
    count = op.get_bind().execute(statement).rowcount
    _log.info("018: %s: %d row(s)", label, count)


def _discard_drafts_without_text() -> None:
    """Step 3: release each drained live-case key, then discard."""
    if not op.get_context().as_sql:
        ids = [row[0] for row in op.get_bind().execute(SELECT_TO_DISCARD)]
        _log.info(
            "018: discarding %d live draft(s) whose text is gone: %s",
            len(ids),
            ", ".join(ids) or "none",
        )
    _execute_counted(RELEASE_LIVE_CASE_KEYS, "released live case keys")
    _execute_counted(DISCARD_EMPTY_LIVE, "discarded drafts with no text")


def _settle_content() -> None:
    _execute_counted(
        FILL_VERIFIED_FROM_KNOWLEDGE_ITEMS,
        "filled verified drafts from knowledge_items",
    )
    _fill_from_files()
    _discard_drafts_without_text()
    _execute_counted(EMPTY_THE_REST, "set discarded drafts' text to ''")


def upgrade() -> None:
    """Give every draft its text in ``content``; drop ``file_path``."""
    context = op.get_context()
    if context.as_sql:
        if context.dialect.name == "sqlite":
            raise RuntimeError(
                "018 cannot be rendered with --sql on SQLite: step (b) reads "
                "draft files; standalone migrates online from the app root"
            )
        op.execute(OFFLINE_WARNING)
    _refuse_if_standalone_would_lose_drafts()
    op.add_column("conversion_drafts", sa.Column("content", sa.Text(), nullable=True))

    if op.get_context().dialect.name == "postgresql":
        op.execute(ROW_SECURITY_OFF)
        _settle_content()
        op.execute(ROW_SECURITY_RESTORED)
        op.alter_column(
            "conversion_drafts", "content", existing_type=sa.Text(), nullable=False
        )
        op.drop_column("conversion_drafts", "file_path")
        return

    _settle_content()
    with op.batch_alter_table(
        "conversion_drafts",
        copy_from=_frozen_conversion_drafts(),
        recreate="always",
    ) as batch:
        batch.alter_column("content", existing_type=sa.Text(), nullable=False)
        batch.drop_column("file_path")


def downgrade() -> None:
    """Re-add ``file_path`` (nullable, empty) and drop ``content``. The text is
    not restored anywhere."""
    op.add_column(
        "conversion_drafts",
        sa.Column("file_path", sa.String(length=500), nullable=True),
    )
    op.drop_column("conversion_drafts", "content")
