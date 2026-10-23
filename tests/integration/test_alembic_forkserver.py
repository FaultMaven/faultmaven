"""The alembic fork server's contract (#2171): a command it runs behaves as a
fresh ``python -m alembic`` with the same argv, cwd and environment."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.integration import alembic_forkserver

PROJECT_ROOT = Path(__file__).parent.parent.parent

#: An env.py with the real one's import order around ``fileConfig``. It dumps
#: what the interpreter looks like, then changes the environment and a logger,
#: and exits with ``-x exit=<code>``.
PROBE_ENV_PY = """\
import hashlib, json, logging, os, sys
from logging.config import fileConfig
from dotenv import load_dotenv
from sqlalchemy import engine_from_config
from alembic import context

fileConfig(context.config.config_file_name)
import faultmaven.cli._database_gate, faultmaven.config.settings
import faultmaven.infrastructure.persistence.models

x = context.get_x_argument(as_dictionary=True)
pairs = ((sys.stdout, sys.__stdout__), (sys.stderr, sys.__stderr__))
items = logging.Logger.manager.loggerDict.items()
state = {
    "cwd": os.getcwd(), "path": sys.path, "argv": sys.argv,
    "environ": hashlib.sha256(repr(sorted(os.environ.items())).encode()).hexdigest(),
    "probe": os.environ.get("FM_FORKSERVER_PROBE"),
    "leak": os.environ.get("FM_FORKSERVER_LEAK"),
    "streams": [[s.encoding, s.errors, s.line_buffering, s is d] for s, d in pairs],
    "loggers": {n: [o.disabled, o.level, o.propagate, len(o.handlers)]
                for n, o in items if isinstance(o, logging.Logger)},
}
with open(x["dump"], "w", encoding="utf-8") as f:
    json.dump(state, f)
os.environ["FM_FORKSERVER_LEAK"] = "1"
logging.getLogger("fm.forkserver.probe").setLevel(logging.DEBUG)
print("to stdout: é")
print("to stderr: ❌", file=sys.stderr)
sys.exit(int(x.get("exit", 0)))
"""


@pytest.fixture
def probe(tmp_path: Path) -> Path:
    """A project whose env.py is :data:`PROBE_ENV_PY`, logging as the repo's."""
    script = tmp_path / "script"
    (script / "versions").mkdir(parents=True)
    (script / "env.py").write_text(PROBE_ENV_PY, encoding="utf-8")
    ini = (PROJECT_ROOT / "alembic.ini").read_text(encoding="utf-8")
    ini = ini.replace("script_location = alembic", f"script_location = {script}")
    (tmp_path / "alembic.ini").write_text(ini, encoding="utf-8")
    return tmp_path


def _env() -> dict[str, str]:
    """The environment ``run_alembic`` builds, less the database."""
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(PROJECT_ROOT), env.get("PYTHONPATH")) if part
    )
    return env


def _argv(probe: Path, *extra: str) -> list[str]:
    ini, dump = probe / "alembic.ini", probe / "dump.json"
    return ["-c", str(ini), "-x", f"dump={dump}", *extra, "current"]


def _dump(probe: Path) -> dict:
    return json.loads((probe / "dump.json").read_text(encoding="utf-8"))


def test_a_child_is_a_fresh_interpreter(probe):
    """Result, cwd, argv, sys.path, environment, streams and every logger's
    state after env.py's ``fileConfig`` are a fresh interpreter's."""
    argv, env = _argv(probe, "-x", "exit=3"), _env()
    forked = alembic_forkserver.run(argv, cwd=probe, env=env)
    forked_state = _dump(probe)
    fresh = subprocess.run(
        [sys.executable, "-m", "alembic", *argv],
        cwd=probe,
        env=env,
        capture_output=True,
        text=True,
    )
    fresh_state = _dump(probe)

    assert isinstance(forked, subprocess.CompletedProcess)
    assert (fresh.returncode, fresh.stdout) == (3, "to stdout: é\n"), fresh.stderr
    assert fresh.stderr == "to stderr: ❌\n"
    assert (forked.args, forked.returncode, forked.stdout, forked.stderr) == (
        fresh.args,
        fresh.returncode,
        fresh.stdout,
        fresh.stderr,
    )
    assert fresh_state["cwd"] == str(probe.resolve())
    assert fresh_state["argv"][1:] == argv
    assert forked_state == fresh_state
    # Created after fileConfig in a fresh process, so live; dotenv's existed
    # before it, so fileConfig disabled it.
    assert fresh_state["loggers"]["faultmaven.config.presets"][0] is False
    assert fresh_state["loggers"]["dotenv.main"][0] is True


def test_a_command_leaves_nothing_to_the_next(probe):
    """Each command runs in its request's environment, and its environment and
    logging changes die with its child."""
    for value in ("first", "second"):
        env = {**_env(), "FM_FORKSERVER_PROBE": value}
        result = alembic_forkserver.run(_argv(probe), cwd=probe, env=env)
        assert result.returncode == 0, result.stderr
        state = _dump(probe)
        assert (state["probe"], state["leak"]) == (value, None)
        assert "fm.forkserver.probe" not in state["loggers"]
    assert "FM_FORKSERVER_LEAK" not in os.environ


@pytest.mark.parametrize("key", ["PYTHONPATH", "PYTHONDONTWRITEBYTECODE"])
def test_an_interpreter_start_variable_a_fork_cannot_honour_raises(probe, key):
    env = _env()
    assert alembic_forkserver.run(_argv(probe), cwd=probe, env=env).returncode == 0
    changed = {**env, key: f"{env.get(key, '')}{os.pathsep}{probe}"}
    with pytest.raises(RuntimeError, match=key):
        alembic_forkserver.run(_argv(probe), cwd=probe, env=changed)
    assert alembic_forkserver.run(_argv(probe), cwd=probe, env=env).returncode == 0
