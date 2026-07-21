"""031_drop_oauth_revoked_tokens

Drop the ``oauth_revoked_tokens`` table (#767).

The table backed ``PostgresTokenRevocationStore``, which was never wired into
the DI container — every deployment has always used the Redis-backed store, so
no code path has ever written to (or read from) this table. #767 consolidates
token revocation onto ONE store (Redis, single key prefix) written by every
revoke path and read by the request-path check; a second, unreachable
persistence backend is exactly the kind of fragmentation that caused the bug,
so the dead table goes with the dead store class.

No data migration: the table is empty by construction (no writer ever existed).

Revision ID: a3b4c5d6e7f8
Revises: f2a3b4c5d6e7
Create Date: 2026-07-21 14:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a3b4c5d6e7f8"
down_revision: Union[str, Sequence[str], None] = "f2a3b4c5d6e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Drop the never-written oauth_revoked_tokens table."""
    op.drop_index("idx_revoked_tokens_expires_at", table_name="oauth_revoked_tokens")
    op.drop_table("oauth_revoked_tokens")


def downgrade() -> None:
    """Recreate oauth_revoked_tokens as the baseline (001) defined it."""
    op.create_table(
        "oauth_revoked_tokens",
        sa.Column("jti", sa.String(length=64), nullable=False),
        sa.Column(
            "revoked_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("jti"),
    )
    op.create_index(
        "idx_revoked_tokens_expires_at",
        "oauth_revoked_tokens",
        ["expires_at"],
        unique=False,
    )
