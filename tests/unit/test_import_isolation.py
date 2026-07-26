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
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

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
    program = (
        "import json, sys\n"
        + "".join(f"import {name}\n" for name in module_names)
        + "print(json.dumps(sorted(sys.modules)))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env={"PYTHONPATH": str(REPO_ROOT), "PATH": "/usr/bin:/bin"},
        timeout=300,
    )
    assert result.returncode == 0, (
        f"subprocess failed importing {module_names}:\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    # The app logs to stdout on import; the JSON payload is the final line.
    return set(json.loads(result.stdout.strip().splitlines()[-1]))


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
    def test_previously_eager_submodules_stay_attribute_accessible(
        self, module_path: str
    ):
        """`pkg.submodule` must keep working for callers holding only the package.

        Importing a submodule binds it on its parent, so the eager re-exports
        these packages used to do made `shims.metrics` and
        `investigation.milestone_engine` reachable as attributes as a side
        effect. Going lazy silently dropped that until this test pinned it: the
        `from pkg import submodule` form still worked (the import machinery falls
        back to a submodule import), so nothing in-tree broke and the loss was
        invisible.
        """
        module = __import__(module_path, fromlist=["_EXPORTS_BY_SUBMODULE"])
        for submodule_name in module._EXPORTS_BY_SUBMODULE:
            resolved = getattr(module, submodule_name, None)
            assert resolved is not None, (
                f"{module_path}.{submodule_name} is no longer reachable as an "
                "attribute. The lazy __getattr__ must resolve the submodules it "
                "used to import eagerly, not just their re-exported names."
            )
            assert resolved.__name__ == f"{module_path}.{submodule_name}"
