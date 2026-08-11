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
from faultmaven.infrastructure.embedding_guard import embed_query_or_raise
from faultmaven.infrastructure.persistence.chromadb_store import ChromaDBVectorStore
from faultmaven.models.exceptions import KnowledgeBaseError
from faultmaven.models.report import (
    CaseReport,
    ReportStatus,
    ReportType,
    RunbookMetadata,
    RunbookSource,
    SimilarRunbook,
)
from faultmaven.models.vector_metadata import VectorMetadata
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

    async def search_by_text(
        self,
        query_text: str,
        *,
        organization_id: str,
        filters: Optional[Dict[str, Any]] = None,
        top_k: int = 5,
        min_similarity: float = MIN_SIMILARITY_THRESHOLD,
    ) -> List[SimilarRunbook]:
        """Find runbooks similar to ``query_text``, scoped to one tenant.

        The text entry point to :meth:`search_runbooks`: embeds the query with
        BGE-M3 (off the event loop) and delegates. This exists because BOTH
        dedup callers had text, not a vector, and neither could get one:

        - ``ReportRecommendationService`` shipped a placeholder that returned
          ``[]`` as the "embedding". ChromaDB rejects a 0-dim vector, the error
          was swallowed, and the recommendation was permanently "generate".
        - ``terminal_transitions`` probed ``hasattr(runbook_kb,
          "search_by_text")`` — which was False, because this method did not
          exist — then fell through to ``search_runbooks(query_text=...)``,
          a signature mismatch that raised ``TypeError`` into an
          ``except`` that returned ``[]``.

        Both therefore concluded "no similar runbook exists" from a search that
        never ran, and every resolution proposed a new runbook regardless of
        what the KB already held (#944). One implementation now serves both, so
        the refusal semantics cannot drift apart again.

        Raises:
            KnowledgeBaseError: If the embedding model is unavailable or its
                load exceeds ``EMBED_TIMEOUT_SECONDS``; also anything
                :meth:`search_runbooks` raises, which this method delegates to
                and does not catch (``RUNBOOK_SEARCH_UNSCOPED``,
                ``RUNBOOK_SEARCH_FAILED``, ``RUNBOOK_RESULTS_UNREADABLE``). The
                caller must NOT render any of them as "no similar runbooks
                found" — that is the affirmative negative this method exists to
                end.
        """
        # Refuse an unscoped search HERE rather than relying on
        # search_runbooks' falsy-org guard. That guard fails closed by
        # returning [] without querying — correct for tenancy, but every
        # caller of this method reads [] as "the KB holds nothing similar"
        # and turns it into action="generate". A refused search must not be
        # reportable as a finding (#944).
        if not organization_id:
            logger.warning(
                "Refusing unscoped runbook search: no organization_id supplied"
            )
            raise KnowledgeBaseError(
                "Runbook similarity search requires a tenant; refusing to "
                "search without one",
                error_code="RUNBOOK_SEARCH_UNSCOPED",
            )

        # The shared guard, not a fourth hand-rolled copy of it. #944 gave this
        # site the right refusal but its own implementation, which meant it
        # also missed the time bound. Both callers sit under a caller-side
        # deadline they can exhaust: the runbook dedup in terminal_transitions
        # runs inside the 120s per-turn budget, and the report-recommendation
        # endpoint under the HTTP/ingress budget. A cold BGE-M3 load is 60-120s,
        # so an unbounded await here consumes that whole budget instead of
        # refusing in 10s.
        #
        # Consequence, accepted: a terminal transition during a cold start
        # refuses, and the resolution carries the "dedup could not run" caveat
        # rather than blocking on the load. The timed-out load does run to
        # completion and warm the cache, so the refusals stop once it finishes
        # — but not only the first is affected: ``get_bge_m3_model`` loads under
        # a ``threading.Lock``, so every embed attempt arriving during the
        # window blocks on that lock and exhausts its own 10s bound too.
        query_embedding = await embed_query_or_raise(
            query_text,
            subject="Runbook similarity search",
            operation="runbook_search_by_text",
            log=logger,
        )

        return await self.search_runbooks(
            query_embedding=query_embedding,
            organization_id=organization_id,
            filters=filters,
            top_k=top_k,
            min_similarity=min_similarity,
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

        Raises:
            KnowledgeBaseError: ``RUNBOOK_SEARCH_FAILED`` if the vector query
                itself failed, or ``RUNBOOK_RESULTS_UNREADABLE`` if rows matched
                but none carried a usable identity. Neither may be rendered as
                "no similar runbooks found": callers turn an empty list into
                "generate a new runbook", so a search that could not be
                completed or could not be read must not answer as one that was
                (#944).
        """
        if not organization_id:
            logger.warning(
                "Refusing unscoped runbook search: no organization_id supplied"
            )
            return []

        async def _query():
            """The external call, and ONLY the external call.

            Result parsing used to live in here too. It must not: parsing is
            pure CPU that cannot fail transiently, but everything inside this
            function runs under ``call_external``'s retry-and-circuit-breaker
            policy. A deterministic parse failure was therefore retried three
            times with backoff and recorded as three RunbookKB failures — and
            five of those open a breaker shared with ``index_runbook``, so an
            unreadable row would have blocked the very re-index that repairs
            it. Retries belong to the query; the parse gets none.
            """
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

            # Query vector database. A failure here is NOT "no similar
            # runbooks" — returning [] made every caller conclude the KB holds
            # nothing similar and propose a new runbook, so duplicates
            # accumulated on every resolution (#944). Surfaced as a typed
            # error so a caller that wants to tolerate it must say so.
            try:
                results = await self.vector_store.query_by_embedding(
                    query_embedding=query_embedding, where=where_clause, top_k=top_k
                )
            except Exception as e:
                logger.error(f"ChromaDB runbook query failed: {e}")
                raise KnowledgeBaseError(
                    f"Runbook similarity search failed: {e}",
                    error_code="RUNBOOK_SEARCH_FAILED",
                ) from e

            return results

        results = await self.call_external(
            operation_name="search_runbooks",
            call_func=_query,
            timeout=5.0,  # 5 seconds for vector search
            retries=2,
        )

        def _parse(results) -> List[SimilarRunbook]:
            # Parse results and filter by minimum similarity
            similar_runbooks: List[SimilarRunbook] = []
            # Candidates that cleared the similarity threshold and could NOT be
            # turned into a result — for any reason: a missing identity key, or
            # a stored row that no longer satisfies ``CaseReport``. Both are
            # tracked the same way on purpose. The caller does not consume the
            # list, it consumes the TOP match, so what makes an answer
            # trustworthy is not "did we read most rows" but "did we read the
            # best one". A candidate we could not read might have been the
            # duplicate, and reporting the runner-up's score as the best
            # available match is a false negative dressed as a measurement
            # (#944).
            unreadable = 0
            best_unreadable = 0.0

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

                # Reconstruct CaseReport from stored data.
                #
                # The identity keys are read with ``[]``, not ``.get(default)``.
                # ``index_runbook`` stamps all of them on every row it writes,
                # so a row missing one was not written by this path and cannot
                # be reconstructed — and the defaults that used to stand in for
                # them did not read as "missing", they read as facts:
                # ``case_id="unknown"`` and ``case_title="Unknown"`` for every
                # row this path wrote, because normalization dropped the real
                # values before ChromaDB ever saw them (#912). The handler below
                # turns a missing key into a logged skip, which is the honest
                # answer.
                # Identity is resolved FIRST, catching absence and
                # uninterpretability together: a missing ``runbook_source`` and
                # a ``runbook_source="manual"`` both mean "not written by this
                # path", and both must count towards ``unreadable``.
                #
                # Since every unreadable candidate now counts regardless of
                # cause, this split is about the LOG LINE, not the arithmetic —
                # the identity case can name which stamps the row carries,
                # which a bare ``CaseReport`` validation error cannot. Narrowing
                # it back to ``except KeyError`` is an equivalent mutation
                # today; it stops being one the moment the two branches diverge
                # again, which is why they stay explicit.
                #
                # No defaults anywhere here. The ones that used to stand in did
                # not read as "missing", they read as facts: ``case_id="unknown"``
                # and ``case_title="Unknown"`` for every row this path wrote, and
                # a ``"incident_driven"`` fallback that labelled document-driven
                # runbooks as incident-driven (#912).
                try:
                    identity_case_id = metadata["case_id"]
                    identity_case_title = metadata["case_title"]
                    identity_source = RunbookSource(metadata["runbook_source"])
                except (KeyError, ValueError) as e:
                    unreadable += 1
                    best_unreadable = max(best_unreadable, similarity)
                    logger.warning(
                        f"Runbook {report_id} carries no usable identity "
                        f"({e!r}), skipping it",
                        extra={
                            "report_id": report_id,
                            "similarity": similarity,
                            "metadata_keys": sorted(metadata),
                        },
                    )
                    continue

                try:
                    runbook = CaseReport(
                        report_id=report_id,
                        case_id=identity_case_id,
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
                            source=identity_source,
                            domain=metadata.get("domain", "general"),
                            tags=(
                                metadata.get("tags", "").split(",")
                                if metadata.get("tags")
                                else []
                            ),
                            document_title=metadata.get("document_title"),
                            original_document_id=metadata.get("original_document_id"),
                            case_context=None,  # Not reconstructed from search
                        ),
                    )

                    similar_runbook = SimilarRunbook(
                        runbook=runbook,
                        similarity_score=similarity,
                        case_title=identity_case_title,
                        case_id=identity_case_id,
                    )

                    similar_runbooks.append(similar_runbook)

                except Exception as e:
                    # Any other reconstruction failure — a stored row that no
                    # longer satisfies ``CaseReport``, say. Counted exactly like
                    # a missing identity key above: these rows ARE runbooks this
                    # path wrote, which makes them MORE likely to be the
                    # duplicate the caller is asking about, not less. Skipping
                    # them quietly and answering with the runner-up is the same
                    # false negative either way, so the reason we could not read
                    # a candidate does not change what it costs.
                    unreadable += 1
                    best_unreadable = max(best_unreadable, similarity)
                    logger.warning(
                        f"Failed to reconstruct runbook {report_id}, skipping it: {e!r}",
                        extra={"report_id": report_id, "similarity": similarity},
                    )
                    continue

            # Sort by similarity score descending
            similar_runbooks.sort(key=lambda x: x.similarity_score, reverse=True)

            # Refuse when an unread candidate could have been the answer.
            #
            # "Some rows were skipped" is not by itself a reason to fail — a
            # skipped row that scores below a match we DID read cannot change
            # the verdict, because every caller consumes the top match and
            # nothing else. What is not survivable is an unread candidate
            # scoring STRICTLY ABOVE the best readable one: the caller then gets
            # a similarity number presented as the best available, computed from
            # a set whose actual best was discarded. Both dedup callers turn
            # that into "generate a new runbook", stating a fact about the KB's
            # contents that the search did not establish — the #944 collapse,
            # reached through the partial case rather than the empty one.
            #
            # A TIE is deliberately not a refusal. Every caller decides on the
            # top score, so at equal similarity the readable row produces the
            # same verdict and names a runbook that really does exist at that
            # score — nothing in the answer is unestablished. Refusing there
            # would fail a correct answer over a row that could not have
            # changed it.
            #
            # The empty case needs no branch of its own: the sentinel makes
            # "nothing readable" the degenerate instance of the same rule, where
            # any unread candidate is strictly the best.
            best_readable = (
                similar_runbooks[0].similarity_score if similar_runbooks else -1.0
            )
            if unreadable and best_unreadable > best_readable:
                # States only what is established. ``unreadable`` counts
                # candidates that MET the threshold and could not be read — not
                # the number of rows matched, since rows read successfully and
                # then correctly discarded as dissimilar are neither.
                raise KnowledgeBaseError(
                    f"{unreadable} runbook(s) scoring up to "
                    f"{best_unreadable:.0%} met the similarity threshold but "
                    f"could not be read, outranking every readable match; "
                    f"refusing to report a best match from a result set whose "
                    f"strongest candidate was unreadable. Re-index the affected "
                    f"rows — retrying the search cannot clear this.",
                    error_code="RUNBOOK_RESULTS_UNREADABLE",
                )

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

        return _parse(results)

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

        # Extract metadata
        metadata_obj = runbook.metadata
        final_domain = domain or (metadata_obj.domain if metadata_obj else "general")
        final_tags = tags or (metadata_obj.tags if metadata_obj else [])
        final_case_title = case_title or runbook.title

        # Build metadata dict for ChromaDB.
        #
        # Every key here must be declared by ``VectorMetadata``, which
        # ``ChromaDBVectorStore.add_documents`` normalizes through — it stores
        # the keys it declares and refuses the ones it does not, rather than
        # dropping them silently as it did when the identity keys below were
        # being discarded on every write (#912).
        #
        # ``report_id`` is deliberately absent: it is the row id (see
        # ``documents`` below), which is where ``search_runbooks`` reads it
        # from. Writing it here as well would store the same fact twice.
        chroma_metadata = {
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

        # Refused HERE, outside ``call_external``, and not left to the identical
        # check inside ``add_documents``. That one runs inside THIS method's
        # retry wrapper, so an undeclared key would be retried three times with
        # backoff and recorded as three RunbookKB service failures — five such
        # calls open RunbookKB's circuit breaker, which is shared with
        # ``search_runbooks``/``search_by_text``. A deterministic programming
        # error in the write path would then take the read path down with it.
        VectorMetadata.reject_undeclared_keys(chroma_metadata)

        async def _index_wrapper():
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
