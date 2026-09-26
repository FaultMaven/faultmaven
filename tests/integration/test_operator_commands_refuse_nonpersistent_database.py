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
entrypoint, once for every mode or subcommand its ``main()`` branches on. The
assertions are the whole contract: exit 1, the boot gate's message on stderr,
nothing on stdout (so no token and no "✅"), no traceback, and nothing written.
A credentialed URL that fails to parse is refused without its password being
printed. ``tests/unit/cli/test_database_gate.py`` holds the
structural census. This file is the behavioural half, because a check on the
source says nothing about what the process does.

``JWT_SECRET_KEY`` is set in the child. Without it, ``get_settings()`` writes
``data/.jwt_secret`` on its first call in local auth mode (the standalone
convenience), whichever command runs and whatever ``DATABASE_URL`` says. The
gate reads settings, so that write would land ahead of the refusal. The API
boot refused by the same gate writes it too, and its probe
(``test_boot_refuses_nonpersistent_database.py``) also sets the key.
``PYTHON_DOTENV_DISABLED`` keeps the child from loading a ``.env`` above the
checkout.

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

_ENT = ["--enterprise-id", _E]
_ORG = ["--organization-id", "org-1"]
_REASSIGN = [*_ENT, "--from-user", "a", "--to-user", "b", "--case-ids-file", "ids.txt"]
_SSO = ["--name", "Acme", "--slug", "acme", "--domain", "acme.com"]

#: Valid argument vectors per declared command, one for each mode or subcommand
#: its ``main()`` branches on (a dry run and a write, each subcommand, each
#: wipe mode). Each gets through argparse and the command's own argument checks,
#: so what runs next is the gate. Covering every branch matters: a vector per
#: command could not see a new ungated branch on a mode it did not drive. Every
#: declared command needs a row. A missing row fails, never skips.
ARGV: dict[str, list[list[str]]] = {
    "fm-provision-service-account": [
        ["-u", "slack-agent", "--token-only"],
        ["-u", "slack-agent"],
    ],
    "fm-promote-platform-admin": [["admin"]],
    "fm-demote-platform-admin": [["admin"], ["admin", "--keep-org-admin"]],
    "fm-remove-org-member": [
        [*_ENT, *_ORG, "--user", "admin", "--dry-run"],
        [*_ENT, *_ORG, "--user", "admin", "--yes"],
    ],
    "fm-personal-tenant": [
        ["retire", "--subject", "user_01H"],
        ["retire", "--enterprise-id", _E, "--apply"],
        ["re-anchor", "--subject", "user_01H", "--enterprise-id", _E],
        ["purge-idp-org", "--provider-org-id", "org_01H", "--apply"],
    ],
    "fm-reassign-cases": [[*_REASSIGN, "--dry-run"], [*_REASSIGN, "--yes"]],
    "fm-reset-kb": [["--dry-run"], ["--yes"], ["--yes", "--rebuild"]],
    "fm-set-turn-cap": [
        [*_ENT, *_ORG, "--show"],
        [*_ENT, *_ORG, "--cap", "5", "--dry-run"],
        [*_ENT, *_ORG, "--unlimited", "--yes"],
        [*_ENT, "--account-id", "acct-1", "--show"],
    ],
    "fm-provision-sso-org": [
        [*_SSO, "--workos-org-id", "org_01H"],
        [*_SSO, "--workos-org-id", "org_01H", "--enterprise-id", _E],
    ],
    "fm-wipe-deployment": [
        [],
        ["--verify"],
        ["--wipe", "--confirm-target", "faultmaven", "--yes"],
    ],
}

#: The dev scripts ``tests/unit/cli/test_database_gate.py`` finds reaching the
#: database, with an argument vector for each path through the script.
DEV_SCRIPTS: dict[str, list[list[str]]] = {
    "scripts/auth/create_user.py": [
        ["--username", "u1", "--email", "u1@example.com"],
        ["--interactive"],
    ],
    "scripts/auth/list_users.py": [[]],
    "scripts/cleanup_corrupt_cases.py": [[]],
}

#: (command, argv) pairs, so each branch is its own test id.
COMMAND_RUNS = [(command, argv) for command in sorted(ARGV) for argv in ARGV[command]]
SCRIPT_RUNS = [
    (script, argv) for script in sorted(DEV_SCRIPTS) for argv in DEV_SCRIPTS[script]
]


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
            # Hermetic: settings' load_dotenv() otherwise walks up from the
            # faultmaven package when __main__ has a __file__ (runpy), and in
            # a nested worktree finds the parent checkout's .env.
            "PYTHON_DOTENV_DISABLED": "1",
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


def _command_code(command: str, argv: list[str]) -> str:
    module_path, _, attr = DECLARED[command].partition(":")
    return _prologue() + (
        f"sys.argv = [{command!r}] + {argv!r}\n"
        f"from {module_path} import {attr}\n"
        f"{attr}()\n"
    )


def _script_code(script: str, argv: list[str]) -> str:
    return _prologue() + (
        "import runpy\n"
        f"sys.argv = [{script!r}] + {argv!r}\n"
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
    missing = sorted(command for command in DECLARED if not ARGV.get(command))
    assert not missing, (
        f"no ARGV row for {missing}: add a vector for each mode the command's "
        "main() branches on, so this file drives it"
    )
    stale = set(ARGV) - set(DECLARED)
    assert not stale, f"ARGV rows for commands no longer declared: {sorted(stale)}"


def _run_id(run) -> str:
    target, argv = run
    return f"{target} {' '.join(argv)}".strip()


@pytest.mark.parametrize("run", COMMAND_RUNS, ids=[_run_id(r) for r in COMMAND_RUNS])
def test_declared_command_refuses_an_empty_database_url(run, oauth_keys):
    command, argv = run
    extra = oauth_keys if command == "fm-provision-service-account" else None
    completed, entries = _run(_command_code(command, argv), "", extra)
    _assert_refused(_run_id(run), completed, entries)


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
        "sqlite+aiosqlite:///file:d?uri=true&cache=shared%26mode%3Dmemory",
        "sqlite+aiosqlite:///file:x?uri=true&vfs=memdb",
    ],
)
def test_the_token_minting_command_refuses_every_in_memory_spelling(
    database_url, oauth_keys
):
    """The harmful case from the issue, over every spelling it lists."""
    completed, entries = _run(
        _command_code("fm-provision-service-account", ["-u", "slack-agent"]),
        database_url,
        oauth_keys,
    )
    _assert_refused(f"DATABASE_URL={database_url!r}", completed, entries)


def test_the_refusal_does_not_print_a_password(oauth_keys):
    """A credentialed URL that fails to parse is refused without being echoed.
    Before #1659 no PostgreSQL URL reached this message."""
    password = "Pa55wordValue"
    completed, entries = _run(
        _command_code("fm-provision-service-account", ["-u", "slack-agent"]),
        f"postgresql+asyncpg://faultmaven_app:{password}@pg:5432x/faultmaven",
        oauth_keys,
    )
    assert completed.returncode == 1, completed.stderr[-2000:]
    assert REFUSAL in completed.stderr
    assert password not in completed.stdout + completed.stderr
    assert entries == []


@pytest.mark.parametrize("run", SCRIPT_RUNS, ids=[_run_id(r) for r in SCRIPT_RUNS])
def test_dev_script_refuses_an_empty_database_url(run):
    script, argv = run
    completed, entries = _run(_script_code(script, argv), "")
    _assert_refused(_run_id(run), completed, entries)


def test_positive_control_a_file_database_gets_past_the_gate_and_writes_here():
    """Same child, same cwd layout, a file URL: the gate lets it through, and
    the database the command opens appears in the listing the refusals left
    empty."""
    command = "fm-reset-kb"
    completed, entries = _run(
        _command_code(command, ["--dry-run"]), "sqlite+aiosqlite:///./fm.db"
    )
    assert REFUSAL not in completed.stderr, completed.stderr[-2000:]
    assert "fm.db" in entries, (entries, completed.stderr[-2000:])
