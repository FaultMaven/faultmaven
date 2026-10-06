"""K16 (#1812): every confirmation pair the engine builds names the offer
standing when it is built.

A click executes only when it names the offer standing when it arrives, so a
pair built before its offer stands (or with none) ships cards that can never be
honoured. The census below states N — the builder call sites in ``faultmaven/``
— and the drivers reach every one of them, recording what each pair was
stamped with. A site the census names that no driver reached fails the test,
so it cannot pass by looking in the wrong place.

The shape it exists for: before #1812 the deferred-disposition proposer
(``terminal_proposals._maybe_propose_deferred_close``) built its pair above
``propose_transition``, where no offer stood yet. Restoring that order fails
this test (verified by mutation).
"""

import ast
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import faultmaven
import faultmaven.core.investigation.milestone_engine.cause_state as cause_state
import faultmaven.core.investigation.milestone_engine.statement_revision as statement_revision
import faultmaven.core.investigation.milestone_engine.terminal_replies as terminal_replies
import faultmaven.core.investigation.milestone_engine.transition_consent as transition_consent
import faultmaven.core.investigation.terminal_transitions as terminal_transitions
from faultmaven.core.investigation.milestone_engine.affordances import (
    engine_owned_affordances,
)
from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.milestone_engine.stage_gates import (
    _apply_stage_gate_side_effects,
)
from faultmaven.core.investigation.milestone_engine.terminal_proposals import (
    _maybe_propose_false_alarm_close,
)
from faultmaven.core.investigation.milestone_engine.transition_consent import (
    gate1_offer_key,
    revision_offer_key,
    terminal_offer_key,
)
from faultmaven.core.investigation.milestone_engine.turn_completion import (
    _compose_turn_reply,
)
from faultmaven.core.investigation.problem_status import (
    invalidate_problem,
    propose_revision,
)
from faultmaven.core.investigation.schemas import (
    InvestigationResponse_Diagnosis,
    MilestoneUpdates,
)
from faultmaven.core.investigation.terminal_transitions import propose_transition
from faultmaven.modules.agent.domain.services.orientation import (
    OrientationKind,
    build_orientation,
)
from faultmaven.modules.case.contracts import MitigationRecord, ProblemStatus
from faultmaven.modules.case.domain.models.case import Case
from faultmaven.modules.case.domain.models.conclusion import (
    ConfidenceLevel,
    RootCauseConclusion,
)
from faultmaven.modules.case.domain.models.evidence import (
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
)
from faultmaven.modules.case.domain.models.lifecycle import CaseState
from faultmaven.modules.case.domain.models.problem import ProblemVerification
from faultmaven.modules.case.domain.models.progress import InvestigationProgress
from faultmaven.modules.case.domain.models.solution import Solution, SolutionType

pytestmark = pytest.mark.unit

BUILDERS = (
    "_resolution_confirmation_suggestions",
    "_close_confirmation_suggestions",
    "_investigation_confirmation_suggestions",
    "revision_confirmation_suggestions",
)
PACKAGE = Path(faultmaven.__file__).resolve().parent
STATEMENT = "Checkout API returns 503 for all users since 14:00 UTC"


def _census() -> dict[tuple[str, int], int]:
    """Every call to a builder in ``faultmaven/``: (file, line) -> arg count."""
    sites: dict[tuple[str, int], int] = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in BUILDERS
            ):
                rel = path.relative_to(PACKAGE).as_posix()
                sites[(rel, node.lineno)] = len(node.args) + len(node.keywords)
    return sites


def test_the_census_finds_the_twenty_one_sites_and_each_passes_the_case():
    """State N: 21. The plan's 17 sites, plus the one #1812 added —
    ``transition_turns._refuse_offer_click`` re-shows the Gate 1 pair beside
    its "earlier offer" line — and the three the statement-revision handshake
    adds: its pair served by ``engine_owned_affordances`` and re-shown by
    ``_refuse_offer_click``, and the false-alarm close the engine offers. A
    builder takes the case (no default), because its key is the case's
    standing offer; a zero-argument call could name none.
    """
    sites = _census()
    assert len(sites) == 21, sorted(sites)
    assert all(n == 1 for n in sites.values()), sorted(sites.items())


# ---------------------------------------------------------------------------
# The spy
# ---------------------------------------------------------------------------


@pytest.fixture
def built(monkeypatch):
    """Record, for every pair built, the call site that built it and the key it
    was stamped with. The spy sits under the builders, so the call site is two
    frames up: spy <- builder <- site."""
    records: list[tuple[tuple[str, int], object]] = []
    real = transition_consent.offer_intent_fields

    def spy(proposal_id, *, case, gate):
        site = sys._getframe(2)
        path = Path(site.f_code.co_filename).resolve()
        rel = path.relative_to(PACKAGE).as_posix()
        records.append(((rel, site.f_lineno), proposal_id))
        return real(proposal_id, case=case, gate=gate)

    # ``terminal_replies`` and ``cause_state`` bind it at import;
    # ``stage_gates`` imports it from ``transition_consent`` at call time.
    for module in (
        terminal_replies,
        cause_state,
        transition_consent,
        statement_revision,
    ):
        monkeypatch.setattr(module, "offer_intent_fields", spy)
    return records


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


def _repo():
    repo = MagicMock()
    repo.save = AsyncMock(side_effect=lambda c: c)
    repo.get = AsyncMock(side_effect=lambda cid: None)
    return repo


def _engine(response=None) -> MilestoneEngine:
    engine = MilestoneEngine(MagicMock(), _repo(), investigation_tools=MagicMock())
    engine.generator.generate_structured_output = AsyncMock(
        return_value=response
        or InvestigationResponse_Diagnosis(agent_response="Noted.", state_updates={})
    )
    return engine


def _investigating(*, cause: bool = False, absence: bool = False) -> Case:
    case = Case(
        case_id="case_1812cccccccc",
        title="Checkout 503s",
        state=CaseState.INQUIRY,
        user_id="user_1812",
        enterprise_id="org_1812",
        description="checkout 503s",
        problem_verification=ProblemVerification(
            symptom_statement="checkout returns 503",
            severity="HIGH",
            temporal_state="ongoing",
            urgency_level="high",
        ),
    )
    case.inquiry.proposed_problem_statement = STATEMENT
    case.inquiry.problem_statement_confirmed = True
    case.inquiry.problem_statement_confirmed_at = datetime.now(UTC)
    case.state = CaseState.INVESTIGATING
    case.progress = InvestigationProgress()
    case.current_turn = 5
    if cause:
        case.progress.problem_status = ProblemStatus.VERIFIED
        case.root_cause_conclusion = RootCauseConclusion(
            root_cause="The checkout pool's max connections was lowered to 5.",
            mechanism="Requests queue past the gateway timeout and return 503.",
            confidence_level=ConfidenceLevel.CONFIDENT,
            likelihood=0.85,
        )
        case.solutions = [
            Solution(
                solution_type=SolutionType.CONFIG_CHANGE,
                title="Restore the checkout pool size",
                longterm_fix="Set max connections back to 50.",
            )
        ]
    if absence:
        case.evidence.append(
            Evidence(
                category=EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
                primary_purpose="confirm the cause was eliminated",
                summary="After the pool fix the 503s stopped and did not return.",
                source_type=EvidenceSourceType.USER_DESCRIPTION,
                collected_by="user",
                collected_at_turn=1,
            )
        )
    return case


def _pending(to_state, **kw) -> Case:
    case = _investigating(**kw)
    propose_transition(case, to_state=to_state, summary=f"Shall I {to_state} it?")
    return case


def _gate1_case() -> Case:
    case = Case(
        case_id="case_1812dddddddd",
        title="Checkout 503s",
        state=CaseState.INQUIRY,
        user_id="user_1812",
        enterprise_id="org_1812",
        description="",
    )
    case.inquiry.proposed_problem_statement = STATEMENT
    case.current_turn = 2
    return case


def _keys(follow_ups) -> list:
    return [(f.get("intent") or {}).get("proposal_id") for f in follow_ups]


def _served_the_standing_offer(result, case):
    key = terminal_offer_key(case.pending_transition)
    assert key, "premise: an offer stands after the turn"
    assert _keys(result["suggested_follow_ups"]) == [key, key]


# ---------------------------------------------------------------------------
# The drivers: one per path onto the builders
# ---------------------------------------------------------------------------


async def _gate1_pending_turn():
    """affordances.py (engine_owned_affordances) and orientation.py."""
    case = _gate1_case()
    _, pair = engine_owned_affordances(case)
    assert _keys(pair) == [gate1_offer_key(STATEMENT)] * 2
    reply = build_orientation(case, OrientationKind.GREETING)
    assert _keys(reply["suggested_follow_ups"]) == [gate1_offer_key(STATEMENT)] * 2


async def _rca_infeasible_close():
    """stage_gates.py: mitigation verified on a case whose RCA is infeasible."""
    case = _investigating()
    case.problem_verification.rca_infeasible = True
    case.problem_verification.rca_infeasible_rationale = "third-party API outage"
    case.progress.mitigation = MitigationRecord(
        proposed_at_turn=1, accepted=True, verified=True, completed_at_turn=1
    )
    metadata: dict = {}
    _apply_stage_gate_side_effects(case, {"mitigation_verified"}, "it held", metadata)
    assert (
        _keys(metadata["override_suggestions"])
        == [terminal_offer_key(case.pending_transition)] * 2
    )


async def _deferred_disposition(*, resolvable: bool):
    """terminal_proposals.py: the deferred-disposition proposer, both targets."""
    case = _investigating(cause=True, absence=resolvable)
    response = InvestigationResponse_Diagnosis(
        agent_response="The fix is documented.",
        state_updates={"milestones": MilestoneUpdates(solution_feasible="deferred")},
    )
    result = await _engine(response).process_turn(
        case=case, user_message="the platform team ships it"
    )
    assert case.pending_transition["to_state"] == (
        "resolved" if resolvable else "closed"
    )
    _served_the_standing_offer(result, case)


async def _resolution_backstop():
    """terminal_proposals.py: the INV-43 backstop on a READY case."""
    case = _investigating(cause=True, absence=True)
    result = await _engine().process_turn(case=case, user_message="it's gone now")
    assert case.pending_transition["to_state"] == "resolved"
    _served_the_standing_offer(result, case)


async def _gate_re_asks():
    """transition_turns.py: the re-ask, for both targets."""
    for to_state in ("resolved", "closed"):
        case = _pending(to_state)
        result = await _engine().process_turn(case=case, user_message="hmm")
        _served_the_standing_offer(result, case)


async def _click_close_on_a_resolvable_case():
    """transition_turns.py: the INV-37 pivot at 0b's confirm."""
    case = _pending("closed", cause=True, absence=True)
    with patch.object(terminal_transitions, "close_pivoted_to_resolve_total"):
        result = await _engine().process_turn(
            case=case,
            user_message="Yes, close this case without resolution.",
            intent_type="confirmation",
            intent_data={
                "value": True,
                "proposal_id": terminal_offer_key(case.pending_transition),
            },
        )
    assert case.pending_transition["to_state"] == "resolved"
    _served_the_standing_offer(result, case)


async def _close_picked_from_the_menu():
    """transition_turns.py: the status dropdown's close, plain and pivoted."""
    for resolvable in (False, True):
        case = _investigating(cause=resolvable, absence=resolvable)
        result = await _engine().process_turn(
            case=case,
            user_message="",
            intent_type="status_transition",
            intent_data={"to_state": "closed"},
        )
        assert case.pending_transition["to_state"] == (
            "resolved" if resolvable else "closed"
        )
        _served_the_standing_offer(result, case)


async def _typed_yes_on_a_pending_close_pivots():
    """transitions.py (step 0 of check_automatic_transitions) and
    turn_completion.py's close_pivoted_to_resolve branch. Step 0's confirm arm
    is reached only by a direct call: 0b answers or withdraws every
    non-needs_info pending before the LLM runs."""
    case = _pending("closed", cause=True, absence=True)
    engine = _engine()
    metadata: dict = {}
    with patch.object(terminal_transitions, "close_pivoted_to_resolve_total"):
        await engine.transitions.check_automatic_transitions(case, metadata, "yes")
    assert metadata.get("close_pivoted_to_resolve") is True
    assert (
        _keys(metadata["override_suggestions"])
        == [terminal_offer_key(case.pending_transition)] * 2
    )

    reply = await _compose_turn_reply(
        None,
        None,
        _repo(),
        case_updated=case,
        follow_ups=[],
        metadata={"close_pivoted_to_resolve": True},
        redaction_ctx=None,
        response_obj=InvestigationResponse_Diagnosis(
            agent_response="Noted.", state_updates={}
        ),
        stagnation_str="",
        summary_failed=False,
        summary_payload=None,
        validation_repairs=[],
    )
    _served_the_standing_offer(reply, case)


async def _llm_proposes_a_transition():
    """transitions.py: the model's proposed_transition, both targets."""
    for to_state, case in (
        ("resolved", _investigating(cause=True, absence=True)),
        ("closed", _investigating()),
    ):
        response = MagicMock()
        response.state_updates.proposed_transition = MagicMock(
            to_state=to_state, evidence_ids=[]
        )
        metadata: dict = {"response_obj": response}
        await _engine().transitions.check_automatic_transitions(
            case=case, metadata=metadata, user_message="The fix worked."
        )
        assert (
            _keys(metadata["override_suggestions"])
            == [terminal_offer_key(case.pending_transition)] * 2
        )


async def _needs_info_answered():
    """turn_completion.py: a needs_info RESOLVED offer, once answered — met
    (the resolve pair) or not (the pivot to a close pair)."""
    for met in (True, False):
        case = _pending("resolved", cause=True, absence=met)
        case.pending_transition["needs_info"] = True
        result = await _engine().process_turn(
            case=case, user_message="here is how we verified it"
        )
        assert case.pending_transition["to_state"] == ("resolved" if met else "closed")
        _served_the_standing_offer(result, case)


async def _stale_click_on_gate1():
    """transition_turns.py: a refused click re-shows Gate 1 with its pair."""
    case = _gate1_case()
    result = await _engine().process_turn(
        case=case,
        user_message="Yes, the issue is resolved.",
        intent_type="confirmation",
        intent_data={"value": True, "proposal_id": "2026-01-01T00:00:00+00:00"},
    )
    assert _keys(result["suggested_follow_ups"]) == [gate1_offer_key(STATEMENT)] * 2


REVISED = "Checkout API returns 503 only for EU-region users since 14:00 UTC"


def _revision_case() -> Case:
    case = _investigating()
    propose_revision(
        case,
        text=REVISED,
        evidence_ids=[],
        basis="the gateway log shows 503s only from eu-west",
        offer_key=revision_offer_key(REVISED),
    )
    return case


async def _revision_pending_turn():
    """affordances.py: the statement-revision pair, served while it waits."""
    case = _revision_case()
    gate, pair = engine_owned_affordances(case)
    assert gate == "statement_revision"
    assert _keys(pair) == [revision_offer_key(REVISED)] * 2


async def _stale_click_on_the_revision():
    """transition_turns.py: a refused click re-shows the revision with its pair."""
    case = _revision_case()
    result = await _engine().process_turn(
        case=case,
        user_message="Yes, the revised problem statement is right.",
        intent_type="confirmation",
        intent_data={"value": True, "proposal_id": "revision:0000000000000000"},
    )
    assert _keys(result["suggested_follow_ups"]) == [revision_offer_key(REVISED)] * 2


async def _false_alarm_close():
    """terminal_proposals.py: the close the engine offers on a false alarm."""
    case = _investigating()
    invalidate_problem(case, evidence_ids=[], basis="nothing failed in that window.")
    metadata = {"problem_invalidated_this_turn": True}
    _maybe_propose_false_alarm_close(case, metadata)
    assert (
        _keys(metadata["override_suggestions"])
        == [terminal_offer_key(case.pending_transition)] * 2
    )


DRIVERS = [
    _gate1_pending_turn,
    _revision_pending_turn,
    _stale_click_on_the_revision,
    _false_alarm_close,
    _stale_click_on_gate1,
    _rca_infeasible_close,
    lambda: _deferred_disposition(resolvable=False),
    lambda: _deferred_disposition(resolvable=True),
    _resolution_backstop,
    _gate_re_asks,
    _click_close_on_a_resolvable_case,
    _close_picked_from_the_menu,
    _typed_yes_on_a_pending_close_pivots,
    _llm_proposes_a_transition,
    _needs_info_answered,
]


@pytest.mark.asyncio
async def test_every_site_builds_its_pair_with_its_offer_standing(built):
    for drive in DRIVERS:
        await drive()

    unkeyed = sorted({site for site, key in built if not key})
    assert unkeyed == [], f"pairs built with no offer standing, at {unkeyed}"
    unreached = sorted(set(_census()) - {site for site, _ in built})
    assert unreached == [], (
        f"census sites no driver reached: {unreached} — the test would pass "
        "without looking at them"
    )
