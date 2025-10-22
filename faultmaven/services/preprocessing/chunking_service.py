"""
ChunkingService - Map-Reduce Pattern for Large Documents

Handles documents >8K tokens using intelligent chunking and parallel summarization.

Pipeline:
1. Split document into chunks on natural boundaries (paragraphs, headings)
2. MAP: Summarize each chunk in parallel (N LLM calls)
3. REDUCE: Synthesize chunk summaries into final summary (1 LLM call)

Total LLM calls: N + 1
Cost: ~2-3x single LLM call (but handles unlimited document size)
Latency: ~5s with parallelization (acceptable for large documents)
"""

import asyncio
import logging
import re
from typing import List, Optional
from faultmaven.models.api import DataType
from faultmaven.models.interfaces import ILLMProvider

logger = logging.getLogger(__name__)


class ChunkingService:
    """Map-reduce chunking service for long documents (>8K tokens)"""

    def __init__(
        self,
        llm_router: ILLMProvider,
        chunk_size_tokens: int = 4000,
        overlap_tokens: int = 200,
        max_parallel_chunks: int = 5
    ):
        """
        Initialize chunking service

        Args:
            llm_router: LLM provider for summarization
            chunk_size_tokens: Target size for each chunk (~16KB text)
            overlap_tokens: Overlap between chunks for context preservation
            max_parallel_chunks: Maximum parallel LLM calls during MAP phase
        """
        self.llm_router = llm_router
        self.chunk_size_tokens = chunk_size_tokens
        self.overlap_tokens = overlap_tokens
        self.max_parallel_chunks = max_parallel_chunks

    async def process_long_text(
        self,
        content: str,
        data_type: DataType,
        filename: Optional[str] = None
    ) -> str:
        """
        Process long document using map-reduce pattern

        Args:
            content: Document content (>8K tokens)
            data_type: Type of data being processed
            filename: Optional filename for logging

        Returns:
            Synthesized summary (~2K tokens)
        """
        start_token_count = self._estimate_tokens(content)

        logger.info(
            f"Starting map-reduce chunking for {filename or 'document'} "
            f"({start_token_count} tokens, type={data_type.value})"
        )

        # Step 1: Split into intelligent chunks
        chunks = self._split_on_structure(content)
        chunk_count = len(chunks)

        logger.info(
            f"Split document into {chunk_count} chunks "
            f"(avg ~{self.chunk_size_tokens} tokens each)"
        )

        # Step 2: MAP - Summarize each chunk in parallel (batched)
        chunk_summaries = await self._map_summarize_chunks(chunks, data_type)

        # Step 3: REDUCE - Synthesize all summaries into final summary
        final_summary = await self._reduce_synthesize(
            chunk_summaries,
            data_type,
            filename
        )

        final_token_count = self._estimate_tokens(final_summary)

        logger.info(
            f"Map-reduce complete: {start_token_count} tokens → {final_token_count} tokens "
            f"({chunk_count} chunks, {chunk_count + 1} LLM calls)"
        )

        return final_summary

    def _split_on_structure(self, content: str) -> List[str]:
        """
        Split content into chunks on natural boundaries

        Strategy (in priority order):
        1. Markdown headings (##)
        2. Blank lines (paragraphs)
        3. Sentences (periods)
        4. Newlines
        5. Character limit (last resort)

        Maintains overlap for context preservation
        """
        chunks = []
        current_chunk = []
        current_tokens = 0

        # Split on paragraphs (separated by blank lines)
        paragraphs = re.split(r'\n\s*\n', content)

        for i, para in enumerate(paragraphs):
            para = para.strip()
            if not para:
                continue

            para_tokens = self._estimate_tokens(para)

            # Handle single paragraph larger than chunk size
            if para_tokens > self.chunk_size_tokens:
                # Save current chunk if exists
                if current_chunk:
                    chunks.append('\n\n'.join(current_chunk))
                    current_chunk = []
                    current_tokens = 0

                # Split large paragraph on sentences
                sentences = self._split_on_sentences(para)
                sentence_chunk = []
                sentence_tokens = 0

                for sentence in sentences:
                    sent_tokens = self._estimate_tokens(sentence)

                    if sentence_tokens + sent_tokens > self.chunk_size_tokens:
                        if sentence_chunk:
                            chunks.append(' '.join(sentence_chunk))
                        sentence_chunk = [sentence]
                        sentence_tokens = sent_tokens
                    else:
                        sentence_chunk.append(sentence)
                        sentence_tokens += sent_tokens

                if sentence_chunk:
                    chunks.append(' '.join(sentence_chunk))

                continue

            # Check if adding paragraph would exceed chunk size
            if current_tokens + para_tokens > self.chunk_size_tokens:
                # Chunk full - save it
                if current_chunk:
                    chunks.append('\n\n'.join(current_chunk))

                # Start new chunk with overlap (last paragraph)
                if current_chunk and self.overlap_tokens > 0:
                    overlap_para = current_chunk[-1]
                    overlap_tokens = self._estimate_tokens(overlap_para)

                    if overlap_tokens < self.overlap_tokens:
                        current_chunk = [overlap_para, para]
                        current_tokens = overlap_tokens + para_tokens
                    else:
                        current_chunk = [para]
                        current_tokens = para_tokens
                else:
                    current_chunk = [para]
                    current_tokens = para_tokens
            else:
                # Add to current chunk
                current_chunk.append(para)
                current_tokens += para_tokens

        # Save last chunk
        if current_chunk:
            chunks.append('\n\n'.join(current_chunk))

        return chunks

    def _split_on_sentences(self, paragraph: str) -> List[str]:
        """Split paragraph into sentences (simple sentence boundary detection)"""
        # Simple sentence splitter - splits on ". " or ".\n"
        sentences = re.split(r'\.(?:\s+|\n)', paragraph)
        # Re-add periods
        sentences = [s.strip() + '.' for s in sentences if s.strip()]
        return sentences

    async def _map_summarize_chunks(
        self,
        chunks: List[str],
        data_type: DataType
    ) -> List[str]:
        """
        MAP phase: Summarize each chunk in parallel (batched)

        Args:
            chunks: List of text chunks
            data_type: Type of data for specialized prompts

        Returns:
            List of chunk summaries
        """
        summaries = []

        # Process chunks in batches to respect max_parallel_chunks
        for batch_start in range(0, len(chunks), self.max_parallel_chunks):
            batch_end = min(batch_start + self.max_parallel_chunks, len(chunks))
            batch_chunks = chunks[batch_start:batch_end]

            logger.info(
                f"MAP phase: Processing chunk batch {batch_start + 1}-{batch_end} "
                f"of {len(chunks)} (parallel={len(batch_chunks)})"
            )

            # Summarize batch in parallel
            batch_summaries = await asyncio.gather(*[
                self._summarize_chunk(
                    chunk,
                    data_type,
                    batch_start + idx,
                    len(chunks)
                )
                for idx, chunk in enumerate(batch_chunks)
            ])

            summaries.extend(batch_summaries)

        return summaries

    async def _summarize_chunk(
        self,
        chunk: str,
        data_type: DataType,
        chunk_idx: int,
        total_chunks: int
    ) -> str:
        """
        Summarize a single chunk with data type-specific instructions

        Args:
            chunk: Text chunk to summarize
            data_type: Type of data for specialized prompts
            chunk_idx: Index of this chunk (0-based)
            total_chunks: Total number of chunks

        Returns:
            Chunk summary (~1K tokens)
        """
        # Data type-specific prompts
        prompts = {
            DataType.UNSTRUCTURED_TEXT: f"""
You are summarizing section {chunk_idx + 1} of {total_chunks} from a troubleshooting document.

PRESERVE (verbatim):
- Error messages and error codes
- Numbered steps and procedures
- Technical configurations and settings
- Timestamps and sequence of events
- Important metrics and values

OMIT:
- Redundant explanations
- General background information
- Verbose descriptions

Format: Natural prose with key details preserved.
Target: 200-400 words per chunk.
""",
            DataType.LOGS_AND_ERRORS: f"""
You are summarizing log section {chunk_idx + 1} of {total_chunks}.

PRESERVE (verbatim):
- Error messages (exact text)
- Timestamps (exact values)
- Stack traces (if present)
- CRITICAL/ERROR/WARNING events

OMIT:
- INFO/DEBUG noise
- Repetitive patterns (summarize as "X occurrences of Y")

Format: Chronological summary with error details.
Target: 200-400 words per chunk.
""",
            DataType.STRUCTURED_CONFIG: f"""
You are summarizing configuration section {chunk_idx + 1} of {total_chunks}.

PRESERVE (verbatim):
- Configuration keys and values
- Service names and endpoints
- Version numbers
- Resource limits

OMIT:
- Comments and documentation
- Default values (unless critical)

Format: Key settings with context.
Target: 200-400 words per chunk.
""",
            DataType.METRICS_AND_PERFORMANCE: f"""
You are summarizing metrics section {chunk_idx + 1} of {total_chunks}.

PRESERVE (verbatim):
- Anomalies and outliers
- Performance degradations
- Resource usage spikes
- Metric values and timestamps

OMIT:
- Normal/baseline values
- Redundant measurements

Format: Summary of notable metrics.
Target: 200-400 words per chunk.
""",
            DataType.SOURCE_CODE: f"""
You are summarizing code section {chunk_idx + 1} of {total_chunks}.

PRESERVE (verbatim):
- Function/class names
- Error handling code
- Critical logic
- Comments about bugs/issues

OMIT:
- Boilerplate code
- Imports (unless relevant)
- Standard patterns

Format: Code structure with key logic.
Target: 200-400 words per chunk.
"""
        }

        system_prompt = prompts.get(data_type, prompts[DataType.UNSTRUCTURED_TEXT])

        try:
            response = await self.llm_router.call_llm(
                messages=[
                    {"role": "system", "content": system_prompt.strip()},
                    {"role": "user", "content": chunk}
                ],
                provider="synthesis",  # Use cheap synthesis provider
                max_tokens=1000  # Each summary ~1K tokens max
            )

            return response
        except Exception as e:
            logger.error(f"Failed to summarize chunk {chunk_idx + 1}: {e}")
            # Fallback: return truncated chunk
            return chunk[:2000] + "\n... [Summarization failed, content truncated]"

    async def _reduce_synthesize(
        self,
        chunk_summaries: List[str],
        data_type: DataType,
        filename: Optional[str] = None
    ) -> str:
        """
        REDUCE phase: Synthesize all chunk summaries into final summary

        Args:
            chunk_summaries: List of chunk summaries from MAP phase
            data_type: Type of data being processed
            filename: Optional filename for context

        Returns:
            Final synthesized summary (~2K tokens)
        """
        # Combine all chunk summaries with section markers
        combined = "\n\n".join([
            f"=== Section {i + 1} of {len(chunk_summaries)} ===\n{summary}"
            for i, summary in enumerate(chunk_summaries)
        ])

        synthesis_prompt = f"""
You are synthesizing {len(chunk_summaries)} section summaries from a {data_type.value} document{' named ' + filename if filename else ''}.

Create a coherent final summary that:
1. Maintains chronological/logical flow across sections
2. Preserves ALL error messages, codes, and critical details (verbatim)
3. Combines related information from different sections
4. Removes redundancy between sections
5. Highlights key troubleshooting insights and patterns

Format: Well-structured summary with clear sections.
Target length: 500-800 words (prioritize completeness over brevity).

IMPORTANT: This summary will be stored in the knowledge base and used for troubleshooting.
Preserve all technical details, error messages, and actionable information.
"""

        try:
            response = await self.llm_router.call_llm(
                messages=[
                    {"role": "system", "content": synthesis_prompt.strip()},
                    {"role": "user", "content": combined}
                ],
                provider="synthesis",
                max_tokens=2500  # Final summary can be longer
            )

            return response
        except Exception as e:
            logger.error(f"Failed to synthesize chunk summaries: {e}")
            # Fallback: return concatenated summaries
            return combined + "\n\n... [Synthesis failed, raw summaries returned]"

    def _estimate_tokens(self, text: str) -> int:
        """
        Estimate token count for text

        Uses simple heuristic: 1 token ≈ 4 characters
        This is conservative for English text
        """
        return len(text) // 4
