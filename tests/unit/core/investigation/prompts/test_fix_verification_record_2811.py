"""The ``<fix_verification>`` block rendered from the case outcome record
(#2811 S2b-1a and S2b-1b-i, #2615 stage S2).

The block is rendered by ``narration.fix_verification`` from the record, the
case's identity, the text of an action or a need by id (``content_by_id``),
the fresh window verdicts (``FixVerificationFresh``, ruling Q1 (a)) and the
needs pool (``NeedsView``, ruling 2 of 2026-11-17). On every fixture of the
corpus (``fix_verification_cases_2811``) it is byte for byte what ``main``
rendered (``fix_verification_oracle_2811.json``, captured through
``build_fix_verification_block``: at 9b4d0b1e1, and the fixtures S2b-1b-i
adds at 92e98cdbf), but on the one fixture per declared difference, where it
differs exactly as declared.
"""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from faultmaven.core.investigation import outcome as outcome_module
from faultmaven.core.investigation.cause_assurance import (
    fix_windows,
    pending_symptom_check,
    subject_index,
)
from faultmaven.core.investigation.narration.fix_verification import (
    CLOSE_TAG,
    DECLINE_STILL_BINDS,
    OMITTED,
    OPEN_TAG,
    CaseIdentity,
    FixVerificationFresh,
    content_by_id,
    render_fix_verification,
)
from faultmaven.core.investigation.narration.needs import NeedsView, NeedView
from faultmaven.core.investigation.outcome import (
    SHAPE_STEPS,
    compute_case_outcome,
    narration_outcome,
)
from faultmaven.core.investigation.prompts.context_builder import (
    fix_verification_block as block_module,
)
from faultmaven.core.investigation.prompts.context_builder.fix_verification_block import (
    NarrationFold,
    build_fix_verification_block,
)
from faultmaven.core.investigation.prompts.templates import assembly as templates
from faultmaven.modules.case.contracts import (
    CURRENT_OUTCOME_SHAPE,
    CaseOutcome,
    EvidenceCategory,
    PendingCheckReason,
    ProblemStatus,
    ResolutionReason,
    WritePoint,
)
from tests.unit.core.investigation.bound_rows import VERIFYING_ID, bound_row
from tests.unit.core.investigation.given_tools import DEFAULT_GIVEN
from tests.unit.core.investigation.prompts.fix_verification_cases_2811 import (
    CASES,
    DECLARED,
    build,
)

pytestmark = pytest.mark.unit

ORACLE = json.loads(
    (Path(__file__).parent / "fix_verification_oracle_2811.json").read_text()
)
_OMITTED = f"{OPEN_TAG}\n{OMITTED}\n{CLOSE_TAG}"
_DECLARED_FIXTURES = {name for name, _build in DECLARED.values()}


def test_the_oracle_covers_the_corpus():
    assert set(ORACLE) == set(CASES)


@pytest.mark.parametrize("name", sorted(set(CASES) - _DECLARED_FIXTURES))
async def test_the_block_renders_as_main_did(name):
    assert build_fix_verification_block(await build(name)) == ORACLE[name]


# ---------------------------------------------------------------------------
# The declared differences, each shown by its fixture, and only those
# ---------------------------------------------------------------------------


def test_the_declared_differences_are_the_plans():
    assert set(DECLARED) == {
        "closure",
        "accepted_in_turn",
        "gone_action",
        "omitted",
        "invalidated",
    }


async def _shows_closure(case, block, want):
    """The record keeps the closure a terminal write folded; the block prints
    no closure, so it renders as ``main`` did."""
    assert "closure" in narration_outcome(case).disagreements
    return block == want == ""


async def _shows_accepted_in_turn(case, block, want):
    """The record's carried turn is printed; the window's verdict is the one
    ``main`` reads at the action's current turn."""
    assert "executed turn 7: succeeded" in want, "premise: main's fresh read"
    return block == want.replace("executed turn 7:", "executed turn 6:")


async def _shows_gone_action(case, block, want):
    """The carried FAILED attempt is listed, its action's text gone; every
    other line is ``main``'s."""
    extra = [line for line in block.splitlines() if line not in want.splitlines()]
    return (
        len(extra) == 1
        and re.fullmatch(r"  - \[act_\w+\]  — executed turn 6: \w.*", extra[0])
        and [line for line in block.splitlines() if line != extra[0]]
        == want.splitlines()
    )


async def _shows_omitted(case, block, want):
    return block == _OMITTED and want.startswith(OPEN_TAG)


_HOLDS = " (holds the resolution offer until answered)"


async def _shows_invalidated(case, block, want):
    """On a false alarm readiness answers before it reads the held need, so
    the record holds none and the need is listed unmarked; ``main`` marked
    it from a fresh read."""
    (need,) = case.evidence_needs
    assert pending_symptom_check(case) is need, "premise: main's fresh read"
    resolution = narration_outcome(case).resolution
    assert resolution.reason == ResolutionReason.FALSE_ALARM
    assert resolution.pending_check_need_id is None
    assert f"[{need.need_id}]" in want and _HOLDS in want
    return block == want.replace(_HOLDS, "")


_SHOWS = {
    "closure": _shows_closure,
    "accepted_in_turn": _shows_accepted_in_turn,
    "gone_action": _shows_gone_action,
    "omitted": _shows_omitted,
    "invalidated": _shows_invalidated,
}


@pytest.mark.parametrize("difference", sorted(DECLARED))
async def test_each_declared_difference_is_shown_by_its_fixture(difference):
    name, _build = DECLARED[difference]
    case = await build(name)
    block = build_fix_verification_block(case)
    assert await _SHOWS[difference](case, block, ORACLE[name])


# ---------------------------------------------------------------------------
# Pin 2: the renderer reads the record, the identity, content and fresh alone
# ---------------------------------------------------------------------------

_IDENTITY = {"case_id", "state", "current_stage", "current_turn"}


class _Poisoned:
    """A case on which every attribute read raises but ``allowed``, and
    ``progress.outcome`` when an outcome is given."""

    def __init__(self, case, allowed, outcome=None) -> None:
        object.__setattr__(self, "_case", case)
        object.__setattr__(self, "_allowed", allowed)
        object.__setattr__(self, "progress", SimpleNamespace(outcome=outcome))

    def __getattribute__(self, name: str):
        if name == "progress" and object.__getattribute__(self, name).outcome:
            return object.__getattribute__(self, name)
        if name in object.__getattribute__(self, "_allowed"):
            return getattr(object.__getattribute__(self, "_case"), name)
        raise AssertionError(f"the renderer read case.{name}")


_ID = re.compile(r"(ev|act|eneed|hyp)_[0-9a-f]{12}")


def _no_text(value) -> None:
    """No field of ``fresh`` or ``NeedsView``, nested ones included, is text:
    every ``str`` it holds, as a field, a key or a member, is an id."""
    if dataclasses.is_dataclass(value):
        for f in dataclasses.fields(value):
            _no_text(getattr(value, f.name))
    elif isinstance(value, dict):
        for key, nested in value.items():
            _no_text(key)
            _no_text(nested)
    elif isinstance(value, tuple):
        for nested in value:
            _no_text(nested)
    elif type(value) is str:
        assert _ID.fullmatch(value), value


def test_fresh_holds_the_q1_verdicts():
    assert {f.name for f in dataclasses.fields(FixVerificationFresh)} == {
        "fixes",
        "problem_leg",
        "problem_leg_turn",
        "gone_row_ids",
        "mitigation_verify_refused",
    }


def test_the_needs_view_holds_the_rulings_reading():
    """Ruling 2 of 2026-11-17: the pool's own state, read through one typed
    accessor that pin 2 exempts permanently."""
    assert [f.name for f in dataclasses.fields(NeedsView)] == ["needs"]
    assert {f.name for f in dataclasses.fields(NeedView)} == {
        "need_id",
        "purpose",
        "state",
        "outstanding",
        "exhausted",
    }


def _fresh(case, outcome):
    subjects = subject_index(case)
    return block_module._fresh(case, outcome, subjects, fix_windows(case, subjects))


@pytest.mark.parametrize("name", sorted(set(CASES) - _DECLARED_FIXTURES))
async def test_the_renderer_reads_no_case_field(name):
    case = await build(name)
    if not block_module._shown(case):
        assert ORACLE[name] == ""
        return
    outcome = narration_outcome(case)
    fresh = _fresh(case, outcome)
    needs = NeedsView.of(case)
    _no_text(fresh)
    _no_text(needs)
    poisoned = _Poisoned(case, _IDENTITY, outcome)
    texts = _Poisoned(case, {"proposed_actions", "evidence_needs"})
    lines = render_fix_verification(
        poisoned.progress.outcome,
        CaseIdentity.of(poisoned),
        lambda item_id: content_by_id(texts, item_id),
        fresh,
        needs,
    )
    assert ("" if lines is None else "\n".join(lines)) == ORACLE[name]


# ---------------------------------------------------------------------------
# Shape 3's two facts, each the gate of its line
# ---------------------------------------------------------------------------


def _render(case, outcome: CaseOutcome) -> str:
    lines = render_fix_verification(
        outcome,
        CaseIdentity.of(case),
        lambda item_id: content_by_id(case, item_id),
        _fresh(case, outcome),
        NeedsView.of(case),
    )
    return "\n".join(lines)


def _resolution(outcome: CaseOutcome, **update) -> CaseOutcome:
    resolution = outcome.resolution.model_copy(update=update)
    return outcome.model_copy(update={"resolution": resolution})


async def test_an_off_cause_attempt_withholds_the_cited_basis():
    """The one attempt is an interception: the off-cause turn is recorded,
    and it alone keeps the cited basis out of the cause leg."""
    case = await build("T:interception_only")
    outcome = narration_outcome(case)
    assert outcome.resolution.off_cause_turn == 3
    assert "the cited basis to re-check" not in _render(case, outcome)
    cleared = _resolution(outcome, off_cause_turn=None)
    assert "  - cause: missing; the cited basis to re-check: ev_" in (
        _render(case, cleared)
    )


async def test_the_off_cause_turn_is_recorded_whatever_readiness_answers():
    """On a false alarm readiness returns before reading the off-cause
    attempt; the record still holds it, and the block withholds the basis as
    ``main`` did."""
    case = await build("T:interception_only")
    want = build_fix_verification_block(case)
    case.progress.problem_status = ProblemStatus.INVALIDATED
    assert narration_outcome(case).resolution.off_cause_turn == 3
    assert build_fix_verification_block(case) == want


@pytest.mark.parametrize("name", ["R:other_cause", "T:no_dated_fix"])
async def test_the_unrechecked_rows_are_recorded(name):
    case = await build(name)
    outcome = narration_outcome(case)
    assert outcome.resolution.unrechecked_row_ids == [VERIFYING_ID]
    line = f"; not re-checked since the fix: {VERIFYING_ID}"
    assert line in _render(case, outcome)
    assert line not in _render(case, _resolution(outcome, unrechecked_row_ids=[]))


async def test_no_row_is_unrechecked_without_the_cause_leg():
    outcome = narration_outcome(await build("R:held_open"))
    assert outcome.resolution.cause_row_ids == [], "premise"
    assert outcome.resolution.unrechecked_row_ids == []


# ---------------------------------------------------------------------------
# The held need's mark, from the record (shape 4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, reason",
    [
        ("N:held_pending", PendingCheckReason.UNANSWERED),
        ("N:held_exhausted", PendingCheckReason.UNANSWERED),
        ("N:held_fulfilled", PendingCheckReason.FULFILLED_BEFORE_ASK),
    ],
)
async def test_the_held_need_and_its_reason_are_recorded(name, reason):
    case = await build(name)
    resolution = narration_outcome(case).resolution
    held = pending_symptom_check(case)
    assert resolution.pending_check_need_id == held.need_id
    assert resolution.pending_check_reason == reason


async def test_no_reason_without_a_held_need():
    resolution = narration_outcome(await build("N:none_stopped")).resolution
    assert resolution.pending_check_need_id is None
    assert resolution.pending_check_reason is None


async def test_the_mark_renders_the_records_reason_and_decline():
    """The mark reads the record alone: its reason gates "predate the ask",
    ``declined_stands`` gates ``DECLINE_STILL_BINDS``, and the held id gates
    the mark and the order."""
    case = await build("N:held_exhausted")
    outcome = narration_outcome(case)
    held = outcome.resolution.pending_check_need_id
    plain = _render(case, outcome)
    assert f"needs:\n  - [{held}] " in plain, "premise: listed first"
    fulfilled = _render(
        case,
        _resolution(
            outcome,
            pending_check_reason=PendingCheckReason.FULFILLED_BEFORE_ASK,
            declined_stands=True,
        ),
    )
    mark = f": the rows it cites predate the ask{DECLINE_STILL_BINDS})"
    assert plain.replace("until answered)", "until answered" + mark) == fulfilled
    released = _render(case, _resolution(outcome, pending_check_need_id=None))
    assert "holds the resolution offer" not in released
    assert "(1 the engine has STOPPED surfacing" in released


async def test_a_need_reads_its_current_text():
    """A need's ``request_text`` can be rewritten (``evidence_need_updates``):
    ``content`` reads it as it stands."""
    case = await build("N:held_pending")
    need = case.evidence_needs[0]
    need.request_text = "the scrape target, re-read"
    assert content_by_id(case, need.need_id) == "the scrape target, re-read"
    assert f"[{need.need_id}] the scrape target, re-read (holds" in (
        build_fix_verification_block(case)
    )


async def test_a_carried_turn_keeps_the_fresh_verdict():
    """The window is read at the action's current turn, not the carried one."""
    case = await build("D:carried_turn")
    outcome = narration_outcome(case)
    (attempt,) = [a for a in outcome.attempts if a.action_id]
    assert attempt.accepted_in_turn == 6, "premise: carried"
    fix = _fresh(case, outcome).fixes[attempt.action_id]
    assert fix.state == "succeeded"


# ---------------------------------------------------------------------------
# The fold: once per assembly, its failure omits only the block
# ---------------------------------------------------------------------------


@pytest.fixture
def failures(monkeypatch) -> list:
    counted: list = []
    monkeypatch.setattr(
        block_module,
        "case_outcome_write_failures_total",
        SimpleNamespace(
            labels=lambda at: SimpleNamespace(inc=lambda: counted.append(at))
        ),
    )
    return counted


def _raise(*_args, **_kwargs):
    raise RuntimeError("probe")


def _rest(prompt: str, block: str) -> str:
    """The prompt without the block, its ``CURRENT_TIME`` line and its fence
    token (drawn per build) masked."""
    fence = re.search(r'fence="([0-9a-f]+)"', prompt).group(1)
    rest = prompt.replace(block, "").replace(fence, "FENCE")
    return re.sub(r"CURRENT_TIME: .*", "CURRENT_TIME:", rest)


@pytest.mark.parametrize(
    "where",
    [
        "narration_outcome",
        "_fresh",
        "CaseIdentity",
        "NeedsView",
        "render_fix_verification",
    ],
)
async def test_a_failure_omits_only_the_block(monkeypatch, failures, where):
    from tests.unit.core.investigation.prompts.test_fix_verification_1906 import (
        _block,
        _prompt,
    )

    case = await build("R:other_cause")
    whole = _prompt(case)
    raising = (
        SimpleNamespace(of=_raise) if where in ("CaseIdentity", "NeedsView") else _raise
    )
    monkeypatch.setattr(block_module, where, raising)
    assert build_fix_verification_block(case) == _OMITTED
    assert failures == [WritePoint.NARRATION]
    prompt = _prompt(case)
    assert _block(prompt) == _OMITTED
    assert _rest(prompt, _block(prompt)) == _rest(whole, _block(whole))


_STAGES = ["narration_outcome", "_fresh", "render_fix_verification"]


@pytest.mark.parametrize("where", _STAGES)
async def test_a_failed_counter_still_omits_the_block(monkeypatch, where):
    """A metrics error is swallowed where the counter is the only guard (the
    fresh read and the render) as on the fold's path; the build never
    raises."""
    monkeypatch.setattr(block_module, where, _raise)
    monkeypatch.setattr(
        block_module,
        "case_outcome_write_failures_total",
        SimpleNamespace(labels=lambda at: SimpleNamespace(inc=_raise)),
    )
    assert build_fix_verification_block(await build("R:other_cause")) == _OMITTED


@pytest.mark.parametrize("where", _STAGES)
async def test_one_fold_serves_and_counts_once_per_assembly(
    monkeypatch, failures, where
):
    """Three builds of one assembly: the record is folded once, and a
    failure at any stage is counted once."""
    case = await build("R:other_cause")
    calls: list = []
    real = block_module.narration_outcome

    def fold(c):
        calls.append(c)
        return _raise() if where == "narration_outcome" else real(c)

    monkeypatch.setattr(block_module, "narration_outcome", fold)
    if where != "narration_outcome":
        monkeypatch.setattr(block_module, where, _raise)
    assembly = NarrationFold(case)
    for _ in range(3):
        assert build_fix_verification_block(case, assembly) == _OMITTED
    assert (len(calls), failures) == (1, [WritePoint.NARRATION])


async def test_each_failure_is_logged_and_counted_once(monkeypatch, failures, caplog):
    """A ``NeedsView`` that raises on each of an assembly's three builds: each
    failure is logged, the count is one."""
    monkeypatch.setattr(block_module, "NeedsView", SimpleNamespace(of=_raise))
    case = await build("N:held_pending")
    assembly = NarrationFold(case)
    with caplog.at_level("ERROR", logger=block_module.__name__):
        for _ in range(3):
            assert build_fix_verification_block(case, assembly) == _OMITTED
    logged = [r for r in caplog.records if "was not rendered" in r.getMessage()]
    assert len(logged) == 3
    assert failures == [WritePoint.NARRATION]


@pytest.mark.parametrize("name", ["gate_one", "terminal_qa", "M:refused"])
async def test_no_fold_where_main_shows_no_block(monkeypatch, failures, name):
    case = await build(name)
    if name == "M:refused":
        # MITIGATION with neither a SOLUTION nor the mitigation dated.
        case.proposed_actions = []
    monkeypatch.setattr(block_module, "narration_outcome", _raise)
    assert build_fix_verification_block(case) == ""
    assert failures == []


async def test_an_assembly_that_rebuilds_the_context_folds_once():
    """Under a target the first cut overshoots, ``get_prompt_for_case``
    builds the context again; the block's record is folded once."""
    case = await build("R:other_cause")
    for i in range(60):
        bound_row(
            case,
            EvidenceCategory.CAUSAL_EVIDENCE,
            turn=2,
            summary=f"pool log line {i}",
            extract="connection refused by pgbouncer " * 60,
        )
    real_fold = outcome_module.compute_case_outcome
    real_ctx = templates.build_investigation_context
    rebuilt = False
    for target in range(20000, 40001, 500):
        counts = {"fold": 0, "ctx": 0}

        def fold(*args, **kwargs):
            counts["fold"] += 1
            return real_fold(*args, **kwargs)

        def ctx(*args, **kwargs):
            counts["ctx"] += 1
            return real_ctx(*args, **kwargs)

        with (
            patch.object(outcome_module, "compute_case_outcome", fold),
            patch.object(templates, "build_investigation_context", ctx),
        ):
            prompt = templates.get_prompt_for_case(
                case, "status", given_tools=DEFAULT_GIVEN, target_tokens=target
            )
        if counts["ctx"] > 1 and OPEN_TAG in prompt:
            rebuilt = True
            assert counts["fold"] == 1, counts
    assert rebuilt, "premise: some target rebuilds the context"


# ---------------------------------------------------------------------------
# The shape
# ---------------------------------------------------------------------------


def _stored(case, shape: int, *dropped: str) -> CaseOutcome:
    """The case's record as a shape-``shape`` writer stored it, without the
    resolution fields later shapes add."""
    stored = compute_case_outcome(None, case, at=WritePoint.CYCLE)
    return CaseOutcome.model_validate(
        {
            **stored.model_dump(mode="json"),
            "shape": shape,
            "resolution": stored.resolution.model_dump(
                mode="json", exclude=set(dropped)
            ),
        }
    )


async def test_a_shape_2_record_upgrades_through_the_identity_steps(monkeypatch):
    case = await build("R:other_cause")
    old = _stored(case, 2, "off_cause_turn", "unrechecked_row_ids")
    assert old.resolution.unrechecked_row_ids == [], "premise: loads with defaults"
    folded = compute_case_outcome(old, case, at=WritePoint.COMMIT)
    assert folded.shape == CURRENT_OUTCOME_SHAPE
    assert folded.resolution.unrechecked_row_ids == [VERIFYING_ID]

    monkeypatch.delitem(SHAPE_STEPS, 3)
    with pytest.raises(KeyError):
        compute_case_outcome(old, case, at=WritePoint.COMMIT)


async def test_a_shape_3_record_upgrades_to_4(monkeypatch):
    """Shape 4 adds ``pending_check_reason``: a shape-3 record loads with its
    default and folds to shape 4 with the reason; without ``_shape_4`` the
    upgrade raises."""
    case = await build("N:held_fulfilled")
    old = _stored(case, 3, "pending_check_reason")
    assert CURRENT_OUTCOME_SHAPE == 4
    assert old.resolution.pending_check_reason is None, "premise: the default"
    folded = compute_case_outcome(old, case, at=WritePoint.COMMIT)
    assert folded.shape == 4
    assert (
        folded.resolution.pending_check_reason
        == PendingCheckReason.FULFILLED_BEFORE_ASK
    )

    monkeypatch.delitem(SHAPE_STEPS, 4)
    with pytest.raises(KeyError):
        compute_case_outcome(old, case, at=WritePoint.COMMIT)
