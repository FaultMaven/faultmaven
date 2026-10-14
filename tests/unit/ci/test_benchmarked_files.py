"""`tests.wallclock.benchmarked_files`: what "benchmarked code" means (#1989).

The shadow count rule is promoted on evidence from pull requests that touch
no benchmarked code, so the list is the denominator of that evidence. The
first version of it was written by hand, covered 58 of the 112 `faultmaven`
files the benchmark modules import, and called a pull request that edited
`faultmaven/utils/serialization.py` clean. These tests hold the list to the
interpreter's own answer.

Deliberately unmarked, like its siblings in this directory.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.wallclock import benchmarked_files

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def listed() -> list:
    """The helper's real output, from a FRESH interpreter.

    ‼ Not `benchmarked_files.closure(...)` in this process: by the time this
    file runs, the test session has imported far more than the benchmarks do,
    so `sys.modules` here answers a different question. The helper is
    specified as a command for that reason.
    """
    result = subprocess.run(
        [sys.executable, "-m", "tests.wallclock.benchmarked_files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.splitlines()


class TestTheListedFiles:
    def test_it_is_sorted_unique_and_every_entry_is_a_file(self, listed):
        assert listed == sorted(set(listed))
        missing = [name for name in listed if not (REPO_ROOT / name).is_file()]
        assert missing == []

    def test_it_reaches_the_code_the_benchmarks_run(self, listed):
        """Positive controls spread across the closure, none of them in the
        five directories the hand-written list named."""
        for name in (
            # what the rows exercise
            "faultmaven/modules/case/infrastructure/sqlite_case_repository/repository.py",
            "faultmaven/modules/knowledge/infrastructure/persistence/knowledge_item_repository.py",
            "faultmaven/infrastructure/persistence/models.py",
            # reached only through imports: the file the hand-written list missed
            "faultmaven/utils/serialization.py",
            # the recorder and the helpers every row goes through
            "tests/wallclock/record.py",
            "tests/wallclock/assertions.py",
            "tests/benchmarks/budgets.py",
        ):
            assert name in listed, name

    def test_every_benchmark_module_is_listed(self, listed):
        """The A/B job runs `pytest tests/benchmarks/`: all of it."""
        for path in (REPO_ROOT / "tests" / "benchmarks").glob("test_*.py"):
            assert f"tests/benchmarks/{path.name}" in listed

    def test_the_inputs_no_import_names_are_listed(self, listed):
        for name in (
            "tests/conftest.py",
            "pytest.ini",
            "pyproject.toml",
            "requirements/test.txt",
        ):
            assert name in listed, name

    def test_the_instrument_is_listed(self, listed):
        """A pull request that edits the comparator or the job is not a
        clean sample of the rule's false-red rate."""
        assert "tests/wallclock/ab.py" in listed
        assert ".github/workflows/benchmarks.yml" in listed

    def test_it_is_a_closure_and_not_the_whole_repository(self, listed):
        """The negative control: a list of everything would make "touches
        none" unattainable and the criterion could never be met. These are
        imported by `tests/conftest.py` alone (the 44 files left out on
        purpose) or by nothing the benchmarks touch."""
        for name in (
            "faultmaven/main.py",
            "faultmaven/modules/agent/tools/base.py",
            "faultmaven/infrastructure/llm/router.py",
            "docs/development/testing/performance.md",
            "tests/unit/ci/test_benchmarked_files.py",
        ):
            assert name not in listed, name


class TestTheRefusals:
    def test_a_module_outside_the_checkout_is_refused(self):
        """‼ An editable install of another tree ahead of this one would make
        the closure a list of paths that exist nowhere here, and "touches
        none" true of every pull request. The helper refuses instead."""
        with pytest.raises(RuntimeError, match="outside this checkout"):
            benchmarked_files.repository_file(
                "faultmaven.modules.case", "/elsewhere/faultmaven/modules/case.py"
            )

    def test_a_module_that_is_not_ours_is_not_listed(self):
        assert benchmarked_files.repository_file("json", json.__file__) is None
        assert benchmarked_files.repository_file("sqlalchemy.orm", "/x/y.py") is None
        assert benchmarked_files.repository_file("faultmaven", None) is None

    def test_a_module_inside_the_checkout_is_listed_relative_to_it(self):
        file = str(REPO_ROOT / "faultmaven" / "utils" / "serialization.py")
        assert (
            benchmarked_files.repository_file("faultmaven.utils.serialization", file)
            == "faultmaven/utils/serialization.py"
        )

    def test_every_extra_is_a_real_file(self):
        """A renamed input would otherwise stay in the list and match
        nothing, forever."""
        for name in benchmarked_files.EXTRA_FILES:
            assert (REPO_ROOT / name).is_file(), name
        for pattern in benchmarked_files.EXTRA_GLOBS:
            assert list(REPO_ROOT.glob(pattern)), pattern
