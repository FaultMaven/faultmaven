"""No test module writes ``os.environ`` at import (#2291).

pytest imports every collected module before it runs any test, so a write at
module level reaches every test of the session, in any order, whatever its job
set. Six OAuth integration modules did it (``OAUTH_ENABLED``,
``OAUTH_REQUIRE_CONSENT``, ``SKIP_SERVICE_CHECKS``); they now build under
``rebuild_app_under`` and run under the ``oauth_environment`` fixture.

A write inside a function, a fixture or a test is not module level and is not
flagged (its decorators and default arguments are); nor is source held in a
string for a child process, nor the body of a ``__main__`` guard. The root ``tests/conftest.py`` is exempt: setting the
session's environment for every test is what it is for. Any other conftest is
scanned, because its import-time write would leak the same way.
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
_LOADERS = {"load_dotenv"}
_PATCHERS = {"setenv", "delenv"}


def _is_environ(node: ast.AST) -> bool:
    """``os.environ`` or a bare ``environ``."""
    if isinstance(node, ast.Attribute):
        return node.attr == "environ" and isinstance(node.value, ast.Name)
    return isinstance(node, ast.Name) and node.id == "environ"


def _called_name(call: ast.Call) -> str:
    func = call.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def _is_environ_write_call(call: ast.Call) -> bool:
    name = _called_name(call)
    func = call.func
    if name in _MUTATORS and isinstance(func, ast.Attribute):
        return _is_environ(func.value)
    if name in {"putenv", "unsetenv"} or name in _LOADERS or name in _PATCHERS:
        return True
    # mock.patch.dict(os.environ, ...).start()
    if name == "start" and isinstance(func, ast.Attribute):
        inner = func.value
        return (
            isinstance(inner, ast.Call)
            and _called_name(inner) == "dict"
            and bool(inner.args)
            and _is_environ(inner.args[0])
        )
    return False


def _nodes_run_now(node: ast.AST):
    """Every node of a statement that runs when the statement does: not the
    bodies of lambdas, functions or classes defined inside it."""
    for child in ast.iter_child_nodes(node):
        if isinstance(
            child, (ast.Lambda, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            continue
        yield child
        yield from _nodes_run_now(child)


def _writes_environ(statement: ast.stmt) -> bool:
    targets = []
    if isinstance(statement, (ast.Assign, ast.Delete)):
        targets = statement.targets
    elif isinstance(statement, (ast.AugAssign, ast.AnnAssign)):
        targets = [statement.target]
    for target in targets:
        if isinstance(target, ast.Subscript) and _is_environ(target.value):
            return True
        # os.environ |= {...}
        if isinstance(statement, ast.AugAssign) and _is_environ(target):
            return True
    return any(
        isinstance(node, ast.Call) and _is_environ_write_call(node)
        for node in _nodes_run_now(statement)
    )


def _is_main_guard(statement: ast.stmt) -> bool:
    """``if __name__ == "__main__":``, whose body pytest's import never runs."""
    test = getattr(statement, "test", None)
    return (
        isinstance(statement, ast.If)
        and isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Eq)
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value == "__main__"
    )


def _module_level_statements(body):
    """What runs at import, as statements and expressions: the module's own
    statements; the headers and every block of its compound statements
    (``try``/``except*``, ``match`` and loops included), except the body of a
    ``__main__`` guard; class bodies, bases and keywords; and the decorators
    and default arguments of functions, never their bodies."""
    for statement in body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield from statement.decorator_list
            yield from statement.args.defaults
            yield from (d for d in statement.args.kw_defaults if d is not None)
            continue
        if isinstance(statement, ast.ClassDef):
            yield from statement.decorator_list
            yield from statement.bases
            yield from (keyword.value for keyword in statement.keywords)
            yield from _module_level_statements(statement.body)
            continue
        if not any(
            isinstance(getattr(statement, field, None), list)
            for field in ("body", "cases")
        ):
            yield statement  # a simple statement
            continue
        for field in ("test", "iter", "subject"):
            header = getattr(statement, field, None)
            if header is not None:
                yield header
        yield from (item.context_expr for item in getattr(statement, "items", []))
        blocks = ["orelse", "finalbody", "handlers", "cases"]
        if not _is_main_guard(statement):
            blocks.insert(0, "body")
        for field in blocks:
            for item in getattr(statement, field, None) or []:
                if isinstance(item, ast.ExceptHandler):
                    yield from _module_level_statements(item.body)
                elif isinstance(item, ast.match_case):
                    if item.guard is not None:
                        yield item.guard
                    yield from _module_level_statements(item.body)
                else:
                    yield from _module_level_statements([item])


def _offenders(source: str):
    tree = ast.parse(source)
    found = []
    for node in _module_level_statements(tree.body):
        if isinstance(node, ast.stmt):
            hit = _writes_environ(node)
        else:  # an expression in a block's header
            hit = any(
                isinstance(n, ast.Call) and _is_environ_write_call(n)
                for n in [node, *_nodes_run_now(node)]
            )
        if hit:
            found.append(node.lineno)
    return found


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
        'import os\nclass C:\n    os.environ["X"] = "1"\n',
        'import os\nvalue = os.environ.pop("X", None)\n',
        'import os\nvalue = os.environ.setdefault("X", "1")\n',
        'import os\nos.environ |= {"X": "1"}\n',
        "import os\nfrom unittest import mock\nmock.patch.dict(os.environ, X='1').start()\n",
        "from dotenv import load_dotenv\nload_dotenv()\n",
        'import pytest\npytest.MonkeyPatch().setenv("X", "1")\n',
        'import os\ntry:\n    os.environ["X"] = "1"\nexcept* ValueError:\n    pass\n',
        'import os\nmatch 1:\n    case 1:\n        os.environ["X"] = "1"\n',
        'import os\nif __name__ != "__main__":\n    os.environ["X"] = "1"\n',
        'import os\nif __name__ == "__main__":\n    pass\nelse:\n    os.environ["X"] = "1"\n',
        'import os\nfor _ in [os.environ.pop("X", None)]:\n    pass\n',
        'import os\n@print(os.environ.setdefault("X", "1"))\ndef f():\n    pass\n',
        'import os\ndef f(x=os.environ.pop("X", None)):\n    pass\n',
        'import os\nclass C(os.environ.setdefault("X", "1") and object):\n    pass\n',
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
        'import os\nif __name__ == "__main__":\n    os.environ["X"] = "1"\n',
        'import os\nhandler = lambda: os.environ.pop("X")\n',
    ],
)
def test_the_scan_leaves_what_runs_later_alone(source):
    assert not _offenders(source), source


def test_no_test_module_writes_the_environment_at_import():
    offenders = {
        str(path.relative_to(TESTS.parent)): lines
        for path in sorted(TESTS.rglob("*.py"))
        if path != TESTS / "conftest.py"
        and (lines := _offenders(path.read_text(encoding="utf-8")))
    }

    assert offenders == {}, (
        "These write os.environ at import, so every test the session collects "
        "runs with it. Set it in a fixture (monkeypatch, or a module-scoped "
        "MonkeyPatch.context) instead."
    )
