"""Tests for `engine_owned_affordances`: the consolidated gate-affordance predicate.

`engine_owned_affordances(case, metadata)` is the single source of truth for
"when is a state-machine gate pending, and what is the canonical clickable
affordance pair?" It replaces the previously-scattered handshake-deferred /
Gate 2 / Gate 3 / override_suggestions branches in the response builder with
one pure function.

The architectural commitment these tests pin: **intent ↔ gate.** When a gate
is pending, the engine owns the affordance pair and attaches intent metadata.
When no gate is pending, the function returns None and the LLM's exploratory
COOPERATIVE suggestions pass through unmodified.

Design reference: cross-tier composition seam in INV-01 / INV-19 / INV-21.
"""

from __future__ import annotations

from faultmaven.core.investigation.milestone_engine import (
    _gate1_is_pending,
    _gate2_is_pending,
    _gate3_is_pending,
    engine_owned_affordances,
)
from faultmaven.modules.case.domain.models import (
    Case,
    CaseState,
    InquiryData,
    InvestigationPath,
    PathSelection,
    PreliminaryUrgency,
    ProblemConfirmation,
    UrgencyLevel,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _inquiry_case(
    *,
    proposed_statement: str | None = "Production API is returning 500s",
    problem_statement_confirmed: bool = False,
    path_selection: PathSelection | None = None,
) -> Case:
    """INQUIRY-stage case with controllable Gate 1 / Gate 2 inputs."""
    inquiry = InquiryData(
        proposed_problem_statement=proposed_statement,
        problem_statement_confirmed=problem_statement_confirmed,
    )
    if proposed_statement is not None:
        inquiry.problem_confirmation = ProblemConfirmation(
            problem_type="unavailability",
            severity_guess="high",
            preliminary_guidance="API down",
        )
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
        description=proposed_statement or "",
        inquiry=inquiry,
    )
    if path_selection is not None:
        case.path_selection = path_selection
    return case


def _investigating_case(
    *,
    path_selection: PathSelection | None = None,
    symptom_verified: bool = False,
) -> Case:
    """INVESTIGATING-stage case. Post-redesign, ``path_selection`` is
    written by the Gate 2 click handler in INVESTIGATING (not INQUIRY),
    so it's optional here — pass None to construct a pre-path case."""
    from faultmaven.modules.case.contracts import InvestigationProgress

    inquiry = InquiryData(
        proposed_problem_statement="Production API is returning 500s",
        problem_statement_confirmed=True,
        decided_to_investigate=True,
        preliminary_urgency=PreliminaryUrgency(
            level=UrgencyLevel.CRITICAL,
            is_ongoing=True,
            is_incident_report=True,
            impact_assessment="prod outage",
            assessed_at_turn=1,
        ),
    )
    case = Case(
        user_id="u1",
        organization_id="o1",
        title="Test",
        state=CaseState.INVESTIGATING,
        description="Production API is returning 500s",
        inquiry=inquiry,
        progress=InvestigationProgress(symptom_verified=symptom_verified),
    )
    if path_selection is not None:
        case.path_selection = path_selection
    return case


def _mitigation_first_path(
    *,
    mitigation_completed_at_turn: int | None = None,
    rca_after_mitigation_confirmed: bool = False,
) -> PathSelection:
    """Build a committed MITIGATION_FIRST PathSelection. Existence on a case
    means Gate 2 was passed (after the path-selection commit-timing refactor;
    the legacy ``user_confirmed`` field has been removed)."""
    return PathSelection(
        path=InvestigationPath.MITIGATION_FIRST,
        auto_selected=True,
        rationale="ongoing critical impact",
        alternate_path=InvestigationPath.ROOT_CAUSE,
        selected_by="u1",
        mitigation_completed_at_turn=mitigation_completed_at_turn,
        rca_after_mitigation_confirmed=rca_after_mitigation_confirmed,
    )


def _root_cause_path() -> PathSelection:
    """Build a committed ROOT_CAUSE PathSelection."""
    return PathSelection(
        path=InvestigationPath.ROOT_CAUSE,
        auto_selected=True,
        rationale="historical issue",
        alternate_path=InvestigationPath.MITIGATION_FIRST,
        selected_by="u1",
    )


# ---------------------------------------------------------------------------
# Gate predicates
# ---------------------------------------------------------------------------


class TestGate1Predicate:
    """Gate 1 is pending whenever a proposed_problem_statement is awaiting confirmation."""

    def test_pending_when_statement_proposed_and_unconfirmed(self):
        case = _inquiry_case(problem_statement_confirmed=False)
        assert _gate1_is_pending(case) is True

    def test_not_pending_when_statement_confirmed(self):
        case = _inquiry_case(problem_statement_confirmed=True)
        assert _gate1_is_pending(case) is False

    def test_not_pending_when_no_statement_proposed(self):
        case = _inquiry_case(proposed_statement=None)
        assert _gate1_is_pending(case) is False

    def test_not_pending_when_case_not_inquiry(self):
        case = _investigating_case()
        assert _gate1_is_pending(case) is False


class TestGate2Predicate:
    """Gate 2 is pending when the case is INVESTIGATING with
    ``symptom_verified=True`` and ``path_selection`` not yet
    committed. Post-redesign, the gate fires after symptom verification
    rather than at INQUIRY → INVESTIGATING transition — the path
    choice is data-grounded, not based on user-claimed urgency."""

    def test_pending_when_symptom_verified_and_no_path_committed(self):
        """INVESTIGATING + symptom_verified + path_selection=None → Gate 2 pending."""
        case = _investigating_case(
            path_selection=None,
            symptom_verified=True,
        )
        assert _gate2_is_pending(case) is True

    def test_not_pending_when_symptom_not_yet_verified(self):
        """Pre-symptom_verified, Gate 2 is not yet open — the agent's
        job is still symptom validation."""
        case = _investigating_case(
            path_selection=None,
            symptom_verified=False,
        )
        assert _gate2_is_pending(case) is False

    def test_not_pending_when_path_committed(self):
        """Path_selection existence means user has clicked Gate 2; gate closed."""
        case = _investigating_case(
            path_selection=_root_cause_path(),
            symptom_verified=True,
        )
        assert _gate2_is_pending(case) is False

    def test_not_pending_in_inquiry(self):
        """Pre-redesign Gate 2 fired in INQUIRY; post-redesign it does not.
        Pre-symptom verification of any kind happens after the transition."""
        case = _inquiry_case(
            problem_statement_confirmed=True,
            path_selection=None,
        )
        assert _gate2_is_pending(case) is False


# ---------------------------------------------------------------------------
# Affordance consolidator
# ---------------------------------------------------------------------------


class TestEngineOwnedAffordances:
    """The consolidator returns ``(gate_name, affordance_pair)`` when any gate
    is pending, and ``None`` otherwise. The gate identifiers are
    telemetry-stable labels (gate1 / gate2 / gate3 / disposition).
    """

    def test_returns_none_when_no_gate_pending(self):
        # INVESTIGATING with all gates closed: no engine-owned affordance.
        case = _investigating_case(
            path_selection=_mitigation_first_path(
                mitigation_completed_at_turn=5,
                rca_after_mitigation_confirmed=True,
            ),
        )
        assert engine_owned_affordances(case) is None

    def test_returns_none_for_inquiry_without_proposed_statement(self):
        # First user turn — vague query, no problem statement proposed yet.
        # Engine has nothing to gate on; LLM owns suggestion emission.
        case = _inquiry_case(proposed_statement=None)
        assert engine_owned_affordances(case) is None

    def test_gate1_pending_returns_confirmation_pair(self):
        case = _inquiry_case(problem_statement_confirmed=False)
        result = engine_owned_affordances(case)
        assert result is not None
        gate, affordances = result
        assert gate == "gate1"
        labels = [s["label"] for s in affordances]
        assert "Yes, let's investigate" in labels
        assert any(
            s["intent"] == {"type": "confirmation", "confirmation_value": True}
            for s in affordances
        )

    def test_gate2_pending_returns_path_selection_pair(self):
        case = _investigating_case(
            path_selection=None,
            symptom_verified=True,  # Gate 2 fires after symptom_verified
        )
        result = engine_owned_affordances(case)
        assert result is not None
        gate, affordances = result
        assert gate == "gate2"
        intent_types = {s["intent"]["type"] for s in affordances}
        assert intent_types == {"path_selection"}

    def test_gate3_pending_returns_post_mitigation_pair(self):
        case = _investigating_case(
            path_selection=_mitigation_first_path(
                mitigation_completed_at_turn=5,
                rca_after_mitigation_confirmed=False,
            ),
        )
        result = engine_owned_affordances(case)
        assert result is not None
        gate, affordances = result
        assert gate == "gate3"
        intent_types = {s["intent"]["type"] for s in affordances}
        assert "post_mitigation_choice" in intent_types

    def test_override_suggestions_takes_priority(self):
        """Imperative override (set by propose_transition during turn processing)
        wins over case-state-derived gates. Maps to the ``disposition`` gate
        identifier for telemetry.
        """
        case = _inquiry_case(problem_statement_confirmed=False)
        custom = [
            {
                "label": "Custom override",
                "action_type": "COOPERATIVE",
                "payload": "override",
                "intent": {"type": "confirmation", "confirmation_value": True},
            }
        ]
        result = engine_owned_affordances(case, {"override_suggestions": custom})
        assert result is not None
        gate, affordances = result
        assert gate == "disposition"
        assert affordances == custom

    def test_rca_infeasible_suppresses_gate3(self):
        """When `rca_infeasible_closure_message` is set, the engine already
        produced override suggestions earlier in the turn — Gate 3 must not
        double-emit. Matches the existing behavior on the LLM-driven shortcut.
        """
        case = _investigating_case(
            path_selection=_mitigation_first_path(
                mitigation_completed_at_turn=5,
                rca_after_mitigation_confirmed=False,
            ),
        )
        result = engine_owned_affordances(
            case, {"rca_infeasible_closure_message": "Some message"}
        )
        # Without override_suggestions to fall back on, the consolidator
        # passes through to the next predicate (which won't match here).
        assert result is None

    def test_gate2_takes_priority_over_gate1_after_gate1_passes(self):
        """Gate 1 is closed (case has transitioned to INVESTIGATING)
        and Gate 2 is now pending after symptom_verified: only Gate 2's
        pair fires."""
        case = _investigating_case(
            path_selection=None,
            symptom_verified=True,
        )
        result = engine_owned_affordances(case)
        assert result is not None
        gate, affordances = result
        assert gate == "gate2"
        intent_types = {s["intent"]["type"] for s in affordances}
        assert intent_types == {"path_selection"}
        # Sanity: gate1 predicate itself reports False — case is in
        # INVESTIGATING, not INQUIRY.
        assert _gate1_is_pending(case) is False


class TestLLMContract:
    """Pin the LLM-facing schema contract. Intent routing is engine-owned;
    the LLM must never see an ``intent`` field on its suggestion schema.

    Removed in step 3 of the 2026-05-20 intent-on-suggestions redesign.
    If the field is reintroduced, the LLM would be invited to populate it
    (provider-variable compliance) and the engine's deterministic intent
    attachment would become a redundant second source of truth — exactly
    the failure shape the redesign exists to close.
    """

    def test_suggested_follow_up_has_no_intent_field(self):
        from faultmaven.core.investigation.schemas import SuggestedFollowUp

        assert "intent" not in SuggestedFollowUp.model_fields, (
            "SuggestedFollowUp.intent has been reintroduced. The LLM-facing "
            "schema must not expose `intent` — intent is engine-owned and "
            "attached at the response-builder layer onto "
            "SuggestedActionResponse. See engine_owned_affordances() in "
            "milestone_engine.py."
        )

    def test_suggested_follow_up_schema_does_not_mention_intent(self):
        """Belt-and-suspenders: even if someone adds a model_config alias
        or a computed field, the JSON schema embedded in the LLM prompt
        must not mention 'intent'. This is what the LLM literally sees;
        absent from the schema → cannot be emitted (and any attempt is
        caught by `_log_dropped_fields`).
        """
        import json

        from faultmaven.core.investigation.schemas import SuggestedFollowUp

        schema_text = json.dumps(SuggestedFollowUp.model_json_schema())
        assert '"intent"' not in schema_text, (
            "The JSON schema the LLM sees for SuggestedFollowUp contains "
            "'intent'. The LLM contract must not advertise this field; "
            "intent is engine-owned."
        )
