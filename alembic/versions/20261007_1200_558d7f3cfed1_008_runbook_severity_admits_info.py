"""008_runbook_severity_admits_info

``conversion_drafts_severity_check`` admits the runbook severity vocabulary,
``info`` included (#1886).

The runbook spec (runbook-content-architecture.md §Taxonomy Schema) defines
``severity`` as ``critical, high, medium, low, info``. The baseline's CHECK was a
hand copy of it that lost ``info``, so a runbook the validator passed with
``severity: info`` failed at verify, when the frontmatter value is written into
this column. The vocabulary now has one owner in code
(``faultmaven.modules.knowledge.taxonomy.RunbookSeverity``) and the ORM builds
this CHECK from it.

The constraint text here is frozen, not built from the enum: migrations are
history, and a revision that imported the enum would rewrite what it did the day
the enum changed. What keeps the two equal is
``test_runbook_taxonomy_one_owner.py``, which migrates a database and compares
the CHECK's value set with the enum's; a change to the vocabulary fails it until
a new revision moves the constraint.

Dialects
--------

PostgreSQL drops and re-adds the constraint in place. No row changes, so the
upgrade needs no ``row_security`` setting: every existing row already
satisfies the wider set. The downgrade's guard COUNTS rows, and runs that count
under ``SET LOCAL row_security = off`` (restored to ``DEFAULT`` after), the
choice and reasoning of revisions 006 and 007: the count is tenant-wide by
construction, and a role the policy would filter raises rather than counting a
subset.

SQLite cannot alter a CHECK, so the table is rebuilt (``batch_alter_table`` from
a frozen copy of the baseline definition, ``recreate="always"``). Unlike 007's
``hypotheses``, no table references ``conversion_drafts`` by foreign key, so
dropping the old table runs no ON DELETE action whatever ``PRAGMA foreign_keys``
says, and there is nothing for a guard to refuse; a test pins that no foreign
key targets this table, so the claim cannot go stale silently. The table has no
triggers. Its indexes, the partial unique ``(enterprise_id, runbook_id)`` among
them, are part of the frozen definition and come back with it.

``downgrade()``
---------------

Restores the four-value CHECK. A row holding ``info`` cannot survive that, and
neither guessing a severity nor erasing one is this revision's call, so the
downgrade REFUSES while any such row exists and names the count. The operator
decides: re-grade or clear those drafts (``UPDATE conversion_drafts SET
severity = NULL WHERE severity = 'info'`` keeps the runbook files, which still
carry the value in their frontmatter), then downgrade.

Revision ID: 558d7f3cfed1
Revises: 497ae8900ae2
Create Date: 2026-10-07 12:00:00
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "558d7f3cfed1"  # pragma: allowlist secret
down_revision: Union[str, Sequence[str], None] = "497ae8900ae2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SEVERITY_CHECK = "conversion_drafts_severity_check"
#: The spec's vocabulary, in its order.
SEVERITY_WITH_INFO = (
    "severity IS NULL OR severity IN ('critical', 'high', 'medium', 'low', 'info')"
)
#: The baseline's constraint, verbatim.
SEVERITY_WITHOUT_INFO = (
    "severity IS NULL OR severity IN ('low', 'medium', 'high', 'critical')"
)

COUNT_INFO_ROWS = text("SELECT COUNT(*) FROM conversion_drafts WHERE severity = 'info'")

#: ``conversion_drafts`` is tenant-scoped. The count runs with row security off
#: so it is every enterprise's by construction, as 006's and 007's UPDATEs are:
#: the migrating role owns the table and is exempt anyway, and a role the
#: policy would filter raises here instead of counting one enterprise's rows and
#: letting the downgrade proceed over the others'.
ROW_SECURITY_OFF = "SET LOCAL row_security = off"
#: Back to the value before, for whatever runs later in the same transaction.
ROW_SECURITY_RESTORED = "SET LOCAL row_security TO DEFAULT"

_TAGS_ARRAY = sa.Text().with_variant(
    postgresql.ARRAY(sa.String(length=50)), "postgresql"
)


def _frozen_conversion_drafts(severity_check: str) -> sa.Table:
    """The baseline ``conversion_drafts`` table, with the given severity CHECK.

    Frozen here because migrations are history: the ORM moves on, this does not.
    No revision between the baseline and this one alters the table.
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
        sa.CheckConstraint(severity_check, name=SEVERITY_CHECK),
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


def _set_severity_check(*, check: str, old_check: str) -> None:
    if op.get_context().dialect.name == "postgresql":
        op.drop_constraint(SEVERITY_CHECK, "conversion_drafts", type_="check")
        op.create_check_constraint(SEVERITY_CHECK, "conversion_drafts", check)
        return
    with op.batch_alter_table(
        "conversion_drafts",
        copy_from=_frozen_conversion_drafts(old_check),
        recreate="always",
    ) as batch:
        batch.drop_constraint(SEVERITY_CHECK, type_="check")
        batch.create_check_constraint(SEVERITY_CHECK, check)


def _refuse_while_info_rows_exist() -> None:
    """The four-value CHECK cannot hold an ``info`` row; refuse, naming them."""
    context = op.get_context()
    if context.as_sql:
        return
    postgresql_dialect = context.dialect.name == "postgresql"
    bind = op.get_bind()
    if postgresql_dialect:
        bind.execute(text(ROW_SECURITY_OFF))
    count = bind.execute(COUNT_INFO_ROWS).scalar()
    if postgresql_dialect:
        bind.execute(text(ROW_SECURITY_RESTORED))
    if count:
        raise RuntimeError(
            f"008 downgrade refused: {count} conversion_drafts row(s) hold "
            "severity 'info', which the parent revision's CHECK does not admit. "
            "Re-grade them or clear the column (UPDATE conversion_drafts SET "
            "severity = NULL WHERE severity = 'info'; the runbook files keep the "
            "value), then downgrade again."
        )


def upgrade() -> None:
    """Widen the severity CHECK to the spec's vocabulary, ``info`` included."""
    _set_severity_check(check=SEVERITY_WITH_INFO, old_check=SEVERITY_WITHOUT_INFO)


def downgrade() -> None:
    """Restore the baseline's four-value CHECK; refuse while ``info`` rows exist."""
    _refuse_while_info_rows_exist()
    _set_severity_check(check=SEVERITY_WITHOUT_INFO, old_check=SEVERITY_WITH_INFO)
