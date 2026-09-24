"""The engine names an unusable answer by the provider's stop reason (#1442).

Owner ruling 2026-09-19: ``MilestoneEngine`` owns response synthesis, because it
is the only layer that can see WHY the answer is unusable; the service is a
persistence backstop. It replaced one fabricated reply ("I've updated the
investigation based on the latest information.") — written for every cause, and
quoted back to the model as its own words — with one placeholder per failure
shape, keyed on the NORMALISED :class:`StopReason`, and an explicit
no-placeholder arm for ``TOOL_CALLS``.

Each row of the matrix is pinned by EXACT text on the path that produces it, so
collapsing the matrix back to a single placeholder — any one of the four, for
every reason — fails here.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation import milestone_engine as me
from faultmaven.core.investigation.milestone_engine import (
    RESPONSE_EMPTY_TEXT,
    RESPONSE_NO_SIGNAL_TEXT,
    RESPONSE_TRUNCATED_TEXT,
    RESPONSE_WITHHELD_TEXT,
    MilestoneEngine,
    is_agent_response_synthesized,
    synthesized_agent_response,
)
from faultmaven.core.investigation.schemas import BaseInteractionResponse
from faultmaven.infrastructure.llm.providers.base import (
    LLMResponse,
    StopReason,
    ToolCall,
)

MATRIX = [
    (StopReason.CONTENT_FILTER, RESPONSE_WITHHELD_TEXT),
    (StopReason.MAX_TOKENS, RESPONSE_TRUNCATED_TEXT),
    (StopReason.STOP, RESPONSE_EMPTY_TEXT),
    (StopReason.UNKNOWN, RESPONSE_NO_SIGNAL_TEXT),
]
MATRIX_IDS = [reason.value for reason, _ in MATRIX]


class _Resp(BaseInteractionResponse):
    """The production base, so the private flag is the real one."""

    note: str = "kept"


def _llm(content: str = "", *, stop_reason: StopReason, tool_calls=None):
    return LLMResponse(
        content=content,
        confidence=0.9,
        provider="test",
        model="test-model",
        tokens_used=10,
        response_time_ms=5,
        tool_calls=tool_calls,
        stop_reason=stop_reason,
    )


@pytest.mark.unit
class TestTheMatrix:
    @pytest.mark.parametrize("reason,text", MATRIX, ids=MATRIX_IDS)
    def test_each_failure_shape_has_its_own_text(self, reason, text):
        assert synthesized_agent_response(reason) == text

    def test_the_four_texts_are_distinct(self):
        texts = [text for _, text in MATRIX]
        assert len(set(texts)) == 4

    def test_tool_calls_gets_no_placeholder(self):
        """A response that stopped to hand control to a tool is not a failure."""
        assert synthesized_agent_response(StopReason.TOOL_CALLS) is None

    def test_every_stop_reason_has_an_arm(self):
        """A reason added to the enum must be decided here, not borrow a row."""
        for reason in StopReason:
            synthesized_agent_response(reason)  # raises on an unhandled member
        assert {r for r, _ in MATRIX} | {StopReason.TOOL_CALLS} == set(StopReason)

    def test_the_fabricated_reply_is_gone(self):
        """The old single string must not survive as any row's text."""
        for reason in StopReason:
            assert "I've updated the investigation" not in (
                synthesized_agent_response(reason) or ""
            )


def _single_shot_engine(content: dict, stop_reason: StopReason) -> MilestoneEngine:
    from faultmaven.infrastructure.llm.structured_output_capability import (
        StructuredOutputCapability,
        StructuredOutputMode,
        StructuredOutputStrategy,
    )

    provider = MagicMock()
    provider.generate = AsyncMock(
        return_value=_llm(json.dumps(content), stop_reason=stop_reason)
    )
    provider.get_structured_output_strategy = MagicMock(
        return_value=StructuredOutputStrategy(
            capability=StructuredOutputCapability.BEST_EFFORT,
            mode=StructuredOutputMode.JSON_OBJECT,
            include_schema_in_prompt=True,
            response_format={"type": "json_object"},
        )
    )
    repo = MagicMock()
    repo.save = AsyncMock()
    return MilestoneEngine(
        llm_provider=provider, repository=repo, investigation_tools=MagicMock()
    )


@pytest.mark.unit
@pytest.mark.asyncio
class TestTheSingleShotPath:
    """``_generate_structured_output_inner`` — the path fm#1116's tool-less
    turns, tool-incapable models and every tool-loop fallback take."""

    @pytest.mark.parametrize("reason,text", MATRIX, ids=MATRIX_IDS)
    async def test_an_empty_answer_is_named_by_its_stop_reason(self, reason, text):
        engine = _single_shot_engine({"agent_response": ""}, reason)
        parsed = await engine._generate_structured_output_inner("p", _Resp)

        assert parsed.agent_response == text
        assert is_agent_response_synthesized(parsed)

    @pytest.mark.parametrize("reason,text", MATRIX, ids=MATRIX_IDS)
    async def test_a_missing_answer_is_named_by_its_stop_reason(self, reason, text):
        """The rung that used to write the fabricated reply now only blanks
        the field; the wording comes from here, and the model's other fields
        survive."""
        engine = _single_shot_engine({"note": "model's own"}, reason)
        parsed = await engine._generate_structured_output_inner("p", _Resp)

        assert parsed.agent_response == text
        assert parsed.note == "model's own"
        assert is_agent_response_synthesized(parsed)

    async def test_tool_calls_leaves_the_empty_answer_unwritten(self):
        engine = _single_shot_engine({"agent_response": ""}, StopReason.TOOL_CALLS)
        parsed = await engine._generate_structured_output_inner("p", _Resp)

        assert parsed.agent_response == ""
        assert not is_agent_response_synthesized(parsed)

    @pytest.mark.parametrize("reason", list(StopReason), ids=lambda r: r.value)
    async def test_a_usable_answer_is_never_overwritten(self, reason):
        """Whatever the stop reason says — a filter can stop a body that still
        carries a whole answer."""
        engine = _single_shot_engine({"agent_response": "pool exhausted"}, reason)
        parsed = await engine._generate_structured_output_inner("p", _Resp)

        assert parsed.agent_response == "pool exhausted"
        assert not is_agent_response_synthesized(parsed)


def _schema_call(data: dict, *, stop_reason: StopReason) -> LLMResponse:
    return _llm(
        stop_reason=stop_reason,
        tool_calls=[
            ToolCall(
                id="call_schema",
                type="function",
                function={"name": "_Resp", "arguments": json.dumps(data)},
            )
        ],
    )


async def _tool_loop(response: LLMResponse):
    provider = AsyncMock()
    provider.generate = AsyncMock(return_value=response)
    repo = MagicMock()
    repo.save = AsyncMock()
    engine = MilestoneEngine(
        llm_provider=provider, repository=repo, investigation_tools=MagicMock()
    )
    tools = [
        {
            "type": "function",
            "function": {
                "name": "search_file",
                "description": "Search files",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    return await engine._tool_augmented_generate(
        prompt="p",
        schema_model=_Resp,
        investigation_tools=tools,
        tool_context=MagicMock(),
    )


@pytest.mark.unit
@pytest.mark.asyncio
class TestTheToolLoopSchemaCall:
    """The tool loop's own exit: the model answers by calling the schema tool.
    Gemini reports ``STOP`` on a function call, so this is where the
    gemini-3.5-flash omission the rung was written for actually lands."""

    @pytest.mark.parametrize("reason,text", MATRIX, ids=MATRIX_IDS)
    async def test_an_omitted_answer_is_named_by_its_stop_reason(self, reason, text):
        parsed = await _tool_loop(_schema_call({"note": "x"}, stop_reason=reason))

        assert parsed.agent_response == text
        assert is_agent_response_synthesized(parsed)

    async def test_tool_calls_leaves_it_to_the_backstop(self):
        parsed = await _tool_loop(
            _schema_call({"agent_response": ""}, stop_reason=StopReason.TOOL_CALLS)
        )

        assert parsed.agent_response == ""
        assert not is_agent_response_synthesized(parsed)

    async def test_a_real_answer_passes_untouched(self):
        parsed = await _tool_loop(
            _schema_call(
                {"agent_response": "found it"}, stop_reason=StopReason.TOOL_CALLS
            )
        )
        assert parsed.agent_response == "found it"
        assert not is_agent_response_synthesized(parsed)


@pytest.mark.unit
def test_the_text_recovery_path_still_rejects_an_empty_answer():
    """Why synthesis moved UP instead of being threaded into the validator.

    ``_parse_text_as_schema`` shares ``_validate_with_degradation`` and rejects
    a blank answer, because a blank one there usually means it picked up an
    example block from the model's prose. A placeholder written inside the
    validator would pass that check; so would the old fabricated reply, which
    let a missing answer through this path.
    """
    engine = MilestoneEngine.__new__(MilestoneEngine)
    with pytest.raises(ValueError, match="empty agent_response"):
        engine._parse_text_as_schema(json.dumps({"note": "x"}), _Resp)


@pytest.mark.unit
def test_the_flag_reader_is_strict_about_mocks():
    """A test double's attributes are truthy Mocks; none may read as synthesized."""
    assert is_agent_response_synthesized(MagicMock()) is False
    assert me.is_agent_response_synthesized(object()) is False


# ---------------------------------------------------------------------------
# Through a REAL turn: what the synthesized answer becomes on the way out.
# ---------------------------------------------------------------------------


def _synthesized_diagnosis(reason: StopReason):
    """A real schema instance, synthesized by the real step."""
    from faultmaven.core.investigation.schemas import InvestigationResponse_Diagnosis

    blank = InvestigationResponse_Diagnosis(agent_response="", state_updates={})
    engine = MilestoneEngine.__new__(MilestoneEngine)
    return engine._synthesize_agent_response(blank, _llm(stop_reason=reason))


def _turn_engine(response):
    repo = MagicMock()
    repo.save = AsyncMock(side_effect=lambda c: c)
    repo.get = AsyncMock(return_value=None)
    engine = MilestoneEngine(MagicMock(), repo, investigation_tools=MagicMock())
    engine._generate_structured_output = AsyncMock(return_value=response)
    return engine


@pytest.mark.unit
@pytest.mark.asyncio
class TestThroughAProcessedTurn:
    """``process_turn`` end to end with only the LLM seam replaced — the
    reply, the metadata the service persists onto the row, and the
    ``TurnProgress`` the next prompt renders from must all agree."""

    async def test_an_uncomposed_placeholder_is_returned_and_flagged(self):
        from faultmaven.modules.case.contracts import (
            MESSAGE_METADATA_AGENT_SYNTHESIZED,
        )
        from faultmaven.modules.case.domain.models import EvidenceCategory
        from tests.unit.core.investigation.test_resolution_backstop_turn import _case

        # A stabilized case: no engine gate composes prose on this turn.
        case = _case(absence=EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE)
        result = await _turn_engine(
            _synthesized_diagnosis(StopReason.CONTENT_FILTER)
        ).process_turn(case=case, user_message="failover is holding for now")

        assert result["agent_response"] == RESPONSE_WITHHELD_TEXT
        assert result["metadata"].get(MESSAGE_METADATA_AGENT_SYNTHESIZED) is True
        record = case.turn_history[-1]
        assert record.agent_response_synthesized is True
        assert record.agent_response_summary == RESPONSE_WITHHELD_TEXT

    async def test_engine_prose_replaces_the_placeholder_and_clears_the_flag(self):
        """A gate notice composed onto a placeholder would arrive under
        "[Response withheld...]" and be hidden from the next prompt by the
        marker. It stands alone instead, as a real engine answer."""
        from faultmaven.modules.case.contracts import (
            MESSAGE_METADATA_AGENT_SYNTHESIZED,
        )
        from faultmaven.modules.case.domain.models import EvidenceCategory
        from tests.unit.core.investigation.test_resolution_backstop_turn import _case

        case = _case(absence=EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE)
        result = await _turn_engine(
            _synthesized_diagnosis(StopReason.CONTENT_FILTER)
        ).process_turn(case=case, user_message="yep, the errors are gone now")

        text = result["agent_response"]
        # Positive control: the INV-43 gate really composed on this turn.
        assert "confirmed eliminated" in text
        assert RESPONSE_WITHHELD_TEXT not in text
        assert not text.startswith("---")
        assert MESSAGE_METADATA_AGENT_SYNTHESIZED not in result["metadata"]
        record = case.turn_history[-1]
        assert record.agent_response_synthesized is False
        assert RESPONSE_WITHHELD_TEXT not in (record.agent_response_summary or "")

    async def test_a_blank_tool_calls_answer_marks_the_turn_record(self):
        """No engine placeholder (TOOL_CALLS), so the reply leaves blank for the
        service backstop — but the record the next prompt reads must not
        present an empty summary as an ordinary turn."""
        from faultmaven.modules.case.contracts import (
            MESSAGE_METADATA_AGENT_SYNTHESIZED,
        )
        from faultmaven.modules.case.domain.models import EvidenceCategory
        from tests.unit.core.investigation.test_resolution_backstop_turn import _case

        case = _case(absence=EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE)
        result = await _turn_engine(
            _synthesized_diagnosis(StopReason.TOOL_CALLS)
        ).process_turn(case=case, user_message="failover is holding for now")

        assert result["agent_response"] == ""
        assert MESSAGE_METADATA_AGENT_SYNTHESIZED not in result["metadata"]
        assert case.turn_history[-1].agent_response_synthesized is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_terminal_qa_path_reports_the_flag_on_its_metadata():
    """A terminal case answers questions through ``_process_terminal_qa``, which
    returns before Step 6 records a turn. The service's backfill writes that
    turn's record and the persisted row, and both read the flag from THIS
    metadata — so it has to be on it."""
    from faultmaven.core.investigation.schemas import TerminalResponse
    from faultmaven.modules.case.contracts import MESSAGE_METADATA_AGENT_SYNTHESIZED
    from faultmaven.modules.case.domain.models import Case, CaseState

    blank = TerminalResponse(agent_response="", state_updates={})
    synthesized = MilestoneEngine.__new__(MilestoneEngine)._synthesize_agent_response(
        blank, _llm(stop_reason=StopReason.MAX_TOKENS)
    )
    repo = MagicMock()
    repo.save = AsyncMock(side_effect=lambda c: c)
    repo.get = AsyncMock(return_value=None)
    engine = MilestoneEngine(MagicMock(), repo, investigation_tools=None)
    engine._generate_structured_output = AsyncMock(return_value=synthesized)

    from datetime import UTC, datetime

    opened = datetime.now(UTC)
    case = Case(
        created_at=opened,
        title="closed case",
        enterprise_id="org_test",
        user_id="user_test",
        description="d",
        state=CaseState.CLOSED,
        closed_at=opened,
        closure_reason="solution_deferred",
    )
    assert case.is_terminal

    result = await engine.process_turn(case=case, user_message="what was it?")

    engine._generate_structured_output.assert_awaited_once()
    assert result["agent_response"] == RESPONSE_TRUNCATED_TEXT
    assert result["metadata"].get(MESSAGE_METADATA_AGENT_SYNTHESIZED) is True
