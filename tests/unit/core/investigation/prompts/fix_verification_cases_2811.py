"""The ``<fix_verification>`` block's fixture corpus (#2811 S2b-1a).

The 26 narration fixtures of ``test_narrator_record_parity_2811`` (its 12
``FIXTURES`` and 14 ``REASONS``), plus one per block line none of them
renders: the mitigation's lines (each state, the unregistered relief with and
without the gate's refusal, no rows on record, beside a dated fix), "Fix
attempts: none recorded", "…and N earlier", an interception seen gone, the
cited basis withheld after an off-cause attempt, a repeated pending why, each
re-check reason's text no parity fixture renders in the block, an id list and
a description past their bounds, and a problem-gone row outside the latest
window. ``NEEDS`` adds one per need line or mark (S2b-1b-i). ``DECLARED``
adds one fixture per declared difference.

``render_all`` renders each through ``build_fix_verification_block``. Its
output on ``main`` is ``fix_verification_oracle_2811.json``, captured by
running ``render_all`` with this file copied into that tree, never typed by
hand: at 9b4d0b1e1, and at 92e98cdbf for ``NEEDS`` and the parity corpus's
``held_check_on_a_false_alarm``. Ids are made deterministic while a fixture
is built (``_deterministic_ids``), so the capture is compared byte for byte.
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


async def _treatment(build):
    """A parity fixture in DIAGNOSIS, moved to TREATMENT."""
    case = await build()
    case.progress.solution_accepted = True
    return case


async def _other_cause_and_a_disconfirmation():
    """A re-check of the other root (OTHER_CAUSE), and the fixed root's
    re-check REFUTES-linked with an engine marker (DISCONFIRMATION)."""
    from faultmaven.modules.case.contracts import (
        CausalNode,
        EvidenceStance,
        NodeEvidenceLink,
        NodeType,
    )
    from tests.unit.core.investigation.test_declined_resolution_binds_the_model_1895 import (
        _engine_disconfirmation,
    )
    from tests.unit.core.investigation.test_m6_cause_gone_problem_persists_1927 import (
        _eid,
    )

    case = await parity._other_cause()
    marker = _engine_disconfirmation(7)
    case.evidence.append(marker)
    node = CausalNode(
        statement="the restart cleared the pool",
        node_type=NodeType.INTERMEDIATE,
        generated_at_turn=7,
        evidence_links=[
            NodeEvidenceLink(
                evidence_id=i, stance=EvidenceStance.REFUTES, reasoning="x"
            )
            for i in (marker.evidence_id, _eid("poolgone"))
        ],
    )
    case.causal_nodes[node.node_id] = node
    return case


async def _outside_the_window():
    """A cause re-check from before the fix, whose window the problem's
    persistence observed."""
    from tests.unit.core.investigation.prompts.test_fix_verification_1906 import (
        _case,
    )

    case = _case("treatment", cause=False, persists=True)
    bound_row(case, EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE, turn=2)
    return case


async def _past_the_bounds():
    """Seven cause re-checks (one past ``MAX_IDS``) and a description past
    80 characters, its whitespace runs collapsed."""
    from tests.unit.core.investigation.prompts.test_fix_verification_1906 import (
        _case,
    )

    case = _case("treatment")
    for _ in range(6):
        bound_row(case, EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE, turn=4)
    case.proposed_actions[0].description = (
        "Point   ExecStart at /usr/bin/billing-exporter,\n reload systemd, restart "
        "the unit and confirm the scrape target reports UP again"
    )
    return case


async def _gone_outside_the_window():
    """The problem re-checked gone before the fix too: only the latest
    window's rows are named."""
    case = await parity._uncited(True)
    shown = case.problem_verification.verifying_evidence_ids[0]
    bound_row(case, _GONE, turn=2, rechecks=shown)
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
    "T:predates_failed_fix": lambda: _treatment(parity._before_the_failed_fix),
    "T:uncited_no_root": lambda: _treatment(parity._uncited_with_no_root),
    "T:other_cause_disconfirmation": _other_cause_and_a_disconfirmation,
    "T:outside_window": _outside_the_window,
    "T:past_the_bounds": _past_the_bounds,
    "T:gone_outside_window": _gone_outside_the_window,
}


async def _needs(held: str = "", *, declined=False, extra=0, long=False):
    """The fix's case with its one symptom-verification need (S2b-1b-i).
    ``held``: "pending" or "fulfilled" (both legs standing, asked at 5, the
    second citing only a row from before the ask), "exhausted" (asked at 7
    and 8, the block at 9, behind a live need asked before the fix) or
    "stopped" (exhausted, nothing held); ``extra`` more live needs."""
    from faultmaven.modules.case.contracts import EvidenceNeed, NeedPurpose, NeedState
    from tests.case_builders import with_declined_offer
    from tests.unit.core.investigation.prompts.test_fix_verification_1906 import (
        _case,
    )

    case = _case("treatment", problem=held in ("pending", "fulfilled", "exhausted"))
    need = case.evidence_needs[0]
    if held in ("pending", "fulfilled"):
        need.created_at_turn = 5
    if held == "fulfilled":
        need.state = NeedState.FULFILLED
        need.fulfilling_evidence_ids = list(
            case.problem_verification.verifying_evidence_ids
        )
    if held in ("exhausted", "stopped"):
        case.current_turn = 9
        need.created_at_turn = 7
        need.surfaced_turns = [7, 8]
    if held == "exhausted":
        extra = 1
    for i in range(extra):
        case.evidence_needs.insert(
            i,
            EvidenceNeed(
                case_id=case.case_id,
                purpose=NeedPurpose.SYMPTOM_VERIFICATION,
                request_text=f"the scrape target's state from probe {i}",
                rationale="the reported symptom, re-checked",
                created_at_turn=2,
            ),
        )
    if long:
        need.request_text = "the billing-exporter scrape target " * 6
    if declined:
        with_declined_offer(case, kind="resolve", basis="backstop")
    return case


#: The fixtures S2b-1b-i adds, one per need line or mark no fixture above
#: shows; captured on ``main`` at 92e98cdbf.
NEEDS = {
    "N:held_pending": lambda: _needs("pending"),
    "N:held_pending_declined": lambda: _needs("pending", declined=True),
    "N:held_fulfilled": lambda: _needs("fulfilled"),
    "N:held_fulfilled_declined": lambda: _needs("fulfilled", declined=True),
    "N:held_exhausted": lambda: _needs("exhausted"),
    "N:more_than_five": lambda: _needs(extra=6),
    "N:none_stopped": lambda: _needs("stopped"),
    "N:long_request": lambda: _needs(long=True),
    "N:invalidated": lambda: _invalidated(),
}


async def _invalidated():
    """The finding of a false alarm applied with ``invalidate_problem``
    directly, past ``invalidation_refusal``, which refuses it on this case
    while its cause leg stands; a need FULFILLED on rows from before its ask
    holds the resolution. The finding supersedes the open symptom need, and
    the held one stays listed and marked. Readiness answers FALSE_ALARM
    before it reads the held need."""
    from faultmaven.core.investigation.problem_status import invalidate_problem

    case = await _needs("fulfilled", extra=1)
    invalidate_problem(
        case,
        evidence_ids=list(case.problem_verification.verifying_evidence_ids),
        basis="the scrape target was never down",
    )
    return case


async def _closure_disagrees():
    """A closed case whose stored reason moved since its terminal write."""
    from faultmaven.modules.case.contracts import ClosureReason

    case = await parity._terminal_qa()
    case.closure_reason = ClosureReason.CLOSED_FALSE_ALARM
    return case


async def _carried_turn():
    """The fix's turn minted final, then its execution turn moved by one."""
    case = await parity._2595()
    record_case_outcome(case, at=WritePoint.CYCLE)
    for action in case.proposed_actions:
        if action.accepted_in_turn is not None:
            action.accepted_in_turn += 1
    return case


async def _gone_action():
    """A FAILED window minted final, then its action dropped from the case."""
    case = await parity._held_check_then_interception()
    case.proposed_actions = [
        a for a in case.proposed_actions if a.accepted_in_turn != 6
    ]
    return case


async def _unreadable_record():
    """A stored record of a shape no step reaches: the fold raises."""
    case = await parity._other_cause()
    record_case_outcome(case, at=WritePoint.CYCLE)
    case.progress.outcome = case.progress.outcome.model_copy(update={"shape": 0})
    return case


#: One fixture per declared difference, by the difference it shows.
DECLARED = {
    "closure": ("D:closure", _closure_disagrees),
    "accepted_in_turn": ("D:carried_turn", _carried_turn),
    "gone_action": ("D:gone_action", _gone_action),
    "omitted": ("D:omitted", _unreadable_record),
}

#: Every fixture, by name: the parity corpus's 26, the added ones, then the
#: declared differences'.
CASES = {
    **{name: build for name, (build, _declared) in parity.FIXTURES.items()},
    **{f"R:{name}": build for name, (build, _shows) in parity.REASONS.items()},
    **ADDED,
    **NEEDS,
    **dict(DECLARED.values()),
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
