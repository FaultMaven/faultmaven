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
  (``cause_recheck_status``) or whether a cause is identified
  (``cause_identification_leg``), both seeded by the fold.
- On every cycle-written record of the golden and parity corpora the
  record's value is the flag ``main`` stored there (an oracle captured from
  ``main``): no cycle-written record changes. No production cycle writes its
  record after a stale flag: every INVESTIGATING cycle runs step 1's
  recompute and, gated on it, the final one (``turn_application.py:417``); a
  terminal case leaves the turn before any hypothesis intent
  (``engine.py:770``); an INQUIRY case holds no hypotheses.
  ``test_turn_reorder_2636``'s stale CYCLE write is a fixture of the fold
  alone.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from faultmaven.core.investigation.causal_graph.projection import (
    held_open_root_ids,
    identification_is_held_open,
)
from faultmaven.core.investigation.milestone_engine.cause_state import (
    _recompute_cause_state_from_chain,
)
from faultmaven.core.investigation.narration.notices import empty_pool_text
from faultmaven.core.investigation.outcome import compute_case_outcome
from faultmaven.modules.case.contracts import (
    CaseState,
    CauseState,
    ConfidenceLevel,
    HypothesisState,
    PoolEmptyReason,
    RecheckReason,
    RootCauseConclusion,
    WritePoint,
)
from tests.unit.core.investigation import held_open_cases_3053 as cases
from tests.unit.core.investigation import offer_cases_2811 as offers
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


def _rcc_not_held():
    """No row re-checks the cause and nothing is held; the cause is known on
    the conclusion backstop alone (``cause_state`` CANDIDATES, an RCC at
    0.8), so ``cause_identification_leg`` reads the held-open state."""
    case = _case("treatment", cause=False, problem=True)
    _recompute_cause_state_from_chain(case)
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause="max_connections is set to 20, below the worker pool size",
        mechanism="the pool waits on connections the server refuses",
        confidence_level=ConfidenceLevel.CONFIDENT,
        likelihood=0.8,
    )
    assert case.progress.cause_state == CauseState.CANDIDATES, "premise"
    assert not held_open_root_ids(case), "premise: nothing held"
    assert not case.progress.cause_identification_held_open, "premise"
    return case


def _sections(record) -> dict:
    resolution = record.resolution
    return {
        "recheck": record.cause.recheck,
        "identification_leg": record.cause.identification_leg,
        "verdict": resolution.verdict,
        "reason": resolution.reason,
        "missing": resolution.missing,
        "statement": resolution.statement,
        "confirmation_gaps": resolution.confirmation_gaps,
        "cause_row_ids": resolution.cause_row_ids,
        "pending_check_need_id": resolution.pending_check_need_id,
        "pending_check_reason": resolution.pending_check_reason,
        "closure": record.closure,
    }


@pytest.mark.parametrize(
    ("build", "recheck", "leg"),
    [
        (_not_held, {RecheckReason.COUNTS}, None),
        (_held, {RecheckReason.HELD_OPEN}, None),
        (_rcc_not_held, set(), "rcc"),
    ],
    ids=["stale_true", "stale_false", "stale_true_rcc_leg"],
)
def test_a_stale_flag_moves_no_section_of_the_record(build, recheck, leg):
    """A narration fold of the case with its flag flipped reads what the fold
    of the case as the recompute left it reads: ``cause.recheck`` and the
    identification leg, and the resolution's verdict, missing items,
    statement, confirmation gaps and pending check and the closure, which
    reach ``cause_recheck_status`` and ``cause_identification_leg`` through
    other paths of the same fold."""
    case = build()
    want = _sections(_narrated(case))
    assert set(want["recheck"].values()) == recheck, "premise"
    assert want["identification_leg"] == leg, "premise"
    case.progress.cause_identification_held_open ^= True
    assert _sections(_narrated(case)) == want


# ---------------------------------------------------------------------------
# Corpus equality (ruling of 2026-11-26, item 3): no cycle-written record moves
# ---------------------------------------------------------------------------

#: The flag ``main`` stored at each compared point (``held_open_cases_3053``).
ORACLE = json.loads(
    (Path(__file__).parent / "held_open_oracle_3053.json").read_text(encoding="utf-8")
)


def _recorded(case) -> bool:
    return case.progress.outcome.cause.identification_held_open


def test_every_cycle_written_record_reads_the_flag_main_stored():
    """At every cycle's record over the golden corpus's whole turns, the
    record's value is the flag ``main``'s cycle stored at the same write. No
    oracle difference: none of these records is a stale-flag fold."""
    got = cases.golden_reads(_recorded)
    assert got == ORACLE["golden"]
    assert sum(len(seen) for seen in got.values()) >= 5, "premise: whole turns"
    assert any(
        any(seen) for seen in ORACLE["golden"].values()
    ), "premise: one is held open"


@pytest.mark.parametrize("name", sorted(cases.CORPUS))
async def test_every_parity_fixture_reads_the_flag_main_stored(name):
    """Over the parity and reply corpora: the record's value is the flag
    ``main``'s recompute stored."""
    case = await cases.cycled(name)
    assert _narrated(case).cause.identification_held_open == ORACLE["corpus"][name]


def test_the_oracle_covers_the_corpus_and_holds_a_held_open_record():
    """The oracle names every compared fixture, and one of them is held open:
    a positive control for the parametrized equality above."""
    assert set(ORACLE["corpus"]) == set(cases.CORPUS)
    assert set(ORACLE["golden"]) == set(cases.GOLDEN)
    assert any(ORACLE["corpus"].values())
