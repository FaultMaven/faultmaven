"""
Vector DB Storage for Structural Indexes

Chunks Tier 1 structural indexes and stores them in ChromaDB for
semantic search across case evidence.

Functions:
- chunk_structural_index(): Section-aware chunking for vector DB
- store_in_vector_db_background(): Async background storage task

Design Reference:
    docs/architecture/data-processing/data-preprocessing-design-specification.md Section 5
"""

import logging
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from faultmaven.core.preprocessing.models import Chunk, UnifiedDataType
from faultmaven.infrastructure.model_cache import model_cache

logger = logging.getLogger(__name__)


# -- Token counting -----------------------------------------------------------
#
# Chunks that are inconsistently sized in token space produce embeddings with
# different information densities, and cosine similarity doesn't compensate —
# retrieval precision drops. The prior estimator ``len(text) // 4`` diverges
# from real BPE tokenization by ±40% on content with long hex strings, CSV
# rows with repeated patterns, or non-ASCII text — all common in log data.
#
# We use tiktoken's ``cl100k_base`` encoding here. tiktoken is already a
# project dependency (pyproject.toml), initializes in microseconds, and
# encodes at ~3M tokens/sec — negligible overhead next to BGE-M3 embedding
# cost. It is not BGE-M3's exact tokenizer (BGE-M3 uses XLM-RoBERTa BPE),
# but both are BPE-family; token counts agree within ±15% on typical mixed
# content, which is far tighter than the prior heuristic's ±40%.
#
# Fallback to the heuristic if tiktoken is unavailable so startup never
# breaks — the preprocessor must continue to work with degraded precision.

try:
    import tiktoken

    _ENCODING = tiktoken.get_encoding("cl100k_base")
    _TIKTOKEN_AVAILABLE = True
except Exception:  # tiktoken missing, or encoding download failed offline
    _ENCODING = None
    _TIKTOKEN_AVAILABLE = False


def _estimate_tokens(text: str) -> int:
    """Return token count for *text*.

    Uses tiktoken cl100k_base when available, the ``len(text) // 4``
    heuristic otherwise. The heuristic is lossy (±40% on realistic
    content) — only reached when tiktoken is missing entirely.
    """
    if _ENCODING is not None:
        return len(_ENCODING.encode(text))
    return len(text) // 4


def _get_last_n_tokens(text: str, n_tokens: int) -> str:
    """Return the suffix of *text* whose length is ``n_tokens`` tokens.

    Exact when tiktoken is available — encodes, slices the last
    ``n_tokens`` ids, decodes back. Approximation otherwise (``n × 4``
    characters from the tail).
    """
    if _ENCODING is not None:
        ids = _ENCODING.encode(text)
        if len(ids) <= n_tokens:
            return text
        return _ENCODING.decode(ids[-n_tokens:])

    char_count = n_tokens * 4
    if len(text) <= char_count:
        return text
    return text[-char_count:]


def chunk_structural_index(
    structural_index: str,
    max_chunk_tokens: int = 500,
    overlap_tokens: int = 50,
) -> List[Chunk]:
    """
    Split a Tier 1 structural index into chunks for vector DB storage.

    Strategy:
    1. Split on section headers (=== ... ===) into logical sections
    2. If a section fits in one chunk, keep it whole
    3. If a section exceeds max_chunk_tokens, split on paragraph
       boundaries (\\n\\n) with overlap
    4. Never split mid-line

    Each chunk carries metadata for retrieval context.

    Args:
        structural_index: The full Tier 1 structural index text
        max_chunk_tokens: Maximum tokens per chunk (default 500 ~ 2000 chars)
        overlap_tokens: Overlap between chunks (default 50 ~ 200 chars)

    Returns:
        List of Chunk objects with text and metadata

    Design Reference:
        data-preprocessing-design-specification.md Section 5.2
    """
    if not structural_index.strip():
        return []

    # Split on section headers, preserving the headers as separators
    parts = re.split(r"(===\s+.+?\s+===)", structural_index)

    chunks: list[Chunk] = []
    current_section_name = "HEADER"

    for part in parts:
        part = part.strip()
        if not part:
            continue

        # Track current section name for metadata
        if re.match(r"===\s+.+?\s+===", part):
            current_section_name = part.strip("= ").strip()
            continue

        # If section fits in one chunk, emit as-is
        if _estimate_tokens(part) <= max_chunk_tokens:
            chunks.append(
                Chunk(
                    text=part,
                    metadata={"section": current_section_name},
                )
            )
            continue

        # Section too large — split on paragraph boundaries with overlap
        paragraphs = part.split("\n\n")
        buffer = ""

        for para in paragraphs:
            candidate = (buffer + "\n\n" + para) if buffer else para
            if _estimate_tokens(candidate) > max_chunk_tokens and buffer:
                chunks.append(
                    Chunk(
                        text=buffer.strip(),
                        metadata={"section": current_section_name},
                    )
                )
                # Overlap: keep the last ~overlap_tokens of buffer
                overlap_text = _get_last_n_tokens(buffer, overlap_tokens)
                buffer = overlap_text + "\n\n" + para
            else:
                buffer = candidate

        if buffer.strip():
            chunks.append(
                Chunk(
                    text=buffer.strip(),
                    metadata={"section": current_section_name},
                )
            )

    return chunks


class VectorIndexOutcome(str, Enum):
    """What actually happened to a file's vector index.

    Four states, because they mean four different things to whoever asks next
    and only one of them makes the file searchable. ``store_in_vector_db_background``
    returned ``None`` for all of them, which is fine for a fire-and-forget
    background task and wrong for ``vectorize_file``: that tool reported
    "vectorized and now searchable via case_evidence_search" on every one, so a
    file that was never written was announced as indexed, and the empty
    ``case_evidence_search`` that followed read as "searched this case, found
    nothing" — the affirmative negative moved from the store to the tool (#941).
    """

    #: Chunks were embedded and written. The file is searchable.
    INDEXED = "indexed"
    #: The structural index held no chunkable content. Nothing to write, and
    #: nothing wrong — a real fact about the file, not a failure.
    NOTHING_TO_INDEX = "nothing_to_index"
    #: BGE-M3 could not be loaded. Nothing written; retry once it is back.
    EMBEDDER_UNAVAILABLE = "embedder_unavailable"
    #: Chunking, embedding, or the ChromaDB write raised. Nothing guaranteed
    #: written.
    FAILED = "failed"


async def store_in_vector_db_background(
    case_id: str,
    evidence_id: str,
    structural_index: str,
    data_type: UnifiedDataType,
    metadata: Dict[str, Any],
    case_vector_store: Any,
    max_chunk_tokens: Optional[int] = None,
    overlap_tokens: Optional[int] = None,
) -> VectorIndexOutcome:
    """
    Chunk a structural index and store it in the case's ChromaDB collection.

    Never raises — a failure here must not take down whatever scheduled it.

    It does, however, **report** what happened. Swallowing the exception is not
    the same as discarding the outcome: ``vectorize_file`` awaits this and tells
    the investigating model whether the file is searchable, so a caller that
    cannot tell "indexed" from "nothing was written" states the former (#941).

    Despite the name, there is no upload-time caller: ``VectorizeFileTool`` is
    the only one, and it awaits this inside the directed-analysis tool loop —
    proactively as a background task, or reactively under
    ``vectorization_reactive_timeout_seconds``. The time-bound policy lives
    there, not here.

    Args:
        case_id: Case identifier
        evidence_id: Evidence identifier
        structural_index: Full Tier 1 structural index text
        data_type: Unified data type
        metadata: Additional metadata to store with each chunk
        case_vector_store: ChromaDB vector store instance
        max_chunk_tokens: Maximum tokens per chunk. When None, read from
            settings (VECTOR_CHUNK_SIZE_TOKENS, default 500).
        overlap_tokens: Overlap between chunks. When None, read from
            settings (VECTOR_CHUNK_OVERLAP_TOKENS, default 50).

    Returns:
        The :class:`VectorIndexOutcome`. Only ``INDEXED`` means searchable.

    Design Reference:
        data-preprocessing-design-specification.md Section 5.3
    """
    if max_chunk_tokens is None or overlap_tokens is None:
        from faultmaven.config.settings import get_settings

        settings = get_settings()
        if max_chunk_tokens is None:
            max_chunk_tokens = settings.database.vector_chunk_size_tokens
        if overlap_tokens is None:
            overlap_tokens = settings.database.vector_chunk_overlap_tokens

    try:
        # 1. Chunk the structural index
        chunks = chunk_structural_index(
            structural_index,
            max_chunk_tokens=max_chunk_tokens,
            overlap_tokens=overlap_tokens,
        )

        if not chunks:
            logger.info(
                f"No chunks generated for {evidence_id} — " f"structural index is empty"
            )
            return VectorIndexOutcome.NOTHING_TO_INDEX

        # 2. Build documents with metadata
        documents = []
        for i, chunk in enumerate(chunks):
            chunk_metadata = {
                "evidence_id": evidence_id,
                "case_id": case_id,
                "data_type": data_type.value,
                "section": chunk.metadata.get("section", ""),
                "chunk_index": i,
                "total_chunks": len(chunks),
                "upload_timestamp": datetime.now(timezone.utc).isoformat(),
            }
            # Add any scalar metadata from the caller
            for k, v in metadata.items():
                if isinstance(v, (str, int, float, bool)):
                    chunk_metadata[k] = v

            documents.append(
                {
                    "id": f"{evidence_id}_chunk_{i}",
                    "content": chunk.text,
                    "metadata": chunk_metadata,
                }
            )

        # 3. Generate BGE-M3 embeddings for all chunks (off the event loop via
        # the model_cache async boundary).
        #
        # None → BGE-M3 unavailable, and there is nothing useful to write. This
        # used to fall back to ChromaDB's default embedding, which pins the
        # collection to that model's dimension: search embeds with BGE-M3
        # (CaseVectorStore.search, #941), so those chunks are unsearchable
        # forever, AND every later BGE-M3 write into the same collection is
        # rejected on dimension — one unavailable-model window poisoning the
        # case's evidence index for good. Skipping leaves the collection
        # writable once the model returns, and the evidence itself is still
        # reachable through the Evidence record and the Tier 1 structural index.
        texts = [doc["content"] for doc in documents]
        embeddings = await model_cache.aembed_texts(texts)
        if embeddings is None:
            logger.warning(
                f"BGE-M3 unavailable for {evidence_id}: skipping vector "
                f"indexing rather than writing into a second embedding space. "
                f"Evidence remains available via the Evidence record; semantic "
                f"search will not find this file until it is re-indexed."
            )
            return VectorIndexOutcome.EMBEDDER_UNAVAILABLE
        logger.debug(
            f"Generated BGE-M3 embeddings for {len(texts)} chunks ({evidence_id})"
        )

        # 4. Store in vector DB
        await case_vector_store.add_documents(
            case_id=case_id,
            documents=documents,
            embeddings=embeddings,
        )

        logger.info(
            f"Structural index stored in vector DB: {evidence_id} "
            f"({len(chunks)} chunks)"
        )
        return VectorIndexOutcome.INDEXED
    except Exception as e:
        logger.error(
            f"Failed to store in vector DB for {evidence_id}: {e}. "
            f"Evidence is still available via the Evidence record, "
            f"but semantic search will not find this file."
        )
        # Does not raise — evidence is still available via the Evidence object,
        # and the agent can still use the Tier 1 structural_index passed
        # in-memory during the upload turn. The outcome is still reported: a
        # caller that awaits this needs to know the file is not searchable.
        return VectorIndexOutcome.FAILED
