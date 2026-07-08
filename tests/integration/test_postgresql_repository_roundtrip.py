"""Round-trip the PostgreSQL case repository against a REAL PostgreSQL.

This is the test that would have caught the ``:name::jsonb`` bind-drop bug
(#441): it executes ``PostgreSQLHybridCaseRepository``'s actual INSERT/UPDATE
SQL — every JSONB and timestamptz cast — against a live PostgreSQL and reads
the rows back. The SQLite and mocked-session suites cannot catch that class
because this repository runs ONLY on PostgreSQL (SQLite uses a different
repo) and the defect lives in how SQLAlchemy/asyncpg bind the casts, which a
mocked session never exercises.

It is skipped unless ``DATABASE_URL`` points at PostgreSQL, so the default
SQLite dev/CI flow is unaffected. CI provides a ``postgres`` service and runs
``alembic upgrade head`` before this suite (see the ``test-postgres`` job in
.github/workflows/ci-cd.yml).

Run locally:

    docker run -d -e POSTGRES_PASSWORD=pw -p 5432:5432 postgres:16
    export DATABASE_URL=postgresql+asyncpg://postgres:pw@localhost:5432/postgres
    .venv/bin/alembic upgrade head
    .venv/bin/pytest tests/integration/test_postgresql_repository_roundtrip.py -v
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from faultmaven.modules.case.domain.models import (
    Case,
    CaseEntity,
    CaseState,
    CausalEdge,
    CausalNode,
    EntityType,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    EvidenceStance,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    InquiryData,
    InterventionQuadrant,
    NodeEvidenceLink,
    NodeState,
    NodeType,
    Solution,
    SolutionType,
    UploadedFile,
    ValidationMethod,
)
from faultmaven.modules.case.domain.owned_models.checkpoint import CaseCheckpoint
from faultmaven.modules.case.domain.owned_models.report import (
    CaseReport,
    ReportStatus,
    ReportType,
)
from faultmaven.modules.case.infrastructure.postgresql_hybrid_case_repository import (
    PostgreSQLHybridCaseRepository,
)
from tests.utils import seed_organizations, seed_users

pytestmark = [
    pytest.mark.integration,
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get("DATABASE_URL", "").startswith("postgresql"),
        reason="PostgreSQL-only; set DATABASE_URL to a PG instance to run.",
    ),
]


@pytest.fixture
async def pg_engine():
    """Async engine bound to the PostgreSQL under test.

    Assumes the schema already exists (CI runs ``alembic upgrade head``
    first). Verifies the dialect is actually PostgreSQL so this never
    silently runs against the wrong backend.
    """
    engine = create_async_engine(os.environ["DATABASE_URL"], future=True)
    assert engine.dialect.name == "postgresql", (
        "This suite must run against PostgreSQL; "
        f"got dialect={engine.dialect.name!r}"
    )
    yield engine
    await engine.dispose()


@pytest.fixture
async def pg_repo(pg_engine):
    """A PostgreSQLHybridCaseRepository on a real PG session, with the
    case's FK prerequisites (enterprise/org/user) seeded."""
    Session = async_sessionmaker(pg_engine, expire_on_commit=False)
    async with Session() as session:
        # Sanity: the factory-style detection must agree this is PG, or the
        # cast helper would silently emit SQLite-style bare placeholders.
        repo = PostgreSQLHybridCaseRepository(session)
        assert repo._is_pg is True
        yield repo


def _make_case(org_id: str, user_id: str) -> Case:
    """A case whose JSONB columns are all non-trivially populated, so a
    dropped/again-broken cast surfaces as a real read-back mismatch."""
    return Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id=user_id,
        organization_id=org_id,
        title="PG round-trip case",
        description="Exercises JSONB + timestamptz casts on real PostgreSQL",
        state=CaseState.INVESTIGATING,
        inquiry=InquiryData(
            proposed_problem_statement="DB connections time out under load",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
    )


@pytest.mark.asyncio
async def test_case_save_roundtrip_with_jsonb_columns(pg_repo):
    """save() executes the cases UPDATE/INSERT (8 JSONB casts) plus
    evidence/uploaded_files/messages inserts (more JSONB casts) on real PG,
    then get() reads them back. The ``:name::jsonb`` bug made every one of
    these writes raise 'syntax error at or near ":"'."""
    session = pg_repo.db
    org_id = f"org_{uuid4().hex[:8]}"
    user_id = f"user_{uuid4().hex[:8]}"
    await seed_organizations(session, [org_id])
    await seed_users(session, [user_id])

    case = _make_case(org_id, user_id)
    file_id = f"file_{uuid4().hex[:12]}"
    case.uploaded_files.append(
        UploadedFile(
            file_id=file_id,
            filename="app.log",
            size_bytes=2048,
            content_type="text/plain",
            uploaded_at_turn=1,
            uploaded_by=user_id,
            upload_source="file_upload",
            summary="timeouts",
            structural_index="ERROR: timeout",
            data_type="logs",
            # tz-aware coverage window — the exact shape that 500'd the cluster
            # case-save. uploaded_files.coverage_*_ts is TIMESTAMPTZ (migration 022
            # widens the naive TIMESTAMP that migration 010 created); a naive column
            # makes asyncpg's naive codec reject the offset-aware datetime on PG.
            coverage_start_ts=datetime(2026, 6, 13, 10, 0, 0, tzinfo=timezone.utc),
            coverage_end_ts=datetime(2026, 6, 13, 10, 30, 0, tzinfo=timezone.utc),
        )
    )
    case.evidence.append(
        Evidence(
            evidence_id=f"ev_{uuid4().hex[:12]}",
            category=EvidenceCategory.SYMPTOM_EVIDENCE,
            primary_purpose="symptom_verified",
            summary="connection timeouts in the log",
            extract="ERROR: timeout after 30s",
            source_type=EvidenceSourceType.LOGS,
            source_file_id=file_id,
            collected_by=user_id,
            collected_at_turn=1,
            # TagsArray binds a Python list on PG, so advances_milestones is
            # VARCHAR(50)[] (migration 022 widens the TEXT that migration 009
            # created); a TEXT column makes asyncpg reject the bound list. Exercises
            # that bind.
            advances_milestones=["symptom_verified"],
        )
    )

    # case.messages is List[Dict] with an ISO-STRING created_at (exactly how
    # the live turn flow populates it). save() -> _upsert_messages binds this
    # to the timestamptz column; a bare str raises asyncpg DataError (the
    # case-save 500 on the cluster) unless the repo coerces it to a datetime.
    case.messages.append(
        {
            "message_id": f"msg_{uuid4().hex[:12]}",
            "turn_number": 1,
            "role": "user",
            "content": "how to fix the timeout?",
            "created_at": "2026-06-13T10:15:30.123456+00:00",
            "metadata": {},
        }
    )

    saved = await pg_repo.save(case)
    assert saved.version == 1

    fetched = await pg_repo.get(case.case_id)
    assert fetched is not None
    # JSONB round-tripped from the `inquiry` column, not stored as a literal
    # ':inquiry::jsonb' string (which is exactly what the bug produced).
    assert fetched.inquiry.problem_statement_confirmed is True
    assert fetched.inquiry.proposed_problem_statement == (
        "DB connections time out under load"
    )
    assert len(fetched.evidence) == 1
    assert fetched.evidence[0].source_file_id == file_id
    # advances_milestones (TagsArray -> varchar[] on PG) round-trips; a non-empty
    # list bound to a TEXT column would raise asyncpg DataError (column is
    # VARCHAR(50)[] per migration 022).
    assert fetched.evidence[0].advances_milestones == ["symptom_verified"]
    # The tz-aware coverage window round-tripped (columns are TIMESTAMPTZ, not
    # the naive TIMESTAMP that raised asyncpg DataError on the cluster save).
    saved_file = next(f for f in fetched.uploaded_files if f.file_id == file_id)
    assert saved_file.coverage_start_ts == datetime(
        2026, 6, 13, 10, 0, 0, tzinfo=timezone.utc
    )
    assert saved_file.coverage_end_ts == datetime(
        2026, 6, 13, 10, 30, 0, tzinfo=timezone.utc
    )
    # The string-timestamped message persisted (timestamptz coercion worked).
    persisted = await pg_repo.get_messages(case.case_id)
    assert any(m.get("content") == "how to fix the timeout?" for m in persisted)


@pytest.mark.asyncio
async def test_upsert_case_entities_roundtrip(pg_repo):
    """upsert_case_entities() is a standalone path NOT reached by save() — it
    is the 4th org-id-subquery site, and the one whose enclosing text() was
    newly converted to an f-string. Exercise it directly so the
    AmbiguousParameterError fix (and the f-string conversion) is validated."""
    session = pg_repo.db
    org_id = f"org_{uuid4().hex[:8]}"
    user_id = f"user_{uuid4().hex[:8]}"
    await seed_organizations(session, [org_id])
    await seed_users(session, [user_id])

    # The entity FK-references an evidence row, so save a case with one first.
    case = _make_case(org_id, user_id)
    file_id = f"file_{uuid4().hex[:12]}"
    evidence_id = f"ev_{uuid4().hex[:12]}"
    case.uploaded_files.append(
        UploadedFile(
            file_id=file_id,
            filename="app.log",
            size_bytes=512,
            content_type="text/plain",
            uploaded_at_turn=1,
            uploaded_by=user_id,
            upload_source="file_upload",
            summary="s",
            structural_index="ERROR: timeout from 10.0.0.5",
            data_type="logs",
        )
    )
    case.evidence.append(
        Evidence(
            evidence_id=evidence_id,
            category=EvidenceCategory.SYMPTOM_EVIDENCE,
            primary_purpose="symptom_verified",
            summary="timeout referencing host 10.0.0.5",
            extract="ERROR: timeout from 10.0.0.5",
            source_type=EvidenceSourceType.LOGS,
            source_file_id=file_id,
            collected_by=user_id,
            collected_at_turn=1,
        )
    )
    await pg_repo.save(case)

    # The actual path under test: the case_entities INSERT with the reused
    # :case_id org-id subquery.
    await pg_repo.upsert_case_entities(
        case.case_id,
        evidence_id,
        [
            CaseEntity(
                case_id=case.case_id,
                entity_type=EntityType.IP,
                entity_value="10.0.0.5",
                evidence_id=evidence_id,
                mention_count=2,
                in_error_context=True,
            )
        ],
    )

    found = await pg_repo.find_entity(case.case_id, "10.0.0.5")
    assert len(found) == 1
    assert found[0].entity_type == EntityType.IP
    assert found[0].mention_count == 2


@pytest.mark.asyncio
async def test_add_message_roundtrip(pg_repo):
    """add_message() exercises the case_messages metadata JSONB cast."""
    session = pg_repo.db
    org_id = f"org_{uuid4().hex[:8]}"
    user_id = f"user_{uuid4().hex[:8]}"
    await seed_organizations(session, [org_id])
    await seed_users(session, [user_id])
    case = _make_case(org_id, user_id)
    await pg_repo.save(case)

    # created_at is an ISO STRING here, exactly as the live turn flow passes
    # it — asyncpg rejects a str for the timestamptz column unless the repo
    # coerces it to a datetime (the case-save 500 on the cluster). A datetime
    # would have hidden the bug, so the string form is the regression guard.
    ok = await pg_repo.add_message(
        case.case_id,
        {
            "message_id": f"msg_{uuid4().hex[:12]}",
            "turn_number": 1,
            "role": "user",
            "content": "the API returns 500",
            "created_at": "2026-06-13T10:15:30.123456+00:00",
            "metadata": {"client": "copilot"},
        },
    )
    assert ok is True
    messages = await pg_repo.get_messages(case.case_id)
    assert any(m.get("content") == "the API returns 500" for m in messages)


@pytest.mark.asyncio
async def test_add_report_roundtrip_with_timestamptz(pg_repo):
    """add_report() exercises the reports metadata (JSONB) AND
    generated_at/updated_at (TIMESTAMPTZ) casts — the timestamptz arm of the
    same bug class."""
    session = pg_repo.db
    org_id = f"org_{uuid4().hex[:8]}"
    user_id = f"user_{uuid4().hex[:8]}"
    await seed_organizations(session, [org_id])
    await seed_users(session, [user_id])
    case = _make_case(org_id, user_id)
    await pg_repo.save(case)

    report = CaseReport(
        report_id=f"report_{uuid4().hex[:12]}",
        case_id=case.case_id,
        report_type=ReportType.RESOLUTION_SUMMARY,
        title="Resolution summary",
        content="# Summary\n\nFixed the pool size.",
        format="markdown",
        generation_status=ReportStatus.COMPLETED,
        generated_at="2026-06-12T10:30:00Z",
        generation_time_ms=1500,
        is_current=True,
        version=1,
        linked_to_closure=False,
    )
    # add_report returns the input object, so don't assert on it (tautology);
    # the real check is the read-back from PostgreSQL.
    await pg_repo.add_report(report)

    fetched = await pg_repo.get_report(report.report_id)
    assert fetched is not None
    assert fetched.title == "Resolution summary"
    assert fetched.generation_status == ReportStatus.COMPLETED


@pytest.mark.asyncio
async def test_create_checkpoint_roundtrip_with_timestamptz(pg_repo):
    """create_checkpoint() exercises case_snapshot + metadata (JSONB) AND
    created_at (TIMESTAMPTZ) casts."""
    session = pg_repo.db
    org_id = f"org_{uuid4().hex[:8]}"
    user_id = f"user_{uuid4().hex[:8]}"
    await seed_organizations(session, [org_id])
    await seed_users(session, [user_id])
    case = _make_case(org_id, user_id)
    await pg_repo.save(case)

    from datetime import datetime, timezone

    checkpoint = CaseCheckpoint(
        checkpoint_id=f"{case.case_id}:turn:1",
        case_id=case.case_id,
        turn_number=1,
        case_snapshot={"state": "investigating", "turn": 1},
        snapshot_hash="0" * 64,
        trigger="turn_complete",
        created_at=datetime.now(timezone.utc),
        metadata={"reason": "test"},
    )
    saved = await pg_repo.create_checkpoint(checkpoint)
    assert saved.checkpoint_id == checkpoint.checkpoint_id

    fetched = await pg_repo.get_checkpoint(checkpoint.checkpoint_id)
    assert fetched is not None
    assert fetched.case_snapshot == {"state": "investigating", "turn": 1}


@pytest.mark.asyncio
async def test_causal_graph_roundtrip(pg_repo):
    """Round-trip the causal graph on real PG (the 1d slice): causal_nodes +
    causal_edges (incl. an AND-group) + the causal_node_evidence junction, plus
    the chain-header fields on hypotheses (root_node_id/path) and the node/
    quadrant linkage on solutions. Exercises the JSONB ``path`` cast and the
    FK ordering (nodes before edges/hypotheses/solutions; node-evidence after
    evidence)."""
    session = pg_repo.db
    org_id = f"org_{uuid4().hex[:8]}"
    user_id = f"user_{uuid4().hex[:8]}"
    await seed_organizations(session, [org_id])
    await seed_users(session, [user_id])

    case = _make_case(org_id, user_id)

    # Evidence the node-evidence junction can FK to (USER_DESCRIPTION needs no file).
    ev_id = f"ev_{uuid4().hex[:12]}"
    case.evidence.append(
        Evidence(
            evidence_id=ev_id,
            category=EvidenceCategory.CAUSAL_EVIDENCE,
            primary_purpose="root_cause",
            summary="psql from the migration pod times out connecting to :5432",
            source_type=EvidenceSourceType.USER_DESCRIPTION,
            collected_by=user_id,
            collected_at_turn=2,
        )
    )

    # Build the graph: D <- intermediate <- (root AND co-cause).
    d = CausalNode(
        statement="Deploy to on-prem job fails",
        node_type=NodeType.PROBLEM,
        generated_at_turn=0,
    )
    inter = CausalNode(
        statement="migration pod connection to postgres times out",
        node_type=NodeType.INTERMEDIATE,
        node_state=NodeState.VALIDATED,
        validation_method=ValidationMethod.EMPIRICAL,
        category=HypothesisCategory.NETWORK,
        generated_at_turn=2,
        evidence_links=[
            NodeEvidenceLink(
                evidence_id=ev_id,
                stance=EvidenceStance.SUPPORTS,
                reasoning="timeout signature observed",
                stance_confidence=0.9,
                linked_at_turn=2,
            )
        ],
    )
    root = CausalNode(
        statement="NetworkPolicy denies ingress to postgres on 5432",
        node_type=NodeType.ROOT,
        node_state=NodeState.VALIDATED,
        validation_method=ValidationMethod.EMPIRICAL,
        actionable=True,
        category=HypothesisCategory.NETWORK,
        generated_at_turn=3,
    )
    co_cause = CausalNode(
        statement="migration Job has an aggressive connect deadline",
        node_type=NodeType.INTERMEDIATE,
        category=HypothesisCategory.CONFIG,
        generated_at_turn=3,
    )
    case.causal_nodes = {n.node_id: n for n in (d, inter, root, co_cause)}
    # root AND co_cause -> intermediate (co-necessary, shared and_group); then
    # intermediate -> D.
    case.causal_edges = [
        CausalEdge(
            cause_node_id=root.node_id,
            effect_node_id=inter.node_id,
            and_group="g1",
            created_at_turn=3,
        ),
        CausalEdge(
            cause_node_id=co_cause.node_id,
            effect_node_id=inter.node_id,
            and_group="g1",
            created_at_turn=3,
        ),
        CausalEdge(
            cause_node_id=inter.node_id,
            effect_node_id=d.node_id,
            created_at_turn=2,
        ),
    ]

    # Chain header on a hypothesis + intervention linkage on a solution.
    hyp = Hypothesis(
        statement="NetworkPolicy blocks the migration connection",
        category=HypothesisCategory.NETWORK,
        generation_mode=HypothesisGenerationMode.SYSTEMATIC,
        generated_at_turn=3,
        rationale="timeout signature points at reachability",
        root_node_id=root.node_id,
        path=[root.node_id, inter.node_id, d.node_id],
    )
    case.hypotheses[hyp.hypothesis_id] = hyp
    case.solutions.append(
        Solution(
            solution_type=SolutionType.CONFIG_CHANGE,
            title="Add an ingress from-clause to the NetworkPolicy",
            immediate_action="patch the NetworkPolicy to allow ingress on 5432",
            node_id=root.node_id,
            quadrant=InterventionQuadrant.REMEDIATION,
        )
    )

    await pg_repo.save(case)
    fetched = await pg_repo.get(case.case_id)
    assert fetched is not None

    # Nodes round-tripped, with per-node empirical state intact.
    assert len(fetched.causal_nodes) == 4
    fr = fetched.causal_nodes[root.node_id]
    assert fr.node_type == NodeType.ROOT
    assert fr.node_state == NodeState.VALIDATED
    assert fr.validation_method == ValidationMethod.EMPIRICAL
    assert fr.actionable is True
    fi = fetched.causal_nodes[inter.node_id]
    assert fi.node_state == NodeState.VALIDATED
    # Node-scoped evidence link survived the junction.
    assert len(fi.evidence_links) == 1
    assert fi.evidence_links[0].evidence_id == ev_id
    assert fi.evidence_links[0].stance == EvidenceStance.SUPPORTS

    # Edges round-tripped, AND-group preserved (two edges into `inter`).
    assert len(fetched.causal_edges) == 3
    and_edges = [
        e
        for e in fetched.causal_edges
        if e.effect_node_id == inter.node_id and e.and_group == "g1"
    ]
    assert len(and_edges) == 2

    # Chain header + solution linkage.
    fh = fetched.hypotheses[hyp.hypothesis_id]
    assert fh.root_node_id == root.node_id
    assert fh.path == [root.node_id, inter.node_id, d.node_id]
    fs = fetched.solutions[0]
    assert fs.node_id == root.node_id
    assert fs.quadrant == InterventionQuadrant.REMEDIATION


async def test_pruned_causal_graph_does_not_resurrect(pg_repo):
    """On real PG: a node/edge removed in memory (e.g. a bridge stub GC'd by a
    hypothesis re-root) must be deleted from the DB on save, not survive the
    additive upsert and resurrect on the next load. Covers both a stale node and
    a stale edge whose endpoints both survive (not caught by the FK cascade)."""
    session = pg_repo.db
    org_id = f"org_{uuid4().hex[:8]}"
    user_id = f"user_{uuid4().hex[:8]}"
    await seed_organizations(session, [org_id])
    await seed_users(session, [user_id])

    case = _make_case(org_id, user_id)
    d = CausalNode(
        statement="Checkout returns 500s",
        node_type=NodeType.PROBLEM,
        generated_at_turn=0,
    )
    stub = CausalNode(
        statement="config drift on the gateway",
        node_type=NodeType.ROOT,
        generated_at_turn=1,
    )
    root = CausalNode(
        statement="a leaked database connection",
        node_type=NodeType.ROOT,
        generated_at_turn=2,
    )
    inter = CausalNode(
        statement="the connection pool is exhausted",
        node_type=NodeType.INTERMEDIATE,
        generated_at_turn=2,
    )
    case.causal_nodes = {n.node_id: n for n in (d, stub, root, inter)}
    shortcut = CausalEdge(
        cause_node_id=root.node_id, effect_node_id=d.node_id, created_at_turn=2
    )
    case.causal_edges = [
        CausalEdge(
            cause_node_id=stub.node_id, effect_node_id=d.node_id, created_at_turn=1
        ),
        CausalEdge(
            cause_node_id=root.node_id,
            effect_node_id=inter.node_id,
            created_at_turn=2,
        ),
        CausalEdge(
            cause_node_id=inter.node_id, effect_node_id=d.node_id, created_at_turn=2
        ),
        shortcut,  # root->D direct edge; both endpoints survive the prune below
    ]
    await pg_repo.save(case)
    fetched = await pg_repo.get(case.case_id)
    assert len(fetched.causal_nodes) == 4
    assert len(fetched.causal_edges) == 4

    # GC the stub node (+ its edge) and drop the surviving-endpoint shortcut edge.
    del fetched.causal_nodes[stub.node_id]
    fetched.causal_edges = [
        e
        for e in fetched.causal_edges
        if e.cause_node_id != stub.node_id and e.edge_id != shortcut.edge_id
    ]
    await pg_repo.save(fetched)

    reloaded = await pg_repo.get(case.case_id)
    assert stub.node_id not in reloaded.causal_nodes  # did NOT resurrect
    assert len(reloaded.causal_nodes) == 3
    assert len(reloaded.causal_edges) == 2
    assert all(e.cause_node_id != stub.node_id for e in reloaded.causal_edges)
    assert all(e.edge_id != shortcut.edge_id for e in reloaded.causal_edges)
