"""An exception renders as class names and frames only (#2888, principles §7
rules 4 and 5): on the API chain, the jobs' line, a worker child's records and
the sites that pasted traceback text. No exception's message, note or group
member's message reaches the line.

The marker is built at run time (``chained_2888`` says why).
"""

import collections
import dataclasses
import io
import json
import logging
import os
import secrets
import sys
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


@dataclasses.dataclass
class _Holder:
    error: object


@dataclasses.dataclass
class _Login:
    password: str = dataclasses.field(repr=False)
    error: object = None


@dataclasses.dataclass(repr=False)
class _Session:
    user: str
    token: str
    error: object

    def __repr__(self):
        return f"_Session(user={self.user!r})"


def _deep(error, levels=12):
    for _ in range(levels):
        error = [error]
    return error


def _twin(error):
    """Another exception with ``error``'s class chain."""
    twin = type(error)()
    twin.__cause__ = error.__cause__
    return twin


#: Containers an exception field can sit in, each built around ``error``.
SHAPES = {
    "nested": lambda e: {"all": [(e,)]},
    "ordered-dict": lambda e: collections.OrderedDict(e=e),
    "default-dict": lambda e: collections.defaultdict(list, e=e),
    "namedtuple": lambda e: collections.namedtuple("Pair", "e n")(e, 1),
    "deque": lambda e: collections.deque([e]),
    "dataclass": _Holder,
    "namespace": lambda e: types.SimpleNamespace(e=e),
    "dict-key": lambda e: {e: 1},
    "tuple-key": lambda e: {(e, 1): 1},
    "keys-of-one-chain": lambda e: {e: 1, _twin(e): 2},
    "twelve-deep": _deep,
}


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

    def test_exc_info_any_truthy_value_is_the_exception_in_flight(self, api, marker):
        try:
            raise chained(marker)
        except RuntimeError:
            get_logger("faultmaven.test").error("failed", exc_info=1)
        _api_frames(api, marker)

    @pytest.mark.parametrize("where", ["extra", "top-level", "event", *SHAPES])
    def test_an_exception_field_is_its_class_chain(self, api, marker, where, capsys):
        error = chained(marker)
        if where == "extra":
            logging.getLogger("faultmaven.test").error("failed", extra={"e": error})
        elif where == "top-level":
            get_logger("faultmaven.test").error("failed", e=error)
        elif where == "event":
            get_logger("faultmaven.test").error(error)
        else:
            get_logger("faultmaven.test").error("failed", e=SHAPES[where](error))
        output = api.buffer.getvalue()
        assert marker not in output
        assert capsys.readouterr().err == ""  # the line rendered
        if api.mode == "json":
            line = json.loads(output)
            if where == "keys-of-one-chain":  # both values kept
                assert sorted(line["e"].values()) == [1, 2]
        elif where == "keys-of-one-chain":
            assert "ExceptionGroup': 1" in output and "ExceptionGroup #2': 2" in output
        if where != "twelve-deep":
            assert "RuntimeError <- ExceptionGroup" in output
        else:  # past the walk's depth, a container is its type name
            assert "'list'" in output or '"list"' in output

    def test_a_dataclass_field_hidden_from_its_repr_stays_hidden(self, api, marker):
        secret = secrets.token_hex(8)
        get_logger("faultmaven.test").error("failed", e=_Login(secret, chained(marker)))
        output = api.buffer.getvalue()
        assert secret not in output and marker not in output
        assert "RuntimeError <- ExceptionGroup" in output

    def test_a_class_with_its_own_repr_renders_through_it(self, api, marker):
        token = secrets.token_hex(8)
        get_logger("faultmaven.test").error(
            "failed", e=_Session("u", token, chained(marker))
        )
        output = api.buffer.getvalue()
        assert token not in output and marker not in output
        assert "_Session(user='u')" in output

    def test_a_self_reference_ends_and_renders_as_before(self, api, monkeypatch):
        visits = []
        walk = logging_config._parts
        monkeypatch.setattr(
            logging_config, "_parts", lambda v: visits.append(v) or walk(v)
        )
        loop: list = []
        loop.extend([loop] * 4)
        get_logger("faultmaven.test").info("plain", v=loop)
        # Walked once: each revisit is a self-reference, returned as it is.
        assert sum(v is loop for v in visits) == 1
        if api.mode == "console":
            assert f"v={loop!r}" in api.buffer.getvalue()

    @pytest.mark.parametrize(
        "value",
        [_deep(1, levels=11), collections.UserString("hello")],
        ids=["eleven-deep", "user-string"],
    )
    def test_a_field_with_no_exception_renders_as_before(self, api, value):
        get_logger("faultmaven.test").info("plain", v=value)
        output = api.buffer.getvalue()
        if api.mode == "json":
            expected = value if isinstance(value, list) else repr(value)
            assert json.loads(output)["v"] == expected
        else:
            assert f"v={value!r}" in output

    @pytest.mark.parametrize("api", ["json"], indirect=True)
    def test_a_line_without_an_exception_keeps_its_keys(self, api):
        get_logger("faultmaven.test").info("plain", count=3)
        line = json.loads(api.buffer.getvalue())
        assert set(line) == {"event", "logger", "level", "timestamp", "count"}


def _job_record(message: str, error=None, exc_info=None) -> logging.LogRecord:
    if error is not None:
        exc_info = (type(error), error, error.__traceback__)
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

    @pytest.mark.parametrize("json_lines", [True, False], ids=["json", "plain"])
    def test_stack_info_and_frames_follow_the_line(self, json_lines):
        record = _job_record("failed")
        record.stack_info, record.frames = "Stack (most recent call last):", "F:\n"
        line = JobLogFormatter(json_lines).format(record)
        if json_lines:
            fields = json.loads(line)
            assert (fields["stack"], fields["frames"]) == (record.stack_info, "F:\n")
        else:
            assert line.endswith(" - failed\nF:\n\nStack (most recent call last):")

    @pytest.mark.parametrize("exc_info", [1, (), (1, 2), True], ids=repr)
    def test_any_exc_info_renders_or_is_ignored(self, marker, exc_info):
        record = logging.makeLogRecord({"msg": "failed", "exc_info": exc_info})
        fields = json.loads(JobLogFormatter(True).format(record))
        assert "exception" not in fields  # nothing in flight here
        try:
            raise chained(marker)
        except RuntimeError:
            fields = json.loads(JobLogFormatter(True).format(record))
        if exc_info:
            _frames_only(fields["exception"], marker)

    def test_a_tuples_own_traceback_is_rendered(self, marker):
        error = chained(marker)
        tb, error.__traceback__ = error.__traceback__, None
        record = _job_record("failed", exc_info=(RuntimeError, error, tb))
        frames = json.loads(JobLogFormatter(True).format(record))["exception"]
        assert frames.startswith("RuntimeError:\n  File ")

    @pytest.mark.parametrize(
        "env",
        [{"LOG_FORMAT": "json"}, {"LOG_FORMAT": "bogus"}, {"LOG_LEVEL": "bogus"}],
        ids=["json", "unrecognized-format", "invalid-level"],
    )
    def test_a_job_writes_json_through_it_on_stdout(
        self, monkeypatch, capsys, marker, env
    ):
        from faultmaven.config.settings import env_var_spellings
        from faultmaven.jobs.run import setup_logging

        monkeypatch.setenv("FAULTMAVEN_SKIP_DOTENV", "1")
        for name in env_var_spellings(dict(os.environ), "LOG_FORMAT"):
            monkeypatch.delenv(name)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
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
        written = capsys.readouterr()
        assert written.err == ""  # nothing reached lastResort
        *_, line = [json.loads(row) for row in written.out.splitlines()]
        _frames_only(line["exception"], marker)


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
    with _records() as records:
        assert create_team_repository() is None
    _fields_hold_frames(api, records, marker, "TeamRepository initialization failed")


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
        _records() as records,
        patch(f"{impl}.get_settings", return_value=settings),
        patch(f"{impl}.register_infrastructure", new=MagicMock(side_effect=nothing)),
        patch(f"{impl}.register_tools"),
        patch(f"{impl}.register_services", side_effect=chained(marker)),
        pytest.raises(RuntimeError),
    ):
        await reset_container.initialize()
    _fields_hold_frames(api, records, marker, "Critical initialization error")
    (line,) = [row for row in api.buffer.getvalue().splitlines() if "Critical" in row]
    assert marker not in line
