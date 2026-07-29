"""Operator procedures are reachable as installed commands (#887).

The bug this guards: the container image COPYs ``faultmaven/`` and runs
``pip install --no-deps .`` — it never COPYs ``scripts/``, and the wheel
excludes it. So every documented in-pod procedure spelled
``python scripts/auth/<script>.py`` failed with "No such file or directory".
Moving the operator scripts into ``faultmaven/cli/`` and declaring them under
``[project.scripts]`` makes them ship with the package.

Two independent sources are checked so drift is caught from either side:

* the **declaration** in ``pyproject.toml`` — always live, in every environment,
  and it is what a fresh install would be built from;
* the **installed metadata** via ``importlib.metadata`` — what a pod would
  actually resolve on ``PATH``. Only observable where the distribution is
  installed (CI does ``pip install -e . --no-deps``), so it skips otherwise
  rather than passing vacuously.

Both resolve each target to a real callable, so a renamed module or a deleted
``main()`` fails here rather than in a pod at 3am.
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

#: Every operator procedure that must be runnable inside a pod. Adding a
#: command here without declaring it fails the first test below.
EXPECTED_COMMANDS = {
    "fm-provision-sso-org",
    "fm-provision-service-account",
    "fm-promote-platform-admin",
    "fm-demote-platform-admin",
    "fm-reset-kb",
}


def _declared_scripts() -> dict[str, str]:
    """``[project.scripts]`` as written on disk."""
    with PYPROJECT.open("rb") as handle:
        return tomllib.load(handle)["project"]["scripts"]


def _load_target(target: str):
    """Resolve a ``module:attr`` entry-point target the way the wrapper does."""
    module_path, _, attr = target.partition(":")
    module = importlib.import_module(module_path)
    return getattr(module, attr)


def _installed_console_scripts() -> dict[str, str]:
    """Console scripts recorded in this distribution's installed metadata.

    Skips when faultmaven is not installed into the environment (running
    straight from a checkout), because there is no metadata to read — not
    because the assertion would pass.
    """
    try:
        dist = importlib.metadata.distribution("faultmaven")
    except importlib.metadata.PackageNotFoundError:
        pytest.skip(
            "faultmaven is not installed in this environment; entry-point "
            "metadata is only observable after `pip install -e . --no-deps`"
        )
    return {
        ep.name: ep.value for ep in dist.entry_points if ep.group == "console_scripts"
    }


def test_pyproject_declares_every_operator_command():
    """The declaration is the source a wheel/image build reads."""
    assert set(_declared_scripts()) == EXPECTED_COMMANDS


@pytest.mark.parametrize("command", sorted(EXPECTED_COMMANDS))
def test_declared_target_resolves_to_a_callable(command):
    """A renamed module or a removed ``main()`` breaks here, not in a pod."""
    target = _declared_scripts()[command]
    assert target.startswith("faultmaven.cli."), (
        f"{command} must target the in-package CLI so it ships with the "
        f"install; got {target!r}"
    )
    assert callable(_load_target(target))


def test_installed_metadata_exposes_every_operator_command():
    """What a pod resolves on PATH is what pyproject declares.

    Fails on drift in either direction: a command declared but not installed
    (stale metadata) or installed but no longer expected.
    """
    assert set(_installed_console_scripts()) == EXPECTED_COMMANDS


@pytest.mark.parametrize("command", sorted(EXPECTED_COMMANDS))
def test_installed_entry_point_loads(command):
    """``ep.load()`` is exactly what the generated console script performs."""
    installed = _installed_console_scripts()
    assert command in installed, f"{command} is not installed as a command"
    (entry_point,) = [
        ep
        for ep in importlib.metadata.distribution("faultmaven").entry_points
        if ep.group == "console_scripts" and ep.name == command
    ]
    assert callable(entry_point.load())
