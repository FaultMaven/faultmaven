"""The coverage scan reads a long line at its two ends only (#3116 B1).

``extract_time_range_ts`` runs inline in the API process over at most 110
lines (the upload's, and an evidence extract the model wrote), each of any
length: a single-line 10 MB upload cost seconds on the event loop. Principle 14
lets it stay there under a declared input limit. A line longer than
``COVERAGE_LINE_SCAN_MAX_CHARS`` plus a margin is read in two windows, its first
and its last ``COVERAGE_LINE_SCAN_MAX_CHARS`` characters, each pattern over both
before the next, so a match is whole and patterns keep their priority. Only the
scanned lines are split off the content.
"""

from __future__ import annotations

import random
from datetime import datetime, timezone

import pytest

from faultmaven.modules.preprocessing.extractors import utils
from faultmaven.modules.preprocessing.extractors.utils import (
    _COVERAGE_MATCH_MARGIN,
    _HEAD_SCAN_LINES,
    _TAIL_SCAN_LINES,
    COVERAGE_LINE_SCAN_MAX_CHARS,
    extract_time_range_ts,
)
from tests.wallclock import assert_linear_growth

pytestmark = [pytest.mark.unit]

N = COVERAGE_LINE_SCAN_MAX_CHARS
#: The longest line read whole; past it, the two windows.
WHOLE = N + _COVERAGE_MATCH_MARGIN
START = "2024-01-15T10:30:00"
END = "2024-01-15T11:45:00"
#: Longer than both windows together.
LONG = 1_000_000


def _utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=timezone.utc)


def _main_time_range(content: str, allow_bare_epoch: bool = True):
    """``extract_time_range_ts`` as it stood on ``main``: every line split off
    the content, each read whole."""
    lines = content.split("\n")
    first = source = head_idx = None
    for i, line in enumerate(lines[:_HEAD_SCAN_LINES]):
        first, source = utils._extract_timestamp_with_source(
            line, allow_bare_epoch=allow_bare_epoch
        )
        if first:
            head_idx = i
            break
    floor = head_idx + 1 if head_idx is not None else 0
    last = None
    for line in reversed(lines[max(floor, len(lines) - _TAIL_SCAN_LINES) :]):
        last, tail_source = utils._extract_timestamp_with_source(
            line, allow_bare_epoch=allow_bare_epoch
        )
        if last:
            source = source or tail_source
            break
    return first, last, source


class _Recording:
    """A pattern that records how many characters each read of it may cover."""

    def __init__(self, pattern, reads: list[int]):
        self._pattern, self._reads = pattern, reads

    def search(self, line, pos=0, endpos=None):
        end = len(line) if endpos is None else min(endpos, len(line))
        self._reads.append(end - pos)
        return self._pattern.search(line, pos, end)

    def finditer(self, line, pos=0):
        self._reads.append(len(line) - pos)
        return self._pattern.finditer(line, pos)


def test_no_pattern_reads_past_a_window(monkeypatch):
    """However long the lines, each read covers at most one window."""
    reads: list[int] = []
    monkeypatch.setattr(
        utils,
        "_TS_PATTERNS",
        [(name, _Recording(p, reads)) for name, p in utils._TS_PATTERNS],
    )
    content = "\n".join(["x" * LONG] * 3 + ["y" * 10_000] * 200)

    assert extract_time_range_ts(content) == (None, None, None)
    assert reads and max(reads) == WHOLE


@pytest.mark.parametrize("unit", ["a", "1", "Abc ", "12 "])
def test_the_scan_is_linear_in_line_length_up_to_the_whole_line_read(unit):
    small = WHOLE // 64
    assert_linear_growth(
        extract_time_range_ts,
        lambda n: "\n".join([(unit * n)[:n]] * (_HEAD_SCAN_LINES + _TAIL_SCAN_LINES)),
        small=small,
        max_escalations=0,
        undecided_repeats=2,
        label=f"extract_time_range_ts over 110 lines of {unit!r} * n",
    )


def test_a_timestamp_at_each_end_of_a_long_line_is_found():
    content = f"{START} ERROR {'x' * LONG}\nmiddle\n{'y' * LONG} {END} INFO"

    assert extract_time_range_ts(content) == (
        _utc(2024, 1, 15, 10, 30),
        _utc(2024, 1, 15, 11, 45),
        "iso8601_t",
    )


@pytest.mark.parametrize(
    "at, found",
    [
        (N - 1, True),  # starts in the head window
        (N, False),
        (10 * N - N - len(START), False),  # ends at the tail window's edge
        (10 * N - N - len(START) + 1, True),  # ends inside it
    ],
)
def test_the_windows_edges(at, found):
    line = "x" * at + START
    line += "x" * (10 * N - len(line))

    start, _, _ = extract_time_range_ts(line)

    assert (start is not None) is found


def test_a_timestamp_only_mid_line_on_a_line_past_both_windows_is_not_found():
    line = "q " * 1500 + START + " " + "r " * 1500
    assert len(line) > 2 * WHOLE

    assert extract_time_range_ts(line) == (None, None, None)
    assert _main_time_range(line)[0] == _utc(2024, 1, 15, 10, 30)


#: Lines ``main`` read right that a plain cut at the window read wrong.
AS_ON_MAIN = {
    # The cut leaves 10 of the id's digits, read as a 2009 epoch end.
    "an id across the cut": (
        f"{START} INFO start\nx\n{END} INFO end\n"
        + "y" * (N - 11)
        + " 12345678901234567890 tail",
        (_utc(2024, 1, 15, 10, 30), _utc(2024, 1, 15, 11, 45), "iso8601_t"),
    ),
    # The cut drops the year.
    "a syslog year across the cut": (
        "a\n" + "z" * (N - 19) + " Jan 15 10:30:00 2019 msg",
        (_utc(2019, 1, 15, 10, 30), None, "syslog_bsd"),
    ),
    # A JSON log writes its time after the message: an id's epoch must not win.
    "a JSON time after an epoch-shaped id": (
        "\n".join(
            '{"msg": "'
            + "a " * 600
            + "request 1712345678 handled "
            + "b " * 600
            + f'", "ts": "{ts}Z"}}'
            for ts in (START, END)
        ),
        (_utc(2024, 1, 15, 10, 30), _utc(2024, 1, 15, 11, 45), "iso8601_t"),
    ),
    "a JSON time after a long message": (
        "\n".join(
            '{"msg": "' + "a " * 1400 + f'", "ts": "{ts}Z"}}' for ts in (START, END)
        ),
        (_utc(2024, 1, 15, 10, 30), _utc(2024, 1, 15, 11, 45), "iso8601_t"),
    ),
}


@pytest.mark.parametrize("name", AS_ON_MAIN)
def test_a_long_line_reads_as_on_main(name):
    content, expected = AS_ON_MAIN[name]

    assert extract_time_range_ts(content) == expected == _main_time_range(content)


#: Line shapes for the split pin: timestamps of several patterns, a bare
#: integer, blank lines and text.
_LINES = [
    "",
    "   ",
    "plain text",
    f"{START} a",
    f"{END} b",
    "Jan 15 10:30:00 host x",
    "17/06/09 20:10:40 INFO y",
    "maxBytes: 2147483647",
    "1712345678901 tick",
]


@pytest.mark.parametrize("seed", range(40))
@pytest.mark.parametrize("allow_bare_epoch", [True, False])
def test_splitting_only_the_scanned_lines_reads_as_on_main(seed, allow_bare_epoch):
    rng = random.Random(seed)
    count = rng.choice([0, 1, 2, 9, 10, 11, 100, 109, 110, 111, rng.randint(0, 300)])
    content = "\n".join(rng.choice(_LINES) for _ in range(count))
    content += rng.choice(["", "\n", "\n\n\n"])

    assert extract_time_range_ts(
        content, allow_bare_epoch=allow_bare_epoch
    ) == _main_time_range(content, allow_bare_epoch)
