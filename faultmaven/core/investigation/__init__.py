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
# ``__all__`` by tests/unit/test_import_isolation.py.
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
    """Resolve a re-exported name, or a submodule, on first access."""
    from importlib import import_module

    submodule = _SUBMODULE_BY_EXPORT.get(name)
    if submodule is not None:
        value = getattr(import_module(f".{submodule}", __name__), name)
        # Cache on the package so repeat access skips this path entirely.
        globals()[name] = value
        return value

    # Submodules. Importing a submodule binds it on its parent package, so the
    # old eager `__init__` made 15 of them reachable as plain attributes as a
    # side effect of pulling in hypothesis_manager and milestone_engine —
    # `investigation.terminal_transitions`, `.schemas`, `.causal_graph` and so
    # on. Resolving any real submodule keeps every one of those working.
    try:
        module = import_module(f".{name}", __name__)
    except ModuleNotFoundError as exc:
        # Only translate "this package has no such submodule". An import error
        # raised from *inside* a real submodule must propagate, not be reported
        # as a missing attribute.
        if exc.name == f"{__name__}.{name}":
            raise AttributeError(
                f"module {__name__!r} has no attribute {name!r}"
            ) from None
        raise
    globals()[name] = module
    return module


def __dir__() -> list:
    # `iter_modules` does not report namespace packages (a directory with no
    # __init__.py), so `prompts` is absent here even though `getattr` resolves
    # it via the fallback above. Introspection-only gap, deliberately not worth
    # a directory scan on every dir() call; the guard test does scan, so the
    # attribute itself stays covered.
    from pkgutil import iter_modules

    return sorted(set(__all__) | {m.name for m in iter_modules(__path__)})
