"""A system notice reaches the next prompt that is BUILT (#1688).

``system_feedback`` is the engine's correction to the model: a stripped
milestone, a rejected stage-gate signal, a withdrawn solution offer. It is
written onto the turn record and the next prompt reads it positionally, from
``turn_history[-1]``. So it arrives only if

- every turn that builds NO prompt carries it forward onto its own record, and
- every prompt that can be built renders it, the minimal fallback included.

Neither held. The pending-gate branches recorded ``None`` over it — five of them
leave the case open — and the three routes into the fallback prompt dropped it.
The service's consumed-turn backstop already forwarded it (#1267); the engine's
deterministic branches were a second copy of that record without the rule.

The per-branch table is ENUMERATED here by hand rather than derived from the
engine's call sites: a table computed from the code under test would lose its
own row the moment a branch stopped forwarding.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.milestone_engine import (
    TOKEN_LIMIT,
    MilestoneEngine,
    MilestoneEngineError,
)
from faultmaven.core.investigation.prompts import templates
from faultmaven.core.investigation.prompts.templates import (
    _FALLBACK_FENCE_RULE_HEAD,
    get_fallback_prompt_for_case,
    get_prompt_for_case,
)
from faultmaven.core.investigation.terminal_transitions import propose_transition
from faultmaven.infrastructure.llm.structured_output_capability import (
    StructuredOutputCapability,
    StructuredOutputMode,
    StructuredOutputStrategy,
)
from faultmaven.models.interfaces import ILLMProvider
from faultmaven.modules.agent.domain.services.investigation_service import (
    _backfill_consumed_turn,
)
from faultmaven.modules.case.domain.models import (
    Case,
    CaseState,
    ConfidenceLevel,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    InquiryData,
    InvestigationProgress,
    ProblemVerification,
    RootCauseConclusion,
    Solution,
    SolutionType,
    TurnOutcome,
    TurnProgress,
)
from faultmaven.utils.model_context import resolve_model_budget

pytestmark = pytest.mark.unit

NOTICE = "REASONING VALIDATION: probe-1688 — root_cause_identified was NOT recorded"
SUBSTANTIVE = "what does the etcd member log show around the time of the alerts?"
DROPDOWN_CLOSE = {
    "intent_type": "status_transition",
    "intent_data": {"to_state": "closed"},
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _SeamReached(Exception):
    """Raised by the patched LLM seam: the turn built a prompt."""


def _engine() -> MilestoneEngine:
    repo = MagicMock()
    repo.save = AsyncMock(side_effect=lambda c: c)
    repo.get = AsyncMock(side_effect=lambda cid: None)
    engine = MilestoneEngine(MagicMock(), repo, investigation_tools=MagicMock())
    engine._generate_structured_output = AsyncMock(side_effect=_SeamReached())
    return engine


def _record(turn: int, feedback: str | None = None) -> TurnProgress:
    return TurnProgress(
        turn_number=turn,
        timestamp=datetime.now(UTC),
        progress_made=False,
        outcome=TurnOutcome.CONVERSATION,
        user_message_summary=f"user {turn}",
        agent_response_summary=f"agent {turn}",
        system_feedback=feedback,
    )


def _with_notice(case: Case) -> Case:
    """Three recorded turns, the last carrying the notice; turn 4 is next."""
    case.turn_history = [_record(1), _record(2), _record(3, NOTICE)]
    case.current_turn = 3
    return case


def _inquiry_case() -> Case:
    return Case(
        case_id="case_1688aaaaaaaa",
        title="Feedback delivery",
        state=CaseState.INQUIRY,
        user_id="user_test",
        enterprise_id="org_test",
        description="etcdInsufficientMembers alerts",
    )


def _investigating_case() -> Case:
    case = _inquiry_case()
    case.problem_verification = ProblemVerification(
        symptom_statement="recurring etcdInsufficientMembers alerts",
        severity="HIGH",
        temporal_state="ongoing",
        urgency_level="high",
    )
    case.inquiry.proposed_problem_statement = "etcd connectivity"
    case.inquiry.problem_statement_confirmed = True
    case.inquiry.problem_statement_confirmed_at = datetime.now(UTC)
    case.state = CaseState.INVESTIGATING
    case.progress = InvestigationProgress()
    return case


def _closed(case: Case) -> Case:
    # All at once: assigning any one alone fails the terminal-state validator,
    # which runs on every assignment.
    return case.model_copy(
        update={
            "state": CaseState.CLOSED,
            "closed_at": datetime.now(UTC),
            "closure_reason": "solution_deferred",
        }
    )


def _pending_close(case: Case) -> Case:
    case.pending_transition = {
        "to_state": "closed",
        "summary": "You can **close** the case instead.",
        "evidence_ids": [],
        "proposed_at": datetime.now(UTC).isoformat(),
    }
    return case


def _resolution_ready_case() -> Case:
    """Carries a root cause, a fix and a qualifying causal-absence row, the
    only shape on which a CLOSE request pivots to a RESOLVE offer."""
    case = _investigating_case()
    case.progress.symptom_verified = True
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause="etcd peer certificate expired on member 2",
        mechanism="Expired peer cert drops the member from the quorum.",
        confidence_level=ConfidenceLevel.CONFIDENT,
        likelihood=0.85,
    )
    case.solutions = [
        Solution(
            solution_type=SolutionType.CONFIG_CHANGE,
            title="Rotate the etcd peer certificate",
            longterm_fix="Automate peer-cert rotation before expiry.",
        )
    ]
    case.evidence.append(
        Evidence(
            category=EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
            primary_purpose="confirm the cause was eliminated",
            summary="After the cert rotation the member rejoined and the alerts stopped.",
            source_type=EvidenceSourceType.USER_DESCRIPTION,
            collected_by="user",
            collected_at_turn=1,
        )
    )
    return case


async def _turn(engine: MilestoneEngine, case: Case, message: str, **intent) -> bool:
    """One service-shaped turn (the service advances the counter first).

    Returns whether the turn reached the LLM seam, i.e. built a prompt.
    """
    case.current_turn += 1
    seam_calls = engine._generate_structured_output.call_count
    try:
        await engine.process_turn(case=case, user_message=message, **intent)
    except MilestoneEngineError:
        # The engine wraps a turn error; the seam's own count says whether
        # this was the sentinel rather than an earlier failure.
        pass
    return engine._generate_structured_output.call_count > seam_calls


# ---------------------------------------------------------------------------
# Path 1: a turn that builds no prompt passes the notice on
# ---------------------------------------------------------------------------

#: Every case-open deterministic branch, each with the gate turns that reach it
#: from a case carrying the notice. Written out, not derived: see the module
#: docstring.
_CASE_OPEN_BRANCHES = {
    "re-present of a pending close": (
        lambda: _pending_close(_investigating_case()),
        [("hmm", {})],
    ),
    "bare decline of a pending close": (
        lambda: _pending_close(_investigating_case()),
        [("no", {})],
    ),
    "dropdown close, then decline": (
        _investigating_case,
        [("", DROPDOWN_CLOSE), ("no", {})],
    ),
    "dropdown close pivoting to resolve, then decline": (
        _resolution_ready_case,
        [("", DROPDOWN_CLOSE), ("no", {})],
    ),
    "confirmed close pivoting to resolve (INV-37), then decline": (
        lambda: _pending_close(_resolution_ready_case()),
        [("yes", {}), ("no", {})],
    ),
    "INQUIRY dropdown close, then decline": (
        _inquiry_case,
        [("", DROPDOWN_CLOSE), ("no", {})],
    ),
}


class TestAPromptlessTurnPassesTheNoticeOn:
    @pytest.mark.asyncio
    async def test_control_the_next_prompt_renders_the_notice(self):
        """Anti-vacuity: with nothing in between, the seam sees the notice."""
        engine = _engine()
        case = _with_notice(_investigating_case())
        assert await _turn(engine, case, SUBSTANTIVE)
        assert NOTICE in engine._generate_structured_output.call_args[0][0]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("branch", sorted(_CASE_OPEN_BRANCHES))
    async def test_the_next_llm_turn_still_sees_the_notice(self, branch):
        build, gate_turns = _CASE_OPEN_BRANCHES[branch]
        engine = _engine()
        case = _with_notice(build())

        for message, intent in gate_turns:
            # The scenario is only about deterministic turns if they are.
            assert not await _turn(engine, case, message, **intent), message
            assert case.turn_history[-1].system_feedback == NOTICE, message
        assert not case.is_terminal
        assert engine._generate_structured_output.call_count == 0

        assert await _turn(engine, case, SUBSTANTIVE)
        assert NOTICE in engine._generate_structured_output.call_args[0][0]

    @pytest.mark.asyncio
    async def test_a_terminal_case_does_not_carry_the_notice(self):
        """Nothing renders feedback once the case is closed, so the confirming
        turn's record does not copy the dead notice forward."""
        engine = _engine()
        case = _with_notice(_investigating_case())
        # The engine's own proposal, so the confirm executes a real close.
        propose_transition(case=case, to_state="closed", summary="Close it?")

        assert not await _turn(engine, case, "yes")
        assert case.state == CaseState.CLOSED
        assert case.turn_history[-1].system_feedback is None

    def test_the_service_backstop_follows_the_same_rule(self):
        """The service's record for a turn that never reached the engine is
        built by the same function, terminal rule included."""
        open_case = _with_notice(_investigating_case())
        open_case.current_turn += 1
        _backfill_consumed_turn(
            open_case, user_message="hi", agent_response="Hello!", metadata={}
        )
        assert open_case.turn_history[-1].system_feedback == NOTICE

        closed = _closed(_with_notice(_investigating_case()))
        closed.current_turn += 1
        _backfill_consumed_turn(
            closed, user_message="thanks", agent_response="Glad to help.", metadata={}
        )
        assert closed.turn_history[-1].system_feedback is None


# ---------------------------------------------------------------------------
# End to end: a real notice, a gate turn, the next LLM turn, consumption
# ---------------------------------------------------------------------------


class _StubLLM(ILLMProvider):
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
                "json_schema": {"name": "Test", "strict": True, "schema": schema},
            },
        )


_STRIPPED = "Milestones ['symptom_verified'] were NOT recorded"


def _llm_reply(**state_updates) -> str:
    return json.dumps(
        {
            "agent_response": "Let's look at the unit file.",
            "internal_reasoning": {
                "evidence_analyzed": [],
                "conclusions": [],
                "milestone_justifications": {},
            },
            "state_updates": {"outcome": "conversation", **state_updates},
        }
    )


class TestTheNoticeIsDeliveredOnceAcrossAGate:
    @pytest.mark.asyncio
    async def test_strip_then_gate_then_llm_turn(self):
        llm = _StubLLM()
        llm.generate = AsyncMock()
        repo = MagicMock()
        repo.save = AsyncMock(side_effect=lambda c: c)
        repo.get = AsyncMock()
        engine = MilestoneEngine(llm, repo, investigation_tools=MagicMock())
        case = _investigating_case()
        case.evidence.append(
            Evidence(
                summary="etcd member 2 logs x509: certificate has expired",
                category=EvidenceCategory.SYMPTOM_EVIDENCE,
                source_type=EvidenceSourceType.USER_DESCRIPTION,
                collected_by="user_test",
                primary_purpose="Symptom verification",
                collected_at_turn=1,
            )
        )

        # Turn 1: an unjustified milestone claim is stripped, and the engine
        # records why.
        case.current_turn = 1
        llm.generate.return_value = _llm_reply(
            milestones={"symptom_verified": True}, outcome="milestone_completed"
        )
        case = (await engine.process_turn(case, "it still fails"))["case_updated"]
        assert _STRIPPED in (case.turn_history[-1].system_feedback or "")

        # Turn 2: the user opens a close from the dropdown. No prompt is built.
        llm.generate.reset_mock()
        case.current_turn = 2
        case = (await engine.process_turn(case, "", **DROPDOWN_CLOSE))["case_updated"]
        assert llm.generate.call_count == 0
        assert case.pending_transition

        # Turn 3: the user declines with a question, which is processed as a
        # normal turn. Its prompt carries the notice from turn 1.
        case.current_turn = 3
        llm.generate.return_value = _llm_reply()
        case = (await engine.process_turn(case, "no — what should I check next?"))[
            "case_updated"
        ]
        prompts = [c.kwargs["prompt"] for c in llm.generate.call_args_list]
        assert prompts and any(_STRIPPED in p for p in prompts)

        # Delivered, therefore consumed: turn 3's record carries only what
        # turn 3 produced, so turn 4's prompt does not repeat the notice.
        assert _STRIPPED not in (case.turn_history[-1].system_feedback or "")


# ---------------------------------------------------------------------------
# Path 2: every route into the minimal fallback renders the notice
# ---------------------------------------------------------------------------


def _fallback_case(state: CaseState) -> Case:
    case = _investigating_case() if state != CaseState.INQUIRY else _inquiry_case()
    case = _with_notice(case)
    case.current_turn = 4
    return _closed(case) if state == CaseState.CLOSED else case


class TestTheFallbackRendersTheNotice:
    @pytest.mark.parametrize("state", [CaseState.INQUIRY, CaseState.INVESTIGATING])
    def test_the_fallback_body_renders_it(self, state):
        prompt = get_fallback_prompt_for_case(_fallback_case(state), SUBSTANTIVE)
        assert NOTICE in prompt
        # Directly above the user's message, as on the main prompt.
        assert prompt.index(NOTICE) < prompt.index("USER:")

    def test_the_terminal_fallback_does_not(self):
        """Consistent with the main TERMINAL prompt, which has no slot."""
        prompt = get_fallback_prompt_for_case(
            _fallback_case(CaseState.CLOSED), SUBSTANTIVE
        )
        assert NOTICE not in prompt

    def test_it_is_guarded_not_fenced(self):
        """Fenced content is, by the fallback's own rule, not an instruction
        to the model; the notice is. So it renders as renderer text, with no
        element around it and no mention in the rule's block list."""
        prompt = get_fallback_prompt_for_case(
            _fallback_case(CaseState.INVESTIGATING), SUBSTANTIVE
        )
        assert "<system_feedback" not in prompt
        rule = prompt[prompt.index(_FALLBACK_FENCE_RULE_HEAD) : prompt.index("FENCE:")]
        assert "system_feedback" not in rule

    def test_the_starvation_arm(self, monkeypatch):
        calls = []
        real = templates.get_fallback_prompt_for_case
        monkeypatch.setattr(
            templates,
            "get_fallback_prompt_for_case",
            lambda *a: calls.append(a) or real(*a),
        )
        prompt = get_prompt_for_case(
            _fallback_case(CaseState.INVESTIGATING), SUBSTANTIVE, target_tokens=1200
        )
        assert calls, "the cap must force the starvation fallback"
        assert NOTICE in prompt

    def test_the_hard_ceiling_arm(self):
        """Room for variable content, but a render that never fits the hard
        ceiling: the backstop's second arm returns the fallback."""
        case = _fallback_case(CaseState.INVESTIGATING)
        resolved = dataclasses.replace(
            resolve_model_budget(None, None), prompt_target=50_000, prompt_budget=10_000
        )
        prompt = templates._assemble_allocated(
            case,
            lambda budget: {},
            lambda ctx: "x" * 20_000,
            resolved,
            0,
            10,
            len,
            SUBSTANTIVE,
            None,
            None,
        )
        assert prompt.startswith("You are FaultMaven investigating an issue.")
        assert NOTICE in prompt

    @pytest.mark.asyncio
    async def test_the_runtime_provider_rejection(self):
        """The provider rejects the prompt as too long; the one retry carries
        the notice. The fallback builder is deliberately NOT patched here."""
        engine = MilestoneEngine(MagicMock(), MagicMock(), investigation_tools=None)
        overflow = RuntimeError("prompt is too long")
        overflow.error_code = TOKEN_LIMIT
        prompts: list[str] = []

        async def inner(prompt, schema_model, **kwargs):
            prompts.append(prompt)
            if len(prompts) == 1:
                raise overflow
            return MagicMock()

        engine._generate_structured_output_inner = inner
        await engine._generate_structured_output(
            "the full prompt",
            MagicMock(),
            case=_fallback_case(CaseState.INVESTIGATING),
            user_message=SUBSTANTIVE,
        )
        assert len(prompts) == 2
        assert NOTICE in prompts[1]
