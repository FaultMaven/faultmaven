"""Strip query strings from URLs in every log record.

A URL's query string is a credential channel. The SSO callback carries the
identity provider's authorization code there; the web-search tool reaches
Google CSE as ``?key=<GOOGLE_API_KEY>&q=<the investigation's search text>``.
Both reached the log store, which retains for 14 days, and both were fixed at
the call site — after which two more of the same shape were found on the error
path (``main.py``'s 500 handler, ``web_search.py``'s exception message).

That recurrence is the reason this exists. Roughly 470 first-party call sites
interpolate an exception into a log message, and an HTTP client's exception
message embeds the request URL, so the next one arrives the same way.

**Why a URL query string and not secrets in general.** Checked against the five
leaks actually found: an OAuth authorization code is an opaque string and case
content is free text, so neither is pattern-matchable. A secrets-detection
sweep would have caught one of the five and would have to scan every value of
every record — which in this product means scanning evidence, where IPs and
hostnames are the signal, not noise. Stripping a query string catches three of
the five, costs two substring checks on most values, and cannot blank an
investigation's own data.

**What it does not cover.** It runs where records are rendered by FaultMaven's
handler, so a handler installed outside ``configure_structlog`` would not see
redacted output. That is the trade for covering every logger rather than a
list of named ones: a ``logging.Filter`` protects all handlers but only the
logger it is attached to — not even that logger's children — and the leaks
came from first-party loggers nobody would have thought to list.
"""

import re
from typing import Any

# Anchored on the scheme, so this matches a URL's query string and nothing
# else. An unanchored ``\?[^\s]*`` would eat prose: "did the disk fill?" is a
# perfectly ordinary thing for an investigation to log.
#
# The terminator set stops at whitespace and at the quote characters a URL is
# usually embedded in, so "for url 'https://x/y?k=v'" keeps its closing quote.
_URL_QUERY = re.compile(r"(https?://[^\s\"'<>]*)\?[^\s\"'<>]*")

_REPLACEMENT = r"\1?<redacted>"

# How far into nested containers to look. ``extra={"payload": {...}}`` is one
# level; beyond a few, a log record is carrying a data structure rather than a
# message, and walking it per record would cost more than it protects.
_MAX_DEPTH = 4


def redact_urls(text: str) -> str:
    """Strip the query string from every URL in ``text``.

    Two substring checks run before the regex. Most log values contain no URL,
    and ``in`` on a str is a C-level scan, so the common case never pays for
    the pattern.
    """
    if "?" not in text or "://" not in text:
        return text
    return _URL_QUERY.sub(_REPLACEMENT, text)


def _redacted(value: Any, depth: int) -> Any:
    """Return ``value`` with URLs redacted, or the same object if unchanged.

    Containers are copied rather than mutated. A caller that logs
    ``extra={"config": some_live_dict}`` hands us an object it still owns, and
    a log processor has no business editing it.
    """
    if isinstance(value, str):
        return redact_urls(value)
    if depth >= _MAX_DEPTH:
        return value
    if isinstance(value, dict):
        items = [(k, _redacted(v, depth + 1)) for k, v in value.items()]
        if any(new is not old for (_, new), old in zip(items, value.values())):
            return dict(items)
        return value
    if isinstance(value, (list, tuple)):
        items = [_redacted(v, depth + 1) for v in value]
        if any(new is not old for new, old in zip(items, value)):
            return tuple(items) if isinstance(value, tuple) else items
        return value
    return value


def redact_urls_processor(logger, method_name, event_dict):
    """structlog processor: strip URL query strings from every value.

    Placed in ``ProcessorFormatter.processors`` after ``format_exc_info``, so
    it sees native structlog events and foreign stdlib records alike, with
    ``%``-args already interpolated and exception text already rendered into
    the dict — the three places a URL actually turns up.

    The event dict itself is structlog's, built per call, so assigning into it
    is safe; the values inside may not be, which is why ``_redacted`` copies.

    No ``try``/``except``: the only operations here are ``in`` and ``re.sub``
    on objects already known to be ``str``. There is no failure to swallow, and
    a bare except would turn this into a guard that reports success while
    doing nothing — which is how the last one in this codebase went wrong.
    """
    for key, value in event_dict.items():
        # The str case is inlined rather than delegated. A record has a dozen
        # values and almost all are strings or scalars, so a per-value function
        # call is most of the cost; two ``in`` checks are not.
        if isinstance(value, str):
            if "?" in value and "://" in value:
                event_dict[key] = _URL_QUERY.sub(_REPLACEMENT, value)
        elif isinstance(value, (dict, list, tuple)):
            new = _redacted(value, 0)
            if new is not value:
                event_dict[key] = new
    return event_dict
