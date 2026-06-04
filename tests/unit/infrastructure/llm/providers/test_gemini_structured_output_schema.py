"""Gemini structured-output (`response_schema`) must resolve Pydantic $ref/$defs.

Regression guard for the bug where the structured-output path passed the raw
Pydantic JSON schema to Gemini's ``response_schema``. Gemini's schema dialect
(an OpenAPI-3.0 subset) rejects ``$ref``/``$defs``/``anyOf``/``oneOf``/
``additionalProperties`` — exactly what ``model_json_schema()`` emits for
nested / ``Optional`` / ``Union`` models — yielding a 400 (non-retryable) that
surfaced as an intermittent 500 on milestone turns with complex schemas.

The tool-calling path already inlined refs via ``_resolve_refs_for_gemini``;
the fix reuses that helper on the ``response_schema`` path. These tests pin:
  1. the helper recursively removes all unsupported keys from a realistic
     nested+Optional+Union Pydantic schema (and maps Optional -> nullable);
  2. a root ``$ref`` wrapping ``$defs`` (Pydantic sometimes emits this) resolves;
  3. the live ``generate()`` wiring actually applies the helper to
     ``response_schema`` (guards the one-line fix, not just the helper).
"""

from typing import List, Optional, Union
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel, Field

from faultmaven.infrastructure.llm.providers.base import ProviderConfig
from faultmaven.infrastructure.llm.providers.gemini import GeminiProvider

_UNSUPPORTED_KEYS = {
    "$ref",
    "$defs",
    "anyOf",
    "oneOf",
    "additionalProperties",
    "$schema",
}


def _assert_no_unsupported_keys(node, path="root"):
    """Recursively assert none of Gemini's unsupported schema keys remain."""
    if isinstance(node, dict):
        for key in _UNSUPPORTED_KEYS:
            assert key not in node, f"unsupported key {key!r} survived at {path}"
        for k, v in node.items():
            _assert_no_unsupported_keys(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, item in enumerate(node):
            _assert_no_unsupported_keys(item, f"{path}[{i}]")


# --- Realistic schemas: nested model + Optional + Union + List ---------------


class _Inner(BaseModel):
    label: str
    score: Optional[float] = None  # Optional scalar -> anyOf


class _Outer(BaseModel):
    name: str
    inner: _Inner  # nested model -> $ref/$defs
    maybe_inner: Optional[_Inner] = None  # Optional nested -> anyOf + $ref
    tags: List[str] = Field(default_factory=list)
    either: Union[int, str] = 0  # Union -> anyOf
    note: Optional[str] = None


@pytest.fixture
def provider():
    return GeminiProvider(
        ProviderConfig(
            name="gemini",
            api_key="test-key",
            base_url="https://generativelanguage.googleapis.com/v1beta",
            models=["gemini-2.5-pro"],
            default_model="gemini-2.5-pro",
            timeout=30,
            confidence_score=0.9,
        )
    )


def _mock_aiohttp_session(response_data: dict):
    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value=response_data)
    mock_response.text = AsyncMock(return_value="")
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)
    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_response)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session


@pytest.mark.unit
def test_resolve_refs_strips_all_unsupported_keys_from_pydantic_schema(provider):
    """A nested+Optional+Union Pydantic schema resolves to Gemini-safe schema."""
    raw = _Outer.model_json_schema()
    # Sanity: the raw Pydantic schema DOES contain the offending constructs.
    assert "$defs" in raw

    resolved = provider._resolve_refs_for_gemini(raw)

    _assert_no_unsupported_keys(resolved)
    # Nested model was inlined to an object with its properties.
    assert resolved["properties"]["inner"]["type"] == "object"
    assert "label" in resolved["properties"]["inner"]["properties"]
    # Optional fields became nullable (not anyOf).
    assert resolved["properties"]["note"].get("nullable") is True
    assert resolved["properties"]["maybe_inner"].get("nullable") is True


@pytest.mark.unit
def test_resolve_refs_handles_root_ref_wrapping_defs(provider):
    """Pydantic sometimes wraps the root model in $ref -> $defs; resolve it."""
    schema = {
        "$ref": "#/$defs/Root",
        "$defs": {
            "Root": {
                "type": "object",
                "properties": {"x": {"type": "string", "title": "X"}},
                "additionalProperties": False,
                "required": ["x"],
            }
        },
    }
    resolved = provider._resolve_refs_for_gemini(schema)

    _assert_no_unsupported_keys(resolved)
    assert resolved["type"] == "object"
    assert resolved["properties"]["x"]["type"] == "string"


@pytest.mark.unit
async def test_generate_applies_ref_resolution_to_response_schema(provider):
    """The live structured-output path must send a Gemini-safe response_schema.

    Guards the one-line fix itself: without ref-resolution Gemini returns 400
    on this payload (nested+Optional+Union) and the turn 500s.
    """
    mock_resp = {
        "candidates": [
            {"content": {"parts": [{"text": "{}"}]}, "finishReason": "STOP"}
        ],
        "usageMetadata": {"candidatesTokenCount": 5},
    }
    mock_session = _mock_aiohttp_session(mock_resp)

    response_format = {
        "type": "json_schema",
        "json_schema": {"name": "Outer", "schema": _Outer.model_json_schema()},
    }

    with patch("aiohttp.ClientSession", return_value=mock_session):
        await provider.generate("Test", response_format=response_format)

    call_kwargs = mock_session.post.call_args
    request_body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
    sent_schema = request_body["generationConfig"]["response_schema"]

    _assert_no_unsupported_keys(sent_schema)
    assert request_body["generationConfig"]["response_mime_type"] == "application/json"
