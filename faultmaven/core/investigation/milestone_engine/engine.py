"""MilestoneEngine: process_turn and the investigation-turn state machine."""

import asyncio
import logging
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

# Module initialization
logger = logging.getLogger(__name__)


from faultmaven.core.investigation.hypothesis_manager import create_hypothesis_manager
from faultmaven.core.investigation.lifecycle_metrics import (
    inquiry_handshake_deferred_total,
)
from faultmaven.core.investigation.llm_error_handler import LLMErrorHandler
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
    gate1_bare_consent,
    gate1_offer_key,
    offer_click_refusal,
    opens_with_consent_loosely,
    pending_gate_verdict,
    terminal_offer_key,
)
from faultmaven.core.investigation.milestone_engine.transition_turns import (
    _close_on_explicit_intent,
    _confirm_pending_transition,
    _decline_bare_reply,
    _refuse_offer_click,
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
from faultmaven.core.investigation.state_validator import StateValidator
from faultmaven.core.investigation.terminal_transitions import is_question
from faultmaven.infrastructure.llm.metering import (
    TurnTokenTracker,
    active_token_tracker,
)
from faultmaven.infrastructure.llm.usage_ledger import (
    capture_attribution,
    flush_turn,
    warn_attribution_error,
)
from faultmaven.models.interfaces import ILLMProvider
from faultmaven.modules.agent.tools.vectorize_file_tool import VECTORIZED_SYSTEM_MESSAGE
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

from .affordances import _gate1_is_pending, gate1_statement_is_confirmable
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


# The pending-transition gate's consumption rule (#1783, ruling (a)). Without
# an LLM turn, it answers with the proposal's buttons, every time and never
# recording a refusal: a consent-shaped reply that is not bare and that
# ``is_substantive_reply`` does not call substantive (the set the gate used to
# execute on); a reply whose text and minted intent disagree; a minted
# confirmation on text that is not bare; a status-dropdown re-pick of the
# pending target (#1838); and a short (at most this many characters)
# question-free non-answer ("hm"). It NEVER consumes a turn carrying an upload,
# nor a non-answer longer than this or carrying a question mark in any script
# (``is_question``, #1840) — new evidence, a question, an instruction to keep
# investigating: those withdraw the proposal and are processed as a normal
# investigation turn, so the gate can never swallow them.
_PENDING_GATE_SUBSTANTIVE_LEN = 40


def _commit_gate1(case: Case, *, via: str) -> None:
    """Commit Gate 1 (the problem-statement confirmation) on ``case``.

    The one commit section 0c makes, whichever of its two readers decided it:
    a confirmation intent (a click naming the statement shown, or a mint on a
    bare token), or a bare typed consent while Gate 1 is pending, with no
    intent at all (#1841, ruling (b)). One helper, so the two cannot drift.
    ``via`` names the reader, for the log line only.

    There is no path fork (redesign R5): the investigation proceeds
    opportunistically once INVESTIGATING begins. Nothing transitions here:
    ``_check_automatic_transitions`` fires INQUIRY -> INVESTIGATING on Gate 1
    alone. Committed before the LLM call, so the write guard in
    ``_apply_inquiry_updates`` drops a same-turn rewording of the statement.
    """
    case.inquiry.problem_statement_confirmed = True
    case.inquiry.problem_statement_confirmed_at = datetime.now(UTC)
    logger.info(
        f"Case {case.case_id}: Gate 1 confirmed via {via} "
        f"(transitioning to INVESTIGATING)"
    )


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
        typed: bool = False,
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
            typed: True when the service MINTED ``intent_type`` from typed text
                (the intent resolver) rather than receiving it from a click. It
                names how a terminal transition was confirmed (#1748): a typed
                "ok" the resolver turned into a confirmation is not a click. A
                keyword of its own, not an ``intent_data`` key: that dict is
                filled from the client's intent payload, and server facts never
                ride in it (the same reason ``user_id`` is kept out).

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
            #
            # Who pays and who acted are captured HERE, in the context every
            # call of the turn runs in, and the turn's address with them: the
            # service has already advanced the message clock for this turn, so
            # ``current_turn`` at entry IS this turn's number (#640).
            #
            # Capturing them must not fail the turn: a raise here leaves the
            # turn unattributed (counted ``attribution_error`` at the flush) and
            # the turn runs on.
            tracker = TurnTokenTracker(actor_user_id=user_id or "")
            turn_number = 0
            investigation_turn = 0
            try:
                turn_number = case.current_turn
                tracker.attribution = capture_attribution(user_id)
                investigation_turn = case.investigation_turn_at(turn_number)
            except Exception as exc:
                tracker.attribution = None
                tracker.attribution_failed = True
                investigation_turn = 0
                warn_attribution_error(exc)
            token = active_token_tracker.set(tracker)
            try:
                result = await self._process_turn_impl(
                    case,
                    user_message,
                    attachments,
                    intent_type,
                    intent_data,
                    user_id=user_id,
                    typed=typed,
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
                # Persist the turn's spend (#640). Fails open by contract —
                # flush_turn never raises — so a ledger outage cannot cost a
                # turn its answer; what it did not persist is counted instead.
                await flush_turn(
                    tracker,
                    case_id=case.case_id,
                    turn_number=turn_number,
                    investigation_turn=investigation_turn,
                )
            return result

    async def _process_turn_impl(
        self,
        case: Case,
        user_message: str,
        attachments: list[dict[str, Any]] | None = None,
        intent_type: str | None = None,
        intent_data: dict[str, Any] | None = None,
        user_id: str | None = None,
        typed: bool = False,
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

            # Set when section 0b answered this turn's confirmation click: it
            # named the standing offer, and the gate executed or declined it. A
            # declined click whose payload is substantive falls through to 0c
            # with no offer left standing, and 0c must not refuse it there.
            click_answered_by_gate = False

            # 0b. Pending transition confirmation — short-circuit before LLM
            # When a pending transition exists (User-Agent Handshake), check if
            # the user is confirming or declining BEFORE calling the LLM. This
            # avoids unnecessary LLM calls and prevents schema validation errors
            # from blocking the confirmation.
            #
            # Two detection paths (checked in order):
            # 1. Intent-based: DECIDE suggestion clicks carry
            #    intent_type="confirmation" + confirmation_value — deterministic,
            #    and the offer the card presents (``proposal_id``). A click
            #    naming any other offer is refused before the verdict (#1812).
            #    A status-dropdown pick names a state, not an offer: a re-pick
            #    of the pending target re-shows the card and executes nothing
            #    (#1838).
            # 2. Pattern-based: fallback for users who type instead of clicking.
            #    Only a BARE consent token executes (#1783); a longer typed
            #    reply is re-asked.
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
                    or is_question(stripped_message)
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
                    # The answer the turn's intent carries, if any.
                    intent_confirms = (
                        intent_type == "confirmation"
                        and (intent_data or {}).get("value") is True
                    )
                    # A status_transition intent naming the pending target. A
                    # MINTED one (``typed``) carries a confirmation whose text
                    # decides, as any mint does. A CLICKED one is the status
                    # dropdown picked again, and is NOT consent (#1838, ruling
                    # (b)): the pick names a state, not an offer, and a double
                    # submit, a client retry or a second tab sends it as surely
                    # as a deliberate second pick. It is forced to a re-ask
                    # below, never left to its text: the dropdown's text ("Close
                    # this case as unresolved. Summarize what we found so
                    # far.") is over the substantive bound, and as text it
                    # would escape and be recorded as a refusal.
                    status_transition_confirms = (
                        intent_type == "status_transition"
                        and (intent_data or {}).get("to_state")
                        == case.pending_transition.get("to_state")
                    )
                    status_repick_clicked = status_transition_confirms and not typed
                    intent_declines = (
                        intent_type == "confirmation"
                        and (intent_data or {}).get("value") is False
                    )
                    intent_value: bool | None = (
                        True
                        if intent_confirms or status_transition_confirms
                        else False if intent_declines else None
                    )
                    # A CLICK answers only the offer it names (#1812, ruling
                    # (a)). One that names another offer, or none, executes
                    # nothing, withdraws nothing and records nothing: the reply
                    # says so and re-shows the standing offer. A minted intent
                    # (``typed``) is not a click, so the text decides and the
                    # key is not consulted.
                    if (intent_confirms or intent_declines) and not typed:
                        refusal = offer_click_refusal(
                            intent_data, terminal_offer_key(case.pending_transition)
                        )
                        if refusal is not None:
                            return await _refuse_offer_click(
                                self.deps.repository,
                                case=case,
                                upload_report=upload_report,
                                user_message=user_message,
                                standing="terminal",
                                reason=refusal,
                            )
                        click_answered_by_gate = True
                    # One verdict from the text and the intent together
                    # (#1783, ruling (a)). A terminal proposal executes only
                    # on its click or on a BARE typed consent token; an
                    # intent the service minted from typed text (``typed``)
                    # is not a click, and never overrides the text. The
                    # verdict also names how the user confirmed, for the
                    # turn record (#1748). A clicked re-pick of the pending
                    # target is a re-ask that records nothing (#1838): the
                    # standing offer's card comes back, and only its click or
                    # a bare typed consent executes it.
                    if status_repick_clicked:
                        verdict, confirmed_via = "reask", None
                    else:
                        verdict, confirmed_via = pending_gate_verdict(
                            user_message,
                            case.pending_transition.get("to_state"),
                            intent_value=intent_value,
                            typed=typed,
                        )
                    # A turn carrying an upload is never consumed by the gate:
                    # the file is new data and must be analysed, whatever the
                    # caption says ("logs", "", "ok here are the logs"). Only
                    # a consent executes on it, as it always did.
                    turn_carries_upload = bool(attachments)

                    if verdict == "confirm":
                        return await _confirm_pending_transition(
                            self.deps.checkpoint_service,
                            self.deps.report_service,
                            self.deps.repository,
                            self.terminal,
                            case=case,
                            upload_report=upload_report,
                            user_message=user_message,
                            confirmed_via=confirmed_via,
                        )
                    elif verdict == "decline":
                        # Record the refusal BEFORE cancelling: the cancel is
                        # what erases the provenance this reads (fm#1122).
                        _record_deferred_disposition_decline(case)
                        _note_engine_disposition_withdrawn(case, metadata)
                        cancel_pending_transition(case)

                        if message_is_substantive or turn_carries_upload:
                            # The decline carries substance beyond a bare
                            # "no" — new data, a question, a redirection
                            # ("no, we did not do anything yet — did you
                            # see anything wrong?"), or an upload. The
                            # proposal is withdrawn; the message itself must
                            # still be processed as a normal turn so nothing
                            # the user said or sent is swallowed by the gate.
                            logger.info(
                                f"Pending transition declined with a "
                                f"substantive message or an upload for case "
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
                        # ``reask`` (consent-shaped but not bare, text and
                        # minted intent disagree, or a clicked re-pick of the
                        # pending target, #1838), or ``not_an_answer``. A
                        # SUBSTANTIVE non-answer (long, or carrying a question)
                        # is not an answer to the gate at all, and neither is
                        # a turn carrying an upload: holding the gate against
                        # those swallowed every typed turn with no LLM call
                        # and bricked the case (#656, turns 12-13). The
                        # proposal is withdrawn instead and the message
                        # processed as a normal turn; the engine can always
                        # re-propose later from fresher state. Everything else
                        # — a re-ask, a short question-free reply ("hmm"),
                        # blank input — is answered with the proposal's
                        # buttons again, every time it is sent, and never
                        # recorded as a refusal: every proposal is terminal,
                        # and a re-ask must never become a decline (#1783,
                        # ruling (a); the one-re-present cap #656 added is
                        # gone). None of these is worth an LLM turn.
                        text_escapes = (
                            verdict == "not_an_answer" and message_is_substantive
                        )
                        if text_escapes or turn_carries_upload:
                            # The offer is withdrawn either way; whether that
                            # is a REFUSAL splits on the two halves of
                            # message_is_substantive, which the gate
                            # deliberately conflates. A QUESTION (a question
                            # mark in any script, ``is_question``; #1840) is a
                            # user deciding — "what happens to the runbook if
                            # I close this?" — and recording it would make the
                            # affordance disappear, unexplained, until a
                            # premise moved: the same engine-acts-without-
                            # saying-why defect this PR family exists to kill.
                            # A long non-question non-answer is a deflection
                            # ("we'll do it in Friday's window") and IS a
                            # refusal. Either way the withdrawal is noted for
                            # the turn, because the fall-through below reaches
                            # _maybe_propose_deferred_close again and would
                            # otherwise re-take the affordances on this very
                            # turn (fm#1122). An upload is not a refusal: it
                            # is withdrawn and recorded only by the text rule.
                            # A reply that OPENS with consent is not a
                            # deflection either (#1808): "Yes, go ahead and
                            # close it. We verified …" is withdrawn and
                            # processed, and the offer may come back. Read
                            # LOOSELY here, and only here (#1840): "*Yes*, …",
                            # "_Yes_, …" and a "Yes" behind an invisible
                            # character open with consent too. The gate did
                            # not take this turn (its readers stay strict), so
                            # the LLM sees it either way; reading loosely
                            # costs at most a refusal left unrecorded.
                            if (
                                text_escapes
                                and not is_question(stripped_message)
                                and not opens_with_consent_loosely(stripped_message)
                            ):
                                _record_deferred_disposition_decline(case)
                            _note_engine_disposition_withdrawn(case, metadata)
                            cancel_pending_transition(case)
                            logger.info(
                                f"Pending transition withdrawn for case "
                                f"{case.case_id}: message is not a gate "
                                f"answer (substantive="
                                f"{message_is_substantive}, upload="
                                f"{turn_carries_upload}) — processing "
                                f"message normally"
                            )
                            # Fall through to normal processing (section 0c)
                        else:
                            return await _represent_pending_transition(
                                self.deps.repository,
                                case=case,
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
            # detector anywhere: ``pending_gate_verdict`` only answers a
            # STANDING pending, and ``IntentResolver`` matches typed text
            # against suggestions already on screen. A typed "mark this
            # resolved" with nothing standing reaches the state machine solely
            # by the MODEL emitting ``proposed_transition``.
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

                # A CLICK answers only the offer it names (#1812, ruling (a)).
                # Not one 0b answered (``click_answered_by_gate``). The only
                # offer a click can answer here is Gate 1: while it is pending,
                # every other click is checked against its key, whatever else
                # is pending. With Gate 1 not pending, a click on a standing
                # ``needs_info`` offer (which 0b skips) falls through to the LLM
                # as it always did, and any other click finds nothing standing.
                gate1_standing = _gate1_is_pending(case)
                if (
                    not typed
                    and not click_answered_by_gate
                    and (
                        gate1_standing
                        or not (case.pending_transition or {}).get("needs_info")
                    )
                ):
                    refusal = offer_click_refusal(
                        intent_data,
                        (
                            gate1_offer_key(case.inquiry.proposed_problem_statement)
                            if gate1_standing
                            else None
                        ),
                    )
                    if refusal is not None:
                        return await _refuse_offer_click(
                            self.deps.repository,
                            case=case,
                            upload_report=upload_report,
                            user_message=user_message,
                            standing="gate1" if gate1_standing else None,
                            reason=refusal,
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
                elif typed and not gate1_bare_consent(user_message):
                    # A MINTED confirmation commits Gate 1 only on a bare
                    # consent token (#1794, ruling (a)), the same screen as
                    # the LLM's flag and the adoption guard. Nothing commits;
                    # the turn is processed normally, Gate 1 stays pending and
                    # the engine composes its card (#1607).
                    inquiry_handshake_deferred_total.labels(reason="not_bare").inc()
                    logger.info(
                        f"Case {case.case_id}: minted Gate 1 confirmation on "
                        f"text that is not one bare consent token — not "
                        f"committed, processing the message normally"
                    )
                else:
                    _commit_gate1(case, via="confirmation intent")

            # ============================================================
            # GATE 1 - A bare typed consent, with no intent (#1841)
            # ============================================================
            # Gate 1's presentation says "reply with the single word yes"
            # (#1814), so the engine reads that reply itself while Gate 1 is
            # pending, as ``pending_gate_verdict`` reads the terminal gate
            # (#1841, ruling (b)). Without this, a typed "yes" committed only
            # through the LLM's flag or a resolver mint, and an aside that had
            # cleared ``last_suggestions`` left the resolver nothing to match.
            # The LLM's ``user_confirmed_investigation`` and the resolver mint
            # stay screened as before (``gate1_bare_consent``, #1794). Same
            # commit as the click branch above (``_commit_gate1``).
            elif (
                intent_type in (None, "conversation")
                and _gate1_is_pending(case)
                and gate1_bare_consent(user_message)
            ):
                _commit_gate1(case, via="a bare typed consent")

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
