"""The runbook gate runs in a worker process its deadline kills (#2661).

The gate's checks ran on the default executor's threads under a semaphore,
which bounded how many ran, never how long: a check outlived its caller, and
native regex code holding the interpreter lock stalled the loop. Each check now
runs in a worker (``runbook_gate``, ``validation_worker``), killed at its
deadline, and the gate fails closed: the deadline is the ruled 422
(``validation_timeout``), a wait for a slot past its bound and any other end of
the worker are 503s, and none admits content.

The hostile checks are ``gate_stubs``, served as the worker in place of the
real one. Every worker is a real process; the assertions are on state, never
on elapsed time. Each route test runs under a harness bound, so on a design
with no deadline it fails by the bound instead of hanging.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.api.exception_handlers import get_exception_handlers
from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.infrastructure.persistence.models import (
    Base,
    ConversionDraftModel,
    EnterpriseModel,
    KnowledgeItemModel,
)
from faultmaven.modules.knowledge.api import conversion_routes, routes
from faultmaven.modules.knowledge.domain.services import (
    runbook_gate,
    runbook_validator,
    validation_worker,
)
from faultmaven.modules.knowledge.domain.services.conversion_service.service import (
    ConversionService,
)
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)
from tests.runbook_samples import valid_runbook
from tests.unit.modules.knowledge import gate_stubs
from tests.utils import regex_only_sanitizer

pytestmark = [pytest.mark.unit, pytest.mark.knowledge_base, pytest.mark.security]

#: The harness bound: a design whose check cannot be stopped fails by it.
_HARNESS_SECONDS = 60

_TEMPLATE = {
    "title": "Connection Pool Exhausted On The API",
    "domain": "database",
    "service": "postgresql",
    "symptom_class": ["timeout"],
    "severity": "high",
    "scope": "personal",
    "symptom_recognition": "Requests fail with pool timeout errors under load.",
    "applicability": "Any service using a bounded PostgreSQL connection pool.",
    "diagnostic_steps": "### Step 1: Check pool usage\nCount the connections.",
    "causes": "### Cause A: Pool leak\n**Statement:** connections leak.",
    "prevention": "Alert on pool saturation above eighty percent.",
}


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        session.add(
            EnterpriseModel(enterprise_id=STANDALONE_ENTERPRISE_ID, name="E", slug="e")
        )
        await session.commit()
    yield factory
    await engine.dispose()


@pytest.fixture
def spawned(monkeypatch):
    """Every worker the test starts."""
    processes: list[asyncio.subprocess.Process] = []
    spawn = asyncio.create_subprocess_exec

    async def record(*args, **kwargs):
        processes.append(await spawn(*args, **kwargs))
        return processes[-1]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", record)
    return processes


@pytest.fixture
def client(session_factory):
    """The real KB routers, handlers and services, over an in-memory database."""
    knowledge = KnowledgeService(
        sanitizer=regex_only_sanitizer(),
        tracer=None,
        vector_store=None,
        db_session_factory=session_factory,
    )
    knowledge._index_document_in_vector_store = AsyncMock(return_value=5)
    app = FastAPI()
    for router in (routes.router, conversion_routes.router):
        app.include_router(router, prefix="/api/v1")
    for exc_type, handler in get_exception_handlers().items():
        app.add_exception_handler(exc_type, handler)
    app.state.team_service = None
    app.state.conversion_service = ConversionService(
        llm_router=None,
        settings=None,
        db_session_factory=session_factory,
        knowledge_service=knowledge,
        sanitizer=regex_only_sanitizer(),
    )

    async def user():
        return SimpleNamespace(
            user_id="u1",
            enterprise_id=STANDALONE_ENTERPRISE_ID,
            is_platform_admin=lambda: False,
        )

    app.dependency_overrides[routes.get_knowledge_service] = lambda: knowledge
    app.dependency_overrides[require_authentication] = user
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://kb")


def _upload(client):
    return client.post(
        "/api/v1/knowledge/documents",
        data={
            "title": "Redis evictions",
            "document_type": "runbook",
            "scope": "personal",
        },
        files={"file": ("rb.md", valid_runbook().encode(), "text/markdown")},
    )


def _create(client):
    return client.post("/api/v1/knowledge/runbooks/create", json=_TEMPLATE)


_ROUTES = [pytest.param(_upload, id="upload"), pytest.param(_create, id="draft")]


async def _written(session_factory) -> int:
    async with session_factory() as session:
        return sum(
            [
                (
                    await session.execute(select(func.count()).select_from(m))
                ).scalar_one()
                for m in (KnowledgeItemModel, ConversionDraftModel)
            ]
        )


def _in_this_process(*functions) -> bool:
    """Whether a thread of this process is inside any of ``functions``."""
    codes = {f.__code__ for f in functions}
    for frame in sys._current_frames().values():
        while frame is not None:
            if frame.f_code in codes:
                return True
            frame = frame.f_back
    return False


def _serve_stub(monkeypatch, stub: str) -> None:
    monkeypatch.setattr(runbook_gate, "_VALIDATION_WORKER", gate_stubs.__name__)
    monkeypatch.setenv("FM_GATE_STUB", stub)
    monkeypatch.setattr(runbook_gate, "_gate_slots_state", None)


@pytest.mark.parametrize("request_", _ROUTES)
@pytest.mark.parametrize("stub", ["holds_the_lock_in_c", "loops_in_python"])
async def test_a_check_past_its_deadline_is_killed_and_refused(
    monkeypatch, session_factory, client, spawned, caplog, stub, request_
):
    """While the check runs the loop turns three times with its worker alive.
    At the deadline the answer is the ruled 422, the worker was killed, no
    thread of this process is inside a check, and nothing was written."""
    _serve_stub(monkeypatch, stub)
    monkeypatch.setattr(runbook_gate, "GATE_START_SECONDS", 60)
    monkeypatch.setattr(runbook_gate, "GATE_TRANSFER_SECONDS", 60)
    monkeypatch.setattr(runbook_gate, "GATE_WORK_SECONDS", 0.5)
    caplog.set_level(logging.WARNING)
    loop, alive_after_three_turns, tasks = asyncio.get_running_loop(), [], []

    async def three_turns() -> None:
        for _ in range(3):
            await asyncio.sleep(0.005)
        alive_after_three_turns.append([p.returncode is None for p in spawned])

    class OnEntered(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.getMessage() == gate_stubs.ENTERED:
                tasks.append(loop.create_task(three_turns()))

    logging.getLogger(gate_stubs.__name__).addHandler(handler := OnEntered())
    try:
        async with asyncio.timeout(_HARNESS_SECONDS):
            response = await request_(client)
    finally:
        logging.getLogger(gate_stubs.__name__).removeHandler(handler)
    await asyncio.gather(*tasks)

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["reason"] == "validation_timeout"
    assert body["detail"] == body["help"] == runbook_gate.GATE_TIMEOUT_MESSAGE
    assert alive_after_three_turns == [[True]], "the loop did not run beside it"
    assert spawned[0].returncode == -signal.SIGKILL
    assert not _in_this_process(
        getattr(gate_stubs, stub),
        runbook_validator.RunbookValidator.validate_content,
        runbook_validator.find_security_hazards,
    )
    assert await _written(session_factory) == 0


@pytest.mark.parametrize("request_", _ROUTES)
async def test_no_slot_within_its_wait_is_503_busy(
    monkeypatch, session_factory, client, spawned, request_
):
    monkeypatch.setattr(runbook_gate, "GATE_SLOTS", 1)
    monkeypatch.setattr(runbook_gate, "GATE_SLOT_WAIT_SECONDS", 0.2)
    monkeypatch.setattr(runbook_gate, "_gate_slots_state", None)
    await runbook_gate._gate_slots().acquire("another-user")

    response = await request_(client)

    assert response.status_code == 503, response.text
    assert response.headers["x-error-code"] == "RUNBOOK_VALIDATION_BUSY"
    assert response.headers["Retry-After"] == "1"
    assert spawned == []
    assert await _written(session_factory) == 0


@pytest.mark.parametrize("request_", _ROUTES)
async def test_a_worker_that_exits_is_never_a_pass(
    monkeypatch, session_factory, client, spawned, request_
):
    _serve_stub(monkeypatch, "exits")

    response = await request_(client)

    assert response.status_code == 503, response.text
    assert response.headers["x-error-code"] == "RUNBOOK_VALIDATION_UNAVAILABLE"
    assert spawned[0].returncode == 7
    assert await _written(session_factory) == 0


@pytest.mark.parametrize("refused", ["timed_out", "busy"])
async def test_a_conversion_read_that_cannot_score_its_draft_is_refused(
    monkeypatch, session_factory, client, refused
):
    """A KB upload's draft is stored unscored, and ``GET /conversions/{id}``
    scores it: the read answers the gate's own 422 or 503, never a draft
    without a score."""
    assert (await _upload(client)).status_code == 201
    async with session_factory() as session:
        (conversion_id,) = (
            await session.execute(select(ConversionDraftModel.conversion_id))
        ).scalars()
    if refused == "busy":
        monkeypatch.setattr(runbook_gate, "GATE_SLOTS", 1)
        monkeypatch.setattr(runbook_gate, "GATE_SLOT_WAIT_SECONDS", 0.2)
        monkeypatch.setattr(runbook_gate, "_gate_slots_state", None)
        await runbook_gate._gate_slots().acquire("another-user")
    else:
        _serve_stub(monkeypatch, "loops_in_python")
        monkeypatch.setattr(runbook_gate, "GATE_WORK_SECONDS", 0.5)

    async with asyncio.timeout(_HARNESS_SECONDS):
        response = await client.get(f"/api/v1/knowledge/conversions/{conversion_id}")

    if refused == "busy":
        assert response.status_code == 503, response.text
        assert response.headers["x-error-code"] == "RUNBOOK_VALIDATION_BUSY"
        assert response.headers["Retry-After"] == "1"
    else:
        assert response.status_code == 422, response.text
        assert response.json()["reason"] == "validation_timeout"


# ---------------------------------------------------------------------------
# The same answers as in process (the oracle)
# ---------------------------------------------------------------------------


def _corpus() -> list[str]:
    pack = Path(runbook_gate.__file__).parents[5] / "resources/knowledge/pack"
    bodies = [p.read_text(encoding="utf-8") for p in sorted(pack.rglob("*.md"))]
    assert len(bodies) > 50, len(bodies)
    hazards = (
        "\n```bash\nrm -rf /\nexport AWS_ACCESS_KEY_ID=AK" + "IAQ3EGRPBZ7MXT4LKD\n```\n"
    )
    return bodies + [bodies[0] + hazards, "# Not a runbook\n", valid_runbook()]


def test_the_worker_job_answers_as_the_validator_does_in_process():
    """Every check over the pack and the refusal fixtures: the worker's job,
    in this process (coverage does not follow a worker), against the call."""
    job = validation_worker.CHECKS
    for content in _corpus():
        v, q = runbook_validator.validate_and_score(content)
        assert job["validate_and_score"](content) == {
            "validation": v.model_dump(),
            "quality": q.model_dump(),
        }
        for name in ("enforce_runbook_quality", "enforce_runbook_security"):
            try:
                getattr(runbook_validator, name)(content)
                expected = None
            except runbook_validator.RunbookQualityError as e:
                expected = {"errors": e.errors, "warnings": e.warnings}
            assert job[name](content) == {"refused": expected}, name


async def test_each_check_through_a_real_worker_answers_as_in_process(monkeypatch):
    monkeypatch.setattr(runbook_gate, "_gate_slots_state", None)
    content = _corpus()[-3]  # a pack runbook holding a credential

    v, q = await runbook_gate.avalidate_and_score(content, principal="u1")

    assert (v, q) == runbook_validator.validate_and_score(content)
    for check in ("enforce_runbook_quality", "enforce_runbook_security"):
        with pytest.raises(runbook_validator.RunbookQualityError) as in_process:
            getattr(runbook_validator, check)(content)
        with pytest.raises(runbook_validator.RunbookQualityError) as gated:
            await getattr(runbook_gate, f"a{check}")(content, principal="u1")
        assert type(gated.value) is runbook_validator.RunbookQualityError
        assert (gated.value.errors, gated.value.warnings) == (
            in_process.value.errors,
            in_process.value.warnings,
        )


# ---------------------------------------------------------------------------
# A conversion's draft fails with the reason
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "refusal,retryable,reason",
    [
        (runbook_gate.GateTimedOut, False, "validation_timeout"),
        (runbook_gate.GateBusy, True, None),
    ],
)
async def test_a_conversion_draft_fails_with_the_gates_reason(
    monkeypatch, refusal, retryable, reason
):
    from unittest.mock import AsyncMock, MagicMock

    from faultmaven.infrastructure.llm.providers import LLMResponse, StopReason
    from faultmaven.modules.knowledge.domain.models.conversion import (
        ConversionError,
        FailureModeAnalysis,
    )
    from faultmaven.modules.knowledge.domain.services.conversion_service import (
        service as service_module,
    )

    async def refuse(content, *, principal):
        assert principal == "u1"
        raise refusal()

    monkeypatch.setattr(service_module, "avalidate_and_score", refuse)
    router = MagicMock()
    router.route = AsyncMock(
        return_value=LLMResponse(
            content=valid_runbook("Redis Notes For The Cache Tier"),
            confidence=0.9,
            provider="test",
            model="test-model",
            tokens_used=100,
            response_time_ms=10,
            stop_reason=StopReason.STOP,
        )
    )
    settings = MagicMock()
    settings.llm.explicit_role_provider.return_value = None
    service = ConversionService(
        sanitizer=regex_only_sanitizer(),
        llm_router=router,
        settings=settings,
        db_session_factory=None,
    )

    result = await service._convert_single_failure_mode(
        prepared=service_module._PreparedConversion(
            knowledge_model="test-model",
            redacted_messages=[],
            runbook_id="redis-notes",
            conversion_id="conv_test",
        ),
        failure_mode=FailureModeAnalysis(
            id="fm-1",
            title="Redis Notes",
            domain="database",
            service="redis",
            symptom_class=[],
            severity="high",
            symptoms_summary="s",
            resolution_summary="r",
        ),
        scope="global",
        user_id="u1",
        enterprise_id=None,
    )

    assert result == ConversionError(
        failure_mode_id="fm-1",
        error=str(refusal()),
        retryable=retryable,
        reason=reason,
    )
