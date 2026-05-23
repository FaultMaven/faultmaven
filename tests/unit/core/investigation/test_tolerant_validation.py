"""Tests for tolerant_validate — field-level schema degradation.

Covers the architectural claim that one general fix replaces an
unbounded per-variant patching effort. Each test maps to one of the
LLM-output failure variants observed in the test sequence (Runs 14-18):

  - Variant B (prose strings where objects expected)
  - Variant C (key:value fragments scattered as array elements)
  - Variant D (cross-field validator violation on a list item)
  - Variant E (null where typed dict expected, has default)
  - Plus: critical (required, no default) fields still hard-fail
  - Plus: mixed strippable + non-strippable in same response
"""

from typing import Any, Dict, List, Optional

import pytest
from pydantic import BaseModel, Field, model_validator

from faultmaven.core.investigation.tolerant_validation import tolerant_validate

# ============================================================
# Fixture schemas — modeled on the real failure shapes
# ============================================================


class _ReasoningItem(BaseModel):
    observation: str
    inference: str
    confidence: float = Field(ge=0.0, le=1.0)


class _FollowUp(BaseModel):
    label: str
    payload: str


class _Evidence(BaseModel):
    """Models the EvidenceToAdd cross-field validator pattern (Variant D)."""

    summary: str
    source_type: str  # 'logs' | 'user_description' etc.
    source_file_id: Optional[str] = None

    @model_validator(mode="after")
    def _require_source_file_id_unless_user_description(self) -> "_Evidence":
        if self.source_file_id is None and self.source_type != "user_description":
            raise ValueError(
                "source_file_id is required unless source_type=user_description"
            )
        return self


class _StateUpdates(BaseModel):
    """Models the InvestigationResponse_Treatment.state_updates pattern."""

    evidence_to_add: List[_Evidence] = Field(default_factory=list)
    hypotheses_to_update: Dict[str, Any] = Field(default_factory=dict)


class _Response(BaseModel):
    """Root model — agent_response is critical (required), everything else degrades."""

    agent_response: str  # critical: required, no default
    reasoning: Optional[List[_ReasoningItem]] = Field(default_factory=list)
    follow_ups: Optional[List[_FollowUp]] = Field(default=None)
    state_updates: _StateUpdates = Field(default_factory=_StateUpdates)


# ============================================================
# Variant B — prose-string-where-object on list items
# ============================================================


class TestVariantB_ProseStrings:
    """LLM returned a List[ReasoningItem] field as a list of prose strings."""

    def test_strings_in_object_list_are_stripped(self):
        """All list items that are strings (not dicts) get removed."""
        content = {
            "agent_response": "OK",
            "reasoning": [
                "The root cause is X causing Y because Z",  # invalid: not an object
                "Therefore the mitigation should be ...",  # invalid: not an object
                {"observation": "logs", "inference": "ok", "confidence": 0.9},  # valid
            ],
        }
        result, drops = tolerant_validate(content, _Response)
        assert len(result.reasoning) == 1
        assert result.reasoning[0].observation == "logs"
        # Two strings were stripped
        assert len(drops) == 2
        assert all(d["action"] == "removed_list_item" for d in drops)

    def test_all_invalid_items_strip_to_empty_list(self):
        """When every list item is invalid, the list ends up empty."""
        content = {
            "agent_response": "OK",
            "reasoning": ["bad1", "bad2", "bad3"],
        }
        result, drops = tolerant_validate(content, _Response)
        assert result.reasoning == []
        assert len(drops) == 3


# ============================================================
# Variant C — key:value fragments as standalone array elements
# ============================================================


class TestVariantC_KeyValueFragments:
    """LLM emitted object fields as bare key:value strings (missing wrapping {})."""

    def test_kvp_fragments_get_stripped(self):
        """Fragment-shaped strings get treated as invalid list items and removed."""
        # Run 17 T7 pattern: object fields scattered as siblings of the parent array
        content = {
            "agent_response": "OK",
            "follow_ups": [
                {"label": "valid first", "payload": "do this"},
                'label":"Delete crashlooping pods',  # fragment
                'payload":"kubectl delete ...',  # fragment
                'action_type":"COOPERATIVE',  # fragment
            ],
        }
        result, drops = tolerant_validate(content, _Response)
        assert len(result.follow_ups) == 1
        assert result.follow_ups[0].label == "valid first"
        # Three fragments stripped
        assert len(drops) == 3


# ============================================================
# Variant D — cross-field validator failure on list item
# ============================================================


class TestVariantD_CrossFieldValidation:
    """LLM omitted source_file_id on non-USER_DESCRIPTION evidence."""

    def test_invalid_evidence_item_stripped_keeps_valid_ones(self):
        """The evidence item violating the cross-field rule is removed; others kept."""
        content = {
            "agent_response": "OK",
            "state_updates": {
                "evidence_to_add": [
                    {
                        "summary": "valid evidence",
                        "source_type": "logs",
                        "source_file_id": "file_abc123",
                    },
                    {
                        # Run 18 T8 pattern: source_type=logs but source_file_id omitted
                        "summary": "invalid evidence — missing source_file_id",
                        "source_type": "logs",
                    },
                    {
                        "summary": "valid user-description (no file_id needed)",
                        "source_type": "user_description",
                    },
                ]
            },
        }
        result, drops = tolerant_validate(content, _Response)
        assert len(result.state_updates.evidence_to_add) == 2
        assert result.state_updates.evidence_to_add[0].summary == "valid evidence"
        assert "user-description" in result.state_updates.evidence_to_add[1].summary
        assert len(drops) == 1
        # The stripped one was a list item (the @model_validator error fires
        # at the object level, loc ends in the int index)
        assert drops[0]["action"] == "removed_list_item"


# ============================================================
# Variant E — null where typed dict expected
# ============================================================


class TestVariantE_NullForTypedDict:
    """LLM returned null for a Dict[str, Any] field that has default_factory=dict."""

    def test_null_dict_field_replaced_with_default(self):
        """A null value for a dict-typed field gets replaced with the declared default."""
        content = {
            "agent_response": "OK",
            "state_updates": {
                "evidence_to_add": [],
                "hypotheses_to_update": None,  # Run 18 T13 pattern
            },
        }
        result, drops = tolerant_validate(content, _Response)
        assert result.state_updates.hypotheses_to_update == {}
        assert len(drops) == 1
        assert drops[0]["action"] == "defaulted_dict_field"
        assert "hypotheses_to_update" in drops[0]["loc"]

    def test_null_optional_list_field_replaced_with_default(self):
        """Null on Optional[List[X]] = Field(default=None) becomes None."""
        content = {
            "agent_response": "OK",
            "follow_ups": "this is a string not a list",  # invalid type
        }
        result, drops = tolerant_validate(content, _Response)
        # follow_ups has default=None — so it gets defaulted to None
        assert result.follow_ups is None
        assert len(drops) == 1
        assert drops[0]["action"] == "defaulted_dict_field"


# ============================================================
# Critical fields — required + no default → hard fail
# ============================================================


class TestCriticalFieldHardFail:
    """Required fields without defaults must NOT be stripped — they hard-fail."""

    def test_missing_required_field_re_raises_validation_error(self):
        """Omitting agent_response (required, no default) hard-fails."""
        from pydantic import ValidationError

        content = {
            "reasoning": [],  # missing agent_response
        }
        with pytest.raises(ValidationError) as exc_info:
            tolerant_validate(content, _Response)
        # Error mentions the actual missing field
        assert "agent_response" in str(exc_info.value)

    def test_invalid_required_field_re_raises_validation_error(self):
        """Wrong type on agent_response (required) hard-fails — can't strip a required field."""
        from pydantic import ValidationError

        content = {"agent_response": 12345}  # wrong type, but required
        with pytest.raises(ValidationError):
            tolerant_validate(content, _Response)


# ============================================================
# Mixed: some strippable + some not
# ============================================================


class TestMixedStrippableAndNot:
    """Behavior preservation: required-field failures don't get masked by
    strippable-field successes — once a required field fails, hard-fail."""

    def test_strippable_plus_required_failure_re_raises(self):
        """One strippable failure + one required-field failure → hard fail (the required-field error)."""
        from pydantic import ValidationError

        content = {
            # agent_response missing (required) — should hard fail
            "reasoning": ["invalid prose"],  # strippable
        }
        with pytest.raises(ValidationError) as exc_info:
            tolerant_validate(content, _Response)
        # The hard fail mentions agent_response, even though we
        # successfully stripped the bad reasoning item along the way.
        assert "agent_response" in str(exc_info.value)


# ============================================================
# Clean input — passes through unchanged
# ============================================================


class TestPassthrough:
    """Valid input should validate normally with no drops."""

    def test_fully_valid_passes_with_no_drops(self):
        content = {
            "agent_response": "hello",
            "reasoning": [
                {"observation": "x", "inference": "y", "confidence": 0.5},
            ],
            "follow_ups": [{"label": "L", "payload": "P"}],
            "state_updates": {
                "evidence_to_add": [],
                "hypotheses_to_update": {},
            },
        }
        result, drops = tolerant_validate(content, _Response)
        assert result.agent_response == "hello"
        assert len(result.reasoning) == 1
        assert drops == []

    def test_minimal_valid_input_passes(self):
        """Only the required field — everything else defaults."""
        content = {"agent_response": "hello"}
        result, drops = tolerant_validate(content, _Response)
        assert result.agent_response == "hello"
        assert result.reasoning == []
        assert result.follow_ups is None
        assert drops == []


# ============================================================
# Observability — drop records contain useful info
# ============================================================


class TestDropRecords:
    """Drop records must carry enough info to be useful for tuning."""

    def test_drop_record_includes_loc_error_type_and_action(self):
        content = {
            "agent_response": "OK",
            "reasoning": ["prose"],
        }
        _, drops = tolerant_validate(content, _Response)
        assert len(drops) == 1
        d = drops[0]
        assert d["loc"] == "reasoning.0"
        assert d["error_type"] == "model_type"
        assert d["action"] == "removed_list_item"
        assert "msg" in d
