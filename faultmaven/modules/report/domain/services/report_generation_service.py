"""
Report Generation Service - Terminal Summary Generation

Auto-generates structured summaries when cases reach terminal state:
- RESOLUTION_SUMMARY: For RESOLVED cases (root cause, solution, evidence, timeline)
- CLOSURE_SUMMARY: For CLOSED cases (investigation state, approaches, closure reason)

Runbook generation is handled separately by ConversionService.

Architecture Reference: docs/architecture/investigation-engine/investigation-lifecycle-logic.md §1.7.4
"""

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from faultmaven.exceptions import (
    QUOTA_EXHAUSTED,
    ServiceException,
    ValidationException,
    is_billing_error,
)
from faultmaven.infrastructure.concurrency import (
    LockAcquisitionError,
    ReportLockManager,
)
from faultmaven.infrastructure.observability.tracing import trace

# Cross-module imports via contracts (Principle 2: Vertical Modules with Contracts).
# Report models are Case-owned and live in case.contracts.
from faultmaven.modules.case.contracts import (
    TERMINAL_HYPOTHESIS_STATES,
    Case,
    CaseReport,
    CaseState,
    CauseAssuranceGrade,
    ICaseRepository,
    InvestigationActionType,
    NeedObtainability,
    NeedPurpose,
    ReportGenerationRequest,
    ReportGenerationResponse,
    ReportStatus,
    ReportType,
    SolutionOutcome,
    classify_solution_outcome,
)
from faultmaven.utils.serialization import to_json_compatible

logger = logging.getLogger(__name__)


class ReportGenerationService:
    """
    Auto-generate terminal summaries for resolved and closed cases.

    Key Features:
    - RESOLUTION_SUMMARY for RESOLVED cases (root cause, solution, evidence, timeline)
    - CLOSURE_SUMMARY for CLOSED cases (investigation state, approaches, closure reason)
    - PII sanitization before storage
    - Fire-and-forget from milestone engine (failure doesn't block transition)
    - Trivial case detection (skipped by should_generate_terminal_summary guardrail)
    """

    MAX_REGENERATIONS = 2  # max total versions per type (initial + 1 regen)
    GENERATION_TIMEOUT_SECONDS = 30

    def __init__(
        self,
        case_repository: Optional[ICaseRepository] = None,
        lock_manager: Optional[ReportLockManager] = None,
        pii_redactor: Optional[Any] = None,
    ):
        """
        Initialize report generation service.

        Args:
            case_repository: Case repository for report persistence
            lock_manager: Optional lock manager for concurrency control
            pii_redactor: Optional PII redactor for sanitization
        """
        self.case_repository = case_repository
        self.lock_manager = lock_manager
        self.pii_redactor = pii_redactor

    @trace("generate_reports")
    async def generate_reports(
        self, case: Case, report_types: List[ReportType]
    ) -> ReportGenerationResponse:
        """
        Generate requested reports for a case with concurrency control.

        Args:
            case: Case object with investigation context
            report_types: List of report types to generate

        Returns:
            ReportGenerationResponse with generated reports

        Raises:
            ValidationException: If case not in valid state or regeneration limit exceeded
            LockAcquisitionError: If cannot acquire lock (another generation in progress)
        """
        # Validate case state
        self._validate_case_for_report_generation(case)

        # Cap check is now per-report-type and derived from the persisted
        # row count (every regen writes a new row). The previous
        # ``case.report_generation_count`` field never existed on the Case
        # model, so the cap was silently never enforced. See
        # ICaseRepository.count_reports.
        if self.case_repository:
            for report_type in report_types:
                count = await self.case_repository.count_reports(
                    case.case_id, report_type
                )
                if count >= self.MAX_REGENERATIONS:
                    raise ValidationException(
                        "regeneration_limit_exceeded",
                        f"Maximum {self.MAX_REGENERATIONS} versions of "
                        f"{report_type.value} reached for this case",
                    )

        logger.info(
            f"Generating {len(report_types)} reports for case",
            extra={"case_id": case.case_id, "types": [t.value for t in report_types]},
        )

        # Acquire lock if lock_manager available (prevents concurrent report generation)
        if self.lock_manager:
            async with self.lock_manager.lock(case.case_id, wait_timeout=30):
                logger.debug(f"Acquired report generation lock for case {case.case_id}")
                return await self._generate_reports_locked(case, report_types)
        else:
            # No lock manager - proceed without concurrency protection
            logger.warning(
                "No lock manager available - proceeding without concurrency protection"
            )
            return await self._generate_reports_locked(case, report_types)

    async def _generate_reports_locked(
        self, case: Case, report_types: List[ReportType]
    ) -> ReportGenerationResponse:
        """
        Internal method: Generate reports with lock already acquired.

        Args:
            case: Case object with investigation context
            report_types: List of report types to generate

        Returns:
            ReportGenerationResponse with generated reports
        """
        # Generate each report
        reports = []
        for report_type in report_types:
            start_time = time.time()

            try:
                report = await self._generate_single_report(case, report_type)

                # Persist report to storage via Case repository (TD-001: migrated from IReportStore)
                if self.case_repository:
                    await self.case_repository.add_report(report)
                    logger.info(
                        f"Report persisted to Case repository",
                        extra={"report_id": report.report_id, "case_id": case.case_id},
                    )

                reports.append(report)

                generation_time = int((time.time() - start_time) * 1000)
                logger.info(
                    f"Report generated successfully",
                    extra={
                        "case_id": case.case_id,
                        "report_type": report_type.value,
                        "generation_time_ms": generation_time,
                    },
                )

            except Exception as e:
                # Billing/quota exhaustion is permanent and affects every report
                # type — don't swallow-and-continue (the next type would hit the
                # same out-of-credits provider). Abort and propagate so the route
                # maps it to 402 instead of a misleading "no reports generated".
                if is_billing_error(e):
                    raise ServiceException(
                        "Report generation failed: AI provider is out of "
                        "quota or credits",
                        details={"error_code": QUOTA_EXHAUSTED},
                    ) from e
                logger.error(
                    f"Failed to generate {report_type.value} report: {e}",
                    extra={"case_id": case.case_id},
                    exc_info=True,
                )
                # Continue with other reports even if one fails
                continue

        if not reports:
            raise ValidationException(
                "report_generation_failed", "Failed to generate any reports"
            )

        # Calculate remaining regenerations from the persisted row count
        # AFTER this call's writes. Reported per-report-type would be
        # more accurate, but the response carries a single integer — use
        # the MAX (rosiest) remaining across the types we just wrote, so
        # we don't falsely zero out the UI affordance when there are
        # still slots on another type.
        remaining = self.MAX_REGENERATIONS
        if self.case_repository:
            type_remainings = []
            for report in reports:
                count = await self.case_repository.count_reports(
                    case.case_id, report.report_type
                )
                type_remainings.append(max(0, self.MAX_REGENERATIONS - count))
            if type_remainings:
                remaining = max(type_remainings)

        return ReportGenerationResponse(
            case_id=case.case_id, reports=reports, remaining_regenerations=remaining
        )

    async def _generate_single_report(
        self, case: Case, report_type: ReportType
    ) -> CaseReport:
        """Generate a single report by routing to the type-specific generator.

        Both RESOLUTION_SUMMARY and CLOSURE_SUMMARY are produced
        deterministically from case fields (markdown templated by Python),
        not via an LLM call. PII redaction runs over the rendered content
        if a redactor is configured.
        """
        start_time = time.time()

        # Extract case context
        context = self._extract_case_context(case)

        # Generate report content based on type
        auto_generated = False
        if report_type == ReportType.RESOLUTION_SUMMARY:
            content = await self._generate_resolution_summary(case, context)
            title = f"Resolution Summary: {case.title}"
            auto_generated = True
        elif report_type == ReportType.CLOSURE_SUMMARY:
            content = await self._generate_closure_summary(case, context)
            title = f"Closure Summary: {case.title}"
            auto_generated = True
        else:
            raise ValidationException(
                "invalid_report_type", f"Unknown report type: {report_type}"
            )

        # Sanitize PII if redactor available
        if self.pii_redactor:
            content = await self.pii_redactor.redact(content)

        generation_time_ms = int((time.time() - start_time) * 1000)

        # Version = (existing-row count for this type) + 1. Each
        # regeneration writes a new row, so this naturally increments
        # 1 → 2 → 3 → ... up to MAX_REGENERATIONS. Falls back to 1 when
        # the repo is unavailable (test paths).
        version = 1
        if self.case_repository:
            existing_count = await self.case_repository.count_reports(
                case.case_id, report_type
            )
            version = existing_count + 1

        now = datetime.now(timezone.utc)
        generated_at_str = to_json_compatible(now)
        return CaseReport(
            case_id=case.case_id,
            report_type=report_type,
            title=title,
            content=content,
            format="markdown",
            generation_status=ReportStatus.COMPLETED,
            generated_at=generated_at_str,
            updated_at=None,  # Will be set by repository.add_report to generated_at (for new reports)
            generation_time_ms=generation_time_ms,
            is_current=True,
            version=version,
            linked_to_closure=False,
            auto_generated=auto_generated,
            metadata=None,
        )

    # ============================================================
    # Shared formatters
    # ============================================================

    _CLOSURE_REASON_LABELS = {
        "inquiry_only": "Inquiry only — no investigation started",
        "solution_deferred": (
            "Closed — cause identified and fix documented, implementation deferred"
        ),
        "closed_rca_infeasible": (
            "Closed — root cause structurally unreachable; mitigation is the "
            "accepted strategy"
        ),
        "mitigation_sufficient": (
            "Closed — stabilized by a verified mitigation, root-cause analysis "
            "deferred"
        ),
        "closed_insufficient_evidence": (
            "Closed — insufficient evidence to establish the problem or its cause"
        ),
    }

    def _format_closure_reason_label(self, reason: Optional[str]) -> str:
        """Map a closure_reason enum string to a human label."""
        if not reason:
            return "Not specified"
        return self._CLOSURE_REASON_LABELS.get(reason, reason)

    def _insufficient_evidence_boundary_block(
        self, case: Case, hypotheses: List[Any]
    ) -> List[str]:
        """Render the data-boundary capture for an insufficient-evidence close.

        The two durable signals — surfaced from persisted case state:

        - **Residual candidates** — hypotheses still in play (state not REFUTED
          or RETIRED): the causes that remain plausible but could not be
          distinguished.
        - **Unmet discriminating data** — the outstanding ``causal_verification``
          evidence needs, flagging any the model declared *unobtainable* (the
          data wall). This is the "why" — the specific data whose absence
          blocked grounding.

        Empty-safe: if either list is empty the corresponding line is omitted;
        the header still records that the case hit a data boundary.

        The wording distinguishes the two ways the case reached
        INSUFFICIENT_EVIDENCE: a **declared data wall** (some discriminator was
        declared unobtainable) vs a **time-stall** (the investigation stalled
        with data that was never declared unavailable). Overstating a wall that
        was never asserted would be a false honesty claim.
        """
        outstanding = [
            n
            for n in (case.evidence_needs or [])
            if n.purpose == NeedPurpose.CAUSAL_VERIFICATION and n.is_outstanding
        ]
        has_declared_wall = any(
            n.obtainability == NeedObtainability.UNOBTAINABLE for n in outstanding
        )

        # What this block may CLAIM depends on what actually happened. The
        # "did enough work to rule things out" phrasing was licensed by the old
        # derivation, which only reached this reason from the
        # INSUFFICIENT_EVIDENCE cell — i.e. behind work_gate_passed (>=2
        # hypotheses across >=2 categories). This reason is now the default for
        # any close that established nothing, so a turn-2 close with no
        # candidates lands here too, and the sentence would assert an
        # elimination pass that never ran.
        #
        # Ruling things OUT requires having had candidates, so that is the
        # discriminator; the symptom flag only refines the no-candidates case.
        ruled_things_out = bool(getattr(case, "hypotheses", None))
        symptom_verified = bool(
            case.progress and getattr(case.progress, "symptom_verified", False)
        )

        block: List[str] = ["## Data Boundary — Why This Remains Unresolved\n"]
        if not ruled_things_out:
            block.append(
                "The reported problem was never established from the data "
                "available, so no cause was pursued.\n"
                if not symptom_verified
                else "The problem was verified, but the case closed before any "
                "candidate causes were put forward.\n"
            )
        elif has_declared_wall:
            block.append(
                "The investigation did enough work to rule things out but could "
                "not ground a single cause: the discriminating data needed to "
                "decide between the remaining candidates could not be obtained.\n"
            )
        else:
            block.append(
                "The investigation did enough work to rule things out but "
                "stalled before a single cause could be grounded from the data "
                "gathered so far.\n"
            )

        residual = [
            h
            for h in hypotheses
            if getattr(h, "state", None) not in TERMINAL_HYPOTHESIS_STATES
        ]
        if residual:
            residual.sort(key=lambda h: getattr(h, "likelihood", 0), reverse=True)
            block.append("**Candidates still in play:**")
            for h in residual[:5]:
                block.append(
                    f"- {h.statement} (confidence: {getattr(h, 'likelihood', 0):.0%})"
                )
            block.append("")

        if outstanding:
            block.append("**Outstanding discriminating data:**")
            for n in outstanding:
                wall = (
                    " — declared unobtainable"
                    if n.obtainability == NeedObtainability.UNOBTAINABLE
                    else ""
                )
                block.append(f"- {n.request_text}{wall}")
            block.append("")

        if has_declared_wall:
            block.append(
                "_If the missing data later becomes available, reopening or a "
                "fresh investigation can resolve this._\n"
            )
        else:
            block.append(
                "_Resuming the investigation with additional data or a fresh "
                "angle can resolve this._\n"
            )
        return block

    def _list_hypotheses(self, case: Case) -> List[Any]:
        """case.hypotheses is Dict[str, Hypothesis] — iterate values, not keys."""
        if isinstance(case.hypotheses, dict):
            return list(case.hypotheses.values())
        return list(case.hypotheses or [])

    def _evidence_citation_line(self, case: Case, ev: Any) -> str:
        """Format a single Evidence row as a citation bullet.

        ``category``, ``summary``, and ``source_type`` are required fields
        on the Pydantic Evidence model, so direct attribute access is
        safe. ``source_file_id`` is Optional (NULL for chat-extracted
        evidence) and ``case.find_uploaded_file`` is None-safe by design.
        """
        category_label = ev.category.value.replace("_", " ")
        summary = ev.summary
        file_meta = case.find_uploaded_file(ev.source_file_id)
        if file_meta is not None:
            source = f" — _{file_meta.filename}_"
        else:
            source = f" — _{ev.source_type.value}_"
        return f"- **[{category_label}]** {summary}{source}"

    @staticmethod
    def _solutions_by_outcome(case: Case) -> tuple[List[Any], List[Any]]:
        """Split the case's solutions into ``(applied, still-proposed)``.

        The engine mints one ``Solution`` per LLM fix proposal and never stamps
        its lifecycle fields, so ``case.solutions`` accumulates every re-proposal
        of the same remediation — three paraphrases of "bound the cache and
        restore the limit" in the case that prompted this (fm#1091), rendered as
        three numbered fixes under a heading that says they were *applied*.

        What actually happened is recorded on the ``ProposedAction`` each
        solution is co-created with. So the section is derived the same way the
        runbook boundary derives it (``classify_solution_outcome``) rather than by
        paraphrase-matching:

        - ``APPLIED``  — the user executed it. These are the fix.
        - ``FAILED``   — superseded, rejected, or engine-downgraded: never run,
          and therefore never "applied". Dropped entirely; the runbook boundary
          already refuses to launder these into knowledge, and a summary
          asserting them to the user is the same over-claim.
        - ``PROPOSED`` — uncorrelated or still pending. Whether it is rendered is
          the caller's call (see ``_permanent_fix_executed``), because a standing
          proposal means different things beside an executed permanent fix and
          beside an executed stop-gap.

        A case carrying NO ``ProposedAction`` at all is un-instrumented rather
        than un-executed — there is nothing to correlate against, and
        ``classify_solution_outcome`` would call every row PROPOSED. Such a case
        is left to the pre-existing "surface every solution" behavior by the
        caller, so an old resolved case does not acquire a "no fix was executed"
        claim the record cannot support.
        """
        proposed_actions = getattr(case, "proposed_actions", []) or []
        applied: List[Any] = []
        standing: List[Any] = []
        for sol in case.solutions or []:
            outcome = classify_solution_outcome(sol, proposed_actions)
            if outcome == SolutionOutcome.APPLIED:
                applied.append(sol)
            elif outcome != SolutionOutcome.FAILED:
                standing.append(sol)
        return applied, standing

    @staticmethod
    def _permanent_fix_executed(case: Case) -> bool:
        """Did the user execute a SOLUTION (permanent remediation), as opposed to
        a MITIGATION stop-gap?

        This is what decides whether a standing proposal is noise or content.
        ``classify_solution_outcome`` reports APPLIED for any accepted actionable
        action, mitigations included, and offer supersession covers SOLUTION
        offers only — so a pending SOLUTION beside an accepted MITIGATION is a
        genuinely distinct remediation (the permanent fix, still not run), while a
        pending SOLUTION beside an accepted SOLUTION is the model restating the
        fix at resolution time (the third entry in fm#1091). Dropping the first
        would delete the actual fix from the summary; keeping the second is the
        redundancy this section exists to remove.
        """
        for action in getattr(case, "proposed_actions", []) or []:
            action_type = getattr(action, "action_type", None)
            if (
                getattr(action, "state", None) == "accepted"
                and getattr(action_type, "value", action_type)
                == InvestigationActionType.SOLUTION.value
            ):
                return True
        return False

    def _format_solution_block(self, sol: Any, index: int) -> List[str]:
        """Render a single Solution as a list of paragraph-level lines.

        Each returned string is intended to become its own paragraph
        when the surrounding ``"\\n".join`` produces ``\\n\\n`` between
        them (every string ends with ``\\n``). Used by both the
        resolution summary's "Solution Applied" section and the closure
        summary's "Mitigation Status" section — the rendering is
        identical; only the surrounding heading differs.
        """
        sol_title = getattr(sol, "title", f"Solution {index}")
        # Defensive: occasional upstream rows leak the raw enum repr
        # ("SolutionType.CODE_FIX") into the title field. Substitute the
        # prettified solution_type label until that pipeline is fixed.
        if "SolutionType." in sol_title:
            sol_type = getattr(sol, "solution_type", None)
            sol_title = (
                sol_type.value.replace("_", " ").title()
                if hasattr(sol_type, "value")
                else f"Solution {index}"
            )
        sol_desc = getattr(sol, "longterm_fix", None) or getattr(
            sol, "immediate_action", None
        )
        lines = [f"**{index}. {sol_title}**\n"]
        if sol_desc:
            lines.append(f"{sol_desc}\n")
        if getattr(sol, "verification_method", None):
            lines.append(f"_Verified by: {sol.verification_method}_\n")
        return lines

    @staticmethod
    def _assurance_note(case: Case) -> Optional[str]:
        """A lower-assurance qualifier under the Root Cause heading, from the
        engine's assurance grade (M2 confirmation ladder). A counterfactually
        CONFIRMED cause needs no note; anything less is labeled so the report
        never presents an unconfirmed conclusion at full certainty (#572).

        The grade is RECOMPUTED from the persisted causal graph (a pure
        function), not read from the persisted progress field: terminal cases
        never recompute, so a case resolved before the field existed would
        read the NO_ROOT default forever and a regenerated report would
        falsely discredit a validated conclusion. The persisted field remains
        the per-turn observability signal; the report reads the graph truth.
        """
        from faultmaven.core.investigation.cause_assurance import (
            grade_cause_assurance,
        )

        grade = grade_cause_assurance(case)
        if grade == CauseAssuranceGrade.MECHANISTIC:
            return (
                "_Assurance: identified from the investigation's evidence, but "
                "not counterfactually confirmed — removing this cause was never "
                "explicitly tied to the problem disappearing._\n"
            )
        if grade == CauseAssuranceGrade.NO_ROOT:
            return (
                "_Assurance: stated by the assistant; this conclusion was not "
                "validated in the causal analysis._\n"
            )
        return None

    def _causal_map_block(self, case: Case) -> List[str]:
        """The engine-derived causal map section, or nothing.

        Rendering, gating, and the legend are owned by
        ``core.investigation.causal_map`` (see its docstrings). Fail-closed
        here: any error — the import included — omits the section rather
        than blocking report generation.
        """
        try:
            from faultmaven.core.investigation.causal_map import (
                CAUSAL_MAP_LEGEND,
                render_causal_map,
            )

            fenced = render_causal_map(case)
        except Exception:
            logger.warning(
                "Causal map rendering failed; omitting section",
                extra={"case_id": case.case_id},
                exc_info=True,
            )
            return []
        if not fenced:
            return []
        return [
            "## Causal Map\n",
            f"{CAUSAL_MAP_LEGEND}\n",
            f"{fenced}\n",
        ]

    async def _generate_resolution_summary(
        self, case: Case, context: Dict[str, Any]
    ) -> str:
        """Generate resolution summary for RESOLVED cases.

        Content structure per investigation-lifecycle-logic.md §4.5.0:
        - Problem Statement
        - Root Cause (with mechanism if available)
        - Causal Map (gated — established cause, non-trivial graph, no
          validated/refuted contradiction)
        - Solution Applied (the executed fix; "Proposed Solution" when none
          was executed)
        - Confirming Evidence (citation list, not a count)
        - Hypotheses Considered (grouped by status)
        - Timeline

        Milestone listing is intentionally omitted — the Issue tab
        already displays milestone chips with the same data, so the
        report should narrate rather than re-enumerate.
        """
        title = case.title or "Untitled Case"
        description = case.description or "No description provided."
        created = to_json_compatible(case.created_at) if case.created_at else "Unknown"
        resolved = (
            to_json_compatible(case.resolved_at) if case.resolved_at else "Unknown"
        )
        duration = context.get("duration", "Unknown")
        hypotheses = self._list_hypotheses(case)
        evidence_items = case.evidence or []

        parts = [
            f"# Resolution Summary: {title}\n",
            "## Problem Statement\n",
            f"{description}\n",
        ]

        # Root Cause — prefer the authoritative root_cause_conclusion,
        # fall back to validated hypotheses only when no conclusion exists.
        rcc = case.root_cause_conclusion
        if rcc and rcc.root_cause:
            root_cause_stated = True
            parts.append("## Root Cause\n")
            parts.append(f"{rcc.root_cause}\n")
            assurance_note = self._assurance_note(case)
            if assurance_note:
                parts.append(assurance_note)
            # Provenance (#987): when the engine PROMOTED this cause from the
            # user's confirmation plus a cited absence row rather than from
            # chain validation alone, say so. The assurance note above grades
            # how strongly the cause is held; this says how it came to be held.
            # Recorded and rendered together — a provenance field nothing reads
            # is not provenance, it is a comment in a database column.
            established_by = getattr(rcc, "established_by", None)
            if established_by:
                parts.append(f"_Established by: {established_by}._\n")
            if getattr(rcc, "mechanism", None):
                parts.append(f"**How it produced the symptom:** {rcc.mechanism}\n")
            if getattr(rcc, "contributing_factors", None):
                # The cause's co-necessary conjuncts (#1096) — engine-derived
                # from the graph's AND-sets, so the heading states co-necessity
                # rather than the weaker "contributed to". Without this the
                # report renders a cause the investigation established as a
                # conjunction as its first conjunct alone, and a runbook
                # harvested from it inherits the same half-cause.
                #
                # The heading attributes the requirement to PRODUCING THE
                # PROBLEM, not to the root cause: an AND-set can sit on any rung
                # of the mirrored chain, and a conjunct co-necessary with an
                # intermediate is a condition of that mechanism step rather than
                # of the cause. This wording is what the graph proves at every
                # depth.
                # Trailing \n on the bold label so the join produces a
                # blank line before the list — CommonMark requires this
                # for the bullets to render as a proper list.
                parts.append("**Producing the problem also required:**\n")
                for cf in rcc.contributing_factors:
                    parts.append(f"- {cf}")
                parts.append("")
        else:
            validated_hyps = [
                h
                for h in hypotheses
                if hasattr(h, "state")
                and hasattr(h.state, "value")
                and h.state.value == "validated"
            ]
            if validated_hyps:
                parts.append("## Root Cause\n")
                parts.append("_Identified via validated hypothesis._\n")
                for h in validated_hyps:
                    parts.append(f"**{h.statement}**\n")
                    if getattr(h, "rationale", None):
                        parts.append(f"{h.rationale}\n")
                # Last line already ends with \n; no extra separator needed.
            root_cause_stated = bool(validated_hyps)

        # Causal Map — after the cause is stated, before the fix. Renders
        # only for an established cause over a non-trivial graph, and only
        # under a stated Root Cause section: the assurance grade can pass
        # on graph state alone (validated root, no RootCauseConclusion, no
        # validated Hypothesis row), and a map with no named cause above it
        # would be an unexplained picture.
        if root_cause_stated:
            parts.extend(self._causal_map_block(case))

        # Solution Applied — the fix the user actually executed, not every fix
        # the assistant proposed on the way there (see _solutions_by_outcome).
        # A case with no ProposedAction rows carries no execution signal at all,
        # so it keeps the pre-existing "surface every solution" rendering rather
        # than acquiring a claim its record cannot support.
        if not (getattr(case, "proposed_actions", []) or []):
            applied_solutions, standing_solutions = list(case.solutions or []), []
        else:
            applied_solutions, standing_solutions = self._solutions_by_outcome(case)
        if applied_solutions:
            parts.append("## Solution Applied\n")
            for i, sol in enumerate(applied_solutions, 1):
                parts.extend(self._format_solution_block(sol, i))
        # A standing proposal is rendered only while the permanent fix is still
        # outstanding — beside an executed SOLUTION it is a restatement of it.
        if standing_solutions and not self._permanent_fix_executed(case):
            parts.append("## Proposed Solution\n")
            parts.append(
                "_The permanent fix below was proposed but not recorded as "
                "executed._\n"
                if applied_solutions
                else "_No fix was recorded as executed; this was the standing "
                "proposal when the case resolved._\n"
            )
            for i, sol in enumerate(standing_solutions, 1):
                parts.extend(self._format_solution_block(sol, i))
        # _format_solution_block's last line ends with \n; no extra separator needed.

        # Confirming Evidence — cite the evidence that grounded the conclusion,
        # not a bare count. Prefer the explicit evidence_basis on the
        # root cause conclusion; otherwise show evidence tagged with the
        # claim-anchored categories that prove cause or solution.
        cited_ids: List[str] = []
        if rcc and getattr(rcc, "evidence_basis", None):
            cited_ids = list(rcc.evidence_basis)

        # INV-30 polarity guard on BOTH branches (the evidence_basis list is
        # LLM-authored and can cite anything): an absence row renders as
        # "confirming" only when it qualifies as a resolution confirmation —
        # engine-authored M6 rows are failed-fix DISCONFIRMATIONS (the
        # opposite polarity) and premature rows from a failed fix window
        # confirm nothing. Same shared predicate as the readiness gate and
        # the confirm-stamp (function-local import: the _assurance_note
        # precedent for report → core.investigation reads).
        from faultmaven.core.investigation.cause_assurance import (
            resolution_confirmation_rows,
        )

        qualified_absence_ids = {
            getattr(e, "evidence_id", None) for e in resolution_confirmation_rows(case)
        }

        def _confirming_polarity(ev) -> bool:
            if getattr(getattr(ev, "category", None), "value", None) != (
                "causal_absence_evidence"
            ):
                return True
            return getattr(ev, "evidence_id", None) in qualified_absence_ids

        cited: List = []
        if cited_ids:
            ev_by_id = {getattr(ev, "evidence_id", ""): ev for ev in evidence_items}
            cited = [
                ev_by_id[i]
                for i in cited_ids
                if i in ev_by_id and _confirming_polarity(ev_by_id[i])
            ]
        if not cited:
            # Category fallback — also taken when the polarity filter emptied
            # the cited list (an LLM-authored evidence_basis can cite ONLY
            # disqualified absence rows; the section must then fall back to
            # the tagged rows rather than silently vanish).
            cited = [
                ev
                for ev in evidence_items
                if hasattr(getattr(ev, "category", None), "value")
                # Confirming evidence = the cause (causal_evidence) plus the
                # "fix worked at the cause level" signal (causal_absence_evidence,
                # which supersedes the removed legacy ``solution_evidence``).
                and ev.category.value in ("causal_evidence", "causal_absence_evidence")
                and _confirming_polarity(ev)
            ]

        if cited:
            parts.append("## Confirming Evidence\n")
            for ev in cited:
                parts.append(self._evidence_citation_line(case, ev))
            # Note any remaining evidence not cited here.
            remaining = max(0, len(evidence_items) - len(cited))
            if remaining > 0:
                parts.append(
                    f"\n_{remaining} additional evidence item"
                    f"{'s' if remaining != 1 else ''} collected during "
                    f"investigation (see the Evidence tab)._"
                )
            parts.append("")

        # Hypotheses Considered — what the prior section was actually showing,
        # now correctly named. Group by status so the reader sees the
        # validate/refute reasoning at a glance.
        if hypotheses:
            parts.append("## Hypotheses Considered\n")
            buckets: Dict[str, List[Any]] = {
                "validated": [],
                "refuted": [],
                "inconclusive": [],
                "other": [],
            }
            for h in hypotheses:
                status = h.state.value if hasattr(h.state, "value") else str(h.state)
                if status in buckets:
                    buckets[status].append(h)
                else:
                    buckets["other"].append(h)

            bucket_order = [
                ("validated", "Validated"),
                ("refuted", "Refuted"),
                ("inconclusive", "Inconclusive"),
                ("other", "Other"),
            ]
            for key, label in bucket_order:
                if not buckets[key]:
                    continue
                # Trailing \n on the bold label so the join produces a
                # blank line before the list (required by CommonMark for
                # the bullets to render as a list).
                parts.append(f"**{label}:**\n")
                for h in buckets[key]:
                    confidence = getattr(h, "likelihood", 0)
                    line = f"- {h.statement} _(confidence: {confidence:.0%})_"
                    if key == "refuted" and getattr(h, "refutation_reason", None):
                        # Two trailing spaces before \n produce a hard
                        # line break within the same list item.
                        line += f"  \n  Refuted by: {h.refutation_reason}"
                    parts.append(line)
                parts.append("")
            # Trailing empty already appended per-group; no extra needed here.

        # Timeline
        parts.append("## Timeline\n")
        parts.append(f"- **Created:** {created}")
        parts.append(f"- **Resolved:** {resolved}")
        parts.append(f"- **Duration:** {duration}")
        parts.append(f"- **Turns:** {getattr(case, 'current_turn', 0)}\n")

        return "\n".join(parts)

    async def _generate_closure_summary(
        self, case: Case, context: Dict[str, Any]
    ) -> str:
        """Generate closure summary for CLOSED cases.

        Content structure per investigation-lifecycle-logic.md §4.5.0:
        - Problem Statement
        - Investigation State (milestone/evidence/hypothesis counts)
        - Closure Reason (with human label)
        - Leading Hypotheses (top 5 by confidence)
        - Mitigation Status (when a mitigation was inserted)
        - Timeline
        - Recommendation (for closed_insufficient_evidence only)

        No Causal Map here — the map is a resolution-summary section only
        (product decision): a closed case's graph reads as a conclusion
        the case never reached.
        """
        title = case.title or "Untitled Case"
        description = case.description or "No description provided."
        created = to_json_compatible(case.created_at) if case.created_at else "Unknown"
        closed = to_json_compatible(case.closed_at) if case.closed_at else "Unknown"
        duration = context.get("duration", "Unknown")
        closure_reason_raw = getattr(case, "closure_reason", None)

        hypotheses = self._list_hypotheses(case)
        evidence_items = case.evidence or []
        solutions = case.solutions or []
        milestones = (
            case.progress.completed_milestones
            if hasattr(case, "progress") and case.progress
            else []
        )

        parts = [
            f"# Closure Summary: {title}\n",
            "## Problem Statement\n",
            f"{description}\n",
        ]

        # Investigation State — how far diagnosis progressed
        parts.append("## Investigation State\n")
        if milestones:
            parts.append(
                f"{len(milestones)} milestone"
                f"{'s' if len(milestones) != 1 else ''} reached: "
                + ", ".join(m.replace("_", " ") for m in milestones)
                + "."
            )
        else:
            parts.append("No investigation milestones were reached.")
        evidence_noun = "item" if len(evidence_items) == 1 else "items"
        hypothesis_noun = "hypothesis" if len(hypotheses) == 1 else "hypotheses"
        parts.append(
            f"\n{len(evidence_items)} evidence {evidence_noun} collected. "
            f"{len(hypotheses)} {hypothesis_noun} explored.\n"
        )

        # Closure Reason — human label, not raw enum
        parts.append("## Closure Reason\n")
        parts.append(f"{self._format_closure_reason_label(closure_reason_raw)}\n")

        # Data Boundary — the durable capture for insufficient-evidence closes.
        # The residual candidates + the specific unmet (unobtainable) need are the
        # honest partial: what could NOT be resolved, and why. This is the
        # flywheel/calibration signal (§5.4); it is rendered from persisted case
        # state, so no separate snapshot column is needed.
        if closure_reason_raw == "closed_insufficient_evidence":
            parts.extend(self._insufficient_evidence_boundary_block(case, hypotheses))

        # Leading Hypotheses — top hypotheses at time of closure
        if hypotheses:
            parts.append("## Leading Hypotheses\n")
            sorted_hyps = sorted(
                hypotheses,
                key=lambda h: getattr(h, "likelihood", 0),
                reverse=True,
            )
            for h in sorted_hyps[:5]:
                status_str = (
                    h.state.value if hasattr(h.state, "value") else str(h.state)
                )
                parts.append(
                    f"- **{h.statement}** — {status_str} "
                    f"(confidence: {getattr(h, 'likelihood', 0):.0%})"
                )
            parts.append("")

        # Mitigation Status — only meaningful when a mitigation was
        # actually inserted (the "stop the bleeding" move). For cases that
        # never stabilized, this section would mislead.
        was_mitigated = (
            case.progress is not None and case.progress.mitigation is not None
        )
        if solutions and was_mitigated:
            parts.append("## Mitigation Status\n")
            for i, sol in enumerate(solutions, 1):
                parts.extend(self._format_solution_block(sol, i))
            # _format_solution_block's last line ends with \n; no extra separator needed.

        # Timeline
        parts.append("## Timeline\n")
        parts.append(f"- **Created:** {created}")
        parts.append(f"- **Closed:** {closed}")
        parts.append(f"- **Duration:** {duration}")
        parts.append(f"- **Turns:** {getattr(case, 'current_turn', 0)}\n")

        # Recommendation — fires when investigation ran but didn't conclude.
        # The prior "escalated"/"abandoned" guard was dead code: those
        # values are not in VALID_CLOSURE_REASONS.
        # Only the insufficient-evidence close leaves an open lead worth
        # naming. A deferred fix, an unreachable cause, and a sufficient
        # mitigation each already carry their own next step, so a
        # "start here next time" block would misdescribe them.
        if closure_reason_raw == "closed_insufficient_evidence":
            parts.append("## Recommendation\n")
            if hypotheses:
                top_hyp = max(hypotheses, key=lambda h: getattr(h, "likelihood", 0))
                parts.append(
                    f"The most promising lead at time of closure was: "
                    f"**{top_hyp.statement}**. A follow-up investigation "
                    f"should start there.\n"
                )
            else:
                parts.append(
                    "No hypotheses were formulated. A fresh investigation "
                    "may be needed.\n"
                )

        return "\n".join(parts)

    def _extract_case_context(self, case: Case) -> Dict[str, Any]:
        """Extract relevant context from case for report generation."""
        context = {
            "title": case.title,
            "description": case.description,
            "status": case.state.value,
            "created_at": (
                to_json_compatible(case.created_at) if case.created_at else None
            ),
            "resolved_at": (
                to_json_compatible(case.resolved_at) if case.resolved_at else None
            ),
            "duration": self._calculate_duration(case),
            "message_count": case.message_count,
        }

        return context

    def _calculate_duration(self, case: Case) -> str:
        """Calculate case duration in human-readable format."""
        end_time = case.resolved_at or case.closed_at
        if end_time and case.created_at:
            duration = (end_time - case.created_at).total_seconds()
            hours = duration / 3600
            if hours < 1:
                return f"{int(hours * 60)} minutes"
            elif hours < 24:
                return f"{hours:.1f} hours"
            else:
                days = hours / 24
                return f"{days:.1f} days"
        return "Unknown"

    def _validate_case_for_report_generation(self, case: Case) -> None:
        """Validate case is in valid state for report generation."""
        valid_states = [CaseState.RESOLVED, CaseState.CLOSED]

        if case.state not in valid_states:
            raise ValidationException(
                "invalid_case_state",
                f"Cannot generate reports from {case.state.value} state. Case must be resolved or closed first.",
            )
