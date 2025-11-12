"""
KB-Neutral Document Q&A Tool

Stateless document Q&A that works with ANY knowledge base via Strategy Pattern.
Adding new KB type = create new KBConfig, zero changes to this class.

Design: Design C (Stateless Sub-Agent + Proactive Phase Handlers)
- Sub-agent is purely factual, KB-neutral
- Investigation intelligence lives in main agent and phase handlers
- Strategy pattern enables extension without modification
"""

from typing import Optional, Dict, Any
import logging

from langchain.tools import BaseTool as LangChainBaseTool
from pydantic import PrivateAttr

from faultmaven.tools.kb_config import KBConfig
from faultmaven.infrastructure.persistence.case_vector_store import CaseVectorStore
from faultmaven.infrastructure.llm.router import LLMRouter
from faultmaven.config.settings import get_settings


logger = logging.getLogger(__name__)


class DocumentQATool(LangChainBaseTool):
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

    # Private attributes (not part of Pydantic schema)
    _vector_store: CaseVectorStore = PrivateAttr()
    _llm_router: LLMRouter = PrivateAttr()
    _settings = PrivateAttr()
    _kb_config: KBConfig = PrivateAttr()  # Strategy pattern

    def __init__(
        self,
        vector_store: CaseVectorStore,
        llm_router: LLMRouter,
        kb_config: KBConfig
    ):
        """
        Initialize KB-neutral document Q&A tool.

        Args:
            vector_store: ChromaDB vector store instance
            llm_router: LLM router for synthesis calls
            kb_config: KB-specific configuration strategy
        """
        LangChainBaseTool.__init__(self)

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
        k: int = 5
    ) -> str:
        """
        Answer factual question from documents (KB-neutral).

        Args:
            question: User's question
            scope_id: Scoping identifier (case_id, user_id, etc.) or None
            k: Number of chunks to retrieve

        Returns:
            Formatted answer with citations
        """
        logger.info(f"Query: {question[:100]}, scope_id: {scope_id}")

        result = await self.answer_question(question, scope_id, k)

        # Delegate response formatting to KB config
        return self._kb_config.format_response(
            result["answer"],
            result["sources"],
            result["chunk_count"],
            result["confidence"]
        )

    async def answer_question(
        self,
        question: str,
        scope_id: Optional[str],
        k: int
    ) -> Dict[str, Any]:
        """
        Core Q&A logic (KB-neutral).

        All KB-specific logic delegated to self._kb_config.
        """

        # Step 1: Get collection name from config (KB-specific)
        collection = self._kb_config.get_collection_name(scope_id)

        logger.debug(f"Querying collection: {collection}, k={k}")

        # Step 2: Retrieve chunks from vector store (same for all KBs)
        try:
            chunks = await self._vector_store.search(
                collection_name=collection,
                query=question,
                k=k
            )
        except Exception as e:
            logger.error(f"Vector store search failed: {e}")
            return {
                "answer": f"Error retrieving documents: {str(e)}",
                "sources": [],
                "chunk_count": 0,
                "confidence": 0.0
            }

        if not chunks:
            logger.info(f"No chunks found in collection: {collection}")
            # This can happen if:
            # 1. No files uploaded yet
            # 2. Background vectorization still in progress
            # 3. Vectorization failed
            return {
                "answer": (
                    "No evidence documents are available for deep analysis yet. "
                    "This could mean:\n"
                    "- No files have been uploaded to this case\n"
                    "- Uploaded files are still being indexed (usually takes 5-15 seconds)\n"
                    "- Evidence indexing failed (check logs)\n\n"
                    "Note: You can still ask about the file summaries I received when the files were uploaded."
                ),
                "sources": [],
                "chunk_count": 0,
                "confidence": 0.0
            }

        logger.debug(f"Retrieved {len(chunks)} chunks")

        # Step 3: Build context using config (KB-specific metadata formatting)
        context = self._build_context_from_chunks(chunks)

        # Step 4: Build synthesis prompt using config
        synthesis_prompt = f"""Answer the following question using ONLY the provided context.

Question: {question}

Context from documents:
{context}

Instructions:
- Answer based strictly on the provided context
- Cite sources accurately with {self._kb_config.get_citation_format()}
- If information is missing, state that clearly
- Be concise and factual

Answer:"""

        # Step 5: Call synthesis LLM with config's system prompt
        try:
            synthesis_provider = self._settings.llm.get_synthesis_provider()
            synthesis_model = self._settings.llm.get_synthesis_model()

            logger.debug(
                f"Calling synthesis LLM: {synthesis_provider}/{synthesis_model}"
            )

            response = await self._llm_router.call_llm(
                provider=synthesis_provider,
                model=synthesis_model,
                messages=[
                    {"role": "system", "content": self._kb_config.system_prompt},
                    {"role": "user", "content": synthesis_prompt}
                ],
                max_tokens=500,
                temperature=0.3  # Low temperature for factual accuracy
            )

            answer = response.get("content", "").strip()

            # Extract sources using config (KB-specific)
            sources = list(set(
                self._kb_config.extract_source_name(chunk['metadata'])
                for chunk in chunks
            ))

            avg_score = sum(chunk['score'] for chunk in chunks) / len(chunks)

            logger.info(
                f"Generated answer from {len(chunks)} chunks, avg score: {avg_score:.2f}"
            )

            return {
                "answer": answer,
                "sources": sources,
                "chunk_count": len(chunks),
                "confidence": avg_score
            }

        except Exception as e:
            logger.error(f"Synthesis LLM call failed: {e}")
            return {
                "answer": f"Error generating answer: {str(e)}",
                "sources": [],
                "chunk_count": len(chunks),
                "confidence": 0.0
            }

    def _build_context_from_chunks(self, chunks: list) -> str:
        """
        Build context string from chunks (KB-neutral).

        Delegates metadata formatting to KB config.
        """
        context_parts = []

        for i, chunk in enumerate(chunks, 1):
            metadata = chunk.get('metadata', {})
            content = chunk['content']

            # Delegate metadata formatting to config (KB-specific)
            meta_str = self._kb_config.format_chunk_metadata(metadata, chunk['score'])

            context_parts.append(f"[Chunk {i} - {meta_str}]\n{content}\n")

        return "\n---\n".join(context_parts)

    @property
    def cache_ttl(self) -> int:
        """Get cache TTL from config"""
        return self._kb_config.cache_ttl
