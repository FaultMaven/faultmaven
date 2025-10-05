"""Doctor/Patient architecture services.

This module contains the revolutionary doctor/patient prompting architecture
implementation with adaptive guidance and single powerful LLM approach.
"""

from .prompt_builder import build_diagnostic_prompt, format_diagnostic_state
from .turn_processor import process_turn
from .state_extractor import extract_diagnostic_state_updates

__all__ = [
    "build_diagnostic_prompt",
    "format_diagnostic_state",
    "process_turn",
    "extract_diagnostic_state_updates",
]
