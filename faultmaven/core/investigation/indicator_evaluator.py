"""Indicator evaluator for v3 KB-resolution.

Evaluates each `### Cause N` subsection's Indicator field against
current case state to attribute the active Cause. Output drives
same-turn milestone collapse on KB-resolved cases — see
docs/architecture/investigation-engine/indicator-resolution.md.

Two evaluation paths:
- **Deterministic fast path**: ``match_predicates`` parsed from
  ``<!-- match: --> `` HTML-comment hints. Cheap, no LLM call.
- **Fallback**: ``case_evidence_qa`` — the canonical "given case
  evidence, answer X" tool. Used when no predicate hint is present
  or when the predicate is unknown to the evaluator.

A Cause is matched when ALL its Indicators evaluate true. Partial
matches do not count.
"""

from __future__ import annotations

import logging
import re
from typing import Awaitable, Callable, List, Optional

from faultmaven.core.investigation.schemas import (
    CauseChunk,
    CauseMatch,
    CauseMatchResult,
    IndicatorResult,
)

logger = logging.getLogger(__name__)


# Callable signatures injected by the engine at evaluator construction.
StepOutputResolver = Callable[[int], Optional[str]]
CaseEvidenceQAFn = Callable[[str], Awaitable[bool]]

STEP_REF_RE = re.compile(r"\[Step (\d+)\]")


class IndicatorEvaluator:
    """Stateless evaluator: holds engine-supplied data resolvers, no case state."""

    def __init__(
        self,
        step_output_resolver: StepOutputResolver,
        case_evidence_qa: Optional[CaseEvidenceQAFn] = None,
    ):
        """
        Args:
            step_output_resolver: Returns the text output recorded for
                Diagnostic Step ``N`` in the current case, or ``None``
                if no output is available yet. Used by deterministic
                predicates and as context for the prose-fallback question.
            case_evidence_qa: Async function for prose Indicator fallback.
                Takes the Indicator text question, returns True/False.
                When ``None``, prose Indicators evaluate to False (fail
                closed) so partial-data turns don't spuriously match.
        """
        self._resolve_step = step_output_resolver
        self._case_evidence_qa = case_evidence_qa

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def evaluate(
        self, runbook_id: str, causes: List[CauseChunk]
    ) -> CauseMatchResult:
        """Evaluate all Causes from a single runbook against case state.

        Returns a ``CauseMatchResult`` whose ``verdict`` and
        ``selected_cause`` drive the engine's branching per
        indicator-resolution.md §4.
        """
        cause_matches: List[CauseMatch] = []
        for cause in causes:
            match = await self._evaluate_cause(cause)
            cause_matches.append(match)

        # Real-Cause matches only — fallback (`is_fallback=True`) never
        # counts toward "matched" because its only Indicator is `[Default]`
        # which always evaluates False on the normal path.
        matched_real = [m for m in cause_matches if m.matched and not m.is_fallback]

        if len(matched_real) == 1:
            verdict = "single"
            selected = matched_real[0]
        elif len(matched_real) == 0:
            verdict = "none"
            selected = next((m for m in cause_matches if m.is_fallback), None)
        else:
            verdict = "multiple"
            selected = None

        return CauseMatchResult(
            runbook_id=runbook_id,
            causes=cause_matches,
            matched_causes=matched_real,
            verdict=verdict,
            selected_cause=selected,
        )

    # ------------------------------------------------------------------
    # Per-Cause / per-Indicator evaluation
    # ------------------------------------------------------------------

    async def _evaluate_cause(self, cause: CauseChunk) -> CauseMatch:
        results: List[IndicatorResult] = []
        for indicator_text in cause.indicators:
            results.append(await self._evaluate_indicator(indicator_text, cause))

        if not results:
            return CauseMatch(
                cause_name=cause.cause_name,
                indicator_results=[],
                matched=False,
                is_fallback=cause.is_fallback,
            )

        all_matched = all(r.matched for r in results)
        return CauseMatch(
            cause_name=cause.cause_name,
            indicator_results=results,
            matched=all_matched,
            is_fallback=cause.is_fallback,
        )

    async def _evaluate_indicator(
        self, indicator_text: str, cause: CauseChunk
    ) -> IndicatorResult:
        # `[Default]` is the fallback sentinel — never matches via the
        # normal evaluation path; the fallback Cause is selected only when
        # verdict="none" (all real Causes failed). See §4.
        if "[Default]" in indicator_text:
            return IndicatorResult(
                indicator_text=indicator_text,
                matched=False,
                method="deterministic",
            )

        predicate = self._find_matching_predicate(indicator_text, cause)
        if predicate is not None:
            try:
                matched = self._evaluate_predicate(predicate)
                return IndicatorResult(
                    indicator_text=indicator_text,
                    matched=matched,
                    method="deterministic",
                )
            except Exception as exc:  # noqa: BLE001 — log + fall through
                logger.warning(
                    "Predicate evaluation failed for %r: %s; falling back to case_evidence_qa",
                    predicate,
                    exc,
                )

        # Fallback: delegate to case_evidence_qa (engine-injected).
        if self._case_evidence_qa is None:
            logger.debug(
                "No case_evidence_qa available; indicator %r evaluates False",
                indicator_text,
            )
            return IndicatorResult(
                indicator_text=indicator_text,
                matched=False,
                method="case_evidence_qa",
            )

        question = f"Does the case evidence satisfy: {indicator_text}?"
        try:
            matched = await self._case_evidence_qa(question)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "case_evidence_qa fallback failed for indicator %r: %s",
                indicator_text,
                exc,
            )
            matched = False
        return IndicatorResult(
            indicator_text=indicator_text,
            matched=bool(matched),
            method="case_evidence_qa",
        )

    # ------------------------------------------------------------------
    # Predicate matching + evaluators
    # ------------------------------------------------------------------

    def _find_matching_predicate(
        self, indicator_text: str, cause: CauseChunk
    ) -> Optional[dict]:
        """Find a predicate whose ``step`` matches a ``[Step N]`` token
        in the Indicator entry. Predicates with no matching step or with
        unknown ``predicate`` names return None (caller falls back to
        ``case_evidence_qa``)."""
        if not cause.match_predicates:
            return None
        step_refs = {int(m.group(1)) for m in STEP_REF_RE.finditer(indicator_text)}
        if not step_refs:
            return None
        for pred in cause.match_predicates:
            step = pred.get("step")
            if isinstance(step, int) and step in step_refs:
                return pred
        return None

    def _evaluate_predicate(self, predicate: dict) -> bool:
        name = predicate.get("predicate")
        step = predicate.get("step")
        if not isinstance(step, int):
            raise ValueError(f"predicate missing valid 'step': {predicate!r}")
        output = self._resolve_step(step)
        if output is None:
            # Step has not been run yet — predicate cannot evaluate true.
            return False

        if name == "contains":
            target = predicate.get("target", "")
            return isinstance(target, str) and target in output

        if name == "absent":
            target = predicate.get("target", "")
            # Best-effort: 'absent' on text output = target not present.
            return isinstance(target, str) and target not in output

        if name == "exit_code":
            target = predicate.get("target")
            if not isinstance(target, int):
                return False
            # Match "exit code: N", "exit_code=N", "rc=N" patterns.
            for pattern in (
                rf"exit[_ ]?code[:=\s]+{target}\b",
                rf"\brc[:=\s]+{target}\b",
            ):
                if re.search(pattern, output, re.IGNORECASE):
                    return True
            return False

        if name == "threshold":
            target = predicate.get("target", "")
            op = predicate.get("op", ">")
            value = predicate.get("value")
            if not isinstance(target, str) or not isinstance(value, (int, float)):
                return False
            # Find "target = N" or "target: N" with optional %.
            pattern = rf"{re.escape(target)}\s*[:=]\s*([-+]?\d+(?:\.\d+)?)"
            m = re.search(pattern, output)
            if not m:
                return False
            actual = float(m.group(1))
            return _apply_op(actual, op, value)

        # Unknown predicate name — let the caller fall back.
        raise ValueError(f"unknown predicate '{name}'")


def _apply_op(actual: float, op: str, value: float) -> bool:
    if op == ">":
        return actual > value
    if op == ">=":
        return actual >= value
    if op == "<":
        return actual < value
    if op == "<=":
        return actual <= value
    if op == "==":
        return actual == value
    if op == "!=":
        return actual != value
    raise ValueError(f"unknown threshold op '{op}'")
