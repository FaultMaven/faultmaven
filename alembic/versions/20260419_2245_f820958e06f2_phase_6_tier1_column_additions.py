"""phase_6_tier1_column_additions

Storage redesign 2026-04 — Phase 6: Tier 1 column additions.

Per deployment-schema-strategy.md §7.1 (evidence target shape) and §12
decision #19 (locked Tier 1 column additions list). This is the most
additive phase — new columns + supporting indexes, no deletions.

Stale design columns explicitly EXCLUDED per §12 decision #18:
``cases.description`` is already on the table baseline (originally added
elsewhere); ``hypotheses.test_plan/test_results/priority`` and
``solutions.impact_scope/verification_plan`` are intentionally not added.

Tier 2 PG-only enhancements (CHECK constraints on
``evidence.reliability_score``, ``TEXT[]``+GIN on ``evidence.tags``,
expression / partial indexes) are deferred to Phase 7. Indexes added
here are plain B-tree, dialect-agnostic.

Columns added (table.column → type / nullability):

* cases:
    - closure_reason         VARCHAR(100) NULL
    - last_activity_at       TIMESTAMPTZ  NULL  (indexed)
    - resolved_at            TIMESTAMPTZ  NULL
    - closed_at              TIMESTAMPTZ  NULL  (indexed)
* solutions:
    - hypothesis_id          VARCHAR(36)  NULL  FK → hypotheses(hypothesis_id)
                                                ON DELETE SET NULL  (indexed)
* reports:
    - generated_by           VARCHAR(36)  NULL
* evidence:
    - form                   VARCHAR(20)  NOT NULL DEFAULT 'text'
    - is_primary             BOOLEAN      NOT NULL DEFAULT FALSE
    - content_type           VARCHAR(100) NULL
    - reliability_score      REAL         NULL
    - tags                   TEXT         NULL

Composite index on evidence(case_id, is_primary) added for
``list_evidence_tool``-style "primary evidence per case" lookups.

Revision ID: f820958e06f2
Revises: 7372cc0135c3
Create Date: 2026-04-19 22:45:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "f820958e06f2"
down_revision: Union[str, Sequence[str], None] = "7372cc0135c3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add Tier 1 columns to cases / solutions / reports / evidence."""

    # ------------------------------------------------------------------
    # 1. cases — terminal-state metadata + activity timestamp
    # ------------------------------------------------------------------
    with op.batch_alter_table("cases") as batch_op:
        batch_op.add_column(sa.Column("closure_reason", sa.String(length=100), nullable=True))
        batch_op.add_column(
            sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True))

    op.create_index("ix_cases_last_activity_at", "cases", ["last_activity_at"])
    op.create_index("ix_cases_closed_at", "cases", ["closed_at"])

    # ------------------------------------------------------------------
    # 2. solutions — link to hypothesis (nullable FK)
    # ------------------------------------------------------------------
    # batch_alter_table is used so SQLite can rebuild the table to attach
    # the FK; PostgreSQL applies the ADD COLUMN + ADD CONSTRAINT in place.
    with op.batch_alter_table("solutions") as batch_op:
        batch_op.add_column(sa.Column("hypothesis_id", sa.String(length=36), nullable=True))
        batch_op.create_foreign_key(
            "fk_solutions_hypothesis_id",
            "hypotheses",
            ["hypothesis_id"],
            ["hypothesis_id"],
            ondelete="SET NULL",
        )

    op.create_index("ix_solutions_hypothesis_id", "solutions", ["hypothesis_id"])

    # ------------------------------------------------------------------
    # 3. reports — generator identity
    # ------------------------------------------------------------------
    with op.batch_alter_table("reports") as batch_op:
        batch_op.add_column(sa.Column("generated_by", sa.String(length=36), nullable=True))

    # ------------------------------------------------------------------
    # 4. evidence — form/is_primary/content_type/reliability_score/tags
    # ------------------------------------------------------------------
    with op.batch_alter_table("evidence") as batch_op:
        batch_op.add_column(
            sa.Column(
                "form",
                sa.String(length=20),
                nullable=False,
                server_default="text",
            )
        )
        batch_op.add_column(
            sa.Column(
                "is_primary",
                sa.Boolean(),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.add_column(sa.Column("content_type", sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column("reliability_score", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("tags", sa.Text(), nullable=True))

    op.create_index(
        "ix_evidence_case_is_primary", "evidence", ["case_id", "is_primary"]
    )


def downgrade() -> None:
    """Reverse Phase 6 column additions in inverse order."""

    # 4. evidence
    op.drop_index("ix_evidence_case_is_primary", table_name="evidence")
    with op.batch_alter_table("evidence") as batch_op:
        batch_op.drop_column("tags")
        batch_op.drop_column("reliability_score")
        batch_op.drop_column("content_type")
        batch_op.drop_column("is_primary")
        batch_op.drop_column("form")

    # 3. reports
    with op.batch_alter_table("reports") as batch_op:
        batch_op.drop_column("generated_by")

    # 2. solutions
    op.drop_index("ix_solutions_hypothesis_id", table_name="solutions")
    with op.batch_alter_table("solutions") as batch_op:
        batch_op.drop_constraint("fk_solutions_hypothesis_id", type_="foreignkey")
        batch_op.drop_column("hypothesis_id")

    # 1. cases
    op.drop_index("ix_cases_closed_at", table_name="cases")
    op.drop_index("ix_cases_last_activity_at", table_name="cases")
    with op.batch_alter_table("cases") as batch_op:
        batch_op.drop_column("closed_at")
        batch_op.drop_column("resolved_at")
        batch_op.drop_column("last_activity_at")
        batch_op.drop_column("closure_reason")
