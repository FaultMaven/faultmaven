"""
Comprehensive Stall Detection Service Tests

Tests the investigation stall detection system that prevents infinite loops
and provides graceful termination with 4 stall conditions:

1. Multiple critical evidence blocked (>=3 BLOCKED requests)
2. All hypotheses refuted (Phase 4 - VALIDATION)
3. No phase progress for extended period (>=5 turns in same phase)
4. Unable to formulate hypotheses (Phase 3 - HYPOTHESIS, 0 hypotheses after 3 turns)

Coverage Areas:
- All 4 stall condition detection
- Boundary testing (exact thresholds)
- Valid progression (no false positives)
- Invalid phase number validation
- Edge cases and false positive prevention
- Stall reason string validation
- Counter increment logic
- Escalation vs abandonment logic
- User-facing message generation

Design Reference: docs/architecture/EVIDENCE_CENTRIC_TROUBLESHOOTING_DESIGN.md
"""

import pytest
from typing import List, Dict, Any
from datetime import datetime

from faultmaven.services.evidence.stall_detection import (
    check_for_stall,
    increment_stall_counters,
    should_escalate,
    generate_stall_message,
    _phase_name
)
from faultmaven.models.case import CaseDiagnosticState, UrgencyLevel
from faultmaven.models.evidence import (
    EvidenceRequest,
    EvidenceProvided,
    EvidenceStatus,
    EvidenceCategory,
    EvidenceForm,
    AcquisitionGuidance
)


# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture
def base_state() -> CaseDiagnosticState:
    """Base diagnostic state with minimal configuration"""
    return CaseDiagnosticState(
        has_active_problem=True,
        problem_statement="Test problem",
        current_phase=1,
        turns_without_phase_advance=0,
        turns_in_current_phase=1,
        hypotheses=[],
        evidence_requests=[],
        evidence_provided=[]
    )


@pytest.fixture
def blocked_evidence_requests() -> List[EvidenceRequest]:
    """Critical evidence requests in BLOCKED status"""
    return [
        EvidenceRequest(
            request_id="req-001",
            label="Error logs",
            description="Application error logs from the last 24 hours",
            category=EvidenceCategory.SYMPTOMS,
            guidance=AcquisitionGuidance(
                commands=["kubectl logs -l app=api --since=24h"],
                expected_output="Recent error messages"
            ),
            status=EvidenceStatus.BLOCKED,
            created_at_turn=1,
            completeness=0.0
        ),
        EvidenceRequest(
            request_id="req-002",
            label="Configuration settings",
            description="Current application configuration",
            category=EvidenceCategory.CONFIGURATION,
            guidance=AcquisitionGuidance(
                file_locations=["/etc/app/config.yaml"],
                expected_output="Application configuration"
            ),
            status=EvidenceStatus.BLOCKED,
            created_at_turn=1,
            completeness=0.0
        ),
        EvidenceRequest(
            request_id="req-003",
            label="CPU and memory metrics",
            description="Resource utilization metrics",
            category=EvidenceCategory.METRICS,
            guidance=AcquisitionGuidance(
                commands=["kubectl top pods"],
                expected_output="Resource usage statistics"
            ),
            status=EvidenceStatus.BLOCKED,
            created_at_turn=1,
            completeness=0.0
        )
    ]


@pytest.fixture
def refuted_hypotheses() -> List[Dict[str, Any]]:
    """Multiple hypotheses all marked as refuted"""
    return [
        {
            "hypothesis_id": "hyp-001",
            "description": "Network connectivity issue",
            "status": "refuted",
            "evidence": ["Network tests passed"]
        },
        {
            "hypothesis_id": "hyp-002",
            "description": "Memory leak in application",
            "status": "refuted",
            "evidence": ["Memory usage is stable"]
        },
        {
            "hypothesis_id": "hyp-003",
            "description": "Database connection pool exhaustion",
            "status": "refuted",
            "evidence": ["Connection pool has available slots"]
        }
    ]


@pytest.fixture
def mixed_hypotheses() -> List[Dict[str, Any]]:
    """Mix of confirmed and refuted hypotheses"""
    return [
        {
            "hypothesis_id": "hyp-001",
            "description": "Network connectivity issue",
            "status": "refuted",
            "evidence": ["Network tests passed"]
        },
        {
            "hypothesis_id": "hyp-002",
            "description": "Memory leak in application",
            "status": "confirmed",
            "evidence": ["Memory usage increasing over time"]
        }
    ]


@pytest.fixture
def evidence_provided_recent() -> List[EvidenceProvided]:
    """Recent evidence from user (last 3 turns)"""
    from faultmaven.models.evidence import CompletenessLevel, EvidenceType, UserIntent

    return [
        EvidenceProvided(
            evidence_id="ev-001",
            form=EvidenceForm.USER_INPUT,
            content="Error logs showing database timeout",
            turn_number=8,
            addresses_requests=["req-001"],
            completeness=CompletenessLevel.COMPLETE,
            evidence_type=EvidenceType.SUPPORTIVE,
            user_intent=UserIntent.PROVIDING_EVIDENCE
        ),
        EvidenceProvided(
            evidence_id="ev-002",
            form=EvidenceForm.USER_INPUT,
            content="CPU at 45%, Memory at 60%",
            turn_number=9,
            addresses_requests=["req-002"],
            completeness=CompletenessLevel.COMPLETE,
            evidence_type=EvidenceType.SUPPORTIVE,
            user_intent=UserIntent.PROVIDING_EVIDENCE
        )
    ]


# =============================================================================
# Stall Condition 1: Multiple Critical Evidence Blocked
# =============================================================================


def test_stall_multiple_critical_evidence_blocked(base_state, blocked_evidence_requests):
    """Test stall detection with >=3 BLOCKED critical evidence requests"""
    state = base_state.model_copy(update={
        "current_phase": 1,
        "evidence_requests": blocked_evidence_requests
    })

    reason = check_for_stall(state)

    assert reason is not None, "Should detect stall with 3 blocked critical evidence"
    assert "blocked" in reason.lower(), "Reason should mention blocked evidence"
    assert "Error logs" in reason or "Configuration settings" in reason or "CPU and memory metrics" in reason, \
        "Reason should include specific blocked item labels"


def test_stall_exactly_3_blocked_requests(base_state, blocked_evidence_requests):
    """Test boundary condition: exactly 3 blocked requests triggers stall"""
    state = base_state.model_copy(update={
        "evidence_requests": blocked_evidence_requests[:3]  # Exactly 3
    })

    reason = check_for_stall(state)

    assert reason is not None, "Exactly 3 blocked requests should trigger stall"


def test_no_stall_2_blocked_requests(base_state):
    """Test boundary condition: 2 blocked requests should NOT trigger stall"""
    blocked_requests = [
        EvidenceRequest(
            request_id="req-001",
            label="Error logs",
            description="Application error logs",
            category=EvidenceCategory.SYMPTOMS,
            guidance=AcquisitionGuidance(),
            status=EvidenceStatus.BLOCKED,
            created_at_turn=1,
            completeness=0.0
        ),
        EvidenceRequest(
            request_id="req-002",
            label="Config",
            description="Configuration",
            category=EvidenceCategory.CONFIGURATION,
            guidance=AcquisitionGuidance(),
            status=EvidenceStatus.BLOCKED,
            created_at_turn=1,
            completeness=0.0
        )
    ]

    state = base_state.model_copy(update={
        "evidence_requests": blocked_requests
    })

    reason = check_for_stall(state)

    assert reason is None, "2 blocked requests should NOT trigger stall (threshold is 3)"


def test_no_stall_non_critical_blocked(base_state):
    """Test that non-critical evidence categories don't trigger stall"""
    non_critical_blocked = [
        EvidenceRequest(
            request_id="req-001",
            label="Timeline info",
            description="When did this start",
            category=EvidenceCategory.TIMELINE,  # Not critical
            guidance=AcquisitionGuidance(),
            status=EvidenceStatus.BLOCKED,
            created_at_turn=1,
            completeness=0.0
        ),
        EvidenceRequest(
            request_id="req-002",
            label="Changes",
            description="Recent changes",
            category=EvidenceCategory.CHANGES,  # Not critical
            guidance=AcquisitionGuidance(),
            status=EvidenceStatus.BLOCKED,
            created_at_turn=1,
            completeness=0.0
        ),
        EvidenceRequest(
            request_id="req-003",
            label="Scope",
            description="Impact scope",
            category=EvidenceCategory.SCOPE,  # Not critical
            guidance=AcquisitionGuidance(),
            status=EvidenceStatus.BLOCKED,
            created_at_turn=1,
            completeness=0.0
        )
    ]

    state = base_state.model_copy(update={
        "evidence_requests": non_critical_blocked
    })

    reason = check_for_stall(state)

    assert reason is None, "Non-critical evidence should NOT trigger stall"


# =============================================================================
# Stall Condition 2: All Hypotheses Refuted (Phase 4)
# =============================================================================


def test_stall_all_hypotheses_refuted_phase4(base_state, refuted_hypotheses):
    """Test stall detection when all hypotheses are refuted in Phase 4"""
    state = base_state.model_copy(update={
        "current_phase": 4,  # Validation phase
        "hypotheses": refuted_hypotheses
    })

    reason = check_for_stall(state)

    assert reason is not None, "Should detect stall when all hypotheses refuted"
    assert "refuted" in reason.lower(), "Reason should mention refuted hypotheses"
    assert "3" in reason or "all" in reason.lower(), "Reason should mention number of hypotheses"


def test_no_stall_some_hypotheses_not_refuted_phase4(base_state, mixed_hypotheses):
    """Test no stall when some hypotheses are still valid in Phase 4"""
    state = base_state.model_copy(update={
        "current_phase": 4,
        "hypotheses": mixed_hypotheses
    })

    reason = check_for_stall(state)

    assert reason is None, "Should NOT stall when some hypotheses are confirmed"


def test_no_stall_refuted_hypotheses_wrong_phase(base_state, refuted_hypotheses):
    """Test no stall for refuted hypotheses outside Phase 4"""
    state = base_state.model_copy(update={
        "current_phase": 3,  # Hypothesis formulation, not validation
        "hypotheses": refuted_hypotheses
    })

    reason = check_for_stall(state)

    assert reason is None, "Refuted hypotheses only trigger stall in Phase 4"


def test_no_stall_phase4_less_than_3_hypotheses_refuted(base_state):
    """Test no stall when fewer than 3 hypotheses in Phase 4"""
    few_hypotheses = [
        {"hypothesis_id": "hyp-001", "status": "refuted"},
        {"hypothesis_id": "hyp-002", "status": "refuted"}
    ]

    state = base_state.model_copy(update={
        "current_phase": 4,
        "hypotheses": few_hypotheses
    })

    reason = check_for_stall(state)

    assert reason is None, "Need at least 3 hypotheses to trigger refutation stall"


# =============================================================================
# Stall Condition 3: No Phase Progress (>=5 turns)
# =============================================================================


def test_stall_no_phase_progress_5_turns(base_state):
    """Test stall detection after 5 turns without phase advancement"""
    state = base_state.model_copy(update={
        "current_phase": 2,
        "turns_without_phase_advance": 5
    })

    reason = check_for_stall(state)

    assert reason is not None, "Should detect stall after 5 turns without progress"
    assert "5 turns" in reason or "progress" in reason.lower(), "Reason should mention turns"


def test_stall_no_phase_progress_more_than_5_turns(base_state):
    """Test stall detection with more than 5 turns stuck"""
    state = base_state.model_copy(update={
        "current_phase": 2,
        "turns_without_phase_advance": 8
    })

    reason = check_for_stall(state)

    assert reason is not None, "Should detect stall after 8 turns"
    assert "8 turns" in reason, "Reason should mention actual turn count"


def test_no_stall_4_turns_same_phase(base_state):
    """Test boundary condition: 4 turns should NOT trigger stall"""
    state = base_state.model_copy(update={
        "current_phase": 2,
        "turns_without_phase_advance": 4
    })

    reason = check_for_stall(state)

    assert reason is None, "4 turns should NOT trigger stall (threshold is 5)"


def test_stall_exactly_5_turns_same_phase(base_state):
    """Test boundary condition: exactly 5 turns should trigger stall"""
    state = base_state.model_copy(update={
        "current_phase": 2,
        "turns_without_phase_advance": 5
    })

    reason = check_for_stall(state)

    assert reason is not None, "Exactly 5 turns should trigger stall"


# =============================================================================
# Stall Condition 4: No Hypotheses in Phase 3
# =============================================================================


def test_stall_no_hypotheses_after_3_turns_phase3(base_state):
    """Test stall detection in Phase 3 with no hypotheses after 3 turns"""
    state = base_state.model_copy(update={
        "current_phase": 3,  # Hypothesis formulation
        "turns_in_current_phase": 3,
        "hypotheses": []
    })

    reason = check_for_stall(state)

    assert reason is not None, "Should detect stall with 0 hypotheses after 3 turns in Phase 3"
    assert "hypotheses" in reason.lower() or "formulate" in reason.lower(), \
        "Reason should mention inability to formulate hypotheses"
    assert "3 turns" in reason, "Reason should mention turn count"


def test_stall_no_hypotheses_after_more_than_3_turns_phase3(base_state):
    """Test stall with no hypotheses after more than 3 turns in Phase 3"""
    state = base_state.model_copy(update={
        "current_phase": 3,
        "turns_in_current_phase": 5,
        "hypotheses": []
    })

    reason = check_for_stall(state)

    assert reason is not None, "Should detect stall after 5 turns with no hypotheses"


def test_no_stall_hypotheses_exist_phase3(base_state):
    """Test no stall when hypotheses exist in Phase 3"""
    state = base_state.model_copy(update={
        "current_phase": 3,
        "turns_in_current_phase": 3,
        "hypotheses": [{"hypothesis_id": "hyp-001", "description": "Network issue"}]
    })

    reason = check_for_stall(state)

    assert reason is None, "Should NOT stall when hypotheses exist"


def test_no_stall_phase3_less_than_3_turns(base_state):
    """Test no stall in Phase 3 before 3 turns elapsed"""
    state = base_state.model_copy(update={
        "current_phase": 3,
        "turns_in_current_phase": 2,
        "hypotheses": []
    })

    reason = check_for_stall(state)

    assert reason is None, "Should NOT stall before 3 turns in Phase 3"


def test_no_stall_no_hypotheses_wrong_phase(base_state):
    """Test no stall for missing hypotheses outside Phase 3"""
    state = base_state.model_copy(update={
        "current_phase": 2,  # Timeline phase
        "turns_in_current_phase": 5,
        "hypotheses": []
    })

    reason = check_for_stall(state)

    assert reason is None, "Missing hypotheses only trigger stall in Phase 3"


# =============================================================================
# Valid Progression (No False Positives)
# =============================================================================


def test_no_stall_valid_progression(base_state):
    """Test that normal investigation progression doesn't trigger false stalls"""
    state = base_state.model_copy(update={
        "current_phase": 2,
        "turns_without_phase_advance": 2,
        "turns_in_current_phase": 2,
        "hypotheses": [],
        "evidence_requests": [
            EvidenceRequest(
                request_id="req-001",
                label="Test request",
                description="Test",
                category=EvidenceCategory.SYMPTOMS,
                guidance=AcquisitionGuidance(),
                status=EvidenceStatus.PENDING,  # Not blocked
                created_at_turn=1,
                completeness=0.0
            )
        ]
    })

    reason = check_for_stall(state)

    assert reason is None, "Valid progression should NOT trigger stall"


def test_no_false_positive_edge_cases(base_state):
    """Test edge cases that should NOT trigger false stalls"""
    # Edge case 1: Phase 4 with no hypotheses (different from Phase 3)
    state = base_state.model_copy(update={
        "current_phase": 4,
        "hypotheses": []
    })
    assert check_for_stall(state) is None, "No hypotheses in Phase 4 should not stall"

    # Edge case 2: Phase 0 (Intake) with long duration
    state = base_state.model_copy(update={
        "current_phase": 0,
        "turns_without_phase_advance": 3
    })
    assert check_for_stall(state) is None, "Phase 0 should not stall early"

    # Edge case 3: Mix of blocked and non-blocked (only 2 critical blocked)
    state = base_state.model_copy(update={
        "evidence_requests": [
            EvidenceRequest(
                request_id="req-001",
                label="Critical 1",
                description="Test",
                category=EvidenceCategory.SYMPTOMS,
                guidance=AcquisitionGuidance(),
                status=EvidenceStatus.BLOCKED,
                created_at_turn=1,
                completeness=0.0
            ),
            EvidenceRequest(
                request_id="req-002",
                label="Critical 2",
                description="Test",
                category=EvidenceCategory.CONFIGURATION,
                guidance=AcquisitionGuidance(),
                status=EvidenceStatus.BLOCKED,
                created_at_turn=1,
                completeness=0.0
            ),
            EvidenceRequest(
                request_id="req-003",
                label="Non-critical",
                description="Test",
                category=EvidenceCategory.TIMELINE,
                guidance=AcquisitionGuidance(),
                status=EvidenceStatus.BLOCKED,
                created_at_turn=1,
                completeness=0.0
            )
        ]
    })
    assert check_for_stall(state) is None, "Only 2 critical blocked should not stall"


# =============================================================================
# Phase Number Validation
# =============================================================================


def test_stall_invalid_phase_number_negative(base_state):
    """Test that invalid phase number (negative) raises ValueError"""
    state = base_state.model_copy(update={
        "current_phase": -1
    })

    with pytest.raises(ValueError, match="Invalid phase number.*Must be 0-5"):
        check_for_stall(state)


def test_stall_invalid_phase_number_too_high(base_state):
    """Test that invalid phase number (>5) raises ValueError"""
    state = base_state.model_copy(update={
        "current_phase": 6
    })

    with pytest.raises(ValueError, match="Invalid phase number.*Must be 0-5"):
        check_for_stall(state)


def test_stall_valid_phase_numbers(base_state):
    """Test that all valid phase numbers (0-5) are accepted"""
    for phase in range(6):
        state = base_state.model_copy(update={
            "current_phase": phase
        })
        # Should not raise exception
        try:
            check_for_stall(state)
        except ValueError:
            pytest.fail(f"Phase {phase} should be valid")


# =============================================================================
# Counter Increment Logic
# =============================================================================


def test_increment_stall_counters_phase_advanced(base_state):
    """Test counter reset when phase advances"""
    state = base_state.model_copy(update={
        "current_phase": 2,
        "turns_without_phase_advance": 3,
        "turns_in_current_phase": 3
    })

    increment_stall_counters(state, phase_advanced=True)

    assert state.turns_without_phase_advance == 0, "Should reset no-progress counter"
    assert state.turns_in_current_phase == 1, "Should reset to 1 for new phase"


def test_increment_stall_counters_no_phase_advance(base_state):
    """Test counter increment when phase doesn't advance"""
    state = base_state.model_copy(update={
        "turns_without_phase_advance": 2,
        "turns_in_current_phase": 2
    })

    increment_stall_counters(state, phase_advanced=False)

    assert state.turns_without_phase_advance == 3, "Should increment no-progress counter"
    assert state.turns_in_current_phase == 3, "Should increment phase-specific counter"


def test_increment_stall_counters_from_zero(base_state):
    """Test counter increment from initial state"""
    state = base_state.model_copy(update={
        "turns_without_phase_advance": 0,
        "turns_in_current_phase": 0
    })

    increment_stall_counters(state, phase_advanced=False)

    assert state.turns_without_phase_advance == 1
    assert state.turns_in_current_phase == 1


# =============================================================================
# Escalation vs Abandonment Logic
# =============================================================================


def test_should_escalate_blocked_evidence(base_state):
    """Test escalation recommended for blocked evidence"""
    stall_reason = "Multiple critical evidence sources blocked"

    result = should_escalate(base_state, stall_reason)

    assert result is True, "Blocked evidence should trigger escalation"


def test_should_escalate_refuted_hypotheses(base_state):
    """Test escalation recommended for refuted hypotheses"""
    stall_reason = "All formulated hypotheses have been refuted by evidence"

    result = should_escalate(base_state, stall_reason)

    assert result is True, "Refuted hypotheses should trigger escalation"


def test_should_escalate_no_progress_with_evidence(base_state, evidence_provided_recent):
    """Test escalation when user is engaged (providing evidence)"""
    state = base_state.model_copy(update={
        "current_phase": 5,
        "evidence_provided": evidence_provided_recent
    })
    stall_reason = "No investigation progress after 5 turns"

    result = should_escalate(state, stall_reason)

    assert result is True, "Active user should trigger escalation"


def test_should_not_escalate_no_progress_no_evidence(base_state):
    """Test abandonment when user not providing evidence"""
    state = base_state.model_copy(update={
        "current_phase": 5,
        "evidence_provided": []
    })
    stall_reason = "No investigation progress after 5 turns"

    result = should_escalate(state, stall_reason)

    assert result is False, "Inactive user should trigger abandonment"


def test_default_escalation(base_state):
    """Test default escalation for unknown reasons"""
    stall_reason = "Unknown stall reason"

    result = should_escalate(base_state, stall_reason)

    assert result is True, "Default should be escalation (optimistic)"


# =============================================================================
# User-Facing Message Generation
# =============================================================================


def test_generate_stall_message_escalate(base_state):
    """Test escalation message format"""
    stall_reason = "Multiple critical evidence sources blocked"

    message = generate_stall_message(stall_reason, escalate=True, state=base_state)

    assert "Investigation Stalled" in message or "stalled" in message.lower()
    assert stall_reason in message, "Should include stall reason"
    assert "Escalate" in message or "escalate" in message.lower()
    assert "escalation report" in message.lower()


def test_generate_stall_message_abandon(base_state):
    """Test abandonment message format"""
    stall_reason = "No investigation progress after 5 turns"

    message = generate_stall_message(stall_reason, escalate=False, state=base_state)

    assert "Investigation Incomplete" in message or "incomplete" in message.lower()
    assert stall_reason in message
    assert "unable to proceed" in message.lower() or "unresolved" in message.lower()


def test_generate_stall_message_includes_context(base_state):
    """Test that messages include relevant case context"""
    from faultmaven.models.evidence import CompletenessLevel, EvidenceType, UserIntent

    state = base_state.model_copy(update={
        "problem_statement": "Database timeout errors",
        "current_phase": 3,
        "evidence_provided": [
            EvidenceProvided(
                evidence_id="ev-001",
                form=EvidenceForm.USER_INPUT,
                content="Test evidence",
                turn_number=1,
                addresses_requests=[],
                completeness=CompletenessLevel.COMPLETE,
                evidence_type=EvidenceType.SUPPORTIVE,
                user_intent=UserIntent.PROVIDING_EVIDENCE
            )
        ],
        "hypotheses": [{"hypothesis_id": "hyp-001", "description": "Network issue"}]
    })

    message = generate_stall_message("Test stall reason", True, state)

    assert "Database timeout errors" in message or "problem" in message.lower()
    assert "1" in message, "Should show evidence count"
    assert "Hypothesis" in message or "hypothesis" in message.lower()


# =============================================================================
# Phase Name Utility
# =============================================================================


def test_phase_name_all_valid_phases():
    """Test phase name mapping for all valid phases"""
    expected_names = {
        0: "Intake (Problem Identification)",
        1: "Blast Radius (Impact Assessment)",
        2: "Timeline (Change Analysis)",
        3: "Hypothesis (Root Cause Theories)",
        4: "Validation (Testing & Verification)",
        5: "Solution (Resolution & Prevention)"
    }

    for phase, expected in expected_names.items():
        assert _phase_name(phase) == expected


def test_phase_name_invalid_phase():
    """Test phase name for invalid phase number"""
    result = _phase_name(99)
    assert "Unknown Phase 99" in result


# =============================================================================
# Complex Stall Scenarios
# =============================================================================


def test_multiple_stall_conditions_first_wins(base_state, blocked_evidence_requests):
    """Test that first stall condition wins when multiple conditions met"""
    state = base_state.model_copy(update={
        "current_phase": 4,
        "evidence_requests": blocked_evidence_requests,  # Condition 1
        "hypotheses": [
            {"hypothesis_id": "h1", "status": "refuted"},
            {"hypothesis_id": "h2", "status": "refuted"},
            {"hypothesis_id": "h3", "status": "refuted"}
        ],  # Condition 2
        "turns_without_phase_advance": 6  # Condition 3
    })

    reason = check_for_stall(state)

    # First check (blocked evidence) should win
    assert "blocked" in reason.lower(), "First stall condition should be reported"


def test_stall_reason_string_format(base_state, blocked_evidence_requests):
    """Test that stall reasons are properly formatted user-facing strings"""
    test_cases = [
        # Blocked evidence
        (
            base_state.model_copy(update={"evidence_requests": blocked_evidence_requests}),
            ["blocked", "critical"]
        ),
        # Refuted hypotheses
        (
            base_state.model_copy(update={
                "current_phase": 4,
                "hypotheses": [
                    {"hypothesis_id": "h1", "status": "refuted"},
                    {"hypothesis_id": "h2", "status": "refuted"},
                    {"hypothesis_id": "h3", "status": "refuted"}
                ]
            }),
            ["refuted", "hypotheses"]
        ),
        # No progress
        (
            base_state.model_copy(update={"turns_without_phase_advance": 5}),
            ["progress", "turns"]
        ),
        # No hypotheses in Phase 3
        (
            base_state.model_copy(update={
                "current_phase": 3,
                "turns_in_current_phase": 3,
                "hypotheses": []
            }),
            ["formulate", "hypotheses"]
        )
    ]

    for state, expected_keywords in test_cases:
        reason = check_for_stall(state)
        assert reason is not None
        for keyword in expected_keywords:
            assert keyword.lower() in reason.lower(), \
                f"Stall reason should contain '{keyword}': {reason}"
