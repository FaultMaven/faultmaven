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
# mark. Admitting it let the group backtrack to the LAST one, which shipped the
# credential verbatim; RFC 3986 permits "?" inside a query. That exclusion is
# the ONLY thing pinning the match, which is why the replacement below is a
# capture group rather than a function -- a function that truncated at the first
# "?" would mask the bug and make the mutation test for it vacuous.
#
# ESC is excluded so the console renderer's colour reset after a URL is not
# swallowed with the query string.
_URL_QUERY = re.compile(r"(://[^\s\"'<>?\x1b]*)\?[^\s\"'<>\x1b]*")
_URL_QUERY_BYTES = re.compile(_URL_QUERY.pattern.encode())
_REPLACEMENT = r"\1?<redacted>"
_REPLACEMENT_BYTES = rb"\1?<redacted>"


def redact_urls(text: str) -> str:
    """Strip the query string from every URL in ``text``.

    Two substring checks run before the pattern. A line with no URL is the
    common case, and ``in`` on a str is a C-level scan, so it never pays for
    the regex.
    """
    if "?" not in text or "://" not in text:
        return text
    return _URL_QUERY.sub(_REPLACEMENT, text)


def redact_urls_bytes(data: bytes) -> bytes:
    """``redact_urls`` for renderers that emit bytes."""
    if b"?" not in data or b"://" not in data:
        return data
    return _URL_QUERY_BYTES.sub(_REPLACEMENT_BYTES, data)


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
