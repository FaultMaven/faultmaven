"""No test module writes ``os.environ`` at import (#2291).

pytest imports every collected module before it runs any test, so a write at
module level reaches every test of the session, in any order, whatever its job
set. Six OAuth integration modules did it (``OAUTH_ENABLED``,
``OAUTH_REQUIRE_CONSENT``, ``SKIP_SERVICE_CHECKS``); they now build under
``rebuild_app_under`` and run under the ``oauth_environment`` fixture.

A write inside a function, a fixture or a test is not module level and is not
flagged; nor is source held in a string for a child process. A ``conftest.py``
is not a test module: setting the session's environment for every test is what
the root one is for.
"""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

TESTS = Path(__file__).resolve().parents[2]

_MUTATORS = {
    "update",
    "setdefault",
    "pop",
    "popitem",
    "clear",
    "__setitem__",
    "__delitem__",
}


def _is_environ(node: ast.AST) -> bool:
    """``os.environ`` or a bare ``environ``."""
    if isinstance(node, ast.Attribute):
        return node.attr == "environ" and isinstance(node.value, ast.Name)
    return isinstance(node, ast.Name) and node.id == "environ"


def _writes_environ(statement: ast.stmt) -> bool:
    targets = []
    if isinstance(statement, (ast.Assign, ast.Delete)):
        targets = statement.targets
    elif isinstance(statement, (ast.AugAssign, ast.AnnAssign)):
        targets = [statement.target]
    if any(isinstance(t, ast.Subscript) and _is_environ(t.value) for t in targets):
        return True
    if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
        func = statement.value.func
        if isinstance(func, ast.Attribute):
            if func.attr in _MUTATORS and _is_environ(func.value):
                return True
            if func.attr in {"putenv", "unsetenv"}:
                return True
    return False


def _module_level_statements(body):
    """Statements that run at import: the module's own, and those nested in
    its top-level ``if``/``try``/``with``/``for`` blocks, never a function's or
    a class's."""
    for statement in body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        yield statement
        for field in ("body", "orelse", "finalbody", "handlers"):
            nested = getattr(statement, field, None) or []
            for item in nested:
                inner = item.body if isinstance(item, ast.ExceptHandler) else [item]
                yield from _module_level_statements(inner)


def _offenders(source: str):
    tree = ast.parse(source)
    return [s.lineno for s in _module_level_statements(tree.body) if _writes_environ(s)]


@pytest.mark.parametrize(
    "source",
    [
        'import os\nos.environ["X"] = "1"\n',
        "import os\nos.environ.update(X='1')\n",
        'import os\nos.environ.setdefault("X", "1")\n',
        'import os\nos.environ.pop("X", None)\n',
        'import os\ndel os.environ["X"]\n',
        'import os\nos.putenv("X", "1")\n',
        'from os import environ\nenviron["X"] = "1"\n',
        'import os\nif True:\n    os.environ["X"] = "1"\n',
        'import os\ntry:\n    os.environ["X"] = "1"\nexcept Exception:\n    pass\n',
    ],
)
def test_the_scan_sees_a_module_level_write(source):
    assert _offenders(source), source


@pytest.mark.parametrize(
    "source",
    [
        'import os\ndef f():\n    os.environ["X"] = "1"\n',
        'import os\nclass C:\n    def m(self):\n        os.environ["X"] = "1"\n',
        'import os\nvalue = os.environ.get("X")\n',
        'SCRIPT = """\nimport os\nos.environ["X"] = "1"\n"""\n',
    ],
)
def test_the_scan_leaves_what_runs_later_alone(source):
    assert not _offenders(source), source


def test_no_test_module_writes_the_environment_at_import():
    offenders = {
        str(path.relative_to(TESTS.parent)): lines
        for path in sorted(TESTS.rglob("*.py"))
        if path.name != "conftest.py"
        and (lines := _offenders(path.read_text(encoding="utf-8")))
    }

    assert offenders == {}, (
        "These write os.environ at import, so every test the session collects "
        "runs with it. Set it in a fixture (monkeypatch, or a module-scoped "
        "MonkeyPatch.context) instead."
    )
