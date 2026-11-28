"""``vectorize_file``'s split is linear up to its input limit (#3116 B3).

``chunk_structural_index`` runs in a thread (``vector_storage.py``), over a
stored structural index of at most ``MAX_STORED_EXTRACT_CHARS``. Its encodes
are native code that releases the interpreter lock; the split around them
holds it, so Principle 14 asks for a growth test over that limit. Measured
before this test, each shape grew about 4x per 4x, at worst 0.66 s at 64 KiB
(``ab\\n`` runs), with the event loop's lag 6-12 ms.
"""

from __future__ import annotations

import pytest

from faultmaven.core.preprocessing.vector_storage import chunk_structural_index
from faultmaven.modules.preprocessing.extractors.utils import MAX_STORED_EXTRACT_CHARS
from tests.wallclock import assert_linear_growth

pytestmark = [pytest.mark.unit]

STEP = 8
SMALL = MAX_STORED_EXTRACT_CHARS // STEP**2

#: The shapes probed: one long spaced line, no whitespace, short lines, short
#: paragraphs, header runs, prose with sections, and digits.
SHAPES = {
    "spaced line": "word ",
    "no spaces": "a",
    "ab-lines": "ab\n",
    "ab-paragraphs": "ab\n\n",
    "headers": "=== S ===\nx\n",
    "mixed prose": "=== ERRORS ===\nThe pod restarted under load.\n\nexit 137 at 10:40\n",
    "digits": "1234567890",
}


def test_the_window_tops_out_at_the_limit():
    assert SMALL * STEP**2 == MAX_STORED_EXTRACT_CHARS


@pytest.mark.performance
@pytest.mark.parametrize("unit", SHAPES.values(), ids=SHAPES.keys())
def test_the_split_is_linear_up_to_the_stored_extract(unit):
    assert_linear_growth(
        chunk_structural_index,
        lambda n: (unit * (n // len(unit) + 1))[:n],
        small=SMALL,
        step=STEP,
        max_escalations=0,
        undecided_repeats=2,
        label=f"chunk_structural_index over {unit!r} runs",
    )
