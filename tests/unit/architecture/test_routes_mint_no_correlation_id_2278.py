"""No route mints a correlation id of its own (#2278).

``LoggingMiddleware`` gives every request one correlation id: it is stamped on
every log line of the request and returned as the ``X-Correlation-ID``
header. Routes used to mint a second one with ``uuid4()``. They logged it in
their own ``extra``, set it as an ``x-correlation-id`` header that the
middleware then overwrote, and named it in a 500 body as the ``Error ID``. So
the id a user reported matched neither the header nor the request's lines. A
route that needs the id reads ``current_correlation_id()``.

Middleware inside ``LoggingMiddleware`` is held to the same rule: it answers
within the request's context, so a body it writes names the request's id.
``IdempotencyMiddleware`` minted one for each of its 400 and 409 bodies.

**What is scanned.** Every module under ``faultmaven/api/`` and
``faultmaven/modules/*/api/``, and ``faultmaven/main.py``, less
:data:`_OUTSIDE_THE_REQUEST`.

**What is a mint.** A call of a ``uuid`` generator (``uuid1`` … ``uuid8``,
never ``UUID``, which parses an id) or a ``secrets.token_*``. A call of a
function or method that returns a minted value also counts, whether it is
defined in the same module or imported from another module of the package.

**Where a mint must not reach.** Any place that says it holds an id meant to
correlate a request (:data:`_SINK_WORDS`):

* a name, attribute or subscript key;
* a keyword argument;
* a dict key;
* a string argument beside it in the same call
  (``headers.append("x-correlation-id", …)``, ``log("correlation %s", …)``);
* the literal text of the f-string, ``%`` or ``+`` or ``.format`` that
  carries it (``f"Error ID: {…}"``).

A minted value counts whether it is built in place or reaches the sink through
a local name, including one an enclosing function minted. An entity id
(``report_id``, ``case_id``) is none of these, so it may be minted freely.
"""

from __future__ import annotations

import ast
import functools
from dataclasses import dataclass, field
from pathlib import Path

import pytest

import faultmaven

pytestmark = [pytest.mark.unit, pytest.mark.architecture]

_PACKAGE = Path(faultmaven.__file__).parent

#: Middleware that runs OUTSIDE ``LoggingMiddleware``, so there is no request
#: id for it to read. ``ContractProbeMiddleware`` is registered after
#: ``LoggingMiddleware`` in ``setup_middleware``, which makes it an outer layer.
#: Its own short id names only its probe lines and reaches no response.
#: ``test_the_exempt_middleware_is_registered_outside_logging`` keeps that
#: reason true.
_OUTSIDE_THE_REQUEST = frozenset({"api/middleware/contract_probe.py"})

#: Words that mark a place as holding a request-correlating id. Matched
#: case-insensitively against identifiers and literal text.
_SINK_WORDS = ("correlation", "error_id", "error id", "error-id", "errorid")

#: Modules whose calls mint an id, by the prefix their generators share.
_GENERATORS = {"uuid": "uuid", "secrets": "token_"}

_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


def _scanned_modules(root: Path) -> list[Path]:
    """The request-path modules under the package at ``root``."""
    paths = [
        *(root / "api").glob("**/*.py"),
        *(root / "modules").glob("*/api/**/*.py"),
        root / "main.py",
    ]
    return sorted(
        p for p in paths if p.relative_to(root).as_posix() not in _OUTSIDE_THE_REQUEST
    )


def _says_sink(text: object) -> bool:
    return isinstance(text, str) and any(w in text.lower() for w in _SINK_WORDS)


def _module_name(root: Path, path: Path) -> str:
    parts = list(path.relative_to(root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join([root.name, *parts])


def _absolute(module: str, is_package: bool, node: ast.ImportFrom) -> str:
    """The module an ``ImportFrom`` in ``module`` names, relative or not."""
    if not node.level:
        return node.module or ""
    base = module.split(".")
    if not is_package:
        base = base[:-1]
    base = base[: len(base) - (node.level - 1)]
    return ".".join(base + ([node.module] if node.module else []))


@dataclass
class _Module:
    """What one module binds that can mint."""

    name: str
    tree: ast.Module
    #: Aliases of ``uuid`` / ``secrets``, to the prefix their generators share.
    generator_modules: dict[str, str] = field(default_factory=dict)
    #: Names bound to a generator (``from uuid import uuid4``).
    generators: set[str] = field(default_factory=set)
    #: Names bound by ``from <package module> import …``: alias -> (module, name).
    package_imports: dict[str, tuple[str, str]] = field(default_factory=dict)

    @classmethod
    def read(cls, name: str, tree: ast.Module, is_package: bool) -> _Module:
        module = cls(name, tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in _GENERATORS:
                        prefix = _GENERATORS[alias.name]
                        module.generator_modules[alias.asname or alias.name] = prefix
            elif isinstance(node, ast.ImportFrom):
                source = _absolute(name, is_package, node)
                for alias in node.names:
                    bound = alias.asname or alias.name
                    if source in _GENERATORS:
                        if alias.name.startswith(_GENERATORS[source]):
                            module.generators.add(bound)
                    elif source.split(".")[0] == name.split(".")[0]:
                        module.package_imports[bound] = (source, alias.name)
        return module


#: Calls that keep an id an id: ``str(uuid4())``, ``f"{x}".lower()``.
_ID_PRESERVING_CALLS = frozenset({"str", "repr", "format"})
_ID_PRESERVING_METHODS = frozenset(
    {"format", "replace", "lower", "upper", "strip", "join"}
)


class _Analysis:
    """Mints and sinks in one module, given the package's minting functions."""

    def __init__(self, module: _Module, package_minters: frozenset) -> None:
        self._module = module
        self._package_minters = package_minters
        #: Functions and methods of this module that return a minted id,
        #: called by name or as an attribute of anything.
        self._local_minters: set[str] = set()
        self._own: dict[int, list[ast.AST]] = {}
        self._children: dict[int, list[ast.AST]] = {}
        self._find_local_minters()

    # -- what is a mint ------------------------------------------------------

    def _is_mint(self, node: ast.AST) -> bool:
        if not isinstance(node, ast.Call):
            return False
        func, module = node.func, self._module
        if isinstance(func, ast.Name):
            return (
                func.id in module.generators
                or func.id in self._local_minters
                or module.package_imports.get(func.id) in self._package_minters
            )
        if not isinstance(func, ast.Attribute):
            return False
        if func.attr in self._local_minters:
            return True
        if isinstance(func.value, ast.Name):
            owner = func.value.id
            prefix = module.generator_modules.get(owner)
            if prefix is not None:
                return func.attr.startswith(prefix)
            imported = module.package_imports.get(owner)
            if imported is not None:
                source, name = imported
                return (source, f"{name}.{func.attr}") in self._package_minters
        return False

    def _carries(self, value: ast.AST | None, minted: set[str]) -> bool:
        """A mint, or a name holding one, anywhere in ``value``."""
        if value is None:
            return False
        return any(
            self._is_mint(node)
            or (
                isinstance(node, ast.Name)
                and isinstance(node.ctx, ast.Load)
                and node.id in minted
            )
            for node in ast.walk(value)
        )

    def _is_id(self, value: ast.AST | None, minted: set[str]) -> bool:
        """``value`` IS a minted id, formatted or not: not an object or a
        mapping that merely holds one. What a name or a helper's return
        must be to count as minted."""
        if value is None:
            return False
        if self._is_mint(value):
            return True
        if isinstance(value, ast.Name):
            return value.id in minted
        if isinstance(value, ast.Await):
            return self._is_id(value.value, minted)
        if isinstance(value, ast.Call):
            func = value.func
            if isinstance(func, ast.Name) and func.id in _ID_PRESERVING_CALLS:
                return any(self._is_id(a, minted) for a in value.args)
            if isinstance(func, ast.Attribute) and func.attr in _ID_PRESERVING_METHODS:
                return self._is_id(func.value, minted) or any(
                    self._is_id(a, minted) for a in value.args
                )
            return False
        if isinstance(value, (ast.Attribute, ast.Subscript)):
            return self._is_id(value.value, minted)
        if isinstance(value, ast.JoinedStr):
            return any(
                self._is_id(v.value, minted)
                for v in value.values
                if isinstance(v, ast.FormattedValue)
            )
        if isinstance(value, ast.BinOp):
            return self._is_id(value.left, minted) or self._is_id(value.right, minted)
        if isinstance(value, ast.IfExp):
            return self._is_id(value.body, minted) or self._is_id(value.orelse, minted)
        return False

    # -- scopes --------------------------------------------------------------

    def _own_nodes(self, scope: ast.AST) -> list[ast.AST]:
        """``scope``'s nodes, short of the functions nested in it, which are
        scopes of their own. A class body is read as part of its scope."""
        key = id(scope)
        if key not in self._own:
            nodes, children = [], []
            stack = list(ast.iter_child_nodes(scope))
            while stack:
                node = stack.pop()
                if isinstance(node, (*_FUNCTIONS, ast.Lambda)):
                    children.append(node)
                    continue
                nodes.append(node)
                stack.extend(ast.iter_child_nodes(node))
            self._own[key], self._children[key] = nodes, children
        return self._own[key]

    def _child_scopes(self, scope: ast.AST) -> list[ast.AST]:
        """The functions defined directly in ``scope`` (its classes' methods
        included), not those nested in them."""
        self._own_nodes(scope)
        return self._children[id(scope)]

    def _minted_names(self, scope: ast.AST, inherited: frozenset) -> set[str]:
        """Local names in ``scope`` that hold a minted id."""
        minted = set(inherited)
        assignments = [
            node
            for node in self._own_nodes(scope)
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr))
        ]
        for _ in range(2):  # a name minted later in a loop still counts
            for node in assignments:
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                for target in targets:
                    for name, value in _paired(target, node.value):
                        if self._is_id(value, minted):
                            minted.update(_bound_names(name))
        return minted

    def _scopes(self, scope: ast.AST | None = None, inherited: frozenset = frozenset()):
        """Every scope, with the names in it that hold a minted id. A closure
        inherits its enclosing function's; a module-level function does not
        inherit module names."""
        scope = self._module.tree if scope is None else scope
        minted = self._minted_names(scope, inherited)
        yield scope, minted
        inherit = (
            frozenset(minted)
            if isinstance(scope, (*_FUNCTIONS, ast.Lambda))
            else frozenset()
        )
        for child in self._child_scopes(scope):
            yield from self._scopes(child, inherit)

    def _find_local_minters(self) -> None:
        """Functions and methods of this module that return a minted id."""
        while True:
            found = {
                scope.name
                for scope, minted in self._scopes()
                if isinstance(scope, _FUNCTIONS)
                and scope.name not in self._local_minters
                and any(
                    isinstance(node, ast.Return) and self._is_id(node.value, minted)
                    for node in self._own_nodes(scope)
                )
            }
            if not found:
                return
            self._local_minters |= found

    def minting_functions(self) -> set[str]:
        """This module's minting functions as ``name`` or ``Class.name``."""
        names = set()
        for node in self._module.tree.body:
            if isinstance(node, _FUNCTIONS) and node.name in self._local_minters:
                names.add(node.name)
            elif isinstance(node, ast.ClassDef):
                names.update(
                    f"{node.name}.{item.name}"
                    for item in node.body
                    if isinstance(item, _FUNCTIONS) and item.name in self._local_minters
                )
        return names

    # -- where a mint must not reach -----------------------------------------

    @staticmethod
    def _target_says_sink(target: ast.AST) -> bool:
        if isinstance(target, ast.Name):
            return _says_sink(target.id)
        if isinstance(target, ast.Attribute):
            return _says_sink(target.attr)
        if isinstance(target, ast.Subscript):
            key = target.slice
            return isinstance(key, ast.Constant) and _says_sink(key.value)
        if isinstance(target, (ast.Tuple, ast.List)):
            return any(_Analysis._target_says_sink(e) for e in target.elts)
        return False

    def _sink_lines(self, node: ast.AST, minted: set[str]) -> list[int]:
        carries = functools.partial(self._carries, minted=minted)
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(self._target_says_sink(t) for t in targets) and carries(node.value):
                return [node.lineno]
        elif isinstance(node, ast.keyword):
            if _says_sink(node.arg) and carries(node.value):
                return [node.value.lineno]
        elif isinstance(node, ast.Dict):
            return [
                key.lineno
                for key, value in zip(node.keys, node.values)
                if isinstance(key, ast.Constant)
                and _says_sink(key.value)
                and carries(value)
            ]
        elif isinstance(node, ast.Call):
            arguments = [*node.args, *(kw.value for kw in node.keywords)]
            labels = [
                a
                for a in node.args
                if isinstance(a, ast.Constant) and _says_sink(a.value)
            ]
            if labels and any(carries(a) for a in arguments if a not in labels):
                return [node.lineno]
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "format"
                and isinstance(func.value, ast.Constant)
                and _says_sink(func.value.value)
                and any(carries(a) for a in arguments)
            ):
                return [node.lineno]
        elif isinstance(node, ast.JoinedStr):
            text = [v for v in node.values if isinstance(v, ast.Constant)]
            holes = [v for v in node.values if isinstance(v, ast.FormattedValue)]
            if any(_says_sink(t.value) for t in text) and any(
                carries(h) for h in holes
            ):
                return [node.lineno]
        elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Mod, ast.Add)):
            for label, value in ((node.left, node.right), (node.right, node.left)):
                if (
                    isinstance(label, ast.Constant)
                    and _says_sink(label.value)
                    and carries(value)
                ):
                    return [node.lineno]
        return []

    def sinks(self) -> list[int]:
        return sorted(
            {
                line
                for scope, minted in self._scopes()
                for node in self._own_nodes(scope)
                for line in self._sink_lines(node, minted)
            }
        )


def _names_a_minter(imported: tuple[str, str], minters: frozenset) -> bool:
    """An imported name is a minting function, or a class holding one."""
    source, name = imported
    return any(
        module == source and (qualname == name or qualname.startswith(f"{name}."))
        for module, qualname in minters
    )


@functools.lru_cache(maxsize=None)
def _package(root: Path) -> tuple[dict[Path, _Module], frozenset]:
    """Every module of the package at ``root``, and its minting functions as
    ``(module, name)``, resolved across imports to a fixed point."""
    modules = {}
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        modules[path] = _Module.read(
            _module_name(root, path), tree, path.name == "__init__.py"
        )
    minters: frozenset = frozenset()
    # First the modules that call a generator, then, each round, only those
    # importing a function the round before found to mint.
    pending = [m for m in modules.values() if m.generator_modules or m.generators]
    while pending:
        new = {
            (module.name, name)
            for module in pending
            for name in _Analysis(module, minters).minting_functions()
        } - minters
        if not new:
            break
        minters = minters | new
        pending = [
            m
            for m in modules.values()
            if any(_names_a_minter(i, new) for i in m.package_imports.values())
        ]
    return modules, minters


def _offenders(root: Path) -> dict[str, list[int]]:
    """Per scanned module of the package at ``root``, the lines where a mint
    reaches a sink."""
    modules, minters = _package(root)
    offenders = {}
    for path in _scanned_modules(root):
        lines = _Analysis(modules[path], minters).sinks()
        if lines:
            offenders[path.relative_to(root).as_posix()] = lines
    return offenders


def _bound_names(target: ast.AST) -> list[str]:
    """The names an assignment target binds: ``x``, and each name of a tuple
    or list unpacking. ``x.attr = ...`` and ``x[k] = ...`` bind nothing: they
    store into ``x``, which does not then hold the id."""
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        return [n for elt in target.elts for n in _bound_names(elt)]
    if isinstance(target, ast.Starred):
        return _bound_names(target.value)
    return []


def _paired(target: ast.AST, value: ast.AST | None):
    """``(target, value)``, or each element pair of a tuple assigned to a tuple
    of the same length, so ``cid, n = mint(), 1`` binds ``cid`` alone."""
    if (
        isinstance(target, (ast.Tuple, ast.List))
        and isinstance(value, (ast.Tuple, ast.List))
        and len(target.elts) == len(value.elts)
    ):
        for t, v in zip(target.elts, value.elts):
            yield from _paired(t, v)
        return
    yield target, value


def _sinks(source: str) -> list[int]:
    """The sinks in ``source``, read as a module of the real package."""
    _, minters = _package(_PACKAGE)
    module = _Module.read("faultmaven.planted", ast.parse(source), False)
    return _Analysis(module, minters).sinks()


def test_the_scan_reads_the_request_path_modules():
    """Not vacuous: the globs reach the modules #2278 found minting, and the
    route modules the first version of this scan left out."""
    relative = {p.relative_to(_PACKAGE).as_posix() for p in _scanned_modules(_PACKAGE)}
    assert {
        "modules/case/api/routes/cases.py",
        "modules/case/api/routes/conversation.py",
        "modules/case/api/routes/data.py",
        "modules/auth/api/auth.py",
        "api/v1/auth_dependencies.py",
        "api/middleware/idempotency.py",
        "api/middleware/deduplication.py",
        "api/routes/admin.py",
        "api/exception_handlers.py",
        "main.py",
    } <= relative
    assert not relative & _OUTSIDE_THE_REQUEST


def test_the_package_index_finds_a_real_minting_helper():
    """Not vacuous: the cross-module index resolves a helper the package
    really has, so the imported-helper rows below test the real index."""
    _, minters = _package(_PACKAGE)
    assert ("faultmaven.utils.runbook_id", "authored_item_id") in minters


def _setup_middleware_call_line(tree: ast.Module, callee: str) -> int:
    """The line of ``setup_middleware``'s one call that installs ``callee``:
    ``app.add_middleware(callee, …)``, or a call of ``callee`` itself."""
    (setup,) = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "setup_middleware"
    ]
    lines = [
        node.lineno
        for node in ast.walk(setup)
        if isinstance(node, ast.Call)
        and (
            (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_middleware"
                and node.args
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == callee
            )
            or (isinstance(node.func, ast.Name) and node.func.id == callee)
        )
    ]
    assert len(lines) == 1, (callee, lines)
    return lines[0]


def _setup_middleware_tree() -> ast.Module:
    return ast.parse(
        (_PACKAGE / "bootstrap" / "middleware.py").read_text(encoding="utf-8")
    )


def test_the_exempt_middleware_is_registered_outside_logging():
    """Starlette wraps in reverse registration order, so a middleware added
    after ``LoggingMiddleware`` runs outside the request's context. The
    exemption holds only while that stays so."""
    tree = _setup_middleware_tree()
    logging_line = _setup_middleware_call_line(tree, "LoggingMiddleware")
    assert _setup_middleware_call_line(tree, "ContractProbeMiddleware") > logging_line


@pytest.mark.parametrize(
    "callee", ["IdempotencyMiddleware", "setup_protection_middleware"]
)
def test_the_refusing_middleware_is_registered_inside_logging(callee):
    """Idempotency and deduplication (installed by
    ``setup_protection_middleware``) are registered before ``LoggingMiddleware``,
    so they answer inside the request's context. Their tests stack them that
    way (``test_idempotency_caller_scoping.py``'s ``_as_deployed``)."""
    tree = _setup_middleware_tree()
    logging_line = _setup_middleware_call_line(tree, "LoggingMiddleware")
    assert _setup_middleware_call_line(tree, callee) < logging_line


@pytest.mark.parametrize(
    "source",
    [
        # The shapes #2278 removed.
        pytest.param(
            "import uuid\ndef r():\n    correlation_id = str(uuid.uuid4())\n",
            id="name",
        ),
        pytest.param(
            "import uuid\ndef r(response):\n"
            '    response.headers["x-correlation-id"] = str(uuid.uuid4())\n',
            id="header-subscript",
        ),
        pytest.param(
            "import uuid\ndef r():\n    return Response(204, headers="
            '{"x-correlation-id": str(uuid.uuid4())})\n',
            id="dict-key",
        ),
        pytest.param(
            "import uuid\ndef r():\n    return f(correlation_id=str(uuid.uuid4())[:8])\n",
            id="keyword",
        ),
        pytest.param(
            "import uuid\ndef r():\n    cid = uuid.uuid4().hex\n"
            '    log(extra={"correlation_id": cid})\n',
            id="through-a-local-name",
        ),
        pytest.param(
            "import uuid as u\ndef r(self):\n    self.correlation_id = str(u.uuid4())\n",
            id="module-alias",
        ),
        pytest.param(
            "from uuid import uuid4 as mint\ndef r():\n"
            "    request_correlation: str = f'{mint()}'\n",
            id="generator-alias",
        ),
        # The shapes #2296's review found the first version missed.
        pytest.param(
            "import uuid\ndef r():\n"
            '    raise HTTPException(500, detail=f"Error ID: {uuid.uuid4()}")\n',
            id="error-id-in-an-f-string",
        ),
        pytest.param(
            "from uuid import uuid4\ndef r():\n    error_id = str(uuid4())\n",
            id="error-id-name",
        ),
        pytest.param(
            "import uuid\ndef r():\n"
            '    logger.info(f"done (correlation: {uuid.uuid4()})")\n',
            id="correlation-in-a-log-f-string",
        ),
        pytest.param(
            "import uuid\ndef r(resp):\n"
            '    resp.headers.append("x-correlation-id", str(uuid.uuid4()))\n',
            id="header-append",
        ),
        pytest.param(
            "import secrets\ndef r():\n    correlation_id = secrets.token_hex(8)\n",
            id="secrets-token",
        ),
        pytest.param(
            "from secrets import token_urlsafe\ndef r():\n"
            '    log(extra={"correlation_id": token_urlsafe(8)})\n',
            id="secrets-generator-imported",
        ),
        pytest.param(
            "import uuid\ndef _new_id():\n    return str(uuid.uuid4())\n"
            "def r():\n    correlation_id = _new_id()\n",
            id="helper-function",
        ),
        pytest.param(
            "import uuid\ndef _new_id():\n    minted = uuid.uuid4()\n"
            "    return minted.hex\ndef _wrapped():\n    return _new_id()\n"
            "def r():\n    correlation_id = _wrapped()\n",
            id="helper-of-a-helper-through-a-local",
        ),
        pytest.param(
            "import uuid\nclass R:\n    def _mint(self):\n"
            "        return uuid.uuid4().hex\n    def r(self):\n"
            "        self.correlation_id = self._mint()\n",
            id="helper-method",
        ),
        pytest.param(
            "from faultmaven.utils.runbook_id import authored_item_id\ndef r():\n"
            "    correlation_id = authored_item_id()\n",
            id="helper-imported-from-the-package",
        ),
        pytest.param(
            "import uuid\ndef r():\n"
            '    logger.info("correlation %s", uuid.uuid4())\n',
            id="log-argument-beside-its-label",
        ),
        pytest.param(
            'import uuid\ndef r():\n    detail = "Error ID: %s" % uuid.uuid4()\n',
            id="percent-format",
        ),
        pytest.param(
            'import uuid\ndef r():\n    detail = "Error ID: " + str(uuid.uuid4())\n',
            id="concatenation",
        ),
        pytest.param(
            "import uuid\ndef r():\n"
            '    detail = "Error ID: {}".format(uuid.uuid4())\n',
            id="str-format",
        ),
        pytest.param(
            "import uuid\ndef r():\n    cid = str(uuid.uuid4())\n"
            '    def inner():\n        log(extra={"correlation_id": cid})\n',
            id="closure",
        ),
        pytest.param(
            "import uuid\nasync def _new_id():\n    return str(uuid.uuid4())\n"
            "async def r():\n    cid = await _new_id()\n"
            '    log(extra={"correlation_id": cid})\n',
            id="awaited-helper-through-a-name",
        ),
        pytest.param(
            "import uuid\nasync def _a():\n    return str(uuid.uuid4())\n"
            "async def _b():\n    return await _a()\n"
            "async def r():\n    correlation_id = await _b()\n",
            id="awaited-helper-of-a-helper",
        ),
        pytest.param(
            "import uuid\ndef r():\n    cid, other = str(uuid.uuid4()), 1\n"
            '    log(extra={"correlation_id": cid})\n',
            id="tuple-unpacking",
        ),
    ],
)
def test_the_scan_flags_every_minting_shape(source):
    """Positive controls: a scan that flags nothing proves nothing."""
    assert _sinks(source), source


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(
            "import uuid\ndef r():\n    user_id = f'user_{uuid.uuid4().hex[:8]}'\n",
            id="entity-id",
        ),
        pytest.param(
            "from faultmaven.infrastructure.logging.coordinator import "
            "current_correlation_id\ndef r():\n"
            "    correlation_id = current_correlation_id()\n"
            '    detail = f"Error ID: {current_correlation_id()}"\n',
            id="the-requests-own-id",
        ),
        pytest.param(
            "import uuid\ndef a():\n    cid = str(uuid.uuid4())\n"
            'def b(cid):\n    log(extra={"correlation_id": cid})\n',
            id="minted-in-a-sibling-function",
        ),
        pytest.param(
            "import uuid\ndef r():\n    report_id = str(uuid.uuid4())\n"
            '    logger.info("Created report %s", report_id)\n'
            '    raise HTTPException(404, detail=f"Report {report_id} not found")\n',
            id="report-id-logged-and-in-a-body",
        ),
        pytest.param(
            "import uuid\ndef r(response):\n"
            '    case_id = f"case_{uuid.uuid4().hex}"\n'
            '    response.headers["Location"] = f"/api/v1/cases/{case_id}"\n',
            id="case-id-in-a-location-header",
        ),
        pytest.param(
            "import uuid\ndef r():\n    draft_id = uuid.uuid4().hex\n"
            '    logger.error("Error creating draft %s", draft_id)\n',
            id="entity-id-on-an-error-line",
        ),
        pytest.param(
            "import secrets\ndef r(redis, name):\n"
            "    token = secrets.token_hex(16)\n    redis.set(name, token)\n",
            id="claim-owner-token",
        ),
        pytest.param(
            "from faultmaven.utils.runbook_id import authored_item_id\ndef r():\n"
            '    item_id = authored_item_id()\n    log(extra={"item_id": item_id})\n',
            id="imported-helper-minting-an-entity-id",
        ),
        pytest.param(
            "import uuid\ndef r(exc):\n"
            '    detail = f"Error: {exc}"\n    parsed = uuid.UUID(detail)\n'
            '    log(extra={"correlation_id": parsed})\n',
            id="uuid-parsed-not-minted",
        ),
        pytest.param(
            "import uuid\ndef r(request):\n"
            "    request.state.request_id = str(uuid.uuid4())\n"
            '    log("upstream correlation id: %s", request.headers.get("x"))\n',
            id="an-object-a-minted-id-is-stored-into",
        ),
        pytest.param(
            "import secrets\ndef r(session):\n"
            "    session['state'] = secrets.token_urlsafe()\n"
            '    log(extra={"correlation_id": session.get("cid")})\n',
            id="a-mapping-a-token-is-stored-into",
        ),
    ],
)
def test_the_scan_passes_what_is_not_a_minted_correlation_id(source):
    assert _sinks(source) == []


def test_no_route_mints_a_correlation_id_of_its_own():
    offenders = [
        f"{path}:{line}"
        for path, lines in _offenders(_PACKAGE).items()
        for line in lines
    ]
    assert not offenders, (
        "A route or an inner middleware minted a correlation id of its own. "
        "Read the request's with current_correlation_id() "
        "(faultmaven/infrastructure/logging/coordinator.py); LoggingMiddleware "
        "already sets X-Correlation-ID on the response and stamps the id on "
        "every log line (#2278): " + ", ".join(offenders)
    )
