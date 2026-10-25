"""The live-model lane runs only when ``-m`` names it, whatever else ``-m`` says (#2288).

pytest.ini deselected it with ``addopts = ... -m "not real_llm"``, and pytest
keeps only the last ``-m`` it is given: ``pytest -m "not cloud"``, CI's
``pytest tests/ -m postgres`` and every preset of ``scripts/tests.py`` replaced
it, so any expression the lane's tests satisfied selected them. The deselect is
a collection hook in ``tests/conftest.py`` now, which no ``-m`` replaces.
"""

import configparser
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.conftest import (
    OPT_IN_MARKERS,
    markers_named_by,
    pytest_collection_modifyitems,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
LANE = "tests/integration/real_llm/"


@pytest.mark.parametrize(
    ("markexpr", "named"),
    [
        ("", set()),
        ("not cloud", {"cloud"}),
        ("real_llm", {"real_llm"}),
        ("not real_llm and security", {"real_llm", "security"}),
        # One identifier in pytest's grammar, which a word-boundary match
        # would have read as naming real_llm.
        ("integration or real_llm-x", {"integration", "real_llm-x"}),
        # A quoted keyword value is a string, not a marker name.
        ('skipif(reason="real_llm")', {"skipif", "reason"}),
        ('skipif(reason="open)', set()),  # the lexer refuses it, as pytest does
    ],
)
def test_the_names_are_read_with_pytests_own_lexer(markexpr, named):
    assert markers_named_by(markexpr) == named


class _Item:
    def __init__(self, *marks):
        self.marks = set(marks)

    def get_closest_marker(self, name):
        return name if name in self.marks else None


def _collect(markexpr, items):
    deselected = []
    config = SimpleNamespace(
        option=SimpleNamespace(markexpr=markexpr),
        hook=SimpleNamespace(pytest_deselected=lambda items: deselected.extend(items)),
    )
    pytest_collection_modifyitems(config, items)
    return deselected


@pytest.mark.parametrize(
    "markexpr",
    [
        "",
        "not cloud",
        "postgres",
        "integration or real_llm-x",
        'skipif(reason="real_llm")',
    ],
)
def test_the_lane_is_deselected_unless_named(markexpr):
    live, unit = _Item("real_llm", "integration"), _Item("unit")
    items = [live, unit]

    deselected = _collect(markexpr, items)

    assert items == [unit]
    assert deselected == [live]


@pytest.mark.parametrize("markexpr", ["real_llm", "integration and real_llm"])
def test_naming_the_lane_runs_it(markexpr):
    """``-m`` itself then decides, as for any other marker."""
    live = _Item("real_llm", "integration")
    items = [live]

    assert _collect(markexpr, items) == []
    assert items == [live]


def test_every_opt_in_lane_is_a_declared_marker():
    config = configparser.ConfigParser()
    config.read(REPO_ROOT / "pytest.ini")
    declared = {
        line.split(":", 1)[0].strip()
        for line in config["pytest"]["markers"].splitlines()
        if line.strip()
    }

    assert set(OPT_IN_MARKERS) <= declared


def test_pytest_ini_holds_no_marker_expression():
    """An ``-m`` in ``addopts`` is replaced by any ``-m`` on the command line,
    so it cannot keep a lane opt-in."""
    config = configparser.ConfigParser()
    config.read(REPO_ROOT / "pytest.ini")

    assert "-m" not in shlex.split(config["pytest"]["addopts"])


@pytest.mark.parametrize(
    ("argv", "collected"),
    [
        ([], 0),
        (["-m", "not cloud"], 0),  # the expression that used to select it
        (["-m", "real_llm"], 1),
    ],
)
def test_a_pytest_run_collects_the_lane_only_when_named(argv, collected):
    """Collected, never run: ``--collect-only`` makes no call."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-qq",
            "-p",
            "no:cacheprovider",
        ]
        + argv
        + [LANE],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode in (0, 5), result.stdout + result.stderr  # 5: none
    node_ids = [line for line in result.stdout.splitlines() if line.startswith(LANE)]
    assert len(node_ids) == collected, result.stdout
