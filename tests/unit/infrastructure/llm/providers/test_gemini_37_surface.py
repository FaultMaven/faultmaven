"""Gemini 3.7+ API-surface migration — version-gated request shape.

Starting at gemini-3.7-*, the Gemini API removed the classic surface the
adapter grew up on (measured live 2026-08-26, and per Google's 3.7 migration
guidance):

  - ``temperature`` / ``topP`` / ``topK`` are gone — omitted from 3.7+
    requests entirely;
  - ``thinkingLevel`` becomes the ONLY reasoning knob, defaulting to "medium"
    on the server, and this adapter pins the lowest accepted level ("low";
    "minimal" is rejected) on EVERY 3.7+ call shape — product requirement is
    little/no reasoning, and thinking tokens bill at the full output rate;
  - prefilled (trailing) model turns are not supported (400 measured);
  - ``candidateCount`` is unsupported (400 measured; the adapter never sent
    it — pinned here so it stays that way).

The functionResponse shape changed one generation EARLIER, at 3.6 (measured
2026-08-26): the classic ``role: "function"`` turn is rejected there ("Role
'function' is not supported…"), while a ``role: "user"`` turn round-trips —
and the API issues ``functionCall.id`` (from 3.5 already), which 3.6 accepts
echoed on the functionResponse and the 3.7 guide makes mandatory. The
adapter therefore has TWO gates: the FR shape (user role + id echo) at
``_uses_36_function_response_surface`` (>= (3, 6)), and everything else at
``_uses_37_api_surface`` (>= (3, 7)). gemini-3.5-* requests stay
byte-for-byte what they were — the full classic shape (role "function",
temperature/topP/topK, no ids) measured working end-to-end on 2026-08-26.
"""

import json
from unittest.mock import patch

import pytest
from pydantic import BaseModel

from faultmaven.infrastructure.llm.providers.base import (
    ProviderConfig,
    ReasoningIntent,
)
from faultmaven.infrastructure.llm.providers.gemini import GeminiProvider


def _config(model):
    return ProviderConfig(
        name="gemini",
        api_key="test-key",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        models=[model],
        default_model=model,
        timeout=30,
        confidence_score=0.9,
    )


_OK_RESP = {
    "candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}],
    "usageMetadata": {"candidatesTokenCount": 5},
}


class _Schema(BaseModel):
    answer: str


def _response_format():
    return {
        "type": "json_schema",
        "json_schema": {"name": "Schema", "schema": _Schema.model_json_schema()},
    }


async def _sent_body(mock_factory, provider, **generate_kwargs):
    """*mock_factory* is the shared ``mock_aiohttp_session`` conftest fixture
    (tests/unit/infrastructure/llm/conftest.py) — never a local copy."""
    mock_session = mock_factory(_OK_RESP)
    with patch("aiohttp.ClientSession", return_value=mock_session):
        await provider.generate("Test", **generate_kwargs)
    call_kwargs = mock_session.post.call_args
    return call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")


# --- version gate ------------------------------------------------------------


@pytest.mark.unit
class TestSurfaceGate:
    @pytest.mark.parametrize(
        "model,expected",
        [
            ("gemini-3.5-flash", False),
            ("gemini-3.5-flash-lite", False),
            ("gemini-3.6-flash", False),
            ("gemini-3.7-flash", True),
            ("GEMINI-3.7-FLASH", True),  # case-insensitive like the 3.x gate
            ("gemini-3.8-pro", True),
            ("gemini-4.0-flash", True),  # new baseline going forward, not ==3.7
            ("gemini-1.5-pro", False),
            ("gemini-2.5-flash", False),
            ("not-a-gemini-model", False),
        ],
    )
    def test_gate_by_model_id(self, model, expected):
        assert GeminiProvider._uses_37_api_surface(model) is expected

    def test_minor_version_compares_numerically_not_lexically(self):
        # A hypothetical gemini-3.10 is NEWER than 3.7; a string compare
        # ("10" < "7") would demote it back to the legacy surface.
        assert GeminiProvider._uses_37_api_surface("gemini-3.10-flash") is True

    @pytest.mark.parametrize(
        "model,expected",
        [
            ("gemini-3.5-flash", False),
            ("gemini-3.5-flash-lite", False),
            ("gemini-3.6-flash", True),  # role vocabulary changed HERE
            ("gemini-3.7-flash", True),
            ("gemini-4.0-flash", True),
            ("gemini-2.5-flash", False),
            (None, False),  # legacy callers pass no model
        ],
    )
    def test_function_response_surface_gate(self, model, expected):
        assert GeminiProvider._uses_36_function_response_surface(model) is expected


# --- sampling parameters -----------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
class TestSamplingParams:
    async def test_37_omits_temperature_top_p_top_k(self, mock_aiohttp_session):
        provider = GeminiProvider(_config("gemini-3.7-flash"))
        body = await _sent_body(
            mock_aiohttp_session,
            provider,
            max_tokens=500,
            temperature=0.7,
            top_p=0.9,
            top_k=40,
        )
        gen = body["generationConfig"]
        assert "temperature" not in gen
        assert "topP" not in gen
        assert "topK" not in gen
        assert gen["maxOutputTokens"] == 500

    async def test_37_still_sends_stop_sequences(self, mock_aiohttp_session):
        provider = GeminiProvider(_config("gemini-3.7-flash"))
        body = await _sent_body(
            mock_aiohttp_session, provider, max_tokens=500, stop_sequences=["END"]
        )
        assert body["generationConfig"]["stopSequences"] == ["END"]

    @pytest.mark.parametrize("model", ["gemini-3.5-flash", "gemini-3.6-flash"])
    async def test_pre_37_request_unchanged(self, model, mock_aiohttp_session):
        """3.5/3.6 accept the classic params (measured 2026-08-26) and must
        keep receiving the exact pre-migration generationConfig."""
        provider = GeminiProvider(_config(model))
        body = await _sent_body(
            mock_aiohttp_session,
            provider,
            max_tokens=500,
            temperature=0.7,
            top_p=0.9,
            top_k=40,
        )
        gen = body["generationConfig"]
        assert gen["temperature"] == 0.7
        assert gen["topP"] == 0.9
        assert gen["topK"] == 40
        # Field order is part of "byte-for-byte": temperature first, exactly
        # as the adapter always built it.
        assert list(gen)[:2] == ["temperature", "maxOutputTokens"]

    @pytest.mark.parametrize("model", ["gemini-3.5-flash", "gemini-3.7-flash"])
    async def test_candidate_count_never_sent(self, model, mock_aiohttp_session):
        """The adapter has never sent candidateCount; 3.7 removed it — pin the
        absence on both surfaces."""
        provider = GeminiProvider(_config(model))
        body = await _sent_body(mock_aiohttp_session, provider, max_tokens=500)
        assert "candidateCount" not in body["generationConfig"]

    async def test_drop_note_logged_once_per_instance(
        self, mock_aiohttp_session, caplog
    ):
        provider = GeminiProvider(_config("gemini-3.7-flash"))
        with caplog.at_level("INFO"):
            await _sent_body(
                mock_aiohttp_session, provider, max_tokens=500, temperature=0.3
            )
            await _sent_body(
                mock_aiohttp_session, provider, max_tokens=500, temperature=0.3
            )
        notes = [r for r in caplog.records if "sampling parameters" in r.message]
        assert len(notes) == 1


# --- thinkingLevel default ---------------------------------------------------


@pytest.mark.unit
class TestThinkingLevel37:
    def test_37_plain_call_capped_low(self):
        """The widened 3.7+ shape default: EVERY call is capped, plain chat
        included — little/no reasoning is the product requirement, and
        thinking bills at the output rate."""
        provider = GeminiProvider(_config("gemini-3.7-flash"))
        assert provider._structured_thinking_config("gemini-3.7-flash", False) == {
            "thinkingLevel": "low"
        }

    def test_37_structured_call_capped_low(self):
        provider = GeminiProvider(_config("gemini-3.7-flash"))
        assert provider._structured_thinking_config("gemini-3.7-flash", True) == {
            "thinkingLevel": "low"
        }

    def test_35_plain_call_still_uncapped(self):
        """Regression: the widened default is 3.7+-only. 3.5 plain calls keep
        native dynamic thinking (no thinkingConfig at all)."""
        provider = GeminiProvider(_config("gemini-3.5-flash"))
        assert provider._structured_thinking_config("gemini-3.5-flash", False) is None

    def test_37_inference_with_floor_lifts_cap_both_shapes(self):
        provider = GeminiProvider(_config("gemini-3.7-flash"))
        for structured in (True, False):
            assert (
                provider._structured_thinking_config(
                    "gemini-3.7-flash",
                    structured,
                    intent=ReasoningIntent.INFERENCE,
                    has_output_floor=True,
                )
                is None
            )

    def test_37_inference_no_floor_structured_fails_closed(self, caplog):
        """Same guard as 3.x: a starved structured body is unusable, so the
        floor stays the price of lifting the cap."""
        provider = GeminiProvider(_config("gemini-3.7-flash"))
        with caplog.at_level("WARNING"):
            result = provider._structured_thinking_config(
                "gemini-3.7-flash",
                True,
                intent=ReasoningIntent.INFERENCE,
                has_output_floor=False,
            )
        assert result == {"thinkingLevel": "low"}
        assert any("REFUSED" in r.message for r in caplog.records)

    def test_37_inference_no_floor_plain_lifts(self):
        """Plain-call starvation is non-fatal (partial prose returns, flagged)
        — the caller's explicit request for reasoning is honoured."""
        provider = GeminiProvider(_config("gemini-3.7-flash"))
        assert (
            provider._structured_thinking_config(
                "gemini-3.7-flash",
                False,
                intent=ReasoningIntent.INFERENCE,
                has_output_floor=False,
            )
            is None
        )

    def test_37_extraction_capped_both_shapes(self):
        provider = GeminiProvider(_config("gemini-3.7-flash"))
        for structured in (True, False):
            assert provider._structured_thinking_config(
                "gemini-3.7-flash", structured, intent=ReasoningIntent.EXTRACTION
            ) == {"thinkingLevel": "low"}


@pytest.mark.unit
@pytest.mark.asyncio
class TestThinkingLevel37Wiring:
    async def test_37_plain_generate_sends_thinking_level_low(
        self, mock_aiohttp_session
    ):
        provider = GeminiProvider(_config("gemini-3.7-flash"))
        body = await _sent_body(mock_aiohttp_session, provider, max_tokens=500)
        assert body["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "low"}

    async def test_35_plain_generate_sends_no_thinking_config(
        self, mock_aiohttp_session
    ):
        provider = GeminiProvider(_config("gemini-3.5-flash"))
        body = await _sent_body(mock_aiohttp_session, provider, max_tokens=500)
        assert "thinkingConfig" not in body["generationConfig"]

    async def test_37_structured_generate_sends_thinking_level_low(
        self, mock_aiohttp_session
    ):
        provider = GeminiProvider(_config("gemini-3.7-flash"))
        body = await _sent_body(
            mock_aiohttp_session,
            provider,
            max_tokens=500,
            response_format=_response_format(),
        )
        assert body["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "low"}


# --- functionResponse id + name ---------------------------------------------


_TOOL_LOOP_MESSAGES = [
    {"role": "user", "content": "check the weather"},
    {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "fc-abc123",
                "type": "function",
                "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'},
            }
        ],
    },
    {
        "role": "tool",
        "tool_call_id": "fc-abc123",
        "name": "get_weather",
        "content": '{"result": "22C"}',
    },
]


@pytest.mark.unit
class TestFunctionResponseShape:
    @pytest.mark.parametrize("model", ["gemini-3.6-flash", "gemini-3.7-flash"])
    def test_36_plus_fr_turn_is_user_role_with_id_and_name(self, model):
        """3.6 rejects role "function" outright (400 'Role function is not
        supported'); the FR turn must be role "user" and carry id+name."""
        provider = GeminiProvider(_config(model))
        result = provider._convert_messages_to_gemini(_TOOL_LOOP_MESSAGES, model=model)
        fn_turn = result["contents"][-1]
        assert fn_turn["role"] == "user"
        fr = fn_turn["parts"][0]["functionResponse"]
        assert fr["id"] == "fc-abc123"
        assert fr["name"] == "get_weather"
        assert fr["response"] == {"result": "22C"}

    def test_36_plus_rebuilt_function_call_carries_matching_id(self):
        """The fabricated model turn and the functionResponse must be a
        matched pair — the whole history is client-authored, so internal
        consistency is the contract."""
        provider = GeminiProvider(_config("gemini-3.7-flash"))
        result = provider._convert_messages_to_gemini(
            _TOOL_LOOP_MESSAGES, model="gemini-3.7-flash"
        )
        model_turn = result["contents"][1]
        fc = model_turn["parts"][0]["functionCall"]
        assert fc["id"] == "fc-abc123"

    def test_pre_36_shape_unchanged_function_role_no_ids(self):
        """Regression: the default (and every 3.5-generation call) emits
        exactly the old shape — role "function", no id on functionCall or
        functionResponse. Measured working end-to-end on gemini-3.5-flash."""
        provider = GeminiProvider(_config("gemini-3.5-flash"))
        for kwargs in ({}, {"model": "gemini-3.5-flash"}):
            result = provider._convert_messages_to_gemini(_TOOL_LOOP_MESSAGES, **kwargs)
            model_turn = result["contents"][1]
            assert "id" not in model_turn["parts"][0]["functionCall"]
            fn_turn = result["contents"][-1]
            assert fn_turn["role"] == "function"
            fr = fn_turn["parts"][0]["functionResponse"]
            assert "id" not in fr
            assert set(fr) == {"name", "response"}

    def test_36_plus_fr_turn_does_not_merge_into_real_user_text_turn(self):
        """With FR turns now role "user", a genuine user TEXT message
        followed by a tool result must stay two separate turns — grouping
        requires every part of the previous turn to be a functionResponse."""
        provider = GeminiProvider(_config("gemini-3.7-flash"))
        messages = [
            {"role": "user", "content": "here is more context"},
            {
                "role": "tool",
                "tool_call_id": "fc-1",
                "name": "t",
                "content": "result",
            },
        ]
        result = provider._convert_messages_to_gemini(
            messages, model="gemini-3.7-flash"
        )
        assert len(result["contents"]) == 2
        assert result["contents"][0]["parts"][0] == {"text": "here is more context"}
        assert "functionResponse" in result["contents"][1]["parts"][0]

    def test_36_plus_consecutive_fr_parts_group_into_one_user_turn(self):
        """Parallel tool results still group into a single turn on the new
        surface, each functionResponse keeping its own id."""
        provider = GeminiProvider(_config("gemini-3.7-flash"))
        messages = [
            {"role": "tool", "tool_call_id": "fc-1", "name": "a", "content": "r1"},
            {"role": "tool", "tool_call_id": "fc-2", "name": "b", "content": "r2"},
        ]
        result = provider._convert_messages_to_gemini(
            messages, model="gemini-3.7-flash"
        )
        assert len(result["contents"]) == 1
        turn = result["contents"][0]
        assert turn["role"] == "user"
        ids = [p["functionResponse"]["id"] for p in turn["parts"]]
        assert ids == ["fc-1", "fc-2"]

    def test_37_saved_assistant_parts_still_echoed_verbatim(self):
        """The verbatim-parts path is how Gemini's own functionCall id (and
        every thoughtSignature) round-trips — the surface gates must not
        disturb it. Expected is a pre-call deep copy, so the assertion catches
        in-place mutation instead of comparing a mutated list to itself."""
        import copy

        provider = GeminiProvider(_config("gemini-3.7-flash"))
        saved = [
            {
                "functionCall": {
                    "id": "fc-from-api",
                    "name": "get_weather",
                    "args": {"city": "Paris"},
                },
                "thoughtSignature": "sig-1",
            }
        ]
        expected_parts = copy.deepcopy(saved)
        messages = [
            {"role": "user", "content": "check"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "fc-from-api",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": "{}"},
                    }
                ],
                "provider_metadata": {"assistant_parts": saved},
            },
            {
                "role": "tool",
                "tool_call_id": "fc-from-api",
                "name": "get_weather",
                "content": '{"result": "22C"}',
            },
        ]
        result = provider._convert_messages_to_gemini(
            messages, model="gemini-3.7-flash"
        )
        assert result["contents"][1] == {"role": "model", "parts": expected_parts}
        # Fully-id'd parts take the no-copy fast path: the echo stays the
        # caller's own list, byte-identical.
        assert result["contents"][1]["parts"] is saved
        fr = result["contents"][2]["parts"][0]["functionResponse"]
        assert fr["id"] == "fc-from-api"

    def test_36_plus_idless_saved_function_call_backfilled_from_tool_calls(self):
        """When the API sent an id-less functionCall, the parser synthesized a
        ToolCall.id and the functionResponse will carry it — the verbatim echo
        must agree or the FR names a call that carries no id. The synthesized
        id is back-filled into a COPY; the caller's saved parts are never
        mutated, and API-issued ids on sibling parts are untouched."""
        import copy

        provider = GeminiProvider(_config("gemini-3.7-flash"))
        saved = [
            {"text": "thinking...", "thoughtSignature": "sig-0"},
            {
                # API sent no id here — parser minted call_synth1.
                "functionCall": {"name": "search_file", "args": {"query": "x"}},
                "thoughtSignature": "sig-1",
            },
            {
                # API DID issue this one — must not be overwritten.
                "functionCall": {
                    "id": "call_api2",
                    "name": "kb_qa",
                    "args": {"query": "y"},
                }
            },
        ]
        original = copy.deepcopy(saved)
        messages = [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_synth1",
                        "type": "function",
                        "function": {"name": "search_file", "arguments": "{}"},
                    },
                    {
                        "id": "call_api2",
                        "type": "function",
                        "function": {"name": "kb_qa", "arguments": "{}"},
                    },
                ],
                "provider_metadata": {"assistant_parts": saved},
            },
            {
                "role": "tool",
                "tool_call_id": "call_synth1",
                "name": "search_file",
                "content": "r1",
            },
        ]
        result = provider._convert_messages_to_gemini(
            messages, model="gemini-3.7-flash"
        )
        echoed = result["contents"][1]["parts"]
        assert echoed[1]["functionCall"]["id"] == "call_synth1"  # back-filled
        assert echoed[1]["thoughtSignature"] == "sig-1"  # signature preserved
        assert echoed[2]["functionCall"]["id"] == "call_api2"  # untouched
        assert saved == original  # caller state never mutated
        fr = result["contents"][2]["parts"][0]["functionResponse"]
        assert fr["id"] == "call_synth1"  # pair matches

    def test_37_tool_message_without_id_omits_field(self):
        """A tool message with no tool_call_id (degraded cross-provider
        history) can't invent an id — omit the field rather than send a
        mismatch."""
        provider = GeminiProvider(_config("gemini-3.7-flash"))
        messages = [{"role": "tool", "name": "t", "content": "x"}]
        result = provider._convert_messages_to_gemini(
            messages, model="gemini-3.7-flash"
        )
        fr = result["contents"][0]["parts"][0]["functionResponse"]
        assert "id" not in fr


@pytest.mark.unit
@pytest.mark.asyncio
class TestFunctionCallIdAdoption:
    async def test_gemini_issued_id_adopted_as_tool_call_id(self, mock_aiohttp_session):
        """3.7 populates functionCall.id; it must become ToolCall.id so the
        engine's tool_call_id plumbing echoes it back on the
        functionResponse."""
        resp = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "functionCall": {
                                    "id": "fc-api-1",
                                    "name": "get_weather",
                                    "args": {"city": "Paris"},
                                }
                            }
                        ]
                    },
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {"candidatesTokenCount": 5},
        }
        provider = GeminiProvider(_config("gemini-3.7-flash"))
        mock_session = mock_aiohttp_session(resp)
        with patch("aiohttp.ClientSession", return_value=mock_session):
            response = await provider.generate("q", max_tokens=500)
        assert response.tool_calls[0].id == "fc-api-1"

    async def test_absent_id_still_synthesized(self, mock_aiohttp_session):
        """Pre-3.7 responses carry no functionCall.id — the synthetic call_*
        id keeps the engine's plumbing non-empty, exactly as before."""
        resp = {
            "candidates": [
                {
                    "content": {
                        "parts": [{"functionCall": {"name": "get_weather", "args": {}}}]
                    },
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {"candidatesTokenCount": 5},
        }
        provider = GeminiProvider(_config("gemini-3.5-flash"))
        mock_session = mock_aiohttp_session(resp)
        with patch("aiohttp.ClientSession", return_value=mock_session):
            response = await provider.generate("q", max_tokens=500)
        assert response.tool_calls[0].id.startswith("call_")


# --- prefill guard -----------------------------------------------------------


@pytest.mark.unit
class TestPrefillGuard:
    _PREFILL = [
        {"role": "user", "content": "Complete this."},
        {"role": "assistant", "content": "The answer is"},
    ]

    def test_37_trailing_model_turn_warns(self, caplog):
        provider = GeminiProvider(_config("gemini-3.7-flash"))
        with caplog.at_level("WARNING"):
            provider._convert_messages_to_gemini(
                self._PREFILL, model="gemini-3.7-flash"
            )
        assert any("prefill" in r.message.lower() for r in caplog.records)

    def test_pre_37_trailing_model_turn_silent(self, caplog):
        provider = GeminiProvider(_config("gemini-3.5-flash"))
        with caplog.at_level("WARNING"):
            provider._convert_messages_to_gemini(self._PREFILL)
        assert not any("prefill" in r.message.lower() for r in caplog.records)

    def test_37_normal_tool_loop_does_not_warn(self, caplog):
        """The engine's real shape (ends on a tool turn) must never trip the
        guard."""
        provider = GeminiProvider(_config("gemini-3.7-flash"))
        with caplog.at_level("WARNING"):
            provider._convert_messages_to_gemini(
                _TOOL_LOOP_MESSAGES, model="gemini-3.7-flash"
            )
        assert not any("prefill" in r.message.lower() for r in caplog.records)


# --- generate() end-to-end wiring against the 3.7 surface --------------------


@pytest.mark.unit
@pytest.mark.asyncio
class TestGenerate37EndToEnd:
    async def test_full_tool_roundtrip_body_shape(self, mock_aiohttp_session):
        """One request carrying the whole loop: no sampling params, thinking
        capped low, functionCall/functionResponse id pair intact."""
        provider = GeminiProvider(_config("gemini-3.7-flash"))
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "d",
                    "parameters": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                    },
                },
            }
        ]
        body = await _sent_body(
            mock_aiohttp_session,
            provider,
            max_tokens=500,
            temperature=0.7,
            messages=_TOOL_LOOP_MESSAGES,
            tools=tools,
            tool_choice="auto",
        )
        gen = body["generationConfig"]
        assert "temperature" not in gen
        assert gen["thinkingConfig"] == {"thinkingLevel": "low"}
        model_turn = body["contents"][1]
        assert model_turn["parts"][0]["functionCall"]["id"] == "fc-abc123"
        fn_turn = body["contents"][2]
        assert fn_turn["role"] == "user"  # 3.6+: role "function" is a 400
        fr = fn_turn["parts"][0]["functionResponse"]
        assert fr["id"] == "fc-abc123"
        assert fr["name"] == "get_weather"

    async def test_35_full_tool_roundtrip_body_unchanged(self, mock_aiohttp_session):
        """The same call on 3.5 keeps the classic body: temperature present,
        no ids anywhere, thinkingConfig only because tools make it a
        structured call (the pre-existing 3.x starvation cap)."""
        provider = GeminiProvider(_config("gemini-3.5-flash"))
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "d",
                    "parameters": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                    },
                },
            }
        ]
        body = await _sent_body(
            mock_aiohttp_session,
            provider,
            max_tokens=500,
            temperature=0.7,
            messages=_TOOL_LOOP_MESSAGES,
            tools=tools,
            tool_choice="auto",
        )
        gen = body["generationConfig"]
        assert gen["temperature"] == 0.7
        assert gen["thinkingConfig"] == {"thinkingLevel": "low"}
        assert "id" not in body["contents"][1]["parts"][0]["functionCall"]
        fn_turn = body["contents"][2]
        assert fn_turn["role"] == "function"  # classic role, measured working
        fr = fn_turn["parts"][0]["functionResponse"]
        assert set(fr) == {"name", "response"}


# --- every cap decision is reported (finding: floored INFERENCE on 3.7 plain) -


@pytest.mark.unit
class TestThinkingCapDecisionLogging:
    def test_37_inference_with_floor_on_plain_call_logs_the_lift(self, caplog):
        """On the 3.7+ surface a plain call has a cap to lift too — the lift
        must be reported like every sibling combination, or spend audits read
        the call as capped when it ran at native thinking."""
        provider = GeminiProvider(_config("gemini-3.7-flash"))
        with caplog.at_level("INFO"):
            result = provider._structured_thinking_config(
                "gemini-3.7-flash",
                False,
                intent=ReasoningIntent.INFERENCE,
                has_output_floor=True,
            )
        assert result is None
        assert any("lifting the 3.7-surface" in r.message for r in caplog.records)

    def test_pre_37_inference_with_floor_on_plain_call_stays_quiet(self, caplog):
        """Pre-3.7 a plain call never had a cap — nothing is lifted, so
        logging a lift there would report an event that did not happen."""
        provider = GeminiProvider(_config("gemini-3.5-flash"))
        with caplog.at_level("INFO"):
            result = provider._structured_thinking_config(
                "gemini-3.5-flash",
                False,
                intent=ReasoningIntent.INFERENCE,
                has_output_floor=True,
            )
        assert result is None
        assert not any("lifting" in r.message for r in caplog.records)


# --- unparseable model ids fail open loudly ----------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
class TestUnparseableVersionWarning:
    async def test_tool_call_on_versionless_alias_warns_once(
        self, mock_aiohttp_session, caplog
    ):
        """gemini-flash-latest parses to no version, so both surface gates
        fail open to the legacy shape; if the alias is served by a 3.6+
        backend the legacy tool-result turn is a deterministic 400 — the
        fail-open must be named once, or that 400 is unattributable."""
        provider = GeminiProvider(_config("gemini-flash-latest"))
        tools = [
            {
                "type": "function",
                "function": {"name": "t", "parameters": {"type": "object"}},
            }
        ]
        with caplog.at_level("WARNING"):
            await _sent_body(
                mock_aiohttp_session, provider, max_tokens=500, tools=tools
            )
            await _sent_body(
                mock_aiohttp_session, provider, max_tokens=500, tools=tools
            )
        warns = [
            r for r in caplog.records if "Cannot parse a Gemini version" in r.message
        ]
        assert len(warns) == 1

    async def test_plain_call_on_versionless_alias_does_not_warn(
        self, mock_aiohttp_session, caplog
    ):
        """Plain chat is unaffected by the fail-open (sampling params are
        merely tolerated) — warning there would be noise."""
        provider = GeminiProvider(_config("gemini-flash-latest"))
        with caplog.at_level("WARNING"):
            await _sent_body(mock_aiohttp_session, provider, max_tokens=500)
        assert not any(
            "Cannot parse a Gemini version" in r.message for r in caplog.records
        )

    async def test_versioned_model_never_warns(self, mock_aiohttp_session, caplog):
        provider = GeminiProvider(_config("gemini-3.7-flash"))
        tools = [
            {
                "type": "function",
                "function": {"name": "t", "parameters": {"type": "object"}},
            }
        ]
        with caplog.at_level("WARNING"):
            await _sent_body(
                mock_aiohttp_session, provider, max_tokens=500, tools=tools
            )
        assert not any(
            "Cannot parse a Gemini version" in r.message for r in caplog.records
        )
