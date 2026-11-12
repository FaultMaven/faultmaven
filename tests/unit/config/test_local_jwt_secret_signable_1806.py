"""A local HS256 secret PyJWT will not sign with stops startup (#1806).

Since PyJWT 2.14 the HMAC key preparation refuses a value that looks like a
PEM or SSH key or a JWK. Such a ``JWT_SECRET_KEY`` (or secret file) booted
cleanly, then failed every login (500) and every request (401). The secret is
now judged by the signer's key path (prepare, sign, verify on the HS256
algorithm) where it is resolved: an explicit value, a file value, a generated
value before it is published, and a lost race's winner before it is used. A
refused value is never written, set or exported, and no refusal (nor any
exception it chains) carries any part of it. The parity table pins that check
to a real ``jwt.encode`` / ``jwt.decode`` round trip. OAuth mode is not judged; a short secret, which PyJWT only warns about,
still starts. The real boot and an ``fm-*`` command are driven in children
by ``tests/integration/test_local_jwt_secret_refused_1806.py``.
"""

from __future__ import annotations

import json
import os
import secrets
import traceback
import warnings

import jwt
import pytest

from faultmaven.config import settings as S
from faultmaven.models.exceptions import ConfigurationError
from tests.utils import (
    delenv_every_spelling,
    get_live_settings,
    reset_settings_singleton,
)

pytestmark = pytest.mark.unit

MESSAGE = "PyJWT refuses it as an HMAC secret"

#: The three shapes PyJWT 2.15 refuses, each carrying a marker body.
REFUSED = {
    "jwk-set": json.dumps({"keys": [{"kty": "oct", "k": "Qm9va1806Jwk"}]}),
    "pem": "-----BEGIN PUBLIC KEY-----\nMIIBIjAN1806PemBody\n-----END PUBLIC KEY-----",
    "ssh": "ssh-rsa AAAAB3NzaC1yc2E1806SshBody operator@host",
}
#: Raw bytes through an env var: not UTF-8, so the signer cannot encode it.
NON_UTF8 = "fm1806-\udcff-raw"


def _fragments(secret: str) -> set[str]:
    """Every 8-character slice of ``secret``, and each non-ASCII character in
    every spelling an error message gives it (a single byte leaks that way)."""
    found = {secret[i : i + 8] for i in range(len(secret) - 7)}
    for char in {c for c in secret if not c.isascii()}:
        found |= {char, ascii(char).strip("'"), f"\\x{ord(char) & 0xFF:02x}"}
    return found


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


def _refused(secret: str, *phrases: str) -> None:
    """``get_settings()`` refuses, saying ``phrases``, and nothing in the raised
    chain (text, ``context``, args, formatted traceback) carries ``secret``."""
    with pytest.raises(ConfigurationError) as refused:
        get_live_settings()
    text = str(refused.value)
    for phrase in (MESSAGE, *phrases):
        assert phrase in text, text
    seen = "".join(traceback.format_exception(refused.value))
    exc: BaseException | None = refused.value
    while exc is not None:
        seen += repr(exc.args) + repr(getattr(exc, "context", None))
        exc = exc.__cause__ or exc.__context__
    _assert_unseen(secret, seen)


@pytest.mark.parametrize(
    "secret", [*REFUSED.values(), NON_UTF8], ids=[*REFUSED, "non-utf8"]
)
def test_an_explicit_secret_the_signer_refuses_stops_startup(local_env, secret):
    os.environ["JWT_SECRET_KEY"] = secret

    _refused(secret, "the JWT_SECRET_KEY setting", "unset it")

    assert os.environ["JWT_SECRET_KEY"] == secret


@pytest.mark.parametrize("secret", REFUSED.values(), ids=REFUSED.keys())
def test_a_secret_file_the_signer_refuses_stops_startup_unexported(local_env, secret):
    secret_file = local_env / ".jwt_secret"
    secret_file.write_text(secret)

    _refused(secret, f"the secret file {secret_file}", "delete the file")

    assert "JWT_SECRET_KEY" not in os.environ
    assert secret_file.read_text() == secret


def test_a_generated_secret_the_signer_refuses_is_never_written(local_env, monkeypatch):
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{local_env}/fm1806.db"
    monkeypatch.setattr(S.secrets, "token_urlsafe", lambda *_: REFUSED["pem"])

    _refused(REFUSED["pem"], "a secret generated for this process")

    assert "JWT_SECRET_KEY" not in os.environ
    assert list(local_env.iterdir()) == []


def test_a_lost_race_to_a_refused_winner_is_refused_unexported(local_env, monkeypatch):
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{local_env}/fm1806.db"
    winner = REFUSED["jwk-set"]

    def another_process_published_first(path, data, replace_if):
        path.write_text(winner)
        return False

    monkeypatch.setattr(S, "publish_secret_once", another_process_published_first)
    secret_file = local_env / ".jwt_secret"

    _refused(winner, f"the secret file {secret_file}")

    assert "JWT_SECRET_KEY" not in os.environ
    assert secret_file.read_text() == winner


@pytest.mark.parametrize(
    "secret",
    ["plain-operator-secret-1806", "short", "héllo-ünïcode-1806"],
    ids=["plain", "short", "non-ascii"],
)
def test_an_explicit_secret_the_signer_accepts_starts(local_env, secret):
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
    "non-utf8": NON_UTF8,
    "certificate-line": "-----BEGIN CERTIFICATE-----",
    "json-without-kty": json.dumps({"a": 1}),
    "json-with-kty": json.dumps({"kty": "x"}),
    "non-ascii": "héllo-ünïcode",
    "short": "short",
    "token-urlsafe": "token_urlsafe-48",
}


@pytest.mark.parametrize("name", PARITY)
def test_the_check_refuses_exactly_what_the_signer_refuses(name, tmp_path):
    """Pinned to an HS256 ``jwt.encode`` / ``jwt.decode`` round trip, so a
    PyJWT bump cannot move the check and the signer apart."""
    value = PARITY[name]
    if value == "token_urlsafe-48":
        value = secrets.token_urlsafe(48)
    with warnings.catch_warnings(action="ignore"):  # a short key only warns
        try:
            token = jwt.encode({"sub": "u"}, value, "HS256")
            signs = jwt.decode(token, value, algorithms=["HS256"]) == {"sub": "u"}
        except Exception:
            signs = False
    try:
        S.require_signable_hs256_secret(value, "file", tmp_path / ".jwt_secret")
        accepted = True
    except S.LocalJwtSecretError:
        accepted = False

    assert accepted is signs
