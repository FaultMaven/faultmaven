"""Tests for standalone JWT-secret auto-generation (ensure_local_jwt_secret_env).

Local auth needs an HS256 secret; get_settings() generates+persists one on first
run so a standalone install needs no JWT_SECRET_KEY. These tests pin that the
generation is local-mode-only, idempotent, overridable, persisted at 0o600, and
non-fatal on write failure — and that the field itself does NO per-construction
I/O (the generation lives in get_settings(), not a default_factory).
"""

import os

import pytest

from faultmaven.config import settings as S


@pytest.fixture
def clean_jwt_env(monkeypatch, tmp_path):
    """Local mode, no JWT_SECRET_KEY, secret file pointed at a temp path."""
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
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
