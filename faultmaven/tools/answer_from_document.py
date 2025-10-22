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

from langchain.tools import BaseTool as LangChainBaseTool
from pydantic import PrivateAttr
from faultmaven.infrastructure.persistence.case_vector_store import CaseVectorStore
from faultmaven.infrastructure.llm.router import LLMRouter
from faultmaven.config.settings import get_settings
from faultmaven.models.interfaces import BaseTool as IBaseTool


logger = logging.getLogger(__name__)


class AnswerFromDocumentTool(LangChainBaseTool, IBaseTool):
    """
    QA sub-agent tool for answering questions from case-specific documents.

    Uses Session-Specific RAG with dedicated synthesis LLM.
    Implements LangChain BaseTool interface for agent integration.
    """

    name: str = "answer_from_document"
    description: str = """Answer detailed questions about files uploaded in this case.
Use this tool when the user asks specific questions about their uploaded logs, configs, or data.
This tool searches uploaded documents and returns precise answers with source citations.

Examples of when to use:
- "What's on line 42 of the config file?"
- "Show me all ERROR entries in the log"
- "Find timeout values in configuration"
- "When did errors start according to the logs?"

Note: You already receive high-level summaries when files are uploaded.
Use this tool for detailed forensic questions requiring line-by-line analysis.

Inputs:
- case_id: Case identifier (required)
- question: The user's question about their documents (required)
- k: Number of document chunks to retrieve (default: 5)
- investigation_phase: Current investigation phase for context (optional)
"""

    # Private attributes for dependencies (not part of Pydantic schema)
    _case_vector_store: CaseVectorStore = PrivateAttr()
    _llm_router: LLMRouter = PrivateAttr()
    _settings = PrivateAttr()

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
        LangChainBaseTool.__init__(self)
        IBaseTool.__init__(self)

        self._case_vector_store = case_vector_store
        self._llm_router = llm_router
        self._settings = get_settings()

    def _run(self, *args, **kwargs):
        """Synchronous run not supported"""
        raise NotImplementedError("Use async _arun instead")

    async def _arun(
        self,
        case_id: str,
        question: str,
        k: int = 5,
        investigation_phase: Optional[str] = None
    ) -> str:
        """
        LangChain tool invocation method.

        Args:
            case_id: Case identifier
            question: User question
            k: Number of chunks to retrieve
            investigation_phase: Current investigation phase

        Returns:
            Formatted answer string with citations
        """
        result = await self.answer_question(case_id, question, k, investigation_phase)

        # Format result for agent consumption
        answer = result["answer"]
        sources = result.get("sources", [])
        chunk_count = result.get("chunk_count", 0)
        confidence = result.get("confidence", 0.0)

        response = f"{answer}\n\n"
        if sources:
            response += f"Sources: {', '.join(sources[:3])}"  # Show top 3 sources
            if len(sources) > 3:
                response += f" (+{len(sources) - 3} more)"
            response += f" ({chunk_count} chunks, {confidence:.0%} avg relevance)"

        return response

    async def answer_question(
        self,
        case_id: str,
        question: str,
        k: int = 5,
        investigation_phase: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Answer a question using case-specific documents.

        Args:
            case_id: Case identifier
            question: User question
            k: Number of chunks to retrieve (default: 5)
            investigation_phase: Current investigation phase (blast_radius, timeline, hypothesis, validation, solution, document)

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
            chunks = await self._case_vector_store.search(
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

        # Step 2: Build synthesis prompt with phase awareness
        context = self._build_context_from_chunks(chunks)
        synthesis_prompt = self._build_synthesis_prompt(question, context, investigation_phase)

        # Step 3: Call synthesis LLM (uses SYNTHESIS_PROVIDER)
        try:
            # Get synthesis provider configuration
            synthesis_provider = self._settings.llm.get_synthesis_provider()
            synthesis_model = self._settings.llm.get_synthesis_model()

            logger.debug(
                f"Using synthesis provider: {synthesis_provider}, model: {synthesis_model}"
            )

            # Make LLM call via router with phase-aware system prompt
            response = await self._llm_router.call_llm(
                provider=synthesis_provider,
                model=synthesis_model,
                messages=[
                    {"role": "system", "content": self._get_system_prompt(investigation_phase)},
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
        Build context string from retrieved chunks with enhanced metadata.

        Args:
            chunks: List of chunk dicts from vector search

        Returns:
            Formatted context string with line numbers and timestamps when available
        """
        context_parts = []

        for i, chunk in enumerate(chunks, 1):
            metadata = chunk.get('metadata', {})
            source = metadata.get('filename', metadata.get('source_id', 'Unknown'))
            content = chunk['content']

            # Extract enhanced metadata if available
            line_number = metadata.get('line_number', '')
            timestamp = metadata.get('timestamp', '')

            # Build metadata header
            meta_info = [f"Source: {source}", f"Score: {chunk['score']:.2f}"]
            if line_number:
                meta_info.append(f"Line: {line_number}")
            if timestamp:
                meta_info.append(f"Time: {timestamp}")

            context_parts.append(
                f"[Chunk {i} - {', '.join(meta_info)}]\n{content}\n"
            )

        return "\n---\n".join(context_parts)

    def _build_synthesis_prompt(self, question: str, context: str, investigation_phase: Optional[str] = None) -> str:
        """
        Build synthesis prompt for LLM with phase-aware guidance.

        Args:
            question: User question
            context: Retrieved context chunks
            investigation_phase: Current investigation phase

        Returns:
            Formatted prompt with phase-specific instructions
        """
        phase_guidance = self._get_phase_specific_guidance(investigation_phase)

        return f"""Answer the following question using ONLY the provided context.

Question: {question}

Context from uploaded documents:
{context}

Instructions:
- Base your answer strictly on the provided context
- If the context doesn't contain enough information, say so
- Cite specific sources when possible (e.g., "According to [filename]...")
- Include line numbers and timestamps when available
- Be concise but complete
- If multiple chunks contradict, mention the discrepancy
{phase_guidance}

Answer:"""

    def _get_phase_specific_guidance(self, investigation_phase: Optional[str]) -> str:
        """
        Get phase-specific answer guidance.

        Args:
            investigation_phase: Current investigation phase

        Returns:
            Phase-specific instruction text
        """
        if not investigation_phase:
            return ""

        guidance_map = {
            "blast_radius": """
- Focus on: Scope, affected users/components, error rates, impact severity
- Prioritize: Statistics, counts, percentages, affected scope
- Example useful info: "500 errors in last hour", "affects 30% of users"
""",
            "timeline": """
- Focus on: When things happened, sequence of events, timestamps
- Prioritize: Chronological order, time correlations, event sequences
- Example useful info: "First error at 14:23", "spike started after deployment at 14:20"
""",
            "hypothesis": """
- Focus on: Potential root causes, error patterns, anomaly indicators
- Prioritize: Unusual patterns, correlations, root cause clues
- Example useful info: "database timeout errors", "memory leak pattern"
""",
            "validation": """
- Focus on: Evidence for/against theories, proof points, test results
- Prioritize: Confirmatory or contradictory evidence
- Example useful info: "config value confirms theory", "logs show no connection errors"
""",
            "solution": """
- Focus on: Fix details, remediation steps, verification data
- Prioritize: How solution was applied, success metrics
- Example useful info: "config changed from X to Y", "error rate dropped to 0"
""",
            "document": """
- Focus on: Comprehensive documentation, key insights, lessons learned
- Prioritize: Root cause summary, resolution steps, prevention measures
- Example useful info: Full timeline, validated hypothesis, solution applied
"""
        }

        return guidance_map.get(investigation_phase, "")

    def _get_system_prompt(self, investigation_phase: Optional[str] = None) -> str:
        """
        Get system prompt for synthesis LLM with optional phase awareness.

        Args:
            investigation_phase: Current investigation phase

        Returns:
            System prompt optimized for phase if provided
        """
        base_prompt = """You are a precise document analysis assistant helping with technical troubleshooting. Your job is to answer questions based on provided document excerpts.

Guidelines:
- Only use information from the provided context
- Be factual and concise
- Cite sources with line numbers/timestamps when available
- If information is missing, clearly state that
- Never make up information not in the context
- If chunks contradict each other, point that out"""

        if not investigation_phase:
            return base_prompt

        # Add phase-specific role enhancement
        phase_roles = {
            "blast_radius": "\n\nCurrent Focus: You're helping assess the SCOPE and IMPACT of an issue. Prioritize quantitative data (error rates, affected users, severity metrics).",
            "timeline": "\n\nCurrent Focus: You're helping establish a TIMELINE of events. Prioritize temporal information (timestamps, sequence, duration, correlation with changes).",
            "hypothesis": "\n\nCurrent Focus: You're helping identify potential ROOT CAUSES. Prioritize error patterns, anomalies, and indicators that suggest why something failed.",
            "validation": "\n\nCurrent Focus: You're helping VALIDATE or REFUTE hypotheses. Prioritize evidence that proves or disproves theories about the root cause.",
            "solution": "\n\nCurrent Focus: You're helping document the SOLUTION and its effectiveness. Prioritize fix details, verification data, and success metrics.",
            "document": "\n\nCurrent Focus: You're helping create comprehensive DOCUMENTATION. Provide thorough answers that capture key insights and lessons learned."
        }

        return base_prompt + phase_roles.get(investigation_phase, "")
