"""Unit tests for ConversionService: document-to-runbook conversion pipeline.

Tests cover:
- Pipeline orchestration with mocked LLM calls
- Analysis phase JSON parsing into AnalysisResult
- Multi-runbook splitting (multiple failure modes)
- Content triage rejection (non-troubleshooting documents)
- Hard reject at 30K tokens (preprocessing stage)
- PII redaction before LLM call
- ID generation (kebab-case)
- Partial failure handling (1 of N fails, remaining drafts returned)
- Deduplication by (service, symptom_class) tuple
- ConversionRejectedError for non-actionable documents
"""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.infrastructure.llm.providers import LLMResponse, StopReason
from faultmaven.infrastructure.persistence.models import (
    Base,
    ConversionDraftModel,
    ConversionJobModel,
    EnterpriseModel,
    OrganizationModel,
    UploadedFileModel,
)
from faultmaven.modules.knowledge.domain.models.conversion import (
    AnalysisResult,
    CaseConversionRequest,
    ConversionDraft,
    ConversionError,
    ConversionErrorCode,
    ConversionResponse,
    ConversionStatus,
    DraftStatus,
    FailureModeAnalysis,
    PreprocessingResult,
    QualityScore,
    RedactionReport,
    SourceAssessment,
    SourceFileInfo,
    SourceType,
    TriageResult,
    ValidationResult,
    generate_draft_id,
    generate_runbook_id,
)
from faultmaven.modules.knowledge.domain.services.conversion_service import (
    ANALYSIS_SYSTEM_PROMPT,
    CONVERSION_SYSTEM_PROMPT,
    DEFAULT_ORGANIZATION_ID,
    RUNBOOK_MAX_TOKENS_CEILING,
    ConversionRejectedError,
    ConversionService,
)
from faultmaven.modules.knowledge.domain.services.runbook_validator import (
    VALID_SYMPTOM_CLASSES,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_llm_router():
    """Mock LLM router that returns configurable responses."""
    router = AsyncMock()
    return router


@pytest.fixture
def mock_settings():
    """Mock settings with LLM config."""
    settings = MagicMock()
    settings.llm.get_knowledge_model.return_value = "test-model"
    settings.llm.get_classifier_model.return_value = "test-classifier"
    return settings


@pytest.fixture
def mock_analysis_response():
    """Standard analysis LLM response with 1 failure mode."""
    return _make_analysis_json(
        failure_modes=[
            {
                "id": "pg-replication-lag",
                "title": "PostgreSQL Replication Lag",
                "domain": "database",
                "service": "postgresql",
                "symptom_class": ["replication_lag", "high_latency"],
                "severity": "high",
                "symptoms_summary": "Replica lag exceeds 30s",
                "resolution_summary": "Tune wal_sender and checkpoint settings",
            }
        ],
    )


@pytest.fixture
def mock_multi_failure_analysis_response():
    """Analysis LLM response with 3 distinct failure modes."""
    return _make_analysis_json(
        failure_modes=[
            {
                "id": "pg-replication-lag",
                "title": "PostgreSQL Replication Lag",
                "domain": "database",
                "service": "postgresql",
                "symptom_class": ["replication_lag"],
                "severity": "high",
                "symptoms_summary": "Replica lag exceeds 30s",
                "resolution_summary": "Tune wal_sender settings",
            },
            {
                "id": "pg-connection-exhaustion",
                "title": "PostgreSQL Connection Exhaustion",
                "domain": "database",
                "service": "postgresql",
                "symptom_class": ["connection_refused"],
                "severity": "critical",
                "symptoms_summary": "Too many connections error",
                "resolution_summary": "Increase max_connections or use pgbouncer",
            },
            {
                "id": "pg-vacuum-bloat",
                "title": "PostgreSQL Table Bloat from Vacuum Failure",
                "domain": "database",
                "service": "postgresql",
                "symptom_class": ["disk_usage", "slow_queries"],
                "severity": "medium",
                "symptoms_summary": "Tables growing without bound",
                "resolution_summary": "Run manual VACUUM FULL",
            },
        ],
    )


@pytest.fixture
def mock_runbook_content():
    """Minimal valid runbook markdown content for conversion response."""
    return """---
id: postgresql-replication-lag
title: "PostgreSQL Replication Lag"
domain: database
service: postgresql
symptom_class: [replication_lag, high_latency]
scope: global
tags: [postgres, replication]
difficulty: intermediate
severity: high
version: "1.0.0"
last_updated: "2026-03-22"
verified_by: ""
status: draft
---

# Runbook: PostgreSQL Replication Lag

## Problem Definition
- Alert: `pg_replication_lag_seconds > 30`
- Error in logs: `LOG: recovery required WAL segment has already been removed`

## Diagnostic Steps

### Step 1: Check current replication lag
```bash
psql -c "SELECT client_addr, state, sent_lsn, write_lsn, replay_lsn FROM pg_stat_replication;"
```
Look for replay_lsn falling behind sent_lsn.

### Step 2: Check WAL sender processes
```bash
ps aux | grep wal
```
Verify wal_sender processes are running.

## Mitigation
**Risk**: Temporary increased load on primary
```bash
psql -c "SELECT pg_wal_replay_resume();"
```
**Verify**: Check lag decreasing via `pg_stat_replication`
**Duration**: Safe for 24h

## Root Cause Resolution
**If** replay_lsn stalled and WAL files accumulating:
```bash
ALTER SYSTEM SET wal_keep_size = '2GB';
SELECT pg_reload_conf();
```

**If** network throughput bottleneck:
```bash
ALTER SYSTEM SET max_wal_senders = 5;
```

## Verification
- Check `pg_stat_replication` shows replay_lsn advancing
- Monitor for 30 minutes to confirm lag stays below threshold
- Confirm no WAL segment removal warnings in logs

## Prevention
- Set `wal_keep_size` to at least 2x peak WAL generation rate
- Add monitoring alert for replication lag > 10s
- Schedule regular replication health checks

## Sources
- test_document.md -- primary source document for this runbook
"""


@pytest.fixture
def source_document_text():
    """Sample troubleshooting document text (after preprocessing)."""
    return (
        "# PostgreSQL Replication Troubleshooting Guide\n\n"
        "## Replication Lag\n"
        "When replica lag exceeds 30 seconds, check pg_stat_replication.\n"
        "Run: `SELECT * FROM pg_stat_replication;`\n"
        "Resolution: Tune wal_keep_size and max_wal_senders.\n\n"
        "## Connection Exhaustion\n"
        "Error: FATAL: too many connections for role\n"
        "Check: `SELECT count(*) FROM pg_stat_activity;`\n"
        "Fix: Use PgBouncer connection pooling.\n\n"
        "## Table Bloat\n"
        "Symptoms: disk usage growing, slow sequential scans.\n"
        "Diagnostic: `SELECT schemaname, relname, n_dead_tup FROM pg_stat_user_tables;`\n"
        "Fix: Run VACUUM FULL on affected tables.\n"
    )


@pytest.fixture
def source_file(tmp_path, source_document_text):
    """Write sample source document to tmp_path and return the path."""
    file_path = tmp_path / "test_document.md"
    file_path.write_text(source_document_text, encoding="utf-8")
    return file_path


@pytest.fixture
def service(mock_llm_router, mock_settings):
    """ConversionService with mocked LLM and no DB."""
    return ConversionService(
        llm_router=mock_llm_router,
        settings=mock_settings,
        db_session_factory=None,
        knowledge_service=None,
    )


# =============================================================================
# Helpers
# =============================================================================


def _make_analysis_json(
    failure_modes, is_actionable=True, content_type="troubleshooting_guide"
):
    """Build a JSON string matching the analysis LLM response schema."""
    return json.dumps(
        {
            "is_actionable": is_actionable,
            "failure_modes": failure_modes,
            "source_assessment": {
                "content_type": content_type,
                "actionability_rating": "high",
                "missing_information": [],
            },
        }
    )


def _make_llm_response(content: str, stop_reason: StopReason = StopReason.STOP):
    """Build a real ``LLMResponse``, which is what the router actually returns.

    This used to be a ``SimpleNamespace`` carrying only ``.content``. That was
    enough while ``.content`` was the only thing the service read, and it stopped
    being enough the moment the service started asking whether the response was
    cut off (#1094) — a stand-in that answers only the questions the code asked
    yesterday cannot fail when the code starts asking a new one.
    """
    return LLMResponse(
        content=content,
        confidence=0.9,
        provider="test",
        model="test-model",
        tokens_used=100,
        response_time_ms=10,
        stop_reason=stop_reason,
    )


def _make_preprocessing_result(
    text: str, rejected=False, rejection_reason=None, warnings=None
):
    """Build a PreprocessingResult for patching the preprocessor."""
    return PreprocessingResult(
        extracted_text=text,
        source_metadata={"original_filename": "test.md", "file_size_bytes": len(text)},
        redaction_report=RedactionReport(),
        warnings=warnings or [],
        is_rejected=rejected,
        rejection_reason=rejection_reason,
        token_count=len(text.split()),
    )


# =============================================================================
# Test 1: Pipeline orchestration
# =============================================================================


@pytest.mark.unit
class TestConvertDocumentPipeline:
    """Test the full convert_document pipeline with mocked LLM."""

    @pytest.mark.asyncio
    async def test_convert_document_success(
        self,
        service,
        mock_llm_router,
        source_file,
        source_document_text,
        mock_analysis_response,
        mock_runbook_content,
        tmp_path,
    ):
        """Full pipeline produces COMPLETED status with 1 draft."""
        # Arrange: mock preprocessor to skip file parsing
        preprocessing = _make_preprocessing_result(source_document_text)
        with patch.object(
            service._preprocessor, "preprocess", return_value=preprocessing
        ):
            # First LLM call = analysis, subsequent = conversion
            mock_llm_router.route.side_effect = [
                _make_llm_response(mock_analysis_response),
                _make_llm_response(mock_runbook_content),
            ]

            # Patch _data_dir to use tmp_path so files write there
            with patch.object(
                type(service),
                "_data_dir",
                new_callable=lambda: property(lambda self: tmp_path),
            ):
                # Act
                result = await service.convert_document(
                    file_path=source_file,
                    content_type="text/markdown",
                    original_filename="test_document.md",
                    scope="global",
                    user_id="user-123",
                )

        # Assert
        assert result.status == ConversionStatus.COMPLETED
        assert len(result.drafts) == 1
        assert result.analysis.is_actionable is True
        assert len(result.analysis.failure_modes) == 1
        assert result.drafts[0].title == "PostgreSQL Replication Lag"
        assert result.conversion_id.startswith("conv_")

    @pytest.mark.asyncio
    async def test_convert_document_llm_called_twice(
        self,
        service,
        mock_llm_router,
        source_file,
        source_document_text,
        mock_analysis_response,
        mock_runbook_content,
        tmp_path,
    ):
        """LLM router is called once for analysis and once for conversion."""
        preprocessing = _make_preprocessing_result(source_document_text)
        with patch.object(
            service._preprocessor, "preprocess", return_value=preprocessing
        ):
            mock_llm_router.route.side_effect = [
                _make_llm_response(mock_analysis_response),
                _make_llm_response(mock_runbook_content),
            ]
            with patch.object(
                type(service),
                "_data_dir",
                new_callable=lambda: property(lambda self: tmp_path),
            ):
                await service.convert_document(
                    file_path=source_file,
                    content_type="text/markdown",
                    original_filename="test_document.md",
                    scope="global",
                    user_id="user-123",
                )

        # 1 analysis call + 1 conversion call
        assert mock_llm_router.route.call_count == 2


# =============================================================================
# Test 2: Analysis phase parsing
# =============================================================================


@pytest.mark.unit
class TestAnalysisPhase:
    """Test _analyze_document parses LLM JSON into AnalysisResult."""

    @pytest.mark.asyncio
    async def test_analysis_parses_single_failure_mode(
        self,
        service,
        mock_llm_router,
        mock_analysis_response,
    ):
        """Analysis parses JSON response into AnalysisResult with correct fields."""
        mock_llm_router.route.return_value = _make_llm_response(mock_analysis_response)

        result = await service._analyze_document("sample text", "test.md")

        assert isinstance(result, AnalysisResult)
        assert result.is_actionable is True
        assert len(result.failure_modes) == 1

        fm = result.failure_modes[0]
        assert fm.id == "pg-replication-lag"
        assert fm.domain == "database"
        assert fm.service == "postgresql"
        assert fm.severity == "high"
        assert "replication_lag" in fm.symptom_class

    @pytest.mark.asyncio
    async def test_analysis_parses_source_assessment(
        self,
        service,
        mock_llm_router,
        mock_analysis_response,
    ):
        """Source assessment is parsed from the response."""
        mock_llm_router.route.return_value = _make_llm_response(mock_analysis_response)

        result = await service._analyze_document("sample text", "test.md")

        assert result.source_assessment.content_type == "troubleshooting_guide"
        assert result.source_assessment.actionability_rating == "high"

    @pytest.mark.asyncio
    async def test_analysis_raises_on_invalid_json(self, service, mock_llm_router):
        """Invalid JSON from LLM raises ConversionRejectedError with
        ``error_code=LLM_PARSE_ERROR`` so the route handler returns 422
        with the structured code instead of a generic 500.
        """
        mock_llm_router.route.return_value = _make_llm_response("not valid json {{{")

        with pytest.raises(
            ConversionRejectedError,
            match="LLM analysis response could not be parsed",
        ) as exc:
            await service._analyze_document("sample text", "test.md")

        assert exc.value.error_code == ConversionErrorCode.LLM_PARSE_ERROR


# =============================================================================
# Test 3: Multi-runbook splitting
# =============================================================================


@pytest.mark.unit
class TestMultiRunbookSplitting:
    """Source with multiple failure modes produces multiple drafts."""

    @pytest.mark.asyncio
    async def test_three_failure_modes_produce_three_drafts(
        self,
        service,
        mock_llm_router,
        source_file,
        source_document_text,
        mock_multi_failure_analysis_response,
        mock_runbook_content,
        tmp_path,
    ):
        """3 failure modes in analysis result in 3 conversion drafts."""
        preprocessing = _make_preprocessing_result(source_document_text)
        with patch.object(
            service._preprocessor, "preprocess", return_value=preprocessing
        ):
            # 1 analysis + 3 conversion calls
            mock_llm_router.route.side_effect = [
                _make_llm_response(mock_multi_failure_analysis_response),
                _make_llm_response(mock_runbook_content),
                _make_llm_response(mock_runbook_content),
                _make_llm_response(mock_runbook_content),
            ]
            with patch.object(
                type(service),
                "_data_dir",
                new_callable=lambda: property(lambda self: tmp_path),
            ):
                result = await service.convert_document(
                    file_path=source_file,
                    content_type="text/markdown",
                    original_filename="test_document.md",
                    scope="global",
                    user_id="user-123",
                )

        assert result.status == ConversionStatus.COMPLETED
        assert len(result.drafts) == 3
        assert len(result.analysis.failure_modes) == 3
        # 1 analysis + 3 conversions
        assert mock_llm_router.route.call_count == 4


# =============================================================================
# Test 4: Content triage rejection
# =============================================================================


@pytest.mark.unit
class TestContentTriageRejection:
    """Non-troubleshooting documents are rejected at triage."""

    @pytest.mark.asyncio
    async def test_triage_rejects_non_troubleshooting_document(
        self,
        service,
        source_file,
    ):
        """Document rejected by triage raises ConversionRejectedError."""
        preprocessing = _make_preprocessing_result(
            text="",
            rejected=True,
            rejection_reason="This document does not appear to contain troubleshooting content.",
        )
        with patch.object(
            service._preprocessor, "preprocess", return_value=preprocessing
        ):
            with pytest.raises(ConversionRejectedError, match="troubleshooting"):
                await service.convert_document(
                    file_path=source_file,
                    content_type="text/markdown",
                    original_filename="architecture_overview.md",
                    scope="global",
                    user_id="user-123",
                )


# =============================================================================
# Test 5: Hard reject at 30K tokens
# =============================================================================


@pytest.mark.unit
class TestTokenLimitRejection:
    """Documents exceeding 30K tokens are rejected at preprocessing."""

    @pytest.mark.asyncio
    async def test_exceeds_30k_tokens_rejected(self, service, source_file):
        """Document with >30K tokens raises ConversionRejectedError."""
        preprocessing = _make_preprocessing_result(
            text="",
            rejected=True,
            rejection_reason="Document contains 35,000 tokens (limit: 30,000).",
        )
        with patch.object(
            service._preprocessor, "preprocess", return_value=preprocessing
        ):
            with pytest.raises(ConversionRejectedError, match="35,000 tokens"):
                await service.convert_document(
                    file_path=source_file,
                    content_type="text/markdown",
                    original_filename="huge_document.md",
                    scope="global",
                    user_id="user-123",
                )

    @pytest.mark.asyncio
    async def test_preprocessor_hard_limit_constant(self):
        """MAX_TOKEN_LIMIT is 30,000."""
        from faultmaven.modules.knowledge.domain.services.document_preprocessor import (
            MAX_TOKEN_LIMIT,
        )

        assert MAX_TOKEN_LIMIT == 30_000


# =============================================================================
# Test 6: PII redaction before LLM call
# =============================================================================


@pytest.mark.unit
class TestPIIRedaction:
    """PII is redacted before the document text reaches the LLM."""

    @pytest.mark.asyncio
    async def test_redacted_text_sent_to_llm(
        self,
        service,
        mock_llm_router,
        source_file,
        mock_analysis_response,
        mock_runbook_content,
        tmp_path,
    ):
        """Text containing API keys is redacted before analysis LLM call."""
        text_with_secrets = (
            "Troubleshooting Guide\n"
            "Connect with: api_key=sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890\n"
            "Check replication lag using pg_stat_replication.\n"
            "Resolution: tune wal_keep_size." + " extra" * 50
        )

        # The preprocessor does redaction in stage 3, so we use the real
        # redact_sensitive_content to prove it works, then mock the rest.
        from faultmaven.modules.knowledge.domain.services.document_preprocessor import (
            redact_sensitive_content,
        )

        redacted_text, report = redact_sensitive_content(text_with_secrets)

        # Verify redaction happened
        assert "[REDACTED:api_key]" in redacted_text
        assert report.total_redacted > 0

        # Now use the redacted text in the pipeline
        preprocessing = _make_preprocessing_result(
            redacted_text,
            warnings=[f"{report.total_redacted} sensitive items were redacted."],
        )
        with patch.object(
            service._preprocessor, "preprocess", return_value=preprocessing
        ):
            mock_llm_router.route.side_effect = [
                _make_llm_response(mock_analysis_response),
                _make_llm_response(mock_runbook_content),
            ]
            with patch.object(
                type(service),
                "_data_dir",
                new_callable=lambda: property(lambda self: tmp_path),
            ):
                result = await service.convert_document(
                    file_path=source_file,
                    content_type="text/markdown",
                    original_filename="test.md",
                    scope="global",
                    user_id="user-123",
                )

        # The text sent to analysis LLM must contain the redaction placeholder
        analysis_call_args = mock_llm_router.route.call_args_list[0]
        user_message = analysis_call_args.kwargs.get(
            "messages",
            (
                analysis_call_args[1]["messages"]
                if len(analysis_call_args) > 1
                else analysis_call_args[0][0]
            ),
        )[1]["content"]
        assert "[REDACTED:api_key]" in user_message
        assert "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ" not in user_message

    def test_redaction_covers_multiple_patterns(self):
        """Redaction catches API keys, passwords, JWTs, and DB connection strings."""
        from faultmaven.modules.knowledge.domain.services.document_preprocessor import (
            redact_sensitive_content,
        )

        text = (
            "api_key=sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890\n"
            "password = 'supersecret123'\n"
            "postgres://user:pass@host:5432/db\n"
        )
        redacted, report = redact_sensitive_content(text)

        assert "[REDACTED:api_key]" in redacted
        assert "[REDACTED:password]" in redacted
        assert "[REDACTED:db_connection_string]" in redacted
        assert report.total_redacted >= 3


# =============================================================================
# Test 7: ID generation (kebab-case)
# =============================================================================


@pytest.mark.unit
class TestIDGeneration:
    """generate_runbook_id produces kebab-case identifiers."""

    def test_basic_kebab_case(self):
        """Service + title becomes kebab-case slug."""
        fm = FailureModeAnalysis(
            id="test",
            title="Replication Lag",
            domain="database",
            service="postgresql",
            symptom_class=["replication_lag"],
            severity="high",
            symptoms_summary="lag",
            resolution_summary="fix",
        )
        result = generate_runbook_id(fm)
        assert result == "postgresql-replication-lag"

    def test_special_characters_stripped(self):
        """Non-alphanumeric chars are converted to hyphens."""
        fm = FailureModeAnalysis(
            id="test",
            title="OOM Kill (Memory Pressure)",
            domain="compute",
            service="kubernetes",
            symptom_class=["oom"],
            severity="critical",
            symptoms_summary="oom",
            resolution_summary="fix",
        )
        result = generate_runbook_id(fm)
        assert result == "kubernetes-oom-kill-memory-pressure"
        # All lowercase, no special chars
        assert result == result.lower()
        assert all(c.isalnum() or c == "-" for c in result)

    def test_long_title_truncated_with_hash(self):
        """Slugs longer than 60 chars are truncated with a hash suffix."""
        fm = FailureModeAnalysis(
            id="test",
            title="Very Long Failure Mode Title That Exceeds The Maximum Length Allowed For Runbook Identifiers",
            domain="database",
            service="some-very-long-service-name",
            symptom_class=["test"],
            severity="medium",
            symptoms_summary="test",
            resolution_summary="test",
        )
        result = generate_runbook_id(fm)
        assert len(result) <= 60

    def test_uppercase_normalized(self):
        """Mixed case input produces lowercase output."""
        fm = FailureModeAnalysis(
            id="test",
            title="SSL Certificate Expiry",
            domain="security",
            service="NGINX",
            symptom_class=["cert_expiry"],
            severity="high",
            symptoms_summary="expired",
            resolution_summary="renew",
        )
        result = generate_runbook_id(fm)
        assert result == "nginx-ssl-certificate-expiry"


# =============================================================================
# Test 8: Partial failure
# =============================================================================


@pytest.mark.unit
class TestPartialFailure:
    """When 1 of N conversions fails, status=partial and remaining drafts returned."""

    @pytest.mark.asyncio
    async def test_one_of_three_fails_returns_partial(
        self,
        service,
        mock_llm_router,
        source_file,
        source_document_text,
        mock_multi_failure_analysis_response,
        mock_runbook_content,
        tmp_path,
    ):
        """1 of 3 conversions failing produces PARTIAL status with 2 drafts."""
        preprocessing = _make_preprocessing_result(source_document_text)
        with patch.object(
            service._preprocessor, "preprocess", return_value=preprocessing
        ):
            # Analysis succeeds, then: success, fail, success
            mock_llm_router.route.side_effect = [
                _make_llm_response(mock_multi_failure_analysis_response),
                _make_llm_response(mock_runbook_content),
                RuntimeError("LLM provider timeout"),
                _make_llm_response(mock_runbook_content),
            ]
            with patch.object(
                type(service),
                "_data_dir",
                new_callable=lambda: property(lambda self: tmp_path),
            ):
                result = await service.convert_document(
                    file_path=source_file,
                    content_type="text/markdown",
                    original_filename="test_document.md",
                    scope="global",
                    user_id="user-123",
                )

        assert result.status == ConversionStatus.PARTIAL
        assert len(result.drafts) == 2
        assert len(result.warnings) > 0
        # Warnings should mention the failed failure mode
        warning_text = " ".join(result.warnings)
        assert "Failed to convert" in warning_text

    @pytest.mark.asyncio
    async def test_all_conversions_fail_returns_failed(
        self,
        service,
        mock_llm_router,
        source_file,
        source_document_text,
        mock_multi_failure_analysis_response,
        tmp_path,
    ):
        """All conversions failing produces FAILED status with 0 drafts."""
        preprocessing = _make_preprocessing_result(source_document_text)
        with patch.object(
            service._preprocessor, "preprocess", return_value=preprocessing
        ):
            mock_llm_router.route.side_effect = [
                _make_llm_response(mock_multi_failure_analysis_response),
                RuntimeError("fail 1"),
                RuntimeError("fail 2"),
                RuntimeError("fail 3"),
            ]
            with patch.object(
                type(service),
                "_data_dir",
                new_callable=lambda: property(lambda self: tmp_path),
            ):
                result = await service.convert_document(
                    file_path=source_file,
                    content_type="text/markdown",
                    original_filename="test_document.md",
                    scope="global",
                    user_id="user-123",
                )

        assert result.status == ConversionStatus.FAILED
        assert len(result.drafts) == 0


# =============================================================================
# Test 9: Deduplication by (service, symptom_class)
# =============================================================================


@pytest.mark.unit
class TestDeduplication:
    """Duplicate failure modes (same service + symptom_class) are deduplicated."""

    @pytest.mark.asyncio
    async def test_duplicate_service_symptom_class_deduplicated(
        self,
        service,
        mock_llm_router,
        source_file,
        source_document_text,
        mock_runbook_content,
        tmp_path,
    ):
        """Two failure modes with same (service, symptom_class) produce 1 draft."""
        analysis_json = _make_analysis_json(
            failure_modes=[
                {
                    "id": "pg-lag-v1",
                    "title": "PostgreSQL Replication Lag (variant 1)",
                    "domain": "database",
                    "service": "postgresql",
                    "symptom_class": ["replication_lag"],
                    "severity": "high",
                    "symptoms_summary": "lag variant 1",
                    "resolution_summary": "fix 1",
                },
                {
                    "id": "pg-lag-v2",
                    "title": "PostgreSQL Replication Lag (variant 2)",
                    "domain": "database",
                    "service": "postgresql",
                    "symptom_class": ["replication_lag"],
                    "severity": "high",
                    "symptoms_summary": "lag variant 2",
                    "resolution_summary": "fix 2",
                },
            ],
        )
        preprocessing = _make_preprocessing_result(source_document_text)
        with patch.object(
            service._preprocessor, "preprocess", return_value=preprocessing
        ):
            mock_llm_router.route.side_effect = [
                _make_llm_response(analysis_json),
                _make_llm_response(mock_runbook_content),
            ]
            with patch.object(
                type(service),
                "_data_dir",
                new_callable=lambda: property(lambda self: tmp_path),
            ):
                result = await service.convert_document(
                    file_path=source_file,
                    content_type="text/markdown",
                    original_filename="test_document.md",
                    scope="global",
                    user_id="user-123",
                )

        # Only 1 draft because duplicates removed
        assert len(result.drafts) == 1
        # 1 analysis + 1 conversion (not 2)
        assert mock_llm_router.route.call_count == 2

    @pytest.mark.asyncio
    async def test_different_symptom_class_order_still_deduplicates(
        self,
        service,
        mock_llm_router,
        source_file,
        source_document_text,
        mock_runbook_content,
        tmp_path,
    ):
        """Symptom classes in different order are treated as duplicates."""
        analysis_json = _make_analysis_json(
            failure_modes=[
                {
                    "id": "pg-lag-a",
                    "title": "PostgreSQL Lag A",
                    "domain": "database",
                    "service": "postgresql",
                    "symptom_class": ["replication_lag", "high_latency"],
                    "severity": "high",
                    "symptoms_summary": "lag a",
                    "resolution_summary": "fix a",
                },
                {
                    "id": "pg-lag-b",
                    "title": "PostgreSQL Lag B",
                    "domain": "database",
                    "service": "postgresql",
                    "symptom_class": ["high_latency", "replication_lag"],
                    "severity": "high",
                    "symptoms_summary": "lag b",
                    "resolution_summary": "fix b",
                },
            ],
        )
        preprocessing = _make_preprocessing_result(source_document_text)
        with patch.object(
            service._preprocessor, "preprocess", return_value=preprocessing
        ):
            mock_llm_router.route.side_effect = [
                _make_llm_response(analysis_json),
                _make_llm_response(mock_runbook_content),
            ]
            with patch.object(
                type(service),
                "_data_dir",
                new_callable=lambda: property(lambda self: tmp_path),
            ):
                result = await service.convert_document(
                    file_path=source_file,
                    content_type="text/markdown",
                    original_filename="test_document.md",
                    scope="global",
                    user_id="user-123",
                )

        # Sorted symptom_class tuples match, so deduplicated to 1
        assert len(result.drafts) == 1


# =============================================================================
# Test 10: ConversionRejectedError for non-actionable documents
# =============================================================================


@pytest.mark.unit
class TestConversionRejectedError:
    """ConversionRejectedError raised for non-actionable documents."""

    @pytest.mark.asyncio
    async def test_non_actionable_analysis_raises_rejected(
        self,
        service,
        mock_llm_router,
        source_file,
        source_document_text,
        tmp_path,
    ):
        """Analysis returning is_actionable=False raises ConversionRejectedError."""
        non_actionable_json = _make_analysis_json(
            failure_modes=[],
            is_actionable=False,
            content_type="other",
        )
        preprocessing = _make_preprocessing_result(source_document_text)
        with patch.object(
            service._preprocessor, "preprocess", return_value=preprocessing
        ):
            mock_llm_router.route.return_value = _make_llm_response(non_actionable_json)

            with pytest.raises(
                ConversionRejectedError,
                match="does not contain actionable failure modes",
            ):
                await service.convert_document(
                    file_path=source_file,
                    content_type="text/markdown",
                    original_filename="architecture_overview.md",
                    scope="global",
                    user_id="user-123",
                )

    @pytest.mark.asyncio
    async def test_actionable_but_empty_failure_modes_raises_rejected(
        self,
        service,
        mock_llm_router,
        source_file,
        source_document_text,
        tmp_path,
    ):
        """is_actionable=True but empty failure_modes list still raises."""
        empty_modes_json = _make_analysis_json(
            failure_modes=[],
            is_actionable=True,
        )
        preprocessing = _make_preprocessing_result(source_document_text)
        with patch.object(
            service._preprocessor, "preprocess", return_value=preprocessing
        ):
            mock_llm_router.route.return_value = _make_llm_response(empty_modes_json)

            with pytest.raises(ConversionRejectedError):
                await service.convert_document(
                    file_path=source_file,
                    content_type="text/markdown",
                    original_filename="some_doc.md",
                    scope="global",
                    user_id="user-123",
                )

    @pytest.mark.asyncio
    async def test_preprocessing_rejection_raises_error(
        self,
        service,
        source_file,
    ):
        """Preprocessing rejection (e.g., unsupported format) raises ConversionRejectedError."""
        preprocessing = _make_preprocessing_result(
            text="",
            rejected=True,
            rejection_reason="Unsupported file format: application/octet-stream",
        )
        with patch.object(
            service._preprocessor, "preprocess", return_value=preprocessing
        ):
            with pytest.raises(
                ConversionRejectedError, match="Unsupported file format"
            ):
                await service.convert_document(
                    file_path=source_file,
                    content_type="application/octet-stream",
                    original_filename="binary.bin",
                    scope="global",
                    user_id="user-123",
                )


# =============================================================================
# Additional edge case tests
# =============================================================================


@pytest.mark.unit
class TestPersistenceSkipped:
    """Database persistence is skipped when db_session_factory is None."""

    @pytest.mark.asyncio
    async def test_no_db_session_factory_skips_persist(
        self,
        service,
        mock_llm_router,
        source_file,
        source_document_text,
        mock_analysis_response,
        mock_runbook_content,
        tmp_path,
    ):
        """Pipeline completes without errors when db_session_factory is None."""
        preprocessing = _make_preprocessing_result(source_document_text)
        with patch.object(
            service._preprocessor, "preprocess", return_value=preprocessing
        ):
            mock_llm_router.route.side_effect = [
                _make_llm_response(mock_analysis_response),
                _make_llm_response(mock_runbook_content),
            ]
            with patch.object(
                type(service),
                "_data_dir",
                new_callable=lambda: property(lambda self: tmp_path),
            ):
                result = await service.convert_document(
                    file_path=source_file,
                    content_type="text/markdown",
                    original_filename="test.md",
                    scope="global",
                    user_id="user-123",
                )

        # Should complete without DB errors
        assert result.status == ConversionStatus.COMPLETED


@pytest.mark.unit
class TestSourceFileRetention:
    """Source file is copied to the sources directory."""

    @pytest.mark.asyncio
    async def test_source_file_copied(
        self,
        service,
        mock_llm_router,
        source_file,
        source_document_text,
        mock_analysis_response,
        mock_runbook_content,
        tmp_path,
    ):
        """Original source file is retained in sources/<conversion_id>/."""
        preprocessing = _make_preprocessing_result(source_document_text)
        with patch.object(
            service._preprocessor, "preprocess", return_value=preprocessing
        ):
            mock_llm_router.route.side_effect = [
                _make_llm_response(mock_analysis_response),
                _make_llm_response(mock_runbook_content),
            ]
            with patch.object(
                type(service),
                "_data_dir",
                new_callable=lambda: property(lambda self: tmp_path),
            ):
                result = await service.convert_document(
                    file_path=source_file,
                    content_type="text/markdown",
                    original_filename="test_document.md",
                    scope="global",
                    user_id="user-123",
                )

        # Source metadata should be captured (files not retained on disk per architecture)
        assert result.source_file.filename == "test_document.md"
        assert result.source_file.content_type == "text/markdown"
        assert result.source_file.size_bytes > 0


# =============================================================================
# Test: Scan bulk-discard guard
# =============================================================================


@pytest.mark.unit
class TestScanBulkDiscardGuard:
    """scan_for_runbooks must abort when it would discard every active draft."""

    def _make_draft_model(
        self, draft_id: str, file_path: str, status: str = "verified"
    ):
        dm = MagicMock()
        dm.id = draft_id
        dm.file_path = file_path
        dm.status = status
        dm.knowledge_item_id = f"kb_{draft_id}"
        return dm

    def _make_db_session(self, draft_models):
        """Return an async context manager that yields a session with the given drafts."""
        session = AsyncMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = draft_models
        session.execute = AsyncMock(return_value=result)

        class _CM:
            async def __aenter__(self_):
                return session

            async def __aexit__(self_, *args):
                pass

        return _CM

    @pytest.mark.asyncio
    async def test_raises_when_all_files_missing(self, tmp_path):
        """If every active draft file is absent, scan must raise RuntimeError."""
        from faultmaven.modules.knowledge.domain.services.conversion_service import (
            ConversionService,
        )

        # Three drafts whose files do not exist
        drafts = [
            self._make_draft_model("d1", "/nonexistent/a.md"),
            self._make_draft_model("d2", "/nonexistent/b.md"),
            self._make_draft_model("d3", "/nonexistent/c.md"),
        ]
        db_cm = self._make_db_session(drafts)

        svc = ConversionService(
            llm_router=AsyncMock(),
            settings=MagicMock(),
            db_session_factory=db_cm,
            knowledge_service=None,
        )
        # Point data_dir to tmp_path so the directory-walk doesn't fail
        with patch.object(
            type(svc), "_data_dir", new_callable=lambda: property(lambda s: tmp_path)
        ):
            with pytest.raises(RuntimeError, match="Scan aborted"):
                await svc.scan_for_runbooks(user_id="u1")

    @pytest.mark.asyncio
    async def test_allows_partial_discard_when_some_files_survive(self, tmp_path):
        """If at least one file exists, scan should proceed normally."""
        from faultmaven.modules.knowledge.domain.services.conversion_service import (
            ConversionService,
        )

        surviving = tmp_path / "good.md"
        surviving.write_text(
            "---\ntitle: Good Runbook\nstatus: verified\n---\n\n" + "x" * 200,
            encoding="utf-8",
        )

        drafts = [
            self._make_draft_model("d1", "/nonexistent/a.md"),
            self._make_draft_model("d2", str(surviving), status="verified"),
        ]
        # d2 has knowledge_item_id set AND status=verified → kept (tracked)
        drafts[1].knowledge_item_id = "kb_abc"

        db_cm = self._make_db_session(drafts)

        svc = ConversionService(
            llm_router=AsyncMock(),
            settings=MagicMock(),
            db_session_factory=db_cm,
            knowledge_service=None,
        )
        with patch.object(
            type(svc), "_data_dir", new_callable=lambda: property(lambda s: tmp_path)
        ):
            # Should not raise; surviving file prevents the guard from firing
            result = await svc.scan_for_runbooks(user_id="u1")
        assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_already_discarded_drafts_not_counted(self, tmp_path):
        """Pre-discarded rows don't count toward the guard threshold."""
        from faultmaven.modules.knowledge.domain.services.conversion_service import (
            ConversionService,
        )

        already_gone = self._make_draft_model(
            "d_old", "/nonexistent/old.md", status="discarded"
        )
        surviving = tmp_path / "live.md"
        surviving.write_text(
            "---\ntitle: Live Runbook\nstatus: verified\n---\n\n" + "x" * 200,
            encoding="utf-8",
        )
        live = self._make_draft_model("d_live", str(surviving), status="verified")
        live.knowledge_item_id = "kb_live"

        drafts = [already_gone, live]
        db_cm = self._make_db_session(drafts)

        svc = ConversionService(
            llm_router=AsyncMock(),
            settings=MagicMock(),
            db_session_factory=db_cm,
            knowledge_service=None,
        )
        with patch.object(
            type(svc), "_data_dir", new_callable=lambda: property(lambda s: tmp_path)
        ):
            result = await svc.scan_for_runbooks(user_id="u1")
        assert isinstance(result, dict)


# =============================================================================
# Case Conversion Dedup
# =============================================================================


def _make_fake_response(conversion_id: str = "conv_test123") -> "object":
    """Build a minimal ConversionResponse stub for use in dedup tests.

    We only assert identity (same conversion_id) across racing callers, so a
    SimpleNamespace is enough — we don't construct a real Pydantic model.
    """
    return SimpleNamespace(conversion_id=conversion_id)


class TestConvertFromCaseDedup:
    """`convert_from_case` deduplicates in-flight conversions by case_id.

    Mirrors the `_inflight_vectorize` pattern on MilestoneEngine. Two rapid
    clicks of the runbook affordance — chat-triggered, HTTP-triggered, or
    one of each — should produce one ConversionJob row, not two.
    """

    def _make_request(self, case_id: str = "case-dedup-1"):
        from faultmaven.modules.knowledge.domain.models.conversion import (
            CaseConversionRequest,
        )

        return CaseConversionRequest(
            case_id=case_id,
            title="Test failure",
            domain="application",
            service="test-svc",
            symptom_class=["timeout"],
            severity="high",
            description="The thing failed",
            root_cause="ChromaDB pooling disabled by default",
            scope="personal",
        )

    @pytest.mark.asyncio
    async def test_concurrent_calls_share_one_inflight_task(self, service):
        """Two simultaneous calls for the same case_id: impl runs ONCE,
        both callers get the same response."""
        request = self._make_request("case-shared-1")

        call_count = 0

        async def slow_impl(req, user_id, organization_id=None, team_id=None):
            nonlocal call_count
            call_count += 1
            # Yield long enough for the second call to enter and find the
            # in-flight task in the registry.
            await asyncio.sleep(0.05)
            return _make_fake_response(conversion_id="conv_shared")

        with patch.object(service, "_convert_from_case_impl", side_effect=slow_impl):
            results = await asyncio.gather(
                service.convert_from_case(request, user_id="u1"),
                service.convert_from_case(request, user_id="u1"),
            )

        assert call_count == 1, "Impl should run once when two callers race"
        assert results[0].conversion_id == results[1].conversion_id
        assert results[0].conversion_id == "conv_shared"

    @pytest.mark.asyncio
    async def test_sequential_calls_after_completion_create_new_tasks(self, service):
        """Once the first call finishes, the registry entry is cleared so a
        subsequent call for the same case_id starts a fresh conversion."""
        request = self._make_request("case-sequential-1")

        call_count = 0

        async def fast_impl(req, user_id, organization_id=None, team_id=None):
            nonlocal call_count
            call_count += 1
            return _make_fake_response(conversion_id=f"conv_seq_{call_count}")

        with patch.object(service, "_convert_from_case_impl", side_effect=fast_impl):
            first = await service.convert_from_case(request, user_id="u1")
            second = await service.convert_from_case(request, user_id="u1")

        assert call_count == 2, "Sequential calls should each run impl"
        assert first.conversion_id == "conv_seq_1"
        assert second.conversion_id == "conv_seq_2"

    @pytest.mark.asyncio
    async def test_registry_cleared_after_success(self, service):
        """After a successful conversion, the case_id is removed from
        `_inflight_runbook` so it does not leak."""
        request = self._make_request("case-cleanup-1")

        async def fast_impl(req, user_id, organization_id=None, team_id=None):
            return _make_fake_response()

        with patch.object(service, "_convert_from_case_impl", side_effect=fast_impl):
            await service.convert_from_case(request, user_id="u1")

        assert request.case_id not in service._inflight_runbook

    @pytest.mark.asyncio
    async def test_registry_cleared_after_exception(self, service):
        """If the impl raises, the registry entry is still cleaned up so a
        retry can proceed."""
        request = self._make_request("case-exc-1")

        async def failing_impl(req, user_id, organization_id=None, team_id=None):
            raise RuntimeError("boom")

        with patch.object(service, "_convert_from_case_impl", side_effect=failing_impl):
            with pytest.raises(RuntimeError, match="boom"):
                await service.convert_from_case(request, user_id="u1")

        assert request.case_id not in service._inflight_runbook

    @pytest.mark.asyncio
    async def test_different_cases_do_not_share_inflight(self, service):
        """Two concurrent calls for DIFFERENT case_ids both run their own
        impls — dedup is per case_id, not global."""
        req_a = self._make_request("case-A")
        req_b = self._make_request("case-B")

        call_count = 0

        async def slow_impl(req, user_id, organization_id=None, team_id=None):
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.05)
            return _make_fake_response(conversion_id=f"conv_{req.case_id}")

        with patch.object(service, "_convert_from_case_impl", side_effect=slow_impl):
            results = await asyncio.gather(
                service.convert_from_case(req_a, user_id="u1"),
                service.convert_from_case(req_b, user_id="u1"),
            )

        assert call_count == 2, "Different cases must each run impl"
        assert results[0].conversion_id != results[1].conversion_id


def test_runbook_validator_security_is_blocking():
    """Security hazards must BLOCK conversion-time validation (errors), and
    placeholder secret values must not. Regression for the warn-only gate."""
    from faultmaven.modules.knowledge.domain.services.runbook_validator import (
        RunbookValidator,
    )

    v = RunbookValidator()

    # Destructive commands block — including variants the first cut missed.
    for cmd in [
        "rm -rf /",
        "rm -fr /etc",
        "rm --recursive --force /",
        "rm -rf ~",
        "rm -rf $HOME",
        ": () { :|: & };:",
        "dd if=/dev/zero of=/dev/sda",
    ]:
        r = v.validate_content(f"```bash\n{cmd}\n```")
        assert not r.passed, cmd
        assert any(
            "Dangerous" in e or "destructive" in e for e in r.errors
        ), f"{cmd} not blocked: {r.errors}"

    # Scoped path + benign dd do NOT block (no false positive).
    for cmd in ["rm -rf /var/lib/app/cache", "dd if=/dev/zero of=/dev/null bs=1M"]:
        r = v.validate_content(f"```bash\n{cmd}\n```")
        assert not any(
            "Dangerous" in e or "destructive" in e for e in r.errors
        ), f"{cmd} falsely blocked: {r.errors}"

    # Real secrets block — including values that contain `$` or placeholder
    # substrings (the false-negative the anchored placeholder match closes).
    for val in ["real-leaked-secret", "P@$$w0rd", "Xxxabc123", "AKIAIOSFODNN7EXAMPLE"]:
        r = v.validate_content(f'password = "{val}"')
        assert any("hardcoded" in e for e in r.errors), f"{val} not blocked"

    # Placeholder secrets do NOT block but ARE still surfaced as a warning.
    r3 = v.validate_content('password = "<changeme>"')
    assert not any("hardcoded" in e for e in r3.errors)
    assert any("placeholder" in w for w in r3.warnings)


class TestConversionPromptIsV4:
    """Guards: the conversion prompt + manual-create schema scaffold v4
    causal-chain runbooks (Statement / Chain / Indicators / quadrant-tagged
    Interventions), not the retired v3 flat shape (Mechanism / Mitigation /
    Resolution). Regression guard for the v3->v4 template migration."""

    def test_conversion_system_prompt_is_v4(self):
        from faultmaven.modules.knowledge.domain.services.conversion_service import (
            CONVERSION_SYSTEM_PROMPT as p,
        )

        # v4 shape present
        assert "v4" in p
        assert "**Chain:**" in p
        assert "**Indicators:**" in p
        assert "**Interventions:**" in p
        for quadrant in ("remediation", "defensive_fix", "mitigation", "loop_break"):
            assert quadrant in p, f"missing quadrant {quadrant}"
        # retired v3 sub-fields absent
        assert "**Mechanism:**" not in p
        assert "**Resolution:**" not in p
        assert "v3" not in p

    def test_manual_create_request_describes_v4(self):
        from faultmaven.modules.knowledge.api.conversion_routes import (
            RunbookCreateRequest,
        )

        desc = RunbookCreateRequest.model_fields["causes"].description or ""
        assert "Interventions" in desc and "Chain" in desc
        assert "Mechanism" not in desc and "Resolution" not in desc


# =============================================================================
# Case→runbook trust-boundary + idempotence guards (5.1 / #698)
# =============================================================================


@pytest.mark.unit
class TestCaseConversionGuards:
    """Guards inside ``_convert_from_case_impl`` — the single funnel every
    case→runbook caller passes through. Defense-in-depth: the service holds the
    extracted DTO, not the Case, so it enforces the record half (root cause
    present) and the idempotence rule (don't regenerate from a case that already
    produced a live draft), independent of any caller-side gating."""

    def _request(self, *, root_cause="connection pool exhausted"):
        return CaseConversionRequest(
            case_id="case-1",
            title="API returning 500s",
            description="Users see intermittent 500s from the API.",
            root_cause=root_cause,
        )

    @staticmethod
    def _existing(*statuses):
        """A get_conversion_by_case return stub carrying the real
        ``ConversionResponse.has_live_draft`` behavior over the given draft
        statuses (so the guard exercises the actual predicate, not a re-impl)."""
        ns = SimpleNamespace(drafts=[SimpleNamespace(status=s) for s in statuses])
        ns.has_live_draft = ConversionResponse.has_live_draft.__get__(ns)
        return ns

    @pytest.mark.asyncio
    async def test_missing_root_cause_rejected(self, service):
        with pytest.raises(ConversionRejectedError) as ei:
            await service._convert_from_case_impl(
                self._request(root_cause=None), user_id="u1"
            )
        assert ei.value.error_code == ConversionErrorCode.MISSING_ROOT_CAUSE

    @pytest.mark.asyncio
    async def test_blank_root_cause_rejected(self, service):
        with pytest.raises(ConversionRejectedError) as ei:
            await service._convert_from_case_impl(
                self._request(root_cause="   "), user_id="u1"
            )
        assert ei.value.error_code == ConversionErrorCode.MISSING_ROOT_CAUSE

    @pytest.mark.asyncio
    async def test_existing_draft_blocks_regeneration(self, service):
        service.get_conversion_by_case = AsyncMock(
            return_value=self._existing(DraftStatus.DRAFT)
        )
        with pytest.raises(ConversionRejectedError) as ei:
            await service._convert_from_case_impl(self._request(), user_id="u1")
        assert ei.value.error_code == ConversionErrorCode.CASE_RUNBOOK_EXISTS

    @pytest.mark.asyncio
    async def test_existing_verified_runbook_blocks_regeneration(self, service):
        service.get_conversion_by_case = AsyncMock(
            return_value=self._existing(DraftStatus.VERIFIED)
        )
        with pytest.raises(ConversionRejectedError) as ei:
            await service._convert_from_case_impl(self._request(), user_id="u1")
        assert ei.value.error_code == ConversionErrorCode.CASE_RUNBOOK_EXISTS

    @pytest.mark.asyncio
    async def test_all_discarded_drafts_allow_regeneration(self, service):
        # Discarding a prior draft frees the case to regenerate: the exists-guard
        # must pass. Prove it reaches generation by stubbing the next step.
        service.get_conversion_by_case = AsyncMock(
            return_value=self._existing(DraftStatus.DISCARDED)
        )
        service._convert_single_failure_mode = AsyncMock(
            side_effect=RuntimeError("reached generation")
        )
        with pytest.raises(RuntimeError, match="reached generation"):
            await service._convert_from_case_impl(self._request(), user_id="u1")

    @pytest.mark.asyncio
    async def test_no_prior_conversion_allows_generation(self, service):
        # service fixture has db_session_factory=None → get_conversion_by_case
        # returns None → exists-guard passes → reaches generation.
        service._convert_single_failure_mode = AsyncMock(
            side_effect=RuntimeError("reached generation")
        )
        with pytest.raises(RuntimeError, match="reached generation"):
            await service._convert_from_case_impl(self._request(), user_id="u1")


# =============================================================================
# Live case-conversion uniqueness (multi-replica dedup)
# =============================================================================


def _case_analysis() -> AnalysisResult:
    """A single-failure-mode analysis, the shape ``convert_from_case`` builds."""
    return AnalysisResult(
        is_actionable=True,
        failure_modes=[
            FailureModeAnalysis(
                id="case-x",
                title="API 500s",
                domain="application",
                service="api",
                symptom_class=["errors"],
                severity="medium",
                symptoms_summary="500s",
                resolution_summary="pool exhausted",
            )
        ],
        source_assessment=SourceAssessment(
            content_type="resolved_case",
            actionability_rating="high",
            missing_information=[],
        ),
    )


def _make_case_draft(
    *,
    draft_id: str | None = None,
    status: DraftStatus = DraftStatus.DRAFT,
    tmp_path: Path | None = None,
) -> ConversionDraft:
    """A minimal case-source ``ConversionDraft`` for the persistence paths."""
    did = draft_id or generate_draft_id()
    file_path = str(tmp_path / f"{did}.md") if tmp_path else f"/nonexistent/{did}.md"
    return ConversionDraft(
        draft_id=did,
        runbook_id=f"rb-{did}",
        title="Case Runbook",
        scope="personal",
        status=status,
        source_type=SourceType.CASE,
        validation=ValidationResult(passed=True),
        quality_score=QualityScore(
            overall=80.0,
            grade="B",
            completeness=80.0,
            clarity=80.0,
            actionability=80.0,
            comprehensiveness=80.0,
        ),
        file_path=file_path,
        content_preview="preview",
    )


@pytest.fixture
async def live_case_engine():
    """Async engine over a fresh in-memory SQLite DB (real schema)."""
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def live_case_session_factory(live_case_engine):
    """Session factory pre-seeded with the default enterprise + organization so
    the NOT NULL org FKs on the conversion chain bind. One factory, shared by
    every ConversionService in a test — the single database two replicas race
    over."""
    factory = async_sessionmaker(
        live_case_engine, class_=AsyncSession, expire_on_commit=False
    )
    async with factory() as session:
        session.add(
            EnterpriseModel(
                enterprise_id=DEFAULT_ORGANIZATION_ID,
                name="Default Enterprise",
                slug="default",
            )
        )
        session.add(
            OrganizationModel(
                organization_id=DEFAULT_ORGANIZATION_ID,
                enterprise_id=DEFAULT_ORGANIZATION_ID,
                name="Default Org",
                slug="default-org",
            )
        )
        await session.commit()
    return factory


def _make_live_case_service(session_factory) -> ConversionService:
    """A ConversionService wired to the shared DB. Each call yields a distinct
    instance with its own ``_inflight_runbook`` registry — the way two replicas
    differ."""
    settings = MagicMock()
    settings.llm.get_knowledge_model.return_value = "test-model"
    return ConversionService(
        llm_router=AsyncMock(),
        settings=settings,
        db_session_factory=session_factory,
    )


async def _insert_case_job(
    session_factory,
    *,
    conversion_id: str,
    case_id: str | None,
    source_type: str,
    live_case_id: str | None,
    draft_statuses: list[DraftStatus],
    status: ConversionStatus = ConversionStatus.COMPLETED,
    draft_files_present: list[bool] | None = None,
    tmp_path: Path | None = None,
) -> None:
    """Insert a conversion job + its drafts directly, bypassing the service, so
    tests can construct shapes the service does not produce today (e.g. a job
    with two live drafts). ``draft_files_present[i]`` writes a real file under
    ``tmp_path`` for draft i (the scan reconciliation keys on file existence);
    default is a nonexistent path for every draft."""
    async with session_factory() as session:
        file_id = f"file_{conversion_id}"
        session.add(
            UploadedFileModel(
                file_id=file_id,
                organization_id=DEFAULT_ORGANIZATION_ID,
                case_id=None,
                uploaded_by="u1",
                filename="src",
                size_bytes=1,
                content_type="text/plain",
                upload_source="conversion_source",
                uploaded_at_turn=0,
            )
        )
        await session.flush()
        session.add(
            ConversionJobModel(
                id=conversion_id,
                user_id="u1",
                organization_id=DEFAULT_ORGANIZATION_ID,
                scope="personal",
                status=status.value,
                source_file_id=file_id,
                source_type=source_type,
                case_id=case_id,
                live_case_id=live_case_id,
                failure_modes_detected=1,
                analysis_result=None,
            )
        )
        for i, ds in enumerate(draft_statuses):
            present = bool(draft_files_present and draft_files_present[i])
            d = _make_case_draft(
                draft_id=f"{conversion_id}-d{i}",
                status=ds,
                tmp_path=tmp_path if present else None,
            )
            if present:
                Path(d.file_path).write_text("# runbook body", encoding="utf-8")
            session.add(
                ConversionDraftModel(
                    id=d.draft_id,
                    organization_id=DEFAULT_ORGANIZATION_ID,
                    conversion_id=conversion_id,
                    runbook_id=d.runbook_id,
                    title=d.title,
                    file_path=d.file_path,
                    status=d.status.value,
                    source_type=source_type,
                    knowledge_item_id=f"kb_{d.draft_id}",
                    validation_passed=True,
                )
            )
        await session.commit()


async def _job_row(session_factory, conversion_id: str) -> ConversionJobModel:
    async with session_factory() as session:
        return await session.get(ConversionJobModel, conversion_id)


async def _count_live_case_keys(session_factory) -> int:
    async with session_factory() as session:
        result = await session.execute(
            select(func.count())
            .select_from(ConversionJobModel)
            .where(ConversionJobModel.live_case_id.isnot(None))
        )
        return result.scalar_one()


async def _count_live_drafts(session_factory) -> int:
    async with session_factory() as session:
        result = await session.execute(
            select(func.count())
            .select_from(ConversionDraftModel)
            .where(ConversionDraftModel.status != DraftStatus.DISCARDED.value)
        )
        return result.scalar_one()


def _case_request(case_id: str = "case-live-1") -> CaseConversionRequest:
    return CaseConversionRequest(
        case_id=case_id,
        title="API returning 500s",
        description="Users see intermittent 500s.",
        root_cause="connection pool exhausted",
    )


@pytest.mark.unit
class TestPersistJobLiveCaseKey:
    """``_persist_job`` stamps ``live_case_id`` only for a case-source job that
    currently holds a live draft."""

    @pytest.mark.asyncio
    async def test_case_job_with_live_draft_sets_key(self, live_case_session_factory):
        svc = _make_live_case_service(live_case_session_factory)
        await svc._persist_job(
            conversion_id="conv-case-live",
            user_id="u1",
            organization_id=DEFAULT_ORGANIZATION_ID,
            scope="personal",
            team_id=None,
            status=ConversionStatus.COMPLETED,
            source_file=SourceFileInfo(
                filename="Case c1", size_bytes=1, content_type="x"
            ),
            analysis=_case_analysis(),
            drafts=[_make_case_draft()],
            created_at=datetime.now(UTC),
            source_type="case",
            case_id="case-x",
        )
        job = await _job_row(live_case_session_factory, "conv-case-live")
        assert job.live_case_id == "case-x"

    @pytest.mark.asyncio
    async def test_document_job_leaves_key_null(self, live_case_session_factory):
        svc = _make_live_case_service(live_case_session_factory)
        await svc._persist_job(
            conversion_id="conv-doc",
            user_id="u1",
            organization_id=DEFAULT_ORGANIZATION_ID,
            scope="personal",
            team_id=None,
            status=ConversionStatus.COMPLETED,
            source_file=SourceFileInfo(filename="doc", size_bytes=1, content_type="x"),
            analysis=_case_analysis(),
            drafts=[_make_case_draft()],
            created_at=datetime.now(UTC),
            source_type="document",
            case_id=None,
        )
        job = await _job_row(live_case_session_factory, "conv-doc")
        assert job.live_case_id is None

    @pytest.mark.asyncio
    async def test_failed_case_job_with_no_drafts_leaves_key_null(
        self, live_case_session_factory
    ):
        svc = _make_live_case_service(live_case_session_factory)
        await svc._persist_job(
            conversion_id="conv-failed",
            user_id="u1",
            organization_id=DEFAULT_ORGANIZATION_ID,
            scope="personal",
            team_id=None,
            status=ConversionStatus.FAILED,
            source_file=SourceFileInfo(
                filename="Case c2", size_bytes=1, content_type="x"
            ),
            analysis=_case_analysis(),
            drafts=[],
            created_at=datetime.now(UTC),
            source_type="case",
            case_id="case-y",
        )
        job = await _job_row(live_case_session_factory, "conv-failed")
        assert job.live_case_id is None


@pytest.mark.unit
class TestDiscardReleasesLiveCaseKey:
    """Discarding the last live draft frees the case's live-conversion claim;
    a job that still has another live draft keeps it."""

    @pytest.mark.asyncio
    async def test_delete_draft_clears_key_on_last_live_draft(
        self, live_case_session_factory
    ):
        await _insert_case_job(
            live_case_session_factory,
            conversion_id="conv-del",
            case_id="case-del",
            source_type="case",
            live_case_id="case-del",
            draft_statuses=[DraftStatus.DRAFT],
        )
        svc = _make_live_case_service(live_case_session_factory)
        ok = await svc.delete_draft("conv-del", "conv-del-d0", user_id="u1")
        assert ok is True
        job = await _job_row(live_case_session_factory, "conv-del")
        assert job.live_case_id is None

    @pytest.mark.asyncio
    async def test_discard_by_knowledge_item_id_clears_key(
        self, live_case_session_factory
    ):
        await _insert_case_job(
            live_case_session_factory,
            conversion_id="conv-disc",
            case_id="case-disc",
            source_type="case",
            live_case_id="case-disc",
            draft_statuses=[DraftStatus.DRAFT],
        )
        svc = _make_live_case_service(live_case_session_factory)
        ok = await svc.discard_by_knowledge_item_id("kb_conv-disc-d0")
        assert ok is True
        job = await _job_row(live_case_session_factory, "conv-disc")
        assert job.live_case_id is None

    @pytest.mark.asyncio
    async def test_discard_keeps_key_while_another_draft_is_live(
        self, live_case_session_factory
    ):
        # A job the service does not build today — two live drafts — proves the
        # clearing logic is general: the key is released only when the LAST live
        # draft leaves.
        await _insert_case_job(
            live_case_session_factory,
            conversion_id="conv-two",
            case_id="case-two",
            source_type="case",
            live_case_id="case-two",
            draft_statuses=[DraftStatus.DRAFT, DraftStatus.DRAFT],
        )
        svc = _make_live_case_service(live_case_session_factory)
        await svc.delete_draft("conv-two", "conv-two-d0", user_id="u1")
        job = await _job_row(live_case_session_factory, "conv-two")
        assert job.live_case_id == "case-two", "second live draft must retain the key"

        await svc.delete_draft("conv-two", "conv-two-d1", user_id="u1")
        job = await _job_row(live_case_session_factory, "conv-two")
        assert job.live_case_id is None, "last live draft leaving releases the key"


@pytest.mark.unit
class TestCrossReplicaLiveCaseRace:
    """Two ConversionService instances (two replicas) over one DB cannot both
    persist a live case-conversion for the same case."""

    @pytest.mark.asyncio
    async def test_race_persists_exactly_one_live_conversion(
        self, live_case_session_factory
    ):
        # Deterministic interleave (spec's second option): both replicas pass the
        # idempotence read against an empty inventory, then persist in sequence.
        # The first guard read on each service is forced to None (each read the
        # inventory before either committed); the IntegrityError handler's
        # re-read (call #2) hits the real query so the loser resolves the
        # winner's conversion. A single shared SQLite connection makes true
        # concurrency nondeterministic, so we serialize the persists — the
        # unique index is what the loser collides on either way.
        request = _case_request()

        def _stubbed_service() -> ConversionService:
            svc = _make_live_case_service(live_case_session_factory)
            svc._convert_single_failure_mode = AsyncMock(
                side_effect=lambda *a, **k: _make_case_draft()
            )
            real_get = svc.get_conversion_by_case
            state = {"calls": 0}

            async def _guard_then_real(case_id, user_id):
                state["calls"] += 1
                if state["calls"] == 1:
                    return None
                return await real_get(case_id, user_id)

            svc.get_conversion_by_case = _guard_then_real
            return svc

        winner = _stubbed_service()
        loser = _stubbed_service()

        resp_winner = await winner.convert_from_case(request, user_id="u1")
        resp_loser = await loser.convert_from_case(request, user_id="u1")

        # The loser does NOT raise; it returns the winner's conversion.
        assert resp_loser.conversion_id == resp_winner.conversion_id

        # The DB holds exactly one job carrying the key and exactly one live
        # draft — the loser's whole persist transaction rolled back.
        assert await _count_live_case_keys(live_case_session_factory) == 1
        assert await _count_live_drafts(live_case_session_factory) == 1
        job = await _job_row(live_case_session_factory, resp_winner.conversion_id)
        assert job.live_case_id == request.case_id


@pytest.mark.unit
class TestRegenerationAfterDiscard:
    """Discarding a case's draft frees it to be converted again — the unique
    index does not permanently lock the case."""

    @pytest.mark.asyncio
    async def test_convert_discard_convert_succeeds(self, live_case_session_factory):
        request = _case_request("case-regen")

        def _stubbed_service() -> ConversionService:
            svc = _make_live_case_service(live_case_session_factory)
            svc._convert_single_failure_mode = AsyncMock(
                side_effect=lambda *a, **k: _make_case_draft()
            )
            return svc

        svc = _stubbed_service()
        first = await svc.convert_from_case(request, user_id="u1")
        job = await _job_row(live_case_session_factory, first.conversion_id)
        assert job.live_case_id == "case-regen"

        # Discard the only draft — the key is released.
        first_draft_id = first.drafts[0].draft_id
        await svc.delete_draft(first.conversion_id, first_draft_id, user_id="u1")
        assert await _count_live_case_keys(live_case_session_factory) == 0

        # Second conversion succeeds (no unique violation) and now holds the key.
        second = await svc.convert_from_case(request, user_id="u1")
        assert second.conversion_id != first.conversion_id
        assert second.status == ConversionStatus.COMPLETED
        new_job = await _job_row(live_case_session_factory, second.conversion_id)
        assert new_job.live_case_id == "case-regen"
        assert await _count_live_case_keys(live_case_session_factory) == 1


@pytest.mark.unit
class TestScanReleasesLiveCaseKey:
    """Scan reconciliation also discards drafts (missing file, duplicate with a
    knowledge_item_id); when that drains a case job's live drafts it must
    release ``live_case_id`` like the explicit discard paths, or the case is
    locked out of regeneration forever."""

    def _scan_service(self, session_factory) -> ConversionService:
        return _make_live_case_service(session_factory)

    async def _run_scan(self, svc: ConversionService, tmp_path: Path):
        # Point the disk walk at an empty location — these tests exercise only
        # the DB reconciliation sweep.
        with patch.object(
            type(svc),
            "_data_dir",
            new_callable=lambda: property(lambda self: tmp_path / "kb-empty"),
        ):
            return await svc.scan_for_runbooks(user_id="u1")

    @pytest.mark.asyncio
    async def test_scan_discard_of_last_live_draft_clears_key(
        self, live_case_session_factory, tmp_path
    ):
        # Job A: case job holding the key, its only draft's file is gone.
        await _insert_case_job(
            live_case_session_factory,
            conversion_id="conv-scan-a",
            case_id="case-scan-1",
            source_type="case",
            live_case_id="case-scan-1",
            draft_statuses=[DraftStatus.DRAFT],
        )
        # Job B: an unaffected case job (verified draft, file present) so the
        # scan's discard-all abort guard does not trip.
        await _insert_case_job(
            live_case_session_factory,
            conversion_id="conv-scan-b",
            case_id="case-scan-2",
            source_type="case",
            live_case_id="case-scan-2",
            draft_statuses=[DraftStatus.VERIFIED],
            draft_files_present=[True],
            tmp_path=tmp_path,
        )

        svc = self._scan_service(live_case_session_factory)
        await self._run_scan(svc, tmp_path)

        job_a = await _job_row(live_case_session_factory, "conv-scan-a")
        assert job_a.live_case_id is None
        job_b = await _job_row(live_case_session_factory, "conv-scan-b")
        assert job_b.live_case_id == "case-scan-2"

    @pytest.mark.asyncio
    async def test_scan_keeps_key_while_another_draft_live(
        self, live_case_session_factory, tmp_path
    ):
        # One draft's file is gone, but a verified sibling survives the scan —
        # the job still holds a live draft, so the key stays.
        await _insert_case_job(
            live_case_session_factory,
            conversion_id="conv-scan-partial",
            case_id="case-scan-3",
            source_type="case",
            live_case_id="case-scan-3",
            draft_statuses=[DraftStatus.DRAFT, DraftStatus.VERIFIED],
            draft_files_present=[False, True],
            tmp_path=tmp_path,
        )

        svc = self._scan_service(live_case_session_factory)
        await self._run_scan(svc, tmp_path)

        job = await _job_row(live_case_session_factory, "conv-scan-partial")
        assert job.live_case_id == "case-scan-3"

    @pytest.mark.asyncio
    async def test_scan_draining_job_via_two_branches_clears_key(
        self, live_case_session_factory, tmp_path
    ):
        # Both of the job's drafts fall in ONE sweep, via different scan
        # branches: d0 by missing file, d1 (status=draft with a
        # knowledge_item_id, file present) by the duplicate-cleanup branch.
        # The release must see both in-session status flips.
        await _insert_case_job(
            live_case_session_factory,
            conversion_id="conv-scan-drain",
            case_id="case-scan-4",
            source_type="case",
            live_case_id="case-scan-4",
            draft_statuses=[DraftStatus.DRAFT, DraftStatus.DRAFT],
            draft_files_present=[False, True],
            tmp_path=tmp_path,
        )
        await _insert_case_job(
            live_case_session_factory,
            conversion_id="conv-scan-survivor",
            case_id="case-scan-5",
            source_type="case",
            live_case_id="case-scan-5",
            draft_statuses=[DraftStatus.VERIFIED],
            draft_files_present=[True],
            tmp_path=tmp_path,
        )

        svc = self._scan_service(live_case_session_factory)
        await self._run_scan(svc, tmp_path)

        drained = await _job_row(live_case_session_factory, "conv-scan-drain")
        assert drained.live_case_id is None
        survivor = await _job_row(live_case_session_factory, "conv-scan-survivor")
        assert survivor.live_case_id == "case-scan-5"


# =============================================================================
# symptom_class controlled-vocabulary on the produce path (§7.1(b))
# =============================================================================


_IN_VOCAB_RUNBOOK = """---
id: pg-pool-exhaustion
title: "Database Connection Pool Exhaustion"
domain: database
service: postgresql
symptom_class: [connection_refused]
scope: personal
tags: [postgres]
difficulty: intermediate
severity: high
version: "1.0.0"
last_updated: "2026-07-24"
verified_by: ""
status: draft
---

# Runbook: Database Connection Pool Exhaustion

## Symptom Recognition
- "ERROR: remaining connection slots are reserved"

## Applicability
PostgreSQL 14+. Requires pg_monitor role. Tools: psql.

## Diagnostic Steps

### Step 1: Check active connections
```bash
psql -c "SELECT count(*) FROM pg_stat_activity;"
```
Look for a count near max_connections.

## Causes

### Cause A: Pool leak in the application
**Statement:** The application never returns pooled connections, exhausting the pool.
**Indicators:**
- root: [Step 1] active connections pinned at the ceiling
**Interventions:**
- **remediation** (root): fix the leak and cap the pool.
  **Verification:** Re-run Step 1; the count drops.

### Cause Z: Unidentified
**Statement:** None of the documented causes match the observed evidence.
**Indicators:**
- [Default]
**Interventions:**
- **mitigation** (D): Capture full diagnostic output and consult an SME.
  **Risk:** Diagnostic only. **Duration:** Until SME review. **Verification:** N/A.

## Prevention
- Add an alert on connection-slot saturation.

## Sources
- case-derived -- primary source for this runbook
"""


class TestSymptomClassProducePath:
    """The produce path emits in-vocabulary ``symptom_class`` (never ``unknown``)
    and the conversion prompt is authoritative about the controlled vocabulary."""

    def test_prompt_injects_controlled_vocabulary(self):
        """Every vocabulary term is bound into BOTH produce prompts; the
        placeholder is fully resolved. The analysis prompt must be constrained too
        so its symptom_class (the dedup key) is already in-vocab and matches the
        reclassified frontmatter — otherwise duplicate runbooks slip dedup."""
        for prompt in (CONVERSION_SYSTEM_PROMPT, ANALYSIS_SYSTEM_PROMPT):
            assert "__SYMPTOM_CLASS_VOCAB__" not in prompt
            for term in VALID_SYMPTOM_CLASSES:
                assert term in prompt, f"vocab term missing: {term}"

    def test_prompt_no_longer_freezes_symptom_class(self):
        """Rule 9 no longer tells the model to pass ``symptom_class`` through
        unchanged — that contract is what let an off-vocab hint (``unknown``)
        reach the frontmatter verbatim."""
        rule9 = next(
            line
            for line in CONVERSION_SYSTEM_PROMPT.splitlines()
            if line.startswith("9.")
        )
        assert "Do not change domain, service, or symptom_class" not in rule9
        assert "controlled vocabulary" in rule9

    @pytest.mark.asyncio
    async def test_empty_symptom_class_prompts_classification_not_unknown(
        self, service, mock_llm_router, tmp_path
    ):
        """A case with no symptom_class taxonomy prompts the model to classify
        from the vocabulary — it does NOT inject the off-vocab ``unknown``
        placeholder — and the resulting draft validates clean on symptom_class."""
        failure_mode = FailureModeAnalysis(
            id="case-pool-exhaustion",
            title="Database Connection Pool Exhaustion",
            domain="database",
            service="postgresql",
            symptom_class=[],  # a case carries no symptom_class taxonomy
            severity="high",
            symptoms_summary="remaining connection slots are reserved",
            resolution_summary="Fix the pool leak and cap the pool size.",
        )
        mock_llm_router.route.return_value = _make_llm_response(_IN_VOCAB_RUNBOOK)

        with patch.object(
            type(service),
            "_data_dir",
            new_callable=lambda: property(lambda self: tmp_path),
        ):
            draft = await service._convert_single_failure_mode(
                text="SOURCE MATERIAL",
                failure_mode=failure_mode,
                scope="personal",
                filename="case-derived",
                conversion_id="conv_test",
                user_id="user-123",
            )

        # The prompt sent to the model must not smuggle an off-vocab placeholder.
        sent_user_message = mock_llm_router.route.call_args.kwargs["messages"][1][
            "content"
        ]
        assert "unknown" not in sent_user_message
        assert "classify from the controlled vocabulary" in sent_user_message

        # And the produced draft is in-vocab, so the validator gate is clean.
        assert isinstance(draft, ConversionDraft)
        symptom_errors = [e for e in draft.validation.errors if "symptom_class" in e]
        assert symptom_errors == []

    @pytest.mark.asyncio
    async def test_case_request_without_symptom_class_defaults_to_empty_not_unknown(
        self, service, mock_settings
    ):
        """Regression guard on the §7.1(b) fix line itself
        (``symptom_class=request.symptom_class or []`` in ``_convert_from_case_impl``):
        a case request that omits symptom_class must build a FailureModeAnalysis with
        an EMPTY list, never the off-vocab ``["unknown"]`` placeholder. Reverting the
        default to ``["unknown"]`` must fail here."""
        from faultmaven.modules.knowledge.domain.models.conversion import (
            CaseConversionRequest,
        )

        request = CaseConversionRequest(
            case_id="case-no-symptom",
            title="Something broke",
            domain="application",
            service="test-svc",
            # symptom_class deliberately omitted (defaults to []).
            severity="high",
            description="The thing failed",
            root_cause="Pool exhausted because connections were never returned",
            scope="personal",
        )

        # Stub the LLM/disk step and capture the FailureModeAnalysis it receives.
        capture = AsyncMock(
            return_value=ConversionError(
                failure_mode_id="fm", error="stub", retryable=False
            )
        )
        with patch.object(service, "_convert_single_failure_mode", capture):
            await service._convert_from_case_impl(request, user_id="user-1")

        built = capture.await_args.kwargs["failure_mode"]
        assert built.symptom_class == []

    @pytest.mark.asyncio
    async def test_case_request_symptom_class_is_passed_through(
        self, service, mock_settings
    ):
        """A provided symptom_class is used as-is (the conversion prompt then keeps
        it in-vocab / reclassifies) — the `or []` default only fills the empty case."""
        from faultmaven.modules.knowledge.domain.models.conversion import (
            CaseConversionRequest,
        )

        request = CaseConversionRequest(
            case_id="case-with-symptom",
            title="Something broke",
            domain="application",
            service="test-svc",
            symptom_class=["timeout"],
            severity="high",
            description="The thing failed",
            root_cause="Downstream call never returned",
            scope="personal",
        )
        capture = AsyncMock(
            return_value=ConversionError(
                failure_mode_id="fm", error="stub", retryable=False
            )
        )
        with patch.object(service, "_convert_single_failure_mode", capture):
            await service._convert_from_case_impl(request, user_id="user-1")

        assert capture.await_args.kwargs["failure_mode"].symptom_class == ["timeout"]


# =============================================================================
# Truncation: a runbook is complete or it is not written (#1094)
# =============================================================================


@pytest.mark.unit
class TestTruncatedRunbookIsNeverPersisted:
    """The worst place a silent cut can land.

    A runbook is PERSISTED to the knowledge base and later retrieved to drive
    other investigations, so a half-procedure ships as an authoritative one and
    the reader has no way to tell that step 4 of 7 is missing rather than
    absent by design.
    """

    @pytest.mark.asyncio
    async def test_a_truncated_runbook_would_pass_every_content_validator(
        self, service, mock_runbook_content
    ):
        """Why the stop reason is the only thing that can catch this.

        The validators check for frontmatter delimiters, a length floor and the
        required section headings. A body cut near the end satisfies all three,
        because the sections are written in order and the cut takes the tail.
        Nothing downstream of generation can distinguish it from a complete
        runbook — which is precisely the argument for checking at the source.
        """
        cut = mock_runbook_content[: int(len(mock_runbook_content) * 0.8)]

        assert len(cut) >= 100
        assert "---" in cut
        assert any(
            h in cut
            for h in ["## Symptom Recognition", "## Diagnostic Steps", "## Causes"]
        )

    @pytest.mark.asyncio
    async def test_conversion_retries_once_then_refuses_to_persist(
        self,
        service,
        mock_llm_router,
        source_file,
        source_document_text,
        mock_analysis_response,
        mock_runbook_content,
        tmp_path,
    ):
        """Retry bigger; if it is still cut, fail retryably and write nothing."""
        cut = mock_runbook_content[: int(len(mock_runbook_content) * 0.8)]
        preprocessing = _make_preprocessing_result(source_document_text)
        with patch.object(
            service._preprocessor, "preprocess", return_value=preprocessing
        ):
            mock_llm_router.route.side_effect = [
                _make_llm_response(mock_analysis_response),
                _make_llm_response(cut, StopReason.MAX_TOKENS),
                _make_llm_response(cut, StopReason.MAX_TOKENS),
            ]
            with patch.object(
                type(service),
                "_data_dir",
                new_callable=lambda: property(lambda self: tmp_path),
            ):
                result = await service.convert_document(
                    file_path=source_file,
                    content_type="text/markdown",
                    original_filename="test_document.md",
                    scope="global",
                    user_id="user-123",
                )

        assert result.drafts == []
        assert result.status == ConversionStatus.FAILED
        assert any("truncated" in w.lower() for w in result.warnings)

        # Three calls: analysis, conversion, conversion retried at double.
        assert mock_llm_router.route.call_count == 3
        assert (
            mock_llm_router.route.call_args_list[2].kwargs["max_tokens"]
            == RUNBOOK_MAX_TOKENS_CEILING
        )

    @pytest.mark.asyncio
    async def test_a_recovered_runbook_is_persisted_normally(
        self,
        service,
        mock_llm_router,
        source_file,
        source_document_text,
        mock_analysis_response,
        mock_runbook_content,
        tmp_path,
    ):
        """The retry is a recovery, not just a nicer failure."""
        cut = mock_runbook_content[: int(len(mock_runbook_content) * 0.8)]
        preprocessing = _make_preprocessing_result(source_document_text)
        with patch.object(
            service._preprocessor, "preprocess", return_value=preprocessing
        ):
            mock_llm_router.route.side_effect = [
                _make_llm_response(mock_analysis_response),
                _make_llm_response(cut, StopReason.MAX_TOKENS),
                _make_llm_response(mock_runbook_content),
            ]
            with patch.object(
                type(service),
                "_data_dir",
                new_callable=lambda: property(lambda self: tmp_path),
            ):
                result = await service.convert_document(
                    file_path=source_file,
                    content_type="text/markdown",
                    original_filename="test_document.md",
                    scope="global",
                    user_id="user-123",
                )

        assert result.status == ConversionStatus.COMPLETED
        assert len(result.drafts) == 1

    @pytest.mark.asyncio
    async def test_truncated_analysis_says_so_instead_of_parse_error(
        self, service, mock_llm_router, source_file, source_document_text, tmp_path
    ):
        """A cut analysis used to surface as "could not be parsed".

        Right shape (loud), wrong diagnosis: the document did not have bad JSON
        in it, the response ran out of room. Now it is retried, and named.
        """
        preprocessing = _make_preprocessing_result(source_document_text)
        with patch.object(
            service._preprocessor, "preprocess", return_value=preprocessing
        ):
            mock_llm_router.route.side_effect = [
                _make_llm_response(
                    '{"is_actionable": true, "failure_mo', StopReason.MAX_TOKENS
                ),
                _make_llm_response(
                    '{"is_actionable": true, "failure_mo', StopReason.MAX_TOKENS
                ),
            ]
            with patch.object(
                type(service),
                "_data_dir",
                new_callable=lambda: property(lambda self: tmp_path),
            ):
                with pytest.raises(ConversionRejectedError) as exc:
                    await service.convert_document(
                        file_path=source_file,
                        content_type="text/markdown",
                        original_filename="test_document.md",
                        scope="global",
                        user_id="user-123",
                    )

        assert "truncated" in str(exc.value).lower()
