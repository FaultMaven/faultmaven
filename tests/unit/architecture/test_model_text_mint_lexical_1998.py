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
* the token's name appears nowhere — in code, a string or a comment;
* R1: the module's dotted path appears in no string (``importlib``,
  ``sys.modules``, ``__import__``, a ``patch`` target);
* R2: no private name of the module — read from its own source, so the list
  cannot go stale — appears as an attribute or a string in a file that names
  the module or the class;
* R3: no function's ``__globals__`` is read;
* R4: no descriptor's ``__set__`` is called directly;
* R5: no ``__setattr__`` call writes a ``redacted`` attribute — the slot every
  forgery has to set;
* R6: no ``__class__`` is assigned, and no class declares a ``redacted`` slot —
  the two halves of swapping a look-alike's class.

The raw-text scan catches the first group's shapes where the AST cannot see
them, inside strings handed to ``eval``, ``exec`` or ``getattr``.

Out of scope, by the ruling, and nothing else: reflection through a name
computed at runtime (``import_module("faultmaven." + name)``,
``getattr(obj, "__se" + "t__")``, a slot name built from pieces). Every
spelled route to a forged value, including through ``type()`` of a minted
value, ends in one of the shapes above.
"""

from __future__ import annotations

import ast
import re
import warnings
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.architecture, pytest.mark.security]

_ROOT = Path(__file__).resolve().parents[3]
#: Where Python lives in the repository.
_SCANNED = ("faultmaven", "tests", "scripts", "alembic")
# Spelled in pieces, so this file passes its own scan (R1, and the raw text).
_LEAF = "model_" + "content"
_PARENT = "faultmaven.infrastructure.security"
_MODULE = f"{_PARENT}.{_LEAF}"
_CLASS = "Model" + "Text"
_TOKEN = "_MINT" + "_TOKEN"
#: The one slot every forgery must set (R5, R6).
_SLOT = "redac" + "ted"
_MODULE_FILE = "faultmaven/infrastructure/security/" + _LEAF + ".py"
#: The two files allowed to mint.
EXEMPT = frozenset({_MODULE_FILE, "tests/" + _LEAF + ".py"})

#: (file, rule) -> why that file's hit of that rule reaches no ModelText.
RULE_EXEMPTIONS: Dict[Tuple[str, str], str] = {
    ("tests/unit/modules/agent/tools/test_web_search_tool.py", "R6"): (
        "sets a mock's __class__ to GoogleCSEProvider so the tool's isinstance "
        "dispatch takes the CSE branch; the file mints nothing"
    ),
}

#: Every rule needs one of these in a file's text, so a file without any of
#: them has nothing to find. A necessary condition, not the check.
_TRIGGERS = (
    _CLASS,
    _LEAF,
    _TOKEN,
    "__globals__",
    "__set__",
    "__setattr__",
    "__class__",
    "__slots__",
)

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


@lru_cache(maxsize=1)
def private_names() -> frozenset:
    """The minting module's private top-level names, read from its source."""
    tree = _parse((_ROOT / _MODULE_FILE).read_text(encoding="utf-8"))
    names: Set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return frozenset(n for n in names if n.startswith("_") and not n.startswith("__"))


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


def _docstrings(tree: ast.AST) -> Set[ast.AST]:
    found = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                found.add(first.value)
    return found


def _string_args(node: ast.Call) -> List[str]:
    return [
        arg.value
        for arg in node.args[:2]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
    ]


def violations(source: str, path: str) -> List[Tuple[int, str, str]]:
    """``(line, rule, what)`` for every spelled way ``source`` reaches the mint.

    ``path`` is repo-relative; it resolves relative imports.
    """
    found: List[Tuple[int, str, str]] = []
    if not any(trigger in source for trigger in _TRIGGERS):
        return found
    for lineno, line in enumerate(source.splitlines(), 1):
        for pattern, what in _TEXT_PATTERNS:
            if pattern.search(line):
                found.append((lineno, "text", what))
    tree = _parse(source)
    parents = {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
    docstrings = _docstrings(tree)
    mentions = _CLASS in source or _LEAF in source
    private = private_names() if mentions else frozenset()
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == _MODULE or alias.name.startswith(_MODULE + "."):
                    found.append((line, "import", "the minting module imported whole"))
        elif isinstance(node, ast.ImportFrom):
            module = _module_of(node, path)
            for alias in node.names:
                if module == _PARENT and alias.name == _LEAF:
                    found.append((line, "import", "the minting module imported whole"))
                if module == _MODULE and (
                    alias.name.startswith("_") or alias.name == "*"
                ):
                    found.append(
                        (line, "import", f"a private name imported: {alias.name}")
                    )
                if module == _MODULE and alias.name == _CLASS and alias.asname:
                    found.append(
                        (line, "import", f"{_CLASS} imported under another name")
                    )
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node in docstrings:
                continue
            if node.value == _MODULE or node.value.startswith(_MODULE + "."):
                found.append((line, "R1", "the minting module named in a string"))
            if node.value in private:
                found.append((line, "R2", f"a private name as a string: {node.value}"))
        elif isinstance(node, (ast.Name, ast.Attribute)):
            name = node.id if isinstance(node, ast.Name) else node.attr
            if name == _TOKEN:
                found.append((line, "token", "the mint token"))
            if isinstance(node, ast.Attribute):
                if node.attr == _LEAF and _dotted(node) == _MODULE:
                    found.append(
                        (line, "import", "the minting module reached by attribute")
                    )
                if node.attr in private:
                    found.append(
                        (line, "R2", f"a private name by attribute: {node.attr}")
                    )
                if node.attr == "__globals__":
                    found.append((line, "R3", "a function's __globals__"))
                if node.attr == "__class__" and isinstance(node.ctx, ast.Store):
                    found.append((line, "R6", "a __class__ assignment"))
            if (
                name == _CLASS
                and isinstance(node.ctx, ast.Load)
                and not _a_type_test(node, parents)
            ):
                found.append((line, "type", f"{_CLASS} used other than to test a type"))
        elif isinstance(node, ast.ClassDef):
            for base in node.bases:
                if (_dotted(base) or "").split(".")[-1] == _CLASS:
                    found.append((line, "type", f"a subclass of {_CLASS}"))
        elif isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__slots__" for t in node.targets
        ):
            if any(
                isinstance(n, ast.Constant) and n.value == _SLOT
                for n in ast.walk(node.value)
            ):
                found.append((line, "R6", f"a class declaring a {_SLOT} slot"))
        if isinstance(node, ast.Call):
            callee = node.func
            name = callee.attr if isinstance(callee, ast.Attribute) else None
            if isinstance(callee, ast.Name):
                name = callee.id
            if name == "__set__":
                found.append((line, "R4", "a descriptor's __set__ called directly"))
            strings = _string_args(node)
            if name == "__setattr__" and _SLOT in strings:
                found.append((line, "R5", f"__setattr__ writing {_SLOT}"))
            if name in {"setattr", "__setattr__"} and "__class__" in strings:
                found.append((line, "R6", "a __class__ assignment by setattr"))
    return sorted(set(found))


def _files() -> Iterable[Tuple[str, Path]]:
    for top in _SCANNED:
        for path in sorted((_ROOT / top).rglob("*.py")):
            yield path.relative_to(_ROOT).as_posix(), path


@lru_cache(maxsize=1)
def _scan() -> Tuple[dict, dict, int]:
    """``(violations, exempted hits, files scanned)`` over the repository."""
    found, exempted, files = {}, {}, 0
    for rel, path in _files():
        files += 1
        if rel in EXEMPT:
            continue
        for hit in violations(path.read_text(encoding="utf-8"), rel):
            if (rel, hit[1]) in RULE_EXEMPTIONS:
                exempted.setdefault((rel, hit[1]), []).append(hit)
            else:
                found.setdefault(rel, []).append(hit)
    return found, exempted, files


def test_nothing_outside_the_minting_module_reaches_the_mint():
    found, _, files = _scan()
    assert files > 1000, f"scanned only {files} files"
    assert not found, (
        "model-bound content is minted only by "
        "faultmaven/infrastructure/security/model_content.py (#1998):\n  "
        + "\n  ".join(
            f"{rel}:{line} [{rule}] {what}"
            for rel, hits in found.items()
            for line, rule, what in hits
        )
    )


def test_every_rule_exemption_is_still_needed():
    """An exemption whose hit is gone would silently cover a new one."""
    _, exempted, _ = _scan()
    assert set(exempted) == set(
        RULE_EXEMPTIONS
    ), f"stale exemptions: {sorted(set(RULE_EXEMPTIONS) - set(exempted))}"


def test_the_exempt_files_exist_and_mint():
    """A positive control on the exemption: both files exist and use the mint."""
    for rel in EXEMPT:
        text = (_ROOT / rel).read_text(encoding="utf-8")
        assert violations(text, rel), f"{rel} no longer mints: drop its exemption"


def test_the_private_names_are_read_from_the_module():
    names = private_names()
    assert {"_mi" + "nt", _TOKEN} <= names, names
    assert not any(n.startswith("__") for n in names)


# ---------------------------------------------------------------------------
# Positive controls: each shape, planted in a file, is caught.
# ---------------------------------------------------------------------------

_IMPORT = f"from {_MODULE} import {_CLASS}\n"
_PUBLIC = f"from {_MODULE} import non_case_text\n"
_FORGE = "mt = non_case_text('x')\nf = str.__new__(type(mt), raw)\n"

PLANTED = {
    "construct": _IMPORT + f"x = {_CLASS}('raw', 'case', True)\n",
    "construct_by_attribute": "import faultmaven\n"
    f"x = faultmaven.infrastructure.security.{_LEAF}.{_CLASS}('raw')\n",
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
    "token_in_string": "import importlib\n"
    f"getattr(importlib.import_module('m'), '{_TOKEN}')\n",
    "private_import": f"from {_MODULE} import _mint\n",
    "star_import": f"from {_MODULE} import *\n",
    "module_import": f"import {_MODULE}\n",
    "module_import_as": f"import {_MODULE} as mc\n",
    "module_from_parent": f"from {_PARENT} import {_LEAF}\n",
    "module_by_attribute": "import faultmaven\n"
    f"m = faultmaven.infrastructure.security.{_LEAF}\n",
    "getattr_module": f"getattr({_LEAF}, 'ModelTex' + 't')\n",
    "vars_module": f"vars({_LEAF})\n",
    "dict_module": f"{_LEAF}.__dict__\n",
    "eval_string": f"eval('{_CLASS}(1)')\n",
    # The nine forgeries the #2015 review reached (rev2015-probes/lexical_reach.py).
    "forge_type_object_setattr": _PUBLIC
    + _FORGE
    + f"object.__setattr__(f, 'source', 'case')\nobject.__setattr__(f, '{_SLOT}', True)\n",
    "forge_type_descriptor_set": _PUBLIC
    + _FORGE
    + f"type(mt).source.__set__(f, 'case')\ntype(mt).{_SLOT}.__set__(f, True)\n",
    "forge_class_assignment": _PUBLIC
    + f"class Fake(str):\n    __slots__ = ('source', '{_SLOT}')\n"
    f"f = Fake(raw)\nf.source = 'case'\nf.{_SLOT} = True\n"
    "f.__class__ = type(non_case_text('x'))\n",
    "forge_globals": _PUBLIC
    + "x = non_case_text.__globals__['_mint'](raw, 'case', True)\n",
    "forge_importlib_literal": "import importlib\n"
    f"x = importlib.import_module('{_MODULE}')._mint(raw, 'case', True)\n",
    "forge_sys_modules_literal": "import sys\n"
    f"x = sys.modules['{_MODULE}']._mint(raw, 'case', True)\n",
    "forge_dunder_import_literal": f"x = __import__('{_MODULE}', fromlist=['x'])"
    "._mint(raw, 'case', True)\n",
    "forge_getattr_private": "import importlib\n"
    f"m = importlib.import_module('{_MODULE}')\nx = getattr(m, '_mint')(raw)\n",
    "forge_getattr_class": "import importlib\n"
    f"m = importlib.import_module('{_MODULE}')\nK = getattr(m, '{_CLASS}')\n",
    # Each R-rule on its own, with nothing else in the file to catch it.
    "r3_globals_alone": "g = f.__globals__\n",
    "r4_set_alone": "d.__set__(obj, 1)\n",
    "r5_setattr_alone": f"object.__setattr__(obj, '{_SLOT}', True)\n",
    "r5_bound_setattr": f"obj.__setattr__('{_SLOT}', True)\n",
    "r6_class_alone": "obj.__class__ = K\n",
    "r6_setattr_class": "setattr(obj, '__class__', K)\n",
    "r6_slots_alone": f"class L(str):\n    __slots__ = ['{_SLOT}']\n",
    "r1_patch_target": f"patch('{_MODULE}.should_redact')\n",
    # R2 alone: the module object reached without naming its path.
    "r2_getmodule_attribute": _PUBLIC
    + "import inspect\nm = inspect.getmodule(non_case_text)\nx = m._mint(raw)\n",
    "r2_getmodule_getattr": _PUBLIC
    + "import inspect\nm = inspect.getmodule(non_case_text)\n"
    "x = getattr(m, '_mi' 'nt')(raw)\n",
}


@pytest.mark.parametrize("snippet", sorted(PLANTED))
def test_a_planted_violation_is_caught(snippet, tmp_path):
    planted = tmp_path / "planted.py"
    planted.write_text(PLANTED[snippet], encoding="utf-8")
    assert violations(
        planted.read_text(encoding="utf-8"), "faultmaven/planted.py"
    ), snippet


def test_a_relative_import_of_the_private_name_is_caught():
    other = "faultmaven/infrastructure/security/other.py"
    assert violations(f"from .{_LEAF} import _mint\n", other)
    assert violations(f"from . import {_LEAF}\n", other)


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
    "docstring_naming_the_module": f'"""See {_MODULE}."""\n',
    "other_slots": "class P:\n    __slots__ = ('source',)\n",
    "other_setattr": "object.__setattr__(obj, 'source', 1)\n",
}


@pytest.mark.parametrize("snippet", sorted(ALLOWED))
def test_a_type_test_is_allowed(snippet):
    assert not violations(ALLOWED[snippet], "faultmaven/ok.py"), snippet
