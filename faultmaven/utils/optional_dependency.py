"""Is an optional dependency actually usable?

Every optional dependency in this codebase is detected the same way — a
``try: import X`` that sets an ``X_AVAILABLE`` flag consulted later — and that
idiom answers the wrong question.

**pip and uv remove a package's FILES on uninstall but leave its
DIRECTORIES.** PEP 420 then resolves the leftover tree to a namespace package,
so ``import X`` SUCCEEDS and installs a module object exposing nothing:

    >>> import opik            # an empty site-packages/opik/ tree
    >>> opik.__file__
    None
    >>> opik.is_tracing_active
    AttributeError: module 'opik' has no attribute 'is_tracing_active'

The flag is then True for a directory containing nothing, and — because it is
cached at import and consulted far away — the lie is acted on somewhere else
entirely. That is what makes this worth a shared primitive rather than a guard
per site: the failure is silent and non-local.

It is not hypothetical. This repo's own venv was in that state for ``opik``
(#1231, 7 hard test failures) and the same predicate was live for
``sentence-transformers`` (#1233), where a wrong True reached an ``import
torch`` — the ~690 MiB that #868 exists to keep out of 512Mi CronJobs.

Two shapes are usually safe:

* ``from X import Y`` where ``Y`` is a SYMBOL — raises ImportError on a shadow,
  because the symbol is not there. Most of this codebase's optional imports are
  this shape.
* ``import X.Y`` — the submodule cannot resolve (ModuleNotFoundError).

``from X import Y`` where ``Y`` is a SUBPACKAGE is **not** safe: an uninstall
leaves nested directories too, so ``X/Y/`` resolves as a nested namespace
package and the from-import succeeds. Measured, not assumed. AST cannot tell a
symbol from a subpackage, which is why the enforcement scan does not treat a
from-import as exempting anything.

A **top-level bare** ``import X`` always succeeds against an empty directory.

**What this is NOT a general rule for.** ``origin is None`` means "namespace
package", and some distributions ship that way legitimately — measured in this
repo's venv, ``google``, ``zope`` and ``backports`` all resolve with
``origin is None`` and are correctly installed. So the spec-based check asks
about a REGULAR package.

**``attr`` is only consulted when the module is already imported.** Checking an
attribute requires the module object, and the whole point of the spec-based
path is to answer without importing (#868). So ``dependency_is_usable(name,
attr)`` gives the strong answer for a module in ``sys.modules`` and the
spec-based one otherwise — it never silently imports to satisfy ``attr``. Pass
it anyway: it costs nothing and it is what makes the already-imported path
correct, which is the path a namespace shadow actually reaches (an earlier
importer is how the shadow gets into ``sys.modules`` at all).
"""

import importlib.util
import logging
import sys
from types import ModuleType
from typing import Optional

logger = logging.getLogger(__name__)

__all__ = ["module_is_usable", "dependency_is_usable"]


def module_is_usable(module: Optional[ModuleType], attr: Optional[str] = None) -> bool:
    """True when ``module`` is a real package rather than a namespace shadow.

    Use this when the caller already holds the module — after its own
    ``try: import X`` — so nothing extra is imported.

    ``attr`` is the stronger test: it distinguishes a usable package from both
    a namespace shadow AND a partially-removed tree whose ``__init__.py``
    survived. Without it the check falls back to ``__file__``, which only
    separates a real package from a namespace one.

    Pass it when the symbol is STABLE. A version-sensitive symbol turns an
    upstream rename into the dependency silently reading as absent, which for
    an observability SDK means tracing switching itself off — see the call site
    in ``infrastructure/observability/tracing.py``, which deliberately passes
    nothing for that reason.
    """
    if module is None:
        return False
    if attr is not None:
        return hasattr(module, attr)
    return getattr(module, "__file__", None) is not None


def dependency_is_usable(name: str, attr: Optional[str] = None) -> bool:
    """True when ``name`` is installed and usable — WITHOUT importing it.

    For callers that must not pay the import: ``faultmaven.infrastructure
    .model_cache`` answers this for ``sentence_transformers`` in every process
    that touches the DI container, including cleanup CronJobs that never embed
    and were OOMKilled at 512Mi when torch came in behind it (#868).

    ``sys.modules`` is consulted FIRST for two reasons. It is the only correct
    answer for a module already present, and ``find_spec`` RAISES ValueError
    for a name in ``sys.modules`` whose ``__spec__`` is None. The test-suite
    doubles in ``tests/conftest.py`` used to be exactly that shape; since #942
    they carry a real ``ModuleSpec``, so they no longer trip it. The ordering
    stays regardless — the ValueError is a property of find_spec's contract for
    ANY spec-less entry, not of that one harness, and the branch is pinned
    against the live double by
    ``tests/unit/container/test_no_eager_embedding_load.py``.

    Being present is not being usable, though: importing a namespace shadow
    succeeds, so that branch discriminates exactly as ``module_is_usable``
    does. ``attr`` matters most there — a ``ModuleType`` double has no
    ``__file__`` either, so only a named symbol tells a stand-in apart from a
    shadow.

    On the spec path ``attr`` is NOT consulted: it would require importing the
    module, which this function exists to avoid. That path answers the weaker
    "is it a regular package" question. See the module docstring.
    """
    imported = sys.modules.get(name)
    if imported is not None:
        return module_is_usable(imported, attr)

    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, ValueError):
        # A parent package raising while being imported, or a spec-less
        # sys.modules entry. The second is answered above and cannot reach
        # here; both are kept because find_spec's contract allows them.
        return False

    if spec is None:
        return False
    if spec.origin is not None:
        return True

    # Namespace package. Say which directory, or an operator cannot act:
    # "never installed" and "shadowed by a leftover tree" are different
    # problems with different fixes, and nothing downstream names either.
    logger.warning(
        "optional dependency %r resolved to a namespace package (no module) at "
        "%s — an empty leftover directory shadows the real package; treating "
        "it as unavailable",
        name,
        list(spec.submodule_search_locations or ["<unknown>"]),
    )
    return False
