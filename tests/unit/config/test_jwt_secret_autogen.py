"""Tests for standalone JWT-secret auto-generation (ensure_local_jwt_secret_env).

Local auth needs an HS256 secret; get_settings() generates+persists one on first
run so a standalone install needs no JWT_SECRET_KEY. These tests pin that the
generation is local-mode-only, idempotent, overridable, persisted at 0o600, and
non-fatal on write failure — and that the field itself does NO per-construction
I/O (the generation lives in get_settings(), not a default_factory).

They also pin that a process whose DATABASE_URL configures no persistent
database persists nothing (#1703): every persistent-database gate reads the URL
through get_settings(), which calls this first, so a refused boot or ``fm-*``
command used to leave ``data/.jwt_secret`` behind.
"""

import os

import pytest

from faultmaven.config import settings as S


def _unset_jwt_secret_key(monkeypatch) -> None:
    """Unset JWT_SECRET_KEY so that the value the function exports is removed
    again at teardown. A bare ``delenv(..., raising=False)`` records nothing when
    the key is already absent, so the export would leak into later tests."""
    monkeypatch.setenv("JWT_SECRET_KEY", "restored-at-teardown")
    monkeypatch.delenv("JWT_SECRET_KEY")


@pytest.fixture
def clean_jwt_env(monkeypatch, tmp_path):
    """Local mode, no JWT_SECRET_KEY, secret file pointed at a temp path, and
    DATABASE_URL unset: the shipped persistent default, the arm that persists."""
    _unset_jwt_secret_key(monkeypatch)
    monkeypatch.delenv("DATABASE_URL", raising=False)
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
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
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
    _unset_jwt_secret_key(monkeypatch)
    monkeypatch.delenv("JWT_SECRET_FILE", raising=False)
    monkeypatch.setenv("AUTH_MODE", "local")
    monkeypatch.chdir(tmp_path)
    return tmp_path


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
    assert list(local_mode_in_empty_cwd.iterdir()) == [], (
        "a process with no persistent database wrote a durable secret: "
        f"{sorted(p.name for p in local_mode_in_empty_cwd.iterdir())}"
    )


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
    if database_url is None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
    else:
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
    assert list(local_mode_in_empty_cwd.iterdir()) == []
