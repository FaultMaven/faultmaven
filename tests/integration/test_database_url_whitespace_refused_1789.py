"""A padded ``DATABASE_URL`` is refused before anything is written (#1789).

The boot, an operator command and an online ``alembic upgrade head`` each run in
a clean child from an empty directory. Each must refuse with the message naming
``DATABASE_URL``, print neither the URL nor its password, and leave the
directory empty: no ``data/``, no JWT secret, and no database file under the
padded name or the stripped one. Unpadded URLs are the existing positive
controls in ``test_boot_refuses_nonpersistent_database.py``,
``test_operator_commands_refuse_nonpersistent_database.py`` and
``test_alembic_refuses_nonpersistent_database.py``.
"""

from __future__ import annotations

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
MESSAGE = "DATABASE_URL has leading or trailing whitespace"
NAME, PASSWORD = "p1789", "FakePw1789"
SQLITE = f"sqlite+aiosqlite:///./{NAME}.db"
POSTGRES = f"postgresql+asyncpg://fm_app:{PASSWORD}@127.0.0.1:1/{NAME}"
PADS = {
    "leading-space": " {}",
    "trailing-space": "{} ",
    "lf": "{}\n",
    "crlf": "{}\r\n",
    "tab": "\t{}",
}
#: Every pad on a file URL (a write would show in the listing), and the
#: realistic trailing newline on a credentialed URL (an echo would show).
CASES = [(pad, SQLITE) for pad in PADS.values()] + [(PADS["lf"], POSTGRES)]
IDS = [f"{key}-sqlite" for key in PADS] + ["lf-postgres"]

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


def _child(argv: list[str], database_url: str, prepare=None):
    """Run ``argv`` from an empty directory with only ``database_url`` set.

    The temp root's name holds no "test", so the boot child does not take the
    test-environment predicate. Returns the process and every path the run left.
    """
    root = Path(tempfile.mkdtemp(prefix="fm-ws1789-"))
    try:
        cwd = root / "run"
        cwd.mkdir()
        before = set(prepare(cwd)) if prepare else set()
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
            "DATABASE_URL": database_url,
        }
        completed = subprocess.run(
            [sys.executable, *argv],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=600,
        )
        left = sorted(str(p.relative_to(cwd)) for p in cwd.rglob("*"))
        return completed, [entry for entry in left if entry not in before]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _assert_refused_unseen(completed, left) -> str:
    output = completed.stdout + completed.stderr
    detail = f"exit {completed.returncode}\n{output[-3000:]}"
    assert MESSAGE in output, detail
    assert NAME not in output and PASSWORD not in output, detail
    assert left == [], f"the refused run wrote {left}\n{detail}"
    return detail


@pytest.mark.parametrize("pad, url", CASES, ids=IDS)
def test_the_boot_refuses_a_padded_url(pad, url):
    completed, left = _child(["-c", _BOOT], pad.format(url))
    detail = _assert_refused_unseen(completed, left)
    assert "BOOTED" not in completed.stdout, detail
    assert "REFUSED ConfigurationError" in completed.stdout, detail


@pytest.mark.parametrize("pad, url", CASES, ids=IDS)
def test_an_operator_command_refuses_a_padded_url(pad, url):
    code = (
        "import sys\n"
        "sys.argv = ['fm-promote-platform-admin', 'admin']\n"
        "from faultmaven.cli.promote_platform_admin import main\n"
        "main()\n"
    )
    completed, left = _child(["-c", code], pad.format(url))
    detail = _assert_refused_unseen(completed, left)
    assert completed.returncode == 1, detail
    assert completed.stdout == "", detail


def _alembic_copy(cwd: Path) -> list[str]:
    shutil.copy2(TREE / "alembic.ini", cwd / "alembic.ini")
    shutil.copytree(
        TREE / "alembic",
        cwd / "alembic",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    (cwd / "data").mkdir()
    return [str(p.relative_to(cwd)) for p in cwd.rglob("*")]


@pytest.mark.parametrize("pad, url", CASES, ids=IDS)
def test_online_alembic_refuses_a_padded_url(pad, url):
    argv = ["-m", "alembic", "-c", "alembic.ini", "upgrade", "head"]
    completed, left = _child(argv, pad.format(url), prepare=_alembic_copy)
    detail = _assert_refused_unseen(completed, left)
    assert completed.returncode == 1, detail
    assert f"❌ Refusing to run: {MESSAGE}" in completed.stderr, detail
    assert "Traceback" not in completed.stderr, detail
