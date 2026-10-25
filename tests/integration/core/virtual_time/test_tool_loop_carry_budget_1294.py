"""#1294: the fallback ladder budgets against the FAILING tool-loop call's cost.

The tool loop's hung call is carried into the tool-less fallback ladder as its
first failure, and the ladder's budget check prices the retry at that call's
cost. Priced at the whole loop instead (earlier good iterations included), a
retry that fits was refused: two good 15 s iterations then a 30 s hang, at a
120 s turn, answered ``TURN_BUDGET_EXHAUSTED`` with no fallback call.

Virtual time (this directory's ``conftest.py``): every sleep costs exactly its
nominal duration, so the budget arithmetic is the code's, not the runner's.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from pydantic import BaseModel

from faultmaven.core.investigation import llm_error_handler, turn_budget
from faultmaven.core.investigation.milestone_engine import generation
from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.milestone_engine.errors import MilestoneEngineError
from faultmaven.core.investigation.turn_budget import bind_turn_deadline
from faultmaven.exceptions import TURN_BUDGET_EXHAUSTED, ExternalCallTimeout
from faultmaven.infrastructure.llm.providers.base import LLMResponse, ToolCall
from faultmaven.infrastructure.llm.structured_output_capability import (
    StructuredOutputCapability,
    StructuredOutputMode,
    StructuredOutputStrategy,
)
from faultmaven.infrastructure.security.case_redaction import CaseRedactionContext
from tests.utils import sockets_refused
from tests.wallclock import virtual_time_module


@pytest.fixture(autouse=True)
def _virtual_clock_no_network(monkeypatch):
    for module in (turn_budget, llm_error_handler, generation):
        monkeypatch.setattr(module, "time", virtual_time_module())
    with sockets_refused():
        yield


class _Schema(BaseModel):
    agent_response: str = "ok"


_TOOL = {
    "type": "function",
    "function": {
        "name": "search_file",
        "description": "s",
        "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
    },
}


def _response(name, arguments):
    return LLMResponse(
        content="",
        confidence=0.9,
        provider="t",
        model="m",
        tokens_used=1,
        response_time_ms=1,
        tool_calls=[
            ToolCall(
                id=name,
                type="function",
                function={"name": name, "arguments": arguments},
            )
        ],
    )


async def _turn(*, good_iterations, good_seconds, hang_seconds, turn_seconds=120):
    """A tool loop whose first ``good_iterations`` calls answer (with a tool
    the turn did not offer, so nothing runs) and whose next call hangs; the
    tool-less fallback answers in 5 s."""
    calls = {"tool_loop": 0, "single_shot": 0}

    async def generate(**kwargs):
        if kwargs.get("messages"):
            calls["tool_loop"] += 1
            if calls["tool_loop"] <= good_iterations:
                await asyncio.sleep(good_seconds)
                return _response("not_offered", "{}")
            await asyncio.sleep(hang_seconds)
            raise ExternalCallTimeout(
                "timed out", service="LLM_Providers", operation="route", timeout=1
            )
        calls["single_shot"] += 1
        await asyncio.sleep(5)
        return _response("_Schema", json.dumps({"agent_response": "answered"}))

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
    provider.route = AsyncMock(side_effect=generate)
    engine = MilestoneEngine(
        llm_provider=provider, repository=MagicMock(), investigation_tools=None
    )
    with bind_turn_deadline(turn_seconds):
        try:
            result = await engine.generator._generate_structured_output_inner(
                "prompt",
                _Schema,
                investigation_tools=[_TOOL],
                tool_context=MagicMock(),
                redaction_ctx=CaseRedactionContext(
                    case_id="c-1294", sanitizer=None, enabled=False
                ),
            )
            return result.agent_response, calls
        except MilestoneEngineError as exc:
            return exc.error_code, calls


@pytest.mark.integration
@pytest.mark.asyncio
class TestTheCarriedCostIsTheFailingCalls:
    async def test_a_late_hang_whose_retry_fits_gets_its_fallback_attempt(self):
        """30 s of good iterations, then a 30 s hang, at T = 30, A = 120: the
        retry costs 2 + 30 s against 55.5 s left, so it is made and answers."""
        outcome, calls = await _turn(
            good_iterations=2, good_seconds=15, hang_seconds=30
        )

        assert outcome == "answered"
        assert calls == {"tool_loop": 3, "single_shot": 1}

    async def test_a_hang_whose_retry_does_not_fit_is_refused(self):
        """70 s of good iterations, then a 30 s hang: 2 + 30 s against 15.5 s
        left. The paid retry is lost, so the turn says the budget refused it."""
        outcome, calls = await _turn(
            good_iterations=2, good_seconds=35, hang_seconds=30
        )

        assert outcome == TURN_BUDGET_EXHAUSTED
        assert calls == {"tool_loop": 3, "single_shot": 0}
