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
the row is read long before it is written, and a write of EVERY column from
that read would revert whatever another writer changed meanwhile — including
``is_published``, which is how a built-in runbook is retired. Hence the re-read
at commit time, and (#1966) a targeted write of only the changed fields,
followed by a read of the row as committed.

What none of this does any more is delete vectors (#2397, #2388). The update
writes its chunks under a new ``vector_generation`` and then writes the content
and that generation in one UPDATE, so the row and the chunks it serves always
describe the same text: a failed commit leaves the old content served, a commit
that landed despite raising serves the new, and two racing updates serve
whichever committed last. Every KB door admits a chunk only when its row is
complete and published and carries the chunk's generation
(``admitted_generations``, which the fake below answers from the row it
models).

The fake below models the ROW, not a single mock object: reads return a fresh
view of current state and a successful commit is what changes it. That is what
lets these tests tell "the commit failed" apart from "the commit landed but
raised", which a single-shared-mock fake silently collapses.
"""

import asyncio
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

COMMITTED_AT = datetime(2026, 6, 1, tzinfo=timezone.utc)
OLD_CONTENT = "# Draining a node\n\nCordon, then drain."
NEW_CONTENT = "# Draining a node\n\nCordon, drain, then uncordon."
OTHER_CONTENT = "# Draining a node\n\nCordon, drain, verify, uncordon."
OLD_GENERATION = "00" * 16


def _state():
    """The persisted row, as a plain dict — the source of truth for the fake —
    and the chunks in the store, as ``(content, generation)``."""
    return {
        "present": True,
        "title": "Draining a node",
        "content": OLD_CONTENT,
        "tags": [],
        "is_published": True,
        "updated_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "generation": OLD_GENERATION,
        "chunks": [(OLD_CONTENT, OLD_GENERATION)],
    }


async def _served(service, state, repo):
    """What a KB door serves for the document: the chunks the real admission
    check admits, over the modelled row."""
    with patch(_REPO, return_value=repo):
        admitted = (await service.admitted_generations(["doc-1"])).get("doc-1")
    return [
        content for content, generation in state["chunks"] if generation == admitted
    ]


def _row_from(state):
    """A freshly materialised row view, as `get_by_id` would return one."""
    if not state["present"]:
        return None
    row = MagicMock()
    row.item_id = "doc-1"
    # The NOT NULL isolation key every persisted row carries, and the value
    # the indexer stamps on its chunks (#1168) — a bare MagicMock attribute is
    # not a tenant, and the indexer refuses it before doing any work.
    row.enterprise_id = "ent-row"
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
    row.vector_generation = state["generation"]
    return row


def _service(state, *, commit_raises=None, commit_lands=True):
    """Service wired so every store write appends to `calls`, in order.

    Args:
        commit_raises: exception the targeted write (`repo.update_fields`) raises, if any.
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
        # Record what the vectors DESCRIBE, not merely that a write happened.
        calls.append(("vector_add", doc_dicts[0]["content"]))
        calls.append(("chunk_updated_at", doc_dicts[0]["metadata"].get("updated_at")))
        state["chunks"].append(
            (doc_dicts[0]["content"], doc_dicts[0]["metadata"]["vector_generation"])
        )

    async def _delete_generation(parent_id, generation):
        # The writer's one delete, after a committed switch (ruling (i)).
        calls.append(("generation_delete", generation))
        gone = [c for c in state["chunks"] if c[1] == generation]
        state["chunks"] = [c for c in state["chunks"] if c[1] != generation]
        return gone

    vector_store.delete_documents_by_parent_id = AsyncMock(side_effect=_delete)
    vector_store.delete_generation = AsyncMock(side_effect=_delete_generation)
    vector_store.add_documents = AsyncMock(side_effect=_add)
    service._vector_store = vector_store
    service._extract_frontmatter_for_rag = staticmethod(lambda content: {})

    service._sanitizer = MagicMock()
    service._sanitizer.asanitize = AsyncMock(side_effect=lambda text: text)

    repo = MagicMock()
    repo.get_by_id = AsyncMock(side_effect=lambda _id: _row_from(state))
    # The admission check's query, over the modelled row: complete is implied
    # (an update refuses an incomplete row before it starts).
    repo.admitted_generations = AsyncMock(
        side_effect=lambda ids: {
            i: state["generation"]
            for i in ids
            if i == "doc-1" and state["present"] and state["is_published"]
        }
    )

    async def _update_fields(item_id, fields, *, commit=True, touch=True):
        # The targeted write (#1966): only the named fields move, so a column
        # another writer changed — `is_published` above all — keeps its value.
        calls.append(("sql_commit", fields.get("content", state["content"])))
        if not state["present"]:
            return False
        if commit_raises is None or commit_lands:
            for name in ("title", "content", "tags"):
                if name in fields:
                    state[name] = fields[name]
            if "vector_generation" in fields:
                state["generation"] = fields["vector_generation"]
            if touch:
                state["updated_at"] = COMMITTED_AT
        if commit_raises is not None:
            raise commit_raises
        return True

    repo.update_fields = AsyncMock(side_effect=_update_fields)

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
    assert _kinds(calls) == ["vector_add", "sql_commit", "generation_delete"], (
        "the chunks first, then the commit, then — and only then — the delete "
        f"of the generation the commit replaced: {_kinds(calls)}"
    )
    assert ("generation_delete", OLD_GENERATION) in calls
    assert await _served(service, state, repo) == [NEW_CONTENT]
    # Only the new generation's chunks are left.
    assert [content for content, _ in state["chunks"]] == [NEW_CONTENT]


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

    A write of every column would put back what was read up to a cold load
    earlier, silently restoring any column another writer changed meanwhile — and
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
async def test_a_row_deleted_mid_update_leaves_no_admitted_vector():
    """Deleted while we embedded. The update reports not-found and makes no
    vector delete of its own (#2397): the chunks it wrote sit under an id with
    no row, which no KB door admits; only the authored-orphan sweep, once an
    operator arms it, removes them."""
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
        admitted = await service.admitted_generations(["doc-1"])

    assert result is None, "a document deleted mid-update must report not-found"
    # The update's own add, and nothing after it.
    assert _kinds(calls) == ["vector_add"], _kinds(calls)
    assert admitted == {}


# ---------------------------------------------------------------------------
# Vectors written, then the commit: what is served is what the row holds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failed_commit_serves_the_previous_content():
    """The row keeps its content and generation, so its old chunks are still
    the served ones and the new chunks are superseded (#2388): nothing is
    restored, and nothing is deleted."""
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

    assert _kinds(calls) == ["vector_add", "sql_commit"], _kinds(calls)
    assert await _served(service, state, repo) == [OLD_CONTENT]


@pytest.mark.asyncio
async def test_a_commit_that_landed_but_raised_serves_the_new_content():
    """A raised commit does NOT prove the write failed — a connection dropped
    after the server committed raises here with the new content durably stored,
    and with it the generation its chunks carry."""
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

    assert _kinds(calls) == ["vector_add", "sql_commit"], _kinds(calls)
    assert await _served(service, state, repo) == [NEW_CONTENT]


@pytest.mark.asyncio
@pytest.mark.parametrize("last", [NEW_CONTENT, OTHER_CONTENT], ids=["first", "second"])
async def test_two_racing_updates_serve_the_content_that_committed(last):
    """Both updates write their chunks, then their UPDATEs land in either order:
    the row ends on the content and the generation of the same call, so the
    content served is the content committed, whichever call it was."""
    state = _state()
    service, repo, calls = _service(state)
    gates = {NEW_CONTENT: asyncio.Event(), OTHER_CONTENT: asyncio.Event()}
    write = repo.update_fields.side_effect

    async def in_order(item_id, fields, **kwargs):
        await gates[fields["content"]].wait()
        return await write(item_id, fields, **kwargs)

    repo.update_fields = AsyncMock(side_effect=in_order)
    first = OTHER_CONTENT if last == NEW_CONTENT else NEW_CONTENT

    with patch(_REPO, return_value=repo):
        with patch(_EMBED_TEXTS, new=AsyncMock(return_value=[[0.1] * 1024])):
            updates = [
                asyncio.create_task(
                    service.update_document_metadata(document_id="doc-1", content=c)
                )
                for c in (NEW_CONTENT, OTHER_CONTENT)
            ]
            while _kinds(calls).count("vector_add") < 2:
                await asyncio.sleep(0)
            gates[first].set()
            while "sql_commit" not in _kinds(calls):
                await asyncio.sleep(0)
            gates[last].set()
            await asyncio.gather(*updates)

    assert state["content"] == last
    assert await _served(service, state, repo) == [last]
    assert "vector_delete" not in _kinds(calls)
    # Each landed update deletes only the generation it read before its write;
    # neither deletes a generation the row serves.
    assert {g for kind, g in calls if kind == "generation_delete"} == {OLD_GENERATION}


# ---------------------------------------------------------------------------
# The response describes the COMMITTED row, and the whole write is compensated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_concurrent_unpublish_leaves_the_vectors_we_added_unadmitted():
    """Re-reading fixes the SQL half of a retirement. The chunks this update
    already re-added stay, and are never served: no KB door admits a chunk
    whose row is unpublished (#2397), so nothing is deleted in-process."""
    state = _state()
    service, repo, calls = _service(state)

    async def _embed_then_someone_unpublishes(texts):
        state["is_published"] = False
        return [[0.1] * 1024 for _ in texts]

    with patch(_REPO, return_value=repo):
        with patch(_EMBED_TEXTS, new=_embed_then_someone_unpublishes):
            await service.update_document_metadata(
                document_id="doc-1", content=NEW_CONTENT
            )
        admitted = await service.admitted_generations(["doc-1"])

    assert state["is_published"] is False, "the retirement was reverted in SQL"
    assert _kinds(calls) == ["vector_add", "sql_commit", "generation_delete"]
    assert admitted == {}


@pytest.mark.asyncio
async def test_a_failed_commit_on_a_row_unpublished_meanwhile_writes_nothing_more():
    """A row unpublished meanwhile, whose update then fails to commit, gets no
    further write and no vector delete (#2397): its vectors are never served."""
    state = _state()
    service, repo, calls = _service(
        state, commit_raises=RuntimeError("database gone"), commit_lands=False
    )

    async def _embed_then_someone_unpublishes(texts):
        state["is_published"] = False
        return [[0.1] * 1024 for _ in texts]

    with patch(_REPO, return_value=repo):
        with patch(_EMBED_TEXTS, new=_embed_then_someone_unpublishes):
            with pytest.raises(RuntimeError):
                await service.update_document_metadata(
                    document_id="doc-1", content=NEW_CONTENT
                )
        admitted = await service.admitted_generations(["doc-1"])

    assert _kinds(calls) == ["vector_add", "sql_commit"], _kinds(calls)
    assert admitted == {}


@pytest.mark.asyncio
async def test_a_failed_generation_delete_is_logged_and_the_update_stands(caplog):
    """The writer's delete is best effort: a store that refuses it leaves the
    replaced generation to the generation sweep, and the update succeeds."""
    state = _state()
    service, repo, calls = _service(state)
    service._vector_store.delete_generation = AsyncMock(
        side_effect=OSError("chromadb gone")
    )

    with patch(_REPO, return_value=repo):
        with patch(_EMBED_TEXTS, new=AsyncMock(return_value=[[0.1] * 1024])):
            result = await service.update_document_metadata(
                document_id="doc-1", content=NEW_CONTENT
            )

    assert result["content"] == NEW_CONTENT
    assert await _served(service, state, repo) == [NEW_CONTENT]
    assert "Could not delete the replaced generation" in caplog.text
    assert OLD_GENERATION in caplog.text


@pytest.mark.asyncio
async def test_the_response_describes_the_committed_row_not_the_snapshot():
    """The row is read long before it is written. Reporting the snapshot back
    would return the PRE-EDIT timestamp, and would describe a concurrent
    writer's field as whatever we happened to read at the start."""
    state = _state()
    service, repo, calls = _service(state)

    async def _embed_then_someone_retitles(texts):
        state["title"] = "Renamed by someone else"
        return [[0.1] * 1024 for _ in texts]

    with patch(_REPO, return_value=repo):
        with patch(_EMBED_TEXTS, new=_embed_then_someone_retitles):
            result = await service.update_document_metadata(
                document_id="doc-1", content=NEW_CONTENT
            )

    assert (
        result["updated_at"] == COMMITTED_AT.isoformat()
    ), "the response carries the pre-edit timestamp from the snapshot"
    assert (
        result["title"] == "Renamed by someone else"
    ), "the response describes a title that was never stored"


@pytest.mark.asyncio
async def test_a_failing_re_read_leaves_the_previous_content_served():
    """A re-read that raises after the chunks are written fails the update with
    the row unchanged, so its old chunks are still the served ones."""
    state = _state()
    service, repo, calls = _service(state)

    reads = {"n": 0}

    async def _read_then_fail(_id):
        reads["n"] += 1
        if reads["n"] == 2:  # the re-read inside the write session
            raise RuntimeError("connection lost before the re-read")
        return _row_from(state)

    repo.get_by_id = AsyncMock(side_effect=_read_then_fail)

    with patch(_REPO, return_value=repo):
        with patch(_EMBED_TEXTS, new=AsyncMock(return_value=[[0.1] * 1024])):
            with pytest.raises(RuntimeError):
                await service.update_document_metadata(
                    document_id="doc-1", content=NEW_CONTENT
                )

    assert _kinds(calls) == ["vector_add"], _kinds(calls)
    assert await _served(service, state, repo) == [OLD_CONTENT]
