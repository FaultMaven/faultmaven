"""``search_file``'s vocabulary scan reads host:port with the shared grammar (#2723).

The scan runs inline in the tool's async path on a zero-result search, over the
first 102,400 characters of an evidence file. Its own host:port pattern,
``\\b[\\w-]+:\\d{2,5}\\b``, tried every split of a long colon-less ``[\\w-]`` run:
1.87 s at 12,800 characters of ``"a-"``, quadratic within the bound. It also
read a syslog time (``10:30``) and a truncated host (``5:22``) as hints for the
model. The scan now reads ``HOST_PORT_RE``, which fm#2641 made linear.
"""

from __future__ import annotations

import pytest

from faultmaven.modules.agent.tools.search_file_tool import SearchFileTool
from faultmaven.utils.host_port import HOST_PORT_RE
from tests.wallclock import assert_linear_growth

#: The scan's bound: ``_extract_file_vocabulary`` reads ``content[:102400]``.
SCAN_BOUND = 102_400

#: Growth windows are ``small``, ``8*small`` and ``64*small``, moved up by 8
#: at most once; every window's top stays inside the bound, where it measures.
STEP = 8
ESCALATIONS = 1

#: The hostile shapes probed, as (prefix, repeated unit), ``"a-"`` first: a
#: long colon-less ``[\\w-]`` run, quadratic under the old pattern.
SHAPES = [("", "a-"), ("", "A"), ("A", "a"), ("", "1."), ("", "/a")]
SHAPES += [("", "4"), ("", "aA")]

#: Each line and the scan's ``patterns`` for it, negatives first: a syslog
#: time, an ISO-8601 time and a dotted image tag give no host:port.
READINGS = [
    ("2024-01-15 10:30:15 INFO x", []),
    ("2024-01-15T06:55:12", []),
    ("image nginx:1.25", []),
    ("connect to db.local:5432 failed", ["db.local:5432"]),
    ("10.0.0.5:22", ["10.0.0.5:22", "10.0.0.5"]),
    ("app.py:118", ["app.py:118"]),
    ("postgres:16", ["postgres:16"]),
    ("status:200", ["status:200"]),
    ("web-01:80", ["web-01:80"]),
    ("T06:55:45Z", ["T06:55"]),
]


@pytest.mark.unit
def test_the_scan_reads_host_port_with_the_shared_pattern():
    patterns = dict(SearchFileTool._VOCAB_PATTERNS)
    assert patterns["host_port"] is HOST_PORT_RE


@pytest.mark.unit
@pytest.mark.parametrize("line, expected", READINGS)
def test_the_scan_gives_the_shared_readings(line, expected):
    assert SearchFileTool()._extract_file_vocabulary(line)["patterns"] == expected


def _small(prefix: str, unit: str) -> int:
    """A ``small`` whose last window's top stays inside the bound."""
    return (SCAN_BOUND - len(prefix)) // (len(unit) * STEP ** (2 + ESCALATIONS))


@pytest.mark.unit
@pytest.mark.parametrize("prefix, unit", SHAPES)
def test_every_window_stays_inside_the_bound(prefix, unit):
    top = _small(prefix, unit) * STEP ** (2 + ESCALATIONS)
    assert len(prefix + unit * top) <= SCAN_BOUND


@pytest.mark.unit
@pytest.mark.performance
@pytest.mark.parametrize("prefix, unit", SHAPES)
def test_the_scan_is_linear_within_its_bound(prefix, unit):
    assert_linear_growth(
        SearchFileTool()._extract_file_vocabulary,
        lambda repetitions: prefix + unit * repetitions,
        small=_small(prefix, unit),
        step=STEP,
        max_escalations=ESCALATIONS,
        label=f"the vocabulary scan on {prefix!r} + {unit!r} * n",
    )
