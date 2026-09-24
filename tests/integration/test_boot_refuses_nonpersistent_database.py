"""A boot without a persistent database is refused, before anything is written (fm#1647).

An empty or in-memory ``DATABASE_URL`` used to boot as far as the single-tenant
enterprise seed and die there as ``RuntimeError: Critical bootstrap failure``,
having already created ``data/`` and written the pseudonym key file. The gate
(``config/persistent_database.py``) runs in the lifespan straight after the
deployment coherence gate, BEFORE ``resolve_pseudonym_key``, and OUTSIDE the
``_is_test_environment`` skip.

These tests drive the real lifespan through ``TestClient(app)``, because a
direct call to the gate says nothing about where it runs:

- **In a child process with the test-environment predicate FALSE** — the shape
  a deployment boots in. ``_is_test_environment`` is true for any argv entry
  containing "test", so the child's script path and temp root must not contain
  it (see ``test_fresh_install_boots.py``, whose first version was vacuous for
  exactly that reason). The child reports ``test_env_predicate`` so that is asserted,
  not assumed.
- **In-process with the predicate TRUE** — pins that the gate is not behind the
  test-environment skip.

The no-disk-write assertion has a positive control (``slow``): the same child,
same cwd-relative layout, with a real file URL, boots and DOES write ``data/``
there — so an empty cwd after a refusal means the refusal came first, not that
the probe looked in the wrong place.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

import faultmaven
from faultmaven.config.persistent_database import (
    DEFAULT_DATABASE_URL,
    NonPersistentDatabaseError,
)

_MARKER = "RESULT "

_CHILD = textwrap.dedent("""
    import json, os

    from fastapi.testclient import TestClient

    from faultmaven.config.settings import DatabaseSettings, get_settings
    from faultmaven.main import app, _is_test_environment

    result = {
        "test_env_predicate": _is_test_environment(get_settings()),
        "shipped_default": DatabaseSettings.model_fields["database_url"].default,
        "faultmaven_file": __import__("faultmaven").__file__,
    }
    try:
        with TestClient(app) as client:
            result["health_status"] = client.get("/health").status_code
        result["booted"] = True
    except Exception as exc:
        result["booted"] = False
        result["error_type"] = type(exc).__name__
        result["error"] = str(exc)
    result["cwd_entries"] = sorted(os.listdir("."))
    print("RESULT " + json.dumps(result))
    """)


def _boot_in_child(database_url: str) -> dict:
    """Boot the real app in a clean child process, cwd an empty directory."""
    # Its own temp root, never pytest's tmp_path: that path contains "test",
    # which flips ``_is_test_environment`` in the child and hides the question.
    root = Path(tempfile.mkdtemp(prefix="fm-dbgate-"))
    try:
        run_dir = root / "run"
        run_dir.mkdir()
        script = root / "boot_probe.py"
        script.write_text(_CHILD, encoding="utf-8")
        env = {
            "HOME": str(root),
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": str(Path(faultmaven.__file__).parent.parent),
            "DATABASE_URL": database_url,
            "DEPLOYMENT_MODE": "standalone",
            "AUTH_MODE": "local",
            "ENVIRONMENT": "development",
            "JWT_SECRET_KEY": "dbgate-probe-secret-please-ignore-000001",
            # The LLM credential gate is live when the predicate is false.
            "CHAT_PROVIDER": "gemini",
            "GEMINI_API_KEY": "dummy-key-for-boot-probe",
        }
        completed = subprocess.run(
            [sys.executable, str(script)],
            cwd=run_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=600,
        )
        for line in completed.stdout.splitlines():
            if line.startswith(_MARKER):
                result = json.loads(line[len(_MARKER) :])
                result["stderr_tail"] = completed.stderr[-3000:]
                return result
        raise AssertionError(
            "the child never reported:\n"
            f"stdout tail:\n{completed.stdout[-2000:]}\n"
            f"stderr tail:\n{completed.stderr[-2000:]}"
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _assert_probe_is_live(result: dict) -> None:
    assert result["test_env_predicate"] is False, (
        "the child ran under the test-environment predicate, so it does not "
        f"boot the way a deployment does: {result}"
    )
    here = Path(faultmaven.__file__).resolve().parent
    assert Path(result["faultmaven_file"]).resolve().parent == here, (
        "the child imported a different faultmaven tree than this test: "
        f"{result['faultmaven_file']} vs {here}"
    )


@pytest.mark.integration
@pytest.mark.parametrize(
    "database_url",
    ["", "sqlite+aiosqlite:///:memory:", ":memory:"],
    ids=["empty", "sqlite-memory", "bare-memory"],
)
def test_deployment_boot_is_refused_before_anything_is_written(database_url):
    result = _boot_in_child(database_url)
    _assert_probe_is_live(result)

    assert result["booted"] is False, f"booted on DATABASE_URL={database_url!r}"
    assert result["error_type"] == "NonPersistentDatabaseError", result
    assert DEFAULT_DATABASE_URL in result["error"]
    assert "needs a database" in result["error"]
    assert result["cwd_entries"] == [], (
        "the refused boot wrote to disk before refusing (data/ and the "
        f"pseudonym key live under the cwd): {result['cwd_entries']}"
    )


@pytest.mark.integration
def test_refusal_names_the_default_the_product_ships():
    """The message's default must be the settings field's SHIPPED default.

    Read in the clean child: in this process the harness rebinds the field's
    default to a per-worker file, so it cannot be compared here.
    """
    result = _boot_in_child("")
    assert result["shipped_default"] == DEFAULT_DATABASE_URL


@pytest.mark.slow
@pytest.mark.integration
def test_a_file_database_boots_and_writes_where_the_refusal_did_not():
    """Positive control for the empty-cwd assertion, and the ordinary boot."""
    result = _boot_in_child(DEFAULT_DATABASE_URL)
    _assert_probe_is_live(result)
    assert result["booted"] is True, result.get("error")
    assert result["health_status"] == 200
    assert "data" in result["cwd_entries"], result["cwd_entries"]


@pytest.mark.integration
@pytest.mark.parametrize(
    "database_url", ["", "sqlite+aiosqlite:///:memory:"], ids=["empty", "memory"]
)
def test_gate_is_not_behind_the_test_environment_skip(
    database_url, tmp_path, monkeypatch, unshared_app_boot
):
    """In-process, where ``_is_test_environment`` is TRUE, the boot still refuses.

    Boots ``faultmaven.main.app`` itself because the lifespan is the subject, so
    it takes ``unshared_app_boot`` and is listed as a "real" site in the census
    (``tests/unit/architecture/test_app_boot_is_shared.py``).
    """
    from fastapi.testclient import TestClient

    from faultmaven.config.settings import get_settings, reset_settings
    from faultmaven.main import _is_test_environment, app

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", database_url)
    reset_settings()
    try:
        settings = get_settings()
        assert settings.database.database_url == database_url
        assert _is_test_environment(settings) is True

        with pytest.raises(NonPersistentDatabaseError, match="needs a database"):
            with TestClient(app):
                pass  # pragma: no cover - the lifespan refuses before yielding

        assert (
            list(tmp_path.iterdir()) == []
        ), "the refused boot wrote under the cwd before refusing"
    finally:
        monkeypatch.undo()
        reset_settings()
