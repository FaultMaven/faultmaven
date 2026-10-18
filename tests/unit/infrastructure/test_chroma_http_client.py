"""ChromaDB HTTP requests are bounded, and they leave the event loop (#2056).

On Cloud every turn-path ChromaDB call was a synchronous HTTP round trip on the
event-loop thread, through a session chromadb builds with ``timeout=None``. A
stalled server therefore held the whole API loop, ``/health`` included, for as
long as it stalled, and ``call_external``'s ``asyncio.wait_for`` could not
interrupt it. These tests pin both halves of the fix against a real
``chromadb.HttpClient``:

- T1/T2: every client is built by ``build_http_client``, which applies the
  shared credential and a request timeout that actually ends a stalled request;
- T3: a store over such a client keeps the loop ticking while the server
  stalls, and the same store with its executor removed does not (the control
  that proves the test bites);
- T5: ``is_http_client`` decides which arm a store is on, by the client object;
- the configuration refusals (``ChromaClientConfigError``) are never answered
  with a fallback or a degraded mode, at either acquisition site.

The server is an in-process stub bound to port 0 by the test itself: the
suite's network guard allows only ports the test process binds. It answers the
three requests ``HttpClient()`` makes at construction and stalls every
``/collections`` request (and every POST) until the test releases it.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional, Tuple
from unittest.mock import MagicMock

import chromadb
import httpx
import pytest

from faultmaven.container.providers.infrastructure import _create_chromadb_client
from faultmaven.container.providers.tools import create_knowledge_ingester
from faultmaven.infrastructure import chroma_client
from faultmaven.infrastructure.chroma_client import (
    CHROMA_EXECUTOR,
    CHROMA_HTTP_TIMEOUT_SECONDS,
    ChromaClientConfigError,
    ChromaUnavailableError,
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


# --------------------------------------------------------------------------- #
# The stub server
# --------------------------------------------------------------------------- #


class _StubChroma:
    """A ChromaDB server stand-in that answers construction and stalls the rest."""

    def __init__(self) -> None:
        self.requests: List[Tuple[str, str, Optional[str]]] = []
        self.release = threading.Event()
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

            def _stall(self) -> None:
                stub.release.wait(30)
                self._send({})

            def do_GET(self) -> None:
                self._record("GET")
                path = self.path
                if "/collections" in path:
                    return self._stall()
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
                return self._stall()

            def do_POST(self) -> None:
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self._record("POST")
                self._stall()

        class _Server(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request: Any, client_address: Any) -> None:
                # A released stall answers a client that already timed out;
                # the broken pipe is expected, not a failure to report.
                pass

        self.server = _Server(("127.0.0.1", 0), _Handler)
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def stalled_requests(self) -> List[Tuple[str, str, Optional[str]]]:
        return [r for r in self.requests if "/collections" in r[1] or r[0] == "POST"]

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


@pytest.fixture
def short_timeout(monkeypatch: pytest.MonkeyPatch) -> float:
    """A 1 s client timeout, set BEFORE the client is built (the builder reads it)."""
    monkeypatch.setattr(chroma_client, "CHROMA_HTTP_TIMEOUT_SECONDS", 1.0)
    return 1.0


def _build(stub: _StubChroma, **settings_kwargs: Any) -> Any:
    return build_http_client(
        make_chroma_settings(**settings_kwargs), host="127.0.0.1", port=stub.port
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
        build_http_client(make_chroma_settings(), host="chromadb", port=8000)


def test_a_malformed_credential_is_refused_before_any_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _must_not_be_called(**kwargs: Any) -> None:  # pragma: no cover
        raise AssertionError("no client may be built with a malformed token")

    monkeypatch.setattr(chromadb, "HttpClient", _must_not_be_called)

    with pytest.raises(ChromaClientConfigError, match="CHROMADB_AUTH_TOKEN"):
        build_http_client(
            make_chroma_settings(auth_token="bad token"), host="chromadb", port=8000
        )


# --------------------------------------------------------------------------- #
# T2: the timeout ends a stalled request
# --------------------------------------------------------------------------- #


def test_a_stalled_request_ends_at_the_client_timeout(
    stub: _StubChroma, short_timeout: float
) -> None:
    client = _build(stub)

    started = time.monotonic()
    with pytest.raises(httpx.ReadTimeout):
        client.get_collection("anything")
    elapsed = time.monotonic() - started

    assert stub.stalled_requests(), "the request never reached the stalled path"
    assert elapsed < 3.0


# --------------------------------------------------------------------------- #
# T3: the loop stays live end to end
# --------------------------------------------------------------------------- #


async def _max_tick_gap_while(coro: Any) -> Tuple[float, Any]:
    """Run ``coro`` beside a 50 ms ticker; return the ticker's longest gap."""
    gaps: List[float] = []
    stop = asyncio.Event()

    async def _ticker() -> None:
        last = time.monotonic()
        while not stop.is_set():
            await asyncio.sleep(0.05)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    ticker = asyncio.create_task(_ticker())
    # Let the ticker take its first step: a call that blocks the loop from
    # its first line would otherwise run before the ticker ever did, and
    # leave no gap to measure.
    await asyncio.sleep(0)
    try:
        result = await coro
    finally:
        stop.set()
        await ticker
    return max(gaps), result


async def test_a_stalled_server_does_not_hold_the_event_loop(
    stub: _StubChroma, short_timeout: float
) -> None:
    store = CaseVectorStore(_build(stub))
    assert store.blocking_executor is CHROMA_EXECUTOR

    started = time.monotonic()
    max_gap, count = await _max_tick_gap_while(
        store.get_case_document_count("case-stalled")
    )
    elapsed = time.monotonic() - started

    assert stub.stalled_requests(), "the call never reached the stalled path"
    assert max_gap < 0.25, f"the loop was held for {max_gap:.2f}s"
    # The wrapper answers any failure with 0 (pre-existing); what matters
    # here is that the call ENDED, at the client timeout, not at the stall.
    assert count == 0
    assert elapsed < 3.0


async def test_control_the_same_call_on_the_loop_thread_holds_it(
    stub: _StubChroma, short_timeout: float
) -> None:
    """Proves the test above bites: with the executor removed, the same call
    blocks the loop for the whole client timeout."""
    store = CaseVectorStore(_build(stub))
    store.blocking_executor = None

    max_gap, _ = await _max_tick_gap_while(
        store.get_case_document_count("case-stalled")
    )

    assert max_gap >= 0.9, f"the loop was held for only {max_gap:.2f}s"


# --------------------------------------------------------------------------- #
# T5: is_http_client, and the arm each store is on
# --------------------------------------------------------------------------- #


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
    # stub stalls that request, and this test is about the arm, not the call.
    http.get_or_create_collection = MagicMock()
    local = create_persistent_client(str(tmp_path / "chroma"))

    for make in (
        CaseVectorStore,
        KnowledgeVectorStore,
        lambda c: ChromaDBVectorStore(client=c, collection_name="arm_test"),
    ):
        assert make(http).blocking_executor is CHROMA_EXECUTOR
        assert make(local).blocking_executor is None


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
