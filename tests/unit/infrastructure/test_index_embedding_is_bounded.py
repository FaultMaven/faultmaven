"""The indexing embed is bounded, by its token count (#953, #2055).

#951 bounded QUERY embedding: ``embed_query_or_raise`` wraps ``aembed_query`` in
``asyncio.wait_for`` because the call sits outside ``call_external``'s timeout,
so a cold BGE-M3 load would otherwise hang a tool call to the outer turn budget.
The indexing path handled the same model's *unavailability* and not its
*silence* (#953).

The bound was then sized per chunk, ``180 + 2.0 x chunks``, and a chunk can hold
50 tokens or 3,000. The 97k-character runbook (61 chunks, 29,746 tokens) took
373 s on Cloud's 2 CPUs, and the guard allowed 302 s, so a document under the
upload cap was refused after the work (#2055). The bound is now a load plus
twice the measured estimate, ``tokens x 25 core-ms / cores``. It is still
proportional and uncapped: a ceiling tighter than what a large document costs
would make that document permanently un-editable. There is no opt-out.

The cores, token counts and model are patched; no test loads the tokenizer.
"""

import asyncio
import sys
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.infrastructure import embedding_guard, index_slot
from faultmaven.infrastructure.embedding_guard import (
    EMBED_BATCH_LOAD_SECONDS,
    EMBED_TIMEOUT_SECONDS,
    batch_embed_timeout,
    count_index_tokens,
    embed_texts_or_raise,
    index_cores,
    index_estimate_seconds,
)
from faultmaven.infrastructure.model_cache import BGE_M3_MAX_SEQ_LENGTH, model_cache
from faultmaven.models.exceptions import KnowledgeBaseError
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)

pytestmark = [pytest.mark.unit]

_EMBED_TEXTS = "faultmaven.infrastructure.model_cache.model_cache.aembed_texts"
_QUOTA = "faultmaven.infrastructure.model_cache._cgroup_cpu_quota"
#: The round-49 probe's runbook (#2055): 61 chunks, 29,746 tokens, 373 s.
RUNBOOK_TOKENS, RUNBOOK_CHUNKS, RUNBOOK_SECONDS = 29_746, 61, 373.0


@pytest.fixture
def two_cores():
    with patch(_QUOTA, return_value=2.0):
        yield


@pytest.fixture
def tokens_counted_as():
    """The guard's token count, without the tokenizer."""

    def _as(value):
        return patch.object(
            embedding_guard, "count_index_tokens", AsyncMock(return_value=value)
        )

    return _as


def _never_returns(*_args, **_kwargs):
    async def _hang(*_a, **_k):
        await asyncio.sleep(3600)

    return _hang()


async def _slow_but_finishes(texts):
    await asyncio.sleep(0.15)
    return [[0.1] * 1024 for _ in texts]


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


def test_the_measured_runbook_fits_where_the_chunk_rule_refused_it(two_cores):
    """The regression: 373 s of work under a 302 s bound."""
    bound = batch_embed_timeout(RUNBOOK_TOKENS)

    assert bound == pytest.approx(180 + 2 * 371.825)
    assert bound > RUNBOOK_SECONDS
    assert 180 + 2.0 * RUNBOOK_CHUNKS < RUNBOOK_SECONDS, "the old rule's 302 s"


def test_the_bound_scales_linearly_with_tokens(two_cores):
    for tokens in (1, 500, 29_746, 1_000_000):
        assert batch_embed_timeout(tokens) - EMBED_BATCH_LOAD_SECONDS == pytest.approx(
            2 * tokens * 0.025 / 2
        )
    assert batch_embed_timeout(-5) == EMBED_BATCH_LOAD_SECONDS


def test_the_load_term_covers_a_cold_load_the_query_bound_skips():
    assert EMBED_BATCH_LOAD_SECONDS > 120
    assert batch_embed_timeout(0) == EMBED_BATCH_LOAD_SECONDS > EMBED_TIMEOUT_SECONDS


@pytest.mark.parametrize(
    "quota,cpus,cores",
    [(2.0, 48, 2.0), (1.5, 48, 1.5), (None, 48, 4.0), (None, 2, 2.0)],
)
def test_the_cores_are_the_quota_else_the_node_capped_at_four(quota, cpus, cores):
    with patch(_QUOTA, return_value=quota), patch("os.cpu_count", return_value=cpus):
        assert index_cores() == cores
        assert index_estimate_seconds(1000) == pytest.approx(25 / cores)


# ---------------------------------------------------------------------------
# The count
# ---------------------------------------------------------------------------


async def test_tokens_are_counted_by_the_models_tokenizer_with_special_tokens():
    tokenizer = MagicMock(return_value={"input_ids": [[0] * 7, [0] * 5]})
    with patch.object(
        model_cache, "get_bge_m3_tokenizer", return_value=(tokenizer, 8192)
    ):
        assert await count_index_tokens(["a b c", "d e"]) == 12

    tokenizer.assert_called_once_with(
        ["a b c", "d e"], add_special_tokens=True, truncation=True, max_length=8192
    )


@pytest.mark.parametrize(
    "loaded",
    [None, (MagicMock(side_effect=RuntimeError("broken")), 8192), (MagicMock(), 1)],
    ids=["no-tokenizer", "tokenizer-raises", "not-a-count"],
)
async def test_no_usable_tokenizer_counts_nothing(loaded):
    with patch.object(model_cache, "get_bge_m3_tokenizer", return_value=loaded):
        assert await count_index_tokens(["a", "b"]) is None


def test_the_tokenizer_is_the_resident_models_else_loaded_once_offline(monkeypatch):
    resident = MagicMock(tokenizer="resident-tokenizer", max_seq_length=512)
    monkeypatch.setattr(model_cache, "_tokenizer", None)
    with patch.object(model_cache, "peek_bge_m3_model", return_value=resident):
        assert model_cache.get_bge_m3_tokenizer() == ("resident-tokenizer", 512)

    loader = MagicMock(return_value="standalone-tokenizer")
    transformers = SimpleNamespace(
        AutoTokenizer=SimpleNamespace(from_pretrained=loader)
    )
    with (
        patch.object(model_cache, "peek_bge_m3_model", return_value=None),
        patch.dict(sys.modules, {"transformers": transformers}),
    ):
        first = model_cache.get_bge_m3_tokenizer()
        second = model_cache.get_bge_m3_tokenizer()

    assert first == second == ("standalone-tokenizer", BGE_M3_MAX_SEQ_LENGTH)
    loader.assert_called_once_with("BAAI/bge-m3", local_files_only=True)


async def test_without_a_tokenizer_the_guard_bounds_by_bytes(
    two_cores, tokens_counted_as
):
    """An upper bound (no token is shorter than a byte, plus the two special
    tokens), so a hang bound sized from it is never too tight."""
    texts = ["héllo", "wörld!"]
    seen = []

    def _bound(tokens):
        seen.append(tokens)
        return 5.0

    with (
        tokens_counted_as(None),
        patch.object(embedding_guard, "batch_embed_timeout", _bound),
        patch(_EMBED_TEXTS, new=AsyncMock(return_value=[[0.1]] * 2)),
    ):
        await embed_texts_or_raise(texts, subject="Indexing", operation="test")

    assert seen == [sum(len(t.encode("utf-8")) + 2 for t in texts)]


# ---------------------------------------------------------------------------
# The guard itself
# ---------------------------------------------------------------------------


async def test_a_hung_embed_raises_instead_of_hanging(tokens_counted_as):
    with (
        patch(_EMBED_TEXTS, new=_never_returns),
        patch.object(embedding_guard, "EMBED_BATCH_LOAD_SECONDS", 0.05),
        tokens_counted_as(0),
        pytest.raises(KnowledgeBaseError) as excinfo,
    ):
        await embed_texts_or_raise(["chunk"], subject="Indexing", operation="test")

    assert excinfo.value.error_code == "KNOWLEDGE_EMBEDDER_TIMEOUT"


async def test_an_unavailable_model_still_raises_unavailable_not_timeout():
    with patch(_EMBED_TEXTS, new=AsyncMock(return_value=None)):
        with pytest.raises(KnowledgeBaseError) as excinfo:
            await embed_texts_or_raise(["chunk"], subject="Indexing", operation="test")

    assert excinfo.value.error_code == "KNOWLEDGE_EMBEDDER_UNAVAILABLE"


async def test_a_healthy_embed_returns_its_vectors():
    with patch(_EMBED_TEXTS, new=AsyncMock(return_value=[[0.1] * 1024])):
        vectors = await embed_texts_or_raise(
            ["chunk"], subject="Indexing", operation="test"
        )

    assert vectors == [[0.1] * 1024]


async def test_a_slow_but_progressing_embed_is_not_cut_off(tokens_counted_as):
    """Pinned against a bound just above the embed's own duration."""
    with (
        patch(_EMBED_TEXTS, new=_slow_but_finishes),
        patch.object(embedding_guard, "EMBED_BATCH_LOAD_SECONDS", 1.0),
        tokens_counted_as(0),
    ):
        vectors = await embed_texts_or_raise(
            ["chunk"], subject="Indexing", operation="test"
        )

    assert vectors == [[0.1] * 1024]


# ---------------------------------------------------------------------------
# Wired through the indexing paths
# ---------------------------------------------------------------------------


def _service() -> KnowledgeService:
    service = KnowledgeService.__new__(KnowledgeService)
    vector_store = MagicMock()
    vector_store.delete_documents_by_parent_id = AsyncMock()
    vector_store.add_documents = AsyncMock()
    service._vector_store = vector_store
    service._extract_frontmatter_for_rag = staticmethod(lambda content: {})
    return service


def _document():
    from faultmaven.models import KnowledgeBaseDocument

    return KnowledgeBaseDocument(
        document_id="doc-1",
        title="Draining a node",
        content="# Draining a node\n\nCordon, then drain.",
        document_type="runbook",
        tags=[],
        source_url=None,
        scope="global",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )


async def test_the_indexing_path_is_bounded_by_default(tokens_counted_as):
    service = _service()

    with (
        patch(_EMBED_TEXTS, new=_never_returns),
        patch.object(embedding_guard, "EMBED_BATCH_LOAD_SECONDS", 0.05),
        tokens_counted_as(0),
        pytest.raises(KnowledgeBaseError) as excinfo,
    ):
        await service._index_document_in_vector_store(
            _document(), enterprise_id=STANDALONE_ENTERPRISE_ID, generation="1f" * 16
        )

    assert excinfo.value.error_code == "KNOWLEDGE_EMBEDDER_TIMEOUT"
    assert service._vector_store.delete_documents_by_parent_id.await_count == 0


async def test_boot_repair_is_bounded_too_so_a_hang_cannot_crashloop_the_pod(
    tokens_counted_as,
):
    """#953: ``kb_init``'s per-boot chunk budget is checked BETWEEN rows, so
    only a bound on the call stops a non-returning load hanging the lifespan.
    The repair encodes on the KB indexing slot, whose thread then holds the
    slot until it settles; the repair itself returns 0."""
    service = _service()
    row = MagicMock(
        item_id="doc-1",
        enterprise_id=STANDALONE_ENTERPRISE_ID,
        title="Draining a node",
        content="# Draining a node\n\nCordon, then drain.",
        item_type="runbook",
        tags=[],
        source_url=None,
        scope="global",
        owner_id=None,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        vector_generation="1f" * 16,
    )
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=row)
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    service._db_session_factory = MagicMock(return_value=session)

    entered, release = threading.Event(), threading.Event()

    def _hang_on_the_slot(texts):
        entered.set()
        release.wait(30)
        return None

    try:
        with (
            patch.object(model_cache, "embed_texts_sync", _hang_on_the_slot),
            patch.object(embedding_guard, "EMBED_BATCH_LOAD_SECONDS", 0.05),
            tokens_counted_as(0),
        ):
            chunks = await service.reindex_missing_vectors("doc-1")
            attempted = entered.is_set()
    finally:
        release.set()
        while index_slot.index_admission.queued():
            await asyncio.sleep(0.01)

    assert attempted, "boot repair never attempted the embed at all"
    assert chunks == 0, "a timed-out repair must degrade, not claim success"
    assert service._vector_store.add_documents.await_count == 0
