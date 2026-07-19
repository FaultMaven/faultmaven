"""Regression tests for the KnowledgeService → KnowledgeVectorStore.search contract.

`KnowledgeService` invokes ``vector_store.search``. The vector store's
signature is

    async def search(
        self,
        collection_name: str,
        query: str,
        k: int = 5,
        where: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:

Prior to 2026-05-20, the call sites used the pre-refactor signature
``search(query, k=..., filters=...)`` — missing the required
``collection_name`` and using the wrong kwarg name (`filters` vs `where`).
Every call raised TypeError, the surrounding try/except swallowed it, and
FaultMaven silently proceeded without KB context. Several Run 6 behavioral
findings (hallucinated evidence, no alternative hypotheses considered,
incomplete solutions) traced back to this silent KB failure.

The bug was invisible to existing tests because they all mock
``vector_store.search`` with ``AsyncMock(...)``, which accepts any call
shape. The tests below pin the call shape against the real signature.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
    KB_COLLECTION,
    KnowledgeVectorStore,
)


class _RecordingMockVectorStore:
    """A mock that exposes the real KnowledgeVectorStore.search signature.

    Using ``MagicMock(spec=KnowledgeVectorStore)`` would reject wrong-shaped
    calls but accept any kwarg names. This explicit signature records the
    exact kwargs passed so the test can assert against them.
    """

    def __init__(self):
        self.search_calls: list[dict] = []

    async def search(
        self,
        collection_name: str,
        query: str,
        k: int = 5,
        where: dict | None = None,
    ) -> list[dict]:
        self.search_calls.append(
            {
                "collection_name": collection_name,
                "query": query,
                "k": k,
                "where": where,
            }
        )
        return []


@pytest.mark.asyncio
async def test_knowledge_service_search_uses_real_signature():
    """``KnowledgeService.search`` must invoke ``vector_store.search`` with
    the kwargs matching the real signature (``collection_name``, ``query``,
    ``k``, ``where``). Passing ``filters=`` (the old name) would raise
    TypeError against the real store.
    """
    from faultmaven.modules.knowledge.domain.services.knowledge_service import (
        KnowledgeService,
    )

    store = _RecordingMockVectorStore()
    # Use a no-op tracer that supports the trace(name) context-manager
    # protocol the service expects.
    tracer = MagicMock()
    tracer.trace = MagicMock(
        return_value=MagicMock(__enter__=lambda s: s, __exit__=lambda *a: None)
    )

    service = KnowledgeService(
        knowledge_ingester=MagicMock(),
        sanitizer=MagicMock(asanitize=AsyncMock(side_effect=lambda q: q)),
        tracer=tracer,
        vector_store=store,
    )

    await service.search_knowledge("kubernetes pod crashloop", limit=5)

    assert len(store.search_calls) == 1
    call = store.search_calls[0]
    assert call["collection_name"] == KB_COLLECTION
    assert call["query"] == "kubernetes pod crashloop"
    assert call["k"] == 5
    assert isinstance(call["where"], dict)
    # Scope filter required by KnowledgeVectorStore._enforce_scope_invariant.
    from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
        SCOPE_FILTER_KEYS,
        _flatten_filter_keys,
    )

    assert _flatten_filter_keys(call["where"]) & SCOPE_FILTER_KEYS


def test_scope_filter_never_carries_team_metadata():
    """Unshare-trap guard (ADR-013 §D4 / ADR-011 D3): team visibility is an id
    allowlist, never scope/team_id metadata. A mutable 'team' tag in metadata
    would orphan a chunk for everyone (incl. its owner) on unshare, and a
    'global' tag would leak it. So the read filter must never emit either.
    """
    from faultmaven.modules.knowledge.domain.services.knowledge_service import (
        build_kb_scope_filter,
    )

    f = build_kb_scope_filter("user-1", ["kb_shared_1", "kb_shared_2"])
    flat = repr(f)
    assert "team_id" not in flat
    assert "'team'" not in flat  # no scope == 'team' condition
    # The shared arm is an id allowlist against the immutable parent id.
    assert {"parent_document_id": {"$in": ["kb_shared_1", "kb_shared_2"]}} in f["$or"]


def test_vector_metadata_has_no_team_field():
    """VectorMetadata carries only the immutable floor (owner + personal/global);
    team visibility never round-trips through ChromaDB metadata."""
    from faultmaven.models.vector_metadata import VectorMetadata

    meta = VectorMetadata(scope="personal", owner_id="user-1")
    chroma = meta.to_chroma_metadata()
    assert "team_id" not in chroma
    assert "team_id" not in VectorMetadata.model_fields


class _HitReturningStore:
    """Vector store returning raw hit dicts (id/content/metadata/score)."""

    def __init__(self, hits):
        self._hits = hits

    async def search(self, collection_name, query, k=5, where=None):
        return self._hits


def _service_with_hits(hits):
    from faultmaven.modules.knowledge.domain.services.knowledge_service import (
        KnowledgeService,
    )

    tracer = MagicMock()
    tracer.trace = MagicMock(
        return_value=MagicMock(__enter__=lambda s: s, __exit__=lambda *a: None)
    )
    return KnowledgeService(
        knowledge_ingester=MagicMock(),
        sanitizer=MagicMock(asanitize=AsyncMock(side_effect=lambda q: q)),
        tracer=tracer,
        vector_store=_HitReturningStore(hits),
    )


@pytest.mark.asyncio
async def test_search_result_surfaces_parent_document_id_from_metadata():
    """The matched runbook id (``metadata['parent_document_id']``) survives to
    ``SearchResult`` — the KB cause seeder loads that row's ``causes``."""
    service = _service_with_hits(
        [
            {
                "id": "kb_abc123_chunk_0",
                "content": "chunk text",
                "metadata": {"parent_document_id": "kb_abc123"},
                "score": 0.8,
            }
        ]
    )
    results = await service.search_knowledge("q", limit=5)
    assert len(results) == 1
    assert results[0].parent_document_id == "kb_abc123"


@pytest.mark.asyncio
async def test_search_result_parent_id_falls_back_to_chunk_suffix_strip():
    """When metadata omits the parent id, it is recovered from the chunk id
    (minted as ``f'{parent}_chunk_{i}'``)."""
    service = _service_with_hits(
        [
            {
                "id": "kb_def456_chunk_3",
                "content": "chunk text",
                "metadata": {},
                "score": 0.7,
            }
        ]
    )
    results = await service.search_knowledge("q", limit=5)
    assert results[0].parent_document_id == "kb_def456"


def test_knowledge_vector_store_search_signature_unchanged():
    """If KnowledgeVectorStore.search signature ever changes, this test
    breaks loudly. Any future refactor must update both callers AND this
    test in lockstep — the failure mode the 2026-05-20 fix surfaced (silent
    TypeError swallowed by try/except in callers) is otherwise easy to
    reintroduce.
    """
    import inspect

    sig = inspect.signature(KnowledgeVectorStore.search)
    params = list(sig.parameters.keys())
    # Drop 'self'
    params = [p for p in params if p != "self"]
    assert params == ["collection_name", "query", "k", "where"], (
        f"KnowledgeVectorStore.search signature changed to {params}. "
        f"If this is intentional, update the callers in "
        f"knowledge_service.py AND this test."
    )
