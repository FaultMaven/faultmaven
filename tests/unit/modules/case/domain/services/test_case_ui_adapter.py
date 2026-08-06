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
    CaseState,
    ConfidenceLevel,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisState,
    InquiryData,
    ProblemConfirmation,
    RootCauseConclusion,
    Solution,
    SolutionType,
    UploadedFile,
)
from faultmaven.modules.case.domain.services.case_ui_adapter import (
    transform_case_for_ui,
)

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
    case.state = CaseState.INVESTIGATING

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
    # closure_reason is None for RESOLVED — sub-categorization would be
    # redundant with the status itself.
    object.__setattr__(case, "closure_reason", None)
    object.__setattr__(case, "state", CaseState.RESOLVED)
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
    object.__setattr__(case, "closure_reason", "closed_insufficient_evidence")
    object.__setattr__(case, "state", CaseState.CLOSED)
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
        extract="2025-01-01 ERROR: timeout",
        source_type=source_type,
        source_file_id="file_aabb12345678",
        collected_by="test-user",
        collected_at_turn=turn,
    )


def _make_hypothesis(
    state: HypothesisState = HypothesisState.ACTIVE,
    likelihood: float = 0.7,
    statement: str = "DNS cache stale",
    turn: int = 2,
    refutation_reason: str | None = None,
) -> Hypothesis:
    """Create a Hypothesis object.

    When ``state=REFUTED``, a refutation_reason is required by the domain
    invariant; callers should pass one or accept the default placeholder.
    """
    if state == HypothesisState.REFUTED and refutation_reason is None:
        refutation_reason = "test refutation reason"
    return Hypothesis(
        hypothesis_id=f"hyp_{uuid4().hex[:12]}",
        statement=statement,
        category=HypothesisCategory.CONFIG,
        state=state,
        likelihood=likelihood,
        generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
        rationale="DNS TTL expired",
        generated_at_turn=turn,
        refutation_reason=refutation_reason,
    )


def _make_uploaded_file(turn: int = 1, filename: str = "test.log") -> UploadedFile:
    """Create an UploadedFile object."""
    return UploadedFile(
        file_id=f"file_{uuid4().hex[:12]}",
        filename=filename,
        size_bytes=2048,
        uploaded_at_turn=turn,
        upload_source="file_upload",
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
        assert result.state == CaseState.INQUIRY
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
        assert result.state == CaseState.INVESTIGATING
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
        h1 = _make_hypothesis(state=HypothesisState.ACTIVE, likelihood=0.8)
        h2 = _make_hypothesis(
            state=HypothesisState.REFUTED,
            likelihood=0.3,
            statement="Network partition",
            refutation_reason="packet capture shows consistent connectivity",
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

        result = transform_case_for_ui(case)

        assert "symptom_verified" in result.progress.completed_indicators
        assert result.progress.total_evidence == 0

    def test_progress_total_evidence_count(self):
        case = _make_investigating_case()
        case.evidence.append(_make_evidence(turn=1))
        case.evidence.append(_make_evidence(turn=2))

        result = transform_case_for_ui(case)

        assert result.progress.total_evidence == 2

    def test_progress_transparency_when_stalled(self):
        from datetime import datetime, timezone

        from faultmaven.modules.case.domain.models import (
            TurnOutcome,
            TurnProgress,
            VerificationStatus,
        )

        case = _make_investigating_case()
        # Phase 3: the persisted verification status is surfaced alongside the
        # stalled-milestone info so the frontend can show the honest partial.
        case.progress.verification_status = VerificationStatus.INSUFFICIENT_EVIDENCE
        # Add 5+ investigative turns without milestones to trigger transparency
        case.turn_history = [
            TurnProgress(
                turn_number=i,
                timestamp=datetime.now(timezone.utc),
                milestones_completed=[],
                evidence_added=[f"ev_{i}"] if i % 2 == 0 else [],
                hypotheses_generated=[],
                hypotheses_validated=[],
                solutions_proposed=[],
                progress_made=False,
                outcome=(
                    TurnOutcome.DATA_PROVIDED
                    if i % 2 == 0
                    else TurnOutcome.DATA_REQUESTED
                ),
                user_message_summary="test",
                agent_response_summary="test",
            )
            for i in range(6)
        ]

        result = transform_case_for_ui(case)

        assert result.progress_transparency is not None
        assert result.progress_transparency.active is True
        assert result.progress_transparency.pending_milestone is not None
        assert (
            result.progress_transparency.verification_status == "insufficient_evidence"
        )

    def test_progress_transparency_carries_cause_assurance(self):
        from datetime import datetime, timezone

        from faultmaven.modules.case.domain.models import (
            CauseAssuranceGrade,
            TurnOutcome,
            TurnProgress,
        )

        case = _make_investigating_case()
        # The persisted assurance grade rides the same surfacing object so the
        # dashboard can label a lower-assurance (unconfirmed) conclusion.
        case.progress.cause_assurance = CauseAssuranceGrade.MECHANISTIC
        case.turn_history = [
            TurnProgress(
                turn_number=i,
                timestamp=datetime.now(timezone.utc),
                milestones_completed=[],
                evidence_added=[f"ev_{i}"] if i % 2 == 0 else [],
                hypotheses_generated=[],
                hypotheses_validated=[],
                solutions_proposed=[],
                progress_made=False,
                outcome=(
                    TurnOutcome.DATA_PROVIDED
                    if i % 2 == 0
                    else TurnOutcome.DATA_REQUESTED
                ),
                user_message_summary="test",
                agent_response_summary="test",
            )
            for i in range(6)
        ]

        result = transform_case_for_ui(case)

        assert result.progress_transparency is not None
        assert result.progress_transparency.cause_assurance == "mechanistic"

    def test_progress_transparency_advances_past_identified_cause(self):
        """#675 regression on the user-facing path: once cause_state=IDENTIFIED,
        the surfaced pending milestone advances to solution_proposed — not stuck
        on root_cause_identified. The old getattr(progress, milestone_name)
        returned False for the INV-35-removed boolean and reported it perpetually
        pending in the CaseUIResponse."""
        from datetime import datetime, timezone

        from faultmaven.modules.case.domain.models import (
            CauseState,
            InvestigationProgress,
            TurnOutcome,
            TurnProgress,
        )

        case = _make_investigating_case()
        # symptom verified AND cause identified (engine-derived); only
        # solution_proposed remains in DIAGNOSIS.
        case.progress = InvestigationProgress(
            symptom_verified=True,
            cause_state=CauseState.IDENTIFIED,
            root_cause_likelihood=0.7,
            root_cause_method="hypothesis_validation",
        )
        case.turn_history = [
            TurnProgress(
                turn_number=i,
                timestamp=datetime.now(timezone.utc),
                milestones_completed=[],
                evidence_added=[f"ev_{i}"] if i % 2 == 0 else [],
                hypotheses_generated=[],
                hypotheses_validated=[],
                solutions_proposed=[],
                progress_made=False,
                outcome=(
                    TurnOutcome.DATA_PROVIDED
                    if i % 2 == 0
                    else TurnOutcome.DATA_REQUESTED
                ),
                user_message_summary="test",
                agent_response_summary="test",
            )
            for i in range(6)
        ]

        result = transform_case_for_ui(case)

        assert result.progress_transparency is not None
        assert result.progress_transparency.pending_milestone == "solution_proposed"


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
        assert result.state == CaseState.RESOLVED
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
        assert "resolution_summary" in report_types

    def test_resolved_root_cause_from_validated_hypothesis(self):
        case = _make_resolved_case()
        case.root_cause_conclusion = RootCauseConclusion(
            root_cause="Config drift",
            confidence_level=ConfidenceLevel.VERIFIED,
            likelihood=0.95,
            mechanism="TTL changed",
        )
        hyp = _make_hypothesis(state=HypothesisState.VALIDATED, likelihood=0.95)
        case.hypotheses[hyp.hypothesis_id] = hyp

        result = transform_case_for_ui(case)

        assert result.root_cause.root_cause_id == hyp.hypothesis_id
        assert result.root_cause.category == "config"

    def test_resolved_root_cause_category_from_rcc_named_hypothesis(self):
        # #695 Defect A item 2: after removing the flat-VALIDATED remnant, a
        # conclusion that NAMED its cause (validated_hypothesis_id) must still
        # label the UI category even when no hypothesis is in the VALIDATED state
        # (e.g. a MECHANISTIC grade) — the RCC-named fallback, not silently None.
        case = _make_resolved_case()
        hyp = _make_hypothesis(state=HypothesisState.ACTIVE, likelihood=0.8)
        case.hypotheses[hyp.hypothesis_id] = hyp
        case.root_cause_conclusion = RootCauseConclusion(
            root_cause="Config drift",
            confidence_level=ConfidenceLevel.VERIFIED,
            likelihood=0.95,
            mechanism="TTL changed",
            validated_hypothesis_id=hyp.hypothesis_id,
        )

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
        assert result.state == CaseState.CLOSED

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
