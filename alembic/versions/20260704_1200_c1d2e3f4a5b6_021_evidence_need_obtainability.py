"""021_evidence_need_obtainability

Adds ``obtainability`` to the ``evidence_needs`` table so the model-declared
judgment — *is the discriminating data obtainable at all?* — survives across
turns:

- ``unknown``      — no declaration (default; fail-safe, keeps the case engaging)
- ``obtainable``   — the data can be gathered
- ``unobtainable`` — the data cannot be gathered (a data wall)

Without this column the in-memory ``EvidenceNeed.obtainability`` (model-set via
``evidence_need_updates``) was silently dropped on save and reloaded as
``unknown``, so the declared-data-wall arm of the verification-status handoff
could only ever fire within a single turn. Persisting it is what activates that
sound arm turn-to-turn (insufficient-evidence-handling.md §5.3 / §5.4).

Revision ID: c1d2e3f4a5b6
Revises: b2d4f6a8c0e1
Create Date: 2026-07-04 12:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "c1d2e3f4a5b6"
down_revision: Union[str, Sequence[str], None] = "b2d4f6a8c0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the NOT NULL ``obtainability`` column (server-default 'unknown', so
    existing rows backfill safely) + a value CHECK (batch mode so it applies on
    SQLite, which cannot ALTER a CHECK constraint in place)."""
    with op.batch_alter_table("evidence_needs") as batch:
        batch.add_column(
            sa.Column(
                "obtainability",
                sa.String(length=20),
                nullable=False,
                server_default="unknown",
            )
        )
        batch.create_check_constraint(
            "evidence_needs_obtainability_check",
            "obtainability IN ('unknown', 'obtainable', 'unobtainable')",
        )


def downgrade() -> None:
    with op.batch_alter_table("evidence_needs") as batch:
        batch.drop_constraint("evidence_needs_obtainability_check", type_="check")
        batch.drop_column("obtainability")
