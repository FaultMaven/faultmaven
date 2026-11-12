#!/usr/bin/env python3
"""Every TLS client in §13.4's census verifies against the one root set (#2692).

A throwaway CA signs a ``localhost``/``127.0.0.1`` leaf and is written as
``TLS_EXTRA_CA_FILE``. Loopback servers present the leaf and count completed
handshakes (gRPC's counts completed calls): HTTPS and Redis on one port, and a
PostgreSQL ``SSLRequest`` responder. A second responder presents a leaf from a
CA outside the set.

Each client runs twice, in a fresh process with no owned variable inherited.
**applied** calls ``apply_root_set()`` first, and passes when the client
succeeds and its server completed a handshake. **bare** does not (the
behaviour before #2692), and passes when the client fails with no handshake;
``sslmode=require`` verified nothing then, so its bare run passes when it fails
at all (it accepts the peer outside the set). Each client goes through its own
SDK client or pool, retries off. One more row, applied only, shows httpx's
context holds exactly the stdlib's roots, so certifi's extra roots are gone
(#2574); certifi may equal the OS bundle on a given host, so it has no bare run.

A client not installed is reported, and fails the run only under
``--require-all``. Exit 1 on any failure. A client process refuses any connect
or name lookup off loopback.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import importlib.util
import ipaddress
import json
import os
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if (ROOT / "faultmaven").is_dir():
    sys.path.insert(0, str(ROOT))

HOST = "127.0.0.1"
MARKER = "tls-root-set-probe reached"
NOT_INSTALLED = 3
CHILD_TIMEOUT = 90
# The client processes': no proxy, no SDK telemetry, download or cloud lookup.
_CHILD_ENV = {
    "FAULTMAVEN_SKIP_DOTENV": "1",
    "LITELLM_LOCAL_MODEL_COST_MAP": "True",
    "OPIK_TRACK_DISABLE": "true",
    "OPIK_SENTRY_ENABLE": "false",
    "AWS_EC2_METADATA_DISABLED": "true",
    "HF_HUB_OFFLINE": "1",
}
_PROXY_VARIABLES = {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}


def _url(ports: dict) -> str:
    return f"https://{HOST}:{ports['https']}/probe"


def _stdlib(ports: dict) -> None:
    import urllib.request

    urllib.request.urlopen(_url(ports), timeout=10).read()


def _aiohttp(ports: dict) -> None:
    import aiohttp

    async def get() -> None:
        async with aiohttp.ClientSession() as session:
            async with session.get(_url(ports)) as response:
                await response.read()

    asyncio.run(get())


def _httpx(ports: dict) -> None:
    import httpx

    httpx.get(_url(ports), timeout=10)


def _requests(ports: dict) -> None:
    import requests

    requests.get(_url(ports), timeout=10)


def _botocore(ports: dict) -> None:
    import botocore.session
    from botocore.config import Config

    config = Config(retries={"total_max_attempts": 1}, s3={"addressing_style": "path"})
    botocore.session.get_session().create_client(
        "s3",
        endpoint_url=f"https://{HOST}:{ports['https']}",
        region_name="us-east-1",
        aws_access_key_id="probe",
        aws_secret_access_key="probe",
        config=config,
    ).head_bucket(Bucket="probe")


def _redis(ports: dict) -> None:
    import redis
    from redis.backoff import NoBackoff
    from redis.retry import Retry

    retry = Retry(NoBackoff(), 0)
    redis.Redis(HOST, ports["https"], ssl=True, socket_timeout=10, retry=retry).ping()


def _grpc(ports: dict) -> None:
    import grpc

    credentials, options = grpc.ssl_channel_credentials(), [("grpc.enable_retries", 0)]
    with grpc.secure_channel(
        f"{HOST}:{ports['grpc']}", credentials, options
    ) as channel:
        channel.unary_unary("/probe.Probe/Call")(b"", timeout=10)


def _reached(connect) -> None:
    """The responder's post-handshake error came back: TLS completed."""
    try:
        connect()
    except Exception as error:
        if MARKER not in str(error):
            raise
        return
    raise RuntimeError("the responder never answered")


def _asyncpg_verify_full(ports: dict) -> None:
    import asyncpg

    dsn = f"postgresql://probe@{HOST}:{ports['pg']}/probe?sslmode=verify-full"
    _reached(lambda: asyncio.run(asyncpg.connect(dsn, timeout=10)))


def _sqlalchemy_asyncpg_require(ports: dict) -> None:
    import asyncpg  # noqa: F401  (not installed without it)
    from sqlalchemy.ext.asyncio import create_async_engine

    async def connect(port: int) -> None:
        url = f"postgresql+asyncpg://probe@{HOST}:{port}/probe?ssl=require"
        engine = create_async_engine(url, connect_args={"timeout": 10})
        try:
            async with engine.connect():
                pass
        finally:
            await engine.dispose()

    _reached(lambda: asyncio.run(connect(ports["pg"])))
    try:
        _reached(lambda: asyncio.run(connect(ports["stranger"])))
    except Exception:
        return
    raise RuntimeError("accepted a peer outside the set: require did not verify")


def _psycopg2(ports: dict) -> None:
    import psycopg2

    dsn = f"host={HOST} port={ports['pg']} user=probe dbname=probe gssencmode=disable"
    _reached(lambda: psycopg2.connect(dsn, sslmode="verify-full", connect_timeout=10))


def _litellm(ports: dict) -> None:
    from litellm.llms.custom_httpx.http_handler import HTTPHandler

    HTTPHandler(timeout=10).get(_url(ports))


def _sentry(ports: dict) -> None:
    import sentry_sdk

    dsn = f"https://probe@{HOST}:{ports['https']}/1"
    sentry_sdk.init(dsn=dsn, default_integrations=False)
    client = sentry_sdk.get_client()
    try:
        client.transport._pool.request("GET", _url(ports), retries=False)
    finally:
        client.close(timeout=0)


def _workos(ports: dict) -> None:
    from workos import WorkOSClient

    base_url = f"https://{HOST}:{ports['https']}"
    client = WorkOSClient(api_key="p", client_id="p", base_url=base_url, max_retries=0)
    client.request("GET", ["probe"])


def _opik(ports: dict) -> None:
    from opik import httpx_client

    httpx_client.get(None, None, True, False).get(_url(ports))


def _httpx_roots(ports: dict) -> None:
    import httpx

    httpx_roots = set(httpx.create_ssl_context().get_ca_certs(True))
    stdlib_roots = set(ssl.create_default_context().get_ca_certs(True))
    if httpx_roots != stdlib_roots:
        raise RuntimeError(
            f"httpx holds {len(httpx_roots - stdlib_roots)} roots the stdlib "
            f"context does not, and lacks {len(stdlib_roots - httpx_roots)}"
        )


# name: (client, the module it needs, its server, bare is refused pre-handshake)
ROWS = {
    "stdlib": (_stdlib, "ssl", "https", True),
    "aiohttp": (_aiohttp, "aiohttp", "https", True),
    "httpx": (_httpx, "httpx", "https", True),
    "requests": (_requests, "requests", "https", True),
    "botocore": (_botocore, "botocore", "https", True),
    "redis": (_redis, "redis", "https", True),
    "grpc": (_grpc, "grpc", "grpc", True),
    "asyncpg verify-full": (_asyncpg_verify_full, "asyncpg", "pg", True),
    "sqlalchemy asyncpg require": (_sqlalchemy_asyncpg_require, "asyncpg", "pg", False),
    "psycopg2 verify-full": (_psycopg2, "psycopg2", "pg", True),
    "litellm": (_litellm, "litellm", "https", True),
    "sentry-sdk": (_sentry, "sentry_sdk", "https", True),
    "workos": (_workos, "workos", "https", True),
    "opik": (_opik, "opik", "https", True),
    "httpx roots = stdlib roots": (_httpx_roots, "httpx", None, True),
}


def _loopback_only() -> None:
    allowed = {None, HOST, "::1", "localhost", HOST.encode(), b"localhost"}
    real_connect, real_lookup = socket.socket.connect, socket.getaddrinfo

    def connect(self, address):
        if (
            self.family in (socket.AF_INET, socket.AF_INET6)
            and address[0] not in allowed
        ):
            raise OSError(f"check_tls_root_set: refused connecting to {address[0]}")
        return real_connect(self, address)

    def lookup(host, *args, **kwargs):
        if host not in allowed:
            raise socket.gaierror(f"check_tls_root_set: refused resolving {host!r}")
        return real_lookup(host, *args, **kwargs)

    socket.socket.connect, socket.getaddrinfo = connect, lookup


def _child(name: str, applied: bool, ports: dict) -> int:
    client, module, _, _ = ROWS[name]
    if importlib.util.find_spec(module) is None:
        return NOT_INSTALLED
    _loopback_only()
    if applied:
        from faultmaven.config.tls_root_set import apply_root_set

        apply_root_set()
    try:
        client(ports)
    except BaseException as error:
        print(f"refused: {type(error).__name__}: {error}"[:300])
        return 1
    return 0


def _serve_tls(context: ssl.SSLContext, postgres: bool) -> tuple[socket.socket, list]:
    """A listener answering HTTP, Redis or PostgreSQL; the list grows by one per
    completed handshake."""
    listener, handshakes = socket.create_server((HOST, 0)), []

    def answer(connection: socket.socket) -> None:
        with connection:
            connection.settimeout(30)
            while postgres:  # SSLRequest gets "S"; GSSENCRequest "N"
                code = struct.unpack("!ii", connection.recv(8, socket.MSG_WAITALL))[1]
                connection.sendall(b"S" if code == 80877103 else b"N")
                if code != 80877104:
                    break
            tls = context.wrap_socket(connection, server_side=True)
            handshakes.append(1)
            data = tls.recv(65536)
            if postgres:  # an ErrorResponse to the StartupMessage
                fields = b"SFATAL\0VFATAL\0C28000\0M" + MARKER.encode() + b"\0\0"
                tls.sendall(b"E" + struct.pack("!i", len(fields) + 4) + fields)
                return
            while data.startswith(b"*"):  # Redis: CLIENT SETINFO, then PING
                replies = [
                    b"+PONG\r\n" if b"PING" in c else b"+OK\r\n"
                    for c in data.split(b"\r\n*")
                ]
                tls.sendall(b"".join(replies))
                data = tls.recv(65536)
            while data and b"\r\n\r\n" not in data:
                data += tls.recv(65536)
            head = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n"
            tls.sendall(head + (b"" if data.startswith(b"HEAD") else b"{}"))

    def serve(connection: socket.socket) -> None:
        try:
            answer(connection)
        except (OSError, ssl.SSLError, struct.error):
            pass

    def accept() -> None:
        while True:
            try:
                connection = listener.accept()[0]
            except OSError:
                return
            threading.Thread(target=serve, args=(connection,), daemon=True).start()

    threading.Thread(target=accept, daemon=True).start()
    return listener, handshakes


def _serve_grpc(chain: bytes, key: bytes):
    """A gRPC TLS server, its port, and the list of completed calls."""
    from concurrent import futures

    import grpc

    calls: list = []
    method = grpc.unary_unary_rpc_method_handler(
        lambda request, _: calls.append(1) or b""
    )
    handler = grpc.method_handlers_generic_handler("probe.Probe", {"Call": method})
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2), handlers=[handler])
    port = server.add_secure_port(
        f"{HOST}:0", grpc.ssl_server_credentials([(key, chain)])
    )
    server.start()
    return server, port, calls


def _certificates(directory: Path, name: str) -> tuple[Path, bytes, bytes]:
    """A CA written to ``<name>-ca.pem``, and the PEM chain and key of a leaf it signs."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key, leaf_key = (ec.generate_private_key(ec.SECP256R1()) for _ in range(2))
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"{name} probe CA")])

    def build(subject, key, ca: bool, *extensions):
        builder = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(ca_name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(hours=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=ca, path_length=None), True)
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False
            )
        )
        for extension in extensions:
            builder = builder.add_extension(extension, False)
        return builder.sign(ca_key, hashes.SHA256())

    ca = build(ca_name, ca_key, True, x509.KeyUsage(
        True, False, False, False, False, True, True, False, False
    ))  # fmt: skip
    leaf = build(
        x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]),
        leaf_key,
        False,
        x509.SubjectAlternativeName(
            [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address(HOST))]
        ),
        x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
        x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
    )
    pem = serialization.Encoding.PEM
    ca_file = directory / f"{name}-ca.pem"
    ca_file.write_bytes(ca.public_bytes(pem))
    key = leaf_key.private_bytes(
        pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    return ca_file, leaf.public_bytes(pem) + ca.public_bytes(pem), key


def _server_context(
    directory: Path, name: str, chain: bytes, key: bytes
) -> ssl.SSLContext:
    (directory / f"{name}-chain.pem").write_bytes(chain)
    (directory / f"{name}-key.pem").write_bytes(key)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(
        directory / f"{name}-chain.pem", directory / f"{name}-key.pem"
    )
    return context


def _run(
    name: str, applied: bool, ports: dict, counts: dict, env: dict
) -> tuple[str, str]:
    """(verdict, detail) for one client in one mode."""
    server, refused_bare = ROWS[name][2:]
    before = len(counts[server]) if server else 0
    command = [sys.executable, __file__, "--child", name, "--ports", json.dumps(ports)]
    try:
        result = subprocess.run(
            command + ([] if applied else ["--bare"]),
            env=env, capture_output=True, text=True, timeout=CHILD_TIMEOUT,
        )  # fmt: skip
        code, lines = result.returncode, (result.stdout + result.stderr).splitlines()
    except subprocess.TimeoutExpired:
        code, lines = 1, [f"refused: timed out after {CHILD_TIMEOUT}s"]
    if code == NOT_INSTALLED:
        return "NOT INSTALLED", ""
    said = next((line for line in lines if line.startswith("refused: ")), None)
    said = said or (lines[-1] if code and lines else "ok")
    if server is None:
        return ("PASS" if code == 0 else "FAIL"), said
    delta = len(counts[server]) - before
    if applied:
        ok = code == 0 and delta >= 1
    else:
        ok = code != 0 and (delta == 0 or not refused_bare)
    return ("PASS" if ok else "FAIL"), f"{delta} handshake(s); {said}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="TLS clients verify against the root set."
    )
    parser.add_argument(
        "--require-all", action="store_true", help="fail on a client not installed"
    )
    parser.add_argument("--child", help=argparse.SUPPRESS)
    parser.add_argument("--ports", help=argparse.SUPPRESS)
    parser.add_argument("--bare", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.child:
        return _child(args.child, not args.bare, json.loads(args.ports))
    from faultmaven.config.tls_root_set import OWNED_VARIABLES

    failed, ports, counts, listeners = False, {}, {}, []
    with tempfile.TemporaryDirectory(prefix="check-tls-root-set-") as scratch:
        directory = Path(scratch)
        ca_file, chain, key = _certificates(directory, "set")
        context = _server_context(directory, "set", chain, key)
        stranger = _server_context(
            directory, "stranger", *_certificates(directory, "stranger")[1:]
        )
        for server, server_context, postgres in (
            ("https", context, False), ("pg", context, True), ("stranger", stranger, True)
        ):  # fmt: skip
            listener, counts[server] = _serve_tls(server_context, postgres)
            ports[server] = listener.getsockname()[1]
            listeners.append(listener)
        grpc_server = None
        if importlib.util.find_spec("grpc") is not None:
            grpc_server, ports["grpc"], counts["grpc"] = _serve_grpc(chain, key)
        env = {
            name: value
            for name, value in os.environ.items()
            if name not in OWNED_VARIABLES and name.lower() not in _PROXY_VARIABLES
        }
        env.update(_CHILD_ENV, TLS_EXTRA_CA_FILE=str(ca_file))
        for name in ROWS:
            for applied in (True, False) if ROWS[name][2] else (True,):
                verdict, detail = _run(name, applied, ports, counts, env)
                failed |= verdict == "FAIL" or (verdict != "PASS" and args.require_all)
                mode = "applied" if applied else "bare"
                print(f"{name:<28} {mode:<8} {verdict:<14} {detail}", flush=True)
        if grpc_server is not None:
            grpc_server.stop(None)
        for listener in listeners:
            listener.close()
    print("FAIL" if failed else "OK: every installed client verifies against the set")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
