"""017_rename_mitigation_to_stabilization

Completes the investigation-flow rename "mitigation" -> "stabilization" at the
persisted-data layer. The redesign renamed the concept (StabilizationRecord,
"Stabilizing" stage label) but its R2 as-built reconciliation deferred the
wire-level rename: the `EvidenceCategory` value, the LLM emission milestone
names, and the stage/action enums were kept as `mitigation_*`. That deferral is
now reversed in code; this migration brings the one durable relational column in
line.

`evidence.category` is a plain VARCHAR (not a native ENUM), so no type DDL is
needed — this is a pure forward value rename:
    'mitigation_evidence' -> 'stabilization_evidence'

Transient runtime state serialized into the `cases` JSON blobs
(`proposed_actions` / `action_attempts` with `action_type='mitigation'`) is
regenerated per turn and is not migrated. Pre-production, no data to preserve
(per feedback_no_backcompat_pre_data); case data may be wiped via
scripts/wipe_case_data.py for a fully clean baseline.

Revision ID: 1a2b3c4d5e6f
Revises: 0a1b2c3d4e5f
Create Date: 2026-06-07 09:00:00.000000
"""

from typing import Sequence, Union

from alembic import op

revision: str = "1a2b3c4d5e6f"
down_revision: Union[str, Sequence[str], None] = "0a1b2c3d4e5f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Rename the evidence.category value forward."""
    op.execute(
        "UPDATE evidence SET category = 'stabilization_evidence' "
        "WHERE category = 'mitigation_evidence'"
    )


def downgrade() -> None:
    """Reverse the value rename."""
    op.execute(
        "UPDATE evidence SET category = 'mitigation_evidence' "
        "WHERE category = 'stabilization_evidence'"
    )
