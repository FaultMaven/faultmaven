"""The coverage scan reads each line to ``COVERAGE_LINE_SCAN_MAX_CHARS`` (#3116 B1).

``extract_time_range_ts`` runs inline in the API process over at most 110
lines (the upload's, and an evidence extract the model wrote), each of any
length: a single-line 10 MB upload cost seconds on the event loop. Principle 14
lets it stay there under a declared input limit, so each scanned line is read
to the window and no further, and a timestamp inside it is still found.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from faultmaven.modules.preprocessing.extractors import utils
from faultmaven.modules.preprocessing.extractors.utils import (
    COVERAGE_LINE_SCAN_MAX_CHARS,
    extract_time_range_ts,
)

pytestmark = [pytest.mark.unit]

START = "2024-01-15T10:30:00"
END = "2024-01-15T11:45:00"
#: Longer than any window, and over every scanned line.
LONG = 1_000_000


def test_the_patterns_read_no_line_past_the_window(monkeypatch):
    """However long the lines, each one the patterns see is at most the window."""
    seen: list[int] = []
    parse = utils._extract_timestamp_with_source

    def spy(line, **kwargs):
        seen.append(len(line))
        return parse(line, **kwargs)

    monkeypatch.setattr(utils, "_extract_timestamp_with_source", spy)
    content = "\n".join(["x" * LONG] * 3 + ["y" * 10_000] * 200)

    assert extract_time_range_ts(content) == (None, None, None)
    assert seen and max(seen) == COVERAGE_LINE_SCAN_MAX_CHARS


def test_a_timestamp_inside_the_window_is_found_on_a_long_line():
    content = f"{START} ERROR {'x' * LONG}\nmiddle\n{END} INFO {'y' * LONG}"

    assert extract_time_range_ts(content) == (
        datetime(2024, 1, 15, 10, 30, tzinfo=timezone.utc),
        datetime(2024, 1, 15, 11, 45, tzinfo=timezone.utc),
        "iso8601_t",
    )


@pytest.mark.parametrize(
    "end, found",
    [(COVERAGE_LINE_SCAN_MAX_CHARS, True), (COVERAGE_LINE_SCAN_MAX_CHARS + 1, False)],
)
def test_the_window_is_read_to_its_last_character_and_no_further(end, found):
    line = " " * (end - len(START)) + START

    start, _, _ = extract_time_range_ts(line)

    assert (start is not None) is found
