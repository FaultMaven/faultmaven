"""A failed re-index must not leave the row describing text the vectors don't (#952).

#951 stopped a failed re-index from DESTROYING a document: the indexing path
now embeds before it deletes, so the old vectors survive an unavailable
embedder. What it did not change is that ``update_document_metadata`` committed
the SQL row *first*. On any indexing failure the row therefore held the new
content while ChromaDB still held vectors for the old — the document stayed
searchable, and matched on superseded text.

That state is worse than it looks, and the reason is detectability rather than
severity:

* SQL new / vectors old is a MISPAIRING. Every consistency check sees a row
  with vectors present and calls it healthy, so nothing finds it and nothing
  repairs it. Retrieval keeps answering with content the document no longer has.
* SQL old / vectors missing is RECOVERABLE. The row never moved, so the content
  needed to rebuild the vectors is still there: on single-tenant the boot
  reconcile pass finds the chunkless parent and repairs it, and everywhere else
  (Cloud skips that bootstrap entirely) re-saving does the same by hand.

So the fix is an ordering one: index first, commit last. The most likely
failure by far (the embedder is unavailable) then touches neither store.

Moving the write after the embed opens a second problem these tests also pin:
the row is read long before it is written, and ``repo.update`` writes EVERY
column from the object it is given. Committing the pre-embed snapshot would
revert whatever another writer changed meanwhile — including ``is_published``,
which is how a built-in runbook is retired. Hence the re-read at commit time.

The fake below models the ROW, not a single mock object: reads return a fresh
view of current state and a successful commit is what changes it. That is what
lets these tests tell "the commit failed" apart from "the commit landed but
raised" — a distinction the compensation now turns on, and which a
single-shared-mock fake silently collapses.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.models.exceptions import KnowledgeBaseError
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)

pytestmark = [pytest.mark.unit]

_EMBED_TEXTS = "faultmaven.infrastructure.model_cache.model_cache.aembed_texts"
_REPO = (
    "faultmaven.modules.knowledge.infrastructure.persistence."
    "knowledge_item_repository.DatabaseKnowledgeItemRepository"
)

OLD_CONTENT = "# Draining a node\n\nCordon, then drain."
NEW_CONTENT = "# Draining a node\n\nCordon, drain, then uncordon."


def _state():
    """The persisted row, as a plain dict — the source of truth for the fake."""
    return {
        "present": True,
        "title": "Draining a node",
        "content": OLD_CONTENT,
        "tags": [],
        "is_published": True,
        "updated_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }


def _row_from(state):
    """A freshly materialised row view, as `get_by_id` would return one."""
    if not state["present"]:
        return None
    row = MagicMock()
    row.item_id = "doc-1"
    row.title = state["title"]
    row.content = state["content"]
    row.tags = list(state["tags"])
    row.is_published = state["is_published"]
    row.item_type = MagicMock(value="runbook")
    row.source_url = None
    row.scope = MagicMock(value="global")
    row.owner_id = None
    row.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    row.updated_at = state["updated_at"]
    row.metadata = {}
    return row


def _service(state, *, commit_raises=None, commit_lands=True):
    """Service wired so every store write appends to `calls`, in order.

    Args:
        commit_raises: exception `repo.update` raises, if any.
        commit_lands: whether a raising commit nonetheless PERSISTED. Models
            the ack-lost case (connection dropped after the server committed).
    """
    service = KnowledgeService.__new__(KnowledgeService)
    calls = []

    vector_store = MagicMock()

    async def _delete(parent_id):
        calls.append(("vector_delete", parent_id))
        return 1

    async def _add(doc_dicts, embeddings=None):
        # Record what the vectors DESCRIBE, not merely that a write happened —
        # the compensation tests turn on which content was written back.
        calls.append(("vector_add", doc_dicts[0]["content"]))
        calls.append(("chunk_updated_at", doc_dicts[0]["metadata"].get("updated_at")))

    vector_store.delete_documents_by_parent_id = AsyncMock(side_effect=_delete)
    vector_store.add_documents = AsyncMock(side_effect=_add)
    service._vector_store = vector_store
    service._extract_frontmatter_for_rag = staticmethod(lambda content: {})

    service._sanitizer = MagicMock()
    service._sanitizer.asanitize = AsyncMock(side_effect=lambda text: text)

    repo = MagicMock()
    repo.get_by_id = AsyncMock(side_effect=lambda _id: _row_from(state))

    async def _update(obj):
        calls.append(("sql_commit", obj.content))
        if commit_raises is None or commit_lands:
            state["title"] = obj.title
            state["content"] = obj.content
            state["is_published"] = obj.is_published
        if commit_raises is not None:
            raise commit_raises
        return obj

    repo.update = AsyncMock(side_effect=_update)

    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    service._db_session_factory = MagicMock(return_value=session)

    return service, repo, calls


def _kinds(calls):
    return [kind for kind, _ in calls if kind != "chunk_updated_at"]


# ---------------------------------------------------------------------------
# The row must not move when the vectors cannot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_row_is_not_committed_when_the_embedder_is_unavailable():
    """The common failure. Before #952 this committed the new content and left
    the old vectors in place — undetectably mispaired."""
    state = _state()
    service, repo, calls = _service(state)

    with patch(_REPO, return_value=repo):
        with patch(_EMBED_TEXTS, new=AsyncMock(return_value=None)):
            with pytest.raises(KnowledgeBaseError):
                await service.update_document_metadata(
                    document_id="doc-1", content=NEW_CONTENT
                )

    assert calls == [], f"the update wrote to a store despite failing: {calls}"
    assert state["content"] == OLD_CONTENT


@pytest.mark.asyncio
async def test_row_is_not_committed_when_the_vector_swap_fails():
    """The OTHER indexing failure — ChromaDB rejecting the add, AFTER the
    delete. The row must still not move: leaving it correct is what makes the
    missing vectors repairable from it."""
    state = _state()
    service, repo, calls = _service(state)
    service._vector_store.add_documents = AsyncMock(
        side_effect=RuntimeError("chromadb unreachable")
    )

    with patch(_REPO, return_value=repo):
        with patch(_EMBED_TEXTS, new=AsyncMock(return_value=[[0.1] * 1024])):
            with pytest.raises(KnowledgeBaseError):
                await service.update_document_metadata(
                    document_id="doc-1", content=NEW_CONTENT
                )

    assert "sql_commit" not in _kinds(calls)
    assert state["content"] == OLD_CONTENT


# ---------------------------------------------------------------------------
# The ordering itself, on the path that succeeds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vectors_are_written_before_the_row_commits():
    """The gate must be able to pass, and pass in the right ORDER — a fix that
    simply stopped committing would satisfy the failure tests above."""
    state = _state()
    service, repo, calls = _service(state)

    with patch(_REPO, return_value=repo):
        with patch(_EMBED_TEXTS, new=AsyncMock(return_value=[[0.1] * 1024])):
            result = await service.update_document_metadata(
                document_id="doc-1", content=NEW_CONTENT
            )

    assert result["content"] == NEW_CONTENT
    assert _kinds(calls) == ["vector_delete", "vector_add", "sql_commit"], (
        "the SQL commit must be the LAST store write, so no failure before it "
        f"can leave the row ahead of the vectors: {_kinds(calls)}"
    )
    assert ("vector_add", NEW_CONTENT) in calls


@pytest.mark.asyncio
async def test_indexed_chunks_carry_the_updated_timestamp_not_the_previous_one():
    """The chunks are built before the row is written, so their `updated_at`
    would otherwise be the PREVIOUS value — the document would look stale to
    any recency signal the moment it was edited."""
    state = _state()
    service, repo, calls = _service(state)
    before = datetime.now(timezone.utc)

    with patch(_REPO, return_value=repo):
        with patch(_EMBED_TEXTS, new=AsyncMock(return_value=[[0.1] * 1024])):
            await service.update_document_metadata(
                document_id="doc-1", content=NEW_CONTENT
            )

    stamped = [value for kind, value in calls if kind == "chunk_updated_at"]
    assert stamped and stamped[0], "chunks carry no updated_at at all"
    assert (
        stamped[0] >= before.isoformat()
    ), f"chunk metadata carries the pre-update timestamp {stamped[0]!r}"


@pytest.mark.asyncio
async def test_a_content_edit_still_commits_when_no_vector_store_is_wired():
    """A deployment can compose without a vector store — every indexing path
    already no-ops on it. The rollback snapshot must be gated on the same
    condition as the re-index, or taking it would start dereferencing row
    fields on a path that never touched them."""
    state = _state()
    service, repo, calls = _service(state)
    service._vector_store = None

    original = _row_from

    def _row_without_scope(s):
        row = original(s)
        if row is not None:
            # Fields the index model reads, which a store-less deployment
            # never needs — if the snapshot is built anyway, this trips.
            row.scope = None
        return row

    repo.get_by_id = AsyncMock(side_effect=lambda _id: _row_without_scope(state))

    with patch(_REPO, return_value=repo):
        result = await service.update_document_metadata(
            document_id="doc-1", content=NEW_CONTENT
        )

    assert result["content"] == NEW_CONTENT
    assert _kinds(calls) == ["sql_commit"]


@pytest.mark.asyncio
async def test_metadata_only_update_commits_without_touching_the_vectors():
    """Re-indexing is content-triggered. A title/tag edit must still commit —
    otherwise the ordering fix would have disabled metadata editing."""
    state = _state()
    service, repo, calls = _service(state)

    with patch(_REPO, return_value=repo):
        result = await service.update_document_metadata(
            document_id="doc-1", tags=["kubernetes"]
        )

    assert result is not None
    assert _kinds(calls) == ["sql_commit"]


# ---------------------------------------------------------------------------
# The row is re-read at commit time, not written from the stale snapshot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_concurrent_unpublish_is_not_resurrected_by_the_commit():
    """The regression that moving the write after the embed introduces.

    `repo.update` writes every column from the object it is handed. The row is
    now read up to a cold load before it is written, so committing the snapshot
    would silently restore any column another writer changed meanwhile — and
    `delete_document` retires a built-in runbook precisely by setting
    `is_published=False` and dropping its vectors. Since this method re-adds
    vectors, a snapshot write would bring a retired runbook back in BOTH
    stores, with nothing logged.
    """
    state = _state()
    service, repo, calls = _service(state)

    async def _embed_then_someone_unpublishes(texts):
        # The concurrent retirement lands while we are embedding.
        state["is_published"] = False
        return [[0.1] * 1024 for _ in texts]

    with patch(_REPO, return_value=repo):
        with patch(_EMBED_TEXTS, new=_embed_then_someone_unpublishes):
            await service.update_document_metadata(
                document_id="doc-1", content=NEW_CONTENT
            )

    assert (
        state["is_published"] is False
    ), "the update resurrected a runbook that was retired while it embedded"
    assert state["content"] == NEW_CONTENT, "the edit itself should still apply"


@pytest.mark.asyncio
async def test_vectors_are_discarded_when_the_row_was_deleted_mid_update():
    """Deleted while we embedded. Restoring "the previous content" would leave
    chunks for a row that no longer exists, and orphan pruning only covers
    built-in pack ids — an authored document would stay searchable after
    deletion. Undo our own write instead, and report not-found."""
    state = _state()
    service, repo, calls = _service(state)

    async def _embed_then_someone_deletes(texts):
        state["present"] = False
        return [[0.1] * 1024 for _ in texts]

    with patch(_REPO, return_value=repo):
        with patch(_EMBED_TEXTS, new=_embed_then_someone_deletes):
            result = await service.update_document_metadata(
                document_id="doc-1", content=NEW_CONTENT
            )

    assert result is None, "a document deleted mid-update must report not-found"
    # Counted, not merely "a delete happened": the swap itself deletes once
    # before it adds, so `await_count >= 1` is satisfied by the swap and would
    # pass with no compensation at all.
    assert _kinds(calls) == [
        "vector_delete",
        "vector_add",
        "vector_delete",
    ], f"chunks for a deleted row were left behind: {_kinds(calls)}"


# ---------------------------------------------------------------------------
# The residual: vectors written, commit failed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failed_commit_puts_the_previous_vectors_back():
    """The one window the ordering cannot close on its own. If the row fails to
    commit after the swap, the vectors are ahead of it — the undetectable kind
    of mispairing — so it is compensated rather than tolerated."""
    state = _state()
    service, repo, calls = _service(
        state, commit_raises=RuntimeError("database gone"), commit_lands=False
    )

    with patch(_REPO, return_value=repo):
        with patch(_EMBED_TEXTS, new=AsyncMock(return_value=[[0.1] * 1024])):
            with pytest.raises(RuntimeError):
                await service.update_document_metadata(
                    document_id="doc-1", content=NEW_CONTENT
                )

    adds = [content for kind, content in calls if kind == "vector_add"]
    assert adds == [NEW_CONTENT, OLD_CONTENT], (
        "after the commit failed the vectors must be restored to the content "
        f"the row still holds: {adds}"
    )


@pytest.mark.asyncio
async def test_a_commit_that_landed_but_raised_is_not_rolled_back():
    """A raised commit does NOT prove the write failed — a connection dropped
    after the server committed raises here with the new content durably stored.

    Restoring the previous content on that signal alone would re-index
    superseded text under an updated row: the compensation would manufacture
    the exact mispairing #952 exists to prevent. So the row is re-read and the
    OBSERVED state decides.
    """
    state = _state()
    service, repo, calls = _service(
        state, commit_raises=RuntimeError("connection lost"), commit_lands=True
    )

    with patch(_REPO, return_value=repo):
        with patch(_EMBED_TEXTS, new=AsyncMock(return_value=[[0.1] * 1024])):
            with pytest.raises(RuntimeError):
                await service.update_document_metadata(
                    document_id="doc-1", content=NEW_CONTENT
                )

    adds = [content for kind, content in calls if kind == "vector_add"]
    assert adds == [NEW_CONTENT], (
        "the compensation re-indexed the OLD content over a row that actually "
        f"holds the new content — it created the mispairing: {adds}"
    )


@pytest.mark.asyncio
async def test_a_failed_restore_names_the_state_and_tells_the_operator_what_to_do():
    """If the compensation fails too, nothing downstream can find the state —
    the row has vectors and looks healthy. The log is the only signal, so it
    must both name the condition and carry the recovery step; a bare marker
    with no remedy leaves the operator with a greppable word and no action."""
    state = _state()
    service, repo, calls = _service(
        state, commit_raises=RuntimeError("database gone"), commit_lands=False
    )
    service._vector_store.add_documents = AsyncMock(
        side_effect=[None, RuntimeError("chromadb gone too")]
    )

    with patch(_REPO, return_value=repo):
        with patch(_EMBED_TEXTS, new=AsyncMock(return_value=[[0.1] * 1024])):
            with patch(
                "faultmaven.modules.knowledge.domain.services."
                "knowledge_service.logger"
            ) as log:
                with pytest.raises(RuntimeError):
                    await service.update_document_metadata(
                        document_id="doc-1", content=NEW_CONTENT
                    )

    logged = " ".join(
        str(arg) for call in log.error.call_args_list for arg in call.args
    )
    assert "MISPAIRED" in logged, "the state is not named"
    assert "doc-1" in logged, "the affected document is not identified"
    assert (
        "re-save" in logged.lower()
    ), "the operator is told a bad thing happened but not what to do about it"
