"""The tools a terminal Q&A turn is offered, and the tools its loop runs (#1964).

A resolved or closed case still answers questions, through a tool loop. That
loop used to be handed the whole registry (``deep_analysis``, ``web_search``,
``vectorize_file``, ``reclassify_evidence`` included) beside a template that
forbids new investigation, and the loop ran any registered tool the model
named, offered or not.

The invariant: a terminal Q&A turn is offered only read-only lookups of this
case's record and the knowledge base (``registry ∩ TERMINAL_QA_TOOLS``); it
executes only tools it was offered; beyond what every turn writes it writes
nothing; and its system message names exactly the offered set.

This file holds the shared harness (a scripted provider and a fake registry,
so the real terminal handler and the real tool loop run with no model call)
and the cases T3, T4, T6 and T8. The invariant's own pins — T2 (a withheld
tool named by the model is refused) and T9 (a terminal turn writes nothing) —
are the ``test_inv09_*`` tests in ``test_lifecycle_invariants.py``, built on
this harness. T1 is the call-site pin in
``test_turn_prompts_name_the_given_tools_1939.py``; the message columns (T5,
T7) are in the #1934 render matrix.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.milestone_engine.errors import MilestoneEngineError
from faultmaven.core.investigation.milestone_engine.terminal_turns import (
    TERMINAL_QA_TOOLS,
)
from faultmaven.core.investigation.milestone_engine.tool_messages import (
    _TERMINAL_TOOL_LINES,
    _build_da_system_instruction,
)
from faultmaven.core.investigation.milestone_engine.turn_commit import TurnCommitPlan
from faultmaven.core.investigation.prompts.templates.tool_text import (
    NO_TOOLS,
    tools_line,
)
from faultmaven.core.investigation.schemas import TerminalResponse
from faultmaven.infrastructure.llm.providers import StopReason
from faultmaven.infrastructure.llm.providers.base import LLMResponse, ToolCall
from faultmaven.infrastructure.llm.structured_output_capability import (
    StructuredOutputCapability,
)
from faultmaven.infrastructure.security.case_redaction import CaseRedactionContext
from faultmaven.models.interfaces import ToolResult
from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    UploadedFile,
)

pytestmark = pytest.mark.unit

#: What ``_create_investigation_tools`` registers by default.
DEFAULT_SIX = (
    "search_file",
    "vectorize_file",
    "deep_analysis",
    "kb_qa",
    "case_evidence_search",
    "web_search",
)
ENTITY_TOOLS = ("find_entity", "list_top_entities")

#: Both terminal states: every invariant test runs on each.
TERMINAL_STATES = [CaseState.CLOSED, CaseState.RESOLVED]

EV_ID = "ev_196400000001"
FILE_ID = "file_196400000001"
SCHEMA_TOOL = "TerminalResponse"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _response(*calls: tuple[str, dict]) -> LLMResponse:
    return LLMResponse(
        content="",
        confidence=0.9,
        provider="test",
        model="test-model",
        tokens_used=10,
        response_time_ms=5,
        tool_calls=[
            ToolCall(
                id=f"call_{i}_{name}",
                type="function",
                function={"name": name, "arguments": json.dumps(args)},
            )
            for i, (name, args) in enumerate(calls)
        ],
        stop_reason=StopReason.TOOL_CALLS,
    )


def answer(text: str = "The log shows the upstream timeouts.") -> LLMResponse:
    """The schema-tool call that ends the loop."""
    return _response(
        (SCHEMA_TOOL, {"agent_response": text, "state_updates": {}}),
    )


def calls(*named: tuple[str, dict]) -> LLMResponse:
    """A model turn that calls these tools."""
    return _response(*named)


class ScriptedProvider:
    """A tool-calling provider that answers from a script, and records each
    request. No model is called."""

    provider_name = "test"

    def __init__(self, *script: LLMResponse) -> None:
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []

    def supports_tool_calling(self, model: Any = None) -> bool:
        return True

    def get_structured_output_capability(self) -> StructuredOutputCapability:
        return StructuredOutputCapability.FUNCTION_CALLING

    async def route(self, **kwargs: Any) -> LLMResponse:
        self.requests.append(kwargs)
        return self.script.pop(0)

    def system_message(self) -> str:
        """The system message the loop opened with."""
        first = self.requests[0]["messages"][0]
        assert first["role"] == "system"
        return first["content"]

    def offered(self) -> set[str]:
        """The investigation tools the first request offered."""
        return {t["function"]["name"] for t in self.requests[0]["tools"]} - {
            SCHEMA_TOOL
        }

    def tool_results(self) -> dict[str, str]:
        """Each relayed tool result, by tool name (the last one wins)."""
        return dict(self.relayed())

    def relayed(self) -> list[tuple[str, str]]:
        """Every relayed tool result, in order, as (tool name, content)."""
        return [
            (m["name"], m["content"])
            for m in self.requests[-1]["messages"]
            if m.get("role") == "tool"
        ]


class FakeRegistry:
    """The registry seam: what is registered, and what was executed."""

    def __init__(self, *names: str, results: dict[str, ToolResult] | None = None):
        self._tools = []
        for name in names:
            tool = MagicMock()
            tool.name = name
            tool.get_schema.return_value = {
                "name": name,
                "description": f"the {name} tool",
                "parameters": {"type": "object", "properties": {}},
            }
            self._tools.append(tool)
        self.results = results or {}
        self.executed: list[str] = []

    def get_all_tools(self):
        return list(self._tools)

    async def execute_tool(self, name: str, params: dict, context: Any) -> ToolResult:
        self.executed.append(name)
        return self.results.get(name, ToolResult(success=True, data=f"{name} ran"))


def terminal_case(state: CaseState = CaseState.CLOSED) -> Case:
    """A terminal case with one uploaded file large enough to vectorize, and
    the evidence row backed by it, not yet vectorized."""
    now = datetime.now(UTC)
    stamp = (
        {"closed_at": now, "closure_reason": "solution_deferred"}
        if state == CaseState.CLOSED
        else {"resolved_at": now, "closed_at": now}
    )
    return Case(
        created_at=now,
        case_id="case_1964aaaaaaaa",
        title="API timeouts",
        description="Upstream timeouts since 10:00",
        user_id="user_1964",
        enterprise_id="org_1964",
        state=state,
        current_turn=6,
        uploaded_files=[
            UploadedFile(
                file_id=FILE_ID,
                filename="app.log",
                size_bytes=900_000,
                content_type="text/plain",
                uploaded_at_turn=1,
                upload_source="file_upload",
                storage_ref=f"evidence/case_1964/{FILE_ID}.txt",
                data_type="logs",
                summary="App log.",
            )
        ],
        evidence=[
            Evidence(
                evidence_id=EV_ID,
                source_file_id=FILE_ID,
                summary="upstream timeouts",
                extract="ERROR upstream timeout",
                category=EvidenceCategory.SYMPTOM_EVIDENCE,
                source_type=EvidenceSourceType.LOGS,
                primary_purpose="Test",
                collected_by="user_1964",
                collected_at_turn=1,
            )
        ],
        **stamp,
    )


def terminal_engine(provider: ScriptedProvider, registry: FakeRegistry):
    repo = MagicMock()
    repo.save = AsyncMock(side_effect=lambda c, **_: c)
    repo.get = AsyncMock(return_value=None)
    repo.update_evidence_vectorized = AsyncMock(return_value=True)
    engine = MilestoneEngine(provider, repo, investigation_tools=registry)
    engine.runbooks.case_has_runbook_draft = AsyncMock(return_value=False)
    engine.vectorizer.start_proactive_vectorization = AsyncMock(return_value={})
    return engine, repo


async def run_terminal_qa(engine, case: Case, message: str = "what did the log say?"):
    return await engine.terminal._process_terminal_qa(
        case,
        message,
        metadata={},
        plan=TurnCommitPlan(),
        redaction_ctx=CaseRedactionContext(case.case_id, None, enabled=False),
    )


# ---------------------------------------------------------------------------
# The set and the message builder
# ---------------------------------------------------------------------------


def test_the_terminal_set_excludes_new_investigation_and_writes():
    assert TERMINAL_QA_TOOLS == {
        "search_file",
        "case_evidence_search",
        "kb_qa",
        "find_entity",
        "list_top_entities",
    }
    assert not TERMINAL_QA_TOOLS & {
        "deep_analysis",
        "web_search",
        "vectorize_file",
        "reclassify_evidence",
    }


def test_every_terminal_tool_has_its_one_guidance_line():
    """The builder can describe exactly the set the handler offers."""
    assert set(_TERMINAL_TOOL_LINES) == TERMINAL_QA_TOOLS


@pytest.mark.parametrize("stray", ["deep_analysis", "web_search", "vectorize_file"])
def test_the_terminal_message_refuses_a_tool_outside_the_set(stray):
    with pytest.raises(ValueError, match=stray):
        _build_da_system_instruction(["kb_qa", stray], "Schema", terminal=True)


def test_the_terminal_message_is_never_built_for_no_tools():
    with pytest.raises(ValueError):
        _build_da_system_instruction([], "Schema", terminal=True)


def test_the_search_file_line_says_how_to_find_an_id():
    """The terminal prompt renders no evidence block, so the evidence ids
    reach the model through search_file's own miss error."""
    text = _build_da_system_instruction(["search_file"], "Schema", terminal=True)
    assert (
        "It needs an evidence_id or a file_id; a call with an id that is not on "
        "this case returns the list of every file it can read, with its id."
    ) in text


@pytest.mark.parametrize("state", TERMINAL_STATES)
async def test_a_terminal_loop_offered_a_withheld_tool_fails_before_any_call(state):
    """The call-site filter is the first line; the loop refuses an offer
    outside the set loudly rather than sending it."""
    provider = ScriptedProvider(answer())
    engine, _ = terminal_engine(provider, FakeRegistry(*DEFAULT_SIX))
    case = terminal_case(state)

    with pytest.raises(MilestoneEngineError, match="deep_analysis"):
        await engine.generator._tool_augmented_generate(
            prompt="p",
            schema_model=TerminalResponse,
            investigation_tools=engine.generator.build_da_tool_schemas(),
            tool_context=MagicMock(),
            case=case,
        )
    assert provider.requests == []


# ---------------------------------------------------------------------------
# T3 / T4: flag-gated tools on a terminal turn
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", TERMINAL_STATES)
async def test_t3_the_entity_tools_are_offered_when_registered(state):
    provider = ScriptedProvider(answer())
    engine, _ = terminal_engine(provider, FakeRegistry(*DEFAULT_SIX, *ENTITY_TOOLS))

    await run_terminal_qa(engine, terminal_case(state))

    expected = {
        "search_file",
        "case_evidence_search",
        "kb_qa",
        "find_entity",
        "list_top_entities",
    }
    assert provider.offered() == expected
    message = provider.system_message()
    assert tools_line(frozenset(expected)) in message
    for name in ENTITY_TOOLS:
        assert _TERMINAL_TOOL_LINES[name] in message


@pytest.mark.parametrize("state", TERMINAL_STATES)
async def test_t4_reclassify_evidence_is_not_offered_and_is_refused_if_named(state):
    provider = ScriptedProvider(
        calls(("reclassify_evidence", {"evidence_id": EV_ID, "data_type": "logs"})),
        answer(),
    )
    registry = FakeRegistry(*DEFAULT_SIX, "reclassify_evidence")
    engine, _ = terminal_engine(provider, registry)
    case = terminal_case(state)
    before = case.model_dump()

    result = await run_terminal_qa(engine, case)

    assert "reclassify_evidence" not in provider.offered()
    assert "reclassify_evidence" not in provider.system_message()
    assert registry.executed == []
    relayed = provider.tool_results()["reclassify_evidence"]
    assert relayed.startswith(
        "Error: Tool 'reclassify_evidence' is not available on this turn."
    )
    assert tools_line(frozenset(provider.offered())) in relayed
    assert result["agent_response"]
    assert case.model_dump() == before


# ---------------------------------------------------------------------------
# T6: nothing of the terminal set is registered
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", TERMINAL_STATES)
async def test_t6_no_terminal_tool_registered_sends_no_tools(state):
    engine, _ = terminal_engine(
        ScriptedProvider(),
        FakeRegistry("deep_analysis", "web_search", "vectorize_file"),
    )
    engine.generator.generate_structured_output = AsyncMock(
        return_value=TerminalResponse(agent_response="ok", state_updates={})
    )
    tool_loop = AsyncMock()
    engine.generator._tool_augmented_generate = tool_loop

    await run_terminal_qa(engine, terminal_case(state))

    call = engine.generator.generate_structured_output.call_args
    # No tools, so the tool loop (the only builder of a DA message) is never
    # entered, and there is no tool-named prompt to rebuild.
    assert "investigation_tools" not in call.kwargs
    assert "tool_context" not in call.kwargs
    assert "fallback_prompt_builder" not in call.kwargs
    assert tools_line(NO_TOOLS) in call.args[0]
    tool_loop.assert_not_awaited()


# ---------------------------------------------------------------------------
# T8: the dispatch gate on an investigating turn
# ---------------------------------------------------------------------------

_SEARCH_FILE_SCHEMA = [
    {
        "type": "function",
        "function": {"name": "search_file", "parameters": {"type": "object"}},
    }
]


@pytest.mark.parametrize(
    "name",
    [
        # Not registered at all: the registry's own miss used to answer it,
        # as "Error: Tool 'x' not found".
        "kubectl_delete_prod",
        # Registered but not offered on this call: it used to run.
        "deep_analysis",
    ],
)
async def test_t8_an_investigating_turn_runs_only_what_it_offered(name):
    provider = ScriptedProvider(
        calls((name, {"evidence_id": EV_ID})),
        _response(("SampleResponse", {"agent_response": "done"})),
    )
    registry = FakeRegistry("search_file", "deep_analysis")
    engine, _ = terminal_engine(provider, registry)

    from pydantic import BaseModel

    class SampleResponse(BaseModel):
        agent_response: str = ""

    await engine.generator._tool_augmented_generate(
        prompt="p",
        schema_model=SampleResponse,
        investigation_tools=_SEARCH_FILE_SCHEMA,
        tool_context=MagicMock(),
        case=None,
    )

    assert registry.executed == []
    assert provider.tool_results()[name] == (
        f"Error: Tool '{name}' is not available on this turn. "
        "Tools available on this turn: search_file."
    )


async def test_t8_an_offered_tool_still_runs():
    """Positive control for the gate: it refuses only what was not offered."""
    provider = ScriptedProvider(
        calls(("search_file", {"evidence_id": EV_ID, "query": "timeout"})),
        _response(("SampleResponse", {"agent_response": "done"})),
    )
    registry = FakeRegistry("search_file", "deep_analysis")
    engine, _ = terminal_engine(provider, registry)

    from pydantic import BaseModel

    class SampleResponse(BaseModel):
        agent_response: str = ""

    await engine.generator._tool_augmented_generate(
        prompt="p",
        schema_model=SampleResponse,
        investigation_tools=_SEARCH_FILE_SCHEMA,
        tool_context=MagicMock(),
        case=None,
    )

    assert registry.executed == ["search_file"]


# ---------------------------------------------------------------------------
# A file no evidence row records is reachable on a terminal turn (#1964 review)
# ---------------------------------------------------------------------------


class _Storage:
    """Object storage holding the one uploaded file."""

    async def retrieve_file(self, storage_ref: str) -> bytes:
        assert storage_ref == f"evidence/case_1964/{FILE_ID}.txt", storage_ref
        return b"10:01 INFO ok\n10:02 ERROR upstream timeout req=7\n10:03 INFO ok\n"


class _RealSearchFile(FakeRegistry):
    """The fake registry, with the REAL search_file behind its name."""

    def __init__(self, *names: str):
        super().__init__(*names)
        from faultmaven.modules.agent.tools.search_file_tool import SearchFileTool

        self._search_file = SearchFileTool(storage_service=_Storage())

    async def execute_tool(self, name: str, params: dict, context: Any) -> ToolResult:
        if name != "search_file":
            return await super().execute_tool(name, params, context)
        self.executed.append(name)
        return await self._search_file.execute_with_context(params, context)


@pytest.mark.parametrize("state", TERMINAL_STATES)
async def test_a_file_no_evidence_row_records_is_listed_and_read(state):
    """The terminal search_file line promises that a call with an id not on
    the case lists every file the tool reads. A paste no evidence row named,
    or every upload of a case closed before any row existed, is read by its
    file_id, so the list names it by file_id; the model reads it with that."""
    case = terminal_case(state).model_copy(update={"evidence": []})
    provider = ScriptedProvider(
        calls(("search_file", {"evidence_id": "ev_guess", "query": "10:02"})),
        calls(("search_file", {"evidence_id": FILE_ID, "query": "10:02"})),
        answer("At 10:02 the upstream timed out (app.log, line 2)."),
    )
    registry = _RealSearchFile(*DEFAULT_SIX)
    engine, _ = terminal_engine(provider, registry)
    before = case.model_dump()

    result = await run_terminal_qa(engine, case)

    (_, miss), (_, hit) = provider.relayed()
    assert miss.startswith("Error: Evidence ev_guess not found in this case.")
    assert f"{FILE_ID} (app.log)" in miss
    assert '"results_count": 1' in hit and "10:02 ERROR upstream timeout" in hit
    assert registry.executed == ["search_file", "search_file"]
    assert result["agent_response"].startswith("At 10:02")
    assert case.model_dump() == before
