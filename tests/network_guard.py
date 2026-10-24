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
3. Resolving a name that is not this machine's is refused, before the lookup.
   ``localhost`` (and names under ``.localhost``) and this machine's own
   hostname, short or qualified, are this machine's. An address is not a name:
   nothing is looked up, and the connect that follows is held to rules 1 and 2.
4. A refusal raises ``NetworkAccessBlocked`` (a ``ConnectionRefusedError``, so
   code written for an unreachable server takes the path it always took) or,
   for a lookup, ``NameResolutionBlocked`` (a ``socket.gaierror``, what a name
   that does not resolve raises), names the host, and is RECORDED. The record
   fails the test (see ``drain_refusals`` and ``pytest_runtest_makereport`` in
   ``tests/conftest.py``) even when the code under test caught the exception:
   an application that treats "provider unreachable" as a fallback would
   otherwise turn the refused connect into a green test, which is the
   situation this exists to end.

Why lookups are refused (#2232). A lookup that fails ends the attempt before
any connection, so the guard never saw it: three tests that composed the app
health-checked Presidio at its shipped URLs, passed in CI because
``*.faultmaven.local`` does not resolve there, and failed on a box whose
resolver answers, where the guard refused the connect. Whether a test passed
depended on DNS, and the lookup was itself traffic to a server the test did not
start. Refused before the lookup, the name fails the test wherever it runs.
Measured before this rule, over a full run under each CI profile (26,073
tests and the 316 Python processes they start): two names that are not this
machine's were resolved, 24 times, all of them Presidio's, and nothing looked
up this machine's own name. Such a lookup (``getaddrinfo(gethostname())``)
stays allowed all the same: it is about this machine, not a step toward a
server. Reverse lookups (``gethostbyaddr``, which ``getfqdn`` makes, and
``getnameinfo``) lead to no connection and are left alone.

A test that needs to prove its code survives a refused connection binds a port
and closes it (the port is then its own, and the kernel refuses for real), or
wraps the call in ``expecting_refusals``. A test of "server unreachable" hands
the code under test that loopback port, never a name that does not resolve.

Patched paths. Connects: ``socket.socket.connect`` / ``connect_ex`` (every
stdlib and third-party client ends here, ``ssl`` and asyncio's selector loops
included), ``socket.create_connection``, and asyncio's ``create_connection`` /
``sock_connect``, which are checked before name resolution. Lookups:
``socket.getaddrinfo`` / ``gethostbyname`` / ``gethostbyname_ex`` (urllib3,
anyio and aiohttp's threaded resolver end here), asyncio's ``getaddrinfo``,
checked in the caller before it hands the lookup to a worker thread so the
record names the caller, and c-ares' ``pycares.Channel`` lookups: aiohttp's
default resolver when aiodns is installed, as the lockfile has it, and c-ares
resolves below the ``socket`` module. Not covered: a child process (it has its
own interpreter), uvloop's native connect, UDP ``sendto`` with no ``connect``,
and dnspython's resolver (its own UDP; email-validator's deliverability check is
its one user here, and pydantic's ``EmailStr`` turns that check off).

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


class NameResolutionBlocked(socket.gaierror):
    """A test process tried to resolve a name that is not this machine's."""

    def __init__(self, message: str, host: object) -> None:
        super().__init__(socket.EAI_NONAME, message)
        self.host = host


def _name(host: object) -> Optional[str]:
    """``host`` lowercased, without an IPv6 zone or a trailing dot; None if not text."""
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if not isinstance(host, str):
        return None
    return host.split("%", 1)[0].lower().rstrip(".")


def _address(name: str) -> Optional[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        return ipaddress.ip_address(name)
    except ValueError:
        return None


def _is_local_host(host: object) -> bool:
    """Whether ``host`` is this machine, decided without a DNS lookup.

    ``localhost`` and the loopback addresses are; so is the unspecified address
    (``0.0.0.0`` / ``::`` / ``''``), because connecting to it reaches a local
    listener -- ``connect(("0.0.0.0", 11434))`` is Ollama. Any other NAME is
    not local: resolving it would itself be the outbound traffic.
    """
    name = _name(host)
    if name is None:
        return False
    if name in ("", "localhost") or name.endswith(".localhost"):
        return True
    address = _address(name)
    if address is None:
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

    def __init__(
        self, service_ports: Optional[Mapping[int, str]] = None, hostname: str = ""
    ) -> None:
        self.service_ports: dict[int, str] = dict(service_ports or {})
        #: This machine's name (``install`` passes ``socket.gethostname()``):
        #: a lookup of it, short or qualified, is about this machine.
        self.hostname = _name(hostname) or ""
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
        elif number in MODEL_SERVER_PORTS:
            # Before the bind record: a test that binds a model server's port
            # and connects to it would otherwise be admitted (on a box where
            # that port is free).
            reason = "it is a model server's port, which no test may reach"
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

    def check_lookup(self, host: object, via: str) -> None:
        """Raise ``NameResolutionBlocked`` unless ``host`` is this machine's name.

        ``None`` (a passive lookup) and anything that is not text pass, to the
        resolver's own answer; so does an address, which names no lookup.
        """
        name = _name(host)
        if (
            name is None
            or _is_local_host(name)
            or _address(name) is not None
            or self._is_this_machine(name)
        ):
            return
        message = (
            f"tests may not resolve names that are not this machine's: {via} of "
            f"{host!r} was refused. Where a name does not resolve, the lookup "
            f"ends the attempt before any connection, so the test would pass or "
            f"fail on whether DNS answers (#2232). Give the code under test a "
            f"loopback port the test bound (closed, for an unreachable server) "
            f"or a double (respx, a fake transport), not a server's name. (A run "
            f"whose purpose is to reach a provider sets "
            f"{ALLOW_LIVE_NETWORK_ENV}=1.)"
        )
        self._record(f"{host} (name lookup) via {via}, from {_where()}")
        raise NameResolutionBlocked(message, host)

    def _is_this_machine(self, name: str) -> bool:
        if not self.hostname:
            return False
        short = self.hostname.split(".", 1)[0]
        return name in (self.hostname, short) or name.startswith(short + ".")

    def _record(self, record: str) -> None:
        with self._lock:
            (self._expected[-1] if self._expected else self._refusals).append(record)

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
        self._record(f"{target} via {via}, from {_where()}")
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
    guard = GUARD = NetworkGuard(
        configured_service_ports(environ), hostname=socket.gethostname()
    )

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_bind = socket.socket.bind
    real_create_connection = socket.create_connection
    real_loop_create_connection = asyncio.base_events.BaseEventLoop.create_connection
    real_sock_connect = asyncio.selector_events.BaseSelectorEventLoop.sock_connect
    real_getaddrinfo = socket.getaddrinfo
    real_gethostbyname = socket.gethostbyname
    real_gethostbyname_ex = socket.gethostbyname_ex
    real_loop_getaddrinfo = asyncio.base_events.BaseEventLoop.getaddrinfo

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

    def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        guard.check_lookup(host, "socket.getaddrinfo")
        return real_getaddrinfo(host, port, family, type, proto, flags)

    def gethostbyname(hostname):
        guard.check_lookup(hostname, "socket.gethostbyname")
        return real_gethostbyname(hostname)

    def gethostbyname_ex(hostname):
        guard.check_lookup(hostname, "socket.gethostbyname_ex")
        return real_gethostbyname_ex(hostname)

    async def loop_getaddrinfo(self, host, port, **kwargs):
        guard.check_lookup(host, "loop.getaddrinfo")
        return await real_loop_getaddrinfo(self, host, port, **kwargs)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.socket.bind = bind
    socket.create_connection = create_connection
    asyncio.base_events.BaseEventLoop.create_connection = loop_create_connection
    asyncio.selector_events.BaseSelectorEventLoop.sock_connect = sock_connect
    socket.getaddrinfo = getaddrinfo
    socket.gethostbyname = gethostbyname
    socket.gethostbyname_ex = gethostbyname_ex
    asyncio.base_events.BaseEventLoop.getaddrinfo = loop_getaddrinfo
    _guard_c_ares(guard)
    return guard


def _guard_c_ares(guard: NetworkGuard) -> None:
    """Hold c-ares' forward lookups to rule 3, where c-ares is installed.

    c-ares resolves in C, below the ``socket`` module, so the patches above
    never see it; it is what aiohttp resolves with by default once aiodns is
    importable. The refusal is raised synchronously from the ``Channel`` call:
    aiodns lets it through (it reroutes only ``AresError``) and aiohttp reports
    it as the DNS failure (``ClientConnectorDNSError``) it is.
    """
    try:
        import pycares
    except ImportError:
        return

    def lookup(real, via):
        def guarded(self, name, *args, **kwargs):
            guard.check_lookup(name, via)
            return real(self, name, *args, **kwargs)

        return guarded

    for method in ("getaddrinfo", "query", "search"):
        real = getattr(pycares.Channel, method)
        setattr(pycares.Channel, method, lookup(real, f"pycares.Channel.{method}"))


def drain_refusals() -> list[str]:
    """``NetworkGuard.drain_refusals`` of the installed guard (empty if none)."""
    return GUARD.drain_refusals() if GUARD is not None else []


def expecting_refusals() -> contextlib.AbstractContextManager[list[str]]:
    """``NetworkGuard.expecting_refusals`` of the installed guard."""
    if GUARD is None:
        raise RuntimeError("the network guard is not installed")
    return GUARD.expecting_refusals()
