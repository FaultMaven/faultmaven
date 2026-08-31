"""Data-Driven and Opportunistic Investigation Engine

This module implements the data-driven investigation system: instead of
rigid phase orchestration, the engine completes milestones
opportunistically based on data availability.

Key Design Principles:
- Process-Agnostic: No rigid phase transitions - milestones complete when data is available
- Opportunistic: Multiple milestones can complete in one turn
- Data-Driven Context: Status-based prompt generation based on available data
- Progress tracked via InvestigationProgress

Design Reference:
- docs/architecture/milestone-based-investigation-framework.md


Architecture:
- Process turn → Generate status-based prompt → Invoke LLM → Process response
- Update milestones based on LLM state_updates
- Track turn progress for analytics
- Automatic status transitions (INVESTIGATING → RESOLVED)
"""

import asyncio
import difflib
import inspect
import json
import logging
import re
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any, Callable, Literal, Optional
from uuid import uuid4

# Module initialization
logger = logging.getLogger(__name__)

from faultmaven.core.investigation.case_telemetry import (
    TELEMETRY_HANDOFF_KEY,
    TurnPath,
    collect_progress_arms,
)
from faultmaven.core.investigation.causal_graph import (
    any_chain_root_inconclusive,
    any_chain_root_validated,
    chain_path_to_problem,
    demote_disconfirmed_cause_via_evidence,
    derive_node_states,
    find_duplicate_hypothesis,
    hypothesis_statements_duplicate,
    ingest_emitted_chain,
    is_chain_root_validated,
    link_llm_rcc_to_cause,
    mece_contested_root_ids,
    mirror_hypothesis_support_to_root_nodes,
    project_hypothesis_states_from_roots,
    prune_abandoned_nodes,
    resolve_orphan_chains,
    retract_disconfirmed_rcc,
    retract_stale_engine_rcc,
    support_count_held_root_ids,
    synthesize_rcc_from_validated_root,
    validate_by_exclusion,
)
from faultmaven.core.investigation.cause_assurance import (
    CauseAssuranceGrade,
    _graph_hooks,
    absence_row_link_refused,
    conclusion_overclaims,
    evidence_datum_key,
    grade_cause_assurance,
    runbook_conversion_ready,
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
    cause_identification_held_mece_total,
    engine_owned_affordance_served_total,
    evidence_need_created_total,
    evidence_need_id_dropped_total,
    evidence_need_status_changed_total,
    evidence_suggestion_unlinked_total,
    hypothesis_dedup_skipped_total,
    hypothesis_root_adoption_refused_total,
    inquiry_handshake_deferred_total,
    inquiry_handshake_recovered_total,
    kb_cause_seed_attempt_total,
    kb_cause_seed_letter_mismatch_total,
    kb_cause_seed_uncorroborated_total,
    narration_overclaim_total,
    pending_action_superseded_stale_total,
    prompt_context_recovery_total,
    solution_offer_superseded_total,
    work_gate_crossed_total,
)
from faultmaven.core.investigation.llm_error_handler import (
    CONTEXT_OVERFLOW_PHRASES,
    LLMErrorHandler,
    OutputTruncationError,
    classify_token_limit_reason,
    is_output_truncation_error,
    is_truncated_json_error,
)
from faultmaven.core.investigation.progress_monitor import (
    ProgressMonitor,
)
from faultmaven.core.investigation.prompts.context_builder import (
    structural_index_is_searchable,
)
from faultmaven.core.investigation.prompts.templates import (
    SCHEMA_INSTRUCTIONS,
    get_prompt_for_case,
)
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
from faultmaven.core.investigation.verification_status import (
    VerificationStatus,
    assess_verification_status,
    is_progress_stalled,
    is_stalled,
    restatement_hold_governs,
    work_gate_passed,
)
from faultmaven.core.investigation.working_conclusion_generator import (
    calculate_progress_metrics,
    generate_working_conclusion,
    is_early_stage_conclusion,
)
from faultmaven.exceptions import TOKEN_LIMIT
from faultmaven.infrastructure.llm.metering import (
    TurnTokenTracker,
    active_token_tracker,
    record_provider_call,
)
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
    TERMINAL_HYPOTHESIS_STATES,
    ActionAttempt,
    Case,
    CaseAction,
    CaseState,
    CauseState,
    ConfidenceLevel,
    Evidence,
    EvidenceCategory,
    EvidenceNeed,
    EvidenceSourceType,
    HypothesisState,
    InterventionQuadrant,
    InvestigationActionType,
    InvestigationMomentum,
    InvestigationProgress,
    InvestigationStage,
    JournalEntry,
    KnowledgeMatch,
    KnowledgeResolution,
    MitigationRecord,
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
    SolutionState,
    SolutionType,
    TemporalState,
    TurnOutcome,
    TurnProgress,
    UrgencyLevel,
)
from faultmaven.modules.case.exceptions import StaleCaseException
from faultmaven.modules.knowledge.contracts import IKnowledgeService

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
# several of the top-ranked slots. The KB cause seeder's parent-runbook dedup
# needs diversity ACROSS chunks to see more than one distinct runbook, so the
# fetch depth is deeper than the prompt surface: fetch KB_PREFETCH_FETCH_LIMIT
# chunks (the seeder's parent-dedup consumes the full ranked list), but render
# only the top KB_CONTEXT_MAX_ENTRIES into `case.kb_context`. Because results are
# score-ranked, the rendered top slice is byte-identical to the old limit-3 fetch,
# so the prompt the LLM sees does not change.
KB_PREFETCH_FETCH_LIMIT = 10
KB_CONTEXT_MAX_ENTRIES = 3

# Distinct chunks of ONE runbook that must appear in the relevance-filtered
# retrieval set before any of that runbook's causes may be seeded as a candidate
# root cause (#1144). Corroboration, not rank.
#
# Seeding asserts "this may be why your system is broken". Retrieval score
# supports only "this text is semantically nearby", and the gap between the two
# is where #1144 lives: a page-captured, symptom-vague problem statement seeded
# an NGINX-502 chain and a MongoDB WiredTiger chain into a Kubernetes OOMKilled
# case, then carried the NGINX text into the case header as the working
# conclusion.
#
# The obvious guards do not work, and were measured rather than assumed
# (41 candidate seeds over 24 problem statements against the shipped pack):
#
#   * A minimum SCORE floor cannot separate them. On-domain seeds scored
#     0.603-0.731 and off-domain ones 0.519-0.715 — overlapping, because the
#     score tracks how much concrete text the QUERY carries far more than how
#     well the runbook fits. A floor at 0.66 (needed to drop most junk) also
#     dropped 8 of 14 correct seeds.
#   * Requiring the runbook to also appear in the turn's kb_context/Sources
#     does almost nothing: it keeps 19 of the 27 off-domain seeds, which were
#     ALREADY in the top-3 kb_context — kb_context is the top slice of the very
#     same ranking, so it cannot cross-check a ranking against itself.
#
# What does separate them is BREADTH OF MATCH WITHIN ONE DOCUMENT. A runbook
# that genuinely covers the failure matches on several of its sections at once
# (symptom recognition, a cause, diagnostic steps); an off-domain runbook
# matches exactly one paragraph, by lexical coincidence. At >=2 chunks the same
# measurement kept 13 of 14 on-domain seeds while dropping 21 of 27 off-domain
# ones — and of the six survivors, four were runbooks a reader would call
# defensible for the query asked.
#
# The bar is relative to the document's own length (see _chunks_required): a
# runbook cannot corroborate itself beyond its chunk count, so a document that IS
# one chunk corroborates itself. Read as a flat minimum it would exclude compact
# documents entirely — and those are the flywheel's own output, which is the one
# population this must not exclude. The threshold is meaningful only relative to
# KB_PREFETCH_FETCH_LIMIT — a shallower fetch would tighten it silently — so the
# two are pinned together by test.
# ``kb_cause_seed_uncorroborated_total`` counts what it declines, which is how
# the number gets re-sized on evidence rather than on this comment. The
# measurement itself is re-runnable:
# ``tests/eval/kb_cause_seeder/run_corroboration_eval.py``.
KB_SEED_MIN_CORROBORATING_CHUNKS = 2

# Cosine floor a pre-fetched runbook must clear to enter `case.kb_context` (and
# to reach the KB cause seeder). Same scale, corpus and calibration as
# ``UnifiedKBConfig.relevance_threshold`` — this path reads the identical
# ``KnowledgeVectorStore.search`` score, so the two must move together; see that
# docstring for the measured distribution the number comes from.
#
# This was 0.3 and shared the #1072 defect: the score it filters was not cosine
# but ``2*cos - 1``, making it a cosine floor of 0.65 that silently dropped
# on-topic runbooks. Quieter than the QA-tool symptom the issue was opened on —
# nothing is logged and no message reaches the model, the prefetched context is
# simply thinner than it should be — and on this path that starves both
# symptom-verification context and the cause seeder.
KB_PREFETCH_RELEVANCE_THRESHOLD = 0.5

# Generation cap for schema-bound calls, and the ceiling the truncation ladder
# may raise it to. Investigation schemas (``_Verification`` especially) are
# large and turn 2+ carries substantial context, so the starting cap is
# generous; the ceiling bounds how much a single turn may spend chasing an
# answer that keeps overrunning. Reaching the ceiling is the signal to switch
# levers — from "give the answer more room" to "give the answer more room by
# shrinking the question" (the #662 minimal-prompt degrade).
STRUCTURED_OUTPUT_MAX_TOKENS = 8000
STRUCTURED_OUTPUT_MAX_TOKENS_CEILING = 16000


def _matches_gate_token(msg: str, tokens: list[str]) -> bool:
    """Word-boundary prefix match for typed gate answers.

    Bare ``startswith`` also matched words that merely share the prefix —
    "note db latency spiked…" read as "no", "yesterday the pod restarted…"
    as "yes" — turning evidence-bearing messages into gate answers.
    Requiring a word boundary after the token keeps the intended matches
    ("no", "no.", "nope!", "yes, it's resolved") while rejecting the
    prefix-sharing words. ``msg`` must already be stripped/lowercased.
    """
    return any(re.match(rf"{re.escape(t)}\b", msg) for t in tokens)


CATEGORY_MILESTONE_MAP = {
    EvidenceCategory.SYMPTOM_EVIDENCE: [
        "symptom_verified",  # Confirms problem exists
    ],
    EvidenceCategory.CAUSAL_EVIDENCE: [
        # "root_cause_identified" is NOT here (#675 / INV-35): identification is
        # engine-derived from the validated causal chain (cause_state), not an
        # LLM-claimed milestone. The map's only consumer, _infer_milestones,
        # intersects category-eligible names with this turn's MilestoneUpdates —
        # and MilestoneUpdates no longer carries root_cause_identified, so the
        # entry could never be attributed (it attributed to nothing). Causal
        # evidence's contribution to identification flows through the chain
        # derivation, not this attribution map.
        "solution_proposed",  # Justifies proposed solution
    ],
    # Absence categories map to [] DELIBERATELY (not an oversight). The
    # verification gates (mitigation_verified / solution_verified) are set by
    # the LLM via the User-Agent Handshake / compliance detection — NOT by
    # evidence category. The map's only consumer (_infer_milestones) does
    # *attribution* (intersect category-eligible milestones with what the LLM
    # completed this turn), and these gates are not evidence-attributed.
    # The absence rows' disposition role is read DIRECTLY by the readiness
    # checks: assess_resolution_readiness/_closure consult _has_causal_absence()
    # to decide RESOLVED vs CLOSED. So absence evidence drives dispositions
    # through readiness, not through this map — keep these at [].
    EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE: [],
    EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE: [],
    # Baseline/environmental data lives on ``uploaded_files``, not Evidence;
    # Evidence rows are only created when the agent extracts a
    # claim-relevant slice.
}


def _case_has_symptom_evidence(case: Case) -> bool:
    """Return True if the case has at least one SYMPTOM_EVIDENCE row.

    Backstop for Behavioral Rule 2 applied to MITIGATION ProposedActions:
    a mitigation must target an observed failure (recorded as
    SYMPTOM_EVIDENCE), not an unverified user claim. Used at the
    ProposedAction-creation site to gate MITIGATION → DIAGNOSTIC
    downgrades. Pure over case state; no side effects.
    """
    return any(e.category == EvidenceCategory.SYMPTOM_EVIDENCE for e in case.evidence)


def _has_searchable_material(case: Case) -> bool:
    """True when the case holds content the ``search_file`` tool can target.

    Two sources: any Evidence row (existing claim-anchored evidence), or any
    uploaded file with a non-trivial structural index. Post-010, a fresh
    upload creates an ``UploadedFile`` (not an Evidence row until the LLM
    extracts a slice), so on the evidence-*delivering* turn the searchable
    material lives on ``uploaded_files`` — ``bool(case.evidence)`` alone
    would be False and wrongly leave ``tool_choice=auto`` (#708). The
    searchability test is the context builder's own
    ``structural_index_is_searchable`` (single source of truth for the
    ``searchable="true"`` threshold), so a forced-DA turn is guaranteed a
    real search target and the tool loop cannot crash for lack of one.
    """
    if getattr(case, "evidence", None):
        return True
    return any(
        structural_index_is_searchable(uf.structural_index)
        for uf in getattr(case, "uploaded_files", None) or []
    )


def _should_force_tools(
    processing_mode: Optional[str], case: Case, has_pending: bool
) -> bool:
    """Decide ``tool_choice=required`` for a generation turn.

    Force Directed-Analysis tools only when all three hold: (a) the turn is
    classified ``directed_analysis``, (b) the case has searchable material for
    the tool to target, and (c) the user is not mid-confirmation (a
    ``pending_transition`` turn is a typed confirm/decline with nothing to
    search — forcing tools would crash the loop). This is the linchpin of
    #708: a fresh evidence-bearing upload reroutes to ``directed_analysis``
    AND satisfies ``_has_searchable_material`` via its UploadedFile, so tools
    are forced on the delivering turn instead of leaving it on
    ``tool_choice=auto`` where the agent can skip analysis.
    """
    return (
        processing_mode == "directed_analysis"
        and _has_searchable_material(case)
        and not has_pending
    )


def _solution_cause_validated(case: Case) -> bool:
    """M5 gate predicate: is the cause established enough to register a SOLUTION?

    Delegates to the **same** "cause established" predicate the terminal /
    resolution gate uses (``terminal_transitions._cause_identified``):
    ``cause_state == IDENTIFIED`` **or** a set ``RootCauseConclusion`` **or** a
    ``working_conclusion`` at ≥ 0.6. This shared predicate is load-bearing for
    two reasons:

    1. **Consistency — no deadlock.** M5 must never be *stricter* than the gate
       that lets a case RESOLVE. The resolution gate accepts the RCC / working-
       conclusion backstop because ``cause_state`` is a SOFT, under-reporting
       signal (see ``_recompute_cause_state_from_chain``). Keying M5 on the raw
       ``cause_state == IDENTIFIED`` alone would block a permanent fix on a case
       the engine would otherwise let the user resolve — the engine refusing to
       register the very fix that resolves the case.
    2. **Same-turn correctness — no false stall.** This turn's ``cause_state`` is
       recomputed only at the END of ``_apply_investigation_updates`` (after
       chain emission), *after* this gate runs, so reading ``cause_state`` here
       yields the PRIOR turn's value. The ``RootCauseConclusion`` is applied
       early in the same method (before this gate), so on the opportunistic
       same-turn "validate the root AND propose the fix" path the RCC branch of
       ``_cause_identified`` correctly sees this turn's grounding.

    A premature SOLUTION (cause not established by any signal) is downgraded to
    DIAGNOSTIC — flow continues; the LLM grounds the root or proposes a
    mitigation. Mitigation (WORKAROUND) is exempt.

    Scope (deferred): the methodology also exempts ``defensive_fix`` (permanent @
    intermediate), but the solution emission carries no ``InterventionQuadrant``,
    so the engine cannot distinguish it from a remediation — per-quadrant
    precision waits until the emission carries a quadrant. Pure; no side effects.
    """
    # Local import mirrors the module's other terminal_transitions uses (avoids
    # an import cycle) and keeps M5 and the resolution gate on ONE predicate.
    from faultmaven.core.investigation.terminal_transitions import _cause_identified

    return _cause_identified(case)


def _coerce_intervention_quadrant(raw: object) -> Optional[InterventionQuadrant]:
    """Honor-or-reject a solution emission's ``quadrant`` string (R9).

    The LLM may tag a ``SolutionToAdd`` with the intervention quadrant of a
    surfaced runbook candidate. Coerce the free-text value to the enum; a missing
    or unrecognized value yields ``None`` (recorded as unquadranted) rather than a
    hard parse failure — BEST_EFFORT providers must not crash the turn on a typo.
    Recorded as DATA only: M5's downgrade logic is unchanged (per-quadrant
    exemptions are a separate, soundness-sensitive decision).
    """
    if not raw:
        return None
    try:
        return InterventionQuadrant(str(raw).strip().lower())
    except ValueError:
        return None


def _determine_action_type(
    case: Case, solution_type: SolutionType
) -> InvestigationActionType:
    """
    Determine whether a proposed solution is a MITIGATION or SOLUTION action.

    Used when creating ProposedAction from SolutionToAdd. The action_type
    determines which stage-gate behavior follows:
    - MITIGATION → mitigation insert (Mitigating)
    - SOLUTION → solution_accepted → enters TREATMENT stage

    Logic:
    1. WORKAROUND solution_type → MITIGATION (explicitly temporary)
    2. Otherwise → SOLUTION

    There is no prospective path fork (redesign R5). A mitigation is an
    opportunistic insert driven by the prompt, surfaced via a WORKAROUND
    solution_type.
    """
    if solution_type == SolutionType.WORKAROUND:
        return InvestigationActionType.MITIGATION

    return InvestigationActionType.SOLUTION


# ProposedAction states that count as a LIVE offer for the solution_proposed
def _supersede_pending_solution_offers(
    case: Case, *, reason: Literal["reproposal", "license_lost"]
) -> tuple[int, int | None]:
    """Mark every PENDING SOLUTION offer superseded; return (count, newest_turn).

    ``newest_turn`` is the greatest ``proposed_in_turn`` among the offers just
    superseded (None when count is 0) — the withdrawal path needs it as the
    INV-33 shadow cutoff, and this single pass already visits exactly those
    actions and reads their turn before flipping state, so it is returned here
    rather than recomputed in a duplicate pre-pass.

    Only pending offers are touched: an ACCEPTED offer records that the user
    executed the fix — a fact supersession cannot unmake (its truth surface
    is the M6 failed-fix machinery, not offer liveness). MITIGATION and
    DIAGNOSTIC actions are out of scope (they never fed ``solution_proposed``
    and mitigations are not licensed by an established cause). ``reason`` is
    a closed vocabulary (typed here AND on the model field) because it feeds
    the ``solution_offer_superseded_total`` metric label — a free-form string
    would grow label cardinality silently.
    """
    count = 0
    newest_turn: int | None = None
    for action in case.proposed_actions:
        if (
            action.action_type == InvestigationActionType.SOLUTION
            and action.state == "pending"
        ):
            if newest_turn is None or action.proposed_in_turn > newest_turn:
                newest_turn = action.proposed_in_turn
            action.state = "superseded"
            action.superseded_reason = reason
            action.superseded_in_turn = case.current_turn
            solution_offer_superseded_total.labels(reason=reason).inc()
            count += 1
    return count, newest_turn


def _retire_shadowed_diagnostic_asks(case: Case, *, before_turn: int) -> int:
    """Retire stale DIAGNOSTIC pending asks a SOLUTION offer shadowed (INV-33).

    The ``<pending_action>`` render (context_builder: newest pending action of
    ANY type) shows one action at a time — a SOLUTION offer, while it stands,
    shadows any EARLIER ask beneath it. When that offer LEAVES pending state —
    WITHDRAWN on license loss (the cause fell, the case is back in active
    diagnosis) or ACCEPTED (the case moves to TREATMENT to verify the executed
    fix) — the render falls through to a shadowed ask and resurfaces it as the
    current compliance target, an ask the investigation already moved past.
    Retire the shadowed DIAGNOSTIC asks (pending, proposed STRICTLY BEFORE
    ``before_turn`` = the offer's turn) so compliance detection reads a clean
    slate. Asks proposed in the offer's OWN turn or AFTER are a live/reopening
    thread — the de-absolutized Zone-3 prompt (INV-33) now invites a parallel
    diagnostic when the user reopens the thread — and stand (strict ``<``, the
    same-turn create-then-withdraw edge preserves the reopened ask).

    DIAGNOSTIC-ONLY by design. A DIAGNOSTIC ask has no compliance gate (it never
    transitions to ``accepted``), so retiring one is a pure display cleanup with
    zero functional loss — the stale pre-fix evidence request is exactly what
    goes obsolete once the fix is on the table. A pending MITIGATION is NOT
    retired: a workaround is cause-INDEPENDENT symptom relief the user may still
    execute, so its liveness survives (INV-32) and its reappearance as the top
    pending ask is correct, not stale.
    """
    count = 0
    for action in case.proposed_actions:
        if (
            action.action_type == InvestigationActionType.DIAGNOSTIC
            and action.state == "pending"
            and action.proposed_in_turn < before_turn
        ):
            action.state = "superseded"
            action.superseded_reason = "stale_pending"
            action.superseded_in_turn = case.current_turn
            pending_action_superseded_stale_total.inc()
            count += 1
    return count


def _withdraw_unlicensed_solution_offers(
    case: Case, metadata: dict[str, Any] | None = None
) -> int:
    """Re-check the M5 license on standing PENDING solution offers (INV-32).

    A SOLUTION offer is admitted only while a cause is established — the M5
    creation gate, ``_solution_cause_validated``, called here VERBATIM so the
    creation gate, this liveness re-check, and the deferred-close gate can
    never diverge on what "established" means. Re-checked at recompute time,
    after this turn's demotions/retractions have settled: when the license
    has fallen (M6 failed-fix demotion, conclusion retraction, MECE hold, or
    the working-conclusion proxy dropping below its bar — e.g. stagnation
    decay), the pending offer is WITHDRAWN — superseded, out of the
    ``<pending_action>`` context block and the ``solution_proposed``
    derivation — rather than kept standing as "awaiting execution" for a
    cause the engine no longer asserts (#656 DF-3: the frame latch).

    The withdrawal is surfaced to the LLM via ``system_feedback`` so the next
    turn re-grounds the cause (or proposes a WORKAROUND mitigation) instead of
    referencing a proposal the user can no longer see as pending. The notice
    is PREPENDED: the turn record truncates feedback head-first, and on messy
    turns (exactly when withdrawals happen) earlier accumulators can push a
    tail-appended notice past the cap. The notice deliberately does NOT name
    a mechanism — the license can fall to a demotion, a retraction, a MECE
    hold, or plain confidence decay on the working-conclusion proxy, and the
    engine cannot always tell which from here.

    Known timing edges (documented, accepted):

    - The ``working_conclusion`` proxy leg reads the PREVIOUS turn's
      conclusion (regeneration runs after this recompute), so a license
      resting solely on it clears on the FOLLOWING turn's recompute —
      one-turn lag. The prompt frame still exits same-turn regardless: the
      Zone-3-pending conjunction requires ``cause_state == IDENTIFIED``,
      which the demotion drops in this same recompute. cause_state / RCC /
      contest falls withdraw same-turn.
    - The reverse composition — M5 admits on the PRIOR turn's truth, this
      re-check reads the settled truth — means an offer emitted in the very
      turn that knocks its cause down is admitted then withdrawn SAME TURN
      (pinned; the engine must not end a turn presenting a fix for a cause
      it no longer asserts). The assistant's already-delivered prose may
      still describe that fix for one turn; the next turn's context carries
      this notice and no ``<pending_action>`` block.
    - A standing LLM-authored RootCauseConclusion keeps the license through a
      MECE hold (trust boundary — the engine withholds only its OWN
      assertions; LLM-conclusion retraction is the follow-up tracked on
      #656), so the MECE trigger withdraws only licenses resting on
      cause_state / the engine mirror / the working-conclusion proxy.
    """
    if _solution_cause_validated(case):
        return 0
    count, withdrawn_cutoff = _supersede_pending_solution_offers(
        case, reason="license_lost"
    )
    if not count:
        return 0
    # INV-33: retire the DIAGNOSTIC asks the withdrawn offer shadowed, so the
    # <pending_action> render cannot resurface a stale earlier ask now that the
    # SOLUTION on top of it is gone. count>0 ⇒ withdrawn_cutoff is a real turn.
    _retire_shadowed_diagnostic_asks(case, before_turn=withdrawn_cutoff)
    logger.warning(
        f"Withdrew {count} pending SOLUTION offer(s) for case {case.case_id}: "
        f"the established-cause license fell "
        f"(cause_state={case.progress.cause_state.value}, "
        f"rcc={'set' if case.root_cause_conclusion else 'none'})",
        extra={
            "event": "solution_offer_withdrawn",
            "case_id": case.case_id,
            "turn": case.current_turn,
            "withdrawn": count,
        },
    )
    if metadata is not None:
        _add_system_feedback(
            metadata,
            "SYSTEM: Your pending SOLUTION proposal was withdrawn because "
            "the root cause it targeted is no longer established. Re-ground "
            "the root cause with evidence before re-proposing a permanent "
            "fix, or propose a temporary mitigation (WORKAROUND) instead.",
            # Prepend (see docstring): truncation keeps the head.
            prepend=True,
        )
    return count


def _add_system_feedback(
    metadata: dict[str, Any], message: str, *, prepend: bool = False
) -> None:
    """Accumulate a system notice for the next turn's prompt context.

    ``prepend`` puts the notice at the HEAD: the turn record truncates
    feedback head-first, so a notice that must survive a messy turn (many
    accumulators active) goes in front. Default is chronological append.
    """
    current = metadata.get("system_feedback", "") or ""
    parts = (message, current) if prepend else (current, message)
    metadata["system_feedback"] = "\n".join(p for p in parts if p).strip()


# Gate signal → the ProposedAction type it may register against (INV-32
# type-matched compliance). ONE table consumed by both the guards in
# ``_apply_stage_gate_signals`` and the acceptance targeting in
# ``_apply_stage_gate_side_effects`` — the mapping previously lived in three
# hand-written sites, and a gate added to one but not another recreates the
# any-type misattribution this table exists to prevent. ``mitigation_verified``
# is deliberately absent: verification accepts no action (its action left
# pending at the accept step).
_GATE_ACTION_TYPE: dict[str, InvestigationActionType] = {
    "solution_accepted": InvestigationActionType.SOLUTION,
    "mitigation_accepted": InvestigationActionType.MITIGATION,
}


def _apply_stage_gate_side_effects(
    case: Case,
    completed_gates: set[str],
    user_message: str,
    metadata: dict[str, Any],
) -> None:
    """Apply side effects when stage-gate milestones are completed.

    When the LLM sets a stage-gate milestone, we:
    1. Mark the pending ProposedAction OF THE GATE'S TYPE as "accepted" —
       ``solution_accepted`` accepts the most recent pending SOLUTION,
       the mitigation-accept signal the most recent pending MITIGATION.
       Type-matched targeting (INV-32 hardening): the old any-type pick
       could stamp "accepted" on a never-executed SOLUTION when the user
       reported a mitigation (or vice versa), and an accepted offer is a
       PERMANENT liveness source for the derived ``solution_proposed`` —
       a misattributed accept re-latches the frame the withdrawal
       machinery exists to dissolve. ``mitigation_verified`` alone
       accepts nothing (its action was accepted at the accept step).
    2. Create an ActionAttempt audit record per accepted action.
    3. Handle mitigation-verified side effects (3B propose-close).

    This replaces the old compliance_detector.py logic — the LLM now
    detects compliance per Framework §4.1.
    """
    target_types = [
        action_type
        for gate, action_type in _GATE_ACTION_TYPE.items()
        if gate in completed_gates
    ]

    for target_type in target_types:
        pending_action = None
        for action in reversed(case.proposed_actions):
            if action.state == "pending" and action.action_type == target_type:
                pending_action = action
                break
        if pending_action is None:
            continue
        pending_action.state = "accepted"
        # #987: stamp WHEN the user executed it. `proposed_in_turn` is the OFFER
        # turn; anything reasoning about what happened *after the fix* (M6's
        # persistence precondition) must key on execution, or evidence from the
        # offering turn — recorded before the fix was ever run — reads as a
        # post-fix outcome.
        pending_action.accepted_in_turn = case.current_turn
        # INV-33: a SOLUTION acceptance moves the case to TREATMENT (verify the
        # executed fix). Retire the DIAGNOSTIC asks the offer shadowed so an
        # earlier pre-fix ask cannot resurface in <pending_action> once the
        # accepted SOLUTION (now state="accepted", no longer "pending") stops
        # covering it — the symmetric twin of the withdrawal-path retirement.
        # SOLUTION-scoped, NOT mitigation: accepting a SOLUTION moves the case to
        # TREATMENT (diagnosis is done, pre-fix asks are stale), but accepting a
        # MITIGATION keeps it in active diagnosis where a shadowed DIAGNOSTIC is
        # plausibly still live — retiring it there could drop a real ask. A
        # genuinely-stale accumulation under a mitigation falls under the general
        # "DIAGNOSTIC asks carry no lifecycle" boundary INV-33 leaves standing.
        if target_type == InvestigationActionType.SOLUTION:
            _retire_shadowed_diagnostic_asks(
                case, before_turn=pending_action.proposed_in_turn
            )
        # Create audit trail
        attempt = ActionAttempt(
            action_id=pending_action.action_id,
            user_message=user_message[:10000],
            submitted_at=datetime.now(UTC),
            compliance_detected=True,
            compliance_confidence=1.0,  # LLM-detected = full confidence
        )
        case.action_attempts.append(attempt)
        logger.info(
            f"Stage-gate milestone(s) {completed_gates} set by LLM for case "
            f"{case.case_id} (action {pending_action.action_id}, "
            f"type={pending_action.action_type.value})"
        )

    # 3B: Mitigation-verified side effects (optional propose-close).
    #
    # Redesign R5: there is no post-mitigation path choice. After a
    # mitigation verifies, the case simply continues opportunistically.
    # The pre-mitigation evidence boundary is carried by
    # ``progress.mitigation.completed_at_turn`` (set in the apply-loop where
    # the record is materialized).
    if "mitigation_verified" in completed_gates:
        # rca_infeasible advisory signal: propose closure as stabilized rather
        # than push RCA on a problem the LLM has flagged as intractable.
        # Reference: investigation-lifecycle-logic.md §2.4.
        rca_infeasible = case.problem_verification and getattr(
            case.problem_verification, "rca_infeasible", False
        )
        # Don't clobber an in-flight disposition handshake, and never on a
        # terminal case (symmetric with _maybe_propose_deferred_close).
        if (
            rca_infeasible
            and not getattr(case, "pending_transition", None)
            and not case.is_terminal
        ):
            # The generic fallback is fine for the user-facing sentence but is
            # NOT a rationale: derive_closure_reason's guard requires a real one
            # (the label forecloses future work, so it must carry its own
            # justification). Keep them apart so the log cannot claim a reason
            # the guard will refuse.
            declared_rationale = getattr(
                case.problem_verification, "rca_infeasible_rationale", None
            )
            rationale = (
                declared_rationale
                or "root cause analysis is not feasible for this problem"
            )
            closure_message = (
                "The mitigation is verified and stable. "
                f"Since {rationale}, shall we close this case as stabilized?"
            )
            from faultmaven.core.investigation.terminal_transitions import (
                propose_transition,
            )

            propose_transition(
                case=case,
                to_state="closed",
                summary=closure_message,
            )
            # Unified same-turn proposal flag: keeps step 0 of
            # _check_automatic_transitions from confirming this close with
            # the very message that produced it (#722 same-turn-confirmation
            # guard) — the mitigation-verified message that triggered this
            # proposal often pattern-matches as a bare "yes".
            metadata["transition_proposed_this_turn"] = True
            metadata["override_suggestions"] = _close_confirmation_suggestions()
            metadata["rca_infeasible_closure_message"] = closure_message
            # Read the reason propose_transition just STORED rather than
            # re-deriving it here. Mirroring the derivation meant reproducing 2
            # of its 5 branches, which diverges whenever another branch wins —
            # an rca_infeasible declaration alongside a standing working
            # conclusion and a solution record derives `solution_deferred`,
            # which a two-branch mirror cannot express.
            stored_reason = (getattr(case, "pending_transition", None) or {}).get(
                "closure_reason"
            )
            logger.info(
                f"Proposed CLOSED transition for case {case.case_id} "
                f"(rca_infeasible=True; closure_reason derived as "
                f"{stored_reason}, rationale: {rationale})"
            )

    metadata["compliance_detected"] = True
    metadata["progress_made"] = True


def _apply_stage_gate_signals(
    case: Case,
    m: Any,
    user_message: str,
    metadata: dict[str, Any],
) -> None:
    """Apply stage-gate compliance signals (Framework §4.1).

    Runs AFTER the solutions step of ``_apply_investigation_updates`` so the
    guards see ProposedActions created THIS turn: the prompt's KB-resolution
    flow mandates SolutionToAdd + ``solution_accepted`` in ONE response, and
    evaluating these signals before the solutions step deterministically
    rejected that bundle (the guard read a pre-solutions snapshot) while
    telling the LLM to re-propose what it had just proposed.

    Idempotency FIRST, guards second: a re-emitted, already-registered signal
    is absorbed silently — LLMs re-assert standing booleans, and rejecting
    the re-emission produced false "was not registered" feedback that invited
    a redundant re-proposal.

    Type-matched guards (INV-32, via ``_GATE_ACTION_TYPE``): a gate registers
    only against a pending action of ITS type — the old any-type check let
    ``solution_accepted`` pass against a lone pending DIAGNOSTIC and
    manufacture a permanent accepted-ladder latch, and let a mitigation
    report register against a 3C/3D-downgraded DIAGNOSTIC, defeating the
    downgrade contract (the gate prevents REGISTERING, not the action
    happening in the user's environment). Rejections surface via
    ``system_feedback``. When the mitigation ACCEPT signal is rejected,
    same-turn VERIFY is suppressed under the SAME notice — processing it
    would fire the ordering guard's "set BOTH in the same response" advice,
    directly contradicting the rejection it accompanies.
    """
    p = case.progress

    def _has_pending(action_type: InvestigationActionType) -> bool:
        return any(
            a.state == "pending" and a.action_type == action_type
            for a in case.proposed_actions
        )

    # --- solution_accepted --------------------------------------------
    if getattr(m, "solution_accepted", False):
        if p.solution_accepted:
            pass  # already registered — idempotent re-emission, silent
        elif not _has_pending(_GATE_ACTION_TYPE["solution_accepted"]):
            logger.warning(
                f"Rejected stage-gate milestone 'solution_accepted' for case "
                f"{case.case_id}: no pending SOLUTION ProposedAction exists"
            )
            _add_system_feedback(
                metadata,
                "SYSTEM: 'solution_accepted' was not registered — no "
                "SOLUTION proposal is currently pending (it may have been "
                "withdrawn or superseded since it was made). If the user "
                "executed a fix and the root cause stands established, "
                "re-propose the fix as a SolutionToAdd this turn and set the "
                "milestone when the user confirms against the standing "
                "proposal; if the problem is already resolved, record the "
                "confirming causal_absence evidence instead.",
            )
        else:
            p.solution_accepted = True
            metadata["milestones_completed"].append("solution_accepted")

    # --- mitigation signals (redesign R2: materialize progress.mitigation;
    # the milestone NAMEs still enter milestones_completed for telemetry) ---
    stab_accepted_signal = bool(getattr(m, "mitigation_accepted", False))
    stab_verified_signal = bool(getattr(m, "mitigation_verified", False))

    if stab_accepted_signal and p.mitigation is not None and p.mitigation.accepted:
        # Already registered. Single-mitigation model (INV-24): a SECOND
        # workaround's execution cannot re-enter the gate ladder. Surface
        # that when a pending MITIGATION stands (the rendered
        # MILESTONE_TO_SET affordance is dead for it — a silent drop leaves
        # the LLM re-emitting forever); a bare re-emission with nothing
        # pending is absorbed silently.
        stab_accepted_signal = False
        if _has_pending(InvestigationActionType.MITIGATION):
            _add_system_feedback(
                metadata,
                "SYSTEM: 'mitigation_accepted' was not re-registered — the "
                "case's single mitigation record already registered "
                "acceptance (no mitigation re-entry, INV-24). Treat the "
                "executed workaround as part of the standing mitigation: "
                "record its outcome as evidence, and set "
                "mitigation_verified only when the situation is confirmed "
                "stable.",
            )
    elif stab_accepted_signal and not _has_pending(InvestigationActionType.MITIGATION):
        logger.warning(
            f"Rejected mitigation_accepted for case {case.case_id}: "
            f"no pending MITIGATION ProposedAction exists"
        )
        notice = (
            "SYSTEM: 'mitigation_accepted' was not registered — no "
            "MITIGATION proposal is currently pending. A mitigation "
            "registers only against a standing WORKAROUND proposal grounded "
            "in symptom evidence; file the symptom evidence and re-propose "
            "the workaround (SolutionToAdd, solution_type=WORKAROUND) "
            "before setting the milestone."
        )
        stab_accepted_signal = False
        if stab_verified_signal and (p.mitigation is None or not p.mitigation.accepted):
            # Same-turn verify presupposes the acceptance just rejected —
            # suppress it under the SAME notice rather than letting the
            # ordering guard below add contradictory retry advice.
            stab_verified_signal = False
            notice += (
                " The same applies to 'mitigation_verified' emitted this "
                "turn — it presupposes the acceptance that was not "
                "registered."
            )
        _add_system_feedback(metadata, notice)

    if stab_accepted_signal:
        if p.mitigation is None:
            # proposed_at_turn = the turn the latest workaround
            # ProposedAction was proposed, else current_turn.
            proposed_turn = case.current_turn
            for action in reversed(case.proposed_actions):
                if action.action_type == InvestigationActionType.MITIGATION:
                    proposed_turn = action.proposed_in_turn
                    break
            p.mitigation = MitigationRecord(proposed_at_turn=proposed_turn)
        if not p.mitigation.accepted:
            p.mitigation.accepted = True
            metadata["milestones_completed"].append("mitigation_accepted")

    if stab_verified_signal:
        # Ordering guard: verification presupposes acceptance. If accept
        # wasn't signalled (now or earlier), reject and surface via
        # system_feedback for retry, instead of crashing the turn on the
        # record validator.
        if p.mitigation is None or not p.mitigation.accepted:
            logger.warning(
                f"Rejected mitigation 'mitigation_verified' for case "
                f"{case.case_id}: prerequisite 'mitigation_accepted' is "
                f"not set (state-machine ordering)."
            )
            _add_system_feedback(
                metadata,
                "MILESTONE ORDER ERROR: You set mitigation_verified=True "
                "without first setting mitigation_accepted=True. "
                "Verification presupposes acceptance — set "
                "mitigation_accepted=True (based on the user's confirmation "
                "signals) before mitigation_verified=True. Set BOTH "
                "milestones in the same response if both happened this "
                "turn, OR set mitigation_accepted=True first and verify on "
                "a follow-up turn after the user confirms.",
            )
            metadata.setdefault("validation_repairs", []).append(
                "Rejected mitigation_verified "
                "(prerequisite mitigation_accepted not set)"
            )
        elif not p.mitigation.verified:
            p.mitigation.verified = True
            p.mitigation.completed_at_turn = case.current_turn
            metadata["milestones_completed"].append("mitigation_verified")

    # --- side effects (Framework §4.1): mark the type-matched pending
    # ProposedAction accepted + ActionAttempt audit ----------------------
    stage_gate_completed = {
        "mitigation_accepted",
        "mitigation_verified",
        "solution_accepted",
    } & set(metadata["milestones_completed"])
    if stage_gate_completed:
        _apply_stage_gate_side_effects(
            case, stage_gate_completed, user_message, metadata
        )


def _is_context_length_error(exc: Exception) -> bool:
    """True if *exc* is a provider context-length / prompt-too-long rejection.

    Provider-agnostic: the gateway may enforce a smaller window than our registry
    estimate (proxy/aggregator/reduced-context serving). We classify ONLY on
    length-specific phrases — deliberately NOT on a bare ``400 + "token"`` or the
    generic Pydantic phrase ``"string too long"``, which fire on ordinary
    request-validation errors and would trigger needless fallback retries.

    Two shapes reach here. A **raw provider exception** (proxy/aggregator path)
    carries the overflow wording in its message. The **retry-loop path**
    (``with_retry`` → ``handle_error`` classifies the overflow as
    ``COMPRESS_MEMORY`` → ``_generate_structured_output_inner`` re-raises a
    ``MilestoneEngineError``) has already consumed the provider's wording, but it
    stamps the shared ``TOKEN_LIMIT`` error_code on the raised exception. Recognizing
    that deterministic engine signal — and walking the ``__cause__`` chain in case
    it is wrapped — is what makes the degrade-recovery in
    ``_generate_structured_output`` actually reachable for an overflow that
    surfaced through the retry loop. Without it a *recoverable* overflow fails the
    turn instead of degrading to the minimal fallback prompt (the NO-COLLAPSE
    guarantee; #662).
    """
    # Deterministic engine signal from the retry-loop path (see docstring).
    # ``seen`` bounds the walk: ``__cause__`` is assignable, so a hand-built cycle
    # would otherwise spin here — and hanging this classifier would stall the very
    # turn the degrade path exists to rescue. (We cannot reuse
    # ``api.exception_handlers._walk_cause_chain``; core importing the API layer
    # breaks import-linter contract 2.)
    cursor: Optional[BaseException] = exc
    seen: set[int] = set()
    while cursor is not None and id(cursor) not in seen:
        if getattr(cursor, "error_code", None) == TOKEN_LIMIT:
            return True
        seen.add(id(cursor))
        cursor = cursor.__cause__

    msg = str(getattr(exc, "message", "") or exc).lower()
    # Shared with llm_error_handler.is_token_limit_error so the two overflow
    # classifiers cannot drift (see CONTEXT_OVERFLOW_PHRASES).
    return any(p in msg for p in CONTEXT_OVERFLOW_PHRASES)


def _apply_symptom_retraction(
    case: "Case", milestones, response_obj, metadata: dict
) -> bool:
    """Honor an explicit, justified ``symptom_verified=False``.

    The symptom claim was a one-way latch: nothing could lower it once set, so
    a verification that later proved WRONG — misread data, the wrong system, an
    artefact — stayed on the case and kept the investigation pointed at a cause
    for something that was never really the symptom.

    This is about the CLAIM being mistaken, not about the problem being quiet.
    A problem is investigable while it EXISTS (evidence collectible, cause
    unidentified, solution unknown), so "not firing right now" is never grounds
    to retract; the prompt says so explicitly.

    The LLM is already the authority for this milestone (it is the only party
    that sets it), so retraction goes through the same authority rather than a
    parallel engine-side rule. The downstream layers were built for this: the
    ``rcc`` / ``working_conclusion`` backstop legs in ``cause_identification_leg``
    are explicitly gated on a verified symptom precisely so a conclusion
    "left behind after a symptom claim is withdrawn" stops counting, and
    ``verification_status._is_grounded`` reads the same anchor. Withdrawal was
    designed for; it was simply unreachable.

    TWO GUARDS, because a spurious retraction is worse than a missed one — it
    would discard real progress and could oscillate:

    1. Only an EXPLICIT ``False`` counts. The field is ``Optional[bool]``, so
       "absent" and "false" are distinguishable; a model that omits it (the
       overwhelmingly common case) changes nothing.
    2. A justification for the retraction must be present in
       ``internal_reasoning.milestone_justifications``. Providers differ in how
       eagerly they populate optional booleans, and some will emit ``false`` by
       habit rather than by judgement; requiring the model to also write down
       WHY separates a decision from a default. This reuses the justification
       channel the prompt already mandates for milestone changes.

    Returns True when a retraction was applied.
    """
    claimed = getattr(milestones, "symptom_verified", None)
    if claimed is not False:
        return False
    if not case.progress or not case.progress.symptom_verified:
        return False  # nothing to retract

    reasoning = getattr(response_obj, "internal_reasoning", None)
    justifications = getattr(reasoning, "milestone_justifications", None)
    rationale = (
        justifications.as_dict().get("symptom_verified") if justifications else None
    )
    if not rationale or not str(rationale).strip():
        logger.warning(
            "Case %s: ignoring unjustified symptom_verified=False. Retraction "
            "discards established progress, so it requires an explicit "
            "justification — an unexplained false is treated as a provider "
            "default, not a judgement.",
            case.case_id,
        )
        return False

    case.progress.symptom_verified = False
    metadata.setdefault("milestones_retracted", []).append("symptom_verified")
    logger.info(
        "Case %s: symptom_verified RETRACTED at turn %s — %s",
        case.case_id,
        case.current_turn,
        str(rationale)[:200],
    )
    return True


def _evidence_coverage(
    case: "Case", source_file_id: str | None, extract: str | None = None
) -> "tuple[datetime | None, datetime | None, str | None]":
    """The time span this evidence's CONTENT covers, and where it came from.

    ``Evidence.coverage_start_ts`` / ``coverage_end_ts`` have existed (with a DB
    index) since the case-timeline work, and the model docstring has always said
    the system fills them — but no writer ever did, so every LLM-authored row
    landed NULL. The only temporal signal left was ``collected_at_turn``, i.e.
    WHEN THE AGENT LOOKED, which says nothing about how old the observation is.

    Resolved in order:

    1. **The extract's own timestamps.** An evidence row is a SLICE, so its own
       quoted lines are the authority on what it covers. ``extract_time_range_ts``
       was promoted out of the extractors for exactly this (see its docstring).
    2. **The file's span, but only when it is a single instant.** A point-in-time
       file — an alert notification, a paste stamped from a forwarding caller's
       ``observed_at`` — describes one moment, so the slice can only be that
       moment too.
    3. **Unknown.**

    A RANGED file is deliberately NOT inherited. Doing so was a real defect, and
    the justification for it was inverted: it claimed widening "can only make
    evidence look OLDER, never fresher", but ``coverage_end_ts`` is what the
    staleness read consults, and widening moves the END LATER. A dump spanning
    12:00-19:45 that contains a 17:36 symptom would report "last observed 19:45"
    and read CURRENT — masking exactly the staleness this machinery exists to
    surface. Unknown is honest; a fabricated recent timestamp is not.
    """
    if extract and extract.strip():
        # Local import: keeps the module-level import graph unchanged, and this
        # is the only caller.
        from faultmaven.modules.preprocessing.extractors.utils import (
            extract_time_range_ts,
        )

        try:
            start_ts, end_ts, source = extract_time_range_ts(extract)
        except Exception:  # noqa: BLE001 - a parse failure must not lose the turn
            logger.warning(
                "Could not parse timestamps from an evidence extract; falling "
                "back to the source file's coverage.",
                exc_info=True,
            )
        else:
            # A single-timestamp extract parses as (start, None) - the head and
            # tail scans land on the same line. Content covering one instant
            # starts AND ends there; leaving the end None would read as UNDATED
            # and discard the very observation time this exists to capture.
            if start_ts is not None or end_ts is not None:
                # The slice's own timestamps, so the slice's own provenance.
                return start_ts or end_ts, end_ts or start_ts, source

    if source_file_id is None:
        return None, None, None
    uploaded = case.find_uploaded_file(source_file_id)
    if uploaded is None:
        return None, None, None
    file_start = getattr(uploaded, "coverage_start_ts", None)
    file_end = getattr(uploaded, "coverage_end_ts", None)
    if file_start is not None and file_start == file_end:
        # Inherit the provenance with the span. A row that inherits an
        # ``epoch_s`` guess is exactly as unfounded as the file it came from,
        # and losing that here would put the trust decision back where it was.
        return file_start, file_end, getattr(uploaded, "coverage_source", None)
    return None, None, None


def _resolve_evidence_source(
    case: "Case", source_file_id: str | None, source_type: "EvidenceSourceType"
) -> "tuple[str | None, EvidenceSourceType]":
    """Guard a hallucinated / stale ``source_file_id``.

    The LLM declares ``source_file_id`` on each ``EvidenceToAdd``; the schema
    validator enforces it is PRESENT (unless ``USER_DESCRIPTION``) but NOT that it
    points at a real uploaded file. An id that resolves to no file passes
    validation, then fails the ``evidence.source_file_id`` foreign key at save —
    which aborts the entire turn (silent progress loss, observed in a behavioral
    run). When the id does not resolve, drop the file anchor and record the slice
    as ``USER_DESCRIPTION`` (the only fileless-legal source type per the
    ``evidence_source_invariant`` CHECK): the evidence content is preserved, just
    not file-attributed. Returns the (possibly adjusted) ``(source_file_id,
    source_type)``.
    """
    if source_file_id is not None and case.find_uploaded_file(source_file_id) is None:
        logger.warning(
            "Evidence source_file_id %s does not resolve to an uploaded file for "
            "case %s; recording the slice as USER_DESCRIPTION (no file anchor) so "
            "the turn is not lost to a foreign-key failure.",
            source_file_id,
            case.case_id,
        )
        return None, EvidenceSourceType.USER_DESCRIPTION
    return source_file_id, source_type


def _infer_milestones(
    category: EvidenceCategory, milestones_completed_this_turn: list[str]
) -> list[str]:
    """
    Infer which milestones this evidence likely advanced.

    This implements Tier 2 of the three-tier milestone attribution logic:
    - Tier 1: MilestoneUpdates drives milestone state (turn-level, LLM specifies)
    - Tier 2: System infers advances_milestones from category (THIS FUNCTION - handles 90%)
    - Tier 3: LLM overrides when explicit (optional, handles 10% edge cases)

    Design Reference:
    - docs/working/MILESTONE-ADVANCEMENT-ANALYSIS.md (Option 2.5)
    - docs/working/DESIGN-DISCUSSION-SUMMARY-2026-02-11.md

    Args:
        category: The evidence category (the verification quartet:
            SYMPTOM / CAUSAL + their ABSENCE rows)
        milestones_completed_this_turn: Milestones completed this turn from MilestoneUpdates

    Returns:
        List of milestone names this evidence contributed to

    Logic:
        1. Get eligible milestones for this category from CATEGORY_MILESTONE_MAP
        2. Intersect with milestones completed this turn (from MilestoneUpdates)
        3. Result = milestones this evidence can claim credit for

    Example:
        category = SYMPTOM_EVIDENCE
        milestones_completed_this_turn = ["symptom_verified"]
        eligible = ["symptom_verified"]
        result = ["symptom_verified"]

    Key Insight:
        With one-file-per-turn constraint (UI limitation), inference is UNAMBIGUOUS.
        There's only one evidence record per turn, so all eligible milestones completed
        that turn get attributed to it. No guessing needed.

    Note:
        - The verification quartet (SYMPTOM/CAUSAL + their ABSENCE rows).
          The absence categories map to [] — mitigation_verified /
          solution_verified are gate milestones set by compliance detection,
          not by evidence category; the absence rows are consumed directly by
          the readiness checks (see CATEGORY_MILESTONE_MAP).
        - If category not in map, returns [] (safe fallback).
        - LLM can override by explicitly setting advances_milestones in EvidenceToAdd.
    """
    # Get eligible milestones for this category
    eligible_milestones = CATEGORY_MILESTONE_MAP.get(category, [])

    # Intersect with milestones completed this turn
    # This is the "system inference" - we know this evidence contributed to these milestones
    inferred = [m for m in milestones_completed_this_turn if m in eligible_milestones]

    logger.debug(
        f"_infer_milestones: category={category.value}, "
        f"milestones_completed_this_turn={milestones_completed_this_turn}, "
        f"eligible={eligible_milestones}, "
        f"inferred={inferred}"
    )

    return inferred


# =============================================================================
# Content Sanitization
# =============================================================================


# =============================================================================
# Reasoning Validation
# =============================================================================


def validate_reasoning_first(
    response_obj: BaseInteractionResponse, case: Case
) -> tuple[bool, list[str], set[str]]:
    """
    Validate that milestone completions are justified with internal reasoning.

    This function enforces the "Reasoning-First" pattern where the LLM must provide
    justifications for milestone completions BEFORE setting state updates. This prevents
    the LLM from arbitrarily completing milestones without evidence-based reasoning.

    EXCEPTION: Validation is skipped during terminal state transitions to allow graceful
    case closure without forcing justifications. This handles the scenario where:
    - User confirms a pending transition via the User-Agent Handshake
    - Case is transitioning to RESOLVED or CLOSED

    Reference: Prompt Engineering Guide Section 13 (lines 3236-3281)

    Args:
        response_obj: LLM's structured response (InquiryResponse, InvestigationResponse_*, or TerminalResponse)
        case: Current case state

    Returns:
        (is_valid, error_messages, offending_milestones): validation result, the
        error messages, and the SET of milestone names that failed validation.
        The caller strips ONLY ``offending_milestones`` from the emission — a
        single unjustified milestone no longer wipes co-emitted valid ones
        (the S1 collateral-wipe fix; redesign §5). Global failures (no
        internal_reasoning, no actionable evidence) implicate every completed
        milestone; per-milestone justification gaps implicate only that one.
        Turn-reference format errors implicate no milestone (advisory only).

    Skip Conditions (validation bypassed):
        1. Response is InquiryResponse or TerminalResponse (no investigation milestones)
        2. Case is already in terminal state (RESOLVED or CLOSED)
        3. Case has a pending_transition (user confirmation in progress)
    """
    errors: list[str] = []
    offending: set[str] = set()

    # Debug logging for Turn 2 issue
    logger.debug(
        f"validate_reasoning_first: response_type={type(response_obj).__name__}, "
        f"case_status={case.state.value}, "
        f"is_InquiryResponse={isinstance(response_obj, InquiryResponse)}, "
        f"is_TerminalResponse={isinstance(response_obj, TerminalResponse)}"
    )

    # Only validate investigation responses (not INQUIRY or TERMINAL)
    if isinstance(response_obj, (InquiryResponse, TerminalResponse)):
        logger.debug("Skipping reasoning validation (INQUIRY or TERMINAL response)")
        return True, [], set()

    # Skip validation if case is already in terminal state
    if case.is_terminal:
        logger.debug("Skipping reasoning validation (case already in terminal state)")
        return True, [], set()

    # Check if response has internal_reasoning field
    internal_reasoning = getattr(response_obj, "internal_reasoning", None)
    milestones = getattr(response_obj.state_updates, "milestones", None)

    if not milestones:
        # No milestones being completed, no validation needed
        return True, [], set()

    # Get list of milestone fields being completed (set to True)
    completed_milestones = []
    milestone_dict = milestones.model_dump(exclude_none=True)
    for milestone_name, value in milestone_dict.items():
        if isinstance(value, bool) and value is True:
            completed_milestones.append(milestone_name)

    if not completed_milestones:
        # No milestones actually completed, no validation needed
        return True, [], set()

    # ===== TERMINAL TRANSITION EXCEPTION =====
    # Skip validation if case has a pending transition (User-Agent Handshake in progress).
    # The user has already confirmed the transition, so we allow graceful closure
    # without forcing the LLM to justify additional milestones.
    if case.state == CaseState.INVESTIGATING:
        has_pending = hasattr(case, "pending_transition") and case.pending_transition
        already_solution_verified = case.progress.solution_verified

        if has_pending or already_solution_verified:
            logger.debug(
                f"Skipping reasoning validation (terminal transition in progress: "
                f"pending={has_pending}, solution_verified={already_solution_verified})"
            )
            return True, [], set()

    # If milestones are being completed, internal_reasoning is REQUIRED.
    # Global failure: none of the completed milestones are justified.
    if not internal_reasoning:
        errors.append(
            f"Milestones {completed_milestones} completed without internal_reasoning. "
            "You MUST provide internal_reasoning with justifications when completing milestones."
        )
        return False, errors, set(completed_milestones)

    # Check 1: All completed milestones must have justifications.
    # Per-milestone failure: only the unjustified milestone is offending.
    #
    # ``as_dict()`` and not the model itself: under strict mode every milestone
    # key arrives populated, ``null`` where the model had nothing to say, so a
    # membership test against the raw model would report every milestone as
    # justified and this gate would never fire again (fm#1057).
    justifications = internal_reasoning.milestone_justifications.as_dict()
    for milestone in completed_milestones:
        if milestone not in justifications:
            offending.add(milestone)
            errors.append(
                f"Milestone '{milestone}' completed without justification. "
                f"You MUST set internal_reasoning.milestone_justifications.{milestone} "
                f"to a justification citing specific evidence IDs. "
                f"Example: {{{milestone}: 'Confirmed via ev_abc123 (logs) showing X and ev_def456 (metrics) showing Y'}}. "
                f"Leaving {milestone} null or blank is what caused this rejection."
            )

    # Check 1.5: Warn if trying to complete milestones with no actionable evidence.
    # Contextual evidence (raw uploads) cannot justify milestones — only
    # LLM-classified evidence (symptom, causal, mitigation, solution) counts.

    evidence_being_added = (
        getattr(response_obj.state_updates, "evidence_to_add", []) or []
    )
    # Every evidence row is claim-anchored — any existing or to-add row counts.
    has_actionable_evidence = bool(case.evidence) or bool(evidence_being_added)

    # ``justifications`` (the dict), not the model: a Pydantic model is ALWAYS
    # truthy, so testing the field directly would make this branch fire on every
    # turn that reaches it, including one that justified nothing (fm#1057).
    if justifications and not has_actionable_evidence:
        # Global failure: with no actionable evidence, no milestone is justifiable.
        offending.update(completed_milestones)
        errors.append(
            "Cannot complete milestones when no actionable evidence has been collected. "
            "You must first analyze and classify evidence before completing milestones."
        )

    # Check 2: REMOVED - Category-based validation no longer requires evidence_analyzed
    # evidence_analyzed is now OPTIONAL and only used for historical turn references
    # Milestone validation is done via evidence categories in evidence_processor.py

    # Check 3: Validate turn references if provided (optional)
    # If evidence_analyzed contains turn references (e.g., "turn_2"), validate format
    for ref in internal_reasoning.evidence_analyzed:
        if isinstance(ref, str) and ref.startswith("turn_"):
            try:
                turn_num = int(ref.split("_")[1])
                if turn_num < 1 or turn_num > case.current_turn:
                    errors.append(
                        f"Invalid turn reference '{ref}': turn number must be between 1 and current turn ({case.current_turn})"
                    )
            except (IndexError, ValueError):
                errors.append(
                    f"Invalid turn reference format: '{ref}'. Expected format: 'turn_N' where N is a number"
                )

    # Turn-reference errors are advisory and implicate no milestone — they do
    # not add to `offending`, so they never strip a validated milestone.
    return len(errors) == 0, errors, offending


def _post_process_llm_response(
    updates: Any,
    user_message: str,
    case: Case,
) -> Any:
    """
    Post-process LLM response — currently a no-op pass-through.

    Previously this function ran regex-based pattern detection on the user
    message to create fallback evidence when the LLM didn't produce any.
    That approach was removed because:

    1. It second-guessed the LLM with crude regexes. When the LLM
       deliberately chose NOT to classify a message as data (e.g., an SSH
       banner with incidental "memory" / "8%" text), the fallback overrode
       that judgment and created bogus SYMPTOM_EVIDENCE records.

    2. It conflated "user pasted data into the text box" with "user
       submitted external data for analysis". A user who pastes terminal
       output as a conversational message should get a conversational
       response — or a clarifying question — not silent evidence creation.

    3. When attachments existed, it duplicated the attachment pipeline's
       evidence with a lower-quality regex-derived record.

    The LLM already sees every user message and can:
    - Create evidence via ``evidence_to_add`` when it recognizes data.
    - Ask for clarification when the message is ambiguous.
    - Treat non-data messages as conversation.

    If the LLM consistently fails to recognise a specific class of data,
    the fix belongs in the prompt or LLM schema, not in a post-hoc regex
    layer that cannot understand context.

    Args:
        updates: Parsed LLM response (InquiryResponse or InvestigationResponse_*)
        user_message: Original user message (retained for future use / logging)
        case: Current case state

    Returns:
        The updates object, unmodified.
    """
    evidence_to_add = getattr(updates, "evidence_to_add", []) or []
    logger.debug(
        f"Post-processing LLM response: "
        f"evidence_to_add_count={len(evidence_to_add)}"
    )
    return updates


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
NO_ROOT_CAUSE_ESTABLISHED = "No root cause established"


def _get_root_cause_summary(case) -> str:
    """A brief root-cause description for confirmation prompts.

    Precedence: the recorded ``RootCauseConclusion`` → the working conclusion,
    but ONLY when it carries a real finding → the honest
    ``NO_ROOT_CAUSE_ESTABLISHED``.

    The working-conclusion leg is gated on
    ``is_early_stage_conclusion`` (#987): that fallback exists to surface a
    cause the engine holds outside the conclusion record, and the early-stage
    PLACEHOLDER is definitionally not that. Rendering it here told the user
    "Root cause: Investigating potential causes - awaiting hypothesis
    generation" in the resolution recap of a case whose preceding ten turns had
    identified, fixed, and verified the cause. Saying nothing was established
    is honest; saying the investigation has not begun is false.
    """
    if case.root_cause_conclusion and getattr(
        case.root_cause_conclusion, "root_cause", None
    ):
        cause = case.root_cause_conclusion.root_cause
        return cause[:200] + "..." if len(cause) > 200 else cause
    wc = case.working_conclusion
    if wc and getattr(wc, "statement", None) and not is_early_stage_conclusion(wc):
        stmt = wc.statement
        return stmt[:200] + "..." if len(stmt) > 200 else stmt
    return NO_ROOT_CAUSE_ESTABLISHED


def _get_solution_summary(case) -> str:
    """Extract a brief solution description from the case for confirmation prompts."""
    if case.solutions:
        sol = case.solutions[-1]  # Most recent solution
        # Try fields in order of specificity. Skip titles that look like
        # raw enum references (e.g., "Solution: SolutionType.CONFIG_CHANGE")
        # which indicate the LLM wrote a placeholder instead of a description.
        title = getattr(sol, "title", None)
        if title and "SolutionType." not in title:
            return title[:200] + "..." if len(title) > 200 else title
        longterm = getattr(sol, "longterm_fix", None)
        if longterm:
            return longterm[:200] + "..." if len(longterm) > 200 else longterm
        immediate = getattr(sol, "immediate_action", None)
        if immediate:
            return immediate[:200] + "..." if len(immediate) > 200 else immediate
        # Last resort: return title even if it has enum reference
        if title:
            return title[:200] + "..." if len(title) > 200 else title
    return "Not yet documented"


def _investigation_confirmation_suggestions() -> list:
    """Generate DECIDE follow-up suggestions for investigation confirmation.

    Used when the dropdown triggers INQUIRY → INVESTIGATING and a problem
    statement already exists. One positive (confirm) and one mild negative (refine).
    """
    return [
        {
            "label": "Yes, let's investigate",
            "action_type": "DECIDE",
            "payload": "Yes, that's correct. Let's investigate.",
            "body": "Confirm the problem statement and start the investigation.",
            "intent": {"type": "confirmation", "confirmation_value": True},
        },
        {
            "label": "Not quite, let me clarify",
            "action_type": "DECIDE",
            "payload": "Not quite — let me clarify the problem before we investigate.",
            "body": "Refine the problem statement before starting the investigation.",
            "intent": {"type": "confirmation", "confirmation_value": False},
        },
    ]


def _kb_prefetch_query_on_identification(
    prior_cause_state: "CauseState",
    current_cause_state: "CauseState",
    root_cause_conclusion,
    working_conclusion,
) -> "str | None":
    """The KB-remediation warm-up query for the ``cause_state``→IDENTIFIED edge.

    Returns the cause text to pre-fetch KB remediation for, but ONLY on the turn
    ``cause_state`` newly crosses to IDENTIFIED (INV-35) — since cause_state is
    engine-derived there is no milestone event to hang this on, so the caller
    passes the pre-recompute value and the post-recompute value and this detects
    the rising edge. Prefers the LLM conclusion's ``root_cause`` over the working
    conclusion's ``statement``. Returns None when it is not the edge, or no cause
    text is available yet.
    """
    if (
        prior_cause_state == CauseState.IDENTIFIED
        or current_cause_state != CauseState.IDENTIFIED
    ):
        return None
    if root_cause_conclusion and getattr(root_cause_conclusion, "root_cause", None):
        return root_cause_conclusion.root_cause
    if working_conclusion and getattr(working_conclusion, "statement", None):
        return working_conclusion.statement
    return None


def _recompute_cause_state_from_chain(
    case: "Case", *, exclusion_survivors: "set[str] | frozenset[str]" = frozenset()
) -> None:
    """Chain-derived ``cause_state`` (Option A, methodology §9.2; flag ON).

    ``exclusion_survivors`` are the ROOT nodes the LLM certified this turn as the
    sole survivor of an exhaustive differential (§7.1.1); each is validated by
    ``validate_by_exclusion`` iff its differential has genuinely collapsed. Empty on
    reload/terminal recomputes — a previously stamped DEDUCTIVE node survives on its
    own (``derive_node_states`` locks it), so no re-assertion is needed to keep it.

    ``IDENTIFIED`` iff some live hypothesis's chain ROOT is VALIDATED from real
    rung evidence (``derive_node_states`` + ``any_chain_root_validated``) **AND
    the symptom is verified** (the cause-identification anchor) **AND the
    validated root is UNCONTESTED** (§7.1.2 MECE arbitration: >1 simultaneously-
    validated distinct roots is a coherence violation — hold at CANDIDATES
    pending discrimination) — never from a flat assertion.
    The chain is load-bearing: a cause reaches IDENTIFIED only by emitting a chain,
    grounding its root, AND having established the evidence-grounded verified
    symptom that anchors it. A validated root without ``symptom_verified`` holds
    at CANDIDATES (never UNKNOWN).

    Pure structural grounding by design — there is deliberately NO flat fallback
    and NO separate disconfirmation guard. Disconfirmation is handled by the chain
    itself: M6 attaches a durable refutation to the root and ``derive_node_states``
    holds it REFUTED, so a disproven cause simply fails ``any_chain_root_validated``.
    ``cause_state`` is a SOFT signal — under-reporting (the LLM emits a chain +
    correct conclusion but does not attach the rung evidence) is backstopped for
    terminal soundness by the ``RootCauseConclusion`` (``terminal_transitions.
    _cause_identified`` reads cause_state OR the RCC OR the working conclusion), so
    it costs only prompt-focus accuracy, never a wrong terminal conclusion.

    Order matters:
      1. M6 (Option c): a counterfactually-disconfirmed grounded cause gets a
         DURABLE engine refutation attached to its root + the conclusion retracted
         — BEFORE derive, so derive refutes the root from that evidence this turn
         and every later turn (preventing the turn-28 resurrection that an
         imperative-only refutation would allow once stale support re-derives it).
      2. ``derive_node_states``: evidence → node states (validate/refute each rung).
      3. cause_state: IDENTIFIED if a live chain root is validated; else
         CANDIDATES (≥2 active hypotheses, OR a live root that is INCONCLUSIVE —
         the soft floor) / UNKNOWN. It follows the root's evidence-derived truth
         (M6 demotion drops it automatically), but a root that merely loses
         validation to an evidence TIE (INCONCLUSIVE, not REFUTED) holds the case
         at CANDIDATES rather than flapping to UNKNOWN (finding-5 / NO-COLLAPSE);
         only a counterfactual REFUTED drops it fully.
    """
    p = case.progress
    # §7.6 / INV-34 + §7.7 / INV-35: attribute an LLM-authored conclusion to the
    # standing hypothesis it names — authoritatively when it named its cause's root
    # node (names_root_node_id), else by lexical fallback. Runs BEFORE the M6
    # demotion, so M6 tracks the LLM's actual cause (not a max-likelihood proxy)
    # and retract_disconfirmed_rcc can reach a disconfirmed LLM conclusion.
    link_llm_rcc_to_cause(case)
    demote_disconfirmed_cause_via_evidence(case)
    derive_node_states(case)
    # Deductive validation (§7.1.1, proof-by-exclusion): stamp DEDUCTIVE on any
    # LLM-certified survivor whose differential has now collapsed to it. Runs AFTER
    # derive_node_states (so siblings have reached REFUTED and their exclusion
    # strength is set) and BEFORE any_chain_root_validated below, so a freshly
    # validated root promotes cause_state in this same pass. The asserted set is the
    # agent's exhaustiveness certification; validate_by_exclusion re-checks the
    # engine-computable guards (≥2 members, all-but-survivor absolutely refuted).
    # If it stamps anything, re-derive: a newly-DEDUCTIVE root can satisfy a
    # downstream effect's AND-gate (M7) that the empirical pass above missed because
    # it settled before the stamp. The demotion-guard preserves the DEDUCTIVE node on
    # the re-run, so this only ADDS downstream validations (no churn when nothing
    # stamped — the common case returns early).
    if validate_by_exclusion(case, exclusion_survivors):
        derive_node_states(case)
    # Source-of-truth retraction: clear a RootCauseConclusion whose named cause
    # (validated_hypothesis_id) is now disconfirmed, so no consumer asserts a
    # disproven cause. Covers the gap M6 misses when cause_state never reached
    # IDENTIFIED. Runs BEFORE the cause_state branch so a freshly-validated root
    # below re-synthesizes a correct RCC via synthesize_rcc_from_validated_root.
    retract_disconfirmed_rcc(case)
    # Engine-mirror coherence: an ENGINE-authored RCC whose grounding root no
    # longer stands validated (demoted by the restatement guard on a pre-guard
    # persisted case, or by an evidence tie) is cleared here — the readiness/
    # report readers key on RCC presence, and a mirror must not outlive its
    # chain. Runs on EVERY recompute (the IDENTIFIED branch below re-mints via
    # synthesize when a validated root stands; the demotion path otherwise had
    # no owner for the stale mirror). LLM-authored conclusions are untouched.
    # §7.1.2 MECE arbitration (#656): >1 simultaneously-validated DISTINCT
    # standing roots is a coherence violation (S2 — at most one origin can be
    # the cause), so identification is HELD at CANDIDATES pending
    # discrimination — the forward mirror of the §7.1.1 exclusion collapse.
    # Node states are untouched (each root's evidence rules it); duplicates and
    # same-LIVE-causal-line roots collapse to one cause; a counterfactually
    # confirmed root settles the contest. Computed ONCE here (post-derive, the
    # graph is settled) and threaded into every same-frame consumer.
    contested_ids = mece_contested_root_ids(case)
    retract_stale_engine_rcc(case, contested_ids=contested_ids)
    root_validated = any_chain_root_validated(case)
    # The persisted flag records CONTEST EXISTENCE — the same predicate every
    # behavioral consumer acts on (the IDENTIFIED gate below, the mirror
    # retraction above, the context-builder discrimination ask) — NOT the
    # symptom-anchored sub-case: a contest whose symptom is still unverified
    # already retracts the mirror and renders the ask, so the queryable flag
    # and the metric must see it too (behavior and observability keyed apart
    # is how holds go invisible).
    contested = bool(contested_ids)
    if contested and not p.cause_identification_contested:
        # Block-event semantics (one increment per transition INTO the
        # contest), edge-triggered on the persisted flag like the M2
        # over-claim seam.
        cause_identification_held_mece_total.inc()
        logger.warning(
            "MECE arbitration hold: case=%s turn=%s — %d simultaneously-"
            "validated roots across competing causes (%s); cause "
            "identification held at CANDIDATES pending discriminating "
            "evidence",
            case.case_id,
            case.current_turn,
            len(contested_ids),
            sorted(contested_ids),
            extra={
                "event": "cause_identification_mece_hold",
                "case_id": case.case_id,
                "turn": case.current_turn,
                "contested_root_ids": sorted(contested_ids),
            },
        )
    p.cause_identification_contested = contested
    # The evidence-grounded VERIFIED SYMPTOM is the anchor for cause
    # identification: IDENTIFIED requires ``symptom_verified``. A validated chain
    # root WITHOUT a verified symptom is held at CANDIDATES (never flapped to
    # UNKNOWN), pending verification; it is not promoted to IDENTIFIED and no
    # RootCauseConclusion is synthesized. This gates CAUSE IDENTIFICATION only —
    # not runbook retrieval / early triage, which engage before the symptom is
    # verified.
    if root_validated and p.symptom_verified and not contested_ids:
        p.cause_state = CauseState.IDENTIFIED
        # Case invariant: IDENTIFIED requires a positive likelihood + a method.
        # Floor them (the LLM's own higher confidence still wins where applied).
        if not p.root_cause_likelihood or p.root_cause_likelihood <= 0:
            p.root_cause_likelihood = 0.8
        if not p.root_cause_method:
            p.root_cause_method = "hypothesis_validation"
        # §9.3/§7.7: the validated root IS the cause, so mirror it into the
        # RootCauseConclusion the disposition/report layer reads. The mirror
        # outranks an LLM-authored conclusion and replaces it here — this runs
        # after this turn's LLM conclusion has been applied, so the surfaced text
        # is rendered from the chain whenever one stands. With no validated root
        # this branch is not reached at all and the LLM's conclusion stands as the
        # explicit fallback.
        synthesize_rcc_from_validated_root(case)
    elif (
        root_validated
        or HypothesisManager.count_active_hypotheses(case) >= 2
        or any_chain_root_inconclusive(case)
    ):
        # CANDIDATES covers: ≥2 active hypotheses, an INCONCLUSIVE live root (the
        # soft floor), a validated root still awaiting symptom verification —
        # the anchor exists structurally but is not yet grounded — AND the
        # §7.1.2 MECE-contested hold (several validated roots, none arbitrated:
        # honest state is "several candidates", not "identified").
        p.cause_state = CauseState.CANDIDATES
    else:
        p.cause_state = CauseState.UNKNOWN

    # #695 Defect A: derive hypothesis VALIDATED from its chain root's final
    # node_state — the sole producer of a VALIDATED hypothesis. Runs LAST, after
    # the whole node-state settling (derive_node_states + the validate_by_exclusion
    # re-derive above) AND the M6 demotion, so it reads final node states and
    # cannot resurrect a just-REFUTED hypothesis. Keeps hypothesis.state, the
    # report bucket, the grade, and cause_state on ONE determination.
    project_hypothesis_states_from_roots(case)


def _resolve_chat_provider_name(llm_provider: "Any") -> str:
    """Best-effort name of the CHAT provider driving the investigation, for the
    DF-6 provider-floor metric (INV-39).

    In the real deployment ``self.llm_provider`` is the ``LLMRouter``, which has
    no ``provider_name`` — so fall back to its configured chat provider
    (``settings.llm.provider``, the ``CHAT_PROVIDER`` the router routes through).
    A raw provider (unit tests, non-router deployments) exposes ``provider_name``
    directly. ``settings.llm.provider`` is an ``LLMProvider`` enum (``.value``).
    Returns ``"unknown"`` when neither resolves — the metric labels the crossing
    rather than dropping it."""
    name = getattr(llm_provider, "provider_name", None)
    if isinstance(name, str) and name:
        return name
    chat = getattr(
        getattr(getattr(llm_provider, "settings", None), "llm", None),
        "provider",
        None,
    )
    if chat is not None:
        return getattr(chat, "value", None) or str(chat)
    return "unknown"


def _recompute_assessment_state(
    case: "Case",
    *,
    exclusion_survivors: "set[str] | frozenset[str]" = frozenset(),
    rcc_authored_this_turn: bool = False,
    metadata: "dict[str, Any] | None" = None,
    provider_name: "str | None" = None,
) -> None:
    """Recompute the engine-owned assessment variables each INVESTIGATING turn.

    Assessment variables are TRUTH signals the engine derives — never
    path-stripped (redesign R1). Called at the end of
    ``_apply_investigation_updates`` so hypotheses/solutions added this turn
    are reflected.

    - ``cause_state`` is chain-derived (Option A, §9.2) via
      ``_recompute_cause_state_from_chain`` (documented at its definition):
      ``IDENTIFIED`` iff a standing chain root is VALIDATED; NOT sticky (it
      follows the root's evidence-derived truth, so M6 drops it on its own).
    - ``solution_proposed`` / ``solution_state`` are DERIVED from live
      SOLUTION offers each recompute (INV-32, #656 DF-3 — the write-once
      latch dissolved): first the M5 license is re-checked (pending offers
      whose established-cause license fell this turn are withdrawn), then
      the pair is derived by the SHARED
      ``terminal_transitions.derive_solution_surface`` (also called by the
      resolution finalizer and the CLOSED executor — one definition, no
      terminal drift).
    - ``verification_status``: the grounding × progress join
      (``assess_verification_status``), computed LAST so it reads the grade the
      cause_state recompute (including its deductive-exclusion stamp) just
      settled — the #593 recompute-after-stamp ordering. Persisted in the
      progress blob so the model-declared obtainability signal it reads survives
      across turns (Phase 3).
    """
    p = case.progress

    # NOTE: the M2 confirm-side stamp (confirm_root_from_resolution_absence)
    # deliberately does NOT run here. An absence row's mere appearance is an
    # LLM self-claim — a premature "it's stable now" row emitted mid-rollout
    # must not confirm anything (observed live in the gate sims). The stamp
    # fires only at RESOLVED transition execution, on the user's explicit
    # confirmation (terminal_transitions._execute_resolved_transition).
    _recompute_cause_state_from_chain(case, exclusion_survivors=exclusion_survivors)

    # INV-32 (#656 DF-3): solution_proposed is DERIVED, not latched. Runs
    # AFTER the cause recompute so the license re-check reads this turn's
    # settled truth (a root demoted or a conclusion retracted above withdraws
    # the pending offer in the same turn).
    from faultmaven.core.investigation.terminal_transitions import (
        derive_solution_surface,
    )

    _withdraw_unlicensed_solution_offers(case, metadata)
    derive_solution_surface(case)

    # Assurance grade + verification status LAST — after the cause_state
    # recompute above has run derive_node_states + the deductive-exclusion
    # stamp, so both read a fresh graph rather than pre-empting the deductive
    # arm. The grade is persisted (progress blob, like verification_status) so
    # the grade × conclusion-confidence seam is queryable per turn (#656);
    # the join reads the just-persisted grade rather than recomputing, so both
    # persisted signals derive from the same graph snapshot.
    p.cause_assurance = grade_cause_assurance(case)
    p.verification_status = assess_verification_status(case, grade=p.cause_assurance)

    # DF-6 provider-floor metric (§5.2, INV-39): count the FIRST time this case
    # crosses the work gate, per CHAT provider. ``work_gate_passed`` is the
    # documented observability primitive; the ``work_gate_crossed`` latch makes
    # the count exactly once-per-case (a later drop below the gate never
    # re-counts, and re-emitting the same hypotheses next turn does not
    # double-count). ``provider_name`` is supplied by the caller (where the
    # provider is in scope). Metric-only; it never changes engine behavior.
    if not p.work_gate_crossed and work_gate_passed(case):
        p.work_gate_crossed = True
        provider = provider_name or "unknown"
        work_gate_crossed_total.labels(provider=provider).inc()
        logger.info(
            "work_gate_crossed case=%s turn=%s provider=%s",
            case.case_id,
            case.current_turn,
            provider,
            extra={
                "event": "work_gate_crossed",
                "case_id": case.case_id,
                "turn": case.current_turn,
                "provider": provider,
            },
        )

    # M2 over-claim seam (#656 turn-6 shape): a recorded conclusion claims
    # "verified" while the graph grade lacks counterfactual confirmation. The
    # engine mirror can no longer produce this (its confidence is grade-derived),
    # so a hit here is an LLM-authored conclusion over-claiming — and, since a
    # standing validated root takes the conclusion over (§7.7), specifically a
    # FALLBACK conclusion over-claiming with no such root behind it. Surfaced at
    # WARNING (prod-visible, unlike the DEBUG grounding trace). Edge-triggered via the persisted
    # flag so a standing over-claim warns once, not once per turn (alert
    # hygiene); the per-turn state stays visible in the DEBUG grounding trace
    # and the persisted flag itself. The under-claim polarity lives in
    # ``_log_grounding_assessment``.
    rcc = case.root_cause_conclusion
    overclaims = conclusion_overclaims(rcc, p.cause_assurance)
    # Edge-triggered on the persisted flag, RE-ARMED when a conclusion was
    # (re)authored this turn: a NEW over-claiming conclusion replacing a
    # retracted one while the flag is still True is a distinct over-claim event
    # and must get its own WARNING, not be absorbed as "standing".
    if overclaims and (rcc_authored_this_turn or not p.cause_overclaim):
        logger.warning(
            "M2 over-claim seam: case=%s turn=%s conclusion claims verified "
            "(likelihood=%.2f, determined_by=%s) but cause_assurance=%s",
            case.case_id,
            case.current_turn,
            rcc.likelihood,
            getattr(rcc, "determined_by", None),
            p.cause_assurance.value,
            extra={
                "event": "cause_confidence_overclaim",
                "case_id": case.case_id,
                "turn": case.current_turn,
                "rcc_likelihood": rcc.likelihood,
                "rcc_determined_by": getattr(rcc, "determined_by", None),
                "cause_assurance": p.cause_assurance.value,
            },
        )
    p.cause_overclaim = overclaims

    _log_grounding_assessment(case)


def _log_grounding_assessment(case: "Case") -> None:
    """Debug-level structured trace of the grounding assessment, emitted at the
    one point where the grade × progress join is computed each turn.

    Permanent observability (not throwaway): it traces the join AND its inputs so
    a **grade ↔ cause_state divergence** — the composition-seam drift the design
    flags in §4.1 — is visible per turn in any case, not only in a debugger. Both
    polarities are flagged: ``seam_divergence`` is the UNDER-claim (a
    counterfactually CONFIRMED root with no identified cause_state / unverified
    symptom — the join reads healthier than the progress signals, masking a stuck
    investigation); ``seam_overclaim`` is the OVER-claim (#656 turn 6 — a
    conclusion claiming "verified" while the grade lacks counterfactual
    confirmation; also emitted at WARNING by the caller so prod sees it).

    Guarded by the level check so the payload construction (the
    node/hypothesis summaries; the grade is read from the field the caller just
    persisted) costs nothing above DEBUG, and
    the whole body is failure-isolated: a diagnostic trace must never break the
    turn pipeline it runs inside, whatever shape the case is in.

    **Not the progress ledger.** This runs inside response application — before
    the turn's progress decision and before the counter update at Step 5.8 — so
    its ``turns_without_progress`` / ``is_progress_stalled`` are the PREVIOUS
    turn's values, and it fires only on the generation path. Those fields are
    kept because they are useful context for the grounding readings around them,
    not because they are authoritative. Anything asking "did the engine stall
    this case?" wants the always-on per-turn stream in
    ``core/investigation/case_telemetry.py`` (#1142), which is emitted after the
    counter update, on every path, and carries the per-arm counts this trace
    does not.
    """
    if not logger.isEnabledFor(logging.DEBUG):
        return

    try:
        p = case.progress
        grade = p.cause_assurance  # persisted fresh by the caller this turn
        hyp_states: dict[str, int] = {}
        for h in case.hypotheses.values():
            hyp_states[h.state.value] = hyp_states.get(h.state.value, 0) + 1
        nodes = [
            {
                "type": n.node_type.value,
                "state": n.node_state.value,
                "method": (n.validation_method.value if n.validation_method else None),
            }
            for n in case.causal_nodes.values()
        ]
        seam_divergence = grade == CauseAssuranceGrade.CONFIRMED and (
            p.cause_state != CauseState.IDENTIFIED or not p.symptom_verified
        )
        seam_overclaim = conclusion_overclaims(case.root_cause_conclusion, grade)
        logger.debug(
            "grounding-assessment case=%s turn=%s verification_status=%s grade=%s "
            "cause_state=%s symptom_verified=%s hyps=%s nodes=%s seam_divergence=%s",
            case.case_id,
            case.current_turn,
            p.verification_status.value,
            grade.value,
            p.cause_state.value,
            p.symptom_verified,
            len(case.hypotheses),
            len(nodes),
            seam_divergence,
            extra={
                "event": "grounding_assessment",
                "case_id": case.case_id,
                "turn": case.current_turn,
                "verification_status": p.verification_status.value,
                "grade": grade.value,
                "cause_state": p.cause_state.value,
                "symptom_verified": p.symptom_verified,
                "work_gate_passed": work_gate_passed(case),
                "is_progress_stalled": is_progress_stalled(case),
                "turns_without_progress": case.turns_without_progress,
                "hypothesis_count": len(case.hypotheses),
                "hypothesis_states": hyp_states,
                "causal_nodes": nodes,
                "seam_divergence": seam_divergence,
                "seam_overclaim": seam_overclaim,
                "mece_contested": p.cause_identification_contested,
            },
        )
    except Exception:  # noqa: BLE001 - observability must never break the turn
        logger.debug("grounding-assessment trace failed", exc_info=True)


def _maybe_propose_deferred_close(case: "Case", metadata: dict) -> None:
    """Deferred-implementation disposition (redesign §3.1 row 3 / §6 Q2).

    When the cause + fix are known but the fix cannot be applied or verified
    this session (``solution_feasible == DEFERRED`` — e.g. it needs an
    out-of-band change request, a maintenance window, or another team), the
    case should reach a DISPOSITION with the solution documented rather than
    be held open waiting indefinitely (the failure mode observed in validation
    run 2). Which disposition depends on the case; see the pivot below.

    The engine proposes the disposition DETERMINISTICALLY: the LLM does not
    reliably drive to a disposition on its own when implementation is deferred.
    The user still confirms via the standard disposition handshake, and the
    documented root cause + solution are preserved either way
    (``closure_reason=solution_deferred`` on the close branch).

    Which disposition is proposed follows ``assess_closure_readiness``, the same
    resolve-preservation pivot the LLM-proposal path and the confirm-time INV-37
    guard apply. Its trigger is a QUALIFYING COUNTERFACTUAL CONFIRMATION —
    ``_has_causal_absence``, a gone=>gone row, the same bar
    ``assess_resolution_readiness`` uses for READY — NOT merely "a root cause
    and a solution are on record" (that phrasing survives in
    ``assess_closure_readiness``'s own summary line and is stale there too).
    Deferred implementation is a statement about WHEN the remaining work lands,
    not about whether the cause was found, so it must not cost a confirmed case
    its attribution.

    The proposal's rationale is published on
    ``metadata["deferred_solution_gate_message"]`` for the response composer to
    render below the LLM's reply — an engine-proposed disposition has to say why
    it is on the table.
    """
    p = case.progress
    if p.solution_feasible != SolutionFeasible.DEFERRED:
        return
    # Only meaningful once a fix is actually on record.
    if not (p.solution_proposed or case.solutions):
        return
    # INV-32: the closure message asserts "the root cause and fix are
    # documented" — that claim needs the SAME established-cause license the
    # fix offer needed (the M5 wrapper, verbatim — three gates, ONE
    # predicate). Solution records are monotone (never withdrawn), so
    # without this gate a case whose cause fell THIS turn (offer just
    # withdrawn license_lost, feedback telling the LLM to re-ground) would be
    # proposed for closure citing the disconfirmed cause in the same breath.
    if not _solution_cause_validated(case):
        return
    # Don't clobber an in-flight handshake.
    if getattr(case, "pending_transition", None):
        return
    # Defense in depth, and stricter than the `is_terminal` check it replaces:
    # the proposal target is now STATE-DEPENDENT. "closed" was a legal edge
    # from any state, so hardcoding it made this guard free; "resolved" is NOT
    # a legal edge from INQUIRY (ALLOWED_ACTIONS — resolution requires
    # investigation work). A proposal that cannot execute leaves
    # `pending_transition` standing, so every later confirm turn would fail the
    # same way. Only the INVESTIGATING pipeline calls this today; the guard
    # keeps that assumption true if another caller is ever added.
    if case.state != CaseState.INVESTIGATING:
        return

    from faultmaven.core.investigation.terminal_transitions import (
        assess_closure_readiness,
        deferred_disposition_signature,
        propose_transition,
    )

    # Resolve preservation (INV-37), the SAME choice the other two disposition
    # paths already make: the LLM-proposal path pivots CLOSED->RESOLVED on
    # SUGGEST_RESOLVE, and the confirm-time guard pivots a pending CLOSE the
    # same way. This proposer used to call propose_transition("closed")
    # directly, so it was the one path that could offer "close this case
    # without resolution" on a case its OWN eligibility scored resolvable.
    # Deferred implementation says WHEN the fix lands, not that the cause went
    # unfound, so it must not cost an attributable case its resolution.
    #
    # Scope of the evidence, stated precisely: case_fa29e0023b85 ended with
    # disposition_eligibility {resolved: ready, closed: suggests_alternative},
    # but that column is recomputed at every save, so it describes the FINAL
    # turn — after the user reported applying the fix — not the five earlier
    # turns on which this function offered the close pair. The defect this
    # fixes is therefore path INCONSISTENCY (one proposer disagreeing with the
    # other two), not a reconstructed history of that case. What kept those
    # five turns going is the re-proposal loop, which is a separate concern.
    # The engine's own offer was withdrawn earlier this turn (a decline, a
    # question, a deflection, a contradicting status pick). Re-proposing it
    # now would take the affordances back on the very turn the user acted on
    # them. Turn-scoped: unlike the refusal record this does not survive the
    # turn, so an offer the user only asked about returns on the next one.
    if metadata.get(_ENGINE_DISPOSITION_WITHDRAWN_KEY):
        return

    closure = assess_closure_readiness(case)
    # A decline POSTPONES the offer until the case changes underneath it.
    # Re-proposing every turn regardless is what produced five identical
    # offers against five explicit declines (fm#1122): the decline clears
    # `pending_transition`, which is the only state the guards above read, so
    # nothing carried the refusal forward. Keyed on the JUSTIFYING state, not
    # on a decline count: counting declines and giving up would be the engine
    # steering toward abandonment (D4 soft-collapse), and it would also strand
    # a case whose situation later genuinely warrants the offer again.
    signature = deferred_disposition_signature(case, closure.verdict)
    if signature in p.deferred_disposition_declined_signatures:
        return

    if closure.verdict == closure.SUGGEST_RESOLVE:
        to_state = "resolved"
        # Purpose-written, NOT `closure.message`. That text is a pivot-FROM-a-
        # close ("Closing would record it as unresolved and discard the
        # resolution"), which is coherent on the LLM path — where a close was
        # actually requested — and incoherent here, where the engine proposed
        # this turn's disposition unprompted. Reusing it would presuppose a
        # close the user never asked for and would never state the deferred-
        # implementation reason: the same prose/affordance incoherence this
        # function is being fixed to stop producing.
        gate_message = (
            "The fix on this case is confirmed to have eliminated the root "
            "cause — the cause was removed and the problem went with it — so "
            "it qualifies for **resolved**. The implementation work you "
            "flagged as out-of-band (a change request, maintenance window, or "
            "another team) is follow-up: it stays documented on the resolved "
            "case and does not need the incident held open. Shall I mark this "
            "case resolved?"
        )
        suggestions = _resolution_confirmation_suggestions()
    else:
        to_state = "closed"
        gate_message = (
            "The root cause and fix are documented, but the fix can't be applied "
            "or verified during this session — it needs out-of-band implementation "
            "(a change request, maintenance window, or another team). Shall I close "
            "this case with the solution documented for your team to apply?"
        )
        suggestions = _close_confirmation_suggestions()

    propose_transition(case=case, to_state=to_state, summary=gate_message)
    # Provenance AND payload in one key: this proposer is the only writer of
    # `justifying_signature`, so its presence identifies the offer and its
    # value is what a decline is recorded against. A separate `proposed_by`
    # tag was redundant — it duplicated a check the missing signature already
    # covers, and no test could tell the two guards apart.
    case.pending_transition["justifying_signature"] = signature
    # Unified same-turn proposal flag: keeps step 0 of
    # _check_automatic_transitions from confirming this disposition with the
    # very message that produced it (#722 same-turn-confirmation guard).
    metadata["transition_proposed_this_turn"] = True
    metadata["override_suggestions"] = suggestions
    # Rendered by the response composer, the same way the rca_infeasible
    # sibling's message is. Before this the key was written and read NOWHERE,
    # so the engine proposed a disposition the user saw only as a bare
    # confirm/decline pair with no stated reason.
    metadata["deferred_solution_gate_message"] = gate_message
    logger.info(
        f"Proposed {to_state.upper()} transition for case {case.case_id} "
        f"(solution_feasible=DEFERRED; closure_verdict={closure.verdict}; "
        f"the documented root cause and solution are preserved either way)"
    )


def _supersede_needs_on_terminal_hypothesis(
    case: "Case", terminal_hyp_id: str, current_turn: int
) -> int:
    """Deterministic engine rule: when a hypothesis reaches a TERMINAL state
    (``REFUTED`` or ``RETIRED``), remove its ID from every need's
    ``motivating_hypothesis_ids``. If the list becomes empty AND the need is
    causal-purpose AND not FULFILLED, mark the need SUPERSEDED.

    Returns the count of needs whose state was flipped to SUPERSEDED.

    Per evidence-needs-design.md §7.4:

    - Needs motivated by multiple hypotheses survive a partial sweep;
      supersession fires only when all motivators are gone.
    - ``symptom_verification`` needs have empty motivating lists by
      design (motivated by the problem statement) — they are exempt
      from this rule.
    - FULFILLED needs are never auto-superseded — they remain as the
      audit trail of what was collected.

    **Both** terminal states are swept, not retirement alone: ``REFUTED`` and
    ``RETIRED`` are equally immutable (``_apply_hypothesis_updates`` refuses to
    revive either) and equally out of the differential
    (``verification_status._residual_candidates``), so a discriminator motivated
    solely by a refuted cause discriminates nothing. Sweeping only retirement
    left those needs PENDING for the life of the case — the staleness leak this
    rule exists to prevent, since nothing else GCs an LLM-authored causal need.

    Wired as an end-of-turn sweep in ``_process_turn_impl`` over **every**
    terminal hypothesis, not a newly-terminal diff. This function is idempotent
    — the first pass removes ``terminal_hyp_id`` from every motivating list, so
    later passes hit the ``continue`` below and change nothing — so re-sweeping
    costs nothing in the steady state and needs no pre-turn snapshot. It also
    self-heals a need that is already carrying a terminal id (one that went
    terminal before this rule existed, or one sitting in the list beside a
    still-active motivator), which a diff could never reach.

    A single integration point covers every terminal write site
    (``hypothesis_manager.py`` low-confidence + anchoring-prevention +
    ``refute_hypothesis``, ``progress_monitor.py`` INCONCLUSIVE → RETIRED, and
    LLM-emitted refutation/retirement here) without threading ``case`` through
    those APIs.

    Persistence rides on the next ``repo.save(case)`` (no scoped repo
    method — needs live on the Case aggregate per Phase 1 §1.5).
    """
    superseded_count = 0
    for need in case.evidence_needs:
        if terminal_hyp_id not in need.motivating_hypothesis_ids:
            continue
        new_motivators = [
            hyp_id
            for hyp_id in need.motivating_hypothesis_ids
            if hyp_id != terminal_hyp_id
        ]
        prior_status = need.state
        if (
            not new_motivators
            and need.purpose == NeedPurpose.CAUSAL_VERIFICATION
            and need.state != NeedState.FULFILLED
        ):
            # Pydantic-frozen behavior: EvidenceNeed isn't frozen, so
            # in-place mutation is allowed and re-validated at save time.
            # The Case domain model's save path runs full validation.
            new_status = NeedState.SUPERSEDED
            new_reason = "all motivating hypotheses are terminal"
            superseded_count += 1
        else:
            new_status = need.state
            new_reason = need.superseded_reason

        # Apply the update. Use object.__setattr__-free path since
        # EvidenceNeed isn't frozen — direct attribute assignment is
        # allowed and re-runs field validators (not the model validator
        # though; the cross-field invariants are checked at save time
        # via Case.model_validate in the repository).
        need.motivating_hypothesis_ids = new_motivators
        need.state = new_status
        need.superseded_reason = new_reason
        need.revoke_obtainability_if_terminal()
        need.updated_at = datetime.now(UTC)

        if new_status == NeedState.SUPERSEDED and prior_status != NeedState.SUPERSEDED:
            try:
                from faultmaven.core.investigation.lifecycle_metrics import (
                    evidence_need_status_changed_total,
                )

                evidence_need_status_changed_total.labels(
                    from_state=prior_status.value, to_state=new_status.value
                ).inc()
            except Exception:
                # Metrics are best-effort; never block lifecycle on them.
                pass

    if superseded_count:
        logger.info(
            f"Superseded {superseded_count} causal-verification need(s) on "
            f"case {case.case_id} after hypothesis {terminal_hyp_id} became "
            f"terminal."
        )
    return superseded_count


def _sweep_needs_for_terminal_hypotheses(case: "Case") -> int:
    """Run the §7.4 supersession rule against every terminal hypothesis.

    The end-of-turn integration point, extracted so tests pin the sweep the
    engine actually runs rather than a replica of it.

    Sweeping the whole terminal set — rather than only the hypotheses that
    turned terminal this turn — is what lets a need already carrying a terminal
    motivator heal itself, and ``_supersede_needs_on_terminal_hypothesis`` is
    idempotent, so repeating the sweep every turn costs nothing once the
    motivating lists are clean.

    Returns the number of needs flipped to SUPERSEDED — 0 in the steady state.
    """
    return sum(
        _supersede_needs_on_terminal_hypothesis(case, h_id, case.current_turn)
        for h_id, h in case.hypotheses.items()
        if h.state in TERMINAL_HYPOTHESIS_STATES
    )


def _gate1_is_pending(case: "Case") -> bool:
    """Whether Gate 1 (problem-statement confirmation) is open for this case.

    Returns True when the LLM has proposed a problem statement and the user
    has not yet confirmed it. Subsumes the prior handshake-deferred-recovery
    condition: the same affordance pair is appropriate on every Gate-1-pending
    turn, not only on the recovery turn after the same-turn guard fires.

    Used by ``engine_owned_affordances`` so the engine emits the canonical
    confirmation pair deterministically regardless of LLM compliance with the
    INQUIRY prompt's confirmation-suggestion enumeration. Matches the pattern
    already established for Gate 2 and Gate 3.
    """
    if case.state != CaseState.INQUIRY:
        return False
    inq = case.inquiry
    if inq is None:
        return False
    if not inq.proposed_problem_statement:
        return False
    return not inq.problem_statement_confirmed


def _restates_standing_solution(s_item, case: "Case") -> bool:
    """Whether an emitted solution merely RESTATES one already on the case (#1136).

    Re-proposing the standing fix is what a well-behaved model does while it waits
    for the user to apply it — every turn, in the same words. Each restatement
    minted a fresh ``sol_*`` row, and a minted row counted as progress, so a case
    parked on an unapplied fix reset ``turns_without_progress`` indefinitely and no
    stall net could ever arm (observed: ``case_07a2d687f057``, turns 8-18 — eleven
    consecutive turns whose only artifact was the same fix re-offered).

    The engine already names this situation on the *action* side: INV-32's
    ``_supersede_pending_solution_offers(reason="reproposal")`` retires the standing
    pending offer when a new one arrives. This is the same judgement applied to the
    progress signal.

    Deliberately NOT a mint-time skip (the INV-36 hypothesis treatment): a
    ``Solution`` row anchors a ``ProposedAction``, which is the compliance-detection
    chain the user's later "I ran it" is matched against. Dropping the row to fix a
    counter would risk that chain. The row is kept; only ``metadata`` records
    whether it was NEW. Duplicate rows accumulating on the case is a real but
    separate defect — see the PR's follow-up note.

    Text comparison reuses ``hypothesis_statements_duplicate`` for its two
    fail-open guards, both of which matter more here than for hypotheses: the
    numeric-discriminator guard keeps a REVISED fix distinct (``-Xmx256m`` is not a
    restatement of ``-Xmx512m``), and the mutual-mirror bar keeps a more-specific
    elaboration distinct from the general fix it refines. Same ``solution_type`` is
    required as well — the same words proposed as a WORKAROUND and as a permanent
    SOLUTION are different offers.
    """
    description = getattr(s_item, "description", None)
    if not description:
        # No text to compare — fail open (treat as new), never dedup on absence.
        return False
    for standing in case.solutions or []:
        if standing.solution_type != s_item.solution_type:
            continue
        if not standing.immediate_action:
            continue
        if hypothesis_statements_duplicate(description, standing.immediate_action):
            return True
    return False


def _restates_standing_evidence(ev_item, case: "Case") -> bool:
    """Whether an emitted evidence row quotes an extract already on the case (#1136).

    The counterpart of ``_restates_standing_solution`` on the supply side: a user
    re-submitting a snapshot they already sent (or a model re-extracting the same
    lines from the same file) minted new ``ev_*`` rows, and a minted row counted as
    progress.

    The bar is **exact match after normalisation**, not the fuzzy mirror used for
    solutions, and the difference is deliberate. An evidence ``extract`` is a quoted
    span, not a paraphrase — two spans are the same datum or they are not, and a
    near-miss is far more likely to be a genuinely different span (an adjacent log
    window, the next occurrence of a repeating line) than a restatement. Source
    identity is required too: the same text observed in two different files is two
    observations, which is precisely the independent-corroboration signal the
    grading layer counts.

    Fail-open on an empty extract — a row with nothing quoted is never deduped away.
    """
    if not getattr(ev_item, "extract", None):
        return False
    key = evidence_datum_key(ev_item)
    return any(evidence_datum_key(standing) == key for standing in case.evidence or [])


#: The recovery that is true of EVERY restatement-held shape: the leading cause
#: reads as a restatement of the problem, so what moves it is a mechanism, not
#: another observation. Shared by the restatement-held handoff and by the
#: composite wall+hold turn, where the insufficient-evidence handoff substitutes
#: it for its data ask — one string, so the two turns cannot drift apart on the
#: one piece of advice that is correct on both.
_MECHANISM_MOVE = {
    "label": "Ask for the cause to be stated as a mechanism",
    "action_type": "FREE_SPEECH",
    "body": (
        "The leading explanation currently restates the problem rather than "
        "explaining it. Asking what specifically is misconfigured, exhausted, "
        "or failing — and how that produces the symptom — is what moves it "
        "forward. More data will not."
    ),
}


def _insufficient_evidence_handoff_suggestions(
    case: "Case | None" = None, *, hold=None
) -> list:
    """Deterministic structured-handoff affordances for an insufficient-evidence
    case (verification-status Phase 1).

    These are the code-guaranteed *options* half of the structured handoff (the
    boundary statement — *what specifically* is needed — stays model-authored in
    the prose). The moves are keep-engaging by construction: they invite the
    discriminating data or a fresh angle so the case never collapses into a
    fabricated cause or a silent spin — the two failure modes the handoff exists
    to prevent. They deliberately do **not** steer toward close: pausing/closing
    is the user's call (the prompt's handoff already names it as an option), and
    the engine nudging abandonment would be soft-collapse (D4). Non-clickable
    FREE_SPEECH — the user supplies the content.

    THE COMPOSITE (#1195 review). A case can reach this cell on a model-declared
    data wall while ALSO carrying a governing §7.1 restatement hold. The status
    stays ``INSUFFICIENT_EVIDENCE`` there — the wall is a real, user-declared
    boundary and the close must record it — but the DATA ASK must not survive:
    the user has already declared that data unobtainable, and the same turn
    tells the MODEL that more evidence will not validate the held root. Asking
    anyway is the exact contradiction #1195 exists to remove, reached by a
    different route. So on a governing hold the data ask is replaced by
    ``_MECHANISM_MOVE``, the one move that IS actionable there. The fresh-angle
    move survives unchanged: it asks for a DIRECTION, not a datum, and stays
    true of a walled case.

    ``case`` is optional so the pair remains constructible without one (the peer
    builders take no argument and several tests call it bare); every engine call
    site passes it, and without it the historical data-ask pair is returned.
    ``hold`` lets the caller hand over the read it already did.
    """
    # ``hold`` is passed by ``engine_owned_affordances``, which computed it
    # once for the whole call; deriving it again here would re-sweep the graph.
    if hold is None and case is not None:
        hold = restatement_hold_governs(case)
    first = (
        _MECHANISM_MOVE
        if hold is not None
        else {
            "label": "Share data that would distinguish the causes",
            "action_type": "FREE_SPEECH",
            "body": (
                "The investigation has narrowed the problem but can't ground a "
                "single cause from the current evidence. New discriminating data "
                "would let it resume."
            ),
        }
    )
    return [
        first,
        {
            "label": "Suggest a diagnostic angle not yet tried",
            "action_type": "FREE_SPEECH",
            "body": (
                "Point the investigation at an angle the differential hasn't "
                "covered — a different subsystem, timeframe, or signal."
            ),
        },
    ]


def _insufficient_evidence_handoff_pending(
    case: "Case", *, status: "VerificationStatus | None" = None
) -> bool:
    """Whether the engine should drive the insufficient-evidence structured
    handoff this turn (verification-status Phase 1).

    Code-guarded promotion of the §5.3 direction: the engine computes the
    objective, work-gated stall (``INSUFFICIENT_EVIDENCE`` — not grounded, work
    gate passed, stalled) and *drives* the handoff, rather than depending on the
    LLM to state the boundary. This is a soundness fix (the engine must not spin
    silently or fabricate a cause on a walled case), always on — not a flag-gated
    enhancement; it is validated by simulation, not toggled.

    Scoped to ``INVESTIGATING``: the reading is only meaningful mid-investigation
    (a stall in INQUIRY or a terminal case is a different concern), and this
    keeps the handoff from colliding with the Gate-1 / disposition affordances,
    which own their own states.

    Must be evaluated AFTER the deductive-validation stamp in the turn pipeline
    (``_recompute_assessment_state``) so ``assess_verification_status`` reads a
    fresh grounding grade and never pre-empts the deductive arm (the #593
    re-derive-after-stamp ordering). Both ``engine_owned_affordances`` call
    sites in ``process_turn`` satisfy this — they run well after
    ``_apply_investigation_updates``.

    NOT every work-gated stall reaches here. A stall whose only block is the
    §7.1 restatement guard reads ``RESTATEMENT_HELD`` instead (#1195) and gets
    ``_restatement_held_pending``'s moves: this handoff asks for discriminating
    data, and on that shape more data provably cannot help — the engine says so
    to the model in the same turn. The carve-out is made once, at the join, so
    this predicate and the reported status cannot disagree.
    """
    if case.state != CaseState.INVESTIGATING:
        return False
    # Cheap short-circuit before the (relatively expensive) grounding-grade
    # computation: INSUFFICIENT_EVIDENCE requires a stall, so a not-yet-stalled
    # case — the large majority of INVESTIGATING turns — can never reach the
    # handoff. Uses the full progress axis (time thresholds OR a declared data
    # wall), the same predicate ``assess_verification_status`` uses, so it cannot
    # disagree and a fully-declared wall fires the handoff immediately.
    if not is_progress_stalled(case):
        return False
    if status is None:
        status = assess_verification_status(case)
    return status == VerificationStatus.INSUFFICIENT_EVIDENCE


def _restatement_held_suggestions(case: "Case", *, hold=None) -> list:
    """Deterministic affordances for a case whose leading cause is held by the
    §7.1 RESTATEMENT guard (#1195) — the fourth peer of the insufficient-evidence
    handoff, the hypothesis-vacuum pull-back and the treatment-blocked handoff:
    same mechanism (a code-guarded branch substituting a deterministic pair
    regardless of LLM compliance), different trigger, different ask.

    Its two ungrounded peers ask for data that would GROUND a cause. Here the
    causal grounding is already in hand — in the incident, three independent
    qualifying causal supports against a bar of two, at 100% evidence coverage —
    and what blocks validation is that the ROOT's STATEMENT adds no content
    beyond the problem and the other standing hypotheses. Asking such a case for
    discriminating data is not merely unhelpful: it is the exact opposite of what
    the engine tells the MODEL in the same turn ("MORE SUPPORTING EVIDENCE WILL
    NOT VALIDATE IT" — the restatement recovery note in ``context_builder``).
    Removing that contradiction by SUPPRESSION alone would leave a silent case,
    which is the failure ``_insufficient_evidence_handoff_pending`` exists to
    prevent — so the carve-out ships with this replacement, not without one.

    The mechanism move is unconditional — it is the recovery for every held
    shape. The SIBLING move is offered only when the hold actually depends on
    another standing hypothesis (``RestatementHold.involves_siblings``). The
    frame is ``anchors | other-hypothesis tokens``, so a root that restates the
    PROBLEM STATEMENT alone is held with no two hypotheses overlapping at all —
    and telling that user "two of the causes on the table may be one cause
    worded twice" asserts an overlap that does not exist (#1195 review, finding
    5). That is the same class of wrong guidance this fix exists to remove, so
    the engine discriminates instead: re-run the novelty core with the siblings
    dropped from the frame, and offer the move only if that releases the root.

    When the move IS offered, both recoveries are named for the reason the
    model-facing note names both: the sibling-held population has two shapes the
    engine cannot tell apart (the fm#1137 known limit). A root held by a TRUE
    DUPLICATE of its own hypothesis needs the mechanism stated distinctly; a
    root held by frame DILUTION — a different cause's verbose statement
    happening to cover this one — clears the moment that alternative is settled.

    Neither move asks for data, and neither steers toward close: a hold the
    engine can describe is not a reason to abandon the case (D4 soft-collapse).
    Non-clickable FREE_SPEECH — the user supplies the content.
    """
    # ``hold`` is passed by ``engine_owned_affordances``, which has already
    # computed it: recomputing here ran the tokenization sweep a SECOND time per
    # call and opened a window where the two reads could disagree (#1195
    # review). Absent (a bare call, or a cleared hook — see verification_status)
    # it degrades to the move that is true of every shape and drops the one that
    # needs evidence for its claim: the smaller, always-true offer is the safe
    # direction.
    if hold is None:
        hold = restatement_hold_governs(case)
    moves = [_MECHANISM_MOVE]
    if hold is not None and hold.involves_siblings:
        moves.append(
            {
                "label": "Say whether the standing explanations are the same cause",
                "action_type": "FREE_SPEECH",
                # The "these two may be one cause worded twice" framing was
                # retired with fm#1122: a root whose whole overlap ONE standing
                # explanation accounts for is now released as a duplicate, so a
                # root that is still sibling-held is one the standing
                # explanations SPAN — each contributing something the others do
                # not. Ruling one out is what collapses that span; asking
                # whether they are the same cause no longer describes the
                # population this move is offered to.
                "body": (
                    "The leading explanation spans two of the causes on the "
                    "table rather than picking one. Ruling one of them out — or "
                    "saying which single mechanism is doing the work — clears "
                    "the overlap that is holding it."
                ),
            }
        )
    return moves


def _restatement_held_pending(
    case: "Case", *, status: "VerificationStatus | None" = None
) -> bool:
    """Whether the engine should drive the restatement-held handoff this turn
    (#1195 — the fourth code-guarded branch).

    Reads the SAME join as its three peers rather than calling
    ``restatement_held_root_ids`` itself, so the affordance and the reported
    status can never disagree: a case has exactly one verification status, which
    is what makes all four branches mutually exclusive by construction.

    Scoped to ``INVESTIGATING`` and ordered with its peers, below the
    state-machine gates. Must be evaluated AFTER the per-turn recompute so the
    status read is fresh (the #593 re-derive-after-stamp ordering); both
    ``engine_owned_affordances`` call sites satisfy that.
    """
    if case.state != CaseState.INVESTIGATING:
        return False
    # Cheap short-circuit before the grounding-grade computation, mirroring the
    # siblings: RESTATEMENT_HELD is carved out of the not-grounded × stalled
    # cell, so it requires the full progress axis exactly as
    # ``INSUFFICIENT_EVIDENCE`` does (time thresholds OR a declared data wall).
    if not is_progress_stalled(case):
        return False
    if status is None:
        status = assess_verification_status(case)
    return status == VerificationStatus.RESTATEMENT_HELD


def _hypothesis_vacuum_suggestions() -> list:
    """Deterministic pull-back affordances for the NOT_YET_PRODUCTIVE vacuum
    (#656 P3.1, INV-38).

    The engine's half of the corrective is the *moves*; the boundary statement —
    *why* nothing has grounded — stays model-authored in the prose. These moves
    pull the investigation back to the basis a hypothesis needs: a precise symptom
    and a place to look. They are keep-engaging by construction (re-establish the
    diagnostic direction), never steering toward close — the engine nudging
    abandonment would be soft-collapse (D4), and the vacuum is the engine's own
    failure to elicit, not the case's to give up on. Non-clickable FREE_SPEECH:
    the user supplies the content.

    Distinct from ``_insufficient_evidence_handoff_suggestions``: that fires ABOVE
    the work gate on a built differential and asks for *discriminating* data; this
    fires with ZERO hypotheses and asks for the *foundational* symptom framing
    that lets a first hypothesis form at all.
    """
    return [
        {
            "label": "Describe the expected vs. observed behavior",
            "action_type": "FREE_SPEECH",
            "body": (
                "The investigation hasn't formed a working theory yet. A sharp "
                "expected-vs-observed contrast — what should happen, what actually "
                "happens — gives it a symptom precise enough to hypothesize from."
            ),
        },
        {
            "label": "Point to where the problem shows up",
            "action_type": "FREE_SPEECH",
            "body": (
                "Name a system, signal, or recent change tied to the problem — a "
                "concrete place to look seeds the first diagnostic direction."
            ),
        },
    ]


def _hypothesis_vacuum_pending(
    case: "Case", *, status: "VerificationStatus | None" = None
) -> bool:
    """Whether the engine should drive the NOT_YET_PRODUCTIVE pull-back this turn
    (#656 P3.1, DF-6 gap A — the 0-hypothesis corner of NOT_YET_PRODUCTIVE).

    ``assess_verification_status`` returns ``NOT_YET_PRODUCTIVE`` from turn 1
    whenever the work gate hasn't passed — too early to act on, which is why that
    status "drives nothing" today (DF-6). This predicate is the corrective: once
    the vacuum has PERSISTED past the stall thresholds (``is_stalled`` — the same
    turn / no-progress floor the insufficient-evidence handoff uses), a case still
    holding ZERO hypotheses has no diagnostic direction at all, and the engine
    pulls it back toward symptom / expected-vs-observed clarification so a
    hypothesis can form. Without this the only nets for a stuck case
    (insufficient-evidence handoff, exhaustion, deadlock) each require ≥2
    hypotheses, so a model that never hypothesizes evades every one and spins
    silently — the #656 empty-graph spin (`case_5db5417fe445`: 0 hypotheses across
    the whole session, INVESTIGATING for 13 turns).

    Scoped to the true 0-hypothesis VACUUM, not the whole work-gate-failing range:
    a case holding ≥1 hypothesis already has a diagnostic direction (pulling it
    back to re-describe the symptom would be wrong — it needs breadth /
    discrimination, a different and lower-stakes concern), so the corrective is
    deliberately confined to the vacuum the incident exhibits.

    INVESTIGATING-scoped and ordered LAST beside the insufficient-evidence handoff
    (both are mid-investigation readings below the state-machine gates); the two
    are mutually exclusive by construction (this requires 0 hypotheses; that
    requires the ≥2 work gate). Must be evaluated AFTER the per-turn recompute so
    the status read is fresh (the same #593 re-derive-after-stamp ordering the
    sibling handoff requires; both ``engine_owned_affordances`` call sites
    satisfy it).
    """
    if case.state != CaseState.INVESTIGATING:
        return False
    # The vacuum is specifically ZERO hypotheses — a case with a direction is not
    # pulled back. Cheapest discriminator, checked first.
    if case.hypotheses:
        return False
    # Act only once the vacuum has PERSISTED past the stall floor, not on every
    # early NOT_YET_PRODUCTIVE turn. The declared-data-wall arm of the full
    # progress axis is vacuous here (it ranges over residual candidates, of which
    # there are none at 0 hypotheses), so the cheap time arm ``is_stalled`` is the
    # exact and sufficient stall reading.
    if not is_stalled(case):
        return False
    # Authoritative guard: a 0-hypothesis case that is somehow grounded (a chain
    # validated with no backing hypothesis) reads HEALTHY/TREATMENT_BLOCKED, not
    # NOT_YET_PRODUCTIVE, and is not a vacuum — the status join decides.
    if status is None:
        status = assess_verification_status(case)
    return status == VerificationStatus.NOT_YET_PRODUCTIVE


def _treatment_blocked_suggestions() -> list:
    """Deterministic affordances for a case that HAS a cause but cannot reach a
    verified fix (§5.1's grounded × stalled cell — "failed fix, no access, change
    window, waiting on another team").

    The third peer of the insufficient-evidence handoff and the hypothesis-vacuum
    pull-back: same mechanism (a code-guarded branch that substitutes a
    deterministic affordance pair regardless of LLM compliance), different
    trigger, different ask. Where those two ask for data that would *ground* a
    cause, this one has the cause — what it lacks is a path to *verifying the
    fix*. Asking such a case for more diagnostic data is the wrong question, and
    was the observable symptom before this branch existed: the engine either
    re-offered a close every turn or restated the same fix and waited.

    **Names the blocker, never proposes a disposition.** Offering to close here
    would resurrect through the affordance channel exactly the deferred-close nag
    #1138 removed (five offers against five typed declines). Disposition stays
    with the disposition gate, which is checked first in
    ``engine_owned_affordances`` — so before a decline that gate owns the turn,
    and after it these moves take over without re-asking the settled question.
    Keep-engaging by construction (D4: the engine must never steer toward
    abandonment).

    Both sub-shapes of the cell are covered: a fix proposed but not yet applied
    (blocked on access, a window, or another team), and a grounded cause with no
    fix on the table yet. Non-clickable FREE_SPEECH — the user supplies the
    content.
    """
    return [
        {
            "label": "Say what's blocking the fix",
            "action_type": "FREE_SPEECH",
            "body": (
                "The investigation has a cause but can't confirm a fix from here. "
                "Naming what stands in the way — access, a change window, another "
                "team, or a fix already tried that didn't hold — lets it work the "
                "blocker instead of re-asking for data."
            ),
        },
        {
            "label": "Report what happened when the fix was applied",
            "action_type": "FREE_SPEECH",
            "body": (
                "If the change went in, its outcome is the decisive observation — "
                "what recovered, what didn't, or what broke instead. If it hasn't "
                "gone in yet, say so and the case holds without re-asking."
            ),
        },
    ]


def _treatment_blocked_pending(
    case: "Case", *, status: "VerificationStatus | None" = None
) -> bool:
    """Whether the engine should drive the treatment-blocked handoff this turn
    (#1136 — the third code-guarded branch).

    ``TREATMENT_BLOCKED`` was unreachable in-flight before #1136 (the grounding
    axis required ``CONFIRMED``, which only the resolution confirm-stamp mints),
    so the cell drove nothing because nothing ever landed in it. Making the axis
    read "any validated root" lands the **most common stall shape** there — a
    mechanistically identified cause waiting on a fix — and a reachable cell that
    drives nothing is the same defect this issue exists to close, one cell over.

    Scoped to ``INVESTIGATING`` and ordered with its peers, below the
    state-machine gates. Mutually exclusive with them by construction: all four
    read the same join, and a case has exactly one verification status.

    Must be evaluated AFTER the per-turn recompute so the status read is fresh
    (the #593 re-derive-after-stamp ordering); both
    ``engine_owned_affordances`` call sites satisfy that.
    """
    if case.state != CaseState.INVESTIGATING:
        return False
    # Cheap short-circuit before the grounding-grade computation, mirroring the
    # sibling handoff: TREATMENT_BLOCKED requires a stall. The plain time arm is
    # the exact reading here — the declared-data-wall arm belongs to the
    # not-grounded branch (it is about failing to GROUND a cause, which this cell
    # has already done), exactly as ``assess_verification_status`` scopes it.
    if not is_stalled(case):
        return False
    if status is None:
        status = assess_verification_status(case)
    return status == VerificationStatus.TREATMENT_BLOCKED


def _schema_prompt_instruction(schema: dict) -> str:
    """In-prompt schema block for providers that need the schema in prompt
    text (json_object / prompt_only strategies).

    ``SCHEMA_INSTRUCTIONS`` documents the investigation-turn output shape —
    ``internal_reasoning``, milestone/outcome ``state_updates``, 2-4
    ``suggested_follow_ups``. Response models that don't carry that shape
    (TerminalResponse, InquiryResponse) must not receive it: instructing
    "outcome: REQUIRED" against a schema with no such field, or "2-4
    suggestions" on a turn whose template says to leave them empty, misleads
    exactly the weak providers this path serves. The gate keys on the schema
    itself — does it declare ``internal_reasoning``? — so any future model
    gets the block iff it actually has the documented shape, rather than by
    class pedigree. The exact JSON schema remains the authority either way.
    """
    instructions = (
        f"{SCHEMA_INSTRUCTIONS}\n"
        if "internal_reasoning" in schema.get("properties", {})
        else ""
    )
    schema_json = json.dumps(schema, indent=2)
    return (
        f"\n\n{instructions}"
        "You MUST respond with valid JSON matching this exact schema:\n\n"
        f"```json\n{schema_json}\n```\n\n"
        "IMPORTANT:\n"
        "- Use the exact field names shown in the schema\n"
        "- Do not add extra fields not in the schema\n"
        "- Do not include any text before or after the JSON\n"
        "- Ensure all required fields are present\n"
    )


#: Which verification status each mid-investigation gate reports on the turn it
#: fires. A dict rather than the if/elif chain it replaces: the labels are
#: produced in ``engine_owned_affordances`` and consumed at the return boundary,
#: so a gate added in one place and forgotten in the other used to fall through
#: in SILENCE — the turn simply carried no status. Two gates
#: (``treatment_blocked``, ``restatement_held``) were added that way before this
#: map existed. The state-machine gates (``disposition``, ``gate1``) are absent
#: on purpose: they are handshakes, not readings of the join, and have no status
#: to report.
#:
#: ``insufficient_evidence_restatement_held`` maps to INSUFFICIENT_EVIDENCE
#: deliberately — it is the same disposition wearing a different affordance
#: pair, and the turn metadata must agree with the persisted status.
_GATE_VERIFICATION_STATUS: dict[str, VerificationStatus] = {
    "insufficient_evidence": VerificationStatus.INSUFFICIENT_EVIDENCE,
    "insufficient_evidence_restatement_held": (
        VerificationStatus.INSUFFICIENT_EVIDENCE
    ),
    "restatement_held": VerificationStatus.RESTATEMENT_HELD,
    "not_yet_productive": VerificationStatus.NOT_YET_PRODUCTIVE,
    "treatment_blocked": VerificationStatus.TREATMENT_BLOCKED,
}


def engine_owned_affordances(
    case: "Case", metadata: dict[str, Any] | None = None
) -> tuple[str, list] | None:
    """Return ``(gate_name, affordance_list)`` when a state-machine gate is pending.

    The state machine has a small enumerable set of gates: imperative
    pending_transition (set by ``propose_transition`` via
    ``metadata['override_suggestions']``) and Gate 1 (problem-statement
    confirmation). When a gate is pending, the engine knows the canonical
    affordance pair; the LLM cannot add value there and shouldn't try.

    Gate 2 (investigation path) and Gate 3 (post-mitigation continuation)
    were removed (redesign R5): there is no prospective path fork, and a
    mitigation simply continues the flow when verified.

    Returns ``None`` when no gate is pending — the LLM's own DECIDE /
    EVIDENCE / FREE_SPEECH suggestions pass through unmodified.

    Gate identifiers (telemetry-stable labels):
      - ``"disposition"`` — pending_transition / propose_transition override
      - ``"gate1"`` — problem-statement confirmation
      - ``"insufficient_evidence"`` — work-gated stall with no grounded cause
        (code-guarded, always on)
      - ``"restatement_held"`` — a work-gated stall whose leading cause is held
        by the §7.1 restatement guard alone: the block is the cause's PHRASING,
        not missing data, so the moves ask for a distinct restatement rather
        than for evidence (#1195, code-guarded, always on)
      - ``"not_yet_productive"`` — persisted 0-hypothesis vacuum; a pull-back to
        symptom / expected-vs-observed clarification (code-guarded, always on)

    The disposition branch sits above gate1 because pending_transition can
    fire while gate1 is technically open. The mid-investigation readings sit
    LAST — any pending state-machine handshake (disposition, gate1) takes
    precedence over them — and are mutually exclusive with each other: all four
    read the same ``assess_verification_status`` join, and a case has exactly one
    verification status. Their order among themselves is therefore presentational
    only; ``restatement_held`` is written beside ``insufficient_evidence`` because
    it is the cell carved out of it.
    """
    md = metadata or {}

    if md.get("override_suggestions"):
        return ("disposition", md["override_suggestions"])

    if _gate1_is_pending(case):
        return ("gate1", _investigation_confirmation_suggestions())

    # The four mid-investigation readings below all ask the SAME join, and each
    # used to recompute it — across the two ``engine_owned_affordances`` call
    # sites that is up to eight recomputes per turn, each now carrying a
    # causal-graph tokenization sweep (#1195 review). Compute it ONCE here and
    # hand it down: cheaper, and it makes the mutual exclusivity structural
    # rather than merely argued — the four branches read one value. The
    # predicates keep their own cheap pre-checks, and each still computes the
    # status itself when called directly (tests, and any future caller that has
    # not got one).
    #
    # BOTH cheap guards are hoisted with it, not dropped (#1195 review). All
    # four readings require INVESTIGATING, and all four require a stall — the
    # two ungrounded ones via the full progress axis, the other two via its time
    # arm, which it subsumes. Without them here an ordinary PROGRESSING turn
    # would newly pay ``grade_cause_assurance`` plus a ``work_gate_passed``
    # rebuild of every evidence datum key, twice a turn, where each predicate
    # previously returned early. ``is_progress_stalled`` is exactly what
    # ``_insufficient_evidence_handoff_pending`` already ran first, so this
    # restores the pre-existing cost profile rather than adding to it.
    if case.state != CaseState.INVESTIGATING:
        return None
    if not is_progress_stalled(case):
        return None

    status = assess_verification_status(case)
    # ONE hold read per call, hoisted so neither branch below re-derives it: two
    # reads of the same fact in one turn is both a second tokenization sweep and
    # a window where they could disagree. Computed only for the two statuses
    # that can use it — the other branches would pay a graph sweep for an answer
    # they never look at. (The join above derives it once more internally on its
    # way to RESTATEMENT_HELD; threading that back out would mean returning a
    # tuple from a function whose whole contract is "one value per case", so
    # that second read stands, and the sweep-count pins record it.)
    hold = (
        restatement_hold_governs(case)
        if status
        in (
            VerificationStatus.INSUFFICIENT_EVIDENCE,
            VerificationStatus.RESTATEMENT_HELD,
        )
        else None
    )

    if _insufficient_evidence_handoff_pending(case, status=status):
        # The composite (#1195 review): a declared data wall AND a governing
        # restatement hold. Same status, same disposition — but a different pair
        # and its own telemetry label, because a turn that silently swaps its
        # advice is a turn nobody can measure, and #1140's whole lesson was that
        # an unobservable hold costs a database read to find.
        gate = (
            "insufficient_evidence_restatement_held"
            if hold is not None
            else "insufficient_evidence"
        )
        return (gate, _insufficient_evidence_handoff_suggestions(case, hold=hold))

    if _restatement_held_pending(case, status=status):
        return ("restatement_held", _restatement_held_suggestions(case, hold=hold))

    if _hypothesis_vacuum_pending(case, status=status):
        return ("not_yet_productive", _hypothesis_vacuum_suggestions())

    if _treatment_blocked_pending(case, status=status):
        return ("treatment_blocked", _treatment_blocked_suggestions())

    return None


#: Metadata key: the engine's OWN disposition offer was withdrawn during this
#: turn. Turn-scoped and never persisted — it says nothing about whether the
#: user refused, only that re-proposing the same offer later in the SAME turn
#: would be taking back an affordance the user just acted on.
_ENGINE_DISPOSITION_WITHDRAWN_KEY = "engine_disposition_withdrawn_this_turn"

#: How many refused deferred-disposition signatures a case carries. Bounds the
#: progress blob; large enough that an oscillation between a handful of
#: justifying states cannot evict a signature the user is still refusing.
_MAX_DECLINED_DISPOSITION_SIGNATURES = 8


def _note_engine_disposition_withdrawn(case: "Case", metadata: dict) -> None:
    """Mark the engine's own disposition offer as withdrawn for the rest of
    this turn.

    Withdrawal is not refusal — the durable record is a separate, narrower
    decision. This exists because the withdrawing branches fall through to
    normal processing, which reaches ``_maybe_propose_deferred_close`` again
    on the SAME turn and re-proposes the offer the user just moved past,
    re-taking the affordances with it (fm#1122). Scoped to the turn so an
    offer the user merely asked a question about is back on the next one.
    """
    if (getattr(case, "pending_transition", None) or {}).get("justifying_signature"):
        metadata[_ENGINE_DISPOSITION_WITHDRAWN_KEY] = True


def _record_deferred_disposition_decline(case: "Case") -> None:
    """Persist that the user refused THIS proposer's offer, against the state
    that justified it. No-op for any other pending transition — a decline of an
    LLM-initiated or user-initiated disposition says nothing about the
    engine-initiated one.

    Called from the withdrawal paths that constitute a REFUSAL, which is more
    than the explicit-decline branch: a contradicting status pick names a
    different target, and a long non-answer that is not a question is a
    deflection ("we'll do it in Friday's maintenance window"). Cancelling
    those unrecorded lets the proposer re-fire from unchanged state (fm#1122).

    NOT called for a question. ``message_is_substantive`` is true for ANY
    message containing "?" — "what happens to the runbook if I close this?"
    is a user deciding, not declining, and recording it would make the
    affordance vanish, unexplained, until a premise moved. The same-turn
    re-take those messages would otherwise cause is handled by
    ``_note_engine_disposition_withdrawn`` instead, which expires with the
    turn.
    """
    pending = getattr(case, "pending_transition", None) or {}
    # Only THIS proposer writes `justifying_signature`, so its absence means
    # the standing offer came from the LLM or the user — declining that says
    # nothing about the engine-initiated one and must not suppress it.
    signature = pending.get("justifying_signature")
    if not signature or not getattr(case, "progress", None):
        return
    declined = case.progress.deferred_disposition_declined_signatures
    if signature in declined:
        return
    declined.append(signature)
    # Bounded: a case that oscillates between two justifying states could
    # otherwise grow the progress blob without limit. Oldest first — the
    # signatures most likely to recur are the recent ones.
    del declined[:-_MAX_DECLINED_DISPOSITION_SIGNATURES]
    logger.info(
        "Deferred-implementation disposition refused for case %s; not "
        "re-proposing until the justifying state changes (signature=%s)",
        case.case_id,
        signature,
    )


def _prose_with_gate_notice(llm_text: str | None, gate_text: str) -> str:
    """Compose an engine gate message WITH the LLM's reply instead of over it.

    Engine-owned gate turns (resolution needs-info, close pivot,
    RCA-infeasible closure) override the response so the user sees the
    canonical gate prompt. But the LLM's ``agent_response`` on those turns
    often carries the substantive analysis the user just asked for — and its
    ``state_updates`` are already applied and persisted — so replacing the
    prose wholesale makes the engine appear to have ignored the question
    while the case record shows it answered (#656 turns 10-11: configmap
    analyses created hypotheses+solutions, yet the transcript showed only
    the canned resolution ask). The prose is therefore preserved and the
    gate message appended below a separator.

    Scope: prose ONLY. Follow-up *suggestions* on gate turns remain
    engine-owned and are still replaced outright — that is a separate,
    deliberate ownership decision (the #428 "augment" experiment was
    reverted by #430). Do not extend this composition to suggestions.
    """
    llm_text = (llm_text or "").strip()
    if not llm_text:
        return gate_text
    return f"{llm_text}\n\n---\n\n{gate_text}"


# INV-40 (§7.9) / INV-15 (§1.3.1): the disposition-completion phrases scanned by
# BOTH the narration-truth guard (``_narration_overclaim_notice``) and the
# ``transition_compliance`` telemetry. Module-level so the two read the SAME
# narrow list — PR #299 ratified keeping this scan narrow (only high-signal
# transition-completion claims; the broader advisor-role banned list is NOT here
# because it false-positives in benign context). Adding a phrase widens both the
# guard and the telemetry; do so deliberately.
_COMPLETION_PHRASES: tuple[str, ...] = (
    "case closed",
    "case is closed",
    "case is now closed",
    "marking as resolved",
    "marking this as resolved",
    "marking this resolved",
    "marked as resolved",
    "case resolved",
    "case is resolved",
    "case is now resolved",
    "i have resolved",
    "i've resolved",
    "i have closed",
    "i've closed",
)


def _narration_asserts_disposition(agent_text: str | None) -> bool:
    """True if the finalized narration contains a disposition-completion phrase.

    The detector half of INV-40 and the ``transition_compliance`` telemetry —
    the same narrow ``_COMPLETION_PHRASES`` scan, reused verbatim (INV-15).
    """
    lowered = (agent_text or "").lower()
    return any(p in lowered for p in _COMPLETION_PHRASES)


# INV-40 corrective notice. Appended (never substituted) below over-claiming
# prose. Both variants are true on a false positive too (conditional/quoted
# prose): the case IS non-terminal, so the worst case is a mildly-
# redundant-but-true notice (§7.9 graceful denial).
#
# Phase-neutral wording. The guard fires on any non-terminal turn — including
# INQUIRY (intake), which reaches the same response-composition block — so the
# notice must be true in both INQUIRY and INVESTIGATING. It therefore asserts
# only "not resolved/closed — still open" (true in every non-terminal phase),
# NOT "under investigation" (false during intake).
#
# Two wordings because the over-claim reaches the guard in two truth-shapes:
#   - no pending transition — the plain over-claim (#668's shape): nothing is
#     even on the table, so point at what resolution requires.
#   - a transition IS proposed (the LLM narrated "resolved" AND emitted
#     proposed_transition this turn, the suggestions-only override branch that
#     appends no gate prose): the claim is premature, not false-forever — the
#     confirm/decline affordances are right below, so point the user at them.
_NARRATION_OVERCLAIM_NOTICE = (
    "**Note:** this case has not been resolved or closed — it is still open. "
    "Resolving it requires a confirmed root cause and a verified fix; closing it "
    "requires an explicit decision to stop. I'll surface the confirm-to-resolve "
    "step when the case actually reaches it."
)
_NARRATION_OVERCLAIM_NOTICE_PENDING = (
    "**Note:** this case is not resolved or closed yet — a transition has only "
    "been *proposed*. It takes effect only when you confirm it using the options "
    "below."
)


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


def _build_resolution_confirmation(case) -> str:
    """Build the resolution confirmation prompt with optional enrichment hints.

    Shows what we have on record (root cause + solution) and suggests
    additional details that would improve the resolution documentation
    and any runbook generated from it. Makes clear these are optional.
    """
    parts = [
        "Here's what I have on record:\n",
        f"- **Root cause**: {_get_root_cause_summary(case)}",
        f"- **Solution**: {_get_solution_summary(case)}",
    ]

    # Check what enrichment data is missing — these improve docs but don't block resolution
    enrichment_hints = []

    evidence_count = len(case.evidence) if case.evidence else 0
    if evidence_count == 0:
        enrichment_hints.append("diagnostic evidence (logs, metrics, error messages)")

    has_verification = False
    if case.solutions:
        has_verification = any(
            getattr(s, "verification_method", None) for s in case.solutions
        )
    if not has_verification:
        enrichment_hints.append("how you verified the fix worked")

    has_commands = False
    if case.solutions:
        has_commands = any(
            getattr(s, "commands", None) or getattr(s, "implementation_steps", None)
            for s in case.solutions
        )
    if not has_commands:
        enrichment_hints.append("specific commands or steps you used")

    if enrichment_hints:
        parts.append(
            "\nThis is enough to resolve. If you'd like to improve the documentation "
            "(and any runbook generated from it), you can also share:"
        )
        for hint in enrichment_hints:
            parts.append(f"- {hint}")
        parts.append("\nConfirm to resolve now, or share more details first.")
    else:
        parts.append(
            "\nIs this correct? Once you confirm, I'll mark the case as resolved."
        )

    return "\n".join(parts)


def _resolution_confirmation_suggestions() -> list:
    """Generate DECIDE follow-up suggestions for resolution confirmation.

    Mirrors the INQUIRY confirmation pattern: one positive (confirm resolution)
    and one mild negative (continue investigating).

    Each suggestion carries an ``intent`` dict so the frontend can send the
    click as IntentType.CONFIRMATION instead of plain text. This routes
    through the deterministic _handle_confirmation() path, bypassing the
    tool loop and pattern matching entirely.
    """
    return [
        {
            "label": "Yes, mark as resolved",
            "action_type": "DECIDE",
            "payload": "Yes, the issue is resolved. Please mark this case as resolved.",
            "body": "Confirm resolution and close the investigation.",
            "intent": {"type": "confirmation", "confirmation_value": True},
        },
        {
            "label": "Not yet, continue investigating",
            "action_type": "DECIDE",
            "payload": "Not yet — I'd like to continue investigating before resolving.",
            "body": "Decline resolution and continue refining the root cause or exploring alternative solutions.",
            "intent": {"type": "confirmation", "confirmation_value": False},
        },
    ]


def _close_confirmation_suggestions() -> list:
    """Generate DECIDE follow-up suggestions for close (abandon) confirmation.

    Mirrors the INQUIRY and RESOLVED confirmation patterns: one positive
    (confirm close) and one mild negative (continue investigating).

    Note: the confirmation prompt is purely about the irreversibility of
    closing. The summary is a downstream Dashboard artifact; mentioning it
    here would either promise unconditionally (sometimes false, when the
    substance gate skips) or muddy the decision the user is being asked to
    make. The body text deliberately stays silent about the report.
    """
    return [
        {
            "label": "Yes, close this case",
            "action_type": "DECIDE",
            "payload": "Yes, close this case without resolution.",
            "body": "Confirm closing the case. Closing is irreversible — the case becomes read-only.",
            "intent": {"type": "confirmation", "confirmation_value": True},
        },
        {
            "label": "Not yet, continue investigating",
            "action_type": "DECIDE",
            "payload": "Not yet — I'd like to continue investigating.",
            "body": "Keep the investigation open and continue working toward a solution.",
            "intent": {"type": "confirmation", "confirmation_value": False},
        },
    ]


def _terminal_confirmation_response(case) -> str:
    """Deterministic status line after a transition is confirmed.

    Closure-reason-aware so the user can tell at a glance what was preserved.
    The terminal reply is composed by ``_compose_terminal_reply`` which
    appends the auto-generated summary content (when produced).
    """
    if case.state == CaseState.RESOLVED:
        return "Case resolved."

    closure_reason = getattr(case, "closure_reason", "") or ""
    if closure_reason == "inquiry_only":
        return "Case closed without investigation."
    if closure_reason == "closed_insufficient_evidence":
        return (
            "Case closed — insufficient evidence to ground a cause. "
            "Residual candidates and the missing data are preserved in the "
            "closure summary."
        )
    if closure_reason == "closed_restatement_held":
        return (
            "Case closed — a cause was supported by the evidence but never "
            "stated distinctly from the problem. The candidates and what the "
            "cause still needs are preserved in the closure summary."
        )
    if closure_reason == "solution_deferred":
        return (
            "Case closed — cause identified and fix documented; implementation "
            "is deferred out-of-band."
        )
    if closure_reason == "closed_rca_infeasible":
        return (
            "Case closed — the root cause is not reachable for this problem; "
            "the mitigation stands as the accepted strategy."
        )
    if closure_reason == "mitigation_sufficient":
        return (
            "Case closed — stabilized by a verified mitigation; root-cause "
            "analysis was deferred."
        )
    return "Case closed."


def _compose_terminal_reply(case, summary_payload: str | None) -> str:
    """Compose the closure-turn chat reply for the *deterministic* paths.

    Used by the two paths where the engine controls the reply text
    directly: the explicit confirm-button path and the dropdown-resolution
    path. Prepends a deterministic status line (e.g. "Case closed.") and
    appends the auto-generated summary content (or skip / failure note).

    Not used by the LLM-driven transition path (end of process_turn), where
    the LLM has already produced narrative text for the turn — that path
    appends ``summary_payload`` directly to the LLM's text. The end-state
    chat content is equivalent (status line / LLM narrative, then the
    summary inline) but the composition site differs.

    ``summary_payload`` may be:
      - The rendered summary markdown (gate PASS, generation succeeded).
      - A skip note (gate FAIL — low-substance closure).
      - A failure note (gate PASS, LLM error).
      - None (no report service configured — stays silent).
    """
    status_line = _terminal_confirmation_response(case)
    if not summary_payload:
        return status_line
    return f"{status_line}\n\n{summary_payload}"


REGENERATE_RESOLUTION_SUMMARY_PAYLOAD = (
    "Regenerate the resolution summary report for this case"
)

REGENERATE_CLOSURE_SUMMARY_PAYLOAD = (
    "Regenerate the closure summary report for this case"
)

GENERATE_RUNBOOK_PAYLOAD = "Generate a runbook from this resolved case"

#: The explicit-confirmation payload for the SIMILAR_FOUND stop: dedup found a
#: ≥0.70 match, the turn named it and created nothing, and this affordance is
#: what makes the question answerable on the next turn. Routes back into
#: ``_handle_runbook_creation`` with ``dedup_confirmed=True`` — the user has
#: seen the candidate and chosen, so the similar-match stop (and only that
#: stop) is waived.
GENERATE_RUNBOOK_ANYWAY_PAYLOAD = "Generate a new runbook anyway"


def _generate_runbook_anyway_suggestion() -> dict:
    """The DECIDE affordance offered on the SIMILAR_FOUND stop turn."""
    return {
        "label": "Generate a new runbook anyway",
        "action_type": "DECIDE",
        "payload": GENERATE_RUNBOOK_ANYWAY_PAYLOAD,
        "body": ("Create a new draft even though a similar runbook already exists."),
    }


def _runbook_suggestion(case) -> dict | None:
    """The runbook-generation DECIDE suggestion (RESOLVED-only), gated on the
    canonical ``runbook_conversion_ready`` predicate so no button is offered
    whose only outcome is a refusal (#695 Defect A item 3). The affordance and
    the action-time readiness gate share one predicate, so the offer boundary and
    the enforcement boundary cannot drift (#698): a case is offered iff
    ``assess_runbook_readiness`` would not return NOT_SUITABLE. That means both
    the soundness half (CONFIRMED cause — counterfactually borne out) and the
    substance half (a problem definition and an actionable solution) must hold;
    a CONFIRMED-but-content-thin case is suppressed here rather than
    offered-then-denied at action time. Returns None when not offerable. (The
    manual POST /knowledge/runbooks/create path still exists for those cases;
    adding a redirect affordance is a separate suggestion-contract change, out of
    scope.)

    Also suppressed when the confirmed cause was SEEDED from an existing runbook
    (Phase 5.2b provenance-based uniqueness): generating one would only duplicate
    the runbook the case was resolved by applying. That is a knowledge-lifecycle
    decision, not a safety gate — the manual create path and the async
    similarity dedup (which surfaces a ≥70% match by title and score for the
    user to judge) both remain for the residual
    false-negatives (a reused node never restamped, a benign dedup overlap, a
    retrieval miss).
    """
    if not runbook_conversion_ready(case):
        return None
    # Cheap SYNC provenance read via the single offer-gate helper the
    # provenance-blindness invariant carves out for this module. Closes the #695
    # offered-then-refused drift at the offer boundary rather than only at
    # action time (where the async similarity dedup runs).
    from faultmaven.core.investigation.kb_cause_seeder import (
        confirmed_root_seed_origin,
    )

    if confirmed_root_seed_origin(case):
        return None
    return {
        "label": "Generate runbook from this case",
        "action_type": "DECIDE",
        "payload": GENERATE_RUNBOOK_PAYLOAD,
        "body": "Create a reusable troubleshooting runbook from the root cause and solution.",
    }


def _regenerate_resolution_summary_suggestion(remaining: int) -> dict | None:
    """Regenerate-resolution-summary DECIDE suggestion.

    Returns None when ``remaining <= 0`` so the caller can drop the
    affordance from the list entirely — the user has exhausted the
    per-type regeneration cap (MAX_REGENERATIONS). The remaining count
    drives the show/hide decision but is intentionally NOT surfaced in
    the label or body. With a low cap, exposing the count adds a
    ticking-clock feel without helping the user choose.
    """
    if remaining <= 0:
        return None
    return {
        "label": "Regenerate resolution summary",
        "action_type": "DECIDE",
        "payload": REGENERATE_RESOLUTION_SUMMARY_PAYLOAD,
        "body": "Re-create the resolution report.",
    }


def _resolved_ack_suggestions(case) -> list:
    """Suggestions for the resolution-acknowledgment turn.

    The summary was just generated and is rendered inline above in this
    same agent reply — offering "Regenerate" beside it would be noise.
    Only the forward action (runbook) is offered here, and only when the
    cause is CONFIRMED (else the runbook affordance would refuse — #695). Regen
    is reserved for subsequent terminal Q&A turns via ``_resolved_suggestions``.
    """
    runbook = _runbook_suggestion(case)
    return [runbook] if runbook is not None else []


def _select_ack_follow_ups(case, summary_failed: bool, remaining: int) -> list:
    """Choose follow-up suggestions for the closure-acknowledgment turn.

    Success path: minimal suggestions per ``_resolved_ack_suggestions`` /
    ``[]`` for CLOSED — the summary is rendered inline, so a regen card
    next to it would be noise.

    Failure path (G2): include the standard terminal Q&A suggestions —
    ``_resolved_suggestions`` (regen + runbook) for RESOLVED, or
    ``_closed_suggestions`` (regen when substance gate passes) for CLOSED.
    Generation reaches the failure branch only when generation was
    attempted (so the substance gate has already PASSED for CLOSED),
    which means ``_closed_suggestions`` will return a non-empty list with
    the regen affordance — assuming the regen cap has not yet been hit.
    The "noise next to inline summary" rationale doesn't apply when
    there's no inline summary — only a failure note.

    ``remaining`` is the per-type regeneration count remaining
    (precomputed by the caller). Drives both the label suffix and the
    "hide when exhausted" gate inside the per-type suggestion builders.
    """
    if summary_failed:
        if case.state == CaseState.RESOLVED:
            return _resolved_suggestions(case, remaining)
        if case.state == CaseState.CLOSED:
            return _closed_suggestions(case, remaining)
        return []
    if case.state == CaseState.RESOLVED:
        return _resolved_ack_suggestions(case)
    return []


def _resolved_suggestions(
    case, remaining: int, runbook_already_exists: bool = False
) -> list:
    """Suggestions for terminal Q&A turns on a RESOLVED case.

    Both the regen affordance and the runbook affordance are offered.
    The regen path serves as the chat-side recovery if initial generation
    failed and as a way to iterate; the runbook path is the forward
    action. Symmetric with ``_closed_suggestions`` for CLOSED cases.

    Each affordance has its own cap and is dropped silently when exhausted:
      - Regen: per-type ``MAX_REGENERATIONS`` (drives ``remaining``).
      - Runbook: one generation per case, and only when the cause is CONFIRMED
        (else the affordance would refuse — #695 Defect A). After a draft has
        been written the suggestion is hidden; the user iterates on it via the
        Dashboard Drafts editor (no re-roll from chat).
    """
    suggestions: list = []
    regen = _regenerate_resolution_summary_suggestion(remaining)
    if regen is not None:
        suggestions.append(regen)
    if not runbook_already_exists:
        runbook = _runbook_suggestion(case)
        if runbook is not None:
            suggestions.append(runbook)
    return suggestions


def _closed_suggestions(case, remaining: int) -> list:
    """Suggestions offered on terminal Q&A turns for a CLOSED case.

    Returned only on subsequent terminal Q&A turns — NOT on the
    closure-acknowledgment turn itself (that turn's reply renders the
    summary inline; offering "Regenerate" beside the freshly-generated
    summary is noise). Callers must respect that.

    The regenerate affordance is offered when:
      1. The substance gate PASSES (closure summary is something the
         engine would actually generate), AND
      2. ``remaining > 0`` (the per-type regen cap has not been hit).

    The substance gate handles "is there anything to summarize?"; the
    remaining count handles "has the user used up their regen budget?".
    Both gates must pass for the affordance to render.

    Runbooks are intentionally not offered for CLOSED cases — they require
    a confirmed root cause + verified solution, which RESOLVED implies and
    CLOSED does not.
    """
    from faultmaven.core.investigation.terminal_transitions import (
        should_generate_terminal_summary,
    )

    if not should_generate_terminal_summary(case):
        return []
    if remaining <= 0:
        return []
    return [
        {
            "label": "Regenerate closure summary",
            "action_type": "DECIDE",
            "payload": REGENERATE_CLOSURE_SUMMARY_PAYLOAD,
            "body": (
                "Re-create the closure report. View the current report in the Dashboard."
            ),
        },
    ]


# =============================================================================
# Milestone Engine - Main Implementation
# =============================================================================


# The kb_qa relay wrapper, split so the truncation path can protect the tail.
# The SUFFIX is instructions, not prose: the citation format and "return via the
# schema tool, do not reply with plain text". Head-first truncation would delete
# how the model is told to answer, so its length is needed at the truncation
# site as well as at the formatting site.
KB_QA_RELAY_PREFIX = (
    "KNOWLEDGE BASE RESULT — Place the content below into the "
    "`agent_response` field of your structured response. Preserve "
    "key details, diagnostic steps, and resolution procedures — do "
    "NOT collapse it into a single sentence.\n\n"
)
# Appended to a kb_qa answer that ``_format_tool_result`` trimmed to fit the
# relay wrapper (#1086). Named rather than inlined because the tool loop reads
# it back: a result carrying this marker has ALREADY been measured into the
# tool-result budget metrics at the formatter, against its true pre-trim size,
# and must not be measured a second time at the cut site (#1088).
KB_QA_ANSWER_TRUNCATED_MARKER = "\n[answer truncated]"

KB_QA_RELAY_SUFFIX = (
    "\n\n"
    "SOURCE CITATION: At the end of `agent_response`, append a "
    "compact source line in italic markdown using this exact format:\n"
    "*Sources: [title1], [title2]*\n"
    "Use only the primary source title(s) from the content above. "
    "One short line — no verbose attribution paragraph.\n\n"
    "Then return the structured response by calling the response "
    "schema tool. Do not reply with plain text."
)


# Fraction of the answer allowance reserved for the answer's TAIL when a kb_qa
# answer overflows it.
#
# The head-first cut this replaces was wrong for this one payload, for two
# reasons that are properties of the payload rather than of the cap:
#
# 1. The synthesis prompt is written to load the tail. It asks the model to
#    "preserve procedural detail -- include full diagnostic steps, commands,
#    and resolution procedures" and to "compress only background context,
#    never actionable steps". The tail of a procedure is its remediation, so a
#    head-keeping cut deletes exactly what the prompt was written to protect
#    and keeps the background it was told to compress.
# 2. ``UnifiedKBConfig.format_response`` appends the source list to the very
#    end of the answer, and ``KB_QA_RELAY_SUFFIX`` then instructs the model to
#    cite "the primary source title(s) from the content above". A head-keeping
#    cut removes that line before the model reads the instruction depending on
#    it, so on a trimmed answer the citation requirement is unsatisfiable from
#    the content it names.
#
# 0.35 rather than a smaller share because the tail has to hold a whole
# remediation section plus the source line, not just the last paragraph.
# Measured against the run that produced #1088's numbers: answers overflowed
# the allowance by 540-1249 characters (7-17% of the answer), so a 35% tail
# reservation puts every observed elision strictly inside the middle -- the
# preserved tail is real answer text in every case seen, not padding.
#
# Those overflows are CENSORED LOWER BOUNDS, not a demand distribution. The run
# predates #1094: synthesis was capped at 2000 tokens with no retry, and three
# of the five answers sit within a few percent of what 2000 tokens can write.
# So what was measured is how far past the budget a capped answer reached, not
# how long the answer wanted to be, and a post-#1094 run should be expected to
# show a wider band. The mechanism does not depend on the number -- the elide
# fires whatever the overflow, and the budget is hard-bounded either way -- but
# do not treat 0.35 as tuned without re-measuring. See
# docs/operations/monitoring/tool-result-budget.md.
KB_QA_ANSWER_TAIL_SHARE = 0.35

# Marks where content was removed. Carries the count because the model is asked
# to relay this answer onward and "some of the middle is missing" is a different
# instruction from "the answer ends here" -- which is what the end-anchored
# KB_QA_ANSWER_TRUNCATED_MARKER alone used to imply.
KB_QA_ANSWER_ELIDED_TEMPLATE = (
    "\n\n[... {dropped:,} characters elided from the middle of this answer to "
    "fit the relay budget. What follows is the TAIL of the answer as it was "
    "received — its closing steps and source line, where it reached them. "
    "...]\n\n"
)
# "as it was received", not "the end of the answer", and that hedge is
# load-bearing rather than cautious phrasing. An answer can arrive already
# incomplete: when a #1094 retry still comes back ``finish_reason=length``,
# ``truncation.TRUNCATION_NOTICE`` is prepended saying the text "stops
# mid-answer". A marker asserting the tail below IS the end would then
# contradict it, in the same string, with nothing to tell the model which to
# believe. Both are now true at once -- the notice says the answer was cut
# short, this says what follows is the end of what arrived.


def _elide_answer_middle(content: str, budget: int) -> tuple[str, int]:
    """Fit *content* into *budget* characters by removing its MIDDLE.

    Returns the fitted text and the number of *content* characters destroyed.
    That count is returned rather than derived by the caller because the two
    are not the same number: the result carries inserted markers, so a
    before/after length difference nets those off and under-reports what was
    actually lost by roughly their combined length. ``dropped_chars`` is the
    field the ceiling gets sized from (#1090), so it has to mean one thing.

    Keeps the opening (framing and the first diagnostic steps) and the closing
    (remediation and the ``Sources:`` line), which is the opposite of the
    head-first cut every other tool result gets -- see
    ``KB_QA_ANSWER_TAIL_SHARE`` for why kb_qa is the exception.

    Both markers are inside the returned budget. On every path reachable from
    the two production callers the result ENDS on
    ``KB_QA_ANSWER_TRUNCATED_MARKER``, which is load-bearing rather than
    decorative: the tool loop reads that anchor back to know this cut already
    fed the truncation metrics, so one relayed result yields exactly one
    observation (#1090). It reads as an overall "this answer was trimmed" flag;
    the inline marker says where. Content arriving already marked keeps the
    marker it has rather than gaining a second one -- the anchor holds either
    way, since slicing the tail carries the existing marker along with it.

    "Reachable" is the honest qualifier, not a hedge. The degenerate-budget
    branch below slices to ``budget - len(end_marker)``, and on
    already-marked content that slice can land inside the marker it was meant
    to preserve. Both callers pass a budget three orders of magnitude larger,
    so the corner is unreachable today; it is called out rather than asserted
    away because a wrapper edit is exactly what would open it.
    """
    # Nothing to do. Both production callers already gate on the overflow, so
    # this is defensive rather than load-bearing -- but without it the head and
    # tail slices OVERLAP when the budget exceeds the content, duplicating text
    # into the result and returning a negative dropped count. A helper that
    # reports a nonsense number on an easy input is a trap for the next caller.
    if len(content) <= budget:
        return content, 0

    # Already marked means this is the SECOND cut on one answer: the formatter
    # trimmed it, then redaction expanded it back past the cap. One marker
    # still says the true thing; two in a row just read as noise to the model
    # that has to relay this.
    already_marked = content.endswith(KB_QA_ANSWER_TRUNCATED_MARKER)
    end_marker = "" if already_marked else KB_QA_ANSWER_TRUNCATED_MARKER

    # Sized on a worst-case count so the marker cannot itself push the result
    # past the budget once the real number is substituted in.
    elided_len = len(KB_QA_ANSWER_ELIDED_TEMPLATE.format(dropped=len(content)))
    # Reserved only when repair could actually fire. An answer with no fence in
    # it cannot come back with an odd fence count, so holding the room back
    # unconditionally spent up to 8 characters of answer on a repair that was
    # never possible -- on the majority of KB answers, which carry no fenced
    # block at all. The test is exact rather than heuristic: no ``` in, no ```
    # out, because both slices are substrings of the content.
    fence_reserve = FENCE_REPAIR_RESERVE if "```" in content else 0
    available = budget - len(end_marker) - elided_len - fence_reserve

    # Degenerate budget (a wrapper edit that leaves almost no room): fall back
    # to the plain head-first cut rather than emit markers with no answer
    # between them.
    if available < 2:
        kept = max(0, budget - len(end_marker))
        return content[:kept] + end_marker, len(content) - min(kept, len(content))

    tail_chars = int(available * KB_QA_ANSWER_TAIL_SHARE)
    head_chars = available - tail_chars

    head = _trim_head_to_paragraph(content[:head_chars])
    # Sliced from the end, so an existing marker rides along on the tail and
    # the result still ends on the anchor the tool loop looks for.
    tail = _trim_tail_to_paragraph(content[len(content) - tail_chars :])

    # Counted BEFORE fence repair. Repair inserts characters that were never in
    # the answer, so measuring the kept slices afterwards would credit them as
    # retained content and under-report the loss -- the same netting error the
    # returned count exists to avoid.
    dropped = len(content) - len(head) - len(tail)

    # Balanced independently, and only after the budget has been reserved for
    # it (FENCE_REPAIR_RESERVE): an unbalanced fence in either piece makes
    # everything after it render, and read, as code.
    head = _balance_code_fences(head)
    if tail.count("```") % 2:
        tail = "```\n" + tail

    elided = KB_QA_ANSWER_ELIDED_TEMPLATE.format(dropped=dropped)
    return head + elided + tail + end_marker, dropped


# Most a paragraph realignment may spend to land on a clean boundary.
#
# Bounded in absolute characters rather than as a share of the slice, because
# the cost being traded is answer text and the benefit is cosmetic. A share --
# "up to a third" -- scales the cosmetic allowance with the budget, so on the
# standard 7,410 it could discard ~2,400 characters to tidy two seams, on
# answers whose measured overflow was 540-1,249. A paragraph that does not
# begin within this many characters is left cut mid-sentence, which the
# markers on either side already explain.
PARAGRAPH_REALIGN_MAX_CHARS = 400


def _trim_head_to_paragraph(head: str) -> str:
    """Back the head up to a line boundary, if one is cheaply reachable.

    Paragraph first, then any line break. A runbook answer's most valuable
    region is a fenced block or a numbered command list, and neither contains a
    blank line -- so a paragraph-only search walks straight past the whole
    block and leaves the seam mid-command (``kubectl get pod pod-01``, verb
    intact, target truncated). A single newline is a real boundary there.
    """
    return _rewind_to_boundary(head, ("\n\n", "\n")).rstrip()


def _trim_tail_to_paragraph(tail: str) -> str:
    """Advance the tail to a line boundary, if one is cheaply reachable."""
    for sep in ("\n\n", "\n"):
        cut = tail.find(sep)
        if 0 <= cut <= PARAGRAPH_REALIGN_MAX_CHARS:
            return tail[cut:].lstrip()
    return tail.lstrip()


def _rewind_to_boundary(text: str, separators: tuple) -> str:
    """Back *text* up to the nearest of *separators* within the realign bound."""
    for sep in separators:
        cut = text.rfind(sep)
        if cut >= 0 and len(text) - cut <= PARAGRAPH_REALIGN_MAX_CHARS:
            return text[:cut]
    return text


# Room held back so fence repair cannot push the result past the budget.
# One opening fence for the tail and one closing fence for the head, each with
# its newline -- repair adds at most one of each.
#
# Applied only when the content actually contains a fence (see the call site).
# The reservation is otherwise pure loss: it is subtracted from the answer's
# room whether or not repair fires, and for a KB answer with no fenced block it
# never can.
FENCE_REPAIR_RESERVE = len("\n```") + len("```\n")


def _balance_code_fences(text: str) -> str:
    """Close a fenced block the elide cut open.

    The drop zone can contain the closing ``\u0060\u0060\u0060`` of a block whose opening
    survived in the head, or the opening of one whose close survived in the
    tail. Either way the relayed answer carries an unbalanced fence, and every
    downstream reader -- the model asked to relay it, and the Dashboard
    rendering the transcript as markdown -- then treats the rest of the answer
    as code. Cheaper to close it than to reason about which side is short.
    """
    if text.count("```") % 2 == 0:
        return text
    return text + "\n```"


def check_if_progress_made(metadata: dict[str, Any]) -> bool:
    """Whether the investigation ADVANCED this turn — the sole writer of
    ``turns_without_progress``, and therefore of every stall net downstream.

    The distinction this draws is *advancement*, not *activity* (#1136). The
    predicate used to accept any touched artifact, on the reasoning that "a
    skilled troubleshooter gathering information IS making progress". That is
    true of gathering something NEW and false of restating what the case
    already holds — and because the LLM restates constantly while it waits for
    the user (re-proposing the standing fix, re-quoting the same log lines),
    the counter reset almost every turn. It reached the ``EXHAUSTION_*``
    thresholds on 8 of 103 real cases past the turn floor, so ``is_stalled``,
    ``is_progress_stalled``, ``INSUFFICIENT_EVIDENCE``, ``TREATMENT_BLOCKED``,
    the exhaustion detector and the LOW/BLOCKED momentum bands were all
    effectively unreachable together.

    Each arm is therefore keyed to something the case did not already have:

    - ``novel_*`` rather than the raw ``evidence_added`` / ``solutions_proposed``
      / ``files_uploaded`` lists. Those keep every minted id — positional
      ``new_index_N`` resolution, milestone attribution and the turn record all
      depend on them — so the narrowing lives here, in the progress reading,
      not in what gets written. See ``_restates_standing_solution`` /
      ``_restates_standing_evidence`` for the per-arm bars.
    - ``DATA_PROVIDED`` is **dropped** as a separate arm. It is set from
      ``evidence_added`` (``turn_outcome.determine_turn_outcome``), so keeping
      it would readmit through the outcome label exactly the duplicate rows the
      ``novel_evidence_added`` key exists to exclude. Genuinely new evidence
      still lands via that key; nothing else was ever reaching this arm.
    - ``DATA_REQUESTED`` stays, and is now structural — a NEW outstanding
      ``EvidenceNeed`` raised this turn, not a keyword scan of the previous
      turn's prose (``turn_outcome._new_data_request_raised``). Re-asking for
      data the case is already waiting on no longer counts, which is the
      behaviour a parked investigation actually exhibits.
    - ``HYPOTHESIS_TESTED`` stays as-is: it reads ``tested_at ==
      current_turn``, already state-backed and already per-turn.

    Note this makes the counter honest for its OTHER readers too, all of which
    were reading the same inflated signal: ``progress_monitor``'s exhaustion
    detector, the LOW/BLOCKED momentum bands in
    ``working_conclusion_generator``, the "M turns since last progress" line in
    ``prompts/context_builder``, and the ``evidence_need_surfacing`` page
    cursor (which now rotates on genuinely barren turns, as it was meant to).
    """
    # Structural progress: an artifact the case did not already hold.
    structural_keys = [
        "milestones_completed",
        "novel_evidence_added",
        "hypotheses_generated",
        "hypotheses_validated",
        "novel_solutions_proposed",
        "novel_files_uploaded",
    ]
    for key in structural_keys:
        if metadata.get(key):
            return True

    if metadata.get("status_transitioned"):
        return True

    # Investigative progress: active diagnostic behaviors
    outcome = metadata.get("outcome")
    if outcome in (
        TurnOutcome.DATA_REQUESTED,
        TurnOutcome.HYPOTHESIS_TESTED,
    ):
        return True

    # A NEW or materially revised evidence link counts as progress. The
    # caller gates this counter on what ``link_evidence`` reports, so a
    # re-emitted standing link never reaches here (#1136) — linking storage
    # is an upsert, so counting per call was the same restatement leak the
    # ``novel_*`` keys close on the other arms.
    if metadata.get("hypothesis_evidence_links_applied"):
        return True

    return False


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
                seeder pre-fetch to widen the case OWNER's KB read scope with
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
        from faultmaven.core.investigation.kb_cause_seeder import (
            confirmed_root_seed_origin,
        )
        from faultmaven.core.investigation.terminal_transitions import (
            RunbookSuggestion,
            evaluate_runbook_suggestion,
        )

        # Step 0: Provenance-based uniqueness (Phase 5.2b). A case resolved by
        # validating a cause the seeder planted from an existing runbook needs no
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
                    case.organization_id,
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
        to the owner's teams, the same allowlist shape as the KB seeder
        pre-fetch (``_search_kb_for_runbooks``).

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
                    getattr(case, "organization_id", None),
                )
            return build_kb_scope_filter(owner_id, shared_kb_ids)

        return _resolve

    async def _run_runbook_conversion(
        self,
        conversion_service,
        request,
        user_id: str,
        organization_id: str,
    ) -> None:
        """Background task for runbook conversion.

        ``organization_id`` is the SOURCE CASE's org, and it is required, not
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
                organization_id=organization_id,
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
                case.messages.append(
                    {
                        "message_id": f"msg_{uuid4().hex[:12]}",
                        "case_id": case.case_id,
                        # No human wrote this; the role already says "system".
                        # A sentinel string here would reach clients as a
                        # non-resolvable principal id now that author_id
                        # persists (ADR-013 D4: system turns have no author).
                        "author_id": None,
                        "role": "system",
                        "content": notification_content,
                        "created_at": datetime.now(UTC).isoformat(),
                        "turn_number": case.current_turn,
                        "metadata": {"source": "runbook_conversion_complete"},
                    }
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

        # Pass tools with auto tool_choice — LLM decides whether to invoke
        # kb_qa, web_search, etc. based on the user's question.
        tools_kwargs: dict[str, Any] = {}
        if self.investigation_tools:
            tools_kwargs["investigation_tools"] = self._build_da_tool_schemas()
            tools_kwargs["tool_context"] = await self._build_tool_context(
                case, user_id=user_id
            )
            tools_kwargs["force_tool_use"] = False

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
                f"confirmed={case.inquiry.problem_statement_confirmed}, "
                f"decided_to_investigate={case.inquiry.decided_to_investigate}"
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
                    _record_deferred_disposition_decline(case)
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
                            status_transitioned=True,
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
            # TWO COMPLEMENTARY PATHS (Intent-Based Routing Design):
            # 1. EXPLICIT INTENT (frontend buttons/actions) → Skip pattern matching, use intent_data
            # 2. NATURAL LANGUAGE (user types in chat) → Pattern matching fallback (below)
            #
            # Pattern matching order matters: Check abandonment FIRST, then resolution.
            # This prevents "close as unresolved" from matching resolution patterns.
            #
            # BUG FIX (2026-02-08): User said "Close this case as unresolved" but system went to RESOLVED
            # ROOT CAUSE: Patterns were too specific ("close as unresolved" exact match)
            # FIX: Use key phrases that work with variations:
            #   - "as unresolved" matches: "close as unresolved", "close this case as unresolved"
            #   - "without solution" matches: "close without solution", "close this without solution"
            # This handles natural language variations while maintaining correct intent detection.
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

                elif to_status_str == "resolved":
                    if case.state != CaseState.INVESTIGATING:
                        raise ValueError(
                            f"Cannot transition to RESOLVED from {case.state.value}"
                        )

                    from faultmaven.modules.case.domain.services.case_action_manager import (
                        CaseActionManager,
                    )

                    if not user_message or not user_message.strip():
                        user_message = (
                            CaseActionManager.get_agent_message(
                                CaseState.INVESTIGATING, CaseState.RESOLVED
                            )
                            or "The issue is resolved."
                        )

                    # If a pending transition to resolved already exists, this
                    # dropdown click is a confirmation of the existing proposal.
                    # Execute the transition (User-Agent Handshake: confirm step).
                    if (
                        hasattr(case, "pending_transition")
                        and case.pending_transition
                        and case.pending_transition.get("to_state") == "resolved"
                    ):
                        from faultmaven.core.investigation.terminal_transitions import (
                            confirm_pending_transition,
                        )

                        confirm_pending_transition(case, case.user_id)
                        metadata["status_transitioned"] = True

                        logger.info(
                            f"INVESTIGATING->RESOLVED dropdown: confirmed existing pending "
                            f"transition for case {case.case_id}"
                        )

                        # Persist terminal state before synthesis (Report
                        # row FKs to case_id), then synthesize, then record
                        # the composed reply.
                        await self.repository.save(case)
                        (
                            summary_payload,
                            summary_failed,
                        ) = await self._auto_generate_report(case)
                        _resp = _compose_terminal_reply(case, summary_payload)
                        turn_metadata = self._finish_deterministic_turn(
                            case,
                            user_message or "",
                            _resp,
                            upload_report,
                            milestones_completed=["solution_verified"],
                            progress_made=True,
                        )
                        await self.repository.save(case)

                        remaining = await self._remaining_regens_for(case)
                        return {
                            "agent_response": _resp,
                            "suggested_follow_ups": _select_ack_follow_ups(
                                case, summary_failed, remaining
                            ),
                            "case_updated": case,
                            "metadata": turn_metadata,
                        }

                    # No pending transition — check resolution readiness before proposing.
                    from faultmaven.core.investigation.terminal_transitions import (
                        assess_resolution_readiness,
                        propose_transition,
                    )

                    readiness = assess_resolution_readiness(case)

                    if readiness.verdict == readiness.SUGGEST_CLOSE:
                        # Case lacks fundamentals — pivot to CLOSED. Propose the
                        # closed transition so the DECIDE pair the user
                        # sees matches what they will be confirming.
                        logger.info(
                            f"INVESTIGATING->RESOLVED dropdown: case {case.case_id} "
                            f"verdict=SUGGEST_CLOSE (missing: {readiness.missing}). "
                            f"Pivoting to CLOSED."
                        )
                        propose_transition(
                            case=case,
                            to_state="closed",
                            summary=readiness.message,
                        )
                        turn_metadata = self._finish_deterministic_turn(
                            case,
                            user_message or "",
                            readiness.message,
                            upload_report,
                            progress_made=False,
                        )
                        await self.repository.save(case)
                        return {
                            "agent_response": readiness.message,
                            "suggested_follow_ups": _close_confirmation_suggestions(),
                            "case_updated": case,
                            "metadata": turn_metadata,
                        }

                    if readiness.verdict == readiness.NEEDS_INFO:
                        # Partially ready — ask user for the missing pieces but
                        # remember their resolve intent so a follow-up turn
                        # with the missing detail can move forward.
                        logger.info(
                            f"INVESTIGATING->RESOLVED dropdown: case {case.case_id} "
                            f"verdict=NEEDS_INFO (missing: {readiness.missing}). "
                            f"Remembering resolve intent."
                        )
                        propose_transition(
                            case=case,
                            to_state="resolved",
                            summary=readiness.message,
                        )
                        case.pending_transition["needs_info"] = True
                        turn_metadata = self._finish_deterministic_turn(
                            case,
                            user_message or "",
                            readiness.message,
                            upload_report,
                            progress_made=False,
                        )
                        await self.repository.save(case)
                        return {
                            "agent_response": readiness.message,
                            "suggested_follow_ups": _resolution_confirmation_suggestions(),
                            "case_updated": case,
                            "metadata": turn_metadata,
                        }

                    # READY — propose transition via User-Agent Handshake
                    propose_transition(
                        case=case,
                        to_state="resolved",
                        summary="Case meets resolution criteria. Awaiting user confirmation.",
                    )
                    metadata["transition_proposed_this_turn"] = True

                    logger.info(
                        f"INVESTIGATING->RESOLVED dropdown: proposed transition for "
                        f"case {case.case_id} (pending user confirmation)"
                    )

                    # Return immediately with confirmation prompt + canonical
                    # confirm/decline pair (alignment with agent-initiated path).
                    _resp = (
                        "You've indicated this issue is resolved.\n\n"
                        + _build_resolution_confirmation(case)
                    )
                    turn_metadata = self._finish_deterministic_turn(
                        case,
                        user_message or "",
                        _resp,
                        upload_report,
                        progress_made=True,
                    )
                    await self.repository.save(case)
                    return {
                        "agent_response": _resp,
                        "suggested_follow_ups": _resolution_confirmation_suggestions(),
                        "case_updated": case,
                        "metadata": turn_metadata,
                    }

                elif to_status_str == "investigating":
                    if case.state != CaseState.INQUIRY:
                        raise ValueError(
                            f"Cannot transition to INVESTIGATING from {case.state.value}"
                        )

                    # Inject a pre-composed message and let the normal INQUIRY
                    # LLM flow handle the problem statement + transition.
                    # The frontend expects the case to transition in this turn,
                    # so we fall through to the LLM pipeline which can set
                    # user_confirmed_investigation=True and trigger the transition
                    # via _check_automatic_transitions.
                    from faultmaven.modules.case.domain.services.case_action_manager import (
                        CaseActionManager,
                    )

                    if not user_message or not user_message.strip():
                        user_message = (
                            CaseActionManager.get_agent_message(
                                CaseState.INQUIRY, CaseState.INVESTIGATING
                            )
                            or "I want to start a formal investigation to find the root cause."
                        )

                    logger.info(
                        f"INQUIRY->INVESTIGATING dropdown: routing through normal INQUIRY flow "
                        f"for case {case.case_id}"
                    )
                    # Fall through to normal LLM processing (no transition executed here)

                else:
                    raise ValueError(f"Unknown to_state: {to_status_str}")

            elif intent_type == "confirmation":
                logger.info(
                    f"Explicit confirmation intent for case {case.case_id} "
                    f"(has_pending_statement={bool(case.inquiry.proposed_problem_statement)})"
                )

                if case.state != CaseState.INQUIRY:
                    logger.warning(
                        f"Received confirmation intent for case {case.case_id} but status is {case.state.value}"
                    )
                elif not case.inquiry.proposed_problem_statement:
                    logger.warning(
                        f"Received confirmation intent for case {case.case_id} but no proposed problem statement exists"
                    )
                else:
                    # Gate 1 commit (problem-statement confirmation). There is
                    # no path fork (redesign R5) — the investigation proceeds
                    # opportunistically once INVESTIGATING begins.
                    case.inquiry.problem_statement_confirmed = True
                    case.inquiry.problem_statement_confirmed_at = datetime.now(UTC)
                    case.inquiry.decided_to_investigate = True
                    case.inquiry.decision_made_at = datetime.now(UTC)

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

            # NL transition detection happens upstream in
            # InvestigationService._detect_transition_intent — typed
            # transition requests reach this point as
            # intent_type == "status_transition" (handled above), never as
            # "conversation". Conversation that does not request a
            # transition flows directly into the LLM block below.

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
                from faultmaven.core.investigation.prompts.context_builder import (
                    fetch_entity_highlights,
                )

                if get_settings().preprocessing.entity_registry_enabled:
                    entity_highlight_groups = await fetch_entity_highlights(
                        self.repository, case.case_id
                    )
            except Exception as exc:
                logger.warning(
                    "Entity highlights prefetch failed for case %s " "(non-fatal): %s",
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

            def _build_prompt(tools_available: bool) -> str:
                return get_prompt_for_case(
                    case,
                    user_message,
                    kb_results=None,
                    provider_name=provider_name,
                    model_name=model_name,
                    processing_mode=processing_mode,
                    entity_highlight_groups=entity_highlight_groups,
                    tools_available=tools_available,
                )

            prompt = _build_prompt(_tools_avail)

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
            has_pending = (
                hasattr(case, "pending_transition") and case.pending_transition
            )
            if self.investigation_tools:
                force_tools = _should_force_tools(
                    processing_mode, case, bool(has_pending)
                )
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

            # Phase 1: No-Op Detection
            progress_made = self._check_if_progress_made(metadata)
            metadata["progress_made"] = progress_made
            # Outcome is already set by _process_response_structured (default) or applied updates (LLM choice)

            # 4. Check for automatic status transitions
            case_updated = await self._check_automatic_transitions(
                case_updated, metadata, user_message
            )

            # 5. Phase 4: Hypothesis Housekeeping (Decay & Anchoring)
            # This happens after transitions but before recording the turn
            self._perform_hypothesis_housekeeping(case_updated, metadata)

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
            if case_updated.state == CaseState.INVESTIGATING:
                working_conclusion = generate_working_conclusion(
                    case=case_updated, current_turn=case_updated.current_turn
                )
                case_updated.working_conclusion = working_conclusion
                logger.debug(
                    f"Working conclusion updated: likelihood={working_conclusion.likelihood:.2f}"
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

            # Step 5.9b: Wire reasoning_validation_errors into system_feedback.
            # This is the reasoning-first validator — a structural check that
            # required milestone justifications are present. Rule 2 (Evidence-
            # Grounded) compliance is enforced only at the prompt layer; there
            # is no post-generation diagnostic-reasoning validator.
            if metadata.get("reasoning_validation_errors"):
                errors = metadata["reasoning_validation_errors"]
                current_feedback = metadata.get("system_feedback", "") or ""
                metadata["system_feedback"] = (
                    f"{current_feedback}\n"
                    f"REASONING VALIDATION: {'; '.join(errors)}. "
                    "Provide internal_reasoning with milestone_justifications."
                ).strip()

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
                system_feedback=metadata.get("system_feedback"),
                momentum=progress_metrics.investigation_momentum,
                blocked_reasons=progress_metrics.blocked_reasons,
                next_steps=progress_metrics.next_steps,
                repair_pattern=stagnation_str,
                validation_repairs=validation_repairs,
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

            agent_response_text = response_obj.agent_response

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
            if metadata.get("resolution_ready_for_confirmation"):
                agent_response_text = _prose_with_gate_notice(
                    response_obj.agent_response,
                    "Thanks for the additional details.\n\n"
                    + _build_resolution_confirmation(case_updated),
                )
                follow_ups = _resolution_confirmation_suggestions()
                gate_prose_appended = True
            elif metadata.get("resolution_suggest_close"):
                # User didn't provide required info — suggest Close instead.
                agent_response_text = _prose_with_gate_notice(
                    response_obj.agent_response,
                    metadata["resolution_readiness_message"],
                )
                follow_ups = _close_confirmation_suggestions()
                gate_prose_appended = True
            elif metadata.get("resolution_needs_info_first_pass"):
                # LLM proposed RESOLVED but readiness check returned NEEDS_INFO.
                # Append the readiness ask below the LLM's agent_response so
                # the user sees both the turn's analysis and the same
                # missing-info ask the UI dropdown path produces.
                agent_response_text = _prose_with_gate_notice(
                    response_obj.agent_response,
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
                    response_obj.agent_response,
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
                    response_obj.agent_response,
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
                    response_obj.agent_response,
                    metadata["deferred_solution_gate_message"],
                )
                follow_ups = metadata["override_suggestions"]
                gate_prose_appended = True
            elif metadata.get("override_suggestions"):
                # ProposedTransition was emitted by the LLM this turn (either
                # detecting solution success or routing user-expressed
                # transition intent). Replace the LLM's follow-ups with the
                # canonical confirm/decline pair so all three trigger paths
                # (UI click, NL via this branch, agent-initiated) converge on
                # the same deterministic confirmation UX. NOTE: no prose is
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
            _overclaim_notice = _narration_overclaim_notice(
                case_updated,
                agent_response_text,
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
                        )
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
                    "agent_response_contains_completion_phrase": (
                        _narration_asserts_disposition(agent_response_text)
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
    MAX_TOOL_ITERATIONS = 4
    TOOL_RESULT_MAX_CHARS = 8000
    MAX_DEEP_ANALYSIS = 1

    def _resolve_tool_loop_budget(self, provider_name: str) -> int:
        """Per-call token budget for the tool loop: min(model hard ceiling,
        prompt_target + a bounded observation scratchpad). Best-known method for
        an agent working context — the base task fits the jar, the accumulated
        tool observations get a bounded allowance, and no call may exceed the
        model's hard limit."""
        from faultmaven.config.settings import get_settings
        from faultmaven.utils.model_context import resolve_model_budget

        try:
            obs = get_settings().prompt_budget.tool_observation_max_tokens
        except Exception:
            obs = 16_000
        try:
            pn = provider_name if isinstance(provider_name, str) else None
            resolved = resolve_model_budget(pn, self.da_model)
            budget = resolved.prompt_target + obs
            if resolved.prompt_budget is not None:  # known window → hard ceiling
                budget = min(budget, resolved.prompt_budget)
            return budget
        except Exception:
            # Best-effort — never let budget resolution break a turn.
            return 32_000 + obs

    def _bound_tool_loop_messages(
        self,
        messages: list[dict],
        budget_tokens: int,
        provider_name: str,
        token_cache: Optional[dict] = None,
    ) -> list[dict]:
        """Keep the tool-loop ``messages`` within ``budget_tokens`` by eliding the
        OLDEST tool-exchange groups (an assistant tool-call message plus its tool
        results), preserving the system + base task messages and the most-recent
        exchanges. This bounds the ACCUMULATED tool observations so the request
        cannot grow unbounded across iterations.

        Scope: the head (system + base task) is sized upstream by the assembling
        model and is never trimmed here, and the ``tools=`` schema payload is not
        counted — so on a dedicated DA provider whose window is smaller than the
        base's target, or with a very large tool schema, the sent request can
        still exceed the budget; the §7.1 runtime context-length recovery is the
        net for that (see #614).

        Invariants: the elided span is replaced by a single marker (INV-4 — never
        a silent drop; the agent can re-run a search), and whole assistant/tool
        groups are elided together so tool_call ↔ tool_result pairing stays valid
        (providers reject an orphan tool result).
        """
        from faultmaven.utils.token_estimation import estimate_tokens

        _prov = provider_name if isinstance(provider_name, str) else "local"
        _model = self.da_model if isinstance(self.da_model, str) else None

        def _tok(m: dict) -> int:
            # Memoize by object identity — `messages` is append-only within a
            # turn and every dict is held alive in it, so ids are stable and the
            # large, unchanging head is tokenized once, not once per iteration.
            key = id(m)
            if token_cache is not None and key in token_cache:
                return token_cache[key]
            parts = [str(m.get("content") or "")]
            if m.get("tool_calls"):
                parts.append(str(m.get("tool_calls")))
            # Reasoning artifacts are part of the WIRE payload and must be
            # counted, or this bound is not a bound. For a thinking-carrying
            # assistant turn the provider serializes
            # provider_metadata["assistant_content"] (Anthropic thinking /
            # redacted_thinking blocks) or ["assistant_parts"] (Gemini parts
            # with thoughtSignatures) INSTEAD OF `content` — reasoning text
            # that can run to thousands of tokens. Estimating from `content`
            # alone under-counts those turns by roughly the size of their
            # reasoning, so this function would report "under budget" while
            # the request it green-lights blows the provider's context limit
            # — precisely the failure it exists to prevent.
            if m.get("provider_metadata"):
                parts.append(str(m.get("provider_metadata")))
            val = estimate_tokens(" ".join(parts), provider=_prov, model=_model)
            if token_cache is not None:
                token_cache[key] = val
            return val

        if sum(_tok(m) for m in messages) <= budget_tokens:
            return messages

        head = messages[:2]  # system + base task (always kept)
        rest = messages[2:]
        groups: list[list[dict]] = []
        for m in rest:
            if m.get("role") == "assistant" or not groups:
                groups.append([m])
            else:
                groups[-1].append(m)

        marker = {
            "role": "user",
            "content": (
                "[Earlier tool calls and their results were elided to stay within "
                "the context budget. Re-run a search if you need those specifics.]"
            ),
        }
        avail = budget_tokens - sum(_tok(m) for m in head) - _tok(marker)
        kept: list[list[dict]] = []
        for g in reversed(groups):
            gt = sum(_tok(m) for m in g)
            if gt <= avail:
                kept.insert(0, g)
                avail -= gt
            else:
                break
        if len(kept) == len(groups):
            return messages  # nothing to elide (head already near budget)

        logger.warning(
            "tool_loop_context_bounded: elided %d of %d tool-exchange group(s) to "
            "fit the %d-token budget",
            len(groups) - len(kept),
            len(groups),
            budget_tokens,
        )
        out = list(head) + [marker]
        for g in kept:
            out.extend(g)
        return out

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
        # Per-call size budget: every tool-loop call is bounded to this so an
        # oversized prompt can never be sent (accumulated observations compact to
        # fit — see _bound_tool_loop_messages / _resolve_tool_loop_budget).
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
        messages = [
            {"role": "system", "content": da_system_instruction},
            {"role": "user", "content": prompt},
        ]
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
            # Hard-bound EVERY tool-loop call: the accumulated observations can
            # never grow the sent prompt past the budget. The full `messages`
            # history is kept for accumulation; only a bounded, most-recent view
            # is sent.
            bounded_messages = self._bound_tool_loop_messages(
                messages, tool_loop_budget, provider_name, token_cache=_msg_token_cache
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
                    return self._parse_schema_tool_call(tc, schema_model)

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
            "DEFAULT: When uncertain, treat it as Type A (case question) — "
            "evidence search is always safe. Only skip evidence search when "
            "the question clearly cannot be answered from log files, configs, "
            "or other submitted data.\n\n"
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

    async def _resolve_shared_kb_ids(self, user_id: str, organization_id: Any) -> list:
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
                share_repository, team_ids, organization_id
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
        organization_id = getattr(case, "organization_id", "")

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
            organization_id=organization_id,
            user_id=user_id,
            shared_kb_ids=await self._resolve_shared_kb_ids(user_id, organization_id),
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
        2. On failure, PRUNE the specific list entries the ValidationError points
           at (keyed off the error ``loc`` paths — general, not per-invariant)
           and re-validate. The bad sub-records are quarantined; everything else
           on the turn survives.
        3. If it still fails (a top-level / non-list error), drop ``state_updates``
           entirely and keep the conversational ``agent_response`` — the turn
           survives as a conversational reply rather than a 500.
        4. If even that fails, re-raise the original error (truly unrecoverable).

        Upstream remains the real fix: provider-native constrained generation so
        the LLM cannot emit the invalid shape ([[project-llm-structured-output-strategy]]).
        This is the backstop, not a per-variant patch.
        """
        from pydantic import ValidationError

        def _record(outcome: str):
            self._record_schema_validation(schema_model, outcome)

        try:
            parsed = schema_model.model_validate_json(json.dumps(content_obj))
            _record("clean")
            return parsed
        except ValidationError as original_error:
            pruned, dropped = self._prune_invalid_list_entries(
                content_obj, original_error
            )
            if dropped:
                try:
                    parsed = schema_model.model_validate_json(json.dumps(pruned))
                    logger.warning(
                        "structured_output_degraded: pruned invalid sub-record(s) "
                        f"{dropped} from {schema_model.__name__} and continued "
                        "(parse-time validator). Turn preserved.",
                        extra={"schema": schema_model.__name__, "pruned": dropped},
                    )
                    _record("pruned")
                    return parsed
                except ValidationError:
                    pass  # fall through to the conversational fallback

            # Last resort: keep the response text, drop all structured updates.
            if isinstance(content_obj, dict) and content_obj.get("state_updates"):
                fallback = {**content_obj, "state_updates": {}}
                try:
                    parsed = schema_model.model_validate_json(json.dumps(fallback))
                    # The prune path already logs its locs ("Turn preserved"); this
                    # branch is reached only when a NON-prunable (non-list-indexed)
                    # validator error remains — log exactly those so each fallback
                    # is self-diagnosing (was it correctly non-prunable, or a prune
                    # gap?). Reference: S4 backstop observability.
                    non_prunable = [
                        (list(e.get("loc", ())), e.get("msg", ""))
                        for e in original_error.errors()
                        if not any(isinstance(p, int) for p in e.get("loc", ()))
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
                    return parsed
                except ValidationError:
                    pass

            # Rung: the model omitted the required user-facing agent_response
            # ITSELF (observed on gemini-3.5-flash resolution turns) — the rungs
            # above preserve agent_response and so cannot help. Synthesize a
            # neutral placeholder so a turn whose state_updates are otherwise
            # valid survives instead of 500ing. The conclusion stays the model's
            # own (its state_updates), nothing is fabricated, and internal
            # reasoning is never surfaced; on resolution turns the closure
            # summary carries the substantive text.
            # Fire when agent_response is MISSING or non-string (None, or a
            # malformed 0/[]/false the schema rejects) — i.e. not a usable reply.
            # A model-provided string, including "", is a valid (if poor) value
            # the model chose, NOT the defect, so it is never overwritten.
            if isinstance(content_obj, dict) and not isinstance(
                content_obj.get("agent_response"), str
            ):
                placeholder = (
                    "I've updated the investigation based on the latest information."
                )
                base = pruned if dropped else content_obj
                # Prefer keeping the model's state_updates; only DROP them as a
                # last resort — and say so, so a state-update loss is never logged
                # as a mere field-fill.
                for state_dropped, candidate in (
                    (False, base),
                    (True, {**base, "state_updates": {}}),
                ):
                    try:
                        patched = {**candidate, "agent_response": placeholder}
                        parsed = schema_model.model_validate_json(json.dumps(patched))
                        logger.warning(
                            "structured_output_degraded: synthesized missing "
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
                        return parsed
                    except ValidationError:
                        continue

            _record("failed")
            raise original_error

    @staticmethod
    def _prune_invalid_list_entries(content_obj, error):
        """Remove the list entries a ValidationError flags. Returns (obj, [paths]).

        Each ValidationError ``loc`` for a list sub-record looks like
        ``('state_updates', 'evidence_to_add', 0, 'source_file_id')`` or
        ``(..., 0)``. We take the deepest int in the loc as the offending list
        index and drop that entry from the corresponding list. General across any
        list field (evidence_to_add, evidence_need_updates, hypotheses_to_add, …).
        """
        import copy

        obj = copy.deepcopy(content_obj)
        to_remove: dict[tuple, set] = {}
        for err in error.errors():
            loc = err.get("loc", ())
            int_positions = [i for i, part in enumerate(loc) if isinstance(part, int)]
            if not int_positions:
                continue  # top-level / non-list error — not prunable here
            last = int_positions[-1]
            list_path = loc[:last]
            to_remove.setdefault(list_path, set()).add(loc[last])

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
        return obj, dropped

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
        cleaned = text.strip()
        if "```" in cleaned:
            match = re.search(r"```(?:json|JSON)?\s*\n(.*?)\n```", cleaned, re.DOTALL)
            if match:
                cleaned = match.group(1).strip()
            elif cleaned.startswith("```"):
                lines = cleaned.split("\n")
                if lines and lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                cleaned = "\n".join(lines).strip()

        content_obj = json.loads(cleaned, strict=False)
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
            )
        except Exception as exc:
            if (
                user_message is not None
                and case is not None
                and _is_context_length_error(exc)
            ):
                from faultmaven.core.investigation.prompts.templates import (
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
                    f"Response truncated at the max_tokens ceiling "
                    f"({old_max}): {exc}",
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
            else:
                # For non-function-calling modes, strip markdown code blocks if present
                # Some LLMs return: ```json\n{...}\n``` instead of raw JSON
                # Or even worse: "Here's the response:\n```json\n{...}\n```"
                if isinstance(content, str):
                    content = content.strip()

                    # Check if content contains a markdown code block
                    if "```" in content:
                        # Extract JSON from markdown code block
                        # Handle both cases:
                        # 1. ```json\n{...}\n```
                        # 2. Some text\n```json\n{...}\n```\nMore text
                        # Match ```json (or ```JSON or just ```) followed by content until closing ```
                        pattern = r"```(?:json|JSON)?\s*\n(.*?)\n```"
                        match = re.search(pattern, content, re.DOTALL)
                        if match:
                            content = match.group(1).strip()
                        elif content.startswith("```"):
                            # Fallback to old logic if regex fails
                            lines = content.split("\n")
                            if lines and lines[0].startswith("```"):
                                lines = lines[1:]
                            if lines and lines[-1].strip() == "```":
                                lines = lines[:-1]
                            content = "\n".join(lines).strip()

            try:
                # First, try to load content as JSON if it's a string
                if isinstance(content, str):
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
                from pydantic import ValidationError as _ValidationError

                try:
                    parsed = schema_model.model_validate_json(content)
                except _ValidationError:
                    self._record_schema_validation(schema_model, "failed")
                    raise
                self._record_schema_validation(schema_model, "clean")
                return parsed
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
        _statement_existed_before_turn = bool(
            case.inquiry.proposed_problem_statement
            and case.inquiry.proposed_problem_statement.strip()
        )

        if updates.proposed_problem_statement:
            case.inquiry.proposed_problem_statement = updates.proposed_problem_statement

        # Convert and store problem_confirmation from LLM schema to domain model
        if updates.problem_confirmation:
            from faultmaven.modules.case.domain.models import (
                ProblemConfirmation as DomainProblemConfirmation,
            )

            case.inquiry.problem_confirmation = DomainProblemConfirmation(
                problem_type=updates.problem_confirmation.problem_type,
                severity_guess=updates.problem_confirmation.severity_guess,
                preliminary_guidance=updates.problem_confirmation.preliminary_guidance
                or "",  # Convert None to empty string
            )

        # Convert and store preliminary_urgency from LLM schema to domain model
        if updates.preliminary_urgency:
            from faultmaven.modules.case.domain.models import (
                PreliminaryUrgency as DomainPreliminaryUrgency,
            )
            from faultmaven.modules.case.domain.models import UrgencyLevel

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

        # STAGE 1: Extract problem statement from LLM (first turn only)
        # Extract problem statement but DON'T auto-confirm yet
        if updates.problem_confirmation and not case.inquiry.proposed_problem_statement:
            if updates.problem_confirmation.preliminary_guidance:
                case.inquiry.proposed_problem_statement = (
                    updates.problem_confirmation.preliminary_guidance
                )
                logger.info(
                    f"Problem statement extracted from preliminary_guidance: {updates.problem_confirmation.problem_type}"
                )
            # If no preliminary_guidance but proposed_problem_statement exists in updates,
            # it was already set above at line 685-686

        # STAGE 2: Two-Step Confirmation (Design Doc Section 1.2)
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
        # Same-turn-confirmation guard: the proposed_problem_statement must
        # have existed BEFORE this turn — otherwise the LLM is trying to
        # write the statement and confirm it in one shot, which collapses
        # the User-Agent Handshake. See the captured
        # _statement_existed_before_turn at the top of this method.
        if (
            getattr(updates, "user_confirmed_investigation", False)
            and case.inquiry.proposed_problem_statement
            and case.inquiry.proposed_problem_statement.strip()
            and not case.inquiry.problem_statement_confirmed
            and _statement_existed_before_turn
        ):
            case.inquiry.problem_statement_confirmed = True
            case.inquiry.problem_statement_confirmed_at = datetime.now(UTC)
            case.inquiry.decided_to_investigate = True
            case.inquiry.decision_made_at = datetime.now(UTC)
            logger.info(
                f"User confirmed problem statement — transitioning to INVESTIGATING. "
                f"statement='{case.inquiry.proposed_problem_statement[:80]}...'"
            )
        elif (
            getattr(updates, "user_confirmed_investigation", False)
            and case.inquiry.proposed_problem_statement
            and case.inquiry.proposed_problem_statement.strip()
            and not case.inquiry.problem_statement_confirmed
            and not _statement_existed_before_turn
        ):
            # LLM tried to set the problem statement AND confirm investigation
            # in the same turn — design forbids this (the user must see the
            # statement first, then confirm on a subsequent turn). Refuse
            # the transition; the agent will re-present the statement on
            # the next turn. Logged so drift is observable in telemetry.
            #
            # Set handshake_deferred_at_turn so the next turn's
            # context_builder switches from NOT_YET_CONFIRMED ("don't re-
            # propose") to HANDSHAKE_DEFERRED ("re-present and ask"), and
            # so process_turn deterministically emits confirmation
            # suggestions regardless of LLM compliance.
            case.inquiry.handshake_deferred_at_turn = case.current_turn
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
        if metadata["milestones_completed"]:
            from faultmaven.core.investigation.evidence_processor import (
                validate_milestone_claims,
            )

            reasoning = getattr(response_obj, "internal_reasoning", None)
            validation_results = validate_milestone_claims(
                case, metadata["milestones_completed"], reasoning
            )
            for result in validation_results:
                if not result.is_valid:
                    # Revert the milestone — evidence doesn't support the claim.
                    # cause_state is engine-derived (INV-35) and never appears in
                    # milestones_completed, so no cause-identification case is
                    # reverted here; the end-of-turn recompute owns cause_state.
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
                    and not _solution_cause_validated(case)
                ):
                    logger.warning(
                        f"Downgrading SOLUTION to DIAGNOSTIC for case {case.case_id}: "
                        f"cause_state={case.progress.cause_state.value}, "
                        f"rcc={'set' if case.root_cause_conclusion else 'none'} "
                        f"(M5 — a permanent fix requires an established root cause)"
                    )
                    action_type = InvestigationActionType.DIAGNOSTIC
                    downgrade_reason = (
                        "Your previous SOLUTION proposal was downgraded to "
                        "DIAGNOSTIC because the root cause is not yet established "
                        "— no validated chain root, no root-cause conclusion, and "
                        "no high-confidence working conclusion. A permanent fix "
                        "must target an established root cause — a diagnostic test "
                        "is not a solution (M5). State the root cause (a "
                        "root_cause_conclusion) backed by the evidence that "
                        "confirms it, then re-propose the fix; or, to intervene "
                        "now, propose a temporary mitigation (WORKAROUND) instead."
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
        prior_cause_state = case.progress.cause_state
        # Provider identity for the DF-6 provider-floor metric (INV-39), passed
        # explicitly (not smuggled through the shared metadata dict). Resolved via
        # the helper because self.llm_provider is the LLMRouter in the real
        # deployment (no provider_name) — the helper reads the configured chat
        # provider off it; a partially constructed engine (some fixtures omit
        # llm_provider) degrades to "unknown" rather than raising.
        _recompute_assessment_state(
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
        if metadata["milestones_completed"] and metadata["evidence_added"]:
            for ev_id in metadata["evidence_added"]:
                ev = next((e for e in case.evidence if e.evidence_id == ev_id), None)
                if ev:
                    ev.advances_milestones.extend(metadata["milestones_completed"])

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
        are logged and skipped. The emitted stance is carried through
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
                stance_confidence=link.stance_confidence,
            ):
                metadata["hypothesis_evidence_links_applied"] = (
                    metadata.get("hypothesis_evidence_links_applied", 0) + 1
                )

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
                    f"Ignored purpose-change attempt on " f"need {target.need_id}"
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
                    f"Ignored resurrection attempt on SUPERSEDED "
                    f"need {target.need_id}"
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

        # Outcome telemetry for INV-01: count cases that reached
        # INVESTIGATING after a prior same-turn-confirmation guard fire.
        # Divided by inquiry_handshake_deferred_total this gives the
        # recovery ratio — sustained ratio drops are the signal that
        # the deferral->recovery path has silently broken.
        if case.inquiry.handshake_deferred_at_turn is not None:
            inquiry_handshake_recovered_total.inc()

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
        kb_hits = await self._prefetch_kb_context(case, case.description, "symptom")

        # KB cause seeder (flag-gated): instantiate the matched runbooks'
        # metadata["causes"] chains as CANDIDATE graph nodes/hypotheses, so the
        # LLM validates/refutes structured priors instead of re-deriving one flat
        # hypothesis from prose. Prior, not gate — no evidentiary privilege.
        await self._seed_candidate_causes_from_kb(case, kb_hits)

    async def _prefetch_kb_context(
        self,
        case: "Case",
        query: str,
        trigger: str,
    ) -> list:
        """Search KB for runbooks matching the query, store on case.

        Args:
            case: Case to update
            query: Search query (problem statement or root cause)
            trigger: What triggered this search ("symptom" or "root_cause")

        Returns:
            The relevance-filtered ``SearchResult`` list (empty on miss/failure),
            so a caller can act on the matched runbooks — the KB cause seeder
            reads ``parent_document_id`` off these hits.
        """
        if not self.knowledge_service:
            return []

        try:
            # Owner-aware scope. The pre-fetch (and the seeder it feeds) may
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
                        getattr(case, "organization_id", None),
                    )
                except Exception:  # noqa: BLE001
                    # Graceful degradation — global ∪ owner-personal still seed.
                    shared_kb_ids = []
            scope_filter = build_kb_scope_filter(owner_id, shared_kb_ids)
            # Fetch deep (KB_PREFETCH_FETCH_LIMIT) so the seeder's parent-runbook
            # dedup sees more than one distinct runbook when a long runbook fills
            # the top chunk slots; render only the top KB_CONTEXT_MAX_ENTRIES into
            # the prompt. The returned `relevant` (full ranked list) is what the
            # seeder's parent-dedup consumes.
            results = await self.knowledge_service.search_knowledge(
                query=query, limit=KB_PREFETCH_FETCH_LIMIT, filters=scope_filter
            )
            relevant = [
                r for r in results or [] if r.score >= KB_PREFETCH_RELEVANCE_THRESHOLD
            ]
            if relevant:
                # Score-ranked, so this top slice is byte-identical to the old
                # limit-3 fetch — the rendered prompt surface is unchanged.
                case.kb_context = [
                    {
                        "title": r.title,
                        "summary": r.snippet,
                        "score": r.score,
                        "type": getattr(r, "document_type", "runbook"),
                        "parent_document_id": getattr(r, "parent_document_id", None),
                        "trigger": trigger,
                    }
                    for r in relevant[:KB_CONTEXT_MAX_ENTRIES]
                ]
                logger.info(
                    f"KB pre-fetch ({trigger}): {len(case.kb_context)} matches "
                    f"for case {case.case_id}"
                )
            elif results:
                # Got results but none cleared the relevance bar → clear stale
                # context. When the search returned nothing at all, leave any
                # existing kb_context untouched (a later trigger's empty search
                # must not wipe context an earlier trigger established).
                case.kb_context = None
            return relevant
        except Exception:
            logger.warning(
                f"KB pre-fetch ({trigger}) failed for case {case.case_id}",
                exc_info=True,
            )
            return []

    async def _seed_candidate_causes_from_kb(self, case: "Case", kb_hits: list) -> None:
        """Seed matched runbooks' cause chains as candidate graph nodes (flag-gated).

        Deterministic, engine-driven — no LLM call. Loads ``metadata["causes"]``
        for the top distinct runbooks the pre-fetch surfaced and instantiates
        their chains as CANDIDATE nodes/hypotheses (prior, not gate). Best-effort:
        a failure never breaks the transition.
        """
        from faultmaven.config.settings import get_settings

        if not get_settings().features.kb_cause_seeder_enabled:
            return
        if not self.knowledge_service or not kb_hits:
            return  # no retrieval this turn — a legitimate no-match, not a failure

        try:
            from faultmaven.core.investigation.kb_cause_seeder import (
                MAX_SEEDED_RUNBOOKS,
                SeededRunbook,
                seed_candidate_causes,
            )

            # Fold hits into {runbook: {cause_letter: best score}}. Retrieval is
            # CHUNK-level and a runbook's ``## Causes`` section chunks
            # one-Cause-per-chunk, so a hit names not just WHICH runbook matched
            # but WHICH OF ITS CAUSES did (`matched_cause_letters`, derived from
            # the chunk's own ``### Cause X:`` headings). Keeping that identity is
            # the fix for #1092: this used to collapse hits to
            # `parent_document_id`, discard which chunk matched, re-fetch the
            # runbook's FULL cause list and seed its first MAX_SEEDED_CAUSES in
            # AUTHOR order — so a k8s OOM/exit-137 case seeded the GKE runbook's
            # three *unschedulable* causes (A/B/C: capacity, machine type,
            # taints) while the OOMKilled cause that actually matched (D) sat one
            # slot past the cap, and a GitHub-Actions runbook that matched on
            # "exit code 137" seeded its causes A/B/C verbatim — runner RAM, disk
            # full, missing secret — into a Kubernetes investigation.
            #
            # A hit on a NON-cause chunk (Symptom Recognition, Diagnostic Steps,
            # Prevention) contributes no letter and therefore seeds nothing. That
            # is deliberate: such a hit is evidence the runbook is topically
            # relevant, never evidence that any particular cause of it applies —
            # and a seeded cause is asserted to the user as a candidate root, so
            # precision is worth more here than fan-out. Runbooks that seed
            # nothing are already a normal, served outcome (the flat-prose path).
            #
            # #1144 adds the CORROBORATION guard below. Knowing which cause a
            # chunk names fixed *which* of a matched runbook's causes seed; it
            # did not establish that the runbook belongs to this case at all.
            # That was left to rank alone ("retrieval has already done the
            # semantic case<->cause alignment"), and rank is a statement about
            # the other nine results, never about fit.
            # DISTINCT chunks, by chunk id — not a hit count. The two are the
            # same today (one vector search returns each chunk at most once), so
            # this changes nothing now; it is here because the day they diverge
            # is the day the guard fails OPEN. A hybrid/BM25 merge returning the
            # same chunk from both arms would let one chunk corroborate itself,
            # silently restoring the exact #1144 behaviour with the guard still
            # apparently in place. Cheaper to enforce than to detect.
            #
            # ``document_id`` is the CHUNK id here (``{parent}_chunk_{n}``):
            # search_knowledge falls through to the vector store's ``id`` because
            # the formatted hit carries no ``document_id`` key. A hit that
            # supplies no usable id still counts as its own chunk — a MISSING id
            # is not the failure mode being closed, a REPEATED one is, and
            # collapsing anonymous hits together would tighten the guard on a
            # source that never duplicated anything.
            chunk_ids_per_runbook: dict[str, set[str]] = {}
            length_of_runbook: dict[str, int] = {}
            for index, hit in enumerate(kb_hits):
                parent_id = getattr(hit, "parent_document_id", None)
                if not parent_id:
                    continue
                chunk_id = getattr(hit, "document_id", None) or ""
                if not chunk_id or chunk_id == "unknown":
                    chunk_id = f"__unidentified_{index}"
                chunk_ids_per_runbook.setdefault(parent_id, set()).add(chunk_id)
                total = getattr(hit, "total_chunks", None)
                if isinstance(total, int) and total > 0:
                    length_of_runbook[parent_id] = total

            def _chunks_required(parent_id: str) -> int:
                """How many chunks THIS runbook must surface to corroborate.

                Corroboration asks whether a runbook matched BROADLY, and breadth
                is only meaningful against the document's own length. A runbook
                cannot corroborate itself beyond how many chunks it has: a
                document that IS one chunk matches completely when that chunk
                matches, which is the strongest evidence available for it, not
                the weakest. A flat threshold read that as marginal and made such
                a document permanently unseedable — and compact documents are
                exactly the flywheel's own output (a runbook authored through
                ``POST /knowledge/runbooks/create``, or converted from a resolved
                case, chunks whole well under the chunker's 3000-char section
                budget), so the flat form silently excluded the personal runbooks
                the owner-aware prefetch scope exists to serve.

                An ABSENT stamp is "unknown", never "small": the full threshold
                applies, so pre-stamp content is treated exactly as it was before
                and no missing metadata can wave a runbook through.
                """
                total = length_of_runbook.get(parent_id)
                if total is None:
                    return KB_SEED_MIN_CORROBORATING_CHUNKS
                return min(KB_SEED_MIN_CORROBORATING_CHUNKS, total)

            # Fold ALL cause-naming runbooks first, guarded or not, then split.
            # The guard's COST cannot be measured from the survivors alone.
            cause_naming: dict[str, dict[str, float]] = {}
            for hit in kb_hits:
                parent_id = getattr(hit, "parent_document_id", None)
                if not parent_id:
                    continue
                for letter in getattr(hit, "matched_cause_letters", None) or []:
                    per_cause = cause_naming.setdefault(parent_id, {})
                    if hit.score > per_cause.get(letter, -1.0):
                        per_cause[letter] = hit.score

            best_score_by_cause: dict[str, dict[str, float]] = {}
            uncorroborated: set[str] = set()
            for parent_id, per_cause in cause_naming.items():
                surfaced = len(chunk_ids_per_runbook[parent_id])
                if surfaced >= _chunks_required(parent_id):
                    best_score_by_cause[parent_id] = per_cause
                else:
                    uncorroborated.add(parent_id)

            if uncorroborated:
                # Count only the declines that COST something: a runbook ranked
                # below MAX_SEEDED_RUNBOOKS would never have been consulted even
                # with the guard off, so counting it would inflate the guard's
                # price with runbooks it never actually turned away. Same
                # reasoning the ``no_seedable_cause`` branch below already
                # applies to ``top`` rather than ``ranked``. One increment per
                # declined runbook, never per chunk or per cause.
                would_consult = [
                    parent_id
                    for parent_id, _ in sorted(
                        cause_naming.items(),
                        key=lambda kv: max(kv[1].values()),
                        reverse=True,
                    )[:MAX_SEEDED_RUNBOOKS]
                ]
                cost = [p for p in would_consult if p in uncorroborated]
                if cost:
                    kb_cause_seed_uncorroborated_total.inc(len(cost))
                    logger.info(
                        "KB cause seeder: %d runbook(s) named a cause on too few "
                        "retrieved chunks (needed %d) and were not seeded for "
                        "case %s (%s) — their prose still reaches the LLM via "
                        "kb_context",
                        len(cost),
                        KB_SEED_MIN_CORROBORATING_CHUNKS,
                        case.case_id,
                        sorted(cost),
                    )

            if not best_score_by_cause:
                # Two different zero-seeds, kept apart: nothing named a cause at
                # all, versus something did and the corroboration guard declined
                # it. Collapsing them would hide the guard's whole cost inside a
                # counter that already means something else.
                outcome = (
                    "no_corroborated_runbook"
                    if uncorroborated
                    else "no_cause_chunk_matched"
                )
                kb_cause_seed_attempt_total.labels(outcome=outcome).inc()
                logger.debug(
                    "KB cause seeder: no seedable runbook cause for case %s "
                    "(%s) — nothing to seed (the runbook prose still reaches "
                    "the LLM via kb_context)",
                    case.case_id,
                    outcome,
                )
                return

            # Rank runbooks by their best MATCHED cause — a runbook is worth
            # entering only for the causes retrieval surfaced in it, so that is
            # the score that should order them.
            ranked = sorted(
                best_score_by_cause.items(),
                key=lambda kv: max(kv[1].values()),
                reverse=True,
            )

            # The per-runbook causes lookups are independent — issue them
            # concurrently (get_runbook_causes catches its own errors → None, so
            # gather never raises).
            top = ranked[:MAX_SEEDED_RUNBOOKS]
            causes_per_runbook = await asyncio.gather(
                *(
                    self.knowledge_service.get_runbook_causes(parent_id)
                    for parent_id, _ in top
                )
            )
            runbooks: list = []
            for (parent_id, scores_by_letter), causes in zip(top, causes_per_runbook):
                if not causes:
                    continue
                # Keep only the causes retrieval matched, best-scoring first.
                # sorted() is stable, so causes tied on score (the common case —
                # one chunk carrying two headings) keep the author's own
                # most-likely-first order.
                matched = sorted(
                    (
                        c
                        for c in causes
                        if str(c.get("cause_letter", "")) in scores_by_letter
                    ),
                    key=lambda c: scores_by_letter[str(c.get("cause_letter", ""))],
                    reverse=True,
                )
                if not matched:
                    # The chunk's heading letter names no cause in the record —
                    # a produce-side inconsistency (heading vs extracted causes),
                    # not a normal outcome. Visible, never silent. Counted as
                    # well as logged: the shipped pack is pinned against this by
                    # a corpus test, but generated/uploaded runbooks are not, so
                    # in production this counter is the only sighting of the
                    # drift. This runbook keeps the MAX_SEEDED_RUNBOOKS slot it
                    # occupied rather than yielding it to the next ranked one —
                    # a deliberate simplicity call: promoting a runbook on the
                    # strength of a DATA BUG in a higher-ranked one would make
                    # the seeded set depend on corruption, and the counter says
                    # how often the micro recall loss is even in play.
                    kb_cause_seed_letter_mismatch_total.inc()
                    logger.warning(
                        "KB cause seeder: runbook %s matched cause letter(s) %s "
                        "but its causes record holds none of them (case %s)",
                        parent_id,
                        sorted(scores_by_letter),
                        case.case_id,
                    )
                    continue
                runbooks.append(
                    SeededRunbook(
                        item_id=parent_id,
                        score=max(scores_by_letter.values()),
                        causes=matched,
                    )
                )
            if not runbooks:
                kb_cause_seed_attempt_total.labels(outcome="no_seedable_cause").inc()
                # None of the runbooks LOOKED UP yielded a seedable cause — either
                # it carried no causes record (flat prose) or its record held none
                # of the matched letters (warned individually just above). Counts
                # `top`, the slice actually fetched, not `ranked`: reporting every
                # ranked runbook here would claim nine runbooks contributed
                # nothing when only MAX_SEEDED_RUNBOOKS were ever consulted.
                # Logged (not silent) so a legitimate zero-seed is traceable and
                # cannot be confused with the seeder crashing below.
                logger.debug(
                    "KB cause seeder: none of the %d consulted runbook(s) yielded "
                    "a seedable cause for case %s — flat-prose path serves them",
                    len(top),
                    case.case_id,
                )
                return

            report = seed_candidate_causes(
                case,
                runbooks,
                case.current_turn,
                hypothesis_manager=self.hypothesis_manager,
            )
            # Counted AFTER the call, so a crash inside the seeder lands on the
            # ``crashed`` label alone — the outcome labels stay exclusive and sum
            # to attempts, which is what makes ``seeded``/total a yield.
            kb_cause_seed_attempt_total.labels(
                outcome=("seeded" if report.seeded_anything else "all_causes_skipped")
            ).inc()
        except Exception:
            kb_cause_seed_attempt_total.labels(outcome="crashed").inc()
            # A crash here is a SEEDER BUG, not a legitimate no-match. Log at ERROR
            # with an explicit marker so that, once the flag is on, "investigation
            # proceeded with zero seeds" from a broken seeder is distinguishable
            # from the normal no-match path (which returns quietly above) — the
            # same no-silent-failure goal the skip taxonomy serves.
            logger.error(
                "KB cause seeder CRASHED for case %s — investigation proceeds with "
                "NO structural seeds (this is a seeder bug, not a no-match)",
                case.case_id,
                exc_info=True,
            )

    async def _check_automatic_transitions(
        self, case: Case, metadata: dict[str, Any], user_message: str = ""
    ) -> Case:
        """
        Check if case should automatically transition status.

        Automatic Transitions (non-terminal):
        - INQUIRY -> INVESTIGATING when decided_to_investigate=True

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
            gate1_passed = case.inquiry.decided_to_investigate or (
                case.inquiry.problem_statement_confirmed
                and case.inquiry.problem_confirmation
            )
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
                from faultmaven.modules.case.domain.services.case_action_manager import (
                    ALLOWED_ACTIONS,
                )

                # Structural validation against the action graph: the LLM
                # cannot emit a ``proposed_transition`` whose ``to_state``
                # is not a valid edge from the current ``case.state`` per
                # ``ALLOWED_ACTIONS``. The prompt instructs the LLM on
                # which edges exist; this is the safety net for prompt
                # non-compliance (e.g., an LLM emitting ``to_state="resolved"``
                # from INQUIRY, which is not a valid edge — INQUIRY can
                # only transition to INVESTIGATING or CLOSED). Rejecting
                # here prevents downstream pivot logic from accepting an
                # invalid emission and quietly converting it into a
                # different transition the user never intended.
                valid_targets = {s.value for s in ALLOWED_ACTIONS.get(case.state, [])}
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
                # via the same helpers the UI dropdown path uses, so all
                # three trigger paths produce identical confirmation prompts.
                #
                # When the LLM proposes RESOLVED, run the same readiness
                # check the UI dropdown path uses so the user sees a
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
                    # UI dropdown path's first-pass behavior.
                    metadata["resolution_needs_info_first_pass"] = True
                    metadata["resolution_needs_info_message"] = needs_info_message
                metadata["transition_proposed_this_turn"] = True
                # Override LLM-emitted suggestions with the canonical
                # confirm/decline pair, so all three trigger paths
                # (UI click, NL via this branch, agent-initiated) produce
                # the same structured DECIDE confirmation UX. The
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

        The deterministic branches (pending resolve/close gates, the
        status-transition dropdown handlers) answer without an LLM call. They
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
        # Upload keys before the progress read: ``_check_if_progress_made``
        # scores ``novel_files_uploaded`` off this same dict.
        metadata.update(upload_report)
        metadata["progress_made"] = progress_made or self._check_if_progress_made(
            metadata
        )

        case.turn_history.append(
            TurnProgress(
                turn_number=case.current_turn,
                timestamp=datetime.now(UTC),
                milestones_completed=metadata["milestones_completed"],
                evidence_added=[],
                hypotheses_generated=[],
                hypotheses_validated=[],
                solutions_proposed=[],
                progress_made=metadata["progress_made"],
                outcome=TurnOutcome.CONVERSATION,
                user_message_summary=self._summarize_text(user_message, 200),
                agent_response_summary=self._summarize_text(agent_response, 500),
            )
        )
        if metadata["progress_made"]:
            case.turns_without_progress = 0
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

    def _check_if_progress_made(self, metadata: dict[str, Any]) -> bool:
        """Thin delegate to :func:`check_if_progress_made`.

        The reading moved to module scope so callers outside this class — the
        service's consumed-turn backstop (#1264) — can score a turn with the
        SAME predicate rather than reimplementing it or hardcoding a verdict.
        Kept as a method because every existing call site and test targets it.
        """
        return check_if_progress_made(metadata)

    def _summarize_text(self, text: str, max_length: int = 200) -> str:
        """Summarize long text for storage."""
        if len(text) <= max_length:
            return text
        return text[: max_length - 3] + "..."

    # =============================================================================
    # Phase 4 Housekeeping & Helpers
    # =============================================================================

    def _perform_hypothesis_housekeeping(
        self, case: Case, metadata: dict[str, Any]
    ) -> None:
        """Apply confidence decay and anchoring detection."""
        active_hypotheses = [
            h for h in case.hypotheses.values() if h.state == HypothesisState.ACTIVE
        ]

        if not active_hypotheses:
            return

        # 1. Apply confidence decay to stagnant hypotheses
        for h in active_hypotheses:
            # Age-based stagnation sweep (#713): a hypothesis no turn ever touches
            # keeps iterations_without_progress=0, so decay/anchoring would never
            # act on it. Advance the stagnation counter for one that has gone
            # stagnant-by-age (origin-blind) so an IGNORED hypothesis decays and
            # can trip anchoring the same as a repeatedly-tested one — never
            # validating or concluding, only lowering belief over time.
            self.hypothesis_manager.advance_stagnation_if_ignored(h, case.current_turn)
            # We decay if NO progress was made this turn for this specific hypothesis
            # (Note: link_evidence resets iterations_without_progress to 0)
            self.hypothesis_manager.apply_likelihood_decay(h, case.current_turn)

        # 2. Detect anchoring and add system feedback if necessary
        is_anchored, reason, hypothesis_ids = self.hypothesis_manager.detect_anchoring(
            active_hypotheses, case.current_turn
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
        """
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
    """

    def __init__(self, message: str, error_code: Optional[str] = None):
        super().__init__(message)
        self.error_code = error_code
