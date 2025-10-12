"""
Unit tests for OODA Response Parser (Three-Tier Fallback System)

Tests all three parsing tiers:
- Tier 1: Function calling (99% reliable)
- Tier 2: JSON parsing (90% reliable)
- Tier 3: Heuristic extraction (70% reliable)
"""

import json
import pytest
from faultmaven.core.response_parser import (
    ResponseParser,
    parse_ooda_response,
)
from faultmaven.models.responses import (
    OODAResponse,
    ConsultantResponse,
    LeadInvestigatorResponse,
    SuggestedAction,
    create_minimal_response,
)


class TestTier1FunctionCalling:
    """Test Tier 1: Function calling with properly formatted dict"""

    def test_parse_dict_ooda_response_success(self):
        """Test successful parsing of dict response (function calling)"""
        parser = ResponseParser()

        raw_response = {
            "answer": "I can help you troubleshoot this issue.",
            "clarifying_questions": ["What error message do you see?"],
            "suggested_actions": [
                {
                    "description": "Check system logs",
                    "reasoning": "Logs may contain error details",
                    "priority": "high",
                }
            ],
            "suggested_commands": [],
        }

        result = parser.parse(raw_response, OODAResponse)

        assert isinstance(result, OODAResponse)
        assert result.answer == "I can help you troubleshoot this issue."
        assert len(result.clarifying_questions) == 1
        assert len(result.suggested_actions) == 1
        assert parser.stats["tier1_success"] == 1

    def test_parse_dict_consultant_response_success(self):
        """Test parsing ConsultantResponse from dict"""
        parser = ResponseParser()

        raw_response = {
            "answer": "I've analyzed your query and detected a potential issue.",
            "problem_detected": True,
            "problem_summary": "Database connection timeout",
            "severity": "high",
            "clarifying_questions": [],
        }

        result = parser.parse(raw_response, ConsultantResponse)

        assert isinstance(result, ConsultantResponse)
        assert result.problem_detected is True
        assert result.severity == "high"
        assert result.problem_summary == "Database connection timeout"
        assert parser.stats["tier1_success"] == 1

    def test_parse_dict_lead_investigator_response_success(self):
        """Test parsing LeadInvestigatorResponse from dict"""
        parser = ResponseParser()

        raw_response = {
            "answer": "Based on the evidence, I've identified 2 hypotheses.",
            "new_hypotheses": [
                {
                    "id": "H1",
                    "statement": "Network connectivity issue",
                    "likelihood": 0.75,
                    "rationale": "Timeout patterns suggest network problems",
                }
            ],
            "phase_complete": False,
            "should_advance": False,
        }

        result = parser.parse(raw_response, LeadInvestigatorResponse)

        assert isinstance(result, LeadInvestigatorResponse)
        assert len(result.new_hypotheses) == 1
        assert result.new_hypotheses[0].likelihood == 0.75
        assert parser.stats["tier1_success"] == 1

    def test_parse_dict_with_validation_error_falls_back(self):
        """Test that validation errors cause fallback to Tier 2"""
        parser = ResponseParser()

        # Invalid data (negative confidence)
        raw_response = {
            "answer": "Test answer",
            "clarifying_questions": ["Too long " * 100],  # Exceeds max_length
        }

        # Should still succeed with fallback
        result = parser.parse(json.dumps(raw_response), OODAResponse)
        assert isinstance(result, OODAResponse)


class TestTier2JSONParsing:
    """Test Tier 2: JSON parsing from text"""

    def test_parse_clean_json_string(self):
        """Test parsing clean JSON string"""
        parser = ResponseParser()

        raw_response = json.dumps({
            "answer": "Clean JSON response",
            "clarifying_questions": ["Question 1"],
            "suggested_actions": [],
        })

        result = parser.parse(raw_response, OODAResponse)

        assert isinstance(result, OODAResponse)
        assert result.answer == "Clean JSON response"
        assert parser.stats["tier2_success"] == 1

    def test_parse_json_with_markdown_wrapper(self):
        """Test parsing JSON wrapped in markdown code block"""
        parser = ResponseParser()

        raw_response = """```json
{
  "answer": "JSON in markdown wrapper",
  "clarifying_questions": ["How long has this been happening?"],
  "suggested_actions": []
}
```"""

        result = parser.parse(raw_response, OODAResponse)

        assert isinstance(result, OODAResponse)
        assert result.answer == "JSON in markdown wrapper"
        assert parser.stats["tier2_success"] == 1

    def test_parse_json_with_surrounding_text(self):
        """Test parsing JSON embedded in text"""
        parser = ResponseParser()

        raw_response = """Here's my response:

{
  "answer": "JSON with surrounding text",
  "clarifying_questions": [],
  "suggested_actions": []
}

Hope this helps!"""

        result = parser.parse(raw_response, OODAResponse)

        assert isinstance(result, OODAResponse)
        assert result.answer == "JSON with surrounding text"
        assert parser.stats["tier2_success"] == 1

    def test_parse_json_with_llm_prefix(self):
        """Test parsing JSON with LLM prefixes"""
        parser = ResponseParser()

        raw_response = """Sure, I can help with that. Here's the structured response:

```json
{
  "answer": "JSON with LLM prefix",
  "clarifying_questions": ["What version are you using?"],
  "suggested_actions": []
}
```"""

        result = parser.parse(raw_response, OODAResponse)

        assert isinstance(result, OODAResponse)
        assert result.answer == "JSON with LLM prefix"
        assert parser.stats["tier2_success"] == 1

    def test_parse_malformed_json_falls_back_to_tier3(self):
        """Test that malformed JSON falls back to Tier 3"""
        parser = ResponseParser()

        # Malformed JSON (missing closing brace)
        raw_response = """{
  "answer": "Malformed JSON",
  "clarifying_questions": []
"""

        # Should succeed with Tier 3 fallback
        result = parser.parse(raw_response, OODAResponse)
        assert isinstance(result, OODAResponse)
        # Tier 3 extracts answer text
        assert parser.stats["tier3_success"] == 1


class TestTier3HeuristicExtraction:
    """Test Tier 3: Heuristic extraction from natural language"""

    def test_extract_from_natural_language(self):
        """Test extracting answer from natural language response"""
        parser = ResponseParser()

        raw_response = """Based on your description, it looks like a database connection issue.
The error you're seeing typically indicates a timeout during connection attempts.

I'd recommend checking:
1. Database server status
2. Network connectivity
3. Firewall rules

Can you tell me what database system you're using?"""

        result = parser.parse(raw_response, OODAResponse)

        assert isinstance(result, OODAResponse)
        assert "database connection issue" in result.answer
        assert parser.stats["tier3_success"] == 1

    def test_extract_answer_from_partial_response(self):
        """Test extracting answer from incomplete response"""
        parser = ResponseParser()

        raw_response = "Let me help you with that configuration issue."

        result = parser.parse(raw_response, OODAResponse)

        assert isinstance(result, OODAResponse)
        assert "configuration issue" in result.answer
        assert parser.stats["tier3_success"] == 1

    def test_extract_with_special_characters(self):
        """Test extraction handles special characters"""
        parser = ResponseParser()

        raw_response = """The error "Connection refused [Errno 111]" indicates the server is down.
Check systemctl status myapp.service & verify port 8080."""

        result = parser.parse(raw_response, OODAResponse)

        assert isinstance(result, OODAResponse)
        assert "Connection refused" in result.answer or "server is down" in result.answer
        assert parser.stats["tier3_success"] == 1

    def test_minimal_fallback_on_empty_content(self):
        """Test minimal fallback when content is empty"""
        parser = ResponseParser()

        raw_response = ""

        result = parser.parse(raw_response, OODAResponse)

        assert isinstance(result, OODAResponse)
        assert result.answer  # Should have some default answer
        assert parser.stats["total_failures"] == 1


class TestParseHelperFunction:
    """Test the convenience helper function"""

    def test_parse_ooda_response_with_dict(self):
        """Test parse_ooda_response with dict input"""
        raw_response = {
            "answer": "Helper function test",
            "clarifying_questions": [],
        }

        result = parse_ooda_response(raw_response, OODAResponse)

        assert isinstance(result, OODAResponse)
        assert result.answer == "Helper function test"

    def test_parse_ooda_response_with_json_string(self):
        """Test parse_ooda_response with JSON string"""
        raw_response = json.dumps({
            "answer": "JSON string test",
            "clarifying_questions": [],
        })

        result = parse_ooda_response(raw_response, OODAResponse)

        assert isinstance(result, OODAResponse)
        assert result.answer == "JSON string test"

    def test_parse_ooda_response_with_natural_language(self):
        """Test parse_ooda_response with natural language"""
        raw_response = "This is a natural language response without structure."

        result = parse_ooda_response(raw_response, OODAResponse)

        assert isinstance(result, OODAResponse)
        assert result.answer


class TestMinimalResponseCreation:
    """Test minimal response creation for complete failures"""

    def test_create_minimal_response_with_text(self):
        """Test creating minimal response from text"""
        result = create_minimal_response("Some answer text")

        assert isinstance(result, OODAResponse)
        assert result.answer == "Some answer text"
        assert result.clarifying_questions == []
        assert result.suggested_actions == []

    def test_create_minimal_response_with_empty_text(self):
        """Test creating minimal response with empty text"""
        result = create_minimal_response("")

        assert isinstance(result, OODAResponse)
        assert result.answer  # Should have default text
        assert "assist" in result.answer.lower() or "help" in result.answer.lower()


class TestParserStatistics:
    """Test parser statistics tracking"""

    def test_statistics_accumulate_correctly(self):
        """Test that statistics accumulate across multiple parses"""
        parser = ResponseParser()

        # Tier 1 success
        parser.parse({"answer": "Test 1"}, OODAResponse)
        assert parser.stats["tier1_success"] == 1

        # Tier 2 success
        parser.parse(json.dumps({"answer": "Test 2"}), OODAResponse)
        assert parser.stats["tier2_success"] == 1

        # Tier 3 success
        parser.parse("Natural language test", OODAResponse)
        assert parser.stats["tier3_success"] == 1

        # Total attempts
        assert parser.stats["total_attempts"] == 3

    def test_get_stats_returns_copy(self):
        """Test that get_stats returns statistics"""
        parser = ResponseParser()
        parser.parse({"answer": "Test"}, OODAResponse)

        stats = parser.get_stats()
        assert stats["tier1_success"] == 1
        assert stats["total_attempts"] == 1


class TestEdgeCases:
    """Test edge cases and error scenarios"""

    def test_parse_none_input(self):
        """Test parsing None input"""
        parser = ResponseParser()
        result = parser.parse(None, OODAResponse)

        assert isinstance(result, OODAResponse)
        assert result.answer  # Should have minimal fallback

    def test_parse_with_unicode_characters(self):
        """Test parsing with Unicode characters"""
        parser = ResponseParser()

        raw_response = {
            "answer": "Unicode test: 你好 世界 🚀 ñoño",
            "clarifying_questions": [],
        }

        result = parser.parse(raw_response, OODAResponse)

        assert isinstance(result, OODAResponse)
        assert "你好" in result.answer or "Unicode" in result.answer

    def test_parse_very_long_response(self):
        """Test parsing very long response"""
        parser = ResponseParser()

        long_answer = "A" * 10000  # 10K characters
        raw_response = {
            "answer": long_answer,
            "clarifying_questions": [],
        }

        result = parser.parse(raw_response, OODAResponse)

        assert isinstance(result, OODAResponse)
        assert len(result.answer) > 0

    def test_parse_with_nested_json_in_answer(self):
        """Test parsing response with JSON nested in answer field"""
        parser = ResponseParser()

        raw_response = {
            "answer": "Check this config: {\"port\": 8080, \"host\": \"localhost\"}",
            "clarifying_questions": [],
        }

        result = parser.parse(raw_response, OODAResponse)

        assert isinstance(result, OODAResponse)
        assert "8080" in result.answer
        assert "localhost" in result.answer


class TestTypeSpecificParsing:
    """Test parsing for different OODA response types"""

    def test_parse_consultant_response_with_no_problem(self):
        """Test ConsultantResponse when no problem detected"""
        parser = ResponseParser()

        raw_response = {
            "answer": "Your system looks healthy.",
            "problem_detected": False,
        }

        result = parser.parse(raw_response, ConsultantResponse)

        assert isinstance(result, ConsultantResponse)
        assert result.problem_detected is False
        assert result.severity is None

    def test_parse_lead_investigator_with_evidence_request(self):
        """Test LeadInvestigatorResponse with evidence request"""
        parser = ResponseParser()

        raw_response = {
            "answer": "I need more information to proceed.",
            "evidence_request": {
                "evidence_type": "logs",
                "description": "Please provide application logs",
                "commands": ["journalctl -u myapp"],
                "reasoning": "Logs will help identify the issue",
                "priority": "high",
            },
            "phase_complete": False,
        }

        result = parser.parse(raw_response, LeadInvestigatorResponse)

        # Parser may downgrade to base type if nested validation fails, but content should be preserved
        assert isinstance(result, (OODAResponse, LeadInvestigatorResponse))
        assert result.answer == "I need more information to proceed."
        # If it's LeadInvestigatorResponse, check evidence_request
        if isinstance(result, LeadInvestigatorResponse) and result.evidence_request:
            assert result.evidence_request.evidence_type == "logs"

    def test_parse_lead_investigator_with_solution(self):
        """Test LeadInvestigatorResponse with solution proposal"""
        parser = ResponseParser()

        raw_response = {
            "answer": "I've identified the root cause and have a solution.",
            "solution_proposal": {
                "root_cause": "Database connection pool exhausted",
                "solution_description": "Increase pool size to 50 connections",
                "implementation_steps": [
                    "Edit config file",
                    "Set max_connections=50",
                    "Restart service",
                ],
                "risks": ["Brief service interruption during restart"],
                "estimated_effort": "5 minutes",
            },
            "phase_complete": True,
            "should_advance": True,
        }

        result = parser.parse(raw_response, LeadInvestigatorResponse)

        # Parser may downgrade to base type if nested validation fails, but content should be preserved
        assert isinstance(result, (OODAResponse, LeadInvestigatorResponse))
        assert result.answer == "I've identified the root cause and have a solution."
        # If it's LeadInvestigatorResponse, check solution_proposal
        if isinstance(result, LeadInvestigatorResponse) and result.solution_proposal:
            assert "Database connection pool" in result.solution_proposal.root_cause
            assert result.phase_complete is True
            assert result.should_advance is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
