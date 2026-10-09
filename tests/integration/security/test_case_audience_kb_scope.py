"""A shared case retrieves only what its audience may read, on every KB path (#1919).

Runbook excerpts a turn retrieves land in the case transcript, and every reader
of the case reads it (ADR-013 D4). This drives the three engine paths that
retrieve — the pre-fetch (push), the ``kb_qa`` tool (pull) and the runbook dedup
— through real collaborators, with a shared case and an unshared one:

- shares and team memberships live in SQLite, behind the production
  ``PostgreSQLShareRepository``, ``PostgreSQLTeamRepository`` and ``TeamService``;
- the runbooks live in a real in-process ChromaDB behind the production
  ``KnowledgeVectorStore``, so ChromaDB itself evaluates every ``where`` clause.

Every vector is the same, so similarity excludes nothing and the scope filter is
the only thing that keeps a runbook out. Each negative assertion sits beside a
positive one on the same path, so a path that retrieved nothing cannot pass.

The scenario: the creator belongs to ``T_SHARED`` and ``T_OTHER``; a teammate
belongs to ``T_SHARED`` and ``T_TEAMMATE_OTHER``. The shared case is shared to
``T_SHARED`` and to ``T_RETIRED``, a team whose last member left. One runbook is
shared to each team (``T_SHARED``'s written by a third member, the author); the
creator, the teammate and a stranger each own a personal runbook; one runbook is
global. The pull path runs the turn as the teammate, so keying on the turn's
user would surface the teammate's own personal and other-team runbooks.
"""

from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Set
from unittest.mock import AsyncMock, MagicMock, patch

import chromadb
import pytest
from chromadb.config import Settings as ChromaSettings
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

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
from faultmaven.infrastructure.persistence.models import Base, TeamModel
from faultmaven.infrastructure.persistence.share_repository import (
    PostgreSQLShareRepository,
)
from faultmaven.infrastructure.persistence.team_repository import (
    PostgreSQLTeamRepository,
)
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
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)

pytestmark = [pytest.mark.integration, pytest.mark.security]

ENT = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
CREATOR = "11111111-1111-1111-1111-111111111111"
TEAMMATE = "22222222-2222-2222-2222-222222222222"
STRANGER = "33333333-3333-3333-3333-333333333333"
AUTHOR = "44444444-4444-4444-4444-444444444444"

T_SHARED = "team-shared"
T_OTHER = "team-other"
T_TEAMMATE_OTHER = "team-teammate-other"
T_RETIRED = "team-retired"

RB_GLOBAL = "rb-global"
RB_CREATOR_PERSONAL = "rb-creator-personal"
RB_TEAMMATE_PERSONAL = "rb-teammate-personal"
RB_STRANGER_PERSONAL = "rb-stranger-personal"
RB_SHARED_TEAM = "rb-shared-team"
RB_OTHER_TEAM = "rb-other-team"
RB_TEAMMATE_TEAM = "rb-teammate-other-team"
RB_RETIRED_TEAM = "rb-retired-team"

#: What a shared case's audience may read: the platform tier and the runbook of
#: the live team the case is shared with.
SHARED_CASE_READS = {RB_GLOBAL, RB_SHARED_TEAM}
#: What an unshared case's one reader, the creator, may read.
UNSHARED_CASE_READS = {RB_GLOBAL, RB_CREATOR_PERSONAL, RB_SHARED_TEAM, RB_OTHER_TEAM}
ALL_RUNBOOKS = {
    RB_GLOBAL,
    RB_CREATOR_PERSONAL,
    RB_TEAMMATE_PERSONAL,
    RB_STRANGER_PERSONAL,
    RB_SHARED_TEAM,
    RB_OTHER_TEAM,
    RB_TEAMMATE_TEAM,
    RB_RETIRED_TEAM,
}

_DIM = 8
_VEC = [0.5] * _DIM


async def _fixed_embedding(*_args: Any, **_kwargs: Any) -> List[float]:
    return list(_VEC)


def _ephemeral_client():
    """In-process ChromaDB, pinned settings, empty KB collection.

    Pinned because chromadb caches one System per settings identifier, so every
    test here shares one store; the collection is dropped so no test inherits
    another's rows (see ``test_kb_tenant_isolation_probe._ephemeral_client``).
    """
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


def _runbook(parent: str, *, scope: str, owner_id: str | None) -> Dict[str, Any]:
    """A runbook chunk with the metadata the live writer stamps.

    A team-shared runbook keeps its personal floor and its author as owner;
    team visibility comes only from ``resource_shares``.
    """
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
    if owner_id is not None:
        metadata["owner_id"] = owner_id
    return {
        "id": f"{parent}_chunk_0",
        "content": (
            f"# Runbook {parent}\nMARKER::{parent}::\n"
            "Connection pool exhausted; queries time out under load."
        ),
        "metadata": metadata,
    }


@pytest.fixture()
async def client():
    kb_client = _ephemeral_client()
    store = KnowledgeVectorStore(kb_client)
    rows = [
        _runbook(RB_GLOBAL, scope="global", owner_id=None),
        _runbook(RB_CREATOR_PERSONAL, scope="personal", owner_id=CREATOR),
        _runbook(RB_TEAMMATE_PERSONAL, scope="personal", owner_id=TEAMMATE),
        _runbook(RB_STRANGER_PERSONAL, scope="personal", owner_id=STRANGER),
        _runbook(RB_SHARED_TEAM, scope="personal", owner_id=AUTHOR),
        _runbook(RB_OTHER_TEAM, scope="personal", owner_id=STRANGER),
        _runbook(RB_TEAMMATE_TEAM, scope="personal", owner_id=STRANGER),
        _runbook(RB_RETIRED_TEAM, scope="personal", owner_id=STRANGER),
    ]
    await store.add_documents(rows, embeddings=[list(_VEC) for _ in rows])
    return kb_client


@pytest.fixture()
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(
            text(
                "INSERT INTO enterprises (enterprise_id, name, slug) "
                "VALUES (:ent, 'Acme', 'acme')"
            ),
            {"ent": ENT},
        )
        for user_id in (CREATOR, TEAMMATE, STRANGER, AUTHOR):
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
    async with factory() as s:
        yield s
    await engine.dispose()


def _case() -> Case:
    """A real case, with enough resolved content for the dedup to form a query."""
    case = Case(
        user_id=CREATOR,
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


async def _seed_sharing(session: AsyncSession, case: Case, *, shared: bool):
    """Teams, memberships and shares, through the production repositories."""
    teams = PostgreSQLTeamRepository(session)
    shares = PostgreSQLShareRepository(session)
    now = datetime.now(timezone.utc)
    for team_id in (T_SHARED, T_OTHER, T_TEAMMATE_OTHER, T_RETIRED):
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
    await teams.add_member(ENT, T_SHARED, CREATOR)
    await teams.add_member(ENT, T_OTHER, CREATOR)
    await teams.add_member(ENT, T_SHARED, TEAMMATE)
    await teams.add_member(ENT, T_SHARED, AUTHOR)
    await teams.add_member(ENT, T_TEAMMATE_OTHER, TEAMMATE)
    # Retired: its last member left (``leave_team`` sets deleted_at).
    await session.execute(
        update(TeamModel).where(TeamModel.team_id == T_RETIRED).values(deleted_at=now)
    )
    await session.commit()

    for team_id, runbook in (
        (T_SHARED, RB_SHARED_TEAM),
        (T_OTHER, RB_OTHER_TEAM),
        (T_TEAMMATE_OTHER, RB_TEAMMATE_TEAM),
        (T_RETIRED, RB_RETIRED_TEAM),
    ):
        await shares.share(
            resource_type="knowledge_item",
            resource_id=runbook,
            scope_type="team",
            scope_id=team_id,
            enterprise_id=ENT,
        )
    if shared:
        for team_id in (T_SHARED, T_RETIRED):
            await shares.share(
                resource_type="case",
                resource_id=case.case_id,
                scope_type="team",
                scope_id=team_id,
                enterprise_id=ENT,
            )
    return (
        TeamService(
            team_repository=teams,
            enterprise_repository=MagicMock(),
            user_repository=MagicMock(),
        ),
        shares,
    )


class _CapturingRouter:
    """Records the prompts ``kb_qa`` sends; answers with a stub."""

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


def _deps(team_service: Any, share_repository: Any) -> EngineDeps:
    deps = EngineDeps()
    deps.team_service = team_service
    deps.share_repository = share_repository
    deps.repository = MagicMock()
    deps.investigation_tools = None
    return deps


class _Sanitizer:
    async def asanitize(self, text: str) -> str:
        return text


async def _push(kb_client, deps: EngineDeps, case: Case) -> Set[str]:
    """The pre-fetch, through the production ``KnowledgeService`` search."""
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


async def _pull(kb_client, deps: EngineDeps, case: Case) -> Set[str]:
    """The ``kb_qa`` tool, with the context the engine builds for this turn."""
    generator = StructuredOutputGenerator(deps=deps, vectorizer=None)
    # A teammate drives this turn: the scope must not depend on who that is.
    context = await generator.build_tool_context(case, user_id=TEAMMATE)
    router = _CapturingRouter()
    adapter = KBToolAdapter(
        AnswerFromKB(vector_store=KnowledgeVectorStore(kb_client), llm_router=router)
    )
    with patch.object(kvs_module, "embed_query_or_raise", new=_fixed_embedding):
        result = await adapter.execute_with_context(
            {"question": "How do we handle connection pool exhaustion?"}, context
        )
    assert result.success, result.error
    shown = "\n".join(router.prompts)
    return {rb for rb in ALL_RUNBOOKS if f"MARKER::{rb}::" in shown}


async def _dedup(kb_client, deps: EngineDeps, case: Case) -> Set[str]:
    """The terminal-turn runbook dedup, with the engine's own resolver.

    The dedup keeps its top 3 matches, fewer than an unshared case may read, so
    what it returned cannot show the whole scope. The scope it searched under is
    recorded and evaluated by the same ChromaDB at a depth that returns all of
    it; what the dedup itself returned must sit inside that set.
    """
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
    resolver = RunbookCreator(case_locks={}, deps=deps)._runbook_dedup_scope_resolver(
        case
    )
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
    """Hold the dedup's content-readiness factor constant."""
    monkeypatch.setattr(
        tt,
        "assess_runbook_readiness",
        lambda case: MagicMock(verdict=tt.RunbookReadiness.READY, message="ready"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("path", sorted(PATHS))
async def test_a_shared_case_retrieves_only_what_its_audience_may_read(
    path, client, session
):
    case = _case()
    team_service, shares = await _seed_sharing(session, case, shared=True)

    got = await PATHS[path](client, _deps(team_service, shares), case)

    assert RB_SHARED_TEAM in got, (
        f"{path}: the shared case did not retrieve the runbook of the team it is "
        f"shared with (got {sorted(got)}) — the negatives below prove nothing"
    )
    assert (
        RB_CREATOR_PERSONAL not in got
    ), f"{path}: the shared case retrieved the creator's personal runbook"
    assert RB_OTHER_TEAM not in got, (
        f"{path}: the shared case retrieved a runbook of a team the creator is in "
        "but the case is not shared with"
    )
    assert (
        RB_RETIRED_TEAM not in got
    ), f"{path}: the shared case retrieved a retired team's runbook"
    assert not got & {
        RB_TEAMMATE_PERSONAL,
        RB_TEAMMATE_TEAM,
    }, f"{path}: the shared case retrieved by the turn's user, not the case"
    assert got == SHARED_CASE_READS


@pytest.mark.asyncio
@pytest.mark.parametrize("path", sorted(PATHS))
async def test_an_unshared_case_keeps_the_creators_scope(path, client, session):
    case = _case()
    team_service, shares = await _seed_sharing(session, case, shared=False)

    got = await PATHS[path](client, _deps(team_service, shares), case)

    assert (
        got == UNSHARED_CASE_READS
    ), f"{path}: an unshared case's scope changed (got {sorted(got)})"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["push", "pull"])
@pytest.mark.parametrize(
    "failing_lookup",
    [
        # Whether the case is shared is unknown: no personal arm is safe.
        "list_scopes_for_resource",
        # The case is shared and its teams' runbooks cannot be resolved: the
        # creator's personal KB is still outside the audience.
        "list_resource_ids",
    ],
)
async def test_a_failed_share_lookup_on_a_shared_case_retrieves_global_only(
    path, failing_lookup, client, session
):
    case = _case()
    team_service, shares = await _seed_sharing(session, case, shared=True)
    setattr(
        shares,
        failing_lookup,
        AsyncMock(side_effect=RuntimeError("share table unavailable")),
    )

    got = await PATHS[path](client, _deps(team_service, shares), case)

    assert got == {RB_GLOBAL}


@pytest.mark.asyncio
async def test_a_failed_share_lookup_fails_the_dedup_rather_than_narrowing_it(
    client, session
):
    case = _case()
    team_service, shares = await _seed_sharing(session, case, shared=True)
    shares.list_scopes_for_resource = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("share table unavailable")
    )
    kb = RunbookKnowledgeBase.over_kb_collection(client)
    kb.search_by_text = AsyncMock()  # type: ignore[method-assign]
    resolver = RunbookCreator(
        case_locks={}, deps=_deps(team_service, shares)
    )._runbook_dedup_scope_resolver(case)

    suggestion = await tt.evaluate_runbook_suggestion(case, kb, scope_resolver=resolver)

    kb.search_by_text.assert_not_awaited()
    assert suggestion.verdict == tt.RunbookSuggestion.SUGGEST_WITH_CAVEATS
    assert "could not check" in suggestion.message
