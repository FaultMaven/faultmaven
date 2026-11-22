"""The held check's notice and the offer prose state the cycle's record
(#2811 S2b-2c, #2615 stage S2; ruling (a) of 2026-11-22).

Switched: ``PENDING_CHECK``, ``RCA_INFEASIBLE_CLOSE``, ``BACKSTOP_RESOLVE``,
``FALSE_ALARM_CLOSE``, ``DEFERRED_CLOSE``, ``NEEDS_INFO_UNMET_CLOSE``,
``RESOLUTION_NEEDS_INFO`` and the model proposal's readiness ask: each takes
the outcome facts its text states from ``case.progress.outcome``, the need's
request text through the narrators' text accessor (``content_by_id``).
Kept until stage S3: ``REQUEST_READY``, ``DEFERRED_RESOLVE`` and the model
proposal's confirmation and closure, which render their decider's params.
Where the record and the decider agree the text is ``main``'s (the engine and
narration suites, unchanged); the conflict counter counts as on ``main``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from faultmaven.core.investigation.milestone_engine import turn_narration
from faultmaven.core.investigation.milestone_engine.turn_narration import (
    OUTCOME_WRITTEN_KEY,
    narrate_turn,
)
from faultmaven.core.investigation.narration.codes import (
    DECISIONS_KEY,
    NoticeCode,
    OfferCode,
)
from faultmaven.core.investigation.narration.notices import pending_check_feedback
from faultmaven.core.investigation.narration.offers import (
    readiness_message,
    render_offer,
)
from faultmaven.core.investigation.offers import offer_view
from faultmaven.core.investigation.outcome import (
    narration_outcome,
    record_case_outcome,
)
from faultmaven.modules.case.contracts import (
    ClosureEligibility,
    ConfirmationGap,
    PendingCheckReason,
    ProblemStatus,
    ResolutionReason,
    WritePoint,
)
from tests.unit.core.investigation import golden_cases_2636 as golden

pytestmark = pytest.mark.unit

_CODE = NoticeCode.PENDING_CHECK

#: Each switched code's scenario: the decision cycle, before its record.
SWITCHED = {
    "rca_infeasible_close": golden._rca_infeasible_relieved,
    "backstop_resolve": lambda: golden._backstop_cycle(golden._confirmed_case()),
    "false_alarm_close": golden._false_alarm_close,
    "deferred_close": lambda: golden._deferred(confirmed=False),
    "needs_info_unmet_close": lambda: golden._requested(golden._bare_case()),
    "resolution_needs_info": lambda: golden._models_proposal(
        golden._identified_case()[0], "resolved"
    ),
    "model_proposal_readiness": lambda: golden._models_proposal(
        golden._bare_case(), "resolved"
    ),
    # The held check: step 2's refusal logs PENDING_CHECK and its
    # RESOLUTION_NEEDS_INFO, which names the need's request text.
    "pending_check": lambda: golden._models_proposal(golden._held(), "resolved"),
}

#: Each kept code's scenario.
KEPT = {
    "request_ready": lambda: golden._requested(golden._confirmed_case()),
    "deferred_resolve": lambda: golden._deferred(confirmed=True),
    "model_proposal_confirmation": lambda: golden._models_proposal(
        golden._confirmed_case(), "resolved"
    ),
    "model_proposal_closure": lambda: golden._models_proposal(
        golden._identified_case()[0], "closed", "close it"
    ),
}


def _entries(metadata: dict) -> list:
    return metadata[DECISIONS_KEY]


def _offer(metadata: dict) -> dict:
    """The cycle's one offer decision."""
    (entry,) = [e for e in _entries(metadata) if e["code"] in set(OfferCode)]
    return entry


def _spy(monkeypatch) -> list:
    """Each offer prose ``narrate_turn`` renders, as ``(code, text)``."""
    rendered: list = []

    def _render(code, params):
        text = render_offer(code, params)
        rendered.append((code, text))
        return text

    monkeypatch.setattr(turn_narration, "render_offer", _render)
    return rendered


def _stored(case, metadata: dict) -> None:
    """The cycle's record, written as ``record_case_outcome`` writes it."""
    metadata[OUTCOME_WRITTEN_KEY] = record_case_outcome(case, at=WritePoint.CYCLE)
    assert metadata[OUTCOME_WRITTEN_KEY], "premise: the record was written"


def _held_notice(metadata: dict) -> list[str]:
    return [
        n
        for n in metadata.get("system_notices") or []
        if n.startswith(("RESOLUTION HELD", "RESOLVED NOT PROPOSED"))
    ]


def _second_need(case, request_text: str = "the B check: billing p99 under 200ms"):
    """A need beside the held one, its own id and request text."""
    need = case.evidence_needs[-1].model_copy(
        update={"need_id": "eneed_00000000b0b0", "request_text": request_text}
    )
    case.evidence_needs.append(need)
    return need


# ---------------------------------------------------------------------------
# Pin 2: the switched prose renders from the record on a poisoned case
# ---------------------------------------------------------------------------


class _Poisoned:
    """A case on which every attribute read raises but the record, the
    identity fields and ``content_by_id``'s reads (a proposed action's
    description, a need's request text)."""

    _ALLOWED = {
        "case_id",
        "state",
        "current_stage",
        "current_turn",
        "proposed_actions",
        "evidence_needs",
    }

    def __init__(self, case) -> None:
        object.__setattr__(self, "_case", case)
        object.__setattr__(
            self, "progress", SimpleNamespace(outcome=case.progress.outcome)
        )

    def __getattribute__(self, name: str):
        if name == "progress":
            return object.__getattribute__(self, name)
        if name in type(self)._ALLOWED:
            return getattr(object.__getattribute__(self, "_case"), name)
        raise AssertionError(f"the narration read case.{name}")


#: A decider's params that disagree with the record on every fact the code's
#: text states from it: the poisoned narration can state the record's only by
#: reading it.
_STALE_READINESS = {
    "reason": ResolutionReason.FIX_FAILED,
    "missing": [],
    "gaps": [],
    "request_text": "a stale ask",
    "off_cause_turn": 99,
}
_STALE = {
    OfferCode.RCA_INFEASIBLE_CLOSE: {"closure_line": "a stale closure line"},
    OfferCode.BACKSTOP_RESOLVE: {
        "record_lines": "- a stale record line",
        "identification_leg": None,
        "reason": ResolutionReason.PENDING_CHECK,
    },
    OfferCode.FALSE_ALARM_CLOSE: {"problem_status": ProblemStatus.VERIFIED},
    OfferCode.DEFERRED_CLOSE: {},
    OfferCode.NEEDS_INFO_UNMET_CLOSE: _STALE_READINESS,
    OfferCode.RESOLUTION_NEEDS_INFO: _STALE_READINESS,
    OfferCode.MODEL_PROPOSAL: _STALE_READINESS,
    NoticeCode.PENDING_CHECK: {
        "need_id": "eneed_stale",
        "request_text": "a stale ask",
        "pending_check_reason": PendingCheckReason.FULFILLED_BEFORE_ASK,
        "declined_stands": True,
    },
}


@pytest.mark.parametrize("name", sorted(SWITCHED))
def test_the_switched_prose_renders_from_the_record_alone(monkeypatch, name):
    """Pin 2: with the decider's facts stale, ``narrate_turn`` on a case that
    lets it read only the record, the identity and ``content_by_id``'s reads
    renders the text the agreeing decider would have: the record's. The offer
    record is read through its own exempt accessor (``OfferView``, S2b-2d)
    and written through ``summarize_offer``, both stubbed here.
    ``FALSE_ALARM_CLOSE`` and ``DEFERRED_CLOSE`` state no record fact in
    their text, so theirs is the same either way."""
    case, metadata = SWITCHED[name]()
    _stored(case, metadata)
    view = offer_view(case)
    monkeypatch.setattr(turn_narration, "offer_view", lambda _case: view)
    monkeypatch.setattr(turn_narration, "summarize_offer", lambda *_a: True)
    rendered = _spy(monkeypatch)
    offer = _offer(metadata)
    want_prose = [(offer["code"], render_offer(offer["code"], offer["params"]))]
    held = [e for e in _entries(metadata) if e["code"] == _CODE]
    want_notices = [
        pending_check_feedback(
            p["need_id"],
            p["request_text"],
            pending_check_reason=p["pending_check_reason"],
            proposed=p["proposed"],
            declined_stands=p["declined_stands"],
        )
        for p in (e["params"] for e in held)
    ]
    for entry in _entries(metadata):
        entry["params"] = {**entry["params"], **_STALE.get(entry["code"], {})}

    narrate_turn(_Poisoned(case), metadata)

    assert rendered == want_prose
    assert _held_notice(metadata) == want_notices
    assert bool(held) == (name == "pending_check")


# ---------------------------------------------------------------------------
# The record wins: a record that disagrees with the decider is stated
# ---------------------------------------------------------------------------


def _edited(record, section: str, **fields):
    setattr(record, section, getattr(record, section).model_copy(update=fields))


_RECORD_WINS = {
    "rca_infeasible_close": (
        lambda r: _edited(r, "closure", statement="the record's closure line"),
        lambda p: "The record's closure line. Since "
        f"{p['rationale']}, shall we close this case?",
    ),
    "backstop_resolve": (
        lambda r: (
            _edited(r, "resolution", statement="- the record's line"),
            _edited(r, "cause", identification_leg=None),
        ),
        lambda p: "The record shows:\n\n- the record's line\n\nThe case is still "
        "open until you confirm: marking it resolved writes up what happened. "
        "Shall I mark this case resolved?",
    ),
    "needs_info_unmet_close": (
        lambda r: _edited(
            r, "resolution", reason=ResolutionReason.FIX_OFF_CAUSE, off_cause_turn=4
        ),
        lambda p: readiness_message(ResolutionReason.FIX_OFF_CAUSE, off_cause_turn=4),
    ),
    "resolution_needs_info": (
        lambda r: _edited(
            r,
            "resolution",
            missing=["root cause"],
            confirmation_gaps=[ConfirmationGap.NO_SYMPTOM_ROW],
        ),
        lambda p: readiness_message(
            ResolutionReason.DOCUMENTATION_GAPS,
            missing=["root cause"],
            gaps=[ConfirmationGap.NO_SYMPTOM_ROW],
        ),
    ),
    "model_proposal_readiness": (
        lambda r: _edited(r, "resolution", reason=ResolutionReason.FALSE_ALARM),
        lambda p: readiness_message(ResolutionReason.FALSE_ALARM),
    ),
}


@pytest.mark.parametrize("name", sorted(_RECORD_WINS))
def test_the_switched_prose_states_the_record_over_the_decider(monkeypatch, name):
    """The stored record disagrees with what the decider read on a fact the
    text states; the text states the record's. The conflict is still counted,
    as on ``main``."""
    edit, want = _RECORD_WINS[name]
    case, metadata = SWITCHED[name]()
    entry = _offer(metadata)
    _stored(case, metadata)
    edit(case.progress.outcome)
    rendered = _spy(monkeypatch)
    narrate_turn(case, metadata)
    assert rendered == [(entry["code"], want(entry["params"]))]
    assert rendered[0][1] != render_offer(entry["code"], entry["params"]), "premise"


def test_the_readiness_ask_names_the_records_held_need(monkeypatch):
    """The needs-info request names the request text of the need the record
    holds the resolution for, read through ``content_by_id``."""
    case, metadata = SWITCHED["pending_check"]()
    entry = _offer(metadata)
    assert entry["code"] == OfferCode.RESOLUTION_NEEDS_INFO, "premise"
    need = _second_need(case)
    _stored(case, metadata)
    _edited(case.progress.outcome, "resolution", pending_check_need_id=need.need_id)
    rendered = _spy(monkeypatch)
    narrate_turn(case, metadata)
    assert rendered == [
        (
            entry["code"],
            readiness_message(
                ResolutionReason.PENDING_CHECK, request_text=need.request_text
            ),
        )
    ]


def test_the_record_params_of_the_codes_whose_text_states_none():
    """``FALSE_ALARM_CLOSE`` takes the problem's status from the record and
    ``DEFERRED_CLOSE`` takes nothing; neither text states it, so neither
    text can differ from the decider's."""
    from faultmaven.core.investigation.narration.offers import record_params

    case, _metadata = SWITCHED["false_alarm_close"]()
    record = narration_outcome(case)
    _edited(record, "problem", status=ProblemStatus.REVISION_PENDING)
    params = {"rationale": "r", "problem_status": ProblemStatus.INVALIDATED}
    assert record_params(OfferCode.FALSE_ALARM_CLOSE, params, record, None) == {
        "rationale": "r",
        "problem_status": ProblemStatus.REVISION_PENDING,
    }
    params = {"proposal_id": "p", "verdict": ClosureEligibility.HAS_SUBSTANCE}
    assert record_params(OfferCode.DEFERRED_CLOSE, params, record, "x") is params


# ---------------------------------------------------------------------------
# Kept until S3: the decider's params, whatever the record says
# ---------------------------------------------------------------------------


def _disagree(record) -> None:
    """A record that disagrees with every kept decider on each fact it
    decides or states."""
    _edited(record, "resolution", statement="- the record's line")
    _edited(record, "resolution", reason=ResolutionReason.DOCUMENTATION_GAPS)
    _edited(
        record,
        "cause",
        identification_leg=None if record.cause.identification_leg else "chain",
    )
    _edited(
        record,
        "closure",
        eligibility=(
            ClosureEligibility.TRIVIAL
            if record.closure.eligibility != ClosureEligibility.TRIVIAL
            else ClosureEligibility.HAS_SUBSTANCE
        ),
    )


@pytest.mark.parametrize("name", sorted(KEPT))
def test_the_kept_prose_renders_the_deciders_params(monkeypatch, name):
    case, metadata = KEPT[name]()
    entry = _offer(metadata)
    _stored(case, metadata)
    _disagree(case.progress.outcome)
    rendered = _spy(monkeypatch)
    narrate_turn(case, metadata)
    assert rendered == [(entry["code"], render_offer(entry["code"], entry["params"]))]
    assert "the record's line" not in rendered[0][1]


# ---------------------------------------------------------------------------
# The held check's notice
# ---------------------------------------------------------------------------


@pytest.fixture
def held():
    """The backstop's hold: a PENDING_CHECK alone, its record stored."""
    case, metadata = golden._backstop_cycle(golden._held())
    assert [e["code"] for e in _entries(metadata)] == [_CODE], "premise"
    return case, metadata


def test_a_record_holding_no_check_renders_no_notice(held):
    case, metadata = held
    _stored(case, metadata)
    _edited(case.progress.outcome, "resolution", pending_check_need_id=None)
    narrate_turn(case, metadata)
    assert _held_notice(metadata) == []


def test_the_notice_names_the_need_the_record_holds(held):
    """The decider named need A; the record holds need B: B is named, with
    its request text, why it holds and whether a decline stands, all the
    record's."""
    case, metadata = held
    (decided,) = [e["params"] for e in _entries(metadata)]
    need = _second_need(case)
    _stored(case, metadata)
    _edited(
        case.progress.outcome,
        "resolution",
        pending_check_need_id=need.need_id,
        pending_check_reason=PendingCheckReason.FULFILLED_BEFORE_ASK,
        declined_stands=True,
    )
    narrate_turn(case, metadata)
    assert decided["need_id"] != need.need_id, "premise"
    assert _held_notice(metadata) == [
        pending_check_feedback(
            need.need_id,
            need.request_text,
            pending_check_reason=PendingCheckReason.FULFILLED_BEFORE_ASK,
            proposed=False,
            declined_stands=True,
        )
    ]


def _no_outcome(monkeypatch) -> None:
    def _boom(_case):
        raise RuntimeError("no outcome")

    monkeypatch.setattr(turn_narration, "narration_outcome", _boom)


def test_with_no_outcome_the_notice_and_prose_are_the_deciders(monkeypatch):
    """The record write failed and so did the in-memory pass: the held check
    and its needs-info request render from the decider's params."""
    case, metadata = SWITCHED["pending_check"]()
    entry = _offer(metadata)
    (decided,) = [e["params"] for e in _entries(metadata) if e["code"] == _CODE]
    _no_outcome(monkeypatch)
    rendered = _spy(monkeypatch)
    narrate_turn(case, metadata)
    assert rendered == [(entry["code"], render_offer(entry["code"], entry["params"]))]
    assert _held_notice(metadata) == [
        pending_check_feedback(
            decided["need_id"],
            decided["request_text"],
            pending_check_reason=decided["pending_check_reason"],
            proposed=decided["proposed"],
            declined_stands=decided["declined_stands"],
        )
    ]


# ---------------------------------------------------------------------------
# The failed write: the in-memory record (``READS_THE_RECORD``)
# ---------------------------------------------------------------------------


def _in_memory(monkeypatch, edit) -> list:
    """``narration_outcome`` returns the case's outcome edited by ``edit``;
    returns its calls."""
    calls: list = []

    def _outcome(case):
        calls.append(1)
        record = narration_outcome(case)
        edit(record)
        return record

    monkeypatch.setattr(turn_narration, "narration_outcome", _outcome)
    return calls


def test_a_failed_write_renders_a_lone_offer_from_the_in_memory_record(
    monkeypatch,
):
    """A cycle that logs only the model's readiness ask, its record write
    failed: the narration computes the record in memory and states it."""
    case, metadata = SWITCHED["model_proposal_readiness"]()
    entry = _offer(metadata)
    assert [e["code"] for e in _entries(metadata)] == [entry["code"]], "premise"
    metadata[OUTCOME_WRITTEN_KEY] = False
    calls = _in_memory(
        monkeypatch,
        lambda r: _edited(r, "resolution", reason=ResolutionReason.FALSE_ALARM),
    )
    rendered = _spy(monkeypatch)
    narrate_turn(case, metadata)
    assert calls == [1]
    assert rendered == [
        (entry["code"], readiness_message(ResolutionReason.FALSE_ALARM))
    ]


def test_a_failed_write_renders_a_lone_held_check_from_the_in_memory_record(
    monkeypatch, held
):
    case, metadata = held
    metadata[OUTCOME_WRITTEN_KEY] = False
    calls = _in_memory(
        monkeypatch, lambda r: _edited(r, "resolution", declined_stands=True)
    )
    (decided,) = [e["params"] for e in _entries(metadata)]
    assert decided["declined_stands"] is False, "premise"
    narrate_turn(case, metadata)
    assert calls == [1]
    assert _held_notice(metadata) == [
        pending_check_feedback(
            decided["need_id"],
            decided["request_text"],
            pending_check_reason=decided["pending_check_reason"],
            proposed=False,
            declined_stands=True,
        )
    ]
