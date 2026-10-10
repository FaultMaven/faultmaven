"""#1919 end to end: retrieval uses the driver's knowledge; stored copies are gated.

Owner ruling on #1919 (2026-10-10):

1. Retrieval uses the case DRIVER's knowledge: global ∪ the driver's personal KB
   ∪ the runbooks shared to the driver's teams. Until #1898 the driver is the
   creator (``cases.user_id``). The pre-fetch (push), ``kb_qa`` (pull) and the
   runbook dedup all take it from ``case_retrieval_scope``.
2. The model's answer written from a runbook is accepted disclosure.
3. Stored COPIES of runbook text (a turn's ``sources``) are checked per viewer
   when read back: a viewer who can open the runbook sees the excerpt; anyone
   else sees a redacted entry.

Real collaborators throughout: SQLite behind the production share and team
repositories, ``TeamService`` and ``KnowledgeService``; a real in-process
ChromaDB behind ``KnowledgeVectorStore``, with one vector for every row so the
scope filter is the only thing that keeps a runbook out. Every negative
assertion sits beside a positive one on the same path.

People and teams: the DRIVER (creator) is in ``T_DRIVER`` and was in
``T_RETIRED`` until its last member left; a TEAMMATE is in ``T_DRIVER`` and
``T_TEAMMATE``; an OUTSIDER is in neither and the case is shared to no team of
theirs. Runbooks, all in one enterprise: one global, one personal runbook each
for the driver and the teammate, and one runbook shared to each team, written
by an author outside every scope.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Set
from unittest.mock import AsyncMock, MagicMock, patch

import chromadb
import pytest
from chromadb.config import Settings as ChromaSettings
from fastapi import Response
from sqlalchemy import event, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import faultmaven.infrastructure.knowledge.knowledge_vector_store as kvs_module
from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.core.investigation import terminal_transitions as tt
from faultmaven.core.investigation.milestone_engine.dependencies import EngineDeps
from faultmaven.core.investigation.milestone_engine.generation import (
    StructuredOutputGenerator,
)
from faultmaven.core.investigation.milestone_engine.kb_prefetch import KbPrefetcher
from faultmaven.core.investigation.milestone_engine.runbook_creation import (
    RunbookCreator,
)
from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
    KB_COLLECTION,
    KnowledgeVectorStore,
)
from faultmaven.infrastructure.knowledge.runbook_kb import RunbookKnowledgeBase
from faultmaven.infrastructure.llm.providers.base import LLMResponse
from faultmaven.infrastructure.persistence.models import (
    Base,
    KnowledgeItemModel,
    TeamModel,
)
from faultmaven.infrastructure.persistence.share_repository import (
    PostgreSQLShareRepository,
)
from faultmaven.infrastructure.persistence.team_repository import (
    PostgreSQLTeamRepository,
)
from faultmaven.models.api import Source, SourceType
from faultmaven.models.api_models import TurnResponse
from faultmaven.models.interfaces_user import Team
from faultmaven.modules.agent.tools.kb_qa import AnswerFromKB
from faultmaven.modules.agent.tools.kb_tool_adapter import KBToolAdapter
from faultmaven.modules.auth.domain.services.team_service import TeamService
from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
    InquiryData,
    ProblemVerification,
    RootCauseConclusion,
    Solution,
    SolutionType,
)
from faultmaven.modules.case.domain.services.case_service import CaseService
from faultmaven.modules.knowledge.contracts import RESTRICTED_SOURCE_ACCESS
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)

pytestmark = [pytest.mark.integration, pytest.mark.security]

ENT = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
DRIVER = "11111111-1111-1111-1111-111111111111"
TEAMMATE = "22222222-2222-2222-2222-222222222222"
OUTSIDER = "33333333-3333-3333-3333-333333333333"
AUTHOR = "44444444-4444-4444-4444-444444444444"

T_DRIVER = "team-driver"
T_TEAMMATE = "team-teammate"
T_RETIRED = "team-retired"

RB_GLOBAL = "rb-global"
RB_DRIVER_PERSONAL = "rb-driver-personal"
RB_TEAMMATE_PERSONAL = "rb-teammate-personal"
RB_DRIVER_TEAM = "rb-driver-team"
RB_TEAMMATE_TEAM = "rb-teammate-team"
RB_RETIRED_TEAM = "rb-retired-team"

#: (scope, owner, team it is shared to) per runbook.
RUNBOOKS = {
    RB_GLOBAL: ("global", None, None),
    RB_DRIVER_PERSONAL: ("personal", DRIVER, None),
    RB_TEAMMATE_PERSONAL: ("personal", TEAMMATE, None),
    RB_DRIVER_TEAM: ("personal", AUTHOR, T_DRIVER),
    RB_TEAMMATE_TEAM: ("personal", AUTHOR, T_TEAMMATE),
    RB_RETIRED_TEAM: ("personal", AUTHOR, T_RETIRED),
}

#: The driver's knowledge: what every retrieval path may surface.
DRIVER_KNOWLEDGE = {RB_GLOBAL, RB_DRIVER_PERSONAL, RB_DRIVER_TEAM}

_DIM = 8
_VEC = [0.5] * _DIM


async def _fixed_embedding(*_args: Any, **_kwargs: Any) -> List[float]:
    return list(_VEC)


def _ephemeral_client():
    """In-process ChromaDB, pinned settings, empty KB collection (see
    ``test_kb_tenant_isolation_probe._ephemeral_client`` for why both)."""
    client = chromadb.EphemeralClient(
        settings=ChromaSettings(
            anonymized_telemetry=False,
            allow_reset=False,
            environment="",
            is_persistent=False,
        )
    )
    try:
        client.delete_collection(KB_COLLECTION)
    except Exception:  # noqa: BLE001 - absent on the first client of a session
        pass
    return client


def _chunk(parent: str) -> Dict[str, Any]:
    scope, owner, _team = RUNBOOKS[parent]
    metadata: Dict[str, Any] = {
        "document_type": "runbook",
        "scope": scope,
        "enterprise_id": STANDALONE_ENTERPRISE_ID if scope == "global" else ENT,
        "title": f"Runbook {parent}",
        "parent_document_id": parent,
        "chunk_index": 0,
        "total_chunks": 1,
        "domain": "database",
        "service": "postgres",
    }
    if owner is not None:
        metadata["owner_id"] = owner
    return {
        "id": f"{parent}_chunk_0",
        "content": (
            f"# Runbook {parent}\nMARKER::{parent}::\n"
            "Connection pool exhausted; queries time out under load."
        ),
        "metadata": metadata,
    }


def _markers(text_: str) -> Set[str]:
    return {rb for rb in RUNBOOKS if f"MARKER::{rb}::" in text_}


@pytest.fixture()
async def client():
    kb_client = _ephemeral_client()
    rows = [_chunk(rb) for rb in RUNBOOKS]
    await KnowledgeVectorStore(kb_client).add_documents(
        rows, embeddings=[list(_VEC) for _ in rows]
    )
    return kb_client


class _World(SimpleNamespace):
    """The SQL side: one connection shared by every session (StaticPool), so
    the repositories and ``KnowledgeService``'s own sessions see one database."""


@pytest.fixture()
async def world():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(
            text(
                "INSERT INTO enterprises (enterprise_id, name, slug) "
                "VALUES (:ent, 'Acme', 'acme')"
            ),
            {"ent": ENT},
        )
        for user_id in (DRIVER, TEAMMATE, OUTSIDER, AUTHOR):
            await conn.execute(
                text(
                    "INSERT INTO users (user_id, username, email, display_name, "
                    "enterprise_id, is_active, created_at, updated_at) VALUES "
                    "(:u, :u, :e, :u, :ent, 1, :now, :now)"
                ),
                {
                    "u": user_id,
                    "e": f"{user_id}@example.test",
                    "ent": ENT,
                    "now": datetime.now(timezone.utc),
                },
            )
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    session = factory()
    teams = PostgreSQLTeamRepository(session)
    shares = PostgreSQLShareRepository(session)
    now = datetime.now(timezone.utc)
    for team_id in (T_DRIVER, T_TEAMMATE, T_RETIRED):
        await teams.create_team(
            Team(
                team_id=team_id,
                enterprise_id=ENT,
                name=f"Team {team_id}",
                description=None,
                created_at=now,
                updated_at=now,
            )
        )
    for team_id, user_id in (
        (T_DRIVER, DRIVER),
        (T_DRIVER, TEAMMATE),
        (T_TEAMMATE, TEAMMATE),
        (T_RETIRED, DRIVER),
    ):
        await teams.add_member(ENT, team_id, user_id)
    # Retired: ``leave_team`` sets deleted_at when the last member leaves; the
    # driver's membership row is left behind, as a retirement leaves it.
    await session.execute(
        update(TeamModel).where(TeamModel.team_id == T_RETIRED).values(deleted_at=now)
    )
    for item_id, (scope, owner, team_id) in RUNBOOKS.items():
        session.add(
            KnowledgeItemModel(
                item_id=item_id,
                enterprise_id=STANDALONE_ENTERPRISE_ID if scope == "global" else ENT,
                title=f"Runbook {item_id}",
                content=f"MARKER::{item_id}::",
                item_type="runbook",
                scope=scope,
                owner_id=owner,
                is_published=True,
            )
        )
    await session.commit()
    for item_id, (_scope, _owner, team_id) in RUNBOOKS.items():
        if team_id:
            await shares.share(
                resource_type="knowledge_item",
                resource_id=item_id,
                scope_type="team",
                scope_id=team_id,
                enterprise_id=ENT,
            )
    await session.commit()

    statements: List[str] = []

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _count(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    team_service = TeamService(
        team_repository=teams,
        enterprise_repository=MagicMock(),
        user_repository=MagicMock(),
    )
    knowledge = KnowledgeService(
        knowledge_ingester=MagicMock(),
        sanitizer=_Sanitizer(),
        tracer=MagicMock(),
        vector_store=MagicMock(),
        db_session_factory=factory,
    )
    yield _World(
        session=session,
        teams=team_service,
        shares=shares,
        knowledge=knowledge,
        statements=statements,
    )
    await session.close()
    await engine.dispose()


class _Sanitizer:
    async def asanitize(self, text_: str) -> str:
        return text_


def _case() -> Case:
    """A real case whose driver (creator) is ``DRIVER``, with enough resolved
    content for the dedup to form a query."""
    case = Case(
        user_id=DRIVER,
        enterprise_id=ENT,
        title="Connection pool exhausted under load",
        description="DB queries timing out",
        state=CaseState.INVESTIGATING,
        problem_verification=ProblemVerification(
            symptom_statement="Timeout errors", severity="HIGH"
        ),
        inquiry=InquiryData(
            problem_statement_confirmed=True, proposed_problem_statement="Timeout"
        ),
    )
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause="Misconfigured connection pool timeout",
        confidence_level="verified",
        likelihood=0.9,
        mechanism="Connection pool timeout set to 1s caused cascading failures",
    )
    case.solutions = [
        Solution(
            solution_type=SolutionType.CONFIG_CHANGE,
            title="Increase connection pool timeout to 30s",
            longterm_fix="Update pool timeout in application config",
        )
    ]
    return case


# =============================================================================
# 1. Retrieval: the driver's knowledge, on every path
# =============================================================================


def _deps(world: _World) -> EngineDeps:
    deps = EngineDeps()
    deps.team_service = world.teams
    deps.share_repository = world.shares
    deps.repository = MagicMock()
    deps.investigation_tools = None
    return deps


async def _push(kb_client, world: _World, case: Case) -> Set[str]:
    deps = _deps(world)
    deps.knowledge_service = KnowledgeService(
        knowledge_ingester=MagicMock(),
        sanitizer=_Sanitizer(),
        tracer=MagicMock(),
        vector_store=KnowledgeVectorStore(kb_client),
        db_session_factory=MagicMock(),
    )
    with patch.object(kvs_module, "embed_query_or_raise", new=_fixed_embedding):
        await KbPrefetcher(deps=deps).prefetch_kb_context(
            case, "connection pool exhausted", "symptom"
        )
    return {entry["parent_document_id"] for entry in case.kb_context or []}


class _CapturingRouter:
    def __init__(self) -> None:
        self.prompts: List[str] = []

    async def route(self, *, model, messages, max_tokens, temperature, **kwargs):
        self.prompts.append(messages[-1]["content"])
        return LLMResponse(
            content="stub answer",
            confidence=1.0,
            provider="stub",
            model="stub",
            tokens_used=1,
            response_time_ms=1,
        )


async def _pull(kb_client, world: _World, case: Case) -> Set[str]:
    """``kb_qa`` with the context the engine builds. The TEAMMATE is the turn's
    user: keyed on the case, their own personal and team runbooks stay out."""
    context = await StructuredOutputGenerator(
        deps=_deps(world), vectorizer=None
    ).build_tool_context(case, user_id=TEAMMATE)
    router = _CapturingRouter()
    adapter = KBToolAdapter(
        AnswerFromKB(vector_store=KnowledgeVectorStore(kb_client), llm_router=router)
    )
    with patch.object(kvs_module, "embed_query_or_raise", new=_fixed_embedding):
        result = await adapter.execute_with_context(
            {"question": "How do we handle connection pool exhaustion?"}, context
        )
    assert result.success, result.error
    return _markers("\n".join(router.prompts))


async def _dedup(kb_client, world: _World, case: Case) -> Set[str]:
    """The terminal-turn dedup with the engine's own resolver. It keeps its top
    3 matches, so the scope it searched is recorded and evaluated by the same
    ChromaDB at full depth; what it returned must sit inside that set."""
    kb = RunbookKnowledgeBase.over_kb_collection(kb_client)
    returned: Set[str] = set()
    scopes: List[Dict[str, Any]] = []
    original = kb.search_by_text

    async def _recording(*args, **kwargs):
        scopes.append(kwargs["scope_filter"])
        matches = await original(*args, **kwargs)
        returned.update(m.item_id for m in matches)
        return matches

    kb.search_by_text = _recording  # type: ignore[method-assign]
    resolver = RunbookCreator(
        case_locks={}, deps=_deps(world)
    )._runbook_dedup_scope_resolver(case)
    with patch(
        "faultmaven.infrastructure.knowledge.runbook_kb.embed_query_or_raise",
        new=AsyncMock(return_value=list(_VEC)),
    ):
        suggestion = await tt.evaluate_runbook_suggestion(
            case, kb, scope_resolver=resolver
        )
    assert suggestion.verdict == tt.RunbookSuggestion.SIMILAR_FOUND, suggestion
    assert len(scopes) == 1, scopes
    with patch.object(kvs_module, "embed_query_or_raise", new=_fixed_embedding):
        hits = await KnowledgeVectorStore(kb_client).search(
            KB_COLLECTION, "connection pool exhausted", k=25, where=scopes[0]
        )
    searched = {hit["metadata"]["parent_document_id"] for hit in hits}
    assert returned and returned <= searched, (returned, searched)
    return searched


PATHS: Dict[str, Callable] = {"push": _push, "pull": _pull, "dedup": _dedup}


@pytest.fixture(autouse=True)
def _ready_for_dedup(monkeypatch):
    monkeypatch.setattr(
        tt,
        "assess_runbook_readiness",
        lambda case: MagicMock(verdict=tt.RunbookReadiness.READY, message="ready"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("path", sorted(PATHS))
async def test_every_path_retrieves_exactly_the_drivers_knowledge(path, client, world):
    got = await PATHS[path](client, world, _case())

    assert {
        RB_DRIVER_PERSONAL,
        RB_DRIVER_TEAM,
    } <= got, (
        f"{path}: the driver's own knowledge was not retrieved (got {sorted(got)})"
    )
    assert RB_RETIRED_TEAM not in got, f"{path}: a retired team's runbook was read"
    assert not got & {
        RB_TEAMMATE_PERSONAL,
        RB_TEAMMATE_TEAM,
    }, f"{path}: keyed on the turn's user, not the case's driver"
    assert got == DRIVER_KNOWLEDGE


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["push", "pull"])
async def test_a_failed_team_lookup_keeps_only_global_and_the_drivers_own(
    path, client, world
):
    world.teams.list_all_user_team_ids = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("team lookup failed")
    )

    got = await PATHS[path](client, world, _case())

    assert got == {RB_GLOBAL, RB_DRIVER_PERSONAL}


# =============================================================================
# 2. Stored copies: gated per viewer on GET .../messages and the turn response
# =============================================================================


def _stored_source(document_id: Any) -> Dict[str, Any]:
    """One stored source exactly as ``_record_turn_kb_sources`` writes it."""
    return Source(
        type=SourceType.KNOWLEDGE_BASE,
        content=f"excerpt MARKER::{document_id}::",
        confidence=0.9,
        metadata={
            "document_id": document_id,
            "title": f"Runbook {document_id}",
            "trigger": "symptom",
        },
        new_this_turn=True,
    ).model_dump(mode="json")


STORED = [RB_GLOBAL, RB_DRIVER_PERSONAL, RB_DRIVER_TEAM, RB_TEAMMATE_TEAM, None]


def _case_with_transcript(turns: int = 1) -> Case:
    case = _case()
    case.messages = [
        {
            "message_id": f"msg_{i}",
            "turn_number": i,
            "role": "assistant",
            "content": "Here is what the runbooks say.",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {"kb_sources": [_stored_source(d) for d in STORED]},
        }
        for i in range(1, turns + 1)
    ]
    return case


def _viewer(user_id: str) -> Any:
    return SimpleNamespace(user_id=user_id, enterprise_id=ENT)


def _request(world: _World) -> Any:
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                knowledge_service=world.knowledge,
                team_service=world.teams,
                llm_provider=None,
                redis_client=None,
            )
        )
    )


async def _messages_as(world: _World, viewer_id: str, case: Case) -> List[Source]:
    """``GET /cases/{id}/messages`` through the route, as ``viewer_id``."""
    from faultmaven.modules.case.api.routes.conversation import (
        get_case_messages_enhanced,
    )

    repo = MagicMock()
    repo.get = AsyncMock(return_value=case)
    case_service = CaseService(case_repository=repo)
    case_service.get_case = AsyncMock(return_value=case)  # read access: granted
    response = await get_case_messages_enhanced(
        case_id=case.case_id,
        request=_request(world),
        response=Response(),
        limit=50,
        offset=0,
        include_debug=False,
        case_service=case_service,
        current_user=_viewer(viewer_id),
    )
    return [s for m in response.messages for s in m.sources or []]


def _shown(sources: List[Source]) -> Dict[Any, str]:
    """document_id → the excerpt shown, or "REDACTED"."""
    out: Dict[Any, str] = {}
    for i, source in enumerate(sources):
        metadata = source.metadata or {}
        if metadata == {"access": RESTRICTED_SOURCE_ACCESS}:
            assert source.content == "" and source.type == SourceType.KNOWLEDGE_BASE
            out[f"redacted#{i}"] = "REDACTED"
        else:
            out[metadata.get("document_id")] = source.content
    return out


@pytest.mark.asyncio
async def test_the_driver_sees_their_own_knowledge_unredacted(world):
    shown = _shown(await _messages_as(world, DRIVER, _case_with_transcript()))

    assert set(k for k in shown if not str(k).startswith("redacted")) == {
        RB_GLOBAL,
        RB_DRIVER_PERSONAL,
        RB_DRIVER_TEAM,
    }
    assert shown[RB_DRIVER_PERSONAL] == f"excerpt MARKER::{RB_DRIVER_PERSONAL}::"
    # The two the driver cannot open: a team they are not in, and no id.
    assert list(shown.values()).count("REDACTED") == 2


@pytest.mark.asyncio
async def test_a_teammate_sees_a_redacted_entry_for_the_drivers_personal_runbook(
    world,
):
    sources = await _messages_as(world, TEAMMATE, _case_with_transcript())
    shown = _shown(sources)

    assert shown[RB_DRIVER_TEAM] == f"excerpt MARKER::{RB_DRIVER_TEAM}::"
    assert shown[RB_TEAMMATE_TEAM] == f"excerpt MARKER::{RB_TEAMMATE_TEAM}::"
    assert RB_DRIVER_PERSONAL not in shown
    assert f"MARKER::{RB_DRIVER_PERSONAL}::" not in str(
        [s.model_dump() for s in sources]
    ), "the driver's personal runbook reached a teammate"


@pytest.mark.asyncio
async def test_an_outsider_sees_only_global_and_redactions(world):
    sources = await _messages_as(world, OUTSIDER, _case_with_transcript())
    shown = _shown(sources)

    assert {k for k in shown if not str(k).startswith("redacted")} == {RB_GLOBAL}
    assert list(shown.values()).count("REDACTED") == len(STORED) - 1


@pytest.mark.asyncio
async def test_a_runbook_whose_share_narrows_is_redacted_for_the_lost_viewer(world):
    case = _case_with_transcript()
    before = _shown(await _messages_as(world, TEAMMATE, case))
    assert RB_DRIVER_TEAM in before, "positive control: shared, so readable"

    await world.shares.unshare(
        resource_type="knowledge_item",
        resource_id=RB_DRIVER_TEAM,
        scope_type="team",
        scope_id=T_DRIVER,
    )
    await world.session.commit()

    after = _shown(await _messages_as(world, TEAMMATE, case))
    assert RB_DRIVER_TEAM not in after
    assert (
        list(after.values()).count("REDACTED")
        == list(before.values()).count("REDACTED") + 1
    )


@pytest.mark.asyncio
async def test_a_source_with_no_document_id_is_redacted(world):
    shown = _shown(await _messages_as(world, DRIVER, _case_with_transcript()))

    assert None not in shown, "a source with no id was shown"


@pytest.mark.asyncio
async def test_one_visibility_query_per_page_whatever_its_size(world):
    for turns in (1, 6):
        world.statements.clear()
        await _messages_as(world, TEAMMATE, _case_with_transcript(turns))
        reads = [s for s in world.statements if "FROM knowledge_items" in s]
        assert len(reads) == 1, (
            f"{turns} turns x {len(STORED)} sources: {len(reads)} visibility "
            "queries, expected one"
        )


def _turn(sources: List[Source]) -> TurnResponse:
    return TurnResponse(
        agent_response="ok",
        turn_number=1,
        milestones_completed=[],
        case_state=CaseState.INVESTIGATING,
        progress_made=False,
        attachments_processed=[],
        sources=sources,
    )


async def _turn_as(
    world: _World, viewer_id: str, sources: List[Source], *, replayed: bool = False
):
    """``POST /cases/{id}/turns`` through the route, returning ``sources``.

    ``replayed``: the request carries an ``Idempotency-Key`` whose turn already
    committed, so the route answers from the receipt (#1888) without running.
    """
    from faultmaven.modules.case.api.routes import conversation

    case = _case()
    case_service = MagicMock()
    case_service.get_case = AsyncMock(return_value=case)
    investigation_service = MagicMock()
    investigation_service.prepare_turn = AsyncMock(return_value=_turn(sources))
    investigation_service.commit_turn = AsyncMock(side_effect=lambda p, **_: p)
    keyed = SimpleNamespace(
        replay=_turn(sources), receipt_key=None, release=AsyncMock()
    )
    with (
        patch.object(
            conversation,
            "_auto_title_case_if_default",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            conversation, "open_keyed_turn", new=AsyncMock(return_value=keyed)
        ),
    ):
        return await conversation.submit_turn(
            case_id=case.case_id,
            request=_request(world),
            query="what next?",
            files=[],
            pasted_content=None,
            intent_type=None,
            intent_data=None,
            input_type=None,
            source_url=None,
            observed_at=None,
            case_service=case_service,
            investigation_service=investigation_service,
            current_user=_viewer(viewer_id),
            idempotency_key="retry-key-0001" if replayed else None,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("replayed", [False, True], ids=["live", "replayed"])
async def test_the_turn_response_is_gated_for_its_requester(world, replayed):
    stored = [Source.model_validate(_stored_source(d)) for d in STORED]

    driver_view = _shown(
        (await _turn_as(world, DRIVER, stored, replayed=replayed)).sources
    )
    outsider_view = _shown(
        (await _turn_as(world, OUTSIDER, stored, replayed=replayed)).sources
    )

    assert driver_view[RB_DRIVER_PERSONAL] == f"excerpt MARKER::{RB_DRIVER_PERSONAL}::"
    assert RB_DRIVER_PERSONAL not in outsider_view
    assert {k for k in outsider_view if not str(k).startswith("redacted")} == {
        RB_GLOBAL
    }


@pytest.mark.asyncio
async def test_an_unpublished_runbook_is_redacted_for_everyone_but_its_owner(world):
    """The id-addressed read's rule includes published-or-mine: unpublishing a
    global runbook is how it is deleted, so its stored excerpt goes too."""
    from sqlalchemy import update as sql_update

    await world.session.execute(
        sql_update(KnowledgeItemModel)
        .where(KnowledgeItemModel.item_id == RB_GLOBAL)
        .values(is_published=False)
    )
    await world.session.commit()

    shown = _shown(await _messages_as(world, TEAMMATE, _case_with_transcript()))

    assert RB_GLOBAL not in shown
    assert RB_DRIVER_TEAM in shown, "positive control"
