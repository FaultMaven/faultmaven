"""Test suite for double-encoding prevention in response parsing

This test suite ensures the three-layer defense against LLM double-encoding
doesn't regress. See docs/bugfixes/response-json-regression-root-cause-analysis.md
for full context.
"""

import pytest
import json
from faultmaven.core.response_parser import ResponseParser
from faultmaven.models.responses import ConsultantResponse, LeadInvestigatorResponse


class TestDoubleEncodingPrevention:
    """Test all three layers of double-encoding prevention"""

    def setup_method(self):
        """Set up test parser"""
        self.parser = ResponseParser()

    # =========================================================================
    # Layer 3: Fallback Extraction Tests
    # =========================================================================

    def test_extract_double_encoded_dict_answer_field(self):
        """Layer 3: Should extract from double-encoded JSON in dict answer field"""
        # Simulate LLM returning double-encoded response
        inner_response = {
            "answer": "This is the actual answer text",
            "problem_detected": True,
            "problem_summary": "Some problem"
        }
        outer_response = {
            "answer": json.dumps(inner_response),  # Double-encoded!
            "clarifying_questions": []
        }

        result = self.parser.parse(outer_response, ConsultantResponse)

        # Should extract the inner answer, not the JSON string
        assert result.answer == "This is the actual answer text"
        assert not result.answer.startswith('{')

    def test_extract_unparsed_json_string(self):
        """Layer 3: Should parse JSON string and extract answer"""
        # Simulate response as JSON string instead of dict
        response_dict = {
            "answer": "This is the actual answer",
            "clarifying_questions": [],
            "problem_detected": False
        }
        json_string = json.dumps(response_dict)

        result = self.parser.parse(json_string, ConsultantResponse)

        # Should parse the JSON and extract answer
        assert result.answer == "This is the actual answer"
        assert not result.answer.startswith('{')

    def test_extract_nested_double_encoding(self):
        """Layer 3: Should handle deeply nested double-encoding (recursive)"""
        # Simulate triple-encoding (LLM really confused)
        level3 = {"answer": "Actual text", "problem_detected": True}
        level2 = {"answer": json.dumps(level3), "clarifying_questions": []}
        level1 = json.dumps(level2)

        result = self.parser.parse(level1, ConsultantResponse)

        # Should recursively extract the actual answer
        assert result.answer == "Actual text"
        assert not result.answer.startswith('{')

    # =========================================================================
    # Tier 2: JSON Parsing Tests (Normal Operation)
    # =========================================================================

    def test_tier2_handles_clean_json(self):
        """Tier 2: Should parse clean JSON correctly"""
        json_string = json.dumps({
            "answer": "Clean answer text",
            "clarifying_questions": ["Question 1"],
            "problem_detected": True,
            "problem_summary": "Issue detected"
        })

        result = self.parser.parse(json_string, ConsultantResponse)

        assert result.answer == "Clean answer text"
        assert result.problem_detected is True
        assert len(result.clarifying_questions) == 1

    def test_tier2_handles_markdown_wrapped_json(self):
        """Tier 2: Should extract JSON from markdown code blocks"""
        response = {
            "answer": "Answer text",
            "clarifying_questions": []
        }
        markdown_wrapped = f"```json\n{json.dumps(response)}\n```"

        result = self.parser.parse(markdown_wrapped, ConsultantResponse)

        assert result.answer == "Answer text"

    # =========================================================================
    # Integration Tests with Real Response Models
    # =========================================================================

    def test_consultant_response_no_double_encoding(self):
        """ConsultantResponse should never have JSON in answer field"""
        # Test all possible variations that could trigger double-encoding
        test_cases = [
            # Normal dict
            {
                "answer": "Normal response",
                "problem_detected": True,
                "severity": "high"
            },
            # Dict with answer containing JSON-like text (should be preserved)
            {
                "answer": "Use this command: {'key': 'value'}",
                "problem_detected": False
            },
            # Double-encoded (should be fixed)
            {
                "answer": json.dumps({"answer": "Fixed text", "problem_detected": True}),
                "clarifying_questions": []
            }
        ]

        for test_case in test_cases:
            result = self.parser.parse(test_case, ConsultantResponse)

            # Answer should never start with { unless it's legitimate content
            if test_case["answer"].startswith("Use this command"):
                # Legitimate JSON-like content should be preserved
                assert "{'key': 'value'}" in result.answer
            else:
                # Double-encoded JSON should be extracted
                assert not result.answer.startswith('{"answer"')

    def test_lead_investigator_response_no_double_encoding(self):
        """LeadInvestigatorResponse should never have JSON in answer field"""
        # Double-encoded case
        inner = {
            "answer": "Investigation findings",
            "phase_complete": True,
            "evidence_request": None
        }
        outer = {
            "answer": json.dumps(inner),
            "phase_complete": False
        }

        result = self.parser.parse(outer, LeadInvestigatorResponse)

        assert result.answer == "Investigation findings"
        assert not result.answer.startswith('{')

    # =========================================================================
    # Stats Tracking Tests
    # =========================================================================

    def test_stats_track_double_encoding_fixes(self):
        """Stats should reflect when double-encoding fixes are applied"""
        # Parse a double-encoded response
        double_encoded = {
            "answer": json.dumps({"answer": "Text", "problem_detected": False}),
            "clarifying_questions": []
        }

        self.parser.parse(double_encoded, ConsultantResponse)

        stats = self.parser.get_stats()

        # Should have attempted parsing
        assert stats["total_attempts"] > 0

        # Should have either succeeded via tier1, tier2, or tier3
        total_success = (
            stats["tier1_success"] +
            stats["tier2_direct_json"] +
            stats["tier2_markdown_extraction"] +
            stats["tier2_brace_extraction"] +
            stats["tier3_success"]
        )
        assert total_success > 0

    # =========================================================================
    # Edge Cases
    # =========================================================================

    def test_empty_answer_not_double_encoded(self):
        """Empty answer should not be treated as double-encoding"""
        response = {
            "answer": "",
            "clarifying_questions": []
        }

        result = self.parser.parse(response, ConsultantResponse)

        # Should fall back to minimal response, not crash
        assert isinstance(result.answer, str)

    def test_json_array_in_answer_not_confused_with_double_encoding(self):
        """JSON arrays in answer should not trigger double-encoding fix"""
        response = {
            "answer": "Try these values: [1, 2, 3]",
            "clarifying_questions": []
        }

        result = self.parser.parse(response, ConsultantResponse)

        # Should preserve the array notation in the text
        assert "[1, 2, 3]" in result.answer

    def test_malformed_json_in_answer_doesnt_crash(self):
        """Malformed JSON in answer field should not crash parser"""
        response = {
            "answer": "{this is not valid json}",
            "clarifying_questions": []
        }

        result = self.parser.parse(response, ConsultantResponse)

        # Should keep the malformed JSON as-is (it's not double-encoding)
        assert result.answer == "{this is not valid json}"


class TestSchemaConsistency:
    """Test that prompt schema matches Pydantic model schema"""

    def test_consultant_response_schema_fields(self):
        """ConsultantResponse schema should have all expected fields"""
        schema = ConsultantResponse.model_json_schema()
        properties = schema.get("properties", {})

        # Required base fields
        assert "answer" in properties
        assert properties["answer"]["type"] == "string"

        # Consultant-specific fields
        assert "problem_detected" in properties
        assert properties["problem_detected"]["type"] == "boolean"

    def test_lead_investigator_response_schema_fields(self):
        """LeadInvestigatorResponse schema should have all expected fields"""
        schema = LeadInvestigatorResponse.model_json_schema()
        properties = schema.get("properties", {})

        # Required base fields
        assert "answer" in properties

        # Lead investigator-specific fields
        assert "phase_complete" in properties
        assert properties["phase_complete"]["type"] == "boolean"

    def test_schema_generation_matches_model(self):
        """Schema example generation should match actual model"""
        from faultmaven.models.responses import ConsultantResponse

        schema = ConsultantResponse.model_json_schema()
        generated_example = {}

        for field_name, field_info in schema.get("properties", {}).items():
            field_type = field_info.get("type", "string")
            if field_type == "string":
                if field_name == "answer":
                    generated_example[field_name] = "Your natural language response here"
                else:
                    generated_example[field_name] = "appropriate value"
            elif field_type == "boolean":
                generated_example[field_name] = False
            elif field_type == "array":
                generated_example[field_name] = []

        # Verify all required fields are present
        required_fields = schema.get("required", [])
        for field in required_fields:
            assert field in generated_example, f"Required field {field} missing from generated example"


@pytest.mark.integration
class TestDoubleEncodingPreventionIntegration:
    """Integration tests with actual phase handler flow"""

    def test_phase_handler_prompt_includes_correct_schema(self):
        """Phase handler should generate prompt with correct schema example"""
        # This would require mocking the phase handler and LLM provider
        # Placeholder for integration test
        pytest.skip("Integration test - requires full phase handler setup")

    def test_post_parse_validation_catches_double_encoding(self):
        """Post-parse validation in base.py should catch double-encoding"""
        # This would test lines 363-386 of base.py
        pytest.skip("Integration test - requires phase handler setup")
