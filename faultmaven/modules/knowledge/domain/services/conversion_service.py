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
from typing import Dict, List, Optional, Tuple
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from faultmaven.exceptions import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ValidationException,
)
from faultmaven.infrastructure.persistence.models import (
    ConversionDraftModel,
    ConversionJobModel,
    KnowledgeItemModel,
    UploadedFileModel,
)
from faultmaven.modules.knowledge.domain.global_authoring import (
    ensure_global_authoring_allowed,
    is_global_authoring_allowed,
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
from faultmaven.modules.knowledge.domain.services.runbook_cause_extractor import (
    extract_causes,
)
from faultmaven.modules.knowledge.domain.services.runbook_validator import (
    QualityScorer,
    RunbookValidator,
)
from faultmaven.providers.tenancy.single_tenant import SingleTenantProvider

logger = logging.getLogger(__name__)

# Single-tenant default organization. Conversion sources (KB-bound uploads)
# require organization_id NOT NULL on both the upload and the conversion job;
# fall back to this when the caller doesn't supply one.
DEFAULT_ORGANIZATION_ID = SingleTenantProvider.DEFAULT_ORG_ID

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

# DESIGN DECISION (predicate-less conversion — intentional, not a gap).
# The conversion path (document -> runbook, case -> runbook) authors the v4 match
# surface + topology: Statement / optional Chain / Indicators / quadrant-tagged
# Interventions. It deliberately does NOT author ``<!-- match -->`` predicates (the
# deterministic validation surface). Predicate authoring is a separate, Phase-0-gated
# enrichment owned by the kb-toolkit generation path (kb-init / kb-researcher; Slice 6
# / #584) — its symptom-telemetry ``target`` contract and ``stance`` counterfactual are
# not yet ratified, so emitting predicates here would produce ill-formed ones. A
# conversion-produced runbook is therefore predicate-less by design; it still MATCHES
# and instantiates (the Statement + Chain do that) and grounds via the LLM tier. When
# the predicate contract lands, predicates are added by re-running the toolkit
# enrichment over these runbooks, not by expanding this prompt.
# See docs/working/AUDIT-runbook-template.md §3 (mirror 5) + PLAN 1a.
CONVERSION_SYSTEM_PROMPT = """You are a technical writer converting source material into a FaultMaven v4
causal-chain runbook. You MUST produce output that exactly matches the template below.
Every section and sub-field is required. Do not add sections. Do not rename sections.
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

## Symptom Recognition
- Exact alert names as they appear: "Alert: ..."
- Error messages as they appear in logs: "ERROR: ..."
- Metric patterns: "metric > threshold for duration"

## Applicability
State the software version range, required access level, and tools needed
(e.g. "PostgreSQL 14+, AWS RDS or self-hosted. Requires pg_monitor role. Tools: psql.").

## Diagnostic Steps

### Step 1: {{description}}
```{{language}}
{{command}}
```
{{what to look for in the output — be specific}}

### Step 2: {{description}}
...

## Causes

### Cause A: {{name}}
**Statement:** Single declarative sentence stating the single root cause (≤300 chars).
**Chain:**
- root: the root cause (the chain's top node; mirrors Statement)
- s1: intermediate state — the direct effect of the node above
- D: the failure (points at Symptom Recognition; do not re-author it)
**Indicators:**
- root: [Step 1] {{observable from Step 1 that confirms the root rung}}
- s1: [Step 2] {{observable that confirms intermediate state s1}}
**Interventions:**
- **remediation** (root): {{the durable fix at the root}}

  ```{{language}}
  {{durable fix command}}
  ```

  **Verification:** Re-run Step N; {{what confirms the fix worked}}.
- **mitigation** (s1): {{a temporary interception — include only if one genuinely exists}}

  ```{{language}}
  {{quick fix command}}
  ```

  **Risk:** {{what could go wrong}}. **Duration:** {{how long safe}}. **Verification:** {{cause-specific check}}.

### Cause Z: Unidentified
**Statement:** None of the documented causes match the observed evidence.
**Indicators:**
- [Default]
**Interventions:**
- **mitigation** (D): Capture full diagnostic output and consult an SME.
  **Risk:** Diagnostic only. **Duration:** Until SME review. **Verification:** N/A.

## Prevention
- {{configuration change to prevent recurrence}}
- {{monitoring alert to add}}

## Sources
- {{source_filename}} -- primary source document for this runbook

=========

RULES:
1. Every section and sub-field MUST contain content. No empty fields.
2. ## Diagnostic Steps MUST contain fenced code blocks under numbered `### Step N: <title>` headers (number, colon, then a short inline title).
3. ## Causes MUST have at least one real ### Cause A subsection AND the fallback ### Cause Z: Unidentified.
4. Each ### Cause declares exactly ONE root — never two roots, never an AND-gate. Each ### Cause (except Z) needs **Statement**, **Indicators**, and **Interventions**; **Chain** is optional (omit it for a simple one-step cause). For two co-necessary conditions: when one enables the other, express them as sequential Chain rungs; when neither causes the other, fold the second into the root Statement.
5. Statement ≤300 characters; each Chain rung ≤300 characters. Hard limits.
6. Each Indicator entry carries a rung ref (`root`, `s1`, …, or `D`) and at least one `[Step N]` (N matches an existing Diagnostic Step) or `[Symptom]`; the Cause Z fallback uses `- [Default]`.
7. Each Intervention is tagged with exactly one quadrant — `remediation` / `defensive_fix` / `mitigation` / `loop_break` — names the rung it targets in `(parens)`, and carries a **Verification:**; every `mitigation` also carries **Risk** and **Duration**.
8. If source material lacks enough information for a field, write "[INSUFFICIENT SOURCE DATA -- manual completion required]".
9. Use the taxonomy values provided. Do not change domain, service, or symptom_class."""


# =============================================================================
# Frontmatter Helpers
# =============================================================================


def _force_frontmatter_id(content: str, runbook_id: str) -> str:
    """Rewrite the frontmatter ``id`` field to ``runbook_id``.

    The conversion prompt instructs the LLM to use a specific kebab-case id,
    but some models emit the failure-mode title (or other text) as the id
    anyway, which then fails ``RunbookValidator`` for case-derived runbooks
    whose titles contain capitals or periods. This helper guarantees the
    frontmatter agrees with the filename + DB row by force-replacing the
    line. If the LLM omitted ``id`` entirely, we insert one as the first
    frontmatter field.
    """
    import re as _re

    fm_match = _re.match(r"^(---\s*\n)(.*?)(\n---\s*\n)", content, _re.DOTALL)
    if not fm_match:
        # No frontmatter — caller's downstream validator will catch this;
        # we don't synthesize one here.
        return content
    head, body, tail = fm_match.groups()
    if _re.search(r"^id:\s*.+$", body, _re.MULTILINE):
        new_body = _re.sub(
            r"^id:\s*.+$", f"id: {runbook_id}", body, count=1, flags=_re.MULTILINE
        )
    else:
        new_body = f"id: {runbook_id}\n{body}"
    return head + new_body + tail + content[fm_match.end() :]


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
        share_repository=None,
    ):
        self._llm_router = llm_router
        self._settings = settings
        self._db_session_factory = db_session_factory
        self._knowledge_service = knowledge_service
        # Source of truth for team visibility (ADR-013 §D4). A team publish
        # target is recorded as a share row on the conversion_job and, on
        # verify, transferred to the promoted knowledge_item. None → team
        # publishing is inert (standalone).
        self._share_repo = share_repository
        self._preprocessor = DocumentPreprocessor(llm_router, settings)
        self._validator = RunbookValidator()
        self._scorer = QualityScorer()
        self._scan_lock = asyncio.Lock()
        # In-flight case-conversion dedup. Keyed by case_id; the value is
        # the running asyncio.Task that other concurrent callers can await.
        # Mirrors `_inflight_vectorize` on MilestoneEngine. Prevents
        # duplicate drafts when the user clicks the runbook affordance
        # twice in rapid succession. See convert_from_case().
        self._inflight_runbook: Dict[str, asyncio.Task] = {}

    @property
    def _data_dir(self) -> Path:
        return Path("data/knowledge")

    def _scope_dir(self, scope: str, team_id: str = None, user_id: str = None) -> Path:
        if scope == "global":
            return self._data_dir / "global"
        elif scope == "team" and team_id:
            return self._data_dir / f"team_{team_id}"
        elif scope == "personal" and user_id:
            return self._data_dir / f"user_{user_id}"
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

        Dedup: if a conversion is already in flight for this case, the
        in-flight Task is awaited rather than a duplicate started. Prevents
        duplicate drafts when the user clicks the runbook affordance twice
        in rapid succession (chat-triggered) or when the chat-triggered and
        HTTP-triggered paths race. Mirrors the `_inflight_vectorize`
        pattern on MilestoneEngine.
        """
        # Short-circuit if a conversion for this case is already running.
        inflight = self._inflight_runbook.get(request.case_id)
        if inflight is not None and not inflight.done():
            logger.info(
                "case_conversion_dedup_hit",
                extra={"case_id": request.case_id},
            )
            return await inflight

        # Wrap the actual work in a Task so other concurrent callers can
        # await the same result.
        task = asyncio.create_task(
            self._convert_from_case_impl(request, user_id, organization_id, team_id)
        )
        self._inflight_runbook[request.case_id] = task
        try:
            return await task
        finally:
            # Defensive cleanup: only remove if this is still our task,
            # in case a subsequent caller has already overwritten the entry.
            if self._inflight_runbook.get(request.case_id) is task:
                self._inflight_runbook.pop(request.case_id, None)

    async def _convert_from_case_impl(
        self,
        request: "CaseConversionRequest",
        user_id: str,
        organization_id: str = None,
        team_id: str = None,
    ) -> ConversionResponse:
        """Internal: the actual conversion pipeline. Always called via
        `convert_from_case`, which wraps this with the dedup registry."""
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

        # Trust-boundary guard (defense-in-depth for #698). This funnel is the
        # single point every case→runbook caller passes through; it must not
        # trust callers to have gated. The service holds the extracted
        # ``CaseConversionRequest`` DTO, not the Case, so it cannot re-evaluate
        # the cause-assurance grade here (that stays at the case-holding sites
        # via ``runbook_conversion_ready``); but it CAN enforce the record half:
        # a request with no root_cause text would otherwise generate a runbook
        # whose Resolution silently degrades to a "See solutions below" stub.
        # Refuse instead — a runbook with no root cause is not reusable knowledge.
        if not (request.root_cause and request.root_cause.strip()):
            raise ConversionRejectedError(
                "This case has no recorded root cause, so it can't be converted "
                "into a runbook.",
                error_code=ConversionErrorCode.MISSING_ROOT_CAUSE,
            )

        # Idempotence guard: never generate a second runbook from a case that
        # already produced one. A prior conversion with at least one live draft
        # (DRAFT or VERIFIED) blocks regeneration; a case whose only drafts were
        # discarded — or whose prior attempt failed with no drafts — is free to
        # regenerate. The in-flight lock in ``convert_from_case`` covers the
        # concurrent double-fire race; this covers the sequential repeat (the
        # first run's persisted job is visible here).
        existing = await self.get_conversion_by_case(request.case_id, user_id)
        if existing and existing.has_live_draft():
            raise ConversionRejectedError(
                "A runbook draft already exists for this case. View or update it "
                "in the Dashboard under Knowledge > Drafts.",
                error_code=ConversionErrorCode.CASE_RUNBOOK_EXISTS,
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
            # Guaranteed non-empty by the trust-boundary guard above.
            resolution_summary=request.root_cause,
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
            # Each block is outcome-tagged (applied vs proposed) by
            # CaseConversionRequest.from_case, which also drops superseded/failed
            # attempts. A neutral header keeps that per-block outcome authoritative
            # instead of asserting every listed fix was applied.
            solutions_text = "\n\n".join(request.solutions)
            source_parts.append(f"SOLUTIONS:\n{solutions_text}")
        if request.hypotheses_summary:
            source_parts.append(f"VALIDATED HYPOTHESES: {request.hypotheses_summary}")
        if request.evidence_summary:
            source_parts.append(f"KEY EVIDENCE:\n{request.evidence_summary}")

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
            raise ConversionRejectedError(
                f"LLM analysis response could not be parsed: {e}",
                error_code=ConversionErrorCode.LLM_PARSE_ERROR,
            ) from e

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

            # Pre-compute the runbook_id so we can pass the exact kebab-case
            # value to the LLM. Without this, the LLM is left to derive `id`
            # from the failure-mode title and routinely uses the title verbatim
            # (e.g. "Case-260526-4"), which fails the kebab-case validator.
            runbook_id = generate_runbook_id(failure_mode)

            user_message = (
                f"Convert the following source material into a runbook for this specific "
                f"failure mode:\n\n"
                f"RUNBOOK_ID: {runbook_id}\n"
                f"FAILURE MODE: {failure_mode.title}\n"
                f"DOMAIN: {failure_mode.domain}\n"
                f"SERVICE: {failure_mode.service}\n"
                f"SYMPTOM_CLASS: {', '.join(failure_mode.symptom_class)}\n"
                f"SEVERITY: {failure_mode.severity}\n"
                f"SCOPE: {scope}\n"
                f"SOURCE FILENAME: {filename}\n"
                f"TODAY: {today_iso}\n\n"
                f"The frontmatter `id` field MUST be exactly: {runbook_id}\n"
                f"(lowercase, kebab-case; do not derive a different id from "
                f"the title).\n\n"
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
                    "## Symptom Recognition",
                    "## Diagnostic Steps",
                    "## Causes",
                ]
            ):
                return ConversionError(
                    failure_mode_id=failure_mode.id,
                    error="LLM response missing required runbook sections",
                    retryable=True,
                )

            # Belt-and-suspenders: prompt instructions don't fully constrain
            # the LLM, so rewrite the frontmatter `id` to the kebab-case
            # value we computed. The filename + DB row + frontmatter all
            # share this single source of truth.
            runbook_content = _force_frontmatter_id(runbook_content, runbook_id)

            # `runbook_id` was computed before the LLM call so it could be
            # passed in the prompt; re-using it here keeps the on-disk
            # filename, the prompt-injected `id`, and the row's runbook_id
            # in sync.
            draft_id = generate_draft_id()

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
            # ``conversion_jobs`` carries a single ``source_file_id`` FK to
            # ``uploaded_files`` (ON DELETE RESTRICT). Create the upload row
            # first; the conversion_jobs row references it. Both tables
            # require organization_id NOT NULL.
            org_id = organization_id or DEFAULT_ORGANIZATION_ID
            source_file_id = f"file_{uuid4().hex[:12]}"
            upload = UploadedFileModel(
                file_id=source_file_id,
                organization_id=org_id,
                case_id=None,  # KB-bound, not case-bound
                uploaded_by=user_id,
                filename=source_file.filename,
                size_bytes=source_file.size_bytes,
                content_type=source_file.content_type,
                storage_ref=source_file.retained_path or None,
                upload_source="conversion_source",
                uploaded_at_turn=0,
            )
            session.add(upload)
            await session.flush()  # ensure upload row exists before FK ref

            job = ConversionJobModel(
                id=conversion_id,
                user_id=user_id,
                organization_id=org_id,
                scope=scope,
                status=status.value,
                source_file_id=source_file_id,
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
                    organization_id=org_id,
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

        # Team publish target: record it as a share row on the conversion_job
        # (source of truth, ADR-013 §D4). On verify, it is transferred to the
        # promoted knowledge_item. Outside the session block — the share repo is
        # sessionless. Inert (no-op) when no share repo is wired.
        if scope == "team" and team_id and self._share_repo:
            await self._share_repo.share(
                resource_type="conversion_job",
                resource_id=conversion_id,
                scope_type="team",
                scope_id=team_id,
                organization_id=org_id,
                created_by=user_id,
            )

    async def _resolve_job_team_id(self, conversion_id: str) -> Optional[str]:
        """Return the team a conversion job is shared to, or None.

        Reads the job's share rows (ADR-013 §D4) — replaces the retired
        ``conversion_jobs.team_id`` column. A job is shared to at most one team
        in v1; the first team share wins.
        """
        if not self._share_repo:
            return None
        shares = await self._share_repo.list_scopes_for_resource(
            "conversion_job", conversion_id
        )
        for s in shares:
            if s.scope_type == "team":
                return s.scope_id
        return None

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
                    ConversionDraftModel.status != DraftStatus.DISCARDED.value,
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

            # Source file metadata lives on ``uploaded_files``; traverse
            # via the ``source_file_id`` FK to read filename / size /
            # content_type / storage_ref.
            upload = await session.get(UploadedFileModel, job.source_file_id)
            source_file = (
                SourceFileInfo(
                    filename=upload.filename,
                    size_bytes=upload.size_bytes,
                    content_type=upload.content_type,
                    retained_path=upload.storage_ref or "",
                )
                if upload
                else SourceFileInfo(
                    filename="<source upload missing>",
                    size_bytes=0,
                    content_type="",
                    retained_path="",
                )
            )

            return ConversionResponse(
                conversion_id=job.id,
                status=ConversionStatus(job.status),
                source_file=source_file,
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
                # A case can accrue more than one job (e.g. regenerated after the
                # prior draft was discarded), so take the latest — a bare
                # scalar_one_or_none() would raise MultipleResultsFound.
                .limit(1)
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

            # Bulk-fetch source uploads for all jobs in this page so we can
            # resolve source_filename without an N+1 query. Filename lives
            # on ``uploaded_files.filename``, reachable via
            # ``conversion_jobs.source_file_id``.
            file_ids = [j.source_file_id for j in jobs if j.source_file_id]
            uploads_by_id: dict[str, str] = {}
            if file_ids:
                uploads_result = await session.execute(
                    select(UploadedFileModel).where(
                        UploadedFileModel.file_id.in_(file_ids)
                    )
                )
                uploads_by_id = {
                    u.file_id: u.filename for u in uploads_result.scalars().all()
                }

            return [
                {
                    "conversion_id": job.id,
                    "status": job.status,
                    "source_filename": uploads_by_id.get(job.source_file_id, ""),
                    "failure_modes_detected": job.failure_modes_detected,
                    "scope": job.scope,
                    "created_at": (
                        job.created_at.isoformat() if job.created_at else None
                    ),
                }
                for job in jobs
            ]

    async def list_drafts_for_case(self, case_id: str) -> List[dict]:
        """Return non-discarded drafts whose parent job links to ``case_id``.

        Used by the case Report tab to surface case-derived runbook drafts
        alongside the auto-generated resolution/closure summaries. Returns an
        empty list when no DB session factory is wired up or no drafts match.
        """
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
                    ConversionJobModel.case_id == case_id,
                    ConversionDraftModel.status != DraftStatus.DISCARDED.value,
                )
                .order_by(ConversionDraftModel.created_at.desc())
            )
            rows = result.all()
            return [
                {
                    "draft_id": dm.id,
                    "conversion_id": job.id,
                    "runbook_id": dm.runbook_id,
                    "title": dm.title,
                    "status": dm.status,
                    "scope": job.scope,
                    "file_path": dm.file_path,
                    "knowledge_item_id": dm.knowledge_item_id,
                    "validation_passed": dm.validation_passed,
                    "created_at": (
                        dm.created_at.isoformat() if dm.created_at else None
                    ),
                    "verified_at": (
                        dm.verified_at.isoformat() if dm.verified_at else None
                    ),
                }
                for dm, job in rows
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
                    ConversionDraftModel.status != DraftStatus.DISCARDED.value,
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
            if not dm or dm.status == DraftStatus.DISCARDED.value:
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
        is_admin: bool = False,
    ) -> dict:
        """Verify multiple drafts sequentially. Returns summary with per-item status.

        ``is_admin`` is threaded into each :meth:`verify_draft` so the global-tier
        authoring gate applies per item: a global draft the caller may not author
        is recorded ``forbidden`` (never published) while the rest of the batch
        proceeds (#770, R4).
        """
        results = []
        verified = 0
        failed = 0
        skipped = 0
        forbidden = 0

        for conversion_id, draft_id in draft_refs:
            try:
                response = await self.verify_draft(
                    conversion_id=conversion_id,
                    draft_id=draft_id,
                    user_id=user_id,
                    username=username,
                    is_admin=is_admin,
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
            except AuthorizationError as e:
                # Global-tier authoring denial (multi-tenant / non-admin). Not a
                # failure of THIS draft — a policy refusal; record it distinctly
                # and never treat it as retryable. The draft stays unpublished.
                results.append(
                    {
                        "conversion_id": conversion_id,
                        "draft_id": draft_id,
                        "status": "forbidden",
                        "error": str(e),
                        "knowledge_item_id": None,
                    }
                )
                forbidden += 1
            except ConflictError as e:
                # An already-verified draft is idempotent, not broken: the
                # runbook is already published, so re-verifying it is a no-op
                # to SKIP, not a failure (#784). ``verify_draft`` signals this
                # with the typed ``ConflictError`` and a structured
                # ``conflict_reason`` — classify on that field, never on the
                # message string (which the exception-contract migration made a
                # dead match). Every other conflict (draft discarded, or in an
                # unexpected state) genuinely cannot be verified → ``failed``.
                if e.conflict_reason == "already_verified":
                    results.append(
                        {
                            "conversion_id": conversion_id,
                            "draft_id": draft_id,
                            "status": "skipped",
                            "error": str(e),
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
                            "error": str(e),
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
            "forbidden": forbidden,
            "results": results,
        }

    async def verify_draft(
        self,
        conversion_id: str,
        draft_id: str,
        user_id: str,
        username: str,
        is_admin: bool = False,
    ) -> Optional[VerifyResponse]:
        """Promote draft to verified status, update frontmatter, trigger ingestion.

        ``is_admin`` gates publication at ``global`` scope: verifying a draft
        ingests it into the KB at ``job.scope``, and a global runbook is the
        org-free platform corpus (readable by every tenant, seeder-consumed), so
        publishing one is a platform-operator action. The draft's scope is only
        known once the job row is loaded, so this gate lives here rather than at
        the route. Defaults ``False`` (fail-closed) so a caller that forgets to
        pass it can never publish global content by omission (#770, R4).
        """
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
                raise NotFoundError(
                    resource_type="conversion_job",
                    resource_id=conversion_id,
                    message="Conversion job not found",
                )

            # Global-tier authoring gate: forbidden from any tenant session under
            # multi, admin-only single-tenant. Enforced before the draft is
            # loaded or any side effect runs (frontmatter mutation / ingestion),
            # and regardless of job ownership — a "system"-owned global draft
            # (e.g. from a disk scan) must not be publishable by a non-admin.
            if job.scope == "global":
                ensure_global_authoring_allowed(is_admin)

            draft_result = await session.execute(
                select(ConversionDraftModel).where(
                    ConversionDraftModel.id == draft_id,
                    ConversionDraftModel.conversion_id == conversion_id,
                )
            )
            dm = draft_result.scalar_one_or_none()
            if not dm:
                raise NotFoundError(
                    resource_type="draft",
                    resource_id=draft_id,
                    message="Draft not found",
                )
            if dm.status == DraftStatus.VERIFIED.value:
                raise ConflictError(
                    "This runbook has already been verified and ingested",
                    resource_type="draft",
                    resource_id=draft_id,
                    conflict_reason="already_verified",
                )
            if dm.status == DraftStatus.DISCARDED.value:
                raise ConflictError(
                    "This draft has been discarded",
                    resource_type="draft",
                    resource_id=draft_id,
                    conflict_reason="discarded",
                )
            if dm.status != DraftStatus.DRAFT.value:
                raise ConflictError(
                    f"Draft is in unexpected state: {dm.status}",
                    resource_type="draft",
                    resource_id=draft_id,
                    conflict_reason="unexpected_state",
                )

            if not dm.validation_passed:
                raise ValidationException(
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

            # Populate metadata from frontmatter (safe to set pre-ingest; the
            # frontmatter on disk was already updated above).
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
                    # ConversionDraftModel.tags is a TagsArray TypeDecorator
                    # expecting list[str]; pass the list shape directly.
                    if isinstance(raw_tags, list):
                        dm.tags = [str(t) for t in raw_tags] or None
                    elif isinstance(raw_tags, str) and raw_tags:
                        dm.tags = [raw_tags]
                except Exception:
                    pass

            # Ingest into ChromaDB (chunk + embed + store). The verified
            # status is committed ONLY after ingestion succeeds — verifying
            # without indexed embeddings is the bug history this guards
            # against. The previous half-state (status=verified,
            # knowledge_item_id=NULL) was then "repaired" by a subsequent
            # KB-page scan that downgraded the row back to draft on every
            # visit, corrupting user-verified runbooks.
            # All KB writes land in the single shared collection, regardless
            # of scope — scope is a per-row metadata field, not a collection
            # split. Report the real collection name so the verify response
            # matches the store the chunks actually went to.
            from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
                KB_COLLECTION,
            )

            collection = KB_COLLECTION

            if not self._knowledge_service:
                raise RuntimeError(
                    "KnowledgeService unavailable — cannot verify draft "
                    "without ingestion. Aborting with no status mutation."
                )

            from faultmaven.utils.runbook_id import authored_item_id

            # 16-hex authored id — must NOT match the 12-hex built-in pattern, or
            # the bootstrap orphan-prune would delete this user runbook on redeploy.
            knowledge_item_id = authored_item_id()

            # Close the knowledge flywheel: extract the v4 ``## Causes`` graph
            # record so this human-verified, case-derived runbook re-enters
            # diagnosis as structured candidates (the Phase-4 seeder consumes
            # ``metadata["causes"]``), exactly like a built-in runbook does from
            # the pack. Extraction is deliberately here — on verify, the
            # human-verification gate — and NOT inside ``ingest_runbook``: the
            # anonymous/experimental ``upload_document`` path also calls
            # ``ingest_runbook`` and must never become a seeder feeder.
            causes = extract_causes(content)
            # Transfer the job's team publish target (a share row on the
            # conversion_job) to the promoted knowledge_item — ingest_runbook
            # creates the item's own share row. Replaces the retired
            # conversion_jobs.team_id column (ADR-013 §D4).
            team_id = await self._resolve_job_team_id(conversion_id)
            try:
                chunks_created = await self._knowledge_service.ingest_runbook(
                    document_id=knowledge_item_id,
                    title=dm.title,
                    content=content,
                    organization_id=job.organization_id,
                    document_type="runbook",
                    source_url=f"conversion:{conversion_id}",
                    scope=job.scope,
                    owner_id=user_id,
                    team_id=team_id,
                    verified_by=user_id,
                    causes=causes or None,
                )
            except Exception as e:
                # `ingest_runbook` cleaned up its own SQL row before raising.
                # The draft stays in DRAFT state; the caller gets a 500 and
                # can retry. No half-state in either store.
                logger.error(f"Ingestion failed for draft {draft_id}: {e}")
                raise

            if chunks_created <= 0:
                # Defence-in-depth: `ingest_runbook` should raise on 0-chunk
                # results. If it doesn't, treat this as a contract violation
                # and refuse to mark the draft verified.
                raise RuntimeError(
                    f"Vector indexing produced 0 chunks for draft {draft_id}. "
                    f"Draft remains in DRAFT state."
                )

            # Ingestion succeeded — NOW commit the verified status.
            now = datetime.now(timezone.utc)
            dm.status = DraftStatus.VERIFIED.value
            dm.verified_at = now
            dm.verified_by = user_id
            dm.knowledge_item_id = knowledge_item_id

            await session.commit()

            return VerifyResponse(
                draft_id=dm.id,
                runbook_id=dm.runbook_id,
                status="verified",
                knowledge_item_id=knowledge_item_id,
                ingested=True,
                ingested_at=now,
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
        symptom_recognition: str,
        applicability: str,
        diagnostic_steps: str,
        causes: str,
        prevention: str,
        user_id: str,
        organization_id: str = None,
        team_id: str = None,
    ) -> ConversionDraft:
        """Create a v4 causal-chain runbook from user-provided template fields (no LLM).

        causes should be pre-formatted markdown containing one or more
        ### Cause N: <name> subsections (one ROOT each), with Statement, an optional
        Chain (root->D rungs), per-rung Indicators ([Step N]-anchored), and
        quadrant-tagged Interventions (remediation/defensive_fix/mitigation/
        loop_break), plus a ### Cause Z: Unidentified fallback with [Default] indicator.
        """
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

## Symptom Recognition
{symptom_recognition}

## Applicability
{applicability}

## Diagnostic Steps
{diagnostic_steps}

## Causes
{causes}

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

    async def scan_for_runbooks(
        self,
        user_id: str,
        organization_id: Optional[str] = None,
        is_admin: bool = False,
    ) -> dict:
        """Scan data/knowledge/ for .md files not tracked in the database.

        Discovers runbooks created by the KB Toolkit or dropped on disk manually.
        Creates draft records so they appear in the Dashboard Drafts tab.

        Uses an async lock to prevent concurrent scans from creating duplicate
        drafts (e.g., React StrictMode fires the mount effect twice).

        Args:
            user_id: User triggering the scan (recorded as conversion job owner).
            organization_id: Org for scoping the conversion job + source upload.
                Falls back to DEFAULT_ORGANIZATION_ID when None.
            is_admin: Whether the caller may author global-scope KB. A file whose
                inferred scope is ``global`` (the org-free platform corpus) is
                SKIPPED when the caller is not a platform operator (any tenant
                session under multi, or a non-admin single-tenant) — minting a
                global draft is platform-tier authoring (#770, R4). Personal/team
                discovery is unaffected. Defaults ``False`` (fail-closed).

        Returns:
            {"discovered": N, "skipped": N, "errors": [...], "drafts": [...]}
        """
        async with self._scan_lock:
            return await self._scan_for_runbooks_impl(
                user_id, organization_id, is_admin
            )

    async def _scan_for_runbooks_impl(
        self,
        user_id: str,
        organization_id: Optional[str] = None,
        is_admin: bool = False,
    ) -> dict:
        import re as _re

        import yaml

        # Whether this caller may mint global-scope drafts. Computed once (the
        # policy is per-caller, not per-file); global-inferred files are skipped
        # below when this is False.
        global_authoring_allowed = is_global_authoring_allowed(is_admin)

        discovered = []
        skipped = 0
        errors = []

        from faultmaven.utils.runbook_id import item_id_from_runbook_id

        # Reconcile DB state before scanning disk
        tracked_paths: set[str] = set()
        # item_ids already published into knowledge_items — e.g. by the
        # startup KB bootstrap, which ingests shipped runbooks DIRECTLY
        # (bypassing conversion_drafts entirely, per kb_init's design). Such
        # files have no draft row, so without this set the disk walk below
        # would treat them as "untracked" and manufacture a phantom draft for
        # every already-published runbook.
        ingested_item_ids: set[str] = set()
        if self._db_session_factory:
            async with self._db_session_factory() as session:
                ingested_result = await session.execute(
                    select(KnowledgeItemModel.item_id)
                )
                ingested_item_ids = set(ingested_result.scalars().all())

                all_drafts_result = await session.execute(select(ConversionDraftModel))
                all_draft_models = all_drafts_result.scalars().all()

                non_discarded_count = sum(
                    1
                    for d in all_draft_models
                    if d.status != DraftStatus.DISCARDED.value
                )
                pending_discard_ids: list[str] = []
                # Discards for drafts whose runbook is ALREADY published in
                # knowledge_items. Kept separate from pending_discard_ids so
                # they do NOT feed the "would discard ALL active drafts ⇒
                # storage failure" abort guard below — clearing redundant
                # phantom drafts is legitimate cleanup even if it empties the
                # Drafts tab, not a wiped-data-dir signal.
                redundant_discard_ids: list[str] = []

                for draft_model in all_draft_models:
                    file_exists = Path(draft_model.file_path).exists()

                    if draft_model.status == DraftStatus.DISCARDED.value:
                        continue

                    if not file_exists:
                        draft_model.status = DraftStatus.DISCARDED.value
                        pending_discard_ids.append(draft_model.id)
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
                        draft_model.status = DraftStatus.DISCARDED.value
                        pending_discard_ids.append(draft_model.id)
                        logger.info(
                            f"Removed duplicate draft {draft_model.id} "
                            f"(already has knowledge_item_id)"
                        )
                        continue

                    # Redundant phantom draft: this runbook is already
                    # published in knowledge_items (typically by the startup
                    # bootstrap, which never sets knowledge_item_id on a
                    # draft because it bypasses the drafts table). Discard so
                    # it stops showing as pending in the Drafts tab.
                    if (
                        draft_model.status == "draft"
                        and draft_model.runbook_id
                        and item_id_from_runbook_id(draft_model.runbook_id)
                        in ingested_item_ids
                    ):
                        draft_model.status = DraftStatus.DISCARDED.value
                        redundant_discard_ids.append(draft_model.id)
                        logger.info(
                            f"Discarded phantom draft {draft_model.id} "
                            f"(runbook '{draft_model.runbook_id}' already "
                            f"published in knowledge_items)"
                        )
                        continue

                    if draft_model.status == "verified":
                        # Trust SQLite: if knowledge_item_id is set, the
                        # document was activated.
                        # If status=verified but knowledge_item_id is missing,
                        # this is a legacy half-state row from the pre-atomic
                        # verify_draft path. The current verify_draft only
                        # commits VERIFIED after successful ingestion, so new
                        # rows should never reach this branch. Warn loudly
                        # rather than silently downgrading — a silent revert
                        # is what corrupted live data in the incident this
                        # path was rewritten to prevent.
                        if not getattr(draft_model, "knowledge_item_id", None):
                            logger.warning(
                                f"Draft {draft_model.id} has status=verified "
                                f"but no knowledge_item_id (legacy half-state). "
                                f"Leaving as-is; investigate and clean up via "
                                f"explicit admin action if needed."
                            )

                    tracked_paths.add(draft_model.file_path)

                # Guard: abort if the scan would discard every active draft.
                # This signals a storage-layer failure (data directory missing/
                # wiped), not legitimate cleanup. Raising here skips the commit
                # so DB state is fully preserved for manual recovery.
                if (
                    non_discarded_count > 0
                    and len(pending_discard_ids) >= non_discarded_count
                ):
                    preview = pending_discard_ids[:20]
                    suffix = "..." if len(pending_discard_ids) > 20 else ""
                    raise RuntimeError(
                        f"Scan aborted: would discard all {non_discarded_count} active runbook "
                        f"draft(s). Runbook files appear to be missing from the knowledge "
                        f"data directory. DB state is unchanged. "
                        f"Affected draft IDs: {preview}{suffix}. "
                        "Restore data/knowledge/ from backup, then retry the scan."
                    )

                await session.commit()

        # Walk all scope directories
        knowledge_dir = self._data_dir
        if not knowledge_dir.exists():
            return {
                "discovered": 0,
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

            # Skip files already published into knowledge_items (e.g. by the
            # startup bootstrap, which ingests directly and never creates a
            # draft). Without this the scan manufactures a phantom draft for
            # every already-published runbook.
            if item_id_from_runbook_id(runbook_id) in ingested_item_ids:
                skipped += 1
                continue

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

            # Global-tier authoring gate: a global-inferred file mints a draft
            # into the platform corpus (verified → readable by every tenant,
            # seeder-consumed). A caller who may not author global scope (any
            # tenant session under multi, or a non-admin single-tenant) skips it
            # rather than minting an ungated global draft; personal/team files
            # discovered in the same scan still proceed (#770, R4).
            if scope == "global" and not global_authoring_allowed:
                logger.info(
                    "Scan skipped global-scope file %s: caller not permitted to "
                    "author global (platform corpus) knowledge",
                    md_file.name,
                )
                skipped += 1
                continue

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
            # ConversionDraftModel.tags is a TagsArray TypeDecorator that
            # expects a list[str] — the decorator handles cross-dialect
            # serialization (TEXT[] on PG, comma-joined TEXT on SQLite).
            # Don't pre-join; pass the list shape directly.
            if isinstance(raw_tags, list):
                tags_list: Optional[List[str]] = [str(t) for t in raw_tags] or None
            elif raw_tags:
                tags_list = [str(raw_tags)]
            else:
                tags_list = None

            # Persist as a synthetic conversion job
            conversion_id = generate_conversion_id()
            await self._persist_job(
                conversion_id=conversion_id,
                user_id=user_id,
                organization_id=organization_id,
                scope=scope,
                team_id=None,
                status=ConversionStatus.COMPLETED,
                source_file=SourceFileInfo(
                    filename=md_file.name,
                    size_bytes=md_file.stat().st_size,
                    content_type="text/markdown",
                    retained_path=str(md_file),
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
                        dm.tags = tags_list
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
            "skipped": skipped,
            "errors": errors,
            "drafts": discovered,
        }

    async def discard_by_knowledge_item_id(self, knowledge_item_id: str) -> bool:
        """Discard the draft that was activated into the given knowledge item.

        Called by KnowledgeService when a verified runbook is deleted from
        the KB. Clears knowledge_item_id and sets status to DISCARDED so
        the draft no longer appears in the pending or verified list.

        Args:
            knowledge_item_id: The knowledge_item_id (or runbook_id) stored
                               on the ConversionDraftModel row.

        Returns:
            True if a matching draft was found and discarded, False otherwise.
        """
        if not self._db_session_factory:
            return False

        async with self._db_session_factory() as session:
            result = await session.execute(
                select(ConversionDraftModel).where(
                    (ConversionDraftModel.knowledge_item_id == knowledge_item_id)
                    | (ConversionDraftModel.runbook_id == knowledge_item_id)
                )
            )
            dm = result.scalar_one_or_none()
            if not dm:
                return False

            dm.status = DraftStatus.DISCARDED.value
            dm.knowledge_item_id = None
            await session.commit()
            return True

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
            dm.status = DraftStatus.DISCARDED.value
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
