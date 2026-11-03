"""Run one slice of the suite: ``pytest tests/ --shard=K/N``.

``tests/conftest.py`` registers this plugin (``pytest_plugins``), so the
option exists in every run of the repository's suite. ``-p tests.ci_shard``
loads it outside that conftest, as tests/unit/ci/test_ci_shard.py does, and
needs the repository on ``sys.path`` (``python -m pytest`` from its root).

CI splits Test Standalone and Test Cloud across N jobs so their wall clock
is no longer one 4-vCPU runner's pytest phase. Each slice collects the whole
selection, exactly as the unsplit job did (same ``-m``, same ``--ignore``), and
then keeps the items of the files assigned to it, deselecting the rest. So:

* every selected test runs in exactly one slice: each file is assigned to one
  slice, and every slice computes the same assignment from the same
  collection (as does every xdist worker, which xdist requires anyway);
* a file never spans two slices, so ``--dist loadfile``'s module-scoped
  fixtures are paid once, as before.

The assignment balances files by expected time: largest first, each to the
slice with the least time so far, ties to the lower slice and, among equal
costs, by path. A file's expected time is its selected item count times its
measured seconds per test, from ``tests/ci_shard_durations.json`` (written by
``scripts/ci_shard_durations.py`` from CI's logs); a file with no measurement
is costed at the measured mean. Balancing by count alone left one Standalone
slice with 1.5-2x the time of the others on every run (2026-11-02/03): a few
files, the migration and boot-refusal tests above all, cost seconds per test
where most cost milliseconds. The assignment runs after every other
``pytest_collection_modifyitems`` (``trylast``), so the counts are of what
``-m`` and the conftest's opt-in lanes left, the items that will actually run.
Stale measurements cost balance, never correctness.

Without ``--shard`` the plugin does nothing.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Mapping, Tuple

import pytest

_SHARD = re.compile(r"^([1-9][0-9]*)/([1-9][0-9]*)$")

#: Measured seconds per test, by file: scripts/ci_shard_durations.py.
DURATIONS = Path(__file__).with_name("ci_shard_durations.json")


def parse_shard(value: str) -> tuple[int, int]:
    """``"K/N"`` -> ``(K, N)``, with 1 <= K <= N; anything else raises."""
    match = _SHARD.match(value.strip())
    if not match:
        raise pytest.UsageError(f"--shard expects K/N (e.g. 2/3), got {value!r}")
    index, total = int(match.group(1)), int(match.group(2))
    if index > total:
        raise pytest.UsageError(f"--shard {value!r}: slice {index} of {total}")
    return index, total


def load_rates(path: Path = DURATIONS) -> Tuple[float, Dict[str, float]]:
    """(the default seconds per test, each measured file's seconds per test)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return float(data["default"]), {
        str(file): float(rate) for file, rate in data["files"].items()
    }


def file_costs(
    counts: Mapping[str, int], default: float, rates: Mapping[str, float]
) -> Dict[str, float]:
    """Each file's expected seconds: its selected tests times its rate."""
    return {path: n * rates.get(path, default) for path, n in counts.items()}


def assign_files(costs: Mapping[str, float], total: int) -> Dict[str, int]:
    """Each file -> the slice (1..``total``) that runs it.

    Largest-first onto the least-loaded slice. The order is fixed by
    (cost descending, path), so the result depends only on ``costs``, never
    on the order its keys were inserted in.
    """
    loads = [0.0] * total
    assignment: Dict[str, int] = {}
    for path in sorted(costs, key=lambda p: (-costs[p], p)):
        target = min(range(total), key=lambda i: (loads[i], i))
        assignment[path] = target + 1
        loads[target] += costs[path]
    return assignment


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--shard",
        default=None,
        metavar="K/N",
        help="Run only slice K of N of the selected tests (CI).",
    )


def _file_of(item: pytest.Item) -> str:
    return item.nodeid.split("::", 1)[0]


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(
    config: pytest.Config, items: List[pytest.Item]
) -> None:
    value = config.getoption("--shard")
    if value is None:
        return
    index, total = parse_shard(value)
    counts: Dict[str, int] = {}
    for item in items:
        path = _file_of(item)
        counts[path] = counts.get(path, 0) + 1
    assignment = assign_files(file_costs(counts, *load_rates()), total)
    kept = [item for item in items if assignment[_file_of(item)] == index]
    deselected = [item for item in items if assignment[_file_of(item)] != index]
    if deselected:
        config.hook.pytest_deselected(items=deselected)
    items[:] = kept
