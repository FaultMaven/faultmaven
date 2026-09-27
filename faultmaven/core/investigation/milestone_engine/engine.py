"""MilestoneEngine: process_turn and the investigation-turn state machine."""

import asyncio
import logging
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any, Optional

# Module initialization
logger = logging.getLogger(__name__)


from faultmaven.core.investigation.case_telemetry import (
    TELEMETRY_HANDOFF_KEY,
    TurnPath,
    collect_progress_arms,
)
from faultmaven.core.investigation.evidence_need_linking import (
    link_evidence_suggestions_to_needs,
    suggestions_are_engine_replaced,
    sweep_silent_inferred_needs,
)
from faultmaven.core.investigation.hypothesis_manager import (
    create_hypothesis_manager,
)
from faultmaven.core.investigation.lifecycle_metrics import (
    engine_owned_affordance_served_total,
    evidence_suggestion_unlinked_total,
    gate1_statement_composed_total,
    narration_overclaim_total,
)
from faultmaven.core.investigation.llm_error_handler import (
    LLMErrorHandler,
)
from faultmaven.core.investigation.milestone_engine.dependencies import EngineDeps
from faultmaven.core.investigation.milestone_engine.errors import MilestoneEngineError
from faultmaven.core.investigation.milestone_engine.generation import (
    TOOLLESS_INFERENCE_OUTPUT_FLOOR,
    StructuredOutputGenerator,
)
from faultmaven.core.investigation.milestone_engine.hypothesis_updates import (
    _apply_hypothesis_action_intent,
)
from faultmaven.core.investigation.milestone_engine.kb_prefetch import KbPrefetcher
from faultmaven.core.investigation.milestone_engine.redaction import _should_redact
from faultmaven.core.investigation.milestone_engine.regeneration import (
    _remaining_regens_for,
)
from faultmaven.core.investigation.milestone_engine.response_application import (
    ResponseApplier,
)
from faultmaven.core.investigation.milestone_engine.runbook_creation import (
    RunbookCreator,
)
from faultmaven.core.investigation.milestone_engine.terminal_turns import (
    TerminalTurnHandler,
)
from faultmaven.core.investigation.milestone_engine.transition_consent import (
    _user_confirms_transition,
    _user_declines_transition,
)
from faultmaven.core.investigation.milestone_engine.transitions import TransitionManager
from faultmaven.core.investigation.milestone_engine.turn_records import (
    _create_turn_record,
    _finish_deterministic_turn,
    _flatten_follow_ups,
    _perform_hypothesis_housekeeping,
    _report_turn_uploads,
    _resolve_id_ref,
)
from faultmaven.core.investigation.milestone_engine.vectorization import (
    EvidenceVectorizer,
)
from faultmaven.core.investigation.progress_monitor import ProgressMonitor
from faultmaven.core.investigation.prompts.templates.assembly import get_prompt_for_case
from faultmaven.core.investigation.schemas import (
    InquiryResponse,
    TerminalResponse,
    get_schema_for_stage,
)
from faultmaven.core.investigation.state_validator import (
    StateValidator,
    ValidationSeverity,
)
from faultmaven.core.investigation.working_conclusion_generator import (
    calculate_progress_metrics,
)
from faultmaven.infrastructure.llm.metering import (
    TurnTokenTracker,
    active_token_tracker,
)
from faultmaven.infrastructure.llm.providers import ReasoningIntent
from faultmaven.models.interfaces import ILLMProvider
from faultmaven.modules.agent.tools.vectorize_file_tool import (
    VECTORIZED_SYSTEM_MESSAGE,
)
from faultmaven.modules.case.contracts import (
    MESSAGE_METADATA_AGENT_SYNTHESIZED,
    Case,
    CaseState,
    InvestigationStage,
    TurnOutcome,
)
from faultmaven.modules.case.domain.services.case_action_manager import (
    earned_edge_refusal,
)
from faultmaven.modules.case.exceptions import StaleCaseException
from faultmaven.modules.knowledge.contracts import IKnowledgeService

from .affordances import (
    _GATE_VERIFICATION_STATUS,
    engine_owned_affordances,
    gate1_statement_is_confirmable,
)
from .cause_state import (
    _gate1_statement_presentation,
    _resolve_chat_provider_name,
)
from .progress import (
    confirmed_transition_arms,
    score_progress,
    summarize_for_turn_record,
)
from .response_synthesis import (
    _DISPOSITION_GATE_ANSWERED_KEY,
    _NARRATION_OVERCLAIM_NOTICE,
    _NARRATION_OVERCLAIM_NOTICE_PENDING,
    _narration_asserts_disposition,
    _note_engine_disposition_withdrawn,
    _prose_with_gate_notice,
    _record_deferred_disposition_decline,
    is_agent_response_synthesized,
)
from .stage_gates import (
    _close_confirmation_suggestions,
    _refresh_working_conclusion,
    _route_toolless_turn_single_shot,
    _should_force_tools,
)
from .terminal_proposals import (
    _maybe_propose_confirmed_resolution,
    _sweep_needs_for_terminal_hypotheses,
)
from .terminal_replies import (
    _build_resolution_confirmation,
    _compose_terminal_reply,
    _resolution_confirmation_suggestions,
    _select_ack_follow_ups,
)

# =============================================================================
# Evidence Category - Milestone Mapping (Option 2.5: System-Inferred Attribution)
# =============================================================================
#
# This mapping defines which milestones each evidence category can potentially advance.
# Used for automatic milestone attribution via the _infer_milestones() function.
#
# Design Reference:
# - docs/working/MILESTONE-ADVANCEMENT-ANALYSIS.md (Option 2.5)
# - docs/working/DESIGN-DISCUSSION-SUMMARY-2026-02-11.md
#
# Derived from MILESTONE_EVIDENCE_EXPECTATIONS in evidence_processor.py
#
# Three-Tier Logic:
#   Tier 1: MilestoneUpdates drives state (turn-level, LLM specifies)
#   Tier 2: System infers advances_milestones from this map (handles 90% of cases)
#   Tier 3: LLM can override with explicit specification (handles 10% edge cases)


# On a pending-transition turn, a typed reply that matches neither the confirm
# nor the decline patterns is either a short ambiguous answer to the gate
# ("why?", "hm") or a message that isn't answering the gate at all — new
# evidence, a question, an instruction to keep investigating. Above this length
# the message is treated as the latter: the proposal is withdrawn and the
# message is processed as a normal investigation turn, so the gate can never
# swallow substantive input. (The confirm matcher's own 100-char guard already
# encodes the same idea in the opposite direction: long messages are not
# gate answers.)
_PENDING_GATE_SUBSTANTIVE_LEN = 40

# KB pre-fetch (`_prefetch_kb_context`) fetch depth vs. prompt-surface cap.
# Retrieval returns CHUNK-level results, so a single long runbook can occupy
# several of the top-ranked slots. The fetch depth is the RERANKER'S candidate
# pool on the hybrid path: ``hybrid_search`` recalls max(3k, 15) vector and 2k
# keyword candidates for a fetch of k and reranks them, so k decides which
# runbooks can reach the prompt at all, not merely how many are kept.
# Render only the top KB_CONTEXT_MAX_ENTRIES into `case.kb_context`. Lowering
# the depth to the surface cap is a retrieval-quality change, not a cleanup
# (it was sized when a deeper consumer existed; that consumer is gone, the
# pool is what remains).


# =============================================================================
# Tool-loop per-call bound: the estimator (#612, #614)
# =============================================================================


# =============================================================================
# Resolution Summary Helpers
# =============================================================================


# The honest rendering of "the engine holds no established root cause" (#987).
# This state is REAL and legitimate — a case can be stabilized, or resolved
# out-of-band, without the cause ever being established — and before this
# string existed there was no sanctioned way to SAY it, so the recap reached
# for whatever text was lying around and rendered the early-stage placeholder
# ("Investigating potential causes - awaiting hypothesis generation") at the
# most trust-sensitive moment of the case. Naming the state is the fix; the
# placeholder leak was the symptom.


#: one says only "the user was mid-decision when the turn began", which is what
#: the resolution backstop needs to know before opening a competing offer.


def _narration_overclaim_notice(
    case, agent_text: str | None, *, gate_prose_appended: bool = False
) -> str | None:
    """Return the INV-40 corrective notice when narration over-claims disposition.

    Reconciles the ``_COMPLETION_PHRASES`` scan against engine truth: the notice
    fires only when the LLM asserted an unqualified resolved/closed claim AND the
    engine's state contradicts it — the case is **not** terminal and **no**
    prose gate notice was already composed this turn (any of the
    ``_prose_with_gate_notice`` override branches, which already frame the
    not-yet-terminal state; ``gate_prose_appended`` is the caller's signal that
    one fired). Critically it does **not** suppress on a bare ``pending_transition``:
    the suggestions-only override branch proposes a transition but appends no
    prose, so an over-claim there would otherwise stand uncontradicted — the
    guard's most probable real-world shape (a model confident enough to
    over-claim is the same one that proposes). The notice wording adapts to
    whether a proposal is pending. Returns ``None`` when there is nothing to
    correct.

    Pure over ``case`` + ``agent_text`` + ``gate_prose_appended``; the caller
    appends via ``_prose_with_gate_notice`` and increments
    ``narration_overclaim_total``.
    """
    if not _narration_asserts_disposition(agent_text):
        return None
    if case.is_terminal:
        # The claim is true — a terminal transition executed (or the case was
        # already terminal). Nothing to correct.
        return None
    if gate_prose_appended:
        # A prose gate notice already frames the real (not-yet-terminal) state
        # below the LLM's reply; a second notice would be redundant.
        return None
    if case.pending_transition:
        return _NARRATION_OVERCLAIM_NOTICE_PENDING
    return _NARRATION_OVERCLAIM_NOTICE


# =============================================================================
# Milestone Engine - Main Implementation
# =============================================================================


# The kb_qa relay wrapper, split so the truncation path can protect the tail.
# The SUFFIX is instructions, not prose: the citation format and "return via the
# schema tool, do not reply with plain text". Head-first truncation would delete
# how the model is told to answer, so its length is needed at the truncation
# site as well as at the formatting site.


class MilestoneEngine:
    """
    Data-Driven and Opportunistic Investigation Engine.

    The agent completes milestones opportunistically based on available
    data, rather than following a rigid phase pipeline.

    Responsibilities:
    - Generate prompts based on case status (INQUIRY, INVESTIGATING, RESOLVED)
    - Invoke LLM with appropriate schema
    - Process LLM responses and update case state
    - Track milestone completion and turn progress
    - Automatic status transitions when milestones complete

    Key Design Principles:
    - No phase orchestration - milestones complete when data is available
    - Status-based prompts instead of phase-based
    - Multiple milestones can complete in single turn
    - Repository abstraction for persistence (no direct DB access)
    """

    def __init__(
        self,
        llm_provider: ILLMProvider,
        repository: Any,  # Case repository abstraction (duck typing)
        investigation_tools: Any,
        knowledge_service: IKnowledgeService | None = None,
        trace_enabled: bool = True,
        checkpoint_service: Any | None = None,
        da_provider: Any | None = None,
        da_model: str | None = None,
        sanitizer: Any | None = None,
        redis_client: Any | None = None,
        report_service: Any | None = None,
        team_service: Any | None = None,
        share_repository: Any | None = None,
        runbook_kb: Any | None = None,
        conversion_service: Any | None = None,
    ):
        """Initialize milestone engine.

        Args:
            llm_provider: LLM provider implementation (ILLMProvider interface)
            repository: Case repository with save/get methods
            investigation_tools: AgentToolRegistry with investigation tools
                (search_file, deep_analysis, etc.). Required — DA turns use
                these for evidence searching during generation.
            knowledge_service: Optional knowledge service for KB searches
            trace_enabled: Enable observability tracing
            checkpoint_service: Optional CheckpointService for state snapshots
            da_provider: Dedicated provider for DA (directed analysis) turns
                (configured via DA_PROVIDER in .env).
                When None, falls back to llm_provider.
            da_model: Model to use with da_provider. When None,
                the provider's default model is used.
            sanitizer: DataSanitizer for case-scoped PII redaction.
                When None, PII redaction at the engine level is disabled.
            redis_client: Async Redis client for persisting redaction
                registries across turns. When None, registries are
                in-memory only (consistent within turn).
            report_service: Optional ReportGenerationService for auto-generating
                reports on terminal transitions. Fire-and-forget — failure
                does not block the transition.
            team_service: Optional team-membership resolver used by the KB
                pre-fetch to widen the case OWNER's KB read scope with
                the owner's team-shared runbooks (ADR-013 §D4). None in
                standalone — the team arm then resolves empty.
            share_repository: Optional ``IShareRepository`` backing that team
                arm. Both degrade gracefully to global ∪ owner-personal.
            runbook_kb: Optional ``RunbookKnowledgeBase`` for terminal-turn
                runbook deduplication, injected explicitly (fm#1030 — the old
                ``hasattr(knowledge_service, "runbook_kb")`` probe was
                permanently False; no such attribute exists on any
                ``IKnowledgeService``). None is legitimate: local dev without
                ChromaDB reaches the dedup site, and
                ``evaluate_runbook_suggestion`` then takes its honest "did not
                run" caveat.
            conversion_service: Optional ``ConversionService`` that turns a
                resolved case into a runbook draft. None when the service is
                unavailable; the runbook paths then report that no draft can
                be created.
        """
        self.deps = EngineDeps(
            llm_provider=llm_provider,
            repository=repository,
            knowledge_service=knowledge_service,
            trace_enabled=trace_enabled,
            checkpoint_service=checkpoint_service,
            investigation_tools=investigation_tools,
            da_provider=da_provider,
            da_model=da_model,
            sanitizer=sanitizer,
            redis_client=redis_client,
            report_service=report_service,
            team_service=team_service,
            share_repository=share_repository,
            runbook_kb=runbook_kb,
            conversion_service=conversion_service,
            hypothesis_manager=create_hypothesis_manager(),
            state_validator=StateValidator(),
            progress_monitor=ProgressMonitor(),
            llm_error_handler=LLMErrorHandler(),
        )

        # G10: Per-case asyncio locks to prevent concurrent process_turn
        # calls on the same case from interleaving and corrupting state
        self._case_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

        logger.info("MilestoneEngine initialized with structured output engine")
        self.vectorizer = EvidenceVectorizer(deps=self.deps)
        self.generator = StructuredOutputGenerator(
            deps=self.deps, vectorizer=self.vectorizer
        )
        self.kb_prefetcher = KbPrefetcher(deps=self.deps)
        self.responses = ResponseApplier(
            deps=self.deps, kb_prefetcher=self.kb_prefetcher
        )
        self.runbooks = RunbookCreator(case_locks=self._case_locks, deps=self.deps)
        self.terminal = TerminalTurnHandler(
            deps=self.deps, generator=self.generator, runbooks=self.runbooks
        )
        self.transitions = TransitionManager(
            deps=self.deps, kb_prefetcher=self.kb_prefetcher
        )

    async def process_turn(
        self,
        case: Case,
        user_message: str,
        attachments: list[dict[str, Any]] | None = None,
        intent_type: str | None = None,
        intent_data: dict[str, Any] | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Process a single conversation turn with optional structured intent.

        This is the main entry point for the milestone engine. It:
        1. Routes based on intent_type (if provided) or processes normally
        2. Generates status-appropriate prompt
        3. Invokes LLM with structured output
        4. Processes response and updates case state
        5. Records turn progress
        6. Checks for automatic status transitions

        Args:
            case: Current case
            user_message: User's message this turn
            attachments: Optional file attachments
            intent_type: Optional structured intent type (status_transition, confirmation, etc.)
            intent_data: Optional intent-specific data
            user_id: Authenticated principal for this turn. Keys the KB read
                allowlist handed to the agent's tools (owner + team arms,
                ADR-013 §D4). ``None`` means no principal — an engine-internal
                turn — and collapses the allowlist to the global corpus.

        Returns:
            {
                "agent_response": str,        # Natural language response to user
                "case_updated": Case,         # Updated case object
                "metadata": {
                    "turn_number": int,
                    "milestones_completed": List[str],
                    "progress_made": bool,
                    "status_transitioned": bool,
                    "outcome": TurnOutcome
                }
            }

        Raises:
            MilestoneEngineError: If processing fails
        """
        # G10: Acquire per-case lock to prevent concurrent turns from
        # interleaving reads/writes on the same case state
        async with self._case_locks[case.case_id]:
            # Bind a per-turn spend tracker for the duration of this turn. Every
            # billed LLM call made while handling the turn — main generation,
            # tool loop, KB Q&A, classifier, synthesis, and any fallback
            # attempts — accrues to it via the registry metering chokepoint.
            tracker = TurnTokenTracker()
            token = active_token_tracker.set(tracker)
            try:
                result = await self._process_turn_impl(
                    case,
                    user_message,
                    attachments,
                    intent_type,
                    intent_data,
                    user_id=user_id,
                )
            finally:
                active_token_tracker.reset(token)
                try:
                    logger.info(
                        "turn_token_spend",
                        extra={
                            "case_id": getattr(case, "case_id", None),
                            "input_tokens": tracker.input_tokens,
                            "output_tokens": tracker.output_tokens,
                            "cache_read_tokens": tracker.cache_read_tokens,
                            "cache_write_tokens": tracker.cache_write_tokens,
                            "total_tokens": tracker.total_tokens,
                            # Cost-weighted spend (cache reads down-weighted) —
                            # the SAME measure the soft-budget alert and hard
                            # ceiling compare against, emitted every turn so
                            # budget headroom is visible without recomputation.
                            "spend_weighted_tokens": tracker.spend_weighted_tokens,
                            "total_calls": tracker.total_calls,
                            "estimated_cost_usd": round(tracker.cost_usd, 6),
                            "unpriced_calls": tracker.unpriced_calls,
                        },
                    )
                    # Soft per-turn budget alert (observability only; no behavior
                    # change) — surfaces high-spend turns for the prompt-sizing work.
                    from faultmaven.config.settings import get_settings

                    _turn_budget = get_settings().prompt_budget.turn_token_budget
                    # Compare on the same cost-weighted measure as the hard
                    # ceiling (cache reads down-weighted) so the two spend guards
                    # are directly comparable; report the raw total too.
                    if _turn_budget and tracker.spend_weighted_tokens > _turn_budget:
                        logger.warning(
                            "turn_token_budget_exceeded",
                            extra={
                                "case_id": getattr(case, "case_id", None),
                                "spend_weighted_tokens": tracker.spend_weighted_tokens,
                                "total_tokens": tracker.total_tokens,
                                "total_calls": tracker.total_calls,
                                "turn_token_budget": _turn_budget,
                            },
                        )
                except Exception:
                    pass
            return result

    async def _process_turn_impl(
        self,
        case: Case,
        user_message: str,
        attachments: list[dict[str, Any]] | None = None,
        intent_type: str | None = None,
        intent_data: dict[str, Any] | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """Inner implementation of process_turn, called under per-case lock."""
        # Refused FIRST, before any state is touched. INVESTIGATING is not a
        # user-selectable case action (#1608) — it is earned by a confirmed
        # problem statement, which Gate 1 performs.
        #
        # Placement is load-bearing, not tidiness. Section 0b below cancels a
        # contradicting pending transition and records the fm#1122 decline
        # signature before reaching the per-target branches, so refusing down
        # there unwound past those mutations with no save: the standing close
        # offer survived with no decline recorded, and the engine re-fired it
        # on the next turn — the re-nag fm#1122 exists to prevent.
        #
        # ``InvestigationService._handle_status_transition`` rejects this at
        # the boundary with a 422, so this is the backstop for direct engine
        # callers rather than the path a client takes.
        #
        # ONE guard, DERIVED from ``USER_SELECTABLE_ACTIONS``. It was two
        # hand-written comparisons, and the second one blinded the static test
        # that pins the first's placement: that test anchors on the phrase
        # "not a user-selectable case action", ``str.find`` returns the FIRST
        # occurrence, and both refusals sat above the mutators — so deleting
        # the Gate-1 refusal entirely left all 58 lifecycle-invariant tests
        # green. Verified by mutation. One guard, one anchor, and the refusal
        # now moves with the dict instead of alongside it.
        if intent_type == "status_transition":
            _refusal = earned_edge_refusal(
                case.state, (intent_data or {}).get("to_state") or ""
            )
            if _refusal:
                raise ValueError(_refusal)

        # Add intent information to logger for tracing
        # Note: current_turn has already been incremented by investigation_service before this point
        intent_info = f" [intent={intent_type}]" if intent_type else ""
        logger.info(
            f"Processing turn {case.current_turn} for case {case.case_id} "
            f"(state: {case.state}){intent_info}"
        )

        # Debug logging for Turn 2 issue
        # Note: current_turn already incremented before this point
        if case.state == CaseState.INQUIRY:
            logger.info(
                f"Turn {case.current_turn} starting: state={case.state.value}, "
                f"confirmed={case.inquiry.problem_statement_confirmed}"
            )
        else:
            logger.info(
                f"Turn {case.current_turn} starting: state={case.state.value}, "
                f"stage={case.current_stage}"
            )

        try:
            # Initialize metadata early so it can be used throughout the function
            metadata = {
                "milestones_completed": [],
                "evidence_added": [],
                "hypotheses_generated": [],
                "hypotheses_validated": [],
                "solutions_proposed": [],
                # Evidence-needs Phase 3: IDs of needs created or updated
                # this turn. Used by Phase 6 to resolve ``new_index_N``
                # references on ``SuggestedFollowUp.evidence_need_id``.
                "evidence_needs_updated": [],
                "progress_made": False,
                "status_transitioned": False,
                "outcome": TurnOutcome.CONVERSATION,
            }

            # The turn's uploads, derived ONCE, above the path fork (#1229).
            # Every branch below — the terminal short-circuit, the
            # deterministic gate/dropdown returns, and the generation path —
            # reports the same reading, and the two degradation warnings in
            # ``_report_turn_uploads`` fire wherever the degradation happens
            # rather than only on the path that used to own the derivation.
            #
            # Deriving the reading here is ALL that happens here. The progress
            # side of it is applied per path, and deliberately not hoisted:
            # ``turns_without_progress`` is read DURING a generation turn — the
            # prompt's "N since last progress" line, the momentum bands, the
            # evidence-need page cursor — and Step 5.8 updates it only after
            # those have run. Resetting it up here made an ordinary turn
            # carrying a novel upload render "0 since last progress" where base
            # rendered "5", which is a change to what the model is told about
            # its own stall state and no part of #1229. The generation path
            # keeps Step 5.8; the deterministic branches apply the same reading
            # in ``_finish_deterministic_turn``, which runs before their save.
            upload_report = _report_turn_uploads(case, attachments)
            metadata.update(upload_report)

            # 0a. Terminal case handling — Q&A and report regeneration only
            if case.is_terminal:
                return await self.terminal.process_terminal_turn(
                    case, user_message, metadata, user_id=user_id
                )

            # 0b. Pending transition confirmation — short-circuit before LLM
            # When a pending transition exists (User-Agent Handshake), check if
            # the user is confirming or declining BEFORE calling the LLM. This
            # avoids unnecessary LLM calls and prevents schema validation errors
            # from blocking the confirmation.
            #
            # Two detection paths (checked in order):
            # 1. Intent-based: DECIDE suggestion clicks carry
            #    intent_type="confirmation" + confirmation_value — deterministic
            # 2. Pattern-based: fallback for users who type instead of clicking
            if hasattr(case, "pending_transition") and case.pending_transition:
                from faultmaven.core.investigation.terminal_transitions import (
                    cancel_pending_transition,
                )

                # This turn BEGAN inside a disposition handshake, whoever
                # opened it. Recorded before any branch below decides what the
                # message was, because several of them withdraw the offer and
                # fall through to normal processing — and the resolution
                # backstop at 4c must not read that as a free channel and put a
                # DIFFERENT target in front of the user on the same turn. A
                # user part-way through deciding about a close, whose next word
                # is "yes", must not have that word land on a resolve offer
                # they were shown while asking about the close. Turn-scoped: the
                # backstop is free again next turn.
                metadata[_DISPOSITION_GATE_ANSWERED_KEY] = True

                # Shared gate-reply classification: a question is never a
                # gate answer regardless of length; otherwise length is the
                # substance proxy. Computed once so the decline and
                # non-answer branches below cannot drift apart.
                stripped_message = (user_message or "").strip()
                message_is_substantive = bool(stripped_message) and (
                    len(stripped_message) > _PENDING_GATE_SUBSTANTIVE_LEN
                    or "?" in stripped_message
                )

                # Contradicting status_transition intent cancels the pending
                # transition. Example: user clicked "Close" (pending), then
                # clicked "Investigating" — cancel the close and process the
                # new intent normally.
                if (
                    intent_type == "status_transition"
                    and intent_data
                    and intent_data.get("to_state")
                    != case.pending_transition.get("to_state")
                ):
                    old_target = case.pending_transition.get("to_state")
                    new_target = intent_data.get("to_state")
                    # Picking a different target is an unmistakable refusal of
                    # the standing offer, so record it before the cancel erases
                    # the provenance (fm#1122) — otherwise the engine's
                    # deferred disposition re-fires next turn from state the
                    # user just contradicted.
                    _record_deferred_disposition_decline(case, superseded_by=new_target)
                    _note_engine_disposition_withdrawn(case, metadata)
                    cancel_pending_transition(case)
                    logger.info(
                        f"Pending transition to '{old_target}' cancelled — user "
                        f"requested different transition to '{new_target}' "
                        f"for case {case.case_id}"
                    )
                    # Fall through to normal intent processing (section 0c)
                elif not case.pending_transition.get("needs_info"):
                    # Resolve confirm/decline from intent or pattern matching
                    intent_confirms = (
                        intent_type == "confirmation"
                        and (intent_data or {}).get("value") is True
                    )
                    # A repeated status_transition intent matching the pending
                    # transition's target is an implicit confirmation — the user
                    # clicked the same dropdown/button again after the agent
                    # proposed the transition.
                    status_transition_confirms = (
                        intent_type == "status_transition"
                        and (intent_data or {}).get("to_state")
                        == case.pending_transition.get("to_state")
                    )
                    intent_confirms = intent_confirms or status_transition_confirms
                    intent_declines = (
                        intent_type == "confirmation"
                        and (intent_data or {}).get("value") is False
                    )
                    user_confirms = intent_confirms or _user_confirms_transition(
                        user_message
                    )
                    user_declines = intent_declines or _user_declines_transition(
                        user_message
                    )

                    if user_confirms:
                        from faultmaven.core.investigation.terminal_transitions import (
                            confirm_pending_transition,
                        )

                        if self.deps.checkpoint_service:
                            to_state = case.pending_transition.get(
                                "to_state", "unknown"
                            )
                            await self.deps.checkpoint_service.create_checkpoint(
                                case,
                                trigger="pre_case_action",
                                metadata={
                                    "from_state": case.state.value,
                                    "to_state": to_state,
                                },
                            )

                        executed = confirm_pending_transition(case, case.user_id)
                        if (
                            not executed
                            and (case.pending_transition or {}).get("to_state")
                            == "resolved"
                        ):
                            # INV-37 resolve-preservation: the pending CLOSE
                            # pivoted to a RESOLVED proposal (the case became
                            # resolvable). Nothing terminal committed — present
                            # the resolve confirmation instead of a CLOSED
                            # report, which would falsely record the case as
                            # closed-unresolved. The pivot's user-facing message
                            # is the SUGGEST_RESOLVE prose the guard already
                            # computed and stored on the resolved pending (same
                            # text the proposal-time pivot shows — one source of
                            # truth, and it renders the no-record out-of-band-fix
                            # case correctly, which _build_resolution_confirmation
                            # does not).
                            resolve_msg = case.pending_transition["summary"]
                            turn_metadata = _finish_deterministic_turn(
                                case,
                                user_message or "",
                                resolve_msg,
                                upload_report,
                                progress_made=False,
                            )
                            await self.deps.repository.save(case)
                            return {
                                "agent_response": resolve_msg,
                                "suggested_follow_ups": (
                                    _resolution_confirmation_suggestions()
                                ),
                                "case_updated": case,
                                "metadata": turn_metadata,
                            }

                        # Persist the terminal status before generating the
                        # summary — the Report row FKs to case_id.
                        await self.deps.repository.save(case)

                        # Synchronous summary generation. Returns rendered
                        # markdown on success, a skip note when the gate
                        # blocks generation, a failure note on LLM error,
                        # or None when no report service is configured.
                        # The second tuple element flags an LLM-error
                        # failure so the ack-turn can offer the regen
                        # affordance (G2 — there's no inline summary to
                        # be noisy next to when generation failed).
                        (
                            summary_payload,
                            summary_failed,
                        ) = await self.terminal.auto_generate_report(case)

                        agent_response = _compose_terminal_reply(case, summary_payload)
                        turn_metadata = _finish_deterministic_turn(
                            case,
                            user_message or "",
                            agent_response,
                            upload_report,
                            progress_made=True,
                            **confirmed_transition_arms(case, executed),
                        )
                        await self.deps.repository.save(case)

                        # Closure-ack follow-ups depend on whether
                        # generation succeeded. Success: minimal
                        # suggestions (the summary is rendered inline,
                        # so a regen card next to it would be noise).
                        # Failure: include the regen affordance so the
                        # user can retry immediately — the "noise next
                        # to inline summary" rationale doesn't apply
                        # when there's no summary inline.
                        remaining = await _remaining_regens_for(
                            self.deps.report_service, self.deps.repository, case
                        )
                        follow_ups = _select_ack_follow_ups(
                            case, summary_failed, remaining
                        )

                        return {
                            "agent_response": agent_response,
                            "suggested_follow_ups": follow_ups,
                            "case_updated": case,
                            "metadata": turn_metadata,
                        }
                    elif user_declines:
                        # Record the refusal BEFORE cancelling: the cancel is
                        # what erases the provenance this reads (fm#1122).
                        _record_deferred_disposition_decline(case)
                        _note_engine_disposition_withdrawn(case, metadata)
                        cancel_pending_transition(case)

                        if message_is_substantive:
                            # The decline carries substance beyond a bare
                            # "no" — new data, a question, a redirection
                            # ("no, we did not do anything yet — did you
                            # see anything wrong?"). The proposal is
                            # withdrawn; the message itself must still be
                            # processed as a normal turn so nothing the
                            # user said is swallowed by the gate.
                            logger.info(
                                f"Pending transition declined with a "
                                f"substantive message for case "
                                f"{case.case_id} — proposal withdrawn, "
                                f"processing message normally"
                            )
                            # Fall through to normal processing (section 0c)
                        else:
                            agent_response = "Understood. The case remains open for further investigation."
                            turn_metadata = _finish_deterministic_turn(
                                case,
                                user_message or "",
                                agent_response,
                                upload_report,
                                progress_made=False,
                            )
                            await self.deps.repository.save(case)

                            return {
                                "agent_response": agent_response,
                                "suggested_follow_ups": [],
                                "case_updated": case,
                                "metadata": turn_metadata,
                            }
                    else:
                        # User said something that isn't a clear yes/no.
                        # A SHORT question-free reply is treated as an
                        # ambiguous answer to the confirmation and
                        # re-presented ONCE (don't send a bare "hmm"
                        # through the LLM tool loop). A substantive message
                        # (long, or carrying a question) — or any second
                        # non-answer — is not an answer to the gate at all:
                        # holding the gate against those swallowed every
                        # typed turn with no LLM call and bricked the case
                        # (#656, turns 12-13). The proposal is withdrawn
                        # instead and the message processed as a normal
                        # turn; the engine can always re-propose later from
                        # fresher state.
                        already_re_presented = case.pending_transition.get(
                            "re_presented", False
                        )
                        # Blank input (whitespace-only slips past the route's
                        # empty-payload guard) is never worth an LLM turn —
                        # it re-presents deterministically without consuming
                        # the one re-present allowance.
                        if stripped_message and (
                            message_is_substantive or already_re_presented
                        ):
                            # The offer is withdrawn either way; whether that
                            # is a REFUSAL splits on the two halves of
                            # message_is_substantive, which the gate
                            # deliberately conflates. A QUESTION is a user
                            # deciding — "what happens to the runbook if I
                            # close this?" — and recording it would make the
                            # affordance disappear, unexplained, until a
                            # premise moved: the same engine-acts-without-
                            # saying-why defect this PR family exists to kill.
                            # A long non-question non-answer is a deflection
                            # ("we'll do it in Friday's window") and IS a
                            # refusal. Either way the withdrawal is noted for
                            # the turn, because the fall-through below reaches
                            # _maybe_propose_deferred_close again and would
                            # otherwise re-take the affordances on this very
                            # turn (fm#1122).
                            if "?" not in stripped_message:
                                _record_deferred_disposition_decline(case)
                            _note_engine_disposition_withdrawn(case, metadata)
                            cancel_pending_transition(case)
                            logger.info(
                                f"Pending transition withdrawn for case "
                                f"{case.case_id}: message is not a gate "
                                f"answer (substantive="
                                f"{message_is_substantive}, "
                                f"already_re_presented="
                                f"{already_re_presented}) — processing "
                                f"message normally"
                            )
                            # Fall through to normal processing (section 0c)
                        else:
                            if stripped_message:
                                case.pending_transition["re_presented"] = True
                            to_state = case.pending_transition.get(
                                "to_state", "resolved"
                            )
                            summary = case.pending_transition.get("summary", "")

                            agent_response = (
                                "Please select one of the options above to continue."
                                if not summary
                                else f"{summary}\n\nPlease select one of the options above to continue."
                            )
                            if to_state == "resolved":
                                follow_ups = _resolution_confirmation_suggestions()
                            else:
                                follow_ups = _close_confirmation_suggestions()

                            turn_metadata = _finish_deterministic_turn(
                                case,
                                user_message or "",
                                agent_response,
                                upload_report,
                                progress_made=False,
                            )
                            await self.deps.repository.save(case)

                            return {
                                "agent_response": agent_response,
                                "suggested_follow_ups": follow_ups,
                                "case_updated": case,
                                "metadata": turn_metadata,
                            }

            # 0c. Detect explicit user intent to close/resolve case
            # This handles cases where user explicitly says "close this case" or "mark as resolved"
            # without relying on LLM to set solution_verified=True
            #
            # CRITICAL DISTINCTION:
            # - CLOSED (without solution): User abandons investigation without finding solution
            # - RESOLVED (with solution): User confirms problem is fixed/resolved
            #
            # ‼ ONE PATH, not two. This described "TWO COMPLEMENTARY PATHS"
            # — an explicit intent, and a NATURAL LANGUAGE "pattern matching
            # fallback (below)" with a 2026-02-08 fix for "close as
            # unresolved" matching resolution patterns. There is no such
            # fallback below, and there is no natural-language transition
            # detector anywhere: ``_user_confirms_transition`` /
            # ``_user_declines_transition`` only answer a STANDING pending, and
            # ``IntentResolver`` matches typed text against suggestions already
            # on screen. A typed "mark this resolved" with nothing standing
            # reaches the state machine solely by the MODEL emitting
            # ``proposed_transition``.
            #
            # So: a structured ``status_transition`` intent is handled below
            # (CLOSE only — the earned edges are refused at the top of this
            # method), and everything else is the model's job.
            # ============================================================
            # USER INTENT DETECTION - EXPLICIT STATUS TRANSITION (Frontend Buttons)
            # ============================================================
            # BUG FIX (2026-02-09): Status dropdown transitions not working
            # ROOT CAUSE: intent_type="status_transition" skipped pattern matching but had no handler
            # FIX: Add explicit handler before pattern matching section
            if intent_type == "status_transition" and intent_data:
                to_status_str = intent_data.get("to_state")
                from_status_str = intent_data.get("from_state")

                if not to_status_str:
                    raise ValueError(
                        "to_state is required for status_transition intent"
                    )

                logger.info(
                    f"Explicit status_transition intent: {from_status_str} → {to_status_str} "
                    f"for case {case.case_id}"
                )

                # Import terminal transition functions
                from faultmaven.core.investigation.terminal_transitions import (
                    assess_closure_readiness,
                    propose_transition,
                )

                # Handle each status transition
                if to_status_str == "closed":
                    if case.state not in (
                        CaseState.INQUIRY,
                        CaseState.INVESTIGATING,
                    ):
                        raise ValueError(
                            f"Cannot transition to CLOSED from {case.state.value}"
                        )

                    # Use closure readiness for a meaningful summary, and
                    # pivot to RESOLVED if the case has root cause + solution
                    # on record (SUGGEST_RESOLVE — symmetric to the LLM-emit
                    # path's SUGGEST_CLOSE pivot for the opposite direction).
                    closure = assess_closure_readiness(case)
                    if closure.verdict == closure.SUGGEST_RESOLVE:
                        # closure_reason auto-derives to None inside
                        # propose_transition for RESOLVED — resolution itself
                        # is the categorization. The user still confirms via
                        # the resolution confirmation pair.
                        propose_transition(
                            case=case,
                            to_state="resolved",
                            summary=closure.message,
                        )
                        logger.info(
                            f"User dropdown-requested CLOSED for case "
                            f"{case.case_id} but verdict=SUGGEST_RESOLVE "
                            f"(case has root cause + solution); pivoting "
                            f"to RESOLVED."
                        )
                        turn_metadata = _finish_deterministic_turn(
                            case,
                            user_message or "",
                            closure.message,
                            upload_report,
                            progress_made=False,
                        )
                        await self.deps.repository.save(case)
                        return {
                            "agent_response": closure.message,
                            "suggested_follow_ups": _resolution_confirmation_suggestions(),
                            "case_updated": case,
                            "metadata": turn_metadata,
                        }

                    # Standard close — closure_reason derived inside
                    # propose_transition from case state.
                    propose_transition(
                        case=case,
                        to_state="closed",
                        summary=closure.message,
                    )

                    logger.info(
                        f"Proposed CLOSED transition for case {case.case_id} via dropdown "
                        f"(pending user confirmation)"
                    )

                    # Save and return with closure summary + canonical
                    # confirm/decline pair (alignment with agent-initiated path).
                    turn_metadata = _finish_deterministic_turn(
                        case,
                        user_message or "",
                        closure.message,
                        upload_report,
                        progress_made=False,
                    )
                    await self.deps.repository.save(case)
                    return {
                        "agent_response": closure.message,
                        "suggested_follow_ups": _close_confirmation_suggestions(),
                        "case_updated": case,
                        "metadata": turn_metadata,
                    }

                # ``resolved`` never reaches here either — the same guard
                # refuses it. The branch that used to live here ran the
                # readiness check AFTER the pick and then argued with it
                # (propose / pivot to close / ask for what is missing), and
                # one of its arms confirmed a standing ``needs_info``
                # proposal without re-reading readiness — executing RESOLVED
                # on a case carrying no qualifying ``causal_absence_evidence``
                # row. Deciding whether to OFFER retires the argument and the
                # bypass together.

                # ``investigating`` never reaches here — the guard at the top
                # of this method refuses it before any state is touched. The
                # branch that used to live here accepted the request and fell
                # through to the LLM on a synthetic user message ("I want to
                # start a formal investigation to find the root cause"), which
                # read as established problem-solving intent and pulled a
                # problem statement out of cases that had none (#1608).

                else:
                    raise ValueError(f"Unknown to_state: {to_status_str}")

            elif intent_type == "confirmation":
                # THE ANSWER THE USER GAVE. A confirmation intent carries one
                # (``QueryIntent`` refuses to validate without it), and this
                # branch commits a gate, so it must read it — a branch that
                # decides off ``intent_type`` alone answers the gate for the
                # user. Section 0b's two reads above are the same rule; the
                # scan that holds all three to it is
                # ``tests/unit/core/investigation/test_gate_one_decline_1464.py``.
                confirmation_value = (intent_data or {}).get("value")

                logger.info(
                    f"Explicit confirmation intent for case {case.case_id} "
                    f"(value={confirmation_value}, has_pending_statement="
                    f"{bool(case.inquiry.proposed_problem_statement)})"
                )

                if case.state != CaseState.INQUIRY:
                    logger.warning(
                        f"Received confirmation intent for case {case.case_id} but status is {case.state.value}"
                    )
                elif not gate1_statement_is_confirmable(
                    case.inquiry.proposed_problem_statement
                ):
                    # Same predicate as the LLM path and the minted path. This
                    # site runs BEFORE ``_apply_inquiry_updates``, so nothing
                    # can have revised the statement yet and both arguments are
                    # the standing text — the rule degrades to "a statement
                    # stands", which is what this branch always meant. Routed
                    # through the shared predicate anyway so the three consent
                    # sites cannot drift apart again (fm#918).
                    logger.warning(
                        f"Received confirmation intent for case {case.case_id} but no proposed problem statement exists"
                    )
                elif confirmation_value is not True:
                    # #1464: Gate 1 commits on AFFIRMATIVE CONSENT ONLY. This
                    # branch used to be blind to the value, so "Not quite, let
                    # me clarify" (``confirmation_value: False``, the engine's
                    # own decline affordance) started the investigation on the
                    # statement the user was asking to refine.
                    #
                    # The decline is NOT a no-op turn — it falls through to
                    # normal LLM processing exactly as section 0b's substantive
                    # decline does, so the message is answered and the LLM can
                    # rewrite ``proposed_problem_statement`` (it stays mutable
                    # precisely because Gate 1 did not commit). ``_gate1_is_pending``
                    # is a pure function of that state, so the confirmation pair
                    # is re-offered on the refined statement with no bookkeeping
                    # here. Nothing to withdraw either: Gate 1 has no
                    # ``pending_transition`` row, which is why this arm has no
                    # counterpart to 0b's ``_record_deferred_disposition_decline``.
                    if confirmation_value is False:
                        logger.info(
                            f"Case {case.case_id}: Gate 1 DECLINED via confirmation "
                            f"intent — nothing committed, processing the message "
                            f"normally so the problem statement can be refined"
                        )
                    else:
                        logger.warning(
                            f"Case {case.case_id}: confirmation intent carried no "
                            f"value ({confirmation_value!r}) — Gate 1 not committed. "
                            f"Only an explicit True is consent"
                        )
                else:
                    # Gate 1 commit (problem-statement confirmation). There is
                    # no path fork (redesign R5) — the investigation proceeds
                    # opportunistically once INVESTIGATING begins.
                    case.inquiry.problem_statement_confirmed = True
                    case.inquiry.problem_statement_confirmed_at = datetime.now(UTC)

                    logger.info(
                        f"Case {case.case_id}: Gate 1 confirmed via confirmation intent "
                        f"(transitioning to INVESTIGATING)"
                    )

                    # Do NOT transition here — _check_automatic_transitions
                    # fires INQUIRY -> INVESTIGATING on Gate 1 alone.

            # ============================================================
            # HYPOTHESIS ACTION - Explicit Intent (Frontend/IntentResolver)
            # ============================================================
            # Applies the state change BEFORE LLM processing so the agent
            # sees updated hypothesis state in its context and can acknowledge.
            elif intent_type == "hypothesis_action" and intent_data:
                _apply_hypothesis_action_intent(
                    self.deps.hypothesis_manager,
                    case,
                    intent_data,
                    user_message,
                    metadata,
                )

                # Fall through to normal LLM processing for acknowledgment

            # ‼ There is NO natural-language transition detector. This said
            # one lived in ``InvestigationService._detect_transition_intent``;
            # no such function exists anywhere in the tree. A typed "mark this
            # resolved" reaches here as ``conversation`` and its only route to
            # the state machine is the MODEL emitting ``proposed_transition``
            # (the COMPLETION prompt's "user expresses transition intent"
            # branch). ``IntentResolver`` cannot substitute — it matches typed
            # text against suggestions ALREADY on screen, so with nothing
            # standing it has nothing to match.
            #
            # Worth knowing before promising a deterministic typed path: the
            # engine-owned openers are Gate 1 and the INV-43 resolution
            # backstop, both driven by case state rather than by what the user
            # typed. A structured ``status_transition`` intent is handled above
            # and is now CLOSED-only.

            # 1. Gather Context & Build Prompt
            # KB retrieval during turns is handled by the kb_qa tool in the
            # tool-augmented generation loop. The agent decides when to call
            # kb_qa based on prompt directives (Rule 6: Knowledge First).
            # This ensures proper scope filtering via ToolContext (user_id,
            # team_ids) which the engine doesn't have at this level.

            # Initialize case-scoped PII redaction context.
            # Created fresh each turn — the assembled prompt contains raw
            # structural indices from ALL evidence files, so a single
            # sanitize() call builds a collision-free registry. Redis
            # load() provides cross-turn numbering consistency (same IP
            # keeps the same placeholder across turns) but is not required
            # for correctness.
            from faultmaven.config.settings import get_settings
            from faultmaven.infrastructure.security.case_redaction import (
                CaseRedactionContext,
            )

            redaction_settings = get_settings()
            redaction_ctx = CaseRedactionContext(
                case_id=case.case_id,
                sanitizer=self.deps.sanitizer,
                redis_client=self.deps.redis_client,
                enabled=_should_redact(self.deps.sanitizer),
                ttl_hours=redaction_settings.protection.redaction_registry_ttl_hours,
            )
            await redaction_ctx.load()

            # Build prompt using the adaptive template system
            # Gap #6: Pass provider info for dynamic token budget calculation
            provider_name = getattr(self.deps.llm_provider, "provider_name", None)
            model_name = (
                getattr(self.deps.llm_provider.config, "default_model", None)
                if hasattr(self.deps.llm_provider, "config")
                else None
            )

            # Processing mode for prompt framing (structural-index role
            # tagging). Prefer the authoritative query_mode the service already
            # computed — it factors in a fresh evidence-bearing attachment and
            # re-routes a generic cover message to DIRECTED_ANALYSIS (#708).
            # Fall back to a local text classification for engine entry points
            # that don't thread query_mode (tests, direct calls). Keeping the
            # prompt mode and the force_tools mode in sync avoids framing the
            # turn as TRIAGE while tools are forced for DIRECTED_ANALYSIS.
            processing_mode = (intent_data or {}).get("query_mode")
            if not processing_mode:
                from faultmaven.modules.agent.domain.services.query_classifier import (
                    classify_query,
                )

                processing_mode = classify_query(
                    user_message, has_attachments=bool(case.evidence)
                ).mode.value

            # Phase 4c — prefetch entity highlight ROWS from the Phase 4
            # ``case_entities`` registry when the feature is on. When
            # the flag is off (or the producer wrote no entities),
            # ``fetch_entity_highlights`` returns [] and the template
            # slot renders empty. Rows rather than a formatted block: the
            # values come out of file content, so the block is fenced, and
            # the fence re-renders on a token collision — which it cannot do
            # around an awaited query (#1228). Formatting happens inside the
            # fenced assembly in ``build_investigation_context``.
            entity_highlight_groups: list = []
            try:
                from faultmaven.config.settings import get_settings
                from faultmaven.core.investigation.prompts.context_builder.entity_highlights import (
                    fetch_entity_highlights,
                )

                if get_settings().preprocessing.entity_registry_enabled:
                    entity_highlight_groups = await fetch_entity_highlights(
                        self.deps.repository, case.case_id
                    )
            except Exception as exc:
                logger.warning(
                    "Entity highlights prefetch failed for case %s (non-fatal): %s",
                    case.case_id,
                    exc,
                )

            # Build the prompt. In directed-analysis turns with tools available,
            # historical evidence is rendered as index+stub (the agent will
            # search_file). If tools then fail at RUNTIME and we fall through to
            # the non-tool path, that elided prompt would strand the agent (no
            # tool to recover the evidence), so keep a builder that reconstructs
            # the full-evidence prompt for that fallback.
            _tools_avail = self.generator.tools_effectively_available()

            def _build_prompt(
                tools_available: bool,
                *,
                target_tokens: Optional[int] = None,
                sizing_provider: Optional[str] = provider_name,
                sizing_model: Optional[str] = model_name,
            ) -> str:
                return get_prompt_for_case(
                    case,
                    user_message,
                    kb_results=None,
                    provider_name=sizing_provider,
                    model_name=sizing_model,
                    processing_mode=processing_mode,
                    entity_highlight_groups=entity_highlight_groups,
                    tools_available=tools_available,
                    target_tokens=target_tokens,
                )

            def _build_tool_loop_base(
                *,
                target_tokens: int,
                provider_name: Optional[str],
                model_name: Optional[str],
            ) -> str:
                # #614: the same prompt, re-assembled for the model the tool
                # loop sends to, when the chat-sized one does not fit there.
                return _build_prompt(
                    _tools_avail,
                    target_tokens=target_tokens,
                    sizing_provider=provider_name,
                    sizing_model=model_name,
                )

            # fm#1116: decide the generation route BEFORE the single prompt
            # build. A tool-less turn with nothing to search takes the
            # single-shot structured path (reasoning declared) and must get the
            # un-elided prompt — no search_file will run to recover an extract
            # replaced by an index stub. Pure over case state; computed once.
            has_pending = (
                hasattr(case, "pending_transition") and case.pending_transition
            )
            force_tools = (
                _should_force_tools(processing_mode, case, bool(has_pending))
                if self.deps.investigation_tools
                else False
            )
            route_single_shot = bool(
                self.deps.investigation_tools
            ) and _route_toolless_turn_single_shot(processing_mode, case, force_tools)

            prompt = _build_prompt(_tools_avail and not route_single_shot)

            # Determine schema based on status/stage
            if case.state == CaseState.INQUIRY:
                schema_model = InquiryResponse
                logger.info(
                    f"Turn {case.current_turn} schema selection: "
                    f"state={case.state.value}, schema=InquiryResponse"
                )
            elif case.state in [CaseState.RESOLVED, CaseState.CLOSED]:
                schema_model = TerminalResponse
                logger.info(
                    f"Turn {case.current_turn} schema selection: "
                    f"state={case.state.value}, schema=TerminalResponse"
                )
            else:
                schema_model = get_schema_for_stage(
                    case.current_stage or InvestigationStage.DIAGNOSIS
                )
                logger.info(
                    f"Turn {case.current_turn} schema selection: "
                    f"state={case.state.value}, stage={case.current_stage}, "
                    f"schema={schema_model.__name__}"
                )

            # 2. Invoke LLM with structured output
            # Tool availability: all turns get tools when tools are registered.
            # The LLM decides which tool to invoke based on the user's question.
            #
            # tool_choice varies by query mode:
            # - directed_analysis + searchable material: "required" — LLM must
            #   search evidence
            # - all other turns: "auto" — LLM decides whether to use tools
            #
            # Searchable material is Evidence rows OR fresh uploaded files
            # (post-010, a delivering turn has only an UploadedFile, not yet an
            # Evidence row — ``bool(case.evidence)`` alone would leave the
            # evidence-delivering turn on tool_choice=auto and let the agent
            # skip analysis, #708). ``_has_searchable_material`` guarantees a
            # real search target so forcing tools cannot crash the loop.
            #
            # Safety net: when a pending_transition exists, the user is in a
            # confirmation flow. Don't force tool_choice=required — the user's
            # message is a confirmation/decline that may have fallen through
            # pattern matching (typed instead of clicked). Forcing tools crashes
            # the tool loop when the LLM has nothing to search for.
            if route_single_shot:
                # fm#1116: nothing to search on this turn — take the single-shot
                # structured path with reasoning declared, instead of a tool
                # loop that would pin reasoning to "none" for tools it cannot
                # use. ``prompt`` was built un-elided above for this route.
                logger.info(
                    f"Turn {case.current_turn}: no searchable material and tools "
                    f"not forced (mode={processing_mode}) — single-shot "
                    f"structured path with reasoning_intent=INFERENCE"
                )
                response_obj = await self.generator.generate_structured_output(
                    prompt,
                    schema_model,
                    redaction_ctx=redaction_ctx,
                    case=case,
                    user_message=user_message,
                    reasoning_intent=ReasoningIntent.INFERENCE,
                    min_output_tokens=TOOLLESS_INFERENCE_OUTPUT_FLOOR,
                )
            elif self.deps.investigation_tools:
                da_tools = self.generator.build_da_tool_schemas()
                da_context = await self.generator.build_tool_context(
                    case, user_id=user_id
                )
                response_obj = await self.generator.generate_structured_output(
                    prompt,
                    schema_model,
                    investigation_tools=da_tools,
                    tool_context=da_context,
                    force_tool_use=force_tools,
                    redaction_ctx=redaction_ctx,
                    case=case,
                    user_message=user_message,
                    # Only meaningful when elision happened (tools available);
                    # the non-tool fallback rebuilds with full evidence.
                    fallback_prompt_builder=(
                        (lambda: _build_prompt(False)) if _tools_avail else None
                    ),
                    base_prompt_builder=_build_tool_loop_base,
                )
            else:
                response_obj = await self.generator.generate_structured_output(
                    prompt,
                    schema_model,
                    redaction_ctx=redaction_ctx,
                    case=case,
                    user_message=user_message,
                )

            # Debug: Log what type was actually returned
            logger.info(
                f"Turn {case.current_turn} response type: {type(response_obj).__name__}"
            )

            # 4. Apply state from the final accepted response (exactly once)
            case_updated, response_metadata = (
                await self.responses.process_response_structured(
                    case, user_message, response_obj, attachments, upload_report
                )
            )
            # Merge response metadata with early metadata (which may have transition_proposed_this_turn)
            metadata.update(response_metadata)

            # 4a. Stage-gate compliance is now handled via LLM milestone output
            # (Framework §4.1). The LLM sets stage-gate milestones in its
            # structured response; side effects are applied in
            # _apply_investigation_updates → _apply_stage_gate_side_effects.

            # Phase 1: No-Op Detection happens at 4b below, not here. This is
            # where the reading USED to be taken, five lines before
            # ``_check_automatic_transitions`` wrote the arm it scores (#1270).
            #
            # The provisional reading that briefly stood here is gone rather than
            # kept: nothing between this point and 4b reads ``progress_made``
            # (``_check_automatic_transitions`` writes ``status_transitioned``
            # and the readiness verdicts, and reads neither), so it decided
            # nothing — while leaving a value on the shared dict that is
            # provisional BY DESIGN, which is the read-before-write hazard this
            # fix exists to remove, re-armed for whatever gets inserted here
            # next. One decision point per turn.
            #
            # This is not the "move the block" the issue warned against: the
            # block's other statements stay where they are, and deleting a dead
            # call touches less than keeping it.
            # Outcome is already set by _process_response_structured (default) or applied updates (LLM choice)

            # 4. Check for automatic status transitions
            case_updated = await self.transitions.check_automatic_transitions(
                case_updated, metadata, user_message
            )

            # 4b. Re-score progress now that EVERY arm writer has run (#1270).
            #
            # ``status_transitioned`` is one of the nine arms
            # ``check_if_progress_made`` scores, and ``_check_automatic_transitions``
            # is its only writer on this path — five lines AFTER the read above.
            # So an automatic INQUIRY→INVESTIGATING transition never counted as
            # progress, and ``turns_without_progress`` climbed through a turn
            # that demonstrably advanced the case. Measured on the local corpus
            # before this fix: 158 of 170 cases with an observable
            # INQUIRY→INVESTIGATING turn scored that turn ``progress_made=False``,
            # every visible arm empty. The 12 exceptions all carried an upload,
            # so ``novel_files_uploaded`` — written back at Step 0 — fired for
            # them independently.
            #
            # HERE rather than by moving the read: this is the first point at
            # which every arm writer has run, and it precedes every reader —
            # the stall counter at Step 5.8, the turn record at Step 6, and the
            # #1142 telemetry handoff all re-read ``metadata`` rather than a
            # local. ``_check_automatic_transitions`` never reads
            # ``progress_made``, so scoring after it is not circular, and
            # ``_perform_hypothesis_housekeeping`` (the only other thing between
            # here and Step 5.8) reads ``metadata["system_feedback"]``, the
            # ``case.turns_without_progress`` ATTRIBUTE, which Step 5.8 updates
            # afterwards either way — so anti-anchoring sees the same value it
            # saw before — and ``metadata["progress_made"]``, which decides
            # whether the turn ages ignored priors. That last read is why
            # housekeeping must stay AFTER this call.
            #
            # The invariant this restores is the one the deterministic path
            # already states: every arm is written before the read. See
            # ``_finish_deterministic_turn``.
            score_progress(metadata)

            # 4c. Resolution backstop (INV-43). LAST of the openers, which is
            # what makes it a backstop rather than a fourth competing proposer:
            # step 4 has just run the LLM's own ``proposed_transition`` and the
            # deferred proposer ran inside the apply step before it, so anything
            # they opened is standing on ``pending_transition`` and this bails on
            # it. Only a resolution-READY case that NOBODY offered reaches the
            # proposal. Placed after 4b rather than before it because the offer
            # is engine action, not case progress — it writes none of the arms
            # ``score_progress`` reads.
            _maybe_propose_confirmed_resolution(case_updated, metadata)

            # 5. Phase 4: Hypothesis Housekeeping (Decay & Anchoring)
            # This happens after transitions but before recording the turn
            _perform_hypothesis_housekeeping(
                self.deps.hypothesis_manager,
                case_updated,
                metadata,
                investigation_advanced=metadata["progress_made"],
            )

            # Step 5.5: Calculate progress metrics
            progress_metrics = calculate_progress_metrics(
                case=case_updated, current_turn=case_updated.current_turn
            )
            metadata["momentum"] = progress_metrics.investigation_momentum
            metadata["blocked_reasons"] = progress_metrics.blocked_reasons
            metadata["next_steps"] = progress_metrics.next_steps

            # Step 5.6: Generate working conclusion EVERY turn during INVESTIGATING
            # Gap #7: Working Conclusion Every Turn
            # Reference: Prompt Engineering Guide Section 11.7
            # Why: Provides consistent context tracking, prevents "lost context" issues
            _refresh_working_conclusion(case_updated)
            if case_updated.working_conclusion is not None:
                logger.debug(
                    "Working conclusion updated: likelihood="
                    f"{case_updated.working_conclusion.likelihood:.2f}"
                )

            # Step 5.7: Validate state consistency
            is_valid, validation_issues = self.deps.state_validator.is_valid(
                case_updated
            )
            validation_repairs: list[str] = []
            if validation_issues:
                # Log validation issues and collect repairs
                for issue in validation_issues:
                    if issue.severity == ValidationSeverity.ERROR:
                        logger.warning(
                            f"State validation error: {issue.code} - {issue.message}"
                        )
                        if issue.suggested_fix:
                            validation_repairs.append(
                                f"{issue.code}: {issue.suggested_fix}"
                            )
                    elif issue.severity == ValidationSeverity.WARNING:
                        logger.debug(
                            f"State validation warning: {issue.code} - {issue.message}"
                        )
                metadata["validation_issues"] = [
                    {"code": i.code, "message": i.message, "severity": i.severity.value}
                    for i in validation_issues
                ]

            # Step 5.8: Update progress tracking (before stagnation check)
            if metadata.get("progress_made", False):
                case_updated.turns_without_progress = 0
            else:
                case_updated.turns_without_progress += 1

            # Step 5.9: Progress monitoring (before recording turn)
            # Check if transparent mode should activate and/or repair
            # patterns are detected. Replaces the old stagnation detector.
            progress_result = self.deps.progress_monitor.check_progress(case_updated)
            stagnation_str: str | None = None
            if progress_result:
                # Record repair pattern if detected
                if progress_result.repair_type:
                    stagnation_str = progress_result.repair_type.value
                    metadata["stagnation_type"] = progress_result.repair_type.value
                    metadata["breakout_action"] = progress_result.repair_action

                metadata["progress_transparent"] = True
                metadata["pending_milestone"] = progress_result.pending_milestone
                metadata["milestone_description"] = (
                    progress_result.milestone_description
                )

                # Store prompt injection in system_feedback for next turn
                if progress_result.prompt_injection:
                    current_feedback = metadata.get("system_feedback", "") or ""
                    metadata["system_feedback"] = (
                        f"{current_feedback}\n{progress_result.prompt_injection}".strip()
                    )

                log_msg = (
                    f"Progress transparency activated: pending milestone "
                    f"'{progress_result.pending_milestone}'"
                )
                if progress_result.repair_type:
                    log_msg += f", repair: {progress_result.repair_type.value}"
                logger.info(log_msg)

            # Step 6: Record turn progress
            turn_record = _create_turn_record(
                turn_number=case_updated.current_turn,
                milestones_completed=metadata.get("milestones_completed", []),
                evidence_added=metadata.get("evidence_added", []),
                hypotheses_generated=metadata.get("hypotheses_generated", []),
                hypotheses_validated=metadata.get("hypotheses_validated", []),
                solutions_proposed=metadata.get("solutions_proposed", []),
                progress_made=metadata.get("progress_made", False),
                outcome=metadata.get("outcome", TurnOutcome.CONVERSATION),
                user_message=user_message,
                agent_response=response_obj.agent_response,
                agent_response_synthesized=(
                    is_agent_response_synthesized(response_obj)
                    or not response_obj.agent_response.strip()
                ),
                system_feedback=metadata.get("system_feedback"),
                momentum=progress_metrics.investigation_momentum,
                blocked_reasons=progress_metrics.blocked_reasons,
                next_steps=progress_metrics.next_steps,
                repair_pattern=stagnation_str,
                # The state validator's repairs, then everything the turn's
                # apply steps recorded on ``metadata["validation_repairs"]`` —
                # the schema's confidence repairs (fm#1502) and the apply-time
                # rejections. The latter were appended to that key and read by
                # nothing, so no turn record carried them; this is where the
                # channel lands. The #1142 telemetry count below stays the state
                # validator's alone, which is what that stream documents.
                validation_repairs=[
                    *validation_repairs,
                    *metadata.get("validation_repairs", []),
                ],
            )
            case_updated.turn_history.append(turn_record)

            # Evidence-needs Phase 3: run the supersession rule for
            # causal-purpose needs anchored to any TERMINAL hypothesis. Covers
            # every terminal write path without threading ``case`` through
            # their APIs:
            #   - hypothesis_manager.py (low-confidence retirement)
            #   - hypothesis_manager.py (anchoring-prevention retirement)
            #   - hypothesis_manager.py (``refute_hypothesis``)
            #   - progress_monitor.py (INCONCLUSIVE → RETIRED)
            #   - milestone_engine/engine.py (LLM-emitted refutation / retirement)
            #
            # The FULL terminal set is swept, not a newly-terminal diff. The
            # helper is idempotent — it removes the id from every motivating
            # list on the first pass, so later sweeps hit its ``continue`` and
            # change nothing — which makes the steady-state cost a no-op and
            # removes the need for a pre-turn snapshot. The diff form could
            # only ever supersede needs whose motivator turned terminal in the
            # same turn, so a need already carrying a terminal id (a motivator
            # that went terminal before this rule existed, or one left in the
            # list beside a still-active motivator) stayed PENDING for the life
            # of the case with nothing able to clear it. Sweeping everything
            # self-heals those instead of requiring a backfill.
            #
            # Runs BEFORE save() so the supersession lands in the same turn's
            # persisted state.
            _sweep_needs_for_terminal_hypotheses(case_updated)

            # #1079: give every EVIDENCE suggestion a need to hang on, and
            # record the ask on it. Both anti-nagging mechanisms (the
            # obtainability wall and mention decay) act on an EvidenceNeed, so
            # an ask with no need behind it is one neither can ever see — which
            # is how the same request survived ten consecutive turns against a
            # user declining it six times.
            #
            # Placed here for two ordering reasons: BEFORE save() so created
            # needs and the recorded turn persist with the rest of the turn,
            # and BEFORE _flatten_follow_ups (below) so the wire response
            # carries the IDs assigned here. After the terminal sweep, so a need
            # superseded this turn is not a match candidate.
            # Skipped when the engine is going to REPLACE these suggestions
            # further down (gate affordances, the resolution/close prose
            # branches, the closure ack). Those turns never render the model's
            # EVIDENCE asks, and recording an ask the user never saw would decay
            # it toward "stop surfacing" for the wrong reason.
            # GC for engine-inferred needs. They are the orphan shape the
            # terminal-hypothesis sweep above cannot reach (no motivator to key
            # off) — the same shape ``_apply_evidence_need_updates`` refuses to
            # let the MODEL create, for that exact reason. Run before linking so
            # an ask repeated THIS turn is refreshed rather than swept a moment
            # early, and unconditionally (not inside the suggestion branch) so a
            # case that stops emitting suggestions still gets its pool cleaned.
            try:
                sweep_silent_inferred_needs(case_updated, case_updated.current_turn)
            except Exception as sweep_err:  # noqa: BLE001
                logger.warning(
                    "Inferred-need sweep failed on case %s: %s",
                    case_updated.case_id,
                    sweep_err,
                )

            if getattr(response_obj, "suggested_follow_ups", None):
                try:
                    gate_pending = (
                        engine_owned_affordances(case_updated, metadata) is not None
                    )
                    if suggestions_are_engine_replaced(
                        case_updated, metadata, gate_pending
                    ):
                        logger.debug(
                            "Skipping evidence-need linking on case %s turn %s: "
                            "the engine replaces this turn's suggestions",
                            case_updated.case_id,
                            case_updated.current_turn,
                        )
                    else:
                        link_evidence_suggestions_to_needs(
                            case_updated,
                            response_obj.suggested_follow_ups,
                            metadata,
                            case_updated.current_turn,
                            _resolve_id_ref,
                        )
                except Exception as link_err:  # noqa: BLE001
                    # Never fail a turn over suggestion bookkeeping — the reply
                    # is still correct without the linkage, just un-countable.
                    # Counted, not merely logged: a systematic failure here
                    # turns the whole fix off, and a flat created/matched rate
                    # reads identically to a model that started declaring its
                    # own needs.
                    logger.warning(
                        "Evidence-need linking failed on case %s: %s",
                        case_updated.case_id,
                        link_err,
                    )
                    try:
                        evidence_suggestion_unlinked_total.labels(
                            resolution="error"
                        ).inc()
                    except Exception:
                        pass

            # Step 7: Save case (only if changes made, but turn history always updates)
            case_updated.updated_at = datetime.now(UTC)
            case_updated.last_activity_at = datetime.now(UTC)
            await self.deps.repository.save(case_updated)

            # Step 7b: Auto-generate terminal summary synchronously on
            # terminal transition. The rendered summary (or skip / failure
            # note) is appended to the agent reply below so it appears in
            # chat at the moment of generation — consistent with the
            # explicit-confirmation path. `summary_failed` flags an LLM-
            # error so the ack-turn follow-ups can include the regen
            # affordance (G2).
            summary_payload: str | None = None
            summary_failed: bool = False
            if metadata.get("status_transitioned") and case_updated.state in (
                CaseState.RESOLVED,
                CaseState.CLOSED,
            ):
                summary_payload, summary_failed = (
                    await self.terminal.auto_generate_report(case_updated)
                )

            logger.info(
                f"Turn {case_updated.current_turn} processed successfully. "
                f"Status: {case_updated.state}, "
                f"Progress made: {metadata.get('progress_made', False)}"
            )

            # Extract follow-up suggestions from LLM response
            follow_ups: list[dict[str, Any]] = []
            if (
                hasattr(response_obj, "suggested_follow_ups")
                and response_obj.suggested_follow_ups
            ):
                follow_ups = _flatten_follow_ups(
                    response_obj.suggested_follow_ups, metadata
                )

            # Persist redaction registry for cross-turn consistency
            await redaction_ctx.save()

            # A synthesized placeholder (#1442) is not the model's reply, so
            # the compositions below start from NOTHING rather than from it:
            # an engine gate notice then stands alone instead of arriving under
            # "[No response generated]", and the turn is a real engine answer
            # rather than a placeholder. Restored below, before the turn record
            # is re-read, only when nothing was composed.
            response_synthesized = is_agent_response_synthesized(response_obj)
            agent_response_text = (
                "" if response_synthesized else response_obj.agent_response
            )

            # Post-LLM overrides for resolution readiness re-evaluation.
            # Gate PROSE is composed with (appended below) the LLM's reply via
            # _prose_with_gate_notice — never replacing the analysis the user
            # asked for. Gate SUGGESTIONS stay engine-owned replacements.
            # After a needs_info turn, check whether requirements are now met.
            #
            # ``gate_prose_appended`` records whether one of the PROSE
            # branches fired: each frames the not-yet-terminal state below the
            # LLM's reply, so the INV-40 guard suppresses on it. The
            # suggestions-only branch (override_suggestions) appends NO prose,
            # so the guard must still
            # fire there (INV-40 — a proposed transition alone does not
            # contradict a "Case resolved." narration).
            gate_prose_appended = False
            # Set when Gate 1 composed its statement, and checked against the
            # FINAL reply at the return boundary — see the counter there.
            _gate1_presented_statement: str | None = None
            if metadata.get("resolution_ready_for_confirmation"):
                agent_response_text = _prose_with_gate_notice(
                    agent_response_text,
                    "Thanks for the additional details.\n\n"
                    + _build_resolution_confirmation(case_updated),
                )
                follow_ups = _resolution_confirmation_suggestions()
                gate_prose_appended = True
            elif metadata.get("resolution_suggest_close"):
                # User didn't provide required info — suggest Close instead.
                agent_response_text = _prose_with_gate_notice(
                    agent_response_text,
                    metadata["resolution_readiness_message"],
                )
                follow_ups = _close_confirmation_suggestions()
                gate_prose_appended = True
            elif metadata.get("resolution_needs_info_first_pass"):
                # LLM proposed RESOLVED but readiness check returned NEEDS_INFO.
                # Append the readiness ask below the LLM's agent_response so
                # the user sees both the turn's analysis and the same
                # missing-info ask the readiness gate produces.
                agent_response_text = _prose_with_gate_notice(
                    agent_response_text,
                    metadata["resolution_needs_info_message"],
                )
                follow_ups = metadata["override_suggestions"]
                gate_prose_appended = True
            elif metadata.get("close_pivoted_to_resolve"):
                # INV-37 resolve-preservation: the user confirmed a pending
                # CLOSE, but the case had become resolvable — the confirm-time
                # guard pivoted it to a RESOLVED proposal. Append (below the
                # LLM's reply) the SUGGEST_RESOLVE prose the guard already
                # computed and stored on the resolved pending — the same text
                # the proposal-time pivot shows, so both pivot paths render one
                # message.
                agent_response_text = _prose_with_gate_notice(
                    agent_response_text,
                    (case_updated.pending_transition or {}).get("summary", ""),
                )
                follow_ups = _resolution_confirmation_suggestions()
                gate_prose_appended = True
            elif metadata.get("rca_infeasible_closure_message"):
                # Stage-gate side effect: mitigation_verified + rca_infeasible=True.
                # Append the engine-built closure proposal below the LLM's
                # mitigation-confirmation reply, with the canonical close
                # confirm/decline pair.
                agent_response_text = _prose_with_gate_notice(
                    agent_response_text,
                    metadata["rca_infeasible_closure_message"],
                )
                follow_ups = metadata["override_suggestions"]
                gate_prose_appended = True
            elif metadata.get("deferred_solution_gate_message"):
                # Deferred-implementation disposition: the ENGINE proposed this
                # one, so its rationale has to be rendered the same way the
                # rca_infeasible sibling's is. Without this the key was written
                # and never read, and the user got a bare confirm/decline pair
                # with no stated reason — on the close branch, a "without
                # resolution" affordance sitting directly under LLM prose that
                # had just said it would not propose closure (case_fa29e0023b85
                # turns 11-15). Must precede the generic override_suggestions
                # branch below, which swaps suggestions but appends NO prose.
                agent_response_text = _prose_with_gate_notice(
                    agent_response_text,
                    metadata["deferred_solution_gate_message"],
                )
                follow_ups = metadata["override_suggestions"]
                gate_prose_appended = True
            elif metadata.get("resolution_ready_gate_message"):
                # Resolution backstop (INV-43): the ENGINE opened this
                # handshake because the cause is confirmed eliminated and no
                # other opener proposed it. Same treatment as its two
                # engine-proposed siblings above — the rationale is composed
                # below the model's reply, because an offer the user did not
                # ask for and the model did not narrate is otherwise two
                # unexplained buttons. Must precede the generic
                # override_suggestions branch, which appends no prose.
                agent_response_text = _prose_with_gate_notice(
                    agent_response_text,
                    metadata["resolution_ready_gate_message"],
                )
                follow_ups = metadata["override_suggestions"]
                gate_prose_appended = True
            elif metadata.get("override_suggestions"):
                # ProposedTransition was emitted by the LLM this turn (either
                # detecting solution success or routing user-expressed
                # transition intent). Replace the LLM's follow-ups with the
                # canonical confirm/decline pair so both remaining openers
                # (the model's proposed_transition via this branch, and the
                # engine's own INV-43 backstop) converge on the same
                # deterministic confirmation UX. There used to be a third, the
                # resolve dropdown; it went when RESOLVED left the menu. NOTE: no prose is
                # appended here, so the INV-40 guard below still runs — an
                # over-claiming narration on this branch is corrected.
                follow_ups = metadata["override_suggestions"]

            # Engine-owned gate affordances. When a state-machine gate is
            # pending (Gate 1 — problem-statement confirmation; or a
            # pending_transition disposition handshake), the engine
            # emits the canonical clickable affordance pair regardless of
            # LLM compliance with the prompt's suggestion-emission
            # directives. The consolidator is a single source of truth that
            # replaced the previously-scattered handshake-deferred / Gate 2
            # / Gate 3 branches. Gate 1 now fires on every Gate-1-pending
            # turn (not only the handshake-deferred recovery turn) — the
            # architectural completion that makes Gate 1 symmetric with
            # Gate 2 and Gate 3, and removes LLM compliance from the
            # correctness path. See INV-01, INV-19, INV-21.
            #
            # It also drives the mid-investigation correctives (code-guarded,
            # always on): the insufficient-evidence structured handoff (a
            # work-gated stall with no grounded cause), the restatement-held
            # handoff (#1195 — the same stall where the block is the cause's
            # phrasing rather than missing data) and the NOT_YET_PRODUCTIVE
            # pull-back (a persisted 0-hypothesis vacuum — #656 P3.1). All read a
            # FRESH grounding grade because this runs after
            # ``_apply_investigation_updates`` recomputed cause_state this turn
            # (the #593 re-derive-after-stamp ordering the plan requires).
            gate_result = engine_owned_affordances(case_updated, metadata)
            if gate_result is not None:
                gate_name, gate_affordances = gate_result
                # REPLACE the LLM's suggestions with the engine-owned gate
                # affordances. This is the engine↔LLM suggestion-ownership
                # boundary:
                #
                #   A suggestion answers "what is the user's next move?"
                #   - STATE-MACHINE moves (confirm/refine a gate, close,
                #     resolve) advance the case's formal lifecycle. The ENGINE
                #     owns them: only it knows the valid transitions, can
                #     attach deterministic ``intent``, and can guarantee the
                #     affordance is clickable every turn (INV-01).
                #   - CONTENT moves (share data, explore an angle, describe
                #     symptoms) advance the investigation's content. The LLM
                #     owns these — but only when NO gate is pending, in which
                #     case ``engine_owned_affordances`` returns None and the
                #     LLM's suggestions pass through untouched (above).
                #
                # When a gate IS pending the case is BLOCKED on a state-machine
                # decision, so the gate moves are the only meaningful next
                # moves — content moves are premature (you cannot gather
                # investigation data before the problem is even confirmed).
                # The engine therefore owns the whole list. A tangential user
                # question on a gate turn is answered in the agent's PROSE, not
                # via suggestions; the next-move affordances stay confirm/refine.
                #
                # The insufficient-evidence handoff replaces for a parallel
                # reason: on a work-gated stall the LLM's own suggestions are the
                # least trustworthy (this is exactly the turn a weak model
                # fabricates a cause or spins), so the engine overrides them with
                # honest keep-engaging moves. The model's *content* — what
                # specifically would decide it — still lands in the PROSE.
                #
                # We do NOT augment (append the LLM's suggestions). The LLM,
                # asked to confirm, naturally emits its OWN confirm/decline
                # suggestions ("Yes, that's correct. Let's investigate." / "No,
                # that's not quite right."), which carry no ``intent`` and would
                # render as duplicate, overlapping buttons beside the engine's
                # authoritative pair (observed on case_d22ebbd63784). Relevance
                # on a gate turn comes from the gate opening ONLY when it should
                # (intent detection — Answer First), not from mixing in the
                # LLM's premature/duplicate suggestions.
                # Gate 1 is the one gate whose affordances refer to a piece
                # of CASE TEXT, so it is the one gate that has to ship that
                # text with them. INV-26(b) already says the visible transcript
                # may not contradict the applied state_updates and enforces it
                # through ``_prose_with_gate_notice`` for every disposition
                # gate; Gate 1 predates that mechanism and was never brought
                # under it. It is now.
                #
                # Composed, never substituted: whatever the user actually asked
                # this turn stays above, and the statement lands below it. That
                # is the #430 boundary holding — the engine owns the gate's
                # SUGGESTIONS outright, and composes (does not replace) prose.
                #
                # ``gate_prose_appended`` is deliberately NOT set: this block
                # frames the INQUIRY problem, not a disposition, so it does not
                # contradict a "case resolved" over-claim. INV-40 must still be
                # free to fire on the same turn.
                if gate_name == "gate1":
                    _gate1_presented_statement = (
                        case_updated.inquiry.proposed_problem_statement or ""
                    ).strip()
                    agent_response_text = _prose_with_gate_notice(
                        agent_response_text,
                        _gate1_statement_presentation(case_updated),
                    )

                follow_ups = gate_affordances
                engine_owned_affordance_served_total.labels(gate=gate_name).inc()
                logger.info(
                    "engine_owned_affordances_served",
                    extra={
                        "case_id": case_updated.case_id,
                        "turn": case_updated.current_turn,
                        "gate": gate_name,
                        "affordance_count": len(gate_affordances),
                    },
                )
                # Record the verification status on the turn when the handoff
                # fired. This turn-metadata copy is the return-boundary signal;
                # the durable reading lives on ``case.progress.verification_status``
                # (persisted each turn). The affordance-served metric above
                # already carries the firing count per gate.
                if gate_name in _GATE_VERIFICATION_STATUS:
                    metadata["verification_status"] = _GATE_VERIFICATION_STATUS[
                        gate_name
                    ].value

            # Closure-ack turn (LLM-driven path): when generation
            # succeeded, suggestions stay minimal — the rendered summary
            # is right above and a regen card next to it would be noise.
            # When generation failed, include the regen affordance so the
            # user can retry immediately (G2 — the "noise" guard doesn't
            # apply when there's no inline summary).
            if metadata.get("status_transitioned") and case_updated.state in (
                CaseState.RESOLVED,
                CaseState.CLOSED,
            ):
                remaining = await _remaining_regens_for(
                    self.deps.report_service, self.deps.repository, case_updated
                )
                follow_ups = _select_ack_follow_ups(
                    case_updated, summary_failed, remaining
                )

            # Append the synthesized summary (or skip / failure note) so it
            # appears in chat at the moment of generation. The composed reply
            # is persisted by the caller (investigation_service step 4) from
            # the returned ``agent_response`` — turn_history records are
            # frozen and carry only a summary, never the chat text.
            if summary_payload:
                agent_response_text = (
                    f"{agent_response_text}\n\n{summary_payload}".strip()
                )

            # INV-40 (§7.9): narration-truth coherence guard. The narration
            # channel (agent_response) is LLM free text and sits outside every
            # truth surface the §7.6 reconciliation lane reads — so an LLM that
            # narrates "Case resolved." on a case the engine holds at
            # INVESTIGATING (the #668 incident, 3/3 on long-context haiku)
            # delivers a false disposition claim the user acts on. Reconcile the
            # existing narrow completion-phrase scan against engine truth and,
            # when it over-claims, APPEND a corrective notice below the LLM's
            # prose (the INV-26 composition lane, never a substitution — the DF-4
            # lesson). This runs after the summary append above, so a genuine
            # terminal transition (state now RESOLVED/CLOSED) is excluded by
            # construction; the guard fires only on the truth-split.
            # ``gate_prose_appended`` suppresses the guard on the branches
            # that already appended a state-framing gate notice — but NOT on the
            # suggestions-only override branch, whose bare proposed_transition
            # leaves an over-claim uncontradicted (the guard's likeliest shape).
            # Scans what the MODEL wrote, not the composed turn. Gate prose is
            # engine-authored, and Gate 1's carries the user's own problem
            # statement verbatim — a statement reading "users report the case
            # resolved itself overnight" would otherwise trip the completion
            # scan and have the engine contradict its own presentation.
            _overclaim_notice = _narration_overclaim_notice(
                case_updated,
                response_obj.agent_response,
                gate_prose_appended=gate_prose_appended,
            )
            if _overclaim_notice is not None:
                agent_response_text = _prose_with_gate_notice(
                    agent_response_text, _overclaim_notice
                )
                narration_overclaim_total.labels(
                    provider=_resolve_chat_provider_name(self.deps.llm_provider)
                ).inc()
                logger.warning(
                    "narration_overclaim_corrected",
                    extra={
                        "case_id": case_updated.case_id,
                        "turn": case_updated.current_turn,
                        "state": case_updated.state.value,
                    },
                )

            # The turn record (step 6) summarized the RAW LLM text; the gate,
            # summary, and INV-40 compositions above changed only the returned
            # reply. Re-record the summary channel when they diverge: the
            # next-turn prompt (context_builder) and the turn_outcome
            # heuristics read ``agent_response_summary``, so without this the
            # model is replayed its own uncorrected over-claim (the #668 loop
            # INV-40 exists to break) and terminal summaries vanish from
            # long-case state prompts. TurnProgress is frozen — replace the
            # record, never mutate; the caller's step-4 save persists it
            # alongside the messages.
            # Nothing composed onto a synthesized placeholder: it IS the reply,
            # and stays flagged. Anything composed replaced it with engine
            # prose, which is a real answer and is not flagged.
            if response_synthesized and not agent_response_text.strip():
                agent_response_text = response_obj.agent_response
            else:
                response_synthesized = False
            if (
                case_updated.turn_history
                and agent_response_text != response_obj.agent_response
            ):
                case_updated.turn_history[-1] = case_updated.turn_history[
                    -1
                ].model_copy(
                    update={
                        "agent_response_summary": summarize_for_turn_record(
                            agent_response_text, 500
                        ),
                        # Re-derived with the text, never carried over: the
                        # record step 6 wrote described the raw reply.
                        "agent_response_synthesized": (not agent_response_text.strip()),
                    }
                )

            # Compliance instrumentation: per-turn signal on whether the LLM
            # is honoring the transition-handling prompt rules. Used for
            # quarterly drift review across model-version changes and prompt
            # growth. Cheap regex on agent_response checks for completion
            # phrases the rule explicitly forbids.
            #
            # Scope (INV-15 §1.3.1): scan is deliberately narrow — only
            # transition-completion claims. The broader _ADVISOR_ROLE_-
            # CONSTRAINT banned-phrase list ("Let me check", "I will run",
            # etc.) is NOT scanned here because those phrases have higher
            # false-positive rates in legitimate context. If broader
            # advisor-role drift detection becomes valuable, add a
            # separately-tagged "advisor_role_compliance" log signal
            # alongside this one — don't dilute the transition_compliance
            # tuple. See investigation-lifecycle-logic.md §1.3.1
            # (INV-15 drift note). The scan reuses the module-level
            # _COMPLETION_PHRASES via _narration_asserts_disposition, so the
            # telemetry and the INV-40 guard share ONE scan implementation (not
            # just one phrase list) — no re-implemented any(...) to drift.
            # Capture LLM-vs-engine drift on the proposed-transition path.
            # When the LLM emits to_state=resolved on a thin case, the engine
            # pivots to closed (see _check_automatic_transitions). Recording
            # the pivot here lets us compare LLM intent against engine action
            # over time without diffing log lines.
            _llm_proposed = getattr(
                getattr(response_obj, "state_updates", None),
                "proposed_transition",
                None,
            )
            _llm_proposed_to_status = (
                getattr(_llm_proposed, "to_state", None) if _llm_proposed else None
            )
            _engine_to_status = (
                case_updated.pending_transition.get("to_state")
                if case_updated.pending_transition
                else None
            )
            _transition_pivoted = bool(
                _llm_proposed_to_status
                and _engine_to_status
                and _llm_proposed_to_status != _engine_to_status
            )
            logger.info(
                "transition_compliance",
                extra={
                    "case_id": case_updated.case_id,
                    "turn": case_updated.current_turn,
                    "state": case_updated.state.value,
                    "proposed_transition_emitted": bool(
                        metadata.get("transition_proposed_this_turn")
                    ),
                    "llm_proposed_to_status": _llm_proposed_to_status,
                    "engine_effective_to_status": _engine_to_status,
                    "transition_pivoted": _transition_pivoted,
                    "user_confirmed_investigation_emitted": bool(
                        getattr(
                            getattr(response_obj, "state_updates", None),
                            "user_confirmed_investigation",
                            False,
                        )
                    ),
                    # The model's own narration, for the same reason the INV-40
                    # guard above reads it: attributing an engine-composed
                    # phrase to the model corrupts the telemetry it feeds.
                    "agent_response_contains_completion_phrase": (
                        _narration_asserts_disposition(response_obj.agent_response)
                    ),
                    "status_transitioned": bool(metadata.get("status_transitioned")),
                    # Readiness verdicts explain WHY a proposed transition did
                    # not transition this turn (pending confirmation /
                    # needs_info / pivot) — without them a pending handshake
                    # reads as a silent gate refusal (#656 triage).
                    "resolution_readiness_verdict": metadata.get(
                        "resolution_readiness_verdict"
                    ),
                    "resolution_readiness_missing": metadata.get(
                        "resolution_readiness_missing"
                    ),
                    "closure_readiness_verdict": metadata.get(
                        "closure_readiness_verdict"
                    ),
                },
            )

            # INV-01 outcome check. Counting at the composition site would be
            # a second rule-fire counter for one rule fire — the ratio against
            # the affordance counter would read 1.0 by construction, two
            # adjacent lines apart, and could not detect anything. Verified
            # HERE instead, against the text actually returned, so anything
            # that drops or mangles the block between composition and return
            # shows up as the gap the alert is written for.
            if _gate1_presented_statement:
                if _gate1_presented_statement in agent_response_text:
                    gate1_statement_composed_total.inc()
                else:
                    logger.error(
                        "gate1_statement_missing_from_reply",
                        extra={
                            "case_id": case_updated.case_id,
                            "turn": case_updated.current_turn,
                        },
                    )

            return {
                "agent_response": agent_response_text,
                "suggested_follow_ups": follow_ups,
                "case_updated": case_updated,
                "redaction_ctx": redaction_ctx,
                "metadata": {
                    "turn_number": case_updated.current_turn,
                    "milestones_completed": metadata.get("milestones_completed", []),
                    "progress_made": metadata.get("progress_made", False),
                    "status_transitioned": metadata.get("status_transitioned", False),
                    "outcome": metadata.get("outcome", TurnOutcome.CONVERSATION),
                    "momentum": metadata.get("momentum"),
                    "next_steps": metadata.get("next_steps", []),
                    # Verification-status Phase 1: the insufficient-evidence
                    # handoff records the status on the internal working dict;
                    # surface it here so it crosses the return boundary (the
                    # calibration eval / Phase-3 persistence read it). Absent
                    # (None) on turns the handoff did not fire.
                    "verification_status": metadata.get("verification_status"),
                    "timestamp": datetime.now(UTC).isoformat(),
                    # The turn's uploads, on the SAME footing as on the
                    # deterministic branches (#1229). This return rebuilds
                    # metadata from a fixed key list rather than forwarding the
                    # working dict, so a key added to that dict does not reach a
                    # caller unless it is named here — and the two upload keys
                    # were not, which made an identical file visible on a gate
                    # turn and invisible on an ordinary one. Spread rather than
                    # ``.get()``-ed so the keys stay ABSENT on a turn with no
                    # uploads, which is what every consumer expects and what the
                    # deterministic branches do. The service persists this dict
                    # onto the assistant ``case_messages`` row, so it is durable,
                    # not merely returned.
                    **{
                        k: metadata[k]
                        for k in ("files_uploaded", "novel_files_uploaded")
                        if k in metadata
                    },
                    # #1451: the reply is a placeholder this engine wrote. The
                    # service persists this dict onto the assistant row, which
                    # is what tells every renderer not to quote it. Absent,
                    # not False, on an answered turn — the same footing as the
                    # service backstop that writes this key.
                    **(
                        {MESSAGE_METADATA_AGENT_SYNTHESIZED: True}
                        if response_synthesized
                        else {}
                    ),
                    # #1142 handoff. Four of the nine arms
                    # ``check_if_progress_made`` scores — ``novel_evidence_added``,
                    # ``novel_solutions_proposed``, ``status_transitioned``,
                    # ``hypothesis_evidence_links_applied`` — live only on the
                    # working dict above and are written nowhere, so
                    # ``progress_made`` is currently recorded without the evidence
                    # for WHY. Counted here, at the point of decision, and read by
                    # the service one frame up.
                    #
                    # Underscore-prefixed and POPPED by the service before the
                    # returned metadata is persisted onto the assistant
                    # ``case_messages`` row: unlike the keys above this is
                    # monitoring data, and that row is readable through the
                    # transcript API.
                    TELEMETRY_HANDOFF_KEY: {
                        "path": TurnPath.LLM,
                        "arms": collect_progress_arms(metadata),
                        "gate_name": gate_result[0] if gate_result else None,
                        "validation_repairs": len(validation_repairs),
                        "repair_pattern": stagnation_str,
                    },
                },
            }

        except StaleCaseException:
            # OCC conflict on the case row — the route handler maps this
            # to HTTP 409. Do NOT wrap in MilestoneEngineError, or the
            # type identity is lost and the handler falls through to 500.
            raise
        except Exception as e:
            # Use LLMErrorHandler's classification instead of duplicating patterns
            is_external = self.deps.llm_error_handler.is_retryable_error(e)

            if is_external:
                logger.warning(
                    f"External service error for case {case.case_id}: {str(e)[:200]}",
                    extra={"case_id": case.case_id, "turn": case.current_turn},
                )
            else:
                logger.error(
                    f"Error processing turn for case {case.case_id}: {e}",
                    exc_info=True,
                    extra={"case_id": case.case_id, "turn": case.current_turn},
                )

            raise MilestoneEngineError(
                f"Turn processing failed: {e}",
                error_code=getattr(e, "error_code", None),
            ) from e

    # =========================================================================
    # Prompt Generation
    # =========================================================================

    # ================================================================
    # Vectorization tracking (v5.2) — mechanical safety nets
    # Same pattern as deep_analysis_count / MAX_DEEP_ANALYSIS above.
    # ================================================================

    #: Re-exported from the tool that owns it so all emission sites
    #: carry the same text and the same rule. They used to hold separate copies (#941).
    _VECTORIZED_SYSTEM_MESSAGE = VECTORIZED_SYSTEM_MESSAGE

    # =========================================================================
    # Response Processing
    # =========================================================================

    # Post-010 (strict evidence model): NO evidence creation during
    # INQUIRY. Evidence presupposes a confirmed claim; during INQUIRY
    # the claim is still being formed. Uploaded files persist in
    # ``case.uploaded_files`` with their preprocessing artifacts
    # (summary, structural_index, data_type, coverage timestamps);
    # the LLM evaluates them and emits ``evidence_to_add`` once the
    # case transitions to INVESTIGATING.
    # See docs/architecture/investigation-engine/
    # evidence-driven-investigation-framework.md §5.

    # =========================================================================
    # Evidence Need apply-layer (Phase 3 of evidence-needs rollout)
    # =========================================================================

    # =========================================================================
    # State Management
    # =========================================================================

    # v3: `_check_fast_track_resolution` and `KB_FAST_TRACK_THRESHOLD` removed.
    # KB-driven cases route through INVESTIGATING via the KB-resolution
    # milestone collapse. See indicator-resolution.md +
    # investigation-lifecycle-logic.md §1.2 INVESTIGATING → RESOLVED →
    # KB-Resolution Path. The collapse is state authoring only, applied in
    # `_apply_investigation_updates`'s `knowledge_resolution` branch (gate
    # milestones set there); RootCauseConclusion + Solution are populated
    # from the LLM's structured emissions in the same turn. The RESOLVED
    # disposition still requires the explicit confirm turn (#722).

    # =========================================================================
    # Helper Methods
    # =========================================================================

    # #1210: ``_create_uploaded_file_from_attachment`` is gone. The engine does
    # not mint ``UploadedFile`` rows — ``investigation_service
    # ._preprocess_attachment`` persists the authoritative row and appends it to
    # the case aggregate before ``process_turn`` runs. The row the engine built
    # from the attachment metadata was a strict subset of that one and was
    # discarded on every turn once #1209 gated the append.

    # Post-010: auto-Evidence creation at file-upload time is gone.
    # Under the strict evidence model, files are data (uploaded_files)
    # and evidence is a claim-anchored extract that the LLM produces
    # via evidence_to_add during INVESTIGATING. The previous
    # ``_create_evidence_from_attachment`` and ``_infer_evidence_category``
    # helpers (auto-DOCUMENT path) have been removed.

    # =============================================================================
    # Phase 4 Housekeeping & Helpers
    # =============================================================================


# =============================================================================
# Exceptions
# =============================================================================
