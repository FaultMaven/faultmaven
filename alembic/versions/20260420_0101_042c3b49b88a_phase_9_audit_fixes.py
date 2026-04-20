"""phase_9_audit_fixes

Storage redesign 2026-04 — Phase 9: residual fixes surfaced by the post-Phase-8
audit (`docs/working/ANALYSIS-design-check-storage.md`).

Schema changes:

1. **organization_id NOT NULL + FK on tenanted tables** (Audit Issue 1):
   The Phase 8 RLS policies depend on `organization_id` being a real,
   non-null tenant id. Previously most tenanted tables had a bare
   `String(36)` column with no FK and (for `cases`) nullable. The
   Phase 8 RLS policy was technically functional but the schema didn't
   enforce that every tenanted row had a tenant. This phase adds:
   - `cases.organization_id`: nullable → NOT NULL + FK to organizations
   - `cases.team_id`: bare → FK to teams (ON DELETE SET NULL)
   - 13 other tenanted tables: + FK to organizations on existing
     `nullable=False` `organization_id` column
   - `conversion_jobs.organization_id`: nullable → NOT NULL + FK
   - `conversion_jobs.team_id`: bare → FK to teams (ON DELETE SET NULL)

2. **Width policy normalization** (Audit Issue 8): five more columns
   missed by Phase 4:
   - `oauth_authorization_codes.user_id`: VARCHAR(255) → VARCHAR(36)
   - `knowledge_suggestions.extracted_by`: VARCHAR(64) → VARCHAR(36)
   - `knowledge_suggestions.reviewed_by`: VARCHAR(64) → VARCHAR(36)
   - `knowledge_suggestions.pii_remediated_by`: VARCHAR(64) → VARCHAR(36)
   - `knowledge_items.verified_by`: VARCHAR(64) → VARCHAR(36)

3. **`evidence.form` CHECK constraint** (Audit Issue 2):
   Phase 6 declared `EvidenceFormEnum` but the column wasn't bound to
   it via SQLAlchemy `Enum()`. SQLite had no enforcement; only PG (via
   the Phase 7 PG-only CHECK) caught bad values. This adds a Tier 1
   cross-dialect CHECK on SQLite (PG already has the equivalent from
   Phase 7).

Code-only changes accompanying this migration (no schema impact):
- `database.py:286` raw SQL → `text("SELECT 1")` wrapper (Audit Issue 6)
- `ToolCallStatusEnum` deleted from models.py — zero callers, mismatched
  the agent_tool_calls.status CHECK (Audit Issue 5)
- `DatabaseCaseRepository._case_to_model` now populates Phase-6
  first-class columns directly instead of relying on the cases.metadata
  JSON shadow copy (Audit Issue 4)
- `DatabaseCaseRepository._model_to_case` reads from columns first,
  falls back to metadata for legacy rows (Audit Issue 4)
- `cleanup_expired` filters on the closed_at column directly
  (Audit Issue 7)
- `init_database()` docstring strengthened to "TEST/DEV ONLY"
  (Audit Issue 8)
- `EvidenceModel.form` column declared as `Enum(EvidenceFormEnum,
  native_enum=False)` so the SQLEnum binding is enforced cross-dialect

Per audit recommendation Priority 1 + 2.

Revision ID: 042c3b49b88a
Revises: 729d00ba9049
Create Date: 2026-04-20 01:01:54.063691

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "042c3b49b88a"
down_revision: Union[str, Sequence[str], None] = "729d00ba9049"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Tables that gain an FK on their existing organization_id column.
# (cases is handled separately because it also flips nullable=False and
# adds the team_id FK.)
TENANTED_TABLES_FK_ONLY = [
    "evidence",
    "hypotheses",
    "solutions",
    "case_messages",
    "uploaded_files",
    "case_actions",
    "case_tags",
    "case_checkpoints",
    "agent_executions",
    "agent_tool_calls",
    "investigation_sessions",
    "knowledge_items",
    "knowledge_suggestions",
]


def upgrade() -> None:
    """Phase 9 audit fixes — schema-level changes."""
    bind = op.get_bind()
    dialect = bind.dialect.name

    # ------------------------------------------------------------------
    # 1. cases: organization_id nullable→NOT NULL + FK; team_id + FK
    # ------------------------------------------------------------------
    # Backfill any existing NULLs to the default org so the NOT NULL
    # constraint can be applied. (No-op on a clean schema.)
    op.execute(
        "UPDATE cases SET organization_id = '00000000-0000-0000-0000-000000000001' "
        "WHERE organization_id IS NULL"
    )
    with op.batch_alter_table("cases") as batch_op:
        batch_op.alter_column(
            "organization_id",
            existing_type=sa.String(length=36),
            nullable=False,
            server_default="00000000-0000-0000-0000-000000000001",
        )
        batch_op.create_foreign_key(
            "fk_cases_organization_id",
            "organizations",
            ["organization_id"],
            ["organization_id"],
        )
        batch_op.create_foreign_key(
            "fk_cases_team_id",
            "teams",
            ["team_id"],
            ["team_id"],
            ondelete="SET NULL",
        )

    # ------------------------------------------------------------------
    # 2. Other tenanted tables: add FK on organization_id
    # ------------------------------------------------------------------
    for table in TENANTED_TABLES_FK_ONLY:
        with op.batch_alter_table(table) as batch_op:
            batch_op.create_foreign_key(
                f"fk_{table}_organization_id",
                "organizations",
                ["organization_id"],
                ["organization_id"],
            )

    # ------------------------------------------------------------------
    # 3. conversion_jobs: organization_id nullable→NOT NULL + FK; team_id + FK
    # ------------------------------------------------------------------
    op.execute(
        "UPDATE conversion_jobs SET organization_id = '00000000-0000-0000-0000-000000000001' "
        "WHERE organization_id IS NULL"
    )
    with op.batch_alter_table("conversion_jobs") as batch_op:
        batch_op.alter_column(
            "organization_id",
            existing_type=sa.String(length=36),
            nullable=False,
            server_default="00000000-0000-0000-0000-000000000001",
        )
        batch_op.create_foreign_key(
            "fk_conversion_jobs_organization_id",
            "organizations",
            ["organization_id"],
            ["organization_id"],
        )
        batch_op.create_foreign_key(
            "fk_conversion_jobs_team_id",
            "teams",
            ["team_id"],
            ["team_id"],
            ondelete="SET NULL",
        )

    # ------------------------------------------------------------------
    # 4. Width normalization (VARCHAR → VARCHAR(36)) for entity-id refs
    #    missed by Phase 4.
    # ------------------------------------------------------------------
    with op.batch_alter_table("oauth_authorization_codes") as batch_op:
        batch_op.alter_column(
            "user_id", existing_type=sa.String(length=255), type_=sa.String(length=36)
        )
    with op.batch_alter_table("knowledge_suggestions") as batch_op:
        batch_op.alter_column(
            "extracted_by",
            existing_type=sa.String(length=64),
            type_=sa.String(length=36),
        )
        batch_op.alter_column(
            "reviewed_by",
            existing_type=sa.String(length=64),
            type_=sa.String(length=36),
            existing_nullable=True,
        )
        batch_op.alter_column(
            "pii_remediated_by",
            existing_type=sa.String(length=64),
            type_=sa.String(length=36),
            existing_nullable=True,
        )
    with op.batch_alter_table("knowledge_items") as batch_op:
        batch_op.alter_column(
            "verified_by",
            existing_type=sa.String(length=64),
            type_=sa.String(length=36),
            existing_nullable=True,
        )

    # ------------------------------------------------------------------
    # 5. evidence.form: Tier 1 cross-dialect CHECK constraint.
    #    PG already has the equivalent CHECK from Phase 7 (with the same
    #    value list); this branch only adds it on SQLite.
    # ------------------------------------------------------------------
    if dialect != "postgresql":
        with op.batch_alter_table("evidence") as batch_op:
            batch_op.create_check_constraint(
                "evidence_form_enum_check",
                "form IN ('text', 'image', 'metric', 'structured')",
            )


def downgrade() -> None:
    """Reverse Phase 9 audit fixes."""
    bind = op.get_bind()
    dialect = bind.dialect.name

    # 5. evidence.form CHECK
    if dialect != "postgresql":
        with op.batch_alter_table("evidence") as batch_op:
            batch_op.drop_constraint("evidence_form_enum_check", type_="check")

    # 4. Width normalization back
    with op.batch_alter_table("knowledge_items") as batch_op:
        batch_op.alter_column(
            "verified_by",
            existing_type=sa.String(length=36),
            type_=sa.String(length=64),
            existing_nullable=True,
        )
    with op.batch_alter_table("knowledge_suggestions") as batch_op:
        batch_op.alter_column(
            "pii_remediated_by",
            existing_type=sa.String(length=36),
            type_=sa.String(length=64),
            existing_nullable=True,
        )
        batch_op.alter_column(
            "reviewed_by",
            existing_type=sa.String(length=36),
            type_=sa.String(length=64),
            existing_nullable=True,
        )
        batch_op.alter_column(
            "extracted_by",
            existing_type=sa.String(length=36),
            type_=sa.String(length=64),
        )
    with op.batch_alter_table("oauth_authorization_codes") as batch_op:
        batch_op.alter_column(
            "user_id", existing_type=sa.String(length=36), type_=sa.String(length=255)
        )

    # 3. conversion_jobs
    with op.batch_alter_table("conversion_jobs") as batch_op:
        batch_op.drop_constraint("fk_conversion_jobs_team_id", type_="foreignkey")
        batch_op.drop_constraint(
            "fk_conversion_jobs_organization_id", type_="foreignkey"
        )
        batch_op.alter_column(
            "organization_id",
            existing_type=sa.String(length=36),
            nullable=True,
            server_default=None,
        )

    # 2. Other tenanted tables
    for table in TENANTED_TABLES_FK_ONLY:
        with op.batch_alter_table(table) as batch_op:
            batch_op.drop_constraint(f"fk_{table}_organization_id", type_="foreignkey")

    # 1. cases
    with op.batch_alter_table("cases") as batch_op:
        batch_op.drop_constraint("fk_cases_team_id", type_="foreignkey")
        batch_op.drop_constraint("fk_cases_organization_id", type_="foreignkey")
        batch_op.alter_column(
            "organization_id",
            existing_type=sa.String(length=36),
            nullable=True,
            server_default=None,
        )
