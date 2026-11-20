"""A journal entry written on one turn reaches the next turn's prompt (#2945).

The journal was saved by neither case repository, so every entry was lost at
the turn's end. ``InMemoryCaseRepository`` keeps the object itself, which is
why no engine test saw it: these turns go through the real repository, SQLite
and (under the ``postgres`` marker) PostgreSQL.
The case is stored before the first turn, as ``create_case`` stores it, so each
turn's save is the versioned UPDATE, and each turn runs on a case from a
``get()`` on a fresh session. The engine runs for real; only the LLM seam is
stubbed. No live LLM.
"""

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.milestone_engine.statement_revision import (
    confirm_revision,
)
from faultmaven.core.investigation.schemas import (
    InvestigationResponse_Diagnosis,
    JournalEntryOutput,
)
from faultmaven.infrastructure.persistence.models import Base
from faultmaven.modules.case.contracts import AnswerVia
from faultmaven.modules.case.infrastructure.postgresql_hybrid_case_repository.repository import (
    PostgreSQLHybridCaseRepository,
)
from faultmaven.modules.case.infrastructure.sqlite_case_repository.repository import (
    SQLiteCaseRepository,
)
from tests.case_builders import investigating_case
from tests.unit.core.investigation import (
    test_problem_statement_revision_and_false_alarm as revision,
)
from tests.utils import pg_tenant

pytestmark = [pytest.mark.integration]

BLOCKER = "The user cannot run psql against the orders database"


_ON_PG = os.environ.get("DATABASE_URL", "").startswith("postgresql")


@pytest.fixture(
    params=[
        "sqlite",
        pytest.param(
            "postgres",
            marks=[
                pytest.mark.postgres,
                pytest.mark.skipif(not _ON_PG, reason="set DATABASE_URL to a PG"),
            ],
        ),
    ]
)
async def store(request, tmp_path):
    """The repository class, a session factory, and the tenant a case is
    written in."""
    if request.param == "postgres":
        async with pg_tenant(os.environ["DATABASE_URL"], "jr") as tenant:
            yield SimpleNamespace(
                repo=PostgreSQLHybridCaseRepository,
                factory=tenant.factory,
                enterprise_id=tenant.enterprise_id,
                user_id=tenant.owner,
            )
        return
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'cases.db'}", poolclass=NullPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield SimpleNamespace(
        repo=SQLiteCaseRepository,
        factory=sessionmaker(engine, class_=AsyncSession, expire_on_commit=False),
        enterprise_id="ent_journal",
        user_id="user_journal",
    )
    await engine.dispose()


async def _reload(store, case):
    """Save ``case``, then load it on a fresh session, as the next request does."""
    async with store.factory() as session:
        await store.repo(session).save(case)
    async with store.factory() as session:
        return await store.repo(session).get(case.case_id)


def _engine(*responses) -> MilestoneEngine:
    llm = MagicMock()
    # Real strings where the turn path reads the provider (see
    # test_narration_overclaim_apply._make_engine for why).
    llm.provider_name = "test-provider"
    llm.config.default_model = "test-model"
    repo = MagicMock()
    repo.save = AsyncMock(side_effect=lambda c, **_: c)
    engine = MilestoneEngine(llm, repo, investigation_tools=MagicMock())
    engine.generator.generate_structured_output = AsyncMock(
        side_effect=[
            InvestigationResponse_Diagnosis.model_validate(r) for r in responses
        ]
    )
    return engine


def _prompt(call) -> str:
    return call.kwargs.get("prompt") or call.args[0]


async def test_a_blocker_written_on_turn_one_is_in_turn_twos_journal_block(
    store,
):
    engine = _engine(
        {
            "agent_response": "Please share the pool settings instead.",
            "state_updates": {
                "journal_entries": [{"entry_type": "blocker", "content": BLOCKER}]
            },
        },
        {"agent_response": "Noted.", "state_updates": {}},
    )
    case = investigating_case(
        title="Journal across turns",
        user_id=store.user_id,
        enterprise_id=store.enterprise_id,
        description="Orders requests time out",
        statement="Orders requests time out",
        current_turn=1,
    )
    case = await _reload(store, case)

    first = await engine.process_turn(
        case=case, user_message="I can't reach the DB host.", redaction_ctx=None
    )
    loaded = await _reload(store, first["case_updated"])
    # The investigation service advances the clock before the engine runs.
    loaded.current_turn += 1
    await engine.process_turn(
        case=loaded, user_message="Here is what I have.", redaction_ctx=None
    )

    second_prompt = _prompt(engine.generator.generate_structured_output.await_args)
    _, opened, rest = second_prompt.partition("<investigation_journal>")
    assert opened, "turn 2's prompt has no <investigation_journal> block"
    assert BLOCKER in rest.partition("</investigation_journal>")[0]


async def test_a_statement_revision_replay_does_not_append_the_entry_again(
    store,
):
    """The staging turn's journal entry is applied live (``journal_entries`` is
    not among ``_STAGED_FIELDS``), so confirming the revision on a later,
    reloaded turn replays the cause work and leaves the journal as it was."""
    engine = revision._engine()
    case = revision._case()
    case.user_id, case.enterprise_id = store.user_id, store.enterprise_id
    case = await _reload(store, case)
    staging = revision._revision_turn_with_cause_work()
    staging.journal_entries = [JournalEntryOutput(entry_type="finding", content="x")]
    await revision._apply(engine, case, staging)
    assert case.hypotheses == {}, "premise: the cause work is staged"

    loaded = await _reload(store, case)
    loaded.current_turn += 1
    await confirm_revision(engine.responses, loaded, {}, via=AnswerVia.CLICK)

    assert loaded.hypotheses, "premise: the staged cause work was replayed"
    assert [e.content for e in loaded.investigation_journal] == ["x"]
