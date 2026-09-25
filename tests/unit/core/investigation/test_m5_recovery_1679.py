"""M5 judges this turn's hypotheses, and a downgraded fix the user applied can be registered (fm#1679).

``case_0e35b59ddb9d`` turn 4: the model created a hypothesis, an evidence link
raised it to 0.65, and in the same turn it proposed the fix. M5 downgraded the
fix to DIAGNOSTIC because it read ``case.working_conclusion``, which was still
the previous turn's (built at Step 5.6, after the apply). By M5's own rule the
fix was licensed from the next turn. The user then applied it, and the
downgrade note told the model to re-propose and wait for the user to accept a
fix they had already carried out.

- ``TestLicenseReadsThisTurn`` — M5 and the end-of-turn license re-check judge
  the working-conclusion leg on the hypotheses as they stand, not on the field.
- ``TestLicenseReadsTheSettledTurn`` — the re-check also sees likelihood
  updates that land after M5 (deferred past chain emission).
- ``TestAppliedFixRecovery`` — the one-response path both notes now promise
  registers the fix from the downgraded state.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from faultmaven.core.investigation.hypothesis_manager import HypothesisManager
from faultmaven.core.investigation.milestone_engine import MilestoneEngine
from faultmaven.core.investigation.schemas import MilestoneUpdates, SolutionToAdd
from faultmaven.modules.case.domain.models import (
    Case,
    CaseState,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisState,
    InquiryData,
    InvestigationActionType,
    ProposedAction,
    SolutionType,
    WorkingConclusion,
)

pytestmark = pytest.mark.unit


class _Updates:
    """Any field not set reads as None, so only the set blocks run."""

    def __init__(self, **set_fields):
        self.__dict__.update(set_fields)

    def __getattr__(self, name):
        return None


def _meta() -> dict:
    return {
        "milestones_completed": [],
        "evidence_added": [],
        "hypotheses_generated": [],
        "hypotheses_validated": [],
        "solutions_proposed": [],
        "evidence_needs_updated": [],
        "progress_made": False,
        "status_transitioned": False,
    }


def _make_engine() -> MilestoneEngine:
    """Bare engine with the chain-emission tail stubbed, as the M5 gate tests do."""
    eng = MilestoneEngine.__new__(MilestoneEngine)
    eng._apply_chain_emission = lambda *a, **k: None
    eng._nudge_ambiguous_orphan_chains = lambda *a, **k: None
    return eng


def _case(*, leading_likelihood: float, stale_wc: float | None = None) -> Case:
    """Symptom verified, no root-cause conclusion, one ACTIVE hypothesis.

    ``stale_wc`` is what ``case.working_conclusion`` still holds from the
    previous turn; ``None`` is turn 4's state (no hypothesis existed on turn 3).
    """
    case = Case(
        user_id="u1",
        enterprise_id="o1",
        title="t",
        description="billing-exporter fails to start",
        state=CaseState.INVESTIGATING,
        inquiry=InquiryData(
            proposed_problem_statement="billing-exporter fails to start",
            problem_statement_confirmed=True,
        ),
    )
    case.current_turn = 4
    case.progress.symptom_verified = True
    case.evidence.append(
        Evidence(
            evidence_id="ev_000000000001",
            summary="status=203/EXEC",
            content_ref="status.txt",
            category=EvidenceCategory.SYMPTOM_EVIDENCE,
            source_type=EvidenceSourceType.USER_DESCRIPTION,
            collected_at=datetime.now(UTC),
            collected_by="user",
            primary_purpose="symptom",
            collected_at_turn=3,
        )
    )
    hyp = Hypothesis(
        hypothesis_id="hyp_000000000001",
        statement="ExecStart points at a binary the upgrade moved",
        category=HypothesisCategory.CONFIG,
        state=HypothesisState.ACTIVE,
        generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
        rationale="203/EXEC on a path that no longer exists",
        likelihood=leading_likelihood,
        generated_at_turn=4,
    )
    case.hypotheses[hyp.hypothesis_id] = hyp
    if stale_wc is not None:
        case.working_conclusion = WorkingConclusion(
            statement=hyp.statement,
            reasoning="last turn's reading",
            likelihood=stale_wc,
        )
    return case


def _fix() -> list[SolutionToAdd]:
    return [
        SolutionToAdd(
            description="Point ExecStart at /usr/bin/billing-exporter",
            solution_type=SolutionType.CONFIG_CHANGE,
            estimated_impact="the unit starts",
            risks="low",
            commands=["systemctl daemon-reload"],
        )
    ]


def _actions(case: Case, action_type: InvestigationActionType) -> list[ProposedAction]:
    return [a for a in case.proposed_actions if a.action_type == action_type]


@pytest.mark.asyncio
class TestLicenseReadsThisTurn:
    async def test_fix_is_not_downgraded_on_the_turn_its_hypothesis_crosses(self):
        """Turn 4 of case_0e35b59ddb9d: 0.65 now, nothing in the field yet."""
        case = _case(leading_likelihood=0.65, stale_wc=None)

        await _make_engine()._apply_investigation_updates(
            case, _Updates(solutions_to_add=_fix()), _meta()
        )

        action = case.proposed_actions[-1]
        assert action.action_type == InvestigationActionType.SOLUTION
        assert action.downgrade_reason is None
        assert action.state == "pending", "the end-of-turn re-check kept it"
        assert case.progress.solution_proposed is True

    async def test_the_bar_is_unchanged_below_threshold(self):
        case = _case(leading_likelihood=0.55, stale_wc=None)

        await _make_engine()._apply_investigation_updates(
            case, _Updates(solutions_to_add=_fix()), _meta()
        )

        action = case.proposed_actions[-1]
        assert action.action_type == InvestigationActionType.DIAGNOSTIC
        assert action.downgrade_reason is not None

    async def test_a_stale_license_does_not_admit_a_fix(self):
        """The other direction: last turn's 0.65 must not license a fix once
        the leading hypothesis stands at 0.55 this turn."""
        case = _case(leading_likelihood=0.55, stale_wc=0.65)

        await _make_engine()._apply_investigation_updates(
            case, _Updates(solutions_to_add=_fix()), _meta()
        )

        assert case.proposed_actions[-1].action_type == (
            InvestigationActionType.DIAGNOSTIC
        )

    async def test_a_pending_fix_is_withdrawn_the_turn_its_license_falls(self):
        """The recompute's re-check (INV-32) reads this turn's hypotheses too:
        a license resting on last turn's 0.65 falls in the same turn."""
        case = _case(leading_likelihood=0.55, stale_wc=0.65)
        case.proposed_actions.append(
            ProposedAction(
                case_id=case.case_id,
                action_type=InvestigationActionType.SOLUTION,
                description="Point ExecStart at /usr/bin/billing-exporter",
                proposed_in_turn=3,
            )
        )
        metadata = _meta()

        await _make_engine()._apply_investigation_updates(case, _Updates(), metadata)

        (offer,) = _actions(case, InvestigationActionType.SOLUTION)
        assert offer.state == "superseded"
        assert offer.superseded_reason == "license_lost"


@pytest.mark.asyncio
class TestLicenseReadsTheSettledTurn:
    async def test_a_fix_undercut_later_in_the_turn_is_withdrawn_that_turn(self):
        """M5 admits on the hypotheses at the solutions step; the model's own
        likelihood update lands after chain emission and drops the leader to
        0.55. The end-of-turn re-check must judge that settled value."""
        case = _case(leading_likelihood=0.65, stale_wc=None)
        engine = _make_engine()
        engine.hypothesis_manager = HypothesisManager()
        metadata = _meta()
        # What _apply_hypothesis_updates stashes for an LLM likelihood update.
        metadata["deferred_likelihood_updates"] = [("hyp_000000000001", 0.55)]

        await engine._apply_investigation_updates(
            case, _Updates(solutions_to_add=_fix()), metadata
        )

        assert case.hypotheses["hyp_000000000001"].likelihood == pytest.approx(0.55)
        (offer,) = _actions(case, InvestigationActionType.SOLUTION)
        assert offer.state == "superseded"
        assert offer.superseded_reason == "license_lost"
        assert case.progress.solution_proposed is False


def _downgraded_case() -> Case:
    """The #1679 state after turn 4: symptom verified, no conclusion, the fix
    standing as a downgraded DIAGNOSTIC, and no hypothesis carrying a license."""
    case = _case(leading_likelihood=0.4)
    case.current_turn = 5
    case.proposed_actions.append(
        ProposedAction(
            case_id=case.case_id,
            action_type=InvestigationActionType.DIAGNOSTIC,
            description="Point ExecStart at /usr/bin/billing-exporter",
            proposed_in_turn=4,
            downgrade_reason="downgraded (M5)",
        )
    )
    return case


@pytest.mark.asyncio
class TestAppliedFixRecovery:
    async def test_one_response_registers_the_applied_fix(self):
        """What the downgrade note and the stage-gate notice now tell the model
        to do when the user has already carried out the fix."""
        case = _downgraded_case()
        updates = _Updates(
            root_cause_conclusion=SimpleNamespace(
                root_cause="ExecStart points at a binary the upgrade moved",
                mechanism="systemd cannot exec the configured path (203/EXEC)",
                evidence_ids=["ev_000000000001"],
                likelihood=0.85,
            ),
            solutions_to_add=_fix(),
            milestones=MilestoneUpdates(solution_accepted=True),
        )
        metadata = _meta()

        await _make_engine()._apply_investigation_updates(case, updates, metadata)

        assert case.progress.solution_accepted is True
        assert case.progress.solution_proposed is True
        (diagnostic,) = _actions(case, InvestigationActionType.DIAGNOSTIC)
        (solution,) = _actions(case, InvestigationActionType.SOLUTION)
        assert diagnostic.state == "superseded"
        assert solution.state == "accepted"
        assert "was not registered" not in (metadata.get("system_feedback") or "")

    async def test_the_downgrade_note_names_that_path(self):
        case = _case(leading_likelihood=0.4)

        await _make_engine()._apply_investigation_updates(
            case, _Updates(solutions_to_add=_fix()), _meta()
        )

        note = case.proposed_actions[-1].downgrade_reason
        # What was true when it was proposed, not a claim about now: the note
        # is rendered on every later turn the action stays pending.
        assert "when proposed, the root cause was not established" in note
        assert "is not yet established" not in note
        for part in ("ONE response", "root_cause_conclusion", "SolutionToAdd"):
            assert part in note
        assert "already carried out the fix" in note
        assert "solution_accepted" in note

    async def test_a_refused_accept_names_that_path(self):
        """The model claims solution_accepted with only the DIAGNOSTIC pending."""
        case = _downgraded_case()
        metadata = _meta()

        await _make_engine()._apply_investigation_updates(
            case,
            _Updates(milestones=MilestoneUpdates(solution_accepted=True)),
            metadata,
        )

        assert case.progress.solution_accepted is False
        feedback = metadata.get("system_feedback") or ""
        assert "'solution_accepted' was not registered" in feedback
        assert "ONE response" in feedback
        assert "do not ask them to accept it again" in feedback
