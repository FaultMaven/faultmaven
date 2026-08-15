"""Pins for ``_schema_prompt_instruction`` — see its docstring for the
rationale. The gate keys on the schema's shape (``internal_reasoning``
present), so these tests drive the helper with the REAL model schemas.

Run:
    pytest tests/unit/core/investigation/test_schema_prompt_instruction.py -v
"""

from __future__ import annotations

import json

import pytest

from faultmaven.core.investigation.milestone_engine import _schema_prompt_instruction
from faultmaven.core.investigation.prompts.templates import SCHEMA_INSTRUCTIONS
from faultmaven.core.investigation.schemas import (
    InquiryResponse,
    InvestigationResponse_Diagnosis,
    TerminalResponse,
)


@pytest.mark.unit
class TestSchemaPromptInstruction:
    def test_investigation_schema_gets_field_documentation(self):
        schema = InvestigationResponse_Diagnosis.model_json_schema()
        text = _schema_prompt_instruction(schema)
        assert SCHEMA_INSTRUCTIONS in text
        assert "You MUST respond with valid JSON" in text
        assert json.dumps(schema, indent=2) in text

    def test_inquiry_schema_omits_field_documentation(self):
        """InquiryResponse has no internal_reasoning / milestones / outcome —
        "outcome: REQUIRED" against that schema is a contradiction."""
        text = _schema_prompt_instruction(InquiryResponse.model_json_schema())
        assert SCHEMA_INSTRUCTIONS not in text
        assert "You MUST respond with valid JSON" in text

    def test_terminal_schema_omits_field_documentation(self):
        text = _schema_prompt_instruction(TerminalResponse.model_json_schema())
        assert SCHEMA_INSTRUCTIONS not in text
        assert "You MUST respond with valid JSON" in text

    def test_terminal_schema_carries_no_follow_up_quota(self):
        """The inherited field description used to say "2-4 contextual
        follow-up actions" — reaching terminal prompts through the dumped
        schema JSON even with SCHEMA_INSTRUCTIONS gated off."""
        description = TerminalResponse.model_json_schema()["properties"][
            "suggested_follow_ups"
        ]["description"]
        assert "2-4" not in description
        assert "Leave empty" in description
