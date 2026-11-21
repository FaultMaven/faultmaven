"""The resolve confirmation and the closed reply rendered from the record
(#2811 S2b-2b, #2615 stage S2).

Whether a cause is identified and the resolution's record lines come from the
record folded where the confirmation is decided; the closed reply's reason
and mitigation line from the record the terminal transition wrote, folded
again only when that write failed. The texts the confirmation names besides
are ``ResolutionContent`` (ruling of 2026-11-21). On every fixture of the
corpus (``reply_cases_2811``) the replies are byte for byte what ``main``
returned (``reply_oracle_2811.json``); the one declared difference is the
closed reply's double failure, the reason's line alone.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from faultmaven.core.investigation import outcome as outcome_module
from faultmaven.core.investigation.milestone_engine import (
    terminal_replies as replies_module,
)
from faultmaven.core.investigation.milestone_engine.terminal_replies import (
    _compose_terminal_reply,
    _terminal_confirmation_response,
    resolution_confirmation_params,
    resolution_content,
)
from faultmaven.core.investigation.narration.offers import confirmation_message
from faultmaven.core.investigation.narration.replies import (
    _CLOSED_REPLIES,
    _MITIGATION_LINE_FOLLOWS,
    NO_ROOT_ON_RECORD,
    ResolutionContent,
    terminal_status_line,
)
from faultmaven.core.investigation.outcome import narration_outcome
from faultmaven.core.investigation.terminal_transitions import (
    mitigation_closure_line,
)
from faultmaven.modules.case.contracts import CaseState, ClosureReason, WritePoint
from tests.unit.core.investigation.reply_cases_2811 import (
    CASES,
    CONFIRMATIONS,
    SUMMARY,
    TERMINALS,
    build,
    render,
)

pytestmark = pytest.mark.unit

ORACLE = json.loads((Path(__file__).parent / "reply_oracle_2811.json").read_text())


def test_the_oracle_covers_the_corpus():
    assert set(ORACLE) == set(CASES)


@pytest.mark.parametrize("name", sorted(CASES))
async def test_the_replies_render_as_main_did(name):
    assert render(await build(name)) == ORACLE[name]


def test_each_leg_and_solution_text_is_shown_by_a_fixture():
    """The corpus shows what the replies can say: each leg, both root
    fallbacks, each enrichment hint, and a mitigation line in a reply."""
    legs = {ORACLE[n]["params"]["identification_leg"] for n in CONFIRMATIONS}
    assert legs == {None, "chain", "rcc", "working_conclusion"}
    roots = {ORACLE[n]["params"]["root"] for n in CONFIRMATIONS}
    assert {NO_ROOT_ON_RECORD, "No root cause established"} <= roots
    hints = {
        (ORACLE[n]["params"]["no_evidence"], ORACLE[n]["params"]["no_commands"])
        for n in CASES
    }
    assert hints == {(True, True), (True, False), (False, True), (False, False)}
    assert "Is this correct?" in ORACLE["C:evidence_commands"]["confirmation"]
    assert any(ORACLE[n]["params"]["record_lines"] for n in CASES)
    assert ORACLE["T:mitigation_sufficient:line:written"]["terminal"] != (
        _CLOSED_REPLIES[ClosureReason.MITIGATION_SUFFICIENT]
    )


@pytest.mark.parametrize("name", sorted(n for n in TERMINALS if n != "T:resolved"))
async def test_a_written_close_stores_a_terminal_record_and_a_copied_one_does_not(
    name,
):
    """The premise of the two paths: a close through the transition stores
    the closure; a copy carries an open record, so the reply folds."""
    stored = (await build(name)).progress.outcome
    terminal = stored is not None and stored.folded_under.is_terminal
    assert terminal == name.endswith(":written")


# ---------------------------------------------------------------------------
# Pin 2: the adapters read the record, the identity and ResolutionContent
# ---------------------------------------------------------------------------


class _Poisoned:
    """A case on which every attribute read raises but ``state``,
    ``case_id`` and ``progress.outcome``."""

    _ALLOWED = {"state", "case_id"}

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
        raise AssertionError(f"the reply read case.{name}")


@pytest.mark.parametrize("name", sorted(CASES))
async def test_the_adapters_read_no_case_field(monkeypatch, name):
    case = await build(name)
    outcome, content = narration_outcome(case), resolution_content(case)
    monkeypatch.setattr(replies_module, "narration_outcome", lambda _c: outcome)
    monkeypatch.setattr(replies_module, "resolution_content", lambda _c: content)
    poisoned = _Poisoned(case)
    want = ORACLE[name]
    assert resolution_confirmation_params(poisoned) == want["params"]
    if case.is_terminal:
        assert _terminal_confirmation_response(poisoned) == want["terminal"]
        assert _compose_terminal_reply(poisoned, SUMMARY) == want["composed"]


def test_resolution_content_holds_the_rulings_reading():
    """Ruling of 2026-11-21 (S2b-2b (a)): the root-cause text, the latest
    solution's text and two presence flags; no outcome fact."""
    assert {f.name: f.type for f in dataclasses.fields(ResolutionContent)} == {
        "root_cause": str,
        "solution": str,
        "has_evidence": bool,
        "has_commands": bool,
    }
    with pytest.raises(dataclasses.FrozenInstanceError):
        ResolutionContent("r", "s", True, True).root_cause = "x"


# ---------------------------------------------------------------------------
# The record wins over a derivation from the case
# ---------------------------------------------------------------------------

_CONTENT = ResolutionContent("the root", "the fix", True, False)


def _record(case, *, leg, statement="- the crafted record line"):
    outcome = narration_outcome(case)
    return outcome.model_copy(
        update={
            "cause": outcome.cause.model_copy(update={"identification_leg": leg}),
            "resolution": outcome.resolution.model_copy(
                update={"statement": statement}
            ),
        }
    )


@pytest.mark.parametrize(
    "name, leg, root",
    [
        ("C:solution_title", None, NO_ROOT_ON_RECORD),
        ("C:leg_none", "rcc", "the root"),
    ],
)
async def test_the_confirmation_states_the_records_leg_and_lines(
    monkeypatch, name, leg, root
):
    """The record's leg says whether a root is named, whatever the case's
    derivation says; its record lines are printed."""
    case = await build(name)
    assert ORACLE[name]["params"]["identification_leg"] != leg, "premise"
    record = _record(case, leg=leg)
    monkeypatch.setattr(
        replies_module, "narration_outcome", lambda _c: record, raising=False
    )
    monkeypatch.setattr(
        replies_module, "resolution_content", lambda _c: _CONTENT, raising=False
    )
    params = resolution_confirmation_params(case)
    assert params == {
        "root": root,
        "solution": "the fix",
        "record_lines": "- the crafted record line",
        "no_evidence": False,
        "no_commands": True,
        "identification_leg": leg,
    }
    assert "- the crafted record line" in confirmation_message(params)


async def test_the_closed_reply_states_the_closure_the_transition_wrote():
    """The stored terminal record's mitigation line is printed, not
    ``mitigation_closure_line(case)``; its reason, not the case's."""
    case = await build("T:solution_deferred:bare:written")
    assert mitigation_closure_line(case) is None, "premise"
    stored = case.progress.outcome
    case.progress.outcome = stored.model_copy(
        update={
            "closure": stored.closure.model_copy(
                update={
                    "reason": ClosureReason.CLOSED_RCA_INFEASIBLE,
                    "statement": "the crafted mitigation line",
                }
            )
        }
    )
    assert _terminal_confirmation_response(case) == (
        f"{_CLOSED_REPLIES[ClosureReason.CLOSED_RCA_INFEASIBLE]} "
        "The crafted mitigation line."
    )


# ---------------------------------------------------------------------------
# Pin 6: every closure reason has a user renderer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reason", list(ClosureReason))
def test_every_closure_reason_renders_with_and_without_a_line(reason):
    bare = terminal_status_line(CaseState.CLOSED, reason, None)
    assert bare == _CLOSED_REPLIES[reason] and bare.startswith("Case closed")
    lined = terminal_status_line(CaseState.CLOSED, reason, "a line was seen")
    if reason == ClosureReason.MITIGATION_SUFFICIENT:
        assert (
            lined == "Case closed — a line was seen; root-cause analysis was deferred."
        )
    elif reason in _MITIGATION_LINE_FOLLOWS:
        assert lined == f"{bare} A line was seen."
    else:
        assert lined == bare


def test_a_resolve_renders_whatever_the_closure():
    assert terminal_status_line(CaseState.RESOLVED, None, "x") == "Case resolved."


# ---------------------------------------------------------------------------
# Fold failure: counted once and logged once; the confirmation's fallback
# re-raises a failure inside its own two derivations, as main did
# ---------------------------------------------------------------------------


@pytest.fixture
def failures(monkeypatch) -> list:
    counted: list = []
    monkeypatch.setattr(
        replies_module,
        "case_outcome_write_failures_total",
        SimpleNamespace(
            labels=lambda at: SimpleNamespace(inc=lambda: counted.append(at))
        ),
    )
    return counted


def _raise(*_args, **_kwargs):
    raise RuntimeError("probe")


def _logged(caplog) -> list:
    return [
        r
        for r in caplog.records
        if "the record could not be read for the reply" in r.getMessage()
    ]


@pytest.mark.parametrize(
    "name", ["held_check", "C:leg_rcc", "2595_unkinded_failure_rebound"]
)
async def test_a_failed_fold_confirms_from_the_deciders_read(
    monkeypatch, failures, caplog, name
):
    """A fold failure outside the confirmation's two derivations renders
    from the decider's read."""
    case = await build(name)
    monkeypatch.setattr(replies_module, "narration_outcome", _raise)
    with caplog.at_level(logging.ERROR, logger=replies_module.__name__):
        params = resolution_confirmation_params(case)
    assert params == ORACLE[name]["params"]
    assert confirmation_message(params) == ORACLE[name]["confirmation"]
    assert failures == [WritePoint.NARRATION]
    (record,) = _logged(caplog)
    assert record.exc_info is not None


async def test_a_failure_inside_a_derivation_the_fallback_reads_raises_as_on_main(
    monkeypatch, failures
):
    """``resolution_record_lines`` raising fails the fold, which is counted,
    and raises again from the fallback, which reads it too: the exception
    propagates, as the decider's read did on ``main``."""
    case = await build("held_check")

    def _broken(_case):
        raise LookupError("record lines")

    monkeypatch.setattr(outcome_module, "resolution_record_lines", _broken)
    monkeypatch.setattr(replies_module, "resolution_record_lines", _broken)
    with pytest.raises(LookupError, match="record lines"):
        resolution_confirmation_params(case)
    assert failures == [WritePoint.NARRATION]


async def test_a_failed_terminal_write_and_fold_states_the_reason_alone(
    monkeypatch, failures, caplog
):
    """The declared difference: with no record to read, the mitigation
    sentence ``main`` printed is dropped."""
    name = "T:solution_deferred:line:copied"
    case = await build(name)
    bare = _CLOSED_REPLIES[ClosureReason.SOLUTION_DEFERRED]
    assert ORACLE[name]["terminal"] != bare, "premise: main printed the line"
    monkeypatch.setattr(replies_module, "narration_outcome", _raise)
    with caplog.at_level(logging.ERROR, logger=replies_module.__name__):
        assert _terminal_confirmation_response(case) == bare
    assert failures == [WritePoint.NARRATION]
    assert len(_logged(caplog)) == 1


@pytest.mark.parametrize(
    "name", sorted(n for n in TERMINALS if n.endswith(":written")) + ["T:resolved"]
)
async def test_a_written_close_or_a_resolve_never_folds(monkeypatch, failures, name):
    case = await build(name)
    monkeypatch.setattr(replies_module, "narration_outcome", _raise)
    assert _terminal_confirmation_response(case) == ORACLE[name]["terminal"]
    assert failures == []
