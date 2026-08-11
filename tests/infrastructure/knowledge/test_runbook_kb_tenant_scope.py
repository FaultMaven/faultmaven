"""The runbook collection's mandatory tenant predicate (#877).

``search_runbooks`` resolves by similarity, not by id: nothing about the query
names a tenant, so the ``organization_id`` predicate is the ONLY thing keeping
one organization's runbooks out of another's results. These tests pin that it is
mandatory, that it fails closed, and that it actually filters.

**Both halves of the round trip are the real code.** Rows are written through
the production ``ChromaDBVectorStore.add_documents`` — normalization included —
and the ``where`` clause is evaluated by a real ephemeral ChromaDB. Neither half
is negotiable: a stand-in that wrote raw metadata would prove isolation only on
rows production cannot write (``report_type`` was being dropped on every real
write, so "org B sees nothing" held because *nobody* saw anything), and a
permissive fake would agree with a clause the engine rejects. Every isolation
assertion is therefore paired with a positive one — the owning tenant *does* get
its row — so total failure cannot masquerade as isolation.

Companion rule: ``docs/architecture/security/rbac.md`` — "Tenant-Scoped
Resolution".
"""

from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, patch

import pytest

from faultmaven.infrastructure.knowledge.runbook_kb import RunbookKnowledgeBase
from faultmaven.infrastructure.persistence.chromadb_store import ChromaDBVectorStore
from faultmaven.models.report import (
    CaseReport,
    ReportStatus,
    ReportType,
    RunbookMetadata,
    RunbookSource,
)

# Genuinely distinct tenants. Every cross-tenant assertion below relies on these
# never being equal, and on the two seeded rows being identical in EVERY other
# indexed field — so a row that is excluded can only have been excluded by the
# org predicate.
ORG_A = "org-alpha-11111111"
ORG_B = "org-beta-22222222"

_DIM = 8


def _vec(seed: float) -> List[float]:
    return [seed] * _DIM


class _ChromaBackedStore(ChromaDBVectorStore):
    """The **real** vector store, over an in-process ephemeral ChromaDB.

    Not a hand-written double. ``add_documents`` and ``query_by_embedding`` are
    the production methods, so both halves of the round trip are real: writes
    pass through the ``VectorMetadata`` normalization (an allowlist that silently
    drops every key it does not declare) and the ``where`` clause is evaluated by
    real ChromaDB, which rejects the multi-key implicit-AND form outright rather
    than quietly ANDing it.

    That matters more than it looks. A double that wrote raw metadata straight
    into the collection would prove the isolation property only on rows
    production cannot write — ``report_type`` was in fact being dropped on every
    real write, so the guard was passing against fictional data. Only the query
    half is overridden here, and only to record the clause.
    """

    def __init__(self, name: str):
        import chromadb
        from chromadb.config import Settings as ChromaSettings

        # Settings are PINNED, for the same reason ``create_persistent_client``
        # pins them (#823): chromadb caches one System per identifier and
        # refuses a second client for it whose ``Settings`` differ in any field
        # — and ``Settings.environment`` defaults to the ambient ENVIRONMENT
        # variable, which tests in this suite set and clear. With bare
        # ``EphemeralClient()`` that made every store built after the first
        # such change raise "An instance of Chroma already exists for ephemeral
        # with different settings", so this file's tests errored out or passed
        # purely on collection order. A gate that only runs in some orders is
        # not a gate.
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
        self, doc_id: str, metadata: Dict[str, Any], embedding: List[float]
    ) -> None:
        """Write a row **the way production writes one** — normalization included."""
        await self.add_documents(
            [{"id": doc_id, "content": f"# Runbook {doc_id}", "metadata": metadata}],
            embeddings=[embedding],
        )

    async def query_by_embedding(self, query_embedding, where=None, top_k=5):
        self.queries.append(where)
        return await super().query_by_embedding(
            query_embedding, where=where, top_k=top_k
        )


def _normalized(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Run a metadata dict through the exact normalization ``add_documents`` applies.

    Calls the production normalizer rather than re-implementing it — a local
    copy would drift from the real allowlist and start agreeing with itself.
    """
    return ChromaDBVectorStore._normalize_metadata(raw)


def _runbook_metadata(organization_id: str, doc_id: str) -> Dict[str, Any]:
    """Indexed metadata for a runbook. Identical across tenants but for the org.

    Deliberately the same key set ``index_runbook`` writes — no ``report_id``,
    which is the row id rather than a metadata field. ``add_documents`` refuses
    any key ``VectorMetadata`` does not declare, so a hand-seeded dict that
    drifts from the production one fails here instead of silently seeding rows
    production could never write (#912).
    """
    return {
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
# The round trip — the test that keeps the isolation property from being vacuous
# =============================================================================


@pytest.mark.security
@pytest.mark.asyncio
@pytest.mark.parametrize("owner_org,other_org", [(ORG_A, ORG_B), (ORG_B, ORG_A)])
async def test_an_indexed_runbook_round_trips_to_its_own_tenant_and_no_other(
    kb, store, owner_org, other_org
):
    """Index through ``index_runbook``, retrieve through ``search_runbooks``.

    Both directions are pinned deliberately:

    * **positive** — the owner gets the row back. This is what proves the search
      predicate matches what the *write path* actually stores. Without it, the
      negative assertion below is satisfied by a search that returns nothing to
      anyone, which is a dead gate, not isolation. It is also the assertion that
      fails if ``report_type`` is dropped by ``VectorMetadata`` normalization
      again: the ``{"report_type": "runbook"}`` condition then matches no row
      this path can write.
    * **negative** — the other tenant gets nothing.

    No hand-seeded metadata anywhere in this test: the row exists only because
    the production writer put it there.
    """
    with patch(
        "faultmaven.infrastructure.model_cache.model_cache.aembed_query",
        AsyncMock(return_value=_vec(0.5)),
    ):
        await kb.index_runbook(_report("rb-round-trip"), organization_id=owner_org)

    mine = await kb.search_runbooks(
        query_embedding=_vec(0.5), organization_id=owner_org, min_similarity=0.0
    )
    theirs = await kb.search_runbooks(
        query_embedding=_vec(0.5), organization_id=other_org, min_similarity=0.0
    )

    assert [r.runbook.report_id for r in mine] == ["rb-round-trip"], (
        "the runbook the production write path just indexed must come back to "
        "its own tenant — a search that returns nothing to everyone proves no "
        "isolation at all"
    )
    assert theirs == []


@pytest.mark.asyncio
async def test_an_indexed_runbook_round_trips_every_field_it_was_written_with(
    kb, store
):
    """The whole row survives the write path, not just the two predicate keys.

    ``search_runbooks`` does not return the stored row — it rebuilds a
    ``CaseReport``/``SimilarRunbook`` out of the stored metadata. Any key
    normalization drops is therefore not merely missing at read time: it is
    replaced by whatever the reconstruction falls back to. Before #912
    ``VectorMetadata`` declared none of the identity keys, so every runbook came
    back with ``case_id="unknown"``, ``case_title="Unknown"``, no document
    linkage, and — the reason this is a correctness bug rather than a cosmetic
    one — ``source=incident_driven``, asserted with full confidence about a
    runbook that was document-driven.

    A DOCUMENT_DRIVEN runbook is used deliberately: ``incident_driven`` was the
    old fallback, so an incident-driven fixture would pass this test while the
    bug was fully present.

    Every value below is distinct and non-default, so no assertion can be
    satisfied by a coincidence.
    """
    report = CaseReport(
        report_id="rb-identity",
        case_id="case-REAL-42",
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
            source=RunbookSource.DOCUMENT_DRIVEN,
            domain="database",
            tags=["postgresql"],
            document_title="PostgreSQL Operations Guide",
            original_document_id="doc-77",
        ),
    )

    with patch(
        "faultmaven.infrastructure.model_cache.model_cache.aembed_query",
        AsyncMock(return_value=_vec(0.5)),
    ):
        await kb.index_runbook(
            report,
            organization_id=ORG_A,
            source=RunbookSource.DOCUMENT_DRIVEN,
            case_title="Pool exhaustion in prod",
        )

    found = await kb.search_runbooks(
        query_embedding=_vec(0.5), organization_id=ORG_A, min_similarity=0.0
    )
    assert len(found) == 1, "the row the production writer just wrote must come back"
    match = found[0]

    # The report id is the ChromaDB row id, not a metadata key — pinned here so
    # that stays true if anyone reintroduces a metadata copy of it.
    assert match.runbook.report_id == "rb-identity"

    assert match.runbook.case_id == "case-REAL-42"
    assert match.case_id == "case-REAL-42"
    assert match.case_title == "Pool exhaustion in prod"
    assert match.runbook.metadata.source is RunbookSource.DOCUMENT_DRIVEN
    assert match.runbook.metadata.document_title == "PostgreSQL Operations Guide"
    assert match.runbook.metadata.original_document_id == "doc-77"

    # And the fabricated stand-ins are gone, named explicitly so a regression
    # that reinstates them fails on the symptom the issue was filed for.
    assert match.runbook.case_id != "unknown"
    assert match.case_title != "Unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_key", ["case_id", "case_title", "runbook_source"])
async def test_a_runbook_row_missing_an_identity_key_is_skipped_not_invented(
    kb, store, missing_key
):
    """A row this write path could not have produced is dropped, not guessed at.

    The collection is shared with KB documents and ``report_type`` is the only
    discriminator, so the search predicate can match a row that is not a runbook
    this path wrote. Reconstructing one anyway is how a confident, wrong
    ``source``/``case_id``/``case_title`` reaches the caller. Absence must read
    as absence.

    Parametrized over EVERY identity key rather than one representative: the
    reconstruction reads each key at its own call site, so a test covering one
    of them leaves the others free to go back to a ``.get(default)`` — which is
    the whole defect. Each seeded row is identical to a good one but for the one
    missing key, so the exclusion can only be due to that key.
    """
    good = _runbook_metadata(ORG_A, "rb-good")
    incomplete = {k: v for k, v in good.items() if k != missing_key}

    await store.seed("rb-good", good, _vec(0.5))
    await store.seed("rb-incomplete", incomplete, _vec(0.5))

    found = await kb.search_runbooks(
        query_embedding=_vec(0.5), organization_id=ORG_A, min_similarity=0.0
    )

    assert [r.runbook.report_id for r in found] == ["rb-good"], (
        f"the row missing {missing_key!r} must be skipped — and the complete "
        "row must still come back, or this passes because nothing was returned"
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
    await store.seed("rb-owned", _runbook_metadata(owner_org, "rb-owned"), _vec(0.5))

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
    await store.seed("rb-a", _runbook_metadata(ORG_A, "rb-a"), _vec(0.5))
    await store.seed("rb-b", _runbook_metadata(ORG_B, "rb-b"), _vec(0.5))

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
    await store.seed("a-db", a_db, _vec(0.5))
    await store.seed("b-db", b_db, _vec(0.5))
    await store.seed("a-net", a_net, _vec(0.5))

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
    await store.seed("rb-a", _runbook_metadata(ORG_A, "rb-a"), _vec(0.5))

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
    """An untenanted row is unreachable by every scoped search, so refuse to write it.

    The load-bearing assertion is ``count() == 0`` — *nothing was written*.
    "No search returns it" is true either way and proves nothing on its own: the
    normalization drops an empty tenant stamp, so a row written without the
    refusal is simply an orphan no scoped query can ever reach. Orphaned storage
    growth that no reader can see is the failure this refusal exists to prevent,
    so the count is what has to be pinned.
    """
    with patch(
        "faultmaven.infrastructure.model_cache.model_cache.aembed_query",
        AsyncMock(return_value=_vec(0.5)),
    ):
        await kb.index_runbook(_report("rb-x"), organization_id=falsy_org)

    assert store.collection.count() == 0, "an org-less index must write no row at all"
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

    stored = store.collection.get(ids=["rb-1"], include=["metadatas"])
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

    stored = store.collection.get(ids=[runbook_id], include=["metadatas"])
    assert stored["metadatas"][0]["organization_id"] == org


@pytest.mark.security
@pytest.mark.parametrize("org", [ORG_A, ORG_B, "org-3"])
def test_the_tenant_key_survives_vector_metadata_normalization(org):
    """``ChromaDBVectorStore.add_documents`` normalizes through ``VectorMetadata``.

    A tenant key this schema dropped would leave the row unreachable by every
    scoped search — an isolation guarantee that held only because nothing was
    ever returned. Pin that it round-trips.
    """
    assert _normalized(_runbook_metadata(org, "rb-1"))["organization_id"] == org


@pytest.mark.security
def test_the_report_type_discriminator_survives_vector_metadata_normalization():
    """``report_type`` is the only thing separating runbooks from KB documents.

    ``RunbookKnowledgeBase.COLLECTION_NAME`` is decorative — the injected store is
    bound to the general KB collection — so runbooks and documents share one
    collection and ``search_runbooks`` relies on ``report_type == "runbook"`` to
    tell them apart. A schema that drops it makes that predicate match nothing
    the write path produced (#912).
    """
    assert _normalized(_runbook_metadata(ORG_A, "rb-1"))["report_type"] == "runbook"
