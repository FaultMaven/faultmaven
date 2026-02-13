"""Tests for vector DB chunking and background storage."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from faultmaven.core.preprocessing.models import Chunk, UnifiedDataType
from faultmaven.core.preprocessing.vector_storage import (
    EXTRACTION_VERSION,
    _estimate_tokens,
    _get_last_n_tokens,
    chunk_structural_index,
    store_in_vector_db_background,
)


# =============================================================================
# _estimate_tokens / _get_last_n_tokens
# =============================================================================


class TestTokenHelpers:
    def test_estimate_tokens(self):
        # 1 token ~ 4 chars
        assert _estimate_tokens("") == 0
        assert _estimate_tokens("abcd") == 1
        assert _estimate_tokens("a" * 400) == 100

    def test_get_last_n_tokens_short_text(self):
        text = "short"
        assert _get_last_n_tokens(text, 100) == text

    def test_get_last_n_tokens_exact(self):
        text = "a" * 400  # 100 tokens
        result = _get_last_n_tokens(text, 100)
        assert result == text

    def test_get_last_n_tokens_truncates(self):
        text = "a" * 800  # 200 tokens
        result = _get_last_n_tokens(text, 50)  # 50 tokens = 200 chars
        assert len(result) == 200
        assert result == "a" * 200


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
        text = (
            "=== Small Section ===\n"
            "Just a few lines\nof content"
        )
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

    def test_extraction_version_constant(self):
        assert EXTRACTION_VERSION == "v3.0"


# =============================================================================
# store_in_vector_db_background
# =============================================================================


class TestStoreInVectorDbBackground:
    @pytest.mark.asyncio
    async def test_successful_storage(self):
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
        assert docs[0]["metadata"]["extraction_version"] == EXTRACTION_VERSION

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
    async def test_scalar_metadata_filtered(self):
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
    async def test_error_handled_silently(self):
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
    async def test_chunk_index_and_total_in_metadata(self):
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
