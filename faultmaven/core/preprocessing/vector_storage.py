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
from enum import Enum
from typing import Any, Dict, Iterator, List, Optional, Tuple

from faultmaven.core.preprocessing.models import Chunk, UnifiedDataType
from faultmaven.infrastructure.concurrency import inflight
from faultmaven.infrastructure.embedding_guard import embed_texts_or_raise
from faultmaven.models.exceptions import KnowledgeBaseError
from faultmaven.utils.exception_name import exception_log_name
from faultmaven.utils.token_estimation import _get_tiktoken_encoder

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
# The encoding comes from ``utils.token_estimation``'s loader, the one every
# tiktoken count in the codebase shares, on first use rather than at import.
# tiktoken downloads the encoding's file on first use (the Docker image bakes it
# in), with no request timeout, so loading it at import could hold startup on
# the network. The loader returns None when tiktoken is missing or the file
# cannot be loaded, and this falls back to the heuristic: the preprocessor
# keeps working with degraded precision.


def _encoding():
    """tiktoken's ``cl100k_base``, or None when it cannot be loaded."""
    return _get_tiktoken_encoder("gpt-4")


def _estimate_tokens(text: str) -> int:
    """Return token count for *text*.

    Uses tiktoken cl100k_base when available, the ``len(text) // 4``
    heuristic otherwise. The heuristic is lossy (±40% on realistic
    content) — reached only when tiktoken is missing or its encoding file
    cannot be loaded.
    """
    encoding = _encoding()
    if encoding is not None:
        return len(encoding.encode(text))
    return len(text) // 4


def _char_offsets(encoding: Any, ids: List[int]) -> List[int]:
    """The character index at which each token of *ids* starts.

    A token that starts inside a multi-byte character is moved forward to the
    next character boundary, so slicing the text at these indexes never
    splits a character (decoding a slice of ids can, #2936).
    """
    offsets, chars = [], 0
    for token in encoding.decode_tokens_bytes(ids):
        offsets.append(chars)
        chars += sum(1 for byte in token if not 0x80 <= byte < 0xC0)
    return offsets


def _get_last_n_tokens(text: str, n_tokens: int) -> str:
    """Return the suffix of *text* whose length is about ``n_tokens`` tokens.

    With tiktoken, the suffix starts where the last ``n_tokens`` ids start,
    moved to a character boundary. Approximation otherwise (``n × 4``
    characters from the tail).
    """
    if n_tokens <= 0:
        return ""
    encoding = _encoding()
    if encoding is not None:
        ids = encoding.encode(text)
        if len(ids) <= n_tokens:
            return text
        return text[_char_offsets(encoding, ids)[-n_tokens] :]

    char_count = n_tokens * 4
    if len(text) <= char_count:
        return text
    return text[-char_count:]


def _emitted_tokens(text: str, kept: int) -> int:
    """*text*'s tokens as emitted (stripped), given its *kept* count."""
    stripped = text.strip()
    return kept if stripped == text else _estimate_tokens(stripped)


def _count(text: str, max_tokens: int) -> Optional[int]:
    """*text*'s tokens when a join may stand, else None.

    A join is judged as kept, the rule the paragraph split always used, so a
    section whose chunks all fit is chunked as before; and as emitted
    (stripped), so the chunk fits. A unit or a seeded buffer standing alone
    is judged as emitted only (#2936).
    """
    tokens = _estimate_tokens(text)
    if tokens > max_tokens or _emitted_tokens(text, tokens) > max_tokens:
        return None
    return tokens


def _cut_line(line: str, max_tokens: int, window: int) -> List[Tuple[str, int]]:
    """*line* whole when it fits as emitted, else consecutive pieces of at most
    *window* tokens as emitted, each with its token count as kept.

    The pieces concatenate back to *line*. Cuts fall on character boundaries,
    and each piece is measured once built and shrunk until it fits; a single
    character is never cut, so it is the one piece that can exceed *window*.
    """
    encoding = _encoding()
    if encoding is None:
        if _estimate_tokens(line.strip()) <= max_tokens:
            return [(line, _estimate_tokens(line))]
        step = window * 4
        pieces = [line[i : i + step] for i in range(0, len(line), step)]
        return [(piece, len(piece) // 4) for piece in pieces]
    ids = encoding.encode(line)
    if _emitted_tokens(line, len(ids)) <= max_tokens:
        return [(line, len(ids))]
    starts = _char_offsets(encoding, ids) + [len(line)]
    pieces: List[Tuple[str, int]] = []
    pos = i = 0
    while pos < len(line):
        j = min(i + window, len(ids))
        while starts[j] <= pos:
            j += 1
        while (
            starts[j - 1] > pos
            and _estimate_tokens(line[pos : starts[j]].strip()) > window
        ):
            j -= 1
        piece = line[pos : starts[j]]
        pieces.append((piece, _estimate_tokens(piece)))
        pos, i = starts[j], j
    return pieces


def _units(
    section: str, max_tokens: int, window: int
) -> Iterator[Tuple[str, str, int]]:
    """Yield ``(separator, unit, tokens)`` that rebuild *section*, each unit
    fitting as emitted (stripped), with its tokens as kept.

    Paragraphs (``\\n\\n``); a paragraph over *max_tokens* as its lines
    (``\\n``); a line over *max_tokens* as ``_cut_line``'s pieces, joined by
    nothing. A paragraph or line is encoded once as kept, and again stripped
    when that differs; an over-long line's ids are reused for its cuts, a
    piece's stripped text is encoded at each check of its size, and the
    piece once more as kept for its count.
    """
    for p, paragraph in enumerate(section.split("\n\n")):
        paragraph_separator = "\n\n" if p else ""
        tokens = _estimate_tokens(paragraph)
        if _emitted_tokens(paragraph, tokens) <= max_tokens:
            yield paragraph_separator, paragraph, tokens
            continue
        for n, line in enumerate(paragraph.split("\n")):
            line_separator = "\n" if n else paragraph_separator
            for k, (piece, tokens) in enumerate(_cut_line(line, max_tokens, window)):
                yield (line_separator if k == 0 else ""), piece, tokens


def _accumulate(
    units: List[Tuple[str, str, int]], max_tokens: int, overlap_tokens: int
) -> List[str]:
    """The chunk texts of one oversize section's units.

    A unit is taken on ``bound``, the buffer's last exact count plus each unit
    and separator taken since, plus one a join; only near the limit is the
    joined text encoded (``_count``), since re-encoding the buffer for every
    unit made short lines quadratic. A buffer is measured exactly before it is
    emitted, and units it took past the limit are given back. A buffer holding
    nothing but whitespace past its seeded overlap is not emitted.
    """
    separator_tokens = {sep: _estimate_tokens(sep) for sep in ("", "\n", "\n\n")}
    texts: List[str] = []
    parts: List[str] = []  # the buffer, as the strings joined into it
    bound = overlap_chars = i = 0
    while i < len(units) or parts:
        if i < len(units):
            separator, unit, tokens = units[i]
            if not parts:
                parts, bound, overlap_chars = [unit], tokens, 0
                i += 1
                continue
            step = separator_tokens[separator] + tokens + 1
            exact = (
                bound + step
                if bound + step <= max_tokens
                else _count("".join(parts) + separator + unit, max_tokens)
            )
            if exact is not None:
                parts.append(separator + unit)
                bound, i = exact, i + 1
                continue
        buffer = "".join(parts)
        while len(parts) > 1 and _count(buffer, max_tokens) is None:
            parts.pop()
            i -= 1
            buffer = "".join(parts)
        if buffer[overlap_chars:].strip():
            texts.append(buffer.strip())
        parts = []
        if i < len(units):
            separator, unit, tokens = units[i]
            overlap = _get_last_n_tokens(buffer, overlap_tokens)
            seeded = overlap + separator + unit
            kept = _estimate_tokens(seeded)
            if _emitted_tokens(seeded, kept) <= max_tokens:
                parts, bound, overlap_chars = [seeded], kept, len(overlap)
            else:
                parts, bound, overlap_chars = [unit], tokens, 0
            i += 1
    return texts


#: A section header: one whole line ``=== NAME ===``, whatever produced it.
#: It starts the text (after an optional byte-order mark) or follows ``\n`` or
#: ``\r``, and ends before ``\r\n``, ``\r``, ``\n`` or the end of the text.
#: Its group, the label, is NAME without its surrounding blanks; NAME's first
#: and last characters are not blank, so no run of blanks can be claimed two
#: ways and a match takes linear time. Not anchored to a line, ``\s+`` crossed
#: newlines, and a multi-line banner (an ``=====`` rule, a line, a rule) took
#: its middle line out of every chunk (#2949).
_SECTION_HEADER = re.compile(
    r"(?:\A|(?<=\A\ufeff)|(?<=[\r\n]))"
    r"===[ \t]+([^ \t\r\n](?:[^\r\n]*[^ \t\r\n])?)[ \t]+===[ \t]*(?=[\r\n]|\Z)"
)


def chunk_structural_index(
    structural_index: str,
    max_chunk_tokens: int = 500,
    overlap_tokens: int = 50,
) -> List[Chunk]:
    """
    Split a Tier 1 structural index into chunks for vector DB storage.

    Strategy:
    1. Split on section headers (a whole line ``=== NAME ===``) into
       logical sections
    2. If a section fits in one chunk, keep it whole
    3. If a section exceeds max_chunk_tokens, accumulate its paragraphs
       (\\n\\n) with overlap; a paragraph over the limit once stripped goes
       in as its lines (\\n)
    4. Never split mid-line, unless the line alone exceeds max_chunk_tokens
       once stripped: then it is cut into pieces of max_chunk_tokens -
       overlap_tokens

    No chunk exceeds max_chunk_tokens, measured on the text emitted, and none
    is only whitespace past its overlap (#2936). Each chunk carries metadata
    for retrieval context.

    Args:
        structural_index: The full Tier 1 structural index text
        max_chunk_tokens: Maximum tokens per chunk (default 500 ~ 2000 chars);
            at least 16 (the settings refuse less)
        overlap_tokens: Overlap between chunks (default 50 ~ 200 chars);
            at most half of max_chunk_tokens

    Returns:
        List of Chunk objects with text and metadata

    Design Reference:
        data-preprocessing-design-specification.md Section 5.2
    """
    if not structural_index.strip():
        return []

    # Split on section headers: the parts alternate text and a header's label
    parts = _SECTION_HEADER.split(structural_index)

    chunks: list[Chunk] = []
    current_section_name = "HEADER"

    for index, part in enumerate(parts):
        # Track current section name for metadata
        if index % 2:
            current_section_name = part.strip()
            continue

        part = part.strip()
        if not part:
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

        # Section too large: accumulate its units, seeding each next buffer
        # with the last ~overlap_tokens of the one emitted, when that fits.
        window = max(1, max_chunk_tokens - overlap_tokens)
        units = list(_units(part, max_chunk_tokens, window))
        chunks.extend(
            Chunk(text=text, metadata={"section": current_section_name})
            for text in _accumulate(units, max_chunk_tokens, overlap_tokens)
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
    #: Chunking, embedding, or the ChromaDB write raised, the embed timed
    #: out, or a shutdown's drain began before the write. Nothing guaranteed
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
    ``vectorization_reactive_timeout_seconds``. Those are the callers' budgets.
    The embed itself runs through ``embedding_guard.embed_texts_or_raise``,
    which bounds it by its UTF-8 bytes, on the evidence slot: one evidence
    encode at a time per process, in this process at ``ENCODE_BATCH_SIZE`` (a
    text over ``INDEX_RATE_BREAKPOINT_TOKENS`` alone), so
    a re-trigger queues behind an encode still running, in the KB indexing
    slot's one order, under its host lock (#2055, #2851, #2920).

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
        # 1. Chunk the structural index, off the loop: it encodes every unit
        # (§5.6.3's rule for the encoder holds for the chunker too).
        chunks = await asyncio.to_thread(
            chunk_structural_index,
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

        # 3. Generate BGE-M3 embeddings for all chunks, through the guard that
        # bounds every indexing embed (#2055): this call used to await the
        # model with no bound at all. A timeout raises, and is FAILED below.
        #
        # An unavailable model → nothing useful to write. This
        # used to fall back to ChromaDB's default embedding, which pins the
        # collection to that model's dimension: search embeds with BGE-M3
        # (CaseVectorStore.search, #941), so those chunks are unsearchable
        # forever, AND every later BGE-M3 write into the same collection is
        # rejected on dimension — one unavailable-model window poisoning the
        # case's evidence index for good. Skipping leaves the collection
        # writable once the model returns, and the evidence itself is still
        # reachable through the Evidence record and the Tier 1 structural index.
        texts = [doc["content"] for doc in documents]
        try:
            embeddings = await embed_texts_or_raise(
                texts,
                subject=f"Indexing evidence {evidence_id}",
                operation="vectorize_evidence",
            )
        except KnowledgeBaseError as embed_error:
            if embed_error.error_code != "KNOWLEDGE_EMBEDDER_UNAVAILABLE":
                raise  # a timeout: FAILED, below
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

        # 4. Store in vector DB, unless a shutdown's drain has begun: a cancel
        # stops the task but not a thread, so no write starts once the stores
        # may close under it (#2490). The embed above ran; nothing is written.
        if inflight.draining():
            logger.info(f"Not storing {evidence_id}: the process is draining")
            return VectorIndexOutcome.FAILED
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
            f"Failed to store in vector DB for {evidence_id}: {exception_log_name(e)}. "
            f"Evidence is still available via the Evidence record, "
            f"but semantic search will not find this file."
        )
        # Does not raise — evidence is still available via the Evidence object,
        # and the agent can still use the Tier 1 structural_index passed
        # in-memory during the upload turn. The outcome is still reported: a
        # caller that awaits this needs to know the file is not searchable.
        return VectorIndexOutcome.FAILED
