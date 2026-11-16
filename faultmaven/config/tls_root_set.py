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
Configuration). ``root_set_status`` (``features.tls_root_set`` on ``GET
/admin/config/status``) re-reads the set's sources, writing nothing, and
compares them with the bytes of the file the environment names. Imports no
client.

The Postgres drivers have root inputs of their own outside these variables
(#2786, #2834): a CRL source (``PGSSLCRL``, ``PGSSLCRLDIR``, a URL's ``sslcrl``
or ``sslcrldir``, ``root.crl`` under ``~/.postgresql``), whose every
certificate asyncpg and libpq load as a trusted root, and a service-file section
(``PGSERVICE``, a URL's ``service``), whose ``sslrootcert`` and ``sslcrl`` win
over the environment. libpq, the migration process, reads all of them. asyncpg,
the app's driver, reads a CRL file through ``PGSSLCRL`` or the home default, and
the URL keys and a service file only when handed a DSN (a URL's ``dsn``). No
file is safe to export in their place (an empty one does not load, and any file
that loads turns on CRL checking), so each one present at start refuses it,
named without echoing the database URL: a variable with any value, a
``DATABASE_URL`` query naming one of ``PG_ROOT_INPUT_URL_KEYS`` (``dsn`` too,
which carries any of them) or holding any key that is not a plain option name
(which can smuggle one into libpq's conninfo), and a ``root.crl`` under
``Path.home()`` or the effective user's password-database home (on Windows,
under libpq's APPDATA folder too, ``%APPDATA%\\postgresql``). Postgres CRL
checking is not supported under the set. ``PGGSSENCMODE`` is set to
``disable`` unless already set (#2788), so libpq negotiates no GSSAPI
encryption in place of TLS unless the URL's ``gssencmode`` or the environment
names a mode.
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
    DatabaseUrlWhitespaceError,
    configured_database_url,
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

#: Postgres root inputs outside the set, refused when present with any value:
#: libpq's (and asyncpg's) CRL file, libpq's CRL directory, and the service
#: file's section name. ``PGSERVICEFILE`` and ``PGSYSCONFDIR`` name no section.
PG_ROOT_INPUT_VARIABLES = ("PGSSLCRL", "PGSSLCRLDIR", "PGSERVICE")
#: The same inputs as a database URL's query keys, plus ``dsn``: both dialects
#: pass its value to the driver's ``connect(dsn=...)``, which can carry any.
PG_ROOT_INPUT_URL_KEYS = ("sslcrl", "sslcrldir", "service", "dsn")
#: libpq's default ``prefer`` negotiates GSSAPI encryption, not TLS, when
#: Kerberos credentials are present; set to ``disable`` unless already set.
GSSENC_VARIABLE = "PGGSSENCMODE"

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
    os_source, os_file, extra, pem = _set_sources()
    if extra is not None:  # a child in another working directory reads the same file
        set_env_var(os.environ, EXTRA_CA_FILE_SETTING, extra)
    named = extra is None and os_file
    path = os_source if named else os.path.join(_set_directory(), _name(pem))
    _check_inherited(path, pem, extra)
    _check_postgres_root_inputs()
    if not named:  # written, or re-written if missing or altered (self-heal)
        _write_set(path, pem)
    for name in FILE_VARIABLES:
        os.environ[name] = path
    os.environ[DIRECTORY_VARIABLE] = EMPTY_CAPATH
    os.environ.setdefault(GSSENC_VARIABLE, "disable")  # an operator's value stays
    sources = (os_source,) + ((extra,) if extra else ())
    return RootSet(path, sources, extra, len(pem_digests(pem)))


def _set_sources() -> tuple[str, bool, str | None, bytes]:
    """(where the OS bundle came from, whether that is a file to name, the
    setting's real path, the set's PEM), read from the sources alone."""
    os_source, pem, os_file = _resolve_os_bundle()
    extra = _extra_ca_file()
    if extra is not None:
        # The newline: a bundle without a trailing one would merge its last
        # END line with the first BEGIN, and that set does not load.
        pem += b"\n" + _extra_certificates(extra)
    return os_source, os_file, extra, pem


@dataclass(frozen=True)
class RootSetStatus:
    """``features.tls_root_set``: whether this process verifies against the set."""

    in_force: bool
    certificates: int | None
    description: str


def root_set_status() -> RootSetStatus:
    """Read from the environment, never module state, so each worker answers
    for itself, and writes nothing. In force when every owned file variable
    names one file, that file loads, ``SSL_CERT_DIR`` names a directory with no
    hashed entry, and the file's bytes are the set its sources give now."""
    values = {os.environ.get(name) for name in FILE_VARIABLES}
    paths = {os.path.realpath(value) for value in values if value}
    if None in values or "" in values or len(paths) != 1:
        return _not_in_force(None, "the TLS root variables do not name one file")
    path = paths.pop()
    if not (os.path.isfile(path) and _loads(path)):
        return _not_in_force(None, f"the file they name, {path}, does not load")
    content = _read(path)
    count = len(pem_digests(content))
    directory = os.environ.get(DIRECTORY_VARIABLE, "")
    if not os.path.isdir(directory) or _hashed_entries(directory):
        reason = f"{DIRECTORY_VARIABLE} is not a directory free of certificates"
        return _not_in_force(count, reason)
    try:
        extra, pem = _set_sources()[2:]
    except SystemExit:  # a source cannot be read now (a rotated Secret)
        return RootSetStatus(False, count, (
            "The TLS root set cannot be verified because its sources changed "
            f"since this process started: it keeps the file it started with, "
            f"{path} ({count} certificates), and a restart re-reads them."
        ))  # fmt: skip
    if content != pem:
        reason = f"{path} is not the OS CA bundle plus {EXTRA_CA_FILE_SETTING} now"
        return _not_in_force(count, reason)
    return RootSetStatus(True, count, (
        f"TLS clients verify against {path}: {count} certificates, "
        f"{EXTRA_CA_FILE_SETTING} {extra or 'unset'}."
    ))  # fmt: skip


def _not_in_force(count: int | None, reason: str) -> RootSetStatus:
    return RootSetStatus(False, count, f"The TLS root set is not in force: {reason}.")


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


def _is_bytes(path: str, pem: bytes) -> bool:
    """Read only a regular file of ``pem``'s size: a FIFO or device is not read."""
    try:
        status = os.stat(path)
    except OSError:
        return False
    size_matches = stat.S_ISREG(status.st_mode) and status.st_size == len(pem)
    return size_matches and _read(path) == pem


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
    """The setting, in any letter case, as an absolute real path."""
    spellings = env_var_spellings(os.environ, EXTRA_CA_FILE_SETTING)
    value = os.environ[spellings[-1]].strip() if spellings else ""
    return os.path.realpath(value) if value else None


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
            if not _is_bytes(value, pem):
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


def pg_root_inputs_in_url(database_url: str) -> list[str]:
    """What ``database_url``'s query would hand a driver outside the set, as the
    engine's own parser (``make_url``) reads it: each ``PG_ROOT_INPUT_URL_KEYS``
    it names, then ``MALFORMED_QUERY_KEY`` when any key is not a plain option
    name. None for a value that does not parse, which no engine can open.

    psycopg2 writes a key into the conninfo unescaped, so a key holding ``=``,
    whitespace, a backslash or a quote ends libpq's quoting of the value after
    it, and text inside that value becomes an option of its own
    (``?application_name%3D=%20sslcrl%3D/z``). Every libpq and asyncpg option
    is a plain name, so any other key either fails in the driver or smuggles
    one in; it is refused by that rule, never named, as its text can hold a
    value."""
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import ArgumentError

    try:
        query = make_url(database_url).query
    except (ArgumentError, ValueError):
        return []
    found = [key for key in PG_ROOT_INPUT_URL_KEYS if key in query]
    if any(not _OPTION_NAME.fullmatch(key) for key in query):
        found.append(MALFORMED_QUERY_KEY)
    return found


#: What a database URL's query key must be: the shape of every driver option.
_OPTION_NAME = re.compile(r"[a-z][a-z0-9_]*")
MALFORMED_QUERY_KEY = "a query key that is not a plain option name"


def pg_url_refusal(found: list[str]) -> str:
    """Why a database URL is refused for what ``pg_root_inputs_in_url`` found:
    the keys or the rule only, never the URL, which can hold a password. Not
    ``DATABASE_URL`` by name: an engine's URL can come from a nested
    ``DATABASE`` setting or its caller."""
    keys = [key for key in found if key != MALFORMED_QUERY_KEY]
    reasons = [f"names {', '.join(keys)}, a Postgres root input"] if keys else []
    if MALFORMED_QUERY_KEY in found:
        reasons.append(
            f"holds {MALFORMED_QUERY_KEY}, which can smuggle a Postgres root input"
        )
    return (
        f"the database URL's query {' and '.join(reasons)} outside the TLS root "
        f"set: remove it from the database URL. {_PG_REMEDY}"
    )


_PG_REMEDY = (
    "The Postgres drivers trust every certificate a CRL input holds, and a "
    "service file's sslrootcert and sslcrl win over the set; put a private CA "
    f"in {EXTRA_CA_FILE_SETTING}. Postgres CRL checking is not supported under "
    "the root set."
)


def _check_postgres_root_inputs() -> None:
    """Refuse a Postgres CRL or service-file input present at start (#2786,
    #2834), naming it; never a value or the database URL."""
    for name in PG_ROOT_INPUT_VARIABLES:
        if name in os.environ:
            raise SystemExit(
                f"TLS root set: {name} is set, a Postgres root input outside "
                f"the set: unset {name}. {_PG_REMEDY}"
            )
    try:
        url = configured_database_url()
    except DatabaseUrlWhitespaceError:  # the settings refuse it themselves
        url = None
    keys = pg_root_inputs_in_url(url) if url else []
    if keys:
        raise SystemExit(f"TLS root set: {pg_url_refusal(keys)}")
    for path in _root_crl_paths():
        if os.path.exists(path):  # a directory fails closed; a dangling link is none
            raise SystemExit(
                f"TLS root set: {path} exists, a Postgres root input outside "
                f"the set (the drivers' default CRL file): remove or rename it. "
                f"{_PG_REMEDY}"
            )


def _root_crl_paths() -> list[str]:
    """Each default ``root.crl`` a driver reads for this process."""
    directories = dict.fromkeys(_crl_directories())
    return [os.path.join(directory, "root.crl") for directory in directories]


def _crl_directories() -> list[str]:
    """Where a driver looks for its default ``root.crl``: ``.postgresql`` under
    ``Path.home()`` (asyncpg; libpq when ``HOME`` is set, ``USERPROFILE`` on
    Windows) and under the effective user's password-database home (libpq
    otherwise), and on Windows libpq's ``postgresql`` under the APPDATA known
    folder, resolved as libpq resolves it (``SHGetFolderPathW``), else the
    ``APPDATA`` variable. A home that cannot be resolved is none."""
    homes = []
    try:
        homes.append(str(Path.home()))
    except (RuntimeError, KeyError):
        pass
    try:
        import pwd

        homes.append(pwd.getpwuid(os.geteuid()).pw_dir)
    except (ImportError, KeyError):  # no password database (Windows), no entry
        pass
    directories = [os.path.join(home, ".postgresql") for home in homes]
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        try:
            import ctypes

            folder = ctypes.create_unicode_buffer(260)
            # CSIDL_APPDATA; 0 is S_OK
            if not ctypes.windll.shell32.SHGetFolderPathW(
                None, 0x001A, None, 0, folder
            ):
                appdata = folder.value
        except (ImportError, AttributeError, OSError):  # no ctypes or shell32
            pass
        if appdata:
            directories.append(os.path.join(appdata, "postgresql"))
    return directories


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
