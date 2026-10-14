"""#1956: every LLM send site in the package is classified, by AST.

A send site is any ``<x>.route(...)`` / ``<x>.generate(...)`` call, and also
any way of reaching those methods without calling them at the site: an
uncalled reference (``send = router.route``, ``functools.partial(router.route,
...)``), ``getattr(x, "route")``, and a call through ``getattr(x, <computed
name>)`` — made directly, or through a name bound to it and called with any
argument (#1971). Each one must be ONE of:

(R) redacted at the site: every value that reaches the provider is provably
    what a case-redaction helper returned, or configuration. Anything the guard
    cannot prove is not R.

    - The content goes in exactly one of ``messages=`` / ``prompt=``, as a
      NAME. Every binding of that name that can reach the call — in the calling
      function or, for a closure, the enclosing one; both arms of a branch; a
      rebinding later in a loop around the call, after the closure is defined
      or in a nested function that declares it ``nonlocal`` — is
      ``await case_redacted(..., ctx)``,
      ``await case_redacted_messages(..., ctx)`` or
      ``await <ctx>.asanitize_messages(...)``.
    - The context cannot be ``None``: not a literal ``None``, not a name any
      binding of which is ``None`` (directly, through another name, or on one
      arm of a conditional), not a parameter that defaults to ``None``.
    - Nothing changes the sent value afterwards. The guard follows every name
      that can hold it or a part of it, in the binding function and every
      function nested in it: an alias (``alias = messages``), an element
      (``first = messages[0]``, ``for m in messages``), a container holding it
      (``[messages]``), a shallow copy (``messages[:]``, ``list(messages)``).
      It refuses a mutating method (``_MUTATORS``: the ones that write content
      into a list or dict) called on any of them or on anything reached from
      them (``messages[0].update(...)``); an item, attribute or augmented
      assignment through them; handing one to a call that is not a known
      reader, copy, redaction or send (``_add_context(messages, raw)``); and
      storing one on another object (``self.pending = messages``). A shallow
      copy's own list is new, so appending to it does not reach the send; its
      elements are the sent ones, so changing them does. A deep copy is
      independent.
    - Every other keyword is a routing parameter (``model``, ``max_tokens``,
      ...) whose value is configuration: a constant; a module-level name (or
      an attribute of one: an enum member) that the module binds only to
      configuration, that no function rebinds (``global``) and that, if it is
      a dict, list or set, nothing changes; the settings object
      (``get_settings()``, a name bound only from it, a ``settings`` parameter,
      ``self._settings``) or one of its methods called with configuration; a
      dict or list of those; or a parameter used for nothing but routing
      keywords. A local name must be bound only from those and changed by
      nothing but being stored under a routing key. An f-string, any other
      call, an attribute of a local or parameter, a parameter built into a
      larger value or also used as content: refused.
    - A ``**`` splat provably carries routing parameters only: a dict literal
      or ``dict(...)`` keyed only by them, with configuration values; a
      conditional between such; a local name bound only to such and changed
      only by assigning a routing key; or a call to a function defined in the
      same module, handed configuration, every return of which is such. Its
      name proves nothing, and a parameter cannot be proven.
(A) allowlisted below, with the reason it needs no case pass of its own AND the
    kinds of refusal it is admitted for (``Allowed.kinds``). A site in an
    allowlisted function refused for any other kind fails, so a different
    defect cannot hide behind an entry.

Anything else fails, with its location and kinds: a new send site has to
decide. What the guard does not follow: a value into another module (an
imported helper's returns or constants, a caller's arguments — so a routing
parameter's caller is trusted, a content parameter's is not), a ``getattr``
result handed on rather than called (``functools.partial(method, ...)``), and
the redaction helpers and the settings object by anything but their names. The #1952 guard
(``test_rebuilt_prompts_are_redacted_1949.py``) still pins the engine's own
prompt assembly; the two engine splat sites are allowlisted here on its
strength. The same idiom as ``test_openapi_documents_auth.py``: a classifier
that is told about every case, and refuses the ones it is not.
"""

from __future__ import annotations

import ast
import functools
import textwrap
import warnings
from pathlib import Path
from typing import NamedTuple

import pytest

import faultmaven

pytestmark = pytest.mark.unit

_ROOT = Path(faultmaven.__file__).parent
_SEND_METHODS = {"route", "generate"}
_REDACTING_HELPERS = {"case_redacted", "case_redacted_messages"}
_REDACTING_METHODS = {"asanitize_messages"}

# The kinds of refusal. An allowlist entry names the kinds it admits (#1971).
UNCALLED = "uncalled-reference"
GETATTR = "getattr-constant"
GETATTR_COMPUTED = "getattr-computed"
POSITIONAL = "positional"
NO_CONTENT = "no-content-keyword"
TWO_CONTENTS = "two-content-keywords"
OTHER_CHANNEL = "unfollowed-keyword"
ROUTING_VALUE = "routing-value"
SPLAT = "splat"
EXPRESSION = "content-expression"
PARAMETER = "content-parameter"
UNREDACTED = "unredacted-binding"
NONE_CONTEXT = "none-context"
CHANGED = "changed-after-redaction"


class Allowed(NamedTuple):
    kinds: frozenset
    reason: str


def _allowed(*kinds: str, reason: str) -> Allowed:
    return Allowed(frozenset(kinds), reason)


_STARLETTE = "a Starlette routing context's `route` attribute, not an LLM"
_LOGGER = "a logger's method chosen by level; the receiver is a logger, not an LLM"

#: (path relative to the repo, innermost enclosing function) -> the kinds of
#: refusal its sites are admitted for, and why they need no case pass.
ALLOWLIST: dict[tuple[str, str], Allowed] = {
    ("faultmaven/api/routes/admin_config.py", "check_llm_connection"): _allowed(
        EXPRESSION, reason="a constant connection-test prompt; no case content"
    ),
    ("faultmaven/infrastructure/llm/router.py", "generate"): _allowed(
        TWO_CONTENTS,
        OTHER_CHANNEL,
        ROUTING_VALUE,
        reason="router internals: ILLMProvider.generate delegating to route",
    ),
    ("faultmaven/infrastructure/llm/providers/registry.py", "route_request"): _allowed(
        PARAMETER,
        SPLAT,
        ROUTING_VALUE,
        reason="registry internals: the provider dispatch every routed call ends in",
    ),
    (
        "faultmaven/modules/knowledge/domain/services/conversion_service/service.py",
        "_convert",
    ): _allowed(
        UNREDACTED,
        ROUTING_VALUE,
        SPLAT,
        reason=(
            "sends prepared.redacted_messages, redacted once in _prepare_conversion "
            "(pinned by test_conversion_model_boundary_redaction_1901.py); the model "
            "is prepared.knowledge_model (settings.llm.get_knowledge_model() there) "
            "and the splat pipeline's _knowledge_route_kwargs, which the guard does "
            "not follow across the dataclass and the import"
        ),
    ),
    (
        "faultmaven/modules/agent/domain/services/out_of_band.py",
        "_one_answer_attempt",
    ): _allowed(
        SPLAT,
        reason=(
            "**route_kwargs is a parameter; its one caller, answer_out_of_band, "
            "hands it _role_route_kwargs(settings, 'synthesis') or {}. The "
            "content it sends is redacted at the site"
        ),
    ),
    (
        "faultmaven/core/investigation/milestone_engine/generation.py",
        "_tool_loop_call",
    ): _allowed(
        SPLAT,
        NO_CONTENT,
        reason=(
            "the engine's tool-loop splat; every message it carries is built "
            "through case_redacted (pinned by test_rebuilt_prompts_are_redacted_1949.py)"
        ),
    ),
    (
        "faultmaven/core/investigation/milestone_engine/generation.py",
        "llm_operation",
    ): _allowed(
        SPLAT,
        NO_CONTENT,
        reason=(
            "the engine's single-shot splat; its prompt is case_redacted at the top "
            "of _generate_structured_output_inner (pinned by #1952's guard)"
        ),
    ),
    ("faultmaven/api/route_enumeration.py", "iter_served_routes"): _allowed(
        GETATTR, reason=_STARLETTE
    ),
    ("faultmaven/api/route_enumeration.py", "iter_documented_routes"): _allowed(
        GETATTR, reason=_STARLETTE
    ),
    ("faultmaven/api/route_enumeration.py", "iter_served_endpoints"): _allowed(
        GETATTR, reason=_STARLETTE
    ),
    ("faultmaven/api/route_enumeration.py", "iter_unresolved_routes"): _allowed(
        GETATTR, reason=_STARLETTE
    ),
    ("faultmaven/api/middleware/route_policy.py", "_flattened_post_paths"): _allowed(
        GETATTR, reason=_STARLETTE
    ),
    ("faultmaven/infrastructure/logging/coordinator.py", "log_once"): _allowed(
        GETATTR_COMPUTED, reason=_LOGGER
    ),
    ("faultmaven/infrastructure/logging/unified.py", "operation"): _allowed(
        GETATTR_COMPUTED, reason=_LOGGER
    ),
    ("faultmaven/infrastructure/logging/unified.py", "operation_sync"): _allowed(
        GETATTR_COMPUTED, reason=_LOGGER
    ),
    ("faultmaven/infrastructure/logging/unified.py", "log_event"): _allowed(
        GETATTR_COMPUTED, reason=_LOGGER
    ),
    ("faultmaven/infrastructure/persistence/user_repository.py", "_account_filters"): (
        _allowed(
            GETATTR_COMPUTED,
            reason="a SQLAlchemy `func` case-folding function chosen by dialect",
        )
    ),
}

#: The sites that redact at the send, which the scan must find (positive
#: control: a classifier that recognised nothing would pass vacuously).
REDACTED_SITES = {
    ("faultmaven/core/investigation/intent_resolver.py", "_classify"),
    ("faultmaven/core/preprocessing/tier2/local_service.py", "_analyze"),
    ("faultmaven/modules/agent/domain/services/out_of_band.py", "_classify"),
    ("faultmaven/modules/agent/tools/document_qa_tool.py", "_synthesize"),
    ("faultmaven/modules/case/api/title_generation.py", "_generate_title_with_llm"),
    (
        "faultmaven/modules/knowledge/domain/services/conversion_service/pipeline.py",
        "_analyze",
    ),
    (
        "faultmaven/modules/knowledge/domain/services/document_preprocessor.py",
        "_run_content_triage",
    ),
}

_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)
_NESTED_SCOPES = (*_FUNCS, ast.ClassDef, ast.Lambda)

#: Keywords that carry routing parameters, never content.
_ROUTING_KWARGS = {
    "model",
    "max_tokens",
    "temperature",
    "top_p",
    "min_output_tokens",
    "reasoning_intent",
    "response_format",
    "provider_override",
}
_CONTENT_KWARGS = ("messages", "prompt")
#: Methods that write content into a list or dict in place. A method outside
#: this set (``get``, ``replace``, ``strip``, ...) is read as non-mutating.
_MUTATORS = {
    "append",
    "extend",
    "insert",
    "update",
    "setdefault",
    "__setitem__",
    "__iadd__",
    "__ior__",
}
#: Calls that only read what they are handed.
_READERS = {"len", "str", "repr", "bool", "isinstance", "id", "type", "hash"}
#: Calls that build a new container holding the same elements.
_SHALLOW_COPIES = {"list", "tuple", "dict", "sorted", "copy.copy"}
_DEEP_COPIES = {"copy.deepcopy"}
#: Calls a loop iterates through to reach the elements they are handed.
_ITERATORS = {"enumerate", "reversed", "iter", "zip"}


# --------------------------------------------------------------------------
# Scopes and bindings
# --------------------------------------------------------------------------


def _parents(tree: ast.AST) -> dict:
    return {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}


def _scopes(node, parents) -> list:
    """Enclosing functions, innermost first."""
    chain = []
    while node in parents:
        node = parents[node]
        if isinstance(node, _FUNCS):
            chain.append(node)
    return chain


def _module(node, parents) -> ast.AST:
    while node in parents:
        node = parents[node]
    return node


def _own_nodes(func):
    """The function's (or module's) body, not descending into nested
    functions, lambdas or classes."""
    stack = list(func.body)
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, _NESTED_SCOPES):
            stack.extend(ast.iter_child_nodes(node))


def _stmt(node, parents):
    while not isinstance(node, ast.stmt):
        node = parents[node]
    return node


def _binding_nodes(func, name: str) -> list:
    """Where ``func`` binds ``name``: in its own body, and in a nested function
    that declares it ``nonlocal``."""
    out = _own_bindings(func, name)
    for inner in ast.walk(func):
        if (
            inner is not func
            and isinstance(inner, _FUNCS)
            and any(
                isinstance(n, ast.Nonlocal) and name in n.names
                for n in _own_nodes(inner)
            )
        ):
            out += _own_bindings(inner, name)
    return out


def _own_bindings(func, name: str) -> list:
    out = []
    for node in _own_nodes(func):
        targets: list = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
        elif isinstance(node, (ast.AugAssign, ast.NamedExpr)):
            targets = [node.target]
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            targets = [node.target]
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            targets = [node.optional_vars]
        if any(
            isinstance(t, ast.Name) and t.id == name
            for target in targets
            for t in _store_targets(target)
        ):
            out.append(node)
    return out


def _bound_value(node, name: str):
    """The value a plain binding gives ``name``; ``None`` for a loop target,
    a with-item, an unpacking or an augmented assignment."""
    if isinstance(node, ast.Assign) and any(
        isinstance(t, ast.Name) and t.id == name for t in node.targets
    ):
        return node.value
    if isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and isinstance(
        node.target, ast.Name
    ):
        return node.value
    return None


def _parameters(func) -> set[str]:
    a = func.args
    names = [x.arg for x in a.posonlyargs + a.args + a.kwonlyargs]
    names += [x.arg for x in (a.vararg, a.kwarg) if x is not None]
    return set(names)


def _defaults_to_none(func, name: str) -> bool:
    a = func.args
    positional = a.posonlyargs + a.args
    defaults = dict(
        zip(
            [p.arg for p in positional[len(positional) - len(a.defaults) :]], a.defaults
        )
    )
    defaults.update(
        {p.arg: d for p, d in zip(a.kwonlyargs, a.kw_defaults) if d is not None}
    )
    default = defaults.get(name)
    return isinstance(default, ast.Constant) and default.value is None


class _Binding(NamedTuple):
    """Where a name seen from a scope chain is bound: the innermost function
    that binds it or takes it as a parameter (``None`` for a module-level or
    builtin name), its bindings there, and the chain from there outwards."""

    func: ast.AST | None
    nodes: list
    is_param: bool
    chain: list


def _resolve(name: str, chain: list) -> _Binding:
    for i, func in enumerate(chain):
        nodes = _binding_nodes(func, name)
        is_param = name in _parameters(func)
        if nodes or is_param:
            return _Binding(func, nodes, is_param, chain[i:])
    return _Binding(None, [], False, [])


def _ancestors(node, parents):
    while node in parents:
        node = parents[node]
        yield node


def _kills(later, earlier, parents) -> bool:
    """Whether ``later`` always runs after ``earlier``: it sits in a statement
    list that holds, at an earlier position, a statement containing
    ``earlier``. Two arms of a branch never kill each other."""
    s_later, s_earlier = _stmt(later, parents), _stmt(earlier, parents)
    block_owner = parents[s_later]
    for field in ("body", "orelse", "finalbody", "handlers"):
        block = getattr(block_owner, field, None)
        if isinstance(block, list) and s_later in block:
            index = block.index(s_later)
            chain = {s_earlier, *_ancestors(s_earlier, parents)}
            return any(stmt in chain for stmt in block[:index])
    return False


def _definitely_bound(stmts, name: str, before_line: int) -> bool:
    """Whether running ``stmts`` (those before ``before_line``) binds ``name``
    on every path: a plain binding, an ``if`` whose arms both do, or a
    ``with`` / ``try`` body that does. A loop body may run zero times."""
    for stmt in stmts:
        if stmt.lineno >= before_line:
            return False
        targets: list = []
        if isinstance(stmt, ast.Assign):
            targets = stmt.targets
        elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
            targets = [stmt.target]
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            return True
        if isinstance(stmt, ast.If) and (
            _definitely_bound(stmt.body, name, before_line)
            and _definitely_bound(stmt.orelse, name, before_line)
        ):
            return True
        if isinstance(stmt, (ast.With, ast.AsyncWith, ast.Try)) and (
            _definitely_bound(stmt.body, name, before_line)
        ):
            return True
    return False


def _loops_around(node, func, parents) -> list:
    out = []
    for anc in _ancestors(node, parents):
        if anc is func:
            break
        if isinstance(anc, (ast.For, ast.AsyncFor, ast.While)):
            out.append(anc)
    return out


# --------------------------------------------------------------------------
# What can change a value after it is bound (#1971 rules 1 and 2)
# --------------------------------------------------------------------------


def _least(*depths):
    known = [d for d in depths if d is not None]
    return min(known) if known else None


def _reach(expr, depth: dict):
    """The depth from which a change through ``expr`` reaches the tracked
    value, or ``None`` when ``expr`` holds no part of it. Depth 0: the value
    itself, an alias, or a part of it — any change reaches. Depth 1: a new
    container whose elements are tracked (a shallow copy, ``[messages]``) —
    changing the container does not reach, changing an element does."""
    if isinstance(expr, ast.Name):
        return depth.get(expr.id)
    if isinstance(expr, (ast.NamedExpr, ast.Await, ast.Starred)):
        return _reach(expr.value, depth)
    if isinstance(expr, ast.IfExp):
        return _least(_reach(expr.body, depth), _reach(expr.orelse, depth))
    if isinstance(expr, ast.BoolOp):
        return _least(*(_reach(v, depth) for v in expr.values))
    if isinstance(expr, (ast.Subscript, ast.Attribute)):
        d = _reach(expr.value, depth)
        if d is None:
            return None
        if isinstance(expr, ast.Subscript) and isinstance(expr.slice, ast.Slice):
            return max(d, 1)
        return max(d - 1, 0)
    if isinstance(expr, ast.Call):
        return _call_reach(expr, depth)
    if isinstance(expr, (ast.List, ast.Tuple, ast.Set)):
        d = _least(*(_reach(e, depth) for e in expr.elts))
        return None if d is None else d + 1
    if isinstance(expr, ast.Dict):
        d = _least(*(_reach(v, depth) for v in expr.values))
        return None if d is None else d + 1
    if isinstance(expr, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
        names = {n.id for n in ast.walk(expr) if isinstance(n, ast.Name)}
        return 1 if names & set(depth) else None
    if isinstance(expr, ast.BinOp):
        d = _least(_reach(expr.left, depth), _reach(expr.right, depth))
        return None if d is None else max(d, 1)
    return None


def _call_reach(call, depth: dict):
    func = ast.unparse(call.func)
    args = [_reach(a, depth) for a in call.args]
    if func in _DEEP_COPIES:
        return None
    if func in _SHALLOW_COPIES:
        d = _least(*args)
        return None if d is None else max(d, 1)
    if func in _ITERATORS:
        return _least(*args)
    if isinstance(call.func, ast.Attribute):
        d = _reach(call.func.value, depth)
        if d is not None:
            # ``x.copy()`` is a shallow copy; any other method's result is
            # read as a part of ``x``.
            return max(d, 1) if call.func.attr == "copy" else max(d - 1, 0)
    return None


def _store_targets(target):
    if isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            yield from _store_targets(elt)
    elif isinstance(target, ast.Starred):
        yield from _store_targets(target.value)
    else:
        yield target


def _derivations(node, depth: dict):
    """(name, depth) for each name ``node`` binds to a part of the value."""
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
        d = None if node.value is None else _reach(node.value, depth)
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    elif isinstance(node, ast.AugAssign):
        d = _reach(node.value, depth)
        d = None if d is None else max(d, 1)
        targets = [node.target]
    elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
        d = _reach(node.iter, depth)
        d = None if d is None else max(d - 1, 0)
        targets = [node.target]
    else:
        return
    if d is None:
        return
    for target in targets:
        unpacked = not isinstance(target, ast.Name)
        for t in _store_targets(target):
            if isinstance(t, ast.Name):
                yield t.id, max(d - 1, 0) if unpacked else d


def _holders(func, name: str) -> dict:
    """Every name in ``func`` and the functions nested in it that can hold the
    value ``name`` holds, or a part of it, with its depth (see ``_reach``)."""
    depth = {name: 0}
    grown = True
    while grown:
        grown = False
        for node in ast.walk(func):
            for held, d in _derivations(node, depth):
                if held not in depth or d < depth[held]:
                    depth[held] = d
                    grown = True
    return depth


def _root(expr):
    """The name an access chain starts from, and the hops between."""
    hops = 0
    while True:
        if isinstance(expr, (ast.Subscript, ast.Attribute)):
            expr, hops = expr.value, hops + 1
        elif isinstance(expr, ast.Call):
            expr = expr.func
        elif isinstance(expr, (ast.Starred, ast.Await, ast.NamedExpr)):
            expr = expr.value
        else:
            return (expr.id if isinstance(expr, ast.Name) else None), hops


def _harmless(call) -> bool:
    """A call that reads, copies, redacts or sends what it is handed."""
    func = ast.unparse(call.func)
    if func in _READERS | _SHALLOW_COPIES | _DEEP_COPIES | _ITERATORS:
        return True
    if func in _REDACTING_HELPERS:
        return True
    return isinstance(call.func, ast.Attribute) and (
        call.func.attr in _SEND_METHODS | _REDACTING_METHODS
    )


def _call_changes(call, depth: dict):
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr in _MUTATORS:
        root, hops = _root(func.value)
        if root in depth and hops >= depth[root]:
            yield f"is changed by {ast.unparse(func)[:50]}()"
    if isinstance(func, ast.Name) and func.id in depth:
        yield f"is changed through {func.id}(), a method taken from it"
    if not _harmless(call) and any(
        _reach(arg, depth) is not None
        for arg in [*call.args, *(k.value for k in call.keywords)]
    ):
        yield f"is handed to {ast.unparse(func)[:50]}(), which may change it"


def _store_changes(node, depth: dict):
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    for t in (t for target in targets for t in _store_targets(target)):
        if isinstance(t, ast.Name):
            if isinstance(node, ast.AugAssign) and depth.get(t.id) == 0:
                yield f"is extended in place by {t.id} +="
            continue
        root, hops = _root(t)
        if root in depth and hops - 1 >= depth[root]:
            yield f"is written through {ast.unparse(t)[:50]}"
        elif node.value is not None and _reach(node.value, depth) is not None:
            yield f"is stored on {ast.unparse(t)[:50]}, where anything can change it"


def _changes(func, name: str) -> list:
    """Every place in ``func`` (and the functions nested in it) that can change
    the value ``name`` holds, through any name that holds it or a part of it:
    ``(node, how)``. Order-insensitive: a change before the binding counts."""
    depth = _holders(func, name)
    out = []
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            out += [(node, how) for how in _call_changes(node, depth)]
        elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            out += [(node, how) for how in _store_changes(node, depth)]
    return out


# --------------------------------------------------------------------------
# Routing values and splats (#1971 rules 3 and 5)
# --------------------------------------------------------------------------


def _is_get_settings(expr) -> bool:
    return (
        isinstance(expr, ast.Call)
        and not expr.args
        and not expr.keywords
        and ast.unparse(expr.func).split(".")[-1] == "get_settings"
    )


def _settings_object(expr, chain: list) -> bool:
    """Whether ``expr`` is the settings object or an attribute of it:
    ``get_settings()``, a name bound only from it, a ``settings`` parameter,
    ``self._settings`` / ``self.settings``."""
    attrs = []
    while isinstance(expr, ast.Attribute):
        attrs.append(expr.attr)
        expr = expr.value
    if _is_get_settings(expr):
        return True
    if not isinstance(expr, ast.Name):
        return False
    if expr.id == "self" and attrs and attrs[-1] in {"settings", "_settings"}:
        return True
    binding = _resolve(expr.id, chain)
    if binding.is_param:
        return expr.id == "settings" and not binding.nodes
    return bool(binding.nodes) and all(
        _is_get_settings(_bound_value(node, expr.id)) for node in binding.nodes
    )


def _used_only_for_routing(func, name: str) -> bool:
    """Whether every read of ``name`` in ``func`` is the whole value of a
    routing keyword of a send."""
    parents = _parents(func)
    for node in ast.walk(func):
        if not (
            isinstance(node, ast.Name)
            and node.id == name
            and isinstance(node.ctx, ast.Load)
        ):
            continue
        keyword = parents.get(node)
        call = parents.get(keyword)
        if not (
            isinstance(keyword, ast.keyword)
            and keyword.arg in _ROUTING_KWARGS
            and isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr in _SEND_METHODS
        ):
            return False
    return True


def _routing_problem(
    value, chain: list, module, *, top=True, trusted=frozenset(), seen=frozenset()
) -> str | None:
    """``None`` when ``value`` is configuration, else what it is. ``top``: the
    whole value of the keyword (where a routing-only parameter is accepted);
    ``trusted``: a helper's parameters, checked at its call site."""
    nested = functools.partial(
        _routing_problem,
        chain=chain,
        module=module,
        top=False,
        trusted=trusted,
        seen=seen,
    )
    if isinstance(value, ast.Constant) or _settings_object(value, chain):
        return None
    if isinstance(value, ast.UnaryOp):
        return nested(value.operand)
    if isinstance(value, ast.JoinedStr):
        if any(isinstance(v, ast.FormattedValue) for v in value.values):
            return f"an f-string ({ast.unparse(value)[:50]})"
        return None
    if isinstance(value, (ast.List, ast.Tuple)):
        return next(filter(None, map(nested, value.elts)), None)
    if isinstance(value, ast.Dict):
        if not all(isinstance(k, ast.Constant) for k in value.keys):
            return f"a dict with a computed key ({ast.unparse(value)[:50]})"
        return next(filter(None, map(nested, value.values)), None)
    if isinstance(value, ast.IfExp):
        arms = (value.body, value.orelse)
        return next(
            filter(
                None,
                (
                    _routing_problem(
                        arm, chain, module, top=top, trusted=trusted, seen=seen
                    )
                    for arm in arms
                ),
            ),
            None,
        )
    if isinstance(value, ast.Name):
        return _routing_name_problem(value.id, chain, module, top, trusted, seen)
    if isinstance(value, ast.Attribute):
        base = value
        while isinstance(base, ast.Attribute):
            base = base.value
        if (
            isinstance(base, ast.Name)
            and base.id not in trusted
            and _resolve(base.id, chain).func is None
        ):
            # An attribute of a module-level name: an enum member, a constant.
            return _module_name_problem(base.id, module, seen)
        return f"an attribute of a local or parameter ({ast.unparse(value)[:50]})"
    if (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Attribute)
        and _settings_object(value.func.value, chain)
    ):
        args = [*value.args, *(k.value for k in value.keywords)]
        return next(filter(None, map(nested, args)), None)
    return f"computed ({ast.unparse(value)[:50]})"


def _routing_name_problem(name, chain, module, top, trusted, seen) -> str | None:
    if name in trusted:
        return None
    binding = _resolve(name, chain)
    if binding.func is None:
        return _module_name_problem(name, module, seen)
    key = (name, id(binding.func))
    if key in seen:
        return None
    seen = seen | {key}
    if binding.is_param:
        if not top:
            return f"parameter {name!r} built into a larger value"
        if not _used_only_for_routing(binding.func, name):
            return f"parameter {name!r}, also used as something else"
    for node in binding.nodes:
        value = _bound_value(node, name)
        if value is None:
            return f"{name!r}, bound at line {node.lineno} by a {type(node).__name__}"
        why = _routing_problem(
            value, binding.chain, module, top=top, trusted=trusted, seen=seen
        )
        if why:
            return f"{name!r}, bound at line {node.lineno} to {why}"
    for node, how in _changes(binding.func, name):
        # Storing it under a routing key of a dict is how a routing-only splat
        # is built; that dict's own changes are checked where it is splatted.
        if not _routing_key_assignment(node, binding.chain, module, trusted, seen):
            return f"{name!r}, which {how} at line {node.lineno}"
    return None


def _module_name_problem(name, module, seen) -> str | None:
    """A module-level (or imported, or builtin) name is configuration when the
    module binds it only to configuration, no function rebinds it
    (``global``), and — when it is a mutable display — nothing changes it."""
    if any(isinstance(n, ast.Global) and name in n.names for n in ast.walk(module)):
        return f"{name!r}, a module global a function rebinds"
    key = (name, id(module))
    if key in seen:
        return None
    seen = seen | {key}
    nodes = _binding_nodes(module, name)
    for node in nodes:
        value = _bound_value(node, name)
        if value is None:
            return f"{name!r}, bound at line {node.lineno} by a {type(node).__name__}"
        why = _routing_problem(value, [], module, top=False, seen=seen)
        if why:
            return f"{name!r}, bound at line {node.lineno} to {why}"
    mutable = (ast.Dict, ast.List, ast.Set)
    if any(isinstance(_bound_value(node, name), mutable) for node in nodes):
        for node, how in _changes(module, name):
            return f"{name!r}, which {how} at line {node.lineno}"
    return None


def _module_function(module, name: str):
    """The module-level function ``name`` refers to (the last definition)."""
    return next(
        (
            node
            for node in reversed(module.body)
            if isinstance(node, ast.FunctionDef) and node.name == name
        ),
        None,
    )


def _splat_problem(
    value, chain: list, module, *, trusted=frozenset(), seen=frozenset()
) -> str | None:
    """``None`` when a ``**`` splat provably carries routing parameters only,
    with configuration values; else what it is. Its name proves nothing."""
    splat = functools.partial(
        _splat_problem, chain=chain, module=module, trusted=trusted, seen=seen
    )
    if isinstance(value, ast.Dict):
        for key, item in zip(value.keys, value.values):
            why = _routing_item_problem(key, item, chain, module, trusted, splat)
            if why:
                return why
        return None
    if isinstance(value, ast.IfExp):
        return splat(value.body) or splat(value.orelse)
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
        if value.func.id == "dict" and _resolve("dict", chain).func is None:
            for arg in value.args:
                if why := splat(arg):
                    return why
            for k in value.keywords:
                key = None if k.arg is None else ast.Constant(k.arg)
                why = _routing_item_problem(key, k.value, chain, module, trusted, splat)
                if why:
                    return why
            return None
        helper = _module_function(module, value.func.id)
        if helper is not None and _resolve(value.func.id, chain).func is None:
            return _helper_problem(value, helper, chain, module, trusted, seen)
        return f"a call to {value.func.id}() the guard cannot follow"
    if isinstance(value, ast.Name):
        return _splat_name_problem(value.id, chain, module, trusted, seen)
    return f"{ast.unparse(value)[:50]}"


def _routing_item_problem(key, item, chain, module, trusted, splat) -> str | None:
    if key is None:
        return splat(item)
    if not (isinstance(key, ast.Constant) and key.value in _ROUTING_KWARGS):
        return f"key {ast.unparse(key)} is not a routing parameter"
    why = _routing_problem(item, chain, module, top=False, trusted=trusted)
    return f"{key.value!r} is {why}" if why else None


def _helper_problem(call, helper, chain, module, trusted, seen) -> str | None:
    if helper in seen:
        return f"{helper.name}() is recursive"
    for arg in [*call.args, *(k.value for k in call.keywords)]:
        why = _routing_problem(arg, chain, module, top=False, trusted=trusted)
        if why:
            return f"{helper.name}() is handed {why}"
    returns = [n for n in _own_nodes(helper) if isinstance(n, ast.Return)]
    if not returns or any(r.value is None for r in returns):
        return f"{helper.name}() can return what the guard cannot follow"
    params = frozenset(_parameters(helper))
    for ret in returns:
        why = _splat_problem(
            ret.value, [helper], module, trusted=params, seen=seen | {helper}
        )
        if why:
            return f"{helper.name}() returns {why}"
    return None


def _splat_name_problem(name, chain, module, trusted, seen) -> str | None:
    binding = _resolve(name, chain)
    if name in trusted or binding.is_param:
        return f"{name!r} is a parameter: the guard cannot see what the caller puts in"
    if binding.func is None:
        return f"{name!r} is not a local the guard can follow"
    key = (name, id(binding.func))
    if key in seen:
        return None
    seen = seen | {key}
    for node in binding.nodes:
        value = _bound_value(node, name)
        if value is None:
            return f"{name!r} is bound at line {node.lineno} by a {type(node).__name__}"
        why = _splat_problem(value, binding.chain, module, trusted=trusted, seen=seen)
        if why:
            return f"{name!r} is bound at line {node.lineno} to {why}"
    for node, how in _changes(binding.func, name):
        if not _routing_key_assignment(node, binding.chain, module, trusted, seen):
            return f"{name!r} {how} at line {node.lineno}"
    return None


def _routing_key_assignment(node, chain, module, trusted, seen) -> bool:
    """``kwargs["<routing key>"] = <configuration>``: the one change a
    routing-only dict may take."""
    if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
        return False
    target = node.targets[0]
    return (
        isinstance(target, ast.Subscript)
        and isinstance(target.value, ast.Name)
        and isinstance(target.slice, ast.Constant)
        and target.slice.value in _ROUTING_KWARGS
        and _routing_problem(
            node.value, chain, module, top=False, trusted=trusted, seen=seen
        )
        is None
    )


# --------------------------------------------------------------------------
# The redaction binding and its context (#1971 rule 4)
# --------------------------------------------------------------------------


def _none_problem(ctx, chain: list, module, seen=frozenset()) -> str | None:
    """How the context ``ctx`` can be ``None``; ``None`` when no binding the
    guard can see makes it so."""
    if ctx is None:
        return "no context argument"
    if isinstance(ctx, ast.Constant):
        return "a literal None" if ctx.value is None else None
    if isinstance(ctx, ast.IfExp):
        return _none_problem(ctx.body, chain, module, seen) or _none_problem(
            ctx.orelse, chain, module, seen
        )
    if isinstance(ctx, ast.BoolOp):
        return next(
            filter(None, (_none_problem(v, chain, module, seen) for v in ctx.values)),
            None,
        )
    if isinstance(ctx, ast.NamedExpr):
        return _none_problem(ctx.value, chain, module, seen)
    if not isinstance(ctx, ast.Name):
        return None
    binding = _resolve(ctx.id, chain)
    if binding.is_param and _defaults_to_none(binding.func, ctx.id):
        return f"{ctx.id!r}, a parameter that defaults to None"
    scope = module if binding.func is None else binding.func
    key = (ctx.id, id(scope))
    if key in seen:
        return None
    nodes = (
        binding.nodes if binding.func is not None else _binding_nodes(module, ctx.id)
    )
    for node in nodes:
        value = _bound_value(node, ctx.id)
        why = (
            None
            if value is None
            else _none_problem(value, binding.chain, module, seen | {key})
        )
        if why:
            return f"{ctx.id!r}, bound at line {node.lineno} to {why}"
    return None


def _redaction_problem(node, name: str, chain: list, module):
    """``None`` when the binding is an awaited redaction with a context that
    cannot be ``None``, else ``(kind, detail)``."""
    value = _bound_value(node, name)
    call = value.value if isinstance(value, ast.Await) else None
    if (
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and (call.func.id in _REDACTING_HELPERS)
    ):
        ctx = call.args[1] if len(call.args) > 1 else None
        ctx = next((k.value for k in call.keywords if k.arg == "ctx"), ctx)
    elif (
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and (call.func.attr in _REDACTING_METHODS)
    ):
        ctx = call.func.value
    else:
        return UNREDACTED, f"{name!r} is bound at line {node.lineno} by something else"
    why = _none_problem(ctx, chain, module)
    if why:
        return NONE_CONTEXT, (
            f"{name!r} is redacted at line {node.lineno} with a context that can "
            f"be None: {why}"
        )
    return None


# --------------------------------------------------------------------------
# The classifier
# --------------------------------------------------------------------------


def _content_problems(call, name: str, parents) -> list:
    """Why the content name ``name`` may reach ``call`` unredacted."""
    # The reference point: the call itself, or — when the name is bound in an
    # enclosing function — the nested def the call sits in (a closure can run
    # whenever, so a rebinding after its definition reaches it too).
    point, scopes = call, _scopes(call, parents)
    module = _module(call, parents)
    for i, func in enumerate(scopes):
        bindings = _binding_nodes(func, name)
        if not bindings:
            if name in _parameters(func):
                detail = (
                    f"{name!r} is a parameter of {func.name}; redact it at the site"
                )
                return [(PARAMETER, detail)]
            point = func
            continue
        loops = _loops_around(point, func, parents)
        before = [b for b in bindings if b.lineno < point.lineno]
        reaching = [
            b
            for b in before
            if not any(_kills(b2, b, parents) for b2 in before if b2 is not b)
        ]
        after = [b for b in bindings if b.lineno > point.lineno]
        if point is not call:
            reaching += after  # rebinding after the closure is defined
        else:
            reaching += [
                b for b in after if set(_loops_around(b, func, parents)) & set(loops)
            ]
        problems = []
        if not reaching:
            problems.append((UNREDACTED, f"{name!r} is not bound before the send"))
        elif not _definitely_bound(func.body, name, point.lineno):
            problems.append(
                (UNREDACTED, f"{name!r} may reach the send unbound on some path")
            )
        for binding in reaching:
            problem = _redaction_problem(binding, name, scopes[i:], module)
            if problem:
                problems.append(problem)
        problems += [
            (CHANGED, f"{name!r} {how} at line {node.lineno}")
            for node, how in _changes(func, name)
        ]
        return problems
    return [(UNREDACTED, f"{name!r} is not bound in an enclosing function")]


def _call_problems(call, parents) -> list:
    chain = _scopes(call, parents)
    module = _module(call, parents)
    problems = []
    if call.args:
        problems.append((POSITIONAL, "passes a positional argument"))
    for k in call.keywords:
        if k.arg is None:
            why = _splat_problem(k.value, chain, module)
            if why:
                splat = ast.unparse(k.value)[:40]
                problems.append(
                    (SPLAT, f"splats **{splat}, which can carry content: {why}")
                )
        elif k.arg in _ROUTING_KWARGS:
            why = _routing_problem(k.value, chain, module)
            if why:
                problems.append(
                    (ROUTING_VALUE, f"{k.arg}= is not configuration: {why}")
                )
        elif k.arg not in _CONTENT_KWARGS:
            problems.append(
                (OTHER_CHANNEL, f"passes {k.arg}=, a channel the guard does not follow")
            )
    sent = {k.arg: k.value for k in call.keywords if k.arg in _CONTENT_KWARGS}
    if not sent:
        problems.append(
            (NO_CONTENT, "no messages=/prompt= argument the guard can follow")
        )
    elif len(sent) > 1:
        problems.append((TWO_CONTENTS, "sends content in both messages= and prompt="))
    else:
        [value] = sent.values()
        if isinstance(value, ast.Name):
            problems += _content_problems(call, value.id, parents)
        else:
            detail = (
                f"sends an expression ({ast.unparse(value)[:60]}), not a redacted name"
            )
            problems.append((EXPRESSION, detail))
    return problems


def _is_getattr(node) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) > 1
    )


def _is_computed_getattr(node) -> bool:
    return _is_getattr(node) and not isinstance(node.args[1], ast.Constant)


def classify(node, parents) -> tuple:
    """Every reason the site is not redacted at the send, as ``(kind,
    detail)``; empty when it is (R)."""
    if isinstance(node, ast.Attribute):
        site = ast.unparse(parents[node])[:60]
        return ((UNCALLED, f"an uncalled reference to .{node.attr} ({site})"),)
    if _is_getattr(node):
        return ((GETATTR, f"reaches .{node.args[1].value} through getattr"),)
    if _is_computed_getattr(node.func) or isinstance(node.func, ast.Name):
        detail = (
            f"calls {ast.unparse(node.func)[:50]}, a method found by a computed name"
        )
        return ((GETATTR_COMPUTED, detail),)
    return tuple(_call_problems(node, parents))


def _calls_computed_getattr(call, parents) -> bool:
    """``getattr(x, name)(...)``, or ``f = getattr(x, name)`` then ``f(...)``
    called with anything that could be content."""
    if _is_computed_getattr(call.func):
        return True
    if not isinstance(call.func, ast.Name) or not (call.args or call.keywords):
        return False
    binding = _resolve(call.func.id, _scopes(call, parents))
    return any(
        _is_computed_getattr(_bound_value(node, call.func.id)) for node in binding.nodes
    )


def _send_sites(tree):
    parents = _parents(tree)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _SEND_METHODS
        ):
            yield node
        elif (
            isinstance(node, ast.Attribute)
            and node.attr in _SEND_METHODS
            and isinstance(node.ctx, ast.Load)
            and not (
                isinstance(parents.get(node), ast.Call) and parents[node].func is node
            )
        ):
            yield node
        elif (
            _is_getattr(node)
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in _SEND_METHODS
        ):
            yield node
        elif isinstance(node, ast.Call) and _calls_computed_getattr(node, parents):
            yield node


@functools.lru_cache(maxsize=1)
def _scan() -> tuple:
    """Every send site: (path, innermost function, line, problems)."""
    return tuple(_walk_package())


def _walk_package():
    for path in sorted(_ROOT.rglob("*.py")):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", (DeprecationWarning, SyntaxWarning))
            tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = _parents(tree)
        rel = path.relative_to(_ROOT.parent).as_posix()
        for call in _send_sites(tree):
            scopes = _scopes(call, parents)
            where = scopes[0].name if scopes else "<module>"
            yield rel, where, call.lineno, classify(call, parents)


def _kinds(problems) -> frozenset:
    return frozenset(kind for kind, _ in problems)


def _unadmitted(rows) -> list:
    """The refused sites no allowlist entry admits: not listed, or refused for
    kinds other than the entry's."""
    out = []
    for rel, where, line, problems in rows:
        if not problems:
            continue
        entry = ALLOWLIST.get((rel, where))
        if entry is not None and _kinds(problems) == entry.kinds:
            continue
        admitted = sorted(entry.kinds) if entry else "nothing"
        details = "; ".join(detail for _, detail in problems)
        out.append(
            f"{rel}:{line} in {where}: {details} "
            f"[refused for {sorted(_kinds(problems))}; allowlisted for {admitted}]"
        )
    return out


def test_every_send_site_is_redacted_or_allowlisted():
    unknown = _unadmitted(_scan())
    assert unknown == [], (
        "an LLM send site must send what the case redaction returned "
        "(`messages = await case_redacted_messages(..., redaction_ctx)`, then "
        "`route(messages=messages)`), or be allowlisted here with its reason "
        "and the kinds it is refused for:\n" + "\n".join(unknown)
    )


def test_the_redacted_sites_are_the_ones_expected():
    found = {(rel, where) for rel, where, _, problems in _scan() if not problems}
    assert found == REDACTED_SITES


def test_every_allowlist_entry_still_names_an_unredacted_site():
    """A stale entry, or a stale kind, would silently admit a new site that
    reuses its function name or its kind."""
    observed: dict = {}
    for rel, where, _, problems in _scan():
        if problems:
            observed.setdefault((rel, where), set()).add(_kinds(problems))
    stale = {
        key: sorted(entry.kinds)
        for key, entry in ALLOWLIST.items()
        if entry.kinds not in observed.get(key, set())
    }
    assert stale == {}


# --------------------------------------------------------------------------
# The classifier, on synthetic sources (positive and negative controls).
# --------------------------------------------------------------------------


def _verdicts(source: str) -> list:
    tree = ast.parse(textwrap.dedent(source))
    parents = _parents(tree)
    return [classify(call, parents) for call in _send_sites(tree)]


@pytest.mark.parametrize(
    "source",
    [
        # the direct form
        """
        async def f(router, prompt, ctx):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            return await router.route(messages=messages)
        """,
        # bound in the enclosing function, sent from a retry closure
        """
        async def f(router, prompt, ctx):
            messages = await ctx.asanitize_messages([{"content": prompt}])

            async def _send(cap):
                return await router.route(messages=messages, max_tokens=cap)

            return await retry(_send)
        """,
        # a prompt rebound through the helper before the send
        """
        async def f(provider, ctx):
            prompt = f"Title: {raw}"
            prompt = await case_redacted(prompt, ctx)
            return await provider.generate(prompt=prompt)
        """,
        # both arms of a branch redact
        """
        async def f(router, prompt, ctx, cond):
            if cond:
                messages = await case_redacted_messages([{"content": prompt}], ctx)
            else:
                messages = await ctx.asanitize_messages([{"content": "x"}])
            return await router.route(messages=messages)
        """,
        # routing overrides splatted from a helper in the same module, every
        # return of which is a routing-only dict (its name proves nothing)
        """
        def _role_route_kwargs(settings, role):
            override = settings.llm.explicit_role_provider(role)
            return {"provider_override": override} if override else {}

        async def f(router, prompt, ctx, settings):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            return await router.route(
                messages=messages, model="m", **_role_route_kwargs(settings, "chat")
            )
        """,
        # #1971: the routing values the real sites pass — settings reads, a
        # routing-only parameter, module constants, an enum member, a constant
        # response_format, a routing dict built by item assignment
        """
        async def f(router, prompt, ctx, cap):
            from faultmaven.config.settings import get_settings

            settings = get_settings()
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            model = settings.llm.get_classifier_model()
            route_kwargs = {}
            override = settings.llm.explicit_role_provider("classifier")
            if override:
                route_kwargs["provider_override"] = override
            return await router.route(
                messages=messages,
                model=model,
                max_tokens=cap,
                min_output_tokens=MIN_OUTPUT_TOKENS,
                temperature=0.0,
                reasoning_intent=ReasoningIntent.EXTRACTION,
                response_format={"type": "json_object"},
                **route_kwargs,
            )
        """,
        """
        _CAP = 512

        async def f(self, prompt, ctx):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            return await self._router.route(
                messages=messages,
                model=self._settings.llm.get_synthesis_model(),
                max_tokens=_CAP,
                temperature=-0.0,
                **(
                    {"provider_override": override}
                    if (override := self._settings.llm.explicit_role_provider("x"))
                    else {}
                ),
            )
        """,
        # #1971: a shallow copy is a new list — appending to it, or replacing
        # one of its slots, does not reach the sent one
        """
        async def f(router, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            transcript = list(messages)
            transcript.append({"content": raw})
            return await router.route(messages=messages)
        """,
        """
        async def f(router, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            snapshot = messages[:]
            snapshot.append({"content": raw})
            snapshot[0] = {"content": raw}
            return await router.route(messages=messages)
        """,
        # #1971: a deep copy is independent, whatever is done to it
        """
        async def f(router, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            shadow = copy.deepcopy(messages)
            shadow[0]["content"] = raw
            return await router.route(messages=messages)
        """,
        # #1971: a non-mutating method through an element, and a reader
        """
        async def f(router, prompt, ctx):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            if len(messages[-1]["content"].replace(" ", "")):
                return await router.route(messages=messages)
        """,
    ],
)
def test_the_classifier_accepts_a_redacted_send(source):
    assert _verdicts(source) == [()]


@pytest.mark.parametrize(
    "source",
    [
        # a literal list: the pre-#1956 shape of every side call
        """
        async def f(router, prompt):
            return await router.route(messages=[{"content": prompt}])
        """,
        # bound, but not by the redaction
        """
        async def f(router, prompt):
            messages = build(prompt)
            return await router.route(messages=messages)
        """,
        # redacted, then overwritten before the send
        """
        async def f(router, prompt, ctx):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            messages = [{"content": prompt}]
            return await router.route(messages=messages)
        """,
        # handed in by the caller: the guard cannot see where it came from
        """
        async def f(router, messages):
            return await router.route(messages=messages)
        """,
        # a splat
        """
        async def f(provider, kwargs):
            return await provider.generate(**kwargs)
        """,
        # positional
        """
        async def f(provider, prompt):
            return await provider.generate(prompt)
        """,
        # a redaction that is not awaited is a coroutine, not text
        """
        async def f(router, prompt, ctx):
            messages = case_redacted_messages([{"content": prompt}], ctx)
            return await router.route(messages=messages)
        """,
        # an alias: the send happens through a name the scan cannot follow
        """
        async def f(router, prompt):
            send = router.route
            return await send(messages=[{"content": prompt}])
        """,
        # functools.partial over the method
        """
        async def f(router, prompt):
            send = functools.partial(router.route, messages=[{"content": prompt}])
            return await send()
        """,
        # getattr
        """
        async def f(router, prompt):
            return await getattr(router, "route")(messages=[{"content": prompt}])
        """,
        # the helper, with a literal None context, redacts nothing
        """
        async def f(router, prompt):
            messages = await case_redacted_messages([{"content": prompt}], None)
            return await router.route(messages=messages)
        """,
        """
        async def f(provider, prompt):
            prompt = await case_redacted(prompt, None)
            return await provider.generate(prompt=prompt)
        """,
        # mutated after the redaction
        """
        async def f(router, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            messages.append({"role": "user", "content": raw})
            return await router.route(messages=messages)
        """,
        """
        async def f(router, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            messages.extend([{"role": "user", "content": raw}])
            return await router.route(messages=messages)
        """,
        """
        async def f(router, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            messages.insert(0, {"role": "system", "content": raw})
            return await router.route(messages=messages)
        """,
        """
        async def f(router, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            messages += [{"role": "user", "content": raw}]
            return await router.route(messages=messages)
        """,
        """
        async def f(router, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            messages[0]["content"] = raw
            return await router.route(messages=messages)
        """,
        # raw content through a second channel
        """
        async def f(router, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            return await router.route(messages=messages, system=raw)
        """,
        """
        async def f(router, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            return await router.route(messages=messages, prompt=raw)
        """,
        # a splat that is not a routing override
        """
        async def f(router, prompt, ctx, extra):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            return await router.route(messages=messages, **extra)
        """,
        # one arm of a branch redacts, the other does not
        """
        async def f(router, prompt, ctx, cond):
            if cond:
                messages = [{"content": prompt}]
            else:
                messages = await case_redacted_messages([{"content": prompt}], ctx)
            return await router.route(messages=messages)
        """,
        # redacted only on one arm, the caller's value on the other
        """
        async def f(router, messages, ctx, cond):
            if cond:
                messages = await case_redacted_messages(messages, ctx)
            return await router.route(messages=messages)
        """,
        # rebound later in the loop the send sits in
        """
        async def f(router, prompts, ctx):
            messages = await case_redacted_messages([{"content": "x"}], ctx)
            for p in prompts:
                await router.route(messages=messages)
                messages = [{"content": p}]
        """,
        # rebound after the closure that sends it is defined
        """
        async def f(router, prompt, ctx):
            messages = await case_redacted_messages([{"content": prompt}], ctx)

            async def _send():
                return await router.route(messages=messages)

            messages = [{"content": prompt}]
            return await _send()
        """,
        # rebound by a nested function that declares it nonlocal
        """
        async def f(router, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)

            def _reset():
                nonlocal messages
                messages = [{"content": raw}]

            _reset()
            return await router.route(messages=messages)
        """,
    ],
)
def test_the_classifier_refuses_an_unredacted_send(source):
    [problems] = _verdicts(source)
    assert problems != ()


#: #1971: shapes the name-based guard classified R. Each is refused for the
#: kind its rule names; the PR records the verdict the previous guard gave.
_UNPROVEN = {
    # rule 1: a name that holds the sent value, or a part of it, changes it
    "1-alias-appended": (
        CHANGED,
        """
        async def f(router, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            alias = messages
            alias.append({"role": "user", "content": raw})
            return await router.route(messages=messages)
        """,
    ),
    "1-alias-of-an-alias": (
        CHANGED,
        """
        async def f(router, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            alias = messages
            other = alias
            other[0]["content"] = raw
            return await router.route(messages=messages)
        """,
    ),
    "1-element-bound-to-a-name": (
        CHANGED,
        """
        async def f(router, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            first = messages[0]
            first["content"] = raw
            return await router.route(messages=messages)
        """,
    ),
    "1-loop-over-the-elements": (
        CHANGED,
        """
        async def f(router, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            for message in messages:
                message["content"] = raw
            return await router.route(messages=messages)
        """,
    ),
    "1-shallow-copy-shares-its-elements": (
        CHANGED,
        """
        async def f(router, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            snapshot = messages[:]
            snapshot[0]["content"] = raw
            return await router.route(messages=messages)
        """,
    ),
    "1-held-in-a-container": (
        CHANGED,
        """
        async def f(router, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            holder = [messages]
            holder[0].append({"content": raw})
            return await router.route(messages=messages)
        """,
    ),
    "1-handed-to-a-call": (
        CHANGED,
        """
        async def f(router, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            _add_context(messages, raw)
            return await router.route(messages=messages)
        """,
    ),
    "1-stored-on-an-object": (
        CHANGED,
        """
        async def f(self, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            self.pending = messages
            self.pending.append({"content": raw})
            return await self.router.route(messages=messages)
        """,
    ),
    "1-changed-in-a-lambda": (
        CHANGED,
        """
        async def f(router, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            add = lambda: messages.append({"content": raw})
            add()
            return await router.route(messages=messages)
        """,
    ),
    "1-changed-in-a-nested-function": (
        CHANGED,
        """
        async def f(router, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            if raw:

                def _add():
                    messages.append({"content": raw})

                _add()
            return await router.route(messages=messages)
        """,
    ),
    # rule 2: a mutating method called through an element
    "2-element-update": (
        CHANGED,
        """
        async def f(router, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            messages[0].update({"content": raw})
            return await router.route(messages=messages)
        """,
    ),
    "2-element-setdefault": (
        CHANGED,
        """
        async def f(router, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            messages[-1].setdefault("name", raw)
            return await router.route(messages=messages)
        """,
    ),
    "2-content-blocks-appended": (
        CHANGED,
        """
        async def f(router, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            messages[0]["content"].append({"type": "text", "text": raw})
            return await router.route(messages=messages)
        """,
    ),
    # rule 3: a splat is traced to what it holds, not trusted by its name
    "3-named-dict-with-content": (
        SPLAT,
        """
        async def f(router, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            route_kwargs = {"system": raw}
            return await router.route(messages=messages, **route_kwargs)
        """,
    ),
    "3-named-dict-given-a-content-key": (
        SPLAT,
        """
        async def f(router, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            route_kwargs = {}
            route_kwargs["system"] = raw
            return await router.route(messages=messages, **route_kwargs)
        """,
    ),
    "3-named-dict-updated": (
        SPLAT,
        """
        async def f(router, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            route_kwargs = {"model": "m"}
            route_kwargs.update(system=raw)
            return await router.route(messages=messages, **route_kwargs)
        """,
    ),
    "3-named-parameter": (
        SPLAT,
        """
        async def f(router, prompt, ctx, route_kwargs):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            return await router.route(messages=messages, **route_kwargs)
        """,
    ),
    "3-named-call-not-defined-here": (
        SPLAT,
        """
        async def f(router, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            return await router.route(messages=messages, **_route_kwargs_for(raw))
        """,
    ),
    "3-named-helper-returning-content": (
        SPLAT,
        """
        def _role_route_kwargs(raw):
            return {"system": raw}

        async def f(router, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            return await router.route(messages=messages, **_role_route_kwargs(raw))
        """,
    ),
    # rule 4: a context bound to None through a variable
    "4-variable-bound-to-none": (
        NONE_CONTEXT,
        """
        async def f(router, prompt):
            ctx = None
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            return await router.route(messages=messages)
        """,
    ),
    "4-none-on-one-arm": (
        NONE_CONTEXT,
        """
        async def f(router, prompt, turn_ctx, cond):
            ctx = turn_ctx if cond else None
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            return await router.route(messages=messages)
        """,
    ),
    "4-none-through-another-name": (
        NONE_CONTEXT,
        """
        async def f(provider, prompt):
            nothing = None
            ctx = nothing
            prompt = await case_redacted(prompt, ctx=ctx)
            return await provider.generate(prompt=prompt)
        """,
    ),
    "4-parameter-defaulting-to-none": (
        NONE_CONTEXT,
        """
        async def f(router, prompt, ctx=None):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            return await router.route(messages=messages)
        """,
    ),
    # rule 5: a routing keyword carries configuration, not content
    "5-model-from-an-f-string": (
        ROUTING_VALUE,
        """
        async def f(router, prompt, ctx):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            return await router.route(messages=messages, model=f"m-{prompt}")
        """,
    ),
    "5-response-format-carrying-content": (
        ROUTING_VALUE,
        """
        async def f(router, prompt, ctx):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            return await router.route(
                messages=messages,
                response_format={"type": "json_schema", "schema": {"title": prompt}},
            )
        """,
    ),
    "5-routing-name-built-by-a-call": (
        ROUTING_VALUE,
        """
        async def f(router, prompt, ctx):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            fmt = build_format(prompt)
            return await router.route(messages=messages, response_format=fmt)
        """,
    ),
    "5-parameter-that-is-also-content": (
        ROUTING_VALUE,
        """
        async def f(router, prompt, ctx):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            return await router.route(messages=messages, provider_override=prompt)
        """,
    ),
    "5-attribute-of-a-parameter": (
        ROUTING_VALUE,
        """
        async def f(router, case, ctx):
            messages = await case_redacted_messages([{"content": case.title}], ctx)
            return await router.route(messages=messages, model=case.description)
        """,
    ),
    "5-method-of-content": (
        ROUTING_VALUE,
        """
        async def f(router, prompt, ctx):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            return await router.route(messages=messages, model=prompt.strip())
        """,
    ),
    "5-routing-dict-changed-with-content": (
        ROUTING_VALUE,
        """
        async def f(router, prompt, ctx):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            fmt = {"type": "json_schema"}
            fmt["schema"] = {"title": prompt}
            return await router.route(messages=messages, response_format=fmt)
        """,
    ),
    "5-module-dict-changed-with-content": (
        ROUTING_VALUE,
        """
        _FORMAT = {"type": "json_schema"}

        async def f(router, prompt, ctx):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            _FORMAT["schema"] = {"title": prompt}
            return await router.route(messages=messages, response_format=_FORMAT)
        """,
    ),
    "5-module-global-rebound": (
        ROUTING_VALUE,
        """
        MODEL = "m"

        def remember(raw):
            global MODEL
            MODEL = raw

        async def f(router, prompt, ctx):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            return await router.route(messages=messages, model=MODEL)
        """,
    ),
    "5-routing-splat-carrying-content": (
        SPLAT,
        """
        async def f(router, prompt, ctx):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            return await router.route(messages=messages, **{"model": prompt.strip()})
        """,
    ),
    # rule 6: a method getattr finds by a computed name
    "6-getattr-computed-called": (
        GETATTR_COMPUTED,
        """
        async def f(router, prompt, name):
            return await getattr(router, name)(messages=[{"content": prompt}])
        """,
    ),
    "6-getattr-computed-bound": (
        GETATTR_COMPUTED,
        """
        async def f(router, prompt, name):
            send = getattr(router, name)
            return await send(messages=[{"content": prompt}])
        """,
    ),
    "6-getattr-computed-positional": (
        GETATTR_COMPUTED,
        """
        async def f(router, prompt, name):
            send = getattr(router, name)
            return await send(prompt)
        """,
    ),
}


@pytest.mark.parametrize("kind, source", list(_UNPROVEN.values()), ids=list(_UNPROVEN))
def test_the_classifier_refuses_what_it_cannot_prove_1971(kind, source):
    [problems] = _verdicts(source)
    assert kind in _kinds(problems), problems


_ADMITTED_FOR_ITS_KIND = [
    # rule 7: the entry admits the shape it was written for ...
    (
        ("faultmaven/api/routes/admin_config.py", "check_llm_connection"),
        """
        async def check_llm_connection(provider):
            return await provider.generate(prompt="Say hello", max_tokens=50)
        """,
        True,
    ),
    (
        (
            "faultmaven/core/investigation/milestone_engine/generation.py",
            "llm_operation",
        ),
        """
        async def llm_operation(provider, generate_params):
            return await provider.generate(**generate_params)
        """,
        True,
    ),
    # ... and not a different defect in the same function
    (
        ("faultmaven/api/routes/admin_config.py", "check_llm_connection"),
        """
        async def check_llm_connection(provider, raw):
            return await provider.generate(prompt="Say hello", system=raw)
        """,
        False,
    ),
    (
        (
            "faultmaven/core/investigation/milestone_engine/generation.py",
            "llm_operation",
        ),
        """
        async def llm_operation(provider, generate_params, raw):
            return await provider.generate(messages=raw, **generate_params)
        """,
        False,
    ),
]


@pytest.mark.parametrize(
    "entry, source, admitted",
    _ADMITTED_FOR_ITS_KIND,
    ids=["own-kind", "own-kinds", "other-channel", "content-parameter"],
)
def test_an_allowlist_entry_admits_only_its_kinds_1971(entry, source, admitted):
    rows = [(*entry, 1, problems) for problems in _verdicts(source)]
    assert (_unadmitted(rows) == []) is admitted
