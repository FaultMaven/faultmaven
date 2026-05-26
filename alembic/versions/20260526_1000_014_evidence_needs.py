"""014_evidence_needs

Phase 1 of the Evidence Needs feature: create the demand-side persistence
structure. Two new tables:

- ``evidence_needs``: pool of verification requirements identified during
  investigation. Created by the LLM at problem-statement confirmation
  (symptom needs) and at hypothesis creation (causal needs).
  ``motivating_hypothesis_ids`` is a JSON list — small, mutated as a
  unit, never queried by ID across cases (so a junction table would be
  overhead). See evidence-needs-design.md §8.7 for the storage rationale.

- ``evidence_need_fulfillment``: many-to-many junction between needs and
  the evidence rows that fulfill them. A need accumulates multiple
  fulfillments across DIAGNOSIS (presence evidence) and
  MITIGATION/TREATMENT (absence evidence); a single evidence row may
  fulfill multiple needs when cross-hypothesis sharing applies.

This migration is additive — no existing tables are touched, no data is
migrated. Phase 2+ of the feature will wire the LLM schema and engine
lifecycle into these tables.

Revision ID: e1d2c3b4a5f6
Revises: b2c3d4e5f607
Create Date: 2026-05-26 10:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "e1d2c3b4a5f6"
down_revision: Union[str, Sequence[str], None] = "b2c3d4e5f607"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # evidence_needs — demand-side pool on the case.
    # ------------------------------------------------------------------
    op.create_table(
        "evidence_needs",
        sa.Column("need_id", sa.String(length=36), primary_key=True),
        sa.Column(
            "organization_id",
            sa.String(length=36),
            sa.ForeignKey("organizations.organization_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "case_id",
            sa.String(length=36),
            sa.ForeignKey("cases.case_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("purpose", sa.String(length=30), nullable=False),
        sa.Column("request_text", sa.String(length=500), nullable=False),
        sa.Column("rationale", sa.String(length=500), nullable=False),
        sa.Column(
            "priority",
            sa.String(length=10),
            nullable=False,
            server_default="medium",
        ),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="pending",
        ),
        # motivating_hypothesis_ids: JSON list. PG promotes to JSONB via
        # the JsonBlob TypeDecorator, but Alembic ops here just see Text;
        # the dialect-specific behavior is handled by SQLAlchemy at
        # query/bind time, not at migration time.
        sa.Column(
            "motivating_hypothesis_ids",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
        sa.Column("superseded_reason", sa.String(length=500), nullable=True),
        sa.Column("created_at_turn", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "purpose IN ('symptom_verification', 'causal_verification')",
            name="evidence_needs_purpose_check",
        ),
        sa.CheckConstraint(
            "priority IN ('high', 'medium', 'low')",
            name="evidence_needs_priority_check",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'partially_met', 'fulfilled', 'superseded')",
            name="evidence_needs_status_check",
        ),
        # superseded_reason required iff status='superseded'.
        sa.CheckConstraint(
            "(status = 'superseded' AND superseded_reason IS NOT NULL)"
            " OR (status != 'superseded' AND superseded_reason IS NULL)",
            name="evidence_needs_superseded_reason_invariant",
        ),
        sa.CheckConstraint(
            "LENGTH(TRIM(request_text)) > 0",
            name="evidence_needs_request_text_not_empty",
        ),
        sa.CheckConstraint(
            "LENGTH(TRIM(rationale)) > 0",
            name="evidence_needs_rationale_not_empty",
        ),
        sa.CheckConstraint(
            "created_at_turn >= 0",
            name="evidence_needs_created_at_turn_nonnegative",
        ),
    )

    # Indexes (separate from create_table so they have explicit names
    # matching the ORM model's __table_args__).
    op.create_index(
        "ix_evidence_needs_organization_id",
        "evidence_needs",
        ["organization_id"],
    )
    op.create_index(
        "ix_evidence_needs_case_id",
        "evidence_needs",
        ["case_id"],
    )
    op.create_index(
        "ix_evidence_needs_status",
        "evidence_needs",
        ["status"],
    )
    op.create_index(
        "ix_evidence_needs_case_status",
        "evidence_needs",
        ["case_id", "status"],
    )
    op.create_index(
        "ix_evidence_needs_case_purpose",
        "evidence_needs",
        ["case_id", "purpose"],
    )

    # ------------------------------------------------------------------
    # evidence_need_fulfillment — many-to-many junction.
    # ------------------------------------------------------------------
    op.create_table(
        "evidence_need_fulfillment",
        sa.Column(
            "need_id",
            sa.String(length=36),
            sa.ForeignKey("evidence_needs.need_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "evidence_id",
            sa.String(length=36),
            sa.ForeignKey("evidence.evidence_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "organization_id",
            sa.String(length=36),
            sa.ForeignKey("organizations.organization_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("linked_at_turn", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "linked_at_turn >= 0",
            name="evidence_need_fulfillment_linked_at_turn_nonnegative",
        ),
    )
    op.create_index(
        "ix_evidence_need_fulfillment_organization_id",
        "evidence_need_fulfillment",
        ["organization_id"],
    )
    op.create_index(
        "ix_evidence_need_fulfillment_evidence",
        "evidence_need_fulfillment",
        ["evidence_id"],
    )


def downgrade() -> None:
    # Drop junction first (FKs to evidence_needs).
    op.drop_index(
        "ix_evidence_need_fulfillment_evidence",
        table_name="evidence_need_fulfillment",
    )
    op.drop_index(
        "ix_evidence_need_fulfillment_organization_id",
        table_name="evidence_need_fulfillment",
    )
    op.drop_table("evidence_need_fulfillment")

    op.drop_index("ix_evidence_needs_case_purpose", table_name="evidence_needs")
    op.drop_index("ix_evidence_needs_case_status", table_name="evidence_needs")
    op.drop_index("ix_evidence_needs_status", table_name="evidence_needs")
    op.drop_index("ix_evidence_needs_case_id", table_name="evidence_needs")
    op.drop_index("ix_evidence_needs_organization_id", table_name="evidence_needs")
    op.drop_table("evidence_needs")
