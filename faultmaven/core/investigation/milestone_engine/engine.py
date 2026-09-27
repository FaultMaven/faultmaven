"""MilestoneEngine: process_turn and the investigation-turn state machine."""

import asyncio
import logging
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

# Module initialization
logger = logging.getLogger(__name__)


from faultmaven.core.investigation.hypothesis_manager import (
    create_hypothesis_manager,
)
from faultmaven.core.investigation.llm_error_handler import (
    LLMErrorHandler,
)
from faultmaven.core.investigation.milestone_engine.dependencies import EngineDeps
from faultmaven.core.investigation.milestone_engine.errors import MilestoneEngineError
from faultmaven.core.investigation.milestone_engine.generation import (
    StructuredOutputGenerator,
)
from faultmaven.core.investigation.milestone_engine.hypothesis_updates import (
    _apply_hypothesis_action_intent,
)
from faultmaven.core.investigation.milestone_engine.kb_prefetch import KbPrefetcher
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
from faultmaven.core.investigation.milestone_engine.transition_turns import (
    _close_on_explicit_intent,
    _confirm_pending_transition,
    _decline_bare_reply,
    _represent_pending_transition,
)
from faultmaven.core.investigation.milestone_engine.transitions import TransitionManager
from faultmaven.core.investigation.milestone_engine.turn_application import (
    _apply_turn_response,
)
from faultmaven.core.investigation.milestone_engine.turn_completion import (
    _compose_turn_reply,
    _persist_turn,
)
from faultmaven.core.investigation.milestone_engine.turn_generation import (
    _generate_turn_response,
)
from faultmaven.core.investigation.milestone_engine.turn_records import (
    _report_turn_uploads,
)
from faultmaven.core.investigation.milestone_engine.vectorization import (
    EvidenceVectorizer,
)
from faultmaven.core.investigation.progress_monitor import ProgressMonitor
from faultmaven.core.investigation.state_validator import (
    StateValidator,
)
from faultmaven.infrastructure.llm.metering import (
    TurnTokenTracker,
    active_token_tracker,
)
from faultmaven.models.interfaces import ILLMProvider
from faultmaven.modules.agent.tools.vectorize_file_tool import (
    VECTORIZED_SYSTEM_MESSAGE,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
    TurnOutcome,
)
from faultmaven.modules.case.domain.services.case_action_manager import (
    earned_edge_refusal,
)
from faultmaven.modules.case.exceptions import StaleCaseException
from faultmaven.modules.knowledge.contracts import IKnowledgeService

from .affordances import (
    gate1_statement_is_confirmable,
)
from .response_synthesis import (
    _DISPOSITION_GATE_ANSWERED_KEY,
    _note_engine_disposition_withdrawn,
    _record_deferred_disposition_decline,
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
                        return await _confirm_pending_transition(
                            self.deps.checkpoint_service,
                            self.deps.report_service,
                            self.deps.repository,
                            self.terminal,
                            case=case,
                            upload_report=upload_report,
                            user_message=user_message,
                        )
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
                            return await _decline_bare_reply(
                                self.deps.repository,
                                case=case,
                                upload_report=upload_report,
                                user_message=user_message,
                            )
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
                            return await _represent_pending_transition(
                                self.deps.repository,
                                case=case,
                                stripped_message=stripped_message,
                                upload_report=upload_report,
                                user_message=user_message,
                            )

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
                    return await _close_on_explicit_intent(
                        self.deps.repository,
                        assess_closure_readiness=assess_closure_readiness,
                        case=case,
                        propose_transition=propose_transition,
                        upload_report=upload_report,
                        user_message=user_message,
                    )

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
            redaction_ctx, response_obj = await _generate_turn_response(
                self.deps.investigation_tools,
                self.deps.llm_provider,
                self.deps.redis_client,
                self.deps.repository,
                self.deps.sanitizer,
                self.generator,
                case=case,
                intent_data=intent_data,
                user_id=user_id,
                user_message=user_message,
            )

            # 4. Apply state from the final accepted response (exactly once)
            case_updated, stagnation_str, validation_repairs = (
                await _apply_turn_response(
                    self.deps.hypothesis_manager,
                    self.deps.progress_monitor,
                    self.deps.state_validator,
                    self.responses,
                    self.transitions,
                    attachments=attachments,
                    case=case,
                    metadata=metadata,
                    response_obj=response_obj,
                    upload_report=upload_report,
                    user_message=user_message,
                )
            )

            # Step 7: Save case (only if changes made, but turn history always updates)
            follow_ups, summary_failed, summary_payload = await _persist_turn(
                self.deps.repository,
                self.terminal,
                case_updated=case_updated,
                metadata=metadata,
                redaction_ctx=redaction_ctx,
                response_obj=response_obj,
            )

            # A synthesized placeholder (#1442) is not the model's reply, so
            # the compositions below start from NOTHING rather than from it:
            # an engine gate notice then stands alone instead of arriving under
            # "[No response generated]", and the turn is a real engine answer
            # rather than a placeholder. Restored below, before the turn record
            # is re-read, only when nothing was composed.
            return await _compose_turn_reply(
                self.deps.llm_provider,
                self.deps.report_service,
                self.deps.repository,
                case_updated=case_updated,
                follow_ups=follow_ups,
                metadata=metadata,
                redaction_ctx=redaction_ctx,
                response_obj=response_obj,
                stagnation_str=stagnation_str,
                summary_failed=summary_failed,
                summary_payload=summary_payload,
                validation_repairs=validation_repairs,
            )

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
