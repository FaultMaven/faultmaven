"""Tests for the v4 rung-level Indicator evaluator.

Covers:
- Deterministic predicates (contains, absent, exit_code, threshold), tri-state
- Refutation (deterministic contradiction → belief 0)
- k-of-n belief and the surface threshold
- Multi-Cause verdict resolution (single / none / multiple)
- Fallback Cause selection on verdict="none" and [Default] never matching
- Holistic per-cause T2 (#545): one judgment per cause, never refutes
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
# Holistic per-cause T2 (#545) — one judgment per cause, never refutes
# ---------------------------------------------------------------------------


class TestHolisticT2:
    @pytest.mark.asyncio
    async def test_holistic_match_makes_cause_live(self):
        # No determinable predicate; the holistic per-cause judgment says the
        # evidence supports the cause → belief 1.0 → live → single verdict.
        cause = _cause("A", "Disk full", {"root": ["[Step 1] df shows 100%"]})
        cause_z = _cause("Z", "fallback", {"D": ["[Default]"]}, is_fallback=True)

        calls = []

        async def yes_qa(condition: str) -> bool:
            calls.append(condition)
            return True

        e = IndicatorEvaluator(lambda _: None, case_evidence_qa=yes_qa)
        result = await e.evaluate("rb", [cause, cause_z])
        assert result.causes[0].belief == 1.0
        assert result.verdict == "single"
        # ONE holistic call per non-fallback cause, built from the cause
        # description — NOT the per-rung operator indicator.
        assert len(calls) == 1
        assert "Disk full" in calls[0]
        assert "df shows 100%" not in calls[0]  # the operator step is not the question

    @pytest.mark.asyncio
    async def test_holistic_no_is_not_a_refutation(self):
        # Holistic "no" → not matched, belief 0 (no T1 signal), but NEVER refuted.
        cause = _cause("A", "x", {"root": ["[Symptom] log line FATAL"]})

        async def no_qa(_condition: str) -> bool:
            return False

        e = IndicatorEvaluator(lambda _: None, case_evidence_qa=no_qa)
        result = await e.evaluate("rb", [cause])
        assert result.causes[0].belief == 0.0
        assert not any(rr.refuted for rr in result.causes[0].rung_results)

    @pytest.mark.asyncio
    async def test_holistic_discriminates_to_single(self):
        # Two real causes; holistic supports only the matching one → single.
        a = _cause("A", "Right cause", {"root": ["[Symptom] x"]})
        b = _cause("B", "Wrong cause", {"root": ["[Symptom] y"]})
        z = _cause("Z", "fallback", {"D": ["[Default]"]}, is_fallback=True)

        async def qa(condition: str) -> bool:
            return "Right cause" in condition

        e = IndicatorEvaluator(lambda _: None, case_evidence_qa=qa)
        result = await e.evaluate("rb", [a, b, z])
        assert result.verdict == "single"
        assert result.selected_cause.cause_name == "Right cause"

    @pytest.mark.asyncio
    async def test_deterministic_refutation_overrides_holistic(self):
        # A deterministic T1 refutation prunes the chain even when holistic = yes.
        cause = _cause(
            "A",
            "x",
            {"root": ["[Step 1] cond"]},
            [{"step": 1, "predicate": "contains", "target": "missing"}],
        )

        async def yes_qa(_condition: str) -> bool:
            return True

        e = IndicatorEvaluator(_step_outputs((1, "present")), case_evidence_qa=yes_qa)
        result = await e.evaluate("rb", [cause])
        assert result.causes[0].belief == 0.0  # refuted dominates

    @pytest.mark.asyncio
    async def test_unknown_predicate_is_untested_no_crash(self):
        # `regex` is not in the controlled vocabulary → untested (T1), no crash;
        # the holistic tier then carries the semantics.
        cause = _cause(
            "A",
            "x",
            {"root": ["[Step 1] cond"]},
            [{"step": 1, "predicate": "regex", "target": "x.*"}],
        )

        async def no_qa(_condition: str) -> bool:
            return False

        e = IndicatorEvaluator(lambda _: "anything", case_evidence_qa=no_qa)
        result = await e.evaluate("rb", [cause])
        assert result.causes[0].rung_results[0].method == "untested"
        assert result.causes[0].belief == 0.0

    @pytest.mark.asyncio
    async def test_no_qa_injection_rests_on_t1(self):
        # Without a QA resolver the cause rests on T1 alone (untested → belief 0).
        cause = _cause("A", "x", {"root": ["[Symptom] something"]})
        e = IndicatorEvaluator(lambda _: None, case_evidence_qa=None)
        result = await e.evaluate("rb", [cause])
        rr = result.causes[0].rung_results[0]
        assert not rr.matched and not rr.refuted
        assert result.causes[0].belief == 0.0

    @pytest.mark.asyncio
    async def test_indicatorless_cause_with_statement_goes_live_on_holistic(self):
        # Decision (b): the symptom-level Statement is the sole load-bearing match
        # surface; per-rung indicators are optional/inert. So a cause with a usable
        # Statement but NO indicator rungs IS eligible — a holistic YES lifts it to
        # belief 1.0 and consults the classifier. (Earlier #545 drafts zeroed such
        # causes; that contradicted the ratified match surface and was removed.)
        cause = _cause("A", "x", {})  # empty rung_indicators, but cause_statement="s"

        called = []

        async def yes_qa(condition: str) -> bool:
            called.append(condition)
            return True

        e = IndicatorEvaluator(lambda _: None, case_evidence_qa=yes_qa)
        result = await e.evaluate("rb", [cause])
        assert result.causes[0].belief == 1.0
        assert len(called) == 1  # holistic IS consulted over the Statement

    @pytest.mark.asyncio
    async def test_contentless_cause_cannot_match_even_with_holistic_yes(self):
        # The empty-Statement protection: a cause with NO name, NO statement, and
        # no non-problem chain prose has nothing to judge — `_build_cause_condition`
        # returns None, the classifier is NOT consulted, and belief stays 0 even if
        # the QA would have said YES. This is what keeps a degenerate cause from
        # matching on a contentless question.
        cause = CauseRecord(
            cause_letter="A",
            cause_name="",
            cause_statement="",
            chain_nodes=[{"ref": "D", "node_type": "problem", "statement": ""}],
            rung_indicators={},
        )

        called = []

        async def yes_qa(condition: str) -> bool:
            called.append(condition)
            return True

        e = IndicatorEvaluator(lambda _: None, case_evidence_qa=yes_qa)
        result = await e.evaluate("rb", [cause])
        assert result.causes[0].belief == 0.0
        assert called == []  # contentless → classifier never asked


class TestBuildCauseCondition:
    def test_uses_name_and_statement(self):
        c = CauseRecord(
            cause_letter="A",
            cause_name="Disk full",
            cause_statement="the data volume is at 100%",
            chain_nodes=[],
            rung_indicators={},
        )
        cond = IndicatorEvaluator._build_cause_condition(c)
        assert "Disk full" in cond and "100%" in cond

    def test_falls_back_to_chain_excluding_problem_node(self):
        c = CauseRecord(
            cause_letter="A",
            cause_name="N",
            cause_statement="",
            chain_nodes=[
                {"ref": "root", "node_type": "root", "statement": "root happens"},
                {"ref": "D", "node_type": "problem", "statement": "the problem"},
            ],
            rung_indicators={},
        )
        cond = IndicatorEvaluator._build_cause_condition(c)
        assert "root happens" in cond
        assert "the problem" not in cond  # the shared problem node is excluded

    def test_excludes_problem_node_identified_by_ref_d_only(self):
        # A pack may mark the problem node by ref='D' without node_type. The
        # symptom-under-investigation must still be excluded (else it leaks into
        # the condition and the classifier matches every cause).
        c = CauseRecord(
            cause_letter="A",
            cause_name="N",
            cause_statement="",
            chain_nodes=[
                {"ref": "root", "node_type": "root", "statement": "root happens"},
                {"ref": "D", "statement": "the deploy is broken"},  # no node_type
            ],
            rung_indicators={},
        )
        cond = IndicatorEvaluator._build_cause_condition(c)
        assert "root happens" in cond
        assert "the deploy is broken" not in cond

    def test_none_when_no_usable_content(self):
        # No name, no statement, no non-problem chain prose → None (don't ask the
        # classifier a contentless question).
        c = CauseRecord(
            cause_letter="A",
            cause_name="",
            cause_statement="",
            chain_nodes=[{"ref": "D", "node_type": "problem", "statement": "p"}],
            rung_indicators={},
        )
        assert IndicatorEvaluator._build_cause_condition(c) is None
