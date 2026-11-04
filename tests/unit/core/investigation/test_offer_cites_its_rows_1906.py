"""#1906 D5.1 and #2428: every resolve offer and close→resolve pivot says what
its rows show, citing them, and nothing stronger.

Every opener used to assert a cause "confirmed eliminated" and a problem that
"went with it", composed from a verdict and citing no row, so the engine told
the user "the problem went with it" in the same reply that asked them to
check whether it had (#1906). Each path below is driven through
``MilestoneEngine.process_turn`` with only the LLM seam replaced. Each offer
must carry the cause row's and the problem row's summaries with their turns,
name the verifying row nobody re-checked (here the Prometheus target), and
contain neither old phrase. The backstop keeps the "still open until you
confirm" clause INV-40's suppression rests on.

#2428: the READY confirmation asked "how you verified the fix worked", keyed
on a ``Solution.verification_method`` no investigation path writes, under a
fixed "Thanks for the additional details." whatever the user wrote.
"""

import pytest

from faultmaven.core.investigation.cause_assurance import (
    resolution_confirmation_rows,
    subject_index,
)
from faultmaven.core.investigation.offers import remember_resolve_request
from faultmaven.core.investigation.terminal_transitions import (
    ResolutionReadiness,
    assess_resolution_readiness,
    cause_identification_leg,
)
from faultmaven.modules.case.contracts import EvidenceCategory
from tests.case_builders import with_open_offer
from tests.unit.core.investigation import (
    test_declined_resolution_binds_the_model_1895 as t1895,
)
from tests.unit.core.investigation.bound_rows import bound_evidence, upload
from tests.unit.core.investigation.test_deferred_disposition_turn import (
    _case as _deferred_case,
)
from tests.unit.core.investigation.test_deferred_disposition_turn import (
    _deferred_response,
)
from tests.unit.core.investigation.test_resolution_backstop_turn import (
    _case,
    _confirmed_case,
    _engine,
    _recording,
    _silent_response,
)

pytestmark = pytest.mark.unit

#: The second signal the user reported, recorded as a verifying row and never
#: re-checked after the fix: #1906's scrape target.
_UNCHECKED = "Prometheus shows the billing scrape target DOWN"
_STILL_OPEN = "still open until you confirm"
_OLD = ("confirmed eliminated", "went with it")
_HOW_VERIFIED = "how you verified"
_THANKS = "Thanks for the additional details."


def _with_an_unchecked_signal(case):
    """``case`` plus a second row that showed the problem, which no post-fix
    row re-checks."""
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
    return case


def _ready_case():
    return _with_an_unchecked_signal(_confirmed_case())


def _assert_cites_its_rows(text: str, case) -> None:
    rows = resolution_confirmation_rows(case)
    categories = {row.category for row in rows}
    assert categories == {
        EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
        EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE,
    }, "premise: both legs stand"
    subjects = subject_index(case)
    for row in rows:
        assert row.summary.rstrip(".") in text, row.category
        assert f"(turn {subjects.observed(row)})" in text
    assert "**The cause** —" in text
    assert "**The problem** —" in text
    assert f"**Not re-checked since the fix** — {_UNCHECKED}" in text
    for phrase in _OLD:
        assert phrase not in text


async def _backstop(*, root: bool):
    case = _ready_case()
    if not root:
        case.root_cause_conclusion = None
    result = await _engine(_silent_response()).process_turn(
        case=case, user_message="the errors are gone", redaction_ctx=None
    )
    assert (cause_identification_leg(case) is not None) is root, "premise"
    assert case.offer_record.open.basis == "backstop"
    return result["agent_response"], case


async def _deferred_pivot():
    case = _with_an_unchecked_signal(_deferred_case(confirmed=True))
    result = await _engine(_deferred_response()).process_turn(
        case=case, user_message="the platform team ships it", redaction_ctx=None
    )
    assert (case.offer_record.open.kind, case.offer_record.open.basis) == (
        "resolve",
        "deferred",
    )
    return result["agent_response"], case


async def _model_proposes(to_state: str):
    case = _ready_case()
    engine = t1895._engine()
    await t1895._turn(engine, case, "all clear", propose=to_state)
    offer = case.offer_record.open
    assert (offer.kind, offer.basis) == ("resolve", "model")
    # The model's offer composes no prose; its summary is what the user is
    # shown on a re-ask (``_pending_transition_reask``).
    return offer.summary, case


async def _menu_pivot():
    case = _ready_case()
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


async def _confirm_time_pivot():
    case = _ready_case()
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


async def _step_zero_ready():
    """#2428's turn-13 shape (c1030-o3): the user was asked for the problem's
    check, and this turn pastes it; the model records the re-check, and step
    0 finds the request met."""
    case = _with_an_unchecked_signal(
        _case(absence=EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE)
    )
    case.offer_record = remember_resolve_request(
        case, ["confirmation the problem is now resolved"]
    )
    case.current_turn += 1
    verifying = case.problem_verification.verifying_evidence_ids[0]
    engine = _engine(
        _recording(
            "symptom_absence_evidence",
            "AssumeRoleWithWebIdentity succeeds for the data-processor pods",
            "AssumeRoleWithWebIdentity: 200 OK",
            verifying,
        )
    )
    result = await engine.process_turn(
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


async def _reopen_chip():
    case = _ready_case()
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
    "backstop with a root": lambda: _backstop(root=True),
    "backstop without a root": lambda: _backstop(root=False),
    "deferred pivot": _deferred_pivot,
    "model closed pivot": lambda: _model_proposes("closed"),
    "menu pivot": _menu_pivot,
    "confirm-time pivot": _confirm_time_pivot,
    "step 0 READY": _step_zero_ready,
    "model resolved READY": lambda: _model_proposes("resolved"),
    "reopen chip": _reopen_chip,
}

#: The paths that compose the READY confirmation (#2428).
_CONFIRMATIONS = ("step 0 READY", "model resolved READY", "reopen chip")


@pytest.mark.parametrize("path", list(_PATHS))
async def test_every_resolve_offer_cites_the_rows_that_earn_it(path):
    text, case = await _PATHS[path]()
    _assert_cites_its_rows(text, case)


@pytest.mark.parametrize("root", [True, False])
async def test_the_backstop_keeps_its_still_open_clause(root):
    """INV-40's suppression rests on the clause, not on the offer's other
    words (``turn_completion._narration_overclaim_notice``)."""
    text, _ = await _backstop(root=root)
    assert _STILL_OPEN in text
    assert text.rstrip().endswith("Shall I mark this case resolved?")


async def test_a_backstop_with_no_cause_on_record_names_no_root():
    text, case = await _backstop(root=False)
    assert "**The cause** — no root cause on record:" in text
    for node in case.causal_nodes.values():
        assert node.statement not in text


async def test_the_deferred_pivot_keeps_its_out_of_band_reason():
    text, _ = await _deferred_pivot()
    assert "out-of-band" in text
    assert "Closing would" not in text


@pytest.mark.parametrize("path", ["menu pivot", "confirm-time pivot"])
async def test_a_close_pivot_keeps_what_closing_would_do(path):
    text, _ = await _PATHS[path]()
    assert "Closing would close it without marking it resolved" in text


@pytest.mark.parametrize("path", _CONFIRMATIONS)
async def test_the_confirmation_asks_nothing_the_record_holds(path):
    """#2428: no "how you verified" hint and no fixed thanks; the commands
    hint, which the record does not hold, stays."""
    text, _ = await _PATHS[path]()
    assert _HOW_VERIFIED not in text
    assert _THANKS not in text
    assert "specific commands or steps you used" in text
