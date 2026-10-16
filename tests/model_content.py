"""The test minter: model-bound values for tests, without a redaction pass (#1998).

The one place outside ``faultmaven/infrastructure/security/model_content.py``
allowed to reach the minting internals
(``tests/unit/architecture/test_model_text_mint_lexical_1998.py``). A test that
drives a provider or the router builds its content here, synchronously, as if
it had passed a disabled case context — the posture of CI's Standalone
profile. A test of redaction itself uses the real minting functions instead.

``forged`` is here for the same reason: a boundary test needs a ``ModelText``
the module never minted, and building one is a spelling no other file may use.

No private minting name is bound at module scope: ``vetted`` imports the
mint inside itself, after its check. Every minter here — ``vetted``,
``vetted_messages``, ``vetted_kwargs``, ``forged`` and
``disabled_redaction`` (a context that redacts nothing lets ``case_redacted``
mint raw text) — refuses to run outside pytest (``_test_runs_only``): rule T of
``test_model_dispatch_lexical_1998.py`` flags an import of ``tests`` from
production code, and this guard stops the spellings no lexical rule sees
(``importlib.import_module(".model_content", "tests")``,
``pkgutil.resolve_name``, ``runpy.run_path``, ``spec_from_file_location``).

:class:`FakeProvider` is the shared concrete provider double: a real
``BaseLLMProvider`` whose ``_generate`` records each call and answers from a
script. A test that sends to it goes through
``faultmaven.infrastructure.llm.dispatch.send_to_model``, so the model
boundary runs exactly as it does for a shipped adapter.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from faultmaven.infrastructure.llm.providers.base import (
    BaseLLMProvider,
    LLMResponse,
    ProviderConfig,
)
from faultmaven.infrastructure.security.case_redaction import CaseRedactionContext
from faultmaven.infrastructure.security.model_content import (
    SOURCE_CASE,
    SOURCE_MODEL_OUTPUT,
    ModelText,
)


def _test_runs_only() -> None:
    """These helpers mint without a redaction pass: they run under pytest only."""
    import sys

    if "_pytest" not in sys.modules:
        raise RuntimeError(
            "tests.model_content mints model-bound content without redaction; "
            "it runs under pytest only (#1998)"
        )


def disabled_redaction(
    case_id: str = "case-test-model-content",
) -> CaseRedactionContext:
    """A case redaction context that redacts nothing, as ``SANITIZE_PII=false`` builds."""
    _test_runs_only()
    return CaseRedactionContext(case_id=case_id, sanitizer=None, enabled=False)


def vetted(text: str, source: str = SOURCE_CASE) -> ModelText:
    """``text`` minted as if through a disabled case context (``redacted`` False)."""
    _test_runs_only()
    # Imported here, after the check, never bound at module scope: a module
    # attribute would be reachable without calling a guarded helper
    # (``pkgutil.resolve_name("tests.model_content:_mint")``).
    from faultmaven.infrastructure.security.model_content import _mint

    if not isinstance(text, str):
        raise TypeError(f"vetted takes a str, not {type(text).__name__}")
    return _mint(str.__str__(text), source, False)


def vetted_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Copies of ``messages`` with every content channel minted.

    ``content`` is minted as case content, or as model output on an assistant
    message; tool-call ``arguments`` and every ``str`` (leaf or key) of
    ``provider_metadata`` (on a message or a tool call) as model output, as
    ``model_output`` mints them. Every other key is copied as it is, so a test
    can still hand the boundary a grammar violation.
    """
    _test_runs_only()
    out = []
    for message in messages:
        new = dict(message)
        content = new.get("content")
        if isinstance(content, str):
            source = (
                SOURCE_MODEL_OUTPUT if new.get("role") == "assistant" else SOURCE_CASE
            )
            new["content"] = vetted(content, source)
        if "provider_metadata" in new:
            new["provider_metadata"] = _vetted_leaves(new["provider_metadata"])
        calls = new.get("tool_calls")
        if isinstance(calls, list):
            new["tool_calls"] = [_vetted_call(call) for call in calls]
        out.append(new)
    return out


def vetted_kwargs(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """A copy of a call's keywords with every content channel minted.

    ``prompt``, ``system`` and ``preamble`` when they are a ``str``;
    ``messages`` through :func:`vetted_messages`; each ``str`` element of
    ``stop_sequences``. Everything else is copied as it is — a value that is
    not text, a keyword no adapter reads — so a test can still hand the
    boundary a violation on purpose (#1998 P2b).
    """
    _test_runs_only()
    out = dict(kwargs)
    for key in ("prompt", "system", "preamble"):
        if isinstance(out.get(key), str):
            out[key] = vetted(out[key])
    if isinstance(out.get("messages"), list):
        out["messages"] = vetted_messages(out["messages"])
    stops = out.get("stop_sequences")
    if isinstance(stops, (list, tuple)):
        out["stop_sequences"] = type(stops)(
            vetted(item) if isinstance(item, str) else item for item in stops
        )
    return out


def _vetted_leaves(value: Any) -> Any:
    if isinstance(value, str):
        return vetted(value, SOURCE_MODEL_OUTPUT)
    if isinstance(value, dict):
        return {
            _vetted_leaves(key): _vetted_leaves(item) for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return type(value)(_vetted_leaves(item) for item in value)
    return value


def _vetted_call(call: Any) -> Any:
    if isinstance(call, dict) and "provider_metadata" in call:
        call = {**call, "provider_metadata": _vetted_leaves(call["provider_metadata"])}
    if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
        return call
    function = dict(call["function"])
    if isinstance(function.get("arguments"), str):
        function["arguments"] = vetted(function["arguments"], SOURCE_MODEL_OUTPUT)
    return {**call, "function": function}


def forged(text: str) -> ModelText:
    """A ``ModelText`` built without the token: its slots were never set."""
    _test_runs_only()
    return str.__new__(ModelText, text)


def fake_response(content: str = "ok", **fields: Any) -> LLMResponse:
    """An ``LLMResponse`` as a provider would return it, ``content`` and all."""
    values: Dict[str, Any] = {
        "content": content,
        "confidence": 1.0,
        "provider": "fake",
        "model": "fake-model",
        "tokens_used": 1,
        "response_time_ms": 1,
    }
    values.update(fields)
    return LLMResponse(**values)


class FakeProvider(BaseLLMProvider):
    """A concrete provider whose ``_generate`` records every call (#1998).

    ``responses`` is the script: each call takes the next item, raising it if
    it is an exception; past its end every call answers ``fake_response()``.
    ``calls`` holds each call's keywords as ``_generate`` received them — the
    plain values the dispatch hands an adapter.
    """

    def __init__(
        self,
        responses: Optional[Iterable[Any]] = None,
        *,
        name: str = "fake",
        models: Optional[List[str]] = None,
    ) -> None:
        models = list(models or ["fake-model"])
        super().__init__(
            ProviderConfig(
                name=name,
                api_key="test-key-not-real",
                models=models,
                default_model=models[0],
            )
        )
        self._name = name
        self._script = list(responses or [])
        self.calls: List[Dict[str, Any]] = []

    @property
    def provider_name(self) -> str:
        return self._name

    def is_available(self) -> bool:
        return True

    def get_supported_models(self) -> List[str]:
        return list(self.config.models)

    async def _generate(
        self,
        prompt: Optional[str] = None,
        model: Optional[str] = None,
        max_tokens: int = 1000,
        temperature: float = 0.7,
        **kwargs: Any,
    ) -> LLMResponse:
        self.calls.append(
            {
                "prompt": prompt,
                "model": model,
                "max_tokens": max_tokens,
                "temperature": temperature,
                **kwargs,
            }
        )
        if self._script:
            item = self._script.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        return fake_response()
