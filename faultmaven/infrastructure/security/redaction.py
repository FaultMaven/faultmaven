"""redaction.py

Purpose: Sanitize sensitive information

Requirements:
--------------------------------------------------------------------------------
• Implement DataSanitizer class
• Use K8s Presidio microservice for PII detection
• Apply custom regex patterns for secrets

Key Components:
--------------------------------------------------------------------------------
  class DataSanitizer: sanitize(text: str) -> str
  SECRET_PATTERNS: the one secret-detection pattern set (find_redaction_spans)

Technology Stack:
--------------------------------------------------------------------------------
K8s Presidio microservice, HTTP requests, regex

Core Design Principles:
--------------------------------------------------------------------------------
• Privacy-First: Sanitize all external-bound data
• Resilience: Implement retries and fallbacks
• Extensibility: Use interfaces for pluggable components
• Observability: Add tracing spans for key operations
"""

import asyncio
import hashlib
import hmac
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import requests

from faultmaven.infrastructure.base_client import BaseExternalClient
from faultmaven.infrastructure.security.pseudonym_key import resolve_pseudonym_key
from faultmaven.models.interfaces import ISanitizer

# =============================================================================
# The regex half of detection (#1980)
# =============================================================================
#
# ONE secret-detection set, `SECRET_PATTERNS`, and every redaction path detects
# secrets with it: the sanitizer's regex half (case redaction, the router's
# pass, case titles) and the knowledge module's document preprocessor. The
# sanitizer adds `NETWORK_ADDRESS_PATTERNS` (internal IPs, MACs), which are
# identifiers, not secrets: the preprocessor's unconditional scrub leaves them
# to the model-boundary redaction, which `SANITIZE_PII` governs (#1901).
#
# Detection runs on the RAW text, once per pattern, and the matches are merged
# into non-overlapping spans before anything is substituted
# (`find_redaction_spans`). So what is detected never depends on what a caller
# substitutes, and no pattern ever reads another's placeholder.
#
# A pattern's entity type names its placeholder (`<PASSWORD_digest>`) and the
# knowledge marker (`[REDACTED:password]`). The match is the span replaced,
# except where a pattern names a `secret` group, which then is the span.


@dataclass(frozen=True)
class RedactionPattern:
    """One detector: the entity type its matches are recorded under, and the
    compiled expression. ``secret``, when the expression names that group, is
    the span replaced; otherwise the whole match is.

    ``triggers`` are lower-case literals, at least one of which every match
    contains (case-insensitively); empty means none is known. ``keyed`` says
    more: no match crosses a newline, and every match starts at the start of
    its line or where a trigger starts. Both only let ``find_redaction_spans``
    skip positions where no match can start.
    """

    entity_type: str
    regex: "re.Pattern[str]"
    triggers: Tuple[str, ...] = ()
    keyed: bool = False

    @property
    def span_group(self) -> int | str:
        """The group whose span is replaced: ``secret`` when named, else the
        whole match."""
        return "secret" if "secret" in self.regex.groupindex else 0


@dataclass(frozen=True)
class RedactionSpan:
    """A detected span of the raw text, ``text[start:end]``."""

    start: int
    end: int
    entity_type: str


# --- value grammar for `key <separator> value` secrets -----------------------

#: A value that REFERS to a secret instead of holding one: a braced shell
#: variable, a command substitution, a template expression, a documentation
#: placeholder (`<your-password>`), or a placeholder this sanitizer wrote.
#: A bare `$NAME` is not on the list: a real secret can start with `$`.
_REFERENCE = r"""(?:\$\{[A-Za-z_][A-Za-z0-9_]*\}|\$\(|\{\{|<[A-Za-z][A-Za-z0-9_ -]*>)"""
_NOT_A_REFERENCE = rf"""(?!["']?{_REFERENCE})"""
_QUOTED_VALUE = r"""(?:"[^"\r\n]*"|'[^'\r\n]*')"""
#: Any value: quoted (spaces allowed inside), else the run up to whitespace.
_ANY_VALUE = rf"{_NOT_A_REFERENCE}(?:{_QUOTED_VALUE}|\S+)"
#: A word as prose writes one: lower case, Capitalised, or a short ALL-CAPS
#: word, joined by `-`, `_`, `'` or `[` (`read-only`, `invalid_grant`,
#: `Optional[str]`), or short ones joined by `/` (`n/a`, `and/or`, `TCP/IP`)
#: — a longer `/` run is as likely a password (`w/ddrf/afdff`).
_WORD = r"(?:[a-z]+|[A-Z][a-z]+|[A-Z]{1,8})"
_SHORT = r"(?:[a-z]{1,3}|[A-Z][a-z]{0,2}|[A-Z]{2,3})"
_PLAIN_WORD_RUN = rf"{_WORD}(?:[-_'\[]{_WORD})*"
_WORD_RUN = rf"(?:{_PLAIN_WORD_RUN}|{_SHORT}(?:/{_SHORT}){{1,2}})"
#: A word run in optional parentheses with optional trailing punctuation, or
#: a number of at most three digits (a count, a status code).
_PROSE_VALUE = rf"\(?(?:{_WORD_RUN}|[0-9]{{1,3}})[.,;:!?)\]]*(?=\s|\Z)"
#: A value after `key: ` in running text. That spelling is also prose
#: ("password: expired for the svc account", MySQL's "(using password: YES)"),
#: so a value shaped like a word is not read as a secret; anything else —
#: a digit, a symbol, mixed case inside the run, quotes — is.
_MIDLINE_VALUE = rf"(?-i:(?!{_PROSE_VALUE})){_ANY_VALUE}"
#: A value that is the rest of its line (an optional `#` comment after it) —
#: unless it is a word closing a sentence (`api_key: missing.`).
_LINE_END = r"[ \t]*(?:#[^\r\n]*)?(?:\r?\n|\Z)"
_LINE_VALUE = (
    rf"(?-i:(?!{_PLAIN_WORD_RUN}[.!?]+[ \t]*(?:\r?\n|\Z)))"
    rf"{_NOT_A_REFERENCE}\S+(?={_LINE_END})"
)
#: A compound key's literal that is a count or a flag, not a secret
#: (`failed_password=520`, `reset_password=true`) — the config extractor's
#: `_NON_SECRET_VALUE_RE` precedent.
_NOT_A_COUNT_OR_FLAG = (
    r"""(?!["']?(?:true|false|yes|no|on|off|none|null|nil|\d{1,5})["']?"""
    r"""[,;)\]}]*(?:\s|\Z))"""
)
#: `=` or `=>`. A spaced `==` is a comparison (`if password == "":`), not an
#: assignment; a tight `password==x` is a value that starts with `=`.
_ASSIGN = r"(?:=>?|[ \t]+=>?(?!=))[ \t]*"
_QUOTED_KEY_SEPARATOR = r"""["'][ \t]*[:=][ \t]*"""


def _keyed_secret(keywords: str, cased: str, value_guard: str = "") -> str:
    """``key <separator> value``, for one family of secret-naming keys.

    ``keywords`` match in any case; ``cased`` are the spellings that mark a
    keyword inside a longer name with no separator (``clientSecret``,
    ``PGPASSWORD``). The match runs from the keyword to the end of the value
    (the key is part of what is replaced, as it always was). The spellings:

    1. ``key: value`` as a whole line (YAML, ``.env``, a header block), under
       optional indentation, a list dash and a dotted key path: any value.
    2. A bare key with a separator that prose does not use — ``=``, ``=>``,
       ``key:value``, ``key : value``, or a quoted key (``"key": value``): any
       value.
    3. A bare key as ``key: value`` in running text, or as a command-line
       flag (``--password value``): any value not shaped like a word (see
       ``_MIDLINE_VALUE``).
    4. A compound key — ``db_password``, ``clientSecret``, ``PGPASSWORD`` —
       with ``=``/``=>`` or as a quoted key, and a value that is not a count
       or a flag. A compound ``key: value`` is not read as a secret: that is
       how event counts print (``failed_password: 520``, #654).

    "Bare" means not preceded by a letter, digit or underscore, so
    ``X-Auth-Token`` and ``spring.datasource.password`` are bare keys and
    ``failed_password`` is compound.
    """
    key = rf"(?:{keywords})"
    bare = rf"(?<![A-Za-z0-9_]){key}"
    compound = rf"(?:(?<=[A-Za-z0-9]_){key}|(?<=[A-Za-z0-9])(?-i:{cased}))"
    return (
        rf"(?:^|(?<=\n))[ \t]*(?:-[ \t]+)?(?:[\w-]+\.)*"
        rf"(?P<secret>{key}[ \t]*:[ \t]+{value_guard}{_LINE_VALUE})"
        rf"|{bare}(?:{_ASSIGN}|:(?=\S)|[ \t]+:[ \t]*|{_QUOTED_KEY_SEPARATOR})"
        rf"{value_guard}{_ANY_VALUE}"
        rf"|(?:{bare}:|(?<=-){key})[ \t]+{value_guard}{_MIDLINE_VALUE}"
        rf"|{compound}(?:{_ASSIGN}|{_QUOTED_KEY_SEPARATOR})"
        rf"{_NOT_A_COUNT_OR_FLAG}{value_guard}{_ANY_VALUE}"
    )


# --- private keys ------------------------------------------------------------

_PEM_BEGIN = r"-----BEGIN[A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----"
#: A key block with no END line (a truncated paste): the BEGIN line and every
#: line after it that is still key material — a base64 line, a PEM header
#: (`Proc-Type:`/`DEK-Info:`), a blank line, the same run flattened onto one
#: line, or JSON-escaped `\n`s. It stops at the first line that is not, so the
#: log lines after a truncated key survive; bounding it by end of text would
#: redact every one of them.
_PEM_TRUNCATED_BODY = (
    r"(?:[ \t]+[A-Za-z0-9+/=]{16,}"
    r"|(?:\\r)?\\n[A-Za-z0-9+/=]*"
    r"|\r?\n[ \t]*(?:[A-Za-z0-9+/=]+|(?:Proc-Type|DEK-Info|Comment):[^\r\n]*)?"
    r"[ \t]*(?=\r?\n|\Z))*"
)

# --- the set -----------------------------------------------------------------

_I = re.IGNORECASE
_CREDENTIAL_SCHEMES = r"(?:postgres(?:ql)?|mysql|mariadb|mongodb|redis|rediss|amqps?)"


def _pattern(
    entity_type: str,
    expression: str,
    flags: int,
    triggers: Tuple[str, ...],
    keyed: bool = False,
) -> RedactionPattern:
    return RedactionPattern(entity_type, re.compile(expression, flags), triggers, keyed)


def _keyed(
    entity_type: str,
    keywords: str,
    cased: str,
    triggers: Tuple[str, ...],
    value_guard: str = "",
) -> RedactionPattern:
    """A ``_keyed_secret`` family, case-insensitive. ``triggers`` must each
    be the start of a keyword (every keyword and cased spelling starts with
    one), so a match starts at a trigger or, as a whole-line spelling, at the
    start of its line."""
    return _pattern(
        entity_type,
        _keyed_secret(keywords, cased, value_guard),
        _I,
        triggers,
        keyed=True,
    )


#: The secret-detection pattern set. Order is the tie-break when two patterns
#: claim the same span (see ``find_redaction_spans``), so the more specific
#: come first.
SECRET_PATTERNS: Tuple[RedactionPattern, ...] = (
    # Private keys: a whole block, else a truncated one (no END line).
    _pattern(
        "PRIVATE_KEY",
        _PEM_BEGIN + r".*?-----END[A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----",
        _I | re.DOTALL,
        ("private key",),
    ),
    _pattern("PRIVATE_KEY", _PEM_BEGIN + _PEM_TRUNCATED_BODY, _I, ("private key",)),
    # Connection URLs with a user (and password) before the host. The user
    # part never crosses whitespace — the former `[^@]+` ran on to the next
    # `@` anywhere later in the text (an email, an @mention, `user@service`)
    # and redacted everything between. Within the URL it runs to the LAST
    # `@`, so a password holding a raw `@` and `/` (`p@ss/w0rd`) is not cut
    # short with its tail in clear.
    _pattern(
        "DATABASE_URL",
        rf"(?<![A-Za-z0-9]){_CREDENTIAL_SCHEMES}(?:\+[A-Za-z0-9]+)?://\S+@[^/\s]+",
        _I,
        ("://",),
    ),
    # Any other URL that carries `user:password@`. (A former "docker registry
    # credentials" pattern `email@host.tld:token` was removed in #654 — it
    # false-matched any `user@host.tld:port` in logs/connection strings. This
    # one needs the `scheme://` and the `:password`.)
    _pattern(
        "CREDENTIAL_URL",
        r"(?<![A-Za-z0-9+.-])[A-Za-z][A-Za-z0-9+.-]*://"
        r"[^\s:/?#@]+:[^\s/?#@]+@[^\s/?#]+",
        _I,
        ("://",),
    ),
    # Provider tokens with a fixed prefix.
    _pattern(
        "GITHUB_TOKEN",
        r"(?<![A-Za-z0-9_])"
        r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})",
        0,
        ("ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_"),
    ),
    _pattern(
        "SLACK_TOKEN",
        r"(?<![A-Za-z0-9_-])"
        r"(?:xox[a-z]-[A-Za-z0-9-]{10,}|xapp-[0-9]-[A-Za-z0-9-]{10,})",
        0,
        ("xox", "xapp-"),
    ),
    # OpenAI-style keys, `sk-proj-…`/`sk-ant-…` included, and `pk-` keys.
    # Not inside a word: `task-0123…abcdef` is not a key.
    _pattern(
        "API_KEY",
        r"(?<![A-Za-z0-9])sk-(?:[A-Za-z0-9]{32,}"
        r"|(?=[A-Za-z0-9_-]*[0-9])[A-Za-z0-9_-]{20,})",
        _I,
        ("sk-",),
    ),
    _pattern("API_KEY", r"(?<![A-Za-z0-9])pk-[A-Za-z0-9]{32,}", _I, ("pk-",)),
    # AWS access key ids (long-term AKIA, temporary ASIA). Case-sensitive: a
    # real id is upper case, and "asia" + 16 letters is prose.
    _pattern(
        "AWS_ACCESS_KEY",
        r"(?<![A-Z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}",
        0,
        ("akia", "asia"),
    ),
    # AWS secret access keys — CONTEXT-ANCHORED. An AWS secret is 40 base64
    # chars, indistinguishable from any hash/SHA/base64 blob, so it MUST be
    # gated on an adjacent `aws...secret...key =` context. A bare
    # `[0-9a-zA-Z/+]{40}` pattern here previously corrupted opaque data (e.g.
    # Gemini thought_signature base64) — see #654.
    _pattern(
        "AWS_SECRET_KEY",
        r"aws[_-]?secret[_-]?access[_-]?key['\"]?\s*[=:]\s*['\"]?[0-9a-zA-Z/+]{40}",
        _I,
        ("secret",),
    ),
    # JWTs. (The former `[A-Za-z0-9-_=]` was a `9-_` RANGE, which also took
    # `:;<=>?@[\]^`.)
    _pattern(
        "JWT_TOKEN",
        r"(?<![A-Za-z0-9])eyJ[A-Za-z0-9_=-]+\.[A-Za-z0-9_=-]+\.?[A-Za-z0-9_.+/=-]*",
        _I,
        ("eyj",),
    ),
    # `Bearer <token>`: the token is the span, so a JWT keeps its own type and
    # placeholder here as anywhere else. Eight or more token characters with
    # a digit, an inner `.`/`_`/`~`/`+`/`/`, or a capital after a lower-case
    # letter — never "the bearer of bad news", "Bearer authentication", a
    # `$TOKEN` variable or a `<token>` placeholder.
    _pattern(
        "BEARER_TOKEN",
        r"(?<![A-Za-z0-9_])bearer[ \t]+(?P<secret>"
        r"(?=[A-Za-z0-9._~+/-]*"
        r"(?:[0-9]|[._~+/](?=[A-Za-z0-9])|(?-i:[a-z][A-Z])))"
        r"[A-Za-z0-9._~+/-]{8,}=*)",
        _I,
        ("bearer",),
    ),
    # `Authorization: Basic <base64 user:password>`: the credential is the
    # span.
    _pattern(
        "BASIC_AUTH",
        r"(?<![A-Za-z0-9_-])(?:proxy-)?authorization[\"']?[ \t]*[:=][ \t]*"
        r"[\"']?basic[ \t]+(?P<secret>"
        r"(?=[A-Za-z0-9+/]*(?:[0-9+/]|(?-i:[a-z][A-Z])))"
        r"[A-Za-z0-9+/]{8,}={0,2})",
        _I,
        ("basic",),
    ),
    _pattern("K8S_SECRET", r"k8s[_-]?secret[_-]?[0-9a-fA-F]{32,}", _I, ("k8s",)),
    # `apikey<hex>` with no separator, then the keyed spellings.
    _pattern("API_KEY", r"api[_-]?key[_-]?[0-9a-fA-F]{32,}", _I, ("api",)),
    _keyed("API_KEY", r"api[_-]?key", r"Api[_-]?Key|API[_-]?K(?:ey|EY)", ("api",)),
    _keyed(
        "PASSWORD",
        r"password|passwd|passphrase",
        r"Pass(?:word|wd|phrase)|PASS(?:WORD|WD|PHRASE)",
        ("pass",),
    ),
    # `pwd` is also the working directory (`PWD=/home/app` in every env
    # dump): a path is not its password.
    _keyed("PASSWORD", r"pwd", r"Pwd|PWD", ("pwd",), value_guard=r"""(?!["']?[/~])"""),
    _keyed(
        "SECRET",
        r"secret(?:[_-]?key)?",
        r"Secret(?:Key)?|SECRET(?:KEY)?",
        ("secret",),
    ),
    _keyed("TOKEN", r"token", r"Token|TOKEN", ("token",)),
)

#: Network identifiers the sanitizer also redacts: internal addresses and
#: hardware addresses. Not secrets — see the section comment.
NETWORK_ADDRESS_PATTERNS: Tuple[RedactionPattern, ...] = (
    # IP addresses (internal ranges). Two things this spelling fixes.
    #
    # Each alternative must leave EXACTLY two octets for the shared tail:
    # 10/8 has three octets after its prefix, 172.16/12 and 192.168/16 have
    # two. The former `(10\.|...)` spelling gave 10/8 the same two-octet tail
    # as the others, so `10.1.2.3` matched only `10.1.2` — the final octet
    # survived redaction in the clear, and every host on a /24 shared one
    # placeholder in the registry.
    #
    # The bounds are lookarounds rather than `\b`, because `\b` only holds
    # against a non-word character. Fixing the octet count while keeping `\b`
    # would have made an abutting address WORSE: `10.1.2.3_backup` (a
    # filename, a log field) matches nothing at all under a `\b`-bounded
    # four-octet pattern and leaks in full, where the old three-octet pattern
    # at least caught `10.1.2`.
    #
    # They exclude an adjacent DIGIT, and separately a dot that is part of a
    # longer dotted run — not any adjacent dot. A blanket
    # `(?<![\d.])`/`(?![\d.])` looks equivalent and is not: it refuses to
    # match an address ending a sentence, so `refused from 192.168.1.10.`
    # leaked the whole address that the pattern it replaced had redacted.
    # `(?!\d)(?!\.\d)` accepts the full stop and still refuses the
    # continuation, so `10.1.2.34567`, `10.1.2.3.4`, `110.1.2.3` and
    # `1.10.1.2.3` stay unmatched.
    _pattern(
        "IP_ADDRESS",
        r"(?<!\d)(?<!\d\.)"
        r"(?:10\.\d{1,3}|172\.(?:1[6-9]|2[0-9]|3[01])|192\.168)"
        r"\.\d{1,3}\.\d{1,3}(?!\d)(?!\.\d)",
        _I,
        ("10.", "172.", "192.168"),
    ),
    # MAC addresses — except six dash-joined pairs of decimal digits, which
    # are a timestamp or a release id (`24-01-02-03-04-05`), not a MAC.
    _pattern(
        "MAC_ADDRESS",
        r"[0-9A-F]{2}(?:[:-][0-9A-F]{2}){5}"
        r"(?<![0-9]{2}-[0-9]{2}-[0-9]{2}-[0-9]{2}-[0-9]{2}-[0-9]{2})",
        _I,
        (),
    ),
)

#: The sanitizer's regex half: every secret, then the network identifiers.
SANITIZER_PATTERNS: Tuple[RedactionPattern, ...] = (
    SECRET_PATTERNS + NETWORK_ADDRESS_PATTERNS
)

#: The non-ASCII letters `re.IGNORECASE` folds onto an ASCII one that
#: `str.lower()` leaves alone: dotless i and long s (sre's `_equivalences`).
#: Mapped back so a trigger is found wherever its pattern can match.
_IGNORECASE_ASCII_FOLDS = {0x131: "i", 0x17F: "s"}


def _trigger_view(text: str) -> Optional[str]:
    """``text`` lower-cased for the trigger checks, index for index — or
    None when lower-casing changes its length (``İ``), when no index maps
    and every pattern runs on the whole text."""
    view = text.lower().translate(_IGNORECASE_ASCII_FOLDS)
    return view if len(view) == len(text) else None


def _keyed_starts(text: str, view: str, triggers: Tuple[str, ...]) -> List[int]:
    """Where a keyed pattern's match can start: each trigger, and the start
    of each line that holds one. Sorted."""
    starts = set()
    for trigger in triggers:
        hit = view.find(trigger)
        while hit != -1:
            starts.add(hit)
            starts.add(text.rfind("\n", 0, hit) + 1)
            hit = view.find(trigger, hit + 1)
    return sorted(starts)


def _keyed_matches(
    pattern: RedactionPattern, text: str, view: str
) -> List["re.Match[str]"]:
    """``pattern.regex.finditer(text)`` for a keyed pattern, trying only the
    positions where its match can start: leftmost first, and the next try
    from where the last match ended, as ``finditer`` does."""
    matches = []
    resume = 0
    for start in _keyed_starts(text, view, pattern.triggers):
        if start >= resume:
            match = pattern.regex.match(text, start)
            if match:
                matches.append(match)
                resume = match.end()
    return matches


def find_redaction_spans(
    text: str, patterns: Sequence[RedactionPattern]
) -> List[RedactionSpan]:
    """Every span of ``text`` that ``patterns`` detect, in order, never
    overlapping.

    Each pattern runs on the raw text. Matches are taken leftmost first, the
    longer first at one start, the earlier pattern first at one span; a match
    that overlaps the span before it extends that span instead of starting
    its own, so no character any pattern detected is left in clear and no
    placeholder ever wraps another.

    A pattern whose triggers are all absent cannot match, and is skipped; a
    keyed one is tried only where a match can start (``match`` at a position
    of the whole text, so its lookarounds read what a scan's would). Neither
    changes what is found — only what it costs.
    """
    if not text:
        return []
    view = _trigger_view(text)
    candidates = []
    for priority, pattern in enumerate(patterns):
        if view is None or not pattern.triggers:
            matches: Iterable["re.Match[str]"] = pattern.regex.finditer(text)
        elif not any(trigger in view for trigger in pattern.triggers):
            continue
        elif pattern.keyed:
            matches = _keyed_matches(pattern, text, view)
        else:
            matches = pattern.regex.finditer(text)
        for match in matches:
            start, end = match.span(pattern.span_group)
            if start == -1:  # the span group did not take part
                start, end = match.span()
            if end > start:
                candidates.append((start, -end, priority, pattern.entity_type))
    candidates.sort()
    spans: List[RedactionSpan] = []
    for start, negative_end, _priority, entity_type in candidates:
        end = -negative_end
        if spans and start < spans[-1].end:
            if end > spans[-1].end:
                last = spans[-1]
                spans[-1] = RedactionSpan(last.start, end, last.entity_type)
            continue
        spans.append(RedactionSpan(start, end, entity_type))
    return spans


def redact_spans(
    text: str,
    patterns: Sequence[RedactionPattern],
    replace: Callable[[RedactionSpan, str], str],
) -> str:
    """``text`` with each detected span replaced by ``replace(span, value)``.

    Substitution is by span, on the raw text: what one path writes in place
    of a secret (a keyed placeholder, a knowledge marker) cannot change what
    is detected.
    """
    spans = find_redaction_spans(text, patterns)
    if not spans:
        return text
    pieces = []
    position = 0
    for span in spans:
        pieces.append(text[position : span.start])
        pieces.append(replace(span, text[span.start : span.end]))
        position = span.end
    pieces.append(text[position:])
    return "".join(pieces)


class RedactionUnavailableError(RuntimeError):
    """Raised when PII redaction is required (``sanitize_pii=True``) but the
    Presidio analyzer is unavailable and the posture is fail-closed
    (``PROTECTION_FAIL_OPEN=false``). Refusing to pass un-analyzed text to an
    external provider is the privacy-preserving default (#654).
    """


class DataSanitizer(BaseExternalClient, ISanitizer):
    """Sanitizes sensitive information from text data

    Implements ISanitizer interface for privacy-first data processing.
    Supports both Presidio-based PII detection and custom regex patterns.
    """

    def __init__(self, settings: Optional["FaultMavenSettings"] = None):  # noqa: F821
        """Initialize the DataSanitizer with optional unified settings.

        If settings are provided and indicate skip_service_checks, external
        connectivity tests are skipped to avoid network calls during tests.
        """
        # Initialize BaseExternalClient
        super().__init__(
            client_name="data_sanitizer",
            service_name="Presidio_Services",
            enable_circuit_breaker=True,
            circuit_breaker_threshold=3,  # Lower threshold for privacy-critical service
            circuit_breaker_timeout=30,  # Shorter timeout for privacy service recovery
        )

        # Resolve settings — use injected settings or fall back to global
        if settings is None:
            from faultmaven.config.settings import get_settings

            settings = get_settings()
        self._settings = settings

        # Resolved on first redaction, not here — see the _pseudonym_key property.
        self.__key: Optional[bytes] = None

        # Presidio service endpoints
        analyzer_url = settings.protection.presidio_analyzer_url
        anonymizer_url = settings.protection.presidio_anonymizer_url

        self.analyzer_url = analyzer_url
        self.anonymizer_url = anonymizer_url

        # HTTP client configuration
        self.request_timeout = 10.0  # seconds
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Content-Type": "application/json",
                "User-Agent": "FaultMaven-DataSanitizer/1.0",
            }
        )

        # Determine whether to skip external checks
        # Skip when: tests (skip_service_checks), or protection is disabled (local deployment)
        skip_checks = getattr(settings.server, "skip_service_checks", False)
        protection_disabled = (
            not settings.protection.protection_enabled
            and not settings.protection.sanitize_pii
        )

        # Whether Presidio was actually REACHED FOR, recorded here rather than
        # re-derived by consumers. `analyzer_available = False` collapses two
        # unrelated states — "we never tried, by configuration" and "we tried
        # and it did not answer" — and only the second is a fault. The health
        # probe needs to tell them apart or it reports every standalone
        # deployment as degraded forever, which is how a signal gets ignored.
        self.presidio_probed = not (skip_checks or protection_disabled)

        # Test service connectivity only when protection is enabled
        if skip_checks or protection_disabled:
            self.analyzer_available = False
            self.anonymizer_available = False
            if skip_checks:
                self.logger.info(
                    "Skipping Presidio health checks (SKIP_SERVICE_CHECKS=True)"
                )
            else:
                self.logger.info(
                    "Skipping Presidio health checks (protection disabled)"
                )
        else:
            self.analyzer_available = self._test_service_health(self.analyzer_url)
            self.anonymizer_available = self._test_service_health(self.anonymizer_url)
            if self.analyzer_available and self.anonymizer_available:
                self.logger.info("✅ Connected to K8s Presidio services")
            else:
                self.logger.warning(
                    f"⚠️ Limited Presidio connectivity - Analyzer: {self.analyzer_available}, Anonymizer: {self.anonymizer_available}"
                )
                self.logger.info("📝 Falling back to regex-only sanitization")

        # Presidio detection config from settings
        self._presidio_entities = list(settings.protection.entities_to_protect)
        self._presidio_score_threshold = settings.protection.min_score_threshold

        # The regex half of detection: the module's one pattern set (#1980),
        # held here so a test can stand in a broken one.
        self.regex_patterns: Sequence[RedactionPattern] = SANITIZER_PATTERNS

        # Dict keys whose values are opaque model/provider artifacts (base64
        # reasoning signatures, provider round-trip metadata). They carry no
        # user data and MUST pass through verbatim — redacting them corrupts the
        # bytes and breaks the provider's decode on the next call (#654).
        self._opaque_artifact_keys = frozenset(
            {
                "provider_metadata",
                "assistant_parts",
                "thoughtsignature",
                "thought_signature",
            }
        )

    def sanitize(self, data: Any) -> Any:
        """
        ISanitizer interface implementation

        Sanitize sensitive information from data of various types.

        Args:
            data: Data to sanitize (can be string, dict, list, etc.)

        Returns:
            Sanitized data of the same type
        """
        if data is None:
            return data

        if isinstance(data, str):
            return self._sanitize_text(data)
        elif isinstance(data, dict):
            return self._sanitize_dict(data)
        elif isinstance(data, list):
            return self._sanitize_list(data)
        elif isinstance(data, (int, float, bool)):
            # Primitive types that don't contain sensitive data
            return data
        else:
            # For other types, convert to string, sanitize, and return as string
            return self._sanitize_text(str(data))

    async def asanitize(self, data: Any) -> Any:
        """Async boundary for :meth:`sanitize`.

        Runs the CPU-bound regex passes AND the blocking Presidio HTTP round-trip
        on a worker thread via ``asyncio.to_thread``, so the event loop is NEVER
        blocked. Any ``async`` caller MUST use this instead of ``sanitize()`` —
        a synchronous ``sanitize()`` on the loop stalls it and, on k8s, starves
        the liveness probe (the same class as inline embedding, #654).
        """
        if data is None:
            return data
        return await asyncio.to_thread(self.sanitize, data)

    def _fail_closed_active(self) -> bool:
        """True when PII redaction is required but must not silently degrade.

        Redaction is only enforced when the operator turned it on
        (``sanitize_pii``); when on, ``fail_open=False`` (the default) means a
        Presidio outage must raise rather than pass un-analyzed text through.
        Standalone / log-only sanitize calls (``sanitize_pii=False``) are never
        affected.
        """
        prot = self._settings.protection
        return bool(prot.sanitize_pii) and not bool(prot.fail_open)

    @property
    def _pseudonym_key(self) -> bytes:
        """The deployment's pseudonym key, resolved once per sanitizer.

        Resolved lazily rather than in ``__init__`` so that constructing a
        sanitizer — which happens all over the codebase and in every test —
        never touches the filesystem or fails; only actually redacting
        something requires a key.
        """
        if self.__key is None:
            self.__key = resolve_pseudonym_key(self._settings)
        return self.__key

    def _hashed_placeholder(self, entity_type: str, value: str) -> str:
        """Stable ``<TYPE_digest>`` placeholder for a value.

        ``HMAC-SHA256(deployment key, value)`` truncated to 64 bits. Keyed, not
        bare: an unkeyed digest — the ``md5(value)[:12]`` this replaced — is a
        commitment to the plaintext, and the spaces redaction protects run
        10^7–10^10 candidates (internal IPs, SSNs, phone numbers), all
        trivially enumerated offline by anyone holding the redacted text
        (#971). With the key withheld there is nothing to enumerate against.

        Still *deterministic*, deliberately. The same value must land on the
        same placeholder across separately-sanitized artifacts — evidence files
        are redacted one at a time and persisted, the KB at ingestion, prompts
        per turn — or one host reads as two and the investigation loses the
        co-reference it exists to find. That is why this is a keyed hash and
        not a random token: a random token satisfies the secrecy half and
        destroys this half.

        Truncated to 64 bits, wider than the 48 this replaced. A collision
        maps two distinct values onto one placeholder, and the reverse map then
        resolves one of them to the WRONG original — a wrong value handed back
        to the user, not merely a lost one. The redaction registry is
        unbounded (``entity_registry_cap_per_type`` caps a different
        subsystem's extractor, not this), so the size to plan for is however
        many distinct entities a large log has: at 48 bits the birthday bound
        over 10^5 entries is ~2e-5, at 64 bits it is ~3e-10.
        """
        digest = hmac.new(
            self._pseudonym_key, value.encode("utf-8"), hashlib.sha256
        ).hexdigest()[:16]
        return f"<{entity_type}_{digest}>"

    def _sanitize_text(self, text: str) -> str:
        """Sanitize text with a fresh, call-local entity registry.

        Thin wrapper over :meth:`sanitize_text_with_registry` — the single
        detection path (regex patterns + Presidio) — with a throwaway registry.
        """
        return self.sanitize_text_with_registry(text, {})

    def apply_regex_redaction(
        self,
        text: str,
        entity_registry: Dict[str, Dict[str, str]],
    ) -> str:
        """The regex half of detection, on its own — never touches Presidio.

        Split out of :meth:`sanitize_text_with_registry` so that a caller
        needing redaction it can *prove* stays in-process has one to call. The
        health probe is that caller: reaching Presidio from a probe means an
        HTTP round trip every ten seconds through ``call_external_sync``,
        which records on the circuit breaker that gates real redaction — probe
        successes reset the failure count so three consecutive real failures
        are never observed, and a hung analyzer opens the breaker with no user
        traffic at all. Under a fail-closed posture that turns every turn into
        a ``RedactionUnavailableError``.

        A structural guarantee, not a convention: there is no call into
        ``_apply_presidio`` from here, so no configuration can route this
        through the network.
        """
        if not text or not isinstance(text, str):
            return text

        def _get_placeholder(span: RedactionSpan, value: str) -> str:
            type_map = entity_registry.setdefault(span.entity_type, {})
            if value not in type_map:
                type_map[value] = self._hashed_placeholder(span.entity_type, value)
            return type_map[value]

        return redact_spans(text, self.regex_patterns, _get_placeholder)

    def sanitize_text_with_registry(
        self,
        text: str,
        entity_registry: Dict[str, Dict[str, str]],
    ) -> str:
        """Sanitize text using an externally-provided entity registry.

        THE single text-detection path: regex patterns first, then the Presidio
        analyzer. Each unique sensitive value maps to a stable ``<TYPE_hash>``
        placeholder via the shared registry, so the same value is consistent
        across calls (the registry is mutated in place).

        Raises:
            RedactionUnavailableError: when redaction is required and the
                Presidio analyzer is unavailable under a fail-closed posture.
        """
        if not text or not isinstance(text, str):
            return text

        # Regex patterns are local and cannot fail-open — always applied.
        sanitized_text = self.apply_regex_redaction(text, entity_registry)

        # Presidio (cards/emails/SSNs) — gated on the analyzer only (the
        # anonymizer service is unused; we replace in-process). When the analyzer
        # was never established (not configured, health-check skipped in
        # tests/dev, or down at startup), we run regex-only — this is NOT the
        # fail-closed trigger. Fail-closed applies to a RUNTIME Presidio error
        # (analyzer was working, then failed mid-operation), handled in
        # _apply_presidio; raising here would make every call fail whenever
        # Presidio simply isn't wired up.
        if self.analyzer_available:
            sanitized_text = self._apply_presidio(sanitized_text, entity_registry)

        return sanitized_text

    def _sanitize_dict(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Sanitize dictionary data recursively

        Args:
            data: Dictionary to sanitize

        Returns:
            Sanitized dictionary
        """
        sanitized = {}
        for key, value in data.items():
            # Opaque model/provider artifacts pass through verbatim — never walk
            # them (redacting reasoning signatures corrupts them, #654).
            if isinstance(key, str) and key.lower() in self._opaque_artifact_keys:
                sanitized[key] = value
                continue

            # Sanitize the key itself
            sanitized_key = self.sanitize(key)

            # If the key names a secret, redact its whole value.
            key_redaction = self._sensitive_key_redaction(key)
            if key_redaction is not None and isinstance(value, str):
                sanitized_value = key_redaction
            else:
                # Normal sanitization for non-sensitive keys
                sanitized_value = self.sanitize(value)

            sanitized[sanitized_key] = sanitized_value
        return sanitized

    # Secret-naming key words → the placeholder for their value. Ordered:
    # first category with a matching word wins. The base words are matched as
    # whole tokens (split on separators AND camelCase, see _key_tokens), so
    # "monkey"/"tokenizer"/"secretary"/"keyboard" are NOT flagged while
    # "apiKey"/"access_token"/"SecretAccessKey" are. The extra all-lowercase
    # concatenations catch common forms that don't split (e.g. "apikey") without
    # matching benign words like "monkey"/"turnkey" (#654).
    _SENSITIVE_KEY_CATEGORIES = (
        ("[PASSWORD_REDACTED]", frozenset({"password", "passwd", "pwd"})),
        (
            "[SECRET_REDACTED]",
            frozenset({"secret", "apisecret", "secretkey", "secretaccesskey"}),
        ),
        ("[TOKEN_REDACTED]", frozenset({"token", "accesstoken", "authtoken"})),
        (
            "[KEY_REDACTED]",
            frozenset({"key", "apikey", "accesskey", "privatekey"}),
        ),
    )

    @staticmethod
    def _key_tokens(key: str) -> set:
        """Lowercased word tokens of a key, split on separators AND camelCase
        boundaries: 'apiKey' → {'api','key'}, 'SecretAccessKey' → {'secret',
        'access','key'}, 'api_key' → {'api','key'}, 'monkey' → {'monkey'}."""
        spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", key)
        return {t.lower() for t in re.split(r"[^A-Za-z0-9]+", spaced) if t}

    @classmethod
    def _sensitive_key_redaction(cls, key: Any) -> Optional[str]:
        """Return the placeholder for a secret-naming dict key, else None."""
        tokens = cls._key_tokens(str(key))
        for placeholder, words in cls._SENSITIVE_KEY_CATEGORIES:
            if tokens & words:
                return placeholder
        return None

    def _sanitize_list(self, data: List[Any]) -> List[Any]:
        """
        Sanitize list data recursively

        Args:
            data: List to sanitize

        Returns:
            Sanitized list
        """
        return [self.sanitize(item) for item in data]

    def _test_service_health(self, service_url: str) -> bool:
        """Test if a Presidio service is available with external call wrapping"""

        def health_check():
            health_url = f"{service_url}/health"
            response = self.session.get(health_url, timeout=5.0)
            return response.status_code == 200

        try:
            return self.call_external_sync(
                operation_name="health_check",
                call_func=health_check,
                retries=1,
                retry_delay=1.0,
            )
        except Exception as e:
            self.logger.debug(f"Health check failed for {service_url}: {e}")
            return False

    def _apply_presidio(
        self, text: str, entity_registry: Dict[str, Dict[str, str]] | None = None
    ) -> str:
        """Apply K8s Presidio PII detection with consistent pseudonymization.

        Uses the Presidio *analyzer* to detect entities, then performs our own
        indexed replacement (skipping the Presidio anonymizer) so that each
        unique entity value gets a distinct, stable placeholder.
        """
        if not self.analyzer_available:
            self.logger.debug("Presidio analyzer not available, skipping PII detection")
            return text

        if entity_registry is None:
            entity_registry = {}

        try:
            # Step 1: Analyze text for PII entities using K8s analyzer service
            def analyze_text():
                analyze_payload = {
                    "text": text,
                    "language": "en",
                    "entities": self._presidio_entities,
                    "score_threshold": self._presidio_score_threshold,
                }

                analyze_response = self.session.post(
                    f"{self.analyzer_url}/analyze",
                    json=analyze_payload,
                    timeout=self.request_timeout,
                )

                if analyze_response.status_code != 200:
                    raise RuntimeError(
                        f"Presidio analyzer failed with status {analyze_response.status_code}"
                    )

                return analyze_response.json()

            analyzer_results = self.call_external_sync(
                operation_name="analyze_pii",
                call_func=analyze_text,
                retries=1,
                retry_delay=1.0,
            )

            if not analyzer_results:
                return text

            # Step 2: Build indexed replacements from analyzer results.
            # Sort by start position descending so we can replace right-to-left
            # without invalidating earlier offsets.
            sorted_results = sorted(
                analyzer_results, key=lambda r: r["start"], reverse=True
            )

            result = text
            for entity in sorted_results:
                start = entity["start"]
                end = entity["end"]
                entity_type = entity["entity_type"]
                original_value = text[start:end]

                # Get or create hashed placeholder via shared registry
                if entity_type not in entity_registry:
                    entity_registry[entity_type] = {}
                type_map = entity_registry[entity_type]
                if original_value not in type_map:
                    type_map[original_value] = self._hashed_placeholder(
                        entity_type, original_value
                    )

                result = result[:start] + type_map[original_value] + result[end:]

            return result

        except Exception as e:
            self.logger.warning(f"Presidio K8s service error: {e}")
            # NB: do NOT latch self.analyzer_available=False here. That flag is
            # only ever set True at startup, so latching it off on a transient
            # blip would permanently disable Presidio (and, under fail-closed,
            # permanently fail every turn) until process restart. The
            # BaseExternalClient circuit breaker already backs off on repeated
            # failures and recovers automatically, so transient errors self-heal.
            #
            # Fail-closed: if redaction is required, refuse rather than pass
            # un-analyzed text through (regex redaction already applied upstream).
            if self._fail_closed_active():
                raise RedactionUnavailableError(
                    "Presidio analyzer errored; refusing to pass un-analyzed text "
                    "to an external provider (fail-closed). Set "
                    "PROTECTION_FAIL_OPEN=true to override."
                ) from e
            return text

    def is_sensitive(self, data: Any) -> bool:
        """
        Check if data contains sensitive information

        Args:
            data: Data to check (string, dict, list, etc.)

        Returns:
            True if sensitive information is detected
        """
        if data is None:
            return False

        if isinstance(data, str):
            return self._is_text_sensitive(data)
        elif isinstance(data, dict):
            return any(
                self.is_sensitive(key) or self.is_sensitive(value)
                for key, value in data.items()
            )
        elif isinstance(data, list):
            return any(self.is_sensitive(item) for item in data)
        elif isinstance(data, (int, float, bool)):
            # Primitive types typically don't contain sensitive data
            return False
        else:
            # For other types, convert to string and check
            return self._is_text_sensitive(str(data))

    def _is_text_sensitive(self, text: str) -> bool:
        """
        Check if text contains sensitive information with external call wrapping

        Args:
            text: Text to check

        Returns:
            True if sensitive information is detected
        """
        if not text:
            return False

        # The regex half.
        if find_redaction_spans(text, self.regex_patterns):
            return True

        # Check with K8s Presidio analyzer if available
        if self.analyzer_available:
            try:

                def check_sensitivity():
                    analyze_payload = {
                        "text": text,
                        "language": "en",
                        "entities": self._presidio_entities,
                        "score_threshold": self._presidio_score_threshold,
                    }

                    analyze_response = self.session.post(
                        f"{self.analyzer_url}/analyze",
                        json=analyze_payload,
                        timeout=self.request_timeout,
                    )

                    if analyze_response.status_code == 200:
                        analyzer_results = analyze_response.json()
                        return len(analyzer_results) > 0
                    return False

                return self.call_external_sync(
                    operation_name="check_sensitivity",
                    call_func=check_sensitivity,
                    retries=1,
                    retry_delay=0.5,
                )

            except Exception as e:
                self.logger.warning(f"Presidio K8s sensitivity check failed: {e}")

        return False

    async def health_check(self) -> Dict[str, Any]:
        """
        Perform comprehensive health check for DataSanitizer.

        Returns:
            Dictionary containing health status and metrics
        """
        base_health = await super().health_check()

        # Add sanitizer-specific health data
        try:
            # Test service connectivity
            analyzer_health = self._test_service_health(self.analyzer_url)
            anonymizer_health = self._test_service_health(self.anonymizer_url)

            sanitizer_health = {
                "analyzer_available": analyzer_health,
                "anonymizer_available": anonymizer_health,
                "presidio_services": {
                    "analyzer_url": self.analyzer_url,
                    "anonymizer_url": self.anonymizer_url,
                },
                "regex_patterns_count": len(self.regex_patterns),
            }

            # Determine overall status
            if analyzer_health and anonymizer_health:
                status = "healthy"
            elif analyzer_health or anonymizer_health:
                status = "degraded"  # Partial functionality
            else:
                status = "degraded"  # Custom patterns still work

            base_health.update(
                {"sanitizer_specific": sanitizer_health, "status": status}
            )

        except Exception as e:
            base_health.update(
                {"sanitizer_specific": {"error": str(e)}, "status": "unhealthy"}
            )

        return base_health
