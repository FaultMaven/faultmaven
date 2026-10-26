"""Knowledge Service Refactored Module - Phase 3.3

Purpose: Interface-based knowledge service using dependency injection

This refactored service implements clean architecture principles by depending
on interfaces rather than concrete implementations, enabling better testability
and flexibility in the knowledge management system.

Core Responsibilities:
- Knowledge document management via interfaces
- Semantic search operations using IKnowledgeVectorStore
- Runbook body security checks (a body holding a credential is refused, never
  rewritten, #2012) and title and query sanitization via ISanitizer
- Distributed tracing via ITracer

Key Improvements over Original:
- Interface-based dependency injection
- Cleaner separation of concerns
- Better error handling and validation
- Improved testability through mocking interfaces
- Standardized tracing and logging patterns
"""

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set

if TYPE_CHECKING:
    # Type-only import; the runtime import stays lazy inside ingest_runbook
    # to avoid the service↔persistence cycle. This satisfies the forward-ref
    # annotation on ``verification_level`` for ruff/mypy without importing at
    # module load.
    from faultmaven.modules.knowledge.domain.models.knowledge_item import (
        VerificationLevel,
    )

from faultmaven.config.tenant_context import (
    get_current_billing_organization_id,
    usable_tenant_id,
    writable_enterprise_id,
)
from faultmaven.exceptions import ValidationException
from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
    KB_COLLECTION,
)
from faultmaven.models import KnowledgeBaseDocument, SearchResult
from faultmaven.models.exceptions import KnowledgeBaseError
from faultmaven.models.interfaces import (
    IKnowledgeVectorStore,
    ILLMProvider,
    ISanitizer,
    ITracer,
)
from faultmaven.models.vector_metadata import VectorMetadata
from faultmaven.modules.knowledge.domain.item_title import sanitize_item_title
from faultmaven.modules.knowledge.domain.services.runbook_validator import (
    RunbookQualityError,
    aenforce_runbook_quality,
    aenforce_runbook_security,
)
from faultmaven.modules.knowledge.domain.write_scope import (
    metadata_scope_floor,
    require_write_scope,
)
from faultmaven.utils.line_endings import normalize_line_endings
from faultmaven.utils.serialization import decode_json_blob, to_json_compatible

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


def _read_total_chunks(chunk_metadata: Optional[Dict[str, Any]]) -> Optional[int]:
    """How many chunks this hit's parent document was split into, or None.

    Returns None rather than a guess whenever the stamp is missing or unusable —
    the seeder's corroboration guard reads None as "unknown" and applies its full
    threshold, so a bad parse must never be able to masquerade as a small
    document and wave a runbook through.
    """
    if not chunk_metadata:
        return None
    raw = chunk_metadata.get("total_chunks")
    try:
        total = int(raw)
    except (TypeError, ValueError):
        return None
    return total if total > 0 else None


def _read_tags(
    result: Dict[str, Any], chunk_metadata: Optional[Dict[str, Any]]
) -> List[str]:
    """Tags for a hit, from wherever they actually are.

    ChromaDB metadata values must be scalars, so the write path joins the tag
    list into one comma-separated string; reading it back as a list is this
    function's whole job. Returns ``[]`` for an absent or unparseable value —
    tags are a display and filtering aid, never an admission input, so an empty
    list is a safe answer and an exception is not.
    """
    raw = result.get("tags")
    if raw is None and chunk_metadata:
        raw = chunk_metadata.get("tags")
    if isinstance(raw, str):
        return [t.strip() for t in raw.split(",") if t.strip()]
    if isinstance(raw, (list, tuple)):
        return [str(t).strip() for t in raw if str(t).strip()]
    return []


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
    enterprise_id: Optional[str],
) -> List[str]:
    """Resolve the ``knowledge_item`` ids shared to any of ``team_ids``.

    The team arm of the visible-id allowlist (ADR-011 D3). Returns ``[]`` when
    there is no share repository (e.g. an in-memory fallback), the principal
    belongs to no teams, or no enterprise is in hand — retrieval then
    collapses to ``personal ∪ global``, which is the fail-closed outcome: the
    two remaining arms are keyed on the caller's own ids.

    ``enterprise_id`` is the tenant the share row must itself be stamped
    with, matching the inventory clause's share sub-select. It is resolved
    through ``usable_tenant_id`` rather than used raw, because both callers hand
    over a value that can be the Standalone sentinel under
    ``TENANT_PROVIDER=multi`` — ``MilestoneEngine`` passes ``case.enterprise_id``,
    which ``CaseService.create_case`` stamps from the *total*
    ``get_current_enterprise_id``, and ``KnowledgeService.search_documents`` passes the
    requester's claim. Under multi the sentinel is not a tenant, so it must
    collapse the arm here rather than become the SQL predicate; under ``single``
    it is the deployment's one legitimate tenant and passes unchanged. This is
    the same decision ``require_actor_enterprise`` refuses on and the case
    read-allowlist arms degrade on — one predicate, three call sites.
    """
    tenant_id = usable_tenant_id(enterprise_id)
    if not share_repository or not team_ids or not tenant_id:
        return []
    return await share_repository.list_resource_ids(
        resource_type="knowledge_item",
        scope_type="team",
        scope_ids=list(team_ids),
        enterprise_id=tenant_id,
    )


# Relative weight of a title hit vs a body hit in the lexical search that backs
# ``POST /documents/search`` (#1288). They sum to 1.0, so the combined score
# shares the 0..1 scale the API's ``similarity_threshold`` declares. Title is
# weighted higher because it is the stronger signal, but a body-only hit still
# scores well above zero — matching content is the whole point of the endpoint.
_TITLE_MATCH_WEIGHT = 0.7
_CONTENT_MATCH_WEIGHT = 0.3

# How much of the body to return around a hit.
_EXCERPT_RADIUS = 120

# Upper bound on the documents whose BODY this scorer will tokenize for one
# request. The pre-#1288 endpoint read at most 500 inventory rows and scored
# only their titles; scoring content without a ceiling would tokenize the whole
# visible knowledge base per call, on a route an unauthenticated caller can
# reach. Kept at the same 500 so the bound is no looser than what it replaced.
_MAX_SCORED_DOCUMENTS = 500

# Matching is on WORD boundaries, not substrings. The first cut of this used
# ``word in field_lower``, which scored a document because a query's one-letter
# word was a substring of an unrelated one: the query "how to fix a disk issue"
# scored 0.1667 against "Kafka rebalance loop" on the strength of the ``a`` in
# "Kafka", and the single-character query "a" scored 0.7 against it via the
# verbatim branch below. Both contradicted the endpoint's own published text —
# "``timeout`` finds documents containing that token" and "documents matching
# neither field are not returned" — and with the default threshold the endpoint
# went back to answering every query with the whole visible inventory, which is
# the defect #1288 was opened about.
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def _may_read_document_content(user: Any) -> bool:
    """Whether this caller may be handed runbook BODY text by a search.

    Both knowledge search endpoints are optional-auth, so an unauthenticated
    caller reaches them and — via ``_inventory_visibility_clause`` / the vector
    scope filter — sees the global platform tier. That governs WHICH documents
    are visible; it says nothing about how much of one may be returned.

    The project already answers that separately: ``GET /documents/{id}``, the
    canonical "read this document's body", requires authentication (#867). A
    search that hands an anonymous caller an excerpt of the same body routes
    around that gate, so search results carry titles and metadata for everyone
    and body text only for callers who could have read the document directly.

    #1288 introduced that exposure on ``POST /documents/search`` by populating
    a ``content`` field that had always been ``""``; the check is applied on
    the vector arm of ``POST /knowledge/search`` too, which had been returning
    200 characters of chunk text to anonymous callers before this change. Both
    arms share the rule so the answer cannot depend on whether a vector store
    happens to be bound.
    """
    return bool(user is not None and getattr(user, "user_id", None))


def _tokenize(text: str) -> List[str]:
    """Lowercased word tokens, the same way for queries and for fields."""
    return _TOKEN_RE.findall(text.lower())


def _contains_phrase(field_tokens: List[str], query_tokens: List[str]) -> bool:
    """Whether the query's tokens appear consecutively in the field's."""
    span = len(query_tokens)
    if not span or span > len(field_tokens):
        return False
    return any(
        field_tokens[i : i + span] == query_tokens
        for i in range(len(field_tokens) - span + 1)
    )


def _score_field_match(query_tokens: List[str], field: str) -> float:
    """Lexical match of a query against one field, in 0..1.

    1.0 when the query's tokens appear consecutively (a verbatim phrase, modulo
    punctuation), else the fraction of the query's DISTINCT tokens present.
    """
    if not field or not query_tokens:
        return 0.0
    field_tokens = _tokenize(field)
    if not field_tokens:
        return 0.0
    if _contains_phrase(field_tokens, query_tokens):
        return 1.0
    present = set(field_tokens)
    distinct = list(dict.fromkeys(query_tokens))
    hits = sum(1 for token in distinct if token in present)
    return hits / len(distinct)


def _score_text_match(query_tokens: List[str], title: str, content: str) -> float:
    """Combined title+content lexical relevance, in 0..1.

    Monotone in both fields, so a document matching title *and* body outranks
    one matching either alone.
    """
    title_score = _score_field_match(query_tokens, title)
    content_score = _score_field_match(query_tokens, content)
    return _TITLE_MATCH_WEIGHT * title_score + _CONTENT_MATCH_WEIGHT * content_score


def _match_excerpt(query_tokens: List[str], content: str) -> str:
    """A short body excerpt around the query, for the search result card.

    Anchors on the whole query phrase where it occurs, and otherwise on the
    first query TOKEN present — a multi-word query rarely appears verbatim, so
    anchoring only on the full phrase returned the head of the document for the
    common case (the endpoint's own documented example,
    "PostgreSQL connection timeout", never occurs verbatim). Falls back to the
    head only when no token occurs at all, which is what a title-only match
    looks like.
    """
    if not content or not query_tokens:
        return ""

    lowered = content.lower()
    position = -1
    matched_len = 0

    phrase = " ".join(query_tokens)
    if len(query_tokens) > 1:
        position = lowered.find(phrase)
        matched_len = len(phrase)

    if position < 0:
        for token in query_tokens:
            found = _token_position(lowered, token)
            if found >= 0:
                position, matched_len = found, len(token)
                break

    if position < 0:
        excerpt = content[: _EXCERPT_RADIUS * 2].strip()
        return f"{excerpt}..." if len(content) > _EXCERPT_RADIUS * 2 else excerpt

    start = max(0, position - _EXCERPT_RADIUS)
    end = min(len(content), position + matched_len + _EXCERPT_RADIUS)
    excerpt = content[start:end].strip()
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(content) else ""
    return f"{prefix}{excerpt}{suffix}"


def _token_position(lowered: str, token: str) -> int:
    """Offset of ``token`` in ``lowered`` as a whole word, or -1."""
    match = re.search(rf"\b{re.escape(token)}\b", lowered)
    return match.start() if match else -1


class KnowledgeService:
    """Knowledge service using interface dependencies"""

    def __init__(
        self,
        sanitizer: ISanitizer,
        tracer: ITracer,
        vector_store: Optional[IKnowledgeVectorStore] = None,
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
            sanitizer: Interface for data sanitization (PII redaction)
            tracer: Interface for distributed tracing
            vector_store: Optional KB vector store (``IKnowledgeVectorStore``)
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

    async def search_knowledge(
        self,
        query: str,
        limit: int = 10,
        filters: Optional[Dict[str, Any]] = None,
        use_hybrid: bool = False,
        min_score: Optional[float] = None,
    ) -> List[SearchResult]:
        """
        Search knowledge base using interface dependencies

        Args:
            query: Search query text
            limit: Maximum number of results to return
            filters: Optional filters for search refinement
            use_hybrid: Retrieve through the store's hybrid pipeline (vector +
                keyword-constrained recall, then reranking) instead of pure
                vector search. Off by default so existing callers are unchanged;
                the KB pre-fetch that feeds the investigation prompt and the
                cause seeder passes True, because the terms that identify an
                incident are exactly the ones an embedding smooths away.
            min_score: Minimum raw cosine a hit must carry. Honoured only on
                the hybrid path, where results are ordered by the reranker's
                blend and a caller's own post-filter would therefore thin its
                window from the middle. The pure-vector path is ordered BY
                score, so filtering its output is equivalent and the caller's
                own filter remains the authority there.

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
                        if use_hybrid:
                            results = await self._vector_store.hybrid_search(
                                collection_name=KB_COLLECTION,
                                query=sanitized_query,
                                k=limit,
                                where=effective_filters,
                                min_score=min_score,
                            )
                        else:
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
                        # the knowledge_items row. Fall
                        # back to stripping the "_chunk_N" suffix off the chunk id
                        # (id == f"{parent}_chunk_{i}") when metadata omits it.
                        parent_document_id = result_meta.get(
                            "parent_document_id"
                        ) or _strip_chunk_suffix(result.get("id"))
                        # Title, type and tags live in the CHUNK METADATA — the
                        # vector store's formatted hit carries only
                        # id/content/metadata/score, so reading them off the top
                        # level meant every hit came back titled "Untitled",
                        # typed "general" and untagged. That reached the model:
                        # `case.kb_context` stores `r.title`, and the prompt
                        # renders it as "MATCH 1: Untitled", so the LLM was shown
                        # runbook prose with no way to tell which runbook it was
                        # reading or to cite it back. Top level is still
                        # preferred, so a store that does supply these wins.
                        search_result = SearchResult(
                            document_id=result.get(
                                "document_id", result.get("id", "unknown")
                            ),
                            title=(
                                result.get("title")
                                or result_meta.get("title")
                                or "Untitled"
                            ),
                            document_type=(
                                result.get("document_type")
                                or result_meta.get("document_type")
                                or "general"
                            ),
                            tags=_read_tags(result, result_meta),
                            score=result.get("score", 0.0),
                            snippet=result.get("snippet", result.get("content", ""))[
                                :200
                            ]
                            + "...",
                            parent_document_id=parent_document_id,
                            total_chunks=_read_total_chunks(result_meta),
                            rerank_score=result.get("rerank_score"),
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

    async def delete_document(self, document_id: str) -> Dict[str, Any]:
        """Remove a published runbook from the inventory (provenance-gated).

        Semantics depend on provenance (see ``is_builtin_item_id``):

        - **Built-in** (``kb_<12 hex>``) → **unpublish**: set
          ``is_published=False`` AND delete its ChromaDB vectors. A bare
          ``is_published=False`` is NOT sufficient — investigation retrieval
          does not honor the flag (``kb_qa`` filters ChromaDB by scope only),
          so the vectors must be removed or the runbook stays queryable.
          The row is kept because the pack would resurrect a hard delete anyway.
        - **Authored** (UUID / ``kb_<16 hex>``) → **hard delete**: drop the
          ``knowledge_items`` row and its vectors.

        **Invariant (#1966): a document reported removed has no retrievable
        chunk**, now or after any later restart, reconcile, repair or kb_seed
        run. Retrieval never checks that a chunk's row exists, and the
        reconcile pass deletes only built-in vectors (diffing the shared
        collection against one tenant's rows would delete other tenants'
        content), so a chunk whose row is gone would stay searchable for good.

        **What a restart (or kb_seed run) does to a removed document:**

        - An unpublished built-in STAYS unpublished with no vectors. The pack
          skip compares the pack's content hash with the hash stamped on the
          row when the pack last ingested it (``metadata.pack_content_hash``),
          not with the row's current content, so an operator's edit is never
          mistaken for a new pack version. The reconcile pass never repairs an
          unpublished row, and it deletes any vectors an unpublished built-in
          still has (an older version's repair re-embedded them). Only a NEW
          pack version of that runbook republishes it: a shipped update is a
          new document.
        - A deleted authored document has no row, so nothing re-indexes it:
          repair works from rows, and it re-checks the row after embedding,
          discarding what it wrote if a delete or unpublish landed meanwhile.
          That re-check is a locked read, which also waits for a delete still
          holding the row — for every repair pass whose role may UPDATE
          ``knowledge_items``: the web-startup bootstrap and SQLite. The one
          exception is ``kb_seed`` under ``TENANT_PROVIDER=multi``, whose
          maintenance role holds no UPDATE: its re-check is a plain read, so a
          delete or unpublish inside the milliseconds between its vector delete
          and its commit, while that job's repair embeds the same row, is not
          waited for.

        Two stores cannot commit atomically, so the order decides which
        failure is possible:

        1. The row change is issued UNCOMMITTED: a ``DELETE``, or a targeted
           ``UPDATE ... SET is_published = false`` that writes no other column
           but ``updated_at`` (so it cannot revert a concurrent committed
           edit). Zero rows
           matched — absent, deleted concurrently, or refused by an RLS write
           policy (a tenant session against a platform-tier row under multi,
           #770) — rolls back and reports not-removed WITHOUT touching the
           vector store. The shared ChromaDB collection holds every tenant's
           chunks, so a vector delete is only ever issued for a row this
           session proved it may change.
        2. ONE vector delete attempt (``retries=0``). A failure rolls the row
           change back and raises ``KnowledgeBaseError``: the document stays
           listed and published, and a retry completes the delete (the DELETE
           route answers 503). The vectors are not guaranteed intact — a
           remote store can fail after applying the delete, or part of it —
           so the outcome is the document as it was or the split state below,
           never a chunk without a row.
        3. Commit. A commit that fails, or a task cancelled at it, after the
           vector delete leaves the one recoverable split state, row present
           with no vectors: the document is still live (the caller was told
           the delete failed), a retry completes it, and otherwise the
           reconcile/repair pass re-indexes the published row. Logged at
           ERROR with the id.
        4. Team share rows, after the commit and best effort. A share row for
           a resource with no row grants nothing (visibility is computed from
           rows), so a failure here is logged and the delete still succeeds.
           Unpublish keeps the row, so keeps its shares.

        Holding the transaction across the vector call departs from
        :meth:`update_document_metadata`'s rule of never holding one across a
        vector-store call. That rule is about a 60-120 s embed; this is a
        single ChromaDB delete by metadata, and it is kept to one attempt
        because the hold is real: on SQLite it is the database's single writer
        lock, and the KB store's ``timeout`` cannot bound it (its ChromaDB
        calls are synchronous inside ``async def``). If a deployment's
        ``idle_in_transaction_session_timeout`` reaps the connection mid-hold,
        the commit fails and the outcome is the split state of step 3.

        Returns ``{success, document_id, action}`` where action is
        "unpublished" or "deleted", or ``{success: False, error}`` when the
        document is absent or no row matched.

        Raises:
            KnowledgeBaseError: removing the vectors failed, so the row change
                was rolled back and the delete did not complete; retrying it
                finishes it. ``error_code`` is
                ``KNOWLEDGE_VECTOR_DELETE_FAILED``.
            ValueError: empty ``document_id``.
            RuntimeError: any other failure, including a failed commit (whose
                outcome is the split state above).
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

                    # Step 1: the row change, uncommitted.
                    if builtin:
                        removed = await repo.update_fields(
                            document_id, {"is_published": False}, commit=False
                        )
                        action = "unpublished"
                    else:
                        removed = await repo.delete(document_id, commit=False)
                        action = "deleted"

                    if not removed:
                        # Zero rows matched. NEVER touch the shared vector store
                        # when this session did not change the row: an
                        # unguarded vector delete here would let any tenant
                        # destroy platform content for everyone (#770).
                        await repo.rollback()
                        return {
                            "success": False,
                            "error": f"Document {document_id} not {action}",
                        }

                    # Step 2: one vector delete attempt, row change still open.
                    store = self._vector_store
                    if store:
                        try:
                            count = await store.delete_documents_by_parent_id(
                                document_id, retries=0
                            )
                        except asyncio.CancelledError:
                            # Closing the session rolls the row change back;
                            # the vectors may be partly gone (split state).
                            logger.error(
                                "Document %s: the delete was cancelled while "
                                "removing its vectors, so it was not %s. The "
                                "row change is rolled back; the vectors may be "
                                "partly removed. Retry the delete to complete "
                                "it.",
                                document_id,
                                action,
                            )
                            raise
                        except Exception as vector_error:
                            await repo.rollback()
                            logger.error(
                                "Document %s was not %s: removing its vectors "
                                "failed (%s). The row change was rolled back, "
                                "so the document is still live; its vectors may "
                                "be partly removed if the store failed after "
                                "applying the delete. Retrying the delete "
                                "completes it.",
                                document_id,
                                action,
                                vector_error,
                            )
                            # Wraps CircuitBreakerError and timeouts too: to the
                            # caller every vector-side failure is the same
                            # retryable "did not complete".
                            raise KnowledgeBaseError(
                                f"Could not remove the vectors of document "
                                f"{document_id}; it was not {action}",
                                error_code="KNOWLEDGE_VECTOR_DELETE_FAILED",
                            ) from vector_error
                        logger.info(
                            f"Removed {count} chunks for document {document_id}"
                        )

                    # Step 3: commit. A failure — or a cancellation, which is
                    # not an Exception — after the vector delete leaves the
                    # split state, and says so.
                    try:
                        await repo.commit()
                    except (Exception, asyncio.CancelledError) as commit_error:
                        logger.error(
                            "SPLIT KNOWLEDGE DOCUMENT %s: its vectors were "
                            "removed, but committing the row change failed "
                            "(%r), so it was not %s. Unless the commit landed "
                            "regardless, the row is still present with no "
                            "vectors: live, but not retrievable — and the "
                            "caller was told the delete failed. Retry the "
                            "delete to complete it; otherwise the KB "
                            "reconcile/repair pass (startup bootstrap or "
                            "kb_seed) re-indexes the published row.",
                            document_id,
                            commit_error,
                            action,
                        )
                        raise

                # Step 4: team shares, after the commit, best effort.
                if action == "deleted" and self._share_repo:
                    try:
                        await self._share_repo.delete_for_resource(
                            "knowledge_item", document_id
                        )
                    except Exception as share_error:
                        logger.error(
                            "Deleted document %s, but removing its team share "
                            "rows failed (%s). They grant nothing — visibility "
                            "is computed from knowledge_items rows — but remain "
                            "in resource_shares until removed by hand.",
                            document_id,
                            share_error,
                        )

                logger.info(f"{action} document {document_id}")
                return {
                    "success": True,
                    "document_id": document_id,
                    "action": action,
                }

            except (ValidationException, KnowledgeBaseError):
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
        *,
        enterprise_id: str,
    ) -> int:
        """Index a document's chunks + embeddings into the vector store.

        Args:
            enterprise_id: REQUIRED — the ``enterprise_id`` of the persisted
                ``knowledge_items`` row this document is, stamped onto every
                chunk (#1168). Always the ROW's own value, never the ambient
                request tenant: a global-tier row carries the platform
                (Standalone) enterprise, and a repair or re-index may run with
                no request bound at all. ``KnowledgeBaseDocument`` has no such
                field (it is an API model), which is why it travels here.
            prechunked: Build-time ``(chunk_text, embedding)`` pairs from a KB
                pack. When supplied, the document is NOT re-chunked or
                re-embedded — these exact chunk texts and vectors are written.
                This is the fast, model-free boot path for shipped runbooks
                (embedding 1244 chunks on a CPU-limited pod otherwise takes
                ~tens of minutes). When None, the document is chunked with
                :class:`ContentChunker` and embedded with BGE-M3 — the
                upload/draft path. Per-chunk metadata is derived from the
                document frontmatter either way (it is not carried in the pack).
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
                Also raised when ``document`` names no knowledge tier
                (``KNOWLEDGE_SCOPE_REQUIRED``) or names one that does not exist
                (``KNOWLEDGE_SCOPE_INVALID``) — see :func:`require_write_scope`.
            ValueError: If ``enterprise_id`` is not a non-blank string (#1168)
                — refused first, before any store check, chunking, embedding
                or deletion, with the same rule and message as the store's own
                refusal (:meth:`VectorMetadata.require_enterprise_id_value`).
        """
        # The live KB writer's tier check (#1166). ``KnowledgeBaseDocument.scope``
        # is required, which stops an omission at construction; this is the belt
        # to that brace, for anything reaching the indexer without having gone
        # through the model — a duck-typed stand-in, a future DTO. Ahead of the
        # ``_vector_store`` check on purpose: whether a store happens to be
        # configured says nothing about whether the caller made a tier decision,
        # and a refusal that only fires in some deployments is not a guard.
        #
        # The document's ``scope`` is read exactly ONCE, here, and the stamp
        # below is derived from the value this returns. Re-reading
        # ``document.scope`` at stamping time would be a second evaluation —
        # and for the property-backed stand-ins this guard exists to catch, the
        # second one is the unchecked one.
        _scope = require_write_scope(
            getattr(document, "document_id", None), getattr(document, "scope", None)
        )

        # The tenant check (#1168), for the same reasons and in the same place:
        # ahead of the ``_vector_store`` early exit, because a refusal that only
        # fires in deployments with a store is not a guard; before any chunking
        # or embedding, because a call that cannot be written must not pay for
        # a cold model load first; and OUTSIDE the ``try`` below, because a
        # missing tenant is the caller's programming error, not the transient
        # ``KNOWLEDGE_INDEXING_FAILED`` that handler reports. The RAW argument
        # is checked, and this check is what refuses a non-string or blank
        # tenant: ``VectorMetadata`` alone decodes bytes (``b"ent"`` becomes
        # ``"ent"``) and admits whitespace. The store re-checks each chunk's
        # metadata as a second layer.
        VectorMetadata.require_enterprise_id_value(
            enterprise_id, document_id=getattr(document, "document_id", None)
        )

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

            # Extract RAG-enrichment fields from frontmatter
            fm_meta = self._extract_frontmatter_for_rag(document.content)

            # Derived from the tier validated at the top of this method — not
            # from a second read of ``document.scope`` (#1166). This line used
            # to be ``getattr(document, "scope", "global") or "global"``, which
            # reached the platform tier three separate ways: pass it, omit it,
            # or pass a falsy value.
            _meta_scope = metadata_scope_floor(_scope)

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
                    enterprise_id=enterprise_id,
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
            # implements delete_documents_by_parent_id (IKnowledgeVectorStore
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

    async def _discard_vectors_for_vanished_row(self, document_id: str) -> None:
        """Drop vectors this process just wrote for a row that was deleted or
        unpublished while it wrote them — by an update (#952) or a repair
        (#1966).

        Unlike :meth:`delete_document`, which can refuse the delete and leave
        the row in place when its vector delete fails, this runs after the row
        is already gone or retired. For a deleted authored document these
        chunks sit under an id that now 404s, and orphan pruning only covers
        built-in pack ids — so nothing will ever remove them. That earns a
        loud, specific log rather than a shrug.

        Still does not raise: the caller is already abandoning its write and
        reporting that; a secondary failure must not replace the primary one.
        """
        try:
            await self._vector_store.delete_documents_by_parent_id(document_id)
        except Exception as discard_error:
            logger.error(
                "ORPHANED KNOWLEDGE VECTORS %s: the document was deleted or "
                "unpublished while its vectors were being written, and "
                "removing those chunks failed (%s). They stay retrievable "
                "under an id that no longer resolves (or is retired), and "
                "orphan pruning does not cover authored documents. Remove them "
                "by hand, or re-create and re-delete the document.",
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
        """Realign the vectors after the update's row write failed, by OBSERVING it.

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
                    enterprise_id=observed.enterprise_id,
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

    async def _create_team_share(
        self,
        *,
        resource_type: str,
        resource_id: str,
        team_id: str,
        enterprise_id: str,
        created_by: Optional[str] = None,
    ) -> None:
        """Record a team share for a resource (idempotent), the source of truth
        for team visibility (ADR-013 §D4).

        No-ops when no share repository is wired (e.g. minimal/test wiring) —
        team publishing is then inert. Cross-ENTERPRISE sharing is structurally
        impossible: the share carries the resource's own ``enterprise_id`` and
        the read allowlist only ever resolves teams the requester belongs to
        (within their RLS-isolated enterprise), so a share to a foreign team is
        unreachable. RLS is the backstop. Sharing across ORGANIZATIONS inside one
        enterprise is, by contrast, exactly what a team is for (ADR-017 D4).
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
            enterprise_id=enterprise_id,
            created_by=created_by,
        )

    async def ingest_runbook(
        self,
        document_id: str,
        title: str,
        content: str,
        enterprise_id: str,
        scope: str,
        document_type: str = "runbook",
        tags: Optional[List[str]] = None,
        source_url: Optional[str] = None,
        owner_id: Optional[str] = None,
        team_id: Optional[str] = None,
        verified_by: Optional[str] = None,
        verification_level: "Optional[VerificationLevel]" = None,
        prechunked: Optional[List[tuple[str, List[float]]]] = None,
        pack_content_hash: Optional[str] = None,
        shipped: bool = False,
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
            enterprise_id: The enterprise the row is isolated to (ADR-017 D1).
                Required for every tier including global: it is what RLS keys
                on, and the global-write policy compares it against the
                Standalone sentinel.
            scope: REQUIRED knowledge tier — ``global`` | ``team`` |
                ``personal``. No default (#1166): ``global`` is the platform
                corpus every tenant reads, so publishing into it must be a
                decision the call site states, not one it inherits by staying
                quiet. Global authoring is gated separately (route-layer
                ``require_global_authoring_allowed`` /
                ``ensure_global_authoring_allowed``); this parameter is about
                the tier being *named*, not about who may name it.
            pack_content_hash: The KB pack's ``content_hash`` for this
                runbook, when the pack bootstrap is the caller. Stamped on the
                row as ``metadata.pack_content_hash`` — what the next boot's
                skip compares the pack against, so a later edit of the row is
                never mistaken for a new pack version (#1966). Every other
                caller leaves it out and the metadata stays ``{}``.
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
            shipped: True ONLY for platform-shipped KB pack content
                (``bootstrap/kb_init.py``). The title of every other runbook
                is user- or model-supplied text, shown to every reader of its
                tier and never reversed, so it is redacted here, once, before
                the row or the vectors carry it (#1981) — the rule
                :meth:`update_document` already applies. Its BODY is never
                rewritten: a body holding a credential is refused
                (:func:`enforce_runbook_security`, #2012), here, before
                anything is written. A shipped title and body are vendored,
                reviewed source: stored as written, unchecked, so that a
                redaction outage that begins after a healthy start (Presidio
                going down mid-seed under a fail-closed posture) cannot stop
                the platform corpus from seeding, and the pack's own titles
                are never rewritten. A seed that STARTS with the analyzer down
                runs regex-only and does not raise either way. Defaults to
                False: a caller that says nothing gets the scrub and the check.

        Returns:
            Number of chunks indexed in ChromaDB (always > 0 on success). On
            ChromaDB failure the method raises after deleting the SQL row, so
            a successful return always means both stores are populated.

        Raises:
            RunbookQualityError: a non-shipped body holds a secret or a
                destructive command (422). Nothing has been written.
        """
        # Lazy imports keep this method self-contained against the knowledge
        # vertical (avoids top-level cycles between service and persistence).
        from faultmaven.modules.knowledge.domain.models.knowledge_item import (
            PACK_CONTENT_HASH_KEY,
            KnowledgeItem,
            KnowledgeItemType,
            VerificationLevel,
        )
        from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (
            DatabaseKnowledgeItemRepository,
        )
        from faultmaven.modules.knowledge.taxonomy import KnowledgeScope

        now = datetime.now(timezone.utc)

        if not shipped:
            from faultmaven.infrastructure.persistence.models import (
                KnowledgeItemModel,
            )

            # The body, before any row or vector carries it (#2012): one
            # holding a credential is refused, never rewritten. Every
            # non-shipped publish converges here — upload, and ``verify_draft``
            # for a template, conversion or case draft, including a draft
            # whose ``validation_passed`` predates the secret check.
            await aenforce_runbook_security(content)

            # The title, before any row or vector carries it (#1981). A title
            # that cannot be scrubbed (``RedactionUnavailableError`` under a
            # fail-closed posture) is not written, and nothing has been written
            # yet.
            title = await sanitize_item_title(
                self._sanitizer, title, KnowledgeItemModel.title
            )

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
            # Isolation is not optional for any tier (ADR-017 D1): every row
            # carries an enterprise, GLOBAL included, and the column is NOT
            # NULL. What GLOBAL drops is the ORGANIZATION — it is the
            # organization-free platform tier (#770), which is what
            # ``knowledge_items_global_org_check`` states.
            enterprise_id=enterprise_id,
            # Billing attribution, read from the actor's organization like every
            # other writer (ADR-017 D2). It used to be a parameter, and not one
            # of the three call sites ever supplied it — so every runbook this
            # path published was billed to nobody, silently, and the parameter
            # read as a decision somebody had made. Forced to None for GLOBAL:
            # the platform tier is organization-free (#770,
            # ``knowledge_items_global_org_check``).
            organization_id=(
                None
                if KnowledgeScope(scope) == KnowledgeScope.GLOBAL
                else get_current_billing_organization_id()
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
            metadata=(
                {PACK_CONTENT_HASH_KEY: pack_content_hash} if pack_content_hash else {}
            ),
        )
        async with self._db_session_factory() as session:
            repo = DatabaseKnowledgeItemRepository(session)
            await repo.create(item)

        # Team publish: record visibility in the share table (source of truth,
        # ADR-013 §D4). The scope enum stays 'team' ⟺ ≥1 share row.
        # Cross-ENTERPRISE shares are impossible by construction — the share
        # carries the item's own enterprise_id, and a team belongs to exactly one
        # enterprise. Cross-organization shares inside it are the point.
        if scope == KnowledgeScope.TEAM.value and team_id:
            await self._create_team_share(
                resource_type="knowledge_item",
                resource_id=document_id,
                team_id=team_id,
                enterprise_id=enterprise_id,
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
            # The value just written to the row's NOT NULL enterprise_id.
            chunks_created = await self._index_document_in_vector_store(
                doc_model, prechunked=prechunked, enterprise_id=enterprise_id
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

    async def reindex_missing_vectors(
        self, item_id: str, *, role_may_update: bool
    ) -> int:
        """Re-chunk + re-embed an existing row's chunks into the vector store.

        Cross-store repair for an ORPHANED row — a ``knowledge_items`` row whose
        ChromaDB chunks are missing (a crash between the SQL commit and the
        vector write in :meth:`ingest_runbook` leaves exactly this half-state,
        as does a :meth:`delete_document` whose commit failed after its vector
        delete; the KB bootstrap reconcile pass can detect it but cannot fix it
        without the embedding model). This re-derives the vectors from the row's
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

        **Only a published row is repaired** (#1966). An unpublished row with
        no vectors is not an orphan but the intended state:
        :meth:`delete_document` unpublishes a built-in by dropping its vectors,
        because retrieval does not honour ``is_published``. Re-indexing it
        would silently undo the unpublish at the next boot. The reconcile pass
        already lists published rows only; this refusal (logged, nothing
        written) is the second line, for any caller that does not. The row is
        read again AFTER the vectors are written, and if it was deleted or
        unpublished meanwhile they are discarded and 0 is returned; a read
        that fails discards too.

        ``role_may_update`` is the repair pass's one capability answer
        (``KnowledgeItemRepository.may_update``). When the role may UPDATE the
        re-read is LOCKED, so it also waits for a delete or unpublish still
        holding the row. The ``kb_seed`` maintenance role (multi-tenant) holds
        no UPDATE, and a locked read would be refused — so it reads plainly:
        it still sees a delete or unpublish that committed during the embed,
        but not one inside the milliseconds between that writer's vector
        delete and its commit.

        Returns the number of chunks indexed. Fail-safe 0 (no raise) when the
        row is absent or unpublished (before or after the embed), no vector
        store is wired, the embedding
        model is unavailable, or the row names no usable tenant (the indexer's
        #1168 ``ValueError``, logged at ERROR with the item id) — leaving the
        row orphaned for a later boot to repair, which the caller counts as a
        failed repair.

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
        from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (  # noqa: E501
            DatabaseKnowledgeItemRepository,
        )

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

        if not row.is_published:
            logger.warning(
                "reindex_missing_vectors: %s is unpublished, and an unpublished "
                "row has no vectors by design (that is how it was removed from "
                "retrieval) — refusing to re-index it",
                item_id,
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
        try:
            chunks = await self._index_document_in_vector_store(
                doc_model, prechunked=None, enterprise_id=row.enterprise_id
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
        except ValueError as e:
            # The indexer's tenant check (#1168), refused before any work. Not
            # reachable from a real row — ``knowledge_items.enterprise_id`` is
            # NOT NULL — but this method promises no raise, so a row that names
            # no usable tenant is a failed repair like any other: loud, per row,
            # and never an aborted startup.
            logger.error(
                f"reindex_missing_vectors: repair refused for {item_id}: {e} — "
                "the row names no usable enterprise_id; leaving it orphaned"
            )
            return 0

        # RE-READ after writing (#1966), as update_document_metadata does
        # (#952): the row was read before an embed that can take minutes, and
        # a delete or unpublish that landed meanwhile — on another replica or
        # worker, or the user's retry of a delete that left the split state —
        # has already removed whatever vectors it found. Keeping ours would
        # leave a chunk with no row (permanent for an authored id) or a retired
        # runbook retrievable. Where the role may UPDATE the read is LOCKED: it
        # waits for a delete or unpublish still holding the row, so it sees
        # that writer's outcome whichever order the two ran in (FOR SHARE on
        # PostgreSQL, the writer lock on SQLite — see ``read_is_published``).
        # The maintenance role reads plainly (see the docstring).
        if chunks > 0:
            try:
                async with self._db_session_factory() as session:
                    repo = DatabaseKnowledgeItemRepository(session)
                    published = await repo.read_is_published(
                        item_id, lock=role_may_update
                    )
                    await repo.rollback()  # releases the lock; nothing to keep
            except Exception as reread_error:
                # Unverified means discarded: the worst outcome is then the
                # row without vectors that the next pass repairs again, never
                # a chunk without a row.
                logger.error(
                    "reindex_missing_vectors: re-reading %s after writing its "
                    "vectors failed (%s); discarding them rather than keeping "
                    "vectors for a row that may be gone",
                    item_id,
                    reread_error,
                )
                published = None
            if not published:
                await self._discard_vectors_for_vanished_row(item_id)
                logger.warning(
                    "reindex_missing_vectors: %s was deleted or unpublished (or "
                    "could not be re-read) while it was being repaired; the "
                    "vectors just written were discarded",
                    item_id,
                )
                return 0
        return chunks

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
        scope: str,
        tags: Optional[List[str]] = None,
        source_url: Optional[str] = None,
        category: Optional[str] = None,
        description: Optional[str] = None,
        owner_id: Optional[str] = None,
        team_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Upload document: create SQLite record + ingest into ChromaDB.

        Everything this publishes becomes a ``KnowledgeItemType.RUNBOOK`` (see
        :meth:`ingest_runbook`) and the ``ConversionDraftModel`` written below
        claims ``validation_passed=True``. The structural gate is enforced HERE
        (#1214), before the first side effect, not at the route, so every caller
        gets it and the ``validation_passed`` claim is one this method has
        actually checked.

        Args:
            title: Redacted with the sanitizer before it is written anywhere
                (#1981), so the upload row, the draft row, the
                ``knowledge_items`` row, the vectors and the response all carry
                the same scrubbed text.
            scope: REQUIRED knowledge tier — ``global`` | ``team`` |
                ``personal``. No default (#1166); see
                :meth:`ingest_runbook`, which this delegates to.

        Raises:
            RunbookQualityError: the content fails the runbook quality gate
                (422 at the API boundary). Raised before any row is
                created, so a refusal leaves nothing behind and needs no
                compensation.
        """
        try:
            import uuid as _uuid

            from faultmaven.modules.knowledge.draft_content import (
                ensure_draft_content_within_bound,
            )
            from faultmaven.utils.frontmatter import extract_frontmatter_metadata
            from faultmaven.utils.runbook_id import authored_item_id

            # Line endings, before the gate and before any write (#1403). This
            # is the publish choke point, which is why the quality gate was
            # moved here in #1214 — and the same argument applies: routes
            # multiply, this does not.
            #
            # Placed here rather than only at the request boundary because this
            # method owns what gets STORED: ``content`` below becomes the
            # ``knowledge_items.content`` row, the draft row's ``content``,
            # ``size_bytes`` AND the ChromaDB chunks. Normalising only for
            # matching would leave the authoring gate measuring an LF document
            # while the chunker
            # chunked a CRLF one — the gate's "measured exactly as the chunker
            # sees it" claim (``_validate_cause_chunk_boundaries``) would become
            # false, silently.
            content = normalize_line_endings(content)

            # The draft bound (#1950), ahead of the gate's CPU: this text becomes
            # a draft row, and one over the bound could never be saved back.
            # Refused before anything is minted or ingested; 422.
            ensure_draft_content_within_bound(content)

            # The quality gate, before the id is minted and before any row
            # exists. A refusal must leave nothing behind, which is only free
            # if nothing has happened yet.
            #
            # Off the event loop: the validator is pure CPU (compiled regexes +
            # yaml.safe_load, no shared mutable state, so thread-safe) and
            # measured at 31.5 ms on the largest shipped runbook (48 KB),
            # scaling with size toward the upload cap. Blocking the loop for
            # that long stalls every other in-flight request. This is the ONLY
            # place the gate runs now — the upload route's second pass was
            # deleted (#1214 review) — so one hop covers both publish paths.
            await aenforce_runbook_quality(content)

            from faultmaven.infrastructure.persistence.models import (
                ConversionDraftModel,
                ConversionJobModel,
                KnowledgeItemModel,
                UploadedFileModel,
            )

            # The title, once, before the first row: the upload row's
            # ``filename`` and the draft row's ``title`` below are written from
            # it BEFORE ``ingest_runbook`` runs, so they would otherwise hold
            # the text the ``knowledge_items`` row does not (#1981). Still
            # nothing written, so a title that cannot be scrubbed leaves
            # nothing behind.
            #
            # Cut to the item's column in the same breath (``String(512)``):
            # the upload, job and draft rows below are COMMITTED before
            # ``ingest_runbook`` writes the item, so a title the item cannot
            # hold would fail there and strand a verified draft with no item,
            # listed to everyone at global scope.
            title = await sanitize_item_title(
                self._sanitizer, title, KnowledgeItemModel.title
            )
            # The two ``String(255)`` copies are scrubbed again to their own
            # column, never cut from the scrubbed text blind: see
            # ``sanitize_item_title``.
            draft_title = await sanitize_item_title(
                self._sanitizer, title, ConversionDraftModel.title
            )
            filename = await sanitize_item_title(
                self._sanitizer, title, UploadedFileModel.filename
            )

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
            # enterprise_id NOT NULL — fall back to the single-tenant default
            # when no explicit enterprise is in scope. Resolved before the
            # session block because ingest_runbook below needs it too.
            # The enterprise the SESSION is bound to, never a hardcoded
            # sentinel (#1143). This path used to be unreachable under
            # TENANT_PROVIDER=multi — the route refused every upload with the
            # platform-tier gate — so stamping the Standalone sentinel was
            # harmless. #1377 makes personal and team uploads reachable under
            # multi, and `writable_enterprise_id`'s own docstring names the
            # failure that would follow: "under TENANT_PROVIDER=multi it is not
            # the caller's tenant but the sentinel enterprise, so PostgreSQL
            # rejects the INSERT with new row violates row-level security
            # policy" — and on SQLite, which has no RLS, it would silently
            # write the row into a foreign tenant instead.
            enterprise_id = writable_enterprise_id(None)

            # Create SQLite record (synthetic draft, immediately verified)
            conversion_id = f"conv_{_uuid.uuid4().hex[:12]}"
            draft_id = f"draft_{_uuid.uuid4().hex[:12]}"

            async with self._db_session_factory() as session:
                # ``conversion_jobs.source_file_id`` is a FK to
                # ``uploaded_files``. Create the upload row first.
                source_file_id = f"file_{_uuid.uuid4().hex[:12]}"
                upload = UploadedFileModel(
                    file_id=source_file_id,
                    enterprise_id=enterprise_id,
                    case_id=None,  # KB-bound, not case-bound
                    uploaded_by=owner_id,
                    # The title, which is what this upload has for a name:
                    # nothing is written to a file (#1950).
                    filename=filename,
                    size_bytes=len(content.encode()),
                    content_type="text/markdown",
                    # A storage-backend key or NULL, never a path: the text is
                    # in the draft row below and in ``knowledge_items``, not
                    # in a backend, so there is no key to store (#836).
                    storage_ref=None,
                    upload_source="conversion_source",
                    uploaded_at_turn=0,
                )
                session.add(upload)
                await session.flush()

                job = ConversionJobModel(
                    id=conversion_id,
                    user_id=owner_id,  # NULL if anonymous; FK SET NULL
                    enterprise_id=enterprise_id,
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
                    enterprise_id=enterprise_id,
                    conversion_id=conversion_id,
                    runbook_id=document_id,
                    title=draft_title,
                    content=content,
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
                enterprise_id=enterprise_id,
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

        except RunbookQualityError:
            # A refused draft is a client-side outcome (422), not a service
            # fault. Logged as a warning by the global handler; re-raised here
            # without the "Failed to upload document" ERROR line, which would
            # otherwise make every rejected paste look like an outage.
            raise
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
            from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
            from faultmaven.modules.knowledge.domain.models.knowledge_item import (
                KnowledgeItemType,
            )
            from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (  # noqa: E501
                DatabaseKnowledgeItemRepository,
            )

            enterprise_id = (
                getattr(user, "enterprise_id", None) or STANDALONE_ENTERPRISE_ID
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
                    enterprise_id=enterprise_id,
                    user_id=user_id,
                    team_ids=team_ids,
                    item_type=item_type,
                )

            # DTO build + tag filter over the RBAC-isolated set. Response shape
            # is the legacy conversion_drafts path's plus the two trust fields
            # (verification_level / verification_status, #878) — an additive
            # change, so the dashboard needs no contract change;
            # conversion-pipeline metadata (domain/service/severity/
            # quality_score) is null for built-ins, which never went through
            # that pipeline.
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
                        **KnowledgeService._verification_fields(item),
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
            **KnowledgeService._verification_fields(item),
        }

    @staticmethod
    def _verification_fields(item: Any) -> Dict[str, Any]:
        """The trust fields every document read carries (#878).

        One helper for ``list_documents`` and ``_document_dto`` so the two
        shapes cannot drift; the status comes from the one rule,
        ``KnowledgeItem.get_verification_status``.
        """
        return {
            "verification_level": int(item.verification_level),
            "verification_status": item.get_verification_status(),
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
        shared-to-my-teams), and ``enterprise_id`` is sourced exactly as
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

            from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
            from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (  # noqa: E501
                DatabaseKnowledgeItemRepository,
            )

            enterprise_id = (
                getattr(user, "enterprise_id", None) or STANDALONE_ENTERPRISE_ID
            )
            user_id = getattr(user, "user_id", None) if user else None

            async with self._db_session_factory() as session:
                repo = DatabaseKnowledgeItemRepository(session)
                item = await repo.get_visible_by_id(
                    document_id,
                    enterprise_id=enterprise_id,
                    user_id=user_id,
                    team_ids=team_ids,
                )

            if item is None:
                return None

            return self._document_dto(item)

        except Exception as e:
            logger.error(f"Failed to get visible document {document_id}: {e}")
            return None

    async def visible_document_ids(
        self,
        document_ids: List[str],
        user: Optional[Any] = None,
        team_ids: Optional[List[str]] = None,
    ) -> Set[str]:
        """The subset of ``document_ids`` the requester may read, in one query.

        The batched counterpart of :meth:`get_document_visible`, with the same
        rule and the same ``enterprise_id`` sourcing: an id is in the result iff
        ``get_document_visible`` would return its document. Fails CLOSED: any
        error answers the empty set, so a caller gating on it withholds rather
        than discloses (#1919).
        """
        ids = [d for d in document_ids if d]
        if not ids:
            return set()
        try:
            from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
            from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (  # noqa: E501
                DatabaseKnowledgeItemRepository,
            )

            enterprise_id = (
                getattr(user, "enterprise_id", None) or STANDALONE_ENTERPRISE_ID
            )
            user_id = getattr(user, "user_id", None) if user else None

            async with self._db_session_factory() as session:
                repo = DatabaseKnowledgeItemRepository(session)
                return await repo.visible_item_ids(
                    ids,
                    enterprise_id=enterprise_id,
                    user_id=user_id,
                    team_ids=team_ids,
                )

        except Exception as e:
            logger.error(f"Failed to resolve visible document ids: {e}")
            return set()

    async def get_relevant_snippet(
        self,
        document_id: str,
        query: str,
        max_lines: int = 5,
    ) -> Optional[Dict[str, Any]]:
        """Pick the document window that best matches a query, LEXICALLY.

        Splits the document into consecutive ``max_lines`` windows and scores
        each by word overlap with the query, plus a bonus when the query
        appears verbatim. **No embedding and no vector search happen here** —
        the ranking is Python string matching over the live document text, so
        a window that repeats the query's words outranks a paraphrase that
        shares none of them.

        That is deliberate, not a stand-in for something better (#1288):

        * The response is a ``line_start``/``line_end`` range over the text as
          it is stored. The KB's vectors are 512-token chunks with 50-token
          overlap carrying no line numbers, so they cannot answer this shape
          without re-deriving offsets — and overlapping chunks make that
          mapping ambiguous.
        * This backs a hover card. Embedding the windows per hover would put a
          BGE-M3 load (cold: 60-120s, behind a process-wide lock) on a pointer
          movement.
        * A document can be published with its vectors missing — that is what
          :meth:`reindex_missing_vectors` repairs. The hover card still has to
          render, and a lexical window always can.

        This used to be gated on ``if self._vector_store:`` while doing the
        same string matching inside the gate. The store was truthiness-tested
        and never called, so the only thing the gate decided was *which* of two
        lexical algorithms ran — the same query on the same document returned a
        different window depending on whether a vector store happened to be
        configured. The gate is gone; the window scoring below is now what
        every deployment gets.

        Args:
            document_id: Document identifier
            query: User's query to find relevant content
            max_lines: Maximum lines to return in the snippet

        Returns:
            Dict with snippet, line_start, line_end, relevance_score
            or None if the document is not found or has no content.
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

            # Rank consecutive line windows by overlap with the query.
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

            best_chunk = None
            best_score = 0.0
            query_lower = query.lower()
            # The same tokenizer the /documents/search scorer uses. Splitting on
            # whitespace made "timeout." a single token, so a query for
            # "timeout" missed the line that mentioned it — two lexical paths
            # documented in near-identical words behaving differently.
            query_words = set(_tokenize(query))

            for chunk in chunks:
                chunk_lower = chunk["text"].lower()
                chunk_words = set(_tokenize(chunk["text"]))

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

            # Nothing overlapped a whole window: centre one on the FIRST line
            # containing the query verbatim, else fall back to the head of the
            # document. First, not last — the score below is the constant 1.0
            # and the guard is strict, so every later hit compares 1.0 < 1.0
            # and loses. That is fine as behaviour (the earliest mention is as
            # good an anchor as any) but it was previously commented as "last",
            # which is the kind of comment-vs-code drift this whole change is
            # about.
            best_start = 0
            best_score = 0.0

            for i, line in enumerate(lines):
                if query_lower in line.lower():
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
            logger.error(f"Failed to get relevant snippet for {document_id}: {e}")
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
                # Without this the vectorless fallback saw a narrower corpus
                # than the vector path it stands in for: the scope filter
                # resolves shared-to-my-teams, and dropping team_ids here made
                # team-shared runbooks invisible whenever the store was absent.
                team_ids=team_ids,
            )

        try:
            user_id = getattr(user, "user_id", None) if user else None
            may_read_content = _may_read_document_content(user)

            # Resolve the "shared-to-my-teams" arm from the share table (source
            # of truth), then build the visible-id allowlist filter
            # (global ∪ owned ∪ shared). ADR-013 §D4 / ADR-011 D3. The share
            # rows are matched against the caller's own org, so a row stamped
            # with a foreign tenant never widens this filter.
            shared_ids = await resolve_shared_kb_ids(
                self._share_repo,
                team_ids,
                getattr(user, "enterprise_id", None) if user else None,
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
                        "content": (
                            r.get("content", "")[:200] + "..."
                            if may_read_content
                            else ""
                        ),
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
        team_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Full-text keyword search across document titles AND CONTENT (#1288).

        Backs ``POST /documents/search``. Matches exact tokens, not meaning —
        prefer :meth:`search_documents` for intent-based queries.

        Reads the same RBAC-isolated row set the inventory does
        (``list_for_inventory``: global ∪ own-org owned ∪ shared-to-my-teams,
        enforced in-query), rather than the inventory *DTO*. The DTO drops
        ``content`` and ``category``, which is why this endpoint could only
        ever score titles; going to the repository directly gains both with
        identical tenant isolation — same function, same arguments, no extra
        query. It deliberately does NOT use
        ``KnowledgeItemRepository.search_by_text``, whose ``enterprise_id``
        predicate carries no global arm and would therefore drop every
        platform-tier runbook (global rows hold no org — #770).

        Scoring is a weighted sum, normalised to 0..1 so it shares a scale with
        the ``similarity_threshold`` the API declares:

        * per field, the query scores 1.0 if it appears verbatim, else the
          fraction of its words present;
        * title is weighted above content, so a title hit outranks a body hit
          while a document matching both outranks either.

        **Non-matches are dropped.** This used to return the entire visible
        inventory, scoring a genuine body match 0.0 — identical to a document
        matching nothing at all — so real hits were crowded out by ``limit`` in
        creation order (#1288).
        """
        from faultmaven.api.v1.utils.parsing import normalize_tags_field
        from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
        from faultmaven.modules.knowledge.domain.models.knowledge_item import (
            KnowledgeItemType,
        )
        from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (  # noqa: E501
            DatabaseKnowledgeItemRepository,
        )

        try:
            enterprise_id = (
                getattr(user, "enterprise_id", None) or STANDALONE_ENTERPRISE_ID
            )
            user_id = getattr(user, "user_id", None) if user else None
            may_read_content = _may_read_document_content(user)

            item_type = None
            if document_type:
                try:
                    item_type = KnowledgeItemType(document_type)
                except ValueError:
                    # Unknown type → no matching items, as in list_documents.
                    logger.info(
                        f"Unknown document_type filter '{document_type}' — "
                        "no matching items"
                    )
                    return {"query": query, "total_results": 0, "results": []}

            async with self._db_session_factory() as session:
                repo = DatabaseKnowledgeItemRepository(session)
                items = await repo.list_for_inventory(
                    enterprise_id=enterprise_id,
                    user_id=user_id,
                    team_ids=team_ids,
                    item_type=item_type,
                )

            query_tokens = _tokenize(query)
            # Every score is 0 without tokens (a blank or punctuation-only
            # query), and a search that matches nothing returns nothing.
            if not query_tokens:
                return {"query": query, "total_results": 0, "results": []}

            scored_results = []
            for item in items[:_MAX_SCORED_DOCUMENTS]:
                tag_list = list(item.tags) if item.tags else []

                # Filters are applied over the already tenant-isolated set,
                # mirroring list_documents.
                if tags and not any(t in tag_list for t in tags):
                    continue
                if category and (item.category or "") != category:
                    continue

                score = _score_text_match(
                    query_tokens, item.title or "", item.content or ""
                )
                if score <= 0.0:
                    continue
                if similarity_threshold is not None and score < similarity_threshold:
                    continue

                scored_results.append((item, score))

            if rank_by and rank_by in ["priority"]:
                scored_results.sort(
                    key=lambda x: (
                        (
                            -1
                            if (x[0].metadata or {}).get(rank_by) == "high"
                            else (
                                0
                                if (x[0].metadata or {}).get(rank_by) == "medium"
                                else 1
                            )
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
                        "document_id": item.item_id,
                        "content": (
                            _match_excerpt(query_tokens, item.content or "")
                            if may_read_content
                            else ""
                        ),
                        "metadata": {
                            "title": item.title or "Untitled",
                            "document_type": item.item_type.value,
                            "category": item.category or item.item_type.value,
                            "tags": normalize_tags_field(
                                list(item.tags) if item.tags else []
                            ),
                            "priority": (item.metadata or {}).get("priority", "normal"),
                        },
                        "similarity_score": score,
                    }
                    for item, score in limited_results
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

        A new ``content`` is stored as sent, line endings normalised, and is
        refused with ``RunbookQualityError`` (422) before anything is written
        when it holds a credential or a destructive command
        (:func:`enforce_runbook_security`, #2012). It is never rewritten. A
        new ``title`` is redacted (#1981).

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
        whole embed, so the row is RE-READ inside the write session, and the
        write is TARGETED (``repo.update_fields``): it sets only the fields
        this update changes and never ``is_published``, which is how a
        built-in runbook is retired. A full-row write would put back whatever
        it read for every other column, so an unpublish committing between
        the re-read and the write would be reverted (#1966). After the write
        commits the row is read once more, and if it is unpublished the
        vectors this update wrote are discarded.

        Residuals, stated rather than papered over:

        * The re-index itself is not atomic. If ``add_documents`` fails after
          the delete, the row keeps its previous content and the vectors are
          missing or partial — the recoverable class above, not a mispairing,
          but not "unchanged" either.
        * If the vector swap succeeds and the row write
          (``repo.update_fields``) then fails, the vectors are ahead of the
          row. That is compensated by
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
            from faultmaven.infrastructure.persistence.models import (
                KnowledgeItemModel,
            )
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
            if kwargs.get("content"):
                # Normalised here too (#1403). This is the ONLY write path that
                # reaches ChromaDB with caller-supplied content and NO runbook
                # quality gate: ``PUT /knowledge/documents/{id}`` is
                # ``require_authentication`` with an untyped ``dict`` body, so
                # there is no request model to normalise on, and a ``content``
                # key here sets ``needs_reindex`` and re-chunks the document
                # below. Under CRLF that put ``\r`` into the embedded text and
                # moved the chunk boundaries — measured across the shipped
                # corpus as 16 extra chunks and 1284 of 1297 chunks carrying a
                # ``\r``.
                content = normalize_line_endings(kwargs["content"])
                # The security half of the gate, before the first write
                # (#2012): a body holding a credential is refused (422), and
                # the body is otherwise stored as the author wrote it. It used
                # to be run through the sanitizer instead, which rewrote 20 of
                # the 91 shipped bodies — IP addresses, and commands cut
                # (``get-login-password <PASSWORD_…> "$REGION"``) — on every
                # edit, and redacted nothing on create.
                await aenforce_runbook_security(content)
                updates["content"] = content
            if kwargs.get("title"):
                updates["title"] = await sanitize_item_title(
                    self._sanitizer, kwargs["title"], KnowledgeItemModel.title
                )
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
            # the row write at commit time and will be LATER than this by
            # however long the embed takes — seconds, or a cold load's minutes.
            # Nothing compares the two; retrieval reads the chunk value only as
            # a recency signal.
            if needs_reindex:
                item.updated_at = datetime.now(timezone.utc)
                await self._index_document_in_vector_store(
                    self._build_index_model(item),
                    enterprise_id=item.enterprise_id,
                )

            # Everything from here is compensated as one unit. The vectors
            # already hold the new content, so ANY failure below — the session
            # failing to open, the re-read raising, the commit raising — leaves
            # them ahead of the row. Wrapping only the commit would let the
            # first two produce the undetectable mispairing silently.
            try:
                async with self._db_session_factory() as session:
                    repo = DatabaseKnowledgeItemRepository(session)

                    # RE-READ before writing: it tells a document deleted while
                    # we embedded from one that is still there.
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

                    # A TARGETED write of only the fields this update changes
                    # (#1966). A full-row write from the re-read would put back
                    # whatever it read for every other column — and one of
                    # those, `is_published`, is how a built-in runbook is
                    # retired: an unpublish committing between our re-read and
                    # our write would be reverted, and the runbook republished
                    # with the vectors written above. The targeted UPDATE waits
                    # for such a writer (its row lock on PostgreSQL, the writer
                    # lock on SQLite) and leaves its `is_published` alone.
                    # (The metadata blob is still read-modify-written when a
                    # `version` is given; the only other metadata writer is the
                    # pack bootstrap's hash stamp.)
                    fields = dict(updates)
                    if "version" in kwargs:
                        meta = dict(current.metadata or {})
                        meta["version"] = kwargs["version"]
                        fields["metadata"] = meta
                    if fields and not await repo.update_fields(document_id, fields):
                        # Deleted between the re-read and the write.
                        if needs_reindex:
                            await self._discard_vectors_for_vanished_row(document_id)
                        logger.warning(
                            f"Document {document_id} was deleted during its "
                            "update; the update was abandoned"
                        )
                        return None

                    # The row AS COMMITTED, after our write: whether it is
                    # still published is only known now. Retrieval does not
                    # honour `is_published` (`kb_qa` filters ChromaDB by scope
                    # alone) — which is exactly why `delete_document` deletes
                    # the vectors rather than only clearing the flag — so an
                    # unpublished row must end this method with no vectors, or
                    # a retired runbook stays queryable through chunks this
                    # update wrote. An unpublish committing after this read
                    # deletes the vectors itself.
                    committed = await repo.get_by_id(document_id)
                    if committed is None:
                        # Deleted after our write committed: that delete's own
                        # vector delete came after ours, so nothing is left.
                        _apply(current)
                        committed = current
                    elif needs_reindex and not committed.is_published:
                        await self._discard_vectors_for_vanished_row(document_id)
                        logger.warning(
                            "Document %s was unpublished during its update; the "
                            "edit was saved but its vectors were dropped, "
                            "because an unpublished runbook must not stay "
                            "retrievable",
                            document_id,
                        )
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

        except RunbookQualityError:
            # A refused body is a client-side outcome (422), not a fault:
            # re-raised without the ERROR line, as ``upload_document`` does.
            raise
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
            except RunbookQualityError as refused:
                # The body holds a credential (#2012): the caller's content,
                # refused before anything was written, not a fault. The entry
                # stays the generic text this endpoint's policy requires.
                errors.append(f"Document {doc_id}: update failed")
                logger.info(f"Refused the update of document {doc_id}: {refused}")
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
        echoed; the diagnostic stays in the log line. Each entry is a constant
        sentence per id: a vector-store failure (``KnowledgeBaseError``, which
        :meth:`delete_document` raises having rolled its row change back) says
        the delete did not complete and a retry finishes it; anything else
        says only that the delete failed.
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
            except KnowledgeBaseError as e:
                errors.append(
                    f"Document {doc_id}: not deleted, because removing it from "
                    "search did not complete; retrying the delete finishes it"
                )
                logger.error(f"Failed to delete document {doc_id}: {e}")
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
