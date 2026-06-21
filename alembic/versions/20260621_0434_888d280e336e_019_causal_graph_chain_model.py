"""019 causal graph chain model

Adds the causal-graph substrate for the Two-Dimensional Hypothesis Methodology
— a hypothesis is a root->D chain over a per-case causal DAG:

- causal_nodes         : problem / intermediate / root nodes (per-node empirical state)
- causal_edges         : directed cause->effect, with and_group for AND-sets (M7)
- causal_node_evidence : node <-> evidence junction with stance
- hypotheses.root_node_id, hypotheses.path : chain header over the graph
- solutions.node_id, solutions.quadrant    : intervention linkage (§7.4)

Additive only (clean build, no data): new tables + nullable columns. SQLite +
PostgreSQL. See docs/architecture/investigation-engine/
two-dimensional-hypothesis-methodology.md §9.1.

Revision ID: 888d280e336e
Revises: c1f3a5b7d9e2
Create Date: 2026-06-21 04:34:06.118000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "888d280e336e"
down_revision: Union[str, Sequence[str], None] = "c1f3a5b7d9e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _json():
    """Match the ORM JsonBlob: Text on SQLite, JSONB on PostgreSQL."""
    return sa.Text().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    """Upgrade schema."""
    # ---- causal_nodes ----------------------------------------------------
    op.create_table(
        "causal_nodes",
        sa.Column("node_id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("case_id", sa.String(length=36), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("node_type", sa.String(length=20), nullable=False),
        sa.Column(
            "node_state",
            sa.String(length=20),
            server_default="candidate",
            nullable=False,
        ),
        sa.Column(
            "validation_method",
            sa.String(length=20),
            server_default="none",
            nullable=False,
        ),
        sa.Column(
            "belief",
            sa.Numeric(precision=3, scale=2),
            server_default="0.5",
            nullable=True,
        ),
        sa.Column(
            "signature_consistent", sa.Boolean(), server_default="1", nullable=False
        ),
        sa.Column("actionable", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("category", sa.String(length=50), nullable=True),
        sa.Column("state_epoch", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "generated_at_turn", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column(
            "last_updated_turn", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column(
            "last_progress_at_turn", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column(
            "iterations_without_progress",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("refutation_reason", sa.String(length=200), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("metadata", _json(), server_default="{}", nullable=False),
        sa.Column(
            "proposed_at",
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
        sa.CheckConstraint(
            "LENGTH(TRIM(statement)) > 0", name="causal_nodes_statement_not_empty"
        ),
        sa.CheckConstraint(
            "node_type IN ('problem', 'intermediate', 'root')",
            name="causal_nodes_node_type_check",
        ),
        sa.CheckConstraint(
            "node_state IN ('candidate', 'validated', 'refuted', 'inconclusive')",
            name="causal_nodes_node_state_check",
        ),
        sa.CheckConstraint(
            "validation_method IN ('none', 'empirical', 'deductive')",
            name="causal_nodes_validation_method_check",
        ),
        sa.CheckConstraint(
            "belief IS NULL OR (belief >= 0 AND belief <= 1)",
            name="causal_nodes_belief_range",
        ),
        sa.ForeignKeyConstraint(["case_id"], ["cases.case_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.organization_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("node_id"),
    )
    op.create_index(
        "ix_causal_nodes_organization_id", "causal_nodes", ["organization_id"]
    )
    op.create_index("ix_causal_nodes_case_id", "causal_nodes", ["case_id"])
    op.create_index("ix_causal_nodes_node_type", "causal_nodes", ["node_type"])
    op.create_index("ix_causal_nodes_node_state", "causal_nodes", ["node_state"])
    op.create_index("ix_causal_nodes_category", "causal_nodes", ["category"])

    # ---- causal_edges ----------------------------------------------------
    op.create_table(
        "causal_edges",
        sa.Column("edge_id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("case_id", sa.String(length=36), nullable=False),
        sa.Column("cause_node_id", sa.String(length=36), nullable=False),
        sa.Column("effect_node_id", sa.String(length=36), nullable=False),
        sa.Column("and_group", sa.String(length=64), nullable=True),
        sa.Column("reasoning", sa.Text(), nullable=True),
        sa.Column("created_at_turn", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "cause_node_id <> effect_node_id", name="causal_edges_no_self_loop"
        ),
        sa.ForeignKeyConstraint(["case_id"], ["cases.case_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["cause_node_id"], ["causal_nodes.node_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["effect_node_id"], ["causal_nodes.node_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.organization_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("edge_id"),
    )
    op.create_index(
        "ix_causal_edges_organization_id", "causal_edges", ["organization_id"]
    )
    op.create_index("ix_causal_edges_case_id", "causal_edges", ["case_id"])
    op.create_index("ix_causal_edges_cause_node_id", "causal_edges", ["cause_node_id"])
    op.create_index(
        "ix_causal_edges_effect_node_id", "causal_edges", ["effect_node_id"]
    )

    # ---- causal_node_evidence (junction) ---------------------------------
    op.create_table(
        "causal_node_evidence",
        sa.Column("node_id", sa.String(length=36), nullable=False),
        sa.Column("evidence_id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("stance", sa.String(length=20), nullable=False),
        sa.Column("stance_confidence", sa.Numeric(precision=3, scale=2), nullable=True),
        sa.Column("reasoning", sa.Text(), nullable=True),
        sa.Column("linked_at_turn", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "stance IN ('supports', 'refutes', 'neutral')",
            name="causal_node_evidence_stance_check",
        ),
        sa.CheckConstraint(
            "stance_confidence IS NULL OR "
            "(stance_confidence >= 0 AND stance_confidence <= 1)",
            name="causal_node_evidence_confidence_range",
        ),
        sa.ForeignKeyConstraint(
            ["evidence_id"], ["evidence.evidence_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["node_id"], ["causal_nodes.node_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.organization_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("node_id", "evidence_id"),
    )
    op.create_index(
        "ix_causal_node_evidence_organization_id",
        "causal_node_evidence",
        ["organization_id"],
    )
    op.create_index(
        "ix_causal_node_evidence_evidence", "causal_node_evidence", ["evidence_id"]
    )

    # ---- chain-header columns on existing tables (batch for SQLite) ------
    # SQLite cannot ALTER-ADD a FK or CHECK; batch mode rebuilds the table.
    # Cross-table FKs go in a separate batch (009 precedent) so PG links
    # directly without a needless rebuild.
    with op.batch_alter_table("hypotheses") as batch:
        batch.add_column(sa.Column("root_node_id", sa.String(length=36), nullable=True))
        batch.add_column(
            sa.Column("path", _json(), server_default="[]", nullable=False)
        )
    op.create_index("ix_hypotheses_root_node_id", "hypotheses", ["root_node_id"])
    with op.batch_alter_table("hypotheses") as batch:
        batch.create_foreign_key(
            "fk_hypotheses_root_node_id",
            "causal_nodes",
            ["root_node_id"],
            ["node_id"],
            ondelete="SET NULL",
        )

    with op.batch_alter_table("solutions") as batch:
        batch.add_column(sa.Column("node_id", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("quadrant", sa.String(length=20), nullable=True))
        batch.create_check_constraint(
            "solutions_quadrant_check",
            "quadrant IS NULL OR quadrant IN "
            "('remediation', 'defensive_fix', 'mitigation', 'loop_break')",
        )
    op.create_index("ix_solutions_node_id", "solutions", ["node_id"])
    with op.batch_alter_table("solutions") as batch:
        batch.create_foreign_key(
            "fk_solutions_node_id",
            "causal_nodes",
            ["node_id"],
            ["node_id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("solutions") as batch:
        batch.drop_constraint("fk_solutions_node_id", type_="foreignkey")
    op.drop_index("ix_solutions_node_id", table_name="solutions")
    with op.batch_alter_table("solutions") as batch:
        batch.drop_constraint("solutions_quadrant_check", type_="check")
        batch.drop_column("quadrant")
        batch.drop_column("node_id")

    with op.batch_alter_table("hypotheses") as batch:
        batch.drop_constraint("fk_hypotheses_root_node_id", type_="foreignkey")
    op.drop_index("ix_hypotheses_root_node_id", table_name="hypotheses")
    with op.batch_alter_table("hypotheses") as batch:
        batch.drop_column("path")
        batch.drop_column("root_node_id")

    # New tables: DROP TABLE removes their indexes with them.
    op.drop_table("causal_node_evidence")
    op.drop_table("causal_edges")
    op.drop_table("causal_nodes")
