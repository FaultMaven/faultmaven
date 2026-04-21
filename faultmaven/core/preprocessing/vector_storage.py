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

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from faultmaven.core.preprocessing.models import Chunk, UnifiedDataType
from faultmaven.infrastructure.model_cache import model_cache

logger = logging.getLogger(__name__)


def _estimate_tokens(text: str) -> int:
    """Estimate token count: 1 token ~ 4 characters."""
    return len(text) // 4


def _get_last_n_tokens(text: str, n_tokens: int) -> str:
    """Get approximately the last n_tokens worth of text."""
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


async def store_in_vector_db_background(
    case_id: str,
    evidence_id: str,
    structural_index: str,
    data_type: UnifiedDataType,
    metadata: Dict[str, Any],
    case_vector_store: Any,
    max_chunk_tokens: Optional[int] = None,
    overlap_tokens: Optional[int] = None,
) -> None:
    """
    Background task: Chunk and store structural index in ChromaDB.

    User has already received response. This doesn't block upload.
    Silent failure — doesn't affect user experience.

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
            return

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

        # 3. Generate BGE-M3 embeddings for all chunks.
        # Both the model lookup (which may trigger a lazy load on cold start)
        # and encode() are CPU-bound and synchronous; run them on a worker
        # thread so we don't block the event loop.
        embeddings = None
        bge_model = await asyncio.to_thread(model_cache.get_bge_m3_model)
        if bge_model is not None:
            texts = [doc["content"] for doc in documents]
            embeddings = await asyncio.to_thread(
                lambda: bge_model.encode(texts).tolist()
            )
            logger.debug(
                f"Generated BGE-M3 embeddings for {len(texts)} chunks "
                f"({evidence_id})"
            )
        else:
            logger.warning(
                f"BGE-M3 unavailable for {evidence_id}, "
                f"falling back to ChromaDB default embedding"
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
    except Exception as e:
        logger.error(
            f"Failed to store in vector DB for {evidence_id}: {e}. "
            f"Evidence is still available via the Evidence record, "
            f"but semantic search will not find this file."
        )
        # Silent failure — evidence is still available via Evidence object.
        # The agent can still use the Tier 1 structural_index passed in-memory
        # during the upload turn. Only future semantic searches are affected.
