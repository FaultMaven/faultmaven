"""``search_file``'s vocabulary scan reads host:port with the shared grammar (#2723).

The scan runs inline in the tool's async path on a zero-result search, over the
first ``SearchFileTool._VOCAB_SCAN_CHARS`` characters of an evidence file. Its
own host:port pattern, ``\\b[\\w-]+:\\d{2,5}\\b``, tried every split of a long
colon-less ``[\\w-]`` run: 1.87 s at 12,800 characters of ``"a-"``, quadratic
within the bound. It also read a syslog time (``10:30``) and a truncated host
(``5:22``) as hints for the model. The scan now reads ``HOST_PORT_RE``, which
fm#2641 made linear.
"""

from __future__ import annotations

import pytest

from faultmaven.modules.agent.tools.search_file_tool import SearchFileTool
from tests.wallclock import assert_linear_growth

#: Growth windows are ``small``, ``8*small`` and ``64*small``, with no
#: escalation, so the one window measured tops out at the scan's bound.
STEP = 8

#: The hostile shapes probed, as (prefix, repeated unit), ``"a-"`` first: a
#: long colon-less ``[\\w-]`` run, quadratic under the old pattern. The prefix
#: replaces the shape's first characters, so ``n`` units are ``n * len(unit)``
#: characters.
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
]


def _payload(prefix: str, unit: str, repetitions: int) -> str:
    return (prefix + unit * repetitions)[: len(unit) * repetitions]


def _small(unit: str) -> int:
    """The ``small`` whose window's top size is the scan's bound."""
    return SearchFileTool._VOCAB_SCAN_CHARS // (len(unit) * STEP**2)


@pytest.mark.unit
def test_the_scan_reads_host_port_with_the_shared_pattern():
    from faultmaven.utils.host_port import HOST_PORT_RE

    patterns = dict(SearchFileTool._VOCAB_PATTERNS)
    assert patterns["host_port"] is HOST_PORT_RE


@pytest.mark.unit
@pytest.mark.parametrize("line, expected", READINGS)
def test_the_scan_gives_the_shared_readings(line, expected):
    assert SearchFileTool()._extract_file_vocabulary(line)["patterns"] == expected


@pytest.mark.unit
def test_the_scan_stops_at_thirty_patterns():
    codes = " ".join(f"code {400 + i}" for i in range(40))
    content = codes + " FooError BarError db:5432 10.0.0.1 /var/log/x"
    patterns = SearchFileTool()._extract_file_vocabulary(content)["patterns"]
    assert patterns == [str(400 + i) for i in range(30)]


@pytest.mark.unit
@pytest.mark.parametrize("prefix, unit", SHAPES)
def test_the_window_tops_out_at_the_bound(prefix, unit):
    top = _small(unit) * STEP**2
    assert len(_payload(prefix, unit, top)) == SearchFileTool._VOCAB_SCAN_CHARS


@pytest.mark.unit
@pytest.mark.parametrize("prefix, unit", SHAPES)
def test_the_scan_is_linear_up_to_its_bound(prefix, unit):
    assert_linear_growth(
        SearchFileTool()._extract_file_vocabulary,
        lambda repetitions: _payload(prefix, unit, repetitions),
        small=_small(unit),
        step=STEP,
        max_escalations=0,
        label=f"the vocabulary scan on {prefix!r} + {unit!r} * n",
    )
