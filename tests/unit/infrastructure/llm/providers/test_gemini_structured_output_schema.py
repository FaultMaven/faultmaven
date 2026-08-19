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

from typing import List, Literal, Optional, Union
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel, Field

from faultmaven.infrastructure.llm.providers.base import (
    ProviderConfig,
    StructuredOutputCapability,
)
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
def test_single_value_literal_keeps_its_constraint(provider):
    """A one-member ``Literal`` must still constrain the model's output.

    Gemini rejects ``const``, so the sanitizer drops it — but dropping it
    alone throws the constraint away. Pydantic < 2.10 hid that: a
    single-value ``Literal`` emitted ``const`` AND a redundant
    ``enum: [x]``, so the enum survived the strip. Pydantic >= 2.10 emits
    ``const`` alone, which is why the sanitizer rewrites it rather than
    merely removing it.
    """

    class _Tagged(BaseModel):
        state: Literal["inquiry"]
        level: Literal["low", "high"]
        free: str

    raw = _Tagged.model_json_schema()
    # Sanity: this pydantic really does emit a bare const for the one-member
    # Literal. If a future pydantic reinstates the redundant enum, this
    # assertion fails loudly rather than making the test vacuous.
    assert raw["properties"]["state"].get("const") == "inquiry"
    assert "enum" not in raw["properties"]["state"]

    resolved = provider._resolve_refs_for_gemini(raw)

    _assert_no_unsupported_keys(resolved)
    # The constraint survives, expressed the way Gemini accepts it.
    assert resolved["properties"]["state"]["enum"] == ["inquiry"]
    assert "const" not in resolved["properties"]["state"]
    # A multi-member Literal is untouched, and a plain string gains nothing.
    assert resolved["properties"]["level"]["enum"] == ["low", "high"]
    assert "enum" not in resolved["properties"]["free"]


@pytest.mark.unit
def test_const_rewrite_does_not_override_an_existing_enum(provider):
    """Where both keys are present, the enum is authoritative."""
    resolved = provider._resolve_refs_for_gemini(
        {
            "type": "object",
            "properties": {
                "x": {"type": "string", "const": "a", "enum": ["a", "b"]},
            },
        }
    )

    assert resolved["properties"]["x"]["enum"] == ["a", "b"]
    assert "const" not in resolved["properties"]["x"]


@pytest.mark.unit
def test_nested_single_value_literal_keeps_its_constraint(provider):
    """The rewrite applies at every depth, not just at the top level."""

    class _Leaf(BaseModel):
        kind: Literal["leaf"]

    class _Root(BaseModel):
        leaf: _Leaf
        leaves: List[_Leaf]

    resolved = provider._resolve_refs_for_gemini(_Root.model_json_schema())

    _assert_no_unsupported_keys(resolved)
    assert resolved["properties"]["leaf"]["properties"]["kind"]["enum"] == ["leaf"]
    assert resolved["properties"]["leaves"]["items"]["properties"]["kind"]["enum"] == [
        "leaf"
    ]


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


# --- response-schema capacity (the axis that decides whether a schema is
# --- ACCEPTED at all, not how it is enforced) --------------------------------


class TestSchemaCapacity:
    """gemini-2.5-flash advertises STRICT and honours it for small schemas, then
    rejects the engine's DIAGNOSIS schema with 400 'too many states for serving'
    (measured 6/6 on 2026-07-30). The two axes must therefore disagree for it."""

    @staticmethod
    def _provider(default_model, models=None):
        """Real ProviderConfig — capacity resolution runs through
        get_effective_model, whose fallback rules are part of the behaviour."""
        return GeminiProvider(
            ProviderConfig(
                name="gemini",
                api_key="test-key",
                base_url="https://generativelanguage.googleapis.com/v1beta",
                models=list(models if models is not None else [default_model]),
                default_model=default_model,
                timeout=30,
                confidence_score=0.9,
            )
        )

    def test_denylisted_model_cannot_serve_engine_schemas(self):
        p = self._provider("gemini-2.5-flash")
        assert p.supports_engine_response_schemas("gemini-2.5-flash") is False

    def test_documented_default_can_serve_engine_schemas(self):
        p = self._provider("gemini-3.5-flash")
        assert p.supports_engine_response_schemas("gemini-3.5-flash") is True

    def test_falls_back_to_configured_default_when_no_model_requested(self):
        assert (
            self._provider("gemini-2.5-flash").supports_engine_response_schemas()
            is False
        )
        assert (
            self._provider("gemini-3.5-flash").supports_engine_response_schemas()
            is True
        )

    def test_judges_the_model_generate_would_actually_send(self):
        """The probe must resolve the model exactly as ``generate()`` does —
        ``model or config.default_model``, verbatim.

        ``get_effective_model`` (the obvious-looking helper) collapses anything
        outside ``config.models`` to the default, so using it let the gate clear a
        capable DEFAULT while the request went out with the denylisted model that
        was actually asked for. Both directions are pinned so the resolution can't
        drift back."""
        # Capable default, denylisted model requested and NOT in config.models:
        # generate() sends the requested one, so capacity must be False.
        p = self._provider("gemini-3.5-flash", models=["gemini-3.5-flash"])
        assert p.supports_engine_response_schemas("gemini-2.5-flash") is False
        # Converse: denylisted default, capable model requested — generate() sends
        # the requested one, so capacity must be True.
        p = self._provider("gemini-2.5-flash", models=["gemini-2.5-flash"])
        assert p.supports_engine_response_schemas("gemini-3.5-flash") is True

    @pytest.mark.parametrize(
        "variant",
        [
            "gemini-2.5-flash-002",
            "gemini-2.5-flash-preview-09-2025",
            "gemini-2.5-flash-latest",
            "gemini-2.5-flash-lite",
            "GEMINI-2.5-FLASH",  # ids are compared case-insensitively
        ],
    )
    def test_denylist_covers_variant_ids_of_a_measured_family(self, variant):
        """GEMINI_MODEL is free-form and dated/aliased/tier variants share the
        constrained-decoding backend, so an exact-id match would let
        `gemini-2.5-flash-002` walk straight past the gate."""
        p = self._provider(variant, models=[variant])
        assert p.supports_engine_response_schemas(variant) is False

    def test_denylist_does_not_over_match_other_families(self):
        """Prefix matching must not swallow unrelated families."""
        for capable in ("gemini-3.5-flash", "gemini-2.5-pro", "gemini-1.5-flash"):
            p = self._provider(capable, models=[capable])
            assert p.supports_engine_response_schemas(capable) is True, capable

    def test_missing_model_configuration_is_not_a_false_clear(self):
        """No model at all resolves to an empty string, which must not be treated
        as a denylisted match nor crash — the credential/config gates own that."""
        p = self._provider("", models=[])
        assert p.supports_engine_response_schemas(None) is True

    def test_capacity_is_independent_of_enforcement_capability(self):
        """The denylisted model still reports STRICT — capacity is a separate
        axis, so one 'is it capable' question cannot express both."""
        p = self._provider("gemini-2.5-flash")
        assert (
            p.get_structured_output_capability("gemini-2.5-flash")
            == StructuredOutputCapability.STRICT
        )
        assert p.supports_engine_response_schemas("gemini-2.5-flash") is False

    def test_unlisted_models_are_assumed_capable(self):
        """Default-open: only measured models are denied, so an unmeasured or
        future model is never refused on speculation."""
        p = self._provider("gemini-9.9-someday")
        assert p.supports_engine_response_schemas("gemini-9.9-someday") is True
