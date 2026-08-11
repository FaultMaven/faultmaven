"""RunbookKnowledgeBase search behaviour over the live-writer row shape (fm#1030).

Dedup reads what ``KnowledgeService._index_document_in_vector_store`` writes:
N chunk rows per runbook carrying ``document_type``, ``scope``, ``owner_id``,
``title``, ``parent_document_id``. These tests pin the read path at the mock
level — the query shape, the chunk→runbook collapse, the thresholds, and the
typed refusals. The same properties are proven against a real ChromaDB client
in ``test_runbook_kb_scope.py``, and end-to-end through the live
``ingest_runbook`` writer in
``tests/integration/test_runbook_dedup_live_path.py``.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.infrastructure.knowledge.runbook_kb import (
    RESULTS_UNREADABLE_CODE,
    RunbookKnowledgeBase,
)
from faultmaven.models.exceptions import KnowledgeBaseError
from faultmaven.models.report import RunbookMatch

pytestmark = [pytest.mark.unit, pytest.mark.knowledge_base]

_DIM = 8
_SCOPE = {"$or": [{"scope": "global"}, {"owner_id": "user-a"}]}


def _vec(seed: float = 0.1) -> list:
    return [seed] * _DIM


def _chunk_meta(parent: str, title: str, scope: str = "global") -> dict:
    """The key subset of what the LIVE writer stamps on every chunk."""
    return {
        "document_type": "runbook",
        "scope": scope,
        "title": title,
        "parent_document_id": parent,
        "chunk_index": 0,
        "total_chunks": 1,
    }


def _results(rows: list) -> dict:
    """ChromaDB result shape: [(id, distance, metadata), ...]."""
    return {
        "ids": [[r[0] for r in rows]],
        "distances": [[r[1] for r in rows]],
        "metadatas": [[r[2] for r in rows]],
        "documents": [["# chunk" for _ in rows]],
    }


def _kb(rows=None) -> RunbookKnowledgeBase:
    store = MagicMock()
    store.query_by_embedding = AsyncMock(return_value=_results(rows or []))
    return RunbookKnowledgeBase(vector_store=store)


# ---------------------------------------------------------------------------
# Query shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_query_filters_on_document_type_and_the_callers_scope():
    """The where clause is exactly {"$and": [document_type, scope_filter]}.

    ``document_type == "runbook"`` is what the LIVE writer stamps — the old
    ``report_type`` predicate matched only rows a dead writer would have
    written, so every dedup query could only return [] (fm#1030). The scope
    filter is the caller's, composed as ONE ``$and`` operand.
    """
    kb = _kb()

    await kb.search_runbooks(query_embedding=_vec(), scope_filter=_SCOPE)

    where = kb.vector_store.query_by_embedding.await_args.kwargs["where"]
    assert where == {"$and": [{"document_type": "runbook"}, _SCOPE]}


@pytest.mark.asyncio
async def test_the_fetch_is_chunk_deep_not_result_shallow():
    """top_k runbooks needs more than top_k CHUNKS fetched.

    One runbook is N chunk rows, so a fetch of ``top_k`` chunks can be
    entirely filled by one long runbook. The fetch depth must be
    ``top_k * CHUNK_FETCH_MULTIPLIER``.
    """
    kb = _kb()

    await kb.search_runbooks(query_embedding=_vec(), scope_filter=_SCOPE, top_k=5)

    kwargs = kb.vector_store.query_by_embedding.await_args.kwargs
    # The literal number, NOT `5 * CHUNK_FETCH_MULTIPLIER`: asserting against
    # the constant that produces the value is self-referential — a mutation
    # of the multiplier (or of the fetch expression to use it differently)
    # changes both sides together and sails through (fm#1030 review).
    assert kwargs["top_k"] == 15


# ---------------------------------------------------------------------------
# Chunk → runbook collapse
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_long_runbook_does_not_fill_every_result_slot():
    """Chunks collapse by parent_document_id, best (max) chunk score wins."""
    kb = _kb(
        [
            ("kb-long_chunk_0", 0.10, _chunk_meta("kb-long", "Long runbook")),
            ("kb-long_chunk_1", 0.20, _chunk_meta("kb-long", "Long runbook")),
            ("kb-long_chunk_2", 0.30, _chunk_meta("kb-long", "Long runbook")),
            ("kb-other_chunk_0", 0.40, _chunk_meta("kb-other", "Other runbook")),
        ]
    )

    matches = await kb.search_runbooks(
        query_embedding=_vec(), scope_filter=_SCOPE, top_k=2
    )

    assert [m.item_id for m in matches] == ["kb-long", "kb-other"]
    # MAX per parent: distance 0.10 → similarity 0.95, not the weaker chunks'.
    assert matches[0].similarity_score == pytest.approx(0.95)
    assert matches[1].similarity_score == pytest.approx(0.80)
    assert isinstance(matches[0], RunbookMatch)
    assert matches[0].title == "Long runbook"
    assert matches[0].scope == "global"


@pytest.mark.asyncio
async def test_results_are_sorted_descending_and_truncated_to_top_k():
    kb = _kb(
        [
            ("a_chunk_0", 0.60, _chunk_meta("a", "A")),
            ("b_chunk_0", 0.20, _chunk_meta("b", "B")),
            ("c_chunk_0", 0.40, _chunk_meta("c", "C")),
        ]
    )

    matches = await kb.search_runbooks(
        query_embedding=_vec(), scope_filter=_SCOPE, top_k=2
    )

    assert [m.item_id for m in matches] == ["b", "c"]
    scores = [m.similarity_score for m in matches]
    assert scores == sorted(scores, reverse=True)


@pytest.mark.asyncio
async def test_chunks_below_the_similarity_threshold_are_discarded():
    kb = _kb(
        [
            ("near_chunk_0", 0.20, _chunk_meta("near", "Near")),
            # distance 1.2 → similarity 0.40 < 0.65 default threshold
            ("far_chunk_0", 1.20, _chunk_meta("far", "Far")),
        ]
    )

    matches = await kb.search_runbooks(query_embedding=_vec(), scope_filter=_SCOPE)

    assert [m.item_id for m in matches] == ["near"]


@pytest.mark.asyncio
async def test_a_genuine_miss_returns_an_empty_list():
    kb = _kb([])

    assert await kb.search_runbooks(query_embedding=_vec(), scope_filter=_SCOPE) == []


# ---------------------------------------------------------------------------
# Typed refusals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_scope", [None, {}, ""])
async def test_an_unscoped_search_is_refused_without_querying(bad_scope):
    """No scope filter → typed refusal, and the store is never queried.

    Similarity search is id-free: without the caller's scope there is no
    predicate keeping one principal's personal runbooks out of another's
    results. The refusal is typed so it can never be read as "none found".
    """
    kb = _kb()

    with pytest.raises(KnowledgeBaseError) as excinfo:
        await kb.search_runbooks(query_embedding=_vec(), scope_filter=bad_scope)

    assert excinfo.value.error_code == "RUNBOOK_SEARCH_UNSCOPED"
    kb.vector_store.query_by_embedding.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_scope", [None, {}])
async def test_search_by_text_refuses_an_unscoped_search_before_embedding(
    bad_scope, monkeypatch
):
    """The text entry point refuses BEFORE spending the embedding budget."""
    embed = AsyncMock(return_value=_vec())
    monkeypatch.setattr(
        "faultmaven.infrastructure.knowledge.runbook_kb.embed_query_or_raise", embed
    )
    kb = _kb()

    with pytest.raises(KnowledgeBaseError) as excinfo:
        await kb.search_by_text(query_text="oom", scope_filter=bad_scope)

    assert excinfo.value.error_code == "RUNBOOK_SEARCH_UNSCOPED"
    embed.assert_not_awaited()
    kb.vector_store.query_by_embedding.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_query_failure_is_a_typed_error_not_an_empty_list():
    kb = _kb()
    kb.vector_store.query_by_embedding = AsyncMock(
        side_effect=RuntimeError("chromadb unreachable")
    )

    with pytest.raises(KnowledgeBaseError) as excinfo:
        await kb.search_runbooks(query_embedding=_vec(), scope_filter=_SCOPE)

    assert excinfo.value.error_code == "RUNBOOK_SEARCH_FAILED"


# ---------------------------------------------------------------------------
# Unreadable identity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_matched_chunk_without_a_title_is_unreadable_and_can_refuse():
    """``title`` is truthiness-gated at write, so an empty-titled document's
    chunks carry NO title key — reachable, not hypothetical. When such a chunk
    outranks every readable match the search refuses instead of reporting the
    runner-up as the best available (#944 via the partial case)."""
    meta_no_title = {
        "document_type": "runbook",
        "scope": "global",
        "parent_document_id": "kb-untitled",
        "chunk_index": 0,
        "total_chunks": 1,
    }
    kb = _kb(
        [
            ("kb-untitled_chunk_0", 0.10, meta_no_title),  # 0.95, unreadable
            ("kb-ok_chunk_0", 0.40, _chunk_meta("kb-ok", "Readable")),  # 0.80
        ]
    )

    with pytest.raises(KnowledgeBaseError) as excinfo:
        await kb.search_runbooks(query_embedding=_vec(), scope_filter=_SCOPE)

    assert excinfo.value.error_code == RESULTS_UNREADABLE_CODE


@pytest.mark.asyncio
async def test_an_unreadable_chunk_below_the_best_readable_match_does_not_refuse():
    """A skipped row that cannot change the top-match verdict is tolerated."""
    meta_no_title = {
        "document_type": "runbook",
        "scope": "global",
        "parent_document_id": "kb-untitled",
    }
    kb = _kb(
        [
            ("kb-ok_chunk_0", 0.10, _chunk_meta("kb-ok", "Readable")),  # 0.95
            ("kb-untitled_chunk_0", 0.40, meta_no_title),  # 0.80, unreadable
        ]
    )

    matches = await kb.search_runbooks(query_embedding=_vec(), scope_filter=_SCOPE)

    assert [m.item_id for m in matches] == ["kb-ok"]


@pytest.mark.asyncio
async def test_a_tie_between_unreadable_and_readable_is_not_a_refusal():
    """At equal similarity the readable row produces the same verdict and
    names a runbook that really exists at that score — refusing would fail a
    correct answer over a row that could not have changed it."""
    meta_no_title = {
        "document_type": "runbook",
        "scope": "global",
        "parent_document_id": "kb-untitled",
    }
    kb = _kb(
        [
            ("kb-ok_chunk_0", 0.30, _chunk_meta("kb-ok", "Readable")),
            ("kb-untitled_chunk_0", 0.30, meta_no_title),
        ]
    )

    matches = await kb.search_runbooks(query_embedding=_vec(), scope_filter=_SCOPE)

    assert [m.item_id for m in matches] == ["kb-ok"]


@pytest.mark.asyncio
async def test_a_dissimilar_unreadable_chunk_is_not_counted_at_all():
    """Rows correctly discarded as dissimilar are neither matches nor
    unreadable — an unparseable row below the threshold cannot manufacture a
    refusal."""
    meta_no_title = {
        "document_type": "runbook",
        "scope": "global",
        "parent_document_id": "kb-untitled",
    }
    kb = _kb(
        [
            # distance 1.4 → similarity 0.30 < threshold; also unreadable
            ("kb-untitled_chunk_0", 1.40, meta_no_title),
        ]
    )

    assert await kb.search_runbooks(query_embedding=_vec(), scope_filter=_SCOPE) == []


@pytest.mark.asyncio
async def test_an_entirely_unreadable_result_set_refuses():
    """The degenerate case of the same rule: with nothing readable, any
    unread candidate is strictly the best (sentinel -1.0)."""
    meta_no_title = {
        "document_type": "runbook",
        "scope": "global",
        "parent_document_id": "kb-untitled",
    }
    kb = _kb([("kb-untitled_chunk_0", 0.10, meta_no_title)])

    with pytest.raises(KnowledgeBaseError) as excinfo:
        await kb.search_runbooks(query_embedding=_vec(), scope_filter=_SCOPE)

    assert excinfo.value.error_code == RESULTS_UNREADABLE_CODE


@pytest.mark.asyncio
async def test_the_unreadable_refusal_is_not_retried():
    """The parse runs OUTSIDE ``call_external``: a deterministic refusal must
    not consume the retry budget or count towards the shared breaker (#912).
    One query means the parse raised past the retry machinery."""
    meta_no_title = {
        "document_type": "runbook",
        "scope": "global",
        "parent_document_id": "kb-untitled",
    }
    kb = _kb([("kb-untitled_chunk_0", 0.10, meta_no_title)])

    with pytest.raises(KnowledgeBaseError):
        await kb.search_runbooks(query_embedding=_vec(), scope_filter=_SCOPE)

    assert kb.vector_store.query_by_embedding.await_count == 1
