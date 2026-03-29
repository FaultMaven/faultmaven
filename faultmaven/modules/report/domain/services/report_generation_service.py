"""
Report Generation Service - LLM-Based Case Documentation

Generates professional documentation for resolved troubleshooting cases:
1. Incident Report: Timeline, root cause, resolution, recommendations
2. Runbook: Step-by-step reproduction and resolution procedures
3. Post-Mortem: Comprehensive retrospective with lessons learned

Architecture Reference: docs/architecture/document-generation-and-closure-design.md
"""

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from faultmaven.exceptions import ValidationException
from faultmaven.infrastructure.concurrency import (
    LockAcquisitionError,
    ReportLockManager,
)
from faultmaven.infrastructure.knowledge.runbook_kb import RunbookKnowledgeBase
from faultmaven.infrastructure.observability.tracing import trace

# Cross-module imports via contracts (Principle 2: Vertical Modules with Contracts)
from faultmaven.modules.case.contracts import (  # Report models - now owned by Case module
    Case,
    CaseReport,
    CaseStatus,
    ICaseRepository,
    ReportGenerationRequest,
    ReportGenerationResponse,
    ReportStatus,
    ReportType,
    RunbookMetadata,
    RunbookSource,
)

# Backward compatibility re-export (imported from case.contracts now)
from faultmaven.modules.report.domain.models import (
    CaseReport,
    ReportGenerationRequest,
    ReportGenerationResponse,
    ReportStatus,
    ReportType,
    RunbookMetadata,
    RunbookSource,
)
from faultmaven.utils.serialization import to_json_compatible

logger = logging.getLogger(__name__)


class ReportGenerationService:
    """
    Generate professional case documentation using LLM.

    Key Features:
    - Three report types: Incident Report, Runbook, Post-Mortem
    - LLM-based generation from case context
    - PII sanitization before storage
    - Report versioning (up to 5 regenerations per type)
    - Automatic runbook indexing for similarity search
    """

    MAX_REGENERATIONS = 5
    GENERATION_TIMEOUT_SECONDS = 30

    def __init__(
        self,
        llm_router: Any,  # LLMRouter for generation
        case_repository: Optional[ICaseRepository] = None,
        runbook_kb: Optional[RunbookKnowledgeBase] = None,
        lock_manager: Optional[ReportLockManager] = None,
        pii_redactor: Optional[Any] = None,
    ):
        """
        Initialize report generation service.

        Args:
            llm_router: LLM router for text generation
            case_repository: Case repository for report persistence (TD-001: migrated from IReportStore)
            runbook_kb: Optional RunbookKB for auto-indexing runbooks
            lock_manager: Optional lock manager for concurrency control
            pii_redactor: Optional PII redactor for sanitization
        """
        self.llm_router = llm_router
        self.case_repository = case_repository
        self.runbook_kb = runbook_kb
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

                # Optional: Auto-index runbook in User KB for similarity search (separate from storage)
                if report_type == ReportType.RUNBOOK and self.runbook_kb:
                    await self._index_generated_runbook(report, case)

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
        """Generate a single report using LLM."""
        start_time = time.time()

        # Extract case context
        context = self._extract_case_context(case)

        # Generate report content using LLM
        if report_type == ReportType.INCIDENT_REPORT:
            content = await self._generate_incident_report(case, context)
            title = f"Incident Report: {case.title}"
        elif report_type == ReportType.RUNBOOK:
            content = await self._generate_runbook(case, context)
            title = f"Runbook: {case.title}"
        elif report_type == ReportType.POST_MORTEM:
            content = await self._generate_post_mortem(case, context)
            title = f"Post-Mortem: {case.title}"
        else:
            raise ValidationException(
                "invalid_report_type", f"Unknown report type: {report_type}"
            )

        # Sanitize PII if redactor available
        if self.pii_redactor:
            content = await self.pii_redactor.redact(content)

        generation_time_ms = int((time.time() - start_time) * 1000)

        # Create report metadata for runbooks
        metadata = None
        if report_type == ReportType.RUNBOOK:
            metadata = RunbookMetadata(
                source=RunbookSource.INCIDENT_DRIVEN,
                domain=getattr(case, "domain", "general"),
                tags=getattr(case, "tags", []),
                case_context=context,
                llm_model="gpt-4",  # TODO: Get from llm_router
            )

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
            metadata=metadata,
        )

    async def _generate_incident_report(
        self, case: Case, context: Dict[str, Any]
    ) -> str:
        """Generate incident report from case data.

        Builds a structured report directly from case fields.
        When LLM integration is available, this can be enhanced to use
        LLM for natural language generation from the same data.
        """
        title = case.title or "Untitled Case"
        description = case.description or "No description provided."
        status = case.status.value.replace("_", " ").title()
        created = to_json_compatible(case.created_at) if case.created_at else "Unknown"
        resolved = context.get("resolved_at", "In progress")
        duration = context.get("duration", "Unknown")
        closure = getattr(case, "closure_reason", None) or ""

        # Build evidence summary
        evidence_items = case.evidence if case.evidence else []
        evidence_count = len(evidence_items)

        # Build hypothesis summary
        hypotheses = case.hypotheses if case.hypotheses else []
        hypothesis_count = len(hypotheses)

        # Build solutions summary
        solutions = case.solutions if case.solutions else []

        parts = [
            f"# Incident Report: {title}\n",
            f"## Summary\n",
            f"- **Status:** {status}",
            f"- **Created:** {created}",
            f"- **Resolved:** {resolved}",
            f"- **Duration:** {duration}",
            f"- **Evidence collected:** {evidence_count} item{'s' if evidence_count != 1 else ''}",
            f"- **Hypotheses explored:** {hypothesis_count}",
            f"- **Solutions proposed:** {len(solutions)}\n",
            f"## Problem Description\n",
            f"{description}\n",
        ]

        if closure:
            parts.append(f"## Resolution\n")
            parts.append(f"{closure}\n")

        if solutions:
            parts.append("## Solutions Applied\n")
            for i, sol in enumerate(solutions, 1):
                sol_title = getattr(sol, "title", f"Solution {i}")
                sol_desc = getattr(sol, "description", "")
                parts.append(f"### {i}. {sol_title}\n")
                if sol_desc:
                    parts.append(f"{sol_desc}\n")

        if hypotheses:
            parts.append("## Investigation Summary\n")
            for h in hypotheses:
                h_title = getattr(h, "title", "")
                h_status = getattr(h, "status", "")
                h_conf = getattr(h, "confidence", 0)
                status_str = (
                    h_status.value if hasattr(h_status, "value") else str(h_status)
                )
                parts.append(
                    f"- **{h_title}** — {status_str} (confidence: {h_conf:.0%})"
                )
            parts.append("")

        return "\n".join(parts)

    async def _generate_runbook(self, case: Case, context: Dict[str, Any]) -> str:
        """Generate runbook using LLM.

        DEPRECATED: This method uses a non-canonical template that does not match
        the runbook content architecture (runbook-content-architecture.md).
        New runbook generation should use ConversionService.convert_from_case()
        which produces drafts with the canonical template (Problem Definition,
        Diagnostic Steps, Mitigation, Root Cause Resolution, Verification,
        Prevention, Sources) and YAML frontmatter.

        This method is retained for backward compatibility with the Report tab's
        existing generation flow. It will be removed once the Runbook tab
        (which uses convert_from_case) is the primary path.
        """
        prompt = f"""Generate a step-by-step operational runbook for the following incident.

**Incident:** {case.title}
**Problem:** {case.description or 'N/A'}
**Root Cause:** {context.get('root_cause', 'Not determined')}
**Solution:** {context.get('resolution_steps', 'N/A')}

Generate a detailed runbook in Markdown format with the following sections:
1. Problem Description (symptoms, error messages, impact)
2. Prerequisites (required access, tools, knowledge)
3. Diagnosis Steps (how to confirm this is the same issue)
4. Resolution Procedure (step-by-step fix instructions)
5. Validation Steps (how to verify the fix worked)
6. Rollback Procedure (if resolution doesn't work)
7. Related Issues (similar problems to watch for)

Make it actionable - someone should be able to follow this runbook without prior knowledge of the incident."""

        response = await self._call_llm(prompt, max_tokens=2500)
        return response

    async def _generate_post_mortem(self, case: Case, context: Dict[str, Any]) -> str:
        """Generate post-mortem using LLM."""
        prompt = f"""Generate a comprehensive post-mortem analysis for the following incident.

**Incident:** {case.title}
**Duration:** {context.get('duration', 'Unknown')}
**Impact:** {context.get('impact', 'See problem description')}
**Root Cause:** {context.get('root_cause', 'Not fully determined')}

Generate a thorough post-mortem in Markdown format with the following sections:
1. Incident Summary (what happened, when, impact)
2. Timeline (detailed sequence of events and actions taken)
3. Root Cause Analysis (why it happened, contributing factors)
4. What Went Well (positive aspects of response)
5. What Went Wrong (gaps, delays, miscommunications)
6. Action Items (specific improvements with owners and deadlines)
7. Lessons Learned (key takeaways for the team)
8. Related Work (links to similar incidents, documentation updates)

Be honest, blameless, and focused on learning. This is for team improvement."""

        response = await self._call_llm(prompt, max_tokens=3000)
        return response

    async def _call_llm(self, prompt: str, max_tokens: int = 2000) -> str:
        """
        Call LLM for text generation.

        In production, this would use the LLMRouter with proper error handling,
        retries, and fallback providers.
        """
        # TODO: Implement proper LLM router integration
        # For now, return a template-based mock response
        return self._generate_template_fallback(prompt)

    def _generate_template_fallback(self, prompt: str) -> str:
        """
        Generate template-based fallback when LLM unavailable.

        This ensures reports are always generated even if LLM fails.
        """
        if "incident report" in prompt.lower():
            return """# Incident Report

## Executive Summary
This incident report was auto-generated from case investigation data.

## Problem Description
See case description and timeline for details.

## Timeline of Events
- Case opened
- Investigation conducted
- Issue resolved

## Root Cause Analysis
Root cause analysis is available in the case investigation state.

## Resolution Steps
Resolution steps documented in case resolution.

## Recommendations
Review case context for specific recommendations."""

        elif "runbook" in prompt.lower():
            return """# Operational Runbook

## Problem Description
See case for symptom details.

## Prerequisites
- System access
- Diagnostic tools

## Diagnosis Steps
1. Check system status
2. Review error logs
3. Verify symptoms match case description

## Resolution Procedure
See case resolution for detailed steps.

## Validation Steps
1. Verify issue resolved
2. Monitor for recurrence

## Rollback Procedure
Documented in case if applicable."""

        elif "post-mortem" in prompt.lower():
            return """# Post-Mortem Analysis

## Incident Summary
Post-mortem generated from case investigation.

## Timeline
See case timeline for detailed sequence.

## Root Cause Analysis
Root cause documented in case resolution.

## What Went Well
- Issue identified and resolved
- Documentation created

## What Went Wrong
See case notes for areas of improvement.

## Action Items
- Review case recommendations
- Update procedures as needed

## Lessons Learned
Key learnings available in case context."""

        return "# Report\n\nReport content generated from case data."

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
        if case.resolved_at and case.created_at:
            duration = (case.resolved_at - case.created_at).total_seconds()
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

    async def _index_generated_runbook(self, report: CaseReport, case: Case) -> None:
        """Auto-index generated runbook for similarity search."""
        if not self.runbook_kb:
            return

        try:
            await self.runbook_kb.index_runbook(
                runbook=report,
                source=RunbookSource.INCIDENT_DRIVEN,
                case_title=case.title,
                domain=getattr(case, "domain", "general"),
                tags=case.tags,
            )
            logger.info(
                f"Runbook indexed for similarity search",
                extra={"case_id": case.case_id, "report_id": report.report_id},
            )
        except Exception as e:
            # Don't fail report generation if indexing fails
            logger.warning(
                f"Failed to index runbook: {e}", extra={"case_id": case.case_id}
            )
