"""#1328: a question about FaultMaven itself is answered about the assistant.

The reported turn ("what LLM model and provider are currently generating
these responses?") reached the engine as ``directed_analysis`` in
INVESTIGATING: tools were forced, the EVIDENCE GROUNDING block applied, and
the agent — unable to find FaultMaven's architecture in the case evidence —
asked the user for FaultMaven's own deployment manifests. Three seams close
that, and each is pinned here:

1. ``classify_query`` routes the question to ``agent_meta`` (its tests live
   with the classifier); ``get_prompt_for_case`` renders the self-knowledge
   block for that mode and waives the grounding / diagnostic-reasoning
   blocks, in INVESTIGATING and INQUIRY alike.
2. The shared advisor block carries a short self-reference rule on EVERY
   generation turn, so a phrasing the heuristic misses still gets an honest,
   high-level answer and never a request for FaultMaven's configuration.
3. The tool-loop system instruction has a Type D for the assistant itself,
   so its "when uncertain, search the evidence" default does not reach these
   questions; and the engine's routing predicates treat the mode like any
   other non-forced turn.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from faultmaven.core.investigation.milestone_engine import (
    MilestoneEngine,
    _route_toolless_turn_single_shot,
    _should_force_tools,
)
from faultmaven.core.investigation.prompts.templates import (
    _DIAGNOSTIC_REASONING_BLOCK,
    _EVIDENCE_GROUNDING_BLOCK,
    _FAULTMAVEN_DOCS_URL,
    _SELF_REFERENCE_RULE,
    AGENT_META_INSTRUCTIONS,
    KNOWLEDGE_QUERY_INSTRUCTIONS,
    get_prompt_for_case,
)
from faultmaven.modules.agent.domain.services.investigation_service import (
    _EVIDENCE_REROUTE_MODES,
)
from faultmaven.modules.agent.domain.services.query_classifier import (
    ProcessingMode,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    InquiryData,
    InvestigationStage,
    ProblemVerification,
    UploadedFile,
)

pytestmark = [pytest.mark.unit]

REPORTED = (
    "Curious about how you work under the hood though: what LLM model and "
    "provider are currently generating these responses?"
)

# Substrings that identify each block without overlapping the waiver
# sentence inside AGENT_META_INSTRUCTIONS (which NAMES the two blocks).
GROUNDING_MARKER = "EVIDENCE GROUNDING (CRITICAL - Anti-Hallucination)"
DIAGNOSTIC_MARKER = "When you make a diagnostic claim, propose an action"
ABOUT_MARKER = "ABOUT FAULTMAVEN (self-knowledge"

assert GROUNDING_MARKER in _EVIDENCE_GROUNDING_BLOCK
assert DIAGNOSTIC_MARKER in _DIAGNOSTIC_REASONING_BLOCK
assert GROUNDING_MARKER not in AGENT_META_INSTRUCTIONS
assert DIAGNOSTIC_MARKER not in AGENT_META_INSTRUCTIONS


def _case(state: CaseState) -> Case:
    investigating = state == CaseState.INVESTIGATING
    case = Case(
        case_id="case_aabb11223344",
        title="Nightly OOM kills",
        description="postgres is OOM-killed every night around 02:00",
        user_id="user_123",
        organization_id="org_123",
        state=state,
        current_stage=InvestigationStage.DIAGNOSIS if investigating else None,
        inquiry=InquiryData(
            problem_statement_confirmed=investigating,
            decided_to_investigate=investigating,
            proposed_problem_statement="Nightly OOM kills",
        ),
        current_turn=6,
    )
    if investigating:
        case.problem_verification = ProblemVerification(
            symptom_statement="postgres OOM-killed nightly", severity="HIGH"
        )
    return case


def _case_with_searchable_material() -> Case:
    case = _case(CaseState.INVESTIGATING)
    uf = UploadedFile(filename="dmesg.log", size_bytes=4096, uploaded_at_turn=2)
    uf.structural_index = "oom-killer x3"
    case.uploaded_files = [uf]
    case.evidence = [
        Evidence(
            evidence_id="ev_000000000001",
            summary="dmesg shows three OOM kills",
            category=EvidenceCategory.SYMPTOM_EVIDENCE,
            source_type=EvidenceSourceType.LOGS,
            source_file_id=uf.file_id,
            collected_at=datetime.now(UTC),
            collected_by="user_123",
            primary_purpose="symptom",
            collected_at_turn=2,
        )
    ]
    return case


def _render(state: CaseState, mode: str, case: Case | None = None) -> str:
    return get_prompt_for_case(
        case or _case(state),
        REPORTED,
        processing_mode=mode,
        tools_available=True,
    )


class TestPromptDispatch:
    def test_investigating_agent_meta_renders_profile_and_waives_grounding(self):
        prompt = _render(CaseState.INVESTIGATING, "agent_meta")
        assert ABOUT_MARKER in prompt
        assert prompt.count(ABOUT_MARKER) == 1
        assert GROUNDING_MARKER not in prompt
        assert DIAGNOSTIC_MARKER not in prompt
        # The stage instructions are replaced, not appended.
        assert "**FOCUS: QUESTION ABOUT FAULTMAVEN ITSELF**" in prompt
        assert KNOWLEDGE_QUERY_INSTRUCTIONS.strip() not in prompt

    def test_investigating_directed_analysis_is_unchanged_in_shape(self):
        prompt = _render(CaseState.INVESTIGATING, "directed_analysis")
        assert ABOUT_MARKER not in prompt
        assert GROUNDING_MARKER in prompt
        assert DIAGNOSTIC_MARKER in prompt

    def test_investigating_knowledge_query_does_not_get_the_profile(self):
        """The two waived modes stay distinct: KQ still searches kb_qa."""
        prompt = _render(CaseState.INVESTIGATING, "knowledge_query")
        assert ABOUT_MARKER not in prompt
        assert "**FOCUS: GENERAL KNOWLEDGE QUESTION**" in prompt

    def test_inquiry_agent_meta_renders_profile(self):
        prompt = _render(CaseState.INQUIRY, "agent_meta")
        assert ABOUT_MARKER in prompt
        assert prompt.count(ABOUT_MARKER) == 1

    @pytest.mark.parametrize("mode", ["directed_analysis", "triage", None])
    def test_inquiry_other_modes_pay_nothing_for_the_slot(self, mode):
        prompt = _render(CaseState.INQUIRY, mode)
        assert ABOUT_MARKER not in prompt
        assert "{agent_meta_instructions}" not in prompt

    def test_agent_meta_with_evidence_on_file_still_waives_grounding(self):
        """The reported case was at turn 6 with searchable evidence."""
        prompt = _render(
            CaseState.INVESTIGATING, "agent_meta", _case_with_searchable_material()
        )
        assert ABOUT_MARKER in prompt
        assert GROUNDING_MARKER not in prompt


class TestBackstopRule:
    """The short rule is paid on every generation turn, in both active states."""

    @pytest.mark.parametrize(
        "state, mode",
        [
            (CaseState.INVESTIGATING, "directed_analysis"),
            (CaseState.INVESTIGATING, "triage"),
            (CaseState.INVESTIGATING, "knowledge_query"),
            (CaseState.INQUIRY, "directed_analysis"),
            (CaseState.INQUIRY, None),
        ],
    )
    def test_rule_present_on_non_meta_turns(self, state, mode):
        prompt = _render(state, mode)
        assert "Questions about YOU" in prompt
        assert _FAULTMAVEN_DOCS_URL in prompt

    def test_rule_names_the_forbidden_move(self):
        flat = " ".join(_SELF_REFERENCE_RULE.split())
        assert "NEVER ask for FaultMaven's own configuration" in flat
        assert "never guess a vendor or model name" in flat

    def test_grounding_block_skip_list_names_self_reference(self):
        assert "questions about FaultMaven\nitself" in _EVIDENCE_GROUNDING_BLOCK


class TestSelfKnowledgeContent:
    """The profile is honest about what the model is NOT told."""

    def test_profile_does_not_name_a_vendor_or_model(self):
        lowered = AGENT_META_INSTRUCTIONS.lower()
        for vendor in ("gemini", "gpt", "claude", "anthropic", "openai", "llama"):
            assert vendor not in lowered, vendor

    def test_profile_says_the_model_is_not_told_its_provider(self):
        assert "NOT told which provider or model" in AGENT_META_INSTRUCTIONS

    def test_profile_points_at_the_docs(self):
        assert _FAULTMAVEN_DOCS_URL in AGENT_META_INSTRUCTIONS

    def test_answer_discipline_is_short_and_leaves_the_case_alone(self):
        assert "Three to six sentences" in AGENT_META_INSTRUCTIONS
        assert (
            "Do NOT call search_file, deep_analysis or kb_qa" in AGENT_META_INSTRUCTIONS
        )
        assert "Leave the investigation untouched" in AGENT_META_INSTRUCTIONS


class TestEngineRouting:
    def test_agent_meta_never_forces_tools(self):
        case = _case_with_searchable_material()
        assert _should_force_tools("directed_analysis", case, False) is True
        assert _should_force_tools("agent_meta", case, False) is False

    def test_agent_meta_follows_the_material_rule_for_single_shot(self):
        # Nothing to search → single-shot structured call (reasoning declared).
        assert (
            _route_toolless_turn_single_shot(
                "agent_meta", _case(CaseState.INVESTIGATING), False
            )
            is True
        )
        # Searchable material → the tool loop, tool_choice=auto, Type D applies.
        assert (
            _route_toolless_turn_single_shot(
                "agent_meta", _case_with_searchable_material(), False
            )
            is False
        )

    def test_tool_loop_instruction_has_a_type_for_the_assistant(self):
        instruction = MilestoneEngine._build_da_system_instruction(
            ["search_file", "deep_analysis", "kb_qa"], "submit_investigation_response"
        )
        assert "TYPE D — ABOUT FAULTMAVEN" in instruction
        assert "Do NOT search the evidence or the knowledge base" in instruction
        # The uncertainty default is scoped to the case/knowledge types.
        assert "When uncertain between Types A–C" in instruction

    def test_fresh_upload_still_reroutes_agent_meta_to_directed_analysis(self):
        """#708 composes: an upload delivered with a meta question is analysed."""
        assert ProcessingMode.AGENT_META in _EVIDENCE_REROUTE_MODES
        assert ProcessingMode.TRIAGE in _EVIDENCE_REROUTE_MODES
        assert ProcessingMode.KNOWLEDGE_QUERY in _EVIDENCE_REROUTE_MODES
