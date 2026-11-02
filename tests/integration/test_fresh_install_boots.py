"""A brand-new install must boot (#828 delta review).

The revocation storage gate refuses the boot when ``token_revocations`` cannot be
read. Placed in the composition root it ran BEFORE ``bootstrap_application``,
which is what runs the migrations — and creates ``data/`` at all — so the
documented Quick Start (``cp .env.example .env && ./faultmaven.sh start``) on a
clean machine probed a database with no tables and refused, advising the operator
to re-provision a deployment that had never run.

**Why a subprocess.** Ordering is the whole of that gate's correctness, and a
direct call to ``validate_revocation_storage`` cannot observe it — which is
exactly why the unit tests passed while this was broken. The gate is also skipped
whenever ``_is_test_environment`` is true, and that keys on ``sys.argv``
containing "pytest", so an in-process test can never run it live. A child process
with a clean environment can: it boots the real app, through the real lifespan,
against a database file that does not exist yet, with the gate ACTIVE.
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

#: What the child prints. Anything else on stdout is log noise.
_MARKER = "RESULT "

_CHILD = textwrap.dedent("""
    import json, os, sys

    # The suite's network guard, in this interpreter too: the child runs
    # outside the test process, so nothing else stops it reaching the network.
    # It downloaded BGE-M3 (~2 GB) from huggingface.co on every CI run, and a
    # stalled download hung the boot past the 600 s timeout. Its refusals are
    # reported below, and the test requires there are none.
    from tests import network_guard

    network_guard.install()

    from fastapi.testclient import TestClient

    from faultmaven.bootstrap.composition import _is_test_environment
    from faultmaven.main import app
    from faultmaven.config.settings import get_settings

    from faultmaven.infrastructure.llm.usage_ledger import get_installed_usage_ledger

    result = {"gate_live": not _is_test_environment(get_settings())}
    try:
        with TestClient(app) as client:
            result["health_status"] = client.get("/health").status_code
            result["usage_ledger_serving"] = type(
                get_installed_usage_ledger()
            ).__name__
        result["booted"] = True
        result["usage_ledger_after_shutdown"] = type(
            get_installed_usage_ledger()
        ).__name__
    except Exception as exc:
        result["booted"] = False
        result["error"] = f"{type(exc).__name__}: {exc}"

    import sqlite3

    db = os.environ["FM_PROBE_DB"]
    if os.path.exists(db):
        con = sqlite3.connect(db)
        names = [r[0] for r in con.execute(
            "select name from sqlite_master where type='table'")]
        result["token_revocations_exists"] = "token_revocations" in names
        result["llm_usage_tables"] = sorted(n for n in names if n.startswith("llm_"))
        result["table_count"] = len(names)
    else:
        result["token_revocations_exists"] = False
        result["llm_usage_tables"] = []
        result["table_count"] = 0
    result["network_refusals"] = network_guard.drain_refusals()
    print("RESULT " + json.dumps(result))
    """)


def _boot_a_fresh_install(root: Path) -> dict:
    """Boot the app in a child process against a database that does not exist."""
    run_dir = root / "run"
    run_dir.mkdir()
    db = run_dir / "data" / "faultmaven.db"
    assert not db.exists(), "the point of this test is that nothing is there yet"

    # Neither the script name NOR ANY PART OF ITS PATH may contain "test":
    # ``_is_test_environment`` returns True for any argv entry containing that
    # substring, and pytest's own ``tmp_path`` is
    # ``/tmp/pytest-of-.../test_<name>0`` — which silently skipped the gate and
    # made the first version of this test vacuous. Caught by its own
    # ``gate_live`` assertion, which is why that assertion is first.
    script = root / "boot_probe.py"
    script.write_text(_CHILD, encoding="utf-8")

    env = {
        "HOME": os.environ.get("HOME", str(root)),
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(Path(faultmaven.__file__).parent.parent),
        "DATABASE_URL": f"sqlite+aiosqlite:///{db}",
        "FM_PROBE_DB": str(db),
        "DEPLOYMENT_MODE": "standalone",
        "AUTH_MODE": "local",
        "ENVIRONMENT": "development",
        "JWT_SECRET_KEY": "fresh-install-probe-secret-please-ignore-01",
        # A provider and a credential, because with the gate live the LLM
        # credential gate beside it is live too. Presence is what it checks.
        "CHAT_PROVIDER": "gemini",
        "GEMINI_API_KEY": "dummy-key-for-boot-probe",
        # No model preload: the startup preload of BGE-M3 fetched it from
        # huggingface.co on every CI run (~2 GB; the image bakes it in), and
        # it is not what this probe tests. The app's own opt-out; the value
        # is JSON, so an empty string would fail settings construction.
        "PRELOAD_MODELS": "[]",
        # And if anything else loads a model: offline, as the shipped image
        # runs (its Dockerfile's runtime ENV), from an empty cache, so the
        # load fails fast and the boot is the same on every machine.
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_HOME": str(root / "hf-home"),
        # The parent's Redis, which the suite's steps leave empty
        # (tests/conftest.py): otherwise the client resolves the default host
        # (faultmaven-redis-master) first.
        "REDIS_HOST": os.environ.get("REDIS_HOST", ""),
    }

    try:
        completed = subprocess.run(
            [sys.executable, str(script)],
            cwd=run_dir,
            env=env,
            capture_output=True,
            text=True,
            # An offline boot takes ~45 s. 240 fails a hung boot in minutes
            # rather than holding a CI slice for ten, and says where it was.
            timeout=240,
        )
    except subprocess.TimeoutExpired as exc:
        # The output a timeout carries is bytes even under text=True: CPython
        # decodes only a run that completes.
        def tail(data) -> str:
            if isinstance(data, bytes):
                data = data.decode(errors="replace")
            return (data or "")[-2000:]

        raise AssertionError(
            f"the child did not finish in {exc.timeout} s:\n"
            f"stdout tail:\n{tail(exc.stdout)}\n"
            f"stderr tail:\n{tail(exc.stderr)}"
        ) from exc
    for line in completed.stdout.splitlines():
        if line.startswith(_MARKER):
            return json.loads(line[len(_MARKER) :])
    raise AssertionError(
        "the child never reported:\n"
        f"stdout tail:\n{completed.stdout[-2000:]}\n"
        f"stderr tail:\n{completed.stderr[-2000:]}"
    )


@pytest.fixture(scope="class")
def fresh_boot() -> dict:
    """One boot of a fresh install, shared by the class — a boot is the cost.

    Its own temp root rather than ``tmp_path``, because pytest's contains
    "test" and that is enough to skip the gate in the child.
    """
    root = Path(tempfile.mkdtemp(prefix="fm-fresh-"))
    try:
        yield _boot_a_fresh_install(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.slow
@pytest.mark.integration
class TestAFirstEverInstallBoots:
    def test_a_clean_machine_boots_and_serves(self, fresh_boot):
        """`cp .env.example .env && ./faultmaven.sh start`, essentially.

        Asserts the gate was ACTIVE first: with it skipped this test would pass
        against the very ordering it exists to forbid.
        """
        result = fresh_boot

        assert result["gate_live"] is True, (
            "the revocation storage gate was skipped, so this test proves "
            f"nothing about ordering: {result}"
        )
        assert result["booted"] is True, result.get("error")
        assert result["health_status"] == 200
        assert result["token_revocations_exists"] is True
        assert result["table_count"] > 1, (
            "bootstrap did not create the schema, so nothing about the gate's "
            f"position was exercised: {result}"
        )

    def test_the_boot_reaches_no_network(self, fresh_boot):
        """A fresh install boots offline: the image runs with HF_HUB_OFFLINE
        and the model baked in, and a test reaches no network (CLAUDE.md).
        The child runs the suite's guard; anything it refused is listed."""
        # Refusals first: when a refused call is what broke the boot, they
        # name its call sites, which `error` alone does not.
        assert fresh_boot["network_refusals"] == [], fresh_boot.get("error")
        assert fresh_boot["booted"] is True, fresh_boot.get("error")

    def test_the_usage_ledger_lives_as_long_as_the_app(self, fresh_boot):
        """#640: a fresh install migrates to the ledger's tables, the composition
        root installs the shipped ledger for the life of the app, and shutdown
        uninstalls it after draining. Without the install every billed call is
        counted ``not_composed`` and nothing is ever persisted — silently, since
        that reason logs nothing."""
        assert fresh_boot["booted"] is True, fresh_boot.get("error")
        assert fresh_boot["llm_usage_tables"] == ["llm_turn_spend", "llm_usage_daily"]
        assert fresh_boot["usage_ledger_serving"] == "SqlUsageLedger"
        assert fresh_boot["usage_ledger_after_shutdown"] == "NoneType"


class TestTheGateRunsAfterTheMigrationsItDependsOn:
    """A cheap structural guard beside the expensive behavioural one.

    The subprocess test above is the real evidence, and is marked ``slow``
    because it runs a whole boot including KB ingestion. This one costs nothing
    and fails the moment the call moves back above ``bootstrap_application``, so
    the ordering stays pinned in fast CI too.
    """

    def test_the_storage_gate_is_called_after_bootstrap(self):
        import ast
        import inspect

        from faultmaven.bootstrap import composition

        tree = ast.parse(inspect.getsource(composition._wire_composition_root))
        calls = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name in {"bootstrap_application", "validate_revocation_storage"}:
                calls.setdefault(name, node.lineno)

        assert "bootstrap_application" in calls, "bootstrap call not found"
        assert "validate_revocation_storage" in calls, "storage gate not found"
        assert calls["validate_revocation_storage"] > calls["bootstrap_application"], (
            "the revocation storage gate runs BEFORE bootstrap_application, which "
            "is what creates the table it probes — a first-ever install would "
            "refuse to boot (#828 delta review)."
        )
