"""013_evidence_is_submitted_data

Evidence is submitted data (#1930): a row with no ``source_file_id`` is legal only
for a fact the ENGINE derived, never for anything the user wrote in a chat message.

``evidence_source_invariant`` changes from
``source_file_id IS NOT NULL OR source_type = 'user_description'`` to
``source_file_id IS NOT NULL OR source_type = 'engine_inference'``, and the
``user_description`` source type leaves the vocabulary. The rows that carried it
are settled first, in this order:

1. **The engine's own rows** (``collected_by = 'engine'``: M6's failed-treatment
   disconfirmation) were typed ``user_description`` only because the old CHECK
   admitted no other fileless type. They become ``engine_inference``.
2. **Rows with a file** keep their file and become ``text``. The old validator let
   the model label a file-backed row ``user_description``; the file is the source,
   the label was wrong.
3. **The rest are DELETED**: model-written rows quoting the user's chat message,
   with no file. Under the invariant they were never evidence, and no source type
   can hold them. Their links go with them, deleted explicitly
   (``hypothesis_evidence``, ``causal_node_evidence``, ``evidence_need_fulfillment``,
   ``case_entities``) and ``solutions.verification_evidence_id`` is cleared:
   PostgreSQL would run those ON DELETE actions itself, SQLite under this
   revision's connection does not (see Dialects), and one explicit statement
   per table gives the same result on both. Each count is logged.

   What the deletion changes: the engine recomputes cause and resolution
   assurance from the rows on record, terminal cases included, so a RESOLVED case
   whose absence rows were chat quotes now reads as unconfirmed. Its state, its
   history and its written reports are untouched. Ids of deleted rows left inside
   case JSON (hypothesis evidence lists, conclusion evidence ids) are skipped by
   every reader.

Dialects
--------

PostgreSQL drops and re-adds the CHECK in place. The data statements run under
``SET LOCAL row_security = off`` (restored to ``DEFAULT`` after), the choice and
reasoning of revisions 006-008: they are tenant-wide by construction, and a role
the policy would filter raises rather than settling one enterprise's rows.

SQLite cannot alter a CHECK, so the table is rebuilt (``batch_alter_table`` from a
frozen copy of the baseline definition, ``recreate="always"``). Five tables
reference ``evidence`` by foreign key (four CASCADE, one SET NULL), and SQLite
runs those actions when the old table is dropped if foreign keys are enforced.
``env.py`` does not enforce them; the revision checks that and refuses to run
otherwise, as 007 does for ``hypotheses``.

``downgrade()``
---------------

Restores the old CHECK and re-types the engine's rows ``user_description``. The
deleted rows cannot come back, and the rows step 2 re-typed stay ``text`` (each
still has its file, which the old CHECK admits).

Revision ID: 9b3e5c71d2a4
Revises: 6c1f0d2a9b47
Create Date: 2026-10-11 12:00:00
"""

import logging
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "9b3e5c71d2a4"  # pragma: allowlist secret
down_revision: Union[str, Sequence[str], None] = "6c1f0d2a9b47"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_log = logging.getLogger("alembic.runtime.migration")

SOURCE_CHECK = "evidence_source_invariant"
#: Only an engine-derived fact may have no file.
SOURCE_ENGINE_INFERENCE = (
    "source_file_id IS NOT NULL OR source_type = 'engine_inference'"
)
#: The baseline's constraint, verbatim.
SOURCE_USER_DESCRIPTION = (
    "source_file_id IS NOT NULL OR source_type = 'user_description'"
)

#: The rows step 3 deletes, as a subquery every link statement shares.
_CHAT_QUOTE_ROWS = (
    "SELECT evidence_id FROM evidence WHERE source_type = 'user_description' "
    "AND source_file_id IS NULL"
)

RETYPE_ENGINE_ROWS = text(
    "UPDATE evidence SET source_type = 'engine_inference' "
    "WHERE source_type = 'user_description' AND collected_by = 'engine'"
)
RETYPE_FILE_BACKED_ROWS = text(
    "UPDATE evidence SET source_type = 'text' "
    "WHERE source_type = 'user_description' AND source_file_id IS NOT NULL"
)
#: (label, statement): the links first, then the rows.
DELETE_CHAT_QUOTES = (
    (
        "hypothesis_evidence links",
        text(
            f"DELETE FROM hypothesis_evidence WHERE evidence_id IN ({_CHAT_QUOTE_ROWS})"
        ),
    ),
    (
        "causal_node_evidence links",
        text(
            f"DELETE FROM causal_node_evidence WHERE evidence_id IN ({_CHAT_QUOTE_ROWS})"
        ),
    ),
    (
        "evidence_need_fulfillment links",
        text(
            "DELETE FROM evidence_need_fulfillment "
            f"WHERE evidence_id IN ({_CHAT_QUOTE_ROWS})"
        ),
    ),
    (
        "case_entities mentions",
        text(f"DELETE FROM case_entities WHERE evidence_id IN ({_CHAT_QUOTE_ROWS})"),
    ),
    (
        "solutions verification references",
        text(
            "UPDATE solutions SET verification_evidence_id = NULL "
            f"WHERE verification_evidence_id IN ({_CHAT_QUOTE_ROWS})"
        ),
    ),
    (
        "chat-quote evidence rows",
        text(f"DELETE FROM evidence WHERE evidence_id IN ({_CHAT_QUOTE_ROWS})"),
    ),
)
RETYPE_ENGINE_ROWS_BACK = text(
    "UPDATE evidence SET source_type = 'user_description' "
    "WHERE source_type = 'engine_inference'"
)

ROW_SECURITY_OFF = "SET LOCAL row_security = off"
ROW_SECURITY_RESTORED = "SET LOCAL row_security TO DEFAULT"

_TAGS_ARRAY = sa.Text().with_variant(
    postgresql.ARRAY(sa.String(length=50)), "postgresql"
)


def _frozen_evidence(source_check: str) -> sa.Table:
    """The baseline ``evidence`` table, with the given source CHECK.

    Frozen here because migrations are history: the ORM moves on, this does not.
    No revision between the baseline and this one alters the table.
    """
    meta = sa.MetaData()
    table = sa.Table(
        "evidence",
        meta,
        sa.Column("evidence_id", sa.String(length=36), nullable=False),
        sa.Column("enterprise_id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=True),
        sa.Column("case_id", sa.String(length=36), nullable=False),
        sa.Column("source_file_id", sa.String(length=36), nullable=True),
        sa.Column("category", sa.String(length=50), nullable=False),
        sa.Column("source_type", sa.String(length=50), nullable=True),
        sa.Column(
            "primary_purpose",
            sa.String(length=100),
            server_default="legacy",
            nullable=False,
        ),
        sa.Column("analysis", sa.Text(), nullable=True),
        sa.Column("processing_mode", sa.String(length=50), nullable=True),
        sa.Column("advances_milestones", _TAGS_ARRAY, nullable=True),
        sa.Column("summary", sa.String(length=500), nullable=False),
        sa.Column("extract", sa.Text(), nullable=True),
        sa.Column("is_primary", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("reliability_score", sa.Float(), nullable=True),
        sa.Column("tags", _TAGS_ARRAY, nullable=True),
        sa.Column("collected_at_turn", sa.Integer(), nullable=True),
        sa.Column("coverage_start_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("coverage_end_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("coverage_source", sa.String(length=50), nullable=True),
        sa.Column("vectorized", sa.Boolean(), server_default="0", nullable=False),
        sa.Column(
            "collected_by",
            sa.String(length=50),
            server_default="system",
            nullable=False,
        ),
        sa.Column(
            "metadata",
            sa.Text().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint(source_check, name=SOURCE_CHECK),
        sa.CheckConstraint(
            "LENGTH(TRIM(summary)) > 0", name="evidence_summary_not_empty"
        ),
        sa.CheckConstraint(
            "extract IS NULL OR LENGTH(TRIM(extract)) > 0",
            name="evidence_extract_not_empty",
        ),
        sa.CheckConstraint(
            "reliability_score IS NULL OR (reliability_score >= 0 AND reliability_score <= 1)",
            name="evidence_reliability_range",
        ),
        sa.ForeignKeyConstraint(["case_id"], ["cases.case_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["enterprise_id"], ["enterprises.enterprise_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.organization_id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["source_file_id"], ["uploaded_files.file_id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("evidence_id"),
    )
    for column in (
        "case_id",
        "category",
        "collected_at_turn",
        "enterprise_id",
        "organization_id",
        "source_file_id",
    ):
        sa.Index(f"ix_evidence_{column}", table.c[column])
    sa.Index("ix_evidence_case_is_primary", table.c.case_id, table.c.is_primary)
    sa.Index(
        "ix_evidence_coverage",
        table.c.case_id,
        table.c.coverage_start_ts,
        table.c.coverage_end_ts,
    )
    sa.Index("ix_evidence_tags", table.c.tags)
    return table


def _execute_counted(statement, label: str) -> None:
    """Run one data statement; log its row count when there is a connection."""
    if op.get_context().as_sql:
        op.execute(statement)
        return
    count = op.get_bind().execute(statement).rowcount
    _log.info("013: %s on %d row(s)", label, count)


def _refuse_cascading_rebuild() -> None:
    """SQLite runs ON DELETE actions when a referenced table is dropped if
    foreign keys are enforced; the rebuild would then delete every link to every
    evidence row. env.py does not enforce them — refuse if something has turned
    them on."""
    if op.get_context().as_sql:
        return
    enforced = op.get_bind().execute(text("PRAGMA foreign_keys")).scalar()
    if enforced:
        raise RuntimeError(
            "013 rebuilds the evidence table on SQLite and must run with "
            "PRAGMA foreign_keys=OFF: with it on, dropping the old table runs the "
            "ON DELETE actions of hypothesis_evidence, causal_node_evidence, "
            "evidence_need_fulfillment, case_entities (CASCADE) and solutions "
            "(SET NULL)."
        )


def _replace_source_constraint(*, check: str, old_check: str, data_steps) -> None:
    """Swap the source CHECK, running ``data_steps`` while neither CHECK binds.

    Neither constraint admits every intermediate state: the old one refuses an
    ``engine_inference`` row with no file, the new one a ``user_description``
    row with no file, and each direction has rows of both kinds until its data
    steps finish. So the rows are settled with the constraint lifted, and the
    new constraint is then checked against every remaining row: PostgreSQL
    validates on ``ADD CONSTRAINT``, and SQLite's rebuild copies each row
    through the new CHECK.
    """
    if op.get_context().dialect.name == "postgresql":
        op.drop_constraint(SOURCE_CHECK, "evidence", type_="check")
        op.execute(ROW_SECURITY_OFF)
        data_steps()
        op.execute(ROW_SECURITY_RESTORED)
        op.create_check_constraint(SOURCE_CHECK, "evidence", check)
        return
    _refuse_cascading_rebuild()
    # SQLite cannot drop a CHECK without a rebuild; lift enforcement for the data
    # steps instead, and restore it before the rebuild so the copy is checked.
    op.execute("PRAGMA ignore_check_constraints = ON")
    data_steps()
    op.execute("PRAGMA ignore_check_constraints = OFF")
    with op.batch_alter_table(
        "evidence",
        copy_from=_frozen_evidence(old_check),
        recreate="always",
    ) as batch:
        batch.drop_constraint(SOURCE_CHECK, type_="check")
        batch.create_check_constraint(SOURCE_CHECK, check)


def _settle_user_description_rows() -> None:
    _execute_counted(RETYPE_ENGINE_ROWS, "re-typed engine rows engine_inference")
    _execute_counted(RETYPE_FILE_BACKED_ROWS, "re-typed file-backed rows text")
    for label, statement in DELETE_CHAT_QUOTES:
        _execute_counted(statement, f"deleted {label}")


def _restore_engine_rows() -> None:
    _execute_counted(RETYPE_ENGINE_ROWS_BACK, "re-typed engine rows user_description")


def upgrade() -> None:
    """Settle every ``user_description`` row, then admit a fileless row only for
    ``engine_inference``."""
    _replace_source_constraint(
        check=SOURCE_ENGINE_INFERENCE,
        old_check=SOURCE_USER_DESCRIPTION,
        data_steps=_settle_user_description_rows,
    )


def downgrade() -> None:
    """Restore the old CHECK and re-type the engine's rows ``user_description``.
    The deleted chat-quote rows are not restored."""
    _replace_source_constraint(
        check=SOURCE_USER_DESCRIPTION,
        old_check=SOURCE_ENGINE_INFERENCE,
        data_steps=_restore_engine_rows,
    )
