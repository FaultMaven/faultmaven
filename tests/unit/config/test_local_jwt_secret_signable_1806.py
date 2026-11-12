"""A local HS256 secret PyJWT refuses as an HMAC key stops startup (#1806).

Since PyJWT 2.14 the HMAC key preparation refuses a value that looks like a
PEM or SSH key or a JWK. Such a ``JWT_SECRET_KEY`` (or secret file) booted
cleanly, then failed every login (500) and every request (401). The secret is
now judged where it is resolved, before it is set or exported, and the
refusal never carries the value. OAuth mode is not judged; a short secret,
which PyJWT only warns about, still starts.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import textwrap
import warnings
from pathlib import Path

import jwt
import pytest

import faultmaven
from faultmaven.config import settings as S
from faultmaven.models.exceptions import ConfigurationError
from tests.utils import (
    delenv_every_spelling,
    get_live_settings,
    reset_settings_singleton,
)

pytestmark = pytest.mark.unit

TREE = Path(faultmaven.__file__).resolve().parent.parent
MESSAGE = "PyJWT refuses this value as an HMAC secret"

#: The three shapes PyJWT 2.15 refuses, each carrying a marker body.
REFUSED = {
    "jwk-set": json.dumps({"keys": [{"kty": "oct", "k": "Qm9va1806Jwk"}]}),
    "pem": "-----BEGIN PUBLIC KEY-----\nMIIBIjAN1806PemBody\n-----END PUBLIC KEY-----",
    "ssh": "ssh-rsa AAAAB3NzaC1yc2E1806SshBody operator@host",
}


def _fragments(secret: str) -> set[str]:
    """Every 8-character slice of ``secret``: none may appear in an output."""
    return {secret[i : i + 8] for i in range(len(secret) - 7)}


def _assert_unseen(secret: str, text: str) -> None:
    leaked = sorted(f for f in _fragments(secret) if f in text)
    assert not leaked, f"secret fragments {leaked} in:\n{text[-3000:]}"


@pytest.fixture
def local_env(monkeypatch, tmp_path):
    """Local mode in a scratch cwd, with every name the outcome reads cleared.

    ``JWT_SECRET_KEY`` is set then deleted, so a value the code exports is
    removed again at teardown; the singleton is cleared on both sides and
    the environment restored (presets write to it without monkeypatch).
    """
    saved = dict(os.environ)
    delenv_every_spelling(
        monkeypatch,
        "DATABASE_URL",
        "DATABASE",
        "JWT_SECRET_KEY",
        "JWT_SECRET_FILE",
        "AUTH_MODE",
        "OAUTH_ENABLED",
    )
    monkeypatch.setenv("JWT_SECRET_KEY", "restored-at-teardown")
    monkeypatch.delenv("JWT_SECRET_KEY")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTH_MODE", "local")
    monkeypatch.setenv("JWT_SECRET_FILE", str(tmp_path / ".jwt_secret"))
    reset_settings_singleton()
    try:
        yield tmp_path
    finally:
        reset_settings_singleton()
        os.environ.clear()
        os.environ.update(saved)


def _assert_refused(refused: pytest.ExceptionInfo, secret: str, source: str):
    text = str(refused.value)
    assert MESSAGE in text and source in text, text
    _assert_unseen(secret, text)
    _assert_unseen(secret, repr(refused.value.context))


@pytest.mark.parametrize("secret", REFUSED.values(), ids=REFUSED.keys())
def test_an_explicit_secret_pyjwt_refuses_stops_startup(local_env, secret):
    os.environ["JWT_SECRET_KEY"] = secret

    with pytest.raises(ConfigurationError) as refused:
        get_live_settings()

    _assert_refused(refused, secret, "JWT_SECRET_KEY")
    assert os.environ["JWT_SECRET_KEY"] == secret


@pytest.mark.parametrize(
    "database_url",
    [None, "sqlite+aiosqlite:///:memory:"],
    ids=["persistent", "non-persistent"],
)
@pytest.mark.parametrize("secret", REFUSED.values(), ids=REFUSED.keys())
def test_a_secret_file_pyjwt_refuses_stops_startup_unexported(
    local_env, secret, database_url
):
    secret_file = local_env / ".jwt_secret"
    secret_file.write_text(secret)
    if database_url is not None:
        os.environ["DATABASE_URL"] = database_url

    with pytest.raises(ConfigurationError) as refused:
        get_live_settings()

    _assert_refused(refused, secret, str(secret_file))
    assert "JWT_SECRET_KEY" not in os.environ
    assert secret_file.read_text() == secret


@pytest.mark.parametrize(
    "secret",
    ["plain-operator-secret-1806", "short", "héllo-ünïcode-1806"],
    ids=["plain", "short", "non-ascii"],
)
def test_an_explicit_secret_pyjwt_signs_with_starts(local_env, secret):
    os.environ["JWT_SECRET_KEY"] = secret

    settings = get_live_settings()

    assert settings.security.jwt_secret_key.get_secret_value() == secret


def test_a_generated_secret_starts(local_env):
    settings = get_live_settings()

    generated = settings.security.jwt_secret_key.get_secret_value()
    assert generated and os.environ["JWT_SECRET_KEY"] == generated


def test_oauth_mode_does_not_judge_jwt_secret_key(local_env):
    """OAuth signs with RS256 keys; a stray HS256 value is not its concern."""
    os.environ["AUTH_MODE"] = "oauth"
    os.environ["OAUTH_ENABLED"] = "true"
    os.environ["JWT_SECRET_KEY"] = REFUSED["pem"]
    settings = S.FaultMavenSettings(_env_file=None)
    assert settings.auth.auth_mode is S.AuthMode.OAUTH  # the probe is live

    S.resolve_local_jwt_secret(settings)

    assert settings.security.jwt_secret_key.get_secret_value() == REFUSED["pem"]


PARITY = {
    **REFUSED,
    "certificate-line": "-----BEGIN CERTIFICATE-----",
    "json-without-kty": json.dumps({"a": 1}),
    "json-with-kty": json.dumps({"kty": "x"}),
    "non-ascii": "héllo-ünïcode",
    "short": "short",
    "token-urlsafe": "token_urlsafe-48",
}


@pytest.mark.parametrize("name", PARITY)
def test_the_check_refuses_exactly_what_the_signer_refuses(name):
    """Pinned to ``jwt.encode`` so a PyJWT bump cannot move them apart."""
    value = PARITY[name]
    if value == "token_urlsafe-48":
        value = secrets.token_urlsafe(48)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # a short key warns; it still signs
        try:
            jwt.encode({"sub": "u"}, value, "HS256")
            signs = True
        except jwt.InvalidKeyError:
            signs = False
    try:
        S.require_signable_hs256_secret(value, "JWT_SECRET_KEY")
        accepted = True
    except S.LocalJwtSecretError:
        accepted = False

    assert accepted is signs


# -- the real entry points, each in a clean child ---------------------------

_BOOT = textwrap.dedent("""
    try:
        from fastapi.testclient import TestClient
        from faultmaven.main import app
        with TestClient(app):
            pass
        print("BOOTED")
    except BaseException as exc:
        print("REFUSED " + type(exc).__name__ + ": " + str(exc))
    """)

_OPERATOR = (
    "import sys\n"
    "sys.argv = ['fm-promote-platform-admin', 'admin']\n"
    "from faultmaven.cli.promote_platform_admin import main\n"
    "main()\n"
)


def _child(code: str, secret: str) -> subprocess.CompletedProcess:
    """Run ``code`` from an empty directory, local mode, ``secret`` set.

    Follows the #1789 child pattern: a clean environment and no ``.env``.
    """
    root = Path(tempfile.mkdtemp(prefix="fm-jwt1806-"))
    try:
        cwd = root / "run"
        cwd.mkdir()
        env = {
            "HOME": str(root),
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": str(TREE),
            "DEPLOYMENT_MODE": "standalone",
            "AUTH_MODE": "local",
            "ENVIRONMENT": "development",
            "CHAT_PROVIDER": "gemini",
            "GEMINI_API_KEY": "dummy-key-for-boot-probe",
            "FAULTMAVEN_SKIP_DOTENV": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "DATABASE_URL": "sqlite+aiosqlite:///./fm1806.db",
            "JWT_SECRET_KEY": secret,
        }
        return subprocess.run(
            [sys.executable, "-c", code],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=600,
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_the_boot_refuses_a_secret_pyjwt_refuses():
    secret = REFUSED["jwk-set"]

    completed = _child(_BOOT, secret)

    output = completed.stdout + completed.stderr
    detail = f"exit {completed.returncode}\n{output[-3000:]}"
    assert "BOOTED" not in completed.stdout, detail
    assert "REFUSED ConfigurationError" in completed.stdout, detail
    assert MESSAGE in completed.stdout, detail
    _assert_unseen(secret, output)


def test_an_operator_command_refuses_a_secret_pyjwt_refuses():
    secret = REFUSED["pem"]

    completed = _child(_OPERATOR, secret)

    output = completed.stdout + completed.stderr
    detail = f"exit {completed.returncode}\n{output[-3000:]}"
    assert completed.returncode == 1, detail
    assert MESSAGE in completed.stderr, detail
    _assert_unseen(secret, output)
