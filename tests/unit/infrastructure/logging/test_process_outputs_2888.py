"""Every output a process opens itself leaves through the chain or as a fixed
line (#2888 S2b, #2898; principles §7, rule 5): ``logging.lastResort``,
``handleError``, the uncaught-exception hooks, ``warnings``, asyncio's handler,
uvicorn's handlers, a third party's own handlers, structlog in a job, a worker
child's stderr, the startup migration's subprocess, an ``fm-*`` command, a
uvicorn child's failed import and a lifespan failure.

A process-level hook is at stake in most of these, so each runs in a fresh
interpreter. The marker is built at run time and passed in (``chained_2888``
says why); it is in no test name or path.
"""

import asyncio
import io
import json
import logging
import os
import secrets
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import faultmaven
from faultmaven.bootstrap import data_init
from faultmaven.infrastructure import worker_process
from faultmaven.infrastructure.logging.config import (
    _INSTALLED_HANDLER_MARKER,
    LateBindingStreamHandler,
    build_log_formatter,
)
from tests.unit.infrastructure.logging import stderr_worker_2888

pytestmark = [pytest.mark.unit, pytest.mark.security]

ROOT = Path(faultmaven.__file__).resolve().parents[1]
SCRIPTS = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["scripts"]

#: The two entries a process installs the chain through: a job's, alembic's and
#: a command's (``install_log_handler``), and the API worker's (``get_logger``).
JOBS = (
    "from faultmaven.infrastructure.logging.config import install_log_handler\n"
    "install_log_handler(sys.stderr, logging.INFO)\n"
)
API = (
    "from faultmaven.infrastructure.logging.config import get_logger\nget_logger('t')\n"
)
PRELUDE = "import gc, json, logging, sys, threading, warnings\nmarker = sys.argv[1]\n"


@pytest.fixture
def marker() -> str:
    return secrets.token_hex(16)


def _run(code: str, marker: str, **env: str) -> subprocess.CompletedProcess:
    environment = {
        **os.environ,
        "PYTHONPATH": str(ROOT),
        "FAULTMAVEN_SKIP_DOTENV": "1",
        "LOG_FORMAT": "json",
        **env,
    }
    return subprocess.run(
        [sys.executable, "-c", PRELUDE + code, marker],
        cwd=ROOT, env=environment, capture_output=True, text=True, timeout=300,
    )  # fmt: skip


def _lines(text: str) -> list:
    """The JSON lines of ``text``; any other line fails the test."""
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _clean(result: subprocess.CompletedProcess, marker: str) -> list:
    """No marker on either stream; the stderr's lines."""
    assert marker not in result.stdout + result.stderr, result.stderr[-3000:]
    return _lines(result.stderr)


def _framed(lines: list, event: str, link: str = "RuntimeError:") -> dict:
    (line,) = [line for line in lines if line["event"] == event]
    assert line["frames"].startswith(link), line
    return line


def test_a_record_before_any_configure_is_a_json_line(marker):
    code = (
        "import faultmaven\n"
        "logging.getLogger('t.early').error('early', exc_info=RuntimeError(marker))\n"
    )
    _framed(_clean(_run(code, marker), marker), "early")


class _Raising:
    def __init__(self, marker: str) -> None:
        self.marker = marker

    def __repr__(self) -> str:
        raise ValueError(self.marker)


REFUSED = (
    "class Raising:\n"
    "    def __repr__(self):\n"
    "        raise ValueError(marker)\n"
    "logging.getLogger('p').error('y %s', Raising())\n"
)
#: A structlog event: before any install it is structlog's own line (§7).
REFUSED_EVENT = (
    "import structlog\n"
    "structlog.get_logger('p').error('x', counts={(1, 2): 3}, "
    "url='https://x/cb?code=SECRET' + marker)\n"
)
FIXED = {"event": "log record not rendered", "level": "error", "logger": "p"}


@pytest.mark.parametrize(
    "install", [JOBS, "import faultmaven\n"], ids=["install_log_handler", "lastResort"]
)
def test_a_record_the_renderer_refuses_is_the_fixed_line(install, marker):
    """The evidence's two shapes on #2898: a tuple key under JSON, a raising
    repr. On the lastResort path structlog is unconfigured: its own line."""
    result = _run(
        install + REFUSED + (REFUSED_EVENT if install == JOBS else ""), marker
    )
    assert "SECRET" not in result.stdout + result.stderr
    lines = _clean(result, marker)
    assert {**FIXED, "error": "ValueError"} in lines
    if install == JOBS:
        assert {**FIXED, "error": "TypeError"} in lines


@pytest.mark.parametrize("message, args", [("y %s", None), ("x", {(1, 2): 3})])
def test_the_api_handler_writes_the_fixed_line(message, args, marker, capsys):
    handler = LateBindingStreamHandler()
    handler.setFormatter(build_log_formatter("json", False, sys.stderr))
    handler.stream = io.StringIO()
    record = logging.LogRecord("p", logging.ERROR, __file__, 1, message, None, None)
    if args is None:
        record.args = (_Raising(marker),)
    else:
        record.counts = args
    handler.handle(record)
    err = capsys.readouterr().err
    assert marker not in err and handler.stream.getvalue() == ""
    assert json.loads(err)["event"] == "log record not rendered"


HOOKS = {
    "the main thread": "raise RuntimeError(marker)\n",
    "a thread": "t = threading.Thread(target=fail)\nt.start()\nt.join()\n",
    "a __del__": "class D:\n    def __del__(self):\n        fail()\nD()\ngc.collect()\n",
}
EVENTS = {
    "the main thread": "Uncaught exception",
    "a thread": "Uncaught exception in a thread",
    "a __del__": "Unraisable exception",
}


@pytest.mark.parametrize("where", sorted(HOOKS))
def test_an_uncaught_exception_is_frames_and_class_only(where, marker):
    code = JOBS + "def fail():\n    raise RuntimeError(marker)\n" + HOOKS[where]
    _framed(_clean(_run(code, marker), marker), EVENTS[where])


def test_a_hook_already_set_stands_and_a_default_one_is_replaced(marker):
    code = (
        "def mine(*args):\n    pass\n"
        "sys.excepthook = mine\n" + JOBS + "print(json.dumps([\n"
        "    sys.excepthook is mine,\n"
        "    threading.excepthook is not threading.__excepthook__,\n"
        "    sys.unraisablehook is not sys.__unraisablehook__,\n"
        "    warnings.showwarning is not warnings._showwarning_orig]))\n"
    )
    result = _run(code, marker)
    assert json.loads(result.stdout) == [True, True, True, True], result.stderr


def test_a_never_awaited_coroutines_warning_is_class_file_and_line(marker):
    code = JOBS + "async def never():\n    pass\nnever()\ngc.collect()\n"
    lines = _clean(_run(code, marker), marker)
    (line,) = [line for line in lines if line["event"] == "RuntimeWarning"]
    assert {"warning_file", "warning_line"} <= set(line)
    assert "never awaited" not in json.dumps(lines)


@pytest.mark.parametrize("install", [JOBS, API], ids=["jobs", "api"])
def test_a_warnings_message_is_not_written(install, marker):
    """A warning ``catch_warnings(record=True)`` records stays recorded, the
    hook installed before the block or inside it."""
    code = (
        install + "warnings.warn(marker)\n"
        "with warnings.catch_warnings(record=True) as before:\n"
        "    warnings.simplefilter('always')\n"
        "    warnings.warn('recorded')\n"
        "warnings.showwarning = warnings._showwarning_orig\n"
        "with warnings.catch_warnings(record=True) as inside:\n"
        "    warnings.simplefilter('always')\n"
        "    from faultmaven.infrastructure.logging import process_outputs\n"
        "    process_outputs.install_process_outputs()\n"
        "    warnings.warn('recorded')\n"
        "print(json.dumps([len(before), len(inside)]))\n"
    )
    result = _run(code, marker)
    lines = _clean(result, marker)
    assert json.loads(result.stdout.splitlines()[-1]) == [1, 1]
    (line,) = [line for line in lines if line["event"] == "UserWarning"]
    assert {"warning_file", "warning_line"} <= set(line)


def test_asyncio_writes_no_future_or_handle_repr(marker):
    code = JOBS + (
        "import asyncio\n"
        "async def boom():\n    raise RuntimeError(marker)\n"
        "def callback(value):\n    raise RuntimeError(value)\n"
        "async def main():\n"
        "    task = asyncio.ensure_future(boom())\n"
        "    await asyncio.sleep(0)\n"
        "    del task\n"
        "    gc.collect()\n"
        "    asyncio.get_running_loop().call_soon(callback, marker)\n"
        "    await asyncio.sleep(0)\n"
        "asyncio.run(main())\n"
    )
    lines = _clean(_run(code, marker), marker)
    _framed(lines, "Task exception was never retrieved")
    _framed(lines, "Exception in callback")


@pytest.mark.parametrize("install", [JOBS, API], ids=["jobs", "api"])
def test_uvicorns_own_handlers_are_stripped(install, marker):
    code = (
        "import logging.config\nfrom uvicorn.config import LOGGING_CONFIG\n"
        "logging.config.dictConfig(LOGGING_CONFIG)\n" + install + "print(json.dumps("
        "{n: [len(logging.getLogger(n).handlers), logging.getLogger(n).propagate]"
        " for n in ('uvicorn', 'uvicorn.error', 'uvicorn.access')}))\n"
    )
    result = _run(code, marker)
    assert json.loads(result.stdout.splitlines()[-1]) == {
        "uvicorn": [0, True],
        "uvicorn.error": [0, True],
        "uvicorn.access": [0, False],
    }, result.stderr


def test_database_echo_logs_through_the_chain(marker, tmp_path):
    code = JOBS + (
        "import asyncio\nfrom faultmaven.infrastructure.persistence import database\n"
        "async def main():\n"
        "    async with database.get_engine().connect() as connection:\n"
        "        await connection.exec_driver_sql('SELECT 1')\n"
        "asyncio.run(main())\n"
    )
    url = f"sqlite+aiosqlite:///{tmp_path / 'echo.db'}"
    result = _run(code, marker, DATABASE_ECHO="true", DATABASE_URL=url)
    assert result.stdout == ""
    lines = _clean(result, marker)
    assert {"sqlalchemy.engine.Engine"} <= {line["logger"] for line in lines}


def test_the_embedding_models_libraries_log_through_the_chain(marker):
    """The model's class is a stand-in that imports what the real one does."""
    pytest.importorskip("sentence_transformers")
    code = JOBS + (
        "from faultmaven.infrastructure import model_cache\n"
        "class Model:\n"
        "    def __init__(self, _name):\n"
        "        import sentence_transformers\n"
        "model_cache.SentenceTransformer = Model\n"
        "assert model_cache.ModelCache().get_bge_m3_model() is not None\n"
        "print(json.dumps(sorted(\n"
        "    name for name, l in logging.root.manager.loggerDict.items()\n"
        "    if isinstance(l, logging.Logger) and any(\n"
        "        type(h) is not logging.NullHandler for h in l.handlers))))\n"
    )
    result = _run(code, marker, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    assert json.loads(result.stdout.splitlines()[-1]) == [], result.stderr[-3000:]


def test_a_jobs_structlog_event_is_a_chain_line(marker):
    code = (
        "import structlog\nfrom faultmaven.jobs.run import setup_logging\n"
        "setup_logging()\nstructlog.get_logger('faultmaven.jobs.t').warning('e', k=1)\n"
    )
    result = _run(code, marker)
    (line,) = _lines(result.stdout)
    assert (line["event"], line["k"], line["logger"]) == ("e", 1, "faultmaven.jobs.t")


@pytest.mark.parametrize("command", sorted(SCRIPTS))
def test_every_command_installs_the_chain(command, marker):
    module, function = SCRIPTS[command].split(":")
    code = (
        f"sys.argv = [{command!r}, '--help']\nfrom {module} import {function}\n"
        f"try:\n    {function}()\nexcept SystemExit:\n    pass\n"
        "print(json.dumps(any(getattr(h, "
        f"{_INSTALLED_HANDLER_MARKER!r}, False) for h in logging.getLogger().handlers)))\n"
    )
    result = _run(code, marker)
    assert result.stdout.splitlines()[-1] == "true", result.stderr[-2000:]


def test_a_childs_failed_app_import_logs_frames_and_no_traceback(marker):
    code = (
        "import multiprocessing\n"
        "from tests.unit.infrastructure.logging.asgi_child_2888 import import_the_app\n"
        "child = multiprocessing.get_context('spawn').Process(\n"
        "    target=import_the_app, args=(marker,))\n"
        "child.start()\nchild.join()\nprint(child.exitcode)\n"
    )
    result = _run(code, marker)
    assert result.stdout.strip() == "1" and "Traceback" not in result.stderr
    _framed(_clean(result, marker), "The API failed to import")


def test_a_startup_failure_logs_frames_and_no_traceback_text(marker):
    code = API + (
        "import asyncio, uvicorn\nfrom unittest.mock import patch\n"
        "from fastapi import FastAPI\nfrom uvicorn.lifespan.on import LifespanOn\n"
        "from faultmaven.bootstrap import lifespan as module\n"
        "async def refuse(*args, **kwargs):\n    raise RuntimeError(marker)\n"
        "with patch.object(module, 'compose_application', refuse), \\\n"
        "        patch.object(module.opik_sdk, 'configure'), \\\n"
        "        patch('faultmaven.bootstrap.configuration_gates.validate_configuration'):\n"
        "    config = uvicorn.Config(FastAPI(lifespan=module.lifespan), log_config=None)\n"
        "    config.load()\n"
        "    asyncio.run(LifespanOn(config).startup())\n"
    )
    lines = _clean(_run(code, marker), marker)
    _framed(lines, "API startup failed")
    assert "Traceback (most recent call last):" in {line["event"] for line in lines}


async def _run_worker(marker: str, end: str):
    return await worker_process.run(
        stderr_worker_2888.__name__,
        {"marker": marker, "end": end},
        memory_bytes=4 << 30, cpu_seconds=60, output_limit_bytes=1 << 16,
    )  # fmt: skip


def _no_marker_logged(caplog, marker: str) -> list:
    for record in caplog.records:
        assert marker not in record.getMessage() + repr(record.__dict__)
    return [r for r in caplog.records if r.getMessage().startswith("Worker ")]


@pytest.mark.parametrize("end, level", [("return", "INFO"), ("crash", "WARNING")])
async def test_a_workers_stderr_is_dropped_and_its_size_logged(
    end, level, marker, caplog, capfd
):
    """Past a pipe's buffer, under an output limit smaller than it: the drain
    keeps the worker from blocking, and the limit is the stdout's alone."""
    caplog.set_level(logging.INFO)
    result = await _run_worker(marker, end)
    assert result.result == ({"done": True} if end == "return" else None)
    out, err = capfd.readouterr()
    assert marker not in out + err
    (record,) = _no_marker_logged(caplog, marker)
    assert record.levelname == level
    assert record.args[2] >= stderr_worker_2888.PADDING


async def test_a_cancellation_mid_drain_leaves_no_task(marker, monkeypatch, capfd):
    reading = asyncio.Event()
    drain = worker_process._drain

    async def watched(stream, seen):
        read = stream.read

        async def first(n=-1):
            chunk = await read(n)
            reading.set()
            return chunk

        stream.read = first
        await drain(stream, seen)

    monkeypatch.setattr(worker_process, "_drain", watched)
    running = asyncio.ensure_future(_run_worker(marker, "hold"))
    await reading.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert [t for t in asyncio.all_tasks() if t is not asyncio.current_task()] == []
    assert marker not in "".join(capfd.readouterr())


@pytest.mark.parametrize("error", ["RuntimeError", "CommandError"])
def test_a_failing_migration_reaches_no_parent_output(
    error, marker, tmp_path, monkeypatch, caplog, capfd
):
    """Its stdout (alembic's ``FAILED:`` print under its CLI) and its stderr
    (a traceback) reach no parent line, no exception and no fd 2: the child's
    stderr is its chain. The exit code is logged."""
    scripts = tmp_path / "scripts"
    (scripts / "versions").mkdir(parents=True)
    (scripts / "env.py").write_text(
        "import os\nfrom alembic.util import CommandError\n"
        "marker = os.environ['FM_TEST_MIGRATION_TOKEN']\n"
        f"print(marker)\nraise {error}(marker)\n"
    )
    (tmp_path / "alembic.ini").write_text(f"[alembic]\nscript_location = {scripts}\n")
    monkeypatch.setenv("ALEMBIC_CONFIG", str(tmp_path / "alembic.ini"))
    monkeypatch.setenv("FM_TEST_MIGRATION_TOKEN", marker)
    url = f"sqlite+aiosqlite:///{tmp_path / 'app.db'}"
    settings = SimpleNamespace(database=SimpleNamespace(database_url=url))
    with (
        patch("faultmaven.config.settings.get_settings", return_value=settings),
        pytest.raises(RuntimeError) as raised,
    ):
        data_init.run_alembic_migrations()
    out, err = capfd.readouterr()
    assert marker not in str(raised.value) + out + err + caplog.text
    assert "exit code 1" in str(raised.value)
    _framed(_lines(err), "Uncaught exception", f"{error}:")
