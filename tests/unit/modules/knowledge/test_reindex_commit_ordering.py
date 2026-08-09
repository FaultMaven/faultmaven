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

These tests pin the ordering itself, not one instance of it — a fix that
happened to work for the embedder-unavailable case while still committing early
on a ChromaDB failure would pass a narrower test and leave the mispairing
reachable.
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


def _item():
    """A ``knowledge_items`` row as loaded for update."""
    item = MagicMock()
    item.item_id = "doc-1"
    item.title = "Draining a node"
    item.content = OLD_CONTENT
    item.item_type = MagicMock(value="runbook")
    item.tags = []
    item.source_url = None
    item.scope = MagicMock(value="global")
    item.owner_id = None
    item.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    item.updated_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    item.metadata = {}
    return item


def _service(item, *, calls=None):
    """A service wired so every store write appends to ``calls`` in order."""
    service = KnowledgeService.__new__(KnowledgeService)
    calls = [] if calls is None else calls

    vector_store = MagicMock()

    async def _delete(parent_id):
        calls.append(("vector_delete", parent_id))
        return 1

    async def _add(doc_dicts, embeddings=None):
        # Record what the vectors will DESCRIBE, not just that a write happened
        # — the compensation test turns on which content was written back.
        calls.append(("vector_add", doc_dicts[0]["content"]))

    vector_store.delete_documents_by_parent_id = AsyncMock(side_effect=_delete)
    vector_store.add_documents = AsyncMock(side_effect=_add)
    service._vector_store = vector_store
    service._extract_frontmatter_for_rag = staticmethod(lambda content: {})

    service._sanitizer = MagicMock()
    service._sanitizer.asanitize = AsyncMock(side_effect=lambda text: text)

    repo = MagicMock()
    repo.get_by_id = AsyncMock(return_value=item)

    async def _update(updated):
        calls.append(("sql_commit", updated.content))
        return updated

    repo.update = AsyncMock(side_effect=_update)

    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    service._db_session_factory = MagicMock(return_value=session)

    return service, repo, calls


# ---------------------------------------------------------------------------
# The row must not move when the vectors cannot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_row_is_not_committed_when_the_embedder_is_unavailable():
    """The common failure. Before #952 this committed the new content and left
    the old vectors in place — undetectably mispaired."""
    item = _item()
    service, repo, calls = _service(item)

    with patch(_REPO, return_value=repo):
        with patch(_EMBED_TEXTS, new=AsyncMock(return_value=None)):
            with pytest.raises(KnowledgeBaseError):
                await service.update_document_metadata(
                    document_id="doc-1", content=NEW_CONTENT
                )

    assert calls == [], "the update wrote to a store despite failing: " f"{calls}"
    repo.update.assert_not_awaited()


@pytest.mark.asyncio
async def test_row_is_not_committed_when_the_vector_swap_fails():
    """The OTHER indexing failure — ChromaDB rejecting the add, AFTER the
    delete. The row must still not move: leaving it correct is what makes the
    missing vectors repairable from it."""
    item = _item()
    service, repo, calls = _service(item)
    service._vector_store.add_documents = AsyncMock(
        side_effect=RuntimeError("chromadb unreachable")
    )

    with patch(_REPO, return_value=repo):
        with patch(_EMBED_TEXTS, new=AsyncMock(return_value=[[0.1] * 1024])):
            with pytest.raises(KnowledgeBaseError):
                await service.update_document_metadata(
                    document_id="doc-1", content=NEW_CONTENT
                )

    repo.update.assert_not_awaited()
    assert not any(kind == "sql_commit" for kind, _ in calls)


# ---------------------------------------------------------------------------
# The ordering itself, on the path that succeeds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vectors_are_written_before_the_row_commits():
    """The gate must be able to pass, and pass in the right ORDER — a fix that
    simply stopped committing would satisfy the failure tests above."""
    item = _item()
    service, repo, calls = _service(item)

    with patch(_REPO, return_value=repo):
        with patch(_EMBED_TEXTS, new=AsyncMock(return_value=[[0.1] * 1024])):
            result = await service.update_document_metadata(
                document_id="doc-1", content=NEW_CONTENT
            )

    assert result["content"] == NEW_CONTENT
    kinds = [kind for kind, _ in calls]
    assert kinds == ["vector_delete", "vector_add", "sql_commit"], (
        "the SQL commit must be the LAST store write, so no failure before it "
        f"can leave the row ahead of the vectors: {kinds}"
    )
    assert ("vector_add", NEW_CONTENT) in calls


@pytest.mark.asyncio
async def test_a_content_edit_still_commits_when_no_vector_store_is_wired():
    """A deployment can compose without a vector store — every indexing path
    already no-ops on it. The rollback snapshot must be gated on the same
    condition as the re-index, or taking it would start dereferencing row
    fields on a path that never touched them."""
    item = _item()
    service, repo, calls = _service(item)
    service._vector_store = None
    # Fields the index model reads, but a store-less deployment never needs.
    item.scope = None
    item.item_type.value = "runbook"

    with patch(_REPO, return_value=repo):
        result = await service.update_document_metadata(
            document_id="doc-1", content=NEW_CONTENT
        )

    assert result["content"] == NEW_CONTENT
    assert [kind for kind, _ in calls] == ["sql_commit"]


@pytest.mark.asyncio
async def test_metadata_only_update_commits_without_touching_the_vectors():
    """Re-indexing is content-triggered. A title/tag edit must still commit —
    otherwise the ordering fix would have disabled metadata editing."""
    item = _item()
    service, repo, calls = _service(item)

    with patch(_REPO, return_value=repo):
        result = await service.update_document_metadata(
            document_id="doc-1", tags=["kubernetes"]
        )

    assert result is not None
    assert [kind for kind, _ in calls] == ["sql_commit"]


# ---------------------------------------------------------------------------
# The residual: vectors written, commit failed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failed_commit_puts_the_previous_vectors_back():
    """The one window the ordering cannot close on its own. If the row fails to
    commit after the swap, the vectors are ahead of it — and that mispairing is
    the undetectable kind, so it is compensated rather than tolerated."""
    item = _item()
    service, repo, calls = _service(item)
    repo.update = AsyncMock(side_effect=RuntimeError("database gone"))

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
async def test_vectors_are_removed_when_the_row_was_deleted_mid_update():
    """`repo.update` raises ValueError on rowcount == 0 — the row was deleted
    while we were embedding. Restoring "the previous content" would then leave
    chunks for a row that no longer exists, and the reconcile pass only prunes
    orphan vectors for built-in pack ids, so an authored document would stay
    searchable after deletion. The right compensation here is the opposite
    one: undo our own write."""
    item = _item()
    service, repo, calls = _service(item)
    repo.update = AsyncMock(side_effect=ValueError("Knowledge item doc-1 not found"))

    with patch(_REPO, return_value=repo):
        with patch(_EMBED_TEXTS, new=AsyncMock(return_value=[[0.1] * 1024])):
            with pytest.raises(ValueError):
                await service.update_document_metadata(
                    document_id="doc-1", content=NEW_CONTENT
                )

    # Counted, not merely "a delete happened": the swap itself deletes once
    # before it adds, so `await_count >= 1` is satisfied by the swap and would
    # pass with no compensation at all. The compensating removal is the SECOND
    # delete, and nothing may be re-added after it.
    kinds = [kind for kind, _ in calls]
    assert kinds == ["vector_delete", "vector_add", "vector_delete"], (
        "the chunks written for a row that no longer exists were left behind, "
        f"or the previous content was restored under a deleted row: {kinds}"
    )


@pytest.mark.asyncio
async def test_a_failed_restore_is_reported_as_a_mispairing_by_name():
    """If the compensation fails too, nothing downstream can find the state —
    the row has vectors and looks healthy. The log line is the only signal, so
    it has to name what happened rather than log a generic failure."""
    item = _item()
    service, repo, calls = _service(item)
    repo.update = AsyncMock(side_effect=RuntimeError("database gone"))

    add = AsyncMock(side_effect=[None, RuntimeError("chromadb gone too")])
    service._vector_store.add_documents = add

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

    assert log.error.called
    logged = " ".join(str(a) for call in log.error.call_args_list for a in call.args)
    assert "MISPAIRED" in logged
    assert "doc-1" in logged
