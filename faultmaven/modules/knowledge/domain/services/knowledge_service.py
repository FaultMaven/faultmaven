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

from faultmaven.exceptions import ServiceException, ValidationException
from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
    KB_COLLECTION,
)
from faultmaven.models import KnowledgeBaseDocument, SearchResult
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
    share_repository: Optional[Any], team_ids: Optional[List[str]]
) -> List[str]:
    """Resolve the ``knowledge_item`` ids shared to any of ``team_ids``.

    The team arm of the visible-id allowlist (ADR-011 D3). Returns ``[]`` when
    there is no share repository (e.g. an in-memory fallback) or the principal
    belongs to no teams — retrieval then collapses to ``personal ∪ global``.
    """
    if not share_repository or not team_ids:
        return []
    return await share_repository.list_resource_ids(
        resource_type="knowledge_item",
        scope_type="team",
        scope_ids=list(team_ids),
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
        db_session_factory: Optional[Any] = None,
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
            db_session_factory: Optional async session factory for SQLite access
        """
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
        # Set by main.py after ConversionService is created (avoids circular DI).
        # When present, all ConversionDraftModel mutations are delegated to it.
        self._conversion_service: Optional[Any] = None

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

            if not self._db_session_factory:
                return {"success": False, "error": "No database session factory"}

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

                if self._db_session_factory:
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

    async def _index_document_in_vector_store(
        self,
        document: KnowledgeBaseDocument,
        prechunked: Optional[List[tuple[str, List[float]]]] = None,
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

        Returns:
            Number of chunks indexed (0 on failure).
        """
        if not self._vector_store:
            return 0

        try:
            # Remove old chunks for this document (safe for re-ingestion). The
            # vector store implements delete_documents_by_parent_id (IVectorStore
            # contract) — calling it directly, with no hasattr guard, so a store
            # that silently lacks it raises here instead of leaving stale vectors
            # behind on every re-ingest (the KB drift this campaign fixed).
            await self._vector_store.delete_documents_by_parent_id(document.document_id)

            if prechunked is not None:
                # Pack fast path: write the pack's chunk texts + vectors as-is.
                # No ContentChunker, no model load.
                chunks = [text for text, _ in prechunked]
                embeddings = [embedding for _, embedding in prechunked]
                if not chunks:
                    logger.warning(f"Pack supplied 0 chunks for {document.document_id}")
                    return 0
            else:
                from faultmaven.infrastructure.model_cache import model_cache
                from faultmaven.modules.knowledge.domain.services.content_chunker import (  # noqa: E501
                    ContentChunker,
                )

                chunks = ContentChunker().split(document.content)
                if not chunks:
                    logger.warning(
                        f"No chunks produced for document {document.document_id}"
                    )
                    return 0
                # Batch-embed all chunks off the event loop via the model_cache
                # async boundary. None → BGE unavailable, skip vector indexing.
                embeddings = await model_cache.aembed_texts(chunks)
                if embeddings is None:
                    logger.error("BGE-M3 model unavailable, skipping vector indexing")
                    return 0

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
                )
                doc_dicts.append(
                    {
                        "id": f"{document.document_id}_chunk_{i}",
                        "content": chunk,
                        "metadata": meta.to_chroma_metadata(),
                    }
                )

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

        except Exception as e:
            logger.error(f"Failed to index document in vector store: {e}")
            return 0

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
        if self._db_session_factory is None:
            raise ServiceException(
                "ingest_runbook requires a db_session_factory; "
                "vector-only ingestion is no longer supported"
            )
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
            metadata={"causes": causes} if causes else None,
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
                doc_model, prechunked=prechunked
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

    async def _delete_knowledge_item_row(self, item_id: str) -> None:
        """Best-effort cleanup of an orphaned knowledge_items row.

        Used by `ingest_runbook` when vector indexing fails — guarantees no
        half-state remains. Errors are logged but suppressed since the
        caller is already on a failure path and will raise its own error.
        """
        from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (
            DatabaseKnowledgeItemRepository,
        )

        if self._db_session_factory is None:
            return
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
            # default when no explicit org is in scope. Hoisted out of the
            # db-session block so ingest_runbook below has it regardless.
            from faultmaven.providers.tenancy.single_tenant import (
                SingleTenantProvider,
            )

            org_id = SingleTenantProvider.DEFAULT_ORG_ID

            # Create SQLite record (synthetic draft, immediately verified)
            if self._db_session_factory:
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
                        source_type="upload",
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
                        source_type="upload",
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
            if not self._db_session_factory:
                logger.warning("No db_session_factory for list_documents")
                return {
                    "documents": [],
                    "total_count": 0,
                    "limit": limit,
                    "offset": offset,
                }

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
            return {
                "documents": [],
                "total_count": 0,
                "limit": limit,
                "offset": offset,
                "error": str(e),
            }

    async def get_document(self, document_id: str) -> Optional[Dict[str, Any]]:
        """Get a published runbook by ID from knowledge_items.

        Content comes from the stored row (``knowledge_items.content``), not
        from disk — the row is the source of truth for the published
        inventory. Mirrors the ``list_documents`` DTO shape with a ``content``
        field added.
        """
        try:
            if not document_id:
                return None

            if not self._db_session_factory:
                return None

            from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (  # noqa: E501
                DatabaseKnowledgeItemRepository,
            )

            async with self._db_session_factory() as session:
                repo = DatabaseKnowledgeItemRepository(session)
                item = await repo.get_by_id(document_id)

            if item is None:
                return None

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

        except Exception as e:
            logger.error(f"Failed to get document {document_id}: {e}")
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
            if not item_id or not self._db_session_factory:
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
            if not item_id or not self._db_session_factory:
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
            # (global ∪ owned ∪ shared). ADR-013 §D4 / ADR-011 D3.
            shared_ids = await resolve_shared_kb_ids(self._share_repo, team_ids)
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

        except Exception as e:
            logger.error(f"Semantic search failed: {e}")
            return {"query": query, "total_results": 0, "results": [], "error": str(e)}

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
            return {"query": query, "total_results": 0, "results": [], "error": str(e)}

    async def update_document_metadata(
        self, document_id: str, **kwargs
    ) -> Optional[Dict[str, Any]]:
        """Update a published runbook's fields in knowledge_items.

        Loads the row (source of truth), applies title/content/tags/category/
        document_type/version updates, persists via the repository, and
        re-indexes ChromaDB when content changed. Returns the updated DTO, or
        ``None`` when the document is not found.
        """
        try:
            if not self._db_session_factory:
                return None

            from faultmaven.modules.knowledge.domain.models.knowledge_item import (
                KnowledgeItemType,
            )
            from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (  # noqa: E501
                DatabaseKnowledgeItemRepository,
            )

            content_changed = bool(kwargs.get("content"))

            async with self._db_session_factory() as session:
                repo = DatabaseKnowledgeItemRepository(session)
                item = await repo.get_by_id(document_id)
                if item is None:
                    logger.warning(f"Document {document_id} not found for update")
                    return None

                if kwargs.get("title"):
                    item.title = await self._sanitizer.asanitize(kwargs["title"])
                if kwargs.get("content"):
                    item.content = await self._sanitizer.asanitize(kwargs["content"])
                if "tags" in kwargs:
                    item.tags = [str(t) for t in (kwargs["tags"] or [])]
                if kwargs.get("category"):
                    item.category = kwargs["category"]
                if kwargs.get("document_type"):
                    try:
                        item.item_type = KnowledgeItemType(kwargs["document_type"])
                    except ValueError:
                        logger.info(
                            f"Ignoring unknown document_type "
                            f"'{kwargs['document_type']}' on update"
                        )
                if "version" in kwargs:
                    meta = dict(item.metadata or {})
                    meta["version"] = kwargs["version"]
                    item.metadata = meta

                await repo.update(item)  # commits + bumps updated_at

            # Re-index ChromaDB when content changed (delete+add atomic swap).
            if content_changed and self._vector_store:
                doc_model = KnowledgeBaseDocument(
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
                await self._index_document_in_vector_store(doc_model)

            logger.info(f"Successfully updated document {document_id}")

            return {
                "document_id": item.item_id,
                "title": item.title,
                "content": item.content,
                "document_type": item.item_type.value,
                "category": item.category or "",
                "tags": list(item.tags) if item.tags else [],
                "updated_at": item.updated_at.isoformat() if item.updated_at else "",
            }

        except Exception as e:
            logger.error(f"Failed to update document {document_id}: {e}")
            raise

    async def bulk_update_documents(
        self, document_ids: List[str], updates: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Bulk update document metadata"""
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
                errors.append(f"Failed to update document {doc_id}: {e}")
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
        """Bulk delete documents"""
        deleted_count = 0
        errors = []

        for doc_id in document_ids:
            try:
                result = await self.delete_document(doc_id)
                if result.get("success"):
                    deleted_count += 1
                else:
                    errors.append(
                        f"Failed to delete document {doc_id}: {result.get('error', 'Unknown error')}"
                    )
            except Exception as e:
                errors.append(f"Failed to delete document {doc_id}: {e}")
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
