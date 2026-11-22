"""The credential-header redaction tests' shared fixture and helpers (#2865,
#2891): a line as the API's handler and as a job's formatter write it."""

import io
import json
import logging
import types

import pytest
import structlog

import faultmaven.infrastructure.logging.config as logging_config
from faultmaven.infrastructure.logging.config import (
    FaultMavenLogger,
    LoggingConfig,
    build_log_formatter,
    get_logger,
)


@pytest.fixture(params=["json", "console"])
def rendered(request, monkeypatch):
    """What the real ``configure_structlog`` handler writes, as JSON and as
    coloured console, every global restored (``written`` in
    ``test_url_query_redaction.py`` says why each one)."""
    root = logging.getLogger()
    saved_singleton = logging_config._logger_config
    get_logger("faultmaven.test.warmup")
    saved = structlog.get_config(), root.level, list(root.handlers)
    if request.param == "console":
        tty = types.SimpleNamespace(isatty=lambda: True)
        monkeypatch.setattr(logging_config, "sys", types.SimpleNamespace(stderr=tty))
    config = LoggingConfig()
    config.LOG_FORMAT, config.LOG_HUMAN_READABLE = request.param, False
    FaultMavenLogger(config).configure_structlog()
    root.setLevel(logging.INFO)
    buffer = io.StringIO()
    marker = FaultMavenLogger._ROOT_HANDLER_MARKER
    next(h for h in root.handlers if getattr(h, marker, False)).setStream(buffer)
    try:
        yield types.SimpleNamespace(buffer=buffer, mode=request.param)
        if request.param == "console" and buffer.getvalue():
            assert "\x1b[" in buffer.getvalue(), "the console case was not coloured"
    finally:
        structlog.configure(**saved[0])
        root.setLevel(saved[1])
        root.handlers[:] = saved[2]
        logging_config._logger_config = saved_singleton


def _json_lines(output):
    return [json.loads(row) for row in output.splitlines() if row.startswith('{"')]


def _job_line(log_format, message):
    """``message`` as a job writes it, through the formatter its handler
    (``install_log_handler``) builds (#2888)."""
    record = logging.LogRecord("job", logging.ERROR, __file__, 1, message, (), None)
    return build_log_formatter(log_format, False, io.StringIO()).format(record)
