"""#2223: a declined resolution is answered by the model, with the decline as
context, so it can ask what the user still needs to see (ruling (b)).

The canned reply to a bare "no" named no missing confirmation and asked for
nothing, so the case stalled after the decline. Every resolve decline the gate
RECORDS (a bare token, a minted confirmation, the Not-yet click, a deflection;
any offer basis) now falls through to a model turn whose prompt carries
``RESOLVE_DECLINED_THIS_TURN_LINE`` beside ``RESOLVE_DECLINED_LINE``, on every
processing mode. A bare one never forces tools; every other turn that answers
an offer is forced as before (#708). 0c skips a confirmation the gate consumed,
and only that one. The decline still
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
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.milestone_engine.errors import MilestoneEngineError
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
from faultmaven.modules.case.contracts import CaseState, ProblemStatus
from faultmaven.modules.case.domain.models.evidence import UploadedFile
from tests.case_builders import inquiry_case, with_open_offer
from tests.unit.core.investigation.test_declined_resolution_binds_the_model_1895 import (
    _DECLINE_VIA,
    _MODEL_FOLLOW_UPS,
    _decline,
    _declined,
    _engine,
    _labels,
    _turn,
)
from tests.unit.core.investigation.test_problem_statement_revision_and_false_alarm import (
    _case as _revision_case,
)
from tests.unit.core.investigation.test_problem_statement_revision_and_false_alarm import (
    _engine as _revision_engine,
)
from tests.unit.core.investigation.test_problem_statement_revision_and_false_alarm import (
    _propose,
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

    async def test_an_upload_answering_the_offer_is_still_forced(self):
        """The forcing exemption is for a turn with nothing new. A turn that
        delivers a file while the offer is open answers the offer too, and
        stays #708's delivering turn: forced, as before #2223."""
        engine, case = await _offered(stored=True)
        upload = case.uploaded_files[0]
        await _turn(
            engine,
            case,
            "here are the overnight logs",
            attachments=[{"file_id": upload.file_id, "filename": upload.filename}],
        )

        _, kwargs = _call(engine)
        assert case.offer_record.open is None
        assert case.offer_record.answers[-1].at_turn == case.current_turn
        assert kwargs["force_tool_use"] is True

    async def test_a_question_withdrawing_the_offer_is_still_forced(self):
        """Only the bare decline #2223 routes to the model is exempt. A question
        withdraws the offer and keeps the ``tool_choice=required`` it has on
        ``main``."""
        engine, case = await _offered(stored=True)
        await _turn(engine, case, "Check sts.log for AssumeRole errors since the fix?")

        _, kwargs = _call(engine)
        assert case.offer_record.answers[-1].answer == "withdrawn"
        assert case.offer_record.answers[-1].at_turn == case.current_turn
        assert kwargs["force_tool_use"] is True

    @pytest.mark.parametrize("mode", ["knowledge_query", "agent_meta"])
    async def test_a_decline_on_a_mode_with_no_focus_block_carries_both_lines(
        self, mode
    ):
        """A knowledge question or a question about FaultMaven itself renders
        no focus block of its own; the decline's lines are appended all the
        same."""
        engine, case = await _offered()
        await _turn(
            engine,
            case,
            "Not yet. Explain how Redis failover works first",
            intent_type="conversation",
            intent_data={"query_mode": mode},
        )

        prompt, _ = _call(engine)
        assert _declined(case)[-1].at_turn == case.current_turn
        assert RESOLVE_DECLINED_LINE in prompt
        assert RESOLVE_DECLINED_THIS_TURN_LINE in prompt

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

    async def test_0c_does_not_log_a_minted_revision_answer_0b_prime_took(self, caplog):
        """The revision gate consumes a minted answer as 0b consumes a minted
        decline: 0c does not see it again."""
        engine, case = _revision_engine(), _revision_case()
        _propose(case)
        case.current_turn += 1
        with caplog.at_level(logging.INFO, logger=_ENGINE_LOGGER):
            await engine.process_turn(
                case=case,
                user_message="yes",
                intent_type="confirmation",
                intent_data={"value": True},
                typed=True,
                redaction_ctx=None,
            )

        assert case.progress.problem_status == ProblemStatus.VERIFIED
        engine_lines = [
            r.getMessage() for r in caplog.records if r.name == _ENGINE_LOGGER
        ]
        assert any(
            line.startswith("Processing turn") for line in engine_lines
        ), "positive control: the engine's log is captured"
        assert not [line for line in engine_lines if "but status is" in line]

    async def test_a_mint_the_gate_set_aside_still_reaches_gate_one(self):
        """An upload carries a re-asked mint through 0b: the offer is
        withdrawn, and the mint was never taken. It is Gate 1's to read, and
        a confirmation on text that is not one bare consent is counted
        deferred, as on ``main``."""
        case = inquiry_case(
            case_id="case_2223aaaaaaaa",
            title="Replica lag",
            user_id="user_test",
            enterprise_id="org_test",
            description="replica lag",
            statement="Replica db-2 lags the primary by >30s since 09:00 UTC",
        )
        case.uploaded_files.append(
            UploadedFile(
                file_id="file_2223aaaaaaaa",
                filename="lag.log",
                size_bytes=512,
                uploaded_at_turn=1,
            )
        )
        with_open_offer(case, kind="close", basis="model", summary="Close it?")
        repo = MagicMock()
        repo.save = AsyncMock(side_effect=lambda c, **_: c)
        repo.get = AsyncMock(side_effect=lambda cid: None)
        engine = MilestoneEngine(MagicMock(), repo, investigation_tools=MagicMock())
        engine.generator.generate_structured_output = AsyncMock(
            side_effect=RuntimeError("the seam: 0c has run")
        )
        case.current_turn += 1

        with patch(
            "faultmaven.core.investigation.milestone_engine.engine."
            "inquiry_handshake_deferred_total"
        ) as deferred:
            with pytest.raises(MilestoneEngineError):
                await engine.process_turn(
                    case=case,
                    user_message="yes, that's the problem, the lag log is attached",
                    attachments=[
                        {"file_id": "file_2223aaaaaaaa", "filename": "lag.log"}
                    ],
                    intent_type="confirmation",
                    intent_data={"value": True},
                    typed=True,
                    redaction_ctx=None,
                )

        assert case.offer_record.answers[-1].answer == "withdrawn"
        assert engine.generator.generate_structured_output.await_count == 1
        deferred.labels.assert_called_once_with(reason="not_bare")

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
