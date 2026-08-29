"""Knowledge Suggestion Service.

Handles extraction of knowledge from cases into suggestions,
PII scanning, and the review workflow (approve/reject).

Design Reference: Source Verification Badges Feature
"""

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from faultmaven.exceptions import ConflictError, ServiceUnavailableException
from faultmaven.infrastructure.llm.truncation import generate_with_truncation_retry
from faultmaven.modules.knowledge.contracts import (
    ISuggestionRepository,
    SuggestionConcurrencyError,
)
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
    VALID_DOMAINS,
    RunbookValidator,
)
from faultmaven.utils.runbook_id import runbook_id_from_parts
from faultmaven.utils.serialization import to_json_compatible

#: Statuses that mean "a reviewer has not dealt with this yet". The store's
#: ceiling counts these and nothing else: a decided suggestion is a permanent
#: record — and, for an approved one, the only link from its case to the
#: runbook it produced — not queue depth.
UNREVIEWED_STATUSES = (SuggestionStatus.PENDING_REVIEW, SuggestionStatus.DRAFT)


def _as_kebab(runbook_id: str) -> str:
    """Coerce a minted id into the kebab grammar the validator enforces.

    ``runbook_id_from_parts`` can return a value its own docstring says it does
    not: for an over-length input it truncates to 55 characters and appends
    ``"-" + md5[:4]``, so a slug whose 55th character is already a hyphen yields
    a DOUBLE hyphen — and ``^[a-z0-9]+(-[a-z0-9]+)*$`` rejects that. Measured on
    the extraction eval: ``tls-outbound-tls-failure-due-to-expired-ca-certificate--7217``
    was one of eight first drafts, refused by the gate for its id alone.

    Repaired HERE rather than in the shared helper on purpose. That helper is
    pinned byte-for-byte by ``tests/unit/utils/test_runbook_id_consolidation.py``
    because its output is a PERSISTED id (``conversion_drafts.runbook_id``,
    runbook frontmatter), so changing it is a decision about orphaning existing
    rows — a call for the owner of that seam, not a side effect of this lane.
    The defect is real and reaches the two conversion call sites as well; it is
    reported as a residual rather than fixed silently.
    """
    return re.sub(r"-{2,}", "-", runbook_id or "").strip("-")


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

SCOPE: global
TODAY: {today_iso}
SOURCE FILENAME: {source_label}

The frontmatter `scope` field MUST be exactly: global
The frontmatter `id` field is kebab-case derived from the DE-IDENTIFIED title
you write below — NEVER from the case title, which names an incident. It is
normalised after you write it, so spend your care on the title.
`domain`, `service`, `severity` and `symptom_class` are NOT supplied for a case:
infer each one from the case content, using only the controlled vocabularies.
`domain` MUST be one of: {domain_vocab}. It is a coarse system-layer label, not
the technology — a Kubernetes scheduling failure is `compute`, a cache eviction
is `database`, a resolver timeout is `networking`, a web tier is `application`.
Put the technology in `service` and `tags`, where it is free text.

Each Indicator carries exactly ONE `[Step N]` token. To cite two steps, write
two Indicator entries — `[Step 2, Step 3]` is not a token and is rejected.

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
    #: from taste. On that corpus (8 cases, claude-sonnet-4-5, 2026-08-29):
    #: **5/8 cleared the gate on the first draft and the remaining 3/8 cleared
    #: it on the repair turn** — 8/8 within the budget, and nothing left for a
    #: third turn to buy. That is a measurement of when it is ENOUGH, not proof
    #: that a third turn never helps: the corpus never produced a twice-failing
    #: draft for one to act on.
    #:
    #: The cost side is what makes 2 the right stopping point rather than 3.
    #: This counts GATE attempts, and each one wraps
    #: ``generate_with_truncation_retry``, which spends a second generation of
    #: its own if the body comes back cut — so the budget is up to FOUR
    #: generations, not two, inside a synchronous HTTP request. At the ~20-60 s
    #: per generation the eval measured, a speculative third gate attempt is
    #: bought with reviewer latency on every extraction that was going to fail
    #: anyway — and a draft that fails twice reaches the reviewer with its
    #: errors attached, which is the recovery this lane built.
    #:
    #: Re-run that driver before changing this.
    MAX_EXTRACTION_ATTEMPTS = 2

    #: How many UNREVIEWED suggestions ONE ORGANIZATION may have queued at once.
    #:
    #: Two things changed with the durable store (#1227), and both are
    #: deliberate.
    #:
    #: **It counts unreviewed work, and nothing is ever deleted.** The old cap
    #: bounded a process-local dict and made room by EVICTING approved and
    #: rejected entries. Over a table that is permanent destruction of the only
    #: case → runbook link that exists: ``knowledge_items`` carries no
    #: back-pointer, so ``knowledge_suggestions.knowledge_item_id`` is the
    #: whole provenance trail, and the flywheel this feature exists to build is
    #: made of exactly those rows. Rows are cheap and a decided suggestion is a
    #: record, not queue depth — so the ceiling now applies to PENDING_REVIEW
    #: and DRAFT only, and a full queue REFUSES rather than evicting.
    #:
    #: **It is scoped per organization.** The table is shared by every tenant;
    #: a deployment-wide count would let one tenant's undrained inbox refuse
    #: another tenant's extraction, a cross-tenant denial of service the
    #: per-worker dict could only ever inflict within one worker.
    #:
    #: Sized for a review inbox, not a corpus: the queue is admin-facing and
    #: drained by hand, so a few hundred is already far past what anyone reviews.
    MAX_UNREVIEWED_SUGGESTIONS = 500

    #: Retired spelling of :attr:`MAX_UNREVIEWED_SUGGESTIONS`. Kept as an alias
    #: because the old name said "stored", which is no longer what is counted.
    MAX_STORED_SUGGESTIONS = MAX_UNREVIEWED_SUGGESTIONS

    def __init__(
        self,
        case_repository: Optional[Any] = None,
        knowledge_service: Optional[Any] = None,
        sanitizer: Optional[Any] = None,
        llm_provider: Optional[Any] = None,
        max_stored_suggestions: Optional[int] = None,
        max_extraction_attempts: Optional[int] = None,
        suggestion_repository: Optional[ISuggestionRepository] = None,
    ):
        """Initialize the suggestion service.

        Args:
            case_repository: Repository for case access
            knowledge_service: Service for creating knowledge items
            sanitizer: ISanitizer for PII detection/redaction
            llm_provider: LLM provider for extraction
            max_stored_suggestions: Cap on how many UNREVIEWED suggestions one
                organization may have queued; defaults to
                :attr:`MAX_UNREVIEWED_SUGGESTIONS`. Injectable so a test can
                drive the refusal without minting hundreds of suggestions.
            max_extraction_attempts: Total runbook-generation attempts per
                extraction, first try included; defaults to
                :attr:`MAX_EXTRACTION_ATTEMPTS`. Injectable so a test can pin
                the retry budget instead of inheriting whatever the shipped
                number happens to be.
            suggestion_repository: The store (#1227) — REQUIRED. Production
                passes a ``DatabaseSuggestionRepository`` over
                ``knowledge_suggestions``; a deployment with no database
                configured, and every unit test, passes
                ``InMemorySuggestionRepository``.

                There is deliberately no default. A default would make the one
                mistake that matters — composing a service whose store nobody
                chose — silent, and it would force this domain service to
                import a concrete infrastructure class at module scope, pulling
                the ORM graph in and pinning it to one implementation. Refusing
                is what the class docstring already claimed happened.

        Raises:
            ValueError: no repository was supplied.
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

        # The store. Durable and worker-shared when it is the database
        # repository the composition root builds; a process-local double
        # otherwise. Every read and write below goes through this seam — the
        # service holds no suggestion state of its own, which is what makes an
        # extract on one pod visible to the approve on another.
        if suggestion_repository is None:
            raise ValueError(
                "SuggestionService requires a suggestion_repository. There is "
                "no default: a service whose store nobody chose is the failure "
                "this argument exists to prevent (#1227)."
            )
        self._repository: ISuggestionRepository = suggestion_repository

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

        # Refuse a full queue BEFORE spending the generation budget, not after
        # (#1226 rework). This raises ServiceUnavailableException, and running
        # it last meant a deployment whose review inbox was full burned the
        # whole budget — up to four LLM generations, 20-60 s each on the
        # measured path — producing a runbook that was then thrown away, on
        # every extract request, for as long as the queue stayed full. The
        # answer does not depend on anything generated below, so there is
        # nothing to wait for.
        #
        # Nothing is stored between here and the write below, so the check
        # remains a real ceiling rather than a ceiling plus one.
        await self._refuse_if_review_queue_full(organization_id)

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

        prompt = CONVERSION_SYSTEM_PROMPT + self.EXTRACTION_PROMPT.format(
            # The document path gets `domain` from its analysis pass and the
            # prompt tells the model not to change it; a case supplies none, so
            # the model free-picks — and picked `kubernetes`, `cache` and `web`,
            # every one of them a hard gate error, in 4 of 8 first drafts before
            # the vocabulary was named here (see the eval's --attempts 1 mode).
            domain_vocab=", ".join(VALID_DOMAINS),
            today_iso=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            source_label=f"Case {case_id}",
            case_title=case_title,
            case_description=case_description,
            messages_section=messages_section or "No messages included.",
            evidence_section=evidence_section or "No evidence included.",
        )

        # Generate the runbook, re-prompting with the validator's own errors if
        # the first draft is refused (#1226).
        suggested_content = await self._generate_runbook_draft(prompt, case_id)

        # Title, in preference order: the caller's, then the DRAFT'S OWN
        # frontmatter title, then the case title with its severity prefixes
        # stripped.
        #
        # The draft's title comes second-but-first-in-practice for the reason
        # the eval surfaced on the id (#1226): this value is what
        # ``upload_document`` publishes as the knowledge item's title AND what
        # ``runbook_filename`` names the file after, and deriving it from the
        # case title carried the incident into both — "Troubleshooting:
        # INC-48213: prod-web-07 returning 502 for customer Contoso from
        # 2026-03-14 02:11 UTC". The draft's frontmatter title is the
        # de-identified one, and it names the failure mode rather than the
        # incident, which is what a corpus entry should be called.
        suggested_title = (
            title_suggestion
            or self._title_from_draft(suggested_content)
            or await self._generate_title(case_title, suggested_content)
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

        # Scan for PII, then record the gate's verdict on whatever the scan
        # left behind (#1226). Never reuse ``validation`` from the generation
        # loop: redaction rewrites the very text approval will publish.
        await self._scan_and_record(suggestion)
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

        # Capacity was checked and made at the top of this method, before the
        # generation budget was spent.
        # Reassigned, not just called: ``save`` returns the persisted copy
        # carrying the version the store now holds, and a caller that keeps the
        # pre-save object would fail its OWN next write's concurrency check
        # against a row only it had touched.
        suggestion = await self._repository.save(suggestion)
        self.logger.info(f"Created suggestion {suggestion_id} from case {case_id}")

        return suggestion

    async def _refuse_if_review_queue_full(self, organization_id: str) -> None:
        """Refuse a new extraction when ``organization_id``'s inbox is full.

        A PENDING_REVIEW or DRAFT suggestion is the one thing in this store that
        exists nowhere else, so the ceiling is enforced by refusing to add to it
        — never by removing something a reviewer has not seen, and never by
        removing anything at all.

        This REPLACED an eviction policy, and the change is deliberate (#1227).
        The old cap bounded a process-local dict and made room by deleting the
        oldest APPROVED/REJECTED entry, on the reasoning that a decided
        suggestion "loses only history". That reasoning does not survive the
        move to a table: ``knowledge_items`` carries no back-pointer, so an
        approved suggestion's ``knowledge_item_id`` is the ONLY link from a case
        to the runbook it produced, and deleting the row destroys the provenance
        the knowledge flywheel exists to accumulate. A process-memory bound
        became permanent destruction, so the bound moved to the thing that
        actually needs bounding.

        Scoped to ONE organization, because the store is a table shared by every
        tenant. A deployment-wide count would let one tenant's undrained inbox
        refuse another tenant's extraction.

        Called BEFORE the generation budget is spent, not after: it raises, and
        running it last meant a deployment whose inbox was full burned up to
        four LLM generations producing a runbook that was then thrown away, on
        every extract request, for as long as the queue stayed full.

        Args:
            organization_id: the tenant the extraction is being stored under

        Raises:
            ServiceUnavailableException: this organization's review queue is
                full. The route answers 503, which is honest — the queue is full
                and the fix is to review it.
        """
        capacity = self._max_stored_suggestions
        if capacity <= 0:
            return
        unreviewed = await self._repository.count_for_organization(
            organization_id, statuses=UNREVIEWED_STATUSES
        )
        if unreviewed < capacity:
            return

        self.logger.error(
            "Review queue is full for organization %s (%d/%d unreviewed); "
            "refusing to extract more knowledge until the review inbox is "
            "drained. Nothing is evicted: an approved suggestion is the only "
            "link from its case to the runbook it produced",
            organization_id,
            unreviewed,
            capacity,
        )
        # DIAGNOSTIC wording, with the numbers an operator needs. The
        # user-facing sentence is the route's (``SUGGESTION_QUEUE_FULL``) —
        # not this string re-rendered, because a domain service may not
        # import the API layer (import-linter contract 2) and because the
        # route's own AST guard forbids echoing a caught exception into a
        # 5xx body anyway. One audience each, no duplication.
        raise ServiceUnavailableException(
            f"Suggestion review queue at capacity ({unreviewed}/{capacity}) "
            f"for this organization"
        )

    async def _generate_runbook_draft(self, base_prompt: str, case_id: str) -> str:
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
            case_id: Source case, used only as the last-resort id stem.

        Returns:
            The best draft produced — the first one that passes, else the one
            with the fewest gate errors (earliest on a tie), else the skeleton
            template when no draft was produced at all.
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

            content = _force_frontmatter_id(content, self._mint_id(content, case_id))
            validation = self._validator.validate_content(content)

            # Keep the FEWEST-ERRORS draft, not simply the latest (#1226
            # rework). A repair turn is not monotonic — it can come back worse,
            # having "fixed" three flagged errors by restructuring a section
            # into two new ones — and overwriting unconditionally would hand the
            # reviewer that regression while the docstring promised them the
            # best draft. Ties keep the EARLIER draft: it is the one the model
            # produced from the case alone, without a repair turn's pressure to
            # edit.
            if best_validation is None or len(validation.errors) < len(
                best_validation.errors
            ):
                best, best_validation = content, validation

            if validation.passed:
                if attempt > 1:
                    self.logger.info(
                        "Extraction draft passed the runbook quality gate on "
                        "attempt %d/%d after repair",
                        attempt,
                        self._max_extraction_attempts,
                    )
                # ``best`` is this draft: zero errors always wins the comparison
                # above. Returned through the same variable so the passing path
                # and the exhausted path cannot disagree about what "best" is.
                return best

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
                "Extraction exhausted its %d-attempt budget; returning the best "
                "draft with %d unresolved gate error(s) for review",
                self._max_extraction_attempts,
                len(best_validation.errors) if best_validation else 0,
            )
            return best

        return self.fallback_template(self._case_stem_id(case_id))

    @staticmethod
    def _case_stem_id(case_id: str) -> str:
        """The last-resort runbook id: the case's own identifier, slugged.

        Opaque, but an internal case identifier and therefore safe to publish —
        unlike the case TITLE, which names an incident (see :meth:`_mint_id`).
        """
        return (
            _as_kebab(runbook_id_from_parts("case", case_id))
            or "extracted-runbook-draft"
        )

    def _mint_id(self, content: str, case_id: str) -> str:
        """The kebab-case ``id`` to force onto a draft's frontmatter.

        Minted from the draft's OWN ``service`` + ``title`` — the same
        ``(service, title)`` mint the conversion path uses — and deliberately
        NOT from the case title.

        That distinction was measured, not reasoned about. The first cut minted
        from the case title, and the eval's deliberately-noisy fixture
        ("INC-48213: prod-web-07 returning 502 for customer Contoso from
        2026-03-14 02:11 UTC") produced a body the model had de-identified
        perfectly and a frontmatter line reading
        ``id: case-inc-48213-prod-web-07-returning-502-for-customer-c-fd3a``.
        The id is inside the content, so it is chunked, embedded and retrieved:
        a ticket number, a hostname and a customer name would have entered the
        global corpus through the one field the extractor writes itself.

        The emitted title is de-identified because the prompt says so; the mint
        is normalisation only, so a title that slipped is a prompt failure, not
        one this can catch. Falls back to the case stem when the draft carries
        no usable title (an empty slug would otherwise mean no ``id`` at all).
        """
        try:
            # The validator's frontmatter parse, reused so "what the id is
            # derived from" is the same text the gate will read it as.
            metadata = self._validator._extract_metadata(content) or {}
        except Exception:
            metadata = {}
        title = metadata.get("title") if isinstance(metadata, dict) else None
        service = metadata.get("service") if isinstance(metadata, dict) else None
        if isinstance(title, str) and title.strip():
            minted = _as_kebab(
                runbook_id_from_parts(
                    service if isinstance(service, str) else "", title
                )
            )
            if minted:
                return minted
        return self._case_stem_id(case_id)

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
        no draft there is no knowledge here, only a form, and letting a click
        publish ``[INSUFFICIENT SOURCE DATA]`` into the global corpus would be a
        hole this lane opened rather than closed — the prose template it
        replaces was unapprovable too, just for a worse reason (it was not a
        runbook at all).

        What blocks it is not a contrivance bolted on to force a failure: ALL
        THREE of Cause A's required sub-fields — ``**Statement:**``,
        ``**Indicators:**``, ``**Interventions:**`` — are empty, because there
        genuinely is no cause, no observable and no fix to record. An empty
        required sub-field is already a gate error, so the reviewer gets the v4
        shape to fill in and three precise sentences saying what is missing.

        Three, not one, and that is the point (#1226 rework). Leaving only the
        Statement empty made the form ONE keystroke from publishable: filling
        it cleared the single remaining error while every other field, the
        frontmatter ``title`` included, still read ``[INSUFFICIENT SOURCE
        DATA]``. The three that are empty now are the cause, its evidence and
        its fix — the runbook's actual knowledge — so clearing them is
        authoring, not a formality.

        What this does NOT claim: that a filled-in form is a GOOD runbook. The
        gate is structural and cannot tell grounded prose from plausible prose
        (the same limit the eval records for a thin case), so the remaining
        ``[INSUFFICIENT SOURCE DATA]`` markers are visible to a human reviewer
        and to nothing else.

        ``tests/unit/modules/knowledge/test_extraction_emits_v4_schema_1226.py``
        pins all of it: v4-shaped, refused, and still refused after any single
        sub-field is filled.
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
<!-- All three sub-fields below are required and all three are empty. Fill in,
     in order: one declarative sentence naming the single root cause; one
     bullet per observable, each tagged with the diagnostic step that shows it
     (root: [Step 1] ...); one bullet per fix, each tagged with its quadrant
     and the rung it targets, each carrying its own verification.
     Do NOT write example sub-field labels inside this comment: the cause
     parser does not strip HTML comments, so a label written here is read as
     the real field and the gate passes a form nobody filled in. -->
**Statement:**
**Indicators:**
**Interventions:**

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

    async def _scan_and_record(self, suggestion: KnowledgeSuggestion) -> None:
        """Scan for PII, then record the gate's verdict on what the scan left.

        The two are paired in ONE method rather than left as two calls every
        mutation site has to remember, because forgetting the second is silent:
        the suggestion keeps a ``validation_passed`` describing text that no
        longer exists, and a reviewer reads a green verdict on content the gate
        would refuse. That is exactly what happened at the third site — the
        SCAN_FAILED re-scan inside ``approve_suggestion`` — while the other two
        got it right (#1226 rework).

        Ordering is load-bearing and always this way round: the scan may REDACT,
        redaction can itself break the gate (a redacted frontmatter value, a
        redacted Cause Statement), and the verdict must be about the text
        approval will actually publish.
        """
        await self._scan_for_pii(suggestion)
        self._record_validation(suggestion)

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

    def _title_from_draft(self, content: str) -> Optional[str]:
        """The draft's own frontmatter ``title``, when it has a usable one.

        ``None`` for a draft with no frontmatter, no title, or the rule-8
        ``[INSUFFICIENT SOURCE DATA]`` placeholder the skeleton carries — that
        last one is a form, not a name, and putting it in the review inbox as a
        heading would say less than the case title does.
        """
        try:
            metadata = self._validator._extract_metadata(content) or {}
        except Exception:
            return None
        if not isinstance(metadata, dict):
            return None
        title = metadata.get("title")
        if not isinstance(title, str):
            return None
        title = title.strip()
        if not title or "INSUFFICIENT SOURCE DATA" in title:
            return None
        return title

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
                # TITLE AND CONTENT ARE SANITIZED SEPARATELY, and each is written
                # back to its OWN field (#1226 rework).
                #
                # This used to scan ``f"{title}\n\n{content}"`` and assign the
                # WHOLE sanitized buffer to ``suggested_content``. Two bugs in
                # one line, and both only became reachable once extraction
                # started producing real runbooks:
                #
                #   1. It DESTROYED the runbook. A v4 runbook must open with
                #      ``---`` on line 1; a title prepended in front of it
                #      guarantees that it never is. Measured over the eval's
                #      eight recorded drafts, all of which pass the gate:
                #      prepending a title flips all 8 to
                #      ``['No YAML frontmatter found']``. So on any deployment
                #      with a real sanitizer, ANY suggestion carrying PII became
                #      permanently unapprovable — in exactly the population this
                #      lane serves, since case transcripts are where PII lives.
                #      The eval measured 8/8 with ``sanitizer=None``, which takes
                #      the ``else`` branch below and never rewrites, so the
                #      headline number never touched this path.
                #   2. It never redacted the TITLE. The title was scanned but
                #      only content was stored, so PII in the title survived into
                #      ``upload_document(title=...)`` — the published knowledge
                #      item's name AND, through ``runbook_filename``, its
                #      filename on disk. The same leak class the frontmatter
                #      ``id`` had (see ``_mint_id``), one field over.
                #
                # Two calls rather than one concatenation: a title and a runbook
                # are separate documents, and the only thing the concatenation
                # ever bought was slightly wider NER context, at the cost of
                # making the result structurally unusable.
                original_title = suggestion.suggested_title or ""
                original_content = suggestion.suggested_content or ""

                sanitized_title = (
                    await self._sanitizer.asanitize(original_title)
                    if original_title
                    else original_title
                )
                sanitized_content = (
                    await self._sanitizer.asanitize(original_content)
                    if original_content
                    else original_content
                )

                title_changed = sanitized_title != original_title
                content_changed = sanitized_content != original_content

                if title_changed or content_changed:
                    suggestion.mark_pii_scan_complete(
                        status=PIIScanStatus.PII_DETECTED,
                        result={
                            "original_length": len(original_title)
                            + len(original_content),
                            "sanitized_length": len(sanitized_title)
                            + len(sanitized_content),
                            "pii_removed": True,
                            "title_redacted": title_changed,
                            "content_redacted": content_changed,
                            "message": "PII detected. Manual review required before approval.",
                        },
                    )
                    # Each field back into its own slot. Never the concatenation.
                    suggestion.suggested_title = sanitized_title
                    suggestion.suggested_content = sanitized_content
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
        return await self._repository.get(suggestion_id)

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
        return await self._repository.get_for_organization(
            suggestion_id, organization_id
        )

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

        # Filtering, ordering (newest first) and pagination are the store's, so
        # the database does them in SQL instead of this service loading every
        # row to slice three of them.
        suggestions, total_count = await self._repository.list_for_organization(
            organization_id,
            status=status,
            limit=limit,
            offset=offset,
        )

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
            # Through the paired helper, not a bare scan: this re-scan can
            # REDACT, and a redaction rewrites the text the recorded verdict
            # describes. Left unpaired it was the one mutation site of three
            # that skipped ``_record_validation``, so a suggestion could reach
            # the reviewer reading ``validation_passed=True`` about content that
            # no longer existed (#1226 rework).
            await self._scan_and_record(suggestion)
            # Persist the re-scan whatever it concluded. The loaded suggestion
            # is a detached copy of the row (#1227), so without this a
            # successful re-scan is discarded on the way out and the next
            # approve re-runs it — and a redaction the scan applied to the
            # content would be lost while the verdict it produced was not.
            #
            # Reassigned, so the object carries the version this write produced.
            # Without that, the approval's own later save would be checked
            # against a version IT had already superseded and would report a
            # concurrent modification that never happened.
            suggestion = await self._repository.save(suggestion)

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
        #
        # The ``save`` is inside the SAME try for the same reason the mutation
        # is (#1227). Marking the loaded copy approved changes nothing until it
        # is written back, so a store failure here leaves a published knowledge
        # item that no suggestion links to — the identical orphan, arrived at
        # one line later.
        #
        # AND it is where the cross-process double-approve is stopped. The
        # ``is_approved()`` guard above reads a DETACHED COPY, so on two pods it
        # is a TOCTOU: both load PENDING_REVIEW, both pass, both publish. That
        # guard was sound while one worker owned the store as a single live
        # object; #1227 removes that premise, so the real decision has to be
        # taken by the database. ``save`` is an optimistically-locked UPDATE
        # (``WHERE version = :loaded``), so exactly one of the two racing
        # approvals commits and the loser raises
        # ``SuggestionConcurrencyError`` — at which point the ``except`` below
        # rolls ITS OWN published item back out of the global corpus and the
        # caller gets a 409. Net effect: one knowledge item survives, linked;
        # the duplicate is created and then removed rather than left orphaned.
        #
        # Preventing the second publish outright would need a claim written
        # before ``upload_document`` runs, which means a durable "approving"
        # state that a crashed pod never releases. Publish-then-claim reuses the
        # compensation that already exists and leaves no state that can get
        # stuck; the cost is transient duplicate work in a rare race.
        try:
            suggestion.approve(
                reviewed_by=reviewed_by,
                knowledge_item_id=knowledge_item_id,
                review_notes=review_notes,
            )
            await self._repository.save(suggestion)
        except SuggestionConcurrencyError:
            self.logger.warning(
                "Concurrent approval detected for suggestion %s: another writer "
                "committed first, so this approval's knowledge item %s is being "
                "rolled back",
                suggestion_id,
                knowledge_item_id,
            )
            await self._rollback_published_item(knowledge_item_id, suggestion_id)
            raise ConflictError(
                "Suggestion was decided by another reviewer while this "
                "approval was in flight",
                resource_type="suggestion",
                resource_id=suggestion_id,
                conflict_reason="concurrent_modification",
            ) from None
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
        suggestion = await self._repository.save(suggestion)

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
            # Re-scan for PII since content changed, and re-record the gate's
            # verdict on the result. This is the loop the reviewer actually
            # works in — edit, see which errors cleared, edit again — so a stale
            # verdict here is worse than none.
            await self._scan_and_record(suggestion)

        if suggested_type:
            suggestion.suggested_type = suggested_type
            suggestion.touch()

        suggestion = await self._repository.save(suggestion)
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
        suggestion = await self._repository.save(suggestion)
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
