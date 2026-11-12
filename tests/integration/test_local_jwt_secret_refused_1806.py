"""A local JWT secret PyJWT will not sign with stops the real entry points (#1806).

The boot and an operator command each run in a clean child from an empty
directory, following ``test_database_url_whitespace_refused_1789.py``'s child
pattern. The boot refuses at ``from faultmaven.main import app``, whose import
already builds the settings, so before the lifespan starts. Each must refuse
with the message, and print no fragment of the secret anywhere in its stdout or
stderr. The settings-level cases are in
``tests/unit/config/test_local_jwt_secret_signable_1806.py``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

import faultmaven

pytestmark = pytest.mark.integration

TREE = Path(faultmaven.__file__).resolve().parent.parent
MESSAGE = "PyJWT refuses it as an HMAC secret"
JWK_SET = json.dumps({"keys": [{"kty": "oct", "k": "Qm9va1806Jwk"}]})
PEM = "-----BEGIN PUBLIC KEY-----\nMIIBIjAN1806PemBody\n-----END PUBLIC KEY-----"

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
    """Run ``code`` from an empty directory, local mode, ``secret`` set."""
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


def _assert_unseen(secret: str, completed: subprocess.CompletedProcess) -> str:
    """No 8-character slice of ``secret`` in the child's whole output."""
    output = completed.stdout + completed.stderr
    leaked = sorted(
        secret[i : i + 8] for i in range(len(secret) - 7) if secret[i : i + 8] in output
    )
    detail = f"exit {completed.returncode}\n{output[-3000:]}"
    assert not leaked, f"secret fragments {leaked}\n{detail}"
    return detail


def test_the_boot_refuses_a_secret_the_signer_refuses():
    completed = _child(_BOOT, JWK_SET)

    detail = _assert_unseen(JWK_SET, completed)
    assert "BOOTED" not in completed.stdout, detail
    assert "REFUSED ConfigurationError" in completed.stdout, detail
    assert MESSAGE in completed.stdout, detail


def test_an_operator_command_refuses_a_secret_the_signer_refuses():
    completed = _child(_OPERATOR, PEM)

    detail = _assert_unseen(PEM, completed)
    assert completed.returncode == 1, detail
    assert MESSAGE in completed.stderr, detail
