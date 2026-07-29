"""
Runbook Knowledge Base - Vector Similarity Search for Runbooks

Provides intelligent runbook search and indexing with dual-source support:
1. Incident-driven runbooks: Generated from resolved incidents
2. Document-driven runbooks: Generated from uploaded operational documentation

Both types are indexed in ChromaDB for unified similarity matching to prevent
duplicate runbook generation.

Architecture Reference: docs/architecture/document-generation-and-closure-design.md
Section 5.4.5: Dual-Source Runbook Architecture
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from faultmaven.infrastructure.base_client import BaseExternalClient
from faultmaven.infrastructure.persistence.chromadb_store import ChromaDBVectorStore
from faultmaven.models.report import (
    CaseReport,
    ReportStatus,
    ReportType,
    RunbookMetadata,
    RunbookSource,
    SimilarRunbook,
)
from faultmaven.utils.serialization import to_json_compatible

logger = logging.getLogger(__name__)


class RunbookKnowledgeBase(BaseExternalClient):
    """
    Knowledge base for runbook similarity search.

    Supports TWO runbook sources:
    1. Incident-driven: Generated after case resolution
    2. Document-driven: Generated from uploaded documentation

    Both types indexed and searched uniformly for maximum knowledge reuse.
    """

    #: Decorative — logged at init and nowhere else. The injected
    #: ``ChromaDBVectorStore`` is bound to its own collection (the general KB's
    #: ``faultmaven_kb`` by default) and this class never selects a collection,
    #: so runbooks live alongside KB documents and the ``report_type ==
    #: "runbook"`` metadata predicate in ``search_runbooks`` is the ONLY thing
    #: separating them. Do not "simplify" that predicate away, and do not read
    #: this constant as evidence of a dedicated collection (#912).
    COLLECTION_NAME = "faultmaven_runbooks"
    MIN_SIMILARITY_THRESHOLD = 0.65  # Minimum 65% similarity

    def __init__(self, vector_store: ChromaDBVectorStore, embedding_model=None):
        """
        Initialize runbook knowledge base

        Args:
            vector_store: ChromaDB vector store instance
            embedding_model: Optional embedding model (uses BGE-M3 from KB if not provided)
        """
        super().__init__(
            client_name="runbook_knowledge_base",
            service_name="RunbookKB",
            enable_circuit_breaker=True,
            circuit_breaker_threshold=5,
            circuit_breaker_timeout=30,
        )

        self.vector_store = vector_store
        self.embedding_model = embedding_model

        logger.info(
            "RunbookKnowledgeBase initialized",
            extra={"collection": self.COLLECTION_NAME},
        )

    async def search_runbooks(
        self,
        query_embedding: List[float],
        *,
        organization_id: str,
        filters: Optional[Dict[str, Any]] = None,
        top_k: int = 5,
        min_similarity: float = MIN_SIMILARITY_THRESHOLD,
    ) -> List[SimilarRunbook]:
        """
        Search for similar runbooks using semantic similarity, scoped to one tenant.

        Searches BOTH incident-driven and document-driven runbooks belonging to
        ``organization_id``. The tenant predicate is mandatory and the method
        **fails closed**: with no organization the search returns ``[]`` without
        issuing a query, never an unscoped one (see
        ``docs/architecture/security/rbac.md`` — "Tenant-Scoped Resolution").

        Args:
            query_embedding: Query embedding vector (1024-dim for BGE-M3)
            organization_id: Owning tenant; REQUIRED, and the only predicate that
                keeps one tenant's runbooks out of another's results
            filters: Optional metadata filters (domain, tags, etc.)
            top_k: Number of results to return (default 5)
            min_similarity: Minimum similarity threshold (default 0.65)

        Returns:
            List of SimilarRunbook objects sorted by similarity score (descending)
        """
        if not organization_id:
            logger.warning(
                "Refusing unscoped runbook search: no organization_id supplied"
            )
            return []

        async def _search_wrapper():
            # Build the ChromaDB where clause. Every condition is a separate
            # single-key dict combined under an explicit ``$and``: ChromaDB
            # (>=1.0) validates that a ``where`` mapping carries exactly one
            # operator, so the multi-key implicit-AND form this code used before
            # is rejected outright. ``$and`` needs >= 2 operands, which the
            # report_type + organization_id pair always satisfies.
            conditions: List[Dict[str, Any]] = [
                {"report_type": "runbook"},
                {"organization_id": organization_id},
            ]
            if filters and filters.get("domain"):
                conditions.append({"domain": filters["domain"]})
                # Note: ChromaDB doesn't support array filtering directly for tags
                # Tags will be filtered post-query if needed
            where_clause: Dict[str, Any] = {"$and": conditions}

            # Query vector database
            try:
                results = await self.vector_store.query_by_embedding(
                    query_embedding=query_embedding, where=where_clause, top_k=top_k
                )
            except Exception as e:
                logger.error(f"ChromaDB query failed: {e}")
                # Return empty list on query failure
                return []

            # Parse results and filter by minimum similarity
            similar_runbooks = []

            if not results or "ids" not in results or not results["ids"]:
                logger.debug("No runbooks found matching query")
                return similar_runbooks

            # ChromaDB returns nested lists: [[id1, id2, ...]]
            ids_list = results["ids"][0] if results["ids"] else []
            distances_list = results["distances"][0] if results.get("distances") else []
            metadatas_list = results["metadatas"][0] if results.get("metadatas") else []
            documents_list = results["documents"][0] if results.get("documents") else []

            for i, report_id in enumerate(ids_list):
                # Convert distance to similarity score (ChromaDB uses L2 distance)
                # For cosine similarity: similarity = 1 - distance
                # Assuming normalized embeddings, so distance ≈ 2(1-similarity)
                distance = distances_list[i] if i < len(distances_list) else 1.0
                similarity = max(0.0, 1.0 - (distance / 2.0))

                if similarity < min_similarity:
                    continue

                metadata = metadatas_list[i] if i < len(metadatas_list) else {}
                content = documents_list[i] if i < len(documents_list) else ""

                # Reconstruct CaseReport from stored data
                try:
                    runbook = CaseReport(
                        report_id=report_id,
                        case_id=metadata.get("case_id", "unknown"),
                        report_type=ReportType.RUNBOOK,
                        title=metadata.get("title", "Untitled Runbook"),
                        content=content,
                        format="markdown",
                        generation_status=ReportStatus.COMPLETED,
                        generated_at=metadata.get(
                            "created_at", to_json_compatible(datetime.now(timezone.utc))
                        ),
                        generation_time_ms=0,  # Not stored for indexed runbooks
                        is_current=True,
                        version=1,
                        linked_to_closure=False,
                        metadata=RunbookMetadata(
                            source=RunbookSource(
                                metadata.get("runbook_source", "incident_driven")
                            ),
                            domain=metadata.get("domain", "general"),
                            tags=(
                                metadata.get("tags", "").split(",")
                                if metadata.get("tags")
                                else []
                            ),
                            document_title=metadata.get("document_title"),
                            case_context=None,  # Not reconstructed from search
                        ),
                    )

                    similar_runbook = SimilarRunbook(
                        runbook=runbook,
                        similarity_score=similarity,
                        case_title=metadata.get("case_title", "Unknown"),
                        case_id=metadata.get("case_id", "unknown"),
                    )

                    similar_runbooks.append(similar_runbook)

                except Exception as e:
                    logger.warning(f"Failed to reconstruct runbook {report_id}: {e}")
                    continue

            # Sort by similarity score descending
            similar_runbooks.sort(key=lambda x: x.similarity_score, reverse=True)

            logger.info(
                f"Found {len(similar_runbooks)} similar runbooks",
                extra={
                    "top_similarity": (
                        similar_runbooks[0].similarity_score
                        if similar_runbooks
                        else 0.0
                    ),
                    "min_threshold": min_similarity,
                },
            )

            return similar_runbooks

        return await self.call_external(
            operation_name="search_runbooks",
            call_func=_search_wrapper,
            timeout=5.0,  # 5 seconds for vector search
            retries=2,
        )

    async def index_runbook(
        self,
        runbook: CaseReport,
        *,
        organization_id: str,
        source: RunbookSource = RunbookSource.INCIDENT_DRIVEN,
        case_title: Optional[str] = None,
        domain: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> None:
        """
        Index runbook for future similarity search, stamped with its tenant.

        Supports BOTH runbook sources:
        - Incident-driven: Called after case resolution (default)
        - Document-driven: Called from document processing flow

        Both types stored with identical structure for uniform search.

        ``organization_id`` is REQUIRED and is stamped into the ChromaDB
        metadata: it is the key ``search_runbooks`` filters on, so a row written
        without one is invisible to every search. Indexing therefore fails
        closed — an org-less call writes nothing rather than an unreachable,
        unattributed row. ``CaseReport`` carries no organization of its own, so
        the tenant must be supplied by the caller (from ``Case.organization_id``).

        Args:
            runbook: CaseReport object to index
            organization_id: Owning tenant; REQUIRED
            source: RunbookSource (incident_driven or document_driven)
            case_title: Title of case or document (optional, will use runbook.title)
            domain: Technology domain (optional, will use from runbook.metadata)
            tags: Classification tags (optional, will use from runbook.metadata)
        """
        if runbook.report_type != ReportType.RUNBOOK:
            logger.warning(
                f"Attempted to index non-runbook report type: {runbook.report_type}"
            )
            return

        if not organization_id:
            logger.error(
                "Refusing to index runbook without an organization_id: "
                "an untenanted row is unreachable by every scoped search",
                extra={"report_id": runbook.report_id},
            )
            return

        async def _index_wrapper():
            # Extract metadata
            metadata_obj = runbook.metadata
            final_domain = domain or (
                metadata_obj.domain if metadata_obj else "general"
            )
            final_tags = tags or (metadata_obj.tags if metadata_obj else [])
            final_case_title = case_title or runbook.title

            # Build metadata dict for ChromaDB
            chroma_metadata = {
                "report_id": runbook.report_id,
                "organization_id": organization_id,
                "case_id": runbook.case_id,
                "case_title": final_case_title,
                "title": runbook.title,
                "report_type": "runbook",
                "runbook_source": source.value,
                "domain": final_domain,
                "tags": ",".join(final_tags),  # ChromaDB stores as string
                "created_at": runbook.generated_at,
            }

            # Add source-specific metadata
            if source == RunbookSource.DOCUMENT_DRIVEN and metadata_obj:
                if metadata_obj.document_title:
                    chroma_metadata["document_title"] = metadata_obj.document_title
                if metadata_obj.original_document_id:
                    chroma_metadata["original_document_id"] = (
                        metadata_obj.original_document_id
                    )

            # Generate BGE-M3 embedding explicitly to match collection dimensions
            from faultmaven.infrastructure.model_cache import model_cache

            # Embed off the event loop via the model_cache async boundary.
            embedding = await model_cache.aembed_query(runbook.content)
            if embedding is None:
                logger.error("BGE-M3 model unavailable, skipping runbook indexing")
                return

            documents = [
                {
                    "id": runbook.report_id,
                    "content": runbook.content,
                    "metadata": chroma_metadata,
                }
            ]

            await self.vector_store.add_documents(documents, embeddings=[embedding])

            logger.info(
                f"Indexed runbook for similarity search",
                extra={
                    "report_id": runbook.report_id,
                    "source": source.value,
                    "domain": final_domain,
                    "tags": final_tags,
                },
            )

        await self.call_external(
            operation_name="index_runbook",
            call_func=_index_wrapper,
            timeout=10.0,
            retries=2,
        )

    async def index_document_derived_runbook(
        self,
        runbook_content: str,
        document_title: str,
        domain: str,
        tags: List[str],
        *,
        organization_id: str,
        original_document_id: Optional[str] = None,
    ) -> str:
        """
        Convenience method for indexing document-driven runbooks.

        Called from knowledge base ingestion flow when processing
        user-uploaded operational documentation. Delegates to
        :meth:`index_runbook`, which carries the fail-closed tenant stamp.

        Args:
            runbook_content: Full runbook markdown content
            document_title: Title of source document
            domain: Technology domain
            tags: Classification tags
            organization_id: Owning tenant; REQUIRED
            original_document_id: Optional reference to uploaded document

        Returns:
            runbook_id for reference
        """
        import uuid

        # Create runbook record
        runbook_id = str(uuid.uuid4())
        runbook = CaseReport(
            report_id=runbook_id,
            case_id="doc-derived",  # Special marker for document-derived
            report_type=ReportType.RUNBOOK,
            title=f"Runbook: {document_title}",
            content=runbook_content,
            format="markdown",
            generation_status=ReportStatus.COMPLETED,
            generated_at=to_json_compatible(datetime.now(timezone.utc)),
            generation_time_ms=0,  # Pre-generated from documentation
            is_current=True,
            version=1,
            linked_to_closure=False,  # Not tied to any case closure
            metadata=RunbookMetadata(
                source=RunbookSource.DOCUMENT_DRIVEN,
                document_title=document_title,
                original_document_id=original_document_id,
                domain=domain,
                tags=tags,
            ),
        )

        # Index for similarity search
        await self.index_runbook(
            runbook=runbook,
            organization_id=organization_id,
            source=RunbookSource.DOCUMENT_DRIVEN,
            case_title=document_title,
            domain=domain,
            tags=tags,
        )

        logger.info(
            f"Indexed document-derived runbook",
            extra={
                "runbook_id": runbook_id,
                "document_title": document_title,
                "domain": domain,
            },
        )

        return runbook_id
