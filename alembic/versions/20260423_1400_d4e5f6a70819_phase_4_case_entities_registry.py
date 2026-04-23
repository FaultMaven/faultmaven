"""phase_4_case_entities_registry

Create ``case_entities`` — a cross-evidence index of entities (IPs,
hostnames, users, PIDs, ports, services, paths, devices, metric names)
surfaced during preprocessing. Built per Phase 4 of
``docs/working/WIP-data-processing-improvement-plan.md``.

Before this phase, only the logs_extractor emitted an entity profile,
and it lived embedded in the per-evidence structural_index string —
answering "where does IP 10.0.0.5 show up in this case?" required the
agent to LLM-scan every evidence summary. This registry makes that
question a single indexed lookup.

Schema:

    CREATE TABLE case_entities (
        case_id          VARCHAR(36)  NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
        entity_type      VARCHAR(20)  NOT NULL,
        entity_value     VARCHAR(255) NOT NULL,
        evidence_id      VARCHAR(36)  NOT NULL REFERENCES evidence(evidence_id) ON DELETE CASCADE,
        mention_count    INTEGER      NOT NULL DEFAULT 1,
        in_error_context BOOLEAN      NOT NULL DEFAULT FALSE,
        first_seen_ts    TIMESTAMPTZ  NULL,
        PRIMARY KEY (case_id, entity_type, entity_value, evidence_id)
    );
    CREATE INDEX idx_case_entities_lookup
        ON case_entities(case_id, entity_type, entity_value);
    CREATE INDEX idx_case_entities_by_evidence
        ON case_entities(evidence_id);

Notes:

- Composite primary key is idempotent — re-extracting an evidence upserts
  rows keyed by (case, type, value, evidence).
- ``first_seen_ts`` is nullable: populated from the evidence's
  ``coverage_start_ts`` when that column is non-NULL (Phase 3a), else
  NULL for timeless evidence (configs, source code, short pastes).
- Cascade delete on both FKs — case deletion or evidence deletion sweeps
  registry rows without a separate cleanup step.
- The lookup index supports the primary query "find evidence mentioning
  entity X in case C"; the by_evidence index supports the cleanup path
  when evidence is re-extracted or deleted.

Row growth is bounded by the preprocessor-side cap of 500 entities per
(evidence, entity_type). An evidence row that exceeds the cap for any
type emits an ``entities.overflow_types`` marker in
``evidence.metadata`` so the agent knows the registry is incomplete
for that (evidence, type) pair.

Revision ID: d4e5f6a70819
Revises: c3d4e5f6a708
Create Date: 2026-04-23 14:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4e5f6a70819"
down_revision: Union[str, Sequence[str], None] = "c3d4e5f6a708"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "case_entities",
        sa.Column(
            "case_id",
            sa.String(36),
            sa.ForeignKey("cases.case_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("entity_type", sa.String(20), nullable=False),
        sa.Column("entity_value", sa.String(255), nullable=False),
        sa.Column(
            "evidence_id",
            sa.String(36),
            sa.ForeignKey("evidence.evidence_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("mention_count", sa.Integer, nullable=False, server_default="1"),
        sa.Column(
            "in_error_context",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("first_seen_ts", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint(
            "case_id",
            "entity_type",
            "entity_value",
            "evidence_id",
            name="pk_case_entities",
        ),
    )
    # Primary query path: "find evidence mentioning entity X in case C".
    # Case-prefixed so the index narrows cheaply before the value match
    # runs. ``entity_type`` before ``entity_value`` because a
    # type-filter query ("all IPs in case C") is also a common access
    # pattern and benefits from the prefix ordering.
    op.create_index(
        "idx_case_entities_lookup",
        "case_entities",
        ["case_id", "entity_type", "entity_value"],
    )
    # Cleanup path: when an evidence is re-extracted (e.g. via Phase
    # 1.5 reclassification) or deleted, the associated registry rows
    # must be located by evidence_id. ON DELETE CASCADE handles
    # deletion automatically; re-extraction uses this index to delete
    # stale rows before inserting fresh ones.
    op.create_index(
        "idx_case_entities_by_evidence",
        "case_entities",
        ["evidence_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_case_entities_by_evidence", table_name="case_entities")
    op.drop_index("idx_case_entities_lookup", table_name="case_entities")
    op.drop_table("case_entities")
