"""Run one slice of the suite: ``pytest -p tests.ci_shard --shard=K/N``.

CI splits Test Standalone and Test Cloud across N jobs so their wall clock
is no longer one 4-vCPU runner's pytest phase. Each slice collects the whole
selection, exactly as the unsplit job did (same ``-m``, same ``--ignore``), and
then keeps the items of the files assigned to it, deselecting the rest. So:

* every selected test runs in exactly one slice: each file is assigned to one
  slice, and every slice computes the same assignment from the same
  collection (as does every xdist worker, which xdist requires anyway);
* a file never spans two slices, so ``--dist loadfile``'s module-scoped
  fixtures are paid once, as before.

The assignment balances files by their selected item count: largest first,
each to the slice with the fewest items so far, ties to the lower slice and,
among equal counts, by path. It runs after every other
``pytest_collection_modifyitems`` (``trylast``), so the counts are of what
``-m`` and the conftest's opt-in lanes left, the items that will actually run.

Without ``--shard`` the plugin does nothing.
"""

from __future__ import annotations

import re
from typing import Dict, List, Mapping

import pytest

_SHARD = re.compile(r"^([1-9][0-9]*)/([1-9][0-9]*)$")


def parse_shard(value: str) -> tuple[int, int]:
    """``"K/N"`` -> ``(K, N)``, with 1 <= K <= N; anything else raises."""
    match = _SHARD.match(value.strip())
    if not match:
        raise pytest.UsageError(f"--shard expects K/N (e.g. 2/3), got {value!r}")
    index, total = int(match.group(1)), int(match.group(2))
    if index > total:
        raise pytest.UsageError(f"--shard {value!r}: slice {index} of {total}")
    return index, total


def assign_files(counts: Mapping[str, int], total: int) -> Dict[str, int]:
    """Each file -> the slice (1..``total``) that runs it.

    Largest-first onto the least-loaded slice. The order is fixed by
    (count descending, path), so the result depends only on ``counts``, never
    on the order its keys were inserted in.
    """
    loads = [0] * total
    assignment: Dict[str, int] = {}
    for path in sorted(counts, key=lambda p: (-counts[p], p)):
        target = min(range(total), key=lambda i: (loads[i], i))
        assignment[path] = target + 1
        loads[target] += counts[path]
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
    assignment = assign_files(counts, total)
    kept = [item for item in items if assignment[_file_of(item)] == index]
    deselected = [item for item in items if assignment[_file_of(item)] != index]
    if deselected:
        config.hook.pytest_deselected(items=deselected)
    items[:] = kept
