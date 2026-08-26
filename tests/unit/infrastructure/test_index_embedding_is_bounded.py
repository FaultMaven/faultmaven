"""The indexing embed needs a time bound too, not just the query one (#953).

#951 bounded QUERY embedding: ``embed_query_or_raise`` wraps ``aembed_query`` in
``asyncio.wait_for`` because the call sits outside ``call_external``'s timeout,
so a cold BGE-M3 load would otherwise hang a tool call to the outer turn budget.

The indexing path handled the same model's *unavailability* — ``aembed_texts``
returning ``None`` — and not its *silence*. On ``PUT /knowledge/documents/{id}``
that difference is a request that never answers.

The bound is deliberately proportional to the batch rather than a flat ceiling:
a fixed number tight enough to be useful for a 3-chunk edit would make a large
document permanently un-editable, trading a hang for a silent capability loss.
There is no way to opt out of it — an earlier revision of this change carried a
`bounded=False` knob for the boot repair path, and it was removed once the
argument for it turned out to be wrong (the per-boot chunk budget bounds how
much work a boot starts, not how long one call may take). The only remaining
way to embed unbounded is to bypass this module, which several write-side sites
still do; see its docstring.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.infrastructure import embedding_guard
from faultmaven.infrastructure.embedding_guard import (
    EMBED_BATCH_LOAD_SECONDS,
    EMBED_BATCH_PER_TEXT_SECONDS,
    EMBED_TIMEOUT_SECONDS,
    batch_embed_timeout,
    embed_texts_or_raise,
)
from faultmaven.models.exceptions import KnowledgeBaseError
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)

pytestmark = [pytest.mark.unit]

_EMBED_TEXTS = "faultmaven.infrastructure.model_cache.model_cache.aembed_texts"


def _never_returns(*_args, **_kwargs):
    """A model load that hangs — the failure ``None`` handling never covered."""

    async def _hang(*_a, **_k):
        await asyncio.sleep(3600)

    return _hang()


async def _slow_but_finishes(texts):
    """Slower than a deliberately tiny bound, but it does return — the case an
    opt-out has to let through."""
    await asyncio.sleep(0.15)
    return [[0.1] * 1024 for _ in texts]


# ---------------------------------------------------------------------------
# The policy is one policy, shared with the query path
# ---------------------------------------------------------------------------


def test_the_batch_bound_covers_a_cold_load_where_the_query_bound_does_not():
    """The two bounds differ on purpose, and the direction is the point.

    A query may give up before a cold BGE-M3 load finishes (60-120s) because
    the load continues in the background and the retry finds it warm — a read
    is cheap to repeat. Indexing is the last step before a document is saved,
    so a bound that fires during a HEALTHY cold load fails the write instead,
    and on the publish path deletes the row it just created. The batch bound
    must therefore outlast the load the query bound is willing to skip.
    """
    assert EMBED_BATCH_LOAD_SECONDS > 120, (
        "the constant term must cover the 60-120s cold load this repo "
        "documents, or a healthy first index times out"
    )
    assert batch_embed_timeout(0) == EMBED_BATCH_LOAD_SECONDS
    assert batch_embed_timeout(0) > EMBED_TIMEOUT_SECONDS


def test_the_bound_scales_with_the_work():
    """A property over the space, not one instance: every additional chunk buys
    exactly one more per-text allowance, so no document size is structurally
    un-embeddable."""
    for n in (1, 5, 50, 1244):
        assert batch_embed_timeout(n) == pytest.approx(
            EMBED_BATCH_LOAD_SECONDS + EMBED_BATCH_PER_TEXT_SECONDS * n
        )
    assert batch_embed_timeout(1244) > batch_embed_timeout(50) > batch_embed_timeout(1)


def test_a_negative_count_cannot_shrink_the_bound_below_the_load_budget():
    assert batch_embed_timeout(-5) == EMBED_BATCH_LOAD_SECONDS


# ---------------------------------------------------------------------------
# The guard itself
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_hung_embed_raises_instead_of_hanging():
    with patch(_EMBED_TEXTS, new=_never_returns):
        with patch.object(embedding_guard, "EMBED_BATCH_LOAD_SECONDS", 0.05):
            with patch.object(embedding_guard, "EMBED_BATCH_PER_TEXT_SECONDS", 0.0):
                with pytest.raises(KnowledgeBaseError) as excinfo:
                    await embed_texts_or_raise(
                        ["chunk"], subject="Indexing", operation="test"
                    )

    assert excinfo.value.error_code == "KNOWLEDGE_EMBEDDER_TIMEOUT"


@pytest.mark.asyncio
async def test_an_unavailable_model_still_raises_unavailable_not_timeout():
    """The two conditions stay distinguishable: the route tells the operator
    whether the previous vectors survived, and that answer differs."""
    with patch(_EMBED_TEXTS, new=AsyncMock(return_value=None)):
        with pytest.raises(KnowledgeBaseError) as excinfo:
            await embed_texts_or_raise(["chunk"], subject="Indexing", operation="test")

    assert excinfo.value.error_code == "KNOWLEDGE_EMBEDDER_UNAVAILABLE"


@pytest.mark.asyncio
async def test_a_healthy_embed_returns_its_vectors():
    """The gate must be able to pass."""
    with patch(_EMBED_TEXTS, new=AsyncMock(return_value=[[0.1] * 1024])):
        vectors = await embed_texts_or_raise(
            ["chunk"], subject="Indexing", operation="test"
        )

    assert vectors == [[0.1] * 1024]


@pytest.mark.asyncio
async def test_a_slow_but_progressing_embed_is_not_cut_off():
    """The bound must fire on work that has STOPPED, not work that is merely
    slow — the normal case on a CPU-limited pod.

    Pinned against a bound deliberately tightened to just above the embed's
    own duration, so this constrains the boundary rather than restating
    "a fast embed succeeds" with the 180s default doing the work.
    """
    with patch(_EMBED_TEXTS, new=_slow_but_finishes):
        with patch.object(embedding_guard, "EMBED_BATCH_LOAD_SECONDS", 1.0):
            with patch.object(embedding_guard, "EMBED_BATCH_PER_TEXT_SECONDS", 0.0):
                vectors = await embed_texts_or_raise(
                    ["chunk"], subject="Indexing", operation="test"
                )

    assert vectors == [[0.1] * 1024]


# ---------------------------------------------------------------------------
# Wired through the indexing path the issue was actually about
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
        # Required since #1166 — this fixture is not about the tier;
        # "global" keeps it exercising exactly what it did before.
        scope="global",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )


@pytest.mark.asyncio
async def test_the_indexing_path_is_bounded_by_default():
    """The live route (``PUT /knowledge/documents/{id}``) reaches this. Default
    must be bounded — a caller should not have to know to ask."""
    service = _service()

    with patch(_EMBED_TEXTS, new=_never_returns):
        with patch.object(embedding_guard, "EMBED_BATCH_LOAD_SECONDS", 0.05):
            with patch.object(embedding_guard, "EMBED_BATCH_PER_TEXT_SECONDS", 0.0):
                with pytest.raises(KnowledgeBaseError) as excinfo:
                    await service._index_document_in_vector_store(_document())

    assert excinfo.value.error_code == "KNOWLEDGE_EMBEDDER_TIMEOUT"
    assert (
        service._vector_store.delete_documents_by_parent_id.await_count == 0
    ), "a timed-out embed must not have destroyed the old vectors first (#945)"


@pytest.mark.asyncio
async def test_boot_repair_is_bounded_too_so_a_hang_cannot_crashloop_the_pod():
    """#953 asked whether boot repair could stay unbounded because ``kb_init``
    already applies a per-boot chunk budget. It cannot: that budget is checked
    BETWEEN rows, so it bounds how much work a boot starts, not how long one
    call may take — a single non-returning load never reaches the next check
    and hangs the awaited startup lifespan until the probe kills the pod.

    The repair loop already catches per-row exceptions and defers the row to
    the next boot, so the bound costs a retry and buys back the crashloop."""
    service = _service()

    row = MagicMock()
    row.item_id = "doc-1"
    row.title = "Draining a node"
    row.content = "# Draining a node\n\nCordon, then drain."
    row.item_type = "runbook"
    row.tags = []
    row.source_url = None
    row.scope = "global"
    row.owner_id = None
    row.created_at = "2026-01-01T00:00:00Z"
    row.updated_at = "2026-01-01T00:00:00Z"

    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=row)
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    service._db_session_factory = MagicMock(return_value=session)

    embed_calls = []

    async def _hang_but_record(texts):
        embed_calls.append(len(texts))
        await asyncio.sleep(3600)

    with patch(_EMBED_TEXTS, new=_hang_but_record):
        with patch.object(embedding_guard, "EMBED_BATCH_LOAD_SECONDS", 0.05):
            with patch.object(embedding_guard, "EMBED_BATCH_PER_TEXT_SECONDS", 0.0):
                chunks = await service.reindex_missing_vectors("doc-1")

    # `chunks == 0` alone is ambiguous — a repair that silently did NOTHING
    # returns 0 too, so that assertion would bless gutting boot repair
    # entirely. Pin that the repair was actually ATTEMPTED and then gave up:
    # the embed ran, and the destructive delete did not.
    assert embed_calls, "boot repair never attempted the embed at all"
    assert chunks == 0, "a timed-out repair must degrade, not claim success"
    assert service._vector_store.delete_documents_by_parent_id.await_count == 0
