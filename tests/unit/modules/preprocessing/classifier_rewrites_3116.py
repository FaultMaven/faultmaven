"""The Tier 0 classifier's rewritten patterns, as ``main`` had them (#3116 B2).

``DataClassifier.classify`` runs synchronously on the event loop over the first
5,000 characters of an upload or a paste. Principle 14 lets it stay there only
if its cost is linear in that sample. On ``main`` (``a2c02e8df``) twelve of its
patterns rescanned a run from every position inside it, or (iostat's) from
every pair and triple of its header words: one long token (a base64 blob, a
minified line, a hex dump) or a run of blank lines held the loop for up to
1.6 s, a line of repeated iostat words for 4 s. Each is rewritten to cost time linear in its input and
to answer as before: the same yes/no, or, for the prose pattern, the same
``findall`` count.

``REWRITES`` holds each pattern as ``main`` had it and as it is now, with the
flags its site passes and the input bound it runs under. The rule path
lowercases its sample after the cut, and ``İ`` lowercases to two code points,
so its 5,000 characters reach 10,000 code points; the email runs only behind
the short-text gate's 1,500 characters, so at most 3,000; the config hint reads
its 5,000 characters as written.
"""

from __future__ import annotations

import re
from typing import Callable, NamedTuple

from faultmaven.modules.preprocessing.classifier import DataClassifier

M, I = re.MULTILINE, re.IGNORECASE
SAMPLE = 5_000
#: The bounds the patterns run under (see above).
LOWERED = 2 * SAMPLE
SHORT_LOWERED = 2 * DataClassifier._SHORT_TEXT_MAX_CHARS


class Rewrite(NamedTuple):
    main: str
    now: str
    flags: int
    #: The input bound the site runs it under.
    limit: int
    #: ``(head, unit)``: ``head`` then ``unit`` repeated. The first is a shape
    #: ``main``'s form is quadratic on.
    shapes: tuple[tuple[str, str], ...]
    #: Each ``(alphabet, longest)``: every string of its symbols up to
    #: ``longest`` of them is compared with ``main``'s answer.
    alphabets: tuple[tuple[tuple[str, ...], int], ...]
    #: ``findall``'s count is read, not a yes/no.
    count: bool = False


#: A run of blank lines.
NL = ("a", "\n")
CALL = ("a", "(", ")", "1", " ", "\n", " = ")
REWRITES = {
    "ps (COMMAND_OUTPUTS)": Rewrite(
        r"^\s*[\w\-]+\s+\d+\s+[\d\.]+\s+[\d\.]+",
        r"^[^\S\n]*[\w\-]+\s+\d+\s+[\d\.]+\s+[\d\.]+",
        M | I,
        LOWERED,
        (NL, ("", "a 1 ")),
        ((("a", "1", ".", " ", "\t", "\n", "\r"), 7),),
    ),
    "strace (COMMAND_OUTPUTS)": Rewrite(
        r"\w+\([^)]*\)\s+=\s+-?\d+",
        r"(?<![^)])(?>[^)]*?\w\()[^)]*+\)\s+=\s+-?\d+",
        M | I,
        LOWERED,
        (("", "a("), ("", "a"), ("", "a() = ")),
        ((CALL, 7),),
    ),
    "ltrace (COMMAND_OUTPUTS)": Rewrite(
        r"\w+\([^)]*\)\s+=\s+\w+",
        r"(?<![^)])(?>[^)]*?\w\()[^)]*+\)\s+=\s+\w+",
        M | I,
        LOWERED,
        (("", "a("), ("", "1"), ("", "a() =")),
        ((CALL, 7),),
    ),
    "config hint (_validate_hint)": Rewrite(
        r"[a-z_]+\s*[:=]",
        r"[a-z_]\s*[:=]",
        M,
        SAMPLE,  # _validate_hint reads content[:5000], not lowercased
        (("", "a"), ("", "a "), ("", "_")),
        ((("a", "_", "A", " ", "\n", ":", "="), 7),),
    ),
    "Prometheus metric": Rewrite(
        r'\w+{[\w="]+}',
        r'\w{[\w="]+}',
        M,
        LOWERED,
        (("", "a_"), ("", "a{"), ("", 'a{"=')),
        ((("a", "{", "}", "=", '"', " ", "-"), 7),),
    ),
    "code modifier": Rewrite(
        r"^\s*(public|private|protected)\s+(class|interface|enum)",
        r"^[^\S\n]*(public|private|protected)\s+(class|interface|enum)",
        M,
        LOWERED,
        (NL, ("", " \n")),
        ((("public", "class", " ", "\n", "\t", "\r", "x"), 7),),
    ),
    "prose sentence (counted)": Rewrite(
        r"[A-Z][a-z]+\s+[a-z]+\s+[a-z]+",
        r"(?<![a-z])[A-Z][a-z]+\s+[a-z]+\s+[a-z]+",
        I,
        LOWERED,
        (("", "a"), ("", "ab "), ("", "a\n")),
        (
            (("a", "b", " ", "\n", "1", "."), 8),
            # Long s, dotless i and the Kelvin sign: [a-z] under IGNORECASE.
            (("a", "A", " ", "ſ", "ı", "K", "_"), 7),
        ),
        count=True,
    ),
    "email (short-text gate)": Rewrite(
        r"\b[\w._%+-]+@[\w.-]+\.[A-Za-z]{2,}\b",
        r"(?<![\w._%+-])[.%+-]*+\w[\w._%+-]*+@[\w.-]+\.[A-Za-z]{2,}\b",
        0,
        SHORT_LOWERED,
        (("", "a."), ("a@", "a."), ("", "a.a@"), ("", "_")),
        (
            (("a", "aa", ".", "%", "@", "_", "-"), 7),
            (("a", "aa", ".", "@", "_", "é", "\n"), 7),
        ),
    ),
    "trace operationName": Rewrite(
        r'"operationName".*"spans":\s*\[',
        r'(?m)^(?>.*?"operationName").*"spans":\s*\[',
        I,
        LOWERED,
        (("", '"operationname"'), NL),
        ((('"operationName"', '"spans":', "[", " ", "\n", "x", '"'), 7),),
    ),
    "iostat (COMMAND_OUTPUTS)": Rewrite(
        r"Device.*tps.*kB_read/s.*kB_wrtn/s",
        r"^(?>.*?Device)(?>.*?tps)(?>.*?kB_read/s).*kB_wrtn/s",
        M | I,
        LOWERED,
        (("", "devicetps"), ("", "devicetpskb_read/s"), NL),
        ((("Device", "tps", "kB_read/s", "kB_wrtn/s", " ", "\n", "\r"), 7),),
    ),
    "flame graph": Rewrite(
        r"[\w\.]+(?:;[\w\.]+)+\s+\d+",
        r"[\w.];[\w.]+\s+\d+",
        I,
        LOWERED,
        (("", "a;"), ("", "a.;"), ("", "a"), ("", "a;a x")),
        ((("a", "1", ".", ";", " ", "\n"), 8),),
    ),
    "profiling summary": Rewrite(
        r"\d+\s+calls?\s+in\s+[\d\.]+\s+seconds",
        r"\d\s+calls?\s+in\s+[\d\.]+\s+seconds",
        I,
        LOWERED,
        (("", "1"), ("", "1 calls in ")),
        ((("1", ".", " ", "\n", "calls", " in ", "seconds"), 7),),
    ),
}

#: Strings named in the design critique, each a place a plausible rewrite
#: answered differently from ``main``.
NAMED = [
    "%x@a.com",
    "..@a.com",
    "_@a.aa",
    "x%@a.com",
    "a;b 1",
    "a(( ) = 1",
    "ſ",
    "ı",
]


def payload(shape: tuple[str, str], n: int) -> str:
    head, unit = shape
    return head + (unit * (n // len(unit) + 1))[: n - len(head)]


def answer(rewrite: Rewrite, form: str) -> Callable[[str], object]:
    """The rewrite's reading of a text under ``form``: yes/no, or the count."""
    rx = re.compile(form, rewrite.flags)
    if rewrite.count:
        return lambda text: len(rx.findall(text))
    return lambda text: rx.search(text) is not None
