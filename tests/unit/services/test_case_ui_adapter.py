"""Unit tests for case_ui_adapter.py (transform_case_for_ui).

Tests the phase-adaptive UI transformation layer:
- INQUIRY → CaseUIResponse_Inquiry
- INVESTIGATING → CaseUIResponse_Investigating
- RESOLVED → CaseUIResponse_Resolved
- CLOSED → CaseUIResponse_Resolved (with closure status)

Covers new fields: EvidenceSummary.collected_at_turn, EvidenceSummary.category,
uploaded_files_count on Investigating and Resolved responses.
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from faultmaven.models.case_ui import (
    CaseUIResponse_Inquiry,
    CaseUIResponse_Investigating,
    CaseUIResponse_Resolved,
)
from faultmaven.modules.case.domain.models import (
    Case,
    CaseStatus,
    ConfidenceLevel,
    Evidence,
    EvidenceCategory,
    EvidenceForm,
    EvidenceSourceType,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisStatus,
    InquiryData,
    InvestigationPath,
    PathSelection,
    ProblemConfirmation,
    RootCauseConclusion,
    Solution,
    SolutionType,
    UploadedFile,
)
from faultmaven.services.adapters.case_ui_adapter import transform_case_for_ui

# ============================================================
# Helpers
# ============================================================


def _make_case_id() -> str:
    return f"case_{uuid4().hex[:12]}"


def _make_inquiry_case(**overrides) -> Case:
    """Create a minimal INQUIRY case."""
    defaults = dict(
        case_id=_make_case_id(),
        user_id="test-user",
        organization_id="test-org",
        title="DNS resolution failing",
        description="",
    )
    defaults.update(overrides)
    return Case(**defaults)


def _make_investigating_case(**overrides) -> Case:
    """Create a valid INVESTIGATING case with required fields."""
    case = _make_inquiry_case(
        description="Production DNS failing",
        **{
            k: v
            for k, v in overrides.items()
            if k
            in {
                "case_id",
                "user_id",
                "organization_id",
                "title",
                "description",
            }
        },
    )
    case.inquiry.proposed_problem_statement = "DNS resolution failing on prod"
    case.inquiry.problem_statement_confirmed = True
    case.inquiry.decided_to_investigate = True
    case.status = CaseStatus.INVESTIGATING

    # Apply remaining overrides
    for k, v in overrides.items():
        if k not in {"case_id", "user_id", "organization_id", "title", "description"}:
            setattr(case, k, v)

    return case


def _make_resolved_case(**overrides) -> Case:
    """Create a valid RESOLVED case.

    Uses object.__setattr__ to bypass Pydantic's bidirectional validators
    (resolved_at requires RESOLVED status; RESOLVED status requires resolved_at).
    """
    case = _make_investigating_case()
    # Set all terminal fields atomically to bypass cross-field validators
    now = datetime.now(timezone.utc)
    object.__setattr__(case, "resolved_at", now)
    object.__setattr__(case, "closed_at", now)
    object.__setattr__(case, "closure_reason", "resolved")
    object.__setattr__(case, "status", CaseStatus.RESOLVED)
    for k, v in overrides.items():
        object.__setattr__(case, k, v)
    return case


def _make_closed_case(**overrides) -> Case:
    """Create a valid CLOSED case.

    Uses object.__setattr__ to bypass Pydantic's bidirectional validators.
    """
    case = _make_investigating_case()
    now = datetime.now(timezone.utc)
    object.__setattr__(case, "closed_at", now)
    object.__setattr__(case, "closure_reason", "closed_by_user")
    object.__setattr__(case, "status", CaseStatus.CLOSED)
    for k, v in overrides.items():
        object.__setattr__(case, k, v)
    return case


def _make_evidence(
    turn: int = 1,
    category: EvidenceCategory = EvidenceCategory.SYMPTOM_EVIDENCE,
    source_type: EvidenceSourceType = EvidenceSourceType.LOGS,
    summary: str = "Connection timeout errors",
) -> Evidence:
    """Create an Evidence object."""
    return Evidence(
        evidence_id=f"ev_{uuid4().hex[:12]}",
        category=category,
        primary_purpose="symptom_verified",
        summary=summary,
        preprocessed_content="2025-01-01 ERROR: timeout",
        source_type=source_type,
        form=EvidenceForm.DOCUMENT,
        content_size_bytes=1024,
        preprocessing_method="crime_scene_extraction",
        collected_by="test-user",
        collected_at_turn=turn,
    )


def _make_hypothesis(
    status: HypothesisStatus = HypothesisStatus.ACTIVE,
    likelihood: float = 0.7,
    statement: str = "DNS cache stale",
    turn: int = 2,
) -> Hypothesis:
    """Create a Hypothesis object."""
    return Hypothesis(
        hypothesis_id=f"hyp_{uuid4().hex[:12]}",
        statement=statement,
        category=HypothesisCategory.CONFIG,
        status=status,
        likelihood=likelihood,
        generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
        rationale="DNS TTL expired",
        generated_at_turn=turn,
    )


def _make_uploaded_file(turn: int = 1, filename: str = "test.log") -> UploadedFile:
    """Create an UploadedFile object."""
    return UploadedFile(
        file_id=f"file_{uuid4().hex[:12]}",
        filename=filename,
        size_bytes=2048,
        data_type="log",
        uploaded_at_turn=turn,
        source_type="file_upload",
    )


# ============================================================
# INQUIRY Phase Tests
# ============================================================


class TestTransformInquiry:
    """Tests for INQUIRY → CaseUIResponse_Inquiry."""

    def test_basic_inquiry(self):
        case = _make_inquiry_case()
        result = transform_case_for_ui(case)

        assert isinstance(result, CaseUIResponse_Inquiry)
        assert result.status == CaseStatus.INQUIRY
        assert result.title == "DNS resolution failing"
        assert result.uploaded_files_count == 0

    def test_inquiry_with_uploaded_files(self):
        case = _make_inquiry_case()
        case.uploaded_files.append(_make_uploaded_file(turn=1, filename="auth.log"))
        case.uploaded_files.append(_make_uploaded_file(turn=2, filename="nginx.conf"))

        result = transform_case_for_ui(case)

        assert isinstance(result, CaseUIResponse_Inquiry)
        assert result.uploaded_files_count == 2

    def test_inquiry_with_problem_confirmation(self):
        case = _make_inquiry_case()
        case.inquiry.proposed_problem_statement = "DNS failing"
        case.inquiry.problem_confirmation = ProblemConfirmation(
            problem_type="unavailability",
            severity_guess="high",
            preliminary_guidance="Check DNS servers",
        )

        result = transform_case_for_ui(case)

        assert result.inquiry.proposed_problem_statement == "DNS failing"
        assert result.inquiry.problem_confirmation is not None
        assert result.inquiry.problem_confirmation["severity_guess"] == "high"

    def test_inquiry_null_inquiry_data(self):
        """Adapter handles None inquiry gracefully via defensive init."""
        case = _make_inquiry_case()
        # Bypass Pydantic validation to simulate corrupted state
        object.__setattr__(case, "inquiry", None)

        result = transform_case_for_ui(case)

        assert isinstance(result, CaseUIResponse_Inquiry)
        assert result.inquiry is not None

    def test_inquiry_valid_next_states(self):
        case = _make_inquiry_case()
        result = transform_case_for_ui(case)

        assert isinstance(result.valid_next_states, list)
        assert len(result.valid_next_states) > 0


# ============================================================
# INVESTIGATING Phase Tests
# ============================================================


class TestTransformInvestigating:
    """Tests for INVESTIGATING → CaseUIResponse_Investigating."""

    def test_basic_investigating(self):
        case = _make_investigating_case()
        result = transform_case_for_ui(case)

        assert isinstance(result, CaseUIResponse_Investigating)
        assert result.status == CaseStatus.INVESTIGATING
        assert result.title == "DNS resolution failing"

    def test_investigating_uploaded_files_count(self):
        """uploaded_files_count is populated from case.uploaded_files."""
        case = _make_investigating_case()
        case.uploaded_files.append(_make_uploaded_file(turn=1, filename="auth.log"))
        case.uploaded_files.append(_make_uploaded_file(turn=2, filename="syslog"))
        case.uploaded_files.append(_make_uploaded_file(turn=3, filename="metrics.csv"))

        result = transform_case_for_ui(case)

        assert result.uploaded_files_count == 3

    def test_investigating_zero_uploaded_files(self):
        """uploaded_files_count is 0 when no files uploaded."""
        case = _make_investigating_case()
        result = transform_case_for_ui(case)

        assert result.uploaded_files_count == 0

    def test_evidence_summary_has_collected_at_turn(self):
        """EvidenceSummary includes collected_at_turn from domain Evidence."""
        case = _make_investigating_case()
        case.evidence.append(_make_evidence(turn=3))
        case.evidence.append(_make_evidence(turn=5, summary="Config mismatch"))

        result = transform_case_for_ui(case)

        assert len(result.latest_evidence) == 2
        turns = {ev.collected_at_turn for ev in result.latest_evidence}
        assert turns == {3, 5}

    def test_evidence_summary_has_category(self):
        """EvidenceSummary includes category from domain Evidence."""
        case = _make_investigating_case()
        case.evidence.append(
            _make_evidence(turn=1, category=EvidenceCategory.SYMPTOM_EVIDENCE)
        )
        case.evidence.append(
            _make_evidence(
                turn=2,
                category=EvidenceCategory.CAUSAL_EVIDENCE,
                summary="Root cause found",
            )
        )

        result = transform_case_for_ui(case)

        categories = {ev.category for ev in result.latest_evidence}
        assert "symptom_evidence" in categories
        assert "causal_evidence" in categories

    def test_evidence_summary_category_is_string(self):
        """Category is serialized as string, not enum."""
        case = _make_investigating_case()
        case.evidence.append(_make_evidence(turn=1))

        result = transform_case_for_ui(case)

        assert isinstance(result.latest_evidence[0].category, str)

    def test_evidence_summary_type_field(self):
        """Evidence type is populated from source_type."""
        case = _make_investigating_case()
        case.evidence.append(
            _make_evidence(turn=1, source_type=EvidenceSourceType.METRICS)
        )

        result = transform_case_for_ui(case)

        assert result.latest_evidence[0].type == "metrics"

    def test_evidence_sorted_by_recency_limited_to_5(self):
        """Latest evidence returns most recent 5 items."""
        case = _make_investigating_case()
        for i in range(8):
            case.evidence.append(_make_evidence(turn=i + 1, summary=f"Evidence {i}"))

        result = transform_case_for_ui(case)

        assert len(result.latest_evidence) == 5
        # Should be sorted by collected_at descending (most recent first)
        turns = [ev.collected_at_turn for ev in result.latest_evidence]
        assert turns == sorted(turns, reverse=True)

    def test_hypothesis_summaries(self):
        case = _make_investigating_case()
        h1 = _make_hypothesis(status=HypothesisStatus.ACTIVE, likelihood=0.8)
        h2 = _make_hypothesis(
            status=HypothesisStatus.REFUTED,
            likelihood=0.3,
            statement="Network partition",
        )
        case.hypotheses[h1.hypothesis_id] = h1
        case.hypotheses[h2.hypothesis_id] = h2

        result = transform_case_for_ui(case)

        assert len(result.active_hypotheses) == 2
        # Sorted by likelihood descending
        assert (
            result.active_hypotheses[0].likelihood
            >= result.active_hypotheses[1].likelihood
        )

    def test_working_conclusion_from_best_hypothesis(self):
        case = _make_investigating_case()
        h1 = _make_hypothesis(likelihood=0.6, statement="DNS cache stale")
        h2 = _make_hypothesis(likelihood=0.9, statement="Upstream config drift")
        case.hypotheses[h1.hypothesis_id] = h1
        case.hypotheses[h2.hypothesis_id] = h2

        result = transform_case_for_ui(case)

        assert result.working_conclusion is not None
        assert result.working_conclusion.summary == "Upstream config drift"
        assert result.working_conclusion.confidence == 0.9

    def test_no_working_conclusion_without_hypotheses(self):
        case = _make_investigating_case()
        result = transform_case_for_ui(case)

        assert result.working_conclusion is None

    def test_progress_summary(self):
        case = _make_investigating_case()
        case.progress.symptom_verified = True
        case.progress.scope_assessed = True

        result = transform_case_for_ui(case)

        assert "symptom_verified" in result.progress.completed_indicators
        assert "scope_assessed" in result.progress.completed_indicators
        assert result.progress.total_evidence == 0

    def test_progress_total_evidence_count(self):
        case = _make_investigating_case()
        case.evidence.append(_make_evidence(turn=1))
        case.evidence.append(_make_evidence(turn=2))

        result = transform_case_for_ui(case)

        assert result.progress.total_evidence == 2

    def test_investigation_strategy_mitigation_first(self):
        case = _make_investigating_case()
        case.path_selection = PathSelection(
            path=InvestigationPath.MITIGATION_FIRST,
            auto_selected=True,
            rationale="Ongoing critical issue",
        )

        result = transform_case_for_ui(case)

        assert result.investigation_strategy is not None
        assert "mitigation" in result.investigation_strategy.approach.lower()

    def test_is_stuck_flag(self):
        case = _make_investigating_case()
        case.turns_without_progress = 6  # > 5 triggers stuck

        result = transform_case_for_ui(case)

        assert result.is_stuck is True
        assert "stuck" in result.agent_status.lower()


# ============================================================
# RESOLVED Phase Tests
# ============================================================


class TestTransformResolved:
    """Tests for RESOLVED → CaseUIResponse_Resolved."""

    def test_basic_resolved(self):
        case = _make_resolved_case()
        case.root_cause_conclusion = RootCauseConclusion(
            root_cause="DNS config drift",
            confidence_level=ConfidenceLevel.CONFIDENT,
            likelihood=0.85,
            mechanism="Upstream DNS changed TTL",
        )

        result = transform_case_for_ui(case)

        assert isinstance(result, CaseUIResponse_Resolved)
        assert result.status == CaseStatus.RESOLVED
        assert result.root_cause.description == "DNS config drift"

    def test_resolved_uploaded_files_count(self):
        """uploaded_files_count is populated on resolved response."""
        case = _make_resolved_case()
        case.uploaded_files.append(_make_uploaded_file(turn=1))
        case.uploaded_files.append(_make_uploaded_file(turn=3))

        result = transform_case_for_ui(case)

        assert result.uploaded_files_count == 2

    def test_resolved_zero_uploaded_files(self):
        case = _make_resolved_case()
        result = transform_case_for_ui(case)

        assert result.uploaded_files_count == 0

    def test_resolved_with_solution(self):
        case = _make_resolved_case()
        case.solutions.append(
            Solution(
                solution_id=f"sol_{uuid4().hex[:12]}",
                solution_type=SolutionType.CONFIG_CHANGE,
                title="Fix DNS config",
                immediate_action="Reverted upstream DNS TTL",
                applied_at=datetime.now(timezone.utc),
            )
        )

        result = transform_case_for_ui(case)

        assert "Reverted" in result.solution_applied.description

    def test_resolved_duration_calculation(self):
        case = _make_resolved_case()
        resolved_at = case.created_at + timedelta(hours=2, minutes=15)
        object.__setattr__(case, "resolved_at", resolved_at)
        # closed_at must be >= resolved_at
        object.__setattr__(case, "closed_at", resolved_at)

        result = transform_case_for_ui(case)

        assert result.resolution_summary.total_duration_minutes == 135  # 2h15m

    def test_resolved_reports_available(self):
        case = _make_resolved_case()

        result = transform_case_for_ui(case)

        assert len(result.reports_available) > 0
        report_types = [r.report_type for r in result.reports_available]
        assert "incident_report" in report_types

    def test_resolved_root_cause_from_validated_hypothesis(self):
        case = _make_resolved_case()
        case.root_cause_conclusion = RootCauseConclusion(
            root_cause="Config drift",
            confidence_level=ConfidenceLevel.VERIFIED,
            likelihood=0.95,
            mechanism="TTL changed",
        )
        hyp = _make_hypothesis(status=HypothesisStatus.VALIDATED, likelihood=0.95)
        case.hypotheses[hyp.hypothesis_id] = hyp

        result = transform_case_for_ui(case)

        assert result.root_cause.root_cause_id == hyp.hypothesis_id
        assert result.root_cause.category == "config"


# ============================================================
# CLOSED Phase Tests
# ============================================================


class TestTransformClosed:
    """Tests for CLOSED → CaseUIResponse_Resolved (with CLOSED status)."""

    def test_closed_returns_resolved_format(self):
        case = _make_closed_case()

        result = transform_case_for_ui(case)

        assert isinstance(result, CaseUIResponse_Resolved)
        assert result.status == CaseStatus.CLOSED

    def test_closed_uploaded_files_count(self):
        case = _make_closed_case()
        case.uploaded_files.append(_make_uploaded_file(turn=1))

        result = transform_case_for_ui(case)

        assert result.uploaded_files_count == 1


# ============================================================
# Serialization / JSON Output Tests
# ============================================================


class TestSerialization:
    """Tests that new fields appear in serialized JSON output."""

    def test_evidence_summary_json_has_new_fields(self):
        """Serialized evidence includes collected_at_turn and category."""
        case = _make_investigating_case()
        case.evidence.append(
            _make_evidence(
                turn=4,
                category=EvidenceCategory.CAUSAL_EVIDENCE,
                source_type=EvidenceSourceType.CONFIGURATION,
            )
        )

        result = transform_case_for_ui(case)
        json_data = result.model_dump()

        ev = json_data["latest_evidence"][0]
        assert "collected_at_turn" in ev
        assert ev["collected_at_turn"] == 4
        assert "category" in ev
        assert ev["category"] == "causal_evidence"

    def test_investigating_json_has_uploaded_files_count(self):
        """Serialized investigating response includes uploaded_files_count."""
        case = _make_investigating_case()
        case.uploaded_files.append(_make_uploaded_file())

        result = transform_case_for_ui(case)
        json_data = result.model_dump()

        assert "uploaded_files_count" in json_data
        assert json_data["uploaded_files_count"] == 1

    def test_resolved_json_has_uploaded_files_count(self):
        """Serialized resolved response includes uploaded_files_count."""
        case = _make_resolved_case()
        case.uploaded_files.append(_make_uploaded_file())
        case.uploaded_files.append(_make_uploaded_file(filename="metrics.csv"))

        result = transform_case_for_ui(case)
        json_data = result.model_dump()

        assert "uploaded_files_count" in json_data
        assert json_data["uploaded_files_count"] == 2

    def test_evidence_summary_defaults(self):
        """EvidenceSummary fields have sensible defaults for backward compat."""
        from faultmaven.models.case_ui import EvidenceSummary

        ev = EvidenceSummary(
            evidence_id="ev_test",
            type="log_file",
            summary="Test",
            timestamp=datetime.now(timezone.utc),
            relevance_score=0.5,
        )

        assert ev.collected_at_turn == 0
        assert ev.category == "OTHER"
