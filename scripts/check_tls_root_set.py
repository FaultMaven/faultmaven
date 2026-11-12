#!/usr/bin/env python3
"""Every TLS client in §13.4's census verifies against the one root set (#2692).

A throwaway CA, written as ``TLS_EXTRA_CA_FILE``, signs a loopback leaf. Loopback
servers count completed handshakes (gRPC's, completed calls). Each client runs
in a fresh process, through its own SDK client or pool, retries off, twice:
**applied** (``apply_root_set()`` first) passes when it succeeds after a
handshake; **bare** (as before #2692) passes when it fails with none.
``sslmode=require`` verified nothing before, so its row also meets a CA outside
the set: applied refuses it, bare accepts it. A client not installed fails only
under ``--require-all``. Each client process refuses any traffic off loopback.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import importlib
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
NOT_INSTALLED, CHILD_TIMEOUT = 3, 90
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


def _url(ports: dict, path: str = "/probe") -> str:
    return f"https://{HOST}:{ports['https']}{path}"


def _reached(connect) -> None:
    """The Postgres responder's post-handshake error came back: TLS completed."""
    try:
        connect()
    except Exception as error:
        if MARKER in str(error):
            return
        raise
    raise RuntimeError("the responder never answered")


def _aiohttp(aiohttp, ports: dict) -> None:
    async def get() -> None:
        async with aiohttp.ClientSession() as session:
            async with session.get(_url(ports)) as response:
                await response.read()

    asyncio.run(get())


def _botocore(session, ports: dict) -> None:
    from botocore.config import Config

    config = Config(retries={"total_max_attempts": 1}, s3={"addressing_style": "path"})
    session.get_session().create_client(
        "s3",
        endpoint_url=_url(ports, ""),
        region_name="us-east-1",
        aws_access_key_id="probe",
        aws_secret_access_key="probe",
        config=config,
    ).head_bucket(Bucket="probe")


def _grpc(grpc, ports: dict) -> None:
    credentials, options = grpc.ssl_channel_credentials(), [("grpc.enable_retries", 0)]
    with grpc.secure_channel(f"{HOST}:{ports['grpc']}", credentials, options) as c:
        c.unary_unary("/probe.Probe/Call")(b"", timeout=10)


def _sqlalchemy_require(asyncio_engine, ports: dict) -> None:
    async def connect(port: int) -> None:
        url = f"postgresql+asyncpg://probe@{HOST}:{port}/probe?ssl=require"
        engine = asyncio_engine.create_async_engine(url, connect_args={"timeout": 10})
        try:
            async with engine.connect():
                pass
        finally:
            await engine.dispose()

    _reached(lambda: asyncio.run(connect(ports["pg"])))
    try:
        _reached(lambda: asyncio.run(connect(ports["outside"])))
    except Exception:
        return
    raise RuntimeError("accepted a peer outside the set: require did not verify")


def _sentry(sentry_sdk, ports: dict) -> None:
    sentry_sdk.init(
        dsn=f"https://p@{HOST}:{ports['https']}/1", default_integrations=False
    )
    client = sentry_sdk.get_client()
    try:
        client.transport._pool.request("GET", _url(ports), retries=False)
    finally:
        client.close(timeout=0)


def _httpx_roots(httpx, ports: dict) -> None:
    ours = set(httpx.create_ssl_context().get_ca_certs(True))
    stdlib = set(ssl.create_default_context().get_ca_certs(True))
    if ours != stdlib:
        raise RuntimeError(
            f"httpx holds {len(ours - stdlib)} roots the stdlib context does not, "
            f"and lacks {len(stdlib - ours)}"
        )


def _verify_full(port: int) -> str:
    return f"postgresql://probe@{HOST}:{port}/probe?sslmode=verify-full"


# name: (module the client is given, its server, client(module, ports)).
# The equality row has no server and no bare run: certifi can hold exactly the
# OS bundle's roots on a host (it does on the one #2692 was built on), so a bare
# run is not guaranteed to differ.
ROWS = {
    "stdlib": (
        "urllib.request",
        "https",
        lambda m, p: m.urlopen(_url(p), timeout=10).read(),
    ),
    "aiohttp": ("aiohttp", "https", _aiohttp),
    "httpx": ("httpx", "https", lambda m, p: m.get(_url(p), timeout=10)),
    "requests": ("requests", "https", lambda m, p: m.get(_url(p), timeout=10)),
    "botocore": ("botocore.session", "https", _botocore),
    "redis": (
        "redis",
        "https",
        lambda m, p: m.Redis(
            HOST, p["https"], ssl=True, socket_timeout=10,
            retry=m.retry.Retry(m.backoff.NoBackoff(), 0),
        ).ping(),
    ),  # fmt: skip
    "grpc": ("grpc", "grpc", _grpc),
    "asyncpg verify-full": (
        "asyncpg",
        "pg",
        lambda m, p: _reached(
            lambda: asyncio.run(m.connect(_verify_full(p["pg"]), timeout=10))
        ),
    ),
    "sqlalchemy asyncpg require": ("sqlalchemy.ext.asyncio", "pg", _sqlalchemy_require),
    "psycopg2 verify-full": (
        "psycopg2",
        "pg",
        lambda m, p: _reached(
            lambda: m.connect(
                _verify_full(p["pg"]), gssencmode="disable", connect_timeout=10
            )
        ),
    ),
    "litellm": (
        "litellm.llms.custom_httpx.http_handler",
        "https",
        lambda m, p: m.HTTPHandler(timeout=10).get(_url(p)),
    ),
    "sentry-sdk": ("sentry_sdk", "https", _sentry),
    "workos": (
        "workos",
        "https",
        lambda m, p: m.WorkOSClient(
            api_key="p", client_id="p", base_url=_url(p, ""), max_retries=0
        ).request("GET", ["probe"]),
    ),
    "opik": (
        "opik.httpx_client",
        "https",
        lambda m, p: m.get(None, None, True, False).get(_url(p)),
    ),
    "httpx roots = stdlib roots": ("httpx", None, _httpx_roots),
}
# A row needs these installed besides its module's top-level package.
_ALSO_NEEDS = {"sqlalchemy asyncpg require": "asyncpg"}


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
    module, _, client = ROWS[name]
    # Top-level names only: find_spec of a dotted name imports its parents.
    for needed in (module.split(".")[0], _ALSO_NEEDS.get(name)):
        if needed and importlib.util.find_spec(needed) is None:
            return NOT_INSTALLED
    _loopback_only()
    if applied:
        from faultmaven.config.tls_root_set import apply_root_set

        apply_root_set()
    try:
        client(importlib.import_module(module), ports)
    except BaseException as error:
        print(f"refused: {type(error).__name__}: {error}"[:300])
        return 1
    return 0


def _serve_tls(context: ssl.SSLContext, postgres: bool) -> tuple[socket.socket, list]:
    """A listener answering HTTP and Redis, or PostgreSQL; the list grows by one
    per completed handshake."""
    listener, handshakes = socket.create_server((HOST, 0)), []

    def answer(connection: socket.socket) -> None:
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
            commands = data.split(b"\r\n*")
            tls.sendall(
                b"".join(b"+PONG\r\n" if b"PING" in c else b"+OK\r\n" for c in commands)
            )
            data = tls.recv(65536)
        while data and b"\r\n\r\n" not in data:
            data += tls.recv(65536)
        head = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n"
        tls.sendall(head + (b"" if data.startswith(b"HEAD") else b"{}"))

    def serve(connection: socket.socket) -> None:
        with connection:
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
    from concurrent import futures

    import grpc

    calls: list = []
    method = grpc.unary_unary_rpc_method_handler(lambda _, __: calls.append(1) or b"")
    handler = grpc.method_handlers_generic_handler("probe.Probe", {"Call": method})
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2), handlers=[handler])
    port = server.add_secure_port(
        f"{HOST}:0", grpc.ssl_server_credentials([(key, chain)])
    )
    server.start()
    return server, port, calls


def _certificates(directory: Path, name: str):
    """A CA (written to ``<name>-ca.pem``) and a leaf it signs: the CA file, a
    server context presenting the leaf, and the leaf's PEM chain and key."""
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
    files = [directory / f"{name}-{part}.pem" for part in ("ca", "chain", "key")]
    chain = leaf.public_bytes(pem) + ca.public_bytes(pem)
    key = leaf_key.private_bytes(
        pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    for file, content in zip(files, (ca.public_bytes(pem), chain, key)):
        file.write_bytes(content)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(files[1], files[2])
    return files[0], context, chain, key


def _run(
    name: str, applied: bool, ports: dict, counts: dict, env: dict
) -> tuple[str, str]:
    """(verdict, detail) for one client in one mode."""
    server = ROWS[name][1]
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
    else:  # the require row's bare run completes a handshake with the outside CA
        ok = code != 0 and (delta == 0 or "require" in name)
    return ("PASS" if ok else "FAIL"), f"{delta} handshake(s); {said}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--require-all", action="store_true", help="fail on a client not installed"
    )
    for hidden in ("--child", "--ports"):
        parser.add_argument(hidden, help=argparse.SUPPRESS)
    parser.add_argument("--bare", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.child:
        return _child(args.child, not args.bare, json.loads(args.ports))
    from faultmaven.config.tls_root_set import OWNED_VARIABLES

    failed, ports, counts, listeners, grpc_server = False, {}, {}, [], None
    with tempfile.TemporaryDirectory(prefix="check-tls-root-set-") as scratch:
        ca_file, context, chain, key = _certificates(Path(scratch), "set")
        outside = _certificates(Path(scratch), "outside")[1]
        for server, server_context, postgres in (
            ("https", context, False), ("pg", context, True), ("outside", outside, True)
        ):  # fmt: skip
            listener, counts[server] = _serve_tls(server_context, postgres)
            ports[server] = listener.getsockname()[1]
            listeners.append(listener)
        if importlib.util.find_spec("grpc") is not None:
            grpc_server, ports["grpc"], counts["grpc"] = _serve_grpc(chain, key)
        env = {
            name: value
            for name, value in os.environ.items()
            if name not in OWNED_VARIABLES and name.lower() not in _PROXY_VARIABLES
        }
        env.update(_CHILD_ENV, TLS_EXTRA_CA_FILE=str(ca_file))
        for name in ROWS:
            for applied in (True, False) if ROWS[name][1] else (True,):
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
