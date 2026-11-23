"""A turn whose parse lost ``state_updates`` says so (#2811, stage S2c-2 of #2615).

The structured-output ladder can lose the model's case updates at four rungs:
``state_dropped``, ``response_synthesized_state_dropped``, and a coercion
before validation of a value that could have carried state (all of them), or
the prune rung on a path under an applied state field (some of them). The
ladder marks the response it returns, and the turn shows the owner-approved
note for the kind, placed by the rulings of 2026-11-23:

- model text, ``---``, the note, then any gate prose;
- a #1442 placeholder, ``---``, the placeholder note, flag kept;
- a placeholder with engine prose: the placeholder note, ``---``, the prose,
  flag cleared (the #1442 rule).

Everything runs through the real parse: only the provider is doubled, and it
returns raw bodies. The note texts are pinned verbatim here, not imported.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.core.investigation.case_telemetry import TELEMETRY_HANDOFF_KEY
from faultmaven.core.investigation.milestone_engine import (
    structured_output as so,
)
from faultmaven.core.investigation.milestone_engine.cause_state import (
    _gate1_statement_presentation,
)
from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.milestone_engine.response_synthesis import (
    RESPONSE_EMPTY_TEXT,
    RESPONSE_TRUNCATED_TEXT,
    is_agent_response_synthesized,
)
from faultmaven.core.investigation.milestone_engine.structured_output import (
    _parse_schema_tool_call,
    _parse_text_as_schema,
    _synthesize_agent_response,
)
from faultmaven.core.investigation.schemas import (
    InquiryResponse,
    InvestigationResponse_Diagnosis,
)
from faultmaven.infrastructure.llm.providers.base import (
    LLMResponse,
    StopReason,
    ToolCall,
)
from faultmaven.modules.case.contracts import MESSAGE_METADATA_AGENT_SYNTHESIZED
from faultmaven.modules.case.domain.models.evidence import EvidenceCategory
from tests.unit.core.investigation.test_confidence_repair_1502 import CASES, _keys
from tests.unit.core.investigation.test_gate_one_decline_1464 import (
    _inquiry_case_awaiting_gate_one,
)
from tests.unit.core.investigation.test_resolution_backstop_turn import _case

pytestmark = pytest.mark.unit

WHOLE_NOTE = (
    "**Note:** the case updates in this reply arrived malformed and were not "
    "recorded. Anything it says it changed is not saved; ask me to record it again."
)
PARTIAL_NOTE = (
    "**Note:** some case updates in this reply arrived malformed and were not "
    "recorded; ask me to record any it says it changed that you don't see."
)
WHOLE_PLACEHOLDER_NOTE = (
    "**Note:** the case updates in this turn arrived malformed and were not "
    "recorded; ask me to record them again."
)
PARTIAL_PLACEHOLDER_NOTE = (
    "**Note:** some case updates in this turn arrived malformed and were not "
    "recorded; ask me to record any you expected and don't see."
)
WHOLE_NOTICE = (
    "Your previous response's state_updates arrived malformed and were not "
    "applied: none of them were recorded. The user was shown a note inviting "
    "them to ask you to record them again."
)
PARTIAL_NOTICE = (
    "Part of your previous response's state_updates arrived malformed and was "
    "not applied. The user was shown a note inviting them to ask you to record "
    "any update they expected and don't see."
)
NOTE_PREFIXES = ("**Note:** the case updates", "**Note:** some case updates")

MODEL_TEXT = "The pods fail AssumeRoleWithWebIdentity on the audience check."
SEP = "\n\n---\n\n"

#: A fulfilled need whose only evidence id is not a string. The prune rung
#: takes that id (#3020), which leaves the need fulfilled with no id, so
#: re-validation fails and the ladder falls to ``state_dropped`` (or, with no
#: answer, ``response_synthesized_state_dropped``).
BAD_NEED = {
    "need_id": "need_aaaaaaaaaaaa",
    "state": "fulfilled",
    "fulfilling_evidence_ids": [{"a": 1}],
}
#: A create-path need with no purpose: pruned alone, at index 0.
PRUNABLE_NEED = {"state": "fulfilled"}


def _llm(body, stop_reason: StopReason, *, as_text: bool = False) -> LLMResponse:
    return LLMResponse(
        content=json.dumps(body) if as_text else "",
        confidence=0.9,
        provider="test",
        model="test-model",
        tokens_used=10,
        response_time_ms=5,
        tool_calls=None,
        stop_reason=stop_reason,
    )


def _engine(body: dict, stop_reason: StopReason = StopReason.STOP, *, text=False):
    """A real engine on the single-shot structured path (``generation.py``'s
    caller of the ladder), whose provider answers every call with *body* as
    the schema tool's arguments, or, with *text*, as JSON text in the reply."""
    from faultmaven.infrastructure.llm.structured_output_capability import (
        StructuredOutputCapability,
        StructuredOutputMode,
        StructuredOutputStrategy,
    )

    async def _generate(**kwargs):
        names = [t["function"]["name"] for t in kwargs.get("tools") or []]
        response = _llm(body, stop_reason, as_text=text)
        if names:
            response.tool_calls = [
                ToolCall(
                    id="call_schema",
                    type="function",
                    function={"name": names[-1], "arguments": json.dumps(body)},
                )
            ]
        return response

    provider = MagicMock()
    provider.provider_name = "anthropic"
    provider.route = AsyncMock(side_effect=_generate)
    provider.get_structured_output_strategy = MagicMock(
        return_value=(
            StructuredOutputStrategy(
                capability=StructuredOutputCapability.BEST_EFFORT,
                mode=StructuredOutputMode.JSON_OBJECT,
                include_schema_in_prompt=True,
                response_format={"type": "json_object"},
            )
            if text
            else StructuredOutputStrategy(
                capability=StructuredOutputCapability.FUNCTION_CALLING,
                mode=StructuredOutputMode.FUNCTION_CALLING,
                include_schema_in_prompt=False,
                response_format=None,
            )
        )
    )
    repo = MagicMock()
    repo.save = AsyncMock(side_effect=lambda c, **_: c)
    repo.get = AsyncMock(return_value=None)
    return MilestoneEngine(provider, repo, investigation_tools=None), provider


async def _turn(body, *, case=None, stop_reason=StopReason.STOP, text=False):
    """``process_turn`` end to end; returns the result, the case, and the
    ladder's counter labels (outcomes, ``state_updates`` repairs)."""
    case = case or _case(absence=EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE)
    engine, provider = _engine(body, stop_reason, text=text)
    with (
        patch.object(so, "schema_validation_total") as outcomes,
        patch.object(so, "schema_state_updates_repairs_total") as repairs,
    ):
        result = await engine.process_turn(
            case=case, user_message="the pods still fail", redaction_ctx=None
        )
    # Positive control: the single shot, the schema tool offered unless *text*.
    offered = [
        t["function"]["name"]
        for t in provider.route.await_args.kwargs.get("tools") or []
    ]
    assert (offered and "search_file" not in offered) if not text else not offered
    return (
        result,
        case,
        [c.kwargs["outcome"] for c in outcomes.labels.call_args_list],
        [c.kwargs["repair"] for c in repairs.labels.call_args_list],
    )


def _flags(result, case):
    """The row flag (persisted message metadata) and the record flag."""
    return (
        result["metadata"].get(MESSAGE_METADATA_AGENT_SYNTHESIZED) is True,
        case.turn_history[-1].agent_response_synthesized,
    )


# ---------------------------------------------------------------------------
# One positive case per rung, each with the counter it already increments.
# ---------------------------------------------------------------------------

RUNGS = [
    (
        "state_dropped",
        {
            "agent_response": MODEL_TEXT,
            "state_updates": {"evidence_need_updates": [BAD_NEED]},
        },
        ["state_dropped"],
        [],
        WHOLE_NOTE,
        WHOLE_NOTICE,
    ),
    (
        "string_dropped",
        {
            "agent_response": MODEL_TEXT,
            "state_updates": "hypotheses_to_add: the audience mismatch",
        },
        ["clean"],
        ["string_dropped"],
        WHOLE_NOTE,
        WHOLE_NOTICE,
    ),
    (
        "string_dropped_with_a_brace",
        {"agent_response": MODEL_TEXT, "state_updates": "see {the audience"},
        ["clean"],
        ["string_dropped"],
        WHOLE_NOTE,
        WHOLE_NOTICE,
    ),
    (
        "cut_missing",
        {"agent_response": MODEL_TEXT},
        ["clean"],
        ["absent_defaulted"],
        WHOLE_NOTE,
        WHOLE_NOTICE,
    ),
    (
        "cut_null",
        {"agent_response": MODEL_TEXT, "state_updates": None},
        ["clean"],
        ["absent_defaulted"],
        WHOLE_NOTE,
        WHOLE_NOTICE,
    ),
    (
        "non_object_dropped",
        {"agent_response": MODEL_TEXT, "state_updates": [{"hypotheses_to_add": []}]},
        ["clean"],
        ["non_object_dropped"],
        WHOLE_NOTE,
        WHOLE_NOTICE,
    ),
    (
        "pruned_state_updates",
        {
            "agent_response": MODEL_TEXT,
            "state_updates": {"evidence_need_updates": [PRUNABLE_NEED]},
        },
        ["pruned"],
        [],
        PARTIAL_NOTE,
        PARTIAL_NOTICE,
    ),
]


@pytest.mark.parametrize(
    "body, outcomes, repairs, note, notice",
    [r[1:] for r in RUNGS],
    ids=[r[0] for r in RUNGS],
)
async def test_a_rung_that_lost_state_shows_its_note(
    body, outcomes, repairs, note, notice
):
    # The provider reports the two ``cut_*`` bodies cut at ``max_tokens``: the
    # cut can have taken their ``state_updates``.
    cut = "state_updates" not in body or body["state_updates"] is None
    stop = StopReason.MAX_TOKENS if cut else StopReason.STOP
    result, case, seen_outcomes, seen_repairs = await _turn(body, stop_reason=stop)

    assert seen_outcomes == outcomes
    assert seen_repairs == repairs
    assert result["agent_response"] == MODEL_TEXT + SEP + note
    # The next prompt is told, and the record carries the composed reply.
    assert notice in case.turn_history[-1].system_notices
    assert note in case.turn_history[-1].agent_response_summary
    assert _flags(result, case) == (False, False)


# ---------------------------------------------------------------------------
# A #1442 placeholder: kept, flagged, with the placeholder note under it.
# ---------------------------------------------------------------------------

PLACEHOLDER_ROWS = [
    (
        "response_synthesized_state_dropped",
        {"state_updates": {"evidence_need_updates": [BAD_NEED]}},
        StopReason.STOP,
        RESPONSE_EMPTY_TEXT,
        ["response_synthesized_state_dropped"],
        [],
        WHOLE_PLACEHOLDER_NOTE,
        WHOLE_NOTICE,
    ),
    (
        # AM6: string_dropped, then the blanked-answer rung with
        # ``state_dropped=False`` — only ``pre_drop`` makes it WHOLE.
        "truncated_string_dropped",
        {"state_updates": '{"hypotheses_to_add": [{"statement": "the aud'},
        StopReason.MAX_TOKENS,
        RESPONSE_TRUNCATED_TEXT,
        ["response_synthesized"],
        ["string_dropped"],
        WHOLE_PLACEHOLDER_NOTE,
        WHOLE_NOTICE,
    ),
    (
        "response_synthesized_on_a_pruned_body",
        {"state_updates": {"evidence_need_updates": [PRUNABLE_NEED]}},
        StopReason.STOP,
        RESPONSE_EMPTY_TEXT,
        ["response_synthesized"],
        [],
        PARTIAL_PLACEHOLDER_NOTE,
        PARTIAL_NOTICE,
    ),
]


@pytest.mark.parametrize(
    "body, stop, placeholder, outcomes, repairs, note, notice",
    [r[1:] for r in PLACEHOLDER_ROWS],
    ids=[r[0] for r in PLACEHOLDER_ROWS],
)
async def test_a_placeholder_keeps_its_flag_with_the_note_under_it(
    body, stop, placeholder, outcomes, repairs, note, notice
):
    result, case, seen_outcomes, seen_repairs = await _turn(body, stop_reason=stop)

    assert seen_outcomes == outcomes
    assert seen_repairs == repairs
    assert result["agent_response"] == placeholder + SEP + note
    # Row and record agree: the reply is still the engine's placeholder.
    assert _flags(result, case) == (True, True)
    assert notice in case.turn_history[-1].system_notices


# ---------------------------------------------------------------------------
# Gate turns: a Gate-1-pending INQUIRY case composes its statement below.
# ---------------------------------------------------------------------------

INQUIRY_PRUNE = {"preliminary_urgency": {"level": 5}}


@pytest.mark.parametrize(
    "state_updates, note",
    [("preliminary_urgency: high", WHOLE_NOTE), (INQUIRY_PRUNE, PARTIAL_NOTE)],
    ids=["whole", "partial"],
)
async def test_on_a_gate_turn_the_note_sits_between_the_reply_and_the_gate(
    state_updates, note
):
    case = _inquiry_case_awaiting_gate_one()
    gate = _gate1_statement_presentation(case)
    result, case, _, _ = await _turn(
        {"agent_response": MODEL_TEXT, "state_updates": state_updates}, case=case
    )

    assert result["agent_response"] == MODEL_TEXT + SEP + note + SEP + gate


@pytest.mark.parametrize(
    "state_updates, note, notice",
    [
        ("preliminary_urgency: high", WHOLE_PLACEHOLDER_NOTE, WHOLE_NOTICE),
        (INQUIRY_PRUNE, PARTIAL_PLACEHOLDER_NOTE, PARTIAL_NOTICE),
    ],
    ids=["whole", "partial"],
)
async def test_a_placeholder_with_gate_prose_gives_way_to_its_note(
    state_updates, note, notice
):
    """The #1442 rule (ruling 2026-11-23, Q1): engine prose replaces the
    placeholder and clears the flag; the placeholder note heads it."""
    case = _inquiry_case_awaiting_gate_one()
    gate = _gate1_statement_presentation(case)
    result, case, _, _ = await _turn({"state_updates": state_updates}, case=case)

    assert result["agent_response"] == note + SEP + gate
    assert RESPONSE_EMPTY_TEXT not in result["agent_response"]
    assert _flags(result, case) == (False, False)
    assert notice in case.turn_history[-1].system_notices


# ---------------------------------------------------------------------------
# Nothing new where nothing in state_updates was lost.
# ---------------------------------------------------------------------------


def _diag_body(state_updates=None, **top):
    return {"agent_response": MODEL_TEXT, "state_updates": state_updates or {}, **top}


VALID_HYPOTHESIS = {
    "statement": "The OIDC client ID does not match the token audience",
    "category": "config",
    "likelihood": 0.6,
    "rationale": "The audience check is what fails",
}


NO_NOTE = [
    (
        "pruned_follow_up_chip_only",
        _diag_body(
            suggested_follow_ups=[
                {"label": "Show the pods", "payload": "x"},
                {"label": 5},
            ]
        ),
        ["pruned"],
    ),
    (
        "first_try_dropped_confidence",
        {**CASES[_keys("update")[0]].body(250), "agent_response": MODEL_TEXT},
        ["pruned"],
    ),
    (
        "set_aside_only",
        {**CASES[_keys("link")[0]].body(90), "agent_response": MODEL_TEXT},
        ["pruned"],
    ),
    ("clean", _diag_body(), ["clean"]),
    # A filler string carried no update: counted, not marked.
    ("filler_none", _diag_body("none"), ["clean"]),
    ("filler_n_a", _diag_body("N/A"), ["clean"]),
    ("filler_no_changes", _diag_body("no changes"), ["clean"]),
    # Not cut: a missing value lost nothing.
    ("uncut_missing", {"agent_response": MODEL_TEXT}, ["clean"]),
    # ``outcome`` is recomputed by the server, so its prune loses nothing.
    # A string naming only a field the server never applies carried nothing.
    ("unapplied_name_only", _diag_body("outcome: conversation"), ["clean"]),
    (
        "pruned_working_conclusion_only",
        _diag_body(
            {
                "working_conclusion": {"next_steps": 5},
                "hypotheses_to_add": [VALID_HYPOTHESIS],
            }
        ),
        ["pruned"],
    ),
    (
        "pruned_evidence_quality_issue_only",
        _diag_body(
            {
                "evidence_quality_issues": [{"evidence_id": "ev_1"}],
                "hypotheses_to_add": [VALID_HYPOTHESIS],
            }
        ),
        ["pruned"],
    ),
    (
        "pruned_outcome_only",
        _diag_body({"outcome": 5, "hypotheses_to_add": [VALID_HYPOTHESIS]}),
        ["pruned"],
    ),
]


@pytest.mark.parametrize(
    "body, outcomes", [r[1:] for r in NO_NOTE], ids=[r[0] for r in NO_NOTE]
)
async def test_a_turn_that_lost_no_state_update_shows_nothing_new(body, outcomes):
    result, case, seen_outcomes, seen_repairs = await _turn(body)

    # Positive control: the ladder really ran this rung (``pruned`` counts
    # the chip, the dropped confidence, the set-aside link and the outcome).
    assert seen_outcomes == outcomes
    su = body.get("state_updates")
    if isinstance(su, str):
        assert seen_repairs == ["string_dropped"]
    if isinstance(su, dict) and VALID_HYPOTHESIS in su.get("hypotheses_to_add", []):
        # The model's other updates were applied.
        assert any(
            h.statement == VALID_HYPOTHESIS["statement"]
            for h in case.hypotheses.values()
        )
    assert result["agent_response"] == MODEL_TEXT
    record = case.turn_history[-1]
    assert not any(
        n.startswith(("Your previous response's state_updates", "Part of your"))
        for n in record.system_notices
    )
    assert result["metadata"][TELEMETRY_HANDOFF_KEY].get("state_drop") is None


def test_a_mock_response_carries_no_marker():
    from faultmaven.core.investigation.milestone_engine.response_synthesis import (
        state_drop_of,
    )

    assert state_drop_of(MagicMock()) is None
    assert state_drop_of(object()) is None


# ---------------------------------------------------------------------------
# The parser marks at each of its three callers.
# ---------------------------------------------------------------------------

STRING_BODY = {"agent_response": MODEL_TEXT, "state_updates": "hypotheses_to_add: x"}


def test_the_tool_loop_parser_marks_the_response():
    from faultmaven.core.investigation.schemas import StateDrop

    call = ToolCall(
        id="c1",
        type="function",
        function={
            "name": "InvestigationResponse_Diagnosis",
            "arguments": json.dumps(STRING_BODY),
        },
    )
    parsed = _parse_schema_tool_call(call, InvestigationResponse_Diagnosis)
    assert parsed._state_drop is StateDrop.WHOLE


def test_the_text_parser_marks_the_response():
    from faultmaven.core.investigation.schemas import StateDrop

    parsed = _parse_text_as_schema(
        json.dumps(STRING_BODY), InvestigationResponse_Diagnosis
    )
    assert parsed._state_drop is StateDrop.WHOLE


CUT_BODIES = [
    {"agent_response": MODEL_TEXT},
    {"agent_response": MODEL_TEXT, "state_updates": None},
    {"agent_response": MODEL_TEXT, "state_updates": "none"},
]
CUT_IDS = ["missing", "null", "filler"]


@pytest.mark.parametrize("body", CUT_BODIES, ids=CUT_IDS)
def test_a_cut_reaches_the_marker_from_tool_input_only(body):
    """The tool-loop parser reads provider-built tool input, which a cut can
    leave partial and still parseable: what it lacks is lost. The text parser
    reads a strict parse, which proves the object closed: nothing is lost."""
    from faultmaven.core.investigation.milestone_engine.response_synthesis import (
        state_drop_of,
    )
    from faultmaven.core.investigation.schemas import StateDrop

    call = ToolCall(
        id="c1",
        type="function",
        function={
            "name": "InvestigationResponse_Diagnosis",
            "arguments": json.dumps(body),
        },
    )
    tool = _parse_schema_tool_call(call, InvestigationResponse_Diagnosis, cut=True)
    assert state_drop_of(tool) is StateDrop.WHOLE

    text = _parse_text_as_schema(
        json.dumps(body), InvestigationResponse_Diagnosis, cut=True
    )
    assert state_drop_of(text) is None
    assert text.agent_response == MODEL_TEXT


async def test_the_single_shot_parser_marks_the_response():
    """``generation.py``'s caller: the single shot's own parse."""
    from faultmaven.core.investigation.schemas import StateDrop
    from tests.model_content import disabled_redaction

    engine, _ = _engine(STRING_BODY)
    parsed = await engine.generator._generate_structured_output_inner(
        "p", InvestigationResponse_Diagnosis, redaction_ctx=disabled_redaction()
    )
    assert parsed._state_drop is StateDrop.WHOLE


# ---------------------------------------------------------------------------
# Across calls: the copy, and which kind wins.
# ---------------------------------------------------------------------------


def test_the_marker_survives_the_placeholder_copy():
    from faultmaven.core.investigation.schemas import StateDrop

    blank = InquiryResponse(agent_response="", state_updates={})
    blank._state_drop = StateDrop.PARTIAL
    synthesized = _synthesize_agent_response(blank, StopReason.STOP)
    assert synthesized is not blank
    assert is_agent_response_synthesized(synthesized)
    assert synthesized._state_drop is StateDrop.PARTIAL


def test_whole_wins_over_partial():
    """The reachable shape: a ``state_updates`` prune whose re-validation
    falls to ``state_dropped``, counted there."""
    from faultmaven.core.investigation.schemas import StateDrop

    seen = []
    real = so._prune_invalid_sub_records

    def spy(*args, **kwargs):
        out = real(*args, **kwargs)
        seen.extend(out[1])
        return out

    with (
        patch.object(so, "_prune_invalid_sub_records", side_effect=spy),
        patch.object(so, "schema_validation_total") as outcomes,
    ):
        parsed = so._validate_with_degradation(
            {
                "agent_response": MODEL_TEXT,
                # The first need's id and the second need are pruned; the
                # first need, fulfilled with no id, still fails.
                "state_updates": {"evidence_need_updates": [BAD_NEED, PRUNABLE_NEED]},
            },
            InvestigationResponse_Diagnosis,
        )
    # Positive control: the prune really took a path under state_updates.
    assert seen == [
        "state_updates.evidence_need_updates[0].fulfilling_evidence_ids[0]",
        "state_updates.evidence_need_updates[1]",
    ]
    assert [c.kwargs["outcome"] for c in outcomes.labels.call_args_list] == [
        "state_dropped"
    ]
    assert parsed._state_drop is StateDrop.WHOLE

    # The exit's own precedence, where a pre-validation drop meets a prune.
    # No parse reaches it (a dropped ``state_updates`` leaves nothing to prune
    # under it), so the ladder is called with the label directly.
    parsed = so._validate_with_degradation(
        {
            "agent_response": MODEL_TEXT,
            "state_updates": {"evidence_need_updates": [PRUNABLE_NEED]},
        },
        InvestigationResponse_Diagnosis,
        pre_drop=StateDrop.WHOLE,
    )
    assert parsed._state_drop is StateDrop.WHOLE


# ---------------------------------------------------------------------------
# The record: the telemetry handoff.
# ---------------------------------------------------------------------------


async def test_the_telemetry_handoff_carries_the_kind():
    result, _, _, _ = await _turn(
        {
            "agent_response": MODEL_TEXT,
            "state_updates": {"evidence_need_updates": [PRUNABLE_NEED]},
        }
    )
    assert result["metadata"][TELEMETRY_HANDOFF_KEY]["state_drop"] == "partial"


def test_the_telemetry_event_admits_the_kind():
    from faultmaven.core.investigation.case_telemetry import (
        TurnPath,
        build_case_turn_event,
    )

    case = _case(absence=EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE)
    event = build_case_turn_event(case, path=TurnPath.LLM, state_drop="whole")
    assert event["state_drop"] == "whole"


@pytest.mark.parametrize(
    "state_updates, repairs",
    [({}, []), ([], ["absent_defaulted"]), (5, ["absent_defaulted"])]
    + [(True, ["absent_defaulted"])],
    ids=["object", "empty_list", "number", "bool"],
)
async def test_cut_tool_input_that_carried_nothing_shows_no_note(
    state_updates, repairs
):
    """An object is marked by the rungs alone (what a cut took from it cannot
    be told), and ``[]``, a number or a bool could not have carried a field."""
    result, case, outcomes, seen_repairs = await _turn(
        _diag_body() | {"state_updates": state_updates},
        stop_reason=StopReason.MAX_TOKENS,
    )
    assert (outcomes, seen_repairs) == (["clean"], repairs)
    assert result["agent_response"] == MODEL_TEXT


async def test_a_cut_tool_input_string_is_lost_state():
    """Tool input the provider built can be partial and still parse, so even
    a filler string may be the head of a cut object."""
    result, case, outcomes, repairs = await _turn(
        _diag_body("none"), stop_reason=StopReason.MAX_TOKENS
    )
    assert repairs == ["string_dropped"]
    assert result["agent_response"] == MODEL_TEXT + SEP + WHOLE_NOTE


@pytest.mark.parametrize(
    "body",
    [{"agent_response": MODEL_TEXT}, _diag_body() | {"state_updates": None}]
    + [_diag_body("none")],
    ids=["missing", "null", "filler"],
)
async def test_a_closed_text_body_lost_nothing_to_a_cut(body):
    """A text body parses strictly, so a parse proves the object closed: the
    cut landed after it, and the model's own omission is not a lost update."""
    result, case, outcomes, repairs = await _turn(
        body, stop_reason=StopReason.MAX_TOKENS, text=True
    )
    # Positive control: the body was parsed, and counted.
    assert outcomes == ["clean"] and len(repairs) == 1
    assert result["agent_response"] == MODEL_TEXT


#: Pinned literally, so dropping a member fails here rather than shrinking
#: the parametrization.
UNAPPLIED = (
    "outcome",
    "working_conclusion",
    "evidence_quality_issues",
    "final_summary_update",
    "documentation_links",
    "solution_feedback",
)


@pytest.mark.parametrize("field", UNAPPLIED)
def test_a_prune_of_an_unapplied_field_loses_no_update(field):
    assert not so._loses_an_applied_update(f"state_updates.{field}")
    assert not so._loses_an_applied_update(f"state_updates.{field}[0]")
    assert not so._loses_an_applied_update(f"state_updates.{field}.likelihood")


def test_the_unapplied_set_is_exactly_the_pinned_one():
    assert so._UNAPPLIED_STATE_FIELDS == frozenset(UNAPPLIED)
    # Positive controls: an applied field, and a path outside state_updates.
    assert so._loses_an_applied_update("state_updates.hypotheses_to_add[0]")
    assert not so._loses_an_applied_update("suggested_follow_ups[1]")


async def test_the_drop_notice_heads_a_busy_turn():
    """The prompt renders notices from the head until its budget is spent,
    and on a flag-kept placeholder turn this one is the only word of the drop."""
    result, case, outcomes, _ = await _turn(
        _diag_body(
            {
                "milestones": {"accepted_fix_reported_in": "yesterday"},
                "evidence_need_updates": [PRUNABLE_NEED],
            }
        )
    )
    notices = case.turn_history[-1].system_notices
    # Positive control: another notice was written before this one.
    assert any("accepted_fix_reported_in 'yesterday'" in n for n in notices)
    assert notices[0] == PARTIAL_NOTICE
