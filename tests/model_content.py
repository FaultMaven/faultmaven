"""The test minter: model-bound values for tests, without a redaction pass (#1998).

The one place outside ``faultmaven/infrastructure/security/model_content.py``
allowed to reach the minting internals
(``tests/unit/architecture/test_model_text_mint_lexical_1998.py``). A test that
drives a provider or the router builds its content here, synchronously, as if
it had passed a disabled case context — the posture of CI's Standalone
profile. A test of redaction itself uses the real minting functions instead.

``forged`` is here for the same reason: a boundary test needs a ``ModelText``
the module never minted, and building one is a spelling no other file may use.
"""

from __future__ import annotations

from typing import Any, Dict, List

from faultmaven.infrastructure.security.case_redaction import CaseRedactionContext
from faultmaven.infrastructure.security.model_content import (
    SOURCE_CASE,
    SOURCE_MODEL_OUTPUT,
    ModelText,
    _mint,
)


def disabled_redaction(
    case_id: str = "case-test-model-content",
) -> CaseRedactionContext:
    """A case redaction context that redacts nothing, as ``SANITIZE_PII=false`` builds."""
    return CaseRedactionContext(case_id=case_id, sanitizer=None, enabled=False)


def vetted(text: str, source: str = SOURCE_CASE) -> ModelText:
    """``text`` minted as if through a disabled case context (``redacted`` False)."""
    if not isinstance(text, str):
        raise TypeError(f"vetted takes a str, not {type(text).__name__}")
    return _mint(text, source, False)


def vetted_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Copies of ``messages`` with every content channel minted.

    ``content`` is minted as case content, or as model output on an assistant
    message; tool-call ``arguments`` as model output. Every other key is copied
    as it is, so a test can still hand the boundary a grammar violation.
    """
    out = []
    for message in messages:
        new = dict(message)
        content = new.get("content")
        if isinstance(content, str):
            source = (
                SOURCE_MODEL_OUTPUT if new.get("role") == "assistant" else SOURCE_CASE
            )
            new["content"] = vetted(content, source)
        calls = new.get("tool_calls")
        if isinstance(calls, list):
            new["tool_calls"] = [_vetted_call(call) for call in calls]
        out.append(new)
    return out


def _vetted_call(call: Any) -> Any:
    if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
        return call
    function = dict(call["function"])
    if isinstance(function.get("arguments"), str):
        function["arguments"] = vetted(function["arguments"], SOURCE_MODEL_OUTPUT)
    return {**call, "function": function}


def forged(text: str) -> ModelText:
    """A ``ModelText`` built without the token: its slots were never set."""
    return str.__new__(ModelText, text)
