"""#1956: every LLM send site in the package is classified, by AST.

A send site is any ``<x>.route(...)`` or ``<x>.generate(...)`` call. Each one
must be ONE of:

(R) redacted at the site: its ``messages=`` (or ``prompt=``) argument is a name
    whose last binding before the call, in the calling function or an
    enclosing one, is ``await case_redacted(...)``,
    ``await case_redacted_messages(...)`` or ``await <x>.asanitize_messages(...)``.
    So the sites read ``messages = await case_redacted_messages(...)`` and then
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
_SEND_METHODS = {"route", "generate"}
_REDACTING_HELPERS = {"case_redacted", "case_redacted_messages"}
_REDACTING_METHODS = {"asanitize_messages"}

#: (path relative to the repo, innermost enclosing function) -> why the site
#: needs no case pass of its own.
ALLOWLIST: dict[tuple[str, str], str] = {
    ("faultmaven/api/routes/admin_config.py", "check_llm_connection"): (
        "a constant connection-test prompt; no case content"
    ),
    ("faultmaven/infrastructure/llm/router.py", "generate"): (
        "router internals: ILLMProvider.generate delegating to route"
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


def _binds(node, name: str) -> bool:
    targets: list = []
    if isinstance(node, ast.Assign):
        targets = node.targets
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
        targets = [node.target]
    elif isinstance(node, (ast.For, ast.AsyncFor)):
        targets = [node.target]
    elif isinstance(node, ast.withitem) and node.optional_vars is not None:
        targets = [node.optional_vars]
    return any(
        isinstance(n, ast.Name) and n.id == name for t in targets for n in ast.walk(t)
    )


def _is_redacting(value) -> bool:
    if not isinstance(value, ast.Await) or not isinstance(value.value, ast.Call):
        return False
    func = value.value.func
    if isinstance(func, ast.Name):
        return func.id in _REDACTING_HELPERS
    return isinstance(func, ast.Attribute) and func.attr in _REDACTING_METHODS


def _parameters(func) -> set[str]:
    a = func.args
    names = [x.arg for x in a.posonlyargs + a.args + a.kwonlyargs]
    names += [x.arg for x in (a.vararg, a.kwarg) if x is not None]
    return set(names)


def classify(call: ast.Call, parents) -> str | None:
    """``None`` when the call is redacted at the site (R), else why not."""
    sent = {k.arg: k.value for k in call.keywords if k.arg in ("messages", "prompt")}
    if not sent:
        return "no messages=/prompt= argument the guard can follow"
    value = sent.get("messages", sent.get("prompt"))
    if not isinstance(value, ast.Name):
        return f"sends an expression ({ast.unparse(value)[:60]}), not a redacted name"
    name = value.id
    for func in _scopes(call, parents):
        bindings = [
            n for n in _own_nodes(func) if _binds(n, name) and n.lineno <= call.lineno
        ]
        if bindings:
            last = max(bindings, key=lambda n: (n.lineno, n.col_offset))
            if isinstance(last, (ast.Assign, ast.AnnAssign)) and _is_redacting(
                last.value
            ):
                return None
            return f"{name!r} is last bound at line {last.lineno} by something else"
        if name in _parameters(func):
            return f"{name!r} is a parameter of {func.name}; redact it at the site"
    return f"{name!r} is not bound in an enclosing function"


def _send_sites(tree):
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _SEND_METHODS
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
            return await provider.generate(prompt=prompt)
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
    ],
)
def test_the_classifier_refuses_an_unredacted_send(source):
    [verdict] = _verdicts(source)
    assert verdict is not None
