"""Record the knowledge-base question lookup's search for a labelled topic set.

Offline: a scratch Chroma built from the shipped pack's pre-built vectors, the
real hybrid pipeline (``KnowledgeVectorStore.hybrid_search``), and the local
BGE-M3 model for the query embedding. No server, no provider key, no LLM.

Each topic is searched the way ``kb_question_lookup.look_up_kb_question``
searches it — the bare topic, ``KB_QUESTION_FETCH_LIMIT`` results, admitted at
``KB_QUESTION_CANDIDATE_FLOOR`` — and every result is written to
``question-lookups.json`` beside this file. The unit test
``tests/unit/core/investigation/test_kb_question_floor_recording.py`` replays
the recording through the lookup itself.

Run from the repository root (BGE-M3 must be in the local model cache):

    HF_HUB_OFFLINE=1 python tests/eval/kb_retrieval/record_question_lookups.py <scratch_dir>
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path

import chromadb
import numpy as np

from faultmaven.core.investigation.kb_question import (
    KB_QUESTION_CANDIDATE_FLOOR,
    KB_QUESTION_FETCH_LIMIT,
)
from faultmaven.infrastructure.knowledge import knowledge_vector_store as kvs
from faultmaven.infrastructure.model_cache import model_cache

HERE = Path(__file__).resolve().parent
PACK = HERE.parents[2] / "resources" / "knowledge" / "pack"

#: A topic, and the title fragments of the runbooks that cover it; ``None``
#: when the shipped pack covers nothing on it.
TOPICS: list[tuple[str, list[str] | None]] = [
    ("pods OOMKilled with exit code 137", ["OOMKilled", "CrashLoopBackOff"]),
    (
        "postgres running out of connection slots",
        ["PostgreSQL Connection Pool", "RDS Connection"],
    ),
    ("nginx 502 bad gateway", ["NGINX 502"]),
    ("kafka consumer lag", ["Kafka Consumer Lag"]),
    ("redis out of memory", ["Redis Out of Memory"]),
    ("expired TLS certificate", ["TLS Certificate Expiry", "cert-manager"]),
    ("ImagePullBackOff", ["ImagePullBackOff"]),
    ("disk full on a linux server", ["Linux Disk Full"]),
    ("JVM heap OutOfMemoryError", ["JVM OutOfMemoryError"]),
    ("ArgoCD sync failures", ["ArgoCD Application Sync"]),
    ("lambda timeouts", ["Lambda Function Timeout"]),
    ("elasticsearch red cluster health", ["Elasticsearch Cluster"]),
    ("CoreDNS resolution failures", ["CoreDNS"]),
    ("S3 403 access denied", ["S3 403", "IAM Access Denied"]),
    ("stuck terraform state lock", ["Terraform State Lock"]),
    ("mysql replication stopped", ["MySQL"]),
    ("SAP HANA backup failures", None),
    ("Oracle RAC interconnect errors", None),
    ("Cisco ASA VPN tunnel flapping", None),
    ("office printer paper jam", None),
    ("Salesforce Apex governor limits", None),
    ("mainframe CICS abend codes", None),
]


async def _record(scratch: Path) -> dict:
    pack = json.loads((PACK / "pack.json").read_text())
    vectors = np.load(PACK / "vectors.npz")["vectors"]
    path = scratch / "chroma"
    shutil.rmtree(path, ignore_errors=True)
    store = kvs.KnowledgeVectorStore(chromadb.PersistentClient(path=str(path)))
    docs, embeddings, i = [], [], 0
    for runbook in pack["runbooks"]:
        for chunk in runbook["chunks"]:
            docs.append(
                {
                    "id": f"{runbook['item_id']}_chunk_{chunk['chunk_index']}",
                    "content": chunk["text"],
                    "metadata": {
                        "scope": "global",
                        "enterprise_id": "e1",
                        "title": runbook["title"],
                        "parent_document_id": runbook["item_id"],
                        "vector_generation": "g1",
                        "chunk_index": chunk["chunk_index"],
                        "total_chunks": len(runbook["chunks"]),
                    },
                }
            )
            embeddings.append(vectors[i].tolist())
            i += 1
    for start in range(0, len(docs), 500):
        await store.add_documents(
            docs[start : start + 500],
            embeddings=embeddings[start : start + 500],
            collection_name=kvs.KB_COLLECTION,
        )
    # BGE-M3 loads in ~15 s and the embedder guard times out at 10 s.
    await model_cache.aembed_query("warmup")

    async def admit(parent_ids):
        return {p: "g1" for p in parent_ids}

    rows = []
    for topic, covered_by in TOPICS:
        hits = await store.hybrid_search(
            kvs.KB_COLLECTION,
            topic,
            k=KB_QUESTION_FETCH_LIMIT,
            where={"scope": "global"},
            min_score=KB_QUESTION_CANDIDATE_FLOOR,
            admit=admit,
        )
        rows.append(
            {
                "topic": topic,
                "covered_by": covered_by,
                "results": [
                    {
                        "title": (h.get("metadata") or {}).get("title"),
                        "parent_document_id": (h.get("metadata") or {}).get(
                            "parent_document_id"
                        ),
                        "score": round(float(h.get("score") or 0.0), 4),
                        "rerank_score": round(float(h.get("rerank_score") or 0.0), 4),
                        "head": (h.get("content") or "")[:160],
                    }
                    for h in hits
                ],
            }
        )
    return {
        "pack_version": pack["version"],
        "pack_vectors_sha256": pack["vectors_sha256"],
        "fetch_limit": KB_QUESTION_FETCH_LIMIT,
        "admission_floor": KB_QUESTION_CANDIDATE_FLOOR,
        "rows": rows,
    }


def main() -> None:
    recording = asyncio.run(_record(Path(sys.argv[1])))
    out = HERE / "question-lookups.json"
    out.write_text(json.dumps(recording, indent=1, ensure_ascii=False) + "\n")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
