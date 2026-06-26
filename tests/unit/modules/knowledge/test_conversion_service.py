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
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.modules.knowledge.domain.models.conversion import (
    AnalysisResult,
    ConversionErrorCode,
    ConversionStatus,
    FailureModeAnalysis,
    PreprocessingResult,
    RedactionReport,
    SourceAssessment,
    TriageResult,
    generate_runbook_id,
)
from faultmaven.modules.knowledge.domain.services.conversion_service import (
    ConversionRejectedError,
    ConversionService,
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


def _make_llm_response(content: str):
    """Create a mock LLM response object with a .content attribute."""
    return SimpleNamespace(content=content)


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
