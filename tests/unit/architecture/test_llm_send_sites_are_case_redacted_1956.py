"""#1956: every LLM send site in the package is classified, by AST.

A send site is any ``<x>.route(...)`` call, any ``send_to_model(provider,
...)`` call (the model dispatch, #1998: its one positional argument is the
provider) and any ``<x>._generate(...)`` call, and also any way of reaching
those without calling them at the site: an uncalled reference (``send =
router.route``, ``functools.partial(send_to_model, p)``) or ``getattr(x,
"route")``. Each one must be ONE of:

(R) redacted at the site. The call passes its content in exactly one of
    ``messages=`` / ``prompt=``, as a NAME. Every binding of that name that can
    reach the call — in the calling function or, for a closure, the enclosing
    one; both arms of a branch; a rebinding later in a loop around the call or
    after the closure is defined — is ``await case_redacted(..., ctx)``,
    ``await case_redacted_messages(..., ctx)`` or
    ``await <ctx>.asanitize_messages(...)``, with a context that is not a
    literal ``None``. Nothing mutates the name in between (``.append``,
    ``.extend``, ``.insert``, item or attribute assignment, ``+=``). Every
    other keyword is a routing parameter (``model``, ``max_tokens``, ...): no
    second channel (``system=``, ``prompt=`` beside ``messages=``) and no
    ``**`` splat except routing overrides. So the sites read
    ``messages = await case_redacted_messages(..., ctx)`` and then
    ``route(messages=messages)``: what is sent is what the redaction returned.
(A) allowlisted below, with the reason it needs no case pass of its own.

Anything else fails, with its location: a new send site has to decide. The
#1952 guard (``test_rebuilt_prompts_are_redacted_1949.py``) still pins the
engine's own prompt assembly; the two engine splat sites are allowlisted here
on its strength. The same idiom as ``test_openapi_documents_auth.py``: a
classifier that is told about every case, and refuses the ones it is not.
"""

from __future__ import annotations

import ast
import functools
import textwrap
import warnings
from pathlib import Path

import pytest

import faultmaven

pytestmark = pytest.mark.unit

_ROOT = Path(faultmaven.__file__).parent
_SEND_METHODS = {"route", "_generate"}
#: The model dispatch, a module function: ``send_to_model(provider, **call)``.
_DISPATCH = "send_to_model"
_REDACTING_HELPERS = {"case_redacted", "case_redacted_messages"}
_REDACTING_METHODS = {"asanitize_messages"}

#: (path relative to the repo, innermost enclosing function) -> why the site
#: needs no case pass of its own.
ALLOWLIST: dict[tuple[str, str], str] = {
    (
        "faultmaven/infrastructure/llm/providers/registry.py",
        "send_connection_check",
    ): ("a constant connection-test prompt; no case content"),
    ("faultmaven/infrastructure/llm/dispatch.py", "send_to_model"): (
        "the dispatch's own provider._generate call; every caller of "
        "send_to_model is classified here"
    ),
    ("faultmaven/infrastructure/llm/providers/registry.py", "route_request"): (
        "registry internals: the provider dispatch every routed call ends in"
    ),
    (
        "faultmaven/modules/knowledge/domain/services/conversion_service/service.py",
        "_convert",
    ): (
        "sends prepared.redacted_messages, redacted once in _prepare_conversion "
        "(pinned by test_conversion_model_boundary_redaction_1901.py)"
    ),
    (
        "faultmaven/core/investigation/milestone_engine/generation.py",
        "_tool_loop_call",
    ): (
        "the engine's tool-loop splat; every message it carries is built through "
        "case_redacted (pinned by test_rebuilt_prompts_are_redacted_1949.py)"
    ),
    (
        "faultmaven/core/investigation/milestone_engine/generation.py",
        "llm_operation",
    ): (
        "the engine's single-shot splat; its prompt is case_redacted at the top "
        "of _generate_structured_output_inner (pinned by #1952's guard)"
    ),
    ("faultmaven/api/route_enumeration.py", "iter_served_routes"): (
        "a Starlette routing context's `route` attribute, not an LLM"
    ),
    ("faultmaven/api/route_enumeration.py", "iter_documented_routes"): (
        "a Starlette routing context's `route` attribute, not an LLM"
    ),
    ("faultmaven/api/route_enumeration.py", "iter_served_endpoints"): (
        "a Starlette routing context's `route` attribute, not an LLM"
    ),
    ("faultmaven/api/route_enumeration.py", "iter_unresolved_routes"): (
        "a Starlette routing context's `route` attribute, not an LLM"
    ),
    ("faultmaven/api/middleware/route_policy.py", "_flattened_post_paths"): (
        "a Starlette routing context's `route` attribute, not an LLM"
    ),
}

#: The sites that redact at the send, which the scan must find (positive
#: control: a classifier that recognised nothing would pass vacuously).
REDACTED_SITES = {
    ("faultmaven/core/investigation/intent_resolver.py", "_classify"),
    ("faultmaven/core/preprocessing/tier2/local_service.py", "_analyze"),
    ("faultmaven/modules/agent/domain/services/out_of_band.py", "_classify"),
    (
        "faultmaven/modules/agent/domain/services/out_of_band.py",
        "_one_answer_attempt",
    ),
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


def _own_nodes(func):
    """The function's body, not descending into nested functions or classes."""
    stack = list(func.body)
    while stack:
        node = stack.pop()
        yield node
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, (*_FUNCS, ast.ClassDef, ast.Lambda)):
                stack.append(child)


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
_MUTATORS = {"append", "extend", "insert", "update", "setdefault", "__setitem__"}


def _stmt(node, parents):
    while not isinstance(node, ast.stmt):
        node = parents[node]
    return node


def _binding_nodes(func, name: str) -> list:
    out = []
    for node in _own_nodes(func):
        targets: list = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
            targets = [node.target]
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            targets = [node.target]
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            targets = [node.optional_vars]
        if any(
            isinstance(n, ast.Name) and n.id == name
            for t in targets
            for n in ast.walk(t)
        ):
            out.append(node)
    return out


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


def _redaction_problem(binding) -> str | None:
    """``None`` when the binding is an awaited redaction with a real context."""
    if not isinstance(binding, (ast.Assign, ast.AnnAssign)):
        return f"bound at line {binding.lineno} by something else"
    value = binding.value
    if not isinstance(value, ast.Await) or not isinstance(value.value, ast.Call):
        return f"bound at line {binding.lineno} by something else"
    call = value.value
    if isinstance(call.func, ast.Name) and call.func.id in _REDACTING_HELPERS:
        ctx = call.args[1] if len(call.args) > 1 else None
        ctx = next((k.value for k in call.keywords if k.arg == "ctx"), ctx)
    elif isinstance(call.func, ast.Attribute) and call.func.attr in _REDACTING_METHODS:
        ctx = call.func.value
    else:
        return f"bound at line {binding.lineno} by something else"
    if ctx is None or (isinstance(ctx, ast.Constant) and ctx.value is None):
        return f"redacted at line {binding.lineno} with no context (None)"
    return None


def _mutations(func, name: str) -> list:
    out = []
    for node in _own_nodes(func):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _MUTATORS
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == name
        ):
            out.append(node)
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        for t in targets:
            if isinstance(t, (ast.Subscript, ast.Attribute)) and any(
                isinstance(n, ast.Name) and n.id == name for n in ast.walk(t)
            ):
                out.append(node)
    return out


def _routing_splat(value) -> bool:
    """A ``**`` splat that can only carry routing overrides: a name or call
    following the ``*route_kwargs`` convention, or dict literals keyed only by
    routing parameters."""
    if isinstance(value, ast.Name):
        return "route_kwargs" in value.id
    if isinstance(value, ast.Call):
        func = value.func
        ident = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        return "route_kwargs" in ident
    if isinstance(value, ast.IfExp):
        return _routing_splat(value.body) and _routing_splat(value.orelse)
    if isinstance(value, ast.Dict):
        return all(
            isinstance(k, ast.Constant) and k.value in _ROUTING_KWARGS
            for k in value.keys
        )
    return False


def _parameters(func) -> set[str]:
    a = func.args
    names = [x.arg for x in a.posonlyargs + a.args + a.kwonlyargs]
    names += [x.arg for x in (a.vararg, a.kwarg) if x is not None]
    return set(names)


def classify(node, parents) -> str | None:
    """``None`` when the site is redacted at the send (R), else why not."""
    if isinstance(node, (ast.Attribute, ast.Name)):
        name = node.attr if isinstance(node, ast.Attribute) else node.id
        return f"an uncalled reference to {name} ({ast.unparse(parents[node])[:60]})"
    if _is_dispatch(node.func):
        # The provider is the one positional argument; content is keywords.
        if len(node.args) != 1 or isinstance(node.args[0], ast.Starred):
            return "passes a positional argument besides the provider"
        call = node
    elif isinstance(node.func, ast.Name):  # getattr(x, "route")
        return f"reaches {node.args[1].value} through getattr"
    else:
        call = node
        if call.args:
            return "passes a positional argument"
    sent = {k.arg: k.value for k in call.keywords if k.arg in _CONTENT_KWARGS}
    if not sent:
        return "no messages=/prompt= argument the guard can follow"
    if len(sent) > 1:
        return "sends content in both messages= and prompt="
    for k in call.keywords:
        if k.arg is None and not _routing_splat(k.value):
            return f"splats **{ast.unparse(k.value)[:40]}, which can carry content"
        if k.arg is not None and k.arg not in _CONTENT_KWARGS + tuple(_ROUTING_KWARGS):
            return f"passes {k.arg}=, a channel the guard does not follow"
    [value] = sent.values()
    if not isinstance(value, ast.Name):
        return f"sends an expression ({ast.unparse(value)[:60]}), not a redacted name"
    name = value.id

    # The reference point: the call itself, or — when the name is bound in an
    # enclosing function — the nested def the call sits in (a closure can run
    # whenever, so a rebinding after its definition reaches it too).
    point, scopes = call, _scopes(call, parents)
    for func in scopes:
        bindings = _binding_nodes(func, name)
        mutated = _mutations(func, name)
        if mutated:
            return f"{name!r} is mutated at line {mutated[0].lineno}"
        if not bindings:
            if name in _parameters(func):
                return f"{name!r} is a parameter of {func.name}; redact it at the site"
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
        if not reaching:
            return f"{name!r} is not bound before the send"
        if not _definitely_bound(func.body, name, point.lineno):
            return f"{name!r} may reach the send unbound or unredacted on some path"
        for binding in reaching:
            problem = _redaction_problem(binding)
            if problem:
                return f"{name!r} {problem}"
        return None
    return f"{name!r} is not bound in an enclosing function"


def _is_dispatch(func) -> bool:
    return (isinstance(func, ast.Name) and func.id == _DISPATCH) or (
        isinstance(func, ast.Attribute) and func.attr == _DISPATCH
    )


def _send_sites(tree):
    parents = _parents(tree)
    for node in ast.walk(tree):
        called = isinstance(parents.get(node), ast.Call) and parents[node].func is node
        if isinstance(node, ast.Call) and (
            _is_dispatch(node.func)
            or (
                isinstance(node.func, ast.Attribute) and node.func.attr in _SEND_METHODS
            )
        ):
            yield node
        elif (
            isinstance(node, ast.Attribute)
            and node.attr in _SEND_METHODS | {_DISPATCH}
            and isinstance(node.ctx, ast.Load)
            and not called
        ):
            yield node
        elif (
            isinstance(node, ast.Name)
            and node.id == _DISPATCH
            and isinstance(node.ctx, ast.Load)
            and not called
        ):
            yield node
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) > 1
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in _SEND_METHODS | {_DISPATCH}
        ):
            yield node


@functools.lru_cache(maxsize=1)
def _scan() -> tuple:
    """Every send site: (path, innermost function, line, verdict)."""
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


def test_every_send_site_is_redacted_or_allowlisted():
    unknown = [
        f"{rel}:{line} in {where}: {why}"
        for rel, where, line, why in _scan()
        if why is not None and (rel, where) not in ALLOWLIST
    ]
    assert unknown == [], (
        "an LLM send site must send what the case redaction returned "
        "(`messages = await case_redacted_messages(..., redaction_ctx)`, then "
        "`route(messages=messages)`), or be allowlisted here with its reason: "
        + "\n".join(unknown)
    )


def test_the_redacted_sites_are_the_ones_expected():
    found = {(rel, where) for rel, where, _, why in _scan() if why is None}
    assert found == REDACTED_SITES


def test_every_allowlist_entry_still_names_an_unredacted_site():
    """A stale entry would silently admit a new site that reuses its name."""
    unredacted = {(rel, where) for rel, where, _, why in _scan() if why is not None}
    assert set(ALLOWLIST) <= unredacted, set(ALLOWLIST) - unredacted


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
            return await send_to_model(provider, prompt=prompt)
        """,
        # the dispatch, reached by attribute
        """
        async def f(provider, prompt, ctx):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            return await dispatch.send_to_model(provider, messages=messages)
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
        # routing overrides splatted by the *route_kwargs convention
        """
        async def f(router, prompt, ctx, settings):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            return await router.route(
                messages=messages, model="m", **_role_route_kwargs(settings)
            )
        """,
    ],
)
def test_the_classifier_accepts_a_redacted_send(source):
    assert _verdicts(source) == [None]


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
            return await send_to_model(provider, **kwargs)
        """,
        # positional content beside the provider
        """
        async def f(provider, prompt):
            return await send_to_model(provider, prompt)
        """,
        # the provider splatted in
        """
        async def f(args, prompt, ctx):
            prompt = await case_redacted(prompt, ctx)
            return await send_to_model(*args, prompt=prompt)
        """,
        # a provider's transport called directly
        """
        async def f(provider, prompt):
            return await provider._generate(prompt=prompt)
        """,
        # the dispatch, uncalled
        """
        async def f(provider, prompt):
            send = functools.partial(send_to_model, provider)
            return await send(prompt=prompt)
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
            return await send_to_model(provider, prompt=prompt)
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
    ],
)
def test_the_classifier_refuses_an_unredacted_send(source):
    [verdict] = _verdicts(source)
    assert verdict is not None
