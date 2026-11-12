"""tls_root_set.py

Purpose: the one writer of the process's TLS root set (§13.4, #2692).

Every TLS client in the process verifies the peer against one set: the
operating system's CA bundle, plus the CA certificates in ``TLS_EXTRA_CA_FILE``
when that setting is set. Each client reads its roots from a place of its own
(OpenSSL's default paths, certifi, gRPC's packaged ``roots.pem``, libpq's root
file), so ``apply_root_set`` names the set in every variable a client reads
(``OWNED_VARIABLES``) before any client is imported. It is the first statement
of every launcher and entrypoint: ``run_server``, each ``fm-*`` ``main()``, the
jobs runner and ``alembic/env.py``. Spawned children inherit the variables, and
a child that applies the set again reuses its parent's file.

The set is read once per start. With ``TLS_EXTRA_CA_FILE`` set, it is written to
a new file in a fresh temporary directory, which lives as long as the process
and its children and is never deleted by the process; a change to the setting
or to the OS bundle takes effect at the next start.

The start is refused (``SystemExit``, Fail-Fast Configuration) when no OS bundle
loads, when ``TLS_EXTRA_CA_FILE`` does not load, when the set's file cannot be
written or does not load whole, and when an owned variable is already set to
something other than the set (ruling 1 on #2692).

Imports only the standard library and the settings' dotenv loader, never a client.
"""

from __future__ import annotations

import hashlib
import os
import re
import ssl
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from faultmaven.config.settings import load_settings_dotenv

#: The operator's one setting: a PEM file of CA certificates added to the OS bundle.
EXTRA_CA_FILE_SETTING = "TLS_EXTRA_CA_FILE"

#: Each names the set's file. ``SSL_CERT_FILE``: OpenSSL's default file (the
#: stdlib, aiohttp, redis, asyncpg's ``ssl=True``), httpx, opik, litellm and
#: sentry-sdk. ``REQUESTS_CA_BUNDLE``, then ``CURL_CA_BUNDLE``: requests (which
#: reads the second when the first is unset). ``AWS_CA_BUNDLE``: botocore.
#: ``GRPC_DEFAULT_SSL_ROOTS_FILE_PATH``: gRPC, in place of its ``roots.pem``.
#: ``PGSSLROOTCERT``: asyncpg's and libpq's ``sslmode=require`` and ``verify-*``,
#: so ``require`` verifies the chain (ruling 2 on #2692).
FILE_VARIABLES = (
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "AWS_CA_BUNDLE",
    "GRPC_DEFAULT_SSL_ROOTS_FILE_PATH",
    "PGSSLROOTCERT",
)
#: Names a directory holding no certificate, so OpenSSL's default directory is
#: never a second root source (a CA only there would be trusted by the stdlib
#: and refused by httpx and requests).
DIRECTORY_VARIABLE = "SSL_CERT_DIR"
OWNED_VARIABLES = FILE_VARIABLES + (DIRECTORY_VARIABLE,)

#: Shipped in the package (``pyproject.toml`` package data); holds only a README.
EMPTY_CAPATH = str(Path(__file__).with_name("tls_empty_capath"))

#: Tried in order when OpenSSL's compiled default file does not load (a
#: uv-managed CPython names ``/etc/ssl/cert.pem``, absent on Debian).
DISTRIBUTION_BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/etc/ssl/ca-bundle.pem",
    "/etc/ssl/cert.pem",
)

_PEM_CERTIFICATE = re.compile(
    rb"-----BEGIN CERTIFICATE-----\s.*?-----END CERTIFICATE-----", re.DOTALL
)
#: OpenSSL's hashed-directory entry for a certificate (``.r0`` is a CRL).
_HASHED_ENTRY = re.compile(r"[0-9a-f]{8}\.\d+")


@dataclass(frozen=True)
class RootSet:
    """The set ``apply_root_set`` exported."""

    path: str
    sources: tuple[str, ...]
    extra_ca_file: str | None
    certificates: int


def apply_root_set() -> RootSet:
    """Name the one root set in every owned variable, or refuse the start."""
    load_settings_dotenv()
    os_source, os_pem, os_file = _resolve_os_bundle()
    extra = _extra_ca_file()
    pem = os_pem
    sources: tuple[str, ...] = (os_source,)
    if extra is not None:
        extra_pem = _load_extra(extra)
        # The newline: a bundle without a trailing one would merge its last
        # END line with the first BEGIN, and that set does not load.
        pem = os_pem + b"\n" + extra_pem
        sources += (extra,)
    digests = pem_digests(pem)
    inherited = _inherited_file(digests, extra)
    if inherited is not None:
        path = inherited
    elif extra is None and os_file:
        path = os_source
    else:
        path = _write_set(pem, digests)
    for name in FILE_VARIABLES:
        os.environ[name] = path
    os.environ[DIRECTORY_VARIABLE] = EMPTY_CAPATH
    return RootSet(path, sources, extra, len(digests))


def pem_digests(pem: bytes) -> frozenset[str]:
    """The SHA-256 of each certificate's DER, by its PEM blocks: order,
    comments and duplicates do not count."""
    digests = set()
    for block in _PEM_CERTIFICATE.findall(pem):
        try:
            der = ssl.PEM_cert_to_DER_cert(block.decode("ascii"))
        except (UnicodeDecodeError, ValueError):
            continue
        digests.add(hashlib.sha256(der).hexdigest())
    return frozenset(digests)


def _loaded_digests(path: str) -> frozenset[str]:
    """The certificates OpenSSL loads from ``path``; empty if it does not load."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    try:
        context.load_verify_locations(cafile=path)
    except (OSError, ssl.SSLError):
        return frozenset()
    return frozenset(
        hashlib.sha256(der).hexdigest() for der in context.get_ca_certs(True)
    )


def _read(path: str) -> bytes:
    try:
        return Path(path).read_bytes()
    except OSError:
        return b""


def _hashed_entries(directory: str) -> list[str]:
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    return sorted(name for name in names if _HASHED_ENTRY.fullmatch(name))


def _resolve_os_bundle() -> tuple[str, bytes, bool]:
    """(where the OS bundle came from, its PEM, whether that is a file to name).

    OpenSSL's default file if it loads, else the first distribution bundle that
    does, else the certificates in OpenSSL's default directory, joined.
    """
    paths = ssl.get_default_verify_paths()
    tried: list[str] = []
    for candidate in (paths.openssl_cafile, *DISTRIBUTION_BUNDLES):
        if not candidate or candidate in tried:
            continue
        tried.append(candidate)
        if _loaded_digests(candidate):
            return candidate, _read(candidate), True
    capath = paths.openssl_capath
    entries = _hashed_entries(capath) if capath else []
    pem = b"\n".join(_read(os.path.join(capath, name)) for name in entries)
    if pem_digests(pem):
        return capath, pem, False
    tried.append(f"{capath} (directory)")
    raise SystemExit(
        "TLS root set: no operating-system CA bundle loads; tried "
        + ", ".join(tried)
        + ". Install the OS CA bundle (on a python.org macOS build, run its "
        f"'Install Certificates' step). {EXTRA_CA_FILE_SETTING} only adds to it."
    )


def _extra_ca_file() -> str | None:
    """The setting as an absolute real path, written back so a child in
    another working directory reads the same file."""
    value = os.environ.get(EXTRA_CA_FILE_SETTING, "").strip()
    if not value:
        return None
    path = os.path.realpath(value)
    os.environ[EXTRA_CA_FILE_SETTING] = path
    return path


def _load_extra(path: str) -> bytes:
    pem = _read(path)
    if not _loaded_digests(path) or not pem_digests(pem):
        raise SystemExit(
            f"TLS root set: {EXTRA_CA_FILE_SETTING}={path!r} does not load as a "
            "PEM file of CA certificates."
        )
    return pem


def _inherited_file(digests: frozenset[str], extra: str | None) -> str | None:
    """The inherited file that holds the set, if any; refuse any owned
    variable that holds something else."""
    found = None
    for name in FILE_VARIABLES:
        value = os.environ.get(name)
        if not value:
            continue
        if pem_digests(_read(value)) != digests:
            _refuse(name, value, extra)
        found = found or value
    directory = os.environ.get(DIRECTORY_VARIABLE)
    if directory and _hashed_entries(directory):
        _refuse(DIRECTORY_VARIABLE, directory, extra)
    return found


def _refuse(name: str, value: str, extra: str | None) -> NoReturn:
    the_set = "the OS CA bundle" + (f" plus {EXTRA_CA_FILE_SETTING}" if extra else "")
    raise SystemExit(
        f"TLS root set: {name}={value!r} is set and does not hold the set's "
        f"certificates ({the_set}). FaultMaven names the set in {name} itself: "
        f"remove {name}, or put its CA certificates in {EXTRA_CA_FILE_SETTING}."
    )


def _write_set(pem: bytes, digests: frozenset[str]) -> str:
    try:
        directory = tempfile.mkdtemp(prefix="faultmaven-tls-")
        path = os.path.join(directory, "root-set.pem")
        Path(path).write_bytes(pem)
        os.chmod(path, 0o644)
    except OSError as error:
        raise SystemExit(
            f"TLS root set: cannot write the set's file ({error}); "
            "TMPDIR must name a writable directory."
        ) from error
    if _loaded_digests(path) != digests:
        raise SystemExit(
            f"TLS root set: the set written to {path} does not load all "
            f"{len(digests)} certificates; check {EXTRA_CA_FILE_SETTING} and "
            "the OS CA bundle."
        )
    return path
