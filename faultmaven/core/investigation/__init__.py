"""Investigation Core Module

This module provides the core investigation framework for FaultMaven's
Data-Driven and Opportunistic troubleshooting system.

Components:
- hypothesis_manager: Hypothesis lifecycle management
- milestone_engine: Data-driven investigation engine

Import cost:
    These names are resolved lazily (PEP 562). They used to be eager, which
    meant importing ANY submodule of this package executed the whole
    investigation engine. `MilestoneEngine` reaches the knowledge module's
    contracts, whose package `__init__` pulls in the FastAPI routes and from
    there the observability stack; `hypothesis_manager` reaches the metrics
    shim. So a caller wanting one small module got all of it.

    That is not hypothetical: `SQLiteCaseRepository.save()` imports
    `terminal_transitions` from this package to decide terminal transitions.
    `terminal_transitions` is light, but the eager re-exports below made a
    single case write pay for the engine, the knowledge routes, and their
    optional cloud dependencies. See #849.

    Submodule imports (`from faultmaven.core.investigation.X import Y`) were
    always the cheap path and are unaffected; only attribute access on the
    package itself is deferred.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # static analysis only — never executed at runtime
    from faultmaven.core.investigation.hypothesis_manager import (
        HypothesisManager,
        create_hypothesis_manager,
    )
    from faultmaven.core.investigation.milestone_engine import MilestoneEngine

# Which submodule owns each re-exported name. Kept exhaustive against
# ``__all__`` by tests/unit/core/test_investigation_lazy_imports.py.
_EXPORTS_BY_SUBMODULE = {
    "hypothesis_manager": ("HypothesisManager", "create_hypothesis_manager"),
    "milestone_engine": ("MilestoneEngine",),
}

_SUBMODULE_BY_EXPORT = {
    name: submodule
    for submodule, names in _EXPORTS_BY_SUBMODULE.items()
    for name in names
}

__all__ = [
    "HypothesisManager",
    "create_hypothesis_manager",
    "MilestoneEngine",
]


def __getattr__(name: str):
    """Resolve a re-exported name by importing only its own submodule."""
    from importlib import import_module

    # The two submodules that used to be imported eagerly. Importing a submodule
    # binds it on its parent package, so `investigation.milestone_engine` worked
    # as a side effect of those imports. Resolve them explicitly to keep that
    # behaviour, and only them — the package's other submodules were never bound
    # this way, and resolving them now would mask typos.
    if name in _EXPORTS_BY_SUBMODULE:
        module = import_module(f".{name}", __name__)
        globals()[name] = module
        return module

    submodule = _SUBMODULE_BY_EXPORT.get(name)
    if submodule is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    value = getattr(import_module(f".{submodule}", __name__), name)
    # Cache on the package so repeat access skips this path entirely.
    globals()[name] = value
    return value


def __dir__() -> list:
    return sorted(__all__)
