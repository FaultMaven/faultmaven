"""The engine builds every prompt of a turn for the tools that turn gives (#1939).

The render matrix (``prompts/test_prompt_tools_are_given_tools_1934.py``) pins
what a prompt says for a given set. This file pins that each call site hands
the renderers the RIGHT set:

- a tool-loop turn: the names of the tools the loop sends, for the first build
  and the #614 resize alike (one set per turn, so one cached prefix);
- a single-shot turn, or a provider that cannot call tools: no tools;
- the ``ToolCallingUnsupportedError`` fallback and the #662 minimal retry: no
  tools, since neither sends any;
- a terminal Q&A turn: the registry's tools in ``TERMINAL_QA_TOOLS`` (#1964),
  which are the tools it sends, and the suggestion list its reply is given
  (#1955) — the prompt points at exactly the suggestions on screen.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.core.investigation.milestone_engine import terminal_replies
from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.milestone_engine.errors import MilestoneEngineError
from faultmaven.core.investigation.milestone_engine.terminal_replies import (
    GENERATE_RUNBOOK_PAYLOAD,
    REGENERATE_RESOLUTION_SUMMARY_PAYLOAD,
)
from faultmaven.core.investigation.milestone_engine.terminal_turns import (
    TERMINAL_QA_TOOLS,
)
from faultmaven.core.investigation.milestone_engine.turn_commit import TurnCommitPlan
from faultmaven.core.investigation.prompts.templates.tool_text import (
    NO_TOOLS,
    tools_line,
)
from faultmaven.core.investigation.schemas import (
    InvestigationResponse_Diagnosis,
    TerminalResponse,
)
from faultmaven.exceptions import TOKEN_LIMIT
from faultmaven.infrastructure.llm.prompt_cache import CACHE_BOUNDARY
from faultmaven.infrastructure.security.case_redaction import CaseRedactionContext
from faultmaven.modules.case.contracts import (
    CaseState,
    InvestigationProgress,
    ProblemVerification,
)
from faultmaven.modules.case.domain.models.case import Case
from faultmaven.modules.case.domain.models.evidence import (
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
)
from tests.model_content import disabled_redaction

#: A redaction context that redacts nothing: the terminal Q&A path takes the
#: turn's context as a required argument (#1956).
_DISABLED_REDACTION = CaseRedactionContext("case", None, enabled=False)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

#: Snake_case words that are tool names; the prompts' own field names
#: (``evidence_to_add`` …) are not in it.
_TOOL_NAMES = {
    "search_file",
    "deep_analysis",
    "kb_qa",
    "case_evidence_search",
    "web_search",
    "vectorize_file",
    "reclassify_evidence",
    "find_entity",
    "list_top_entities",
}
_TOKEN = re.compile(r"(?<![\w.])[a-z]+(?:_[a-z]+)+(?!\w|\.\w)")


def _named(prompt: str) -> set[str]:
    return set(_TOKEN.findall(prompt)) & _TOOL_NAMES


def _registry(*names: str):
    tools = []
    for name in names:
        tool = MagicMock()
        tool.name = name
        tool.get_schema.return_value = {"name": name, "parameters": {}}
        tools.append(tool)
    registry = MagicMock()
    registry.get_all_tools.return_value = tools
    return registry


def _engine(*names: str, capable: bool = True, response=None) -> MilestoneEngine:
    repo = MagicMock()
    repo.save = AsyncMock(side_effect=lambda c, **_: c)
    repo.get = AsyncMock(side_effect=lambda cid: None)
    provider = MagicMock()
    provider.supports_tool_calling = lambda model: capable
    engine = MilestoneEngine(provider, repo, investigation_tools=_registry(*names))
    engine.generator.generate_structured_output = AsyncMock(
        return_value=response
        or InvestigationResponse_Diagnosis(agent_response="ok", state_updates={})
    )
    return engine


def _case(with_evidence: bool = True) -> Case:
    case = Case(
        case_id="case_1939a0b1c2d3",
        title="VM will not start",
        state=CaseState.INQUIRY,
        user_id="u",
        enterprise_id="o",
        description="libvirt cannot write its PID file",
        problem_verification=ProblemVerification(
            symptom_statement="VM fails to start", severity="high"
        ),
    )
    case.inquiry.proposed_problem_statement = "VM fails to start"
    case.inquiry.problem_statement_confirmed = True
    case.inquiry.problem_statement_confirmed_at = datetime.now(UTC)
    case.state = CaseState.INVESTIGATING
    case.progress = InvestigationProgress()
    case.current_turn = 8
    if with_evidence:
        case.evidence.append(
            Evidence(
                evidence_id="ev_000000000001",
                summary="s",
                category=EvidenceCategory.SYMPTOM_EVIDENCE,
                source_type=EvidenceSourceType.LOGS,
                collected_at=datetime.now(UTC),
                collected_by="u",
                primary_purpose="p",
                source_file_id="file_0123456789ab",
                collected_at_turn=1,
            )
        )
    return case


def _prefix(prompt: str) -> str:
    return prompt[: prompt.index(CACHE_BOUNDARY)]


_TOOLS = ("search_file", "kb_qa", "case_evidence_search")
#: Every tool the registry can hold: the default six and both flag-gated
#: families.
_EVERY_TOOL = tuple(sorted(_TOOL_NAMES))


async def test_a_tool_loop_turn_names_the_tools_the_loop_sends():
    engine = _engine(*_TOOLS)
    await engine.process_turn(
        case=_case(), user_message="what does the log show?", redaction_ctx=None
    )

    call = engine.generator.generate_structured_output.call_args
    prompt = call.args[0]
    sent = {t["function"]["name"] for t in call.kwargs["investigation_tools"]}
    assert sent == set(_TOOLS)
    assert tools_line(frozenset(sent)) in prompt
    assert _named(prompt) == sent
    # The tools run with the set the prompt was built for (#1977): a result
    # that points at another tool (an empty ``case_evidence_search`` at
    # ``search_file``) names only what this turn gives.
    assert call.kwargs["tool_context"].given_tools == frozenset(sent)
    # The #614 resize builds for the same set: the cached prefix is the
    # first build's, byte for byte.
    resized = call.kwargs["base_prompt_builder"](
        target_tokens=10**6, provider_name=None, model_name=None
    )
    assert _prefix(resized) == _prefix(prompt)
    # The non-tool fallback is built for no tools.
    fallback = call.kwargs["fallback_prompt_builder"]()
    assert tools_line(NO_TOOLS) in fallback
    assert _named(fallback) == set()


async def test_a_single_shot_turn_names_no_tool():
    """Nothing to search: the turn takes the single-shot route, which sends
    no tools, so its prompt names none — though the registry has them."""
    engine = _engine(*_TOOLS)
    await engine.process_turn(
        case=_case(with_evidence=False),
        user_message="df -h shows /var 100%",
        redaction_ctx=None,
    )

    call = engine.generator.generate_structured_output.call_args
    assert call.kwargs.get("investigation_tools") is None
    prompt = call.args[0]
    assert tools_line(NO_TOOLS) in prompt
    assert _named(prompt) == set()


async def test_a_provider_that_cannot_call_tools_gets_a_prompt_naming_none():
    """The Layer-1 pre-check will take the non-tool path, so nothing is
    given even on a turn the tool loop is asked for."""
    engine = _engine(*_TOOLS, capable=False)
    await engine.process_turn(
        case=_case(), user_message="what does the log show?", redaction_ctx=None
    )

    call = engine.generator.generate_structured_output.call_args
    prompt = call.args[0]
    assert tools_line(NO_TOOLS) in prompt
    assert _named(prompt) == set()
    # Nothing was given, so there is no tool-named prompt to rebuild, and the
    # tool context states the same (#1977).
    assert call.kwargs["fallback_prompt_builder"] is None
    assert call.kwargs["tool_context"].given_tools == NO_TOOLS


async def test_the_minimal_retry_is_built_for_no_tools():
    """#662: the context-overflow retry drops the tools, and so does its
    prompt."""
    engine = _engine(*_TOOLS)
    overflow = MilestoneEngineError(
        "Structured output generation failed: Context too large.",
        error_code=TOKEN_LIMIT,
    )
    inner = AsyncMock(side_effect=[overflow, MagicMock(name="degraded")])
    case = _case()
    with patch.object(engine.generator, "_generate_structured_output_inner", inner):
        await type(engine.generator).generate_structured_output(
            engine.generator,
            "a prompt",
            MagicMock(),
            investigation_tools=[{"function": {"name": "search_file"}}],
            tool_context=MagicMock(),
            case=case,
            user_message="what is wrong?",
            redaction_ctx=disabled_redaction(),
        )

    retry_prompt = inner.await_args_list[1].args[0]
    assert tools_line(NO_TOOLS) in retry_prompt
    assert _named(retry_prompt) == set()


@pytest.mark.parametrize("draft_exists", [False, True])
async def test_the_terminal_prompt_points_at_the_suggestions_its_reply_gets(
    draft_exists, monkeypatch
):
    """#1955 at the call site: one list decides both the redirects the prompt
    renders and the suggestions the reply is given."""
    monkeypatch.setattr(terminal_replies, "runbook_conversion_ready", lambda c: True)
    engine = _engine(
        *_EVERY_TOOL, response=TerminalResponse(agent_response="ok", state_updates={})
    )
    engine.runbooks.case_has_runbook_draft = AsyncMock(return_value=draft_exists)
    case = _case()
    object.__setattr__(case, "state", CaseState.RESOLVED)

    result = await engine.terminal._process_terminal_qa(
        case,
        "write me a runbook",
        metadata={},
        plan=TurnCommitPlan(),
        redaction_ctx=_DISABLED_REDACTION,
    )

    call = engine.generator.generate_structured_output.call_args
    prompt = call.args[0]
    payloads = {s["payload"] for s in result["suggested_follow_ups"]}
    assert REGENERATE_RESOLUTION_SUMMARY_PAYLOAD in payloads
    assert "use the **Regenerate** suggestion" in prompt
    offered = GENERATE_RUNBOOK_PAYLOAD in payloads
    assert offered is not draft_exists
    assert ("Generate a runbook from this\nresolved case** suggestion" in prompt) is (
        offered
    )
    assert ("already exists in the case creator's" in prompt) is draft_exists
    # #1964: the call site offers the registry's read-only lookups, and the
    # prompt names exactly what it sends.
    sent = {t["function"]["name"] for t in call.kwargs["investigation_tools"]}
    assert sent == set(_EVERY_TOOL) & TERMINAL_QA_TOOLS
    assert tools_line(frozenset(sent)) in prompt
    assert _named(prompt) == sent
    # The tools run with the set the prompt names, not the registry's (#1977).
    assert call.kwargs["tool_context"].given_tools == frozenset(sent)
    resized = call.kwargs["base_prompt_builder"](
        target_tokens=10**6, provider_name=None, model_name=None
    )
    assert tools_line(frozenset(sent)) in resized
    fallback = call.kwargs["fallback_prompt_builder"]()
    assert tools_line(NO_TOOLS) in fallback and _named(fallback) == set()


async def test_the_terminal_turn_offers_nothing_on_an_exhausted_closed_case(
    monkeypatch,
):
    """A CLOSED case whose regenerations are spent offers no suggestion, and
    its prompt points at none — the redirect names only where the summary is."""
    from faultmaven.core.investigation.milestone_engine import terminal_turns

    async def _none_left(*a, **k):
        return 0

    monkeypatch.setattr(terminal_turns, "_remaining_regens_for", _none_left)
    engine = _engine(
        *_TOOLS, response=TerminalResponse(agent_response="ok", state_updates={})
    )
    case = _case()
    object.__setattr__(case, "state", CaseState.CLOSED)

    result = await engine.terminal._process_terminal_qa(
        case,
        "summarise it",
        metadata={},
        plan=TurnCommitPlan(),
        redaction_ctx=_DISABLED_REDACTION,
    )

    prompt = engine.generator.generate_structured_output.call_args.args[0]
    assert result["suggested_follow_ups"] == []
    assert "suggestion" not in prompt.split("SUMMARY REQUESTS:")[1].split(
        "FOLLOW-UP SUGGESTIONS"
    )[0].replace("suggested_follow_ups", "")
    assert "No runbook can be generated from this case here" in prompt
