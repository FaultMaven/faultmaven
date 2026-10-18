"""ChromaDB HTTP requests are bounded, and they leave the event loop (#2056).

On Cloud every turn-path ChromaDB call was a synchronous HTTP round trip on the
event-loop thread, through a session chromadb builds with ``timeout=None``. A
stalled server therefore held the whole API loop, ``/health`` included, for as
long as it stalled, and ``call_external``'s ``asyncio.wait_for`` could not
interrupt it. These tests pin the fix against a real ``chromadb.HttpClient``:

- T1/T2: every client is built by ``build_http_client``, which applies the
  shared credential, TLS from the URL's scheme, and a per-phase request
  timeout that actually ends a held request;
- T3: a store over such a client keeps the loop running while the server holds
  a request, and the same store with its executor removed does not (the
  control that proves the test bites);
- T5 and R4: ``is_http_client`` decides which arm a store is on, a store
  refuses an HTTP client the door did not build, and a "persistent" client
  cannot be turned into an HTTP one by the environment;
- the configuration refusals (``ChromaClientConfigError``) are never answered
  with a fallback or a degraded mode, at either acquisition site, in the
  ingester's container wrapper, or in ``DIContainer.initialize``.

No test here reads a clock (#1579). Each judges an ORDER of events instead:
whether the loop ran while the stub held a request, and whether the client
gave up before the stub answered.

The server is an in-process stub bound to port 0 by the test itself: the
suite's network guard allows only ports the test process binds. It answers the
three requests ``HttpClient()`` makes at construction and HOLDS every other
request until the test releases it, or until its own hold expires.
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional, Tuple
from unittest.mock import MagicMock, patch

import chromadb
import httpx
import pytest
from chromadb.config import Settings as ChromaSettings

from faultmaven.container import DIContainer
from faultmaven.container.providers.infrastructure import _create_chromadb_client
from faultmaven.container.providers.tools import create_knowledge_ingester
from faultmaven.infrastructure import chroma_client
from faultmaven.infrastructure.chroma_client import (
    CHROMA_EXECUTOR,
    CHROMA_HTTP_TIMEOUT_SECONDS,
    ChromaClientConfigError,
    ChromaUnavailableError,
    blocking_executor_for,
    build_http_client,
    is_http_client,
)
from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
    KnowledgeVectorStore,
)
from faultmaven.infrastructure.persistence.case_vector_store import CaseVectorStore
from faultmaven.infrastructure.persistence.chromadb_store import (
    ChromaDBVectorStore,
    create_persistent_client,
)
from faultmaven.modules.knowledge.domain.services.ingestion import KnowledgeIngester
from tests.unit.container.conftest import make_chroma_settings

pytestmark = [pytest.mark.unit]

#: How long the stub holds a request nobody releases. Longer than the short
#: client timeout the timeout tests set, so a timed-out request is one the
#: stub had not answered yet.
_HOLD_SECONDS = 2.0


# --------------------------------------------------------------------------- #
# The stub server
# --------------------------------------------------------------------------- #


class _StubChroma:
    """A ChromaDB server stand-in that answers construction and holds the rest."""

    def __init__(self, hold: float = _HOLD_SECONDS) -> None:
        self.requests: List[Tuple[str, str, Optional[str]]] = []
        self.hold = hold
        #: A held request reached the stub.
        self.arrived = threading.Event()
        #: The test lets held requests be answered.
        self.release = threading.Event()
        #: A held request was answered, set BEFORE the response is written.
        self.answered = threading.Event()
        stub = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _send(self, payload: Dict[str, Any]) -> None:
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _record(self, method: str) -> None:
                stub.requests.append(
                    (method, self.path, self.headers.get("Authorization"))
                )

            def _held(self, payload: Dict[str, Any]) -> None:
                stub.arrived.set()
                stub.release.wait(stub.hold)
                stub.answered.set()
                self._send(payload)

            def do_GET(self) -> None:
                self._record("GET")
                path = self.path
                if path.endswith("/heartbeat"):
                    return self._held({"nanosecond heartbeat": 1})
                if "/collections" in path:
                    return self._held({})
                if path.endswith("/auth/identity"):
                    return self._send(
                        {
                            "user_id": "",
                            "tenant": "default_tenant",
                            "databases": ["default_database"],
                        }
                    )
                if "/databases/" in path:
                    return self._send(
                        {
                            "id": "00000000-0000-0000-0000-000000000000",
                            "name": "default_database",
                            "tenant": "default_tenant",
                        }
                    )
                if "/tenants/" in path:
                    return self._send({"name": "default_tenant"})
                return self._held({})

            def do_POST(self) -> None:
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self._record("POST")
                self._held({})

        class _Server(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request: Any, client_address: Any) -> None:
                # A held request answers a client that already timed out; the
                # broken pipe is expected, not a failure to report.
                pass

        self.server = _Server(("127.0.0.1", 0), _Handler)
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def stub() -> Iterator[_StubChroma]:
    server = _StubChroma()
    try:
        yield server
    finally:
        server.close()


def _build(stub: _StubChroma, **settings_kwargs: Any) -> Any:
    return build_http_client(
        make_chroma_settings(**settings_kwargs),
        host="127.0.0.1",
        port=stub.port,
        ssl=False,
    )


# --------------------------------------------------------------------------- #
# T1: the builder
# --------------------------------------------------------------------------- #


def test_the_builder_bounds_every_client_it_builds(stub: _StubChroma) -> None:
    """chromadb's own session is ``httpx.Client(timeout=None)``. Each
    ``HttpClient()`` has its own session, so each one is bounded here."""
    first = _build(stub)
    second = _build(stub)

    assert first._server._session is not second._server._session
    for client in (first, second):
        assert client._server._session.timeout == httpx.Timeout(
            CHROMA_HTTP_TIMEOUT_SECONDS
        )
    assert CHROMA_HTTP_TIMEOUT_SECONDS == 30.0


def test_the_builder_applies_the_shared_credential(stub: _StubChroma) -> None:
    """The token ``chroma_token_auth_kwargs`` resolves is on the session, and
    the server saw it on every construction request (#1173, #1462)."""
    client = _build(stub, auth_token="sekrit")

    assert client._server._session.headers.get("authorization") == "Bearer sekrit"
    construction = [r for r in stub.requests if r[0] == "GET"]
    assert len(construction) == 3
    assert {auth for _, _, auth in construction} == {"Bearer sekrit"}


def test_the_builder_sends_no_credential_when_none_is_configured(
    stub: _StubChroma,
) -> None:
    client = _build(stub)

    assert "authorization" not in client._server._session.headers
    assert {auth for _, _, auth in stub.requests} == {None}


def test_the_builder_applies_settings_overrides(stub: _StubChroma) -> None:
    """The ingester passes ``allow_reset=True``; the builder keeps it."""
    client = build_http_client(
        make_chroma_settings(),
        host="127.0.0.1",
        port=stub.port,
        ssl=False,
        allow_reset=True,
    )

    assert client.get_settings().allow_reset is True


def test_a_client_with_no_session_to_bound_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chromadb upgrade that moves the session fails loudly rather than
    shipping requests with no timeout."""
    monkeypatch.setattr(chromadb, "HttpClient", lambda **kwargs: SimpleNamespace())

    with pytest.raises(ChromaClientConfigError, match="exposes no HTTP session"):
        build_http_client(make_chroma_settings(), host="chromadb", port=8000, ssl=False)


def test_a_malformed_credential_is_refused_before_any_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _must_not_be_called(**kwargs: Any) -> None:  # pragma: no cover
        raise AssertionError("no client may be built with a malformed token")

    monkeypatch.setattr(chromadb, "HttpClient", _must_not_be_called)

    with pytest.raises(ChromaClientConfigError, match="CHROMADB_AUTH_TOKEN"):
        build_http_client(
            make_chroma_settings(auth_token="bad token"),
            host="chromadb",
            port=8000,
            ssl=False,
        )


# --------------------------------------------------------------------------- #
# R8: TLS through the door
# --------------------------------------------------------------------------- #


@pytest.fixture
def created_clients(monkeypatch: pytest.MonkeyPatch) -> List[Any]:
    """The REAL ``chromadb.HttpClient`` runs; only the client it creates is
    stubbed, since connecting is not the point. ``HttpClient`` writes its
    ``ssl`` argument into ``settings.chroma_server_ssl_enabled`` itself."""
    created: List[Any] = []

    def _client_creator(tenant: str, database: str, settings: Any) -> Any:
        client = SimpleNamespace(
            _server=SimpleNamespace(_session=SimpleNamespace()),
            get_settings=lambda: settings,
            get_or_create_collection=MagicMock(),
        )
        created.append(client)
        return client

    monkeypatch.setattr(chromadb, "ClientCreator", _client_creator)
    return created


@pytest.mark.parametrize(
    "url,ssl,port",
    [("https://chroma.internal", True, 443), ("http://chroma.internal", False, 80)],
)
def test_the_container_path_takes_tls_from_the_url(
    created_clients: List[Any], url: str, ssl: bool, port: int
) -> None:
    """An ``https://`` server must never receive the bearer token in
    plaintext on port 443."""
    client = _create_chromadb_client(
        make_chroma_settings(chromadb_url=url, auth_token="sekrit"),
        "/nonexistent",
        "KB",
    )

    settings = client.get_settings()
    assert settings.chroma_server_ssl_enabled is ssl
    assert settings.chroma_server_http_port == port


@pytest.mark.parametrize(
    "branch,ssl",
    [
        ({"chromadb_url": "https://chroma.internal"}, True),
        ({"chromadb_url": "http://chroma.internal"}, False),
        ({"chromadb_host": "chromadb.faultmaven.svc"}, False),
    ],
    ids=["url-https", "url-http", "host"],
)
def test_the_ingester_takes_tls_from_the_url(
    created_clients: List[Any], branch: dict, ssl: bool
) -> None:
    ingester = KnowledgeIngester(settings=make_chroma_settings(**branch))

    assert ingester.degraded is False
    assert ingester.chroma_client.get_settings().chroma_server_ssl_enabled is ssl


# --------------------------------------------------------------------------- #
# T2: the timeout ends a held request
# --------------------------------------------------------------------------- #


def test_a_held_request_ends_at_the_client_timeout(
    stub: _StubChroma, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stub answers 200 only after its hold; the client gives up first."""
    monkeypatch.setattr(chroma_client, "CHROMA_HTTP_TIMEOUT_SECONDS", 0.5)
    client = _build(stub)

    with pytest.raises(httpx.ReadTimeout):
        client.heartbeat()

    assert stub.arrived.is_set(), "the request never reached the stub"
    assert not stub.answered.is_set(), "the stub answered before the client gave up"


def test_control_without_the_timeout_the_same_request_is_answered(
    stub: _StubChroma,
) -> None:
    """Proves the test above bites: the same request on chromadb's own
    unbounded session waits out the hold and returns normally."""
    client = _build(stub)
    client._server._session.timeout = httpx.Timeout(None)

    assert client.heartbeat() == 1
    assert stub.answered.is_set()


# --------------------------------------------------------------------------- #
# T3: the loop stays live end to end
# --------------------------------------------------------------------------- #


async def _loop_ran_while_held(stub: _StubChroma, call: Any) -> Tuple[bool, Any]:
    """Await ``call`` beside a ticker; report whether the ticker ran while the
    stub held the request.

    The ticker is the evidence: it can only run while the loop is free. When
    it sees the request held, it releases it, so on the HTTP arm the call
    ends at once. On the loop thread the ticker cannot run until the stub's
    own hold has expired and the request was answered.
    """
    ran_while_held = False
    stop = asyncio.Event()

    async def _ticker() -> None:
        nonlocal ran_while_held
        while not stop.is_set():
            if stub.arrived.is_set() and not stub.answered.is_set():
                ran_while_held = True
                stub.release.set()
            await asyncio.sleep(0.01)

    ticker = asyncio.create_task(_ticker())
    # Let the ticker take its first step: a call that blocks the loop from
    # its first line would otherwise run before the ticker ever did.
    await asyncio.sleep(0)
    try:
        result = await call
    finally:
        stop.set()
        await ticker
    return ran_while_held, result


async def test_a_held_request_does_not_hold_the_event_loop(stub: _StubChroma) -> None:
    store = CaseVectorStore(_build(stub))
    assert store.blocking_executor is CHROMA_EXECUTOR

    ran_while_held, count = await _loop_ran_while_held(
        stub, store.get_case_document_count("case-held")
    )

    assert stub.arrived.is_set(), "the call never reached the held path"
    assert ran_while_held, "the loop did not run while the request was held"
    # The wrapper answers any failure with 0 (pre-existing); the stub's
    # empty answer is one.
    assert count == 0


async def test_control_the_same_call_on_the_loop_thread_holds_it(
    stub: _StubChroma,
) -> None:
    """Proves the test above bites: with the executor removed, the loop
    cannot run until the stub's hold has expired."""
    store = CaseVectorStore(_build(stub))
    store.blocking_executor = None

    ran_while_held, _ = await _loop_ran_while_held(
        stub, store.get_case_document_count("case-held")
    )

    assert stub.arrived.is_set(), "the call never reached the held path"
    assert not ran_while_held


# --------------------------------------------------------------------------- #
# T5 and R4: the arm each store is on, and the run-time door check
# --------------------------------------------------------------------------- #


def _stores(client: Any) -> List[Any]:
    return [
        CaseVectorStore(client),
        KnowledgeVectorStore(client),
        ChromaDBVectorStore(client=client, collection_name="arm_test"),
    ]


def test_is_http_client_is_decided_by_the_client_object(
    stub: _StubChroma, tmp_path: Any
) -> None:
    assert is_http_client(_build(stub)) is True
    assert is_http_client(create_persistent_client(str(tmp_path / "chroma"))) is False
    assert is_http_client(None) is False
    assert is_http_client(MagicMock()) is False


def test_each_store_runs_off_the_loop_only_over_an_http_client(
    stub: _StubChroma, tmp_path: Any
) -> None:
    http = _build(stub)
    # ChromaDBVectorStore opens its collection at construction (boot); the
    # stub holds that request, and this test is about the arm, not the call.
    http.get_or_create_collection = MagicMock()
    local = create_persistent_client(str(tmp_path / "chroma"))

    assert [s.blocking_executor for s in _stores(http)] == [CHROMA_EXECUTOR] * 3
    assert [s.blocking_executor for s in _stores(local)] == [None] * 3


@pytest.mark.parametrize(
    "make_store",
    [
        CaseVectorStore,
        KnowledgeVectorStore,
        lambda c: ChromaDBVectorStore(client=c, collection_name="arm_test"),
    ],
    ids=["case", "knowledge", "chromadb"],
)
def test_each_store_refuses_an_http_client_built_outside_the_door(
    stub: _StubChroma, make_store: Any
) -> None:
    """Built directly, the client has no request timeout. The refusal comes
    before ChromaDBVectorStore opens its collection, which the stub would
    hold."""
    outside = chromadb.HttpClient(
        host="127.0.0.1",
        port=stub.port,
        settings=ChromaSettings(anonymized_telemetry=False),
    )
    assert is_http_client(outside)

    with pytest.raises(ChromaClientConfigError, match="build_http_client"):
        make_store(outside)
    with pytest.raises(ChromaClientConfigError):
        blocking_executor_for(outside)


def test_a_persistent_client_cannot_be_turned_into_an_http_one_by_the_env(
    stub: _StubChroma, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """chromadb's ``Settings`` reads ``CHROMA_API_IMPL`` and the server's host
    and port from the environment and ``.env``. Left unpinned, this would
    build an untimed HTTP client to the stub, outside the door."""
    monkeypatch.setenv("CHROMA_API_IMPL", "chromadb.api.fastapi.FastAPI")
    monkeypatch.setenv("CHROMA_SERVER_HOST", "127.0.0.1")
    monkeypatch.setenv("CHROMA_SERVER_HTTP_PORT", str(stub.port))

    client = create_persistent_client(str(tmp_path / "chroma"))

    assert is_http_client(client) is False
    assert blocking_executor_for(client) is None
    assert stub.requests == []


# --------------------------------------------------------------------------- #
# Configuration refusals: never a fallback, never a degraded mode
# --------------------------------------------------------------------------- #


def _no_session_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        chromadb,
        "HttpClient",
        lambda **kwargs: SimpleNamespace(get_or_create_collection=MagicMock()),
    )


@pytest.mark.parametrize("cloud", [False, True], ids=["standalone", "cloud"])
def test_container_refuses_a_client_with_no_session_and_does_not_fall_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, cloud: bool
) -> None:
    """Standalone would otherwise swap the external store for an empty local
    one; cloud would otherwise blame connectivity (ChromaUnavailableError)."""
    _no_session_client(monkeypatch)
    settings = make_chroma_settings(cloud=cloud, chromadb_url="http://chromadb:8000")

    with pytest.raises(ChromaClientConfigError) as raised:
        _create_chromadb_client(settings, str(tmp_path / "chroma"), "KB")

    assert not isinstance(raised.value, ChromaUnavailableError)
    assert not (tmp_path / "chroma").exists()


@pytest.mark.parametrize(
    "branch",
    [
        {"chromadb_url": "http://chromadb:8000"},
        {"chromadb_host": "chromadb.faultmaven.svc"},
    ],
    ids=["url", "host"],
)
@pytest.mark.parametrize("cloud", [False, True], ids=["standalone", "cloud"])
def test_ingester_refuses_a_client_with_no_session_and_does_not_degrade(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, cloud: bool, branch: dict
) -> None:
    _no_session_client(monkeypatch)
    settings = make_chroma_settings(cloud=cloud, **branch)
    settings.database.chromadb_kb_persist_dir = str(tmp_path / "chroma-kb")

    with pytest.raises(ChromaClientConfigError) as raised:
        KnowledgeIngester(settings=settings)

    assert not isinstance(raised.value, ChromaUnavailableError)
    assert not (tmp_path / "chroma-kb").exists()


def test_the_ingester_wrapper_does_not_turn_a_refusal_into_no_ingester(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``create_knowledge_ingester`` answers ordinary failures with ``None``;
    ``None`` is the degraded mode a configuration refusal rules out."""
    _no_session_client(monkeypatch)
    settings = make_chroma_settings(chromadb_url="http://chromadb:8000")

    with pytest.raises(ChromaClientConfigError):
        create_knowledge_ingester(settings)


async def test_the_container_does_not_serve_without_its_service_layer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """R7. Standalone in development is the lenient posture, where
    ``initialize()`` swallows a composition failure and the API serves on.
    A refusal raised by ``register_tools`` (the ingester, host-only, with a
    malformed token) would then leave the API serving with no service layer,
    a worse degraded mode than the fallback the class rules out."""
    monkeypatch.setenv("ENVIRONMENT", "development")
    settings = make_chroma_settings(
        chromadb_host="chromadb.faultmaven.svc", auth_token="bad token"
    )
    settings.database.chromadb_kb_persist_dir = str(tmp_path / "chroma-kb")
    assert settings.must_not_degrade is False, "not the lenient path"

    container = DIContainer()
    container._initialized = False
    container._initializing = False

    async def _infrastructure(*_args: Any, **_kwargs: Any) -> None:
        return None

    with (
        patch("faultmaven._container_impl.get_settings", return_value=settings),
        patch(
            "faultmaven._container_impl.register_infrastructure",
            new=MagicMock(side_effect=_infrastructure),
        ),
        patch("faultmaven._container_impl.register_services") as services,
    ):
        with pytest.raises(ChromaClientConfigError, match="CHROMADB_AUTH_TOKEN"):
            await container.initialize()

    services.assert_not_called()
    assert container._initialized is False
