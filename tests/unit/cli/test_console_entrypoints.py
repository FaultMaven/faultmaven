"""Operator procedures are reachable as installed commands (#887).

The bug this guards: the container image COPYs ``faultmaven/`` and runs
``pip install --no-deps .`` — it never COPYs ``scripts/``, and the wheel
excludes it. So every documented in-pod procedure spelled
``python scripts/auth/<script>.py`` failed with "No such file or directory".
Moving the operator scripts into ``faultmaven/cli/`` and declaring them under
``[project.scripts]`` makes them ship with the package.

``pyproject.toml`` is the authority here — it is what a wheel or image build
reads — so the commands are not hand-copied into this file. Two things are
checked against it:

* every declared target resolves to a real callable, so a renamed module or a
  deleted ``main()`` fails here rather than in a pod at 3am;
* the **installed metadata** (what a pod resolves on ``PATH``) matches the
  declaration exactly. That is only observable where the distribution is
  installed — CI does ``pip install -e . --no-deps`` — so it skips in a bare
  checkout rather than passing vacuously.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
PYPROJECT = REPO_ROOT / "pyproject.toml"


def _declared_scripts() -> dict[str, str]:
    """``[project.scripts]`` as written on disk — the single source of truth."""
    with PYPROJECT.open("rb") as handle:
        return tomllib.load(handle)["project"]["scripts"]


#: Read once at collection so the commands can drive parametrization.
DECLARED = _declared_scripts()


@pytest.fixture(scope="module")
def installed_console_scripts():
    """This distribution's installed console-script entry points, by name.

    Skips when faultmaven is not installed into the environment (running
    straight from a checkout): there is no metadata to read — not an assertion
    that would have passed.
    """
    try:
        dist = importlib.metadata.distribution("faultmaven")
    except importlib.metadata.PackageNotFoundError:
        pytest.skip(
            "faultmaven is not installed in this environment; entry-point "
            "metadata is only observable after `pip install -e . --no-deps`"
        )
    return {ep.name: ep for ep in dist.entry_points if ep.group == "console_scripts"}


def test_the_declaration_ships_operator_commands_from_the_package():
    """A `[project.scripts]` that quietly emptied out would defeat every other
    check here, and a target outside the package would reintroduce the bug:
    only what lives under ``faultmaven/`` is copied into the image."""
    assert DECLARED, "no console entrypoints declared in pyproject.toml"
    off_package = {k: v for k, v in DECLARED.items() if not v.startswith("faultmaven.")}
    assert not off_package, (
        f"these targets are outside the installed package, so they will not "
        f"ship in the image: {off_package}"
    )


@pytest.mark.parametrize("command", sorted(DECLARED))
def test_declared_target_resolves_to_a_callable(command):
    """Resolve ``module:attr`` the way the generated wrapper does."""
    module_path, _, attr = DECLARED[command].partition(":")
    target = getattr(importlib.import_module(module_path), attr)
    assert callable(target), f"{command} -> {DECLARED[command]} is not callable"


def test_installed_metadata_matches_the_declaration(installed_console_scripts):
    """What a pod resolves on PATH is exactly what pyproject declares.

    Dict equality, so it fails on drift in either direction and on a *retargeted*
    command, not only a missing name. Each entry point is then loaded through
    ``ep.load()`` — the call the generated console script itself performs.
    """
    installed = {name: ep.value for name, ep in installed_console_scripts.items()}
    assert installed == DECLARED

    for name, ep in installed_console_scripts.items():
        assert callable(ep.load()), f"installed command {name} does not load"
