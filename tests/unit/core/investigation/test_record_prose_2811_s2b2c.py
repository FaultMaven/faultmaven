"""The held check's notice and the offer prose state the cycle's record
(#2811 S2b-2c, #2615 stage S2, ruling (a) of 2026-11-22; #3053 S3d-3, ruling
Q5 of 2026-11-25).

Switched: ``PENDING_CHECK`` and every offer code (since S3d-3 also
``REQUEST_READY``, ``DEFERRED_RESOLVE`` and the model proposal's confirmation
and closure). Each takes the facts its text states from
``case.progress.outcome``, the need's request text through the narrators'
text accessor (``content_by_id``), per decision: a decision that disagrees
with the record on a field it was decided from (``DECIDED_FROM``) is counted
and still renders the record, but in the four cases ruled on 2026-11-26
(``offers.RULED_CASES``), which render the decision's params.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from faultmaven.core.investigation.milestone_engine import turn_narration
from faultmaven.core.investigation.milestone_engine.stage_gates import (
    record_decision,
)
from faultmaven.core.investigation.milestone_engine.terminal_replies import (
    resolution_content,
)
from faultmaven.core.investigation.milestone_engine.turn_narration import (
    NARRATION_CONFLICTS_KEY,
    OUTCOME_WRITTEN_KEY,
    narrate_turn,
)
from faultmaven.core.investigation.narration.codes import (
    DECISIONS_KEY,
    NoticeCode,
    OfferCode,
)
from faultmaven.core.investigation.narration.fix_verification import content_by_id
from faultmaven.core.investigation.narration.notices import (
    pending_check_feedback,
    render_notice,
)
from faultmaven.core.investigation.narration.offer_view import OfferView
from faultmaven.core.investigation.narration.offers import (
    narrated_params,
    readiness_message,
    record_params,
    render_offer,
)
from faultmaven.core.investigation.offers import offer_view
from faultmaven.core.investigation.outcome import (
    narration_outcome,
    record_case_outcome,
)
from faultmaven.modules.case.contracts import (
    AnswerKind,
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
    # #3053 S3d-3: the four offers kept on their deciders' params until then.
    "request_ready": lambda: golden._requested(golden._confirmed_case()),
    "deferred_resolve": lambda: golden._deferred(confirmed=True),
    "model_proposal_confirmation": lambda: golden._models_proposal(
        golden._confirmed_case(), "resolved"
    ),
    "model_proposal_closure": lambda: golden._models_proposal(
        golden._identified_case()[0], "closed", "close it"
    ),
}

#: The four offers S3d-3 switched (``KEPT`` before it).
S3D3 = (
    "request_ready",
    "deferred_resolve",
    "model_proposal_confirmation",
    "model_proposal_closure",
)


def _entries(metadata: dict) -> list:
    return metadata[DECISIONS_KEY]


def _of(entry: dict) -> dict:
    return {"code": entry["code"], "params": entry["params"]}


def _offer(metadata: dict) -> dict:
    """The cycle's one offer decision."""
    (entry,) = [e for e in _entries(metadata) if e["code"] in set(OfferCode)]
    return entry


def _decided(case, code, params: dict) -> dict:
    """The decision's own params as the narration renders them: the
    confirmation's with the content read at render (``narrated_params``)."""
    return narrated_params(
        code,
        params,
        None,
        None,
        resolution_content(case),
        lambda item_id: content_by_id(case, item_id),
    )


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


def _conflicts(metadata: dict) -> list[str]:
    return metadata.get(NARRATION_CONFLICTS_KEY, [])


def _held_notice(metadata: dict) -> list[str]:
    return [
        n
        for n in metadata.get("system_notices") or []
        if n.startswith(("RESOLUTION HELD", "RESOLVED NOT PROPOSED"))
    ]


def _feedback(params: dict) -> str:
    """The held check's notice from the decider's params."""
    return pending_check_feedback(
        params["need_id"],
        params["request_text"],
        pending_check_reason=params["pending_check_reason"],
        proposed=params["proposed"],
        declined_stands=params["declined_stands"],
    )


def _second_need(case, request_text: str = "the B check: billing p99 under 200ms"):
    """A need beside the held one, its own id and request text."""
    need = case.evidence_needs[-1].model_copy(
        update={"need_id": "eneed_00000000b0b0", "request_text": request_text}
    )
    case.evidence_needs.append(need)
    return need


def _edited(record, section: str, **fields):
    setattr(record, section, getattr(record, section).model_copy(update=fields))


# ---------------------------------------------------------------------------
# Pin 2: the switched prose renders from the record on a poisoned case
# ---------------------------------------------------------------------------


class _Poisoned:
    """A case on which every attribute read raises but the record and the
    identity fields. ``content_by_id`` reads the case itself (``_unwrapped``)."""

    _ALLOWED = {"case_id", "state", "current_stage", "current_turn"}

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


def _unwrapped(case, item_id, **kwargs):
    """The narrators' text accessor, the one way pin 2 lets a narrator read a
    case's text (exempt permanently)."""
    if isinstance(case, _Poisoned):
        case = object.__getattribute__(case, "_case")
    return content_by_id(case, item_id, **kwargs)


#: A decider's params stale on every fact the code's text states from the
#: record that the decision was not decided on: the poisoned narration can
#: state the record's only by reading it. The decided fields agree, so the
#: record is the one the narration renders from.
_STALE_READINESS = {
    "missing": [],
    "gaps": [],
    "request_text": "a stale ask",
    "off_cause_turn": 99,
}
_STALE_LINES = {"record_lines": "- a stale record line"}
_STALE = {
    OfferCode.RCA_INFEASIBLE_CLOSE: {"closure_line": "a stale closure line"},
    OfferCode.BACKSTOP_RESOLVE: _STALE_LINES,
    OfferCode.DEFERRED_RESOLVE: _STALE_LINES,
    OfferCode.REQUEST_READY: _STALE_LINES,
    OfferCode.NEEDS_INFO_UNMET_CLOSE: _STALE_READINESS,
    OfferCode.RESOLUTION_NEEDS_INFO: _STALE_READINESS,
    OfferCode.MODEL_PROPOSAL: {
        **_STALE_READINESS,
        **_STALE_LINES,
        "root_cause": "a stale root",
        "milestones_completed": 99,
    },
    NoticeCode.PENDING_CHECK: {
        "need_id": "eneed_stale",
        "request_text": "a stale ask",
        "declined_stands": True,
    },
}

#: Controls: their text states no record fact the decision was not decided
#: on (the false-alarm close names the decider's rationale, the deferred close
#: is fixed text, and the two SUGGEST_CLOSE readiness reasons, NOTHING_ON_RECORD
#: here, state only the reason), so they pass on ``main`` by design; their
#: record source is pinned by ``test_the_record_params_of_each_switched_code``.
_CONTROLS = (
    "false_alarm_close",
    "deferred_close",
    "needs_info_unmet_close",
    "model_proposal_readiness",
)


@pytest.mark.parametrize(
    "name",
    [
        pytest.param(n, id=f"{n}-control" if n in _CONTROLS else n)
        for n in sorted(SWITCHED)
    ],
)
def test_the_switched_prose_renders_from_the_record_alone(monkeypatch, name):
    """Pin 2: with the decider's undecided facts stale, ``narrate_turn`` on a
    case that lets it read only the record, the identity and, through
    ``content_by_id``, a need's text, renders the text the agreeing decider
    would have: the record's. The offer record is read through its own
    exempt accessor (``OfferView``, S2b-2d) and written through
    ``summarize_offer``, both stubbed here; the case content the
    confirmation and the closure name through ``ResolutionContent``, exempt
    permanently (#3053 S3d-3)."""
    case, metadata = SWITCHED[name]()
    _stored(case, metadata)
    view, content = offer_view(case), resolution_content(case)
    monkeypatch.setattr(turn_narration, "offer_view", lambda _case: view)
    monkeypatch.setattr(turn_narration, "summarize_offer", lambda *_a: True)
    monkeypatch.setattr(turn_narration, "content_by_id", _unwrapped)
    monkeypatch.setattr(turn_narration, "resolution_content", lambda _c: content)
    rendered = _spy(monkeypatch)
    offer = _offer(metadata)
    want_prose = [
        (offer["code"], render_offer(offer["code"], _decided(case, **_of(offer))))
    ]
    held = [e for e in _entries(metadata) if e["code"] == _CODE]
    want_notices = [_feedback(e["params"]) for e in held]
    for entry in _entries(metadata):
        entry["params"] = {**entry["params"], **_STALE.get(entry["code"], {})}

    narrate_turn(_Poisoned(case), metadata)

    assert rendered == want_prose
    assert _held_notice(metadata) == want_notices
    assert bool(held) == (name == "pending_check")
    assert _conflicts(metadata) == []


# ---------------------------------------------------------------------------
# A fact the decision was not decided on: the record's
# ---------------------------------------------------------------------------


_RECORD_WINS = {
    "rca_infeasible_close": (
        lambda r: _edited(r, "closure", statement="the record's closure line"),
        lambda p: "The record's closure line. Since "
        f"{p['rationale']}, shall we close this case?",
    ),
    "backstop_resolve": (
        lambda r: _edited(r, "resolution", statement="- the record's line"),
        lambda p: "The record shows:\n\n- the record's line\n\nThe case is still "
        "open until you confirm: marking it resolved records the attribution and "
        "writes up the resolution summary. Shall I mark this case resolved?",
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
}


@pytest.mark.parametrize("name", sorted(_RECORD_WINS))
def test_an_undecided_fact_the_record_states_is_the_records(monkeypatch, name):
    """The stored record agrees with the decision on what it was decided
    from and differs on a fact the text states besides; the text states the
    record's, and nothing is counted."""
    edit, want = _RECORD_WINS[name]
    case, metadata = SWITCHED[name]()
    entry = _offer(metadata)
    _stored(case, metadata)
    edit(case.progress.outcome)
    rendered = _spy(monkeypatch)
    narrate_turn(case, metadata)
    assert rendered == [(entry["code"], want(entry["params"]))]
    assert rendered[0][1] != render_offer(entry["code"], entry["params"]), "premise"
    assert _conflicts(metadata) == []


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


# ---------------------------------------------------------------------------
# A field the decision was decided from: on a disagreement, the record's,
# counted (Q5); in a ruled case, the decision's, counted (2026-11-26)
# ---------------------------------------------------------------------------


_RECORD_ON_DISAGREEMENT = {
    "backstop_resolve": lambda r: (
        _edited(r, "cause", identification_leg=None),
        _edited(r, "resolution", statement="- the record's line"),
    ),
    "needs_info_unmet_close": lambda r: _edited(
        r, "resolution", reason=ResolutionReason.FIX_OFF_CAUSE, off_cause_turn=4
    ),
    "model_proposal_readiness": lambda r: _edited(
        r, "resolution", reason=ResolutionReason.FALSE_ALARM
    ),
    "request_ready": lambda r: (
        _edited(r, "cause", identification_leg=None),
        _edited(r, "resolution", statement="- the record's line"),
    ),
    "model_proposal_confirmation": lambda r: (
        _edited(r, "cause", identification_leg=None),
        _edited(r, "resolution", statement="- the record's line"),
    ),
    "model_proposal_closure": lambda r: _edited(r, "cause", identification_leg=None),
    "deferred_resolve": lambda r: (
        _edited(r, "cause", identification_leg=None),
        _edited(r, "resolution", statement="- the record's line"),
    ),
}


@pytest.mark.parametrize("name", sorted(_RECORD_ON_DISAGREEMENT))
def test_a_decided_field_the_record_does_not_state_renders_the_record(
    monkeypatch, name
):
    """The record disagrees with the decision on a field it was decided
    from, in no ruled case: the text is the record's (Q5, #3053 S3d-3), and
    the conflict is counted. On ``main`` it was the decider's."""
    case, metadata = SWITCHED[name]()
    entry = _offer(metadata)
    _stored(case, metadata)
    _RECORD_ON_DISAGREEMENT[name](case.progress.outcome)
    rendered = _spy(monkeypatch)
    narrate_turn(case, metadata)
    want = record_params(
        entry["code"],
        entry["params"],
        case.progress.outcome,
        None,
        resolution_content(case),
        lambda item_id: content_by_id(case, item_id),
    )
    assert rendered == [(entry["code"], render_offer(entry["code"], want))]
    decided = render_offer(entry["code"], _decided(case, **_of(entry)))
    assert rendered[0][1] != decided, "premise: the decision reads otherwise"
    assert _conflicts(metadata) == [f"narration.{entry['code']}"]


def _flip(record) -> None:
    _edited(
        record,
        "closure",
        eligibility=(
            ClosureEligibility.TRIVIAL
            if record.closure.eligibility != ClosureEligibility.TRIVIAL
            else ClosureEligibility.HAS_SUBSTANCE
        ),
    )


#: Each ruled case's scenario and the record edit that reaches it.
_RULED = {
    # Case 3, by each offer's own field.
    "request_ready": lambda r: _edited(
        r, "resolution", reason=ResolutionReason.DOCUMENTATION_GAPS, statement=""
    ),
    "model_proposal_confirmation": lambda r: _edited(
        r, "resolution", reason=ResolutionReason.FIX_FAILED, statement=""
    ),
    "backstop_resolve": lambda r: _edited(
        r, "resolution", reason=ResolutionReason.PENDING_CHECK
    ),
    "deferred_resolve": _flip,
    # Case 4: an offer decided on the closure eligibility.
    "model_proposal_closure": _flip,
    "rca_infeasible_close": lambda r: (
        _edited(r, "closure", eligibility=ClosureEligibility.TRIVIAL),
        _edited(r, "closure", statement="the record's closure line"),
    ),
    "deferred_close": _flip,
}


@pytest.mark.parametrize("name", sorted(_RULED))
def test_a_ruled_case_renders_the_decision_and_is_counted(monkeypatch, name):
    """Ruled 2026-11-26: a resolve offer over a record whose reason is not
    CONFIRMED (the deferred resolve: whose eligibility differs), and a
    closure offer whose eligibility flipped, keep their decider's text; the
    conflict is counted."""
    case, metadata = SWITCHED[name]()
    entry = _offer(metadata)
    _stored(case, metadata)
    _RULED[name](case.progress.outcome)
    rendered = _spy(monkeypatch)
    narrate_turn(case, metadata)
    want = render_offer(entry["code"], _decided(case, **_of(entry)))
    assert rendered == [(entry["code"], want)]
    assert "the record's" not in rendered[0][1]
    assert _conflicts(metadata) == [f"narration.{entry['code']}"]


def test_a_held_ask_with_no_request_text_renders_the_decision(monkeypatch):
    """Ruled case 2: the record holds a pending check whose request text
    neither it nor the decision has; the ask is the decision's, counted."""
    case, metadata = SWITCHED["resolution_needs_info"]()
    entry = _offer(metadata)
    assert entry["params"]["request_text"] is None, "premise"
    _stored(case, metadata)
    _edited(
        case.progress.outcome,
        "resolution",
        reason=ResolutionReason.PENDING_CHECK,
        pending_check_need_id="eneed_0000000000ff",
        pending_check_reason=PendingCheckReason.UNANSWERED,
    )
    rendered = _spy(monkeypatch)
    narrate_turn(case, metadata)
    assert rendered == [(entry["code"], render_offer(entry["code"], entry["params"]))]
    assert _conflicts(metadata) == [f"narration.{entry['code']}"]


def test_a_held_ask_the_record_reads_confirmed_renders_the_decision(monkeypatch):
    """Review item 1, ruled case 1 (2026-11-26): step 2 refused a held
    ``resolved`` and the record reads CONFIRMED. The needs-info request is the
    decision's ask, never the empty CONFIRMED message, and the held check's
    notice is the decision's, the record holding no check; the one decision
    logged under two codes counts one conflict."""
    case, metadata = SWITCHED["pending_check"]()
    entry = _offer(metadata)
    (decided,) = [e["params"] for e in _entries(metadata) if e["code"] == _CODE]
    assert entry["params"]["reason"] == ResolutionReason.PENDING_CHECK, "premise"
    _stored(case, metadata)
    _edited(
        case.progress.outcome,
        "resolution",
        reason=ResolutionReason.CONFIRMED,
        pending_check_need_id=None,
        pending_check_reason=None,
    )
    rendered = _spy(monkeypatch)
    narrate_turn(case, metadata)
    assert rendered == [(entry["code"], render_offer(entry["code"], entry["params"]))]
    assert rendered[0][1]
    assert _held_notice(metadata) == [_feedback(decided)]
    assert _conflicts(metadata) == [f"narration.{_CODE}"]


def test_one_decision_under_two_codes_names_one_need(monkeypatch):
    """Delta F1: step 2's one decision is logged as the held check (decided
    on the reason and why the check holds) and its needs-info request
    (decided on the reason). A record agreeing on the reason but holding need
    B, held for another reason, disagrees with the held check: both the
    model's notice and the user's request name the record's need, never one
    each (#3053 S3d-3: per decision, the record; on ``main`` both named the
    decider's), and the conflict is counted."""
    case, metadata = SWITCHED["pending_check"]()
    entry = _offer(metadata)
    (decided,) = [e["params"] for e in _entries(metadata) if e["code"] == _CODE]
    assert decided["pending_check_reason"] == PendingCheckReason.UNANSWERED
    need = _second_need(case)
    _stored(case, metadata)
    _edited(
        case.progress.outcome,
        "resolution",
        pending_check_need_id=need.need_id,
        pending_check_reason=PendingCheckReason.FULFILLED_BEFORE_ASK,
    )
    assert case.progress.outcome.resolution.reason == decided["reason"], "premise"
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
    assert decided["request_text"] not in rendered[0][1]
    assert _held_notice(metadata) == [
        pending_check_feedback(
            need.need_id,
            need.request_text,
            pending_check_reason=PendingCheckReason.FULFILLED_BEFORE_ASK,
            proposed=True,
            declined_stands=decided["declined_stands"],
        )
    ]
    assert _conflicts(metadata) == [f"narration.{_CODE}"]


def test_a_non_switched_code_reads_the_record_beside_a_disagreement():
    """A switched decision that disagrees with the stored record in a ruled
    case renders its decider's text (#3053 S3d-3: that decision alone; on
    ``main`` every switched one did): a B1b code logged in the same cycle
    (``SOLUTION_OFFER_WITHDRAWN``) still renders the record's fact, as on
    ``main``."""
    case, metadata = SWITCHED["pending_check"]()
    record_decision(
        metadata, NoticeCode.SOLUTION_OFFER_WITHDRAWN, prepend=True, held_open=False
    )
    _stored(case, metadata)
    _edited(
        case.progress.outcome,
        "resolution",
        reason=ResolutionReason.CONFIRMED,
        pending_check_need_id=None,
        pending_check_reason=None,
    )
    _edited(case.progress.outcome, "cause", identification_held_open=True)
    narrate_turn(case, metadata)
    assert _conflicts(metadata) == [f"narration.{_CODE}"], "premise"
    withdrawn = [
        n
        for n in metadata["system_notices"]
        if n.startswith("SYSTEM: Your pending SOLUTION proposal was withdrawn")
    ]
    assert len(withdrawn) == 1
    assert "its why is open" in withdrawn[0]


def test_a_lapsed_offer_is_not_compared(monkeypatch):
    """An offer that lapsed in its cycle shows nothing and is not compared:
    one that disagrees with the record, logged beside the held check and its
    needs-info request that agree, leaves both rendering from the record."""
    case, metadata = SWITCHED["pending_check"]()
    entry = _offer(metadata)
    lapsed_id = "0123456789abcdef"
    metadata[DECISIONS_KEY].append(
        {
            "code": OfferCode.BACKSTOP_RESOLVE,
            "params": {
                "proposal_id": lapsed_id,
                "record_lines": "- the lapsed offer's line",
                "reason": ResolutionReason.CONFIRMED,
                "identification_leg": None,
            },
            "token": None,
        }
    )
    need = _second_need(case)
    _stored(case, metadata)
    _edited(case.progress.outcome, "resolution", pending_check_need_id=need.need_id)
    view = OfferView(
        None, ((lapsed_id, AnswerKind.LAPSED),), lambda: None, lambda: None
    )
    monkeypatch.setattr(turn_narration, "offer_view", lambda _case: view)
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
    (notice,) = _held_notice(metadata)
    assert f"[{need.need_id}] {need.request_text}" in notice
    assert _conflicts(metadata) == []


def test_the_record_params_of_each_switched_code():
    """What ``record_params`` takes from an agreeing record: the false-alarm
    close the problem's status (its text states none), the deferred close
    nothing, and a readiness ask the decision's request text where the
    record's need has none."""
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
    params = {"reason": record.resolution.reason, "request_text": "the decision's"}
    readiness = record_params(OfferCode.RESOLUTION_NEEDS_INFO, params, record, None)
    assert readiness["request_text"] == "the decision's"


# ---------------------------------------------------------------------------
# The four offers S3d-3 switched, over a record that disagrees on every fact
# they decide or state: each is a ruled case, so the decision's
# ---------------------------------------------------------------------------


def _disagree(record) -> None:
    """A record that disagrees with each of the four on each fact it
    decides or states."""
    _edited(record, "resolution", statement="- the record's line")
    _edited(record, "resolution", reason=ResolutionReason.DOCUMENTATION_GAPS)
    _edited(
        record,
        "cause",
        identification_leg=None if record.cause.identification_leg else "chain",
    )
    _flip(record)


@pytest.mark.parametrize("name", S3D3)
def test_the_four_render_the_decision_over_a_record_that_disagrees_throughout(
    monkeypatch, name
):
    """``main`` (as ``KEPT``) rendered their deciders' params whatever the
    record said. Here each meets a ruled case (case 3 by its reason or its
    eligibility, case 4 by its eligibility) and renders the decision, now
    counted."""
    case, metadata = SWITCHED[name]()
    entry = _offer(metadata)
    _stored(case, metadata)
    _disagree(case.progress.outcome)
    rendered = _spy(monkeypatch)
    narrate_turn(case, metadata)
    want = render_offer(entry["code"], _decided(case, **_of(entry)))
    assert rendered == [(entry["code"], want)]
    assert "the record's line" not in rendered[0][1]
    # One label per field that disagrees.
    assert set(_conflicts(metadata)) == {f"narration.{entry['code']}"}


# ---------------------------------------------------------------------------
# The held check's notice
# ---------------------------------------------------------------------------


@pytest.fixture
def held():
    """The backstop's hold: a PENDING_CHECK alone, its record stored."""
    case, metadata = golden._backstop_cycle(golden._held())
    assert [e["code"] for e in _entries(metadata)] == [_CODE], "premise"
    return case, metadata


def test_a_record_holding_no_check_renders_the_decisions_notice(held):
    """Review item 2: the model is still told why its resolution is held."""
    case, metadata = held
    (decided,) = [e["params"] for e in _entries(metadata)]
    _stored(case, metadata)
    _edited(case.progress.outcome, "resolution", pending_check_need_id=None)
    narrate_turn(case, metadata)
    assert _held_notice(metadata) == [_feedback(decided)]


def test_the_notice_names_the_need_the_record_holds(held):
    """The decider named need A; the record, agreeing on the reason and why
    the check holds, holds need B: B is named, with its request text and
    whether a decline stands, both the record's."""
    case, metadata = held
    (decided,) = [e["params"] for e in _entries(metadata)]
    need = _second_need(case)
    _stored(case, metadata)
    _edited(
        case.progress.outcome,
        "resolution",
        pending_check_need_id=need.need_id,
        declined_stands=True,
    )
    narrate_turn(case, metadata)
    assert decided["need_id"] != need.need_id, "premise"
    assert _held_notice(metadata) == [
        pending_check_feedback(
            need.need_id,
            need.request_text,
            pending_check_reason=decided["pending_check_reason"],
            proposed=False,
            declined_stands=True,
        )
    ]
    assert _conflicts(metadata) == []


def test_a_held_reason_the_record_does_not_state_renders_the_records_notice(
    held,
):
    """#3053 S3d-3 (Q5): per decision, the record's need and reason, the
    conflict counted (on ``main``, the decision's notice)."""
    case, metadata = held
    (decided,) = [e["params"] for e in _entries(metadata)]
    need = _second_need(case)
    _stored(case, metadata)
    _edited(
        case.progress.outcome,
        "resolution",
        pending_check_need_id=need.need_id,
        pending_check_reason=PendingCheckReason.FULFILLED_BEFORE_ASK,
    )
    narrate_turn(case, metadata)
    assert _held_notice(metadata) == [
        pending_check_feedback(
            need.need_id,
            need.request_text,
            pending_check_reason=PendingCheckReason.FULFILLED_BEFORE_ASK,
            proposed=False,
            declined_stands=decided["declined_stands"],
        )
    ]
    assert _conflicts(metadata) == [f"narration.{_CODE}"]


def test_a_need_with_no_text_is_named_with_the_decisions(held):
    """Review item 6: the record's need has no text the accessor finds; the
    notice names the decision's request text, never "None"."""
    case, metadata = held
    (decided,) = [e["params"] for e in _entries(metadata)]
    outcome = narration_outcome(case)
    params = {**decided, "record_request_text": None}
    (text,) = render_notice(_CODE, params, outcome, [])
    assert text == _feedback(decided)


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
    assert _held_notice(metadata) == [_feedback(decided)]


# ---------------------------------------------------------------------------
# The failed write: the in-memory record, for a decision that reads it
# ---------------------------------------------------------------------------


def _in_memory(monkeypatch, edit=lambda _r: None) -> list:
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
    """A cycle that logs only the needs-info request, its record write
    failed: the narration computes the record in memory and states it."""
    case, metadata = SWITCHED["resolution_needs_info"]()
    entry = _offer(metadata)
    assert [e["code"] for e in _entries(metadata)] == [entry["code"]], "premise"
    metadata[OUTCOME_WRITTEN_KEY] = False
    edit, want = _RECORD_WINS["resolution_needs_info"]
    calls = _in_memory(monkeypatch, edit)
    rendered = _spy(monkeypatch)
    narrate_turn(case, metadata)
    assert calls == [1]
    assert rendered == [(entry["code"], want(entry["params"]))]


@pytest.mark.parametrize(
    "name",
    [
        "model_proposal_readiness",
        "model_proposal_closure",
        "model_proposal_confirmation",
    ],
)
def test_a_failed_write_folds_once_for_a_model_proposal(monkeypatch, name):
    """Review item 3: a model proposal reads the record by its composer; since
    #3053 S3d-3 every composer does, so a failed write folds one in-memory
    record for each (on ``main``, for the readiness ask alone)."""
    computed = [1]
    case, metadata = SWITCHED[name]()
    assert [e["code"] for e in _entries(metadata)] == [OfferCode.MODEL_PROPOSAL]
    metadata[OUTCOME_WRITTEN_KEY] = False
    calls = _in_memory(monkeypatch)
    narrate_turn(case, metadata)
    assert calls == computed


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
    assert _held_notice(metadata) == [_feedback({**decided, "declined_stands": True})]
