"""`tests.wallclock.closure_plugin`: what "benchmarked code" means (#1989, #2014).

The shadow count rule is promoted on evidence from pull requests that touch no
benchmarked code, so the list is the denominator of that evidence. The first
version was a hand-written list (58 of 112 files), the second a static import
walk that missed the files only conftest and function bodies import (112 of
160). The plugin reads what a real run imported, so these tests run a real
(tiny) pytest session with it, in a FRESH interpreter, and hold the output to
the interpreter's own answer.

Deliberately unmarked, like its siblings in this directory.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.wallclock import closure_plugin

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def listed(tmp_path_factory) -> list:
    """The plugin's real output for a session that imports one module at the
    top of a test file and another lazily inside a test body."""
    work = tmp_path_factory.mktemp("closure")
    test_file = work / "test_probe.py"
    test_file.write_text(
        "from faultmaven.utils import serialization  # top-level import\n"
        "\n"
        "def test_probe():\n"
        "    import faultmaven.core.investigation.terminal_transitions  # lazy\n"
    )
    out = work / "closure.txt"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(test_file),
            "--rootdir",
            str(REPO_ROOT),
            "-c",
            str(REPO_ROOT / "pytest.ini"),
            "-q",
            "--no-cov",
            "-p",
            "tests.wallclock.closure_plugin",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        env={
            **os.environ,
            "PYTHONPATH": str(REPO_ROOT),
            closure_plugin.OUT_ENV: str(out),
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return out.read_text().splitlines()


class TestTheListedFiles:
    def test_it_is_sorted_unique_and_every_entry_is_a_file(self, listed):
        assert listed == sorted(set(listed))
        assert [n for n in listed if not (REPO_ROOT / n).is_file()] == []

    def test_a_top_level_and_a_lazy_import_are_both_listed(self, listed):
        """The lazy one is what the static walk missed (#2014)."""
        assert "faultmaven/utils/serialization.py" in listed
        assert "faultmaven/core/investigation/terminal_transitions.py" in listed

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
        none" unattainable and the criterion could never be met."""
        for name in (
            "faultmaven/main.py",
            "docs/development/testing/performance.md",
            "tests/unit/ci/test_closure_plugin.py",
        ):
            assert name not in listed, name

    def test_it_writes_nothing_without_the_output_variable(self, tmp_path):
        env = {k: v for k, v in os.environ.items() if k != closure_plugin.OUT_ENV}
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import tests.wallclock.closure_plugin as p;"
                "p.pytest_sessionfinish(None, 0)",
            ],
            cwd=REPO_ROOT,
            env={**env, "PYTHONPATH": str(REPO_ROOT)},
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr


class TestTheRefusals:
    def test_a_module_outside_the_checkout_is_refused(self):
        """‼ An editable install of another tree ahead of this one would make
        the closure a list of paths that exist nowhere here, and "touches
        none" true of every pull request. The plugin refuses instead."""
        with pytest.raises(RuntimeError, match="outside this checkout"):
            closure_plugin.repository_file(
                "faultmaven.modules.case", "/elsewhere/faultmaven/modules/case.py"
            )

    def test_a_module_that_is_not_ours_is_not_listed(self):
        assert closure_plugin.repository_file("json", json.__file__) is None
        assert closure_plugin.repository_file("sqlalchemy.orm", "/x/y.py") is None
        assert closure_plugin.repository_file("faultmaven", None) is None

    def test_a_module_inside_the_checkout_is_listed_relative_to_it(self):
        file = str(REPO_ROOT / "faultmaven" / "utils" / "serialization.py")
        assert (
            closure_plugin.repository_file("faultmaven.utils.serialization", file)
            == "faultmaven/utils/serialization.py"
        )

    def test_every_extra_is_a_real_file(self):
        """A renamed input would otherwise stay in the list and match
        nothing, forever."""
        for name in closure_plugin.EXTRA_FILES:
            assert (REPO_ROOT / name).is_file(), name
        for pattern in closure_plugin.EXTRA_GLOBS:
            assert list(REPO_ROOT.glob(pattern)), pattern
