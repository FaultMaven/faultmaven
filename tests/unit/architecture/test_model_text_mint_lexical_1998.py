"""Only the redaction module mints model-bound content: the lexical check (#1998, A8).

``ModelText`` is minted by ``faultmaven/infrastructure/security/model_content.py``
and, for tests, by ``tests/model_content.py``. Everywhere else the module's
internals and the class's constructor are off limits, and every SPELLED way of
reaching them fails here — an AST walk over every Python file in the
repository, with a raw-text scan behind it:

* ``ModelText`` is named only to test a value's type: ``type(x) is ModelText``,
  ``isinstance(x, ModelText)``, an annotation, and the import that binds it
  (without ``as``). Any other use — calling it, its ``__new__``, handing it to
  ``str.__new__``, subclassing it, ``getattr``/``vars``/``__dict__`` on it,
  or binding it to another name — is refused, so an alias cannot hide a
  construction;
* the module is reached only by ``from ... model_content import <public
  names>``: never imported whole (``import ...model_content``, ``from
  ...security import model_content``), never reached by dotted attribute
  access, and never asked for a private (``_``) name;
* the token's name appears nowhere — in code, a string or a comment.

The raw-text scan catches the same shapes where the AST cannot see them, inside
strings handed to ``eval``, ``exec`` or ``getattr``.

What this does NOT see, stated so nobody mistakes it for more: a name computed
at runtime (``importlib.import_module("faultmaven." + name)``, ``type(value)``
of a minted value) — a deliberate act, declared out of scope by the ruling —
and ``object.__setattr__`` on a value forged that way. The boundary's runtime
check that a ``ModelText``'s slots are set is what stands behind it.
"""

from __future__ import annotations

import ast
import re
import warnings
from functools import lru_cache
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.architecture, pytest.mark.security]

_ROOT = Path(__file__).resolve().parents[3]
#: Where Python lives in the repository.
_SCANNED = ("faultmaven", "tests", "scripts", "alembic")
_MODULE = "faultmaven.infrastructure.security.model_content"
_PARENT, _, _LEAF = _MODULE.rpartition(".")
#: The two files allowed to mint.
EXEMPT = frozenset(
    {
        "faultmaven/infrastructure/security/model_content.py",
        "tests/model_content.py",
    }
)

# Spelled in pieces, so this file passes its own raw-text scan.
_CLASS = "Model" + "Text"
_TOKEN = "_MINT" + "_TOKEN"

#: The raw-text backstop: the shapes, wherever they appear in a file's text.
_TEXT_PATTERNS = [
    (re.compile(re.escape(_TOKEN)), "the mint token"),
    (re.compile(rf"\b{_CLASS}\s*\("), f"a call to {_CLASS}"),
    (re.compile(rf"__new__\s*\(\s*{_CLASS}\b"), f"__new__ on {_CLASS}"),
    (re.compile(rf"\b{_CLASS}\s*\.\s*__new__"), f"{_CLASS}.__new__"),
    (re.compile(rf"\bclass\s+\w+\s*\([^)]*\b{_CLASS}\b"), f"a subclass of {_CLASS}"),
    (
        re.compile(
            rf"\b(?:getattr|vars|setattr|delattr)\s*\(\s*(?:{_CLASS}|{_LEAF})\b"
        ),
        "reflection on the minting module",
    ),
    (
        re.compile(rf"\b(?:{_CLASS}|{_LEAF})\s*\.\s*__dict__"),
        "__dict__ of the minting module",
    ),
]


def _parse(source: str) -> ast.Module:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", (DeprecationWarning, SyntaxWarning))
        return ast.parse(source)


def _module_of(node: ast.ImportFrom, path: str) -> str:
    """The absolute module an ``ImportFrom`` names, resolving relative imports."""
    if not node.level:
        return node.module or ""
    package = path.removesuffix(".py").replace("/", ".").split(".")[:-1]
    base = package[: len(package) - (node.level - 1)]
    return ".".join(base + ([node.module] if node.module else []))


def _dotted(node: ast.AST) -> Optional[str]:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _in_annotation(node: ast.AST, parents: dict) -> bool:
    child = node
    while child in parents:
        parent = parents[child]
        if isinstance(parent, (ast.arg, ast.AnnAssign)) and parent.annotation is child:
            return True
        if (
            isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef))
            and parent.returns is child
        ):
            return True
        child = parent
    return False


def _a_type_test(node: ast.AST, parents: dict) -> bool:
    """``ModelText`` used only to test a value's type."""
    parent = parents.get(node)
    if isinstance(parent, ast.Compare):
        return True
    if isinstance(parent, ast.Tuple):  # isinstance(x, (str, ModelText))
        node, parent = parent, parents.get(parent)
    return (
        isinstance(parent, ast.Call)
        and isinstance(parent.func, ast.Name)
        and parent.func.id in {"isinstance", "issubclass"}
        and len(parent.args) == 2
        and parent.args[1] is node
    ) or _in_annotation(node, parents)


def violations(source: str, path: str) -> List[Tuple[int, str]]:
    """Every spelled way ``source`` (at repo-relative ``path``) reaches the mint."""
    found: List[Tuple[int, str]] = []
    # Every rule below needs one of these names in the file's text, so a file
    # without any of them has nothing to find. A necessary condition, not the
    # check: the check is the walk.
    if not any(name in source for name in (_CLASS, _LEAF, _TOKEN)):
        return found
    for lineno, line in enumerate(source.splitlines(), 1):
        for pattern, what in _TEXT_PATTERNS:
            if pattern.search(line):
                found.append((lineno, what))
    tree = _parse(source)
    parents = {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == _MODULE or alias.name.startswith(_MODULE + "."):
                    found.append((line, "the minting module imported whole"))
        elif isinstance(node, ast.ImportFrom):
            module = _module_of(node, path)
            for alias in node.names:
                if module == _PARENT and alias.name == _LEAF:
                    found.append((line, "the minting module imported whole"))
                if module == _MODULE and (
                    alias.name.startswith("_") or alias.name == "*"
                ):
                    found.append((line, f"a private name imported: {alias.name}"))
                if module == _MODULE and alias.name == _CLASS and alias.asname:
                    found.append((line, f"{_CLASS} imported under another name"))
        elif isinstance(node, (ast.Name, ast.Attribute)):
            name = node.id if isinstance(node, ast.Name) else node.attr
            if name == _TOKEN:
                found.append((line, "the mint token"))
            if (
                isinstance(node, ast.Attribute)
                and node.attr == _LEAF
                and _dotted(node) == _MODULE
            ):
                found.append((line, "the minting module reached by attribute"))
            if (
                name == _CLASS
                and isinstance(node.ctx, ast.Load)
                and not _a_type_test(node, parents)
            ):
                found.append((line, f"{_CLASS} used other than to test a type"))
        elif isinstance(node, ast.ClassDef):
            for base in node.bases:
                if (_dotted(base) or "").split(".")[-1] == _CLASS:
                    found.append((line, f"a subclass of {_CLASS}"))
    return sorted(set(found))


def _files() -> Iterable[Tuple[str, Path]]:
    for top in _SCANNED:
        for path in sorted((_ROOT / top).rglob("*.py")):
            yield path.relative_to(_ROOT).as_posix(), path


@lru_cache(maxsize=1)
def _scan() -> Tuple[dict, int]:
    found, files = {}, 0
    for rel, path in _files():
        files += 1
        if rel in EXEMPT:
            continue
        hits = violations(path.read_text(encoding="utf-8"), rel)
        if hits:
            found[rel] = hits
    return found, files


def test_nothing_outside_the_minting_module_reaches_the_mint():
    found, files = _scan()
    assert files > 1000, f"scanned only {files} files"
    assert not found, (
        "model-bound content is minted only by "
        "faultmaven/infrastructure/security/model_content.py (#1998):\n  "
        + "\n  ".join(
            f"{rel}:{line} {what}" for rel, hits in found.items() for line, what in hits
        )
    )


def test_the_exempt_files_exist_and_mint():
    """A positive control on the exemption: both files exist and use the mint."""
    for rel in EXEMPT:
        text = (_ROOT / rel).read_text(encoding="utf-8")
        assert violations(text, rel), f"{rel} no longer mints: drop its exemption"


# ---------------------------------------------------------------------------
# Positive controls: each shape, planted in a file, is caught.
# ---------------------------------------------------------------------------

_IMPORT = f"from {_MODULE} import {_CLASS}\n"

PLANTED = {
    "construct": _IMPORT + f"x = {_CLASS}('raw', 'case', True)\n",
    "construct_by_attribute": f"import faultmaven\nx = faultmaven.infrastructure.security.{_LEAF}.{_CLASS}('raw')\n",
    "dunder_new": _IMPORT + f"x = {_CLASS}.__new__({_CLASS}, 'raw')\n",
    "str_new": _IMPORT + f"x = str.__new__({_CLASS}, 'raw')\n",
    "str_new_spaced": _IMPORT + f"x = str.__new__(\n    {_CLASS},\n    'raw',\n)\n",
    "subclass": _IMPORT + f"class Sub({_CLASS}):\n    pass\n",
    "alias": _IMPORT + f"T = {_CLASS}\nx = T('raw')\n",
    "passed_along": _IMPORT + f"def f(make):\n    return make('raw')\nf({_CLASS})\n",
    "import_as": f"from {_MODULE} import {_CLASS} as T\n",
    "getattr_class": _IMPORT + f"getattr({_CLASS}, 'source')\n",
    "vars_class": _IMPORT + f"vars({_CLASS})\n",
    "dict_class": _IMPORT + f"{_CLASS}.__dict__\n",
    "slot_descriptor": _IMPORT + f"{_CLASS}.source.__set__(x, 'case')\n",
    "token_import": f"from {_MODULE} import {_TOKEN}\n",
    "token_in_string": f"import importlib\ngetattr(importlib.import_module('m'), '{_TOKEN}')\n",
    "private_import": f"from {_MODULE} import _mint\n",
    "star_import": f"from {_MODULE} import *\n",
    "module_import": f"import {_MODULE}\n",
    "module_import_as": f"import {_MODULE} as mc\n",
    "module_from_parent": f"from {_PARENT} import {_LEAF}\n",
    "module_by_attribute": f"import faultmaven\nm = faultmaven.infrastructure.security.{_LEAF}\n",
    "getattr_module": f"getattr({_LEAF}, 'ModelTex' + 't')\n",
    "vars_module": f"vars({_LEAF})\n",
    "dict_module": f"{_LEAF}.__dict__\n",
    "eval_string": f"eval('{_CLASS}(1)')\n",
}


@pytest.mark.parametrize("snippet", sorted(PLANTED))
def test_a_planted_violation_is_caught(snippet, tmp_path):
    planted = tmp_path / "planted.py"
    planted.write_text(PLANTED[snippet], encoding="utf-8")
    assert violations(
        planted.read_text(encoding="utf-8"), "faultmaven/planted.py"
    ), snippet


def test_a_relative_import_of_the_private_name_is_caught():
    source = "from .model_content import _mint\n"
    assert violations(source, "faultmaven/infrastructure/security/other.py")
    assert violations(
        "from . import model_content\n", "faultmaven/infrastructure/security/other.py"
    )


ALLOWED = {
    "import": _IMPORT,
    "public_names": f"from {_MODULE} import case_redacted, non_case_text\n",
    "type_is": _IMPORT + f"ok = type(v) is {_CLASS}\n",
    "type_is_not": _IMPORT + f"ok = type(v) is not {_CLASS}\n",
    "isinstance": _IMPORT + f"ok = isinstance(v, {_CLASS})\n",
    "isinstance_tuple": _IMPORT + f"ok = isinstance(v, (str, {_CLASS}))\n",
    "annotation": _IMPORT + f"def f(v: {_CLASS}) -> {_CLASS}:\n    return v\n",
    "annotated_assign": _IMPORT + f"v: {_CLASS} = make()\n",
    "generic_annotation": _IMPORT + f"def f(v: list[{_CLASS}]) -> None:\n    pass\n",
}


@pytest.mark.parametrize("snippet", sorted(ALLOWED))
def test_a_type_test_is_allowed(snippet):
    assert not violations(ALLOWED[snippet], "faultmaven/ok.py"), snippet
