"""032_user_audit_log_success_session

Add ``success`` and ``session_id`` to ``user_audit_log`` (ADR-015 PR 7).

The ``UserAuditLog`` domain model and ``IAuditRepository.log_event`` have
carried ``success`` and ``session_id`` since their introduction, but the table
never had the columns — harmless while the interface had no implementation.
PR 7 adds the first real writer (SSO just-in-time provisioning audit entries),
so the table gains the columns rather than silently dropping the fields.

No data migration: the table has never had a writer, so it is empty by
construction. ``success`` is NOT NULL DEFAULT TRUE (an audit row records an
event that happened; failure entries must say so explicitly); ``session_id``
is nullable (a JIT-provisioning entry predates any session).

Revision ID: b4c5d6e7f8a9
Revises: a3b4c5d6e7f8
Create Date: 2026-07-21 16:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b4c5d6e7f8a9"
down_revision: Union[str, Sequence[str], None] = "a3b4c5d6e7f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the success flag and session linkage the domain model already has."""
    op.add_column(
        "user_audit_log",
        sa.Column(
            "success",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.add_column(
        "user_audit_log",
        sa.Column("session_id", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    """Drop the audit success/session columns."""
    op.drop_column("user_audit_log", "session_id")
    op.drop_column("user_audit_log", "success")
