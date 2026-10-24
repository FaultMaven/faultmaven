"""Every way core launches the API goes through ``run_server``, and runs ``main.py`` once.

``test_run_server_options.py`` pins what ``run_server`` passes to uvicorn. That
is only the invariant if every launch path reaches it, so these pin the paths:

* the image: the Dockerfile's ``CMD`` is ``python -m faultmaven.server``, and its
  ``ENV`` sets the ``HOST`` / ``PORT`` / ``RELOAD`` that ``run_server`` reads
  from settings;
* the no-Docker dev launcher, ``scripts/faultmaven-dev.sh``, which launches the
  same module;
* the launcher module itself: its ``__main__`` guard calls ``run_server()``;
* ``main.py``, which refuses to be a launcher: a ``__main__`` guard at the top
  raises ``SystemExit`` naming ``faultmaven.server`` before any import-time work.

A launch that bypasses ``run_server`` (the ``uvicorn`` CLI) cannot set
``log_config=None``, so ``uvicorn.error`` records stop at uvicorn's own handler,
outside the redacting renderer.

The second invariant is that a launch executes ``faultmaven/main.py`` exactly
once (#1985). ``python -m faultmaven.main`` executed it as ``__main__`` and then
again when uvicorn imported the ``"faultmaven.main:app"`` string, so every
import-time side effect (duplicate logs, ``Request.form`` wrapped twice,
Prometheus scrape hooks registered twice) ran twice. These tests take the launch
module from the Dockerfile and the dev script, run it the way ``python -m`` does
in a child process with ``uvicorn.run`` replaced by a stub that imports the app
string as uvicorn does, and count the executions of ``main.py``'s code object
(an audit hook on the ``exec`` event, which sees ``runpy`` and the import system
alike and leaves the caller's frame alone). A positive control proves the counter
sees a second execution.

The shell-script check is a **text pin**: it reads the launch line and asserts
its shape, it does not run the script (that needs ``ss``, ``curl`` and a free
port). The script's behaviour was checked by hand with a stub ``python`` on a
free port; this keeps the line from drifting back to the CLI or losing an
override. Its launch module is also exercised for real, by the child-process
tests.
"""

import ast
import functools
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "Dockerfile"
DEV_SCRIPT = REPO_ROOT / "scripts" / "faultmaven-dev.sh"

#: What a launch must set because ``run_server`` takes host, port and reload
#: from settings, and a deployment that configures no LLM provider at all gets
#: the zero-config ``local`` preset (127.0.0.1:8000 with reload).
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


#: The one module the image and the dev script launch. It is not the app's
#: module, so ``__main__`` is never ``main.py`` (#1985).
LAUNCHER = "faultmaven.server"

#: What ``run_server`` hands uvicorn, for the env ``_run_launcher`` sets.
EXPECTED_UVICORN_KWARGS = {
    "host": "127.0.0.9",
    "port": 18999,
    "reload": False,
    "log_level": "info",
    "access_log": False,
    "timeout_keep_alive": 33,
    "log_config": None,
}


def _dockerfile_cmd() -> list[str]:
    cmds = [args for ins, args in _dockerfile_instructions() if ins == "CMD"]
    entrypoints = [ins for ins, _ in _dockerfile_instructions() if ins == "ENTRYPOINT"]

    assert entrypoints == [], "an ENTRYPOINT would wrap the CMD this test pins"
    assert len(cmds) == 1, f"expected one CMD, found {cmds!r}"
    return json.loads(cmds[0])


def test_dockerfile_cmd_is_run_server_through_the_launcher_module():
    assert _dockerfile_cmd() == ["python", "-m", LAUNCHER]


def test_dockerfile_sets_the_bind_run_server_reads_from_settings():
    env: dict[str, str] = {}
    for ins, args in _dockerfile_instructions():
        if ins == "ENV":
            env.update(pair.split("=", 1) for pair in shlex.split(args))

    assert env.get("HOST") == "0.0.0.0"
    assert env.get("PORT") == "8090"
    assert env.get("RELOAD") == "false"


def _dev_script_launch_tokens() -> list[str]:
    """The tokens of the dev script's one line that launches the API."""
    lines = [
        line
        for line in DEV_SCRIPT.read_text().splitlines()
        if re.search(r"\bpython -m [\w.]+ > ", line)  # the log redirect: a launch
        and not line.lstrip().startswith("#")
    ]
    assert len(lines) == 1, f"expected one launch line, found {lines!r}"
    return shlex.split(lines[0])


def _dev_script_launch() -> tuple[dict[str, str], list[str]]:
    """``(environment assignments, command)`` of the dev script's launch line."""
    tokens = _dev_script_launch_tokens()
    assignments = {}
    while tokens and re.fullmatch(r"[A-Z_]+=.*", tokens[0]):
        name, value = tokens.pop(0).split("=", 1)
        assignments[name] = value
    return assignments, tokens


def test_dev_script_launches_through_the_launcher_with_its_overrides():
    """Text pin (see the module docstring): the launch line's shape."""
    assignments, tokens = _dev_script_launch()

    assert tokens[:3] == ["python", "-m", LAUNCHER]
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


#: The child process. It runs a launch the way ``python -m <module>`` does
#: (``runpy`` with ``__main__`` as the run name), with two instruments in place
#: before anything is imported:
#:
#: * an audit hook on the ``exec`` event counts every execution of ``main.py``'s
#:   module code, however it arrives: ``runpy`` running it as ``__main__`` and
#:   the import system both ``exec`` the code object. An audit hook, not a
#:   wrapper around ``exec``: a wrapper changes what code that calls ``exec`` on
#:   a string sees (it broke ``scipy.stats`` and alembic's ``register_operation``
#:   in a real boot), the hook leaves the caller's frame alone. A hook cannot be
#:   removed, so this runs only in the child process;
#: * ``uvicorn.run`` is replaced by a stub that records the call and then
#:   imports the app string with uvicorn's own importer, as ``uvicorn.run`` does
#:   in-process (``reload=False``, one worker) before it serves. Without that
#:   import a second execution would never happen and the count would prove
#:   nothing.
#:
#: A ``SystemExit`` from the launch is recorded, not propagated: it is what
#: ``python -m faultmaven.main`` is meant to raise.
#:
#: ``control`` is not a launch: it executes ``main.py`` under a run name that is
#: not ``__main__`` (so the refusal guard stays quiet) and then imports the
#: string, the old launch, to show the counter sees both.
_CHILD = r"""
import importlib.util, json, os, runpy, sys, types
from unittest.mock import patch

from uvicorn.importer import import_from_string

launch, out = sys.argv[1], sys.argv[2]
main_py = os.path.realpath(importlib.util.find_spec("faultmaven.main").origin)

executions = []


def count_main_py_executions(event, args):
    if event == "exec" and args and isinstance(args[0], types.CodeType):
        if os.path.realpath(args[0].co_filename) == main_py:
            executions.append(args[0].co_name)


sys.addaudithook(count_main_py_executions)


def import_the_app(app, **kwargs):
    import_from_string(app)


system_exit = None
with patch("uvicorn.run", autospec=True, side_effect=import_the_app) as run:
    if launch == "control":
        runpy.run_module("faultmaven.main", run_name="__control__", alter_sys=True)
        import_from_string("faultmaven.main:app")
    else:
        try:
            runpy.run_module(launch, run_name="__main__", alter_sys=True)
        except SystemExit as exc:
            system_exit = {"code": exc.code}

json.dump(
    {
        "main_py_executions": executions,
        "system_exit": system_exit,
        "uvicorn_calls": run.call_count,
        "args": list(run.call_args.args) if run.call_args else None,
        "kwargs": dict(run.call_args.kwargs) if run.call_args else None,
    },
    open(out, "w"),
)
"""


@functools.cache
def _run_launcher(launch: str) -> tuple[subprocess.CompletedProcess, dict | None]:
    """Run ``launch`` (a module name, or ``control``) in a child process.

    A child process, because executing ``main.py`` wraps
    ``starlette.requests.Request.form`` and registers Prometheus scrape hooks,
    process-global changes that must not leak into the rest of this worker's
    tests. Cached by launch, so the Dockerfile and the dev script, which launch
    the same module, share one child.
    """
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
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "launch.json"
        result = subprocess.run(
            [sys.executable, "-c", _CHILD, launch, str(out)],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )
        return result, json.loads(out.read_text()) if out.exists() else None


def _launched(launch: str) -> dict:
    result, record = _run_launcher(launch)
    assert record is not None, (
        f"`python -m {launch}` failed before the child could report "
        f"(rc={result.returncode}):\n{result.stderr[-2000:]}"
    )
    return record


def _launch_modules() -> dict[str, str]:
    """The module each launch path runs, read from the launch path itself."""
    cmd = _dockerfile_cmd()
    assert cmd[:2] == ["python", "-m"] and len(cmd) == 3, cmd
    _, dev = _dev_script_launch()
    assert dev[:2] == ["python", "-m"], dev
    return {"Dockerfile CMD": cmd[2], "dev script": dev[2]}


@pytest.mark.parametrize("path", ["Dockerfile CMD", "dev script"])
def test_launch_path_reaches_uvicorn_run_with_every_option(path):
    """Each launch path's module calls ``run_server`` -> ``uvicorn.run``, once."""
    launch = _launch_modules()[path]

    record = _launched(launch)

    assert record["uvicorn_calls"] == 1, (
        f"`python -m {launch}` ({path}) never reached uvicorn.run "
        "(is the __main__ guard in faultmaven/server.py gone?)"
    )
    assert record["args"] == ["faultmaven.main:app"]
    assert record["kwargs"] == EXPECTED_UVICORN_KWARGS


@pytest.mark.parametrize("path", ["Dockerfile CMD", "dev script"])
def test_launch_path_executes_main_py_exactly_once(path):
    """#1985: a launch executes ``main.py`` once, when uvicorn imports the app.

    ``python -m faultmaven.main`` executed it twice: as ``__main__``, then again
    for uvicorn's import of ``faultmaven.main:app``.
    """
    launch = _launch_modules()[path]

    record = _launched(launch)

    # A launch that executes main.py once but never serves is not a launch.
    assert (
        record["uvicorn_calls"] == 1
    ), f"`python -m {launch}` ({path}) never reached uvicorn.run"
    assert record["main_py_executions"] == ["<module>"], (
        f"`python -m {launch}` ({path}) executed main.py "
        f"{len(record['main_py_executions'])} times, expected exactly 1"
    )


def test_the_execution_counter_sees_a_second_execution():
    """Positive control: the old launch (``main.py`` executed, then uvicorn's
    import of the string) counts two, so a count of one means one."""
    assert _launched("control")["main_py_executions"] == ["<module>", "<module>"]


def test_running_main_py_as_main_refuses_and_serves_nothing():
    """``python -m faultmaven.main`` raises ``SystemExit`` naming the launcher.

    Without the refusal it would execute ``main.py`` as ``__main__`` and, if a
    runner were added to it, execute it again for uvicorn (#1985). Exiting
    silently, with status 0 and nothing served, would hide a stale ``command:``.
    """
    record = _launched("faultmaven.main")

    assert record["system_exit"] is not None, "main.py ran to completion as __main__"
    code = record["system_exit"]["code"]
    # A string code is printed to stderr and exits 1; a non-zero int exits so.
    assert isinstance(code, str) or code not in (0, None), code
    assert "faultmaven.server" in str(code)
    assert record["uvicorn_calls"] == 0
    assert record["main_py_executions"] == ["<module>"]


def test_python_dash_m_main_exits_one_before_any_import_time_work():
    """The real process: status 1, the one-line message, and nothing else.

    The refusal sits above the ``.env`` load and every import, so no log line is
    produced. At the bottom of the file the status is the same, but only after
    every import-time side effect has run.
    """
    # Were the refusal ever lost, this would be a real launch. Keep it harmless:
    # loopback, a port that was free a moment ago, no reloader (whose child a
    # timeout kill would orphan), and a short timeout.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = str(probe.getsockname()[1])
    result = subprocess.run(
        [sys.executable, "-m", "faultmaven.main"],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "HOST": "127.0.0.1",
            "PORT": free_port,
            "RELOAD": "false",
            "WORKERS": "1",
            "PYTHONPATH": str(REPO_ROOT),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    lines = result.stderr.strip().splitlines()
    assert len(lines) == 1 and "faultmaven.server" in lines[0], result.stderr[-2000:]


def _compares_name(node: ast.Compare) -> bool:
    """True when any operand of the comparison, however nested, reads ``__name__``."""
    return any(
        isinstance(sub, ast.Name) and sub.id == "__name__"
        for operand in (node.left, *node.comparators)
        for sub in ast.walk(operand)
    )


def _is_name_equals_main(test: ast.expr) -> bool:
    """``__name__ == "__main__"``, with the operands in either order."""
    if not (
        isinstance(test, ast.Compare)
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Eq)
    ):
        return False
    operands = [test.left, *test.comparators]
    return any(
        isinstance(a, ast.Name)
        and a.id == "__name__"
        and isinstance(b, ast.Constant)
        and b.value == "__main__"
        for a, b in (operands, operands[::-1])
    )


def _bound_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.alias):
            names.add((node.asname or node.name).split(".")[0])
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
    return names


def test_main_py_opens_with_the_one_refusal_guard():
    """``main.py`` carries exactly one ``__main__`` guard, and it refuses.

    A guard that launched would make ``python -m faultmaven.main`` a launch path
    that executes ``main.py`` twice; one placed after the import-time work would
    refuse only once that work (49 log lines) had run. So: the one comparison of
    ``__name__`` in the module is the first statement after the docstring, its
    whole body is ``raise SystemExit("<message naming faultmaven.server>")`` and
    it has no ``else``. Every operand of every comparison is read, in either
    order, so ``"__main__" == __name__`` does not slip past.
    """
    tree = ast.parse((REPO_ROOT / "faultmaven" / "main.py").read_text())

    docstring, guard = tree.body[0], tree.body[1]
    assert (
        isinstance(docstring, ast.Expr)
        and isinstance(docstring.value, ast.Constant)
        and isinstance(docstring.value.value, str)
    ), "main.py must open with its docstring"
    assert isinstance(guard, ast.If) and _is_name_equals_main(
        guard.test
    ), "the first statement after the docstring must be `if __name__ == '__main__':`"

    assert guard.orelse == []
    assert len(guard.body) == 1 and isinstance(guard.body[0], ast.Raise)
    raised = guard.body[0].exc
    assert (
        isinstance(raised, ast.Call)
        and isinstance(raised.func, ast.Name)
        and raised.func.id == "SystemExit"
        and not raised.keywords
        and len(raised.args) == 1
        and isinstance(raised.args[0], ast.Constant)
        and isinstance(raised.args[0].value, str)
        and "faultmaven.server" in raised.args[0].value
    ), "the guard must raise SystemExit('<message naming faultmaven.server>')"
    assert guard.body[0].cause is None

    top_level_guards = [
        node
        for node in tree.body
        if isinstance(node, ast.If) and _is_name_equals_main(node.test)
    ]
    assert top_level_guards == [guard]
    comparisons = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Compare) and _compares_name(node)
    ]
    assert comparisons == [guard.test], "no other comparison may involve __name__"

    assert "run_server" not in _bound_names(
        tree
    ), "run_server lives in faultmaven/server.py, the one launcher"
