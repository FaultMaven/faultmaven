"""An exception renders as class names and frames only (#2888, principles §7
rules 4 and 5): on the API chain, the jobs' line, a worker child's records and
the sites that pasted traceback text. No exception's message, note or group
member's message reaches the line.

The marker is built at run time (``chained_2888`` says why).
"""

import io
import json
import logging
import secrets
import types
from contextlib import contextmanager
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
from faultmaven.infrastructure.logging.url_redaction import JobLogFormatter
from tests.unit.infrastructure.logging import chained_2888
from tests.unit.infrastructure.logging.chained_2888 import LINKS, chained

pytestmark = [pytest.mark.unit, pytest.mark.security]

JOB_KEYS = {"timestamp", "logger", "level", "event"}


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


@contextmanager
def _records():
    """The records the root sees meanwhile."""
    seen: list = []
    handler = logging.Handler()
    handler.emit = seen.append
    logging.getLogger().addHandler(handler)
    try:
        yield seen
    finally:
        logging.getLogger().removeHandler(handler)


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

    @pytest.mark.parametrize("where", ["extra", "top-level", "nested", "event"])
    def test_an_exception_field_is_its_class_chain(self, api, marker, where):
        error = chained(marker)
        if where == "extra":
            logging.getLogger("faultmaven.test").error("failed", extra={"e": error})
        elif where == "top-level":
            get_logger("faultmaven.test").error("failed", e=error)
        elif where == "nested":
            get_logger("faultmaven.test").error("failed", e={"all": [(error,)]})
        else:
            get_logger("faultmaven.test").error(error)
        output = api.buffer.getvalue()
        assert marker not in output
        assert "RuntimeError <- ExceptionGroup" in output

    @pytest.mark.parametrize("api", ["json"], indirect=True)
    def test_the_json_renderer_names_a_deeper_exception_by_class(self, api, marker):
        deep: object = chained(marker)
        for _ in range(12):
            deep = [deep]
        get_logger("faultmaven.test").error("failed", e=deep)
        assert marker not in api.buffer.getvalue()
        assert "RuntimeError <- ExceptionGroup" in api.buffer.getvalue()

    @pytest.mark.parametrize("api", ["json"], indirect=True)
    def test_a_line_without_an_exception_keeps_its_keys(self, api):
        get_logger("faultmaven.test").info("plain", count=3)
        line = json.loads(api.buffer.getvalue())
        assert set(line) == {"event", "logger", "level", "timestamp", "count"}


def _job_record(message: str, error=None) -> logging.LogRecord:
    exc_info = (type(error), error, error.__traceback__) if error else None
    return logging.LogRecord(
        "faultmaven.jobs.t", logging.ERROR, "", 1, message, (), exc_info
    )


class TestTheJobsLine:
    @pytest.mark.parametrize("json_lines", [True, False], ids=["json", "plain"])
    def test_an_exception_is_frames_only(self, marker, json_lines):
        record = _job_record("failed", chained(marker))
        record.exc_text = marker  # a cached traceback is never read
        line = JobLogFormatter(json_lines).format(record)
        if json_lines:
            fields = json.loads(line)
            assert set(fields) == JOB_KEYS | {"exception"}
            line = fields["exception"]
        _frames_only(line, marker)

    @pytest.mark.parametrize(
        "message",
        ['say "hi"', "C:\\temp\\x", "one\ntwo", 'x", "level": "INFO'],
        ids=["quote", "backslash", "newline", "injected-field"],
    )
    @pytest.mark.parametrize("with_error", [False, True], ids=["plain", "exc_info"])
    def test_a_json_line_parses_to_its_fields(self, marker, message, with_error):
        record = _job_record(message, chained(marker) if with_error else None)
        fields = json.loads(JobLogFormatter(json_lines=True).format(record))
        assert set(fields) == JOB_KEYS | ({"exception"} if with_error else set())
        assert fields["event"] == message
        assert fields["level"] == "ERROR"

    def test_a_job_writes_through_it_on_stdout(self, monkeypatch, capsys, marker):
        from faultmaven.jobs.run import setup_logging

        monkeypatch.setenv("FAULTMAVEN_SKIP_DOTENV", "1")
        monkeypatch.setenv("LOG_FORMAT", "json")
        root = logging.getLogger()
        saved = root.handlers[:], root.level
        root.handlers[:] = []
        try:
            setup_logging(verbose=False)
            assert root.level == logging.INFO
            logging.getLogger("faultmaven.jobs.t").error(
                "failed", exc_info=chained(marker)
            )
        finally:
            root.handlers[:] = saved[0]
            root.setLevel(saved[1])
        (line,) = capsys.readouterr().out.splitlines()
        _frames_only(json.loads(line)["exception"], marker)


async def test_a_workers_records_carry_frames_only(caplog, marker):
    """A worker child's forwarded record, and the record of the failure that
    ended it, are re-logged in the parent with frames, never the text."""
    caplog.set_level(logging.INFO)
    end = await worker_process.run(
        chained_2888.__name__,
        {"marker": marker},
        memory_bytes=4 << 30,
        cpu_seconds=60,
        output_limit_bytes=1 << 20,
    )
    assert end.code == 1
    messages = [r.getMessage() for r in caplog.records]
    for head in ("the forwarded record\n", "the test job failed\n"):
        (message,) = [m for m in messages if m.startswith(head)]
        _frames_only(message, marker)


def _fields_hold_frames(api, records, marker: str, event: str) -> None:
    """The record's exception, as ``frames`` on the API line and ``exception``
    on the jobs' line. The message is S3's (#2889)."""
    (line,) = [
        json.loads(row)
        for row in api.buffer.getvalue().splitlines()
        if json.loads(row)["event"].startswith(event)
    ]
    _frames_only(line["frames"], marker)
    (record,) = [r for r in records if r.getMessage().startswith(event)]
    _frames_only(json.loads(JobLogFormatter(True).format(record))["exception"], marker)


@pytest.mark.parametrize("api", ["json"], indirect=True)
def test_a_repository_that_fails_to_build_logs_frames(api, marker):
    from faultmaven.container.providers.services import create_team_repository

    target = (
        "faultmaven.infrastructure.persistence.sessionless_team_repository."
        "SessionlessTeamRepository"
    )
    with _records() as records, patch(target, side_effect=chained(marker)):
        assert create_team_repository() is None
    _fields_hold_frames(api, records, marker, "TeamRepository initialization failed")


@pytest.mark.parametrize("api", ["json"], indirect=True)
async def test_the_containers_critical_error_logs_frames(api, marker, monkeypatch):
    from faultmaven.config.settings import FaultMavenSettings
    from faultmaven.container import DIContainer

    monkeypatch.setenv("DEPLOYMENT_MODE", "standalone")
    monkeypatch.setenv("ENVIRONMENT", "development")
    settings = FaultMavenSettings(_env_file=None)
    container = DIContainer()
    container._initialized = container._initializing = False

    async def nothing(*_args, **_kwargs):
        return None

    impl = "faultmaven._container_impl"
    with (
        _records() as records,
        patch(f"{impl}.get_settings", return_value=settings),
        patch(f"{impl}.register_infrastructure", new=MagicMock(side_effect=nothing)),
        patch(f"{impl}.register_tools"),
        patch(f"{impl}.register_services", side_effect=chained(marker)),
        pytest.raises(RuntimeError),
    ):
        await container.initialize()
    _fields_hold_frames(api, records, marker, "Critical initialization error")
    (line,) = [row for row in api.buffer.getvalue().splitlines() if "Critical" in row]
    assert marker not in line
