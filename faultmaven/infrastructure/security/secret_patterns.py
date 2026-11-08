"""The secret-detection pattern sets and the span machinery every redaction
uses (#1980). Standard library only: the document preparation worker imports
it without the sanitizer's HTTP client and settings (#2622)."""

import bisect
import re
import string
from dataclasses import dataclass, field
from typing import (
    Callable,
    Dict,
    Iterable,
    List,
    Optional,
    Sequence,
    Set,
    Tuple,
)

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
# except where a pattern names a `secret` group, which then is the span: a
# keyed secret's value (`password=<…>`, the key in clear), a bearer or basic
# credential. A value that is a provider token takes the token's own type, so
# one secret has one placeholder however it is written. A keyed value is the
# whole run after its separator, as main read it, unless the WHOLE value is a
# word from a closed list each of whose entries a negative line needs, or a
# reference (`${VAR}`, a placeholder): nothing of a secret is left in clear
# (the #2021 review, round 3).
#
# Every pattern runs in time linear in the text: no quantifier can re-split
# what it read (possessive or atomic where it could), no attempt reads past
# the next place another attempt would start (`(?!://)`, `(?!-----)`), and
# the masked rounds are capped (`_MAX_MASKED_ROUNDS`). The growth tests in
# `tests/unit/infrastructure/security/test_secret_patterns_1980.py` pin each
# hostile shape found by review.


@dataclass(frozen=True)
class RedactionPattern:
    """One detector: the entity type its matches are recorded under, and the
    compiled expression. ``secret``, when the expression names that group, is
    the span replaced; otherwise the whole match is.

    The rest only lets ``find_redaction_spans`` skip text where no match can
    be, and never changes what it finds:

    - ``triggers`` are lower-case literals, and ``hint`` a pattern over the
      lower-cased text; every match contains a trigger or a hint match. With
      neither, nothing is known and the whole text is scanned.
    - ``scope`` says where a match can lie. ``"token"``: inside one run of
      non-whitespace, which is scanned whole. ``"keyed"``: starting where a
      trigger starts, and ending on that line or the next. ``"line"``: an
      empty match at the start of a line holding a trigger, its span read
      from there to the end of the next line. ``"block"``: a match starting
      at the start of a line holding a trigger and ending in a YAML block
      indicator, its span the block's lines below it, however many
      (``_keyed_block_scalar``). ``"text"``: anywhere, across lines too.

    A token pattern's lookarounds read at most three characters past its
    run; the masked rounds rely on it (``_detect``).

    ``names_a_value`` marks a ``key <separator> value`` family: its span is a
    value a key names, and when that value is a provider token (a JWT, an
    ``sk-`` key) the token's own pattern types it, so one secret has one
    placeholder however it is spelled.
    """

    entity_type: str
    regex: "re.Pattern[str]"
    triggers: Tuple[str, ...] = ()
    scope: str = "text"
    hint: Optional["re.Pattern[str]"] = None
    names_a_value: bool = False

    #: The group whose span is replaced: ``secret`` when named, else the
    #: whole match. Derived from ``regex``, once.
    span_group: int | str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        group = "secret" if "secret" in self.regex.groupindex else 0
        object.__setattr__(self, "span_group", group)


@dataclass(frozen=True)
class RedactionSpan:
    """A detected span of the raw text, ``text[start:end]``."""

    start: int
    end: int
    entity_type: str


# --- value grammar for `key <separator> value` secrets -----------------------

#: Where a quoted value's closing quote must stand for the quotes to delimit
#: it: before closing punctuation and whitespace or the end of the text, or
#: before closing punctuation and the next item (`{"password":"x","u":1}`).
#: Otherwise (`"ab"cd`) the value is the whole run, quotes and all.
_QUOTED_END = r"""(?=[,;)\]}]*+(?:\s|\Z)|[,;)\]}]++["'{\[(])"""
#: Where an unquoted value ends: whitespace or the end of the text, after
#: closing punctuation and quotes — the run ``_merge`` leaves after a provider
#: token (``_TRAILING_PUNCTUATION``), so `token=<JWT_TOKEN_…>.` written by one
#: pass is a whole reference to the next.
_VALUE_END = r"""[,;.)\]}'"]*(?:\s|\Z)"""


def _not_the_whole_value(shape: str, end: str = _VALUE_END) -> str:
    """Not a value that IS ``shape``: quoted, the whole quoted text as
    ``_VALUE`` delimits it; else the whole run, to ``end``. A value that only
    starts with ``shape`` is not exempt: `password=${{x` and `password=<a>b`
    are secrets (#1980 review)."""
    return (
        rf"""(?!"(?:{shape})"{_QUOTED_END}|'(?:{shape})'{_QUOTED_END}"""
        rf"""|["']?(?:{shape}){end})"""
    )


#: A value that REFERS to a secret instead of holding one: a shell or GitHub
#: Actions variable (`${VAR}`, `${{ secrets.X }}`), a command substitution
#: (`$(vault read …)`), a template expression (`{{ .Values.x }}`), a
#: documentation placeholder (`<your-password>`), a placeholder or marker a
#: redaction pass wrote (`<PASSWORD_ab12>`, `[REDACTED:password]`, a title's
#: `[password]`, a sanitized dict's `[PASSWORD_REDACTED]` — so a second pass
#: over redacted text changes nothing), or a
#: SQL bind parameter (`?`, `$2`, `:pwd`, `%s`). A spaced expression's value
#: is its opening (`${{`, `{{`, `$(vault`). Markers and bind names are
#: matched case-sensitively, as written. A bare `$NAME` is not on the list: a
#: real secret can start with `$`.
_REFERENCE = (
    r"(?:\$\{\{(?:[^\s{}]*\}\})?|\{\{(?:[^\s{}]*\}\})?|\$\{[A-Za-z_][A-Za-z0-9_]*\}"
    r"|\$\((?:(?-i:[a-z][a-z0-9_.-]*)|[^\s()]*\))|<[A-Za-z][A-Za-z0-9_ -]*>"
    r"|(?-i:\[(?:REDACTED:)?[a-z][a-z0-9_]*\]|\[[A-Z][A-Z0-9_]*_REDACTED\])"
    r"|\?|\$[0-9]{1,4}|(?-i::[a-z_][a-z0-9_]*|:[A-Z_][A-Z0-9_]*)|%s"
    r"|%\([A-Za-z_][A-Za-z0-9_]*\)s)"
)
_NOT_A_REFERENCE = _not_the_whole_value(_REFERENCE)
_WHOLE_REFERENCE = re.compile(_REFERENCE, re.IGNORECASE)


def is_reference(value: str) -> bool:
    """Whether ``value``, whole, refers to a secret instead of holding one
    (``_REFERENCE``, read as the keyed patterns read it): what a keyed value
    may be and not be redacted. The runbook validator reads a detected
    secret's value with it too (#2012)."""
    return _WHOLE_REFERENCE.fullmatch(value) is not None


#: Values that say a key's state, not its secret: the closed list a keyed
#: value is never read as (#1980 review rulings). Each entry is there for a
#: line of the negative corpora that needs it (`test_secret_patterns_1980`'s
#: and the review's), and costs the rockyou hits the PR lists: `(using
#: password: YES)`/`NO`, `token: false`, `apiKey: true`, `secret: null`,
#: `Password: required`, `password: expired`, `password: n/a`, `password:
#: incorrect`, `token: invalid_grant`, `password: changed 2 days ago`,
#: `secret: rotated last week`, `api_key: missing.`, `apikey: not provided`,
#: `password: str)` and `password: Optional[str]` in code, `Password:
#: ********`, `password: ""`. Every other value is read as a secret, a
#: dictionary word included: `password: changeme` is.
_NON_SECRET_WORDS = (
    r"yes|no|true|false|null|required|expired|n/a|incorrect|invalid_grant"
    r"|changed|rotated|missing|not|str|optional\[str\]"
)
_NOT_A_STATUS = (
    _not_the_whole_value(
        rf"{_NON_SECRET_WORDS}|\*{{3,}}", r"""["']?[.,;:!?)\]}]*(?:\s|\Z)"""
    )
    + rf"""(?!""{_QUOTED_END})"""
)
#: The value, and nothing else: it is the span replaced, so the key and a
#: quoted value's quotes stay in clear (`id_token=<…>`, `"password": "<…>"`
#: stays JSON). Quoted, the text between the quotes (a backslash escapes the
#: next character), when the closing quote ends the value too. Else the
#: whole run up to whitespace, as main read it — through a quoted part that
#: has more after it (`'x y'=z`, `"ab"cd`), quote and all, and from an
#: unterminated quote (`"ab` cd: `"ab`, as main) — an escaped `\n` inside it, a
#: closing bracket or quote at its end, are all part of the value
#: (`{"msg": "password: x"}` loses its `"}`; a secret never keeps a tail).
_VALUE = (
    rf"{_NOT_A_STATUS}{_NOT_A_REFERENCE}"
    r"""(?P<quote>["'])?(?P<secret>(?(quote)"""
    r"""(?:\\[^\r\n]|(?!(?P=quote))[^\\\r\n])++"""
    r"""|(?:(?P<run_quote>["'])(?:\\[^\r\n]|(?!(?P=run_quote))[^\\\r\n])*+"""
    r"""(?P=run_quote)\S*+|\S++)))"""
    rf"(?(quote)(?P=quote){_QUOTED_END})"
)
#: A key on its line, then a value: the next line's first token after `=`
#: with nothing after it on its own line, as main read it (`password=` then
#: the value) — unless that line starts a key of its own (`USER=bob`, but not
#: a `scheme://` URL) or a list item. After `:` see ``_keyed_on_next_line``.
_NOT_A_KEY_OR_ITEM = r"(?![\w.-]+[ \t]*[:=](?!//)|-(?:[ \t]|\Z))"
_NEXT_LINE = rf"(?:\r?\n[ \t]*+{_NOT_A_KEY_OR_ITEM})?"
#: The same for a YAML block, as YAML reads one: a key is `name:` before
#: whitespace (`secretKeyRef:`), a list item `- `; `john3:16`, `a=b` and
#: `redis://…` are scalars, so values.
_NOT_A_YAML_KEY_OR_ITEM = r"(?![\w.-]+[ \t]*:(?:\s|\Z)|-(?:\s|\Z))"
#: A word as prose writes one: lower case, Capitalised, or a short ALL-CAPS
#: word, joined by `-`, `_`, `'` or `[`, or short ones joined by `/`
#: (`n/a`, `and/or`), in optional parentheses, with optional trailing
#: punctuation; or a number of at most three digits. Only a command-line flag
#: (`--password value`) reads it: "use --token for the cli" is prose, and
#: main never read a flag at all.
_WORD = r"(?:[a-z]+|[A-Z][a-z]+|[A-Z]{1,8})"
_SHORT = r"(?:[a-z]{1,3}|[A-Z][a-z]{0,2}|[A-Z]{2,3})"
_WORD_RUN = rf"(?:{_WORD}(?:[-_'\[]{_WORD})*|{_SHORT}(?:/{_SHORT}){{1,2}})"
_PROSE_VALUE = rf"\(?(?:{_WORD_RUN}|[0-9]{{1,3}})[.,;:!?)\]]*(?=\s|\Z)"
#: A compound key's value that is a count, not a secret: that is how event
#: counts print (`failed_password=520`, `failed_password: 520`,
#: `output_token: 30`, #654) — the config extractor's `_NON_SECRET_VALUE_RE`
#: precedent, cut to what the negative corpora need (its flags are status
#: words already).
_NOT_A_COUNT = _not_the_whole_value(r"\d{1,5}")
#: `=`, or `=>` before whitespace (`"password" => "x"`; a `>` glued to what
#: follows is the value's: `password=>x9` is the password `>x9`). A spaced
#: `==` is a comparison (`if password == "":`), not an assignment; a tight
#: `password==x` is a value that starts with `=`. Possessive, so `pwd =>
#: ${VAR}` cannot give back the `>` and read it as a value.
_ASSIGN = r"(?:=|[ \t]++=(?!=))(?:>(?=\s))?+[ \t]*+"
_COLON = r"[ \t]*+:[ \t]*+"
#: A quoted key's closing quote (`"password": x`, `'pwd' => x`).
_KEY_QUOTE = r"""["']?+"""
#: An escaped newline, tab or return (`"line one\npassword: …"` in a JSON log
#: field), or a percent-encoded byte (`%3Dsk-…`), before a token: a boundary,
#: as the character it encodes would be. Case-sensitive: `\N` escapes
#: nothing.
_ESCAPED_CONTROL = r"(?-i:\\[ntr])"
_ESCAPE = rf"(?<={_ESCAPED_CONTROL})|(?<=%[0-9A-Fa-f]{{2}})"
#: A YAML block-scalar indicator (`|`, `>`, a chomping `-`/`+` and an
#: indentation digit, in either order), and what may end its line: a comment
#: (YAML's ` #`, whatever follows it) or the line's end. Read only after
#: `key:` (#2025): `--password | tee` and `token=>` are not blocks.
_BLOCK_INDICATOR = r"[|>](?:[-+][1-9]?|[1-9][-+]?)?"
#: Up to two node properties before the indicator, a tag or an anchor
#: (`password: !!binary |`, `password: &pw |`, `db_password: !vault |`),
#: each ended by blanks. A property is read to the next blank or `:`, so a
#: scan that tries every `:` reads each character once (`: !` glued to `:`
#: again and again would be re-read to the end of the run per `:`).
_NODE_PROPERTIES = r"(?:[!&][^\s:]*+[ \t]++){0,2}+"
_BLOCK_START = rf"{_NODE_PROPERTIES}{_BLOCK_INDICATOR}"
_BLOCK_LINE_REST = r"(?:[ \t]++#|[ \t]*+(?![^\r\n]))"
#: After `key:`, a block indicator is not the value (``_keyed_block_scalar``
#: reads the block), so `password: |` keeps its indicator and stays YAML.
_NOT_A_BLOCK_INDICATOR = rf"(?!{_BLOCK_START}{_BLOCK_LINE_REST})"


def _keys(keywords: str) -> Tuple[str, str, str]:
    """A family's keyword, as a bare key and as a compound key (see
    ``_keyed_secret``)."""
    key = rf"(?:{keywords})"
    bare = rf"(?:(?<![A-Za-z0-9_])|{_ESCAPE}){key}[_-]?"
    compound = (
        rf"(?<=[A-Za-z0-9_])(?<!{_ESCAPED_CONTROL})(?<!%[0-9A-Fa-f]{{2}})"
        rf"{key}[_-]?"
    )
    return key, bare, compound


def _keyed_secret(keywords: str, value_guard: str = "") -> str:
    """``key <separator> value``, for one family of secret-naming keys.

    The span is the VALUE (``_VALUE``): the key stays in clear. The spellings:

    1. A bare key with ``=``/``=>``, ``:`` (``key:value``, ``key : value``,
       ``key: value``), as a quoted key too (``"key": value``), optionally
       after `_`/`-` (``password_=``, main's spelling): any value but a
       status word (``_NOT_A_STATUS``) or a reference, on the same line — or,
       after ``=`` with nothing after it, on the next (``_NEXT_LINE``; after
       ``:`` that is ``_keyed_on_next_line``'s).
    2. A compound key — ``db_password``, ``clientSecret``, ``PGPASSWORD``,
       ``xpassword``, a keyword glued to whatever precedes it — with the same
       separators, and a value that is not a count either (``_NOT_A_COUNT``):
       that is how event counts print (``failed_password: 520`` survives,
       #654).
    3. A command-line flag (``--password value``): a value not shaped like a
       word (``_PROSE_VALUE``), and not a separator (`` : `` is spelling 1's).
       The empty ``flag`` group marks it: a match that took it stands only
       when the word holding the key starts with ``-`` (``_flag_in_a_word``,
       #2025), so ``--db-password x`` is a flag and the command names
       ``get-login-password`` and ``rotate-secret`` are not.

    After ``:`` a YAML block indicator is not a value (``password: |``):
    ``_keyed_block_scalar`` reads the block.

    "Bare" means not preceded by a letter, digit or underscore — or preceded
    by an escaped newline/tab/return or a percent-encoded byte — so
    ``X-Auth-Token``,
    ``spring.datasource.password`` and ``"…\\npassword: …"`` hold bare keys
    and ``failed_password`` a compound one.
    """
    key, bare, compound = _keys(keywords)
    separator = (
        rf"{_KEY_QUOTE}(?:{_ASSIGN}{_NEXT_LINE}|{_COLON}{_NOT_A_BLOCK_INDICATOR})"
    )
    return (
        rf"(?:{bare}{separator}"
        rf"|{compound}{separator}{_NOT_A_COUNT}"
        rf"|(?<=-){key}(?P<flag>)[ \t]++(?![:=])(?-i:(?!{_PROSE_VALUE})))"
        rf"{value_guard}{_VALUE}"
    )


def _keyed_on_next_line(keywords: str, value_guard: str = "") -> str:
    """``key:`` ending its line, and the value starting the next — only when
    that line is indented deeper than the key's line, as a YAML block writes
    a value it continues (``password:`` then ``    hunter2``), and is not a
    comment, a key of its own or a list item. ``Enter password:`` then a log
    line, a status line or ``# filled in by CI``, is a prompt or an empty
    key, not a value.

    It matches, empty, at the start of the key's line, so the line's
    indentation is read; the value is the ``secret`` group, read inside the
    lookahead. Same keys, guards and value as ``_keyed_secret``.
    """
    _key, bare, compound = _keys(keywords)
    return (
        r"(?<![^\n])(?=(?P<indent>[ \t]*+)[^\r\n]*?"
        rf"(?:{bare}|{compound}(?P<compound>)){_KEY_QUOTE}{_COLON}"
        rf"\r?\n(?P=indent)[ \t]++(?!#){_NOT_A_YAML_KEY_OR_ITEM}"
        rf"(?(compound){_NOT_A_COUNT}){value_guard}{_VALUE})"
    )


#: Lines of only blanks, each followed by another line; a block body's
#: indentation: deeper than the key's line (``indent``), and for a list item's
#: key (`- password: |`, ``item``) deeper than the key itself, so the item's
#: sibling keys (`  user: bob`) are not its body; and a body line, not blank.
_BLANK_LINES = r"(?:\r?\n[ \t]*+(?=\r?\n))*+"
_DEEPER = r"(?P=indent)(?(item)[ \t]{2})[ \t]++"
_BODY_LINE = rf"\r?\n{_DEEPER}[^ \t\r\n][^\r\n]*+"


def _keyed_block_scalar(keywords: str) -> str:
    """``key: |`` (or ``>``, with a chomping ``-``/``+`` and an indentation
    digit, after up to two node properties, a tag or an anchor, and with an
    optional comment): the value is the YAML block below it (#2025). The body
    is every following line indented deeper than the key's line — for a
    ``- key: |`` list item, deeper than the key itself; a run of blank lines
    belongs to it only when a deeper line follows the run, and it ends at the
    first other line or at the end of the text.
    An empty block (the indicator, then a sibling or the end) reads nothing.

    The span runs from the first body line's first non-blank character to the
    end of the last body line, so one placeholder stands at the first line's
    indentation, the text stays YAML, and a second pass reads a block whose
    whole body is a reference (``_REFERENCE``): exempt, so one pass is
    idempotent. Same keys as ``_keyed_secret``, after ``:`` only.

    Linear: the key line is matched atomically, committing to the first key
    whose indicator ends the line (after a ``#`` everything is comment), and
    every quantifier over the body is possessive. Whether the key is a list
    item's is decided there too, by the first body line's indentation (a body
    at the dash's own depth is read as the dash's), and never revisited: a
    second pass reading a placeholder body must not fall back to the dash's
    depth and take the item's siblings. The match consumes the body,
    so a scan resumes after it, and a key line inside a body (whose own block
    lies inside the outer one) is not read again.
    """
    _key, bare, compound = _keys(keywords)
    return (
        r"(?<![^\n])(?P<indent>[ \t]*+)"
        rf"(?>(?P<item>-[ \t](?=[^ \t]))?"
        rf"(?>[^\r\n]*?(?:{bare}|{compound}){_KEY_QUOTE}{_COLON}"
        rf"{_BLOCK_START}{_BLOCK_LINE_REST})[^\r\n]*+"
        rf"{_BLANK_LINES}\r?\n{_DEEPER})"
        rf"(?!{_REFERENCE}[ \t]*+(?![^\r\n])"
        rf"(?!{_BLANK_LINES}\r?\n{_DEEPER}[^ \t\r\n]))"
        rf"(?P<secret>[^ \t\r\n][^\r\n]*+(?:{_BLANK_LINES}{_BODY_LINE})*+)"
    )


# --- private keys ------------------------------------------------------------

_PEM_BEGIN = r"-----BEGIN[A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----"
_PEM_END = r"-----END[A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----"
#: A whole block: its body never holds `-----`, so a BEGIN with no END ahead
#: costs only the run to the next `-----`, not the rest of the text.
_PEM_BLOCK = _PEM_BEGIN + r"(?:(?!-----)[\s\S])*+" + _PEM_END
#: A key block with no END line (a truncated paste): the BEGIN line and every
#: line after it that is still key material — a base64 line, a PEM header
#: (`Proc-Type:`/`DEK-Info:`), a blank line, the same run flattened onto one
#: line, or JSON-escaped `\n`s. It stops at the first line that is not, so the
#: log lines after a truncated key survive; bounding it by end of text would
#: redact every one of them. Possessive throughout: a line of spaces is read
#: once, never re-split between two `[ \t]*`.
_PEM_TRUNCATED_BODY = (
    r"(?:[ \t]++[A-Za-z0-9+/=]{16,}+"
    r"|(?:\\r)?\\n[A-Za-z0-9+/=]*+"
    r"|\r?\n[ \t]*+(?:(?:[A-Za-z0-9+/=]++|(?:Proc-Type|DEK-Info|Comment):[^\r\n]*+)"
    r"[ \t]*+)?(?=\r?\n|\Z))*+"
)

# --- the set -----------------------------------------------------------------

_I = re.IGNORECASE
#: `namespace/name`, each part two or more DNS-label characters (`n/a` is a
#: status, not a namespace).
_NOT_A_K8S_REF = _not_the_whole_value(r"(?-i:[a-z0-9][-a-z0-9]+/[a-z0-9][-.a-z0-9]+)")
#: A path, the whole value (`PWD=/home/app`, `pwd: ~/src`): the working
#: directory, not a password — as a title's scrub writes it too, its parts
#: marked (`PWD=[path][email]`, ``_sanitize_title_content``), so a scrubbed
#: title stays as it is. `pwd=/x$Q9!` is a password.
_NOT_A_PATH = _not_the_whole_value(
    r"(?:[/~]|~?(?-i:\[path\]))(?:[\w./@+-]|(?-i:\[[a-z][a-z0-9_]*\]))*+"
)
_CREDENTIAL_SCHEMES = r"(?:postgres(?:ql)?|mysql|mariadb|mongodb|redis|rediss|amqps?)"


def _pattern(
    entity_type: str,
    expression: str,
    flags: int,
    triggers: Tuple[str, ...],
    scope: str = "token",
    hint: Optional[str] = None,
    names_a_value: bool = False,
) -> RedactionPattern:
    return RedactionPattern(
        entity_type,
        re.compile(expression, flags),
        triggers,
        scope,
        re.compile(hint) if hint else None,
        names_a_value,
    )


def _keyed(
    entity_type: str,
    keywords: str,
    triggers: Tuple[str, ...],
    value_guard: str = "",
) -> Tuple[RedactionPattern, RedactionPattern, RedactionPattern]:
    """A family of secret-naming keys, case-insensitive: its value on the
    key's line (``_keyed_secret``), its value on the next line after ``key:``
    (``_keyed_on_next_line``), and the YAML block after ``key: |``
    (``_keyed_block_scalar``). ``triggers`` must each be the start of a
    keyword (every keyword starts with one): the first starts at a trigger,
    the other two at the start of a line holding one."""
    return (
        _pattern(
            entity_type,
            _keyed_secret(keywords, value_guard),
            _I,
            triggers,
            scope="keyed",
            names_a_value=True,
        ),
        _pattern(
            entity_type,
            _keyed_on_next_line(keywords, value_guard),
            _I,
            triggers,
            scope="line",
            names_a_value=True,
        ),
        _pattern(
            entity_type,
            _keyed_block_scalar(keywords),
            _I,
            triggers,
            scope="block",
            names_a_value=True,
        ),
    )


#: The secret-detection pattern set. Order is the tie-break when two patterns
#: claim the same span (see ``find_redaction_spans``), so the more specific
#: come first.
SECRET_PATTERNS: Tuple[RedactionPattern, ...] = (
    # Private keys: a whole block, else a truncated one (no END line).
    _pattern(
        "PRIVATE_KEY",
        _PEM_BLOCK,
        _I,
        ("private key",),
        scope="text",
    ),
    _pattern(
        "PRIVATE_KEY",
        _PEM_BEGIN + _PEM_TRUNCATED_BODY,
        _I,
        ("private key",),
        scope="text",
    ),
    # Connection URLs with a user (and password) before the host. The user
    # part never crosses whitespace — the former `[^@]+` ran on to the next
    # `@` anywhere later in the text (an email, an @mention, `user@service`)
    # and redacted everything between. Within the URL it runs to the LAST
    # `@`, so a password holding a raw `@` and `/` (`p@ss/w0rd`) is not cut
    # short with its tail in clear.
    _pattern(
        "DATABASE_URL",
        rf"{_CREDENTIAL_SCHEMES}(?:\+[A-Za-z0-9]+)?://"
        r"(?:(?:(?!://)[^@\s])*+@)++[^/\s]+",
        _I,
        ("://",),
    ),
    # Any other URL that carries `user:password@`. (A former "docker registry
    # credentials" pattern `email@host.tld:token` was removed in #654 — it
    # false-matched any `user@host.tld:port` in logs/connection strings. This
    # one needs the `scheme://` and the `:password`.)
    _pattern(
        "CREDENTIAL_URL",
        rf"(?:(?<![A-Za-z0-9+.-])|{_ESCAPE})[A-Za-z][A-Za-z0-9+.-]*://"
        r"[^\s:/?#@]+:[^\s/?#@]+@[^\s/?#]+",
        _I,
        ("://",),
    ),
    # Provider tokens with a fixed prefix.
    _pattern(
        "GITHUB_TOKEN",
        rf"(?:(?<![A-Za-z0-9_])|{_ESCAPE})"
        r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})",
        0,
        ("ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_"),
    ),
    _pattern(
        "SLACK_TOKEN",
        rf"(?:(?<![A-Za-z0-9_-])|{_ESCAPE})"
        r"(?:xox[a-z]-[A-Za-z0-9-]{10,}|xapp-[0-9]-[A-Za-z0-9-]{10,})",
        0,
        ("xox", "xapp-"),
    ),
    # GitLab personal, impersonation, project and group access tokens: the
    # `glpat-` prefix (docs.gitlab.com/security/tokens/#token-prefixes), and
    # a 20-character token (docs.gitlab.com/user/profile/personal_access_tokens/),
    # longer when routable. GitLab's other prefixes document no shape.
    _pattern(
        "GITLAB_TOKEN",
        rf"(?:(?<![A-Za-z0-9_])|{_ESCAPE})glpat-[0-9A-Za-z_-]{{20,}}+",
        0,
        ("glpat-",),
    ),
    # OpenAI-style keys, `sk-proj-…`/`sk-ant-…` included, and `pk-` keys.
    # Not after a letter: `task-0123…abcdef` is a task id, not a key (a
    # listed trade: main read `xsk-…`). The digit a dashed key must hold is
    # looked for in its first 64 characters, so `sk-sk-sk-…` costs no rescan.
    _pattern(
        "API_KEY",
        rf"(?:(?<![A-Za-z])|{_ESCAPE})sk-(?:[A-Za-z0-9]{{32,}}"
        r"|(?=[A-Za-z_-]{0,64}+[0-9])[A-Za-z0-9_-]{20,})",
        _I,
        ("sk-",),
    ),
    _pattern(
        "API_KEY", rf"(?:(?<![A-Za-z])|{_ESCAPE})pk-[A-Za-z0-9]{{32,}}", _I, ("pk-",)
    ),
    # AWS access key ids (long-term AKIA, temporary ASIA). Case-sensitive: a
    # real id is upper case, and "asia" + 16 letters is prose (a listed
    # trade: main also read lower-case `akia…`). No boundary, as on main: an
    # id glued to anything is still one.
    _pattern(
        "AWS_ACCESS_KEY",
        r"(?:AKIA|ASIA)[0-9A-Z]{16}",
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
        r"aws[_-]?secret[_-]?access[_-]?key['\"]?\s*+[=:]\s*+['\"]?[0-9a-zA-Z/+]{40}",
        _I,
        ("secret",),
        scope="text",
    ),
    # JWTs. (The former `[A-Za-z0-9-_=]` was a `9-_` RANGE, which also took
    # `:;<=>?@[\]^`.) A JWT glued to what precedes it is one, as on main
    # (`x{jwt}`, `"…\n{jwt}"`): each run of its characters is tried ONCE,
    # from its start, committed to its first `eyJ` — the header runs to the
    # first `.`, so a later `eyJ` in the same run could only match the same
    # way — and the span starts at the `eyJ`. Read at every `eyJ`, a run of
    # them with no `.` rescanned itself per occurrence.
    _pattern(
        "JWT_TOKEN",
        r"(?<![A-Za-z0-9_=-])(?>[A-Za-z0-9_=-]*?(?=eyJ))"
        r"(?P<secret>eyJ[A-Za-z0-9_=-]++\.[A-Za-z0-9_=-]++\.?[A-Za-z0-9_.+/=-]*+)",
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
        rf"(?:(?<![A-Za-z0-9_])|{_ESCAPE})bearer[ \t]++(?P<secret>"
        r"(?=[A-Za-z0-9._~+/-]*"
        r"(?:[0-9]|[._~+/](?=[A-Za-z0-9])|(?-i:[a-z][A-Z])))"
        r"[A-Za-z0-9._~+/-]{8,}=*)",
        _I,
        ("bearer",),
        scope="keyed",
    ),
    # `Authorization: Basic <base64 user:password>`: the credential is the
    # span.
    _pattern(
        "BASIC_AUTH",
        rf"(?:(?<![A-Za-z0-9_-])|{_ESCAPE})(?:proxy-)?authorization[\"']?[ \t]*+[:=]"
        r"[ \t]*+[\"']?basic[ \t]++(?P<secret>"
        r"(?=[A-Za-z0-9+/]*(?:[0-9+/]|(?-i:[a-z][A-Z])))"
        r"[A-Za-z0-9+/]{8,}={0,2})",
        _I,
        ("authorization", "proxy-authorization"),
        scope="keyed",
    ),
    _pattern("K8S_SECRET", r"k8s[_-]?secret[_-]?[0-9a-fA-F]{32,}", _I, ("k8s",)),
    # `apikey<hex>` with no separator, then the keyed spellings.
    _pattern("API_KEY", r"api[_-]?key[_-]?[0-9a-fA-F]{32,}", _I, ("api",)),
    *_keyed("API_KEY", r"api[_-]?key", ("api",)),
    *_keyed("PASSWORD", r"password|passwd|passphrase", ("pass",)),
    # `pwd` is also the working directory (`PWD=/home/app` in every env
    # dump): a path is not its password.
    *_keyed("PASSWORD", r"pwd", ("pwd",), value_guard=_NOT_A_PATH),
    # A secret or a token named `namespace/name` is a Kubernetes object, not
    # its value (`Secret: monitoring/grafana-admin not found`).
    *_keyed("SECRET", r"secret(?:[_-]?key)?", ("secret",), value_guard=_NOT_A_K8S_REF),
    *_keyed("TOKEN", r"token", ("token",), value_guard=_NOT_A_K8S_REF),
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
        hint=r"[:-][0-9a-f]{2}[:-]",
    ),
)

#: The sanitizer's regex half: every secret, then the network identifiers.
SANITIZER_PATTERNS: Tuple[RedactionPattern, ...] = (
    SECRET_PATTERNS + NETWORK_ADDRESS_PATTERNS
)

#: A detection before merging: ``(start, -end, priority, entity_type,
#: names_a_value)``, so sorting puts the leftmost, the longest at one start,
#: the earliest pattern at one span first.
_Candidate = Tuple[int, int, int, str, bool]

#: The non-ASCII letters `re.IGNORECASE` folds onto an ASCII one that
#: `str.lower()` leaves alone: dotless i and long s (sre's `_equivalences`).
#: Mapped back so a trigger is found wherever its pattern can match.
_IGNORECASE_ASCII_FOLDS = {0x131: "i", 0x17F: "s"}


def _trigger_view(text: str) -> Optional[str]:
    """``text`` lower-cased for the trigger checks, index for index — or
    None when lower-casing changes its length (``İ``), when no index maps
    and every pattern runs on the whole text."""
    view = text.lower()
    if "\u0131" in view or "\u017f" in view:
        view = view.translate(_IGNORECASE_ASCII_FOLDS)
    return view if len(view) == len(text) else None


#: Each pattern set's distinct triggers, by the set's identity: one ``in``
#: per trigger per text, not per pattern.
_TRIGGER_UNIONS: Dict[int, Tuple[Sequence[RedactionPattern], Tuple[str, ...]]] = {}


def _triggers_in(view: str, patterns: Sequence[RedactionPattern]) -> Set[str]:
    """The triggers of ``patterns`` that occur in ``view``."""
    cached = _TRIGGER_UNIONS.get(id(patterns))
    if cached is None or cached[0] is not patterns:
        union = tuple(sorted({t for pattern in patterns for t in pattern.triggers}))
        cached = _TRIGGER_UNIONS[id(patterns)] = (patterns, union)
    return {trigger for trigger in cached[1] if trigger in view}


def _present(pattern: RedactionPattern, view: str, found: Set[str]) -> bool:
    """Whether any of ``pattern``'s triggers (``found``: those in ``view``),
    or its hint, occurs in ``view``."""
    if not found.isdisjoint(pattern.triggers):
        return True
    return pattern.hint is not None and pattern.hint.search(view) is not None


def _hits(pattern: RedactionPattern, view: str) -> List[int]:
    """Where ``pattern``'s triggers and hint occur in ``view``, sorted."""
    hits = []
    for trigger in pattern.triggers:
        hit = view.find(trigger)
        while hit != -1:
            hits.append(hit)
            hit = view.find(trigger, hit + 1)
    if pattern.hint is not None:
        hits.extend(match.start() for match in pattern.hint.finditer(view))
    return sorted(hits)


def _within(hits: List[int], lines: Optional[List[Tuple[int, int]]]) -> List[int]:
    """``hits`` that lie on ``lines``, or all of them when there are none.
    Both in order, walked once."""
    if lines is None:
        return hits
    inside = []
    line = 0
    for hit in hits:
        while line < len(lines) and lines[line][1] <= hit:
            line += 1
        if line < len(lines) and lines[line][0] <= hit:
            inside.append(hit)
    return inside


#: What a command-line word is made of: a flag (`--db-password`) or a
#: hyphenated command name (`get-login-password`), and nothing else, so a
#: flag glued after punctuation (`"--password x"`, `;--password`, `$(--`)
#: is still a word that starts with `-`.
_WORD_CHARS = frozenset(string.ascii_letters + string.digits + "_.-")


def _run_start(text: str, position: int, walked: List[int]) -> int:
    """Where the run of word characters (``_WORD_CHARS``) holding
    ``position`` starts.

    ``walked`` is ``[position, run start]`` of the last call, and positions
    only grow, so the walk back stops where the last one began: each
    character is read once per scan, as ``_token_matches`` reads each run
    once."""
    floor, floor_start = walked
    start = position
    while start > floor and start and text[start - 1] in _WORD_CHARS:
        start -= 1
    if start == floor and floor >= 0:
        start = floor_start
    walked[0], walked[1] = position, start
    return start


def _flag_in_a_word(match: "re.Match[str]", text: str, walked: List[int]) -> bool:
    """Whether ``match`` read the command-line flag spelling (its ``flag``
    group took part) of a key whose word does not start with ``-``: a
    hyphenated command name (``get-login-password --region``,
    ``rotate-secret \\``), not a flag (#2025). ``--db-password x`` is one, and
    so is ``"--password x"``: the word is ``_WORD_CHARS``, not the whole run
    of non-whitespace. A key right after ``--`` is a long option wherever the
    word starts (``me@example.com--token x``), so the verdict does not change
    when what is glued before it is redacted or marked (a title's
    ``[email]--token x``)."""
    start = match.start()
    return (
        match.start("flag") != -1
        and text[max(start - 2, 0) : start] != "--"
        and text[_run_start(text, start, walked)] != "-"
    )


def _finditer(
    pattern: RedactionPattern, text: str, pos: int = 0, endpos: Optional[int] = None
) -> Iterable["re.Match[str]"]:
    """``pattern.regex.finditer(text, pos, endpos)``, less the matches
    ``_flag_in_a_word`` drops. After a dropped match the scan resumes one past
    its start, so a match it covered is found, as a scan from each hit finds
    it (``_keyed_matches``)."""
    end = len(text) if endpos is None else endpos
    if "flag" not in pattern.regex.groupindex:
        return pattern.regex.finditer(text, pos, end)
    matches = []
    walked = [-1, -1]
    while pos <= end:
        match = pattern.regex.search(text, pos, end)
        if match is None:
            break
        if _flag_in_a_word(match, text, walked):
            pos = match.start() + 1
            continue
        matches.append(match)
        pos = max(match.end(), match.start() + 1)
    return matches


def _keyed_matches(
    pattern: RedactionPattern, text: str, hits: List[int]
) -> List["re.Match[str]"]:
    """``finditer`` for a keyed pattern, trying only where its match can
    start — each hit, in order: leftmost first, and the next try from where
    the last match ended, as ``finditer`` does. A match ``_flag_in_a_word``
    drops is no match: the next hit is tried, even inside it."""
    matches = []
    resume = 0
    flagged = "flag" in pattern.regex.groupindex
    walked = [-1, -1]
    for start in hits:
        if start >= resume:
            match = pattern.regex.match(text, start)
            if match and not (flagged and _flag_in_a_word(match, text, walked)):
                matches.append(match)
                resume = match.end()
    return matches


#: What every line pattern's match holds: `:`, blanks, a newline, and the next
#: line's indentation.
_KEY_LINE_END = re.compile(r":[ \t]*\r?\n[ \t]")
#: What every block pattern's match holds: `:`, a block indicator, and a
#: comment or the line's end. Neither can a stand-in make.
_BLOCK_LINE_END = re.compile(rf":[ \t]*+{_BLOCK_START}(?:[ \t]++#|[ \t]*+\r?\n)")
#: The same, ending the line ``_block_matches`` is given.
_BLOCK_KEY_LINE = re.compile(rf":[ \t]*+{_BLOCK_START}(?:[ \t]++#|[ \t]*+\r?$)")


def _block_matches(
    pattern: RedactionPattern, text: str, hits: List[int]
) -> List["re.Match[str]"]:
    """``finditer`` for a block pattern: tried once at the start of each line
    holding a hit that ends in a block indicator after ``:``, and only from
    where the last match ended — a match consumes its block, and a key line
    inside a block is part of that block's value. Hits are in order, so the
    text is read once."""
    matches = []
    resume = 0
    line_start = -1
    searched = 0
    for hit in hits:
        if hit < resume:
            continue
        newline = text.rfind("\n", searched, hit)
        start = newline + 1 if newline != -1 else max(line_start, 0)
        searched = hit
        if start == line_start:
            continue
        line_start = start
        end = text.find("\n", hit)
        if end == -1:
            continue
        searched = end
        if _BLOCK_KEY_LINE.search(text, start, end):
            match = pattern.regex.match(text, start)
            if match:
                matches.append(match)
                resume = match.end()
    return matches


def _line_matches(
    pattern: RedactionPattern, text: str, hits: List[int]
) -> List["re.Match[str]"]:
    """``finditer`` for a line pattern: tried once at the start of each line
    holding a hit — where its every match is — and only when the line can
    be a key's: it ends in ``:`` (and blanks), and the next line starts
    indented. The hits are in order, so each newline search starts where the
    last one ended: the text is read once."""
    matches = []
    line_start = -1
    searched = 0
    for hit in hits:
        newline = text.rfind("\n", searched, hit)
        start = newline + 1 if newline != -1 else max(line_start, 0)
        searched = hit
        if start == line_start:
            continue
        line_start = start
        end = text.find("\n", hit)
        if end == -1 or text[end + 1 : end + 2] not in (" ", "\t"):
            continue
        searched = end
        if text[start:end].rstrip("\r").rstrip(" \t").endswith(":"):
            match = pattern.regex.match(text, start)
            if match:
                matches.append(match)
    return matches


_NON_WHITESPACE = re.compile(r"\S*")


def _token_matches(
    pattern: RedactionPattern,
    text: str,
    hits: List[int],
    span_starts: Optional[List[int]] = None,
    span_ends: Optional[List[int]] = None,
) -> List["re.Match[str]"]:
    """``finditer`` for a token pattern, over only the non-whitespace runs
    holding a hit — and, given the spans so far, only those holding a span —
    each scanned from its start to just past its end, with ``pos``/``endpos``
    on the whole text so lookarounds read the real neighbours. Each run is
    walked once."""
    matches: List["re.Match[str]"] = []
    scanned_to = -1
    for hit in hits:
        if hit < scanned_to:
            continue
        start = hit
        while start > scanned_to and start and not text[start - 1].isspace():
            start -= 1
        end = min(_NON_WHITESPACE.match(text, hit).end() + 1, len(text))
        scanned_to = end
        if span_starts is not None and not _overlaps(
            span_starts, span_ends, start, end
        ):
            continue
        matches.extend(pattern.regex.finditer(text, start, end))
    return matches


def _overlaps(starts: List[int], ends: List[int], start: int, end: int) -> bool:
    """Whether a span (by its sorted ``starts`` and ``ends``) meets
    ``[start, end)``."""
    index = bisect.bisect_left(ends, start + 1)
    return index < len(starts) and starts[index] < end


def _detect(
    text: str,
    patterns: Sequence[RedactionPattern],
    lines: Optional[List[Tuple[int, int]]] = None,
    candidates: Optional[List[_Candidate]] = None,
    only: Optional[Set[int]] = None,
    spans: Optional[List[RedactionSpan]] = None,
) -> Set[int]:
    """Add every match of every pattern on ``text`` to ``candidates``, as
    sortable ``(start, -end, priority, entity_type, names_a_value)`` — or,
    given ``lines`` (``(start, end)`` of whole lines, newline included), every
    match that can lie on them: a token pattern is scanned only there, a keyed
    or line one from there and the line before (its value can sit on the next
    line). ``only`` limits the run to those priorities. ``spans``, in a masked
    round, are the spans masked: a new match must read or cover one, and a
    token pattern reads nothing past its run but up to three characters, so
    it is scanned only in the runs holding one. (A keyed or line pattern's
    guards read its whole value and the next line, so it is tried on every
    touched line and the line before.) Returns the priorities that were
    scanned: the patterns that can match ``text`` at all.

    Each pattern is scanned only where its ``triggers``/``hint`` and
    ``scope`` say a match can be (see ``RedactionPattern``), always with
    ``pos``/``endpos`` or ``match`` at a position of the whole text, so
    lookarounds read what a whole-text scan's would (a window's ``endpos``
    reaches the end of the line after it). None of this changes what is
    found — only what it costs.
    """
    view = _trigger_view(text)
    found = _triggers_in(view, patterns) if view is not None else set()
    span_starts = [span.start for span in spans] if spans is not None else None
    span_ends = [span.end for span in spans] if spans is not None else None
    if candidates is None:
        candidates = []
    scanned = set()
    # A family's keyed and line patterns share their triggers: their hits,
    # and the windows of a masked round, are found once.
    shared_hits: Dict[Tuple[str, ...], List[int]] = {}
    before = _with_line_before(text, lines)
    # A line pattern needs a line ending in `:` before an indented one, a
    # block pattern a line ending in `: |`; no stand-in can make either, so a
    # text without one skips them every round.
    key_line: Optional[bool] = None
    block_line: Optional[bool] = None
    # A block pattern's key line can lie any number of lines above the lines
    # a masked round touched, so it is tried at every hit, every round: the
    # rounds are capped, so that stays linear.
    all_hits: Dict[Tuple[str, ...], List[int]] = {}
    for priority, pattern in enumerate(patterns):
        if only is not None and priority not in only:
            continue
        if pattern.scope == "line":
            if key_line is None:
                key_line = _KEY_LINE_END.search(text) is not None
            if not key_line:
                continue
        if pattern.scope == "block":
            if block_line is None:
                block_line = _BLOCK_LINE_END.search(text) is not None
            if not block_line:
                continue
        located = view is not None and (pattern.triggers or pattern.hint)
        if located and not _present(pattern, view, found):
            continue
        scanned.add(priority)
        if not located or pattern.scope == "text":
            if lines is not None and pattern.scope not in ("text", "block"):
                windows = (
                    _with_line_before_and_after(text, lines)
                    if pattern.scope in ("keyed", "line")
                    else lines
                )
                matches: Iterable["re.Match[str]"] = [
                    match
                    for start, end in windows
                    for match in _finditer(pattern, text, start, end)
                ]
            else:
                matches = _finditer(pattern, text)
        else:
            if pattern.scope == "block":
                hits = all_hits.get(pattern.triggers) if pattern.hint is None else None
                if hits is None:
                    hits = _hits(pattern, view)
                    if pattern.hint is None:
                        all_hits[pattern.triggers] = hits
                matches = _block_matches(pattern, text, hits)
            elif pattern.scope in ("keyed", "line") and pattern.hint is None:
                hits = shared_hits.get(pattern.triggers)
                if hits is None:
                    hits = _within(_hits(pattern, view), before)
                    shared_hits[pattern.triggers] = hits
                if pattern.scope == "keyed":
                    matches = _keyed_matches(pattern, text, hits)
                else:
                    matches = _line_matches(pattern, text, hits)
            else:
                hits = _hits(pattern, view)
                if pattern.scope in ("keyed", "line"):
                    hits = _within(hits, before)
                    matches = (
                        _keyed_matches(pattern, text, hits)
                        if pattern.scope == "keyed"
                        else _line_matches(pattern, text, hits)
                    )
                else:
                    matches = _token_matches(
                        pattern, text, hits, span_starts, span_ends
                    )
        group = pattern.span_group
        entity_type = pattern.entity_type
        names_a_value = pattern.names_a_value
        for match in matches:
            start, end = match.span(group)
            if start == -1:  # the span group did not take part
                start, end = match.span()
            if end > start:
                candidates.append((start, -end, priority, entity_type, names_a_value))
    return scanned


#: What may follow a provider token inside a value read to whitespace and
#: still be the token: closing punctuation (`token=eyJ…,` / `…eyJ…"}`).
_TRAILING_PUNCTUATION = re.compile(r"""[,;.)\]}'"]*""")


def _merge(candidates: List[_Candidate], text: str) -> List[RedactionSpan]:
    """Leftmost first, the longer first at one start, the earlier pattern
    first at one span; a candidate that overlaps the span before it extends
    that span instead of starting its own. Spans that only touch stay two.

    A keyed value that is a provider token, with only closing punctuation
    after it, is that token: the token's span and type stand, so a JWT is
    ``<JWT_TOKEN_…>`` under any key, as bare (#1980 review, co-reference).
    """
    tokens: Dict[int, List[Tuple[int, int]]] = {}
    for start, negative_end, priority, _entity_type, names_a_value in candidates:
        if not names_a_value:
            tokens.setdefault(start, []).append((-negative_end, priority))
    spans: List[RedactionSpan] = []
    for start, negative_end, priority, entity_type, names_a_value in sorted(candidates):
        end = -negative_end
        if names_a_value and any(
            token_priority < priority
            and token_end <= end
            and _TRAILING_PUNCTUATION.fullmatch(text, token_end, end)
            for token_end, token_priority in tokens.get(start, ())
        ):
            continue
        if spans and start < spans[-1].end:
            if end > spans[-1].end:
                last = spans[-1]
                spans[-1] = RedactionSpan(last.start, end, last.entity_type)
            continue
        spans.append(RedactionSpan(start, end, entity_type))
    return spans


def _masked(text: str, spans: List[RedactionSpan]) -> str:
    """``text`` with each span replaced by a stand-in of the same length,
    shaped as a placeholder is at its edges: ``<XXXX>``."""
    pieces = []
    position = 0
    for span in spans:
        width = span.end - span.start
        pieces.append(text[position : span.start])
        pieces.append("<" + "X" * (width - 2) + ">" if width >= 2 else "X")
        position = span.end
    pieces.append(text[position:])
    return "".join(pieces)


def _lines_of(text: str, spans: List[RedactionSpan]) -> List[Tuple[int, int]]:
    """The whole lines ``spans`` touch, newline included, merged, in order.
    Spans are in order, so each newline search starts where the last line
    ended: the text is read once, however many spans share a line."""
    lines: List[Tuple[int, int]] = []
    for span in spans:
        if lines and span.start < lines[-1][1]:
            if span.end > lines[-1][1]:
                newline = text.find("\n", span.end)
                end = len(text) if newline == -1 else newline + 1
                lines[-1] = (lines[-1][0], end)
            continue
        previous_end = lines[-1][1] if lines else 0
        start = text.rfind("\n", previous_end, span.start) + 1 or previous_end
        newline = text.find("\n", span.end)
        end = len(text) if newline == -1 else newline + 1
        if lines and start <= lines[-1][1]:
            lines[-1] = (lines[-1][0], max(end, lines[-1][1]))
        else:
            lines.append((start, end))
    return lines


def _with_line_before(
    text: str, lines: Optional[List[Tuple[int, int]]]
) -> Optional[List[Tuple[int, int]]]:
    """``lines``, each widened to start at the line before it."""
    if lines is None:
        return None
    return [(text.rfind("\n", 0, max(start - 1, 0)) + 1, end) for start, end in lines]


def _with_line_before_and_after(
    text: str, lines: List[Tuple[int, int]]
) -> List[Tuple[int, int]]:
    """``lines``, each widened to the line before it and the line after it:
    the windows a keyed or line match starting on or before a touched line
    can lie in, when the text has no index-mapped view and is scanned by
    ``finditer`` (its ``endpos`` cuts what a lookahead reads)."""
    windows = []
    for start, end in _with_line_before(text, lines) or ():
        newline = text.find("\n", end)
        windows.append((start, len(text) if newline == -1 else newline + 1))
    return windows


#: Real text settles within three masked rounds (a 175,000-input fuzz); only a
#: crafted chain of glued detections needs more, one per link, and each round
#: rescans the lines it touched. Past this, the lines still growing are
#: redacted whole: it fails closed, and stays linear.
_MAX_MASKED_ROUNDS = 8


def find_redaction_spans(
    text: str, patterns: Sequence[RedactionPattern]
) -> List[RedactionSpan]:
    """Every span of ``text`` that ``patterns`` detect, in order, never
    overlapping.

    Each pattern runs on the raw text, and overlapping matches merge into one
    span, so no character any pattern detected is left in clear and no
    placeholder ever wraps another.

    Then again on the text with every span masked — the same text a second
    redaction pass would read, a placeholder's edges included — until the
    spans stop growing. A match can need its neighbour gone: in
    ``10.0.0.5password=hunter2`` the keyword is glued to the address and is no
    key, but once the address is a ``<placeholder>`` it is one. Without this
    the first pass left ``password=hunter2`` in clear and a second pass took
    it, so redaction was not idempotent (#1980, from the review of #2011).
    The stand-in is placeholder-shaped where a lookaround can read it (``<``
    and ``>``) and nothing can match inside it, as nothing can match inside a
    placeholder. The coverage grows strictly each round. Real text settles in
    three rounds; a crafted chain that would take one round per link is cut
    at ``_MAX_MASKED_ROUNDS``, and each line still growing is then redacted
    whole — closed, and in linear time. Spans that only touch stay two (a
    password glued to an address is not hidden under the address's type).
    """
    if not text:
        return []
    candidates: List[_Candidate] = []
    scanned = _detect(text, patterns, candidates=candidates)
    spans = _merge(candidates, text)
    grew = spans
    for _round in range(_MAX_MASKED_ROUNDS):
        if not spans:
            return spans
        # A stand-in only removes text, so no pattern absent from the text
        # can match the masked text; and it changes what a scan reads only on
        # the lines it sits on. Every span is masked, so a round that adds no
        # candidate found nothing new.
        found = len(candidates)
        _detect(
            _masked(text, spans),
            patterns,
            _lines_of(text, spans),
            candidates=candidates,
            only=scanned,
            spans=spans,
        )
        if len(candidates) == found:
            return spans
        grown = _merge(candidates, text)
        if [(s.start, s.end) for s in grown] == [(s.start, s.end) for s in spans]:
            return grown
        before = {(s.start, s.end) for s in spans}
        grew = [s for s in grown if (s.start, s.end) not in before]
        spans = grown
    ends = [span.end for span in spans]
    for start, end in _lines_of(text, grew):
        stop = end - 1 if text[end - 1 : end] == "\n" else end
        first = spans[bisect.bisect_right(ends, start)]
        candidates.append((start, -stop, -1, first.entity_type, False))
    return _merge(candidates, text)


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
