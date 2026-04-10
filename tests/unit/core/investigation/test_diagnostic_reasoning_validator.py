"""Tests for DiagnosticReasoningValidator

Tests diagnostic reasoning validation for agent responses as required by
Section 3.3 of the investigation specification. Covers all 7 validation
helper functions and the main validate_diagnostic_reasoning() entry point.
"""

import pytest

from faultmaven.core.investigation.diagnostic_reasoning_validator import (
    DiagnosticReasoningError,
    _detect_suggestions,
    _has_analysis_section,
    _has_causal_reasoning,
    _has_observation_section,
    _is_checklist_engineering,
    _is_generic_best_practices,
    _lacks_case_specificity,
    _references_specific_evidence,
    validate_diagnostic_reasoning,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseStatus,
    InquiryData,
    InvestigationProgress,
    ProblemVerification,
)

# ============================================================
# Fixtures
# ============================================================


@pytest.fixture
def inquiry_case():
    """Case in INQUIRY status (exempt from diagnostic reasoning validation)."""
    return Case(
        case_id="case_1234567890ab",
        title="API Latency Spike",
        status=CaseStatus.INQUIRY,
        user_id="user_123",
        organization_id="org_123",
        description="",
    )


@pytest.fixture
def investigating_case():
    """Case in INVESTIGATING status with problem verification."""
    return Case(
        case_id="case_abcdef012345",
        title="Database Connection Timeout",
        status=CaseStatus.INVESTIGATING,
        user_id="user_123",
        organization_id="org_123",
        description="Database connection pool exhaustion causing timeouts",
        problem_verification=ProblemVerification(
            symptom_statement="Database connections timing out under load with error rate spike",
            severity="HIGH",
        ),
        inquiry=InquiryData(
            problem_statement_confirmed=True,
            decided_to_investigate=True,
            proposed_problem_statement="Database connection pool exhaustion",
        ),
        progress=InvestigationProgress(),
    )


@pytest.fixture
def investigating_case_no_symptom():
    """Investigating case without problem verification (for specificity tests)."""
    return Case(
        case_id="case_000000000000",
        title="Unknown Issue",
        status=CaseStatus.INVESTIGATING,
        user_id="user_123",
        organization_id="org_123",
        description="Some issue under investigation",
        problem_verification=None,
        inquiry=InquiryData(
            problem_statement_confirmed=True,
            decided_to_investigate=True,
            proposed_problem_statement="Unknown issue",
        ),
        progress=InvestigationProgress(),
    )


# ============================================================
# Tests for _detect_suggestions()
# ============================================================


class TestDetectSuggestions:
    """Test detection of suggestion/mitigation/hypothesis keywords."""

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "keyword",
        [
            "you should",
            "i recommend",
            "try",
            "suggest",
            "hypothesis",
            "mitigation",
            "solution",
            "could you",
            "can you check",
            "you might want to",
            "consider",
        ],
    )
    def test_detects_each_suggestion_keyword(self, keyword):
        """Each suggestion keyword should be detected."""
        response = f"Based on the logs, {keyword} restarting the service."
        assert _detect_suggestions(response) is True

    @pytest.mark.unit
    def test_case_insensitive_detection(self):
        """Detection should be case-insensitive."""
        assert _detect_suggestions("You Should restart the service") is True
        assert _detect_suggestions("I RECOMMEND checking the logs") is True
        assert _detect_suggestions("CONSIDER the deployment timeline") is True

    @pytest.mark.unit
    def test_no_suggestion_keywords(self):
        """Response without suggestion keywords should return False."""
        response = (
            "The error rate increased to 15% at 14:30 UTC. "
            "The deployment of version abc1234 correlates with the spike."
        )
        assert _detect_suggestions(response) is False

    @pytest.mark.unit
    def test_pure_observation_not_detected(self):
        """Purely observational statements should not trigger detection."""
        response = (
            "OBSERVATION: The CPU usage spiked to 95% at 10:30. "
            "ANALYSIS: The memory leak in the worker process explains the degradation."
        )
        assert _detect_suggestions(response) is False

    @pytest.mark.unit
    def test_empty_response(self):
        """Empty response should not contain suggestions."""
        assert _detect_suggestions("") is False


# ============================================================
# Tests for _has_observation_section()
# ============================================================


class TestHasObservationSection:
    """Test detection of OBSERVATION section markers."""

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "marker",
        [
            "OBSERVATION:",
            "OBSERVED:",
            "I NOTICE",
            "EVIDENCE SHOWS",
            "THE DATA SHOWS",
        ],
    )
    def test_detects_each_observation_marker(self, marker):
        """Each observation marker should be detected."""
        response = f"{marker} the error rate is elevated."
        assert _has_observation_section(response) is True

    @pytest.mark.unit
    def test_case_insensitive_matching(self):
        """Observation markers should be matched case-insensitively."""
        assert _has_observation_section("observation: CPU is at 95%") is True
        assert _has_observation_section("i notice a pattern in the logs") is True
        assert _has_observation_section("evidence shows a correlation") is True
        assert _has_observation_section("the data shows an anomaly") is True
        assert _has_observation_section("Observed: memory leak detected") is True

    @pytest.mark.unit
    def test_no_observation_marker(self):
        """Response without observation markers should return False."""
        response = "The service is experiencing high latency due to a memory leak."
        assert _has_observation_section(response) is False

    @pytest.mark.unit
    def test_empty_response(self):
        """Empty response has no observation section."""
        assert _has_observation_section("") is False

    @pytest.mark.unit
    def test_marker_embedded_in_text(self):
        """Marker embedded within normal text should still be detected."""
        response = "After reviewing the metrics, OBSERVATION: the p99 latency spiked."
        assert _has_observation_section(response) is True


# ============================================================
# Tests for _has_analysis_section()
# ============================================================


class TestHasAnalysisSection:
    """Test detection of ANALYSIS section markers."""

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "marker",
        [
            "ANALYSIS:",
            "REASONING:",
            "THIS SUGGESTS",
            "THIS INDICATES",
            "THIS MEANS",
            "BECAUSE",
        ],
    )
    def test_detects_each_analysis_marker(self, marker):
        """Each analysis marker should be detected."""
        response = f"{marker} the root cause is likely a memory leak."
        assert _has_analysis_section(response) is True

    @pytest.mark.unit
    def test_case_insensitive_matching(self):
        """Analysis markers should be matched case-insensitively."""
        assert (
            _has_analysis_section("analysis: the connection pool is exhausted") is True
        )
        assert _has_analysis_section("reasoning: the timeline correlates") is True
        assert _has_analysis_section("this suggests a configuration issue") is True
        assert _has_analysis_section("this indicates resource exhaustion") is True
        assert _has_analysis_section("this means the cache is stale") is True
        assert (
            _has_analysis_section("because the deployment changed the config") is True
        )

    @pytest.mark.unit
    def test_no_analysis_marker(self):
        """Response without analysis markers should return False."""
        response = "The CPU usage is at 95% and memory is at 80%."
        assert _has_analysis_section(response) is False

    @pytest.mark.unit
    def test_empty_response(self):
        """Empty response has no analysis section."""
        assert _has_analysis_section("") is False


# ============================================================
# Tests for _references_specific_evidence()
# ============================================================


class TestReferencesSpecificEvidence:
    """Test detection of specific case evidence references.

    Requires at least 2 of 4 categories: timestamps, metrics, IDs, error details.
    """

    @pytest.mark.unit
    def test_timestamps_only_insufficient(self, investigating_case):
        """A single category (timestamps) is not enough."""
        response = "The issue started at 14:30 and was seen again at 15:00."
        assert _references_specific_evidence(response, investigating_case) is False

    @pytest.mark.unit
    def test_metrics_only_insufficient(self, investigating_case):
        """A single category (metrics) is not enough."""
        response = "The error rate reached 45% with response time of 3.2 seconds."
        assert _references_specific_evidence(response, investigating_case) is False

    @pytest.mark.unit
    def test_ids_only_insufficient(self, investigating_case):
        """A single category (IDs) is not enough."""
        response = "The commit abc1234def was deployed in the latest version."
        assert _references_specific_evidence(response, investigating_case) is False

    @pytest.mark.unit
    def test_error_details_only_insufficient(self, investigating_case):
        """A single category (error details) is not enough."""
        response = "The request failed with a timeout after 30 seconds."
        assert _references_specific_evidence(response, investigating_case) is False

    @pytest.mark.unit
    def test_timestamps_and_metrics_sufficient(self, investigating_case):
        """Two categories (timestamps + metrics) should be sufficient."""
        response = "At 14:30, the error rate spiked to 45%."
        assert _references_specific_evidence(response, investigating_case) is True

    @pytest.mark.unit
    def test_ids_and_error_details_sufficient(self, investigating_case):
        """Two categories (IDs + error details) should be sufficient."""
        response = "After deploying commit abc1234def, the service failed with connection timeout."
        assert _references_specific_evidence(response, investigating_case) is True

    @pytest.mark.unit
    def test_metrics_and_error_details_sufficient(self, investigating_case):
        """Two categories (metrics + error details) should be sufficient."""
        response = (
            "The error rate hit 15% and multiple requests failed with timeout errors."
        )
        assert _references_specific_evidence(response, investigating_case) is True

    @pytest.mark.unit
    def test_timestamps_and_ids_sufficient(self, investigating_case):
        """Two categories (timestamps + IDs) should be sufficient."""
        response = "At 2024-03-08, the deployment of version abc1234def went live."
        assert _references_specific_evidence(response, investigating_case) is True

    @pytest.mark.unit
    def test_all_four_categories_present(self, investigating_case):
        """All four categories should pass."""
        response = (
            "At 14:30, the error rate reached 45%. "
            "Commit abc1234def caused the failure with timeout errors."
        )
        assert _references_specific_evidence(response, investigating_case) is True

    @pytest.mark.unit
    def test_three_categories_present(self, investigating_case):
        """Three categories should pass."""
        response = (
            "At 14:30, the error rate reached 45%. "
            "The service failed with connection timeout."
        )
        assert _references_specific_evidence(response, investigating_case) is True

    @pytest.mark.unit
    def test_timestamp_via_relative_time(self, investigating_case):
        """Relative time phrases like 'minutes ago' should count as timestamps."""
        response = "The issue started 30 minutes ago and the error rate is 15%."
        assert _references_specific_evidence(response, investigating_case) is True

    @pytest.mark.unit
    def test_timestamp_via_utc(self, investigating_case):
        """UTC references should count as timestamps."""
        response = "The spike occurred in UTC and the deployment version changed."
        assert _references_specific_evidence(response, investigating_case) is True

    @pytest.mark.unit
    def test_metrics_via_keyword(self, investigating_case):
        """Keywords like 'error rate' and 'latency' should count as metrics."""
        response = "The error rate increased after the deployment of version abc1234."
        assert _references_specific_evidence(response, investigating_case) is True

    @pytest.mark.unit
    def test_ids_via_deployment_keyword(self, investigating_case):
        """The keyword 'deployment' should count as IDs category."""
        response = "The deployment correlated with an error rate spike of 25%."
        assert _references_specific_evidence(response, investigating_case) is True

    @pytest.mark.unit
    def test_no_evidence_categories(self, investigating_case):
        """Response with no evidence categories should fail."""
        response = "The system seems to be having some kind of performance issue."
        assert _references_specific_evidence(response, investigating_case) is False

    @pytest.mark.unit
    def test_empty_response(self, investigating_case):
        """Empty response has no evidence references."""
        assert _references_specific_evidence("", investigating_case) is False

    @pytest.mark.unit
    def test_decimal_counts_as_metric(self, investigating_case):
        """A decimal number like 3.5 should count as a metric."""
        response = "The latency was 3.5 seconds. The request failed."
        # Metrics (decimal + latency keyword) + error details (failed)
        assert _references_specific_evidence(response, investigating_case) is True

    @pytest.mark.unit
    def test_hours_ago_counts_as_timestamp(self, investigating_case):
        """'hours ago' should count as a timestamp reference."""
        response = "The issue started 2 hours ago and the request failed."
        assert _references_specific_evidence(response, investigating_case) is True


# ============================================================
# Tests for _has_causal_reasoning()
# ============================================================


class TestHasCausalReasoning:
    """Test detection of causal reasoning indicators."""

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "indicator",
        [
            "causes",
            "leads to",
            "results in",
            "because",
            "since",
            "therefore",
            "this explains why",
            "this is why",
            "the reason is",
        ],
    )
    def test_detects_each_causal_indicator(self, indicator):
        """Each causal indicator should be detected."""
        response = f"The memory leak {indicator} the connection pool exhaustion."
        assert _has_causal_reasoning(response) is True

    @pytest.mark.unit
    def test_case_insensitive_detection(self):
        """Causal reasoning detection should be case-insensitive."""
        assert _has_causal_reasoning("BECAUSE the config changed") is True
        assert _has_causal_reasoning("Therefore the service restarted") is True
        assert _has_causal_reasoning("This Explains Why the latency increased") is True

    @pytest.mark.unit
    def test_no_causal_indicators(self):
        """Response without causal indicators should return False."""
        response = "The error rate is 15%. The CPU usage is 95%. The memory is at 80%."
        assert _has_causal_reasoning(response) is False

    @pytest.mark.unit
    def test_empty_response(self):
        """Empty response has no causal reasoning."""
        assert _has_causal_reasoning("") is False

    @pytest.mark.unit
    def test_pure_listing_without_causation(self):
        """A response that lists facts without causal links should fail."""
        response = (
            "CPU: 95%. Memory: 80%. Disk: 60%. "
            "Network latency: 200ms. Error rate: 15%."
        )
        assert _has_causal_reasoning(response) is False


# ============================================================
# Tests for _is_checklist_engineering()
# ============================================================


class TestIsChecklistEngineering:
    """Test detection of prohibited checklist patterns."""

    @pytest.mark.unit
    def test_try_these_n_things_pattern(self):
        """'Try these N things' pattern should be detected."""
        response = "Here is what to do: try these 10 things to fix the issue."
        assert _is_checklist_engineering(response) is True

    @pytest.mark.unit
    def test_here_are_n_things_pattern(self):
        """'Here are N things' pattern should be detected."""
        response = "here are 5 things you can do to resolve this."
        assert _is_checklist_engineering(response) is True

    @pytest.mark.unit
    def test_try_the_following_pattern(self):
        """'Try the following:' pattern should be detected."""
        response = "try the following: restart the service."
        assert _is_checklist_engineering(response) is True

    @pytest.mark.unit
    def test_numbered_list_with_three_items(self):
        """Numbered list with 3+ items should be detected."""
        response = "1. Check the logs\n 2. Restart the service\n 3. Verify the fix"
        assert _is_checklist_engineering(response) is True

    @pytest.mark.unit
    def test_five_bullets_triggers_detection(self):
        """Five or more bullet points should trigger checklist detection."""
        response = (
            "Steps:\n"
            "- Check CPU\n"
            "- Check memory\n"
            "- Check disk\n"
            "- Check network\n"
            "- Check logs"
        )
        assert _is_checklist_engineering(response) is True

    @pytest.mark.unit
    def test_four_bullets_does_not_trigger(self):
        """Fewer than 5 bullet points should not trigger detection."""
        response = (
            "Key findings:\n"
            "- CPU at 95%\n"
            "- Memory at 80%\n"
            "- Error rate at 15%\n"
            "- Latency at 200ms"
        )
        assert _is_checklist_engineering(response) is False

    @pytest.mark.unit
    def test_asterisk_bullets_counted(self):
        """Asterisk bullet points should also be counted."""
        response = (
            "Actions:\n"
            "* Step one\n"
            "* Step two\n"
            "* Step three\n"
            "* Step four\n"
            "* Step five"
        )
        assert _is_checklist_engineering(response) is True

    @pytest.mark.unit
    def test_mixed_dash_and_asterisk_bullets(self):
        """Mixed dash and asterisk bullets should be summed."""
        response = (
            "Steps:\n"
            "- Step one\n"
            "* Step two\n"
            "- Step three\n"
            "* Step four\n"
            "- Step five"
        )
        assert _is_checklist_engineering(response) is True

    @pytest.mark.unit
    def test_no_checklist_pattern(self):
        """Normal diagnostic reasoning should not be flagged."""
        response = (
            "OBSERVATION: The error rate spiked at 14:30. "
            "ANALYSIS: This correlates with the v2.1.3 deployment because "
            "the new connection pooling logic has a race condition."
        )
        assert _is_checklist_engineering(response) is False

    @pytest.mark.unit
    def test_empty_response(self):
        """Empty response is not a checklist."""
        assert _is_checklist_engineering("") is False

    @pytest.mark.unit
    def test_exactly_five_bullets_threshold(self):
        """Exactly 5 bullets is the threshold (>=5 triggers)."""
        four_bullets = "x\n- a\n- b\n- c\n- d"
        five_bullets = "x\n- a\n- b\n- c\n- d\n- e"
        assert _is_checklist_engineering(four_bullets) is False
        assert _is_checklist_engineering(five_bullets) is True


# ============================================================
# Tests for _is_generic_best_practices()
# ============================================================


class TestIsGenericBestPractices:
    """Test detection of generic best practices advice."""

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "phrase",
        [
            "implement monitoring",
            "add logging",
            "set up alerting",
            "follow best practices",
            "best practice is",
            "you should always",
            "it's recommended to",
        ],
    )
    def test_detects_each_generic_phrase(self, phrase):
        """Each generic phrase should be detected."""
        response = f"To prevent this in the future, {phrase} for all services."
        assert _is_generic_best_practices(response) is True

    @pytest.mark.unit
    def test_case_insensitive_detection(self):
        """Detection should be case-insensitive."""
        assert (
            _is_generic_best_practices("IMPLEMENT MONITORING for the service") is True
        )
        assert _is_generic_best_practices("Add Logging to the application") is True
        assert (
            _is_generic_best_practices("Follow Best Practices for deployments") is True
        )

    @pytest.mark.unit
    def test_no_generic_phrases(self):
        """Case-specific advice should not be flagged."""
        response = (
            "The connection pool in service-abc is exhausted because "
            "the max_connections parameter is set to 10 but the current "
            "load requires at least 50 connections."
        )
        assert _is_generic_best_practices(response) is False

    @pytest.mark.unit
    def test_empty_response(self):
        """Empty response is not generic best practices."""
        assert _is_generic_best_practices("") is False

    @pytest.mark.unit
    def test_specific_monitoring_advice_not_flagged(self):
        """Advice about a specific metric is not generic best practices (unless it
        uses a generic phrase)."""
        # This should NOT be flagged since it doesn't contain any generic phrase
        response = (
            "The p99 latency for the auth-service exceeded 5s at 14:30. "
            "The database query in getUserById is doing a full table scan."
        )
        assert _is_generic_best_practices(response) is False


# ============================================================
# Tests for _lacks_case_specificity()
# ============================================================


class TestLacksCaseSpecificity:
    """Test detection of advice lacking case-specific details."""

    @pytest.mark.unit
    def test_short_generic_response_lacks_specificity(self, investigating_case):
        """Short response without problem domain or case ID should lack specificity."""
        response = "You should check the service and see if it is running."
        assert _lacks_case_specificity(response, investigating_case) is True

    @pytest.mark.unit
    def test_response_mentioning_symptom_keywords_is_specific(self, investigating_case):
        """Response mentioning symptom keywords should be case-specific."""
        # The symptom_statement is "Database connections timing out under load with error rate spike"
        # First 5 words: "database", "connections", "timing", "out", "under"
        # Words > 4 chars: "database", "connections", "timing", "under"
        response = "The database is showing increased connection pool usage."
        assert _lacks_case_specificity(response, investigating_case) is False

    @pytest.mark.unit
    def test_response_mentioning_case_id_is_specific(self, investigating_case):
        """Response mentioning the case ID should be case-specific."""
        response = f"For case {investigating_case.case_id}, the root cause is clear."
        assert _lacks_case_specificity(response, investigating_case) is False

    @pytest.mark.unit
    def test_long_response_not_flagged(self, investigating_case):
        """Response >= 300 characters should not be flagged even without domain mention."""
        response = "x" * 300
        assert _lacks_case_specificity(response, investigating_case) is False

    @pytest.mark.unit
    def test_response_at_299_chars_without_specificity(self, investigating_case):
        """Response just under 300 chars without specificity should be flagged."""
        response = "x" * 299
        assert _lacks_case_specificity(response, investigating_case) is True

    @pytest.mark.unit
    def test_no_problem_verification(self, investigating_case_no_symptom):
        """Case without problem_verification: short generic response should lack specificity."""
        response = "Check the logs and restart the service."
        assert _lacks_case_specificity(response, investigating_case_no_symptom) is True

    @pytest.mark.unit
    def test_short_symptom_words_skipped(self, investigating_case):
        """Symptom words of length <= 4 should be skipped from matching."""
        # Symptom: "Database connections timing out under load with error rate spike"
        # "out", "with", "rate" are <= 4 chars and should be skipped.
        # Response containing only short words from the symptom should lack specificity.
        response = "The out with rate issue is not clear."
        assert _lacks_case_specificity(response, investigating_case) is True


# ============================================================
# Tests for validate_diagnostic_reasoning() — Main Entry Point
# ============================================================


class TestValidateDiagnosticReasoning:
    """Test the main validation entry point."""

    @pytest.mark.unit
    def test_inquiry_status_always_passes(self, inquiry_case):
        """INQUIRY status cases are exempt from diagnostic reasoning validation."""
        response = "Try restarting the service. You should check the logs."
        is_valid, violations = validate_diagnostic_reasoning(inquiry_case, response)
        assert is_valid is True
        assert violations == []

    @pytest.mark.unit
    def test_no_suggestions_always_passes(self, investigating_case):
        """Response without suggestion keywords passes regardless of content."""
        response = (
            "The error rate increased to 15% at 14:30 UTC. "
            "The deployment correlates with the spike."
        )
        is_valid, violations = validate_diagnostic_reasoning(
            investigating_case, response
        )
        assert is_valid is True
        assert violations == []

    @pytest.mark.unit
    def test_contains_suggestion_override_true(self, investigating_case):
        """When contains_suggestion is explicitly True, validation runs even without keywords."""
        # Response has no suggestion keywords but contains_suggestion=True forces validation
        response = "The error rate is high."
        is_valid, violations = validate_diagnostic_reasoning(
            investigating_case, response, contains_suggestion=True
        )
        assert is_valid is False
        assert len(violations) > 0

    @pytest.mark.unit
    def test_contains_suggestion_override_false(self, investigating_case):
        """When contains_suggestion is explicitly False, validation is skipped."""
        response = "Try restarting the service."
        is_valid, violations = validate_diagnostic_reasoning(
            investigating_case, response, contains_suggestion=False
        )
        assert is_valid is True
        assert violations == []

    @pytest.mark.unit
    def test_well_structured_response_passes(self, investigating_case):
        """Response with OBSERVATION + ANALYSIS + evidence + causal reasoning passes."""
        response = (
            "OBSERVATION: At 14:30, the error rate spiked to 45% after commit abc1234def "
            "was deployed. The database connections are timing out. "
            "ANALYSIS: This suggests that the new connection pooling logic "
            "causes the pool to exhaust under load, because the max_connections "
            "parameter was reduced from 50 to 10 in the deployment. "
            "Therefore, I recommend reverting the connection pool configuration."
        )
        is_valid, violations = validate_diagnostic_reasoning(
            investigating_case, response
        )
        assert is_valid is True
        assert violations == []

    @pytest.mark.unit
    def test_missing_causal_reasoning_fails(self, investigating_case):
        """Response with suggestions but no causal reasoning should fail."""
        response = (
            "OBSERVATION: At 14:30, the error rate spiked to 45% after commit abc1234def. "
            "The database connections are timing out. "
            "ANALYSIS: This indicates a connection pool issue. "
            "I recommend restarting the service."
        )
        is_valid, violations = validate_diagnostic_reasoning(
            investigating_case, response
        )
        assert is_valid is False
        assert any("causal reasoning" in v.lower() for v in violations)

    @pytest.mark.unit
    def test_not_grounded_in_evidence_fails(self, investigating_case):
        """Response lacking specific evidence references should fail."""
        response = (
            "OBSERVATION: Something went wrong with the service. "
            "ANALYSIS: This suggests a problem. "
            "Because of the issue, I recommend checking the service."
        )
        is_valid, violations = validate_diagnostic_reasoning(
            investigating_case, response
        )
        assert is_valid is False
        assert any("evidence" in v.lower() for v in violations)

    @pytest.mark.unit
    def test_checklist_engineering_fails(self, investigating_case):
        """Response with checklist pattern should fail with PROHIBITED violation."""
        response = (
            "OBSERVATION: At 14:30, the error rate spiked to 45% after commit abc1234def. "
            "The database connections are timing out. "
            "ANALYSIS: This indicates a connection pool issue because the config changed. "
            "Try these 10 things to fix it:\n"
            "- Check CPU\n"
            "- Check memory\n"
            "- Check disk\n"
            "- Check network\n"
            "- Check logs"
        )
        is_valid, violations = validate_diagnostic_reasoning(
            investigating_case, response
        )
        assert is_valid is False
        assert any("checklist engineering" in v.lower() for v in violations)

    @pytest.mark.unit
    def test_generic_best_practices_fails(self, investigating_case):
        """Response with generic best practices should fail with PROHIBITED violation."""
        response = (
            "OBSERVATION: At 14:30, the error rate spiked to 45% after commit abc1234def. "
            "The database connections are timing out. "
            "ANALYSIS: Because the config changed, the pool is exhausted. "
            "I suggest you implement monitoring and add logging for all services."
        )
        is_valid, violations = validate_diagnostic_reasoning(
            investigating_case, response
        )
        assert is_valid is False
        assert any("generic best practices" in v.lower() for v in violations)

    @pytest.mark.unit
    def test_missing_observation_section_fails(self, investigating_case):
        """Response with suggestions but no evidence grounding should fail."""
        response = (
            "ANALYSIS: Because the deployment changed the config, this causes timeouts. "
            "I recommend reverting the deployment and checking the configuration."
        )
        is_valid, violations = validate_diagnostic_reasoning(
            investigating_case, response
        )
        assert is_valid is False
        assert any(
            "observation" in v.lower() or "evidence" in v.lower() for v in violations
        )

    @pytest.mark.unit
    def test_natural_evidence_grounding_passes_observation(self, investigating_case):
        """Response with natural-language evidence grounding should pass observation check."""
        response = (
            "The error rate is at 45% and latency at 3.5s after commit abc1234def. "
            "Because the deployment changed the config, this causes timeouts. "
            "I recommend reverting the deployment."
        )
        is_valid, violations = validate_diagnostic_reasoning(
            investigating_case, response
        )
        # Should not have observation violation (natural language evidence grounding is valid)
        assert not any("observation" in v.lower() for v in violations)

    @pytest.mark.unit
    def test_missing_analysis_section_fails(self, investigating_case):
        """Response with suggestions but no ANALYSIS section should fail."""
        response = (
            "OBSERVATION: At 14:30, the error rate spiked to 45% after commit abc1234def. "
            "The database connections are timing out. "
            "I recommend reverting the deployment."
        )
        is_valid, violations = validate_diagnostic_reasoning(
            investigating_case, response
        )
        assert is_valid is False
        assert any("analysis" in v.lower() for v in violations)

    @pytest.mark.unit
    def test_multiple_violations_reported(self, investigating_case):
        """Response with multiple issues should report all violations."""
        response = "Try restarting the service."
        is_valid, violations = validate_diagnostic_reasoning(
            investigating_case, response
        )
        assert is_valid is False
        # Should have at least: missing observation, analysis, evidence, causal reasoning
        assert len(violations) >= 4

    @pytest.mark.unit
    def test_return_type_is_tuple(self, investigating_case):
        """Return value should be a (bool, list) tuple."""
        result = validate_diagnostic_reasoning(
            investigating_case, "No suggestions here."
        )
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], bool)
        assert isinstance(result[1], list)

    @pytest.mark.unit
    def test_lacks_case_specificity_violation(self, investigating_case):
        """Short generic suggestion without case-specific data should fail specificity check."""
        response = (
            "OBSERVATION: There is an issue. "
            "ANALYSIS: Because of the problem, things break. "
            "I suggest fixing it."
        )
        is_valid, violations = validate_diagnostic_reasoning(
            investigating_case, response
        )
        assert is_valid is False
        assert any("specificity" in v.lower() for v in violations)


# ============================================================
# Tests for DiagnosticReasoningError
# ============================================================


class TestDiagnosticReasoningError:
    """Test the custom exception class."""

    @pytest.mark.unit
    def test_exception_is_exception_subclass(self):
        """DiagnosticReasoningError should be an Exception subclass."""
        assert issubclass(DiagnosticReasoningError, Exception)

    @pytest.mark.unit
    def test_exception_can_be_raised_and_caught(self):
        """DiagnosticReasoningError should be raisable and catchable."""
        with pytest.raises(DiagnosticReasoningError):
            raise DiagnosticReasoningError("Missing diagnostic reasoning")

    @pytest.mark.unit
    def test_exception_message(self):
        """DiagnosticReasoningError should carry a message."""
        error = DiagnosticReasoningError("Test message")
        assert str(error) == "Test message"
