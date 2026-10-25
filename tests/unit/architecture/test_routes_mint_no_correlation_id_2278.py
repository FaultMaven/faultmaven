"""No route mints a correlation id of its own (#2278).

``LoggingMiddleware`` gives every request one correlation id: it is stamped on
every log line of the request and returned as the ``X-Correlation-ID``
header. Routes used to mint a second one with ``uuid4()``. They logged it in
their own ``extra``, set it as an ``x-correlation-id`` header that the
middleware then overwrote, and named it in a 500 body as the ``Error ID``. So
the id a user reported matched neither the header nor the request's lines. A
route that needs the id reads ``current_correlation_id()``.

This scan reads every module under ``faultmaven/modules/*/api/`` and
``faultmaven/api/v1/``. It flags a value built from a ``uuid`` call that
reaches a correlation id. That means a name, attribute, keyword or dict key
that says ``correlation``, or a header subscript that does. The value counts
whether it is built in place or reaches the sink through a local name.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import faultmaven

pytestmark = [pytest.mark.unit, pytest.mark.architecture]

_PACKAGE = Path(faultmaven.__file__).parent


def _scanned_modules() -> list[Path]:
    """The route and route-dependency modules: ``modules/*/api/`` and
    ``api/v1/``."""
    paths = [
        *(_PACKAGE / "modules").glob("*/api/**/*.py"),
        *(_PACKAGE / "api" / "v1").glob("**/*.py"),
    ]
    return sorted(paths)


def _uuid_callables(tree: ast.Module) -> tuple[set[str], set[str]]:
    """The names this module binds to the ``uuid`` module, and to its
    generators (``from uuid import uuid4``): ``uuid1`` … ``uuid8``, not
    ``UUID``, which parses an id rather than minting one."""
    modules, functions = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "uuid":
                    modules.add(alias.asname or "uuid")
        elif isinstance(node, ast.ImportFrom) and node.module == "uuid":
            for alias in node.names:
                if alias.name.startswith("uuid"):
                    functions.add(alias.asname or alias.name)
    return modules, functions


def _says_correlation(text: object) -> bool:
    return isinstance(text, str) and "correlation" in text.lower()


class _MintScan:
    """The correlation-id sinks in one module that a ``uuid`` call reaches."""

    def __init__(self, tree: ast.Module) -> None:
        self._modules, self._functions = _uuid_callables(tree)
        self._tree = tree

    def _is_mint(self, node: ast.AST) -> bool:
        if not isinstance(node, ast.Call):
            return False
        func = node.func
        if isinstance(func, ast.Name):
            return func.id in self._functions
        return (
            isinstance(func, ast.Attribute)
            and func.attr.startswith("uuid")
            and isinstance(func.value, ast.Name)
            and func.value.id in self._modules
        )

    def _carries_mint(self, value: ast.AST, minted: set[str]) -> bool:
        return any(
            self._is_mint(node)
            or (
                isinstance(node, ast.Name)
                and isinstance(node.ctx, ast.Load)
                and node.id in minted
            )
            for node in ast.walk(value)
        )

    @staticmethod
    def _target_says_correlation(target: ast.AST) -> bool:
        if isinstance(target, ast.Name):
            return _says_correlation(target.id)
        if isinstance(target, ast.Attribute):
            return _says_correlation(target.attr)
        if isinstance(target, ast.Subscript):
            key = target.slice
            return isinstance(key, ast.Constant) and _says_correlation(key.value)
        return False

    def _scope_sinks(self, scope: ast.AST) -> list[int]:
        """Lines in ``scope`` (a function, or the module's own statements)
        where a minted value reaches a correlation id."""
        minted: set[str] = set()
        # Two passes: a name minted after its first use in a loop still counts.
        for _ in range(2):
            for node in self._own_nodes(scope):
                if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value:
                    if self._carries_mint(node.value, minted):
                        targets = (
                            node.targets
                            if isinstance(node, ast.Assign)
                            else [node.target]
                        )
                        minted.update(t.id for t in targets if isinstance(t, ast.Name))
        lines = []
        for node in self._own_nodes(scope):
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value:
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                if any(self._target_says_correlation(t) for t in targets) and (
                    self._carries_mint(node.value, minted)
                ):
                    lines.append(node.lineno)
            elif isinstance(node, ast.keyword):
                if _says_correlation(node.arg) and self._carries_mint(
                    node.value, minted
                ):
                    lines.append(node.value.lineno)
            elif isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if (
                        isinstance(key, ast.Constant)
                        and _says_correlation(key.value)
                        and self._carries_mint(value, minted)
                    ):
                        lines.append(key.lineno)
        return lines

    @staticmethod
    def _own_nodes(scope: ast.AST):
        """``scope``'s nodes, short of the functions nested in it, which are
        scopes of their own."""
        stack = list(ast.iter_child_nodes(scope))
        while stack:
            node = stack.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            yield node
            stack.extend(ast.iter_child_nodes(node))

    def sinks(self) -> list[int]:
        if not (self._modules or self._functions):
            return []
        scopes = [self._tree] + [
            node
            for node in ast.walk(self._tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
        ]
        return sorted({line for scope in scopes for line in self._scope_sinks(scope)})


def _sinks(source: str) -> list[int]:
    return _MintScan(ast.parse(source)).sinks()


def test_the_scan_reads_the_route_modules():
    """Not vacuous: the globs reach the modules #2278 found minting."""
    relative = {p.relative_to(_PACKAGE).as_posix() for p in _scanned_modules()}
    assert {
        "modules/case/api/routes/cases.py",
        "modules/case/api/routes/conversation.py",
        "modules/case/api/routes/data.py",
        "modules/auth/api/auth.py",
        "api/v1/auth_dependencies.py",
    } <= relative


@pytest.mark.parametrize(
    "source",
    [
        # The shapes #2278 removed, one per row.
        "import uuid\ndef r():\n    correlation_id = str(uuid.uuid4())\n",
        "import uuid\ndef r(response):\n"
        '    response.headers["x-correlation-id"] = str(uuid.uuid4())\n',
        "import uuid\ndef r():\n"
        '    return Response(204, headers={"x-correlation-id": str(uuid.uuid4())})\n',
        "import uuid\ndef r():\n    return f(correlation_id=str(uuid.uuid4())[:8])\n",
        # Through a local name that does not say what it holds.
        "import uuid\ndef r():\n    cid = uuid.uuid4().hex\n"
        '    log(extra={"correlation_id": cid})\n',
        # Under an alias, and imported by name.
        "import uuid as u\ndef r(self):\n    self.correlation_id = str(u.uuid4())\n",
        "from uuid import uuid4 as mint\ndef r():\n"
        "    request_correlation: str = f'{mint()}'\n",
    ],
)
def test_the_scan_flags_every_minting_shape(source):
    """Positive controls: a scan that flags nothing proves nothing."""
    assert _sinks(source), source


@pytest.mark.parametrize(
    "source",
    [
        # A uuid that is not a correlation id.
        "import uuid\ndef r():\n    user_id = f'user_{uuid.uuid4().hex[:8]}'\n",
        # The request's own id.
        "from faultmaven.infrastructure.logging.coordinator import "
        "current_correlation_id\ndef r():\n"
        "    correlation_id = current_correlation_id()\n",
        # A minted name in ANOTHER function does not reach this one's sink.
        "import uuid\ndef a():\n    cid = str(uuid.uuid4())\n"
        'def b(cid):\n    log(extra={"correlation_id": cid})\n',
    ],
)
def test_the_scan_passes_what_is_not_a_minted_correlation_id(source):
    assert _sinks(source) == []


def test_no_route_mints_a_correlation_id_of_its_own():
    offenders = []
    for path in _scanned_modules():
        for line in _sinks(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.relative_to(_PACKAGE).as_posix()}:{line}")
    assert not offenders, (
        "A route minted a correlation id of its own. Read the request's with "
        "current_correlation_id() (faultmaven/infrastructure/logging/"
        "coordinator.py); LoggingMiddleware already sets X-Correlation-ID on "
        "the response and stamps the id on every log line (#2278): "
        + ", ".join(offenders)
    )
