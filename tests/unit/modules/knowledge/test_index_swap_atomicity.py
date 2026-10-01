"""A failed re-index must not destroy the document it was re-indexing (#945).

``_index_document_in_vector_store`` was described in the code above its call
site as a "delete+add atomic swap". It was neither atomic nor a swap: it
deleted the old vectors FIRST, then embedded. When the embedder was
unavailable it returned the sentinel ``0`` with the old vectors already gone.

``update_document_metadata`` ignored that return value and logged
"Successfully updated document"; the API answered 200. Net result: the SQL row
held the new content, no vectors existed, and the document was permanently
unsearchable — with the row looking healthy to every later consistency check,
so nothing would ever repair it.

A sentinel that only some callers check is the wrong shape for a data-integrity
signal (``ingest_runbook`` checked it; ``update_document_metadata``,
``ingest_document`` and ``update_document`` did not). It is now an exception,
so callers fail closed by default and tolerance is opt-in.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.models import KnowledgeBaseDocument
from faultmaven.models.exceptions import KnowledgeBaseError
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)

pytestmark = [pytest.mark.unit]

_EMBED_TEXTS = "faultmaven.infrastructure.model_cache.model_cache.aembed_texts"


def _service() -> KnowledgeService:
    service = KnowledgeService.__new__(KnowledgeService)
    vector_store = MagicMock()
    vector_store.delete_documents_by_parent_id = AsyncMock()
    vector_store.add_documents = AsyncMock()
    service._vector_store = vector_store
    service._extract_frontmatter_for_rag = staticmethod(lambda content: {})
    return service


def _document() -> KnowledgeBaseDocument:
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


# ---------------------------------------------------------------------------
# The destructive step must not run before its replacement exists
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_old_vectors_survive_when_embedding_fails():
    """The core property. If the delete has already happened when embedding
    fails, the document is unrecoverable — this is the data loss in #945."""
    service = _service()

    with patch(_EMBED_TEXTS, new=AsyncMock(return_value=None)):
        with pytest.raises(KnowledgeBaseError):
            await service._index_document_in_vector_store(
                _document(), enterprise_id=STANDALONE_ENTERPRISE_ID
            )

    assert service._vector_store.delete_documents_by_parent_id.await_count == 0, (
        "the old vectors were deleted before a replacement existed — the "
        "document is now permanently unsearchable"
    )


@pytest.mark.asyncio
async def test_unchunkable_content_also_deletes_nothing():
    """The other early-return that used to sit AFTER the delete."""
    service = _service()
    document = _document()
    document.content = "   "

    with patch(_EMBED_TEXTS, new=AsyncMock(return_value=[[0.1] * 1024])):
        with pytest.raises(KnowledgeBaseError):
            await service._index_document_in_vector_store(
                document, enterprise_id=STANDALONE_ENTERPRISE_ID
            )

    assert service._vector_store.delete_documents_by_parent_id.await_count == 0


@pytest.mark.asyncio
async def test_the_swap_still_happens_on_the_success_path():
    """The gate must be able to pass: a healthy re-index still replaces the
    old vectors, or this "fix" would simply have disabled re-indexing."""
    service = _service()

    with patch(_EMBED_TEXTS, new=AsyncMock(return_value=[[0.1] * 1024])):
        chunks = await service._index_document_in_vector_store(
            _document(), enterprise_id=STANDALONE_ENTERPRISE_ID
        )

    assert chunks >= 1
    assert service._vector_store.delete_documents_by_parent_id.await_count == 1
    assert service._vector_store.add_documents.await_count == 1


# ---------------------------------------------------------------------------
# Failure must reach the caller, not a sentinel half of them ignore
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_indexing_failure_raises_rather_than_returning_zero():
    service = _service()

    with patch(_EMBED_TEXTS, new=AsyncMock(return_value=None)):
        with pytest.raises(KnowledgeBaseError) as excinfo:
            await service._index_document_in_vector_store(
                _document(), enterprise_id=STANDALONE_ENTERPRISE_ID
            )

    assert excinfo.value.error_code == "KNOWLEDGE_EMBEDDER_UNAVAILABLE"


@pytest.mark.asyncio
async def test_the_trailing_blanket_handler_does_not_restore_the_sentinel():
    """``_index_document_in_vector_store`` ends in ``except Exception``. A
    typed raise that a handler swallows one frame later is not a fix — the
    same shape that made #868's first attempt inert."""
    service = _service()
    service._vector_store.add_documents = AsyncMock(
        side_effect=RuntimeError("chromadb unreachable")
    )

    with patch(_EMBED_TEXTS, new=AsyncMock(return_value=[[0.1] * 1024])):
        result = None
        try:
            result = await service._index_document_in_vector_store(
                _document(), enterprise_id=STANDALONE_ENTERPRISE_ID
            )
        except KnowledgeBaseError:
            pass

    assert result is None, "an indexing failure was flattened back into a return value"


@pytest.mark.asyncio
async def test_update_document_metadata_does_not_report_success_on_failure():
    """The live route defect: PUT /knowledge/documents/{id} answered 200 over a
    document whose vectors it had just destroyed."""
    service = _service()
    service._sanitizer = MagicMock()
    service._sanitizer.asanitize = AsyncMock(side_effect=lambda text: text)

    item = MagicMock()
    item.item_id = "doc-1"
    item.enterprise_id = STANDALONE_ENTERPRISE_ID  # NOT NULL on every row (#1168)
    item.title = "Draining a node"
    item.content = "# Draining a node\n\nCordon, then drain."
    item.item_type = MagicMock(value="runbook")
    item.tags = []
    item.source_url = None
    item.scope = MagicMock(value="global")
    item.owner_id = None
    item.created_at = None
    item.updated_at = None
    item.metadata = {}

    repo = MagicMock()
    repo.get_by_id = AsyncMock(return_value=item)
    repo.update = AsyncMock()

    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    service._db_session_factory = MagicMock(return_value=session)

    with patch(
        "faultmaven.modules.knowledge.infrastructure.persistence."
        "knowledge_item_repository.DatabaseKnowledgeItemRepository",
        return_value=repo,
    ):
        with patch(_EMBED_TEXTS, new=AsyncMock(return_value=None)):
            with pytest.raises(KnowledgeBaseError):
                await service.update_document_metadata(
                    document_id="doc-1", content="# New content\n\nUpdated."
                )

    assert (
        service._vector_store.delete_documents_by_parent_id.await_count == 0
    ), "vectors were destroyed on a path that then reported success"


# ---------------------------------------------------------------------------
# The one caller that legitimately tolerates failure must do so explicitly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_boot_repair_still_tolerates_an_unavailable_embedder():
    """``reindex_missing_vectors`` is a bounded best-effort repair run at boot;
    a failed repair must not abort startup. That tolerance is now an explicit
    ``except KnowledgeBaseError`` at one call site rather than a sentinel every
    caller silently inherited."""
    service = _service()

    row = MagicMock()
    row.item_id = "doc-1"
    row.enterprise_id = STANDALONE_ENTERPRISE_ID  # NOT NULL on every row (#1168)
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

    with patch(_EMBED_TEXTS, new=AsyncMock(return_value=None)):
        chunks = await service.reindex_missing_vectors("doc-1")

    assert chunks == 0, "boot repair must degrade, not raise"
    assert service._vector_store.delete_documents_by_parent_id.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "enterprise_id", ["", "   ", None], ids=["empty", "blank", "none"]
)
async def test_boot_repair_counts_a_row_with_no_tenant_as_a_failed_repair(
    enterprise_id, caplog
):
    """``reindex_missing_vectors`` promises "fail-safe 0 (no raise)". The
    indexer's #1168 tenant check raises ``ValueError`` before any work, outside
    the ``KnowledgeBaseError`` handling — so a row naming no usable tenant must
    be caught here, logged at ERROR with its item id, and returned as a failed
    repair (0), never allowed to abort the boot pass. Unreachable from a real
    row (the column is NOT NULL), which is why only a double can reach it."""
    service = _service()

    row = MagicMock()
    row.item_id = "doc-no-tenant"
    row.enterprise_id = enterprise_id
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
    embed = AsyncMock(return_value=[[0.1] * 1024])

    with caplog.at_level("ERROR"), patch(_EMBED_TEXTS, new=embed):
        chunks = await service.reindex_missing_vectors("doc-no-tenant")

    assert chunks == 0, "a refused repair must degrade to 0, not raise"
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert any(
        "doc-no-tenant" in r.getMessage() for r in errors
    ), "the refused repair was not logged at ERROR with its item id"
    embed.assert_not_awaited()
    assert service._vector_store.delete_documents_by_parent_id.await_count == 0
    assert service._vector_store.add_documents.await_count == 0


# ---------------------------------------------------------------------------
# The human-facing search surface must not read as "nothing matched" either
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_semantic_search_says_unavailable_not_zero_results():
    """``POST /knowledge/search`` returns ``total_results: 0`` on failure.

    The count cannot change without breaking the response contract, so the
    ``error`` text carries the distinction: a client rendering the count would
    otherwise show "no results found" for a search that never ran — the same
    affirmative negative closed on the agent path in #943, on the human surface.
    """
    from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
        KnowledgeVectorStore,
    )

    service = KnowledgeService.__new__(KnowledgeService)
    store = KnowledgeVectorStore(client=MagicMock())
    store._get_or_create_collection = MagicMock(return_value=MagicMock())
    service._vector_store = store
    service._sanitizer = MagicMock()
    service._sanitizer.asanitize = AsyncMock(side_effect=lambda text: text)
    service._share_repo = None

    user = MagicMock()
    user.user_id = "u1"

    with patch(
        "faultmaven.infrastructure.model_cache.model_cache.aembed_query",
        new=AsyncMock(return_value=None),
    ):
        result = await service.search_documents(query="drain a node", user=user)

    assert result["total_results"] == 0
    assert "unavailable" in result["error"].lower()
    assert "not a result of zero matches" in result["error"].lower()


# ---------------------------------------------------------------------------
# The tenant argument is checked first, before any work (#1168)
# ---------------------------------------------------------------------------

#: Every shape that names no tenant. ``7`` and ``b"ent"`` are the non-strings a
#: stringifying layer would have turned into a value that passes a non-blank
#: check and matches no enterprise.
_NO_TENANT = ["", "   ", None, 7, b"ent"]
_NO_TENANT_IDS = ["empty", "blank", "none", "int", "bytes"]


@pytest.mark.asyncio
@pytest.mark.parametrize("enterprise_id", _NO_TENANT, ids=_NO_TENANT_IDS)
async def test_no_tenant_is_refused_before_the_embedder_is_awaited(enterprise_id):
    """Refused before chunking or embedding — a call that cannot be written must
    not pay for a cold BGE-M3 load first — and before the destructive delete,
    so the old vectors are untouched. A ``ValueError``: the caller's programming
    error, not the transient ``KNOWLEDGE_INDEXING_FAILED``."""
    service = _service()
    embed = AsyncMock(return_value=[[0.1] * 1024])

    with patch(_EMBED_TEXTS, new=embed):
        with pytest.raises(ValueError, match="carries no enterprise_id"):
            await service._index_document_in_vector_store(
                _document(), enterprise_id=enterprise_id
            )

    embed.assert_not_awaited()
    assert service._vector_store.delete_documents_by_parent_id.await_count == 0
    assert service._vector_store.add_documents.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("enterprise_id", _NO_TENANT, ids=_NO_TENANT_IDS)
async def test_no_tenant_is_refused_even_with_no_vector_store_wired(enterprise_id):
    """Ahead of the ``if not self._vector_store: return 0`` exit, like the tier
    check: a refusal that only fires in deployments with a store is not a
    guard, and the caller's omission is the same omission either way."""
    service = _service()
    service._vector_store = None

    with pytest.raises(ValueError, match="carries no enterprise_id"):
        await service._index_document_in_vector_store(
            _document(), enterprise_id=enterprise_id
        )


@pytest.mark.asyncio
async def test_a_named_tenant_with_no_store_is_still_the_quiet_zero():
    """Positive control for the test above: the early exit itself survives."""
    service = _service()
    service._vector_store = None

    assert (
        await service._index_document_in_vector_store(
            _document(), enterprise_id="ent-given"
        )
        == 0
    )


@pytest.mark.asyncio
async def test_every_chunk_carries_the_enterprise_it_was_given():
    service = _service()

    with patch(_EMBED_TEXTS, new=AsyncMock(return_value=[[0.1] * 1024])):
        await service._index_document_in_vector_store(
            _document(), enterprise_id="ent-given"
        )

    (documents,), _ = service._vector_store.add_documents.await_args
    assert documents, "nothing was written"
    assert {d["metadata"]["enterprise_id"] for d in documents} == {"ent-given"}
