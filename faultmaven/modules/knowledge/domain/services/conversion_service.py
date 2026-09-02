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
from typing import Dict, List, Optional, Sequence, Tuple
from uuid import uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from faultmaven.config.tenant_context import writable_org_id
from faultmaven.exceptions import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ValidationException,
)
from faultmaven.infrastructure.llm.truncation import generate_with_truncation_retry
from faultmaven.infrastructure.persistence.models import (
    ConversionDraftModel,
    ConversionJobModel,
    KnowledgeItemModel,
    UploadedFileModel,
)
from faultmaven.modules.auth.contracts import is_team_member
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
from faultmaven.modules.knowledge.domain.services.runbook_validator import (
    VALID_SYMPTOM_CLASSES,
    QualityScorer,
    RunbookValidator,
)
from faultmaven.providers.tenancy.single_tenant import SingleTenantProvider
from faultmaven.utils.runbook_id import (
    RunbookPathEscape,
    draft_filename,
    knowledge_root,
    resolve_runbook_path,
    runbook_id_from_parts,
    safe_path_component,
    write_runbook_file,
)

logger = logging.getLogger(__name__)

# Output budget for one runbook conversion, and the cap the single truncation
# retry may raise it to (#1094). A full runbook — frontmatter, symptom
# recognition, diagnostic steps, causes, resolution — is a long document, so a
# genuine overrun is plausible here in a way it is not on short prose paths.
RUNBOOK_MAX_TOKENS = 4096
RUNBOOK_MAX_TOKENS_CEILING = RUNBOOK_MAX_TOKENS * 2

# Same pair for the analysis pass that precedes conversion.
ANALYSIS_MAX_TOKENS = 2048
ANALYSIS_MAX_TOKENS_CEILING = ANALYSIS_MAX_TOKENS * 2

# Single-tenant default organization. It is the *contextvar's* default (see
# ``config.tenant_context``), not a fallback any writer here applies directly:
# stamping this constant on a write is only correct in a single-tenant
# deployment, and under ``TENANT_PROVIDER=multi`` it is the sentinel org, which
# no tenant session may write (#1143). Writers resolve the org through
# :func:`writable_org_id` instead. No production code reads this any more; it
# stays exported because the tests name the single-tenant org by it.
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
- `symptom_class` values MUST come from this controlled vocabulary: __SYMPTOM_CLASS_VOCAB__. Choose the closest-fitting value(s); omit anything that doesn't fit (the runbook author uses free-text `tags` for long-tail symptoms). This is the same vocabulary the runbook frontmatter is validated against, and it keys failure-mode deduplication -- an off-vocabulary value here silently escapes both.

Respond with JSON matching this schema:
{
  "is_actionable": true/false,
  "failure_modes": [
    {
      "id": "kebab-case-id",
      "title": "Technology Failure Description",
      "domain": "database|networking|compute|application|security|storage|messaging",
      "service": "specific-service-name",
      "symptom_class": ["<one or more values from the controlled vocabulary above>"],
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

# Constrain the analysis LLM's `symptom_class` to the controlled vocabulary so the
# extracted value is already in-vocab: it keys failure-mode dedup
# (``_convert_all_failure_modes``) AND is validated in the produced frontmatter.
# Without this, analysis free-picks an off-vocab label, the conversion prompt
# (rule 9) later reclassifies it, and the dedup key no longer equals the persisted
# symptom_class — so two modes that classify to the same value slip dedup and yield
# duplicate runbooks. Sourced from the single VALID_SYMPTOM_CLASSES constant.
ANALYSIS_SYSTEM_PROMPT = ANALYSIS_SYSTEM_PROMPT.replace(
    "__SYMPTOM_CLASS_VOCAB__", ", ".join(VALID_SYMPTOM_CLASSES)
)

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
5. Statement ≤300 characters; each Chain rung ≤300 characters. Each complete ### Cause block (heading through its last Intervention) under 2800 characters — split a sprawling failure mode into separate Causes. Hard limits.
6. Each Indicator entry carries a rung ref (`root`, `s1`, …, or `D`) and at least one `[Step N]` (N matches an existing Diagnostic Step) or `[Symptom]`; the Cause Z fallback uses `- [Default]`.
7. Each Intervention is tagged with exactly one quadrant — `remediation` / `defensive_fix` / `mitigation` / `loop_break` — names the rung it targets in `(parens)`, and carries a **Verification:**; every `mitigation` also carries **Risk** and **Duration**.
8. If source material lacks enough information for a field, write "[INSUFFICIENT SOURCE DATA -- manual completion required]".
9. Use the `domain` and `service` values provided; do not change them. `symptom_class` MUST be one or more values from this controlled vocabulary: __SYMPTOM_CLASS_VOCAB__ — usually one; add another only if the failure mode genuinely spans a second class. Choose the closest fit to this failure mode (use any suggested value only as a starting point); never invent a value — put a long-tail symptom in `tags` instead."""

# Bind the controlled `symptom_class` vocabulary into the rules so the produced
# frontmatter is in-vocab for BOTH the document and case paths — the case path
# supplies no symptom_class taxonomy, so the model classifies here rather than
# emitting an off-vocab placeholder. RunbookValidator (the draft-validation gate)
# is the mechanical backstop if the model still strays off-vocab. Sourced from the
# single VALID_SYMPTOM_CLASSES constant so the prompt can't drift from the gate.
CONVERSION_SYSTEM_PROMPT = CONVERSION_SYSTEM_PROMPT.replace(
    "__SYMPTOM_CLASS_VOCAB__", ", ".join(VALID_SYMPTOM_CLASSES)
)


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


def _partition_failure_modes(
    failure_modes: List[FailureModeAnalysis],
) -> Tuple[List[FailureModeAnalysis], List[ConversionError]]:
    """Decide which of one job's failure modes can yield a runbook, and why not.

    Two keys, applied in order, both meaning "an earlier mode already covers
    this one". They are in ONE function because they are one decision — which
    modes survive — and because splitting them is what let the second key's
    outcome be reported while the first key's stayed silent:

    1. ``(service, sorted(symptom_class))`` — the pre-existing collapse. It is
       deliberately coarse: an analysis routinely emits near-duplicate modes for
       one document. **Kept as a product behaviour, not endorsed as a key** —
       it also collapses modes that mint DIFFERENT ids and are therefore
       genuinely different runbooks (``"PostgreSQL Lag A"`` / ``"Lag B"`` under
       one service and symptom class), which is a real loss. What changed here
       is that it no longer collapses them **silently**: before #1258's follow-up
       it dropped them with no error, no warning, and a COMPLETED status, so a
       document that produced one runbook out of two looked like a clean run.
       Now every dropped mode is accounted for by its own ``ConversionError``,
       exactly like key 2, which also makes the job's status PARTIAL — the
       truthful answer for a document that yielded fewer runbooks than it
       analysed into.
    2. The minted ``runbook_id`` (#1258). Migration 046 admits one live draft
       per ``(organization_id, runbook_id)``, and nothing is committed while a
       multi-mode conversion runs, so two modes minting one id were invisible to
       both ``refuse_if_draft_slot_taken`` and ``_raise_if_runbook_id_taken``.
       They wrote both runbooks to a single file (both ids resolve to one
       ``draft_filename``) and then failed the commit with a bare
       ``IntegrityError`` — a 500 that says nothing, after silent data loss.
       Measured on ``e1cf27371``: ``"Redis OOM"`` and ``"redis oom"`` under
       service ``"redis"`` both mint ``redis-redis-oom``.

    Called BEFORE any conversion runs, which is what makes key 2 a fix rather
    than a nicer error: ``runbook_id_from_parts`` is a pure function of
    ``(service, title)``, so the whole batch is knowable up front and a losing
    mode never spends an LLM call or touches the filesystem. It also cannot race
    — the parallel branch dispatches with ``asyncio.gather``, so a check made
    inside the per-mode coroutine would be concurrent readers of one
    unsynchronised set.

    **Key 2 refuses rather than disambiguating.** Minting a distinct id for the
    second mode — the way the empty-slug branch appends a hash — cannot be made
    deterministic, and determinism is not optional (the disk scan reconciles a
    file to its row by this id). A disambiguator must be a function of something
    that DIFFERS between the two modes; but "collide" means their
    ``(service, title)`` are identical after normalisation, and every other
    field on a ``FailureModeAnalysis`` is LLM output for this one analysis pass,
    so hashing any of them gives a different id on a re-run. An ordinal suffix
    is worse still: it keys on position in ``analysis.failure_modes``, the
    model's own ordering. And even given a stable disambiguator, minting one
    would persist two runbooks that every normalised signal says are the same
    failure mode — exactly the indistinguishable pair migration 046 exists to
    reject, recreated one hash apart.

    **Key 2 is shape-agnostic on purpose.** It compares minted IDS, not titles,
    so it covers every shape ``_slug`` collapses without enumerating any:
    case, punctuation, underscore/hyphen, whitespace runs, leading/trailing
    trim, tabs and control characters, NBSP and zero-width joiners, emoji,
    accents and non-latin scripts, full-width forms; the ``service``/``title``
    join, which makes the delimiter part of the data (``("redis-cache", "OOM")``
    and ``("redis", "cache OOM")`` both mint ``redis-cache-oom``); and the
    over-length branch, both when two titles differ only where the slug rule
    normalises and when two distinct long slugs land on the same 4-hex
    disambiguator. A future tightening of the slug rule is covered for free. The
    mint itself is deliberately untouched: it is a PERSISTED id, and re-minting
    it would orphan rows that already exist — the boundary #1230 and #1243 drew.

    Keying on the id ALONE is safe because ``draft_filename`` is injective over
    the ids this mint produces: they match ``^[a-z0-9]+(-[a-z0-9]+)*$`` and are
    bounded by ``_MAX_RUNBOOK_ID_CHARS``, which is ``<= _MAX_SLUG_CHARS``, so
    ``draft_filename`` neither re-slugs nor truncates them. That is a property
    of two constants that are deliberately SEPARATE, so it is pinned by a test
    rather than trusted; a second index over filenames here would be dead code
    whose failure mode is looking like coverage.
    """
    survivors: List[FailureModeAnalysis] = []
    errors: List[ConversionError] = []
    claimed_coarse: Dict[Tuple[str, Tuple[str, ...]], FailureModeAnalysis] = {}
    claimed_id: Dict[str, FailureModeAnalysis] = {}

    for fm in failure_modes:
        coarse_key = (fm.service, tuple(sorted(fm.symptom_class)))
        # ``is not None``, never truthiness: the value is a model instance and
        # ``FailureModeAnalysis`` does not promise to be truthy. Same rule
        # ``_find_live_draft_owning`` states for its own id filter, and for the
        # same reason — a falsy-but-present value would read as "free slot".
        holder = claimed_coarse.get(coarse_key)
        if holder is not None:
            errors.append(
                ConversionError(
                    failure_mode_id=fm.id,
                    error=(
                        f"Failure mode {fm.id!r} ({fm.title!r}) was collapsed "
                        f"into {holder.id!r} ({holder.title!r}): both describe "
                        f"service {fm.service!r} with symptom class "
                        f"{sorted(fm.symptom_class)!r}, and this conversion "
                        f"produces one runbook per (service, symptom class). "
                        f"Give them distinct symptom classes in the source "
                        f"document if they are genuinely different failures."
                    ),
                    retryable=False,
                )
            )
            continue

        # The same pure function ``_convert_single_failure_mode`` calls, so the
        # id decided here and the id it mints for itself agree by construction
        # rather than by a parameter that could drift.
        runbook_id = generate_runbook_id(fm)
        holder = claimed_id.get(runbook_id)
        if holder is not None:
            errors.append(
                ConversionError(
                    failure_mode_id=fm.id,
                    error=(
                        f"Runbook id {runbook_id!r} was already claimed in this "
                        f"conversion by failure mode {holder.id!r} "
                        f"(service {holder.service!r}, title {holder.title!r}). "
                        f"This failure mode (service {fm.service!r}, title "
                        f"{fm.title!r}) mints the same id, because the id is "
                        f"derived from service and title with case, punctuation, "
                        f"whitespace and non-latin characters normalised away — "
                        f"so the two would produce one runbook, not two. Give "
                        f"them distinct titles in the source document, or merge "
                        f"them into a single failure mode."
                    ),
                    # Nothing about retrying frees the id — the same wording and
                    # the same reason as the committed-duplicate branch in
                    # ``_convert_single_failure_mode``.
                    retryable=False,
                )
            )
            continue

        claimed_coarse[coarse_key] = fm
        claimed_id[runbook_id] = fm
        survivors.append(fm)

    return survivors, errors


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
        team_service=None,
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
        # Membership resolver for the team publish target (#854). None →
        # teams don't exist in this deployment, so a team-scoped publish is
        # refused rather than silently minting an unresolvable share target.
        self._team_service = team_service
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
        # Delegates so this service and ``KnowledgeService`` cannot drift apart
        # on where the knowledge tree is; that agreement is what every
        # containment check below is anchored on.
        return knowledge_root()

    #: ``conflict_reason`` for a draft row whose ``file_path`` is not inside the
    #: knowledge tree. Structured so ``verify_batch`` and any client can key on
    #: the field rather than parse a message (the same rule #784 established for
    #: ``already_verified``).
    PATH_ESCAPE_CONFLICT_REASON = "path_outside_knowledge_tree"

    def _refuse_escaping_draft(
        self, draft_id: str, exc: RunbookPathEscape
    ) -> ConflictError:
        """Translate a containment refusal into the module's typed exception.

        Two jobs, and the split between them is the point:

        * the **log** gets ``exc`` in full — the resolved absolute paths are
          what an operator needs to repair the row; and
        * the **client** gets a message naming the draft id and the refusal
          class and nothing else. ``str()`` of this exception reaches a response
          body two ways — the 409 handler's ``detail``, and ``verify_batch``'s
          per-item ``error`` — and echoing a server filesystem path into either
          is the disclosure #866 closed for this module.

        ``ConflictError`` rather than a bare ``ValueError``: the row is in a
        state the operation cannot proceed from, which is what 409 means, and
        ``verify_draft`` already documents that every failure shape here is a
        typed exception (a raw ``ValueError`` would surface as an unmapped 500).
        """
        logger.error("refusing a filesystem operation on draft %s: %s", draft_id, exc)
        return ConflictError(
            f"Draft {draft_id} references a runbook file outside the knowledge "
            "tree and cannot be read or modified. The stored path must be "
            "repaired by an operator; see the server log for details.",
            resource_type="draft",
            resource_id=draft_id,
            conflict_reason=self.PATH_ESCAPE_CONFLICT_REASON,
        )

    def _scope_dir(self, scope: str, team_id: str = None, user_id: str = None) -> Path:
        """Scope directory for a draft, with both id components sanitised.

        #1213: these are interpolated into a directory NAME. They come from the
        auth context rather than a request body, so they are a lower-risk source
        than a title — but the same shape bit ``KnowledgeService.upload_document``,
        where a ``user_id`` of ``../../../../escaped`` sent the write outside
        ``data/knowledge`` entirely. ``safe_path_component`` reduces each to one
        segment, so the layout (``global/``, ``team_*/``, ``user_*/``) that the
        scan pass infers scope from is preserved while an escape is
        unconstructible.
        """
        if scope == "global":
            return self._data_dir / "global"
        elif scope == "team" and team_id:
            return self._data_dir / f"team_{safe_path_component(team_id)}"
        elif scope == "personal" and user_id:
            return self._data_dir / f"user_{safe_path_component(user_id)}"
        return self._data_dir / "global"

    async def _ensure_team_publish_allowed(
        self, scope: str, team_id: Optional[str], user_id: str
    ) -> None:
        """Refuse a team publish target the caller may not use (#854).

        A ``team_id`` becomes a ``resource_shares`` row on verify — content
        injected into that team's knowledge scope — so it must name a team the
        caller belongs to, the same rule ``CaseService.share_case_with_team``
        enforces (via the shared ``is_team_member`` predicate). Runs at mint
        time, the single point the caller-supplied value enters the pipeline.

        Raises:
            ValidationException: teams are unavailable in this deployment
                (no team service wired — standalone), so a team-scoped
                publish cannot be honored.
            AuthorizationError: the caller is not a member of ``team_id``.
        """
        if scope != "team":
            return
        if not self._team_service:
            raise ValidationException(
                "Team publishing is not available in this deployment"
            )
        if not await is_team_member(self._team_service, user_id, team_id):
            raise AuthorizationError(
                "You can only publish a runbook to a team you belong to"
            )

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
        await self._ensure_team_publish_allowed(scope, team_id, user_id)

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
            organization_id=organization_id,
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
            warnings=warnings,
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
        await self._ensure_team_publish_allowed(request.scope, team_id, user_id)

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
                "in the Dashboard under Knowledge Base > Drafts.",
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
            # A case carries no symptom_class taxonomy, so leave it empty when the
            # request omits it — the conversion prompt classifies into the
            # controlled vocabulary (rule 9). Never inject an off-vocab placeholder
            # like ``["unknown"]``: it fails the RunbookValidator symptom_class gate.
            symptom_class=request.symptom_class or [],
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
        if request.root_cause_conditions:
            # Co-necessary with the cause above, not optional extras (#1096):
            # the runbook's Cause must record every condition the problem
            # required, or the next investigator reads a one-factor cause.
            conditions = "\n".join(f"- {c}" for c in request.root_cause_conditions)
            source_parts.append(f"CONDITIONS THE CAUSE ALSO REQUIRED:\n{conditions}")
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
            organization_id=organization_id,
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

        # Persist to database with source_type and case_id. The unique index on
        # ``conversion_jobs.live_case_id`` is the cross-replica dedup backstop:
        # if another replica committed a live case-conversion for this case while
        # this one ran the LLM, the commit raises IntegrityError. ``_persist_job``
        # writes the upload row + job + drafts in ONE session, so that failed
        # commit rolls the whole unit back — no orphan rows. Mirror the
        # in-process dedup semantics (a concurrent caller awaits the winner's
        # task and receives the winner's response) by returning the winner's
        # conversion. The typed exception plus a confirming re-read is the
        # discriminator — never classify by matching the exception message.
        #
        # ``ConflictError`` is caught alongside ``IntegrityError`` because
        # migration 046 made the two indistinguishable at the commit:
        # ``_persist_job`` re-reads on any IntegrityError and, in exactly this
        # race, FINDS the winner's drafts — two replicas converting one case
        # mint the same ``(service, title)`` ids — so it reports a runbook_id
        # duplicate for what is really the live-case race. Catching only
        # IntegrityError would have handed the loser a 409 instead of the
        # winner's conversion. The re-read below is the discriminator that does
        # tell them apart; anything it cannot confirm is re-raised unchanged,
        # so a genuine duplicate from a DIFFERENT case still surfaces as its
        # 409.
        try:
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
                warnings=warnings,
            )
        except (IntegrityError, ConflictError):
            logger.warning(
                "case_conversion_cross_replica_dedup",
                extra={"case_id": request.case_id},
            )
            existing = await self.get_conversion_by_case(request.case_id, user_id)
            if existing and existing.has_live_draft():
                return existing
            raise

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

    def _knowledge_route_kwargs(self) -> dict:
        """``{"provider_override": <name>}`` when KNOWLEDGE_PROVIDER is set,
        else ``{}`` — the kwarg is added only when the role provider is
        explicitly configured, so the unset case is byte-identical to before
        role routing and duck-typed routers without the parameter keep
        working."""
        override = self._settings.llm.explicit_role_provider("knowledge")
        return {"provider_override": override} if override else {}

    async def _analyze_document(self, text: str, filename: str) -> AnalysisResult:
        """Analyze document for failure modes using KNOWLEDGE_PROVIDER."""
        knowledge_model = self._settings.llm.get_knowledge_model()

        async def _analyze(cap: int):
            return await self._llm_router.route(
                messages=[
                    {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT},
                    {"role": "user", "content": f"Analyze this document:\n\n{text}"},
                ],
                model=knowledge_model,
                max_tokens=cap,
                temperature=0.2,
                response_format={"type": "json_object"},
                # Land on KNOWLEDGE_PROVIDER when the operator set one — the
                # model alone doesn't route (kwarg only when set, so
                # duck-typed routers keep working).
                **self._knowledge_route_kwargs(),
            )

        # A document with many failure modes can genuinely outgrow the budget.
        # This path already failed LOUDLY on a cut body — the JSON does not
        # parse — which was the right shape but the wrong recovery: it reported
        # "could not be parsed" for a document that simply needed more room, and
        # a retry with the same cap could never differ. Raise the cap once, and
        # say what actually happened if it is still cut (#1094).
        response = await generate_with_truncation_retry(
            _analyze,
            max_tokens=ANALYSIS_MAX_TOKENS,
            ceiling=ANALYSIS_MAX_TOKENS_CEILING,
            label=f"document analysis ({filename})",
        )

        if response.is_truncated:
            raise ConversionRejectedError(
                "LLM analysis response was truncated at the output limit "
                f"({ANALYSIS_MAX_TOKENS_CEILING} tokens); the document may "
                "contain too many failure modes for a single analysis pass",
                error_code=ConversionErrorCode.LLM_PARSE_ERROR,
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
        organization_id: str = None,
    ) -> Tuple[List[ConversionDraft], List[ConversionError]]:
        """Convert all failure modes, parallel for <=5, sequential for 6+."""
        drafts: List[ConversionDraft] = []
        errors: List[ConversionError] = []

        # Which modes can yield a runbook at all — both intra-job keys, with a
        # per-mode ``ConversionError`` for every one that cannot. See
        # ``_partition_failure_modes``.
        unique_modes, partition_errors = _partition_failure_modes(failure_modes)
        errors.extend(partition_errors)

        # And which of the survivors are refused by a draft that is ALREADY
        # COMMITTED. The same argument that moved the intra-job check ahead of
        # the LLM calls applies verbatim here: the ids are knowable up front, so
        # a mode whose id some live draft already holds should not spend a
        # generation first. Re-converting a document whose modes all already
        # have drafts used to burn one full generation per mode before refusing
        # each one; this refuses them all in a single query.
        #
        # This does NOT replace ``refuse_if_draft_slot_taken`` inside
        # ``_convert_single_failure_mode``. That one is the authoritative
        # pre-write guard and still keys on the resolved file path as well as
        # the id; it also closes the window between this query and the write,
        # which is exactly the cross-replica race migration 046 backstops. This
        # is a cost pre-filter in front of it, and the two agree because both
        # ask ``_find_live_draft_owning``.
        unique_modes, taken_errors = await self._refuse_modes_whose_id_is_taken(
            unique_modes, organization_id
        )
        errors.extend(taken_errors)

        # The concurrency decision is a property of the DOCUMENT — how many
        # failure modes it analysed into — not of how many of them turned out to
        # be duplicates. Keying it on the survivor count let a collision flip a
        # 6-mode document from the sequential rate-limit-avoiding path to
        # concurrent dispatch, which is a provider-load decision made by the
        # model's choice of titles. ``len(failure_modes)`` is also the
        # conservative direction: it is never smaller than the survivor count,
        # so this can only ever choose sequential where the old expression chose
        # parallel.
        if len(failure_modes) < PARALLEL_THRESHOLD:
            # Parallel conversion
            tasks = [
                self._convert_single_failure_mode(
                    text,
                    fm,
                    scope,
                    filename,
                    conversion_id,
                    user_id,
                    team_id,
                    organization_id=organization_id,
                )
                for fm in unique_modes
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for i, result in enumerate(results):
                if isinstance(result, RunbookPathEscape):
                    # Re-raised, never laundered into a ``ConversionError``.
                    # ``return_exceptions=True`` turns the deliberate
                    # ``except RunbookPathEscape: raise`` in
                    # ``_convert_single_failure_mode`` into a returned value,
                    # and appending ``str(result)`` here would put the resolved
                    # SERVER PATHS the message carries into ``response.warnings``
                    # — a 200/201 body (#866), which is precisely what that
                    # bare re-raise exists to prevent. The sequential branch
                    # below propagates it, so without this the same event
                    # behaved differently purely on failure-mode count.
                    raise result
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
                    text,
                    fm,
                    scope,
                    filename,
                    conversion_id,
                    user_id,
                    team_id,
                    organization_id=organization_id,
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
        organization_id: str = None,
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
                f"SYMPTOM_CLASS: {', '.join(failure_mode.symptom_class) or '(none supplied — classify from the controlled vocabulary in rule 9)'}\n"
                f"SEVERITY: {failure_mode.severity}\n"
                f"SCOPE: {scope}\n"
                f"SOURCE FILENAME: {filename}\n"
                f"TODAY: {today_iso}\n\n"
                f"The frontmatter `id` field MUST be exactly: {runbook_id}\n"
                f"(lowercase, kebab-case; do not derive a different id from "
                f"the title).\n\n"
                f"--- SOURCE MATERIAL ---\n{text}\n--- END SOURCE MATERIAL ---"
            )

            async def _convert(cap: int):
                return await self._llm_router.route(
                    messages=[
                        {"role": "system", "content": CONVERSION_SYSTEM_PROMPT},
                        {"role": "user", "content": user_message},
                    ],
                    model=knowledge_model,
                    max_tokens=cap,
                    temperature=0.3,
                    # Same KNOWLEDGE_PROVIDER routing as _analyze_document.
                    **self._knowledge_route_kwargs(),
                )

            response = await generate_with_truncation_retry(
                _convert,
                max_tokens=RUNBOOK_MAX_TOKENS,
                ceiling=RUNBOOK_MAX_TOKENS_CEILING,
                label=f"runbook conversion ({failure_mode.id})",
            )

            runbook_content = response.content.strip()

            # A runbook cut mid-procedure is complete-or-nothing.
            #
            # This is the one consumer where a partial is worse than an error.
            # The output is PERSISTED as a KB document and later retrieved to
            # drive other investigations, so a half-procedure ships as an
            # authoritative one — the reader has no way to tell that step 4 of 7
            # is missing rather than absent by design. And it passes every
            # validator below: a cut body still has frontmatter delimiters, is
            # far longer than 100 characters, and (since the sections are
            # written in order) still carries the required headings. Nothing
            # after this point can catch it, which is precisely why the check
            # belongs here (#1094).
            if response.is_truncated:
                return ConversionError(
                    failure_mode_id=failure_mode.id,
                    error=(
                        "LLM response was truncated at the output limit "
                        f"({RUNBOOK_MAX_TOKENS_CEILING} tokens) — refusing to "
                        "persist an incomplete runbook"
                    ),
                    retryable=True,
                )

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

            # Write draft to disk. Through the shared helper: it validates
            # containment against the ROOT of the knowledge tree and does so
            # BEFORE creating the scope directory. ``runbook_id`` is minted
            # from an allowlist so an escape is unconstructible today — the
            # guard is what keeps that true if the mint rule is loosened or a
            # new caller assembles its own name (#1213 follow-up).
            draft_path = self._scope_dir(scope, team_id, user_id) / draft_filename(
                runbook_id
            )

            # BEFORE the write. The path is derived from ``runbook_id``, so a
            # duplicate lands on the EXISTING draft's file and would replace
            # its content on the way to an INSERT migration 046 rejects. See
            # ``refuse_if_draft_slot_taken``.
            await self.refuse_if_draft_slot_taken(
                organization_id, runbook_id, str(draft_path)
            )

            write_runbook_file(
                draft_path,
                runbook_content,
                source=f"converted draft (runbook_id={runbook_id})",
                root=self._data_dir,
            )

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

        except RunbookPathEscape:
            # Never laundered into a generic ConversionError: that would put the
            # resolved server paths into ``error`` (a 200 response body, see
            # #866) and would report a containment refusal as a retryable
            # conversion failure. Today the mint makes this unreachable — which
            # is exactly why it must not be swallowed if that ever changes.
            raise
        except ConflictError as exc:
            # Deliberately the OPPOSITE call to the one above, and spelled out
            # rather than left to fall through: a taken runbook id is a fact
            # about ONE failure mode, and a document analysed into five of them
            # should still yield the other four. So it degrades to this mode's
            # ``ConversionError`` — which the response already carries per mode
            # — instead of failing the whole conversion. Not retryable: nothing
            # about retrying frees the id.
            logger.warning(
                "conversion_draft_id_taken",
                extra={"failure_mode_id": failure_mode.id, "reason": str(exc)},
            )
            return ConversionError(
                failure_mode_id=failure_mode.id, error=str(exc), retryable=False
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
        warnings: Optional[List[str]] = None,
    ) -> None:
        """Persist conversion job and drafts to database."""
        if not self._db_session_factory:
            return

        org_id = writable_org_id(organization_id)

        try:
            await self._persist_job_rows(
                org_id=org_id,
                conversion_id=conversion_id,
                user_id=user_id,
                scope=scope,
                status=status,
                source_file=source_file,
                analysis=analysis,
                drafts=drafts,
                created_at=created_at,
                source_type=source_type,
                case_id=case_id,
                warnings=warnings,
            )
        except IntegrityError:
            # Classified AFTER the session block has exited, never inside it:
            # the classifier opens a SECOND session, and holding both at once
            # deadlocks a deployment pooled at one connection.
            await self._raise_if_runbook_id_taken(
                org_id, [d.runbook_id for d in drafts]
            )
            raise

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

    async def _persist_job_rows(
        self,
        *,
        org_id: str,
        conversion_id: str,
        user_id: str,
        scope: str,
        status: ConversionStatus,
        source_file: SourceFileInfo,
        analysis: AnalysisResult,
        drafts: List[ConversionDraft],
        created_at: datetime,
        source_type: str,
        case_id: Optional[str],
        warnings: Optional[List[str]] = None,
    ) -> None:
        """The upload + job + drafts write, as ONE transaction and one session.

        Split out of ``_persist_job`` only so its caller can classify an
        ``IntegrityError`` after this session has been returned to the pool.
        """
        async with self._db_session_factory() as session:
            # ``conversion_jobs`` carries a single ``source_file_id`` FK to
            # ``uploaded_files`` (ON DELETE RESTRICT). Create the upload row
            # first; the conversion_jobs row references it. Both tables
            # require organization_id NOT NULL.
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

            # ``live_case_id`` holds the case only while this case-source job has
            # a live (non-discarded) draft; it is the value the unique index
            # dedups on. Freshly generated drafts are DRAFT (live), a failed
            # conversion persists zero drafts (never blocks regeneration), and
            # document jobs carry no case — all three resolve to NULL here.
            live_case_id = (
                case_id
                if source_type == "case"
                and case_id
                and any(d.status != DraftStatus.DISCARDED for d in drafts)
                else None
            )

            job = ConversionJobModel(
                id=conversion_id,
                user_id=user_id,
                organization_id=org_id,
                scope=scope,
                status=status.value,
                source_file_id=source_file_id,
                source_type=source_type,
                case_id=case_id,
                live_case_id=live_case_id,
                failure_modes_detected=len(analysis.failure_modes),
                analysis_result=analysis.model_dump(),
                # ``None`` rather than ``[]`` when there is nothing to say, so a
                # job with no warnings and a pre-048 job are not conflated at
                # the storage layer (migration 048).
                warnings=list(warnings) if warnings else None,
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

    async def _find_live_draft_owning(
        self, org_id: str, runbook_ids: Sequence[Optional[str]], file_path: str = None
    ) -> Optional[Tuple[str, str, str]]:
        """``(runbook_id, file_path, draft_id)`` of a live draft already holding
        one of these ids OR this file, in this tenant. ``None`` if the slot is
        free.

        Two keys, one query, because a draft occupies two slots and losing
        either one loses a runbook:

        - its ``runbook_id``, which migration 046 makes unique per tenant; and
        - its ``file_path``, which is NOT in that index and does not follow
          from the id. ``draft_filename`` runs the id back through ``_slug``,
          so ``"foo--bar"`` and ``"foo-bar"`` resolve to one ``foo-bar.md``.
          A legacy row holding the double-hyphen form — which the mint could
          produce before #1243 — is invisible to an id lookup, and the write would
          clobber its file while the INSERT sailed past the index. Measured on
          the dev database: 0 rows of that shape today, so this is a guard
          against a state I could not reproduce rather than one I observed —
          which is exactly the case for keying on the resolved slot rather than
          on the id that was supposed to imply it.

        Ordered, and deliberately not ``LIMIT 1`` on an unordered scan: the
        message names a specific row, and naming a different row on each call
        makes it unactionable.
        """
        if not self._db_session_factory:
            return None
        # ``is not None``, NOT truthiness: an empty ``runbook_id`` is a real
        # (legacy) value that collides with every other empty one — the very
        # shape #1230 reported — and dropping it here let the raw
        # ``IntegrityError`` escape past every caller written to catch the
        # typed refusal.
        ids = [rid for rid in runbook_ids if rid is not None]
        if not ids and file_path is None:
            return None
        conditions = []
        if ids:
            conditions.append(ConversionDraftModel.runbook_id.in_(ids))
        if file_path is not None:
            conditions.append(ConversionDraftModel.file_path == file_path)
        async with self._db_session_factory() as probe:
            result = await probe.execute(
                select(
                    ConversionDraftModel.runbook_id,
                    ConversionDraftModel.file_path,
                    ConversionDraftModel.id,
                )
                .where(ConversionDraftModel.organization_id == org_id)
                .where(or_(*conditions))
                .where(ConversionDraftModel.status != "discarded")
                .order_by(ConversionDraftModel.created_at, ConversionDraftModel.id)
                .limit(1)
            )
            return result.first()

    @staticmethod
    def _duplicate_draft_conflict(taken: Tuple[str, str, str]) -> ConflictError:
        """The 409 for a slot already held. One wording, both call sites."""
        runbook_id, file_path, draft_id = taken
        return ConflictError(
            f"A runbook draft with id '{runbook_id}' already exists in this "
            f"organization (draft {draft_id}, {file_path}). Discard it before "
            "creating another with the same service and title — verifying it "
            "does not release the id.",
            resource_type="conversion_draft",
            resource_id=draft_id,
            conflict_reason="duplicate_runbook_id",
        )

    async def _refuse_modes_whose_id_is_taken(
        self,
        failure_modes: List[FailureModeAnalysis],
        organization_id: Optional[str],
    ) -> Tuple[List[FailureModeAnalysis], List[ConversionError]]:
        """Drop the modes whose minted id a LIVE draft already holds, in ONE query.

        The cost pre-filter described at the call site. ``refuse_if_draft_slot_taken``
        asks the same question one id at a time, from inside the per-mode
        coroutine — i.e. after that mode's generation has already been paid for.
        Both remain: this one saves the call, that one is the authoritative
        pre-write guard and closes the race this query cannot.

        Degrades per mode, exactly like the committed-duplicate branch in
        ``_convert_single_failure_mode``: a taken id is a fact about ONE failure
        mode, and a document analysed into five of them should still yield the
        other four. The wording is ``_duplicate_draft_conflict``'s, so a
        duplicate refused here and one refused at the write site read the same.

        Returns ``(survivors, errors)``. Inert with no database (nothing is
        written, so nothing can be taken) — the same early return
        ``refuse_if_draft_slot_taken`` and ``_persist_job`` make, and for the
        same reason: ``writable_org_id`` raises on an unscoped context, which
        must not become a failure on a path that writes nothing.
        """
        if not self._db_session_factory or not failure_modes:
            return list(failure_modes), []

        minted = {fm.id: generate_runbook_id(fm) for fm in failure_modes}
        taken = await self._find_live_drafts_owning(
            writable_org_id(organization_id), list(minted.values())
        )
        if not taken:
            return list(failure_modes), []

        survivors: List[FailureModeAnalysis] = []
        errors: List[ConversionError] = []
        for fm in failure_modes:
            row = taken.get(minted[fm.id])
            if row is None:
                survivors.append(fm)
                continue
            logger.warning(
                "conversion_draft_id_taken",
                extra={"failure_mode_id": fm.id, "runbook_id": minted[fm.id]},
            )
            errors.append(
                ConversionError(
                    failure_mode_id=fm.id,
                    error=str(self._duplicate_draft_conflict(row)),
                    retryable=False,
                )
            )
        return survivors, errors

    async def _find_live_drafts_owning(
        self, org_id: str, runbook_ids: Sequence[Optional[str]]
    ) -> Dict[str, Tuple[str, str, str]]:
        """``{runbook_id: (runbook_id, file_path, draft_id)}`` for every live
        draft in this tenant holding one of these ids.

        The batch form of ``_find_live_draft_owning``. That one answers "is this
        ONE slot free" and stops at the first row, which is right for a
        pre-write guard and wrong for a pre-flight over a whole batch: five
        taken ids would need five queries, or one query that names only one of
        them.

        Ordered, and the FIRST row per id wins, so the draft this names is the
        same one ``_find_live_draft_owning`` would name for that id — the two
        refusals must not point at different rows for the same collision.
        """
        ids = [rid for rid in runbook_ids if rid is not None]
        if not ids:
            return {}
        async with self._db_session_factory() as probe:
            result = await probe.execute(
                select(
                    ConversionDraftModel.runbook_id,
                    ConversionDraftModel.file_path,
                    ConversionDraftModel.id,
                )
                .where(ConversionDraftModel.organization_id == org_id)
                .where(ConversionDraftModel.runbook_id.in_(ids))
                .where(ConversionDraftModel.status != "discarded")
                .order_by(ConversionDraftModel.created_at, ConversionDraftModel.id)
            )
            found: Dict[str, Tuple[str, str, str]] = {}
            for row in result.all():
                found.setdefault(row[0], (row[0], row[1], row[2]))
            return found

    async def refuse_if_draft_slot_taken(
        self, organization_id: Optional[str], runbook_id: str, draft_path: str
    ) -> None:
        """Refuse BEFORE writing, on every path that mints a NEW draft file.

        The draft file is named after ``runbook_id``, so a duplicate resolves
        to the SAME path. Writing first and letting migration 046 reject the
        INSERT leaves the EXISTING draft's row pointing at the new author's
        content — a worse state than the duplicate rows the index removes,
        because the surviving row then lies about its own file.

        Both new-draft write paths call this: the LLM conversion
        (``_generate_runbook_draft``) and the manual template create. The edit
        path (``update_draft``) does NOT and must not — it rewrites the file
        its own row already owns, so the row it would "conflict" with is
        itself.

        This is the ordinary case; the index stays the backstop for the genuine
        cross-replica race, which is what an index is for.

        Takes the RAW ``organization_id`` and resolves it here, after the
        factory check — unlike ``_raise_if_runbook_id_taken``, whose only caller
        has already resolved it. ``writable_org_id`` raises on an unscoped
        context, and evaluating it at the call site would make that a failure on
        a path with no database, which writes nothing and has no index to
        honour. Mirrors ``_persist_job``'s own early return.
        """
        if not self._db_session_factory:
            return
        taken = await self._find_live_draft_owning(
            writable_org_id(organization_id), [runbook_id], file_path=draft_path
        )
        if taken:
            raise self._duplicate_draft_conflict(taken)

    async def _raise_if_runbook_id_taken(
        self, org_id: str, runbook_ids: Sequence[Optional[str]]
    ) -> None:
        """Translate the 046 unique-index violation into a 409, or return.

        ``uq_conversion_drafts_org_runbook_id`` (migration 046) admits one LIVE
        draft per ``(organization_id, runbook_id)``. Two drafts reaching the
        same id is ordinary — ``runbook_id_from_parts`` is deterministic on
        ``(service, title)``, deliberately, because the disk scan reconciles a
        file to its row by that id — so a user converting the same source
        twice, or two cases about the same failure, lands here. Without this
        the whole commit surfaces as an unhandled ``IntegrityError``, i.e. a
        500 that says nothing.

        This is the BACKSTOP. ``refuse_if_draft_slot_taken`` catches the
        ordinary case before anything is written; what reaches here is a race,
        or a shape the pre-check could not see.

        Classification is by a **confirming re-read**, never by matching the
        exception's message: the same commit also carries
        ``uq_conversion_jobs_live_case_id``. That one is NOT distinguishable
        from a runbook_id duplicate by re-read alone — two replicas converting
        the same case produce the same ``(service, title)`` pairs and therefore
        the same ids, so this re-read finds the winner's drafts and raises a
        409 for what is really the live-case race. ``convert_from_case``
        therefore catches ``ConflictError`` as well as ``IntegrityError`` and
        resolves it with ITS OWN confirming re-read
        (``get_conversion_by_case``), which is the discriminator that actually
        distinguishes the two. Anything it cannot confirm it re-raises.

        Two drafts in ONE job colliding with each other no longer reaches here
        (#1258), and could never have been resolved here: nothing is committed,
        so the re-read finds nothing, this returns, and the caller re-raises the
        bare ``IntegrityError`` — a 500 that says nothing, after the second
        draft's write has already replaced the first one's file (both ids
        resolve to one ``draft_filename``). It is refused where the duplicate is
        produced instead: ``_partition_failure_modes`` mints every id in the
        batch before any conversion runs and degrades each repeat to that
        failure mode's ``ConversionError``, so every draft list reaching
        ``_persist_job`` carries distinct ids and what arrives here is a
        cross-job duplicate or the live-case race.
        """
        taken = await self._find_live_draft_owning(org_id, runbook_ids)
        if taken:
            raise self._duplicate_draft_conflict(taken)

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
                # Read content from disk. Guarded like the write paths — an
                # escaped row here would put an arbitrary file's contents into
                # the API response. This is the one caller that degrades rather
                # than refuses: one bad row must not deny the whole listing, so
                # the escape is logged and that draft's content is omitted
                # (which is already what an unreadable file does here).
                content = None
                try:
                    resolved = resolve_runbook_path(
                        dm.file_path,
                        source=f"conversion_drafts.file_path (draft_id={dm.id})",
                        root=self._data_dir,
                    )
                    content = resolved.read_text(encoding="utf-8")
                except RunbookPathEscape as exc:
                    logger.error(
                        "refusing to read draft %s; omitting its content: %s",
                        dm.id,
                        exc,
                    )
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
                # NULL (a pre-048 row, or a job with nothing to report) becomes
                # ``[]`` here because the response field is non-optional. This
                # is the one place that distinction is collapsed; see migration
                # 048. Without this the field was ALWAYS ``[]`` on read-back, so
                # the reason a job was PARTIAL survived exactly one response.
                warnings=job.warnings or [],
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
        is_platform_admin: bool = False,
    ) -> Optional[ConversionDraft]:
        """Update draft content, re-validate, and re-score.

        ``is_platform_admin`` gates editing at ``global`` scope (#785): a global
        draft is pre-verification platform-corpus content, and letting any
        authenticated user shape what an admin later verifies is the hardening
        hole this closes. Same policy and placement as :meth:`verify_draft` —
        the scope is only known once the job row is loaded, and the gate applies
        regardless of job ownership (a "system"-owned global draft from a disk
        scan included). Defaults ``False`` (fail-closed).
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
                return None

            if job.scope == "global":
                ensure_global_authoring_allowed(is_platform_admin)

            draft_result = await session.execute(
                select(ConversionDraftModel).where(
                    ConversionDraftModel.id == draft_id,
                    ConversionDraftModel.conversion_id == conversion_id,
                )
            )
            dm = draft_result.scalar_one_or_none()
            if not dm or dm.status == DraftStatus.DISCARDED.value:
                return None

            # Write updated content to disk.
            #
            # ``dm.file_path`` comes straight back out of the database. Every
            # mint point that produces it is sanitised now, but a row persisted
            # BEFORE #1215 was written by a mint that could escape, and this
            # edit path would re-open and rewrite it without ever re-checking.
            # Re-validate on use: containment is a property of the path at the
            # moment it is used, not of the code that happened to create it.
            # Refuses as a typed 409 naming the row; the resolved paths go to
            # the log, never to the client (#1213 follow-up, see #866).
            try:
                write_runbook_file(
                    dm.file_path,
                    content,
                    source=f"conversion_drafts.file_path (draft_id={dm.id})",
                    root=self._data_dir,
                )
            except RunbookPathEscape as exc:
                raise self._refuse_escaping_draft(dm.id, exc) from exc

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
        is_platform_admin: bool = False,
    ) -> dict:
        """Verify multiple drafts sequentially. Returns summary with per-item status.

        ``is_platform_admin`` is threaded into each :meth:`verify_draft` so the global-tier
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
                    is_platform_admin=is_platform_admin,
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
        is_platform_admin: bool = False,
    ) -> Optional[VerifyResponse]:
        """Promote draft to verified status, update frontmatter, trigger ingestion.

        ``is_platform_admin`` gates publication at ``global`` scope: verifying a draft
        ingests it into the KB at ``job.scope``, and a global runbook is the
        org-free platform corpus (readable by every tenant), so
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
                ensure_global_authoring_allowed(is_platform_admin)

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

            # Update frontmatter on disk using python-frontmatter.
            #
            # Resolved through the shared guard FIRST, before any read or write:
            # this path is a database value, and on this method it is read back
            # into the response as well as rewritten, so an escaping row would
            # both leak an arbitrary file and be overwritten. Refuses as a typed
            # 409 naming the row — the same exception contract every other
            # failure shape on this method already follows (#1213 follow-up).
            try:
                file_path = resolve_runbook_path(
                    dm.file_path,
                    source=f"conversion_drafts.file_path (draft_id={dm.id})",
                    root=self._data_dir,
                )
            except RunbookPathEscape as exc:
                raise self._refuse_escaping_draft(dm.id, exc) from exc
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

            # Transfer the job's team publish target (a share row on the
            # conversion_job) to the promoted knowledge_item — ingest_runbook
            # creates the item's own share row. Replaces the retired
            # conversion_jobs.team_id column (ADR-013 §D4).
            team_id = await self._resolve_job_team_id(conversion_id)
            # Membership was checked when the target was minted (#854), but
            # THIS is where it takes effect as a knowledge_item share — and
            # the verifier may differ from the minter ("system" jobs) or have
            # left the team since. Re-check fail-closed at the point of effect.
            if team_id and not await is_team_member(
                self._team_service, user_id, team_id
            ):
                raise AuthorizationError(
                    "You can only publish a runbook to a team you belong to"
                )
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
        await self._ensure_team_publish_allowed(scope, team_id, user_id)

        today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Generate kebab-case ID. Shared mint point with the LLM conversion
        # path's ``generate_runbook_id`` (#1213 follow-up); the inline copy this
        # replaces was byte-identical, which a differential test pins.
        runbook_id = runbook_id_from_parts(service_name, title)

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

        # Write to disk through the shared containment-checked helper — same
        # anchor, same before-mkdir ordering as every other runbook write.
        draft_path = self._scope_dir(scope, team_id, user_id) / draft_filename(
            runbook_id
        )

        # BEFORE the write, for the reason on ``refuse_if_draft_slot_taken``.
        # Unlike the LLM path this does NOT degrade to a per-mode error: a
        # manual create is one runbook, so refusing it IS the answer, and the
        # caller gets the 409.
        await self.refuse_if_draft_slot_taken(
            organization_id, runbook_id, str(draft_path)
        )

        write_runbook_file(
            draft_path,
            content,
            source=f"manually created runbook (runbook_id={runbook_id})",
            root=self._data_dir,
        )

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
        is_platform_admin: bool = False,
    ) -> dict:
        """Scan data/knowledge/ for .md files not tracked in the database.

        Discovers runbooks created by the KB Toolkit or dropped on disk manually.
        Creates draft records so they appear in the Dashboard Drafts tab.

        Uses an async lock to prevent concurrent scans from creating duplicate
        drafts (e.g., React StrictMode fires the mount effect twice).

        Args:
            user_id: User triggering the scan (recorded as conversion job owner).
            organization_id: Org for scoping the conversion job + source upload.
                Falls back to the tenant the database session is bound to when
                None (``writable_org_id``) — which is the single-tenant org in
                a standalone deployment, and the caller's tenant under multi.
            is_platform_admin: Whether the caller may author global-scope KB. A file whose
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
                user_id, organization_id, is_platform_admin
            )

    async def _scan_for_runbooks_impl(
        self,
        user_id: str,
        organization_id: Optional[str] = None,
        is_platform_admin: bool = False,
    ) -> dict:
        import re as _re

        import yaml

        # Whether this caller may mint global-scope drafts. Computed once (the
        # policy is per-caller, not per-file); global-inferred files are skipped
        # below when this is False.
        global_authoring_allowed = is_global_authoring_allowed(is_platform_admin)

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
                    # Cheap status check FIRST: an already-discarded row is not
                    # reconciled at all, so probing its path was two wasted
                    # resolve() walks per scan and made a discarded escaping row
                    # log a refusal on every scan, forever.
                    if draft_model.status == DraftStatus.DISCARDED.value:
                        continue

                    # A row whose path is not inside the tree is SKIPPED — not
                    # discarded, not counted, not touched (#1213 follow-up).
                    #
                    # It was treated as "absent" in the first version of this
                    # change, and that was wrong in two measurable ways. A
                    # deployment whose only active drafts have escaping paths
                    # discards all of them, trips the "would discard ALL active
                    # drafts" abort guard, and gets a deterministic "Restore
                    # from backup" RuntimeError on EVERY scan — so the repair
                    # this was supposed to perform never runs. And a knowledge
                    # tree assembled with symlinks (``team_x -> /mnt/share``)
                    # worked before containment existed; treating it as absent
                    # soft-discards every draft under it on the first scan after
                    # the upgrade.
                    #
                    # Refusing to TOUCH such a path is the security posture and
                    # it stands. Refusing to touch it while deleting the row
                    # that points at it is just data loss. Skip and warn: an
                    # operator loses access to those drafts, not the drafts.
                    try:
                        file_exists = resolve_runbook_path(
                            draft_model.file_path,
                            source=(
                                "conversion_drafts.file_path "
                                f"(draft_id={draft_model.id})"
                            ),
                            root=self._data_dir,
                        ).exists()
                    except RunbookPathEscape as exc:
                        # NOT counted in ``skipped``: that number is returned to
                        # the client and means "files skipped during the disk
                        # walk". An escaping DB row is a bad ROW, not a walked
                        # file, so folding it in (as round 2 did) made "N files
                        # skipped" stop meaning walk skips. The bad row is
                        # surfaced by this WARNING, which names it for the
                        # operator repair it needs (#1213 follow-up).
                        logger.warning(
                            "skipping draft %s during scan reconciliation "
                            "(not discarded): %s",
                            draft_model.id,
                            exc,
                        )
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

                # Release the live-conversion claim of any case job whose last
                # live draft this sweep discarded — same transaction, same rule
                # as the explicit discard paths: a held unique slot on
                # conversion_jobs.live_case_id with no live draft behind it
                # would block that case's regeneration forever. Flush first so
                # the drained-count sees every status flipped above, even when
                # one job lost several drafts in this sweep.
                discarded_ids = pending_discard_ids + redundant_discard_ids
                if discarded_ids:
                    await session.flush()
                    drafts_by_id = {d.id: d for d in all_draft_models}
                    released_job_ids: set[str] = set()
                    for draft_id in discarded_ids:
                        job_id = drafts_by_id[draft_id].conversion_id
                        if job_id in released_job_ids:
                            continue
                        released_job_ids.add(job_id)
                        job = await session.get(ConversionJobModel, job_id)
                        if job is not None:
                            await self._release_live_case_key_if_drained(
                                session, job, draft_id
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

            # The walk starts inside the tree, but ``rglob`` follows symlinks:
            # a link planted at ``data/knowledge/global/innocent.md`` pointing
            # at ``/etc/anything`` is yielded here, and before this check it was
            # read and minted into a draft row — the exact shape every other
            # path in this service refuses. Both halves of the module must agree
            # on whether a file is a runbook, so the walk asks the same guard
            # (#1213 follow-up).
            try:
                resolve_runbook_path(
                    md_file,
                    source=f"scanned file ({md_file.name})",
                    root=knowledge_dir,
                )
            except RunbookPathEscape as exc:
                logger.warning("skipping a scanned file outside the tree: %s", exc)
                skipped += 1
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
            # retrieved for every tenant). A caller who may not author global scope (any
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

            # Persist as a synthetic conversion job.
            #
            # A ``ConflictError`` here means another live draft in this tenant
            # already holds this file's ``runbook_id`` (migration 046) — two
            # on-disk runbooks carrying the same frontmatter ``id``. The scan
            # SKIPS what it cannot take, the way it does for a path it cannot
            # contain: one unmintable file must not abort the walk over the
            # rest, and the operator needs the filename to fix it.
            conversion_id = generate_conversion_id()
            try:
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
            except ConflictError as exc:
                logger.warning(
                    "skipping a scanned runbook whose id is already taken: %s (%s)",
                    md_file.name,
                    exc,
                )
                errors.append(
                    f"{md_file.name}: runbook id {runbook_id!r} is already held "
                    f"by another live draft in this organization"
                )
                continue

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

    async def _release_live_case_key_if_drained(
        self,
        session: AsyncSession,
        job: "ConversionJobModel",
        discarded_draft_id: str,
    ) -> None:
        """Clear ``job.live_case_id`` once the job holds no more live drafts.

        The unique-index slot on ``conversion_jobs.live_case_id`` is the case's
        one live-conversion claim; it must be released in the same transaction
        as the last live draft leaving so a later regeneration can take it. The
        clearing is general (count the OTHER non-discarded drafts of this job) so
        a job carrying more than one live draft keeps the key until the last one
        is gone. No-op for jobs that never held the key (document jobs, failed
        no-draft jobs)."""
        if job.live_case_id is None:
            return
        remaining_live = await session.execute(
            select(func.count())
            .select_from(ConversionDraftModel)
            .where(
                ConversionDraftModel.conversion_id == job.id,
                ConversionDraftModel.id != discarded_draft_id,
                ConversionDraftModel.status != DraftStatus.DISCARDED.value,
            )
        )
        if remaining_live.scalar_one() == 0:
            job.live_case_id = None

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
            job = await session.get(ConversionJobModel, dm.conversion_id)
            if job is not None:
                await self._release_live_case_key_if_drained(session, job, dm.id)
            await session.commit()
            return True

    async def delete_draft(
        self,
        conversion_id: str,
        draft_id: str,
        user_id: str,
        is_platform_admin: bool = False,
    ) -> bool:
        """Soft-delete a draft and remove the file from disk.

        ``is_platform_admin`` gates deletion at ``global`` scope: destroying a
        global draft (file unlinked from disk) is platform-corpus authoring,
        the same policy as :meth:`update_draft` / :meth:`verify_draft` on the
        adjacent verbs. Defaults ``False`` (fail-closed).
        """
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
            job = job_result.scalar_one_or_none()
            if not job:
                return False

            if job.scope == "global":
                ensure_global_authoring_allowed(is_platform_admin)

            draft_result = await session.execute(
                select(ConversionDraftModel).where(
                    ConversionDraftModel.id == draft_id,
                    ConversionDraftModel.conversion_id == conversion_id,
                )
            )
            dm = draft_result.scalar_one_or_none()
            if not dm:
                return False

            # Remove file from disk. Same guard as the write paths, for the
            # same reason and with more at stake: this is an ``unlink`` driven
            # by a database value, so an escaping row would delete an arbitrary
            # file.
            #
            # Unlike the write paths this does NOT abort the operation. The
            # dangerous half is the unlink, and it must never keep the ROW from
            # being discarded — the "permanently undeletable row" this design
            # exists to prevent. So BOTH the containment refusal AND a failure
            # of the unlink itself degrade: the soft-delete below always runs.
            #
            # ``unlink`` can raise ``OSError`` independently of containment — a
            # read-only or full filesystem, a permission denial, or a TOCTOU
            # race where the file vanishes between ``exists()`` and ``unlink()``
            # (``FileNotFoundError`` is an ``OSError``). Catching only
            # ``RunbookPathEscape`` here left every one of those propagating
            # before the status flip. Same degrade-and-continue shape as
            # ``get_conversion``'s read (#1213 follow-up).
            try:
                file_path = resolve_runbook_path(
                    dm.file_path,
                    source=f"conversion_drafts.file_path (draft_id={dm.id})",
                    root=self._data_dir,
                )
                if file_path.exists():
                    file_path.unlink()
            except RunbookPathEscape as exc:
                logger.error(
                    "refusing to unlink for draft %s; discarding the row anyway: %s",
                    dm.id,
                    exc,
                )
            except OSError as exc:
                logger.error(
                    "failed to unlink the file for draft %s; discarding the "
                    "row anyway: %s",
                    dm.id,
                    exc,
                )

            # Soft delete in database
            dm.status = DraftStatus.DISCARDED.value
            await self._release_live_case_key_if_drained(session, job, dm.id)
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
