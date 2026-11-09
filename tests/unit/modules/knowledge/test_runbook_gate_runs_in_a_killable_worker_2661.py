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
import json
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

#: The authenticated caller: every gate check must run under this principal.
USER = "user-7"

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
            user_id=USER,
            enterprise_id=STANDALONE_ENTERPRISE_ID,
            is_platform_admin=lambda: False,
        )

    app.dependency_overrides[routes.get_knowledge_service] = lambda: knowledge
    app.dependency_overrides[require_authentication] = user
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    client = httpx.AsyncClient(transport=transport, base_url="http://kb")
    client.knowledge = knowledge
    return client


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


@pytest.mark.parametrize("case_id,slot_wait", [(None, None), ("case-1", 120)])
@pytest.mark.parametrize(
    "refusal,retryable",
    [(runbook_gate.GateTimedOut, False), (runbook_gate.GateBusy, True)],
)
async def test_a_conversion_draft_fails_with_the_gates_answer(
    monkeypatch, refusal, retryable, case_id, slot_wait
):
    """A case's conversion, which no request waits on, waits for a slot within
    the conversion's own 120 s; a document's, the interactive 10 s."""
    from unittest.mock import AsyncMock, MagicMock

    from faultmaven.infrastructure.llm.providers import LLMResponse, StopReason
    from faultmaven.modules.knowledge.domain.models.conversion import (
        ConversionError,
        FailureModeAnalysis,
    )
    from faultmaven.modules.knowledge.domain.services.conversion_service import (
        service as service_module,
    )

    async def refuse(content, *, principal, slot_wait=None):
        assert (principal, slot_wait) == ("u1", slot_wait_expected)
        raise refusal()

    slot_wait_expected = slot_wait

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
        case_id=case_id,
    )

    assert result == ConversionError(
        failure_mode_id="fm-1", error=str(refusal()), retryable=retryable
    )


# ---------------------------------------------------------------------------
# The principal, by value, at every gate call site
# ---------------------------------------------------------------------------


async def test_every_gate_check_runs_under_the_callers_principal(
    monkeypatch, tmp_path, session_factory, client
):
    """Upload (the quality gate, then ingest's security half), document edit,
    template create, draft edit, draft verify (its own check, then ingest's),
    the conversion read that scores an upload's draft, and the scan."""
    seen: list[tuple[str, str]] = []
    real_run = runbook_gate._run

    async def run(check, content, principal, slot_wait=None):
        seen.append((check, principal))
        return await real_run(check, content, principal, slot_wait)

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
    async with session_factory() as session:
        (upload_job,) = (
            await session.execute(select(ConversionDraftModel.conversion_id))
        ).scalars()
    edited = await client.put(
        f"/api/v1/knowledge/documents/{uploaded['document_id']}",
        json={"content": valid_runbook("Redis Evictions, Edited"), "principal": "x"},
    )
    created = (await _create(client)).json()
    draft = f"/api/v1/knowledge/conversions/{created['conversion_id']}/drafts/"
    draft += created["draft"]["draft_id"]
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
    assert [check for check, _ in seen] == [
        "enforce_runbook_quality",
        "enforce_runbook_security",
        "enforce_runbook_security",
        "validate_and_score",
        "validate_and_score",
        "enforce_runbook_security",
        "enforce_runbook_security",
        "validate_and_score",
        "validate_and_score",
    ]


async def test_bulk_update_never_takes_the_principal_from_its_body(monkeypatch):
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


# ---------------------------------------------------------------------------
# The bounds the gate's limits are measured at
# ---------------------------------------------------------------------------


def _causes(statement: str, size: int = 100_000) -> str:
    """A runbook filled to ``size`` characters with causes."""
    head = valid_runbook().partition("## Causes")[0] + "## Causes\n"
    body, i = head, 0
    while len(body) < size - 60:
        body += f"### Cause {chr(65 + i % 25)}: c{i}\n**Statement:** "
        body += statement.format(i=i) + "\n"
        i += 1
    return body[: size - 30] + "\n## Prevention\nAlert.\n"


def test_near_duplicate_statements_are_named_up_to_a_cap():
    """One warning per pair grew as the square of the causes: 300 near-
    duplicates answered 6.6 MB. Twenty pairs are named, one line counts the
    rest."""
    statements = [
        (chr(65 + i % 25), f"the connection pool is exhausted by leak {i}")
        for i in range(30)
    ]

    _, warnings = runbook_validator.check_cause_statement_invariants(statements)

    assert len(warnings) == 21
    assert warnings[-1] == (
        "And 415 more pairs of causes have near-duplicate Statements."
    )


@pytest.mark.parametrize(
    "statement",
    [
        pytest.param("the connection pool is exhausted by leak number {i}", id="near"),
        pytest.param("a", id="one-word"),
        pytest.param("w{i} x{i} y{i} z{i}", id="distinct"),
    ],
)
async def test_the_worst_shapes_at_the_bound_are_answered(monkeypatch, statement):
    """The shapes the limits were measured on, at 100,000 characters: each is
    answered by a real worker under the declared limits, not refused, and its
    answer is under a quarter of the output limit."""
    monkeypatch.setattr(runbook_gate, "_gate_slots_state", None)
    content = _causes(statement)
    answer = validation_worker.CHECKS["validate_and_score"](content)

    async with asyncio.timeout(_HARNESS_SECONDS):
        v, q = await runbook_gate.avalidate_and_score(content, principal=USER)

    assert (v.model_dump(), q.model_dump()) == (answer["validation"], answer["quality"])
    assert 4 * len(json.dumps(answer).encode()) < runbook_gate.GATE_OUTPUT_LIMIT_BYTES


# ---------------------------------------------------------------------------
# The callers of a busy or failing gate
# ---------------------------------------------------------------------------


async def test_a_conversion_read_holds_no_session_through_the_gate(
    monkeypatch, session_factory, client
):
    from contextlib import asynccontextmanager

    from faultmaven.modules.knowledge.domain.services.conversion_service import (
        service as service_module,
    )

    assert (await _upload(client)).status_code == 201
    async with session_factory() as session:
        (conversion_id,) = (
            await session.execute(select(ConversionDraftModel.conversion_id))
        ).scalars()
    open_sessions, at_the_gate = [0], []

    @asynccontextmanager
    async def counted():
        open_sessions[0] += 1
        try:
            async with session_factory() as session:
                yield session
        finally:
            open_sessions[0] -= 1

    async def gate(content, *, principal, slot_wait=None):
        at_the_gate.append(open_sessions[0])
        return runbook_validator.validate_and_score(content)

    monkeypatch.setattr(service_module, "avalidate_and_score", gate)
    service = ConversionService(
        llm_router=None, settings=None, db_session_factory=counted
    )

    assert await service.get_conversion(conversion_id, USER) is not None
    assert at_the_gate == [0]


async def test_a_busy_gate_defers_the_rest_of_a_batch():
    service = ConversionService(llm_router=None, settings=None, db_session_factory=None)
    service.verify_draft = AsyncMock(side_effect=runbook_gate.GateBusy())

    result = await service.verify_batch([("c", "d1"), ("c", "d2")], user_id=USER)

    assert (result["deferred"], result["failed"]) == (2, 0)
    service.verify_draft.assert_awaited_once()


async def test_a_busy_gate_in_ingest_is_not_logged_as_an_ingestion_failure(
    caplog, session_factory, client
):
    created = (await _create(client)).json()
    draft = f"/api/v1/knowledge/conversions/{created['conversion_id']}/drafts/"
    draft += created["draft"]["draft_id"]
    client.knowledge.ingest_runbook = AsyncMock(side_effect=runbook_gate.GateBusy())

    response = await client.post(f"{draft}/verify")

    assert response.headers["x-error-code"] == "RUNBOOK_VALIDATION_BUSY"
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert await _written(session_factory) == 1  # the draft, unverified


@pytest.mark.parametrize("refusal", [runbook_gate.GateTimedOut, runbook_gate.GateBusy])
def test_a_refusal_survives_copy_and_pickle(refusal):
    import copy
    import pickle

    for clone in (copy.copy(refusal()), pickle.loads(pickle.dumps(refusal()))):
        assert (type(clone), str(clone)) == (refusal, str(refusal()))
