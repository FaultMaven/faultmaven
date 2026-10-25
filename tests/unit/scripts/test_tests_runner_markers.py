"""The runner's ``-m`` keeps pytest.ini's opt-in lanes deselected (#2288).

pytest.ini's ``addopts`` deselects the live-model tests with
``-m "not real_llm"``, and pytest keeps only the last ``-m`` it is given. Every
preset mode, ``--benchmarks`` and ``--marker`` passed an ``-m`` of its own, so
each replaced that deselect and selected ``tests/integration/real_llm/``; and
``--benchmarks -m X`` or ``--ci -m X`` kept only ``X``.

The commands are built from real argv through the runner's own parser and
``build_pytest_command``, which launches nothing (``main`` would). Each ``-m``
is evaluated with pytest's own expression grammar.
"""

import configparser
import importlib.util
import shlex
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
        ["--unit", "-m", "not slow"],
        ["--ci", "-m", "security"],
        ["--benchmarks", "-m", "slow"],
    ],
    ids=" ".join,
)
def test_every_marker_selection_still_deselects_the_live_model_tests(runner, argv):
    command = _command(runner, argv)

    expressions = _marker_expressions(command)
    assert len(expressions) == 1, command  # a second -m would replace the first
    (expression,) = expressions
    assert not _selects(expression, "real_llm")
    assert not _selects(expression, "real_llm", "security", "benchmark", "cloud")


@pytest.mark.parametrize(
    ("argv", "selected", "deselected"),
    [
        (["--ci", "-m", "security"], [("security",)], [("security", "slow"), ()]),
        (["--benchmarks", "-m", "slow"], [("benchmark", "slow")], [("slow",)]),
        (["--ci-cloud"], [("cloud",)], [()]),
        (["--ci-nightly"], [(), ("slow",), ("benchmark",)], [("cloud",)]),
    ],
)
def test_every_selection_is_kept(runner, argv, selected, deselected):
    """Each selection is ANDed in; none replaces another."""
    (expression,) = _marker_expressions(_command(runner, argv))

    for marks in selected:
        assert _selects(expression, *marks), marks
    for marks in deselected:
        assert not _selects(expression, *marks), marks


def test_the_live_model_lane_runs_when_named(runner):
    (expression,) = _marker_expressions(_command(runner, ["-m", "real_llm"]))

    assert _selects(expression, "real_llm")


def test_no_selection_passes_no_marker_so_pytest_ini_applies(runner):
    assert _marker_expressions(_command(runner, ["--unit"])) == []


def test_the_runner_deselects_what_pytest_ini_deselects(runner):
    """``OPT_IN_MARKERS`` is exactly the declared markers pytest.ini's
    ``addopts`` ``-m`` deselects, so a new opt-in lane cannot be missed."""
    config = configparser.ConfigParser()
    config.read(REPO_ROOT / "pytest.ini")
    addopts = shlex.split(config["pytest"]["addopts"])
    (ini_expression,) = [
        addopts[i + 1] for i, part in enumerate(addopts) if part == "-m"
    ]
    declared = [
        line.split(":", 1)[0].strip()
        for line in config["pytest"]["markers"].splitlines()
        if line.strip()
    ]

    assert "real_llm" in declared  # the positive control: the walk reads markers
    opt_in = {marker for marker in declared if not _selects(ini_expression, marker)}
    assert opt_in == set(runner.OPT_IN_MARKERS)
