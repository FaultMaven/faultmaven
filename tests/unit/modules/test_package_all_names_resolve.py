"""Every name a package's ``__all__`` promises actually exists.

The module packages under ``faultmaven/modules/`` deliberately import nothing at
import time (circular-import avoidance), so ``__all__`` is the only statement a
package makes about its own contents — and nothing checks it. That makes it the
one kind of declaration that stays green while becoming false: deleting
``routes.py`` or ``agent_orchestration_service.py`` leaves its name sitting in
``__all__``, where the next reader takes it for an inventory. Both were stale
after the agent-service removal, alongside a ``tools`` list that had drifted
past a third of the tools it was supposed to name.

Checked by parsing rather than importing. ``from pkg import *`` would only prove
the names resolve *today*, at the cost of eagerly importing the packages that
were written specifically not to be — and it would pass on a lazy ``__getattr__``
that fabricates any attribute asked of it. Reading the source and the directory
answers the question these lists are actually making a claim about: is there
something here by that name?
"""

import ast
from pathlib import Path

import pytest

import faultmaven

pytestmark = pytest.mark.unit

_MODULES_ROOT = Path(faultmaven.__file__).parent / "modules"


def _declared_all(tree: ast.Module) -> list[str] | None:
    """The ``__all__`` string entries, or None if absent/not a literal list."""
    for node in tree.body:
        targets = (
            node.targets
            if isinstance(node, ast.Assign)
            else [node.target] if isinstance(node, ast.AnnAssign) else []
        )
        if not any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
            continue
        value = node.value
        if not isinstance(value, (ast.List, ast.Tuple)):
            return None
        return [
            e.value
            for e in value.elts
            if isinstance(e, ast.Constant) and isinstance(e.value, str)
        ]
    return None


def _bound_names(tree: ast.Module) -> set[str]:
    """Names the ``__init__`` itself binds — imports, defs, classes, assignments."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return names


def _packages_with_all():
    for init in sorted(_MODULES_ROOT.rglob("__init__.py")):
        if "__pycache__" in init.parts:
            continue
        tree = ast.parse(init.read_text())
        entries = _declared_all(tree)
        if entries:
            yield pytest.param(
                init, entries, tree, id=str(init.relative_to(_MODULES_ROOT.parent))
            )


_PACKAGES = list(_packages_with_all())


def test_the_sweep_found_the_packages():
    """A collection bug would make every case below vacuously pass."""
    assert (
        len(_PACKAGES) > 10
    ), f"only found {len(_PACKAGES)} packages declaring __all__"


@pytest.mark.parametrize("init,entries,tree", _PACKAGES)
def test_every_exported_name_exists(init, entries, tree):
    """Each entry is a submodule on disk, or a name the ``__init__`` binds."""
    folder = init.parent
    available = _bound_names(tree)

    missing = [
        name
        for name in entries
        if name not in available
        and not (folder / f"{name}.py").is_file()
        and not (folder / name / "__init__.py").is_file()
    ]

    assert not missing, (
        f"{init.relative_to(_MODULES_ROOT.parent)} exports names that do not exist: "
        f"{missing}. Either the submodule was deleted and the entry should go, or "
        "it was renamed and the entry should follow."
    )
