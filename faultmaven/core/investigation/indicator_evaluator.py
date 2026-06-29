"""Runbook Cause matcher — rung-level evaluation (v4).

Evaluates each retrieved Cause's per-rung indicators against current case state
and returns rung-level results with a k-of-n belief, per
docs/architecture/investigation-engine/runbook-cause-matching.md §2–§5.

Two tiers:

- **T1 deterministic** (per rung): a ``match_predicate`` (``absent`` /
  ``contains`` / ``exit_code`` / ``threshold``) whose ``step`` matches a
  ``[Step N]`` token in the indicator. When the referenced step has run and the
  predicate is *determinable*: holds → **matched**, contradicted → **refuted** (a
  deterministic contradiction prunes the chain). When the step has not run, or
  the value can't be parsed: **untested**. In FaultMaven this tier is largely
  inert — FM investigates uploaded evidence rather than executing a runbook's
  numbered steps, so there is no per-step output to resolve — but it still
  *refutes* when a step output happens to contradict a predicate.

- **T2 semantic** (per CAUSE, holistic — #545): one ``case_evidence_qa`` call
  asking whether the case evidence *supports this whole cause*, judged from the
  cause's symptom-level description (name + statement + chain prose), NOT the
  per-rung ``[Step N]`` operator indicators. Diagnosis showed the operator-step
  indicators (``operationState.phase is Failed``, ``kubectl describe job shows
  …``) are written at tool/API-output level, which case evidence (symptom-level
  logs) rarely satisfies literally, so per-rung T2 returned NO for everything and
  the matcher never fired. A holistic per-cause judgment matches the right cause
  and discriminates the wrong ones (clean ``single`` verdict). A "no" lowers
  belief but never *refutes*; when ``case_evidence_qa`` is unavailable the cause
  rests on T1 alone.

Belief: ``0`` if any rung is deterministically refuted; else
``1.0`` when the holistic T2 supports the cause, otherwise the deterministic
T1 matched-rung fraction (so a step-output match still counts when T2 is absent).

Verdict (spec §4): a Cause is "live" when its belief is at/above the surface
threshold (refuted Causes have belief 0, so they're excluded). 0 live → fallback;
1 live → single attribution; ≥2 live → surface the set for LLM disambiguation.
The runbook is a *prior, not a gate*.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from typing import Awaitable, Callable, Dict, List, Literal, Optional

from faultmaven.core.investigation.cause_schemas import (
    CauseMatch,
    CauseMatchResult,
    CauseRecord,
    RungResult,
    is_problem_node,
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
            case_evidence_qa: Async T2 semantic resolver — takes a condition
                string, returns True/False against the case evidence. Called
                ONCE per cause with the holistic cause condition (#545). When
                ``None``, the cause rests on the deterministic T1 tier alone
                (fail-open: no semantic match, and never a refutation).
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
        # Each cause's holistic T2 is one independent classifier call — evaluate
        # them concurrently so a multi-cause runbook costs one round-trip of wall
        # time, not N. gather preserves input order.
        cause_matches = list(
            await asyncio.gather(*(self._evaluate_cause(c) for c in causes))
        )

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
        # ref → aggregated (matched, refuted) over that rung's T1 indicators.
        rung_state: Dict[str, Dict[str, bool]] = {}
        for ref, indicators in cause.rung_indicators.items():
            for indicator_text in indicators:
                rr = self._evaluate_rung_t1(ref, indicator_text, cause)
                rung_results.append(rr)
                st = rung_state.setdefault(ref, {"matched": False, "refuted": False})
                st["matched"] = st["matched"] or rr.matched
                st["refuted"] = st["refuted"] or rr.refuted

        refuted = any(st["refuted"] for st in rung_state.values())
        total = len(cause.rung_indicators)  # rungs that carry indicators

        if refuted:
            # A deterministic contradiction prunes the chain hard (§4).
            belief = 0.0
        else:
            # T2 semantic tier is ONE holistic per-cause judgment (#545) over the
            # cause's symptom-level STATEMENT — the sole load-bearing match surface
            # (runbook-content-architecture.md § "Match surface"). Per-rung
            # indicators are optional/inert for matching, so the Statement match is
            # NOT gated on the cause having indicator rungs: a Cause with a real
            # Statement but no Indicators is still eligible (the earlier
            # ``total == 0 → 0`` gate contradicted that ratified decision). A
            # content-less Cause (no usable Statement) still can't match —
            # ``_build_cause_condition`` returns None → ``_holistic_cause_match``
            # is False → it falls to the (here 0) deterministic T1 fraction.
            t1_matched = sum(1 for st in rung_state.values() if st["matched"])
            t1_frac = (t1_matched / total) if total else 0.0
            holistic = await self._holistic_cause_match(cause)
            belief = 1.0 if holistic else t1_frac

        return CauseMatch(
            cause_letter=cause.cause_letter,
            cause_name=cause.cause_name,
            path=cause.path,
            rung_results=rung_results,
            belief=belief,
            is_fallback=False,
        )

    def _evaluate_rung_t1(
        self, ref: str, indicator_text: str, cause: CauseRecord
    ) -> RungResult:
        """Deterministic (T1) evaluation of one rung indicator.

        Returns ``matched`` / ``refuted`` only when a ``[Step N]`` predicate is
        determinable against recorded step output; otherwise ``untested``. The
        semantic (T2) tier is handled once per cause in ``_holistic_cause_match``,
        not here — see the module docstring (#545).
        """
        # `[Default]` is the "no real indicator" sentinel (fallback Cause only;
        # short-circuited upstream — defensive here).
        if "[Default]" in indicator_text:
            return RungResult(
                rung_ref=ref,
                indicator_text=indicator_text,
                matched=False,
                refuted=False,
                method="untested",
            )

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
            except Exception as exc:  # noqa: BLE001 — log + treat as untested
                logger.warning(
                    "Predicate evaluation failed for %r: %s; treating as untested",
                    predicate,
                    exc,
                )

        # No determinable predicate → untested (the holistic T2 carries semantics).
        return RungResult(
            rung_ref=ref,
            indicator_text=indicator_text,
            matched=False,
            refuted=False,
            method="untested",
        )

    async def _holistic_cause_match(self, cause: CauseRecord) -> bool:
        """One holistic T2 judgment: does the case evidence support this cause?

        Judged from the cause's SYMPTOM-level description (name + statement +
        chain prose), not the per-rung operator indicators (which are
        tool-output-phrased and don't match symptom-level evidence — #545). Never
        refutes; ``False`` (or no QA injected / an error) just means 'not matched'.
        """
        if self._case_evidence_qa is None:
            return False
        condition = self._build_cause_condition(cause)
        if condition is None:
            # No symptom-level description to judge on — abstain rather than ask
            # the classifier a contentless question (which it might answer YES to).
            return False
        try:
            return bool(await self._case_evidence_qa(condition))
        except Exception as exc:  # noqa: BLE001 — a prior must never break the turn
            logger.warning(
                "holistic case_evidence_qa failed for cause %r: %s",
                cause.cause_letter,
                exc,
            )
            return False

    @staticmethod
    def _build_cause_condition(cause: CauseRecord) -> Optional[str]:
        """Render a cause as a symptom-level condition for the holistic T2 call.

        Returns ``None`` when the cause carries no usable **mechanism** — no
        ``cause_statement`` and no non-problem chain prose. Per decision (b) the
        symptom-level Statement (or, failing that, the chain prose) is the
        load-bearing match surface; the ``cause_name`` is only its *subject*, so a
        name on its own is NOT enough to judge on — matching on a bare title
        ("The case is explained by 'Disk saturation'") would let a Statement-less
        cause attribute on its heading alone. The PROBLEM (D) node is excluded via
        the shared ``is_problem_node`` so the symptom-under-investigation never
        leaks into the condition (which would make every cause trivially match).
        """
        chain = " → ".join(
            (n.get("statement") or "").strip()
            for n in cause.chain_nodes
            if not is_problem_node(n) and (n.get("statement") or "").strip()
        )
        mechanism = (cause.cause_statement or "").strip() or chain
        if not mechanism:
            return None
        subject = (cause.cause_name or "").strip() or cause.cause_letter
        return f"The case is explained by the cause '{subject}' — {mechanism}"

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
        """Tri-state evaluation of a step-addressed predicate against its step's
        FULL output.

        Resolves the predicate's ``step`` to that step's complete output, then
        defers operator semantics to ``evaluate_predicate_against_text`` with
        ``complete=True`` — a step's output is the entire source the predicate
        ranges over, so an absent substring is decisive here. ``untested`` when
        the step hasn't run; we never *refute* on missing data.
        """
        step = predicate.get("step")
        if not isinstance(step, int):
            raise ValueError(f"predicate missing valid 'step': {predicate!r}")
        output = self._resolve_step(step)
        if output is None:
            return "untested"
        return evaluate_predicate_against_text(predicate, output, complete=True)


def evaluate_predicate_against_text(
    predicate: dict, text: str, *, complete: bool
) -> PredicateVerdict:
    """Tri-state evaluation of a deterministic predicate against a text source.

    Shared by the step-addressed tier (``IndicatorEvaluator._evaluate_predicate``,
    one step's full output) and the content-addressed differential-intake tier
    (one submitted datum's preprocessing digest). The two differ only in whether
    ``text`` is COMPLETE, and ``complete`` is what keeps both sound.

    ``complete=True`` — ``text`` is the entire content the predicate ranges over,
    so absence is decisive: ``contains`` → refuted when missing, ``absent`` →
    matched when missing.

    ``complete=False`` — ``text`` is a verbatim SUBSET of the real content (a
    Tier-1 extractor digest such as ``structural_index.file_extract``, which keeps
    the crime-scene/context, not every raw line). A substring may live in the
    dropped remainder, so absence is NOT decisive: we trust only what is PRESENT
    and return ``untested`` on a miss — never a false ``refuted`` (``contains``)
    or false ``matched`` (``absent``) inferred from the digest's gaps. This is the
    subset-trust rule that preserves NO-INCORRECT-CONCLUSION on lossy input.

    ``exit_code`` / ``threshold`` are presence-only either way — a parsed value is
    verbatim wherever it appears, and no parse → ``untested`` — so ``complete``
    does not change them. ``untested`` for any unparseable/malformed value; a
    ValueError only for an unknown predicate name (the caller treats it as
    ``untested``).
    """
    name = predicate.get("predicate")

    if name == "contains":
        target = predicate.get("target", "")
        if not isinstance(target, str):
            return "untested"
        if target in text:
            return "matched"
        return "refuted" if complete else "untested"

    if name == "absent":
        target = predicate.get("target", "")
        if not isinstance(target, str):
            return "untested"
        if target in text:
            return "refuted"  # present → the 'absent' assertion is contradicted
        return "matched" if complete else "untested"

    if name == "exit_code":
        target = predicate.get("target")
        if not isinstance(target, int):
            return "untested"
        found = re.findall(r"(?:exit[_ ]?code|rc)[:=\s]+(-?\d+)", text, re.IGNORECASE)
        if not found:
            return "untested"  # no exit code reported — can't decide
        # Decide on the LAST reported code — the final command's status. An
        # earlier OR-over-all-codes form matched a benign cleanup `exit 0` while
        # the real command failed (and missed when both appeared). Compare as int
        # so leading-zero forms ('007') still equal target 7.
        return "matched" if int(found[-1]) == target else "refuted"

    if name == "threshold":
        target = predicate.get("target", "")
        op = predicate.get("op", ">")
        value = predicate.get("value")
        if not isinstance(target, str) or not isinstance(value, (int, float)):
            return "untested"
        m = re.search(rf"{re.escape(target)}\s*[:=]\s*([-+]?\d+(?:\.\d+)?)", text)
        if not m:
            return "untested"  # metric not present — can't decide
        return "matched" if _apply_op(float(m.group(1)), op, value) else "refuted"

    # Unknown predicate name — let the caller fall back (T2 / untested).
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
        return math.isclose(actual, value, rel_tol=1e-9, abs_tol=1e-9)
    if op == "!=":
        return not math.isclose(actual, value, rel_tol=1e-9, abs_tol=1e-9)
    raise ValueError(f"unknown threshold op '{op}'")
