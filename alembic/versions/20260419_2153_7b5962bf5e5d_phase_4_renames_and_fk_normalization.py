"""phase_4_renames_and_fk_normalization

Storage redesign 2026-04 — Phase 4: mechanical schema cleanup.

This migration applies four sets of changes (in order) per
deployment-schema-strategy.md §4.2, §4.3, §12 decisions #2, #3, #13, #19:

1. Rename ``evidence.source_type_new`` column → ``source_type``. Finishes
   the abandoned column-rename migration; the ORM ``name=`` alias has been
   dropped from ``EvidenceModel``.

2. Rename ``agent_tool_calls_v2`` table → canonical ``agent_tool_calls``.
   The v1 ``agent_tool_calls`` table was deleted in Phase 1 (zero
   functional usage), so v2 now takes the canonical name. Indexes and
   CHECK constraints are renamed to drop the ``_v2`` suffix.

3. Type change ``evidence.file_size``: ``Integer`` nullable → ``BigInteger``
   NOT NULL with ``server_default '0'``. Avoids the 2 GB ``Integer`` cap
   and enforces "size always known".

4. FK width normalization: every entity-id PK and every foreign-key column
   referencing one is normalized to ``VARCHAR(36)``. Net widening for
   ``cases``/``evidence``/``hypotheses``/``solutions``/``checkpoints``/etc.
   from short widths (15/17/20/30/64) and net narrowing for
   ``organizations``/``role``/``permission``/``execution``/``session``/``item``/
   ``suggestion``/``tool_call`` from 64-wide to 36. Excludes auth artifact
   columns (``oauth_revoked_tokens.jti``, ``oauth_authorization_codes.code``)
   and non-id Strings (names, emails, enum-value columns, etc.).

SQLite parity: column type changes use ``batch_alter_table``. SQLite does
not enforce VARCHAR length, so the column-width changes are mainly for PG
metadata consistency, but we emit them on both dialects so the schema is
consistent.

Revision ID: 7b5962bf5e5d
Revises: 575bb832f512
Create Date: 2026-04-19 21:53:25.799581

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "7b5962bf5e5d"
down_revision: Union[str, Sequence[str], None] = "575bb832f512"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ---------------------------------------------------------------------------
# Width policy (per strategy doc §4.3): all entity id PKs and FK columns
# referencing them are VARCHAR(36). The lists below capture every width
# change applied by this migration, so upgrade() and downgrade() stay in
# sync. Tuples are (table_name, column_name, original_width).
# ---------------------------------------------------------------------------

# Net WIDENING (original width < 36). Upgrade widens; downgrade narrows back.
WIDENED_TO_36 = [
    # cases (PK + tenancy)
    ("cases", "case_id", 17),
    ("cases", "organization_id", 20),
    ("cases", "team_id", 20),
    # evidence (PK + tenancy + FK case_id + source_file_id)
    ("evidence", "evidence_id", 15),
    ("evidence", "case_id", 17),
    ("evidence", "source_file_id", 20),
    # hypotheses
    ("hypotheses", "hypothesis_id", 15),
    ("hypotheses", "case_id", 17),
    ("hypotheses", "organization_id", 20),
    # solutions
    ("solutions", "solution_id", 15),
    ("solutions", "case_id", 17),
    ("solutions", "organization_id", 20),
    # case_messages
    ("case_messages", "message_id", 20),
    ("case_messages", "case_id", 17),
    # uploaded_files
    ("uploaded_files", "file_id", 15),
    ("uploaded_files", "case_id", 17),
    # case_actions / case_tags / case_checkpoints
    ("case_actions", "case_id", 17),
    ("case_tags", "case_id", 17),
    ("case_checkpoints", "checkpoint_id", 50),
    ("case_checkpoints", "case_id", 17),
    # agent_executions
    ("agent_executions", "case_id", 17),
    # investigation_sessions
    ("investigation_sessions", "case_id", 17),
    # reports
    ("reports", "case_id", 17),
    # roles / permissions (auth)
    ("roles", "role_id", 20),
    ("permissions", "permission_id", 30),
    ("role_permissions", "role_id", 20),
    ("role_permissions", "permission_id", 30),
    ("organization_members", "role_id", 20),
]

# Net NARROWING (original width > 36 → 36). Upgrade narrows; downgrade
# widens back to the original width.
NARROWED_TO_36 = [
    # organizations
    ("organizations", "organization_id", 64),
    ("organization_members", "organization_id", 64),
    ("teams", "organization_id", 64),
    ("user_audit_log", "organization_id", 64),
    # evidence + multi-tenancy fields that were 64-wide on the case-domain tables
    ("evidence", "organization_id", 64),
    ("case_messages", "organization_id", 64),
    ("uploaded_files", "organization_id", 64),
    ("case_actions", "organization_id", 64),
    ("case_tags", "organization_id", 64),
    ("case_checkpoints", "organization_id", 64),
    ("agent_executions", "execution_id", 64),
    ("agent_executions", "organization_id", 64),
    ("agent_executions", "session_id", 64),
    ("investigation_sessions", "session_id", 64),
    ("investigation_sessions", "user_id", 255),
    ("investigation_sessions", "organization_id", 64),
    # knowledge
    ("knowledge_items", "item_id", 64),
    ("knowledge_items", "organization_id", 64),
    ("knowledge_items", "source_suggestion_id", 64),
    ("knowledge_suggestions", "suggestion_id", 64),
    ("knowledge_suggestions", "organization_id", 64),
    ("knowledge_suggestions", "case_id", 64),
    ("knowledge_suggestions", "knowledge_item_id", 64),
]


# Tables that need a batch_alter_table for the agent_tool_calls rename of
# its own FK width — handled together with the table rename below.


def upgrade() -> None:
    """Apply Phase 4 schema cleanup: column rename → table rename → type
    change → FK width normalization."""
    bind = op.get_bind()
    dialect = bind.dialect.name

    # ------------------------------------------------------------------
    # 1. Rename evidence.source_type_new column → source_type
    # ------------------------------------------------------------------
    with op.batch_alter_table("evidence") as batch_op:
        batch_op.alter_column(
            "source_type_new",
            new_column_name="source_type",
            existing_type=sa.String(length=50),
            existing_nullable=True,
        )

    # ------------------------------------------------------------------
    # 2. Rename agent_tool_calls_v2 table → agent_tool_calls
    # ------------------------------------------------------------------
    # PG: rename indexes explicitly first (PG enforces index renames; SQLite
    # rebuilds the table when it's renamed and recreates indexes
    # automatically with the new table name).
    if dialect == "postgresql":
        for old, new in [
            ("ix_agent_tool_calls_v2_created_at", "ix_agent_tool_calls_created_at"),
            ("ix_agent_tool_calls_v2_execution_id", "ix_agent_tool_calls_execution_id"),
            (
                "ix_agent_tool_calls_v2_organization_id",
                "ix_agent_tool_calls_organization_id",
            ),
            ("ix_agent_tool_calls_v2_status", "ix_agent_tool_calls_status"),
            ("ix_agent_tool_calls_v2_tool_name", "ix_agent_tool_calls_tool_name"),
        ]:
            op.execute(f'ALTER INDEX "{old}" RENAME TO "{new}"')
        # CHECK constraints are renamed via DROP+ADD; SQLite handles them
        # automatically when the table is rebuilt.
        op.execute(
            'ALTER TABLE "agent_tool_calls_v2" '
            'RENAME CONSTRAINT "agent_tool_calls_v2_status_check" '
            'TO "agent_tool_calls_status_check"'
        )
        op.execute(
            'ALTER TABLE "agent_tool_calls_v2" '
            'RENAME CONSTRAINT "agent_tool_calls_v2_duration_check" '
            'TO "agent_tool_calls_duration_check"'
        )

    op.rename_table("agent_tool_calls_v2", "agent_tool_calls")

    # ------------------------------------------------------------------
    # 3. Type change evidence.file_size: Integer nullable → BigInteger NOT NULL
    # ------------------------------------------------------------------
    with op.batch_alter_table("evidence") as batch_op:
        batch_op.alter_column(
            "file_size",
            existing_type=sa.Integer(),
            type_=sa.BigInteger(),
            nullable=False,
            server_default="0",
        )

    # ------------------------------------------------------------------
    # 4. FK width normalization → VARCHAR(36)
    # ------------------------------------------------------------------
    # Group changes per table so each table is rebuilt at most once on SQLite.
    all_changes = WIDENED_TO_36 + NARROWED_TO_36
    by_table: dict[str, list[tuple[str, int]]] = {}
    for table, column, original_width in all_changes:
        by_table.setdefault(table, []).append((column, original_width))

    for table, columns in by_table.items():
        with op.batch_alter_table(table) as batch_op:
            for column, original_width in columns:
                batch_op.alter_column(
                    column,
                    existing_type=sa.String(length=original_width),
                    type_=sa.String(length=36),
                )

    # The `agent_tool_calls` table also needs FK widths normalized — its
    # own PK (tool_call_id), FK (execution_id), and tenancy column
    # (organization_id) were 64-wide. Apply post-rename so we operate on
    # the new table name.
    with op.batch_alter_table("agent_tool_calls") as batch_op:
        for column in ("tool_call_id", "execution_id", "organization_id"):
            batch_op.alter_column(
                column,
                existing_type=sa.String(length=64),
                type_=sa.String(length=36),
            )


def downgrade() -> None:
    """Reverse Phase 4 changes in inverse order: FK widths back → type
    change back → table rename back → column rename back."""
    bind = op.get_bind()
    dialect = bind.dialect.name

    # ------------------------------------------------------------------
    # Reverse 4: FK widths back to originals.
    # ------------------------------------------------------------------
    # Reverse the agent_tool_calls width normalization first (still under
    # the post-rename name).
    with op.batch_alter_table("agent_tool_calls") as batch_op:
        for column in ("tool_call_id", "execution_id", "organization_id"):
            batch_op.alter_column(
                column,
                existing_type=sa.String(length=36),
                type_=sa.String(length=64),
            )

    all_changes = WIDENED_TO_36 + NARROWED_TO_36
    by_table: dict[str, list[tuple[str, int]]] = {}
    for table, column, original_width in all_changes:
        by_table.setdefault(table, []).append((column, original_width))

    for table, columns in by_table.items():
        with op.batch_alter_table(table) as batch_op:
            for column, original_width in columns:
                batch_op.alter_column(
                    column,
                    existing_type=sa.String(length=36),
                    type_=sa.String(length=original_width),
                )

    # ------------------------------------------------------------------
    # Reverse 3: file_size BigInteger NOT NULL → Integer nullable.
    # ------------------------------------------------------------------
    with op.batch_alter_table("evidence") as batch_op:
        batch_op.alter_column(
            "file_size",
            existing_type=sa.BigInteger(),
            type_=sa.Integer(),
            nullable=True,
            server_default=None,
        )

    # ------------------------------------------------------------------
    # Reverse 2: rename agent_tool_calls table back to agent_tool_calls_v2.
    # ------------------------------------------------------------------
    op.rename_table("agent_tool_calls", "agent_tool_calls_v2")
    if dialect == "postgresql":
        op.execute(
            'ALTER TABLE "agent_tool_calls_v2" '
            'RENAME CONSTRAINT "agent_tool_calls_status_check" '
            'TO "agent_tool_calls_v2_status_check"'
        )
        op.execute(
            'ALTER TABLE "agent_tool_calls_v2" '
            'RENAME CONSTRAINT "agent_tool_calls_duration_check" '
            'TO "agent_tool_calls_v2_duration_check"'
        )
        for old, new in [
            ("ix_agent_tool_calls_created_at", "ix_agent_tool_calls_v2_created_at"),
            ("ix_agent_tool_calls_execution_id", "ix_agent_tool_calls_v2_execution_id"),
            (
                "ix_agent_tool_calls_organization_id",
                "ix_agent_tool_calls_v2_organization_id",
            ),
            ("ix_agent_tool_calls_status", "ix_agent_tool_calls_v2_status"),
            ("ix_agent_tool_calls_tool_name", "ix_agent_tool_calls_v2_tool_name"),
        ]:
            op.execute(f'ALTER INDEX "{old}" RENAME TO "{new}"')

    # ------------------------------------------------------------------
    # Reverse 1: rename evidence.source_type column back to source_type_new.
    # ------------------------------------------------------------------
    with op.batch_alter_table("evidence") as batch_op:
        batch_op.alter_column(
            "source_type",
            new_column_name="source_type_new",
            existing_type=sa.String(length=50),
            existing_nullable=True,
        )
