"""009_drop_case_checkpoints

Retires case checkpoints: ``case_checkpoints`` is dropped (#1882, owner ruling
2026-10-08).

The table held a full snapshot of a case taken before each state transition. No
production code ever wrote one (the composition root never constructed the
checkpoint service), and nothing ever read the table: no route, job, CLI or
restore path selected from it. The facts a snapshot would have preserved are
kept elsewhere, as data that is read: ``case_actions`` (every state transition,
who and when), ``statement_history`` (every problem-statement revision) and
``turn_history`` (every turn). And since #1882 a turn commits once, so the state
before a transition is simply the committed row the transition's turn started
from.

Dialects
--------

``DROP TABLE`` on both. Nothing references ``case_checkpoints`` by foreign key
(it only references ``cases``, ``enterprises`` and ``organizations``), so the
drop runs no ON DELETE action on SQLite whatever ``PRAGMA foreign_keys`` says,
and it needs no guard. On PostgreSQL the table's RLS policy
(``case_checkpoints_tenant_isolation``) and its indexes go with it; no other
revision enrols the table anywhere.

``downgrade()``
---------------

Recreates the table exactly as the baseline (``001_enterprise_baseline``) left
it at revision ``558d7f3cfed1``: columns, CHECK, foreign keys, primary key,
the five indexes, and on PostgreSQL row-level security with the plain tenant
policy. It comes back EMPTY: the snapshots this revision dropped are not
recoverable, which is the ruling (nothing read them).

Revision ID: 1e713f2d0e74
Revises: 558d7f3cfed1
Create Date: 2026-10-08 12:00:00
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "1e713f2d0e74"  # pragma: allowlist secret
down_revision: Union[str, Sequence[str], None] = (
    "558d7f3cfed1"  # pragma: allowlist secret
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "case_checkpoints"
#: The baseline's tenant policy expression, verbatim.
_ENTERPRISE_MATCHES_SESSION = (
    "enterprise_id = current_setting('app.current_enterprise_id', true)"
)


def upgrade() -> None:
    """Drop the table; its indexes and (on PostgreSQL) its policy go with it."""
    op.drop_table(TABLE)


def downgrade() -> None:
    """Recreate the table, empty, exactly as the baseline defined it."""
    op.create_table(
        TABLE,
        sa.Column("checkpoint_id", sa.String(length=36), nullable=False),
        sa.Column("enterprise_id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=True),
        sa.Column("case_id", sa.String(length=36), nullable=False),
        sa.Column("turn_number", sa.Integer(), nullable=False),
        sa.Column(
            "case_snapshot",
            sa.JSON().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column("snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("trigger", sa.String(length=50), nullable=False),
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
        sa.CheckConstraint(
            "LENGTH(TRIM(snapshot_hash)) > 0", name="case_checkpoints_hash_not_empty"
        ),
        sa.ForeignKeyConstraint(["case_id"], ["cases.case_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["enterprise_id"], ["enterprises.enterprise_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.organization_id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("checkpoint_id"),
    )
    op.create_index(op.f("ix_case_checkpoints_case_id"), TABLE, ["case_id"])
    op.create_index("ix_case_checkpoints_case_turn", TABLE, ["case_id", "turn_number"])
    op.create_index(op.f("ix_case_checkpoints_created_at"), TABLE, ["created_at"])
    op.create_index(op.f("ix_case_checkpoints_enterprise_id"), TABLE, ["enterprise_id"])
    op.create_index(
        op.f("ix_case_checkpoints_organization_id"), TABLE, ["organization_id"]
    )

    # PostgreSQL only: SQLite (standalone) is single-tenant and has no RLS.
    if op.get_context().dialect.name == "postgresql":
        op.execute(f'ALTER TABLE "{TABLE}" ENABLE ROW LEVEL SECURITY')
        op.execute(
            f'CREATE POLICY "{TABLE}_tenant_isolation" ON "{TABLE}" '
            f"USING ({_ENTERPRISE_MATCHES_SESSION})"
        )
