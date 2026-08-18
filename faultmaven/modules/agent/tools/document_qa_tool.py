"""
KB-Neutral Document Q&A Tool

Stateless document Q&A that works with ANY knowledge base via Strategy Pattern.
Adding new KB type = create new KBConfig, zero changes to this class.

Design: Design C (Stateless Sub-Agent + Proactive Phase Handlers)
- Sub-agent is purely factual, KB-neutral
- Investigation intelligence lives in main agent and phase handlers
- Strategy pattern enables extension without modification
"""

import logging
from typing import Any, Dict, Optional

from faultmaven.config.settings import get_settings
from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
    KnowledgeVectorStore,
)
from faultmaven.infrastructure.llm.router import LLMRouter
from faultmaven.modules.agent.tools.kb_config import KBConfig

logger = logging.getLogger(__name__)

# Output budget for one synthesis answer.
#
# Sized to the ceiling that actually binds, which is DOWNSTREAM of this call.
# The engine truncates any tool result to ``MilestoneEngine.TOOL_RESULT_MAX_CHARS``
# (8000) before it re-enters the model's context, and the kb_qa result carries
# ~590 characters of relay wrapper on top of this answer plus its own "Sources:"
# line. That leaves roughly 7.2K characters for the answer itself -- about 1850
# tokens at the 3.9-4.1 characters/token measured on real KB answers. 2000
# therefore already covers everything the pipeline can accept: raising it only
# buys generation time for text the engine then discards.
#
# What made this budget LOOK too small was not its size. On models that reason
# by default, hidden reasoning billed against this same budget: one observed
# call spent ~1950 of 2000 tokens reasoning and returned a 215-character stub
# while siblings on the same prompt returned 5-8KB. That competing claimant is
# removed in the OpenAI provider, by capping effort on plain chat calls. The
# budget itself is correct and stays put.
#
# Raise this only TOGETHER with TOOL_RESULT_MAX_CHARS -- on its own it is inert.
# ``test_kb_synthesis_budget.py`` pins the two in agreement so a future change
# to either one cannot silently drift out of step.
SYNTHESIS_MAX_TOKENS = 2000


class DocumentQATool:
    """
    KB-neutral stateless document Q&A tool.

    Works with ANY knowledge base type via injected KBConfig strategy.
    All KB-specific logic delegated to config - NO hardcoded KB types.

    Design principles:
    - Stateless: Returns factual answers, no investigation context
    - KB-neutral: Works with any KB via strategy pattern
    - Single responsibility: Document retrieval and factual synthesis only
    - Extensible: Add new KB = create config, zero changes here
    """

    name: str = "document_qa"  # Overridden by wrappers
    description: str = "Answer factual questions from documents"

    def __init__(
        self,
        vector_store: KnowledgeVectorStore,
        llm_router: LLMRouter,
        kb_config: KBConfig,
    ):
        """
        Initialize KB-neutral document Q&A tool.

        Args:
            vector_store: ChromaDB vector store instance
            llm_router: LLM router for synthesis calls
            kb_config: KB-specific configuration strategy
        """
        self._vector_store = vector_store
        self._llm_router = llm_router
        self._settings = get_settings()
        self._kb_config = kb_config  # Inject strategy

        logger.debug(f"Initialized DocumentQATool with {kb_config.__class__.__name__}")

    def _run(self, *args, **kwargs):
        """Synchronous run not supported"""
        raise NotImplementedError("Use async _arun instead")

    async def _arun(
        self,
        question: str,
        scope_id: Optional[str] = None,
        k: int = 5,
        filters: Optional[Dict[str, Any]] = None,
        context_metadata: Optional[Dict[str, str]] = None,
    ) -> str:
        """
        Answer factual question from documents (KB-neutral).

        Args:
            question: User's question
            scope_id: Scoping identifier (case_id, user_id, etc.) or None
            k: Number of chunks to retrieve
            filters: Optional ChromaDB metadata filters
            context_metadata: Optional case context (domain/service) for
                metadata-aware reranking. Applied as a soft boost only —
                aligned chunks score higher but nothing is filtered out.

        Returns:
            Formatted answer with citations
        """
        logger.info(f"Query: {question[:100]}, scope_id: {scope_id}")

        result = await self.answer_question(
            question, scope_id, k, filters, context_metadata
        )

        # Delegate response formatting to KB config
        return self._kb_config.format_response(
            result["answer"],
            result["sources"],
            result["chunk_count"],
            result["confidence"],
        )

    async def answer_question(
        self,
        question: str,
        scope_id: Optional[str],
        k: int,
        filters: Optional[Dict[str, Any]] = None,
        context_metadata: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """
        Core Q&A logic (KB-neutral).

        All KB-specific logic delegated to self._kb_config.

        Returns an answer only when one was actually produced. Retrieval and
        synthesis failures propagate rather than being rendered as an answer
        string, so "could not search" stays distinguishable from "searched and
        found nothing" (#943).

        Raises:
            KnowledgeBaseError: Retrieval could not be performed.
            Exception: Whatever the synthesis LLM call raises.
        """

        # Step 1: Get collection name from config (KB-specific)
        collection = self._kb_config.get_collection_name(scope_id)

        logger.debug(f"Querying collection: {collection}, k={k}")

        # Step 2: Retrieve chunks from vector store (same for all KBs).
        #
        # Retrieval failure is NOT caught here. Converting it into an `answer`
        # string is what made an unavailable knowledge base indistinguishable
        # from an empty one: the adapter above stamps success=True on whatever
        # string comes back, so the model was handed infrastructure failure
        # rendered as a substantive finding (#943). Letting it propagate is
        # what lets the adapter report success=False. A store-level fix alone
        # would merge inert against this handler.
        chunks = await self._dispatch_search(
            collection, question, k, filters, context_metadata
        )

        if not chunks:
            logger.info(f"No chunks found in collection: {collection}")
            # Reached ONLY when the search ran and matched nothing — a failed
            # search raised above. The message comes from the KB config so each
            # store states its own truth; this class does not know what
            # collection it just queried and must not guess at a cause.
            return {
                "answer": self._kb_config.empty_result_message,
                "sources": [],
                "chunk_count": 0,
                "confidence": 0.0,
            }

        logger.debug(f"Retrieved {len(chunks)} chunks")

        # Refuse synthesis if retrieval is at the noise floor. Without this,
        # the synthesizer grounds answers in whatever chunks came back —
        # which for an uncovered topic means citing off-topic runbooks that
        # share vocabulary (e.g. ZooKeeper query → Kafka chunks via "leader
        # election"). Opt-in per KB type via KBConfig.relevance_threshold.
        #
        # `score` here is ALWAYS the raw per-chunk cosine similarity, even in
        # hybrid mode: `_rerank` computes its four-signal composite to sort by
        # and never writes it back onto the candidate. So the keyword and
        # term-overlap legs reorder results but cannot lift one over this
        # floor — a query whose distinguishing term appears verbatim in a
        # chunk still clears it on embedding similarity alone (#1072). That is
        # deliberate: the threshold is calibrated in cosine, and a composite of
        # four heterogeneous signals is not a scale anyone has calibrated.
        threshold = self._kb_config.relevance_threshold
        if threshold is not None:
            max_score = max(chunk.get("score", 0.0) for chunk in chunks)
            if max_score < threshold:
                logger.info(
                    f"Refusing synthesis: max score {max_score:.3f} < "
                    f"threshold {threshold:.3f} (below relevance floor)"
                )
                return {
                    # Says only what a similarity score can support: these
                    # particular results were too weak to answer from. It does
                    # NOT claim "the KB does not cover this topic" — that is a
                    # fact about coverage, and a cosine floor is not evidence
                    # of it. The old wording asserted it, so a mis-scaled
                    # threshold did not merely withhold an answer, it handed
                    # the model a false statement about its own knowledge base
                    # and told it to stop asking (#1072, same rule as #943).
                    "answer": (
                        "The knowledge base was searched, and nothing it "
                        "returned was a close enough match to answer from "
                        "confidently. This does not establish whether the "
                        "knowledge base covers the topic — consider "
                        "rephrasing the query with different terms, or "
                        "answer from other available context (file "
                        "summaries, evidence, entity profile)."
                    ),
                    "sources": [],
                    "chunk_count": 0,
                    "confidence": 0.0,
                }

        # Step 3: Build context using config (KB-specific metadata formatting)
        context = self._build_context_from_chunks(chunks)

        # Step 4: Build synthesis prompt using config
        synthesis_prompt = f"""Answer the following question using ONLY the provided context.

Question: {question}

Context from documents:
{context}

Instructions:
- Answer based strictly on the provided context
- Preserve procedural detail — include full diagnostic steps, commands, and resolution procedures rather than summarizing them
- Cite sources accurately with {self._kb_config.get_citation_format()}
- If information is missing, state that clearly
- Compress only background context, never actionable steps

Answer:"""

        # Step 5: Call synthesis LLM with config's system prompt.
        # Failure propagates for the same reason as retrieval above: a tool
        # that did not answer must not report success (#943).
        synthesis_provider = self._settings.llm.get_synthesis_provider()
        synthesis_model = self._settings.llm.get_synthesis_model()

        logger.debug(f"Calling synthesis LLM: {synthesis_provider}/{synthesis_model}")

        response = await self._llm_router.route(
            model=synthesis_model,
            messages=[
                {"role": "system", "content": self._kb_config.system_prompt},
                {"role": "user", "content": synthesis_prompt},
            ],
            max_tokens=SYNTHESIS_MAX_TOKENS,
            temperature=0.3,  # Low temperature for factual accuracy
        )

        answer = response.content.strip()

        # Extract sources using config (KB-specific)
        sources = list(
            set(
                self._kb_config.extract_source_name(chunk["metadata"])
                for chunk in chunks
            )
        )

        avg_score = sum(chunk["score"] for chunk in chunks) / len(chunks)

        logger.info(
            f"Generated answer from {len(chunks)} chunks, avg score: {avg_score:.2f}"
        )

        return {
            "answer": answer,
            "sources": sources,
            "chunk_count": len(chunks),
            "confidence": avg_score,
        }

    async def _dispatch_search(
        self,
        collection: str,
        question: str,
        k: int,
        filters: Optional[Dict[str, Any]],
        context_metadata: Optional[Dict[str, str]] = None,
    ) -> list:
        """Retrieve chunks per the KB config's search mode.

        ``hybrid`` (when the store supports it) → vector + keyword + reranking;
        otherwise pure vector search. Raises on store error, and that error is
        NOT caught upstream: it propagates to the tool boundary so the adapter
        renders ``success=False`` rather than an answer (#943).

        ``context_metadata`` (case domain/service) only informs the hybrid
        reranker's metadata signal and is applied as a soft boost
        (``filter_mode="soft"``); pure vector search ignores it.
        """
        if self._kb_config.search_mode == "hybrid" and hasattr(
            self._vector_store, "hybrid_search"
        ):
            return await self._vector_store.hybrid_search(
                collection_name=collection,
                query=question,
                k=k,
                where=filters,
                context_metadata=context_metadata,
                filter_mode="soft",
            )
        return await self._vector_store.search(
            collection_name=collection, query=question, k=k, where=filters
        )

    def _build_context_from_chunks(self, chunks: list) -> str:
        """
        Build context string from chunks (KB-neutral).

        Delegates metadata formatting to KB config.
        """
        context_parts = []

        for i, chunk in enumerate(chunks, 1):
            metadata = chunk.get("metadata", {})
            content = chunk["content"]

            # Delegate metadata formatting to config (KB-specific)
            meta_str = self._kb_config.format_chunk_metadata(metadata, chunk["score"])

            context_parts.append(f"[Chunk {i} - {meta_str}]\n{content}\n")

        return "\n---\n".join(context_parts)

    @property
    def cache_ttl(self) -> int:
        """Get cache TTL from config"""
        return self._kb_config.cache_ttl
