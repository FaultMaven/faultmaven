"""Tests for the Gate 2 PENDING — REINFORCEMENT prompt block.

Pins the prompt-layer reinforcement that fires when Gate 1 has passed
but Gate 2 has not committed. The reminder:

- Forbids structured INVESTIGATION-stage emission while Gate 2 is open
  (no ``hypotheses_to_add`` / no ``causal_evidence`` / no
  ``solutions_to_add``)
- Tells the LLM how to handle the data-without-pick failure mode
  (acknowledge briefly THEN re-assert the path question)

The fire condition is narrow: INQUIRY status + Gate 1 closed +
path_selection is None + preliminary_urgency populated (so the
recommendation helper returns non-None). Tests pin firing in that
exact shape and non-firing on every other.

Engine-side backstop (``_path_selection_suggestions``) is independent
and unchanged — the reminder is prompt-layer reinforcement of the
existing affordance pair (see INV-19).
"""

from __future__ import annotations

from faultmaven.core.investigation.prompts.context_builder import (
    _GATE2_PENDING_REMINDER,
    build_investigation_context,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseStatus,
    InquiryData,
    InvestigationPath,
    PathSelection,
    UrgencyLevel,
)
from faultmaven.modules.case.domain.models import (
    PreliminaryUrgency,
    ProblemConfirmation,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _case(
    *,
    status: CaseStatus = CaseStatus.INVESTIGATING,
    problem_statement_confirmed: bool = True,
    symptom_verified: bool = True,
    has_preliminary_urgency: bool = True,
    path_selection: PathSelection | None = None,
) -> Case:
    """Build a case with controllable gate inputs.

    Post-redesign default: INVESTIGATING + Gate 1 passed +
    symptom_verified + path_selection None + urgency populated —
    i.e., the state where the Gate 2 reminder should fire.
    """
    from faultmaven.modules.case.contracts import InvestigationProgress

    inquiry = InquiryData(
        proposed_problem_statement="Production API returning 503s",
        problem_statement_confirmed=problem_statement_confirmed,
        decided_to_investigate=(status == CaseStatus.INVESTIGATING),
        problem_confirmation=ProblemConfirmation(
            problem_type="unavailability",
            severity_guess="high",
            preliminary_guidance="503s on /api/checkout",
        ),
    )
    if has_preliminary_urgency:
        inquiry.preliminary_urgency = PreliminaryUrgency(
            level=UrgencyLevel.CRITICAL,
            is_ongoing=True,
            is_incident_report=True,
            impact_assessment="prod outage",
            assessed_at_turn=1,
        )
    case = Case(
        user_id="u1",
        organization_id="o1",
        title="Test",
        status=status,
        description="Production API returning 503s",
        inquiry=inquiry,
        progress=InvestigationProgress(symptom_verified=symptom_verified),
    )
    if path_selection is not None:
        case.path_selection = path_selection
    return case


# Backwards-compatible alias for the few tests that still want INQUIRY-shape.
def _inquiry_case(
    *,
    problem_statement_confirmed: bool,
    has_preliminary_urgency: bool = True,
    path_selection: PathSelection | None = None,
) -> Case:
    return _case(
        status=CaseStatus.INQUIRY,
        problem_statement_confirmed=problem_statement_confirmed,
        symptom_verified=False,
        has_preliminary_urgency=has_preliminary_urgency,
        path_selection=path_selection,
    )


def _committed_path() -> PathSelection:
    """A committed Gate 2 selection."""
    return PathSelection(
        path=InvestigationPath.MITIGATION_FIRST,
        auto_selected=True,
        rationale="Ongoing critical impact",
        alternate_path=InvestigationPath.ROOT_CAUSE,
        selected_by="u1",
    )


def _rendered(case: Case) -> str:
    """Render the context and flatten all values into a single string
    so reminder presence/absence can be checked regardless of which ctx
    key the block lands in (currently ``inquiry_state``).
    """
    ctx = build_investigation_context(case, user_message="hello")
    return "\n".join(str(v) for v in ctx.values())


# ---------------------------------------------------------------------------
# Reminder content invariants
# ---------------------------------------------------------------------------


class TestReminderContent:
    """The reminder's content covers the two pieces the original
    ``<path_selection_state>`` block didn't pin: structured-emission
    constraints + data-without-pick behavior. Failure here means the
    block has been weakened or rewritten in a way that loses the
    structural guardrails."""

    def test_block_forbids_hypotheses_emission(self):
        normalized = " ".join(_GATE2_PENDING_REMINDER.split())
        assert "DO NOT emit ``hypotheses_to_add``" in normalized

    def test_block_forbids_causal_evidence_classification(self):
        normalized = " ".join(_GATE2_PENDING_REMINDER.split())
        assert "causal_evidence" in normalized
        assert "DO NOT classify any evidence as ``causal_evidence``" in normalized

    def test_block_forbids_solutions_emission(self):
        normalized = " ".join(_GATE2_PENDING_REMINDER.split())
        assert "DO NOT emit ``solutions_to_add``" in normalized

    def test_block_addresses_data_without_pick_scenario(self):
        """When the user provides data without picking a path, the
        reminder must tell the LLM to acknowledge briefly then re-assert
        the path question — not engage with the data and let the path
        question fade into conversation noise."""
        normalized = " ".join(_GATE2_PENDING_REMINDER.split())
        # Acknowledge briefly
        assert "ONE short sentence" in normalized
        # Re-assert the path question
        assert "re-assert the path question prominently" in normalized

    def test_block_acknowledges_engine_backstop(self):
        """The reminder references the deterministic button pair so the
        LLM knows it doesn't need to invent typed-choice instructions.
        Tied to ``_path_selection_suggestions`` in milestone_engine."""
        normalized = " ".join(_GATE2_PENDING_REMINDER.split())
        assert "COOPERATIVE buttons remain attached" in normalized


# ---------------------------------------------------------------------------
# Conditional injection — the reminder must fire only in the Gate-2-pending shape
# ---------------------------------------------------------------------------


class TestConditionalInjection:
    """The reminder fires only when Gate 1 has closed AND Gate 2 is
    pending AND urgency signals are populated (otherwise the
    recommendation helper returns None and the entire
    ``<path_selection_state>`` block is suppressed).
    """

    # The reminder emits as a sibling XML envelope to <path_selection_state>;
    # its opening tag is a stable, structural marker that doesn't drift if
    # the prose inside is reworded.
    _REMINDER_OPEN_TAG = "<gate2_pending_instructions>"

    def test_reminder_present_when_symptom_verified_and_path_not_committed(self):
        """Post-redesign default state: INVESTIGATING + symptom_verified
        + path_selection None + urgency populated. Reminder fires."""
        case = _case()
        rendered = _rendered(case)
        assert self._REMINDER_OPEN_TAG in rendered
        # And the full structured-emission ban is reachable in the prompt.
        normalized = " ".join(rendered.split())
        assert "DO NOT emit ``hypotheses_to_add``" in normalized

    def test_reminder_absent_in_inquiry(self):
        """Pre-redesign Gate 2 was an INQUIRY-stage concept; post-redesign
        it isn't. The reminder must NOT fire in INQUIRY regardless of
        Gate 1 state. Prevents premature firing."""
        case = _case(
            status=CaseStatus.INQUIRY,
            problem_statement_confirmed=True,
            symptom_verified=False,  # symptom_verified is INVESTIGATING-stage
        )
        rendered = _rendered(case)
        assert self._REMINDER_OPEN_TAG not in rendered

    def test_reminder_absent_when_symptom_not_yet_verified(self):
        """In INVESTIGATING but pre-symptom-verified: agent is still in
        Zone 1 doing symptom validation. Reminder must not fire — Gate
        2 hasn't yet opened. The ``_PRE_PATH_DIAGNOSIS_BLOCK`` carries
        its own constraints for this state."""
        case = _case(symptom_verified=False)
        rendered = _rendered(case)
        assert self._REMINDER_OPEN_TAG not in rendered

    def test_reminder_absent_when_path_committed(self):
        """Once the user has clicked a Gate 2 button (path_selection
        is not None), Gate 2 has closed. Continuing to inject the
        reminder would tell the LLM to keep re-asking — exactly the
        kind of stale-instruction drift the test guards against.
        """
        case = _case(path_selection=_committed_path())
        rendered = _rendered(case)
        assert self._REMINDER_OPEN_TAG not in rendered

    def test_reminder_absent_when_preliminary_urgency_missing(self):
        """Without preliminary_urgency the recommendation helper
        returns None. Both ``<path_selection_state>`` and the reminder
        are gated on the same helper — when it returns None, neither
        block renders. Keeps prompt content coupled to a renderable
        recommendation.
        """
        case = _case(has_preliminary_urgency=False)
        rendered = _rendered(case)
        assert self._REMINDER_OPEN_TAG not in rendered

    # NOTE: Pre-redesign there was a ``test_reminder_absent_outside_inquiry_status``
    # case that pinned an INQUIRY-stage status check. Post-redesign,
    # ``test_reminder_absent_in_inquiry`` above covers the equivalent
    # invariant (reminder must not fire in INQUIRY). The original test
    # has been replaced.
