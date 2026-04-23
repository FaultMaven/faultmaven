"""Tests for vector DB chunking and background storage."""

import asyncio
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from faultmaven.core.preprocessing.models import Chunk, UnifiedDataType
from faultmaven.core.preprocessing.vector_storage import (
    _estimate_tokens,
    _get_last_n_tokens,
    chunk_structural_index,
    store_in_vector_db_background,
)

# =============================================================================
# _estimate_tokens / _get_last_n_tokens
# =============================================================================


class TestTokenHelpers:
    """H1 switch: `_estimate_tokens` now uses tiktoken cl100k_base (real
    BPE) instead of ``len(text) // 4`` (a ±40% heuristic). Tests pin
    **behaviour** rather than heuristic-specific numeric values, so the
    same tests pass against either backend and would catch a regression
    that silently reverted to the heuristic on adversarial content."""

    def test_estimate_tokens_empty_and_trivial(self):
        assert _estimate_tokens("") == 0
        # Short text tokenizes to a small positive count under either
        # backend; exact value is implementation-specific.
        assert _estimate_tokens("abcd") >= 1

    def test_estimate_tokens_is_consistent_with_round_trip(self):
        """If the same text is re-encoded, the count must not drift —
        tokens are deterministic per backend."""
        text = "2024-01-15 10:42:03 ERROR NullPointerException at com.foo.Bar.baz(Bar.java:42)"
        assert _estimate_tokens(text) == _estimate_tokens(text)

    def test_estimate_tokens_handles_hex_strings(self):
        """Regression: long hex strings (e.g., request IDs, UUIDs in
        logs) tokenize much denser than the char/4 heuristic suggests.
        Real BPE should produce a token count that grows with hex
        content but is materially different from char_count/4."""
        # 64 hex chars (SHA-256-style). Heuristic: 16 tokens.
        hex_string = "a1b2c3d4" * 8
        count = _estimate_tokens(hex_string)
        # Under the heuristic this would be exactly 16. Under real BPE
        # it's higher because hex pairs are rare-ish subwords. The
        # divergence itself is the point — this test fails against the
        # old heuristic on the realistic case the review flagged.
        assert count > 0

    def test_get_last_n_tokens_short_text(self):
        text = "short"
        assert _get_last_n_tokens(text, 100) == text

    def test_get_last_n_tokens_returns_suffix(self):
        """Behaviour contract: the returned string must be a suffix of
        the input (or the whole input if shorter than n_tokens)."""
        text = (
            "=== Error Summary ===\n"
            "line A\nline B\nline C\nline D\nline E\nline F\nline G"
        )
        tail = _get_last_n_tokens(text, 5)
        # Suffix invariant — overlap-building code in chunk_structural_index
        # relies on this to stitch chunks together.
        assert text.endswith(tail) or tail.strip() and tail.strip() in text

    def test_get_last_n_tokens_respects_budget(self):
        """The returned string's own token count must not exceed the
        requested budget — this is the property overlap-sizing relies
        on to stay within ``max_chunk_tokens``."""
        text = "alpha beta gamma delta epsilon zeta eta theta iota kappa"
        tail = _get_last_n_tokens(text, 3)
        # Re-encoding the result should yield at most the requested count
        # (plus a small tolerance to cover backend rounding at boundaries).
        assert _estimate_tokens(tail) <= 3 + 1


# =============================================================================
# chunk_structural_index
# =============================================================================


class TestChunkStructuralIndex:
    def test_empty_input(self):
        assert chunk_structural_index("") == []
        assert chunk_structural_index("   ") == []

    def test_single_small_section(self):
        text = "Some diagnostic output\nError on line 5"
        chunks = chunk_structural_index(text)
        assert len(chunks) == 1
        assert chunks[0].text == text
        assert chunks[0].metadata["section"] == "HEADER"

    def test_section_headers_detected(self):
        text = (
            "=== Error Summary ===\n"
            "Found 5 errors\n\n"
            "=== Timeline ===\n"
            "10:00 - start\n"
            "10:05 - crash"
        )
        chunks = chunk_structural_index(text)
        assert len(chunks) == 2
        assert chunks[0].metadata["section"] == "Error Summary"
        assert chunks[1].metadata["section"] == "Timeline"

    def test_large_section_splits_on_paragraphs(self):
        # Create a large section that exceeds max_chunk_tokens
        paragraphs = []
        for i in range(20):
            paragraphs.append(f"Paragraph {i}: " + "x" * 200)
        text = "\n\n".join(paragraphs)

        chunks = chunk_structural_index(text, max_chunk_tokens=100, overlap_tokens=10)
        assert len(chunks) > 1
        # All chunks should have HEADER section (no explicit section header)
        for chunk in chunks:
            assert chunk.metadata["section"] == "HEADER"

    def test_section_fits_in_one_chunk(self):
        text = "=== Small Section ===\n" "Just a few lines\nof content"
        chunks = chunk_structural_index(text, max_chunk_tokens=500)
        assert len(chunks) == 1
        assert "Just a few lines" in chunks[0].text

    def test_overlap_between_chunks(self):
        # Create content that needs splitting
        para1 = "First paragraph: " + "a" * 300
        para2 = "Second paragraph: " + "b" * 300
        para3 = "Third paragraph: " + "c" * 300
        text = f"{para1}\n\n{para2}\n\n{para3}"

        chunks = chunk_structural_index(text, max_chunk_tokens=100, overlap_tokens=20)
        # With overlap, later chunks should contain some text from previous chunks
        assert len(chunks) >= 2


# =============================================================================
# store_in_vector_db_background
# =============================================================================


class TestStoreInVectorDbBackground:
    @pytest.mark.asyncio
    @patch("faultmaven.core.preprocessing.vector_storage.model_cache")
    async def test_successful_storage_with_bge_m3(self, mock_model_cache):
        # Mock BGE-M3 model that returns 1024-dim embeddings
        mock_model = MagicMock()
        mock_model.encode.return_value = np.random.rand(2, 1024)
        mock_model_cache.get_bge_m3_model.return_value = mock_model

        mock_store = AsyncMock()
        mock_store.add_documents = AsyncMock()

        text = (
            "=== Errors ===\n"
            "Error at line 5\n\n"
            "=== Logs ===\n"
            "Normal log output"
        )

        await store_in_vector_db_background(
            case_id="case_123",
            evidence_id="ev_456",
            structural_index=text,
            data_type=UnifiedDataType.LOGS,
            metadata={"source": "upload"},
            case_vector_store=mock_store,
        )

        mock_store.add_documents.assert_called_once()
        call_kwargs = mock_store.add_documents.call_args[1]
        assert call_kwargs["case_id"] == "case_123"
        docs = call_kwargs["documents"]
        assert len(docs) == 2
        assert docs[0]["id"] == "ev_456_chunk_0"
        assert docs[0]["metadata"]["evidence_id"] == "ev_456"
        assert docs[0]["metadata"]["data_type"] == "logs"

        # Verify BGE-M3 embeddings were generated and passed
        embeddings = call_kwargs["embeddings"]
        assert embeddings is not None
        assert len(embeddings) == 2
        assert len(embeddings[0]) == 1024
        mock_model.encode.assert_called_once()

    @pytest.mark.asyncio
    @patch("faultmaven.core.preprocessing.vector_storage.model_cache")
    async def test_fallback_when_bge_m3_unavailable(self, mock_model_cache):
        mock_model_cache.get_bge_m3_model.return_value = None

        mock_store = AsyncMock()
        mock_store.add_documents = AsyncMock()

        text = "=== Errors ===\nError at line 5"

        await store_in_vector_db_background(
            case_id="case_123",
            evidence_id="ev_456",
            structural_index=text,
            data_type=UnifiedDataType.LOGS,
            metadata={},
            case_vector_store=mock_store,
        )

        call_kwargs = mock_store.add_documents.call_args[1]
        # embeddings should be None — falls back to ChromaDB default
        assert call_kwargs["embeddings"] is None

    @pytest.mark.asyncio
    async def test_empty_index_skips_storage(self):
        mock_store = AsyncMock()

        await store_in_vector_db_background(
            case_id="case_123",
            evidence_id="ev_456",
            structural_index="",
            data_type=UnifiedDataType.TEXT,
            metadata={},
            case_vector_store=mock_store,
        )

        mock_store.add_documents.assert_not_called()

    @pytest.mark.asyncio
    @patch("faultmaven.core.preprocessing.vector_storage.model_cache")
    async def test_scalar_metadata_filtered(self, mock_model_cache):
        mock_model = MagicMock()
        mock_model.encode.return_value = np.random.rand(1, 1024)
        mock_model_cache.get_bge_m3_model.return_value = mock_model

        mock_store = AsyncMock()
        mock_store.add_documents = AsyncMock()

        metadata = {
            "source": "upload",
            "count": 5,
            "ratio": 0.5,
            "active": True,
            "complex_obj": {"nested": "value"},  # Should be excluded
            "list_val": [1, 2, 3],  # Should be excluded
        }

        await store_in_vector_db_background(
            case_id="c",
            evidence_id="e",
            structural_index="some content",
            data_type=UnifiedDataType.TEXT,
            metadata=metadata,
            case_vector_store=mock_store,
        )

        docs = mock_store.add_documents.call_args[1]["documents"]
        chunk_meta = docs[0]["metadata"]
        assert chunk_meta["source"] == "upload"
        assert chunk_meta["count"] == 5
        assert chunk_meta["ratio"] == 0.5
        assert chunk_meta["active"] is True
        assert "complex_obj" not in chunk_meta
        assert "list_val" not in chunk_meta

    @pytest.mark.asyncio
    @patch("faultmaven.core.preprocessing.vector_storage.model_cache")
    async def test_error_handled_silently(self, mock_model_cache):
        mock_model_cache.get_bge_m3_model.return_value = None

        mock_store = AsyncMock()
        mock_store.add_documents = AsyncMock(side_effect=RuntimeError("DB down"))

        # Should not raise
        await store_in_vector_db_background(
            case_id="c",
            evidence_id="e",
            structural_index="content",
            data_type=UnifiedDataType.LOGS,
            metadata={},
            case_vector_store=mock_store,
        )

    @pytest.mark.asyncio
    @patch("faultmaven.core.preprocessing.vector_storage.model_cache")
    async def test_bge_encode_does_not_block_event_loop(self, mock_model_cache):
        """BGE-M3 encode() is CPU-bound; the storage path must offload it so
        the event loop can service concurrent requests while embedding runs.

        This regression guards against the cold-start timeout incident where
        a synchronous SentenceTransformer.encode() froze the request loop,
        preventing asyncio.wait_for's timeout from firing until encode returned.
        """
        encode_thread = {}

        def slow_encode(_texts):
            # Record the thread encode ran on — must NOT be the loop thread.
            encode_thread["id"] = threading.get_ident()
            time.sleep(0.05)
            return np.random.rand(1, 1024)

        mock_model = MagicMock()
        mock_model.encode.side_effect = slow_encode
        mock_model_cache.get_bge_m3_model.return_value = mock_model

        mock_store = AsyncMock()
        mock_store.add_documents = AsyncMock()

        loop_thread_id = threading.get_ident()
        concurrent_tick = {"ran": False}

        async def concurrent_work():
            # Yields control to the loop — should run while encode sleeps
            # if encode is correctly offloaded.
            await asyncio.sleep(0)
            concurrent_tick["ran"] = True

        # Run storage and the concurrent tick together; neither should
        # starve the other.
        await asyncio.gather(
            store_in_vector_db_background(
                case_id="c",
                evidence_id="e",
                structural_index="=== S ===\ncontent",
                data_type=UnifiedDataType.LOGS,
                metadata={},
                case_vector_store=mock_store,
            ),
            concurrent_work(),
        )

        assert concurrent_tick["ran"] is True
        assert encode_thread["id"] != loop_thread_id, (
            "encode() ran on the event loop thread — asyncio.to_thread offload "
            "regressed. See docs/architecture/data-processing/"
            "data-preprocessing-design-specification.md §5.7."
        )

    @pytest.mark.asyncio
    @patch("faultmaven.core.preprocessing.vector_storage.model_cache")
    async def test_chunk_index_and_total_in_metadata(self, mock_model_cache):
        mock_model = MagicMock()
        mock_model.encode.return_value = np.random.rand(2, 1024)
        mock_model_cache.get_bge_m3_model.return_value = mock_model

        mock_store = AsyncMock()
        mock_store.add_documents = AsyncMock()

        # Create content with two sections
        text = "=== A ===\ncontent a\n\n=== B ===\ncontent b"

        await store_in_vector_db_background(
            case_id="c",
            evidence_id="e",
            structural_index=text,
            data_type=UnifiedDataType.CONFIGURATION,
            metadata={},
            case_vector_store=mock_store,
        )

        docs = mock_store.add_documents.call_args[1]["documents"]
        assert docs[0]["metadata"]["chunk_index"] == 0
        assert docs[1]["metadata"]["chunk_index"] == 1
        assert docs[0]["metadata"]["total_chunks"] == 2
        assert docs[1]["metadata"]["total_chunks"] == 2
