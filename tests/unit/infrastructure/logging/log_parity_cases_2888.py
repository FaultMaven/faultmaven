"""The API chain's lines without an exception, for #2888's parity pin.

``render_all`` logs a fixed record set through ``FaultMavenLogger`` (JSON and
console, the root's other handlers cleared) and returns each line normalized:
the timestamp, and a ``stack`` cut to its last frame with the file's path
dropped, since the frames above it are the caller's. Its output on ``main`` is
``log_parity_oracle_2888.json``, captured at 19ab0e8b8 by running
``render_all`` with this file copied into a ``git archive`` extract of that
commit, never typed by hand.
"""

import contextlib
import io
import json
import logging
import re
import types

import structlog
from opentelemetry import trace

from faultmaven.infrastructure.logging.config import FaultMavenLogger
from faultmaven.infrastructure.logging.coordinator import (
    RequestContext,
    request_context,
)

_TIMESTAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+Z")
_FILE = re.compile(r'File "[^"]*/([^"/]+)", line \d+')


class _RecordingSpan(trace.NonRecordingSpan):
    def is_recording(self) -> bool:
        return True


def _native():
    return structlog.get_logger("faultmaven.parity")


def _stdlib():
    return logging.getLogger("faultmaven.parity.std")


def _with_request_context(emit):
    token = request_context.set(
        RequestContext(
            correlation_id="corr-1",
            claimed_session_id="sess-1",
            user_id="user-1",
            claimed_case_id="case-1",
            agent_phase="phase-1",
        )
    )
    try:
        emit()
    finally:
        request_context.reset(token)


def _in_a_span(emit):
    context = trace.SpanContext(
        trace_id=0x1234567890ABCDEF1234567890ABCDEF,
        span_id=0x1234567890ABCDEF,
        is_remote=False,
    )
    with trace.use_span(_RecordingSpan(context)):
        emit()


#: The record set: name -> what it logs.
CASES = {
    "native keywords": lambda: _native().info("native", count=3, name="x"),
    "native extra": lambda: _native().info("native extra", extra={"job_id": 7}),
    "stdlib extra": lambda: _stdlib().warning("stdlib extra", extra={"job_id": 7}),
    "stdlib positional": lambda: _stdlib().info("env=%s n=%d", "prod", 3),
    "native positional": lambda: _native().info("env=%s", "prod"),
    "colliding level": lambda: _stdlib().info("collide", extra={"level": "top"}),
    "colliding user_id": lambda: _with_request_context(
        lambda: _stdlib().info("collide", extra={"user_id": "other", "n": 1})
    ),
    "request context": lambda: _with_request_context(
        lambda: _native().info("in a request", step=2)
    ),
    "recording span": lambda: _in_a_span(lambda: _stdlib().info("in a span")),
    "stack_info": lambda: _stdlib().info("with a stack", stack_info=True),
    "bytes value": lambda: _native().info("bytes", payload=b"\x00ab\xff"),
}


def _last_frame(stack: str) -> str:
    """A stack's header and last frame, reduced to its file's name: the line
    number is the Python version's."""
    lines = stack.splitlines()
    return _FILE.sub(r'File "\1"', "\n".join([lines[0], *lines[-2:]]))


def _normalized(text: str, log_format: str) -> str:
    text = _TIMESTAMP.sub("<timestamp>", text)
    if log_format == "json":
        line = json.loads(text, object_pairs_hook=dict)
        if "stack" in line:
            line["stack"] = _last_frame(line["stack"])
        return json.dumps(line)
    head, _, stack = text.partition("\n")
    return f"{head}\n{_last_frame(stack)}" if stack else head


def _render(log_format: str) -> dict:
    root = logging.getLogger()
    saved = structlog.get_config(), root.level, list(root.handlers)
    config = types.SimpleNamespace(
        LOG_FORMAT=log_format,
        LOG_HUMAN_READABLE=False,
        get_log_level=lambda: logging.INFO,
    )
    buffer = io.StringIO()
    try:
        root.handlers[:] = []
        root.setLevel(logging.NOTSET)
        with contextlib.redirect_stderr(io.StringIO()):  # not a terminal
            FaultMavenLogger(config)
        (handler,) = root.handlers
        handler.setStream(buffer)
        rendered = {}
        for name, emit in CASES.items():
            buffer.seek(0)
            buffer.truncate()
            emit()
            rendered[name] = _normalized(buffer.getvalue().rstrip("\n"), log_format)
        return rendered
    finally:
        structlog.configure(**saved[0])
        root.setLevel(saved[1])
        root.handlers[:] = saved[2]


def render_all() -> dict:
    return {log_format: _render(log_format) for log_format in ("json", "console")}


if __name__ == "__main__":
    print(json.dumps(render_all(), indent=2, sort_keys=True))
