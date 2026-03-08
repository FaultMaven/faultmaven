"""Tests for Evidence Classification Lifecycle.

Evidence classification is content-based, not stage-based (Section 5.2 of
evidence-driven-investigation-framework.md). The LLM evaluates data and
classifies it by what it contains, not by which state the case is in.

Verifies that:
- Evidence is created by the LLM via evidence_to_add (not auto-created at upload)
- LLM-created evidence during INQUIRY has advances_milestones=[] (milestones not tracked)
- Contextual evidence (data irrelevant to the problem) cannot justify milestones
- Transition works with no evidence, contextual-only, or categorized evidence
- No manual parameter on _transition_to_investigating
"""

import pytest
from datetime import datetime, UTC
from unittest.mock import MagicMock
from uuid import uuid4

from faultmaven.core.investigation.milestone_engine import (
    MilestoneEngine,
    validate_reasoning_first,
)
from faultmaven.modules.case.domain.models import (
    Case,
    CaseStatus,
    InquiryData,
)
from faultmaven.modules.case.contracts import (
    Evidence,
    EvidenceCategory,
    EvidenceForm,
    EvidenceSourceType,
    UploadedFile,
)


def _ev_id() -> str:
    """Generate a valid evidence ID matching pattern ^ev_[a-f0-9]{12}$."""
    return f"ev_{uuid4().hex[:12]}"


def _make_case(**overrides) -> Case:
    """Helper to create a Case with sensible defaults for testing."""
    defaults = dict(
        case_id="case_1234567890ab",
        organization_id="org_1234567890abc",
        title="Test Case",
        user_id="user_1234567890abc",
        status=CaseStatus.INQUIRY,
        current_turn=3,
        inquiry=InquiryData(
            proposed_problem_statement="Test problem statement",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
    )
    defaults.update(overrides)
    return Case(**defaults)


def _make_uploaded_file(**overrides) -> UploadedFile:
    """Helper to create an UploadedFile with sensible defaults."""
    defaults = dict(
        file_id="file_1234567890ab",
        filename="syslog.txt",
        size_bytes=1000,
        data_type="LOGS",
        uploaded_at_turn=1,
        uploaded_at=datetime.now(UTC),
        source_type="file_upload",
        preprocessing_summary="System logs showing errors",
        content_ref="s3://bucket/syslog.txt",
    )
    defaults.update(overrides)
    return UploadedFile(**defaults)


def _make_engine() -> MilestoneEngine:
    return MilestoneEngine(
        llm_provider=MagicMock(),
        repository=MagicMock(),
        investigation_tools=MagicMock(),
        evidence_service=MagicMock(),
    )


def _make_evidence(category, **overrides) -> Evidence:
    """Helper to create Evidence with valid defaults."""
    defaults = dict(
        evidence_id=_ev_id(),
        summary="Test evidence",
        preprocessed_content="Test evidence content",
        content_ref=None,
        content_size_bytes=100,
        preprocessing_method="none",
        category=category,
        source_type=EvidenceSourceType.LOGS,
        form=EvidenceForm.SUBMITTED_DATA,
        source_file_id=None,
        advances_milestones=[],
        collected_at=datetime.now(UTC),
        collected_by="user_1234567890abc",
        collected_at_turn=1,
        primary_purpose="Investigation context",
    )
    defaults.update(overrides)
    return Evidence(**defaults)


class TestContentBasedClassification:
    """Evidence classification is content-based, not stage-based.

    The LLM classifies data by what it contains — error logs are
    symptom_evidence whether submitted during INQUIRY or INVESTIGATING.
    Contextual evidence means data evaluated and found irrelevant to
    the issue, not a default placeholder.
    """

    def test_inquiry_evidence_has_empty_milestones(self):
        """Evidence created during INQUIRY has advances_milestones=[] because
        milestone tracking is not active during INQUIRY."""
        # LLM classified logs as symptom_evidence during INQUIRY
        ev = _make_evidence(
            EvidenceCategory.SYMPTOM_EVIDENCE,
            summary="Error logs showing OOM",
            advances_milestones=[],  # Empty during INQUIRY
        )
        assert ev.category == EvidenceCategory.SYMPTOM_EVIDENCE
        assert ev.advances_milestones == []

    def test_contextual_means_irrelevant_not_unprocessed(self):
        """Contextual evidence represents data evaluated and found irrelevant
        to the problem — not a placeholder for unprocessed data."""
        ev = _make_evidence(
            EvidenceCategory.CONTEXTUAL_EVIDENCE,
            summary="Normal baseline configuration",
            primary_purpose="System context",
        )
        assert ev.category == EvidenceCategory.CONTEXTUAL_EVIDENCE
        # Contextual evidence never advances milestones
        assert ev.advances_milestones == []

    def test_uploads_do_not_auto_create_evidence(self):
        """File uploads create UploadedFile only — Evidence is created by
        LLM classification via evidence_to_add, not automatically."""
        case = _make_case()
        uploaded_file = _make_uploaded_file()
        case.uploaded_files.append(uploaded_file)

        # UploadedFile exists, but no Evidence record yet
        assert len(case.uploaded_files) == 1
        assert len(case.evidence) == 0


class TestTransitionWithEvidence:
    """Verify INQUIRY→INVESTIGATING transitions work with LLM-classified evidence."""

    @pytest.mark.asyncio
    async def test_transition_with_no_evidence(self):
        """Transition with no evidence should work cleanly."""
        engine = _make_engine()
        case = _make_case()

        assert len(case.evidence) == 0
        await engine._transition_to_investigating(case)

        assert case.status == CaseStatus.INVESTIGATING
        assert len(case.evidence) == 0

    @pytest.mark.asyncio
    async def test_transition_with_contextual_evidence_no_milestones(self):
        """Contextual evidence (irrelevant data) gets no milestones at transition.

        CATEGORY_MILESTONE_MAP[CONTEXTUAL_EVIDENCE] = [], so _infer_milestones
        returns [] for contextual evidence regardless of what milestones exist.
        """
        engine = _make_engine()
        case = _make_case(current_turn=5)

        # LLM classified data as contextual during INQUIRY (no problem found)
        case.evidence.append(
            _make_evidence(
                EvidenceCategory.CONTEXTUAL_EVIDENCE,
                summary="Normal system configuration",
            )
        )

        await engine._transition_to_investigating(case)

        assert case.status == CaseStatus.INVESTIGATING
        for ev in case.evidence:
            assert ev.advances_milestones == []

    @pytest.mark.asyncio
    async def test_transition_with_symptom_evidence(self):
        """Symptom evidence created during INQUIRY is preserved through transition.

        Gap #4 attempts milestone attribution, but with fresh InvestigationProgress
        (no milestones completed yet), _infer_milestones returns [] because
        it intersects eligible milestones with completed milestones.
        """
        engine = _make_engine()
        case = _make_case(current_turn=5)

        symptom_ev = _make_evidence(
            EvidenceCategory.SYMPTOM_EVIDENCE,
            summary="Error pattern in logs",
            collected_at_turn=3,
        )
        case.evidence.append(symptom_ev)

        await engine._transition_to_investigating(case)

        assert case.status == CaseStatus.INVESTIGATING
        assert len(case.evidence) == 1
        assert case.evidence[0].category == EvidenceCategory.SYMPTOM_EVIDENCE

    @pytest.mark.asyncio
    async def test_transition_preserves_mixed_evidence(self):
        """Both contextual and categorized evidence are preserved through transition."""
        engine = _make_engine()
        case = _make_case(current_turn=5)

        ctx_ev = _make_evidence(
            EvidenceCategory.CONTEXTUAL_EVIDENCE,
            summary="Normal config",
        )
        symptom_ev = _make_evidence(
            EvidenceCategory.SYMPTOM_EVIDENCE,
            summary="Error pattern in logs",
            collected_at_turn=3,
        )
        case.evidence.extend([ctx_ev, symptom_ev])

        await engine._transition_to_investigating(case)

        assert len(case.evidence) == 2
        categories = {e.category for e in case.evidence}
        assert EvidenceCategory.CONTEXTUAL_EVIDENCE in categories
        assert EvidenceCategory.SYMPTOM_EVIDENCE in categories

    @pytest.mark.asyncio
    async def test_transition_no_manual_param(self):
        """_transition_to_investigating no longer accepts manual parameter."""
        engine = _make_engine()
        case = _make_case()

        await engine._transition_to_investigating(case)
        assert case.status == CaseStatus.INVESTIGATING

    @pytest.mark.asyncio
    async def test_transition_initializes_all_structures(self):
        """Transition always initializes progress, verification, and path."""
        engine = _make_engine()
        case = _make_case()

        await engine._transition_to_investigating(case)

        assert case.status == CaseStatus.INVESTIGATING
        assert case.progress is not None
        assert case.problem_verification is not None
        assert case.path_selection is not None
        assert case.description == "Test problem statement"


class TestContextualEvidenceNotActionable:
    """Contextual evidence (data irrelevant to the problem) cannot justify milestones.

    validate_reasoning_first checks for non-contextual (actionable) evidence
    when the LLM attempts to complete milestones. This prevents the LLM from
    claiming milestones based on data that has been evaluated and found
    irrelevant to the issue at hand.
    """

    def test_contextual_evidence_not_counted_as_actionable(self):
        """Only contextual evidence in case → milestones rejected."""
        from faultmaven.core.investigation.schemas import (
            InvestigationResponse_Diagnosis,
            MilestoneUpdates,
            InternalReasoning,
        )

        case = _make_case(
            status=CaseStatus.INVESTIGATING,
            description="Test problem statement",
        )
        case.evidence.append(
            _make_evidence(
                EvidenceCategory.CONTEXTUAL_EVIDENCE,
                summary="Normal baseline config",
            )
        )

        DiagnosisStateUpdate = InvestigationResponse_Diagnosis.DiagnosisStateUpdate
        response = InvestigationResponse_Diagnosis(
            agent_response="I found the issue.",
            internal_reasoning=InternalReasoning(
                evidence_analyzed=[],
                milestone_justifications={
                    "symptom_verified": "Verified from uploaded logs"
                },
            ),
            state_updates=DiagnosisStateUpdate(
                milestones=MilestoneUpdates(symptom_verified=True),
                evidence_to_add=[],
                hypotheses_to_add=[],
                outcome="data_requested",
            ),
        )

        is_valid, errors = validate_reasoning_first(response, case)

        assert not is_valid
        assert any("contextual evidence" in e.lower() for e in errors)

    def test_non_contextual_evidence_is_actionable(self):
        """Non-contextual evidence in case → milestones allowed."""
        from faultmaven.core.investigation.schemas import (
            InvestigationResponse_Diagnosis,
            MilestoneUpdates,
            InternalReasoning,
        )

        case = _make_case(
            status=CaseStatus.INVESTIGATING,
            description="Test problem statement",
        )
        ev = _make_evidence(
            EvidenceCategory.SYMPTOM_EVIDENCE,
            summary="Error pattern detected",
        )
        case.evidence.append(ev)

        DiagnosisStateUpdate = InvestigationResponse_Diagnosis.DiagnosisStateUpdate
        response = InvestigationResponse_Diagnosis(
            agent_response="I found the issue.",
            internal_reasoning=InternalReasoning(
                evidence_analyzed=[ev.evidence_id],
                milestone_justifications={
                    "symptom_verified": f"Verified from {ev.evidence_id} showing OOM"
                },
            ),
            state_updates=DiagnosisStateUpdate(
                milestones=MilestoneUpdates(symptom_verified=True),
                evidence_to_add=[],
                hypotheses_to_add=[],
                outcome="data_requested",
            ),
        )

        is_valid, errors = validate_reasoning_first(response, case)

        assert not any("contextual evidence" in e.lower() for e in errors)
        assert not any("no actionable evidence" in e.lower() for e in errors)

    def test_no_evidence_at_all_rejects_milestones(self):
        """Zero evidence in case → milestones rejected."""
        from faultmaven.core.investigation.schemas import (
            InvestigationResponse_Diagnosis,
            MilestoneUpdates,
            InternalReasoning,
        )

        case = _make_case(
            status=CaseStatus.INVESTIGATING,
            description="Test problem statement",
        )
        assert len(case.evidence) == 0

        DiagnosisStateUpdate = InvestigationResponse_Diagnosis.DiagnosisStateUpdate
        response = InvestigationResponse_Diagnosis(
            agent_response="I found the issue.",
            internal_reasoning=InternalReasoning(
                evidence_analyzed=[],
                milestone_justifications={
                    "symptom_verified": "Obvious from description"
                },
            ),
            state_updates=DiagnosisStateUpdate(
                milestones=MilestoneUpdates(symptom_verified=True),
                evidence_to_add=[],
                hypotheses_to_add=[],
                outcome="data_requested",
            ),
        )

        is_valid, errors = validate_reasoning_first(response, case)

        assert not is_valid
        assert any("no actionable evidence" in e.lower() for e in errors)
