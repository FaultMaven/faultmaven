"""Title generation's in-process scrub reads a bounded window of each source (#3135).

The scrub (``_scrub_title_source``, ``_has_problem_statement`` and, through the
first, ``_extract_user_signals_from_context``) runs on the event loop. A source
of at most ``TITLE_SOURCE_MAX_CHARS`` is redacted whole, as on ``main``. A
longer one is redacted twice, its first ``TITLE_SOURCE_MAX_CHARS`` characters
with the token the cut splits dropped and its first
``TITLE_SOURCE_REDACT_WINDOW`` (a hard cut), and only the prefix the two agree
on is kept. A span the shorter read splits is whole in the longer one when it
ends within the window, so the two part at its start and no fragment of it
reaches a title (#1967). A case whose scrubbed substance is empty is refused,
so no prompt is built from its raw context.

``_sanitize_title_content``'s email pattern no longer rescans a dotted run from
every position in it, and its class no longer admits a literal ``|``. Its
parenthesized-phone pattern no longer needs a word character before the ``(``.
Both are intended differences from ``main``.
"""

from __future__ import annotations

import base64
import itertools
import random
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.modules.case.api.title_generation import (
    TITLE_SOURCE_MAX_CHARS,
    TITLE_SOURCE_REDACT_WINDOW,
    _extract_user_signals_from_context,
    _generate_and_persist_title,
    _has_problem_statement,
    _redact_title_source,
    _sanitize_title_content,
    _scrub_title_source,
    _TitleSubstanceTooThin,
)
from faultmaven.modules.case.contracts import CaseState
from tests.case_builders import make_case
from tests.utils import regex_only_sanitizer
from tests.wallclock import assert_linear_growth

pytestmark = pytest.mark.unit

SANITIZER = regex_only_sanitizer()
STEP = 8
LIMIT = TITLE_SOURCE_MAX_CHARS

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


def _token(rnd: random.Random, k: int) -> str:
    return "".join(
        rnd.choice("abcdefghjkmnpqrstuvwxyzABCDEFGH23456789") for _ in range(k)
    )


def _base64(rnd: random.Random, nchars: int) -> str:
    """A whitespace-free run, as a certificate or blob field carries."""
    return base64.b64encode(rnd.randbytes(nchars)).decode()[:nchars]


def _pem_lines(rnd: random.Random, nbytes: int) -> list[str]:
    body = base64.b64encode(rnd.randbytes(nbytes * 3 // 4)).decode()
    return [body[i : i + 64] for i in range(0, len(body), 64)]


def _pem(rnd: random.Random, nbytes: int = 1600, newline: str = "\n") -> str:
    lines = newline.join(_pem_lines(rnd, nbytes))
    return (
        f"-----BEGIN RSA PRIVATE KEY-----{newline}{lines}"
        f"{newline}-----END RSA PRIVATE KEY-----"
    )


def _filler(n: int) -> str:
    return ("the service restarts under load " * (n // 32 + 1))[:n]


def _secret(rnd: random.Random) -> str:
    t = lambda k: _token(rnd, k)  # noqa: E731
    return rnd.choice(
        [
            lambda: _pem(rnd, rnd.choice([400, 900, 1600])),
            lambda: f'password="{t(7)}! {t(9)}# {t(8)}"',
            lambda: f"postgresql://app:{t(10)}!x@db.internal:5432/x",
            lambda: f"Authorization: Bearer {t(40)}",
            lambda: f"password = {t(14)}!",
            lambda: '{"db_password": "%s!", "x": 1}' % t(14),
            lambda: f"aws_secret_access_key = {t(12)}/{t(12)}/{t(14)}",
            lambda: f"api_key={t(32)} mail ops.{t(5)}@example.com at 10.0.0.7",
        ]
    )()


def _document(rnd: random.Random, target: int) -> str:
    words = "the service restarts under load and pods crash with oom errors".split()
    parts, size = [], 0
    rate = rnd.choice([0.02, 0.1, 0.4])
    while size < target:
        part = (
            _secret(rnd)
            if rnd.random() < rate
            else " ".join(rnd.choice(words) for _ in range(rnd.randrange(1, 30)))
        )
        parts.append(part)
        size += len(part) + 1
    return rnd.choice([" ", "\n"]).join(parts).strip()


def _straddling(secret: str, after: str, *, head: str = "", offsets=None) -> list:
    """``secret`` placed so the cut at ``LIMIT`` falls ``offset`` characters
    into it (every character by default), ``after`` following it."""
    return [
        f"{head}{_filler(LIMIT - offset - len(head) - 1)} {secret}{after}"
        for offset in offsets or range(1, len(secret))
    ]


def _leaked(raws: list, needles: list) -> list:
    return [
        (i, needle)
        for i, raw in enumerate(raws)
        for needle in needles
        if needle in _scrub_title_source(raw, SANITIZER)
    ]


# --- Within TITLE_SOURCE_MAX_CHARS: as on main ------------------------------

#: The sources the #1967 title tests scrub.
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
    rnd = random.Random(3135)
    sources = _EXISTING_SOURCES + [
        _document(rnd, rnd.randrange(50, LIMIT))[:LIMIT] for _ in range(150)
    ]
    sources.append(_document(rnd, 2 * LIMIT)[:LIMIT])
    sources.append("  " + _filler(LIMIT - 1) + "x  ")
    assert max(len(s.strip()) for s in sources) == LIMIT
    for source in sources:
        assert _redact_title_source(source, SANITIZER) == _main_redaction(source)


def test_the_limit_is_4000_characters_read_whole():
    """At 4,000 a source is redacted whole, its last token kept with its secret
    marked; at 4,001 the cut splits that token and it is dropped."""
    tail = "key=sk-" + "a1b2c3d4" * 5
    at_limit = _filler(LIMIT - len(tail) - 1) + " " + tail
    assert len(at_limit) == LIMIT
    whole = _redact_title_source(at_limit, SANITIZER)
    assert whole == _main_redaction(at_limit) and whole.startswith(at_limit[:3900])
    assert "a1b2c3d4" not in whole and whole.endswith(">")
    over = _redact_title_source(at_limit + "9", SANITIZER)
    assert over == at_limit[: -len(tail) - 1]


# --- Above it: a prefix of main's redaction, never a fragment ---------------


def test_the_scrub_reads_at_most_the_window_of_any_source():
    spy = _ReadSpy()
    statement = "disk is full " + _filler(2_000_000)
    _scrub_title_source(statement, spy)
    _has_problem_statement(_case(statement), spy)
    _extract_user_signals_from_context(f"1. [10:00] User: {statement}", spy)
    assert spy.reads and max(spy.reads) <= TITLE_SOURCE_REDACT_WINDOW


def test_a_long_source_keeps_a_prefix_of_mains_redaction_on_whole_tokens():
    rnd = random.Random(3136)
    for _ in range(120):
        raw = _document(rnd, rnd.choice([5000, 9000, 14000]))
        full = _main_redaction(raw)
        kept = _redact_title_source(raw, SANITIZER)
        assert full.startswith(kept)
        assert len(kept) == len(full) or not kept or full[len(kept)].isspace()


def test_a_secret_past_the_window_never_reaches_a_title_source():
    """A guard, not a reproduction: ``main`` redacts it too. It fails if a read
    ever reaches past the window and leaves what it finds there unredacted."""
    raw = _filler(TITLE_SOURCE_REDACT_WINDOW + 100) + " password=PASTWINDOW9x!"
    assert "PASTWINDOW" not in _scrub_title_source(raw, SANITIZER)


@pytest.mark.parametrize("kind", ["pem-private-key", "credentialed-database-url"])
def test_a_secret_straddling_the_kept_prefix_is_redacted_not_fragmented(kind):
    if kind == "pem-private-key":
        secret = _pem(random.Random(1))
        needles = [ln for ln in secret.splitlines()[1:-1] if len(ln) >= 16]
    else:
        secret = "postgresql://app:Winter2024!xSTRADDLE@db.internal:5432/x"
        needles = ["Winter2024", "STRADDLE"]
    step = max(1, len(secret) // 9)
    raws = _straddling(secret, " " + _filler(5000), offsets=range(1, len(secret), step))
    for raw in raws:
        assert _main_redaction(raw).startswith(_redact_title_source(raw, SANITIZER))
    assert _leaked(raws, needles) == []


def test_a_secret_straddling_the_cut_before_a_run_with_no_whitespace():
    """The long read is a hard cut: a whitespace cut there landed inside the
    same span as the short read's when nothing after it held whitespace before
    8,000 (minified JSON, a base64 field), and both kept its first words."""
    rnd = random.Random(2)
    pair = '"password":"correct horse battery staple"'
    raws = _straddling(pair, ',"cert":"' + _base64(rnd, 7000) + '"}')
    assert _leaked(raws, ["horse", "battery", "staple"]) == []
    pem = _pem(rnd, 4200, newline="\\n")  # JSON-escaped, longer than the window
    raws = _straddling('{"key":"' + pem + '"}', "", offsets=range(1, 45))
    assert _leaked(raws, ["BEGIN RSA"]) == []


def test_a_secret_straddling_the_cut_after_shrinkage_stays_redacted():
    """Two PEM keys up front shrink the redacted text by about 2,400
    characters. A cut measured on the redacted text (the rejected two-cut rule)
    and a whitespace cut for the long read both left ``horse`` in clear."""
    rnd = random.Random(4)
    head = f"disk full on node a. {_pem(rnd, 900)} \n{_pem(rnd, 900)} \n"
    pair = '"password":"correct horse battery staple"'
    tail = ',"cert":"' + _base64(rnd, 9000) + '"}'
    raws = _straddling(pair, tail, head=head)
    assert _leaked(raws, ["horse", "battery", "staple"]) == []


@pytest.mark.parametrize(
    "value, needles",
    [
        pytest.param('"owner":"ops.team@example.com",', ["ops.team"], id="email"),
        pytest.param('"tel":"555-123-4567",', ["555-123", "123-45"], id="phone"),
    ],
)
def test_a_cut_through_whitespace_free_text_leaves_no_token_part(value, needles):
    """The short read drops the token its cut splits, whole: the step-3
    patterns (email, phone) see only what is kept, and a part of either is
    not theirs to match. Minified JSON holds no whitespace at all."""
    record = '{"svc":"api","node":"n1","state":"down"},'
    run = (record * (3 * LIMIT // len(record)))[: 2 * LIMIT]
    raws = [run[: LIMIT - offset] + value + run for offset in range(1, 22)]
    assert _leaked(raws, needles) == []


def test_a_cut_through_unicode_whitespace_lands_on_it():
    """Pure no-break-space prose: the short read is cut back to the no-break
    space before the address (``str.isspace``), not emptied for want of an
    ASCII space, and no part of the address is kept."""
    prose = "the\u00a0service\u00a0restarts\u00a0under\u00a0load\u00a0" * 400
    for offset in range(1, 20):
        head = prose[: LIMIT - offset - 1]
        raw = f"{head}\u00a0ops.team@example.com\u00a0{prose}"
        assert _redact_title_source(raw, SANITIZER) == head, offset
        assert "ops.team" not in _scrub_title_source(raw, SANITIZER), offset


def test_a_message_is_scrubbed_whole_never_cut_before_redaction():
    """A message is not cut raw: a cut inside its secret, before the scrub,
    left the long read the same unclosed span the short read saw (#1967)."""
    rnd = random.Random(5)
    pair = '"password":"correct horse battery staple"'
    tail = ',"cert":"' + _base64(rnd, 6000) + '"}'
    for message in _straddling(
        pair, tail, head="pods crash on node ", offsets=range(1, 41, 2)
    ):
        context = (
            f"1. [10:00] User: {message}\n"
            "2. [10:01] User: the pods still crash on node b"
        )
        signals = _extract_user_signals_from_context(context, SANITIZER)
        assert not [n for n in ("horse", "battery", "staple") if n in signals]


def test_a_long_statement_of_secrets_reads_as_no_statement():
    """Fails closed above the limit: the first 4,000 characters all secrets
    (``main`` counted the words after them)."""
    rnd = random.Random(6)
    keys = " ".join("sk-" + _token(rnd, 40) for _ in range(LIMIT // 40))
    assert not _has_problem_statement(_case(f"{keys} the database is down"), SANITIZER)


async def test_an_empty_substance_builds_no_prompt_from_the_raw_context():
    """The statement counts (its words follow the secret), but the substance
    is empty: its two reads part at the secret's start, the quote closing only
    in the chat. The prompt used to fall back to the raw context, secrets in."""
    words = "the replica rejects the login after the upgrade and the pool drains "
    case = make_case(
        title="Case-260101-1",
        description="",
        user_id="user_123",
        enterprise_id="org_3135",
        state=CaseState.INQUIRY,
    )
    case.inquiry.proposed_problem_statement = (
        '"password":"Hunter2Secret ' + (words * 50)[:3000].strip()
    )
    token = "ghp_" + "a1B2c3D4e5" * 3 + "abcdef"
    context = (
        f'1. [10:00] User: {(words * 30)[:1500].strip()}" with {token} '
        "and Hunter2Secret on the replica"
    )
    assert _has_problem_statement(case, SANITIZER)
    service = AsyncMock()
    service.get_case_conversation_context = AsyncMock(return_value=context)
    model = MagicMock()
    model.route = AsyncMock()
    with pytest.raises(_TitleSubstanceTooThin):
        await _generate_and_persist_title(
            case=case,
            case_id=case.case_id,
            user_id="user_123",
            case_service=service,
            llm_provider=model,
            max_words=8,
            hint=None,
            sanitizer=SANITIZER,
            redis_client=None,
        )
    model.route.assert_not_awaited()


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


@pytest.mark.parametrize(
    "text, marked",
    [
        ("call (555) 123-4567 now", "call [phone] now"),
        ('{"tel":"(555)123-4567"}', '{"tel":"[phone]"}'),
        ("x(555) 123-4567", "x[phone]"),
    ],
)
def test_a_parenthesized_phone_is_marked_after_any_character(text, marked):
    """``main``'s pattern began ``\\b\\(`` and so needed a word character
    before the ``(``: the first two came back unchanged."""
    assert _sanitize_title_content(text) == marked


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
def test_the_scrub_is_linear_up_to_the_limit(path, shape):
    """A run with no whitespace is read whole up to the limit; above it the
    short read drops it (one token). ``main``'s scrub is super-linear on
    ``"a."``."""
    assert_linear_growth(
        _PATHS[path],
        lambda n: _run(shape, n),
        small=LIMIT // STEP**2,
        max_escalations=0,
        undecided_repeats=2,
        label=f"{path} over {shape!r}",
    )


@pytest.mark.performance
@pytest.mark.parametrize("path", _PATHS)
def test_the_scrub_is_linear_up_to_the_window(path):
    """A guard: dotted runs between spaces, read by both reads up to 8,000."""
    assert_linear_growth(
        _PATHS[path],
        lambda n: _run(_run(_DOTTED, 98) + " ", n),
        small=TITLE_SOURCE_REDACT_WINDOW // STEP**2,
        max_escalations=0,
        undecided_repeats=2,
        label=f"{path} over dotted runs",
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
