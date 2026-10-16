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

The check reads the values it is handed, never an attribute of the provider,
so nothing a provider class or instance defines — an override, a mixin, a
patched attribute, a property, a duck-typed class — changes what is checked or
sees the call before it: the dispatch runs no provider code but ``_generate``,
and only after the check. Its reports name the provider by its class's own
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
send site exercises the check, and a raw send is reported at the line that
made it. ``test_model_dispatch_lexical_1998.py`` (rule S) flags ``.route``
read anywhere else.

A violation is reported — one WARNING line, the
``llm_model_content_violations`` metric and one :class:`RawModelContentWarning`,
none of which carries content — and, while :data:`REFUSE_RAW_MODEL_CONTENT` is
off, the call proceeds. ``_generate`` then receives plain values: exact ``str``
text and plain message dicts. A violation is reported ONCE per call, by the
first layer that sees it — the seam, the router's entry, or this dispatch — and
the layers below do not report it again (:data:`REPORTED_UPSTREAM`).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import warnings
from collections import Counter
from contextvars import ContextVar
from types import FrameType
from typing import (
    Any,
    Coroutine,
    Dict,
    FrozenSet,
    Iterator,
    List,
    NamedTuple,
    Optional,
    Tuple,
)

from faultmaven.infrastructure.llm.providers.base import (
    CONTENT_KWARGS,
    CONTENT_LIST_KWARGS,
    GENERATE_KWARGS,
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

logger = logging.getLogger(__name__)

#: Whether a model call whose content the redaction module did not mint is
#: REFUSED (``TypeError`` before any network I/O) instead of reported and sent.
#: Off while callers migrate (#1998 P1/P2); P3 turns refusal on and deletes
#: this switch with :class:`RawModelContentWarning`. A module constant on
#: purpose: no deployment can configure it (fm#990).
REFUSE_RAW_MODEL_CONTENT = False


class RawModelContentWarning(UserWarning):
    """A model call carried content the redaction module did not mint (#1998)."""


class RawModelContentRefused(TypeError):
    """A model call refused under :data:`REFUSE_RAW_MODEL_CONTENT` (#1998).

    A fault of the request, not of a provider: the registry re-raises it
    without counting a provider failure, and ``retryable`` is ``False`` so
    ``BaseExternalClient.call_external`` neither retries it nor counts it
    against the router's circuit breaker (request-scoped). Refusing a raw call
    must never make the next minted call fail.
    """

    retryable = False


class SendSite(NamedTuple):
    """The line a model call was made from, for the dispatch's warning.

    A file name, a line number, a module name and that module's globals — the
    dict ``warnings.warn`` itself would hand ``warn_explicit``, so the default
    once-per-location filter applies. Never a frame or its locals: nothing a
    request carries is held here.
    """

    filename: str
    lineno: int
    module: str
    module_globals: Dict[str, Any]


#: ``faultmaven/infrastructure/llm/`` and asyncio, as path prefixes, and the
#: one transport module outside the llm package a send passes through:
#: ``BaseExternalClient.call_external``, which awaits the registry inline when
#: the call has no timeout.
_LLM_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__)) + os.sep
_ASYNCIO_DIR = os.path.dirname(os.path.abspath(asyncio.__file__)) + os.sep
_TRANSPORT_FILE = os.path.join(
    os.path.dirname(os.path.dirname(_LLM_PACKAGE_DIR)), "base_client.py"
)

#: The send site of the routed call in progress, recorded by ``LLMRouter.route``
#: before ``call_external`` runs the registry in a task of its own (on 3.11,
#: ``asyncio.wait_for`` wraps it in one), across which no frame reaches the
#: caller. A task copies the context it is created in, so the dispatch reads it.
#: ``route`` resets it when the call returns. A task spawned INSIDE that window
#: (a background flush, say) inherits the value, so a send that task makes
#: later, from a stack whose frames cannot name it either, is labelled with the
#: routed call's site: it only ever affects a label, never what is checked.
SEND_SITE: ContextVar[Optional[SendSite]] = ContextVar("llm_send_site", default=None)

#: The ``(argument, kind)`` pairs already reported for the routed call in
#: progress: set by the coroutine :func:`route_model_call` returns around
#: ``router.route``, and widened by ``route()`` around its registry call with
#: what its entry check reported (:func:`reported_upstream`). ``route()``'s
#: entry report and :func:`send_to_model` report only what is not in it, so a raw send is
#: reported once, at the line that made it, however many layers and fallback
#: providers it passes. Refusal never reads it: a refused call is refused
#: wherever it is checked. Like :data:`SEND_SITE`, a task spawned inside the
#: window inherits the value, so a send that task makes later would not
#: report those same pairs again; it is still checked, and still refused.
REPORTED_UPSTREAM: ContextVar[FrozenSet[Tuple[str, str]]] = ContextVar(
    "llm_reported_upstream", default=frozenset()
)


def _site_of(frame: FrameType) -> SendSite:
    return SendSite(
        frame.f_code.co_filename,
        frame.f_lineno,
        frame.f_globals.get("__name__", "?"),
        frame.f_globals,
    )


_ROUTER_MODULE = "faultmaven.infrastructure.llm.router"


def _is_route_override(frame: FrameType) -> bool:
    """Whether ``frame`` runs ``route`` on an ``LLMRouter`` subclass defined
    outside this package (the cloud's tenant router): a hop on the way to the
    router, never the line that made the call.

    The receiver is the frame's first argument, its type matched by ``__mro__``
    (no ``isinstance``, which a metaclass can answer). The router class is read
    from ``sys.modules``, so this module never imports the router.
    """
    code = frame.f_code
    if code.co_name != "route" or code.co_argcount < 1:
        return False
    router_module = sys.modules.get(_ROUTER_MODULE)
    router = getattr(router_module, "LLMRouter", None)
    receiver = frame.f_locals.get(code.co_varnames[0])
    return router is not None and router in type(receiver).__mro__


def _walk_to_caller(
    frame: FrameType,
) -> "tuple[Optional[SendSite], FrameType]":
    """The nearest frame from ``frame`` up outside ``infrastructure/llm/`` (and
    the transport module, and any ``route`` override of the router), and the
    outermost frame inside it the walk passed.

    The first is ``None`` when an asyncio frame comes first: the stack ends at
    a task boundary, and whatever lies beyond it (the event loop's driver) is
    not the caller.
    """
    outermost = frame
    cursor: Optional[FrameType] = frame
    while cursor is not None:
        filename = os.path.abspath(cursor.f_code.co_filename)
        if filename.startswith(_ASYNCIO_DIR):
            return None, outermost
        if (
            not filename.startswith(_LLM_PACKAGE_DIR)
            and filename != _TRANSPORT_FILE
            and not _is_route_override(cursor)
        ):
            return _site_of(cursor), outermost
        outermost = cursor
        cursor = cursor.f_back
    return None, outermost


def find_send_site(frame: FrameType) -> Optional[SendSite]:
    """The nearest frame from ``frame`` up outside ``infrastructure/llm/``,
    or ``None`` when a task boundary comes first."""
    return _walk_to_caller(frame)[0]


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


def refuse_if_refusing(target: str, violations: List[ContentViolation]) -> None:
    """With :data:`REFUSE_RAW_MODEL_CONTENT` on, refuse: :class:`RawModelContentRefused`
    (a ``TypeError``), naming where and what kind — never the content. Off,
    return."""
    if violations and REFUSE_RAW_MODEL_CONTENT:
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
    """The provider's label for a report, read without invoking anything on it.

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


def report_raw_model_content(
    target: str, violations: List[ContentViolation], frame: FrameType
) -> None:
    """Report, from outside the dispatch, a call's violations at its entry.

    ``LLMRouter.route`` reports what its entry check found here, in warn mode
    (under refusal it has already refused them), naming the call's site from
    ``frame``, and then holds them in :func:`reported_upstream` around the
    registry, so :func:`send_to_model` does not report them again (#1998 P2b).
    A router reached through a test's registry double, a cache hit, a falsy
    value it drops: each is reported, once, whether or not a dispatch is ever
    reached. What :func:`route_model_call` already reported for the call is
    not reported again (:data:`REPORTED_UPSTREAM`).
    """
    violations = _not_reported_upstream(violations)
    if violations:
        _report_raw_model_content(target, violations, _send_site(frame))


@contextlib.contextmanager
def reported_upstream(violations: List[ContentViolation]) -> Iterator[None]:
    """Hold ``violations`` as reported for the call inside: what a layer has
    reported, the layers below it do not report again (:data:`REPORTED_UPSTREAM`).
    Refusal never reads it."""
    token = REPORTED_UPSTREAM.set(
        REPORTED_UPSTREAM.get() | frozenset((v.argument, v.kind) for v in violations)
    )
    try:
        yield
    finally:
        REPORTED_UPSTREAM.reset(token)


def _not_reported_upstream(
    violations: List[ContentViolation],
) -> List[ContentViolation]:
    """``violations`` less the ``(argument, kind)`` pairs the seam reported."""
    upstream = REPORTED_UPSTREAM.get()
    if not upstream:
        return violations
    return [v for v in violations if (v.argument, v.kind) not in upstream]


def _report_raw_model_content(
    provider_name: str, violations: List[ContentViolation], site: SendSite
) -> None:
    """Report a call whose content the dispatch does not accept.

    One log line, one metric increment per distinct (argument, kind), and one
    :class:`RawModelContentWarning`, all naming where and what kind — never the
    content; a keyword's name only as an identifier (:func:`key_names`). Both
    name the call's send site.
    """
    counts = Counter((v.argument, v.kind) for v in violations)
    kwargs_named = sorted(v.where for v in violations if v.kind == UNKNOWN_KWARG)
    for argument, kind in sorted(counts):
        llm_model_content_violations.labels(
            provider=provider_name,
            argument=argument,
            kind=kind,
            content="raw",
        ).inc()
    logger.warning(
        "Model-bound content not minted by the redaction module reached the "
        "%s provider dispatch and was sent (#1998), from %s:%d: %s",
        provider_name,
        site.module,
        site.lineno,
        "; ".join(
            f"{argument} {kind} x{count}"
            for (argument, kind), count in sorted(counts.items())
        )
        + (f" [kwargs: {','.join(kwargs_named)}]" if kwargs_named else ""),
    )
    warnings.warn_explicit(
        f"model-bound content not minted by the redaction module: "
        f"{_summary(violations)}",
        RawModelContentWarning,
        site.filename,
        site.lineno,
        module=site.module,
        registry=site.module_globals.setdefault("__warningregistry__", {}),
        module_globals=site.module_globals,
    )


def _send_site(frame: FrameType) -> SendSite:
    """Where the reported call was made: the line a reader can act on.

    The nearest frame above :func:`send_to_model` outside
    ``infrastructure/llm/`` and asyncio. A routed call reaches the dispatch in
    a task of its own, whose stack holds only the registry; it gets the site
    the router recorded on entry (:data:`SEND_SITE`). Failing both — a send its
    caller made the root of a task (``asyncio.wait_for(router.route(...))`` on
    3.11), so no frame reaches it — it names the outermost
    ``infrastructure/llm/`` frame of that task (the registry's dispatch), never
    the event loop.
    """
    site, outermost = _walk_to_caller(frame)
    return site or SEND_SITE.get() or _site_of(outermost)


async def send_to_model(
    provider: Any,
    *,
    prompt: Any = None,
    **kwargs: Any,
) -> LLMResponse:
    """Send one model call to ``provider``: check it, then ``provider._generate``.

    Every send ends here — through the router's registry, or straight to a
    concrete provider (the engine's dedicated DA provider, the connection
    check). The check runs on the values (:func:`model_call_violations`);
    a violation is reported, or refused under :data:`REFUSE_RAW_MODEL_CONTENT`,
    before any network I/O. ``_generate`` then receives plain values: exact
    ``str`` text (unwrapped with ``str.__str__``, so a ``(str, Enum)`` keeps
    its value) and message dicts copied only where something changes.

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
    """
    violations = model_call_violations(prompt, kwargs.get("messages"), kwargs)
    if violations:
        refuse_if_refusing(_provider_label(provider), violations)
        unreported = _not_reported_upstream(violations)
        if unreported:
            _report_raw_model_content(
                _provider_label(provider), unreported, _send_site(sys._getframe())
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
    the router still exercises the check, at the line it drives. Content only:
    the other keywords are ``route()``'s own, a closed signature, not a
    provider's. A violation is refused under :data:`REFUSE_RAW_MODEL_CONTENT`
    (raised here, before the router is called) and otherwise reported, here,
    naming the caller's line.

    A plain function returning the coroutine, not an ``async def``: the send
    site is read from the caller's frame while it is on the stack. Under
    ``asyncio.wait_for`` the coroutine becomes the root of a task of its own,
    from which no frame reaches the caller, and the report would name this
    module. The coroutine records that site (:data:`SEND_SITE`) and what was
    reported (:data:`REPORTED_UPSTREAM`) around ``router.route``, so the
    router's own reports and :func:`send_to_model` name the same line and
    report only what this did not.
    """
    site = _send_site(sys._getframe())
    violations = model_call_violations(
        None if prompt is _UNSET else prompt, kwargs.get("messages"), {}
    )
    if violations:
        refuse_if_refusing("router", violations)
        _report_raw_model_content("router", violations, site)
    if prompt is not _UNSET:
        kwargs["prompt"] = prompt
    return _routed(
        router, site, frozenset((v.argument, v.kind) for v in violations), kwargs
    )


async def _routed(
    router: Any,
    site: SendSite,
    reported: FrozenSet[Tuple[str, str]],
    kwargs: Dict[str, Any],
) -> LLMResponse:
    site_token = SEND_SITE.set(site)
    reported_token = REPORTED_UPSTREAM.set(reported)
    try:
        return await router.route(**kwargs)
    finally:
        REPORTED_UPSTREAM.reset(reported_token)
        SEND_SITE.reset(site_token)
