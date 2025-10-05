"""Tests for doctor/patient function schemas and extraction."""

import pytest
import json

from faultmaven.services.agentic.doctor_patient.function_schemas import (
    extract_diagnostic_state_from_function_call,
    get_function_schemas,
    UPDATE_DIAGNOSTIC_STATE_SCHEMA
)


class TestFunctionSchemas:
    """Tests for function schema definitions."""

    def test_get_function_schemas_returns_list(self):
        """Test that get_function_schemas returns a list."""
        schemas = get_function_schemas()

        assert isinstance(schemas, list)
        assert len(schemas) >= 1

    def test_update_diagnostic_state_schema_structure(self):
        """Test UPDATE_DIAGNOSTIC_STATE_SCHEMA has correct structure."""
        assert "type" in UPDATE_DIAGNOSTIC_STATE_SCHEMA
        assert UPDATE_DIAGNOSTIC_STATE_SCHEMA["type"] == "function"
        assert "function" in UPDATE_DIAGNOSTIC_STATE_SCHEMA

        function_def = UPDATE_DIAGNOSTIC_STATE_SCHEMA["function"]
        assert "name" in function_def
        assert function_def["name"] == "update_diagnostic_state"
        assert "description" in function_def
        assert "parameters" in function_def

    def test_schema_has_required_fields(self):
        """Test schema includes essential diagnostic fields."""
        schema = UPDATE_DIAGNOSTIC_STATE_SCHEMA["function"]["parameters"]
        properties = schema["properties"]

        required_fields = [
            "has_active_problem",
            "problem_statement",
            "urgency_level",
            "current_phase",
            "symptoms",
            "hypotheses",
            "root_cause",
            "solution_proposed"
        ]

        for field in required_fields:
            assert field in properties, f"Missing required field: {field}"

    def test_urgency_level_enum(self):
        """Test urgency_level has correct enum values."""
        schema = UPDATE_DIAGNOSTIC_STATE_SCHEMA["function"]["parameters"]
        urgency = schema["properties"]["urgency_level"]

        assert "enum" in urgency
        assert set(urgency["enum"]) == {"normal", "high", "critical"}

    def test_current_phase_enum(self):
        """Test current_phase has correct enum values."""
        schema = UPDATE_DIAGNOSTIC_STATE_SCHEMA["function"]["parameters"]
        phase = schema["properties"]["current_phase"]

        assert "enum" in phase
        assert set(phase["enum"]) == {0, 1, 2, 3, 4, 5}


class TestExtractDiagnosticState:
    """Tests for extracting diagnostic state from function calls."""

    def test_extract_basic_fields(self):
        """Test extraction of basic diagnostic fields."""
        function_call = {
            "name": "update_diagnostic_state",
            "arguments": json.dumps({
                "has_active_problem": True,
                "problem_statement": "API returning errors",
                "current_phase": 1
            })
        }

        result = extract_diagnostic_state_from_function_call(function_call)

        assert result["has_active_problem"] is True
        assert result["problem_statement"] == "API returning errors"
        assert result["current_phase"] == 1

    def test_extract_all_fields(self):
        """Test extraction of all diagnostic fields."""
        function_call = {
            "name": "update_diagnostic_state",
            "arguments": json.dumps({
                "has_active_problem": True,
                "problem_statement": "Database slow",
                "urgency_level": "high",
                "current_phase": 3,
                "phase_advancement_reason": "Moving to hypothesis phase",
                "symptoms": ["slow queries", "timeout errors"],
                "timeline_info": {
                    "problem_started_at": "2 hours ago",
                    "recent_changes": ["deployment"]
                },
                "hypotheses": [
                    {
                        "hypothesis": "Connection pool exhaustion",
                        "likelihood": "high",
                        "evidence": ["High connection count"],
                        "next_steps": ["Check pool metrics"]
                    }
                ],
                "tests_performed": ["Checked logs"],
                "root_cause": "",
                "solution_proposed": False,
                "solution_text": ""
            })
        }

        result = extract_diagnostic_state_from_function_call(function_call)

        assert result["has_active_problem"] is True
        assert result["urgency_level"] == "high"
        assert result["current_phase"] == 3
        assert len(result["symptoms"]) == 2
        assert len(result["hypotheses"]) == 1
        assert result["hypotheses"][0]["likelihood"] == "high"

    def test_extract_with_dict_arguments(self):
        """Test extraction when arguments are already a dict."""
        function_call = {
            "name": "update_diagnostic_state",
            "arguments": {  # Already a dict, not JSON string
                "has_active_problem": False,
                "current_phase": 0
            }
        }

        result = extract_diagnostic_state_from_function_call(function_call)

        assert result["has_active_problem"] is False
        assert result["current_phase"] == 0

    def test_extract_empty_arguments(self):
        """Test extraction with empty arguments."""
        function_call = {
            "name": "update_diagnostic_state",
            "arguments": "{}"
        }

        result = extract_diagnostic_state_from_function_call(function_call)

        assert result == {}

    def test_extract_wrong_function_name_raises_error(self):
        """Test that wrong function name raises ValueError."""
        function_call = {
            "name": "some_other_function",
            "arguments": '{"field": "value"}'
        }

        with pytest.raises(ValueError, match="Unexpected function call"):
            extract_diagnostic_state_from_function_call(function_call)

    def test_extract_invalid_json_raises_error(self):
        """Test that invalid JSON arguments raise error."""
        function_call = {
            "name": "update_diagnostic_state",
            "arguments": "not valid json {{"
        }

        with pytest.raises(json.JSONDecodeError):
            extract_diagnostic_state_from_function_call(function_call)

    def test_extract_unicode_content(self):
        """Test extraction with unicode characters."""
        function_call = {
            "name": "update_diagnostic_state",
            "arguments": json.dumps({
                "problem_statement": "错误 in API - 🔥 server down",
                "symptoms": ["症状1", "🚨 alert"]
            })
        }

        result = extract_diagnostic_state_from_function_call(function_call)

        assert "错误" in result["problem_statement"]
        assert "🔥" in result["problem_statement"]
        assert "症状1" in result["symptoms"]

    def test_extract_nested_objects(self):
        """Test extraction of nested object structures."""
        function_call = {
            "name": "update_diagnostic_state",
            "arguments": json.dumps({
                "timeline_info": {
                    "problem_started_at": "yesterday",
                    "last_known_good": "last week",
                    "recent_changes": ["deploy", "config change"]
                }
            })
        }

        result = extract_diagnostic_state_from_function_call(function_call)

        assert "timeline_info" in result
        assert result["timeline_info"]["problem_started_at"] == "yesterday"
        assert len(result["timeline_info"]["recent_changes"]) == 2

    def test_extract_complex_hypotheses(self):
        """Test extraction of complex hypothesis structures."""
        function_call = {
            "name": "update_diagnostic_state",
            "arguments": json.dumps({
                "hypotheses": [
                    {
                        "hypothesis": "Memory leak in application",
                        "likelihood": "high",
                        "evidence": ["Increasing memory", "OOM errors"],
                        "next_steps": ["Check heap dump", "Analyze GC logs"]
                    },
                    {
                        "hypothesis": "Database connection issue",
                        "likelihood": "medium",
                        "evidence": ["Connection timeouts"]
                    }
                ]
            })
        }

        result = extract_diagnostic_state_from_function_call(function_call)

        assert len(result["hypotheses"]) == 2
        assert result["hypotheses"][0]["likelihood"] == "high"
        assert len(result["hypotheses"][0]["evidence"]) == 2
        assert len(result["hypotheses"][0]["next_steps"]) == 2

    def test_extract_solution_fields(self):
        """Test extraction of solution-related fields."""
        function_call = {
            "name": "update_diagnostic_state",
            "arguments": json.dumps({
                "current_phase": 5,
                "root_cause": "Connection pool exhaustion",
                "solution_proposed": True,
                "solution_text": "Increase pool size and fix leak"
            })
        }

        result = extract_diagnostic_state_from_function_call(function_call)

        assert result["current_phase"] == 5
        assert result["root_cause"] == "Connection pool exhaustion"
        assert result["solution_proposed"] is True
        assert "Increase pool" in result["solution_text"]
