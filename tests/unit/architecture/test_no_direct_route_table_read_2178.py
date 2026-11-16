"""No direct ``app.routes`` read outside the flattener and ``route_policy`` (#2178).

From FastAPI 0.137.0 ``app.routes`` holds one ``_IncludedRouter`` placeholder
per ``include_router``, so a direct walk misses every included route. CI's
pinned 0.136.0 still copies them in, so such a walk passes every test there and
fails only on a newer FastAPI. The class recurred: #1305 and #1308 fixed it,
#1662 routed the last reads through ``api/route_enumeration.py``, and #1903
added a new one in ``main.py``, which raised at import on every FastAPI from
0.137.0.

Two modules may read the table: ``route_enumeration`` is the version gate, and
``route_policy`` walks composed sub-applications and reports when it cannot.
Every other reader goes through ``route_enumeration``. A read is any ``.routes``
attribute or a literal ``getattr(x, "routes")``, on any receiver: an included
router's own table holds placeholders too.
"""

import ast
import pathlib

import pytest

from tests.source_tree import PACKAGE_ROOT, parse_file

pytestmark = pytest.mark.unit

ALLOWED = {
    PACKAGE_ROOT / "api" / "route_enumeration.py",
    PACKAGE_ROOT / "api" / "middleware" / "route_policy.py",
}


def _route_table_reads(tree: ast.AST) -> list[int]:
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "routes":
            lines.append(node.lineno)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in ("getattr", "hasattr")
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "routes"
        ):
            lines.append(node.lineno)
    return lines


def _readers(paths) -> dict[pathlib.Path, list[int]]:
    found = {}
    for path in paths:
        lines = _route_table_reads(parse_file(path))
        if lines:
            found[path] = lines
    return found


def test_only_the_flattener_and_route_policy_read_the_route_table():
    files = sorted(PACKAGE_ROOT.rglob("*.py"))
    # A scan that walked nothing would pass.
    assert len(files) >= 200 and ALLOWED <= set(files)

    readers = _readers(files)

    # Both allowed modules really read it, so the allowance is not stale.
    assert ALLOWED <= set(readers)
    assert set(readers) == ALLOWED, {
        str(path.relative_to(PACKAGE_ROOT.parent)): lines
        for path, lines in readers.items()
        if path not in ALLOWED
    }


@pytest.mark.parametrize(
    "source",
    [
        "paths = [r.path for r in app.routes]\n",
        "def f(request):\n    for route in request.app.routes:\n        pass\n",
        "class A:\n    def f(self):\n        return len(self.app.routes)\n",
        "table = getattr(app, 'routes', [])\n",
        "table = getattr(app.router, 'routes')\n",
        "nested = hasattr(route, 'routes')\n",
    ],
)
def test_the_scan_flags_a_direct_read(tmp_path, source):
    probe = tmp_path / "probe_module.py"
    probe.write_text(source)

    assert list(_readers([probe])) == [probe]


@pytest.mark.parametrize(
    "source",
    [
        "from faultmaven.api.route_enumeration import iter_served_endpoints\n"
        "paths = [e.path for e in iter_served_endpoints(app)]\n",
        "routes = []\nfor route in routes:\n    pass\n",
        "doc = 'walks app.routes'\n",
    ],
)
def test_the_scan_leaves_the_flattener_alone(tmp_path, source):
    probe = tmp_path / "probe_module.py"
    probe.write_text(source)

    assert _readers([probe]) == {}
