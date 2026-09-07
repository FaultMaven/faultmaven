"""DF-6 / INV-39 — per-provider work-gate crossing metric.

``work_gate_passed`` is the documented observability primitive for the §5.2
provider floor; P3.2 turns it into an actual metric surface. These tests pin the
once-per-case latch (``InvestigationProgress.work_gate_crossed``) and the
per-provider counter (``work_gate_crossed_total``) it drives — the behavior the
metric depends on, independent of any Prometheus scraping.

The Prometheus counter is a NoOp shim unless ``ENABLE_METRICS`` is set (it has no
readable value in the unit env), and its live increments are covered by the sim
gate. Here the counter is patched with a ``Mock`` so the ``inc()`` call count and
the ``provider`` label are asserted deterministically, and the durable behavior
the metric rides on — the once-per-case ``work_gate_crossed`` latch — is asserted
directly on the case.
"""

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from faultmaven.core.investigation.milestone_engine import (
    _recompute_assessment_state,
    _resolve_chat_provider_name,
)
from faultmaven.modules.case.domain.models import (
    Case,
    CaseSeverity,
    CaseState,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisState,
    InquiryData,
    ProblemVerification,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def counter():
    """Patch the module-global counter with a Mock so ``labels(...).inc()`` calls
    are captured deterministically (the real counter is a NoOp in the unit env)."""
    with patch(
        "faultmaven.core.investigation.milestone_engine.work_gate_crossed_total",
        new=MagicMock(),
    ) as m:
        yield m


def _inc_calls(counter: MagicMock, provider: str) -> int:
    """How many times a crossing was counted for ``provider``. The engine calls
    ``labels(provider=…).inc()`` exactly once per increment, so a per-provider
    ``labels`` call count is the increment count."""
    return sum(
        1
        for call in counter.labels.call_args_list
        if call.kwargs.get("provider") == provider
    )


def _ev(i: int) -> Evidence:
    from datetime import datetime, timezone

    return Evidence(
        evidence_id=f"ev_{i:012x}",
        summary="an observed fact",
        primary_purpose="diagnosis",
        category=EvidenceCategory.CAUSAL_EVIDENCE,
        source_type=EvidenceSourceType.USER_DESCRIPTION,
        collected_by="llm",
        collected_at_turn=2,
        collected_at=datetime.now(timezone.utc),
    )


def _hyp(category: HypothesisCategory, seed: int) -> Hypothesis:
    return Hypothesis(
        hypothesis_id=f"hyp_{seed:012x}",
        statement=f"hypothesis {seed}",
        category=category,
        state=HypothesisState.CAPTURED,
        rationale="a reason",
        generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
        generated_at_turn=1,
    )


def _case(*, n_hypotheses: int, n_categories: int, n_evidence: int) -> Case:
    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="u",
        enterprise_id="o",
        title="t",
        description="d",
        state=CaseState.INVESTIGATING,
        current_turn=3,
        inquiry=InquiryData(
            proposed_problem_statement="intermittent latency",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
        problem_verification=ProblemVerification(
            symptom_statement="intermittent latency", severity=CaseSeverity.HIGH
        ),
    )
    case.evidence = [_ev(i) for i in range(n_evidence)]
    cats = list(HypothesisCategory)[: max(n_categories, 1)]
    case.hypotheses = {
        (h := _hyp(cats[i % len(cats)], i)).hypothesis_id: h
        for i in range(n_hypotheses)
    }
    return case


def test_no_crossing_below_the_work_gate(counter):
    """A case that has not crossed the ≥2/≥2/≥2 gate neither latches nor counts."""
    case = _case(n_hypotheses=1, n_categories=1, n_evidence=1)  # too little
    _recompute_assessment_state(case, provider_name="gemini")
    assert case.progress.work_gate_crossed is False
    assert counter.labels.call_count == 0


def test_crossing_latches_and_counts_once_for_the_provider(counter):
    """First crossing sets the latch and increments the provider's counter by 1."""
    case = _case(n_hypotheses=3, n_categories=2, n_evidence=2)  # crosses
    _recompute_assessment_state(case, provider_name="gemini")
    assert case.progress.work_gate_crossed is True
    assert _inc_calls(counter, "gemini") == 1


def test_latch_prevents_double_count_on_later_turns(counter):
    """Re-emitting the same work next turn (still past the gate) does not recount."""
    case = _case(n_hypotheses=3, n_categories=2, n_evidence=2)
    _recompute_assessment_state(case, provider_name="gemini")
    _recompute_assessment_state(case, provider_name="gemini")
    _recompute_assessment_state(case, provider_name="gemini")
    assert _inc_calls(counter, "gemini") == 1  # exactly once across three recomputes


def test_drop_below_gate_after_crossing_does_not_reset_or_recount(counter):
    """The latch is monotone: losing hypotheses later neither clears it nor
    re-arms a future re-count."""
    case = _case(n_hypotheses=3, n_categories=2, n_evidence=2)
    _recompute_assessment_state(case, provider_name="gemini")
    # Retire the differential back below the gate, then recompute again.
    case.hypotheses = {}
    _recompute_assessment_state(case, provider_name="gemini")
    assert case.progress.work_gate_crossed is True  # not reset
    # A case reloaded with the persisted latch already True must not re-count
    # even though it stands past the gate.
    case2 = _case(n_hypotheses=3, n_categories=2, n_evidence=2)
    case2.progress.work_gate_crossed = True  # simulate the persisted latch
    _recompute_assessment_state(case2, provider_name="gemini")
    assert _inc_calls(counter, "gemini") == 1


def test_provider_label_is_per_provider(counter):
    """Two providers crossing are counted under their own labels."""
    cg = _case(n_hypotheses=3, n_categories=2, n_evidence=2)
    co = _case(n_hypotheses=3, n_categories=2, n_evidence=2)
    _recompute_assessment_state(cg, provider_name="gemini")
    _recompute_assessment_state(co, provider_name="openai")
    assert _inc_calls(counter, "gemini") == 1
    assert _inc_calls(counter, "openai") == 1


def test_missing_provider_falls_back_to_unknown(counter):
    """A crossing with no provider identity is counted under 'unknown', not dropped."""
    case = _case(n_hypotheses=3, n_categories=2, n_evidence=2)
    _recompute_assessment_state(case)  # no provider_name → "unknown"
    assert case.progress.work_gate_crossed is True
    assert _inc_calls(counter, "unknown") == 1


class _RawProvider:
    provider_name = "anthropic"


class _EnumProvider:
    value = "gemini"  # mimics LLMProvider.GEMINI


class _RouterLLM:
    provider = _EnumProvider()


class _Router:
    """Mimics LLMRouter: no provider_name, but a settings.llm.provider enum."""

    class settings:  # noqa: N801 - stub attribute container
        llm = _RouterLLM()


class _RouterStrLLM:
    provider = "openai"  # a plain string value (defensive)


class _RouterStr:
    class settings:  # noqa: N801
        llm = _RouterStrLLM()


def test_resolve_provider_from_raw_provider():
    """A raw provider exposes provider_name directly."""
    assert _resolve_chat_provider_name(_RawProvider()) == "anthropic"


def test_resolve_provider_from_router_enum():
    """The router has no provider_name; fall back to settings.llm.provider.value."""
    assert _resolve_chat_provider_name(_Router()) == "gemini"


def test_resolve_provider_from_router_plain_string():
    """settings.llm.provider tolerated as a plain string too."""
    assert _resolve_chat_provider_name(_RouterStr()) == "openai"


def test_resolve_provider_none_is_unknown():
    """A missing/opaque provider resolves to 'unknown', never raises."""
    assert _resolve_chat_provider_name(None) == "unknown"
    assert _resolve_chat_provider_name(object()) == "unknown"


def test_latch_survives_progress_blob_round_trip():
    """The latch persists across a serialize/deserialize of the progress blob so
    the once-per-case guarantee holds across turns (the blob is reloaded each
    turn from the DB)."""
    case = _case(n_hypotheses=3, n_categories=2, n_evidence=2)
    _recompute_assessment_state(case, provider_name="gemini")
    dumped = case.progress.model_dump()
    assert dumped["work_gate_crossed"] is True
    from faultmaven.modules.case.domain.models import InvestigationProgress

    restored = InvestigationProgress.model_validate(dumped)
    assert restored.work_gate_crossed is True
