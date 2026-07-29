"""The runbook collection's mandatory tenant predicate (#877).

``search_runbooks`` resolves by similarity, not by id: nothing about the query
names a tenant, so the ``organization_id`` predicate is the ONLY thing keeping
one organization's runbooks out of another's results. These tests pin that it is
mandatory, that it fails closed, and that it actually filters — the where clause
is evaluated by real ChromaDB here, not by a hand-rolled stand-in that could
agree with a wrong clause.

Companion rule: ``docs/architecture/security/rbac.md`` — "Tenant-Scoped
Resolution".
"""

from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, patch

import pytest

from faultmaven.infrastructure.knowledge.runbook_kb import RunbookKnowledgeBase
from faultmaven.models.report import (
    CaseReport,
    ReportStatus,
    ReportType,
    RunbookMetadata,
    RunbookSource,
)
from faultmaven.models.vector_metadata import VectorMetadata

# Genuinely distinct tenants. Every cross-tenant assertion below relies on these
# never being equal, and on the two seeded rows being identical in EVERY other
# indexed field — so a row that is excluded can only have been excluded by the
# org predicate.
ORG_A = "org-alpha-11111111"
ORG_B = "org-beta-22222222"

_DIM = 8


def _vec(seed: float) -> List[float]:
    return [seed] * _DIM


class _ChromaBackedStore:
    """Vector-store double whose ``where`` semantics are real ChromaDB's.

    The point of using the real engine is that a clause ChromaDB rejects (the
    multi-key implicit-AND form, refused since 1.0) or evaluates differently
    fails here instead of quietly passing against a permissive fake.
    """

    def __init__(self, name: str):
        import chromadb

        self._client = chromadb.EphemeralClient()
        self._collection = self._client.get_or_create_collection(name)
        self.queries: List[Optional[Dict[str, Any]]] = []

    def seed(self, doc_id: str, metadata: Dict[str, Any], embedding: List[float]):
        self._collection.add(
            ids=[doc_id],
            embeddings=[embedding],
            documents=[f"# Runbook {doc_id}"],
            metadatas=[metadata],
        )

    async def add_documents(self, documents, embeddings=None):
        for i, doc in enumerate(documents):
            self.seed(
                doc["id"],
                doc.get("metadata", {}),
                embeddings[i] if embeddings else _vec(0.1),
            )

    async def query_by_embedding(self, query_embedding, where=None, top_k=5):
        self.queries.append(where)
        return self._collection.query(
            query_embeddings=[query_embedding],
            n_results=top_k,
            where=where,
            include=["documents", "metadatas", "distances"],
        )


def _runbook_metadata(organization_id: str, doc_id: str) -> Dict[str, Any]:
    """Indexed metadata for a runbook. Identical across tenants but for the org."""
    return {
        "report_id": doc_id,
        "organization_id": organization_id,
        "case_id": "case-1",
        "case_title": "Connection pool exhaustion",
        "title": "Runbook: Connection pool exhaustion",
        "report_type": "runbook",
        "runbook_source": "incident_driven",
        "domain": "database",
        "tags": "postgresql",
        "created_at": "2026-01-01T00:00:00Z",
    }


@pytest.fixture
def store(request):
    return _ChromaBackedStore(f"runbook-tenant-{abs(hash(request.node.name)) % 10**8}")


@pytest.fixture
def kb(store):
    return RunbookKnowledgeBase(vector_store=store)


def _report(report_id: str) -> CaseReport:
    return CaseReport(
        report_id=report_id,
        case_id="case-1",
        report_type=ReportType.RUNBOOK,
        title="Runbook: Connection pool exhaustion",
        content="# Runbook\n\nrestart the pool",
        format="markdown",
        generation_status=ReportStatus.COMPLETED,
        generated_at="2026-01-01T00:00:00Z",
        generation_time_ms=1,
        is_current=True,
        version=1,
        linked_to_closure=False,
        metadata=RunbookMetadata(
            source=RunbookSource.INCIDENT_DRIVEN, domain="database", tags=["postgresql"]
        ),
    )


# =============================================================================
# The isolation property
# =============================================================================


@pytest.mark.security
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "owner_org,searcher_org",
    [
        (ORG_A, ORG_B),
        (ORG_B, ORG_A),
        ("org-1", "org-2"),
        ("acme", "acme-2"),  # prefix overlap must not leak
        ("ACME", "acme"),  # case difference is a different tenant
    ],
)
async def test_a_runbook_is_never_returned_to_another_tenant(
    kb, store, owner_org, searcher_org
):
    """Sweep of distinct tenant pairs, not one lucky case.

    The seeded row matches EVERY other predicate the search applies
    (report_type, domain) and sits at distance 0 from the query vector, so it
    WOULD be returned but for the org predicate.
    """
    store.seed("rb-owned", _runbook_metadata(owner_org, "rb-owned"), _vec(0.5))

    mine = await kb.search_runbooks(
        query_embedding=_vec(0.5), organization_id=owner_org, min_similarity=0.0
    )
    theirs = await kb.search_runbooks(
        query_embedding=_vec(0.5), organization_id=searcher_org, min_similarity=0.0
    )

    assert [r.runbook.report_id for r in mine] == ["rb-owned"], (
        "the owning tenant must still see its own runbook — otherwise the "
        "isolation assertion below proves nothing"
    )
    assert theirs == []


@pytest.mark.security
@pytest.mark.asyncio
async def test_search_returns_only_the_searching_tenants_rows(kb, store):
    """Two otherwise-identical runbooks, one per tenant: each search sees one."""
    store.seed("rb-a", _runbook_metadata(ORG_A, "rb-a"), _vec(0.5))
    store.seed("rb-b", _runbook_metadata(ORG_B, "rb-b"), _vec(0.5))

    a = await kb.search_runbooks(
        query_embedding=_vec(0.5), organization_id=ORG_A, min_similarity=0.0, top_k=10
    )
    b = await kb.search_runbooks(
        query_embedding=_vec(0.5), organization_id=ORG_B, min_similarity=0.0, top_k=10
    )

    assert [r.runbook.report_id for r in a] == ["rb-a"]
    assert [r.runbook.report_id for r in b] == ["rb-b"]


@pytest.mark.security
@pytest.mark.asyncio
async def test_domain_filter_narrows_within_a_tenant_and_never_widens_across(kb, store):
    """The optional domain filter composes with the org predicate, not instead of it."""
    a_db = _runbook_metadata(ORG_A, "a-db")
    b_db = _runbook_metadata(ORG_B, "b-db")
    a_net = _runbook_metadata(ORG_A, "a-net")
    a_net["domain"] = "network"
    store.seed("a-db", a_db, _vec(0.5))
    store.seed("b-db", b_db, _vec(0.5))
    store.seed("a-net", a_net, _vec(0.5))

    got = await kb.search_runbooks(
        query_embedding=_vec(0.5),
        organization_id=ORG_A,
        filters={"domain": "database"},
        min_similarity=0.0,
        top_k=10,
    )
    assert [r.runbook.report_id for r in got] == ["a-db"]


# =============================================================================
# Fail closed
# =============================================================================


@pytest.mark.security
@pytest.mark.asyncio
@pytest.mark.parametrize("falsy_org", ["", None, 0, [], {}])
async def test_search_without_a_tenant_returns_nothing_and_issues_no_query(
    kb, store, falsy_org
):
    """Sweep every falsy org: no results AND no query — never an unscoped one."""
    store.seed("rb-a", _runbook_metadata(ORG_A, "rb-a"), _vec(0.5))

    got = await kb.search_runbooks(
        query_embedding=_vec(0.5), organization_id=falsy_org, min_similarity=0.0
    )

    assert got == []
    assert store.queries == []


@pytest.mark.security
@pytest.mark.asyncio
async def test_search_is_impossible_without_naming_a_tenant(kb):
    """organization_id is keyword-only and required — omitting it cannot compile."""
    with pytest.raises(TypeError, match="organization_id"):
        await kb.search_runbooks(query_embedding=_vec(0.5))


@pytest.mark.security
@pytest.mark.asyncio
@pytest.mark.parametrize("falsy_org", ["", None])
async def test_indexing_without_a_tenant_writes_nothing(kb, store, falsy_org):
    """An untenanted row is unreachable by every scoped search, so refuse to write it."""
    with patch(
        "faultmaven.infrastructure.model_cache.model_cache.aembed_query",
        AsyncMock(return_value=_vec(0.5)),
    ):
        await kb.index_runbook(_report("rb-x"), organization_id=falsy_org)

    got = await kb.search_runbooks(
        query_embedding=_vec(0.5), organization_id=ORG_A, min_similarity=0.0
    )
    assert got == []


# =============================================================================
# The stamp survives the write path
# =============================================================================


@pytest.mark.security
@pytest.mark.asyncio
@pytest.mark.parametrize("org", [ORG_A, ORG_B, "org-3"])
async def test_index_runbook_stamps_the_tenant(kb, store, org):
    with patch(
        "faultmaven.infrastructure.model_cache.model_cache.aembed_query",
        AsyncMock(return_value=_vec(0.5)),
    ):
        await kb.index_runbook(_report("rb-1"), organization_id=org)

    stored = store._collection.get(ids=["rb-1"], include=["metadatas"])
    assert stored["metadatas"][0]["organization_id"] == org


@pytest.mark.security
@pytest.mark.asyncio
@pytest.mark.parametrize("org", [ORG_A, ORG_B])
async def test_index_document_derived_runbook_stamps_the_tenant(kb, store, org):
    with patch(
        "faultmaven.infrastructure.model_cache.model_cache.aembed_query",
        AsyncMock(return_value=_vec(0.5)),
    ):
        runbook_id = await kb.index_document_derived_runbook(
            runbook_content="# Runbook\n\nsteps",
            document_title="Ops guide",
            domain="database",
            tags=["postgresql"],
            organization_id=org,
        )

    stored = store._collection.get(ids=[runbook_id], include=["metadatas"])
    assert stored["metadatas"][0]["organization_id"] == org


@pytest.mark.security
@pytest.mark.parametrize("org", [ORG_A, ORG_B, "org-3"])
def test_the_tenant_key_survives_vector_metadata_normalization(org):
    """``ChromaDBVectorStore.add_documents`` normalizes through ``VectorMetadata``.

    A tenant key this schema dropped would leave the row unreachable by every
    scoped search — an isolation guarantee that held only because nothing was
    ever returned. Pin that it round-trips.
    """
    raw = _runbook_metadata(org, "rb-1")
    vm = VectorMetadata(
        **{
            k: raw.get(k)
            for k in VectorMetadata.model_fields
            if k in raw or raw.get(k) is not None
        }
    )
    assert vm.to_chroma_metadata()["organization_id"] == org
