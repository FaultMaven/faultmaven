"""Fix schema inconsistencies between domain models and database

Revision ID: 20260126_2200_010
Revises: 20250109_1000_008
Create Date: 2026-01-26 22:00:00.000000

Changes:
1. Rename org_id → organization_id in cases table
2. Make organization_id NOT NULL (required in domain model)
3. Add missing columns to cases table: description, investigation_strategy, last_activity_at, resolved_at, closed_at

This aligns the database schema with the Case domain model.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text


# revision identifiers, used by Alembic.
revision: str = "012_fix_schema_inconsistencies"
down_revision: Union[str, None] = "011_add_standalone_evidence"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Fix schema inconsistencies."""
    conn = op.get_bind()

    # Check if we're using PostgreSQL or SQLite
    dialect = conn.dialect.name

    if dialect == "postgresql":
        # PostgreSQL: Use ALTER TABLE to rename and add columns

        # 1. Rename org_id to organization_id
        conn.execute(
            text(
                """
            ALTER TABLE cases
            RENAME COLUMN org_id TO organization_id
        """
            )
        )

        # 2. Set default value for existing NULL organization_id rows
        conn.execute(
            text(
                """
            UPDATE cases
            SET organization_id = 'default_org'
            WHERE organization_id IS NULL
        """
            )
        )

        # 3. Make organization_id NOT NULL
        conn.execute(
            text(
                """
            ALTER TABLE cases
            ALTER COLUMN organization_id SET NOT NULL
        """
            )
        )

        # 4. Change organization_id length to match domain model (255)
        conn.execute(
            text(
                """
            ALTER TABLE cases
            ALTER COLUMN organization_id TYPE VARCHAR(255)
        """
            )
        )

        # 5. Add missing columns
        conn.execute(
            text(
                """
            ALTER TABLE cases
            ADD COLUMN IF NOT EXISTS description TEXT DEFAULT ''
        """
            )
        )

        conn.execute(
            text(
                """
            ALTER TABLE cases
            ADD COLUMN IF NOT EXISTS investigation_strategy VARCHAR(50) DEFAULT 'post_mortem'
        """
            )
        )

        conn.execute(
            text(
                """
            ALTER TABLE cases
            ADD COLUMN IF NOT EXISTS last_activity_at TIMESTAMPTZ
        """
            )
        )

        conn.execute(
            text(
                """
            ALTER TABLE cases
            ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ
        """
            )
        )

        conn.execute(
            text(
                """
            ALTER TABLE cases
            ADD COLUMN IF NOT EXISTS closed_at TIMESTAMPTZ
        """
            )
        )

    elif dialect == "sqlite":
        # SQLite: Cannot ALTER COLUMN, must recreate table

        # 1. Create new table with correct schema
        conn.execute(
            text(
                """
            CREATE TABLE cases_new (
                case_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                organization_id TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                investigation_strategy TEXT DEFAULT 'post_mortem',
                status TEXT NOT NULL DEFAULT 'inquiry',
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_activity_at TIMESTAMP,
                resolved_at TIMESTAMP,
                closed_at TIMESTAMP,
                consulting TEXT NOT NULL DEFAULT '{"initial_description": "", "context": {}, "user_goals": []}',
                problem_verification TEXT,
                working_conclusion TEXT,
                root_cause_conclusion TEXT,
                path_selection TEXT,
                degraded_mode TEXT,
                escalation_state TEXT,
                documentation TEXT DEFAULT '{"summary": "", "timeline": [], "lessons_learned": []}',
                progress TEXT DEFAULT '{"current_phase": "inquiry", "completion_percentage": 0, "milestones": []}',
                metadata TEXT DEFAULT '{}',
                team_id TEXT,
                CHECK (LENGTH(TRIM(title)) > 0),
                CHECK (LENGTH(TRIM(user_id)) > 0)
            )
        """
            )
        )

        # 2. Copy data from old table, mapping org_id to organization_id
        # and inquiry to consulting (SQLite baseline used 'inquiry')
        conn.execute(
            text(
                """
            INSERT INTO cases_new (
                case_id, user_id, organization_id, title, status,
                created_at, updated_at, consulting, problem_verification,
                working_conclusion, root_cause_conclusion, path_selection,
                degraded_mode, escalation_state, documentation, progress,
                metadata, team_id
            )
            SELECT
                case_id, user_id, COALESCE(org_id, 'default_org'), title, status,
                created_at, updated_at, inquiry, problem_verification,
                working_conclusion, root_cause_conclusion, path_selection,
                degraded_mode, escalation_state, documentation, progress,
                metadata, team_id
            FROM cases
        """
            )
        )

        # 3. Drop old table
        conn.execute(text("DROP TABLE cases"))

        # 4. Rename new table
        conn.execute(text("ALTER TABLE cases_new RENAME TO cases"))

        # 5. Recreate indexes
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS idx_cases_user_id ON cases(user_id)")
        )
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS idx_cases_status ON cases(status)")
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_cases_created_at ON cases(created_at DESC)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_cases_updated_at ON cases(updated_at DESC)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_cases_organization_id ON cases(organization_id)"
            )
        )
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS idx_cases_team_id ON cases(team_id)")
        )


def downgrade() -> None:
    """Revert schema changes."""
    conn = op.get_bind()
    dialect = conn.dialect.name

    if dialect == "postgresql":
        # Remove added columns
        conn.execute(text("ALTER TABLE cases DROP COLUMN IF EXISTS description"))
        conn.execute(
            text("ALTER TABLE cases DROP COLUMN IF EXISTS investigation_strategy")
        )
        conn.execute(text("ALTER TABLE cases DROP COLUMN IF EXISTS last_activity_at"))
        conn.execute(text("ALTER TABLE cases DROP COLUMN IF EXISTS resolved_at"))
        conn.execute(text("ALTER TABLE cases DROP COLUMN IF EXISTS closed_at"))

        # Rename back and make nullable
        conn.execute(
            text("ALTER TABLE cases ALTER COLUMN organization_id DROP NOT NULL")
        )
        conn.execute(
            text("ALTER TABLE cases ALTER COLUMN organization_id TYPE VARCHAR(20)")
        )
        conn.execute(text("ALTER TABLE cases RENAME COLUMN organization_id TO org_id"))

    elif dialect == "sqlite":
        # SQLite: Recreate old table structure
        conn.execute(
            text(
                """
            CREATE TABLE cases_old (
                case_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'inquiry',
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                consulting TEXT NOT NULL DEFAULT '{"initial_description": "", "context": {}, "user_goals": []}',
                problem_verification TEXT,
                working_conclusion TEXT,
                root_cause_conclusion TEXT,
                path_selection TEXT,
                degraded_mode TEXT,
                escalation_state TEXT,
                documentation TEXT DEFAULT '{"summary": "", "timeline": [], "lessons_learned": []}',
                progress TEXT DEFAULT '{"current_phase": "inquiry", "completion_percentage": 0, "milestones": []}',
                metadata TEXT DEFAULT '{}',
                org_id TEXT,
                team_id TEXT
            )
        """
            )
        )

        conn.execute(
            text(
                """
            INSERT INTO cases_old
            SELECT case_id, user_id, title, status, created_at, updated_at,
                   consulting, problem_verification, working_conclusion,
                   root_cause_conclusion, path_selection, degraded_mode,
                   escalation_state, documentation, progress, metadata,
                   organization_id, team_id
            FROM cases
        """
            )
        )

        conn.execute(text("DROP TABLE cases"))
        conn.execute(text("ALTER TABLE cases_old RENAME TO cases"))

        # Recreate indexes
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS idx_cases_user_id ON cases(user_id)")
        )
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS idx_cases_status ON cases(status)")
        )
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS idx_cases_created_at ON cases(created_at)")
        )
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS idx_cases_updated_at ON cases(updated_at)")
        )
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS idx_cases_org_id ON cases(org_id)")
        )
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS idx_cases_team_id ON cases(team_id)")
        )
