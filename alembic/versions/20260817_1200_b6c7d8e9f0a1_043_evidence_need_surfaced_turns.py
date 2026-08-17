"""043_evidence_need_surfaced_turns

Adds ``surfaced_turns`` to ``evidence_needs`` — the turns on which a need was
put to the user as an EVIDENCE suggestion (fm#1079).

The anti-nagging rule in ``_EVIDENCE_NEEDS_LIFECYCLE_BLOCK`` ("first mention:
full request; second: brief reminder; third+: stop surfacing") told the model to
count its own prior mentions "by scanning your prior turns in the conversation
history — no stored counter exists". That instruction is unsatisfiable past the
verbatim window: ``HISTORY_VERBATIM_TURNS`` is 3, and older turns collapse to
``_build_turn_summary``, which records milestones, artifact counts and 200 chars
of the reply — never what was asked for. Beyond three turns the information the
rule depends on is simply not in the prompt, so every turn reads as a first
mention and the ask repeats indefinitely.

This column is the stored counter that comment said did not exist. The engine
records a turn at the suggestion seam; the ``<evidence_needs>`` block renders
the count and the last-asked turn, so the model is told how often it has already
asked instead of being asked to reconstruct it.

Stored as a JSON list of turn numbers rather than an integer counter: the turn
list also answers "how long has this been outstanding", it is idempotent under
re-save (a set, not an increment), and it matches how
``motivating_hypothesis_ids`` already rides on this table.

Revision ID: b6c7d8e9f0a1
Revises: a5b6c7d8e9f0
Create Date: 2026-08-17 12:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy import Text
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b6c7d8e9f0a1"
down_revision: Union[str, Sequence[str], None] = "a5b6c7d8e9f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the NOT NULL ``surfaced_turns`` column with a ``[]`` server default,
    so existing rows backfill to "never surfaced" — the honest reading, since no
    ask history was recorded before this migration. Batch mode for SQLite.

    The type MUST carry the PostgreSQL JSONB variant, matching the ORM
    (``JsonBlob``) and ``motivating_hypothesis_ids`` on this same table. The
    repository writes this column through ``self._cast('surfaced_turns')``,
    which emits ``CAST(... AS JSONB)`` on PostgreSQL — a plain ``sa.Text()``
    column here would take that write on SQLite and reject it on every save in
    Cloud, the PostgreSQL-only dark path
    ``test_pg_evidence_need_obtainability.py`` exists to guard.
    """
    with op.batch_alter_table("evidence_needs") as batch:
        batch.add_column(
            sa.Column(
                "surfaced_turns",
                sa.Text().with_variant(
                    postgresql.JSONB(astext_type=Text()), "postgresql"
                ),
                nullable=False,
                server_default="[]",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("evidence_needs") as batch:
        batch.drop_column("surfaced_turns")
