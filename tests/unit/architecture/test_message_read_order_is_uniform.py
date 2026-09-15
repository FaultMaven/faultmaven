"""Every ``case_messages`` reader must carry the same ORDER BY (#1428).

The defect this guards against already happened: four readers, four different
clauses, and the comment above one of them *claimed* parity it did not have.
Five hand-written literals with nothing relating them will drift again, and the
only thing standing in the way otherwise is a handful of integration tests a
new reader will not know to look for.

The clause is not interpolated from a shared constant because the SQL in these
modules contains ``{}`` literals (JSON defaults), so turning the queries into
f-strings to share one string would be a riskier change than checking them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

#: The order every reader uses. NOT ``message_id``-terminated: a minted id is a
#: uuid4 and breaks ties at random, which inverted same-turn exchanges. See
#: ``CaseRepository.message_sort_key`` for why the order defers to insertion.
CANONICAL = "created_at ASC, turn_number ASC"

_REPOS = Path(__file__).resolve().parents[3] / "faultmaven/modules/case/infrastructure"

#: Files that issue SQL against ``case_messages``.
_FILES = ("sqlite_case_repository.py", "postgresql_hybrid_case_repository.py")

#: How many SELECT-from-case_messages readers we expect. A positive control:
#: if a refactor moves them, this test must fail loudly rather than silently
#: checking nothing.
_EXPECTED_READERS = 5

#: The PostgreSQL aggregate puts its ORDER BY INSIDE ``json_agg(...)``, which
#: sits BEFORE the FROM, while the plain SELECTs put it after. So the check
#: windows around each read rather than scanning forward from it.
_WINDOW = 900


def _readers():
    """Every read of ``case_messages``, with the SQL around it."""
    found = []
    for name in _FILES:
        text = (_REPOS / name).read_text()
        for m in re.finditer(r"FROM case_messages\b", text):
            window = text[max(0, m.start() - _WINDOW) : m.end() + _WINDOW]
            line = text[: m.start()].count("\n") + 1
            # A COUNT(*) subquery selects no rows to order.
            head = text[max(0, m.start() - 40) : m.start()]
            if "COUNT(" in head.upper():
                continue
            found.append((name, line, window))
    return found


@pytest.mark.unit
@pytest.mark.architecture
def test_every_case_messages_reader_uses_the_canonical_order():
    readers = _readers()
    assert len(readers) >= _EXPECTED_READERS, (
        f"expected at least {_EXPECTED_READERS} case_messages readers, found "
        f"{len(readers)} — the scan has stopped matching, so it is checking "
        "nothing. Fix the scan before trusting a green run."
    )

    wrong = []
    for name, line, window in readers:
        # Strip the table alias the PostgreSQL aggregate uses, so one canonical
        # form covers both spellings.
        normalised = re.sub(r"\bm\.", "", window)
        if CANONICAL not in normalised:
            order = re.search(r"ORDER BY[^)\n]*", window)
            wrong.append(
                f"{name}:{line} -> "
                f"{order.group(0).strip() if order else 'NO ORDER BY NEARBY'}"
            )

    assert not wrong, (
        "these case_messages readers do not use the canonical order "
        f"({CANONICAL!r}):\n  " + "\n  ".join(wrong)
    )


@pytest.mark.unit
@pytest.mark.architecture
def test_no_reader_adds_a_tiebreaker_beyond_the_canonical_pair():
    """No reader may order on anything past ``(created_at, turn_number)``.

    A separate guard because the check above tests CONTAINMENT: an APPENDED
    column leaves the canonical text intact, so uniformity alone cannot see it.
    Measured — re-adding ``message_id ASC`` to every SQLite clause left that
    test green.

    The concrete regression this exists for: ``message_id`` is a uuid4, so
    using it to break a tie orders at RANDOM. It rendered a same-turn exchange
    assistant-before-user about half the time, where the partial order had been
    deferring to insertion order and getting it right. Any other appended
    column is suspect for the same reason — the thing that actually orders tied
    rows is insertion, which no column here records.
    """
    allowed = {"created_at", "turn_number", "asc", "case_id"}
    offenders = []
    for name, line, window in _readers():
        normalised = re.sub(r"\bm\.", "", window)
        for clause in re.findall(r"ORDER BY([^)\n]*)", normalised):
            extra = {
                tok.strip().lower()
                for tok in re.split(r"[,\s]+", clause)
                if tok.strip()
            } - allowed
            if extra:
                offenders.append(
                    f"{name}:{line} -> ORDER BY{clause.strip()} ({sorted(extra)})"
                )
    assert not offenders, (
        "case_messages readers must not order on anything beyond "
        "(created_at, turn_number); ties belong to insertion order:\n  "
        + "\n  ".join(offenders)
    )
