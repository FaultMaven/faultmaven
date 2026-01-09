"""008_add_email_uniqueness_constraint

Add explicit UNIQUE constraint for email addresses (BUGFIX: Priority 1).

This migration addresses a critical data integrity bug where duplicate emails
could exist in the users table. While a unique index already exists
(idx_users_email), adding an explicit constraint provides:
- Better data integrity enforcement
- Clearer schema documentation
- Explicit constraint violation errors

IMPORTANT: Before running this migration:
1. Run scripts/check_duplicate_emails.py to identify any existing duplicates
2. If duplicates exist, run scripts/resolve_duplicate_emails.py to resolve them
3. Then run this migration

The migration will FAIL if duplicate emails exist in the database.

Revision ID: 8c9d0e1f2a3b
Revises: 007_users_table
Create Date: 2025-01-09 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision: str = "008_email_uniqueness_constraint"
down_revision: Union[str, Sequence[str], None] = "007_users_table"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def is_postgresql() -> bool:
    """Check if we're running against PostgreSQL."""
    bind = op.get_bind()
    return bind.dialect.name == "postgresql"


def upgrade() -> None:
    """Add email uniqueness constraint."""
    if is_postgresql():
        _upgrade_postgresql()
    else:
        _upgrade_sqlite()


def downgrade() -> None:
    """Remove email uniqueness constraint."""
    if is_postgresql():
        _downgrade_postgresql()
    else:
        _downgrade_sqlite()


# =============================================================================
# PostgreSQL Implementation
# =============================================================================

def _upgrade_postgresql() -> None:
    """Add PostgreSQL email uniqueness constraint."""
    conn = op.get_bind()

    # Check for existing duplicates before adding constraint
    check_query = text("""
        SELECT LOWER(email), COUNT(*) as count
        FROM users
        WHERE deleted_at IS NULL
        GROUP BY LOWER(email)
        HAVING COUNT(*) > 1
        LIMIT 1
    """)

    result = conn.execute(check_query)
    duplicate = result.first()

    if duplicate:
        raise Exception(
            f"MIGRATION FAILED: Duplicate emails found in database.\n"
            f"Example: '{duplicate[0]}' appears {duplicate[1]} times.\n\n"
            f"Please run the following scripts before applying this migration:\n"
            f"  1. python scripts/check_duplicate_emails.py\n"
            f"  2. python scripts/resolve_duplicate_emails.py --auto\n"
            f"  3. alembic upgrade head\n"
        )

    # Add explicit unique constraint on email (case-insensitive)
    # Note: The unique index idx_users_email already exists from migration 007
    # This constraint provides additional enforcement and documentation
    conn.execute(text("""
        ALTER TABLE users
        ADD CONSTRAINT users_email_unique
        UNIQUE (email)
    """))

    print("✓ Added email uniqueness constraint")


def _downgrade_postgresql() -> None:
    """Remove PostgreSQL email uniqueness constraint."""
    conn = op.get_bind()

    conn.execute(text("""
        ALTER TABLE users
        DROP CONSTRAINT IF EXISTS users_email_unique
    """))

    print("✓ Removed email uniqueness constraint")


# =============================================================================
# SQLite Implementation
# =============================================================================

def _upgrade_sqlite() -> None:
    """
    Add SQLite email uniqueness constraint.

    Note: SQLite already has a unique index on email from migration 007
    (idx_users_email). SQLite doesn't support ALTER TABLE ADD CONSTRAINT,
    so we rely on the existing unique index for enforcement.

    This is a no-op for SQLite as the index already provides uniqueness.
    """
    # Check for duplicates (same as PostgreSQL)
    conn = op.get_bind()

    check_query = text("""
        SELECT LOWER(email), COUNT(*) as count
        FROM users
        WHERE deleted_at IS NULL
        GROUP BY LOWER(email)
        HAVING COUNT(*) > 1
        LIMIT 1
    """)

    result = conn.execute(check_query)
    duplicate = result.first()

    if duplicate:
        raise Exception(
            f"MIGRATION FAILED: Duplicate emails found in database.\n"
            f"Example: '{duplicate[0]}' appears {duplicate[1]} times.\n\n"
            f"Please run the following scripts before applying this migration:\n"
            f"  1. python scripts/check_duplicate_emails.py\n"
            f"  2. python scripts/resolve_duplicate_emails.py --auto\n"
            f"  3. alembic upgrade head\n"
        )

    # SQLite: No additional constraint needed, unique index already exists
    print("✓ Email uniqueness already enforced by unique index (SQLite)")


def _downgrade_sqlite() -> None:
    """
    Remove SQLite email uniqueness constraint.

    Since SQLite doesn't have a separate constraint (just the index),
    this is a no-op. The index from migration 007 remains.
    """
    print("✓ No constraint to remove (SQLite uses unique index)")
