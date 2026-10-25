"""The runner passes one ``-m``, with every selection kept (#2288).

pytest keeps only the last ``-m`` it is given. ``--ci -m X`` and
``--benchmarks -m X`` passed two, and ran only ``X``. The opt-in lanes are not
the runner's: tests/conftest.py deselects them (``tests/unit/test_opt_in_markers.py``).

The commands are built from real argv through the runner's own parser and
``build_pytest_command``, which launches nothing (``main`` would). Each ``-m``
is evaluated with pytest's own expression grammar.
"""

import importlib.util
from pathlib import Path

import pytest
from _pytest.mark.expression import Expression

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "tests.py"


@pytest.fixture(scope="module")
def runner():
    spec = importlib.util.spec_from_file_location("tests_runner", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _command(runner, argv):
    args, unknown = runner.build_parser().parse_known_args(argv)
    return runner.build_pytest_command(args, unknown)


def _marker_expressions(command):
    return [command[i + 1] for i, part in enumerate(command) if part == "-m"]


def _selects(expression, *marks):
    """Whether ``-m expression`` selects a test carrying ``marks``."""
    return Expression.compile(expression).evaluate(
        lambda name, /, **kwargs: name in marks
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["--ci"],
        ["--ci-full"],
        ["--ci-nightly"],
        ["--ci-cloud"],
        ["--benchmarks"],
        ["-m", "security"],
        ["--ci", "-m", "security"],
        ["--benchmarks", "-m", "slow"],
    ],
    ids=" ".join,
)
def test_one_marker_expression_is_passed(runner, argv):
    """A second ``-m`` would replace the first."""
    assert len(_marker_expressions(_command(runner, argv))) == 1


@pytest.mark.parametrize(
    ("argv", "selected", "deselected"),
    [
        (["--ci", "-m", "security"], [("security",)], [("security", "slow"), ()]),
        (["--benchmarks", "-m", "slow"], [("benchmark", "slow")], [("slow",)]),
        (["--ci-cloud"], [("cloud",)], [()]),
        (["--ci-nightly"], [(), ("slow",), ("benchmark",)], [("cloud",)]),
        # Each selection is parenthesised: unwrapped, "not slow and not cloud
        # and unit or integration" would select a slow integration test.
        (
            ["--ci", "-m", "unit or integration"],
            [("unit",), ("integration",)],
            [("slow", "integration"), ("cloud", "unit"), ()],
        ),
    ],
)
def test_every_selection_is_kept(runner, argv, selected, deselected):
    """Each selection is ANDed in; none replaces another."""
    (expression,) = _marker_expressions(_command(runner, argv))

    for marks in selected:
        assert _selects(expression, *marks), marks
    for marks in deselected:
        assert not _selects(expression, *marks), marks


def test_no_selection_passes_no_marker(runner):
    assert _marker_expressions(_command(runner, ["--unit"])) == []
