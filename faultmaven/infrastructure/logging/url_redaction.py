"""Strip query strings from URLs, and credential header values, in anything
FaultMaven logs.

A URL's query string is a credential channel here. The SSO callback carries
the identity provider's authorization code in one; the web-search tool reaches
Google CSE as ``?key=<GOOGLE_API_KEY>&q=<the investigation's search text>``.
Five sinks leaked through it, each fixed at the call site, two of them found
only after the first three were fixed. About 470 first-party call sites
interpolate an exception into a log message, and an HTTP client's exception
message embeds the request URL, so the next one arrives the same way.

**Redaction happens at render, on the finished line.** The first version of
this walked the event dict and redacted values by type, and review found that
every type it did not handle leaked: ``bytes`` (decoded to a string one
processor later), ``set``, any object the renderer falls back to ``repr`` for
— an exception passed as ``error=e`` being the obvious one — dictionary
*keys*, and anything nested deeper than the walk went. Each was a separate
hole with a separate fix, and the list had no natural end, because the walk
ran *before* the renderer turned those values into text.

Rendering first collapses all of that: by the time a record is a line, every
value is a string, and one pass covers the message, the fields, the keys, the
exception traceback and the nested structures alike. It is also cheaper — one
scan of one string instead of a recursive walk of a dict — and it deletes the
depth limit, the key/value distinction and the type list entirely.

**Why query strings and not secrets in general.** Checked against the five
leaks actually found: an OAuth authorization code is an opaque string and case
content is free text, so neither is pattern-matchable. A secrets-detection
sweep would have caught one of the five and would have to reason about every
value in a product whose logs legitimately carry evidence, where IPs and
hostnames are the signal. Stripping a query string cannot blank an
investigation's own data.

Four header names are the exception, as defence in depth under the server-log
rule (``utils/exception_name.py``, #2844): ``set-cookie``, ``cookie``,
``authorization`` and ``proxy-authorization``. A client's exception text can
carry a response's headers (an Opik ``ApiError`` carries ``set-cookie``), and
these are pattern-matchable: a fixed name, a separator, a value. Their values
are blanked; prose that merely uses the words is not.
"""

import logging
import re
from typing import Callable

# Anchored on the literal "://".
#
# The pattern used to start with a scheme class, `[a-zA-Z][a-zA-Z0-9+.-]*://`,
# and that is a polynomial ReDoS: at every position the engine consumes a long
# alphanumeric run and only then fails on "://", so a line of n such characters
# costs O(n^2). Measured before this change, one 16 KB line took 443 ms of
# blocking CPU and 64 KB took 8 seconds, with time quadrupling as length
# doubled. Log lines carry case content, which is supplied by whoever is being
# helped, so that input is reachable.
#
# Leading with the literal lets the engine scan for it directly, so a failed
# match is cheap to locate. The scheme is deliberately NOT matched: it sits to
# the left of "://", is not part of what gets replaced, and leaving it out
# covers every scheme at once -- postgresql://, redis://, amqp:// and mongodb://
# DSNs all carry credentials in query options.
#
# The pre-query class excludes "?" so the greedy run stops at the FIRST question
# mark. Admitting it shipped the credential verbatim (RFC 3986 permits "?"
# inside a query): the group backtracked to the LAST one, and with the query
# optional the run would swallow the query whole. That exclusion is the ONLY
# thing pinning the match, which is why the replacement below reuses the
# group's boundary and never looks for a "?" itself -- one that truncated at the
# first "?" would mask the bug and make the mutation test for it vacuous.
#
# The query is optional, so a run with no "?" after it still matches, whole,
# and is put back unchanged; the scan resumes past it. With the query required,
# a rejected run was retried at every "://" inside it, each retry scanning to
# the same end: a line of n "://" cost O(n^2), 18.6 seconds at 64 KB (#2312).
# Skipping those retries changes no output: every "://" inside the run stops
# where the run does, and that stop is not a "?". The price, once a line holds
# a "?", is a Python call per "://" run, query or not, and a copy of each run
# with no query into the result: both linear in the line.
#
# ESC is excluded so the console renderer's colour reset after a URL is not
# swallowed with the query string.
#
# The query runs over a JSON-serialised line, where a quote ending the string
# is escaped (\"). A backslash is part of the query only as an escaped pair
# (\\) or before anything but a quote: a lone backslash before a quote is that
# quote's escape, and swallowing it left a bare quote and an invalid JSON line
# (#2309). Excluding every backslash instead would stop the redaction at an
# escaped backslash and leave the rest of the query visible.
#
# Written unrolled and possessive -- a run of ordinary characters, then any
# number of (escape-ish backslash, run) -- rather than as a starred
# alternation: Python's engine keeps a backtrack frame per pass of a starred
# alternation, so that form held ~180 MB for a 1 MiB query. This one holds none
# and runs at the speed of the plain class. It needs CPython 3.11.5's fixes for
# possessive repeats (python/cpython#100061, #106052), which requires-python
# sets as the floor: on 3.11.4 the escaped quote after a query goes with it, so
# a JSON line stays valid but loses that quote. Nothing leaks.
_QUERY_CHAR = r"[^\s\"'<>\x1b\\]"
_URL_QUERY = re.compile(
    r"(://[^\s\"'<>?\x1b]*)"
    rf"(\?{_QUERY_CHAR}*+(?:(?:\\\\|\\(?!\")){_QUERY_CHAR}*+)*+)?"
)
_URL_QUERY_BYTES = re.compile(_URL_QUERY.pattern.encode())


# A match that ends where the run does has no query. Testing the group itself
# (``match[2] is None``) would copy the whole query to find that out.
def _redact_query(match: re.Match[str]) -> str:
    return match[0] if match.end() == match.end(1) else match[1] + "?<redacted>"


def _redact_query_bytes(match: re.Match[bytes]) -> bytes:
    return match[0] if match.end() == match.end(1) else match[1] + b"?<redacted>"


def redact_urls(text: str) -> str:
    """Strip the query string from every URL in ``text``, and blank the value
    of every credential header (:func:`redact_credential_headers`).

    Two substring checks run before the URL pattern. A line with no URL is the
    common case, and ``in`` on a str is a C-level scan, so it never pays for
    the regex.
    """
    if "?" in text and "://" in text:
        text = _URL_QUERY.sub(_redact_query, text)
    return redact_credential_headers(text)


def redact_urls_bytes(data: bytes) -> bytes:
    """``redact_urls`` for renderers that emit bytes."""
    if b"?" in data and b"://" in data:
        data = _URL_QUERY_BYTES.sub(_redact_query_bytes, data)
    return redact_credential_headers(data)


# A credential header's value (#2844). The head is the name, optionally quoted,
# then ``:`` or ``=`` (ANSI colour codes allowed either side, as the console
# renderer writes them) or a tuple's ``', '``, then an opening quote,
# optionally ``b``-prefixed. The body for that opener measures the value:
# * quoted, to the matching close; a quote of the other kind does not end it;
# * unquoted after ``:`` (a header line), to the end of the line or a ``\n``
#   escape, so quoted cookie attributes go too; a folded line is not chased;
# * unquoted after ``=``, to the next whitespace, plus one more token after an
#   auth scheme (``Bearer``, ``Basic``, ...), so later fields stay.
# In a JSON line (the JSON renderer's ``{"...``) a raw ``"`` only delimits a
# string, so no value runs past one; a quote inside a string is ``\"``, and a
# raw ``"`` opens a value only as a key's (``"cookie": "..."``). Backslashes
# are consumed in pairs, so a value never ends inside an escape and the line
# stays valid JSON (#2309). Every repeat is possessive, as above, and the scan
# resumes past each value, so the cost is linear in the line.
#
# The name is not inside a longer word (``x-cookie``, ``cookies``), though it
# may follow an escape's letter (``\n``) or a colour code's ``m``. The plain
# test runs first and rejects most positions on its own.
_NAME = (
    r"(?:(?<![\w-])|(?<=[nrtm])(?:(?<=\\[nrt])|(?<=\x1b\[\dm)|(?<=\x1b\[\d\dm)"
    r"|(?<=;\d\dm)|(?<=;\dm)))(?:set-cookie|proxy-authorization|authorization|cookie)"
)
_ANSI = r"(?:\x1b\[[0-9;]*+m)*+"
_SCHEME = r"(?:(?:bearer|basic|digest|token|negotiate|oauth)[ \t]++)?+"
_TEXT_HEAD = (
    rf"{_NAME}(?:\\?[\"'])?{_ANSI}"
    rf"(?:[ \t]*+(?P<sep>[:=])[ \t]*+|(?<=[\"']),\s*+(?=b?(?:'|\"|\\\"))){_ANSI}"
    r"(?:b?(?P<open>'|\"|\\\"))?"
)
_JSON_HEAD = (
    rf"{_NAME}(?:\"[ \t]*+:[ \t]*+(?P<key>\")|(?:'|\\\")?"
    r"(?:[ \t]*+(?P<sep>[:=])[ \t]*+|(?<=[\"']),\s*+(?=b?(?:'|\\\")))"
    r"(?:b?(?P<open>'|\\\"))?)"
)


def _run(stop: str, unpaired: str = "") -> str:
    """Characters but ``stop``; a backslash takes the next character with it
    unless that one is in ``unpaired``."""
    escape = rf"\\[^{unpaired}]" if unpaired else r"\\."
    return rf"[^{stop}\\]*+(?:{escape}[^{stop}\\]*+)*+"


_BODIES = {
    False: {  # a text line
        "'": _run(r"'\r\n", r"\r\n"),
        '"': _run(r"\"\r\n", r"\r\n"),
        '\\"': _run(r"\"\r\n", r"\"\r\n"),
        ":": _run(r"\r\n\x1b", "nr"),
        "=": rf"{_SCHEME}[^\s\x1b]*+",
    },
    True: {  # a JSON line; a raw ``"`` opens only a key's value
        '"': _run('"'),
        "'": _run("'\"", "nr"),
        '\\"': _run('"', '"nr'),
        ":": _run('"', "nr"),
        "=": _SCHEME + _run(r"\s\"", "nrt"),
    },
}


def _compile(pattern: str, as_bytes: bool) -> re.Pattern:
    return re.compile(pattern.encode() if as_bytes else pattern, re.IGNORECASE)


_HEADER_PATTERNS = {
    (as_bytes, json_line): (
        _compile(_JSON_HEAD if json_line else _TEXT_HEAD, as_bytes).search,
        {
            (key.encode() if as_bytes else key): _compile(body, as_bytes).match
            for key, body in _BODIES[json_line].items()
        },
    )
    for as_bytes in (False, True)
    for json_line in (False, True)
}


def redact_credential_headers(text):
    """Blank the value of every ``set-cookie``, ``cookie``, ``authorization``
    and ``proxy-authorization`` header in ``text`` (str or bytes).

    A line naming neither ``cookie`` nor ``authorization`` never pays for the
    pattern.
    """
    as_bytes = isinstance(text, bytes)
    cast = str.encode if as_bytes else str
    lowered = text.lower()
    if cast("cookie") not in lowered and cast("authorization") not in lowered:
        return text
    json_line = text.startswith(cast('{"'))
    head, bodies = _HEADER_PATTERNS[(as_bytes, json_line)]
    pieces, position = [], 0
    while found := head(text, position):
        opener = (json_line and found["key"]) or found["open"] or found["sep"]
        end = bodies[opener](text, found.end()).end()
        pieces.append(text[position : found.end()])
        if end > found.end():
            pieces.append(cast("<redacted>"))
        position = end
    pieces.append(text[position:])
    return cast("").join(pieces)


def redacting_renderer(renderer: Callable) -> Callable:
    """Wrap a structlog renderer so its output carries no query strings or
    credential header values.

    Applied to whichever renderer ``configure_structlog`` selects, so the rule
    holds for JSON and console output alike, and for foreign stdlib records as
    well as native structlog events — they converge on the same formatter.
    """

    def render(logger, method_name, event_dict):
        rendered = renderer(logger, method_name, event_dict)
        if isinstance(rendered, str):
            return redact_urls(rendered)
        if isinstance(rendered, bytes):
            return redact_urls_bytes(rendered)
        return rendered

    # The standard unwrap convention, so anything inspecting the chain can
    # still see WHICH renderer is installed. That LOG_FORMAT reaches the right
    # renderer has its own gate (#1338) which asserts on the renderer's type,
    # and wrapping must not blind it.
    render.__wrapped__ = renderer
    return render


class RedactingFormatter(logging.Formatter):
    """A stdlib formatter that redacts its own output.

    For log paths that deliberately do not use structlog — the maintenance
    jobs configure plain ``logging.basicConfig`` so their output stays a
    process's own stdout — the rule has to hold there too, or the exception
    from a failing job is exactly the record that carries a DSN.
    """

    def format(self, record: logging.LogRecord) -> str:
        return redact_urls(super().format(record))
