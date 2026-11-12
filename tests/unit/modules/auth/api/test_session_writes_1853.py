"""Client metadata has one admission rule, and no session write loses another's.

Three defects with one lane (#1853, #1854, #1855):

* ``PUT`` checked client ``metadata`` with ``json.dumps`` only, which accepts
  nesting the store's serializer cannot write: a 600-deep value answered 500.
  ``POST /sessions`` checked nothing. Both now refuse 400 past
  ``MAX_CLIENT_SESSION_METADATA_DEPTH`` and write nothing.
* ``status`` lived in the client's ``metadata``, so ``PUT {"metadata": {}}``
  un-archived a session. It is a server-owned field now.
* Every writer read the record, changed a copy and wrote the whole record
  back, so a heartbeat, resume or archive landing in a PUT's window was lost,
  or the PUT was. Each now goes through the store's compare-and-set.

Driven through the real routes, ``AuthSessionService`` and ``RedisSessionStore``
over FakeRedis. The interleaving is deterministic: the seam is the Redis
client, which exists on both sides of the fix. The read a writer bases its
write on is followed, before that write, by another writer's change, made by
a second, sync client on the same FakeRedis server.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Callable, Dict

import fakeredis
import fakeredis.aioredis as fakeredis_aio
import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError

from faultmaven.api.exception_handlers import (
    get_exception_handlers,
    http_exception_handler,
    request_validation_exception_handler,
)
from faultmaven.api.v1.auth_dependencies import (
    get_current_user_optional,
    require_authentication,
)
from faultmaven.api.v1.dependencies import get_session_service
from faultmaven.exceptions import ServiceException, ValidationException
from faultmaven.modules.auth.api.session import router as session_router
from faultmaven.modules.auth.domain.models.auth import DevUser
from faultmaven.modules.auth.domain.services.auth_session_service import (
    MAX_CLIENT_SESSION_METADATA_DEPTH,
    AuthSessionService,
)
from faultmaven.modules.auth.infrastructure.stores.redis_session_store import (
    RedisSessionStore,
)
from faultmaven.utils.datetime import parse_utc_timestamp

pytestmark = [pytest.mark.unit, pytest.mark.security, pytest.mark.session]

OWNER = "user-owner-01"
SEED = {"seed": "kept"}
#: A ``last_activity`` no write of this test's own can produce.
MARKER = "2001-01-01T00:00:00Z"


def _nested(depth: int) -> Dict[str, Any]:
    """A JSON object ``depth`` levels deep, itself level 1."""
    value: Dict[str, Any] = {}
    for _ in range(depth - 1):
        value = {"a": value}
    return value


@pytest.fixture
async def world():
    server = fakeredis.FakeServer()
    redis = fakeredis_aio.FakeRedis(server=server, decode_responses=True)
    store = RedisSessionStore(redis, session_ttl_seconds=1800)
    service = AuthSessionService(session_store=store)
    own, _ = await service.create_session(OWNER, client_id="laptop", metadata=SEED)
    owner = DevUser(
        user_id=OWNER,
        username="owner",
        email="owner@example.com",
        display_name="Owner",
        created_at=datetime.now(timezone.utc),
    )

    app = FastAPI()
    app.include_router(session_router, prefix="/api/v1")
    for exc_type, handler in get_exception_handlers().items():
        app.add_exception_handler(exc_type, handler)
    app.add_exception_handler(
        RequestValidationError, request_validation_exception_handler
    )
    app.add_exception_handler(HTTPException, http_exception_handler)

    async def _service():
        return service

    async def _owner():
        return owner

    app.dependency_overrides[get_session_service] = _service
    app.dependency_overrides[require_authentication] = _owner
    app.dependency_overrides[get_current_user_optional] = _owner
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield SimpleNamespace(
            client=client,
            redis=redis,
            sync=fakeredis.FakeRedis(server=server, decode_responses=True),
            store=store,
            service=service,
            own_id=own.session_id,
            key=f"{store.prefix}{own.session_id}",
        )


async def _snapshot(world) -> Dict[str, Any]:
    """Every key and value in the store: a refusal must change none of them."""
    keys = sorted(await world.redis.keys("*"))
    return {key: await world.redis.dump(key) for key in keys}


async def _status(world) -> str:
    response = await world.client.get(f"/api/v1/sessions/{world.own_id}")
    assert response.status_code == 200, response.text
    return response.json()["status"]


# ---------------------------------------------------------------------------
# #1853: one admission rule, on update and on create
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "depth, status",
    [
        pytest.param(600, 400, id="600-deep"),
        pytest.param(MAX_CLIENT_SESSION_METADATA_DEPTH + 1, 400, id="one-too-deep"),
        pytest.param(MAX_CLIENT_SESSION_METADATA_DEPTH, 200, id="at-the-bound"),
    ],
)
async def test_put_refuses_metadata_nested_past_the_bound(world, depth, status):
    """On ``main`` the 600-deep body answered 500 from the store's serializer."""
    before = await _snapshot(world)

    response = await world.client.put(
        f"/api/v1/sessions/{world.own_id}", json={"metadata": _nested(depth)}
    )

    assert response.status_code == status, response.text[:300]
    if status == 400:
        assert response.json()["detail"] == (
            f"metadata must not nest deeper than {MAX_CLIENT_SESSION_METADATA_DEPTH} "
            "levels"
        )
        assert await _snapshot(world) == before
    else:
        assert response.json()["metadata"] == _nested(depth)


async def test_list_nesting_counts_toward_the_bound(world):
    response = await world.client.put(
        f"/api/v1/sessions/{world.own_id}",
        json={"metadata": {"b": json.loads("[" * 40 + "]" * 40)}},
    )

    assert response.status_code == 400, response.text[:300]


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(json.dumps({"metadata": _nested(600)}), id="600-deep"),
        pytest.param('{"metadata": {"x": NaN}}', id="nan"),
        pytest.param('{"metadata": {"x": "\\ud800"}}', id="lone-surrogate"),
    ],
)
async def test_post_refuses_metadata_the_update_refuses_and_creates_nothing(world, raw):
    """On ``main`` the mint checked nothing: 500 at depth 600, NaN stored."""
    before = await _snapshot(world)

    response = await world.client.post(
        "/api/v1/sessions", content=raw, headers={"content-type": "application/json"}
    )

    assert response.status_code == 400, response.text[:300]
    assert await _snapshot(world) == before


async def test_post_accepts_metadata_at_the_bound(world):
    """The positive control: the bound refuses depth, not metadata."""
    metadata = _nested(MAX_CLIENT_SESSION_METADATA_DEPTH)

    response = await world.client.post("/api/v1/sessions", json={"metadata": metadata})

    assert response.status_code == 201, response.text[:300]
    stored = await world.service.get_session(response.json()["session_id"])
    assert stored.metadata == {"session_type": "troubleshooting", **metadata}


# ---------------------------------------------------------------------------
# #1854 (a): status is the server's
# ---------------------------------------------------------------------------


async def test_an_archived_session_stays_archived_after_its_metadata_is_cleared(
    world,
):
    """On ``main`` the PUT replaced the dict ``status`` lived in: it read active."""
    archived = await world.client.post(f"/api/v1/sessions/{world.own_id}/archive")
    assert archived.status_code == 200, archived.text
    assert await _status(world) == "archived"

    cleared = await world.client.put(
        f"/api/v1/sessions/{world.own_id}", json={"metadata": {}}
    )

    assert cleared.status_code == 200, cleared.text
    assert await _status(world) == "archived"
    search = await world.client.post("/api/v1/sessions/search", json={})
    assert [s["status"] for s in search.json()["sessions"]] == ["archived"]


async def test_a_client_metadata_status_archives_nothing(world):
    planted = await world.client.put(
        f"/api/v1/sessions/{world.own_id}", json={"metadata": {"status": "archived"}}
    )
    assert planted.status_code == 200, planted.text

    assert await _status(world) == "active"
    archived = await world.client.post(
        "/api/v1/sessions/search", json={"status": "archived"}
    )
    assert archived.json()["sessions"] == []
    active = await world.client.post(
        "/api/v1/sessions/search", json={"status": "active"}
    )
    assert [s["session_id"] for s in active.json()["sessions"]] == [world.own_id]


async def test_a_minted_metadata_status_is_client_data(world):
    minted = await world.client.post(
        "/api/v1/sessions", json={"metadata": {"status": "archived"}}
    )
    assert minted.status_code == 201, minted.text
    session_id = minted.json()["session_id"]

    read = await world.client.get(f"/api/v1/sessions/{session_id}")
    assert read.json()["status"] == "active"
    archived = await world.client.post(
        "/api/v1/sessions/search", json={"status": "archived"}
    )
    assert archived.json()["sessions"] == []


# ---------------------------------------------------------------------------
# #1854 (b): concurrent writers each land their own fields
# ---------------------------------------------------------------------------


def _record_edit(world, edit: Callable[[Dict[str, Any]], None]) -> Callable[[], None]:
    """Another writer: change the stored record, keeping its TTL."""

    def write() -> None:
        record = json.loads(world.sync.get(world.key))
        edit(record)
        world.sync.set(world.key, json.dumps(record), keepttl=True)

    return write


def _interleave(monkeypatch, world, nth_read: int, conflict: Callable[[], None]):
    """Run ``conflict`` once, right after the ``nth_read`` of the session key.

    That read is the one the writer under test bases its write on: the route's
    ownership check is the read before it on ``PUT`` and ``archive``.
    """
    reads = {"n": 0}
    real_get = world.redis.get

    async def get(key, *args, **kwargs):
        value = await real_get(key, *args, **kwargs)
        if key == world.key:
            reads["n"] += 1
            if reads["n"] == nth_read:
                conflict()
        return value

    monkeypatch.setattr(world.redis, "get", get)


def _set_metadata(record):
    record["metadata"] = {"other": "writer"}


def _set_last_activity(record):
    record["last_activity"] = MARKER


def _archive(record):
    record["status"] = "archived"


#: (the call, which read of the key it writes on, the other writer's change,
#: the fields both must leave, whether the call itself moves last_activity)
INTERLEAVINGS = [
    # A heartbeat lands inside the PUT's window. On ``main`` the PUT wrote
    # back the last_activity it read.
    pytest.param(
        ("PUT", "/api/v1/sessions/{id}", {"metadata": {"put": 1}}),
        2,
        _set_last_activity,
        {"metadata": {"put": 1}, "last_activity": MARKER},
        False,
        id="heartbeat-inside-put",
    ),
    pytest.param(
        ("PUT", "/api/v1/sessions/{id}", {"metadata": {"put": 1}}),
        2,
        _archive,
        {"metadata": {"put": 1}, "status": "archived"},
        False,
        id="archive-inside-put",
    ),
    # A PUT lands inside the heartbeat's, the archive's and the resume's
    # window. On ``main`` each wrote back the metadata it read.
    pytest.param(
        ("POST", "/api/v1/sessions/{id}/heartbeat", None),
        1,
        _set_metadata,
        {"metadata": {"other": "writer"}},
        True,
        id="put-inside-heartbeat",
    ),
    pytest.param(
        ("POST", "/api/v1/sessions/{id}/archive", None),
        2,
        _set_metadata,
        {"metadata": {"other": "writer"}, "status": "archived"},
        False,
        id="put-inside-archive",
    ),
    pytest.param(
        ("POST", "/api/v1/sessions", {"client_id": "laptop"}),
        1,
        _set_metadata,
        {"metadata": {"other": "writer"}},
        True,
        id="put-inside-resume",
    ),
]


@pytest.mark.parametrize("call, nth_read, edit, expected, touches", INTERLEAVINGS)
async def test_a_write_inside_another_writers_window_is_not_lost(
    world, monkeypatch, call, nth_read, edit, expected, touches
):
    method, path, body = call
    before = json.loads(world.sync.get(world.key))["last_activity"]
    _interleave(monkeypatch, world, nth_read, _record_edit(world, edit))

    response = await world.client.request(
        method, path.format(id=world.own_id), json=body
    )

    assert response.status_code in (200, 201), response.text
    stored = json.loads(world.sync.get(world.key))
    assert {field: stored[field] for field in expected} == expected
    if touches:
        assert parse_utc_timestamp(stored["last_activity"]) > parse_utc_timestamp(
            before
        )


async def test_a_put_on_a_session_deleted_inside_its_window_is_404_and_writes_nothing(
    world, monkeypatch
):
    """B4: the compare-and-set does not write a deleted session back."""
    _interleave(monkeypatch, world, 2, lambda: world.sync.delete(world.key))

    response = await world.client.put(
        f"/api/v1/sessions/{world.own_id}", json={"metadata": {"put": 1}}
    )

    assert response.status_code == 404, response.text
    assert world.sync.get(world.key) is None


async def test_the_put_answers_with_the_record_it_wrote(world, monkeypatch):
    """No re-read: a write after the update is not reported as its result."""
    real_mutate = world.store.mutate_session

    async def mutate_then_overwrite(session_id, mutate):
        written = await real_mutate(session_id, mutate)
        _record_edit(world, _set_metadata)()
        return written

    monkeypatch.setattr(world.store, "mutate_session", mutate_then_overwrite)

    response = await world.client.put(
        f"/api/v1/sessions/{world.own_id}", json={"metadata": {"put": 1}}
    )

    assert response.status_code == 200, response.text
    assert response.json()["metadata"] == {"put": 1}


# ---------------------------------------------------------------------------
# The store's compare-and-set
# ---------------------------------------------------------------------------


def _set_mutated(session):
    session.metadata = {"mutated": True}
    return session


async def test_mutate_retries_after_a_conflicting_write_and_keeps_it(
    world, monkeypatch
):
    _interleave(monkeypatch, world, 1, _record_edit(world, _set_last_activity))

    written = await world.store.mutate_session(world.own_id, _set_mutated)

    assert written.metadata == {"mutated": True}
    stored = json.loads(world.sync.get(world.key))
    assert (stored["metadata"], stored["last_activity"]) == ({"mutated": True}, MARKER)
    assert 1700 < world.sync.ttl(world.key) <= 1800


async def test_mutate_gives_up_after_five_conflicts_and_writes_nothing(
    world, monkeypatch
):
    conflicts = {"n": 0}
    real_get = world.redis.get

    def stamp(record):
        moment = datetime(2001, 1, 1, tzinfo=timezone.utc)
        record["last_activity"] = (
            moment + timedelta(seconds=conflicts["n"])
        ).isoformat()

    async def always_conflicting(key, *args, **kwargs):
        value = await real_get(key, *args, **kwargs)
        conflicts["n"] += 1
        _record_edit(world, stamp)()
        return value

    monkeypatch.setattr(world.redis, "get", always_conflicting)

    with pytest.raises(ServiceException):
        await world.store.mutate_session(world.own_id, _set_mutated)

    assert conflicts["n"] == 5
    assert json.loads(world.sync.get(world.key))["metadata"] == SEED


async def test_mutate_returning_none_writes_nothing(world):
    before = await _snapshot(world)

    assert await world.store.mutate_session(world.own_id, lambda session: None) is None

    assert await _snapshot(world) == before


async def test_mutate_on_a_missing_session_returns_none_and_creates_nothing(world):
    before = await _snapshot(world)

    assert await world.store.mutate_session("no-such-session", _set_mutated) is None

    assert await _snapshot(world) == before


# ---------------------------------------------------------------------------
# Rules the deleted stand-in's tests pinned that no real-service test did
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("blank", ["", "   ", None])
async def test_a_blank_user_id_is_refused(world, blank):
    with pytest.raises(ValidationException):
        await world.service.create_session(blank)


async def test_a_heartbeat_on_a_session_past_its_expiry_does_not_revive_it(world):
    """The key outlives ``expires_at`` only by the moment between the two; a
    heartbeat in it must not re-arm the session."""
    lapsed = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    _record_edit(world, lambda r: r.update(expires_at=lapsed))()

    assert await world.service.update_last_activity(world.own_id) is False
    stored = world.sync.get(world.key)
    assert stored is None or json.loads(stored)["expires_at"] == lapsed


def test_the_minimal_container_has_no_session_service():
    """#1855: no hand-written second implementation stands in for the real one."""
    from faultmaven.container import DIContainer

    container = object.__new__(DIContainer)
    container._create_minimal_container()

    assert container.session_service is None
