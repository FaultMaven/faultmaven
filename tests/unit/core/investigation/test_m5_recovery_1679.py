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
- ``TestLicenseReadsTheSettledTurn`` — M5 scores each hypothesis at the value
  the model's pending likelihood update will leave, and the re-check reads the
  conclusion rebuilt after the cause recompute.
- ``TestAppliedFixRecovery`` — the one-response path both notes now promise
  registers the fix from the downgraded state.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from faultmaven.core.investigation.hypothesis_manager import HypothesisManager
from faultmaven.core.investigation import milestone_engine
from faultmaven.core.investigation.milestone_engine import MilestoneEngine
from faultmaven.core.investigation.schemas import MilestoneUpdates, SolutionToAdd
from faultmaven.modules.case.domain.models import (
    Case,
    CaseState,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    EvidenceStance,
    Hypothesis,
    HypothesisCategory,
    HypothesisEvidenceLink,
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


def _case(
    *, leading_likelihood: float, stale_wc: float | None = None, supported: bool = False
) -> Case:
    """Symptom verified, no root-cause conclusion, one ACTIVE hypothesis.

    ``stale_wc`` is what ``case.working_conclusion`` still holds from the
    previous turn; ``None`` is turn 4's state (no hypothesis existed on turn 3).
    ``supported`` gives the hypothesis a confident SUPPORTS link, so a raise
    is not held at the evidence-free prior cap.
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
        evidence_links=(
            [
                HypothesisEvidenceLink(
                    hypothesis_id="hyp_000000000001",
                    evidence_id="ev_000000000001",
                    stance=EvidenceStance.SUPPORTS,
                    reasoning="203/EXEC on the configured path",
                    stance_confidence=0.9,
                )
            ]
            if supported
            else []
        ),
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


def _engine_with_manager() -> MilestoneEngine:
    engine = _make_engine()
    engine.hypothesis_manager = HypothesisManager()
    return engine


def _pending(likelihood: float) -> dict:
    """Metadata carrying the model's likelihood update for the one hypothesis,
    as ``_apply_hypothesis_updates`` stashes it for after chain emission."""
    metadata = _meta()
    metadata["deferred_likelihood_updates"] = [("hyp_000000000001", likelihood)]
    return metadata


@pytest.mark.asyncio
class TestLicenseReadsTheSettledTurn:
    """Mid-turn, an evidence link has already rewritten the likelihood by
    formula, while the model's own value lands after chain emission. M5 must
    judge the value the turn settles on, in either direction."""

    async def test_a_pending_lowering_binds_m5_even_with_a_same_turn_accept(self):
        """The formula put the hypothesis at 0.65; the model says 0.45 and, in
        the same response, proposes the fix and marks it accepted."""
        case = _case(leading_likelihood=0.65, stale_wc=0.45)

        await _engine_with_manager()._apply_investigation_updates(
            case,
            _Updates(
                solutions_to_add=_fix(),
                milestones=MilestoneUpdates(solution_accepted=True),
            ),
            _pending(0.45),
        )

        assert case.hypotheses["hyp_000000000001"].likelihood == pytest.approx(0.45)
        assert case.progress.solution_accepted is False
        assert _actions(case, InvestigationActionType.SOLUTION) == []
        (diagnostic,) = _actions(case, InvestigationActionType.DIAGNOSTIC)
        assert diagnostic.downgrade_reason is not None

    async def test_a_formula_reset_below_the_bar_does_not_refuse_a_held_license(self):
        """Stood at 0.8 last turn; a new supporting link resets the formula to
        0.2 + 2×0.15 = 0.5; the model restates 0.85. Licensed before and after
        the turn, so the fix must stand."""
        case = _case(leading_likelihood=0.5, stale_wc=0.8, supported=True)
        hypothesis = case.hypotheses["hyp_000000000001"]
        hypothesis.initial_likelihood = 0.2

        await _engine_with_manager()._apply_investigation_updates(
            case, _Updates(solutions_to_add=_fix()), _pending(0.85)
        )

        assert hypothesis.likelihood == pytest.approx(0.85)
        (offer,) = _actions(case, InvestigationActionType.SOLUTION)
        assert offer.state == "pending"
        assert offer.downgrade_reason is None

    async def test_a_pending_raise_does_not_withhold_a_license_already_held(self):
        case = _case(leading_likelihood=0.65, stale_wc=None, supported=True)

        await _engine_with_manager()._apply_investigation_updates(
            case,
            _Updates(
                solutions_to_add=_fix(),
                milestones=MilestoneUpdates(solution_accepted=True),
            ),
            _pending(0.8),
        )

        assert case.hypotheses["hyp_000000000001"].likelihood == pytest.approx(0.8)
        assert case.progress.solution_accepted is True

    async def test_a_raise_the_prior_cap_will_hold_does_not_license(self):
        """No confident supporting link: the B1 cap holds the model's 0.8 at
        max(0.55, prior cap), so M5 must not score it at 0.8."""
        case = _case(leading_likelihood=0.55, stale_wc=None)

        await _engine_with_manager()._apply_investigation_updates(
            case, _Updates(solutions_to_add=_fix()), _pending(0.8)
        )

        assert case.hypotheses["hyp_000000000001"].likelihood == pytest.approx(0.55)
        assert case.proposed_actions[-1].action_type == (
            InvestigationActionType.DIAGNOSTIC
        )

    async def test_a_license_refuted_inside_the_recompute_is_withdrawn_that_turn(
        self, monkeypatch
    ):
        """M6 demotion runs inside the cause recompute, after every likelihood
        update. The re-check must read the conclusion rebuilt after it."""
        case = _case(leading_likelihood=0.65, stale_wc=0.65)
        case.proposed_actions.append(
            ProposedAction(
                case_id=case.case_id,
                action_type=InvestigationActionType.SOLUTION,
                description="Point ExecStart at /usr/bin/billing-exporter",
                proposed_in_turn=3,
            )
        )
        real = milestone_engine._recompute_cause_state_from_chain

        def _recompute_with_m6(case, **kwargs):
            validated = real(case, **kwargs)
            hypothesis = case.hypotheses["hyp_000000000001"]
            hypothesis.state = HypothesisState.REFUTED
            hypothesis.likelihood = 0.0
            return validated

        monkeypatch.setattr(
            milestone_engine, "_recompute_cause_state_from_chain", _recompute_with_m6
        )

        await _make_engine()._apply_investigation_updates(case, _Updates(), _meta())

        (offer,) = _actions(case, InvestigationActionType.SOLUTION)
        assert offer.state == "superseded"
        assert offer.superseded_reason == "license_lost"


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
        assert "when proposed, no root cause was established" in note
        assert "is not yet established" not in note
        for part in ("ONE response", "root_cause_conclusion", "SolutionToAdd"):
            assert part in note
        assert "already applied that fix" in note
        assert "no rival cause is contested" in note
        # What M5 exists to refuse must stay refused on the retro path.
        assert "a diagnostic test is not a fix" in note
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
        # Only for a cause that stands: the notice also answers a fix withdrawn
        # because its cause fell, which a fresh conclusion must not re-license.
        assert "the root cause stands established" in feedback
        assert "root_cause_conclusion" not in feedback
