"""tls_root_set.py

Purpose: the one writer of the process's TLS root set (§13.4, #2692): the OS CA
bundle, plus the certificates in ``TLS_EXTRA_CA_FILE`` when set.

Clients read roots from places of their own (OpenSSL's default paths, certifi,
gRPC's ``roots.pem``, libpq's root file), so ``apply_root_set`` names the set in
every variable one reads (``OWNED_VARIABLES``) before any client is imported: it
is the first statement of ``run_server``, each ``fm-*`` ``main()``, the jobs
runner, ``alembic/env.py`` and the demo seeder. Children inherit the variables.

A certificate is a PEM block labelled ``CERTIFICATE``. When a set file is
written (``TLS_EXTRA_CA_FILE`` set, or an OS bundle built from the capath) it is
the OS bundle, a newline, then the setting's certificates re-encoded (never its
keys or other blocks), at ``<tmp>/faultmaven-tls-<uid>/<sha256>.pem``: one file
per content, reused by every start that computes it, never deleted. The set is
read once per start, so a change takes effect at the next start.

An inherited owned variable is the set when it names the file this process would
name, holds the bytes it would write, or holds only plain certificates with
exactly the set's digests (an inherited ``SSL_CERT_DIR``: no hashed entry, or
hashed entries with exactly those digests). Anything else refuses the start
(ruling 1 on #2692), as do an OS bundle that does not load, a setting with no
certificate or a non-plain one, and a set file that cannot be written safely or
does not load whole (``SystemExit``, Fail-Fast Configuration). Imports no client.
"""

from __future__ import annotations

import hashlib
import os
import re
import ssl
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from faultmaven.config.settings import (
    env_var_spellings,
    load_settings_dotenv,
    set_env_var,
)

#: The operator's one setting: a PEM file of CA certificates added to the OS bundle.
EXTRA_CA_FILE_SETTING = "TLS_EXTRA_CA_FILE"

#: Each names the set's file. ``SSL_CERT_FILE``: OpenSSL's default file (the
#: stdlib, aiohttp, redis, asyncpg's ``ssl=True``), httpx, opik, litellm and
#: sentry-sdk. ``REQUESTS_CA_BUNDLE``, then ``CURL_CA_BUNDLE``: requests (which
#: reads the second when the first is unset). ``AWS_CA_BUNDLE``: botocore.
#: ``GRPC_DEFAULT_SSL_ROOTS_FILE_PATH``: gRPC, in place of its ``roots.pem``.
#: ``PGSSLROOTCERT``: asyncpg's and libpq's ``require`` and ``verify-*`` verify
#: against it, and libpq's default ``prefer`` too, falling back to plaintext when
#: that fails (ruled 2026-11-12 on #2692).
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
#: OpenSSL loads these as roots too, but their content cannot be compared.
_NON_PLAIN = re.compile(rb"-----BEGIN (?:X509|TRUSTED) CERTIFICATE-----")
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
    pem, sources = os_pem, (os_source,)
    if extra is not None:
        # The newline: a bundle without a trailing one would merge its last
        # END line with the first BEGIN, and that set does not load.
        pem += b"\n" + _extra_certificates(extra)
        sources += (extra,)
    digests = pem_digests(pem)
    named = extra is None and os_file
    ours = os_source if named else os.path.join(_set_directory(), _name(pem))
    path = _inherited(ours, pem, digests, extra)
    if path is None:
        path = ours if named else _write_set(ours, pem, digests)
    for name in FILE_VARIABLES:
        os.environ[name] = path
    os.environ[DIRECTORY_VARIABLE] = EMPTY_CAPATH
    return RootSet(path, sources, extra, len(digests))


def _ders(pem: bytes) -> list[bytes]:
    ders = []
    for block in _PEM_CERTIFICATE.findall(pem):
        try:
            ders.append(ssl.PEM_cert_to_DER_cert(block.decode("ascii")))
        except (UnicodeDecodeError, ValueError):
            continue
    return ders


def pem_digests(pem: bytes) -> frozenset[str]:
    """The SHA-256 of each ``CERTIFICATE`` block's DER: order, comments and
    duplicates do not count."""
    return frozenset(hashlib.sha256(der).hexdigest() for der in _ders(pem))


def _loads(path: str) -> bool:
    try:
        ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_verify_locations(cafile=path)
    except (OSError, ssl.SSLError):
        return False
    return True


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


def _set_directory() -> str:
    return os.path.join(tempfile.gettempdir(), f"faultmaven-tls-{os.getuid()}")


def _name(pem: bytes) -> str:
    return hashlib.sha256(pem).hexdigest() + ".pem"


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
        if _loads(candidate) and pem_digests(_read(candidate)):
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
    """The setting, in any letter case, as an absolute real path, written back
    so a child in another working directory reads the same file."""
    spellings = env_var_spellings(os.environ, EXTRA_CA_FILE_SETTING)
    value = os.environ[spellings[-1]].strip() if spellings else ""
    if not value:
        return None
    path = os.path.realpath(value)
    set_env_var(os.environ, EXTRA_CA_FILE_SETTING, path)
    return path


def _extra_certificates(path: str) -> bytes:
    """The setting's certificates, re-encoded one by one: nothing else is copied."""
    pem = _read(path)
    ders = _ders(pem)
    if not ders or _NON_PLAIN.search(pem):
        raise SystemExit(
            f"TLS root set: {EXTRA_CA_FILE_SETTING}={path!r} must be a readable PEM "
            "file of certificates labelled CERTIFICATE (an X509 or TRUSTED "
            "CERTIFICATE block cannot be compared)."
        )
    return "".join(ssl.DER_cert_to_PEM_cert(der) for der in ders).encode()


def _inherited(ours: str, pem: bytes, digests: frozenset[str], extra: str | None):
    """The real path of an inherited file that is the set, if any; refuse any
    owned variable that is not."""
    found = None
    for name in FILE_VARIABLES:
        value = os.environ.get(name)
        if not value:
            continue
        real, held = os.path.realpath(value), _read(value)
        plain = not _NON_PLAIN.search(held) and pem_digests(held) == digests
        if not (real == os.path.realpath(ours) or held == pem or plain):
            _refuse(name, value, extra)
        found = found or real
    directory = os.environ.get(DIRECTORY_VARIABLE)
    if directory:
        entries = _hashed_entries(directory)
        held = b"\n".join(_read(os.path.join(directory, n)) for n in entries)
        if entries and (_NON_PLAIN.search(held) or pem_digests(held) != digests):
            _refuse(DIRECTORY_VARIABLE, directory, extra)
    return found


def _refuse(name: str, value: str, extra: str | None) -> NoReturn:
    if os.path.realpath(value).startswith(os.path.realpath(_set_directory()) + "/"):
        raise SystemExit(
            f"TLS root set: {name}={value!r} names a set an earlier FaultMaven start "
            "wrote, and the CA files have changed since: restart that process."
        )
    the_set = "the OS CA bundle" + (f" plus {EXTRA_CA_FILE_SETTING}" if extra else "")
    raise SystemExit(
        f"TLS root set: {name}={value!r} is set and does not hold the set's "
        f"certificates ({the_set}). FaultMaven names the set in {name} itself: "
        f"remove {name}, or put its CA certificates in {EXTRA_CA_FILE_SETTING}."
    )


def _write_set(path: str, pem: bytes, digests: frozenset[str]) -> str:
    """Write the set once per content, in a directory only this user can write."""
    directory = os.path.dirname(path)
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        status = os.lstat(directory)
        if not stat.S_ISDIR(status.st_mode) or status.st_uid != os.getuid():
            raise OSError(f"{directory} is not a directory this user owns")
        if status.st_mode & 0o022:
            raise OSError(f"{directory} is writable by others")
        if _read(path) != pem:
            handle, temporary = tempfile.mkstemp(dir=directory, suffix=".tmp")
            with os.fdopen(handle, "wb") as file:
                file.write(pem)
            os.chmod(temporary, 0o644)
            os.replace(temporary, path)
    except OSError as error:
        raise SystemExit(
            f"TLS root set: cannot write the set's file ({error}); "
            "TMPDIR must name a writable directory."
        ) from error
    if not _loads(path) or pem_digests(_read(path)) != digests:
        raise SystemExit(
            f"TLS root set: the set written to {path} does not load all "
            f"{len(digests)} certificates; check {EXTRA_CA_FILE_SETTING} and "
            "the OS CA bundle."
        )
    return path
