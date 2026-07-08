"""Render-time surface cap for causal evidence-needs (#604 anti-deadlock).

``select_surfaced_causal_needs`` is a pure VIEW over the PENDING pool: it caps
what is SHOWN (never what exists), ranks rarity-first, pages the window under
non-progress so the whole pool is swept in ``ceil(pool / CAP)`` turns, and
snaps back to page 0 on progress. These tests pin that liveness contract —
an unanswerable ask must never permanently hide an answerable discriminator.
"""

from __future__ import annotations

import pytest

from faultmaven.core.investigation.evidence_need_surfacing import (
    _SURFACED_CAUSAL_CAP,
    select_surfaced_causal_needs,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    EvidenceNeed,
    InquiryData,
    NeedObtainability,
    NeedPurpose,
    NeedState,
    ProblemVerification,
)
from tests.utils import generate_case_id, generate_evidence_id

pytestmark = pytest.mark.unit


def _case() -> Case:
    case = Case(
        case_id=generate_case_id(),
        user_id="u",
        organization_id="o",
        title="t",
        description="d",
        state=CaseState.INVESTIGATING,
        inquiry=InquiryData(
            proposed_problem_statement="x",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
        problem_verification=ProblemVerification(
            symptom_statement="x", severity=CaseSeverity.HIGH
        ),
    )
    case.current_turn = 5
    return case


def _need(
    case: Case,
    request_text: str,
    *,
    purpose: NeedPurpose = NeedPurpose.CAUSAL_VERIFICATION,
    state: NeedState = NeedState.PENDING,
    **kw,
) -> EvidenceNeed:
    need = EvidenceNeed(
        case_id=case.case_id,
        purpose=purpose,
        request_text=request_text,
        rationale="discriminates a candidate cause",
        state=state,
        created_at_turn=1,
        **kw,
    )
    case.evidence_needs.append(need)
    return need


def _seed_causal(case: Case, texts: list[str]) -> list[EvidenceNeed]:
    return [_need(case, t) for t in texts]


def _pending(case: Case) -> list[EvidenceNeed]:
    return [n for n in case.evidence_needs if n.state == NeedState.PENDING]


def test_surfaced_returns_all_within_cap():
    case = _case()
    _seed_causal(case, ["only1", "only2"])
    assert len(select_surfaced_causal_needs(case)) == 2


def test_surfaced_caps_the_shown_set():
    case = _case()
    _seed_causal(case, [f"tok{i}" for i in range(6)])
    assert len(select_surfaced_causal_needs(case)) == _SURFACED_CAUSAL_CAP
    assert len(_pending(case)) == 6  # all still PENDING — cap is a view, not existence


def test_surfaced_excludes_unobtainable_but_keeps_it_pending():
    # A need declared UNOBTAINABLE yields its surface slot (no futile re-ask) but
    # stays PENDING in the pool, so the verification-status rollup still counts it
    # toward the declared wall and the close record can name it.
    case = _case()
    needs = _seed_causal(case, ["tokA", "tokB"])
    needs[0].obtainability = NeedObtainability.UNOBTAINABLE
    surfaced_ids = {n.need_id for n in select_surfaced_causal_needs(case)}
    assert needs[0].need_id not in surfaced_ids  # yielded its slot
    assert needs[1].need_id in surfaced_ids  # the gettable one still shown
    assert needs[0].state == NeedState.PENDING  # retained in the pool


def test_surfaced_prefers_discriminating():
    # 'shared telemetry' is asked for by 3 needs (one shared request_text → least
    # discriminating); each unique ask (1 need → most discriminating) is surfaced
    # first. Also keeps the shown asks distinct (no near-duplicate spam).
    case = _case()
    for _ in range(3):
        _need(case, "shared telemetry")
    _seed_causal(case, ["uniqueA", "uniqueB", "uniqueC"])
    surfaced = select_surfaced_causal_needs(case)
    assert len(surfaced) == _SURFACED_CAUSAL_CAP
    assert all("unique" in n.request_text for n in surfaced)
    assert not any("shared" in n.request_text for n in surfaced)


def test_surfaced_rotates_under_non_progress():
    # Paged anti-stall guarantee: under sustained non-progress the window sweeps the
    # WHOLE pool in ceil(pool / CAP) non-progress turns (one full page per turn). This
    # is the REACHABLE bound that matters — the engine can declare exhaustion within a
    # few non-progress turns, so coverage must be fast, not merely eventual. Pure
    # render-time paging: nothing is SUPERSEDED (a one-way door that would kill
    # liveness).
    case = _case()
    _seed_causal(case, [f"tok{i}" for i in range(9)])  # 3 full pages of CAP=3
    all_ids = {n.need_id for n in _pending(case)}
    pool_size = len(all_ids)
    pages_to_cover = (pool_size + _SURFACED_CAUSAL_CAP - 1) // _SURFACED_CAUSAL_CAP
    ever_surfaced: set[str] = set()
    for stuck in range(pages_to_cover):
        case.turns_without_progress = stuck
        window = select_surfaced_causal_needs(case)
        # The window must stay FULL on every turn — a union-only assertion would pass
        # a wrap regression that empties at some offset, so assert per-turn size too.
        assert len(window) == _SURFACED_CAUSAL_CAP
        ever_surfaced |= {n.need_id for n in window}
    assert ever_surfaced == all_ids  # full coverage within ceil(pool / CAP) turns
    assert all(
        n.state == NeedState.PENDING for n in case.evidence_needs
    )  # none superseded


def test_surfaced_resets_to_most_discriminating_on_progress():
    # On progress the non-progress counter resets, so the window returns to page 0 —
    # the most-discriminating asks. The other half of the liveness contract: the
    # window advances one page per non-progress turn, and progress snaps it back to
    # page 0.
    case = _case()
    for _ in range(3):
        _need(case, "shared telemetry")
    _seed_causal(case, ["uniqueA", "uniqueB", "uniqueC"])
    case.turns_without_progress = 0
    fresh = {n.need_id for n in select_surfaced_causal_needs(case)}
    case.turns_without_progress = 1  # one non-progress turn: advanced to the next page
    rotated = {n.need_id for n in select_surfaced_causal_needs(case)}
    assert rotated != fresh
    case.turns_without_progress = 0  # progress made: back to page 0
    assert {n.need_id for n in select_surfaced_causal_needs(case)} == fresh


def test_surfaced_caps_a_mixed_pool_regardless_of_author():
    # The cap covers ALL outstanding causal needs regardless of who authored them —
    # engine-owned and LLM-emitted needs are indistinguishable at render.
    case = _case()
    _seed_causal(case, [f"eng{i}" for i in range(4)])
    _seed_causal(case, [f"llm-authored causal ask {i}" for i in range(4)])
    assert len(_pending(case)) == 8
    assert len(select_surfaced_causal_needs(case)) == _SURFACED_CAUSAL_CAP


def test_surfaced_excludes_symptom_and_non_outstanding():
    case = _case()
    _seed_causal(case, ["c1", "c2"])
    _need(case, "symptom detail", purpose=NeedPurpose.SYMPTOM_VERIFICATION)
    _need(
        case,
        "already collected",
        state=NeedState.FULFILLED,
        fulfilling_evidence_ids=[generate_evidence_id()],
    )
    surfaced = select_surfaced_causal_needs(case)
    assert len(surfaced) == 2  # only the 2 outstanding causal needs
    assert all(n.purpose == NeedPurpose.CAUSAL_VERIFICATION for n in surfaced)
    assert all(n.state == NeedState.PENDING for n in surfaced)
