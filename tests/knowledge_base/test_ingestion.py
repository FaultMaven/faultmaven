import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, mock_open, patch

import numpy as np
import pytest

from faultmaven.knowledge_base.ingestion import KnowledgeIngester
from faultmaven.models import KnowledgeBaseDocument

pytestmark = pytest.mark.asyncio


@pytest.fixture
def ingester():
    """Fixture to create a KnowledgeIngester instance with a mocked ChromaDB client."""
    with patch("chromadb.PersistentClient"):
        ingester_instance = KnowledgeIngester()
        ingester_instance.chroma_client = MagicMock()
        ingester_instance.collection = MagicMock()
        # Mock the embedding model to avoid loading it
        ingester_instance.embedding_model = MagicMock()
        # Return numpy array instead of list
        ingester_instance.embedding_model.encode.return_value = np.array(
            [[0.1, 0.2, 0.3]]
        )
        return ingester_instance


async def test_ingest_unsupported_file_type(ingester):
    """
    Test that ingesting an unsupported file type raises a ValueError.
    """
    with patch("os.path.exists", return_value=True):
        with pytest.raises(ValueError, match="Unsupported file type"):
            await ingester.ingest_document("test.zip", title="Test Zip")


async def test_ingest_non_existent_file(ingester):
    """
    Test that ingesting a non-existent file raises a FileNotFoundError.
    """
    with patch("os.path.exists", return_value=False):
        with pytest.raises(FileNotFoundError):
            await ingester.ingest_document(
                "non_existent_file.txt", title="Test Non-existent"
            )


@patch("pathlib.Path.read_text", new_callable=MagicMock)
async def test_ingestion_fails_on_processing(mock_read_text, ingester):
    """
    Test that if processing a document chunk fails, it is handled gracefully.
    """
    ingester._process_and_store = AsyncMock(side_effect=Exception("Processing failed"))

    with patch("os.path.exists", return_value=True):
        with patch.object(
            ingester, "_extract_text", return_value="Some document content."
        ):
            with pytest.raises(Exception, match="Processing failed"):
                await ingester.ingest_document(
                    "fake_path.txt", title="Test Processing Failure"
                )

            ingester._process_and_store.assert_called_once()


async def test_extract_text_empty_content(ingester):
    """
    Test that extracting text from an empty file raises a ValueError.
    """
    with patch("os.path.exists", return_value=True):
        with patch("builtins.open", mock_open(read_data="")):
            with pytest.raises(ValueError, match="Could not extract text"):
                await ingester.ingest_document("empty.txt", title="Empty File")


async def test_extract_text_txt_encoding_error(ingester):
    """
    Test handling of encoding errors in text file extraction.
    """
    with patch(
        "builtins.open",
        side_effect=UnicodeDecodeError("utf-8", b"", 0, 1, "invalid utf-8"),
    ):
        with patch(
            "builtins.open", mock_open(read_data="latin-1 content"), create=True
        ):
            result = await ingester._extract_text_txt("test.txt")
            assert "latin-1 content" in result


async def test_extract_text_pdf_corrupted(ingester):
    """
    Test handling of corrupted PDF files.
    """
    with patch("builtins.open", mock_open(read_data=b"not a pdf")):
        with pytest.raises(Exception):
            await ingester._extract_text_pdf("corrupted.pdf")


async def test_extract_text_pdf_password_protected(ingester):
    """
    Test handling of password-protected PDF files.
    """
    with patch("pypdf.PdfReader", side_effect=Exception("Password required")):
        with pytest.raises(Exception):
            await ingester._extract_text_pdf("password.pdf")


async def test_extract_text_docx_corrupted(ingester):
    """
    Test handling of corrupted DOCX files.
    """
    with patch("docx.Document", side_effect=Exception("Invalid DOCX")):
        with pytest.raises(Exception):
            await ingester._extract_text_docx("corrupted.docx")


async def test_extract_text_csv_empty(ingester):
    """
    Test handling of empty CSV files.
    """
    with patch("pandas.read_csv", return_value=MagicMock(to_string=lambda: "")):
        result = await ingester._extract_text_csv("empty.csv")
        assert result == ""


async def test_extract_text_json_invalid(ingester):
    """
    Test handling of invalid JSON files.
    """
    with patch("builtins.open", mock_open(read_data="invalid json")):
        with pytest.raises(Exception):
            await ingester._extract_text_json("invalid.json")


async def test_extract_text_yaml_invalid(ingester):
    """
    Test handling of invalid YAML files.
    """
    with patch("builtins.open", mock_open(read_data="invalid: yaml: :")):
        with pytest.raises(Exception):
            await ingester._extract_text_yaml("invalid.yaml")


async def test_process_and_store_database_failure(ingester):
    """
    Test that database failures during storage are handled gracefully.
    """
    ingester.collection.add.side_effect = Exception("Database connection failed")

    document = KnowledgeBaseDocument(
        document_id="test-id",
        title="Test Document",
        content="Test content",
        document_type="guide",
    )

    with pytest.raises(Exception, match="Database connection failed"):
        await ingester._process_and_store(document)


async def test_process_and_store_embedding_failure(ingester):
    """
    Test that embedding model failures are handled gracefully.
    """
    ingester.embedding_model.encode.side_effect = Exception("Embedding model failed")

    document = KnowledgeBaseDocument(
        document_id="test-id",
        title="Test Document",
        content="Test content",
        document_type="guide",
    )

    with pytest.raises(Exception, match="Embedding model failed"):
        await ingester._process_and_store(document)


async def test_split_content_empty(ingester):
    """
    Test splitting empty content.
    """
    chunks = ingester._split_content("")
    assert chunks == [""]


async def test_split_content_small(ingester):
    """
    Test splitting content smaller than chunk size.
    """
    content = "Small content"
    chunks = ingester._split_content(content, chunk_size=100)
    assert chunks == [content]


async def test_split_content_with_overlap(ingester):
    """
    Test splitting content with overlap.
    """
    content = "This is a longer piece of content that should be split into multiple chunks with overlap between them"
    chunks = ingester._split_content(content, chunk_size=20, overlap=5)
    assert len(chunks) > 1
    # Check that chunks have some overlap (not necessarily exact 5 chars)
    for i in range(len(chunks) - 1):
        # Check that there's some overlap between consecutive chunks
        assert any(chunks[i][-j:] in chunks[i + 1] for j in range(1, 6))


async def test_search_database_failure(ingester):
    """
    Test that search failures are handled gracefully.
    """
    ingester.collection.query.side_effect = Exception("Search failed")

    # The search method catches exceptions and returns empty list
    results = await ingester.search("test query")
    assert results == []


async def test_search_empty_results(ingester):
    """
    Test search with empty results.
    """
    ingester.collection.query.return_value = {
        "documents": [],
        "metadatas": [],
        "distances": [],
    }

    results = await ingester.search("test query")
    assert results == []


async def test_search_with_filters(ingester):
    """
    Test search with metadata filters.
    """
    ingester.collection.query.return_value = {
        "documents": [["test document"]],
        "metadatas": [[{"type": "guide"}]],
        "distances": [[0.1]],
    }

    results = await ingester.search("test query", filter_metadata={"type": "guide"})
    assert len(results) == 1
    assert results[0]["document"] == "test document"


async def test_delete_document_not_found(ingester):
    """
    Test deleting a document that doesn't exist.
    """
    ingester.collection.get.return_value = {"ids": [], "metadatas": []}

    result = await ingester.delete_document("non-existent-id")
    assert result is False


async def test_delete_document_success(ingester):
    """
    Test successful document deletion.
    """
    ingester.collection.get.return_value = {
        "ids": ["chunk1", "chunk2"],
        "metadatas": [{"document_id": "test-id"}, {"document_id": "test-id"}],
    }
    ingester.collection.delete.return_value = None

    result = await ingester.delete_document("test-id")
    assert result is True


async def test_get_collection_stats(ingester):
    """
    Test getting collection statistics.
    """
    ingester.collection.count.return_value = 10
    ingester.collection.get.return_value = {
        "metadatas": [
            {"document_type": "guide", "tags": "tag1,tag2"},
            {"document_type": "reference", "tags": "tag3"},
        ]
    }

    stats = ingester.get_collection_stats()
    assert "total_chunks" in stats
    assert "document_types" in stats
    assert stats["total_chunks"] == 10


async def test_ingest_document_object_success(ingester):
    """
    Test successful ingestion of a document object.
    """
    ingester._process_and_store = AsyncMock()

    document = KnowledgeBaseDocument(
        document_id="test-id",
        title="Test Document",
        content="Test content",
        document_type="guide",
    )

    result = await ingester.ingest_document_object(document)
    assert result == "job_test-id"  # The actual implementation returns job_ prefix
    ingester._process_and_store.assert_called_once_with(document)


async def test_ingest_document_object_failure(ingester):
    """
    Test ingestion failure of a document object.
    """
    ingester._process_and_store = AsyncMock(side_effect=Exception("Processing failed"))

    document = KnowledgeBaseDocument(
        document_id="test-id",
        title="Test Document",
        content="Test content",
        document_type="guide",
    )

    with pytest.raises(Exception, match="Processing failed"):
        await ingester.ingest_document_object(document)


async def test_get_job_status_not_found(ingester):
    """
    Test getting status of a non-existent job.
    """
    result = await ingester.get_job_status("non-existent-job")
    assert result is None


async def test_list_documents_empty(ingester):
    """
    Test listing documents when collection is empty.
    """
    ingester.collection.get.return_value = {"documents": [], "metadatas": [], "ids": []}

    documents = await ingester.list_documents()
    assert documents == []


async def test_list_documents_with_filters(ingester):
    """
    Test listing documents with type and tag filters.
    """
    ingester.collection.get.return_value = {
        "metadatas": [
            {
                "document_id": "doc1",
                "title": "Test Doc",
                "document_type": "guide",
                "tags": "tag1,tag2",
            }
        ]
    }

    documents = await ingester.list_documents(document_type="guide", tags=["tag1"])
    assert len(documents) == 1


async def test_get_document_not_found(ingester):
    """
    Test getting a document that doesn't exist.
    """
    ingester.collection.get.return_value = {"documents": [], "metadatas": []}

    result = await ingester.get_document("non-existent-id")
    assert result is None


async def test_get_document_success(ingester):
    """
    Test successfully getting a document.
    """
    ingester.collection.get.return_value = {
        "documents": ["test content"],
        "metadatas": [
            {
                "document_id": "test-id",
                "title": "Test Doc",
                "document_type": "guide",
                "source_url": "test.md",
                "tags": "tag1",
            }
        ],
    }

    result = await ingester.get_document("test-id")
    assert result is not None
    assert result.document_id == "test-id"


async def test_search_documents_with_filters(ingester):
    """
    Test searching documents with type and tag filters.
    """
    ingester.collection.query.return_value = {
        "documents": [["test content"]],
        "metadatas": [[{"document_type": "guide", "tags": "tag1"}]],
        "distances": [[0.1]],
    }

    results = await ingester.search_documents(
        "test query", document_type="guide", tags=["tag1"]
    )
    assert len(results) == 1


async def test_initialization_with_http_client(ingester):
    """
    Test initialization with HTTP client when CHROMADB_URL is set.
    """
    with patch.dict(os.environ, {"CHROMADB_URL": "http://localhost:8000"}):
        with patch("chromadb.HttpClient"):
            ingester = KnowledgeIngester()
            assert ingester.chroma_client is not None


async def test_initialization_embedding_model_failure():
    """
    Test initialization failure when embedding model cannot be loaded.
    """
    with patch(
        "faultmaven.knowledge_base.ingestion.SentenceTransformer",
        side_effect=Exception("Model load failed"),
    ):
        with pytest.raises(Exception, match="Model load failed"):
            KnowledgeIngester()
