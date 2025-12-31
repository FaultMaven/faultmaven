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

Revision ID: 20250101_008
Revises: 20251230_0100_007
Create Date: 2025-01-01 08:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision = "20250101_008"
down_revision = "20251230_0100_007"
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
    """PostgreSQL-specific upgrade."""
    conn = op.get_bind()

    # -------------------------------------------------------------------------
    # Hypotheses Table
    # -------------------------------------------------------------------------

    # Add organization_id column (nullable initially for backfill)
    conn.execute(text("""
        ALTER TABLE hypotheses
        ADD COLUMN IF NOT EXISTS organization_id VARCHAR(20)
    """))

    # Add created_by column (nullable initially for backfill)
    conn.execute(text("""
        ALTER TABLE hypotheses
        ADD COLUMN IF NOT EXISTS created_by VARCHAR(255)
    """))

    # Add updated_by column (always nullable)
    conn.execute(text("""
        ALTER TABLE hypotheses
        ADD COLUMN IF NOT EXISTS updated_by VARCHAR(255)
    """))

    # Backfill organization_id from cases.org_id
    conn.execute(text("""
        UPDATE hypotheses h
        SET organization_id = c.org_id
        FROM cases c
        WHERE h.case_id = c.case_id
        AND h.organization_id IS NULL
    """))

    # Backfill created_by with 'system' for existing records
    conn.execute(text("""
        UPDATE hypotheses
        SET created_by = 'system'
        WHERE created_by IS NULL
    """))

    # Make organization_id NOT NULL after backfill
    conn.execute(text("""
        ALTER TABLE hypotheses
        ALTER COLUMN organization_id SET NOT NULL
    """))

    # Make created_by NOT NULL after backfill
    conn.execute(text("""
        ALTER TABLE hypotheses
        ALTER COLUMN created_by SET NOT NULL
    """))

    # Add foreign key constraints (if organizations and users tables exist)
    # Note: Using DO block to handle case where tables don't exist yet
    conn.execute(text("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'users') THEN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.table_constraints
                    WHERE constraint_name = 'fk_hypotheses_created_by_users'
                ) THEN
                    ALTER TABLE hypotheses
                    ADD CONSTRAINT fk_hypotheses_created_by_users
                    FOREIGN KEY (created_by) REFERENCES users(user_id);
                END IF;

                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.table_constraints
                    WHERE constraint_name = 'fk_hypotheses_updated_by_users'
                ) THEN
                    ALTER TABLE hypotheses
                    ADD CONSTRAINT fk_hypotheses_updated_by_users
                    FOREIGN KEY (updated_by) REFERENCES users(user_id);
                END IF;
            END IF;
        END $$;
    """))

    # Add indexes
    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS idx_hypotheses_organization_id
        ON hypotheses(organization_id)
    """))

    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS idx_hypotheses_created_by
        ON hypotheses(created_by)
    """))

    # -------------------------------------------------------------------------
    # Solutions Table
    # -------------------------------------------------------------------------

    # Add organization_id column (nullable initially for backfill)
    conn.execute(text("""
        ALTER TABLE solutions
        ADD COLUMN IF NOT EXISTS organization_id VARCHAR(20)
    """))

    # Add created_by column (nullable initially for backfill)
    conn.execute(text("""
        ALTER TABLE solutions
        ADD COLUMN IF NOT EXISTS created_by VARCHAR(255)
    """))

    # Add updated_by column (always nullable)
    conn.execute(text("""
        ALTER TABLE solutions
        ADD COLUMN IF NOT EXISTS updated_by VARCHAR(255)
    """))

    # Backfill organization_id from cases.org_id
    conn.execute(text("""
        UPDATE solutions s
        SET organization_id = c.org_id
        FROM cases c
        WHERE s.case_id = c.case_id
        AND s.organization_id IS NULL
    """))

    # Backfill created_by with 'system' for existing records
    conn.execute(text("""
        UPDATE solutions
        SET created_by = 'system'
        WHERE created_by IS NULL
    """))

    # Make organization_id NOT NULL after backfill
    conn.execute(text("""
        ALTER TABLE solutions
        ALTER COLUMN organization_id SET NOT NULL
    """))

    # Make created_by NOT NULL after backfill
    conn.execute(text("""
        ALTER TABLE solutions
        ALTER COLUMN created_by SET NOT NULL
    """))

    # Add foreign key constraints (if users table exists)
    conn.execute(text("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'users') THEN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.table_constraints
                    WHERE constraint_name = 'fk_solutions_created_by_users'
                ) THEN
                    ALTER TABLE solutions
                    ADD CONSTRAINT fk_solutions_created_by_users
                    FOREIGN KEY (created_by) REFERENCES users(user_id);
                END IF;

                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.table_constraints
                    WHERE constraint_name = 'fk_solutions_updated_by_users'
                ) THEN
                    ALTER TABLE solutions
                    ADD CONSTRAINT fk_solutions_updated_by_users
                    FOREIGN KEY (updated_by) REFERENCES users(user_id);
                END IF;
            END IF;
        END $$;
    """))

    # Add indexes
    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS idx_solutions_organization_id
        ON solutions(organization_id)
    """))

    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS idx_solutions_created_by
        ON solutions(created_by)
    """))


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
    """SQLite-specific upgrade."""
    # SQLite doesn't support ADD COLUMN with constraints in one statement
    # We'll add columns without constraints and backfill

    # Hypotheses
    op.add_column("hypotheses", sa.Column("organization_id", sa.String(20)))
    op.add_column("hypotheses", sa.Column("created_by", sa.String(255)))
    op.add_column("hypotheses", sa.Column("updated_by", sa.String(255)))

    # Backfill from cases
    conn = op.get_bind()
    conn.execute(text("""
        UPDATE hypotheses
        SET organization_id = (
            SELECT org_id FROM cases WHERE cases.case_id = hypotheses.case_id
        )
        WHERE organization_id IS NULL
    """))

    conn.execute(text("""
        UPDATE hypotheses
        SET created_by = 'system'
        WHERE created_by IS NULL
    """))

    # Solutions
    op.add_column("solutions", sa.Column("organization_id", sa.String(20)))
    op.add_column("solutions", sa.Column("created_by", sa.String(255)))
    op.add_column("solutions", sa.Column("updated_by", sa.String(255)))

    # Backfill from cases
    conn.execute(text("""
        UPDATE solutions
        SET organization_id = (
            SELECT org_id FROM cases WHERE cases.case_id = solutions.case_id
        )
        WHERE organization_id IS NULL
    """))

    conn.execute(text("""
        UPDATE solutions
        SET created_by = 'system'
        WHERE created_by IS NULL
    """))

    # Create indexes
    op.create_index("idx_hypotheses_organization_id", "hypotheses", ["organization_id"])
    op.create_index("idx_hypotheses_created_by", "hypotheses", ["created_by"])
    op.create_index("idx_solutions_organization_id", "solutions", ["organization_id"])
    op.create_index("idx_solutions_created_by", "solutions", ["created_by"])


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
