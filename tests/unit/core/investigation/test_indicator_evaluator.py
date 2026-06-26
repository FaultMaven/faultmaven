"""Tests for the v4 rung-level Indicator evaluator.

Covers:
- Deterministic predicates (contains, absent, exit_code, threshold), tri-state
- Refutation (deterministic contradiction → belief 0)
- k-of-n belief and the surface threshold
- Multi-Cause verdict resolution (single / none / multiple)
- Fallback Cause selection on verdict="none" and [Default] never matching
- case_evidence_qa fallback wiring (with and without it injected; never refutes)
"""

from typing import Dict, List, Optional

import pytest

from faultmaven.core.investigation.cause_schemas import CauseRecord
from faultmaven.core.investigation.indicator_evaluator import IndicatorEvaluator


def _cause(
    letter: str,
    name: str,
    rung_indicators: Dict[str, List[str]],
    predicates: Optional[List[dict]] = None,
    *,
    is_fallback: bool = False,
) -> CauseRecord:
    # A simple root→D chain so `path` is populated; the evaluator only reads
    # rung_indicators + match_predicates, so the node set can stay minimal.
    refs = list(rung_indicators.keys()) or ["root", "D"]
    chain_nodes = [{"ref": r, "node_type": "rung", "statement": r} for r in refs]
    return CauseRecord(
        cause_letter=letter,
        cause_name=name,
        cause_statement="s",
        chain_nodes=chain_nodes,
        rung_indicators=rung_indicators,
        match_predicates=predicates or [],
        is_fallback_cause=is_fallback,
    )


def _step_outputs(*pairs: tuple):
    """Build a step-output resolver from (step_num, output) pairs."""
    table = dict(pairs)
    return table.get


# ---------------------------------------------------------------------------
# Predicate evaluators (tri-state)
# ---------------------------------------------------------------------------


class TestPredicates:
    @pytest.mark.asyncio
    async def test_contains_matches(self):
        cause = _cause(
            "A",
            "x",
            {"root": ["[Step 1] cond"]},
            [{"step": 1, "predicate": "contains", "target": "OOMKilled"}],
        )
        e = IndicatorEvaluator(_step_outputs((1, "Reason=OOMKilled, exit 137")))
        result = await e.evaluate("rb-test", [cause])
        rr = result.causes[0].rung_results[0]
        assert rr.matched
        assert not rr.refuted
        assert rr.method == "deterministic"
        assert result.causes[0].belief == 1.0

    @pytest.mark.asyncio
    async def test_contains_refutes_when_absent(self):
        cause = _cause(
            "A",
            "x",
            {"root": ["[Step 1] cond"]},
            [{"step": 1, "predicate": "contains", "target": "OOMKilled"}],
        )
        e = IndicatorEvaluator(_step_outputs((1, "Reason=Error, exit 1")))
        result = await e.evaluate("rb-test", [cause])
        rr = result.causes[0].rung_results[0]
        assert not rr.matched
        assert rr.refuted
        assert result.causes[0].belief == 0.0

    @pytest.mark.asyncio
    async def test_absent_matches_when_target_missing(self):
        cause = _cause(
            "A",
            "x",
            {"root": ["[Step 1] no limits"]},
            [{"step": 1, "predicate": "absent", "target": "limits.memory"}],
        )
        e = IndicatorEvaluator(_step_outputs((1, "spec without limits set")))
        result = await e.evaluate("rb-test", [cause])
        assert result.causes[0].rung_results[0].matched

    @pytest.mark.asyncio
    async def test_absent_refutes_when_target_present(self):
        cause = _cause(
            "A",
            "x",
            {"root": ["[Step 1] no limits"]},
            [{"step": 1, "predicate": "absent", "target": "limits.memory"}],
        )
        e = IndicatorEvaluator(_step_outputs((1, "limits.memory: 512Mi")))
        result = await e.evaluate("rb-test", [cause])
        assert result.causes[0].rung_results[0].refuted

    @pytest.mark.asyncio
    async def test_exit_code_matches(self):
        cause = _cause(
            "A",
            "x",
            {"root": ["[Step 1] sigkill"]},
            [{"step": 1, "predicate": "exit_code", "target": 137}],
        )
        e = IndicatorEvaluator(_step_outputs((1, "Container exited; exit code: 137")))
        result = await e.evaluate("rb-test", [cause])
        assert result.causes[0].rung_results[0].matched

    @pytest.mark.asyncio
    async def test_exit_code_decides_on_last_reported_code(self):
        # A benign cleanup `exit 0` precedes the real command's failure 137.
        # The OR-over-all-codes form would have matched target=0; we must
        # decide on the last (final) code instead.
        cause0 = _cause(
            "A",
            "x",
            {"root": ["[Step 1] cleanup ok?"]},
            [{"step": 1, "predicate": "exit_code", "target": 0}],
        )
        cause137 = _cause(
            "B",
            "y",
            {"root": ["[Step 1] process killed?"]},
            [{"step": 1, "predicate": "exit_code", "target": 137}],
        )
        out = "cleanup exit_code=0\nprocess exit_code=137"
        e = IndicatorEvaluator(_step_outputs((1, out)))
        r0 = await e.evaluate("rb-test", [cause0])
        r137 = await e.evaluate("rb-test", [cause137])
        assert r0.causes[0].rung_results[0].refuted  # last code is 137, not 0
        assert r137.causes[0].rung_results[0].matched

    @pytest.mark.asyncio
    async def test_exit_code_ignores_leading_zeros(self):
        cause = _cause(
            "A",
            "x",
            {"root": ["[Step 1] code"]},
            [{"step": 1, "predicate": "exit_code", "target": 7}],
        )
        e = IndicatorEvaluator(_step_outputs((1, "exit_code=007")))
        result = await e.evaluate("rb-test", [cause])
        assert result.causes[0].rung_results[0].matched

    @pytest.mark.asyncio
    async def test_exit_code_no_code_is_untested(self):
        cause = _cause(
            "A",
            "x",
            {"root": ["[Step 1] sigkill"]},
            [{"step": 1, "predicate": "exit_code", "target": 137}],
        )
        e = IndicatorEvaluator(_step_outputs((1, "container is running fine")))
        result = await e.evaluate("rb-test", [cause])
        rr = result.causes[0].rung_results[0]
        assert not rr.matched
        assert not rr.refuted
        assert rr.method == "untested"

    @pytest.mark.asyncio
    async def test_threshold_matches(self):
        cause = _cause(
            "A",
            "x",
            {"root": ["[Step 1] high mem"]},
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
        assert result.causes[0].rung_results[0].matched

    @pytest.mark.asyncio
    async def test_threshold_refutes_below(self):
        cause = _cause(
            "A",
            "x",
            {"root": ["[Step 1] high mem"]},
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
        e = IndicatorEvaluator(_step_outputs((1, "memory_pct = 0.40")))
        result = await e.evaluate("rb-test", [cause])
        assert result.causes[0].rung_results[0].refuted

    @pytest.mark.asyncio
    async def test_threshold_metric_missing_is_untested(self):
        cause = _cause(
            "A",
            "x",
            {"root": ["[Step 1] high mem"]},
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
        e = IndicatorEvaluator(_step_outputs((1, "no such metric here")))
        result = await e.evaluate("rb-test", [cause])
        rr = result.causes[0].rung_results[0]
        assert not rr.matched and not rr.refuted
        assert rr.method == "untested"

    @pytest.mark.asyncio
    async def test_threshold_equality_tolerates_float_representation(self):
        # op '==' must not refute on the IEEE-754 representation of 0.3.
        cause = _cause(
            "A",
            "x",
            {"root": ["[Step 1] ratio"]},
            [
                {
                    "step": 1,
                    "predicate": "threshold",
                    "target": "ratio",
                    "op": "==",
                    "value": 0.3,
                }
            ],
        )
        e = IndicatorEvaluator(_step_outputs((1, "ratio = 0.30000000000000004")))
        result = await e.evaluate("rb-test", [cause])
        assert result.causes[0].rung_results[0].matched

    @pytest.mark.asyncio
    async def test_step_not_run_is_untested(self):
        cause = _cause(
            "A",
            "x",
            {"root": ["[Step 1] needed"]},
            [{"step": 1, "predicate": "contains", "target": "x"}],
        )
        e = IndicatorEvaluator(lambda step: None)  # No step output anywhere
        result = await e.evaluate("rb-test", [cause])
        rr = result.causes[0].rung_results[0]
        assert not rr.matched and not rr.refuted
        assert rr.method == "untested"
        # Untested (not refuted) → belief is matched/total = 0/1, not pruned.
        assert result.causes[0].belief == 0.0


# ---------------------------------------------------------------------------
# k-of-n belief
# ---------------------------------------------------------------------------


class TestBelief:
    @pytest.mark.asyncio
    async def test_partial_match_scales_belief(self):
        # 2 rungs, 1 matched, 1 untested (not refuted) → belief 0.5.
        cause = _cause(
            "A",
            "x",
            {"root": ["[Step 1] cond A"], "D": ["[Step 2] cond B"]},
            [
                {"step": 1, "predicate": "contains", "target": "yes"},
                {"step": 2, "predicate": "contains", "target": "missing"},
            ],
        )
        # Step 2 has not run → untested (avoids false refutation).
        e = IndicatorEvaluator(_step_outputs((1, "yes is here")))
        result = await e.evaluate("rb-test", [cause])
        assert result.causes[0].belief == 0.5

    @pytest.mark.asyncio
    async def test_one_refuted_rung_prunes_whole_chain(self):
        # 2 rungs, 1 matched, 1 deterministically refuted → belief 0.
        cause = _cause(
            "A",
            "x",
            {"root": ["[Step 1] cond A"], "D": ["[Step 2] cond B"]},
            [
                {"step": 1, "predicate": "contains", "target": "yes"},
                {"step": 2, "predicate": "contains", "target": "missing"},
            ],
        )
        e = IndicatorEvaluator(_step_outputs((1, "yes is here"), (2, "not present")))
        result = await e.evaluate("rb-test", [cause])
        assert result.causes[0].belief == 0.0


# ---------------------------------------------------------------------------
# Verdict resolution
# ---------------------------------------------------------------------------


class TestVerdict:
    @pytest.mark.asyncio
    async def test_single_match(self):
        cause_a = _cause(
            "A",
            "real",
            {"root": ["[Step 1] cond"]},
            [{"step": 1, "predicate": "contains", "target": "x"}],
        )
        cause_z = _cause("Z", "fallback", {"D": ["[Default]"]}, is_fallback=True)
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
            {"root": ["[Step 1] cond"]},
            [{"step": 1, "predicate": "contains", "target": "missing"}],
        )
        cause_z = _cause("Z", "fallback", {"D": ["[Default]"]}, is_fallback=True)
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
            {"root": ["[Step 1] cond"]},
            [{"step": 1, "predicate": "contains", "target": "x"}],
        )
        cause_b = _cause(
            "B",
            "second",
            {"root": ["[Step 1] cond"]},
            [{"step": 1, "predicate": "contains", "target": "x"}],
        )
        cause_z = _cause("Z", "fallback", {"D": ["[Default]"]}, is_fallback=True)
        e = IndicatorEvaluator(_step_outputs((1, "value x present")))
        result = await e.evaluate("rb-test", [cause_a, cause_b, cause_z])
        assert result.verdict == "multiple"
        assert result.selected_cause is None
        assert len(result.live_causes) == 2

    @pytest.mark.asyncio
    async def test_default_token_never_matches(self):
        cause_z = _cause("Z", "fallback", {"D": ["[Default]"]}, is_fallback=True)
        e = IndicatorEvaluator(_step_outputs((1, "anything")))
        result = await e.evaluate("rb-test", [cause_z])
        # Fallback Cause is skipped (belief 0); its rung_results stay empty.
        assert result.causes[0].is_fallback
        assert result.causes[0].belief == 0.0
        assert result.causes[0].rung_results == []

    @pytest.mark.asyncio
    async def test_below_threshold_not_live(self):
        # A single Cause whose only signal is untested → belief 0 → not live.
        cause_a = _cause(
            "A",
            "real",
            {"root": ["[Step 1] cond"]},
            [{"step": 1, "predicate": "contains", "target": "x"}],
        )
        cause_z = _cause("Z", "fallback", {"D": ["[Default]"]}, is_fallback=True)
        e = IndicatorEvaluator(lambda _: None)  # step never ran → untested
        result = await e.evaluate("rb-test", [cause_a, cause_z])
        assert result.verdict == "none"
        assert result.selected_cause.is_fallback


# ---------------------------------------------------------------------------
# case_evidence_qa fallback (T2 — never refutes)
# ---------------------------------------------------------------------------


class TestFallback:
    @pytest.mark.asyncio
    async def test_no_predicate_falls_back_to_case_evidence_qa(self):
        # Indicator references [Symptom] (no step), so no predicate matches.
        cause = _cause("A", "x", {"root": ["[Symptom] log line FATAL"]})

        calls = []

        async def fake_case_qa(question: str) -> bool:
            calls.append(question)
            return True

        e = IndicatorEvaluator(
            step_output_resolver=lambda _: None,
            case_evidence_qa=fake_case_qa,
        )
        result = await e.evaluate("rb-test", [cause])
        rr = result.causes[0].rung_results[0]
        assert rr.matched
        assert not rr.refuted
        assert rr.method == "case_evidence_qa"
        assert len(calls) == 1
        assert "FATAL" in calls[0]

    @pytest.mark.asyncio
    async def test_case_evidence_qa_no_never_refutes(self):
        # A "no" from T2 lowers belief but must NOT refute (soundness).
        cause = _cause("A", "x", {"root": ["[Symptom] log line FATAL"]})

        async def fake_case_qa(question: str) -> bool:
            return False

        e = IndicatorEvaluator(
            step_output_resolver=lambda _: None,
            case_evidence_qa=fake_case_qa,
        )
        result = await e.evaluate("rb-test", [cause])
        rr = result.causes[0].rung_results[0]
        assert not rr.matched
        assert not rr.refuted
        assert result.causes[0].belief == 0.0

    @pytest.mark.asyncio
    async def test_unknown_predicate_falls_back_to_case_evidence_qa(self):
        # `regex` is not in the controlled vocabulary; the evaluator should
        # fall through to case_evidence_qa rather than crash.
        cause = _cause(
            "A",
            "x",
            {"root": ["[Step 1] cond"]},
            [{"step": 1, "predicate": "regex", "target": "x.*"}],
        )

        async def fake_case_qa(question: str) -> bool:
            return False

        e = IndicatorEvaluator(
            step_output_resolver=lambda _: "anything",
            case_evidence_qa=fake_case_qa,
        )
        result = await e.evaluate("rb-test", [cause])
        assert result.causes[0].rung_results[0].method == "case_evidence_qa"

    @pytest.mark.asyncio
    async def test_no_fallback_injection_is_untested(self):
        cause = _cause("A", "x", {"root": ["[Symptom] something"]})
        e = IndicatorEvaluator(
            step_output_resolver=lambda _: None,
            case_evidence_qa=None,
        )
        result = await e.evaluate("rb-test", [cause])
        rr = result.causes[0].rung_results[0]
        assert not rr.matched and not rr.refuted
        assert rr.method == "untested"
