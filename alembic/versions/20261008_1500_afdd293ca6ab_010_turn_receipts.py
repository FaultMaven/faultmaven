"""010_turn_receipts

The turn receipt (#1888): ``turn_receipts``, one row per committed keyed turn.

A turn submitted with an ``Idempotency-Key`` writes its receipt in the turn's
one transaction (``ICaseRepository.save(case, reports=..., receipt=...)``): the
request's identity (author, key, a sha256 fingerprint of the turn's inputs),
the turn number it committed at, and the ``TurnResponse`` the client was sent.
A retry with the same key is answered from it instead of running the turn
again.

Columns and key
---------------
The primary key is ``(enterprise_id, case_id, author_id, idempotency_key)``,
the enterprise first because RLS scopes the table on it: a key that omits it
can resolve to a row the session cannot see (the ``turn_usage`` lesson).
``idempotency_key`` is ``VARCHAR(255)``, the key grammar's upper bound;
``author_id`` and the two id columns are ``VARCHAR(36)``, the width of the ids
they hold. ``author_id`` has no foreign key, as ``case_messages.author_id`` has
none: the receipt goes with its case, not with the account.

``response`` is ``json``, not ``jsonb``, on PostgreSQL: ``jsonb`` reorders
object keys, and a replay must be the bytes the client was sent. ``TEXT`` on
SQLite, as SQLAlchemy's ``JSON`` renders there.

Lifecycle
---------
``ON DELETE CASCADE`` from ``cases`` and from ``enterprises``; no retention
job. ``ix_turn_receipts_case`` serves the case cascade (the primary key leads
with the enterprise).

Dialects
--------
PostgreSQL: row-level security with the plain tenant policy every
tenant-scoped table carries (no ``FOR`` clause, so ``USING`` is also the
``WITH CHECK`` and an INSERT under another enterprise is refused). SQLite
(standalone) gets the table, the CHECK and the index, and no RLS. No GRANT: the
app role's ``ALTER DEFAULT PRIVILEGES`` covers tables a later migration
creates.

``downgrade()`` drops the table; its policy and index go with it.

Revision ID: afdd293ca6ab
Revises: 1e713f2d0e74
Create Date: 2026-10-08 15:00:00
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "afdd293ca6ab"  # pragma: allowlist secret
down_revision: Union[str, Sequence[str], None] = (
    "1e713f2d0e74"  # pragma: allowlist secret
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "turn_receipts"

#: Frozen copy of the baseline's policy predicate. Migrations are history; they
#: state the text they were written against rather than importing it.
_ENTERPRISE_MATCHES_SESSION = (
    "enterprise_id = current_setting('app.current_enterprise_id', true)"
)


def upgrade() -> None:
    """Create ``turn_receipts`` and, on PostgreSQL, enrol it in RLS."""
    op.create_table(
        TABLE,
        sa.Column("enterprise_id", sa.String(length=36), nullable=False),
        sa.Column("case_id", sa.String(length=36), nullable=False),
        sa.Column("author_id", sa.String(length=36), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("turn_number", sa.Integer(), nullable=False),
        sa.Column("response", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("turn_number >= 0", name="turn_receipts_turn_nonnegative"),
        sa.ForeignKeyConstraint(["case_id"], ["cases.case_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["enterprise_id"], ["enterprises.enterprise_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint(
            "enterprise_id", "case_id", "author_id", "idempotency_key"
        ),
    )
    op.create_index("ix_turn_receipts_case", TABLE, ["case_id"], unique=False)

    # PostgreSQL only: SQLite (standalone) is single-tenant and has no RLS.
    if op.get_context().dialect.name == "postgresql":
        op.execute(f'ALTER TABLE "{TABLE}" ENABLE ROW LEVEL SECURITY')
        op.execute(
            f'CREATE POLICY "{TABLE}_tenant_isolation" ON "{TABLE}" '
            f"USING ({_ENTERPRISE_MATCHES_SESSION})"
        )


def downgrade() -> None:
    """Drop ``turn_receipts``. Its policy and index are dropped with it."""
    op.drop_index("ix_turn_receipts_case", table_name=TABLE)
    op.drop_table(TABLE)
