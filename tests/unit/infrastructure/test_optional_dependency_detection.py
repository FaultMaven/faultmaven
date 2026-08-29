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
import pathlib
import sys
from types import ModuleType

import pytest

from faultmaven.utils.optional_dependency import dependency_is_usable, module_is_usable

pytestmark = pytest.mark.unit

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
PACKAGE_ROOT = REPO_ROOT / "faultmaven"

# Sites allowed to assign a literal True inside an ImportError-guarded try that
# also carries a top-level bare import. Empty on purpose: every such site has
# been converted. An entry here is a considered exemption and needs a reason —
# it is not a place to park a new violation.
ALLOWED: dict[str, str] = {}


def _import_guard_violations() -> list[str]:
    """Every ``FLAG = True`` inside a try that could have imported a shadow.

    Three shapes reach an availability flag, and only one is unsafe:

    * ``from X import Y`` — raises ImportError on a shadow (``Y`` is absent).
    * ``import X.Y``      — raises ModuleNotFoundError (the submodule cannot
                            resolve under a namespace package).
    * ``import X``        — SUCCEEDS. Only this one needs the guard.

    So a try block is flagged only when it contains a top-level bare import
    whose package is not also from-imported in the same block, AND assigns a
    literal ``True`` — which is exactly "this flag is unconditionally True once
    the import survived".
    """
    violations = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            if not _catches_import_error(node):
                continue

            from_roots = {
                stmt.module.split(".")[0]
                for stmt in node.body
                if isinstance(stmt, ast.ImportFrom) and stmt.module
            }
            bare = [
                alias.name
                for stmt in node.body
                if isinstance(stmt, ast.Import)
                for alias in stmt.names
                if "." not in alias.name and alias.name not in from_roots
            ]
            if not bare:
                continue

            for flag, lineno in _literal_true_assignments(node.body):
                rel = path.relative_to(REPO_ROOT)
                key = f"{rel}:{flag}"
                if key in ALLOWED:
                    continue
                violations.append(
                    f"{rel}:{lineno}: `{flag} = True` guarded only by "
                    f"`import {', '.join(bare)}` — a namespace-package shadow "
                    f"would set it True. Use module_is_usable() "
                    f"(faultmaven/utils/optional_dependency.py)."
                )
    return violations


def _catches_import_error(node: ast.Try) -> bool:
    names = {"ImportError", "ModuleNotFoundError"}
    for handler in node.handlers:
        if isinstance(handler.type, ast.Name) and handler.type.id in names:
            return True
        if isinstance(handler.type, ast.Tuple) and any(
            isinstance(e, ast.Name) and e.id in names for e in handler.type.elts
        ):
            return True
    return False


def _literal_true_assignments(body) -> list[tuple[str, int]]:
    found = []
    for stmt in body:
        for sub in ast.walk(stmt):
            if not isinstance(sub, ast.Assign):
                continue
            if not (isinstance(sub.value, ast.Constant) and sub.value.value is True):
                continue
            for target in sub.targets:
                if isinstance(target, ast.Name):
                    found.append((target.id, sub.lineno))
    return found


def test_no_availability_flag_trusts_a_bare_import():
    """A new flag of the shape that caused #1231 and #1233 fails here.

    This is the finding that closes the class. Fixing the call sites was
    necessary and repeatedly insufficient: each pass fixed the instances that
    were visible and the next review found the ones that were not.
    """
    violations = _import_guard_violations()
    assert not violations, "unguarded optional-dependency flags:\n" + "\n".join(
        f"  - {v}" for v in violations
    )


def test_the_scan_would_catch_a_violation():
    """...and the scan is not vacuous.

    A scan asserting "no violations" over a tree that has none passes just as
    well when the scan itself is broken. This feeds it the exact pre-fix shape
    and requires that it objects.
    """
    source = (
        "try:\n"
        "    import somepackage\n"
        "\n"
        "    SOMEPACKAGE_AVAILABLE = True\n"
        "except ImportError:\n"
        "    SOMEPACKAGE_AVAILABLE = False\n"
    )
    tree = ast.parse(source)
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.Try))

    assert _catches_import_error(node)
    assert _literal_true_assignments(node.body) == [("SOMEPACKAGE_AVAILABLE", 4)]


@pytest.mark.parametrize(
    "source, why",
    [
        pytest.param(
            "try:\n    from somepackage import thing\n\n    F = True\nexcept ImportError:\n    F = False\n",
            "a from-import raises on a shadow",
            id="from-import-is-safe",
        ),
        pytest.param(
            "try:\n    import somepackage.sub\n\n    F = True\nexcept ImportError:\n    F = False\n",
            "a submodule cannot resolve under a namespace package",
            id="dotted-import-is-safe",
        ),
    ],
)
def test_the_scan_does_not_flag_the_safe_shapes(source, why):
    """The two shapes that already fail correctly must not be reported.

    Flagging them would push ~26 correct sites through a pointless migration
    and train readers to ignore the scan.
    """
    tree = ast.parse(source)
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.Try))
    from_roots = {
        stmt.module.split(".")[0]
        for stmt in node.body
        if isinstance(stmt, ast.ImportFrom) and stmt.module
    }
    bare = [
        alias.name
        for stmt in node.body
        if isinstance(stmt, ast.Import)
        for alias in stmt.names
        if "." not in alias.name and alias.name not in from_roots
    ]
    assert bare == [], why


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
    import importlib

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
    import importlib

    importlib.invalidate_caches()

    with caplog.at_level("WARNING"):
        assert dependency_is_usable("_fm_dep_disk") is False

    assert str(tmp_path) in caplog.text
    assert "namespace package" in caplog.text


def test_dependency_is_usable_accepts_a_real_package(tmp_path, monkeypatch):
    (tmp_path / "_fm_dep_ok").mkdir()
    (tmp_path / "_fm_dep_ok" / "__init__.py").write_text("thing = 1\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    import importlib

    importlib.invalidate_caches()

    assert dependency_is_usable("_fm_dep_ok") is True


def test_dependency_is_usable_reports_absent_for_nothing_at_all():
    assert dependency_is_usable("_fm_definitely_not_installed_anywhere") is False
