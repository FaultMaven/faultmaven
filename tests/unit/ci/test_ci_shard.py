"""`tests.ci_shard`: CI's split of Test Standalone and Test Cloud into slices.

The split is only safe if the slices PARTITION the selection: every selected
test in exactly one slice, nothing more, nothing less. The pure assignment is
tested directly; the plugin itself is run as CI runs it, in real pytest
sessions in a fresh interpreter, over a small generated suite, with and
without xdist, and with a ``-m`` selection.

Deliberately unmarked, like its siblings in this directory.
"""

from __future__ import annotations

import random
import re
import subprocess
import sys
from pathlib import Path
from typing import Set

import pytest

from tests.ci_shard import assign_files, parse_shard

REPO_ROOT = Path(__file__).resolve().parents[3]


# --- the assignment ---------------------------------------------------------


def test_every_file_is_assigned_exactly_once_to_a_slice_in_range():
    counts = {f"tests/test_{i}.py": (i * 7) % 13 + 1 for i in range(40)}
    assignment = assign_files(counts, 3)
    assert set(assignment) == set(counts)
    assert set(assignment.values()) <= {1, 2, 3}


def test_the_assignment_does_not_depend_on_insertion_order():
    counts = {f"tests/test_{i}.py": (i * 5) % 11 + 1 for i in range(30)}
    shuffled = list(counts.items())
    random.Random(7).shuffle(shuffled)
    assert assign_files(dict(shuffled), 4) == assign_files(counts, 4)


def test_slices_differ_by_at_most_the_largest_file():
    """Largest-first onto the least-loaded slice keeps any two slices within
    one file of each other."""
    counts = {f"tests/test_{i}.py": (i * 31) % 97 + 1 for i in range(60)}
    assignment = assign_files(counts, 3)
    loads = [0, 0, 0]
    for path, slice_ in assignment.items():
        loads[slice_ - 1] += counts[path]
    assert max(loads) - min(loads) <= max(counts.values())


def test_one_slice_takes_everything():
    assert set(assign_files({"a.py": 3, "b.py": 1}, 1).values()) == {1}


@pytest.mark.parametrize("value,expected", [("1/3", (1, 3)), (" 3/3 ", (3, 3))])
def test_parse_shard_accepts_k_of_n(value, expected):
    assert parse_shard(value) == expected


@pytest.mark.parametrize("value", ["0/3", "4/3", "3", "a/b", "1/0", "-1/2", ""])
def test_parse_shard_refuses_anything_else(value):
    with pytest.raises(pytest.UsageError):
        parse_shard(value)


# --- the plugin, in real sessions --------------------------------------------


@pytest.fixture(scope="module")
def suite(tmp_path_factory) -> Path:
    """Seven files of uneven size, one marked test per file, and a pytest.ini
    so the session's rootdir is this directory, not the repository."""
    root = tmp_path_factory.mktemp("shard_suite")
    (root / "pytest.ini").write_text(
        "[pytest]\nmarkers =\n    slow: deselected below\n"
    )
    for f in range(7):
        body = ["import pytest", ""]
        for t in range(f * 3 + 1):
            body.append(f"def test_{t}():\n    pass\n")
        body.append("@pytest.mark.slow\ndef test_slow():\n    pass\n")
        (root / f"test_file_{f}.py").write_text("\n".join(body))
    return root


_PASSED = re.compile(r"PASSED\s+(\S+::\S+)|(\S+::\S+)\s+PASSED")


def _ran(suite: Path, *args: str) -> Set[str]:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-v", *args],
        cwd=suite,
        env={"PYTHONPATH": str(REPO_ROOT), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )
    assert result.returncode in (0, 5), result.stdout[-3000:] + result.stderr[-3000:]
    return {next(g for g in m.groups() if g) for m in _PASSED.finditer(result.stdout)}


@pytest.mark.parametrize("xdist", [[], ["-n", "2"]], ids=["serial", "xdist"])
def test_the_slices_partition_the_selection(suite, xdist):
    whole = _ran(suite, "-m", "not slow", *xdist)
    assert len(whole) == sum(f * 3 + 1 for f in range(7))  # the slow ones gone
    slices = [
        _ran(suite, "-p", "tests.ci_shard", f"--shard={k}/3", "-m", "not slow", *xdist)
        for k in (1, 2, 3)
    ]
    assert all(slices), "a slice ran nothing"
    assert set().union(*slices) == whole
    assert sum(len(s) for s in slices) == len(whole), "a test ran in two slices"
    for one in slices:
        files = {nodeid.split("::")[0] for nodeid in one}
        for other in slices:
            if other is not one:
                assert not files & {n.split("::")[0] for n in other}, "file split"


def test_without_shard_the_plugin_changes_nothing(suite):
    assert _ran(suite, "-p", "tests.ci_shard") == _ran(suite)


def test_a_malformed_shard_is_a_usage_error(suite):
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "tests.ci_shard", "--shard=4/3"],
        cwd=suite,
        env={"PYTHONPATH": str(REPO_ROOT), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 4, result.stdout + result.stderr
    assert "slice 4 of 3" in result.stderr
