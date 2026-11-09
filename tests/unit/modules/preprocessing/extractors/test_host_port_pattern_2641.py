"""The shared port patterns: the same matches as before, in linear time (fm#2641).

The old host-and-port pattern is kept here as the oracle, and so is the port
keyword pattern, which three modules used to define for themselves. On a long run of word characters
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
    PORT_KEYWORD_RE,
    distinct_on_line,
    is_port,
)
from tests.unit.modules.preprocessing.extractors.test_sshd_auth_anchoring import (
    ADVERSARIAL_SHAPES,
    SETTLE_PER_LINE_BYTE,
    adversarial_length,
    adversarial_line,
)
from tests.wallclock import assert_linear_growth

OLD_HOST_PORT_RE = re.compile(r"(?<![\w.-])[\w-]*[A-Za-z.][\w.-]*:(\d{1,5})\b")
OLD_PORT_KEYWORD_RE = re.compile(r"\bport[= :]+(\d{1,5})\b", re.IGNORECASE)

#: Negatives first: a digits-only or empty host, a port that is too long or
#: runs on into a word, a non-ASCII host.
NEGATIVES = ["", "12345", "a" * 50, "1-2-3:", ":80", "-:80", "_:80", "9:80"]
NEGATIVES += ["é:80", "host:123456", "a:80x", "a:80_", "04:47:44"]
#: Then the shapes that match, including a port in non-ASCII digits, which
#: ``\d`` takes, and a version tag: ``a:8.0`` gives ``8`` as ``nginx:1.25``
#: gives ``1``, a false reading (fm#2677).
EDGE_CASES = NEGATIVES + ["1.2:80", "x.y-z:65535 b:1", "db.local:5432", "-a:1"]
EDGE_CASES += ["h:٣٣", "a:8.0"]

#: The keyword in any case, after any of ``= :``, and not inside a word or
#: before a port that is too long.
KEYWORD_CASES = {"Port 22": ["22"], "PORT=80": ["80"], "port: 80": ["80"]}
KEYWORD_CASES |= {"port 123456": [], "sport 22": [], "port:80": ["80"]}


def _corpus(count: int) -> list[str]:
    rng = random.Random(2641)
    alphabet = "aZ9_-.:é 1/x\t٣５"
    return [
        "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 24)))
        for _ in range(count)
    ]


def _matches(pattern: re.Pattern[str], text: str) -> list:
    return [(m.span(), m.groups()) for m in pattern.finditer(text)]


def _expected_ports(lines: list[str]) -> Counter:
    """Port counts by the old pattern, one mention per line."""
    return Counter(
        port
        for line in lines
        for port in distinct_on_line(line, OLD_PORT_KEYWORD_RE, OLD_HOST_PORT_RE)
        if is_port(port)
    )


@pytest.mark.unit
class TestSameMatchesAsTheOldPattern:
    def test_edge_cases(self):
        for text in EDGE_CASES:
            assert _matches(HOST_PORT_RE, text) == _matches(OLD_HOST_PORT_RE, text)
        assert not any(HOST_PORT_RE.search(text) for text in NEGATIVES)
        assert HOST_PORT_RE.findall("a:8.0") == ["8"]
        assert HOST_PORT_RE.findall("x.y-z:65535 b:1") == ["65535", "1"]
        assert HOST_PORT_RE.findall("h:٣٣") == ["٣٣"]

    def test_the_port_keyword(self):
        for text, ports in KEYWORD_CASES.items():
            assert PORT_KEYWORD_RE.findall(text) == ports, text
            assert _matches(PORT_KEYWORD_RE, text) == _matches(
                OLD_PORT_KEYWORD_RE, text
            )

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
