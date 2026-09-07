"""fm#1116: a turn with nothing to search goes to the single-shot structured
path with ``ReasoningIntent.INFERENCE`` + an output floor, instead of a tool
loop that pins reasoning to "none" for tools it cannot use.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.milestone_engine import (
    TOOLLESS_INFERENCE_OUTPUT_FLOOR,
    MilestoneEngine,
    _route_toolless_turn_single_shot,
)
from faultmaven.core.investigation.schemas import InvestigationResponse_Diagnosis
from faultmaven.infrastructure.llm.providers import ReasoningIntent
from faultmaven.infrastructure.llm.providers.base import LLMResponse
from faultmaven.modules.case.domain.models import (
    Case,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    UploadedFile,
)


def _case() -> Case:
    return Case(
        user_id="u", enterprise_id="o", title="t", description="VM will not start"
    )


def _evidence() -> Evidence:
    return Evidence(
        evidence_id="ev_000000000001",
        summary="s",
        category=EvidenceCategory.SYMPTOM_EVIDENCE,
        source_type=EvidenceSourceType.USER_DESCRIPTION,
        collected_at=datetime.now(UTC),
        collected_by="u",
        primary_purpose="p",
        source_file_id=None,
        collected_at_turn=1,
    )


@pytest.mark.unit
class TestRoutingPredicate:
    def test_toolless_turn_with_nothing_to_search_routes_single_shot(self):
        assert (
            _route_toolless_turn_single_shot("directed_analysis", _case(), False)
            is True
        )

    def test_forced_tools_stay_on_the_tool_loop(self):
        assert (
            _route_toolless_turn_single_shot("directed_analysis", _case(), True)
            is False
        )

    def test_knowledge_query_stays_on_the_tool_loop(self):
        # kb_qa / web_search live only in the tool loop.
        assert (
            _route_toolless_turn_single_shot("knowledge_query", _case(), False) is False
        )

    def test_searchable_evidence_row_stays_on_the_tool_loop(self):
        case = _case()
        case.evidence.append(_evidence())
        assert (
            _route_toolless_turn_single_shot("directed_analysis", case, False) is False
        )

    def test_searchable_upload_stays_on_the_tool_loop(self):
        case = _case()
        uf = UploadedFile(filename="a.log", size_bytes=10, uploaded_at_turn=1)
        uf.structural_index = "x" * 40
        case.uploaded_files.append(uf)
        assert (
            _route_toolless_turn_single_shot("directed_analysis", case, False) is False
        )


def _engine_with_recording_provider():
    """A provider whose generate() records its kwargs and answers a minimal
    valid Diagnosis body through the JSON (non-tool) path."""
    provider = MagicMock()
    provider.provider_name = "openai"
    provider.config = MagicMock(default_model="gpt-5.6-luna")
    schema = InvestigationResponse_Diagnosis.model_json_schema()
    from faultmaven.infrastructure.llm.structured_output_capability import (
        StructuredOutputCapability,
        create_strategy_for_capability,
    )

    provider.get_structured_output_strategy = MagicMock(
        return_value=create_strategy_for_capability(
            StructuredOutputCapability.BEST_EFFORT, schema
        )
    )
    provider.supports_tool_calling = MagicMock(return_value=True)
    body = '{"agent_response": "ok", "state_updates": {}}'
    provider.generate = AsyncMock(
        return_value=LLMResponse(
            content=body,
            confidence=0.9,
            provider="openai",
            model="gpt-5.6-luna",
            tokens_used=0,
            response_time_ms=0,
        )
    )
    repo = MagicMock()
    repo.save = AsyncMock()
    return (
        MilestoneEngine(
            llm_provider=provider, repository=repo, investigation_tools=None
        ),
        provider,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_single_shot_call_carries_intent_and_floor_when_declared():
    engine, provider = _engine_with_recording_provider()
    await engine._generate_structured_output(
        "prompt",
        InvestigationResponse_Diagnosis,
        case=_case(),
        user_message="hi",
        reasoning_intent=ReasoningIntent.INFERENCE,
        min_output_tokens=TOOLLESS_INFERENCE_OUTPUT_FLOOR,
    )
    kwargs = provider.generate.call_args.kwargs
    assert kwargs["reasoning_intent"] is ReasoningIntent.INFERENCE
    assert kwargs["min_output_tokens"] == TOOLLESS_INFERENCE_OUTPUT_FLOOR
    assert "tools" not in kwargs


@pytest.mark.unit
@pytest.mark.asyncio
async def test_single_shot_call_sends_no_intent_when_not_declared():
    engine, provider = _engine_with_recording_provider()
    await engine._generate_structured_output(
        "prompt", InvestigationResponse_Diagnosis, case=_case(), user_message="hi"
    )
    kwargs = provider.generate.call_args.kwargs
    assert "reasoning_intent" not in kwargs
    assert "min_output_tokens" not in kwargs


@pytest.mark.unit
def test_floor_sits_under_the_structured_output_cap():
    from faultmaven.core.investigation.milestone_engine import (
        STRUCTURED_OUTPUT_MAX_TOKENS,
    )

    # The floor forbids a starvable partition; it must never RAISE the cap.
    assert 0 < TOOLLESS_INFERENCE_OUTPUT_FLOOR < STRUCTURED_OUTPUT_MAX_TOKENS


# ---------------------------------------------------------------------------
# Turn level: process_turn on an engine WITH tools registered
# ---------------------------------------------------------------------------
from faultmaven.modules.case.domain.models import (  # noqa: E402
    CaseState,
    InvestigationProgress,
    ProblemVerification,
)


def _investigating_case() -> Case:
    case = Case(
        case_id="case_1116a0b1c2d3",
        title="VM will not start",
        state=CaseState.INQUIRY,
        user_id="user_test",
        enterprise_id="org_test",
        description="libvirt cannot write its PID file",
        problem_verification=ProblemVerification(
            symptom_statement="VM fails to start", severity="high"
        ),
    )
    case.inquiry.proposed_problem_statement = "VM fails to start"
    case.inquiry.problem_statement_confirmed = True
    case.inquiry.problem_statement_confirmed_at = datetime.now(UTC)
    case.inquiry.decided_to_investigate = True
    case.inquiry.decision_made_at = datetime.now(UTC)
    case.state = CaseState.INVESTIGATING
    case.progress = InvestigationProgress()
    case.current_turn = 8
    return case


def _tool_engine() -> MilestoneEngine:
    repo = MagicMock()
    repo.save = AsyncMock(side_effect=lambda c: c)
    repo.get = AsyncMock(side_effect=lambda cid: None)
    tools = MagicMock()
    tools.get_all_tools.return_value = []
    engine = MilestoneEngine(MagicMock(), repo, investigation_tools=tools)
    engine._generate_structured_output = AsyncMock(
        return_value=InvestigationResponse_Diagnosis(
            agent_response="Checking the mount.", state_updates={}
        )
    )
    return engine


@pytest.mark.unit
@pytest.mark.asyncio
async def test_turn_with_nothing_to_search_takes_the_single_shot_seam_with_intent():
    engine = _tool_engine()
    await engine.process_turn(
        case=_investigating_case(), user_message="df -h shows /var/lib 100%"
    )
    kwargs = engine._generate_structured_output.call_args.kwargs
    assert kwargs.get("investigation_tools") is None
    assert kwargs["reasoning_intent"] is ReasoningIntent.INFERENCE
    assert kwargs["min_output_tokens"] == TOOLLESS_INFERENCE_OUTPUT_FLOOR


@pytest.mark.unit
@pytest.mark.asyncio
async def test_turn_with_searchable_evidence_stays_on_the_tool_loop():
    engine = _tool_engine()
    case = _investigating_case()
    case.evidence.append(_evidence())
    await engine.process_turn(case=case, user_message="df -h shows /var/lib 100%")
    kwargs = engine._generate_structured_output.call_args.kwargs
    assert kwargs.get("investigation_tools") is not None
    assert "reasoning_intent" not in kwargs


# ---------------------------------------------------------------------------
# The single-shot path degrades like the tool path instead of failing the turn
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.asyncio
async def test_single_shot_path_prunes_an_invalid_list_entry_instead_of_failing_the_turn():
    engine, provider = _engine_with_recording_provider()
    body = (
        '{"agent_response": "Checking the mount.", "state_updates": {}, '
        '"suggested_follow_ups": [{"label": "Run df", "action_type": "RUN", '
        '"evidence_need_id": "eneed_06943c2b1feb"}]}'
    )
    provider.generate = AsyncMock(
        return_value=LLMResponse(
            content=body,
            confidence=0.9,
            provider="openai",
            model="gpt-5.6-luna",
            tokens_used=0,
            response_time_ms=0,
        )
    )
    parsed = await engine._generate_structured_output(
        "prompt", InvestigationResponse_Diagnosis, case=_case(), user_message="hi"
    )
    assert parsed.agent_response == "Checking the mount."
    assert not parsed.suggested_follow_ups  # the offending entry was pruned, not fatal
