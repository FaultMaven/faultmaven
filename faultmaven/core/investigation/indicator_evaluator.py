"""Runbook Cause matcher — rung-level evaluation (v4).

Evaluates each retrieved Cause's per-rung indicators against current case state
and returns rung-level results with a k-of-n belief, per
docs/architecture/investigation-engine/runbook-cause-matching.md §2–§5.

Layered evaluation, per rung indicator:

- **T1 deterministic**: a ``match_predicate`` (``absent`` / ``contains`` /
  ``exit_code`` / ``threshold``) whose ``step`` matches a ``[Step N]`` token in
  the indicator. When the referenced step has run and the predicate is
  *determinable*: holds → **matched**, contradicted → **refuted** (a
  deterministic contradiction prunes the chain). When the step has not run, or
  the value can't be parsed from the output: **untested**.
- **T2 semantic**: ``case_evidence_qa`` on the indicator prose. A "no" lowers
  belief (not matched) but never *refutes* — it is softer than a deterministic
  predicate. When unavailable: untested (fail-open, never a false refutation).

Verdict (spec §4): a Cause is "live" when its belief is above the surface
threshold (refuted Causes have belief 0, so they're excluded). 0 live → fallback;
1 live → single attribution; ≥2 live → surface the set for LLM disambiguation.
The runbook is a *prior, not a gate* — a partially-matching chain still surfaces
at lower confidence, and the always-available T2 tier means predicates never gate.
"""

from __future__ import annotations

import logging
import re
from typing import Awaitable, Callable, Dict, List, Literal, Optional

from faultmaven.core.investigation.cause_schemas import (
    CauseMatch,
    CauseMatchResult,
    CauseRecord,
    RungResult,
)

logger = logging.getLogger(__name__)

# Callable signatures injected by the engine at evaluator construction.
StepOutputResolver = Callable[[int], Optional[str]]
CaseEvidenceQAFn = Callable[[str], Awaitable[bool]]

STEP_REF_RE = re.compile(r"\[Step (\d+)\]")

# A Cause is "live" (surfaced as a candidate / attributable) when at least this
# fraction of its indicator-bearing rungs match. Tunable; validated against the
# sim harness in increment 5. Kept at half because the robust T2 tier is always
# available — a low bar favors recall (a relevant runbook is never invisible),
# while refutation (belief 0) handles the precision side.
SURFACE_THRESHOLD = 0.5

PredicateVerdict = Literal["matched", "refuted", "untested"]


class IndicatorEvaluator:
    """Stateless evaluator: holds engine-supplied data resolvers, no case state."""

    def __init__(
        self,
        step_output_resolver: StepOutputResolver,
        case_evidence_qa: Optional[CaseEvidenceQAFn] = None,
    ):
        """
        Args:
            step_output_resolver: Returns the text output recorded for Diagnostic
                Step ``N`` in the current case, or ``None`` if no output is
                available yet (the step has not run). Drives T1 predicates.
            case_evidence_qa: Async T2 fallback — takes an indicator question,
                returns True/False. When ``None``, prose indicators are
                ``untested`` (fail-open: they don't match and don't refute).
        """
        self._resolve_step = step_output_resolver
        self._case_evidence_qa = case_evidence_qa

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def evaluate(
        self, runbook_id: str, causes: List[CauseRecord]
    ) -> CauseMatchResult:
        """Evaluate all Causes from a single runbook against case state.

        Returns a ``CauseMatchResult`` whose ``verdict`` / ``selected_cause``
        drive the engine's branching per runbook-cause-matching.md §4.
        """
        cause_matches = [await self._evaluate_cause(c) for c in causes]

        # Live = a real (non-fallback) Cause above the surface threshold. A
        # refuted Cause has belief 0, so the threshold already excludes it.
        live = [
            m
            for m in cause_matches
            if not m.is_fallback and m.belief >= SURFACE_THRESHOLD
        ]

        if len(live) == 1:
            verdict, selected = "single", live[0]
        elif len(live) == 0:
            verdict = "none"
            selected = next((m for m in cause_matches if m.is_fallback), None)
        else:
            verdict, selected = "multiple", None

        return CauseMatchResult(
            runbook_id=runbook_id,
            causes=cause_matches,
            live_causes=live,
            verdict=verdict,
            selected_cause=selected,
        )

    # ------------------------------------------------------------------
    # Per-Cause / per-rung evaluation
    # ------------------------------------------------------------------

    async def _evaluate_cause(self, cause: CauseRecord) -> CauseMatch:
        # The fallback Cause is selected by its flag when verdict='none'; its only
        # indicator is [Default], which never matches — skip evaluation.
        if cause.is_fallback_cause:
            return CauseMatch(
                cause_letter=cause.cause_letter,
                cause_name=cause.cause_name,
                path=cause.path,
                rung_results=[],
                belief=0.0,
                is_fallback=True,
            )

        rung_results: List[RungResult] = []
        # ref → aggregated (matched, refuted) over that rung's indicators.
        rung_state: Dict[str, Dict[str, bool]] = {}
        for ref, indicators in cause.rung_indicators.items():
            for indicator_text in indicators:
                rr = await self._evaluate_rung(ref, indicator_text, cause)
                rung_results.append(rr)
                st = rung_state.setdefault(ref, {"matched": False, "refuted": False})
                st["matched"] = st["matched"] or rr.matched
                st["refuted"] = st["refuted"] or rr.refuted

        refuted = any(st["refuted"] for st in rung_state.values())
        total = len(cause.rung_indicators)  # rungs that carry indicators
        matched = sum(1 for st in rung_state.values() if st["matched"])
        # Refutation prunes the chain hard (§4); otherwise monotone k-of-n.
        belief = 0.0 if (refuted or total == 0) else matched / total

        return CauseMatch(
            cause_letter=cause.cause_letter,
            cause_name=cause.cause_name,
            path=cause.path,
            rung_results=rung_results,
            belief=belief,
            is_fallback=False,
        )

    async def _evaluate_rung(
        self, ref: str, indicator_text: str, cause: CauseRecord
    ) -> RungResult:
        # `[Default]` is the fallback sentinel — it has no rung and never matches.
        if "[Default]" in indicator_text:
            return RungResult(
                rung_ref=ref,
                indicator_text=indicator_text,
                matched=False,
                refuted=False,
                method="untested",
            )

        # T1 — deterministic predicate.
        predicate = self._find_matching_predicate(indicator_text, cause)
        if predicate is not None:
            try:
                verdict = self._evaluate_predicate(predicate)
                return RungResult(
                    rung_ref=ref,
                    indicator_text=indicator_text,
                    matched=verdict == "matched",
                    refuted=verdict == "refuted",
                    method="deterministic" if verdict != "untested" else "untested",
                )
            except Exception as exc:  # noqa: BLE001 — log + fall through to T2
                logger.warning(
                    "Predicate evaluation failed for %r: %s; falling back to "
                    "case_evidence_qa",
                    predicate,
                    exc,
                )

        # T2 — semantic fallback (never refutes; an unavailable QA is untested).
        if self._case_evidence_qa is None:
            return RungResult(
                rung_ref=ref,
                indicator_text=indicator_text,
                matched=False,
                refuted=False,
                method="untested",
            )
        question = f"Does the case evidence satisfy: {indicator_text}?"
        try:
            matched = bool(await self._case_evidence_qa(question))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "case_evidence_qa fallback failed for indicator %r: %s",
                indicator_text,
                exc,
            )
            matched = False
        return RungResult(
            rung_ref=ref,
            indicator_text=indicator_text,
            matched=matched,
            refuted=False,
            method="case_evidence_qa",
        )

    # ------------------------------------------------------------------
    # Predicate matching + evaluators
    # ------------------------------------------------------------------

    def _find_matching_predicate(
        self, indicator_text: str, cause: CauseRecord
    ) -> Optional[dict]:
        """Find a predicate whose ``step`` matches a ``[Step N]`` token in the
        indicator. No matching step → None (caller falls back to T2)."""
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

    def _evaluate_predicate(self, predicate: dict) -> PredicateVerdict:
        """Tri-state evaluation of a deterministic predicate.

        ``untested`` — the step hasn't run, or the value can't be parsed from the
        output (we never *refute* on missing/unparseable data — that would be a
        false negative). ``matched`` / ``refuted`` only when the output makes the
        condition decidable.
        """
        name = predicate.get("predicate")
        step = predicate.get("step")
        if not isinstance(step, int):
            raise ValueError(f"predicate missing valid 'step': {predicate!r}")
        output = self._resolve_step(step)
        if output is None:
            return "untested"

        if name == "contains":
            target = predicate.get("target", "")
            if not isinstance(target, str):
                return "untested"
            return "matched" if target in output else "refuted"

        if name == "absent":
            target = predicate.get("target", "")
            if not isinstance(target, str):
                return "untested"
            return "matched" if target not in output else "refuted"

        if name == "exit_code":
            target = predicate.get("target")
            if not isinstance(target, int):
                return "untested"
            found = re.findall(
                r"(?:exit[_ ]?code|rc)[:=\s]+(\d+)", output, re.IGNORECASE
            )
            if not found:
                return "untested"  # step ran but reported no exit code
            return "matched" if str(target) in found else "refuted"

        if name == "threshold":
            target = predicate.get("target", "")
            op = predicate.get("op", ">")
            value = predicate.get("value")
            if not isinstance(target, str) or not isinstance(value, (int, float)):
                return "untested"
            m = re.search(rf"{re.escape(target)}\s*[:=]\s*([-+]?\d+(?:\.\d+)?)", output)
            if not m:
                return "untested"  # metric not in output — can't decide
            return "matched" if _apply_op(float(m.group(1)), op, value) else "refuted"

        # Unknown predicate name — let the caller fall back to T2.
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
