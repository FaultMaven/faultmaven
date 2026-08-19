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
        # Required since #899; the vector search paths never reach it.
        db_session_factory=MagicMock(),
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
        # Required since #899; the vector search paths never reach it.
        db_session_factory=MagicMock(),
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


@pytest.mark.asyncio
async def test_search_result_surfaces_matched_cause_letters():
    """The chunk's own ``### Cause X:`` headings survive to ``SearchResult``.

    This is the #1092 join key. ``parent_document_id`` says which runbook holds
    the ``metadata['causes']`` record; this says which of those causes retrieval
    actually matched. Without it the KB cause seeder can only name the runbook,
    and seeds its first N causes in author order — which is how a Kubernetes
    OOMKilled case ended up with a GKE runbook's three *unschedulable* causes.
    """
    service = _service_with_hits(
        [
            {
                "id": "kb_abc_chunk_6",
                "content": (
                    "### Cause D: Container OOMKilled because memory limit is "
                    "below working-set demand\n\n**Statement**: ...\n"
                ),
                "metadata": {"parent_document_id": "kb_abc"},
                "score": 0.8,
            }
        ]
    )
    results = await service.search_knowledge("q", limit=5)
    assert results[0].matched_cause_letters == ["D"]


@pytest.mark.asyncio
async def test_search_reads_the_stamp_in_preference_to_the_chunk_text():
    """fm#1108 at the seam that matters: ``search_knowledge`` itself.

    The stamp and the text are made to DISAGREE, so this can only pass by
    reading the stamp. Every test above supplies un-stamped hits and therefore
    exercises the legacy fallback — which is the right default for them (that is
    what a pre-1108 chunk looks like) but means none of them cover the new path.
    """
    service = _service_with_hits(
        [
            {
                "id": "kb_abc_chunk_6",
                "content": "### Cause Z: a heading the stamp does not agree with",
                "metadata": {"parent_document_id": "kb_abc", "cause_letters": "D"},
                "score": 0.8,
            }
        ]
    )
    results = await service.search_knowledge("q", limit=5)
    assert results[0].matched_cause_letters == ["D"]


@pytest.mark.asyncio
async def test_matched_cause_letters_empty_for_a_non_cause_chunk():
    """A hit on Symptom Recognition / Diagnostic Steps / Prevention names no
    cause. The seeder reads [] as "retrieval surfaced no cause here" and seeds
    nothing from it — topical relevance is not evidence for any one cause."""
    service = _service_with_hits(
        [
            {
                "id": "kb_abc_chunk_0",
                "content": "## Symptom Recognition\n\n- Pods restart repeatedly\n",
                "metadata": {"parent_document_id": "kb_abc"},
                "score": 0.9,
            }
        ]
    )
    results = await service.search_knowledge("q", limit=5)
    assert results[0].matched_cause_letters == []


@pytest.mark.asyncio
async def test_matched_cause_letters_read_the_full_chunk_not_the_snippet():
    """Derived from the raw chunk ``content``, never from ``snippet``.

    ``snippet`` is a 200-char display truncation. A cause heading past that cut
    would silently attribute the hit to no cause (or, with more than one heading,
    to the wrong subset) rather than fail — so the derivation must not depend on
    it. Here the heading sits well past 200 chars.
    """
    filler = "x" * 400
    service = _service_with_hits(
        [
            {
                "id": "kb_abc_chunk_6",
                "content": f"## Causes\n\n{filler}\n\n### Cause A: something\n",
                "metadata": {"parent_document_id": "kb_abc"},
                "score": 0.8,
            }
        ]
    )
    results = await service.search_knowledge("q", limit=5)
    assert len(results[0].snippet) < 400  # the display field really is truncated
    assert results[0].matched_cause_letters == ["A"]


@pytest.mark.asyncio
async def test_matched_cause_letters_reports_every_heading_in_the_chunk():
    """A chunk spanning two headings was embedded as ONE text, so a hit on it is
    evidence for both causes — attributing to only one would be arbitrary.
    Reported in appearance order; the seeder's stable sort then keeps that
    (author) order for the score tie."""
    service = _service_with_hits(
        [
            {
                "id": "kb_abc_chunk_6",
                "content": ("### Cause A: first\n\ntext\n\n### Cause B: second\n"),
                "metadata": {"parent_document_id": "kb_abc"},
                "score": 0.8,
            }
        ]
    )
    results = await service.search_knowledge("q", limit=5)
    assert results[0].matched_cause_letters == ["A", "B"]


def test_shipped_pack_chunks_recover_every_cause_letter():
    """Corpus guard: on the real KB pack, the cause letters recoverable from the
    chunk texts are EXACTLY the letters of each runbook's causes record.

    The seeder's #1092 join is only as good as this. If chunking ever changes so
    a cause block no longer carries its heading (or the heading form drifts from
    the shared grammar), causes silently stop being seedable — the same class of
    quiet degradation the seeder's skip taxonomy exists to prevent, but upstream
    of it. A runbook's first Cause commonly shares a chunk with the ``## Causes``
    section header, which is why the derivation searches the whole chunk rather
    than anchoring at its start.
    """
    import json
    from pathlib import Path

    from faultmaven.modules.knowledge.domain.services.knowledge_service import (
        _matched_cause_letters,
    )

    pack = Path(__file__).resolve().parents[4] / "resources/knowledge/pack/pack.json"
    if not pack.exists():  # pragma: no cover - pack always vendored
        pytest.skip("KB pack not vendored in this checkout")
    runbooks = json.loads(pack.read_text())["runbooks"]

    checked = 0
    for rb in runbooks:
        causes = rb.get("causes") or []
        if not causes:
            continue
        checked += 1
        expected = {c["cause_letter"] for c in causes}
        recovered = set()
        for chunk in rb["chunks"]:
            recovered.update(_matched_cause_letters(chunk["text"]))
        assert recovered == expected, (
            f"{rb['item_id']} ({rb['title']}): chunk texts recover {sorted(recovered)} "
            f"but the causes record holds {sorted(expected)}"
        )
    assert checked > 0, "pack carried no runbook with a causes record"


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
    assert params == [
        "collection_name",
        "query",
        "k",
        "where",
        "query_embedding",
    ], (
        f"KnowledgeVectorStore.search signature changed to {params}. "
        f"If this is intentional, update the callers in "
        f"knowledge_service.py AND this test."
    )
    # ``query_embedding`` was appended (default None) so ``hybrid_search`` can
    # embed a query once and reuse the vector across the keyword sweep. It is
    # optional and trailing, so the callers in knowledge_service.py -- which
    # pass the first four by keyword and never supply a vector -- keep working
    # unchanged. Pinned here too: dropping the default would silently break them
    # in exactly the swallowed-TypeError way this test exists to catch.
    assert sig.parameters["query_embedding"].default is None
