"""
Report Generation Service - LLM-Based Case Documentation

Generates professional documentation for resolved troubleshooting cases:
1. Incident Report: Timeline, root cause, resolution, recommendations
2. Runbook: Step-by-step reproduction and resolution procedures
3. Post-Mortem: Comprehensive retrospective with lessons learned

Architecture Reference: docs/architecture/document-generation-and-closure-design.md
"""

import time
from typing import List, Optional, Dict, Any
from datetime import datetime, timezone, timedelta
import logging

from faultmaven.services.base import BaseService
from faultmaven.models.report import (
    CaseReport,
    ReportType,
    ReportStatus,
    RunbookSource,
    RunbookMetadata,
    ReportGenerationRequest,
    ReportGenerationResponse
)
from faultmaven.models.case import Case, CaseStatus
from faultmaven.models.interfaces_report import IReportStore
from faultmaven.infrastructure.knowledge.runbook_kb import RunbookKnowledgeBase
from faultmaven.infrastructure.concurrency import ReportLockManager, LockAcquisitionError
from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.exceptions import ValidationException
from faultmaven.utils.serialization import to_json_compatible


logger = logging.getLogger(__name__)


class ReportGenerationService(BaseService):
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
        report_store: Optional[IReportStore] = None,
        runbook_kb: Optional[RunbookKnowledgeBase] = None,
        lock_manager: Optional[ReportLockManager] = None,
        pii_redactor: Optional[Any] = None
    ):
        """
        Initialize report generation service.

        Args:
            llm_router: LLM router for text generation
            report_store: Report storage interface for persistence
            runbook_kb: Optional RunbookKB for auto-indexing runbooks
            lock_manager: Optional lock manager for concurrency control
            pii_redactor: Optional PII redactor for sanitization
        """
        super().__init__("report_generation_service")
        self.llm_router = llm_router
        self.report_store = report_store
        self.runbook_kb = runbook_kb
        self.lock_manager = lock_manager
        self.pii_redactor = pii_redactor

    @trace("generate_reports")
    async def generate_reports(
        self,
        case: Case,
        report_types: List[ReportType]
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

        # Check regeneration limit
        if case.report_generation_count >= case.max_report_regenerations:
            raise ValidationException(
                "regeneration_limit_exceeded",
                f"Maximum {case.max_report_regenerations} regenerations allowed"
            )

        logger.info(
            f"Generating {len(report_types)} reports for case",
            extra={"case_id": case.case_id, "types": [t.value for t in report_types]}
        )

        # Acquire lock if lock_manager available (prevents concurrent report generation)
        if self.lock_manager:
            async with self.lock_manager.lock(case.case_id, wait_timeout=30):
                logger.debug(
                    f"Acquired report generation lock for case {case.case_id}"
                )
                return await self._generate_reports_locked(case, report_types)
        else:
            # No lock manager - proceed without concurrency protection
            logger.warning(
                "No lock manager available - proceeding without concurrency protection"
            )
            return await self._generate_reports_locked(case, report_types)

    async def _generate_reports_locked(
        self,
        case: Case,
        report_types: List[ReportType]
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

                # Persist report to storage
                if self.report_store:
                    await self.report_store.save_report(report)
                    logger.info(
                        f"Report persisted to storage",
                        extra={"report_id": report.report_id, "case_id": case.case_id}
                    )

                reports.append(report)

                generation_time = int((time.time() - start_time) * 1000)
                logger.info(
                    f"Report generated successfully",
                    extra={
                        "case_id": case.case_id,
                        "report_type": report_type.value,
                        "generation_time_ms": generation_time
                    }
                )

                # Note: Runbook auto-indexing now happens in report_store.save_report()
                # via RunbookKnowledgeBase integration

            except Exception as e:
                logger.error(
                    f"Failed to generate {report_type.value} report: {e}",
                    extra={"case_id": case.case_id},
                    exc_info=True
                )
                # Continue with other reports even if one fails
                continue

        if not reports:
            raise ValidationException(
                "report_generation_failed",
                "Failed to generate any reports"
            )

        # Calculate remaining regenerations
        remaining = case.max_report_regenerations - (case.report_generation_count + 1)

        return ReportGenerationResponse(
            case_id=case.case_id,
            reports=reports,
            remaining_regenerations=remaining
        )

    async def _generate_single_report(
        self,
        case: Case,
        report_type: ReportType
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
            raise ValidationException("invalid_report_type", f"Unknown report type: {report_type}")

        # Sanitize PII if redactor available
        if self.pii_redactor:
            content = await self.pii_redactor.redact(content)

        generation_time_ms = int((time.time() - start_time) * 1000)

        # Create report metadata for runbooks
        metadata = None
        if report_type == ReportType.RUNBOOK:
            metadata = RunbookMetadata(
                source=RunbookSource.INCIDENT_DRIVEN,
                domain=getattr(case, 'domain', 'general'),
                tags=case.tags,
                case_context=context,
                llm_model="gpt-4"  # TODO: Get from llm_router
            )

        return CaseReport(
            case_id=case.case_id,
            report_type=report_type,
            title=title,
            content=content,
            format="markdown",
            generation_status=ReportStatus.COMPLETED,
            generated_at=to_json_compatible(datetime.now(timezone.utc)),
            generation_time_ms=generation_time_ms,
            is_current=True,
            version=case.report_generation_count + 1,
            linked_to_closure=False,
            metadata=metadata
        )

    async def _generate_incident_report(
        self,
        case: Case,
        context: Dict[str, Any]
    ) -> str:
        """Generate incident report using LLM."""
        prompt = f"""Generate a professional incident report for the following troubleshooting case.

**Case Title:** {case.title}
**Description:** {case.description or 'N/A'}
**Status:** {case.status.value}
**Created:** {to_json_compatible(case.created_at)}
**Resolved:** {context.get('resolved_at', 'In progress')}

**Problem Summary:**
{context.get('problem_summary', 'See case description')}

**Timeline of Events:**
{context.get('timeline', 'N/A')}

**Root Cause:**
{context.get('root_cause', 'Not yet determined')}

**Resolution Steps:**
{context.get('resolution_steps', 'N/A')}

**Recommendations:**
{context.get('recommendations', 'None')}

Generate a structured incident report in Markdown format with the following sections:
1. Executive Summary
2. Problem Description
3. Timeline of Events
4. Root Cause Analysis
5. Resolution Steps
6. Impact Assessment
7. Recommendations for Prevention
8. Lessons Learned

Keep it professional, concise, and actionable. Focus on facts and outcomes."""

        # Call LLM (simplified - in production would use proper LLM router)
        response = await self._call_llm(prompt, max_tokens=2000)
        return response

    async def _generate_runbook(
        self,
        case: Case,
        context: Dict[str, Any]
    ) -> str:
        """Generate runbook using LLM."""
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

    async def _generate_post_mortem(
        self,
        case: Case,
        context: Dict[str, Any]
    ) -> str:
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
            "created_at": to_json_compatible(case.created_at) if case.created_at else None,
            "resolved_at": to_json_compatible(case.updated_at) if case.status == CaseStatus.RESOLVED else None,
            "duration": self._calculate_duration(case),
            "message_count": case.message_count,
            "tags": case.tags,
        }

        # Extract diagnostic state if available
        if hasattr(case, 'diagnostic_state') and case.diagnostic_state:
            diag = case.diagnostic_state
            context.update({
                "problem_summary": getattr(diag.anomaly_frame, 'statement', None) if hasattr(diag, 'anomaly_frame') else None,
                "root_cause": getattr(diag.root_cause, 'description', None) if hasattr(diag, 'root_cause') else None,
                "hypotheses_count": len(diag.hypotheses) if hasattr(diag, 'hypotheses') else 0,
            })

        return context

    def _calculate_duration(self, case: Case) -> str:
        """Calculate case duration in human-readable format."""
        if case.resolution_time_hours:
            hours = case.resolution_time_hours
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
        valid_states = [
            CaseStatus.RESOLVED,
            CaseStatus.SOLVED,
            CaseStatus.DOCUMENTING
        ]

        if case.status not in valid_states:
            raise ValidationException(
                "invalid_case_state",
                f"Cannot generate reports from {case.status.value} state. Case must be resolved first."
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
                domain=getattr(case, 'domain', 'general'),
                tags=case.tags
            )
            logger.info(
                f"Runbook indexed for similarity search",
                extra={"case_id": case.case_id, "report_id": report.report_id}
            )
        except Exception as e:
            # Don't fail report generation if indexing fails
            logger.warning(
                f"Failed to index runbook: {e}",
                extra={"case_id": case.case_id}
            )
