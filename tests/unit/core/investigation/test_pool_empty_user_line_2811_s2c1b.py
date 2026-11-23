"""The "Root cause" ask says why the cause cannot be confirmed yet (#2811
S2c-1b, stage S2 of #2615; ruling (a) of 2026-11-22).

On the record path, the needs-info request whose record holds a pool-empty
reason carries that reason's line beside the "Root cause" ask (approved copy
item 5), exactly when ``empty_pool_text`` gives a line for the record's
cause. The decide-time message (``ResolutionReadiness.message``) keeps the bare ask
until stage S3: the difference is accepted and pinned here. Pin 6's user
half: every ``PoolEmptyReason`` member has a line.
"""

from __future__ import annotations

import itertools

import pytest

from faultmaven.core.investigation.milestone_engine import turn_narration
from faultmaven.core.investigation.milestone_engine.turn_narration import (
    narrate_turn,
)
from faultmaven.core.investigation.narration.codes import OfferCode
from faultmaven.core.investigation.narration.notices import empty_pool_text
from faultmaven.core.investigation.narration.offers import (
    pool_empty_user_line,
    readiness_message,
    render_offer,
)
from faultmaven.core.investigation.offers import offer_view
from faultmaven.core.investigation.terminal_transitions import (
    assess_resolution_readiness,
)
from faultmaven.modules.case.contracts import (
    CauseOutcome,
    PoolEmptyReason,
    ResolutionReason,
)
from tests.unit.core.investigation import golden_cases_2636 as golden
from tests.unit.core.investigation import offer_cases_2811 as oc
from tests.unit.core.investigation.test_record_prose_2811_s2b2c import (
    _STALE,
    _conflicts,
    _edited,
    _entries,
    _offer,
    _Poisoned,
    _spy,
    _stored,
    _unwrapped,
)

pytestmark = pytest.mark.unit

#: The approved copy, item 5, typed from the ruled text.
APPROVED = {
    PoolEmptyReason.HELD_OPEN: (
        "A cause is on record, but what produced it is still open."
    ),
    PoolEmptyReason.IDENTIFICATION_CONTESTED: (
        "More than one cause still fits the evidence, so none is identified yet."
    ),
    PoolEmptyReason.FALLEN_WITH_OTHER_STANDING: (
        "The cause identified earlier no longer stands, and another candidate "
        "is still open."
    ),
    PoolEmptyReason.FIX_TARGETS_OTHER_CAUSE: (
        "The cause identified earlier no longer stands, and the latest fix "
        "acted on a different cause."
    ),
    PoolEmptyReason.ROOTLESS_VALIDATED_ONLY: (
        "A supported explanation is on record, but it is not tied to a root "
        "cause yet."
    ),
    PoolEmptyReason.NO_CAUSE: (
        "No root cause is on record that a check can confirm yet."
    ),
}

_ASK = "- **Root cause** — what caused the problem?"
_LEGS = ("chain", "rcc", "working_conclusion")
_ROOT = "cn_00000000a001"


def _cause(reason, *, leg=None, ids=True) -> CauseOutcome:
    """A record's cause with ``reason``; ``ids``: every id list a reason
    names holds a root."""
    named = [_ROOT] if ids else []
    return CauseOutcome(
        pool_empty_reason=reason,
        confirmation_root_ids=[] if reason is not None else [_ROOT],
        identification_leg=leg,
        held_open_root_ids=named,
        contested_root_ids=named,
        fallen_root_ids=named,
    )


# ---------------------------------------------------------------------------
# Pin 6, user half: the renderer
# ---------------------------------------------------------------------------


def test_every_reason_has_its_approved_line():
    assert set(APPROVED) == set(PoolEmptyReason)
    for reason in PoolEmptyReason:
        assert pool_empty_user_line(_cause(reason)) == APPROVED[reason], reason


def test_no_reason_has_no_line():
    assert pool_empty_user_line(_cause(None)) is None


@pytest.mark.parametrize("leg", _LEGS)
def test_no_cause_beside_a_leg_has_no_line(leg):
    """Defensive: on the record path "root cause" is missing exactly when no
    leg is set, so this cause never reaches the ask there."""
    cause = _cause(PoolEmptyReason.NO_CAUSE, leg=leg)
    assert pool_empty_user_line(cause) is None
    assert readiness_message(
        ResolutionReason.DOCUMENTATION_GAPS, missing=["root cause"], cause=cause
    ) == readiness_message(ResolutionReason.DOCUMENTATION_GAPS, missing=["root cause"])


@pytest.mark.parametrize(
    "reason, leg, ids",
    list(itertools.product([None, *PoolEmptyReason], (None, *_LEGS), (True, False))),
)
def test_the_user_line_exists_exactly_when_empty_pool_text_gives_one(reason, leg, ids):
    """The user half takes the model half's guard: a reason whose ids are
    empty (a stored flag the fresh ids disagree with) keeps the bare ask."""
    cause = _cause(reason, leg=leg, ids=ids)
    assert (pool_empty_user_line(cause) is None) == (empty_pool_text(cause) is None)


@pytest.mark.parametrize("reason", list(PoolEmptyReason))
def test_the_line_follows_the_ask_and_nothing_else_moves(reason):
    missing = ["root cause", "solution", "confirmation the problem is now resolved"]
    bare = readiness_message(ResolutionReason.DOCUMENTATION_GAPS, missing=missing)
    with_line = readiness_message(
        ResolutionReason.DOCUMENTATION_GAPS, missing=missing, cause=_cause(reason)
    )
    assert with_line == bare.replace(_ASK, f"{_ASK} {APPROVED[reason]}", 1)
    assert with_line.count(APPROVED[reason]) == 1


def test_an_ask_without_the_root_cause_carries_no_line():
    cause = _cause(PoolEmptyReason.HELD_OPEN)
    assert readiness_message(
        ResolutionReason.DOCUMENTATION_GAPS, missing=["solution"], cause=cause
    ) == readiness_message(ResolutionReason.DOCUMENTATION_GAPS, missing=["solution"])


# ---------------------------------------------------------------------------
# The record path: the needs-info request, through the fold
# ---------------------------------------------------------------------------


def _no_cause():
    return golden._models_proposal(
        golden._with_evidence(golden._bare_case()), "resolved"
    )


def _held_open():
    return golden._models_proposal(oc._held_open(), "resolved")


CYCLES = {
    PoolEmptyReason.NO_CAUSE: _no_cause,
    PoolEmptyReason.HELD_OPEN: _held_open,
}


def _premise(case, entry, reason) -> None:
    record = case.progress.outcome
    assert entry["code"] == OfferCode.RESOLUTION_NEEDS_INFO, "premise"
    assert record.cause.pool_empty_reason == reason, "premise"
    assert record.cause.identification_leg is None, "premise"
    assert record.resolution.reason == ResolutionReason.DOCUMENTATION_GAPS, "premise"
    assert "root cause" in record.resolution.missing, "premise"


@pytest.mark.parametrize("reason", sorted(CYCLES))
def test_the_needs_info_request_says_why_the_cause_cannot_be_confirmed(
    monkeypatch, reason
):
    case, metadata = CYCLES[reason]()
    entry = _offer(metadata)
    _stored(case, metadata)
    _premise(case, entry, reason)
    bare = render_offer(entry["code"], entry["params"])
    rendered = _spy(monkeypatch)
    narrate_turn(case, metadata)
    ((code, text),) = rendered
    assert code == entry["code"]
    assert f"{_ASK} {APPROVED[reason]}\n" in text
    assert text == bare.replace(_ASK, f"{_ASK} {APPROVED[reason]}", 1)
    assert metadata["resolution_needs_info_message"] == text
    assert _conflicts(metadata) == []


@pytest.mark.parametrize("reason", sorted(CYCLES))
def test_the_decide_time_ask_keeps_todays_text(monkeypatch, reason):
    """The ruling's accepted difference, on one folded case: the decide-time
    message (``ResolutionReadiness.message``) is today's ask; the record
    path's carries the line. Stage S3 removes the difference."""
    case, metadata = CYCLES[reason]()
    _stored(case, metadata)
    rendered = _spy(monkeypatch)
    narrate_turn(case, metadata)
    ((_code, text),) = rendered
    decided = assess_resolution_readiness(case).message
    assert f"{_ASK}\n" in decided
    assert APPROVED[reason] not in decided
    assert text == decided.replace(_ASK, f"{_ASK} {APPROVED[reason]}", 1)


def test_the_line_reads_only_the_record(monkeypatch):
    """Pin 2: on a poisoned case whose decide-time params are stale (no
    "root cause" missing), the narration states the record's ask and its
    line: it can only have read ``progress.outcome``."""
    case, metadata = _no_cause()
    _stored(case, metadata)
    view = offer_view(case)
    monkeypatch.setattr(turn_narration, "offer_view", lambda _case: view)
    monkeypatch.setattr(turn_narration, "summarize_offer", lambda *_a: True)
    monkeypatch.setattr(turn_narration, "content_by_id", _unwrapped)
    rendered = _spy(monkeypatch)
    for entry in _entries(metadata):
        entry["params"] = {**entry["params"], **_STALE.get(entry["code"], {})}
    offer = _offer(metadata)
    assert _ASK not in render_offer(offer["code"], offer["params"]), "premise"
    narrate_turn(_Poisoned(case), metadata)
    ((_code, text),) = rendered
    assert f"{_ASK} {APPROVED[PoolEmptyReason.NO_CAUSE]}" in text
    assert _conflicts(metadata) == []


def test_a_record_that_disagrees_with_the_decision_shows_no_line(monkeypatch):
    """The record's reason disagrees with the decision (``DECIDED_FROM``): the
    text is the decider's, today's bare ask, and the conflict is counted."""
    case, metadata = _no_cause()
    entry = _offer(metadata)
    _stored(case, metadata)
    _premise(case, entry, PoolEmptyReason.NO_CAUSE)
    _edited(case.progress.outcome, "resolution", reason=ResolutionReason.FIX_FAILED)
    rendered = _spy(monkeypatch)
    narrate_turn(case, metadata)
    assert rendered == [(entry["code"], render_offer(entry["code"], entry["params"]))]
    assert f"{_ASK}\n" in rendered[0][1]
    assert APPROVED[PoolEmptyReason.NO_CAUSE] not in rendered[0][1]
    assert _conflicts(metadata) == [f"narration.{entry['code']}"]
