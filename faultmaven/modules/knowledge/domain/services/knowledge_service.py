"""Knowledge Service Refactored Module - Phase 3.3

Purpose: Interface-based knowledge service using dependency injection

This refactored service implements clean architecture principles by depending
on interfaces rather than concrete implementations, enabling better testability
and flexibility in the knowledge management system.

Core Responsibilities:
- Knowledge document management via interfaces
- Semantic search operations using IVectorStore
- Knowledge ingestion through IKnowledgeIngester
- Content validation and sanitization via ISanitizer
- Distributed tracing via ITracer

Key Improvements over Original:
- Interface-based dependency injection
- Cleaner separation of concerns
- Better error handling and validation
- Improved testability through mocking interfaces
- Standardized tracing and logging patterns
"""

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    # Type-only import; the runtime import stays lazy inside ingest_runbook
    # to avoid the service↔persistence cycle. This satisfies the forward-ref
    # annotation on ``verification_level`` for ruff/mypy without importing at
    # module load.
    from faultmaven.modules.knowledge.domain.models.knowledge_item import (
        VerificationLevel,
    )

from faultmaven.config.tenant_context import usable_tenant_id
from faultmaven.exceptions import ServiceException, ValidationException
from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
    KB_COLLECTION,
)
from faultmaven.models import KnowledgeBaseDocument, SearchResult
from faultmaven.models.exceptions import KnowledgeBaseError
from faultmaven.models.interfaces import (
    IKnowledgeIngester,
    ILLMProvider,
    ISanitizer,
    ITracer,
    IVectorStore,
)
from faultmaven.models.vector_metadata import VectorMetadata
from faultmaven.utils.serialization import to_json_compatible

logger = logging.getLogger(__name__)

# Chunk ids are minted as f"{parent_document_id}_chunk_{i}" (see
# ``_index_document_in_vector_store``). Recover the parent id when chunk
# metadata omits ``parent_document_id``.
_CHUNK_SUFFIX_RE = re.compile(r"_chunk_\d+$")


def _strip_chunk_suffix(chunk_id: Optional[str]) -> Optional[str]:
    """Return the parent document id encoded in a chunk id, or None.

    Fallback only — used when chunk metadata omits ``parent_document_id``.
    Assumes a parent id does not itself end in ``_chunk_<n>`` (KB item ids are
    ``kb_<hash>`` slugs, so this holds); a parent literally ending that way would
    be over-stripped. Behind ``metadata["parent_document_id"]`` in practice, so
    this path is rarely hit.
    """
    if not chunk_id:
        return None
    stripped = _CHUNK_SUFFIX_RE.sub("", chunk_id)
    return stripped or None


def _matched_cause_letters(chunk_text: str) -> List[str]:
    """Cause letters whose ``### Cause X:`` heading appears in this chunk's text.

    The join key the KB cause seeder needs: retrieval is chunk-level and a
    runbook's ``## Causes`` section chunks one-Cause-per-chunk, so the headings
    inside a matched chunk say WHICH of the parent runbook's
    ``metadata["causes"]`` records the query actually matched. Without it the
    seeder can only name the parent runbook, and falls back to seeding its first
    N causes in author order — the #1092 defect, where a k8s OOM case seeded a
    GKE runbook's three *unschedulable* causes (A/B/C) while the OOMKilled cause
    that matched (D) sat one slot past the cap.

    Uses the SHARED ``CAUSE_HEADING_RE`` (``runbook_grammar``) — the same pattern
    the extractor and pack builder use to mint ``cause_letter`` — so the letter
    parsed here is by construction the letter the seeder joins on.

    Searches the whole chunk rather than anchoring at its start: a runbook's
    first Cause commonly shares a chunk with the ``## Causes`` section header
    (60 of 91 shipped runbooks), so an anchored match would drop Cause A on
    two-thirds of the corpus. Returns every letter found, in appearance order —
    a chunk spanning two headings was embedded as one text, so a hit on it is
    evidence for both causes and attributing to only one would be arbitrary.

    Returns [] for a non-cause chunk or a non-runbook document; the seeder reads
    that as "retrieval surfaced no cause here", not as an error.
    """
    if not chunk_text:
        return []
    from faultmaven.modules.knowledge.domain.services.runbook_grammar import (
        CAUSE_HEADING_RE,
    )

    seen: List[str] = []
    for letter, _name in CAUSE_HEADING_RE.findall(chunk_text):
        if letter not in seen:
            seen.append(letter)
    return seen


def _letter_can_head_a_cause(letter: str) -> bool:
    """Could a ``### Cause X:`` heading for this letter exist at all?

    Asked OF the shared grammar rather than by restating its ``[A-Z]`` character
    class here, so the two cannot drift: a letter the grammar widens to admit
    tomorrow is admitted here the same day.

    Used only to word the alarm. A record declaring ``cause_letter: "a"`` is
    genuinely unseedable — the seeder's join is case-sensitive — so it is still
    reported; this just keeps the report from blaming markdown that is fine.
    """
    from faultmaven.modules.knowledge.domain.services.runbook_grammar import (
        CAUSE_HEADING_RE,
    )

    match = CAUSE_HEADING_RE.match(f"### Cause {letter}: name")
    # The capture must be the whole letter, not a prefix of it: a record value
    # like "A: x" builds a heading the pattern happily matches, capturing just
    # "A" — a letter retrieval would never recover under the name declared.
    return bool(match and match.group(1) == letter)


# Bumped when the meaning of a chunk stamp changes in a way that makes already
# written stamps wrong (a new stamped field, a different join key, a changed
# encoding) — NOT for unrelated ``VectorMetadata`` additions.
CHUNK_STAMP_SCHEMA = 1


def chunk_stamp_identity() -> str:
    """A short digest of everything a chunk's cause stamp depends on.

    Stored on the runbook row (``metadata["chunk_stamp"]``) and compared by the
    KB bootstrap's idempotency gate, so a change here forces a re-ingest of the
    shipped pack instead of leaving stamps that no longer mean what they say.

    It covers the grammar as well as the schema, and that is the load-bearing
    half. ``CAUSE_HEADING_RE`` is a MANUAL MIRROR of kb-toolkit's, expected to
    change (a cross-repo CI job requires it) — and before fm#1108 nothing about
    such a change re-ingested anything, so a code-only edit silently
    re-interpreted chunks already in the store. Deriving the identity from the
    live pattern makes that automatic rather than a discipline someone has to
    remember: edit the grammar, and the next boot re-stamps the pack.

    Pack re-ingest is prechunked — no embedding model, no re-chunking — so
    forcing it is seconds of boot, not the minutes the pack exists to avoid.
    It does NOT reach authored runbooks; ``kb_init`` only ever walks the pack.
    Those re-stamp when they are next verified, edited or repaired, and
    ``kb_cause_letters_unstamped_total`` is what says how much of live retrieval
    is still waiting on that.
    """
    from faultmaven.modules.knowledge.domain.services.runbook_grammar import (
        CAUSE_HEADING_RE,
    )

    payload = (
        f"{CHUNK_STAMP_SCHEMA}|{CAUSE_HEADING_RE.pattern}|{CAUSE_HEADING_RE.flags}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _read_stamped_cause_letters(
    chunk_metadata: Optional[Dict[str, Any]], chunk_text: str
) -> List[str]:
    """The hit's cause letters: read the stamp, or parse if it predates one.

    The stamp (``VectorMetadata.cause_letters``) is written at index time by the
    same grammar pass that extracts the parent's ``metadata["causes"]`` record,
    so reading it makes the seeder's join a stored fact rather than a read-time
    re-derivation. That is the whole of fm#1108: a derivation is a function of
    the code in force when it RUNS, and this one ran on every retrieval against
    a grammar that is a manual mirror of kb-toolkit's and expected to change.
    Nothing re-ingests on a grammar change, so an edit silently re-interpreted
    chunks already in the store while their record stayed as the old grammar
    left it.

    KEY-ABSENCE, not emptiness, selects the fallback. ``""`` is a real stamp
    meaning "no cause heading in this chunk" and is stored as such; a chunk
    written before fm#1108 has no key at all. Only the latter is re-parsed —
    which reproduces exactly the old behaviour for old data, so this is strictly
    better than before for everything and worse than before for nothing.

    Each fallback increments ``kb_cause_letters_unstamped_total``. It is a
    DRAIN GAUGE: it tells you how much of live retrieval still depends on the
    derivation, and its arrival at a steady zero is the signal that this
    fallback — and with it the last read-time parse — can be deleted.
    """
    if chunk_metadata is not None and "cause_letters" in chunk_metadata:
        raw = chunk_metadata.get("cause_letters")
        return [letter for letter in str(raw or "").split(",") if letter]

    letters = _matched_cause_letters(chunk_text)
    try:
        from faultmaven.core.investigation.lifecycle_metrics import (
            kb_cause_letters_unstamped_total,
        )

        kb_cause_letters_unstamped_total.inc()
    except Exception:  # pragma: no cover - metrics must never break retrieval
        pass
    return letters


def _carried_cause_letters(letters_per_chunk: List[List[str]]) -> set:
    """Flatten per-chunk letter lists, refusing the one wrong shape that is silent.

    Handed ``List[str]`` (chunk TEXTS) instead of ``List[List[str]]`` (parsed
    letters), ``set.update`` would iterate each string into its CHARACTERS — and
    since cause letters are single uppercase characters that appear in ordinary
    prose, the result looks plausible and the callers report nothing. A wrong
    answer that passes for a right one is precisely what this module exists to
    prevent, so the shape is checked rather than trusted. The callers already
    swallow-and-report, so this surfaces as a loud check failure.
    """
    carried: set = set()
    for letters in letters_per_chunk:
        if isinstance(letters, str):
            raise TypeError(
                "letters_per_chunk holds chunk text, not parsed letters — "
                "iterating it would silently match individual characters"
            )
        carried.update(letters)
    return carried


def _declared_cause_letters(causes: Optional[List[Dict[str, Any]]]) -> List[str]:
    """The letters a ``metadata["causes"]`` record declares, in record order.

    Scope is deliberately the letters the record DECLARES. An entry carrying no
    usable ``cause_letter`` is also unseedable, but it is a malformed record
    rather than a record/chunk disagreement — the extractor and the runbook
    validator own that, and folding it in here would fire the callers' alarms
    for a defect their messages cannot describe.
    """
    declared: List[str] = []
    for cause in causes or []:
        if not isinstance(cause, dict):
            continue
        letter = str(cause.get("cause_letter") or "")
        if letter and letter not in declared:
            declared.append(letter)
    return declared


def _unrecoverable_cause_letters(
    letters_per_chunk: List[List[str]], causes: Optional[List[Dict[str, Any]]]
) -> List[str]:
    """Cause letters the record declares that NO chunk carries (fm#1103).

    The seeder names which of a runbook's ``metadata["causes"]`` records a hit
    matched by joining the hit chunk's ``### Cause X:`` letters against
    ``cause_letter``. A cause whose letter appears in no chunk is therefore
    structurally unseedable — permanently, silently, and no matter how often the
    runbook is retrieved.

    Takes the letters ALREADY PARSED for the stamp rather than re-parsing the
    chunk texts (fm#1108): what is checked is then literally what retrieval will
    read, and the two cannot disagree about what a cause heading is.

    Returns the missing letters in record order, or [] when the record is
    empty/absent — the overwhelmingly common case, and the healthy one.
    """
    declared = _declared_cause_letters(causes)
    if not declared:
        return []
    carried = _carried_cause_letters(letters_per_chunk)
    return [letter for letter in declared if letter not in carried]


def _unrecorded_chunk_letters(
    letters_per_chunk: List[List[str]], causes: Optional[List[Dict[str, Any]]]
) -> List[str]:
    """The reverse disagreement: letters the CHUNKS carry that the record lacks.

    Same defect from the other end, and until fm#1108 it was visible only from
    retrieval — ``kb_cause_seed_letter_mismatch_total`` fires when a matched
    chunk's heading names a letter the record has no entry for, by which point a
    case has already been served without those seeds. Both directions are
    decidable at write time, because both sides are built here.

    Usually means a ``### Cause X:`` heading outside the ``## Causes`` section
    the extractor reads (the extractor scans that section; the stamp scans the
    whole chunk), so retrieval can match a heading that names nothing.

    Silent when the record is absent: a document with cause headings and no
    record is the ordinary anonymous-upload runbook, which is never seedable by
    design rather than by defect (``upload_document`` passes no ``causes``, and
    ``get_runbook_causes`` refuses EXPERIMENTAL rows). Alarming there would fire
    on healthy content.
    """
    declared = set(_declared_cause_letters(causes))
    if not declared:
        return []
    seen: List[str] = []
    for letter in sorted(_carried_cause_letters(letters_per_chunk)):
        if letter not in declared:
            seen.append(letter)
    return seen


def _row_causes(knowledge_metadata: Any) -> Optional[List[Dict[str, Any]]]:
    """Read ``causes`` off a ``knowledge_items`` metadata value, however it arrives.

    Callers hand it either shape and neither has to know which: the *decoded*
    dict off a domain ``KnowledgeItem.metadata``, or the *raw* column off an ORM
    row. The raw one is why both branches are needed — ``JsonBlob`` is
    ``Text().with_variant(JSONB, "postgresql")``, so it comes back as a JSON
    string or an object depending on backend and writer, and handling one shape
    loses the record silently on the other (the bug
    ``kb_init._decode_metadata`` documents). Read-only: the dict branch aliases
    its caller's attribute, so callers must not mutate.

    A third copy of that decode, alongside ``kb_init._decode_metadata`` and
    ``KnowledgeItemRepository._parse_json_dict``. Kept local for the same reason
    the second one was: reaching for either would mean a domain service calling a
    bootstrap-private helper or a repository-private method, to read one key.

    Returns None unless the value decodes to a dict whose ``causes`` is a list —
    the same tolerance :meth:`get_runbook_causes` applies to the domain shape.
    A value that is present but undecodable warns on the way to that None: it is
    the one "no record" answer that is really "unread record", and it silently
    disables the caller's check.
    """
    if isinstance(knowledge_metadata, dict):
        decoded: Any = knowledge_metadata
    elif knowledge_metadata:
        try:
            decoded = json.loads(knowledge_metadata)
        except (json.JSONDecodeError, TypeError) as decode_error:
            # An undecodable value is NOT "no record" — it is a record we could
            # not read, and returning None quietly makes it indistinguishable
            # from a prose runbook. That matters here more than it looks: the
            # caller reads this to CHECK the record, so a silent miss disables
            # the check for exactly the row whose metadata is already suspect.
            logger.warning(
                "Unreadable knowledge_items metadata (%s): treating the row as "
                "carrying no causes record, so its causes/chunk agreement goes "
                "unchecked.",
                decode_error,
            )
            return None
    else:
        return None
    if not isinstance(decoded, dict):
        return None
    causes = decoded.get("causes")
    return causes if isinstance(causes, list) else None


def build_kb_scope_filter(
    owner_id: Optional[str], shared_ids: Optional[List[str]] = None
) -> Dict[str, Any]:
    """Build a vector-store ``where`` filter for the KB items a principal may read.

    The visible-id allowlist (ADR-011 D3): ``global`` ∪ items the principal
    ``owns`` ∪ items ``shared to any of the principal's teams``, as a ChromaDB
    ``$or`` (or the bare single condition when only global applies):

    - ``{"scope": "global"}`` — platform corpus, readable by all.
    - ``{"owner_id": owner_id}`` — the principal's own items, any scope (an
      author always sees their own). Vector metadata tags only the immutable
      floor (personal/global) + owner, never team, so this arm is unshare-proof.
    - ``{"parent_document_id": {"$in": shared_ids}}`` — items shared to one of
      the principal's teams. ``shared_ids`` (``knowledge_items.item_id`` values)
      is resolved in SQL from the ``resource_shares`` table by the caller, which
      is the single source of truth for team visibility. Omitted when empty
      (ChromaDB rejects an empty ``$in``).

    Keyed entirely on the caller's own ids/allowlist, so a filter built for user
    A can never surface user B's non-shared content. Shared by the QA retrieval
    path (``search_documents``) and the KB cause-seeder pre-fetch
    (``MilestoneEngine._prefetch_kb_context``).
    """
    scope_conditions: List[Dict[str, Any]] = [{"scope": "global"}]
    if owner_id:
        scope_conditions.append({"owner_id": owner_id})
    if shared_ids:
        scope_conditions.append({"parent_document_id": {"$in": list(shared_ids)}})
    return (
        {"$or": scope_conditions} if len(scope_conditions) > 1 else scope_conditions[0]
    )


async def resolve_shared_kb_ids(
    share_repository: Optional[Any],
    team_ids: Optional[List[str]],
    organization_id: Optional[str],
) -> List[str]:
    """Resolve the ``knowledge_item`` ids shared to any of ``team_ids``.

    The team arm of the visible-id allowlist (ADR-011 D3). Returns ``[]`` when
    there is no share repository (e.g. an in-memory fallback), the principal
    belongs to no teams, or no organization is in hand — retrieval then
    collapses to ``personal ∪ global``, which is the fail-closed outcome: the
    two remaining arms are keyed on the caller's own ids.

    ``organization_id`` is the tenant the share row must itself be stamped
    with, matching the inventory clause's share sub-select. It is resolved
    through ``usable_tenant_id`` rather than used raw, because both callers hand
    over a value that can be the Standalone sentinel under
    ``TENANT_PROVIDER=multi`` — ``MilestoneEngine`` passes ``case.organization_id``,
    which ``CaseService.create_case`` stamps from the *total*
    ``get_current_org_id``, and ``KnowledgeService.search_documents`` passes the
    requester's claim. Under multi the sentinel is not a tenant, so it must
    collapse the arm here rather than become the SQL predicate; under ``single``
    it is the deployment's one legitimate tenant and passes unchanged. This is
    the same decision ``require_actor_organization`` refuses on and the case
    read-allowlist arms degrade on — one predicate, three call sites.
    """
    tenant_id = usable_tenant_id(organization_id)
    if not share_repository or not team_ids or not tenant_id:
        return []
    return await share_repository.list_resource_ids(
        resource_type="knowledge_item",
        scope_type="team",
        scope_ids=list(team_ids),
        organization_id=tenant_id,
    )


class KnowledgeService:
    """Knowledge service using interface dependencies"""

    def __init__(
        self,
        knowledge_ingester: IKnowledgeIngester,
        sanitizer: ISanitizer,
        tracer: ITracer,
        vector_store: Optional[IVectorStore] = None,
        redis_client: Optional[object] = None,
        settings: Optional[Any] = None,
        llm_provider: Optional[
            ILLMProvider
        ] = None,  # Enhanced: LLM for intelligent processing
        *,
        db_session_factory: Any,
        share_repository: Optional[Any] = None,
    ):
        """
        Initialize with interface dependencies for better testability

        Args:
            knowledge_ingester: Interface for document ingestion operations
            sanitizer: Interface for data sanitization (PII redaction)
            tracer: Interface for distributed tracing
            vector_store: Optional interface for vector database operations
            redis_client: Optional Redis client for metadata storage
            settings: Configuration settings for the service
            llm_provider: Optional LLM for intelligent query processing
            db_session_factory: Async session factory. REQUIRED and keyword-only.
                ``knowledge_items`` is the relational source of truth for the
                published inventory, so a service without a session factory has
                no working read or write path — it is not a degraded service,
                it is a broken one. Omitting it is a ``TypeError`` and passing
                ``None`` is a ``ValueError``, both at construction rather than
                an empty KB in production (#894/#899). Keyword-only because
                this signature's positional tail has already shifted once
                (#894).

        Raises:
            ValueError: if ``db_session_factory`` is None.
        """
        # Rejecting the VALUE, not just the omission. #894 was a None default
        # that the container never overrode, so None is the shape this class
        # actually failed in — and with the per-path guards gone it is now the
        # worse failure: every call raises TypeError inside a `try`, which the
        # broad handlers below turn back into an empty page or a None the KB
        # cause seeder reads as "prose-only source, nothing to seed". Exactly
        # the silent degradation this contract exists to remove.
        if db_session_factory is None:
            raise ValueError(
                "KnowledgeService requires a db_session_factory; None is not a "
                "valid session source. Every KB read and write path needs it, "
                "and it is what binds the RLS tenant scope per transaction."
            )

        self._ingester = knowledge_ingester
        self._sanitizer = sanitizer
        self._tracer = tracer
        self._vector_store = vector_store
        self._redis = redis_client
        self._settings = settings
        self._db_session_factory = db_session_factory
        # Source of truth for team visibility (ADR-013 §D4). Used to create share
        # rows on team publish and to resolve the shared-id read allowlist. May be
        # None (e.g. minimal/test wiring) — team sharing then no-ops.
        self._share_repo = share_repository

        # Enhanced capabilities
        self._llm = llm_provider

    async def ingest_document(
        self,
        title: str,
        content: str,
        document_type: str,
        tags: Optional[List[str]] = None,
        source_url: Optional[str] = None,
    ) -> KnowledgeBaseDocument:
        """
        Ingest document using interface dependencies

        Args:
            title: Document title
            content: Document content
            document_type: Type of document (e.g., 'manual', 'troubleshooting')
            tags: Optional tags for categorization
            source_url: Optional source URL

        Returns:
            KnowledgeBaseDocument model

        Raises:
            ValueError: If validation fails
            RuntimeError: If ingestion fails
        """
        with self._tracer.trace("knowledge_service_ingest_document"):
            logger.info(f"Ingesting document: {title}")

            # Validate input
            self._validate_document_data(title, content)

            # Sanitize content for privacy compliance
            sanitized_content = await self._sanitizer.asanitize(content)
            sanitized_title = await self._sanitizer.asanitize(title)

            # Generate unique document ID
            document_id = self._generate_document_id(sanitized_title, document_type)

            # Prepare metadata
            metadata = {
                "tags": tags or [],
                "source_url": source_url,
                "document_type": document_type,
                "created_at": to_json_compatible(datetime.now(timezone.utc)),
            }

            try:
                # Ingest via interface with tracing
                with self._tracer.trace("knowledge_document_ingestion"):
                    result_id = await self._ingester.ingest_document(
                        title=sanitized_title,
                        content=sanitized_content,
                        document_type=document_type,
                        metadata=metadata,
                    )
            except ValidationException:
                # Re-raise validation exceptions
                raise
            except RuntimeError:
                # Re-raise runtime exceptions
                raise
            except Exception as e:
                # Wrap external ingester exceptions in ServiceException
                logger.error(f"Knowledge ingestion failed: {e}")
                raise ServiceException(
                    f"Document ingestion failed: {str(e)}",
                    details={
                        "operation": "ingest_document",
                        "title": sanitized_title,
                        "error": str(e),
                    },
                ) from e

            # Create response model with proper error handling
            try:
                document = KnowledgeBaseDocument(
                    document_id=result_id,
                    title=sanitized_title,
                    content=sanitized_content,
                    document_type=document_type,
                    tags=tags or [],
                    source_url=source_url,
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
            except Exception as model_error:
                raise RuntimeError(
                    f"Failed to create document model: {str(model_error)}"
                ) from model_error

            # Index in vector store if available
            if self._vector_store:
                await self._index_document_in_vector_store(document)

            logger.info(f"Successfully ingested document {result_id}")
            return document

    async def search_knowledge(
        self, query: str, limit: int = 10, filters: Optional[Dict[str, Any]] = None
    ) -> List[SearchResult]:
        """
        Search knowledge base using interface dependencies

        Args:
            query: Search query text
            limit: Maximum number of results to return
            filters: Optional filters for search refinement

        Returns:
            List of SearchResult models

        Raises:
            ValueError: If query is empty or invalid
        """
        with self._tracer.trace("knowledge_service_search"):
            logger.debug(f"Searching knowledge base: {query}")

            # Validate and sanitize query
            if not query or not query.strip():
                raise ValueError("Query cannot be empty")

            sanitized_query = await self._sanitizer.asanitize(query)

            try:
                # Search via vector store interface if available
                if self._vector_store:
                    with self._tracer.trace("knowledge_vector_search"):
                        # Default to global scope when caller provides no filter —
                        # prevents unintentional cross-tenant reads.
                        effective_filters = filters if filters else {"scope": "global"}
                        results = await self._vector_store.search(
                            collection_name=KB_COLLECTION,
                            query=sanitized_query,
                            k=limit,
                            where=effective_filters,
                        )

                    # Convert to SearchResult models
                    search_results = []
                    for result in results:
                        result_meta = result.get("metadata") or {}
                        # Parent runbook identity lives in chunk metadata; it is
                        # the knowledge_items row holding metadata["causes"]. Fall
                        # back to stripping the "_chunk_N" suffix off the chunk id
                        # (id == f"{parent}_chunk_{i}") when metadata omits it.
                        parent_document_id = result_meta.get(
                            "parent_document_id"
                        ) or _strip_chunk_suffix(result.get("id"))
                        # Which CAUSES of that runbook this hit matched — the
                        # seeder's join key, READ rather than re-derived
                        # (fm#1108). It was stamped at index time by the same
                        # grammar pass that extracted the parent's causes record,
                        # so the two are pinned to one another and a later change
                        # to ``CAUSE_HEADING_RE`` cannot reach back and silently
                        # re-interpret chunks already in the store.
                        matched_cause_letters = _read_stamped_cause_letters(
                            result_meta, result.get("content") or ""
                        )
                        search_result = SearchResult(
                            document_id=result.get(
                                "document_id", result.get("id", "unknown")
                            ),
                            title=result.get("title", "Untitled"),
                            document_type=result.get("document_type", "general"),
                            tags=result.get("tags", []),
                            score=result.get("score", 0.0),
                            snippet=result.get("snippet", result.get("content", ""))[
                                :200
                            ]
                            + "...",
                            parent_document_id=parent_document_id,
                            matched_cause_letters=matched_cause_letters,
                        )
                        search_results.append(search_result)

                    logger.info(
                        f"Found {len(search_results)} results for query: {query}"
                    )
                    return search_results
                else:
                    # Fallback when no vector store available
                    logger.warning("No vector store available, returning empty results")
                    return []

            except Exception as e:
                logger.error(f"Search failed: {e}")
                raise

    async def update_document(
        self,
        document_id: str,
        title: Optional[str] = None,
        content: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> KnowledgeBaseDocument:
        """
        Update document using interface dependencies

        Args:
            document_id: Document identifier
            title: Optional new title
            content: Optional new content
            tags: Optional new tags

        Returns:
            Updated KnowledgeBaseDocument

        Raises:
            ValidationException: If document_id is invalid or no updates provided
        """
        with self._tracer.trace("knowledge_service_update_document"):
            logger.info(f"Updating document {document_id}")

            if not document_id or not document_id.strip():
                raise ValueError("Document ID cannot be empty")

            # Prepare update data
            update_data = {}
            metadata = {}

            if title:
                sanitized_title = await self._sanitizer.asanitize(title)
                update_data["title"] = sanitized_title
                metadata["title"] = sanitized_title

            if content:
                sanitized_content = await self._sanitizer.asanitize(content)
                update_data["content"] = sanitized_content

            if tags is not None:
                update_data["tags"] = tags
                metadata["tags"] = tags

            if not update_data:
                raise ValueError("At least one field must be provided for update")

            metadata["updated_at"] = to_json_compatible(datetime.now(timezone.utc))

            try:
                # Update via interface
                await self._ingester.update_document(
                    document_id=document_id,
                    content=update_data.get("content", ""),
                    metadata=metadata,
                )

                # Return updated document model with proper error handling
                try:
                    updated_document = KnowledgeBaseDocument(
                        document_id=document_id,
                        title=update_data.get("title", "Updated Document"),
                        content=update_data.get("content", ""),
                        document_type="updated",
                        tags=tags or [],
                        updated_at=datetime.now(timezone.utc),
                        created_at=datetime.now(
                            timezone.utc
                        ),  # Would normally fetch from storage
                    )
                except Exception as model_error:
                    raise RuntimeError(
                        f"Failed to create updated document model: {str(model_error)}"
                    ) from model_error

                # Re-index in vector store if content was updated
                if content and self._vector_store:
                    await self._index_document_in_vector_store(updated_document)

                logger.info(f"Successfully updated document {document_id}")
                return updated_document

            except ValidationException:
                # Re-raise validation exceptions without wrapping
                raise
            except RuntimeError:
                # Re-raise runtime exceptions without wrapping
                raise
            except Exception as e:
                logger.error(f"Failed to update document {document_id}: {e}")
                raise RuntimeError(f"Document update failed: {str(e)}") from e

    async def delete_document(self, document_id: str) -> Dict[str, Any]:
        """Remove a published runbook from the inventory (provenance-gated).

        Semantics depend on provenance (see ``is_builtin_item_id``):

        - **Built-in** (``kb_<12 hex>``) → **unpublish**: set
          ``is_published=False`` AND delete its ChromaDB vectors. A bare
          ``is_published=False`` is NOT sufficient — investigation retrieval
          does not honor the flag (``kb_qa`` filters ChromaDB by scope only),
          so the vectors must be removed or the runbook stays queryable.
          Deleting the vectors survives restart: the bootstrap content-hash
          skip won't re-vectorize an unchanged row. (A later content change to
          the on-disk file re-ingests it — an intentional new version.) The
          row is kept because the file would resurrect a hard delete anyway.
        - **Authored** (UUID / ``kb_<16 hex>``) → **hard delete**: drop the
          ``knowledge_items`` row and its vectors.

        Returns ``{success, document_id, action}`` where action is
        "unpublished" or "deleted".
        """
        with self._tracer.trace("knowledge_service_delete_document"):
            if not document_id or not document_id.strip():
                raise ValueError("Document ID cannot be empty")

            from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (  # noqa: E501
                DatabaseKnowledgeItemRepository,
            )
            from faultmaven.utils.runbook_id import is_builtin_item_id

            try:
                builtin = is_builtin_item_id(document_id)

                async with self._db_session_factory() as session:
                    repo = DatabaseKnowledgeItemRepository(session)
                    item = await repo.get_by_id(document_id)
                    if item is None:
                        return {
                            "success": False,
                            "error": f"Document {document_id} not found",
                        }

                    if builtin:
                        # Unpublish: keep the row, flip the flag (commits in repo).
                        item.is_published = False
                        await repo.update(item)
                        action = "unpublished"
                    else:
                        deleted = await repo.delete(document_id)
                        if not deleted:
                            # Zero rows matched — the RLS delete policy refused
                            # (e.g. a tenant session targeting a platform-tier
                            # global row under multi, #770) or a concurrent
                            # delete won. NEVER touch the shared vector store
                            # when the SQL row was not actually removed: the
                            # ChromaDB collection is shared across tenants, so
                            # an unguarded vector delete here would let any
                            # org admin destroy platform content for everyone.
                            return {
                                "success": False,
                                "error": f"Document {document_id} not deleted",
                            }
                        action = "deleted"

                # Cascade the item's team share rows on hard delete (source of
                # truth, ADR-013 §D4). Orphan shares are already fail-safe (they
                # match nothing in the allowlist), but clean them to keep the
                # table tidy. Unpublish keeps the row, so keeps its shares.
                if action == "deleted" and self._share_repo:
                    await self._share_repo.delete_for_resource(
                        "knowledge_item", document_id
                    )

                # Remove ChromaDB vectors in BOTH paths (best-effort, after DB
                # commit). Critical for unpublish: retrieval ignores
                # is_published, so deleting the vectors is what actually
                # removes the runbook from investigations.
                if self._vector_store:
                    await self._remove_from_vector_store(document_id)

                logger.info(f"{action} document {document_id}")
                return {
                    "success": True,
                    "document_id": document_id,
                    "action": action,
                }

            except ValidationException:
                raise
            except Exception as e:
                logger.error(f"Failed to remove document {document_id}: {e}")
                raise RuntimeError(f"Document removal failed: {str(e)}") from e

    async def get_document_statistics(self) -> Dict[str, Any]:
        """Get knowledge base statistics from SQLite."""
        with self._tracer.trace("knowledge_service_get_statistics"):
            try:
                documents_by_type: Dict[str, int] = {}
                tag_counts: Dict[str, int] = {}
                total_documents = 0

                from sqlalchemy import func as sa_func
                from sqlalchemy.future import select

                from faultmaven.infrastructure.persistence.models import (
                    ConversionDraftModel,
                )

                async with self._db_session_factory() as session:
                    # Count by document_type
                    result = await session.execute(
                        select(
                            ConversionDraftModel.document_type,
                            sa_func.count(),
                        )
                        .where(ConversionDraftModel.status == "verified")
                        .group_by(ConversionDraftModel.document_type)
                    )
                    for dtype, count in result.all():
                        documents_by_type[dtype or "runbook"] = count
                        total_documents += count

                    # Tags from verified drafts
                    tag_result = await session.execute(
                        select(ConversionDraftModel.tags).where(
                            ConversionDraftModel.status == "verified",
                            ConversionDraftModel.tags.isnot(None),
                        )
                    )
                    for (raw_tags,) in tag_result.all():
                        if isinstance(raw_tags, str):
                            for t in raw_tags.split(","):
                                t = t.strip()
                                if t:
                                    tag_counts[t] = tag_counts.get(t, 0) + 1

                most_used_tags = sorted(
                    tag_counts.keys(), key=lambda t: tag_counts[t], reverse=True
                )[:10]

                return {
                    "total_documents": total_documents,
                    "documents_by_type": documents_by_type,
                    "most_used_tags": most_used_tags,
                    "last_updated": to_json_compatible(datetime.now(timezone.utc)),
                    "vector_store_enabled": self._vector_store is not None,
                }
            except Exception as e:
                logger.error(f"Failed to get statistics: {e}")
                raise

    def _generate_document_id(self, title: str, document_type: str) -> str:
        """
        Generate unique document ID based on title and type

        Args:
            title: Document title
            document_type: Type of document

        Returns:
            Unique document identifier
        """
        content = (
            f"{title}:{document_type}:{to_json_compatible(datetime.now(timezone.utc))}"
        )
        hash_object = hashlib.sha256(content.encode("utf-8"))
        return f"kb_{hash_object.hexdigest()[:16]}"

    def _validate_document_data(self, title: str, content: str) -> None:
        """
        Validate document data before processing

        Args:
            title: Document title to validate
            content: Document content to validate

        Raises:
            ValueError: If validation fails
        """
        if not isinstance(title, str) or not isinstance(content, str):
            raise ValueError("Title and content must be strings")

        if len(title.strip()) == 0:
            raise ValueError("Title cannot be empty")

        if len(content.strip()) == 0:
            raise ValueError("Content cannot be empty")

        # Additional validation rules can be added here
        if len(title) > 500:
            raise ValueError("Title cannot exceed 500 characters")

    @staticmethod
    def _extract_frontmatter_for_rag(content: str) -> Dict[str, str]:
        """Extract RAG-relevant metadata from YAML frontmatter."""
        from faultmaven.utils.frontmatter import extract_frontmatter_metadata

        return extract_frontmatter_metadata(content)

    @staticmethod
    def _build_index_model(item) -> KnowledgeBaseDocument:
        """Snapshot a ``knowledge_items`` row as the document to index.

        Reads every field eagerly into a new object, so the result keeps
        describing the row as it was at this instant even after the row is
        mutated in place. ``update_document_metadata`` depends on that: it
        takes one of these before applying the update and one after, and the
        first is the only in-process record of what the current vectors mean
        once the snapshot has been mutated.
        """
        return KnowledgeBaseDocument(
            document_id=item.item_id,
            title=item.title,
            content=item.content,
            document_type=item.item_type.value,
            tags=list(item.tags) if item.tags else [],
            source_url=item.source_url,
            scope=item.scope.value,
            owner_id=item.owner_id,
            created_at=item.created_at.isoformat() if item.created_at else "",
            updated_at=item.updated_at.isoformat() if item.updated_at else "",
        )

    async def _index_document_in_vector_store(
        self,
        document: KnowledgeBaseDocument,
        prechunked: Optional[List[tuple[str, List[float]]]] = None,
        causes: Optional[List[Dict[str, Any]]] = None,
    ) -> int:
        """Index a document's chunks + embeddings into the vector store.

        Args:
            prechunked: Build-time ``(chunk_text, embedding)`` pairs from a KB
                pack. When supplied, the document is NOT re-chunked or
                re-embedded — these exact chunk texts and vectors are written.
                This is the fast, model-free boot path for shipped runbooks
                (embedding 1244 chunks on a CPU-limited pod otherwise takes
                ~tens of minutes). When None, the document is chunked with
                :class:`ContentChunker` and embedded with BGE-M3 — the
                upload/draft path. Per-chunk metadata is derived from the
                document frontmatter either way (it is not carried in the pack).
            causes: The ``metadata["causes"]`` record being written alongside
                these chunks, when there is one. Not indexed — checked. This is
                the one moment both sides of the KB cause seeder's join exist
                together, so it is where a record whose letters no chunk can
                recover gets caught (fm#1103). Observed, never enforced: see
                :meth:`_report_unseedable_causes`.
        Returns:
            Number of chunks indexed. Never 0 for a failure — see Raises.

        Raises:
            KnowledgeBaseError: If the embedding model is unavailable or times
                out, or the content yields no chunks. This used to ``return 0``, and half
                the callers checked that sentinel while half ignored it — so
                on the live update path the old vectors were already deleted,
                nothing replaced them, and the API answered 200. The document
                became permanently unsearchable while its SQL row looked
                healthy to every later consistency check (#945). A raise makes
                every caller fail closed by default; tolerating it is now
                opt-IN and visible at the call site.
        """
        if not self._vector_store:
            return 0

        try:
            # Build the replacement FIRST. The delete below is destructive and
            # irreversible, so nothing may be removed until its replacement
            # exists — the comment above this method used to call it an
            # "atomic swap" while deleting before it had embeddings, which is
            # what made a failed re-index destroy the document (#945).
            if prechunked is not None:
                # Pack fast path: write the pack's chunk texts + vectors as-is.
                # No ContentChunker, no model load.
                chunks = [text for text, _ in prechunked]
                embeddings = [embedding for _, embedding in prechunked]
                if not chunks:
                    raise KnowledgeBaseError(
                        f"Pack supplied 0 chunks for {document.document_id}",
                        error_code="KNOWLEDGE_NO_CHUNKS",
                    )
            else:
                from faultmaven.infrastructure.embedding_guard import (
                    embed_texts_or_raise,
                )
                from faultmaven.modules.knowledge.domain.services.content_chunker import (  # noqa: E501
                    ContentChunker,
                )

                chunks = ContentChunker().split(document.content)
                if not chunks:
                    raise KnowledgeBaseError(
                        f"No chunks produced for document {document.document_id}",
                        error_code="KNOWLEDGE_NO_CHUNKS",
                    )
                # Batch-embed all chunks off the event loop, through the one
                # module that owns "the embedder would not load OR would not
                # return". This used to hand-roll the None check with no time
                # bound at all, so an unavailable model raised but a hung one
                # held the request open indefinitely (#953).
                embeddings = await embed_texts_or_raise(
                    chunks,
                    subject=f"Indexing document {document.document_id}",
                    operation="index_document",
                )

            # Both sides of the seeder's join are in hand for the only time:
            # these exact chunk texts, and the causes record about to be stored
            # against them. Check it here (fm#1103) — after this the two are
            # only ever re-united by a retrieval, in a case that has already
            # lost the seeds.
            #
            # Ahead of the write, so an indexing failure (or the SQL rollback
            # ingest_runbook does on one) can leave an alarm naming a document
            # that never landed. Deliberate: the disagreement is a property of
            # the DATA, independent of whether this attempt stored it, so the
            # alarm is true either way and a retry at worst counts it twice.
            # Checking after the write instead would trade that for missing the
            # report entirely whenever indexing fails.
            #
            # The letters are parsed ONCE here and then both stamped onto every
            # chunk and handed to the check — the stamp is what retrieval will
            # read, so the thing checked is literally the thing stored (fm#1108).
            letters_per_chunk = [_matched_cause_letters(chunk) for chunk in chunks]
            self._report_unseedable_causes(
                document.document_id,
                letters_per_chunk,
                causes,
                chunker=("pack" if prechunked is not None else "runtime"),
            )

            # Extract RAG-enrichment fields from frontmatter
            fm_meta = self._extract_frontmatter_for_rag(document.content)

            # Metadata scope carries only the immutable floor: 'global' vs
            # 'personal'. Team visibility lives in the share table and is
            # resolved to an id allowlist at query time — never written here
            # (a 'team'/'global' tag would orphan-on-unshare or leak). ADR-013 §D4.
            _raw_scope = getattr(document, "scope", "global") or "global"
            _meta_scope = "global" if _raw_scope == "global" else "personal"

            # Build per-chunk document dicts
            doc_dicts = []
            for i, chunk in enumerate(chunks):
                meta = VectorMetadata(
                    title=document.title,
                    document_type=document.document_type,
                    tags=document.tags or [],
                    source_url=document.source_url,
                    scope=_meta_scope,
                    owner_id=getattr(document, "owner_id", None),
                    created_at=document.created_at,
                    updated_at=document.updated_at,
                    domain=fm_meta.get("domain"),
                    service=fm_meta.get("service"),
                    last_updated=fm_meta.get("last_updated"),
                    status=fm_meta.get("status"),
                    severity=fm_meta.get("severity"),
                    symptom_class=fm_meta.get("symptom_class"),
                    chunk_index=i,
                    total_chunks=len(chunks),
                    parent_document_id=document.document_id,
                    # Stamped on EVERY chunk, including the empty stamp for a
                    # chunk with no cause heading and for non-runbook content.
                    # Uniformity is what makes key-absence mean exactly one
                    # thing at read time — "indexed before fm#1108" — so the
                    # legacy-parse fallback can be counted and eventually
                    # retired. Stamping only cause-bearing chunks would make the
                    # fallback fire forever on ordinary prose and tell us
                    # nothing.
                    cause_letters=",".join(letters_per_chunk[i]),
                )
                doc_dicts.append(
                    {
                        "id": f"{document.document_id}_chunk_{i}",
                        "content": chunk,
                        "metadata": meta.to_chroma_metadata(),
                    }
                )

            # Validate the replacement BEFORE the destructive delete below.
            # add_documents re-runs this same refusal, but there it fires
            # after the old chunks are gone — a refusal at that point leaves
            # the document with ZERO vectors, where the pre-#1035 behaviour
            # at least kept it searchable. Unreachable for dicts built by
            # to_chroma_metadata() (it emits only declared keys), so this is
            # ordering insurance, not a second authority (fm#1035 review).
            for d in doc_dicts:
                VectorMetadata.reject_undeclared_keys(d["metadata"])

            # Replacement is fully in hand (embeddings + validated chunk
            # dicts) — only now remove the old chunks. The vector store
            # implements delete_documents_by_parent_id (IVectorStore
            # contract) — calling it directly, with no hasattr guard, so a
            # store that silently lacks it raises here instead of leaving
            # stale vectors behind on every re-ingest (the KB drift this
            # campaign fixed).
            await self._vector_store.delete_documents_by_parent_id(document.document_id)

            await self._vector_store.add_documents(doc_dicts, embeddings=embeddings)

            logger.info(
                "Indexed document into vector store",
                extra={
                    "document_id": document.document_id,
                    "title": document.title,
                    "chunk_count": len(chunks),
                },
            )
            return len(chunks)

        except KnowledgeBaseError:
            # Must survive the blanket handler below, which would otherwise
            # catch it and restore the exact 0-sentinel this change removes —
            # a typed raise a handler swallows one frame later is not a fix.
            raise
        except Exception as e:
            logger.error(f"Failed to index document in vector store: {e}")
            raise KnowledgeBaseError(
                f"Vector indexing failed for {document.document_id}: {e}",
                error_code="KNOWLEDGE_INDEXING_FAILED",
            ) from e

    @staticmethod
    def _report_unseedable_causes(
        document_id: str,
        letters_per_chunk: List[List[str]],
        causes: Optional[List[Dict[str, Any]]],
        *,
        chunker: str,
    ) -> None:
        """Warn + count when a stored causes record outruns its own chunks (fm#1103).

        The KB cause seeder joins a retrieval hit to a cause by parsing
        ``### Cause X:`` out of the matched chunk's text and matching it against
        ``cause_letter``. A letter no chunk carries therefore names a cause that
        can never be seeded — and nothing says so: the runbook is well-formed
        from either side alone, the case just quietly gets fewer candidates.

        Observed, not enforced. Refusing the write would convert a recall loss
        into a failed ingest, which on the pack path is a failed KB bootstrap —
        a produce-side data bug must not take the deployment down. So the
        document is indexed as asked and the drift is made loud instead: a
        WARNING naming the document and the missing letters (the unit a producer
        acts on) plus ``kb_cause_unseedable_at_ingest_total`` labeled by which
        chunker produced the disagreement.

        Swallows its own errors — a diagnostic on the ingest path must not be
        able to fail the write it is observing — but reports the swallow LOUDLY,
        which is the part that is easy to get wrong. A check that dies quietly
        leaves ``kb_cause_unseedable_at_ingest_total`` reading zero, and zero is
        this alarm's healthy state: a broken guard would be indistinguishable
        from a clean corpus, which is the exact silent-failure shape the guard
        exists to close. So the swallow costs a WARNING with the traceback and
        an increment of ``kb_cause_ingest_check_failed_total``.
        """
        try:
            missing = _unrecoverable_cause_letters(letters_per_chunk, causes)
            unrecorded = _unrecorded_chunk_letters(letters_per_chunk, causes)
            if not missing and not unrecorded:
                return
            from faultmaven.core.investigation.lifecycle_metrics import (
                kb_cause_unseedable_at_ingest_total,
            )

            kb_cause_unseedable_at_ingest_total.labels(chunker=chunker).inc()
            # Say which SIDE is wrong when the letter itself proves it. A letter
            # the heading grammar cannot express (``cause_letter: "a"``) is
            # unseedable — the seeder's join is case-sensitive, so reporting it
            # is correct — but the Causes section is not what needs fixing, and
            # advice pointing there would send a producer looking at healthy
            # markdown. Normalising case instead would be worse than imprecise:
            # it would call a cause recoverable that retrieval cannot join.
            ungrammatical = [
                letter for letter in missing if not _letter_can_head_a_cause(letter)
            ]
            absent = [letter for letter in missing if letter not in ungrammatical]
            if absent:
                logger.warning(
                    "UNSEEDABLE RUNBOOK CAUSES %s: the causes record declares "
                    "letter(s) %s that no chunk of this document carries a "
                    "'### Cause X:' heading for (%d chunks, %s chunker). Those "
                    "causes can never be seeded into an investigation — "
                    "retrieval has no way to name them. Fix the runbook's "
                    "Causes section (or the producer that emitted the record) "
                    "and re-ingest.",
                    document_id,
                    ", ".join(absent),
                    len(letters_per_chunk),
                    chunker,
                )
            if unrecorded:
                logger.warning(
                    "UNSEEDABLE RUNBOOK CAUSES %s: chunk(s) carry a "
                    "'### Cause X:' heading for letter(s) %s that the causes "
                    "record does not declare (%s chunker), so a hit on them "
                    "names nothing and seeds nothing. Usually a cause heading "
                    "outside the '## Causes' section the extractor reads. This "
                    "is what kb_cause_seed_letter_mismatch_total reports from "
                    "the far end, after a case has already lost the seeds.",
                    document_id,
                    ", ".join(unrecorded),
                    chunker,
                )
            if ungrammatical:
                logger.warning(
                    "UNSEEDABLE RUNBOOK CAUSES %s: the causes record declares "
                    "letter(s) %s that no '### Cause X:' heading can express, "
                    "so no chunk could ever carry them (%s chunker). The "
                    "markdown is not what is wrong here — the RECORD is "
                    "malformed, and whichever producer emitted it is what to "
                    "fix.",
                    document_id,
                    ", ".join(ungrammatical),
                    chunker,
                )
        except Exception as check_error:
            # Loud, not debug. Production runs at INFO, so a DEBUG line here
            # would let a broken check disappear while its counter kept
            # reporting a clean corpus — see the docstring.
            logger.warning(
                "UNSEEDABLE-CAUSES CHECK FAILED for %s (%s chunker): %s. That "
                "document went unchecked, and kb_cause_unseedable_at_ingest_"
                "total cannot be read as a clean bill of health until this "
                "stops.",
                document_id,
                chunker,
                check_error,
                exc_info=True,
            )
            try:
                from faultmaven.core.investigation.lifecycle_metrics import (
                    kb_cause_ingest_check_failed_total,
                )

                kb_cause_ingest_check_failed_total.inc()
            except Exception:
                # The deferred import is among the likelier things to have
                # failed above; the WARNING already carries the report, and
                # nothing here may raise into the write.
                pass

    async def _discard_vectors_for_vanished_row(self, document_id: str) -> None:
        """Drop vectors written for a row that was deleted mid-update (#952).

        Separate from :meth:`_remove_from_vector_store`, which swallows its
        errors because its callers are deleting a document and a failed vector
        delete is one more orphan among many the reconcile pass can find. Here
        the failure is not equivalent: these chunks carry content that was
        never saved, under an id that now 404s, and orphan pruning only covers
        built-in pack ids — so for an authored document nothing will ever
        remove them. That earns a loud, specific log rather than a shrug.

        Still does not raise: the caller is already abandoning the update and
        reporting that: a secondary failure must not replace the primary one.
        """
        try:
            await self._vector_store.delete_documents_by_parent_id(document_id)
        except Exception as discard_error:
            logger.error(
                "ORPHANED KNOWLEDGE VECTORS %s: the document was deleted "
                "during an update, and removing the chunks written for that "
                "update failed (%s). They describe content that was never "
                "saved, under an id that no longer resolves, and orphan "
                "pruning does not cover authored documents. Remove them by "
                "hand or re-create and re-delete the document.",
                document_id,
                discard_error,
            )

    async def _resynchronise_after_failed_commit(
        self,
        document_id: str,
        *,
        attempted_content: str,
        commit_error: Exception,
    ) -> None:
        """Realign the vectors after ``repo.update`` failed, by OBSERVING the row.

        The vectors already describe the attempted update. The obvious response
        — put the previous content back — is right only if the write actually
        failed, and a raised commit does not prove that: a connection dropped
        after the server committed raises here with the new content durably
        stored. Restoring on that signal alone would re-index superseded text
        under an updated row, manufacturing the exact undetectable mispairing
        this whole change exists to prevent.

        So the row is re-read first and the outcome decides:

        * row gone → drop the vectors (nothing left to describe)
        * row holds the attempted content → the commit landed; leave the
          vectors alone, they already match
        * row is unpublished → drop the vectors, because retrieval does not
          honour that flag and a retired runbook must not stay queryable
        * row holds anything else → re-index it AS OBSERVED

        That last case rebuilds from the row just read, not from a pre-embed
        snapshot. They are usually the same, and when they differ the snapshot
        is the wrong one: another writer committed during the embed, so
        restoring the snapshot would write vectors for a state the row does not
        hold — manufacturing the mispairing instead of repairing it.

        If the re-read itself fails, nothing is written and the state is
        reported as UNKNOWN rather than guessed at — an unverified corrective
        write is how the mispairing gets created rather than removed.
        """
        try:
            async with self._db_session_factory() as session:
                from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (  # noqa: E501
                    DatabaseKnowledgeItemRepository,
                )

                observed = await DatabaseKnowledgeItemRepository(session).get_by_id(
                    document_id
                )
        except Exception as reread_error:
            logger.error(
                "UNRESOLVED KNOWLEDGE DOCUMENT %s: committing an update failed "
                "(%s) and the row could not be re-read to decide what its "
                "vectors should describe (%s). The vectors currently hold the "
                "attempted update; whether the row does is unknown. Re-save "
                "the document to resynchronise.",
                document_id,
                commit_error,
                reread_error,
            )
            return

        if observed is not None and observed.content == attempted_content:
            # The commit landed despite raising. The vectors already match it;
            # touching them here is what would break the pairing.
            logger.warning(
                "Update of document %s reported a failure (%s) but the row "
                "holds the new content — the commit landed. Leaving the "
                "vectors, which already match it, in place.",
                document_id,
                commit_error,
            )
            return

        try:
            if observed is None or not observed.is_published:
                await self._discard_vectors_for_vanished_row(document_id)
            else:
                await self._index_document_in_vector_store(
                    self._build_index_model(observed),
                    causes=_row_causes(observed.metadata),
                )
        except Exception as restore_error:
            logger.error(
                "MISPAIRED KNOWLEDGE DOCUMENT %s: the update did not commit "
                "(%s), so the row still holds its previous content, and "
                "realigning the vectors to it failed too (%s). They now "
                "describe the update that was never saved, or part of it, or "
                "nothing — and a row with vectors present looks healthy to "
                "every consistency check there is, so nothing else will "
                "report this. Re-save the document to resynchronise.",
                document_id,
                commit_error,
                restore_error,
            )

    async def _remove_from_vector_store(self, document_id: str) -> None:
        """Remove all chunks for a document from the vector store.

        Indexing always chunks (see ``_index_in_vector_store``), so deletion
        keys off ``parent_document_id``.
        """
        if not self._vector_store:
            return

        try:
            count = await self._vector_store.delete_documents_by_parent_id(document_id)
            logger.info(f"Removed {count} chunks for document {document_id}")
        except Exception as e:
            logger.error(f"Failed to remove document from vector store: {e}")

    async def _create_team_share(
        self,
        *,
        resource_type: str,
        resource_id: str,
        team_id: str,
        organization_id: str,
        created_by: Optional[str] = None,
    ) -> None:
        """Record a team share for a resource (idempotent), the source of truth
        for team visibility (ADR-013 §D4).

        No-ops when no share repository is wired (e.g. minimal/test wiring) —
        team publishing is then inert. Cross-org sharing is structurally
        impossible: the share carries the resource's own ``organization_id`` and
        the read allowlist only ever resolves teams the requester belongs to
        (within their RLS-isolated org), so a share to a foreign team is
        unreachable. RLS is the backstop.
        """
        if not self._share_repo:
            logger.debug(
                "Team share skipped for %s %s (no share repository wired)",
                resource_type,
                resource_id,
            )
            return
        await self._share_repo.share(
            resource_type=resource_type,
            resource_id=resource_id,
            scope_type="team",
            scope_id=team_id,
            organization_id=organization_id,
            created_by=created_by,
        )

    async def ingest_runbook(
        self,
        document_id: str,
        title: str,
        content: str,
        organization_id: str,
        document_type: str = "runbook",
        tags: Optional[List[str]] = None,
        source_url: Optional[str] = None,
        scope: str = "global",
        owner_id: Optional[str] = None,
        team_id: Optional[str] = None,
        verified_by: Optional[str] = None,
        verification_level: "Optional[VerificationLevel]" = None,
        prechunked: Optional[List[tuple[str, List[float]]]] = None,
        causes: Optional[List[Dict[str, Any]]] = None,
    ) -> int:
        """Promote runbook content to a fully-published KnowledgeItem.

        Atomically maintains both stores by writing the relational source-of-
        truth first, then the ChromaDB embeddings. On ChromaDB failure (raise
        OR 0 chunks): delete the just-written SQL row before re-raising, so
        the two stores never diverge. The earlier "leave the SQL row for a
        scan-and-recover re-embed" policy produced half-state rows that
        downstream scans mis-classified, so rollback is now the contract.

        Args:
            document_id: Stable id used for both the relational row and the
                ChromaDB document.
            organization_id: Owning org for the org-owned tiers (personal/team).
                Ignored for global scope — global rows are the org-free
                platform tier (#770) and are stored with organization_id NULL.
            verified_by: A REAL user_id (from verify_draft) or None. Never a
                sentinel string — it is an FK to users.user_id. When None and
                no explicit verification_level is given, the item is
                EXPERIMENTAL. Trust for non-user-verified content (e.g.
                platform-shipped runbooks) goes through verification_level,
                not a fake verified_by.
            verification_level: Explicit trust level. When provided it wins
                over the verified_by-derived default — lets platform runbooks
                ship as COMMUNITY without a verified_by FK value. When None,
                falls back to the legacy derive so upload callers are
                unchanged.
            prechunked: Optional build-time ``(chunk_text, embedding)`` pairs
                from a KB pack. When provided, the runbook is written without
                chunking or loading BGE-M3 — the fast boot path for shipped
                runbooks. See :meth:`_index_document_in_vector_store`.

        Returns:
            Number of chunks indexed in ChromaDB (always > 0 on success). On
            ChromaDB failure the method raises after deleting the SQL row, so
            a successful return always means both stores are populated.
        """
        # Lazy imports keep this method self-contained against the knowledge
        # vertical (avoids top-level cycles between service and persistence).
        from faultmaven.modules.knowledge.domain.models.knowledge_item import (
            KnowledgeItem,
            KnowledgeItemType,
            KnowledgeScope,
            VerificationLevel,
        )
        from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (
            DatabaseKnowledgeItemRepository,
        )

        now = datetime.now(timezone.utc)

        # Normalize tags to strings up front so BOTH the relational
        # KnowledgeItem and the Pydantic KnowledgeBaseDocument below receive
        # str tags. YAML frontmatter can yield numeric tags (e.g. a bare
        # ``503`` in ``tags: [istio, 503, envoy]``). KnowledgeItem coerces in
        # __post_init__, but KnowledgeBaseDocument is a Pydantic model with a
        # ``List[str]`` field and no such hook — it rejected the int and
        # failed the whole runbook ingest. Coercing once here covers both.
        if tags:
            tags = [str(tag) for tag in tags]

        # 1) SQL first — relational source-of-truth. If this fails, ChromaDB
        # is never touched. If ChromaDB later fails (step 2), the SQL row
        # is rolled back before we raise (see lines below) — atomic across
        # both stores.
        item = KnowledgeItem(
            item_id=document_id,
            # Global scope is the org-free platform tier (#770): the row carries
            # NO organization_id (knowledge_items_global_org_check). Org-owned
            # tiers (personal/team) keep the caller's org.
            organization_id=(
                None
                if KnowledgeScope(scope) == KnowledgeScope.GLOBAL
                else organization_id
            ),
            title=title,
            content=content,
            item_type=KnowledgeItemType.RUNBOOK,
            scope=KnowledgeScope(scope),
            owner_id=owner_id,
            tags=list(tags) if tags else [],
            source_url=source_url,
            # Explicit verification_level wins; otherwise derive from
            # verified_by (COMMUNITY when a real user verified, else
            # EXPERIMENTAL). The explicit override exists so platform-
            # shipped runbooks can carry COMMUNITY trust WITHOUT a fake
            # verified_by FK value — verified_by is a real user_id or
            # NULL, never a sentinel.
            verification_level=(
                verification_level
                if verification_level is not None
                else (
                    VerificationLevel.COMMUNITY
                    if verified_by
                    else VerificationLevel.EXPERIMENTAL
                )
            ),
            verified_by=verified_by,
            verified_at=now if verified_by else None,
            created_at=now,
            updated_at=now,
            # v4 per-Cause graph records, stored verbatim (absent/None on the
            # upload path and pre-v4 runbooks). Runtime reader: the KB cause
            # seeder (get_runbook_causes → core.investigation.kb_cause_seeder)
            # instantiates these chains as CANDIDATE graph nodes when
            # FAULTMAVEN_KB_CAUSE_SEEDER is on. The shape is also the cross-repo
            # pack contract (test_runbook_causes_contract). Co-located in the row
            # so the orphan-prune removes them with it, and re-ingested on a
            # causes drift even when the markdown is byte-identical (kb_init
            # compares the persisted causes, not just the content hash).
            # ``causes`` verbatim, plus the identity of the chunk stamp
            # written alongside it (fm#1108). The stamp identity is recorded
            # even for a runbook with no causes record, because it describes the
            # CHUNKS, not the record — and the bootstrap's idempotency gate
            # compares it to decide whether stored stamps still mean what they
            # say.
            metadata={
                **({"causes": causes} if causes else {}),
                "chunk_stamp": chunk_stamp_identity(),
            },
        )
        async with self._db_session_factory() as session:
            repo = DatabaseKnowledgeItemRepository(session)
            await repo.create(item)

        # Team publish: record visibility in the share table (source of truth,
        # ADR-013 §D4). The scope enum stays 'team' ⟺ ≥1 share row. Cross-org
        # shares are impossible by construction — the share carries the item's
        # own organization_id (a team belongs to exactly one org).
        if scope == KnowledgeScope.TEAM.value and team_id:
            await self._create_team_share(
                resource_type="knowledge_item",
                resource_id=document_id,
                team_id=team_id,
                organization_id=organization_id,
                created_by=owner_id or verified_by,
            )

        # 2) ChromaDB second — chunks + embeddings. On failure (raises OR
        # returns 0 chunks), delete the SQL row before raising. The prior
        # "leave SQL for recovery" policy produced half-state rows that
        # downstream scans then mis-classified.
        doc_model = KnowledgeBaseDocument(
            document_id=document_id,
            title=title,
            content=content,
            document_type=document_type,
            tags=tags or [],
            source_url=source_url,
            scope=scope,
            owner_id=owner_id,
            created_at=to_json_compatible(now),
            updated_at=to_json_compatible(now),
        )
        try:
            chunks_created = await self._index_document_in_vector_store(
                doc_model, prechunked=prechunked, causes=causes
            )
        except Exception:
            await self._delete_knowledge_item_row(document_id)
            raise

        if chunks_created <= 0:
            await self._delete_knowledge_item_row(document_id)
            raise RuntimeError(
                f"Vector indexing produced 0 chunks for {document_id}. "
                f"Common causes: BGE-M3 model unavailable, ChromaDB unreachable, "
                f"or chunker produced no output. SQL row cleaned up."
            )

        return chunks_created

    async def reindex_missing_vectors(self, item_id: str) -> int:
        """Re-chunk + re-embed an existing row's chunks into the vector store.

        Cross-store repair for an ORPHANED row — a ``knowledge_items`` row whose
        ChromaDB chunks are missing (a crash between the SQL commit and the
        vector write in :meth:`ingest_runbook` leaves exactly this half-state;
        the KB bootstrap reconcile pass can detect it but cannot fix it without
        the embedding model). This re-derives the vectors from the row's
        persisted ``content`` using the runtime chunker + BGE-M3 and writes them
        under the row's ``item_id`` — the SQL row itself is NEVER touched (it is
        the source of truth and already correct). Per-chunk RAG metadata
        (scope/domain/service/…) is rebuilt by the shared indexing path, so a
        repaired chunk carries the same retrieval filters as a fresh ingest.

        Unlike the pack boot path this DOES load the embedding model (via the
        non-prechunked branch of :meth:`_index_document_in_vector_store` →
        ``model_cache.aembed_texts``, off the event loop). Callers MUST bound how
        many rows they repair per boot: re-embedding the whole KB reintroduces
        the on-pod CPU-embedding timeout the prechunked pack exists to avoid.

        Repaired chunks use the runtime chunker/embedder, so their text
        boundaries may differ from the pack's build-time chunks; the vectors are
        BGE-M3 (guarded by the pack model-identity check) so they remain
        query-compatible — a retrievable runbook beats a silently missing one.

        Returns the number of chunks indexed. Fail-safe 0 (no raise) when the
        row is absent, no vector store is wired, or the embedding model is
        unavailable — leaving the row orphaned for a later boot to repair.

        This is the ONE caller that legitimately tolerates an indexing failure,
        because it runs as a bounded best-effort repair pass during boot and a
        failed repair must not abort startup. It therefore catches
        ``KnowledgeBaseError`` **explicitly**: after #945 the tolerance is
        opt-in and visible here, rather than a 0-sentinel that every caller
        silently inherited and only some checked.

        Caveat (pre-existing, narrow): if ``add_documents`` writes SOME chunks
        then raises, the parent now has partial vectors — so the reconcile pass
        (which keys on parent PRESENCE, not chunk completeness) no longer flags
        it and repair won't re-attempt it. Acceptable: the caller only invokes
        this on genuinely vectorless rows, and the
        ``delete_documents_by_parent_id`` in the indexing path makes a re-run
        idempotent, so a partial write is at worst under-retrievable,
        never mispaired.
        """
        from sqlalchemy import select as _select

        from faultmaven.infrastructure.persistence.models import KnowledgeItemModel

        async with self._db_session_factory() as session:
            found = await session.execute(
                _select(KnowledgeItemModel).where(KnowledgeItemModel.item_id == item_id)
            )
            row = found.scalar_one_or_none()

        if row is None:
            logger.warning(
                f"reindex_missing_vectors: no knowledge_items row for {item_id} — "
                "nothing to repair"
            )
            return 0

        # Mirror the KnowledgeBaseDocument that ingest_runbook builds from the
        # same fields (knowledge_service.py ~968) so the repaired index is
        # metadata-identical to a normal ingest.
        doc_model = KnowledgeBaseDocument(
            document_id=row.item_id,
            title=row.title,
            content=row.content,
            # item_type is the DURABLE source for document_type: ingest_runbook
            # takes a separate document_type param but does not persist it, so the
            # stored item_type is the faithful reconstruction. For all pack
            # content item_type == "runbook" == the ingest default, so a repaired
            # runbook's chunk metadata matches a fresh ingest exactly.
            document_type=row.item_type,
            tags=list(row.tags) if row.tags else [],
            source_url=row.source_url,
            scope=row.scope,
            owner_id=row.owner_id,
            created_at=to_json_compatible(row.created_at),
            updated_at=to_json_compatible(row.updated_at),
        )
        # A repair re-chunks with the RUNTIME chunker while the row keeps the
        # causes record it was ingested with — for a pack runbook that pairs our
        # chunks with kb-toolkit's record, the one place the two producers meet.
        # Read straight off the row: get_runbook_causes applies the seeder's
        # verification-level filter, and an EXPERIMENTAL row's record is
        # unseedable for that reason rather than this one.
        row_causes = _row_causes(getattr(row, "knowledge_metadata", None))
        try:
            return await self._index_document_in_vector_store(
                doc_model, prechunked=None, causes=row_causes
            )
        except KnowledgeBaseError as e:
            # Deliberate, documented tolerance — see the docstring. A failed
            # boot-time repair leaves the row orphaned for a later attempt; it
            # must not abort startup.
            logger.warning(
                f"reindex_missing_vectors: repair failed for {item_id}: {e} — "
                "leaving the row orphaned for a later boot to retry"
            )
            return 0

    async def _delete_knowledge_item_row(self, item_id: str) -> None:
        """Best-effort cleanup of an orphaned knowledge_items row.

        Used by `ingest_runbook` when vector indexing fails — guarantees no
        half-state remains. Errors are logged but suppressed since the
        caller is already on a failure path and will raise its own error.
        """
        from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (
            DatabaseKnowledgeItemRepository,
        )

        try:
            async with self._db_session_factory() as session:
                repo = DatabaseKnowledgeItemRepository(session)
                await repo.delete(item_id)
        except Exception as cleanup_err:
            logger.warning(
                f"Failed to clean up orphaned knowledge_items row {item_id}: "
                f"{cleanup_err}"
            )

    # API-compatible methods that match the router expectations
    async def upload_document(
        self,
        content: str,
        title: str,
        document_type: str,
        tags: Optional[List[str]] = None,
        source_url: Optional[str] = None,
        category: Optional[str] = None,
        description: Optional[str] = None,
        scope: str = "global",
        owner_id: Optional[str] = None,
        team_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Upload document: create SQLite record + ingest into ChromaDB."""
        try:
            import uuid as _uuid

            from faultmaven.utils.frontmatter import extract_frontmatter_metadata
            from faultmaven.utils.runbook_id import authored_item_id

            # 16-hex authored id — must NOT match the 12-hex built-in pattern, or
            # the bootstrap orphan-prune would delete this user runbook on redeploy.
            document_id = authored_item_id()
            created_at = datetime.now(timezone.utc)

            # Extract metadata from frontmatter
            fm_meta = extract_frontmatter_metadata(content)
            # ConversionDraftModel.tags is a TagsArray TypeDecorator expecting
            # list[str]. Pass the list shape directly; the decorator handles
            # cross-dialect serialization.
            tags_list: Optional[List[str]] = list(tags) if tags else None

            # Both conversion_jobs / uploaded_files / knowledge_items require
            # organization_id NOT NULL — fall back to the single-tenant
            # default when no explicit org is in scope. Resolved before the
            # session block because ingest_runbook below needs it too.
            from faultmaven.providers.tenancy.single_tenant import (
                SingleTenantProvider,
            )

            org_id = SingleTenantProvider.DEFAULT_ORG_ID

            # Create SQLite record (synthetic draft, immediately verified)
            from faultmaven.infrastructure.persistence.models import (
                ConversionDraftModel,
                ConversionJobModel,
                UploadedFileModel,
            )

            conversion_id = f"conv_{_uuid.uuid4().hex[:12]}"
            draft_id = f"draft_{_uuid.uuid4().hex[:12]}"

            # Write content to disk
            from pathlib import Path

            # Flat by scope, matching the canonical layout the scan pass
            # infers scope from (ConversionService._scope_dir): global/,
            # team_{id}/, user_{id}/ — NO domain subdirectory (domain lives
            # in frontmatter + ChromaDB metadata). Writing a literal
            # "personal"/"team" folder would break scan scope-inference,
            # which keys off the user_/team_ prefixes.
            data_dir = Path("data/knowledge")
            if scope == "team" and team_id:
                target_dir = data_dir / f"team_{team_id}"
            elif scope == "personal" and owner_id:
                target_dir = data_dir / f"user_{owner_id}"
            else:
                target_dir = data_dir / "global"
            target_dir.mkdir(parents=True, exist_ok=True)

            filename = (
                f"{title.lower().replace(' ', '-')[:60]}-{_uuid.uuid4().hex[:4]}.md"
            )
            file_path = target_dir / filename
            file_path.write_text(content, encoding="utf-8")

            async with self._db_session_factory() as session:
                # ``conversion_jobs.source_file_id`` is a FK to
                # ``uploaded_files``. Create the upload row first.
                source_file_id = f"file_{_uuid.uuid4().hex[:12]}"
                upload = UploadedFileModel(
                    file_id=source_file_id,
                    organization_id=org_id,
                    case_id=None,  # KB-bound, not case-bound
                    uploaded_by=owner_id,
                    filename=filename,
                    size_bytes=len(content.encode()),
                    content_type="text/markdown",
                    storage_ref=str(file_path),
                    upload_source="conversion_source",
                    uploaded_at_turn=0,
                )
                session.add(upload)
                await session.flush()

                job = ConversionJobModel(
                    id=conversion_id,
                    user_id=owner_id,  # NULL if anonymous; FK SET NULL
                    organization_id=org_id,
                    scope=scope,
                    status="completed",
                    source_file_id=source_file_id,
                    source_type="document",
                    failure_modes_detected=0,
                    analysis_result={},
                    created_at=created_at,
                    completed_at=created_at,
                )
                session.add(job)

                draft = ConversionDraftModel(
                    id=draft_id,
                    organization_id=org_id,
                    conversion_id=conversion_id,
                    runbook_id=document_id,
                    title=title,
                    file_path=str(file_path),
                    status="verified",
                    source_type="document",
                    validation_passed=True,
                    knowledge_item_id=document_id,
                    domain=fm_meta.get("domain"),
                    service=fm_meta.get("service"),
                    severity=fm_meta.get("severity"),
                    tags=tags_list,
                    document_type=document_type,
                    created_at=created_at,
                    verified_at=created_at,
                    verified_by=owner_id,  # NULL if anonymous; FK SET NULL
                )
                session.add(draft)
                await session.commit()

            # Ingest both relationally and into ChromaDB.
            # upload_document is called by anonymous / non-verified upload
            # paths, so verified_by is None (verification_level defaults to
            # EXPERIMENTAL inside ingest_runbook).
            chunks_created = await self.ingest_runbook(
                document_id=document_id,
                title=title,
                content=content,
                organization_id=org_id,
                document_type=document_type,
                tags=tags,
                source_url=source_url,
                scope=scope,
                owner_id=owner_id,
                team_id=team_id,
                verified_by=None,
            )

            logger.info(
                f"Uploaded document {document_id}: {chunks_created} chunks indexed"
            )

            return {
                "document_id": document_id,
                "status": "completed",
                "metadata": {
                    "title": title,
                    "document_type": document_type,
                    "category": category or document_type,
                    "tags": tags or [],
                    "created_at": to_json_compatible(created_at),
                },
            }

        except Exception as e:
            logger.error(f"Failed to upload document: {e}")
            raise

    async def list_documents(
        self,
        document_type: Optional[str] = None,
        tags: Optional[List[str]] = None,
        scope: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        user: Optional[Any] = None,
        team_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """List published runbooks from knowledge_items with RBAC filtering.

        ``knowledge_items`` is the source of truth for the published runbook
        inventory — both bootstrap built-ins and verify_draft promotions land
        there. ``conversion_drafts`` is only the review queue (Drafts tab).
        RBAC (org + personal/team isolation) is enforced in-query by the
        repository; tag/scope filtering and pagination are applied here over
        the already tenant-isolated set.
        """
        try:
            from faultmaven.modules.knowledge.domain.models.knowledge_item import (
                KnowledgeItemType,
            )
            from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (  # noqa: E501
                DatabaseKnowledgeItemRepository,
            )
            from faultmaven.providers.tenancy.single_tenant import (
                SingleTenantProvider,
            )

            organization_id = (
                getattr(user, "organization_id", None)
                or SingleTenantProvider.DEFAULT_ORG_ID
            )
            user_id = getattr(user, "user_id", None) if user else None

            item_type = None
            if document_type:
                try:
                    item_type = KnowledgeItemType(document_type)
                except ValueError:
                    # Unknown type → no matching items (mirrors legacy empty
                    # result for an unrecognized document_type filter).
                    logger.info(
                        f"Unknown document_type filter '{document_type}' — "
                        "no matching items"
                    )
                    return {
                        "documents": [],
                        "total_count": 0,
                        "limit": limit,
                        "offset": offset,
                        "filters": {
                            "document_type": document_type,
                            "tags": tags,
                            "scope": scope,
                        },
                        "scope_counts": {"global": 0, "team": 0, "personal": 0},
                    }

            async with self._db_session_factory() as session:
                repo = DatabaseKnowledgeItemRepository(session)
                items = await repo.list_for_inventory(
                    organization_id=organization_id,
                    user_id=user_id,
                    team_ids=team_ids,
                    item_type=item_type,
                )

            # DTO build + tag filter over the RBAC-isolated set. Response shape
            # is kept identical to the legacy conversion_drafts path so the
            # dashboard needs no contract change; conversion-pipeline metadata
            # (domain/service/severity/quality_score) is null for built-ins,
            # which never went through that pipeline.
            all_documents: List[Dict[str, Any]] = []
            for item in items:
                tag_list = list(item.tags) if item.tags else []

                # Tag filter
                if tags and not any(t in tag_list for t in tags):
                    continue

                meta = item.metadata or {}
                all_documents.append(
                    {
                        "document_id": item.item_id,
                        "title": item.title,
                        "document_type": item.item_type.value,
                        "tags": tag_list,
                        "scope": item.scope.value,
                        "owner_id": item.owner_id,
                        "source_url": item.source_url,
                        "created_at": (
                            item.created_at.isoformat() if item.created_at else ""
                        ),
                        "updated_at": (
                            item.updated_at.isoformat() if item.updated_at else ""
                        ),
                        "metadata": {
                            "domain": meta.get("domain"),
                            "service": meta.get("service"),
                            "severity": meta.get("severity"),
                            "quality_score": meta.get("quality_score"),
                        },
                    }
                )

            # Scope counts (before scope filter)
            scope_counts = {"global": 0, "team": 0, "personal": 0}
            for doc in all_documents:
                s = doc.get("scope", "global")
                if s in scope_counts:
                    scope_counts[s] += 1

            # Apply explicit scope filter
            filtered_docs = (
                [d for d in all_documents if d.get("scope") == scope]
                if scope
                else all_documents
            )

            total = len(filtered_docs)
            paginated_docs = filtered_docs[offset : offset + limit]

            logger.info(f"Listed {len(paginated_docs)} documents (total: {total})")

            return {
                "documents": paginated_docs,
                "total_count": total,
                "limit": limit,
                "offset": offset,
                "filters": {
                    "document_type": document_type,
                    "tags": tags,
                    "scope": scope,
                },
                "scope_counts": scope_counts,
            }

        except Exception as e:
            logger.error(f"Failed to list documents: {e}")
            # Degraded-but-successful return: callers key on `error` being
            # present, so the shape is kept — but the value is a static
            # message, never the exception text (#866). A driver raises with
            # the connection URI in its message, and this body is reachable
            # with no credentials (the route takes optional auth). The log
            # line above is where the driver text belongs.
            return {
                "documents": [],
                "total_count": 0,
                "limit": limit,
                "offset": offset,
                "error": "Failed to list documents",
            }

    @staticmethod
    def _document_dto(item: Any) -> Dict[str, Any]:
        """Build the single-document DTO from a ``KnowledgeItem``.

        Mirrors the ``list_documents`` DTO shape with a ``content`` field
        added. One builder so the scoped and unscoped reads can never return
        different shapes for the same row.
        """
        meta = item.metadata or {}
        return {
            "document_id": item.item_id,
            "title": item.title,
            "content": item.content,
            "document_type": item.item_type.value,
            "tags": list(item.tags) if item.tags else [],
            "scope": item.scope.value,
            "owner_id": item.owner_id,
            "source_url": item.source_url,
            "created_at": item.created_at.isoformat() if item.created_at else "",
            "updated_at": item.updated_at.isoformat() if item.updated_at else "",
            "metadata": {
                "domain": meta.get("domain"),
                "service": meta.get("service"),
                "severity": meta.get("severity"),
                "quality_score": meta.get("quality_score"),
            },
        }

    async def get_document(self, document_id: str) -> Optional[Dict[str, Any]]:
        """Get a published runbook by ID from knowledge_items — UNSCOPED.

        Content comes from the stored row (``knowledge_items.content``), not
        from disk — the row is the source of truth for the published
        inventory.

        This is the trusted load: it applies no requester scope, so the write
        routes can evaluate the write policy against the real row (the
        single-tenant operator override has to work on documents the operator
        cannot list) and internal ingestion can read any row. Actor-facing
        reads must use :meth:`get_document_visible` instead.
        """
        try:
            if not document_id:
                return None

            from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (  # noqa: E501
                DatabaseKnowledgeItemRepository,
            )

            async with self._db_session_factory() as session:
                repo = DatabaseKnowledgeItemRepository(session)
                item = await repo.get_by_id(document_id)

            if item is None:
                return None

            return self._document_dto(item)

        except Exception as e:
            logger.error(f"Failed to get document {document_id}: {e}")
            return None

    async def get_document_visible(
        self,
        document_id: str,
        user: Optional[Any] = None,
        team_ids: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Get a runbook by ID, scoped to what the requester may see (#867).

        The actor-facing counterpart of :meth:`get_document`: RBAC is enforced
        in-query by ``get_visible_by_id`` (global ∪ own-org owned ∪ own-org
        shared-to-my-teams), and ``organization_id`` is sourced exactly as
        ``list_documents`` sources it. Returns None both for an absent id and
        for one the requester cannot see, so callers cannot distinguish the
        two.

        Rejected alternative: scoping ``get_document`` itself — it is the
        trusted load behind the write-policy check and internal ingestion, so
        scoping it would break the operator override and push visibility
        decisions into callers that have no actor.
        """
        try:
            if not document_id:
                return None

            from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (  # noqa: E501
                DatabaseKnowledgeItemRepository,
            )
            from faultmaven.providers.tenancy.single_tenant import (
                SingleTenantProvider,
            )

            organization_id = (
                getattr(user, "organization_id", None)
                or SingleTenantProvider.DEFAULT_ORG_ID
            )
            user_id = getattr(user, "user_id", None) if user else None

            async with self._db_session_factory() as session:
                repo = DatabaseKnowledgeItemRepository(session)
                item = await repo.get_visible_by_id(
                    document_id,
                    organization_id=organization_id,
                    user_id=user_id,
                    team_ids=team_ids,
                )

            if item is None:
                return None

            return self._document_dto(item)

        except Exception as e:
            logger.error(f"Failed to get visible document {document_id}: {e}")
            return None

    async def get_runbook_causes(self, item_id: str) -> Optional[List[Dict[str, Any]]]:
        """Return a runbook's structured causal-graph records, or None.

        Loads ``knowledge_items.metadata["causes"]`` for the given row — the
        machine-readable per-Cause chains the KB cause seeder instantiates as
        candidate graph nodes. Returns None when the id is unknown, the row has
        no causes record, or lookup fails (the seeder treats None as "prose-only
        source, nothing to seed").
        """
        try:
            if not item_id:
                return None

            from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (  # noqa: E501
                DatabaseKnowledgeItemRepository,
            )

            async with self._db_session_factory() as session:
                repo = DatabaseKnowledgeItemRepository(session)
                item = await repo.get_by_id(item_id)

            if item is None or not item.metadata:
                return None

            # Runtime trust invariant: EXPERIMENTAL (AI-generated / unreviewed /
            # anonymous-upload) knowledge must never seed candidate causes. The
            # call sites already extract causes only at the human-verification
            # gate — verify_draft ingests as COMMUNITY, and the anonymous
            # upload_document path never extracts — but enforcing it here makes
            # it a runtime invariant: the seeder can never consume an
            # unverified item's causes record no matter how it was written.
            # Pack runbooks ship COMMUNITY and verified drafts are COMMUNITY, so
            # this refuses only the EXPERIMENTAL tier.
            from faultmaven.modules.knowledge.domain.models.knowledge_item import (
                VerificationLevel,
            )

            if (
                getattr(item, "verification_level", None)
                == VerificationLevel.EXPERIMENTAL
            ):
                logger.debug(
                    f"Refusing to seed causes from EXPERIMENTAL item {item_id}"
                )
                return None

            causes = item.metadata.get("causes")
            return causes if isinstance(causes, list) else None

        except Exception as e:
            logger.error(f"Failed to load causes for {item_id}: {e}")
            return None

    async def get_runbook_title(self, item_id: str) -> Optional[str]:
        """Return a knowledge item's display title, or None.

        Used to name the runbook a resolved case was seeded from when the
        runbook-generation offer is short-circuited for provenance-based
        uniqueness (Phase 5.2b) — so the "already covered by X" message can name
        X. Returns None when the id is falsy, the row is unknown, or lookup fails
        (the caller degrades to a runbook-unnamed message; unlike the seeder
        loader this applies no verification-level filter — naming an item the
        user already applied is not a trust-boundary crossing).
        """
        try:
            if not item_id:
                return None

            from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (  # noqa: E501
                DatabaseKnowledgeItemRepository,
            )

            async with self._db_session_factory() as session:
                repo = DatabaseKnowledgeItemRepository(session)
                item = await repo.get_by_id(item_id)

            return getattr(item, "title", None) if item is not None else None
        except Exception as e:
            logger.error(f"Failed to load title for {item_id}: {e}")
            return None

    async def get_semantic_snippet(
        self,
        document_id: str,
        query: str,
        max_lines: int = 5,
    ) -> Optional[Dict[str, Any]]:
        """Get semantically relevant snippet from a document.

        Uses vector similarity to find the most relevant chunk of the document
        based on the user's query. This is more robust than line-based extraction
        when documents are edited.

        Args:
            document_id: Document identifier
            query: User's query to find relevant content
            max_lines: Maximum lines to return in the snippet

        Returns:
            Dict with snippet, line_start, line_end, relevance_score
            or None if document not found or semantic search unavailable
        """
        try:
            # Get the full document
            document = await self.get_document(document_id)
            if not document:
                return None

            content = document.get("content", "")
            if not content:
                return None

            lines = content.split("\n")
            total_lines = len(lines)

            # Try to use vector store for semantic search if available
            if self._vector_store:
                try:
                    # Search within this specific document
                    # Create chunks from the document content
                    chunk_size = max_lines
                    chunks = []
                    for i in range(0, total_lines, chunk_size):
                        chunk_lines = lines[i : i + chunk_size]
                        chunk_text = "\n".join(chunk_lines)
                        if chunk_text.strip():
                            chunks.append(
                                {
                                    "text": chunk_text,
                                    "line_start": i + 1,
                                    "line_end": min(i + chunk_size, total_lines),
                                }
                            )

                    if not chunks:
                        return None

                    # Use simple text similarity as fallback
                    # (In production, this would use embeddings)
                    best_chunk = None
                    best_score = 0.0
                    query_lower = query.lower()
                    query_words = set(query_lower.split())

                    for chunk in chunks:
                        chunk_lower = chunk["text"].lower()
                        chunk_words = set(chunk_lower.split())

                        # Calculate word overlap score
                        overlap = len(query_words & chunk_words)
                        if overlap > 0:
                            score = overlap / len(query_words)
                            # Bonus for exact phrase match
                            if query_lower in chunk_lower:
                                score += 0.5

                            if score > best_score:
                                best_score = score
                                best_chunk = chunk

                    if best_chunk:
                        return {
                            "snippet": best_chunk["text"],
                            "line_start": best_chunk["line_start"],
                            "line_end": best_chunk["line_end"],
                            "relevance_score": min(best_score, 1.0),
                        }
                except Exception as e:
                    logger.warning(f"Vector-based semantic search failed: {e}")

            # Fallback: simple text matching
            query_lower = query.lower()
            best_start = 0
            best_score = 0.0

            for i, line in enumerate(lines):
                if query_lower in line.lower():
                    # Found a match, calculate a simple score
                    score = 1.0
                    if best_score < score:
                        best_score = score
                        best_start = max(0, i - max_lines // 2)

            end_line = min(best_start + max_lines, total_lines)
            snippet = "\n".join(lines[best_start:end_line])

            return {
                "snippet": snippet,
                "line_start": best_start + 1,
                "line_end": end_line,
                "relevance_score": best_score if best_score > 0 else None,
            }

        except Exception as e:
            logger.error(f"Failed to get semantic snippet for {document_id}: {e}")
            return None

    async def search_documents(
        self,
        query: str,
        document_type: Optional[str] = None,
        category: Optional[str] = None,
        tags: Optional[List[str]] = None,
        limit: int = 10,
        similarity_threshold: Optional[float] = None,
        rank_by: Optional[str] = None,
        user: Optional[Any] = None,
        team_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Semantic search using vector embeddings with RBAC scope filtering.

        Builds a scope filter from the requesting user's accessible scopes
        (personal + team memberships + global) and issues a vector search
        against the KB collection. Falls back to fulltext_search_documents()
        when no vector store is available.
        """
        from faultmaven.api.v1.utils.parsing import normalize_tags_field

        if not self._vector_store:
            return await self.fulltext_search_documents(
                query=query,
                document_type=document_type,
                tags=tags,
                limit=limit,
                similarity_threshold=similarity_threshold,
                rank_by=rank_by,
                user=user,
            )

        try:
            user_id = getattr(user, "user_id", None) if user else None

            # Resolve the "shared-to-my-teams" arm from the share table (source
            # of truth), then build the visible-id allowlist filter
            # (global ∪ owned ∪ shared). ADR-013 §D4 / ADR-011 D3. The share
            # rows are matched against the caller's own org, so a row stamped
            # with a foreign tenant never widens this filter.
            shared_ids = await resolve_shared_kb_ids(
                self._share_repo,
                team_ids,
                getattr(user, "organization_id", None) if user else None,
            )
            scope_filter: Dict[str, Any] = build_kb_scope_filter(user_id, shared_ids)

            if document_type:
                scope_filter = {
                    "$and": [scope_filter, {"document_type": document_type}]
                }

            vector_results = await self._vector_store.search(
                collection_name=KB_COLLECTION,
                query=query,
                k=limit * 3,
                where=scope_filter,
            )

            if similarity_threshold is not None:
                vector_results = [
                    r
                    for r in vector_results
                    if r.get("score", 0.0) >= similarity_threshold
                ]

            limited = vector_results[:limit]

            logger.info(f"Semantic search '{query}' returned {len(limited)} results")

            return {
                "query": query,
                "total_results": len(limited),
                "results": [
                    {
                        "document_id": (
                            r.get("metadata", {}).get("parent_document_id")
                            or r.get("id", "").rsplit("_chunk_", 1)[0]
                        ),
                        "content": r.get("content", "")[:200] + "...",
                        "metadata": {
                            "title": r.get("metadata", {}).get("title", "Untitled"),
                            "document_type": r.get("metadata", {}).get(
                                "document_type", "runbook"
                            ),
                            "category": r.get("metadata", {}).get(
                                "document_type", "general"
                            ),
                            "tags": normalize_tags_field(
                                r.get("metadata", {}).get("tags", [])
                            ),
                            "priority": "normal",
                        },
                        "similarity_score": r.get("score", 0.0),
                    }
                    for r in limited
                ],
            }

        except KnowledgeBaseError as e:
            # Distinguished from the generic handler below so the response does
            # not read as "nothing matched". `total_results: 0` alongside a bare
            # "Search failed" is the same affirmative negative this campaign
            # closes on the agent path (#943) — a client rendering the count
            # shows "no results" for a search that never ran. The shape is
            # unchanged (no contract break); only the error text now says which
            # of the two happened.
            logger.error(f"Semantic search unavailable: {e}")
            return {
                "query": query,
                "total_results": 0,
                "results": [],
                "error": (
                    "Knowledge base search is unavailable — no documents were "
                    "searched. This is not a result of zero matches."
                ),
            }
        except Exception as e:
            logger.error(f"Semantic search failed: {e}")
            # Static `error` value — see list_documents (#866).
            return {
                "query": query,
                "total_results": 0,
                "results": [],
                "error": "Search failed",
            }

    async def fulltext_search_documents(
        self,
        query: str,
        document_type: Optional[str] = None,
        category: Optional[str] = None,
        tags: Optional[List[str]] = None,
        limit: int = 10,
        similarity_threshold: Optional[float] = None,
        rank_by: Optional[str] = None,
        user: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Full-text keyword search across document titles with RBAC filtering.

        Scores results by substring/word matches in title. Used by the
        /documents/search endpoint. Prefer search_documents() for
        intent-based queries — this method matches exact tokens, not meaning.
        """
        from faultmaven.api.v1.utils.parsing import normalize_tags_field

        try:
            result = await self.list_documents(
                document_type=document_type,
                tags=tags,
                limit=500,
                offset=0,
                user=user,
            )
            filtered_docs = result.get("documents", [])

            scored_results = []
            query_lower = query.lower()

            for doc in filtered_docs:
                score = 0.0
                title = doc.get("title", "").lower()

                if query_lower in title:
                    score += 0.8

                for word in query_lower.split():
                    if word in title:
                        score += 0.3

                if similarity_threshold is not None and score < similarity_threshold:
                    continue

                scored_results.append((doc, score))

            if rank_by and rank_by in ["priority"]:
                scored_results.sort(
                    key=lambda x: (
                        (
                            -1
                            if x[0].get(rank_by) == "high"
                            else 0 if x[0].get(rank_by) == "medium" else 1
                        ),
                        -x[1],
                    )
                )
            else:
                scored_results.sort(key=lambda x: x[1], reverse=True)

            limited_results = scored_results[:limit]

            logger.info(
                f"Full-text search '{query}' returned {len(limited_results)} results"
            )

            return {
                "query": query,
                "total_results": len(limited_results),
                "results": [
                    {
                        "document_id": doc.get("document_id", "unknown"),
                        "content": "",
                        "metadata": {
                            "title": doc.get("title", "Untitled"),
                            "document_type": doc.get("document_type", "general"),
                            "category": doc.get(
                                "category", doc.get("document_type", "general")
                            ),
                            "tags": normalize_tags_field(doc.get("tags", [])),
                            "priority": doc.get("priority", "normal"),
                        },
                        "similarity_score": score,
                    }
                    for doc, score in limited_results
                ],
            }

        except Exception as e:
            logger.error(f"Full-text search failed: {e}")
            # Static `error` value — see list_documents (#866).
            return {
                "query": query,
                "total_results": 0,
                "results": [],
                "error": "Search failed",
            }

    async def update_document_metadata(
        self, document_id: str, **kwargs
    ) -> Optional[Dict[str, Any]]:
        """Update a published runbook's fields in knowledge_items.

        Loads the row (source of truth), applies title/content/tags/category/
        document_type/version updates, re-indexes ChromaDB when content
        changed, and only then persists the row. Returns the updated DTO, or
        ``None`` when the document is not found.

        **The SQL row moves last, on purpose** (#952). Two stores cannot be
        committed atomically, so the question is only which inconsistent state
        a failure can leave — and the two are not equally bad:

        * SQL new / vectors old is a MISPAIRING. Retrieval answers with
          superseded text under a row that looks healthy to every consistency
          check there is, so nothing detects it and nothing repairs it. The
          investigation reasons from content the document no longer has.
        * SQL old / vectors missing is RECOVERABLE. The row is still correct,
          so the content needed to rebuild the vectors never left — on
          single-tenant the boot reconcile pass detects the chunkless parent
          and :meth:`reindex_missing_vectors` repairs it automatically, and
          everywhere else re-saving the document does the same thing by hand.
          (Under ``TENANT_PROVIDER=multi`` the web-startup KB bootstrap is
          skipped entirely, so there is no automatic pass on Cloud — the state
          is still repairable, just not self-repairing.)

        Committing the row before the re-index made the first state the common
        outcome — an unavailable embedder, the single most likely failure here,
        produced it every time. Indexing first makes the same failure leave
        both stores untouched, which is what #952 asked for.

        The read and the write are deliberately SEPARATE sessions, rather than
        one transaction held open across the re-index (the other way to order
        this). ``get_by_id`` returns a detached domain object, so nothing
        between them needs a connection — and embedding can take a cold BGE-M3
        load's 60-120s, which is a long time to hold a pool slot idle in
        transaction (and, where a deployment sets
        ``idle_in_transaction_session_timeout``, long enough to have the
        connection killed out from under the commit).

        Splitting them would widen the read-modify-write window to cover the
        whole embed, so the row is RE-READ inside the write session and the
        update applied to that fresh copy. ``repo.update`` writes every column
        from the object it is handed, so writing the pre-embed snapshot would
        revert any column another writer touched meanwhile — and one of those,
        ``is_published``, is how a built-in runbook is retired. The window is
        therefore back to milliseconds, as it was before the re-index moved
        ahead of the write.

        Residuals, stated rather than papered over:

        * The re-index itself is not atomic. If ``add_documents`` fails after
          the delete, the row keeps its previous content and the vectors are
          missing or partial — the recoverable class above, not a mispairing,
          but not "unchanged" either.
        * If the vector swap succeeds and ``repo.update`` then fails, the
          vectors are ahead of the row. That is compensated by
          :meth:`_resynchronise_after_failed_commit`, which re-reads the row
          and decides from what it OBSERVES — a raised commit does not prove
          the write failed, and restoring on that assumption alone would
          manufacture the mispairing rather than repair it.
        * Nothing compensates a process that dies, or a task cancelled
          (``CancelledError`` is not an ``Exception``), between the swap and
          the commit. No non-2PC design closes that window; it is named here so
          it is not mistaken for one that is covered.
        """
        try:
            from faultmaven.modules.knowledge.domain.models.knowledge_item import (
                KnowledgeItemType,
            )
            from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (  # noqa: E501
                DatabaseKnowledgeItemRepository,
            )

            content_changed = bool(kwargs.get("content"))

            # Read in its own session, which then CLOSES. `get_by_id` returns a
            # detached domain object (`_to_domain`), so nothing below needs the
            # connection — and holding one across the embed would leave a
            # PostgreSQL transaction idle for the whole cold-load window,
            # burning a pool slot and inviting
            # `idle_in_transaction_session_timeout` to kill the very connection
            # the commit still needs.
            async with self._db_session_factory() as session:
                item = await DatabaseKnowledgeItemRepository(session).get_by_id(
                    document_id
                )

            if item is None:
                logger.warning(f"Document {document_id} not found for update")
                return None

            # Gated on a wired vector store as well as on the content change:
            # with no store there is nothing to index, and building the index
            # model anyway would make a deployment that never indexes start
            # dereferencing `item.scope` / `item.item_type` on a path that used
            # to skip them entirely.
            needs_reindex = content_changed and bool(self._vector_store)

            # Nothing is snapshotted for rollback. The compensation re-reads the
            # row and rebuilds from what it OBSERVES, which is strictly better
            # than a pre-embed image: if anything committed while we embedded,
            # the snapshot describes a state the row no longer holds, and
            # restoring it would write exactly the mispairing the compensation
            # exists to remove.

            # Apply the requested changes ONCE, as data. The same values are
            # applied twice — to the snapshot, to build what gets embedded, and
            # to a freshly re-read row at commit time. Computing them once is
            # what keeps those two from drifting.
            updates = {}
            if kwargs.get("title"):
                updates["title"] = await self._sanitizer.asanitize(kwargs["title"])
            if kwargs.get("content"):
                updates["content"] = await self._sanitizer.asanitize(kwargs["content"])
            if "tags" in kwargs:
                updates["tags"] = [str(t) for t in (kwargs["tags"] or [])]
            if kwargs.get("category"):
                updates["category"] = kwargs["category"]
            if kwargs.get("document_type"):
                try:
                    updates["item_type"] = KnowledgeItemType(kwargs["document_type"])
                except ValueError:
                    logger.info(
                        f"Ignoring unknown document_type "
                        f"'{kwargs['document_type']}' on update"
                    )

            def _apply(target) -> None:
                for field, value in updates.items():
                    setattr(target, field, value)
                if "version" in kwargs:
                    meta = dict(target.metadata or {})
                    meta["version"] = kwargs["version"]
                    target.metadata = meta

            _apply(item)

            # Re-index ChromaDB BEFORE the row is written. The indexing path
            # embeds before it deletes, so an unavailable or hung embedder
            # raises here having written to neither store, and the update is
            # abandoned with both still describing the previous content
            # (#945 made it stop reporting success; #952 makes it stop
            # half-applying).
            #
            # The chunk metadata is stamped with this timestamp so it does not
            # carry the PREVIOUS one. The row's own `updated_at` is set by
            # `repo.update` at commit time and will be LATER than this by
            # however long the embed takes — seconds, or a cold load's minutes.
            # Nothing compares the two; retrieval reads the chunk value only as
            # a recency signal.
            if needs_reindex:
                item.updated_at = datetime.now(timezone.utc)
                # An edit re-chunks the content but leaves ``metadata["causes"]``
                # exactly as it was — editing a runbook's Causes section is the
                # everyday way for the record and the chunks to stop agreeing, so
                # the new pairing is checked like an ingest (fm#1103).
                await self._index_document_in_vector_store(
                    self._build_index_model(item),
                    causes=_row_causes(item.metadata),
                )

            # Everything from here is compensated as one unit. The vectors
            # already hold the new content, so ANY failure below — the session
            # failing to open, the re-read raising, the commit raising — leaves
            # them ahead of the row. Wrapping only the commit would let the
            # first two produce the undetectable mispairing silently.
            try:
                async with self._db_session_factory() as session:
                    repo = DatabaseKnowledgeItemRepository(session)

                    # RE-READ before writing, and apply the update to the fresh
                    # row. `repo.update` writes EVERY column from the object it
                    # is given, so committing the pre-embed snapshot would
                    # silently revert whatever another writer changed while we
                    # embedded — the read-modify-write window is now the whole
                    # embed rather than the milliseconds it used to be.
                    current = await repo.get_by_id(document_id)
                    if current is None:
                        # Deleted while we embedded. Our vectors describe a row
                        # that no longer exists, and orphan pruning only covers
                        # built-in pack ids — an authored document would stay
                        # searchable after deletion. Undo our own write.
                        if needs_reindex:
                            await self._discard_vectors_for_vanished_row(document_id)
                        logger.warning(
                            f"Document {document_id} was deleted during its "
                            "update; the update was abandoned"
                        )
                        return None

                    _apply(current)
                    await repo.update(current)  # commits + stamps updated_at

                    # Re-reading fixes the SQL half of a concurrent retirement;
                    # it does NOT undo the vectors we already re-added. That
                    # matters because retrieval does not honour `is_published`
                    # (`kb_qa` filters ChromaDB by scope alone) — which is
                    # exactly why `delete_document` deletes the vectors rather
                    # than only clearing the flag. So an unpublished row must
                    # end this method with no vectors, or a retired runbook
                    # stays queryable through chunks this update wrote.
                    if needs_reindex and not current.is_published:
                        await self._discard_vectors_for_vanished_row(document_id)
                        logger.warning(
                            "Document %s was unpublished during its update; the "
                            "edit was saved but its vectors were dropped, "
                            "because an unpublished runbook must not stay "
                            "retrievable",
                            document_id,
                        )

                    committed = current
            except Exception as commit_error:
                if needs_reindex:
                    await self._resynchronise_after_failed_commit(
                        document_id,
                        attempted_content=item.content,
                        commit_error=commit_error,
                    )
                raise

            logger.info(f"Successfully updated document {document_id}")

            # Built from the COMMITTED row, not the pre-embed snapshot: the
            # snapshot's `updated_at` predates the edit, and any field another
            # writer changed during the embed would be reported as the value we
            # tried to write rather than the one that is stored.
            return {
                "document_id": committed.item_id,
                "title": committed.title,
                "content": committed.content,
                "document_type": committed.item_type.value,
                "category": committed.category or "",
                "tags": list(committed.tags) if committed.tags else [],
                "updated_at": (
                    committed.updated_at.isoformat() if committed.updated_at else ""
                ),
            }

        except Exception as e:
            logger.error(f"Failed to update document {document_id}: {e}")
            raise

    async def bulk_update_documents(
        self, document_ids: List[str], updates: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Bulk update document metadata.

        ``errors`` is returned verbatim to the caller in a 200 body by
        ``POST /knowledge/documents/bulk-update``, which any authenticated
        caller may reach (#866), so per-target entries never carry exception
        text — the diagnostic stays in the log line.
        """
        updated_count = 0
        errors = []

        for doc_id in document_ids:
            try:
                result = await self.update_document_metadata(doc_id, **updates)
                if result:  # If not None (document found and updated)
                    updated_count += 1
                else:
                    errors.append(f"Document {doc_id} not found")
            except Exception as e:
                errors.append(f"Document {doc_id}: update failed")
                logger.error(f"Failed to update document {doc_id}: {e}")

        logger.info(
            f"Bulk update completed: {updated_count}/{len(document_ids)} documents updated"
        )

        return {
            "success": True,
            "updated_count": updated_count,
            "total_requested": len(document_ids),
            "errors": errors if errors else [],
        }

    async def bulk_delete_documents(self, document_ids: List[str]) -> Dict[str, Any]:
        """Bulk delete documents.

        ``errors`` is returned verbatim to the caller in a 200 body (see
        ``bulk_update_documents``), so neither the raised exception nor the
        per-document ``error`` field — both of which carry driver text — is
        echoed; the diagnostic stays in the log line.
        """
        deleted_count = 0
        errors = []

        for doc_id in document_ids:
            try:
                result = await self.delete_document(doc_id)
                if result.get("success"):
                    deleted_count += 1
                else:
                    errors.append(f"Document {doc_id}: delete failed")
                    logger.error(
                        f"Failed to delete document {doc_id}: "
                        f"{result.get('error', 'Unknown error')}"
                    )
            except Exception as e:
                errors.append(f"Document {doc_id}: delete failed")
                logger.error(f"Failed to delete document {doc_id}: {e}")

        logger.info(
            f"Bulk delete completed: {deleted_count}/{len(document_ids)} documents deleted"
        )

        return {
            "success": True,
            "deleted_count": deleted_count,
            "total_requested": len(document_ids),
            "errors": errors if errors else [],
        }

    async def get_knowledge_stats(self) -> Dict[str, Any]:
        """Get knowledge base statistics - API compatible method"""
        base_stats = await self.get_document_statistics()

        return {
            "total_documents": base_stats.get("total_documents", 0),
            "document_types": base_stats.get("documents_by_type", {}),
            "categories": {},  # Would be populated from real data
            "total_chunks": 0,  # Would be calculated from chunked documents
            "avg_chunk_size": 0,  # Would be calculated
            "storage_used": "0 MB",  # Would be calculated
            "last_updated": base_stats.get("last_updated"),
        }

    async def get_search_analytics(self) -> Dict[str, Any]:
        """Get search analytics"""
        return {
            "popular_queries": [],
            "search_volume": 0,
            "avg_response_time": 0.0,
            "hit_rate": 0.0,
            "category_distribution": {},
            "enhanced_metrics": {},
        }


# Phase 4 Complete: Adapter classes have been removed as core components now implement interfaces directly
