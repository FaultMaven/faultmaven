"""The ``<evidence_needs>`` block's fixture corpus (#2811 S2b-1b-ii).

The fix-verification corpus (``fix_verification_cases_2811.CASES``, 56
fixtures), plus one per block line none of them renders: a DIAGNOSIS pool
with each purpose, priority and ask history; the STOPPED section alone and
past its cap; the causal surface cap rotating, with an UNOBTAINABLE need left
out of the hidden count; the render cap with reserved causal slots; the
checklist's fallback with no verifying row, and with verifying ids that name
no row; a row's text falling back to its extract, then to "(no summary)", and
one past the cap; the checklist past its cap; and the two attempts only the
record holds dated (carried, gone), for the checklist's heading. The corpus
already shows both headings, MITIGATION with nothing dated and a terminal
case. Every added fixture is hand-built: none makes a claim that a turn
reaches it.

``render_all`` renders each through ``_build_evidence_needs_block``. Its
output on ``main`` is ``evidence_needs_oracle_2811.json``, captured at
9ed524b07 by running ``render_all`` with this file copied into a
``git archive`` extract of that commit, never typed by hand.
"""

from __future__ import annotations

from uuid import uuid4

from faultmaven.core.investigation.outcome import record_case_outcome
from faultmaven.modules.case.contracts import (
    EvidenceCategory,
    InvestigationActionType,
    InvestigationStage,
    NeedObtainability,
    NeedPriority,
    NeedPurpose,
    NeedState,
    WritePoint,
)
from tests.unit.core.investigation import test_narrator_record_parity_2811 as parity
from tests.unit.core.investigation.prompts import fix_verification_cases_2811 as fv
from tests.unit.core.investigation.test_evidence_needs_context_block import (
    _make_case,
    _make_evidence,
    _make_need,
)

_CAUSAL = NeedPurpose.CAUSAL_VERIFICATION
_SYMPTOM_ROW = EvidenceCategory.SYMPTOM_EVIDENCE


async def _diagnosis():
    """Each purpose and priority, ``motivated_by: []`` and with ids,
    PARTIALLY_MET, never asked, asked once and asked twice; a FULFILLED and a
    SUPERSEDED need left out."""
    case = _make_case()
    hypotheses = [f"hyp_{uuid4().hex[:12]}" for _ in range(2)]
    _make_need(case, priority=NeedPriority.LOW, request_text="kubectl get pods")
    once = _make_need(case, purpose=_CAUSAL, request_text="pgbouncer SHOW POOLS")
    once.record_surfaced(3)
    twice = _make_need(
        case,
        purpose=_CAUSAL,
        priority=NeedPriority.HIGH,
        request_text="DB connection pool metrics",
        motivating_hypothesis_ids=hypotheses,
    )
    twice.record_surfaced(4)
    twice.record_surfaced(5)
    _make_need(
        case,
        state=NeedState.PARTIALLY_MET,
        priority=NeedPriority.HIGH,
        request_text="API p99 latency during the incident",
    )
    _make_need(case, state=NeedState.FULFILLED, request_text="the error rate")
    _make_need(case, state=NeedState.SUPERSEDED, request_text="the old ask")
    return case


def _exhaust(need) -> None:
    """Asked at 2 and 3, read at 5: the engine stopped putting it."""
    need.record_surfaced(2)
    need.record_surfaced(3)


async def _exhausted_only():
    case = _make_case()
    _exhaust(_make_need(case, purpose=_CAUSAL, request_text="app-side STS debug"))
    return case


async def _exhausted_overflow():
    """A live need, then the STOPPED section past its cap."""
    case = _make_case()
    _make_need(case, request_text="the scrape target's state")
    for i in range(17):
        _exhaust(_make_need(case, request_text=f"node exporter dump {i}"))
    return case


async def _surface_rotation():
    """Seven gettable causal needs, two sharing a text, rotated one page by a
    turn without progress; an UNOBTAINABLE one is neither shown nor counted."""
    case = _make_case()
    for i in range(7):
        text = "pgbouncer SHOW POOLS" if i < 2 else f"causal datum {i}"
        _make_need(case, purpose=_CAUSAL, request_text=text)
    walled = _make_need(case, purpose=_CAUSAL, request_text="the vendor's logs")
    walled.obtainability = NeedObtainability.UNOBTAINABLE
    case.turns_without_progress = 1
    return case


async def _render_cap():
    """Sixteen HIGH symptom needs, a LOW one and five causal: the surface
    cap's three keep their reserved slots behind the wall."""
    case = _make_case()
    for i in range(16):
        _make_need(case, priority=NeedPriority.HIGH, request_text=f"symptom {i}")
    _make_need(case, priority=NeedPriority.LOW, request_text="the low ask")
    for i in range(5):
        _make_need(case, purpose=_CAUSAL, request_text=f"causal datum {i}")
    return case


def _row(case, turn: int, text: str, **fields):
    case.current_turn = turn
    row = _make_evidence(case, category=_SYMPTOM_ROW, summary=text)
    for name, value in fields.items():
        setattr(row, name, value)
    return row


async def _fallback():
    """No verifying row: every symptom row, stable by turn, an engine-authored
    one included, and a cited cause row after them."""
    case = _make_case(stage=InvestigationStage.MITIGATION)
    _row(case, 5, "p99 at 8.9s")
    _row(case, 3, "502s from the edge")
    _row(case, 3, "the engine's marker", collected_by="engine")
    _row(case, 4, "queue depth 40k")
    _make_evidence(case, category=EvidenceCategory.CAUSAL_EVIDENCE, summary="no index")
    case.current_turn = 6
    return case


async def _verifying_names_no_row():
    """The verifying ids name no row: no fallback, as on ``main``."""
    from faultmaven.modules.case.contracts import ProblemVerification

    case = await _fallback()
    case.problem_verification = ProblemVerification(
        symptom_statement="Test problem",
        verifying_evidence_ids=["ev_00000000dead"],
    )
    return case


async def _row_text():
    """A summary emptied (hand-built: the model refuses an empty one) falls
    back to the extract, then to "(no summary)"; one runs past the cap."""
    case = _make_case(stage=InvestigationStage.TREATMENT)
    _row(case, 2, "x", summary="")
    _row(case, 3, "x", summary="", extract=None)
    _row(case, 4, "the billing-exporter scrape target " * 5)
    return case


async def _checklist_overflow():
    case = _make_case(stage=InvestigationStage.MITIGATION)
    for i in range(17):
        _row(case, i + 1, f"edge 502 burst {i}")
    return case


async def _only_carried():
    """The one fix minted final, then its execution turn cleared: only the
    record holds it dated."""
    from tests.unit.core.investigation.prompts.test_fix_verification_1906 import (
        _case,
    )

    case = _case("treatment")
    record_case_outcome(case, at=WritePoint.CYCLE)
    case.proposed_actions[0].accepted_in_turn = None
    return case


async def _only_gone():
    """A FAILED window minted final, then every accepted SOLUTION dropped:
    only the record holds an attempt dated."""
    case = await parity._held_check_then_interception()
    case.proposed_actions = [
        a
        for a in case.proposed_actions
        if a.state != "accepted" or a.action_type != InvestigationActionType.SOLUTION
    ]
    return case


#: The fixtures this corpus adds, one per block line no fixture above shows.
ADDED = {
    "E:diagnosis": _diagnosis,
    "E:exhausted_only": _exhausted_only,
    "E:exhausted_overflow": _exhausted_overflow,
    "E:surface_rotation": _surface_rotation,
    "E:render_cap": _render_cap,
    "E:fallback": _fallback,
    "E:verifying_names_no_row": _verifying_names_no_row,
    "E:row_text": _row_text,
    "E:checklist_overflow": _checklist_overflow,
    "E:only_carried": _only_carried,
    "E:only_gone": _only_gone,
}

#: Every fixture, by name: the fix-verification corpus, then the added ones.
CASES = {**fv.CASES, **ADDED}


async def build(name: str):
    """The fixture ``name``, built with deterministic ids."""
    with fv._deterministic_ids(name):
        return await CASES[name]()


async def render_all() -> dict[str, str]:
    """Each fixture's ``<evidence_needs>`` block, by name."""
    from faultmaven.core.investigation.prompts.context_builder.evidence_needs import (
        _build_evidence_needs_block,
    )

    return {name: _build_evidence_needs_block(await build(name)) for name in CASES}
