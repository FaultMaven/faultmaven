"""The Postgres root inputs outside the TLS root set refuse the start (#2787).

A CRL source (``PGSSLCRL``, ``PGSSLCRLDIR``, a URL's ``sslcrl`` / ``sslcrldir``,
a ``root.crl`` under ``~/.postgresql``) is loaded by asyncpg and libpq with every
certificate in it trusted as a root (#2786), and a service-file section
(``PGSERVICE``, a URL's ``service``) supplies ``sslrootcert`` and ``sslcrl``
ahead of the environment (#2834). No file is safe to export in their place, so
each one present refuses the start, naming it and never the database URL.
``PGGSSENCMODE`` defaults to ``disable`` (#2788). The applied set is the host's
real OS bundle; each test's homes are under its ``tmp_path``, read through the
real ``_crl_directories`` (``tests/conftest.py`` stubs it for every other test).
"""

from __future__ import annotations

import os
import sys
import tempfile
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

import dotenv
import dotenv.main
import pytest

from faultmaven.config import tls_root_set
from faultmaven.config.settings import env_var_spellings
from faultmaven.config.tls_root_set import (
    FILE_VARIABLES,
    GSSENC_VARIABLE,
    OWNED_VARIABLES,
    PG_ROOT_INPUT_VARIABLES,
    RootSetRefusal,
    apply_root_set,
    pg_root_inputs_in_url,
)
from faultmaven.infrastructure.persistence import pg_connection

pytestmark = pytest.mark.unit

PASSWORD, HOST = "s3cret-pw", "db.internal.example"
URL = f"postgresql+asyncpg://fm:{PASSWORD}@{HOST}:5432/fm"
#: The real lookup, taken before the conftest fixture stubs it.
REAL_CRL_DIRECTORIES = tls_root_set._crl_directories
#: Query keys that are not plain option names. psycopg2 writes a key unescaped
#: into the conninfo, so each reaches libpq as a root input: by its own tokens,
#: or by ending the quoting of the value after it (the reviews' bypasses).
MALFORMED = {
    "plus": "+sslcrl=/x",
    "encoded-space": "%20service=x",
    "inside-another-key": "application_name%3Dx%20sslcrl=/x",
    "trailing-equals": "application_name%3D=%20sslcrl%3D/z",
    "x-equals": "x%3D=%20sslcrl%3D/z",
    "backslash": "a%5C=%20sslcrl%3D/z",
    "quote": "a%27=%20sslcrl%3D/z",
    "trailing-equals-service": "application_name%3D=%20service%3Dfm",
}
#: Every driver option is a plain name; these stay accepted.
PLAIN = (
    "sslmode=verify-full",
    "ssl=require",
    "application_name=fm",
    "prepared_statement_cache_size=0",
    "target_session_attrs=read-write",
)
_BOUNDS = SimpleNamespace(
    database_command_timeout_seconds=30.0,
    database_connect_timeout_seconds=10.0,
    database_statement_timeout_margin_seconds=2.0,
    database_client_connection_check_interval_seconds=0.0,
)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch) -> SimpleNamespace:
    """No .env, no inherited input or database URL, and two empty homes: ``HOME``
    and the password database's, as libpq falls back to it."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setenv("FAULTMAVEN_SKIP_DOTENV", "1")
    monkeypatch.setattr(tls_root_set, "_crl_directories", REAL_CRL_DIRECTORIES)
    for name in (*PG_ROOT_INPUT_VARIABLES, GSSENC_VARIABLE, "APPDATA"):
        monkeypatch.delenv(name, raising=False)
    for key in env_var_spellings(os.environ, "DATABASE_URL"):
        monkeypatch.delenv(key)
    homes = SimpleNamespace(home=tmp_path / "home", passwd=tmp_path / "passwd-home")
    for home in vars(homes).values():
        (home / ".postgresql").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(homes.home))
    if sys.platform != "win32":
        import pwd

        entry = SimpleNamespace(pw_dir=str(homes.passwd))
        monkeypatch.setattr(pwd, "getpwuid", lambda uid: entry)
    return homes


def _refusal() -> str:
    with pytest.raises(RootSetRefusal) as refusal:
        tls_root_set._apply_root_set()
    message = str(refusal.value)
    assert message.startswith("TLS root set: ") and "TLS_EXTRA_CA_FILE" in message
    assert not set(FILE_VARIABLES) & set(os.environ), "refused before any export"
    return message


# --- the variables -----------------------------------------------------------


@pytest.mark.parametrize("value", ["", "/some/crl.pem"], ids=["empty", "a-path"])
@pytest.mark.parametrize("name", PG_ROOT_INPUT_VARIABLES)
def test_an_inherited_crl_or_service_variable_refuses_with_any_value(
    monkeypatch, name, value
):
    monkeypatch.setenv(name, value)
    message = _refusal()
    assert f"{name} is set" in message and f"unset {name}" in message
    assert value == "" or value not in message


def test_an_inherited_owned_variable_is_refused_before_a_postgres_input(
    tmp_path, monkeypatch
):
    """The must-not-change order: ``_check_inherited`` refuses first."""
    foreign = tmp_path / "foreign.pem"
    foreign.write_bytes(b"not the set")
    monkeypatch.setenv("SSL_CERT_FILE", str(foreign))
    monkeypatch.setenv("PGSERVICE", "x")
    with pytest.raises(RootSetRefusal, match=r"^TLS root set: SSL_CERT_FILE="):
        tls_root_set._apply_root_set()


def test_a_crl_variable_written_only_in_the_dotenv_refuses(tmp_path, monkeypatch):
    """``load_settings_dotenv`` puts it in the environment the drivers read."""
    (tmp_path / ".env").write_text("PGSSLCRL=/some/crl.pem\n")
    monkeypatch.delenv("FAULTMAVEN_SKIP_DOTENV")
    monkeypatch.setattr(dotenv, "dotenv_values", dotenv.main.dotenv_values)
    monkeypatch.chdir(tmp_path)
    assert "PGSSLCRL is set" in _refusal()


# --- the database URL --------------------------------------------------------


@pytest.mark.parametrize("key", ["sslcrl", "sslcrldir", "service", "dsn"])
def test_a_database_url_naming_a_root_input_refuses_naming_only_the_key(
    monkeypatch, key
):
    """``dsn`` too: both dialects hand it to ``connect(dsn=...)``, which can
    carry any of the others."""
    monkeypatch.setenv("DATABASE_URL", f"{URL}?sslmode=verify-full&{key}=/x")
    message = _refusal()
    assert f"query names {key}," in message
    assert PASSWORD not in message and HOST not in message


@pytest.mark.parametrize(
    "url",
    [
        f"postgresql://fm:{PASSWORD}@{HOST}/fm#x?sslcrl=/f",
        f"postgresql://fm:pa#{PASSWORD}@{HOST}/fm?sslcrl=/f",
    ],
    ids=["fragment-before-query", "unencoded-hash-in-password"],
)
def test_a_query_urllib_cannot_see_is_read_as_the_engine_reads_it(monkeypatch, url):
    """psycopg2 completed an outside-CA handshake through both (critique P1)."""
    assert "sslcrl" not in urllib.parse.urlsplit(url).query
    monkeypatch.setenv("database_url", url)  # any spelling, as the settings bind
    message = _refusal()
    assert "query names sslcrl," in message
    assert PASSWORD not in message and HOST not in message


@pytest.mark.parametrize("query", MALFORMED.values(), ids=MALFORMED)
def test_a_key_that_is_not_a_plain_option_name_refuses_by_the_rule(monkeypatch, query):
    """Named by the rule, never by its text, which can carry a value (the
    reviews reproduced an outside-CA handshake and an opened CRL path)."""
    monkeypatch.setenv("DATABASE_URL", f"{URL}?{query}")
    message = _refusal()
    assert "the database URL's query holds a query key that is not a plain " in message
    assert "query names" not in message and "/x" not in message
    assert "/z" not in message and "=fm" not in message
    assert PASSWORD not in message and HOST not in message
    with pytest.raises(ValueError, match="not a plain option name"):
        pg_connection.pg_connect_args(f"{URL}?{query}", _BOUNDS)


@pytest.mark.parametrize("query", PLAIN)
def test_a_plain_option_name_is_accepted(monkeypatch, query):
    assert pg_root_inputs_in_url(f"{URL}?{query}") == []
    monkeypatch.setenv("DATABASE_URL", f"{URL}?{query}")
    apply_root_set()


def test_a_value_holding_root_input_text_reaches_libpq_as_that_value_only():
    """psycopg2 quotes a value, so its text never becomes an option: read back
    from the conninfo SQLAlchemy's psycopg2 dialect builds. The same path with
    a malformed key does hand libpq ``sslcrl`` (the control)."""
    pytest.importorskip("psycopg2")
    from psycopg2.extensions import make_dsn, parse_dsn
    from sqlalchemy.dialects.postgresql.psycopg2 import PGDialect_psycopg2
    from sqlalchemy.engine import make_url

    def conninfo(query: str) -> dict:
        url = make_url(f"postgresql://fm:{PASSWORD}@{HOST}/fm?{query}")
        cargs, cparams = PGDialect_psycopg2().create_connect_args(url)
        return parse_dsn(make_dsn(cargs[0] if cargs else None, **cparams))

    value = "application_name=a%27%20sslcrl%3D%2Fz"
    assert pg_root_inputs_in_url(f"{URL}?{value}") == []
    assert conninfo(value)["application_name"] == "a' sslcrl=/z"
    assert "sslcrl" not in conninfo(value)
    assert "sslcrl" in conninfo(MALFORMED["trailing-equals"])


@pytest.mark.parametrize(
    "url",
    [
        "sqlite+aiosqlite:///./data/faultmaven.db",
        f"{URL}?sslmode=verify-full&sslrootcert=/etc/fm/db-ca.pem",
        f"{URL}?application_name=a%27%20sslcrl%3D%2Fz",  # a value, quoted for libpq
        "not a url",
        f" {URL}?sslcrl=/x",  # the settings refuse surrounding whitespace
    ],
    ids=[
        "sqlite",
        "sslrootcert-verify-full",
        "a-value-with-spaces",
        "unparseable",
        "whitespace",
    ],
)
def test_a_url_naming_no_root_input_is_left_to_the_settings(monkeypatch, url):
    monkeypatch.setenv("DATABASE_URL", url)
    assert apply_root_set().path == os.environ["PGSSLROOTCERT"]


def test_every_caller_reads_the_url_through_one_helper():
    """``pg_connect_args`` checks the engine's own URL: a nested ``DATABASE``
    variable or an explicit ``get_engine(database_url=...)`` skips the start's
    check (#1785)."""
    settings = _BOUNDS
    assert pg_root_inputs_in_url(f"{URL}?service=a&dsn=b&sslcrl=c") == [
        "sslcrl",
        "service",
        "dsn",
    ]
    for url in (f"{URL}?dsn=x", "postgresql+psycopg://fm@h/fm?sslcrldir=/d"):
        with pytest.raises(ValueError, match="query names (dsn|sslcrldir),") as raised:
            pg_connection.pg_connect_args(url, settings)
        assert PASSWORD not in str(raised.value)
    assert pg_connection.pg_connect_args("sqlite+aiosqlite:///x.db", settings) == {}


# --- the default CRL file ----------------------------------------------------


@pytest.mark.parametrize("home", ["home", "passwd"])
def test_a_root_crl_in_either_home_refuses_naming_the_file(isolated, home):
    crl = getattr(isolated, home) / ".postgresql" / "root.crl"
    crl.write_bytes(b"")
    message = _refusal()
    assert f"{crl} exists" in message and "remove or rename it" in message


def test_a_root_crl_directory_refuses_and_a_dangling_link_does_not(isolated):
    crl = isolated.home / ".postgresql" / "root.crl"
    crl.symlink_to(isolated.home / "gone.crl")
    apply_root_set()
    for name in OWNED_VARIABLES:  # a second start, not a child
        os.environ.pop(name)  # not monkeypatch: its undo would restore the set
    crl.unlink()
    crl.mkdir()
    assert f"{crl} exists" in _refusal()


@pytest.mark.skipif(sys.platform == "win32", reason="no password database")
def test_a_home_that_cannot_be_resolved_is_no_source(isolated, monkeypatch):
    import pwd

    def unresolvable():
        raise RuntimeError("Could not determine home directory.")

    def no_entry(uid):
        raise KeyError(uid)

    monkeypatch.setattr(Path, "home", unresolvable)
    monkeypatch.setattr(pwd, "getpwuid", no_entry)
    assert tls_root_set._root_crl_paths() == []
    apply_root_set()


@pytest.mark.parametrize("known_folder", [True, False], ids=["shell32", "variable"])
def test_on_windows_libpqs_appdata_crl_is_a_source(tmp_path, monkeypatch, known_folder):
    """The known folder, as libpq resolves it; the variable without shell32."""
    import ctypes

    def shget_folder_path(owner, csidl, token, flags, folder):
        assert csidl == 0x001A  # CSIDL_APPDATA
        folder.value = str(tmp_path / "known")
        return 0

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path / "variable"))
    if known_folder:  # the Windows ctypes surface, faked whole
        shell32 = SimpleNamespace(SHGetFolderPathW=shget_folder_path)
        monkeypatch.setattr(
            ctypes, "windll", SimpleNamespace(shell32=shell32), raising=False
        )
        buffer = lambda size: SimpleNamespace(value="")  # noqa: E731
        monkeypatch.setattr(ctypes, "create_unicode_buffer", buffer, raising=False)
    else:
        monkeypatch.delattr(ctypes, "windll", raising=False)
    folder = "known" if known_folder else "variable"
    expected = os.path.join(str(tmp_path / folder), "postgresql", "root.crl")
    assert tls_root_set._root_crl_paths()[-1] == expected


# --- what the set exports ----------------------------------------------------


def test_a_clean_apply_exports_no_crl_or_service_input():
    apply_root_set()
    assert not set(PG_ROOT_INPUT_VARIABLES) & set(os.environ)


@pytest.mark.parametrize("inherited", [None, "require", ""])
def test_gssencmode_defaults_to_disable_and_an_operators_value_stays(
    monkeypatch, inherited
):
    if inherited is not None:
        monkeypatch.setenv(GSSENC_VARIABLE, inherited)
    apply_root_set()
    assert os.environ[GSSENC_VARIABLE] == (
        "disable" if inherited is None else inherited
    )
