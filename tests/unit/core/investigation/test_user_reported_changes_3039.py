"""What the user did is recorded apart from what it counts toward (INV-49, #3039).

``case_61110c61f798``: the user carried out the fix the assistant proposed
before any root cause was established and said so ("Done: …"). The model set
``solution_accepted``; the gate refused it, because a fix proposed before its
licence is an M5-downgraded DIAGNOSTIC (or was never minted), and nothing
recorded that the change happened. Later turns read the state it produced as
unexplained and asked who made it, and the closure said "No fix was carried
out in the case."

- ``TestTheReportIsRecorded`` — every refusal of a report stamps
  ``reported_done_in_turn``; nothing else moves (no gate, window, liveness).
- ``TestOnlyAReportRecords`` — the simulator's report sentences: a held,
  refused or unjustified report records nothing.
- ``TestTheRecordRenders`` — ``<pending_action>`` drops a reported change and
  ``<user_changes>`` shows it, with the rule, and the register line only once
  the record holds an identified cause.
- ``TestTheClosureLine`` — "No fix was carried out" is no longer said over a
  report.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from faultmaven.core.investigation.cause_assurance import fix_windows
from faultmaven.core.investigation.milestone_engine import response_application
from faultmaven.core.investigation.milestone_engine.response_application import (
    WHY_OPEN_DOWNGRADE_REASON,
)
from faultmaven.core.investigation.milestone_engine.stage_gates import (
    FIX_JUSTIFICATION_KEY,
    NO_SYMPTOM_EVIDENCE_DOWNGRADE_REASON,
    reported_change_turns,
)
from faultmaven.core.investigation.narration.replies import closed_line
from faultmaven.core.investigation.prompts.context_builder.assembly import (
    USER_CHANGES_REGISTER_LINE,
    USER_CHANGES_RULE,
    build_investigation_context,
    user_changes_block,
)
from faultmaven.core.investigation.schemas import MilestoneUpdates, SolutionToAdd
from faultmaven.modules.case.contracts import ClosureReason
from faultmaven.modules.case.domain.models.solution import (
    InvestigationActionType,
    ProposedAction,
    Solution,
    SolutionType,
)
from tests.unit.core.investigation.narrated import narrated
from tests.unit.core.investigation.test_m5_recovery_1679 import (
    _case,
    _downgraded_case,
    _make_engine,
    _meta,
    _Updates,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _stub_chain_emission(monkeypatch):
    """The chain-emission tail stubbed, as the M5 tests do: the applier reads
    both from its own module namespace."""
    monkeypatch.setattr(
        response_application, "_apply_chain_emission", lambda *a, **k: None
    )
    monkeypatch.setattr(
        response_application, "_nudge_ambiguous_orphan_chains", lambda *a, **k: None
    )


_FIX = "Point ExecStart at /usr/bin/billing-exporter"


def _diagnostic(case):
    (action,) = [
        a
        for a in case.proposed_actions
        if a.action_type == InvestigationActionType.DIAGNOSTIC
    ]
    return action


def _fix_entry(description: str = _FIX) -> list[SolutionToAdd]:
    return [
        SolutionToAdd(
            description=description,
            solution_type=SolutionType.CONFIG_CHANGE,
            estimated_impact="the unit starts",
            risks="low",
            commands=["systemctl daemon-reload"],
        )
    ]


async def _apply(case, metadata=None, **fields):
    metadata = _meta() if metadata is None else metadata
    await _make_engine().responses._apply_investigation_updates(
        case, _Updates(**fields), metadata
    )
    return metadata


def _notices(case, metadata) -> str:
    return "\n".join(narrated(case, metadata))


@pytest.mark.asyncio
class TestTheReportIsRecorded:
    async def test_a_report_naming_the_downgraded_action_is_recorded(self):
        """``case_471402312da2`` turn 5: the ref names the DIAGNOSTIC that M5
        downgraded the turn before; ``_fix_named_by`` refuses it."""
        case = _downgraded_case()
        action = _diagnostic(case)
        windows_before = fix_windows(case)

        metadata = await _apply(
            case,
            milestones=MilestoneUpdates(
                solution_accepted=True, accepted_action_ref=action.action_id
            ),
        )

        assert action.reported_done_in_turn == 5
        # Registers nothing: the gate, the action's state and the windows stand.
        assert case.progress.solution_accepted is False
        assert case.progress.solution_proposed is False
        assert action.state == "pending"
        assert action.accepted_in_turn is None
        assert case.action_attempts == []
        assert fix_windows(case) == windows_before
        text = _notices(case, metadata)
        assert "'solution_accepted' was not registered" in text
        assert f"{action.action_id} is on record as carried out at turn 5" in text

    async def test_a_change_never_on_record_is_recorded_through_its_re_proposal(
        self,
    ):
        """``case_61110c61f798`` turn 4: nothing was ever minted. The notice's
        path — re-propose it, named by new_index_N — mints the downgraded
        DIAGNOSTIC and records the report in the same apply."""
        case = _case(leading_likelihood=0.4)

        metadata = await _apply(
            case,
            solutions_to_add=_fix_entry(),
            milestones=MilestoneUpdates(
                solution_accepted=True, accepted_action_ref="new_index_0"
            ),
        )

        action = _diagnostic(case)
        assert action.reported_done_in_turn == case.current_turn
        assert case.progress.solution_accepted is False
        assert "is on record as carried out" in _notices(case, metadata)

    async def test_a_report_with_no_ref_and_its_re_proposal_is_recorded(self):
        case = _case(leading_likelihood=0.4)

        await _apply(
            case,
            solutions_to_add=_fix_entry(),
            milestones=MilestoneUpdates(solution_accepted=True),
        )

        assert _diagnostic(case).reported_done_in_turn == case.current_turn

    async def test_a_report_naming_nothing_on_record_asks_for_the_name(self):
        """No ref, nothing pending, nothing proposed: there is no action to
        stamp, and the notice says how to put the change on record."""
        case = _case(leading_likelihood=0.4)

        metadata = await _apply(
            case, milestones=MilestoneUpdates(solution_accepted=True)
        )

        assert case.proposed_actions == []
        text = _notices(case, metadata)
        assert "put it on record in ONE response" in text
        assert "recorded as carried out (<user_changes>)" in text

    async def test_the_first_report_wins(self):
        case = _downgraded_case()
        action = _diagnostic(case)
        accept = MilestoneUpdates(
            solution_accepted=True, accepted_action_ref=action.action_id
        )
        await _apply(case, milestones=accept)
        case.current_turn = 7
        await _apply(case, milestones=accept)

        assert action.reported_done_in_turn == 5

    async def test_a_later_licensed_re_proposal_registers_at_its_own_turn(self):
        """The recorded change becomes the fix only through the existing
        registration path, dated at the registration (no backdating)."""
        case = _downgraded_case()
        action = _diagnostic(case)
        await _apply(
            case,
            milestones=MilestoneUpdates(
                solution_accepted=True, accepted_action_ref=action.action_id
            ),
        )
        case.current_turn = 8

        await _apply(
            case,
            root_cause_conclusion=SimpleNamespace(
                root_cause="ExecStart points at a binary the upgrade moved",
                mechanism="systemd cannot exec the configured path (203/EXEC)",
                evidence_ids=["ev_000000000001"],
                likelihood=0.85,
            ),
            solutions_to_add=_fix_entry(),
            milestones=MilestoneUpdates(
                solution_accepted=True, accepted_action_ref="new_index_0"
            ),
        )

        (solution,) = [
            a
            for a in case.proposed_actions
            if a.action_type == InvestigationActionType.SOLUTION
        ]
        assert solution.state == "accepted"
        assert solution.accepted_in_turn == 8
        assert solution.reported_done_in_turn is None
        assert action.reported_done_in_turn == 5  # never cleared
        assert reported_change_turns(case) == []  # registered under its identity

    async def test_a_reworded_re_proposal_registers_as_another_fix(self):
        """#3047: a fix's identity is its description, so a re-proposal that
        rewords the reported change registers as another fix at its own turn,
        and the report stays listed as not registered (never backdated)."""
        case = _downgraded_case()
        action = _diagnostic(case)
        await _apply(
            case,
            milestones=MilestoneUpdates(
                solution_accepted=True, accepted_action_ref=action.action_id
            ),
        )
        case.current_turn = 8

        await _apply(
            case,
            root_cause_conclusion=SimpleNamespace(
                root_cause="ExecStart points at a binary the upgrade moved",
                mechanism="systemd cannot exec the configured path (203/EXEC)",
                evidence_ids=["ev_000000000001"],
                likelihood=0.85,
            ),
            solutions_to_add=_fix_entry("Correct the unit's executable path"),
            milestones=MilestoneUpdates(
                solution_accepted=True, accepted_action_ref="new_index_0"
            ),
        )

        (solution,) = [
            a
            for a in case.proposed_actions
            if a.action_type == InvestigationActionType.SOLUTION
        ]
        assert solution.state == "accepted"
        assert solution.accepted_in_turn == 8
        assert action.state != "accepted" and action.accepted_in_turn is None
        assert [w.fix_turn for w in fix_windows(case)] == [None, 8]
        assert reported_change_turns(case) == [5]

    async def test_a_reported_workaround_is_recorded_by_mitigation_accepted(self):
        """The mitigation twin: a WORKAROUND 3D downgraded (no symptom
        evidence), reported through ``mitigation_accepted``."""
        case = _case(leading_likelihood=0.4)
        case.evidence.clear()
        await _apply(
            case,
            solutions_to_add=[
                SolutionToAdd(
                    description="Restart billing-exporter",
                    solution_type=SolutionType.WORKAROUND,
                    estimated_impact="restores scraping",
                    risks="low",
                )
            ],
        )
        action = _diagnostic(case)
        case.current_turn = 5

        metadata = await _apply(
            case, milestones=MilestoneUpdates(mitigation_accepted=True)
        )

        assert action.reported_done_in_turn == 5
        assert case.progress.mitigation is None
        assert f"{action.action_id} is on record as carried out" in _notices(
            case, metadata
        )


#: The simulator's report sentences (#3039, simulator comment). The engine
#: reads none of them: whether the model sets ``solution_accepted`` is the
#: report. A positive form sets it; a held or refused one does not.
_REPORTS = [
    ('Done: "<change>".', True),
    ('That\'s already in place: "<change>".', True),
    ('Yes, that\'s done: "<change>".', True),
    ('I last made "<change>" at 04:44.', True),
    ('… — read after I made "<change>"', True),
    ('No, I haven\'t made "<change>".', False),
    (
        'I haven\'t made "<change>" — what I asked about the root cause is '
        "still open.",
        False,
    ),
]


@pytest.mark.asyncio
class TestOnlyAReportRecords:
    @pytest.mark.parametrize("sentence,reported", _REPORTS)
    async def test_each_simulator_sentence(self, sentence, reported):
        case = _downgraded_case()
        action = _diagnostic(case)
        milestones = (
            MilestoneUpdates(
                solution_accepted=True, accepted_action_ref=action.action_id
            )
            if reported
            else MilestoneUpdates(accepted_action_ref=action.action_id)
        )

        await _apply(case, milestones=milestones)

        assert (action.reported_done_in_turn == 5) is reported, sentence

    async def test_an_unjustified_report_records_nothing(self):
        case = _downgraded_case()
        action = _diagnostic(case)
        metadata = _meta()
        metadata[FIX_JUSTIFICATION_KEY] = "solution_accepted lacks a justification"

        await _apply(
            case,
            metadata,
            milestones=MilestoneUpdates(
                solution_accepted=True, accepted_action_ref=action.action_id
            ),
        )

        assert action.reported_done_in_turn is None


class _Fold:
    """A narration fold double: ``outcome`` returns the record given."""

    def __init__(self, leg=None, record=True):
        self._record = (
            SimpleNamespace(cause=SimpleNamespace(identification_leg=leg))
            if record
            else None
        )

    def outcome(self, _block):
        return self._record


def _reported_case():
    case = _downgraded_case()
    _diagnostic(case).reported_done_in_turn = 5
    case.current_turn = 7
    return case


class TestTheRecordRenders:
    def test_the_block_lists_the_change_with_its_turn_and_the_rule(self):
        case = _reported_case()
        action = _diagnostic(case)

        block = user_changes_block(case, _Fold())

        assert block.startswith("<user_changes>")
        assert block.endswith("</user_changes>")
        assert f"- [T5] {action.action_id}: {_FIX} — recorded as carried out" in block
        assert USER_CHANGES_RULE in block
        assert USER_CHANGES_REGISTER_LINE not in block

    def test_the_register_line_follows_an_identified_cause_on_record(self):
        case = _reported_case()
        assert USER_CHANGES_REGISTER_LINE in user_changes_block(
            case, _Fold(leg="chain")
        )
        assert USER_CHANGES_REGISTER_LINE not in user_changes_block(
            case, _Fold(record=False)
        )

    def test_the_register_line_asks_for_the_description_unchanged(self):
        """#3047: the register path matches by description (``fix_identity``),
        so the line the model reads says so."""
        block = user_changes_block(_reported_case(), _Fold(leg="chain"))
        assert "re-propose it, with its description unchanged" in block

    def test_a_superseded_reported_change_still_renders(self):
        """``_retire_shadowed_diagnostic_asks`` may supersede it: the block
        reads the field, never the state."""
        case = _reported_case()
        action = _diagnostic(case)
        action.state = "superseded"
        action.superseded_reason = "stale_pending"

        assert f"[T5] {action.action_id}" in user_changes_block(case, _Fold())

    def test_a_registered_fix_is_listed_once_as_registered(self):
        case = _reported_case()
        case.proposed_actions.append(
            ProposedAction(
                case_id=case.case_id,
                action_type=InvestigationActionType.SOLUTION,
                description=_FIX,
                proposed_in_turn=8,
                state="accepted",
                accepted_in_turn=8,
            )
        )

        block = user_changes_block(case, _Fold())

        assert block.count(_FIX) == 1
        assert "[T8]" in block and "registered as the fix" in block
        assert "[T5]" not in block

    def test_no_change_no_block(self):
        assert user_changes_block(_downgraded_case(), _Fold()) == ""

    def test_the_prompt_moves_a_reported_change_out_of_pending_action(self):
        before = build_investigation_context(_downgraded_case(), "status pasted")
        assert "<pending_action>" in before["pending_action"]
        assert "IF_CARRIED_OUT: set solution_accepted" in before["pending_action"]
        assert "Actions the user reported carrying out" not in before["pending_action"]

        after = build_investigation_context(_reported_case(), "status pasted")
        assert "<pending_action>" not in after["pending_action"]
        assert "<user_changes>" in after["pending_action"]
        assert USER_CHANGES_RULE in after["pending_action"]

    def test_the_block_is_capped_newest_kept(self):
        case = _downgraded_case()
        case.proposed_actions.clear()
        for turn in range(1, 12):
            case.proposed_actions.append(
                ProposedAction(
                    case_id=case.case_id,
                    action_type=InvestigationActionType.DIAGNOSTIC,
                    description=f"change {turn}",
                    proposed_in_turn=turn,
                    reported_done_in_turn=turn,
                )
            )

        block = user_changes_block(case, _Fold())

        assert "[... 3 earlier actions omitted ...]" in block
        assert "[T3]" not in block and "[T4]" in block and "[T11]" in block


class TestTheClosureLine:
    def test_a_reported_change_is_said_instead_of_no_fix(self):
        line = closed_line(
            ClosureReason.CLOSED_INSUFFICIENT_EVIDENCE, "chain", (), reported=[4]
        )
        assert "No fix was carried out" not in line
        assert "The user reported carrying out a proposed action (turn 4)" in line
        assert "not registered as a fix of the identified cause" in line

    def test_with_no_report_the_line_is_unchanged(self):
        line = closed_line(ClosureReason.CLOSED_INSUFFICIENT_EVIDENCE, "chain", ())
        assert line.endswith("No fix was carried out in the case.")

    def test_a_deferred_fix_with_a_report(self):
        line = closed_line(ClosureReason.SOLUTION_DEFERRED, "chain", (), reported=[4])
        assert "It was not carried out in the case." not in line
        assert "(turn 4)" in line

    def test_the_turns_come_from_the_unregistered_reports(self):
        assert reported_change_turns(_reported_case()) == [5]
        assert reported_change_turns(_downgraded_case()) == []


@pytest.mark.asyncio
class TestReviewCases:
    """The review of #3049: the report's own turn, refused SOLUTION bearers,
    the workaround marker and the cue."""

    async def test_a_report_declared_in_the_previous_message_is_dated_there(self):
        case = _downgraded_case()
        action = _diagnostic(case)
        case.current_turn = 6

        await _apply(
            case,
            milestones=MilestoneUpdates(
                solution_accepted=True,
                accepted_action_ref=action.action_id,
                accepted_fix_reported_in="previous_message",
            ),
        )

        assert action.reported_done_in_turn == 5

    @pytest.mark.parametrize("state", ["superseded", "rejected"])
    async def test_a_refused_solution_bearer_is_recorded(self, state):
        """A SOLUTION withdrawn while its cause was down, or rejected, is
        refused; the user's report of carrying it out is still recorded."""
        case = _case(leading_likelihood=0.4)
        case.current_turn = 6
        solution = ProposedAction(
            case_id=case.case_id,
            action_type=InvestigationActionType.SOLUTION,
            description=_FIX,
            proposed_in_turn=4,
            state=state,
            superseded_reason="license_lost" if state == "superseded" else None,
            superseded_in_turn=5 if state == "superseded" else None,
        )
        case.proposed_actions.append(solution)

        metadata = await _apply(
            case,
            milestones=MilestoneUpdates(
                solution_accepted=True, accepted_action_ref=solution.action_id
            ),
        )

        assert solution.reported_done_in_turn == 6
        assert solution.state == state
        assert case.progress.solution_accepted is False
        assert reported_change_turns(case) == [6]
        assert "is on record as carried out at turn 6" in _notices(case, metadata)

    @pytest.mark.parametrize("reason", ["downgraded (M5)", WHY_OPEN_DOWNGRADE_REASON])
    async def test_mitigation_accepted_never_stamps_a_downgraded_fix(self, reason):
        """Only the 3D marker makes a DIAGNOSTIC a workaround: an M5 copy
        sharing a workaround's text is not stamped by mitigation_accepted.
        The notice names the fix and the channel that records it
        (``case_068b23d0c444`` turn 17, ``case_076a60d585b3`` turn 6)."""
        case = _downgraded_case()
        action = _diagnostic(case)
        action.downgrade_reason = reason
        case.solutions.append(
            Solution(
                solution_id="sol_000000000001",
                solution_type=SolutionType.WORKAROUND,
                title="Solution: workaround",
                immediate_action=action.description,
            )
        )

        metadata = await _apply(
            case, milestones=MilestoneUpdates(mitigation_accepted=True)
        )

        assert action.reported_done_in_turn is None
        aid = action.action_id
        text = _notices(case, metadata)
        assert "'mitigation_accepted' was not registered" in text
        assert f"DIAGNOSTIC {aid} is a downgraded fix" in text
        assert f"report it as {aid}'s IF_CARRIED_OUT line in <pending_action>" in text
        assert "on record as carried out" not in text

    async def test_the_named_fix_is_recorded_at_the_report_turn(self):
        """The turn after the notice, the model names the fix as told, with
        the report in the previous message: stamped at the report's turn."""
        case = _downgraded_case()
        action = _diagnostic(case)
        metadata = await _apply(
            case, milestones=MilestoneUpdates(mitigation_accepted=True)
        )
        assert action.reported_done_in_turn is None
        assert "is a downgraded fix" in _notices(case, metadata)
        # The cue the notice points at, rendered while the fix is unreported.
        cue = build_investigation_context(case, "status pasted")["pending_action"]
        assert f"accepted_action_ref: {action.action_id}" in cue
        case.current_turn = 6

        await _apply(
            case,
            milestones=MilestoneUpdates(
                solution_accepted=True,
                accepted_action_ref=action.action_id,
                accepted_fix_reported_in="previous_message",
            ),
        )

        assert action.reported_done_in_turn == 5
        assert action.state == "pending"
        assert case.progress.solution_accepted is False

    async def test_a_pending_workaround_wins_over_a_downgraded_fix(self):
        """Both pending: the 3D workaround type-matches and is stamped; the
        fix is neither stamped nor named."""
        case = _downgraded_case()
        fix = _diagnostic(case)
        workaround = ProposedAction(
            case_id=case.case_id,
            action_type=InvestigationActionType.DIAGNOSTIC,
            description="Restart billing-exporter",
            proposed_in_turn=4,
            downgrade_reason=NO_SYMPTOM_EVIDENCE_DOWNGRADE_REASON,
        )
        case.proposed_actions.append(workaround)

        metadata = await _apply(
            case, milestones=MilestoneUpdates(mitigation_accepted=True)
        )

        assert workaround.reported_done_in_turn == 5
        assert fix.reported_done_in_turn is None
        assert "is a downgraded fix" not in _notices(case, metadata)

    async def test_a_reported_workaround_does_not_hide_an_unreported_fix(self):
        """A workaround stamped earlier is not matched again: the report of
        the fix proposed since is named, not absorbed by the old stamp."""
        case = _downgraded_case()
        fix = _diagnostic(case)
        workaround = ProposedAction(
            case_id=case.case_id,
            action_type=InvestigationActionType.DIAGNOSTIC,
            description="Restart billing-exporter",
            proposed_in_turn=2,
            downgrade_reason=NO_SYMPTOM_EVIDENCE_DOWNGRADE_REASON,
            reported_done_in_turn=3,
        )
        case.proposed_actions.insert(0, workaround)

        metadata = await _apply(
            case, milestones=MilestoneUpdates(mitigation_accepted=True)
        )

        assert workaround.reported_done_in_turn == 3
        assert fix.reported_done_in_turn is None
        assert f"DIAGNOSTIC {fix.action_id} is a downgraded fix" in _notices(
            case, metadata
        )

    async def test_the_notice_names_the_newest_unreported_fix(self):
        """A fix already on record as carried out is not named again; the
        older unreported one is."""
        case = _downgraded_case()
        older = _diagnostic(case)
        reported = ProposedAction(
            case_id=case.case_id,
            action_type=InvestigationActionType.DIAGNOSTIC,
            description="Pin billing-exporter to 2.4.1",
            proposed_in_turn=4,
            downgrade_reason=WHY_OPEN_DOWNGRADE_REASON,
            reported_done_in_turn=4,
        )
        case.proposed_actions.append(reported)

        metadata = await _apply(
            case, milestones=MilestoneUpdates(mitigation_accepted=True)
        )

        text = _notices(case, metadata)
        assert f"DIAGNOSTIC {older.action_id} is a downgraded fix" in text
        assert reported.action_id not in text

    async def test_a_mitigation_report_with_nothing_downgraded_names_nothing(self):
        """``case_0bab2dc5aa92`` turn 6 shape: the user reports a change no
        stored action holds (the downgraded fix comes a turn later)."""
        case = _case(leading_likelihood=0.4)
        case.current_turn = 6

        metadata = await _apply(
            case, milestones=MilestoneUpdates(mitigation_accepted=True)
        )

        assert reported_change_turns(case) == []
        text = _notices(case, metadata)
        assert "'mitigation_accepted' was not registered" in text
        assert "is a downgraded fix" not in text

    async def test_an_unjustified_solution_report_does_not_hold_back_a_workaround(
        self,
    ):
        case = _case(leading_likelihood=0.4)
        case.evidence.clear()
        await _apply(
            case,
            solutions_to_add=[
                SolutionToAdd(
                    description="Restart billing-exporter",
                    solution_type=SolutionType.WORKAROUND,
                    estimated_impact="restores scraping",
                    risks="low",
                )
            ],
        )
        action = _diagnostic(case)
        case.current_turn = 5
        metadata = _meta()
        metadata[FIX_JUSTIFICATION_KEY] = "solution_accepted lacks a justification"

        await _apply(
            case, metadata, milestones=MilestoneUpdates(mitigation_accepted=True)
        )

        assert action.reported_done_in_turn == 5


class TestTheCue:
    def _pending(self, case, fold):
        from faultmaven.core.investigation.prompts.context_builder.assembly import (
            _if_carried_out,
        )

        return _if_carried_out(_diagnostic(case), fold)

    def test_a_downgraded_fix_is_named_while_the_cause_is_down(self):
        cue = self._pending(_downgraded_case(), _Fold())
        assert "set solution_accepted, with accepted_action_ref: act_" in cue

    def test_once_the_cause_is_identified_the_cue_registers(self):
        cue = self._pending(_downgraded_case(), _Fold(leg="rcc"))
        assert (
            "re-propose it, with its description unchanged, as a SolutionToAdd" in cue
        )
        assert "registers it as the fix" in cue

    def test_a_downgraded_workaround_is_reported_by_mitigation_accepted(self):
        case = _downgraded_case()
        _diagnostic(case).downgrade_reason = NO_SYMPTOM_EVIDENCE_DOWNGRADE_REASON
        cue = self._pending(case, _Fold(leg="rcc"))
        assert "set mitigation_accepted" in cue
        assert "solution_accepted" not in cue

    def test_a_recorded_workaround_says_why_and_gets_no_register_line(self):
        case = _reported_case()
        _diagnostic(case).downgrade_reason = NO_SYMPTOM_EVIDENCE_DOWNGRADE_REASON

        block = user_changes_block(case, _Fold(leg="chain"))

        assert "not registered as the mitigation (no symptom evidence" in block
        assert USER_CHANGES_REGISTER_LINE not in block
