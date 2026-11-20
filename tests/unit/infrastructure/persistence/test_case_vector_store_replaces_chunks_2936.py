"""Re-vectorizing an evidence replaces its chunks (#2936).

Chunk ids are ``{evidence_id}_chunk_{i}`` and ChromaDB's ``add`` keeps an
existing id's old text, so a file re-chunked into fewer or different chunks
kept its old chunks beside the new ones. A real in-memory ChromaDB answers
what is stored.
"""

from __future__ import annotations

import uuid

import chromadb
import pytest
from chromadb.config import Settings as ChromaSettings

from faultmaven.infrastructure.persistence.case_vector_store import CaseVectorStore

pytestmark = [pytest.mark.unit]


@pytest.fixture
def client():
    """Pinned settings, as in ``test_case_vector_store_read_creates_nothing_1994``."""
    return chromadb.EphemeralClient(
        settings=ChromaSettings(
            anonymized_telemetry=False,
            allow_reset=False,
            environment="",
            is_persistent=False,
        )
    )


@pytest.fixture
def case_id(client):
    case = f"case_2936{uuid.uuid4().hex[:12]}"
    yield case
    client.delete_collection(f"case_{case}")


def _documents(evidence_id: str, texts: list[str]) -> list[dict]:
    return [
        {
            "id": f"{evidence_id}_chunk_{i}",
            "content": text,
            "metadata": {
                "evidence_id": evidence_id,
                "chunk_index": i,
                "total_chunks": len(texts),
            },
        }
        for i, text in enumerate(texts)
    ]


async def _add(store, case_id, evidence_id, texts):
    await store.add_documents(
        case_id=case_id,
        documents=_documents(evidence_id, texts),
        embeddings=[[float(i + 1), 0.5] for i in range(len(texts))],
    )


async def test_a_re_index_leaves_only_the_new_chunks(client, case_id):
    store = CaseVectorStore(client=client)
    await _add(store, case_id, "ev1", ["old0", "old1", "old2", "old3"])
    await _add(store, case_id, "ev2", ["other0"])

    await _add(store, case_id, "ev1", ["new0", "new1"])

    stored = client.get_collection(f"case_{case_id}").get(where={"evidence_id": "ev1"})
    assert sorted(stored["documents"]) == ["new0", "new1"]
    assert {m["total_chunks"] for m in stored["metadatas"]} == {2}
    other = client.get_collection(f"case_{case_id}").get(where={"evidence_id": "ev2"})
    assert other["documents"] == ["other0"]
