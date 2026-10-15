"""Only the dispatch reaches a provider's ``_generate``: the lexical check (#1998).

A provider has no public send method. Production code reaches an adapter's
``_generate`` only through ``faultmaven.infrastructure.llm.dispatch.send_to_model``,
which checks the call's model-bound content first. Overriding, mixing or
patching any OTHER provider attribute therefore bypasses nothing: the
dispatch never reads one. What is left is a deliberate private-name call, and
these rules flag every spelled one, by an AST walk over every Python file
outside ``tests/`` (``faultmaven/``, ``scripts/``, ``alembic/``):

* A1 — ``_generate`` named as an attribute (``provider._generate(...)``,
  ``OpenAIProvider._generate``, ``functools.partialmethod(X._generate)``,
  a store into it) anywhere but the adapters' own files and the dispatch. The
  exemption is by FILE, never by the name of the enclosing ``def``: a
  caller-side ``def _generate`` helper is no adapter. The parser normalises
  identifiers (NFKC), so ``provider._ｇenerate`` is ``_generate`` here.
* A2 — the string ``"_generate"`` (or a dotted path ending in it) anywhere
  but those files: ``getattr(p, "_generate")``, ``operator.methodcaller``,
  ``type(p).__dict__["_generate"]``, a ``patch`` target.
* A3 — ``send_to_model`` bound to anything but the dispatch's own function,
  outside ``dispatch.py``: an assignment or deletion (``dispatch.send_to_model =
  mine``, a module-level ``send_to_model = mine``), a ``def``/``class``/argument
  of that name, an import that binds the name from elsewhere or under ``as``,
  and the name as a string (``setattr``, a ``patch`` target). Binding the
  real function to another local name and rebinding THAT reaches nothing: a
  replacement still has to reach ``_generate``, which A1 flags.
* A4 — the adapters' own transport callables, the coroutines their
  ``_generate`` sends through (``_post_chat_completion``, ``_call_ollama_api``,
  ...), as attributes or strings, under the same exemption. Their names are
  read from the adapters' own ASTs, so the list cannot go stale.
* A5 — what ``send_to_model`` reads, rebound. Outside ``dispatch.py`` the
  dispatch module is never imported whole and its dotted path appears in no
  string (``importlib``, ``sys.modules``, a ``patch`` target); and no
  attribute store or delete, no subscript store into a namespace, no
  ``setattr``/``delattr`` and no ``__dict__.update`` targets a name
  ``dispatch.py`` binds — its functions, its constants and the names it
  imports, read from its own AST, less the standard library's and
  ``logger``. So ``dispatch.model_call_violations = lambda *a: []``,
  ``setattr(d, "refuse_if_refusing", ...)`` and ``d.CONTENT_KWARGS = ()``
  are flagged however the module object was reached
  (``sys.modules[send_to_model.__module__]``, ``inspect.getmodule``). The
  tables the check reads in the security module are read-only
  (``MappingProxyType``, ``frozenset``).
* T — nothing outside ``tests/`` imports ``tests``: the test minter
  (``tests.model_content.vetted``) mints raw text without a redaction pass,
  and is exempt from the mint check for that reason only.

The adapters' files are the providers package less ``registry.py``: the
registry is a caller of the dispatch, not an adapter, so it is held to the
rules like any other caller.

Out of scope, as for the mint check: reflection through a name computed at
runtime (``getattr(p, "_gen" + "erate")``), and reflection by ENUMERATION —
finding a callable by iterating a namespace and matching on a property of its
name or value (``[f for n, f in vars(cls).items() if n.endswith("generate")]``).
Both spell nothing a lexical rule can see; both are deliberate acts.
"""

from __future__ import annotations

import ast
import asyncio
import sys
import textwrap
import unicodedata
import warnings
from functools import lru_cache
from pathlib import Path
from typing import Iterable, List, Set, Tuple

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.architecture, pytest.mark.security]

_ROOT = Path(__file__).resolve().parents[3]
#: Where non-test Python lives in the repository.
_SCANNED = ("faultmaven", "scripts", "alembic")
_PROVIDERS = "faultmaven/infrastructure/llm/providers/"
_REGISTRY = _PROVIDERS + "registry.py"
_DISPATCH = "faultmaven/infrastructure/llm/dispatch.py"
_DISPATCH_MODULE = "faultmaven.infrastructure.llm.dispatch"
# Spelled in pieces, so a reader grepping for the names finds the rules, and
# the module docstring is not the only place they appear.
_SEND = "_gen" + "erate"
_DISPATCHER = "send_to" + "_model"


def exempt(path: str) -> bool:
    """The adapters' files and the dispatch: the only files that may name
    ``_generate`` or a transport callable (A1, A2, A4)."""
    if path == _DISPATCH:
        return True
    return (
        path.startswith(_PROVIDERS)
        and "/" not in path[len(_PROVIDERS) :]
        and path != _REGISTRY
    )


def _parse(source: str) -> ast.Module:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", (DeprecationWarning, SyntaxWarning))
        return ast.parse(source)


@lru_cache(maxsize=1)
def transport_names() -> frozenset:
    """A4: every coroutine the adapters' files define, ``_generate`` aside.

    A request is sent from a coroutine (every adapter awaits ``aiohttp``), so
    every ``async def`` in an adapter is one ``_generate`` may send through.
    """
    names: Set[str] = set()
    for path in sorted((_ROOT / _PROVIDERS).glob("*.py")):
        rel = path.relative_to(_ROOT).as_posix()
        if not exempt(rel):
            continue
        for node in ast.walk(_parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.AsyncFunctionDef) and node.name != _SEND:
                names.add(node.name)
    return frozenset(names)


@lru_cache(maxsize=1)
def dispatch_names() -> frozenset:
    """A5: every name ``dispatch.py`` binds at top level — what ``send_to_model``
    reads — read from its AST: its defs and classes, its assignments, and what
    it imports, less what it imports from the standard library and its
    ``logger``, which carry no check."""
    names: Set[str] = set()
    for node in _parse((_ROOT / _DISPATCH).read_text(encoding="utf-8")).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names.update(t.id for t in targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] not in sys.stdlib_module_names:
                    names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] not in sys.stdlib_module_names:
                names.update(alias.asname or alias.name for alias in node.names)
    return frozenset(names - {"logger"})


#: Calls that write into a namespace by name: ``setattr(obj, name, ...)`` and
#: ``delattr(obj, name)``, the name second; ``obj.__setattr__(name, ...)``
#: bound, the name first (``object.__setattr__(obj, name, ...)`` second).
_NAMED_WRITERS = frozenset({"setattr", "delattr"})
_BOUND_WRITERS = frozenset({"__setattr__", "__delattr__"})
#: Mapping methods that write a key: on a module's ``__dict__`` or ``vars()``.
_MAPPING_WRITERS = frozenset(
    {"update", "setdefault", "__setitem__", "pop", "__delitem__"}
)


def _written_name(node: ast.Call) -> object:
    """The constant name a namespace-writing call targets, else ``None``."""
    func, args = node.func, node.args
    index = None
    if isinstance(func, ast.Name) and func.id in _NAMED_WRITERS:
        index = 1
    elif isinstance(func, ast.Attribute) and func.attr in _BOUND_WRITERS:
        unbound = isinstance(func.value, ast.Name) and func.value.id in {
            "object",
            "type",
        }
        index = 1 if unbound else 0
    if index is not None and len(args) > index:
        arg = args[index]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
    return None


def _tests_module(name: str) -> bool:
    return name == "tests" or name.startswith("tests.")


def _rebinding_violations(
    tree: ast.AST, path: str, docstrings: Set[ast.AST]
) -> List[Tuple[int, str, str]]:
    """A5 and T, for a file outside ``dispatch.py`` (A5) and ``tests/`` (T)."""
    found: List[Tuple[int, str, str]] = []
    guarded = dispatch_names()
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == _DISPATCH_MODULE:
                    found.append((line, "A5", "the dispatch imported whole"))
                if _tests_module(alias.name):
                    found.append((line, "T", f"tests imported: {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            module = _module_of(node, path)
            for alias in node.names:
                if f"{module}.{alias.name}" == _DISPATCH_MODULE:
                    found.append((line, "A5", "the dispatch imported whole"))
            if node.level == 0 and _tests_module(module):
                found.append((line, "T", f"tests imported: {module}"))
        elif isinstance(node, ast.Attribute):
            if node.attr in guarded and isinstance(node.ctx, (ast.Store, ast.Del)):
                found.append((line, "A5", f"a dispatch name rebound: {node.attr}"))
        elif isinstance(node, ast.Subscript):
            key = node.slice
            if (
                isinstance(node.ctx, (ast.Store, ast.Del))
                and isinstance(key, ast.Constant)
                and key.value in guarded
            ):
                found.append(
                    (line, "A5", f"a dispatch name rebound by key: {key.value}")
                )
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node not in docstrings and (
                node.value == _DISPATCH_MODULE
                or node.value.startswith(_DISPATCH_MODULE + ".")
            ):
                found.append((line, "A5", "the dispatch named in a string"))
        elif isinstance(node, ast.Call):
            name = _written_name(node)
            if name in guarded:
                found.append((line, "A5", f"a dispatch name set by name: {name}"))
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in _MAPPING_WRITERS:
                keys = {k.arg for k in node.keywords} | {
                    a.value
                    for a in node.args[:1]
                    if isinstance(a, ast.Constant) and isinstance(a.value, str)
                }
                if keys & guarded:
                    found.append(
                        (
                            line,
                            "A5",
                            f"a dispatch name written: {sorted(keys & guarded)}",
                        )
                    )
            callee = (
                func.attr
                if isinstance(func, ast.Attribute)
                else getattr(func, "id", None)
            )
            if callee in {"import_module", "__import__"} and node.args:
                first = node.args[0]
                if (
                    isinstance(first, ast.Constant)
                    and isinstance(first.value, str)
                    and _tests_module(first.value)
                ):
                    found.append((line, "T", f"tests imported: {first.value}"))
    return found


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


def _names(value: str, names: Iterable[str]) -> Set[str]:
    """The names in ``names`` a string spells, alone or as a dotted path's end."""
    text = unicodedata.normalize("NFKC", value)
    return {name for name in names if text == name or text.endswith("." + name)}


def _module_of(node: ast.ImportFrom, path: str) -> str:
    if not node.level:
        return node.module or ""
    package = path.removesuffix(".py").replace("/", ".").split(".")[:-1]
    base = package[: len(package) - (node.level - 1)]
    return ".".join(base + ([node.module] if node.module else []))


def violations(source: str, path: str) -> List[Tuple[int, str, str]]:
    """``(line, rule, what)`` for every spelled way ``source`` reaches past the
    dispatch. ``path`` is repo-relative: it decides the exemptions."""
    found: List[Tuple[int, str, str]] = []
    tree = _parse(source)
    docstrings = _docstrings(tree)
    private = not exempt(path)
    transports = transport_names()
    rebindable = path != _DISPATCH
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Attribute):
            if private and node.attr == _SEND:
                found.append((line, "A1", f"a provider's {_SEND} reached"))
            if private and node.attr in transports:
                found.append((line, "A4", f"an adapter's transport: {node.attr}"))
            if (
                rebindable
                and node.attr == _DISPATCHER
                and isinstance(node.ctx, (ast.Store, ast.Del))
            ):
                found.append((line, "A3", f"{_DISPATCHER} rebound by attribute"))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node in docstrings:
                continue
            if private and _names(node.value, {_SEND}):
                found.append((line, "A2", f"{_SEND} named in a string"))
            if private and _names(node.value, transports):
                found.append((line, "A4", "an adapter's transport named in a string"))
            if rebindable and _names(node.value, {_DISPATCHER}):
                found.append((line, "A3", f"{_DISPATCHER} named in a string"))
        elif not rebindable:
            continue
        elif isinstance(node, ast.Name) and node.id == _DISPATCHER:
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                found.append((line, "A3", f"{_DISPATCHER} rebound"))
        elif (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.name == _DISPATCHER
        ):
            found.append((line, "A3", f"a def named {_DISPATCHER}"))
        elif isinstance(node, ast.arg) and node.arg == _DISPATCHER:
            found.append((line, "A3", f"an argument named {_DISPATCHER}"))
        elif isinstance(node, ast.ExceptHandler) and node.name == _DISPATCHER:
            found.append((line, "A3", f"{_DISPATCHER} bound by except"))
        elif isinstance(node, (ast.Global, ast.Nonlocal)) and _DISPATCHER in node.names:
            found.append((line, "A3", f"{_DISPATCHER} declared rebindable"))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname == _DISPATCHER:
                    found.append((line, "A3", f"a module imported as {_DISPATCHER}"))
        elif isinstance(node, ast.ImportFrom):
            module = _module_of(node, path)
            for alias in node.names:
                bound = alias.asname or alias.name
                canonical = (
                    module == _DISPATCH_MODULE
                    and alias.name == _DISPATCHER
                    and alias.asname is None
                )
                if bound == _DISPATCHER and not canonical:
                    found.append(
                        (line, "A3", f"{_DISPATCHER} bound by import from {module}")
                    )
    if rebindable:
        found.extend(_rebinding_violations(tree, path, docstrings))
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
        hits = violations(path.read_text(encoding="utf-8"), rel)
        if hits:
            found[rel] = hits
    return found, files


def test_nothing_outside_the_dispatch_reaches_a_provider():
    found, files = _scan()
    assert files > 500, f"scanned only {files} files"
    assert not found, (
        "a provider's _generate (or transport) is reached only by "
        f"{_DISPATCH} (#1998):\n  "
        + "\n  ".join(
            f"{rel}:{line} [{rule}] {what}"
            for rel, hits in found.items()
            for line, rule, what in hits
        )
    )


def test_the_exempt_files_are_the_adapters_and_the_dispatch():
    """A positive control on the exemption: every exempt file exists, the
    adapters define ``_generate``, and the registry is not exempt."""
    exempted = sorted(rel for rel, _ in _files() if exempt(rel))
    assert _DISPATCH in exempted
    assert _REGISTRY not in exempted
    adapters = [rel for rel in exempted if rel.startswith(_PROVIDERS)]
    defining = [
        rel
        for rel in adapters
        if any(
            isinstance(n, ast.AsyncFunctionDef) and n.name == _SEND
            for n in ast.walk(_parse((_ROOT / rel).read_text(encoding="utf-8")))
        )
    ]
    # base.py declares it abstract; every adapter but OpenRouter (which
    # inherits OpenAI's) defines its own.
    assert len(defining) >= 9, defining
    assert set(adapters) - set(defining) <= {
        _PROVIDERS + "__init__.py",
        _PROVIDERS + "openrouter_provider.py",
    }


def test_the_transport_names_are_read_from_the_adapters():
    names = transport_names()
    assert {
        "_post_chat_completion",
        "_correct_token_param_and_retry",
        "_call_ollama_api",
        "_call_openai_compatible_api",
        "_call_llamacpp_api",
    } <= names, names
    assert _SEND not in names


# ---------------------------------------------------------------------------
# Positive controls: each bypass, planted in a production file, is caught.
# ---------------------------------------------------------------------------

#: Fable's probe_bypass.py cases (B3, B5, B7, B11-B14) and the dispatch's own
#: positive control (P0).
PLANTED = {
    "P0 provider._generate(prompt=raw)": "r = await provider._generate(prompt=raw)\n",
    "B3 type(p).__dict__['_generate']": (
        "r = await type(provider).__dict__['_generate'](provider, prompt=raw)\n"
    ),
    "B5 operator.methodcaller": (
        "r = await operator.methodcaller('_generate', prompt=raw)(provider)\n"
    ),
    "B7 NFKC identifier": "r = await provider._ｇenerate(prompt=raw)\n",
    "B7 NFKC string": "r = await getattr(provider, '_ｇenerate')(prompt=raw)\n",
    "B11 caller-side def _generate": (
        "class Loop:\n"
        "    async def _generate(self, provider, raw):\n"
        "        return await provider._generate(prompt=raw)\n"
    ),
    "B12 class attribute": (
        "r = await OpenAIProvider._generate(provider, prompt=raw)\n"
    ),
    "B13 partialmethod": "send = functools.partialmethod(BaseLLMProvider._generate)\n",
    "B14a dispatch.send_to_model = mine": "dispatch.send_to_model = mine\n",
    "B14b setattr": "setattr(dispatch, 'send_to_model', mine)\n",
    "B14c patch target": (
        "patch('faultmaven.infrastructure.llm.dispatch.send_to_model')\n"
    ),
    "getattr _generate": "f = getattr(provider, '_generate')\n",
    "patch _generate": "patch.object(OpenAIProvider, '_generate', raw_send)\n",
    "a store into _generate": "provider._generate = raw_send\n",
    # A3: every other way to bind the name to something else.
    "A3 module-level rebind": "send_to_model = raw_send\n",
    "A3 tuple target": "send_to_model, other = raw_send, 1\n",
    "A3 del": "del dispatch.send_to_model\n",
    "A3 def": "async def send_to_model(provider, **kw):\n    return 1\n",
    "A3 argument": "async def f(send_to_model=raw_send):\n    return 1\n",
    "A3 import from elsewhere": "from mine import send_to_model\n",
    "A3 import as": "from mine import raw_send as send_to_model\n",
    "A3 the real one under another name, then that name": (
        "from faultmaven.infrastructure.llm.dispatch import raw as send_to_model\n"
    ),
    "A3 module as the name": "import mine as send_to_model\n",
    "A3 global": "def f():\n    global send_to_model\n    send_to_model = 1\n",
    "A3 except": "try:\n    pass\nexcept E as send_to_model:\n    pass\n",
    # A5: what send_to_model reads, rebound (#2023 review).
    "A5 validator rebound": (
        "from faultmaven.infrastructure.llm import dispatch\n"
        "dispatch.model_call_violations = lambda *a: []\n"
    ),
    "A5 the dispatch imported whole": "import faultmaven.infrastructure.llm.dispatch\n",
    "A5 the dispatch imported whole, aliased": (
        "from faultmaven.infrastructure.llm import dispatch as d\n"
    ),
    "A5 setattr": "setattr(d, 'refuse_if_refusing', lambda *a: None)\n",
    "A5 delattr": "delattr(d, 'CONTENT_KWARGS')\n",
    "A5 object.__setattr__": "object.__setattr__(d, 'GENERATE_KWARGS', frozenset())\n",
    "A5 bound __setattr__": "d.__setattr__('REFUSE_RAW_MODEL_CONTENT', False)\n",
    "A5 content kwargs emptied": "d.CONTENT_KWARGS = ()\n",
    "A5 an imported validator rebound": "d.message_violations = lambda m: []\n",
    "A5 via __module__": (
        "import sys\nsys.modules[send_to_model.__module__].model_call_violations = f\n"
    ),
    "A5 vars() store": "vars(inspect.getmodule(send_to_model))['plain_text'] = f\n",
    "A5 __dict__.update": "sys.modules[m].__dict__.update(text_violation=f)\n",
    "A5 patch target": "patch('faultmaven.infrastructure.llm.dispatch.text_violation')\n",
    "A5 sys.modules by name": (
        "import sys\nm = sys.modules['faultmaven.infrastructure.llm.dispatch']\n"
    ),
    # T: the test minter imported from production code.
    "T from tests import": "from tests.model_content import vetted\n",
    "T import tests": "import tests.model_content\n",
    "T import_module": "importlib.import_module('tests.model_content')\n",
    # A4: an adapter's transport reached around _generate.
    "A4 OpenAI transport": "r = await provider._post_chat_completion(payload)\n",
    "A4 local transport": "r = await provider._call_ollama_api(raw)\n",
    "A4 transport by string": "f = getattr(provider, '_call_llamacpp_api')\n",
}

#: rev2015e-probes/r7_reach.py: every way the #2015 review replaced
#: ``generate()``. On the dispatch none of them changes what is sent — the
#: dispatch never calls ``generate`` — and each one names ``_generate``, as an
#: attribute (A1) or a string (A2).
R7_PRE = """import functools, types
from faultmaven.infrastructure.llm.providers.openai_provider import OpenAIProvider
p = OpenAIProvider(CFG)
"""
R7_REACH = {
    "vars(p)['generate'] = f": "vars(p)['generate'] = p._generate\n",
    "p.__dict__['generate'] = f": "p.__dict__['generate'] = p._generate\n",
    "p.__dict__.update(generate=f)": "p.__dict__.update(generate=p._generate)\n",
    "setattr(p, name, f) [computed]": (
        "name = 'gene' + 'rate'\nsetattr(p, name, p._generate)\n"
    ),
    "p.generate = types.MethodType(...)": (
        "p.generate = types.MethodType(OpenAIProvider._generate, p)\n"
    ),
    "class decorator sets cls.generate": textwrap.dedent("""\
        def deco(cls):
            cls.generate = cls._generate
            return cls
        @deco
        class P(OpenAIProvider):
            pass
        p = P(CFG)
        """),
    "mixin defines async def generate": textwrap.dedent("""\
        class Mixin:
            async def generate(self, prompt, **kw):
                return await self._generate(prompt, **kw)
        class P(Mixin, OpenAIProvider):
            pass
        p = P(CFG)
        """),
    "mixin: generate = partialmethod(...)": textwrap.dedent("""\
        class Mixin:
            async def _send(self, prompt, **kw):
                return await self._generate(prompt, **kw)
            generate = functools.partialmethod(_send)
        class P(Mixin, OpenAIProvider):
            pass
        p = P(CFG)
        """),
    "subclass __getattribute__ redirect": textwrap.dedent("""\
        class P(OpenAIProvider):
            def __getattribute__(self, name):
                if name == "generate":
                    return object.__getattribute__(self, "_generate")
                return object.__getattribute__(self, name)
        p = P(CFG)
        """),
    "type.__setattr__(cls, 'generate', f)": (
        "class P(OpenAIProvider):\n    pass\n"
        "type.__setattr__(P, 'generate', P._generate)\np = P(CFG)\n"
    ),
    "duck-typed provider (no BaseLLMProvider)": textwrap.dedent("""\
        class Duck:
            provider_name = "duck"
            async def generate(self, prompt, **kw):
                return await OpenAIProvider._generate(self, prompt, **kw)
        p = Duck()
        """),
}


@pytest.mark.parametrize("name", sorted(PLANTED))
def test_a_planted_bypass_is_caught(name):
    assert violations(PLANTED[name], "faultmaven/planted.py"), name


@pytest.mark.parametrize("name", sorted(R7_REACH))
def test_every_r7_reach_replacement_is_flagged(name):
    hits = violations(R7_PRE + R7_REACH[name], "faultmaven/planted.py")
    assert {hit[1] for hit in hits} & {"A1", "A2"}, (name, hits)


def test_the_registry_is_held_to_the_rules():
    """The registry is in the providers package and is still a caller."""
    assert violations(PLANTED["P0 provider._generate(prompt=raw)"], _REGISTRY)


def test_the_exemption_is_by_file_not_by_def_name():
    """B11: a caller-side ``def _generate`` exempts nothing; an adapter's file
    may call its own ``_generate`` (OpenRouter's inheritance, a retry)."""
    source = PLANTED["B11 caller-side def _generate"]
    assert {hit[1] for hit in violations(source, "faultmaven/planted.py")} == {"A1"}
    assert not violations(source, _PROVIDERS + "openai_provider.py")
    assert not violations(source, _DISPATCH)


def test_a_relative_whole_import_of_the_dispatch_is_caught():
    """``from . import dispatch`` from the router's own package resolves to the
    dispatch module; from anywhere else it is some other module."""
    source = "from . import dispatch\n"
    in_llm = "faultmaven/infrastructure/llm/router_like.py"
    assert {hit[1] for hit in violations(source, in_llm)} == {"A5"}
    assert not violations(source, "faultmaven/planted.py")


def test_the_dispatch_may_bind_its_own_name():
    assert not violations(
        "async def send_to_model(provider, **kw):\n    return 1\n", _DISPATCH
    )


ALLOWED = {
    "the canonical import": (
        "from faultmaven.infrastructure.llm.dispatch import send_to_model\n"
    ),
    "a call": "r = await send_to_model(provider, prompt=p)\n",
    "a call by attribute": "r = await dispatch.send_to_model(provider, prompt=p)\n",
    "the real one under a local alias, rebound (B14d)": (
        "from faultmaven.infrastructure.llm.dispatch import send_to_model as s\n"
        "s = mine\n"
    ),
    "route": "r = await router.route(prompt=p)\n",
    "a public provider attribute": "provider.generate = mine\n",
    "a docstring naming both": '"""Calls send_to_model, which calls _generate."""\n',
    "a longer identifier": "x = self._generate_title_with_llm()\n",
    "a computed name (out of scope)": "getattr(p, '_gen' + 'erate')\n",
    # A5 is narrow: reading a dispatch name, or the name as plain data, is not
    # a rebinding (the reviewer's first draft flagged plain string equality).
    "importing dispatch names": (
        "from faultmaven.infrastructure.llm.dispatch import (\n"
        "    RawModelContentRefused, model_call_violations, send_to_model,\n)\n"
    ),
    "a dispatch name read as an attribute": "x = dispatch_like.CONTENT_KWARGS\n",
    "a dispatch name as a dict key": "row = {'plain_text': 1}\nrow['plain_text']\n",
    "a dispatch name compared": "ok = name == 'model_call_violations'\n",
    "a dict update with other keys": "kwargs.update(messages=m, tools=t)\n",
    "the logger rebound": "module.logger = other\n",
    "a module named like tests": "from faultmaven.tests_support import x\n",
}


@pytest.mark.parametrize("name", sorted(ALLOWED))
def test_an_ordinary_use_is_allowed(name):
    assert not violations(ALLOWED[name], "faultmaven/ok.py"), name


# ---------------------------------------------------------------------------
# Runtime: every r7_reach replacement is harmless at the dispatch.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(R7_REACH))
def test_every_r7_reach_replacement_is_refused_raw_at_the_dispatch(name, monkeypatch):
    """With refusal on, raw content sent to each modified provider is refused
    before its transport, and minted content still reaches the adapter's own
    ``_generate`` — whatever ``generate`` now is."""
    from faultmaven.infrastructure.llm import dispatch
    from faultmaven.infrastructure.llm.providers.base import (
        LLMResponse,
        ProviderConfig,
    )
    from faultmaven.infrastructure.llm.providers.openai_provider import (
        OpenAIProvider,
    )
    from tests.model_content import vetted

    sent = []

    async def transport(self, prompt=None, **kwargs):
        sent.append(prompt)
        return LLMResponse(
            content="ok",
            confidence=1.0,
            provider="p",
            model="m",
            tokens_used=1,
            response_time_ms=1,
        )

    monkeypatch.setattr(OpenAIProvider, _SEND, transport)
    monkeypatch.setattr(dispatch, "REFUSE_RAW_MODEL_CONTENT", True)
    namespace = {
        "CFG": ProviderConfig(
            name="openai",
            api_key="k",
            base_url="http://127.0.0.1:9",
            models=["m"],
            default_model="m",
        )
    }
    exec(compile(R7_PRE + R7_REACH[name], "planted.py", "exec"), namespace)
    provider = namespace["p"]

    with pytest.raises(TypeError, match="prompt raw"):
        asyncio.run(dispatch.send_to_model(provider, prompt="RAW 10.9.8.7"))
    assert not sent, f"{name}: raw content reached a transport"
    if hasattr(provider, _SEND):  # the duck has no transport of its own
        asyncio.run(dispatch.send_to_model(provider, prompt=vetted("ok")))
        assert sent == ["ok"] and type(sent[0]) is str
