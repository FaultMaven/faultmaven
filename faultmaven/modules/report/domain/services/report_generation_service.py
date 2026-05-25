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

from faultmaven.exceptions import ValidationException
from faultmaven.infrastructure.concurrency import (
    LockAcquisitionError,
    ReportLockManager,
)
from faultmaven.infrastructure.observability.tracing import trace

# Cross-module imports via contracts (Principle 2: Vertical Modules with Contracts).
# Report models are Case-owned and live in case.contracts.
from faultmaven.modules.case.contracts import (
    Case,
    CaseReport,
    CaseStatus,
    ICaseRepository,
    InvestigationPath,
    ReportGenerationRequest,
    ReportGenerationResponse,
    ReportStatus,
    ReportType,
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

    MAX_REGENERATIONS = 5
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

        # Check regeneration limit (fields may not exist on older Case models)
        report_gen_count = getattr(case, "report_generation_count", 0)
        max_regenerations = getattr(case, "max_report_regenerations", 5)
        if report_gen_count >= max_regenerations:
            raise ValidationException(
                "regeneration_limit_exceeded",
                f"Maximum {max_regenerations} regenerations allowed",
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

        # Calculate remaining regenerations
        report_gen_count = getattr(case, "report_generation_count", 0)
        max_regenerations = getattr(case, "max_report_regenerations", 5)
        remaining = max(0, max_regenerations - (report_gen_count + 1))

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
            version=getattr(case, "report_generation_count", 0) + 1,
            linked_to_closure=False,
            auto_generated=auto_generated,
            metadata=None,
        )

    # ============================================================
    # Shared formatters
    # ============================================================

    _PATH_LABELS = {
        InvestigationPath.ROOT_CAUSE: "Root Cause (DIAGNOSIS → TREATMENT)",
        InvestigationPath.MITIGATION_FIRST: (
            "Mitigation First (DIAGNOSIS → MITIGATION → DIAGNOSIS → TREATMENT)"
        ),
    }

    _CLOSURE_REASON_LABELS = {
        "inquiry_only": "Inquiry only — no investigation started",
        "closed_after_investigation": (
            "Closed after investigation — root cause not confirmed"
        ),
        "mitigation_sufficient": (
            "Mitigation sufficient — no further root-cause analysis pursued"
        ),
    }

    def _format_investigation_path_section(self, case: Case) -> Optional[str]:
        """Render the '## Investigation Path' section from case.path_selection.

        Returns None when no path was selected (e.g. case closed before
        Gate 2) so the caller can skip the section entirely rather than
        emit an empty heading.

        Each line is its own paragraph (joined with blank lines) so
        react-markdown renders them with visible separation rather than
        collapsing them into a single run of text.
        """
        ps = case.path_selection
        if ps is None:
            return None

        path_label = self._PATH_LABELS.get(ps.path, ps.path.value)
        paragraphs: List[str] = [
            "## Investigation Path",
            f"**{path_label}**",
        ]
        if ps.rationale:
            paragraphs.append(ps.rationale)
        paragraphs.append(
            "_Auto-selected by the system._"
            if ps.auto_selected
            else "_Chosen by the user._"
        )

        if ps.path == InvestigationPath.MITIGATION_FIRST:
            if ps.rca_after_mitigation_confirmed:
                paragraphs.append(
                    "_Post-mitigation: user continued to root-cause analysis._"
                )
            elif ps.mitigation_completed_at_turn is not None:
                paragraphs.append(
                    "_Post-mitigation: case did not continue to root-cause analysis._"
                )

        # Trailing newline so the caller's "\n".join produces \n\n
        # between this section and the next heading.
        return "\n\n".join(paragraphs) + "\n"

    def _format_closure_reason_label(self, reason: Optional[str]) -> str:
        """Map a closure_reason enum string to a human label."""
        if not reason:
            return "Not specified"
        return self._CLOSURE_REASON_LABELS.get(reason, reason)

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

    async def _generate_resolution_summary(
        self, case: Case, context: Dict[str, Any]
    ) -> str:
        """Generate resolution summary for RESOLVED cases.

        Content structure per investigation-lifecycle-logic.md §1.7.4:
        - Problem Statement
        - Investigation Path (from case.path_selection)
        - Root Cause (with mechanism if available)
        - Solution Applied
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
        solutions = case.solutions or []
        hypotheses = self._list_hypotheses(case)
        evidence_items = case.evidence or []

        parts = [
            f"# Resolution Summary: {title}\n",
            "## Problem Statement\n",
            f"{description}\n",
        ]

        # Investigation Path — surface the actual path_selection
        path_section = self._format_investigation_path_section(case)
        if path_section:
            parts.append(path_section)

        # Root Cause — prefer the authoritative root_cause_conclusion,
        # fall back to validated hypotheses only when no conclusion exists.
        rcc = case.root_cause_conclusion
        if rcc and rcc.root_cause:
            parts.append("## Root Cause\n")
            parts.append(f"{rcc.root_cause}\n")
            if getattr(rcc, "mechanism", None):
                parts.append(f"**How it produced the symptom:** {rcc.mechanism}\n")
            if getattr(rcc, "contributing_factors", None):
                # Trailing \n on the bold label so the join produces a
                # blank line before the list — CommonMark requires this
                # for the bullets to render as a proper list.
                parts.append("**Contributing factors:**\n")
                for cf in rcc.contributing_factors:
                    parts.append(f"- {cf}")
                parts.append("")
        else:
            validated = [
                h
                for h in hypotheses
                if hasattr(h, "status")
                and hasattr(h.status, "value")
                and h.status.value == "validated"
            ]
            if validated:
                parts.append("## Root Cause\n")
                parts.append("_Identified via validated hypothesis._\n")
                for h in validated:
                    parts.append(f"**{h.statement}**\n")
                    if getattr(h, "rationale", None):
                        parts.append(f"{h.rationale}\n")
                # Last line already ends with \n; no extra separator needed.

        # Solution Applied — same renderer as closure's Mitigation Status.
        if solutions:
            parts.append("## Solution Applied\n")
            for i, sol in enumerate(solutions, 1):
                parts.extend(self._format_solution_block(sol, i))
            # _format_solution_block's last line ends with \n; no extra separator needed.

        # Confirming Evidence — cite the evidence that grounded the conclusion,
        # not a bare count. Prefer the explicit evidence_basis on the
        # root cause conclusion; otherwise show evidence tagged with the
        # claim-anchored categories that prove cause or solution.
        cited_ids: List[str] = []
        if rcc and getattr(rcc, "evidence_basis", None):
            cited_ids = list(rcc.evidence_basis)

        if cited_ids:
            ev_by_id = {getattr(ev, "evidence_id", ""): ev for ev in evidence_items}
            cited = [ev_by_id[i] for i in cited_ids if i in ev_by_id]
        else:
            cited = [
                ev
                for ev in evidence_items
                if hasattr(getattr(ev, "category", None), "value")
                and ev.category.value in ("causal_evidence", "solution_evidence")
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
                status = h.status.value if hasattr(h.status, "value") else str(h.status)
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

        Content structure per investigation-lifecycle-logic.md §1.7.4:
        - Problem Statement
        - Investigation Path (from case.path_selection)
        - Investigation State (milestone/evidence/hypothesis counts)
        - Closure Reason (with human label)
        - Leading Hypotheses (top 5 by confidence)
        - Mitigation Status (mitigation-first path only)
        - Timeline
        - Recommendation (for closed_after_investigation only)
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

        # Investigation Path — same as resolution; especially informative
        # for closures (shows whether mitigation-first was chosen).
        path_section = self._format_investigation_path_section(case)
        if path_section:
            parts.append(path_section)

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
                    h.status.value if hasattr(h.status, "value") else str(h.status)
                )
                parts.append(
                    f"- **{h.statement}** — {status_str} "
                    f"(confidence: {getattr(h, 'likelihood', 0):.0%})"
                )
            parts.append("")

        # Mitigation Status — only meaningful when the path actually
        # routed through mitigation. For inquiry_only and
        # closed_after_investigation, this section would mislead.
        path_is_mitigation = (
            case.path_selection is not None
            and case.path_selection.path == InvestigationPath.MITIGATION_FIRST
        )
        if solutions and path_is_mitigation:
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
        if closure_reason_raw == "closed_after_investigation":
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
            "status": case.status.value,
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
        valid_states = [CaseStatus.RESOLVED, CaseStatus.CLOSED]

        if case.status not in valid_states:
            raise ValidationException(
                "invalid_case_state",
                f"Cannot generate reports from {case.status.value} state. Case must be resolved or closed first.",
            )
