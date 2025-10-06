"""Function schemas for doctor/patient architecture function calling.

These schemas define the structure for LLM function calling to update diagnostic state.
"""

from typing import Any, Dict, List


# Diagnostic state update function schema (OpenAI/Fireworks compatible)
UPDATE_DIAGNOSTIC_STATE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "update_diagnostic_state",
        "description": "Update the internal diagnostic state based on conversation analysis. Call this after each user interaction to track troubleshooting progress.",
        "parameters": {
            "type": "object",
            "properties": {
                "has_active_problem": {
                    "type": "boolean",
                    "description": "Whether the user has an active problem that needs troubleshooting"
                },
                "problem_statement": {
                    "type": "string",
                    "description": "Concise summary of the user's problem (max 200 chars)"
                },
                "urgency_level": {
                    "type": "string",
                    "enum": ["normal", "high", "critical"],
                    "description": "Urgency level: normal (routine), high (important), critical (production down)"
                },
                "current_phase": {
                    "type": "integer",
                    "enum": [0, 1, 2, 3, 4, 5],
                    "description": "Current SRE phase: 0=Intake, 1=Blast Radius, 2=Timeline, 3=Hypothesis, 4=Validation, 5=Solution"
                },
                "phase_advancement_reason": {
                    "type": "string",
                    "description": "Why you're advancing to this phase (goal-oriented assessment)"
                },
                "symptoms": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of observed symptoms and error indicators"
                },
                "timeline_info": {
                    "type": "object",
                    "description": "Timeline information (when problem started, what changed, etc.)",
                    "properties": {
                        "problem_started_at": {"type": "string"},
                        "last_known_good": {"type": "string"},
                        "recent_changes": {
                            "type": "array",
                            "items": {"type": "string"}
                        }
                    }
                },
                "hypotheses": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "hypothesis": {"type": "string", "description": "The hypothesis about root cause"},
                            "likelihood": {
                                "type": "string",
                                "enum": ["low", "medium", "high"],
                                "description": "Confidence in this hypothesis"
                            },
                            "evidence": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Supporting evidence for this hypothesis"
                            },
                            "next_steps": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Steps to validate this hypothesis"
                            }
                        },
                        "required": ["hypothesis", "likelihood"]
                    },
                    "description": "Working hypotheses about root cause"
                },
                "tests_performed": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Diagnostic tests/commands already run by user"
                },
                "root_cause": {
                    "type": "string",
                    "description": "Identified root cause (if determined)"
                },
                "solution_proposed": {
                    "type": "boolean",
                    "description": "Whether a solution has been proposed"
                },
                "solution_text": {
                    "type": "string",
                    "description": "The proposed solution"
                }
            },
            "required": []  # All fields are optional (delta updates)
        }
    }
}


def get_function_schemas() -> List[Dict[str, Any]]:
    """Get all function schemas for doctor/patient architecture."""
    return [UPDATE_DIAGNOSTIC_STATE_SCHEMA]


def extract_diagnostic_state_from_function_call(function_call: Dict[str, Any]) -> Dict[str, Any]:
    """Extract and parse diagnostic state updates from function call.

    Args:
        function_call: The function call dict from LLM response
                      Format: {"name": "...", "arguments": "..."}

    Returns:
        Parsed state updates dictionary
    """
    import json

    if function_call.get("name") != "update_diagnostic_state":
        raise ValueError(f"Unexpected function call: {function_call.get('name')}")

    # Parse arguments (might be string or dict)
    arguments = function_call.get("arguments", "{}")
    if isinstance(arguments, str):
        arguments = json.loads(arguments)

    return arguments
