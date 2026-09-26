"""Operator commands, run for real, refuse a non-persistent database (#1659).

On an empty ``DATABASE_URL``, ``fm-provision-service-account -u slack-agent``
under ``AUTH_MODE=oauth`` printed "✅ Created service account 'slack-agent'"
and a refresh token, then exited 0. The account lived in the container's
in-memory user store, so the token authenticated nothing once the process
exited. The other commands named the wrong cause ("User 'admin' not found") or
died three layers down on a URL parse error, after writing ``data/`` and Chroma
files into the working directory.

Each declared ``fm-*`` command and each dev script that reaches the database is
run here in a child process, from an empty working directory, through its real
entrypoint. The assertions are the whole contract: exit 1, the boot gate's
message on stderr, nothing on stdout (so no token and no "✅"), no traceback,
and nothing written. ``tests/unit/cli/test_database_gate.py`` holds the
structural census. This file is the behavioural half, because a check on the
source says nothing about what the process does.

``JWT_SECRET_KEY`` is set in the child. Without it, ``get_settings()`` writes
``data/.jwt_secret`` on its first call in local auth mode (the standalone
convenience), whichever command runs and whatever ``DATABASE_URL`` says. The
gate reads settings, so that write would land ahead of the refusal. The API
boot probe (``test_boot_refuses_nonpersistent_database.py``) sets it for the
same reason.

The positive control runs a command with a file URL relative to the same
working directory. It gets past the gate, and the database file it creates
shows up in the cwd listing. So an empty listing after a refusal means the
refusal came first, not that the probe was looking in the wrong place.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

import pytest

import faultmaven

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
TREE = Path(faultmaven.__file__).resolve().parent.parent

with (REPO_ROOT / "pyproject.toml").open("rb") as _handle:
    DECLARED: dict[str, str] = tomllib.load(_handle)["project"]["scripts"]

_E = "00000000-0000-0000-0000-00000000e001"

#: A valid argument vector per declared command: one that gets through argparse
#: and the command's own argument checks, so what runs next is the gate. Every
#: declared command needs a row. A missing row fails, never skips.
ARGV: dict[str, list[str]] = {
    "fm-provision-service-account": ["-u", "slack-agent", "--token-only"],
    "fm-promote-platform-admin": ["admin"],
    "fm-demote-platform-admin": ["admin"],
    "fm-remove-org-member": [
        "--enterprise-id",
        _E,
        "--organization-id",
        "org-1",
        "--user",
        "admin",
        "--yes",
    ],
    "fm-personal-tenant": ["retire", "--subject", "user_01H", "--apply"],
    "fm-reassign-cases": [
        "--enterprise-id",
        _E,
        "--from-user",
        "a",
        "--to-user",
        "b",
        "--case-ids-file",
        "ids.txt",
        "--yes",
    ],
    "fm-reset-kb": ["--yes"],
    "fm-set-turn-cap": [
        "--enterprise-id",
        _E,
        "--organization-id",
        "org-1",
        "--cap",
        "5",
        "--yes",
    ],
    "fm-provision-sso-org": [
        "--name",
        "Acme",
        "--slug",
        "acme",
        "--domain",
        "acme.com",
        "--workos-org-id",
        "org_01H",
    ],
    "fm-wipe-deployment": ["--wipe", "--confirm-target", "faultmaven", "--yes"],
}

#: The dev scripts ``tests/unit/cli/test_database_gate.py`` finds reaching the
#: database, with an argument vector each.
DEV_SCRIPTS: dict[str, list[str]] = {
    "scripts/auth/create_user.py": ["--username", "u1", "--email", "u1@example.com"],
    "scripts/auth/list_users.py": [],
    "scripts/cleanup_corrupt_cases.py": [],
}

REFUSAL = "configures no persistent database"


@pytest.fixture(scope="module")
def oauth_keys():
    """An RS256 key pair outside any probe's cwd, for the oauth-mode command."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    root = Path(tempfile.mkdtemp(prefix="fm-cligate-keys-"))
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = root / "private.pem"
    public = root / "public.pem"
    private.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    public.write_bytes(
        key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    yield {
        "AUTH_MODE": "oauth",
        "OAUTH_ENABLED": "true",
        "JWT_PRIVATE_KEY_PATH": str(private),
        "JWT_PUBLIC_KEY_PATH": str(public),
    }
    shutil.rmtree(root, ignore_errors=True)


def _run(code: str, database_url: str, extra_env: dict | None = None):
    """Run ``code`` in a clean child whose cwd is an empty directory.

    Returns the completed process and the cwd's entries afterwards, less the
    one input file a command reads (``ids.txt``).
    """
    root = Path(tempfile.mkdtemp(prefix="fm-cligate-"))
    try:
        cwd = root / "run"
        cwd.mkdir()
        (cwd / "ids.txt").write_text("case-1\n", encoding="utf-8")
        env = {
            "HOME": str(root),
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": str(TREE),
            "DATABASE_URL": database_url,
            "DEPLOYMENT_MODE": "standalone",
            "AUTH_MODE": "local",
            "JWT_SECRET_KEY": "cligate-probe-secret-please-ignore-00001",
            **(extra_env or {}),
        }
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=600,
        )
        entries = sorted(p.name for p in cwd.iterdir() if p.name != "ids.txt")
        return completed, entries
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _prologue() -> str:
    # The child must import the tree under test, not an editable install of
    # another checkout; it asserts so rather than trusting PYTHONPATH.
    return (
        "import sys, faultmaven\n"
        f"assert faultmaven.__file__.startswith({str(TREE)!r}), faultmaven.__file__\n"
    )


def _command_code(command: str) -> str:
    module_path, _, attr = DECLARED[command].partition(":")
    return _prologue() + (
        f"sys.argv = [{command!r}] + {ARGV[command]!r}\n"
        f"from {module_path} import {attr}\n"
        f"{attr}()\n"
    )


def _script_code(script: str) -> str:
    return _prologue() + (
        "import runpy\n"
        f"sys.argv = [{script!r}] + {DEV_SCRIPTS[script]!r}\n"
        f"runpy.run_path({str(REPO_ROOT / script)!r}, run_name='__main__')\n"
    )


def _assert_refused(label: str, completed, entries) -> None:
    detail = f"{label}\nstdout:\n{completed.stdout[-2000:]}\nstderr:\n{completed.stderr[-3000:]}"
    assert completed.returncode == 1, detail
    assert REFUSAL in completed.stderr, detail
    assert "sqlite+aiosqlite:///./data/faultmaven.db" in completed.stderr, detail
    assert completed.stdout == "", f"a refusal printed to stdout\n{detail}"
    assert "✅" not in completed.stderr, detail
    assert "Traceback" not in completed.stderr, detail
    assert entries == [], f"the refused run wrote {entries} before refusing\n{detail}"


def test_every_declared_command_has_an_argument_vector():
    missing = set(DECLARED) - set(ARGV)
    assert not missing, (
        f"no ARGV row for {sorted(missing)}: add one that gets through the "
        "command's argument checks, so this file drives it"
    )
    stale = set(ARGV) - set(DECLARED)
    assert not stale, f"ARGV rows for commands no longer declared: {sorted(stale)}"


@pytest.mark.parametrize("command", sorted(DECLARED), ids=str)
def test_declared_command_refuses_an_empty_database_url(command, oauth_keys):
    if command not in ARGV:
        pytest.fail(f"no ARGV row for {command}")
    extra = oauth_keys if command == "fm-provision-service-account" else None
    completed, entries = _run(_command_code(command), "", extra)
    _assert_refused(command, completed, entries)


@pytest.mark.parametrize(
    "database_url",
    [
        ":memory:",
        "sqlite+aiosqlite:///:memory:",
        "sqlite://",
        "sqlite+aiosqlite:///?timeout=30",
        "sqlite+aiosqlite://?check_same_thread=false",
        "sqlite+aiosqlite:///file:x?uri=true&mode=memor%79",
        "file::memory:?cache=shared",
    ],
)
def test_the_token_minting_command_refuses_every_in_memory_spelling(
    database_url, oauth_keys
):
    """The harmful case from the issue, over every spelling it lists."""
    completed, entries = _run(
        _command_code("fm-provision-service-account"), database_url, oauth_keys
    )
    _assert_refused(f"DATABASE_URL={database_url!r}", completed, entries)


@pytest.mark.parametrize("script", sorted(DEV_SCRIPTS), ids=str)
def test_dev_script_refuses_an_empty_database_url(script):
    completed, entries = _run(_script_code(script), "")
    _assert_refused(script, completed, entries)


def test_positive_control_a_file_database_gets_past_the_gate_and_writes_here():
    """Same child, same cwd layout, a file URL: the gate lets it through, and
    the database the command opens appears in the listing the refusals left
    empty."""
    command = "fm-reset-kb"
    code = _prologue() + (
        f"sys.argv = [{command!r}, '--dry-run']\n"
        "from faultmaven.cli.reset_kb import main\n"
        "main()\n"
    )
    completed, entries = _run(code, "sqlite+aiosqlite:///./fm.db")
    assert REFUSAL not in completed.stderr, completed.stderr[-2000:]
    assert "fm.db" in entries, (entries, completed.stderr[-2000:])
