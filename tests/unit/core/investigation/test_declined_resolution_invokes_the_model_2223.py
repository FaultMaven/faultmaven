"""#2223: a declined resolution is answered by the model, with the decline as
context, so it can ask what the user still needs to see (ruling (b)).

The canned reply to a bare "no" named no missing confirmation and asked for
nothing, so the case stalled after the decline. Every resolve decline the gate
RECORDS (a bare token, a minted confirmation, the Not-yet click, a deflection;
any offer basis) now falls through to a model turn whose prompt carries
``RESOLVE_DECLINED_THIS_TURN_LINE`` beside ``RESOLVE_DECLINED_LINE``. That turn
never forces tools, and 0c does not log the click it skips. The decline still
binds the model's same-turn ``resolved``: that refusal is
``test_declined_resolution_binds_the_model_1895.py::TestTheDeclineTurn::
test_a_decline_recording_a_row_and_proposing_is_refused``.

A typed reply the gate does not take as a decline is still re-asked with no
model call (round E's ground), and a close decline keeps its deterministic
replies: ``test_pending_gate_escape_lane.py::
test_bare_decline_keeps_cheap_canned_acknowledgment`` (deferred close) and
``test_declined_close_binds_the_model_1889.py::...::
test_a_bare_no_is_told_the_hold_and_given_the_card`` (false-alarm close).

Real ``process_turn`` turns with a stubbed generator: no live LLM.
"""

import logging

import pytest

from faultmaven.core.investigation.milestone_engine.transition_consent import (
    TYPED_CONFIRMATION_LINE,
)
from faultmaven.core.investigation.offer_projection import (
    DECLINED_RESOLVE_CARD_LABEL,
    project_offers,
)
from faultmaven.core.investigation.prompts.templates.assembly import (
    RESOLVE_DECLINED_LINE,
    RESOLVE_DECLINED_THIS_TURN_LINE,
)
from faultmaven.modules.case.contracts import CaseState
from tests.unit.core.investigation.test_declined_resolution_binds_the_model_1895 import (
    _DECLINE_VIA,
    _MODEL_FOLLOW_UPS,
    _decline,
    _declined,
    _engine,
    _labels,
    _turn,
)
from tests.unit.core.investigation.test_resolution_backstop_turn import (
    _confirmed_case,
)

pytestmark = pytest.mark.unit

_ENGINE_LOGGER = "faultmaven.core.investigation.milestone_engine.engine"

#: What the stubbed model answers on the decline turn.
_ASK = "What would you still need to see before calling it resolved?"


def _store_the_upload(case) -> None:
    """Store the case's upload, so a reading tool could open the file its
    confirmation rows cite: the forcing rule's material test then holds
    (``stage_gates._has_readable_material``)."""
    case.uploaded_files[0].storage_ref = "org_test/case/sts.log"


async def _offered(*, stored: bool = False):
    """A READY case; the engine's backstop offers the resolution on turn 1."""
    case = _confirmed_case()
    if stored:
        _store_the_upload(case)
    engine = _engine()
    await _turn(engine, case, "errors gone")
    assert case.offer_record.open.kind == "resolve"
    return engine, case


def _call(engine):
    """The one generator call of the turn just run: (prompt, keywords)."""
    generate = engine.generator.generate_structured_output
    assert generate.await_count == 1
    return generate.call_args.args[0], generate.call_args.kwargs


class TestTheDeclineIsAnsweredByTheModel:
    @pytest.mark.parametrize("how", ["token", "minted"])
    async def test_a_short_decline_reaches_the_model_unforced(self, how):
        """#2223's turn 10 shape: a short typed refusal, bare or minted. The
        stored upload makes the forcing rule's material test hold, and a bare
        "no" classifies ``directed_analysis``, so only the answered offer keeps
        the decline turn off ``tool_choice=required``."""
        engine, case = await _offered(stored=True)
        message, process_kw = _decline(case, how)
        result = await _turn(
            engine,
            case,
            message,
            narration=_ASK,
            follow_ups=_MODEL_FOLLOW_UPS,
            **process_kw,
        )

        prompt, kwargs = _call(engine)
        assert RESOLVE_DECLINED_LINE in prompt
        assert RESOLVE_DECLINED_THIS_TURN_LINE in prompt
        assert kwargs["force_tool_use"] is False
        assert result["agent_response"].startswith(_ASK)
        assert _labels(result) == ["Share more context", DECLINED_RESOLVE_CARD_LABEL]
        (decline,) = _declined(case)
        assert decline.via == _DECLINE_VIA[how]
        assert decline.at_turn == case.current_turn
        assert case.offer_record.open is None
        assert case.state == CaseState.INVESTIGATING

    @pytest.mark.parametrize("how", ["click", "deflection"])
    async def test_every_recorded_decline_carries_the_line(self, how):
        """The Not-yet click and a deflection reached the model before #2223,
        with nothing that asked it to seek a verification."""
        engine, case = await _offered()
        message, process_kw = _decline(case, how)
        await _turn(engine, case, message, **process_kw)

        prompt, _ = _call(engine)
        assert RESOLVE_DECLINED_THIS_TURN_LINE in prompt
        assert _declined(case)[-1].via == _DECLINE_VIA[how]

    async def test_a_decline_of_the_users_own_reopened_offer_carries_the_line(
        self,
    ):
        """The reopen chip's offer (basis ``reopen``) is a resolution the user
        asked for, and its decline is a declined resolution all the same."""
        engine, case = await _offered()
        await _turn(engine, case, "no")
        chip = project_offers(case).reopen_chip
        await _turn(
            engine,
            case,
            chip["payload"],
            intent_type="status_transition",
            intent_data=dict(chip["intent"]),
        )
        assert case.offer_record.open.basis == "reopen", "premise: the reopen"

        await _turn(engine, case, "no")
        prompt, _ = _call(engine)
        assert RESOLVE_DECLINED_THIS_TURN_LINE in prompt
        assert _declined(case)[-1].basis == "reopen"

    async def test_the_next_turn_carries_only_the_standing_line(self):
        engine, case = await _offered()
        await _turn(engine, case, "no")
        await _turn(engine, case, "what else is in that log?")

        prompt, _ = _call(engine)
        assert RESOLVE_DECLINED_LINE in prompt
        assert RESOLVE_DECLINED_THIS_TURN_LINE not in prompt


class TestTheGateStillDecides:
    @pytest.mark.parametrize("how", ["click", "minted"])
    async def test_0c_does_not_log_the_confirmation_0b_answered(self, how, caplog):
        """0c's confirmation branch is Gate 1's: it used to log "Received
        confirmation intent ... but status is investigating" on every
        confirmation-intent decline that fell through."""
        engine, case = await _offered()
        message, process_kw = _decline(case, how)
        with caplog.at_level(logging.INFO, logger=_ENGINE_LOGGER):
            await _turn(engine, case, message, **process_kw)

        engine_lines = [
            r.getMessage() for r in caplog.records if r.name == _ENGINE_LOGGER
        ]
        assert any(
            line.startswith("Open resolve offer declined") for line in engine_lines
        ), "positive control: the engine's log is captured"
        assert not [line for line in engine_lines if "but status is" in line]
        assert not [
            line for line in engine_lines if "Explicit confirmation intent" in line
        ]

    async def test_a_short_refusal_the_gate_does_not_read_as_a_decline_is_re_asked(
        self,
    ):
        """The boundary: un-minted, "I can't call it resolved yet." is no bare
        decline token, so the gate re-asks it with no model call and records
        nothing. Typed replies are round E's."""
        engine, case = await _offered()
        offer = case.offer_record.open
        answers = list(case.offer_record.answers)
        result = await _turn(engine, case, "I can't call it resolved yet.")

        assert not engine.generator.generate_structured_output.called
        assert TYPED_CONFIRMATION_LINE in result["agent_response"]
        assert case.offer_record.open == offer
        assert list(case.offer_record.answers) == answers
