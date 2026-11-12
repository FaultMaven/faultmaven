"""tls_root_set.py

Purpose: the one writer of the process's TLS root set (§13.4, #2692): the OS CA
bundle, plus the certificates in ``TLS_EXTRA_CA_FILE`` when set.

Clients read roots from places of their own (OpenSSL's default paths, certifi,
gRPC's ``roots.pem``, libpq's root file), so ``apply_root_set`` names the set in
every variable one reads (``OWNED_VARIABLES``) before any client is imported: it
is the first statement of ``run_server``, each ``fm-*`` ``main()``, the jobs
runner, ``alembic/env.py`` and the demo seeder. Children inherit the variables.

A certificate is a PEM block labelled ``CERTIFICATE``. When a set file is
written (``TLS_EXTRA_CA_FILE`` set, an OS bundle built from the capath, or on
Windows when the certificate store yields server-auth roots) it is the OS bundle,
a newline, then the setting's certificates re-encoded through DER (never its keys
or other blocks), at ``<tmp>/faultmaven-tls-<uid>/<sha256>.pem``: one file per
content, reused by every start that computes it, never deleted, and re-written
by every apply that finds it missing or altered (a host that ages ``/tmp`` can
remove it). The set is read once per start, so a change takes effect at the
next start.

An inherited owned file variable is accepted only when its real path is the file
this process names, or its bytes are exactly the bytes this process would write
(ruling 1 on #2692, narrowed 2026-11-12: no inherited file's certificates are
compared); every file variable is then exported as this process's own path. An
inherited ``SSL_CERT_DIR``, read as an ``os.pathsep`` list, is accepted only
when no element holds a hashed entry. Anything else refuses the start (a Nix or
conda shell's ``SSL_CERT_FILE``: unset it, or put its CA in
``TLS_EXTRA_CA_FILE``), as do an OS bundle that does not load, a setting with no
certificate or a non-plain one, no usable temporary directory, and a set file
that cannot be written safely or does not load (``SystemExit``, Fail-Fast
Configuration). Imports no client.
"""

from __future__ import annotations

import getpass
import hashlib
import os
import re
import ssl
import stat
import sys
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
_SERVER_AUTH = "1.3.6.1.5.5.7.3.1"
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
    named = extra is None and os_file
    path = os_source if named else os.path.join(_set_directory(), _name(pem))
    _check_inherited(path, pem, extra)
    if not named:  # written, or re-written if missing or altered (self-heal)
        _write_set(path, pem)
    for name in FILE_VARIABLES:
        os.environ[name] = path
    os.environ[DIRECTORY_VARIABLE] = EMPTY_CAPATH
    return RootSet(path, sources, extra, len(pem_digests(pem)))


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


def _directory_name() -> str:
    user = str(os.getuid()) if hasattr(os, "getuid") else getpass.getuser()
    return "faultmaven-tls-" + re.sub(r"[^A-Za-z0-9_.-]", "_", user)


def _set_directory() -> str:
    try:
        return os.path.join(tempfile.gettempdir(), _directory_name())
    except OSError as error:  # no usable temporary directory
        raise _tmpdir_refusal(error) from error


def _tmpdir_refusal(error: OSError) -> SystemExit:
    return SystemExit(
        f"TLS root set: cannot write the set's file ({error}); "
        "TMPDIR must name a writable directory."
    )


def _windows_store() -> tuple[bytes, bool]:
    """The CA and ROOT stores' server-auth roots, read as CPython's
    ``SSLContext.load_default_certs`` reads them (``Lib/ssl.py``), and whether
    a store could not be read."""
    ders, denied = [], False
    for store in ("CA", "ROOT"):
        try:
            entries = ssl.enum_certificates(store)
        except PermissionError:
            denied = True
            continue
        for der, encoding, trust in entries:
            if encoding == "x509_asn" and (trust is True or _SERVER_AUTH in trust):
                ders.append(der)
    return "".join(ssl.DER_cert_to_PEM_cert(der) for der in ders).encode(), denied


def _name(pem: bytes) -> str:
    return hashlib.sha256(pem).hexdigest() + ".pem"


def _resolve_os_bundle() -> tuple[str, bytes, bool]:
    """(where the OS bundle came from, its PEM, whether that is a file to name).

    OpenSSL's default file if it loads, else the first distribution bundle that
    does, else the certificates in OpenSSL's default directory, joined. On
    Windows the certificate store comes first, when it yields server-auth roots:
    there is no OS file to name.
    """
    windows, denied = sys.platform == "win32", False
    if windows:
        store, denied = _windows_store()
        if pem_digests(store):
            return "the Windows certificate store", store, False
    paths = ssl.get_default_verify_paths()
    tried = ['the Windows certificate store ("CA" and "ROOT")'] if windows else []
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
    advice = {
        "win32": (
            "No server-auth root could be read from the Windows certificate store."
            if denied
            else "The Windows certificate store holds no server-auth root."
        ),
        "darwin": "Install the OS CA bundle (on a python.org build, run its "
        "'Install Certificates' step).",
    }.get(sys.platform, "Install the OS CA bundle.")
    raise SystemExit(
        "TLS root set: no operating-system CA bundle loads; tried "
        + ", ".join(tried)
        + f". {advice} {EXTRA_CA_FILE_SETTING} only adds to it."
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


def _check_inherited(path: str, pem: bytes, extra: str | None) -> None:
    """Refuse an owned file variable already set to anything but ``path`` (by
    real path) or ``pem`` (by bytes), and an ``SSL_CERT_DIR`` holding a hashed
    entry. No inherited file's certificates are compared (narrowed 2026-11-12)."""
    the_set = "the OS CA bundle" + (f" plus {EXTRA_CA_FILE_SETTING}" if extra else "")
    reason = f"is neither the set's file nor byte-identical to it ({the_set})"
    for name in FILE_VARIABLES:
        value = os.environ.get(name)
        if value and os.path.realpath(value) != os.path.realpath(path):
            if _read(value) != pem:
                _refuse(name, value, reason)
    # OpenSSL reads SSL_CERT_DIR as a list; an element it cannot list holds none,
    # and a dangling hashed link is an entry.
    directories = os.environ.get(DIRECTORY_VARIABLE, "")
    if any(
        _hashed_entries(element) for element in directories.split(os.pathsep) if element
    ):
        reason = "holds a hashed certificate entry, and the set names none"
        _refuse(DIRECTORY_VARIABLE, directories, reason)


def _refuse(name: str, value: str, reason: str) -> NoReturn:
    parent = os.path.dirname(os.path.realpath(value))
    if os.path.basename(parent) == _directory_name():
        raise SystemExit(
            f"TLS root set: {name}={value!r} names a set file from an earlier "
            "FaultMaven start that is missing, altered or not this start's set: "
            f"restart that process, or unset {name}."
        )
    raise SystemExit(
        f"TLS root set: {name}={value!r} {reason}. FaultMaven names the set in "
        f"{name} itself: unset {name}, or put its CA certificates in "
        f"{EXTRA_CA_FILE_SETTING}."
    )


def _write_set(path: str, pem: bytes) -> None:
    """Write the set once per content, in a directory only this user can write."""
    directory = os.path.dirname(path)
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        status = os.lstat(directory)
        if not stat.S_ISDIR(status.st_mode):
            raise OSError(f"{directory} is not a directory")
        # Owner and mode bits say nothing of Windows ACLs; checked elsewhere only.
        if sys.platform != "win32" and (
            status.st_uid != os.getuid() or status.st_mode & 0o022
        ):
            raise OSError(f"{directory} is not this user's alone")
        if _read(path) != pem:
            handle, temporary = tempfile.mkstemp(dir=directory, suffix=".tmp")
            with os.fdopen(handle, "wb") as file:
                file.write(pem)
            os.chmod(temporary, 0o644)
            os.replace(temporary, path)
    except OSError as error:
        raise _tmpdir_refusal(error) from error
    if not _loads(path):
        raise SystemExit(
            f"TLS root set: the set written to {path} does not load; check "
            f"{EXTRA_CA_FILE_SETTING} and the OS CA bundle."
        )
