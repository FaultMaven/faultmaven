"""Unit tests for conversion domain models and ID generation."""

import pytest

from faultmaven.modules.knowledge.domain.models.conversion import (
    AnalysisResult,
    ConversionDraft,
    ConversionResponse,
    ConversionStatus,
    DraftStatus,
    DraftUpdateRequest,
    FailureModeAnalysis,
    QualityScore,
    RedactionEntry,
    RedactionReport,
    SourceAssessment,
    SourceFileInfo,
    TriageResult,
    ValidationResult,
    VerifyResponse,
    generate_conversion_id,
    generate_draft_id,
    generate_runbook_id,
)


@pytest.mark.unit
class TestIDGeneration:

    def test_conversion_id_format(self):
        cid = generate_conversion_id()
        assert cid.startswith("conv_")
        assert len(cid) == 17  # "conv_" + 12 hex chars

    def test_conversion_id_unique(self):
        ids = {generate_conversion_id() for _ in range(100)}
        assert len(ids) == 100

    def test_draft_id_format(self):
        did = generate_draft_id()
        assert did.startswith("draft_")
        assert len(did) == 18  # "draft_" + 12 hex chars

    def test_runbook_id_kebab_case(self):
        fm = FailureModeAnalysis(
            id="test",
            title="Connection Pool Exhaustion",
            domain="database",
            service="postgresql",
            symptom_class=["connection_refused"],
            severity="high",
            symptoms_summary="Too many connections",
            resolution_summary="Resize pool",
        )
        rid = generate_runbook_id(fm)
        assert rid == "postgresql-connection-pool-exhaustion"
        assert rid.islower()
        assert " " not in rid

    def test_runbook_id_strips_special_chars(self):
        fm = FailureModeAnalysis(
            id="test",
            title="OOM Kill (Out of Memory)",
            domain="compute",
            service="kubernetes",
            symptom_class=["oom"],
            severity="critical",
            symptoms_summary="OOMKilled",
            resolution_summary="Increase limits",
        )
        rid = generate_runbook_id(fm)
        assert "(" not in rid
        assert ")" not in rid
        assert rid.startswith("kubernetes-")

    def test_runbook_id_truncation(self):
        fm = FailureModeAnalysis(
            id="test",
            title="A" * 100,
            domain="database",
            service="very-long-service-name",
            symptom_class=["test"],
            severity="low",
            symptoms_summary="test",
            resolution_summary="test",
        )
        rid = generate_runbook_id(fm)
        assert len(rid) <= 60


@pytest.mark.unit
class TestConversionStatus:

    def test_status_values(self):
        assert ConversionStatus.PROCESSING.value == "processing"
        assert ConversionStatus.COMPLETED.value == "completed"
        assert ConversionStatus.PARTIAL.value == "partial"
        assert ConversionStatus.FAILED.value == "failed"

    def test_draft_status_values(self):
        assert DraftStatus.DRAFT.value == "draft"
        assert DraftStatus.VERIFIED.value == "verified"
        assert DraftStatus.DELETED.value == "deleted"


@pytest.mark.unit
class TestFailureModeAnalysis:

    def test_create_from_dict(self):
        data = {
            "id": "pg-pool-exhaustion",
            "title": "PostgreSQL Connection Pool Exhaustion",
            "domain": "database",
            "service": "postgresql",
            "symptom_class": ["connection_refused", "latency"],
            "severity": "high",
            "symptoms_summary": "FATAL: too many connections",
            "resolution_summary": "Terminate idle connections",
        }
        fm = FailureModeAnalysis(**data)
        assert fm.id == "pg-pool-exhaustion"
        assert len(fm.symptom_class) == 2

    def test_analysis_result_actionable(self):
        result = AnalysisResult(
            is_actionable=True,
            failure_modes=[],
            source_assessment=SourceAssessment(
                content_type="troubleshooting_guide",
                actionability_rating="high",
                missing_information=[],
            ),
        )
        assert result.is_actionable is True

    def test_analysis_result_not_actionable(self):
        result = AnalysisResult(
            is_actionable=False,
            failure_modes=[],
            source_assessment=SourceAssessment(
                content_type="architecture_doc",
                actionability_rating="low",
                missing_information=["No diagnostic steps"],
            ),
        )
        assert result.is_actionable is False
        assert len(result.source_assessment.missing_information) == 1


@pytest.mark.unit
class TestRedactionReport:

    def test_empty_report(self):
        report = RedactionReport()
        assert report.total_redacted == 0
        assert report.warning is None
        assert len(report.redactions) == 0

    def test_report_with_redactions(self):
        report = RedactionReport(
            redactions=[
                RedactionEntry(type="api_key", count=2, context="Found in code blocks"),
            ],
            warning="2 sensitive items were redacted.",
            total_redacted=2,
        )
        assert report.total_redacted == 2
        assert report.redactions[0].type == "api_key"


@pytest.mark.unit
class TestDraftUpdateRequest:

    def test_valid_content(self):
        req = DraftUpdateRequest(content="x" * 100)
        assert len(req.content) == 100

    def test_content_too_short(self):
        with pytest.raises(Exception):
            DraftUpdateRequest(content="short")


@pytest.mark.unit
class TestTriageResult:

    def test_actionable(self):
        result = TriageResult(
            is_actionable=True, confidence=0.95, reason="Contains diagnostic steps"
        )
        assert result.is_actionable
        assert result.confidence > 0.8

    def test_not_actionable(self):
        result = TriageResult(
            is_actionable=False,
            confidence=0.85,
            reason="Architecture documentation",
        )
        assert not result.is_actionable
