"""Everything an Agent package advertises must actually be there (#982).

Most Agent packages do not eagerly import their contents — circular imports — so
each ``__init__.py`` advertises its submodules in ``__all__`` instead. Nothing
executes those names at import time, which is why they rot in the one direction
that hurts: ``__all__`` kept naming ``agent_orchestration_service`` and ``routes``
for as long as it took someone to read the file after the modules were deleted,
and ``knowledge_base`` had never existed at all. A star import of any of those
packages raises ``AttributeError`` on a name the package itself published.

The property is resolvability, not submodule-ness: ``kb_configs`` imports eagerly
and advertises classes, which is equally valid and equally worth checking. A name
counts as resolvable if it is an attribute of the package or imports as a
submodule of it — the two ways ``import *`` can succeed.

This walks the package tree rather than listing the packages, so a new Agent
subpackage is covered the day it is added.
"""

import importlib
import pkgutil

import pytest

import faultmaven.modules.agent

pytestmark = pytest.mark.unit


def _agent_packages():
    """Every package under ``faultmaven.modules.agent``, including the root."""
    packages = [faultmaven.modules.agent]
    for info in pkgutil.walk_packages(
        faultmaven.modules.agent.__path__, prefix="faultmaven.modules.agent."
    ):
        if info.ispkg:
            packages.append(importlib.import_module(info.name))
    return packages


def _advertised():
    """(package name, advertised name) for every entry in every ``__all__``."""
    return sorted(
        (package.__name__, name)
        for package in _agent_packages()
        for name in getattr(package, "__all__", ())
    )


def test_the_walk_finds_the_agent_packages():
    """Without this, an empty walk would make the real test vacuously green."""
    found = {package.__name__ for package in _agent_packages()}
    assert {
        "faultmaven.modules.agent",
        "faultmaven.modules.agent.domain",
        "faultmaven.modules.agent.domain.services",
        "faultmaven.modules.agent.tools",
    } <= found, f"the package walk missed known Agent packages: {sorted(found)}"

    assert len(_advertised()) > 20, "the __all__ collection came back suspiciously thin"


@pytest.mark.parametrize("package_name,exported", _advertised())
def test_every_advertised_name_resolves(package_name, exported):
    """``from <package> import *`` must not raise on a name the package published."""
    package = importlib.import_module(package_name)
    if hasattr(package, exported):
        return

    try:
        importlib.import_module(f"{package_name}.{exported}")
    except ModuleNotFoundError as exc:  # pragma: no cover - the failure path
        pytest.fail(
            f"{package_name}.__all__ advertises {exported!r}, but it is neither "
            f"an attribute of the package nor an importable submodule: {exc}. "
            "Either it was deleted and __all__ was not updated, or the name was "
            "never there at all."
        )
