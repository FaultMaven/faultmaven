"""Tests for the evidence-grounding validator.

Item 5 in the 2026-05-20 investigation-pipeline-followups handoff:
code-side safety net for the hallucinated-evidence class. The
validator scans ``agent_response`` for ``ev_*`` IDs and verifies each
exists on the case.

These tests pin:
  - Pure ``find_evidence_id_references`` token extraction.
  - The three-way classification in ``validate_evidence_grounding``
    (clean / ungrounded / cited-but-real).
  - Word-boundary anchoring so substrings of similar-shape tokens
    don't false-positive.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from faultmaven.core.investigation.evidence_grounding_validator import (
    find_evidence_id_references,
    validate_evidence_grounding,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseStatus,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    InquiryData,
    ProblemVerification,
)


def _make_evidence(ev_id: str) -> Evidence:
    return Evidence(
        evidence_id=ev_id,
        summary="test",
        content_ref="test.log",
        category=EvidenceCategory.SYMPTOM_EVIDENCE,
        source_type=EvidenceSourceType.USER_DESCRIPTION,
        collected_at=datetime.now(UTC),
        collected_by="user_test",
        primary_purpose="testing",
        preprocessed_content="content",
        content_size_bytes=100,
        preprocessing_method="manual",
        source_file_id=None,
        collected_at_turn=1,
    )


def _make_case(evidence_ids: list[str]) -> Case:
    case = Case(
        case_id="case_abcdef012345",
        title="Test",
        status=CaseStatus.INVESTIGATING,
        user_id="user_test",
        organization_id="org_test",
        description="Test case",
        problem_verification=ProblemVerification(
            symptom_statement="Test symptom",
            severity="HIGH",
            temporal_state="ongoing",
            urgency_level="high",
        ),
        inquiry=InquiryData(
            problem_statement_confirmed=True,
            decided_to_investigate=True,
            thread_id="thread_test",
            proposed_problem_statement="Test symptom",
        ),
    )
    for ev_id in evidence_ids:
        case.evidence.append(_make_evidence(ev_id))
    return case


@pytest.mark.unit
class TestFindEvidenceIdReferences:
    """Pure-function token extraction tests."""

    def test_empty_string_returns_empty(self):
        assert find_evidence_id_references("") == []

    def test_no_evidence_ids_returns_empty(self):
        text = "The nginx error log shows 47 auth failures starting at 14:02 UTC."
        assert find_evidence_id_references(text) == []

    def test_single_id_extracted(self):
        text = "See ev_001122334455 for the failure window."
        assert find_evidence_id_references(text) == ["ev_001122334455"]

    def test_multiple_ids_extracted(self):
        text = (
            "Cross-referencing ev_aaaabbbbcccc with ev_dddd11112222 shows "
            "the same pattern."
        )
        assert sorted(find_evidence_id_references(text)) == sorted(
            ["ev_aaaabbbbcccc", "ev_dddd11112222"]
        )

    def test_duplicate_id_extracted_each_time(self):
        text = "Refer to ev_abcdef012345 — also ev_abcdef012345 confirms this."
        # Findall returns all occurrences; the validator dedupes downstream.
        assert find_evidence_id_references(text) == [
            "ev_abcdef012345",
            "ev_abcdef012345",
        ]

    def test_uppercase_hex_not_matched(self):
        # Evidence IDs are lowercase hex per the contract pattern.
        text = "See ev_AABBCCDDEEFF for the failure."
        assert find_evidence_id_references(text) == []

    def test_wrong_length_not_matched(self):
        # 11 chars (one too short) and 13 chars (one too long) — neither matches.
        text = "Bad: ev_001122334 4 (short) ev_001122334455X (long)"
        assert find_evidence_id_references(text) == []

    def test_substring_of_longer_hex_not_matched(self):
        # Word-boundary anchoring: an ev_ prefix sitting inside a longer
        # hex token should NOT match (e.g., a checksum that happens to
        # contain "ev_001122334455somethingelse").
        text = "Hash: prev_001122334455postfix is not an evidence ID."
        assert find_evidence_id_references(text) == []

    def test_id_at_start_and_end_of_string(self):
        assert find_evidence_id_references("ev_111122223333") == ["ev_111122223333"]
        assert find_evidence_id_references("see ev_111122223333") == ["ev_111122223333"]


@pytest.mark.unit
class TestValidateEvidenceGrounding:
    """Three-way classification tests."""

    def test_clean_response_no_ids(self):
        case = _make_case(["ev_001122334455"])
        text = "The nginx error log shows 47 auth failures starting at 14:02 UTC."
        is_clean, ungrounded, cited = validate_evidence_grounding(case, text)
        assert is_clean is True
        assert ungrounded == []
        assert cited == []

    def test_cited_real_id_flagged_as_compliance_break(self):
        """Real ev_* IDs should not appear in prose (prompt rule says
        use the label instead). Flag as cited-but-real — not
        fabrication, but still a violation."""
        case = _make_case(["ev_001122334455"])
        text = "The evidence ev_001122334455 confirms the failure."
        is_clean, ungrounded, cited = validate_evidence_grounding(case, text)
        assert is_clean is False
        assert ungrounded == []
        assert cited == ["ev_001122334455"]

    def test_ungrounded_id_flagged_as_fabrication(self):
        """ev_* IDs that don't exist on the case are the
        hallucination shape Run 6 surfaced — the LLM invented an ID."""
        case = _make_case(["ev_001122334455"])
        text = "See ev_ffffffffffff for the OOMKill event."
        is_clean, ungrounded, cited = validate_evidence_grounding(case, text)
        assert is_clean is False
        assert ungrounded == ["ev_ffffffffffff"]
        assert cited == []

    def test_mix_of_cited_and_ungrounded(self):
        case = _make_case(["ev_001122334455", "ev_aaaabbbbcccc"])
        text = (
            "Cross-referencing ev_001122334455 with ev_ffffffffffff and "
            "ev_aaaabbbbcccc shows the OOM pattern."
        )
        is_clean, ungrounded, cited = validate_evidence_grounding(case, text)
        assert is_clean is False
        assert ungrounded == ["ev_ffffffffffff"]
        assert cited == sorted(["ev_001122334455", "ev_aaaabbbbcccc"])

    def test_duplicate_ids_deduped_in_output(self):
        """Repeated mentions of the same ID dedup to one entry per
        bucket — counting repetition isn't useful for the warning."""
        case = _make_case(["ev_001122334455"])
        text = (
            "Look at ev_ffffffffffff — actually ev_ffffffffffff confirms it. "
            "And ev_001122334455 too."
        )
        is_clean, ungrounded, cited = validate_evidence_grounding(case, text)
        assert is_clean is False
        assert ungrounded == ["ev_ffffffffffff"]
        assert cited == ["ev_001122334455"]

    def test_empty_response_is_clean(self):
        case = _make_case(["ev_001122334455"])
        is_clean, ungrounded, cited = validate_evidence_grounding(case, "")
        assert is_clean is True
        assert ungrounded == []
        assert cited == []

    def test_case_with_no_evidence_treats_all_ids_as_ungrounded(self):
        """A case with no evidence rows means any ev_* in prose is
        definitionally fabricated."""
        case = _make_case([])
        text = "See ev_001122334455 for details."
        is_clean, ungrounded, cited = validate_evidence_grounding(case, text)
        assert is_clean is False
        assert ungrounded == ["ev_001122334455"]
        assert cited == []


@pytest.mark.unit
@pytest.mark.asyncio
class TestEvidenceGroundingEngineIntegration:
    """The validator must run inside ``_process_turn_impl`` and surface
    its findings in the returned metadata. Pins the wiring so a future
    refactor doesn't silently drop the check.
    """

    @staticmethod
    def _llm_response_with_prose(prose: str) -> str:
        import json

        return json.dumps(
            {
                "agent_response": prose,
                "state_updates": {"outcome": "conversation"},
            }
        )

    async def _build_engine_and_case(self, mock_llm_response: str):
        from unittest.mock import AsyncMock, MagicMock

        from faultmaven.core.investigation.milestone_engine import MilestoneEngine
        from faultmaven.infrastructure.llm.structured_output_capability import (
            StructuredOutputCapability,
            StructuredOutputMode,
            StructuredOutputStrategy,
        )
        from faultmaven.models.interfaces import ILLMProvider

        class _MockLLM(ILLMProvider):
            async def generate(self, prompt, **kwargs):
                return "{}"

            async def generate_stream(self, prompt, **kwargs):
                yield "mock"

            async def generate_with_history(self, messages, **kwargs):
                return "{}"

            def get_structured_output_strategy(self, schema):
                return StructuredOutputStrategy(
                    capability=StructuredOutputCapability.STRICT,
                    mode=StructuredOutputMode.JSON_SCHEMA_STRICT,
                    include_schema_in_prompt=False,
                    response_format={
                        "type": "json_schema",
                        "json_schema": {
                            "name": "TestSchema",
                            "strict": True,
                            "schema": schema,
                        },
                    },
                )

        llm = _MockLLM()
        llm.generate = AsyncMock(return_value=mock_llm_response)
        repo = MagicMock()
        repo.save = AsyncMock(side_effect=lambda c: c)
        repo.get = AsyncMock()
        engine = MilestoneEngine(
            llm,
            repo,
            investigation_tools=MagicMock(),
        )
        case = _make_case(["ev_001122334455"])
        return engine, case

    async def test_clean_response_yields_empty_lists_in_metadata(self):
        prose = "The nginx log shows OOM events starting at 14:02 UTC."
        engine, case = await self._build_engine_and_case(
            self._llm_response_with_prose(prose)
        )

        result = await engine.process_turn(case, "Investigate")

        assert result["metadata"]["ungrounded_evidence_ids"] == []
        assert result["metadata"]["cited_evidence_ids_in_prose"] == []

    async def test_ungrounded_id_surfaces_in_metadata(self):
        """Fabricated ev_* ID in prose must appear in
        metadata['ungrounded_evidence_ids']."""
        prose = "See ev_ffffffffffff for the OOMKill event."
        engine, case = await self._build_engine_and_case(
            self._llm_response_with_prose(prose)
        )

        result = await engine.process_turn(case, "Investigate")

        assert result["metadata"]["ungrounded_evidence_ids"] == ["ev_ffffffffffff"]
        assert result["metadata"]["cited_evidence_ids_in_prose"] == []

    async def test_real_id_in_prose_surfaces_as_compliance_break(self):
        """Real ev_* ID cited in prose (rule violation but not
        fabrication) goes in metadata['cited_evidence_ids_in_prose']."""
        prose = "The evidence ev_001122334455 confirms the failure."
        engine, case = await self._build_engine_and_case(
            self._llm_response_with_prose(prose)
        )

        result = await engine.process_turn(case, "Investigate")

        assert result["metadata"]["cited_evidence_ids_in_prose"] == ["ev_001122334455"]
        assert result["metadata"]["ungrounded_evidence_ids"] == []
