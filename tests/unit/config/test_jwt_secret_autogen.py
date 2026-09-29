"""Tests for standalone JWT-secret auto-generation (ensure_local_jwt_secret_env).

Local auth needs an HS256 secret; get_settings() generates+persists one on first
run so a standalone install needs no JWT_SECRET_KEY. These tests pin that the
generation is local-mode-only, idempotent, overridable, persisted at 0o600, and
non-fatal on write failure — and that the field itself does NO per-construction
I/O (the generation lives in get_settings(), not a default_factory).

They also pin that a process whose DATABASE_URL configures no persistent
database creates nothing (#1703): every persistent-database gate reads the URL
through get_settings(), which calls this first, so a refused boot or ``fm-*``
command used to leave ``data/.jwt_secret`` behind. And that each of the
function's three decisions is read the way the settings read it — in any case,
validated — never from ``os.environ`` by exact name.
"""

import os

import pytest

from faultmaven.config import settings as S

#: Every name the function's decisions depend on. pydantic-settings binds them
#: case-insensitively, so ANY spelling in the ambient environment (a CI job's
#: ``DATABASE_URL``, the xdist worker's per-worker file URL) would steer a test.
_ISOLATED_NAMES = {"DATABASE_URL", "JWT_SECRET_KEY", "AUTH_MODE", "JWT_SECRET_FILE"}


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    """Remove every spelling of the isolated names, for every test in this file.

    Then record ``JWT_SECRET_KEY`` as absent, so the value the function exports
    is removed again at teardown: a bare ``delenv(..., raising=False)`` records
    nothing when the key is already absent, and the export would leak into
    later tests.
    """
    for name in [n for n in os.environ if n.upper() in _ISOLATED_NAMES]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("JWT_SECRET_KEY", "restored-at-teardown")
    monkeypatch.delenv("JWT_SECRET_KEY")


@pytest.fixture
def clean_jwt_env(monkeypatch, tmp_path):
    """Local mode, no JWT_SECRET_KEY, secret file pointed at a temp path, and
    DATABASE_URL unset: the shipped persistent default, the arm that persists."""
    monkeypatch.setenv("AUTH_MODE", "local")
    secret_file = tmp_path / ".jwt_secret"
    monkeypatch.setenv("JWT_SECRET_FILE", str(secret_file))
    return secret_file


def test_field_has_no_io_default():
    """The jwt_secret_key field must be a plain default (no I/O default_factory)."""
    field = S.SecuritySettings.model_fields["jwt_secret_key"]
    assert field.default is None
    assert field.default_factory is None


def test_generates_and_persists_in_local_mode(clean_jwt_env):
    S.ensure_local_jwt_secret_env()

    assert clean_jwt_env.exists()
    assert os.environ.get("JWT_SECRET_KEY")
    assert os.environ["JWT_SECRET_KEY"] == clean_jwt_env.read_text().strip()
    # persisted private to the owner
    assert (clean_jwt_env.stat().st_mode & 0o777) == 0o600


def test_idempotent_reuses_persisted_secret(clean_jwt_env, monkeypatch):
    S.ensure_local_jwt_secret_env()
    first = os.environ["JWT_SECRET_KEY"]

    # Simulate a fresh process: env cleared but the persisted file remains.
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
    S.ensure_local_jwt_secret_env()

    assert os.environ["JWT_SECRET_KEY"] == first


def test_explicit_env_var_wins_and_writes_nothing(clean_jwt_env, monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "user-provided-secret")
    S.ensure_local_jwt_secret_env()

    assert os.environ["JWT_SECRET_KEY"] == "user-provided-secret"
    assert not clean_jwt_env.exists()  # never generated a file


def test_oauth_mode_is_a_noop(clean_jwt_env, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "oauth")
    S.ensure_local_jwt_secret_env()

    assert "JWT_SECRET_KEY" not in os.environ
    assert not clean_jwt_env.exists()


def test_write_failure_is_nonfatal(monkeypatch, tmp_path):
    """A filesystem error must log+return, not raise (auth then errors clearly)."""
    monkeypatch.setenv("AUTH_MODE", "local")
    # Parent path is a regular file, so mkdir(parents=True) raises OSError.
    blocker = tmp_path / "iam_a_file"
    blocker.write_text("x")
    monkeypatch.setenv("JWT_SECRET_FILE", str(blocker / "nope" / ".jwt_secret"))

    S.ensure_local_jwt_secret_env()  # must not raise

    assert "JWT_SECRET_KEY" not in os.environ


# ---------------------------------------------------------------------------
# No durable secret for a process with no durable database (#1703)
# ---------------------------------------------------------------------------


@pytest.fixture
def local_mode_in_empty_cwd(monkeypatch, tmp_path):
    """Local mode, no JWT_SECRET_KEY, the SHIPPED secret path (``data/.jwt_secret``
    under the cwd), and the cwd an empty directory, so any write shows up."""
    monkeypatch.setenv("AUTH_MODE", "local")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _written(root) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


@pytest.mark.unit
@pytest.mark.parametrize(
    "database_url",
    [
        "sqlite+aiosqlite:///:memory:",
        ":memory:",
        "sqlite:///file:x?mode=memory&uri=true",
        "",
    ],
    ids=["sqlite-memory", "bare-memory", "file-uri-memory", "empty"],
)
def test_a_non_persistent_database_url_exports_a_secret_and_writes_nothing(
    local_mode_in_empty_cwd, monkeypatch, database_url
):
    monkeypatch.setenv("DATABASE_URL", database_url)

    S.ensure_local_jwt_secret_env()

    assert os.environ.get("JWT_SECRET_KEY"), "local auth was left with no secret"
    assert _written(local_mode_in_empty_cwd) == [], (
        "a process with no persistent database wrote a durable secret: "
        f"{_written(local_mode_in_empty_cwd)}"
    )


@pytest.mark.unit
def test_a_lowercase_non_persistent_database_url_writes_nothing(
    local_mode_in_empty_cwd, monkeypatch
):
    """pydantic-settings binds ``database_url`` to the field, so the gate refuses
    it; the skip must see the same spelling, or the refused run writes."""
    monkeypatch.setenv("database_url", "sqlite+aiosqlite:///:memory:")

    S.ensure_local_jwt_secret_env()

    assert os.environ.get("JWT_SECRET_KEY")
    assert _written(local_mode_in_empty_cwd) == []


@pytest.mark.unit
def test_an_existing_secret_file_is_read_not_replaced_on_a_non_persistent_database(
    local_mode_in_empty_cwd, monkeypatch
):
    """Creating nothing is the rule, not reading nothing: a secret already on
    disk is still the one this install signs with."""
    secret_file = local_mode_in_empty_cwd / "data" / ".jwt_secret"
    secret_file.parent.mkdir()
    secret_file.write_text("existing-secret\n", encoding="utf-8")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

    S.ensure_local_jwt_secret_env()

    assert os.environ["JWT_SECRET_KEY"] == "existing-secret"
    assert secret_file.read_text(encoding="utf-8") == "existing-secret\n"
    assert _written(local_mode_in_empty_cwd) == ["data", "data/.jwt_secret"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "database_url",
    [None, "sqlite+aiosqlite:///./fm.db"],
    ids=["unset-is-the-file-default", "file-url"],
)
def test_a_persistent_database_persists_the_secret_at_0600(
    local_mode_in_empty_cwd, monkeypatch, database_url
):
    """Unset DATABASE_URL is the shipped file default, a persistent database, so
    a real standalone install still keeps one secret across restarts."""
    if database_url is not None:
        monkeypatch.setenv("DATABASE_URL", database_url)

    S.ensure_local_jwt_secret_env()

    secret_file = local_mode_in_empty_cwd / "data" / ".jwt_secret"
    assert secret_file.exists(), "a persistent deployment did not persist its secret"
    assert os.environ["JWT_SECRET_KEY"] == secret_file.read_text().strip()
    assert (secret_file.stat().st_mode & 0o777) == 0o600


@pytest.mark.unit
def test_an_explicit_secret_still_wins_on_a_non_persistent_database(
    local_mode_in_empty_cwd, monkeypatch
):
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("JWT_SECRET_KEY", "user-provided-secret")

    S.ensure_local_jwt_secret_env()

    assert os.environ["JWT_SECRET_KEY"] == "user-provided-secret"
    assert _written(local_mode_in_empty_cwd) == []


# ---------------------------------------------------------------------------
# Each decision is read as the settings read it (#1703 review)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_lowercase_jwt_secret_key_is_honoured_not_shadowed(
    local_mode_in_empty_cwd, monkeypatch
):
    """The settings bind ``jwt_secret_key``. An exact-name read missed it,
    generated and persisted a secret, and exported ``JWT_SECRET_KEY``, which
    then shadowed the operator's value."""
    monkeypatch.setenv("jwt_secret_key", "operator-set-secret")

    S.ensure_local_jwt_secret_env()

    assert "JWT_SECRET_KEY" not in os.environ, "a generated secret was exported"
    assert os.environ["jwt_secret_key"] == "operator-set-secret"
    assert S.SecuritySettings().jwt_secret_key.get_secret_value() == (
        "operator-set-secret"
    )
    assert _written(local_mode_in_empty_cwd) == []


@pytest.mark.unit
def test_an_auth_mode_the_settings_refuse_writes_nothing(
    local_mode_in_empty_cwd, monkeypatch
):
    """``AuthMode`` accepts ``local`` and ``oauth`` exactly, so the settings
    refuse ``LOCAL``. A ``.strip().lower()`` read took it as local and wrote the
    secret for a run that could not start."""
    monkeypatch.setenv("AUTH_MODE", "LOCAL")

    S.ensure_local_jwt_secret_env()

    assert "JWT_SECRET_KEY" not in os.environ
    assert _written(local_mode_in_empty_cwd) == []
