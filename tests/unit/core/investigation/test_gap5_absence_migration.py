"""GAP-5 — legacy→absence evidence-category migration regression pins.

Tier-1 deterministic guards (merge gate) for the migration that removed the
legacy ``mitigation_evidence`` / ``solution_evidence`` categories in favour of
the presence/absence verification quartet. See
docs/working/context-assembly-gaps/GAP-5-implementation-plan.md.

These assert the *clean end-state*: the legacy categories are gone from the
enum, no prompt emits them, the milestone map is correctly neutralized, and the
schema rejects them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from faultmaven.core.investigation.milestone_engine import CATEGORY_MILESTONE_MAP
from faultmaven.modules.case.domain.models import EvidenceCategory

_LEGACY = ("mitigation_evidence", "solution_evidence")
_QUARTET = {
    "symptom_evidence",
    "causal_evidence",
    "symptom_absence_evidence",
    "causal_absence_evidence",
}

_TEMPLATES = (
    Path(__file__).resolve().parents[4]
    / "faultmaven/core/investigation/prompts/templates.py"
)


# --- enum is exactly the quartet -------------------------------------------
def test_enum_is_the_quartet_no_legacy():
    assert {c.value for c in EvidenceCategory} == _QUARTET
    for name in ("MITIGATION_EVIDENCE", "SOLUTION_EVIDENCE"):
        assert not hasattr(EvidenceCategory, name)


@pytest.mark.parametrize("legacy", _LEGACY)
def test_legacy_category_value_is_rejected(legacy):
    with pytest.raises(ValueError):
        EvidenceCategory(legacy)


# --- no prompt emits a legacy category (source grep) -----------------------
def test_prompts_emit_no_legacy_category():
    src = _TEMPLATES.read_text(encoding="utf-8")
    offenders = [tok for tok in _LEGACY if tok in src]
    assert not offenders, (
        f"templates.py still references legacy categories {offenders}; "
        "prompts must emit only the absence quartet"
    )


# --- milestone map: legacy gone, absence neutralized to [] -----------------
def test_milestone_map_has_no_legacy_keys():
    keys = {k.value for k in CATEGORY_MILESTONE_MAP}
    assert not (keys & set(_LEGACY))


def test_absence_categories_map_to_empty():
    # Gate milestones are handshake-set; absence rows are consumed by the
    # readiness checks, NOT auto-fired via the map. So absence → [].
    assert CATEGORY_MILESTONE_MAP[EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE] == []
    assert CATEGORY_MILESTONE_MAP[EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE] == []


def test_presence_categories_still_attribute():
    # The presence categories remain attributed (these milestones ARE
    # evidence-validated, unlike the handshake gates).
    assert (
        "symptom_verified" in CATEGORY_MILESTONE_MAP[EvidenceCategory.SYMPTOM_EVIDENCE]
    )
    # Causal evidence attributes to solution_proposed. root_cause_identified is
    # NOT here (#675 / INV-35): identification is engine-derived from cause_state,
    # never an LLM-claimed milestone, so it could never be attributed via this
    # map (its only consumer intersects with MilestoneUpdates, which no longer
    # carries the removed boolean).
    assert (
        "solution_proposed" in CATEGORY_MILESTONE_MAP[EvidenceCategory.CAUSAL_EVIDENCE]
    )
    assert (
        "root_cause_identified"
        not in CATEGORY_MILESTONE_MAP[EvidenceCategory.CAUSAL_EVIDENCE]
    )


# ===========================================================================
# Golden dispositions — the absence-driven flow reaches the CORRECT outcomes.
# Deterministic (no LLM): the migration's payoff is that emitting the right
# absence category drives the right disposition via the readiness checks.
# ===========================================================================
from datetime import datetime, timezone  # noqa: E402

from faultmaven.core.investigation.terminal_transitions import (  # noqa: E402
    ClosureReadiness,
    ResolutionReadiness,
    assess_closure_readiness,
    assess_resolution_readiness,
)
from faultmaven.modules.case.domain.models import (  # noqa: E402
    Case,
    Evidence,
    EvidenceSourceType,
)


def _ev(category: EvidenceCategory, idx: int = 1) -> Evidence:
    return Evidence(
        evidence_id=f"ev_{idx:012d}",
        summary=f"evidence {idx}",
        category=category,
        source_type=EvidenceSourceType.USER_DESCRIPTION,
        source_file_id=None,  # user_description is the legal NULL-source case
        collected_at=datetime.now(timezone.utc),
        collected_by="u1",
        collected_at_turn=1,
        primary_purpose="test",
    )


def _case(*evidence: Evidence) -> Case:
    return Case(
        user_id="u1",
        organization_id="o1",
        title="t",
        description="Pods are crashing",
        evidence=list(evidence),
    )


def test_causal_absence_makes_case_resolution_ready():
    """The migration's payoff: emitting causal_absence_evidence is the bar that
    makes a case RESOLVED — no separate solution record required."""
    case = _case(_ev(EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE))
    assert assess_resolution_readiness(case).verdict == ResolutionReadiness.READY


def test_symptom_absence_only_does_not_resolve():
    """A mitigation (symptom relieved, cause persists) yields symptom_absence
    but NOT causal_absence — it must NOT auto-resolve; it converges to close."""
    case = _case(
        _ev(EvidenceCategory.SYMPTOM_EVIDENCE, 1),
        _ev(EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE, 2),
    )
    verdict = assess_resolution_readiness(case).verdict
    assert verdict != ResolutionReadiness.READY
    # Has investigation substance but no causal_absence → asks for the missing
    # confirmation (NEEDS_INFO), not READY.
    assert verdict == ResolutionReadiness.NEEDS_INFO


def test_causal_absence_pivots_close_to_resolve():
    """Closing a causal-absence (resolution-grade) case pivots to RESOLVE."""
    case = _case(_ev(EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE))
    assert assess_closure_readiness(case).verdict == ClosureReadiness.SUGGEST_RESOLVE


def test_symptom_absence_only_closes_without_pivot():
    """A stabilized case (symptom_absence, no causal_absence) closes cleanly —
    no pivot to resolve."""
    case = _case(
        _ev(EvidenceCategory.SYMPTOM_EVIDENCE, 1),
        _ev(EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE, 2),
    )
    assert assess_closure_readiness(case).verdict != ClosureReadiness.SUGGEST_RESOLVE


def test_nothing_investigated_suggests_close():
    case = _case()  # no evidence, no cause, no solution
    assert (
        assess_resolution_readiness(case).verdict == ResolutionReadiness.SUGGEST_CLOSE
    )
