"""A test process connects to nothing it did not start itself.

Why this exists. Twice a test reached the Ollama server running on the
developer box at ``localhost:11434``. Both times the server answered 404 (no
model loaded), so nothing failed, but the rule is no live model calls at all.
Three things let it happen and none of them is fixed by a better test:

* a real ``ProviderRegistry`` defaults to the ``local`` provider, ``llama2`` at
  ``http://localhost:11434``, so any test that builds one and sends a request
  has a live target without naming it;
* a socket block built on a test's own ``monkeypatch`` is undone by
  ``monkeypatch.undo()``, and by that test's teardown;
* many files that build a real ``LLMRouter`` / ``ProviderRegistry`` block
  nothing, and a per-file block is a rule each new file has to remember.

So the guard is installed once, at conftest import, on the ``socket`` module
itself, before any test is collected. It is not a fixture and not a
``monkeypatch``: no test owns it, so no test's teardown restores the original.

The rule it enforces:

1. A connection to a non-loopback address is refused. Always.
2. A connection to a loopback address is refused unless the port is
   (a) one this process bound itself during the session -- ``bind`` records
   it, which covers the mock servers a test starts and the free-port probes it
   makes -- or (b) a service the job configures in its own environment: the
   PostgreSQL named by ``DATABASE_URL`` and the Redis named by ``REDIS_URL`` /
   ``REDIS_HOST``. The model-server ports (``MODEL_SERVER_PORTS``) are never
   allowed, whatever the environment says.
3. A refusal raises ``NetworkAccessBlocked`` (a ``ConnectionRefusedError``, so
   code written for an unreachable server takes the path it always took), names
   the host and port, and is RECORDED. The record fails the test (see
   ``drain_refusals`` and ``pytest_runtest_makereport`` in ``tests/conftest.py``)
   even when the code under test caught the exception: an application that
   treats "provider unreachable" as a fallback would otherwise turn the refused
   connect into a green test, which is the situation this exists to end.

A test that needs to prove its code survives a refused connection binds a port
and closes it (the port is then its own, and the kernel refuses for real), or
wraps the call in ``expecting_refusals``.

Patched paths: ``socket.socket.connect`` / ``connect_ex`` (every stdlib and
third-party client ends here, ``ssl`` and asyncio's selector loops included),
``socket.create_connection``, and asyncio's ``create_connection`` /
``sock_connect``, which are checked before name resolution. Not covered: a
child process (it has its own interpreter), uvloop's native connect, and UDP
``sendto`` with no ``connect``. Name resolution is not a connection and is left
alone.

``FM_TEST_ALLOW_LIVE_NETWORK=1`` in the environment of the pytest process
disables the guard. It is read once, at install, so a test cannot set it. It
exists for the opt-in ``-m real_llm`` lane, which exists to reach a provider.
"""

from __future__ import annotations

import asyncio.base_events
import asyncio.selector_events
import contextlib
import errno
import ipaddress
import os
import socket
import threading
import traceback
from typing import Iterator, Mapping, Optional
from urllib.parse import urlsplit

#: The well-known local model servers, named in a refusal so a failure says
#: what the test was about to call. Also the ports no environment can allow.
MODEL_SERVER_PORTS = {
    11434: "Ollama",
    8080: "llama.cpp server",
    8000: "vLLM",
    1234: "LM Studio",
    12434: "Docker Model Runner",
}

#: Set in the environment of the pytest process to disable the guard (the
#: opt-in ``-m real_llm`` lane). Read once, in ``install``.
ALLOW_LIVE_NETWORK_ENV = "FM_TEST_ALLOW_LIVE_NETWORK"

_INET_FAMILIES = (socket.AF_INET, socket.AF_INET6)


class NetworkAccessBlocked(ConnectionRefusedError):
    """A test process tried to open a connection the guard does not allow."""

    def __init__(self, message: str, host: object, port: object) -> None:
        super().__init__(errno.ECONNREFUSED, message)
        self.host = host
        self.port = port


def _is_local_host(host: object) -> bool:
    """Whether ``host`` is this machine, decided without a DNS lookup.

    ``localhost`` and the loopback addresses are; so is the unspecified address
    (``0.0.0.0`` / ``::`` / ``''``), because connecting to it reaches a local
    listener -- ``connect(("0.0.0.0", 11434))`` is Ollama. Any other NAME is
    not local: resolving it would itself be the outbound traffic.
    """
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if not isinstance(host, str):
        return False
    name = host.split("%", 1)[0].lower().rstrip(".")
    if name in ("", "localhost") or name.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_loopback or address.is_unspecified


def _endpoint(url: str, default_port: int) -> Optional[tuple[str, int]]:
    """``(host, port)`` of a service URL, or None when it names no host."""
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if not host:
        return None
    return host, port or default_port


def configured_service_ports(environ: Mapping[str, str]) -> dict[int, str]:
    """The loopback service ports THIS job's environment says it runs.

    Derived, not listed: the ``-m postgres`` job names its PostgreSQL in
    ``DATABASE_URL`` and the Cloud job names its Redis in ``REDIS_HOST`` /
    ``REDIS_PORT``, so each job allows exactly the service it started, and a
    run that configures neither allows neither. A service on a non-loopback
    host is not allowed (rule 1), and a model-server port never is.
    """
    ports: dict[int, str] = {}

    def add(endpoint: Optional[tuple[str, int]], reason: str) -> None:
        if endpoint is None:
            return
        host, port = endpoint
        if _is_local_host(host) and port not in MODEL_SERVER_PORTS:
            ports[port] = reason

    database_url = environ.get("DATABASE_URL", "")
    if database_url.startswith(("postgresql", "postgres:")):
        add(_endpoint(database_url, 5432), "the PostgreSQL named by DATABASE_URL")
    redis_url = environ.get("REDIS_URL", "")
    if redis_url:
        add(_endpoint(redis_url, 6379), "the Redis named by REDIS_URL")
    elif environ.get("REDIS_HOST"):
        try:
            redis_port = int(environ.get("REDIS_PORT", "6379"))
        except ValueError:
            redis_port = 6379
        add((environ["REDIS_HOST"], redis_port), "the Redis named by REDIS_HOST")
    return ports


def _in_repository(filename: str) -> bool:
    """A frame of this repository or the package under test, not a library's.

    The test virtualenv can itself live under a directory called ``faultmaven``,
    so the path alone is not enough: site-packages is excluded first.
    """
    if "site-packages" in filename or f"{os.sep}.venv" in filename:
        return False
    return any(
        f"{os.sep}{part}{os.sep}" in filename
        for part in ("tests", "faultmaven", "faultmaven_cloud")
    )


def _where() -> str:
    """The nearest repository frames above the refused call, for the record."""
    here = os.path.abspath(__file__)
    frames = [
        f"{os.path.relpath(frame.filename)}:{frame.lineno} {frame.name}"
        for frame in traceback.extract_stack()
        if frame.filename != here and _in_repository(frame.filename)
    ]
    return " <- ".join(reversed(frames[-3:])) or "(no repository frame)"


class NetworkGuard:
    """The policy and the record. Pure: patching is ``install``'s job."""

    def __init__(self, service_ports: Optional[Mapping[int, str]] = None) -> None:
        self.service_ports: dict[int, str] = dict(service_ports or {})
        self.bound_ports: set[int] = set()
        self._refusals: list[str] = []
        self._expected: list[list[str]] = []
        self._lock = threading.Lock()

    def note_bound(self, port: int) -> None:
        self.bound_ports.add(port)

    def check(self, host: object, port: object, via: str) -> None:
        """Raise ``NetworkAccessBlocked`` unless ``(host, port)`` is allowed."""
        try:
            number = int(port)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            number = None
        if not _is_local_host(host):
            reason = "it is not a loopback address"
        elif number is None:
            reason = "its port is not a number"
        elif number in self.bound_ports or number in self.service_ports:
            return
        else:
            reason = "no test in this session bound that port and no configured service uses it"
        raise self._refuse(host, port, number, reason, via)

    def check_address(self, address: object, via: str) -> None:
        """``check`` for a socket address: ``(host, port, ...)``."""
        if isinstance(address, tuple) and len(address) >= 2:
            self.check(address[0], address[1], via)
        elif isinstance(address, (bytes, str, tuple)):
            self.check(address, None, via)

    def _refuse(
        self, host: object, port: object, number: Optional[int], reason: str, via: str
    ) -> NetworkAccessBlocked:
        named = MODEL_SERVER_PORTS.get(number) if number is not None else None
        target = f"{host}:{port}" + (f" ({named}'s default port)" if named else "")
        allowed = ", ".join(
            f"{p} ({why})" for p, why in sorted(self.service_ports.items())
        )
        message = (
            f"tests may not open network connections: {via} to {target} was refused "
            f"because {reason}. A test may connect to a port it bound itself "
            f"({len(self.bound_ports)} so far) and to "
            f"{allowed or 'no configured service'}. Give the code under test a "
            f"double (respx, a fake transport, a server on a port the test binds), "
            f"not a real server. (A run whose purpose is to reach a provider sets "
            f"{ALLOW_LIVE_NETWORK_ENV}=1.)"
        )
        record = f"{target} via {via}, from {_where()}"
        with self._lock:
            (self._expected[-1] if self._expected else self._refusals).append(record)
        return NetworkAccessBlocked(message, host, port)

    def drain_refusals(self) -> list[str]:
        """The refusals recorded since the last drain, then forget them."""
        with self._lock:
            refusals, self._refusals = self._refusals, []
        return refusals

    @contextlib.contextmanager
    def expecting_refusals(self) -> Iterator[list[str]]:
        """Refusals inside the block are the test's subject, not a failure.

        Yields the list they are appended to, so the test can assert on it.
        """
        sink: list[str] = []
        with self._lock:
            self._expected.append(sink)
        try:
            yield sink
        finally:
            with self._lock:
                self._expected.remove(sink)


#: The installed guard, or None while none is (before ``install``, or when the
#: environment disabled it).
GUARD: Optional[NetworkGuard] = None
_installed = False


def install(environ: Optional[Mapping[str, str]] = None) -> Optional[NetworkGuard]:
    """Patch the process once; later calls return the same guard.

    Idempotent because ``tests/conftest.py`` is imported twice in a process
    (as ``conftest`` and as ``tests.conftest``) and both copies call this.
    """
    global GUARD, _installed
    if _installed:
        return GUARD
    _installed = True
    environ = os.environ if environ is None else environ
    if environ.get(ALLOW_LIVE_NETWORK_ENV):
        return None
    guard = GUARD = NetworkGuard(configured_service_ports(environ))

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_bind = socket.socket.bind
    real_create_connection = socket.create_connection
    real_loop_create_connection = asyncio.base_events.BaseEventLoop.create_connection
    real_sock_connect = asyncio.selector_events.BaseSelectorEventLoop.sock_connect

    def connect(self, address):
        if self.family in _INET_FAMILIES:
            guard.check_address(address, "socket.connect")
        return real_connect(self, address)

    def connect_ex(self, address):
        if self.family in _INET_FAMILIES:
            try:
                guard.check_address(address, "socket.connect_ex")
            except NetworkAccessBlocked:
                return errno.ECONNREFUSED
        return real_connect_ex(self, address)

    def bind(self, address):
        result = real_bind(self, address)
        if self.family in _INET_FAMILIES:
            with contextlib.suppress(OSError, IndexError):
                guard.note_bound(self.getsockname()[1])
        return result

    def create_connection(address, *args, **kwargs):
        guard.check_address(address, "socket.create_connection")
        return real_create_connection(address, *args, **kwargs)

    async def loop_create_connection(
        self, protocol_factory, host=None, port=None, **kwargs
    ):
        if host is not None and kwargs.get("sock") is None:
            guard.check(host, port, "loop.create_connection")
        return await real_loop_create_connection(
            self, protocol_factory, host, port, **kwargs
        )

    async def sock_connect(self, sock, address):
        if sock.family in _INET_FAMILIES:
            guard.check_address(address, "loop.sock_connect")
        return await real_sock_connect(self, sock, address)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.socket.bind = bind
    socket.create_connection = create_connection
    asyncio.base_events.BaseEventLoop.create_connection = loop_create_connection
    asyncio.selector_events.BaseSelectorEventLoop.sock_connect = sock_connect
    return guard


def drain_refusals() -> list[str]:
    """``NetworkGuard.drain_refusals`` of the installed guard (empty if none)."""
    return GUARD.drain_refusals() if GUARD is not None else []


def expecting_refusals() -> contextlib.AbstractContextManager[list[str]]:
    """``NetworkGuard.expecting_refusals`` of the installed guard."""
    if GUARD is None:
        raise RuntimeError("the network guard is not installed")
    return GUARD.expecting_refusals()
