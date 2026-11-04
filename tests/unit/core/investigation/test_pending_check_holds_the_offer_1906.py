"""#1906 Phase 2b-ii (D5.2, the stand-down) and #2495.

While a check of the reported problem the model asked for after the fix is
unanswered, no resolve offer opens (rulings of 2026-11-04). The defect: the
model asked the user whether Prometheus showed the billing target UP, and the
engine's backstop, below the same reply, offered the resolution. Readiness
reads NEEDS_INFO while ``pending_symptom_check`` holds a need, and every
opener reads readiness, except the deferred proposer, which reads the
predicate itself. A close still pivots to the resolve (ruling 2, INV-37).

Real turns (``MilestoneEngine.process_turn``, only the LLM seam replaced)
where a reader's ordering matters; the real apply step
(``_apply_investigation_updates``) for the predicate's shapes. Numbered as
the round-59 plan's tests. No live LLM.
"""

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.cause_assurance import (
    cause_recheck_status,
    fix_windows,
    has_resolution_confirmation,
    latest_disconfirmation_turn,
    pending_symptom_check,
)
from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.milestone_engine.transitions import (
    declined_resolution_reask,
)
from faultmaven.core.investigation.offer_projection import (
    DECLINED_RESOLVE_CARD_LABEL,
    DECLINED_RESOLVE_CLOSE_LABEL,
    project_offers,
)
from faultmaven.core.investigation.prompts.context_builder.fix_verification_block import (
    build_fix_verification_block,
)
from faultmaven.core.investigation.prompts.templates.assembly import (
    get_prompt_for_case,
)
from faultmaven.core.investigation.schemas import (
    EvidenceNeedUpdate,
    InvestigationResponse_Diagnosis,
    SuggestedFollowUp,
)
from faultmaven.core.investigation.terminal_transitions import (
    RESOLVE_DECLINED_ACTIONS,
    RESOLVE_DECLINED_CLOSE_ACTION,
    RESOLVE_DECLINED_REOPEN_ACTION,
    ClosureReadiness,
    ResolutionReadiness,
    assess_resolution_readiness,
    closure_verdict,
    derive_disposition_eligibility,
)
from faultmaven.models.api_models import DISPOSITION_ELIGIBILITY_DESCRIPTION
from faultmaven.modules.case.contracts import (
    EvidenceCategory,
    EvidenceNeed,
    InvestigationActionType,
    InvestigationStage,
    NeedPurpose,
    NeedState,
    ProposedAction,
)
from faultmaven.modules.case.infrastructure.case_repository import CaseRepository
from tests.case_builders import with_declined_offer, with_open_offer
from tests.unit.core.investigation.bound_rows import (
    VERIFYING_ID,
    bound_row,
    ensure_subjects,
    upload,
)
from tests.unit.core.investigation.given_tools import DEFAULT_GIVEN
from tests.unit.core.investigation.test_deferred_disposition_turn import (
    _case as _deferred_case,
)
from tests.unit.core.investigation.test_deferred_disposition_turn import (
    _deferred_response,
)
from tests.unit.core.investigation.test_fix_verification_subjects_1906 import (
    _CAUSE_GONE,
    _GONE,
    _PRESENT,
    _SUPPORT,
    _fixed,
    _obs,
    _record,
)
from tests.unit.core.investigation.test_m6_cause_gone_problem_persists_1927 import (
    _accept,
)
from tests.unit.core.investigation.test_offer_cites_its_rows_1906 import _confirm
from tests.unit.core.investigation.test_resolution_backstop_turn import (
    _case as _backstop_case,
)
from tests.unit.core.investigation.test_resolution_backstop_turn import _make_repo

pytestmark = pytest.mark.unit

#: The check #1906's model asked for, in the same reply the backstop offered.
_ASK = "Prometheus targets page showing job=billing UP"
_HELD_ASK = f"- **The check I asked for** — {_ASK}"
_HELD_FEEDBACK = "RESOLVED NOT PROPOSED: the check you asked for is still pending"
_CLOSE_PARAGRAPH = "close** the case instead"


def _ask(**fields) -> EvidenceNeedUpdate:
    """The model's new symptom need, as the prompt rule has it declared."""
    return EvidenceNeedUpdate(
        purpose="symptom_verification",
        request_text=_ASK,
        rationale="the reported symptom, re-checked after the fix",
        **fields,
    )


def _mark(need, state: str, *ids: str, **fields) -> EvidenceNeedUpdate:
    """The model's update of ``need`` to ``state``, citing ``ids``."""
    return EvidenceNeedUpdate(
        need_id=need.need_id, state=state, fulfilling_evidence_ids=list(ids), **fields
    )


def _need(case, turn: int, *surfaced: int, **fields) -> EvidenceNeed:
    """A symptom need already on ``case``'s pool, created at ``turn``."""
    return EvidenceNeed(
        case_id=case.case_id,
        purpose=fields.pop("purpose", NeedPurpose.SYMPTOM_VERIFICATION),
        request_text=_ASK,
        rationale="the reported symptom",
        created_at_turn=turn,
        surfaced_turns=list(surfaced),
        **fields,
    )


# ---------------------------------------------------------------------------
# Real turns
# ---------------------------------------------------------------------------


def _engine() -> MilestoneEngine:
    return MilestoneEngine(MagicMock(), _make_repo(), investigation_tools=MagicMock())


def _fixed_case(fix_turn: int = 6):
    """A case with a cause on record, the row that showed the problem and the
    cause's support (``ensure_subjects``), and its fix executed at
    ``fix_turn``."""
    case = _backstop_case(absence=None)
    verifying, support = ensure_subjects(case)
    case.proposed_actions.append(
        ProposedAction(
            case_id=case.case_id,
            action_type=InvestigationActionType.SOLUTION,
            description="Point ExecStart at /usr/bin/billing-exporter",
            proposed_in_turn=fix_turn - 1,
            accepted_in_turn=fix_turn,
            state="accepted",
        )
    )
    case.current_turn = fix_turn
    return case, verifying, support


def _row(category, summary, file_id, rechecks=None) -> dict:
    return {
        "summary": summary,
        "extract": summary,
        "category": category.value,
        "source_type": "logs",
        "source_file_id": file_id,
        "rechecks": rechecks,
    }


async def _turn(
    engine,
    case,
    turn: int,
    message: str,
    *,
    rows=(),
    needs=(),
    follow_ups=None,
    propose=None,
    **process,
) -> dict:
    """Turn ``turn``: the model's response records ``rows`` and ``needs`` and
    proposes ``propose``; ``process`` passes intents through."""
    case.current_turn = turn
    state_updates: dict = {}
    if rows:
        state_updates["evidence_to_add"] = list(rows)
    if needs:
        state_updates["evidence_need_updates"] = list(needs)
    if propose:
        state_updates["proposed_transition"] = {"to_state": propose}
    response = InvestigationResponse_Diagnosis(
        agent_response="The unit runs the corrected binary again.",
        state_updates=state_updates,
        suggested_follow_ups=follow_ups,
    )
    engine.generator.generate_structured_output = AsyncMock(return_value=response)
    return await engine.process_turn(
        case=case, user_message=message, redaction_ctx=None, **process
    )


async def _asked_at_7(engine):
    """#1906's shape (charter-3/4): the fix at 6; at 7 the status shows the
    cause gone and the unit running (both legs), and the model asks for the
    Prometheus check as a new need, surfaced as the EVIDENCE suggestion that
    names it (the prompt rule). Returns the case, the turn, the need and the
    verifying row's id."""
    case, verifying, support = _fixed_case()
    data = upload(case, 7, "systemctl status")
    result = await _turn(
        engine,
        case,
        7,
        "unit status after the fix",
        rows=[
            _row(
                _CAUSE_GONE, "ExecStart names /usr/bin/billing-exporter", data, support
            ),
            _row(
                _GONE, "billing-exporter.service is active (running)", data, verifying
            ),
        ],
        needs=[_ask()],
        follow_ups=[
            SuggestedFollowUp(
                label="Check the scrape target",
                action_type="EVIDENCE",
                body=_ASK,
                evidence_need_id="new_index_0",
            )
        ],
    )
    (need,) = [n for n in case.evidence_needs if n.request_text == _ASK]
    return case, result, need, verifying


async def test_1_the_check_asked_with_the_legs_holds_the_backstop(caplog):
    """The held backstop tells the model the need and its two exits, on a
    DIAGNOSIS-stage case too, where no ``<fix_verification>`` block or
    RE-VERIFICATION rule renders (fix list 2)."""
    caplog.set_level(logging.INFO)
    case, result, need, _ = await _asked_at_7(_engine())
    assert has_resolution_confirmation(case), "premise: both legs stand at 7"
    assert case.current_stage == InvestigationStage.DIAGNOSIS, "premise"
    assert build_fix_verification_block(case) == "", "premise: no block"
    # Linking ran in the real order: the suggestion named the new need.
    assert (need.created_at_turn, need.surfaced_turns) == (7, [7])
    readiness = assess_resolution_readiness(case)
    assert readiness.verdict == ResolutionReadiness.NEEDS_INFO
    assert readiness.missing == ["the check you asked for"]
    assert readiness.pending_check is need
    assert _HELD_ASK in readiness.message
    assert _CLOSE_PARAGRAPH not in readiness.message
    assert case.offer_record.open is None
    assert "Shall I mark this case resolved?" not in result["agent_response"]
    feedback = result["case_updated"].turn_history[-1].system_feedback
    assert (
        f"RESOLUTION HELD: the check you asked for is still pending "
        f"([{need.need_id}] {_ASK}). The resolution is offered once its answer is "
        "on record: record the row and mark the need FULFILLED citing it. If the "
        "user cannot run the check, SUPERSEDE the need with the reason."
    ) in feedback
    assert _compliance(caplog, 7) == {need.need_id}


def _compliance(caplog, turn: int) -> set:
    """``resolution_pending_check`` on turn ``turn``'s ``transition_compliance``."""
    return {
        r.resolution_pending_check
        for r in caplog.records
        if r.msg == "transition_compliance" and r.turn == turn
    }


@pytest.mark.parametrize("answer", ["bound row", "need fulfilled"])
async def test_1_its_answer_at_8_releases_it_and_the_backstop_offers(answer):
    """Ruling 1's two exits: a later problem-bound row, or the need marked
    FULFILLED citing a row from after the ask (here an unbound one)."""
    engine = _engine()
    case, _, need, verifying = await _asked_at_7(engine)
    data = upload(case, 8, "prometheus targets")
    rechecks = verifying if answer == "bound row" else None
    needs = [_mark(need, "fulfilled", "new_index_0")] if rechecks is None else []
    await _turn(
        engine,
        case,
        8,
        "the targets page",
        rows=[_row(_GONE, "Prometheus shows job=billing UP", data, rechecks)],
        needs=needs,
    )
    if answer == "need fulfilled":
        assert need.state == NeedState.FULFILLED, "premise"
    assert assess_resolution_readiness(case).verdict == ResolutionReadiness.READY
    offer = case.offer_record.open
    assert (offer.kind, offer.basis) == ("resolve", "backstop")


async def test_2_fulfilled_on_a_row_from_before_the_ask_still_holds():
    """The answer is cited data (ruling 1, made exact): the row the model
    cites is from turn 1, before its own ask at 7, so the need holds, its
    state is not demoted, and the model's ``resolved`` is told the rows
    predate the ask."""
    engine = _engine()
    case, _, need, verifying = await _asked_at_7(engine)
    result = await _turn(
        engine,
        case,
        8,
        "already sent it",
        needs=[_mark(need, "fulfilled", verifying)],
        propose="resolved",
    )
    assert need.state == NeedState.FULFILLED
    assert pending_symptom_check(case) is need
    assert case.offer_record.open is None
    feedback = result["case_updated"].turn_history[-1].system_feedback
    assert (
        f"the check you asked for ([{need.need_id}] {_ASK}) is marked FULFILLED, "
        "but the rows you cited predate the ask" in feedback
    )


async def test_3_the_models_resolved_while_held_gets_feedback_and_no_request(
    caplog,
):
    """Ruling 3: refused with feedback naming both exits; no request is
    remembered, so the next turn's step 0 opens no needs-info-unmet close.
    The readiness ask still goes below the reply, in the held frame."""
    engine = _engine()
    case, _, need, _ = await _asked_at_7(engine)
    caplog.set_level(logging.INFO)
    result = await _turn(engine, case, 8, "mark it resolved", propose="resolved")
    feedback = result["case_updated"].turn_history[-1].system_feedback
    assert f"{_HELD_FEEDBACK} ([{need.need_id}] {_ASK})." in feedback
    assert "mark the need FULFILLED citing it" in feedback
    assert "SUPERSEDE the need with the reason" in feedback
    # Told once: the backstop does not repeat step 2's feedback.
    assert "RESOLUTION HELD" not in feedback
    assert case.offer_record.resolve_request is None
    assert case.offer_record.open is None
    assert _HELD_ASK in result["agent_response"]
    assert _CLOSE_PARAGRAPH not in result["agent_response"]
    assert _compliance(caplog, 8) == {need.need_id}

    await _turn(engine, case, 9, "still checking")
    assert case.offer_record.open is None


async def test_4_step_zero_opens_no_close_on_a_check_that_became_pending():
    """A request remembered at 7 (the cause leg alone), re-checked at 8: the
    problem's check lands, and the model asks for another one in that turn.
    The request ends, and nothing opens (ruling 3)."""
    engine = _engine()
    case, verifying, support = _fixed_case()
    data = upload(case, 7, "status")
    await _turn(
        engine,
        case,
        7,
        "it's fixed",
        rows=[_row(_CAUSE_GONE, "ExecStart names the binary", data, support)],
        propose="resolved",
    )
    assert case.offer_record.resolve_request is not None, "premise: remembered"
    data = upload(case, 8, "status again")
    await _turn(
        engine,
        case,
        8,
        "here is the unit",
        rows=[_row(_GONE, "billing-exporter.service is active", data, verifying)],
        needs=[_ask()],
    )
    assert has_resolution_confirmation(case), "premise: both legs stand at 8"
    assert case.offer_record.resolve_request is None
    assert case.offer_record.open is None


def _held_case(asked: bool = True):
    """``_asked_at_7``'s record, built without a turn: the legs and (with
    ``asked``) the ask at 7, no offer open. Test 5's controls start here, so
    they read the same with the hold reverted."""
    case, verifying, support = _fixed_case()
    bound_row(case, _CAUSE_GONE, turn=7, rechecks=support)
    bound_row(case, _GONE, turn=7, rechecks=verifying)
    case.current_turn = 7
    if asked:
        need = _need(case, 7, 7)
        case.evidence_needs.append(need)
        assert pending_symptom_check(case) is need, "premise: held"
    return case


async def test_5_the_models_closed_while_held_pivots_to_the_resolve():
    """Ruling 2 (INV-37): the close→resolve pivot stays on the record."""
    engine = _engine()
    case = _held_case()
    await _turn(engine, case, 8, "close it", propose="closed")
    offer = case.offer_record.open
    assert (offer.kind, offer.basis) == ("resolve", "model")


async def test_5_the_confirm_time_pivot_is_unchanged():
    engine = _engine()
    case = _held_case()
    key = with_open_offer(case, kind="close", basis="model").proposal_id
    await _turn(
        engine,
        case,
        8,
        "yes",
        intent_type="confirmation",
        intent_data={"value": True, "proposal_id": key},
    )
    assert case.offer_record.open.pivoted_from_close


async def test_6_the_reopen_chip_while_held_answers_with_the_pending_ask():
    """The confirmation still stands, so "no longer stands" would be false;
    the reply is the held ask alone, and it carries the case's chips: Slack
    has no other way to close, and its close chip is built only beside the
    reopen chip (#2222, fix list 5)."""
    engine = _engine()
    case, verifying, support = _fixed_case()
    data = upload(case, 7, "status")
    await _turn(
        engine,
        case,
        7,
        "status after the fix",
        rows=[
            _row(_CAUSE_GONE, "ExecStart names the binary", data, support),
            _row(_GONE, "billing-exporter.service is active", data, verifying),
        ],
    )
    assert case.offer_record.open.basis == "backstop", "premise: offered"
    await _turn(engine, case, 8, "no")
    assert case.offer_record.open is None, "premise: declined"
    # The model's check, asked on the decline's turn.
    case.evidence_needs.append(_need(case, 8, 8))
    intent = dict(project_offers(case).reopen_chip["intent"])
    result = await _turn(
        engine,
        case,
        9,
        "Mark this case resolved.",
        intent_type="status_transition",
        intent_data=intent,
    )
    reply = result["agent_response"]
    assert reply == assess_resolution_readiness(case).message
    assert _HELD_ASK in reply
    assert "no longer stands" not in reply
    assert _CLOSE_PARAGRAPH not in reply
    assert case.offer_record.open is None
    assert [f["label"] for f in result["suggested_follow_ups"]] == [
        DECLINED_RESOLVE_CARD_LABEL,
        DECLINED_RESOLVE_CLOSE_LABEL,
    ]


@pytest.mark.parametrize("held", [True, False])
def test_6_while_held_the_decline_points_only_at_close(held):
    """Under a standing resolve decline, the prompt line and the refusal
    point the user at "Close without resolving" only while a check holds the
    case: "Mark it resolved" would answer with the check (fix list 5)."""
    case = _held_case() if held else _held_case(asked=False)
    with_declined_offer(case, kind="resolve", basis="backstop")
    assert project_offers(case).reopen_chip is not None, "premise: chips built"
    prompt = get_prompt_for_case(case, "what now?", given_tools=DEFAULT_GIVEN)
    feedback = declined_resolution_reask(case)
    for text in (prompt, feedback):
        assert RESOLVE_DECLINED_CLOSE_ACTION in text
        assert (RESOLVE_DECLINED_REOPEN_ACTION in text) is not held
        assert (RESOLVE_DECLINED_ACTIONS in text) is not held


@pytest.mark.parametrize("asked", [True, False])
async def test_7_the_deferred_proposer_holds_on_a_pending_check(asked):
    """SUGGEST_RESOLVE and held: no offer, from it or the backstop; its CLOSE
    branch would record a false label. Without the ask it offers the
    resolve (the control)."""
    case = _confirm(_deferred_case(confirmed=False))
    if asked:
        case.evidence_needs.append(_need(case, 3, 3))
    assert closure_verdict(case) == ClosureReadiness.SUGGEST_RESOLVE, "premise"
    await _engine_with(_deferred_response()).process_turn(
        case=case, user_message="the platform team ships it", redaction_ctx=None
    )
    offer = case.offer_record.open
    if asked:
        assert offer is None
    else:
        assert (offer.kind, offer.basis) == ("resolve", "deferred")


def _engine_with(response) -> MilestoneEngine:
    engine = _engine()
    engine.generator.generate_structured_output = AsyncMock(return_value=response)
    return engine


@pytest.mark.parametrize("declined", [False, True])
async def test_11_a_held_case_persists_needs_info_and_the_close_it_would_get(
    declined,
):
    """Ruling 2 under the published contract: ``closed`` keeps the
    resolvable case's value, ``suggests_alternative`` (a close becomes a
    resolve proposal), or ``ready`` while the user's decline stands (#2360,
    fix list 3); the description says both."""
    case, _, _, _ = await _asked_at_7(_engine())
    if declined:
        with_declined_offer(case, kind="resolve", basis="backstop")
    CaseRepository.prepare_for_save(case)
    assert case.disposition_eligibility == {
        "resolved": "needs_info",
        "closed": "ready" if declined else "suggests_alternative",
    }
    assert case.disposition_eligibility == derive_disposition_eligibility(case)
    text = " ".join(DISPOSITION_ELIGIBILITY_DESCRIPTION.split())
    assert (
        "The exception is a resolvable case on which a check the agent asked for "
        "after the fix is unanswered: ``resolved`` reads ``needs_info`` there, "
        "and ``closed`` follows the resolvable case's rule above: "
        "``suggests_alternative`` (a close would become a resolve proposal), or "
        "``ready`` while the user's decline of the resolution stands." in text
    )


# ---------------------------------------------------------------------------
# 2. The predicate's shapes, through the real apply step
# ---------------------------------------------------------------------------


async def _legs(eng, case, turn, *extra, **fields):
    """Both legs at ``turn``: the cause re-checked and the problem gone."""
    return await _record(
        eng,
        case,
        turn,
        _obs(_CAUSE_GONE, "max_connections now reads 100", _SUPPORT),
        _obs(_GONE, "checkout returns 200 again", VERIFYING_ID),
        *extra,
        **fields,
    )


def _verdict(case) -> str:
    return assess_resolution_readiness(case).verdict


async def test_2_a_pre_fix_need_not_asked_again_does_not_hold():
    eng, case, *_ = _fixed(6)
    case.evidence_needs.append(_need(case, 3, 3, 4))
    await _legs(eng, case, 7)
    assert _verdict(case) == ResolutionReadiness.READY


async def test_2_the_same_need_re_surfaced_after_the_fix_holds():
    eng, case, *_ = _fixed(6)
    need = _need(case, 3, 3, 7)
    case.evidence_needs.append(need)
    await _legs(eng, case, 7)
    assert _verdict(case) == ResolutionReadiness.NEEDS_INFO
    assert assess_resolution_readiness(case).pending_check is need


async def test_2_superseded_releases_it():
    eng, case, *_ = _fixed(6)
    await _legs(eng, case, 7, evidence_need_updates=[_ask()])
    assert _verdict(case) == ResolutionReadiness.NEEDS_INFO, "premise: held"
    need = case.evidence_needs[-1]
    await _record(
        eng,
        case,
        8,
        evidence_need_updates=[
            _mark(need, "superseded", superseded_reason="no access to Prometheus")
        ],
    )
    assert _verdict(case) == ResolutionReadiness.READY


async def test_2_an_engine_inferred_causal_need_does_not_hold():
    """Ruling 4 holds on symptom needs: an EVIDENCE suggestion with no need
    is filed CAUSAL_VERIFICATION by design."""
    eng, case, *_ = _fixed(6)
    await _legs(eng, case, 7)
    case.evidence_needs.append(
        _need(case, 7, 7, purpose=NeedPurpose.CAUSAL_VERIFICATION, engine_inferred=True)
    )
    assert _verdict(case) == ResolutionReadiness.READY


async def test_2_an_answer_whose_data_predates_the_ask_does_not_release_it():
    eng, case, *_ = _fixed(6)
    await _legs(eng, case, 7, evidence_need_updates=[_ask()])
    early = upload(case, 7, "a turn-7 paste")
    await _record(
        eng,
        case,
        8,
        _obs(_GONE, "billing target UP", VERIFYING_ID, file_id=early),
    )
    assert _verdict(case) == ResolutionReadiness.NEEDS_INFO


async def test_2_a_re_ask_in_the_answers_turn_does_not_un_answer_it():
    """Critique point 2: "asked" is the first surfacing at or after the
    anchor, so the same need surfaced again at 8 is still asked at 7."""
    eng, case, *_ = _fixed(6)
    await _legs(eng, case, 7, evidence_need_updates=[_ask()])
    await _record(eng, case, 8, _obs(_GONE, "billing target UP", VERIFYING_ID))
    case.evidence_needs[-1].record_surfaced(8)
    assert _verdict(case) == ResolutionReadiness.READY


async def test_2_a_new_symptom_need_after_ready_holds():
    """Ruling 4: any symptom need asked after the fix, a new symptom
    included."""
    eng, case, *_ = _fixed(6)
    await _legs(eng, case, 7)
    assert _verdict(case) == ResolutionReadiness.READY, "premise"
    await _record(
        eng,
        case,
        8,
        evidence_need_updates=[
            EvidenceNeedUpdate(
                purpose="symptom_verification",
                request_text="the invoices queue drained",
                rationale="a new symptom",
            )
        ],
    )
    assert _verdict(case) == ResolutionReadiness.NEEDS_INFO


async def test_2_fulfilled_on_dangling_ids_is_demoted_and_still_holds():
    eng, case, *_ = _fixed(6)
    await _legs(eng, case, 7, evidence_need_updates=[_ask()])
    need = case.evidence_needs[-1]
    await _record(
        eng,
        case,
        8,
        evidence_need_updates=[_mark(need, "fulfilled", "ev_0000dang1e00")],
    )
    assert need.state == NeedState.PARTIALLY_MET
    assert _verdict(case) == ResolutionReadiness.NEEDS_INFO


async def test_2_a_need_declared_and_fulfilled_in_one_response_releases_it():
    """Fix list 1: the model judged the ask turn's own row the answer."""
    eng, case, *_ = _fixed(6)
    await _legs(
        eng,
        case,
        7,
        evidence_need_updates=[
            _ask(),
            EvidenceNeedUpdate(
                need_id="new_index_0",
                state="fulfilled",
                fulfilling_evidence_ids=["new_index_1"],
            ),
        ],
    )
    assert case.evidence_needs[-1].state == NeedState.FULFILLED, "premise"
    assert _verdict(case) == ResolutionReadiness.READY


async def test_2_a_missed_attachment_cited_the_next_turn_releases_it():
    """Fix list 1: the ask turn's upload held the answer, recorded at 8 from
    that file (its data dated 7, the ask's turn) and cited as fulfilling."""
    eng, case, *_ = _fixed(6)
    await _legs(eng, case, 7, evidence_need_updates=[_ask()])
    need = case.evidence_needs[-1]
    missed = upload(case, 7, "the targets screenshot")
    await _record(
        eng,
        case,
        8,
        _obs(_GONE, "Prometheus shows job=billing UP", file_id=missed),
        evidence_need_updates=[_mark(need, "fulfilled", "new_index_0")],
    )
    assert need.state == NeedState.FULFILLED, "premise"
    assert _verdict(case) == ResolutionReadiness.READY


async def test_2_without_the_problem_leg_nothing_is_held():
    """Fix list 6: the cause leg alone, and a need asked after it. Readiness
    asks for the problem's answer itself; the block marks nothing."""
    eng, case, *_ = _fixed(6)
    await _record(
        eng,
        case,
        7,
        _obs(_CAUSE_GONE, "max_connections now reads 100", _SUPPORT),
        evidence_need_updates=[_ask()],
    )
    assert pending_symptom_check(case) is None
    assert assess_resolution_readiness(case).pending_check is None


async def test_2_no_cause_leg_holds_nothing():
    """No anchor without a cause leg: readiness asks for the confirmation."""
    eng, case, *_ = _fixed(6)
    await _record(
        eng,
        case,
        7,
        _obs(_GONE, "checkout returns 200 again", VERIFYING_ID),
        evidence_need_updates=[_ask()],
    )
    assert pending_symptom_check(case) is None


# ---------------------------------------------------------------------------
# 9. #2495: a newer recorded fix nobody has observed yet
# ---------------------------------------------------------------------------


async def _relapsed(second_fix: bool):
    """The fix at 3; at 4 the cause re-checked and the problem gone; at 5 the
    problem back (a relapse, which disconfirms nothing: M6 mints no row);
    optionally a second fix at 6, not yet observed."""
    eng, case, *_ = _fixed(3)
    (cause, _), _ = await _record(
        eng,
        case,
        4,
        _obs(_CAUSE_GONE, "max_connections now reads 100", _SUPPORT),
        _obs(_GONE, "checkout returns 200 again", VERIFYING_ID),
    )
    await _record(eng, case, 5, _obs(_PRESENT, "checkout 503s again", VERIFYING_ID))
    if second_fix:
        _accept(case, InvestigationActionType.SOLUTION, 6)
    case.current_turn = 7
    return case, cause


async def test_9_the_reason_names_the_failed_fix_and_the_newer_one():
    case, cause = await _relapsed(second_fix=True)
    windows = fix_windows(case)
    assert [(w.fix_turn, w.state, w.observed) for w in windows[1:]] == [
        (3, "failed", True),
        (6, "unknown", False),
    ], "premise: the relapse failed fix 3; fix 6 is unobserved"
    assert latest_disconfirmation_turn(case) == -1, "premise: no M6 row"
    assert cause_recheck_status(case) == [
        (
            cause,
            "the fix executed at turn 3 was seen failing at turn 5; the newer "
            "fix (executed turn 6) is not re-checked yet",
        )
    ]
    # Readiness asks to confirm the newer fix, not for a new one.
    missing = assess_resolution_readiness(case).missing
    assert missing[0] == "confirmation the problem is now resolved"
    assert "a new fix after the one that did not hold" not in missing


async def test_9_with_no_newer_fix_the_reason_is_unchanged():
    case, cause = await _relapsed(second_fix=False)
    assert cause_recheck_status(case) == [
        (cause, "the latest fix was seen failing; only a new fix can confirm")
    ]
    assert assess_resolution_readiness(case).missing[0] == (
        "a new fix after the one that did not hold"
    )
