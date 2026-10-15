"""Model-bound content: the one type a provider accepts, and the only ways in (#1998).

The invariant: every model call, through the router or straight to a provider,
carries model-bound content only as values this module minted. The one
dispatch every send passes, ``faultmaven.infrastructure.llm.dispatch``, checks
it at runtime, on the value, so no spelling of a send can get raw text past it.

A minted value is a :class:`ModelText`, a ``str`` subclass that records where
it came from (``source``) and whether the context that minted it redacted
(``redacted``). It is a ``str`` so the code that slices, bounds and
concatenates prompts after redaction keeps working, and so a test comparing
``message["content"]`` with a literal still compares equal. It refuses the
operations that would lose its provenance silently: concatenating it with a
plain ``str`` and formatting it (an f-string or ``str.format``) both raise at
the call site. Any other ``str`` method returns a plain ``str``, which the
boundary then refuses as raw.

Message lists stay plain ``list[dict]``. The boundary validates them by
grammar (:func:`message_violations`): which keys a message of each role may
carry, and which of their values must be minted.

The ways in, and nothing else:

* :func:`case_redacted` / :func:`case_redacted_messages` — case content,
  through the case's own redaction context (required, never ``None``);
* :func:`model_boundary_redacted` — the knowledge-authoring paths, through
  :func:`~faultmaven.infrastructure.security.case_redaction.model_boundary_redaction`;
* :func:`model_output` — the model's own reply, echoed back into a tool loop;
* :func:`non_case_text` — text that carries no case content (a fixed system
  instruction, a connection check). Its call sites are pinned one by one in
  ``tests/unit/architecture/test_non_case_text_census_1998.py``;
* :func:`sanitizer_pass` — the router's ``SANITIZE_PII`` pass: vetted values
  in, the same kind re-minted out. A raw value is sanitized as before and left
  raw, so the boundary still sees it.

Construction is private: ``ModelText`` takes a token only this module holds,
and ``tests/unit/architecture/test_model_text_mint_lexical_1998.py`` refuses,
outside this module and the test minter ``tests/model_content.py``, every
spelled way of reaching the constructor, the token or the module's internals.
``str.__new__`` can still build an instance without the token, so the
boundary also refuses a value whose slots were never set. Bypass requires a
deliberate act — a flagged spelling, computed reflection, or runtime mutation
of a first-party module's namespace (this one's or the dispatch's) — and those
are out of scope: the boundary prevents ACCIDENTAL raw sends.

The case helpers here replaced the ones
:mod:`faultmaven.infrastructure.security.case_redaction` had (same names,
plain return values, ``None`` context allowed) once their last callers moved
(#1998 P2a).
"""

from __future__ import annotations

import json
import re
from types import MappingProxyType
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

from faultmaven.infrastructure.security.case_redaction import (
    MODEL_BOUNDARY_SCOPE,
    CaseRedactionContext,
    model_boundary_redaction,
    should_redact,
)

#: Where a minted value came from. Ordered by precedence: a concatenation of
#: two sources carries the higher one, so ``case + non_case`` is ``case``.
SOURCE_CASE = "case"
SOURCE_MODEL_BOUNDARY = "model_boundary"
SOURCE_MODEL_OUTPUT = "model_output"
SOURCE_NON_CASE = "non_case"
_SOURCE_RANK = MappingProxyType(
    {
        SOURCE_CASE: 3,
        SOURCE_MODEL_BOUNDARY: 2,
        SOURCE_MODEL_OUTPUT: 1,
        SOURCE_NON_CASE: 0,
    }
)

#: The token construction requires. Module-private: no other module may name it.
_MINT_TOKEN = object()


class ModelText(str):
    """Text a model may be sent: minted by this module, never built elsewhere.

    ``source`` is one of the ``SOURCE_*`` constants. ``redacted`` is the
    posture of the context that minted it: ``True`` when that context redacted,
    ``False`` when it was disabled (``SANITIZE_PII`` off) or when there was no
    context to apply (model output, non-case text). Both are fixed at minting.
    """

    __slots__ = ("source", "redacted")

    def __new__(cls, text: str, source: str, redacted: bool, token: object = None):
        if token is not _MINT_TOKEN:
            raise TypeError(
                "ModelText is minted only by "
                "faultmaven.infrastructure.security.model_content "
                "(case_redacted, case_redacted_messages, model_boundary_redacted, "
                "model_output, non_case_text)"
            )
        if source not in _SOURCE_RANK:
            raise ValueError(f"unknown ModelText source {source!r}")
        if not isinstance(text, str):
            raise TypeError(f"ModelText wraps a str, not {type(text).__name__}")
        self = str.__new__(cls, text)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "redacted", bool(redacted))
        return self

    def __init_subclass__(cls, **kwargs: Any) -> None:
        raise TypeError("ModelText cannot be subclassed")

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("ModelText is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("ModelText is immutable")

    def __str__(self) -> str:
        # The exact-``str`` copy: what an adapter or SDK is handed.
        return str.__str__(self)

    def __format__(self, format_spec: str) -> str:
        raise TypeError(
            "a ModelText cannot be formatted (f-string, str.format, format()): "
            "the result would be a plain str with no provenance. Concatenate "
            "minted values instead, or mint the formatted text."
        )

    def __add__(self, other: object) -> "ModelText":
        if type(other) is not ModelText:
            raise TypeError(
                f"cannot concatenate ModelText and {type(other).__name__}: "
                "mint the other operand first"
            )
        return _concat(self, other)

    def __radd__(self, other: object) -> "ModelText":
        # Reached first for ``str + ModelText``: a subclass that defines the
        # reflected method takes priority over its base's operator.
        if type(other) is not ModelText:
            raise TypeError(
                f"cannot concatenate {type(other).__name__} and ModelText: "
                "mint the other operand first"
            )
        return _concat(other, self)

    def __getitem__(self, key: Any) -> "ModelText":
        source, redacted = _require_provenance(self)
        return _mint(str.__getitem__(self, key), source, redacted)

    def __copy__(self) -> "ModelText":
        return self

    def __deepcopy__(self, memo: Any) -> "ModelText":
        return self

    def __reduce__(self) -> Any:
        raise TypeError("ModelText is minted per call and is never pickled")

    def __reduce_ex__(self, protocol: Any) -> Any:
        raise TypeError("ModelText is minted per call and is never pickled")


def _mint(text: str, source: str, redacted: bool) -> ModelText:
    return ModelText(text, source, redacted, _MINT_TOKEN)


def provenance(value: Any) -> Optional[Tuple[str, bool]]:
    """``(source, redacted)`` of a value this module minted, else ``None``.

    Exact type only: a ``str`` that is not a ``ModelText`` is raw. A
    ``ModelText`` whose slots were never set (built by ``str.__new__``,
    bypassing the token) is not minted either.
    """
    if type(value) is not ModelText:
        return None
    try:
        source = value.source
        redacted = value.redacted
    except AttributeError:
        return None
    if source not in _SOURCE_RANK or type(redacted) is not bool:
        return None
    return source, redacted


def _require_provenance(value: ModelText) -> Tuple[str, bool]:
    found = provenance(value)
    if found is None:
        raise TypeError("this ModelText was not minted by the redaction module")
    return found


def _concat(left: ModelText, right: ModelText) -> ModelText:
    """``left + right``, carrying the higher-precedence source.

    ``redacted`` is that of the part that set the source; when both parts share
    the source, the result is redacted only if both were.
    """
    left_source, left_redacted = _require_provenance(left)
    right_source, right_redacted = _require_provenance(right)
    text = str.__add__(str.__str__(left), str.__str__(right))
    if left_source == right_source:
        return _mint(text, left_source, left_redacted and right_redacted)
    if _SOURCE_RANK[left_source] > _SOURCE_RANK[right_source]:
        return _mint(text, left_source, left_redacted)
    return _mint(text, right_source, right_redacted)


# ---------------------------------------------------------------------------
# The ways in
# ---------------------------------------------------------------------------

#: The chat roles a redaction helper accepts. A tool-result message carries a
#: ``tool_call_id`` and is built by the tool loop, not by these helpers.
_CHAT_ROLES = frozenset({"system", "user", "assistant"})


def _require_case_context(ctx: Any, helper: str) -> CaseRedactionContext:
    if not isinstance(ctx, CaseRedactionContext):
        raise TypeError(
            f"{helper} needs the case's CaseRedactionContext (load_case_redaction), "
            f"not {type(ctx).__name__}"
        )
    if ctx.case_id == MODEL_BOUNDARY_SCOPE:
        raise TypeError(
            f"{helper} was handed a model-boundary context: mint knowledge-"
            "authoring content with model_boundary_redacted"
        )
    return ctx


def _require_text(text: Any, helper: str) -> str:
    if not isinstance(text, str):
        raise TypeError(f"{helper} takes a str, not {type(text).__name__}")
    return str.__str__(text)


def _require_chat_messages(messages: Any, helper: str) -> List[Dict[str, Any]]:
    """The chat shape a redaction helper mints: ``{"role", "content": str}``.

    Checked whatever the redaction posture, so a disabled context refuses the
    same shapes an enabled one does (CI's Standalone profile runs disabled).
    """
    if not isinstance(messages, list):
        raise TypeError(
            f"{helper} takes a list of messages, not {type(messages).__name__}"
        )
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise TypeError(
                f"{helper}: message {index} is {type(message).__name__}, not dict"
            )
        extra = [key for key in message if key not in ("role", "content")]
        if extra:
            raise TypeError(
                f"{helper}: message {index} carries {key_names(extra)}; only role "
                "and content are minted here"
            )
        if message.get("role") not in _CHAT_ROLES:
            raise TypeError(f"{helper}: message {index} has no chat role")
        if not isinstance(message.get("content"), str):
            raise TypeError(
                f"{helper}: message {index} content is "
                f"{type(message.get('content')).__name__}, not str"
            )
    return messages


async def _redact_messages(
    messages: List[Dict[str, Any]], ctx: CaseRedactionContext, source: str
) -> List[Dict[str, Any]]:
    return [
        {
            "role": message["role"],
            "content": _mint(
                await ctx.asanitize(str.__str__(message["content"])),
                source,
                ctx.enabled,
            ),
        }
        for message in messages
    ]


async def case_redacted(text: str, ctx: CaseRedactionContext) -> ModelText:
    """Case content as a model may see it: through the case's redaction context.

    ``ctx`` is required. ``text`` must be a ``str`` under every posture: a
    disabled context (``SANITIZE_PII`` off) redacts nothing and still refuses
    anything else. The result records the context's posture in ``redacted``.
    """
    ctx = _require_case_context(ctx, "case_redacted")
    text = _require_text(text, "case_redacted")
    return _mint(await ctx.asanitize(text), SOURCE_CASE, ctx.enabled)


async def case_redacted_messages(
    messages: List[Dict[str, Any]], ctx: CaseRedactionContext
) -> List[Dict[str, Any]]:
    """:func:`case_redacted` for a chat-shaped call: every message's content.

    New dicts, the input is not mutated. Each message is ``{"role", "content"}``
    with ``content`` a ``str``; anything else raises ``TypeError`` under every
    posture.
    """
    ctx = _require_case_context(ctx, "case_redacted_messages")
    messages = _require_chat_messages(messages, "case_redacted_messages")
    return await _redact_messages(messages, ctx, SOURCE_CASE)


async def model_boundary_redacted(value: Any, sanitizer: Any) -> Any:
    """Knowledge-authoring content, through the model-boundary redaction.

    ``value`` is a ``str`` (minted to a :class:`ModelText`) or a chat message
    list (new dicts with minted content), with the same shape rules as the case
    helpers. The context is :func:`model_boundary_redaction` over ``sanitizer``.
    """
    ctx = model_boundary_redaction(sanitizer)
    if isinstance(value, str):
        return _mint(
            await ctx.asanitize(str.__str__(value)), SOURCE_MODEL_BOUNDARY, ctx.enabled
        )
    messages = _require_chat_messages(value, "model_boundary_redacted")
    return await _redact_messages(messages, ctx, SOURCE_MODEL_BOUNDARY)


def model_output(response: Any) -> Dict[str, Any]:
    """The model's own reply as an assistant message to echo back to it.

    ``content``, every tool call's ``arguments`` and every ``str`` (leaf or key) of the
    reasoning artifacts (``provider_metadata`` on the message and on each tool
    call: Anthropic's ``assistant_content``, Gemini's ``assistant_parts`` and
    ``thought_signature``) are minted with source ``model_output``: the model
    wrote them from text it was already sent. An adapter echoes those
    artifacts IN PLACE of the content when they are present, so they are a
    content channel like any other. Their structure, and every non-``str``
    leaf, are copied as they are, so the echo reaches the wire unchanged.
    Arguments that are not a ``str`` are JSON-encoded, the form every adapter
    reads.

    The echo is the OpenAI chat format, whatever shape the server answered
    in: ``content`` text (JSON-encoded when an adapter stored an object
    there), and each call's ``function`` as exactly ``name`` then
    ``arguments`` (a JSON string). A server that deviates — arguments as an
    object, the keys in another order, an extra key — is echoed in that
    normalised shape, which the specification takes on input.

    A constructed ``LLMResponse`` can pass anything through here: that is a
    deliberate act, visible where it is written.
    """
    content = response.content or ""
    if not isinstance(content, str):
        # A server that answers a tool call with no text can have its
        # arguments object copied into ``content`` by the adapter: encoded,
        # like non-str arguments below, never refused (#2033 review, F2).
        content = json.dumps(content)
    message: Dict[str, Any] = {
        "role": "assistant",
        "content": _mint(
            _require_text(content, "model_output"), SOURCE_MODEL_OUTPUT, False
        ),
    }
    tool_calls = []
    for call in response.tool_calls or []:
        function = call.function or {}
        arguments = function.get("arguments", "{}")
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments)
        entry: Dict[str, Any] = {
            "id": call.id,
            "type": call.type,
            "function": {
                "name": function.get("name", ""),
                "arguments": _mint(str.__str__(arguments), SOURCE_MODEL_OUTPUT, False),
            },
        }
        if getattr(call, "provider_metadata", None):
            entry["provider_metadata"] = _mint_leaves(call.provider_metadata)
        tool_calls.append(entry)
    if tool_calls:
        message["tool_calls"] = tool_calls
    if getattr(response, "provider_metadata", None):
        message["provider_metadata"] = _mint_leaves(response.provider_metadata)
    return message


def _mint_leaves(value: Any) -> Any:
    """A copy of ``value`` with every ``str`` in it minted as model output.

    Every ``str``: each leaf, and each dict KEY — a key reaches the wire as
    text exactly as a value does (an Anthropic block's key, a ``tool_use``
    input's key, a Gemini part's key).
    """
    if isinstance(value, str):
        return _mint(str.__str__(value), SOURCE_MODEL_OUTPUT, False)
    if isinstance(value, dict):
        return {_mint_leaves(key): _mint_leaves(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_mint_leaves(item) for item in value)
    return value


def non_case_text(text: str) -> ModelText:
    """Text that carries no case content: a claim, made where it is written.

    A plain ``str`` only — an already-minted value is refused, so case content
    cannot be relabelled as non-case. Every call site is pinned, with its
    reason, in ``tests/unit/architecture/test_non_case_text_census_1998.py``.
    """
    if type(text) is not str:
        raise TypeError(f"non_case_text takes a plain str, not {type(text).__name__}")
    return _mint(text, SOURCE_NON_CASE, False)


async def sanitizer_pass(value: Any, sanitizer: Any) -> Any:
    """The router's ``SANITIZE_PII`` pass: vetted values in, vetted values out.

    When :func:`should_redact` says the pass applies, ``value`` (a prompt or a
    message list) goes through ``sanitizer.asanitize`` exactly as it always
    has, unwrapped to plain ``str`` first. Every value that came in minted is
    re-minted from the sanitized text, with its source kept and ``redacted``
    set; a raw value comes out sanitized and still raw, so the provider
    boundary still reports it. When the pass does not apply, ``value`` is
    returned as it is.
    """
    if value is None or not should_redact(sanitizer):
        return value
    sanitized = await sanitizer.asanitize(plain(value))
    if isinstance(value, str):
        return _remint_text(value, sanitized)
    if isinstance(value, list) and isinstance(sanitized, list):
        return _remint_messages(value, sanitized)
    return sanitized


def _remint_text(original: Any, sanitized: Any) -> Any:
    found = provenance(original)
    if found is None or not isinstance(sanitized, str):
        return sanitized
    return _mint(sanitized, found[0], True)


def _remint_messages(original: List[Any], sanitized: List[Any]) -> List[Any]:
    if len(original) != len(sanitized):
        return sanitized
    out: List[Any] = []
    for before, after in zip(original, sanitized):
        if not (isinstance(before, dict) and isinstance(after, dict)):
            out.append(after)
            continue
        after = dict(after)
        if "content" in before and "content" in after:
            after["content"] = _remint_text(before["content"], after["content"])
        _remint_metadata(before, after)
        calls_before, calls_after = before.get("tool_calls"), after.get("tool_calls")
        if (
            isinstance(calls_before, list)
            and isinstance(calls_after, list)
            and len(calls_before) == len(calls_after)
        ):
            after["tool_calls"] = [
                _remint_call(call_before, call_after)
                for call_before, call_after in zip(calls_before, calls_after)
            ]
        out.append(after)
    return out


def _remint_metadata(before: Dict[str, Any], after: Dict[str, Any]) -> None:
    """Keep ``before``'s minted reasoning artifacts where the pass left them as
    they were (the sanitizer passes them through verbatim, #654)."""
    key = "provider_metadata"
    if key in before and key in after and _plain_leaves(before[key]) == after[key]:
        after[key] = before[key]


def _remint_call(before: Any, after: Any) -> Any:
    if not (isinstance(before, dict) and isinstance(after, dict)):
        return after
    after = dict(after)
    _remint_metadata(before, after)
    function_before, function_after = before.get("function"), after.get("function")
    if not (isinstance(function_before, dict) and isinstance(function_after, dict)):
        return after
    if "arguments" not in function_before or "arguments" not in function_after:
        return after
    return {
        **after,
        "function": {
            **function_after,
            "arguments": _remint_text(
                function_before["arguments"], function_after["arguments"]
            ),
        },
    }


# ---------------------------------------------------------------------------
# Reading minted values: unwrapping and the boundary's grammar
# ---------------------------------------------------------------------------


def plain_text(value: Any) -> Any:
    """``value`` as an exact ``str`` when it is any ``str``, else unchanged.

    ``str.__str__``, never ``str()``: ``str()`` of a ``(str, Enum)`` member is
    ``"Class.MEMBER"``, not its value, and would change what reaches the wire.
    """
    return str.__str__(value) if isinstance(value, str) else value


def plain_messages(messages: Any) -> Any:
    """``messages`` with every ``str`` subclass in the content channels unwrapped.

    The channels are ``content``, ``tool_calls[].function.arguments`` and the
    ``str`` leaves of ``provider_metadata`` (on a message and on a tool call).
    A list with nothing to unwrap is returned as the same object; otherwise the
    messages that change are copied and the input is never mutated.
    """
    if not isinstance(messages, list):
        return messages
    out: List[Any] = []
    changed = False
    for message in messages:
        new = _plain_message(message)
        changed = changed or new is not message
        out.append(new)
    return out if changed else messages


def plain(value: Any) -> Any:
    """A prompt or a message list, unwrapped (:func:`plain_text`, :func:`plain_messages`)."""
    return plain_messages(value) if isinstance(value, list) else plain_text(value)


def _plain_message(message: Any) -> Any:
    if not isinstance(message, dict):
        return message
    new = message
    content = message.get("content")
    if isinstance(content, str) and type(content) is not str:
        new = {**message, "content": str.__str__(content)}
    if "provider_metadata" in message:
        metadata = _plain_leaves(message["provider_metadata"])
        if metadata is not message["provider_metadata"]:
            new = {**new, "provider_metadata": metadata}
    calls = message.get("tool_calls")
    if isinstance(calls, list):
        plain_calls = [_plain_call(call) for call in calls]
        if any(a is not b for a, b in zip(plain_calls, calls)):
            new = {**new, "tool_calls": plain_calls}
    return new


def _plain_call(call: Any) -> Any:
    if not isinstance(call, dict):
        return call
    if "provider_metadata" in call:
        metadata = _plain_leaves(call["provider_metadata"])
        if metadata is not call["provider_metadata"]:
            call = {**call, "provider_metadata": metadata}
    if not isinstance(call.get("function"), dict):
        return call
    arguments = call["function"].get("arguments")
    if isinstance(arguments, str) and type(arguments) is not str:
        return {
            **call,
            "function": {**call["function"], "arguments": str.__str__(arguments)},
        }
    return call


def _plain_leaves(value: Any) -> Any:
    """``value`` with every ``str`` subclass in it (leaf or dict key) unwrapped;
    the same object if there is none."""
    if isinstance(value, str):
        return value if type(value) is str else str.__str__(value)
    if isinstance(value, dict):
        pairs = [(_plain_leaves(k), k, _plain_leaves(v), v) for k, v in value.items()]
        if all(pk is k and pv is v for pk, k, pv, v in pairs):
            return value
        return {pk: pv for pk, _, pv, _ in pairs}
    if isinstance(value, (list, tuple)):
        items = [_plain_leaves(item) for item in value]
        changed = any(a is not b for a, b in zip(items, value))
        return type(value)(items) if changed else value
    return value


class ContentViolation(NamedTuple):
    """One way a model call's content is not what the boundary accepts.

    ``argument`` and ``kind`` are bounded vocabularies (metric labels);
    ``where`` locates it for a log line. None of the three carries content.
    """

    argument: str
    kind: str
    where: str


#: Violation kinds.
RAW = "raw"  # a plain str where a minted value is required
FORGED = "forged"  # a ModelText this module never minted
NOT_TEXT = "not_text"  # neither str nor None where a minted value is required
BAD_SHAPE = "bad_shape"  # a container of the wrong type, or a tool call's type
BAD_ID = "bad_id"  # an identifier channel outside IDENTIFIER_RE
BAD_ROLE = "bad_role"  # a message role outside the grammar
UNKNOWN_KEY = "unknown_key"  # a key the grammar does not allow for that role
NOT_MODEL_OUTPUT = "not_model_output"  # arguments or metadata not from model_output
UNKNOWN_KWARG = "unknown_kwarg"  # a send keyword outside the closed set

#: The message grammar: the keys each role may carry. ``content`` is minted
#: text or ``None`` on every role. An assistant message may carry the model's
#: tool calls and its reasoning artifacts (``provider_metadata``, whose every
#: ``str``, leaf or key, is minted model output); a tool result names its call
#: (``tool_call_id``) and the function that produced it (``name``, which the
#: Gemini adapter needs for ``functionResponse.name``).
MESSAGE_KEYS = MappingProxyType(
    {
        "system": frozenset({"role", "content"}),
        "user": frozenset({"role", "content"}),
        "assistant": frozenset({"role", "content", "tool_calls", "provider_metadata"}),
        "tool": frozenset({"role", "content", "tool_call_id", "name"}),
    }
)
#: A tool call on an assistant message, and its function.
TOOL_CALL_KEYS = frozenset({"id", "type", "function", "provider_metadata"})
FUNCTION_KEYS = frozenset({"name", "arguments"})

#: The identifier channels — ``tool_call_id``, a tool result's ``name``, a tool
#: call's ``id`` and its ``function.name`` — are plain ``str`` that carry no
#: text: provider-issued call ids (``call_…``, ``toolu_…``, ``chatcmpl-tool-…``)
#: and tool names the code defines. This pattern keeps text out of them.
IDENTIFIER_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


def text_violation(value: Any, argument: str, where: str) -> Optional[ContentViolation]:
    """Why ``value`` is not minted text, or ``None`` when it is."""
    if provenance(value) is not None:
        return None
    if type(value) is ModelText:
        kind = FORGED
    elif isinstance(value, str):
        kind = RAW
    else:
        kind = NOT_TEXT
    return ContentViolation(argument, kind, where)


def key_names(keys: Any) -> str:
    """Key or keyword names for a log line: identifiers only, never a value."""
    names = sorted(
        key if isinstance(key, str) and key.isidentifier() and len(key) <= 40 else "?"
        for key in keys
    )
    return ",".join(names)


def message_violations(messages: Any) -> List[ContentViolation]:
    """Every way ``messages`` departs from the boundary's message grammar.

    The containers, every key and every role must be EXACT builtins (``list``,
    ``dict``, ``str``): a subclass can iterate, ``get`` or hash one way for
    this check and another for the adapter that reads the same object next
    (a ``list`` whose ``__iter__`` yields checked messages once, a role whose
    ``__hash__`` rewrites a message already checked). An exact builtin runs no
    caller code, so what was checked is what is sent.
    """
    if type(messages) is not list:
        return [ContentViolation("messages", BAD_SHAPE, "messages")]
    found: List[ContentViolation] = []
    for index, message in enumerate(messages):
        at = f"messages[{index}]"
        if type(message) is not dict or any(type(k) is not str for k in message):
            found.append(ContentViolation("messages", BAD_SHAPE, at))
            continue
        role = message.get("role")
        allowed = MESSAGE_KEYS.get(role) if type(role) is str else None
        if allowed is None:
            found.append(ContentViolation("messages.role", BAD_ROLE, at))
            continue
        extra = [key for key in message if key not in allowed]
        if extra:
            found.append(
                ContentViolation("messages", UNKNOWN_KEY, f"{at}:{key_names(extra)}")
            )
        content = message.get("content")
        if content is not None:
            violation = text_violation(content, "messages.content", f"{at}.content")
            if violation is not None:
                found.append(violation)
        if "tool_calls" in message and "tool_calls" in allowed:
            found.extend(_tool_call_violations(message["tool_calls"], at))
        if "provider_metadata" in message and "provider_metadata" in allowed:
            found.extend(
                _metadata_violations(
                    message["provider_metadata"],
                    "messages.provider_metadata",
                    f"{at}.provider_metadata",
                )
            )
        for key in ("tool_call_id", "name"):
            if key in message and key in allowed:
                violation = _identifier_violation(
                    message[key], f"messages.{key}", f"{at}.{key}"
                )
                if violation is not None:
                    found.append(violation)
    return found


def _identifier_violation(
    value: Any, argument: str, where: str
) -> Optional[ContentViolation]:
    if type(value) is str and IDENTIFIER_RE.fullmatch(value):
        return None
    return ContentViolation(argument, BAD_ID, where)


#: The leaf types a reasoning artifact may hold (exact types only).
_METADATA_LEAVES = (str, ModelText, int, float, bool, type(None))


def _metadata_violations(
    value: Any, argument: str, where: str
) -> List[ContentViolation]:
    """Every ``str`` in a reasoning artifact — leaf or dict key — must be minted
    model output.

    Only exact builtins are walked or accepted: ``dict``, ``list`` and
    ``tuple`` containers, and ``str``/``ModelText``, ``int``, ``float``,
    ``bool`` and ``None`` leaves. Anything else, a subclass of one of them
    included, is ``bad_shape``: its methods could answer this walk one way and
    the adapter's serialisation another. Types are compared by identity, so no
    metaclass runs either.

    Out of scope, as a deliberate encoding rather than a channel: text spelled
    as numbers (a list of code points).
    """
    found: List[ContentViolation] = []
    stack = [value]
    while stack:
        item = stack.pop()
        kind = type(item)
        if kind is dict:
            stack.extend(item.keys())
            stack.extend(item.values())
        elif kind is list or kind is tuple:
            stack.extend(item)
        elif not any(kind is leaf for leaf in _METADATA_LEAVES):
            found.append(ContentViolation(argument, BAD_SHAPE, where))
        elif kind is str or kind is ModelText:
            violation = text_violation(item, argument, where)
            if violation is not None:
                found.append(violation)
            elif provenance(item)[0] != SOURCE_MODEL_OUTPUT:
                found.append(ContentViolation(argument, NOT_MODEL_OUTPUT, where))
    return found


def _tool_call_violations(calls: Any, at: str) -> List[ContentViolation]:
    if type(calls) is not list:
        return [ContentViolation("messages.tool_calls", BAD_SHAPE, f"{at}.tool_calls")]
    found: List[ContentViolation] = []
    for index, call in enumerate(calls):
        where = f"{at}.tool_calls[{index}]"
        if type(call) is not dict or any(type(k) is not str for k in call):
            found.append(ContentViolation("messages.tool_calls", BAD_SHAPE, where))
            continue
        extra = [key for key in call if key not in TOOL_CALL_KEYS]
        if extra:
            found.append(
                ContentViolation(
                    "messages.tool_calls", UNKNOWN_KEY, f"{where}:{key_names(extra)}"
                )
            )
        if call.get("type") != "function":
            found.append(ContentViolation("messages.tool_calls", BAD_SHAPE, where))
        violation = _identifier_violation(
            call.get("id"), "messages.tool_calls.id", f"{where}.id"
        )
        if violation is not None:
            found.append(violation)
        if "provider_metadata" in call:
            found.extend(
                _metadata_violations(
                    call["provider_metadata"],
                    "messages.tool_calls.provider_metadata",
                    f"{where}.provider_metadata",
                )
            )
        function = call.get("function")
        if type(function) is not dict or any(type(k) is not str for k in function):
            found.append(
                ContentViolation("messages.tool_calls", BAD_SHAPE, f"{where}.function")
            )
            continue
        extra = [key for key in function if key not in FUNCTION_KEYS]
        if extra:
            found.append(
                ContentViolation(
                    "messages.tool_calls",
                    UNKNOWN_KEY,
                    f"{where}.function:{key_names(extra)}",
                )
            )
        violation = _identifier_violation(
            function.get("name"),
            "messages.tool_calls.function.name",
            f"{where}.function.name",
        )
        if violation is not None:
            found.append(violation)
        arguments = function.get("arguments")
        argument = "messages.tool_calls.function.arguments"
        violation = text_violation(arguments, argument, f"{where}.function.arguments")
        if violation is not None:
            found.append(violation)
        elif provenance(arguments)[0] != SOURCE_MODEL_OUTPUT:
            found.append(
                ContentViolation(
                    argument, NOT_MODEL_OUTPUT, f"{where}.function.arguments"
                )
            )
    return found
