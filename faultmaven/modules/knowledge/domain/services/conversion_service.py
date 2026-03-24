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
{{permanent fix command}}
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
                "filename": original_filename,
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
                source_path=source_file.retained_path,
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
            result = await session.execute(
                select(ConversionJobModel).where(
                    ConversionJobModel.id == conversion_id,
                    ConversionJobModel.user_id == user_id,
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

    async def list_conversions(
        self, user_id: str, limit: int = 20, offset: int = 0
    ) -> List[dict]:
        """List user's conversion jobs (summary, no draft content)."""
        if not self._db_session_factory:
            return []

        async with self._db_session_factory() as session:
            result = await session.execute(
                select(ConversionJobModel)
                .where(ConversionJobModel.user_id == user_id)
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
        """List all non-deleted drafts across all jobs for this user."""
        if not self._db_session_factory:
            return []

        async with self._db_session_factory() as session:
            result = await session.execute(
                select(ConversionDraftModel, ConversionJobModel)
                .join(
                    ConversionJobModel,
                    ConversionDraftModel.conversion_id == ConversionJobModel.id,
                )
                .where(
                    ConversionJobModel.user_id == user_id,
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
            # Verify ownership
            job_result = await session.execute(
                select(ConversionJobModel).where(
                    ConversionJobModel.id == conversion_id,
                    ConversionJobModel.user_id == user_id,
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
            # Verify ownership
            job_result = await session.execute(
                select(ConversionJobModel).where(
                    ConversionJobModel.id == conversion_id,
                    ConversionJobModel.user_id == user_id,
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
            if not dm or dm.status != DraftStatus.DRAFT.value:
                return None

            # Check validation passes
            if not dm.validation_passed:
                return None

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

            # Trigger ingestion into knowledge base
            knowledge_item_id = None
            chunks_created = 0
            collection = f"{job.scope}_kb"

            if self._knowledge_service:
                try:
                    content = file_path.read_text(encoding="utf-8")
                    result = await self._knowledge_service.add_document(
                        {
                            "title": dm.title,
                            "content": content,
                            "document_type": "runbook",
                            "source": f"conversion:{conversion_id}",
                        }
                    )
                    if isinstance(result, str):
                        knowledge_item_id = result
                    elif isinstance(result, dict):
                        knowledge_item_id = result.get("id", result.get("document_id"))
                except Exception as e:
                    logger.error(f"Ingestion failed for draft {draft_id}: {e}")

            if knowledge_item_id:
                dm.knowledge_item_id = knowledge_item_id

            await session.commit()

            return VerifyResponse(
                draft_id=dm.id,
                runbook_id=dm.runbook_id,
                status="verified",
                knowledge_item_id=knowledge_item_id or "",
                ingested=knowledge_item_id is not None,
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
            # Verify ownership
            job_result = await session.execute(
                select(ConversionJobModel).where(
                    ConversionJobModel.id == conversion_id,
                    ConversionJobModel.user_id == user_id,
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
