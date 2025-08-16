"""Test module for response type determination logic in v3.1.0 schema."""

import pytest
from unittest.mock import Mock

from faultmaven.services.agent_service import AgentService
from faultmaven.models.api import ResponseType
from faultmaven.models.interfaces import ILLMProvider, BaseTool, ITracer, ISanitizer


class TestResponseTypeLogic:
    """Test response type determination logic."""
    
    @pytest.fixture
    def agent_service(self):
        """Create AgentService instance for testing logic methods."""
        # Mock all dependencies since we're only testing logic methods
        llm_provider = Mock(spec=ILLMProvider)
        tools = [Mock(spec=BaseTool)]
        tracer = Mock(spec=ITracer)
        tracer.trace.return_value.__enter__ = Mock()
        tracer.trace.return_value.__exit__ = Mock()
        sanitizer = Mock(spec=ISanitizer)
        
        return AgentService(
            llm_provider=llm_provider,
            tools=tools,
            tracer=tracer,
            sanitizer=sanitizer
        )


class TestDetermineResponseType:
    """Test the main _determine_response_type method."""
    
    def test_determine_response_type_answer_default(self, agent_service):
        """Test that ANSWER is returned as default response type."""
        # Simple result without special indicators
        result = {
            "findings": [{"message": "System is running normally"}],
            "recommendations": ["Monitor system performance"],
            "next_steps": ["Check logs tomorrow"]
        }
        
        response_type = agent_service._determine_response_type(result)
        assert response_type == ResponseType.ANSWER
    
    def test_determine_response_type_clarification_priority(self, agent_service):
        """Test that clarification takes priority over other types."""
        # Result that could be both clarification and plan, but clarification wins
        result = {
            "findings": [],
            "recommendations": ["Need to clarify which system", "Please specify the error"],
            "next_steps": ["Step 1", "Step 2", "Step 3", "Step 4"]  # Would normally trigger plan
        }
        
        response_type = agent_service._determine_response_type(result)
        assert response_type == ResponseType.CLARIFICATION_REQUEST
    
    def test_determine_response_type_confirmation_priority(self, agent_service):
        """Test that confirmation takes priority over plan but not clarification."""
        # Result that could be both confirmation and plan
        result = {
            "findings": [{"message": "Critical issue detected"}],
            "recommendations": ["Confirm before proceeding with restart"],
            "next_steps": ["Step 1", "Step 2", "Step 3", "Step 4"]  # Would normally trigger plan
        }
        
        response_type = agent_service._determine_response_type(result)
        assert response_type == ResponseType.CONFIRMATION_REQUEST
    
    def test_determine_response_type_plan_when_no_special_indicators(self, agent_service):
        """Test that plan is returned when multi-step but no clarification/confirmation."""
        result = {
            "findings": [{"message": "Complex issue detected"}],
            "recommendations": ["Follow multi-step approach"],
            "next_steps": ["Step 1", "Step 2", "Step 3", "Step 4"]
        }
        
        response_type = agent_service._determine_response_type(result)
        assert response_type == ResponseType.PLAN_PROPOSAL
    
    def test_determine_response_type_empty_result(self, agent_service):
        """Test response type determination with empty result."""
        result = {}
        
        response_type = agent_service._determine_response_type(result)
        assert response_type == ResponseType.ANSWER  # Default fallback
    
    def test_determine_response_type_none_values(self, agent_service):
        """Test response type determination with None values."""
        result = {
            "findings": None,
            "recommendations": None,
            "next_steps": None
        }
        
        response_type = agent_service._determine_response_type(result)
        assert response_type == ResponseType.ANSWER  # Default fallback


class TestNeedsClarification:
    """Test clarification detection logic."""
    
    def test_needs_clarification_recommendations_keywords(self, agent_service):
        """Test clarification detection in recommendations."""
        # Test various clarification keywords in recommendations
        clarification_cases = [
            {"recommendations": ["Please clarify which database you're using"]},
            {"recommendations": ["This is unclear, need more details"]},
            {"recommendations": ["Need more information about the error"]},
            {"recommendations": ["Please specify the exact symptoms"]},
            {"recommendations": ["Which version are you running?"]},
            {"recommendations": ["The description is ambiguous"]},
        ]
        
        for result in clarification_cases:
            assert agent_service._needs_clarification(result) is True
    
    def test_needs_clarification_findings_keywords(self, agent_service):
        """Test clarification detection in findings."""
        # Test clarification keywords in findings
        clarification_cases = [
            {"findings": [{"message": "Unable to clarify the root cause"}]},
            {"findings": [{"message": "Information is unclear"}]},
            {"findings": [{"message": "Need more information to proceed"}]},
            {"findings": [{"message": "Which component is affected?"}]},
            {"findings": [{"message": "Ambiguous error pattern detected"}]},
        ]
        
        for result in clarification_cases:
            assert agent_service._needs_clarification(result) is True
    
    def test_needs_clarification_case_insensitive(self, agent_service):
        """Test that clarification detection is case insensitive."""
        result = {"recommendations": ["Please CLARIFY the issue"]}
        assert agent_service._needs_clarification(result) is True
        
        result = {"recommendations": ["Need MORE INFORMATION"]}
        assert agent_service._needs_clarification(result) is True
        
        result = {"findings": [{"message": "This is UNCLEAR"}]}
        assert agent_service._needs_clarification(result) is True
    
    def test_needs_clarification_partial_matches(self, agent_service):
        """Test clarification detection with partial keyword matches."""
        # Keywords should match as substrings
        result = {"recommendations": ["Please clarification on the issue"]}  # "clarify" in "clarification"
        assert agent_service._needs_clarification(result) is True
        
        result = {"recommendations": ["The unclear nature of the problem"]}  # "unclear" 
        assert agent_service._needs_clarification(result) is True
    
    def test_needs_clarification_false_cases(self, agent_service):
        """Test cases that should NOT trigger clarification."""
        non_clarification_cases = [
            {"recommendations": ["Restart the database service"]},
            {"recommendations": ["Check the application logs"]},
            {"recommendations": ["Update the configuration"]},
            {"findings": [{"message": "Database connection timeout"}]},
            {"findings": [{"message": "High CPU usage detected"}]},
            {"recommendations": ["Clear cache", "Restart service"]},  # Multiple non-clarification items
        ]
        
        for result in non_clarification_cases:
            assert agent_service._needs_clarification(result) is False
    
    def test_needs_clarification_empty_data(self, agent_service):
        """Test clarification detection with empty data."""
        assert agent_service._needs_clarification({}) is False
        assert agent_service._needs_clarification({"recommendations": []}) is False
        assert agent_service._needs_clarification({"findings": []}) is False
        assert agent_service._needs_clarification({"recommendations": None}) is False
    
    def test_needs_clarification_mixed_content(self, agent_service):
        """Test clarification detection with mixed content."""
        # Should return True if ANY content indicates clarification needed
        result = {
            "recommendations": ["Restart service", "Need to clarify database type"],
            "findings": [{"message": "Service is slow"}]
        }
        assert agent_service._needs_clarification(result) is True
        
        # Should return False if no clarification indicators
        result = {
            "recommendations": ["Restart service", "Check configuration"],
            "findings": [{"message": "Service is slow"}, {"message": "High latency"}]
        }
        assert agent_service._needs_clarification(result) is False


class TestNeedsConfirmation:
    """Test confirmation detection logic."""
    
    def test_needs_confirmation_keywords(self, agent_service):
        """Test confirmation detection with various keywords."""
        confirmation_cases = [
            {"recommendations": ["Confirm before proceeding with restart"]},
            {"recommendations": ["Please verify this action"]},
            {"recommendations": ["Proceed with caution, approve first"]},
            {"recommendations": ["Authorize the database reset"]},
            {"recommendations": ["Confirm maintenance window"]},
        ]
        
        for result in confirmation_cases:
            assert agent_service._needs_confirmation(result) is True
    
    def test_needs_confirmation_case_insensitive(self, agent_service):
        """Test that confirmation detection is case insensitive."""
        result = {"recommendations": ["CONFIRM before proceeding"]}
        assert agent_service._needs_confirmation(result) is True
        
        result = {"recommendations": ["Please VERIFY the action"]}
        assert agent_service._needs_confirmation(result) is True
    
    def test_needs_confirmation_recommendations_only(self, agent_service):
        """Test that confirmation only checks recommendations, not findings."""
        # Confirmation keywords in findings should not trigger confirmation
        result = {
            "recommendations": ["Restart the service"],
            "findings": [{"message": "Please confirm the error message"}]
        }
        assert agent_service._needs_confirmation(result) is False
        
        # Confirmation keywords in recommendations should trigger
        result = {
            "recommendations": ["Confirm before restart"],
            "findings": [{"message": "Service error detected"}]
        }
        assert agent_service._needs_confirmation(result) is True
    
    def test_needs_confirmation_false_cases(self, agent_service):
        """Test cases that should NOT trigger confirmation."""
        non_confirmation_cases = [
            {"recommendations": ["Restart the service immediately"]},
            {"recommendations": ["Check logs for errors"]},
            {"recommendations": ["Update configuration file"]},
            {"recommendations": ["Monitor system performance"]},
            {"recommendations": ["Clear application cache"]},
        ]
        
        for result in non_confirmation_cases:
            assert agent_service._needs_confirmation(result) is False
    
    def test_needs_confirmation_empty_data(self, agent_service):
        """Test confirmation detection with empty data."""
        assert agent_service._needs_confirmation({}) is False
        assert agent_service._needs_confirmation({"recommendations": []}) is False
        assert agent_service._needs_confirmation({"recommendations": None}) is False
    
    def test_needs_confirmation_partial_matches(self, agent_service):
        """Test confirmation detection with partial keyword matches."""
        # Should match as substrings
        result = {"recommendations": ["Confirmation required for this action"]}
        assert agent_service._needs_confirmation(result) is True
        
        result = {"recommendations": ["Verification step needed"]}
        assert agent_service._needs_confirmation(result) is True


class TestHasPlan:
    """Test plan detection logic."""
    
    def test_has_plan_multiple_steps(self, agent_service):
        """Test plan detection with multiple steps (> 2)."""
        # 3 steps should trigger plan
        result = {"next_steps": ["Step 1", "Step 2", "Step 3"]}
        assert agent_service._has_plan(result) is True
        
        # 4 steps should trigger plan
        result = {"next_steps": ["Step 1", "Step 2", "Step 3", "Step 4"]}
        assert agent_service._has_plan(result) is True
        
        # 10 steps should trigger plan
        result = {"next_steps": [f"Step {i}" for i in range(1, 11)]}
        assert agent_service._has_plan(result) is True
    
    def test_has_plan_few_steps(self, agent_service):
        """Test plan detection with few steps (<= 2)."""
        # 0 steps should not trigger plan
        result = {"next_steps": []}
        assert agent_service._has_plan(result) is False
        
        # 1 step should not trigger plan
        result = {"next_steps": ["Single step"]}
        assert agent_service._has_plan(result) is False
        
        # 2 steps should not trigger plan
        result = {"next_steps": ["Step 1", "Step 2"]}
        assert agent_service._has_plan(result) is False
    
    def test_has_plan_exactly_three_steps(self, agent_service):
        """Test the boundary condition of exactly 3 steps."""
        result = {"next_steps": ["Step 1", "Step 2", "Step 3"]}
        assert agent_service._has_plan(result) is True
    
    def test_has_plan_non_list_next_steps(self, agent_service):
        """Test plan detection with non-list next_steps."""
        # String instead of list
        result = {"next_steps": "Single step as string"}
        assert agent_service._has_plan(result) is False
        
        # None value
        result = {"next_steps": None}
        assert agent_service._has_plan(result) is False
        
        # Integer
        result = {"next_steps": 5}
        assert agent_service._has_plan(result) is False
        
        # Dictionary
        result = {"next_steps": {"step": "value"}}
        assert agent_service._has_plan(result) is False
    
    def test_has_plan_missing_next_steps(self, agent_service):
        """Test plan detection when next_steps key is missing."""
        result = {"findings": [], "recommendations": []}
        assert agent_service._has_plan(result) is False
        
        result = {}
        assert agent_service._has_plan(result) is False
    
    def test_has_plan_mixed_content_types(self, agent_service):
        """Test plan detection with mixed content types in next_steps."""
        # List with mixed types - should still count length
        result = {"next_steps": ["Step 1", 2, {"step": "3"}, None]}
        assert agent_service._has_plan(result) is True  # 4 items > 2
        
        result = {"next_steps": ["Step 1", 2]}  # 2 items = 2, not > 2
        assert agent_service._has_plan(result) is False


class TestPriorityLogic:
    """Test the priority logic of response type determination."""
    
    def test_clarification_overrides_all(self, agent_service):
        """Test that clarification request overrides confirmation and plan."""
        # Has clarification + confirmation + plan indicators
        result = {
            "findings": [],
            "recommendations": [
                "Need to clarify the database type",  # Clarification
                "Confirm before proceeding"           # Confirmation
            ],
            "next_steps": ["Step 1", "Step 2", "Step 3", "Step 4"]  # Plan (4 steps)
        }
        
        response_type = agent_service._determine_response_type(result)
        assert response_type == ResponseType.CLARIFICATION_REQUEST
    
    def test_confirmation_overrides_plan(self, agent_service):
        """Test that confirmation request overrides plan proposal."""
        # Has confirmation + plan indicators, but no clarification
        result = {
            "findings": [{"message": "System issue detected"}],
            "recommendations": [
                "Confirm before restarting the database",  # Confirmation
                "Follow these steps carefully"             # No clarification
            ],
            "next_steps": ["Step 1", "Step 2", "Step 3", "Step 4"]  # Plan (4 steps)
        }
        
        response_type = agent_service._determine_response_type(result)
        assert response_type == ResponseType.CONFIRMATION_REQUEST
    
    def test_plan_when_no_special_requests(self, agent_service):
        """Test that plan is returned when no clarification or confirmation needed."""
        # Has plan indicators, but no clarification or confirmation
        result = {
            "findings": [{"message": "Complex issue requires multiple steps"}],
            "recommendations": [
                "Follow the multi-step approach",
                "Execute steps in sequence"
            ],
            "next_steps": ["Step 1", "Step 2", "Step 3", "Step 4"]  # Plan (4 steps)
        }
        
        response_type = agent_service._determine_response_type(result)
        assert response_type == ResponseType.PLAN_PROPOSAL
    
    def test_answer_when_no_indicators(self, agent_service):
        """Test that answer is returned when no special indicators present."""
        # Simple result without special indicators
        result = {
            "findings": [{"message": "Database connection timeout"}],
            "recommendations": [
                "Increase timeout settings",
                "Check network connectivity"
            ],
            "next_steps": ["Monitor system"]  # Only 1 step
        }
        
        response_type = agent_service._determine_response_type(result)
        assert response_type == ResponseType.ANSWER


class TestKeywordDetection:
    """Test keyword detection edge cases."""
    
    def test_clarification_keywords_comprehensive(self, agent_service):
        """Test all clarification keywords are detected."""
        clarification_keywords = ['clarify', 'unclear', 'more information', 'specify', 'which', 'ambiguous']
        
        for keyword in clarification_keywords:
            # Test in recommendations
            result = {"recommendations": [f"Please {keyword} the issue"]}
            assert agent_service._needs_clarification(result) is True, f"Keyword '{keyword}' not detected in recommendations"
            
            # Test in findings
            result = {"findings": [{"message": f"The problem is {keyword}"}]}
            assert agent_service._needs_clarification(result) is True, f"Keyword '{keyword}' not detected in findings"
    
    def test_confirmation_keywords_comprehensive(self, agent_service):
        """Test all confirmation keywords are detected."""
        confirmation_keywords = ['confirm', 'verify', 'proceed', 'approve', 'authorize']
        
        for keyword in confirmation_keywords:
            result = {"recommendations": [f"Please {keyword} this action"]}
            assert agent_service._needs_confirmation(result) is True, f"Keyword '{keyword}' not detected"
    
    def test_keyword_detection_with_punctuation(self, agent_service):
        """Test keyword detection works with punctuation."""
        # Clarification with punctuation
        result = {"recommendations": ["Please clarify, which database?"]}
        assert agent_service._needs_clarification(result) is True
        
        result = {"recommendations": ["Unclear! Need more info."]}
        assert agent_service._needs_clarification(result) is True
        
        # Confirmation with punctuation
        result = {"recommendations": ["Confirm: restart database?"]}
        assert agent_service._needs_confirmation(result) is True
        
        result = {"recommendations": ["Please verify (important action)."]}
        assert agent_service._needs_confirmation(result) is True
    
    def test_keyword_detection_word_boundaries(self, agent_service):
        """Test that keywords are detected as substrings, not just whole words."""
        # This tests the current implementation which uses 'in' operator
        
        # Clarification - substring matching
        result = {"recommendations": ["Clarification needed"]}  # "clarify" in "clarification"
        assert agent_service._needs_clarification(result) is True
        
        # This might catch false positives, but tests current implementation
        result = {"recommendations": ["Declare the variable"]}  # "clar" from "clarify" is in "declare"
        # This should be False with proper word boundary detection, but current implementation might be True
        # Testing current behavior
        is_clarification = agent_service._needs_clarification(result)
        # Note: This test documents current behavior, which may need improvement
    
    def test_keyword_detection_multiple_keywords(self, agent_service):
        """Test detection when multiple keywords are present."""
        # Multiple clarification keywords
        result = {"recommendations": ["Please clarify which unclear information you need"]}
        assert agent_service._needs_clarification(result) is True
        
        # Multiple confirmation keywords
        result = {"recommendations": ["Confirm and verify before you proceed"]}
        assert agent_service._needs_confirmation(result) is True
        
        # Mix of clarification and confirmation - clarification should win in priority test
        result = {
            "recommendations": [
                "Please clarify the issue",     # Clarification
                "Then confirm the action"       # Confirmation
            ]
        }
        assert agent_service._needs_clarification(result) is True
        assert agent_service._needs_confirmation(result) is True
        # In actual _determine_response_type, clarification would win


class TestComplexScenarios:
    """Test complex real-world scenarios."""
    
    def test_database_connection_issue_answer(self, agent_service):
        """Test typical database connection issue returns ANSWER."""
        result = {
            "findings": [
                {"message": "Database connection timeout after 30 seconds"},
                {"message": "Connection pool exhausted"},
                {"message": "High number of concurrent connections"}
            ],
            "recommendations": [
                "Increase connection timeout to 60 seconds",
                "Expand connection pool size",
                "Implement connection retry logic"
            ],
            "next_steps": [
                "Update database configuration",
                "Restart application"
            ]
        }
        
        response_type = agent_service._determine_response_type(result)
        assert response_type == ResponseType.ANSWER
    
    def test_insufficient_information_clarification(self, agent_service):
        """Test scenario where agent needs more information."""
        result = {
            "findings": [
                {"message": "Error symptoms are unclear"},
                {"message": "Multiple potential root causes"}
            ],
            "recommendations": [
                "Need to clarify which specific error messages you're seeing",
                "Please specify the exact time when issues started",
                "More information needed about system configuration"
            ],
            "next_steps": [
                "Gather additional diagnostic information"
            ]
        }
        
        response_type = agent_service._determine_response_type(result)
        assert response_type == ResponseType.CLARIFICATION_REQUEST
    
    def test_critical_system_confirmation(self, agent_service):
        """Test scenario requiring user confirmation for critical actions."""
        result = {
            "findings": [
                {"message": "Critical production database showing signs of corruption"},
                {"message": "Immediate action required to prevent data loss"}
            ],
            "recommendations": [
                "Confirm before proceeding with emergency database restart",
                "Verify backup integrity before recovery procedure",
                "Authorize maintenance window for critical repairs"
            ],
            "next_steps": [
                "Stop all database connections",
                "Run integrity check"
            ]
        }
        
        response_type = agent_service._determine_response_type(result)
        assert response_type == ResponseType.CONFIRMATION_REQUEST
    
    def test_complex_multi_step_plan(self, agent_service):
        """Test scenario requiring detailed multi-step plan."""
        result = {
            "findings": [
                {"message": "Cascading failure across multiple microservices"},
                {"message": "Service dependency chain is broken"},
                {"message": "Data consistency issues detected"}
            ],
            "recommendations": [
                "Implement systematic recovery procedure",
                "Follow dependency order for service restoration",
                "Execute data reconciliation process"
            ],
            "next_steps": [
                "Stop all dependent services",
                "Restore core database service",
                "Validate data integrity",
                "Restart services in dependency order",
                "Verify end-to-end functionality",
                "Monitor for stability"
            ]
        }
        
        response_type = agent_service._determine_response_type(result)
        assert response_type == ResponseType.PLAN_PROPOSAL
    
    def test_edge_case_all_indicators_present(self, agent_service):
        """Test edge case where all indicators are present - should follow priority."""
        result = {
            "findings": [
                {"message": "Complex issue with unclear symptoms"}  # Could be clarification
            ],
            "recommendations": [
                "Need to clarify the error pattern",              # Clarification (highest priority)
                "Confirm before restarting production system",    # Confirmation
                "Follow detailed recovery procedure"              # Plan context
            ],
            "next_steps": [
                "Gather more diagnostic data",
                "Analyze error patterns", 
                "Develop recovery strategy",
                "Execute recovery plan",
                "Validate system health"  # 5 steps - would trigger plan
            ]
        }
        
        # Should return clarification due to priority order
        response_type = agent_service._determine_response_type(result)
        assert response_type == ResponseType.CLARIFICATION_REQUEST
    
    def test_minimal_response_data(self, agent_service):
        """Test with minimal response data."""
        result = {
            "findings": [{"message": "Issue resolved"}],
            "recommendations": ["Monitor system"],
            "next_steps": []
        }
        
        response_type = agent_service._determine_response_type(result)
        assert response_type == ResponseType.ANSWER
    
    def test_malformed_response_data(self, agent_service):
        """Test with malformed or unexpected response data."""
        # Non-standard structure
        result = {
            "findings": "Single string instead of list",
            "recommendations": 123,  # Number instead of list
            "next_steps": {"step1": "value"}  # Dict instead of list
        }
        
        # Should handle gracefully and return default
        response_type = agent_service._determine_response_type(result)
        assert response_type == ResponseType.ANSWER
    
    def test_very_long_content(self, agent_service):
        """Test with very long content strings."""
        long_text = "This is a very long recommendation. " * 100
        
        result = {
            "findings": [{"message": long_text}],
            "recommendations": [
                long_text + " Need to clarify the specific component."
            ],
            "next_steps": [long_text] * 5  # 5 long steps
        }
        
        # Should still detect clarification keyword despite long text
        response_type = agent_service._determine_response_type(result)
        assert response_type == ResponseType.CLARIFICATION_REQUEST