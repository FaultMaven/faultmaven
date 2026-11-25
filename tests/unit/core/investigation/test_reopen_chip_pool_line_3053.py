"""The reopen chip's refusal states the "Root cause" ask as the record path
does (#3053 S3d-1, stage S3d of #2615), and the whole refusal is the fold's
(S3d-2).

S3d-1: the refusal stated the decide-time message, built with no cause, so its
ask lacked the pool-empty line (approved copy item 5, #2811 S2c-1b) the
needs-info request shows for the same case. S3d-2: the chip decides and
narrates from one fold (``terminal_replies._reply_outcome``), every refusal
included; a fold that raises fails closed with the ruled reply.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from faultmaven.core.investigation.milestone_engine import terminal_replies
from faultmaven.core.investigation.milestone_engine import transition_turns as tt
from faultmaven.core.investigation.milestone_engine.turn_narration import (
    narrate_turn,
)
from faultmaven.core.investigation.narration.codes import OfferCode
from faultmaven.core.investigation.narration.offers import (
    readiness_message,
    render_offer,
)
from faultmaven.core.investigation.outcome import narration_outcome
from faultmaven.core.investigation.terminal_transitions import (
    assess_resolution_readiness,
)
from faultmaven.modules.case.contracts import (
    ConfirmationGap,
    PoolEmptyReason,
    ResolutionReason,
    WritePoint,
)
from tests.unit.core.investigation import golden_cases_2636 as golden
from tests.unit.core.investigation import offer_cases_2811 as oc
from tests.unit.core.investigation.test_pool_empty_user_line_2811_s2c1b import (
    _ASK,
    APPROVED,
)
from tests.unit.core.investigation.test_record_prose_2811_s2b2c import (
    _conflicts,
    _spy,
    _stored,
)

pytestmark = pytest.mark.unit

_LEAD = (
    "This case can't be marked resolved right now: the confirmation that "
    "earned the resolution no longer stands."
)

#: Fresh readiness asks for the root cause; the record's cause has a reason.
POOL_EMPTY = {
    PoolEmptyReason.NO_CAUSE: lambda: golden._with_evidence(golden._bare_case()),
    PoolEmptyReason.HELD_OPEN: oc._held_open,
}

#: Refusals with no "Root cause" ask.
NO_ROOT_CAUSE_ASK = {
    ResolutionReason.FIX_FAILED: golden._failed_fix,
    ResolutionReason.FIX_OFF_CAUSE: golden._interception_only,
    ConfirmationGap.RELIEF_WITHOUT_CAUSE_RECHECK: golden._relief_only,
}


def _chip(case) -> str:
    reply = tt._resolve_on_reopen_chip(case=case, upload_report={}, user_message="")
    return reply["agent_response"]


def _today(case) -> str:
    return f"{_LEAD}\n\n{assess_resolution_readiness(case).message}"


@pytest.fixture
def folds(monkeypatch) -> list:
    calls: list = []
    real = tt._reply_outcome
    monkeypatch.setattr(tt, "_reply_outcome", lambda c: calls.append(c) or real(c))
    return calls


@pytest.mark.parametrize("reason", sorted(POOL_EMPTY))
def test_the_refusal_states_the_record_paths_root_cause_ask(monkeypatch, folds, reason):
    """The needs-info render of the fresh params with the record's cause,
    which is the narrated needs-info request on the same case, byte for
    byte."""
    case = POOL_EMPTY[reason]()
    readiness = assess_resolution_readiness(case)
    assert readiness.reason == ResolutionReason.DOCUMENTATION_GAPS, "premise"
    assert "root cause" in readiness.missing, "premise"
    assert narration_outcome(case).cause.pool_empty_reason == reason, "premise"
    assert APPROVED[reason] not in _today(case), "premise: main's ask is bare"
    proposed, metadata = golden._models_proposal(case.model_copy(deep=True), "resolved")
    _stored(proposed, metadata)
    rendered = _spy(monkeypatch)
    narrate_turn(proposed, metadata)
    ((code, record_path),) = rendered
    assert code == OfferCode.RESOLUTION_NEEDS_INFO and not _conflicts(metadata)

    cause = narration_outcome(case).cause
    params = {**readiness.offer_params(), "cause": cause}
    rendered_ask = render_offer(OfferCode.RESOLUTION_NEEDS_INFO, params)

    text = _chip(case)

    assert text == f"{_LEAD}\n\n{rendered_ask}" == f"{_LEAD}\n\n{record_path}"
    assert text == _today(case).replace(_ASK, f"{_ASK} {APPROVED[reason]}", 1)
    assert len(folds) == 1


def test_a_record_cause_with_no_pool_empty_reason_adds_no_line(monkeypatch, folds):
    """Positive control: the same ask from a cause with no reason is bare."""
    case = POOL_EMPTY[PoolEmptyReason.NO_CAUSE]()
    outcome = narration_outcome(case)
    cause = outcome.cause.model_copy(update={"pool_empty_reason": None})
    record = outcome.model_copy(update={"cause": cause})
    monkeypatch.setattr(terminal_replies, "narration_outcome", lambda _c: record)

    assert _chip(case) == _today(case)
    assert f"{_ASK}\n" in _today(case)
    assert len(folds) == 1


#: A fold whose readiness disagrees with the fresh one (a carried FAILED
#: window, a pending need): the record's resolution, by what differs.
DISAGREEING = {
    "fix_failed": {"reason": ResolutionReason.FIX_FAILED},
    "fix_off_cause": {"reason": ResolutionReason.FIX_OFF_CAUSE, "off_cause_turn": 3},
    "no_root_cause": {"missing": ["solution"]},
}


@pytest.mark.parametrize("name", list(DISAGREEING))
def test_a_disagreeing_fold_gives_the_whole_refusal(monkeypatch, folds, name):
    """The record's reason, missing items, gaps and cause, not the fresh
    readiness's: the refusal is the fold's ask, from one fold."""
    case = POOL_EMPTY[PoolEmptyReason.NO_CAUSE]()
    outcome = narration_outcome(case)
    resolution = outcome.resolution.model_copy(update=DISAGREEING[name])
    record = outcome.model_copy(update={"resolution": resolution})
    monkeypatch.setattr(terminal_replies, "narration_outcome", lambda _c: record)
    ask = readiness_message(
        resolution.reason,
        missing=resolution.missing,
        gaps=resolution.confirmation_gaps,
        off_cause_turn=resolution.off_cause_turn,
        cause=record.cause,
    )

    assert _chip(case) == f"{_LEAD}\n\n{ask}" != _today(case)
    assert len(folds) == 1


def test_a_pending_check_the_fold_holds_no_text_for_fails_closed(monkeypatch, folds):
    """The record's PENDING_CHECK with no need on the case to read the
    request text from: the ruled reply, not a raise in the composer."""
    case = POOL_EMPTY[PoolEmptyReason.NO_CAUSE]()
    outcome = narration_outcome(case)
    resolution = outcome.resolution.model_copy(
        update={"reason": ResolutionReason.PENDING_CHECK}
    )
    record = outcome.model_copy(update={"resolution": resolution})
    monkeypatch.setattr(terminal_replies, "narration_outcome", lambda _c: record)

    assert _chip(case) == tt.RECORD_UNREADABLE_LINE
    assert len(folds) == 1


@pytest.mark.parametrize("name", list(NO_ROOT_CAUSE_ASK), ids=str)
def test_a_refusal_with_no_root_cause_ask_is_todays_from_one_fold(folds, name):
    """FIX_FAILED, FIX_OFF_CAUSE and a documentation-gaps ask carrying the
    cause re-check's names: byte-equal to today's refusal, from one fold."""
    case = NO_ROOT_CAUSE_ASK[name]()
    readiness = assess_resolution_readiness(case)
    assert "root cause" not in readiness.missing, "premise"
    assert name in (readiness.reason, *readiness.confirmation_gaps), "premise"

    assert _chip(case) == _today(case)
    assert len(folds) == 1


def test_the_pending_check_reply_is_unchanged_from_one_fold(folds):
    case = golden._held()
    readiness = assess_resolution_readiness(case)
    assert readiness.pending_check is not None, "premise"

    assert _chip(case) == readiness.message
    assert len(folds) == 1


def test_a_fold_that_raises_fails_closed_counted_once(monkeypatch):
    counted: list = []
    metric = SimpleNamespace(
        labels=lambda at: SimpleNamespace(inc=lambda: counted.append(at))
    )
    monkeypatch.setattr(terminal_replies, "case_outcome_write_failures_total", metric)
    monkeypatch.setattr(terminal_replies, "narration_outcome", _raise)
    case = POOL_EMPTY[PoolEmptyReason.NO_CAUSE]()

    assert _chip(case) == tt.RECORD_UNREADABLE_LINE
    assert counted == [WritePoint.NARRATION]


def _raise(_case):
    raise RuntimeError("probe")
