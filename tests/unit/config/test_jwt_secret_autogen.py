"""Tests for the standalone local JWT secret (``resolve_local_jwt_secret``).

Local auth needs an HS256 secret; get_settings() generates and persists one on
first run, so a standalone install needs no JWT_SECRET_KEY. These tests pin that
the generation is local-mode-only, idempotent, overridable, persisted at 0o600,
and non-fatal on write failure — and that the field itself does NO
per-construction I/O.

They also pin #1703: the secret is resolved from the BUILT settings, the object
every persistent-database gate reads, after it validates. So a run the settings
refuse writes nothing, a run whose database is not persistent creates nothing,
and the database judged is ``settings.database.database_url`` in every spelling
pydantic binds (lowercase, nested JSON ``DATABASE``). Every settings object here
is real, built from a controlled environment — never a double, which would
answer whatever the test assumed.
"""

import logging
import os

import pytest

from faultmaven.config import settings as S
from faultmaven.models.exceptions import ConfigurationError
from tests.utils import (
    delenv_every_spelling,
    get_live_settings,
    reset_settings_singleton,
)

#: Every name the outcome depends on, removed in every letter case: the
#: settings bind them case-insensitively, and an ambient one (a CI job's
#: ``DATABASE_URL``, the xdist worker's file URL) would steer a test.
_ISOLATED_NAMES = (
    "DATABASE_URL",
    "DATABASE",
    "JWT_SECRET_KEY",
    "JWT_SECRET_FILE",
    "JWT_PRIVATE_KEY",
    "AUTH_MODE",
    "OAUTH_ENABLED",
    "SESSION_TIMEOUT_MINUTES",
)


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    """Remove every spelling of the isolated names, for every test in this file.

    Then record ``JWT_SECRET_KEY`` as absent, so the value the function exports
    is removed again at teardown: a bare ``delenv(..., raising=False)`` records
    nothing when the key is already absent, and the export would leak into
    later tests.
    """
    delenv_every_spelling(monkeypatch, *_ISOLATED_NAMES)
    monkeypatch.setenv("JWT_SECRET_KEY", "restored-at-teardown")
    monkeypatch.delenv("JWT_SECRET_KEY")


@pytest.fixture
def empty_cwd(monkeypatch, tmp_path):
    """Local mode, the SHIPPED secret path (``data/.jwt_secret`` under the cwd),
    and the cwd an empty directory, so any write shows up."""
    monkeypatch.setenv("AUTH_MODE", "local")
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def clean_jwt_env(monkeypatch, tmp_path):
    """Local mode, secret file pointed at a temp path, DATABASE_URL unset: the
    shipped persistent default, the arm that persists."""
    monkeypatch.setenv("AUTH_MODE", "local")
    secret_file = tmp_path / ".jwt_secret"
    monkeypatch.setenv("JWT_SECRET_FILE", str(secret_file))
    return secret_file


@pytest.fixture
def through_get_settings():
    """``get_settings()`` rebuilt from this test's environment.

    Presets write to ``os.environ`` without ``monkeypatch``, so the environment
    is restored afterwards; the singleton is cleared on both sides.
    """
    saved = dict(os.environ)
    reset_settings_singleton()
    try:
        yield get_live_settings
    finally:
        reset_settings_singleton()
        os.environ.clear()
        os.environ.update(saved)


def _resolved() -> S.FaultMavenSettings:
    """Settings built as get_settings() builds them, then resolved."""
    settings = S.FaultMavenSettings()
    S.resolve_local_jwt_secret(settings)
    return settings


def _secret(settings) -> str | None:
    key = settings.security.jwt_secret_key
    return key.get_secret_value() if key is not None else None


def _written(root) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


def test_field_has_no_io_default():
    """The jwt_secret_key field must be a plain default (no I/O default_factory)."""
    field = S.SecuritySettings.model_fields["jwt_secret_key"]
    assert field.default is None
    assert field.default_factory is None


# ---------------------------------------------------------------------------
# A persistent database: generate once, persist, reuse
# ---------------------------------------------------------------------------


def test_generates_and_persists_in_local_mode(clean_jwt_env):
    settings = _resolved()

    assert clean_jwt_env.exists()
    assert _secret(settings) == clean_jwt_env.read_text().strip()
    assert os.environ["JWT_SECRET_KEY"] == _secret(settings)
    # persisted private to the owner
    assert (clean_jwt_env.stat().st_mode & 0o777) == 0o600


def test_idempotent_reuses_persisted_secret(clean_jwt_env, monkeypatch):
    first = _secret(_resolved())

    # Simulate a fresh process: env cleared but the persisted file remains.
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)

    assert _secret(_resolved()) == first


@pytest.mark.unit
@pytest.mark.parametrize(
    "database_url",
    [None, "sqlite+aiosqlite:///./fm.db"],
    ids=["unset-is-the-file-default", "file-url"],
)
def test_a_persistent_database_persists_the_secret_at_0600(
    empty_cwd, monkeypatch, database_url
):
    """Unset DATABASE_URL is the shipped file default, a persistent database, so
    a real standalone install still keeps one secret across restarts."""
    if database_url is not None:
        monkeypatch.setenv("DATABASE_URL", database_url)

    settings = _resolved()

    secret_file = empty_cwd / "data" / ".jwt_secret"
    assert secret_file.exists(), "a persistent deployment did not persist its secret"
    assert _secret(settings) == secret_file.read_text().strip()
    assert (secret_file.stat().st_mode & 0o777) == 0o600


def test_write_failure_is_nonfatal(monkeypatch, tmp_path):
    """A filesystem error must log+return, not raise (auth then errors clearly)."""
    monkeypatch.setenv("AUTH_MODE", "local")
    # Parent path is a regular file, so mkdir(parents=True) raises OSError.
    blocker = tmp_path / "iam_a_file"
    blocker.write_text("x")
    monkeypatch.setenv("JWT_SECRET_FILE", str(blocker / "nope" / ".jwt_secret"))

    settings = _resolved()  # must not raise

    assert settings.security.jwt_secret_key is None
    assert "JWT_SECRET_KEY" not in os.environ


# ---------------------------------------------------------------------------
# Nothing to do: an explicit secret, or OAuth
# ---------------------------------------------------------------------------


def test_explicit_env_var_wins_and_writes_nothing(clean_jwt_env, monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "user-provided-secret")

    settings = _resolved()

    assert _secret(settings) == "user-provided-secret"
    assert os.environ["JWT_SECRET_KEY"] == "user-provided-secret"
    assert not clean_jwt_env.exists()  # never generated a file


@pytest.mark.unit
def test_a_lowercase_jwt_secret_key_is_honoured_not_shadowed(empty_cwd, monkeypatch):
    """The settings bind ``jwt_secret_key``. An exact-name read missed it,
    generated and persisted a secret, and exported ``JWT_SECRET_KEY``, which
    then shadowed the operator's value."""
    monkeypatch.setenv("jwt_secret_key", "operator-set-secret")

    settings = _resolved()

    assert _secret(settings) == "operator-set-secret"
    assert "JWT_SECRET_KEY" not in os.environ, "a generated secret was exported"
    assert S.SecuritySettings().jwt_secret_key.get_secret_value() == (
        "operator-set-secret"
    )
    assert _written(empty_cwd) == []


@pytest.mark.unit
def test_valid_oauth_settings_are_left_alone(empty_cwd, monkeypatch):
    """A VALID OAuth configuration, so the auth-mode check itself is what
    returns — not a validation failure standing in for it (#1778 review, R4)."""
    monkeypatch.setenv("AUTH_MODE", "oauth")
    monkeypatch.setenv("OAUTH_ENABLED", "true")
    settings = S.FaultMavenSettings()
    assert settings.auth.auth_mode is S.AuthMode.OAUTH  # the probe is live

    S.resolve_local_jwt_secret(settings)

    assert settings.security.jwt_secret_key is None
    assert "JWT_SECRET_KEY" not in os.environ
    assert _written(empty_cwd) == []


# ---------------------------------------------------------------------------
# No durable secret for a process with no durable database (#1703)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "name, database_url",
    [
        ("DATABASE_URL", "sqlite+aiosqlite:///:memory:"),
        ("DATABASE_URL", ":memory:"),
        ("DATABASE_URL", "sqlite:///file:x?mode=memory&uri=true"),
        ("DATABASE_URL", ""),
        ("database_url", "sqlite+aiosqlite:///:memory:"),
    ],
    ids=["sqlite-memory", "bare-memory", "file-uri-memory", "empty", "lowercase"],
)
def test_a_non_persistent_database_url_gets_a_secret_and_writes_nothing(
    empty_cwd, monkeypatch, name, database_url
):
    monkeypatch.setenv(name, database_url)

    settings = _resolved()

    assert _secret(settings), "local auth was left with no secret"
    assert os.environ["JWT_SECRET_KEY"] == _secret(settings)
    assert _written(empty_cwd) == [], (
        "a process with no persistent database wrote a durable secret: "
        f"{_written(empty_cwd)}"
    )


@pytest.mark.unit
def test_an_existing_secret_file_is_read_not_replaced_on_a_non_persistent_database(
    empty_cwd, monkeypatch
):
    """Creating nothing is the rule, not reading nothing: a secret already on
    disk is still the one this install signs with."""
    secret_file = empty_cwd / "data" / ".jwt_secret"
    secret_file.parent.mkdir()
    secret_file.write_text("existing-secret\n", encoding="utf-8")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

    settings = _resolved()

    assert _secret(settings) == "existing-secret"
    assert os.environ["JWT_SECRET_KEY"] == "existing-secret"
    assert secret_file.read_text(encoding="utf-8") == "existing-secret\n"
    assert _written(empty_cwd) == ["data", "data/.jwt_secret"]


@pytest.mark.unit
def test_an_explicit_secret_still_wins_on_a_non_persistent_database(
    empty_cwd, monkeypatch
):
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("JWT_SECRET_KEY", "user-provided-secret")

    settings = _resolved()

    assert _secret(settings) == "user-provided-secret"
    assert _written(empty_cwd) == []


# ---------------------------------------------------------------------------
# Through get_settings(): the same object the gate reads (#1778 review)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "name, value",
    [("SESSION_TIMEOUT_MINUTES", "abc"), ("AUTH_MODE", "LOCAL")],
    ids=["another-class-invalid", "auth-mode-case"],
)
def test_settings_that_do_not_validate_write_nothing(
    empty_cwd, monkeypatch, through_get_settings, name, value
):
    """The settings refuse the run, so nothing is minted or written for it.
    Resolving before the build wrote the secret for every such run (D2)."""
    monkeypatch.setenv(name, value)

    with pytest.raises(ConfigurationError):
        through_get_settings()

    assert _written(empty_cwd) == []
    assert "JWT_SECRET_KEY" not in os.environ


@pytest.mark.unit
def test_a_nested_json_in_memory_database_writes_nothing(
    empty_cwd, monkeypatch, through_get_settings
):
    """``DATABASE`` as JSON reaches the settings (and so the gate) but no flat
    reader of ``DATABASE_URL`` (D3)."""
    monkeypatch.setenv("DATABASE", '{"database_url": "sqlite:///:memory:"}')

    settings = through_get_settings()

    assert settings.database.database_url == "sqlite:///:memory:"
    assert S.persistent_database_configured(settings.database.database_url) is False
    assert _secret(settings)
    assert _written(empty_cwd) == []


@pytest.mark.unit
def test_a_nested_json_file_database_persists_as_the_gate_judges(
    empty_cwd, monkeypatch, through_get_settings
):
    """The other direction: the nested file URL wins over a flat in-memory one
    in the settings, the gate lets the run through, so the secret persists."""
    db = empty_cwd / "p.db"
    monkeypatch.setenv("DATABASE", f'{{"database_url": "sqlite:///{db}"}}')
    monkeypatch.setenv("DATABASE_URL", ":memory:")

    settings = through_get_settings()

    assert settings.database.database_url == f"sqlite:///{db}"
    assert S.persistent_database_configured(settings.database.database_url) is True
    secret_file = empty_cwd / "data" / ".jwt_secret"
    assert secret_file.exists()
    assert _secret(settings) == secret_file.read_text().strip()


@pytest.mark.unit
def test_a_later_empty_lowercase_secret_leaves_one_non_empty_exported_secret(
    empty_cwd, monkeypatch, through_get_settings
):
    """``JWT_SECRET_KEY=abcdef`` then ``jwt_secret_key=``: the later, empty one
    is what the settings bind, so a secret is minted. Exported under the exact
    name only, it left the empty lowercase one to override it in every later
    reader: an empty signing key (D5)."""
    monkeypatch.setenv("JWT_SECRET_KEY", "abcdef")
    monkeypatch.setenv("jwt_secret_key", "")

    settings = through_get_settings()

    assert _secret(settings)
    spellings = [key for key in os.environ if key.upper() == "JWT_SECRET_KEY"]
    assert spellings == ["JWT_SECRET_KEY"], spellings
    # What a child process, or a settings object built later, reads.
    assert S.SecuritySettings().jwt_secret_key.get_secret_value() == _secret(settings)


@pytest.mark.unit
@pytest.mark.parametrize(
    "env",
    [
        {},
        {"DATABASE_URL": "sqlite+aiosqlite:///:memory:"},
        {
            "JWT_SECRET_KEY": "marker-secret-value-0001",
            "SESSION_TIMEOUT_MINUTES": "abc",
        },
    ],
    ids=["persisted", "ephemeral", "refused"],
)
def test_no_log_record_carries_the_secret_or_a_private_key(
    empty_cwd, monkeypatch, through_get_settings, caplog, env
):
    """The secret and the private key never reach a log line at any level. An
    earlier version logged the settings' ValidationError at DEBUG, which
    carries every input value (R5)."""
    private_key = "marker-private-key-value-0002"
    monkeypatch.setenv("JWT_PRIVATE_KEY", private_key)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    caplog.set_level(logging.DEBUG)

    try:
        secret = _secret(through_get_settings())
    except ConfigurationError:
        secret = env["JWT_SECRET_KEY"]

    assert secret
    logged = "\n".join(record.getMessage() for record in caplog.records)
    logged += caplog.text
    assert secret not in logged
    assert private_key not in logged
