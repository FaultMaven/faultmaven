"""One rule for "is this optional dependency usable", enforced by scan.

The defect: pip and uv remove a package's FILES on uninstall but leave its
DIRECTORIES, so PEP 420 resolves the leftover tree to a namespace package and a
bare ``import X`` SUCCEEDS against a directory containing nothing. Any
availability flag built that way then reads True for an absent dependency, is
cached at import, and is acted on somewhere else entirely.

It has been found three times in this repo — ``opik`` (#1231, seven hard test
failures), ``sentence-transformers`` (#1233, a wrong True reaching ``import
torch`` in a 512Mi CronJob) — and each time it was fixed one call site at a
time, which is why it kept coming back. The scan below is the part that stops
that: a NEW flag of the same shape fails here rather than waiting for the next
production incident or code review to notice it.

Enforcement is deliberately shape-based, not name-based, so it cannot be
sidestepped by calling the flag something else.
"""

import ast
import importlib.util
import pathlib
import sys
from types import ModuleType

import pytest

from faultmaven.utils.optional_dependency import dependency_is_usable, module_is_usable

pytestmark = pytest.mark.unit

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
PACKAGE_ROOT = REPO_ROOT / "faultmaven"

# A scan that walked zero files reports zero violations and passes. Moving
# this test one directory deeper would do exactly that, silently.
_MIN_FILES_EXPECTED = 200

# Sites allowed to assign a literal True inside an ImportError-guarded try that
# also carries a top-level bare import. Empty on purpose: every such site has
# been converted. An entry here is a considered exemption and needs a reason —
# it is not a place to park a new violation.
ALLOWED: dict[str, str] = {}


def _scan(paths) -> list[str]:
    """Every ``FLAG = True`` inside a try that could have imported a shadow.

    Takes the paths so a synthetic violating file can be pushed through this
    exact function — a scan only ever run against a clean tree passes just as
    well when the scan itself is broken.

    Only ONE import shape is unsafe: a **top-level bare** ``import X``, which
    succeeds against an empty directory. ``import X.Y`` cannot resolve the
    submodule. A same-block ``from X import Y`` is deliberately NOT treated as
    exempting the bare import: it protects only when ``Y`` is a symbol, and an
    uninstall leaves nested directories too, so ``from X import Ysubpkg``
    succeeds on a shadow just as the bare import does (measured). AST cannot
    tell a symbol from a subpackage, so the conservative reading is used — a
    false positive there costs one call to the helper.
    """
    violations = []
    for path in sorted(paths):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try) or not _catches_import_error(node):
                continue

            bare = [
                alias.name
                for stmt in node.body
                if isinstance(stmt, ast.Import)
                for alias in stmt.names
                if "." not in alias.name
            ]
            if not bare:
                continue

            # `else:` runs only when the try body did not raise, so a flag set
            # there is exactly as unguarded as one set in the body.
            for flag, lineno in _literal_true_assignments(node.body + node.orelse):
                key = f"{path.name}:{flag}"
                if key in ALLOWED:
                    continue
                violations.append(
                    f"{path}:{lineno}: `{flag} = True` guarded only by "
                    f"`import {', '.join(bare)}` — a namespace-package shadow "
                    f"would set it True. Use module_is_usable() "
                    f"(faultmaven/utils/optional_dependency.py)."
                )
    return violations


def _catches_import_error(node: ast.Try) -> bool:
    """Handlers that would swallow a failed import.

    ``Exception`` and a bare ``except:`` count. They catch ImportError too, so
    the same defect written that way is the same defect — and keying only on
    the literal name ``ImportError`` left a one-keyword bypass.
    """
    names = {"ImportError", "ModuleNotFoundError", "Exception", "BaseException"}
    for handler in node.handlers:
        if handler.type is None:  # bare `except:`
            return True
        if isinstance(handler.type, ast.Name) and handler.type.id in names:
            return True
        if isinstance(handler.type, ast.Tuple) and any(
            isinstance(e, ast.Name) and e.id in names for e in handler.type.elts
        ):
            return True
    return False


def _literal_true_assignments(body) -> list[tuple[str, int]]:
    """``F = True`` and ``F: bool = True`` alike — the annotation changes
    nothing about the defect, and matching only ``ast.Assign`` made a plain
    type hint an escape hatch."""
    found = []
    for stmt in body:
        for sub in ast.walk(stmt):
            is_true = (
                isinstance(sub, (ast.Assign, ast.AnnAssign))
                and isinstance(sub.value, ast.Constant)
                and sub.value.value is True
            )
            if not is_true:
                continue
            targets = sub.targets if isinstance(sub, ast.Assign) else [sub.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    found.append((target.id, sub.lineno))
    return found


def test_no_availability_flag_trusts_a_bare_import():
    """A new flag of the shape that caused #1231 and #1233 fails here.

    This is what closes the class. Fixing call sites was necessary and
    repeatedly insufficient: each pass fixed the instances that were visible
    and the next review found the ones that were not.
    """
    files = sorted(PACKAGE_ROOT.rglob("*.py"))
    # Otherwise a layout change makes this pass over an unscanned tree.
    assert PACKAGE_ROOT.is_dir(), f"{PACKAGE_ROOT} is not a directory"
    assert len(files) >= _MIN_FILES_EXPECTED, (
        f"scanned only {len(files)} files under {PACKAGE_ROOT} — the scan is "
        "not reaching the package, so a green result means nothing"
    )

    violations = _scan(files)
    assert not violations, "unguarded optional-dependency flags:\n" + "\n".join(
        f"  - {v}" for v in violations
    )


@pytest.mark.parametrize(
    "source, why",
    [
        pytest.param(
            "try:\n    import x\n\n    F = True\nexcept ImportError:\n    F = False\n",
            "the original shape",
            id="plain",
        ),
        pytest.param(
            "try:\n    import x\n\n    F: bool = True\nexcept ImportError:\n    F = False\n",
            "an annotation is not a guard",
            id="annotated-assignment",
        ),
        pytest.param(
            "try:\n    import x\nexcept ImportError:\n    F = False\nelse:\n    F = True\n",
            "`else` runs exactly when the import survived",
            id="else-clause",
        ),
        pytest.param(
            "try:\n    import x\n\n    F = True\nexcept Exception:\n    F = False\n",
            "a broad handler swallows the same ImportError",
            id="except-Exception",
        ),
        pytest.param(
            "try:\n    import x\n\n    F = True\nexcept:\n    F = False\n",
            "so does a bare except",
            id="bare-except",
        ),
        pytest.param(
            "try:\n    import x\n    from x import sub\n\n    F = True\nexcept ImportError:\n    F = False\n",
            "a from-import of a SUBPACKAGE also survives a shadow",
            id="from-import-does-not-exempt",
        ),
    ],
)
def test_the_scan_catches_every_spelling(tmp_path, source, why):
    """Fed through the REAL entry point, not a re-implementation of its filter.

    Asserting "no violations" over a clean tree passes just as well when the
    scan is broken; these push each spelling through ``_scan`` itself.
    """
    probe = tmp_path / "probe_module.py"
    probe.write_text(source)
    assert _scan([probe]), why


@pytest.mark.parametrize(
    "source, why",
    [
        pytest.param(
            "try:\n    from x import thing\n\n    F = True\nexcept ImportError:\n    F = False\n",
            "a symbol from-import raises on a shadow",
            id="from-import-only",
        ),
        pytest.param(
            "try:\n    import x.sub\n\n    F = True\nexcept ImportError:\n    F = False\n",
            "a submodule cannot resolve under a namespace package",
            id="dotted-import",
        ),
        pytest.param(
            "try:\n    import x\n\n    F = module_is_usable(x)\nexcept ImportError:\n    F = False\n",
            "the converted shape is the point of the exercise",
            id="already-converted",
        ),
    ],
)
def test_the_scan_leaves_the_safe_shapes_alone(tmp_path, source, why):
    """Flagging these would push ~27 correct sites through a pointless
    migration and train readers to ignore the scan."""
    probe = tmp_path / "probe_module.py"
    probe.write_text(source)
    assert _scan([probe]) == [], why


# --------------------------------------------------------------------------- #
# The primitive itself
# --------------------------------------------------------------------------- #


def _namespace_spec(tmp_path, monkeypatch, name, init_py=None):
    """A spec from real CPython import machinery, not a hand-built ModuleSpec.

    Fabricating ``ModuleSpec(name, loader=None, origin=None)`` would assert
    against this test's idea of a namespace package rather than the import
    system's.
    """
    package = tmp_path / name
    package.mkdir()
    if init_py is not None:
        (package / "__init__.py").write_text(init_py)
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    spec = importlib.util.find_spec(name)
    monkeypatch.delitem(sys.modules, name, raising=False)
    return spec


def test_a_namespace_package_really_has_no_origin(tmp_path, monkeypatch):
    """The CPython fact everything here rests on, pinned rather than assumed."""
    shadow = _namespace_spec(tmp_path, monkeypatch, "_fm_dep_ns")
    real = _namespace_spec(tmp_path, monkeypatch, "_fm_dep_real", "VALUE = 1\n")

    assert shadow is not None and shadow.origin is None
    assert real is not None and real.origin is not None


@pytest.mark.parametrize(
    "attrs, attr, expected",
    [
        pytest.param({}, None, False, id="shadow-has-no-file"),
        pytest.param({"__file__": "/x/__init__.py"}, None, True, id="real-has-file"),
        pytest.param({}, "client", False, id="shadow-lacks-the-symbol"),
        pytest.param(
            {"__file__": "/x/__init__.py"}, "client", False, id="partial-lacks-symbol"
        ),
        pytest.param({"client": object}, "client", True, id="stand-in-has-the-symbol"),
    ],
)
def test_module_is_usable_discriminates(attrs, attr, expected):
    """``attr`` is the stronger question: it rejects a half-removed tree whose
    ``__init__.py`` survived, which ``__file__`` alone accepts (row 4), and it
    accepts an attribute-carrying test double that has no ``__file__``
    (row 5) — the shape ``tests/conftest.py`` installs."""
    module = ModuleType("probe")
    for name, value in attrs.items():
        setattr(module, name, value)
    assert module_is_usable(module, attr) is expected


def test_module_is_usable_handles_none():
    assert module_is_usable(None) is False


def test_dependency_is_usable_prefers_sys_modules_over_find_spec(monkeypatch):
    """``find_spec`` RAISES ValueError for a spec-less ``sys.modules`` entry —
    the shape of the doubles ``tests/conftest.py`` installs. Asking it first
    would report those dependencies absent for a whole test session."""
    double = ModuleType("_fm_dep_double")
    double.thing = object
    assert double.__spec__ is None  # the trap
    monkeypatch.setitem(sys.modules, "_fm_dep_double", double)

    assert dependency_is_usable("_fm_dep_double", "thing") is True
    assert dependency_is_usable("_fm_dep_double", "absent") is False


def test_dependency_is_usable_rejects_an_imported_shadow(monkeypatch):
    """Importing a shadow succeeds, so "present in sys.modules" is not
    "usable" — the hole that made the first #1233 fix incomplete."""
    shadow = ModuleType("_fm_dep_shadow")
    shadow.__file__ = None
    shadow.__path__ = []
    monkeypatch.setitem(sys.modules, "_fm_dep_shadow", shadow)

    assert dependency_is_usable("_fm_dep_shadow") is False


def test_dependency_is_usable_rejects_a_namespace_shadow_on_disk(
    tmp_path, monkeypatch, caplog
):
    """End to end against a real empty directory, and it names the path.

    "Never installed" and "shadowed by a leftover tree" are different operator
    problems with different fixes, and nothing downstream distinguishes them.
    """
    (tmp_path / "_fm_dep_disk").mkdir()
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    with caplog.at_level("WARNING"):
        assert dependency_is_usable("_fm_dep_disk") is False

    assert str(tmp_path) in caplog.text
    assert "namespace package" in caplog.text


def test_dependency_is_usable_accepts_a_real_package(tmp_path, monkeypatch):
    (tmp_path / "_fm_dep_ok").mkdir()
    (tmp_path / "_fm_dep_ok" / "__init__.py").write_text("thing = 1\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    assert dependency_is_usable("_fm_dep_ok") is True


def test_dependency_is_usable_reports_absent_for_nothing_at_all():
    assert dependency_is_usable("_fm_definitely_not_installed_anywhere") is False
