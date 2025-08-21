import os
import io
import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from faultmaven.main import app


pytestmark = pytest.mark.integration


@pytest.mark.skipif(
    not os.getenv("CHROMADB_URL"),
    reason="Requires CHROMADB_URL to point to a running ChromaDB service",
)
def test_upload_lists_and_indexes_in_chroma(tmp_path):
    client = TestClient(app)

    # Unique content to search later
    unique_token = f"KB_INDEX_TOKEN_{int(time.time())}"
    content = f"# Test KB Doc\nThis is a test for indexing. Token: {unique_token}."

    # Upload document (multipart/form-data)
    files = {
        "file": ("kb.md", content.encode("utf-8"), "text/markdown"),
    }
    data = {
        "title": "Integration Test Doc",
        "document_type": "reference",
        "tags": "integration,test",
    }

    upload_resp = client.post("/api/v1/knowledge/documents", files=files, data=data)
    assert upload_resp.status_code == 200, upload_resp.text
    body = upload_resp.json()
    document_id = body["document_id"]
    assert document_id

    # List documents should include the new document (Redis-backed)
    list_resp = client.get("/api/v1/knowledge/documents?limit=50&offset=0")
    assert list_resp.status_code == 200, list_resp.text
    listed = list_resp.json().get("documents", [])
    assert any(doc.get("document_id") == document_id for doc in listed)

    # Verify Chroma indexing can find the token via vector store search
    # Use the adapter directly to avoid coupling to agent paths
    from faultmaven.infrastructure.persistence.chromadb_store import ChromaDBVectorStore

    async def _search():
        store = ChromaDBVectorStore()
        # Allow some time for indexing to settle in dev envs
        for _ in range(3):
            try:
                results = await store.search(query=unique_token, k=5)
                if results:
                    return results
            except Exception:
                pass
            await asyncio.sleep(1)
        return []

    results = asyncio.get_event_loop().run_until_complete(_search())
    assert any(r.get("id") == document_id for r in results), (
        f"Expected document {document_id} to be retrievable by vector search"
    )


