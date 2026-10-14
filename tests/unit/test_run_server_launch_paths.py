"""Every way core launches the API goes through ``run_server``.

``test_run_server_options.py`` pins what ``run_server`` passes to uvicorn. That
is only the invariant if every launch path reaches it, so these pin the paths:

* the image: the Dockerfile's ``CMD`` is ``python -m faultmaven.main``, and its
  ``ENV`` sets the ``HOST`` / ``PORT`` / ``RELOAD`` that ``run_server`` reads
  from settings;
* ``python -m faultmaven.main`` itself: the ``__main__`` guard calls
  ``run_server()``;
* the no-Docker dev launcher, ``scripts/faultmaven-dev.sh``.

A launch that bypasses ``run_server`` (the ``uvicorn`` CLI) cannot set
``log_config=None``, so ``uvicorn.error`` records stop at uvicorn's own handler,
outside the redacting renderer.

The shell-script check is a **text pin**: it reads the launch line and asserts
its shape, it does not run the script (that needs ``ss``, ``curl`` and a free
port). The script's behaviour was checked by hand with a stub ``python`` on a
free port; this keeps the line from drifting back to the CLI or losing an
override.
"""

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "Dockerfile"
DEV_SCRIPT = REPO_ROOT / "scripts" / "faultmaven-dev.sh"

#: What a launch must set because ``run_server`` takes host, port and reload
#: from settings, and a configuration the zero-config check does not recognise
#: gets the ``local`` preset (127.0.0.1:8000 with reload, #1984).
LAUNCH_ENV = ("HOST", "PORT", "RELOAD")


def _dockerfile_instructions() -> list[tuple[str, str]]:
    """``(INSTRUCTION, arguments)`` with ``\\`` continuations joined and
    comment lines dropped."""
    logical: list[str] = []
    pending = ""
    for raw in DOCKERFILE.read_text().splitlines():
        if not pending and (not raw.strip() or raw.lstrip().startswith("#")):
            continue
        if raw.lstrip().startswith("#"):
            continue
        if raw.rstrip().endswith("\\"):
            pending += raw.rstrip()[:-1] + " "
            continue
        logical.append(pending + raw)
        pending = ""
    return [
        (parts[0].upper(), parts[1] if len(parts) > 1 else "")
        for parts in (line.strip().split(None, 1) for line in logical)
    ]


def test_dockerfile_cmd_is_run_server_through_python_dash_m():
    cmds = [args for ins, args in _dockerfile_instructions() if ins == "CMD"]
    entrypoints = [ins for ins, _ in _dockerfile_instructions() if ins == "ENTRYPOINT"]

    assert entrypoints == [], "an ENTRYPOINT would wrap the CMD this test pins"
    assert [json.loads(c) for c in cmds] == [["python", "-m", "faultmaven.main"]]


def test_dockerfile_sets_the_bind_run_server_reads_from_settings():
    env: dict[str, str] = {}
    for ins, args in _dockerfile_instructions():
        if ins == "ENV":
            env.update(pair.split("=", 1) for pair in shlex.split(args))

    assert env.get("HOST") == "0.0.0.0"
    assert env.get("PORT") == "8090"
    assert env.get("RELOAD") == "false"


def test_running_the_module_calls_run_server_with_every_uvicorn_option(tmp_path):
    """``python -m faultmaven.main`` reaches ``uvicorn.run`` with the options.

    Run in a child process: executing ``main.py`` again as ``__main__`` wraps
    ``starlette.requests.Request.form`` a second time, a process-global change
    that must not leak into the rest of this worker's tests.
    """
    child = (
        "import json, runpy, sys\n"
        "from unittest.mock import patch\n"
        "with patch('uvicorn.run', autospec=True) as run:\n"
        "    runpy.run_module('faultmaven.main', run_name='__main__')\n"
        "json.dump({'count': run.call_count, 'args': list(run.call_args.args),\n"
        "           'kwargs': run.call_args.kwargs}, open(sys.argv[1], 'w'))\n"
    )
    env = {
        **os.environ,
        # Pinned: run_server reads these from settings, and env beats .env.
        "HOST": "127.0.0.9",
        "PORT": "18999",
        "RELOAD": "false",
        "WORKERS": "1",
        "TIMEOUT_KEEP_ALIVE": "33",
        "PYTHONPATH": str(REPO_ROOT),
    }

    out = tmp_path / "call.json"
    result = subprocess.run(
        [sys.executable, "-c", child, str(out)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, (
        "python -m faultmaven.main never reached uvicorn.run "
        f"(is the __main__ guard gone?):\n{result.stderr[-2000:]}"
    )
    call = json.loads(out.read_text())

    assert call["count"] == 1
    assert call["args"] == ["faultmaven.main:app"]
    assert call["kwargs"] == {
        "host": "127.0.0.9",
        "port": 18999,
        "reload": False,
        "log_level": "info",
        "access_log": False,
        "timeout_keep_alive": 33,
        "log_config": None,
    }


def _dev_script_launch_tokens() -> list[str]:
    """The tokens of the dev script's one line that launches the API."""
    lines = [
        line
        for line in DEV_SCRIPT.read_text().splitlines()
        if "faultmaven.main" in line and not line.lstrip().startswith("#")
    ]
    assert len(lines) == 1, f"expected one launch line, found {lines!r}"
    return shlex.split(lines[0])


def test_dev_script_launches_through_run_server_with_its_overrides():
    """Text pin (see the module docstring): the launch line's shape."""
    tokens = _dev_script_launch_tokens()
    assignments = {}
    while tokens and re.fullmatch(r"[A-Z_]+=.*", tokens[0]):
        name, value = tokens.pop(0).split("=", 1)
        assignments[name] = value

    assert tokens[:3] == ["python", "-m", "faultmaven.main"]
    for name in LAUNCH_ENV:
        assert name in assignments, f"{name} must be set on the launch line"
    assert assignments["HOST"] == "0.0.0.0"
    assert assignments["RELOAD"] == "false"
    # A WORKERS in .env would fork workers the PID file cannot track.
    assert assignments["WORKERS"] == "1"
    assert assignments["PORT"] == "$port"
    assert assignments["TIMEOUT_KEEP_ALIVE"] == "$keepalive"


def test_dev_script_never_starts_the_uvicorn_cli():
    code = [
        line
        for line in DEV_SCRIPT.read_text().splitlines()
        if not line.lstrip().startswith("#")
    ]
    # A command, not prose: echo lines may still say "stale uvicorn".
    assert not [line for line in code if re.search(r"\buvicorn\s+[\w.]+:\w+", line)]
