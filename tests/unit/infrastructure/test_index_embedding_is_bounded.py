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

The token count uses a tokenizer of its own, never the resident model's, whose
truncation state a concurrent encode flips (a count doubled in the review's
reproduction), one count at a time, and pins the thread knobs before its import
brings torch. The cores, counts and model are patched; no test loads the
tokenizer.
"""

import asyncio
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.infrastructure import embedding_guard, index_slot
from faultmaven.infrastructure import model_cache as model_cache_module
from faultmaven.infrastructure.embedding_guard import (
    EMBED_TIMEOUT_SECONDS,
    batch_embed_timeout,
    count_index_tokens,
    embed_texts_or_raise,
)
from faultmaven.infrastructure.index_slot import (
    EMBED_BATCH_LOAD_SECONDS,
    index_cores,
    index_estimate_seconds,
)
from faultmaven.infrastructure.model_cache import BGE_M3_MAX_SEQ_LENGTH, model_cache
from faultmaven.models.exceptions import KnowledgeBaseError
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)

pytestmark = [pytest.mark.unit]

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


@pytest.fixture
async def hang():
    """An encode that does not return until the test ends, then settles."""
    release = threading.Event()
    entered = threading.Event()

    def _encode(texts):
        entered.set()
        release.wait(30)
        return None

    yield SimpleNamespace(encode=_encode, entered=entered)
    release.set()
    for executor in (index_slot._KB_EXECUTOR, index_slot._EVIDENCE_EXECUTOR):
        # Bounded: a slot left blocked fails the teardown, not the run.
        await asyncio.wait_for(asyncio.wrap_future(executor.submit(lambda: None)), 30)


@pytest.fixture
def own_tokenizer(monkeypatch):
    """``AutoTokenizer.from_pretrained`` as a stand-in, and no cached one."""
    loaded = MagicMock(return_value={"input_ids": [[0] * 7, [0] * 5]})
    loader = MagicMock(return_value=loaded)
    transformers = SimpleNamespace(
        AutoTokenizer=SimpleNamespace(from_pretrained=loader)
    )
    monkeypatch.setitem(sys.modules, "transformers", transformers)
    monkeypatch.setattr(model_cache, "_tokenizer", None)
    return SimpleNamespace(tokenizer=loaded, loader=loader)


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


async def test_the_count_uses_a_tokenizer_of_its_own_never_the_resident_models(
    own_tokenizer, monkeypatch
):
    resident = MagicMock()
    resident.tokenizer.side_effect = AssertionError("the model's tokenizer")
    monkeypatch.setattr(model_cache, "peek_bge_m3_model", lambda: resident)

    assert await count_index_tokens(["a b c", "d e"]) == 12
    assert await count_index_tokens(["a b c", "d e"]) == 12

    resident.tokenizer.assert_not_called()
    own_tokenizer.loader.assert_called_once_with("BAAI/bge-m3", local_files_only=True)
    own_tokenizer.tokenizer.assert_called_with(
        ["a b c", "d e"],
        add_special_tokens=True,
        truncation=True,
        max_length=BGE_M3_MAX_SEQ_LENGTH,
    )


async def test_two_counts_never_enter_the_tokenizer_at_once(own_tokenizer):
    entered, release, second = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def _tokenize(texts, **_):
        calls.append(texts)
        (second if len(calls) > 1 else entered).set()
        if len(calls) == 1:
            release.wait(30)
        return {"input_ids": [[0, 0] for _ in texts]}

    own_tokenizer.tokenizer.side_effect = _tokenize
    first = asyncio.create_task(count_index_tokens(["a"]))
    await asyncio.to_thread(entered.wait, 30)
    later = asyncio.create_task(count_index_tokens(["b"]))

    # Without the lock the second count enters well within this.
    entered_beside = await asyncio.to_thread(second.wait, 0.3)
    release.set()

    assert (await first, await later) == (2, 2)
    assert not entered_beside


def test_the_thread_knobs_are_pinned_before_transformers_is_imported(monkeypatch):
    order = []

    class _Transformers:
        @property
        def AutoTokenizer(self):  # noqa: N802 - the import's name
            order.append("import")
            return SimpleNamespace(from_pretrained=lambda *_a, **_k: "tokenizer")

    monkeypatch.setitem(sys.modules, "transformers", _Transformers())
    monkeypatch.setattr(model_cache, "_tokenizer", None)
    monkeypatch.setattr(
        model_cache_module, "pin_thread_env", lambda: order.append("pin")
    )

    assert model_cache.get_bge_m3_tokenizer() == "tokenizer"
    assert order == ["pin", "import"]


@pytest.mark.parametrize(
    "loaded",
    [None, MagicMock(side_effect=RuntimeError("broken")), MagicMock()],
    ids=["no-tokenizer", "tokenizer-raises", "not-a-count"],
)
async def test_no_usable_tokenizer_counts_nothing(loaded):
    with patch.object(model_cache, "get_bge_m3_tokenizer", return_value=loaded):
        assert await count_index_tokens(["a", "b"]) is None


async def test_without_a_tokenizer_the_guard_bounds_by_bytes(
    two_cores, tokens_counted_as, monkeypatch
):
    """An upper bound to within a token per text, so a hang bound sized from it
    is not too tight."""
    texts = ["héllo", "wörld!"]
    seen = []

    def _bound(tokens):
        seen.append(tokens)
        return 5.0

    monkeypatch.setattr(model_cache, "embed_texts_sync", lambda t: [[0.1]] * len(t))
    with (
        tokens_counted_as(None),
        patch.object(embedding_guard, "batch_embed_timeout", _bound),
    ):
        await embed_texts_or_raise(texts, subject="Indexing", operation="test")

    assert seen == [sum(len(t.encode("utf-8")) + 2 for t in texts)]


# ---------------------------------------------------------------------------
# The guard itself
# ---------------------------------------------------------------------------


async def test_a_hung_embed_raises_instead_of_hanging(
    tokens_counted_as, hang, monkeypatch
):
    monkeypatch.setattr(model_cache, "embed_texts_sync", hang.encode)
    monkeypatch.setattr(index_slot, "EMBED_BATCH_LOAD_SECONDS", 0.05)

    with tokens_counted_as(0), pytest.raises(KnowledgeBaseError) as excinfo:
        await embed_texts_or_raise(["chunk"], subject="Indexing", operation="test")

    assert excinfo.value.error_code == "KNOWLEDGE_EMBEDDER_TIMEOUT"


async def test_an_unavailable_model_still_raises_unavailable_not_timeout(monkeypatch):
    monkeypatch.setattr(model_cache, "embed_texts_sync", lambda _texts: None)

    with pytest.raises(KnowledgeBaseError) as excinfo:
        await embed_texts_or_raise(["chunk"], subject="Indexing", operation="test")

    assert excinfo.value.error_code == "KNOWLEDGE_EMBEDDER_UNAVAILABLE"


async def test_a_healthy_embed_returns_its_vectors_from_the_evidence_slot(monkeypatch):
    threads = []

    def _encode(texts):
        threads.append(threading.current_thread().name)
        return [[0.1] * 1024 for _ in texts]

    monkeypatch.setattr(model_cache, "embed_texts_sync", _encode)

    vectors = await embed_texts_or_raise(["chunk"], subject="Indexing", operation="t")

    assert vectors == [[0.1] * 1024]
    assert threads == ["evidence-index_0"]


async def test_a_slow_but_progressing_embed_is_not_cut_off(
    tokens_counted_as, monkeypatch
):
    """Pinned against a bound well above the embed's own duration."""

    def _slow(texts):
        time.sleep(0.15)
        return [[0.1] * 1024 for _ in texts]

    monkeypatch.setattr(model_cache, "embed_texts_sync", _slow)
    monkeypatch.setattr(index_slot, "EMBED_BATCH_LOAD_SECONDS", 5.0)
    with tokens_counted_as(0):
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


async def test_the_indexing_path_is_bounded_by_default(
    tokens_counted_as, hang, monkeypatch
):
    service = _service()
    monkeypatch.setattr(model_cache, "embed_texts_sync", hang.encode)
    monkeypatch.setattr(index_slot, "EMBED_BATCH_LOAD_SECONDS", 0.05)

    with tokens_counted_as(0), pytest.raises(KnowledgeBaseError) as excinfo:
        await service._index_document_in_vector_store(
            _document(), enterprise_id=STANDALONE_ENTERPRISE_ID, generation="1f" * 16
        )

    assert excinfo.value.error_code == "KNOWLEDGE_EMBEDDER_TIMEOUT"
    assert service._vector_store.delete_documents_by_parent_id.await_count == 0


async def test_boot_repair_is_bounded_too_so_a_hang_cannot_crashloop_the_pod(
    tokens_counted_as, hang, monkeypatch
):
    """#953: ``kb_init``'s per-boot chunk budget is checked BETWEEN rows, so
    only a bound on the call stops a non-returning load hanging the lifespan.
    The repair submits its encode to the KB indexing slot, and returns 0 when
    the bound fires, whether the encode had started or not."""
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
    monkeypatch.setattr(model_cache, "embed_texts_sync", hang.encode)
    monkeypatch.setattr(index_slot, "EMBED_BATCH_LOAD_SECONDS", 0.05)
    submitted = []
    real_run = index_slot.IndexTicket.run

    def _run(ticket, texts, **kwargs):
        submitted.append(ticket._slot)
        return real_run(ticket, texts, **kwargs)

    with tokens_counted_as(0), patch.object(index_slot.IndexTicket, "run", _run):
        chunks = await service.reindex_missing_vectors("doc-1")

    assert submitted == [index_slot.index_admission], "the repair never submitted"
    assert chunks == 0, "a timed-out repair must degrade, not claim success"
    assert service._vector_store.add_documents.await_count == 0
