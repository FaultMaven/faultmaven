"""The engine's RESOLVED backstop (INV-43), observed through a REAL turn.

The bar for RESOLVED — a qualifying ``causal_absence_evidence`` row, the cause
confirmed gone — was well covered by ``test_resolution_causal_absence_gate.py``,
which pins what ``assess_resolution_readiness`` DECIDES. What nothing pinned is
who ASKS. Before this, the RESOLVED handshake had three openers (the model's
``proposed_transition``, the user's own request, and the DEFERRED-feasibility
proposer), so the ordinary shape — fix applied in session, user confirms it
worked, the model records the confirmation row and omits the transition the
COMPLETION prompt tells it to co-emit — reached no opener at all. The case read
READY, ``disposition_eligibility.resolved`` persisted ``ready``, and the turn
shipped nothing the user could click.

These drive ``MilestoneEngine.process_turn`` end to end and assert on the wire
payload, for the same reason its deferred-disposition sibling does: the defect
is about what the USER ends up reading. Only the LLM seam is replaced, and it
returns a REAL schema instance so every downstream stage sees genuine typed
objects.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.cause_assurance import ENGINE_EVIDENCE_AUTHOR
from faultmaven.core.investigation.milestone_engine import MilestoneEngine
from faultmaven.core.investigation.schemas import InvestigationResponse_Diagnosis
from faultmaven.core.investigation.terminal_transitions import (
    ResolutionReadiness,
    assess_resolution_readiness,
    derive_disposition_eligibility,
)
from faultmaven.modules.case.domain.models import (
    Case,
    CaseState,
    ConfidenceLevel,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    InvestigationProgress,
    ProblemVerification,
    RootCauseConclusion,
    Solution,
    SolutionType,
)

LLM_ANALYSIS = (
    "The projected audience now matches the provider's registered client ID, "
    "and the pods are obtaining credentials again."
)

RESOLVE_LABEL = "Yes, mark as resolved"
DECLINE_LABEL = "Not yet, continue investigating"


def _make_repo():
    repo = MagicMock()
    repo.save = AsyncMock(side_effect=lambda c: c)
    repo.get = AsyncMock(side_effect=lambda cid: None)
    return repo


def _engine(response):
    engine = MilestoneEngine(MagicMock(), _make_repo(), investigation_tools=MagicMock())
    engine._generate_structured_output = AsyncMock(return_value=response)
    return engine


def _silent_response():
    """A REAL response that proposes NOTHING.

    No ``proposed_transition``, no milestone claims — the compliance shape the
    backstop exists for. ``solution_feasible`` stays at its ``NOW`` default, so
    the deferred proposer is not what fires in any of these tests.
    """
    return InvestigationResponse_Diagnosis(
        agent_response=LLM_ANALYSIS, state_updates={}
    )


def _absence_row(*, category, collected_by="user", turn=1) -> Evidence:
    return Evidence(
        category=category,
        primary_purpose="record the post-fix outcome",
        summary=(
            "After the provider client-ID correction the pods obtained "
            "credentials and the AssumeRole failures stopped."
        ),
        source_type=EvidenceSourceType.USER_DESCRIPTION,
        collected_by=collected_by,
        collected_at_turn=turn,
    )


def _case(*, absence: EvidenceCategory | None, collected_by="user") -> Case:
    """An INVESTIGATING case with a cause on record, parameterized on the
    post-fix row — which is the only thing separating a resolution from a
    stabilization."""
    case = Case(
        title="Cross-account AssumeRole failures",
        enterprise_id="org_test",
        user_id="user_test",
        description="data-processor pods cannot assume the cross-account role",
        problem_verification=ProblemVerification(
            symptom_statement="AssumeRoleWithWebIdentity fails for the pods",
            severity="HIGH",
            temporal_state="ongoing",
            urgency_level="high",
        ),
    )
    case.inquiry.problem_statement_confirmed = True
    case.inquiry.problem_statement_confirmed_at = datetime.now(UTC)
    case.state = CaseState.INVESTIGATING
    case.progress = InvestigationProgress()
    case.progress.symptom_verified = True
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause=(
            "The IAM OIDC provider is registered with client ID "
            "sts.amazonaws.com.cn while the projected token audience is "
            "sts.amazonaws.com."
        ),
        mechanism=(
            "AssumeRoleWithWebIdentity rejects the token because the audience "
            "does not match the provider's registered client ID."
        ),
        confidence_level=ConfidenceLevel.CONFIDENT,
        likelihood=0.85,
    )
    case.solutions = [
        Solution(
            solution_type=SolutionType.CONFIG_CHANGE,
            title="Correct the OIDC provider client ID",
            longterm_fix="Set the provider ClientIDList to sts.amazonaws.com.",
        )
    ]
    if absence is not None:
        case.evidence.append(_absence_row(category=absence, collected_by=collected_by))
    return case


def _confirmed_case() -> Case:
    return _case(absence=EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE)


@pytest.mark.asyncio
async def test_confirmed_case_is_offered_resolve_when_the_model_stays_silent():
    """The gap itself: READY, and nothing had asked."""
    case = _confirmed_case()
    # Premise of the whole test — assert it rather than assume it, so a
    # re-scoping of the readiness gate fails here instead of silently turning
    # every assertion below into a test of the empty case.
    assert assess_resolution_readiness(case).verdict == ResolutionReadiness.READY
    assert derive_disposition_eligibility(case)["resolved"] == "ready"

    result = await _engine(_silent_response()).process_turn(
        case=case, user_message="yep, the errors are gone now"
    )
    text = result["agent_response"]
    labels = [s["label"] for s in result["suggested_follow_ups"]]

    assert case.pending_transition["to_state"] == "resolved"
    assert labels == [RESOLVE_LABEL, DECLINE_LABEL]
    assert LLM_ANALYSIS in text, "the model's reply must survive the gate turn"
    assert "confirmed eliminated" in text, "an engine offer must say why"
    assert "---" in text, "the reason belongs BELOW the reply, not over it"


@pytest.mark.asyncio
async def test_offer_contradicts_an_over_claiming_narration():
    """The co-occurrence this backstop is most likely to meet.

    A model confident enough to write "Case resolved." is the same one that may
    not bother emitting the structured field — so the over-claim and the missing
    transition arrive together. Composing the gate prose sets
    ``gate_prose_appended``, which SUPPRESSES the INV-40 corrective notice; the
    contract for that suppression is that the gate prose already frames the
    not-yet-terminal state. So it has to say so IN WORDS. An offer phrased as a
    question only implies it, and an implication does not contradict a false
    completion claim sitting one line above it.
    """
    case = _confirmed_case()
    engine = _engine(
        InvestigationResponse_Diagnosis(
            agent_response="Case resolved. The audience mismatch is corrected.",
            state_updates={},
        )
    )

    result = await engine.process_turn(case=case, user_message="all clear now")
    text = result["agent_response"]

    assert case.state == CaseState.INVESTIGATING
    assert "still open until you confirm" in text, (
        "the turn asserts 'Case resolved.' with nothing stating otherwise — "
        "INV-40's notice is suppressed by gate_prose_appended, so this prose "
        "is the only thing standing between the user and a false claim"
    )
    assert RESOLVE_LABEL in [s["label"] for s in result["suggested_follow_ups"]]


@pytest.mark.asyncio
async def test_the_offer_does_not_transition_the_case():
    """INV-03 is untouched: the engine opens the handshake, it never closes it."""
    case = _confirmed_case()

    await _engine(_silent_response()).process_turn(
        case=case, user_message="yep, the errors are gone now"
    )

    assert case.state == CaseState.INVESTIGATING
    assert case.resolved_at is None


@pytest.mark.asyncio
async def test_stabilized_case_is_not_offered_resolve():
    """A symptom-absence row is a workaround holding, not a cause eliminated.

    The backstop must read the SAME bar the readiness gate does — widening it
    to "a cause and a fix are on record" is the over-claim the causal-absence
    gate exists to prevent.
    """
    case = _case(absence=EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE)
    assert assess_resolution_readiness(case).verdict != ResolutionReadiness.READY

    result = await _engine(_silent_response()).process_turn(
        case=case, user_message="failover is holding for now"
    )

    assert getattr(case, "pending_transition", None) is None
    assert RESOLVE_LABEL not in [s["label"] for s in result["suggested_follow_ups"]]


@pytest.mark.asyncio
async def test_engine_authored_failed_fix_row_does_not_open_the_handshake():
    """The engine's own M6 disconfirmation is an absence row that DISPROVES a
    resolution. It must never be read as the confirmation it refutes (#656)."""
    case = _case(
        absence=EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
        collected_by=ENGINE_EVIDENCE_AUTHOR,
    )
    assert assess_resolution_readiness(case).verdict != ResolutionReadiness.READY

    await _engine(_silent_response()).process_turn(
        case=case, user_message="that didn't help"
    )

    assert getattr(case, "pending_transition", None) is None


@pytest.mark.asyncio
async def test_declined_offer_is_not_re_proposed_from_unchanged_state():
    """fm#1122's discipline, inherited: a refusal postpones the offer until a
    premise moves. Five identical offers against five declines is the shape
    this must not reproduce."""
    case = _confirmed_case()
    engine = _engine(_silent_response())

    await engine.process_turn(case=case, user_message="yep, the errors are gone now")
    assert case.pending_transition["to_state"] == "resolved"

    # The user declines on the next turn, then says something ordinary on the
    # one after. Nothing about the case has changed, so the offer must stay down.
    await engine.process_turn(case=case, user_message="no")
    assert getattr(case, "pending_transition", None) is None
    assert case.progress.deferred_disposition_declined_signatures

    result = await engine.process_turn(case=case, user_message="one more question")

    assert getattr(case, "pending_transition", None) is None, "re-nagged"
    assert RESOLVE_LABEL not in [s["label"] for s in result["suggested_follow_ups"]]


@pytest.mark.asyncio
async def test_decline_of_an_llm_opened_offer_also_silences_the_backstop():
    """A refusal binds whoever asked — otherwise the backstop re-nags.

    ``justifying_signature`` is written only by the ENGINE proposers, so a
    decline of an LLM-opened offer recorded nothing, and the backstop — which
    fires on readiness alone and knows nothing about who asked — re-proposed on
    the very next turn. Measured before the fix: LLM opens RESOLVED, user types
    "no", the next ordinary turn carries the offer again. fm#1122's shape
    through a side door, and the backstop is what opened it.
    """
    case = _confirmed_case()
    engine = MilestoneEngine(MagicMock(), _make_repo(), investigation_tools=MagicMock())
    engine._generate_structured_output = AsyncMock(
        side_effect=[
            InvestigationResponse_Diagnosis(
                agent_response="Sounds like the fix held — I'll propose resolving.",
                state_updates={"proposed_transition": {"to_state": "resolved"}},
            ),
            _silent_response(),
            _silent_response(),
        ]
    )

    await engine.process_turn(case=case, user_message="the errors stopped")
    assert case.pending_transition["to_state"] == "resolved"
    assert (
        case.pending_transition.get("justifying_signature") is None
    ), "premise: an LLM-opened offer carries no engine signature"

    await engine.process_turn(case=case, user_message="no")
    assert (
        case.progress.deferred_disposition_declined_signatures
    ), "the refusal left no record, so nothing can be suppressed by it"

    result = await engine.process_turn(case=case, user_message="one more question")

    assert getattr(case, "pending_transition", None) is None, "re-nagged"
    assert RESOLVE_LABEL not in [s["label"] for s in result["suggested_follow_ups"]]


@pytest.mark.asyncio
async def test_contradicted_needs_info_offer_records_no_signature():
    """The decline record must not collect strings that suppress nothing.

    A ``needs_info`` resolve pending CAN reach the decline recorder — 0b's
    contradicting-status-pick arm runs before its ``elif not needs_info`` — and
    on such a case ``assess_closure_readiness`` is HAS_SUBSTANCE, not
    SUGGEST_RESOLVE. Deriving a signature there records a string no resolve
    proposer ever computes: inert for its own purpose, but it lands in the list
    ``_maybe_propose_deferred_close`` reads, which is bounded at 8 and evicts
    oldest-first — so it can push out a refusal still doing work.
    """
    case = _case(absence=None)
    case.pending_transition = {
        "to_state": "resolved",
        "summary": "To mark this resolved I just need a few essentials...",
        "evidence_ids": [],
        "needs_info": True,
        "proposed_at": datetime.now(UTC).isoformat(),
    }
    engine = MilestoneEngine(MagicMock(), _make_repo(), investigation_tools=MagicMock())
    engine._generate_structured_output = AsyncMock(return_value=_silent_response())

    await engine.process_turn(
        case=case,
        user_message="close it instead",
        intent_type="status_transition",
        intent_data={"to_state": "closed"},
    )

    assert (
        case.progress.deferred_disposition_declined_signatures == []
    ), "recorded a signature for a verdict no resolve proposer keys on"


@pytest.mark.asyncio
async def test_no_offer_on_a_turn_that_began_inside_a_handshake():
    """A turn answering a standing disposition offer is not a free channel.

    The user is asked about CLOSING, asks a question about it, and the gate
    withdraws the offer and falls through to normal processing — so by the time
    the backstop runs there is no pending transition left to bail on. It must
    still stay quiet: their next word may be the "yes" they meant for the close
    they were reading, and it must not land on a resolve offer substituted
    underneath it. (The case IS resolution-ready, so the backstop would
    otherwise fire — which is exactly what makes this worth pinning.)
    """
    case = _confirmed_case()
    case.pending_transition = {
        "to_state": "closed",
        "summary": "Shall I close this case?",
        "evidence_ids": [],
        "proposed_at": datetime.now(UTC).isoformat(),
    }

    result = await _engine(_silent_response()).process_turn(
        case=case, user_message="what happens to the runbook if I close this?"
    )

    assert getattr(case, "pending_transition", None) is None
    assert RESOLVE_LABEL not in [s["label"] for s in result["suggested_follow_ups"]]


@pytest.mark.asyncio
async def test_offer_returns_on_the_next_turn():
    """Turn-scoped, not a refusal: the question above postponed the offer, it
    did not record a decline. Nothing declined means nothing suppressed."""
    case = _confirmed_case()
    case.pending_transition = {
        "to_state": "closed",
        "summary": "Shall I close this case?",
        "evidence_ids": [],
        "proposed_at": datetime.now(UTC).isoformat(),
    }
    engine = _engine(_silent_response())

    await engine.process_turn(
        case=case, user_message="what happens to the runbook if I close this?"
    )
    assert not case.progress.deferred_disposition_declined_signatures

    result = await engine.process_turn(case=case, user_message="ok, thanks")

    assert case.pending_transition["to_state"] == "resolved"
    assert RESOLVE_LABEL in [s["label"] for s in result["suggested_follow_ups"]]
