"""Machine-readable record of every wall-clock comparison (#1567).

Why a file and not stdout
-------------------------

The A/B job (``.github/workflows/benchmarks.yml``) needs, for each timed
site, the *one number that site's assertion compares*, on two checkouts,
in a form a program can join. That number is already printed — but in five
different shapes across the two suites (``Measurement.report()``'s
``min 7.6ms (median ...)``, ``report_p95``'s indented ``P95: 7.60ms``,
``report_benchmark``'s ``P95: 7.60ms (regression budget: ...)`` and two
more in ``tests/performance/``), and a passing test's stdout only survives
at all because ``--json-report`` keeps it. A comparison built on scraping
five formats out of a report artifact is a detector whose input is its
weakest part, so the number is written down where it is *computed*
instead.

Where it is computed is the single pair of comparison helpers in
``assertions.py`` — the choke point #908 and #1557 spent two issues
creating, and the one ``tests/unit/ci/test_benchmark_calibration.py``
already fails the build for bypassing. Every timed comparison in
``tests/benchmarks/`` and ``tests/performance/`` goes through it, so
recording there needs no per-suite vocabulary and cannot fall behind a new
test.

Inert unless asked
------------------

Nothing is written unless ``FM_WALLCLOCK_RECORD`` names a path, so an
ordinary ``pytest tests/benchmarks/`` run, a developer's run and both
required CI gates are byte-for-byte unaffected.

When it *is* set and the file cannot be written, this raises. That is
deliberate: the caller asked for a recording, and a recorder that fails
quietly hands the comparator an empty file, which is indistinguishable
from "nothing regressed". The comparator refuses an empty head side for
the same reason.

The key
-------

Rows are joined across the two sides on ``(nodeid, label, occurrence)``.

* ``nodeid`` comes from ``PYTEST_CURRENT_TEST``, so a renamed or moved
  test reads as *new* on the head side rather than being silently matched
  to a different test's history.
* ``label`` distinguishes several comparisons made by one test — a
  workload site asserting two operations would otherwise collapse to one
  row, and which of the two survived would depend on dict ordering.
* ``occurrence`` disambiguates a label a test genuinely repeats (a
  parametrised helper called in a loop). Counting per ``(nodeid, label)``
  rather than per ``nodeid`` keeps the join stable when the head reorders
  two differently-labelled comparisons inside one test.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Dict, Tuple

#: Names the JSONL file each comparison is appended to. Unset = inert.
RECORD_ENV = "FM_WALLCLOCK_RECORD"

#: The row schema's version, written into every record as ``v``.
#:
#: ‼ Bump this whenever a field is renamed, removed, or changes meaning.
#: The A/B job compares a HEAD checkout against a BASE checkout that may
#: be any commit on ``main``, and the two trees' ``record.py`` files are
#: therefore different files. Without a version the base can carry the
#: recorder and still write a shape the head's comparator cannot read —
#: which arrives as a hard parse failure and reds every pull request
#: until ``main`` catches up. The workflow reads this constant out of the
#: BASE tree and skips the comparison when the head cannot read it, so a
#: schema change costs a few skipped comparisons instead of a red wall.
#: ``tests/wallclock/ab.py``'s ``SUPPORTED_RECORD_VERSIONS`` is the other
#: half, and a test pins them to each other.
RECORD_FORMAT_VERSION = 1

#: ``observed`` is a duration in seconds; smaller is better.
LATENCY_METRIC = "latency_seconds"

#: ``observed`` is a rate per second; LARGER is better. The comparator
#: inverts this metric's ratio so that "above 1.0 is worse" holds for
#: both, which is the property that lets one threshold govern both.
THROUGHPUT_METRIC = "throughput_per_second"

_lock = threading.Lock()
_occurrences: Dict[Tuple[str, str], int] = {}


def _current_nodeid() -> str:
    """The test being executed, from pytest's own environment variable.

    ``PYTEST_CURRENT_TEST`` is ``"<nodeid> (<phase>)"``; the phase is
    dropped so setup-time and call-time comparisons of one test share a
    key. Outside pytest (a direct call of the helper in a unit test) there
    is no nodeid, and ``"<no pytest test>"`` is recorded rather than
    guessing — a row under that key joins to nothing and shows up as
    unmatched, which is the honest outcome.
    """
    current = os.environ.get("PYTEST_CURRENT_TEST", "")
    if not current:
        return "<no pytest test>"
    # Only the trailing " (phase)" is removed; a nodeid can itself contain
    # spaces (a parametrised id), so rsplit on the LAST space is required
    # and split() would be wrong.
    head, _, tail = current.rpartition(" ")
    if head and tail.startswith("(") and tail.endswith(")"):
        return head
    return current


def reset_for_testing() -> None:
    """Forget the per-key occurrence counters. Only for this file's tests."""
    with _lock:
        _occurrences.clear()


def record_comparison(
    *,
    metric: str,
    label: str,
    observed: float,
    budget: float,
    kind: str,
    scale: float,
) -> None:
    """Append one comparison to ``$FM_WALLCLOCK_RECORD``, if it is set.

    Called from ``assert_latency_within`` / ``assert_throughput_at_least``
    BEFORE the assertion, so a site that fails its absolute budget still
    contributes its number to the A/B. A red benchmark is exactly when
    knowing whether the base was red too is most useful.

    Args:
        metric: ``LATENCY_METRIC`` or ``THROUGHPUT_METRIC``.
        label: The comparison's own name, as the failure message spells it.
        observed: The measured statistic, in the metric's units.
        budget: The threshold it was compared against, BEFORE calibration.
        kind: Which of the budget's two numbers that was.
        scale: The calibration applied to it on this machine.
    """
    path = os.environ.get(RECORD_ENV)
    if not path:
        return

    nodeid = _current_nodeid()
    with _lock:
        key = (nodeid, label)
        occurrence = _occurrences.get(key, 0)
        _occurrences[key] = occurrence + 1
        row = {
            "v": RECORD_FORMAT_VERSION,
            "nodeid": nodeid,
            "label": label,
            "occurrence": occurrence,
            "metric": metric,
            "observed": observed,
            "budget": budget,
            "kind": kind,
            "scale": scale,
        }
        # Opened per row rather than held: the suite runs for minutes and a
        # crash mid-run should still leave every comparison made so far.
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
