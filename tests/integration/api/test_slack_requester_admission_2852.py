"""A Slack service account's jobs are ordered by the person who asked (#2852).

Through ``POST /api/v1/cases/{case_id}/turns`` and ``PATCH …/classification``
behind the real tenant binder and the unmodified ``require_authentication``:
accounts provisioned in a real user store, tokens verified by a real
``AuthService``. The investigation service and the reclassifier are doubles
that run the real Tier 1 on the file, so the key admission uses is the one
``FairSlots`` records; its worker is a gated stand-in.

``AsyncClient`` over ``ASGITransport``, not ``TestClient``: the gated pin-1 test
needs two concurrent requests on the test's own loop, which ``TestClient``
cannot drive.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from faultmaven.api.middleware.tenant_scope import bind_request_enterprise_context
from faultmaven.api.v1 import auth_dependencies
from faultmaven.api.v1.dependencies import (
    get_evidence_reclassifier,
    get_investigation_service,
)
from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.config.settings import reset_settings
from faultmaven.config.tenant_context import get_current_enterprise_id
from faultmaven.exceptions import NotFoundError
from faultmaven.infrastructure import worker_process
from faultmaven.infrastructure.auth.database_user_store import DatabaseUserStore
from faultmaven.infrastructure.persistence.user_repository import (
    InMemoryUserRepository,
)
from faultmaven.infrastructure.worker_process import FairSlots, WorkerEnd
from faultmaven.models.api import DataType
from faultmaven.modules.auth.domain.services.service_account_provisioning import (
    provision_service_account_credential,
)
from faultmaven.modules.auth.domain.services.user_service import UserService
from faultmaven.modules.case.api.routes.dependencies import (
    _di_get_case_service_dependency,
)
from faultmaven.modules.case.api.routes.router import router
from faultmaven.modules.case.contracts import Case, CaseState
from faultmaven.modules.case.exceptions import StaleCaseException
from faultmaven.modules.preprocessing import preprocessing_service
from faultmaven.providers.tenancy import factory as tenancy_factory
from tests.unit.infrastructure.test_fair_slots_2645 import _LOGS, _tier1_service
from tests.utils import forge_access_token, uncomposed_auth_service

pytestmark = pytest.mark.integration

READABLE, UNREADABLE = "case_2852aaaaaaaa", "case_2852bbbbbbbb"
ID1, ID2 = "a" * 32, "b" * 32
# The file's one time limit, bounding a hang, not performance: each wait ends on
# the event it waits for, and a request's wait for a Tier 1 slot is set to it.
HANG_GUARD_SECONDS = 60


async def _provision(store, username, kind, channel="slack"):
    if channel is None:  # no provisioning path mints a channel-less service account
        return await store.create_user(
            username,
            account_kind=kind,
            service_channel=None,
            enterprise_id=STANDALONE_ENTERPRISE_ID,
        )
    stub = SimpleNamespace(
        generate_refresh_token=lambda *_a, **_k: asyncio.sleep(0, "r")
    )
    await provision_service_account_credential(
        username=username, user_store=store, token_generator=stub, account_kind=kind
    )
    return await store.get_user_by_username(username)


@pytest.fixture
async def world(monkeypatch):
    monkeypatch.setattr(auth_dependencies, "_warned_malformed", set())
    monkeypatch.setenv("FAULTMAVEN_RECLASSIFY_ENABLED", "true")
    reset_settings()
    repo = InMemoryUserRepository()
    store = DatabaseUserStore(repo)
    auth = uncomposed_auth_service()
    accounts = {
        "slack": await _provision(store, "slack-agent", "service"),
        "individual": await _provision(store, "alice", "individual"),
        "other_service": await _provision(store, "ops-bot", "service", None),
    }

    def token(who, enterprise_id=STANDALONE_ENTERPRISE_ID):
        user = accounts[who]
        return forge_access_token(
            auth,
            user_id=user.user_id,
            enterprise_id=enterprise_id,
            email=user.email,
            roles=["user"],
        )

    seen = SimpleNamespace(
        keys=[], case_users=[], turn_users=[], gate=None, running=0, now=0, most=0
    )
    seen.changed = asyncio.Event()  # set each time ``keys`` or ``running`` changes
    acquire = FairSlots.acquire

    async def acquiring(self, account, requester=None):
        seen.keys.append((account, requester))
        seen.changed.set()
        await acquire(self, account, requester)

    async def run(*_args, **_kwargs):
        seen.running += 1
        seen.changed.set()
        seen.now += 1
        seen.most = max(seen.most, seen.now)
        try:
            if seen.gate is not None:
                await seen.gate.wait()
            return WorkerEnd(code=1)
        finally:
            seen.now -= 1

    monkeypatch.setattr(FairSlots, "acquire", acquiring)
    monkeypatch.setattr(worker_process, "run", run)
    # A slot waiter falls back to the busy preview past its wait (#2942).
    monkeypatch.setattr(
        preprocessing_service, "TIER1_SLOT_WAIT_SECONDS", HANG_GUARD_SECONDS
    )

    def tier1_slots(count):
        pool = FairSlots(count)
        monkeypatch.setattr(
            preprocessing_service,
            "_tier1_slots_state",
            (asyncio.get_running_loop(), pool),
        )
        return pool

    tier1_slots(2)
    tier1 = _tier1_service()

    async def require_case(case_id, user_id, **_flags):
        seen.case_users.append(user_id)
        if case_id != READABLE:
            raise NotFoundError(message="Case not found")
        return Case(
            case_id=READABLE,
            title="t",
            state=CaseState.INQUIRY,
            user_id=user_id,
            enterprise_id=STANDALONE_ENTERPRISE_ID,
        )

    async def prepare_turn(*, case_id, user_id, payload):
        seen.turn_users.append(user_id)
        for attachment in payload.attachments:
            await tier1.classify_and_extract(
                content=attachment.content.decode(),
                filename=attachment.filename,
                principal=user_id,
            )

    async def commit_turn(prepared, receipt_key=None):
        raise StaleCaseException(READABLE, 1, 2)

    async def reclassify_out_of_band(*, case_id, evidence_id, user_id, **_kwargs):
        seen.turn_users.append(user_id)
        await tier1.reclassify_evidence(
            content=_LOGS,
            filename="x.log",
            user_override=DataType.LOGS_AND_ERRORS,
            principal=user_id,
        )
        return SimpleNamespace(
            evidence_id=evidence_id, source_type="log", summary="", metadata={}
        )

    app = FastAPI(dependencies=[Depends(bind_request_enterprise_context)])
    app.include_router(router, prefix="/api/v1")
    app.state.auth_service = auth
    app.state.user_service = UserService(repo, auth_service=auth)
    app.state.llm_provider = None
    investigation = SimpleNamespace(prepare_turn=prepare_turn, commit_turn=commit_turn)
    reclassifier = SimpleNamespace(reclassify_out_of_band=reclassify_out_of_band)
    app.dependency_overrides[get_investigation_service] = lambda: investigation
    app.dependency_overrides[get_evidence_reclassifier] = lambda: reclassifier
    app.dependency_overrides[_di_get_case_service_dependency] = lambda: (
        SimpleNamespace(require_case=require_case)
    )
    started = []

    def spawn(coro):
        started.append(asyncio.create_task(coro))
        return started[-1]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:

        def headers(who, requester, enterprise_id):
            sent = {"Authorization": f"Bearer {token(who, enterprise_id)}"}
            if requester is not None:
                sent["X-FaultMaven-Requester"] = requester
            return sent

        async def post(
            who="slack",
            requester=None,
            case_id=READABLE,
            enterprise_id=STANDALONE_ENTERPRISE_ID,
        ):
            return await c.post(
                f"/api/v1/cases/{case_id}/turns",
                headers=headers(who, requester, enterprise_id),
                files={"files": ("x.log", _LOGS.encode(), "text/plain")},
            )

        async def patch(who="slack", requester=None):
            return await c.patch(
                f"/api/v1/cases/{READABLE}/evidence/ev-1/classification",
                headers=headers(who, requester, STANDALONE_ENTERPRISE_ID),
                json={"data_type": DataType.LOGS_AND_ERRORS.value},
            )

        yield SimpleNamespace(
            post=post,
            patch=patch,
            spawn=spawn,
            seen=seen,
            app=app,
            tier1_slots=tier1_slots,
            ids={k: u.user_id for k, u in accounts.items()},
        )
        # No request outlives the stand-in: ended before it is undone (#2942).
        for task in started:
            if not task.done():
                task.cancel()
        await asyncio.gather(*started, return_exceptions=True)
    monkeypatch.undo()  # the flag's environment, before the settings rebuild
    reset_settings()


async def _until(seen, what, condition, *requests):
    """Wait until ``condition`` holds, woken by each change ``seen`` notes or a
    request's end; a request that ends first fails the test with its answer."""
    try:
        async with asyncio.timeout(HANG_GUARD_SECONDS):
            while not condition():
                ended = [r.result() for r in requests if r.done()]
                assert not ended, [(a.status_code, a.text) for a in ended]
                seen.changed.clear()
                changed = asyncio.create_task(seen.changed.wait())
                try:
                    await asyncio.wait(
                        [changed, *requests], return_when=asyncio.FIRST_COMPLETED
                    )
                finally:
                    changed.cancel()
    except TimeoutError:
        pytest.fail(f"{what}: not reached in {HANG_GUARD_SECONDS}s")


@pytest.mark.parametrize("count", [2, 3])
async def test_a_second_person_waits_one_job_on_two_slots_and_runs_beside_on_three(
    world, count
):
    """Ruling 2026-11-19: the account is one principal per pool. On a two-slot
    pool the second person is served when the first's job ends, never beside
    it; with three slots they run side by side."""
    pool, seen = world.tier1_slots(count), world.seen
    seen.gate = asyncio.Event()
    first = world.spawn(world.post(requester=ID1))
    await _until(seen, "the first job running", lambda: seen.running == 1, first)
    later = world.spawn(world.post(requester=ID2))
    await _until(seen, "both at the slots", lambda: len(seen.keys) == 2, first, later)

    account = world.ids["slack"]
    if count == 2:  # ``acquire`` decides a grant before it awaits: this one waits
        (waiting,) = [w for w in pool._waiting if w[1] == (account, ID2)]
        assert not waiting[2].done()
    else:
        await _until(
            seen, "the second job running", lambda: seen.running == 2, first, later
        )
    assert world.seen.keys == [(account, ID1), (account, ID2)]
    assert world.seen.running == (1 if count == 2 else 2)
    world.seen.gate.set()
    assert {(await first).status_code, (await later).status_code} == {409}
    assert (world.seen.running, world.seen.most) == (2, count - 1)


async def test_the_requester_grants_nothing(world, caplog):
    caplog.set_level(logging.DEBUG)
    account = world.ids["slack"]
    answers = [
        await world.post(requester=r, case_id=c)
        for c in (READABLE, UNREADABLE)
        for r in (ID1, None)
    ]
    # Positive controls: the slot saw the requester, everything else the account.
    assert world.seen.keys == [(account, ID1), (account, None)]
    assert set(world.seen.case_users) == set(world.seen.turn_users) == {account}
    assert [a.status_code for a in answers] == [409, 409, 404, 404]
    assert answers[0].json() == answers[1].json()
    assert answers[2].json() == answers[3].json()
    assert not any(ID1 in a.text for a in answers)
    assert not any(ID1 in r.getMessage() for r in caplog.records)


async def test_the_reclassification_route_takes_the_header(world):
    account = world.ids["slack"]
    answers = [await world.patch(requester=r) for r in (ID1, None)]
    assert [a.status_code for a in answers] == [200, 200]
    assert answers[0].json() == answers[1].json()
    assert world.seen.keys == [(account, ID1), (account, None)]
    assert set(world.seen.case_users) == set(world.seen.turn_users) == {account}


async def test_the_lookup_runs_under_the_tenant_the_binder_bound(world, monkeypatch):
    """Multi-tenant: the binder binds the token's enterprise before the
    dependency's user-store read, which runs under it."""
    enterprise = "0b2a3c4d-2852-4e5f-8a6b-7c8d9e0f1a2b"
    monkeypatch.setattr(
        tenancy_factory,
        "requested_tenant_provider",
        lambda: tenancy_factory.BUILTIN_MULTI,
    )
    service, read_under = world.app.state.user_service, []
    get_user = service.get_user

    async def recording(user_id):
        read_under.append(get_current_enterprise_id())
        return await get_user(user_id)

    monkeypatch.setattr(service, "get_user", recording)
    answer = await world.post(requester=ID1, enterprise_id=enterprise)
    assert answer.status_code == 409
    assert read_under == [enterprise]
    assert world.seen.keys == [(world.ids["slack"], ID1)]


def _records(caplog, level):
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == auth_dependencies.__name__ and r.levelno == level
    ]


def _warnings(caplog):
    return _records(caplog, logging.WARNING)


@pytest.mark.parametrize(
    "who, requester, warned",
    [
        ("slack", None, None),
        ("individual", ID1, None),
        ("other_service", ID1, None),
        ("slack", "a" * 15, "malformed"),
        ("slack", "a" * 65, "malformed"),
        ("slack", "a" * 20 + ":" + "b" * 20, "malformed"),
        ("slack", "a" * 20 + " " + "b" * 20, "malformed"),
    ],
)
async def test_a_refused_requester_runs_as_the_caller(
    world, caplog, who, requester, warned
):
    caplog.set_level(logging.DEBUG)
    answer = await world.post(who=who, requester=requester)
    assert answer.status_code == 409
    assert world.seen.keys == [(world.ids[who], None)]
    warnings = _warnings(caplog)
    assert len(warnings) == (1 if warned else 0)
    assert all(warned in w and (requester not in w) for w in warnings)


async def test_a_malformed_id_warns_once_per_account_then_logs_at_debug(world, caplog):
    caplog.set_level(logging.DEBUG)
    for _ in range(3):
        assert (await world.post(requester="a" * 15)).status_code == 409
    malformed = [m for m in _records(caplog, logging.DEBUG) if "malformed" in m]
    assert len(_warnings(caplog)) == 1 and len(malformed) == 2


async def test_a_failed_lookup_runs_as_the_caller(world, caplog, monkeypatch):
    async def broken(_user_id):
        raise RuntimeError("store down at db.internal:5432")

    monkeypatch.setattr(world.app.state.user_service, "get_user", broken)
    answer = await world.post(requester=ID1)
    assert answer.status_code == 409
    assert world.seen.keys == [(world.ids["slack"], None)]
    (warning,) = _warnings(caplog)
    assert "RuntimeError" in warning and "store down" not in warning
    assert ID1 not in warning


async def test_no_user_service_warns_once(world, caplog, monkeypatch):
    monkeypatch.setattr(auth_dependencies, "_warned_no_user_service", False)
    world.app.state.user_service = None
    for _ in range(2):
        assert (await world.post(requester=ID1)).status_code == 409
    assert world.seen.keys == [(world.ids["slack"], None)] * 2
    assert _warnings(caplog) == ["Requester ids ignored: no user service"]


async def test_a_binding_does_not_outlive_its_request(world):
    """One task serves both requests here, as the suite's client does."""
    await world.post(requester=ID1)
    await world.post()
    account = world.ids["slack"]
    assert world.seen.keys == [(account, ID1), (account, None)]
