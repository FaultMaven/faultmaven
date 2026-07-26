"""Heavy optional dependencies must stay out of light import paths.

`sentence-transformers`/`torch`, `opik` and `presidio-analyzer` are large:
importing them costs seconds and hundreds of MB resident. They belong to
optional cloud features, but eager package ``__init__`` re-exports used to drag
them into paths that have nothing to do with those features — most visibly the
case persistence layer, where a single `save()` pulled the whole ML stack
(#849).

These are inverted import tests: they assert what must NOT be loaded. Each runs
in a SUBPROCESS on purpose. `sys.modules` is process-global and by the time any
given test executes, earlier tests in the session will have imported plenty —
so asserting against the in-process `sys.modules` would pass or fail depending
on test order and prove nothing.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_probe(body: str) -> object:
    """Run `body` in a fresh interpreter and return the JSON it writes.

    `body` is given a module-level name `OUT`: the path to write its JSON
    result to. Results come back through a file rather than stdout because the
    app logs on import — scraping stdout would break the moment anything (a
    logger, an atexit hook, a background thread) prints after the payload.

    The child inherits the real environment with PYTHONPATH prepended, rather
    than being handed a hand-built one: replacing os.environ wholesale drops
    HOME and any CI-supplied DATABASE_URL / AUTH_MODE / JWT_SECRET_KEY, and
    hardcoding PATH is not portable. It runs in a temporary cwd so that
    pydantic-settings cannot pick up the developer's `.env`.
    """
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "probe.json"
        program = f"OUT = {str(out)!r}\n" + body
        result = subprocess.run(
            [sys.executable, "-c", program],
            capture_output=True,
            text=True,
            cwd=tmp,
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
            timeout=300,
        )
        assert result.returncode == 0, (
            f"probe subprocess failed:\n--- stdout ---\n{result.stdout}\n"
            f"--- stderr ---\n{result.stderr}"
        )
        assert out.exists(), (
            f"probe wrote no result:\n--- stdout ---\n{result.stdout}\n"
            f"--- stderr ---\n{result.stderr}"
        )
        return json.loads(out.read_text())


# The optional dependencies whose import cost motivated #849. Swept as a set
# rather than spot-checked: the guarantee is "none of the heavy optional deps
# leak", not "torch specifically does not leak".
HEAVY_OPTIONAL_DEPS = (
    "torch",
    "sentence_transformers",
    "opik",
    "presidio_analyzer",
)


def _modules_after_importing(*module_names: str) -> set:
    """Import module_names in a fresh interpreter; return its sys.modules keys."""
    body = (
        "import json, sys\n"
        + "".join(f"import {name}\n" for name in module_names)
        + "open(OUT, 'w').write(json.dumps(sorted(sys.modules)))\n"
    )
    return set(_run_probe(body))


def _submodules_failing_attribute_access(package: str) -> list:
    """Return every submodule of `package` not reachable as an attribute."""
    body = f"""
import json, pkgutil, importlib
pkg = importlib.import_module({package!r})
failed = []
for info in pkgutil.iter_modules(pkg.__path__):
    try:
        getattr(pkg, info.name)
    except Exception as exc:
        failed.append(f"{{info.name}} ({{type(exc).__name__}})")
open(OUT, 'w').write(json.dumps(sorted(failed)))
"""
    return _run_probe(body)


# The two imports a case write performs: the repository module itself, then the
# `terminal_transitions` module that `SQLiteCaseRepository.save()` imports
# lazily to decide terminal transitions.
CASE_SAVE_IMPORTS = (
    "faultmaven.modules.case.infrastructure.sqlite_case_repository",
    "faultmaven.core.investigation.terminal_transitions",
)


@pytest.mark.unit
class TestCaseWritePathStaysLight:
    """The persistence layer must not pull optional ML/observability stacks."""

    @pytest.fixture(scope="class")
    def loaded(self) -> set:
        return _modules_after_importing(*CASE_SAVE_IMPORTS)

    @pytest.mark.parametrize("dependency", HEAVY_OPTIONAL_DEPS)
    def test_case_save_path_does_not_import(self, dependency: str, loaded: set):
        assert dependency not in loaded, (
            f"{dependency!r} is imported by the case-write path "
            f"({' + '.join(CASE_SAVE_IMPORTS)}). An eager package __init__ or a "
            "module-level import of an optional dependency has been "
            "reintroduced; import it inside the function that uses it. See #849."
        )

    def test_case_save_path_does_not_pull_investigation_engine(self, loaded: set):
        """`terminal_transitions` is light; the engine behind it is not."""
        assert "faultmaven.core.investigation.milestone_engine" not in loaded, (
            "importing faultmaven.core.investigation.terminal_transitions pulled "
            "in milestone_engine, so the package __init__ re-exports eagerly "
            "again. Keep them lazy (PEP 562 __getattr__)."
        )


@pytest.mark.unit
class TestShimSubmodulesAreIndependent:
    """Importing one shim must not pay for the other two."""

    @pytest.mark.parametrize(
        "shim, forbidden",
        [
            ("metrics", ("opik", "presidio_analyzer", "torch")),
            ("observability", ("presidio_analyzer",)),
            ("security", ("opik",)),
        ],
    )
    def test_shim_does_not_import_siblings_dependencies(self, shim, forbidden):
        loaded = _modules_after_importing(f"faultmaven.infrastructure.shims.{shim}")
        leaked = sorted(set(forbidden) & loaded)
        assert not leaked, (
            f"importing shims.{shim} also imported {leaked}, which belong to "
            "sibling shims. The package __init__ is re-exporting eagerly again."
        )


@pytest.mark.unit
class TestLazyPackagesStayConsistent:
    """A lazy package's name map must not drift from its ``__all__``.

    Without this, adding a name to ``__all__`` and forgetting the map yields an
    `AttributeError` only when some caller happens to touch that name.
    """

    @pytest.mark.parametrize(
        "module_path",
        [
            "faultmaven.infrastructure.shims",
            "faultmaven.core.investigation",
        ],
    )
    def test_all_names_are_mapped_and_resolvable(self, module_path: str):
        module = __import__(module_path, fromlist=["_SUBMODULE_BY_EXPORT"])
        exported = set(module.__all__)
        mapped = set(module._SUBMODULE_BY_EXPORT)

        assert exported == mapped, (
            f"{module_path}: __all__ and the lazy-import map disagree. "
            f"In __all__ only: {sorted(exported - mapped)}. "
            f"In map only: {sorted(mapped - exported)}."
        )
        # Every exported name must actually resolve through __getattr__.
        unresolvable = []
        for name in sorted(exported):
            try:
                getattr(module, name)
            except Exception as exc:  # noqa: BLE001 - report all, not the first
                unresolvable.append(f"{name} ({type(exc).__name__}: {exc})")
        assert (
            not unresolvable
        ), f"{module_path}: names in __all__ that do not resolve: {unresolvable}"

    @pytest.mark.parametrize(
        "module_path",
        [
            "faultmaven.infrastructure.shims",
            "faultmaven.core.investigation",
        ],
    )
    def test_unknown_attribute_raises_attribute_error(self, module_path: str):
        module = __import__(module_path, fromlist=["__name__"])
        with pytest.raises(AttributeError):
            module.this_name_does_not_exist

    @pytest.mark.parametrize(
        "module_path",
        [
            "faultmaven.infrastructure.shims",
            "faultmaven.core.investigation",
        ],
    )
    def test_every_submodule_stays_attribute_accessible(self, module_path: str):
        """`pkg.submodule` must keep working for callers holding only the package.

        Importing a submodule binds it on its parent, so the eager re-exports
        these packages used to do made their submodules reachable as plain
        attributes as a side effect — 15 of them on `core.investigation`, not
        just the two whose names were re-exported. Going lazy silently dropped
        that: the `from pkg import submodule` form still works (the import
        machinery falls back to a submodule import) and mock.patch string
        targets resolve through importlib, so nothing in-tree broke and the
        loss was invisible.

        This sweeps every submodule pkgutil can discover rather than the two in
        the re-export map — checking only the mapped names would certify a
        parity it never tested, which is exactly how the gap survived its first
        version. Runs in a subprocess: resolving them imports them, and that
        would pull the heavy stacks into this session's sys.modules and
        undermine the isolation tests above.
        """
        failed = _submodules_failing_attribute_access(module_path)
        assert not failed, (
            f"{module_path}: submodules not reachable as attributes: {failed}. "
            "The lazy __getattr__ must fall back to importing a real submodule, "
            "not only resolve re-exported names."
        )
