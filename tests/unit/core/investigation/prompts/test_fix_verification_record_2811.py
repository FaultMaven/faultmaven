"""The ``<fix_verification>`` block rendered from the case outcome record
(#2811 S2b-1a, #2615 stage S2).

The block, all but its needs lines, the held need's mark and
``DECLINE_STILL_BINDS``, is rendered by ``narration.fix_verification`` from
the record, the case's identity, an action's text by id (``content``) and the
fresh window verdicts (``FixVerificationFresh``, ruling Q1 (a)). On every
fixture of the corpus (``fix_verification_cases_2811``) it is byte for byte
what ``main`` rendered at 9b4d0b1e1 (``fix_verification_oracle_2811.json``,
captured there through ``build_fix_verification_block``), but for the declared
differences, which no fixture shows.
"""

from __future__ import annotations

import json
import re
from dataclasses import fields
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest

from faultmaven.core.investigation.cause_assurance import fix_windows, subject_index
from faultmaven.core.investigation.narration.fix_verification import (
    CLOSE_TAG,
    OMITTED,
    OPEN_TAG,
    CaseIdentity,
    FixVerificationFresh,
    content,
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
    build_fix_verification_block,
)
from faultmaven.modules.case.contracts import (
    CURRENT_OUTCOME_SHAPE,
    AttemptOutcome,
    CaseOutcome,
    FixWindowRecord,
    FixWindowState,
    WritePoint,
)
from tests.unit.core.investigation.bound_rows import VERIFYING_ID
from tests.unit.core.investigation.prompts.fix_verification_cases_2811 import (
    CASES,
    build,
)

pytestmark = pytest.mark.unit

ORACLE = json.loads(
    (Path(__file__).parent / "fix_verification_oracle_2811.json").read_text()
)

#: Where the block may differ from ``main``, by what differs. No fixture shows
#: one; each is shown by its own test below.
DECLARED = {
    # The record carries a closure a terminal write folded; the block prints
    # none, so this one changes nothing here.
    "closure": "carried over the case's closure_reason (unchanged)",
    "accepted_in_turn": "an attempt's turn, carried once final",
    "gone_action": "a carried FAILED attempt whose action no longer reads",
    "omitted": OMITTED,
}


def test_the_oracle_covers_the_corpus():
    assert set(ORACLE) == set(CASES)


@pytest.mark.parametrize("name", sorted(CASES))
async def test_the_block_renders_as_main_did(name):
    assert build_fix_verification_block(await build(name)) == ORACLE[name]


# ---------------------------------------------------------------------------
# Pin 2: the renderer reads the record, the identity, content and fresh alone
# ---------------------------------------------------------------------------

_IDENTITY = {"case_id", "state", "current_stage", "current_turn"}


class _Poisoned:
    """A case on which every attribute read raises, but the identity fields
    and ``progress.outcome``."""

    def __init__(self, case, outcome) -> None:
        object.__setattr__(self, "_case", case)
        object.__setattr__(self, "progress", SimpleNamespace(outcome=outcome))

    def __getattribute__(self, name: str):
        if name == "progress":
            return object.__getattribute__(self, name)
        if name in _IDENTITY:
            return getattr(object.__getattribute__(self, "_case"), name)
        raise AssertionError(f"the renderer read case.{name}")


def test_fresh_holds_the_q1_verdicts_and_no_text():
    assert {f.name for f in fields(FixVerificationFresh)} == {
        "fixes",
        "problem_leg",
        "problem_leg_turn",
        "gone_row_ids",
        "fix_confirmed",
        "mitigation_verified",
        "mitigation_verify_refused",
    }


@pytest.mark.parametrize("name", sorted(CASES))
async def test_the_renderer_reads_no_case_field(name):
    case = await build(name)
    outcome = narration_outcome(case)
    subjects = subject_index(case)
    fresh = block_module._fresh(case, outcome, subjects, fix_windows(case, subjects))
    poisoned = _Poisoned(case, outcome)
    head = render_fix_verification(
        poisoned.progress.outcome,
        CaseIdentity.of(poisoned),
        partial(content, case),
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
    subjects = subject_index(case)
    fresh = block_module._fresh(case, outcome, subjects, fix_windows(case, subjects))
    head = render_fix_verification(
        outcome, CaseIdentity.of(case), partial(content, case), fresh
    )
    return "\n".join(head.lines)


def _resolution(outcome: CaseOutcome, **update) -> CaseOutcome:
    resolution = outcome.resolution.model_copy(update=update)
    return outcome.model_copy(update={"resolution": resolution})


async def test_an_off_cause_attempt_withholds_the_cited_basis():
    """The one attempt is an interception: readiness's off-cause turn is
    recorded, and it alone keeps the cited basis out of the cause leg."""
    case = await build("T:interception_only")
    outcome = narration_outcome(case)
    assert outcome.resolution.off_cause_turn == 3
    assert "the cited basis to re-check" not in _render(case, outcome)
    cleared = _resolution(outcome, off_cause_turn=None)
    assert "  - cause: missing; the cited basis to re-check: ev_" in (
        _render(case, cleared)
    )


@pytest.mark.parametrize("name", ["R:other_cause", "T:no_dated_fix"])
async def test_the_unrechecked_rows_are_recorded(name):
    case = await build(name)
    outcome = narration_outcome(case)
    assert outcome.resolution.unrechecked_row_ids == [VERIFYING_ID]
    line = f"; not re-checked since the fix: {VERIFYING_ID}"
    assert line in _render(case, outcome)
    assert line not in _render(case, _resolution(outcome, unrechecked_row_ids=[]))


# ---------------------------------------------------------------------------
# The declared differences
# ---------------------------------------------------------------------------


async def test_a_carried_turn_and_a_gone_action_are_printed_as_recorded():
    case = await build("2595_unkinded_failure_rebound")
    outcome = narration_outcome(case)
    attempt = outcome.attempts[-1]
    gone = AttemptOutcome(
        action_id="act_000000000000",
        accepted_in_turn=7,
        window=FixWindowRecord(fix_turn=7, state=FixWindowState.FAILED),
    )
    carried = outcome.model_copy(
        update={
            "attempts": [
                *outcome.attempts[:-1],
                attempt.model_copy(update={"accepted_in_turn": 5}),
                gone,
            ]
        }
    )
    text = _render(case, carried)
    assert f"[{attempt.action_id}] raise max_connections to 100 — executed turn 5:" in (
        text
    )
    assert "  - [act_000000000000]  — executed turn 7: not yet observed" in text


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


@pytest.mark.parametrize("where", ["narration_outcome", "_fresh"])
async def test_a_failed_fold_or_fresh_read_omits_only_the_block(
    monkeypatch, failures, where
):
    from tests.unit.core.investigation.prompts.test_fix_verification_1906 import (
        _block,
        _prompt,
    )

    case = await build("R:other_cause")
    whole = _prompt(case)
    monkeypatch.setattr(block_module, where, _raise)
    assert build_fix_verification_block(case) == f"{OPEN_TAG}\n{OMITTED}\n{CLOSE_TAG}"
    assert failures == [WritePoint.NARRATION]
    prompt = _prompt(case)
    assert _block(prompt) == f"{OPEN_TAG}\n{OMITTED}\n{CLOSE_TAG}"
    assert _rest(prompt, _block(prompt)) == _rest(whole, _block(whole))


def _rest(prompt: str, block: str) -> str:
    """The prompt without the block, its ``CURRENT_TIME`` line and its fence
    token (drawn per build) masked."""
    fence = re.search(r'fence="([0-9a-f]+)"', prompt).group(1)
    rest = prompt.replace(block, "").replace(fence, "FENCE")
    return re.sub(r"CURRENT_TIME: .*", "CURRENT_TIME:", rest)


async def test_no_fold_outside_the_blocks_stages(monkeypatch, failures):
    monkeypatch.setattr(block_module, "narration_outcome", _raise)
    assert build_fix_verification_block(await build("gate_one")) == ""
    assert failures == []


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
