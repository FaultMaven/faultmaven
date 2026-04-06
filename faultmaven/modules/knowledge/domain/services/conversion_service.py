"""ConversionService: Orchestrates document-to-runbook conversion.

Pipeline:
1. Preprocess uploaded document (6 stages)
2. Analyze document for failure modes (LLM)
3. Convert each failure mode to a runbook (LLM, parallel or sequential)
4. Validate and score each draft
5. Persist drafts to disk and database
"""

import asyncio
import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from faultmaven.infrastructure.persistence.models import (
    ConversionDraftModel,
    ConversionJobModel,
)
from faultmaven.modules.knowledge.domain.models.conversion import (
    AnalysisResult,
    CaseConversionRequest,
    ConversionDraft,
    ConversionError,
    ConversionErrorCode,
    ConversionResponse,
    ConversionStatus,
    DraftStatus,
    DraftUpdateRequest,
    FailureModeAnalysis,
    QualityScore,
    SourceAssessment,
    SourceFileInfo,
    SourceType,
    ValidationResult,
    VerifyResponse,
    generate_conversion_id,
    generate_draft_id,
    generate_runbook_id,
)
from faultmaven.modules.knowledge.domain.services.document_preprocessor import (
    DocumentPreprocessor,
)
from faultmaven.modules.knowledge.domain.services.runbook_validator import (
    QualityScorer,
    RunbookValidator,
)

logger = logging.getLogger(__name__)

# Threshold for parallel vs sequential conversion
PARALLEL_THRESHOLD = 6

QUALITY_WARNING_THRESHOLD = 50.0

# =============================================================================
# LLM Prompts
# =============================================================================

ANALYSIS_SYSTEM_PROMPT = """You are an expert at analyzing technical documentation to identify distinct
failure modes. A failure mode is a specific way a system can fail, characterized
by unique symptoms, diagnostic procedures, and resolution steps.

Your task: Read the provided document and identify every distinct failure mode
it covers. For each failure mode, provide:
1. A short title (include the technology and failure type)
2. The symptoms or error messages associated with it
3. A brief summary of the resolution approach

Rules:
- If the document covers only ONE failure mode, return exactly one item.
- If the document is purely architectural/conceptual with no failure modes,
  return an empty list and set "is_actionable" to false.
- Do NOT invent failure modes not present in the source material.
- Failure modes must be distinct -- different symptoms OR different resolutions.

Respond with JSON matching this schema:
{
  "is_actionable": true/false,
  "failure_modes": [
    {
      "id": "kebab-case-id",
      "title": "Technology Failure Description",
      "domain": "database|networking|compute|application|security|storage|messaging",
      "service": "specific-service-name",
      "symptom_class": ["symptom_type_1", "symptom_type_2"],
      "severity": "critical|high|medium|low|info",
      "symptoms_summary": "Error messages and symptoms",
      "resolution_summary": "Brief resolution approach"
    }
  ],
  "source_assessment": {
    "content_type": "troubleshooting_guide|incident_report|postmortem|vendor_docs|other",
    "actionability_rating": "high|medium|low",
    "missing_information": ["list of missing info"]
  }
}"""

CONVERSION_SYSTEM_PROMPT = """You are a technical writer converting source material into a FaultMaven
runbook. You MUST produce output that exactly matches the template below.
Every section is required. Do not add sections. Do not rename sections.
Do not include commentary, explanations, or meta-text -- only the runbook.

TEMPLATE:
=========

---
id: {{id}}
title: "{{title}}"
domain: {{domain}}
service: {{service}}
symptom_class: [{{symptom_classes}}]
scope: {{scope}}
tags: [{{tags}}]
difficulty: intermediate
severity: {{severity}}
version: "1.0.0"
last_updated: "{{today_iso}}"
verified_by: ""
status: draft
---

# Runbook: {{title}}

## Problem Definition
- Exact alert names, error messages as they appear in logs, metric patterns.
- Be specific: include the actual strings a user would grep for.

## Diagnostic Steps

### Step 1: {{description}}
```{{language}}
{{command}}
```
{{interpretation guidance: what to look for, what findings mean}}

### Step 2: {{description}}
...

## Mitigation
**Risk**: {{what could go wrong}}
```{{language}}
{{mitigation command}}
```
**Verify**: {{how to confirm mitigation worked}}
**Duration**: {{how long the mitigation is safe}}

## Root Cause Resolution
**If** {{diagnostic finding from Step N}}:
```{{language}}
{{fix command}}
```

**If** {{alternative diagnostic finding}}:
...

## Verification
- {{specific metric or command to confirm the fix}}
- {{observation period}}
- {{what "back to normal" looks like}}

## Prevention
- {{configuration change to prevent recurrence}}
- {{monitoring alert to add}}
- {{process change}}

## Sources
- {{source_filename}} -- primary source document for this runbook

=========

RULES:
1. Every section MUST contain content. No empty sections.
2. Diagnostic Steps and Root Cause Resolution MUST contain fenced code blocks.
3. Root Cause Resolution MUST use "If X then Y" structure linking to findings
   from Diagnostic Steps.
4. Section sizes: aim for 400-900 characters per section so each fits within
   1-2 retrieval chunks.
5. If the source material does not provide enough information for a section,
   write "[INSUFFICIENT SOURCE DATA -- manual completion required]" and
   continue. Do not fabricate commands or procedures.
6. The Sources section MUST reference the uploaded filename as the primary
   source.
7. Use the taxonomy values provided in the failure mode analysis. Do not
   change domain, service, or symptom_class."""


# =============================================================================
# ConversionService
# =============================================================================


class ConversionService:
    """Orchestrates the document-to-runbook conversion pipeline."""

    def __init__(
        self,
        llm_router,
        settings,
        db_session_factory=None,
        knowledge_service=None,
    ):
        self._llm_router = llm_router
        self._settings = settings
        self._db_session_factory = db_session_factory
        self._knowledge_service = knowledge_service
        self._preprocessor = DocumentPreprocessor(llm_router, settings)
        self._validator = RunbookValidator()
        self._scorer = QualityScorer()
        self._scan_lock = asyncio.Lock()

    @property
    def _data_dir(self) -> Path:
        return Path("data/knowledge")

    def _scope_dir(self, scope: str, team_id: str = None, user_id: str = None) -> Path:
        if scope == "global":
            return self._data_dir / "global"
        elif scope == "team" and team_id:
            return self._data_dir / f"team_{team_id}"
        elif scope == "personal" and user_id:
            return self._data_dir / f"personal_{user_id}"
        return self._data_dir / "global"

    # =========================================================================
    # Main Conversion Pipeline
    # =========================================================================

    async def convert_document(
        self,
        file_path: Path,
        content_type: str,
        original_filename: str,
        scope: str,
        user_id: str,
        organization_id: str = None,
        team_id: str = None,
    ) -> ConversionResponse:
        """Full conversion pipeline: preprocess → analyze → convert → validate → persist."""
        # Step 0: Verify LLM provider is available
        try:
            knowledge_model = self._settings.llm.get_knowledge_model()
            if not knowledge_model:
                raise ConversionRejectedError(
                    "No LLM provider is configured. Set CHAT_PROVIDER in your .env file "
                    "or configure a provider in Dashboard > LLM Settings.",
                    error_code=ConversionErrorCode.LLM_UNAVAILABLE,
                )
        except AttributeError:
            raise ConversionRejectedError(
                "No LLM provider is configured. Set CHAT_PROVIDER in your .env file "
                "or configure a provider in Dashboard > LLM Settings.",
                error_code=ConversionErrorCode.LLM_UNAVAILABLE,
            )

        conversion_id = generate_conversion_id()
        created_at = datetime.now(timezone.utc)
        warnings: List[str] = []

        # Step 1: Preprocess
        logger.info(
            "document_conversion_started",
            extra={
                "conversion_id": conversion_id,
                "source_filename": original_filename,
                "content_type": content_type,
                "scope": scope,
            },
        )

        preprocessing = await self._preprocessor.preprocess(file_path, content_type)

        if preprocessing.is_rejected:
            raise ConversionRejectedError(
                preprocessing.rejection_reason or "Document rejected",
                error_code=preprocessing.error_code
                or ConversionErrorCode.NOT_ACTIONABLE,
            )

        warnings.extend(preprocessing.warnings)

        # Step 2: Source file information (files are NOT retained on disk, per architectural design)
        source_file = SourceFileInfo(
            filename=original_filename,
            size_bytes=file_path.stat().st_size,
            content_type=content_type,
            retained_path=None,
        )

        # Step 3: Analyze for failure modes
        analysis = await self._analyze_document(
            preprocessing.extracted_text, original_filename
        )

        if not analysis.is_actionable or len(analysis.failure_modes) == 0:
            raise ConversionRejectedError(
                "Source document does not contain actionable failure modes. "
                "Runbooks require specific symptoms, diagnostics, and resolution steps.",
                error_code=ConversionErrorCode.NO_FAILURE_MODES,
            )

        # Step 4: Convert each failure mode to a runbook
        drafts, errors = await self._convert_all_failure_modes(
            preprocessing.extracted_text,
            analysis.failure_modes,
            scope,
            original_filename,
            conversion_id,
            user_id,
            team_id,
        )

        if errors:
            for err in errors:
                warnings.append(
                    f"Failed to convert '{err.failure_mode_id}': {err.error}"
                )

        # Determine overall status
        if len(drafts) == 0:
            status = ConversionStatus.FAILED
        elif len(errors) > 0:
            status = ConversionStatus.PARTIAL
        else:
            status = ConversionStatus.COMPLETED

        # Step 5: Persist to database
        await self._persist_job(
            conversion_id=conversion_id,
            user_id=user_id,
            organization_id=organization_id,
            scope=scope,
            team_id=team_id,
            status=status,
            source_file=source_file,
            analysis=analysis,
            drafts=drafts,
            created_at=created_at,
        )

        logger.info(
            "document_conversion_completed",
            extra={
                "conversion_id": conversion_id,
                "failure_modes_detected": len(analysis.failure_modes),
                "drafts_generated": len(drafts),
                "drafts_passed_validation": sum(
                    1 for d in drafts if d.validation.passed
                ),
            },
        )

        return ConversionResponse(
            conversion_id=conversion_id,
            status=status,
            source_file=source_file,
            analysis=analysis,
            drafts=drafts,
            warnings=warnings,
            created_at=created_at,
        )

    # =========================================================================
    # Case-to-Runbook Conversion
    # =========================================================================

    async def convert_from_case(
        self,
        request: "CaseConversionRequest",
        user_id: str,
        organization_id: str = None,
        team_id: str = None,
    ) -> ConversionResponse:
        """Generate a runbook draft from a resolved case using the canonical template.

        Skips preprocessing and analysis (case data is already structured).
        Reuses _convert_single_failure_mode() for LLM generation, validation,
        scoring, and persistence — same pipeline as document-driven conversion.
        """
        # Verify LLM provider is available
        try:
            knowledge_model = self._settings.llm.get_knowledge_model()
            if not knowledge_model:
                raise ConversionRejectedError(
                    "No LLM provider is configured.",
                    error_code=ConversionErrorCode.LLM_UNAVAILABLE,
                )
        except AttributeError:
            raise ConversionRejectedError(
                "No LLM provider is configured.",
                error_code=ConversionErrorCode.LLM_UNAVAILABLE,
            )

        conversion_id = generate_conversion_id()
        created_at = datetime.now(timezone.utc)
        warnings: List[str] = []

        # Construct a FailureModeAnalysis from the case data
        failure_mode = FailureModeAnalysis(
            id=f"case-{request.case_id}",
            title=request.title,
            domain=request.domain,
            service=request.service,
            symptom_class=request.symptom_class or ["unknown"],
            severity=request.severity,
            symptoms_summary=request.description,
            resolution_summary=request.root_cause or "See solutions below",
        )

        # Assemble source material text from case context.
        # Each section maps to a Case domain model field — see CaseConversionRequest docstring.
        source_parts = [f"CASE TITLE: {request.title}"]
        if request.description:
            source_parts.append(f"PROBLEM: {request.description}")
        if request.root_cause:
            source_parts.append(f"ROOT CAUSE: {request.root_cause}")
        if request.root_cause_mechanism:
            source_parts.append(f"CAUSAL MECHANISM: {request.root_cause_mechanism}")
        if request.solutions:
            solutions_text = "\n\n".join(request.solutions)
            source_parts.append(f"SOLUTIONS APPLIED:\n{solutions_text}")
        if request.hypotheses_summary:
            source_parts.append(f"VALIDATED HYPOTHESES: {request.hypotheses_summary}")
        if request.evidence_summary:
            source_parts.append(f"KEY EVIDENCE:\n{request.evidence_summary}")

        # For mitigated cases: provide context so the LLM can write a proper
        # Root Cause Resolution section. The mitigation IS the permanent
        # resolution — framed using the standard "If X then Y" + code block
        # template structure, same as any other runbook.
        if request.is_mitigation_only:
            if request.rca_infeasible_rationale:
                source_parts.append(f"CONSTRAINT: {request.rca_infeasible_rationale}")
            if request.mitigation_actions:
                actions_text = "\n\n".join(request.mitigation_actions)
                source_parts.append(f"MITIGATION ACTIONS PERFORMED:\n{actions_text}")
            if not request.root_cause:
                # No formal root_cause_conclusion — guide the LLM to treat the
                # mitigation as the resolution, using the standard template.
                source_parts.append(
                    "NOTE: This case was closed with mitigation as the accepted "
                    "permanent strategy. For the Root Cause Resolution section, "
                    "use the standard 'If X then Y' structure where the "
                    "diagnostic finding is the identified constraint and the "
                    "resolution is the mitigation implementation with its "
                    "configuration. Include the code/commands used."
                )

        source_text = "\n\n".join(source_parts)

        source_filename = f"Case {request.case_id}"

        logger.info(
            "case_conversion_started",
            extra={
                "conversion_id": conversion_id,
                "case_id": request.case_id,
                "domain": request.domain,
                "service": request.service,
            },
        )

        # Convert using the same pipeline as document-driven
        draft_or_error = await self._convert_single_failure_mode(
            text=source_text,
            failure_mode=failure_mode,
            scope=request.scope,
            filename=source_filename,
            conversion_id=conversion_id,
            user_id=user_id,
            team_id=team_id,
        )

        drafts: List[ConversionDraft] = []
        if isinstance(draft_or_error, ConversionError):
            warnings.append(f"Conversion failed: {draft_or_error.error}")
            status = ConversionStatus.FAILED
        else:
            # Tag the draft with case source info
            draft_or_error.source_type = SourceType.CASE
            draft_or_error.case_id = request.case_id
            drafts.append(draft_or_error)
            status = ConversionStatus.COMPLETED

        # Build analysis result (single failure mode, always actionable)
        analysis = AnalysisResult(
            is_actionable=True,
            failure_modes=[failure_mode],
            source_assessment=SourceAssessment(
                content_type="resolved_case",
                actionability_rating="high",
                missing_information=[],
            ),
        )

        source_file = SourceFileInfo(
            filename=source_filename,
            size_bytes=len(source_text.encode("utf-8")),
            content_type="application/x-faultmaven-case",
            retained_path=None,
        )

        # Persist to database with source_type and case_id
        await self._persist_job(
            conversion_id=conversion_id,
            user_id=user_id,
            organization_id=organization_id,
            scope=request.scope,
            team_id=team_id,
            status=status,
            source_file=source_file,
            analysis=analysis,
            drafts=drafts,
            created_at=created_at,
            source_type="case",
            case_id=request.case_id,
        )

        logger.info(
            "case_conversion_completed",
            extra={
                "conversion_id": conversion_id,
                "case_id": request.case_id,
                "drafts_generated": len(drafts),
                "status": status.value,
            },
        )

        return ConversionResponse(
            conversion_id=conversion_id,
            status=status,
            source_file=source_file,
            analysis=analysis,
            drafts=drafts,
            warnings=warnings,
            created_at=created_at,
        )

    # =========================================================================
    # Analysis Phase
    # =========================================================================

    async def _analyze_document(self, text: str, filename: str) -> AnalysisResult:
        """Analyze document for failure modes using KNOWLEDGE_PROVIDER."""
        knowledge_model = self._settings.llm.get_knowledge_model()

        response = await self._llm_router.route(
            messages=[
                {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT},
                {"role": "user", "content": f"Analyze this document:\n\n{text}"},
            ],
            model=knowledge_model,
            max_tokens=2048,
            temperature=0.2,
            response_format={"type": "json_object"},
        )

        try:
            data = json.loads(response.content)
            return AnalysisResult(
                is_actionable=data.get("is_actionable", False),
                failure_modes=[
                    FailureModeAnalysis(**fm) for fm in data.get("failure_modes", [])
                ],
                source_assessment=SourceAssessment(
                    **data.get(
                        "source_assessment",
                        {
                            "content_type": "unknown",
                            "actionability_rating": "low",
                            "missing_information": [],
                        },
                    )
                ),
            )
        except Exception as e:
            logger.error(f"Failed to parse analysis response: {e}")
            raise ValueError(f"LLM analysis response could not be parsed: {e}")

    # =========================================================================
    # Conversion Phase
    # =========================================================================

    async def _convert_all_failure_modes(
        self,
        text: str,
        failure_modes: List[FailureModeAnalysis],
        scope: str,
        filename: str,
        conversion_id: str,
        user_id: str,
        team_id: str = None,
    ) -> Tuple[List[ConversionDraft], List[ConversionError]]:
        """Convert all failure modes, parallel for <=5, sequential for 6+."""
        drafts: List[ConversionDraft] = []
        errors: List[ConversionError] = []

        # Deduplicate by (service, symptom_class tuple)
        seen = set()
        unique_modes = []
        for fm in failure_modes:
            key = (fm.service, tuple(sorted(fm.symptom_class)))
            if key not in seen:
                seen.add(key)
                unique_modes.append(fm)

        if len(unique_modes) < PARALLEL_THRESHOLD:
            # Parallel conversion
            tasks = [
                self._convert_single_failure_mode(
                    text, fm, scope, filename, conversion_id, user_id, team_id
                )
                for fm in unique_modes
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    errors.append(
                        ConversionError(
                            failure_mode_id=unique_modes[i].id,
                            error=str(result),
                            retryable=False,
                        )
                    )
                elif isinstance(result, ConversionError):
                    errors.append(result)
                else:
                    drafts.append(result)
        else:
            # Sequential conversion (avoid rate limits)
            for fm in unique_modes:
                result = await self._convert_single_failure_mode(
                    text, fm, scope, filename, conversion_id, user_id, team_id
                )
                if isinstance(result, ConversionError):
                    errors.append(result)
                else:
                    drafts.append(result)

        return drafts, errors

    async def _convert_single_failure_mode(
        self,
        text: str,
        failure_mode: FailureModeAnalysis,
        scope: str,
        filename: str,
        conversion_id: str,
        user_id: str,
        team_id: str = None,
    ) -> ConversionDraft | ConversionError:
        """Convert a single failure mode to a runbook draft."""
        try:
            knowledge_model = self._settings.llm.get_knowledge_model()
            today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")

            user_message = (
                f"Convert the following source material into a runbook for this specific "
                f"failure mode:\n\n"
                f"FAILURE MODE: {failure_mode.title}\n"
                f"DOMAIN: {failure_mode.domain}\n"
                f"SERVICE: {failure_mode.service}\n"
                f"SYMPTOM_CLASS: {', '.join(failure_mode.symptom_class)}\n"
                f"SEVERITY: {failure_mode.severity}\n"
                f"SCOPE: {scope}\n"
                f"SOURCE FILENAME: {filename}\n"
                f"TODAY: {today_iso}\n\n"
                f"--- SOURCE MATERIAL ---\n{text}\n--- END SOURCE MATERIAL ---"
            )

            response = await self._llm_router.route(
                messages=[
                    {"role": "system", "content": CONVERSION_SYSTEM_PROMPT},
                    {"role": "user", "content": user_message},
                ],
                model=knowledge_model,
                max_tokens=4096,
                temperature=0.3,
            )

            runbook_content = response.content.strip()

            # Validate LLM output before writing to disk
            if not runbook_content or len(runbook_content) < 100:
                return ConversionError(
                    failure_mode_id=failure_mode.id,
                    error="LLM returned empty or too-short response",
                    retryable=True,
                )
            if "---" not in runbook_content:
                return ConversionError(
                    failure_mode_id=failure_mode.id,
                    error="LLM response missing frontmatter delimiters",
                    retryable=True,
                )
            if not any(
                h in runbook_content
                for h in [
                    "## Problem Definition",
                    "## Diagnostic Steps",
                    "## Mitigation",
                ]
            ):
                return ConversionError(
                    failure_mode_id=failure_mode.id,
                    error="LLM response missing required runbook sections",
                    retryable=True,
                )

            # Generate IDs
            draft_id = generate_draft_id()
            runbook_id = generate_runbook_id(failure_mode)

            # Write draft to disk
            scope_dir = self._scope_dir(scope, team_id, user_id)
            scope_dir.mkdir(parents=True, exist_ok=True)
            draft_path = scope_dir / f"{runbook_id}.md"
            draft_path.write_text(runbook_content, encoding="utf-8")

            # Validate
            validation = self._validator.validate_content(runbook_content)

            # Score quality
            quality = self._scorer.score_content(runbook_content)

            quality_warning = None
            if quality.overall < QUALITY_WARNING_THRESHOLD:
                quality_warning = (
                    "Quality score is below 50. The source material may lack sufficient "
                    "diagnostic commands, resolution steps, or verification procedures. "
                    "Manual editing is recommended before verification."
                )

            return ConversionDraft(
                draft_id=draft_id,
                runbook_id=runbook_id,
                title=failure_mode.title,
                scope=scope,
                status=DraftStatus.DRAFT,
                validation=validation,
                quality_score=quality,
                file_path=str(draft_path),
                content_preview=runbook_content[:500],
                content=runbook_content,
                quality_warning=quality_warning,
            )

        except Exception as e:
            logger.error(f"Conversion failed for {failure_mode.id}: {e}")
            return ConversionError(
                failure_mode_id=failure_mode.id,
                error=str(e),
                retryable=getattr(e, "retryable", False),
            )

    # =========================================================================
    # Persistence
    # =========================================================================

    async def _persist_job(
        self,
        conversion_id: str,
        user_id: str,
        organization_id: str,
        scope: str,
        team_id: str,
        status: ConversionStatus,
        source_file: SourceFileInfo,
        analysis: AnalysisResult,
        drafts: List[ConversionDraft],
        created_at: datetime,
        source_type: str = "document",
        case_id: str = None,
    ) -> None:
        """Persist conversion job and drafts to database."""
        if not self._db_session_factory:
            return

        async with self._db_session_factory() as session:
            job = ConversionJobModel(
                id=conversion_id,
                user_id=user_id,
                organization_id=organization_id,
                scope=scope,
                team_id=team_id,
                status=status.value,
                source_filename=source_file.filename,
                source_content_type=source_file.content_type,
                source_size_bytes=source_file.size_bytes,
                source_path=source_file.retained_path or "",
                source_type=source_type,
                case_id=case_id,
                failure_modes_detected=len(analysis.failure_modes),
                analysis_result=analysis.model_dump(),
                created_at=created_at,
                completed_at=datetime.now(timezone.utc),
            )
            session.add(job)

            for draft in drafts:
                draft_model = ConversionDraftModel(
                    id=draft.draft_id,
                    conversion_id=conversion_id,
                    runbook_id=draft.runbook_id,
                    title=draft.title,
                    file_path=draft.file_path,
                    status=draft.status.value,
                    source_type=source_type,
                    validation_passed=draft.validation.passed,
                    validation_errors=draft.validation.errors,
                    validation_warnings=draft.validation.warnings,
                    quality_score=draft.quality_score.overall,
                    quality_details=draft.quality_score.model_dump(),
                    created_at=created_at,
                )
                session.add(draft_model)

            await session.commit()

    # =========================================================================
    # Draft Management (Phase 2)
    # =========================================================================

    async def get_conversion(
        self, conversion_id: str, user_id: str
    ) -> Optional[ConversionResponse]:
        """Get conversion job with all drafts."""
        if not self._db_session_factory:
            return None

        async with self._db_session_factory() as session:
            # Allow access if user owns the job OR it was created by system
            result = await session.execute(
                select(ConversionJobModel).where(
                    ConversionJobModel.id == conversion_id,
                    (
                        (ConversionJobModel.user_id == user_id)
                        | (ConversionJobModel.user_id == "system")
                    ),
                )
            )
            job = result.scalar_one_or_none()
            if not job:
                return None

            draft_result = await session.execute(
                select(ConversionDraftModel).where(
                    ConversionDraftModel.conversion_id == conversion_id,
                    ConversionDraftModel.status != DraftStatus.DELETED.value,
                )
            )
            draft_models = draft_result.scalars().all()

            drafts = []
            for dm in draft_models:
                # Read content from disk
                content = None
                try:
                    content = Path(dm.file_path).read_text(encoding="utf-8")
                except Exception:
                    pass

                drafts.append(
                    ConversionDraft(
                        draft_id=dm.id,
                        runbook_id=dm.runbook_id,
                        title=dm.title,
                        scope=job.scope,
                        status=DraftStatus(dm.status),
                        source_type=SourceType(dm.source_type or "document"),
                        case_id=job.case_id,
                        validation=ValidationResult(
                            passed=dm.validation_passed,
                            errors=dm.validation_errors or [],
                            warnings=dm.validation_warnings or [],
                        ),
                        quality_score=QualityScore(**(dm.quality_details or {})),
                        file_path=dm.file_path,
                        content_preview=(content or "")[:500],
                        content=content,
                    )
                )

            analysis = AnalysisResult(
                **(
                    job.analysis_result
                    or {
                        "is_actionable": True,
                        "failure_modes": [],
                        "source_assessment": {
                            "content_type": "unknown",
                            "actionability_rating": "low",
                            "missing_information": [],
                        },
                    }
                )
            )

            return ConversionResponse(
                conversion_id=job.id,
                status=ConversionStatus(job.status),
                source_file=SourceFileInfo(
                    filename=job.source_filename,
                    size_bytes=job.source_size_bytes,
                    content_type=job.source_content_type,
                    retained_path=job.source_path,
                ),
                analysis=analysis,
                drafts=drafts,
                created_at=job.created_at,
            )

    async def get_conversion_by_case(
        self, case_id: str, user_id: str
    ) -> Optional[ConversionResponse]:
        """Get conversion job for a specific case."""
        if not self._db_session_factory:
            return None

        async with self._db_session_factory() as session:
            result = await session.execute(
                select(ConversionJobModel)
                .where(
                    ConversionJobModel.case_id == case_id,
                    (
                        (ConversionJobModel.user_id == user_id)
                        | (ConversionJobModel.user_id == "system")
                    ),
                )
                .order_by(ConversionJobModel.created_at.desc())
            )
            job = result.scalar_one_or_none()
            if not job:
                return None

            # Delegate to get_conversion for consistent draft loading
            return await self.get_conversion(job.id, user_id)

    async def list_conversions(
        self, user_id: str, limit: int = 20, offset: int = 0
    ) -> List[dict]:
        """List user's conversion jobs (summary, no draft content)."""
        if not self._db_session_factory:
            return []

        async with self._db_session_factory() as session:
            result = await session.execute(
                select(ConversionJobModel)
                .where(
                    (ConversionJobModel.user_id == user_id)
                    | (ConversionJobModel.user_id == "system")
                )
                .order_by(ConversionJobModel.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
            jobs = result.scalars().all()

            return [
                {
                    "conversion_id": job.id,
                    "status": job.status,
                    "source_filename": job.source_filename,
                    "failure_modes_detected": job.failure_modes_detected,
                    "scope": job.scope,
                    "created_at": (
                        job.created_at.isoformat() if job.created_at else None
                    ),
                }
                for job in jobs
            ]

    async def list_all_drafts(self, user_id: str) -> List[dict]:
        """List all non-deleted drafts the user can access.

        Returns drafts where:
        - User owns the conversion job (personal/team scope), OR
        - Draft scope is 'global' (visible to all users — global KB is shared)
        """
        if not self._db_session_factory:
            return []

        async with self._db_session_factory() as session:
            from sqlalchemy import or_

            result = await session.execute(
                select(ConversionDraftModel, ConversionJobModel)
                .join(
                    ConversionJobModel,
                    ConversionDraftModel.conversion_id == ConversionJobModel.id,
                )
                .where(
                    or_(
                        ConversionJobModel.user_id == user_id,
                        ConversionJobModel.scope == "global",
                    ),
                    ConversionDraftModel.status != DraftStatus.DELETED.value,
                )
                .order_by(ConversionDraftModel.created_at.desc())
            )
            rows = result.all()

            return [
                {
                    "conversion_id": job.id,
                    "draft_id": dm.id,
                    "runbook_id": dm.runbook_id,
                    "title": dm.title,
                    "scope": job.scope,
                    "status": dm.status,
                    "source_type": dm.source_type or "document",
                    "case_id": job.case_id,
                    "validation_passed": dm.validation_passed,
                    "quality_score": (
                        float(dm.quality_score) if dm.quality_score else None
                    ),
                    "quality_details": dm.quality_details,
                    "created_at": (
                        dm.created_at.isoformat() if dm.created_at else None
                    ),
                    "verified_at": (
                        dm.verified_at.isoformat() if dm.verified_at else None
                    ),
                }
                for dm, job in rows
            ]

    async def update_draft(
        self,
        conversion_id: str,
        draft_id: str,
        user_id: str,
        content: str,
    ) -> Optional[ConversionDraft]:
        """Update draft content, re-validate, and re-score."""
        if not self._db_session_factory:
            return None

        async with self._db_session_factory() as session:
            # Verify ownership (system-created jobs accessible to any user)
            job_result = await session.execute(
                select(ConversionJobModel).where(
                    ConversionJobModel.id == conversion_id,
                    (
                        (ConversionJobModel.user_id == user_id)
                        | (ConversionJobModel.user_id == "system")
                    ),
                )
            )
            job = job_result.scalar_one_or_none()
            if not job:
                return None

            draft_result = await session.execute(
                select(ConversionDraftModel).where(
                    ConversionDraftModel.id == draft_id,
                    ConversionDraftModel.conversion_id == conversion_id,
                )
            )
            dm = draft_result.scalar_one_or_none()
            if not dm or dm.status == DraftStatus.DELETED.value:
                return None

            # Write updated content to disk
            file_path = Path(dm.file_path)
            file_path.write_text(content, encoding="utf-8")

            # Re-validate and re-score
            validation = self._validator.validate_content(content)
            quality = self._scorer.score_content(content)

            # Update database
            dm.validation_passed = validation.passed
            dm.validation_errors = validation.errors
            dm.validation_warnings = validation.warnings
            dm.quality_score = quality.overall
            dm.quality_details = quality.model_dump()

            await session.commit()

            quality_warning = None
            if quality.overall < QUALITY_WARNING_THRESHOLD:
                quality_warning = (
                    "Quality score is below 50. Manual editing recommended."
                )

            return ConversionDraft(
                draft_id=dm.id,
                runbook_id=dm.runbook_id,
                title=dm.title,
                scope=job.scope,
                status=DraftStatus(dm.status),
                validation=validation,
                quality_score=quality,
                file_path=dm.file_path,
                content_preview=content[:500],
                content=content,
                quality_warning=quality_warning,
            )

    async def verify_batch(
        self,
        draft_refs: list[tuple[str, str]],  # (conversion_id, draft_id)
        user_id: str,
        username: str,
    ) -> dict:
        """Verify multiple drafts sequentially. Returns summary with per-item status."""
        results = []
        verified = 0
        failed = 0
        skipped = 0

        for conversion_id, draft_id in draft_refs:
            try:
                response = await self.verify_draft(
                    conversion_id=conversion_id,
                    draft_id=draft_id,
                    user_id=user_id,
                    username=username,
                )
                results.append(
                    {
                        "conversion_id": conversion_id,
                        "draft_id": draft_id,
                        "status": "verified",
                        "error": None,
                        "knowledge_item_id": (
                            response.knowledge_item_id if response else None
                        ),
                    }
                )
                verified += 1
            except ValueError as e:
                error_msg = str(e)
                if "already been verified" in error_msg:
                    results.append(
                        {
                            "conversion_id": conversion_id,
                            "draft_id": draft_id,
                            "status": "skipped",
                            "error": error_msg,
                            "knowledge_item_id": None,
                        }
                    )
                    skipped += 1
                else:
                    results.append(
                        {
                            "conversion_id": conversion_id,
                            "draft_id": draft_id,
                            "status": "failed",
                            "error": error_msg,
                            "knowledge_item_id": None,
                        }
                    )
                    failed += 1
            except Exception as e:
                logger.error(f"Batch verify failed for {draft_id}: {e}")
                results.append(
                    {
                        "conversion_id": conversion_id,
                        "draft_id": draft_id,
                        "status": "failed",
                        "error": str(e),
                        "knowledge_item_id": None,
                    }
                )
                failed += 1

        return {
            "total": len(draft_refs),
            "verified": verified,
            "failed": failed,
            "skipped": skipped,
            "results": results,
        }

    async def verify_draft(
        self,
        conversion_id: str,
        draft_id: str,
        user_id: str,
        username: str,
    ) -> Optional[VerifyResponse]:
        """Promote draft to verified status, update frontmatter, trigger ingestion."""
        if not self._db_session_factory:
            return None

        async with self._db_session_factory() as session:
            # Verify ownership (system-created jobs accessible to any user)
            job_result = await session.execute(
                select(ConversionJobModel).where(
                    ConversionJobModel.id == conversion_id,
                    (
                        (ConversionJobModel.user_id == user_id)
                        | (ConversionJobModel.user_id == "system")
                    ),
                )
            )
            job = job_result.scalar_one_or_none()
            if not job:
                raise ValueError("Conversion job not found")

            draft_result = await session.execute(
                select(ConversionDraftModel).where(
                    ConversionDraftModel.id == draft_id,
                    ConversionDraftModel.conversion_id == conversion_id,
                )
            )
            dm = draft_result.scalar_one_or_none()
            if not dm:
                raise ValueError("Draft not found")
            if dm.status == DraftStatus.VERIFIED.value:
                raise ValueError("This runbook has already been verified and ingested")
            if dm.status == DraftStatus.DELETED.value:
                raise ValueError("This draft has been deleted")
            if dm.status != DraftStatus.DRAFT.value:
                raise ValueError(f"Draft is in unexpected state: {dm.status}")

            if not dm.validation_passed:
                raise ValueError(
                    "Draft has validation errors that must be fixed before verification"
                )

            # Update frontmatter on disk using python-frontmatter
            file_path = Path(dm.file_path)
            try:
                import frontmatter

                post = frontmatter.load(str(file_path))
                post.metadata["status"] = "verified"
                post.metadata["verified_by"] = username
                frontmatter.dump(post, str(file_path))
            except Exception as e:
                logger.error(f"Failed to update frontmatter: {e}")
                # Fallback: just update the file content with regex
                content = file_path.read_text(encoding="utf-8")
                content = content.replace("status: draft", "status: verified", 1)
                content = content.replace(
                    'verified_by: ""', f'verified_by: "{username}"', 1
                )
                file_path.write_text(content, encoding="utf-8")

            # Update database
            now = datetime.now(timezone.utc)
            dm.status = DraftStatus.VERIFIED.value
            dm.verified_at = now
            dm.verified_by = user_id

            # Populate metadata from frontmatter
            content = file_path.read_text(encoding="utf-8")
            from faultmaven.utils.frontmatter import extract_frontmatter_metadata

            fm_meta = extract_frontmatter_metadata(content)
            dm.domain = fm_meta.get("domain")
            dm.service = fm_meta.get("service")
            dm.severity = fm_meta.get("severity")
            dm.document_type = "runbook"

            import re as _re

            import yaml

            fm_match = _re.match(r"^---\s*\n(.*?)\n---\s*\n", content, _re.DOTALL)
            if fm_match:
                try:
                    raw_fm = yaml.safe_load(fm_match.group(1)) or {}
                    raw_tags = raw_fm.get("tags", [])
                    if isinstance(raw_tags, list):
                        dm.tags = ", ".join(str(t) for t in raw_tags)
                    elif isinstance(raw_tags, str):
                        dm.tags = raw_tags
                except Exception:
                    pass

            # Ingest into ChromaDB (chunk + embed + store)
            knowledge_item_id = None
            chunks_created = 0
            collection = f"{job.scope}_kb"

            if self._knowledge_service:
                try:
                    import uuid as _uuid

                    knowledge_item_id = f"kb_{_uuid.uuid4().hex[:12]}"
                    chunks_created = (
                        await self._knowledge_service.ingest_to_vector_store(
                            document_id=knowledge_item_id,
                            title=dm.title,
                            content=content,
                            document_type="runbook",
                            source_url=f"conversion:{conversion_id}",
                            scope=job.scope,
                            owner_id=user_id,
                            team_id=job.team_id,
                        )
                    )
                except Exception as e:
                    logger.error(f"Ingestion failed for draft {draft_id}: {e}")
                    knowledge_item_id = None

            if knowledge_item_id and chunks_created > 0:
                dm.knowledge_item_id = knowledge_item_id

            await session.commit()

            return VerifyResponse(
                draft_id=dm.id,
                runbook_id=dm.runbook_id,
                status="verified",
                knowledge_item_id=knowledge_item_id or "",
                ingested=knowledge_item_id is not None and chunks_created > 0,
                ingested_at=now if knowledge_item_id else None,
                collection=collection,
                chunks_created=chunks_created,
            )

    # =========================================================================
    # Manual Runbook Creation
    # =========================================================================

    async def create_runbook_from_template(
        self,
        title: str,
        domain: str,
        service_name: str,
        symptom_class: List[str],
        severity: str,
        scope: str,
        tags: List[str],
        difficulty: str,
        problem_definition: str,
        diagnostic_steps: str,
        mitigation: str,
        root_cause_resolution: str,
        verification: str,
        prevention: str,
        user_id: str,
        organization_id: str = None,
        team_id: str = None,
    ) -> ConversionDraft:
        """Create a runbook from user-provided template fields (no LLM)."""
        import re as _re

        today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Generate kebab-case ID
        base = f"{service_name}-{title}"
        runbook_id = _re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")
        if len(runbook_id) > 60:
            import hashlib as _hashlib

            runbook_id = (
                runbook_id[:55]
                + "-"
                + _hashlib.md5(runbook_id.encode()).hexdigest()[:4]
            )

        symptom_str = ", ".join(symptom_class)
        tags_str = ", ".join(tags) if tags else ""

        content = f"""---
id: {runbook_id}
title: "{title}"
domain: {domain}
service: {service_name}
symptom_class: [{symptom_str}]
scope: {scope}
tags: [{tags_str}]
difficulty: {difficulty}
severity: {severity}
version: "1.0.0"
last_updated: "{today_iso}"
verified_by: ""
status: draft
---

# Runbook: {title}

## Problem Definition
{problem_definition}

## Diagnostic Steps
{diagnostic_steps}

## Mitigation
{mitigation}

## Root Cause Resolution
{root_cause_resolution}

## Verification
{verification}

## Prevention
{prevention}

## Sources
- Manually authored runbook
"""

        # Write to disk
        scope_dir = self._scope_dir(scope, team_id, user_id)
        scope_dir.mkdir(parents=True, exist_ok=True)
        draft_path = scope_dir / f"{runbook_id}.md"
        draft_path.write_text(content, encoding="utf-8")

        # Validate and score
        validation_result = self._validator.validate_content(content)
        quality = self._scorer.score_content(content)

        draft_id = generate_draft_id()

        quality_warning = None
        if quality.overall < QUALITY_WARNING_THRESHOLD:
            quality_warning = (
                "Quality score is below 50. Consider adding more detailed "
                "diagnostic commands, resolution steps, or verification procedures."
            )

        draft = ConversionDraft(
            draft_id=draft_id,
            runbook_id=runbook_id,
            title=title,
            scope=scope,
            status=DraftStatus.DRAFT,
            validation=validation_result,
            quality_score=quality,
            file_path=str(draft_path),
            content_preview=content[:500],
            content=content,
            quality_warning=quality_warning,
        )

        # Persist to database using a synthetic conversion job
        conversion_id = generate_conversion_id()
        await self._persist_job(
            conversion_id=conversion_id,
            user_id=user_id,
            organization_id=organization_id,
            scope=scope,
            team_id=team_id,
            status=ConversionStatus.COMPLETED,
            source_file=SourceFileInfo(
                filename=title,
                size_bytes=len(content.encode()),
                content_type="text/markdown",
                retained_path="",
            ),
            analysis=AnalysisResult(
                is_actionable=True,
                failure_modes=[],
                source_assessment=SourceAssessment(
                    content_type="manual",
                    actionability_rating="high",
                    missing_information=[],
                ),
            ),
            drafts=[draft],
            created_at=datetime.now(timezone.utc),
        )

        return {"conversion_id": conversion_id, "draft": draft}

    # =========================================================================
    # File Discovery Scan
    # =========================================================================

    async def scan_for_runbooks(self, user_id: str) -> dict:
        """Scan data/knowledge/ for .md files not tracked in the database.

        Discovers runbooks created by the KB Toolkit or dropped on disk manually.
        Creates draft records so they appear in the Dashboard Drafts tab.

        Uses an async lock to prevent concurrent scans from creating duplicate
        drafts (e.g., React StrictMode fires the mount effect twice).

        Returns:
            {"discovered": N, "skipped": N, "errors": [...], "drafts": [...]}
        """
        async with self._scan_lock:
            return await self._scan_for_runbooks_impl(user_id)

    async def _scan_for_runbooks_impl(self, user_id: str) -> dict:
        import re as _re

        import yaml

        discovered = []
        skipped = 0
        reverted = 0
        errors = []

        # Reconcile DB state before scanning disk
        tracked_paths: set[str] = set()
        if self._db_session_factory:
            async with self._db_session_factory() as session:
                all_drafts_result = await session.execute(select(ConversionDraftModel))
                all_draft_models = all_drafts_result.scalars().all()

                for draft_model in all_draft_models:
                    file_exists = Path(draft_model.file_path).exists()

                    if draft_model.status == "deleted":
                        continue

                    if not file_exists:
                        draft_model.status = "deleted"
                        continue

                    # If this draft has already been activated (has a
                    # knowledge_item_id linking it to a KB entry), it's a
                    # verified draft that should not be shown as pending.
                    # Do NOT delete drafts based on title matching against
                    # ChromaDB — stale data from previous sessions causes
                    # false positives that remove un-activated drafts.
                    if draft_model.status == "draft" and getattr(
                        draft_model, "knowledge_item_id", None
                    ):
                        draft_model.status = "deleted"
                        logger.info(
                            f"Removed duplicate draft {draft_model.id} "
                            f"(already has knowledge_item_id)"
                        )
                        continue

                    if draft_model.status == "verified":
                        # Trust SQLite: if knowledge_item_id is set, the
                        # document was activated. Don't probe ChromaDB.
                        if not getattr(draft_model, "knowledge_item_id", None):
                            # Verified but no knowledge_item_id — likely from
                            # a failed ingestion. Revert to draft.
                            draft_model.status = "draft"
                            reverted += 1
                            logger.info(
                                f"Reverted draft {draft_model.id} "
                                f"(verified but no knowledge_item_id)"
                            )

                    tracked_paths.add(draft_model.file_path)

                await session.commit()

        # Walk all scope directories
        knowledge_dir = self._data_dir
        if not knowledge_dir.exists():
            return {
                "discovered": 0,
                "reverted": reverted,
                "skipped": 0,
                "errors": [],
                "drafts": [],
            }

        for md_file in sorted(knowledge_dir.rglob("*.md")):
            # Skip sources directory (retained original uploads)
            if "sources" in md_file.parts:
                continue

            file_path_str = str(md_file)

            # Skip if already tracked in drafts DB (in-memory set from
            # reconciliation) or discovered earlier in this scan run.
            # Concurrent scans are serialized by _scan_lock.
            if file_path_str in tracked_paths:
                skipped += 1
                continue

            # Read and validate
            try:
                content = md_file.read_text(encoding="utf-8")
            except Exception as e:
                errors.append(f"{md_file.name}: cannot read ({e})")
                continue

            if len(content.strip()) < 100:
                errors.append(f"{md_file.name}: too short ({len(content)} chars)")
                continue

            # Extract metadata from frontmatter
            fm_match = _re.match(r"^---\s*\n(.*?)\n---\s*\n", content, _re.DOTALL)
            metadata = {}
            if fm_match:
                try:
                    metadata = yaml.safe_load(fm_match.group(1)) or {}
                except Exception:
                    pass

            title = metadata.get("title", md_file.stem.replace("-", " ").title())
            runbook_id = metadata.get("id", md_file.stem)

            # Infer scope from directory path
            scope = "global"
            relative = md_file.relative_to(knowledge_dir)
            scope_dir_name = relative.parts[0] if len(relative.parts) > 1 else ""
            if scope_dir_name.startswith("personal_") or scope_dir_name.startswith(
                "user_"
            ):
                scope = "personal"
            elif scope_dir_name.startswith("team_"):
                scope = "team"

            # Validate
            validation = self._validator.validate_content(content)
            quality = self._scorer.score_content(content)

            draft_id = generate_draft_id()
            quality_warning = None
            if quality.overall < QUALITY_WARNING_THRESHOLD:
                quality_warning = (
                    "Quality score is below 50. Review and edit before verifying."
                )

            draft = ConversionDraft(
                draft_id=draft_id,
                runbook_id=runbook_id,
                title=title if isinstance(title, str) else str(title),
                scope=scope,
                status=DraftStatus.DRAFT,
                validation=validation,
                quality_score=quality,
                file_path=file_path_str,
                content_preview=content[:500],
                content=content,
                quality_warning=quality_warning,
            )

            # Extract metadata from frontmatter for dashboard filters
            from faultmaven.utils.frontmatter import extract_frontmatter_metadata

            fm_meta = extract_frontmatter_metadata(content)
            raw_tags = metadata.get("tags", [])
            tags_str = (
                ", ".join(str(t) for t in raw_tags)
                if isinstance(raw_tags, list)
                else str(raw_tags) if raw_tags else None
            )

            # Persist as a synthetic conversion job
            conversion_id = generate_conversion_id()
            await self._persist_job(
                conversion_id=conversion_id,
                user_id=user_id,
                organization_id=None,
                scope=scope,
                team_id=None,
                status=ConversionStatus.COMPLETED,
                source_file=SourceFileInfo(
                    filename=md_file.name,
                    size_bytes=md_file.stat().st_size,
                    content_type="text/markdown",
                    retained_path="",
                ),
                analysis=AnalysisResult(
                    is_actionable=True,
                    failure_modes=[],
                    source_assessment=SourceAssessment(
                        content_type="file_scan",
                        actionability_rating="unknown",
                        missing_information=[],
                    ),
                ),
                drafts=[draft],
                created_at=datetime.now(timezone.utc),
            )

            # Set metadata columns on the draft record
            if self._db_session_factory:
                async with self._db_session_factory() as session:
                    result = await session.execute(
                        select(ConversionDraftModel).where(
                            ConversionDraftModel.id == draft_id
                        )
                    )
                    dm = result.scalar_one_or_none()
                    if dm:
                        dm.domain = fm_meta.get("domain")
                        dm.service = fm_meta.get("service")
                        dm.severity = fm_meta.get("severity")
                        dm.tags = tags_str
                        dm.document_type = "runbook"
                        await session.commit()

            tracked_paths.add(file_path_str)
            discovered.append(
                {
                    "conversion_id": conversion_id,
                    "draft_id": draft_id,
                    "title": draft.title,
                    "runbook_id": runbook_id,
                    "scope": scope,
                    "validation_passed": validation.passed,
                    "quality_score": quality.overall,
                    "file_path": file_path_str,
                }
            )

        return {
            "discovered": len(discovered),
            "reverted": reverted,
            "skipped": skipped,
            "errors": errors,
            "drafts": discovered,
        }

    async def delete_draft(
        self,
        conversion_id: str,
        draft_id: str,
        user_id: str,
    ) -> bool:
        """Soft-delete a draft and remove the file from disk."""
        if not self._db_session_factory:
            return False

        async with self._db_session_factory() as session:
            # Verify ownership (system-created jobs accessible to any user)
            job_result = await session.execute(
                select(ConversionJobModel).where(
                    ConversionJobModel.id == conversion_id,
                    (
                        (ConversionJobModel.user_id == user_id)
                        | (ConversionJobModel.user_id == "system")
                    ),
                )
            )
            if not job_result.scalar_one_or_none():
                return False

            draft_result = await session.execute(
                select(ConversionDraftModel).where(
                    ConversionDraftModel.id == draft_id,
                    ConversionDraftModel.conversion_id == conversion_id,
                )
            )
            dm = draft_result.scalar_one_or_none()
            if not dm:
                return False

            # Remove file from disk
            file_path = Path(dm.file_path)
            if file_path.exists():
                file_path.unlink()

            # Soft delete in database
            dm.status = DraftStatus.DELETED.value
            await session.commit()

            return True


# =============================================================================
# Custom Exceptions
# =============================================================================


class ConversionRejectedError(Exception):
    """Raised when a document is rejected during preprocessing or analysis."""

    def __init__(self, message: str, error_code: str = "UNKNOWN"):
        super().__init__(message)
        self.error_code = error_code
