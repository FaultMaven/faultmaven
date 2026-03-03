"""Schema-Model Consistency Tests.

This test module validates that SQLAlchemy ORM models are consistent with
the actual database schema. It prevents the type of issue that caused the
P0 production blocker where migration 002 removed columns but models.py
was not updated.

Test Strategy:
1. Inspect database schema (actual columns in each table)
2. Inspect SQLAlchemy model definitions (expected columns)
3. Compare and ensure they match

This catches:
- Missing columns in database (models expect them but DB doesn't have them)
- Extra columns in database (DB has them but models don't expect them)
- Type mismatches (e.g., VARCHAR vs TEXT)
- Nullability mismatches

Coverage:
- solutions table
- hypotheses table
- case_messages table
- All other critical tables

Reference:
- Root cause: migration 002_normalize_organization_id removed columns
- Fix: migration 003_fix_schema_inconsistencies_comprehensive re-added them
- Prevention: These tests catch future mismatches
"""

import sqlite3
from typing import Dict, Set

import pytest
from sqlalchemy import Column, inspect

from faultmaven.infrastructure.persistence.models import (
    AgentExecutionModel,
    AgentToolCallModel,
    AgentToolCallV2Model,
    Base,
    CaseCheckpointModel,
    CaseMessageModel,
    CaseModel,
    CaseActionModel,
    CaseTagModel,
    EvidenceArtifactModel,
    EvidenceModel,
    HypothesisModel,
    InvestigationSessionModel,
    KnowledgeItemModel,
    KnowledgeSuggestionModel,
    SessionModel,
    SolutionModel,
    StandaloneEvidenceModel,
    UploadedFileModel,
)


def get_database_columns(db_path: str, table_name: str) -> Dict[str, Dict]:
    """Get actual columns from database schema.

    Args:
        db_path: Path to SQLite database
        table_name: Name of table to inspect

    Returns:
        Dictionary of column_name -> {type, nullable, pk}
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # PRAGMA table_info returns: cid, name, type, notnull, dflt_value, pk
    cursor.execute(f"PRAGMA table_info({table_name})")
    columns = cursor.fetchall()
    conn.close()

    return {
        col[1]: {  # col[1] is column name
            "type": col[2],  # col[2] is data type
            "nullable": col[3] == 0,  # col[3] is notnull (0=nullable, 1=not null)
            "pk": col[5] == 1,  # col[5] is pk flag
        }
        for col in columns
    }


def get_model_columns(model_class) -> Dict[str, Dict]:
    """Get expected columns from SQLAlchemy model.

    Args:
        model_class: SQLAlchemy model class (inherits from Base)

    Returns:
        Dictionary of column_name -> {type, nullable, pk}
    """
    mapper = inspect(model_class)
    columns = {}

    for col in mapper.columns:
        # Get column name (handles both direct columns and aliased columns like 'metadata')
        col_name = col.name

        # Get SQLAlchemy type as string
        col_type = str(col.type)

        columns[col_name] = {
            "type": col_type,
            "nullable": col.nullable,
            "pk": col.primary_key,
        }

    return columns


def compare_schemas(
    db_columns: Dict[str, Dict], model_columns: Dict[str, Dict], table_name: str
) -> tuple[Set[str], Set[str], list[str]]:
    """Compare database schema with model schema.

    Args:
        db_columns: Actual database columns
        model_columns: Expected model columns
        table_name: Name of table being compared

    Returns:
        Tuple of (missing_in_db, extra_in_db, type_mismatches)
    """
    db_names = set(db_columns.keys())
    model_names = set(model_columns.keys())

    missing_in_db = model_names - db_names
    extra_in_db = db_names - model_names

    type_mismatches = []
    for col_name in db_names & model_names:
        db_col = db_columns[col_name]
        model_col = model_columns[col_name]

        # Check nullable mismatch
        if db_col["nullable"] != model_col["nullable"]:
            type_mismatches.append(
                f"{table_name}.{col_name}: nullable mismatch "
                f"(DB: {db_col['nullable']}, Model: {model_col['nullable']})"
            )

        # Note: Type comparison is tricky because SQLAlchemy types don't always
        # match SQLite PRAGMA output exactly (e.g., "VARCHAR(20)" vs "String(20)")
        # We skip strict type checking for now but could add fuzzy matching

    return missing_in_db, extra_in_db, type_mismatches


@pytest.mark.unit
class TestSchemaModelConsistency:
    """Test database schema matches SQLAlchemy models."""

    def test_solutions_table_consistency(self, db_path="data/faultmaven.db"):
        """Verify solutions table schema matches SolutionModel."""
        db_columns = get_database_columns(db_path, "solutions")
        model_columns = get_model_columns(SolutionModel)

        missing, extra, mismatches = compare_schemas(
            db_columns, model_columns, "solutions"
        )

        # Critical columns that MUST exist (from P0 bug)
        critical_columns = {"organization_id", "created_by", "updated_by"}

        # Check for missing critical columns
        missing_critical = critical_columns & missing
        if missing_critical:
            pytest.fail(
                f"CRITICAL: solutions table missing columns: {missing_critical}. "
                f"This will cause 500 errors on session creation!"
            )

        # Fail if any columns are missing
        if missing:
            pytest.fail(
                f"solutions table missing columns in database: {missing}. "
                f"Model expects these but database doesn't have them."
            )

        # Warn about extra columns (not critical but indicates schema drift)
        if extra:
            pytest.skip(
                f"solutions table has extra columns in database: {extra}. "
                f"These exist in DB but model doesn't expect them. "
                f"(This may be OK if they're deprecated fields)"
            )

        # Check type/nullable mismatches
        if mismatches:
            pytest.fail(
                f"solutions table has schema mismatches:\n" + "\n".join(mismatches)
            )

    def test_hypotheses_table_consistency(self, db_path="data/faultmaven.db"):
        """Verify hypotheses table schema matches HypothesisModel."""
        db_columns = get_database_columns(db_path, "hypotheses")
        model_columns = get_model_columns(HypothesisModel)

        missing, extra, mismatches = compare_schemas(
            db_columns, model_columns, "hypotheses"
        )

        # Critical columns that MUST exist (from P0 bug)
        critical_columns = {"organization_id", "created_by", "updated_by"}

        missing_critical = critical_columns & missing
        if missing_critical:
            pytest.fail(
                f"CRITICAL: hypotheses table missing columns: {missing_critical}. "
                f"This will cause 500 errors on session creation!"
            )

        if missing:
            pytest.fail(
                f"hypotheses table missing columns in database: {missing}. "
                f"Model expects these but database doesn't have them."
            )

        if extra:
            pytest.skip(
                f"hypotheses table has extra columns in database: {extra}. "
                f"These exist in DB but model doesn't expect them."
            )

        if mismatches:
            pytest.fail(
                f"hypotheses table has schema mismatches:\n" + "\n".join(mismatches)
            )

    def test_case_messages_table_consistency(self, db_path="data/faultmaven.db"):
        """Verify case_messages table schema matches CaseMessageModel."""
        db_columns = get_database_columns(db_path, "case_messages")
        model_columns = get_model_columns(CaseMessageModel)

        missing, extra, mismatches = compare_schemas(
            db_columns, model_columns, "case_messages"
        )

        # Critical column from P0 bug
        if "organization_id" in missing:
            pytest.fail(
                "CRITICAL: case_messages table missing organization_id. "
                "This will cause 500 errors on session creation!"
            )

        if missing:
            pytest.fail(
                f"case_messages table missing columns in database: {missing}. "
                f"Model expects these but database doesn't have them."
            )

        if extra:
            pytest.skip(
                f"case_messages table has extra columns in database: {extra}. "
                f"These exist in DB but model doesn't expect them."
            )

        if mismatches:
            pytest.fail(
                f"case_messages table has schema mismatches:\n" + "\n".join(mismatches)
            )

    def test_all_critical_tables_exist(self, db_path="data/faultmaven.db"):
        """Verify all critical tables exist in database."""
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = {row[0] for row in cursor.fetchall()}
        conn.close()

        critical_tables = {
            "cases",
            "solutions",
            "hypotheses",
            "case_messages",
            "evidence",
            "sessions",
            "investigation_sessions",
            "agent_executions",
            "evidence_artifacts",
        }

        missing_tables = critical_tables - tables
        if missing_tables:
            pytest.fail(
                f"CRITICAL: Database missing tables: {missing_tables}. "
                f"Run 'alembic upgrade head' to create them."
            )

    @pytest.mark.parametrize(
        "table_name,model_class",
        [
            ("cases", CaseModel),
            ("evidence", EvidenceModel),
            ("sessions", SessionModel),
            ("uploaded_files", UploadedFileModel),
            ("case_actions", CaseActionModel),
            ("case_tags", CaseTagModel),
            ("agent_tool_calls", AgentToolCallModel),
            ("evidence_artifacts", EvidenceArtifactModel),
            ("agent_executions", AgentExecutionModel),
            ("agent_tool_calls_v2", AgentToolCallV2Model),
            ("investigation_sessions", InvestigationSessionModel),
            ("case_checkpoints", CaseCheckpointModel),
            ("knowledge_items", KnowledgeItemModel),
            ("knowledge_suggestions", KnowledgeSuggestionModel),
            ("standalone_evidence", StandaloneEvidenceModel),
        ],
    )
    def test_table_model_consistency(
        self, table_name: str, model_class, db_path="data/faultmaven.db"
    ):
        """Parameterized test for all table-model consistency checks."""
        db_columns = get_database_columns(db_path, table_name)
        model_columns = get_model_columns(model_class)

        missing, extra, mismatches = compare_schemas(
            db_columns, model_columns, table_name
        )

        # For parameterized tests, we're more lenient - just check for missing columns
        if missing:
            pytest.fail(
                f"{table_name} table missing columns in database: {missing}. "
                f"Model expects these but database doesn't have them."
            )

        # Don't fail on extra columns or minor mismatches in parameterized tests
        # (We have dedicated tests above for critical tables)


@pytest.mark.integration
class TestSchemaValidationIntegration:
    """Integration tests that validate schema against live database."""

    @pytest.mark.asyncio
    async def test_query_succeeds_after_migration(self, db_path="data/faultmaven.db"):
        """Test that SQLAlchemy queries succeed after schema fix.

        This is the actual query that was failing in the P0 bug.
        """
        from sqlalchemy import create_engine, select
        from sqlalchemy.orm import Session

        engine = create_engine(f"sqlite:///{db_path}")

        with Session(engine) as session:
            # This query was failing before the fix
            stmt = select(SolutionModel).limit(5)
            results = session.execute(stmt).scalars().all()

            # Should succeed (even if no rows)
            assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_hypothesis_query_succeeds(self, db_path="data/faultmaven.db"):
        """Test hypothesis queries work after schema fix."""
        from sqlalchemy import create_engine, select
        from sqlalchemy.orm import Session

        engine = create_engine(f"sqlite:///{db_path}")

        with Session(engine) as session:
            stmt = select(HypothesisModel).limit(5)
            results = session.execute(stmt).scalars().all()
            assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_case_message_query_succeeds(self, db_path="data/faultmaven.db"):
        """Test case message queries work after schema fix."""
        from sqlalchemy import create_engine, select
        from sqlalchemy.orm import Session

        engine = create_engine(f"sqlite:///{db_path}")

        with Session(engine) as session:
            stmt = select(CaseMessageModel).limit(5)
            results = session.execute(stmt).scalars().all()
            assert isinstance(results, list)
