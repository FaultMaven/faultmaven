"""Run ``python -m alembic`` commands in forked children of one warm interpreter (#2171).

Most of a fresh ``python -m alembic`` call is interpreter start-up and the
imports ``alembic/env.py`` pulls in. The first :func:`run` in a test process
starts one helper, which imports those once and then forks one child per
command. The child takes the request's cwd, environment, argv and
``sys.path[0]`` as ``python -m alembic`` would, runs alembic's CLI, and exits
with the code a real interpreter gives. Whatever env.py does to the process
(``fileConfig``, ``load_dotenv``, the settings read) dies with the child.

A fork cannot honour everything a fresh interpreter does, so:

- A request whose ``PYTHON*`` keys (``PYTHONPATH`` included) differ from the
  helper's raises: they act at interpreter start, which a child never has.
- env.py's ``fileConfig`` disables the loggers that exist when it runs. A fresh
  process creates the loggers of env.py's later ``faultmaven`` imports after
  that call; here they already exist, so the child re-enables exactly those.
- The child writes through the interpreter's own ``sys.__stdout__`` and
  ``sys.__stderr__``, pointed at the capture files, so encoding, error handler
  and buffering are a real interpreter's.

A dead helper or a failure outside alembic raises. Only where ``os.fork`` is
absent does :func:`run` fall back to a plain subprocess.
"""

from __future__ import annotations

import atexit
import base64
import json
import locale
import os
import subprocess
import sys
import tempfile
import threading
import traceback
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Optional

#: The checkout the helper runs from and must import ``faultmaven`` from.
CHECKOUT = Path(__file__).resolve().parents[2]


def run(
    argv: Sequence[str], *, cwd: str | os.PathLike[str], env: Mapping[str, str]
) -> subprocess.CompletedProcess[str]:
    """``subprocess.run([sys.executable, "-m", "alembic", *argv], cwd=cwd,
    env=env, capture_output=True, text=True)``, served by a forked child."""
    args = [sys.executable, "-m", "alembic", *argv]
    if not hasattr(os, "fork"):
        return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True)
    request = {"argv": list(argv), "cwd": os.fspath(cwd), "env": dict(env)}
    reply = _helper(env).request(request)
    return subprocess.CompletedProcess(
        args, reply["returncode"], _text(reply["stdout"]), _text(reply["stderr"])
    )


def _text(data: str) -> str:
    """Decode captured bytes as ``subprocess.run(text=True)`` does."""
    encoding = "utf-8" if sys.flags.utf8_mode else locale.getencoding()
    text = base64.b64decode(data).decode(encoding)
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _python_keys(env: Mapping[str, str]) -> dict[str, str]:
    return {key: value for key, value in env.items() if key.startswith("PYTHON")}


class _Helper:
    """The test process's end: one JSON line each way per command."""

    def __init__(self, env: Mapping[str, str]) -> None:
        self._log = tempfile.TemporaryFile()
        self._proc = subprocess.Popen(
            [sys.executable, "-m", __name__],
            cwd=CHECKOUT,
            env=dict(env),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._log,
        )
        if self._read() != {"ready": True}:
            raise RuntimeError("alembic fork server: unexpected greeting")

    def request(self, request: dict[str, Any]) -> dict[str, Any]:
        try:
            self._proc.stdin.write(json.dumps(request).encode() + b"\n")
            self._proc.stdin.flush()
        except BrokenPipeError:
            self._died()
        reply = self._read()
        if "error" in reply:
            raise RuntimeError(f"alembic fork server: {reply['error']}")
        return reply

    def _read(self) -> dict[str, Any]:
        line = self._proc.stdout.readline()
        if not line:
            self._died()
        return json.loads(line)

    def _died(self) -> None:
        code = self._proc.wait()
        self._log.seek(0)
        log = self._log.read().decode(errors="backslashreplace")
        raise RuntimeError(f"alembic fork server exited {code}:\n{log}")

    def close(self) -> None:
        self._proc.stdin.close()
        try:
            self._proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait()
        self._log.close()


_HELPER: Optional[_Helper] = None


def _helper(env: Mapping[str, str]) -> _Helper:
    """The process's helper, started by the first request with its env and
    shut down when the test session's process exits."""
    global _HELPER
    if _HELPER is None:
        _HELPER = _Helper(env)
        atexit.register(_HELPER.close)
    return _HELPER


def _serve() -> None:
    """The helper: import once, then fork one child per request until EOF."""
    import logging
    import logging.config

    start_keys, start_path = _python_keys(os.environ), list(sys.path)

    def loggers() -> set[str]:
        items = logging.Logger.manager.loggerDict.items()
        return {name for name, obj in items if isinstance(obj, logging.Logger)}

    # env.py's imports before its fileConfig call, then the ones after it.
    import dotenv  # noqa: F401
    import sqlalchemy  # noqa: F401

    import alembic.config

    before = loggers()
    import faultmaven
    import faultmaven.cli._database_gate  # noqa: F401
    import faultmaven.config.settings  # noqa: F401
    import faultmaven.infrastructure.persistence.models  # noqa: F401

    reenable = sorted(loggers() - before)
    if not Path(faultmaven.__file__).resolve().is_relative_to(CHECKOUT):
        raise RuntimeError(f"faultmaven is {faultmaven.__file__}, not in {CHECKOUT}")
    if threading.active_count() != 1:
        raise RuntimeError("the helper runs threads, and a fork copies only one")

    real_file_config = logging.config.fileConfig

    def file_config(*args: Any, **kwargs: Any) -> None:
        real_file_config(*args, **kwargs)
        for name in reenable:
            logging.getLogger(name).disabled = False

    main_py = os.path.join(os.path.dirname(alembic.config.__file__), "__main__.py")
    proto_in, proto_out = os.fdopen(os.dup(0), "rb"), os.fdopen(os.dup(1), "wb")
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(devnull, 1)

    def send(reply: dict[str, Any]) -> None:
        proto_out.write(json.dumps(reply).encode() + b"\n")
        proto_out.flush()

    def child(request: dict[str, Any], out: int, err: int, report: int) -> None:
        """Become ``python -m alembic`` for ``request``; never returns."""
        code = 70
        try:
            os.close(proto_in.fileno())
            os.close(proto_out.fileno())
            os.dup2(out, 1)
            os.dup2(err, 2)
            os.chdir(request["cwd"])
            os.environ.clear()
            os.environ.update(request["env"])
            sys.path[:] = [os.getcwd(), *start_path[1:]]  # what `-m` puts first
            sys.argv = [main_py, *request["argv"]]
            sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__
            logging.config.fileConfig = file_config
            code = _alembic_main(request["argv"])
            for stream in (sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__):
                stream.flush()
        except BaseException:
            os.write(report, traceback.format_exc().encode())
        finally:
            os._exit(code & 0xFF)

    send({"ready": True})
    for line in proto_in:
        request = json.loads(line)
        keys = _python_keys(request["env"])
        if keys != start_keys:
            names = keys.keys() | start_keys.keys()
            differ = sorted(k for k in names if keys.get(k) != start_keys.get(k))
            send({"error": f"a forked child cannot honour {differ}"})
            continue
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            report_r, report_w = os.pipe()
            sys.stdout.flush()  # nothing buffered may be copied into the child
            sys.stderr.flush()
            pid = os.fork()
            if pid == 0:
                os.close(report_r)
                child(request, out.fileno(), err.fileno(), report_w)
            os.close(report_w)
            with os.fdopen(report_r, "rb") as report:
                failure = report.read().decode(errors="backslashreplace")
            status = os.waitpid(pid, 0)[1]
            if failure:
                send({"error": f"the child failed outside alembic:\n{failure}"})
                continue
            out.seek(0)
            err.seek(0)
            send(
                {
                    "returncode": os.waitstatus_to_exitcode(status),
                    "stdout": base64.b64encode(out.read()).decode(),
                    "stderr": base64.b64encode(err.read()).decode(),
                }
            )


def _alembic_main(argv: list[str]) -> int:
    """Run alembic's CLI; return the exit code a real interpreter would give."""
    import alembic.config

    try:
        alembic.config.main(argv=argv, prog="alembic")
    except SystemExit as exc:
        if exc.code is None or isinstance(exc.code, int):
            return exc.code or 0
        print(exc.code, file=sys.stderr)
        return 1
    except BaseException:
        sys.excepthook(*sys.exc_info())
        return 1
    return 0


if __name__ == "__main__":
    _serve()
