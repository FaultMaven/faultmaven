"""Tests for `engine_owned_affordances`: the consolidated gate-affordance predicate.

`engine_owned_affordances(case, metadata)` is the single source of truth for
"when is a state-machine gate pending, and what is the canonical clickable
affordance pair?" It replaces the previously-scattered handshake-deferred /
override_suggestions branches in the response builder with one pure function.

The architectural commitment these tests pin: **intent ↔ gate.** When a gate
is pending, the engine owns the affordance pair and attaches intent metadata.
When no gate is pending, the function returns None and the LLM's exploratory
COOPERATIVE suggestions pass through unmodified.

Post-redesign (unified opportunistic flow, R5): Gate 2 (path selection) and
Gate 3 (post-mitigation choice) were removed along with the path fork. Only
Gate 1 (problem-statement confirmation) and the disposition override remain.
"""

from __future__ import annotations

from faultmaven.core.investigation.milestone_engine import (
    _gate1_is_pending,
    engine_owned_affordances,
)
from faultmaven.modules.case.domain.models import (
    Case,
    CaseState,
    InquiryData,
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
) -> Case:
    """INQUIRY-stage case with controllable Gate 1 inputs."""
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
    return case


def _investigating_case(*, symptom_verified: bool = False) -> Case:
    """INVESTIGATING-stage case in the unified opportunistic flow (no path
    fork)."""
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
    return case


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


# ---------------------------------------------------------------------------
# Affordance consolidator
# ---------------------------------------------------------------------------


class TestEngineOwnedAffordances:
    """The consolidator returns ``(gate_name, affordance_pair)`` when a gate
    is pending, and ``None`` otherwise. The gate identifiers are
    telemetry-stable labels (gate1 / disposition).
    """

    def test_returns_none_when_no_gate_pending(self):
        # INVESTIGATING with no pending gate: no engine-owned affordance.
        case = _investigating_case(symptom_verified=True)
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

    def test_no_gate_in_investigating_even_after_symptom_verified(self):
        """Post-redesign there is no Gate 2 after symptom verification —
        the engine does not fork on a path choice. With no pending
        transition, an INVESTIGATING case has no engine-owned affordance."""
        case = _investigating_case(symptom_verified=True)
        assert engine_owned_affordances(case) is None
        # Gate 1 is closed (case is INVESTIGATING, not INQUIRY).
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
