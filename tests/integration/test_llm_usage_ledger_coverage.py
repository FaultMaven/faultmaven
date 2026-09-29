"""Every billed path lands in the usage ledger, through the path that runs it (#640).

Each test here drives one billed path with a REAL ``LLMRouter`` over a REAL
``ProviderRegistry`` — so the call is metered by ``registry.route_request``,
the chokepoint production meters at — and the shipped ``SqlUsageLedger`` writing
to a real SQLite database, then reads the rows back. The one double is the
provider, which is the model boundary: a ``BaseLLMProvider`` whose ``generate``
answers with fixed token counts instead of an HTTP call.

The paths, one test each:

* an engine turn — ``MilestoneEngine.process_turn``, whose binding, attribution
  and flush are what is under test. Its inner ``_process_turn_impl`` is a
  stand-in that makes billed calls through the engine's own ``llm_provider``
  (the router): the investigation logic is not what writes rows, and driving
  it would couple this module to prompt behaviour it does not care about;
* the DA direct call — a dedicated concrete DA provider, driven through the
  real tool loop, which meters it at its own call site;
* an out-of-band aside, title generation (through the real route, so the actor
  comes from ``require_authentication``) and a KB suggestion — calls made
  outside any turn, each a row of its own;
* the runbook conversion a turn spawns and does not await — metered after the
  turn has flushed, it must land as its OWN row, never be lost and never be
  added to a turn total already written;
* a call with no turn and no actor.

Also here: the failure direction (a turn keeps its answer when the ledger
cannot write) and a restart (the numbers come back from the database, not the
process).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import FastAPI
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.config.tenant_context import (
    set_current_actor_user_id,
    set_current_billing_organization_id,
    set_current_enterprise_id,
)
from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.infrastructure.llm import usage_ledger
from faultmaven.infrastructure.llm.providers.base import (
    BaseLLMProvider,
    LLMResponse,
    ProviderConfig,
    StopReason,
    StructuredOutputCapability,
    ToolCall,
)
from faultmaven.infrastructure.llm.providers.registry import (
    ProviderRegistry,
    ProviderState,
)
from faultmaven.infrastructure.llm.usage_ledger import (
    REASON_STORE_ERROR,
    SqlUsageLedger,
    drain_pending_usage_writes,
    install_usage_ledger,
)
from faultmaven.modules.case.contracts import TurnOutcome
from faultmaven.modules.case.domain.models.case import Case
from faultmaven.modules.case.domain.models.lifecycle import CaseState
from faultmaven.modules.case.domain.models.turn import TurnProgress
from tests.utils import reset_settings_singleton, seed_users

pytestmark = [pytest.mark.integration]

ENTERPRISE = STANDALONE_ENTERPRISE_ID
USER = "user_ledger_640"
CASE_ID = "case_aabb0640cafe"
PROVIDER = "anthropic"
MODEL = "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# The model boundary
# ---------------------------------------------------------------------------


class _StubProvider(BaseLLMProvider):
    """A concrete provider whose ``generate`` returns fixed usage, no HTTP."""

    def __init__(
        self,
        name: str,
        model: str,
        *,
        usage=(1000, 100, 0, 0),
        content: str = "Checkout Database Connection Pool Exhaustion",
        tool_calls=None,
    ) -> None:
        super().__init__(
            ProviderConfig(name=name, api_key="k", models=[model], default_model=model)
        )
        self._name = name
        self._model = model
        self.usage = usage
        self.content = content
        self.tool_calls = tool_calls
        self.calls = 0

    @property
    def provider_name(self) -> str:
        return self._name

    async def generate(self, prompt=None, model=None, max_tokens=1000, **kwargs):
        self.calls += 1
        input_tokens, output_tokens, cache_read, cache_write = self.usage
        return LLMResponse(
            content=self.content,
            confidence=0.95,
            provider=self._name,
            model=model or self._model,
            tokens_used=input_tokens + output_tokens,
            response_time_ms=3,
            tool_calls=self.tool_calls,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            stop_reason=StopReason.STOP,
        )

    def is_available(self) -> bool:
        return True

    def get_supported_models(self):
        return [self._model]

    def get_structured_output_capability(self, model=None):
        return StructuredOutputCapability.FUNCTION_CALLING


@pytest.fixture
def provider() -> _StubProvider:
    return _StubProvider(PROVIDER, MODEL)


@pytest.fixture
def router(monkeypatch, provider):
    """The real router over a real registry holding the stub provider."""
    registry = ProviderRegistry(settings=None)
    registry._providers = {PROVIDER: provider}
    registry._fallback_chain = [PROVIDER]
    registry._provider_states = {PROVIDER: ProviderState(name=PROVIDER)}
    registry._initialized = True
    monkeypatch.setattr(
        "faultmaven.infrastructure.llm.router.get_registry", lambda: registry
    )
    from faultmaven.infrastructure.llm.router import LLMRouter

    return LLMRouter()


# ---------------------------------------------------------------------------
# A real database, and the shipped ledger writing to it
# ---------------------------------------------------------------------------


@pytest.fixture
async def usage_db(tmp_path, monkeypatch):
    """A migrated-shape SQLite file the global engine points at.

    The tenancy and the PII switch are pinned, because both change what is
    written: CI's Cloud job runs with ``SANITIZE_PII=true``, and a multi-tenant
    provider would refuse the Standalone enterprise as a tenant.
    """
    from faultmaven.infrastructure.persistence.database import (
        close_database,
        get_db_session,
        get_engine,
        reset_engine,
    )
    from faultmaven.infrastructure.persistence.models import Base

    path = tmp_path / "usage.db"
    url = f"sqlite+aiosqlite:///{path}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("SANITIZE_PII", "false")
    monkeypatch.delenv("TENANT_PROVIDER", raising=False)
    reset_settings_singleton()
    reset_engine()

    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with get_db_session() as session:
        await seed_users(session, [USER])
        await session.execute(
            text(
                "INSERT INTO cases (case_id, enterprise_id, user_id, title, "
                "created_at, updated_at) VALUES (:c, :e, :u, 't', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"c": CASE_ID, "e": ENTERPRISE, "u": USER},
        )

    set_current_enterprise_id(ENTERPRISE)
    set_current_billing_organization_id(None)
    set_current_actor_user_id(None)
    install_usage_ledger(SqlUsageLedger())
    yield url
    await drain_pending_usage_writes()
    install_usage_ledger(None)
    set_current_actor_user_id(None)
    set_current_billing_organization_id(None)
    await close_database()
    reset_engine()
    monkeypatch.undo()
    reset_settings_singleton()


async def _read(url: str, sql: str) -> list[dict]:
    """Read through a FRESH engine: nothing the writer's process holds."""
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text(sql))
            return [dict(row._mapping) for row in result]
    finally:
        await engine.dispose()


async def _daily(url: str) -> list[dict]:
    return await _read(url, "SELECT * FROM llm_usage_daily ORDER BY model, outcome")


async def _turns(url: str) -> list[dict]:
    return await _read(url, "SELECT * FROM llm_turn_spend ORDER BY turn_number")


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------


def _case(current_turn: int = 4) -> Case:
    """A case whose clock is at 4 with one aside behind it, so the message
    clock (4) and the investigation turn (3) differ and a swap would show."""
    now = datetime.now(timezone.utc)
    return Case(
        case_id=CASE_ID,
        user_id=USER,
        enterprise_id=ENTERPRISE,
        title="Checkout 500s",
        state=CaseState.INQUIRY,
        current_turn=current_turn,
        turn_history=[
            TurnProgress(
                turn_number=2, progress_made=False, outcome=TurnOutcome.OUT_OF_BAND
            )
        ],
        created_at=now,
        updated_at=now,
    )


def _engine(router, *, da_provider=None) -> MilestoneEngine:
    return MilestoneEngine(
        llm_provider=router,
        repository=MagicMock(),
        investigation_tools=MagicMock(),
        da_provider=da_provider,
    )


def _stand_in(engine, body):
    """Replace the investigation logic with ``body`` — process_turn stays real."""

    async def impl(case, user_message, *args, user_id=None, **kwargs):
        await body()
        return {"agent_response": "the answer", "case_updated": case, "metadata": {}}

    engine._process_turn_impl = impl


async def _route(router, model: str = MODEL):
    return await router.route(
        messages=[{"role": "user", "content": "what is failing?"}], model=model
    )


class TestAnEngineTurn:
    async def test_one_turn_row_and_daily_rows_that_sum_to_it(self, usage_db, router):
        engine = _engine(router)

        async def body():
            await _route(router)
            await _route(router)
            await _route(router, model="claude-haiku-4-5")

        _stand_in(engine, body)
        result = await engine.process_turn(_case(), "why?", user_id=USER)
        assert result["agent_response"] == "the answer"

        (turn,) = await _turns(usage_db)
        assert turn["case_id"] == CASE_ID
        assert turn["turn_number"] == 4, "addressed by the message clock"
        assert turn["investigation_turn"] == 3, "labelled with the ordinal"
        assert turn["actor_user_id"] == USER
        assert (turn["billing_subject_kind"], turn["billing_subject_id"]) == (
            "account",
            USER,
        )
        assert turn["calls"] == 3
        assert turn["input_tokens"] == 3000
        assert turn["spend_weighted_tokens"] == 3300

        daily = await _daily(usage_db)
        assert {(r["model"], r["calls"]) for r in daily} == {
            (MODEL, 2),
            ("claude-haiku-4-5", 1),
        }
        for name in ("calls", "input_tokens", "output_tokens", "cache_read_tokens"):
            assert sum(r[name] for r in daily) == turn[name], name
        assert sum(r["estimated_cost_usd"] for r in daily) == pytest.approx(
            turn["estimated_cost_usd"]
        )
        assert all(r["actor_user_id"] == USER for r in daily)

    async def test_two_turns_add_to_the_days_row(self, usage_db, router):
        engine = _engine(router)
        _stand_in(engine, lambda: _route(router))

        await engine.process_turn(_case(current_turn=4), "one", user_id=USER)
        await engine.process_turn(_case(current_turn=5), "two", user_id=USER)

        assert [t["turn_number"] for t in await _turns(usage_db)] == [4, 5]
        (row,) = await _daily(usage_db)
        assert row["calls"] == 2, "a second flush must ADD to the day's row"
        assert row["input_tokens"] == 2000

    async def test_the_da_direct_call_is_metered_once(self, usage_db, router, provider):
        """A dedicated concrete DA provider bypasses the registry, so its call is
        metered at the tool loop's own site — and only there."""
        da = _StubProvider(
            "openai",
            "gpt-4o",
            usage=(2000, 200, 0, 0),
            content="",
            tool_calls=[
                ToolCall(
                    id="call_schema",
                    type="function",
                    function={
                        "name": "_Answer",
                        "arguments": json.dumps({"agent_response": "found it"}),
                    },
                )
            ],
        )
        engine = _engine(router, da_provider=da)

        class _Answer(BaseModel):
            agent_response: str = ""

        async def body():
            await engine.generator._tool_augmented_generate(
                prompt="Investigate",
                schema_model=_Answer,
                investigation_tools=[
                    {"type": "function", "function": {"name": "search_file"}}
                ],
                tool_context=MagicMock(),
            )

        _stand_in(engine, body)
        await engine.process_turn(_case(), "why?", user_id=USER)

        assert da.calls == 1 and provider.calls == 0
        (turn,) = await _turns(usage_db)
        assert turn["calls"] == 1 and turn["input_tokens"] == 2000
        (row,) = await _daily(usage_db)
        assert (row["provider"], row["model"], row["calls"]) == ("openai", "gpt-4o", 1)

    async def test_the_conversion_that_outlives_its_turn_lands_as_its_own_row(
        self, usage_db, router
    ):
        """The fire-and-forget runbook conversion inherits the turn's context,
        so it sees the turn's tracker — already flushed by the time it calls."""
        from faultmaven.modules.knowledge.domain.models.conversion import (
            CaseConversionRequest,
        )

        engine = _engine(router)
        release = asyncio.Event()
        spawned: list[asyncio.Task] = []

        async def convert_from_case(**_kwargs):
            await release.wait()
            await _route(router, model="claude-haiku-4-5")
            return SimpleNamespace(drafts=[])

        conversion = SimpleNamespace(convert_from_case=convert_from_case)

        async def body():
            await _route(router)
            request = CaseConversionRequest(
                case_id=CASE_ID, title="t", description="d", scope="personal"
            )
            spawned.append(
                asyncio.create_task(
                    engine.runbooks._run_runbook_conversion(
                        conversion, request, USER, ENTERPRISE
                    )
                )
            )

        _stand_in(engine, body)
        await engine.process_turn(_case(), "resolved", user_id=USER)
        # The turn has flushed; now let the conversion make its call.
        (turn,) = await _turns(usage_db)
        assert turn["calls"] == 1
        release.set()
        await spawned[0]
        await drain_pending_usage_writes()

        (turn_after,) = await _turns(usage_db)
        assert turn_after["calls"] == 1, "a late call must not reach the turn row"
        late = [r for r in await _daily(usage_db) if r["model"] == "claude-haiku-4-5"]
        assert len(late) == 1, "the late call was lost"
        assert late[0]["calls"] == 1
        assert late[0]["actor_user_id"] == USER, "it carries the turn's actor"

    async def test_a_ledger_failure_costs_the_turn_nothing(
        self, usage_db, router, monkeypatch
    ):
        counted: dict[str, float] = {}

        class _Counter:
            def labels(self, *, reason):
                self.reason = reason
                return self

            def inc(self, amount=1):
                counted[self.reason] = counted.get(self.reason, 0) + amount

        monkeypatch.setattr(usage_ledger, "llm_usage_unpersisted_calls", _Counter())

        async def _refuse(self, *args, **kwargs):
            raise RuntimeError("database is locked")

        monkeypatch.setattr(SqlUsageLedger, "record_turn", _refuse)
        engine = _engine(router)

        async def body():
            await _route(router)
            await _route(router)

        _stand_in(engine, body)
        result = await engine.process_turn(_case(), "why?", user_id=USER)

        assert result["agent_response"] == "the answer"
        assert counted == {REASON_STORE_ERROR: 2}
        assert await _turns(usage_db) == []


class TestARestart:
    async def test_the_numbers_come_back_from_the_database(self, usage_db, router):
        from faultmaven.container import container
        from faultmaven.infrastructure.persistence.database import (
            close_database,
            reset_engine,
        )

        engine = _engine(router)
        _stand_in(engine, lambda: _route(router))
        await engine.process_turn(_case(), "why?", user_id=USER)
        before = (await _daily(usage_db), await _turns(usage_db))

        # Everything process-local goes: the container, the engine, the ledger.
        container.reset()
        install_usage_ledger(None)
        await close_database()
        reset_engine()

        assert (await _daily(usage_db), await _turns(usage_db)) == before
        assert before[0][0]["calls"] == 1


# ---------------------------------------------------------------------------
# Calls outside any turn
# ---------------------------------------------------------------------------


class TestOutsideATurn:
    async def test_an_out_of_band_aside(self, usage_db, router):
        """Asides skip the engine (#1329): no tracker, so a row of its own."""
        from faultmaven.modules.agent.domain.services.investigation_service.service import (  # noqa: E501
            InvestigationService,
        )
        from faultmaven.modules.agent.domain.services.out_of_band import (
            OutOfBandKind,
        )
        from faultmaven.modules.case.infrastructure.case_repository import (
            InMemoryCaseRepository,
        )

        set_current_actor_user_id(USER)  # as require_authentication binds it
        engine = SimpleNamespace(deps=SimpleNamespace(llm_provider=router))
        service = InvestigationService(
            milestone_engine=engine, case_repository=InMemoryCaseRepository()
        )
        result = await service._handle_out_of_band(
            _case(), "who won the 1998 world cup?", OutOfBandKind.OFF_TOPIC
        )
        await drain_pending_usage_writes()

        assert result["agent_response"]
        assert await _turns(usage_db) == []
        (row,) = await _daily(usage_db)
        assert row["calls"] == 1
        assert row["actor_user_id"] == USER

    async def test_title_generation_through_its_route(self, usage_db, router):
        """The actor here comes from ``require_authentication`` itself."""
        from faultmaven.api.v1.auth_dependencies import get_current_user_optional
        from faultmaven.modules.auth.domain.models.auth import DevUser
        from faultmaven.modules.case.api.routes import cases
        from faultmaven.modules.case.api.routes.dependencies import (
            _di_get_case_service_dependency,
        )

        conversation = "\n".join(
            f"[2026-09-29] User: the checkout service returns HTTP 500 errors "
            f"during the evening peak and the database log shows remaining "
            f"connection slots are reserved, attempt {i}"
            for i in range(4)
        )
        state = {"title": "Case-260929-1"}

        class _Cases:
            async def get_case(self, case_id, user_id):
                return SimpleNamespace(
                    case_id=case_id,
                    title=state["title"],
                    description="",
                    is_terminal=False,
                    inquiry=None,
                )

            async def get_case_conversation_context(self, case_id, limit=10):
                return conversation

            async def update_case(self, case_id, updates, user_id):
                state.update(updates)
                return True

        app = FastAPI()
        app.include_router(cases.router, prefix="/api/v1")
        app.state.llm_provider = router
        user = DevUser(
            user_id=USER,
            username=USER,
            email=f"{USER}@example.com",
            display_name="Ledger",
            created_at=datetime.now(timezone.utc),
        )

        async def _user():
            return user

        app.dependency_overrides[get_current_user_optional] = _user
        app.dependency_overrides[_di_get_case_service_dependency] = lambda: _Cases()

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(f"/api/v1/cases/{CASE_ID}/title")
        await drain_pending_usage_writes()

        assert response.status_code == 200, response.text
        assert response.headers["x-title-source"] == "llm"
        (row,) = await _daily(usage_db)
        assert row["calls"] == 1
        assert row["actor_user_id"] == USER
        assert (row["billing_subject_kind"], row["billing_subject_id"]) == (
            "account",
            USER,
        )

    async def test_a_kb_suggestion(self, usage_db, router, provider):
        from faultmaven.modules.case.infrastructure.case_repository import (
            InMemoryCaseRepository,
        )
        from faultmaven.modules.knowledge.domain.services.suggestion_service import (
            SuggestionService,
        )
        from faultmaven.modules.knowledge.infrastructure.persistence.suggestion_repository import (  # noqa: E501
            InMemorySuggestionRepository,
        )
        from tests.runbook_samples import valid_runbook

        provider.content = valid_runbook()
        repository = InMemoryCaseRepository()
        await repository.save(_case())
        service = SuggestionService(
            case_repository=repository,
            knowledge_service=None,
            sanitizer=None,
            llm_provider=router,
            suggestion_repository=InMemorySuggestionRepository(),
        )
        set_current_actor_user_id(USER)
        await service.extract_knowledge_from_case(
            case_id=CASE_ID, enterprise_id=ENTERPRISE, extracted_by=USER
        )
        await drain_pending_usage_writes()

        (row,) = await _daily(usage_db)
        assert row["calls"] == provider.calls >= 1
        assert row["actor_user_id"] == USER

    async def test_no_turn_and_no_actor(self, usage_db, router):
        await _route(router)
        await drain_pending_usage_writes()

        (row,) = await _daily(usage_db)
        assert (row["actor_user_id"], row["billing_subject_kind"]) == ("", "none")
        assert row["billing_subject_id"] == ""

    async def test_no_actor_but_a_paying_organization(self, usage_db, router):
        """The subject follows ``billing_subject_for``: the organization pays
        even when no account acted."""
        set_current_billing_organization_id("org_640")
        await _route(router)
        await drain_pending_usage_writes()

        (row,) = await _daily(usage_db)
        assert (row["billing_subject_kind"], row["billing_subject_id"]) == (
            "organization",
            "org_640",
        )
        assert row["actor_user_id"] == ""
