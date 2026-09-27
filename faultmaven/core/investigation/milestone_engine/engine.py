"""MilestoneEngine: process_turn and the investigation-turn state machine."""

import asyncio
import difflib
import inspect
import json
import logging
import re
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any, Callable, Optional
from uuid import uuid4

# Module initialization
logger = logging.getLogger(__name__)

#: ``MilestoneEngine._resolve_link_confidence``'s answer for a link that must
#: not be written (fm#1502). A sentinel, because ``None`` already means "keep
#: the stored value".
_PRUNE_LINK = object()

from faultmaven.core.investigation.case_telemetry import (
    TELEMETRY_HANDOFF_KEY,
    TurnPath,
    collect_progress_arms,
)
from faultmaven.core.investigation.causal_graph.derivation import derive_node_states
from faultmaven.core.investigation.causal_graph.ingestion import (
    chain_path_to_problem,
    ingest_emitted_chain,
    mirror_hypothesis_support_to_root_nodes,
)
from faultmaven.core.investigation.causal_graph.pruning import (
    prune_abandoned_nodes,
    resolve_orphan_chains,
)
from faultmaven.core.investigation.causal_graph.queries import is_chain_root_validated
from faultmaven.core.investigation.causal_graph.similarity import (
    find_duplicate_hypothesis,
)
from faultmaven.core.investigation.causal_graph.support import (
    support_count_held_root_ids,
)
from faultmaven.core.investigation.cause_assurance import absence_row_link_refused
from faultmaven.core.investigation.confidence_repair import (
    CONFIDENCE_REPAIRS_CONTEXT_KEY,
    CONFIDENCE_UNREPAIRABLE,
    MEANING_PRESERVING_ACTIONS,
    ConfidenceAction,
    ConfidenceRepair,
    settle_set_aside_link,
)
from faultmaven.core.investigation.confidence_repair import (
    count as count_confidence_repair,
)
from faultmaven.core.investigation.evidence_need_linking import (
    link_evidence_suggestions_to_needs,
    suggestions_are_engine_replaced,
    sweep_silent_inferred_needs,
)
from faultmaven.core.investigation.hypothesis_manager import (
    HypothesisManager,
    create_hypothesis_manager,
)
from faultmaven.core.investigation.lifecycle_metrics import (
    engine_owned_affordance_served_total,
    evidence_need_created_total,
    evidence_need_id_dropped_total,
    evidence_need_status_changed_total,
    evidence_suggestion_unlinked_total,
    gate1_statement_composed_total,
    hypothesis_dedup_skipped_total,
    hypothesis_root_adoption_refused_total,
    inquiry_classified_without_statement_total,
    inquiry_handshake_deferred_total,
    narration_overclaim_total,
    prompt_context_recovery_total,
)
from faultmaven.core.investigation.llm_error_handler import (
    LLMErrorHandler,
    OutputTruncationError,
    classify_token_limit_reason,
    is_output_truncation_error,
    is_truncated_json_error,
)
from faultmaven.core.investigation.progress_monitor import ProgressMonitor
from faultmaven.core.investigation.prompts.templates.assembly import get_prompt_for_case
from faultmaven.core.investigation.reliability_metrics import (
    schema_validation_total,
    tool_call_attempts_total,
)
from faultmaven.core.investigation.schemas import (
    BaseInteractionResponse,
    InquiryResponse,
    TerminalResponse,
    get_schema_for_stage,
)
from faultmaven.core.investigation.state_validator import (
    StateValidator,
    ValidationSeverity,
)
from faultmaven.core.investigation.tool_loop_metrics import (
    tool_result_chars,
    tool_result_relayed_total,
    tool_result_truncated_total,
)
from faultmaven.core.investigation.turn_uploads import report_turn_uploads
from faultmaven.core.investigation.verification_status import is_stalled
from faultmaven.core.investigation.working_conclusion_generator import (
    calculate_progress_metrics,
)
from faultmaven.exceptions import TOKEN_LIMIT, LLMErrorCategory
from faultmaven.infrastructure.llm.json_response import (
    json_payload_text,
    loads_llm_json,
)
from faultmaven.infrastructure.llm.metering import (
    TurnTokenTracker,
    active_token_tracker,
    record_provider_call,
)
from faultmaven.infrastructure.llm.providers import ReasoningIntent, StopReason
from faultmaven.infrastructure.llm.structured_output_capability import (
    StructuredOutputMode,
)
from faultmaven.infrastructure.llm.truncation import generate_with_truncation_retry
from faultmaven.models.interfaces import ILLMProvider
from faultmaven.modules.agent.tools.vectorize_file_tool import (
    VECTORIZED_SYSTEM_MESSAGE,
    append_vectorization_advisory,
)
from faultmaven.modules.case.contracts import (
    MESSAGE_METADATA_AGENT_SYNTHESIZED,
    TERMINAL_HYPOTHESIS_STATES,
    Case,
    CaseAction,
    CaseState,
    CauseState,
    ConfidenceLevel,
    Evidence,
    EvidenceNeed,
    HypothesisState,
    InvestigationActionType,
    InvestigationMomentum,
    InvestigationProgress,
    InvestigationStage,
    JournalEntry,
    KnowledgeMatch,
    KnowledgeResolution,
    MessageRowKind,
    NeedObtainability,
    NeedPriority,
    NeedPurpose,
    NeedState,
    NodeType,
    ProblemVerification,
    ProposedAction,
    RootCauseConclusion,
    Solution,
    SolutionFeasible,
    TemporalState,
    TurnOutcome,
    TurnProgress,
    UrgencyLevel,
    append_message_row,
)
from faultmaven.modules.case.domain.services.case_action_manager import (
    earned_edge_refusal,
)
from faultmaven.modules.case.exceptions import StaleCaseException
from faultmaven.modules.knowledge.contracts import IKnowledgeService

from .affordances import (
    _GATE_VERIFICATION_STATUS,
    _restates_standing_evidence,
    _restates_standing_solution,
    _schema_prompt_instruction,
    engine_owned_affordances,
    gate1_statement_is_confirmable,
)
from .cause_state import (
    _gate1_statement_presentation,
    _kb_prefetch_query_on_identification,
    _recompute_assessment_state,
    _resolve_chat_provider_name,
)
from .kb_prefetch import (
    KB_CONTEXT_MAX_ENTRIES,
    KB_PREFETCH_FETCH_LIMIT,
    KB_PREFETCH_RELEVANCE_THRESHOLD,
    _admit_diverse,
    _chunk_label,
)
from .milestone_inference import (
    _apply_symptom_retraction,
    _evidence_coverage,
    _infer_milestones,
    _post_process_llm_response,
    _resolve_evidence_source,
    validate_reasoning_first,
)
from .progress import (
    check_if_progress_made,
    confirmed_transition_arms,
    record_promptless_turn,
    score_progress,
    summarize_for_turn_record,
)
from .response_synthesis import (
    _COMPLETION_PHRASES,
    _DISPOSITION_GATE_ANSWERED_KEY,
    _NARRATION_OVERCLAIM_NOTICE,
    _NARRATION_OVERCLAIM_NOTICE_PENDING,
    _narration_asserts_disposition,
    _note_engine_disposition_withdrawn,
    _prose_with_gate_notice,
    _record_deferred_disposition_decline,
    is_agent_response_synthesized,
    schema_answer_stop_reason,
    synthesized_agent_response,
)
from .stage_gates import (
    _add_system_feedback,
    _apply_stage_gate_signals,
    _case_has_symptom_evidence,
    _close_confirmation_suggestions,
    _coerce_intervention_quadrant,
    _determine_action_type,
    _matches_gate_token,
    _normalise_id_ref,
    _refresh_working_conclusion,
    _route_toolless_turn_single_shot,
    _settled_working_conclusion,
    _should_force_tools,
    _solution_cause_validated,
    _supersede_pending_solution_offers,
    llm_claimable_milestones,
)
from .terminal_proposals import (
    _maybe_propose_confirmed_resolution,
    _maybe_propose_deferred_close,
    _sweep_needs_for_terminal_hypotheses,
)
from .terminal_replies import (
    GENERATE_RUNBOOK_ANYWAY_PAYLOAD,
    GENERATE_RUNBOOK_PAYLOAD,
    _build_resolution_confirmation,
    _closed_suggestions,
    _compose_terminal_reply,
    _generate_runbook_anyway_suggestion,
    _resolution_confirmation_suggestions,
    _resolved_suggestions,
    _select_ack_follow_ups,
)
from .text_budget import (
    _TOOL_LOOP_ELISION_MARKER,
    KB_QA_ANSWER_TRUNCATED_MARKER,
    KB_QA_RELAY_PREFIX,
    KB_QA_RELAY_SUFFIX,
    _elide_answer_middle,
    _is_context_length_error,
    _tool_loop_message_tokens,
    _tool_payload_tokens,
    _ToolLoopBudget,
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

# Anti-anchoring acts at most once per this many turns (a marker on
# progress.last_anti_anchoring_turn records when it last fired). With a value of
# 2 the intervention skips the single turn immediately after it fires, then may
# act again — enough to avoid per-turn churn without going dormant.
_ANTI_ANCHORING_COOLDOWN_TURNS = 2

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

# Generation cap for schema-bound calls, and the ceiling the truncation ladder
# may raise it to. Investigation schemas (``_Verification`` especially) are
# large and turn 2+ carries substantial context, so the starting cap is
# generous; the ceiling bounds how much a single turn may spend chasing an
# answer that keeps overrunning. Reaching the ceiling is the signal to switch
# levers — from "give the answer more room" to "give the answer more room by
# shrinking the question" (the #662 minimal-prompt degrade).
STRUCTURED_OUTPUT_MAX_TOKENS = 8000
# Visible-output floor declared with ``ReasoningIntent.INFERENCE`` on the
# tool-less single-shot diagnostic call (fm#1116). INFERENCE lifts the
# provider's starvation guards, so the router requires a floor (#1117).
# Anchor: the full Diagnosis body on the replayed case_bf484a484a77 turn 9
# measured 1,495-1,756 completion tokens at effort none and 1,641-1,8xx at
# low/medium (probe results_reasoning.jsonl); 2048 sits above every observed
# body and well under STRUCTURED_OUTPUT_MAX_TOKENS, so the floor never raises
# the cap and only forbids a starvable partition.
TOOLLESS_INFERENCE_OUTPUT_FLOOR = 2048
STRUCTURED_OUTPUT_MAX_TOKENS_CEILING = 16000


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
        """
        self.llm_provider = llm_provider
        self.repository = repository
        self.knowledge_service = knowledge_service
        self.trace_enabled = trace_enabled
        self.checkpoint_service = checkpoint_service
        self.investigation_tools = investigation_tools
        self.da_provider = da_provider
        self.da_model = da_model
        self.sanitizer = sanitizer
        self.redis_client = redis_client
        self.report_service = report_service
        self.team_service = team_service
        self.share_repository = share_repository
        self.runbook_kb = runbook_kb
        self.hypothesis_manager = create_hypothesis_manager()
        self.state_validator = StateValidator()
        self.progress_monitor = ProgressMonitor()
        self.llm_error_handler = LLMErrorHandler()

        # G10: Per-case asyncio locks to prevent concurrent process_turn
        # calls on the same case from interleaving and corrupting state
        self._case_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

        # In-flight proactive vectorization tasks, keyed by evidence_id.
        # MilestoneEngine is a DI singleton, so this dict survives across
        # turns. The persistent Evidence.vectorized flag covers the
        # "already completed" state; this dict covers the "currently
        # running" window between start and completion. Without it, turn
        # N+1 sees vectorized=False (still running) and starts a second
        # concurrent task for the same evidence — the stacking pattern
        # that drove every task past the 60s wait_for bound in the
        # 2026-04-21 test run.
        self._inflight_vectorize: dict[str, asyncio.Task] = {}

        logger.info("MilestoneEngine initialized with structured output engine")

    async def _remaining_regens_for(self, case: "Case") -> int:
        """How many regenerations the user has left for this case's
        canonical terminal summary (RESOLUTION_SUMMARY for RESOLVED,
        CLOSURE_SUMMARY for CLOSED).

        Drives both the label suffix on the regen affordance and the
        "hide when exhausted" gate. Returns ``MAX_REGENERATIONS`` (the
        cap) when the repository or report service is unavailable
        (test/degraded paths) — preserves the legacy behaviour of always
        showing the affordance when the count cannot be checked.

        Counted from the persisted ``reports`` table (each generation
        writes a new row). See ICaseRepository.count_reports.
        """
        from faultmaven.modules.case.contracts import ReportType

        if self.report_service is None or self.repository is None:
            return getattr(self.report_service, "MAX_REGENERATIONS", 5)
        if case.state == CaseState.RESOLVED:
            report_type = ReportType.RESOLUTION_SUMMARY
        elif case.state == CaseState.CLOSED:
            report_type = ReportType.CLOSURE_SUMMARY
        else:
            # Non-terminal cases have no regen affordance at all; the
            # value is unused by callers but keep it self-consistent.
            return getattr(self.report_service, "MAX_REGENERATIONS", 5)
        try:
            count = await self.repository.count_reports(case.case_id, report_type)
        except Exception:
            # Best-effort: if counting fails, don't strand the user
            # without an affordance. Fall back to the cap.
            return getattr(self.report_service, "MAX_REGENERATIONS", 5)
        max_regens = getattr(self.report_service, "MAX_REGENERATIONS", 5)
        return max(0, max_regens - count)

    async def _case_has_runbook_draft(self, case: "Case") -> bool:
        """Whether a runbook draft has already been generated for this case.

        Drives the "hide once used" gate on the Generate-runbook affordance:
        each case gets at most one chat-side generation. Re-rolls happen in
        the Dashboard Drafts editor, not via repeated chat clicks.

        Returns False (i.e. "show the affordance") on any lookup failure or
        when the conversion service isn't wired — preserves the legacy
        behaviour of always offering the affordance when state is unknown,
        which is the safer default for a forward action.
        """
        conversion_service = getattr(self, "conversion_service", None)
        if conversion_service is None:
            return False
        try:
            drafts = await conversion_service.list_drafts_for_case(case.case_id)
        except Exception:
            return False
        return any(d for d in drafts)

    async def _auto_generate_report(self, case: "Case") -> tuple[str | None, bool]:
        """Synchronous auto-generation of terminal summary.

        RESOLVED cases always generate (a confirmed solution is meaningful
        content by definition). CLOSED cases generate only when the
        substance gate passes — gated by
        ``should_generate_terminal_summary``.

        Returns:
            A tuple ``(payload, generation_failed)``:

            - ``(rendered_markdown, False)`` on success — embed inline.
            - ``(failure_note, True)`` on LLM exception — embed inline AND
              offer the regen affordance on the ack-turn (G2).
            - ``(skip_note, False)`` when the substance gate skipped
              generation (CLOSED-only path).
            - ``(None, False)`` when no report service is configured.

        Callers embed ``payload`` in the closure-turn agent reply and use
        ``generation_failed`` to decide whether to offer the regen
        affordance on the ack-turn. Exceptions are caught and reported as
        a return value rather than propagated — the closure state
        transition has already committed and must not be undone by a
        synthesis-LLM hiccup.
        """
        from faultmaven.core.investigation.terminal_transitions import (
            should_generate_terminal_summary,
            terminal_summary_skip_reason,
        )

        if case.state == CaseState.CLOSED and not should_generate_terminal_summary(
            case
        ):
            skip = terminal_summary_skip_reason(case)
            logger.info(f"Auto-summary skipped for case {case.case_id}: {skip}")
            return skip, False

        if not self.report_service:
            logger.debug("No report service available — skipping auto-summary")
            return None, False

        from faultmaven.modules.case.domain.owned_models.report import ReportType

        if case.state == CaseState.RESOLVED:
            report_type = ReportType.RESOLUTION_SUMMARY
            report_label = "Resolution summary"
        elif case.state == CaseState.CLOSED:
            report_type = ReportType.CLOSURE_SUMMARY
            report_label = "Closure summary"
        else:
            logger.warning(
                f"Unexpected state {case.state} for auto-summary on case {case.case_id}"
            )
            return None, False

        try:
            # generate_reports returns ReportGenerationResponse; its
            # .reports field is the list of newly-persisted CaseReports.
            response = await self.report_service.generate_reports(case, [report_type])
            logger.info(
                f"Auto-generated {report_type.value} for case {case.case_id}",
                extra={"case_id": case.case_id, "report_type": report_type.value},
            )
            # Pull the rendered markdown content from the freshly-generated
            # report so it can be embedded in the closure-turn reply.
            if response.reports:
                content = response.reports[0].content
                if content:
                    return content, False
            return None, False
        except Exception as e:
            logger.warning(
                f"Auto-summary generation failed for case {case.case_id}: {e}",
                extra={"case_id": case.case_id},
            )
            return (
                f"{report_label} generation did not complete. "
                f"You can retry from the **Regenerate** option.",
                True,
            )

    # Only the precomposed payloads submitted by the DECIDE regen
    # suggestions reach this set. Free-typed summary-shaped requests
    # (e.g. "give me a recap", "summarize what we discussed") fall through
    # to terminal Q&A on purpose: typing should never produce a persisted
    # Report side effect. The Q&A prompt is instructed to redirect those
    # asks to the existing summary + regen affordance.
    _REPORT_REGEN_PATTERNS = (
        "regenerate the closure summary report for this case",
        "regenerate the resolution summary report for this case",
    )

    # Same exact-match policy as _REPORT_REGEN_PATTERNS: only the
    # precomposed DECIDE-suggestion payload reaches the runbook
    # creation path. Free-typed paraphrases ("create a runbook please")
    # fall through to Q&A. This keeps the principle consistent across
    # terminal-state actions: clicking triggers persisted side effects;
    # typing never does.
    _RUNBOOK_CREATION_PATTERNS = (GENERATE_RUNBOOK_PAYLOAD.lower(),)

    # The explicit "generate anyway" confirmation offered on the
    # SIMILAR_FOUND stop turn. Dispatched separately so the handler knows
    # the user has already seen the similar-runbook candidate and chosen —
    # the similar-match stop is waived, nothing else is.
    _RUNBOOK_CONFIRM_PATTERNS = (GENERATE_RUNBOOK_ANYWAY_PAYLOAD.lower(),)

    async def _process_terminal_turn(
        self,
        case: "Case",
        user_message: str,
        metadata: dict[str, Any],
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """Handle turns on terminal cases: Q&A, report regeneration, runbook creation.

        Terminal cases are immutable — no evidence, milestones, or state changes.
        Three scenarios:
          1. User requests report regeneration → regenerate summary.
          2. User accepts runbook suggestion → evaluate, create draft.
             Eligible: RESOLVED cases only — runbooks codify complete
             troubleshooting scenarios (root cause + verified solution).
          3. User asks questions about the case → answer via TERMINAL_TEMPLATE.
        """
        msg_lower = user_message.lower().strip().rstrip(".!? ")

        # Scenario 1: Report regeneration. Strict exact-match against the
        # DECIDE suggestion payloads — free-typed paraphrases fall
        # through to Q&A so typing can never produce a persisted Report
        # side effect.
        if msg_lower in self._REPORT_REGEN_PATTERNS:
            return await self._handle_report_regeneration(case, metadata)

        # Scenario 2: Runbook creation. Strict exact-match (same policy
        # as regen): only the DECIDE suggestion's precomposed
        # payload triggers persisted runbook generation; paraphrases
        # fall through to Q&A. RESOLVED-only — runbooks codify a
        # confirmed root-cause-to-solution chain.
        is_runbook_eligible = case.state == CaseState.RESOLVED
        if is_runbook_eligible and msg_lower in self._RUNBOOK_CREATION_PATTERNS:
            return await self._handle_runbook_creation(case, metadata)
        if is_runbook_eligible and msg_lower in self._RUNBOOK_CONFIRM_PATTERNS:
            return await self._handle_runbook_creation(
                case, metadata, dedup_confirmed=True
            )

        # Scenario 3: Q&A
        return await self._process_terminal_qa(
            case, user_message, metadata, user_id=user_id
        )

    async def _handle_report_regeneration(
        self,
        case: "Case",
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        """Regenerate the terminal summary report for a terminal case.

        For CLOSED cases, the same substance gate applied at closure time
        applies here — strict gating, no end-run around
        ``should_generate_terminal_summary``. RESOLVED cases regenerate
        unconditionally (a confirmed solution is always summarizable).

        The freshly-generated content is rendered inline in chat (same
        principle as the closure-ack turn), since summary writing is an
        interactive operation in this codebase.
        """
        from faultmaven.core.investigation.terminal_transitions import (
            should_generate_terminal_summary,
            terminal_summary_skip_reason,
        )
        from faultmaven.modules.case.domain.owned_models.report import ReportType

        if case.state == CaseState.RESOLVED:
            report_type = ReportType.RESOLUTION_SUMMARY
            report_label = "Resolution Summary"
        else:
            report_type = ReportType.CLOSURE_SUMMARY
            report_label = "Closure Summary"

        # Strict gating for CLOSED: the verdict at regen time must agree
        # with the verdict at closure time. Substance signals are frozen
        # in CLOSED state, so this is a stable check.
        if case.state == CaseState.CLOSED and not should_generate_terminal_summary(
            case
        ):
            skip = terminal_summary_skip_reason(case) or (
                "No closure summary can be generated for this case."
            )
            return {
                "agent_response": skip,
                "suggested_follow_ups": [],
                "case_updated": case,
                "metadata": metadata,
            }

        if not self.report_service:
            return {
                "agent_response": (
                    "Report generation is not available at the moment. "
                    "Please try again later."
                ),
                "suggested_follow_ups": [],
                "case_updated": case,
                "metadata": metadata,
            }

        try:
            # generate_reports returns ReportGenerationResponse; its
            # .reports field is the list of newly-persisted CaseReports.
            response = await self.report_service.generate_reports(case, [report_type])
            content = response.reports[0].content if response.reports else None
            agent_response = (
                content
                if content
                else f"The {report_label} has been regenerated. "
                f"You can view it in the Dashboard."
            )
            logger.info(
                f"Regenerated {report_type.value} for terminal case {case.case_id}",
                extra={"case_id": case.case_id, "report_type": report_type.value},
            )
        except Exception as e:
            logger.warning(
                f"Report regeneration failed for case {case.case_id}: {e}",
                extra={"case_id": case.case_id},
            )
            agent_response = (
                f"Failed to regenerate the {report_label}. Please try again."
            )

        # Re-offer the regen affordance — the user may want to iterate.
        # The "remaining" count comes from the DB and reflects the row
        # just written, so it correctly decrements turn-over-turn.
        remaining = await self._remaining_regens_for(case)
        if case.state == CaseState.RESOLVED:
            runbook_exists = await self._case_has_runbook_draft(case)
            follow_ups = _resolved_suggestions(case, remaining, runbook_exists)
        else:
            follow_ups = _closed_suggestions(case, remaining)

        return {
            "agent_response": agent_response,
            "suggested_follow_ups": follow_ups,
            "case_updated": case,
            "metadata": metadata,
        }

    async def _handle_runbook_creation(
        self,
        case: "Case",
        metadata: dict[str, Any],
        dedup_confirmed: bool = False,
    ) -> dict[str, Any]:
        """Evaluate readiness + dedup, then create runbook draft (fire-and-forget).

        Only RESOLVED cases reach this path — runbooks codify complete
        troubleshooting scenarios (root cause + verified solution).
        Eligibility is gated by the caller (`_process_terminal_turn`).

        Flow:
        1. Check content readiness (assess_runbook_readiness via evaluate_runbook_suggestion)
        2. Check deduplication. A SIMILAR_FOUND verdict STOPS the turn: the
           candidate is named and nothing is created until the user chooses
           (the "generate anyway" affordance routes back here with
           ``dedup_confirmed=True``, which waives this stop and only this
           stop). Dedup-failure caveats do not stop — the case is
           runbook-worthy and only the duplicate check is uncertain, so
           creation proceeds with the caveat stated (#944).
        3. If eligible: call ConversionService.convert_from_case() in background
        4. Return immediately with a message directing user to Dashboard Drafts

        Args:
            dedup_confirmed: True only on the explicit "generate anyway"
                confirmation payload — the user has already been shown the
                similar-runbook candidate on the previous turn and chosen to
                proceed.
        """
        from faultmaven.core.investigation.seeded_provenance import (
            confirmed_root_seed_origin,
        )
        from faultmaven.core.investigation.terminal_transitions import (
            RunbookSuggestion,
            evaluate_runbook_suggestion,
        )

        # Step 0: Provenance-based uniqueness (Phase 5.2b), LEGACY rows only —
        # the seeder that stamped this provenance is gone (fm#1295, see
        # ``seeded_provenance``). A case resolved by validating a cause it had
        # planted from an existing runbook needs no
        # new runbook — it would duplicate that one. This is the cheap SYNC tier
        # ABOVE the async embedding-similarity dedup (Step 2, which stops and
        # names a ≥70% match for the user to decide on):
        # a direct, certain "you applied runbook X" signal, so we short-circuit
        # with the covering runbook named before spending an embedding search.
        # (The offer gate already suppresses the affordance for these cases; this
        # covers the residual typed-exact-payload path and names the runbook.)
        # A knowledge-lifecycle decision, not a safety gate — the manual
        # POST /knowledge/runbooks/create path stays open.
        seed_origin = confirmed_root_seed_origin(case)
        if seed_origin:
            title = None
            if self.knowledge_service and hasattr(
                self.knowledge_service, "get_runbook_title"
            ):
                title = await self.knowledge_service.get_runbook_title(seed_origin)
            named = f"**{title}**" if title else "an existing runbook"
            return {
                "agent_response": (
                    f"This case was resolved by applying {named}, so it is already "
                    "covered — no new runbook is needed. You can view or update it "
                    "from the Dashboard Knowledge Base."
                ),
                "suggested_follow_ups": [],
                "case_updated": case,
                "metadata": metadata,
            }

        # Step 1+2: Evaluate readiness and deduplication. The KB is injected
        # explicitly (constructor param) — the old probe here,
        # ``hasattr(self.knowledge_service, "runbook_kb")``, was permanently
        # False (no such attribute on any IKnowledgeService), so the engine
        # always passed None and dedup never ran (fm#1030). None stays
        # legitimate: without ChromaDB, evaluate_runbook_suggestion takes its
        # honest "did not run" caveat.
        suggestion = await evaluate_runbook_suggestion(
            case,
            self.runbook_kb,
            scope_resolver=self._runbook_dedup_scope_resolver(case),
        )

        if suggestion.verdict == RunbookSuggestion.NOT_READY:
            return {
                "agent_response": suggestion.message,
                "suggested_follow_ups": [],
                "case_updated": case,
                "metadata": metadata,
            }

        # A similar runbook was found: STOP and let the user choose, unless
        # they already have. Surfacing a likely duplicate and then creating
        # it anyway on the same turn would make the question rhetorical and
        # defeat the point of checking — preventing duplicate runbooks is
        # what dedup is FOR. This is not a coverage claim (best-chunk-max
        # measures overlap, not equivalence — the message says so); the
        # "generate anyway" affordance makes the choice answerable on the
        # next turn, and the Dashboard KB link covers the review path.
        if (
            suggestion.verdict == RunbookSuggestion.SIMILAR_FOUND
            and not dedup_confirmed
        ):
            return {
                "agent_response": suggestion.message,
                "suggested_follow_ups": [_generate_runbook_anyway_suggestion()],
                "case_updated": case,
                "metadata": metadata,
            }

        # Step 3: Create the draft
        conversion_service = getattr(self, "conversion_service", None)
        if not conversion_service:
            logger.warning(
                f"Runbook creation requested for case {case.case_id} but "
                f"conversion_service is not available"
            )
            return {
                "agent_response": (
                    "Runbook generation is not available at the moment. "
                    "You can create one from the Dashboard instead."
                ),
                "suggested_follow_ups": [],
                "case_updated": case,
                "metadata": metadata,
            }

        # Idempotence — mirror the authoritative guard in the service funnel
        # (_convert_from_case_impl) so the chat UX returns a clean "already
        # exists" message instead of firing a background task that then fails
        # with CASE_RUNBOOK_EXISTS. A case whose only prior drafts were discarded
        # is free to regenerate.
        try:
            existing = await conversion_service.get_conversion_by_case(
                case.case_id, case.user_id
            )
        except Exception as e:
            existing = None
            logger.warning(
                f"Existing-conversion check failed for case {case.case_id}: {e}. "
                "Proceeding to generate.",
                extra={"case_id": case.case_id},
            )
        if existing and existing.has_live_draft():
            return {
                "agent_response": (
                    "A runbook draft already exists for this case. You can view or "
                    "update it in the Dashboard under **Knowledge Base > Drafts**."
                ),
                "suggested_follow_ups": [],
                "case_updated": case,
                "metadata": metadata,
            }

        # Fire-and-forget: kick off conversion in background
        try:
            from faultmaven.modules.knowledge.domain.models.conversion import (
                CaseConversionRequest,
            )

            # Case-generated runbooks land in the case owner's personal KB by
            # default. Global is reserved for platform-curated content; the
            # owner can promote later via the Dashboard.
            request = CaseConversionRequest.from_case(case, scope="personal")
            # Don't await the full pipeline — fire and forget
            import asyncio

            asyncio.create_task(
                self._run_runbook_conversion(
                    conversion_service,
                    request,
                    case.user_id,
                    case.enterprise_id,
                )
            )

            # Name only what the reader can act on while reading this turn.
            #
            # No in-chat notification is promised. The background task DOES
            # write a completion notification into the transcript, but it is a
            # `role: "system"` row and the copilot's conversation loader keeps
            # only user/assistant rows — and there is no push channel for case
            # messages, so that row is invisible on this turn and after a
            # reload alike. The FAILURE notifications ride the same row, so a
            # failed or empty conversion is silent there.
            #
            # No chat affordance is named either. "Generate runbook from this
            # case" is deliberately suppressed on THIS turn (see
            # `runbook_already_exists=True` below), and free-typed text never
            # reaches the creation path — `_RUNBOOK_CREATION_PATTERNS` matches
            # the DECIDE payload exactly, so the label is not typeable. Naming
            # it here would point at nothing.
            #
            # No failure is inferred from absence, either. `_persist_job` runs
            # only after the pipeline finishes, so nothing lands in Drafts
            # while the conversion is in flight: "not there yet" and "it
            # failed" look identical to the reader. Telling them to act on an
            # empty Drafts list would fire on the healthy path.
            #
            # What is left is the destination (true, and reachable now) and
            # the Dashboard's own create/edit path (`POST
            # /knowledge/runbooks/create` plus the Drafts editor), offered as
            # a standing capability rather than a failure diagnosis — the same
            # framing the SUGGEST message already uses ("You can also do this
            # later from the Dashboard"). That is the durable way out when the
            # silent failure above happens, and it costs the reader nothing
            # when it does not.
            agent_response = (
                "Creating your runbook draft from this case. "
                "It will appear in the Dashboard under "
                "**Knowledge Base > Drafts** once generation finishes. You can "
                "also create and edit runbooks there directly."
            )
            # Carry the dedup caveat onto the user-visible turn. Only NOT_READY
            # surfaces `suggestion.message` above, so a
            # SUGGEST_WITH_CAVEATS verdict would otherwise reach the user as
            # the unqualified line above — silently implying the KB was checked
            # when it was not (#944). Draft creation still proceeds: the case is
            # runbook-worthy, and what is uncertain is only whether a duplicate
            # already exists.
            if suggestion.verdict == RunbookSuggestion.SUGGEST_WITH_CAVEATS:
                agent_response = f"{suggestion.message}\n\n{agent_response}"
            logger.info(
                f"Runbook creation initiated for case {case.case_id}",
                extra={"case_id": case.case_id},
            )
            # Success path re-offers the standard terminal Q&A affordances so
            # the user can iterate on the summary while the background runbook
            # conversion runs. The runbook affordance is hidden on THIS turn —
            # we just kicked off a generation, so re-offering it would race the
            # background task and risk a duplicate draft. The suppression is
            # per-turn: it returns on subsequent terminal Q&A turns, where the
            # idempotence guard above answers a repeat click with a clean
            # "already exists" instead of a second draft.
            #
            # Because it is absent here, the text above must not name it — a
            # message that points at a chip this turn does not carry sends the
            # reader looking for something that is not on screen, and the label
            # is not typeable either (exact-match dispatch on the DECIDE
            # payload). The Dashboard create/edit path it names instead is
            # reachable independently of any turn's suggestion set.
            remaining = await self._remaining_regens_for(case)
            follow_ups = _resolved_suggestions(
                case, remaining, runbook_already_exists=True
            )
        except Exception as e:
            logger.warning(
                f"Failed to initiate runbook creation for case {case.case_id}: {e}",
                extra={"case_id": case.case_id},
            )
            agent_response = (
                "Failed to start runbook generation. "
                "You can try again or create one from the Dashboard."
            )
            # Failure path stays empty — the text already says "try again",
            # and the user will see the standard terminal Q&A suggestions
            # on the next turn anyway.
            follow_ups = []

        return {
            "agent_response": agent_response,
            "suggested_follow_ups": follow_ups,
            "case_updated": case,
            "metadata": metadata,
        }

    def _runbook_dedup_scope_resolver(self, case: "Case"):
        """Build the CASE OWNER's KB-scope resolver for runbook dedup.

        Dedup answers for the principal who will act on the answer — the case
        owner, whose Dashboard the suggestion points at (owner decision,
        fm#1030). Scope = global ∪ the owner's personal items ∪ items shared
        to the owner's teams, the same allowlist shape as the KB
        pre-fetch (``_prefetch_kb_context``).

        One deliberate divergence from that pre-fetch: NO try/except around
        the team arm. The pre-fetch swallows a team-arm failure and degrades
        to global ∪ personal — correct for seeding, wrong here, because a
        silently narrowed search would underpin a "checked, nothing similar"
        claim it did not establish. A failure raises out of the resolver, and
        ``evaluate_runbook_suggestion`` (which awaits it inside its dedup
        ``try``) takes the failure-caveat branch instead of answering.

        Standalone is not a failure: ``team_service`` is None there, so the
        team arm resolves empty by construction and the scope collapses to
        global ∪ owner-personal.
        """
        from faultmaven.modules.knowledge.domain.services.knowledge_service import (
            build_kb_scope_filter,
            resolve_shared_kb_ids,
        )

        async def _resolve() -> dict:
            owner_id = getattr(case, "user_id", None)
            shared_kb_ids: list[str] = []
            team_service = getattr(self, "team_service", None)
            share_repository = getattr(self, "share_repository", None)
            if owner_id and team_service and share_repository:
                owner_team_ids = await team_service.list_all_user_team_ids(owner_id)
                shared_kb_ids = await resolve_shared_kb_ids(
                    share_repository,
                    owner_team_ids,
                    getattr(case, "enterprise_id", None),
                )
            return build_kb_scope_filter(owner_id, shared_kb_ids)

        return _resolve

    async def _run_runbook_conversion(
        self,
        conversion_service,
        request,
        user_id: str,
        enterprise_id: str,
    ) -> None:
        """Background task for runbook conversion.

        ``enterprise_id`` is the SOURCE CASE's enterprise, and it is required, not
        optional. The conversion persists three RLS-tenanted rows (the synthetic
        ``uploaded_files`` conversion source, the ``conversion_jobs`` row, its
        ``conversion_drafts``); each is stamped with whatever this carries. It
        was a hardcoded single-tenant sentinel before #1143, which PostgreSQL
        RLS rejected for every tenant under ``TENANT_PROVIDER=multi``.

        Passing it explicitly is belt-and-braces, NOT a fix for a context that
        might not propagate — be clear about which. This whole task depends on
        inheriting the request's tenant contextvar and cannot work without it:
        the RLS binding itself is sampled from it per transaction (the ``begin``
        listener in ``infrastructure/persistence/database``), and so are the
        dedup read (``get_conversion_by_case``) and the completion-notification
        read/write below, none of which take an org argument. If that
        propagation ever broke, this parameter would not save the write — stamp
        and binding would simply disagree and RLS would refuse it, which is the
        fail-closed outcome we want rather than a silent cross-tenant write.

        What it buys instead is provenance: the stamp becomes a property of the
        resource being converted (the case's own org, hydrated from its row)
        rather than of the ambient context the task happened to be scheduled
        under, and the missing argument that caused #1143 becomes a TypeError
        instead of a sentinel.

        Logs success/failure and writes a completion notification to the case
        transcript. The notification is best-effort: if writing it fails, the
        background task swallows the secondary error rather than masking the
        primary outcome.

        Who reads these three strings decides what they may name. The copilot
        drops `role: "system"` rows, so its users never see them at all; the
        Dashboard renders the transcript, so it is the only reader to write
        for. That rules out naming a chat affordance — the Dashboard has no
        suggestion-chip UI whatsoever, so "click X" there points at a control
        that has never existed, and it also has no case-to-runbook trigger of
        its own to redirect to. What a Dashboard reader can reach is the
        Knowledge Base: the Drafts tab to view, and the "write a runbook from
        the template" form to author one by hand. The two unhappy notices
        therefore state plainly that nothing was saved and offer that manual
        path, which is a weaker remedy than the conversion they were promised
        but the only one on their screen.
        """
        notification_content: str
        try:
            result = await conversion_service.convert_from_case(
                request=request,
                user_id=user_id,
                enterprise_id=enterprise_id,
            )
            if result.drafts:
                draft = result.drafts[0]
                logger.info(
                    f"Runbook draft created: {draft.runbook_id} "
                    f"(title='{draft.title}', quality={getattr(draft, 'quality_score', 'N/A')})",
                    extra={
                        "case_id": request.case_id,
                        "runbook_id": draft.runbook_id,
                    },
                )
                notification_content = (
                    f"Your runbook draft **{draft.title}** is ready. "
                    f"View it in the Dashboard under **Knowledge Base > Drafts**."
                )
            else:
                logger.warning(
                    f"Runbook conversion completed but no drafts produced "
                    f"for case {request.case_id}",
                    extra={"case_id": request.case_id},
                )
                notification_content = (
                    "Runbook generation finished without producing a draft, "
                    "so nothing was saved for this case. You can write one "
                    "yourself in the Dashboard under **Knowledge Base**."
                )
        except Exception as e:
            logger.error(
                f"Background runbook creation failed for case {request.case_id}: {e}",
                extra={"case_id": request.case_id},
                exc_info=True,
            )
            notification_content = (
                "Runbook generation failed, so no draft was created for this "
                "case. You can write one yourself in the Dashboard under "
                "**Knowledge Base**."
            )

        # Best-effort completion notification. The case is loaded fresh
        # because terminal cases can still receive Q&A turns that mutate
        # `messages`, and the per-case lock prevents this write from
        # interleaving with a concurrent Q&A turn.
        try:
            async with self._case_locks[request.case_id]:
                case = await self.repository.get(request.case_id)
                if case is None:
                    logger.warning(
                        f"Case {request.case_id} not found when writing "
                        f"runbook completion notification — case may have "
                        f"been deleted while the background task was running.",
                        extra={"case_id": request.case_id},
                    )
                    return
                # No human wrote this, so it carries no author: the role says
                # "system", and a sentinel string would reach clients as a
                # non-resolvable principal id now that author_id persists
                # (ADR-013 D4: system turns have no author).
                append_message_row(
                    case,
                    MessageRowKind.SYSTEM_NOTICE,
                    notification_content,
                    turn_number=case.current_turn,
                    metadata={"source": "runbook_conversion_complete"},
                )
                case.message_count = len(case.messages)
                await self.repository.save(case)
        except Exception as e:
            logger.warning(
                f"Failed to write runbook completion notification for case "
                f"{request.case_id}: {e}",
                extra={"case_id": request.case_id},
                exc_info=True,
            )

    async def _process_terminal_qa(
        self,
        case: "Case",
        user_message: str,
        metadata: dict[str, Any],
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """Process a Q&A turn on a terminal case via the LLM.

        Uses TERMINAL_TEMPLATE and TerminalResponse schema. No state mutations.

        ``user_id`` is the authenticated principal for the turn; it keys the KB
        read allowlist the ``kb_qa`` tool builds (owner + team arms). A terminal
        case still answers questions, so its Q&A turn must read the same KB the
        user's non-terminal turns do.
        """
        from faultmaven.config.settings import get_settings
        from faultmaven.infrastructure.security.case_redaction import (
            CaseRedactionContext,
        )

        redaction_settings = get_settings()
        redaction_ctx = CaseRedactionContext(
            case_id=case.case_id,
            sanitizer=self.sanitizer,
            redis_client=self.redis_client,
            enabled=self._should_redact(),
            ttl_hours=redaction_settings.protection.redaction_registry_ttl_hours,
        )
        await redaction_ctx.load()

        # Pass provider/model so the whole-prompt accountant (GAP-1/2/3) can
        # size the budget and engage the overflow backstop on the terminal-QA
        # path too (previously this call supplied neither, so it fell back to
        # the static char cap and was never measured against the model window).
        provider_name = getattr(self.llm_provider, "provider_name", None)
        model_name = (
            getattr(self.llm_provider.config, "default_model", None)
            if hasattr(self.llm_provider, "config")
            else None
        )
        prompt = get_prompt_for_case(
            case,
            user_message,
            provider_name=provider_name,
            model_name=model_name,
        )

        def _build_tool_loop_base(
            *,
            target_tokens: int,
            provider_name: Optional[str],
            model_name: Optional[str],
        ) -> str:
            # #614: re-assembled for the model the tool loop sends to, when the
            # chat-sized prompt does not fit there.
            return get_prompt_for_case(
                case,
                user_message,
                provider_name=provider_name,
                model_name=model_name,
                target_tokens=target_tokens,
            )

        # Pass tools with auto tool_choice — LLM decides whether to invoke
        # kb_qa, web_search, etc. based on the user's question.
        tools_kwargs: dict[str, Any] = {}
        if self.investigation_tools:
            tools_kwargs["investigation_tools"] = self._build_da_tool_schemas()
            tools_kwargs["tool_context"] = await self._build_tool_context(
                case, user_id=user_id
            )
            tools_kwargs["force_tool_use"] = False
            tools_kwargs["base_prompt_builder"] = _build_tool_loop_base

        response_obj = await self._generate_structured_output(
            prompt,
            TerminalResponse,
            **tools_kwargs,
            redaction_ctx=redaction_ctx,
            case=case,
            user_message=user_message,
        )

        await redaction_ctx.save()

        # Extract follow-up suggestions
        follow_ups: list[dict[str, Any]] = []
        if (
            hasattr(response_obj, "suggested_follow_ups")
            and response_obj.suggested_follow_ups
        ):
            follow_ups = self._flatten_follow_ups(
                response_obj.suggested_follow_ups, metadata
            )

        # Attach terminal-Q&A suggestions deterministically. The
        # TERMINAL_TEMPLATE instructs the LLM to leave its own
        # suggested_follow_ups empty; the engine owns these so the rules
        # don't drift turn-to-turn:
        #   - CLOSED: regen-closure-summary card iff the substance gate
        #     PASSes (also the chat-side retry path when initial
        #     generation failed).
        #   - RESOLVED: regen-resolution-summary + runbook cards. Regen
        #     mirrors CLOSED's offering; runbook is the forward action
        #     RESOLVED enables.
        if case.state == CaseState.CLOSED:
            remaining = await self._remaining_regens_for(case)
            follow_ups = follow_ups + _closed_suggestions(case, remaining)
        elif case.state == CaseState.RESOLVED:
            remaining = await self._remaining_regens_for(case)
            runbook_exists = await self._case_has_runbook_draft(case)
            follow_ups = follow_ups + _resolved_suggestions(
                case, remaining, runbook_already_exists=runbook_exists
            )

        # #1451: this path returns before Step 6, so the service's turn-record
        # backfill reads the flag off this metadata to mark the TurnProgress,
        # and persists it onto the assistant row.
        if is_agent_response_synthesized(response_obj):
            metadata[MESSAGE_METADATA_AGENT_SYNTHESIZED] = True

        return {
            "agent_response": response_obj.agent_response,
            "suggested_follow_ups": follow_ups,
            "case_updated": case,
            "metadata": metadata,
        }

    def _should_redact(self) -> bool:
        """Determine whether PII redaction should be applied at the engine level.

        Checks SANITIZE_PII setting. Returns False when no sanitizer is
        configured (redaction disabled at DI level).
        """
        if not self.sanitizer:
            return False

        from faultmaven.config.settings import get_settings

        return get_settings().protection.sanitize_pii

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
            upload_report = self._report_turn_uploads(case, attachments)
            metadata.update(upload_report)

            # 0a. Terminal case handling — Q&A and report regeneration only
            if case.is_terminal:
                return await self._process_terminal_turn(
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
                    user_confirms = intent_confirms or self._user_confirms_transition(
                        user_message
                    )
                    user_declines = intent_declines or self._user_declines_transition(
                        user_message
                    )

                    if user_confirms:
                        from faultmaven.core.investigation.terminal_transitions import (
                            confirm_pending_transition,
                        )

                        if self.checkpoint_service:
                            to_state = case.pending_transition.get(
                                "to_state", "unknown"
                            )
                            await self.checkpoint_service.create_checkpoint(
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
                            turn_metadata = self._finish_deterministic_turn(
                                case,
                                user_message or "",
                                resolve_msg,
                                upload_report,
                                progress_made=False,
                            )
                            await self.repository.save(case)
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
                        await self.repository.save(case)

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
                        ) = await self._auto_generate_report(case)

                        agent_response = _compose_terminal_reply(case, summary_payload)
                        turn_metadata = self._finish_deterministic_turn(
                            case,
                            user_message or "",
                            agent_response,
                            upload_report,
                            progress_made=True,
                            **confirmed_transition_arms(case, executed),
                        )
                        await self.repository.save(case)

                        # Closure-ack follow-ups depend on whether
                        # generation succeeded. Success: minimal
                        # suggestions (the summary is rendered inline,
                        # so a regen card next to it would be noise).
                        # Failure: include the regen affordance so the
                        # user can retry immediately — the "noise next
                        # to inline summary" rationale doesn't apply
                        # when there's no summary inline.
                        remaining = await self._remaining_regens_for(case)
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
                            turn_metadata = self._finish_deterministic_turn(
                                case,
                                user_message or "",
                                agent_response,
                                upload_report,
                                progress_made=False,
                            )
                            await self.repository.save(case)

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

                            turn_metadata = self._finish_deterministic_turn(
                                case,
                                user_message or "",
                                agent_response,
                                upload_report,
                                progress_made=False,
                            )
                            await self.repository.save(case)

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
                        turn_metadata = self._finish_deterministic_turn(
                            case,
                            user_message or "",
                            closure.message,
                            upload_report,
                            progress_made=False,
                        )
                        await self.repository.save(case)
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
                    turn_metadata = self._finish_deterministic_turn(
                        case,
                        user_message or "",
                        closure.message,
                        upload_report,
                        progress_made=False,
                    )
                    await self.repository.save(case)
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
                self._apply_hypothesis_action_intent(
                    case, intent_data, user_message, metadata
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
                sanitizer=self.sanitizer,
                redis_client=self.redis_client,
                enabled=self._should_redact(),
                ttl_hours=redaction_settings.protection.redaction_registry_ttl_hours,
            )
            await redaction_ctx.load()

            # Build prompt using the adaptive template system
            # Gap #6: Pass provider info for dynamic token budget calculation
            provider_name = getattr(self.llm_provider, "provider_name", None)
            model_name = (
                getattr(self.llm_provider.config, "default_model", None)
                if hasattr(self.llm_provider, "config")
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
                        self.repository, case.case_id
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
            _tools_avail = self._tools_effectively_available()

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
                if self.investigation_tools
                else False
            )
            route_single_shot = bool(
                self.investigation_tools
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
                response_obj = await self._generate_structured_output(
                    prompt,
                    schema_model,
                    redaction_ctx=redaction_ctx,
                    case=case,
                    user_message=user_message,
                    reasoning_intent=ReasoningIntent.INFERENCE,
                    min_output_tokens=TOOLLESS_INFERENCE_OUTPUT_FLOOR,
                )
            elif self.investigation_tools:
                da_tools = self._build_da_tool_schemas()
                da_context = await self._build_tool_context(case, user_id=user_id)
                response_obj = await self._generate_structured_output(
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
                response_obj = await self._generate_structured_output(
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
            case_updated, response_metadata = await self._process_response_structured(
                case, user_message, response_obj, attachments, upload_report
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
            case_updated = await self._check_automatic_transitions(
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
            self._score_progress(metadata)

            # 4c. Resolution backstop (INV-43). LAST of the openers, which is
            # what makes it a backstop rather than a fourth competing proposer:
            # step 4 has just run the LLM's own ``proposed_transition`` and the
            # deferred proposer ran inside the apply step before it, so anything
            # they opened is standing on ``pending_transition`` and this bails on
            # it. Only a resolution-READY case that NOBODY offered reaches the
            # proposal. Placed after 4b rather than before it because the offer
            # is engine action, not case progress — it writes none of the arms
            # ``_score_progress`` reads.
            _maybe_propose_confirmed_resolution(case_updated, metadata)

            # 5. Phase 4: Hypothesis Housekeeping (Decay & Anchoring)
            # This happens after transitions but before recording the turn
            self._perform_hypothesis_housekeeping(
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
            is_valid, validation_issues = self.state_validator.is_valid(case_updated)
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
            progress_result = self.progress_monitor.check_progress(case_updated)
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
            turn_record = self._create_turn_record(
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
            #   - milestone_engine.py (LLM-emitted refutation / retirement)
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
                            self._resolve_id_ref,
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
            await self.repository.save(case_updated)

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
                summary_payload, summary_failed = await self._auto_generate_report(
                    case_updated
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
                follow_ups = self._flatten_follow_ups(
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
                remaining = await self._remaining_regens_for(case_updated)
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
                    provider=_resolve_chat_provider_name(self.llm_provider)
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
                        "agent_response_summary": self._summarize_text(
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
                    # ``_check_if_progress_made`` scores — ``novel_evidence_added``,
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
            is_external = self.llm_error_handler.is_retryable_error(e)

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

    # Constants for tool-augmented generation
    #
    # What bounds a turn's tool loop (#611, #614). The loop makes
    # MAX_TOOL_ITERATIONS + 1 calls: iterations 0..MAX_TOOL_ITERATIONS-1 may
    # call tools, the last is schema-only.
    #
    # The MESSAGE bound is structural. _bound_tool_loop_messages trims the
    # ``messages`` of every call to the SOFT cap from _resolve_tool_loop_budget:
    #
    #     soft = PROMPT_TARGET_TOKENS + PROMPT_TOOL_OBSERVATION_MAX_TOKENS
    #          = 32,000 + 16,000 = 48,000 with shipped defaults.
    #
    # That is a bound on ``messages`` only, in ESTIMATED tokens: providers with
    # no local tokenizer (gemini, and the router, whose name is not a provider)
    # are estimated at len // 4, which read 1.5-1.7x under cl100k on two log
    # samples. It does not count the ``tools=`` payload — the schema tool alone
    # is ~950 (TerminalResponse) to ~11,300 (InvestigationResponse_Diagnosis)
    # cl100k tokens, plus ~1,100 for the six investigation tools the DA
    # registry can hold. That payload is bounded only by the WINDOW (#614):
    # when the resolver knows the receiving model's context window, the HARD
    # cap holds messages plus that call's tools= payload (the schema tool alone
    # on the final iteration) to what the window leaves beside the completion
    # the call asks for — its max_tokens (8,000; 16,000 on a truncation retry,
    # which is bounded again), or the registry's reserve if larger.
    # _fit_tool_loop_base fits the base to that before the first call,
    # re-assembling it for the receiving model, or refusing the loop, when it
    # does not fit; with the window unknown it never touches the base. With no
    # window known (the router) or a large one, the loop sends and elides
    # exactly what it did before.
    #
    # PROMPT_TURN_TOKEN_CEILING (150,000) is a separate, METERED net: after each
    # non-final call it compares the turn's spend_weighted_tokens — real
    # provider tokens of every metered call in the turn (messages, tools
    # payload, output, truncation retries, fallback attempts, LLM calls made by
    # tools), cache reads weighted 0.25 — and once over, every remaining
    # iteration is schema-only. It can change the loop only when crossed within
    # the first MAX_TOOL_ITERATIONS - 1 calls (after that, the next iteration is
    # final anyway), i.e. when those calls average more than 50,000 each with
    # defaults. That is NOT excluded by the message bound. An uncached turn
    # with a full-size base and the Diagnosis schema meters ~3 x (32,000 +
    # 1,350 + 11,300 + 1,100) = ~137,000 for the base, system instruction and
    # tools alone; the observation allowance, outputs, or a len // 4 undercount
    # carries it past 150,000, and the ceiling removes the last tool round.
    # Where the provider serves the repeated prefix from its prompt cache on
    # calls after the first, that prefix counts at 0.25 and the ceiling stays
    # out of normal turns.
    #
    # Raising PROMPT_TARGET_TOKENS or PROMPT_TOOL_OBSERVATION_MAX_TOKENS moves
    # the metered spend toward the ceiling; raise the ceiling with them. Pinned
    # by TestToolLoopSpendBound in test_milestone_engine_tool_loop.py, and what
    # each cap counts by TestToolLoopBoundSplitsSoftAndHardCaps /
    # TestToolLoopBaseFitsTheReceivingModel.
    MAX_TOOL_ITERATIONS = 4
    TOOL_RESULT_MAX_CHARS = 8000
    MAX_DEEP_ANALYSIS = 1

    def _resolve_tool_loop_budget(self, provider_name: str) -> _ToolLoopBudget:
        """The two caps on every tool-loop call (#614). The SOFT cap is
        ``prompt_target + a bounded observation scratchpad`` on ``messages``
        alone — the base task fits the jar, the accumulated tool observations get
        a bounded allowance. The HARD cap comes from the model's window when
        known: no call's prompt — messages plus the tools payload it carries —
        plus the completion it asks for may exceed it (``_ToolLoopBudget.hard``).
        """
        from faultmaven.config.settings import get_settings
        from faultmaven.utils.model_context import resolve_model_budget

        try:
            obs = get_settings().prompt_budget.tool_observation_max_tokens
        except Exception:
            obs = 16_000
        try:
            pn = provider_name if isinstance(provider_name, str) else None
            resolved = resolve_model_budget(pn, self.da_model)
            return _ToolLoopBudget(
                soft=resolved.prompt_target + obs,
                window=resolved.context_window,  # None: unknown, trust the target
                response_reserve=resolved.response_reserve or 0,
            )
        except Exception:
            # Best-effort — never let budget resolution break a turn.
            return _ToolLoopBudget(soft=32_000 + obs, window=None)

    def _bound_tool_loop_messages(
        self,
        messages: list[dict],
        budget_tokens: int,
        provider_name: str,
        token_cache: Optional[dict] = None,
        *,
        tools: Optional[list[dict]],
        window_tokens: Optional[int],
    ) -> list[dict]:
        """Keep one tool-loop call within its two caps by eliding the OLDEST
        tool-exchange groups (an assistant tool-call message plus its tool
        results), preserving the system + base task messages and the
        most-recent exchanges. This bounds the ACCUMULATED tool observations so
        the request cannot grow unbounded across iterations.

        - ``budget_tokens`` is the SOFT cap: ``messages`` alone, a size/cost
          target. The ``tools=`` payload is not counted against it, so where no
          window is known the bound is exactly what it was before #614.
        - ``window_tokens`` is the HARD cap: the prompt tokens the receiving
          model's window leaves beside THIS call's completion
          (``_ToolLoopBudget.hard(max_tokens)``), or ``None`` when the window is
          unknown. ``messages`` PLUS ``tools`` — the list sent as ``tools=`` on
          this call, the schema tool alone on the final iteration — must fit it
          (#614). A request that would still exceed it is refused with
          ``ToolCallingUnsupportedError`` (the caller takes the non-tool path),
          never returned; ``_fit_tool_loop_base`` makes that unreachable for the
          head it has fitted, at the first attempt's completion cap.

        Both are REQUIRED keywords, so no call site can bound a request while
        silently skipping the window or leaving its tools uncounted: ``None``
        states "no tools" / "window unknown".

        Invariants: the elided span is replaced by a single marker (INV-4 — never
        a silent drop; the agent can re-run a search), and whole assistant/tool
        groups are elided together so tool_call ↔ tool_result pairing stays valid
        (providers reject an orphan tool result).
        """

        def _tok(m: dict) -> int:
            return _tool_loop_message_tokens(
                m, provider_name, self.da_model, token_cache
            )

        msg_cap = budget_tokens
        tools_tokens = 0
        if window_tokens is not None:
            tools_tokens = _tool_payload_tokens(
                tools, provider_name, self.da_model, token_cache
            )
            msg_cap = min(msg_cap, window_tokens - tools_tokens)
        total = sum(_tok(m) for m in messages)
        if total <= msg_cap:
            return messages

        head = messages[:2]  # system + base task (always kept)
        rest = messages[2:]
        groups: list[list[dict]] = []
        for m in rest:
            if m.get("role") == "assistant" or not groups:
                groups.append([m])
            else:
                groups[-1].append(m)

        marker = {"role": "user", "content": _TOOL_LOOP_ELISION_MARKER}
        # Counted WITHOUT the by-id cache: the marker is not held in `messages`,
        # so once the returned list is dropped its address can be reused by a
        # later message dict — which would then read the marker's count from the
        # cache and be under-counted (#614).
        marker_tokens = _tool_loop_message_tokens(marker, provider_name, self.da_model)
        head_tokens = sum(_tok(m) for m in head)
        avail = msg_cap - head_tokens - marker_tokens
        kept: list[list[dict]] = []
        for g in reversed(groups):
            gt = sum(_tok(m) for m in g)
            if gt <= avail:
                kept.insert(0, g)
                avail -= gt
            else:
                break
        if len(kept) == len(groups):
            out, sent = messages, total  # nothing to elide (head near the cap)
        else:
            logger.warning(
                "tool_loop_context_bounded: elided %d of %d tool-exchange group(s) "
                "to fit the %d-token budget",
                len(groups) - len(kept),
                len(groups),
                msg_cap,
            )
            out = list(head) + [marker]
            for g in kept:
                out.extend(g)
            sent = msg_cap - avail
        if window_tokens is not None and sent + tools_tokens > window_tokens:
            from faultmaven.exceptions import ToolCallingUnsupportedError

            raise ToolCallingUnsupportedError(
                message=(
                    f"Tool-loop head plus tools payload exceeds the "
                    f"{window_tokens}-token window budget; not sending."
                ),
                provider=provider_name if isinstance(provider_name, str) else None,
                model=self.da_model,
            )
        return out

    async def _fit_tool_loop_base(
        self,
        messages: list[dict],
        tools: list[dict],
        budget: _ToolLoopBudget,
        provider_name: str,
        max_tokens: int,
        base_prompt_builder: Optional[Callable[..., str]] = None,
        redaction_ctx: Any | None = None,
        token_cache: Optional[dict] = None,
    ) -> list[dict]:
        """Return the loop's opening ``[system, base task]`` messages with the
        base sized to the WINDOW of the model that receives the tool loop
        (#614), or refuse the loop.

        The base arrives assembled for the chat path (through the router, which
        names no provider or model: ``PROMPT_TARGET_TOKENS``, no window clamp,
        ``len // 4``), but the loop sends it to ``provider_name`` /
        ``self.da_model`` — a dedicated DA model whose window can be too small
        for it — beside a system instruction and a ``tools=`` payload the
        assembly never saw. When that window is known, the head must fit
        ``budget.hard(max_tokens)`` — the window less the completion the first
        attempt asks for — beside the largest ``tools`` list the loop offers and
        the elision marker, or no iteration's request can.

        Only the window. When it is unknown, or the head fits it, the base is
        sent as assembled, exactly as before #614: the SOFT cap governs
        observation elision, not the base, and shrinking the base to it would
        send less case context to buy observation room nobody was short of.

        When it does not fit, it is RE-ASSEMBLED through the same allocator at
        the room that is left (``base_prompt_builder(target_tokens=...,
        provider_name=..., model_name=...)`` → ``get_prompt_for_case``), which
        drops content by the assembly's own priority rules and, if even that
        cannot fit, takes the minimal fallback prompt. The rebuilt text is raw
        case content, so it is redacted like the original, and it goes in a NEW
        message dict; the dropped one's count leaves the by-id ``token_cache``
        with it, so a later dict reusing its address cannot read it. A base that
        still does not fit — or no builder — raises
        ``ToolCallingUnsupportedError``: nothing is sent, and the caller takes
        the non-tool path through the router the original base was sized for.

        Counted through ``token_cache`` on the loop's own message dicts, so the
        head is tokenized once per turn here, and the bound reuses the counts.
        """
        from faultmaven.exceptions import ToolCallingUnsupportedError

        limit = budget.hard(max_tokens)
        if limit is None:
            return messages  # window unknown: nothing to overflow, send as main

        def _tok(m: dict) -> int:
            return _tool_loop_message_tokens(
                m, provider_name, self.da_model, token_cache
            )

        system_msg, base_msg = messages[0], messages[1]
        tools_tokens = _tool_payload_tokens(
            tools, provider_name, self.da_model, token_cache
        )
        fixed = (
            _tok(system_msg)
            + _tool_loop_message_tokens(
                {"content": _TOOL_LOOP_ELISION_MARKER}, provider_name, self.da_model
            )
            + tools_tokens
        )
        room = limit - fixed
        base_tokens = _tok(base_msg)
        if base_tokens <= room:
            return messages

        # What the base is re-assembled FOR: the name and model the tool loop
        # sends to, so the allocator counts with this estimator and clamps to
        # this model's window.
        sizing_provider = provider_name if isinstance(provider_name, str) else None
        model = self.da_model if isinstance(self.da_model, str) else None
        logger.warning(
            "tool_loop_base_resized: base task prompt (%d tokens) does not fit the "
            "%d-token window of provider %s (model %s) beside a %d-token "
            "completion and %d tokens of system instruction, tools payload and "
            "elision marker; re-assembling it at %d tokens",
            base_tokens,
            budget.window,
            provider_name,
            model,
            budget.window - limit,
            fixed,
            room,
        )
        resized: Optional[str] = None
        if base_prompt_builder is not None and room > 0:
            try:
                resized = base_prompt_builder(
                    target_tokens=room,
                    provider_name=sizing_provider,
                    model_name=model,
                )
                if redaction_ctx and resized:
                    resized = await redaction_ctx.asanitize(resized)
            except Exception as exc:  # never break the turn on a rebuild
                logger.warning("tool-loop base re-assembly failed: %s", exc)
                resized = None
        if resized:
            resized_msg = {**base_msg, "content": resized}
            if _tok(resized_msg) <= room:
                if token_cache is not None:
                    token_cache.pop(id(base_msg), None)
                return [system_msg, resized_msg, *messages[2:]]
        raise ToolCallingUnsupportedError(
            message=(
                f"The base task prompt cannot fit the {budget.window}-token window "
                f"of provider {provider_name} (model {model}) beside a "
                f"{budget.window - limit}-token completion and {fixed} tokens of "
                f"system instruction, tools payload and elision marker; not "
                f"sending the tool loop."
            ),
            provider=provider_name if isinstance(provider_name, str) else None,
            model=self.da_model,
        )

    @staticmethod
    def _build_schema_tool(schema_model: Any, provider: Any) -> list[dict]:
        """The structured-output tool, strict-enforced where that is available.

        Returns the plain (unenforced) tool when the provider does not report
        STRICT, or when the schema has no strict representation — the four
        ``InvestigationResponse_*`` schemas carry ``Dict[str, Any]`` fields that
        OpenAI's subset cannot express, and forcing them would guarantee empty
        milestone justifications rather than merely unenforced ones. Capability
        detection failing is treated as "not strict": the unenforced tool is the
        behaviour this path has always had, so it cannot regress a turn.
        """
        from faultmaven.infrastructure.llm.structured_output_capability import (
            StructuredOutputCapability,
        )
        from faultmaven.utils.schema_converter import (
            pydantic_to_openai_tools,
            pydantic_to_strict_openai_tools,
        )

        try:
            capability = provider.get_structured_output_capability()
        except Exception as exc:
            logger.debug(
                "Structured-output capability unavailable (%s); schema tool "
                "stays unenforced",
                exc,
            )
            return pydantic_to_openai_tools(schema_model)

        # The provider API is synchronous. A coroutine here means the provider
        # is a stand-in that answers everything asynchronously, which is not an
        # answer — close it so it does not surface as an un-awaited-coroutine
        # warning, and treat the capability as unknown.
        if inspect.iscoroutine(capability):
            capability.close()
            return pydantic_to_openai_tools(schema_model)

        if capability != StructuredOutputCapability.STRICT:
            return pydantic_to_openai_tools(schema_model)

        return pydantic_to_strict_openai_tools(schema_model)

    async def _tool_augmented_generate(
        self,
        prompt: str,
        schema_model: Any,
        investigation_tools: list[dict],
        tool_context: Any,
        max_tokens: int = 8000,
        redaction_ctx: Any | None = None,
        case: Any | None = None,
        force_tool_use: bool = False,
        base_prompt_builder: Optional[Callable[..., str]] = None,
    ) -> BaseInteractionResponse:
        """Run a bounded tool-calling loop with investigation tools.

        The LLM gets real investigation tools (search_file, deep_analysis,
        kb_qa, web_search) alongside the response schema tool.

        Algorithm:
        1. Build schema tool from Pydantic model (reuses existing converter)
        2. Combine: all_tools = investigation_tools + schema_tools
        3. Loop with tool_choice per force_tool_use:
           - force_tool_use=True (DA turns): "required" — LLM must call a tool
           - force_tool_use=False (other turns): "auto" — LLM may respond directly
        4. When LLM calls schema tool → parse and return structured output
        5. After max iterations → force schema with only schema tools available

        Vectorization (v5.2):
        - Proactive: starts background vectorization for large evidence files
          at loop entry. Runs concurrently with tool calls.
        - Reactive: tracks per-evidence DA failure signals (empty searches,
          timeouts, low confidence). Triggers vectorization as fallback.

        Args:
            prompt: Full investigation prompt
            schema_model: Pydantic model class for structured output
            investigation_tools: OpenAI-format tool defs for search/analysis
            tool_context: ToolContext for tool execution
            max_tokens: Max tokens for LLM calls
            case: Case object for evidence access and DA count persistence
            base_prompt_builder: Re-assembles the base task prompt for the model
                that receives this loop, called as ``(target_tokens=...,
                provider_name=..., model_name=...)`` only when ``prompt`` does
                not fit a KNOWN window beside the completion, the system
                instruction and the tools (see ``_fit_tool_loop_base``).
                ``None``: a base that does not fit is refused instead.

        Returns:
            Instantiated Pydantic model (BaseInteractionResponse)
        """
        # Use dedicated DA provider (DA_PROVIDER from .env) if available,
        # otherwise fall back to the default router
        provider = self.da_provider or self.llm_provider
        provider_name = getattr(provider, "provider_name", type(provider).__name__)
        model_info = f", model: {self.da_model}" if self.da_model else ""
        logger.info(
            f"Tool-augmented generate using provider: {provider_name}{model_info}"
        )
        # Per-call caps: messages within the soft cap (prompt_target +
        # observations), and — when the model's window is known — messages plus
        # that call's tools= payload within what the window leaves beside that
        # call's completion, so no request is sent whose ESTIMATED prompt plus
        # requested completion exceeds the window (the base is fitted before the
        # loop, accumulated observations compact to fit — see
        # _fit_tool_loop_base / _bound_tool_loop_messages /
        # _resolve_tool_loop_budget).
        tool_loop_budget = self._resolve_tool_loop_budget(provider_name)
        # Label vocabulary for the tool-result budget metrics below. The
        # tool name on a tool call is MODEL-SUPPLIED, so it is unbounded:
        # a hallucinated name reaches `execute_tool`, comes back as a short
        # "Tool 'x' not found" error, and would still be relayed -- and a
        # model that invents names freely would mint a Prometheus label per
        # invention. Bound it to what this call actually OFFERED; anything
        # else is by definition not a tool and folds into `unknown`.
        offered_tool_names = frozenset(
            (t.get("function") or {}).get("name") or ""
            for t in investigation_tools
            if isinstance(t, dict)
        ) - {""}
        # Per-message token-count cache (by id) reused across iterations so the
        # large stable head isn't re-tokenized every loop — see
        # _bound_tool_loop_messages.
        _msg_token_cache: dict = {}

        # Build the schema tool, asking for NATIVE ENFORCEMENT when the provider
        # can give it (fm#1051).
        #
        # This path used to deliver the response schema as a plain function with
        # no `strict` key, and never consulted the provider's capability at all —
        # only the single-shot branch below did. So on a provider documented as
        # STRICT, every turn that had tools available (which is every turn with a
        # tool registry, not just Directed Analysis) got unenforced function
        # calling: BEST_EFFORT semantics. The model could omit a required field,
        # the engine dropped the whole `state_updates` payload, and the turn
        # advanced nothing — observed on the first live cloud turn after #819 as
        # a missing `state_updates.knowledge_match.match_type`.
        #
        # Scoped to the SCHEMA tool. The investigation tools keep their existing
        # non-strict definitions: several take optional parameters, and strict
        # mode has no optional keys, so enforcing them would force the model to
        # emit explicit nulls and change directed-analysis behaviour — a
        # regression risk with no bearing on the bug being fixed.
        #
        # Marked from the primary provider's capability. On a mid-chain fallback
        # the request can still land elsewhere carrying `strict: true`; that is
        # valid OpenAI-spec (Anthropic and Gemini rebuild tool definitions and
        # drop it), and the alternative — marking nothing — is the bug itself.
        schema_tools = self._build_schema_tool(schema_model, provider)
        schema_tool_name = schema_tools[0]["function"]["name"]

        # Combine investigation tools + schema tool
        all_tools = investigation_tools + schema_tools

        # Build tool name list for the DA system instruction
        tool_names = [t["function"]["name"] for t in investigation_tools]

        # Initialize conversation with DA system instruction + user prompt
        da_system_instruction = self._build_da_system_instruction(
            tool_names,
            schema_tool_name,
        )
        # Size the base to the WINDOW of the model that receives it (#614): when
        # the window is known, the head must fit it beside the first attempt's
        # completion, the largest tools= payload (all_tools) and the elision
        # marker — or no call can. With the window unknown it is sent as
        # assembled. Raises ToolCallingUnsupportedError (→ the non-tool path)
        # when it cannot fit.
        messages = await self._fit_tool_loop_base(
            [
                {"role": "system", "content": da_system_instruction},
                {"role": "user", "content": prompt},
            ],
            all_tools,
            tool_loop_budget,
            provider_name,
            max_tokens,
            base_prompt_builder=base_prompt_builder,
            redaction_ctx=redaction_ctx,
            token_cache=_msg_token_cache,
        )
        deep_analysis_count = 0

        # Per-evidence DA failure tracking for auto-vectorization (v5.2)
        # Same pattern as deep_analysis_count above — mechanical counters
        # that trigger system actions when thresholds are met.
        # "Already vectorized" is sourced from the persistent
        # Evidence.vectorized flag (set + saved by _vectorize_evidence on
        # success) so dedup holds both within a turn and across turns.
        da_empty_search_counts: dict[str, int] = {}  # evidence_id → consecutive empties

        # Proactive vectorization: start background tasks for large evidence
        # files before the tool loop begins. Runs concurrently so semantic
        # search is available by the time the agent needs it.
        # Gated on force_tool_use=True (Directed Analysis). Triage and
        # Knowledge Query turns don't consult case evidence via semantic
        # search, so preemptive embedding would be wasted work — and on a
        # cold-cached model it can dominate the turn budget. See
        # data-preprocessing-design-specification.md §5 (vectorization is
        # scoped to DA-mode turns).
        proactive_tasks: dict[str, asyncio.Task] = {}
        if case and force_tool_use:
            proactive_tasks = await self._start_proactive_vectorization(
                case, tool_context
            )

        force_schema_next = False
        # Sticky: once the per-turn token ceiling is crossed we must wrap up on
        # every subsequent iteration. Unlike force_schema_next (reset to False on
        # each successful tool response, ~line "Reset the flag on successful tool
        # usage"), this is NEVER cleared — otherwise the ceiling would be a no-op
        # exactly when it matters (the crossing response usually contains tool
        # calls, which would immediately reset force_schema_next).
        ceiling_reached = False

        for iteration in range(self.MAX_TOOL_ITERATIONS + 1):
            is_final = iteration == self.MAX_TOOL_ITERATIONS

            # Tool availability per iteration:
            # - Iteration 0..N-1: all tools (investigation + schema)
            # - Final iteration / force_schema / ceiling reached: schema tools ONLY
            if is_final or force_schema_next or ceiling_reached:
                tools_for_call = schema_tools
            else:
                tools_for_call = all_tools

            # DA turns: "required" — LLM must search evidence before answering
            # Other turns: "auto" — LLM decides whether to use tools
            # Final/force-schema/ceiling iterations always use "required" (schema only)
            if is_final or force_schema_next or ceiling_reached:
                choice = "required"
            elif force_tool_use:
                choice = "required"
            else:
                choice = "auto"

            logger.info(
                f"Tool loop iteration {iteration}/{self.MAX_TOOL_ITERATIONS} "
                f"(is_final={is_final}, force_schema={force_schema_next}, tool_choice={choice})"
            )

            # Pass da_model when using dedicated provider
            # Bound EVERY tool-loop call: messages within the soft cap, and
            # messages plus THIS call's tools= payload within what the window
            # leaves beside its completion, when the window is known (#614 — the
            # schema tool alone on the final iteration).
            # The full `messages` history is kept for accumulation; only a
            # bounded, most-recent view is sent.
            bounded_messages = self._bound_tool_loop_messages(
                messages,
                tool_loop_budget.soft,
                provider_name,
                token_cache=_msg_token_cache,
                tools=tools_for_call,
                window_tokens=tool_loop_budget.hard(max_tokens),
            )
            generate_kwargs = dict(
                prompt="",
                messages=bounded_messages,
                tools=tools_for_call,
                tool_choice=choice,
                max_tokens=max_tokens,
                temperature=0.2,
                case_id=case.case_id if case is not None else None,
                # Cache the stable prefix (system + tools) across the tool-loop
                # iterations. Only Anthropic acts on this; other providers pop it.
                cache_prompt=True,
            )
            if self.da_model and self.da_provider:
                generate_kwargs["model"] = self.da_model

            # Tier 2 — apply STRUCTURED_OUTPUT_PROVIDER override on the
            # tool-augmented path too. Tool-call iterations land Pydantic
            # schemas back through schema_model.model_validate_json (see
            # _parse_schema_tool_call), so the same routing rationale
            # applies: force the LLM call onto a known-STRICT provider
            # when the operator has configured one. The override is only
            # applied when no da_model is set (DA gets first dibs).
            if not (self.da_model and self.da_provider):
                try:
                    from faultmaven.config.settings import get_settings

                    _settings = get_settings()
                    _override_provider = _settings.llm.structured_output_provider
                    if _override_provider is not None:
                        generate_kwargs["provider_override"] = _override_provider.value
                        _override_model = _settings.llm.get_structured_output_model()
                        if _override_model:
                            generate_kwargs["model"] = _override_model
                except Exception:
                    pass

            async def _tool_loop_call(cap: int):
                """One tool-loop generation at *cap*, metered.

                Metering lives INSIDE the retry closure, not after it: a
                truncation retry is a second real API call, billed like the
                first. Counting only the winner would make DA-turn spend
                under-report exactly on the turns that cost the most.
                """
                call_kwargs = dict(generate_kwargs, max_tokens=cap)
                if cap != max_tokens and tool_loop_budget.window is not None:
                    # A truncation retry asks for a bigger completion: bound the
                    # prompt again for THIS cap, so prompt plus completion still
                    # fits the window (#614). Refuses rather than sends if even
                    # the head cannot fit beside it.
                    call_kwargs["messages"] = self._bound_tool_loop_messages(
                        messages,
                        tool_loop_budget.soft,
                        provider_name,
                        token_cache=_msg_token_cache,
                        tools=tools_for_call,
                        window_tokens=tool_loop_budget.hard(cap),
                    )
                result = await provider.generate(**call_kwargs)
                if self.da_provider is not None:
                    # A dedicated DA provider is a concrete provider instance,
                    # so this call bypassed the registry metering chokepoint.
                    # Meter it here so DA-turn spend is still counted. (When no
                    # DA provider is set, `provider` is the router and the
                    # registry already metered the underlying call.)
                    record_provider_call(
                        getattr(provider, "provider_name", "unknown"),
                        call_kwargs.get("model")
                        or getattr(result, "model", None)
                        or "unknown",
                        result,
                        getattr(result, "response_time_ms", 0),
                    )
                return result

            try:
                # Same ladder the non-tool structured path has had since #513:
                # a cut body means the response is unusable, and the first
                # remedy is more room. This path never had it — `max_tokens` was
                # a fixed 8000 and the schema-tool arguments were parsed
                # unguarded, so a truncated tool call went into
                # `_parse_schema_tool_call`, where the partial-repair machinery
                # (nested-JSON parsing, the state_updates → {} coercion,
                # validation degradation) could turn it into a structurally
                # valid response whose state updates were then APPLIED to the
                # case. Raising the cap first is what stops that (#1094).
                #
                # Escalation only; the escalate-to-degrade tail stays on the
                # non-tool path, which owns the case context that drives it. If
                # the retry is also cut, behaviour is what it was before: parse
                # what came back, and let the existing failure handling below
                # take it if the parse fails.
                response = await generate_with_truncation_retry(
                    _tool_loop_call,
                    max_tokens=max_tokens,
                    ceiling=STRUCTURED_OUTPUT_MAX_TOKENS_CEILING,
                    label=f"tool loop iteration {iteration}",
                )
                # Per-turn ceiling: the call is now metered into the active turn
                # tracker (record_provider_call above for a dedicated DA provider,
                # or the registry chokepoint for the router), so its running total
                # reflects this call — force the loop to wrap up if it is over.
                # A net, not the primary bound: see MAX_TOOL_ITERATIONS (#611).
                _turn_tracker = active_token_tracker.get()
                if _turn_tracker is not None:
                    try:
                        from faultmaven.config.settings import get_settings

                        _turn_ceiling = get_settings().prompt_budget.turn_token_ceiling
                    except Exception:
                        _turn_ceiling = 150000
                    if (
                        _turn_tracker.spend_weighted_tokens > _turn_ceiling
                        and not is_final
                        and not ceiling_reached
                    ):
                        logger.warning(
                            f"Turn spend ({_turn_tracker.spend_weighted_tokens} "
                            f"cost-weighted tokens) exceeded ceiling ({_turn_ceiling}). "
                            f"Forcing the tool loop to wrap up."
                        )
                        # Sticky (never reset) so the next iteration forces the
                        # schema even though the current response's tool calls will
                        # clear force_schema_next below. We already spent this
                        # generation; the ceiling stops the NEXT round of tools.
                        ceiling_reached = True
            except Exception as e:
                # Any iteration failure (timeout, provider error, transient
                # issue) raises ToolCallingUnsupportedError so the caller
                # (_generate_structured_output) falls back to the non-tool
                # structured-output path. Iteration 0 typically indicates
                # provider/model incompatibility; iteration 1+ typically
                # indicates a provider can't satisfy tool_choice=required
                # under FaultMaven's schema sizes (e.g., MiniMax M2P7 on
                # Fireworks hangs when forced to use tools, timing out at
                # the 180s LLM_PROVIDER_TIMEOUT_OVERRIDES limit). Either
                # way, the caller's non-tool path is the right recovery —
                # without this, iter-1+ failures killed the turn entirely
                # and subsequent turns operated against a hole in
                # conversation history.
                from faultmaven.exceptions import ToolCallingUnsupportedError

                logger.warning(
                    "Tool loop: generate failed at iteration %d "
                    "(provider=%s, model=%s): %s. "
                    "Raising ToolCallingUnsupportedError for fallback.",
                    iteration,
                    provider_name,
                    model_info,
                    e,
                )
                raise ToolCallingUnsupportedError(
                    message=(
                        f"Tool calling failed at iteration {iteration}: {e}. "
                        f"Falling back to non-tool path."
                    ),
                    provider=provider_name,
                    model=self.da_model,
                ) from e

            # Check for tool calls in response
            if not hasattr(response, "tool_calls") or not response.tool_calls:
                # No tool calls. Two scenarios:
                # 1. Recoverable (force_schema_next=False): the LLM emitted text
                #    instead of calling a tool. Append the text plus a user-role
                #    nudge directing the schema-tool call, then retry with only
                #    schema tools. The nudge is what makes the next turn coherent
                #    — without it, the LLM "already answered" and won't act.
                # 2. Unrecoverable (force_schema_next=True or is_final): we already
                #    nudged once and the LLM still won't call the schema tool. Try
                #    parsing the text as schema JSON; if that fails, raise
                #    ToolCallingUnsupportedError so _generate_structured_output's
                #    fallback path retries via the non-tool structured-output route.
                if is_final or force_schema_next:
                    from faultmaven.exceptions import ToolCallingUnsupportedError

                    text = (response.content or "").strip()
                    if text:
                        try:
                            return self._parse_text_as_schema(text, schema_model)
                        except Exception as parse_err:
                            logger.warning(
                                "Tool loop: text content after forced-schema "
                                "iteration not parseable as schema (%s)",
                                parse_err,
                            )
                    logger.warning(
                        "Tool loop: provider %s ignored tool_choice=required "
                        "with only the schema tool exposed; escalating to "
                        "non-tool fallback path",
                        provider_name,
                    )
                    raise ToolCallingUnsupportedError(
                        message=(
                            f"Provider {provider_name} returned no tool calls "
                            f"under tool_choice=required with the schema tool "
                            f"as the only option. Falling back to non-tool path."
                        ),
                        provider=provider_name,
                        model=self.da_model,
                    )

                logger.warning(
                    "Tool loop: LLM returned no tool calls at iteration %d, "
                    "will force schema on next iteration",
                    iteration,
                )

                # Append the plain text response so the LLM knows what it said.
                #
                # Reasoning artifacts (response.provider_metadata — Anthropic
                # thinking blocks, Gemini assistant_parts) are DELIBERATELY
                # dropped here, unlike _build_assistant_message which
                # round-trips them. This is a recovery re-prompt, not a
                # continuation: the point is to re-ask with a fresh
                # instruction after the model failed to call a tool, and
                # replaying signed reasoning blocks across a state the
                # provider may not accept them in is rejected outright
                # (Anthropic 400s on thinking blocks echoed when thinking is
                # not enabled for that call). Re-prompting without the prior
                # reasoning is the safe direction and is the intended
                # behaviour, not an oversight (#1116).
                messages.append(
                    {
                        "role": "assistant",
                        "content": response.content
                        or "I should use a tool to proceed.",
                    }
                )
                # Append a user nudge that explicitly directs the schema-tool
                # call. Without this the conversation ends on an assistant
                # message with no fresh user instruction — most models read that
                # as "already answered" and either repeat themselves or return
                # empty content, defeating the recovery.
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"You must now produce your structured response by "
                            f"calling the `{schema_tool_name}` tool. Use the text "
                            f"above as the `agent_response` field and fill in the "
                            f"remaining required fields. Do not reply with plain "
                            f"text — only a tool call is acceptable."
                        ),
                    }
                )

                force_schema_next = True
                continue

            # Reset the flag on successful tool usage
            force_schema_next = False

            # Check if LLM called the schema tool (termination signal)
            for tc in response.tool_calls:
                func_name = tc.function.get("name", "")
                if func_name == schema_tool_name:
                    logger.info(
                        "Tool loop: schema tool called at iteration %d, "
                        "parsing structured output",
                        iteration,
                    )
                    return self._synthesize_agent_response(
                        self._parse_schema_tool_call(tc, schema_model),
                        schema_answer_stop_reason(response),
                    )

            # Build assistant message with tool calls
            assistant_msg = self._build_assistant_message(response)
            messages.append(assistant_msg)

            # Execute each investigation tool call
            for tc in response.tool_calls:
                func_name = tc.function.get("name", "")
                args_str = tc.function.get("arguments", "{}")
                logger.info(
                    "Tool loop iter %d: LLM called tool=%s args=%s",
                    iteration,
                    func_name,
                    (
                        args_str[:200]
                        if isinstance(args_str, str)
                        else str(args_str)[:200]
                    ),
                )

                args_well_formed = True
                try:
                    args = (
                        json.loads(args_str) if isinstance(args_str, str) else args_str
                    )
                except (json.JSONDecodeError, TypeError):
                    args = {}
                    args_well_formed = False

                # Reliability metric (read-only): did the MODEL hold up the
                # invocation contract? Same label bounding as the budget
                # metrics below — a hallucinated name folds into "unknown"
                # so an inventive model can't mint a label per invention.
                # execution_error is applied by the dispatch below, which
                # always records the attempt (see the try/finally).
                metric_tool = (
                    func_name if func_name in offered_tool_names else "unknown"
                )
                if func_name not in offered_tool_names:
                    _attempt_outcome = "unknown_tool"
                elif not args_well_formed:
                    _attempt_outcome = "invalid_args"
                else:
                    _attempt_outcome = "ok"

                # Counted no matter how the dispatch below ends. The
                # increment used to sit AFTER it, so an exception from
                # execute_tool or _track_da_result dropped the invocation
                # from both the numerator and the denominator — the
                # well-formed-invocation rate then reads cleaner the more
                # the infrastructure fails, which is exactly backwards. A
                # raise is a tool-side failure, the same class as a tool
                # returning success=False, so it folds into
                # execution_error instead of minting a new label — and
                # only from "ok", because a model that named a tool that
                # does not exist failed the contract first.
                try:
                    # Enforce deep_analysis limit
                    if (
                        func_name == "deep_analysis"
                        and deep_analysis_count >= self.MAX_DEEP_ANALYSIS
                    ):
                        result_text = (
                            "deep_analysis is limited to 1 call per turn. "
                            "Use search_file for additional searches."
                        )
                    else:
                        tool_result = await self.investigation_tools.execute_tool(
                            func_name,
                            args,
                            tool_context,
                        )
                        if func_name == "deep_analysis":
                            deep_analysis_count += 1
                        if _attempt_outcome == "ok" and not getattr(
                            tool_result, "success", True
                        ):
                            # Well-formed call, tool-side failure: infrastructure
                            # noise, not the model failing the contract.
                            _attempt_outcome = "execution_error"

                        result_text = self._format_tool_result(
                            tool_result, tool_name=func_name
                        )

                        # --- Per-evidence DA failure tracking (v5.2) ---
                        # Track search_file empty results and check vectorization
                        # triggers. Same pattern as deep_analysis_count above.
                        evidence_id = args.get("evidence_id", "")
                        if evidence_id and func_name in (
                            "search_file",
                            "deep_analysis",
                        ):
                            result_text = await self._track_da_result(
                                func_name=func_name,
                                evidence_id=evidence_id,
                                tool_result=tool_result,
                                result_text=result_text,
                                case=case,
                                tool_context=tool_context,
                                da_empty_search_counts=da_empty_search_counts,
                                proactive_tasks=proactive_tasks,
                            )

                except Exception:
                    if _attempt_outcome == "ok":
                        _attempt_outcome = "execution_error"
                    raise
                finally:
                    tool_call_attempts_total.labels(
                        tool=metric_tool, outcome=_attempt_outcome
                    ).inc()

                # Redact PII in tool results before sending to LLM.
                # Tool results contain raw file content (search_file,
                # deep_analysis) which bypasses prompt-level redaction.
                # Off the event loop via the async boundary (#654).
                if redaction_ctx:
                    result_text = await redaction_ctx.asanitize(result_text)

                # Truncate long results.
                #
                # This is the point where a tool result stops being what the
                # tool produced and becomes what the model sees, and until
                # #1088 it was silent: no log line, no counter, nothing
                # recorded that it had fired. "We don't know what this costs
                # us" was therefore a property of the implementation, not a
                # gap in the sample -- the only available estimate came from
                # arithmetic across two unrelated log lines, for one tool, on
                # one run.
                #
                # Record it before deciding the ceiling. The cap is a single
                # global constant shared by tools that are not alike, so the
                # measurement is per tool.
                #
                # Measured here rather than at the tool: after redaction and
                # after per-tool formatting is the string that actually enters
                # the context. The ONE exception is a kb_qa answer the
                # formatter already trimmed -- see below.
                original_chars = len(result_text)
                # ``metric_tool`` is the same bounded label computed once for
                # this tool call, above — both counters must agree on which
                # tool an invocation belongs to, and two copies of the folding
                # rule is how they stop agreeing.
                tool_result_relayed_total.labels(tool=metric_tool).inc()

                # #1086 gave kb_qa a SECOND, earlier cut: _format_tool_result
                # trims the answer to fit the wrapper so the relay instructions
                # survive, which means an oversized kb_qa answer usually lands
                # at or under the cap by the time it reaches this line. Measured
                # only here, kb_qa -- the tool this issue was opened about --
                # would report a clip rate near zero while still being clipped,
                # which is worse than not measuring it: the number looks honest
                # and is wrong. The formatter therefore records its own trim
                # into these same counters, against the TRUE pre-trim size, and
                # this site steps aside for that result so the observation is
                # made exactly once, at whichever site last saw the whole
                # string.
                # Anchored to the END rather than a substring search. The
                # formatter emits `... + marker + suffix`, and both are static
                # instruction text carrying no entity the redactor rewrites, so
                # that tail survives sanitisation intact. A plain `in` test
                # would also match an answer that merely QUOTES the marker --
                # costing that result its histogram sample, and, if redaction
                # then expanded it past the cap, its truncation count too. An
                # answer would now have to END on the marker to be misread.
                formatter_trimmed = func_name == "kb_qa" and result_text.endswith(
                    KB_QA_ANSWER_TRUNCATED_MARKER + KB_QA_RELAY_SUFFIX
                )
                if not formatter_trimmed:
                    tool_result_chars.labels(tool=metric_tool).observe(original_chars)

                if original_chars > self.TOOL_RESULT_MAX_CHARS:
                    # A kb_qa result can reach here already trimmed and STILL be
                    # oversized, because redaction runs in between and expands
                    # text (an IPv4 becomes a 29-char placeholder). That is a
                    # second cut on one result, worth a log line but not a
                    # second increment -- the clip rate must stay a rate.
                    if not formatter_trimmed:
                        tool_result_truncated_total.labels(tool=metric_tool).inc()
                    # Cut FIRST, then report, so the count is what the cut
                    # actually destroyed rather than the overflow it started
                    # from. Those diverged once kb_qa began eliding: the elide
                    # spends markers and paragraph realignment on top of the
                    # overflow. Both sites now report the same thing -- source
                    # characters destroyed -- so the two can be summed.
                    result_text, dropped_chars = self._truncate_tool_result(
                        result_text, func_name
                    )
                    # WARNING, not INFO: this discards content the model was
                    # meant to reason over, and the counters are no-ops unless
                    # ENABLE_METRICS -- which a standalone run does not set. The
                    # log line is what makes the clip observable there at all.
                    logger.warning(
                        "tool_result_truncated",
                        extra={
                            "tool": metric_tool,
                            "original_chars": original_chars,
                            "cap_chars": self.TOOL_RESULT_MAX_CHARS,
                            "dropped_chars": dropped_chars,
                            "at": "tool_loop",
                            # True means this result was ALREADY cut and counted
                            # at the formatter, and redaction pushed it back
                            # over the cap. One physical clip, two records: any
                            # aggregation that counts clips must drop these or
                            # it double-counts exactly the tool the ceiling
                            # question is about (#1088).
                            "after_formatter_trim": formatter_trimmed,
                        },
                    )

                # Append tool result message
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "name": func_name,
                        "content": result_text,
                    }
                )

        # Should not reach here (final iteration forces schema)
        raise MilestoneEngineError(
            "Tool loop exhausted without producing structured output"
        )

    # ================================================================
    # Vectorization tracking (v5.2) — mechanical safety nets
    # Same pattern as deep_analysis_count / MAX_DEEP_ANALYSIS above.
    # ================================================================

    async def _start_proactive_vectorization(
        self,
        case: Any,
        tool_context: Any,
    ) -> dict[str, asyncio.Task]:
        """Start background vectorization for qualifying DA-mode evidence.

        Runs concurrently with the tool loop so case_evidence_search is
        available by the time the agent needs it. Only vectorizes files
        above the size threshold that haven't already been vectorized.

        Uses ``self._inflight_vectorize`` to dedup across turns: if a
        task is already running for a given evidence_id, the current
        turn reuses it instead of creating a second concurrent encode.
        The persistent ``Evidence.vectorized`` flag covers the already-
        completed state; the in-flight registry covers the running
        state. Together they prevent cross-turn task stacking.
        """
        from faultmaven.config.settings import get_settings
        from faultmaven.modules.agent.tools.vectorize_file_tool import (
            VECTORIZATION_MAX_SIZE_BYTES,
        )

        settings = get_settings()
        min_size = settings.agent.vectorization_min_size_bytes
        tasks: dict[str, asyncio.Task] = {}

        for ev in getattr(case, "evidence", []):
            # Vectorization size gate. Post-010: file-backed evidence has
            # its size on uploaded_files.size_bytes; chat-extracted evidence
            # (USER_DESCRIPTION, source_file_id IS NULL) has no backing file
            # and is never large enough to vectorize — treat size=0 so it
            # falls below the min-size threshold.
            file_meta = case.find_uploaded_file(getattr(ev, "source_file_id", None))
            size = (
                int(file_meta.size_bytes) if file_meta and file_meta.size_bytes else 0
            )
            if not (
                size >= min_size
                and size <= VECTORIZATION_MAX_SIZE_BYTES
                and not ev.vectorized
            ):
                continue

            existing = self._inflight_vectorize.get(ev.evidence_id)
            if existing is not None and not existing.done():
                # Another turn already started this; reuse the same task
                # so both turns observe the same completion.
                tasks[ev.evidence_id] = existing
                logger.debug(
                    "proactive_vectorization_reused_inflight",
                    extra={"evidence_id": ev.evidence_id},
                )
                continue

            task = asyncio.create_task(
                self._vectorize_evidence(ev.evidence_id, tool_context)
            )
            self._inflight_vectorize[ev.evidence_id] = task
            # Remove from registry once the task settles (success,
            # failure, or cancellation). If persistence succeeded the
            # flag is True and this evidence won't re-enter the loop;
            # if it failed the next turn can retry cleanly.
            task.add_done_callback(
                lambda t, eid=ev.evidence_id: self._inflight_vectorize.pop(eid, None)
            )
            tasks[ev.evidence_id] = task
            logger.info(
                "proactive_vectorization_started",
                extra={
                    "evidence_id": ev.evidence_id,
                    "content_size_bytes": size,
                },
            )
        return tasks

    async def _vectorize_evidence(
        self,
        evidence_id: str,
        tool_context: Any,
    ) -> bool:
        """Vectorize a single evidence file via the registered tool.

        On success, flips ``Evidence.vectorized`` to True via a scoped
        single-row repository UPDATE so proactive + reactive gates skip
        this evidence on subsequent turns. The flag is the single source
        of truth for "is this evidence already in the case vector store".

        No internal ``asyncio.wait_for``: time-bound policy belongs at the
        caller. Proactive callers run this unbounded as a background task
        — the in-flight registry prevents duplicates, and bounding a
        background task that the caller never synchronously awaits only
        guarantees wasted CPU when ``asyncio.wait_for`` cancels the
        asyncio Future while the thread-pool worker (which can't be
        safely killed) continues to completion. Reactive callers wrap
        this with ``asyncio.wait_for`` using
        ``AgentSettings.vectorization_reactive_timeout_seconds`` because
        they do block the agent.
        """
        try:
            result = await self.investigation_tools.execute_tool(
                "vectorize_file",
                {"evidence_id": evidence_id},
                tool_context,
            )
        except Exception as e:
            logger.warning(
                "Vectorization failed for %s: %s",
                evidence_id,
                e,
                exc_info=True,
            )
            return False

        if not result.success:
            logger.warning(
                "vectorize_file returned failure for %s: %s",
                evidence_id,
                result.error,
            )
            return False

        # success is not "the file is in the index". `vectorize_file` reports a
        # file with no chunkable content as a success — the operation completed
        # and established a fact about the file — but nothing was written. This
        # boolean is the only thing the callers read: True flips the persistent
        # `vectorized` flag AND emits `_VECTORIZED_SYSTEM_MESSAGE`, telling the
        # model the file is searchable via case_evidence_search. The model then
        # searches, gets nothing, and reads it as "this file does not contain
        # that" — an index that was never written laundered into a finding about
        # the evidence (#941). The tool's own message says otherwise, but no
        # caller here renders it.
        #
        # `is not True`, deliberately: an unstated key and an unrecognisable
        # payload both mean "this caller did not tell us the file is indexed",
        # and the safe reading of that is that it isn't. Failing the other way
        # would make the guard depend on every future producer remembering to
        # set a key, with a false claim to the model as the penalty for
        # forgetting; failing this way costs a re-attempt.
        data = result.data if isinstance(result.data, dict) else {}
        if data.get("indexed") is not True:
            logger.info(
                "vectorize_file did not report an index for %s (%s) — not "
                "marking vectorized",
                evidence_id,
                data.get("message", ""),
            )
            return False

        logger.info("vectorize_file succeeded for %s", evidence_id)

        # Persist vectorized=True via a scoped single-row UPDATE. Must NOT
        # use repository.save(case) — this runs as a fire-and-forget task
        # that can complete after subsequent turns have written. An
        # aggregate save from a stale snapshot would silently wipe those
        # newer writes across every case-owned table.
        case_id = getattr(tool_context, "case_id", None)
        if case_id:
            try:
                await self.repository.update_evidence_vectorized(
                    case_id, evidence_id, True
                )
            except Exception as e:
                logger.debug(
                    "Failed to persist vectorized flag for %s: %s",
                    evidence_id,
                    e,
                )

        # Flip the flag on the in-memory snapshot so the current turn's
        # gate sees it without another DB read.
        case = getattr(tool_context, "in_memory_case", None)
        if case is not None:
            for ev in getattr(case, "evidence", []) or []:
                if getattr(ev, "evidence_id", None) == evidence_id:
                    ev.vectorized = True
                    break

        return True

    @staticmethod
    def _evidence_is_vectorized(case: Any, evidence_id: str) -> bool:
        """Return True if the given evidence is marked vectorized on the
        in-memory case. Source of truth for dedup — the persistent
        Evidence.vectorized flag set by _vectorize_evidence on success.
        """
        if case is None:
            return False
        for ev in getattr(case, "evidence", []) or []:
            if getattr(ev, "evidence_id", None) == evidence_id:
                return bool(getattr(ev, "vectorized", False))
        return False

    #: Re-exported from the tool that owns it so all emission sites
    #: carry the same text and the same rule. They used to hold separate copies (#941).
    _VECTORIZED_SYSTEM_MESSAGE = VECTORIZED_SYSTEM_MESSAGE

    async def _track_da_result(
        self,
        func_name: str,
        evidence_id: str,
        tool_result: Any,
        result_text: str,
        case: Any | None,
        tool_context: Any,
        da_empty_search_counts: dict[str, int],
        proactive_tasks: dict[str, asyncio.Task],
    ) -> str:
        """Track DA failure signals and trigger vectorization when needed.

        Returns result_text, potentially with [SYSTEM] messages appended.
        Dedup of "already vectorized" is sourced from Evidence.vectorized
        (persistent) — within-turn and across-turn.
        """
        # If the proactive task for this evidence has just completed this
        # turn, emit the [SYSTEM] advisory once. _vectorize_evidence has
        # already flipped and persisted the flag by the time we see
        # task.result()==True, so subsequent reactive checks naturally
        # skip this evidence via _evidence_is_vectorized.
        if evidence_id in proactive_tasks:
            task = proactive_tasks[evidence_id]
            if task.done() and not task.cancelled():
                exc = task.exception()
                if exc:
                    logger.warning(
                        "Proactive vectorization task failed for %s: %s",
                        evidence_id,
                        exc,
                    )
                else:
                    # The advisory decision lives in the helper, not in this
                    # `if`: a file that indexed nothing gets `result_text` back
                    # unchanged however this site is reached (#941).
                    before = result_text
                    result_text = append_vectorization_advisory(
                        result_text, task.result()
                    )
                    if result_text != before:
                        logger.info(
                            "proactive_vectorization_completed",
                            extra={"evidence_id": evidence_id},
                        )

        # Track search_file empty results
        if func_name == "search_file" and tool_result.success:
            try:
                data = (
                    json.loads(tool_result.data)
                    if isinstance(tool_result.data, str)
                    else tool_result.data
                )
                if isinstance(data, dict) and data.get("results_count", 0) == 0:
                    da_empty_search_counts[evidence_id] = (
                        da_empty_search_counts.get(evidence_id, 0) + 1
                    )
                else:
                    da_empty_search_counts[evidence_id] = 0
            except (json.JSONDecodeError, TypeError):
                pass

            # Advisory after 3 consecutive empty searches
            count = da_empty_search_counts.get(evidence_id, 0)
            if count >= 3:
                result_text += (
                    f"\n\n[SYSTEM] Last {count} search_file calls on this "
                    "file returned zero results. Consider using "
                    "deep_analysis with a different query approach."
                )

        already_vectorized = self._evidence_is_vectorized(case, evidence_id)

        # Track deep_analysis confidence for the low-confidence trigger
        # below. In-turn only, like `da_empty_search_counts`: nothing carries
        # DA history across turns any more. The orchestration service that
        # reconstructed it is gone, and the `da_invocation_count` field it
        # read was never added to the Evidence model.
        if func_name == "deep_analysis" and tool_result.success and case:
            try:
                data = (
                    json.loads(tool_result.data)
                    if isinstance(tool_result.data, str)
                    else tool_result.data
                )
                if isinstance(data, dict):
                    confidence = float(data.get("confidence", 1.0))

                    # Low confidence trigger
                    if confidence < 0.2 and not already_vectorized:
                        result_text = await self._reactive_vectorize(
                            evidence_id,
                            tool_context,
                            result_text,
                            "low_confidence",
                        )
                        already_vectorized = self._evidence_is_vectorized(
                            case, evidence_id
                        )
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

        # Track timeouts
        if (
            not tool_result.success
            and "timed out" in (getattr(tool_result, "error", "") or "").lower()
            and not already_vectorized
        ):
            result_text = await self._reactive_vectorize(
                evidence_id,
                tool_context,
                result_text,
                "tool_timeout",
            )
            already_vectorized = self._evidence_is_vectorized(case, evidence_id)

        # Reactive vectorization on repeated empty searches
        empty_count = da_empty_search_counts.get(evidence_id, 0)
        if empty_count >= 3 and not already_vectorized:
            result_text = await self._reactive_vectorize(
                evidence_id,
                tool_context,
                result_text,
                "repeated_empty_searches",
            )

        return result_text

    async def _reactive_vectorize(
        self,
        evidence_id: str,
        tool_context: Any,
        result_text: str,
        trigger: str,
    ) -> str:
        """Attempt reactive vectorization for a qualifying evidence file.

        On success, _vectorize_evidence flips + persists the Evidence
        vectorized flag, so subsequent reactive triggers in this turn
        will see it and skip via _evidence_is_vectorized.
        """
        from faultmaven.config.settings import get_settings
        from faultmaven.modules.agent.tools.vectorize_file_tool import (
            VECTORIZATION_MAX_SIZE_BYTES,
        )

        # Storage redesign 2026-04 phase 2: resolve size from case.evidence
        # (standalone evidence service deleted).
        ev_size = 0
        try:
            case = getattr(tool_context, "in_memory_case", None)
            if case is None and getattr(tool_context, "case_repository", None):
                case = await tool_context.case_repository.get(tool_context.case_id)
            if case is not None:
                for ev in getattr(case, "evidence", []) or []:
                    if getattr(ev, "evidence_id", None) == evidence_id:
                        # Post-010: size lives on uploaded_files via the
                        # source_file_id FK. Chat-extracted evidence has no
                        # backing file → size=0 (which falls below the
                        # vectorization min-size gate below).
                        file_meta = case.find_uploaded_file(
                            getattr(ev, "source_file_id", None)
                        )
                        ev_size = (
                            int(file_meta.size_bytes)
                            if file_meta and file_meta.size_bytes
                            else 0
                        )
                        break
        except Exception:
            return result_text

        settings = get_settings()
        if ev_size < settings.agent.vectorization_min_size_bytes:
            return result_text
        if ev_size > VECTORIZATION_MAX_SIZE_BYTES:
            return result_text

        # Reactive vectorization blocks the agent inside the tool loop;
        # bound it by the configurable reactive budget so a slow encode
        # can't eat the turn timeout. Proactive is unbounded elsewhere —
        # see _vectorize_evidence docstring for the split rationale.
        reactive_timeout = float(settings.agent.vectorization_reactive_timeout_seconds)
        try:
            success = await asyncio.wait_for(
                self._vectorize_evidence(evidence_id, tool_context),
                timeout=reactive_timeout,
            )
        except TimeoutError:
            logger.warning(
                "Reactive vectorization timed out for %s after %ss "
                "(trigger=%s). Agent proceeds without semantic search "
                "results for this turn; a proactive task for the same "
                "evidence may still be in flight.",
                evidence_id,
                reactive_timeout,
                trigger,
            )
            return result_text

        before = result_text
        result_text = append_vectorization_advisory(result_text, success)
        if result_text != before:
            logger.info(
                "reactive_vectorization_triggered",
                extra={
                    "evidence_id": evidence_id,
                    "trigger": trigger,
                    "content_size_bytes": ev_size,
                },
            )
        return result_text

    def _da_provider_supports_tools(self) -> bool:
        """Whether the resolved DA/chat provider+model can do tool calling.

        Single source of truth for the tool-calling capability check, shared by
        ``_tools_effectively_available`` (the directed-analysis elision gate) and
        the Layer-1 pre-check in ``_generate_structured_output_inner`` so the two
        cannot drift. Absent capability info → assume capable (the runtime path
        then catches an actual failure and falls back).
        """
        provider = self.da_provider or self.llm_provider
        model = self.da_model if self.da_provider else None
        supports = getattr(provider, "supports_tool_calling", None)
        if supports is None:
            return True
        try:
            return bool(supports(model))
        except Exception:
            return False

    def _tools_effectively_available(self) -> bool:
        """True when investigation tools are registered AND the resolved
        provider/model can actually do tool calling.

        This is the real precondition behind the directed-analysis evidence
        index+stub elision: the inline extract may only be dropped (telling the
        agent to ``search_file`` for specifics) when ``search_file`` will actually
        run this turn. A tool-less / tool-incapable turn that dropped the extract
        would be stranded with neither the data nor a working tool — the
        premature-conclusion failure FaultMaven guards against.
        """
        return bool(self.investigation_tools) and self._da_provider_supports_tools()

    def _build_da_tool_schemas(self) -> list[dict]:
        """Build OpenAI-format tool definitions for DA investigation tools."""
        if not self.investigation_tools:
            return []

        tools = []
        for agent_tool in self.investigation_tools.get_all_tools():
            schema = agent_tool.get_schema()
            tools.append(
                {
                    "type": "function",
                    "function": schema,
                }
            )
        return tools

    @staticmethod
    def _build_da_system_instruction(
        tool_names: list[str],
        schema_tool_name: str,
    ) -> str:
        """Build the system instruction that tells the LLM how to use DA tools.

        Adapts to whichever investigation tools are actually registered.
        Without this, the LLM sees tool definitions but has no guidance on
        when or why to call them, leading to non-deterministic tool usage.
        """
        has_search = "search_file" in tool_names
        has_da = "deep_analysis" in tool_names
        has_web = "web_search" in tool_names
        has_kb = "kb_qa" in tool_names

        # Build tool guidance based on what's actually available
        search_mode_guidance = (
            "search_file modes:\n"
            "- keyword (DEFAULT): Splits query into tokens and finds lines "
            "containing all of them. Use for IPs, hostnames, error codes, "
            "service names, usernames. Just pass the raw value as query — "
            'e.g., query="173.234.31.186" or query="timeout connection".\n'
            "- regex: Only when keyword mode cannot express the pattern "
            "(e.g., timestamp ranges, capture groups). Regex is error-prone "
            "— prefer keyword mode unless you specifically need pattern matching."
        )

        # Core evidence tools
        tool_lines = []
        if has_search:
            tool_lines.append(
                "- search_file: keyword/regex search against raw evidence files. "
                "Use for exact matches — IPs, timestamps, error codes, service names."
            )
        if has_da:
            tool_lines.append(
                "- deep_analysis: LLM-interpreted analysis of specific evidence sections. "
                "Use for analytical questions keyword search cannot answer. "
                "Limited to 1 call per turn."
            )
        if has_kb:
            tool_lines.append(
                "- kb_qa: Search the knowledge base for runbooks, best practices, "
                "and documented solutions. Returns results from all accessible "
                "sources (global, personal, team) automatically."
            )
        if has_web:
            tool_lines.append(
                "- web_search: Search trusted technical websites (Stack Overflow, "
                "official docs) for error messages and solutions."
            )

        if tool_lines:
            # Build priority guidance
            priority_parts = []
            if has_search or has_da:
                evidence_tools = ", ".join(
                    t for t in ["search_file", "deep_analysis"] if t in tool_names
                )
                priority_parts.append(
                    f"1. Start with case evidence ({evidence_tools}) — "
                    "ground your analysis in THIS case's data first."
                )
            if has_kb:
                priority_parts.append(
                    "2. Check knowledge base (kb_qa) for documented solutions "
                    "when evidence alone doesn't explain the issue."
                )
            if has_web:
                priority_parts.append(
                    "3. Use web_search as a last resort when evidence and KB "
                    "have no answers — e.g., unfamiliar error messages or "
                    "technology-specific issues."
                )

            tool_guidance = (
                f"You have {len(tool_lines)} investigation tools:\n"
                + "\n".join(tool_lines)
                + "\n\nTool priority:\n"
                + "\n".join(priority_parts)
            )
            if has_search:
                tool_guidance += f"\n\n{search_mode_guidance}"
        else:
            tool_guidance = (
                "No investigation tools are available for this turn. "
                "Base your analysis on the evidence context provided."
            )

        return (
            "You have investigation tools available to search and analyze "
            "the raw evidence files attached to this case.\n\n"
            f"{tool_guidance}\n\n"
            "QUESTION ROUTING — Decide which type of question the user is asking:\n\n"
            "TYPE A — CASE QUESTION (about THIS case's evidence):\n"
            "Questions about specific data in the submitted files — IPs, errors, "
            "timestamps, patterns, configurations, or anything that requires "
            "examining the evidence. Examples: 'What IPs failed auth?', "
            "'What happened at 14:00?', 'Is there a pattern in the errors?'\n"
            f"→ You MUST search the evidence ({', '.join(t for t in ['search_file', 'deep_analysis'] if t in tool_names)}) before "
            "responding. The structural indexes are summaries — they lack the "
            "specific values needed for grounded analysis. After searching, call "
            f"{schema_tool_name} to produce your structured response.\n\n"
            "TYPE B — KNOWLEDGE QUESTION (general technical knowledge):\n"
            "Questions about technologies, concepts, best practices, or setup "
            "procedures that are NOT answerable from case evidence. Examples: "
            "'What is Opik?', 'How to set up Redis clustering?', "
            "'Common causes of OOM kills?'\n"
            "→ You MUST search kb_qa first for documented solutions, runbooks, "
            "or best practices. If kb_qa returns relevant results, ground your "
            "answer in them and cite the source. If no relevant results, answer "
            "from your own knowledge (do not mention the failed search). "
            "Optionally use web_search for supplementary detail. Connect your "
            f"answer to the case context when relevant, then call {schema_tool_name}.\n\n"
            "TYPE C — HYBRID (needs both evidence AND knowledge):\n"
            "Questions that bridge case data and external knowledge. Examples: "
            "'Is our Redis config following best practices?', "
            "'Are these SSH settings secure?'\n"
            "→ Search evidence first to understand the current state, then use "
            "your knowledge, web_search, or KB tools for the reference baseline.\n\n"
            "TYPE D — ABOUT FAULTMAVEN (the assistant itself):\n"
            "Questions about YOU — which model or provider generates these "
            "responses, how you retrieve runbooks, who built you, what you can "
            "do. Examples: 'What LLM are you running on?', 'How do you work "
            "under the hood?'\n"
            "→ For that part, do NOT search the evidence or the knowledge base: "
            "FaultMaven is not the system under investigation and nothing about "
            "it is in the case. Answer it briefly from the self-reference "
            "guidance in your instructions and never request FaultMaven's own "
            "configuration as evidence. If the same message ALSO delivers or "
            "asks about case data, that part is Type A/B/C — search it first, "
            "then answer the FaultMaven part alongside — before calling "
            f"{schema_tool_name}.\n\n"
            "DEFAULT: When uncertain between Types A–C, treat it as Type A "
            "(case question) — evidence search is always safe. Only skip "
            "evidence search when the question clearly cannot be answered from "
            "log files, configs, or other submitted data.\n\n"
            "IMPORTANT — Search for the specific entity, not the event type:\n"
            "When the user asks about a specific IP, hostname, username, error "
            "code, or timestamp, search for THAT value directly — e.g., "
            'query="173.234.31.186", not query="Failed password". Searching '
            "for event types returns results for ALL entities and buries the "
            "relevant lines.\n\n"
            "IMPORTANT — PII tokens vs raw data:\n"
            "The <evidence_collected> summaries use PII placeholders "
            "(e.g., <IP_ADDRESS_1>). The raw files contain ORIGINAL values. "
            "When calling search_file, use ORIGINAL values from the user's "
            "message, NOT PII tokens.\n\n"
            "SEARCHABLE EVIDENCE — Only use search_file on evidence with "
            'searchable="true" in <evidence_collected>. These are uploaded '
            "files with raw content on disk. Evidence WITHOUT this attribute "
            "are investigation notes — they have no file to search. If you "
            "need to search a file, take its id and its label from the "
            "searchable entries.\n\n"
            "EVIDENCE vs KNOWLEDGE — These are fundamentally different data types:\n"
            "- EVIDENCE is case-specific data submitted by the user: log files, "
            "metrics, configs, pasted text, screenshots, user statements about "
            "their environment. Only user-submitted data goes in evidence_to_add.\n"
            "- KNOWLEDGE is pre-built reference material from kb_qa, web_search, "
            "or your own training data. Knowledge informs your analysis but is "
            "NEVER recorded as evidence. Do NOT create evidence_to_add entries "
            "from kb_qa results, web_search results, or your own knowledge.\n\n"
            "RESPONSE FORMAT — Ground your response in evidence:\n"
            "- Every item in <evidence_collected> carries a label attribute. "
            "That label is its name — use it verbatim and use nothing else. "
            "Not every item is a file the user named: text they pasted is "
            'labelled like "pasted text (turn 3)", and that IS its name. '
            "Never invent a filename for one, and never reach for a "
            "file-looking name from inside a file's contents.\n"
            "- For case questions, cite the label and line numbers from "
            "search results (e.g., 'In data_6-1.log, line 42: ...' or "
            "'In pasted text (turn 3), line 42: ...') and explain the "
            "significance using causal language.\n"
            "- For knowledge questions, state the relevant facts and relate "
            "them to the user's investigation context when possible.\n"
            "- Reference evidence by its label or by description, never by "
            "ev_ IDs."
        )

    async def _resolve_shared_kb_ids(self, user_id: str, enterprise_id: Any) -> list:
        """KB item ids shared to ``user_id``'s teams — the team arm of the tool
        path's read allowlist (ADR-013 §D4).

        Keyed on the **session** user, matching the owner arm: ``kb_tool_adapter``
        passes ``ToolContext.user_id`` to ``build_kb_scope_filter`` as the owner,
        so both arms must describe the same principal. Keying the team arm on the
        case owner instead would let a collaborator's turn read the owner's
        team-shared items — a wider allowlist than the reader is entitled to.
        (``_prefetch_kb_context`` keys on the case owner precisely because it is
        not acting for a session user; the two are deliberately different.)

        ``team_service``/``share_repository`` are wired post-construction and are
        absent in standalone, so a missing collaborator collapses the team arm to
        empty rather than raising — global ∪ owned still resolves.
        """
        if not user_id or user_id == "system":
            return []

        team_service = getattr(self, "team_service", None)
        share_repository = getattr(self, "share_repository", None)
        if not team_service or not share_repository:
            return []

        from faultmaven.modules.knowledge.domain.services.knowledge_service import (
            resolve_shared_kb_ids,
        )

        try:
            team_ids = await team_service.list_all_user_team_ids(user_id)
            return await resolve_shared_kb_ids(
                share_repository, team_ids, enterprise_id
            )
        except Exception:  # noqa: BLE001
            # Degrade to global ∪ owned rather than failing the turn. Narrowing
            # is safe; the alternative would be an unscoped read.
            logger.warning(
                "shared_kb_id_resolution_failed",
                extra={"user_id": user_id},
                exc_info=True,
            )
            return []

    async def _build_tool_context(self, case: Any, user_id: str | None = None) -> Any:
        """Build ToolContext for tool execution during DA turns.

        ``user_id`` is the turn's authenticated principal, threaded down from
        ``process_turn``. It is the *only* source: it previously came off
        ``intent_data``, which no caller populates — ``InvestigationService``
        builds that dict from ``QueryIntent.model_dump()`` (a model with no
        ``user_id`` field) plus ``query_mode``, so every live turn resolved to
        ``"system"`` and both arms of the KB read allowlist
        (``build_kb_scope_filter(user_id, shared_kb_ids)``) collapsed to the
        global corpus. Reading it from the intent payload would also make the
        read principal client-settable; the parameter comes from
        ``current_user.user_id``.

        ``None`` (engine-internal turn, no principal) keeps the historical
        ``"system"`` sentinel, which matches no owner and resolves no teams.
        """
        from faultmaven.modules.agent.tools.base import (
            ToolContext,
            derive_kb_context_metadata,
        )

        user_id = user_id or "system"
        enterprise_id = getattr(case, "enterprise_id", "")

        # Extract current investigation stage for tool context enrichment
        metadata: dict[str, Any] = {}
        progress = getattr(case, "progress", None)
        if progress:
            current_stage = getattr(progress, "current_stage", None)
            if current_stage:
                stage_value = (
                    current_stage.value
                    if hasattr(current_stage, "value")
                    else str(current_stage)
                )
                metadata["stage"] = stage_value.upper()

        return ToolContext(
            session_id=case.case_id,
            case_id=case.case_id,
            enterprise_id=enterprise_id,
            user_id=user_id,
            shared_kb_ids=await self._resolve_shared_kb_ids(user_id, enterprise_id),
            case_repository=self.repository,
            metadata=metadata,
            in_memory_case=case,
            kb_context_metadata=derive_kb_context_metadata(case),
        )

    def _parse_schema_tool_call(
        self,
        tool_call: Any,
        schema_model: Any,
    ) -> BaseInteractionResponse:
        """Parse a schema tool call response into a Pydantic model.

        Applies the same JSON cleanup (nested parsing + enum fixing) as the
        single-shot path in _generate_structured_output.
        """
        args = tool_call.function.get("arguments", "{}")
        if isinstance(args, dict):
            content = json.dumps(args)
        else:
            content = args

        # Parse JSON (strict=False allows control chars in LLM-generated strings)
        content_obj = json.loads(content, strict=False)

        # Recursively parse nested JSON strings
        content_obj = self._parse_nested_json(content_obj)

        # Coerce unresolvable state_updates to {} so Pydantic field defaults apply.
        # Covers two Fireworks/DeepSeek V3 failure modes:
        #   (a) null — LLM omitted the field entirely
        #   (b) string — JSON was truncated/malformed and _parse_nested_json
        #       could not repair it (e.g. closing "} cut off before XML tag)
        _su = (
            content_obj.get("state_updates") if isinstance(content_obj, dict) else None
        )
        if isinstance(content_obj, dict) and (_su is None or isinstance(_su, str)):
            content_obj["state_updates"] = {}

        # Fix hallucinated enum values
        schema_dict = schema_model.model_json_schema()
        content_obj = self._fix_enum_violations(
            content_obj,
            schema_dict,
            root_defs=schema_dict.get("$defs"),
        )

        # Validate with Pydantic, degrading gracefully instead of 500ing on a
        # single malformed sub-record (parse-time cross-field validators).
        parsed = self._validate_with_degradation(content_obj, schema_model)

        # Dropped-field detection: compare what the LLM emitted to what the
        # schema accepted. Any key the LLM put in the dict that isn't a
        # field on the schema gets silently dropped by Pydantic's default
        # extra="ignore". Log it so prompt-schema drift becomes observable.
        # Motivated by the prompt-instructs/schema-rejects bug class found
        # via behavioral eval — see ADR / docs.
        self._log_dropped_fields(content_obj, parsed, schema_model)
        return parsed

    def _record_schema_validation(self, schema_model, outcome: str) -> None:
        """One increment on ``schema_validation_total``.

        Shared by the degradation ladder and the non-tool structured
        single-shot path so both dispositions land in the same population —
        the A/B schema-validity rate is only meaningful over a denominator
        that includes every body the engine validated.
        """
        schema_validation_total.labels(
            schema=schema_model.__name__, outcome=outcome
        ).inc()

    def _synthesize_agent_response(self, parsed: Any, stop_reason: StopReason) -> Any:
        """Name an unusable ``agent_response`` by the response's stop reason.

        The engine owns response synthesis because it is the only layer that
        can see WHY the answer is unusable (#1442). Applied at the two places
        a parsed structured response leaves the LLM call with its envelope
        still in hand — the single-shot structured path and the tool loop's
        schema-tool call — rather than inside ``_validate_with_degradation``,
        which is shared with ``_parse_text_as_schema``: that recovery path
        must see the model's own blank answer to reject a prose-embedded
        example block, and a placeholder written first would pass its check.
        Moving synthesis up keeps the validator purely structural and passes
        nothing new down to it.

        Takes the stop reason rather than the response so the CALLER decides
        what it means: both callers pass :func:`schema_answer_stop_reason`,
        because at both a tool call is the answer rather than a handoff.

        Fires only when the answer is blank (missing answers were blanked by
        the validator). The result is a copy carrying
        ``_agent_response_synthesized``; *parsed* is returned unchanged when
        the answer is usable or the stop reason names no failure
        (``TOOL_CALLS``, which neither current caller passes).
        """
        answer = getattr(parsed, "agent_response", None)
        if not isinstance(answer, str) or answer.strip():
            return parsed
        text = synthesized_agent_response(stop_reason)
        if text is None:
            return parsed
        synthesized = parsed.model_copy(update={"agent_response": text})
        synthesized._agent_response_synthesized = True
        logger.warning(
            "agent_response_synthesized",
            extra={
                "schema": type(parsed).__name__,
                "stop_reason": stop_reason.value,
            },
        )
        return synthesized

    def _validate_with_degradation(self, content_obj, schema_model):
        """Validate LLM structured output, degrading gracefully instead of 500ing.

        Parse-time cross-field validators (e.g. ``evidence_to_add.source_file_id``
        is required unless ``USER_DESCRIPTION``; ``evidence_need_updates`` state
        ``FULFILLED`` requires ``fulfilling_evidence_ids``) reject the WHOLE
        response object when a single sub-record is malformed — which 500s the
        turn before any milestone logic runs (the surgical strip can't help: that
        operates post-parse). This is the general never-500 backstop for that
        class (redesign §9 / the deferred "S4" item):

        1. Try to validate as-is.
        2. On failure, PRUNE the specific sub-records the ValidationError points
           at (keyed off the error ``loc`` paths — general, not per-invariant)
           and re-validate: the list entry for a ``loc`` with an index, or, for
           one without, the deepest OPTIONAL sub-object on its path (nulled —
           ``root_cause_conclusion``, ``knowledge_match``, ``milestones``; fm#1502).
           The bad sub-records are quarantined; everything else on the turn
           survives.
        3. If it still fails (an error on no prunable path), drop
           ``state_updates`` entirely and keep the conversational
           ``agent_response`` — the turn survives as a conversational reply
           rather than a 500.
        4. If even that fails, re-raise the original error (truly unrecoverable).

        An out-of-range confidence usually never reaches step 2: the schema's
        validators rescale a percentage, coerce a bool, or drop the field of an
        update-shaped record inside Pydantic (fm#1502). They report through the
        validation context, and ``_account_confidence`` turns the successful
        attempt's reports into the field-level counter, the body's ``repaired``
        outcome and the turn's ``validation_repairs``. Only an unrepairable
        value on an ADD-shaped record raises, and step 2 prunes that record.

        Upstream remains the real fix: provider-native constrained generation so
        the LLM cannot emit the invalid shape ([[project-llm-structured-output-strategy]]).
        This is the backstop, not a per-variant patch.
        """
        from pydantic import ValidationError

        def _record(outcome: str):
            self._record_schema_validation(schema_model, outcome)

        def _validate(obj):
            # One attempt, with its OWN repair sink: validators report through
            # the validation context (fm#1502), and an attempt that fails must
            # report nothing — only the attempt whose result is returned counts.
            sink: list[ConfidenceRepair] = []
            parsed = schema_model.model_validate_json(
                json.dumps(obj), context={CONFIDENCE_REPAIRS_CONTEXT_KEY: sink}
            )
            return parsed, sink

        try:
            parsed, repairs = _validate(content_obj)
            # A repair happens INSIDE Pydantic, so this is a first-try success
            # either way — and a body whose confidences were rewritten is not
            # one the model got right. ``repaired`` only when every action kept
            # the model's meaning; a dropped field or a link value set aside for
            # ingest discarded something, which is what ``pruned`` counts.
            _record(
                "clean"
                if not repairs
                else (
                    "repaired"
                    if all(r.action in MEANING_PRESERVING_ACTIONS for r in repairs)
                    else "pruned"
                )
            )
            return self._account_confidence(parsed, repairs, None)
        except ValidationError as original_error:
            pruned, dropped, unhandled = self._prune_invalid_sub_records(
                content_obj, original_error, schema_model
            )
            if dropped:
                try:
                    parsed, repairs = _validate(pruned)
                    logger.warning(
                        "structured_output_degraded: pruned invalid sub-record(s) "
                        f"{dropped} from {schema_model.__name__} and continued "
                        "(parse-time validator). Turn preserved.",
                        extra={"schema": schema_model.__name__, "pruned": dropped},
                    )
                    _record("pruned")
                    return self._account_confidence(parsed, repairs, original_error)
                except ValidationError:
                    pass  # fall through to the conversational fallback

            # What the prune step removed stays removed below: the fallback
            # rungs build on the pruned body, so a record already quarantined
            # outside ``state_updates`` (an ``internal_reasoning`` conclusion)
            # cannot come back and fail the rung that drops everything else.
            base = pruned if dropped else content_obj

            # Last resort: keep the response text, drop all structured updates.
            if isinstance(content_obj, dict) and content_obj.get("state_updates"):
                fallback = {**base, "state_updates": {}}
                try:
                    parsed, repairs = _validate(fallback)
                    # The prune path already logs its locs ("Turn preserved"); this
                    # branch is reached only when an error the prune step could
                    # not place remains (no list index, no optional sub-object on
                    # its path) — log exactly those so each fallback is
                    # self-diagnosing (was it correctly non-prunable, or a prune
                    # gap?). Reference: S4 backstop observability.
                    non_prunable = [
                        (list(e.get("loc", ())), e.get("msg", "")) for e in unhandled
                    ]
                    logger.warning(
                        "structured_output_degraded: dropped all state_updates from "
                        f"{schema_model.__name__} after an unrepairable validation "
                        f"error — conversational fallback (no 500). "
                        f"Non-prunable errors: {non_prunable}",
                        extra={
                            "schema": schema_model.__name__,
                            "non_prunable_errors": non_prunable,
                        },
                    )
                    _record("state_dropped")
                    return self._account_confidence(parsed, repairs, original_error)
                except ValidationError:
                    pass

            # Rung: the model omitted the required user-facing agent_response
            # ITSELF (observed on gemini-3.5-flash resolution turns) — the rungs
            # above preserve agent_response and so cannot help. Fill it with ""
            # so a turn whose state_updates are otherwise valid survives
            # instead of 500ing. The conclusion stays the model's own (its
            # state_updates).
            # Fire when agent_response is MISSING or non-string (None, or a
            # malformed 0/[]/false the schema rejects) — i.e. not a usable reply.
            #
            # This rung is STRUCTURAL ONLY and writes no text (#1442). It used
            # to fill a fabricated "I've updated the investigation..." reply,
            # the same one for every cause, because this helper sees only the
            # parsed dict — never the provider's stop reason, which is what
            # says WHY there is no answer. Wording is now chosen one frame up
            # by ``_synthesize_agent_response``, which holds the response
            # envelope. That also REVERSES a decision this comment used to
            # record ("a model-provided "" is never overwritten"): an empty
            # answer is now named there, keyed on the stop reason, together
            # with the missing one this rung blanks — deliberately, because the
            # engine is the layer that can say why, and the service's blind
            # backstop is not where empty answers should be named.
            if isinstance(content_obj, dict) and not isinstance(
                content_obj.get("agent_response"), str
            ):
                placeholder = ""
                # Prefer keeping the model's state_updates; only DROP them as a
                # last resort — and say so, so a state-update loss is never logged
                # as a mere field-fill.
                for state_dropped, candidate in (
                    (False, base),
                    (True, {**base, "state_updates": {}}),
                ):
                    try:
                        patched = {**candidate, "agent_response": placeholder}
                        parsed, repairs = _validate(patched)
                        logger.warning(
                            "structured_output_degraded: blanked missing "
                            f"agent_response on {schema_model.__name__} (model "
                            "omitted the required user-facing field)"
                            + (
                                " AND dropped all state_updates (unrepairable)"
                                if state_dropped
                                else ""
                            )
                            + " — turn preserved, no 500.",
                            extra={
                                "schema": schema_model.__name__,
                                "state_updates_dropped": state_dropped,
                            },
                        )
                        _record(
                            "response_synthesized_state_dropped"
                            if state_dropped
                            else "response_synthesized"
                        )
                        return self._account_confidence(parsed, repairs, original_error)
                    except ValidationError:
                        continue

            _record("failed")
            raise original_error

    @staticmethod
    def _prune_invalid_sub_records(content_obj, error, schema_model=None):
        """Remove the sub-records a ValidationError flags.

        Returns ``(obj, pruned_paths, unhandled_errors)``.

        - A ``loc`` carrying a list index — ``('state_updates',
          'evidence_to_add', 0, 'source_file_id')`` or ``(..., 0)`` — prunes the
          entry at the DEEPEST index. General across any list field.
        - A ``loc`` with no index is placed on the deepest OPTIONAL sub-object
          along its path, read from ``schema_model``, and that sub-object is
          set to ``None`` — ``('state_updates', 'root_cause_conclusion',
          'likelihood')`` nulls ``root_cause_conclusion`` (fm#1502). Absence is
          what an optional sub-object means when the model has nothing to say,
          so this costs that sub-object and nothing else, where the next rung
          would drop every ``state_updates``. A required object (``state_updates``
          itself) or a non-object field (``outcome``) is never nulled: the error
          is returned as unhandled and falls through as before.

        Without ``schema_model`` only list entries are pruned.
        """
        import copy

        obj = copy.deepcopy(content_obj)
        to_remove: dict[tuple, set] = {}
        to_null: set[tuple] = set()
        unhandled: list[dict] = []
        for err in error.errors():
            loc = tuple(err.get("loc", ()))
            int_positions = [i for i, part in enumerate(loc) if isinstance(part, int)]
            if int_positions:
                last = int_positions[-1]
                list_path = loc[:last]
                to_remove.setdefault(list_path, set()).add(loc[last])
                continue
            prefix = (
                MilestoneEngine._optional_sub_record_prefix(schema_model, loc)
                if schema_model is not None
                else None
            )
            if prefix is None:
                unhandled.append(err)
            else:
                to_null.add(prefix)

        dropped: list[str] = []
        for list_path, indices in to_remove.items():
            node = obj
            ok = True
            for key in list_path:
                if isinstance(node, dict) and key in node:
                    node = node[key]
                else:
                    ok = False
                    break
            if ok and isinstance(node, list):
                for idx in sorted(indices, reverse=True):
                    if 0 <= idx < len(node):
                        del node[idx]
                        path_str = ".".join(str(p) for p in list_path)
                        dropped.append(f"{path_str}[{idx}]")

        # Shortest first, so a sub-object inside one already nulled is skipped
        # (its parent is None by then) rather than reported twice.
        for path in sorted(to_null, key=len):
            parent = obj
            for key in path[:-1]:
                parent = parent.get(key) if isinstance(parent, dict) else None
            if isinstance(parent, dict) and parent.get(path[-1]) is not None:
                parent[path[-1]] = None
                dropped.append(".".join(str(p) for p in path))
        return obj, dropped, unhandled

    @staticmethod
    def _optional_sub_record_prefix(schema_model, loc) -> Optional[tuple]:
        """The deepest prefix of ``loc`` naming an ``Optional[BaseModel]`` field.

        Walks the field annotations from ``schema_model`` down the string parts
        of ``loc``; stops at the first part that is not a model field or whose
        type is not a model. ``None`` when no optional sub-object lies on the
        path.

        Reads resolved annotations only: a quoted forward reference pydantic
        left unresolved would hide the sub-object it names, and the error would
        fall through to the drop-all rung. ``test_confidence_repair_1502``'s
        census fails if any field reachable from an engine schema carries one.
        """
        import types
        import typing

        from pydantic import BaseModel

        model = schema_model
        best: Optional[tuple] = None
        for depth, part in enumerate(loc):
            fields = getattr(model, "model_fields", None)
            if not isinstance(part, str) or not fields or part not in fields:
                break
            annotation = fields[part].annotation
            nullable = False
            if typing.get_origin(annotation) in (typing.Union, types.UnionType):
                args = typing.get_args(annotation)
                members = [a for a in args if a is not type(None)]
                nullable = len(members) < len(args)
                annotation = members[0] if len(members) == 1 else None
            if not (isinstance(annotation, type) and issubclass(annotation, BaseModel)):
                break
            if nullable:
                best = tuple(loc[: depth + 1])
            model = annotation
        return best

    @staticmethod
    def _account_confidence(parsed, repairs, original_error):
        """Make the confidence actions behind ``parsed`` observable (fm#1502).

        ``repairs`` are what the successful attempt's validators reported;
        ``original_error``, when the ladder degraded, carries the unrepairable
        ADD-shaped values whose records the ladder pruned. Each action is
        counted on ``faultmaven_schema_field_repairs_total`` and kept on the
        response for the apply step to write onto the turn's
        ``validation_repairs``. A link value set aside for ingest is neither:
        ingest counts what it decides.
        """
        kept = [r for r in repairs if r.action is not ConfidenceAction.SET_ASIDE]
        if original_error is not None:
            for err in original_error.errors():
                if err.get("type") != CONFIDENCE_UNREPAIRABLE:
                    continue
                ctx = err.get("ctx") or {}
                kept.append(
                    ConfidenceRepair(
                        schema=str(ctx.get("schema", "?")),
                        field=str(ctx.get("field", "?")),
                        action=ConfidenceAction.PRUNED,
                        raw=err.get("input"),
                        where=".".join(str(p) for p in err.get("loc", ())),
                    )
                )
        if not kept:
            return parsed
        for repair in kept:
            count_confidence_repair(repair)
        logger.warning(
            "structured_output_confidence_repaired",
            extra={
                "schema": type(parsed).__name__,
                "repairs": [repair.note() for repair in kept],
            },
        )
        if "_confidence_repairs" in getattr(type(parsed), "__private_attributes__", {}):
            parsed._confidence_repairs = kept
        return parsed

    def _log_dropped_fields(
        self,
        raw: Any,
        parsed: Any,
        schema_model: Any,
    ) -> None:
        """Log when the LLM emitted top-level or state_updates fields that
        the schema doesn't accept (and thus silently dropped). One log line
        per dropped field — feed observability/quarterly review.

        TODO: walk depth limited to top-level + state_updates. Drops nested
        deeper (e.g., state_updates.hypotheses_to_add[].some_unknown_field)
        are invisible. Generalize to recursive descent if state schemas
        grow more nested or if the runtime signal misses real drift.
        """
        try:
            top_known = set(getattr(schema_model, "model_fields", {}).keys())
            if isinstance(raw, dict):
                top_dropped = [k for k in raw.keys() if k not in top_known]
                for k in top_dropped:
                    logger.warning(
                        "structured_output_dropped_field",
                        extra={
                            "schema": schema_model.__name__,
                            "level": "top",
                            "field": k,
                        },
                    )

                # Walk one level into state_updates (the most common drop site).
                state_updates = raw.get("state_updates")
                if isinstance(state_updates, dict):
                    su_field = getattr(schema_model, "model_fields", {}).get(
                        "state_updates"
                    )
                    su_schema = (
                        getattr(su_field, "annotation", None) if su_field else None
                    )
                    su_known = (
                        set(getattr(su_schema, "model_fields", {}).keys())
                        if su_schema
                        else set()
                    )
                    if su_known:
                        for k in state_updates.keys():
                            if k not in su_known:
                                logger.warning(
                                    "structured_output_dropped_field",
                                    extra={
                                        "schema": getattr(su_schema, "__name__", "?"),
                                        "level": "state_updates",
                                        "field": k,
                                    },
                                )
        except Exception:
            # Logging must never break the response path.
            logger.debug("dropped-field detection failed", exc_info=True)

    def _parse_text_as_schema(
        self,
        text: str,
        schema_model: Any,
    ) -> BaseInteractionResponse:
        """Parse free-form LLM text as a schema instance.

        Last-resort path used when a provider ignores tool_choice=required and
        emits the structured response inline as text (often wrapped in a
        ```json fence). Mirrors the markdown stripping + nested-JSON +
        enum-fix logic in _generate_structured_output's single-shot path.

        Raises ValueError if the parsed object is structurally valid but
        semantically empty (e.g., agent_response blank). This guards against
        false positives where prose happens to embed a JSON block that fits
        the schema but doesn't represent a real response — those should
        escalate to the non-tool fallback path, not be returned as-is.
        """
        content_obj = loads_llm_json(text)
        content_obj = self._parse_nested_json(content_obj)
        _su = (
            content_obj.get("state_updates") if isinstance(content_obj, dict) else None
        )
        if isinstance(content_obj, dict) and (_su is None or isinstance(_su, str)):
            content_obj["state_updates"] = {}
        schema_dict = schema_model.model_json_schema()
        content_obj = self._fix_enum_violations(
            content_obj,
            schema_dict,
            root_defs=schema_dict.get("$defs"),
        )
        parsed = self._validate_with_degradation(content_obj, schema_model)
        self._log_dropped_fields(content_obj, parsed, schema_model)

        # Semantic guard: agent_response is the user-facing payload of every
        # BaseInteractionResponse subclass. An empty value means the recovered
        # JSON was structurally valid but contained no actual response — most
        # likely we picked up an example block from the LLM's prose. Reject
        # it so the caller escalates to the non-tool fallback path instead of
        # surfacing an empty bubble to the user.
        agent_response = getattr(parsed, "agent_response", None)
        if not agent_response or not str(agent_response).strip():
            raise ValueError(
                "parsed schema has empty agent_response — likely a prose-embedded "
                "JSON example, not a real response"
            )
        return parsed

    @staticmethod
    def _build_assistant_message(response: Any) -> dict:
        """Convert LLMResponse to OpenAI-format assistant message.

        Round-trips two kinds of provider-specific artifacts when present:
        1. Per-tool-call `provider_metadata` (e.g. signatures bound to a
           specific functionCall).
        2. Response-level `provider_metadata` (e.g. Gemini 3.x's full
           `assistant_parts` array, which carries thoughtSignatures attached
           to text/thought/functionCall parts that must all round-trip
           together — skipping any one produces a 400 on the next turn).

        Both are absent for providers/models that don't emit reasoning
        artifacts (Gemini 2.5, OpenAI Chat Completions, etc.) — the keys
        are omitted entirely so downstream serializers see no change.
        """
        tool_calls_list = []
        for tc in response.tool_calls or []:
            entry = {
                "id": tc.id,
                "type": tc.type,
                "function": tc.function,
            }
            if getattr(tc, "provider_metadata", None):
                entry["provider_metadata"] = tc.provider_metadata
            tool_calls_list.append(entry)

        msg = {
            "role": "assistant",
            "content": response.content or "",
        }
        if tool_calls_list:
            msg["tool_calls"] = tool_calls_list
        if getattr(response, "provider_metadata", None):
            msg["provider_metadata"] = response.provider_metadata
        return msg

    @classmethod
    def _truncate_tool_result(cls, text: str, tool_name: str) -> tuple[str, int]:
        """Cut an oversized tool result to the cap, protecting a tail if it has one.

        Returns the cut text and the number of *text* characters destroyed --
        the same meaning the formatter's cut reports, so the two sites can be
        aggregated into one number (#1088).

        This runs AFTER PII redaction, and that ordering is why the protection
        cannot live in the formatter alone. Redaction *expands* text: every
        entity becomes a ``<TYPE_digest>`` placeholder, so an IPv4 address grows
        from 8 characters to 29. A reservation computed while wrapping is
        therefore no longer true by the time the cap is applied, and a kb_qa
        answer sized exactly to the budget re-crosses it once its entities are
        replaced.

        For kb_qa the tail is instructions rather than prose, so cutting
        head-first would delete how the model is told to answer while keeping
        the answer it is meant to relay. The last ``len(KB_QA_RELAY_SUFFIX)``
        characters are preserved verbatim: that block is static instruction text
        containing no entity the redactor rewrites, so its length survives
        sanitisation and the slice still lands on the suffix.

        Preserving the suffix is necessary and not sufficient. Everything
        between the head and that suffix is the ANSWER, and cutting it
        head-first here would undo the whole point of eliding its middle in the
        formatter: the remediation steps and the ``Sources:`` line would go,
        leaving a suffix that instructs the model to cite "the primary source
        title(s) from the content above" with the source line gone -- the exact
        failure #1088 fixed one step earlier. This path is not hypothetical: the
        formatter sizes the answer to a budget that redaction then invalidates by
        expanding it. So the answer between the wrapper is elided in the middle
        here too, by the same helper, and only genuinely tail-less results (every
        other tool) take the plain head-first cut.
        """
        cap = cls.TOOL_RESULT_MAX_CHARS
        marker = "\n[truncated]"

        protected = len(KB_QA_RELAY_SUFFIX) if tool_name == "kb_qa" else 0
        if protected and len(text) > protected + len(marker):
            body = text[:-protected]
            suffix = text[-protected:]
            elided, dropped = _elide_answer_middle(body, cap - protected)
            return elided + suffix, dropped

        # NOTE: this branch returns cap + len(marker) characters, i.e. 12 over
        # the cap, while the kb_qa branch above fits inside it. That asymmetry
        # is real and pre-dates this change -- ``test_milestone_engine_tool_loop``
        # pins the looser bound explicitly, and #1090 pins this string
        # byte-identical as its behaviour-unchanged guarantee. Tightening it
        # would change what the model sees for every non-kb_qa tool, which is
        # a behaviour change to paths this issue never measured; kb_qa is
        # stricter because its formatter has to reserve the relay wrapper, not
        # because the cap means something different here. Left alone
        # deliberately (#1088).
        return text[:cap] + marker, max(0, len(text) - cap)

    @staticmethod
    def _format_tool_result(result: Any, tool_name: str = "") -> str:
        """Format a ToolResult into a string for the LLM."""
        if not result.success:
            return f"Error: {result.error or 'Unknown error'}"

        if result.data is None:
            return "Success (no data returned)"

        # KB results: wrap with relay instruction and source citation guidance.
        # Note: _arun returns a pre-formatted string (via KBConfig.format_response),
        # not a dict. The string includes "Sources: ..." at the end.
        if tool_name == "kb_qa" and result.data:
            content = (
                result.data if isinstance(result.data, str) else json.dumps(result.data)
            )
            logger.info(f"kb_qa result: {len(content)} chars")
            prefix = KB_QA_RELAY_PREFIX
            suffix = KB_QA_RELAY_SUFFIX
            # Reserve the wrapper before the generic cap can reach it. That
            # cap keeps the HEAD of whatever it is given, so an oversized
            # result loses its TAIL — and the tail here is not prose, it is the
            # citation format plus "return via the schema tool, do not reply
            # with plain text". A long KB answer would therefore silently strip
            # the instructions that tell the model how to answer at all, which
            # is the opposite of the intended failure. Reserving the wrapper
            # and trimming the ANSWER keeps both instructions intact. How the
            # answer itself is trimmed is a separate question, answered by
            # _elide_answer_middle below: not head-first either.
            budget = MilestoneEngine.TOOL_RESULT_MAX_CHARS - len(prefix) - len(suffix)
            if len(content) > budget:
                # Elide FIRST, then report. ``len(content) - budget`` was the
                # true drop while the cut was a plain slice to the budget; the
                # middle-elide also spends its two markers and its paragraph
                # realignment, so that expression under-reports. So does a
                # before/after length difference, which nets the inserted
                # markers off the loss. The helper returns the count instead:
                # ANSWER characters destroyed, the same meaning the loop's cut
                # site reports, because ``dropped_chars`` is what the ceiling
                # gets sized from (#1090) and it has to mean one thing.
                original_chars_answer = len(content)
                content, dropped_chars = _elide_answer_middle(content, budget)
                logger.info(
                    "kb_qa answer trimmed to fit the tool-result budget",
                    extra={
                        "original_chars": original_chars_answer,
                        "budget_chars": budget,
                        "dropped_chars": dropped_chars,
                    },
                )
                # Feed this cut into the SAME counters the tool loop uses
                # (#1088). This trim is the one that actually clips kb_qa in
                # practice -- the loop's cap rarely sees an oversized kb_qa
                # result because this ran first -- so leaving it out would make
                # kb_qa report the lowest clip rate in the system while being
                # the tool the ceiling question is about. Sized on the WRAPPED
                # string, so the number is comparable with every other tool's,
                # which is also measured wrapped and pre-cut.
                wrapped_chars = len(prefix) + original_chars_answer + len(suffix)
                tool_result_chars.labels(tool="kb_qa").observe(wrapped_chars)
                tool_result_truncated_total.labels(tool="kb_qa").inc()
                logger.warning(
                    "tool_result_truncated",
                    extra={
                        "tool": "kb_qa",
                        "original_chars": wrapped_chars,
                        "cap_chars": MilestoneEngine.TOOL_RESULT_MAX_CHARS,
                        "dropped_chars": dropped_chars,
                        "at": "formatter",
                    },
                )
            return prefix + content + suffix

        # search_file results: append citation guidance so the LLM cites
        # the source and line numbers in its response.
        #
        # #666: this instruction is the mechanism that put
        # "pasted-content-20260709T105531.txt (line 20)" in front of Beta
        # users — it tells the model to cite a name and hands it one. The
        # tool supplies ``UploadedFile.display_name`` under that key, which
        # is the same string the item's ``label`` attribute carries in
        # <evidence_collected>, so the name the model is told to cite here
        # designates something it can also see there. "source", not
        # "filename": a paste has no filename to cite.
        if tool_name == "search_file" and isinstance(result.data, dict):
            source_name = result.data.get("label", "unknown")
            results_count = result.data.get("results_count", 0)
            content = json.dumps(result.data)
            if results_count > 0:
                # HEAD, not tail. ``_truncate_tool_result`` protects a tail
                # only for kb_qa (#1088); every other tool takes a plain
                # ``text[:cap] + marker``, which is head-first. A search_file
                # excerpts result over a large paste routinely exceeds the cap,
                # so a tail-appended instruction is deleted exactly on the
                # results big enough to need it — and this is the one line that
                # hands the model the correct name to cite. Leading it also
                # reads better: the rule arrives before the data it governs.
                content = (
                    f"CITATION: When referencing these results, cite the source "
                    f'and line numbers exactly as named here (e.g., "In '
                    f'{source_name}, line 42: ...").\n\n' + content
                )
            return content

        if isinstance(result.data, str):
            return result.data
        return json.dumps(result.data)

    @staticmethod
    def _parse_nested_json(obj):
        """Recursively parse JSON strings in a dict/list structure."""
        if isinstance(obj, dict):
            return {k: MilestoneEngine._parse_nested_json(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [MilestoneEngine._parse_nested_json(item) for item in obj]
        elif isinstance(obj, str):
            try:
                parsed = json.loads(obj)
                return MilestoneEngine._parse_nested_json(parsed)
            except (json.JSONDecodeError, TypeError):
                # Fireworks/DeepSeek V3 leaks XML tool-call format artifacts.
                # Apply two repair passes before giving up:
                #
                # Pass 1: strip trailing XML closing tags (e.g. </parameter></invoke>)
                # Pass 2: for JSON containers, truncate at the last valid terminator
                #         to handle stray closing braces/brackets (e.g. "[...]}")
                stripped_obj = obj.strip()

                # Pass 1 — XML closing tags
                stripped = re.sub(r"(\s*</\w+>)+\s*$", "", stripped_obj)
                if stripped != stripped_obj:
                    try:
                        parsed = json.loads(stripped)
                        return MilestoneEngine._parse_nested_json(parsed)
                    except (json.JSONDecodeError, TypeError):
                        pass

                # Pass 2 — truncate at last valid JSON container terminator
                if stripped_obj:
                    first_ch = stripped_obj[0]
                    search_ch = (
                        "]" if first_ch == "[" else "}" if first_ch == "{" else None
                    )
                    if search_ch:
                        last_pos = stripped_obj.rfind(search_ch)
                        if last_pos > 0:
                            candidate = stripped_obj[: last_pos + 1]
                            if candidate != stripped_obj:
                                try:
                                    parsed = json.loads(candidate)
                                    return MilestoneEngine._parse_nested_json(parsed)
                                except (json.JSONDecodeError, TypeError):
                                    pass

                return obj
        else:
            return obj

    @staticmethod
    def _fix_enum_violations(obj, schema_dict, root_defs=None):
        """Recursively fix enum violations in the response object."""

        if not isinstance(obj, dict):
            return obj

        properties = schema_dict.get("properties", {})
        if root_defs is None:
            root_defs = schema_dict.get("$defs", {})
        local_defs = schema_dict.get("$defs", {})
        all_defs = {**local_defs, **root_defs}

        fixed_obj = {}
        for key, value in obj.items():
            if key not in properties:
                fixed_obj[key] = value
                continue

            prop_schema = properties[key]

            if "enum" in prop_schema and isinstance(value, str):
                valid_values = prop_schema["enum"]
                if value not in valid_values:
                    closest_match = difflib.get_close_matches(
                        value, valid_values, n=1, cutoff=0.6
                    )
                    if closest_match:
                        corrected = closest_match[0]
                        logger.warning(
                            f"Auto-correcting hallucinated enum value: "
                            f"'{value}' -> '{corrected}' for field '{key}'"
                        )
                        fixed_obj[key] = corrected
                    else:
                        fallback = valid_values[0]
                        logger.warning(
                            f"No close match for hallucinated enum '{value}', "
                            f"using fallback '{fallback}' for field '{key}'"
                        )
                        fixed_obj[key] = fallback
                else:
                    fixed_obj[key] = value

            elif isinstance(value, dict):
                nested_schema = None
                if "$ref" in prop_schema:
                    ref_name = prop_schema["$ref"].split("/")[-1]
                    nested_schema = all_defs.get(ref_name, {})
                elif "anyOf" in prop_schema:
                    for option in prop_schema["anyOf"]:
                        if "$ref" in option:
                            ref_name = option["$ref"].split("/")[-1]
                            nested_schema = all_defs.get(ref_name, {})
                            break
                        elif option.get("type") != "null":
                            nested_schema = option
                            break
                elif "properties" in prop_schema:
                    nested_schema = prop_schema

                if nested_schema:
                    fixed_obj[key] = MilestoneEngine._fix_enum_violations(
                        value, nested_schema, root_defs
                    )
                else:
                    fixed_obj[key] = value

            elif isinstance(value, list):
                fixed_list = []
                item_schema = None
                if "items" in prop_schema:
                    if "$ref" in prop_schema["items"]:
                        ref_name = prop_schema["items"]["$ref"].split("/")[-1]
                        item_schema = all_defs.get(ref_name, {})
                    else:
                        item_schema = prop_schema["items"]
                elif "anyOf" in prop_schema:
                    for option in prop_schema["anyOf"]:
                        if option.get("type") == "array" and "items" in option:
                            if "$ref" in option["items"]:
                                ref_name = option["items"]["$ref"].split("/")[-1]
                                item_schema = all_defs.get(ref_name, {})
                            else:
                                item_schema = option["items"]
                            break

                for item in value:
                    if isinstance(item, dict) and item_schema:
                        fixed_list.append(
                            MilestoneEngine._fix_enum_violations(
                                item, item_schema, root_defs
                            )
                        )
                    else:
                        fixed_list.append(item)
                fixed_obj[key] = fixed_list

            else:
                fixed_obj[key] = value

        return fixed_obj

    async def _generate_structured_output(
        self,
        prompt: str,
        schema_model: Any,
        investigation_tools: list[dict] | None = None,
        tool_context: Any | None = None,
        force_tool_use: bool = False,
        redaction_ctx: Any | None = None,
        case: Any | None = None,
        user_message: Optional[str] = None,
        fallback_prompt_builder: Optional[Callable[[], str]] = None,
        reasoning_intent: Optional[Any] = None,
        min_output_tokens: Optional[int] = None,
        base_prompt_builder: Optional[Callable[..., str]] = None,
    ) -> BaseInteractionResponse:
        """Structured-output generation with runtime context-length recovery.

        Wraps the provider call so that a context-length rejection from the LLM
        gateway (which can enforce a smaller window than our registry estimate —
        e.g. a corporate proxy or aggregator) does NOT permanently block the
        case. On such an error it recompiles the turn with the minimal
        ``FALLBACK_*`` prompt and retries once. See the context-management design
        doc §7.1. ``user_message`` is required to build the fallback; when it is
        not supplied the recovery is skipped and the error propagates.
        """
        try:
            return await self._generate_structured_output_inner(
                prompt,
                schema_model,
                investigation_tools=investigation_tools,
                tool_context=tool_context,
                force_tool_use=force_tool_use,
                redaction_ctx=redaction_ctx,
                case=case,
                fallback_prompt_builder=fallback_prompt_builder,
                reasoning_intent=reasoning_intent,
                min_output_tokens=min_output_tokens,
                base_prompt_builder=base_prompt_builder,
            )
        except Exception as exc:
            if (
                user_message is not None
                and case is not None
                and _is_context_length_error(exc)
            ):
                from faultmaven.core.investigation.prompts.templates.fallback import (
                    DEGRADED_NO_TOOLS_NOTICE,
                    get_fallback_prompt_for_case,
                )

                reason = classify_token_limit_reason(exc)
                logger.warning(
                    "prompt_context_error_recovered: provider rejected prompt as "
                    "too long (case %s, reason %s); retrying once with the minimal "
                    "fallback prompt. Original error: %s",
                    getattr(case, "case_id", "?"),
                    reason,
                    exc,
                )
                # Observability half of the degrade: the log line carries the case
                # id, this carries the rate. A SUSTAINED rate means turns are
                # routinely over the window — a prompt-sizing problem, not a
                # recovery problem.
                prompt_context_recovery_total.labels(reason=reason).inc()
                # The notice is required, not cosmetic: the fallback body lists
                # addressable files, but this retry drops the tools to reach them,
                # so without it the agent is told to search what it cannot.
                fb_prompt = get_fallback_prompt_for_case(case, user_message)
                fb_prompt += DEGRADED_NO_TOOLS_NOTICE
                # Minimal retry: drop tools to shrink the request further.
                return await self._generate_structured_output_inner(
                    fb_prompt,
                    schema_model,
                    investigation_tools=None,
                    tool_context=None,
                    force_tool_use=False,
                    redaction_ctx=redaction_ctx,
                    case=case,
                    reasoning_intent=reasoning_intent,
                    min_output_tokens=min_output_tokens,
                )
            raise

    async def _generate_structured_output_inner(
        self,
        prompt: str,
        schema_model: Any,
        investigation_tools: list[dict] | None = None,
        tool_context: Any | None = None,
        force_tool_use: bool = False,
        redaction_ctx: Any | None = None,
        case: Any | None = None,
        fallback_prompt_builder: Optional[Callable[[], str]] = None,
        reasoning_intent: Optional[Any] = None,
        min_output_tokens: Optional[int] = None,
        base_prompt_builder: Optional[Callable[..., str]] = None,
    ) -> BaseInteractionResponse:
        """
        Generate structured output from LLM using provider-agnostic capability system.

        This method automatically detects the provider's structured output capabilities
        and adjusts the prompt and response format accordingly:
        - STRICT mode: Uses json_schema with strict:true (OpenAI GPT-4o, Groq gpt-oss)
        - BEST_EFFORT mode: Uses json_object with schema in prompt (most models)
        - FUNCTION_CALLING mode: Uses tool calling pattern (Anthropic Claude)
        - NONE mode: Schema only in prompt, no API support (legacy models)

        When investigation_tools and tool_context are provided, routes through
        _tool_augmented_generate for a bounded tool-calling loop.

        Args:
            prompt: User prompt
            schema_model: Pydantic model class for expected output
            investigation_tools: OpenAI-format tool defs for investigation tools
            tool_context: ToolContext for tool execution
            force_tool_use: If True, tool_choice="required" (DA turns).
                If False, tool_choice="auto" (LLM decides).
            redaction_ctx: Case-scoped redaction context for PII sanitization
            base_prompt_builder: Re-assembles the base for the model the tool
                loop sends to, when ``prompt`` does not fit there (#614); see
                ``_tool_augmented_generate``. Unused on the non-tool path.

        Returns:
            Instantiated Pydantic model
        """
        # Apply case-scoped PII redaction to the prompt before any LLM call.
        # This covers both the tool-augmented (DA) and single-shot paths.
        # Off the event loop via the async boundary (#654).
        if redaction_ctx:
            prompt = await redaction_ctx.asanitize(prompt)
        # Branch to tool-augmented generation for DA turns with tools.
        # Two layers of protection:
        # 1. Pre-check: skip known-incompatible providers (avoids wasted API call)
        # 2. Runtime fallback: if tool calling fails on first attempt, catch
        #    ToolCallingUnsupportedError and fall through to non-tool path
        if investigation_tools and tool_context:
            from faultmaven.exceptions import ToolCallingUnsupportedError

            # Layer 1: Pre-check for known-incompatible providers/models
            # (shared capability check with the elision gate — see
            # _da_provider_supports_tools).
            if not self._da_provider_supports_tools():
                provider = self.da_provider or self.llm_provider
                model = self.da_model if self.da_provider else None
                logger.warning(
                    "Provider %s (model: %s) does not support tool calling. "
                    "Falling back to non-tool structured output path.",
                    getattr(provider, "provider_name", type(provider).__name__),
                    model or "default",
                )
            else:
                # Layer 2: Runtime fallback for unknown incompatibilities
                try:
                    return await self._tool_augmented_generate(
                        prompt,
                        schema_model,
                        investigation_tools,
                        tool_context,
                        force_tool_use=force_tool_use,
                        redaction_ctx=redaction_ctx,
                        case=case,
                        base_prompt_builder=base_prompt_builder,
                    )
                except ToolCallingUnsupportedError as e:
                    logger.warning(
                        "Tool calling failed at runtime: %s. "
                        "Falling back to non-tool structured output path.",
                        e,
                    )
                    # The prompt may have elided historical evidence on the
                    # assumption search_file would run (directed-analysis
                    # index+stub). On the non-tool path there is no search_file,
                    # so rebuild with full evidence — otherwise the agent would
                    # be stranded (elided evidence + no tool to recover it).
                    if fallback_prompt_builder is not None:
                        try:
                            prompt = fallback_prompt_builder()
                        except Exception as rebuild_exc:  # never break the fallback
                            logger.warning(
                                "fallback_prompt_builder failed (non-fatal): %s",
                                rebuild_exc,
                            )

        # Get provider-specific structured output strategy
        schema = schema_model.model_json_schema()
        strategy = self.llm_provider.get_structured_output_strategy(schema)

        # Conditionally include schema in prompt based on provider capability
        if strategy.include_schema_in_prompt:
            # Provider requires schema in prompt text (json_object or prompt_only
            # modes). SCHEMA_INSTRUCTIONS is gated on the schema's shape inside
            # the helper — see _schema_prompt_instruction.
            final_prompt = f"{prompt}{_schema_prompt_instruction(schema)}"
        else:
            # Provider supports strict json_schema - no need for schema in prompt
            final_prompt = prompt

        # Track the generation cap across retries. ``bumped`` is what tells the
        # next attempt it must not be answered from cache: the cache is keyed on
        # (case, prompt, model) and max_tokens is not part of that key, so the
        # truncated body the first attempt stored would otherwise be served back
        # instantly and the raised cap would never reach the provider (#513).
        max_tokens_state = {"value": STRUCTURED_OUTPUT_MAX_TOKENS, "bumped": False}

        def _on_truncation(exc: Exception) -> OutputTruncationError:
            """Raise the cap for the next attempt and return the typed signal.

            Both truncation sites — the provider reporting the cut, and the
            parse of a body that ran out — funnel through here, so the cap moves
            exactly once per failed attempt whichever site saw it, and the
            ladder (raise the cap, then degrade the prompt) has a single owner.

            The message is written to be self-describing rather than passing the
            original through unchanged: the JSON decoder's wording ("Expecting
            ',' delimiter") carries no hint that this was a truncation, and the
            recovery metric downstream reads exactly this text to attribute the
            degrade.
            """
            old_max = max_tokens_state["value"]
            new_max = min(old_max * 2, STRUCTURED_OUTPUT_MAX_TOKENS_CEILING)
            if new_max <= old_max:
                logger.warning(
                    "JSON truncation at the max_tokens ceiling (%s); handing off "
                    "to the minimal-prompt degrade instead of retrying at the "
                    "same size. Underlying error: %s",
                    old_max,
                    exc,
                )
                return OutputTruncationError(
                    f"Response truncated at the max_tokens ceiling ({old_max}): {exc}",
                    cap_reached=True,
                )
            max_tokens_state["value"] = new_max
            max_tokens_state["bumped"] = True
            logger.warning(
                "JSON truncation detected, increasing max_tokens: %s → %s",
                old_max,
                new_max,
            )
            return OutputTruncationError(
                f"Response truncated at max_tokens={old_max}: {exc}",
                cap_reached=False,
            )

        # Define the LLM operation for retry
        async def llm_operation():
            # Build generate parameters based on strategy mode
            current_max_tokens = max_tokens_state["value"]
            generate_params = {
                "prompt": final_prompt,
                "max_tokens": current_max_tokens,
                "temperature": 0.2,  # Lower temperature for structured output
                "case_id": case.case_id if case is not None else None,
                "bypass_cache": max_tokens_state["bumped"],
            }
            # fm#1116: the tool-less diagnostic call declares its reasoning
            # intent and output floor (#1117/#1118). Only ever set on the
            # single-shot path — the tool loop cannot carry effort on gpt-5.x.
            if reasoning_intent is not None:
                generate_params["reasoning_intent"] = reasoning_intent
            if min_output_tokens is not None:
                generate_params["min_output_tokens"] = min_output_tokens

            # Tier 2 — route schema-bound calls to STRUCTURED_OUTPUT_PROVIDER
            # when set, so operators running a weak-structured-output
            # CHAT_PROVIDER can keep that provider for chat/synthesis but
            # force schema-bound calls onto a known-STRICT provider.
            # Companion to the capability-routing fix (Tier 1):
            # capability detection only helps if the call also LANDS on the
            # provider that has the capability.
            try:
                from faultmaven.config.settings import get_settings

                _settings = get_settings()
                _override_provider = _settings.llm.structured_output_provider
                if _override_provider is not None:
                    generate_params["provider_override"] = _override_provider.value
                    # When override is set, also resolve the override
                    # provider's preferred model so we don't accidentally
                    # send CHAT_PROVIDER's model name to a different provider.
                    _override_model = _settings.llm.get_structured_output_model()
                    if _override_model:
                        generate_params["model"] = _override_model
            except Exception:
                # Settings unavailable (rare; test setup) — proceed with
                # the default routing rather than failing the turn.
                pass

            logger.debug(
                f"Structured output generation attempt with max_tokens={current_max_tokens}"
            )

            # Apply strategy-specific parameters
            if strategy.mode == StructuredOutputMode.FUNCTION_CALLING:
                # Use tools/function calling for structured output (Anthropic, etc.)
                from faultmaven.utils.schema_converter import pydantic_to_openai_tools

                generate_params["tools"] = pydantic_to_openai_tools(schema_model)
                generate_params["tool_choice"] = "required"  # Force tool use
                # Don't include response_format for function calling
            else:
                # Use response_format for JSON modes (STRICT, BEST_EFFORT, NONE)
                if strategy.response_format:
                    generate_params["response_format"] = strategy.response_format

            try:
                response = await self.llm_provider.generate(**generate_params)
            except Exception as gen_exc:
                # Some providers can see the cut themselves and raise before
                # there is any body to parse — Gemini does this on
                # finishReason=MAX_TOKENS. That path never reaches the parse
                # block below, so without this the cap was never raised and the
                # retry repeated the identical full-size call until the attempts
                # ran out (#513).
                if is_output_truncation_error(gen_exc):
                    raise _on_truncation(gen_exc) from None
                raise

            # The provider's own truncation signal, kept for the parse block
            # below rather than acted on here.
            #
            # Deliberately NOT a pre-parse gate. A cut is only a problem if it
            # cost us the ANSWER, and on the prompt-only/BEST_EFFORT modes the
            # answer is not the whole body: those models routinely emit a
            # complete ```json block and then keep talking, which is why the
            # extractor below handles "Some text\n```json\n{...}\n```\nMore
            # text". When the cap lands in that trailing prose the JSON is
            # whole and validates, and raising on the stop reason alone would
            # discard a good response, spend a second full-size generation, and
            # on a second trailing-off hand the turn to the minimal-prompt
            # degrade — throwing away the prompt context too.
            #
            # So: try to parse first, and let the stop reason decide only once
            # something has actually failed.
            provider_reported_cut = not isinstance(response, str) and getattr(
                response, "is_truncated", False
            )

            content = response if isinstance(response, str) else response.content

            # For function calling, extract from tool_calls
            if strategy.mode == StructuredOutputMode.FUNCTION_CALLING:
                # Parse response to handle tool_calls format
                if hasattr(response, "tool_calls") and response.tool_calls:
                    # Extract arguments from first tool call
                    args = response.tool_calls[0].function.get("arguments", "{}")

                    # arguments may be a string (most providers) or dict (some providers)
                    if isinstance(args, dict):
                        # Convert dict to JSON string for model_validate_json
                        content = json.dumps(args)
                    else:
                        # Already a string
                        content = args
            try:
                # First, try to load content as JSON if it's a string.
                #
                # ``content`` is REASSIGNED to whatever actually parsed, which is
                # load-bearing rather than tidiness: ``is_truncated_json_error``
                # below measures a ``JSONDecodeError.pos`` against
                # ``len(content)`` to decide whether the body was cut and the
                # ``max_tokens`` ladder should re-run (#513). Parsing a
                # de-fenced copy while leaving ``content`` fenced makes that
                # comparison read an offset from one string against the length
                # of a longer one, so the guard answers False and the ladder
                # silently stops engaging on any fenced truncated response.
                if isinstance(content, str):
                    content = json_payload_text(content)
                    content_obj = json.loads(content, strict=False)
                else:
                    content_obj = content

                # Parse any nested JSON strings (reuse class static method)
                content_obj = MilestoneEngine._parse_nested_json(content_obj)

                # Some LLMs (Fireworks/DeepSeek V3) return null for required
                # object fields, or leave state_updates as an unparsed string
                # when JSON was truncated. Coerce both to {} so Pydantic field
                # defaults apply instead of a hard validation error.
                _su = (
                    content_obj.get("state_updates")
                    if isinstance(content_obj, dict)
                    else None
                )
                if isinstance(content_obj, dict) and (
                    _su is None or isinstance(_su, str)
                ):
                    content_obj["state_updates"] = {}

                # Fix any hallucinated enum values (reuse class static method)
                schema_dict = schema_model.model_json_schema()
                content_obj = MilestoneEngine._fix_enum_violations(
                    content_obj, schema_dict, root_defs=schema_dict.get("$defs")
                )

                # Convert back to JSON string for Pydantic validation
                content = json.dumps(content_obj)

                # Counted here too, not only in the degradation ladder: this
                # path validates DIRECTLY, so leaving it out made
                # ``schema_validation_total`` a partial population — the A/B
                # schema-validity rate would be read over a denominator that
                # excludes every body the non-tool structured path served (a
                # tool-incapable model, the ToolCallingUnsupportedError
                # fallback, a FUNCTION_CALLING single shot), reporting a rate
                # for a path it never observed. One increment per BODY, so a
                # retried generation contributes one per attempt — the same
                # unit the ladder counts in.
                # Same never-500 backstop the tool-loop path has had
                # (``_parse_schema_tool_call``): a parse-time cross-field
                # validator rejecting ONE list entry (a ``suggested_follow_ups``
                # item carrying ``evidence_need_id`` with the wrong action_type,
                # an ``evidence_to_add`` row without its file id) used to fail
                # the WHOLE turn here, while the tool path pruned the entry and
                # kept the turn. fm#1116 routes tool-less turns to this path,
                # so it must degrade the same way. The helper records the
                # validation outcome itself (one increment per body, as before)
                # and re-raises only when nothing survives, which the
                # truncation check below still sees.
                parsed = self._validate_with_degradation(content_obj, schema_model)
                return self._synthesize_agent_response(
                    parsed, schema_answer_stop_reason(response)
                )
            except Exception as validation_error:
                # A body that ran out is the recoverable case: raise the cap and
                # retry. Decided POSITIONALLY against the content we just tried
                # to parse, not by matching words in the message — CPython's
                # decoder says "Expecting ',' delimiter" or "Unterminated string
                # starting at" for a cut-off body and never "truncated" or "EOF
                # while parsing", so the phrase test that used to guard this
                # matched nothing the decoder actually emits (#513).
                #
                # ``content`` is the raw body while json.loads is what failed; by
                # the time model_validate_json runs it has been re-serialized
                # from a successfully parsed object, so a schema violation there
                # is a ValidationError and correctly falls through untouched.
                if is_truncated_json_error(validation_error, content):
                    raise _on_truncation(validation_error) from None

                # The provider said it hit the cap and the body did not survive
                # parsing. The positional test above misses two shapes of that:
                # a body cut in a way that leaves it malformed in the MIDDLE
                # (correctly not truncation on its own — a bigger cap cannot fix
                # a stray token — but with the provider confirming a cut, more
                # room is the right remedy), and a ValidationError from a body
                # that parsed but lost a required field to the cut, which
                # ``is_truncated_json_error`` deliberately declines to claim
                # because it cannot tell that case from a schema violation.
                # The stop reason can (#1094).
                if provider_reported_cut:
                    raise _on_truncation(
                        RuntimeError(
                            f"provider {getattr(response, 'provider', '?')} "
                            f"reported stop_reason=max_tokens and the body did "
                            f"not parse: {validation_error}"
                        )
                    ) from None
                # Re-raise to trigger retry
                raise

        # Execute with retry and error handling
        result, error_result = await self.llm_error_handler.with_retry(
            operation=llm_operation
        )

        if result is not None:
            return result

        # All retries exhausted or non-retryable error
        if error_result:
            error_msg = error_result.message
            # Fold the triggering provider wording into the message text (e.g.
            # "prompt is too long: 250000 > 200000") so diagnostics keep it — the
            # ErrorResult's own message is the generic classifier string. We do
            # NOT chain via ``raise ... from``: that would put the provider's
            # LLMException (a context overflow is HTTP 400) on the __cause__ chain,
            # and llm_service_error_http_exception reads a provider status BEFORE
            # the engine error_code, silently re-routing the documented
            # TOKEN_LIMIT -> 503 to a 4xx -> 502. The engine error_code stays the
            # authoritative signal for this failure.
            orig = error_result.original_exception
            detail = f"{error_msg} ({orig})" if orig is not None else error_msg
            logger.error(f"Structured generation failed after retries: {detail}")
            raise MilestoneEngineError(
                f"Structured output generation failed: {detail}",
                error_code=error_result.error_code,
                category=error_result.category,
            )
        else:
            raise MilestoneEngineError(
                "Structured output generation failed with unknown error"
            )

    # =========================================================================
    # Response Processing
    # =========================================================================

    async def _process_response_structured(
        self,
        case: Case,
        user_message: str,
        response_obj: BaseInteractionResponse,
        attachments: list[dict[str, Any]] | None = None,
        upload_report: dict[str, list[str]] | None = None,
    ) -> tuple[Case, dict[str, Any]]:
        """Process structured response and update case state.

        ``upload_report`` is the turn's already-derived upload reading (see
        ``_report_turn_uploads``). ``_process_turn_impl`` derives it once, for
        EVERY path, and hands it down so the derivation — and its warnings —
        happen exactly once per turn (#1229). Callers that don't have one (the
        direct-call tests) pass ``attachments`` and the reading is derived here.
        """

        # NOTE: Validation moved AFTER post-processing to allow fallback evidence creation
        # See line 1500 for actual validation

        # Initialize metadata for this response processing
        metadata = {
            "milestones_completed": [],
            "evidence_added": [],
            "hypotheses_generated": [],
            "hypotheses_validated": [],
            "solutions_proposed": [],
            "progress_made": False,
            "status_transitioned": False,
            "outcome": TurnOutcome.CONVERSATION,
        }
        metadata.update(
            upload_report
            if upload_report is not None
            else self._report_turn_uploads(case, attachments)
        )
        # What validation had to do to the model's confidence values (fm#1502).
        # Seeded here, before any apply step appends its own repairs, because
        # this is the first point with both the accepted response and the
        # turn's metadata in hand; ingest adds its link decisions to the same
        # list.
        confidence_notes = [
            repair.note() for repair in getattr(response_obj, "_confidence_repairs", [])
        ]
        if confidence_notes:
            metadata["validation_repairs"] = confidence_notes

        # POST-PROCESSING: Apply LLM failure mitigation (Pattern-based fallback)
        # This repairs LLM classification failures before applying state updates
        # Reference: docs/working/LLM-FAILURE-MITIGATION-STRATEGY.md
        logger.debug(
            f"Post-processing LLM response: response_type={type(response_obj).__name__}, "
            f"has_state_updates={hasattr(response_obj, 'state_updates')}, "
            f"state_updates_exists={response_obj.state_updates is not None if hasattr(response_obj, 'state_updates') else False}"
        )
        if isinstance(response_obj, (InquiryResponse,)) or (
            hasattr(response_obj, "state_updates") and response_obj.state_updates
        ):
            # Apply post-processing to repair state_updates
            logger.debug(
                f"Applying post-processing to state_updates with user_message preview: {user_message[:100]}..."
            )
            response_obj.state_updates = _post_process_llm_response(
                updates=response_obj.state_updates,
                user_message=user_message,
                case=case,
            )
            # None-safe logging
            evidence_list = getattr(response_obj.state_updates, "evidence_to_add", [])
            evidence_count = len(evidence_list) if evidence_list is not None else 0
            logger.debug(
                f"Post-processing complete, evidence_to_add count: {evidence_count}"
            )

        # Validate reasoning-first requirement (AFTER post-processing to allow fallback evidence creation)
        is_valid, validation_errors, offending_milestones = validate_reasoning_first(
            response_obj, case
        )
        if not is_valid:
            error_msg = "Reasoning validation failed:\n" + "\n".join(validation_errors)
            logger.warning(
                f"Reasoning validation failed for case {case.case_id}: {error_msg}"
            )
            # Degrade gracefully: strip ONLY the milestones that actually failed
            # validation, preserving co-emitted valid ones. A single unjustified
            # milestone (e.g. a reflexive root_cause_identified) must NOT wipe a
            # validated mitigation/solution gate emitted the same turn — that
            # all-or-nothing wipe was the S1 trap mechanism (redesign §1.1, §5).
            milestones = getattr(
                getattr(response_obj, "state_updates", None), "milestones", None
            )
            stripped: list[str] = []
            if milestones and offending_milestones:
                for field_name in offending_milestones:
                    if hasattr(milestones, field_name):
                        setattr(milestones, field_name, None)
                        stripped.append(field_name)
                # Drop only the stripped milestones' justifications; keep the rest.
                # ``milestone_justifications`` is a model now, so clearing a
                # justification is setting its field to None rather than popping
                # a key — ``as_dict()`` then omits it, which is what "dropped"
                # meant when this was a dict (fm#1057).
                ir = getattr(response_obj, "internal_reasoning", None)
                justifications = getattr(ir, "milestone_justifications", None)
                if justifications is not None:
                    for field_name in stripped:
                        if field_name in type(justifications).model_fields:
                            setattr(justifications, field_name, None)
            logger.info(
                f"Surgically stripped {stripped or 'no'} milestone(s) for case "
                f"{case.case_id}; preserved the rest. Continuing with response."
            )
            # Tell the model, or it re-claims the same milestone unjustified
            # and is stripped again (fm#1677). The turn record truncates
            # feedback from the tail; the strip is the turn's first writer, and
            # prepending keeps this at the head should that order change.
            not_recorded = (
                f"Milestones {sorted(stripped)} were NOT recorded this turn. "
                if stripped
                else ""
            )
            _add_system_feedback(
                metadata,
                f"REASONING VALIDATION: {not_recorded}" + " ".join(validation_errors),
                prepend=True,
            )

        # Dispatch based on response type
        if isinstance(response_obj, InquiryResponse):
            await self._apply_inquiry_updates(
                case, response_obj.state_updates, metadata, user_message
            )
        elif isinstance(response_obj, TerminalResponse):
            # Terminal updates typically just documentation, no deep state change
            pass
        else:
            # Investigation updates (Verification, Hypothesis, Resolution, General)
            # All check 'state_updates' which matches InvestigationStateUpdate structure
            await self._apply_investigation_updates(
                case,
                response_obj.state_updates,
                metadata,
                response_obj,
                user_message,
            )

        # Store response_obj in metadata so _check_automatic_transitions can
        # access ProposedTransition for the User-Agent Handshake flow
        metadata["response_obj"] = response_obj

        return case, metadata

    async def _apply_inquiry_updates(
        self,
        case: Case,
        updates: Any,
        metadata: dict[str, Any],
        user_message: str = "",
    ) -> None:
        """Apply updates during INQUIRY phase."""
        # Capture pre-turn state for the same-turn-confirmation guard
        # applied later in this method. The design requires the user to
        # confirm a problem statement that was presented on a PRIOR turn —
        # never one that was first written this turn. The INQUIRY_TEMPLATE
        # instructs the LLM accordingly ("Never set user_confirmed_-
        # investigation=True on the same turn you first present the
        # problem statement"), but LLMs are stochastic and the rule was
        # observed to be violated on first-turn cases with explicit
        # "please investigate" phrasing. This local makes the invariant
        # enforceable independently of prompt compliance.
        #
        # The TEXT, not a boolean, because the write guard below reads it to
        # decide whether a statement the user could have SEEN already stood.
        _statement_at_turn_start = case.inquiry.proposed_problem_statement

        # Consent binds to the wording the user was SHOWN, so a revision that
        # arrives on a consent turn is not applied. Two shapes reach here:
        #
        #   - The DECIDE click. Section 0c has already set
        #     ``problem_statement_confirmed`` earlier in THIS turn, and the
        #     turn still renders an InquiryResponse. Without this guard the
        #     LLM's same-turn rewording replaced the statement after consent
        #     and ``_transition_to_investigating`` copied the new text into
        #     ``case.description`` — framing the investigation on wording the
        #     user never saw, which is the hole this whole change exists to
        #     close, left open on the click path.
        #   - The LLM path relaying a plain "yes" while re-emitting the field
        #     with cosmetic edits. Refusing the consent there would be worse
        #     than useless: the engine re-presents the reword, the user says
        #     yes again, the model rewords again, and the case never leaves
        #     INQUIRY. Dropping the reword instead commits the consent against
        #     the text it was actually given for.
        #
        # A FIRST write arriving with consent is still applied — nothing stood
        # for the user to have seen, so there is no reword to protect, and the
        # statement must persist for the next turn to present it. The consent
        # itself is refused below, by ``gate1_statement_is_confirmable``.
        _consent_on_this_turn = (
            bool(getattr(updates, "user_confirmed_investigation", False))
            or case.inquiry.problem_statement_confirmed
        )
        _statement_stood = bool((_statement_at_turn_start or "").strip())
        if updates.proposed_problem_statement and not (
            _consent_on_this_turn and _statement_stood
        ):
            case.inquiry.proposed_problem_statement = updates.proposed_problem_statement

        # Convert and store problem_confirmation from LLM schema to domain model
        if updates.problem_confirmation:
            from faultmaven.modules.case.domain.models.problem import (
                ProblemConfirmation as DomainProblemConfirmation,
            )

            case.inquiry.problem_confirmation = DomainProblemConfirmation(
                problem_type=updates.problem_confirmation.problem_type,
                severity_guess=updates.problem_confirmation.severity_guess,
            )

        # Convert and store preliminary_urgency from LLM schema to domain model
        if updates.preliminary_urgency:
            from faultmaven.modules.case.domain.models.problem import (
                PreliminaryUrgency as DomainPreliminaryUrgency,
            )
            from faultmaven.modules.case.domain.models.problem import UrgencyLevel

            case.inquiry.preliminary_urgency = DomainPreliminaryUrgency(
                level=UrgencyLevel(
                    updates.preliminary_urgency.level.lower()
                ),  # Convert uppercase to lowercase enum
                is_ongoing=getattr(updates.preliminary_urgency, "is_ongoing", False),
                is_incident_report=getattr(
                    updates.preliminary_urgency, "is_incident_report", False
                ),
                impact_assessment=updates.preliminary_urgency.impact_assessment,
                assessed_at_turn=case.current_turn,  # Use current turn number
            )

        # ``proposed_problem_statement`` has exactly ONE writer: the block
        # above, where the LLM sets it deliberately. A second writer used to
        # sit here and promote ``problem_confirmation.preliminary_guidance``
        # into the statement whenever none existed yet. That field carried no
        # description on the LLM-facing schema and was named nowhere in the
        # INQUIRY prompt, so a model filled it from its name alone — with
        # guidance. The guidance then became the problem statement, and on
        # confirmation became ``case.description`` and the frame for the whole
        # investigation. Removed together with the field (#1606); a statement
        # is now only ever what the model deliberately wrote as one.
        #
        # What the promotion hid is now COUNTED rather than papered over: a
        # turn that classified the problem but proposed nothing leaves Gate 1
        # shut, so a user confirmation on it commits nothing.
        if updates.problem_confirmation and not case.inquiry.proposed_problem_statement:
            inquiry_classified_without_statement_total.inc()

        # Two-Step Confirmation (Design Doc Section 1.2)
        #
        # The design requires explicit user confirmation before INQUIRY → INVESTIGATING.
        # Auto-confirm is NOT used — even for CRITICAL/HIGH urgency issues.
        #
        # Flow:
        #   Turn N: User reports incident → Agent presents problem statement + asks "Is this accurate?"
        #   Turn N+1: User confirms ("Yes") → LLM sets user_confirmed_investigation=True → transition fires
        #
        # This block handles two scenarios:
        # (a) LLM signals user confirmation via user_confirmed_investigation=True
        # (b) Logging for informational/urgent cases (no auto-transition)
        _is_incident = updates.preliminary_urgency and getattr(
            updates.preliminary_urgency, "is_incident_report", False
        )

        # Check if LLM detected user confirmation of the problem statement.
        # Same-turn-confirmation guard: a statement must have stood BEFORE
        # this turn, or the LLM is writing it and confirming it in one shot,
        # which collapses the User-Agent Handshake. Binding consent to the
        # wording the user SAW is handled on the write side above. See
        # ``gate1_statement_is_confirmable`` and the captured
        # _statement_at_turn_start at the top of this method.
        if (
            getattr(updates, "user_confirmed_investigation", False)
            and case.inquiry.proposed_problem_statement
            and case.inquiry.proposed_problem_statement.strip()
            and not case.inquiry.problem_statement_confirmed
            and gate1_statement_is_confirmable(_statement_at_turn_start)
        ):
            case.inquiry.problem_statement_confirmed = True
            case.inquiry.problem_statement_confirmed_at = datetime.now(UTC)
            logger.info(
                f"User confirmed problem statement — transitioning to INVESTIGATING. "
                f"statement='{case.inquiry.proposed_problem_statement[:80]}...'"
            )
        elif (
            getattr(updates, "user_confirmed_investigation", False)
            and case.inquiry.proposed_problem_statement
            and case.inquiry.proposed_problem_statement.strip()
            and not case.inquiry.problem_statement_confirmed
            and not gate1_statement_is_confirmable(_statement_at_turn_start)
        ):
            # LLM tried to set the problem statement AND confirm investigation
            # in the same turn — design forbids this (the user must see the
            # statement first, then confirm on a subsequent turn). Refuse
            # the transition; Gate 1 stays pending, so the engine composes the
            # statement into the very next turn and re-offers the pair.
            #
            # There is no recovery FLAG any more. ``handshake_deferred_at_turn``
            # existed to tell the following turn to re-present — a narrow proxy
            # for "the user has not seen this statement", carried because
            # presentation was the LLM's job and it had to be told. Presentation
            # is now the engine's, and it happens on EVERY Gate-1-pending turn,
            # so the recovery the flag arranged is the ordinary path (#1607).
            inquiry_handshake_deferred_total.inc()
            logger.warning(
                f"Same-turn-confirmation guard rejected INQUIRY→INVESTIGATING "
                f"for case {case.case_id}: LLM emitted "
                f"user_confirmed_investigation=True on the same turn that "
                f"first set proposed_problem_statement. Deferring to next turn.",
                extra={
                    "case_id": case.case_id,
                    "turn": case.current_turn,
                    "statement_preview": case.inquiry.proposed_problem_statement[:80],
                },
            )
        elif (
            updates.preliminary_urgency
            and updates.preliminary_urgency.level in ["CRITICAL", "HIGH"]
            and updates.preliminary_urgency.is_ongoing
            and not _is_incident
        ):
            # LLM flagged HIGH urgency but did NOT mark as incident report.
            # This typically means the user asked an informational/how-to question
            # about a topic that involves failures (e.g., "How do I check logs of a
            # restarting pod?"). Stay in INQUIRY.
            logger.info(
                f"Urgent signals detected but is_incident_report=False — "
                f"treating as informational query, staying in INQUIRY. "
                f"level={updates.preliminary_urgency.level}, "
                f"problem_type={updates.problem_confirmation.problem_type if updates.problem_confirmation else 'unknown'}"
            )
        elif (
            _is_incident
            and updates.preliminary_urgency
            and updates.preliminary_urgency.level in ["CRITICAL", "HIGH"]
            and updates.preliminary_urgency.is_ongoing
            and not case.inquiry.problem_statement_confirmed
        ):
            # Urgent incident detected — agent should present problem statement
            # and ask for confirmation in its response. Transition will happen on
            # the NEXT turn when user confirms.
            logger.info(
                f"Urgent incident detected ({updates.preliminary_urgency.level} + ongoing). "
                f"Agent will present problem statement for user confirmation. "
                f"has_statement={bool(case.inquiry.proposed_problem_statement)}"
            )

        # Store KB match on case when LLM identifies one (Gap #5a)
        # This populates InquiryData.knowledge_matches so we can validate
        # confidence thresholds when knowledge_resolution arrives (possibly in a later turn)
        if updates.knowledge_match:
            km = updates.knowledge_match
            case.inquiry.knowledge_matches.append(
                KnowledgeMatch(
                    match_id=km.match_type
                    + "_"
                    + str(len(case.inquiry.knowledge_matches)),
                    match_type=km.match_type,
                    relevance_score=km.match_likelihood,
                    summary=km.match_summary,
                    potential_solution=km.suggested_solution,
                )
            )
            logger.info(
                f"KB match stored: type={km.match_type}, "
                f"likelihood={km.match_likelihood:.2f}, "
                f"summary={km.match_summary[:80]}"
            )

        # Check for KB Resolution
        if updates.knowledge_resolution:
            case.inquiry.knowledge_resolution = KnowledgeResolution(
                match_id=updates.knowledge_resolution.match_id,
                match_type=updates.knowledge_resolution.match_type,
                solution_applied=updates.knowledge_resolution.solution_applied,
                user_confirmation=updates.knowledge_resolution.user_confirmation,
            )
            # v3: knowledge_resolution received during INQUIRY is stored
            # for visibility but is NOT a transition trigger. The LLM
            # should emit knowledge_resolution during INVESTIGATING (when
            # the user confirms a runbook fix worked), not INQUIRY.
            logger.warning(
                "Case %s: knowledge_resolution emitted during INQUIRY; "
                "v3 expects this during INVESTIGATING (after problem confirmation). "
                "Storing for audit but not transitioning.",
                case.case_id,
            )

        # Post-010 (strict evidence model): NO evidence creation during
        # INQUIRY. Evidence presupposes a confirmed claim; during INQUIRY
        # the claim is still being formed. Uploaded files persist in
        # ``case.uploaded_files`` with their preprocessing artifacts
        # (summary, structural_index, data_type, coverage timestamps);
        # the LLM evaluates them and emits ``evidence_to_add`` once the
        # case transitions to INVESTIGATING.
        # See docs/architecture/investigation-engine/
        # evidence-driven-investigation-framework.md §5.

    def _apply_hypothesis_action_intent(
        self,
        case: "Case",
        intent_data: dict,
        user_message: str,
        metadata: dict[str, Any],
    ) -> None:
        """Apply an explicit user ``hypothesis_action`` intent
        (frontend/IntentResolver) — ``refute`` | ``validate`` | ``retire`` —
        BEFORE LLM processing, so the agent sees the updated state in its
        context and can acknowledge.

        Terminal immutability holds on EVERY write path, not just the LLM
        apply layer (#843): a hypothesis already ``REFUTED``/``RETIRED`` is out
        of the differential for good, and this path refuses all three actions
        against it, surfacing why via ``system_feedback``. The concrete
        corruption the guard prevents: retiring an already-REFUTED hypothesis
        would strand ``refutation_reason`` on ``state=RETIRED`` — a pair the
        domain model rejects — and because ``validate_assignment`` is off, the
        in-place write would succeed silently and only surface as a 500 at the
        next Case reconstruction, far from its cause.

        On refusal the action is NOT marked applied
        (``hypothesis_action_applied`` stays unset).
        """
        hypothesis_id = intent_data.get("hypothesis_id")
        action = intent_data.get("action")  # validate | refute | retire

        if not (hypothesis_id and action and case.hypotheses):
            return
        hypothesis = case.hypotheses.get(hypothesis_id)

        if hypothesis and hypothesis.state.is_terminal:
            current_fb = metadata.get("system_feedback", "") or ""
            metadata["system_feedback"] = "\n".join(
                [
                    current_fb,
                    f"Hypothesis {hypothesis_id} is already "
                    f"{hypothesis.state.value} (terminal) — it "
                    f"cannot be {action}d. Open a NEW hypothesis "
                    f"if that theory is back in play.",
                ]
            ).strip()
            logger.info(
                f"Hypothesis {hypothesis_id} {action} intent refused "
                f"for case {case.case_id}: state "
                f"{hypothesis.state.value} is terminal"
            )
        elif hypothesis:
            if action == "refute":
                self.hypothesis_manager.refute_hypothesis(
                    hypothesis=hypothesis,
                    current_turn=case.current_turn,
                    refuting_evidence_ids=[],
                    reason=user_message or "User refuted",
                )
            elif action == "validate":
                # #695 Defect A: a user "validate" intent records a
                # strong PRIOR, not a validation-by-assertion. The
                # single model derives VALIDATED from the chain root's
                # evidence (project_hypothesis_states_from_roots); a
                # bare assertion cannot mint it (the causal-node model
                # forbids validation by assertion). The user's
                # definitive confirmation is the RESOLVED handshake
                # (the confirm-stamp), not this mid-investigation
                # signal. Surface the new semantics so the affordance
                # does not read as a silent no-op.
                hypothesis.likelihood = 1.0
                hypothesis.last_updated_turn = case.current_turn
                # The user's explicit validation restarts the stagnation clock,
                # even when belief was already near 1.0: stagnation flags lines
                # the investigation is not moving, and the user has just named
                # this one as the line to pursue. Left unrecorded, a positive
                # counter from earlier turns made this a stagnant turn, so
                # housekeeping decayed the user's belief on the spot — or
                # anti-anchoring retired the hypothesis the user just affirmed.
                hypothesis.last_progress_at_turn = case.current_turn
                hypothesis.iterations_without_progress = 0
                current_fb = metadata.get("system_feedback", "") or ""
                metadata["system_feedback"] = "\n".join(
                    [
                        current_fb,
                        f"Recorded your strong belief in hypothesis "
                        f"{hypothesis_id}. It is marked validated once "
                        f"its cause chain is confirmed by evidence — "
                        f"link supporting evidence to its root to get "
                        f"there.",
                    ]
                ).strip()
            elif action == "retire":
                hypothesis.state = HypothesisState.RETIRED
                # Bounded at the write, not left to the field validator: this is
                # the user's own message, and letting an over-long one raise here
                # would turn a retire intent into a failed turn.
                hypothesis.retirement_reason = (user_message or "User retired")[:200]
                hypothesis.last_updated_turn = case.current_turn

            metadata["hypothesis_action_applied"] = True
            logger.info(
                f"Hypothesis {hypothesis_id} {action}d via explicit intent "
                f"for case {case.case_id}"
            )
        else:
            logger.warning(
                f"Hypothesis {hypothesis_id} not found in case {case.case_id}"
            )

    def _apply_hypothesis_updates(
        self,
        case: "Case",
        entries: list,
        metadata: dict[str, Any],
        current_turn: int,
    ) -> None:
        """Apply the LLM's per-turn hypothesis lifecycle updates
        (``state_updates.hypotheses_to_update``).

        Scoped to the DISCONFIRMATION signal — ``state=REFUTED`` with a
        ``refutation_reason``, the disproof that drives M6 demotion of a grounded
        cause — plus likelihood tracking. The schema and prompt have long emitted
        these, but the engine never applied them (no read of
        ``hypotheses_to_update`` anywhere); wired here.

        Deliberately NOT applied in this slice: ``VALIDATED`` / ``RETIRED`` /
        ``ACTIVE`` / ``INCONCLUSIVE`` transitions. ``cause_state`` grounding is
        derived from the ``RootCauseConclusion``, not ``hypothesis.state``, so
        flipping state here would only perturb the ACTIVE-count derivation
        without grounding the cause; richer lifecycle wiring is a separate change.

        Guards:

        - **Terminal immutability.** ``REFUTED`` / ``RETIRED`` are terminal — the
          methodology forbids reviving a disproven/retired hypothesis (it would
          undo the very demotion M6 exists for), and a bare state-flip away from
          ``REFUTED`` would strand ``refutation_reason`` and fail the model's
          pair invariant on reload. A change request against a terminal
          hypothesis is refused and surfaced to the LLM via ``system_feedback``.
        - **Pair integrity.** ``state=REFUTED`` without a ``refutation_reason`` is
          refused (we do not record a disproof on no stated grounds) and surfaced
          as feedback; no likelihood from that same entry is applied (it was a
          refutation entry).

        Best-effort otherwise: an unknown id is logged and skipped, never raised;
        ``new_index_N`` placeholders resolve against hypotheses created this turn.
        Refutation goes through the canonical ``refute_hypothesis``; likelihood
        through ``update_hypothesis_likelihood`` (clamps, maintains the
        progress/decay counters).
        """
        if not entries:
            return
        metadata.setdefault("hypotheses_updated", [])
        feedback: list[str] = []

        # ONE entry per hypothesis. The ``Dict[str, HypothesisUpdate]`` this
        # replaced enforced that for free; a list does not, and everything below
        # assumes it (fm#1057). Two entries naming the same hypothesis would BOTH
        # be applied: the second likelihood update reads the value the first just
        # wrote, sees |delta| < 0.05 and charges ``iterations_without_progress``
        # on a turn that made progress, feeding the stagnation/deadlock repair
        # path; a repeated REFUTED tells the model its own accepted refutation
        # was rejected as "terminal". Last entry wins, which is what a duplicated
        # JSON object key did. Resolve FIRST, so an id and the ``new_index_N``
        # that points at the same hypothesis collapse together.
        resolved: dict[str, Any] = {}
        for upd in entries:
            resolved[
                self._resolve_id_ref(
                    upd.hypothesis_id,
                    metadata.get("hyp_emit_order")
                    or metadata.get("hypotheses_generated", []),
                    "hyp",
                )
            ] = upd
        if len(resolved) < len(entries):
            logger.warning(
                "Case %s: hypotheses_to_update carried %d entries for %d "
                "hypotheses; kept the last per hypothesis.",
                case.case_id,
                len(entries),
                len(resolved),
            )

        for h_id, upd in resolved.items():
            raw_id = upd.hypothesis_id
            hypothesis = case.hypotheses.get(h_id)
            if hypothesis is None:
                logger.warning(
                    f"Hypothesis update skipped: id '{h_id}' not found "
                    f"(resolved from '{raw_id}'). "
                    f"Available: {list(case.hypotheses.keys())}"
                )
                continue

            # Terminal states are immutable (see docstring).
            if hypothesis.state.is_terminal:
                if (
                    upd.state and upd.state != hypothesis.state
                ) or upd.likelihood is not None:
                    feedback.append(
                        f"Hypothesis {h_id} is {hypothesis.state.value} (terminal) "
                        f"— its state/likelihood cannot be changed. Open a NEW "
                        f"hypothesis if that theory is back in play."
                    )
                continue

            # A REFUTED request is a refutation ENTRY: handle it and nothing else
            # (no likelihood from the same entry — it was a disconfirmation).
            if upd.state == HypothesisState.REFUTED:
                if upd.refutation_reason and upd.refutation_reason.strip():
                    self.hypothesis_manager.refute_hypothesis(
                        hypothesis=hypothesis,
                        current_turn=current_turn,
                        refuting_evidence_ids=[],
                        reason=upd.refutation_reason,
                    )
                    metadata["hypotheses_updated"].append(h_id)
                else:
                    feedback.append(
                        f"Hypothesis {h_id}: state=REFUTED requires a "
                        f"refutation_reason (they travel as a pair); the "
                        f"refutation was not applied."
                    )
                continue

            # Re-root request (chain mode): record the ref so the chain-emission
            # linking pass re-points this existing hypothesis onto the named chain
            # root, replacing any earlier root it carried. Applied there (not
            # here) because the target node is commonly emitted this same turn in
            # causal_nodes_to_add and must be ingested first.
            reroot = getattr(upd, "root_node_ref", None)
            if reroot:
                metadata.setdefault("hyp_root_refs", {})[h_id] = reroot
                metadata["hypotheses_updated"].append(h_id)

            # Non-REFUTED state transitions are intentionally not applied here.
            # Likelihood updates are DEFERRED to after the same-turn
            # hypothesis_evidence_links pass (``_apply_deferred_likelihood_
            # updates``): the B1 evidence-free cap must judge the hypothesis
            # WITH the links this same emission carries — the prompt mandates
            # record → link → set-likelihood in one turn, and capping before
            # the link lands would gaslight a model that did exactly that.
            if upd.likelihood is not None:
                metadata.setdefault("deferred_likelihood_updates", []).append(
                    (h_id, upd.likelihood)
                )
                if not reroot:
                    metadata["hypotheses_updated"].append(h_id)

        if feedback:
            current = metadata.get("system_feedback", "") or ""
            metadata["system_feedback"] = "\n".join([current, *feedback]).strip()

    def _apply_deferred_likelihood_updates(
        self,
        case: "Case",
        metadata: dict[str, Any],
        current_turn: int,
    ) -> None:
        """Apply the likelihood updates stashed by ``_apply_hypothesis_updates``
        — AFTER the same-turn ``hypothesis_evidence_links`` pass, so the B1
        evidence-free cap sees the links this emission carried. The mutator
        caps an evidence-free (or hedged-links-only) update at the prior bar;
        when it does, tell the LLM WHY its number was not applied — the
        recovery is to record the observation as evidence and link it with a
        confident stance, not to re-assert a larger number."""
        deferred = metadata.pop("deferred_likelihood_updates", None)
        if not deferred:
            return
        feedback: list[str] = []
        for h_id, likelihood in deferred:
            hypothesis = case.hypotheses.get(h_id)
            if hypothesis is None:
                continue
            # Re-check terminal immutability HERE, not only at stash time:
            # the links pass between stash and apply can auto-REFUTE this
            # same hypothesis (two REFUTES links -> likelihood <= 0.20 ->
            # _check_state_transition), and applying the stale pre-refutation
            # number would resurrect a terminal hypothesis's likelihood
            # against its own refutation_reason.
            if hypothesis.state.is_terminal:
                feedback.append(
                    f"Hypothesis {h_id}: likelihood update not applied — the "
                    f"hypothesis became {hypothesis.state.value} this turn "
                    f"(terminal states are immutable)."
                )
                continue
            self.hypothesis_manager.update_hypothesis_likelihood(
                hypothesis,
                likelihood,
                current_turn,
                reason="LLM hypothesis update",
                case=case,  # chain-axis grounding visible to the B1 cap
            )
            if hypothesis.likelihood < min(1.0, likelihood) - 1e-9:
                feedback.append(
                    f"Hypothesis {h_id}: likelihood capped at "
                    f"{hypothesis.likelihood:.2f} — a hypothesis with no "
                    f"confident supporting evidence links is a prior, not a "
                    f"conclusion. Record the observation as evidence and "
                    f"link it (hypothesis_evidence_links) to raise belief."
                )
        if feedback:
            current = metadata.get("system_feedback", "") or ""
            metadata["system_feedback"] = "\n".join([current, *feedback]).strip()

    def _apply_chain_emission(
        self,
        case: "Case",
        updates: Any,
        metadata: dict[str, Any],
    ) -> None:
        """Ingest the LLM's emitted causal chain and link new hypotheses to their
        roots (the emitted chain is the sole source of the causal graph; the
        transitional flag and flat->chain bridge were removed).

        Lazy backward expansion (methodology §5/S3): build the graph from the
        emitted nodes/edges/node-evidence, then set ``root_node_id``/``path`` on
        each hypothesis whose spec carried a ``root_node_ref``. A hypothesis the
        LLM never links stays flat (``root_node_id`` is None) — the graph is
        emission-only, so there is no projection floor.

        Best-effort: an unresolvable ``root_node_ref`` leaves the hypothesis flat
        rather than raising. ``path`` may be ``[]`` when the chain has not yet
        reached ``D`` (still being expanded); the model permits ``root_node_id``
        set with an empty path.
        """
        created = ingest_emitted_chain(
            case,
            getattr(updates, "causal_nodes_to_add", None) or [],
            getattr(updates, "causal_edges_to_add", None) or [],
            getattr(updates, "node_evidence_links", None) or [],
            case.current_turn,
            evidence_created_ids=metadata.get("evidence_added", []),
            validation_repairs=metadata.setdefault("validation_repairs", []),
        )

        def _resolve_root(ref: str | None) -> str | None:
            """Resolve a root_node_ref to a ROOT node id, or None.

            A hypothesis root must be a ROOT node (M1/M3) — refs that resolve to
            an intermediate or to the PROBLEM node D are rejected (the hypothesis
            stays flat).
            """
            if not ref:
                return None
            if ref.startswith("new_index_"):
                try:
                    idx = int(ref[len("new_index_") :])
                except ValueError:
                    return None
                node_id = created[idx] if 0 <= idx < len(created) else None
            else:
                node_id = ref if ref in case.causal_nodes else None
            node = case.causal_nodes.get(node_id) if node_id else None
            return (
                node_id
                if node is not None and node.node_type == NodeType.ROOT
                else None
            )

        # Link each hypothesis to its chain root via the explicit
        # hyp_id -> root_node_ref map (recorded at creation, or on a re-root
        # update when the LLM elaborates a previously-posited hypothesis into a
        # real chain). Re-rooting abandons the hypothesis's old chain; collect any
        # of its now-dead nodes so the elaborated chain does not co-exist with the
        # abandoned degenerate stub for the same cause (the double-representation /
        # orphan-chain divergence).
        def _other_owner(hyp_id: str, root_id: str):
            """The OTHER hypothesis currently rooted at ``root_id``, if any."""
            return next(
                (
                    h
                    for h in case.hypotheses.values()
                    if h.hypothesis_id != hyp_id and h.root_node_id == root_id
                ),
                None,
            )

        def _attach(hyp, root_id: str) -> list | None:
            """Point ``hyp`` at ``root_id``. Returns the path it abandoned (``[]``
            when it abandoned nothing), or None when the move was declined.

            On a RE-ROOT (the hypothesis already had a root) only move it once
            the new chain actually reaches D. Abandoning a working [root, D]
            link for an empty path would strand the hypothesis: the graph is
            emission-only (no projection floor), so nothing would restore the
            link this turn. At creation (no prior root) an empty path is fine —
            there was no link to lose.
            """
            old_root = hyp.root_node_id
            old_path = hyp.path or []
            new_path = chain_path_to_problem(root_id, case)
            if old_root and old_root != root_id and not new_path:
                return None
            hyp.root_node_id = root_id
            hyp.path = new_path
            return old_path if (old_root and old_root != root_id) else []

        # One cause, one chain (M3/§7.8.1): a chain root belongs to exactly ONE
        # hypothesis. A ref naming a root ANOTHER hypothesis owns is REFUSED —
        # adopting a foreign chain silently re-labels this hypothesis's cause with
        # the owner's statement, and everything derived from the root afterwards
        # (the mirrored support, the node state, the VALIDATED projection back onto
        # the hypothesis, the report's causal map) then speaks about a cause this
        # hypothesis never claimed. Observed live (fm#1091): a cache-exhaustion
        # hypothesis adopted the root of a REFUTED runner-out-of-memory hypothesis,
        # and the resolution summary drew that refuted statement as the validated
        # cause of the problem while the real cause appeared nowhere in the map.
        #
        # Contested refs are settled in a SECOND pass, because the batch is applied
        # in emission order (adds before re-roots) and a root that is owned when we
        # first read it may be FREED by a re-root later in the same batch — the
        # hand-off shape, where the owner deepens onto a new root and the old one
        # becomes the new hypothesis's cause. Judging on first read would refuse a
        # hand-off the model expressed correctly, AND then GC the very chain it
        # handed over.
        abandoned: list[list] = []
        contested: list[tuple[str, str]] = []
        for hyp_id, ref in metadata.get("hyp_root_refs", {}).items():
            root_id = _resolve_root(ref)
            hyp = case.hypotheses.get(hyp_id)
            if root_id is None or hyp is None:
                continue
            if _other_owner(hyp_id, root_id) is not None:
                contested.append((hyp_id, root_id))
                continue
            freed = _attach(hyp, root_id)
            if freed:
                abandoned.append(freed)

        for hyp_id, root_id in contested:
            hyp = case.hypotheses.get(hyp_id)
            if hyp is None:
                continue
            owner = _other_owner(hyp_id, root_id)
            if owner is None:  # freed by a re-root above — the hand-off, honored
                freed = _attach(hyp, root_id)
                if freed:
                    abandoned.append(freed)
                continue
            hypothesis_root_adoption_refused_total.inc()
            _add_system_feedback(
                metadata,
                f"Hypothesis {hyp_id} was NOT anchored to node {root_id}: "
                f"that node is already the chain root of {owner.hypothesis_id} "
                f"('{(owner.statement or '')[:80]}'). One cause = one chain. "
                f"Emit a NEW root node stating THIS hypothesis's own cause and "
                f"point its root_node_ref at it — or, if the two are the same "
                f"cause, update {owner.hypothesis_id} instead of keeping both.",
            )
            logger.info(
                "Refused hypothesis root adoption (fm#1091): %s -> %s owned by %s",
                hyp_id,
                root_id,
                owner.hypothesis_id,
            )

        # GC runs only once every move is settled: a chain abandoned by a re-root
        # may have been ADOPTED by a hand-off in the second pass, and
        # prune_abandoned_nodes drops only what no hypothesis still references.
        for old_path in abandoned:
            self._gc_orphan_chain(case, old_path)

        # B1 (#695): mirror each hypothesis's flat causal SUPPORTS links onto its
        # (now-linked) chain ROOT node. The flat hypothesis_evidence and
        # causal_node_evidence axes are disjoint, so grounding the LLM recorded
        # only on the hypothesis left its root node with zero causal support and
        # uncertifiable. Runs AFTER root_node_id is assigned above and BEFORE the
        # derive_node_states recompute; provides candidate links only (the
        # independence/restatement/AND-gate filters still decide validation).
        mirror_hypothesis_support_to_root_nodes(case, case.current_turn)

        # B2 (#695): resolve the RCC's names_root_node_id placeholder. When the
        # LLM names its cause's root as a same-turn new_index_N ref, ingest
        # resolved that ref for nodes/evidence/hypotheses but not for the RCC —
        # it persisted as the placeholder, and Tier-1 RCC->hypothesis attribution
        # (link_llm_rcc_to_cause) could never match a real cn_ id, so
        # validated_hypothesis_id stayed null. Resolve it here against the same
        # `created` list, on the AUTHORING turn only (the placeholder indexes
        # THIS turn's emission; a prior-turn placeholder would mis-resolve). A
        # ref that resolves to a non-root / unknown node becomes None — an honest
        # "unnamed" that falls through to the Tier-2 lexical fallback.
        if metadata.get("rcc_authored_this_turn") and case.root_cause_conclusion:
            named = getattr(case.root_cause_conclusion, "names_root_node_id", None)
            if named and named.startswith("new_index_"):
                case.root_cause_conclusion.names_root_node_id = _resolve_root(named)

        # Deductive validation (§7.1.1): resolve the ROOT survivors the LLM
        # certified as the sole survivor of an EXHAUSTIVE differential. The
        # resolved id set is the exhaustiveness assertion (guard #1 — the one the
        # engine cannot compute); it is stashed for the assessment recompute, which
        # runs ``validate_by_exclusion`` AFTER ``derive_node_states`` has settled the
        # siblings' states so the "all-but-survivor absolutely refuted" guard can be
        # checked. ``_resolve_root`` enforces ROOT-only (a survivor must be a root
        # cause); an unresolvable/non-root ref is silently dropped.
        survivor_ids: set[str] = set()
        for dv in getattr(updates, "deductive_validations", None) or []:
            root_id = _resolve_root(getattr(dv, "survivor_node_ref", None))
            if root_id is not None:
                survivor_ids.add(root_id)
        if survivor_ids:
            metadata["deductive_survivor_ids"] = survivor_ids

    @staticmethod
    def _gc_orphan_chain(case: "Case", abandoned_node_ids: list) -> None:
        """Drop the nodes of a chain abandoned by a hypothesis re-root that are
        now dead. Thin delegate to the pure ``prune_abandoned_nodes`` (shared
        with the orphan-chain resolution post-pass)."""
        prune_abandoned_nodes(case, abandoned_node_ids)

    @staticmethod
    def _nudge_ambiguous_orphan_chains(case: "Case", metadata: dict[str, Any]) -> None:
        """Run the orphan-chain resolution post-pass. ``resolve_orphan_chains``
        re-attaches any UNAMBIGUOUS double-representation in place (T1); for the
        ambiguous remainder it returns the orphan + its candidate hypotheses,
        which we surface to the LLM next turn via ``system_feedback`` (T2a) so it
        re-roots or declares the chain separate — the engine does not guess."""
        ambiguous = resolve_orphan_chains(case)
        if not ambiguous:
            return
        lines = [
            "Unlinked causal chain(s) may restate an existing hypothesis. If a "
            "chain and a hypothesis are the SAME cause, re-root the hypothesis "
            "onto the chain (set its root_node_ref); if they are different "
            "causes, keep them separate:"
        ]
        for orphan in ambiguous[:3]:
            cands = "; ".join(orphan["candidate_hypotheses"][:2])
            lines.append(
                f"- chain root '{orphan['statement'][:80]}' ~ hypothesis '{cands[:120]}'"
            )
        current = metadata.get("system_feedback", "") or ""
        metadata["system_feedback"] = "\n".join([current, *lines]).strip()

    async def _apply_investigation_updates(
        self,
        case: Case,
        updates: Any,
        metadata: dict[str, Any],
        response_obj: Any | None = None,
        user_message: str = "",
    ) -> None:
        """Apply updates during INVESTIGATING phase."""
        # 0. Check for Proactive Blocker Detection — surface as system feedback
        if hasattr(updates, "missing_critical_data") and updates.missing_critical_data:
            blocker = updates.missing_critical_data
            blocker_msg = (
                f"DATA QUALITY ISSUE: {blocker.description}. "
                f"Expected: {blocker.what_was_expected}. Found: {blocker.what_was_found}. "
                f"Impact: {blocker.impact}."
            )
            if blocker.suggested_alternatives:
                blocker_msg += (
                    f" Alternatives: {', '.join(blocker.suggested_alternatives)}"
                )
            current_feedback = metadata.get("system_feedback", "") or ""
            metadata["system_feedback"] = f"{current_feedback}\n{blocker_msg}".strip()
            metadata["data_blocker_detected"] = True
            logger.warning(f"Case {case.case_id} data blocker: {blocker.description}")

        # Track evidence quality issues (non-blocking)
        if (
            hasattr(updates, "evidence_quality_issues")
            and updates.evidence_quality_issues
        ):
            for issue in updates.evidence_quality_issues:
                logger.info(
                    f"Evidence quality issue detected: {issue.evidence_id} - {issue.issue_type} ({issue.severity})"
                )
                # Could store these in case metadata for future reference
                metadata.setdefault("evidence_quality_issues", []).append(
                    {
                        "evidence_id": issue.evidence_id,
                        "issue_type": issue.issue_type,
                        "severity": issue.severity,
                    }
                )

        # 1a. Save Root Cause Conclusion
        # Must happen before milestone processing so the KB pre-fetch below
        # can use the conclusion text in the same turn.
        if hasattr(updates, "root_cause_conclusion") and updates.root_cause_conclusion:
            rcc = updates.root_cause_conclusion
            metadata["rcc_authored_this_turn"] = True
            case.root_cause_conclusion = RootCauseConclusion(
                root_cause=rcc.root_cause,
                mechanism=rcc.mechanism,
                evidence_basis=rcc.evidence_ids,
                likelihood=rcc.likelihood,
                confidence_level=ConfidenceLevel.from_score(rcc.likelihood),
                # INV-35: attribution hint; the chain nodes/hypotheses this turn
                # are ingested later (_apply_chain_emission), so the engine
                # resolves this to validated_hypothesis_id at cause-state recompute
                # (link_llm_rcc_to_cause tier 1), not here.
                names_root_node_id=getattr(rcc, "names_root_node_id", None),
            )

        # 1b. v3 KB-Resolution signal: milestone collapse (state authoring
        # only). When the user confirms a runbook fix worked, the LLM emits
        # `knowledge_resolution` alongside `root_cause_conclusion`,
        # `solutions_to_add`, and the gate milestones (`solution_accepted`)
        # — INVESTIGATING's structured state is authored in this one turn.
        # The RESOLVED disposition is NOT collapsed (#722): the user's "it
        # worked" is the solution-verification claim (FM trusts it), not
        # consent to the irreversible terminal transition — that consent
        # comes from the explicit confirm turn of the standard
        # ProposedTransition handshake. `KnowledgeResolution` (including
        # `user_confirmation`) is an attribution/audit record, not consent.
        # See investigation-lifecycle-logic.md §1.2 →
        # "KB-Resolution Path (Milestone-Collapse Variant)".
        if hasattr(updates, "knowledge_resolution") and updates.knowledge_resolution:
            kr = updates.knowledge_resolution
            case.inquiry.knowledge_resolution = KnowledgeResolution(
                match_id=kr.match_id,
                match_type=kr.match_type,
                solution_applied=kr.solution_applied,
                user_confirmation=kr.user_confirmation,
            )
            # A runbook matched against the reported symptom, and the user
            # confirmed its fix worked — the symptom is, by construction, verified.
            # Establish the cause-identification anchor so the milestone
            # collapse's RootCauseConclusion is honored by the M5 / readiness
            # gates (which require a verified symptom for the RCC signal).
            case.progress.symptom_verified = True
            logger.info(
                "Case %s: knowledge_resolution signalled during INVESTIGATING; "
                "match_id=%s, type=%s. Standard ProposedTransition handshake handles disposition.",
                case.case_id,
                kr.match_id,
                kr.match_type,
            )

        # 1. Update Milestones
        # NOTE: solution_verified is excluded — it requires the User-Agent
        # Handshake via ProposedTransition (see terminal_transitions.py).
        if updates.milestones:
            m = updates.milestones
            p = case.progress
            # Only set to True (never revert).
            #
            # STAGE-GATE SIGNALS ARE NOT APPLIED HERE. ``solution_accepted``
            # and the mitigation pair are compliance signals whose guards
            # must see the ProposedActions created by THIS turn's solutions
            # step (the prompt's KB-resolution flow mandates SolutionToAdd +
            # solution_accepted in one response) — they are applied by
            # ``_apply_stage_gate_signals`` AFTER step 5 below.
            milestone_fields = [
                # Progress indicators (LLM context, non-stage-driving)
                "symptom_verified",
                # cause_state — engine-derived from a validated, uncontested
                #   chain root at the recompute (§9.2 / INV-35), never LLM-set;
                #   there is no root_cause_identified self-claim to honor here.
                # solution_proposed — engine-derived from live SOLUTION offers
                #   at the assessment recompute (INV-32), never LLM-set
                # solution_verified — requires User-Agent Handshake
            ]

            for field in milestone_fields:
                if getattr(m, field, False):
                    # Only append if transitioning from False to True
                    if not getattr(p, field, False):
                        setattr(p, field, True)
                        metadata["milestones_completed"].append(field)

            _apply_symptom_retraction(case, m, response_obj, metadata)

            if m.root_cause_likelihood is not None:
                p.root_cause_likelihood = m.root_cause_likelihood
            if getattr(m, "solution_feasible", None) is not None:
                p.solution_feasible = SolutionFeasible(m.solution_feasible)
            _valid_methods = {
                "direct_analysis",
                "hypothesis_validation",
                "single_shot_validation",
                "correlation",
                "user_provided",
                "other",
            }
            if m.root_cause_method:
                if m.root_cause_method in _valid_methods:
                    p.root_cause_method = m.root_cause_method
                else:
                    logger.warning(
                        f"LLM returned invalid root_cause_method '{m.root_cause_method}', "
                        f"mapping to 'other'"
                    )
                    p.root_cause_method = "other"

            # Ensure consistency: if cause_state was just set to IDENTIFIED,
            # root_cause_method and root_cause_likelihood must also be set
            if p.cause_state == CauseState.IDENTIFIED:
                if not p.root_cause_method:
                    p.root_cause_method = m.root_cause_method or "direct_analysis"
                if p.root_cause_likelihood == 0.0:
                    p.root_cause_likelihood = m.root_cause_likelihood or 0.8

            # KB-remediation pre-fetch is triggered on the cause_state→IDENTIFIED
            # edge AFTER the end-of-turn chain recompute (INV-35) — cause_state is
            # engine-derived there, not from any milestone applied in this block.
            # See the prefetch beside _recompute_assessment_state below.

        # 2. Add Evidence
        # Post-010: every Evidence row comes from the LLM declaring an
        # `evidence_to_add` entry on this turn. Files uploaded earlier in
        # the turn live on `uploaded_files` only — they become Evidence
        # only when the LLM extracts a claim-relevant slice and records
        # it here.
        has_attr = hasattr(updates, "evidence_to_add")
        evidence_list = getattr(updates, "evidence_to_add", None) if has_attr else None
        evidence_count = len(evidence_list) if evidence_list else 0
        logger.info(
            f"Evidence creation check: "
            f"hasattr(updates, 'evidence_to_add')={has_attr}, "
            f"evidence_to_add={evidence_list}, "
            f"count={evidence_count}"
        )

        if hasattr(updates, "evidence_to_add") and updates.evidence_to_add:
            # Post-010: source_file_id is declared by the LLM directly on
            # EvidenceToAdd. The Pydantic ``_source_file_required_unless_user_description``
            # validator on EvidenceToAdd has already enforced the
            # ``evidence_source_invariant``: by the time we get here,
            # ``ev_item.source_file_id is None`` implies
            # ``source_type == USER_DESCRIPTION``. We pass the value
            # through unchanged — no turn-file fallback, because that
            # would silently mis-attribute a chat-extracted USER_DESCRIPTION
            # quote to whatever file happens to be in the same turn.
            #
            # Redesign R5/§2: the former path-conditional causal_evidence ban
            # is removed. Whether RCA-side work runs is decided by the prompt
            # (gated on cause uncertainty), not by an engine emission ban —
            # causal_evidence is always allowed during INVESTIGATING.
            for ev_item in updates.evidence_to_add:
                # Infer milestone attribution (Tier 2 + Tier 3)
                milestones_completed_this_turn = metadata.get(
                    "milestones_completed", []
                )
                if ev_item.advances_milestones is not None:
                    advances_milestones = ev_item.advances_milestones
                else:
                    advances_milestones = _infer_milestones(
                        ev_item.category, milestones_completed_this_turn
                    )

                # Guard a hallucinated/stale source_file_id (FK to uploaded_files)
                # before it aborts the turn at save.
                source_file_id, source_type = _resolve_evidence_source(
                    case, ev_item.source_file_id, ev_item.source_type
                )

                coverage_start, coverage_end, coverage_source = _evidence_coverage(
                    case, source_file_id, ev_item.extract
                )
                ev = Evidence(
                    evidence_id=f"ev_{uuid4().hex[:12]}",
                    summary=ev_item.summary,
                    extract=ev_item.extract,
                    category=ev_item.category,
                    source_type=source_type,
                    source_file_id=source_file_id,
                    collected_at=datetime.now(UTC),
                    collected_by=case.user_id,
                    collected_at_turn=case.current_turn,
                    advances_milestones=advances_milestones,
                    primary_purpose="Investigation context",
                    coverage_start_ts=coverage_start,
                    coverage_end_ts=coverage_end,
                    coverage_source=coverage_source,
                )
                # #1136: does this row carry a datum the case did not already
                # hold? Computed BEFORE the append, or the row would match
                # itself. ``evidence_added`` keeps every minted id (positional
                # ``new_index_N`` refs, milestone attribution and coverage all
                # resolve against it); only the progress signal narrows.
                restates = _restates_standing_evidence(ev_item, case)
                case.evidence.append(ev)
                metadata["evidence_added"].append(ev.evidence_id)
                if not restates:
                    metadata.setdefault("novel_evidence_added", []).append(
                        ev.evidence_id
                    )
                logger.info(
                    f"Created evidence: {ev.evidence_id} | "
                    f"category={ev.category.value}, source_type={ev.source_type.value}, "
                    f"source_file_id={ev.source_file_id}, "
                    f"summary='{ev.summary[:80]}...'"
                )

        # 2b. Validate Milestone Claims Against Cited Evidence
        # Milestones are applied optimistically from LLM output (step 1 above),
        # then validated here. Invalid claims are REVERTED to prevent milestones
        # advancing without supporting evidence.
        # Only names the LLM can CLAIM are reviewable. ``root_cause_identified``
        # is engine-derived (INV-35) and is appended by the recompute later in
        # this method (#1284), so today it is not present here anyway. Filtering
        # explicitly rather than relying on that ordering: the review expects
        # ">=2 CAUSAL_EVIDENCE rows" for that name, which plenty of genuine
        # identification turns lack, so if the recompute were ever hoisted above
        # this step the engine's own derivation would be silently deleted from
        # the turn and the transparency light would go back on with nothing
        # failing. The exclusion makes that reordering harmless instead.
        reviewable = llm_claimable_milestones(metadata["milestones_completed"])
        if reviewable:
            from faultmaven.core.investigation.evidence_processor import (
                validate_milestone_claims,
            )

            reasoning = getattr(response_obj, "internal_reasoning", None)
            validation_results = validate_milestone_claims(case, reviewable, reasoning)
            for result in validation_results:
                if not result.is_valid:
                    # Revert the milestone — evidence doesn't support the claim.
                    if hasattr(case.progress, result.milestone):
                        setattr(case.progress, result.milestone, False)
                    if result.milestone in metadata["milestones_completed"]:
                        metadata["milestones_completed"].remove(result.milestone)
                    logger.warning(
                        f"Milestone '{result.milestone}' REVERTED: claimed with insufficient evidence "
                        f"({result.cited_count}/{result.expected_min} required). "
                        f"Warnings: {result.warnings}"
                    )
                    metadata.setdefault("milestone_validation_warnings", []).extend(
                        result.warnings
                    )

        # 3. Add/Update Hypotheses
        #
        # Redesign R5/§2: the former path-conditional hypothesis ban is
        # removed. Hypothesis formation is always allowed during
        # INVESTIGATING; the prompt (gated on cause uncertainty) decides when
        # the diagnostic machinery runs, not an engine emission ban.
        #
        # Cause hypotheses are anchored on a VERIFIED symptom. ``symptom_verified``
        # is already applied (step 1) and reverted if unsupported (step 2b) by now,
        # so it holds this turn's final value.
        #  - Anchored (symptom_verified): first FLUSH any hypotheses queued
        #    (CAPTURED) on an earlier unverified turn → ACTIVE — applied
        #    automatically, with no LLM re-emission. Then add this turn's
        #    hypotheses as ACTIVE.
        #  - Unanchored: QUEUE this turn's hypotheses as CAPTURED — never drop them
        #    (data of any order is retained), but hold them out of the ACTIVE
        #    differential (CAPTURED is excluded from count_active / chain grounding
        #    / UI) until the anchor lands. This gates activation of cause hypotheses
        #    only — not runbook retrieval / early triage before verification.
        anchored = case.progress.symptom_verified
        # ``hyp_emit_order`` is the positional list ``new_index_N`` refs resolve
        # against (INV-36). It mirrors ``hypotheses_generated`` per item — SAME
        # base, including the promoted-CAPTURED prefix that was already the
        # pre-INV-36 resolution base (the LLM's ``new_index_N`` offset by
        # ``len(promoted)`` is a pre-existing behavior, preserved verbatim here,
        # not introduced) — EXCEPT a dedup skip records the CANONICAL existing id
        # instead of a new one, so downstream refs (evidence links, updates, need
        # motivators) that target a skipped duplicate resolve to the kept
        # hypothesis rather than shifting onto the wrong sibling.
        # ``hypotheses_generated`` stays truly-new so telemetry / turn-outcome
        # progress do not count a dedup as generation (a skip is not diagnostic
        # progress — the DF-6 exhaustion signal).
        emit_order: list[str] = metadata.setdefault("hyp_emit_order", [])
        if anchored:
            promoted = HypothesisManager.activate_queued_hypotheses(case)
            if promoted:
                metadata["hypotheses_generated"].extend(promoted)
                emit_order.extend(promoted)
                logger.info(
                    "Promoted %d queued (CAPTURED) hypotheses to ACTIVE on "
                    "symptom verification",
                    len(promoted),
                )
        new_hyp_state = HypothesisState.ACTIVE if anchored else HypothesisState.CAPTURED
        if hasattr(updates, "hypotheses_to_add") and updates.hypotheses_to_add:
            for h_item in updates.hypotheses_to_add:
                # INV-36: a statement that duplicates a standing (non-terminal)
                # hypothesis is not minted a second time — duplicates spuriously
                # re-satisfy the ≥2-active work gate, corrupting the axis that
                # separates INSUFFICIENT_EVIDENCE from NOT_YET_PRODUCTIVE.
                # Terminal (refuted/retired) causes are NOT dedup targets, so a
                # revival re-enters the differential. Same-batch duplicates are
                # caught for free: a sibling minted earlier this turn is already
                # in ``case.hypotheses`` by the time the next item is checked.
                dup_id = find_duplicate_hypothesis(h_item.statement, case)
                if dup_id is not None:
                    emit_order.append(dup_id)
                    hypothesis_dedup_skipped_total.inc()
                    existing = case.hypotheses.get(dup_id)
                    _add_system_feedback(
                        metadata,
                        f"Hypothesis '{h_item.statement[:80]}' duplicates "
                        f"standing hypothesis {dup_id}"
                        + (
                            f" ('{existing.statement[:80]}')"
                            if existing is not None
                            else ""
                        )
                        + " and was not re-added. To revise it, update the "
                        "existing hypothesis (hypotheses_to_update) with new "
                        "evidence rather than restating it.",
                    )
                    logger.info(
                        "Deduped hypothesis (INV-36): '%s' matches standing %s",
                        h_item.statement[:60],
                        dup_id,
                    )
                    # A chain the LLM emitted for the duplicate is left to the
                    # orphan-chain post-pass (``resolve_orphan_chains``), which
                    # re-attaches it to a FLAT standing hypothesis under its own
                    # anti-clobber guard (``_hypothesis_lacks_real_chain``).
                    # Re-rooting the canonical here would BYPASS that guard and
                    # could GC a validated hypothesis's existing chain.
                    continue
                h = self.hypothesis_manager.create_hypothesis(
                    statement=h_item.statement,
                    category=h_item.category,
                    initial_likelihood=h_item.likelihood,
                    current_turn=case.current_turn,
                    state=new_hyp_state,
                )
                case.hypotheses[h.hypothesis_id] = h
                metadata["hypotheses_generated"].append(h.hypothesis_id)
                emit_order.append(h.hypothesis_id)
                # Record this hypothesis's chain-root ref keyed by its id, so
                # chain linking (when enabled) needs no positional zip against
                # the spec list — robust to any future skip/dedup here.
                if getattr(h_item, "root_node_ref", None):
                    metadata.setdefault("hyp_root_refs", {})[
                        h.hypothesis_id
                    ] = h_item.root_node_ref

        # 3b. Apply the LLM's hypothesis disconfirmation signal (state=REFUTED +
        # reason) and likelihood updates. Emitted by schema+prompt but never
        # applied before; connecting it lets M6 demotion fire on the LLM's own
        # refutation, not only on REFUTES evidence links. (Other state
        # transitions are intentionally deferred — see _apply_hypothesis_updates.)
        if getattr(updates, "hypotheses_to_update", None):
            self._apply_hypothesis_updates(
                case,
                updates.hypotheses_to_update,
                metadata,
                case.current_turn,
            )

        # 4. Link Evidence (Partial Application Check)
        # Note: Hypothesis-evidence linking is best-effort. The LLM may reference
        # evidence IDs that don't exist yet (timing issue), so we silently skip failed links.
        if (
            hasattr(updates, "hypothesis_evidence_links")
            and updates.hypothesis_evidence_links
        ):
            self._apply_hypothesis_evidence_links(
                case, updates.hypothesis_evidence_links, metadata
            )

        # (Deferred likelihood updates are applied AFTER chain emission —
        # see the call beside _apply_chain_emission below: the B1 cap must
        # judge the hypothesis WITH the links this same turn carried on BOTH
        # axes, flat hypothesis_evidence_links AND chain node_evidence_links.)

        # 4b. Evidence Needs (Phase 3 of evidence-needs rollout)
        # Process LLM-emitted ``evidence_need_updates``. Runs AFTER
        # evidence_to_add (so ``metadata["evidence_added"]`` is populated
        # for ``new_index_N`` resolution on ``fulfilling_evidence_ids``)
        # and AFTER hypotheses_to_add (so ``metadata["hypotheses_generated"]``
        # is populated for ``new_index_N`` resolution on
        # ``motivating_hypothesis_ids``). Symptom-purpose needs are
        # always allowed; causal-purpose needs are rejected by the
        # path-conditional emission backstop (parallels the
        # causal_evidence rejection at lines ~5758+).
        if hasattr(updates, "evidence_need_updates") and updates.evidence_need_updates:
            self._apply_evidence_need_updates(
                case=case,
                updates_list=updates.evidence_need_updates,
                metadata=metadata,
                current_turn=case.current_turn,
            )

        # 5. Solutions
        #
        # Redesign R5/§2: the former pre-path solutions ban is removed. There
        # is no path commit gate; solution/workaround proposals are allowed
        # opportunistically during INVESTIGATING.
        if hasattr(updates, "solutions_to_add") and updates.solutions_to_add:
            for s_item in updates.solutions_to_add:
                # R9: causal-graph linkage carried by the emission (optional;
                # honor-or-reject). ``quadrant`` is recorded as DATA — the M5
                # downgrade below is unchanged. ``node_ref`` is kept only when it
                # resolves to a real node on this case's graph. Note: no forward-
                # looking "verification" is written to ``verification_method`` here
                # — that field means *how the fix WAS verified* (past tense, read by
                # the resolution report + resolution-confirmation gate), so writing
                # a proposed check into it would claim a verification that never
                # happened. The runbook's verification prose reaches the LLM via RAG.
                node_ref = getattr(s_item, "node_ref", None)
                node_id = node_ref if node_ref in case.causal_nodes else None
                sol = Solution(
                    solution_id=f"sol_{uuid4().hex[:12]}",
                    solution_type=s_item.solution_type,
                    title=f"Solution: {s_item.solution_type}",
                    immediate_action=s_item.description,
                    commands=s_item.commands or [],
                    risks=[s_item.risks] if s_item.risks else [],
                    node_id=node_id,
                    quadrant=_coerce_intervention_quadrant(
                        getattr(s_item, "quadrant", None)
                    ),
                    proposed_at=datetime.now(UTC),
                )
                # #1136: as for evidence above — computed BEFORE the append so
                # the row cannot match itself. ``solutions_proposed`` keeps every
                # minted id; only the progress signal narrows to NEW offers.
                restates = _restates_standing_solution(s_item, case)
                case.solutions.append(sol)
                metadata["solutions_proposed"].append(sol.solution_id)
                if not restates:
                    metadata.setdefault("novel_solutions_proposed", []).append(
                        sol.solution_id
                    )

                # Gap 0: Create ProposedAction for compliance detection chain
                action_type = _determine_action_type(case, s_item.solution_type)
                downgrade_reason: str | None = None

                # 3C / M5: Solution-validation gate — a SOLUTION (permanent fix)
                # requires the cause to be mechanistically validated, i.e.
                # cause_state == IDENTIFIED (some chain's root validated by
                # evidence — methodology M5 / §9.2). Proposing a permanent
                # remediation before the root is validated is the premature-fix /
                # diagnostic-test-recorded-as-a-solution failure M5 forbids.
                # Downgrade to DIAGNOSTIC and tell the LLM how to recover. This
                # subsumes the prior weaker "≥1 hypothesis" check (IDENTIFIED
                # implies hypotheses). Mitigation (WORKAROUND) is exempt by
                # design — it precedes a known root and is gated on symptom
                # evidence by 3D instead. Graceful denial (no stall): the flow
                # continues as DIAGNOSTIC; the LLM grounds the root and
                # re-proposes, or proposes a mitigation.
                if (
                    action_type == InvestigationActionType.SOLUTION
                    and not _solution_cause_validated(
                        case,
                        working_conclusion=_settled_working_conclusion(case, metadata),
                    )
                ):
                    logger.warning(
                        f"Downgrading SOLUTION to DIAGNOSTIC for case {case.case_id}: "
                        f"cause_state={case.progress.cause_state.value}, "
                        f"rcc={'set' if case.root_cause_conclusion else 'none'} "
                        f"(M5 — a permanent fix requires an established root cause)"
                    )
                    action_type = InvestigationActionType.DIAGNOSTIC
                    # Rendered on every turn this action stays pending, so it
                    # states what was true WHEN it was proposed, never the
                    # current state, and gives a recovery that works either
                    # way: an RCC licenses the fix once the symptom is verified
                    # and no rival cause is contested, whatever the other legs
                    # say (fm#1679). ``ProposedAction.downgrade_reason`` holds
                    # at most 500 characters.
                    downgrade_reason = (
                        "Downgraded from SOLUTION: when proposed, no root cause "
                        "was established; a permanent fix needs one, and a "
                        "diagnostic test is not a fix (M5). To register a real "
                        "fix, send in ONE response a root_cause_conclusion "
                        "backed by evidence and the fix as a SolutionToAdd; the "
                        "conclusion counts once the symptom is verified and no "
                        "rival cause is contested. If the user already applied "
                        "that fix, also set solution_accepted from their report "
                        "rather than asking again. Or propose a WORKAROUND "
                        "mitigation."
                    )

                # 3D: Symptom-evidence gate — MITIGATION requires at least one
                # SYMPTOM_EVIDENCE row on the case. The mitigation must target
                # an observed failure, not an unverified user claim. If no
                # SYMPTOM_EVIDENCE exists, downgrade to DIAGNOSTIC so the
                # mitigation milestone cannot fire on an ungrounded proposal.
                # The LLM receives the downgrade_reason in next-turn context
                # and can recover by gathering symptom data and re-proposing.
                # See Behavioral Rule 2 (Evidence-Grounded) and
                # investigation-lifecycle-logic.md §2.3 (minimum-evidence
                # discipline).
                #
                # Scope of this gate (what it does NOT do): the action's
                # ``description`` and ``commands`` are preserved verbatim
                # below — only ``action_type`` is rewritten. The user sees
                # the original proposal in the chat and may execute it.
                # The gate prevents the engine from REGISTERING the
                # mitigation (firing ``mitigation_accepted`` on the user's
                # subsequent compliance), not from the mitigation HAPPENING
                # in the user's environment. If the user runs the action
                # anyway, the LLM next turn sees both the downgrade_reason
                # and the user's report; the recovery is to file
                # retrospective SYMPTOM_EVIDENCE (from pre-mitigation logs
                # or the user's account of what changed) and then re-propose.
                # Forward-only semantics: an ungrounded mitigation that
                # quietly executes does not register; the case stays in
                # DIAGNOSIS until grounding catches up — which is the
                # correct outcome under "valid results when possible, no
                # false progress otherwise."
                if (
                    action_type == InvestigationActionType.MITIGATION
                    and not _case_has_symptom_evidence(case)
                ):
                    logger.warning(
                        f"Downgrading MITIGATION to DIAGNOSTIC for case {case.case_id}: "
                        f"no SYMPTOM_EVIDENCE exists yet"
                    )
                    action_type = InvestigationActionType.DIAGNOSTIC
                    downgrade_reason = (
                        "Your previous MITIGATION proposal was downgraded to "
                        "DIAGNOSTIC because no SYMPTOM_EVIDENCE existed on "
                        "the case. A mitigation must target an observed "
                        "failure, not an unverified user claim. Inspect the "
                        "case data (pod logs / status / metrics / config "
                        "snapshot), file SYMPTOM_EVIDENCE for what you find, "
                        "then re-propose the mitigation grounded in that "
                        "evidence."
                    )

                # INV-32 (#656 DF-3): a NEW permanent-fix offer replaces any
                # standing pending one — the newest proposal is THE offer
                # (the context builder and compliance detection already key
                # on the most recent pending action; without supersession the
                # stale siblings linger pending forever and keep the derived
                # solution_proposed latched). Runs BEFORE the append so the
                # new offer never supersedes itself.
                if action_type == InvestigationActionType.SOLUTION:
                    _supersede_pending_solution_offers(case, reason="reproposal")

                proposed_action = ProposedAction(
                    case_id=case.case_id,
                    action_type=action_type,
                    description=s_item.description,
                    commands=s_item.commands or [],
                    proposed_in_turn=case.current_turn,
                    downgrade_reason=downgrade_reason,
                )
                case.proposed_actions.append(proposed_action)

                # solution_proposed is DERIVED at the end-of-turn assessment
                # recompute from live SOLUTION offers (INV-32) — the former
                # 3F write-once set here is gone; this new pending offer
                # flips the indicator True in the same turn via the
                # derivation.

        # 5b. Stage-gate compliance signals (Framework §4.1) — AFTER the
        # solutions step so the guards see actions created this turn (the
        # prompt's KB-resolution flow emits SolutionToAdd + solution_accepted
        # in ONE response; see _apply_stage_gate_signals).
        if updates.milestones:
            _apply_stage_gate_signals(case, updates.milestones, user_message, metadata)

        # 6. Journal Entries (append-only investigation memory)
        if hasattr(updates, "journal_entries") and updates.journal_entries:
            for je_item in updates.journal_entries:
                entry = JournalEntry(
                    turn=case.current_turn,
                    entry_type=je_item.entry_type,
                    content=je_item.content[:200],
                    evidence_id=je_item.evidence_id,
                    hypothesis_id=je_item.hypothesis_id,
                )
                case.investigation_journal.append(entry)
            logger.info(
                f"Case {case.case_id}: added {len(updates.journal_entries)} journal entries "
                f"(total: {len(case.investigation_journal)})"
            )

        # Populate the causal graph from the LLM's emitted chain (lazy backward
        # expansion), then resolve any chain the LLM left unlinked. The graph is
        # always populated from the emitted chain; cause_state/M6 derive from the
        # real emitted chains. (cause_state derivation never reads the graph for
        # truth — see _recompute_assessment_state.)
        self._apply_chain_emission(case, updates, metadata)
        # Orphan-chain resolution (B2c invariant: every chain explaining D is
        # attached to exactly one hypothesis). T1 re-attaches an unambiguous
        # double-representation in place; any ambiguous orphan is surfaced to
        # the LLM as a one-turn nudge (T2a) to re-root it or declare it
        # separate, rather than guessing.
        self._nudge_ambiguous_orphan_chains(case, metadata)

        # Deferred likelihood updates — applied AFTER both link passes (flat
        # step 4 AND chain emission above), so the B1 evidence-free cap judges
        # the hypothesis with everything this turn's emission grounded it on;
        # a chain-contract turn (record -> node-link -> set likelihood) must
        # not be capped and gaslit for links it did emit.
        self._apply_deferred_likelihood_updates(case, metadata, case.current_turn)

        # Recompute engine-owned assessment vars (cause_state / solution_state)
        # now that this turn's hypotheses and solutions are applied (redesign R1).
        # Pass this turn's LLM-certified deductive survivors (resolved in
        # _apply_chain_emission) so proof-by-exclusion can stamp them post-derive.
        # Provider identity for the DF-6 provider-floor metric (INV-39), passed
        # explicitly (not smuggled through the shared metadata dict). Resolved via
        # the helper because self.llm_provider is the LLMRouter in the real
        # deployment (no provider_name) — the helper reads the configured chat
        # provider off it; a partially constructed engine (some fixtures omit
        # llm_provider) degrades to "unknown" rather than raising.
        prior_cause_state = _recompute_assessment_state(
            case,
            exclusion_survivors=metadata.get("deductive_survivor_ids", frozenset()),
            rcc_authored_this_turn=metadata.get("rcc_authored_this_turn", False),
            metadata=metadata,
            provider_name=_resolve_chat_provider_name(
                getattr(self, "llm_provider", None)
            ),
        )

        # KB-remediation pre-fetch on the cause_state→IDENTIFIED edge (INV-35):
        # cause_state is engine-derived above, so this warm-up fires the turn it
        # newly crosses to IDENTIFIED (in-flight diagnosis — terminal recompute
        # paths deliberately do not warm KB, the fix has already happened).
        _kb_query = _kb_prefetch_query_on_identification(
            prior_cause_state,
            case.progress.cause_state,
            case.root_cause_conclusion,
            case.working_conclusion,
        )
        if _kb_query:
            await self._prefetch_kb_context(case, _kb_query, "root_cause")

        # Deferred-implementation disposition: if the fix is known but can't be
        # applied this session, propose CLOSE-with-documented-solution (§3.1 row 3).
        _maybe_propose_deferred_close(case, metadata)

        # Bug #4: Evidence-Milestone Linking (Moved here to ensure evidence exists)
        # LLM-claimed milestones only. This runs AFTER the assessment recompute,
        # so the turn's list may also carry the engine's own
        # ``root_cause_identified`` (#1284) — and the attribution is blanket, so
        # it would stamp that onto every evidence row added this turn regardless
        # of category, including SYMPTOM and DOCUMENT rows. That is exactly the
        # attribution INV-35 removed from CATEGORY_MILESTONE_MAP: identification
        # is earned by the causal chain, not by whatever arrived the same turn.
        attributable = llm_claimable_milestones(metadata["milestones_completed"])
        if attributable and metadata["evidence_added"]:
            for ev_id in metadata["evidence_added"]:
                ev = next((e for e in case.evidence if e.evidence_id == ev_id), None)
                if ev:
                    ev.advances_milestones.extend(attributable)

        # Bug #8: Robust Turn Outcome Determination
        metadata["outcome"] = self._determine_turn_outcome(
            case, metadata, updates.outcome
        )

    def _apply_hypothesis_evidence_links(
        self,
        case: Case,
        links: list,
        metadata: dict[str, Any],
    ) -> None:
        """Apply LLM-emitted ``hypothesis_evidence_links`` to the case.

        Linking is best-effort: the LLM may reference hypothesis or
        evidence IDs that don't resolve (timing issue), so failed links
        are logged and skipped. A link on a **terminal** hypothesis
        (``REFUTED``/``RETIRED``) is refused and surfaced via
        ``system_feedback``, mirroring ``_apply_hypothesis_updates``: the
        prompt renders refuted hypotheses WITH their ids (so the model does
        not re-create them), and without this guard one SUPPORTS link lifted a
        refuted hypothesis from 0.0 to 0.35 and reset its progress counter
        (#1116 review). The emitted stance is carried through
        verbatim — NEUTRAL links attach without any likelihood effect
        (#514) — EXCEPT on ``causal_absence`` rows, which carry no
        model-authored stance at all (the M2 trust boundary, #987; see
        ``cause_assurance.absence_row_link_refused``).

        This is the FLAT belief axis. It shares that boundary verbatim with the
        chain axis in ``causal_graph.ingest_emitted_chain`` because the
        invariant belongs to the evidence ROW, not to the link target: a
        REFUTES on a success-confirmation absence row reaches
        ``_net_refuted`` → ``_hypothesis_disconfirmed`` → M6 from here just as
        it reached ``derive_node_states`` from there, so guarding one axis only
        would leave the #987 cascade one stance choice away.
        """
        for link in links:
            # Resolve partial IDs like 'new_index_0' to actual IDs if we just created them
            h_id = self._resolve_id_ref(
                link.hypothesis_id_ref,
                metadata.get("hyp_emit_order")
                or metadata.get("hypotheses_generated", []),
                "hyp",
            )
            e_id = self._resolve_id_ref(
                link.evidence_id_ref, metadata.get("evidence_added", []), "ev"
            )

            # Check existence
            if h_id not in case.hypotheses:
                # Hypothesis ID validation failed - log warning but don't add to system_feedback
                logger.warning(
                    f"Hypothesis-evidence link skipped: Hypothesis ID '{h_id}' not found "
                    f"(resolved from '{link.hypothesis_id_ref}'). "
                    f"Available hypotheses: {list(case.hypotheses.keys())}, "
                    f"Hypotheses added this turn: {metadata.get('hypotheses_generated', [])}"
                )
                continue

            # Terminal states are immutable on every write path (see docstring).
            hypothesis = case.hypotheses[h_id]
            if hypothesis.state.is_terminal:
                logger.warning(
                    f"Hypothesis-evidence link refused: hypothesis '{h_id}' is "
                    f"{hypothesis.state.value} (terminal)."
                )
                _add_system_feedback(
                    metadata,
                    f"Hypothesis {h_id} is {hypothesis.state.value} (terminal) "
                    f"— evidence cannot be linked to it. Open a NEW hypothesis "
                    f"if that theory is back in play.",
                )
                continue

            # Check evidence existence (scan list)
            ev_row = next((e for e in case.evidence if e.evidence_id == e_id), None)
            ev_exists = ev_row is not None
            if not ev_exists:
                # Evidence reference failed to resolve
                # This is only a problem if LLM tried to link evidence but used wrong format/ID
                # It's acceptable if no evidence exists (e.g., user_text message)

                # Build diagnostic info
                evidence_this_turn = metadata.get("evidence_added", [])
                all_evidence_ids = [e.evidence_id for e in case.evidence]

                logger.warning(
                    f"Hypothesis-evidence link validation failed: "
                    f"Cannot resolve reference '{link.evidence_id_ref}' to evidence ID '{e_id}'. "
                    f"Evidence created this turn: {evidence_this_turn}. "
                    f"Recent evidence IDs: {all_evidence_ids[-5:] if len(all_evidence_ids) > 5 else all_evidence_ids}. "
                    f"Note: This is expected if no evidence was created (user_text messages)."
                )
                continue

            # M2 trust boundary (#987), category-gated — the SAME predicate the
            # chain axis applies, so the two entry points cannot drift.
            if absence_row_link_refused(
                getattr(ev_row, "category", None),
                link.stance,
                axis="hypothesis",
                evidence_id=e_id,
                node_or_hypothesis_id=h_id,
                case_id=case.case_id,
                turn=case.current_turn,
            ):
                continue

            # A confidence the schema SET ASIDE as out of range (fm#1502) is
            # decided here, where "re-emitted" is knowable: storage is an upsert
            # by evidence_id, and only the stored link says whether this one
            # re-states the same claim.
            stored = next(
                (el for el in hypothesis.evidence_links if el.evidence_id == e_id),
                None,
            )
            stance_confidence = self._resolve_link_confidence(
                link,
                stored_stance=stored.stance if stored is not None else None,
                where=f"{h_id}<-{e_id}",
                metadata=metadata,
            )
            if stance_confidence is _PRUNE_LINK:
                continue

            # Counts only a NEW or materially revised link (#1136). Storage is an
            # upsert by evidence_id, so counting every call let a model re-emitting
            # the same link each turn hold ``turns_without_progress`` at 0 forever —
            # the same restatement leak the ``novel_*`` keys close on the other
            # arms. ``link_evidence`` decides, because only it holds both the prior
            # link and the new one.
            if self.hypothesis_manager.link_evidence(
                case.hypotheses[h_id],
                e_id,
                link.stance,
                case.current_turn,
                reasoning=link.reasoning,
                stance_confidence=stance_confidence,
            ):
                metadata["hypothesis_evidence_links_applied"] = (
                    metadata.get("hypothesis_evidence_links_applied", 0) + 1
                )

    @staticmethod
    def _resolve_link_confidence(
        link: Any,
        *,
        stored_stance: Any,
        where: str,
        metadata: dict[str, Any],
    ) -> Any:
        """The ``stance_confidence`` to store for a hypothesis link.

        Returns a number, ``None`` (keep the stored value — ``link_evidence``
        applies it), or ``_PRUNE_LINK`` when the link must not be written.

        Only a value the schema SET ASIDE as out of range is decided here
        (fm#1502): a re-emission of the same claim (same evidence, same stance)
        keeps the stored value; a new link, or a stance flip, is rescaled or
        coerced when it can be and otherwise pruned — leaving any stored link
        as it was. The schema's ``1.0`` default must not stand in, and nor may
        the stored confidence of the opposite stance: on REFUTES the first is a
        decisive disconfirmation nobody asserted, and on SUPPORTS the second is
        grounding nobody asserted.

        An OMITTED (or strict-mode ``null``) confidence follows the same
        new-versus-re-emitted rule, per the 2026-09-24 ruling: on a re-emission
        of the same claim it keeps the stored value — the schema's ``1.0``
        default used to overwrite a stored hedge whenever a routine re-listing
        left the field out, "the exact defect the node path documents
        avoiding" — and on a new link or a stance flip it is full confidence,
        the default, as before. A conforming value is the link's own, as before.

        Duck-typed like the rest of this apply path: a link that is not a
        Pydantic model has no fields-set record, so its ``stance_confidence``
        is read as given.
        """
        settled = settle_set_aside_link(
            link,
            stored_stance=stored_stance,
            where=where,
            notes=metadata.setdefault("validation_repairs", []),
        )
        if settled is None:
            fields_set = getattr(link, "model_fields_set", None)
            omitted = isinstance(fields_set, (set, frozenset)) and (
                "stance_confidence" not in fields_set
            )
            if omitted and stored_stance is not None and stored_stance == link.stance:
                return None  # a true re-emission: the stored value stands
            return link.stance_confidence
        action, value = settled
        if action is ConfidenceAction.PRUNED:
            return _PRUNE_LINK
        return value  # None when dropped: the stored value stands

    # =========================================================================
    # Evidence Need apply-layer (Phase 3 of evidence-needs rollout)
    # =========================================================================

    def _apply_evidence_need_updates(
        self,
        case: Case,
        updates_list: list,
        metadata: dict[str, Any],
        current_turn: int,
    ) -> None:
        """Apply LLM-emitted ``evidence_need_updates`` to the case.

        Each ``EvidenceNeedUpdate`` either creates a new ``EvidenceNeed``
        (when ``need_id`` is None) or updates an existing one. Cross-
        emission ``new_index_N`` references are resolved against
        metadata-stored ID lists populated earlier in this same
        ``_apply_investigation_updates`` invocation:

        - ``motivating_hypothesis_ids`` → ``metadata["hypotheses_generated"]``
        - ``fulfilling_evidence_ids`` → ``metadata["evidence_added"]``
        - ``need_id`` → in-loop list of need IDs created earlier in
          this same ``updates_list``

        Redesign R5/§2: the former path-conditional causal-purpose ban is
        removed — causal-verification needs are allowed opportunistically
        during INVESTIGATING; the prompt (gated on cause uncertainty) decides
        when causal work runs, not an engine emission ban.

        See ``docs/architecture/investigation-engine/evidence-needs-design.md``
        §5.3 (out-of-order arrival).
        """
        # Ensure the metadata key exists before any append. The dict built
        # in ``_process_response_structured`` (the one threaded here via
        # ``_apply_investigation_updates``) does not seed
        # ``evidence_needs_updated``, unlike the parallel dict in
        # ``_process_turn_impl``. Without this, the first need created or
        # updated this turn raised ``KeyError`` and 500'd the whole turn.
        # The Phase-6 flatten seam already reads this key defensively
        # (``metadata.get("evidence_needs_updated", [])``).
        metadata.setdefault("evidence_needs_updated", [])
        # Same-turn need_id resolution: needs created earlier in this
        # same ``updates_list`` are tracked here so a later update with
        # ``need_id="new_index_0"`` can find them.
        needs_created_in_this_loop: list[str] = []

        for update in updates_list:
            # Resolve new_index_N references (same pattern as
            # hypothesis_evidence_links at line ~5927 / 5931).
            resolved_motivators = [
                self._resolve_id_ref(
                    hyp_ref,
                    metadata.get("hyp_emit_order")
                    or metadata.get("hypotheses_generated", []),
                    "hyp",
                )
                for hyp_ref in (update.motivating_hypothesis_ids or [])
            ]
            resolved_fulfillments = [
                self._resolve_id_ref(ev_ref, metadata.get("evidence_added", []), "ev")
                for ev_ref in (update.fulfilling_evidence_ids or [])
            ]
            resolved_need_id: str | None = None
            if update.need_id is not None:
                resolved_need_id = self._resolve_id_ref(
                    update.need_id, needs_created_in_this_loop, "eneed"
                )

            # Reference validation: dangling hypothesis IDs are dropped
            # (the link couldn't form anyway), and already-TERMINAL IDs
            # (REFUTED / RETIRED) are also dropped — a hypothesis already out
            # of the differential motivates nothing, so admitting it would
            # create a need the end-of-turn sweep immediately supersedes.
            # Rejecting it at the boundary keeps the churn (and the misleading
            # ask, for the turn it would live) out of the case entirely.
            # Dangling evidence IDs are dropped likewise.
            # These look like prompt-compliance issues, not lifecycle
            # errors, so they go to validation_repairs not system_feedback.
            dangling_hyp_ids = {
                h_id for h_id in resolved_motivators if h_id not in case.hypotheses
            }
            terminal_hyp_ids = {
                h_id
                for h_id in resolved_motivators
                if h_id in case.hypotheses
                and case.hypotheses[h_id].state in TERMINAL_HYPOTHESIS_STATES
            }
            valid_motivators = [
                h_id
                for h_id in resolved_motivators
                if h_id not in dangling_hyp_ids and h_id not in terminal_hyp_ids
            ]
            if dangling_hyp_ids:
                logger.warning(
                    f"Dropped {len(dangling_hyp_ids)} dangling hypothesis "
                    f"ID(s) on evidence_need_update for case {case.case_id}: "
                    f"{dangling_hyp_ids}"
                )
                metadata.setdefault("validation_repairs", []).append(
                    f"Dropped {len(dangling_hyp_ids)} dangling hypothesis "
                    f"ID(s) on evidence_need_update"
                )
            if terminal_hyp_ids:
                logger.warning(
                    f"Dropped {len(terminal_hyp_ids)} terminal hypothesis "
                    f"ID(s) on evidence_need_update for case {case.case_id}: "
                    f"{terminal_hyp_ids}"
                )
                metadata.setdefault("validation_repairs", []).append(
                    f"Dropped {len(terminal_hyp_ids)} terminal hypothesis "
                    f"ID(s) on evidence_need_update"
                )

            valid_ev_ids = {ev.evidence_id for ev in case.evidence}
            valid_fulfillments = [
                e_id for e_id in resolved_fulfillments if e_id in valid_ev_ids
            ]
            if len(valid_fulfillments) != len(resolved_fulfillments):
                dropped = set(resolved_fulfillments) - set(valid_fulfillments)
                logger.warning(
                    f"Dropped {len(dropped)} dangling evidence ID(s) on "
                    f"evidence_need_update for case {case.case_id}: {dropped}"
                )
                metadata.setdefault("validation_repairs", []).append(
                    f"Dropped {len(dropped)} dangling evidence ID(s) "
                    f"on evidence_need_update"
                )

            # CREATE path (need_id is None)
            if resolved_need_id is None:
                # Reject causal-purpose creates with no valid motivator.
                # A causal need without any motivating hypothesis is the
                # exact orphan state §7.4's supersession rule was
                # designed to clean up — but the sweep keys off a
                # terminal hypothesis id, and a need born with no
                # motivator at all has none to key on, so it would
                # never be auto-cleaned. Per design §5.2,
                # causal needs are *motivated by hypotheses*; absent
                # motivators (whether the LLM omitted them or all
                # references filtered away as dangling/retired) makes
                # the emission malformed. Symptom needs are unaffected
                # — empty motivator list is their normal shape, they're
                # motivated by the problem statement.
                if (
                    update.purpose == NeedPurpose.CAUSAL_VERIFICATION
                    and not valid_motivators
                ):
                    logger.warning(
                        f"Rejected causal-purpose evidence_need create on "
                        f"case {case.case_id}: no valid motivating "
                        f"hypothesis (omitted, or all references were "
                        f"dangling/retired). "
                        f"request_text={update.request_text[:80]!r}"
                    )
                    metadata.setdefault("validation_repairs", []).append(
                        "Rejected causal-purpose evidence_need create "
                        "(no valid motivating hypothesis)"
                    )
                    continue

                # FULFILLED→PARTIALLY_MET demotion when all referenced
                # fulfilling evidence IDs were dropped as dangling. The
                # schema's create-path rule rejects FULFILLED + empty
                # list at emission, but the apply-layer drop happens
                # after that check; constructing EvidenceNeed with
                # FULFILLED + [] would raise via the model_validator.
                # The rule lives on the model (single owner); this site owns
                # only the repair note.
                requested_status = update.state or NeedState.PENDING
                effective_superseded_reason = update.superseded_reason
                effective_status = EvidenceNeed.admissible_state(
                    requested_status, valid_fulfillments
                )
                if effective_status != requested_status:
                    metadata.setdefault("validation_repairs", []).append(
                        "Demoted FULFILLED→PARTIALLY_MET on evidence_need "
                        "create (all fulfilling_evidence_ids dropped as "
                        "dangling)"
                    )
                    effective_superseded_reason = None

                new_need = EvidenceNeed(
                    case_id=case.case_id,
                    purpose=update.purpose,
                    request_text=update.request_text,
                    rationale=update.rationale,
                    # priority is Optional on EvidenceNeedUpdate (omitted on
                    # the update path); on create, fall back to MEDIUM.
                    priority=update.priority or NeedPriority.MEDIUM,
                    state=effective_status,
                    motivating_hypothesis_ids=valid_motivators,
                    fulfilling_evidence_ids=valid_fulfillments,
                    superseded_reason=effective_superseded_reason,
                    # Opt-in obtainability (§5.3); the model validator coerces it
                    # to UNKNOWN for symptom needs or terminal states.
                    obtainability=getattr(update, "obtainability", None)
                    or NeedObtainability.UNKNOWN,
                    created_at_turn=current_turn,
                )
                case.evidence_needs.append(new_need)
                needs_created_in_this_loop.append(new_need.need_id)
                metadata["evidence_needs_updated"].append(new_need.need_id)
                try:
                    evidence_need_created_total.labels(
                        purpose=new_need.purpose.value
                    ).inc()
                except Exception:
                    pass
                logger.info(
                    f"Created EvidenceNeed {new_need.need_id} "
                    f"(purpose={new_need.purpose.value}) on case {case.case_id}"
                )
                continue

            # UPDATE path (need_id is set)
            target = next(
                (n for n in case.evidence_needs if n.need_id == resolved_need_id),
                None,
            )
            if target is None:
                logger.warning(
                    f"evidence_need_update references unknown need_id "
                    f"{resolved_need_id!r} on case {case.case_id}; "
                    f"dropping update"
                )
                metadata.setdefault("validation_repairs", []).append(
                    f"Dropped evidence_need_update for unknown need_id "
                    f"{resolved_need_id!r}"
                )
                continue

            # Purpose is immutable on the update path. It is Optional on
            # EvidenceNeedUpdate and is normally OMITTED on update (None);
            # only warn when the LLM actually sent a *different* purpose.
            # (Guarding on ``is not None`` also avoids ``None.value`` here.)
            if update.purpose is not None and update.purpose != target.purpose:
                logger.warning(
                    f"evidence_need_update attempted to flip purpose on "
                    f"need {target.need_id} "
                    f"({target.purpose.value} → {update.purpose.value}); "
                    f"ignoring purpose change"
                )
                metadata.setdefault("validation_repairs", []).append(
                    f"Ignored purpose-change attempt on need {target.need_id}"
                )

            # SUPERSEDED is terminal — cannot resurrect via update.
            if target.state == NeedState.SUPERSEDED and update.state not in (
                None,
                NeedState.SUPERSEDED,
            ):
                logger.warning(
                    f"evidence_need_update attempted to resurrect "
                    f"SUPERSEDED need {target.need_id}; ignoring status "
                    f"change. Emit a new need instead."
                )
                metadata.setdefault("validation_repairs", []).append(
                    f"Ignored resurrection attempt on SUPERSEDED need {target.need_id}"
                )
                continue

            # Merge lists (append-only). Dedup is handled by the
            # EvidenceNeed field validator at assignment time.
            prior_status = target.state
            target.motivating_hypothesis_ids = list(
                dict.fromkeys(list(target.motivating_hypothesis_ids) + valid_motivators)
            )
            target.fulfilling_evidence_ids = list(
                dict.fromkeys(list(target.fulfilling_evidence_ids) + valid_fulfillments)
            )
            # Revise-don't-clobber: request_text / rationale / priority are
            # Optional on the update path and are normally omitted on a
            # fulfill/status update. Only overwrite when the LLM actually
            # supplied a new value — None means "leave unchanged". Without
            # this guard a bare fulfill update would null out request_text /
            # rationale (silent corruption) and downgrade priority to the
            # field default.
            #
            # For the two text fields we guard on truthiness, not ``is not
            # None``: an explicit "" is treated as "leave unchanged" too.
            # request_text/rationale are min_length=1 on the domain model
            # (validate_assignment is off, so "" wouldn't raise here — it
            # would crash on the next repo round-trip), and blanking a
            # mandatory field is never a valid revision. This mirrors the
            # create validator, which rejects ``in (None, "")``.
            if update.request_text:
                target.request_text = update.request_text
            if update.rationale:
                target.rationale = update.rationale
            if update.priority is not None:
                target.priority = update.priority
            # FULFILLED→PARTIALLY_MET demotion when the post-merge
            # fulfilling list is still empty. ``validate_assignment``
            # is off on EvidenceNeed, so in-place mutation bypasses
            # ``_validate_state_consistency`` — without this guard a
            # bad LLM emission could leave the need in FULFILLED+[]
            # state that raises on next reconstruction. The rule lives on the
            # model (single owner); this site owns only the repair note.
            effective_status = EvidenceNeed.admissible_state(
                update.state, target.fulfilling_evidence_ids
            )
            if effective_status != update.state:
                metadata.setdefault("validation_repairs", []).append(
                    f"Demoted FULFILLED→PARTIALLY_MET on need {target.need_id} "
                    f"(all fulfilling_evidence_ids dropped as dangling)"
                )
            if effective_status is not None:
                target.state = effective_status
            if effective_status == NeedState.SUPERSEDED:
                target.superseded_reason = update.superseded_reason
            elif (
                effective_status is not None
                and effective_status != NeedState.SUPERSEDED
            ):
                # Clearing superseded_reason on non-SUPERSEDED transition
                target.superseded_reason = None
            # Obtainability (§5.3): opt-in model declaration, scoped to
            # causal_verification (symptom declarations are out of scope).
            # ``validate_assignment`` is off on EvidenceNeed, so the model
            # validator's auto-revoke does not fire on in-place mutation —
            # apply the same rule here: reset to UNKNOWN when the need reaches a
            # terminal state (the question is moot). The rollup only reads
            # outstanding causal needs, so this is belt-and-suspenders for a
            # clean record rather than the correctness guarantee.
            _declared_obtainability = getattr(update, "obtainability", None)
            if _declared_obtainability is not None and (
                target.purpose == NeedPurpose.CAUSAL_VERIFICATION
            ):
                target.obtainability = _declared_obtainability
            # Auto-revoke on terminal state (§5.3) — centralized invariant.
            target.revoke_obtainability_if_terminal()
            target.updated_at = datetime.now(UTC)
            if target.need_id not in metadata["evidence_needs_updated"]:
                metadata["evidence_needs_updated"].append(target.need_id)

            if effective_status is not None and effective_status != prior_status:
                try:
                    evidence_need_status_changed_total.labels(
                        from_state=prior_status.value,
                        to_state=effective_status.value,
                    ).inc()
                except Exception:
                    pass
                logger.info(
                    f"Need {target.need_id} status "
                    f"{prior_status.value} → {effective_status.value} "
                    f"on case {case.case_id}"
                )

    # =========================================================================
    # State Management
    # =========================================================================

    async def _transition_to_investigating(self, case: Case) -> None:
        """
        Transition case from INQUIRY to INVESTIGATING.

        This creates the initial investigation structures and copies the
        confirmed problem statement to the case description.

        Evidence lifecycle:
            - File uploads create only ``UploadedFile`` rows at intake; no
              Evidence is auto-created. Preprocessing artifacts (summary,
              structural_index, data_type, coverage_*) live on the file row.
            - During INQUIRY no Evidence rows exist — the
              ``InquiryStateUpdate`` schema does not carry ``evidence_to_add``
              and the engine does not synthesize Evidence on transition.
              The LLM reads files via ``<uploaded_file>`` context blocks.
            - Evidence is born during INVESTIGATING: the LLM extracts
              claim-anchored slices via ``evidence_to_add``, each carrying a
              category (the verification quartet: symptom / causal +
              symptom_absence / causal_absence) and a ``source_file_id``
              back to the originating file.
            - Milestones derive from evidence categories as those rows are
              created turn-by-turn, not retroactively at the transition.

        Reference: ``docs/architecture/investigation-engine/
        evidence-driven-investigation-framework.md`` §5.
        """
        logger.info(f"Transitioning case {case.case_id} to INVESTIGATING")

        # Gap #6: Checkpoint before status change
        if self.checkpoint_service:
            await self.checkpoint_service.create_checkpoint(
                case,
                trigger="pre_case_action",
                metadata={
                    "from_state": case.state.value,
                    "to_state": "investigating",
                },
            )

        # Copy confirmed problem statement to description BEFORE changing status
        # (Pydantic validation requires description to be set before INVESTIGATING status)
        if case.inquiry.proposed_problem_statement:
            case.description = case.inquiry.proposed_problem_statement
        elif not case.description:
            # Manual flow: user may transition before agent proposes a statement.
            # Use case title as fallback to satisfy Pydantic validation.
            case.description = case.title or "Investigation requested by user"

        # Change status (Pydantic validation happens here)
        case.state = CaseState.INVESTIGATING

        # Initialize investigation progress
        case.progress = InvestigationProgress()

        # Initialize problem verification with confirmed statement
        verification_kwargs = {
            "symptom_statement": case.description or "Unspecified issue",
            "severity": "MEDIUM",  # Default when unknown (valid value: CRITICAL|HIGH|MEDIUM|LOW)
        }

        # Hydrate from problem confirmation if available
        if case.inquiry.problem_confirmation:
            pc = case.inquiry.problem_confirmation
            if pc.severity_guess.upper() in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
                verification_kwargs["severity"] = pc.severity_guess.upper()
            # else: keep default "MEDIUM" — severity_guess="unknown" is valid
            # for ProblemConfirmation but not for ProblemVerification

        # Hydrate from preliminary urgency if available
        if case.inquiry.preliminary_urgency:
            pu = case.inquiry.preliminary_urgency
            if pu.level:
                verification_kwargs["urgency_level"] = (
                    pu.level.lower()
                )  # Convert to lowercase for enum
                # If severity still at default (MEDIUM), use urgency level as severity (keep uppercase for severity)
                if (
                    verification_kwargs["severity"] == "MEDIUM"
                    and pu.level != UrgencyLevel.UNKNOWN
                ):
                    verification_kwargs["severity"] = (
                        pu.level.value.upper()
                    )  # Convert urgency level to uppercase for severity field
            # Bug fix: Transfer temporal_state from preliminary urgency
            # Without this, path selection receives Temporal:None and the
            # router falls back to the ROOT_CAUSE default (auto_selected=False)
            # rather than matching a definitive matrix row.
            if pu.is_ongoing:
                verification_kwargs["temporal_state"] = TemporalState.ONGOING
            else:
                verification_kwargs["temporal_state"] = TemporalState.HISTORICAL

        case.problem_verification = ProblemVerification(**verification_kwargs)

        # The INQUIRY → INVESTIGATING transition carries Gate 1
        # (problem-statement confirmation) only. There is no path fork
        # (redesign R5) — the investigation proceeds opportunistically.
        logger.info(f"Case {case.case_id}: transitioning to INVESTIGATING")

        # Post-010: no retroactive milestone attribution at INQUIRY→
        # INVESTIGATING. INQUIRY no longer creates Evidence rows, so
        # there is no INQUIRY-phase evidence to back-fill milestones for.
        # KB pre-fetch: search for runbooks matching the confirmed problem.
        # Deterministic, code-level — not an LLM tool call decision.
        # Results are stored on the case and injected into context by
        # context_builder so the LLM sees relevant runbooks from turn 1.
        await self._prefetch_kb_context(case, case.description, "symptom")

    async def _prefetch_kb_context(
        self,
        case: "Case",
        query: str,
        trigger: str,
    ) -> None:
        """Search KB for runbooks matching the query, store on case.

        Args:
            case: Case to update
            query: Search query (problem statement or root cause)
            trigger: What triggered this search ("symptom" or "root_cause")

        Side effect only: writes the top ``KB_CONTEXT_MAX_ENTRIES`` admitted
        hits to ``case.kb_context`` (or clears it on a miss) for the prompt
        builder. Nothing consumes a return value since the KB cause seeder
        was removed (fm#1295).
        """
        if not self.knowledge_service:
            return None

        # Policy gate on the PUSH channel (fm#1360, Option B). Off means the
        # search does not run AT ALL — the cost this gate exists to control is
        # the hybrid retrieval as much as the prompt surface it produces.
        #
        # Clearing rather than merely returning: ``case.kb_context`` is
        # persisted (it must be, or the push can never reach a prompt — see the
        # repository metadata blob), so a case that accumulated context while
        # the push was enabled would otherwise keep standing runbooks in its
        # turn response and telemetry after the operator turned the push off.
        # "Off" has to mean off for the case, not only for new searches.
        #
        # The prompt is guarded independently in ``context_builder`` — that is
        # the seam that decides what the model actually sees, and it must hold
        # for a case reloaded with context already on it.
        from faultmaven.config.settings import get_settings

        if not get_settings().knowledge.kb_prefetch_enabled:
            case.kb_context = None
            return None

        try:
            # Owner-aware scope. The pre-fetch may
            # read only what the case OWNER can read: global (platform-curated)
            # plus the owner's own personal KB. This completes the flywheel
            # loop — a user's resolved cases, converted to personal runbooks,
            # seed that user's own future investigations — while preserving
            # strict cross-user isolation: the personal condition is keyed on
            # the owner's user_id, so user B's case can never surface user A's
            # personal runbooks. Without this filter search_knowledge defaults
            # to global-only, so personal (case-generated) runbooks never seed.
            #
            # The team arm resolves the case OWNER's shared-kb-id allowlist —
            # keyed on case.user_id, NOT the session user, so user B's case can
            # never surface user A's runbooks — via the same share table → id
            # allowlist the QA path uses (resolve_shared_kb_ids, ADR-013 §D4),
            # passed as the second arg to build_kb_scope_filter. It is inert in
            # practice until case→runbook conversion emits team-shared runbooks
            # (there are none to seed yet), and in standalone: team_service is
            # None, so the owner resolves an empty shared set and the scope
            # collapses to global ∪ owner-personal.
            from faultmaven.modules.knowledge.domain.services.knowledge_service import (
                build_kb_scope_filter,
                resolve_shared_kb_ids,
            )

            owner_id = getattr(case, "user_id", None)
            # team_service/share_repository are wired post-construction; use
            # getattr so a partially-built engine (or standalone) safely skips
            # the team arm rather than raising.
            team_service = getattr(self, "team_service", None)
            share_repository = getattr(self, "share_repository", None)
            shared_kb_ids: list[str] = []
            if owner_id and team_service and share_repository:
                try:
                    owner_team_ids = await team_service.list_all_user_team_ids(owner_id)
                    shared_kb_ids = await resolve_shared_kb_ids(
                        share_repository,
                        owner_team_ids,
                        getattr(case, "enterprise_id", None),
                    )
                except Exception:  # noqa: BLE001
                    # Graceful degradation — global ∪ owner-personal still apply.
                    shared_kb_ids = []
            scope_filter = build_kb_scope_filter(owner_id, shared_kb_ids)
            # Fetch KB_PREFETCH_FETCH_LIMIT chunks — the reranker's candidate
            # pool, see the constant — and render only the top
            # KB_CONTEXT_MAX_ENTRIES into the prompt.
            #
            # HYBRID, not pure vector (#1272). An operator writes what they
            # SAW — "cannot write its PID file", "qemu failed to start" — and
            # those words are precisely what an embedding smooths into the
            # neighbourhood of every other "process won't start" runbook. On
            # the shipped pack that put the runbook covering the failure at
            # rank 70 of 91 while the top ten were all Kubernetes. Adding the
            # keyword-constrained arm and the IDF-weighted reranker moves it to
            # rank 1 for the same query, and pure vector search cannot: the
            # fetch limit is applied to CHUNKS before any floor, so no
            # threshold value can admit a chunk ranked 369th.
            results = await self.knowledge_service.search_knowledge(
                query=query,
                limit=KB_PREFETCH_FETCH_LIMIT,
                filters=scope_filter,
                use_hybrid=True,
                # The floor goes in at ADMISSION, not after ranking. Hybrid
                # results are ordered by the reranker's blend, so the filter
                # below would thin this window from the middle: on a measured
                # query it left 2 hits where 10 were asked for. The filter
                # below stays as the
                # authority (and still governs the pure-vector fallback).
                min_score=KB_PREFETCH_RELEVANCE_THRESHOLD,
            )
            relevant = [
                r for r in results or [] if r.score >= KB_PREFETCH_RELEVANCE_THRESHOLD
            ]
            if relevant:
                # `results` is ordered by the reranker's blend; the floor below
                # is applied to `score`, which stays the raw cosine on every
                # path. Two quantities on purpose — an absolute one to admit
                # with, a relative one to order by — so this slice is the top of
                # the RANKING and the filter is a statement about ABSOLUTE
                # similarity. Ordering by cosine instead would discard the
                # keyword and term-overlap evidence that produced the ranking.
                case.kb_context = [
                    {
                        "title": r.title,
                        # Which SECTION of the runbook matched. Two chunks of one
                        # runbook are two entries with the same title, so without
                        # this the prompt cannot tell them apart (#1379 review).
                        "section": _chunk_label(r.snippet),
                        "summary": r.snippet,
                        "score": r.score,
                        "type": getattr(r, "document_type", "runbook"),
                        "parent_document_id": getattr(r, "parent_document_id", None),
                        "trigger": trigger,
                    }
                    for r in _admit_diverse(relevant)
                ]
                # Identity, not just a count (fm#1361). "3 matches" cannot
                # answer "which runbook informed this answer?" or "was
                # retrieval any good?" — both need to know WHICH documents were
                # admitted and at what score, and reconstructing that meant
                # re-running the case. Emitted twice on purpose: in the message
                # for a human reading a log, and under ``extra`` as separate
                # fields for a structured consumer (the root handler is
                # structlog's ProcessorFormatter with ExtraAdder, so these land
                # as top-level keys on the JSON line).
                _kb_ids = [
                    str(r.get("parent_document_id") or "") for r in case.kb_context
                ]
                _kb_scores = [float(r.get("score") or 0.0) for r in case.kb_context]
                logger.info(
                    "KB pre-fetch (%s): %d matches for case %s: %s",
                    trigger,
                    len(case.kb_context),
                    case.case_id,
                    "; ".join(
                        f"{r.get('title') or '(untitled)'}"
                        f" [{r.get('parent_document_id') or 'no-id'}]"
                        f" score={float(r.get('score') or 0.0):.3f}"
                        for r in case.kb_context
                    ),
                    extra={
                        "kb_prefetch_trigger": trigger,
                        "kb_prefetch_hits": len(case.kb_context),
                        "kb_prefetch_top_score": max(_kb_scores),
                        "kb_runbook_ids": _kb_ids,
                        "kb_runbook_titles": [
                            str(r.get("title") or "") for r in case.kb_context
                        ],
                    },
                )
            else:
                # Nothing usable this trigger → clear stale context, so a later
                # trigger's miss cannot leave an earlier trigger's runbooks
                # standing in the prompt as if they still matched.
                #
                # This used to read ``elif results:``, distinguishing "searched
                # and found only weak matches" (clear) from "searched and found
                # nothing at all" (leave alone, in case the search itself had
                # failed). Moving the floor to admission collapsed that
                # distinction — `results` is already floored, so `relevant` is
                # empty exactly when `results` is — which left the branch
                # unreachable and the stale context never cleared.
                #
                # `else` is the right resolution rather than a way to restore
                # the old shape: the hazard the old guard existed for is
                # already handled above. A search that genuinely FAILS raises
                # (the embedder guard turns an unavailable model into an
                # exception rather than an empty list), and the handler below
                # returns without touching `kb_context`. So reaching here means
                # the search ran and produced nothing worth showing, which is
                # precisely when stale context should go.
                case.kb_context = None
            return None
        except Exception:
            logger.warning(
                f"KB pre-fetch ({trigger}) failed for case {case.case_id}",
                exc_info=True,
            )
            return None

    async def _check_automatic_transitions(
        self, case: Case, metadata: dict[str, Any], user_message: str = ""
    ) -> Case:
        """
        Check if case should automatically transition status.

        Automatic Transitions (non-terminal):
        - INQUIRY -> INVESTIGATING when problem_statement_confirmed=True
          (Gate 1 — the single condition; see gate1_passed below)

        v3: INQUIRY -> RESOLVED edge removed. KB-driven cases route through
        INVESTIGATING via the KB-resolution milestone collapse — the
        structured attribution (RootCauseConclusion + Solution + gate
        milestones) is authored in one turn, but the RESOLVED disposition
        still requires the explicit confirm turn like every other terminal
        transition (#722) — see
        docs/architecture/investigation-engine/investigation-lifecycle-logic.md
        §1.2 INVESTIGATING -> RESOLVED -> KB-Resolution Path.

        User-Agent Handshake Transitions (terminal):
        - INVESTIGATING -> RESOLVED requires ProposedTransition + user confirmation
        - Any -> CLOSED requires explicit user action

        ProposedTransition handling:
        - If the LLM response includes a proposed_transition, store it as pending
        - The transition is NOT executed until the user confirms in the next turn
        - If a pending_transition exists and user confirms, execute it
        """
        old_status = case.state

        # 0. Handle pending transition confirmation from previous turn
        # Skip confirmation check if we just proposed a transition this turn
        # (User-Agent Handshake). ``transition_proposed_this_turn`` is the ONE
        # flag every same-turn proposal site sets — the LLM-emit path (step 2
        # below), the rca_infeasible stage-gate side effect, and the deferred-
        # solution close — so a proposal can never be confirmed by the very
        # message that produced it (#722): the user must see the confirmation
        # prompt and answer on a LATER turn. The KB-resolution path is no
        # exception — its same-turn confirm collapse was removed (#722): the
        # user's "it worked" message is the solution-verification claim, not
        # consent to the irreversible RESOLVED transition.
        if hasattr(case, "pending_transition") and case.pending_transition:
            if metadata.get("transition_proposed_this_turn", False):
                logger.info(
                    "Skipping confirmation check - transition was just proposed this turn"
                )
            elif case.pending_transition.get("needs_info"):
                # User was told what's missing and has now responded.
                # Re-evaluate readiness: did the LLM actually capture root
                # cause / solution from what the user provided?
                from faultmaven.core.investigation.terminal_transitions import (
                    assess_resolution_readiness,
                    cancel_pending_transition,
                    propose_transition,
                )

                readiness = assess_resolution_readiness(case)
                # Telemetry: transition_compliance carries the readiness
                # verdict so a pending-but-not-transitioned turn is
                # self-explaining in logs (#656 triage misread this as a
                # silent gate refusal).
                metadata["resolution_readiness_verdict"] = readiness.verdict
                metadata["resolution_readiness_missing"] = readiness.missing

                if readiness.verdict == readiness.READY:
                    # Requirements met — clear needs_info, show confirmation
                    case.pending_transition["needs_info"] = False
                    metadata["resolution_ready_for_confirmation"] = True
                    logger.info(
                        f"Case {case.case_id}: needs_info resolved, "
                        f"requirements met — presenting confirmation"
                    )
                elif readiness.verdict == readiness.SUGGEST_CLOSE:
                    # Still fundamentally lacking — pivot to CLOSED. Propose
                    # the close transition (not just emit a suggestion) so
                    # the user's next positive confirmation actually fires.
                    # The earlier code only emitted the message and the
                    # close suggestions, with no pending transition for
                    # those suggestions to confirm — producing the stuck
                    # loop documented in project-resolution-gate-stuck-loop.
                    # closure_reason auto-derives via derive_closure_reason().
                    cancel_pending_transition(case)
                    propose_transition(
                        case=case,
                        to_state="closed",
                        summary=readiness.message,
                    )
                    metadata["transition_proposed_this_turn"] = True
                    metadata["resolution_suggest_close"] = True
                    metadata["resolution_readiness_message"] = readiness.message
                    logger.info(
                        f"Case {case.case_id}: needs_info not satisfied, "
                        f"proposing Close (missing: {readiness.missing})"
                    )
                else:
                    # NEEDS_INFO still — user was asked once, didn't (or
                    # couldn't) provide. Don't loop asking again. Propose
                    # CLOSE so the user's next positive confirmation fires
                    # — the loop's root cause was emitting a close-
                    # suggestion with no pending transition to confirm.
                    cancel_pending_transition(case)
                    close_message = (
                        "I understand. Without confirmation that the root cause "
                        "was **eliminated** (e.g. the original error is now "
                        "absent after the fix), I can't mark this as "
                        "**resolved** — a restored-but-stabilized or "
                        "deferred-fix case isn't a resolution.\n\n"
                        "You can **close** the case instead — this preserves "
                        "the root cause analysis and the documented (or "
                        "deferred) solution."
                    )
                    propose_transition(
                        case=case,
                        to_state="closed",
                        summary=close_message,
                    )
                    metadata["transition_proposed_this_turn"] = True
                    metadata["resolution_suggest_close"] = True
                    metadata["resolution_readiness_message"] = close_message
                    logger.info(
                        f"Case {case.case_id}: needs_info not satisfied after "
                        f"second ask, proposing Close "
                        f"(missing: {readiness.missing})"
                    )
            else:
                from faultmaven.core.investigation.terminal_transitions import (
                    ClosureReadiness,
                    cancel_pending_transition,
                    confirm_pending_transition,
                )

                # Use the user_message parameter directly, not from metadata
                if self._user_confirms_transition(user_message):
                    # Gap #6: Checkpoint before terminal transition
                    if self.checkpoint_service:
                        to_state = case.pending_transition.get("to_state", "unknown")
                        await self.checkpoint_service.create_checkpoint(
                            case,
                            trigger="pre_case_action",
                            metadata={
                                "from_state": case.state.value,
                                "to_state": to_state,
                            },
                        )
                    executed = confirm_pending_transition(case, case.user_id)
                    if executed:
                        metadata["status_transitioned"] = True
                    else:
                        # INV-37 resolve-preservation: the pending CLOSE pivoted
                        # to a RESOLVED proposal because the case became
                        # resolvable. Nothing terminal committed — surface the
                        # resolve confirmation (prose appended below the LLM's
                        # reply + the canonical resolve DECIDE pair) instead of
                        # closing. The pending_transition now targets "resolved".
                        metadata["close_pivoted_to_resolve"] = True
                        metadata["override_suggestions"] = (
                            _resolution_confirmation_suggestions()
                        )
                        metadata["closure_readiness_verdict"] = (
                            ClosureReadiness.SUGGEST_RESOLVE
                        )
                    return case
                elif self._user_declines_transition(user_message):
                    cancel_pending_transition(case)
                    # Continue normal processing
                # else: user said something ambiguous, let LLM handle it

        # 1. INQUIRY transitions
        # v3: INQUIRY → RESOLVED edge removed. KB-driven cases route through
        # INVESTIGATING via the KB-resolution milestone collapse documented in
        # docs/architecture/investigation-engine/investigation-lifecycle-logic.md
        # §1.2 INVESTIGATING → RESOLVED → KB-Resolution Path. Confirming the
        # problem statement is mandatory even when a runbook applies cleanly.
        #
        # INV-19: INQUIRY → INVESTIGATING requires Gate 1 only (problem
        # statement confirmation). Gate 2 (path selection) is no longer a
        # transition gate — it fires later, inside INVESTIGATING, after
        # ``symptom_verified`` so the user sees the agent's data-inspection
        # work in the transcript before committing. (The recommendation
        # itself is still computed from user-claimed urgency; making the
        # recommendation evidence-derived is deferred follow-up.)
        if case.state == CaseState.INQUIRY:
            # Gate 1 is the problem-statement confirmation, so that is what
            # this reads. It used to be an OR across two fields that every
            # writer sets together — ``decided_to_investigate`` and
            # ``problem_statement_confirmed and problem_confirmation`` — which
            # made the second arm load-bearing only for cases carrying no
            # ``problem_confirmation`` and hid the fact that the first field
            # has no independent meaning. One condition, one gate (#1607).
            gate1_passed = case.inquiry.problem_statement_confirmed
            if gate1_passed:
                await self._transition_to_investigating(case)
                metadata["status_transitioned"] = True
                case.action_history.append(
                    CaseAction(
                        from_state=old_status,
                        to_state=CaseState.INVESTIGATING,
                        triggered_by="system",
                        reason="Problem statement confirmed",
                    )
                )
                return case

        # 2. Handle ProposedTransition from LLM response (User-Agent Handshake)
        # The LLM proposes a terminal transition; we store it pending.
        # Auto-transition on solution_verified is REMOVED — all terminal
        # transitions require explicit user confirmation.
        response_obj = metadata.get("response_obj")
        if response_obj and hasattr(response_obj, "state_updates"):
            proposed = getattr(response_obj.state_updates, "proposed_transition", None)
            if proposed:
                from faultmaven.core.investigation.terminal_transitions import (
                    assess_closure_readiness,
                    assess_resolution_readiness,
                    propose_transition,
                )
                from faultmaven.modules.case.domain.models.lifecycle import (
                    LEGAL_TRANSITIONS,
                )

                # Structural validation against the LEGALITY graph — which
                # edges exist, not which ones a user may pick. The LLM is not
                # a user: it proposes transitions the state machine permits,
                # so ``USER_SELECTABLE_ACTIONS`` would be the wrong bar here.
                # (In practice INQUIRY → INVESTIGATING never arrives as a
                # ``proposed_transition`` — Gate 1 performs it — so the two
                # graphs would accept the same emissions today. The right one
                # is named anyway, so a future edge cannot silently inherit
                # the wrong rule.) The prompt instructs the LLM on
                # which edges exist; this is the safety net for prompt
                # non-compliance (e.g., an LLM emitting ``to_state="resolved"``
                # from INQUIRY, which is not a valid edge — INQUIRY can
                # only transition to INVESTIGATING or CLOSED). Rejecting
                # here prevents downstream pivot logic from accepting an
                # invalid emission and quietly converting it into a
                # different transition the user never intended.
                valid_targets = {s.value for s in LEGAL_TRANSITIONS.get(case.state, [])}
                if proposed.to_state not in valid_targets:
                    logger.warning(
                        f"Rejected proposed_transition for case {case.case_id}: "
                        f"to_state={proposed.to_state!r} is not a valid edge "
                        f"from {case.state.value!r}. "
                        f"Valid targets: {sorted(valid_targets)}."
                    )
                    current_feedback = metadata.get("system_feedback") or ""
                    valid_list = (
                        ", ".join(f"{t!r}" for t in sorted(valid_targets))
                        or "(none — case is terminal)"
                    )
                    metadata["system_feedback"] = (
                        f"{current_feedback}\n"
                        "INVALID TRANSITION ERROR: You emitted "
                        f"``proposed_transition.to_state={proposed.to_state!r}`` "
                        f"from case.state={case.state.value!r}, which is "
                        f"not a valid edge in the case action graph. "
                        f"Valid targets from {case.state.value!r}: "
                        f"{valid_list}. "
                        "Per the lifecycle: from INQUIRY only CLOSED is a "
                        "valid proposed_transition (resolution requires "
                        "investigation work first — there is no "
                        "INQUIRY → RESOLVED edge). Do not re-emit this "
                        "transition; emit only valid edges."
                    ).strip()
                    metadata.setdefault("validation_repairs", []).append(
                        f"Rejected proposed_transition.to_state="
                        f"{proposed.to_state!r} from {case.state.value!r}"
                    )
                    # Skip downstream proposal processing.
                    proposed = None

            # Loop-bound (project-resolution-gate-stuck-loop): if the
            # handshake block above already pivoted this case to CLOSE this
            # turn — a repeated resolution NEEDS_INFO that re-asking cannot
            # satisfy (the user keeps confirming but no Solution is/can be
            # recorded) — do NOT let the LLM's same-turn ``proposed_transition``
            # re-arm RESOLVED and clobber that CLOSE via ``propose_transition``.
            # The LLM re-proposes RESOLVED every turn while the user confirms;
            # without this guard the CLOSE pivot is overwritten every turn and
            # the gate loops forever (Run 36, case_95d86b7daf8c). Honoring the
            # CLOSE pivot terminates the case cleanly (root cause preserved).
            if proposed and metadata.get("resolution_suggest_close"):
                logger.info(
                    f"Case {case.case_id}: honoring handshake CLOSE pivot — "
                    f"ignoring same-turn LLM proposed_transition="
                    f"{getattr(proposed, 'to_state', None)!r} so it does not "
                    f"clobber the escape from a repeated resolution NEEDS_INFO."
                )
                proposed = None

            if proposed:
                # The LLM emits only to_state (and optional evidence_ids).
                # Engine handles everything else: closure_reason is derived
                # inside propose_transition; summary is built programmatically
                # via the same helpers every opener uses, so they produce
                # identical confirmation prompts.
                #
                # When the LLM proposes RESOLVED, run the same readiness
                # check every other opener uses, so the user sees a
                # coherent prompt + suggestion pair:
                #   SUGGEST_CLOSE → pivot to CLOSED (close suggestion pair)
                #   NEEDS_INFO    → keep RESOLVED but flag needs_info; the
                #                   response builder overrides agent_response
                #                   with the readiness message
                #   READY         → propose RESOLVED with confirmation prompt
                # When the LLM proposes CLOSED, symmetric pivot:
                #   SUGGEST_RESOLVE → pivot to RESOLVED (case has root cause
                #                     + solution; closing would discard the
                #                     resolution attribution)
                #   HAS_SUBSTANCE / TRIVIAL → propose CLOSED with summary
                effective_to_status = proposed.to_state
                needs_info_message: str | None = None

                if proposed.to_state == "resolved":
                    readiness = assess_resolution_readiness(case)
                    metadata["resolution_readiness_verdict"] = readiness.verdict
                    metadata["resolution_readiness_missing"] = readiness.missing
                    if readiness.verdict == readiness.SUGGEST_CLOSE:
                        effective_to_status = "closed"
                        summary = readiness.message
                        logger.info(
                            f"Agent proposed RESOLVED but case {case.case_id} "
                            f"verdict=SUGGEST_CLOSE (missing: {readiness.missing}); "
                            f"pivoting to CLOSED."
                        )
                    elif readiness.verdict == readiness.NEEDS_INFO:
                        summary = readiness.message
                        needs_info_message = readiness.message
                        logger.info(
                            f"Agent proposed RESOLVED but case {case.case_id} "
                            f"verdict=NEEDS_INFO (missing: {readiness.missing}); "
                            f"keeping RESOLVED intent with needs_info flag."
                        )
                    else:
                        summary = _build_resolution_confirmation(case)
                else:  # closed
                    closure = assess_closure_readiness(case)
                    metadata["closure_readiness_verdict"] = closure.verdict
                    if closure.verdict == closure.SUGGEST_RESOLVE:
                        effective_to_status = "resolved"
                        summary = closure.message
                        logger.info(
                            f"Agent proposed CLOSED but case {case.case_id} "
                            f"verdict=SUGGEST_RESOLVE (case has root cause "
                            f"+ solution); pivoting to RESOLVED."
                        )
                    else:
                        summary = closure.message

                propose_transition(
                    case=case,
                    to_state=effective_to_status,
                    summary=summary,
                    evidence_ids=getattr(proposed, "evidence_ids", None),
                )
                if needs_info_message is not None:
                    case.pending_transition["needs_info"] = True
                    # The response builder reads this to override the LLM's
                    # agent_response with the readiness message, matching the
                    # readiness gate's first-pass behavior.
                    metadata["resolution_needs_info_first_pass"] = True
                    metadata["resolution_needs_info_message"] = needs_info_message
                metadata["transition_proposed_this_turn"] = True
                # Override LLM-emitted suggestions with the canonical
                # confirm/decline pair, so every opener produces the same
                # structured DECIDE confirmation UX — this branch, the engine's
                # INV-43 backstop, and a CLOSE pick from the menu. The
                # response builder consumes metadata["override_suggestions"]
                # at the final assembly point.
                if effective_to_status == "resolved":
                    metadata["override_suggestions"] = (
                        _resolution_confirmation_suggestions()
                    )
                else:  # closed
                    metadata["override_suggestions"] = _close_confirmation_suggestions()
                logger.info(
                    f"Agent proposed transition → {effective_to_status} "
                    f"(pending user confirmation)"
                )

        return case

    def _user_confirms_transition(self, user_message: str) -> bool:
        """Fallback check for typed confirmations (not DECIDE clicks).

        DECIDE suggestion clicks now carry intent metadata and route
        through IntentType.CONFIRMATION deterministically. This matcher
        is a safety net for users who type instead of clicking.

        Uses a 100-char length guard: short messages are direct responses
        to the confirmation prompt; longer messages likely contain context
        that should go through normal LLM processing.

        A match here executes a TERMINAL transition, so it must be a BARE
        confirmation: tokens match on word boundaries ("yesterday…" is not
        "yes"), and a message carrying a question or a contrastive
        continuation ("ok but what is the root cause?") is substantive
        input, not consent — it falls to the pending-gate escape lane
        instead (INV-26: the gate never consumes substantive input). The
        substance test is the shared ``is_substantive_reply`` predicate —
        the same one that guards classifier-minted confirmation intents at
        the IntentResolver adoption site (#721), so the two confirm lanes
        cannot drift apart.
        """
        from faultmaven.core.investigation.terminal_transitions import (
            is_substantive_reply,
        )

        if not user_message:
            return False
        if is_substantive_reply(user_message):
            return False
        msg = user_message.strip().lower()
        confirm_patterns = [
            "yes",
            "yeah",
            "yep",
            "yup",
            "correct",
            "confirmed",
            "confirm",
            "approve",
            "approved",
            "ok",
            "okay",
            "sure",
            "absolutely",
            "go ahead",
            "go for it",
            "do it",
            "please do",
            "proceed",
            "mark as resolved",
            "mark it as resolved",
            "resolve it",
            "close it",
            "that's right",
            "that's correct",
            "sounds good",
            "looks good",
            "lgtm",
        ]
        return _matches_gate_token(msg, confirm_patterns)

    def _user_declines_transition(self, user_message: str) -> bool:
        """Check if user message declines a pending transition.

        Tokens match on word boundaries — "note db latency spiked" must not
        read as "no", nor "stopped the pod" as "stop" (the old bare
        ``startswith`` swallowed such evidence-bearing messages with a
        canned acknowledgment).
        """
        if not user_message:
            return False
        msg = user_message.strip().lower()
        decline_patterns = [
            "no",
            "nope",
            "not yet",
            "wait",
            "cancel",
            "don't",
            "not ready",
            "hold on",
            "stop",
        ]
        return _matches_gate_token(msg, decline_patterns)

    # v3: `_check_fast_track_resolution` and `KB_FAST_TRACK_THRESHOLD` removed.
    # KB-driven cases route through INVESTIGATING via the KB-resolution
    # milestone collapse. See indicator-resolution.md +
    # investigation-lifecycle-logic.md §1.2 INVESTIGATING → RESOLVED →
    # KB-Resolution Path. The collapse is state authoring only, applied in
    # `_apply_investigation_updates`'s `knowledge_resolution` branch (gate
    # milestones set there); RootCauseConclusion + Solution are populated
    # from the LLM's structured emissions in the same turn. The RESOLVED
    # disposition still requires the explicit confirm turn (#722).

    def _determine_turn_outcome(
        self, case: Case, metadata: dict[str, Any], reported_outcome: TurnOutcome
    ) -> TurnOutcome:
        """
        Determine turn outcome classification (Bug #8).
        Checked AFTER milestone detection and evidence processing.
        """
        from faultmaven.core.investigation.turn_outcome import determine_turn_outcome

        return determine_turn_outcome(
            case=case,
            progress_made=metadata.get("progress_made", False),
            milestones_completed=metadata.get("milestones_completed", []),
            evidence_added=metadata.get("evidence_added", []),
            hypotheses_generated=len(metadata.get("hypotheses_generated", [])),
            solutions_proposed=len(metadata.get("solutions_proposed", [])),
        )

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

    def _create_turn_record(
        self,
        turn_number: int,
        milestones_completed: list[str],
        evidence_added: list[str],
        hypotheses_generated: list[str],
        hypotheses_validated: list[str],
        solutions_proposed: list[str],
        progress_made: bool,
        outcome: TurnOutcome,
        user_message: str,
        agent_response: str,
        system_feedback: str | None = None,
        momentum: InvestigationMomentum | None = None,
        blocked_reasons: list[str] | None = None,
        next_steps: list[str] | None = None,
        repair_pattern: str | None = None,
        validation_repairs: list[str] | None = None,
        agent_response_synthesized: bool = False,
    ) -> TurnProgress:
        """Create turn progress record."""
        # Multiple backstops (path-conditional emission rejection, milestone
        # ordering, data-quality blockers, prompt-injection alerts, etc.)
        # all append to ``metadata["system_feedback"]`` independently. A
        # single turn can fire 4+ backstops (e.g., LLM emits root_cause
        # milestone + causal_evidence + hypotheses_to_add + solutions_to_add
        # in a pre_path_investigating state), pushing the accumulated text
        # past ``TurnProgress.system_feedback``'s 1000-char Pydantic cap and
        # crashing the turn save. Truncate at the chokepoint so every
        # accumulation path is covered without per-call edits.
        if system_feedback and len(system_feedback) > 1000:
            system_feedback = system_feedback[:980] + "\n... [truncated]"
        return TurnProgress(
            turn_number=turn_number,
            timestamp=datetime.now(UTC),
            milestones_completed=milestones_completed,
            evidence_added=evidence_added,
            hypotheses_generated=hypotheses_generated,
            hypotheses_validated=hypotheses_validated,
            solutions_proposed=solutions_proposed,
            progress_made=progress_made,
            outcome=outcome,
            user_message_summary=self._summarize_text(user_message, 200),
            agent_response_summary=self._summarize_text(agent_response, 500),
            agent_response_synthesized=agent_response_synthesized,
            system_feedback=system_feedback,
            momentum=momentum,
            blocked_reasons=blocked_reasons or [],
            next_steps=next_steps or [],
            repair_pattern=repair_pattern,
            validation_repairs=validation_repairs or [],
        )

    def _report_turn_uploads(
        self,
        case: Case,
        attachments: list[dict[str, Any]] | None,
    ) -> dict[str, list[str]]:
        """The turn's uploads, as the two metadata keys that report them.

        Thin bind of ``turn_uploads.report_turn_uploads`` to this ``case``. The
        derivation is a free function because ``InvestigationService`` needs the
        same reading for the two SERVICE-routed handlers that never reach the
        engine (#1229) — one derivation, not a copy per caller.

        Called once per turn from ``_process_turn_impl``, ABOVE the path fork,
        so the reading and its two degradation warnings reach the deterministic
        early-return branches and the terminal short-circuit as well as the
        generation path.
        """
        return report_turn_uploads(case.case_id, case.current_turn, attachments)

    def _finish_deterministic_turn(
        self,
        case: Case,
        user_message: str,
        agent_response: str,
        upload_report: dict[str, list[str]],
        *,
        milestones_completed: list[str] | None = None,
        progress_made: bool = False,
        status_transitioned: bool = False,
    ) -> dict[str, Any]:
        """Close out a deterministic early-return turn: ONE progress decision,
        applied to all three surfaces that report it (#1229).

        The deterministic branches (the pending resolve/close gate, and the
        CLOSE status-transition handler — the resolve one went with the menu
        entry) answer without an LLM call. They
        used to record a hardcoded ``progress_made=False`` ``TurnProgress`` in
        one place and build a hand-written metadata dict in another, and
        neither consulted the turn's uploads. This is both, from one reading,
        so the stored turn-history entry, the returned metadata and the case's
        stall counter cannot disagree about the same turn.

        Must be called BEFORE the branch's ``repository.save(case)`` — the
        counter it writes is part of what that save persists. Every call site
        follows the ``metadata = self._finish_deterministic_turn(...)`` →
        ``save`` → ``return`` shape for that reason. (Recording a
        ``TurnProgress`` at all is load-bearing on its own: without one the
        turn_history validator rejects the case on its next load, because a
        deterministic branch still consumes a turn number.)

        **A genuinely novel upload counts as progress here.** The reading is
        ``_check_if_progress_made`` itself, not a copy of one arm of it, so a
        progress arm added there in future lands on these paths too rather than
        on the generation path alone. Its ``novel_files_uploaded`` arm is what
        fires for an upload: ``_check_if_progress_made`` defines progress as
        *advancement, not activity* — "an artifact the case did not already
        have" — and a file that survived content-hash dedup is exactly that.
        Nothing about a gate turn makes that untrue: whether the user accepted
        a mitigation is orthogonal to whether new data arrived.

        The accounting stays **one-directional**: progress RESETS
        ``turns_without_progress``, and nothing here ever increments it. That
        asymmetry is deliberate, and it is also what these paths already did —
        measured, not assumed: the increment at Step 5.8 sits inside the
        generation block, so a deterministic branch never reached it and the
        counter was FROZEN, not advanced. (#1229 reported it as incrementing;
        it does not.) Both arms therefore err the same way — against a stall
        net firing on a turn the engine did no investigative work on.

        Nothing releases a pending gate on ``turns_without_progress``, so
        resetting it cannot park one open: the gate's own escape lane keys on
        ``pending_transition["re_presented"]`` and withdraws after at most one
        re-present.
        """
        metadata: dict[str, Any] = {
            "turn_number": case.current_turn,
            "milestones_completed": milestones_completed or [],
            "progress_made": progress_made,
        }
        if status_transitioned:
            metadata["status_transitioned"] = status_transitioned
        # Upload keys before the progress read: ``check_if_progress_made``
        # scores ``novel_files_uploaded`` off this same dict.
        metadata.update(upload_report)
        # The SHARED monotone write, not a fourth copy of it (#1270). ``metadata
        # ["progress_made"]`` is already seeded with the caller's ``progress_made``
        # above, and ``score_progress`` is ``seeded or predicate(...)`` -- the same
        # expression this line used to spell out. Spelling it out again meant a
        # refinement to ``score_progress`` silently skipped all ten deterministic
        # branches, which is the divergence-between-copies failure the rest of
        # this work exists to close.
        self._score_progress(metadata)

        # The shared prompt-less record, which also forwards the previous
        # turn's ``system_feedback``: none of these branches builds a prompt
        # (#1688).
        record_promptless_turn(
            case,
            user_message=user_message,
            agent_response=agent_response,
            progress_made=metadata["progress_made"],
            milestones_completed=metadata["milestones_completed"],
        )
        # #1142: the same handoff the generation path builds, so a deterministic
        # turn is a ROW in the stream rather than a gap. A gap is worse than an
        # uninteresting row: streaks computed over the stream silently shorten,
        # and a correct multi-turn confirmation handshake — which is exactly
        # what these branches serve — would read as an engine-dry run.
        metadata[TELEMETRY_HANDOFF_KEY] = {
            "path": TurnPath.DETERMINISTIC,
            "arms": collect_progress_arms(metadata),
            "gate_name": None,
            # Carried in the handoff rather than written onto ``metadata``: the
            # TurnProgress these branches record is CONVERSATION, but the
            # returned dict is persisted onto the assistant message row and
            # adding a key there is a wire-visible change this does not need.
            "outcome": TurnOutcome.CONVERSATION,
        }
        return metadata

    def _score_progress(self, metadata: dict[str, Any]) -> bool:
        """Thin delegate to :func:`score_progress`, the monotone write.

        Kept as a method for the reason ``_check_if_progress_made`` is: the
        engine's own call sites and their tests target the method. The write
        itself lives at module scope so the service's consumed-turn backstop
        (#1264) applies the SAME monotone rule rather than a copy of it.
        """
        return score_progress(metadata)

    def _check_if_progress_made(self, metadata: dict[str, Any]) -> bool:
        """Thin delegate to :func:`check_if_progress_made`.

        The reading moved to module scope so callers outside this class — the
        service's consumed-turn backstop (#1264) — can score a turn with the
        SAME predicate rather than reimplementing it or hardcoding a verdict.
        Kept as a method because every existing call site and test targets it.
        """
        return check_if_progress_made(metadata)

    def _summarize_text(self, text: str, max_length: int = 200) -> str:
        """Thin delegate to :func:`summarize_for_turn_record`."""
        return summarize_for_turn_record(text, max_length)

    # =============================================================================
    # Phase 4 Housekeeping & Helpers
    # =============================================================================

    def _perform_hypothesis_housekeeping(
        self,
        case: Case,
        metadata: dict[str, Any],
        *,
        investigation_advanced: bool,
    ) -> None:
        """Apply confidence decay and anchoring detection.

        ``investigation_advanced`` is the turn's final ``progress_made``, so the
        turn path calls this after ``_score_progress``; it is required, so no
        caller can run the age sweep on a turn it has not judged.
        ``case.turns_without_progress`` is read before Step 5.8 updates it — as
        of the previous turn — so the stall arm below engages one turn after the
        exhaustion detector sees the stall, the direction that errs toward not
        counting.
        """
        active_hypotheses = [
            h for h in case.hypotheses.values() if h.state == HypothesisState.ACTIVE
        ]

        if not active_hypotheses:
            return

        # Whether this turn counts toward an ignored prior's stagnation. It is
        # judged forwards — does the turn make that stagnation more evident?
        # When the investigation advanced on something else and passed the
        # prior over, yes. When nothing advanced — the turn only waited on the
        # user, or restated what the case holds — one such turn says nothing
        # new. A run of them does: once the case has stalled (``is_stalled``,
        # the EXHAUSTED time thresholds) the wait is itself the evidence, and
        # the priors must go on aging so the exhaustion handoff, which needs
        # spent hypotheses, can still be reached.
        turn_counts = investigation_advanced or is_stalled(case)
        # A prior the investigation has just asked to test is not being passed
        # over: while a recent, model-authored request it motivated is still
        # outstanding, the turn does not count against it.
        awaited = self._hypotheses_awaiting_recent_evidence(
            case, _ANTI_ANCHORING_COOLDOWN_TURNS
        )

        # 1. Apply confidence decay to stagnant hypotheses
        for h in active_hypotheses:
            # Age-based stagnation sweep (#713): a prior no turn ever touches
            # keeps iterations_without_progress=0, so decay/anchoring would never
            # act on it. Advance the stagnation counter for one that has gone
            # stagnant-by-age (provenance-blind) so an IGNORED prior decays and
            # can trip anchoring the same as a repeatedly-tested one — never
            # validating or concluding, only lowering belief over time. A
            # hypothesis that causal evidence supports is not aged (#1678).
            self.hypothesis_manager.advance_stagnation_if_ignored(
                h,
                case.current_turn,
                case,
                turn_counts=turn_counts and h.hypothesis_id not in awaited,
            )
            # One decay step if THIS turn left the hypothesis stagnant (touched
            # without progress); an untouched turn does not decay it.
            self.hypothesis_manager.apply_likelihood_decay(h, case.current_turn)

        # 2. Detect anchoring and add system feedback if necessary
        is_anchored, reason, hypothesis_ids = self.hypothesis_manager.detect_anchoring(
            active_hypotheses, case.current_turn
        )

        # 3. Age-out: an ignored prior past the stagnation horizon and below the
        # retirement threshold soft-retires. Anti-anchoring retires only on
        # fixation, which a lone stalled prior beside a healthy leader is not, so
        # without this it sat ACTIVE at the decay floor. Whatever anchoring
        # flagged is left to the intervention below, which also tells the LLM to
        # broaden the differential. Same stand-down and root protections as the
        # intervention; runs here, ahead of the intervention's early returns.
        # Only on a turn that counts: a turn that says nothing new about a
        # prior does not end it either.
        if turn_counts and not self._awaiting_recent_evidence(
            case, _ANTI_ANCHORING_COOLDOWN_TURNS
        ):
            flagged = set(hypothesis_ids) if is_anchored else set()
            count_held = support_count_held_root_ids(case)
            for h in active_hypotheses:
                if (
                    h.hypothesis_id not in flagged
                    and not is_chain_root_validated(h, case.causal_nodes)
                    and h.root_node_id not in count_held
                ):
                    self.hypothesis_manager.retire_if_aged_out(
                        h, case, case.current_turn
                    )

        if is_anchored:
            logger.warning(f"Anchoring detected for case {case.case_id}: {reason}")
            # Anti-anchoring intervenes only on a GENUINE stall:
            #  - Stand down while the investigation RECENTLY asked for data that is
            #    still outstanding — it is waiting on the user, not fixated. Bounded
            #    to recent asks so a single stale, never-answered need cannot
            #    permanently disable the mechanism.
            #  - Cooldown: act at most once per `_ANTI_ANCHORING_COOLDOWN_TURNS`,
            #    read from the explicit `last_anti_anchoring_turn` marker so the
            #    cooldown holds even on a turn that happens to retire nothing.
            if self._awaiting_recent_evidence(case, _ANTI_ANCHORING_COOLDOWN_TURNS):
                return
            if (
                case.current_turn - case.progress.last_anti_anchoring_turn
                < _ANTI_ANCHORING_COOLDOWN_TURNS
            ):
                return

            # Engine action (not merely a prompt nudge): retire the STALLED
            # hypotheses the detector flagged so the differential actually
            # diversifies. Exclude any flagged hypothesis whose chain root is
            # validated — it is grounding the cause, and retiring it for "anchoring"
            # would discard the answer. Same protection for a COUNT-HELD root
            # (§7.1/INV-29: really causally supported, blocked only by the
            # independent-support bar) — pre-INV-29 that root would have been
            # VALIDATED and protected; the raised bar must not feed the true
            # cause to the anchoring retirer while it waits for its second
            # observation.
            count_held = support_count_held_root_ids(case)
            targets = [
                hid
                for hid in hypothesis_ids
                if hid in case.hypotheses
                and not is_chain_root_validated(case.hypotheses[hid], case.causal_nodes)
                and case.hypotheses[hid].root_node_id not in count_held
            ]
            retired = self.hypothesis_manager.force_alternative_generation(
                targets, active_hypotheses, case.current_turn, case
            )
            # Record that the intervention fired THIS turn — drives the cooldown
            # regardless of how many hypotheses were eligible to retire.
            case.progress.last_anti_anchoring_turn = case.current_turn

            # Tell the LLM to broaden the differential. State the retirement only
            # when one happened, so the message never claims "retired 0".
            retired_note = (
                f"Retired {len(retired)} stalled hypothesis(es). " if retired else ""
            )
            anchoring_msg = (
                f"CRITICAL: {reason}. {retired_note}Broaden the differential — "
                "propose alternative hypotheses from different root-cause categories."
            )
            current_feedback = metadata.get("system_feedback", "")
            metadata["system_feedback"] = (
                (current_feedback + "\n" + anchoring_msg)
                if current_feedback
                else anchoring_msg
            )

    @staticmethod
    def _awaiting_recent_evidence(case: Case, within_turns: int) -> bool:
        """True if the investigation RECENTLY (within ``within_turns``) asked for
        data that is still outstanding.

        A fresh, still-outstanding ask means the agent is waiting on the user —
        progress, not fixation — so anti-anchoring stands down. Bounding it to
        recent asks ensures a single stale need the user never answers cannot
        permanently disable anti-anchoring for the rest of the case.

        ENGINE-INFERRED needs are excluded (#1079). Those are minted by
        ``evidence_need_linking`` from any EVIDENCE suggestion the model did not
        declare a need for — which, on a fixated case, is most turns. Counting
        them would stamp a fresh ``created_at_turn`` every turn and hold the
        stand-down open forever, destroying the bound the paragraph above
        promises and disabling anti-anchoring exactly when a stuck investigation
        needs it. The signal this reads is the model's DELIBERATE demand, so it
        reads only the needs the model authored.
        """
        return any(
            n.is_outstanding
            and not n.engine_inferred
            and case.current_turn - n.created_at_turn < within_turns
            for n in (case.evidence_needs or [])
        )

    @staticmethod
    def _hypotheses_awaiting_recent_evidence(case: Case, within_turns: int) -> set:
        """Ids of hypotheses that motivate a recent, still-outstanding request
        for data — the per-hypothesis form of ``_awaiting_recent_evidence``, with
        the same bound and the same exclusion of engine-inferred needs."""
        return {
            hypothesis_id
            for n in (case.evidence_needs or [])
            if n.is_outstanding
            and not n.engine_inferred
            and case.current_turn - n.created_at_turn < within_turns
            for hypothesis_id in n.motivating_hypothesis_ids
        }

    def _resolve_id_ref(self, ref: str, created_ids: list[str], prefix: str) -> str:
        """Resolve ``new_index_N`` to the actual ID from ``created_ids``,
        or return ``ref`` unchanged.

        **Contract (load-bearing across all callers — Phase 3 apply-layer
        for hypothesis/evidence refs, Phase 6 for need refs):** callers
        detect unresolved placeholders by checking
        ``ref.startswith("new_index_")`` on the return value. The
        function returns the input unchanged when ``N`` is out of range
        or malformed, never raises — graceful degradation. A "did this
        resolve?" probe at the caller is the canonical pattern; do not
        switch this to ``Optional[str]`` without auditing every caller.

        The ref is normalised first: surrounding whitespace and one pair of
        square brackets are stripped. The prompt renders ids as ``[hyp_...]`` /
        ``[ev_...]`` and a model that echoes the brackets otherwise misses the
        lookup and has its link or update dropped with only a log line
        (#1116 review). Applies to every prefix, real ids and placeholders
        alike.
        """
        ref = _normalise_id_ref(ref)
        if ref and ref.startswith("new_index_"):
            try:
                idx_str = ref.replace("new_index_", "")
                idx = int(idx_str)
                if 0 <= idx < len(created_ids):
                    return created_ids[idx]
            except (ValueError, IndexError):
                pass
        return ref

    def _flatten_follow_ups(
        self,
        follow_ups: list,
        metadata: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Flatten LLM-emitted ``SuggestedFollowUp`` objects into the
        dict shape the API response carries.

        Phase 6 of the evidence-needs rollout: resolves
        ``evidence_need_id`` ``new_index_N`` placeholders against
        ``metadata["evidence_needs_updated"]`` so the wire-level field
        always carries a real ``eneed_xxxxxxxxxxxx`` ID. Unresolvable
        refs are dropped silently (graceful degradation — matches the
        apply-layer pattern for dangling motivator/evidence IDs).
        """
        out: list[dict[str, Any]] = []
        for f in follow_ups:
            suggestion: dict[str, Any] = {
                "label": f.label,
                "action_type": f.action_type,
            }
            if f.payload:
                suggestion["payload"] = f.payload
            if f.body:
                suggestion["body"] = f.body
            if f.hints:
                suggestion["hints"] = f.hints
            if getattr(f, "evidence_need_id", None):
                created_ids = metadata.get("evidence_needs_updated", [])
                resolved = self._resolve_id_ref(
                    f.evidence_need_id,
                    created_ids,
                    "eneed",
                )
                if resolved.startswith("new_index_"):
                    drop_reason = (
                        "missing_metadata"
                        if "evidence_needs_updated" not in metadata
                        else "out_of_range"
                    )
                    logger.warning(
                        f"Dropped unresolvable evidence_need_id "
                        f"{f.evidence_need_id!r} on a SuggestedFollowUp "
                        f"(reason={drop_reason}; "
                        f"evidence_needs_updated len={len(created_ids)})"
                    )
                    try:
                        evidence_need_id_dropped_total.labels(reason=drop_reason).inc()
                    except Exception:
                        pass
                else:
                    suggestion["evidence_need_id"] = resolved
            out.append(suggestion)
        return out


# =============================================================================
# Exceptions
# =============================================================================


class MilestoneEngineError(Exception):
    """Base exception for milestone engine errors.

    Carries an optional ``error_code`` (e.g. ``QUOTA_EXHAUSTED``) so the API
    layer can map the failure to a precise HTTP status and user-facing message
    instead of a generic 500.

    ``category`` relays the provider's typed ``LLMErrorCategory`` (#509) when
    this error was raised on behalf of one. It has to be RELAYED rather than
    inherited from ``__cause__`` because the retry-loop path deliberately does
    NOT chain the provider exception (chaining would put an HTTP 400 on the
    chain and re-route the documented ``TOKEN_LIMIT`` -> 503 to a 502). Without
    it the degrade metric loses its reason label, which is what the folded-in
    provider wording used to supply.
    """

    def __init__(
        self,
        message: str,
        error_code: Optional[str] = None,
        category: Optional[LLMErrorCategory] = None,
    ):
        super().__init__(message)
        self.error_code = error_code
        self.category = category
