"""The runbook gate runs in a worker process its deadline stops (#2661).

The gate's checks ran on the default executor's threads under a semaphore,
which bounded how many ran, never how long: a check outlived its caller, and
native regex code holding the interpreter lock stalled the loop. The checks a
request needs now run in one worker call (``runbook_gate``,
``validation_worker``), each under a CPU-time deadline, and the gate fails
closed. A refusal for the input alone is a 422 with its ``reason``; one that
depends on load is a 503 with ``Retry-After``; a check that raises is the
generic 500. None admits content.

The hostile checks are ``gate_stubs``, served as the worker in place of the
real one. Every worker is a real process, and every bound a test sets is one
its stub cannot meet (an endless loop, a sleep of an hour), so no assertion
depends on how fast this machine is. Each route test runs under a harness
bound, so on a design with no deadline it fails by the bound instead of
hanging.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import pickle
import signal
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import null, select, update

from faultmaven.api.exception_handlers import (
    SERVICE_ERROR_DETAIL,
    get_exception_handlers,
    http_exception_handler,
)
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
from faultmaven.modules.knowledge.domain.services.conversion_service import (
    service as service_module,
)
from faultmaven.modules.knowledge.domain.services.conversion_service.service import (
    ConversionService,
)
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)
from faultmaven.modules.knowledge.exceptions import (
    GateBusy,
    GateFailed,
    GateOutputTooLarge,
    GateRefused,
    GateTimedOut,
    GateTooLarge,
    GateUnavailable,
    RunbookQualityError,
)
from tests.runbook_samples import valid_runbook
from tests.unit.modules.knowledge import gate_stubs
from tests.utils import regex_only_sanitizer

pytestmark = [pytest.mark.unit, pytest.mark.knowledge_base, pytest.mark.security]

#: The authenticated caller: every gate check must run under this principal.
USER = "user-7"

#: The harness bound: a design whose check cannot be stopped fails by it.
_HARNESS_SECONDS = 60

_REFUSALS = [
    GateTimedOut,
    GateOutputTooLarge,
    GateTooLarge,
]
_UNAVAILABLE = [GateBusy, GateUnavailable]

_TEMPLATE = {
    "title": "Connection Pool Exhausted On The API",
    "domain": "database",
    "service": "postgresql",
    "symptom_class": ["timeout"],
    "severity": "high",
    "scope": "personal",
    "symptom_recognition": "Requests fail with pool timeout errors under load.",
    "applicability": "Any service using a bounded PostgreSQL connection pool.",
    "diagnostic_steps": (
        "### Step 1: Check pool usage\n```bash\npsql -c 'select count(*) from "
        "pg_stat_activity'\n```\nLook for a count near the pool ceiling."
    ),
    "causes": (
        "### Cause A: Pool leak\n"
        "**Statement:** connections are never returned to the pool.\n"
        "**Indicators:**\n- root: [Step 1] active connections pinned at the cap\n"
        "**Interventions:**\n- **remediation** (root): fix the leak.\n"
        "  **Verification:** Re-run Step 1.\n\n"
        "### Cause Z: Unidentified\n"
        "**Statement:** none of the causes match.\n"
        "**Indicators:**\n- [Default]\n"
        "**Interventions:**\n- **mitigation** (D): consult an SME.\n"
    ),
    "prevention": "Alert on pool saturation above eighty percent.",
}


@pytest.fixture
async def session_factory():
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

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


@pytest.fixture(autouse=True)
def fresh_slots(monkeypatch):
    monkeypatch.setattr(runbook_gate, "_gate_slots_state", None)


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
def app(session_factory):
    """The real KB routers over an in-memory database, with the handlers the
    app registers (``main.py``): the domain ones and ``HTTPException``'s."""
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
    app.add_exception_handler(HTTPException, http_exception_handler)
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
            user_id=USER,
            enterprise_id=STANDALONE_ENTERPRISE_ID,
            is_platform_admin=lambda: False,
        )

    app.dependency_overrides[routes.get_knowledge_service] = lambda: knowledge
    app.dependency_overrides[require_authentication] = user
    app.state.knowledge = knowledge
    return app


@pytest.fixture
def client(app):
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    client = httpx.AsyncClient(transport=transport, base_url="http://kb")
    client.knowledge = app.state.knowledge
    return client


def _upload(client):
    return client.post(
        "/api/v1/knowledge/documents",
        data={"title": "Redis evictions", "document_type": "runbook"},
        files={"file": ("rb.md", valid_runbook().encode(), "text/markdown")},
    )


def _create(client):
    return client.post("/api/v1/knowledge/runbooks/create", json=_TEMPLATE)


async def _state(session_factory) -> list:
    """Every row's text and status, which a refusal must leave as they were.
    (A read's stored refusal is ``quality_details``, tested on its own.)"""
    async with session_factory() as session:
        items = (await session.execute(select(KnowledgeItemModel))).scalars()
        drafts = (await session.execute(select(ConversionDraftModel))).scalars()
        return sorted(
            [("item", i.item_id, i.content) for i in items]
            + [("draft", d.id, d.content, d.status) for d in drafts]
        )


async def _only(session_factory, column):
    async with session_factory() as session:
        (value,) = (await session.execute(select(column))).scalars()
        return value


def _in_this_process(*functions) -> bool:
    """Whether a thread of this process is inside any of ``functions``."""
    codes = {f.__code__ for f in functions}
    for frame in sys._current_frames().values():
        while frame is not None:
            if frame.f_code in codes:
                return True
            frame = frame.f_back
    return False


def _serve_stub(monkeypatch, stub: str, **env: str) -> None:
    monkeypatch.setattr(runbook_gate, "_VALIDATION_WORKER", gate_stubs.__name__)
    monkeypatch.setenv("FM_GATE_STUB", stub)
    for name, value in env.items():
        monkeypatch.setenv(name, value)


def _refusal_body(refusal) -> dict:
    return {
        "error": "Validation Error",
        "detail": refusal.message,
        "message": refusal.message,
        "errors": [refusal.message],
        "warnings": [],
        "help": refusal.message,
        "reason": refusal.reason,
    }


# ---------------------------------------------------------------------------
# The deadline: a check past it is stopped, and refused
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("request_", [_upload, _create], ids=["upload", "create"])
@pytest.mark.parametrize("stub", ["holds_the_lock_in_c", "loops_in_python"])
async def test_a_check_past_its_deadline_is_killed_and_refused(
    monkeypatch, session_factory, client, spawned, stub, request_
):
    """While the check runs the loop turns three times with its worker alive.
    Past its CPU deadline the worker is ended by ``SIGPROF``, the answer is the
    ruled 422, no thread of this process is inside a check, and nothing was
    written. On ``main``'s thread design there is no worker to end."""
    _serve_stub(monkeypatch, stub)
    monkeypatch.setattr(runbook_gate, "GATE_START_SECONDS", _HARNESS_SECONDS)
    monkeypatch.setattr(runbook_gate, "GATE_WORK_WALL_SECONDS", _HARNESS_SECONDS)
    monkeypatch.setattr(runbook_gate, "GATE_WORK_CPU_SECONDS", 0.5)
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
    logging.getLogger(gate_stubs.__name__).setLevel(logging.WARNING)
    try:
        async with asyncio.timeout(_HARNESS_SECONDS):
            response = await request_(client)
    finally:
        logging.getLogger(gate_stubs.__name__).removeHandler(handler)
    await asyncio.gather(*tasks)

    assert response.status_code == 422, response.text
    assert response.json() == _refusal_body(GateTimedOut)
    assert alive_after_three_turns == [[True]], "the loop did not run beside it"
    assert [p.returncode for p in spawned] == [-signal.SIGPROF]
    assert not _in_this_process(
        getattr(gate_stubs, stub),
        runbook_validator.RunbookValidator.validate_content,
        runbook_validator.find_security_hazards,
    )
    assert await _state(session_factory) == []


# ---------------------------------------------------------------------------
# How each end of a worker answers (real workers, the gate's entry point)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stub,limits,refusal,code",
    [
        pytest.param(
            "loops_in_python",
            {"GATE_WORK_CPU_SECONDS": 60, "GATE_CPU_LIMIT_SECONDS": 1},
            GateTimedOut,
            -signal.SIGXCPU,
            id="cpu-backstop",
        ),
        pytest.param("allocates", {}, GateTooLarge, 3, id="memory-limit"),
        pytest.param(
            "floods",
            {"GATE_OUTPUT_LIMIT_BYTES": 64 * 1024},
            GateOutputTooLarge,
            None,
            id="output-limit",
        ),
        pytest.param(
            "is_killed",
            {},
            GateUnavailable,
            -signal.SIGKILL,
            id="killed",
        ),
        pytest.param("raises", {}, GateFailed, 1, id="check-raises"),
        pytest.param(
            "fails_costly_to_report",
            {"GATE_WORK_CPU_SECONDS": 0.3, "FM_GATE_STUB_WORK_CPU": "0.3"},
            GateFailed,
            1,
            id="timer-disarmed-on-failure",
        ),
        pytest.param(
            "sleeps",
            {"GATE_WORK_WALL_SECONDS": 0.5},
            GateUnavailable,
            -signal.SIGKILL,
            id="wall-backstop",
        ),
        pytest.param(
            "never_starts",
            {"GATE_START_SECONDS": 0.5},
            GateUnavailable,
            -signal.SIGKILL,
            id="start-timeout",
        ),
    ],
)
async def test_each_end_of_a_worker_answers_as_ruled(
    monkeypatch, spawned, stub, limits, refusal, code
):
    """Only the input decides a 422 (CPU, memory, output). A check that
    raises is a defect, a 500 that no retry changes, and it is reported as
    that even when reporting it costs more CPU than the deadline left: the
    timer ends with the check. A kill, the wall-clock backstop and a start
    that never finishes are 503s."""
    _serve_stub(monkeypatch, stub)
    for name, value in limits.items():
        if name.startswith("FM_"):
            monkeypatch.setenv(name, value)
        else:
            monkeypatch.setattr(runbook_gate, name, value)

    async with asyncio.timeout(_HARNESS_SECONDS):
        with pytest.raises(refusal) as refused:
            await runbook_gate.avalidate_and_score("body", principal=USER)

    assert type(refused.value) is refusal
    assert len(spawned) == 1 and spawned[0].returncode is not None
    if code is not None:
        assert spawned[0].returncode == code
    assert runbook_gate._gate_slots().locked() is False


async def test_imports_that_burn_cpu_leave_the_work_its_whole_deadline(
    monkeypatch, caplog
):
    """The deadline counts the work alone: a worker that spent twice its
    deadline of CPU before the work still answers after half a deadline of
    work. A limit counted from the process's start would have ended it."""
    _serve_stub(
        monkeypatch,
        "works_half_its_deadline",
        FM_GATE_STUB_IMPORT_CPU="1.0",
        FM_GATE_STUB_WORK_CPU="0.25",
    )
    monkeypatch.setattr(runbook_gate, "GATE_WORK_CPU_SECONDS", 0.5)
    monkeypatch.setattr(runbook_gate, "GATE_START_SECONDS", _HARNESS_SECONDS)
    monkeypatch.setattr(runbook_gate, "GATE_WORK_WALL_SECONDS", _HARNESS_SECONDS)
    caplog.set_level(logging.WARNING, logger=gate_stubs.__name__)

    async with asyncio.timeout(_HARNESS_SECONDS):
        validation, _ = await runbook_gate.avalidate_and_score("b", principal=USER)

    assert validation.passed
    (before,) = [
        float(r.getMessage().split()[-1])
        for r in caplog.records
        if r.getMessage().startswith("cpu before work")
    ]
    assert before >= 2 * runbook_gate.GATE_WORK_CPU_SECONDS


async def test_no_slot_within_its_wait_is_busy_and_spawns_nothing(monkeypatch, spawned):
    monkeypatch.setattr(runbook_gate, "GATE_SLOTS", 1)
    monkeypatch.setattr(runbook_gate, "GATE_SLOT_WAIT_SECONDS", 0.2)
    await runbook_gate._gate_slots().acquire("another-user")

    with pytest.raises(GateBusy):
        await runbook_gate.aenforce_runbook_security("body", principal=USER)

    assert spawned == []


async def test_a_check_runs_only_for_a_named_user(spawned):
    for principal in (None, ""):
        with pytest.raises(ValueError):
            await runbook_gate.avalidate_and_score("body", principal=principal)
    assert spawned == []


# ---------------------------------------------------------------------------
# The answer on every route that runs the gate, through the app's handlers
# ---------------------------------------------------------------------------


async def _draft_url(client) -> str:
    created = (await _create(client)).json()
    return (
        f"/api/v1/knowledge/conversions/{created['conversion_id']}/drafts/"
        f"{created['draft']['draft_id']}"
    )


async def _upload_route(client, session_factory):
    return lambda: _upload(client)


async def _edit_route(client, session_factory):
    item_id = (await _upload(client)).json()["document_id"]
    content = valid_runbook("Redis Evictions, Edited")
    return lambda: client.put(
        f"/api/v1/knowledge/documents/{item_id}", json={"content": content}
    )


async def _create_route(client, session_factory):
    return lambda: _create(client)


async def _draft_edit_route(client, session_factory):
    url = await _draft_url(client)
    body = valid_runbook("Connection Pool Exhausted, Edited")
    return lambda: client.put(url, json={"content": body})


async def _verify_route(client, session_factory):
    url = await _draft_url(client)
    return lambda: client.post(f"{url}/verify")


async def _stored_unscored_read_route(client, session_factory):
    """A KB upload's draft as stored before #2661: no score."""
    assert (await _upload(client)).status_code == 201
    async with session_factory() as session:
        await session.execute(
            update(ConversionDraftModel).values(
                quality_score=None, quality_details=null()
            )
        )
        await session.commit()
    conversion_id = await _only(session_factory, ConversionDraftModel.conversion_id)
    return lambda: client.get(f"/api/v1/knowledge/conversions/{conversion_id}")


_GATE_ROUTES = [
    pytest.param(_upload_route, id="POST /knowledge/documents"),
    pytest.param(_edit_route, id="PUT /knowledge/documents/{id}"),
    pytest.param(_create_route, id="POST /knowledge/runbooks/create"),
    pytest.param(_draft_edit_route, id="PUT /conversions/{id}/drafts/{draft_id}"),
    pytest.param(_verify_route, id="POST .../drafts/{draft_id}/verify"),
    pytest.param(_stored_unscored_read_route, id="GET /conversions/{id}"),
]


def _refuse_with(monkeypatch, refusal) -> None:
    async def refuse(checks, principal, slot_wait=None):
        assert principal == USER
        raise refusal()

    monkeypatch.setattr(runbook_gate, "_run", refuse)


@pytest.mark.parametrize("route", _GATE_ROUTES)
@pytest.mark.parametrize("refusal", _REFUSALS, ids=lambda r: r.reason)
async def test_every_gate_route_answers_a_refusal_with_its_reason(
    monkeypatch, session_factory, client, route, refusal
):
    send = await route(client, session_factory)
    before = await _state(session_factory)
    _refuse_with(monkeypatch, refusal)

    response = await send()

    assert response.status_code == 422, response.text
    assert response.json() == _refusal_body(refusal)
    routes.RunbookCheckRefusal.model_validate(response.json())
    assert await _state(session_factory) == before


@pytest.mark.parametrize("route", _GATE_ROUTES)
@pytest.mark.parametrize("refusal", _UNAVAILABLE, ids=lambda r: r.error_code)
async def test_every_gate_route_answers_a_check_that_cannot_run_with_503(
    monkeypatch, session_factory, client, route, refusal
):
    send = await route(client, session_factory)
    before = await _state(session_factory)
    _refuse_with(monkeypatch, refusal)

    response = await send()

    assert response.status_code == 503, response.text
    assert response.headers["x-error-code"] == refusal.error_code
    assert response.headers["Retry-After"] == str(runbook_gate.GATE_SLOT_WAIT_SECONDS)
    assert response.json() == {"detail": refusal.message}
    assert await _state(session_factory) == before


@pytest.mark.parametrize("route", _GATE_ROUTES)
async def test_every_gate_route_answers_a_check_that_raised_with_500(
    monkeypatch, session_factory, client, route
):
    """A defect in the check: the static 500 with no ``Retry-After``, which
    no retry of the same document changes."""
    send = await route(client, session_factory)
    before = await _state(session_factory)
    _refuse_with(monkeypatch, GateFailed)

    response = await send()

    assert response.status_code == 500, response.text
    assert response.headers["x-error-code"] == "SERVICE_ERROR"
    assert "Retry-After" not in response.headers
    assert response.json() == {"detail": SERVICE_ERROR_DETAIL}
    assert await _state(session_factory) == before


async def test_a_check_that_raises_in_its_worker_is_a_500_at_upload(
    monkeypatch, session_factory, client, spawned, caplog
):
    _serve_stub(monkeypatch, "raises")

    async with asyncio.timeout(_HARNESS_SECONDS):
        response = await _upload(client)

    assert response.status_code == 500, response.text
    assert response.headers["x-error-code"] == "SERVICE_ERROR"
    assert "Retry-After" not in response.headers
    assert [p.returncode for p in spawned] == [1]
    assert any(
        r.levelno == logging.ERROR and "a check that failed" in r.getMessage()
        for r in caplog.records
    )
    assert await _state(session_factory) == []


@pytest.mark.parametrize(
    "frontmatter",
    ["last_updated: 2026-11-31", "tags: [2024-02-30]"],
    ids=["no-such-day", "no-such-day-in-a-list"],
)
async def test_a_scalar_yaml_cannot_construct_is_unparseable_frontmatter(
    session_factory, client, frontmatter
):
    """PyYAML resolves these as dates and its constructor raises. They are the
    frontmatter refusal the quality gate answers, never an end of its worker."""
    body = valid_runbook().replace("last_updated: 2026-08-28", frontmatter)
    if frontmatter.startswith("tags"):
        body = valid_runbook().replace("status: draft", f"status: draft\n{frontmatter}")

    async with asyncio.timeout(_HARNESS_SECONDS):
        response = await client.post(
            "/api/v1/knowledge/documents",
            data={"title": "Redis evictions", "document_type": "runbook"},
            files={"file": ("rb.md", body.encode(), "text/markdown")},
        )

    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail.startswith("Runbook does not meet quality standards: ")
    assert "No YAML frontmatter found" in detail
    assert "reason" not in response.json()
    assert await _state(session_factory) == []


def test_every_gate_route_declares_its_answers(app):
    """Each 422 body a gate route answers (a binding error, a `detail`
    refusal, the check's refusal), and the 503 codes and header."""
    paths = app.openapi()["paths"]
    declared = {
        ("post", "/api/v1/knowledge/documents"): (422, 503),
        ("put", "/api/v1/knowledge/documents/{document_id}"): (422, 503),
        ("post", "/api/v1/knowledge/runbooks/create"): (422, 503),
        (
            "put",
            "/api/v1/knowledge/conversions/{conversion_id}/drafts/{draft_id}",
        ): (422, 503),
        (
            "post",
            "/api/v1/knowledge/conversions/{conversion_id}/drafts/{draft_id}/verify",
        ): (422, 503),
        ("get", "/api/v1/knowledge/conversions/{conversion_id}"): (422, 503),
        ("post", "/api/v1/knowledge/scan"): (503,),
    }
    refusal = routes.RunbookCheckRefusal.model_json_schema()
    assert set(refusal["properties"]["reason"]["enum"]) == {r.reason for r in _REFUSALS}
    for (method, path), statuses in declared.items():
        answers = paths[path][method]["responses"]
        if 422 in statuses:
            schema = answers["422"]["content"]["application/json"]["schema"]
            assert schema == {
                "anyOf": [
                    {"$ref": "#/components/schemas/HTTPValidationError"},
                    routes.RefusalDetail.model_json_schema(),
                    refusal,
                ]
            }, path
        assert "RUNBOOK_VALIDATION_BUSY" in answers["503"]["description"], path
        assert {"x-error-code", "Retry-After"} <= set(answers["503"]["headers"])
    assert "HTTPValidationError" in app.openapi()["components"]["schemas"]


# ---------------------------------------------------------------------------
# The read never runs the gate on a row written now
# ---------------------------------------------------------------------------


async def test_an_uploaded_draft_is_stored_with_its_score_and_read_without_a_check(
    monkeypatch, session_factory, client
):
    assert (await _upload(client)).status_code == 201
    stored = await _only(session_factory, ConversionDraftModel.quality_details)
    _, expected = runbook_validator.validate_and_score(valid_runbook())
    conversion_id = await _only(session_factory, ConversionDraftModel.conversion_id)
    checks: list[str] = []
    real_run = runbook_gate._run

    async def counted(check, *args, **kwargs):
        checks.append(check)
        return await real_run(check, *args, **kwargs)

    monkeypatch.setattr(runbook_gate, "_run", counted)

    response = await client.get(f"/api/v1/knowledge/conversions/{conversion_id}")

    assert response.status_code == 200, response.text
    assert stored == expected.model_dump()
    assert response.json()["drafts"][0]["quality_score"] == stored
    assert checks == []


async def test_a_row_stored_without_a_score_is_scored_once_outside_any_session(
    monkeypatch, session_factory, app, client
):
    """The pre-#2661 row: its read scores it through the gate with no session
    open, stores the score, and the next read runs no check."""
    send = await _stored_unscored_read_route(client, session_factory)
    open_sessions, at_the_gate = [0], []
    conversion_service = app.state.conversion_service
    real_factory = conversion_service._db_session_factory

    class Counted:
        def __init__(self):
            self._session = real_factory()

        async def __aenter__(self):
            open_sessions[0] += 1
            return await self._session.__aenter__()

        async def __aexit__(self, *exc):
            open_sessions[0] -= 1
            return await self._session.__aexit__(*exc)

    async def gate(content, *, principal, slot_wait=None):
        at_the_gate.append((open_sessions[0], principal))
        return runbook_validator.validate_and_score(content)

    monkeypatch.setattr(conversion_service, "_db_session_factory", Counted)
    monkeypatch.setattr(service_module, "avalidate_and_score", gate)

    first, second = await send(), await send()

    assert (first.status_code, second.status_code) == (200, 200)
    assert at_the_gate == [(0, USER)]
    stored = await _only(session_factory, ConversionDraftModel.quality_details)
    assert first.json()["drafts"][0]["quality_score"] == stored
    assert second.json() == first.json()


async def test_a_stored_row_refused_by_its_check_answers_that_refusal_again(
    monkeypatch, session_factory, client, spawned
):
    """A pre-#2661 row whose read-time check is refused for its text: the
    refusal is stored, the next read answers the same 422 with no worker, and
    the draft listing shows no score for it."""
    send = await _stored_unscored_read_route(client, session_factory)
    _serve_stub(monkeypatch, "loops_in_python")
    monkeypatch.setattr(runbook_gate, "GATE_WORK_CPU_SECONDS", 0.3)
    monkeypatch.setattr(runbook_gate, "GATE_START_SECONDS", _HARNESS_SECONDS)
    monkeypatch.setattr(runbook_gate, "GATE_WORK_WALL_SECONDS", _HARNESS_SECONDS)
    spawned.clear()

    async with asyncio.timeout(_HARNESS_SECONDS):
        first, second = await send(), await send()

    assert (first.status_code, second.status_code) == (422, 422)
    assert first.json() == second.json() == _refusal_body(GateTimedOut)
    assert [p.returncode for p in spawned] == [-signal.SIGPROF]
    listed = (await client.get("/api/v1/knowledge/drafts")).json()
    assert [d["quality_details"] for d in listed] == [None]


async def test_a_stored_row_whose_check_cannot_run_is_checked_again(
    monkeypatch, session_factory, client
):
    """Load is no fact about the text: a 503 stores nothing."""
    send = await _stored_unscored_read_route(client, session_factory)
    _refuse_with(monkeypatch, GateBusy)
    assert (await send()).status_code == 503
    monkeypatch.undo()

    response = await send()

    assert response.status_code == 200, response.text
    assert response.json()["drafts"][0]["quality_score"]["overall"] > 0


async def test_an_upload_and_a_verify_each_spawn_one_worker(
    session_factory, client, spawned
):
    """Every check a request needs runs in one worker: the upload's quality
    and security checks of one text, and verify's check of the draft and of
    the text it publishes. Nothing is checked twice."""
    async with asyncio.timeout(_HARNESS_SECONDS):
        assert (await _upload(client)).status_code == 201
        uploaded = len(spawned)
        draft = await _draft_url(client)
        created = len(spawned)
        verified = await client.post(f"{draft}/verify")

    assert verified.status_code == 200, verified.text
    assert (uploaded, created - uploaded, len(spawned) - created) == (1, 1, 1)


async def test_an_ingest_no_user_asked_for_checks_as_the_system(monkeypatch):
    """The pack's runbooks have no owner: their check takes the system's slot
    key, and still runs."""
    principals: list[str] = []

    async def run(checks, principal, slot_wait=None):
        principals.append(principal)
        raise GateBusy()

    monkeypatch.setattr(runbook_gate, "_run", run)
    service = KnowledgeService(
        sanitizer=regex_only_sanitizer(),
        tracer=None,
        vector_store=None,
        db_session_factory=AsyncMock(),
    )

    with pytest.raises(GateBusy):
        await service.ingest_runbook(
            document_id="kb_0123456789abcdef",
            title="Runbook",
            content=valid_runbook(),
            enterprise_id=STANDALONE_ENTERPRISE_ID,
            scope="global",
            record=AsyncMock(),
        )

    assert principals == [runbook_gate.SYSTEM_GATE_PRINCIPAL]


# ---------------------------------------------------------------------------
# The same answers as in process (the oracle)
# ---------------------------------------------------------------------------


def _pack() -> list[str]:
    pack = Path(runbook_gate.__file__).parents[5] / "resources/knowledge/pack"
    bodies = [p.read_text(encoding="utf-8") for p in sorted(pack.rglob("*.md"))]
    assert len(bodies) > 50, len(bodies)
    return bodies


def _refused_fixtures() -> list[str]:
    """Bodies each check refuses: hazards, non-ASCII text in the errors, and a
    long list of errors."""
    first = _pack()[0]
    hazards = (
        "\n```bash\nrm -rf /\nexport AWS_ACCESS_KEY_ID=AK" + "IAQ3EGRPBZ7MXT4LKD\n```\n"
    )
    non_ascii = first.replace("title: ", "title: Ünïcödé — 日本語 ", 1)
    many = valid_runbook().replace(
        "symptom_class:", "symptom_class: [" + ", ".join(f"bad{i}" for i in range(40))
    )
    return [first + hazards, non_ascii + "\n## Ünïcödé\n", many, "# Not a runbook\n"]


def _in_process(check: str, content: str) -> dict:
    if check == "validate_and_score":
        v, q = runbook_validator.validate_and_score(content)
        return {"validation": v.model_dump(), "quality": q.model_dump()}
    try:
        getattr(runbook_validator, check)(content)
    except RunbookQualityError as e:
        return {"refused": {"errors": e.errors, "warnings": e.warnings}}
    if check == "enforce_runbook_quality":
        _, q = runbook_validator.validate_and_score(content)
        return {"refused": None, "quality": q.model_dump()}
    return {"refused": None}


async def _through_workers(monkeypatch, bodies: list[str]) -> None:
    """Each body's verdict and score through a real worker, against the call
    in this process, by value. All at once, so each waits for its slot."""
    monkeypatch.setattr(runbook_gate, "GATE_SLOT_WAIT_SECONDS", 10 * _HARNESS_SECONDS)

    async def one(i: int, content: str) -> None:
        v, q = await runbook_gate.avalidate_and_score(content, principal=f"u{i % 4}")
        expected = _in_process("validate_and_score", content)
        assert {"validation": v.model_dump(), "quality": q.model_dump()} == expected

    async with asyncio.timeout(10 * _HARNESS_SECONDS):
        await asyncio.gather(*(one(i, c) for i, c in enumerate(bodies)))


async def test_the_pack_sampled_answers_through_a_worker_as_in_process(monkeypatch):
    await _through_workers(monkeypatch, _pack()[::5] + [valid_runbook()])


@pytest.mark.slow
async def test_the_whole_pack_answers_through_a_worker_as_in_process(monkeypatch):
    await _through_workers(monkeypatch, _pack())


def test_the_worker_job_answers_as_the_validator_does_in_process():
    """Every check over the pack and the refused fixtures: the worker's job in
    this process (coverage does not follow a worker), against the call."""
    for content in _pack() + _refused_fixtures():
        for check, job in validation_worker.CHECKS.items():
            assert job(content) == _in_process(check, content), check


@pytest.mark.parametrize("content", _refused_fixtures())
async def test_each_refusal_round_trips_through_a_real_worker(monkeypatch, content):
    for check in ("enforce_runbook_quality", "enforce_runbook_security"):
        try:
            getattr(runbook_validator, check)(content)
            expected = None
        except RunbookQualityError as e:
            expected = (e.errors, e.warnings)
        gate = getattr(runbook_gate, f"a{check}")
        if expected is None:
            answer = await gate(content, principal=USER)
            if check == "enforce_runbook_quality":
                # The same call's security answer, for the publication.
                _, security = answer
                assert security == runbook_gate.SecurityAnswer(
                    content, _in_process("enforce_runbook_security", content)["refused"]
                )
            continue
        with pytest.raises(RunbookQualityError) as gated:
            await gate(content, principal=USER)
        assert type(gated.value) is RunbookQualityError
        assert (gated.value.errors, gated.value.warnings) == expected, check
    await _through_workers(monkeypatch, [content])


async def test_verify_checks_the_draft_and_the_text_it_publishes_in_one_call():
    """The draft's own refusal answers first and stops the call; once it
    passes, the published text's answer comes back to be enforced later."""
    clean, hazardous = _pack()[0], _refused_fixtures()[0]
    expected = _in_process("enforce_runbook_security", hazardous)["refused"]
    assert expected is not None

    answer = await runbook_gate.aenforce_runbook_security(
        clean, principal=USER, then=hazardous
    )
    with pytest.raises(RunbookQualityError) as refused:
        await runbook_gate.aenforce_runbook_security(
            hazardous, principal=USER, then=clean
        )

    assert answer == runbook_gate.SecurityAnswer(hazardous, expected)
    assert (refused.value.errors, refused.value.warnings) == (
        expected["errors"],
        expected["warnings"],
    )
    with pytest.raises(RunbookQualityError):
        answer.enforce()


def test_a_call_stops_at_its_first_refusal():
    """As the separate calls did: the quality refusal answers, and the
    security check of the same text never runs."""
    ran: list[str] = []

    def run(name, text):
        ran.append(name)
        return validation_worker.CHECKS[name](text)

    work = validation_worker.answers(
        run,
        [["enforce_runbook_quality", 0], ["enforce_runbook_security", 0]],
        ["# Not a runbook\n"],
        runbook_gate.GATE_WORK_CPU_SECONDS,
    )

    given = json.loads(work())

    assert ran == ["enforce_runbook_quality"]
    assert len(given) == 1 and given[0]["refused"]["errors"]


# ---------------------------------------------------------------------------
# Nested frontmatter is unparseable frontmatter (#2838)
# ---------------------------------------------------------------------------


def test_deeply_nested_frontmatter_is_refused_as_unparseable():
    """1.8 KB of nested flow sequences exhausts PyYAML's recursion. It gets
    the refusal any frontmatter the parser refuses gets, never an exception
    (in a worker, an exit 1 and a retryable 503 for a fixed input)."""
    nested = "---\na: " + "[" * 900 + "]" * 900 + "\n---\n# T\n"
    malformed = "---\na: [\n---\n# T\n"

    v, _ = runbook_validator.validate_and_score(nested)
    with pytest.raises(RunbookQualityError) as refused:
        runbook_validator.enforce_runbook_quality(nested)

    assert v.errors == runbook_validator.validate_and_score(malformed)[0].errors
    assert "No YAML frontmatter found" in refused.value.errors


# ---------------------------------------------------------------------------
# The callers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case_id", [None, "case-1"], ids=["document", "case"])
@pytest.mark.parametrize(
    "refusal", _REFUSALS + _UNAVAILABLE + [GateFailed], ids=lambda r: r.__name__
)
async def test_a_conversion_draft_fails_with_the_gates_answer(
    monkeypatch, refusal, case_id
):
    """Every conversion's checks run after its model calls were paid for, a
    document's modes at once, so each waits for a slot as long as a conversion
    may. Only a check that could not run is worth retrying; a check that
    raised fails the draft as any other defect does, by its class."""
    from unittest.mock import MagicMock

    from faultmaven.infrastructure.llm.providers import LLMResponse, StopReason
    from faultmaven.modules.knowledge.domain.models.conversion import (
        ConversionError,
        FailureModeAnalysis,
    )

    async def refuse(content, *, principal, slot_wait=None):
        assert (principal, slot_wait) == ("u1", expected_wait)
        raise refusal()

    expected_wait = runbook_gate.GATE_BACKGROUND_SLOT_WAIT_SECONDS
    monkeypatch.setattr(service_module, "avalidate_and_score", refuse)
    monkeypatch.setattr(service_module, "claim_case_draft_slot", AsyncMock())
    monkeypatch.setattr(service_module, "refuse_if_draft_slot_taken", AsyncMock())
    service_module.claim_case_draft_slot.return_value = "redis-notes"
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
        case_id=case_id,
    )

    error = (
        "Runbook generation failed (GateFailed)"
        if refusal is GateFailed
        else refusal.message
    )
    assert result == ConversionError(
        failure_mode_id="fm-1", error=error, retryable=refusal in _UNAVAILABLE
    )


async def test_every_gate_check_runs_under_the_callers_principal(
    monkeypatch, tmp_path, session_factory, client
):
    """Upload (the quality gate and the security half, in one call), document
    edit, template create, draft edit, draft verify (the draft's check and the
    published text's, in one call), and the scan. A fresh upload's draft is
    read without a check."""
    seen: list[tuple[list[str], str]] = []
    real_run = runbook_gate._run

    async def run(checks, principal, slot_wait=None):
        seen.append(([name for name, _ in checks], principal))
        return await real_run(checks, principal, slot_wait)

    monkeypatch.setattr(runbook_gate, "_run", run)
    from faultmaven.utils import runbook_id

    monkeypatch.setattr(runbook_id, "KNOWLEDGE_ROOT", tmp_path)
    folder = tmp_path / f"personal_{USER}"
    folder.mkdir(parents=True)
    scanned = valid_runbook("A Runbook Found On Disk")
    (folder / "scanned.md").write_text(
        scanned.replace("id: sample-runbook", "id: on-disk")
    )

    uploaded = (await _upload(client)).json()
    upload_job = await _only(session_factory, ConversionDraftModel.conversion_id)
    edited = await client.put(
        f"/api/v1/knowledge/documents/{uploaded['document_id']}",
        json={"content": valid_runbook("Redis Evictions, Edited"), "principal": "x"},
    )
    draft = await _draft_url(client)
    body = valid_runbook("Connection Pool Exhausted On The API")
    responses = [
        edited,
        await client.put(draft, json={"content": body}),
        await client.post(f"{draft}/verify"),
        await client.get(f"/api/v1/knowledge/conversions/{upload_job}"),
        await client.post("/api/v1/knowledge/scan"),
    ]

    assert [r.status_code for r in responses] == [200] * 5, [r.text for r in responses]
    assert {principal for _, principal in seen} == {USER}
    assert [checks for checks, _ in seen] == [
        ["enforce_runbook_quality", "enforce_runbook_security"],
        ["enforce_runbook_security"],
        ["validate_and_score"],
        ["validate_and_score"],
        ["enforce_runbook_security", "enforce_runbook_security"],
        ["validate_and_score"],
    ]


async def test_bulk_update_never_takes_the_principal_from_its_body():
    from unittest.mock import MagicMock

    service = KnowledgeService(
        sanitizer=regex_only_sanitizer(),
        tracer=None,
        vector_store=None,
        db_session_factory=MagicMock(),
    )
    service.update_document_metadata = AsyncMock(return_value={"document_id": "d"})

    await service.bulk_update_documents(
        ["d"], {"tags": ["a"], "principal": "someone-else"}, principal=USER
    )

    service.update_document_metadata.assert_awaited_once_with(
        "d", principal=USER, tags=["a"]
    )


@pytest.mark.parametrize("refusal", _UNAVAILABLE, ids=lambda r: r.__name__)
async def test_a_check_that_cannot_run_defers_the_rest_of_a_batch(refusal):
    service = ConversionService(llm_router=None, settings=None, db_session_factory=None)
    service.verify_draft = AsyncMock(side_effect=refusal())

    result = await service.verify_batch([("c", "d1"), ("c", "d2")], user_id=USER)

    assert (result["deferred"], result["failed"]) == (2, 0)
    assert [r["error"] for r in result["results"]] == [
        "Not verified: the runbook check was busy or could not run. Verify it again."
    ] * 2
    service.verify_draft.assert_awaited_once()


@pytest.mark.parametrize("refusal", _REFUSALS, ids=lambda r: r.reason)
async def test_a_refused_draft_fails_in_a_batch_and_the_rest_go_on(refusal):
    service = ConversionService(llm_router=None, settings=None, db_session_factory=None)
    service.verify_draft = AsyncMock(side_effect=[refusal(), None])

    result = await service.verify_batch([("c", "d1"), ("c", "d2")], user_id=USER)

    assert [r["status"] for r in result["results"]] == ["failed", "verified"]
    assert result["results"][0]["error"] == refusal.message


async def test_a_draft_whose_check_raised_fails_in_a_batch_and_the_rest_go_on():
    service = ConversionService(llm_router=None, settings=None, db_session_factory=None)
    service.verify_draft = AsyncMock(side_effect=[GateFailed(), None])

    result = await service.verify_batch([("c", "d1"), ("c", "d2")], user_id=USER)

    assert [r["status"] for r in result["results"]] == ["failed", "verified"]
    assert result["results"][0]["error"] == "Verification failed (GateFailed)"


async def test_a_check_that_cannot_run_in_ingest_is_not_logged_as_a_failure(
    caplog, session_factory, client
):
    draft = await _draft_url(client)
    client.knowledge.ingest_runbook = AsyncMock(side_effect=GateBusy())

    response = await client.post(f"{draft}/verify")

    assert response.headers["x-error-code"] == "RUNBOOK_VALIDATION_BUSY"
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert await _only(session_factory, ConversionDraftModel.status) == "draft"


@pytest.mark.parametrize("refusal", _REFUSALS + _UNAVAILABLE, ids=lambda r: r.__name__)
async def test_the_scan_lists_a_refused_file_and_stops_on_one_it_cannot_check(
    monkeypatch, tmp_path, client, refusal
):
    from faultmaven.utils import runbook_id

    monkeypatch.setattr(runbook_id, "KNOWLEDGE_ROOT", tmp_path)
    folder = tmp_path / f"personal_{USER}"
    folder.mkdir(parents=True)
    (folder / "refused.md").write_text(valid_runbook("A Runbook Found On Disk"))
    _refuse_with(monkeypatch, refusal)

    response = await client.post("/api/v1/knowledge/scan")

    if refusal in _REFUSALS:
        assert response.status_code == 200, response.text
        body = response.json()
        assert (body["discovered"], body["errors"]) == (
            0,
            [f"refused.md: {refusal.message}"],
        )
    else:
        assert response.status_code == 503, response.text
        assert response.headers["x-error-code"] == refusal.error_code


@pytest.mark.parametrize(
    "refusal", _REFUSALS + _UNAVAILABLE + [GateFailed], ids=lambda r: r.__name__
)
def test_a_refusal_survives_copy_and_pickle(refusal):
    for clone in (copy.copy(refusal()), pickle.loads(pickle.dumps(refusal()))):
        assert (type(clone), str(clone)) == (refusal, str(refusal()))
