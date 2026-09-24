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
    schema_answer_stop_reason,
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

    def test_a_schema_answers_tool_call_is_a_finished_answer(self):
        """The translation both synthesis sites apply first: only TOOL_CALLS
        moves, and it moves to STOP. A cut or filtered schema call keeps its
        reason."""
        for reason in StopReason:
            expected = StopReason.STOP if reason is StopReason.TOOL_CALLS else reason
            assert schema_answer_stop_reason(_llm(stop_reason=reason)) is expected
        # A response with no envelope (a bare string) is no signal.
        assert schema_answer_stop_reason("raw text") is StopReason.UNKNOWN

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

    async def test_a_tool_calls_stop_is_a_completed_answer_here(self):
        """This site reads a response that has already answered: a
        ``TOOL_CALLS`` stop means the answer came through a tool call, not
        that control was handed to one — so the engine names the blank."""
        engine = _single_shot_engine({"agent_response": ""}, StopReason.TOOL_CALLS)
        parsed = await engine._generate_structured_output_inner("p", _Resp)

        assert parsed.agent_response == RESPONSE_EMPTY_TEXT
        assert is_agent_response_synthesized(parsed)

    async def test_a_whitespace_answer_is_blank(self):
        """Emptiness is decided by ``strip()``: a model that answered only
        whitespace said nothing, and the ENGINE names it rather than leaving
        it to the service's blind backstop."""
        engine = _single_shot_engine({"agent_response": " \n\t"}, StopReason.STOP)
        parsed = await engine._generate_structured_output_inner("p", _Resp)

        assert parsed.agent_response == RESPONSE_EMPTY_TEXT
        assert is_agent_response_synthesized(parsed)

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

    @pytest.mark.parametrize(
        "data", [{"agent_response": ""}, {"note": "x"}], ids=["blank", "omitted"]
    )
    async def test_a_tool_calls_stop_is_how_this_answer_normally_ends(self, data):
        """OpenAI, Anthropic, Groq, Fireworks, OpenRouter, local and Cohere all
        report a schema-tool answer as a tool call. Read as a handoff, a blank
        answer would reach the service's blind backstop on seven of nine
        providers; it is the engine's to name."""
        parsed = await _tool_loop(_schema_call(data, stop_reason=StopReason.TOOL_CALLS))

        assert parsed.agent_response == RESPONSE_EMPTY_TEXT
        assert is_agent_response_synthesized(parsed)

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
    return engine._synthesize_agent_response(blank, reason)


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

    async def test_a_raw_blank_answer_still_marks_the_turn_record(self):
        """Defence in depth. Both synthesis sites name every blank now, so no
        structured answer reaches Step 6 empty — but if one ever did, the
        record the next prompt reads must not present an empty summary as an
        ordinary turn."""
        from faultmaven.core.investigation.schemas import (
            InvestigationResponse_Diagnosis,
        )
        from faultmaven.modules.case.contracts import (
            MESSAGE_METADATA_AGENT_SYNTHESIZED,
        )
        from faultmaven.modules.case.domain.models import EvidenceCategory
        from tests.unit.core.investigation.test_resolution_backstop_turn import _case

        case = _case(absence=EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE)
        result = await _turn_engine(
            InvestigationResponse_Diagnosis(agent_response="", state_updates={})
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
        blank, StopReason.MAX_TOKENS
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


# ---------------------------------------------------------------------------
# A TOOL_CALLS stop through the REAL generation path, at both synthesis sites.
# ---------------------------------------------------------------------------


def _schema_tool_provider():
    """A provider shaped like Anthropic's FUNCTION_CALLING answer: the reply is
    a call to the schema tool, reported as ``TOOL_CALLS``, carrying a blank
    ``agent_response``. Only the provider is doubled; the engine's own
    generation, parsing, synthesis and turn bookkeeping all run."""
    from faultmaven.infrastructure.llm.structured_output_capability import (
        StructuredOutputCapability,
        StructuredOutputMode,
        StructuredOutputStrategy,
    )

    async def _generate(**kwargs):
        names = [t["function"]["name"] for t in kwargs.get("tools") or []]
        schema_tool = [n for n in names if n != "search_file"][-1]
        return _llm(
            stop_reason=StopReason.TOOL_CALLS,
            tool_calls=[
                ToolCall(
                    id="call_schema",
                    type="function",
                    function={
                        "name": schema_tool,
                        "arguments": json.dumps(
                            {"agent_response": "", "state_updates": {}}
                        ),
                    },
                )
            ],
        )

    provider = MagicMock()
    provider.provider_name = "anthropic"
    provider.generate = AsyncMock(side_effect=_generate)
    provider.get_structured_output_strategy = MagicMock(
        return_value=StructuredOutputStrategy(
            capability=StructuredOutputCapability.FUNCTION_CALLING,
            mode=StructuredOutputMode.FUNCTION_CALLING,
            include_schema_in_prompt=False,
            response_format=None,
        )
    )
    return provider


def _repo():
    repo = MagicMock()
    repo.save = AsyncMock(side_effect=lambda c: c)
    repo.get = AsyncMock(return_value=None)
    return repo


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("site", ["single_shot", "tool_loop"])
async def test_a_blank_schema_tool_answer_is_named_by_the_engine(site):
    """Review of #1442: at both synthesis sites the response has ALREADY
    answered, so a ``TOOL_CALLS`` stop — the normal completion of a schema-tool
    answer on seven of nine providers — is not the ruled "handoff" arm. Read as
    one, the blank would have reached ``InvestigationService``'s blind backstop
    and the engine would own synthesis on Gemini alone."""
    import contextlib
    from unittest.mock import patch

    from faultmaven.modules.case.contracts import MESSAGE_METADATA_AGENT_SYNTHESIZED
    from faultmaven.modules.case.domain.models import EvidenceCategory
    from tests.unit.core.investigation.test_resolution_backstop_turn import _case

    provider = _schema_tool_provider()
    if site == "single_shot":
        # No investigation tools: the turn takes the single-shot structured
        # path, which on a FUNCTION_CALLING provider forces the schema tool.
        engine = MilestoneEngine(provider, _repo(), investigation_tools=None)
        route = contextlib.nullcontext()
    else:
        engine = MilestoneEngine(provider, _repo(), investigation_tools=MagicMock())
        engine._da_provider_supports_tools = MagicMock(return_value=True)
        engine._build_da_tool_schemas = MagicMock(
            return_value=[
                {
                    "type": "function",
                    "function": {
                        "name": "search_file",
                        "description": "Search files",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ]
        )
        engine._build_tool_context = AsyncMock(return_value=MagicMock())
        route = patch.object(me, "_route_toolless_turn_single_shot", return_value=False)

    case = _case(absence=EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE)
    with route:
        result = await engine.process_turn(
            case=case, user_message="failover is holding for now"
        )

    # Positive control: the call went out as a schema-tool call on the path
    # named, so the site under test really read a TOOL_CALLS response.
    sent = provider.generate.await_args.kwargs
    offered = [t["function"]["name"] for t in sent.get("tools") or []]
    assert offered, "the schema tool was never offered"
    assert ("search_file" in offered) is (site == "tool_loop")
    assert result["agent_response"] == RESPONSE_EMPTY_TEXT
    assert result["metadata"].get(MESSAGE_METADATA_AGENT_SYNTHESIZED) is True
    assert result["case_updated"].turn_history[-1].agent_response_synthesized is True


# ---------------------------------------------------------------------------
# Every place engine prose composes onto the reply, on a placeholder turn.
# ---------------------------------------------------------------------------

GATE_NOTICE = "GATE-NOTICE: engine prose for this turn"

#: Each composition site in ``_process_turn_impl``, keyed by what selects it.
#: Selection reads only ``metadata`` (or the case), never the reply, so every
#: one is reachable on a turn whose reply the engine synthesized.
GATE_SITES = [
    "resolution_ready_for_confirmation",
    "resolution_suggest_close",
    "resolution_needs_info_first_pass",
    "close_pivoted_to_resolve",
    "rca_infeasible_closure_message",
    "deferred_solution_gate_message",
    "resolution_ready_gate_message",
    "gate1",
    "summary_append",
    "inv40_overclaim",
]


def _force_branch(site, case, md, engine):
    """Stand in for what ``_process_response_structured`` records on a turn
    that selects *site*. Returns a replacement case where the branch needs one."""
    suggestions = me._close_confirmation_suggestions()
    if site == "resolution_ready_for_confirmation":
        md[site] = True
    elif site == "resolution_suggest_close":
        md[site] = True
        md["resolution_readiness_message"] = GATE_NOTICE
    elif site == "resolution_needs_info_first_pass":
        md[site] = True
        md["resolution_needs_info_message"] = GATE_NOTICE
        md["override_suggestions"] = suggestions
    elif site == "close_pivoted_to_resolve":
        md[site] = True
        case.pending_transition = {"to_state": "resolved", "summary": GATE_NOTICE}
    elif site in (
        "rca_infeasible_closure_message",
        "deferred_solution_gate_message",
        "resolution_ready_gate_message",
    ):
        md[site] = GATE_NOTICE
        md["override_suggestions"] = suggestions
    elif site == "summary_append":
        # The terminal transition the apply layer makes on such a turn,
        # through a validated copy: state and both timestamps land together.
        from datetime import UTC, datetime

        from faultmaven.modules.case.domain.models import CaseState

        md["status_transitioned"] = True
        engine._auto_generate_report = AsyncMock(return_value=(GATE_NOTICE, False))
        now = datetime.now(UTC)
        return type(case).model_validate(
            {
                **case.model_dump(),
                "state": CaseState.RESOLVED,
                "resolved_at": now,
                "closed_at": now,
            }
        )
    return None


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("site", GATE_SITES)
async def test_engine_prose_on_a_placeholder_turn_is_the_whole_reply(site):
    """Review of #1442: each site composes ``agent_response_text``, which
    starts EMPTY on a synthesized turn. Composing ``response_obj.agent_response``
    instead would put "[Response withheld...]" above the notice, clear the
    flag (the reply is no longer the bare placeholder), and quote the
    placeholder to the model as ``ASSISTANT:`` next turn.

    ``inv40_overclaim`` is not reachable with a placeholder in production —
    the INV-40 scan reads the model's text, and no placeholder carries a
    completion phrase — so its detector is patched to fire; the composition
    site is what is pinned.
    """
    from unittest.mock import patch

    from faultmaven.core.investigation.prompts import context_builder as cb
    from faultmaven.core.investigation.prompts.fence import mint_token
    from faultmaven.core.investigation.schemas import InquiryResponse
    from faultmaven.modules.case.contracts import MESSAGE_METADATA_AGENT_SYNTHESIZED
    from faultmaven.modules.case.domain.models import EvidenceCategory
    from tests.unit.core.investigation.test_gate_one_decline_1464 import (
        _inquiry_case_awaiting_gate_one,
    )
    from tests.unit.core.investigation.test_resolution_backstop_turn import _case

    if site == "gate1":
        case = _inquiry_case_awaiting_gate_one()
        blank = InquiryResponse(agent_response="", state_updates={})
    else:
        case = _case(absence=EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE)
        # Round-trippable, which the summary site's validated copy needs.
        case.inquiry.proposed_problem_statement = "pods cannot assume the role"
        from faultmaven.core.investigation.schemas import (
            InvestigationResponse_Diagnosis,
        )

        blank = InvestigationResponse_Diagnosis(agent_response="", state_updates={})
    response = MilestoneEngine.__new__(MilestoneEngine)._synthesize_agent_response(
        blank, StopReason.CONTENT_FILTER
    )
    assert response.agent_response == RESPONSE_WITHHELD_TEXT

    engine = _turn_engine(response)
    apply = engine._process_response_structured

    async def _apply_then_select(*args, **kwargs):
        case_updated, md = await apply(*args, **kwargs)
        replaced = _force_branch(site, case_updated, md, engine)
        return (replaced or case_updated), md

    engine._process_response_structured = _apply_then_select
    detector = (
        patch.object(me, "_narration_overclaim_notice", return_value=GATE_NOTICE)
        if site == "inv40_overclaim"
        else patch.object(me, "_narration_overclaim_notice", return_value=None)
    )
    with detector:
        result = await engine.process_turn(case=case, user_message="and now?")

    reply = result["agent_response"]
    case_updated = result["case_updated"]
    if site == "gate1":
        expected = me._gate1_statement_presentation(case_updated)
    elif site == "resolution_ready_for_confirmation":
        expected = (
            "Thanks for the additional details.\n\n"
            + me._build_resolution_confirmation(case_updated)
        )
    else:
        expected = GATE_NOTICE
    assert reply == expected, "the notice must be the whole reply"
    assert MESSAGE_METADATA_AGENT_SYNTHESIZED not in result["metadata"]
    record = case_updated.turn_history[-1]
    assert record.agent_response_synthesized is False

    # What the NEXT turn's prompt is built from: the row, and the record.
    rows = [
        {"turn_number": 1, "role": "user", "content": "and now?", "metadata": {}},
        {
            "turn_number": 1,
            "role": "assistant",
            "content": reply,
            "metadata": result["metadata"],
        },
    ]
    history = cb._build_verbatim_history(rows, cb.PromptFence(mint_token()))
    summary = cb._build_turn_summary(record)
    for rendered in (history, summary):
        assert RESPONSE_WITHHELD_TEXT not in rendered
        assert cb.NO_ANSWER_LINE not in rendered
    # Positive control: the engine's prose IS quoted — it is a real answer.
    assert f"ASSISTANT: {reply.splitlines()[0]}" in history
