"""No rendered log line carries a credential header's value (#2865).

Rule 2 of #2844's ruling: the one rendered-line redactor blanks the values of
``set-cookie``, ``cookie``, ``authorization`` (and ``proxy-authorization``).
From the header's separator it blanks to the end of the enclosing unit, so it
never has to measure where a value ends. A JSON line is redacted on its decoded
value and stays valid JSON. Every positive below reached the line unchanged
before this rule existed.
"""

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
    get_logger,
)
from faultmaven.infrastructure.logging.url_redaction import (
    RedactingFormatter,
    redact_credential_headers,
    redact_log_line,
    redact_log_line_bytes,
    redacting_renderer,
)
from tests.wallclock import assert_linear_growth

pytestmark = [pytest.mark.unit, pytest.mark.security]

SECRET = "VALUE"

# Each holds SECRET as a credential header's value, in a form a client's
# exception text, a repr or a header dump gives it.
TEXT_POSITIVES = [
    "headers: {'date': 'x', 'set-cookie': 'AWSALB=VALUE; Path=/'}",
    "Headers({'authorization': 'Bearer VALUE', 'cookie': 'sid=x'})",
    "Headers({'authorization': 'Bearer x', 'cookie': 'sid=VALUE'})",
    "HTTP/1.1 401\r\nSet-Cookie: sid=VALUE; HttpOnly\r\nX: kept",
    'failed: {"Authorization": "Bearer t\\\\VALUE", "x": 1}\nnext',
    "Authorization: Bearer VALUE",
    "authorization=Basic VALUE retry=3",
    "{'Authorization': b'Bearer VALUE'}",
    "[(b'authorization', b'Bearer VALUE')]",
    "headers=[('Cookie', 'a=VALUE; b=2')] next=1",
    'Set-Cookie: sid="x"; tok=VALUE',
    "{'cookie': \"it's VALUE\", 'x': 1}",
    "Proxy-Authorization: Basic VALUE",
    "E('b: {\"d\": 1}, h: {\\'set-cookie\\': \\'a=VALUE; P=/\\'}')",
    "cookie=sid=x; tok=VALUE next=1",
    "authorization=Splunk VALUE next=1",
    "authorization=AWS4-HMAC-SHA256 Credential=A/x, Signature=VALUE",
    'authorization=Digest username="u", realm="r", response="VALUE"',
    "{'set-cookie': ['a=VALUE', 'b=x'], 'x': 1}",
    "headers['Authorization'] = 'Bearer VALUE'",
    "Cookie\tsid=VALUE",
    "raw 'HTTP/1.1 401\\r\\nSet-Cookie: VALUE'",
    "\x1b[36mauthorization\x1b[0m=\x1b[35mBearer VALUE\x1b[0m",
]

# Lines naming the words that carry no header value: the first-party log
# strings that name them (git grep over faultmaven/), and prose.
TEXT_NEGATIVES = [
    "Authorization error: invalid_grant",
    "Invalid authorization header format (not Bearer)",
    "Authorization approval error: boom",
    "Failed to record authorization request metric: x",
    "error_category=authorization detail=foo",
    "the cookies jar is empty: x-cookie: 1",
    "Authorization failed for user 42: missing scope cases:write",
    "user cleared the cookie banner; retrying",
    "exchanged authorization_code=abc for a token",
    "kubectl: error: Unauthorized; authorization mode is RBAC",
    "the request lacked a Cookie header",
    "www-authenticate: Bearer realm=x",
]

JSON_NEGATIVES = [
    {
        "event": "Authorization error: x",
        "category": "authorization",
        # A scopes list: a flat pair is never read as a header (a stated gap).
        "scopes": ["authorization", "cases:read"],
    },
    {"event": "e", "cookies": 3, "x-cookie": "v", "cookie_count": 2},
]


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
    finally:
        structlog.configure(**saved[0])
        root.setLevel(saved[1])
        root.handlers[:] = saved[2]
        logging_config._logger_config = saved_singleton


def _json_lines(output):
    return [json.loads(row) for row in output.splitlines() if row.startswith('{"')]


class TestEveryRenderedLine:
    @pytest.mark.parametrize("message", TEXT_POSITIVES)
    def test_a_header_in_a_message_and_a_field_is_blanked(self, rendered, message):
        get_logger("faultmaven.p").error(message, field=message)
        output = rendered.buffer.getvalue()
        assert "<redacted>" in output, "the record was not written or not redacted"
        assert SECRET not in output
        if rendered.mode == "json":
            assert len(_json_lines(output)) == 1

    @pytest.mark.parametrize(
        "fields",
        [
            {"headers": {"Cookie": "sid=VALUE", "accept": "kept"}},
            {"headers": [("authorization", "Bearer VALUE"), ("accept", "kept")]},
            {"headers": [{"name": "Set-Cookie", "value": "VALUE"}]},
            {"headers": {"set-cookie": ["a=VALUE", "b=x"]}},
            {"cookie": {"sid": "VALUE"}},
            {"error": ValueError("status 401, headers {'set-cookie': 'VALUE'}")},
        ],
        ids=["dict", "pairs", "har", "list-value", "structured", "exception"],
    )
    def test_a_structured_value_is_blanked(self, rendered, fields):
        get_logger("faultmaven.p").error("call failed", **fields, later="kept")
        output = rendered.buffer.getvalue()
        assert "call failed" in output
        assert SECRET not in output
        if rendered.mode == "json":
            (line,) = _json_lines(output)
            assert line["later"] == "kept"

    def test_a_traceback_carrying_a_header_is_blanked(self, rendered):
        try:
            raise RuntimeError("401 with {'set-cookie': 'sid=VALUE'}")
        except RuntimeError:
            get_logger("faultmaven.p").exception("failed")
        assert SECRET not in rendered.buffer.getvalue()

    @pytest.mark.parametrize("message", TEXT_NEGATIVES)
    def test_prose_naming_the_words_is_written_unchanged(self, rendered, message):
        get_logger("faultmaven.p").info(message)
        assert message in rendered.buffer.getvalue()

    def test_a_bytes_renderer_is_redacted(self):
        line = "GET https://h/p?q=1 Cookie: sid=VALUE"
        wrapped = redacting_renderer(lambda *_: line.encode())
        assert (
            wrapped(None, "info", {})
            == b"GET https://h/p?<redacted> Cookie: <redacted>"
        )

    def test_the_bytes_form_matches_the_str_form(self):
        for line in [*TEXT_POSITIVES, *TEXT_NEGATIVES, "\xff cookie: VALUE İ"]:
            data = line.encode("utf-8", "surrogateescape")
            assert redact_log_line_bytes(data) == redact_log_line(line).encode()
        assert SECRET.encode() not in redact_log_line_bytes(b"\xff cookie: VALUE")


def _job_line(monkeypatch, log_format, message):
    """``message`` as a job writes it, through ``_job_log_format``'s template."""
    from faultmaven.config import settings
    from faultmaven.jobs.run import _job_log_format

    chosen = types.SimpleNamespace(log_output_format=log_format)
    monkeypatch.setattr(
        settings, "get_settings", lambda: types.SimpleNamespace(logging=chosen)
    )
    record = logging.LogRecord("job", logging.ERROR, __file__, 1, message, (), None)
    return RedactingFormatter(_job_log_format()).format(record)


class TestTheJobsFormatter:
    @pytest.mark.parametrize("log_format", ["text", "json"])
    @pytest.mark.parametrize("message", TEXT_POSITIVES)
    def test_each_template_blanks_the_value(self, monkeypatch, log_format, message):
        output = _job_line(monkeypatch, log_format, message)
        assert SECRET not in output
        assert "<redacted>" in output

    @pytest.mark.parametrize(
        "message, still_json",
        [
            ("headers {'cookie': 'sid=VALUE'} retry", True),
            ('authorization="Bearer VALUE"', False),  # a raw quote: not JSON
        ],
    )
    def test_the_json_template(self, monkeypatch, message, still_json):
        output = _job_line(monkeypatch, "json", message)
        assert output.startswith('{"timestamp"')
        assert SECRET not in output
        if still_json:
            assert json.loads(output)["level"] == "ERROR"


class TestJsonLines:
    @pytest.mark.parametrize(
        "record",
        [
            # Escapes decode before the rule runs: a \uXXXX boundary and
            # JSON-escaped colour codes are the characters themselves.
            {"event": "sent—Authorization: Bearer VALUE", "after": "kept"},
            {"event": "│cookie: VALUE", "after": "kept"},
            {
                "event": "\x1b[36mauthorization\x1b[0m=\x1b[35mVALUE\x1b[0m",
                "after": "kept",
            },
            {"error": 'ApiError("{\\"cookie\\": \\"VALUE\\"}")', "after": "kept"},
            {"event": "line1\nAuthorization: Bearer VALUE\nline3", "after": "kept"},
            {"headers": {"Cookie": "VALUE", "accept": "kept"}, "after": "kept"},
            {"headers.cookie": "VALUE", "after": "kept"},
            {"http.request.header.authorization": ["Bearer VALUE"], "after": "kept"},
            {"Cookie ": "VALUE", "after": "kept"},
            {"headers": [{"name": "cookie", "value": "VALUE"}], "after": "kept"},
            {
                "headers": [["authorization", "VALUE"], ["accept", "kept"]],
                "after": "kept",
            },
            {"headers": {"set-cookie": ["VALUE", "VALUE"]}, "after": "kept"},
        ],
    )
    def test_the_value_goes_and_the_line_stays_json(self, record):
        redacted = redact_log_line(json.dumps(record))
        assert SECRET not in redacted
        assert json.loads(redacted)["after"] == "kept"

    def test_later_text_in_a_multi_line_string_is_kept(self):
        line = json.dumps({"event": "line1\nAuthorization: Bearer VALUE\nline3"})
        assert json.loads(redact_log_line(line))["event"] == (
            "line1\nAuthorization: <redacted>\nline3"
        )

    @pytest.mark.parametrize("record", JSON_NEGATIVES)
    def test_a_json_line_with_nothing_to_blank_is_unchanged(self, record):
        line = json.dumps(record)
        assert redact_log_line(line) == line

    def test_an_unchanged_line_keeps_its_own_bytes(self):
        """Re-serialized only when something changed."""
        line = '{"event":"cookie jar",  "n":1.50}'
        assert redact_credential_headers(line) == line

    def test_a_line_too_deep_for_the_decoder_falls_back_to_text(self):
        line = '{"a": ' + "[" * 100_000 + "]" * 100_000 + ', "cookie": "VALUE"}'
        redacted = redact_credential_headers(line)
        assert SECRET not in redacted
        assert redacted.endswith('"cookie": <redacted>')

    def test_a_line_deep_enough_for_the_walk_falls_back_to_text(self, monkeypatch):
        """The decoder may accept what the recursive walk cannot."""
        import faultmaven.infrastructure.logging.url_redaction as module

        def too_deep(_value):
            raise RecursionError

        monkeypatch.setattr(module, "_redact_json_value", too_deep)
        line = json.dumps({"x": 1, "cookie": "VALUE"})
        assert redact_credential_headers(line) == '{"x": 1, "cookie": <redacted>'


class TestTheUnitBlanked:
    """The trade the rule makes: the rest of the unit goes with the value."""

    @pytest.mark.parametrize(
        "line, expected",
        [
            ("Cookie: none set", "Cookie: <redacted>"),
            ("authorization: denied for user 42", "authorization: <redacted>"),
            ("authorization=rbac case_id=c1", "authorization=<redacted>"),
            ("Set-Cookie: sid=S\r\nX: kept", "Set-Cookie: <redacted>\r\nX: kept"),
            ("Set-Cookie:\r\n a=S\r\nX: kept", "Set-Cookie:<redacted>\r\nX: kept"),
            (
                "GET https://h/p?k=K failed; cookie=S",
                "GET https://h/p?<redacted> failed; cookie=<redacted>",
            ),
        ],
    )
    def test_to_the_end_of_the_line(self, line, expected):
        assert redact_log_line(line) == expected

    def test_deeper_indented_lines_go_with_it(self):
        dump = json.dumps({"headers": {"cookie": ["a", "S1"], "x": "S2"}}, indent=2)
        redacted = redact_log_line(f"response:\n{dump}\nnext: kept")
        assert "S1" not in redacted
        assert redacted.endswith("\n  }\n}\nnext: kept")

    def test_a_coloured_value_is_closed_with_a_reset(self):
        assert redact_log_line("\x1b[36mcookie\x1b[0m=\x1b[35mS") == (
            "\x1b[36mcookie\x1b[0m=<redacted>\x1b[0m"
        )

    def test_a_console_event_naming_the_header_loses_its_later_fields(self):
        render = redacting_renderer(structlog.dev.ConsoleRenderer(colors=False))
        line = render(None, "error", {"event": "authorization: denied", "user_id": 42})
        assert line == "authorization: <redacted>"


class TestTheCostIsLinear:
    """Each shape names the words densely. Growth shape, not a clock (#1579)."""

    @pytest.mark.performance
    @pytest.mark.parametrize(
        "label, payload_at",
        [
            ("prose-dense text", lambda n: "the cookie jar, authorization mode " * n),
            # Values of 100 characters: with "x" alone, a search back to the
            # line start that rescanned the whole string read linear until
            # 32k headers (#2865 lane measurement).
            (
                "in-string dense",
                lambda n: json.dumps({"e": ("cookie: " + "x" * 100 + "\n") * n}),
            ),
            ("key-dense", lambda n: json.dumps({"e": [{"cookie": 1}] * n})),
            ("pair-dense", lambda n: json.dumps({"e": [["cookie", "x"]] * n})),
            (
                "nested key",
                lambda n: json.dumps({"e": [{"r": {"h": {"authorization": 1}}}] * n}),
            ),
        ],
    )
    def test_each_dense_shape(self, label, payload_at):
        assert_linear_growth(
            redact_credential_headers, payload_at, small=64, label=label
        )
