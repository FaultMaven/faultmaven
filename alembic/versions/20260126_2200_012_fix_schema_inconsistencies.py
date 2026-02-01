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
    """Fix schema inconsistencies.

    NOTE: Baseline migration (001) now creates cases table with organization_id.
    This migration is a no-op for clean installs.
    """
    # No-op: baseline migration already includes correct schema
    pass


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
