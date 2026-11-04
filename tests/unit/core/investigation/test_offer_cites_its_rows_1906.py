"""#1906 D5.1 and #2428: every resolve offer and close→resolve pivot says what
its rows show, citing them, and nothing stronger.

Every opener used to assert a cause "confirmed eliminated" and a problem that
"went with it", composed from a verdict and citing no row, so the engine told
the user "the problem went with it" in the same reply that asked them to
check whether it had (#1906). Each path below is driven through
``MilestoneEngine.process_turn`` with only the LLM seam replaced. Each offer
must carry the cause row's and the problem row's summaries, each with its own
turn, name the verifying row nobody re-checked (here the Prometheus target),
and contain neither old phrase. Whether a root cause is on record is one
answer on every surface (``cause_identification_leg``). The backstop keeps
the "still open until you confirm" clause INV-40's suppression rests on.

#2428: the READY confirmation asked "how you verified the fix worked", keyed
on a ``Solution.verification_method`` no investigation path writes, and step
0's reply opened with a fixed "Thanks for the additional details." whatever
the user wrote.
"""

from unittest.mock import patch

import pytest

from faultmaven.core.investigation import terminal_transitions
from faultmaven.core.investigation.cause_assurance import (
    resolution_confirmation_rows,
    subject_index,
)
from faultmaven.core.investigation.offers import remember_resolve_request
from faultmaven.core.investigation.schemas import InvestigationResponse_Diagnosis
from faultmaven.core.investigation.terminal_transitions import (
    ResolutionReadiness,
    assess_closure_readiness,
    assess_resolution_readiness,
    cause_identification_leg,
    closure_verdict,
)
from faultmaven.modules.case.contracts import EvidenceCategory
from tests.case_builders import with_open_offer
from tests.unit.core.investigation import (
    test_declined_resolution_binds_the_model_1895 as t1895,
)
from tests.unit.core.investigation.bound_rows import bound_evidence, bound_row, upload
from tests.unit.core.investigation.test_deferred_disposition_turn import (
    _case as _deferred_case,
)
from tests.unit.core.investigation.test_deferred_disposition_turn import (
    _deferred_response,
)
from tests.unit.core.investigation.test_resolution_backstop_turn import (
    LLM_ANALYSIS,
    _case,
    _engine,
    _silent_response,
)

pytestmark = pytest.mark.unit

#: The second signal the user reported, recorded as a verifying row and never
#: re-checked after the fix: #1906's scrape target.
_UNCHECKED = "Prometheus shows the billing scrape target DOWN"
_CAUSE_ROW = "ExecStart now names /usr/bin/billing-exporter and the unit runs"
_PROBLEM_ROW = "AssumeRoleWithWebIdentity succeeds for the data-processor pods"
_STILL_OPEN = "still open until you confirm"
_OLD = ("confirmed eliminated", "went with it")


def _confirm(case, *, problem: bool = True):
    """The legs at distinct data turns (the cause at 2, the problem at 3), and
    a second row that showed the problem, which nothing re-checks."""
    bound_row(
        case, EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE, turn=2, summary=_CAUSE_ROW
    )
    if problem:
        bound_row(
            case,
            EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE,
            turn=3,
            summary=_PROBLEM_ROW,
        )
    row = bound_evidence(
        EvidenceCategory.SYMPTOM_EVIDENCE,
        turn=1,
        evidence_id="ev_00000000fe02",
        rechecks=None,
        summary=_UNCHECKED,
        source_file_id=upload(case, 1),
    )
    case.evidence.append(row)
    case.problem_verification.verifying_evidence_ids.append(row.evidence_id)
    case.current_turn = 3
    return case


def _ready_case(leg: bool = True, *, problem: bool = True):
    case = _confirm(_case(absence=None), problem=problem)
    if not leg:
        # The count-held root stays in the cause pool, so READY holds; no
        # cause-identification leg names a root cause.
        case.root_cause_conclusion = None
    return case


def _root_statements(case) -> list[str]:
    return [n.statement for n in case.causal_nodes.values()]


def _cause_texts(case) -> list[str]:
    """Everything a line could name as the cause: the roots, the hypotheses,
    the working conclusion and the conclusion."""
    texts = _root_statements(case) + [h.statement for h in case.hypotheses.values()]
    for record, field in (
        (case.working_conclusion, "statement"),
        (case.root_cause_conclusion, "root_cause"),
    ):
        if getattr(record, field, None):
            texts.append(getattr(record, field))
    return texts


def _assert_cites_its_rows(text: str, case, leg: bool) -> None:
    rows = resolution_confirmation_rows(case)
    subjects = subject_index(case)
    turns = {row.category: subjects.observed(row) for row in rows}
    assert len(set(turns.values())) == 2, "premise: the legs at distinct turns"
    lines = text.splitlines()
    for row in rows:
        (line,) = [ln for ln in lines if row.summary.rstrip(".") in ln]
        assert line.endswith(f"(turn {subjects.observed(row)})"), line
    (cause,) = [ln for ln in lines if ln.startswith("- **The cause** —")]
    assert any(ln.startswith("- **The problem** —") for ln in lines)
    assert f"- **Not re-checked since the fix** — {_UNCHECKED}" in lines
    if leg:
        assert any(statement in cause for statement in _root_statements(case))
    else:
        # One source for the cause on every line (``cause_identification_leg``).
        assert cause.startswith("- **The cause** — no root cause on record:")
        for statement in _cause_texts(case):
            assert statement not in text
        for line in lines:
            if line.startswith("- **Root cause**:"):
                assert line == "- **Root cause**: no root cause on record"
    for phrase in _OLD:
        assert phrase not in text


async def _backstop(leg: bool):
    case = _ready_case(leg)
    result = await _engine(_silent_response()).process_turn(
        case=case, user_message="the errors are gone", redaction_ctx=None
    )
    assert case.offer_record.open.basis == "backstop"
    return result["agent_response"], case


async def _deferred_pivot(leg: bool):
    case = _confirm(_deferred_case(confirmed=False))
    result = await _engine(_deferred_response()).process_turn(
        case=case, user_message="the platform team ships it", redaction_ctx=None
    )
    assert (case.offer_record.open.kind, case.offer_record.open.basis) == (
        "resolve",
        "deferred",
    )
    return result["agent_response"], case


async def _model_proposes(to_state: str, leg: bool):
    case = _ready_case(leg)
    await t1895._turn(t1895._engine(), case, "all clear", propose=to_state)
    offer = case.offer_record.open
    assert (offer.kind, offer.basis) == ("resolve", "model")
    # The model's offer composes no prose; its summary is what the user is
    # shown on a re-ask (``_pending_transition_reask``).
    return offer.summary, case


async def _menu_pivot(leg: bool):
    case = _ready_case(leg)
    result = await t1895._turn(
        t1895._engine(),
        case,
        "close it",
        intent_type="status_transition",
        intent_data={"to_state": "closed"},
    )
    assert (case.offer_record.open.kind, case.offer_record.open.basis) == (
        "resolve",
        "menu",
    )
    return result["agent_response"], case


async def _confirm_time_pivot(leg: bool):
    case = _ready_case(leg)
    key = with_open_offer(case, kind="close", basis="model").proposal_id
    result = await t1895._turn(
        t1895._engine(),
        case,
        "yes",
        intent_type="confirmation",
        intent_data={"value": True, "proposal_id": key},
    )
    assert case.offer_record.open.pivoted_from_close
    return result["agent_response"], case


async def _step_zero_ready(leg: bool):
    """#2428's turn-13 shape (c1030-o3): the user was asked for the problem's
    check, and this turn pastes it; the model records the re-check, and step
    0 finds the request met."""
    case = _ready_case(leg, problem=False)
    case.offer_record = remember_resolve_request(
        case, ["confirmation the problem is now resolved"]
    )
    case.current_turn = 4
    response = InvestigationResponse_Diagnosis(
        agent_response=LLM_ANALYSIS,
        state_updates={
            "evidence_to_add": [
                {
                    "summary": _PROBLEM_ROW,
                    "extract": "AssumeRoleWithWebIdentity: 200 OK",
                    "category": "symptom_absence_evidence",
                    "source_type": "logs",
                    "source_file_id": upload(case, 4),
                    "rechecks": case.problem_verification.verifying_evidence_ids[0],
                }
            ]
        },
    )
    result = await _engine(response).process_turn(
        case=case,
        user_message="Confirmation the original problem is gone:\n<pasted check>",
        redaction_ctx=None,
    )
    assert assess_resolution_readiness(case).verdict == ResolutionReadiness.READY
    assert (case.offer_record.open.kind, case.offer_record.open.basis) == (
        "resolve",
        "model",
    )
    return result["agent_response"], case


async def _reopen_chip(leg: bool):
    case = _ready_case(leg)
    engine = t1895._engine()
    await t1895._turn(engine, case, "errors gone")
    await t1895._turn(engine, case, "no")
    result = await t1895._turn(
        engine,
        case,
        "Mark this case resolved.",
        intent_type="status_transition",
        intent_data=t1895._chip_intent(case),
    )
    assert case.offer_record.open.basis == "reopen"
    return result["agent_response"], case


_PATHS = {
    "backstop": _backstop,
    "deferred pivot": _deferred_pivot,
    "model closed pivot": lambda leg: _model_proposes("closed", leg),
    "menu pivot": _menu_pivot,
    "confirm-time pivot": _confirm_time_pivot,
    "step 0 READY": _step_zero_ready,
    "model resolved READY": lambda leg: _model_proposes("resolved", leg),
    "reopen chip": _reopen_chip,
}

#: Every surface on both shapes, except the deferred pivot without a leg: the
#: deferred proposer offers only on an identified cause
#: (``stage_gates._solution_cause_validated``, which reads the same leg).
_SHAPES = [
    (path, leg)
    for path in _PATHS
    for leg in (True, False)
    if leg or path != "deferred pivot"
]

#: The paths that compose the READY confirmation (#2428).
_CONFIRMATIONS = ("step 0 READY", "model resolved READY", "reopen chip")


@pytest.mark.parametrize("path, leg", _SHAPES)
async def test_every_resolve_offer_cites_the_rows_that_earn_it(path, leg):
    text, case = await _PATHS[path](leg)
    assert (cause_identification_leg(case) is not None) is leg, "premise"
    _assert_cites_its_rows(text, case, leg)


@pytest.mark.parametrize("leg", [True, False])
async def test_the_backstop_keeps_its_still_open_clause(leg):
    """INV-40's suppression rests on the clause, not on the offer's other
    words (``turn_completion._narration_overclaim_notice``)."""
    text, _ = await _backstop(leg)
    assert _STILL_OPEN in text
    assert text.rstrip().endswith("Shall I mark this case resolved?")


async def test_the_deferred_pivot_keeps_its_out_of_band_reason():
    text, _ = await _deferred_pivot(True)
    assert "out-of-band" in text
    assert "Closing would" not in text


@pytest.mark.parametrize("path", ["menu pivot", "confirm-time pivot"])
@pytest.mark.parametrize("leg", [True, False])
async def test_a_close_pivot_states_only_what_the_record_holds(path, leg):
    """It keeps what closing would do; the root-cause line renders only with
    a conclusion on record, never as a placeholder."""
    text, _ = await _PATHS[path](leg)
    assert "Closing would close it without marking it resolved" in text
    assert ("- **Root cause**:" in text) is leg
    assert "the identified root cause" not in text


def test_a_contested_conclusion_names_no_root_in_the_pivot():
    """A conclusion is on record, but contested: no leg stands on it."""
    case = _ready_case()
    case.progress.cause_identification_contested = True
    assert cause_identification_leg(case) is None, "premise"
    message = assess_closure_readiness(case).message
    assert "- **The cause** — no root cause on record:" in message
    assert "**Root cause**" not in message
    for text in _cause_texts(case):
        assert text not in message


def test_the_pivot_claims_a_confirmed_fix_only_with_one_on_record():
    case = _ready_case(False)
    case.solutions = []
    message = assess_closure_readiness(case).message
    assert "Closing would close it without marking it resolved.\n" in message
    assert "the confirmed fix stays on record" not in message


def test_a_verdict_only_read_composes_nothing():
    """Disposition eligibility reads the closure verdict on every save; the
    pivot's rows are composed only when its message is read."""
    case = _ready_case()
    with patch.object(
        terminal_transitions, "resolution_record_lines", return_value="ROWS"
    ) as compose:
        assert closure_verdict(case) == "suggest_resolve"
        readiness = assess_closure_readiness(case)
        compose.assert_not_called()
        assert "ROWS" in readiness.message
        assert "ROWS" in readiness.message
    compose.assert_called_once_with(case)


@pytest.mark.parametrize("path", _CONFIRMATIONS)
async def test_the_confirmation_asks_nothing_the_record_holds(path):
    """#2428: no "how you verified" hint; the commands hint, which the record
    does not hold, stays."""
    text, _ = await _PATHS[path](True)
    assert "how you verified" not in text
    assert "specific commands or steps you used" in text


async def test_step_zero_ready_opens_with_no_fixed_thanks():
    """#2428: the one path that carried the prefix (``turn_completion``)."""
    text, _ = await _step_zero_ready(True)
    assert "Thanks for the additional details." not in text
    assert text.startswith(LLM_ANALYSIS)
    assert "Here's what I have on record:" in text
