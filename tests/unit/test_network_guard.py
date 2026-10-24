"""No test can reach a network or a model server (``tests/network_guard.py``).

Twice a test reached the Ollama server on the developer box at
``localhost:11434`` (a real ``ProviderRegistry`` defaults to the ``local``
provider, and a socket block built on a test's own ``monkeypatch`` is undone by
``monkeypatch.undo()``). The guard is installed at conftest import, on the
``socket`` module, so these tests pin the four things that make it a guard
and not a convention:

* it refuses a loopback model-server port, and keeps refusing after a test
  calls ``monkeypatch.undo()``;
* it lets a test connect to a port that test bound, and to the PostgreSQL the
  job configures;
* it refuses resolving a name that is not this machine's, before the lookup,
  on each resolver path it patches (#2232: three tests passed in CI only
  because Presidio's name did not resolve there);
* a refused connect or lookup FAILS the test even when the code under test
  caught it.

The first three are asserted in this process, which is the one the guard is
installed in. The allowlist is derived from the environment at install, so the
configured-service case runs in a child interpreter whose environment names a
listener the child did not bind. The fourth runs a child pytest session with
the real conftest hook.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import pathlib
import socket
import subprocess
import sys
import textwrap
from unittest import mock

import aiohttp
import aiohttp.resolver
import pycares
import pytest

from faultmaven.infrastructure.llm import local_llm_manager
from tests import network_guard
from tests.network_guard import (
    MODEL_SERVER_PORTS,
    NameResolutionBlocked,
    NetworkAccessBlocked,
    NetworkGuard,
    configured_service_ports,
)

pytest_plugins = ["pytester"]

pytestmark = pytest.mark.unit

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

#: A name that is not this machine's. ``.invalid`` never resolves (RFC 6761),
#: so without the lookup rule a test using it passes on DNS failure alone.
_ELSEWHERE = "model.example.invalid"


@pytest.fixture(autouse=True)
def _forget_the_ports_other_tests_bound():
    """Judge each test here against its own binds, not the worker's history.

    The bind record is session-wide, so an earlier test that bound 8080 for a
    mock server would make this file's refusal of 8080 depend on test order.
    ``mock.patch.object`` rather than ``monkeypatch``: one test below calls
    ``monkeypatch.undo()`` and this must not be undone with it.
    """
    if network_guard.GUARD is None:
        yield
        return
    with mock.patch.object(network_guard.GUARD, "bound_ports", set()):
        yield


def _listener() -> socket.socket:
    """A listening loopback socket for a CHILD interpreter to connect to.

    The child is a separate process, so its guard has no record that this
    process bound the port: reaching it takes the allowlist, not the bind record.
    """
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    return server


def test_the_guard_is_installed_in_this_process():
    assert network_guard.GUARD is not None, (
        "the guard is off: FM_TEST_ALLOW_LIVE_NETWORK is set in the environment "
        "of this pytest process"
    )
    assert socket.socket.connect.__module__ == network_guard.__name__
    assert socket.getaddrinfo.__module__ == network_guard.__name__
    assert network_guard.GUARD.hostname == socket.gethostname().lower().rstrip(".")


@pytest.mark.parametrize("port", sorted(MODEL_SERVER_PORTS))
def test_a_model_server_port_is_refused_and_named(port):
    with network_guard.expecting_refusals() as refused:
        with pytest.raises(NetworkAccessBlocked) as raised:
            socket.create_connection(("127.0.0.1", port), timeout=1)
    assert raised.value.host == "127.0.0.1"
    assert raised.value.port == port
    assert MODEL_SERVER_PORTS[port] in str(raised.value)
    assert f"127.0.0.1:{port}" in str(raised.value)
    assert len(refused) == 1


def test_ollama_stays_refused_after_the_test_calls_monkeypatch_undo(monkeypatch):
    """The reason the guard is not a fixture: ``undo`` has nothing of it to restore."""
    monkeypatch.setattr(socket, "getaddrinfo", socket.getaddrinfo)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://localhost:11434/db")
    monkeypatch.undo()
    with network_guard.expecting_refusals() as refused:
        with pytest.raises(NetworkAccessBlocked, match=r"127\.0\.0\.1:11434"):
            with socket.socket() as sock:
                sock.connect(("127.0.0.1", 11434))
        with pytest.raises(NetworkAccessBlocked, match=r"localhost:11434"):
            socket.create_connection(("localhost", 11434), timeout=1)
    assert len(refused) == 2


def test_every_connect_path_is_guarded():
    async def through_asyncio():
        loop = asyncio.get_running_loop()
        with pytest.raises(NetworkAccessBlocked):
            await asyncio.open_connection("127.0.0.1", 11434)
        with pytest.raises(NetworkAccessBlocked):
            await loop.create_connection(asyncio.Protocol, "localhost", 11434)
        sock = socket.socket()
        sock.setblocking(False)
        try:
            with pytest.raises(NetworkAccessBlocked):
                await loop.sock_connect(sock, ("::1", 11434))
        finally:
            sock.close()

    with network_guard.expecting_refusals() as refused:
        asyncio.run(through_asyncio())
        probe = socket.socket()
        try:
            assert probe.connect_ex(("127.0.0.1", 11434)) != 0
            # ``0.0.0.0`` reaches a local listener, so it is held to the same rule.
            with pytest.raises(NetworkAccessBlocked):
                probe.connect(("0.0.0.0", 11434))
        finally:
            probe.close()
    # Each layer is the one that answered, not a later layer behind it.
    assert [record.split(" via ")[1].split(",")[0] for record in refused] == [
        "loop.create_connection",
        "loop.create_connection",
        "loop.sock_connect",
        "socket.connect_ex",
        "socket.connect",
    ]


def test_a_name_or_an_address_off_the_box_is_refused_without_a_lookup():
    with network_guard.expecting_refusals() as refused:
        with pytest.raises(NetworkAccessBlocked, match="not a loopback address"):
            socket.create_connection(("api.anthropic.com", 443), timeout=1)
        with pytest.raises(NetworkAccessBlocked, match="not a loopback address"):
            with socket.socket() as sock:
                sock.connect(("203.0.113.7", 11434))
    assert len(refused) == 2


def test_a_name_that_is_not_this_machines_is_refused_before_the_lookup():
    """#2232: where such a name does not resolve, nothing reached a connect,
    so a test that reached for the server passed on DNS failure alone."""

    async def through_asyncio():
        with pytest.raises(NameResolutionBlocked):
            await asyncio.get_running_loop().getaddrinfo(_ELSEWHERE, 443)

    with network_guard.expecting_refusals() as refused:
        with pytest.raises(NameResolutionBlocked) as raised:
            socket.getaddrinfo(_ELSEWHERE, 443)
        with pytest.raises(NameResolutionBlocked):
            socket.gethostbyname(_ELSEWHERE)
        with pytest.raises(NameResolutionBlocked):
            socket.gethostbyname_ex(_ELSEWHERE)
        # Given a name, it resolves the name forward first.
        with pytest.raises(NameResolutionBlocked):
            socket.gethostbyaddr(_ELSEWHERE)
        # ``getfqdn`` swallows the refusal and answers the name back; the record
        # still fails the test that made it.
        assert socket.getfqdn(_ELSEWHERE) == _ELSEWHERE
        asyncio.run(through_asyncio())
    # What a name that does not resolve raises, so the caller's path is the one
    # it takes for that; and the record names the name.
    assert isinstance(raised.value, socket.gaierror)
    assert raised.value.errno == socket.EAI_NONAME
    assert raised.value.host == _ELSEWHERE
    assert _ELSEWHERE in str(raised.value)
    assert [record.split(" via ")[1].split(",")[0] for record in refused] == [
        "socket.getaddrinfo",
        "socket.gethostbyname",
        "socket.gethostbyname_ex",
        "socket.gethostbyaddr",
        "socket.gethostbyaddr",
        "loop.getaddrinfo",
    ]
    assert all(record.startswith(f"{_ELSEWHERE} (name lookup)") for record in refused)


def test_a_numeric_host_only_lookup_is_not_a_lookup():
    """``AI_NUMERICHOST`` accepts an address and nothing else: a name fails at
    once, in the resolver, without being looked up, so the guard lets it by."""

    async def through_asyncio():
        loop = asyncio.get_running_loop()
        with pytest.raises(socket.gaierror) as raised:
            await loop.getaddrinfo(_ELSEWHERE, 443, flags=socket.AI_NUMERICHOST)
        assert not isinstance(raised.value, NameResolutionBlocked)

    with network_guard.expecting_refusals() as refused:
        with pytest.raises(socket.gaierror) as raised:
            socket.getaddrinfo(_ELSEWHERE, 443, flags=socket.AI_NUMERICHOST)
        assert not isinstance(raised.value, NameResolutionBlocked)
        assert raised.value.errno == socket.EAI_NONAME
        asyncio.run(through_asyncio())
        assert socket.getaddrinfo(
            "203.0.113.7",
            443,
            socket.AF_INET,
            socket.SOCK_STREAM,
            0,
            socket.AI_NUMERICHOST,
        )[0][4] == ("203.0.113.7", 443)
    assert refused == []


@pytest.mark.parametrize(
    ("method", "call"),
    [
        (
            "getaddrinfo",
            lambda channel: channel.getaddrinfo(
                host=_ELSEWHERE, port=None, callback=lambda *_: None
            ),
        ),
        (
            "query",
            lambda channel: channel.query(
                name=_ELSEWHERE,
                query_type=pycares.QUERY_TYPE_A,
                callback=lambda *_: None,
            ),
        ),
        (
            "search",
            lambda channel: channel.search(
                _ELSEWHERE, pycares.QUERY_TYPE_A, callback=lambda *_: None
            ),
        ),
    ],
)
def test_c_ares_lookups_keep_their_signature_and_are_refused(method, call):
    """The name may come by keyword, as pycares' own signature allows."""
    channel = pycares.Channel()
    try:
        with network_guard.expecting_refusals() as refused:
            with pytest.raises(NameResolutionBlocked):
                call(channel)
    finally:
        channel.close()
    assert len(refused) == 1
    assert f" via pycares.Channel.{method}, " in refused[0]
    guarded = getattr(pycares.Channel, method)
    assert inspect.signature(guarded) == inspect.signature(guarded.__wrapped__)


@pytest.mark.parametrize(
    ("resolver", "via"),
    [
        ("AsyncResolver", "pycares.Channel.getaddrinfo"),
        ("ThreadedResolver", "loop.getaddrinfo"),
    ],
)
async def test_aiohttp_resolves_through_the_guard_with_either_resolver(resolver, via):
    """``AsyncResolver`` (c-ares, aiohttp's default once aiodns is importable)
    resolves below the ``socket`` module; ``ThreadedResolver`` through
    asyncio. Either way the request fails as a DNS failure and is recorded."""
    connector = aiohttp.TCPConnector(resolver=getattr(aiohttp.resolver, resolver)())
    with network_guard.expecting_refusals() as refused:
        async with aiohttp.ClientSession(connector=connector) as session:
            with pytest.raises(aiohttp.ClientConnectorDNSError):
                await session.get(f"http://{_ELSEWHERE}/")
    assert len(refused) == 1
    assert refused[0].startswith(f"{_ELSEWHERE} (name lookup) via {via}, from ")


def test_a_port_the_test_bound_itself_connects():
    with socket.create_server(("127.0.0.1", 0)) as server:
        port = server.getsockname()[1]
        with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
            accepted, _ = server.accept()
            client.sendall(b"ping")
            assert accepted.recv(4) == b"ping"
            accepted.close()


def test_a_port_the_test_bound_is_reached_by_the_name_localhost():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        with socket.create_connection(("localhost", port), timeout=2):
            pass


def test_a_closed_port_the_test_owned_is_the_kernels_refusal_not_the_guards():
    """The pattern for a test of "server unreachable": bind, close, connect."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with pytest.raises(ConnectionRefusedError) as raised:
        socket.create_connection(("127.0.0.1", port), timeout=2)
    assert not isinstance(raised.value, NetworkAccessBlocked)


def test_the_database_url_service_port_is_the_one_the_job_configures():
    ports = configured_service_ports(
        {"DATABASE_URL": "postgresql+asyncpg://localhost:5432/fmtest"}
    )
    assert set(ports) == {5432}
    assert configured_service_ports(
        {"DATABASE_URL": "postgresql://u@localhost/db"}
    ) == {5432: "the PostgreSQL named by DATABASE_URL"}
    assert configured_service_ports({"DATABASE_URL": "sqlite+aiosqlite:///x.db"}) == {}
    # not on this box: rule 1 refuses it whatever the port
    assert (
        configured_service_ports({"DATABASE_URL": "postgresql://u@db.internal/db"})
        == {}
    )
    # a model server's port is never allowed, whatever the environment says
    assert (
        configured_service_ports({"DATABASE_URL": "postgresql://u@localhost:8000/db"})
        == {}
    )
    redis = configured_service_ports({"REDIS_HOST": "localhost", "REDIS_PORT": "6379"})
    assert set(redis) == {6379}
    assert set(configured_service_ports({"REDIS_URL": "redis://127.0.0.1:6380/0"})) == {
        6380
    }
    assert configured_service_ports({}) == {}


def test_the_policy_in_isolation():
    guard = NetworkGuard({5432: "the PostgreSQL named by DATABASE_URL"})
    guard.note_bound(40001)
    guard.note_bound(1234)  # a model server's port, bound by the test itself
    for host, port in (("127.0.0.1", 40001), ("localhost", 5432), ("::1", 5432)):
        guard.check(host, port, "test")
    for host, port in (
        ("127.0.0.1", 1234),
        ("127.0.0.1", 11434),
        ("::ffff:127.0.0.1", 11434),
        ("0.0.0.0", 8080),
        ("localhost", "http"),
        ("10.0.0.5", 5432),
        ("postgres", 5432),
    ):
        with pytest.raises(NetworkAccessBlocked):
            guard.check(host, port, "test")
    assert len(guard.drain_refusals()) == 7
    assert guard.drain_refusals() == []


def test_the_lookup_policy_in_isolation():
    guard = NetworkGuard(hostname="CI-Box.example.internal.")
    for host in (
        None,  # a passive lookup: no name
        "",
        "localhost",
        "LOCALHOST.",
        "api.localhost",
        b"localhost",
        "127.0.0.1",
        "::1",
        "fe80::1%eth0",
        "203.0.113.7",  # an address names no lookup; rule 1 holds its connect
        "ci-box",  # this machine: its short form and its FQDN, exactly
        "CI-BOX.example.internal.",
    ):
        guard.check_lookup(host, "test")
    refused = (
        "presidio-analyzer.faultmaven.local",
        "api.anthropic.com",
        "ci-box-2",
        "ci-box.lan",  # shares the first label, but is not this machine's FQDN
        "ci-box.example.internal.evil.example",
        "postgres",
        b"redis",
        # Loopback spellings that need no DNS, refused all the same: the rule
        # is decided without a lookup and alike on every machine (module doc).
        "127.1",
        "ip6-localhost",
    )
    for host in refused:
        with pytest.raises(NameResolutionBlocked):
            guard.check_lookup(host, "test")
    assert len(guard.drain_refusals()) == len(refused)
    # A box named ``api`` may look itself up, not a server whose name starts so.
    api = NetworkGuard(hostname="api")
    api.check_lookup("api", "test")
    with pytest.raises(NameResolutionBlocked):
        api.check_lookup("api.openai.com", "test")
    with pytest.raises(NameResolutionBlocked):
        NetworkGuard().check_lookup("ci-box", "test")  # no hostname: no such match


async def test_booting_the_app_neither_probes_nor_starts_a_local_model_server():
    """The lifespan's ``localhost:8080`` probe is stood in, with its signature."""
    stand_in = local_llm_manager.check_and_start_local_llm_service
    real = stand_in.__real__
    assert inspect.signature(stand_in) == inspect.signature(real)
    assert inspect.iscoroutinefunction(stand_in)
    assert await stand_in("local", "http://localhost:8080", "llama2-7b") is False
    assert network_guard.drain_refusals() == []


_CHILD_SERVICE_PORT = """
import socket, sys
from tests import network_guard

guard = network_guard.install()
service_port, other_port = int(sys.argv[1]), int(sys.argv[2])

socket.create_connection(("127.0.0.1", service_port), timeout=2).close()
print("service port connected")

try:
    socket.create_connection(("127.0.0.1", other_port), timeout=2).close()
except network_guard.NetworkAccessBlocked:
    print("other port refused")
else:
    print("other port CONNECTED")

try:
    socket.create_connection(("127.0.0.1", 11434), timeout=2).close()
except network_guard.NetworkAccessBlocked:
    print("ollama refused")
else:
    print("ollama CONNECTED")
"""


def test_a_connect_to_the_configured_postgres_port_works_and_others_do_not():
    """Allowlist, end to end, in an interpreter that did not bind the listener."""
    with _listener() as postgres, _listener() as other:
        env = dict(os.environ)
        env.pop(network_guard.ALLOW_LIVE_NETWORK_ENV, None)
        env["DATABASE_URL"] = (
            f"postgresql+asyncpg://127.0.0.1:{postgres.getsockname()[1]}/fmtest"
        )
        env["PYTHONPATH"] = os.pathsep.join(
            filter(None, [str(REPO_ROOT), env.get("PYTHONPATH")])
        )
        done = subprocess.run(
            [
                sys.executable,
                "-c",
                _CHILD_SERVICE_PORT,
                str(postgres.getsockname()[1]),
                str(other.getsockname()[1]),
            ],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
    assert done.returncode == 0, done.stderr
    assert done.stdout.split("\n")[:3] == [
        "service port connected",
        "other port refused",
        "ollama refused",
    ]


def test_the_environment_switch_is_read_once_so_a_test_cannot_set_it(monkeypatch):
    monkeypatch.setenv(network_guard.ALLOW_LIVE_NETWORK_ENV, "1")
    assert network_guard.install() is network_guard.GUARD
    with network_guard.expecting_refusals():
        with pytest.raises(NetworkAccessBlocked):
            socket.create_connection(("127.0.0.1", 11434), timeout=1)


_CHILD_TESTS = """
import socket

import httpx
import pytest
import requests

from tests import network_guard


def test_a_swallowed_refusal_still_fails():
    try:
        socket.create_connection(("127.0.0.1", 11434), timeout=1)
    except OSError:
        pass


def test_a_real_provider_style_fallback_still_fails():
    # What the router does for an unreachable server: catch, log, fall back.
    try:
        httpx.get("http://localhost:11434/api/tags", timeout=1)
    except httpx.HTTPError:
        pass


def test_a_swallowed_lookup_still_fails():
    # #2232: where the name does not resolve, the client fails before any
    # connect, so without the lookup rule this passed on DNS failure alone.
    try:
        requests.get("http://example.invalid/", timeout=1)
    except requests.RequestException:
        pass


def test_a_refusal_the_test_expects_passes():
    with network_guard.expecting_refusals() as refused:
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", 11434), timeout=1)
    assert len(refused) == 1


def test_a_test_that_connects_to_nothing_passes():
    assert True


@pytest.mark.xfail(reason="an xfail mark does not excuse the refusal")
def test_a_swallowed_refusal_in_an_xfail_test_still_fails():
    try:
        socket.create_connection(("127.0.0.1", 11434), timeout=1)
    except OSError:
        pass
    assert False


def test_a_swallowed_refusal_then_a_skip_still_fails():
    try:
        socket.create_connection(("127.0.0.1", 11434), timeout=1)
    except OSError:
        pass
    pytest.skip("skipping does not excuse the refusal")
"""


@pytest.mark.parametrize(
    "registered_by",
    [
        "tests.conftest",
        # ``tests/integration`` has its own pytest.ini, so a run rooted there
        # never registers ``tests/conftest.py``'s hooks; its conftest re-exports
        # this one and has to keep doing so.
        "tests.integration.conftest",
    ],
)
def test_a_refused_connect_fails_the_test_even_when_the_code_swallowed_it(
    pytester, monkeypatch, registered_by
):
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(filter(None, [str(REPO_ROOT), os.environ.get("PYTHONPATH")])),
    )
    monkeypatch.delenv(network_guard.ALLOW_LIVE_NETWORK_ENV, raising=False)
    pytester.makepyfile(textwrap.dedent(_CHILD_TESTS))
    result = pytester.runpytest_subprocess(
        "-p", "no:cacheprovider", "-p", registered_by, timeout=600
    )
    result.assert_outcomes(passed=2, failed=5)
    result.stdout.fnmatch_lines(
        [
            "*the network guard refused 1 connection(s) or name lookup(s) during call of*",
            "*127.0.0.1:11434 (Ollama's default port) via socket.create_connection*",
        ]
    )
    result.stdout.fnmatch_lines(["*localhost:11434*"])
    result.stdout.fnmatch_lines(
        ["*example.invalid (name lookup) via socket.getaddrinfo, from *"]
    )


_CHILD_COLD_TIKTOKEN = """
import os, sys
from tests import network_guard, offline_tiktoken

network_guard.install()
offline_tiktoken.install()
import tiktoken

encoding = tiktoken.get_encoding("cl100k_base")
print(encoding.encode("hello world"))
print(sorted(os.listdir(sys.argv[1])))
print(network_guard.drain_refusals())
"""


def test_a_cold_machine_counts_tokens_without_the_network(tmp_path):
    """A runner with an empty tiktoken cache reads the encoding from the repository.

    Without ``tests/offline_tiktoken.py`` the first ``get_encoding`` in a process
    asks Azure storage for the file, the guard refuses it, and the 45 tests that
    assert on real token counts fail. The cache directory is empty, stays
    empty, and (``TIKTOKEN_CACHE_DIR`` unset) the answer is the same.
    """
    cold = tmp_path / "cold"
    cold.mkdir()
    for env_var in ("TIKTOKEN_CACHE_DIR", None):
        env = dict(os.environ)
        env.pop(network_guard.ALLOW_LIVE_NETWORK_ENV, None)
        env["PYTHONPATH"] = os.pathsep.join(
            filter(None, [str(REPO_ROOT), env.get("PYTHONPATH")])
        )
        env.pop("TIKTOKEN_CACHE_DIR", None)
        env.pop("DATA_GYM_CACHE_DIR", None)
        if env_var:
            env[env_var] = str(cold)
        else:
            env["TMPDIR"] = str(cold)  # tiktoken's default cache lives under it
        done = subprocess.run(
            [sys.executable, "-c", _CHILD_COLD_TIKTOKEN, str(cold)],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert done.returncode == 0, done.stderr
        assert done.stdout.splitlines() == ["[15339, 1917]", "[]", "[]"]
