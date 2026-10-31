"""K16 (#1812): every confirmation pair the engine serves names the offer
standing when it is served.

A click executes only when it names the offer standing when it arrives, so a
pair built before its offer stands (or with none) ships cards that can never be
honoured. The shape it existed for: before #1812 the deferred-disposition
proposer built its pair above ``propose_transition``, where no offer stood yet.

Since #2379 the resolve, close and revision pairs are built in one place, the
offer projection (``offer_projection.project_offers``), from the open offer itself, so a
pair cannot be built before its offer: the census below pins that the
projection is their only builder. Gate 1's pair is not an offer on the record
and keeps its own builder, keyed by the statement; its call sites are counted,
and the drivers reach every one. Each driver also asserts that the pair it is
served names the offer open after its turn.
"""

import ast
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import faultmaven
import faultmaven.core.investigation.milestone_engine.cause_state as cause_state
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
)
from faultmaven.core.investigation.offer_projection import project_offers
from faultmaven.core.investigation.offers import remember_resolve_request
from faultmaven.core.investigation.problem_status import (
    invalidate_problem,
    propose_revision,
)
from faultmaven.core.investigation.schemas import (
    InvestigationResponse_Diagnosis,
    MilestoneUpdates,
)
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
from tests.case_builders import with_declined_offer, with_open_offer
from tests.source_tree import parse_file

_FILE_ID = "file_0123456789ab"

pytestmark = pytest.mark.unit

#: Gate 1's pair builder, keyed by the statement it presents.
BUILDERS = ("_investigation_confirmation_suggestions",)
#: The offer pairs, each built from the open offer by the projection.
PAIR_BUILDERS = ("_resolve_pair", "_close_pair", "_revision_pair")
PACKAGE = Path(faultmaven.__file__).resolve().parent
STATEMENT = "Checkout API returns 503 for all users since 14:00 UTC"


def _census() -> dict[tuple[str, int], int]:
    """Every call to a builder in ``faultmaven/``: (file, line) -> arg count."""
    sites: dict[tuple[str, int], int] = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = parse_file(path)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in BUILDERS
            ):
                rel = path.relative_to(PACKAGE).as_posix()
                sites[(rel, node.lineno)] = len(node.args) + len(node.keywords)
    return sites


def test_the_census_finds_gate_ones_three_sites_and_each_passes_the_case():
    """State N: 3 — ``engine_owned_affordances``, ``_refuse_offer_click``
    (re-showing Gate 1 beside its "earlier offer" line) and the orientation
    reply. The builder takes the case, because its key is the case's
    statement; a zero-argument call could name none."""
    sites = _census()
    assert len(sites) == 3, sorted(sites)
    assert all(n == 1 for n in sites.values()), sorted(sites.items())


def test_the_offer_pairs_are_built_only_by_the_projection():
    """Every reference to the resolve, close and revision pair builders is in
    ``core/investigation/offers.py``: no other site can build a pair, so none
    can build one before its offer is open."""
    refs = set()
    for path in sorted(PACKAGE.rglob("*.py")):
        for node in ast.walk(parse_file(path)):
            if isinstance(node, ast.Name) and node.id in PAIR_BUILDERS:
                refs.add(path.relative_to(PACKAGE).as_posix())
    assert refs == {"core/investigation/offers.py"}


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

    # ``cause_state`` binds it at import.
    for module in (cause_state, transition_consent):
        monkeypatch.setattr(module, "offer_intent_fields", spy)
    return records


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


def _repo():
    repo = MagicMock()
    repo.save = AsyncMock(side_effect=lambda c, **_: c)
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
                source_type=EvidenceSourceType.LOGS,
                source_file_id=_FILE_ID,
                collected_by="user",
                collected_at_turn=1,
            )
        )
        case.evidence.append(
            Evidence(
                category=EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE,
                primary_purpose="re-check the reported symptom after the fix",
                summary="The checkout requests return 200 again; no 503s since the fix.",
                source_type=EvidenceSourceType.LOGS,
                source_file_id=_FILE_ID,
                collected_by="user",
                collected_at_turn=1,
            )
        )
    return case


def _pending(to_state, **kw) -> Case:
    case = _investigating(**kw)
    kind = {"resolved": "resolve", "closed": "close"}[to_state]
    with_open_offer(case, kind=kind, basis="model", summary=f"Shall I {to_state} it?")
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
    key = case.offer_record.open.proposal_id
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
    assert case.offer_record.open.basis == "rca_infeasible"
    assert _keys(project_offers(case).pair) == [case.offer_record.open.proposal_id] * 2


async def _deferred_disposition(*, resolvable: bool):
    """terminal_proposals.py: the deferred-disposition proposer, both targets."""
    case = _investigating(cause=True, absence=resolvable)
    response = InvestigationResponse_Diagnosis(
        agent_response="The fix is documented.",
        state_updates={"milestones": MilestoneUpdates(solution_feasible="deferred")},
    )
    result = await _engine(response).process_turn(
        case=case, user_message="the platform team ships it", redaction_ctx=None
    )
    assert case.offer_record.open.kind == ("resolve" if resolvable else "close")
    _served_the_standing_offer(result, case)


async def _resolution_backstop():
    """terminal_proposals.py: the INV-43 backstop on a READY case."""
    case = _investigating(cause=True, absence=True)
    result = await _engine().process_turn(
        case=case, user_message="it's gone now", redaction_ctx=None
    )
    assert case.offer_record.open.kind == "resolve"
    _served_the_standing_offer(result, case)


async def _gate_re_asks():
    """transition_turns.py: the re-ask, for both targets."""
    for to_state in ("resolved", "closed"):
        case = _pending(to_state)
        result = await _engine().process_turn(
            case=case, user_message="hmm", redaction_ctx=None
        )
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
                "proposal_id": case.offer_record.open.proposal_id,
            },
            redaction_ctx=None,
        )
    assert case.offer_record.open.kind == "resolve"
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
            redaction_ctx=None,
        )
        assert case.offer_record.open.kind == ("resolve" if resolvable else "close")
        _served_the_standing_offer(result, case)


async def _typed_yes_on_a_pending_close_pivots():
    """transition_turns.py: a bare typed "yes" accepts the close at 0b, and the
    INV-37 re-check pivots it to a resolve offer whose pair is served."""
    case = _pending("closed", cause=True, absence=True)
    with patch.object(terminal_transitions, "close_pivoted_to_resolve_total"):
        result = await _engine().process_turn(
            case=case, user_message="yes", redaction_ctx=None
        )
    assert case.offer_record.open.kind == "resolve"
    assert case.offer_record.open.pivoted_from_close
    _served_the_standing_offer(result, case)


async def _reopen_chip_click():
    """transition_turns.py: the "Mark it resolved" chip proposes, and the pair
    names the offer it just made (#1895)."""
    case = _investigating(cause=True, absence=True)
    with_declined_offer(case, kind="resolve", basis="backstop")
    chip = project_offers(case).reopen_chip
    result = await _engine().process_turn(
        case=case,
        user_message=chip["payload"],
        intent_type="status_transition",
        intent_data=dict(chip["intent"]),
        redaction_ctx=None,
    )
    assert case.offer_record.open.kind == "resolve"
    _served_the_standing_offer(result, case)


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
            case=case,
            metadata=metadata,
            user_message="The fix worked.",
        )
        assert case.offer_record.open.basis == "model"
        assert (
            _keys(project_offers(case).pair) == [case.offer_record.open.proposal_id] * 2
        )


async def _needs_info_answered():
    """transitions.py (step 0) and turn_completion.py: a resolution request,
    re-checked on the next turn — met (the resolve pair) or not (the close
    pair, #656's loop bound)."""
    for met in (True, False):
        case = _investigating(cause=True, absence=met)
        case.offer_record = remember_resolve_request(case, ["root cause"])
        case.current_turn += 1
        result = await _engine().process_turn(
            case=case, user_message="here is how we verified it", redaction_ctx=None
        )
        assert case.offer_record.open.kind == ("resolve" if met else "close")
        _served_the_standing_offer(result, case)


async def _stale_click_on_gate1():
    """transition_turns.py: a refused click re-shows Gate 1 with its pair."""
    case = _gate1_case()
    result = await _engine().process_turn(
        case=case,
        user_message="Yes, the issue is resolved.",
        intent_type="confirmation",
        intent_data={"value": True, "proposal_id": "2026-01-01T00:00:00+00:00"},
        redaction_ctx=None,
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
        wording_key=revision_offer_key(REVISED),
    )
    return case


async def _revision_pending_turn():
    """affordances.py: the statement-revision pair, served while it waits."""
    case = _revision_case()
    gate, pair = engine_owned_affordances(case)
    assert gate == "statement_revision"
    assert _keys(pair) == [case.offer_record.open.proposal_id] * 2


async def _stale_click_on_the_revision():
    """transition_turns.py: a refused click re-shows the revision with its pair."""
    case = _revision_case()
    result = await _engine().process_turn(
        case=case,
        user_message="Yes, the revised problem statement is right.",
        intent_type="confirmation",
        intent_data={"value": True, "proposal_id": "revision:0000000000000000"},
        redaction_ctx=None,
    )
    assert (
        _keys(result["suggested_follow_ups"])
        == [case.offer_record.open.proposal_id] * 2
    )


async def _false_alarm_close():
    """terminal_proposals.py: the close the engine offers on a false alarm."""
    case = _investigating()
    invalidate_problem(case, evidence_ids=[], basis="nothing failed in that window.")
    metadata = {"problem_invalidated_this_turn": True}
    _maybe_propose_false_alarm_close(case, metadata)
    assert case.offer_record.open.basis == "false_alarm"
    assert _keys(project_offers(case).pair) == [case.offer_record.open.proposal_id] * 2


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
    _reopen_chip_click,
    _typed_yes_on_a_pending_close_pivots,
    _llm_proposes_a_transition,
    _needs_info_answered,
]


@pytest.mark.asyncio
async def test_every_site_builds_its_pair_with_its_offer_standing(built):
    for drive in DRIVERS:
        await drive()

    unkeyed = sorted({site for site, key in built if not key})
    assert unkeyed == [], f"Gate 1 pairs built with no statement, at {unkeyed}"
    unreached = sorted(set(_census()) - {site for site, _ in built})
    assert unreached == [], (
        f"census sites no driver reached: {unreached} — the test would pass "
        "without looking at them"
    )
