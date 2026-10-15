"""The one way a model call reaches a provider: :func:`send_to_model` (#1998).

A provider exposes no public send method. Its adapter implements
``_generate`` (one request to its API, plain values in), and production code
reaches ``_generate`` only through this module's :func:`send_to_model`, which
first checks the call's model-bound content:

* ``prompt`` is a minted ``ModelText`` or ``None``;
* ``messages`` follows the message grammar
  (:func:`~faultmaven.infrastructure.security.model_content.message_violations`);
* the content keywords (``CONTENT_KWARGS``) are minted text;
* every keyword is one an adapter reads (``GENERATE_KWARGS``).

The check reads the values it is handed, not an attribute of the provider, so
nothing a provider class or instance defines — an override, a mixin, a
patched attribute, a duck-typed class — changes what is checked: the dispatch
never calls anything on the provider but ``_generate``. Calling ``_generate``
(or an adapter's own transport callables) from anywhere else is a private-name
call, and ``tests/unit/architecture/test_model_dispatch_lexical_1998.py``
flags it, as it flags anything that rebinds :func:`send_to_model`.

The router keeps ``route()`` as its API. ``route()`` runs the same check at
entry, on the caller's values and without unwrapping them, so a refusal comes
before its ``SANITIZE_PII`` pass; it then re-mints through that pass and the
registry hands the result here. The router itself is never a ``_generate``
target: this module unwraps, so a router behind it would see every minted
call as raw.

A violation is reported — one WARNING line, the
``llm_model_content_violations`` metric and one :class:`RawModelContentWarning`,
none of which carries content — and, while :data:`REFUSE_RAW_MODEL_CONTENT` is
off, the call proceeds. ``_generate`` then receives plain values: exact ``str``
text and plain message dicts.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import warnings
from collections import Counter
from contextvars import ContextVar
from types import FrameType
from typing import Any, Dict, List, NamedTuple, Optional

from faultmaven.infrastructure.llm.providers.base import (
    CONTENT_KWARGS,
    GENERATE_KWARGS,
    LLMResponse,
)
from faultmaven.infrastructure.security.model_content import (
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


def _site_of(frame: FrameType) -> SendSite:
    return SendSite(
        frame.f_code.co_filename,
        frame.f_lineno,
        frame.f_globals.get("__name__", "?"),
        frame.f_globals,
    )


def _walk_to_caller(
    frame: FrameType,
) -> "tuple[Optional[SendSite], FrameType]":
    """The nearest frame from ``frame`` up outside ``infrastructure/llm/`` (and
    the transport module), and the outermost frame inside it the walk passed.

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
        if not filename.startswith(_LLM_PACKAGE_DIR) and filename != _TRANSPORT_FILE:
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
    """
    found: List[ContentViolation] = []
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
    for key in sorted(kwargs):
        if key not in GENERATE_KWARGS:
            found.append(ContentViolation("kwargs", UNKNOWN_KWARG, key_names([key])))
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
    """With :data:`REFUSE_RAW_MODEL_CONTENT` on, refuse: ``TypeError``, naming
    where and what kind — never the content. Off, return."""
    if violations and REFUSE_RAW_MODEL_CONTENT:
        raise TypeError(
            f"{target}: model-bound content not minted by the redaction "
            f"module: {_summary(violations)}"
        )


def _provider_label(provider: Any) -> str:
    name = getattr(provider, "provider_name", None)
    return name if isinstance(name, str) else type(provider).__name__


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
        _report_raw_model_content(
            _provider_label(provider), violations, _send_site(sys._getframe())
        )
    # ``kwargs`` is this call's own dict: unwrapping in place mutates nothing
    # the caller holds (plain_messages copies what it changes), and keeps each
    # key where the caller put it.
    if "messages" in kwargs:
        kwargs["messages"] = plain_messages(kwargs["messages"])
    for key in CONTENT_KWARGS:
        if key in kwargs:
            kwargs[key] = plain_text(kwargs[key])
    return await provider._generate(
        prompt=plain_text(prompt),
        model=kwargs.pop("model", None),
        max_tokens=kwargs.pop("max_tokens", 1000),
        temperature=kwargs.pop("temperature", 0.7),
        **kwargs,
    )
