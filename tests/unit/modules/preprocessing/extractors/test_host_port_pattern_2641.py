"""The shared host-and-port pattern: the same matches as before, in linear time (fm#2641).

The old pattern is kept here as the oracle. On a long run of word characters
with no colon it tried every split between its two classes, so its cost was
quadratic in the run: 23 s for a 64 KB hex run.
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
    distinct_on_line,
    is_port,
)
from tests.wallclock import assert_linear_growth

OLD_HOST_PORT_RE = re.compile(r"(?<![\w.-])[\w-]*[A-Za-z.][\w.-]*:(\d{1,5})\b")

#: Negatives first: a digits-only or empty host, a port that is too long or
#: runs on into a word, a non-ASCII host. Then the shapes that do match.
EDGE_CASES = ["", "12345", "a" * 50, "1-2-3:", ":80", "-:80", "_:80", "9:80"]
EDGE_CASES += ["é:80", "host:123456", "a:80x", "a:80_", "a:8.0", "04:47:44"]
EDGE_CASES += ["1.2:80", "x.y-z:65535 b:1", "db.local:5432", "-a:1"]


def _corpus(count: int) -> list[str]:
    rng = random.Random(2641)
    alphabet = "aZ9_-.:é 1/x\t"
    return [
        "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 24)))
        for _ in range(count)
    ]


def _matches(pattern: re.Pattern[str], text: str) -> list:
    return [(m.span(), m.groups()) for m in pattern.finditer(text)]


def _expected_ports(lines: list[str]) -> Counter:
    """Port counts by the old pattern, one mention per line."""
    keyword = LogsAndErrorsExtractor._PORT_KEYWORD_RE
    return Counter(
        port
        for line in lines
        for port in distinct_on_line(line, keyword, OLD_HOST_PORT_RE)
        if is_port(port)
    )


@pytest.mark.unit
class TestSameMatchesAsTheOldPattern:
    def test_edge_cases(self):
        for text in EDGE_CASES:
            assert _matches(HOST_PORT_RE, text) == _matches(OLD_HOST_PORT_RE, text)
        assert not HOST_PORT_RE.search("a:80x 04:47:44 _:80 9:80 host:123456")
        assert HOST_PORT_RE.findall("x.y-z:65535 b:1") == ["65535", "1"]

    def test_random_corpus(self):
        corpus = _corpus(50_000)
        assert sum(bool(OLD_HOST_PORT_RE.search(text)) for text in corpus) > 1_000
        for text in corpus:
            assert _matches(HOST_PORT_RE, text) == _matches(OLD_HOST_PORT_RE, text)

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


def _hex_run(length: int) -> str:
    return ("0123456789abcdef" * (length // 16 + 1))[:length]


@pytest.mark.unit
@pytest.mark.performance
class TestLinearInALongHexRun:
    def test_the_pattern_is_linear(self):
        assert_linear_growth(
            HOST_PORT_RE.findall,
            lambda length: "blob=" + _hex_run(length),
            small=128,
            label="HOST_PORT_RE on a colon-less hex run",
        )

    def test_the_extractor_is_linear(self):
        genuine = "Dec 10 06:55:45 LabSZ app[1]: connect db.local:5432 refused"
        extractor = LogsAndErrorsExtractor()
        assert_linear_growth(
            lambda line: extractor.extract(genuine + "\n" + line + "\n"),
            lambda length: "Dec 10 06:55:46 LabSZ app[1]: blob=" + _hex_run(length),
            small=1024,
            label="LogsAndErrorsExtractor.extract beside a hex run",
            max_escalations=1,
        )
