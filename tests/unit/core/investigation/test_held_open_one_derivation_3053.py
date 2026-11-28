"""The HELD_OPEN verdict and the record's ``identification_held_open`` read one
fold-time derivation (#3053 S3d-5, #2615 stage S3).

The spec: the ruling of 2026-11-26 on #2615 (items 1-4), the pin-3 ruling of
2026-11-28 on #3053 and the design critique of the plan.

- One function, ``identification_is_held_open``, decides whether
  identification is held open: the cause recompute stores it and the record's
  fold computes it, so the verdict and the flag on a record agree with its
  ``held_open_root_ids``.
- A fold that runs no recompute (a prompt folded after a pre-prompt retire or
  refute, a replayed bundle) reads the fresh value, in both directions, and so
  does every section of the record that reads the cause re-checks
  (``cause_recheck_status``, seeded by the fold).
- On every cycle-written record of the golden and parity corpora the fresh
  value is the stored flag: no cycle-written record changes.
"""

from __future__ import annotations

import pytest

from faultmaven.core.investigation.causal_graph.projection import (
    held_open_root_ids,
    identification_is_held_open,
)
from faultmaven.core.investigation.milestone_engine import turn_application
from faultmaven.core.investigation.milestone_engine.cause_state import (
    _recompute_cause_state_from_chain,
)
from faultmaven.core.investigation.narration.notices import empty_pool_text
from faultmaven.core.investigation.outcome import compute_case_outcome
from faultmaven.modules.case.contracts import (
    CaseState,
    HypothesisState,
    PoolEmptyReason,
    RecheckReason,
    WritePoint,
)
from tests.unit.core.investigation import golden_cases_2636 as golden
from tests.unit.core.investigation import offer_cases_2811 as offers
from tests.unit.core.investigation import reply_cases_2811 as replies
from tests.unit.core.investigation import test_narrator_record_parity_2811 as parity
from tests.unit.core.investigation.prompts.test_fix_verification_1906 import (
    _case,
    hold_open,
)

pytestmark = pytest.mark.unit


def _narrated(case):
    """The record a prompt or the narration folds: no recompute runs first."""
    return compute_case_outcome(None, case, at=WritePoint.NARRATION)


# ---------------------------------------------------------------------------
# Stale-flag positive controls, both directions (ruling of 2026-11-26, item 3)
# ---------------------------------------------------------------------------


def _stale_true():
    """What a pre-prompt retire leaves: the holding hypothesis is retired and
    no recompute has run, so the stored flag still says held open beside no
    held-open root."""
    case = offers._held_open()
    assert case.progress.cause_identification_held_open, "premise: held open"
    (root,) = held_open_root_ids(case)
    for hyp in case.hypotheses.values():
        if hyp.root_node_id == root:
            hyp.state = HypothesisState.RETIRED
    assert not held_open_root_ids(case), "premise: no root holds it now"
    assert case.progress.cause_identification_held_open, "premise: a stale flag"
    return case


def test_a_stale_held_open_flag_is_not_the_records_verdict():
    """``main`` read HELD_OPEN off the flag beside empty ``held_open_root_ids``,
    so ``empty_pool_text`` returned None and the line dropped. The record now
    reads the roots: not held open, and the next reason's line renders."""
    cause = _narrated(_stale_true()).cause
    assert cause.held_open_root_ids == []
    assert cause.identification_held_open is False
    assert cause.pool_empty_reason == PoolEmptyReason.NO_CAUSE
    assert empty_pool_text(cause) == (
        "no root cause is on record that a re-check can confirm."
    )


def test_a_held_open_root_the_flag_missed_is_the_records_verdict():
    """The other direction: a held-open root stands and the stored flag says
    not held. The record's verdict and flag follow the root, and the line
    names it."""
    case = offers._held_open()
    case.progress.cause_identification_held_open = False
    cause = _narrated(case).cause
    (root,) = cause.held_open_root_ids
    assert cause.identification_held_open is True
    assert cause.pool_empty_reason == PoolEmptyReason.HELD_OPEN
    assert root in empty_pool_text(cause)


# ---------------------------------------------------------------------------
# One value across the fold's sections (critique point 3)
# ---------------------------------------------------------------------------


def _not_held():
    """The fix's cause re-checked and the problem seen gone, nothing held."""
    case = _case("treatment", problem=True)
    _recompute_cause_state_from_chain(case)
    assert not case.progress.cause_identification_held_open, "premise"
    return case


def _held():
    """The same, beside a root whose why is open: identification held."""
    case = hold_open(_case("treatment", problem=True))
    assert case.progress.cause_identification_held_open, "premise"
    return case


def _sections(record) -> dict:
    resolution = record.resolution
    return {
        "recheck": record.cause.recheck,
        "verdict": resolution.verdict,
        "reason": resolution.reason,
        "confirmation_gaps": resolution.confirmation_gaps,
        "cause_row_ids": resolution.cause_row_ids,
        "pending_check_need_id": resolution.pending_check_need_id,
        "pending_check_reason": resolution.pending_check_reason,
    }


@pytest.mark.parametrize(
    ("build", "recheck"),
    [(_not_held, RecheckReason.COUNTS), (_held, RecheckReason.HELD_OPEN)],
    ids=["stale_true", "stale_false"],
)
def test_a_stale_flag_moves_no_section_of_the_record(build, recheck):
    """A narration fold of the case with its flag flipped reads what the fold
    of the case as the recompute left it reads: ``cause.recheck`` and the
    resolution's verdict, confirmation gaps and pending check, which reach
    ``cause_recheck_status`` through other paths of the same fold."""
    case = build()
    want = _sections(_narrated(case))
    assert set(want["recheck"].values()) == {recheck}, "premise"
    case.progress.cause_identification_held_open ^= True
    assert _sections(_narrated(case)) == want


# ---------------------------------------------------------------------------
# Corpus equality (ruling of 2026-11-26, item 3): no cycle-written record moves
# ---------------------------------------------------------------------------


def test_every_cycle_written_record_reads_the_stored_flag(monkeypatch):
    """At every cycle's record over the golden corpus's whole turns (after the
    final recompute, which stores the flag from the same function, where the
    cycle ran one) the fresh value is the stored flag. No oracle difference:
    none of these records is a stale-flag fold."""
    seen: list = []
    real = turn_application.record_case_outcome

    def _check(case, *, at):
        if at == WritePoint.CYCLE:
            stored = case.progress.cause_identification_held_open
            seen.append((identification_is_held_open(case), stored))
        return real(case, at=at)

    monkeypatch.setattr(turn_application, "record_case_outcome", _check)
    for build in (
        *golden.CYCLE_SCENARIOS.values(),
        *golden.B1C_SCENARIOS.values(),
        *golden.B1D_SCENARIOS.values(),
    ):
        build()
    assert len(seen) >= 5, "premise: the corpus runs whole turns"
    assert [fresh for fresh, stored in seen if fresh != stored] == []
    assert any(stored for _fresh, stored in seen), "premise: one is held open"


#: The parity and reply corpora, and the parity re-check reasons.
_CORPUS = {
    **{name: (lambda n=name: replies.build(n)) for name in replies.CASES},
    **{f"R:{name}": build for name, (build, _shows) in parity.REASONS.items()},
}


async def _cycled(name: str):
    """The fixture, the recompute run as a cycle runs it before its record."""
    case = await _CORPUS[name]()
    if case.state == CaseState.INVESTIGATING:
        _recompute_cause_state_from_chain(case)
    return case


@pytest.mark.parametrize("name", sorted(_CORPUS))
async def test_every_parity_fixture_reads_the_stored_flag(name):
    """Over the parity and reply corpora: the record's value is the stored
    flag."""
    case = await _cycled(name)
    cause = _narrated(case).cause
    assert cause.identification_held_open == (
        case.progress.cause_identification_held_open
    )


async def test_the_parity_corpus_holds_a_held_open_record():
    """A positive control for the parametrized equality above: a fixture it
    compares is held open."""
    held = [
        name
        for name in sorted(_CORPUS)
        if (await _cycled(name)).progress.cause_identification_held_open
    ]
    assert held
