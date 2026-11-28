"""The corpus the held-open pins compare (#3053 S3d-5), and its capture.

Two corpora: the golden corpus's whole turns (``golden_cases_2636``'s
``CYCLE``, ``B1C`` and ``B1D`` scenarios), each cycle's record write in
order; and the parity and reply corpora (``reply_cases_2811.CASES``) with
the parity re-check reasons (``test_narrator_record_parity_2811.REASONS``),
each after the cause recompute a cycle runs before its record.

``capture_all`` reads the stored ``cause_identification_held_open`` at each
of them. Its output on ``main`` is ``held_open_oracle_3053.json``, captured
by running ``capture_all`` on a ``git archive`` extract of ``a2c02e8df``
with this file and the S3d-5 branch's rebuilt fixture modules copied in
(``test_fix_verification_1906``, ``test_narrator_record_parity_2811`` and
``test_case_outcome_fold_2636``, its import of ``identification_is_held_open``
replaced by the same expression, which ``main`` lacks), never typed by
hand. ``test_held_open_one_derivation_3053`` compares the head's record
(``cause.identification_held_open``) at the same points against it.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager

from faultmaven.core.investigation.milestone_engine import turn_application
from faultmaven.core.investigation.milestone_engine.cause_state import (
    _recompute_cause_state_from_chain,
)
from faultmaven.modules.case.contracts import CaseState, WritePoint
from tests.unit.core.investigation import golden_cases_2636 as golden
from tests.unit.core.investigation import reply_cases_2811 as replies
from tests.unit.core.investigation import test_narrator_record_parity_2811 as parity

#: The golden scenarios, by ``group:name``.
GOLDEN = {
    f"{group}:{name}": build
    for group, scenarios in (
        ("cycle", golden.CYCLE_SCENARIOS),
        ("b1c", golden.B1C_SCENARIOS),
        ("b1d", golden.B1D_SCENARIOS),
    )
    for name, build in scenarios.items()
}

#: The parity and reply corpora, and the parity re-check reasons.
CORPUS = {
    **{name: (lambda n=name: replies.build(n)) for name in replies.CASES},
    **{f"R:{name}": build for name, (build, _shows) in parity.REASONS.items()},
}


@contextmanager
def cycle_writes(read):
    """``read(case)`` at each cycle's record write, after the write, in
    order, into the list this yields."""
    seen: list = []
    real = turn_application.record_case_outcome

    def _write(case, *, at):
        written = real(case, at=at)
        if at == WritePoint.CYCLE:
            seen.append(read(case))
        return written

    turn_application.record_case_outcome = _write
    try:
        yield seen
    finally:
        turn_application.record_case_outcome = real


def golden_reads(read) -> dict[str, list]:
    """``read`` at every cycle write of each golden scenario."""
    out = {}
    for name, build in GOLDEN.items():
        with cycle_writes(read) as seen:
            build()
        out[name] = seen
    return out


async def cycled(name: str):
    """The corpus fixture, the recompute run as a cycle runs it."""
    case = await CORPUS[name]()
    if case.state == CaseState.INVESTIGATING:
        _recompute_cause_state_from_chain(case)
    return case


def _stored(case) -> bool:
    return case.progress.cause_identification_held_open


def capture_all() -> dict:
    """The stored flag at every compared point. Run outside an event loop:
    the golden corpus's whole turns run their own."""

    async def corpus() -> dict:
        return {name: _stored(await cycled(name)) for name in sorted(CORPUS)}

    return {"golden": golden_reads(_stored), "corpus": asyncio.run(corpus())}
