"""Prompt assembly: builds the final prompt string for a case's state and stage."""

import dataclasses
import logging
from typing import Any, Dict, List, Optional, Sequence

from faultmaven.core.investigation.prompts.context_builder.assembly import (
    build_investigation_context,
)
from faultmaven.core.investigation.prompts.context_builder.entity_highlights import (
    EntityHighlightGroup,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
    InvestigationProgress,
    InvestigationStage,
)
from faultmaven.modules.case.domain.models.progress import CauseState

from .blocks import (
    _DIAGNOSTIC_REASONING_BLOCK,
    _EVIDENCE_GROUNDING_BLOCK,
    _OBSERVATION_TIME_BLOCK,
    AGENT_META_INSTRUCTIONS,
    KNOWLEDGE_QUERY_INSTRUCTIONS,
)
from .diagnosis import _CHAIN_EMISSION_BLOCK, _RCA_DIAGNOSIS_BLOCK
from .fallback import get_fallback_prompt_for_case
from .inquiry import INQUIRY_TEMPLATE
from .investigation import INVESTIGATION_BASE
from .terminal import TERMINAL_TEMPLATE
from .treatment import MITIGATION_INSTRUCTIONS, TREATMENT_INSTRUCTIONS

logger = logging.getLogger(__name__)

# =============================================================================
# DEGRADED MODE INSTRUCTIONS
# =============================================================================


# =============================================================================
# BUILDER FUNCTIONS
# =============================================================================


def _symptom_verification_is_stale(case) -> bool:
    """Whether the symptom's observation window sits away from the present.

    Thin adapter over ``symptom_currency`` so the emphasis text has one reason
    to change. False when no case is supplied (the ``progress``-only callers),
    and false for UNDATED — an unknown observation time gives nothing to anchor
    to, so a window directive built on it would name no window while firing on
    the ordinary case of evidence with no timestamps to parse.
    """
    if case is None:
        return False
    from faultmaven.core.investigation.symptom_currency import (
        SymptomCurrency,
        assess_symptom_currency,
    )

    return assess_symptom_currency(case) == SymptomCurrency.STALE


def _get_diagnosis_focus_emphasis(progress: "InvestigationProgress", case=None) -> str:
    """Compute focus zone from progress milestones (Framework §8.5).

    Returns a contextual status signal injected at the top of DIAGNOSIS
    instructions. Informs the agent where the investigation stands and what
    would advance it, WITHOUT overriding the user's question.

    Four states based on progress milestone state:
    - Zone 1: symptom_verified=False — verify problem exists
    - Zone 2: symptom_verified=True, cause_state != IDENTIFIED — root cause analysis
    - Zone 3: cause_state == IDENTIFIED, solution_proposed=False — propose fix
    - Zone 3 pending: solution_proposed=True — awaiting execution, NON-suppressive
      hold that yields to root-cause analysis on new evidence/dispute (INV-33)

    ``case`` is optional so existing ``progress``-only callers keep working
    unchanged; when supplied, Zone 2 additionally names the symptom's
    observation window. Without it the zone says "Symptoms are confirmed" and
    nothing about WHERE to look, so evidence requests drift to the present —
    the failure mode in which an investigation queries the last 30 minutes for
    a symptom observed two hours earlier.
    """
    if not progress.symptom_verified:
        return """
**INVESTIGATION PROGRESS: Symptom verification pending**
No symptoms have been formally confirmed. When analyzing data, look for
evidence the problem exists — errors, anomalies, user impact — to advance
symptom_verified.

Data showing the problem at an earlier time DOES verify it — a problem is worth
investigating while it EXISTS (evidence still collectible, root cause
unidentified, solution unknown), whether or not it is firing right now. Do not
withhold symptom_verified because the evidence is not from this minute.

What the observation time governs is WHERE the investigation looks. State it
explicitly when you verify: it becomes the window every later evidence request
targets, and requests must name absolute timestamps rather than relative ones,
which silently drift to the present.
"""
    elif progress.symptom_verified and progress.cause_state != CauseState.IDENTIFIED:
        stale = _symptom_verification_is_stale(case)
        if stale:
            return """
**INVESTIGATION PROGRESS: Root cause analysis — anchor to the symptom's window**
The symptom was observed some time ago (see the observation time on
symptom_verified above). That period, not the present, is where this
investigation looks.

This does NOT mean the problem is stale or not worth pursuing — a problem is
worth investigating while it EXISTS: evidence still collectible, root cause
unidentified, solution unknown. Whether it happens to be firing right now
changes how you work it, not whether you do.

It does mean two things about evidence:
  1. Scope every request to the symptom's window using ABSOLUTE timestamps.
     Relative windows silently drift to the present — asking for `--since=30m`
     about a symptom seen two hours ago inspects a period the problem was never
     claimed to be in.
  2. A clean current-state reading is not counter-evidence. It looks at a
     different window and says nothing about what happened in the symptom's.
     Do not treat it as refuting the symptom, and do not retract on it.

Retract symptom_verified only if the symptom CLAIM itself turns out to be
wrong — misread data, the wrong system, an artefact — not because the problem
is not occurring at this moment.
"""
        return """
**INVESTIGATION PROGRESS: Root cause analysis**
Symptoms are confirmed. When evaluating evidence, focus on hypotheses
explaining the root cause. Two independent causal observations grounding a
hypothesis's chain root are what let the engine mark the cause identified.

If new data shows the symptom claim itself was wrong — misread data, the wrong
system, an artefact — do not absorb it as noise. Say so, record it, and set
symptom_verified=False with a justification. (The problem merely not occurring
right now is NOT that: an existing problem is investigable whether or not it is
currently firing.)
"""
    elif (
        progress.cause_state == CauseState.IDENTIFIED and not progress.solution_proposed
    ):
        return """
**INVESTIGATION PROGRESS: Solution needed**
Root cause is identified. A concrete, executable fix with specific commands
advances the investigation to Treatment.
"""
    else:
        return """
**INVESTIGATION PROGRESS: Solution proposal issued — awaiting execution**
A fix has been proposed and is awaiting execution. If the user reports executing
it, set solution_accepted=True and infer the transition to TREATMENT.
This hold is NOT a freeze. If the user's reply instead brings new evidence,
questions the fix, or points at a different cause, resume root-cause analysis on
that signal — investigate it rather than repeating the standby. A pending
proposal never forecloses a live diagnostic thread.
"""


# Seeded-candidate directive — LEGACY rows only (``seeded_provenance``): the
# KB cause seeder that planted these candidates was removed in fm#1295, so the
# swap below fires only for cases opened before 2026-09-02 that still carry
# seeds, and goes with the module's sunset. Rather than appending a
# contradicting override after the flat "matched Cause → create hypotheses_to_add"
# directive (two directives the model must arbitrate), we conditionally REPLACE
# that one directive when the seeder has already instantiated the matched runbook's
# Cause chains as CANDIDATE hypotheses — so a seeded turn reads ONE coherent
# instruction. The flat directive is sliced out of the assembled block by stable
# anchors (no hand-transcription: the source of truth stays the block itself), so
# the runtime replace is exact and guaranteed to hit once. A drift in the anchors
# raises loudly at import.
_MATCHED_CAUSE_ANCHOR_START = "  - **Exactly one Cause matches:**"
_MATCHED_CAUSE_ANCHOR_END = "  - **Two or more Causes"


def _slice_matched_cause_directive(block: str) -> str:
    """Extract the exact 'Exactly one Cause matches' bullet from the block."""
    start = block.index(_MATCHED_CAUSE_ANCHOR_START)
    end = block.index(_MATCHED_CAUSE_ANCHOR_END, start)
    return block[start:end]


# The flat directive verbatim (flag-off / prose-only sources). Sliced, not
# transcribed — identical bytes to what appears in _RCA_DIAGNOSIS_BLOCK.
_KB_MATCHED_CAUSE_FLAT = _slice_matched_cause_directive(_RCA_DIAGNOSIS_BLOCK)

# Its coherent replacement for a seeded turn: the matched Cause is ALREADY a
# CANDIDATE hypothesis in the graph, so validate/refute it against evidence
# rather than re-create it. Preserves the knowledge_match / SolutionToAdd
# TREATMENT handoff, adds priors-to-test framing (anti over-deference) and an
# explicit keep-your-own-hypotheses clause (anti crowd-out). Trailing blank line
# matches the sliced directive so the surrounding list spacing is preserved.
_KB_MATCHED_CAUSE_SEEDED = """  - **Exactly one Cause matches:** its chain is ALREADY in your `<causal_graph>`
    as a CANDIDATE hypothesis (rationale reads "Seeded from runbook …") — a prior
    to TEST, not an answer. Do NOT create a `hypotheses_to_add` record for it;
    that duplicates the cause. Instead link evidence to the existing candidate
    (`hypothesis_evidence_links` SUPPORTS/REFUTES; `causal_evidence` on its rungs).
    Never confirm it on partial or absent evidence — a runbook match is a lead,
    not proof; an unsupported seed decays on its own, so test it rather than
    defend it. When evidence genuinely supports it, emit `knowledge_match` in
    state_updates (match_type / match_likelihood / match_summary /
    suggested_solution) so the TREATMENT-stage KB-RESOLUTION VARIANT can
    direct-copy it, and propose its fix via a SolutionToAdd record. KEEP forming
    your own hypotheses for any cause the runbook did NOT cover — the seeds are a
    starting differential, not a ceiling.
"""


def _select_diagnosis_block(case: Case) -> str:
    """Focus emphasis + the RCA diagnosis block for a diagnosing turn.

    Diagnosis has no path fork (redesign R5): hypothesis formulation and
    evidence-needs run as a single opportunistic flow. Stage guidance is
    selected by the assessment variables (``symptom_verified`` /
    ``cause_state`` / ``solution_proposed``) via
    ``_get_diagnosis_focus_emphasis`` — not by a prospective path choice. The
    focus emphasis is prepended to ``_RCA_DIAGNOSIS_BLOCK``, which carries the
    hypothesis-evidence ordering mandate
    (``_HYPOTHESIS_EVIDENCE_ORDERING_BLOCK``), then the chain-emission block
    teaches lazy backward expansion (the engine ingests the emitted chain).

    Legacy seeded cases (``seeded_provenance``): when this case's graph holds
    candidates the removed KB cause seeder planted, the flat "matched Cause →
    create hypotheses_to_add" directive is REPLACED with the
    validate/refute-the-seeded-candidate one, so the model tests the
    candidate it already has instead of re-creating it beside the seed. Keyed
    on graph state, never on a flag: nothing writes seeds any more, so this
    branch is dead for every case opened after 2026-09-02 and goes with the
    module's sunset.
    """
    focus_emphasis = _get_diagnosis_focus_emphasis(case.progress, case)
    block = focus_emphasis + _RCA_DIAGNOSIS_BLOCK
    block += _CHAIN_EMISSION_BLOCK

    from faultmaven.core.investigation.seeded_provenance import (
        case_has_seeded_candidates,
    )

    if case_has_seeded_candidates(case):
        block = block.replace(_KB_MATCHED_CAUSE_FLAT, _KB_MATCHED_CAUSE_SEEDED, 1)
    return block


def _page_capture_hint(source: "str | None") -> str:
    """Resolve the {page_capture_hint} placeholder from the case's origin.

    The "Analyze current page" capture exists only in the browser extension
    (``source == "copilot"``); pointing a Slack/API-originated user at it
    directs them to a feature their client doesn't have. The prompt cannot
    decide this — no client marker reaches the LLM — so the engine resolves
    it here from the server-stamped ``Case.source``. Unknown/None fails safe
    to copy/paste: never direct a user at an affordance they may not have.

    ``source`` is stamped at case creation, so a copilot-created case later
    continued from another client still reads "copilot" — accepted: the
    pointer is a convenience and the copy/paste path is universal.
    """
    if source == "copilot":
        return (
            "When the data is visible on a page the user is viewing, you may "
            'instead point them to the "Analyze current page" capture, which '
            "submits the page's content for analysis."
        )
    return (
        "When the data is visible on a page the user is viewing, ask them to "
        "copy/paste the relevant page content into the chat."
    )


def get_prompt_for_case(
    case: Case,
    user_message: str,
    kb_results: Optional[List[Dict[str, Any]]] = None,
    provider_name: Optional[str] = None,
    model_name: Optional[str] = None,
    use_state_summary: Optional[bool] = None,
    processing_mode: Optional[str] = None,
    entity_highlight_groups: Optional[Sequence[EntityHighlightGroup]] = None,
    tools_available: bool = False,
    target_tokens: Optional[int] = None,
) -> str:
    """Build the final prompt based on case state and stage.

    Args:
        case: Current case
        user_message: User's message this turn
        kb_results: Optional knowledge base search results
        provider_name: LLM provider name for dynamic budget calculation (Gap #6)
        model_name: LLM model name for fine-grained budget calculation (Gap #6)
        use_state_summary: Optional flag to use compact state summary (Gap #8)
                          (auto-enabled for conversations >15 turns)
        processing_mode: Processing mode (triage/directed_analysis) for structural
                        index role tagging in evidence context
        entity_highlight_groups: Phase 4c registry highlight ROWS. Milestone
            engine fetches via ``fetch_entity_highlights`` when the feature
            flag is on; ``None`` / ``[]`` degrades to an empty section in the
            INVESTIGATING template. Rows, not a formatted block: the values
            come out of file content, so the block is fenced, and the fence
            must be able to re-render on a token collision — which it cannot
            do around an awaited query (#1228).
        tools_available: True when the investigation tools are registered AND the
            resolved model can do tool calling. Gates the directed-analysis
            evidence index+stub elision — the extract is only dropped (telling the
            agent to search_file) when search_file will actually run. Conservative
            default False: no elision unless the caller confirms tools work.
        target_tokens: Optional HARD cap on the assembled prompt, in the
            provider's estimated tokens. It lowers both the fill target and the
            overflow ceiling the backstop enforces, so the result fits it or is
            the minimal fallback prompt. The tool loop uses it to re-assemble a
            base that fits what is left of its per-call budget after the system
            instruction and the ``tools=`` payload (#614). ``None`` (every other
            caller) leaves the resolved budget unchanged.

    Returns:
        Formatted prompt for the LLM

    Whole-prompt token accounting (GAP-2 / GAP-3): this is the single place
    where the dynamic sections and the fixed template text combine into the
    final string, so it owns the whole-prompt budget. After assembling, it
    measures the real assembled token count (GAP-4) against the model's hard
    context ceiling (GAP-1 registry). On overflow it re-assembles once at a
    tighter section budget (so the fixed template overhead is finally
    accounted for); if it is *still* over, it falls back to the minimal safe
    prompt (``get_fallback_prompt_for_case``). Every overflow event is logged.
    """

    def _build_ctx(section_budget: Optional[int]) -> dict:
        """Build the section ctx dict sized to *section_budget*."""
        return build_investigation_context(
            case,
            user_message,
            kb_results,
            max_tokens=section_budget,
            provider_name=provider_name,
            model_name=model_name,
            use_state_summary=use_state_summary,
            processing_mode=processing_mode,
            entity_highlight_groups=entity_highlight_groups,
            tools_available=tools_available,
        )

    def _render(ctx: dict) -> str:
        """Format the full prompt from a prebuilt section ctx dict."""
        # Engine-resolved (not LLM-decidable): which page-capture guidance the
        # follow-up suggestions block renders. See _page_capture_hint.
        ctx["page_capture_hint"] = _page_capture_hint(getattr(case, "source", None))

        # #1328: a question about the assistant itself. Rendered as a block
        # in INQUIRY (which has no stage slot) and as the stage instructions
        # in INVESTIGATING; empty everywhere else so the slot costs nothing.
        is_agent_meta = processing_mode == "agent_meta"
        ctx["agent_meta_instructions"] = (
            AGENT_META_INSTRUCTIONS + "\n\n" if is_agent_meta else ""
        )

        if case.state == CaseState.INQUIRY:
            return INQUIRY_TEMPLATE.format(**ctx)

        elif case.state == CaseState.INVESTIGATING:
            stage = case.current_stage or InvestigationStage.DIAGNOSIS

            # knowledge_query and agent_meta dispatch to their own
            # instructions, bypassing stage logic. This prevents EVIDENCE
            # GROUNDING and DIAGNOSTIC REASONING REQUIREMENTS from forcing the
            # LLM to cite case evidence for general knowledge questions, or
            # for questions about FaultMaven itself (#1328).
            if processing_mode == "knowledge_query":
                adaptive_instr = KNOWLEDGE_QUERY_INSTRUCTIONS
            elif is_agent_meta:
                adaptive_instr = AGENT_META_INSTRUCTIONS
            else:
                # Dispatch to stage instructions (derived display stage)
                if stage == InvestigationStage.DIAGNOSIS:
                    adaptive_instr = _select_diagnosis_block(case)
                elif stage == InvestigationStage.MITIGATION:
                    adaptive_instr = MITIGATION_INSTRUCTIONS
                elif stage == InvestigationStage.TREATMENT:
                    adaptive_instr = TREATMENT_INSTRUCTIONS
                else:
                    adaptive_instr = _RCA_DIAGNOSIS_BLOCK

            # Add stage to context for schema reference
            ctx["stage"] = stage.value if stage else "diagnosis"

            # knowledge_query exempts from evidence grounding AND diagnostic
            # reasoning (KNOWLEDGE_QUERY_INSTRUCTIONS waives both — a
            # general-knowledge answer doesn't ground in case evidence or use
            # the Observation/Analysis/Conclusion structure).
            # agent_meta is waived the same way (#1328): the grounding block is
            # what made "what model are you" a request for deployment manifests.
            waive_grounding = processing_mode == "knowledge_query" or is_agent_meta
            # Waiving grounding used to take the TIME ATTRIBUTES definition
            # with it, while the rule that CONSUMES fresh_this_turn ("user
            # implies new data but no item carries it → ask for the file")
            # sits unconditionally in INVESTIGATION_BASE. Both modes still
            # render <evidence_collected> with the attribute on it, so the
            # model was handed the rule and no statement of which turn the
            # attribute names — the one reading that has to be stated, since
            # what a re-cited file carries is ABSENCE (#512). Keep the
            # definition on those turns and nothing else from the block: a
            # general-knowledge answer still must not go hunting case evidence,
            # which is what the waiver is for.
            evidence_grounding = (
                _OBSERVATION_TIME_BLOCK
                if waive_grounding
                else _EVIDENCE_GROUNDING_BLOCK
            )
            diagnostic_reasoning = (
                "" if waive_grounding else _DIAGNOSTIC_REASONING_BLOCK
            )

            return INVESTIGATION_BASE.format(
                adaptive_instructions=adaptive_instr,
                evidence_grounding=evidence_grounding,
                diagnostic_reasoning=diagnostic_reasoning,
                **ctx,
            )

        else:  # TERMINAL (RESOLVED/CLOSED)
            # "resolution" for RESOLVED, "closure" for CLOSED — the noun used
            # in the canonical summary type names (RESOLUTION_SUMMARY /
            # CLOSURE_SUMMARY). Lets the redirect message read naturally
            # ("the resolution summary") regardless of which terminal state.
            summary_kind = (
                "resolution" if case.state == CaseState.RESOLVED else "closure"
            )
            return TERMINAL_TEMPLATE.format(
                state_upper=case.state.value.upper(),
                state_lower=case.state.value,
                summary_kind=summary_kind,
                **ctx,
            )

    return _budgeted_prompt(
        case,
        user_message,
        _build_ctx,
        _render,
        provider_name,
        model_name,
        target_tokens=target_tokens,
    )


def _budgeted_prompt(
    case: Case,
    user_message: str,
    build_ctx,
    render,
    provider_name: Optional[str],
    model_name: Optional[str],
    target_tokens: Optional[int] = None,
) -> str:
    """Whole-prompt token-budget accountant + overflow/starvation backstop.

    See docs/architecture/investigation-engine/prompt-token-budget-allocation.md.

    Every prompt is assembled through the priority-greedy allocator: reserve the
    bounded fixed parts, fill the variable sections by strict priority up to their
    caps, compact each to fit, then measure the real assembled total against the
    model's hard ceiling (overflow → tighter re-assembly → minimal fallback).
    """
    from faultmaven.config.settings import get_settings
    from faultmaven.utils.model_context import resolve_model_budget
    from faultmaven.utils.token_estimation import estimate_tokens

    def _count(text: str) -> int:
        return estimate_tokens(
            text, provider=provider_name or "local", model=model_name
        )

    try:
        pb = get_settings().prompt_budget
        margin, min_viable = pb.overhead_margin_tokens, pb.min_viable_tokens
    except Exception:
        margin, min_viable = 256, 1500

    # Single assembly path: resolve the budget (unknown provider/model trusts the
    # configured target with no hard ceiling) and assemble through the allocator.
    resolved = resolve_model_budget(provider_name, model_name)
    if target_tokens is not None:
        # A caller-imposed hard cap (#614): lower the fill target AND the
        # ceiling, so the overflow backstop enforces it — re-assemble tighter,
        # then the minimal fallback. Never raises either value.
        cap = max(1, int(target_tokens))
        resolved = dataclasses.replace(
            resolved,
            prompt_target=min(resolved.prompt_target, cap),
            prompt_budget=(
                cap
                if resolved.prompt_budget is None
                else min(resolved.prompt_budget, cap)
            ),
        )
    return _assemble_allocated(
        case,
        build_ctx,
        render,
        resolved,
        margin,
        min_viable,
        _count,
        user_message,
        provider_name,
        model_name,
    )


_RESERVE_KEYS = (
    "identity",
    "core_context",
    "milestones",
    "inquiry_state",
    "pending_action",
    "system_feedback",
    "user_message",
)


def _assemble_allocated(
    case,
    build_ctx,
    render,
    resolved,
    margin,
    min_viable,
    count,
    user_message,
    provider_name,
    model_name,
) -> str:
    """Allocator assembly: template-aware section budget + backstop (§4/§6/§7)."""
    target = resolved.prompt_target
    ceiling = resolved.prompt_budget  # None when window unknown
    # What a fallback from either arm below must fit: the hard ceiling when the
    # window is known, the operator's target otherwise. Without it the fallback
    # would shrink to the smallest ceiling there is on every model (#1688).
    fallback_budget = (ceiling if ceiling is not None else target) - margin

    # First cut: size sections to the full target, then measure the fixed
    # template overhead so we can re-size so template + sections ≈ target (§6).
    ctx = build_ctx(target)
    # Single pass over the ctx: accumulate the total section tokens and the
    # reserve subset together (avoids tokenizing every section twice).
    reserve_keys = frozenset(_RESERVE_KEYS)
    section_sum = 0
    reserve_tokens = 0
    for k, v in ctx.items():
        if not v:
            continue
        n = count(v)
        section_sum += n
        if k in reserve_keys:
            reserve_tokens += n
    prompt = render(ctx)
    total = count(prompt)
    template_overhead = max(0, total - section_sum)

    # Starvation (§7 trigger #2): check the room left for VARIABLE content AFTER
    # both the fixed template AND the (non-trimmable) reserve. A fat reserve
    # (e.g. a near-cap pasted user_message) can leave near-zero for variable
    # sections even when target − template alone looks fine; the minimal
    # FALLBACK_* (smaller skeleton) is then the better use of the budget.
    #
    # The floors pass A will grant (evidence, then continuity) are deliberately
    # NOT subtracted here (#610): room for sections below the conversation is a
    # documented non-goal — they degrade by priority and carry INV-4's marker —
    # while evidence and continuity content is guaranteed on every main-template
    # prompt as long as min_viable exceeds the evidence floor. See
    # prompt-token-budget-allocation.md §7.
    variable_room = target - template_overhead - reserve_tokens - margin
    if variable_room < min_viable:
        fb = get_fallback_prompt_for_case(
            case, user_message, max_tokens=fallback_budget
        )
        logger.warning(
            "prompt_starvation_fallback",
            extra={
                "case_id": case.case_id,
                "provider": provider_name,
                "model": model_name,
                "template_overhead": template_overhead,
                "reserve_tokens": reserve_tokens,
                "variable_room": variable_room,
                "min_viable": min_viable,
                "fallback_tokens": count(fb),
                "action": "minimal_fallback_prompt",
            },
        )
        return fb

    # Re-size sections so the WHOLE prompt fits the target (template reserved) —
    # but skip the re-assembly when the first cut already fits the target (small
    # cases): the sections didn't use their full allotment, so re-sizing is a
    # wasted second assembly + tokenization.
    section_budget = target - template_overhead - margin
    if total > target and section_budget < target:
        ctx = build_ctx(section_budget)
        prompt = render(ctx)
        total = count(prompt)

    # Overflow (§7 trigger #1): exceed the model's HARD ceiling (when known).
    if ceiling is not None and total > ceiling:
        retry_budget = ceiling - template_overhead - margin
        if retry_budget >= min_viable:
            # Only re-assemble if there's viable room; otherwise go straight to
            # the fallback rather than build a prompt that can't fit anyway.
            ctx = build_ctx(retry_budget)
            prompt = render(ctx)
            total = count(prompt)
        if total > ceiling:
            fb = get_fallback_prompt_for_case(
                case, user_message, max_tokens=fallback_budget
            )
            logger.warning(
                "prompt_overflow_fallback",
                extra={
                    "case_id": case.case_id,
                    "provider": provider_name,
                    "model": model_name,
                    "tokens": total,
                    "hard_ceiling": ceiling,
                    "fallback_tokens": count(fb),
                    "action": "minimal_fallback_prompt",
                },
            )
            return fb
        logger.warning(
            "prompt_overflow_trimmed",
            extra={
                "case_id": case.case_id,
                "provider": provider_name,
                "model": model_name,
                "tokens": total,
                "hard_ceiling": ceiling,
                "action": "reassembled_at_tighter_budget",
            },
        )

    logger.debug(
        "prompt_budget_ok",
        extra={
            "case_id": case.case_id,
            "provider": provider_name,
            "model": model_name,
            "assembled_tokens": total,
            "prompt_target": target,
            "hard_ceiling": ceiling,
            "template_overhead": template_overhead,
        },
    )
    return prompt
