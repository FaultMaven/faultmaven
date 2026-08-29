"""Knowledge Suggestion Service.

Handles extraction of knowledge from cases into suggestions,
PII scanning, and the review workflow (approve/reject).

Design Reference: Source Verification Badges Feature
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from faultmaven.exceptions import ConflictError, ServiceUnavailableException
from faultmaven.infrastructure.llm.truncation import generate_with_truncation_retry
from faultmaven.modules.knowledge.domain.models.conversion import ValidationResult
from faultmaven.modules.knowledge.domain.models.suggestion import (
    KnowledgeSuggestion,
    PIIScanStatus,
    SuggestionStatus,
)

# Same-package siblings. The v4 authoring prompt and the frontmatter-id repair
# are the conversion path's, reused rather than re-declared (#1226): a second
# copy of the template is a second thing to keep in step with the validator, and
# ``test_cause_grammar_vocab`` pins exactly one of them against ``cause_grammar``.
from faultmaven.modules.knowledge.domain.services.conversion_service import (
    CONVERSION_SYSTEM_PROMPT,
    RUNBOOK_MAX_TOKENS,
    RUNBOOK_MAX_TOKENS_CEILING,
    _force_frontmatter_id,
)
from faultmaven.modules.knowledge.domain.services.runbook_validator import (
    RunbookValidator,
)
from faultmaven.utils.runbook_id import runbook_id_from_parts
from faultmaven.utils.serialization import to_json_compatible


class SuggestionService:
    """Service for managing knowledge suggestions.

    Handles:
    - Extracting knowledge from case conversations
    - PII scanning before review (HITL requirement)
    - CRUD operations for suggestions
    - Approval workflow with bidirectional linking
    """

    # Case-specific instructions, LAYERED ON the shared v4 authoring prompt
    # (#1226). ``CONVERSION_SYSTEM_PROMPT`` carries the template and the rules;
    # this carries only what the case path adds on top.
    #
    # It used to be the whole prompt, and it asked for
    # ``## Problem / ## Root Cause / ## Solution / ## Prevention``. Since #1214
    # ``upload_document`` enforces ``RunbookValidator`` before its first side
    # effect, and that shape fails it with six errors — no frontmatter, and five
    # of the six required sections missing — so approving an extraction without a
    # human reshape was ALWAYS a 422. The extractor moves to meet the gate; the
    # gate does not move (that is the product decision recorded on #1214).
    EXTRACTION_PROMPT = """
--- CONVERSION REQUEST ---
The source material below is a RESOLVED INCIDENT CASE — its transcript and
evidence summaries — not a document. Convert it into ONE runbook covering the
single failure mode the case is about, following the template and the rules
above exactly.

RUNBOOK_ID: {runbook_id}
SCOPE: global
TODAY: {today_iso}
SOURCE FILENAME: {source_label}

The frontmatter `id` field MUST be exactly: {runbook_id}
The frontmatter `scope` field MUST be exactly: global
`domain`, `service`, `severity` and `symptom_class` are NOT supplied for a case:
infer each one from the case content, using only the controlled vocabularies
named in the rules above.

DE-IDENTIFICATION — mandatory, and applied to every section including code
blocks. A runbook is reusable knowledge, not an incident record. Remove:
- absolute timestamps and dates (write relative time: "after ~2 hours")
- user names, email addresses and account identifiers
- hostnames, IP addresses, internal URLs, cluster and namespace names
- customer and organization names
- ticket, incident and case identifiers
Replace each with a generic placeholder (`<hostname>`, `<namespace>`) or with a
description of the role it played. KEEP product names, versions, error strings
and command shapes — those are what make the runbook usable.

Emit the runbook and nothing else: no preamble, no closing commentary, no
markdown code fence around the document. The first characters of your output are
the opening `---` of the frontmatter.

--- SOURCE MATERIAL: CASE ---
Case Title: {case_title}
Description: {case_description}

{messages_section}

{evidence_section}
--- END SOURCE MATERIAL ---
"""

    #: Appended to the extraction prompt when the previous attempt failed the
    #: gate. The validator's errors are STRUCTURED
    #: (``ValidationResult.errors`` — the same list ``RunbookQualityError``
    #: carries), so the repair turn names the defects instead of re-asking for
    #: the schema and hoping (#1226).
    REPAIR_PROMPT = """
--- REPAIR REQUIRED (attempt {attempt} of {max_attempts}) ---
Your previous output was REJECTED by the runbook validator. The validator is
mechanical: every line below is a specific structural violation, and the
document is refused until all of them are gone.

VALIDATOR ERRORS ({error_count}):
{errors}

Fix exactly these defects. Do not restructure anything that was not flagged,
and do not drop content to make an error go away. Re-emit the COMPLETE
corrected runbook, starting at the opening `---`, and output nothing else.

--- YOUR PREVIOUS OUTPUT ---
{previous_output}
--- END PREVIOUS OUTPUT ---
"""

    #: Total extraction attempts, first try included — so ONE repair turn.
    #:
    #: Sized from the measurement in ``tests/eval/suggestion_extraction``, not
    #: from taste: over the fixture corpus every draft that ever passed passed on
    #: attempt 1 or attempt 2, and no attempt-3 turn converted a failure into a
    #: pass. A third turn would spend another full runbook generation inside a
    #: synchronous HTTP request to re-buy a result the data says does not arrive.
    #: Re-run that driver before changing this.
    MAX_EXTRACTION_ATTEMPTS = 2

    #: Cap on the in-memory store (see #1227 for the durable replacement).
    #:
    #: The store became process-lifetime-scoped when the service became a
    #: singleton, and nothing ever removed an entry: approved, rejected and
    #: abandoned suggestions all accumulated, each holding a full LLM-generated
    #: article. Unbounded growth in a long-lived process is a leak, so the store
    #: is capped and evicts.
    #:
    #: Sized for a review inbox, not a corpus: the queue is admin-facing and
    #: drained by hand, so a few hundred is already far past what anyone reviews.
    MAX_STORED_SUGGESTIONS = 500

    def __init__(
        self,
        case_repository: Optional[Any] = None,
        knowledge_service: Optional[Any] = None,
        sanitizer: Optional[Any] = None,
        llm_provider: Optional[Any] = None,
        max_stored_suggestions: Optional[int] = None,
        max_extraction_attempts: Optional[int] = None,
    ):
        """Initialize the suggestion service.

        Args:
            case_repository: Repository for case access
            knowledge_service: Service for creating knowledge items
            sanitizer: ISanitizer for PII detection/redaction
            llm_provider: LLM provider for extraction
            max_stored_suggestions: Cap on the in-memory store; defaults to
                :attr:`MAX_STORED_SUGGESTIONS`. Injectable so a test can drive
                the eviction path without minting hundreds of suggestions.
            max_extraction_attempts: Total runbook-generation attempts per
                extraction, first try included; defaults to
                :attr:`MAX_EXTRACTION_ATTEMPTS`. Injectable so a test can pin
                the retry budget instead of inheriting whatever the shipped
                number happens to be.
        """
        self.logger = logging.getLogger(__name__)
        self._case_repository = case_repository
        self._knowledge_service = knowledge_service
        self._sanitizer = sanitizer
        self._llm_provider = llm_provider
        self._max_stored_suggestions = (
            self.MAX_STORED_SUGGESTIONS
            if max_stored_suggestions is None
            else max_stored_suggestions
        )
        self._max_extraction_attempts = max(
            1,
            (
                self.MAX_EXTRACTION_ATTEMPTS
                if max_extraction_attempts is None
                else max_extraction_attempts
            ),
        )
        self._validator = RunbookValidator()

        # In-memory store. NOT durable and NOT shared across workers — a restart
        # drops every pending suggestion and with WORKERS>1 an extract handled by
        # one worker is invisible to an approve handled by another. The durable
        # replacement is #1227; until then the store is bounded (see
        # _evict_for_capacity) so a long-lived process cannot grow without limit.
        self._suggestions_store: Dict[str, KnowledgeSuggestion] = {}

    async def extract_knowledge_from_case(
        self,
        case_id: str,
        organization_id: str,
        extracted_by: str,
        include_messages: bool = True,
        include_evidence: bool = True,
        title_suggestion: Optional[str] = None,
    ) -> KnowledgeSuggestion:
        """Extract knowledge from a case into a suggestion.

        Args:
            case_id: Case to extract from
            organization_id: Organization context
            extracted_by: User ID triggering extraction
            include_messages: Include case conversation
            include_evidence: Include evidence summaries
            title_suggestion: Optional title for the suggestion

        Returns:
            Created KnowledgeSuggestion
        """
        self.logger.info(f"Extracting knowledge from case {case_id}")

        # Get case details
        case_title = "Unknown Case"
        case_description = ""
        messages = []
        evidence = []

        if self._case_repository:
            try:
                case = await self._case_repository.get_by_id(case_id)
                if case:
                    case_title = getattr(case, "title", case_id)
                    case_description = getattr(case, "description", "")

                    if include_messages:
                        case_messages = await self._case_repository.get_messages(
                            case_id
                        )
                        messages = case_messages or []

                    if include_evidence:
                        case_evidence = await self._case_repository.get_evidence(
                            case_id
                        )
                        evidence = case_evidence or []
            except Exception as e:
                self.logger.warning(f"Failed to fetch case details: {e}")

        # Build extraction prompt
        messages_section = ""
        if include_messages and messages:
            formatted_messages = []
            for msg in messages[:50]:  # Limit to last 50 messages
                role = getattr(msg, "role", "unknown")
                content = getattr(msg, "content", str(msg))
                formatted_messages.append(f"[{role}]: {content}")
            messages_section = "Messages:\n" + "\n".join(formatted_messages)

        evidence_section = ""
        if include_evidence and evidence:
            evidence_summaries = []
            for ev in evidence[:20]:  # Limit to 20 pieces
                ev_type = getattr(ev, "artifact_type", "unknown")
                ev_name = getattr(ev, "name", "")
                ev_summary = getattr(ev, "summary", "")
                evidence_summaries.append(f"- [{ev_type}] {ev_name}: {ev_summary}")
            evidence_section = "Evidence Summary:\n" + "\n".join(evidence_summaries)

        # Minted here, not asked of the model, and re-forced onto the
        # frontmatter after every attempt. ``id`` must be kebab-case or the gate
        # rejects it, and models routinely echo the title verbatim — the same
        # failure ``_force_frontmatter_id`` exists for on the conversion path.
        # ``"case"`` as the service part keeps the slug non-empty even for a
        # title with no allowlisted characters.
        runbook_id = runbook_id_from_parts("case", case_title or case_id)

        prompt = CONVERSION_SYSTEM_PROMPT + self.EXTRACTION_PROMPT.format(
            runbook_id=runbook_id,
            today_iso=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            source_label=f"Case {case_id}",
            case_title=case_title,
            case_description=case_description,
            messages_section=messages_section or "No messages included.",
            evidence_section=evidence_section or "No evidence included.",
        )

        # Generate the runbook, re-prompting with the validator's own errors if
        # the first draft is refused (#1226).
        suggested_content = await self._generate_runbook_draft(prompt, runbook_id)

        # Generate title if not provided
        suggested_title = title_suggestion or await self._generate_title(
            case_title, suggested_content
        )

        # Create suggestion
        suggestion_id = f"sug_{uuid.uuid4().hex[:12]}"
        suggestion = KnowledgeSuggestion(
            suggestion_id=suggestion_id,
            organization_id=organization_id,
            case_id=case_id,
            status=SuggestionStatus.PENDING_REVIEW,
            suggested_title=suggested_title,
            suggested_content=suggested_content,
            suggested_type="troubleshooting_guide",
            extracted_by=extracted_by,
            extracted_at=datetime.now(timezone.utc),
            include_messages=include_messages,
            include_evidence=include_evidence,
            pii_scan_status=PIIScanStatus.NOT_SCANNED,
            source_case_title=case_title,
            message_count=len(messages),
            evidence_count=len(evidence),
        )

        # Trigger PII scan
        await self._scan_for_pii(suggestion)

        # Carry the gate's verdict onto the suggestion BEFORE it is stored, so
        # the review inbox can show what blocks approval instead of the reviewer
        # discovering it as a bare 422 (#1226).
        #
        # Re-run rather than reuse ``validation`` from the generation loop: a
        # sanitizer that finds PII REPLACES ``suggested_content``, and redaction
        # can itself break the gate (a redacted frontmatter value, a redacted
        # Cause Statement). The verdict on the suggestion must be about the text
        # the suggestion actually holds — which is the text approval will
        # publish — not about the draft the loop last saw.
        self._record_validation(suggestion)
        if not suggestion.validation_passed:
            self.logger.warning(
                "Suggestion %s from case %s does not pass the runbook quality "
                "gate after %d extraction attempt(s); %d error(s) surfaced for "
                "review: %s",
                suggestion_id,
                case_id,
                self._max_extraction_attempts,
                len(suggestion.validation_errors),
                "; ".join(suggestion.validation_errors[:5]),
            )

        # Make room BEFORE storing, so the cap is a real ceiling rather than a
        # ceiling plus one. Raises when the queue is full of pending reviews.
        self._evict_for_capacity()
        self._suggestions_store[suggestion_id] = suggestion
        self.logger.info(f"Created suggestion {suggestion_id} from case {case_id}")

        return suggestion

    def _evict_for_capacity(self) -> None:
        """Make room for one more suggestion, or refuse.

        Terminal suggestions — APPROVED and REJECTED — are the eviction pool:
        their decision is already recorded elsewhere (an approved one has its
        knowledge item in the corpus, a rejected one has nothing to publish), so
        dropping them from an in-memory review inbox loses only history. Oldest
        first, by ``updated_at``, which is when the decision was taken.

        A PENDING_REVIEW or DRAFT suggestion is NEVER evicted for capacity. It is
        the one thing here that exists nowhere else — evicting it would silently
        destroy work a reviewer has not seen, and the extract that caused the
        eviction would look like a success. So when the store is full of items
        still awaiting review, extraction REFUSES:

        Raises:
            ServiceUnavailableException: the store is at capacity and every
                entry is still awaiting review. The route answers 503, which is
                honest — the queue is full and the fix is to review it. #1227's
                durable store removes the ceiling.
        """
        capacity = self._max_stored_suggestions
        if capacity <= 0 or len(self._suggestions_store) < capacity:
            return

        terminal = [
            s
            for s in self._suggestions_store.values()
            if s.status in (SuggestionStatus.APPROVED, SuggestionStatus.REJECTED)
        ]
        needed = len(self._suggestions_store) - capacity + 1
        if len(terminal) < needed:
            self.logger.error(
                "Suggestion store is full (%d/%d) and %d entries are still "
                "awaiting review; refusing to extract more knowledge until the "
                "review inbox is drained",
                len(self._suggestions_store),
                capacity,
                len(self._suggestions_store) - len(terminal),
            )
            # DIAGNOSTIC wording, with the numbers an operator needs. The
            # user-facing sentence is the route's (``SUGGESTION_QUEUE_FULL``) —
            # not this string re-rendered, because a domain service may not
            # import the API layer (import-linter contract 2) and because the
            # route's own AST guard forbids echoing a caught exception into a
            # 5xx body anyway. One audience each, no duplication.
            raise ServiceUnavailableException(
                f"Suggestion store at capacity ({len(self._suggestions_store)}/"
                f"{capacity}) with no reviewed entry to evict"
            )

        terminal.sort(key=lambda s: s.updated_at)
        for victim in terminal[:needed]:
            del self._suggestions_store[victim.suggestion_id]
        self.logger.warning(
            "Suggestion store hit its %d-entry cap; evicted %d reviewed "
            "suggestion(s) (oldest decision first): %s",
            capacity,
            needed,
            ", ".join(s.suggestion_id for s in terminal[:needed]),
        )

    async def _generate_runbook_draft(self, base_prompt: str, runbook_id: str) -> str:
        """Generate a v4 runbook draft, re-prompting with the gate's own errors.

        The extraction path publishes into a corpus fronted by
        ``RunbookValidator`` (#1214), so "the model produced markdown" is not
        the finish line — "the model produced markdown the gate accepts" is. A
        first draft that misses a required section or mis-forms a Cause is
        recoverable information, not a dead end: the validator's errors are
        structured, so the repair turn is told exactly which lines to fix
        instead of being handed the schema again and asked to try harder.

        Bounded by :attr:`MAX_EXTRACTION_ATTEMPTS` — see the constant for why
        that number is what it is. When the budget runs out the BEST draft is
        still returned: the caller records the verdict on the suggestion, so a
        reviewer gets a near-miss runbook plus the list of what is wrong, which
        is a far shorter edit than the ``## Problem / ## Root Cause`` prose they
        used to have to reshape from scratch.

        Args:
            base_prompt: The full first-attempt prompt (shared v4 authoring
                instructions + the case-specific block).
            runbook_id: The minted kebab-case id, re-forced onto every draft's
                frontmatter.

        Returns:
            The best draft produced — the first one that passes, else the last
            one generated, else the skeleton template when no draft was
            produced at all.
        """
        prompt = base_prompt
        best: Optional[str] = None
        best_validation: Optional[ValidationResult] = None

        for attempt in range(1, self._max_extraction_attempts + 1):
            content = await self._generate_once(prompt)
            if content is None:
                # Provider absent, broken, or cut at the ceiling. Retrying the
                # same call cannot fix any of those, and the repair turn has
                # nothing to repair — stop and fall through.
                break

            content = _force_frontmatter_id(content, runbook_id)
            validation = self._validator.validate_content(content)
            best, best_validation = content, validation

            if validation.passed:
                if attempt > 1:
                    self.logger.info(
                        "Extraction draft passed the runbook quality gate on "
                        "attempt %d/%d after repair",
                        attempt,
                        self._max_extraction_attempts,
                    )
                return content

            self.logger.info(
                "Extraction draft failed the runbook quality gate on attempt "
                "%d/%d with %d error(s)",
                attempt,
                self._max_extraction_attempts,
                len(validation.errors),
            )
            if attempt == self._max_extraction_attempts:
                break

            prompt = base_prompt + self.REPAIR_PROMPT.format(
                attempt=attempt + 1,
                max_attempts=self._max_extraction_attempts,
                error_count=len(validation.errors),
                errors="\n".join(f"- {e}" for e in validation.errors),
                previous_output=content,
            )

        if best is not None:
            self.logger.warning(
                "Extraction exhausted its %d-attempt budget; returning the last "
                "draft with %d unresolved gate error(s) for review",
                self._max_extraction_attempts,
                len(best_validation.errors) if best_validation else 0,
            )
            return best

        return self.fallback_template(runbook_id)

    async def _generate_once(self, prompt: str) -> Optional[str]:
        """One generation call. ``None`` means "no usable draft came back".

        Separated from the retry loop so the loop reads as policy and this reads
        as plumbing. Every ``None`` branch below is a reason a REPAIR turn would
        be pointless: there is no draft to repair.
        """
        if not self._llm_provider:
            return None

        async def _call(cap: int):
            return await self._llm_provider.generate(
                prompt=prompt,
                max_tokens=cap,
                temperature=0.3,
            )

        try:
            # A v4 runbook does not fit in the 2000-token cap this path used to
            # pass — that budget was sized for four paragraphs of prose, and the
            # template alone is longer. Sized off the conversion path, which
            # generates the same artifact, and given the same one doubling on a
            # cut body.
            response = await generate_with_truncation_retry(
                _call,
                max_tokens=RUNBOOK_MAX_TOKENS,
                ceiling=RUNBOOK_MAX_TOKENS_CEILING,
                label="knowledge suggestion extraction",
            )
        except Exception as e:
            self.logger.warning(f"LLM generation failed: {e}")
            return None

        # ``generate`` returns an LLMResponse. The old code read it as a dict and
        # otherwise fell back to ``str(response)``, which would have written the
        # dataclass REPR into a knowledge suggestion.
        if response is None:
            # Not truncation — the contract says ``generate`` returns an
            # LLMResponse, so this means a provider broke it. Reported as itself
            # rather than folded into the truncation warning below, which would
            # send a reader looking for an output cap that was never involved.
            self.logger.warning(
                "Suggestion generation returned no response; "
                "falling back to the template"
            )
            return None

        if response.is_truncated:
            # A runbook cut mid-procedure is complete-or-nothing, the same rule
            # the conversion path applies (#1094): a half-procedure still
            # carries frontmatter and (sections being written in order) the
            # early required headings, so it can PASS the gate while missing
            # its last steps. Never repaired, never returned.
            self.logger.warning(
                "Suggestion generation truncated at the output cap even after "
                "the retry; falling back to the template"
            )
            return None

        content = (response.content or "").strip()
        return content or None

    @staticmethod
    def fallback_template(runbook_id: str = "extracted-runbook-draft") -> str:
        """The v4 skeleton used when no draft could be generated at all.

        Deliberately DOES NOT pass the gate, and that is not an oversight: with
        no draft there is no knowledge here, only a form, and letting one click
        publish ``[INSUFFICIENT SOURCE DATA]`` into the global corpus would be a
        hole this lane opened rather than closed — the prose template it
        replaces was unapprovable too, just for a worse reason (it was not a
        runbook at all).

        What blocks it is not a contrivance bolted on to force a failure: Cause
        A's ``**Statement:**`` is EMPTY because there genuinely is no cause
        statement, and an empty required sub-field is already a gate error. So
        the reviewer gets the v4 shape to fill in, one precise sentence telling
        them what is missing, and no way to publish the form by accident.

        ``tests/unit/modules/knowledge/test_extraction_emits_v4_schema_1226.py``
        pins BOTH halves: v4-shaped, and still refused.
        """
        return f"""---
id: {runbook_id}
title: "[INSUFFICIENT SOURCE DATA -- manual completion required]"
domain: application
service: unknown
symptom_class: []
scope: global
tags: [extracted, case-derived]
difficulty: intermediate
severity: medium
version: "1.0.0"
last_updated: "{datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
verified_by: ""
status: draft
---

# Runbook: [INSUFFICIENT SOURCE DATA -- manual completion required]

## Symptom Recognition
- [INSUFFICIENT SOURCE DATA -- record the alert names, log lines and metric
  patterns exactly as an operator sees them]

## Applicability
[INSUFFICIENT SOURCE DATA -- state the software version range, required access
level, and the tools needed.]

## Diagnostic Steps

### Step 1: [INSUFFICIENT SOURCE DATA -- name the first check]
```bash
# [INSUFFICIENT SOURCE DATA -- the command an operator runs]
```
[INSUFFICIENT SOURCE DATA -- what to look for in that output.]

## Causes

### Cause A: [INSUFFICIENT SOURCE DATA -- name the failure mode]
**Statement:**
**Indicators:**
- root: [Step 1] [INSUFFICIENT SOURCE DATA -- the observable that confirms it]
**Interventions:**
- **remediation** (root): [INSUFFICIENT SOURCE DATA -- the durable fix.]
  **Verification:** [INSUFFICIENT SOURCE DATA -- what confirms the fix worked.]

### Cause Z: Unidentified
**Statement:** None of the documented causes match the observed evidence.
**Indicators:**
- [Default]
**Interventions:**
- **mitigation** (D): Capture full diagnostic output and consult an SME.
  **Risk:** Diagnostic only. **Duration:** Until SME review. **Verification:** N/A.

## Prevention
- [INSUFFICIENT SOURCE DATA -- the configuration change or alert to add.]

## Sources
- [INSUFFICIENT SOURCE DATA -- the case this was extracted from.]
"""

    def _record_validation(self, suggestion: KnowledgeSuggestion) -> None:
        """Run the publication gate over a suggestion's CURRENT content and
        record the verdict on it.

        The single place the verdict is computed, so extraction and the review
        edit cannot disagree about what "passes" means, and so both ask the same
        ``RunbookValidator`` ``upload_document`` will ask on approval.
        """
        result = self._validator.validate_content(suggestion.suggested_content)
        suggestion.set_validation(
            passed=result.passed,
            errors=result.errors,
            warnings=result.warnings,
        )

    async def _generate_title(self, case_title: str, content: str) -> str:
        """Generate a knowledge article title.

        Args:
            case_title: Original case title
            content: Generated content

        Returns:
            Suggested title
        """
        # Simple title extraction - in production, use LLM
        if case_title and case_title != "Unknown Case":
            # Clean up the case title for reuse
            title = case_title
            # Remove incident-specific prefixes
            prefixes_to_remove = [
                "Incident:",
                "Alert:",
                "Issue:",
                "[P1]",
                "[P2]",
                "[P3]",
                "[SEV1]",
                "[SEV2]",
            ]
            for prefix in prefixes_to_remove:
                if title.startswith(prefix):
                    title = title[len(prefix) :].strip()
            return f"Troubleshooting: {title}"

        return "Troubleshooting Guide"

    async def _scan_for_pii(self, suggestion: KnowledgeSuggestion) -> None:
        """Scan suggestion content for PII.

        Uses ISanitizer to detect PII entities. If PII is found,
        marks the suggestion for manual remediation (HITL).

        Args:
            suggestion: Suggestion to scan
        """
        suggestion.pii_scan_status = PIIScanStatus.SCANNING

        if self._sanitizer:
            try:
                # Scan content for PII
                content_to_scan = (
                    f"{suggestion.suggested_title}\n\n{suggestion.suggested_content}"
                )
                sanitized = await self._sanitizer.asanitize(content_to_scan)

                # If content was modified, PII was found
                if sanitized != content_to_scan:
                    suggestion.mark_pii_scan_complete(
                        status=PIIScanStatus.PII_DETECTED,
                        result={
                            "original_length": len(content_to_scan),
                            "sanitized_length": len(sanitized),
                            "pii_removed": True,
                            "message": "PII detected. Manual review required before approval.",
                        },
                    )
                    # Store sanitized version as suggestion
                    suggestion.suggested_content = sanitized
                else:
                    suggestion.mark_pii_scan_complete(
                        status=PIIScanStatus.CLEAN,
                        result={"pii_detected": False},
                    )
            except Exception as e:
                self.logger.error(f"PII scan failed: {e}")
                suggestion.mark_pii_scan_complete(
                    status=PIIScanStatus.SCAN_FAILED,
                    result={"error": str(e)},
                )
        else:
            # No sanitizer available, mark as clean (development mode)
            suggestion.mark_pii_scan_complete(
                status=PIIScanStatus.CLEAN,
                result={"pii_detected": False, "note": "No sanitizer configured"},
            )

    async def get_suggestion(self, suggestion_id: str) -> Optional[KnowledgeSuggestion]:
        """Get a suggestion by ID — UNSCOPED.

        This is the trusted internal load: it applies no requester scope, so
        extraction and other in-process flows can read a row they just wrote.
        Anything acting on behalf of an actor must use
        :meth:`get_suggestion_visible` instead, which carries the mandatory
        tenant predicate.

        Args:
            suggestion_id: Suggestion identifier

        Returns:
            KnowledgeSuggestion or None
        """
        return self._suggestions_store.get(suggestion_id)

    async def get_suggestion_visible(
        self, suggestion_id: str, *, organization_id: str
    ) -> Optional[KnowledgeSuggestion]:
        """Get a suggestion by ID, scoped to the actor's tenant.

        The actor-facing counterpart of :meth:`get_suggestion` (the split #871
        introduced for documents). Returns None both for an absent id and for
        one belonging to another organization, so the two are indistinguishable
        to the caller and a route built on it answers 404 rather than acting as
        an existence oracle. Fail-closed: no organization, no result — never a
        deployment-wide lookup.

        Rejected alternative: scoping :meth:`get_suggestion` itself — it is the
        trusted load behind extraction, which has no actor to scope by.

        Args:
            suggestion_id: Suggestion identifier
            organization_id: Actor's tenant; REQUIRED

        Returns:
            KnowledgeSuggestion owned by ``organization_id``, or None
        """
        if not suggestion_id or not organization_id:
            return None
        suggestion = self._suggestions_store.get(suggestion_id)
        if suggestion is None or suggestion.organization_id != organization_id:
            return None
        return suggestion

    async def list_suggestions(
        self,
        organization_id: str,
        status: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """List suggestions belonging to one organization.

        ``organization_id`` is REQUIRED and always applied: an unscoped listing
        would return every tenant's suggestions to a platform admin bound to
        one. Fail-closed — a falsy organization lists nothing.

        Args:
            organization_id: Actor's tenant; REQUIRED
            status: Filter by status
            limit: Max items to return
            offset: Pagination offset

        Returns:
            Dict with suggestions list and pagination info
        """
        if not organization_id:
            return {
                "suggestions": [],
                "total_count": 0,
                "limit": limit,
                "offset": offset,
            }

        suggestions = [
            s
            for s in self._suggestions_store.values()
            if s.organization_id == organization_id
        ]

        if status:
            suggestions = [s for s in suggestions if s.status.value == status]

        # Sort by created_at descending
        suggestions.sort(key=lambda s: s.created_at, reverse=True)

        total_count = len(suggestions)

        # Apply pagination
        suggestions = suggestions[offset : offset + limit]

        return {
            "suggestions": suggestions,
            "total_count": total_count,
            "limit": limit,
            "offset": offset,
        }

    async def approve_suggestion(
        self,
        suggestion_id: str,
        reviewed_by: str,
        review_notes: Optional[str] = None,
        *,
        organization_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Approve a suggestion and create a knowledge item.

        Args:
            suggestion_id: Suggestion to approve
            reviewed_by: User ID of reviewer
            review_notes: Optional notes
            organization_id: Actor's tenant; REQUIRED — an out-of-tenant id
                resolves to None, exactly like an absent one

        Returns:
            Dict with the new ``knowledge_item_id``, or ``None`` when the
            suggestion is absent, out of tenant, or not ready for review.
            ``None`` means ONLY that — it is no longer the catch-all it was.

        Raises:
            ConflictError: the suggestion is already approved
                (``conflict_reason="already_approved"``) — checked before
                anything is published, so a repeat writes nothing. The global
                handler maps this to HTTP 409.
            RunbookQualityError: the suggested content does not meet the
                runbook quality standard (#1214). Raised by ``upload_document``
                BEFORE it writes anything, so the refusal publishes nothing;
                the global handler answers 422. LLM-extracted markdown reaches
                the corpus only after a reviewer edits it into a valid runbook.
            RuntimeError: no knowledge service is wired, so there is nothing to
                publish INTO (#1214). This used to mint a fake id and report
                ``201 {"status": "approved"}`` for an item that was never
                created — and, with ``app.state.suggestion_service`` unset, it
                was the branch every production request took.
            Exception: anything ``upload_document`` raises now PROPAGATES
                rather than being swallowed into ``None`` (#1200). A
                ``TypeError`` from that call is a programming error and an
                ingestion failure is a server-side fault; neither is a client
                error and neither is a statement about PII. The route's own
                handler logs and answers 500.
        """
        suggestion = await self.get_suggestion_visible(
            suggestion_id, organization_id=organization_id
        )
        if not suggestion:
            self.logger.warning(f"Suggestion {suggestion_id} not found")
            return None

        # A scan that FAILED is a transient infrastructure fault, not a verdict
        # about the content — so retry it here rather than leaving the
        # suggestion stuck (#1214 review).
        #
        # This became reachable when the real sanitizer was wired: before that
        # every service was built with sanitizer=None and every scan was marked
        # CLEAN, so SCAN_FAILED could not occur in production. With a real
        # Presidio engine it can, and it was a dead end — approve answered 400,
        # mark_pii_remediated answered 409 (only PII_DETECTED is remediable),
        # and the ONLY re-arm was an undocumented content edit. Re-scanning is
        # the honest recovery: it is the same call that produced the failure, so
        # if the engine is still down the status stays SCAN_FAILED and the 400
        # below is truthful again.
        if suggestion.pii_scan_status is PIIScanStatus.SCAN_FAILED:
            self.logger.info(
                "Suggestion %s has a failed PII scan; re-scanning before " "approval",
                suggestion_id,
            )
            await self._scan_for_pii(suggestion)

        if not suggestion.is_ready_for_review():
            self.logger.warning(
                f"Suggestion {suggestion_id} not ready for review "
                f"(pii_scan_status={suggestion.pii_scan_status})"
            )
            return None

        # Refuse a SECOND approval BEFORE anything is published.
        #
        # ``is_ready_for_review`` inspects ``pii_scan_status`` only, never
        # ``status``, so nothing here used to stop a repeat. That was harmless
        # only because the call below always raised — every approval created
        # nothing. Now that it succeeds, a repeat publishes ANOTHER item into
        # the global corpus and overwrites ``knowledge_item_id``, orphaning the
        # previous one with no back-link. Measured before this guard: three
        # calls gave three knowledge items, three files and three ChromaDB
        # chunk sets, with only the last one linked.
        #
        # ``approve()`` carries the same check as a defence, but it runs AFTER
        # the publish, so the guard has to be here to prevent the write.
        if suggestion.is_approved():
            raise ConflictError(
                "Suggestion has already been approved",
                resource_type="suggestion",
                resource_id=suggestion_id,
                conflict_reason="already_approved",
            )

        # Create knowledge item.
        #
        # No knowledge service means no corpus to publish into, and the only
        # honest answer is a failure (#1214). The old ``else`` minted an id from
        # ``authored_item_id()`` and returned ``{"status": "approved"}`` for a
        # knowledge item that had never been created — the same class of claim
        # #1200 exists to remove, standing inside the function that fixes it.
        # And because ``app.state.suggestion_service`` was written NOWHERE, the
        # route always built a collaborator-less service, so that branch was the
        # one 100% of production approvals took.
        if not self._knowledge_service:
            raise RuntimeError(
                "Cannot approve suggestion "
                f"{suggestion_id}: no knowledge service is configured, so no "
                "knowledge item can be created. Approval reports success only "
                "when something was actually published."
            )

        # NO try/except around this call (#1200).
        #
        # It used to pass ``metadata={...}`` — a parameter
        # ``upload_document`` has never had. The resulting ``TypeError``
        # was caught by a broad ``except Exception`` here, logged, and
        # turned into ``return None``, which the approve route renders as
        # ``400 "Cannot approve: PII scan not complete"``. That claim is
        # false by construction: the scan had to be CLEAN or REMEDIATED to
        # get past ``is_ready_for_review`` above. So the approval step of
        # the knowledge flywheel created nothing and misreported why, and
        # the failure was shaped exactly like "nothing to approve".
        #
        # A ``TypeError`` from a call this service makes to its own
        # collaborator is a programming error, and a failed ingestion is a
        # server-side fault. Neither is a client error and neither is a
        # statement about PII. Both now propagate: the route's own
        # ``except Exception`` logs them and answers 500. ``return None``
        # is left to mean one thing only — the suggestion is not ready —
        # which is the one case that 400 is actually about.
        #
        # ``upload_document`` also enforces the runbook quality gate (#1214)
        # before its first side effect, so content that fails it raises
        # ``RunbookQualityError`` here having published NOTHING.
        result = await self._knowledge_service.upload_document(
            content=suggestion.suggested_content,
            title=suggestion.suggested_title,
            document_type=suggestion.suggested_type,
            # The platform tier, stated rather than inherited from a
            # default (#1166). Gated at the approve route by
            # require_global_authoring_allowed(); an approved
            # suggestion becomes platform-shipped knowledge, which is
            # why that gate is there and why this says "global" out
            # loud instead of taking whatever the service assumed.
            scope="global",
            category="extracted",
            tags=["extracted", "case-derived"],
            source_url=None,
            # ATTRIBUTION, on the one parameter that actually persists.
            #
            # ``owner_id`` reaches four real columns —
            # ``uploaded_files.uploaded_by``, ``conversion_jobs.user_id``,
            # ``conversion_drafts.verified_by``, and ``ingest_runbook``'s
            # own ``owner_id`` — so the approving admin is recorded in the
            # database. For an approved suggestion the approver IS the
            # verifier, which is what ``verified_by`` means.
            #
            # Safe at this scope: the only other use of ``owner_id`` is the
            # ``scope == "personal"`` directory branch, which cannot fire
            # under ``scope="global"``.
            owner_id=reviewed_by,
            # ⚠️ ``description`` is accepted by ``upload_document`` and then
            # IGNORED — referenced zero times in that method's body, so it
            # reaches no column and no ChromaDB metadata. ``category`` is
            # the same, surviving only in the transient return dict.
            #
            # Passed anyway because it is the natural sink and a future one
            # would read it, but it records NOTHING today. The
            # case/extractor/suggestion lineage the dropped ``metadata=``
            # was carrying still has no home, and neither does
            # ``verification_level: 2`` (the derive yields EXPERIMENTAL).
            # That pair IS the "where does the metadata belong" decision
            # this issue names, and #878 owns it. Do not read this argument
            # as provenance.
            description=(
                f"Extracted from case {suggestion.case_id} "
                f"by {suggestion.extracted_by} "
                f"(suggestion {suggestion_id})"
            ),
        )
        knowledge_item_id = result.get("document_id")
        if not knowledge_item_id:
            # Never mark a suggestion approved against an id we did not
            # get: the point of this fix is that approval stops claiming
            # success it cannot back.
            raise RuntimeError(
                "upload_document returned no document_id for suggestion "
                f"{suggestion_id}; nothing was linked"
            )

        # Mark suggestion as approved — COMPENSATED (#1214).
        #
        # ``approve()`` re-checks readiness, and ``update_suggestion``
        # concurrently resets ``pii_scan_status`` on any content edit, so this
        # can raise AFTER the publish has already written a knowledge_items
        # row, its ChromaDB chunks and a file on disk. Without compensation the
        # corpus keeps a published runbook that no suggestion links to, while
        # the client is told the approval failed — an orphan created by the
        # error path itself.
        #
        # ``ingest_runbook`` already applies exactly this discipline one level
        # down (SQL row deleted when the vector write fails); this is the same
        # rule for the step above it. ``delete_document`` hard-deletes an
        # authored id (``kb_<16 hex>``, which is what ``upload_document``
        # mints), removing both the row and its vectors.
        try:
            suggestion.approve(
                reviewed_by=reviewed_by,
                knowledge_item_id=knowledge_item_id,
                review_notes=review_notes,
            )
        except Exception:
            await self._rollback_published_item(knowledge_item_id, suggestion_id)
            raise

        self.logger.info(
            f"Approved suggestion {suggestion_id}, created knowledge item {knowledge_item_id}"
        )

        return {
            "suggestion_id": suggestion_id,
            "knowledge_item_id": knowledge_item_id,
            "status": "approved",
        }

    #: One sentence, used by BOTH residue branches below, so log-based alerting
    #: can match a single string instead of two near-identical ones.
    _ROLLBACK_RESIDUE_LOG = (
        "Rollback after the failed approval of suggestion %s left residue that "
        "needs manual cleanup: %s"
    )

    async def _rollback_published_item(
        self, knowledge_item_id: str, suggestion_id: str
    ) -> None:
        """Undo the publish behind an approval that then failed.

        Delegates to ``KnowledgeService.rollback_uploaded_document``, which
        removes everything ``upload_document`` wrote — the knowledge item and its
        vectors AND the draft / job / uploaded-file rows and the on-disk runbook.
        Deleting only the item leaves a ``status="verified"`` draft row pointing
        at a deleted id, which the reconciliation scan then SKIPS (measured), so
        the file is stranded permanently. The knowledge of what an upload wrote
        lives with the service that wrote it; this method only reports.

        Best-effort and NEVER raises: it runs inside an ``except`` block whose
        original exception is what the caller must see. A rollback that itself
        raised would replace a truthful "approval failed" with an unrelated error
        AND still leave the residue. So residue is logged as the
        operator-actionable event it is — naming which store kept what — and
        swallowed.
        """
        try:
            result = await self._knowledge_service.rollback_uploaded_document(
                knowledge_item_id
            )
            residue = (result or {}).get("residue") or []
            if residue:
                self.logger.error(
                    self._ROLLBACK_RESIDUE_LOG,
                    suggestion_id,
                    "; ".join(str(item) for item in residue),
                )
            else:
                self.logger.warning(
                    "Rolled back knowledge item %s and its upload bookkeeping "
                    "after approval of suggestion %s failed",
                    knowledge_item_id,
                    suggestion_id,
                )
        except Exception as rollback_error:
            # The rollback itself blew up, so nothing is known about which
            # stores were cleaned. Report the whole upload as residue rather
            # than naming a store on a guess.
            self.logger.error(
                self._ROLLBACK_RESIDUE_LOG,
                suggestion_id,
                f"the rollback of {knowledge_item_id} failed outright "
                f"({rollback_error}); every store this upload touched may still "
                f"hold data",
            )

    async def reject_suggestion(
        self,
        suggestion_id: str,
        reviewed_by: str,
        rejection_reason: str,
        review_notes: Optional[str] = None,
        *,
        organization_id: str,
    ) -> bool:
        """Reject a suggestion.

        Args:
            suggestion_id: Suggestion to reject
            reviewed_by: User ID of reviewer
            rejection_reason: Why rejected
            review_notes: Optional additional notes
            organization_id: Actor's tenant; REQUIRED

        Returns:
            True if rejected, False if not found or out of tenant
        """
        suggestion = await self.get_suggestion_visible(
            suggestion_id, organization_id=organization_id
        )
        if not suggestion:
            return False

        suggestion.reject(
            reviewed_by=reviewed_by,
            rejection_reason=rejection_reason,
            review_notes=review_notes,
        )

        self.logger.info(f"Rejected suggestion {suggestion_id}: {rejection_reason}")
        return True

    async def update_suggestion(
        self,
        suggestion_id: str,
        title: Optional[str] = None,
        content: Optional[str] = None,
        suggested_type: Optional[str] = None,
        *,
        organization_id: str,
    ) -> Optional[KnowledgeSuggestion]:
        """Update a suggestion's content.

        Args:
            suggestion_id: Suggestion to update
            title: New title
            content: New content
            suggested_type: New type
            organization_id: Actor's tenant; REQUIRED

        Returns:
            Updated suggestion or None if not found or out of tenant
        """
        suggestion = await self.get_suggestion_visible(
            suggestion_id, organization_id=organization_id
        )
        if not suggestion:
            return None

        if title or content:
            suggestion.update_content(
                title=title or suggestion.suggested_title,
                content=content or suggestion.suggested_content,
            )
            # Re-scan for PII since content changed
            await self._scan_for_pii(suggestion)
            # And re-run the publication gate, AFTER the scan for the same
            # reason extraction does: a sanitizer that redacts rewrites the
            # content the verdict is about (#1226). This is the loop the
            # reviewer actually works in — edit, see which errors cleared, edit
            # again — so a stale verdict here is worse than none.
            self._record_validation(suggestion)

        if suggested_type:
            suggestion.suggested_type = suggested_type
            suggestion.touch()

        self.logger.info(f"Updated suggestion {suggestion_id}")
        return suggestion

    async def remediate_pii(
        self,
        suggestion_id: str,
        remediated_by: str,
        *,
        organization_id: str,
    ) -> Optional[KnowledgeSuggestion]:
        """Mark PII as remediated after manual review.

        Args:
            suggestion_id: Suggestion that was remediated
            remediated_by: User ID who remediated
            organization_id: Actor's tenant; REQUIRED

        Returns:
            Updated suggestion or None if not found or out of tenant
        """
        suggestion = await self.get_suggestion_visible(
            suggestion_id, organization_id=organization_id
        )
        if not suggestion:
            return None

        suggestion.mark_pii_remediated(remediated_by)
        self.logger.info(f"PII remediated for suggestion {suggestion_id}")
        return suggestion

    def to_api_response(
        self, suggestion: KnowledgeSuggestion, include_content: bool = False
    ) -> Dict[str, Any]:
        """Convert suggestion to API response format.

        Args:
            suggestion: Suggestion to convert
            include_content: Include full content (for detail view)

        Returns:
            Dict suitable for API response
        """
        lineage = {
            "case_id": suggestion.case_id,
            "case_title": suggestion.source_case_title,
            "extracted_by": suggestion.extracted_by,
            "extracted_at": to_json_compatible(suggestion.extracted_at),
        }

        # The publication gate's verdict, forward-declared (#1226). Approval
        # runs ``RunbookValidator`` inside ``upload_document`` and answers 422
        # on a refusal; without this the reviewer learns that only by pressing
        # approve, and learns nothing about what to fix. ``passed: null`` means
        # not yet evaluated — never read it as "fine".
        validation = {
            "passed": suggestion.validation_passed,
            "errors": list(suggestion.validation_errors),
            "warnings": list(suggestion.validation_warnings),
        }

        if include_content:
            return {
                "suggestion_id": suggestion.suggestion_id,
                "organization_id": suggestion.organization_id,
                "case_id": suggestion.case_id,
                "status": suggestion.status.value,
                "suggested_title": suggestion.suggested_title,
                "suggested_content": suggestion.suggested_content,
                "suggested_type": suggestion.suggested_type,
                "extracted_by": suggestion.extracted_by,
                "extracted_at": to_json_compatible(suggestion.extracted_at),
                "include_messages": suggestion.include_messages,
                "include_evidence": suggestion.include_evidence,
                "pii_scan_status": suggestion.pii_scan_status.value,
                "pii_scan_result": suggestion.pii_scan_result,
                "pii_remediated_by": suggestion.pii_remediated_by,
                "pii_remediated_at": (
                    to_json_compatible(suggestion.pii_remediated_at)
                    if suggestion.pii_remediated_at
                    else None
                ),
                "lineage": lineage,
                "validation": validation,
                "reviewed_by": suggestion.reviewed_by,
                "reviewed_at": (
                    to_json_compatible(suggestion.reviewed_at)
                    if suggestion.reviewed_at
                    else None
                ),
                "review_notes": suggestion.review_notes,
                "rejection_reason": suggestion.rejection_reason,
                "knowledge_item_id": suggestion.knowledge_item_id,
                "created_at": to_json_compatible(suggestion.created_at),
                "updated_at": to_json_compatible(suggestion.updated_at),
                "metadata": suggestion.metadata,
            }
        else:
            # Summary view
            return {
                "suggestion_id": suggestion.suggestion_id,
                "title": suggestion.suggested_title,
                "content_preview": suggestion.get_content_preview(200),
                "status": suggestion.status.value,
                "verification_status": "experimental",  # Always experimental until approved
                "pii_scan_status": suggestion.pii_scan_status.value,
                "suggested_type": suggestion.suggested_type,
                "created_at": to_json_compatible(suggestion.created_at),
                "lineage": lineage,
                # The summary carries the whole verdict, not just the boolean:
                # the review inbox is a list, and "why is this one blocked" is
                # the question a reviewer asks BEFORE opening a row.
                "validation": validation,
            }
