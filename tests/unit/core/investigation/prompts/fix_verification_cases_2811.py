"""The ``<fix_verification>`` block's fixture corpus (#2811 S2b-1a).

The 25 narration fixtures of ``test_narrator_record_parity_2811`` (its 11
``FIXTURES`` and 14 ``REASONS``), plus one per block line none of them
renders: the mitigation's lines (each state, the unregistered relief with and
without the gate's refusal, no rows on record, beside a dated fix), "Fix
attempts: none recorded", "…and N earlier", an interception seen gone, the
cited basis withheld after an off-cause attempt, and a repeated pending why.

``render_all`` renders each through ``build_fix_verification_block``. Its
output on ``main`` at 9b4d0b1e1 is ``fix_verification_oracle_2811.json``,
captured there by running ``render_all`` with this file copied into that
tree, never typed by hand. Ids are made deterministic while a fixture is
built (``_deterministic_ids``), so the capture is compared byte for byte.
"""

from __future__ import annotations

import os
import random
import uuid
from contextlib import contextmanager
from unittest.mock import patch

from faultmaven.core.investigation.outcome import record_case_outcome
from faultmaven.modules.case.contracts import (
    EvidenceCategory,
    ProblemStatus,
    WritePoint,
)
from tests.unit.core.investigation import bound_rows
from tests.unit.core.investigation import test_narrator_record_parity_2811 as parity
from tests.unit.core.investigation.bound_rows import bound_row

_GONE = EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE
_PRESENT = EvidenceCategory.SYMPTOM_EVIDENCE


async def _mitigation(shape: str = ""):
    """A workaround accepted at 6 (MITIGATION), as ``shape`` re-checks it."""
    from tests.unit.core.investigation.golden_cases_2636 import _accepted_mitigation
    from tests.unit.core.investigation.test_case_outcome_fold_2636 import _fix

    case, shown = _accepted_mitigation()
    if shape == "no_rows":
        case.problem_verification.verifying_evidence_ids = []
    if shape in ("relieved", "refused", "seen_again", "with_fix"):
        bound_row(case, _GONE, turn=7, rechecks=shown)
        case.current_turn = 7
    if shape == "refused":
        case.progress.problem_status = ProblemStatus.REVISION_PENDING
    if shape == "seen_again":
        bound_row(case, _PRESENT, turn=8, rechecks=shown)
        case.current_turn = 8
    if shape == "with_fix":
        _fix(case, turn=8, target_ids=[], fix_kind="remediation")
        case.current_turn = 8
    return case


async def _no_dated_fix():
    """The fix accepted before its turn was recorded: no attempt is dated."""
    from tests.unit.core.investigation.prompts.test_fix_verification_1906 import (
        _case,
    )

    case = _case("treatment")
    case.proposed_actions[0].accepted_in_turn = None
    return case


async def _four_fixes():
    from tests.unit.core.investigation.prompts.test_fix_verification_1906 import (
        _case,
        _fix,
    )

    case = _case("treatment", cause=False)
    for turn in (4, 5, 6):
        _fix(case, turn)
    return case


async def _interception_seen_gone():
    """The fix at 6, then an interception at 8 the problem is re-checked gone
    after: the latest attempt acts on no identified cause."""
    from tests.unit.core.investigation.test_case_outcome_fold_2636 import _fix
    from tests.unit.core.investigation.test_pending_check_holds_the_offer_1906 import (
        _held_case,
    )

    case = _held_case()
    shown = case.problem_verification.verifying_evidence_ids[0]
    _fix(case, turn=8, fix_kind="interception", target_ids=[])
    bound_row(case, _GONE, turn=9, rechecks=shown)
    case.current_turn = 9
    record_case_outcome(case, at=WritePoint.CYCLE)
    return case


async def _interception_only():
    """The one attempt an interception: no fix of the cause can confirm it
    (``off_cause_attempt``), so the cited basis is not asked for."""
    from tests.unit.core.investigation.prompts.test_fix_verification_1906 import (
        _case,
    )

    case = _case("treatment", cause=False)
    case.proposed_actions[0].fix_kind = "interception"
    return case


async def _repeated_why():
    """Two cause re-checks pending for the same reason: the second keeps the
    clause before ";"."""
    case = await parity._held_check_then_interception()
    bound_row(case, EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE, turn=10)
    return case


#: The fixtures this corpus adds, one per block line no parity fixture shows.
ADDED = {
    "M:not_rechecked": lambda: _mitigation(),
    "M:relieved": lambda: _mitigation("relieved"),
    "M:refused": lambda: _mitigation("refused"),
    "M:seen_again": lambda: _mitigation("seen_again"),
    "M:no_rows": lambda: _mitigation("no_rows"),
    "M:with_fix": lambda: _mitigation("with_fix"),
    "T:no_dated_fix": _no_dated_fix,
    "T:four_fixes": _four_fixes,
    "T:interception_seen_gone": _interception_seen_gone,
    "T:interception_only": _interception_only,
    "T:repeated_why": _repeated_why,
}

#: Every fixture, by name: the parity corpus's 25, then the added ones.
CASES = {
    **{name: build for name, (build, _declared) in parity.FIXTURES.items()},
    **{f"R:{name}": build for name, (build, _shows) in parity.REASONS.items()},
    **ADDED,
}


class _Urandom:
    """``os`` as ``uuid`` sees it, with ``urandom`` drawn from a seeded
    generator, so ``uuid4`` is deterministic while a fixture is built."""

    def __init__(self, seed: str) -> None:
        self._random = random.Random(seed)

    def urandom(self, n: int) -> bytes:
        return self._random.randbytes(n)

    def __getattr__(self, name: str):
        return getattr(os, name)


@contextmanager
def _deterministic_ids(seed: str):
    """``uuid4`` seeded by the fixture's name, and ``bound_row``'s labels
    free of the case object's address (``id(case)``)."""
    with (
        patch.object(uuid, "os", _Urandom(seed)),
        patch.object(bound_rows, "id", lambda _case: 0, create=True),
    ):
        yield


async def build(name: str):
    """The fixture ``name``, built with deterministic ids."""
    with _deterministic_ids(name):
        return await CASES[name]()


async def render_all() -> dict[str, str]:
    """Each fixture's ``<fix_verification>`` block, by name."""
    from faultmaven.core.investigation.prompts.context_builder.fix_verification_block import (
        build_fix_verification_block,
    )

    return {name: build_fix_verification_block(await build(name)) for name in CASES}
