"""The live-model lane runs only when ``-m`` names it, whatever else ``-m`` says (#2288).

pytest.ini deselected it with ``addopts = ... -m "not real_llm"``, and pytest
keeps only the last ``-m`` it is given: ``pytest -m "not cloud"``, CI's
``pytest tests/ -m postgres`` and every preset of ``scripts/tests.py`` replaced
it, so any expression the lane's tests satisfied selected them. The deselect is
a collection hook in ``tests/conftest.py`` now, which no ``-m`` replaces.
"""

import configparser
import os
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.conftest import (
    OPT_IN_MARKERS,
    markers_named_by,
    pytest_collection_modifyitems,
)

pytest_plugins = ["pytester"]

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]


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


_SUITE = """
import pytest


@pytest.mark.real_llm
def test_live():
    pass


def test_plain():
    pass
"""


@pytest.mark.parametrize(
    ("argv", "collected"),
    [
        ([], ["test_plain"]),
        (["-m", "not cloud"], ["test_plain"]),  # the shape that used to select it
        (["-m", "real_llm"], ["test_live"]),
    ],
)
def test_a_pytest_run_collects_the_lane_only_when_named(
    pytester, monkeypatch, argv, collected
):
    """A suite of one lane test and one other, collected (never run) in a child
    process with the root conftest loaded, the way
    ``test_a_warning_fails_the_run.py`` runs its suites."""
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(filter(None, [str(REPO_ROOT), os.environ.get("PYTHONPATH")])),
    )
    pytester.makeini("[pytest]\nmarkers =\n    real_llm: the live-model lane\n")
    pytester.makepyfile(test_lane=_SUITE)

    result = pytester.runpytest_subprocess(
        "-p", "no:cacheprovider", "-p", "tests.conftest", "--collect-only", "-q", *argv
    )

    names = [
        line.rsplit("::", 1)[-1]
        for line in result.stdout.lines
        if line.startswith("test_lane.py::")
    ]
    assert names == collected, result.stdout.str()
