"""What ``evidence_trail`` asks the model for matches what the engine reads (fm#1769).

``evidence_analyzed`` holds turn references (``turn_N``): the engine reads
nothing else from it (``validate_milestone_claims``), and both prompt blocks say
so. The schema the model receives described it as "Evidence IDs that were
considered in this turn", so on every structured-output turn the schema
contradicted the prompt, and a milestone backed only by an earlier turn's
evidence, cited by ID, was reverted.

Two prompt lines asked for output no field holds: the RCA diagnosis block's
"Note it in your reasoning / journal" (the journal is ``journal_entries``), and
the FaultMaven-self block's "keep evidence_trail to one line" (an object, and
absent from an INQUIRY turn's output).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Iterator

import pytest

from faultmaven.core.investigation.evidence_processor import validate_milestone_claims
from faultmaven.core.investigation.prompts.templates.assembly import get_prompt_for_case
from faultmaven.core.investigation.schemas import EvidenceTrail, get_schema_for_stage
from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    InquiryData,
    InvestigationStage,
)
from faultmaven.utils.schema_converter import (
    pydantic_to_openai_tools,
    pydantic_to_strict_openai_tools,
)
from tests.case_builders import investigating_case, make_case
from tests.unit.core.investigation.given_tools import DEFAULT_GIVEN

pytestmark = pytest.mark.unit

#: The phrases that named no output field, or the wrong shape for one.
RETIRED_PHRASES = ("your reasoning", "to one line")

#: The schema tool the provider receives: unenforced, or strict (tool_messages).
CONVERTERS = (pydantic_to_openai_tools, pydantic_to_strict_openai_tools)


def _flat(text: str) -> str:
    """Whitespace collapsed, so no assertion depends on line wrapping."""
    return " ".join(text.split())


def _tool_descriptions(node: Any) -> Iterator[str]:
    """Every description the tool schema gives an ``evidence_analyzed`` property."""
    if isinstance(node, dict):
        prop = node.get("properties", {}).get("evidence_analyzed")
        if isinstance(prop, dict):
            yield prop["description"]
        for value in node.values():
            yield from _tool_descriptions(value)
    elif isinstance(node, list):
        for value in node:
            yield from _tool_descriptions(value)


def _description() -> str:
    return EvidenceTrail.model_json_schema()["properties"]["evidence_analyzed"][
        "description"
    ]


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


def _inquiry_case() -> Case:
    return make_case(
        case_id="case_bbbbbbbb1769",
        title="Latency regression",
        user_id="user_123",
        enterprise_id="org_123",
        description="p95 went from 200ms to 4s after the deploy",
        state=CaseState.INQUIRY,
        inquiry=InquiryData(proposed_problem_statement="p95 latency regression"),
        current_turn=1,
    )


class TestSchemaAgreesWithParser:
    @pytest.mark.parametrize("converter", CONVERTERS, ids=lambda c: c.__name__)
    @pytest.mark.parametrize(
        "stage", [*InvestigationStage, None], ids=lambda s: s.value if s else "general"
    )
    def test_provider_tool_describes_turn_references(self, converter, stage):
        descriptions = list(_tool_descriptions(converter(get_schema_for_stage(stage))))
        assert descriptions, "the schema tool carries no evidence_analyzed"
        for description in descriptions:
            assert _flat(description).lower().startswith("turn references")
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
    def test_rca_diagnosis_block_records_the_trigger_in_the_journal(self):
        prompt = _flat(
            get_prompt_for_case(
                _case(evidence_turn=1, current_turn=3),
                "the deploy went out at 14:28",
                given_tools=DEFAULT_GIVEN,
            )
        )
        assert "Record it as a journal entry (journal_entries)" in prompt
        for phrase in RETIRED_PHRASES:
            assert phrase not in prompt

    @pytest.mark.parametrize("state", ["investigating", "inquiry"])
    def test_faultmaven_self_block_holds_on_every_turn_it_renders(self, state):
        """The block renders in INVESTIGATING and INQUIRY; an INQUIRY output has
        no evidence_trail, and a mixed message's other part may need one."""
        case = (
            _case(evidence_turn=1, current_turn=3)
            if state == "investigating"
            else _inquiry_case()
        )
        prompt = _flat(
            get_prompt_for_case(
                case,
                "what model are you running on?",
                processing_mode="agent_meta",
                given_tools=DEFAULT_GIVEN,
            )
        )
        assert (
            "nothing to evidence_trail (where your output has one)" in prompt
        ), "the FaultMaven-self block did not render"
        for phrase in (*RETIRED_PHRASES, "leave evidence_trail empty"):
            assert phrase not in prompt
