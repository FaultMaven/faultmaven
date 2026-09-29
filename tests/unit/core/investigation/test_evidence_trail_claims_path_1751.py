"""A milestone backed only by an earlier turn's evidence stands when the
evidence trail cites that turn (fm#1751).

``ResponseApplier`` reviews every milestone the model claims against the
case's evidence (``validate_milestone_claims``). Current-turn evidence counts
on its own; evidence from an earlier turn counts only when the model cites the
turn as ``turn_N`` in ``evidence_trail.evidence_analyzed``. The applier reads
that list off the response by attribute, and a ``getattr`` of a name the schema
no longer has returns ``None`` without complaint — every historical citation
then reads as absent and the milestone is reverted.

Driven through ``ResponseApplier.process_response_structured``, the path that
runs the review, not by calling ``validate_milestone_claims`` directly: the
defect this guards against lives in the applier's read, not in the validator.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from faultmaven.core.investigation.milestone_engine.dependencies import EngineDeps
from faultmaven.core.investigation.milestone_engine.response_application import (
    ResponseApplier,
)
from faultmaven.core.investigation.schemas import (
    EvidenceTrail,
    InvestigationResponse_Diagnosis,
    MilestoneJustifications,
    MilestoneUpdates,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    InquiryData,
    ProblemVerification,
)

pytestmark = pytest.mark.unit

#: The turn the claim is made on, and the earlier turn the evidence came from.
CURRENT_TURN = 3
EVIDENCE_TURN = 1


def _case() -> Case:
    """INVESTIGATING at turn 3; its only symptom evidence arrived on turn 1."""
    case = Case(
        case_id="case_aaaaaaaa1751",
        title="Latency regression",
        state=CaseState.INVESTIGATING,
        user_id="user_123",
        enterprise_id="org_123",
        description="p95 went from 200ms to 4s after the deploy",
        problem_verification=ProblemVerification(
            symptom_statement="p95 latency 4s since the deploy",
            severity="HIGH",
            temporal_state="ongoing",
            urgency_level="high",
        ),
        inquiry=InquiryData(
            problem_statement_confirmed=True,
            thread_id="thread_123",
            proposed_problem_statement="p95 latency 4s since the deploy",
        ),
    )
    case.current_turn = CURRENT_TURN
    case.evidence.append(
        Evidence(
            summary="checkout-api log: requests at 3.9-4.1s",
            category=EvidenceCategory.SYMPTOM_EVIDENCE,
            source_type=EvidenceSourceType.USER_DESCRIPTION,
            collected_at=datetime.now(UTC),
            collected_by="user_123",
            primary_purpose="Symptom verification",
            preprocessed_content="GET /api/cart 200 3941ms",
            content_size_bytes=24,
            preprocessing_method="manual",
            collected_at_turn=EVIDENCE_TURN,
        )
    )
    return case


def _claim(evidence_analyzed: list[str]) -> InvestigationResponse_Diagnosis:
    """``symptom_verified=True``, justified, citing ``evidence_analyzed``."""
    return InvestigationResponse_Diagnosis(
        agent_response="The turn-1 log confirms the latency.",
        evidence_trail=EvidenceTrail(
            evidence_analyzed=evidence_analyzed,
            milestone_justifications=MilestoneJustifications(
                symptom_verified="Requests at 3.9-4.1s in the turn-1 log"
            ),
        ),
        state_updates=InvestigationResponse_Diagnosis.DiagnosisStateUpdate(
            milestones=MilestoneUpdates(symptom_verified=True)
        ),
    )


async def _apply(response: InvestigationResponse_Diagnosis):
    applier = ResponseApplier(deps=EngineDeps(), kb_prefetcher=None)
    return await applier.process_response_structured(
        _case(), "the latency is still there", response
    )


async def test_a_claim_citing_the_evidence_turn_stands():
    case, metadata = await _apply(_claim([f"turn_{EVIDENCE_TURN}"]))

    assert case.progress.symptom_verified is True
    assert "symptom_verified" in metadata["milestones_completed"]
    assert not metadata.get("milestone_validation_warnings")


async def test_the_same_claim_without_the_citation_is_reverted():
    """The control: the review does revert a claim with no evidence in reach,
    so the test above passes because the citation was read."""
    case, metadata = await _apply(_claim([]))

    assert case.progress.symptom_verified is False
    assert "symptom_verified" not in metadata["milestones_completed"]
    assert metadata["milestone_validation_warnings"]
