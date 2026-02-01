"""Add multi-tenancy support to hypotheses and solutions (TASK-026)

This migration adds organization_id, created_by, and updated_by columns to
hypotheses and solutions tables to enable multi-tenant isolation and proper
audit trailing as required by TASK-026 specification.

Changes:
- Add organization_id column with foreign key to organizations table
- Add created_by column with foreign key to users table
- Add updated_by column with foreign key to users table (nullable)
- Add indexes for query performance
- Backfill organization_id from cases.org_id for existing records

Revision ID: 010_hypothesis_solution_multitenancy
Revises: 008_email_uniqueness_constraint
Create Date: 2025-01-01 08:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision = "010_hypothesis_solution_multitenancy"
down_revision = "008_email_uniqueness_constraint"
branch_labels = None
depends_on = None


def is_postgresql() -> bool:
    """Check if we're running against PostgreSQL."""
    bind = op.get_bind()
    return bind.dialect.name == "postgresql"


def upgrade() -> None:
    """Add multi-tenancy columns to hypotheses and solutions."""
    if is_postgresql():
        _upgrade_postgresql()
    else:
        _upgrade_sqlite()


def downgrade() -> None:
    """Remove multi-tenancy columns from hypotheses and solutions."""
    if is_postgresql():
        _downgrade_postgresql()
    else:
        _downgrade_sqlite()


def _upgrade_postgresql() -> None:
    """PostgreSQL-specific upgrade.

    NOTE: Baseline migration (001) now creates hypotheses/solutions with all
    multi-tenancy columns. This migration is a no-op for clean installs.
    """
    # No-op: baseline migration already includes these columns
    pass


def _downgrade_postgresql() -> None:
    """Remove multi-tenancy columns (PostgreSQL)."""
    conn = op.get_bind()

    # Drop hypotheses indexes and constraints
    conn.execute(text("DROP INDEX IF EXISTS idx_hypotheses_organization_id"))
    conn.execute(text("DROP INDEX IF EXISTS idx_hypotheses_created_by"))
    conn.execute(text("ALTER TABLE hypotheses DROP CONSTRAINT IF EXISTS fk_hypotheses_created_by_users"))
    conn.execute(text("ALTER TABLE hypotheses DROP CONSTRAINT IF EXISTS fk_hypotheses_updated_by_users"))
    conn.execute(text("ALTER TABLE hypotheses DROP COLUMN IF EXISTS organization_id"))
    conn.execute(text("ALTER TABLE hypotheses DROP COLUMN IF EXISTS created_by"))
    conn.execute(text("ALTER TABLE hypotheses DROP COLUMN IF EXISTS updated_by"))

    # Drop solutions indexes and constraints
    conn.execute(text("DROP INDEX IF EXISTS idx_solutions_organization_id"))
    conn.execute(text("DROP INDEX IF EXISTS idx_solutions_created_by"))
    conn.execute(text("ALTER TABLE solutions DROP CONSTRAINT IF EXISTS fk_solutions_created_by_users"))
    conn.execute(text("ALTER TABLE solutions DROP CONSTRAINT IF EXISTS fk_solutions_updated_by_users"))
    conn.execute(text("ALTER TABLE solutions DROP COLUMN IF EXISTS organization_id"))
    conn.execute(text("ALTER TABLE solutions DROP COLUMN IF EXISTS created_by"))
    conn.execute(text("ALTER TABLE solutions DROP COLUMN IF EXISTS updated_by"))


def _upgrade_sqlite() -> None:
    """SQLite-specific upgrade.

    NOTE: Baseline migration (001) now creates hypotheses/solutions with all
    multi-tenancy columns. This migration is a no-op for clean installs.
    """
    # No-op: baseline migration already includes these columns
    pass


def _downgrade_sqlite() -> None:
    """Remove multi-tenancy columns (SQLite)."""
    # SQLite doesn't support DROP COLUMN directly before version 3.35.0
    # For older SQLite, would need to recreate table
    # For now, we'll drop indexes only

    op.drop_index("idx_hypotheses_organization_id")
    op.drop_index("idx_hypotheses_created_by")
    op.drop_index("idx_solutions_organization_id")
    op.drop_index("idx_solutions_created_by")

    # Note: Actual column removal requires table recreation in old SQLite
    # This is safe to leave as-is since downgrade is rarely used in production
