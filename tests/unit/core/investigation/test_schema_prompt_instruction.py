"""SCHEMA_INSTRUCTIONS must not reach terminal turns.

``SCHEMA_INSTRUCTIONS`` documents the investigation-turn output shape
(state_updates milestones/evidence, internal_reasoning, "2-4"
suggested_follow_ups). ``TerminalResponse`` carries none of that shape, and
TERMINAL_TEMPLATE explicitly instructs the LLM to leave suggested_follow_ups
empty — so the in-prompt schema block built for providers that need the
schema in prompt text (json_object / prompt_only strategies) must include
SCHEMA_INSTRUCTIONS on inquiry/investigation turns and omit it on terminal
turns, where the bare schema-compliance directive plus the exact JSON schema
is the whole instruction.

Run:
    pytest tests/unit/core/investigation/test_schema_prompt_instruction.py -v
"""

from __future__ import annotations

import pytest

from faultmaven.core.investigation.milestone_engine import _schema_prompt_instruction
from faultmaven.core.investigation.prompts.templates import SCHEMA_INSTRUCTIONS
from faultmaven.core.investigation.schemas import (
    InquiryResponse,
    InvestigationResponse_Diagnosis,
    TerminalResponse,
)

SCHEMA_JSON = '{"properties": {"marker_key": {}}}'


@pytest.mark.unit
class TestSchemaPromptInstruction:
    def test_investigation_turns_include_field_documentation(self):
        text = _schema_prompt_instruction(InvestigationResponse_Diagnosis, SCHEMA_JSON)
        assert SCHEMA_INSTRUCTIONS in text
        assert "You MUST respond with valid JSON" in text
        assert SCHEMA_JSON in text

    def test_inquiry_turns_include_field_documentation(self):
        text = _schema_prompt_instruction(InquiryResponse, SCHEMA_JSON)
        assert SCHEMA_INSTRUCTIONS in text

    def test_terminal_turns_omit_field_documentation(self):
        """TerminalResponse has no milestones/internal_reasoning, and the
        TERMINAL template says to leave suggested_follow_ups empty — the
        block would contradict both."""
        text = _schema_prompt_instruction(TerminalResponse, SCHEMA_JSON)
        assert SCHEMA_INSTRUCTIONS not in text
        # The schema-compliance directive and the exact schema are still there.
        assert "You MUST respond with valid JSON" in text
        assert SCHEMA_JSON in text
        # The specific contradiction: a "2-4 suggestions" directive on a turn
        # whose template says to leave suggestions empty.
        assert "2-4 suggestions" not in text
