"""001_baseline_schema

Baseline migration capturing existing FaultMaven database schema.

This migration represents the initial schema state as defined in:
- docs/schema/001_initial_hybrid_schema.sql (Cases Database - 10 tables)
- docs/schema/002_add_case_sharing.sql (Case sharing infrastructure)
- Enterprise user/teams schema (Enterprise Edition)
- docs/schema/004_kb_sharing_infrastructure.sql (KB document sharing)

The schema follows a hybrid normalized design:
- Normalized tables for high-cardinality data (evidence, hypotheses, solutions)
- JSONB columns for low-cardinality flexible data (consulting, conclusions)

Database Support:
- PostgreSQL: Full feature set with enums, GIN indexes, triggers
- SQLite: Simplified schema for development (TEXT instead of enums)

Revision ID: da6856719b5f
Revises:
Create Date: 2025-12-29 04:12:49.851535
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "da6856719b5f"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def is_postgresql() -> bool:
    """Check if we're running against PostgreSQL."""
    bind = op.get_bind()
    return bind.dialect.name == "postgresql"


def upgrade() -> None:
    """
    Apply baseline schema migration.

    Creates all tables required for FaultMaven operation.
    Schema adapts based on database dialect (PostgreSQL vs SQLite).
    """
    if is_postgresql():
        _upgrade_postgresql()
    else:
        _upgrade_sqlite()


def downgrade() -> None:
    """
    Remove all baseline schema objects.

    Drops all tables, views, and types created by this migration.
    """
    if is_postgresql():
        _downgrade_postgresql()
    else:
        _downgrade_sqlite()


# =============================================================================
# PostgreSQL Implementation
# =============================================================================

def _upgrade_postgresql() -> None:
    """Create PostgreSQL schema with full feature support."""
    conn = op.get_bind()

    # -------------------------------------------------------------------------
    # Extensions
    # -------------------------------------------------------------------------
    conn.execute(text('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"'))
    conn.execute(text('CREATE EXTENSION IF NOT EXISTS "pgcrypto"'))

    # -------------------------------------------------------------------------
    # Enums
    # -------------------------------------------------------------------------
    conn.execute(text("""
        DO $$ BEGIN
            CREATE TYPE case_status AS ENUM (
                'consulting', 'problem_verification', 'root_cause_analysis',
                'solution_implementation', 'resolved', 'closed', 'archived'
            );
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;
    """))

    conn.execute(text("""
        DO $$ BEGIN
            CREATE TYPE evidence_category AS ENUM (
                'LOGS_AND_ERRORS', 'STRUCTURED_CONFIG', 'METRICS_AND_PERFORMANCE',
                'UNSTRUCTURED_TEXT', 'SOURCE_CODE', 'VISUAL_EVIDENCE', 'UNKNOWN'
            );
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;
    """))

    conn.execute(text("""
        DO $$ BEGIN
            CREATE TYPE hypothesis_status AS ENUM (
                'proposed', 'testing', 'validated', 'invalidated', 'deferred'
            );
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;
    """))

    conn.execute(text("""
        DO $$ BEGIN
            CREATE TYPE solution_status AS ENUM (
                'proposed', 'in_progress', 'implemented', 'verified', 'rejected'
            );
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;
    """))

    conn.execute(text("""
        DO $$ BEGIN
            CREATE TYPE message_role AS ENUM ('user', 'assistant', 'system');
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;
    """))

    conn.execute(text("""
        DO $$ BEGIN
            CREATE TYPE file_processing_status AS ENUM (
                'pending', 'processing', 'completed', 'failed'
            );
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;
    """))

    # -------------------------------------------------------------------------
    # Table: cases (Main table)
    # -------------------------------------------------------------------------
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS cases (
            case_id VARCHAR(17) PRIMARY KEY,
            user_id VARCHAR(255) NOT NULL,
            title VARCHAR(200) NOT NULL,
            status case_status NOT NULL DEFAULT 'consulting',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            consulting JSONB NOT NULL DEFAULT '{"initial_description": "", "context": {}, "user_goals": []}'::jsonb,
            problem_verification JSONB DEFAULT NULL,
            working_conclusion JSONB DEFAULT NULL,
            root_cause_conclusion JSONB DEFAULT NULL,
            path_selection JSONB DEFAULT NULL,
            degraded_mode JSONB DEFAULT NULL,
            escalation_state JSONB DEFAULT NULL,
            documentation JSONB DEFAULT '{"summary": "", "timeline": [], "lessons_learned": []}'::jsonb,
            progress JSONB DEFAULT '{"current_phase": "consulting", "completion_percentage": 0, "milestones": []}'::jsonb,
            metadata JSONB DEFAULT '{}'::jsonb,
            org_id VARCHAR(20),
            team_id VARCHAR(20),
            CONSTRAINT cases_title_not_empty CHECK (LENGTH(TRIM(title)) > 0),
            CONSTRAINT cases_user_id_not_empty CHECK (LENGTH(TRIM(user_id)) > 0)
        )
    """))

    # Cases indexes
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_cases_user_id ON cases(user_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_cases_status ON cases(status)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_cases_created_at ON cases(created_at DESC)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_cases_updated_at ON cases(updated_at DESC)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_cases_org_id ON cases(org_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_cases_team_id ON cases(team_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_cases_consulting_gin ON cases USING GIN (consulting)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_cases_metadata_gin ON cases USING GIN (metadata)"))

    # -------------------------------------------------------------------------
    # Table: evidence
    # -------------------------------------------------------------------------
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS evidence (
            evidence_id VARCHAR(15) PRIMARY KEY,
            case_id VARCHAR(17) NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
            category evidence_category NOT NULL,
            summary VARCHAR(500) NOT NULL,
            preprocessed_content TEXT NOT NULL,
            content_ref VARCHAR(1000),
            file_size INTEGER,
            filename VARCHAR(255),
            upload_timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            metadata JSONB DEFAULT '{}'::jsonb,
            CONSTRAINT evidence_summary_not_empty CHECK (LENGTH(TRIM(summary)) > 0),
            CONSTRAINT evidence_content_not_empty CHECK (LENGTH(TRIM(preprocessed_content)) > 0)
        )
    """))

    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_evidence_case_id ON evidence(case_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_evidence_category ON evidence(category)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_evidence_upload_timestamp ON evidence(upload_timestamp DESC)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_evidence_metadata_gin ON evidence USING GIN (metadata)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_evidence_content_fts ON evidence USING GIN (to_tsvector('english', preprocessed_content))"))

    # -------------------------------------------------------------------------
    # Table: hypotheses
    # -------------------------------------------------------------------------
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS hypotheses (
            hypothesis_id VARCHAR(15) PRIMARY KEY,
            case_id VARCHAR(17) NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
            description TEXT NOT NULL,
            status hypothesis_status NOT NULL DEFAULT 'proposed',
            confidence_score DECIMAL(3,2),
            supporting_evidence_ids TEXT[],
            validation_result TEXT,
            validation_timestamp TIMESTAMPTZ,
            proposed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            metadata JSONB DEFAULT '{}'::jsonb,
            CONSTRAINT hypotheses_description_not_empty CHECK (LENGTH(TRIM(description)) > 0),
            CONSTRAINT hypotheses_confidence_range CHECK (confidence_score IS NULL OR (confidence_score >= 0 AND confidence_score <= 1))
        )
    """))

    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_hypotheses_case_id ON hypotheses(case_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_hypotheses_status ON hypotheses(status)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_hypotheses_proposed_at ON hypotheses(proposed_at DESC)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_hypotheses_confidence_score ON hypotheses(confidence_score DESC NULLS LAST)"))

    # -------------------------------------------------------------------------
    # Table: solutions
    # -------------------------------------------------------------------------
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS solutions (
            solution_id VARCHAR(15) PRIMARY KEY,
            case_id VARCHAR(17) NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
            description TEXT NOT NULL,
            status solution_status NOT NULL DEFAULT 'proposed',
            implementation_steps TEXT[],
            risk_level VARCHAR(20),
            estimated_effort VARCHAR(50),
            verification_result TEXT,
            verification_timestamp TIMESTAMPTZ,
            proposed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            implemented_at TIMESTAMPTZ,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            metadata JSONB DEFAULT '{}'::jsonb,
            CONSTRAINT solutions_description_not_empty CHECK (LENGTH(TRIM(description)) > 0),
            CONSTRAINT solutions_risk_level_valid CHECK (risk_level IS NULL OR risk_level IN ('low', 'medium', 'high', 'critical'))
        )
    """))

    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_solutions_case_id ON solutions(case_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_solutions_status ON solutions(status)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_solutions_proposed_at ON solutions(proposed_at DESC)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_solutions_risk_level ON solutions(risk_level)"))

    # -------------------------------------------------------------------------
    # Table: case_messages
    # -------------------------------------------------------------------------
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS case_messages (
            message_id VARCHAR(20) PRIMARY KEY,
            case_id VARCHAR(17) NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
            role message_role NOT NULL,
            content TEXT NOT NULL,
            timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            metadata JSONB DEFAULT '{}'::jsonb,
            CONSTRAINT case_messages_content_not_empty CHECK (LENGTH(TRIM(content)) > 0)
        )
    """))

    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_case_messages_case_id ON case_messages(case_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_case_messages_timestamp ON case_messages(timestamp ASC)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_case_messages_role ON case_messages(role)"))

    # -------------------------------------------------------------------------
    # Table: uploaded_files
    # -------------------------------------------------------------------------
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS uploaded_files (
            file_id VARCHAR(15) PRIMARY KEY,
            case_id VARCHAR(17) NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
            filename VARCHAR(255) NOT NULL,
            file_size INTEGER NOT NULL,
            content_type VARCHAR(100),
            storage_path VARCHAR(1000),
            processing_status file_processing_status NOT NULL DEFAULT 'pending',
            processing_error TEXT,
            uploaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            processed_at TIMESTAMPTZ,
            metadata JSONB DEFAULT '{}'::jsonb,
            CONSTRAINT uploaded_files_filename_not_empty CHECK (LENGTH(TRIM(filename)) > 0),
            CONSTRAINT uploaded_files_file_size_positive CHECK (file_size > 0)
        )
    """))

    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_uploaded_files_case_id ON uploaded_files(case_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_uploaded_files_uploaded_at ON uploaded_files(uploaded_at DESC)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_uploaded_files_processing_status ON uploaded_files(processing_status)"))

    # -------------------------------------------------------------------------
    # Table: case_status_transitions
    # -------------------------------------------------------------------------
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS case_status_transitions (
            transition_id SERIAL PRIMARY KEY,
            case_id VARCHAR(17) NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
            from_status case_status,
            to_status case_status NOT NULL,
            reason TEXT,
            transitioned_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            metadata JSONB DEFAULT '{}'::jsonb
        )
    """))

    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_case_status_transitions_case_id ON case_status_transitions(case_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_case_status_transitions_timestamp ON case_status_transitions(transitioned_at DESC)"))

    # -------------------------------------------------------------------------
    # Table: case_tags
    # -------------------------------------------------------------------------
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS case_tags (
            tag_id SERIAL PRIMARY KEY,
            case_id VARCHAR(17) NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
            tag VARCHAR(50) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT case_tags_unique UNIQUE (case_id, tag),
            CONSTRAINT case_tags_tag_not_empty CHECK (LENGTH(TRIM(tag)) > 0)
        )
    """))

    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_case_tags_case_id ON case_tags(case_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_case_tags_tag ON case_tags(tag)"))

    # -------------------------------------------------------------------------
    # Table: agent_tool_calls
    # -------------------------------------------------------------------------
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS agent_tool_calls (
            call_id VARCHAR(20) PRIMARY KEY,
            case_id VARCHAR(17) NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
            tool_name VARCHAR(100) NOT NULL,
            tool_input JSONB NOT NULL,
            tool_output JSONB,
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            error_message TEXT,
            duration_ms INTEGER,
            started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            completed_at TIMESTAMPTZ,
            metadata JSONB DEFAULT '{}'::jsonb,
            CONSTRAINT agent_tool_calls_tool_name_not_empty CHECK (LENGTH(TRIM(tool_name)) > 0),
            CONSTRAINT agent_tool_calls_status_valid CHECK (status IN ('pending', 'running', 'success', 'error'))
        )
    """))

    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_agent_tool_calls_case_id ON agent_tool_calls(case_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_agent_tool_calls_tool_name ON agent_tool_calls(tool_name)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_agent_tool_calls_status ON agent_tool_calls(status)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_agent_tool_calls_started_at ON agent_tool_calls(started_at DESC)"))

    # -------------------------------------------------------------------------
    # Function: update_updated_at_column
    # -------------------------------------------------------------------------
    conn.execute(text("""
        CREATE OR REPLACE FUNCTION update_updated_at_column()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = NOW();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """))

    # Triggers for updated_at
    conn.execute(text("""
        DROP TRIGGER IF EXISTS cases_update_updated_at ON cases;
        CREATE TRIGGER cases_update_updated_at
            BEFORE UPDATE ON cases
            FOR EACH ROW
            EXECUTE FUNCTION update_updated_at_column()
    """))

    conn.execute(text("""
        DROP TRIGGER IF EXISTS hypotheses_update_updated_at ON hypotheses;
        CREATE TRIGGER hypotheses_update_updated_at
            BEFORE UPDATE ON hypotheses
            FOR EACH ROW
            EXECUTE FUNCTION update_updated_at_column()
    """))

    conn.execute(text("""
        DROP TRIGGER IF EXISTS solutions_update_updated_at ON solutions;
        CREATE TRIGGER solutions_update_updated_at
            BEFORE UPDATE ON solutions
            FOR EACH ROW
            EXECUTE FUNCTION update_updated_at_column()
    """))

    # -------------------------------------------------------------------------
    # Views
    # -------------------------------------------------------------------------
    conn.execute(text("""
        CREATE OR REPLACE VIEW case_overview AS
        SELECT
            c.case_id,
            c.user_id,
            c.title,
            c.status,
            c.created_at,
            c.updated_at,
            COUNT(DISTINCT e.evidence_id) AS evidence_count,
            COUNT(DISTINCT h.hypothesis_id) AS hypothesis_count,
            COUNT(DISTINCT s.solution_id) AS solution_count,
            COUNT(DISTINCT m.message_id) AS message_count,
            COUNT(DISTINCT f.file_id) AS file_count
        FROM cases c
        LEFT JOIN evidence e ON c.case_id = e.case_id
        LEFT JOIN hypotheses h ON c.case_id = h.case_id
        LEFT JOIN solutions s ON c.case_id = s.case_id
        LEFT JOIN case_messages m ON c.case_id = m.case_id
        LEFT JOIN uploaded_files f ON c.case_id = f.case_id
        GROUP BY c.case_id, c.user_id, c.title, c.status, c.created_at, c.updated_at
    """))

    conn.execute(text("""
        CREATE OR REPLACE VIEW active_hypotheses AS
        SELECT
            h.hypothesis_id,
            h.case_id,
            h.description,
            h.status,
            h.confidence_score,
            h.proposed_at,
            c.title AS case_title,
            c.status AS case_status
        FROM hypotheses h
        JOIN cases c ON h.case_id = c.case_id
        WHERE h.status IN ('proposed', 'testing')
        ORDER BY h.confidence_score DESC NULLS LAST, h.proposed_at DESC
    """))


def _downgrade_postgresql() -> None:
    """Remove PostgreSQL schema objects."""
    conn = op.get_bind()

    # Drop views
    conn.execute(text("DROP VIEW IF EXISTS active_hypotheses CASCADE"))
    conn.execute(text("DROP VIEW IF EXISTS case_overview CASCADE"))

    # Drop triggers
    conn.execute(text("DROP TRIGGER IF EXISTS cases_update_updated_at ON cases CASCADE"))
    conn.execute(text("DROP TRIGGER IF EXISTS hypotheses_update_updated_at ON hypotheses CASCADE"))
    conn.execute(text("DROP TRIGGER IF EXISTS solutions_update_updated_at ON solutions CASCADE"))

    # Drop function
    conn.execute(text("DROP FUNCTION IF EXISTS update_updated_at_column() CASCADE"))

    # Drop tables in reverse order (respecting foreign keys)
    conn.execute(text("DROP TABLE IF EXISTS agent_tool_calls CASCADE"))
    conn.execute(text("DROP TABLE IF EXISTS case_tags CASCADE"))
    conn.execute(text("DROP TABLE IF EXISTS case_status_transitions CASCADE"))
    conn.execute(text("DROP TABLE IF EXISTS uploaded_files CASCADE"))
    conn.execute(text("DROP TABLE IF EXISTS case_messages CASCADE"))
    conn.execute(text("DROP TABLE IF EXISTS solutions CASCADE"))
    conn.execute(text("DROP TABLE IF EXISTS hypotheses CASCADE"))
    conn.execute(text("DROP TABLE IF EXISTS evidence CASCADE"))
    conn.execute(text("DROP TABLE IF EXISTS cases CASCADE"))

    # Drop enums
    conn.execute(text("DROP TYPE IF EXISTS file_processing_status CASCADE"))
    conn.execute(text("DROP TYPE IF EXISTS message_role CASCADE"))
    conn.execute(text("DROP TYPE IF EXISTS solution_status CASCADE"))
    conn.execute(text("DROP TYPE IF EXISTS hypothesis_status CASCADE"))
    conn.execute(text("DROP TYPE IF EXISTS evidence_category CASCADE"))
    conn.execute(text("DROP TYPE IF EXISTS case_status CASCADE"))


# =============================================================================
# SQLite Implementation (Development)
# =============================================================================

def _upgrade_sqlite() -> None:
    """Create SQLite schema for development (simplified, without PostgreSQL features)."""

    # Table: cases
    op.create_table(
        "cases",
        sa.Column("case_id", sa.String(17), primary_key=True),
        sa.Column("user_id", sa.String(255), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("status", sa.String(50), nullable=False, server_default="consulting"),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("consulting", sa.Text, nullable=False, server_default='{}'),
        sa.Column("problem_verification", sa.Text),
        sa.Column("working_conclusion", sa.Text),
        sa.Column("root_cause_conclusion", sa.Text),
        sa.Column("path_selection", sa.Text),
        sa.Column("degraded_mode", sa.Text),
        sa.Column("escalation_state", sa.Text),
        sa.Column("documentation", sa.Text, server_default='{}'),
        sa.Column("progress", sa.Text, server_default='{}'),
        sa.Column("metadata", sa.Text, server_default='{}'),
        sa.Column("org_id", sa.String(20)),
        sa.Column("team_id", sa.String(20)),
    )

    op.create_index("idx_cases_user_id", "cases", ["user_id"])
    op.create_index("idx_cases_status", "cases", ["status"])
    op.create_index("idx_cases_created_at", "cases", ["created_at"])

    # Table: evidence
    op.create_table(
        "evidence",
        sa.Column("evidence_id", sa.String(15), primary_key=True),
        sa.Column("case_id", sa.String(17), sa.ForeignKey("cases.case_id", ondelete="CASCADE"), nullable=False),
        sa.Column("category", sa.String(50), nullable=False),
        sa.Column("summary", sa.String(500), nullable=False),
        sa.Column("preprocessed_content", sa.Text, nullable=False),
        sa.Column("content_ref", sa.String(1000)),
        sa.Column("file_size", sa.Integer),
        sa.Column("filename", sa.String(255)),
        sa.Column("upload_timestamp", sa.DateTime, nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("metadata", sa.Text, server_default='{}'),
    )

    op.create_index("idx_evidence_case_id", "evidence", ["case_id"])
    op.create_index("idx_evidence_category", "evidence", ["category"])

    # Table: hypotheses
    op.create_table(
        "hypotheses",
        sa.Column("hypothesis_id", sa.String(15), primary_key=True),
        sa.Column("case_id", sa.String(17), sa.ForeignKey("cases.case_id", ondelete="CASCADE"), nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="proposed"),
        sa.Column("confidence_score", sa.Numeric(3, 2)),
        sa.Column("supporting_evidence_ids", sa.Text),
        sa.Column("validation_result", sa.Text),
        sa.Column("validation_timestamp", sa.DateTime),
        sa.Column("proposed_at", sa.DateTime, nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("metadata", sa.Text, server_default='{}'),
    )

    op.create_index("idx_hypotheses_case_id", "hypotheses", ["case_id"])
    op.create_index("idx_hypotheses_status", "hypotheses", ["status"])

    # Table: solutions
    op.create_table(
        "solutions",
        sa.Column("solution_id", sa.String(15), primary_key=True),
        sa.Column("case_id", sa.String(17), sa.ForeignKey("cases.case_id", ondelete="CASCADE"), nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="proposed"),
        sa.Column("implementation_steps", sa.Text),
        sa.Column("risk_level", sa.String(20)),
        sa.Column("estimated_effort", sa.String(50)),
        sa.Column("verification_result", sa.Text),
        sa.Column("verification_timestamp", sa.DateTime),
        sa.Column("proposed_at", sa.DateTime, nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("implemented_at", sa.DateTime),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("metadata", sa.Text, server_default='{}'),
    )

    op.create_index("idx_solutions_case_id", "solutions", ["case_id"])
    op.create_index("idx_solutions_status", "solutions", ["status"])

    # Table: case_messages
    op.create_table(
        "case_messages",
        sa.Column("message_id", sa.String(20), primary_key=True),
        sa.Column("case_id", sa.String(17), sa.ForeignKey("cases.case_id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("timestamp", sa.DateTime, nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("metadata", sa.Text, server_default='{}'),
    )

    op.create_index("idx_case_messages_case_id", "case_messages", ["case_id"])
    op.create_index("idx_case_messages_timestamp", "case_messages", ["timestamp"])

    # Table: uploaded_files
    op.create_table(
        "uploaded_files",
        sa.Column("file_id", sa.String(15), primary_key=True),
        sa.Column("case_id", sa.String(17), sa.ForeignKey("cases.case_id", ondelete="CASCADE"), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("file_size", sa.Integer, nullable=False),
        sa.Column("content_type", sa.String(100)),
        sa.Column("storage_path", sa.String(1000)),
        sa.Column("processing_status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("processing_error", sa.Text),
        sa.Column("uploaded_at", sa.DateTime, nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("processed_at", sa.DateTime),
        sa.Column("metadata", sa.Text, server_default='{}'),
    )

    op.create_index("idx_uploaded_files_case_id", "uploaded_files", ["case_id"])

    # Table: case_status_transitions
    op.create_table(
        "case_status_transitions",
        sa.Column("transition_id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("case_id", sa.String(17), sa.ForeignKey("cases.case_id", ondelete="CASCADE"), nullable=False),
        sa.Column("from_status", sa.String(50)),
        sa.Column("to_status", sa.String(50), nullable=False),
        sa.Column("reason", sa.Text),
        sa.Column("transitioned_at", sa.DateTime, nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("metadata", sa.Text, server_default='{}'),
    )

    op.create_index("idx_case_status_transitions_case_id", "case_status_transitions", ["case_id"])

    # Table: case_tags
    op.create_table(
        "case_tags",
        sa.Column("tag_id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("case_id", sa.String(17), sa.ForeignKey("cases.case_id", ondelete="CASCADE"), nullable=False),
        sa.Column("tag", sa.String(50), nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.current_timestamp()),
        sa.UniqueConstraint("case_id", "tag", name="case_tags_unique"),
    )

    op.create_index("idx_case_tags_case_id", "case_tags", ["case_id"])
    op.create_index("idx_case_tags_tag", "case_tags", ["tag"])

    # Table: agent_tool_calls
    op.create_table(
        "agent_tool_calls",
        sa.Column("call_id", sa.String(20), primary_key=True),
        sa.Column("case_id", sa.String(17), sa.ForeignKey("cases.case_id", ondelete="CASCADE"), nullable=False),
        sa.Column("tool_name", sa.String(100), nullable=False),
        sa.Column("tool_input", sa.Text, nullable=False),
        sa.Column("tool_output", sa.Text),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("error_message", sa.Text),
        sa.Column("duration_ms", sa.Integer),
        sa.Column("started_at", sa.DateTime, nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("completed_at", sa.DateTime),
        sa.Column("metadata", sa.Text, server_default='{}'),
    )

    op.create_index("idx_agent_tool_calls_case_id", "agent_tool_calls", ["case_id"])
    op.create_index("idx_agent_tool_calls_tool_name", "agent_tool_calls", ["tool_name"])


def _downgrade_sqlite() -> None:
    """Remove SQLite schema objects."""
    op.drop_table("agent_tool_calls")
    op.drop_table("case_tags")
    op.drop_table("case_status_transitions")
    op.drop_table("uploaded_files")
    op.drop_table("case_messages")
    op.drop_table("solutions")
    op.drop_table("hypotheses")
    op.drop_table("evidence")
    op.drop_table("cases")
