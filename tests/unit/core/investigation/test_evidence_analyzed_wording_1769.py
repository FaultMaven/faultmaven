"""What ``evidence_trail`` asks the model for matches what the engine reads (fm#1769).

``evidence_analyzed`` holds turn references (``turn_N``): the engine reads
nothing else from it (``validate_milestone_claims``), and both prompt blocks say
so. The schema the model receives described it as "Evidence IDs that were
considered in this turn", so on every structured-output turn the schema
contradicted the prompt, and a milestone backed only by an earlier turn's
evidence, cited by ID, was reverted.

Two prompt lines asked for output no field holds: the RCA diagnosis block's
"Note it in your reasoning / journal", and the FaultMaven-self block's "keep
evidence_trail to one line" (an object, not a line).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest

from faultmaven.core.investigation.evidence_processor import validate_milestone_claims
from faultmaven.core.investigation.prompts.templates.assembly import get_prompt_for_case
from faultmaven.core.investigation.schemas import EvidenceTrail
from faultmaven.modules.case.contracts import (
    Case,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    InvestigationStage,
)
from tests.case_builders import investigating_case
from tests.unit.core.investigation.given_tools import DEFAULT_GIVEN

pytestmark = pytest.mark.unit

#: The phrases that named no output field, or the wrong shape for one.
RETIRED_PHRASES = ("your reasoning", "to one line")


def _description() -> str:
    schema = EvidenceTrail.model_json_schema()
    return schema["properties"]["evidence_analyzed"]["description"]


def _case(evidence_turn: int, current_turn: int) -> Case:
    """INVESTIGATING in DIAGNOSIS; its one symptom row arrived on ``evidence_turn``."""
    case = investigating_case(
        case_id="case_aaaaaaaa1769",
        title="Latency regression",
        user_id="user_123",
        enterprise_id="org_123",
        description="p95 went from 200ms to 4s after the deploy",
        symptom="p95 latency 4s since the deploy",
        statement="p95 latency 4s since the deploy",
        current_stage=InvestigationStage.DIAGNOSIS,
        current_turn=current_turn,
    )
    case.evidence.append(
        Evidence(
            summary="checkout-api log: requests at 3.9-4.1s",
            category=EvidenceCategory.SYMPTOM_EVIDENCE,
            source_type=EvidenceSourceType.LOGS,
            source_file_id="file_0123456789ab",
            collected_at=datetime.now(UTC),
            collected_by="user_123",
            primary_purpose="Symptom verification",
            collected_at_turn=evidence_turn,
        )
    )
    return case


class TestSchemaAgreesWithParser:
    def test_description_names_turn_references_not_evidence_ids(self):
        description = _description()
        assert description.lower().startswith("turn references")
        assert "evidence ids" not in description.lower()

    def test_described_example_validates_a_milestone_from_that_turn(self):
        """The description's own example, sent as written, is what the parser
        reads: a symptom claim backed only by that turn's evidence stands."""
        example = re.search(r"turn_\d+", _description())
        assert example is not None, "the description gives no turn reference"
        evidence_turn = int(example.group().split("_")[1])
        case = _case(evidence_turn=evidence_turn, current_turn=evidence_turn + 2)

        cited = validate_milestone_claims(
            case,
            ["symptom_verified"],
            EvidenceTrail(evidence_analyzed=[example.group()]),
        )
        uncited = validate_milestone_claims(
            case, ["symptom_verified"], EvidenceTrail(evidence_analyzed=[])
        )
        ev_id = validate_milestone_claims(
            case,
            ["symptom_verified"],
            EvidenceTrail(evidence_analyzed=[case.evidence[0].evidence_id]),
        )

        assert cited[0].is_valid
        # Controls: without the citation, or with the evidence's ID in its
        # place, the earlier turn's evidence does not count.
        assert not uncited[0].is_valid
        assert not ev_id[0].is_valid


class TestPromptBlocksNameRealOutput:
    def test_rca_diagnosis_block_names_the_trigger_in_the_answer(self):
        prompt = get_prompt_for_case(
            _case(evidence_turn=1, current_turn=3),
            "the deploy went out at 14:28",
            given_tools=DEFAULT_GIVEN,
        )
        assert "Name it as the trigger in your answer" in prompt
        for phrase in RETIRED_PHRASES:
            assert phrase not in prompt

    def test_faultmaven_self_block_leaves_evidence_trail_empty(self):
        prompt = get_prompt_for_case(
            _case(evidence_turn=1, current_turn=3),
            "what model are you running on?",
            processing_mode="agent_meta",
            given_tools=DEFAULT_GIVEN,
        )
        assert "leave evidence_trail\n  empty" in prompt
        for phrase in RETIRED_PHRASES:
            assert phrase not in prompt
