"""Database schema validation health check.

This module provides runtime validation that the database schema matches
the SQLAlchemy ORM model definitions. It prevents production incidents caused
by model-schema mismatches.

Use Case:
- Startup validation: Verify schema consistency before serving requests
- Health endpoint: Expose schema health via /health/schema endpoint
- CI/CD validation: Fail deployments if schema drift detected

Design:
- Lightweight: Only inspects schema metadata, no data access
- Fast: Caches results for configured TTL
- Non-blocking: Logs warnings but doesn't crash the application

Reference:
- Root cause: Migration 002 removed columns but models.py wasn't updated
- Fix: Migration 003 re-added columns to match models
- Prevention: This validator catches future mismatches at runtime
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set, Tuple

from sqlalchemy import inspect
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from faultmaven.infrastructure.persistence.models import (
    Base,
    CaseActionModel,
    CaseCheckpointModel,
    CaseMessageModel,
    CaseModel,
    CaseTagModel,
    EvidenceModel,
    HypothesisModel,
    InvestigationSessionModel,
    KnowledgeItemModel,
    KnowledgeSuggestionModel,
    SolutionModel,
    UploadedFileModel,
)

# SessionModel was removed in storage redesign 2026-04 phase 3 (the SQL
# `sessions` table was dropped). Auth sessions are Redis-only.

logger = logging.getLogger(__name__)


class SchemaValidationResult:
    """Result of schema validation check.

    Attributes:
        is_valid: True if all tables pass validation
        errors: List of error messages for failed validations
        warnings: List of warning messages (non-critical issues)
        checked_at: Timestamp of validation
        tables_checked: Number of tables validated
    """

    def __init__(
        self,
        is_valid: bool,
        errors: List[str],
        warnings: List[str],
        tables_checked: int,
    ):
        self.is_valid = is_valid
        self.errors = errors
        self.warnings = warnings
        self.checked_at = datetime.now(timezone.utc)
        self.tables_checked = tables_checked

    def to_dict(self) -> Dict:
        """Convert to dictionary for health check response."""
        return {
            "is_valid": self.is_valid,
            "status": "healthy" if self.is_valid else "unhealthy",
            "errors": self.errors,
            "warnings": self.warnings,
            "checked_at": self.checked_at.isoformat(),
            "tables_checked": self.tables_checked,
        }


class SchemaValidator:
    """Validates database schema matches SQLAlchemy models.

    This validator inspects the actual database schema and compares it
    with the expected schema defined in SQLAlchemy models.

    Usage:
        validator = SchemaValidator(engine)
        result = validator.validate()
        if not result.is_valid:
            logger.error(f"Schema validation failed: {result.errors}")
    """

    # Critical tables that MUST be validated (from P0 incident)
    CRITICAL_TABLES = {
        "solutions": SolutionModel,
        "hypotheses": HypothesisModel,
        "case_messages": CaseMessageModel,
        "evidence": EvidenceModel,
        "uploaded_files": UploadedFileModel,
        "case_actions": CaseActionModel,
    }

    # All tables to validate (comprehensive)
    # evidence_artifacts + standalone_evidence removed in storage redesign
    # 2026-04 phase 2 (standalone evidence path deletion).
    # `sessions` removed in storage redesign 2026-04 phase 3 (auth sessions
    # are Redis-only via RedisSessionStore).
    ALL_TABLES = {
        "cases": CaseModel,
        "solutions": SolutionModel,
        "hypotheses": HypothesisModel,
        "case_messages": CaseMessageModel,
        "evidence": EvidenceModel,
        "uploaded_files": UploadedFileModel,
        "case_actions": CaseActionModel,
        "case_tags": CaseTagModel,
        "investigation_sessions": InvestigationSessionModel,
        "case_checkpoints": CaseCheckpointModel,
        "knowledge_items": KnowledgeItemModel,
        "knowledge_suggestions": KnowledgeSuggestionModel,
    }

    # Critical columns that caused the P0 incident
    CRITICAL_COLUMNS = {
        "solutions": {"organization_id", "created_by", "updated_by"},
        "hypotheses": {"organization_id", "created_by", "updated_by"},
        "case_messages": {"organization_id"},
        "evidence": {"organization_id"},
        "uploaded_files": {"organization_id"},
        "case_actions": {"organization_id"},
    }

    def __init__(
        self, engine: Engine, cache_ttl_seconds: int = 60, critical_only: bool = False
    ):
        """Initialize schema validator.

        Args:
            engine: SQLAlchemy engine
            cache_ttl_seconds: Cache validation results for this many seconds
            critical_only: Only validate critical tables (faster, for health checks)
        """
        self.engine = engine
        self.cache_ttl_seconds = cache_ttl_seconds
        self.critical_only = critical_only

        self._cached_result: Optional[SchemaValidationResult] = None
        self._cache_expires_at: Optional[datetime] = None

    def validate(self, force: bool = False) -> SchemaValidationResult:
        """Validate database schema matches SQLAlchemy models.

        Args:
            force: Force validation even if cached result is still valid

        Returns:
            SchemaValidationResult with validation status and details
        """
        # Check cache
        if not force and self._is_cache_valid():
            logger.debug("Returning cached schema validation result")
            return self._cached_result

        logger.info("Running database schema validation")

        errors = []
        warnings = []
        tables_to_check = (
            self.CRITICAL_TABLES if self.critical_only else self.ALL_TABLES
        )

        with Session(self.engine) as session:
            inspector = inspect(self.engine)
            existing_tables = set(inspector.get_table_names())

            for table_name, model_class in tables_to_check.items():
                # Check table exists
                if table_name not in existing_tables:
                    errors.append(
                        f"Table '{table_name}' does not exist in database. "
                        f"Run 'alembic upgrade head' to create it."
                    )
                    continue

                # Get expected columns from model
                model_mapper = inspect(model_class)
                expected_columns = {col.name for col in model_mapper.columns}

                # Get actual columns from database
                actual_columns_info = inspector.get_columns(table_name)
                actual_columns = {col["name"] for col in actual_columns_info}

                # Check for missing columns
                missing_columns = expected_columns - actual_columns
                if missing_columns:
                    # Check if any are critical columns
                    if (
                        table_name in self.CRITICAL_COLUMNS
                        and missing_columns & self.CRITICAL_COLUMNS[table_name]
                    ):
                        errors.append(
                            f"CRITICAL: Table '{table_name}' missing columns: {missing_columns}. "
                            f"This will cause 500 errors! Run migration 003 to fix."
                        )
                    else:
                        errors.append(
                            f"Table '{table_name}' missing columns: {missing_columns}. "
                            f"Model expects these but database doesn't have them."
                        )

                # Check for extra columns (warning, not error)
                extra_columns = actual_columns - expected_columns
                if extra_columns:
                    warnings.append(
                        f"Table '{table_name}' has extra columns in database: {extra_columns}. "
                        f"These exist in DB but model doesn't expect them (may be deprecated)."
                    )

        # Create result
        is_valid = len(errors) == 0
        result = SchemaValidationResult(
            is_valid=is_valid,
            errors=errors,
            warnings=warnings,
            tables_checked=len(tables_to_check),
        )

        # Cache result
        self._cached_result = result
        self._cache_expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=self.cache_ttl_seconds
        )

        # Log results
        if is_valid:
            logger.info(
                f"Schema validation PASSED: {result.tables_checked} tables checked"
            )
            if warnings:
                logger.warning(f"Schema validation warnings: {warnings}")
        else:
            logger.error(
                f"Schema validation FAILED: {len(errors)} errors in {result.tables_checked} tables"
            )
            for error in errors:
                logger.error(f"  - {error}")

        return result

    def _is_cache_valid(self) -> bool:
        """Check if cached result is still valid."""
        if self._cached_result is None or self._cache_expires_at is None:
            return False
        return datetime.now(timezone.utc) < self._cache_expires_at

    def validate_critical_columns(self) -> Tuple[bool, List[str]]:
        """Fast validation of only critical columns from P0 incident.

        Returns:
            Tuple of (is_valid, errors)
        """
        errors = []

        with Session(self.engine) as session:
            inspector = inspect(self.engine)

            for table_name, critical_cols in self.CRITICAL_COLUMNS.items():
                # Check table exists
                try:
                    actual_columns_info = inspector.get_columns(table_name)
                    actual_columns = {col["name"] for col in actual_columns_info}

                    missing = critical_cols - actual_columns
                    if missing:
                        errors.append(
                            f"CRITICAL: {table_name} missing columns: {missing}"
                        )
                except Exception as e:
                    errors.append(f"Failed to inspect {table_name}: {e}")

        return len(errors) == 0, errors


def validate_schema_at_startup(
    engine: Engine, fail_on_error: bool = False
) -> SchemaValidationResult:
    """Validate schema at application startup.

    Args:
        engine: SQLAlchemy engine
        fail_on_error: If True, raise exception on validation failure

    Returns:
        SchemaValidationResult

    Raises:
        RuntimeError: If validation fails and fail_on_error=True
    """
    validator = SchemaValidator(engine, critical_only=False)
    result = validator.validate()

    if not result.is_valid:
        error_msg = (
            f"Database schema validation failed with {len(result.errors)} errors:\n"
            + "\n".join(f"  - {e}" for e in result.errors)
        )

        if fail_on_error:
            raise RuntimeError(error_msg)
        else:
            logger.error(error_msg)
            logger.error(
                "Application starting anyway, but some features may not work correctly!"
            )

    return result
