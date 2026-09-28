"""Per-model Anthropic request shape (#1695).

The newest Claude models reject two parts of the request this provider used
to send every model: any ``temperature`` (opus-4-8 onward) and forced tool use,
``tool_choice`` of type ``any``/``tool`` (opus-5-5 and fable-5-1 measured live
on 2026-09-28; mythos-5-1 per the model docs). The provider gates each part on a
per-family version ceiling. An id it cannot parse, or a version above the
ceiling, takes the current shape: no temperature, and ``auto`` plus a trailing
system instruction that names the tool.

Every assertion is on the OUTGOING JSON body handed to aiohttp, driven through
``generate()``.
"""

import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.infrastructure.llm.providers.anthropic import AnthropicProvider
from faultmaven.infrastructure.llm.providers.base import ProviderConfig

ONE_TOOL_INSTRUCTION = "Respond by calling the `{name}` tool."
ANY_TOOL_INSTRUCTION = (
    "Respond by calling one of the provided tools; do not reply in plain text."
)

# (model id, accepts sampling, accepts forced tool_choice)
MODEL_ROWS = [
    ("claude-sonnet-4-6", True, True),
    ("claude-haiku-4-5-20251001", True, True),
    ("claude-sonnet-4-5", True, True),
    ("claude-opus-4-20250514", True, True),
    ("claude-opus-4-8", False, True),
    ("claude-opus-5", False, True),
    ("claude-sonnet-5", False, True),
    ("claude-fable-5", False, True),
    ("claude-opus-5-5", False, False),
    ("claude-fable-5-1", False, False),
    ("claude-mythos-5-1", False, False),
    # An unknown future version and an unparseable id take the current shape.
    ("claude-opus-6", False, False),
    ("claude-mythos-preview", False, False),
]
MODEL_IDS = [row[0] for row in MODEL_ROWS]


def _config(model: str, thinking_mode=None) -> ProviderConfig:
    return ProviderConfig(
        name="anthropic",
        api_key="test-key",
        base_url="https://api.anthropic.com/v1",
        models=[model],
        default_model=model,
        timeout=30,
        confidence_score=0.9,
        thinking_mode=thinking_mode,
    )


def _tool(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"{name} tool",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
            },
        },
    }


SCHEMA_NAME = "InvestigationResponse_Diagnosis"
SCHEMA_TOOL = _tool(SCHEMA_NAME)
SEARCH_TOOL = _tool("search_file")

_TOOL_USE_RESP = {
    "content": [
        {
            "type": "tool_use",
            "id": "toolu_01",
            "name": "InvestigationResponse_Diagnosis",
            "input": {"query": "x"},
        }
    ],
    "stop_reason": "tool_use",
    "usage": {"input_tokens": 10, "output_tokens": 5},
}


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


async def _sent_body(model: str, thinking_mode=None, **generate_kwargs) -> dict:
    """Run generate() on *model* against a mocked transport; return the body."""
    provider = AnthropicProvider(_config(model, thinking_mode=thinking_mode))
    mock_session = _mock_aiohttp_session(_TOOL_USE_RESP)
    generate_kwargs.setdefault("max_tokens", 8000)
    generate_kwargs.setdefault("temperature", 0.2)
    with patch("aiohttp.ClientSession", return_value=mock_session):
        await provider.generate("Test prompt", **generate_kwargs)
    call = mock_session.post.call_args
    return call.kwargs.get("json") or call[1].get("json")


# =========================================================================
# The model-id parse
# =========================================================================


@pytest.mark.unit
@pytest.mark.parametrize(
    "model,expected",
    [
        ("claude-sonnet-4-6", ("sonnet", (4, 6))),
        ("claude-opus-4-20250514", ("opus", (4, 0))),
        ("claude-haiku-4-5-20251001", ("haiku", (4, 5))),
        ("claude-opus-5", ("opus", (5, 0))),
        ("claude-opus-5-5", ("opus", (5, 5))),
        ("claude-fable-5-1", ("fable", (5, 1))),
        ("claude-mythos-5-1", ("mythos", (5, 1))),
        ("claude-opus-4-10", ("opus", (4, 10))),
        ("CLAUDE-OPUS-5-5", ("opus", (5, 5))),
        ("claude-mythos-preview", None),
        ("claude-3-5-sonnet-20241022", None),
        ("claude-opus-5-5-latest", None),
        ("claude-opus-4-123", None),
        ("gpt-5.6-luna", None),
        ("", None),
    ],
)
def test_claude_version_parses_family_and_version(model, expected):
    assert AnthropicProvider._claude_version(model) == expected


# =========================================================================
# The per-model table: temperature, tool_choice and the instruction block
# =========================================================================


@pytest.mark.unit
@pytest.mark.asyncio
class TestRequestShapeTable:
    @pytest.mark.parametrize(
        "model,accepts_sampling,_forcing", MODEL_ROWS, ids=MODEL_IDS
    )
    async def test_temperature_sent_only_when_model_accepts_sampling(
        self, model, accepts_sampling, _forcing
    ):
        plain = await _sent_body(model)
        structured = await _sent_body(
            model, tools=[SCHEMA_TOOL], tool_choice="required"
        )

        for body in (plain, structured):
            if accepts_sampling:
                assert body["temperature"] == 0.2
            else:
                assert "temperature" not in body

    @pytest.mark.parametrize(
        "model,_sampling,accepts_forcing", MODEL_ROWS, ids=MODEL_IDS
    )
    async def test_required_with_one_tool(self, model, _sampling, accepts_forcing):
        body = await _sent_body(model, tools=[SCHEMA_TOOL], tool_choice="required")

        if accepts_forcing:
            assert body["tool_choice"] == {"type": "any"}
            assert "system" not in body
        else:
            assert body["tool_choice"] == {"type": "auto"}
            assert body["system"] == [
                {
                    "type": "text",
                    "text": ONE_TOOL_INSTRUCTION.format(
                        name="InvestigationResponse_Diagnosis"
                    ),
                }
            ]

    @pytest.mark.parametrize(
        "model,_sampling,accepts_forcing", MODEL_ROWS, ids=MODEL_IDS
    )
    async def test_required_with_several_tools(self, model, _sampling, accepts_forcing):
        body = await _sent_body(
            model, tools=[SEARCH_TOOL, SCHEMA_TOOL], tool_choice="required"
        )

        if accepts_forcing:
            assert body["tool_choice"] == {"type": "any"}
            assert "system" not in body
        else:
            assert body["tool_choice"] == {"type": "auto"}
            assert body["system"] == [{"type": "text", "text": ANY_TOOL_INSTRUCTION}]

    @pytest.mark.parametrize("model,_sampling,_forcing", MODEL_ROWS, ids=MODEL_IDS)
    async def test_auto_is_sent_as_auto_without_an_instruction(
        self, model, _sampling, _forcing
    ):
        body = await _sent_body(
            model, tools=[SEARCH_TOOL, SCHEMA_TOOL], tool_choice="auto"
        )

        assert body["tool_choice"] == {"type": "auto"}
        assert "system" not in body

    @pytest.mark.parametrize(
        "model,_sampling,accepts_forcing", MODEL_ROWS, ids=MODEL_IDS
    )
    async def test_native_tool_dict_names_its_tool(
        self, model, _sampling, accepts_forcing
    ):
        """{"type": "tool", "name": X} with several tools offered: the
        instruction names X, not the generic sentence, and above the ceiling
        the offered tools narrow to X so "auto" cannot pick another."""
        choice = {"type": "tool", "name": "search_file"}
        body = await _sent_body(
            model, tools=[SEARCH_TOOL, SCHEMA_TOOL], tool_choice=choice
        )

        sent_tool_names = [t["name"] for t in body["tools"]]
        if accepts_forcing:
            assert body["tool_choice"] == {"type": "tool", "name": "search_file"}
            assert "system" not in body
            assert sent_tool_names == ["search_file", SCHEMA_NAME]
        else:
            assert body["tool_choice"] == {"type": "auto"}
            assert body["system"] == [
                {
                    "type": "text",
                    "text": ONE_TOOL_INSTRUCTION.format(name="search_file"),
                }
            ]
            assert sent_tool_names == ["search_file"]

    @pytest.mark.parametrize(
        "model,_sampling,accepts_forcing", MODEL_ROWS, ids=MODEL_IDS
    )
    async def test_named_tool_not_offered_leaves_tools_unchanged(
        self, model, _sampling, accepts_forcing
    ):
        """Narrowing needs X among the offered tools; otherwise the tools are
        sent as they are."""
        choice = {"type": "tool", "name": "not_offered"}
        body = await _sent_body(
            model, tools=[SEARCH_TOOL, SCHEMA_TOOL], tool_choice=choice
        )

        assert [t["name"] for t in body["tools"]] == ["search_file", SCHEMA_NAME]
        if accepts_forcing:
            assert body["tool_choice"] == choice
        else:
            assert body["tool_choice"] == {"type": "auto"}

    @pytest.mark.parametrize(
        "model,_sampling,accepts_forcing", MODEL_ROWS, ids=MODEL_IDS
    )
    async def test_native_any_dict_maps_like_required(
        self, model, _sampling, accepts_forcing
    ):
        body = await _sent_body(
            model, tools=[SEARCH_TOOL, SCHEMA_TOOL], tool_choice={"type": "any"}
        )

        if accepts_forcing:
            assert body["tool_choice"] == {"type": "any"}
            assert "system" not in body
        else:
            assert body["tool_choice"] == {"type": "auto"}
            assert body["system"] == [{"type": "text", "text": ANY_TOOL_INSTRUCTION}]

    @pytest.mark.parametrize(
        "model,_sampling,accepts_forcing", MODEL_ROWS, ids=MODEL_IDS
    )
    async def test_forced_dict_keeps_its_other_keys(
        self, model, _sampling, accepts_forcing
    ):
        """A caller's keys other than type and name (disable_parallel_tool_use
        is valid with auto) survive the mapping to auto."""
        choice = {
            "type": "tool",
            "name": "search_file",
            "disable_parallel_tool_use": True,
        }
        body = await _sent_body(
            model, tools=[SEARCH_TOOL, SCHEMA_TOOL], tool_choice=choice
        )

        if accepts_forcing:
            assert body["tool_choice"] == {
                "type": "tool",
                "name": "search_file",
                "disable_parallel_tool_use": True,
            }
            assert "system" not in body
        else:
            assert body["tool_choice"] == {
                "type": "auto",
                "disable_parallel_tool_use": True,
            }
            assert body["system"] == [
                {
                    "type": "text",
                    "text": ONE_TOOL_INSTRUCTION.format(name="search_file"),
                }
            ]


# =========================================================================
# Where the instruction goes relative to the system prompt and the cache
# =========================================================================


@pytest.mark.unit
@pytest.mark.asyncio
class TestInstructionPlacement:
    async def test_cached_system_block_is_unchanged_and_instruction_trails(self):
        system_prompt = "You are an SRE investigator. " * 20

        body = await _sent_body(
            "claude-opus-5-5",
            system=system_prompt,
            cache_prompt=True,
            tools=[SCHEMA_TOOL],
            tool_choice="required",
        )

        assert body["system"][0] == {
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"},
        }
        assert body["system"][-1] == {
            "type": "text",
            "text": ONE_TOOL_INSTRUCTION.format(name="InvestigationResponse_Diagnosis"),
        }
        assert len(body["system"]) == 2

    async def test_cached_prefix_matches_a_model_that_accepts_forcing(self):
        """The cached block is byte-identical to what a forcing-capable model
        is sent: the instruction is added after it, never folded into it."""
        common = dict(
            system="Stable system prompt.",
            cache_prompt=True,
            tools=[SCHEMA_TOOL],
            tool_choice="required",
        )
        forcing = await _sent_body("claude-sonnet-4-6", **common)
        instructed = await _sent_body("claude-opus-5-5", **common)

        assert instructed["system"][: len(forcing["system"])] == forcing["system"]
        assert instructed["tools"] == forcing["tools"]

    async def test_system_from_messages_becomes_text_blocks(self):
        """The tool-loop shape: the system prompt arrives as a system message."""
        messages = [
            {"role": "system", "content": "Investigate carefully."},
            {"role": "user", "content": "Pods are crashlooping."},
        ]

        body = await _sent_body(
            "claude-fable-5-1",
            messages=messages,
            tools=[SEARCH_TOOL, SCHEMA_TOOL],
            tool_choice="required",
        )

        assert body["system"] == [
            {"type": "text", "text": "Investigate carefully."},
            {"type": "text", "text": ANY_TOOL_INSTRUCTION},
        ]
        # The engine's messages are sent as converted, with nothing injected.
        assert body["messages"] == [
            {"role": "user", "content": "Pods are crashlooping."}
        ]

    async def test_caller_system_list_is_extended_not_mutated(self):
        caller_system = [{"type": "text", "text": "Caller block."}]

        body = await _sent_body(
            "claude-opus-5-5",
            system=caller_system,
            tools=[SCHEMA_TOOL],
            tool_choice="required",
        )

        assert caller_system == [{"type": "text", "text": "Caller block."}]
        assert body["system"] == [
            {"type": "text", "text": "Caller block."},
            {
                "type": "text",
                "text": ONE_TOOL_INSTRUCTION.format(
                    name="InvestigationResponse_Diagnosis"
                ),
            },
        ]

    async def test_instruction_follows_the_tool_loop_tool_set(self):
        """The tool loop's real shape: iterations with every tool, then
        schema-only iterations (final / force-schema / ceiling). Each
        iteration's trailing block is the sentence for its own tool set, and
        an unchanged tool set sends a byte-identical block."""
        history = [
            {"role": "system", "content": "S"},
            {"role": "user", "content": "u1"},
        ]

        async def iteration(tools, extra_turns):
            messages = history + extra_turns
            body = await _sent_body(
                "claude-opus-5-5",
                messages=messages,
                tools=tools,
                tool_choice="required",
            )
            # The system prompt stays first; the instruction is the last block.
            assert body["system"][0] == {"type": "text", "text": "S"}
            return body["system"][-1]

        all_tools_1 = await iteration([SEARCH_TOOL, SCHEMA_TOOL], [])
        all_tools_2 = await iteration(
            [SEARCH_TOOL, SCHEMA_TOOL],
            [
                {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "u2"},
            ],
        )
        schema_only_1 = await iteration(
            [SCHEMA_TOOL],
            [
                {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "u2"},
            ],
        )
        schema_only_2 = await iteration(
            [SCHEMA_TOOL],
            [
                {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "u2"},
                {"role": "assistant", "content": "a2"},
                {"role": "user", "content": "u3"},
            ],
        )

        assert all_tools_1 == {"type": "text", "text": ANY_TOOL_INSTRUCTION}
        assert schema_only_1 == {
            "type": "text",
            "text": ONE_TOOL_INSTRUCTION.format(name=SCHEMA_NAME),
        }
        # Unchanged tool set -> byte-identical block.
        assert json.dumps(all_tools_2) == json.dumps(all_tools_1)
        assert json.dumps(schema_only_2) == json.dumps(schema_only_1)


# =========================================================================
# Thinking: refused under forcing only where forcing is actually sent
# =========================================================================


@pytest.mark.unit
@pytest.mark.asyncio
class TestThinkingAcrossTheForcingCeiling:
    async def test_adaptive_on_model_above_ceiling_carries_thinking(self, caplog):
        with caplog.at_level("WARNING"):
            body = await _sent_body(
                "claude-opus-5-5",
                thinking_mode="adaptive",
                tools=[SCHEMA_TOOL],
                tool_choice="required",
            )

        assert body["thinking"] == {"type": "adaptive"}
        assert body["tool_choice"] == {"type": "auto"}
        assert "temperature" not in body
        assert not any("thinking refused" in r.message for r in caplog.records)

    async def test_adaptive_on_forcing_model_still_refuses_thinking(self, caplog):
        with caplog.at_level("WARNING"):
            body = await _sent_body(
                "claude-sonnet-4-6",
                thinking_mode="adaptive",
                tools=[SCHEMA_TOOL],
                tool_choice="required",
            )

        assert "thinking" not in body
        assert body["tool_choice"] == {"type": "any"}
        assert body["temperature"] == 0.2
        assert any("thinking refused" in r.message for r in caplog.records)


# =========================================================================
# Logging: one line per model id, not one per call
# =========================================================================


def _shape_records(caplog):
    return [r for r in caplog.records if "(#1695)" in r.getMessage()]


@pytest.mark.unit
@pytest.mark.asyncio
class TestRequestShapeLogging:
    @pytest.fixture(autouse=True)
    def _fresh_log_memory(self, monkeypatch):
        monkeypatch.setattr(AnthropicProvider, "_REQUEST_SHAPE_LOGGED", set())

    async def test_unparseable_id_warns_once_across_two_calls(self, caplog):
        caplog.set_level(logging.INFO)

        for _ in range(2):
            await _sent_body(
                "claude-mythos-preview", tools=[SCHEMA_TOOL], tool_choice="required"
            )

        records = _shape_records(caplog)
        assert len(records) == 1
        assert records[0].levelno == logging.WARNING
        message = records[0].getMessage()
        assert "'claude-mythos-preview'" in message
        assert "no temperature" in message
        assert "auto plus an instruction" in message

    async def test_model_above_both_ceilings_logs_one_info_naming_both(self, caplog):
        caplog.set_level(logging.INFO)

        for _ in range(2):
            await _sent_body(
                "claude-opus-5-5", tools=[SCHEMA_TOOL], tool_choice="required"
            )

        records = _shape_records(caplog)
        assert len(records) == 1
        assert records[0].levelno == logging.INFO
        message = records[0].getMessage()
        assert "claude-opus-5-5" in message
        assert "temperature is not sent" in message
        assert "forced tool_choice is sent as auto plus an instruction" in message

    async def test_model_above_sampling_ceiling_only_names_temperature(self, caplog):
        caplog.set_level(logging.INFO)

        for _ in range(2):
            await _sent_body("claude-opus-5")

        records = _shape_records(caplog)
        assert len(records) == 1
        assert records[0].levelno == logging.INFO
        assert "temperature is not sent" in records[0].getMessage()
        assert "tool_choice" not in records[0].getMessage()

    async def test_model_within_both_ceilings_logs_nothing(self, caplog):
        caplog.set_level(logging.INFO)

        for _ in range(2):
            await _sent_body(
                "claude-sonnet-4-6", tools=[SCHEMA_TOOL], tool_choice="required"
            )

        assert _shape_records(caplog) == []
