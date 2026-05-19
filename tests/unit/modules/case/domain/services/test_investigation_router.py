"""Tests for the investigation-path router.

Covers every cell of the Urgency × Temporal matrix plus the missing-signal
default. The router never returns USER_CHOICE — ambiguous cases default to
ROOT_CAUSE with auto_selected=False and an honest rationale; the user
overrides via Gate 2 if they have out-of-band context. See
docs/working/WIP-investigation-gates-implementation.md (slice 1).
"""

import pytest

from faultmaven.modules.case.contracts import (
    InvestigationPath,
    PathSelection,
    ProblemVerification,
    TemporalState,
    UrgencyLevel,
)
from faultmaven.modules.case.domain.services.investigation_router import (
    determine_investigation_path,
)


def _verification(temporal, urgency, severity="MEDIUM"):
    return ProblemVerification(
        symptom_statement="Test symptom",
        severity=severity,
        temporal_state=temporal,
        urgency_level=urgency,
    )


class TestRouterMatrix:
    """Exhaustive coverage of the Urgency × Temporal routing matrix."""

    @pytest.mark.parametrize("urgency", [UrgencyLevel.CRITICAL, UrgencyLevel.HIGH])
    def test_ongoing_high_urgency_routes_to_mitigation_first(self, urgency):
        sel = determine_investigation_path(
            _verification(TemporalState.ONGOING, urgency)
        )
        assert sel.path == InvestigationPath.MITIGATION_FIRST
        assert sel.auto_selected is True
        assert sel.alternate_path == InvestigationPath.ROOT_CAUSE
        assert "mitigat" in sel.rationale.lower()

    @pytest.mark.parametrize("urgency", [UrgencyLevel.LOW, UrgencyLevel.MEDIUM])
    def test_ongoing_low_urgency_routes_to_root_cause(self, urgency):
        """Previously USER_CHOICE; now defaults to ROOT_CAUSE (auto)."""
        sel = determine_investigation_path(
            _verification(TemporalState.ONGOING, urgency)
        )
        assert sel.path == InvestigationPath.ROOT_CAUSE
        assert sel.auto_selected is True
        assert sel.alternate_path == InvestigationPath.MITIGATION_FIRST
        assert "root-cause" in sel.rationale.lower()

    @pytest.mark.parametrize(
        "urgency",
        [
            UrgencyLevel.CRITICAL,
            UrgencyLevel.HIGH,
            UrgencyLevel.MEDIUM,
            UrgencyLevel.LOW,
        ],
    )
    def test_historical_routes_to_root_cause(self, urgency):
        """Historical at any urgency — immediate impact subsided, focus on permanent fix."""
        sel = determine_investigation_path(
            _verification(TemporalState.HISTORICAL, urgency)
        )
        assert sel.path == InvestigationPath.ROOT_CAUSE
        assert sel.auto_selected is True
        assert sel.alternate_path == InvestigationPath.MITIGATION_FIRST
        assert "historical" in sel.rationale.lower()


class TestRouterAmbiguousFallback:
    """Missing or unknown signals default to ROOT_CAUSE with auto_selected=False."""

    def test_unknown_urgency_defaults_to_root_cause_not_auto_selected(self):
        sel = determine_investigation_path(
            _verification(TemporalState.ONGOING, UrgencyLevel.UNKNOWN)
        )
        assert sel.path == InvestigationPath.ROOT_CAUSE
        assert sel.auto_selected is False
        assert sel.alternate_path == InvestigationPath.MITIGATION_FIRST
        assert "default" in sel.rationale.lower()

    def test_missing_temporal_defaults_to_root_cause_not_auto_selected(self):
        sel = determine_investigation_path(_verification(None, UrgencyLevel.CRITICAL))
        assert sel.path == InvestigationPath.ROOT_CAUSE
        assert sel.auto_selected is False
        assert sel.alternate_path == InvestigationPath.MITIGATION_FIRST

    def test_router_never_returns_user_choice(self):
        """Regression guard: every matrix cell + the missing-signal fallback."""
        for temporal in [None, TemporalState.ONGOING, TemporalState.HISTORICAL]:
            for urgency in [
                UrgencyLevel.UNKNOWN,
                UrgencyLevel.LOW,
                UrgencyLevel.MEDIUM,
                UrgencyLevel.HIGH,
                UrgencyLevel.CRITICAL,
            ]:
                sel = determine_investigation_path(_verification(temporal, urgency))
                assert (
                    sel.path != InvestigationPath.USER_CHOICE
                ), f"Router returned USER_CHOICE for temporal={temporal}, urgency={urgency}"


class TestPathSelectionGateDefaults:
    """The router populates the recommendation; Gate 2 confirmation is left for the user.

    All PathSelection instances the router emits must have user_confirmed=False
    so the engine can detect 'recommendation pending confirmation' state.
    """

    def test_router_output_is_not_yet_user_confirmed(self):
        """Every router-emitted PathSelection should default to user_confirmed=False."""
        sel = determine_investigation_path(
            _verification(TemporalState.ONGOING, UrgencyLevel.CRITICAL)
        )
        assert sel.user_confirmed is False
        assert sel.user_confirmed_at_turn is None

    def test_gate3_fields_default_to_unconfirmed(self):
        """Gate 3 fields default to None/False; only set after mitigation_verified."""
        sel = determine_investigation_path(
            _verification(TemporalState.ONGOING, UrgencyLevel.HIGH)
        )
        assert sel.rca_after_mitigation_confirmed is False
        assert sel.rca_after_mitigation_confirmed_at_turn is None
        assert sel.mitigation_completed_at_turn is None

    def test_path_selection_can_be_constructed_with_gate_fields(self):
        """Direct construction (used in tests + future slice 2 wiring)."""
        ps = PathSelection(
            path=InvestigationPath.MITIGATION_FIRST,
            auto_selected=True,
            rationale="test",
            user_confirmed=True,
            user_confirmed_at_turn=5,
            rca_after_mitigation_confirmed=True,
            rca_after_mitigation_confirmed_at_turn=12,
            mitigation_completed_at_turn=10,
        )
        assert ps.user_confirmed is True
        assert ps.user_confirmed_at_turn == 5
        assert ps.rca_after_mitigation_confirmed is True
        assert ps.mitigation_completed_at_turn == 10
