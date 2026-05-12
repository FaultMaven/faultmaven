"""users: add dev_roles column for local-mode role persistence

In local (dev-login) mode the user store is backed by SQLite. The existing
`users` table has no roles column — roles are normalised into RBAC tables
(`roles`, `role_permissions`, `organization_members`). Those tables are not
populated in local mode, so any role assignment done via the in-memory
DevUserStore (FakeRedis path) is silently lost when the DatabaseUserStore
path is used instead.

Adding `dev_roles` as a TEXT column (JSON array) gives the DatabaseUserStore
a simple, explicit place to persist roles for local mode. Cloud mode continues
to derive roles from the RBAC tables; the column is ignored there.

Revision ID: a1b2c3d4e5f6
Revises: 9c8b7a6d5e4f
Create Date: 2026-05-12 20:15:00
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "9c8b7a6d5e4f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("dev_roles", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "dev_roles")
