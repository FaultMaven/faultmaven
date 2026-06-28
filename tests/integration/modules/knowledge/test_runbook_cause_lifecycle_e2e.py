"""Back half of the AUTHORING -> PACK -> INGEST -> RETRIEVE -> APPLY cross-repo
cause-shape contract test.

The missing end-to-end test is what let the v4 structured causal-chain path go
dead in production: the kb-toolkit shipped a per-Cause ``causes`` record in every
pack, but the app derived ChromaDB metadata from frontmatter only, never read the
record, and no single test crossed the whole boundary — so the break was
invisible. The toolkit half (``faultmaven-kb-toolkit``
``tests/unit/test_pack_causes_contract.py``) proves a real authored runbook packs
into a ``causes`` record with exactly ``CauseRecord``'s shape. THIS half proves
that exact record flows, lossless, through the app:

    INGEST   real ``KnowledgeService.ingest_runbook(causes=...)`` -> a real
             ``knowledge_items`` row (in-memory SQLite, real INSERT)
    RETRIEVE real ``KnowledgeService.get_runbook_causes(item_id)`` reads
             ``metadata['causes']`` straight back
    (shape)  the resolved record loads into ``CauseRecord`` with no field lost
    APPLY    the real runbook cause matcher resolves -> matches (T2) ->
             instantiates the chain as CANDIDATE priors + a capped hypothesis

The two halves are joined by a shared golden fixture: this repo vendors a
byte-identical copy of the toolkit's ``expected_pack_causes.json`` (real
``build_pack`` output). See ``tests/fixtures/runbook_cause_lifecycle/PROVENANCE.md``.

Only the boundaries are mocked: the vector store (retrieval ranks the runbook)
and the T2 ``case_evidence_qa`` yes/no LLM call. Everything between — SQL
persistence, the resolver, ``CauseRecord`` construction, the evaluator, chain
instantiation, the capped hypothesis, the documented-fixes context gate — is
real.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.core.investigation.cause_schemas import CauseRecord
from faultmaven.core.investigation.runbook_cause_matcher import (
    RUNBOOK_INTERVENTIONS_META_KEY,
    is_runbook_match_hypothesis,
)
from faultmaven.infrastructure.persistence.models import (
    Base,
    EnterpriseModel,
    OrganizationModel,
)
from faultmaven.modules.agent.tools.kb_qa import AnswerFromKB
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    CauseState,
    InquiryData,
    NodeState,
    NodeType,
    ProblemVerification,
)
from faultmaven.modules.case.domain.models import ValidationMethod
from faultmaven.modules.knowledge.domain.models.knowledge_item import VerificationLevel
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)

pytestmark = pytest.mark.integration

# The authored runbook's app-derived item_id (kb_<sha256(frontmatter id)[:12]>).
# Stable for ``cause-lifecycle-sample-runbook``; the matcher's mocked retrieval
# returns a chunk pointing at it so the real resolver reads the ingested row.
RUNBOOK_ITEM_ID = "kb_62620ab62e3f"
DEFAULT_ORG_ID = "00000000-0000-0000-0000-000000000001"
DEFAULT_ENTERPRISE_ID = "00000000-0000-0000-0000-000000000002"

GOLDEN_CAUSES = (
    Path(__file__).resolve().parents[3]  # .../tests
    / "fixtures"
    / "runbook_cause_lifecycle"
    / "expected_pack_causes.json"
)

# SHA-256 of the vendored golden. This file is a byte-identical copy of the
# kb-toolkit golden (real ``build_pack`` output) — the shared artifact that joins
# the two halves of the cross-repo cause-shape contract (see PROVENANCE.md).
# Nothing can compare the two repos' copies in one CI run, so this hash is the
# cross-repo pin: it MUST equal ``EXPECTED_GOLDEN_SHA256`` in the kb-toolkit
# ``test_pack_causes_contract.py``. Any edit to either copy fails its own repo's
# test until the hash is regenerated, forcing a conscious re-sync of both copies
# (the field-set checks below pin keys, not values — only this catches value
# drift between the two vendored copies). Regenerate via kb-toolkit
# ``regen_golden.py``, copy here, update this constant AND the kb-toolkit one.
EXPECTED_GOLDEN_SHA256 = (
    "7f1a083dd95830079ac095d63526c52f818cbdf86086923033886a03cfe1b1be"
)


def _load_golden() -> list[dict]:
    return json.loads(GOLDEN_CAUSES.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Real-SQLite KnowledgeService (vector half mocked) — mirrors test_ingest_runbook
# --------------------------------------------------------------------------- #


@pytest.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def session_factory(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
async def seeded_service(session_factory):
    """A real KnowledgeService backed by in-memory SQLite (seeded enterprise +
    org so the knowledge_items FK is satisfied); only the ChromaDB write is
    mocked so we can assert SQL persistence of ``metadata['causes']``."""
    async with session_factory() as session:
        session.add(
            EnterpriseModel(
                enterprise_id=DEFAULT_ENTERPRISE_ID, name="Default", slug="default"
            )
        )
        session.add(
            OrganizationModel(
                organization_id=DEFAULT_ORG_ID,
                enterprise_id=DEFAULT_ENTERPRISE_ID,
                name="Default Org",
                slug="default-org",
            )
        )
        await session.commit()

    service = KnowledgeService(
        knowledge_ingester=MagicMock(),
        sanitizer=MagicMock(),
        tracer=MagicMock(),
        vector_store=MagicMock(),
        db_session_factory=session_factory,
    )
    # Load-bearing: ingest_runbook DELETES the just-written SQL row (incl.
    # metadata['causes']) and raises if the vector write returns <= 0 or raises.
    # A positive int is required for the row to survive — keep this an int >= 1.
    service._index_document_in_vector_store = AsyncMock(return_value=2)
    return service


@pytest.fixture
async def ingested(seeded_service):
    """INGEST the golden causes through the real ingest_runbook, return the
    service + golden so each test can resolve/apply from the persisted row."""
    golden = _load_golden()
    await seeded_service.ingest_runbook(
        document_id=RUNBOOK_ITEM_ID,
        title="Database connection pool exhaustion",
        content="# Database connection pool exhaustion\n\nRunbook body.",
        organization_id=DEFAULT_ORG_ID,
        scope="global",
        causes=golden,
        verification_level=VerificationLevel.COMMUNITY,
    )
    return seeded_service, golden


# --------------------------------------------------------------------------- #
# Case + engine wiring for the APPLY hop (mirrors test_runbook_cause_matcher_e2e)
# --------------------------------------------------------------------------- #


def _case() -> Case:
    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="u1",
        organization_id=DEFAULT_ORG_ID,
        title="t",
        description="App returns 503s under load",
        state=CaseState.INVESTIGATING,
        inquiry=InquiryData(
            proposed_problem_statement="App returns 503s under load",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
        problem_verification=ProblemVerification(
            symptom_statement="Requests fail with database pool-timeout 503s under load",
            severity=CaseSeverity.HIGH,
        ),
    )
    case.current_turn = 3
    return case


def _kb_chunk() -> dict:
    """One retrieved chunk whose parent_document_id is the ingested runbook, so
    the matcher's real resolver reads the row we persisted."""
    return {
        "id": f"{RUNBOOK_ITEM_ID}_chunk_0",
        "content": "connection pool exhaustion runbook body",
        "metadata": {"title": "Pool exhaustion", "parent_document_id": RUNBOOK_ITEM_ID},
        "score": 0.93,
    }


def _real_kb_tool() -> AnswerFromKB:
    vector_store = MagicMock()
    vector_store.hybrid_search = AsyncMock(return_value=[_kb_chunk()])
    vector_store.search = AsyncMock(return_value=[_kb_chunk()])
    return AnswerFromKB(vector_store=vector_store, llm_router=MagicMock())


def _engine(knowledge_service, *, ce_answer=True):
    from faultmaven.core.investigation.hypothesis_manager import (
        create_hypothesis_manager,
    )
    from faultmaven.core.investigation.milestone_engine import MilestoneEngine

    ce_tool = SimpleNamespace(answer_yes_no=AsyncMock(return_value=ce_answer))
    registry = SimpleNamespace(
        get=lambda name: {
            "kb_qa": SimpleNamespace(wrapped=_real_kb_tool()),
            "case_evidence_search": SimpleNamespace(wrapped=ce_tool),
        }.get(name)
    )
    eng = MilestoneEngine.__new__(MilestoneEngine)
    eng.investigation_tools = registry
    eng.knowledge_service = knowledge_service  # the REAL service (real resolver)
    eng.hypothesis_manager = create_hypothesis_manager()
    return eng, ce_tool


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def test_golden_matches_cross_repo_hash():
    """The vendored golden must be byte-identical to the kb-toolkit copy. The
    field-set checks elsewhere pin keys, not values; this hash is the only guard
    against value drift between the two hand-copied golden files (it must equal
    the kb-toolkit ``EXPECTED_GOLDEN_SHA256``)."""
    actual = hashlib.sha256(GOLDEN_CAUSES.read_bytes()).hexdigest()
    assert actual == EXPECTED_GOLDEN_SHA256, (
        "Golden changed without re-sync. Regenerate via kb-toolkit "
        "regen_golden.py, copy the file here, and update EXPECTED_GOLDEN_SHA256 "
        "in BOTH repos' tests to this value: " + actual
    )


class TestIngestResolveRoundTrip:
    @pytest.mark.asyncio
    async def test_causes_survive_ingest_and_resolve_losslessly(self, ingested):
        """RETRIEVE: the real resolver reads back exactly what was authored —
        the round-trip through the SQL JSON column loses nothing."""
        service, golden = ingested
        resolved = await service.get_runbook_causes(RUNBOOK_ITEM_ID)
        assert resolved == golden

    @pytest.mark.asyncio
    async def test_resolved_causes_load_into_causerecord_without_loss(self, ingested):
        """SHAPE: each persisted Cause has exactly ``CauseRecord``'s field set and
        loads into it with no known field dropped. ``CauseRecord(**entry)``
        silently ignores extras, so the explicit field-set assertion is the real
        producer<->consumer drift guard (the toolkit half pins the same set)."""
        service, _ = ingested
        resolved = await service.get_runbook_causes(RUNBOOK_ITEM_ID)

        record_fields = set(CauseRecord.model_fields)
        for entry in resolved:
            keys = set(entry)
            assert keys == record_fields, (
                f"persisted Cause {entry.get('cause_letter')} drifted from "
                f"CauseRecord: extra={keys - record_fields} "
                f"missing={record_fields - keys}"
            )
            record = CauseRecord(**entry)
            # Every authored value is preserved on the loaded record.
            dumped = record.model_dump()
            for field in record_fields:
                assert dumped[field] == entry[field]

    @pytest.mark.asyncio
    async def test_unknown_runbook_resolves_to_none(self, ingested):
        """Resolver is tolerant: an id with no row returns None (the matcher
        treats it as 'no chain to match'), never raises."""
        service, _ = ingested
        assert await service.get_runbook_causes("kb_does_not_exist") is None


class TestApplyFromPersistedCauses:
    @pytest.mark.asyncio
    async def test_full_lifecycle_instantiates_capped_chain_and_gated_fixes(
        self, ingested
    ):
        """APPLY: drive the real matcher off the persisted row. The authored
        chain instantiates as CANDIDATE priors with a capped hypothesis, and the
        documented fixes surface only once the cause is established."""
        from faultmaven.core.investigation.prompts.context_builder import (
            build_investigation_context,
        )

        service, _ = ingested
        eng, ce_tool = _engine(service, ce_answer=True)
        case = _case()

        await eng._apply_runbook_cause_matcher(case, None, {})

        # 1. The authored chain (root -> s1 -> s2 -> D) instantiated, root is a
        #    CANDIDATE (the matcher never validates), D node present.
        roots = [n for n in case.causal_nodes.values() if n.node_type == NodeType.ROOT]
        assert len(roots) == 1
        root = roots[0]
        assert root.node_state == NodeState.CANDIDATE
        # The full authored ladder made it across: root + 2 intermediate rungs
        # (s1, s2) + the engine-seeded D/PROBLEM node (chain_to_specs drops the
        # authored D and the engine re-seeds it). Assert by node type rather than
        # a magic count so the intent — and which node is missing — is explicit.
        types = [n.node_type for n in case.causal_nodes.values()]
        assert types.count(NodeType.ROOT) == 1
        assert types.count(NodeType.PROBLEM) == 1
        assert types.count(NodeType.INTERMEDIATE) == 2

        # 2. T2 actually drove the match (the case-evidence tool was consulted).
        assert ce_tool.answer_yes_no.await_count >= 1

        # 3. A load-bearing hypothesis is attached, capped below the cause gate.
        hyps = list(case.hypotheses.values())
        assert len(hyps) == 1
        assert is_runbook_match_hypothesis(hyps[0])
        assert hyps[0].root_node_id == root.node_id
        assert hyps[0].likelihood <= 0.5

        # 4. The authored remediation is stashed on the root for later surfacing.
        assert RUNBOOK_INTERVENTIONS_META_KEY in root.metadata

        # 5. Before the cause is established, the fixes do NOT surface.
        ctx = build_investigation_context(case, user_message="next?")
        assert "<documented_fixes>" not in ctx["kb_results"]

        # --- evidence validates the root (engine/LLM would, over turns) ---
        object.__setattr__(root, "node_state", NodeState.VALIDATED)
        object.__setattr__(root, "validation_method", ValidationMethod.EMPIRICAL)
        case.progress.cause_state = CauseState.IDENTIFIED

        # 6. Now the authored remediation reaches the prompt; the LLM proposes it
        #    and the M5 gate (cause established) governs the Solution.
        ctx = build_investigation_context(case, user_message="next?")
        assert "<documented_fixes>" in ctx["kb_results"]
        assert "size the pool from observed peak" in ctx["kb_results"]

    @pytest.mark.asyncio
    async def test_no_match_when_evidence_does_not_support(self, ingested):
        """T2 says no for the cause -> belief 0 -> verdict none -> nothing
        instantiated. A runbook is a prior, not forced onto the case."""
        service, _ = ingested
        eng, ce_tool = _engine(service, ce_answer=False)
        case = _case()
        await eng._apply_runbook_cause_matcher(case, None, {})
        # T2 was actually consulted and abstained — proves the matcher RAN and
        # said no, not that it silently crashed (the matcher swallows all
        # exceptions, so an empty graph alone can't distinguish the two).
        assert ce_tool.answer_yes_no.await_count >= 1
        assert case.causal_nodes == {}
        assert case.hypotheses == {}
