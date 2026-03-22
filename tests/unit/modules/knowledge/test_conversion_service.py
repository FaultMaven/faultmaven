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

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.modules.knowledge.domain.models.conversion import (
    AnalysisResult,
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
        """Invalid JSON from LLM raises ValueError."""
        mock_llm_router.route.return_value = _make_llm_response("not valid json {{{")

        with pytest.raises(
            ValueError, match="LLM analysis response could not be parsed"
        ):
            await service._analyze_document("sample text", "test.md")


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

        # Source should be retained
        assert result.source_file.filename == "test_document.md"
        retained = Path(result.source_file.retained_path)
        assert retained.exists()
        assert retained.read_text() == source_document_text
