"""The rest of §7's redactor table is blanked on every rendered line (#2891).

``x-api-key``, ``api-key``, ``x-goog-api-key``, ``x-auth-token`` and
``Set-Cookie2`` name a credential by their name alone, as ``cookie`` does
(#2865), and are blanked by the same unit rule on each output the redactor
serves: the API's JSON and console lines, the jobs' formatter and a bytes
renderer. ``www-authenticate``, a scopes list and a flat list are not.
"""

import dataclasses
import json
import pprint
import re

import pytest

from faultmaven.infrastructure.logging import url_redaction
from faultmaven.infrastructure.logging.config import get_logger
from faultmaven.infrastructure.logging.url_redaction import (
    redact_log_line,
    redact_log_line_bytes,
    redacting_renderer,
)
from tests.unit.infrastructure.logging.conftest import _job_line

pytestmark = [pytest.mark.unit, pytest.mark.security]

SECRET = "VALUE"

NAMES = ["x-api-key", "Api-Key", "X-Goog-Api-Key", "x-auth-token", "Set-Cookie2"]

# Each holds SECRET as the header's value, as a header dump, a repr'd dict and
# a repr'd tuple give it.
TEXT_FORMS = [
    "{}: VALUE",
    "headers={{'{}': 'VALUE', 'accept': 'x'}}",
    "[('{}', 'VALUE')]",
    "[(b'{}', b'VALUE')]",
]
TEXT_POSITIVES = [form.format(name) for name in NAMES for form in TEXT_FORMS]


def _raise(headers):
    raise RuntimeError(headers)


# A frame's source line is on the rendered traceback, so each spells its name.
RAISERS = [
    lambda: _raise("401 with {'x-api-key': 'VALUE'}"),
    lambda: _raise("401 with {'Api-Key': 'VALUE'}"),
    lambda: _raise("401 with {'X-Goog-Api-Key': 'VALUE'}"),
    lambda: _raise("401 with {'x-auth-token': 'VALUE'}"),
    lambda: _raise("401 with {'Set-Cookie2': 'VALUE'}"),
]


@dataclasses.dataclass
class Header:
    name: str
    value: str


TEXT_NEGATIVES = [
    "www-authenticate: Bearer realm=x",
    "scopes: x-api-key, cookie",
    "auth-token: x",
    "my-x-api-key: x",
    "api-key header missing",
    "the x-api-key header was rejected",
]


class TestEveryRenderedLine:
    @pytest.mark.parametrize("name", NAMES)
    def test_a_header_in_a_message_and_a_field_is_blanked(self, rendered, name):
        message = f"{name}: VALUE"
        get_logger("faultmaven.p").error(message, field=message)
        output = rendered.buffer.getvalue()
        assert "<redacted>" in output, "the record was not written or not redacted"
        assert SECRET not in output

    @pytest.mark.parametrize("name", NAMES)
    @pytest.mark.parametrize(
        "fields_of",
        [
            lambda name: {"headers": {name: "VALUE", "accept": "kept"}},
            lambda name: {"headers": [{"name": name, "value": "VALUE"}]},
            lambda name: {"headers": [(name, "VALUE"), ("accept", "kept")]},
            lambda name: {"headers": [(name.encode(), b"VALUE")]},
            lambda name: {"headers": [["accept", "kept"], [name, "VALUE"]]},
            lambda name: {f"headers.{name}": "VALUE"},
        ],
        ids=["dict", "record", "pair", "bytes-pair", "list-pair", "dotted-key"],
    )
    def test_a_structured_value_is_blanked(self, rendered, name, fields_of):
        get_logger("faultmaven.p").error("call failed", **fields_of(name), later="ok")
        output = rendered.buffer.getvalue()
        assert "call failed" in output
        assert SECRET not in output

    @pytest.mark.parametrize("raise_it", RAISERS, ids=NAMES)
    def test_a_traceback_carrying_a_header_is_blanked(self, rendered, raise_it):
        try:
            raise_it()
        except RuntimeError:
            get_logger("faultmaven.p").exception("failed")
        output = rendered.buffer.getvalue()
        assert "failed" in output and "RuntimeError:" in output
        assert SECRET not in output

    @pytest.mark.parametrize("message", TEXT_NEGATIVES)
    def test_prose_and_a_challenge_are_written_unchanged(self, rendered, message):
        get_logger("faultmaven.p").info(message)
        assert message in rendered.buffer.getvalue()

    @pytest.mark.parametrize("name", ["authorization", "x-api-key"])
    def test_a_keyword_form_record_is_blanked(self, rendered, name):
        """A record whose fields are keywords: a grpc metadatum's repr in the
        message, and a dataclass as a field (its repr under both renderers)."""
        logger = get_logger("faultmaven.p")
        logger.error(f"(_Metadatum(key='{name}', value='Bearer VALUE'),)")
        logger.error("call failed", header=Header(name, "VALUE"))
        lines = rendered.buffer.getvalue().splitlines()
        assert len(lines) == 2
        assert all("<redacted>" in line and SECRET not in line for line in lines)

    @pytest.mark.parametrize("nested", [False, True], ids=["list", "under-a-key"])
    def test_a_pprint_wrapped_list_of_pairs_is_blanked(self, rendered, nested):
        """Under a dict key pprint indents the next pair to its bracket's
        column, past any fixed gap."""
        pairs = [["host", "example.com"], ["authorization", "Bearer VALUE"]]
        headers = pprint.pformat({"headers": pairs} if nested else pairs, width=40)
        assert "\n" in headers, "pprint did not wrap the list"
        get_logger("faultmaven.p").error("call failed", headers=headers)
        output = rendered.buffer.getvalue()
        assert "call failed" in output
        assert SECRET not in output

    def test_a_flat_list_is_written_unchanged(self, rendered):
        """The table excludes flat lists: a scopes list cannot be told from a
        header pair, so neither loses its second item."""
        get_logger("faultmaven.p").info(
            "granted",
            scopes=["authorization", "cases:read"],
            allow_headers=["X-API-Key", "Content-Type"],
        )
        output = rendered.buffer.getvalue()
        assert "cases:read" in output and "Content-Type" in output
        assert "<redacted>" not in output


class TestTheJobsFormatter:
    @pytest.mark.parametrize("log_format", ["console", "json"])
    @pytest.mark.parametrize("message", TEXT_POSITIVES)
    def test_each_form_blanks_the_value(self, log_format, message):
        output = _job_line(log_format, message)
        assert SECRET not in output
        assert "<redacted>" in output


class TestJsonLines:
    @pytest.mark.parametrize(
        "record",
        [
            *(
                record
                for name in NAMES
                for record in (
                    {"headers": {name: "VALUE"}, "after": "kept"},
                    {f"headers.{name}": "VALUE", "after": "kept"},
                    {"h": [{"name": name, "value": "VALUE"}], "after": "kept"},
                    {"h": [[name, "VALUE"]], "after": "kept"},
                )
            ),
            # A "_" segment before the name, read as a key is (§7's table).
            *(
                record
                for name in ("HTTP_AUTHORIZATION", "HTTP_COOKIE")
                for record in (
                    {"e": [[name, "VALUE"]], "after": "kept"},
                    {"e": [{"name": name, "value": "VALUE"}], "after": "kept"},
                )
            ),
        ],
    )
    def test_the_value_goes_and_the_line_stays_json(self, record):
        redacted = redact_log_line(json.dumps(record))
        assert SECRET not in redacted
        assert json.loads(redacted)["after"] == "kept"

    def test_an_underscore_spelling_is_a_stated_gap(self):
        # Underscore spellings are out of scope; no FaultMaven code logs a WSGI environ.
        for record in (
            {"e": [["HTTP_X_API_KEY", "VALUE"]]},
            {"e": [{"name": "HTTP_X_API_KEY", "value": "VALUE"}]},
        ):
            line = json.dumps(record)
            assert redact_log_line(line) == line

    def test_a_scopes_list_is_unchanged(self):
        line = json.dumps({"event": "e", "scopes": ["x-api-key", "authorization"]})
        assert redact_log_line(line) == line


# A list of pairs, then "," + 8 blanks + an escaped CRLF + 64 blanks + "[" +
# 8 blanks + "b", two backslashes and a quote, and a 65-character segment.
_LONGEST = (
    "[['a', 'x'],"
    + " " * 8
    + "\\r\\n"
    + " " * 64
    + "["
    + " " * 8
    + "b\\\\'"
    + "h" * 64
    + "_"
)


class TestTheTextRule:
    def test_the_search_back_covers_the_longest_opener(self):
        pattern = url_redaction._PAIR_OPEN
        widest = re._parser.parse(pattern.pattern, pattern.flags).getwidth()[1]
        assert url_redaction._PAIR_OPEN_WIDTH >= widest
        assert len(_LONGEST) - len("[['a', 'x']") == widest

    @pytest.mark.parametrize(
        "line, expected",
        [
            ("HTTP_AUTHORIZATION: Bearer VALUE", "HTTP_AUTHORIZATION: <redacted>"),
            ("HTTP_COOKIE=sid=VALUE", "HTTP_COOKIE=<redacted>"),
            ("[('x-api-key', 'VALUE')]", "[('x-api-key', <redacted>"),
            ("[(b'x-api-key', b'VALUE')]", "[(b'x-api-key', <redacted>"),
            # A "_" segment before a pair's name, and a repr of a repr.
            ("[('HTTP_COOKIE', 'sid=VALUE')]", "[('HTTP_COOKIE', <redacted>"),
            (
                repr("[('authorization', \"Bearer VALUE\")]"),
                "'[(\\'authorization\\', <redacted>",
            ),
            # A list of pairs, as a repr writes decoded JSON headers.
            ("[['a', 'x'], ['cookie', 'VALUE']]", "[['a', 'x'], ['cookie', <redacted>"),
            # Wrapped at a CRLF, raw and escaped.
            (
                "[['a', 'x'],\r\n ['cookie', 'VALUE']]",
                "[['a', 'x'],\r\n ['cookie', <redacted>",
            ),
            (
                "[['a', 'x'],\\r\\n ['cookie', 'VALUE']]",
                "[['a', 'x'],\\r\\n ['cookie', <redacted>",
            ),
            # The longest opener: every bounded run at its bound.
            (
                _LONGEST + "cookie\\\\', b\\\\'VALUE\\\\']]",
                _LONGEST + "cookie\\\\', <redacted>",
            ),
            # Prose naming a header with a separator loses the rest of its
            # line: the safe side, as with "authorization:".
            ("Invalid api-key: VALUE", "Invalid api-key: <redacted>"),
            ("Set-Cookie2: sid=VALUE\r\nX: kept", "Set-Cookie2: <redacted>\r\nX: kept"),
        ],
    )
    def test_the_unit_is_blanked(self, line, expected):
        assert redact_log_line(line) == expected

    @pytest.mark.parametrize(
        "line",
        [
            *TEXT_NEGATIVES,
            "scopes=['authorization', 'cases:read']",
            "allow_headers=['X-API-Key', 'Content-Type']",
        ],
    )
    def test_the_line_is_unchanged(self, line):
        assert redact_log_line(line) == line


class TestTheBytesForm:
    @pytest.mark.parametrize("line", [*TEXT_POSITIVES, *TEXT_NEGATIVES])
    def test_the_bytes_form_matches_the_str_form(self, line):
        assert redact_log_line_bytes(line.encode()) == redact_log_line(line).encode()

    @pytest.mark.parametrize("line", TEXT_POSITIVES)
    def test_a_bytes_renderer_is_redacted(self, line):
        wrapped = redacting_renderer(lambda *_: line.encode())
        output = wrapped(None, "info", {})
        assert SECRET.encode() not in output
        assert b"<redacted>" in output
