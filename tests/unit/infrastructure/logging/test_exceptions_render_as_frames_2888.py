"""An exception renders as class names and frames only, through one chain
(#2888, principles §7 rules 4 and 5): the API process, the jobs (through the
same formatter builder), a worker child's records and the sites that pasted
traceback text. No exception's message, note or group member's message reaches
the line. A line without an exception renders as it did on ``main``.

The marker is built at run time (``chained_2888`` says why).
"""

import dataclasses
import io
import json
import logging
import os
import re
import secrets
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import structlog

import faultmaven.infrastructure.logging.config as logging_config
from faultmaven.infrastructure import worker_process
from faultmaven.infrastructure.logging.config import (
    FaultMavenLogger,
    LoggingConfig,
    get_logger,
)
from tests.unit.infrastructure.logging import chained_2888, log_parity_cases_2888
from tests.unit.infrastructure.logging.chained_2888 import LINKS, chained

pytestmark = [pytest.mark.unit, pytest.mark.security]

#: The keys every JSON line carries, the API's and a job's alike.
LINE_KEYS = {"timestamp", "logger", "level", "event"}


@pytest.fixture
def marker() -> str:
    return secrets.token_hex(16)


@pytest.fixture(params=["json", "console"])
def api(request):
    """The API chain (``configure_structlog``) writing to a buffer, every
    global restored."""
    root = logging.getLogger()
    saved_singleton = logging_config._logger_config
    get_logger("faultmaven.test.warmup")
    saved = structlog.get_config(), root.level, list(root.handlers)
    config = LoggingConfig()
    config.LOG_FORMAT, config.LOG_HUMAN_READABLE = request.param, False
    FaultMavenLogger(config)
    root.setLevel(logging.INFO)
    buffer = io.StringIO()
    flag = FaultMavenLogger._ROOT_HANDLER_MARKER
    next(h for h in root.handlers if getattr(h, flag, False)).setStream(buffer)
    try:
        yield types.SimpleNamespace(buffer=buffer, mode=request.param)
    finally:
        structlog.configure(**saved[0])
        root.setLevel(saved[1])
        root.handlers[:] = saved[2]
        logging_config._logger_config = saved_singleton


def _frames_only(text: str, marker: str) -> None:
    assert marker not in text
    for link in LINKS:
        assert link in text, f"{link} is not rendered"


def _api_frames(api, marker: str) -> None:
    """The API line renders the exception under ``frames`` (JSON) or as its
    stack block (console), with no message."""
    output = api.buffer.getvalue()
    assert marker not in output
    if api.mode == "json":
        (line,) = [json.loads(row) for row in output.splitlines()]
        assert "exception" not in line
        output = line["frames"]
    _frames_only(output, marker)


class TestTheApiChain:
    @pytest.mark.parametrize("form", ["true", "instance", "tuple"])
    def test_a_stdlib_records_exc_info(self, api, marker, form):
        error = chained(marker)
        exc_info = {
            "true": True,
            "instance": error,
            "tuple": (type(error), error, error.__traceback__),
        }[form]
        try:
            raise error
        except RuntimeError:
            logging.getLogger("faultmaven.test").error("failed", exc_info=exc_info)
        _api_frames(api, marker)

    def test_a_native_events_exc_info(self, api, marker):
        get_logger("faultmaven.test").error("failed", exc_info=chained(marker))
        _api_frames(api, marker)

    def test_exc_info_any_truthy_value_is_the_exception_in_flight(self, api, marker):
        try:
            raise chained(marker)
        except RuntimeError:
            get_logger("faultmaven.test").error("failed", exc_info=1)
        _api_frames(api, marker)

    @pytest.mark.parametrize("api", ["json"], indirect=True)
    @pytest.mark.parametrize("exc_info", [1, (), (1, 2), True], ids=repr)
    def test_a_records_odd_exc_info_renders_or_is_ignored(self, api, marker, exc_info):
        flag = FaultMavenLogger._ROOT_HANDLER_MARKER
        root = logging.getLogger()
        handler = next(h for h in root.handlers if getattr(h, flag, False))

        def emit():  # to the chain's handler alone: pytest's cannot format it
            handler.handle(
                logging.LogRecord(
                    "faultmaven.test", logging.ERROR, "", 1, "failed", (), exc_info
                )
            )

        emit()
        assert "frames" not in json.loads(api.buffer.getvalue())  # none in flight
        api.buffer.truncate(0)
        api.buffer.seek(0)
        try:
            raise chained(marker)
        except RuntimeError:
            emit()
        if exc_info:
            _api_frames(api, marker)

    @pytest.mark.parametrize("api", ["json"], indirect=True)
    def test_a_tuples_own_traceback_is_rendered(self, api, marker):
        error = chained(marker)
        tb, error.__traceback__ = error.__traceback__, None
        logging.getLogger("faultmaven.test").error(
            "failed", exc_info=(RuntimeError, error, tb)
        )
        frames = json.loads(api.buffer.getvalue())["frames"]
        assert frames.startswith("RuntimeError:\n  File ")

    @pytest.mark.parametrize("api", ["json"], indirect=True)
    def test_a_callers_frames_win_over_exc_info(self, api, marker):
        logging.getLogger("faultmaven.test").error(
            "failed", exc_info=chained(marker), extra={"frames": "F:\n"}
        )
        assert json.loads(api.buffer.getvalue())["frames"] == "F:\n"


@dataclasses.dataclass
class _Holder:
    error: object


class _Hiding(list):
    def __repr__(self):
        return "_Hiding(...)"


def _emit_field(where: str, error: BaseException) -> None:
    if where == "extra":
        logging.getLogger("faultmaven.test").error("failed", extra={"e": error})
    elif where == "nested-extra":  # the chain nests a colliding key
        logging.getLogger("faultmaven.test").error("failed", extra={"exception": error})
    elif where == "top-level":
        get_logger("faultmaven.test").error("failed", e=error)
    else:
        get_logger("faultmaven.test").error(error)


class TestAnExceptionField:
    """Rule 4's boundary for a field: what the chain names by its class chain,
    and what renders through an object's repr."""

    @pytest.mark.parametrize("where", ["extra", "nested-extra", "top-level", "event"])
    def test_a_top_level_field_is_its_class_chain(self, api, marker, where, capsys):
        _emit_field(where, chained(marker))
        output = api.buffer.getvalue()
        assert capsys.readouterr().err == ""  # the line rendered
        assert marker not in output
        assert "RuntimeError <- ExceptionGroup" in output

    @pytest.mark.parametrize("api", ["json"], indirect=True)
    @pytest.mark.parametrize(
        "shape",
        [list, tuple, lambda e: {"k": e}, _Hiding],
        ids=["list", "tuple", "dict-value", "list-subclass"],
    )
    def test_under_json_the_encoder_names_one_in_a_list_tuple_or_dict(
        self, api, marker, shape
    ):
        error = chained(marker)
        value = shape([error]) if shape in (list, tuple, _Hiding) else shape(error)
        get_logger("faultmaven.test").error("failed", e=value)
        output = api.buffer.getvalue()
        assert marker not in output
        assert "RuntimeError <- ExceptionGroup" in output

    @pytest.mark.parametrize(
        "mode, shape",
        [
            ("json", lambda e: {e}),
            ("json", _Holder),
            ("console", lambda e: [e]),
            ("console", _Hiding),
        ],
        ids=["json-set", "json-dataclass", "console-list", "console-list-subclass"],
    )
    def test_anything_else_renders_through_its_repr(self, marker, mode, shape):
        """The stated boundary, not a guarantee (principles §7, rule 4): an
        exception the chain does not reach renders as its container's repr
        renders it, and keeping it off the line is the call site's, rule 1's
        (S3, #2889). A class with its own ``__repr__`` is not walked."""
        error = chained(marker)
        value = _Hiding([error]) if shape is _Hiding else shape(error)
        rendered = _render_through(mode, value)
        assert repr(value) in rendered
        if shape is _Hiding:
            assert marker not in rendered

    def test_lines_without_an_exception_render_as_on_main(self):
        """Whole lines, JSON and console, captured on ``main`` at 19ab0e8b8
        (``log_parity_cases_2888`` says how), timestamp normalized."""
        oracle = Path(__file__).with_name("log_parity_oracle_2888.json")
        assert log_parity_cases_2888.render_all() == json.loads(oracle.read_text())


def _render_through(mode: str, value) -> str:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging_config.build_log_formatter(mode, False, stream))
    logger = logging.getLogger("faultmaven.test.boundary")
    logger.addHandler(handler)
    logger.propagate = False
    try:
        logger.error("failed", extra={"e": value})
    finally:
        logger.removeHandler(handler)
        logger.propagate = True
    return stream.getvalue()


@pytest.fixture
def job(monkeypatch, capsys):
    """A job's logging (``jobs.run.setup_logging``) on a root with no handler,
    ``LOG_FORMAT`` and ``LOG_LEVEL`` from the test's ``env``; returns the
    lines it wrote on stdout, checking nothing reached stderr."""
    from faultmaven.config.settings import env_var_spellings
    from faultmaven.jobs.run import setup_logging

    monkeypatch.setenv("FAULTMAVEN_SKIP_DOTENV", "1")
    for name in env_var_spellings(dict(os.environ), "LOG_FORMAT"):
        monkeypatch.delenv(name)
    root = logging.getLogger()
    saved = root.handlers[:], root.level

    def start(**env):
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        root.handlers[:] = []  # in the call: pytest adds one per phase
        setup_logging(verbose=False)

    def lines():
        written = capsys.readouterr()
        assert written.err == ""  # nothing reached lastResort
        return written.out.splitlines()

    try:
        yield types.SimpleNamespace(start=start, lines=lines)
    finally:
        root.handlers[:] = saved[0]
        root.setLevel(saved[1])


class TestTheJobsLine:
    @pytest.mark.parametrize("log_format", ["json", "console"])
    def test_an_exception_is_frames_only(self, job, marker, log_format):
        job.start(LOG_FORMAT=log_format)
        logging.getLogger("faultmaven.jobs.t").error("failed", exc_info=chained(marker))
        lines = job.lines()
        if log_format == "json":
            (line,) = [json.loads(row) for row in lines]
            _frames_only(line["frames"], marker)
        else:
            assert "[faultmaven.jobs.t]" in lines[0]
            _frames_only("\n".join(lines), marker)

    @pytest.mark.parametrize(
        "message",
        ['say "hi"', "C:\\temp\\x", "one\ntwo", 'x", "level": "INFO'],
        ids=["quote", "backslash", "newline", "injected-field"],
    )
    @pytest.mark.parametrize("with_error", [False, True], ids=["plain", "exc_info"])
    def test_a_json_line_parses_to_the_apis_keys(
        self, job, marker, message, with_error
    ):
        job.start(LOG_FORMAT="json")
        error = chained(marker) if with_error else None
        logging.getLogger("faultmaven.jobs.t").error(message, exc_info=error)
        (line,) = [json.loads(row) for row in job.lines()]
        assert LINE_KEYS <= set(line)
        assert ("frames" in line) is with_error
        assert (line["event"], line["level"]) == (message, "error")
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+Z", line["timestamp"])

    def test_a_records_extra_fields_are_on_the_line(self, job):
        job.start(LOG_FORMAT="json")
        logging.getLogger("faultmaven.jobs.t").info("seeded", extra={"count": 91})
        (line,) = [json.loads(row) for row in job.lines()]
        assert line["count"] == 91

    @pytest.mark.parametrize(
        "env",
        [{"LOG_FORMAT": "json"}, {"LOG_FORMAT": "bogus"}, {"LOG_LEVEL": "bogus"}],
        ids=["json", "unrecognized-format", "invalid-level"],
    )
    def test_a_job_writes_json_on_stdout(self, job, marker, env):
        job.start(**env)
        assert logging.getLogger().level == logging.INFO
        logging.getLogger("faultmaven.jobs.t").error("failed", exc_info=chained(marker))
        *_, line = [json.loads(row) for row in job.lines()]
        _frames_only(line["frames"], marker)

    def test_a_jobs_process_writes_each_line_once(self, job, monkeypatch):
        """The API's ``get_logger`` in a job adds no second root handler: its
        events render through the job's, on stdout (#2888)."""
        job.start(LOG_FORMAT="json")
        monkeypatch.setattr(logging_config, "_logger_config", None)
        get_logger("faultmaven.jobs.t").info("once", step=1)
        logging.getLogger("faultmaven.jobs.t").info("twice")
        lines = [json.loads(row) for row in job.lines()]  # settings may log too
        lines = [line for line in lines if line["logger"] == "faultmaven.jobs.t"]
        assert [(line["event"], line.get("step")) for line in lines] == [
            ("once", 1),
            ("twice", None),
        ]


async def test_a_workers_records_carry_frames_only(caplog, marker):
    """A worker child's forwarded record, and the record of the failure that
    ended it, are re-logged in the parent with ``frames``, never the text."""
    caplog.set_level(logging.INFO)
    end = await worker_process.run(
        chained_2888.__name__,
        {"marker": marker},
        memory_bytes=4 << 30,
        cpu_seconds=60,
        output_limit_bytes=1 << 20,
    )
    assert end.code == 1
    for message in ("the forwarded record", "the test job failed"):
        (record,) = [r for r in caplog.records if r.getMessage() == message]
        _frames_only(record.frames, marker)


def _line_holds_frames(api, marker: str, event: str) -> None:
    """The record's exception, as ``frames`` on the line. The message is S3's
    (#2889)."""
    (line,) = [
        json.loads(row)
        for row in api.buffer.getvalue().splitlines()
        if json.loads(row)["event"].startswith(event)
    ]
    _frames_only(line["frames"], marker)


@pytest.mark.parametrize("api", ["json"], indirect=True)
def test_a_repository_that_fails_to_build_logs_frames(api, marker, monkeypatch):
    """The repository's module fails as it imports: a package missing from the
    image, the failure these warnings exist for (no interface stands between
    the factory and the class it builds)."""
    from faultmaven.container.providers.services import create_team_repository

    error = chained(marker)

    def refuse(_name):
        raise error

    broken = types.ModuleType("broken")
    broken.__getattr__ = refuse
    module = "faultmaven.infrastructure.persistence.sessionless_team_repository"
    monkeypatch.setitem(sys.modules, module, broken)
    assert create_team_repository() is None
    _line_holds_frames(api, marker, "TeamRepository initialization failed")


@pytest.mark.parametrize("api", ["json"], indirect=True)
async def test_the_containers_critical_error_logs_frames(
    api, marker, monkeypatch, reset_container
):
    from faultmaven.config.settings import FaultMavenSettings

    monkeypatch.setenv("DEPLOYMENT_MODE", "standalone")
    monkeypatch.setenv("ENVIRONMENT", "development")
    settings = FaultMavenSettings(_env_file=None)

    async def nothing(*_args, **_kwargs):
        return None

    impl = "faultmaven._container_impl"
    with (
        patch(f"{impl}.get_settings", return_value=settings),
        patch(f"{impl}.register_infrastructure", new=MagicMock(side_effect=nothing)),
        patch(f"{impl}.register_tools"),
        patch(f"{impl}.register_services", side_effect=chained(marker)),
        pytest.raises(RuntimeError),
    ):
        await reset_container.initialize()
    _line_holds_frames(api, marker, "Critical initialization error")
    (line,) = [row for row in api.buffer.getvalue().splitlines() if "Critical" in row]
    assert marker not in line
