"""Strip query strings from URLs in anything FaultMaven logs.

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
"""

import logging
import re
from typing import Callable

# Any scheme, not just http(s): postgresql://, redis://, amqp:// and mongodb://
# DSNs carry credentials in query options (?password=, ?sslmode=, ?authSource=)
# and a logged DSN is an ordinary thing to find in a connection error.
#
# ‼ The pre-query class excludes "?" on purpose. With "?" allowed, the greedy
# group backtracks to the LAST question mark in the token, so
# "https://api/v1?key=SECRET?" redacted only the trailing "?" and shipped the
# key verbatim. Excluding it pins the match to the FIRST "?", which is where a
# query string actually starts (RFC 3986 permits "?" inside the query itself).
#
# ESC is excluded so the console renderer's colour reset after a URL is not
# swallowed with the query string.
_SCHEME = r"[a-zA-Z][a-zA-Z0-9+.\-]*://"
_URL_QUERY = re.compile(_SCHEME + r"[^\s\"'<>?\x1b]*" + r"\?[^\s\"'<>\x1b]*")
_URL_QUERY_BYTES = re.compile(_URL_QUERY.pattern.encode())


def _replacement(match: "re.Match") -> str:
    url = match.group(0)
    return url[: url.index("?")] + "?<redacted>"


def _replacement_bytes(match: "re.Match") -> bytes:
    url = match.group(0)
    return url[: url.index(b"?")] + b"?<redacted>"


def redact_urls(text: str) -> str:
    """Strip the query string from every URL in ``text``.

    Two substring checks run before the pattern. A line with no URL is the
    common case, and ``in`` on a str is a C-level scan, so it never pays for
    the regex.
    """
    if "?" not in text or "://" not in text:
        return text
    return _URL_QUERY.sub(_replacement, text)


def redact_urls_bytes(data: bytes) -> bytes:
    """``redact_urls`` for renderers that emit bytes."""
    if b"?" not in data or b"://" not in data:
        return data
    return _URL_QUERY_BYTES.sub(_replacement_bytes, data)


def redacting_renderer(renderer: Callable) -> Callable:
    """Wrap a structlog renderer so its output carries no query strings.

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
