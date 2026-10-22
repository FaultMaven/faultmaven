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
* R1: no string names the minting module or the dispatch
  (``faultmaven/infrastructure/llm/dispatch.py``) by path: its dotted path,
  its ``parent:leaf`` form, or anything under either (``path.attr``,
  ``path:attr``, ``parent:leaf.attr``) — the forms ``importlib``,
  ``sys.modules``, ``__import__``, ``pkgutil.resolve_name`` and a ``patch``
  target take — and no string is the minting module's leaf name alone
  (``getattr(security_package, "model_content")``). A module's own file is
  exempt for its own path;
* R2: no private name of the minting module appears as an attribute, and no
  string — any string, in any file — has a SEGMENT (split on ``.`` and ``:``)
  equal to a private name of the minting module or of the dispatch, read from
  their own sources so the lists cannot go stale. So ``'x.y:_mint'``,
  ``'_mint.__call__'``, ``pkgutil.resolve_name('tests.model_content:_mint')``,
  ``operator.attrgetter('model_content._mint')`` and the literal parts of an
  f-string (``f"{mod}:_mint"``) are all caught: the module can be reached
  without naming it (``type(v).__module__``, ``inspect.getmodule``), and a
  private name is the part that cannot be avoided. The minting module, the
  dispatch (for its own names) and the test minter are exempt;
* R3: no function's ``__globals__`` is read;
* R4: no descriptor's ``__set__`` is called directly;
* R5: no ``__setattr__`` call writes a ``redacted`` attribute — the slot every
  forgery has to set;
* R6: no ``__class__`` is assigned, and no class declares a ``redacted`` slot —
  the two halves of swapping a look-alike's class.

Which code may reach a provider's ``_generate`` is a separate rule, kept in
``test_model_dispatch_lexical_1998.py``.

An exemption covers one hit: a (file, rule, line text) entry, so a second
hit of the same rule in the same file is a violation.

The raw-text scan catches the first group's shapes where the AST cannot see
them, inside strings handed to ``eval``, ``exec`` or ``getattr``.

Out of scope, by the ruling, and nothing else: reflection through a name
computed at runtime — a string built by concatenation, ``chr``, a format of
non-literal parts or any other computation (``import_module("faultmaven." +
name)``, ``getattr(obj, "__se" + "t__")``, ``"_mi" + "nt"``, a slot name built
from pieces) — and runtime mutation of a first-party module's namespace
(ruling D1). Bypass requires a deliberate act: a flagged spelling, computed
reflection, or such a mutation. Every LITERAL spelling of a route to a forged
value, including through ``type()`` of a minted value, ends in one of the
shapes above.
"""

from __future__ import annotations

import ast
import re
import warnings
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

import pytest

from tests.source_tree import PACKAGE_ROOT, parse_file

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
# The dispatch: its private names and its path are held to R1/R2 as well.
_DISPATCH_PARENT = "faultmaven.infrastructure.llm"
_DISPATCH_LEAF = "dis" + "patch"
_DISPATCH_MODULE = f"{_DISPATCH_PARENT}.{_DISPATCH_LEAF}"
_DISPATCH_FILE = "faultmaven/infrastructure/llm/" + _DISPATCH_LEAF + ".py"
#: R2's segment separators: ``a.b`` attribute paths and ``a.b:c`` resolve-names.
_SEGMENTS = re.compile(r"[.:]")
#: A private name of each module, in pieces, for the planted controls.
_PRIVATE = "_mi" + "nt"
_DISPATCH_PRIVATE = "_provider" + "_label"
#: The two files allowed to mint.
EXEMPT = frozenset({_MODULE_FILE, "tests/" + _LEAF + ".py"})

#: (file, rule, the hit's line, stripped) -> why that one hit is no forgery.
RULE_EXEMPTIONS: Dict[Tuple[str, str, str], str] = {
    (
        "tests/unit/modules/agent/tools/test_web_search_tool.py",
        "R6",
        "google_provider.__class__ = GoogleCSEProvider",
    ): (
        "sets a mock's __class__ to GoogleCSEProvider so the tool's isinstance "
        "dispatch takes the CSE branch; the file mints nothing"
    ),
}

#: The raw-text scan's patterns all contain one of these, so a file without
#: any is skipped by that scan. The AST walk has no such gate: a string
#: constant need not appear in the source as written (``'_mi' 'nt'``,
#: ``'\\x5fmint'``), so every file is parsed.
_TEXT_TRIGGERS = (_CLASS, _LEAF, _TOKEN)

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


def _parse(source: str | Path) -> ast.Module:
    if isinstance(source, Path):
        if source.is_relative_to(PACKAGE_ROOT):
            return parse_file(source)
        source = source.read_text(encoding="utf-8")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", (DeprecationWarning, SyntaxWarning))
        return ast.parse(source)


@lru_cache(maxsize=1)
def private_names() -> frozenset:
    """The minting module's private top-level names, read from its source."""
    return _private_names_of(_MODULE_FILE)


@lru_cache(maxsize=1)
def dispatch_private_names() -> frozenset:
    """The dispatch's private top-level names, read from its source."""
    return _private_names_of(_DISPATCH_FILE)


def _private_names_of(rel: str) -> frozenset:
    tree = _parse(_ROOT / rel)
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


def _names_a_module(value: str, path: str) -> Optional[str]:
    """R1: which module ``value`` names by path, if any.

    ``:`` is read as ``.``, so ``parent:leaf``, ``path:attr`` and
    ``parent:leaf.attr`` are the dotted path or something under it.
    """
    dotted = value.replace(":", ".")
    if value == _LEAF:
        return "the minting module's leaf name"
    for module, own_file, what in (
        (_MODULE, _MODULE_FILE, "the minting module"),
        (_DISPATCH_MODULE, _DISPATCH_FILE, "the dispatch"),
    ):
        if path != own_file and (dotted == module or dotted.startswith(module + ".")):
            return what
    return None


def violations(
    source: str, path: str, tree: Optional[ast.Module] = None
) -> List[Tuple[int, str, str]]:
    """``(line, rule, what)`` for every spelled way ``source`` reaches the mint.

    ``path`` is repo-relative; it resolves relative imports.
    """
    found: List[Tuple[int, str, str]] = []
    if any(trigger in source for trigger in _TEXT_TRIGGERS):
        for lineno, line in enumerate(source.splitlines(), 1):
            for pattern, what in _TEXT_PATTERNS:
                if pattern.search(line):
                    found.append((lineno, "text", what))
    tree = _parse(source) if tree is None else tree
    # Only the type-test rule reads parents, and it needs the class's name as
    # an identifier — which, unlike a string, is spelled in the source.
    parents = (
        {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
        if _CLASS in source
        else {}
    )
    docstrings: List[Set[ast.AST]] = []  # computed on the first string hit

    def is_docstring(node: ast.AST) -> bool:
        if not docstrings:
            docstrings.append(_docstrings(tree))
        # Only PROSE is exempt: a one-token docstring is a name a caller can
        # read back through ``__doc__`` and resolve (#2023 narrow re-check).
        return node in docstrings[0] and len(node.value.split()) > 1

    private = private_names()
    # Strings may name neither module's privates, except the dispatch its own.
    string_private = private | (
        dispatch_private_names() if path != _DISPATCH_FILE else frozenset()
    )
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
            r1 = _names_a_module(node.value, path)
            segments = set(_SEGMENTS.split(node.value))
            r2 = segments & string_private
            if (r1 or r2) and not is_docstring(node):
                if r1:
                    found.append((line, "R1", f"{r1} named in a string"))
                if r2:
                    found.append(
                        (line, "R2", f"a private name in a string: {sorted(r2)}")
                    )
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


def apply_exemptions(rel: str, source: str, hits: list) -> Tuple[list, dict]:
    """``(violations, {exemption key: [hits]})``: an exemption covers only the
    hit on its own line text."""
    lines = source.splitlines()
    found, exempted = [], {}
    for hit in hits:
        text = lines[hit[0] - 1].strip() if 0 < hit[0] <= len(lines) else ""
        key = (rel, hit[1], text)
        if key in RULE_EXEMPTIONS:
            exempted.setdefault(key, []).append(hit)
        else:
            found.append(hit)
    return found, exempted


@lru_cache(maxsize=1)
def _scan() -> Tuple[dict, dict, int]:
    """``(violations, exempted hits, files scanned)`` over the repository."""
    found, exempted, files = {}, {}, 0
    for rel, path in _files():
        files += 1
        if rel in EXEMPT:
            continue
        source = path.read_text(encoding="utf-8")
        tree = _parse(path)
        hits, covered = apply_exemptions(rel, source, violations(source, rel, tree))
        if hits:
            found[rel] = hits
        exempted.update(covered)
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


def test_every_rule_exemption_covers_exactly_its_one_hit():
    """An exemption whose hit is gone would silently cover a new one, and one
    that covered two would hide the second."""
    _, exempted, _ = _scan()
    assert set(exempted) == set(
        RULE_EXEMPTIONS
    ), f"stale exemptions: {sorted(set(RULE_EXEMPTIONS) - set(exempted))}"
    for key, hits in exempted.items():
        assert len(hits) == 1, f"{key} covers {len(hits)} hits"


def test_a_second_hit_in_an_exempted_file_is_a_violation():
    """A full class-swap forge planted in the R6-exempted file is caught."""
    rel = "tests/unit/modules/agent/tools/test_web_search_tool.py"
    source = (_ROOT / rel).read_text(encoding="utf-8") + (
        f"\n\nclass Fake(str):\n    __slots__ = ('source', '{_SLOT}')\n\n\n"
        "fake = Fake('raw')\nfake.__class__ = type(non_case_text('x'))\n"
    )
    hits, covered = apply_exemptions(rel, source, violations(source, rel))
    assert {hit[1] for hit in hits} == {"R6"} and len(hits) == 2, hits
    assert len(next(iter(covered.values()))) == 1


def test_the_exempt_files_exist_and_mint():
    """A positive control on the exemption: both files exist and use the mint."""
    for rel in EXEMPT:
        text = (_ROOT / rel).read_text(encoding="utf-8")
        tree = _parse(_ROOT / rel)
        assert violations(text, rel, tree), f"{rel} no longer mints: drop its exemption"


def test_the_private_names_are_read_from_the_module():
    names = private_names()
    assert {"_mi" + "nt", _TOKEN} <= names, names
    assert not any(n.startswith("__") for n in names)
    dispatch_names = dispatch_private_names()
    assert {_DISPATCH_PRIVATE, "_sum" + "mary"} <= dispatch_names, dispatch_names


def test_the_dispatch_may_name_its_own_privates_and_path():
    """R1/R2 exempt the dispatch for ITS OWN names, never the minting module's."""
    own = f"x = '{_DISPATCH_MODULE}:{_DISPATCH_PRIVATE}'\n"
    assert not violations(own, _DISPATCH_FILE)
    assert {h[1] for h in violations(own, "faultmaven/planted.py")} == {"R1", "R2"}
    theirs = f"x = 'a:{_PRIVATE}'\n"
    assert {h[1] for h in violations(theirs, _DISPATCH_FILE)} == {"R2"}


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
    # #2023 final delta: every LITERAL spelling of a private name reaches R2,
    # and every path form R1 (built from pieces, so this file passes its scan).
    "r2_resolve_name_module_colon_private": (
        f"m = pkgutil.resolve_name('{_MODULE}:{_PRIVATE}')\n"
    ),
    "r2_resolve_name_parent_colon_leaf_private": (
        f"m = pkgutil.resolve_name('{_PARENT}:{_LEAF}.{_PRIVATE}')\n"
    ),
    "r2_attrgetter_leaf_private": (
        f"m = operator.attrgetter('{_LEAF}.{_PRIVATE}')(faultmaven.infrastructure.security)\n"
    ),
    "r2_resolve_name_test_minter": (
        f"m = pkgutil.resolve_name('tests.{_LEAF}:{_PRIVATE}')\n"
    ),
    "r2_any_module_colon_private": f"m = resolve('x.y:{_PRIVATE}')\n",
    "r2_private_then_attribute": f"m = resolve('{_PRIVATE}.__call__')\n",
    "r2_fstring_literal_segment": "m = resolve(f'{mod}:" + _PRIVATE + "')\n",
    "r2_dispatch_private": (f"f = pkgutil.resolve_name('x:{_DISPATCH_PRIVATE}')\n"),
    "r1_leaf_by_getattr": (
        f"m = getattr(faultmaven.infrastructure.security, '{_LEAF}')\n"
    ),
    "r1_parent_colon_leaf": f"m = pkgutil.resolve_name('{_PARENT}:{_LEAF}')\n",
    "r1_module_colon_attribute": (
        f"m = pkgutil.resolve_name('{_MODULE}:non_case_text')\n"
    ),
    "r1_dispatch_path": f"m = importlib.import_module('{_DISPATCH_MODULE}')\n",
    "r1_dispatch_parent_colon_leaf": (
        f"m = pkgutil.resolve_name('{_DISPATCH_PARENT}:{_DISPATCH_LEAF}')\n"
    ),
    # R2 alone: the module object reached without naming its path.
    "r2_getmodule_attribute": _PUBLIC
    + "import inspect\nm = inspect.getmodule(non_case_text)\nx = m._mint(raw)\n",
    "r2_getmodule_getattr": _PUBLIC
    + "import inspect\nm = inspect.getmodule(non_case_text)\n"
    "x = getattr(m, '_mi' 'nt')(raw)\n",
    # R2 in a file naming neither the module nor the class (#2015 delta).
    "r2_sys_modules_of_type": "import sys\ndef relabel(v, raw):\n"
    "    return sys.modules[type(v).__module__]._mint(raw, 'case', True)\n",
    "r2_getmodule_of_type": "import inspect\ndef relabel(v, raw):\n"
    "    return inspect.getmodule(type(v))._mint(raw, 'case', True)\n",
    "r2_vars_of_module": "import inspect\ndef relabel(v, raw):\n"
    "    return vars(inspect.getmodule(type(v)))['_mi' 'nt'](raw, 'case', True)\n",
    # A one-token docstring is a name read back through ``__doc__`` (#2023).
    "doc_resolve": "import pkgutil\ndef t():\n"
    f'    """{_MODULE}:_mint"""\n'
    "x = pkgutil.resolve_name(t.__doc__)('raw', 'case', True)\n",
}


def test_a_prose_docstring_naming_the_mint_stays_exempt():
    source = f'def t():\n    """Calls {_MODULE}:_mint for the case."""\n'
    assert not violations(source, "faultmaven/planted.py")


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
    # R2 is segment equality: a name that merely contains a private one is not it.
    "a_name_containing_a_private_one": "x = resolve('a.b:my_mint_helper')\n",
    "a_private_name_of_another_module": "x = resolve('a.b:_private')\n",
    "the_test_minter_by_path": "x = 'tests.model_content'\n",
    "a_computed_name_out_of_scope": "x = resolve('a:' + '_mi' + 'nt')\n",
    "the_dispatch_named_in_its_own_file_is_checked_elsewhere": "x = 'dispatch'\n",
}


@pytest.mark.parametrize("snippet", sorted(ALLOWED))
def test_a_type_test_is_allowed(snippet):
    assert not violations(ALLOWED[snippet], "faultmaven/ok.py"), snippet
