"""Tests for the v3 Indicator evaluator.

Covers:
- Deterministic predicates (contains, absent, exit_code, threshold)
- Multi-Cause verdict resolution (single / none / multiple)
- Fallback Cause selection on verdict="none"
- [Default] token never matches via the normal path
- case_evidence_qa fallback wiring (with and without it injected)
"""

from typing import Optional

import pytest

from faultmaven.core.investigation.indicator_evaluator import IndicatorEvaluator
from faultmaven.core.investigation.schemas import CauseChunk


def _cause(
    letter: str,
    name: str,
    indicators: list[str],
    predicates: Optional[list[dict]] = None,
    *,
    is_fallback: bool = False,
) -> CauseChunk:
    return CauseChunk(
        runbook_id="rb-test",
        cause_letter=letter,
        cause_name=name,
        statement="s",
        mechanism="m",
        indicators=indicators,
        match_predicates=predicates or [],
        mitigation="mit",
        resolution="res",
        verification="ver",
        is_fallback=is_fallback,
    )


def _step_outputs(*pairs: tuple[int, str]):
    """Build a step-output resolver from (step_num, output) pairs."""
    table = dict(pairs)
    return table.get


# ---------------------------------------------------------------------------
# Predicate evaluators
# ---------------------------------------------------------------------------


class TestPredicates:
    @pytest.mark.asyncio
    async def test_contains_matches(self):
        cause = _cause(
            "A",
            "x",
            ["[Step 1] cond"],
            [{"step": 1, "predicate": "contains", "target": "OOMKilled"}],
        )
        e = IndicatorEvaluator(_step_outputs((1, "Reason=OOMKilled, exit 137")))
        result = await e.evaluate("rb-test", [cause])
        assert result.causes[0].matched
        assert result.causes[0].indicator_results[0].method == "deterministic"

    @pytest.mark.asyncio
    async def test_contains_does_not_match(self):
        cause = _cause(
            "A",
            "x",
            ["[Step 1] cond"],
            [{"step": 1, "predicate": "contains", "target": "OOMKilled"}],
        )
        e = IndicatorEvaluator(_step_outputs((1, "Reason=Error, exit 1")))
        result = await e.evaluate("rb-test", [cause])
        assert not result.causes[0].matched

    @pytest.mark.asyncio
    async def test_absent_matches_when_target_missing(self):
        cause = _cause(
            "A",
            "x",
            ["[Step 1] no limits"],
            [{"step": 1, "predicate": "absent", "target": "limits.memory"}],
        )
        e = IndicatorEvaluator(_step_outputs((1, "spec without limits set")))
        result = await e.evaluate("rb-test", [cause])
        assert result.causes[0].matched

    @pytest.mark.asyncio
    async def test_absent_does_not_match_when_target_present(self):
        cause = _cause(
            "A",
            "x",
            ["[Step 1] no limits"],
            [{"step": 1, "predicate": "absent", "target": "limits.memory"}],
        )
        e = IndicatorEvaluator(_step_outputs((1, "limits.memory: 512Mi")))
        result = await e.evaluate("rb-test", [cause])
        assert not result.causes[0].matched

    @pytest.mark.asyncio
    async def test_exit_code_matches(self):
        cause = _cause(
            "A",
            "x",
            ["[Step 1] sigkill"],
            [{"step": 1, "predicate": "exit_code", "target": 137}],
        )
        e = IndicatorEvaluator(_step_outputs((1, "Container exited; exit code: 137")))
        result = await e.evaluate("rb-test", [cause])
        assert result.causes[0].matched

    @pytest.mark.asyncio
    async def test_threshold_matches(self):
        cause = _cause(
            "A",
            "x",
            ["[Step 1] high mem"],
            [
                {
                    "step": 1,
                    "predicate": "threshold",
                    "target": "memory_pct",
                    "op": ">",
                    "value": 0.85,
                }
            ],
        )
        e = IndicatorEvaluator(_step_outputs((1, "memory_pct = 0.92")))
        result = await e.evaluate("rb-test", [cause])
        assert result.causes[0].matched

    @pytest.mark.asyncio
    async def test_step_not_run_evaluates_false(self):
        cause = _cause(
            "A",
            "x",
            ["[Step 1] needed"],
            [{"step": 1, "predicate": "contains", "target": "x"}],
        )
        e = IndicatorEvaluator(lambda step: None)  # No step output anywhere
        result = await e.evaluate("rb-test", [cause])
        assert not result.causes[0].matched


# ---------------------------------------------------------------------------
# Verdict resolution
# ---------------------------------------------------------------------------


class TestVerdict:
    @pytest.mark.asyncio
    async def test_single_match(self):
        cause_a = _cause(
            "A",
            "real",
            ["[Step 1] cond"],
            [{"step": 1, "predicate": "contains", "target": "x"}],
        )
        cause_z = _cause("Z", "fallback", ["[Default]"], is_fallback=True)
        e = IndicatorEvaluator(_step_outputs((1, "value x present")))
        result = await e.evaluate("rb-test", [cause_a, cause_z])
        assert result.verdict == "single"
        assert result.selected_cause is not None
        assert result.selected_cause.cause_name == "real"

    @pytest.mark.asyncio
    async def test_none_selects_fallback(self):
        cause_a = _cause(
            "A",
            "real",
            ["[Step 1] cond"],
            [{"step": 1, "predicate": "contains", "target": "missing"}],
        )
        cause_z = _cause("Z", "fallback", ["[Default]"], is_fallback=True)
        e = IndicatorEvaluator(_step_outputs((1, "value x present")))
        result = await e.evaluate("rb-test", [cause_a, cause_z])
        assert result.verdict == "none"
        assert result.selected_cause is not None
        assert result.selected_cause.is_fallback

    @pytest.mark.asyncio
    async def test_multiple_matches_yields_no_selection(self):
        cause_a = _cause(
            "A",
            "first",
            ["[Step 1] cond"],
            [{"step": 1, "predicate": "contains", "target": "x"}],
        )
        cause_b = _cause(
            "B",
            "second",
            ["[Step 1] cond"],
            [{"step": 1, "predicate": "contains", "target": "x"}],
        )
        cause_z = _cause("Z", "fallback", ["[Default]"], is_fallback=True)
        e = IndicatorEvaluator(_step_outputs((1, "value x present")))
        result = await e.evaluate("rb-test", [cause_a, cause_b, cause_z])
        assert result.verdict == "multiple"
        assert result.selected_cause is None
        assert len(result.matched_causes) == 2

    @pytest.mark.asyncio
    async def test_default_token_never_matches_via_normal_path(self):
        cause_z = _cause("Z", "fallback", ["[Default]"], is_fallback=True)
        e = IndicatorEvaluator(_step_outputs((1, "anything")))
        result = await e.evaluate("rb-test", [cause_z])
        # Fallback Cause's only Indicator is [Default] which never evaluates True
        assert not result.causes[0].matched

    @pytest.mark.asyncio
    async def test_all_indicators_required_for_match(self):
        # AND semantics: Indicator A passes, Indicator B fails → Cause not matched.
        cause = _cause(
            "A",
            "x",
            ["[Step 1] cond A", "[Step 2] cond B"],
            [
                {"step": 1, "predicate": "contains", "target": "yes"},
                {"step": 2, "predicate": "contains", "target": "missing"},
            ],
        )
        e = IndicatorEvaluator(_step_outputs((1, "yes is here"), (2, "no match")))
        result = await e.evaluate("rb-test", [cause])
        assert not result.causes[0].matched
        assert result.causes[0].indicator_results[0].matched is True
        assert result.causes[0].indicator_results[1].matched is False


# ---------------------------------------------------------------------------
# case_evidence_qa fallback
# ---------------------------------------------------------------------------


class TestFallback:
    @pytest.mark.asyncio
    async def test_no_predicate_falls_back_to_case_evidence_qa(self):
        # Indicator references [Symptom] (no step), so no predicate matches.
        cause = _cause("A", "x", ["[Symptom] log line FATAL"])

        calls = []

        async def fake_case_qa(question: str) -> bool:
            calls.append(question)
            return True

        e = IndicatorEvaluator(
            step_output_resolver=lambda _: None,
            case_evidence_qa=fake_case_qa,
        )
        result = await e.evaluate("rb-test", [cause])
        assert result.causes[0].matched
        assert result.causes[0].indicator_results[0].method == "case_evidence_qa"
        assert len(calls) == 1
        assert "FATAL" in calls[0]

    @pytest.mark.asyncio
    async def test_unknown_predicate_falls_back_to_case_evidence_qa(self):
        # `regex` is not in the v3 controlled vocabulary; the evaluator
        # should fall through to case_evidence_qa.
        cause = _cause(
            "A",
            "x",
            ["[Step 1] cond"],
            [{"step": 1, "predicate": "regex", "target": "x.*"}],
        )

        async def fake_case_qa(question: str) -> bool:
            return False

        e = IndicatorEvaluator(
            step_output_resolver=lambda _: "anything",
            case_evidence_qa=fake_case_qa,
        )
        result = await e.evaluate("rb-test", [cause])
        assert result.causes[0].indicator_results[0].method == "case_evidence_qa"

    @pytest.mark.asyncio
    async def test_no_fallback_injection_evaluates_false(self):
        cause = _cause("A", "x", ["[Symptom] something"])
        e = IndicatorEvaluator(
            step_output_resolver=lambda _: None,
            case_evidence_qa=None,
        )
        result = await e.evaluate("rb-test", [cause])
        assert not result.causes[0].matched
        assert result.causes[0].indicator_results[0].method == "case_evidence_qa"
