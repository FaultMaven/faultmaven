"""A rendered log line has a declared limit, ``LOG_LINE_MAX_CHARS`` (#3116 B3).

Principle 14 lets ``redact_log_line`` run inline on every record only under a
declared input limit and a growth test over it. A record rendered over the
limit is never redacted or written, but for its frames, redacted whole before
their cut: a replacement record (its kept keys cut, ``log_line_cut``,
``log_line_chars``) is rendered by the same renderer, then redacted, so no
text past a cut reaches the output and a JSON line stays JSON.
A line at or under the limit renders as on ``main``.
"""

from __future__ import annotations

import io
import json
import logging
import sys

import pytest

from faultmaven.infrastructure.logging.config import build_log_formatter
from faultmaven.infrastructure.logging.url_redaction import (
    LOG_LINE_MAX_CHARS,
    _cut_frames,
    redact_log_line,
    redact_log_line_bytes,
    redacting_renderer,
)
from faultmaven.utils.exception_name import exception_frames
from tests.wallclock import assert_linear_growth

pytestmark = [pytest.mark.unit]

MODES = ["json", "console"]
SOURCES = ["stdlib", "native"]
#: The context keys the replacement keeps, each cut to 256 characters.
CONTEXT_KEYS = (
    "correlation_id",
    "request_id",
    "user_id",
    "trace_id",
    "span_id",
    "claimed_session_id",
    "claimed_case_id",
    "agent_phase",
)
FILLER = "disk full on node a " * 4000  # 80,000 characters


def _record(source: str, event: str, exc_info=None, **fields) -> logging.LogRecord:
    """A stdlib record, or a native structlog event as ``wrap_for_formatter``
    hands it to the formatter."""
    if source == "stdlib":
        record = logging.LogRecord(
            "faultmaven.t", logging.ERROR, __file__, 1, event, None, exc_info
        )
        record.__dict__.update(fields)
        return record
    record = logging.LogRecord(
        "faultmaven.t", logging.ERROR, __file__, 1, "", None, None
    )
    record.msg = {
        "event": event,
        "level": "error",
        "logger": "faultmaven.t",
        "timestamp": "2026-11-28T00:00:00Z",
        **({"exc_info": exc_info} if exc_info else {}),
        **fields,
    }
    record._logger = logging.getLogger("faultmaven.t")
    record._name = "error"
    return record


def _format(mode: str, record: logging.LogRecord) -> str:
    return build_log_formatter(mode, False, io.StringIO()).format(record)


def _fields(mode: str, line: str) -> dict:
    """The replacement's fields: JSON parsed, console read off its first line."""
    if mode == "json":
        return json.loads(line)
    head = line.split("\n", 1)[0]
    return {
        key: value
        for key, _, value in (part.partition("=") for part in head.split(" "))
        if value
    }


def _recursion() -> tuple:
    def a(n):
        return b(n)

    def b(n):
        return a(n)

    try:
        a(1)
    except RecursionError:
        return sys.exc_info()


# --- Over the limit: the replacement -----------------------------------------


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("mode", MODES)
def test_a_line_over_the_limit_is_the_replacement(mode, source):
    record = _record(source, "cache miss " + FILLER, detail="x" * 100)

    line = _format(mode, record)
    fields = _fields(mode, line)

    assert len(line) <= LOG_LINE_MAX_CHARS
    assert str(fields["log_line_cut"]) == "True"
    assert int(fields["log_line_chars"]) > LOG_LINE_MAX_CHARS
    assert "detail" not in fields
    if mode == "json":
        assert fields["event"].startswith("cache miss disk full")
        assert len(fields["event"]) <= 1024


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("mode", MODES)
def test_no_text_past_the_cut_is_written(mode, source):
    """A secret in the event's head is redacted; one past the cut, in the
    event or in a dropped field, is not on the line at all."""
    event = (
        "probe https://h.example/p?token=HEADSECRET1 then "
        + FILLER
        + " Authorization: Bearer TAILSECRET1 https://h.example/q?key=TAILSECRET2"
    )
    record = _record(source, event, detail="Bearer TAILSECRET3 " * 10)

    line = _format(mode, record)

    assert "https://h.example/p?<redacted>" in line
    for secret in ("HEADSECRET1", "TAILSECRET1", "TAILSECRET2", "TAILSECRET3"):
        assert secret not in line


@pytest.mark.parametrize(
    "event, kept",
    [
        # Whitespace within 128 of the cut: cut back to it.
        ("a" * 1000 + " " + "b" * 100, "a" * 1000),
        # None within 128: a hard cut.
        ("a" * 800 + " " + "b" * 300, "a" * 800 + " " + "b" * 223),
        ("a" * 2000, "a" * 1024),
    ],
    ids=["back-to-whitespace", "hard-past-the-floor", "no-whitespace"],
)
def test_the_event_is_cut_at_whitespace_with_a_floor(event, kept):
    line = _format("json", _record("stdlib", event, detail=FILLER))
    assert json.loads(line)["event"] == kept


@pytest.mark.parametrize("source", SOURCES)
def test_a_deep_recursion_keeps_bounded_frames(source):
    """A mutual recursion renders ~190,000 characters of frames: the line
    keeps the class header and the last 4,096 characters from a line start."""
    exc_info = _recursion()

    line = _format("json", _record(source, "boom", exc_info))
    fields = json.loads(line)

    assert fields["log_line_cut"] is True and fields["frames_cut"] is True
    header, tail = fields["frames"].split("\n", 1)
    assert header == "RecursionError:"
    full = exception_frames(exc_info[1], exc_info[2])
    assert 0 < len(tail) <= 4096 < len(full)
    # The last characters of the frames, from a line start.
    assert full.endswith(tail) and full[-len(tail) - 1] == "\n"
    console = _format("console", _record(source, "boom", exc_info))
    assert "frames_cut=True" in console and "\nRecursionError:\n" in console
    assert len(console) <= LOG_LINE_MAX_CHARS


NOT_TEXT = {
    "nested header pair": (
        {"value": "Bearer SECRETX", "name": "Authorization"},
        "dict",
    ),
    "exception in a list": ([ValueError("EXCSECRET")], "list"),
}


@pytest.mark.parametrize("value, kind", NOT_TEXT.values(), ids=NOT_TEXT.keys())
@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("mode", MODES)
def test_a_kept_value_that_is_not_text_is_its_type_name(mode, source, value, kind):
    """Its repr would carry what the renderer's own form redacts or names by
    class: a value-first header pair, an exception's message."""
    line = _format(mode, _record(source, "x", request_id=value, detail=FILLER))

    assert "SECRETX" not in line and "EXCSECRET" not in line
    assert _fields(mode, line)["request_id"] == kind


#: A caller's frames: a credential header, then deeper-indented lines that are
#: its value, which reach past the 4,096-character tail.
HEADER_FRAMES = "RuntimeError:\n  Authorization:\n" + "      tok FRAMESECRET\n" * 3500


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("mode", MODES)
def test_the_frames_are_redacted_before_their_tail_is_taken(mode, source):
    line = _format(mode, _record(source, "x", frames=HEADER_FRAMES))

    assert "log_line_cut" in line and "RuntimeError:" in line
    assert "FRAMESECRET" not in line


@pytest.mark.parametrize("source", SOURCES)
def test_console_keeps_the_exceptions_header_after_stack_info(source):
    """``stack_info`` comes first in the console's ``stack``; the exception's
    class header is kept with it."""
    record = _record(source, "boom", _recursion(), stack_info=True)
    if source == "stdlib":
        record.stack_info = "Stack (most recent call last):"

    line = _format("console", record)

    assert "\nStack (most recent call last):\nRecursionError:\n" in line
    assert len(line) <= LOG_LINE_MAX_CHARS


ASTRAL = "\U0001f600" * 300
QUERIES = "https://h.example/?a=1&b=2 " * 12 + "://? " * 10


@pytest.mark.parametrize("fill", [ASTRAL, QUERIES], ids=["astral", "queries"])
@pytest.mark.parametrize("mode", MODES)
def test_the_replacement_is_bounded_whatever_the_kept_keys_hold(mode, fill):
    """Every kept key filled, by a native event (which may set the logger's
    own keys) with characters JSON escapes to twelve, or with dense queries
    the redactor lengthens; the frames and stack as long lines of them."""
    frames = (fill + "\n") * 40
    record = _record("native", fill * 300, frames=frames, stack=frames)
    record.msg.update(
        {key: fill for key in (*CONTEXT_KEYS, "level", "logger", "timestamp")}
    )

    line = _format(mode, record)

    assert len(line) <= LOG_LINE_MAX_CHARS
    if mode == "json":
        fields = json.loads(line)
        assert fields["log_line_cut"] is True and fields["frames_cut"] is True


# --- At or under the limit: as on main ---------------------------------------


def _event_rendering_at(inner, length: int) -> dict:
    """An event dict ``inner`` renders to exactly ``length`` characters."""
    base = {
        "event": "fetch https://h.example/p?token=SECRET ",
        "level": "info",
        "logger": "t",
        "timestamp": "2026-11-28T00:00:00Z",
    }
    pad = length - len(inner(None, "info", dict(base)))
    return {**base, "event": base["event"] + "x" * pad}


@pytest.mark.parametrize("mode", MODES)
def test_a_line_at_or_under_the_limit_renders_as_on_main(mode):
    """``main`` redacted every rendered line: ``redact_log_line`` over the
    inner renderer's output is its line."""
    render = build_log_formatter(mode, False, io.StringIO()).processors[-1]
    inner = render.__wrapped__
    for length in (LOG_LINE_MAX_CHARS, LOG_LINE_MAX_CHARS - 1, 200):
        event = _event_rendering_at(inner, length)
        assert len(inner(None, "info", dict(event))) == length
        main = redact_log_line(inner(None, "info", dict(event)))
        assert render(None, "info", dict(event)) == main
        assert "SECRET" not in main
    over = _event_rendering_at(inner, LOG_LINE_MAX_CHARS + 1)
    assert "log_line_cut" in render(None, "info", over)


def _bytes_renderer(_logger, _name, event_dict):
    return json.dumps(event_dict).encode()


def test_the_bytes_arm_is_bounded_and_unchanged_under_the_limit():
    render = redacting_renderer(_bytes_renderer)
    at = _event_rendering_at(_bytes_renderer, LOG_LINE_MAX_CHARS)
    assert render(None, "info", dict(at)) == redact_log_line_bytes(
        _bytes_renderer(None, "info", dict(at))
    )

    over = {
        "event": "probe https://h.example/p?token=HEADSECRET " + FILLER,
        "detail": "TAILSECRET",
        "level": "info",
    }
    line = render(None, "info", over)

    assert isinstance(line, bytes) and len(line) <= LOG_LINE_MAX_CHARS
    fields = json.loads(line)
    assert fields["log_line_cut"] is True
    assert fields["log_line_chars"] == len(_bytes_renderer(None, "", over))
    assert b"HEADSECRET" not in line and b"TAILSECRET" not in line


# --- Growth over the limit ---------------------------------------------------

STEP = 8
SMALL = LOG_LINE_MAX_CHARS // STEP**2


def _runs(unit: str, n: int) -> str:
    return (unit * (n // len(unit) + 1))[:n]


GROWTH = {
    "urls": lambda n: _runs("see https://h.example/p?q=1 ", n),
    "headers": lambda n: _runs("Authorization: Bearer x\n", n),
    "colons": lambda n: _runs(":", n),
    "separators": lambda n: _runs("a://", n),
    "objects": lambda n: _runs('{"event": "cookie: x", "k": "https://h/?a=1"} ', n),
    # One JSON object, so the decoded path runs, on a value that grows.
    "a json line": lambda n: json.dumps(
        {"event": _runs("cookie: x, https://h/?a=1 ", n), "level": "info"}
    ),
}


@pytest.mark.performance
@pytest.mark.parametrize("arm", ["str", "bytes"])
@pytest.mark.parametrize("payload_at", GROWTH.values(), ids=GROWTH.keys())
def test_redact_log_line_is_linear_up_to_the_limit(payload_at, arm):
    by_arm = {
        "str": (redact_log_line, payload_at),
        "bytes": (redact_log_line_bytes, lambda n: payload_at(n).encode()),
    }
    fn, payload = by_arm[arm]
    assert_linear_growth(
        fn,
        payload,
        small=SMALL,
        step=STEP,
        max_escalations=0,
        undecided_repeats=2,
        label=f"the {arm} arm",
    )


#: Frames shapes: a traceback's lines, and credential blocks the redactor
#: blanks. Their length is set by the recursion limit and the group cap, not
#: by ``LOG_LINE_MAX_CHARS``, so the window runs past it.
FRAMES = {
    "traceback": '  File "/app/a.py", line 10, in f\n    return g(x)\n           ^^^^\n',
    "credential blocks": "  Authorization: Bearer x\n      tok\n",
}


@pytest.mark.performance
@pytest.mark.parametrize("unit", FRAMES.values(), ids=FRAMES.keys())
def test_the_frames_cut_is_linear_past_the_limit(unit):
    assert_linear_growth(
        _cut_frames,
        lambda n: "RecursionError:\n" + _runs(unit, n),
        small=LOG_LINE_MAX_CHARS // STEP,
        step=STEP,
        undecided_repeats=2,
        label="_cut_frames",
    )
