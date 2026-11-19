"""A Slack service account's turn is admitted under the person who asked (#2852).

Through ``POST /api/v1/cases/{case_id}/turns`` with the unmodified
``require_authentication``: accounts provisioned in a real user store, tokens
verified by a real ``AuthService``. The investigation service is a double whose
preparation runs the real Tier 1 on the turn's attachment, so the key admission
uses is the one ``FairSlots`` records; its worker is a gated stand-in.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from faultmaven.api.v1 import auth_dependencies
from faultmaven.api.v1.dependencies import get_investigation_service
from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.exceptions import NotFoundError
from faultmaven.infrastructure import worker_process
from faultmaven.infrastructure.auth.database_user_store import DatabaseUserStore
from faultmaven.infrastructure.persistence.user_repository import (
    InMemoryUserRepository,
)
from faultmaven.infrastructure.worker_process import FairSlots, WorkerEnd
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
from tests.unit.infrastructure.test_fair_slots_2645 import _LOGS, _tier1_service
from tests.utils import forge_access_token, uncomposed_auth_service

pytestmark = pytest.mark.integration

READABLE, UNREADABLE = "case_2852aaaaaaaa", "case_2852bbbbbbbb"
ID1, ID2 = "a" * 32, "b" * 32


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
    repo = InMemoryUserRepository()
    store = DatabaseUserStore(repo)
    auth = uncomposed_auth_service()
    accounts = {
        "slack": await _provision(store, "slack-agent", "service"),
        "individual": await _provision(store, "alice", "individual"),
        "other_service": await _provision(store, "ops-bot", "service", None),
    }
    tokens = {
        name: forge_access_token(
            auth,
            user_id=user.user_id,
            enterprise_id=STANDALONE_ENTERPRISE_ID,
            email=user.email,
            roles=["user"],
        )
        for name, user in accounts.items()
    }
    seen = SimpleNamespace(keys=[], case_users=[], turn_users=[], gate=None)
    acquire = FairSlots.acquire

    async def acquiring(self, principal):
        seen.keys.append(principal)
        await acquire(self, principal)

    async def run(*_args, **_kwargs):
        seen.running = getattr(seen, "running", 0) + 1
        if seen.gate is not None:
            await seen.gate.wait()
        return WorkerEnd(code=1)

    monkeypatch.setattr(FairSlots, "acquire", acquiring)
    monkeypatch.setattr(worker_process, "run", run)
    monkeypatch.setattr(
        preprocessing_service,
        "_tier1_slots_state",
        (asyncio.get_running_loop(), FairSlots(2)),
    )
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

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.state.auth_service = auth
    app.state.user_service = UserService(repo, auth_service=auth)
    app.state.llm_provider = None
    investigation = SimpleNamespace(prepare_turn=prepare_turn, commit_turn=commit_turn)
    app.dependency_overrides[get_investigation_service] = lambda: investigation
    app.dependency_overrides[_di_get_case_service_dependency] = lambda: (
        SimpleNamespace(require_case=require_case)
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:

        async def post(who="slack", requester=None, case_id=READABLE):
            headers = {"Authorization": f"Bearer {tokens[who]}"}
            if requester is not None:
                headers["X-FaultMaven-Requester"] = requester
            return await c.post(
                f"/api/v1/cases/{case_id}/turns",
                headers=headers,
                files={"files": ("x.log", _LOGS.encode(), "text/plain")},
            )

        yield SimpleNamespace(
            post=post,
            seen=seen,
            app=app,
            ids={k: u.user_id for k, u in accounts.items()},
        )


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


async def _until(condition, *requests):
    """Yield to the loop until ``condition`` holds; a request that ends first
    fails the test with its answer."""
    while not condition():
        ended = [r.result() for r in requests if r.done()]
        assert not ended, [(a.status_code, a.text) for a in ended]
        await asyncio.sleep(0)


@pytest.mark.parametrize("second", [ID2, ID1], ids=["other_person", "same_person"])
async def test_two_people_of_one_workspace_hold_separate_slots(world, second):
    world.seen.gate = asyncio.Event()
    first = asyncio.create_task(world.post(requester=ID1))
    await _until(lambda: getattr(world.seen, "running", 0) == 1, first)
    later = asyncio.create_task(world.post(requester=second))
    await _until(lambda: len(world.seen.keys) == 2, first, later)
    await _settle()

    account = world.ids["slack"]
    assert world.seen.keys == [f"{account}:{ID1}", f"{account}:{second}"]
    assert world.seen.running == (2 if second == ID2 else 1)
    world.seen.gate.set()
    assert {(await first).status_code, (await later).status_code} == {409}


async def test_the_requester_grants_nothing(world, caplog):
    caplog.set_level(logging.DEBUG)
    account = world.ids["slack"]
    answers = [
        await world.post(requester=r, case_id=c)
        for c in (READABLE, UNREADABLE)
        for r in (ID1, None)
    ]
    # Positive controls: the slot saw the compound key, everything else the account.
    assert world.seen.keys == [f"{account}:{ID1}", account]
    assert set(world.seen.case_users) == set(world.seen.turn_users) == {account}
    assert [a.status_code for a in answers] == [409, 409, 404, 404]
    assert answers[0].json() == answers[1].json()
    assert answers[2].json() == answers[3].json()
    assert not any(ID1 in a.text for a in answers)
    assert not any(ID1 in r.getMessage() for r in caplog.records)


def _warnings(caplog):
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == auth_dependencies.__name__ and r.levelno >= logging.WARNING
    ]


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
    assert world.seen.keys == [world.ids[who]]
    warnings = _warnings(caplog)
    assert len(warnings) == (1 if warned else 0)
    assert all(warned in w and (requester not in w) for w in warnings)


async def test_a_failed_lookup_runs_as_the_caller(world, caplog, monkeypatch):
    async def broken(_user_id):
        raise RuntimeError("store down")

    monkeypatch.setattr(world.app.state.user_service, "get_user", broken)
    answer = await world.post(requester=ID1)
    assert answer.status_code == 409
    assert world.seen.keys == [world.ids["slack"]]
    assert len(_warnings(caplog)) == 1 and ID1 not in _warnings(caplog)[0]


async def test_no_user_service_warns_once(world, caplog, monkeypatch):
    monkeypatch.setattr(auth_dependencies, "_warned_no_user_service", False)
    world.app.state.user_service = None
    for _ in range(2):
        assert (await world.post(requester=ID1)).status_code == 409
    assert world.seen.keys == [world.ids["slack"]] * 2
    assert _warnings(caplog) == ["Requester ids ignored: no user service"]


async def test_a_binding_does_not_outlive_its_request(world):
    """One task serves both requests here, as the suite's client does."""
    await world.post(requester=ID1)
    await world.post()
    account = world.ids["slack"]
    assert world.seen.keys == [f"{account}:{ID1}", account]
