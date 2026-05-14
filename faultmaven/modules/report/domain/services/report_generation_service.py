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

# Cross-module imports via contracts (Principle 2: Vertical Modules with Contracts)
from faultmaven.modules.case.contracts import (  # Report models - owned by Case module
    Case,
    CaseReport,
    CaseStatus,
    ICaseRepository,
    ReportGenerationRequest,
    ReportGenerationResponse,
    ReportStatus,
    ReportType,
)

# Backward compatibility re-export (imported from case.contracts now)
from faultmaven.modules.case.contracts import (
    CaseReport,
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
        # Legacy kwargs accepted for backward compatibility with existing DI wiring
        **kwargs: Any,
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

    async def _generate_resolution_summary(
        self, case: Case, context: Dict[str, Any]
    ) -> str:
        """Generate resolution summary for RESOLVED cases.

        Content structure per investigation-lifecycle-logic.md §1.7.4:
        - Problem Statement
        - Root Cause
        - Solution Applied
        - Confirming Evidence
        - Timeline
        - Milestones Reached
        - Investigation Path
        """
        title = case.title or "Untitled Case"
        description = case.description or "No description provided."
        created = to_json_compatible(case.created_at) if case.created_at else "Unknown"
        resolved = (
            to_json_compatible(case.resolved_at) if case.resolved_at else "Unknown"
        )
        duration = context.get("duration", "Unknown")
        solutions = case.solutions if case.solutions else []
        # case.hypotheses is Dict[str, Hypothesis] — iterate values, not keys
        hypotheses = (
            list(case.hypotheses.values())
            if isinstance(case.hypotheses, dict)
            else (case.hypotheses or [])
        )
        evidence_items = case.evidence if case.evidence else []
        milestones = (
            case.progress.completed_milestones
            if hasattr(case, "progress") and case.progress
            else []
        )

        parts = [
            f"# Resolution Summary: {title}\n",
            f"## Problem Statement\n",
            f"{description}\n",
        ]

        # Root Cause — from root_cause_conclusion, working_conclusion, or validated hypotheses
        root_cause_text = None
        if case.root_cause_conclusion and getattr(
            case.root_cause_conclusion, "root_cause", None
        ):
            root_cause_text = case.root_cause_conclusion.root_cause
        elif case.working_conclusion and getattr(
            case.working_conclusion, "statement", None
        ):
            root_cause_text = case.working_conclusion.statement

        if root_cause_text:
            parts.append("## Root Cause\n")
            parts.append(f"{root_cause_text}\n")
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
                for h in validated:
                    h_statement = getattr(h, "statement", "") or getattr(h, "title", "")
                    h_desc = getattr(h, "description", "") or getattr(
                        h, "reasoning", ""
                    )
                    parts.append(f"**{h_statement}**")
                    if h_desc:
                        parts.append(f"{h_desc}")
                parts.append("")

        # Solution Applied
        if solutions:
            parts.append("## Solution Applied\n")
            for i, sol in enumerate(solutions, 1):
                sol_title = getattr(sol, "title", f"Solution {i}")
                # Skip titles containing raw enum references
                if "SolutionType." in sol_title:
                    sol_type = getattr(sol, "solution_type", None)
                    type_label = (
                        sol_type.value.replace("_", " ").title()
                        if hasattr(sol_type, "value")
                        else f"Solution {i}"
                    )
                    sol_title = type_label
                sol_longterm = getattr(sol, "longterm_fix", None)
                sol_immediate = getattr(sol, "immediate_action", None)
                sol_desc = sol_longterm or sol_immediate or ""
                parts.append(f"**{i}. {sol_title}**")
                if sol_desc:
                    parts.append(f"{sol_desc}")
            parts.append("")

        # Confirming Evidence
        if evidence_items:
            parts.append("## Confirming Evidence\n")
            parts.append(
                f"{len(evidence_items)} evidence item{'s' if len(evidence_items) != 1 else ''} collected during investigation.\n"
            )

        # Timeline
        parts.append("## Timeline\n")
        parts.append(f"- **Created:** {created}")
        parts.append(f"- **Resolved:** {resolved}")
        parts.append(f"- **Duration:** {duration}")
        parts.append(f"- **Turns:** {getattr(case, 'current_turn', 0)}\n")

        # Milestones Reached
        if milestones:
            parts.append("## Milestones Reached\n")
            for m in milestones:
                parts.append(f"- {m.replace('_', ' ').title()}")
            parts.append("")

        # Investigation Summary
        if hypotheses:
            parts.append("## Investigation Path\n")
            for h in hypotheses:
                h_statement = getattr(h, "statement", "") or getattr(h, "title", "")
                h_status = getattr(h, "status", "")
                h_likelihood = getattr(h, "likelihood", 0) or getattr(
                    h, "confidence", 0
                )
                status_str = (
                    h_status.value if hasattr(h_status, "value") else str(h_status)
                )
                parts.append(
                    f"- **{h_statement}** — {status_str} (confidence: {h_likelihood:.0%})"
                )
            parts.append("")

        return "\n".join(parts)

    async def _generate_closure_summary(
        self, case: Case, context: Dict[str, Any]
    ) -> str:
        """Generate closure summary for CLOSED cases.

        Content structure per investigation-lifecycle-logic.md §1.7.4:
        - Problem Statement
        - Investigation State
        - Approaches Attempted
        - Closure Reason
        - Leading Hypotheses
        - Mitigation Status
        - Timeline
        - Recommendation
        """
        title = case.title or "Untitled Case"
        description = case.description or "No description provided."
        created = to_json_compatible(case.created_at) if case.created_at else "Unknown"
        closed = to_json_compatible(case.closed_at) if case.closed_at else "Unknown"
        duration = context.get("duration", "Unknown")
        closure_reason = getattr(case, "closure_reason", None) or "Not specified"

        # case.hypotheses is Dict[str, Hypothesis] — iterate values, not keys
        hypotheses = (
            list(case.hypotheses.values())
            if isinstance(case.hypotheses, dict)
            else (case.hypotheses or [])
        )
        evidence_items = case.evidence if case.evidence else []
        solutions = case.solutions if case.solutions else []
        milestones = (
            case.progress.completed_milestones
            if hasattr(case, "progress") and case.progress
            else []
        )

        parts = [
            f"# Closure Summary: {title}\n",
            f"## Problem Statement\n",
            f"{description}\n",
        ]

        # Investigation State — how far diagnosis progressed
        parts.append("## Investigation State\n")
        if milestones:
            parts.append(
                f"{len(milestones)} milestone{'s' if len(milestones) != 1 else ''} reached: "
                + ", ".join(m.replace("_", " ") for m in milestones)
                + "."
            )
        else:
            parts.append("No investigation milestones were reached.")
        parts.append(
            f"\n{len(evidence_items)} evidence item{'s' if len(evidence_items) != 1 else ''} collected. "
            f"{len(hypotheses)} hypothesis{'es' if len(hypotheses) != 1 else ''} explored.\n"
        )

        # Closure Reason
        parts.append("## Closure Reason\n")
        parts.append(f"{closure_reason}\n")

        # Leading Hypotheses — top hypotheses at time of closure
        if hypotheses:
            parts.append("## Leading Hypotheses\n")
            sorted_hyps = sorted(
                hypotheses,
                key=lambda h: getattr(h, "likelihood", 0),
                reverse=True,
            )
            for h in sorted_hyps[:5]:  # Top 5
                h_statement = getattr(h, "statement", "") or getattr(h, "title", "")
                h_status = getattr(h, "status", "")
                h_likelihood = getattr(h, "likelihood", 0)
                status_str = (
                    h_status.value if hasattr(h_status, "value") else str(h_status)
                )
                parts.append(
                    f"- **{h_statement}** — {status_str} (confidence: {h_likelihood:.0%})"
                )
            parts.append("")

        # Mitigation Status
        if solutions:
            parts.append("## Mitigation Status\n")
            for i, sol in enumerate(solutions, 1):
                sol_title = getattr(sol, "title", f"Solution {i}")
                if "SolutionType." in sol_title:
                    sol_type = getattr(sol, "solution_type", None)
                    sol_title = (
                        sol_type.value.replace("_", " ").title()
                        if hasattr(sol_type, "value")
                        else f"Solution {i}"
                    )
                sol_longterm = getattr(sol, "longterm_fix", None)
                sol_immediate = getattr(sol, "immediate_action", None)
                sol_desc = sol_longterm or sol_immediate or ""
                parts.append(f"**{i}. {sol_title}**")
                if sol_desc:
                    parts.append(f"{sol_desc}")
            parts.append("")

        # Timeline
        parts.append("## Timeline\n")
        parts.append(f"- **Created:** {created}")
        parts.append(f"- **Closed:** {closed}")
        parts.append(f"- **Duration:** {duration}")
        parts.append(f"- **Turns:** {getattr(case, 'current_turn', 0)}\n")

        # Recommendation — especially for escalated cases
        if closure_reason.lower() in ("escalated", "abandoned"):
            parts.append("## Recommendation\n")
            if hypotheses:
                top_hyp = sorted(
                    hypotheses,
                    key=lambda h: getattr(h, "likelihood", 0),
                    reverse=True,
                )[0]
                top_statement = getattr(top_hyp, "statement", "") or getattr(
                    top_hyp, "title", "Unknown"
                )
                parts.append(
                    f"The most promising lead at time of closure was: **{top_statement}**. "
                    f"A follow-up investigation should start there.\n"
                )
            else:
                parts.append(
                    "No hypotheses were formulated. A fresh investigation may be needed.\n"
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
