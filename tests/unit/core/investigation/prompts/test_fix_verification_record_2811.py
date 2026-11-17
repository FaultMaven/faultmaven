"""The ``<fix_verification>`` block rendered from the case outcome record
(#2811 S2b-1a, #2615 stage S2).

The block, all but its needs lines, the held need's mark and
``DECLINE_STILL_BINDS``, is rendered by ``narration.fix_verification`` from
the record, the case's identity, an action's text by id (``content_by_id``) and the
fresh window verdicts (``FixVerificationFresh``, ruling Q1 (a)). On every
fixture of the corpus (``fix_verification_cases_2811``) it is byte for byte
what ``main`` rendered at 9b4d0b1e1 (``fix_verification_oracle_2811.json``,
captured there through ``build_fix_verification_block``), but on the one
fixture per declared difference, where it differs exactly as declared.
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
from faultmaven.core.investigation.cause_assurance import fix_windows, subject_index
from faultmaven.core.investigation.narration.fix_verification import (
    CLOSE_TAG,
    OMITTED,
    OPEN_TAG,
    CaseIdentity,
    FixVerificationFresh,
    content_by_id,
    render_fix_verification,
)
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
    assert set(DECLARED) == {"closure", "accepted_in_turn", "gone_action", "omitted"}


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


_SHOWS = {
    "closure": _shows_closure,
    "accepted_in_turn": _shows_accepted_in_turn,
    "gone_action": _shows_gone_action,
    "omitted": _shows_omitted,
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


_ID = re.compile(r"(ev|act)_[0-9a-f]{12}")


def _no_text(value) -> None:
    """No field of ``fresh``, nested ones included, is a ``str``; the ids it
    keys and lists are ids."""
    if dataclasses.is_dataclass(value):
        for f in dataclasses.fields(value):
            _no_text(getattr(value, f.name))
    elif isinstance(value, dict):
        for key, nested in value.items():
            assert _ID.fullmatch(key), key
            _no_text(nested)
    elif isinstance(value, tuple):
        for nested in value:
            assert _ID.fullmatch(nested), nested
    else:
        assert type(value) is not str, value


def test_fresh_holds_the_q1_verdicts():
    assert {f.name for f in dataclasses.fields(FixVerificationFresh)} == {
        "fixes",
        "problem_leg",
        "problem_leg_turn",
        "gone_row_ids",
        "mitigation_verify_refused",
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
    _no_text(fresh)
    poisoned = _Poisoned(case, _IDENTITY, outcome)
    actions = _Poisoned(case, {"proposed_actions"})
    head = render_fix_verification(
        poisoned.progress.outcome,
        CaseIdentity.of(poisoned),
        lambda item_id: content_by_id(actions, item_id),
        fresh,
    )
    if head is None:
        assert ORACLE[name] == ""
        return
    assert ORACLE[name].startswith("\n".join(head.lines) + "\n")
    assert head.needs == ("Outstanding symptom-verification needs" in ORACLE[name])


# ---------------------------------------------------------------------------
# Shape 3's two facts, each the gate of its line
# ---------------------------------------------------------------------------


def _render(case, outcome: CaseOutcome) -> str:
    head = render_fix_verification(
        outcome,
        CaseIdentity.of(case),
        lambda item_id: content_by_id(case, item_id),
        _fresh(case, outcome),
    )
    return "\n".join(head.lines)


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
    from faultmaven.modules.case.contracts import ProblemStatus

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
    "where", ["narration_outcome", "_fresh", "CaseIdentity", "render_fix_verification"]
)
async def test_a_failure_omits_only_the_block(monkeypatch, failures, where):
    from tests.unit.core.investigation.prompts.test_fix_verification_1906 import (
        _block,
        _prompt,
    )

    case = await build("R:other_cause")
    whole = _prompt(case)
    raising = SimpleNamespace(of=_raise) if where == "CaseIdentity" else _raise
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


async def test_a_shape_2_record_upgrades_through_the_identity_step(monkeypatch):
    case = await build("R:other_cause")
    stored = compute_case_outcome(None, case, at=WritePoint.CYCLE)
    old = CaseOutcome.model_validate(
        {
            **stored.model_dump(mode="json"),
            "shape": 2,
            "resolution": stored.resolution.model_dump(
                mode="json", exclude={"off_cause_turn", "unrechecked_row_ids"}
            ),
        }
    )
    assert CURRENT_OUTCOME_SHAPE == 3
    assert old.resolution.unrechecked_row_ids == [], "premise: loads with defaults"
    folded = compute_case_outcome(old, case, at=WritePoint.COMMIT)
    assert folded.shape == 3
    assert folded.resolution.unrechecked_row_ids == [VERIFYING_ID]

    monkeypatch.delitem(SHAPE_STEPS, 3)
    with pytest.raises(KeyError):
        compute_case_outcome(old, case, at=WritePoint.COMMIT)
