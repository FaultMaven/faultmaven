"""Title generation's in-process scrub reads a bounded window of each source (#3135).

The scrub (``_scrub_title_source``, ``_has_problem_statement`` and, through the
first, ``_extract_user_signals_from_context``) runs on the event loop. A source
of at most ``TITLE_SOURCE_MAX_CHARS`` is redacted whole, as on ``main``; a
longer one is redacted twice, cut at whitespace to ``TITLE_SOURCE_MAX_CHARS``
and to ``TITLE_SOURCE_REDACT_WINDOW``, and only the prefix the two agree on is
kept. A span the shorter cut splits is whole in the longer read, so the two part
at its start and no fragment of it reaches a title (#1967).

``_sanitize_title_content``'s email pattern no longer rescans a dotted run from
every position in it, and its class no longer admits a literal ``|``.
"""

from __future__ import annotations

import base64
import itertools
import random
import re
from types import SimpleNamespace

import pytest

from faultmaven.modules.case.api.title_generation import (
    TITLE_SOURCE_MAX_CHARS,
    TITLE_SOURCE_REDACT_WINDOW,
    _extract_user_signals_from_context,
    _has_problem_statement,
    _redact_title_source,
    _sanitize_title_content,
    _scrub_title_source,
)
from tests.utils import regex_only_sanitizer
from tests.wallclock import assert_linear_growth

pytestmark = pytest.mark.unit

SANITIZER = regex_only_sanitizer()
STEP = 8

#: ``main``'s email pattern with the ``|`` its class admitted removed.
_MAINS_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


def _main_redaction(text: str) -> str:
    """``main``'s step 1, live: the sanitizer's regex half on the whole source.
    Live rather than frozen, because a placeholder's digest is keyed."""
    return SANITIZER.apply_regex_redaction(text.strip(), {})


def _case(statement: str) -> SimpleNamespace:
    return SimpleNamespace(
        problem_verification=SimpleNamespace(symptom_statement=statement),
        inquiry=None,
    )


class _ReadSpy:
    """The real sanitizer, recording how much of each source it was given."""

    def __init__(self):
        self.reads: list[int] = []

    def apply_regex_redaction(self, text, registry):
        self.reads.append(len(text))
        return SANITIZER.apply_regex_redaction(text, registry)


_rnd = random.Random(3135)


def _token(k: int) -> str:
    return "".join(
        _rnd.choice("abcdefghjkmnpqrstuvwxyzABCDEFGH23456789") for _ in range(k)
    )


def _pem(nbytes: int = 1600) -> str:
    body = base64.b64encode(_rnd.randbytes(nbytes * 3 // 4)).decode()
    lines = "\n".join(body[i : i + 64] for i in range(0, len(body), 64))
    return f"-----BEGIN RSA PRIVATE KEY-----\n{lines}\n-----END RSA PRIVATE KEY-----"


def _filler(n: int) -> str:
    return ("the service restarts under load " * (n // 32 + 1))[:n]


def _secret() -> str:
    return _rnd.choice(
        [
            lambda: _pem(_rnd.choice([400, 900, 1600])),
            lambda: f'password="{_token(7)}! {_token(9)}# {_token(8)}"',
            lambda: f"postgresql://app:{_token(10)}!x@db.internal:5432/x",
            lambda: f"Authorization: Bearer {_token(40)}",
            lambda: f"password = {_token(14)}!",
            lambda: '{"db_password": "%s!", "x": 1}' % _token(14),
            lambda: f"aws_secret_access_key = {_token(12)}/{_token(12)}/{_token(14)}",
            lambda: f"api_key={_token(32)} mail ops.{_token(5)}@example.com at 10.0.0.7",
        ]
    )()


def _document(target: int) -> str:
    words = "the service restarts under load and pods crash with oom errors".split()
    parts, size = [], 0
    rate = _rnd.choice([0.02, 0.1, 0.4])
    while size < target:
        part = (
            _secret()
            if _rnd.random() < rate
            else " ".join(_rnd.choice(words) for _ in range(_rnd.randrange(1, 30)))
        )
        parts.append(part)
        size += len(part) + 1
    return _rnd.choice([" ", "\n"]).join(parts).strip()


# --- Within TITLE_SOURCE_MAX_CHARS: as on main ------------------------------

#: The sources the #1967 title tests scrub, and the seeded sample's.
_EXISTING_SOURCES = [
    "Db login fails with password=hunter2 on the replica",
    "OpenAI calls fail with key sk-" + "a1b2c3d4" * 5 + " in the worker",
    "Uploads fail with AKIAIOSFODNN7EXAMPLE on the batch host",
    "Session dies with eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc123 at login",
    "Checkout charges 4111 1111 1111 1111 twice",
    "Replica on <IP_ADDRESS_1a2b3c4d5e6f7a8b> drops connections",
    "postgresql://app:Winter2024!x@db.internal:5432 is refusing connections",
    "postgresql://db:5432 is down, email me@example.com",
]


def test_a_source_within_the_limit_redacts_as_on_main():
    sources = _EXISTING_SOURCES + [
        _document(_rnd.randrange(50, TITLE_SOURCE_MAX_CHARS)) for _ in range(150)
    ]
    sources += [_filler(TITLE_SOURCE_MAX_CHARS - 1) + " x", "  " + _filler(3990) + "  "]
    for source in sources:
        if len(source.strip()) > TITLE_SOURCE_MAX_CHARS:
            continue
        assert _redact_title_source(source, SANITIZER) == _main_redaction(source)


# --- Above it: a prefix of main's redaction, never a fragment ---------------


def test_the_scrub_reads_at_most_the_window_of_any_source():
    spy = _ReadSpy()
    statement = "disk is full " + _filler(2_000_000)
    _scrub_title_source(statement, spy)
    _has_problem_statement(_case(statement), spy)
    _extract_user_signals_from_context(f"1. [10:00] User: {statement}", spy)
    assert spy.reads and max(spy.reads) <= TITLE_SOURCE_REDACT_WINDOW


def test_a_long_source_keeps_a_prefix_of_mains_redaction_on_whole_tokens():
    for _ in range(120):
        raw = _document(_rnd.choice([5000, 9000, 14000]))
        full = _main_redaction(raw)
        kept = _redact_title_source(raw, SANITIZER)
        assert full.startswith(kept)
        assert len(kept) == len(full) or not kept or full[len(kept)].isspace()


def test_a_secret_past_the_window_never_reaches_a_title_source():
    raw = _filler(TITLE_SOURCE_REDACT_WINDOW + 100) + " password=PASTWINDOW9x!"
    assert "PASTWINDOW" not in _scrub_title_source(raw, SANITIZER)


@pytest.mark.parametrize(
    "secret, needles",
    [
        pytest.param(_pem(), None, id="pem-private-key"),
        pytest.param(
            "postgresql://app:Winter2024!xSTRADDLE@db.internal:5432/x",
            ["Winter2024", "STRADDLE"],
            id="credentialed-database-url",
        ),
    ],
)
def test_a_secret_straddling_the_kept_prefix_is_redacted_not_fragmented(
    secret, needles
):
    needles = needles or [ln for ln in secret.splitlines()[1:-1] if len(ln) >= 16]
    for offset in range(1, len(secret), max(1, len(secret) // 9)):
        head = _filler(TITLE_SOURCE_MAX_CHARS - offset - 1)
        raw = f"{head} {secret} {_filler(5000)}"
        out = _scrub_title_source(raw, SANITIZER)
        assert _main_redaction(raw).startswith(_redact_title_source(raw, SANITIZER))
        assert not [n for n in needles if n in out], offset


def test_a_secret_straddling_the_window_after_shrinkage_stays_redacted():
    """The critique's case: three PEM keys up front shrink the redacted text by
    about 5,000 characters, and a quoted password straddles raw offset 8,000.
    A cut measured on the redacted text emitted ``TAILSECRET9x!`` in clear."""
    secret = 'password="correct horse TAILSECRET9x! battery staple"'
    head = f"disk full on node a. {_pem()} \n{_pem()} \n{_pem()} \n"
    for offset in range(0, len(secret), 4):
        pad = TITLE_SOURCE_REDACT_WINDOW - offset - len(head) - 1
        raw = f"{head}{_filler(pad)} {secret} {_filler(3000)}"
        assert raw[TITLE_SOURCE_REDACT_WINDOW - offset :].startswith(secret)
        out = _scrub_title_source(raw, SANITIZER)
        assert "TAILSECRET" not in out and "correct horse" not in out, offset


def test_a_long_statement_of_secrets_reads_as_no_statement():
    """Fails closed above the limit: the first 4,000 characters all secrets
    (``main`` counted the words after them)."""
    keys = " ".join("sk-" + _token(40) for _ in range(TITLE_SOURCE_MAX_CHARS // 40))
    assert not _has_problem_statement(_case(f"{keys} the database is down"), SANITIZER)


# --- The email pattern ------------------------------------------------------


def _mains_email_marks(text: str) -> str:
    return _MAINS_EMAIL.sub("[email]", text).strip()


def test_the_email_marks_equal_mains_without_the_pipe_exhaustively():
    """Every short string of the email alphabet, with a domain-shaped suffix.
    The alphabet holds one digit and no ``/``, ``:`` or ``\\``, so no other
    pattern of ``_sanitize_title_content`` can match: the output is the email
    substitution's alone."""
    alphabet = "a9._%+-@ |é"
    differing = [
        text
        for length in range(1, 6)
        for chars in itertools.product(alphabet, repeat=length)
        if "@" in chars
        for text in ("".join(chars) + suffix for suffix in ("", "c", ".cc", "@b.cc"))
        if _sanitize_title_content(text) != _mains_email_marks(text)
    ]
    assert differing == []


def test_the_email_marks_equal_mains_without_the_pipe_on_a_sample():
    rnd = random.Random(5)
    alphabet = list("abc9._%+-@@ |é\n_XY")
    suffixes = [".cc", "@b.cc", ".com x", "@a.bc.", "@x.yy.z@q.rr"]
    differing = []
    for _ in range(20_000):
        text = "".join(rnd.choice(alphabet) for _ in range(rnd.randrange(3, 40)))
        if rnd.random() < 0.6:
            text += rnd.choice(suffixes)
        if _sanitize_title_content(text) != _mains_email_marks(text):
            differing.append(text)
    assert differing == []


def test_adjacent_emails_are_each_marked():
    assert _sanitize_title_content("a@b.cc.d@e.ff") == "[email][email]"


def test_a_pipe_is_not_part_of_a_top_level_domain():
    """``main``'s class ``[A-Z|a-z]`` admitted a literal ``|``."""
    assert _sanitize_title_content("me@host.c|m") == "me@host.c|m"


# --- Growth -----------------------------------------------------------------

_DOTTED = "a."  # super-linear through main's scrub
_GUARDS = ("a@", "a_")  # already linear on main: guards, not reproductions


def _run(shape: str, n: int) -> str:
    return shape * (n // len(shape))


def _among_words(n: int) -> str:
    """The issue's shape: a long dotted token among a few words."""
    return f"disk is full {_run(_DOTTED, n)} on node b c d"


_BARE = [pytest.param(shape, id=repr(shape)) for shape in (_DOTTED, *_GUARDS)]
_PATHS = {
    "_scrub_title_source": lambda text: _scrub_title_source(text, SANITIZER),
    "_has_problem_statement": lambda text: _has_problem_statement(
        _case(text), SANITIZER
    ),
}


@pytest.mark.performance
@pytest.mark.parametrize("path", _PATHS)
@pytest.mark.parametrize("shape", _BARE)
def test_the_scrub_is_linear_up_to_the_window(path, shape):
    """A run with no whitespace is read to the window (hard cuts at 4,000 and
    8,000). ``main``'s scrub is super-linear on ``"a."``."""
    assert_linear_growth(
        _PATHS[path],
        lambda n: _run(shape, n),
        small=TITLE_SOURCE_REDACT_WINDOW // STEP**2,
        max_escalations=0,
        undecided_repeats=2,
        label=f"{path} over {shape!r}",
    )


#: The largest dotted run whose source (with its words) is read whole: above
#: TITLE_SOURCE_MAX_CHARS the whitespace cut drops the token.
_AMONG_WORDS_TOP = (TITLE_SOURCE_MAX_CHARS - 32) // STEP**2


@pytest.mark.performance
@pytest.mark.parametrize(
    "path",
    [
        *_PATHS.values(),
        lambda text: _extract_user_signals_from_context(
            f"1. [10:00] User: {text}", SANITIZER
        ),
    ],
    ids=[*_PATHS, "_extract_user_signals_from_context"],
)
def test_a_long_token_among_words_is_linear_up_to_the_limit(path):
    """The issue's shape, super-linear through every path on ``main``."""
    assert_linear_growth(
        path,
        _among_words,
        small=_AMONG_WORDS_TOP,
        max_escalations=0,
        undecided_repeats=2,
        label="a long token among words",
    )
