"""The shared port patterns, against oracles, in linear time (fm#2641, fm#2677).

The host-and-port pattern's oracle is ``_reference``, a scanner written without
a regex. The port keyword pattern's is the old pattern that three modules used
to define for themselves, less its non-ASCII ports. On a long run of word
characters with no colon, the pattern before fm#2641 tried every split between
its two classes, so its cost was quadratic in the run: 23 s for a 64 KB hex run.
"""

from __future__ import annotations

import random
import re
from collections import Counter

import pytest

from faultmaven.modules.case.contracts import EntityType
from faultmaven.modules.preprocessing.entities.logs import LogsEntityExtractor
from faultmaven.modules.preprocessing.extractors.logs_extractor import (
    LogsAndErrorsExtractor,
)
from faultmaven.modules.preprocessing.extractors.utils import (
    HOST_PORT_RE,
    PORT_KEYWORD_RE,
    distinct_values,
    is_port,
)
from tests.unit.modules.preprocessing.extractors.test_host_port_shapes_2677 import (
    NEW_READINGS,
    NON_ASCII,
    NOT_PORTS,
    REMAINING,
    UNCHANGED,
)
from tests.unit.modules.preprocessing.extractors.test_sshd_auth_anchoring import (
    ADVERSARIAL_SHAPES,
    SETTLE_PER_LINE_BYTE,
    adversarial_length,
    adversarial_line,
)
from tests.wallclock import assert_linear_growth

OLD_PORT_KEYWORD_RE = re.compile(r"\bport[= :]+(\d{1,5})\b", re.IGNORECASE)

#: Negatives first: lines with no colon-and-port at all (``""``, ``12345``, a
#: run of ``a``, ``1-2-3:``), an empty, punctuation-only or digits-only host, a
#: non-ASCII host, and a port that is too long or runs on into a word.
NEGATIVES = ["", "12345", "a" * 50, "1-2-3:", ":80", "-:80", "_:80", "9:80"]
NEGATIVES += ["é:80", "host:123456", "a:80x", "a:80_", "04:47:44"]
#: Then the shapes that match, and fm#2677's: a port in non-ASCII digits and a
#: version tag (``a:8.0`` gives nothing, as ``nginx:1.25`` does).
EDGE_CASES = NEGATIVES + ["1.2:80", "x.y-z:65535 b:1", "db.local:5432", "-a:1"]
EDGE_CASES += ["h:٣٣", "a:8.0", *NOT_PORTS, *NON_ASCII, *UNCHANGED]
EDGE_CASES += [*NEW_READINGS, *REMAINING]

#: The keyword in any case, after ``= :`` only, with a port of up to five
#: ASCII digits; not inside a longer word, after another separator, or before
#: a port that is too long.
KEYWORD_CASES = {"Port 22": ["22"], "PORT=80": ["80"], "port: 80": ["80"]}
KEYWORD_CASES |= {"port:80": ["80"], "port 65535": ["65535"], "port ٣٣": []}
KEYWORD_CASES |= {"port 123456": [], "sport 22": [], "ports 22": []}
KEYWORD_CASES |= {"port\t22": [], "port, 22": [], "port-22": [], "port #22": []}


def _is_word(char: str) -> bool:
    """Python's ``\\w``: a letter or digit in any script, or ``_``."""
    return char.isalnum() or char == "_"


def _ends_in_a_date_hour(run: str) -> bool:
    """``…15T06`` or ``2024T06``: two date digits and a separator, or four."""

    def digits(text: str) -> bool:
        return all(char.isdecimal() for char in text)

    tail, short = run[-8:], run[-7:]
    if len(tail) == 8 and tail[2] in "-/." and tail[5] in "Tt":
        if digits(tail[:2] + tail[3:5] + tail[6:]):
            return True
    return len(short) == 7 and short[4] in "Tt" and digits(short[:4] + short[5:])


def _reference(line: str, fired: Counter | None = None) -> list:
    """``HOST_PORT_RE``'s matches, scanning maximal runs without a regex.

    A run with an ASCII letter or dot, ``:`` and one to five ASCII digits is a
    port, unless the run ends in a date's hour or the digits run on into a word
    character, or into ``.`` or ``:`` and an ASCII digit. A rejected run lets
    the scan go on to the next run, inside text a match would have consumed; a
    match skips the rest of the run its port is in. ``fired`` counts each
    rule's rejections.
    """
    fired = Counter() if fired is None else fired
    found, i, n = [], 0, len(line)
    while i < n:
        if not (_is_word(line[i]) or line[i] in ".-"):
            i += 1
            continue
        start = i
        while i < n and (_is_word(line[i]) or line[i] in ".-"):
            i += 1
        run, j = line[start:i], i + 1
        if line[i : i + 1] != ":" or not any(
            char == "." or (char.isascii() and char.isalpha()) for char in run
        ):
            continue
        while j < n and line[j] in "0123456789":
            j += 1
        port, after = line[i + 1 : j], line[j : j + 2]
        if not port:
            fired["non-ascii"] += line[i + 1 : i + 2].isdecimal()
        elif _ends_in_a_date_hour(run):
            fired["date-hour"] += 1
        elif len(port) > 5:
            pass
        elif after[:1] and _is_word(after[0]):
            fired["non-ascii"] += after[0].isdecimal()
        elif len(after) == 2 and after[0] in ".:" and after[1] in "0123456789":
            fired["dot-colon-digit"] += 1
        else:
            found.append(((start, j), (port,)))
            i = j
            while i < n and (_is_word(line[i]) or line[i] in ".-"):
                i += 1
    return found


def _corpus(count: int) -> list[str]:
    """Random lines; the ``port`` tokens reach the keyword pattern too."""
    rng = random.Random(2641)
    tokens = [*"aZ9_-.:é 1/x\t٣５=", "port", "PORT", "ports"]
    tokens += ["T", "t", "06", "2024", "T06", "-15", ":3", ".4"]
    return [
        "".join(rng.choice(tokens) for _ in range(rng.randint(0, 24)))
        for _ in range(count)
    ]


def _matches(pattern: re.Pattern[str], text: str) -> list:
    return [(m.span(), m.groups()) for m in pattern.finditer(text)]


def _ascii_keyword_matches(text: str) -> list:
    """The old keyword pattern's matches whose port is ASCII digits."""
    old = _matches(OLD_PORT_KEYWORD_RE, text)
    return [(span, groups) for span, groups in old if groups[0].isascii()]


def _expected_ports(lines: list[str]) -> Counter:
    """Port counts by the oracles, one mention per line."""
    return Counter(
        port
        for line in lines
        for port in distinct_values(
            OLD_PORT_KEYWORD_RE.findall(line)
            + [groups[0] for _, groups in _reference(line)]
        )
        if is_port(port)
    )


@pytest.mark.unit
class TestSameMatchesAsTheOracles:
    def test_edge_cases(self):
        for text in EDGE_CASES:
            assert _matches(HOST_PORT_RE, text) == _reference(text), text
        assert not any(HOST_PORT_RE.search(text) for text in NEGATIVES)
        assert HOST_PORT_RE.findall("a:8.0") == []
        assert HOST_PORT_RE.findall("x.y-z:65535 b:1") == ["65535", "1"]
        assert HOST_PORT_RE.findall("h:٣٣") == []

    def test_the_port_keyword(self):
        for text, ports in KEYWORD_CASES.items():
            assert PORT_KEYWORD_RE.findall(text) == ports, text
            assert _matches(PORT_KEYWORD_RE, text) == _ascii_keyword_matches(text)

    def test_random_corpus(self):
        corpus, fired = _corpus(50_000), Counter()
        for text in corpus:
            assert _matches(HOST_PORT_RE, text) == _reference(text, fired), text
            assert _matches(PORT_KEYWORD_RE, text) == _ascii_keyword_matches(text)
        assert sum(bool(HOST_PORT_RE.search(text)) for text in corpus) > 1_000
        assert sum(bool(PORT_KEYWORD_RE.search(text)) for text in corpus) > 100
        assert fired["non-ascii"] > 1_000 and fired["dot-colon-digit"] > 500
        assert fired["date-hour"] > 25

    def test_the_entity_registry_counts_the_same_ports(self):
        lines = _corpus(5_000)
        observed = {
            o.entity_value: o.mention_count
            for o in LogsEntityExtractor().extract("\n".join(lines))
            if o.entity_type == EntityType.PORT
        }
        assert observed == _expected_ports(lines)

    def test_the_log_profile_counts_the_same_ports(self):
        lines = _corpus(5_000)
        expected = _expected_ports(lines)
        _, profile = LogsAndErrorsExtractor()._build_entity_profile(
            "\n".join(lines), top_n=len(expected)
        )
        section = profile.split("  Distinct Ports: ", 1)[1]
        rows = re.match(r"(\d+)\n((?:    \d+: \d+ lines\n?)*)", section)
        assert rows and int(rows[1]) == len(expected)
        counts = re.findall(r"(\d+): (\d+) lines", rows[2])
        assert {port: int(count) for port, count in counts} == expected


#: fm#2677's rules on repeated units: a ``line:col`` tail, a dotted version and
#: a ``T`` between digits; the small size is about the hex run's bytes.
RULE_SHAPES = {"a.py:1:": 18, "a:1.a ": 21, "1T11:1 ": 18}


@pytest.mark.unit
@pytest.mark.performance
def test_the_pattern_is_linear_on_a_hex_run():
    """The pattern alone, on the shared ``hex-run`` shape.

    The whole extractor on the same shape is ``test_the_extractor_is_linear``
    in ``test_sshd_auth_anchoring.py``.
    """
    assert_linear_growth(
        HOST_PORT_RE.findall,
        lambda repetitions: adversarial_line("hex-run", repetitions),
        small=max(1, ADVERSARIAL_SHAPES["hex-run"][3] // 512),
        label="HOST_PORT_RE on hex-run",
        settle_bytes=lambda repetitions: SETTLE_PER_LINE_BYTE
        * adversarial_length("hex-run", repetitions),
    )


@pytest.mark.unit
@pytest.mark.performance
@pytest.mark.parametrize("unit, small", RULE_SHAPES.items())
def test_the_pattern_is_linear_on_the_rule_shapes(unit, small):
    assert_linear_growth(
        HOST_PORT_RE.findall,
        lambda repetitions: unit * repetitions,
        small=small,
        label=f"HOST_PORT_RE on {unit!r}",
        settle_bytes=lambda repetitions: SETTLE_PER_LINE_BYTE * len(unit) * repetitions,
    )
