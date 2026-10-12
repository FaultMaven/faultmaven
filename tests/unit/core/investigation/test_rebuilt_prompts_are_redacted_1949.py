"""#1949: no prompt reaches a provider without the case-scoped redaction.

``_generate_structured_output_inner`` redacted its prompt once, at the top.
When the tool loop raised ``ToolCallingUnsupportedError`` the fallback replaced
that prompt with ``fallback_prompt_builder()`` -- a fresh, full-evidence,
UNREDACTED assembly -- and sent it down the non-tool path with no further pass.
The router's global sanitizer (``SANITIZE_PII``) still ran and produces the
same text (one detection path, keyed pseudonyms, #971). What was lost is the
case REGISTRY: a value first seen in the rebuilt prompt never entered it, so
its placeholder could not be reversed in the reply the user reads. Protection
also rested on whichever router is configured. (Presidio availability is fixed
per sanitizer instance at construction, so instances built across a Presidio
outage can still differ.)

Every site where case content becomes model-bound text now goes through one
helper, ``case_redaction.case_redacted`` (moved there from ``generation`` by
#1956, so every send site outside the engine shares it). Each test below drives one site with a
fake provider (no live LLM call) and a redaction double whose output is
recognisable, and asserts what the provider RECEIVED:

- the starting prompt, on the tool-loop and the single-shot path;
- the ``ToolCallingUnsupportedError`` full-evidence fallback (the defect),
  reached both ways the loop refuses at runtime;
- the #662 context-overflow minimal-prompt retry;
- every tool result relayed back into the loop.

The #614 tool-loop base resize is pinned by
``test_milestone_engine_tool_loop.py::TestToolLoopBaseFitsTheReceivingModel::
test_the_reassembled_base_is_redacted_like_the_original``. The last class pins
the shape: nothing in the engine sanitizes except through the helper, and no
prompt builder is called except as the helper's argument.
"""

import ast
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from pydantic import BaseModel

from faultmaven.core.investigation.milestone_engine import generation
from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.exceptions import LLMException
from faultmaven.infrastructure.llm.providers.base import LLMResponse, ToolCall
from faultmaven.infrastructure.llm.structured_output_capability import (
    StructuredOutputCapability,
    StructuredOutputMode,
    StructuredOutputStrategy,
)
from faultmaven.models.interfaces import ToolResult

RAW = "alice@example.com"
PSEUDONYM = "<EMAIL_ADDRESS_1949c0de>"


class _Schema(BaseModel):
    agent_response: str = "ok"


class _MarkingRedaction:
    """Stands in for ``CaseRedactionContext``: rewrites the one entity to a
    recognisable pseudonym and records every text it was handed."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    async def asanitize(self, text: str) -> str:
        self.seen.append(text)
        return text.replace(RAW, PSEUDONYM)


def _schema_call(agent_response: str = "done") -> LLMResponse:
    return LLMResponse(
        content="",
        confidence=0.9,
        provider="test",
        model="test-model",
        tokens_used=10,
        response_time_ms=1,
        tool_calls=[
            ToolCall(
                id="call_schema",
                type="function",
                function={
                    "name": "_Schema",
                    "arguments": json.dumps({"agent_response": agent_response}),
                },
            )
        ],
    )


def _search_call() -> LLMResponse:
    return LLMResponse(
        content="",
        confidence=0.9,
        provider="test",
        model="test-model",
        tokens_used=10,
        response_time_ms=1,
        tool_calls=[
            ToolCall(
                id="call_search",
                type="function",
                function={"name": "search_file", "arguments": '{"query": "q"}'},
            )
        ],
    )


def _no_tool_call() -> LLMResponse:
    return LLMResponse(
        content="",
        confidence=0.5,
        provider="test",
        model="test-model",
        tokens_used=10,
        response_time_ms=1,
        tool_calls=None,
    )


_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "search_file",
        "description": "Search evidence",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
    },
}


def _engine(generate, *, registry=None):
    """An engine whose one provider runs ``generate`` for every call, offers
    tool calling, and answers single-shot calls by function calling."""
    provider = AsyncMock()
    provider.supports_tool_calling = Mock(return_value=True)
    provider.get_structured_output_strategy = MagicMock(
        return_value=StructuredOutputStrategy(
            capability=StructuredOutputCapability.FUNCTION_CALLING,
            mode=StructuredOutputMode.FUNCTION_CALLING,
            include_schema_in_prompt=False,
            response_format=None,
            extra_config={},
        )
    )
    provider.generate = AsyncMock(side_effect=generate)
    repo = MagicMock()
    repo.save = AsyncMock()
    return MilestoneEngine(
        llm_provider=provider,
        repository=repo,
        investigation_tools=registry,
    )


def _sent_text(call_kwargs: dict) -> str:
    """Everything a provider call carried: the prompt and every message body."""
    parts = [call_kwargs.get("prompt") or ""]
    parts += [m.get("content") or "" for m in call_kwargs.get("messages") or []]
    return "\n".join(parts)


@pytest.mark.unit
@pytest.mark.asyncio
class TestTheToolCallingFallbackPromptIsRedacted:
    """The defect: the full-evidence rebuild after ``ToolCallingUnsupportedError``."""

    @pytest.mark.parametrize("refusal", ["iteration_failure", "no_schema_call"])
    async def test_the_rebuilt_prompt_the_non_tool_path_sends_is_redacted(
        self, refusal
    ):
        calls: list[dict] = []

        async def generate(**kwargs):
            calls.append(kwargs)
            if kwargs.get("messages"):  # the tool loop
                if refusal == "iteration_failure":
                    raise RuntimeError("provider rejected tools")
                return _no_tool_call()  # twice → forced-schema refusal
            return _schema_call("fell back")

        engine = _engine(generate)
        redaction = _MarkingRedaction()

        result = await engine.generator.generate_structured_output(
            f"ELIDED {RAW}",
            _Schema,
            investigation_tools=[_SEARCH_TOOL],
            tool_context=MagicMock(),
            redaction_ctx=redaction,
            fallback_prompt_builder=lambda: f"FULL EVIDENCE {RAW}",
        )

        assert result.agent_response == "fell back"
        single_shot = [c for c in calls if not c.get("messages")]
        assert len(single_shot) == 1
        sent = single_shot[0]["prompt"]
        # Positive control: the REBUILT prompt is what went out, not the
        # original that was redacted at the top.
        assert sent.startswith("FULL EVIDENCE")
        assert PSEUDONYM in sent
        assert RAW not in sent
        assert f"FULL EVIDENCE {RAW}" in redaction.seen
        # And nothing the tool loop sent first carried the raw entity either.
        for call in calls:
            assert RAW not in _sent_text(call)

    async def test_a_failed_rebuild_keeps_the_redacted_original(self):
        """The fallback never breaks the turn: when the builder raises, the
        original prompt is sent -- the one already redacted at the top."""
        calls: list[dict] = []

        async def generate(**kwargs):
            calls.append(kwargs)
            if kwargs.get("messages"):
                raise RuntimeError("provider rejected tools")
            return _schema_call()

        def broken_builder():
            raise ValueError("assembly failed")

        engine = _engine(generate)
        await engine.generator.generate_structured_output(
            f"ELIDED {RAW}",
            _Schema,
            investigation_tools=[_SEARCH_TOOL],
            tool_context=MagicMock(),
            redaction_ctx=_MarkingRedaction(),
            fallback_prompt_builder=broken_builder,
        )

        sent = [c for c in calls if not c.get("messages")][0]["prompt"]
        assert sent == f"ELIDED {PSEUDONYM}"


@pytest.mark.unit
@pytest.mark.asyncio
class TestTheContextOverflowRetryPromptIsRedacted:
    """#662: the minimal prompt is assembled from the case after the first
    attempt; it re-enters the inner generator, whose first act redacts it."""

    async def test_the_minimal_retry_prompt_is_redacted(self):
        calls: list[dict] = []

        async def generate(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise LLMException(
                    "prompt is too long: 250000 tokens > 200000 maximum",
                    status_code=400,
                )
            return _schema_call("degraded")

        engine = _engine(generate)
        case = MagicMock()
        case.case_id = "case_1949"

        with patch(
            "faultmaven.core.investigation.prompts.templates.fallback."
            "get_fallback_prompt_for_case",
            return_value=f"MINIMAL {RAW}",
        ):
            result = await engine.generator.generate_structured_output(
                f"HUGE {RAW}",
                _Schema,
                redaction_ctx=_MarkingRedaction(),
                case=case,
                user_message="why is it slow?",
            )

        assert result.agent_response == "degraded"
        # Positive control: the retry happened, with the minimal prompt.
        assert len(calls) == 2
        retry_prompt = calls[1]["prompt"]
        assert retry_prompt.startswith(f"MINIMAL {PSEUDONYM}")
        for call in calls:
            assert RAW not in call["prompt"]


@pytest.mark.unit
@pytest.mark.asyncio
class TestTheStartingPromptIsRedacted:
    async def test_on_the_single_shot_path(self):
        calls: list[dict] = []

        async def generate(**kwargs):
            calls.append(kwargs)
            return _schema_call()

        engine = _engine(generate)
        await engine.generator.generate_structured_output(
            f"PROMPT {RAW}", _Schema, redaction_ctx=_MarkingRedaction()
        )

        assert calls[0]["prompt"] == f"PROMPT {PSEUDONYM}"

    async def test_on_the_tool_loop_path(self):
        calls: list[dict] = []

        async def generate(**kwargs):
            calls.append(kwargs)
            return _schema_call()

        engine = _engine(generate)
        await engine.generator.generate_structured_output(
            f"PROMPT {RAW}",
            _Schema,
            investigation_tools=[_SEARCH_TOOL],
            tool_context=MagicMock(),
            redaction_ctx=_MarkingRedaction(),
        )

        base = calls[0]["messages"][1]
        assert base["role"] == "user"
        assert base["content"] == f"PROMPT {PSEUDONYM}"


@pytest.mark.unit
@pytest.mark.asyncio
class TestToolResultsAreRedactedBeforeTheModelSeesThem:
    async def test_a_tool_result_goes_back_redacted(self):
        snapshots: list[list[dict]] = []

        async def generate(**kwargs):
            snapshots.append([dict(m) for m in kwargs["messages"]])
            if len(snapshots) == 1:
                return _search_call()
            return _schema_call()

        registry = MagicMock()
        registry.get_all_tools.return_value = []
        registry.execute_tool = AsyncMock(
            return_value=ToolResult(success=True, data=f"10:00 conn refused for {RAW}")
        )
        engine = _engine(generate, registry=registry)
        await engine.generator.generate_structured_output(
            "PROMPT",
            _Schema,
            investigation_tools=[_SEARCH_TOOL],
            tool_context=MagicMock(),
            redaction_ctx=_MarkingRedaction(),
        )

        # Positive control: the tool ran and its result was relayed.
        tool_msgs = [m for m in snapshots[1] if m["role"] == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["content"] == f"10:00 conn refused for {PSEUDONYM}"
        assert RAW not in "\n".join(m["content"] or "" for m in snapshots[1])


# ---------------------------------------------------------------------------
# The shape: one choke point
# ---------------------------------------------------------------------------

_ENGINE_DIR = Path(generation.__file__).parent
_HELPER = "case_redacted"
_BUILDERS = {"fallback_prompt_builder", "base_prompt_builder"}


def _parents(tree: ast.AST) -> dict:
    return {
        child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
    }


def _enclosing_function(node, parents) -> str | None:
    while node in parents:
        node = parents[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node.name
    return None


def _is_helper_call(node) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == _HELPER
    )


@pytest.mark.unit
class TestOneChokePoint:
    def test_nothing_in_the_engine_sanitizes_except_through_the_helper(self):
        offenders = []
        for path in sorted(_ENGINE_DIR.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            parents = _parents(tree)
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"asanitize", "sanitize"}
                ):
                    where = _enclosing_function(node, parents)
                    offenders.append(f"{path.name}:{node.lineno} in {where}")
        assert offenders == [], (
            "case content must reach the redaction through "
            f"case_redaction.{_HELPER}, not a direct call: {offenders}"
        )

    def test_every_prompt_builder_call_is_the_helpers_argument(self):
        tree = ast.parse(Path(generation.__file__).read_text(encoding="utf-8"))
        parents = _parents(tree)
        builder_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in _BUILDERS
        ]
        # Positive control: both rebuild sites exist, so this is not vacuous.
        assert {c.func.id for c in builder_calls} == _BUILDERS
        unwrapped = [
            f"{c.func.id} at line {c.lineno}"
            for c in builder_calls
            if not (_is_helper_call(parents.get(c)) and parents[c].args[:1] == [c])
        ]
        assert unwrapped == [], (
            "a rebuilt prompt is raw case content; pass the builder's result "
            f"straight to {_HELPER}: {unwrapped}"
        )
