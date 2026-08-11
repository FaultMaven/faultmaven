"""Runbook dedup's scope predicate, proven against a real ChromaDB (fm#1030).

``search_runbooks`` resolves by similarity, not by id: nothing about the query
names a principal, so the caller-supplied KB scope filter is the ONLY thing
keeping one user's personal runbooks out of another's results. These tests pin
that it is mandatory, that it fails closed, and that it actually filters —
with the filter built by the REAL ``build_kb_scope_filter`` (the consumer's
semantics, not a hand-rolled copy).

**Both halves of the round trip are the real code.** Rows are written through
the production ``ChromaDBVectorStore.add_documents`` — normalization included —
carrying the exact key set the LIVE writer
(``KnowledgeService._index_document_in_vector_store``) stamps, and the
``where`` clause is evaluated by a real ephemeral ChromaDB. Neither half is
negotiable: a stand-in that wrote raw metadata would prove isolation only on
rows production cannot write (the old ``report_type`` predicate "isolated"
perfectly because *nobody* saw anything), and a permissive fake would agree
with a clause the engine rejects. Every isolation assertion is therefore
paired with a positive one — the entitled principal *does* get the row — so
total failure cannot masquerade as isolation.

The same read path is proven end-to-end through the live ``ingest_runbook``
writer in ``tests/integration/test_runbook_dedup_live_path.py``.
"""

from typing import Any, Dict, List, Optional

import pytest

from faultmaven.infrastructure.knowledge.runbook_kb import (
    RESULTS_UNREADABLE_CODE,
    RunbookKnowledgeBase,
)
from faultmaven.infrastructure.persistence.chromadb_store import ChromaDBVectorStore
from faultmaven.models.exceptions import KnowledgeBaseError
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    build_kb_scope_filter,
)

pytestmark = [pytest.mark.knowledge_base]

USER_A = "user-alpha-11111111"
USER_B = "user-beta-22222222"

_DIM = 8


def _vec(seed: float) -> List[float]:
    return [seed] * _DIM


class _ChromaBackedStore(ChromaDBVectorStore):
    """The **real** vector store, over an in-process ephemeral ChromaDB.

    Not a hand-written double. ``add_documents`` and ``query_by_embedding`` are
    the production methods, so both halves of the round trip are real: writes
    pass through the ``VectorMetadata`` normalization (an allowlist that
    refuses undeclared keys) and the ``where`` clause is evaluated by real
    ChromaDB, which rejects the multi-key implicit-AND form outright rather
    than quietly ANDing it. Only the query half is overridden here, and only
    to record the clause.
    """

    def __init__(self, name: str):
        import chromadb
        from chromadb.config import Settings as ChromaSettings

        # Settings are PINNED, for the same reason ``create_persistent_client``
        # pins them (#823): chromadb caches one System per identifier and
        # refuses a second client for it whose ``Settings`` differ in any field
        # — and ``Settings.environment`` defaults to the ambient ENVIRONMENT
        # variable, which other tests set and clear. With bare
        # ``EphemeralClient()`` a gate in this file would run only in some
        # collection orders, and a gate that only runs in some orders is not a
        # gate.
        super().__init__(
            chromadb.EphemeralClient(
                settings=ChromaSettings(
                    anonymized_telemetry=False,
                    allow_reset=False,
                    environment="",
                    is_persistent=False,
                )
            ),
            collection_name=name,
        )
        self.queries: List[Optional[Dict[str, Any]]] = []

    async def seed(
        self, chunk_id: str, metadata: Dict[str, Any], embedding: List[float]
    ) -> None:
        """Write a row **the way production writes one** — normalization included."""
        await self.add_documents(
            [
                {
                    "id": chunk_id,
                    "content": f"# Runbook {chunk_id}",
                    "metadata": metadata,
                }
            ],
            embeddings=[embedding],
        )

    async def query_by_embedding(self, query_embedding, where=None, top_k=5):
        self.queries.append(where)
        return await super().query_by_embedding(
            query_embedding, where=where, top_k=top_k
        )


def _chunk_metadata(
    parent: str,
    *,
    scope: str,
    owner_id: Optional[str] = None,
    title: Optional[str] = "Runbook: Connection pool exhaustion",
    document_type: str = "runbook",
    chunk_index: int = 0,
    total_chunks: int = 1,
) -> Dict[str, Any]:
    """The key set the LIVE writer stamps on every chunk.

    Mirrors ``_index_document_in_vector_store``: ``document_type``, ``scope``
    (immutable floor only — never team), ``owner_id``, ``title``,
    ``parent_document_id``, chunk tracking. ``add_documents`` refuses any key
    ``VectorMetadata`` does not declare, so a hand-seeded dict that drifts from
    the production one fails here instead of silently seeding rows production
    could never write (#912).
    """
    md: Dict[str, Any] = {
        "document_type": document_type,
        "scope": scope,
        "title": title,
        "parent_document_id": parent,
        "chunk_index": chunk_index,
        "total_chunks": total_chunks,
    }
    if owner_id is not None:
        md["owner_id"] = owner_id
    return md


@pytest.fixture()
def store(request) -> _ChromaBackedStore:
    # Trimmed to ChromaDB's name rules (3-512 chars, alnum at both ends).
    name = f"kb_scope_{request.node.name}".strip("_")[:60].rstrip("_-.")
    return _ChromaBackedStore(name)


@pytest.fixture()
def kb(store) -> RunbookKnowledgeBase:
    return RunbookKnowledgeBase(vector_store=store)


# ---------------------------------------------------------------------------
# Scope isolation — every negative paired with its positive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_personal_runbook_is_visible_to_its_owner_and_no_one_else(kb, store):
    """User A's filter surfaces A's personal runbook; user B's does not.

    The two seeded rows are identical in every indexed field but owner, so an
    excluded row can only have been excluded by the scope predicate — and the
    owner's positive arm proves the exclusion is not "nobody sees anything".
    """
    await store.seed(
        "kb-a_chunk_0",
        _chunk_metadata("kb-a", scope="personal", owner_id=USER_A),
        _vec(0.1),
    )
    await store.seed(
        "kb-b_chunk_0",
        _chunk_metadata("kb-b", scope="personal", owner_id=USER_B),
        _vec(0.1),
    )

    seen_by_a = await kb.search_runbooks(
        query_embedding=_vec(0.1), scope_filter=build_kb_scope_filter(USER_A, [])
    )
    seen_by_b = await kb.search_runbooks(
        query_embedding=_vec(0.1), scope_filter=build_kb_scope_filter(USER_B, [])
    )

    assert [m.item_id for m in seen_by_a] == ["kb-a"], "owner cannot see own runbook"
    assert [m.item_id for m in seen_by_b] == ["kb-b"]
    assert "kb-a" not in [m.item_id for m in seen_by_b], "personal runbook leaked"


@pytest.mark.asyncio
async def test_a_global_runbook_is_visible_to_every_principal(kb, store):
    await store.seed(
        "kb-global_chunk_0", _chunk_metadata("kb-global", scope="global"), _vec(0.1)
    )

    for user in (USER_A, USER_B, None):
        matches = await kb.search_runbooks(
            query_embedding=_vec(0.1), scope_filter=build_kb_scope_filter(user, [])
        )
        assert [m.item_id for m in matches] == [
            "kb-global"
        ], f"global hidden from {user}"
        assert matches[0].scope == "global"


@pytest.mark.asyncio
async def test_the_team_arm_surfaces_a_shared_runbook_by_item_id(kb, store):
    """A runbook shared to the searcher's team is visible through the
    ``parent_document_id $in shared_ids`` arm — and only when the id is in the
    allowlist (the same test proves the without-share negative)."""
    await store.seed(
        "kb-shared_chunk_0",
        _chunk_metadata("kb-shared", scope="personal", owner_id=USER_A),
        _vec(0.1),
    )

    without_share = await kb.search_runbooks(
        query_embedding=_vec(0.1), scope_filter=build_kb_scope_filter(USER_B, [])
    )
    with_share = await kb.search_runbooks(
        query_embedding=_vec(0.1),
        scope_filter=build_kb_scope_filter(USER_B, ["kb-shared"]),
    )

    assert without_share == [], "unshared personal runbook leaked across users"
    assert [m.item_id for m in with_share] == ["kb-shared"]


@pytest.mark.asyncio
async def test_only_runbooks_match_never_other_kb_documents(kb, store):
    """``document_type == "runbook"`` is the artifact discriminator: a plain
    KB document at the same embedding must not surface as a runbook match."""
    await store.seed(
        "kb-doc_chunk_0",
        _chunk_metadata("kb-doc", scope="global", document_type="documentation"),
        _vec(0.1),
    )
    await store.seed(
        "kb-rb_chunk_0", _chunk_metadata("kb-rb", scope="global"), _vec(0.1)
    )

    matches = await kb.search_runbooks(
        query_embedding=_vec(0.1), scope_filter=build_kb_scope_filter(USER_A, [])
    )

    assert [m.item_id for m in matches] == ["kb-rb"]


@pytest.mark.asyncio
async def test_the_bare_global_filter_composes_under_and_against_real_chromadb(
    kb, store
):
    """``build_kb_scope_filter(None, [])`` returns a BARE single condition, not
    an ``$or`` — it must still compose as one ``$and`` operand that a real
    ChromaDB accepts and evaluates. (ChromaDB rejects malformed where clauses
    outright, so a mocked store cannot prove this.)"""
    await store.seed(
        "kb-global_chunk_0", _chunk_metadata("kb-global", scope="global"), _vec(0.1)
    )
    await store.seed(
        "kb-personal_chunk_0",
        _chunk_metadata("kb-personal", scope="personal", owner_id=USER_A),
        _vec(0.1),
    )

    scope_filter = build_kb_scope_filter(None, [])
    assert "$or" not in scope_filter, "precondition: the bare-condition shape"

    matches = await kb.search_runbooks(
        query_embedding=_vec(0.1), scope_filter=scope_filter
    )

    assert [m.item_id for m in matches] == ["kb-global"]


# ---------------------------------------------------------------------------
# Fail closed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_scope", [None, {}])
async def test_an_unscoped_search_refuses_and_issues_no_query(kb, store, bad_scope):
    await store.seed(
        "kb-global_chunk_0", _chunk_metadata("kb-global", scope="global"), _vec(0.1)
    )

    with pytest.raises(KnowledgeBaseError) as excinfo:
        await kb.search_runbooks(query_embedding=_vec(0.1), scope_filter=bad_scope)

    assert excinfo.value.error_code == "RUNBOOK_SEARCH_UNSCOPED"
    assert store.queries == [], "a query was issued without a scope predicate"


# ---------------------------------------------------------------------------
# Chunk collapse, against real rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_multi_chunk_runbook_collapses_to_one_match_at_its_best_chunk(
    kb, store
):
    for i, seed in enumerate((0.1, 0.12, 0.14)):
        await store.seed(
            f"kb-long_chunk_{i}",
            _chunk_metadata("kb-long", scope="global", chunk_index=i, total_chunks=3),
            _vec(seed),
        )
    await store.seed(
        "kb-short_chunk_0", _chunk_metadata("kb-short", scope="global"), _vec(0.2)
    )

    matches = await kb.search_runbooks(
        query_embedding=_vec(0.1),
        scope_filter=build_kb_scope_filter(USER_A, []),
        top_k=2,
    )

    ids = [m.item_id for m in matches]
    assert ids[0] == "kb-long"
    assert ids.count("kb-long") == 1, "one runbook filled multiple result slots"
    assert "kb-short" in ids, "the second distinct runbook was crowded out"


# ---------------------------------------------------------------------------
# The unreadable class is reachable through the production write path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_empty_titled_runbook_produces_unreadable_chunks_that_refuse(
    kb, store
):
    """``VectorMetadata.to_chroma_metadata`` truthiness-gates ``title``, so an
    empty-titled document's chunks genuinely carry NO title key after the REAL
    normalization — the unreadable class shrinks under fm#1030 but does not
    vanish. When such a chunk is the strongest candidate, the search refuses
    rather than answering from a set whose best row it could not read."""
    await store.seed(
        "kb-untitled_chunk_0",
        _chunk_metadata("kb-untitled", scope="global", title=""),
        _vec(0.1),
    )

    with pytest.raises(KnowledgeBaseError) as excinfo:
        await kb.search_runbooks(
            query_embedding=_vec(0.1), scope_filter=build_kb_scope_filter(USER_A, [])
        )

    assert excinfo.value.error_code == RESULTS_UNREADABLE_CODE
    # The refusal is deterministic: it must not have consumed the retry budget.
    assert len(store.queries) == 1, "a deterministic parse refusal was retried"


@pytest.mark.asyncio
async def test_an_unreadable_chunk_outranked_by_a_readable_match_does_not_refuse(
    kb, store
):
    """The other half of the refusal rule, against real rows: a skipped row
    below the best readable match cannot change the top-match verdict, so the
    readable answer stands."""
    await store.seed(
        "kb-untitled_chunk_0",
        _chunk_metadata("kb-untitled", scope="global", title=""),
        _vec(0.3),  # similar enough to clear the threshold, below the readable
    )
    await store.seed(
        "kb-ok_chunk_0", _chunk_metadata("kb-ok", scope="global"), _vec(0.1)
    )

    matches = await kb.search_runbooks(
        query_embedding=_vec(0.1), scope_filter=build_kb_scope_filter(USER_A, [])
    )

    assert [m.item_id for m in matches] == ["kb-ok"]
