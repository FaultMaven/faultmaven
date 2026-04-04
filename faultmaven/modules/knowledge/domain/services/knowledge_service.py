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

import asyncio
import hashlib
import json
import logging
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

from faultmaven.exceptions import ServiceException, ValidationException
from faultmaven.models import KnowledgeBaseDocument, SearchResult
from faultmaven.models.interfaces import (
    IKnowledgeIngester,
    ILLMProvider,
    IMemoryService,
    ISanitizer,
    ITracer,
    IVectorStore,
)
from faultmaven.models.vector_metadata import VectorMetadata
from faultmaven.utils.serialization import to_json_compatible

logger = logging.getLogger(__name__)

# Import enhanced components if available
try:
    from faultmaven.core.knowledge.advanced_retrieval import (
        AdvancedKnowledgeRetrieval,
        RetrievalContext,
        RetrievalResult,
    )

    ENHANCED_RETRIEVAL_AVAILABLE = True
except ImportError:
    ENHANCED_RETRIEVAL_AVAILABLE = False


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
        memory_service: Optional[
            IMemoryService
        ] = None,  # Enhanced: Memory service for context
        llm_provider: Optional[
            ILLMProvider
        ] = None,  # Enhanced: LLM for intelligent processing
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
            memory_service: Optional memory service for enhanced context-aware search
            llm_provider: Optional LLM for intelligent query processing
        """
        self._ingester = knowledge_ingester
        self._sanitizer = sanitizer
        self._tracer = tracer
        self._vector_store = vector_store
        self._redis = redis_client
        self._settings = settings

        # Enhanced capabilities
        self._memory = memory_service
        self._llm = llm_provider

        # Initialize advanced retrieval engine if available
        if ENHANCED_RETRIEVAL_AVAILABLE and vector_store:
            try:
                self._advanced_retrieval = AdvancedKnowledgeRetrieval(
                    vector_store=vector_store, memory_service=memory_service
                )
                self._enhanced_mode = True
            except Exception as e:
                logger.warning(f"Advanced retrieval initialization failed: {e}")
                self._advanced_retrieval = None
                self._enhanced_mode = False
        else:
            self._advanced_retrieval = None
            self._enhanced_mode = False

        # Advanced knowledge caching and optimization
        self._query_cache: Dict[str, Any] = {}  # Result caching
        self._user_patterns: Dict[str, Dict[str, Any]] = {}  # User behavior patterns
        self._frequent_query_cache: Dict[str, Any] = {}  # High-frequency query cache

        # _document_counter removed — upload_document() now uses UUID-based IDs

        # Redis key patterns for KB metadata and jobs
        self._kb_doc_key = "kb:doc:{document_id}"
        self._kb_docs_set = "kb:docs"
        self._kb_index_type = "kb:index:type:{document_type}"
        self._kb_index_tag = "kb:index:tag:{tag}"
        self._kb_job_key = "kb:job:{job_id}"
        self._kb_job_ttl = 86400  # 24 hours

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
            sanitized_content = self._sanitizer.sanitize(content)
            sanitized_title = self._sanitizer.sanitize(title)

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

            sanitized_query = self._sanitizer.sanitize(query)

            try:
                # Search via vector store interface if available
                if self._vector_store:
                    with self._tracer.trace("knowledge_vector_search"):
                        results = await self._vector_store.search(
                            sanitized_query, k=limit, filters=filters
                        )

                    # Convert to SearchResult models
                    search_results = []
                    for result in results:
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

    async def search_with_reasoning_context(
        self,
        query: str,
        session_id: str,
        reasoning_type: str = "diagnostic",
        context: Optional[Dict[str, Any]] = None,
        user_profile: Optional[Dict[str, Any]] = None,
        limit: int = 10,
    ) -> Dict[str, Any]:
        """Search knowledge base with full reasoning and memory context

        This method provides the main interface for enhanced knowledge search,
        integrating reasoning context, memory insights, and user patterns.

        Args:
            query: Search query
            session_id: Session identifier for memory context
            reasoning_type: Type of reasoning (diagnostic, analytical, strategic, creative)
            context: Additional context information
            user_profile: User profile for personalization
            limit: Maximum number of results to return

        Returns:
            Dictionary with enhanced search results and metadata

        Raises:
            ServiceException: When search fails
            ValidationException: When inputs are invalid
        """
        try:
            search_start = time.time()

            logger.info(
                f"Enhanced knowledge search: {query[:100]}... (type: {reasoning_type})"
            )

            # Validate inputs
            await self._validate_search_inputs(query, session_id, reasoning_type)

            # Check if enhanced mode is available
            if not self._enhanced_mode:
                # Fallback to regular search with enhanced result format
                regular_results = await self.search_knowledge(query, limit)

                return {
                    "query": query,
                    "reasoning_type": reasoning_type,
                    "results": [
                        {
                            "document_id": result.document_id,
                            "title": result.title,
                            "document_type": result.document_type,
                            "tags": result.tags,
                            "score": result.score,
                            "snippet": result.snippet,
                            "enhancement_metadata": {
                                "reasoning_type": reasoning_type,
                                "memory_enhanced": False,
                                "domain_contextualized": False,
                                "retrieval_strategy": "fallback_search",
                            },
                        }
                        for result in regular_results
                    ],
                    "total_found": len(regular_results),
                    "enhanced_query": query,
                    "retrieval_strategy": "fallback_search",
                    "confidence_score": 0.5,
                    "contextual_relevance": 0.5,
                    "reasoning_insights": [],
                    "knowledge_gaps": [],
                    "search_expansion_paths": [],
                    "curation_applied": {},
                    "performance_metrics": {
                        "search_time_ms": (time.time() - search_start) * 1000,
                        "memory_insights_used": 0,
                        "enhancement_count": 0,
                    },
                }

            # Sanitize query if sanitizer available
            sanitized_query = query
            if self._sanitizer:
                sanitized_query = self._sanitizer.sanitize(query)

            # Enhanced multi-level caching with optimization
            cache_key = self._generate_optimized_cache_key(
                sanitized_query, reasoning_type, context
            )
            cached_result = await self._check_optimized_cache(cache_key)
            if cached_result:
                logger.info(f"Optimized cache hit for query: {sanitized_query[:50]}...")
                return cached_result

            # Retrieve memory context for enhancement
            memory_insights = []
            domain_context = {}

            if self._memory:
                try:
                    conversation_context = await self._memory.retrieve_context(
                        session_id, sanitized_query
                    )
                    if hasattr(conversation_context, "relevant_insights"):
                        memory_insights = conversation_context.relevant_insights
                    if hasattr(conversation_context, "domain_context"):
                        domain_context = conversation_context.domain_context
                except Exception as e:
                    logger.warning(f"Memory context retrieval failed: {e}")

            # Create retrieval context
            retrieval_context = (
                RetrievalContext(
                    session_id=session_id,
                    query=sanitized_query,
                    user_profile=user_profile,
                    reasoning_type=reasoning_type,
                    memory_insights=memory_insights,
                    domain_context=domain_context,
                    urgency_level=(
                        context.get("urgency_level", "medium") if context else "medium"
                    ),
                    technical_constraints=(
                        context.get("technical_constraints", []) if context else []
                    ),
                )
                if ENHANCED_RETRIEVAL_AVAILABLE
                else None
            )

            # Execute optimized advanced retrieval with parallel processing
            retrieval_result = await self._execute_optimized_retrieval(
                retrieval_context or sanitized_query, limit
            )

            # Process and enhance results with parallel optimization
            enhanced_results, curated_results = (
                await self._process_and_enhance_results_parallel(
                    retrieval_result, retrieval_context, limit
                )
            )

            # Update user patterns for future optimization
            if user_profile:
                await self._update_user_patterns(
                    session_id, user_profile, retrieval_context, curated_results
                )

            # Cache results for future use
            final_results = {
                "query": sanitized_query,
                "reasoning_type": reasoning_type,
                "results": curated_results.get("documents", [])[:limit],
                "total_found": len(curated_results.get("documents", [])),
                "enhanced_query": getattr(
                    retrieval_result, "enhanced_query", sanitized_query
                ),
                "retrieval_strategy": getattr(
                    retrieval_result, "retrieval_strategy", "basic_search"
                ),
                "confidence_score": getattr(retrieval_result, "confidence_score", 0.5),
                "contextual_relevance": getattr(
                    retrieval_result, "contextual_relevance", 0.5
                ),
                "reasoning_insights": getattr(
                    retrieval_result, "reasoning_insights", []
                ),
                "knowledge_gaps": getattr(retrieval_result, "knowledge_gaps", []),
                "search_expansion_paths": getattr(
                    retrieval_result, "search_expansion_paths", []
                ),
                "curation_applied": curated_results.get("curation_metadata", {}),
                "performance_metrics": {
                    "search_time_ms": (time.time() - search_start) * 1000,
                    "memory_insights_used": len(memory_insights),
                    "enhancement_count": len(
                        getattr(retrieval_result, "reasoning_insights", [])
                    ),
                },
            }

            await self._cache_search_results_optimized(cache_key, final_results)

            search_time = (time.time() - search_start) * 1000

            logger.info(
                f"Enhanced search completed in {search_time:.2f}ms with confidence {final_results['confidence_score']:.3f}"
            )

            return final_results

        except ValidationException:
            raise
        except Exception as e:
            logger.error(f"Enhanced knowledge search failed: {e}")
            raise ServiceException(f"Knowledge search failed: {str(e)}")

    async def discover_related_knowledge(
        self,
        document_id: str,
        session_id: str,
        exploration_depth: int = 2,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Discover related knowledge using semantic exploration

        Args:
            document_id: Source document for exploration
            session_id: Session identifier for context
            exploration_depth: Depth of knowledge graph exploration
            context: Additional context for exploration

        Returns:
            Dictionary with related knowledge discoveries
        """
        try:
            logger.info(f"Discovering related knowledge for document: {document_id}")

            if not self._vector_store:
                return {"related_documents": [], "knowledge_paths": []}

            # Get source document
            source_doc = await self._get_document_by_id(document_id)
            if not source_doc:
                return {"related_documents": [], "knowledge_paths": []}

            # Extract key concepts from source document
            key_concepts = await self._extract_key_concepts(source_doc)

            # Explore knowledge graph paths
            knowledge_paths = []
            related_documents = []

            for concept in key_concepts[:5]:  # Top 5 concepts
                # Search for related documents
                search_results = await self._vector_store.search(concept, k=3)

                for result in search_results:
                    if result.get("id") != document_id:  # Exclude source document
                        related_doc = {
                            "document_id": result.get("id"),
                            "content": result.get("content", "")[:200] + "...",
                            "metadata": result.get("metadata", {}),
                            "relevance_score": result.get("score", 0.0),
                            "connection_concept": concept,
                            "exploration_level": 1,
                        }
                        related_documents.append(related_doc)

                # Create knowledge path
                knowledge_paths.append(
                    {
                        "concept": concept,
                        "source_document": document_id,
                        "related_count": len(search_results),
                        "exploration_depth": 1,
                    }
                )

            # Remove duplicates and sort by relevance
            unique_docs = {}
            for doc in related_documents:
                doc_id = doc["document_id"]
                if (
                    doc_id not in unique_docs
                    or doc["relevance_score"] > unique_docs[doc_id]["relevance_score"]
                ):
                    unique_docs[doc_id] = doc

            sorted_related = sorted(
                unique_docs.values(), key=lambda x: x["relevance_score"], reverse=True
            )

            return {
                "source_document_id": document_id,
                "key_concepts": key_concepts,
                "related_documents": sorted_related[:10],  # Top 10 related
                "knowledge_paths": knowledge_paths,
                "exploration_metadata": {
                    "exploration_depth": exploration_depth,
                    "concepts_explored": len(key_concepts),
                    "total_connections": len(related_documents),
                },
            }

        except Exception as e:
            logger.error(f"Knowledge discovery failed: {e}")
            return {"related_documents": [], "knowledge_paths": []}

    async def curate_knowledge_for_reasoning(
        self,
        reasoning_type: str,
        session_id: str,
        topic_focus: Optional[str] = None,
        user_profile: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Curate knowledge specifically for a reasoning workflow

        Args:
            reasoning_type: Type of reasoning workflow
            session_id: Session identifier
            topic_focus: Optional topic to focus curation on
            user_profile: User profile for personalization

        Returns:
            Dictionary with curated knowledge for the reasoning type
        """
        try:
            logger.info(f"Curating knowledge for {reasoning_type} reasoning")

            # Define reasoning-specific knowledge requirements
            knowledge_requirements = {
                "diagnostic": {
                    "required_types": [
                        "error_patterns",
                        "troubleshooting_guides",
                        "solution_catalogs",
                    ],
                    "key_concepts": ["symptoms", "causes", "fixes", "validation"],
                    "search_terms": [
                        "error",
                        "problem",
                        "solution",
                        "troubleshoot",
                        "debug",
                    ],
                },
                "analytical": {
                    "required_types": [
                        "analysis_frameworks",
                        "pattern_guides",
                        "relationship_maps",
                    ],
                    "key_concepts": ["patterns", "relationships", "causes", "effects"],
                    "search_terms": [
                        "analysis",
                        "pattern",
                        "relationship",
                        "cause",
                        "effect",
                    ],
                },
                "strategic": {
                    "required_types": [
                        "planning_guides",
                        "strategy_frameworks",
                        "implementation_plans",
                    ],
                    "key_concepts": [
                        "planning",
                        "strategy",
                        "implementation",
                        "roadmap",
                    ],
                    "search_terms": [
                        "plan",
                        "strategy",
                        "approach",
                        "implementation",
                        "roadmap",
                    ],
                },
                "creative": {
                    "required_types": [
                        "innovation_patterns",
                        "alternative_approaches",
                        "creative_solutions",
                    ],
                    "key_concepts": [
                        "alternatives",
                        "innovation",
                        "creativity",
                        "novel",
                    ],
                    "search_terms": [
                        "alternative",
                        "innovative",
                        "creative",
                        "novel",
                        "different",
                    ],
                },
            }

            requirements = knowledge_requirements.get(
                reasoning_type, knowledge_requirements["diagnostic"]
            )

            # Build focused search query
            search_terms = requirements["search_terms"]
            if topic_focus:
                search_terms.append(topic_focus)

            focused_query = " ".join(search_terms)

            # Get memory context for personalization
            memory_context = {}
            if self._memory:
                try:
                    conversation_context = await self._memory.retrieve_context(
                        session_id, focused_query
                    )
                    memory_context = {
                        "insights": getattr(
                            conversation_context, "relevant_insights", []
                        ),
                        "domain": getattr(conversation_context, "domain_context", {}),
                    }
                except Exception as e:
                    logger.warning(f"Memory context retrieval failed: {e}")

            # Perform curated search
            curated_content = []

            if self._vector_store:
                # Search for each required concept
                for concept in requirements["key_concepts"]:
                    search_query = (
                        f"{concept} {topic_focus}" if topic_focus else concept
                    )

                    try:
                        results = await self._vector_store.search(search_query, k=3)
                        for result in results:
                            curated_item = {
                                "document_id": result.get("id"),
                                "content": result.get("content", ""),
                                "metadata": result.get("metadata", {}),
                                "relevance_score": result.get("score", 0.0),
                                "concept_alignment": concept,
                                "reasoning_type": reasoning_type,
                            }
                            curated_content.append(curated_item)
                    except Exception as e:
                        logger.warning(f"Search failed for concept {concept}: {e}")

            # Sort by relevance and remove duplicates
            unique_content = {}
            for item in curated_content:
                doc_id = item["document_id"]
                if (
                    doc_id not in unique_content
                    or item["relevance_score"]
                    > unique_content[doc_id]["relevance_score"]
                ):
                    unique_content[doc_id] = item

            sorted_content = sorted(
                unique_content.values(),
                key=lambda x: x["relevance_score"],
                reverse=True,
            )

            # Apply reasoning-specific scoring
            for item in sorted_content:
                reasoning_bonus = self._calculate_reasoning_alignment_score(
                    item, reasoning_type
                )
                item["reasoning_alignment_score"] = reasoning_bonus
                item["final_score"] = item["relevance_score"] * (1 + reasoning_bonus)

            # Re-sort by final score
            sorted_content.sort(key=lambda x: x["final_score"], reverse=True)

            return {
                "reasoning_type": reasoning_type,
                "topic_focus": topic_focus,
                "curated_content": sorted_content[:15],  # Top 15 curated items
                "curation_metadata": {
                    "required_types": requirements["required_types"],
                    "key_concepts": requirements["key_concepts"],
                    "memory_context_used": bool(memory_context),
                    "total_items_found": len(curated_content),
                    "unique_items": len(unique_content),
                },
                "reasoning_optimization": {
                    "concept_coverage": len(
                        [
                            item
                            for item in sorted_content
                            if item["reasoning_alignment_score"] > 0.5
                        ]
                    ),
                    "avg_alignment_score": (
                        sum(
                            item["reasoning_alignment_score"] for item in sorted_content
                        )
                        / len(sorted_content)
                        if sorted_content
                        else 0.0
                    ),
                },
            }

        except Exception as e:
            logger.error(f"Knowledge curation failed: {e}")
            return {"curated_content": [], "curation_metadata": {}}

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
                sanitized_title = self._sanitizer.sanitize(title)
                update_data["title"] = sanitized_title
                metadata["title"] = sanitized_title

            if content:
                sanitized_content = self._sanitizer.sanitize(content)
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
        """
        Archive (soft-delete) a document.

        Sets archived_at on the document and removes it from vector search.
        The document data is retained for referential integrity — past case
        transcripts that cite this document remain valid.

        Args:
            document_id: Document identifier

        Returns:
            Dict with success status and document_id

        Raises:
            ValidationException: If document_id is empty
            FileNotFoundError: If document not found
        """
        with self._tracer.trace("knowledge_service_delete_document"):
            logger.info(f"Archiving document {document_id}")

            if not document_id or not document_id.strip():
                raise ValueError("Document ID cannot be empty")

            try:
                from datetime import datetime, timezone

                # Verify document exists in ChromaDB (source of truth)
                doc_exists = False

                if self._vector_store and hasattr(self._vector_store, "get_document"):
                    chroma_doc = await self._vector_store.get_document(document_id)
                    if chroma_doc:
                        doc_exists = True

                if not doc_exists:
                    return {
                        "success": False,
                        "error": f"Document {document_id} not found",
                    }

                # Remove from ChromaDB (persistent store)
                if self._vector_store:
                    await self._remove_from_vector_store(document_id)

                # Clean up Redis cache
                try:
                    import json as _json

                    doc_key = self._kb_doc_key.format(document_id=document_id)
                    raw = await self._redis.hget(doc_key, "data")
                    if raw:
                        doc = _json.loads(raw)
                        archived_at = datetime.now(timezone.utc).isoformat()
                        doc["archived_at"] = archived_at
                        await self._redis.hset(doc_key, "data", _json.dumps(doc))

                        pipe = self._redis.pipeline()
                        pipe.srem(self._kb_docs_set, document_id)
                        if doc.get("document_type"):
                            pipe.srem(
                                self._kb_index_type.format(
                                    document_type=doc["document_type"]
                                ),
                                document_id,
                            )
                        for tag in doc.get("tags", []) or []:
                            pipe.srem(self._kb_index_tag.format(tag=tag), document_id)
                        await pipe.execute()
                except Exception as e:
                    logger.warning(f"Failed to clean up Redis for {document_id}: {e}")

                # Remove associated job if exists
                job_id = f"job_{document_id}"
                await self._redis.delete(self._kb_job_key.format(job_id=job_id))

                logger.info(f"Successfully archived document {document_id}")
                return {"success": True, "document_id": document_id}

            except ValidationException:
                raise
            except FileNotFoundError:
                raise
            except Exception as e:
                logger.error(f"Failed to archive document {document_id}: {e}")
                raise RuntimeError(f"Document archive failed: {str(e)}") from e

    async def get_document_statistics(self) -> Dict[str, Any]:
        """Get knowledge base statistics from ChromaDB (source of truth)."""
        with self._tracer.trace("knowledge_service_get_statistics"):
            try:
                documents_by_type: Dict[str, int] = {}
                tag_counts: Dict[str, int] = {}
                total_documents = 0

                if self._vector_store and hasattr(self._vector_store, "list_documents"):
                    raw_docs = await self._vector_store.list_documents(limit=500)

                    seen_ids: set = set()
                    for raw in raw_docs:
                        meta = raw.get("metadata", {})
                        doc_id = raw.get("id", meta.get("document_id", ""))
                        if doc_id in seen_ids:
                            continue
                        seen_ids.add(doc_id)

                        total_documents += 1
                        dtype = meta.get("document_type", "unknown")
                        documents_by_type[dtype] = documents_by_type.get(dtype, 0) + 1
                        raw_tags = meta.get("tags", "")
                        tag_list = (
                            [t.strip() for t in raw_tags.split(",") if t.strip()]
                            if isinstance(raw_tags, str)
                            else raw_tags if isinstance(raw_tags, list) else []
                        )
                        for tag in tag_list:
                            tag_counts[tag] = tag_counts.get(tag, 0) + 1

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
        self, document: KnowledgeBaseDocument
    ) -> int:
        """Index document in vector store with structure-aware chunking.

        Splits the document into semantically meaningful chunks, generates
        BGE-M3 embeddings for each, and stores them with enriched metadata.

        Returns:
            Number of chunks indexed (0 on failure).
        """
        if not self._vector_store:
            return 0

        try:
            from faultmaven.infrastructure.model_cache import model_cache
            from faultmaven.modules.knowledge.domain.services.content_chunker import (
                ContentChunker,
            )

            # Remove old chunks for this document (safe for re-ingestion)
            if hasattr(self._vector_store, "delete_documents_by_parent_id"):
                await self._vector_store.delete_documents_by_parent_id(
                    document.document_id
                )

            # Chunk the content
            chunker = ContentChunker()
            chunks = chunker.split(document.content)
            if not chunks:
                logger.warning(
                    f"No chunks produced for document {document.document_id}"
                )
                return 0

            # Generate BGE-M3 embeddings
            bge_model = model_cache.get_bge_m3_model()
            if bge_model is None:
                logger.error("BGE-M3 model unavailable, skipping vector indexing")
                return 0
            embeddings = [bge_model.encode(chunk).tolist() for chunk in chunks]

            # Extract RAG-enrichment fields from frontmatter
            fm_meta = self._extract_frontmatter_for_rag(document.content)

            # Build per-chunk document dicts
            doc_dicts = []
            for i, chunk in enumerate(chunks):
                meta = VectorMetadata(
                    title=document.title,
                    document_type=document.document_type,
                    tags=document.tags or [],
                    source_url=document.source_url,
                    scope=getattr(document, "scope", "global"),
                    owner_id=getattr(document, "owner_id", None),
                    team_id=getattr(document, "team_id", None),
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
        """Remove document and all its chunks from vector store."""
        if not self._vector_store:
            return

        try:
            # Try chunk-aware deletion first (new chunked documents)
            if hasattr(self._vector_store, "delete_documents_by_parent_id"):
                count = await self._vector_store.delete_documents_by_parent_id(
                    document_id
                )
                if count > 0:
                    logger.info(f"Removed {count} chunks for document {document_id}")
                    return

            # Fallback: try exact ID match (legacy unchunked documents)
            if hasattr(self._vector_store, "delete_documents"):
                await self._vector_store.delete_documents([document_id])
                logger.info(f"Removed document {document_id} from vector store")
        except Exception as e:
            logger.error(f"Failed to remove document from vector store: {e}")

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
        """Upload document - API-compatible wrapper that stores documents"""
        try:
            # Generate unique document ID (UUID-based to survive server restarts)
            import uuid as _uuid

            document_id = f"kb_{_uuid.uuid4().hex[:12]}"
            job_id = f"job_{document_id}"

            # Create document object
            created_at = datetime.now(timezone.utc)
            document_data = {
                "document_id": document_id,
                "title": title,
                "content": content,
                "document_type": document_type,
                "category": category or document_type,
                "tags": tags or [],
                "source_url": source_url,
                "description": description,
                "status": "completed",
                "created_at": to_json_compatible(created_at),
                "updated_at": to_json_compatible(created_at),
                "metadata": {
                    "author": "api-upload",
                    "version": "1.0",
                    "processing_status": "completed",
                },
            }

            # Persist metadata
            try:
                pipe = self._redis.pipeline()
                doc_key = self._kb_doc_key.format(document_id=document_id)
                import json as _json

                pipe.hset(doc_key, mapping={"data": _json.dumps(document_data)})
                pipe.sadd(self._kb_docs_set, document_id)
                pipe.sadd(
                    self._kb_index_type.format(document_type=document_type),
                    document_id,
                )
                for tag in tags or []:
                    pipe.sadd(self._kb_index_tag.format(tag=tag), document_id)
                await pipe.execute()
                logger.info(f"Persisted KB metadata in Redis for {document_id}")
            except Exception as e:
                logger.error(
                    f"Failed to persist KB metadata in Redis for {document_id}: {e}"
                )
                raise

            # Index into vector store with structure-aware chunking
            chunks_created = 0
            try:
                if self._vector_store:
                    doc_model = KnowledgeBaseDocument(
                        document_id=document_id,
                        title=title,
                        content=content,
                        document_type=document_type,
                        tags=tags or [],
                        source_url=source_url,
                        scope=scope,
                        owner_id=owner_id,
                        team_id=team_id,
                        created_at=to_json_compatible(created_at),
                        updated_at=to_json_compatible(created_at),
                    )
                    chunks_created = await self._index_document_in_vector_store(
                        doc_model
                    )
            except Exception as e:
                logger.error(f"Failed to index uploaded document {document_id}: {e}")

            # Create job record
            job_data = {
                "job_id": job_id,
                "document_id": document_id,
                "status": "completed",
                "progress": 100,
                "created_at": to_json_compatible(created_at),
                "completed_at": to_json_compatible(created_at),
                "processing_results": {
                    "chunks_created": chunks_created,
                    "embeddings_generated": chunks_created,
                    "indexing_complete": chunks_created > 0,
                    "error_count": 0,
                },
            }

            # Store job record in Redis
            import json as _json

            await self._redis.setex(
                self._kb_job_key.format(job_id=job_id),
                self._kb_job_ttl,
                _json.dumps(job_data),
            )

            logger.info(f"Successfully stored document {document_id} in Redis store")

            return {
                "document_id": document_id,
                "job_id": job_id,
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
        """List documents from ChromaDB (persistent) with RBAC filtering.

        ChromaDB is the source of truth for document listing. Redis is used
        only as a write-through cache for upload metadata, not for listing.

        Args:
            scope: Optional scope filter (global, team, personal). When set,
                   only documents matching this scope are returned (still
                   subject to RBAC — personal only shows your own, etc.).
            team_ids: List of team IDs the user belongs to (across all orgs).
                      Used for team scope RBAC filtering.
        """
        try:
            if not self._vector_store:
                logger.warning("No vector store available for list_documents")
                return {
                    "documents": [],
                    "total_count": 0,
                    "limit": limit,
                    "offset": offset,
                }

            # Build ChromaDB where clause for scope-based pre-filtering
            user_id = getattr(user, "user_id", None) if user else None
            user_team_ids = set(team_ids) if team_ids else set()

            # Query ChromaDB — fetch more than needed for post-filtering
            # ChromaDB doesn't support OR across scope filters, so we fetch all
            # and filter in Python (same as before, but from persistent store)
            where = None
            if document_type:
                where = {"document_type": document_type}

            raw_docs = await self._vector_store.list_documents(
                limit=500,  # Fetch enough for filtering
                offset=0,
                where=where,
            )

            # Deduplicate by document_id (chunked docs have multiple entries)
            seen_ids: set = set()
            all_documents: List[Dict[str, Any]] = []
            for raw in raw_docs:
                meta = raw.get("metadata", {})
                doc_id = meta.get("document_id") or raw.get("id", "")

                if doc_id in seen_ids:
                    continue
                seen_ids.add(doc_id)

                # Parse tags from comma-joined string back to list
                raw_tags = meta.get("tags", "")
                tag_list = (
                    [t.strip() for t in raw_tags.split(",") if t.strip()]
                    if isinstance(raw_tags, str)
                    else raw_tags if isinstance(raw_tags, list) else []
                )

                doc = {
                    "document_id": doc_id,
                    "title": meta.get("title", "Untitled"),
                    "content": raw.get("content", ""),
                    "document_type": meta.get("document_type", "unknown"),
                    "tags": tag_list,
                    "scope": meta.get("scope", "global"),
                    "owner_id": meta.get("owner_id"),
                    "team_id": meta.get("team_id"),
                    "source_url": meta.get("source_url"),
                    "created_at": meta.get("created_at", ""),
                    "updated_at": meta.get("updated_at", ""),
                    "metadata": meta,
                }
                all_documents.append(doc)

            # Apply RBAC first, then optional scope filter
            accessible_docs = []
            for doc in all_documents:
                doc_scope = doc.get("scope", "global")

                # RBAC: personal/team docs visible only to owner/members
                if doc_scope == "personal":
                    if not user_id or doc.get("owner_id") != user_id:
                        continue
                elif doc_scope == "team":
                    if not user_team_ids or doc.get("team_id") not in user_team_ids:
                        continue

                # Filter by tags
                if tags:
                    doc_tags = doc.get("tags", [])
                    if not any(tag in doc_tags for tag in tags):
                        continue

                accessible_docs.append(doc)

            # Scope counts (from all accessible docs, before scope filter)
            scope_counts = {"global": 0, "team": 0, "personal": 0}
            for doc in accessible_docs:
                s = doc.get("scope", "global")
                if s in scope_counts:
                    scope_counts[s] += 1

            # Apply explicit scope filter
            filtered_docs = (
                [d for d in accessible_docs if d.get("scope") == scope]
                if scope
                else accessible_docs
            )

            # Apply pagination
            total = len(filtered_docs)
            paginated_docs = filtered_docs[offset : offset + limit]

            logger.info(
                f"Listed {len(paginated_docs)} documents from ChromaDB (total: {total})"
            )

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
            logger.error(f"Failed to list documents from ChromaDB: {e}")
            return {
                "documents": [],
                "total_count": 0,
                "limit": limit,
                "offset": offset,
                "error": str(e),
            }

    async def get_document(self, document_id: str) -> Optional[Dict[str, Any]]:
        """Get a specific document by ID from ChromaDB (persistent store)."""
        try:
            if not document_id:
                return None

            # Try ChromaDB first (persistent, source of truth)
            if self._vector_store and hasattr(self._vector_store, "get_document"):
                raw = await self._vector_store.get_document(document_id)
                if raw:
                    meta = raw.get("metadata", {})
                    raw_tags = meta.get("tags", "")
                    tag_list = (
                        [t.strip() for t in raw_tags.split(",") if t.strip()]
                        if isinstance(raw_tags, str)
                        else raw_tags if isinstance(raw_tags, list) else []
                    )
                    return {
                        "document_id": document_id,
                        "title": meta.get("title", "Untitled"),
                        "content": raw.get("content", ""),
                        "document_type": meta.get("document_type", "unknown"),
                        "tags": tag_list,
                        "scope": meta.get("scope", "global"),
                        "owner_id": meta.get("owner_id"),
                        "team_id": meta.get("team_id"),
                        "source_url": meta.get("source_url"),
                        "created_at": meta.get("created_at", ""),
                        "updated_at": meta.get("updated_at", ""),
                        "metadata": meta,
                    }

            # Fallback to Redis cache
            if self._redis:
                import json as _json

                raw_data = await self._redis.hget(
                    self._kb_doc_key.format(document_id=document_id), "data"
                )
                if raw_data:
                    document = _json.loads(raw_data)
                    from faultmaven.api.v1.utils.parsing import normalize_tags_field

                    if "tags" in document:
                        document["tags"] = normalize_tags_field(document["tags"])
                    return document

            return None

        except Exception as e:
            logger.error(f"Failed to get document {document_id}: {e}")
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

    async def get_job_status(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Get processing job status from Redis."""
        try:
            import json as _json

            raw = await self._redis.get(self._kb_job_key.format(job_id=job_id))
            if raw:
                return _json.loads(raw)

            return None

        except Exception as e:
            logger.error(f"Failed to get job status {job_id}: {e}")
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
    ) -> Dict[str, Any]:
        """Search documents from ChromaDB with text scoring and RBAC filtering."""
        from faultmaven.api.v1.utils.parsing import normalize_tags_field

        try:
            # Get all documents from ChromaDB via list_documents (already RBAC-filtered)
            result = await self.list_documents(
                document_type=document_type,
                tags=tags,
                limit=500,
                offset=0,
                user=user,
            )
            filtered_docs = result.get("documents", [])

            # Simple text search within filtered documents
            scored_results = []
            query_lower = query.lower()

            for doc in filtered_docs:
                # Simple scoring based on query matches in title and content
                score = 0.0
                title = doc.get("title", "").lower()
                content = doc.get("content", "").lower()

                # Score based on query matches
                if query_lower in title:
                    score += 0.8
                if query_lower in content:
                    score += 0.6

                # Split query into words and check for partial matches
                query_words = query_lower.split()
                for word in query_words:
                    if word in title:
                        score += 0.3
                    if word in content:
                        score += 0.2

                # Apply similarity threshold filter
                if similarity_threshold is not None and score < similarity_threshold:
                    continue

                scored_results.append((doc, score))

            # Sort by score (or by rank_by field if specified)
            if rank_by and rank_by in ["priority"]:
                # Sort by priority field, then by score
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
                # Sort by score
                scored_results.sort(key=lambda x: x[1], reverse=True)

            # Limit results
            limited_results = scored_results[:limit]

            logger.info(
                f"Search query '{query}' returned {len(limited_results)} results"
            )

            return {
                "query": query,
                "total_results": len(limited_results),
                "results": [
                    {
                        "document_id": doc.get("document_id", "unknown"),
                        "content": doc.get("content", "")[:200] + "...",
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
            logger.error(f"Search failed: {e}")
            return {"query": query, "total_results": 0, "results": [], "error": str(e)}

    async def update_document_metadata(
        self, document_id: str, **kwargs
    ) -> Dict[str, Any]:
        """Update document metadata and/or content.

        Loads from ChromaDB (source of truth), applies updates, re-indexes
        if content changed, and updates Redis cache.
        """
        try:
            # Load current document from ChromaDB (source of truth)
            document = await self.get_document(document_id)
            if not document:
                logger.warning(f"Document {document_id} not found for update")
                return None

            # Apply updates
            if "title" in kwargs and kwargs["title"]:
                document["title"] = kwargs["title"]
            if "content" in kwargs and kwargs["content"]:
                document["content"] = kwargs["content"]
            if "tags" in kwargs:
                document["tags"] = kwargs["tags"] if kwargs["tags"] is not None else []
            if "document_type" in kwargs and kwargs["document_type"]:
                document["document_type"] = kwargs["document_type"]
            if "category" in kwargs and kwargs["category"]:
                document["category"] = kwargs["category"]
            if "version" in kwargs:
                if "metadata" not in document:
                    document["metadata"] = {}
                document["metadata"]["version"] = kwargs["version"]

            document["updated_at"] = to_json_compatible(datetime.now(timezone.utc))

            # Re-index in ChromaDB (add with same ID overwrites — atomic swap)
            content_changed = "content" in kwargs and kwargs["content"]
            if self._vector_store:
                doc_model = KnowledgeBaseDocument(
                    document_id=document_id,
                    title=document.get("title", ""),
                    content=document.get("content", ""),
                    document_type=document.get("document_type", "unknown"),
                    tags=document.get("tags", []),
                    source_url=document.get("source_url"),
                    scope=document.get("scope", "global"),
                    owner_id=document.get("owner_id"),
                    team_id=document.get("team_id"),
                    created_at=document.get("created_at", ""),
                    updated_at=document["updated_at"],
                )
                await self._index_document_in_vector_store(doc_model)

            # Update Redis cache
            try:
                import json as _json

                await self._redis.hset(
                    self._kb_doc_key.format(document_id=document_id),
                    "data",
                    _json.dumps(document),
                )
            except Exception as e:
                logger.warning(f"Failed to update Redis cache for {document_id}: {e}")

            logger.info(f"Successfully updated document {document_id}")

            return {
                "document_id": document_id,
                "title": document.get("title", ""),
                "content": document.get("content", ""),
                "document_type": document.get("document_type", ""),
                "category": document.get("category", ""),
                "tags": document.get("tags", []),
                "updated_at": document["updated_at"],
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

    # Enhanced helper methods for performance optimization and advanced functionality

    async def _validate_search_inputs(
        self, query: str, session_id: str, reasoning_type: str
    ) -> None:
        """Validate inputs for enhanced search methods"""
        if not query or not query.strip():
            raise ValidationException("Query cannot be empty")

        if not session_id or not session_id.strip():
            raise ValidationException("Session ID cannot be empty")

        valid_reasoning_types = ["diagnostic", "analytical", "strategic", "creative"]
        if reasoning_type not in valid_reasoning_types:
            raise ValidationException(
                f"Invalid reasoning type: {reasoning_type}. Must be one of: {valid_reasoning_types}"
            )

    def _generate_optimized_cache_key(
        self, query: str, reasoning_type: str, context: Optional[Dict[str, Any]]
    ) -> str:
        """Generate optimized cache key for search results"""
        key_components = [
            query.lower().strip(),
            reasoning_type,
            str(context.get("urgency_level", "medium")) if context else "medium",
            str(sorted(context.get("technical_constraints", []))) if context else "[]",
        ]

        key_string = "|".join(key_components)
        return hashlib.sha256(key_string.encode()).hexdigest()[:16]

    async def _check_optimized_cache(self, cache_key: str) -> Optional[Dict[str, Any]]:
        """Check optimized multi-level cache for results"""

        # Check frequent query cache first (hot cache)
        if cache_key in self._frequent_query_cache:
            result = self._frequent_query_cache[cache_key]

            # Check if result is still fresh (within 5 minutes for frequent queries)
            if time.time() - result.get("cached_at", 0) < 300:
                return result["data"]

        # Check regular query cache
        if cache_key in self._query_cache:
            result = self._query_cache[cache_key]

            # Check if result is still fresh (within 15 minutes)
            if time.time() - result.get("cached_at", 0) < 900:
                # Promote to frequent cache if accessed multiple times
                if result.get("access_count", 0) > 2:
                    self._frequent_query_cache[cache_key] = result

                result["access_count"] = result.get("access_count", 0) + 1
                return result["data"]
            else:
                # Remove stale cache entry
                del self._query_cache[cache_key]

        return None

    async def _cache_search_results_optimized(
        self, cache_key: str, results: Dict[str, Any]
    ) -> None:
        """Cache search results with optimization"""
        cache_entry = {"data": results, "cached_at": time.time(), "access_count": 1}

        # Store in regular cache
        self._query_cache[cache_key] = cache_entry

        # Cleanup old cache entries if cache is getting large
        if len(self._query_cache) > 1000:
            # Remove 20% of oldest entries
            sorted_entries = sorted(
                self._query_cache.items(), key=lambda x: x[1]["cached_at"]
            )

            entries_to_remove = int(len(sorted_entries) * 0.2)
            for key, _ in sorted_entries[:entries_to_remove]:
                del self._query_cache[key]

    async def _execute_optimized_retrieval(
        self, retrieval_context_or_query: Any, limit: int
    ) -> Any:
        """Execute optimized retrieval with performance enhancements"""

        if self._advanced_retrieval and ENHANCED_RETRIEVAL_AVAILABLE:
            # Use advanced retrieval engine
            try:
                return await self._advanced_retrieval.search_with_context(
                    retrieval_context_or_query, limit
                )
            except Exception as e:
                logger.warning(
                    f"Advanced retrieval failed: {e}, falling back to basic search"
                )

        # Fallback to basic vector search
        query = (
            retrieval_context_or_query.query
            if hasattr(retrieval_context_or_query, "query")
            else str(retrieval_context_or_query)
        )

        if self._vector_store:
            search_results = await self._vector_store.search(query, k=limit)

            # Convert to retrieval result format
            return type(
                "RetrievalResult",
                (),
                {
                    "documents": search_results,
                    "enhanced_query": query,
                    "retrieval_strategy": "fallback_vector_search",
                    "confidence_score": 0.5,
                    "contextual_relevance": 0.5,
                    "reasoning_insights": [],
                    "knowledge_gaps": [],
                    "search_expansion_paths": [],
                },
            )()

        return type(
            "RetrievalResult",
            (),
            {
                "documents": [],
                "enhanced_query": query,
                "retrieval_strategy": "no_vector_store",
                "confidence_score": 0.0,
                "contextual_relevance": 0.0,
                "reasoning_insights": [],
                "knowledge_gaps": [],
                "search_expansion_paths": [],
            },
        )()

    async def _process_and_enhance_results_parallel(
        self, retrieval_result: Any, retrieval_context: Optional[Any], limit: int
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """Process and enhance results using parallel optimization"""

        documents = getattr(retrieval_result, "documents", [])

        if not documents:
            return [], {"documents": [], "curation_metadata": {}}

        # Basic enhancement for documents
        enhanced_docs = []
        for doc in documents[:limit]:
            enhanced_doc = {
                "document_id": doc.get("id", doc.get("document_id", "unknown")),
                "title": doc.get("title", "Untitled"),
                "content": doc.get("content", ""),
                "metadata": doc.get("metadata", {}),
                "relevance_score": doc.get("score", 0.5),
                "enhancement_metadata": {
                    "reasoning_type": (
                        getattr(retrieval_context, "reasoning_type", "diagnostic")
                        if retrieval_context
                        else "diagnostic"
                    ),
                    "memory_enhanced": (
                        bool(getattr(retrieval_context, "memory_insights", []))
                        if retrieval_context
                        else False
                    ),
                    "domain_contextualized": (
                        bool(getattr(retrieval_context, "domain_context", {}))
                        if retrieval_context
                        else False
                    ),
                    "retrieval_strategy": getattr(
                        retrieval_result, "retrieval_strategy", "basic_search"
                    ),
                },
            }
            enhanced_docs.append(enhanced_doc)

        # Basic curation result
        curated_result = {
            "documents": enhanced_docs,
            "curation_metadata": {
                "total_processed": len(documents),
                "enhancement_applied": True,
                "parallel_processing": True,
            },
        }

        return enhanced_docs, curated_result

    async def _update_user_patterns(
        self,
        session_id: str,
        user_profile: Dict[str, Any],
        retrieval_context: Optional[Any],
        curated_results: Dict[str, Any],
    ) -> None:
        """Update user patterns for future optimization"""

        if session_id not in self._user_patterns:
            self._user_patterns[session_id] = {
                "query_patterns": defaultdict(int),
                "reasoning_preferences": defaultdict(int),
                "successful_queries": [],
                "timestamp": time.time(),
            }

        patterns = self._user_patterns[session_id]

        # Update query patterns
        if retrieval_context and hasattr(retrieval_context, "query"):
            patterns["query_patterns"][retrieval_context.query] += 1

        # Update reasoning preferences
        if retrieval_context and hasattr(retrieval_context, "reasoning_type"):
            patterns["reasoning_preferences"][retrieval_context.reasoning_type] += 1

        # Track successful queries
        if curated_results.get("documents"):
            patterns["successful_queries"].append(
                {
                    "query": (
                        getattr(retrieval_context, "query", "")
                        if retrieval_context
                        else ""
                    ),
                    "reasoning_type": (
                        getattr(retrieval_context, "reasoning_type", "")
                        if retrieval_context
                        else ""
                    ),
                    "result_count": len(curated_results["documents"]),
                    "timestamp": time.time(),
                }
            )

            # Keep only recent successful queries (last 100)
            patterns["successful_queries"] = patterns["successful_queries"][-100:]

    def _update_search_metrics(
        self, search_time_ms: float, confidence_score: float
    ) -> None:
        """Update search performance metrics"""
        self._metrics["enhanced_searches_performed"] += 1

        # Update running averages
        count = self._metrics["enhanced_searches_performed"]

        # Average search time
        current_avg_time = self._metrics["avg_search_time"]
        self._metrics["avg_search_time"] = (
            current_avg_time * (count - 1) + search_time_ms
        ) / count

        # Average relevance improvement (confidence score proxy)
        current_avg_relevance = self._metrics["avg_relevance_improvement"]
        self._metrics["avg_relevance_improvement"] = (
            current_avg_relevance * (count - 1) + confidence_score
        ) / count

    async def _get_document_by_id(self, document_id: str) -> Optional[Dict[str, Any]]:
        """Get document by ID from vector store or storage"""

        if self._vector_store:
            try:
                # Try to get document from vector store
                results = await self._vector_store.search(f"id:{document_id}", k=1)
                if results:
                    return results[0]
            except Exception as e:
                logger.warning(f"Failed to get document from vector store: {e}")

        # Fallback to metadata storage (Redis)
        try:
            doc_key = self._kb_doc_key.format(document_id=document_id)
            doc_data = await self._redis.get(doc_key)
            if doc_data:
                return json.loads(doc_data)
        except Exception as e:
            logger.warning(f"Failed to get document from metadata storage: {e}")

        return None

    async def _extract_key_concepts(self, document: Dict[str, Any]) -> List[str]:
        """Extract key concepts from a document"""

        content = document.get("content", "")
        if not content:
            return []

        # Simple keyword extraction (in a real implementation,
        # this might use NLP libraries or the LLM provider)
        words = content.lower().split()

        # Filter for meaningful terms (longer than 3 characters, not common words)
        common_words = {
            "the",
            "and",
            "or",
            "but",
            "in",
            "on",
            "at",
            "to",
            "for",
            "of",
            "with",
            "by",
            "from",
            "up",
            "about",
            "into",
            "through",
            "during",
            "before",
            "after",
            "above",
            "below",
            "between",
            "among",
            "this",
            "that",
            "these",
            "those",
            "they",
            "them",
            "their",
            "there",
            "here",
            "where",
            "when",
            "what",
            "which",
            "who",
            "whom",
            "whose",
            "why",
            "how",
        }

        concepts = []
        for word in words:
            if len(word) > 3 and word not in common_words:
                concepts.append(word)

        # Return top 10 most frequent concepts
        concept_counts = defaultdict(int)
        for concept in concepts:
            concept_counts[concept] += 1

        sorted_concepts = sorted(
            concept_counts.items(), key=lambda x: x[1], reverse=True
        )
        return [concept for concept, _ in sorted_concepts[:10]]

    def _calculate_reasoning_alignment_score(
        self, item: Dict[str, Any], reasoning_type: str
    ) -> float:
        """Calculate how well an item aligns with a reasoning type"""

        content = item.get("content", "").lower()
        concept = item.get("concept_alignment", "").lower()

        # Define alignment keywords for each reasoning type
        alignment_keywords = {
            "diagnostic": [
                "error",
                "problem",
                "issue",
                "bug",
                "failure",
                "troubleshoot",
                "debug",
                "solve",
            ],
            "analytical": [
                "analyze",
                "pattern",
                "relationship",
                "cause",
                "effect",
                "correlation",
                "trend",
            ],
            "strategic": [
                "plan",
                "strategy",
                "approach",
                "roadmap",
                "implementation",
                "goal",
                "objective",
            ],
            "creative": [
                "alternative",
                "innovative",
                "creative",
                "novel",
                "different",
                "unique",
                "breakthrough",
            ],
        }

        keywords = alignment_keywords.get(reasoning_type, [])

        # Count keyword matches
        matches = 0
        total_keywords = len(keywords)

        for keyword in keywords:
            if keyword in content or keyword in concept:
                matches += 1

        # Calculate alignment score (0.0 to 1.0)
        alignment_score = matches / total_keywords if total_keywords > 0 else 0.0

        # Boost score if concept directly matches reasoning type
        if reasoning_type.lower() in concept:
            alignment_score += 0.2

        return min(alignment_score, 1.0)

    async def _cleanup_stale_caches(self) -> None:
        """Clean up stale cache entries"""
        current_time = time.time()

        # Clean query cache (entries older than 1 hour)
        stale_keys = [
            key
            for key, value in self._query_cache.items()
            if current_time - value.get("cached_at", 0) > 3600
        ]
        for key in stale_keys:
            del self._query_cache[key]

        # Clean frequent query cache (entries older than 30 minutes)
        stale_keys = [
            key
            for key, value in self._frequent_query_cache.items()
            if current_time - value.get("cached_at", 0) > 1800
        ]
        for key in stale_keys:
            del self._frequent_query_cache[key]

        logger.debug(f"Cleaned up {len(stale_keys)} stale cache entries")


# Phase 4 Complete: Adapter classes have been removed as core components now implement interfaces directly
