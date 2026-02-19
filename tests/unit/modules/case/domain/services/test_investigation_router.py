import pytest

from faultmaven.modules.case.contracts import (
    InvestigationPath,
    ProblemVerification,
    TemporalState,
    UrgencyLevel,
)
from faultmaven.modules.case.domain.services.investigation_router import (
    determine_investigation_path,
)


class TestInvestigationRouter:
    """Test investigation path routing logic."""

    def test_mitigation_first_routing(self):
        """Test routing to MITIGATION_FIRST for ongoing critical/high issues."""
        # Ongoing + Critical -> Mitigation
        pv = ProblemVerification(
            symptom_statement="Server down",
            severity="CRITICAL",
            temporal_state=TemporalState.ONGOING,
            urgency_level=UrgencyLevel.CRITICAL,
        )
        selection = determine_investigation_path(pv)
        assert selection.path == InvestigationPath.MITIGATION_FIRST
        assert selection.auto_selected is True
        assert "ongoing" in selection.rationale.lower()

        # Ongoing + High -> Mitigation
        pv.urgency_level = UrgencyLevel.HIGH
        pv.severity = "HIGH"
        selection = determine_investigation_path(pv)
        assert selection.path == InvestigationPath.MITIGATION_FIRST
        assert selection.auto_selected is True

    def test_root_cause_routing(self):
        """Test routing to ROOT_CAUSE for historical low/medium issues."""
        # Historical + Low -> Root Cause
        pv = ProblemVerification(
            symptom_statement="Glitch last week",
            severity="LOW",
            temporal_state=TemporalState.HISTORICAL,
            urgency_level=UrgencyLevel.LOW,
        )
        selection = determine_investigation_path(pv)
        assert selection.path == InvestigationPath.ROOT_CAUSE
        assert selection.auto_selected is True
        assert "historical" in selection.rationale.lower()

        # Historical + Medium -> Root Cause
        pv.urgency_level = UrgencyLevel.MEDIUM
        pv.severity = "MEDIUM"
        selection = determine_investigation_path(pv)
        assert selection.path == InvestigationPath.ROOT_CAUSE
        assert selection.auto_selected is True

    def test_user_choice_ambiguous(self):
        """Test fallback to USER_CHOICE for ambiguous combinations."""
        # Ongoing + Low (User might want quick fix OR ignore it)
        pv = ProblemVerification(
            symptom_statement="Minor lag now",
            severity="LOW",
            temporal_state=TemporalState.ONGOING,
            urgency_level=UrgencyLevel.LOW,
        )
        selection = determine_investigation_path(pv)
        assert selection.path == InvestigationPath.USER_CHOICE
        assert selection.auto_selected is False
        assert "ambiguous" in selection.rationale.lower()

        # Historical + Critical → ROOT_CAUSE (post-mortem always benefits from root cause)
        pv = ProblemVerification(
            symptom_statement="Major outage last year",
            severity="CRITICAL",
            temporal_state=TemporalState.HISTORICAL,
            urgency_level=UrgencyLevel.CRITICAL,
        )
        selection = determine_investigation_path(pv)
        assert selection.path == InvestigationPath.ROOT_CAUSE
        assert selection.auto_selected is True

    def test_unknown_urgency(self):
        """Test handling of unknown urgency."""
        pv = ProblemVerification(
            symptom_statement="Unknown issue",
            severity="LOW",
            temporal_state=TemporalState.ONGOING,
            urgency_level=UrgencyLevel.UNKNOWN,
        )
        selection = determine_investigation_path(pv)
        assert selection.path == InvestigationPath.USER_CHOICE
        assert selection.auto_selected is False
