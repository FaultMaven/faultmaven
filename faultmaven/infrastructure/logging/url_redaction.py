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

**The one exception: four credential headers** (#2865, rule 2 of #2844's
ruling). ``set-cookie``, ``cookie``, ``authorization`` and
``proxy-authorization`` name a credential by their name alone, so their values
are blanked too, as defence in depth behind rule 1 (no third-party exception
text on a log line). The rule does not measure where a value ends: from the
header's separator it blanks to the end of the enclosing unit (the line, plus
any following lines indented deeper, and YAML's ``- `` items under a header
with nothing after its separator; in a JSON line, the decoded string or key,
or the whole value of a key, a header record or a name pair). The text after
a credential header on the same unit is lost with it, which is the safe side.
"""

import json
import logging
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from faultmaven.infrastructure.security.secret_patterns import ESCAPE_BOUNDARY

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
    """Strip the query string from every URL in ``text``.

    Two substring checks run before the pattern. A line with no URL is the
    common case, and ``in`` on a str is a C-level scan, so it never pays for
    the regex.
    """
    if "?" not in text or "://" not in text:
        return text
    return _URL_QUERY.sub(_redact_query, text)


def redact_urls_bytes(data: bytes) -> bytes:
    """``redact_urls`` for renderers that emit bytes."""
    if b"?" not in data or b"://" not in data:
        return data
    return _URL_QUERY_BYTES.sub(_redact_query_bytes, data)


# A colour code, raw or as a repr or a JSON string spells it.
_SGR = r"(?:\x1b|\\x1b|\\u001b|\\033)\[[0-9;]*+m"
# A credential header: the name, colour codes, any quoting a repr gave it (a
# closing quote may be followed by "]" or ")": ``headers['Authorization'] =``),
# colour codes, then ":" or "=", a tuple's "', '", or a tab. Matched only at a
# candidate the scan found with str.find, never by a scan of the whole line.
_HEADER_NAMES = ("set-cookie", "proxy-authorization", "authorization", "cookie")
_HEAD = re.compile(
    r"(?:set-cookie|proxy-authorization|authorization|cookie)(?![\w-])"
    rf"(?:{_SGR})*+[\\'\"]*+(?:(?<=['\"])[\])])?(?:{_SGR})*+"
    r"(?:[ \t]*+[:=]|(?<=['\"])[ \t]*+,|\t)",
    re.IGNORECASE,
)
# Before a name: no longer word (``x-cookie``), a "_" or "." segment boundary
# (``session_cookie``, ``headers.cookie``: the JSON key rule's segments), the
# shared escape grammar (``\n``, ``%0A``), or a ``\uXXXX`` escape.
_BOUNDARY = re.compile(
    rf"(?<![\w-])|(?<=[_.])|{ESCAPE_BOUNDARY}|(?<=\\u[0-9A-Fa-f]{{4}})"
)
# Or a colour code of any length up to 64 parameter characters, so the search
# back is bounded.
_SGR_BEFORE = re.compile(rf"(?:\x1b|\\x1b|\\u001b|\\033)\[[0-9;]{{0,64}}m\Z")
_TO_LINE_END = re.compile(r"[^\r\n]*+")
_INDENT = re.compile(r"[ \t]*+")
_KEY_SEGMENT = re.compile(r"[._]")
_REDACTED = "<redacted>"
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def _starts_name(text: str, start: int) -> bool:
    """``start`` begins a name, not the inside of a longer word."""
    if _BOUNDARY.match(text, start):
        return True
    return text[start - 1] == "m" and bool(
        _SGR_BEFORE.search(text, max(0, start - 72), start)
    )


def _head_at(text: str, low: str, hit: int, prefix: str) -> Optional[re.Match[str]]:
    """The header whose name ends in the word found at ``hit``, if any."""
    for start in (hit - len(prefix), hit):
        if start >= 0 and low.startswith(prefix, start) and _starts_name(text, start):
            head = _HEAD.match(text, start)
            if head:
                return head
        prefix = ""
    return None


def _unit_end(text: str, head: re.Match[str], pos: int) -> int:
    """The end of the header's line, extended over the lines after it that are
    indented deeper than it (``indent=2`` JSON, pprint, folded headers) and,
    when nothing follows the separator, over YAML's ``- `` items at its own
    indent.

    ``pos`` is 0 or the line end the last unit stopped at, so the search back
    for this line's start never rescans text already passed.
    """
    start = head.start()
    line_start = max(text.rfind("\n", pos, start), text.rfind("\r", pos, start)) + 1
    indent = _INDENT.match(text, line_start).end() - line_start
    end = _TO_LINE_END.match(text, head.end()).end()
    bare = not text[head.end() : end].strip()
    while end < len(text):
        nxt = end + (2 if text.startswith("\r\n", end) else 1)
        body = _INDENT.match(text, nxt).end()
        if body == len(text) or text[body] in "\r\n":
            break
        depth = body - nxt
        item = bare and depth == indent and text.startswith("- ", body)
        if depth <= indent and not item:
            break
        end = _TO_LINE_END.match(text, body).end()
    return end


def _redact_header_text(text: str, low: str) -> str:
    """Blank from each credential header's separator to the end of its unit.

    Candidates come from two ``str.find`` scans, each resumed past the last
    blanked unit, so the cost is linear in the line.
    """
    out = []
    pos = 0
    hits = {"cookie": low.find("cookie"), "authorization": low.find("authorization")}
    while True:
        word = min((w for w in hits if hits[w] >= 0), key=hits.get, default=None)
        if word is None:
            break
        hit = hits[word]
        head = _head_at(text, low, hit, "set-" if word == "cookie" else "proxy-")
        if head is None:
            hits[word] = low.find(word, hit + 1)
            continue
        end = _unit_end(text, head, pos)
        removed = text[head.end() : end]
        out.append(text[pos : head.end()])
        if removed.strip():
            out.append(removed[: len(removed) - len(removed.lstrip(" \t"))] + _REDACTED)
            if "\x1b[" in removed:
                out.append("\x1b[0m")
        else:
            out.append(removed)
        pos = end
        for w in hits:
            if 0 <= hits[w] < pos:
                hits[w] = low.find(w, pos)
    if not out:
        return text
    out.append(text[pos:])
    return "".join(out)


def _lowered(text: str) -> str:
    """A lowercase copy whose indexes are ``text``'s: ``str.lower`` lengthens a
    few characters (``İ``), and then only ASCII is folded."""
    low = text.lower()
    return low if len(low) == len(text) else text.translate(_ASCII_LOWER)


def _redact_text_value(text: str) -> str:
    low = _lowered(text)
    if "cookie" not in low and "authorization" not in low:
        return text
    return _redact_header_text(text, low)


def _is_name(value: Any) -> bool:
    """A header name, or a bytes repr of one (``b'authorization'``: how the
    JSON renderer's fallback writes a raw header pair)."""
    if not isinstance(value, str):
        return False
    name = value.strip()
    if len(name) > 2 and name[0] == "b" and name[1] in "'\"" and name[-1] == name[1]:
        name = name[2:-1]
    return name.lower() in _HEADER_NAMES


def _key_is_name(key: str) -> bool:
    """``cookie``, ``headers.cookie``, ``http.request.header.authorization``."""
    return _KEY_SEGMENT.split(key.strip().lower())[-1].strip() in _HEADER_NAMES


def _unique_keys(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    """A JSON object, refused when a key repeats: the decoder keeps the last,
    and a value the walk never sees would go out unredacted."""
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise ValueError("duplicate key")
    return fields


def _redact_json_value(value: Any) -> Tuple[Any, bool]:
    """``value`` with every credential blanked, and whether anything changed.

    Strings and keys get the text rule; a key naming a header, a header
    record's value (``name`` or ``key``, with ``value``, in any case) and a
    ``[name, value]`` pair's value are replaced whole.
    """
    if isinstance(value, str):
        redacted = _redact_text_value(value)
        return redacted, redacted != value
    if isinstance(value, list):
        items, changed = [], False
        for item in value:
            if isinstance(item, list) and len(item) == 2 and _is_name(item[0]):
                item_changed = item[1] != _REDACTED
                item = [item[0], _REDACTED]
            else:
                item, item_changed = _redact_json_value(item)
            items.append(item)
            changed = changed or item_changed
        return items, changed
    if isinstance(value, dict):
        record = any(
            key.lower() in ("name", "key") and _is_name(item)
            for key, item in value.items()
        )
        fields, changed = {}, False
        for key, item in value.items():
            if _key_is_name(key) or (record and key.lower() == "value"):
                item_changed = item != _REDACTED
                item = _REDACTED
            else:
                item, item_changed = _redact_json_value(item)
            redacted_key = _redact_text_value(key)
            fields[redacted_key] = item
            changed = changed or item_changed or redacted_key != key
        return fields, changed
    return value, False


def _redact_headers(text: str, ensure_ascii: bool) -> str:
    low = _lowered(text)
    if "cookie" not in low and "authorization" not in low:
        return text
    if text.startswith('{"'):
        try:
            parsed = json.loads(text, object_pairs_hook=_unique_keys)
            redacted, changed = _redact_json_value(parsed)
            return json.dumps(redacted, ensure_ascii=ensure_ascii) if changed else text
        except (ValueError, RecursionError):
            pass
    return _redact_header_text(text, low)


def redact_credential_headers(text: str) -> str:
    """Blank the values of the credential headers named in the module docstring.

    A line naming neither ``cookie`` nor ``authorization`` is returned as is,
    after one lowercase copy and two finds. A JSON object line is redacted on
    its decoded value and re-serialized only when something changed, so it
    stays valid JSON; one the decoder refuses (not JSON, nested too deep, or a
    key repeated) is redacted as text.
    """
    return _redact_headers(text, ensure_ascii=True)


def redact_log_line(text: str) -> str:
    """Every redaction rule for a rendered log line: URL queries, then headers."""
    return redact_credential_headers(redact_urls(text))


def redact_log_line_bytes(data: bytes) -> bytes:
    """``redact_log_line`` for renderers that emit bytes. A changed JSON line
    keeps the input's encoding: unchanged characters keep their bytes."""
    data = redact_urls_bytes(data)
    low = data.lower()
    if b"cookie" not in low and b"authorization" not in low:
        return data
    text = data.decode("utf-8", "surrogateescape")
    redacted = _redact_headers(text, ensure_ascii=False)
    return redacted.encode("utf-8", "surrogateescape")


def redacting_renderer(renderer: Callable) -> Callable:
    """Wrap a structlog renderer so its output passes `redact_log_line`.

    Applied to whichever renderer ``configure_structlog`` selects, so the rule
    holds for JSON and console output alike, and for foreign stdlib records as
    well as native structlog events — they converge on the same formatter.
    """

    def render(logger, method_name, event_dict):
        rendered = renderer(logger, method_name, event_dict)
        if isinstance(rendered, str):
            return redact_log_line(rendered)
        if isinstance(rendered, bytes):
            return redact_log_line_bytes(rendered)
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
        return redact_log_line(super().format(record))
