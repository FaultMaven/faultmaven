"""
Answer From Document Tool - QA Sub-Agent for Session-Specific RAG

Implements the QA sub-agent pattern for Working Memory feature.
Uses CaseVectorStore for document retrieval and synthesis LLM for answer generation.

Architecture:
1. Retrieves relevant chunks from case-specific vector store
2. Uses dedicated synthesis LLM (separate from main agent)
3. Returns concise answer without polluting main agent context
4. Designed for detailed follow-up questions on uploaded documents

Example usage:
- User: "What does the configuration file say about timeouts?"
- Tool: Searches case_abc123 collection → retrieves config chunks
- Synthesis LLM: Generates answer from chunks
- Returns: "The timeout is set to 30 seconds in line 42..."

Configuration:
- Uses SYNTHESIS_PROVIDER from settings (defaults to CHAT_PROVIDER)
- Recommended: Fast, cost-effective models (gpt-4o-mini, claude-haiku, llama-3.1-8b)
- Token budget: ~2K for context, ~500 for answer
"""

from typing import Optional, Dict, Any
import logging

from langchain_core.tools import tool
from faultmaven.infrastructure.persistence.case_vector_store import CaseVectorStore
from faultmaven.infrastructure.llm.router import LLMRouter
from faultmaven.config.settings import get_settings


logger = logging.getLogger(__name__)


class AnswerFromDocumentTool:
    """
    QA sub-agent tool for answering questions from case-specific documents.

    Uses Session-Specific RAG with dedicated synthesis LLM.
    """

    def __init__(
        self,
        case_vector_store: CaseVectorStore,
        llm_router: LLMRouter
    ):
        """
        Initialize answer_from_document tool.

        Args:
            case_vector_store: Case-specific vector store
            llm_router: LLM router for synthesis calls
        """
        self.case_vector_store = case_vector_store
        self.llm_router = llm_router
        self.settings = get_settings()

    async def answer_question(
        self,
        case_id: str,
        question: str,
        k: int = 5
    ) -> Dict[str, Any]:
        """
        Answer a question using case-specific documents.

        Args:
            case_id: Case identifier
            question: User question
            k: Number of chunks to retrieve (default: 5)

        Returns:
            Dict with:
                - answer: Generated answer text
                - sources: List of source document IDs
                - chunk_count: Number of chunks used
                - confidence: Answer confidence (if available)
        """
        logger.info(f"Answering question for case {case_id}: {question[:100]}")

        # Step 1: Retrieve relevant chunks from case vector store
        try:
            chunks = await self.case_vector_store.search(
                case_id=case_id,
                query=question,
                k=k
            )

            if not chunks:
                return {
                    "answer": "I don't have any documents uploaded for this case yet. Please upload relevant files first.",
                    "sources": [],
                    "chunk_count": 0,
                    "confidence": 0.0
                }

            logger.debug(f"Retrieved {len(chunks)} chunks for synthesis")

        except Exception as e:
            logger.error(f"Error retrieving chunks: {e}")
            return {
                "answer": f"Error retrieving documents: {str(e)}",
                "sources": [],
                "chunk_count": 0,
                "confidence": 0.0
            }

        # Step 2: Build synthesis prompt
        context = self._build_context_from_chunks(chunks)
        synthesis_prompt = self._build_synthesis_prompt(question, context)

        # Step 3: Call synthesis LLM (uses SYNTHESIS_PROVIDER)
        try:
            # Get synthesis provider configuration
            synthesis_provider = self.settings.llm.get_synthesis_provider()
            synthesis_model = self.settings.llm.get_synthesis_model()

            logger.debug(
                f"Using synthesis provider: {synthesis_provider}, model: {synthesis_model}"
            )

            # Make LLM call via router
            response = await self.llm_router.call_llm(
                provider=synthesis_provider,
                model=synthesis_model,
                messages=[
                    {"role": "system", "content": self._get_system_prompt()},
                    {"role": "user", "content": synthesis_prompt}
                ],
                max_tokens=500,
                temperature=0.3  # Lower temperature for factual answers
            )

            answer = response.get("content", "").strip()

            # Extract source document IDs
            sources = list(set(chunk['metadata'].get('source_id', chunk['id']) for chunk in chunks))

            # Calculate confidence based on chunk scores
            avg_score = sum(chunk['score'] for chunk in chunks) / len(chunks)

            logger.info(
                f"Generated answer from {len(chunks)} chunks (avg score: {avg_score:.2f})",
                extra={"case_id": case_id, "chunk_count": len(chunks), "sources": len(sources)}
            )

            return {
                "answer": answer,
                "sources": sources,
                "chunk_count": len(chunks),
                "confidence": avg_score
            }

        except Exception as e:
            logger.error(f"Error calling synthesis LLM: {e}")
            return {
                "answer": f"Error generating answer: {str(e)}",
                "sources": [],
                "chunk_count": len(chunks),
                "confidence": 0.0
            }

    def _build_context_from_chunks(self, chunks: list) -> str:
        """
        Build context string from retrieved chunks.

        Args:
            chunks: List of chunk dicts from vector search

        Returns:
            Formatted context string
        """
        context_parts = []

        for i, chunk in enumerate(chunks, 1):
            metadata = chunk.get('metadata', {})
            source = metadata.get('filename', metadata.get('source_id', 'Unknown'))
            content = chunk['content']

            context_parts.append(
                f"[Chunk {i} - Source: {source}, Score: {chunk['score']:.2f}]\n{content}\n"
            )

        return "\n---\n".join(context_parts)

    def _build_synthesis_prompt(self, question: str, context: str) -> str:
        """
        Build synthesis prompt for LLM.

        Args:
            question: User question
            context: Retrieved context chunks

        Returns:
            Formatted prompt
        """
        return f"""Answer the following question using ONLY the provided context.

Question: {question}

Context from uploaded documents:
{context}

Instructions:
- Base your answer strictly on the provided context
- If the context doesn't contain enough information, say so
- Cite specific sources when possible (e.g., "According to [filename]...")
- Be concise but complete
- If multiple chunks contradict, mention the discrepancy

Answer:"""

    def _get_system_prompt(self) -> str:
        """Get system prompt for synthesis LLM"""
        return """You are a precise document analysis assistant. Your job is to answer questions based on provided document excerpts.

Guidelines:
- Only use information from the provided context
- Be factual and concise
- Cite sources when possible
- If information is missing, clearly state that
- Never make up information not in the context
- If chunks contradict each other, point that out"""


# LangChain tool wrapper for agent integration
@tool
async def answer_from_document(
    case_id: str,
    question: str,
    k: int = 5
) -> str:
    """
    Answer a question using case-specific uploaded documents.

    Use this tool when the user asks detailed questions about files they've uploaded
    in the current case. This tool searches through their documents and provides
    accurate answers based on the content.

    Args:
        case_id: Case identifier (from session context)
        question: The user's question about their documents
        k: Number of document chunks to retrieve (default: 5)

    Returns:
        Answer string with source citations
    """
    # This will be injected by the container/agent initialization
    # For now, this is a placeholder - actual implementation happens in container.py
    raise NotImplementedError(
        "answer_from_document tool must be initialized with CaseVectorStore and LLMRouter"
    )
