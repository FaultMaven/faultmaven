"""007_drop_users_password_or_sso_check

Drops the ``users_password_or_sso`` CHECK constraint:

    (hashed_password IS NOT NULL) OR (sso_provider IS NOT NULL)

The constraint was too strict — it blocked the intentional dev-login
flow (local mode) where users are created with both fields NULL and
authenticated by username only. Credential-required-for-login is now
enforced at the auth-endpoint layer via ``AUTH_MODE``, not at the
``users`` table.

Revision ID: 05b6eaf5baad
Revises: be112b702fd4
Create Date: 2026-05-08 07:37:00.000000
"""

from typing import Sequence, Union

from alembic import op

revision: str = "05b6eaf5baad"
down_revision: Union[str, Sequence[str], None] = "be112b702fd4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Drop the users_password_or_sso CHECK.

    batch_alter_table is required for SQLite (no native ALTER for CHECK
    drop); on PostgreSQL it's a passthrough.
    """
    with op.batch_alter_table("users") as batch:
        batch.drop_constraint("users_password_or_sso", type_="check")


def downgrade() -> None:
    """Restore the (hashed_password IS NOT NULL) OR (sso_provider IS NOT NULL) CHECK.

    Note: any rows created in the meantime that violate this rule (i.e.
    dev-login users with both fields NULL) will block the downgrade.
    """
    with op.batch_alter_table("users") as batch:
        batch.create_check_constraint(
            "users_password_or_sso",
            "(hashed_password IS NOT NULL) OR (sso_provider IS NOT NULL)",
        )
