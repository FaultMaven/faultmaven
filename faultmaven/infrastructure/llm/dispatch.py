"""The one way a model call reaches a provider: :func:`send_to_model` (#1998).

A provider exposes no public send method. Its adapter implements
``_generate`` (one request to its API, plain values in), and production code
reaches ``_generate`` only through this module's :func:`send_to_model`, which
first checks the call's model-bound content:

* ``prompt`` is a minted ``ModelText`` or ``None``;
* ``messages`` follows the message grammar
  (:func:`~faultmaven.infrastructure.security.model_content.message_violations`);
* the content keywords (``CONTENT_KWARGS``) are minted text, and every element
  of the content-list keywords (``CONTENT_LIST_KWARGS``: ``stop_sequences``)
  is too;
* every keyword is one an adapter reads (``GENERATE_KWARGS``).

A call that fails the check is REFUSED (:func:`refuse`): it raises
:class:`RawModelContentRefused` before any network I/O and nothing is sent.
There is no warn path. A refusal is a programming error — a send site that did
not mint what it sends — so it is never a reason to fall back: every handler
between a send site and its request or job boundary lets it through
(``tests/unit/architecture/test_model_refusal_propagates_1998.py``).

The check reads the values it is handed, never an attribute of the provider,
so nothing a provider class or instance defines — an override, a mixin, a
patched attribute, a property, a duck-typed class — changes what is checked or
sees the call before it: the dispatch runs no provider code but ``_generate``,
and only after the check. A refusal names the provider by its class's own
name, read without invoking anything on it. The values it accepts are exact
builtins (``list``, ``dict``, ``str``, ``ModelText``), so no method of the
caller's containers runs during the check either, and the adapter reads what
was checked. Calling ``_generate`` (or an adapter's own transport callables)
from anywhere else is a private-name call, and
``tests/unit/architecture/test_model_dispatch_lexical_1998.py`` flags it, as
it flags the direct spellings that rebind :func:`send_to_model` or a name this
module binds. The boundary prevents ACCIDENTAL raw sends: bypass requires a
deliberate act — a flagged spelling, computed reflection, or runtime mutation
of a first-party module's namespace — and those are out of scope.

The router keeps ``route()`` as its API. ``route()`` runs the same check at
entry, on the caller's values and without unwrapping them, so a refusal comes
before its ``SANITIZE_PII`` pass; it then re-mints through that pass and the
registry hands the result here. The router itself is never a ``_generate``
target: this module unwraps, so a router behind it would see every minted
call as raw.

Production code reaches a router through :func:`route_model_call`, never
through ``router.route`` itself (#1998 P2): the seam runs the same content
check BEFORE any router, real or a test's double, so every test that drives a
send site exercises the check, and a raw send is refused at the line that
made it. ``test_model_dispatch_lexical_1998.py`` (rule S) flags ``.route``
read anywhere else.

The one ops signal of a refusal is the ``llm_model_content_violations``
counter (target, argument and kind; never content). No log line is written
here: the exception carries the summary, and the boundary that handles it
logs it.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Coroutine, Dict, FrozenSet, List

from faultmaven.exceptions import RawModelContentRefused
from faultmaven.infrastructure.llm.providers.base import (
    CONTENT_KWARGS,
    CONTENT_LIST_KWARGS,
    FORWARDED_KNOB_KWARGS,
    GENERATE_KWARGS,
    PROVIDER_API_KWARGS,
    ROUTER_CONSUMED_KWARGS,
    LLMResponse,
)
from faultmaven.infrastructure.security.model_content import (
    BAD_SHAPE,
    UNKNOWN_KWARG,
    ContentViolation,
    key_names,
    message_violations,
    plain_messages,
    plain_text,
    text_violation,
)
from faultmaven.infrastructure.shims.metrics import llm_model_content_violations


def model_call_violations(
    prompt: Any, messages: Any, kwargs: Dict[str, Any]
) -> List[ContentViolation]:
    """Every way a model call's content is not what the dispatch accepts.

    ``kwargs`` is every keyword but ``prompt`` (``messages`` may be among them:
    it is a member of the closed set). The same check ``LLMRouter.route`` runs
    at entry, on a routed call's values.

    A keyword name that is not an exact ``str`` (a ``str`` subclass can answer
    ``in`` and ``==`` differently each time it is asked) is a violation, and
    the keywords are then not looked at again: every lookup below would run
    its code.
    """
    found: List[ContentViolation] = []
    if any(type(key) is not str for key in kwargs):
        found.append(ContentViolation("kwargs", BAD_SHAPE, "kwargs"))
        kwargs = {}
    if prompt is not None:
        violation = text_violation(prompt, "prompt", "prompt")
        if violation is not None:
            found.append(violation)
    if messages is not None:
        found.extend(message_violations(messages))
    for key in CONTENT_KWARGS:
        if key in kwargs and kwargs[key] is not None:
            violation = text_violation(kwargs[key], key, key)
            if violation is not None:
                found.append(violation)
    for key in CONTENT_LIST_KWARGS:
        if key in kwargs and kwargs[key] is not None:
            found.extend(_text_list_violations(kwargs[key], key))
    for key in sorted(kwargs):
        if key not in GENERATE_KWARGS:
            found.append(ContentViolation("kwargs", UNKNOWN_KWARG, key_names([key])))
    return found


def _text_list_violations(value: Any, key: str) -> List[ContentViolation]:
    """A content-list keyword: an exact ``list`` or ``tuple`` of minted text."""
    kind = type(value)
    if kind is not list and kind is not tuple:
        return [ContentViolation(key, BAD_SHAPE, key)]
    found: List[ContentViolation] = []
    for index, item in enumerate(value):
        violation = text_violation(item, key, f"{key}[{index}]")
        if violation is not None:
            found.append(violation)
    return found


def _summary(violations: List[ContentViolation]) -> str:
    counts = Counter((v.argument, v.kind) for v in violations)
    kwargs_named = sorted(v.where for v in violations if v.kind == UNKNOWN_KWARG)
    return "; ".join(
        f"{argument} {kind}"
        + (f" ({','.join(kwargs_named)})" if kind == UNKNOWN_KWARG else "")
        for argument, kind in sorted(counts)
    )


def refuse(target: str, violations: List[ContentViolation]) -> None:
    """Refuse a call with ``violations``: count it, then raise
    :class:`RawModelContentRefused` (a ``TypeError``) naming where and what
    kind — never the content; a keyword's name only as an identifier
    (:func:`key_names`). No violations, return.

    ``target`` is ``"router"`` for the seam and the router's entry, else the
    provider's label (:func:`_provider_label`). The counter is incremented
    once per distinct (argument, kind).
    """
    if not violations:
        return
    for argument, kind in sorted({(v.argument, v.kind) for v in violations}):
        llm_model_content_violations.labels(
            target=target, argument=argument, kind=kind
        ).inc()
    raise RawModelContentRefused(
        f"{target}: model-bound content not minted by the redaction "
        f"module: {_summary(violations)}"
    )


#: ``type``'s own slots: a class's name, its MRO and its namespace, read through
#: the type object itself, so no metaclass attribute and no provider code runs
#: to produce a label.
_TYPE_NAME = type.__dict__["__name__"]
_TYPE_MRO = type.__dict__["__mro__"]
_TYPE_NAMESPACE = type.__dict__["__dict__"]


def _provider_label(provider: Any) -> str:
    """The provider's label for a refusal, read without invoking anything on it.

    The ``LABEL`` class constant every adapter declares (``"openai"``, the
    value ``provider_name`` returns and ``llm_requests_total`` /
    ``llm_provider_calls_total`` carry), read from the first class in the MRO
    whose own namespace holds an exact ``str`` under that name. A provider with
    none (a test double, a duck) is labelled with its class's name.
    ``provider_name`` itself is never read: it is a property, and a property
    runs while this frame still holds the call's content.
    """
    cls = type(provider)
    for klass in _TYPE_MRO.__get__(cls):
        label = _TYPE_NAMESPACE.__get__(klass).get("LABEL")
        if type(label) is str:
            return label
    return _TYPE_NAME.__get__(cls)


async def send_to_model(
    provider: Any,
    *,
    prompt: Any = None,
    **kwargs: Any,
) -> LLMResponse:
    """Send one model call to ``provider``: check it, then ``provider._generate``.

    Every send ends here — through the router's registry, or straight to a
    concrete provider (the engine's dedicated DA provider, the connection
    check). The check runs on the values (:func:`model_call_violations`); a
    violation is refused (:func:`refuse`) before any network I/O.
    ``_generate`` then receives plain values: exact ``str`` text (unwrapped
    with ``str.__str__``) and message dicts copied only where something
    changes.

    ``messages`` travels in ``**kwargs``, not as a parameter of its own, so
    every keyword reaches ``_generate`` in the order the caller wrote it: a
    transport that merges leftover keywords into its request body (llama.cpp's
    ``/completion``) emits them in that order, and a ``messages`` parameter
    moved it behind ``tools`` in that body (#1998 wire check).

    Args:
        provider: the concrete provider (its ``_generate`` sends the request)
        prompt: the prompt, or ``None`` when ``messages`` carries the call
        **kwargs: ``messages`` and the other keywords in ``GENERATE_KWARGS``

    Returns:
        The provider's ``LLMResponse``

    Raises:
        RawModelContentRefused: the call's content was not minted, or a
            keyword is outside the closed set; nothing was sent.
    """
    refuse(
        _provider_label(provider),
        model_call_violations(prompt, kwargs.get("messages"), kwargs),
    )
    # ``kwargs`` is this call's own dict: unwrapping in place mutates nothing
    # the caller holds (plain_messages copies what it changes), and keeps each
    # key where the caller put it.
    if "messages" in kwargs:
        kwargs["messages"] = plain_messages(kwargs["messages"])
    for key in CONTENT_KWARGS:
        if key in kwargs:
            kwargs[key] = plain_text(kwargs[key])
    for key in CONTENT_LIST_KWARGS:
        value = kwargs.get(key)
        if type(value) is list or type(value) is tuple:
            kwargs[key] = type(value)(plain_text(item) for item in value)
    return await provider._generate(
        prompt=plain_text(prompt),
        model=kwargs.pop("model", None),
        max_tokens=kwargs.pop("max_tokens", 1000),
        temperature=kwargs.pop("temperature", 0.7),
        **kwargs,
    )


#: ``prompt`` not passed: forwarded to the router only when the caller wrote it.
_UNSET: Any = object()

#: The keywords ``LLMRouter.route`` declares besides ``prompt``: the only ones
#: the seam hands a router. Its three disjoint groups, each pinned to
#: ``route()``'s signature (``test_router_knobs_never_reach_a_provider.py``),
#: and the set itself pinned to it (``test_route_model_call_1998.py``). Read
#: here from the providers' module, so this module never imports the router.
ROUTE_KWARGS: FrozenSet[str] = (
    ROUTER_CONSUMED_KWARGS | FORWARDED_KNOB_KWARGS | PROVIDER_API_KWARGS
)


def _route_keyword_violations(kwargs: Dict[str, Any]) -> List[ContentViolation]:
    """Every keyword the seam would hand a router that ``route()`` does not
    declare. A keyword name that is not an exact ``str`` is a violation and the
    names are not looked at again (``model_call_violations`` says why)."""
    if any(type(key) is not str for key in kwargs):
        return [ContentViolation("kwargs", BAD_SHAPE, "kwargs")]
    return [
        ContentViolation("kwargs", UNKNOWN_KWARG, key_names([key]))
        for key in sorted(kwargs)
        if key not in ROUTE_KWARGS
    ]


def route_model_call(
    router: Any, *, prompt: Any = _UNSET, **kwargs: Any
) -> Coroutine[Any, Any, LLMResponse]:
    """Send one routed model call: check its content, then ``router.route``.

    The one way production code reaches a router (#1998 P2): every send site
    writes ``await route_model_call(router, messages=..., ...)`` where it
    used to write ``await router.route(messages=..., ...)``. The keywords are
    ``route()``'s, passed through unchanged; ``prompt`` reaches the router only
    when the caller passed it.

    It checks the call's model-bound content — ``prompt`` and ``messages``,
    the same check ``route()`` runs at entry (:func:`model_call_violations`)
    — before any router runs, real or a test's double: a test that replaces
    the router still exercises the check, at the line it drives. And it
    refuses a keyword ``route()`` does not declare (:data:`ROUTE_KWARGS`,
    ``unknown_kwarg``): a double would accept ``system=`` or ``preamble=``
    silently, while the real router raises a bare ``TypeError`` that a
    fail-open handler takes for an ordinary failure (#2035 review F1). The
    router's own knobs (``case_id``, ``bypass_cache``) are ``route()``
    parameters and pass. A violation is refused here, before the router is
    called.

    A plain function returning the router's coroutine, not an ``async def``:
    the refusal is raised at the call itself, on the caller's line, whether or
    not the caller then hands the coroutine to ``asyncio.wait_for``.

    Raises:
        RawModelContentRefused: the call's content was not minted, or it
            passed a keyword ``route()`` does not declare; nothing reached
            the router.
    """
    refuse(
        "router",
        model_call_violations(
            None if prompt is _UNSET else prompt, kwargs.get("messages"), {}
        )
        + _route_keyword_violations(kwargs),
    )
    if prompt is not _UNSET:
        kwargs["prompt"] = prompt
    return router.route(**kwargs)
