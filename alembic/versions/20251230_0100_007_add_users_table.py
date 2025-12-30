"""007_add_users_table

Add users table for user management (TASK-018).

This migration creates the users table with:
- User identity (user_id, email, username)
- Authentication (hashed_password, is_active)
- Profile (display_name, avatar_url, timezone, locale)
- Email verification (is_email_verified, email_verified_at)
- SSO integration (sso_provider, sso_provider_id)
- Timestamps (created_at, updated_at, last_login_at, last_password_change_at)
- Soft delete (deleted_at)
- Roles (for development, production uses organization_members table)

Revision ID: 7a8b9c0d1e2f
Revises: 6f5e4d3c2b1a (add_knowledge_items)
Create Date: 2025-12-30 01:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "007_users_table"
down_revision: Union[str, Sequence[str], None] = "006_knowledge_items"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def is_postgresql() -> bool:
    """Check if we're running against PostgreSQL."""
    bind = op.get_bind()
    return bind.dialect.name == "postgresql"


def upgrade() -> None:
    """Create users table."""
    if is_postgresql():
        _upgrade_postgresql()
    else:
        _upgrade_sqlite()


def downgrade() -> None:
    """Drop users table."""
    if is_postgresql():
        _downgrade_postgresql()
    else:
        _downgrade_sqlite()


# =============================================================================
# PostgreSQL Implementation
# =============================================================================

def _upgrade_postgresql() -> None:
    """Create PostgreSQL users table with full feature support."""
    conn = op.get_bind()

    # -------------------------------------------------------------------------
    # Users Table
    # -------------------------------------------------------------------------
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS users (
            -- Identity
            user_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            username VARCHAR(100) NOT NULL,
            email VARCHAR(255) NOT NULL,

            -- Authentication
            hashed_password TEXT,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,

            -- Profile
            display_name VARCHAR(200) NOT NULL,
            avatar_url VARCHAR(500),
            timezone VARCHAR(50) NOT NULL DEFAULT 'UTC',
            locale VARCHAR(10) NOT NULL DEFAULT 'en-US',

            -- Email Verification
            is_email_verified BOOLEAN NOT NULL DEFAULT FALSE,
            email_verified_at TIMESTAMP WITH TIME ZONE,

            -- SSO Integration
            sso_provider VARCHAR(50),
            sso_provider_id VARCHAR(255),

            -- Timestamps
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            last_login_at TIMESTAMP WITH TIME ZONE,
            last_password_change_at TIMESTAMP WITH TIME ZONE,

            -- Soft Delete
            deleted_at TIMESTAMP WITH TIME ZONE,

            -- Roles (development only, production uses organization_members)
            roles TEXT DEFAULT ''
        )
    """))

    # -------------------------------------------------------------------------
    # Indexes
    # -------------------------------------------------------------------------
    conn.execute(text("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email
        ON users(LOWER(email))
    """))

    conn.execute(text("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username
        ON users(LOWER(username))
    """))

    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS idx_users_is_active
        ON users(is_active)
    """))

    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS idx_users_created_at
        ON users(created_at)
    """))

    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS idx_users_sso_provider_id
        ON users(sso_provider, sso_provider_id)
        WHERE sso_provider IS NOT NULL
    """))

    # -------------------------------------------------------------------------
    # Updated At Trigger
    # -------------------------------------------------------------------------
    conn.execute(text("""
        CREATE OR REPLACE FUNCTION update_users_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = NOW();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """))

    conn.execute(text("""
        CREATE TRIGGER trigger_users_updated_at
            BEFORE UPDATE ON users
            FOR EACH ROW
            EXECUTE FUNCTION update_users_updated_at()
    """))


def _downgrade_postgresql() -> None:
    """Drop PostgreSQL users table and related objects."""
    conn = op.get_bind()

    conn.execute(text("DROP TRIGGER IF EXISTS trigger_users_updated_at ON users"))
    conn.execute(text("DROP FUNCTION IF EXISTS update_users_updated_at()"))
    conn.execute(text("DROP TABLE IF EXISTS users CASCADE"))


# =============================================================================
# SQLite Implementation
# =============================================================================

def _upgrade_sqlite() -> None:
    """Create SQLite users table for development."""
    op.create_table(
        'users',
        # Identity
        sa.Column('user_id', sa.Text(), primary_key=True),
        sa.Column('username', sa.Text(), nullable=False),
        sa.Column('email', sa.Text(), nullable=False),

        # Authentication
        sa.Column('hashed_password', sa.Text()),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='1'),

        # Profile
        sa.Column('display_name', sa.Text(), nullable=False),
        sa.Column('avatar_url', sa.Text()),
        sa.Column('timezone', sa.Text(), nullable=False, server_default='UTC'),
        sa.Column('locale', sa.Text(), nullable=False, server_default='en-US'),

        # Email Verification
        sa.Column('is_email_verified', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('email_verified_at', sa.DateTime()),

        # SSO Integration
        sa.Column('sso_provider', sa.Text()),
        sa.Column('sso_provider_id', sa.Text()),

        # Timestamps
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('last_login_at', sa.DateTime()),
        sa.Column('last_password_change_at', sa.DateTime()),

        # Soft Delete
        sa.Column('deleted_at', sa.DateTime()),

        # Roles (development)
        sa.Column('roles', sa.Text(), server_default=''),
    )

    # Create indexes
    op.create_index('idx_users_email', 'users', ['email'], unique=True)
    op.create_index('idx_users_username', 'users', ['username'], unique=True)
    op.create_index('idx_users_is_active', 'users', ['is_active'])
    op.create_index('idx_users_created_at', 'users', ['created_at'])


def _downgrade_sqlite() -> None:
    """Drop SQLite users table."""
    op.drop_table('users')
