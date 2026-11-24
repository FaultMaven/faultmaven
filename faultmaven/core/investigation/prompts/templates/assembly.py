"""Prompt assembly: builds the final prompt string for a case's state and stage."""

import dataclasses
import logging
from typing import Any, Dict, List, Optional, Sequence

from faultmaven.core.investigation.kb_question import KbLookup, lookup_entries_only
from faultmaven.core.investigation.narration.notices import (
    RESOLVE_DECLINED_RULE,
    resolve_declined_actions,
)
from faultmaven.core.investigation.narration.prompt_state import (
    PROGRESS_OMITTED,
    FocusSelection,
    FocusZone,
    HoldKind,
    ProblemHold,
    ResolveDeclined,
    focus_selection,
)
from faultmaven.core.investigation.prompts.context_builder.assembly import (
    build_investigation_context,
)
from faultmaven.core.investigation.prompts.context_builder.entity_highlights import (
    EntityHighlightGroup,
)
from faultmaven.core.investigation.prompts.context_builder.narration_fold import (
    NarrationFold,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
    InvestigationStage,
)

from .blocks import (
    _DIAGNOSTIC_REASONING_BLOCK,
    _OBSERVATION_TIME_BLOCK,
    _WORKING_VOCABULARY_RULE,
    AGENT_META_INSTRUCTIONS,
    evidence_grounding_block,
    knowledge_query_instructions,
    tool_results_note,
    unseen_text_rule,
)
from .diagnosis import _CHAIN_EMISSION_BLOCK, rca_diagnosis_block
from .fallback import get_fallback_prompt_for_case
from .inquiry import (
    INQUIRY_TEMPLATE,
    inquiry_knowledge_help,
    inquiry_reading_uploaded_files,
)
from .investigation import (
    INVESTIGATION_BASE,
    next_data_point_fetch,
    prior_files_note,
)
from .terminal import (
    NO_TERMINAL_AFFORDANCES,
    TERMINAL_TEMPLATE,
    TerminalAffordances,
    terminal_runbook_rule,
    terminal_summary_rule,
)
from .tool_text import NO_TOOLS, GivenTools, tools_line
from .treatment import mitigation_instructions, treatment_instructions

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class PromptReport:
    """What a turn's last prompt build sent (#2811 S2c-3b): ``fallback`` when
    the minimal fallback prompt replaced the assembled one (the budget
    backstop here, or the provider context-length retry in ``generation``).
    The fallback carries no outcome rule, so its turn is not counted."""

    fallback: bool = False


# =============================================================================
# DEGRADED MODE INSTRUCTIONS
# =============================================================================


# =============================================================================
# BUILDER FUNCTIONS
# =============================================================================


def _false_alarm_declined_line(turn: int) -> str:
    """The false-alarm hold's closing rule once the user has declined the
    close. Conditional, never a flat ban (#1889): a model that obeys a ban
    never proposes, so the engine never attaches the close card, and a user
    who later says "ok, close it" on a client with no status menu has no way
    to close."""
    return (
        f"The user declined closing on this finding at turn {turn}; the case "
        "holds on their answer. Propose a close only if the user directs it; "
        "the engine then attaches the Close action to your reply. Do not "
        "propose it unprompted."
    )


#: The line the prompt carries while the user's decline of the resolution
#: stands (#1895): ``RESOLVE_DECLINED_RULE``, the same rule the step-2
#: refusal's feedback states. A NEW verification is evidence, recorded and
#: then proposed on; a request is not. Where to point the user
#: (``RESOLVE_DECLINED_ACTIONS``) follows it only while the chips it names
#: are on the reply (``_resolve_declined_emphasis``, #2472).
RESOLVE_DECLINED_LINE = (
    "**THE USER DECLINED MARKING THIS CASE RESOLVED.** It stays open on their "
    "answer. " + RESOLVE_DECLINED_RULE + " Do not narrate the case as resolved."
)


#: The line the prompt carries on the turn the user declines the resolution
#: (#2223, ruling (b)), beside ``RESOLVE_DECLINED_LINE``: the decline is
#: answered by the model, which asks what would settle it. A READY case already
#: holds both confirmation rows, so the line says so and forbids recording
#: them again this turn; a report in the user's message is requested as data
#: under the chat-data rules (``system_state_report.request``).
RESOLVE_DECLINED_THIS_TURN_LINE = (
    "**THE USER HAS JUST DECLINED MARKING THIS CASE RESOLVED.** The case record "
    "already holds the confirmation the offer rested on (the cause shown "
    "removed, the reported problem seen gone after the fix); do not record the "
    "cause or the problem as gone again this turn. Ask what they still need to "
    "see before calling it resolved, or what makes them doubt the fix, and "
    "name the one check that would settle it (for example the fix mechanism's "
    "own state, a schedule's last run, or a window with no recurrence). If "
    "their message itself reports the state of their system, follow the "
    "chat-data rules: request that output with `system_state_report.request`, "
    "not in your reply."
)


def _resolve_declined_emphasis(declined: Optional[ResolveDeclined]) -> str:
    """``RESOLVE_DECLINED_LINE`` while the user's decline of the resolution
    stands (``prompt_state.resolve_declined``, the record's
    ``resolution.declined_stands``), else "", and
    ``RESOLVE_DECLINED_THIS_TURN_LINE`` after it when that decline was given
    on the current turn (#2223). One selection decides both lines, so they
    cannot disagree. Appended to the focus block on every stage and processing
    mode, never replacing it.

    ``RESOLVE_DECLINED_ACTIONS`` (point the user at "Mark it resolved" and
    "Close without resolving") follows the line only when the projection builds
    those chips (``project_offers``, #2472): no offer open (``OfferView``), the
    record's problem not on hold. While a check the model asked for after the fix
    holds the case (the record's ``resolution.pending_check_need_id``) it
    points at "Close without resolving" only (``resolve_declined_actions``,
    #1906 D5.2). The prompt predicts the reply's chips from the case as the
    turn starts. Three same-turn events can remove them after the prompt has
    pointed at them: a revision the model proposes (the problem goes on hold),
    a disqualification that ends the resolve decline's entry (the closure
    verdict leaves SUGGEST_RESOLVE), and a false-alarm finding (the problem
    goes on hold)."""
    if declined is None:
        return ""
    line = RESOLVE_DECLINED_LINE
    if declined.actions:
        line = f"{line} {resolve_declined_actions(declined.held)}"
    if declined.this_turn:
        return f"\n{line}\n{RESOLVE_DECLINED_THIS_TURN_LINE}\n"
    return f"\n{line}\n"


def _problem_hold_emphasis(hold: Optional[ProblemHold]) -> str:
    """The focus block for a case whose problem statement itself is in
    question, or "" when none is. Rendered as ``{focus_emphasis}`` on EVERY
    stage, not only DIAGNOSIS (#1889): a false alarm found while a fix was
    accepted (TREATMENT) or a mitigation was in flight (MITIGATION) holds just
    the same, and those stages' own instructions tell the model to propose a
    transition on stabilisation, which the hold has to override.

    - REVISION_PENDING: a revised statement awaits the user's re-confirmation.
    - INVALIDATED: the evidence showed the reported symptom was never present.
      The engine offered the close; the model proposes a transition only when
      the user directs it, the rule every stage carries for a close. Once the
      user declined it, the block says so and when.

    ``hold`` is the record's (``prompt_state.problem_hold``).
    """
    if hold is None:
        return ""
    if hold.kind == HoldKind.REVISION:
        return """
**INVESTIGATION PROGRESS: Revised problem statement awaiting confirmation**
The evidence showed a different problem than the one the user confirmed, and
the revised statement is waiting for their answer (the engine shows it to them
below your reply). Answer what they asked. Cause work you send now is held and
applied when they confirm; if they correct the revision, send a better
revised_problem_statement. Do not propose a transition until they answer.
"""
    if hold.kind == HoldKind.FALSE_ALARM:
        closing = (
            "The engine has offered the user the close. Propose a transition "
            "only when the user directs it."
            if hold.declined_at is None
            else _false_alarm_declined_line(hold.declined_at)
        )
        return f"""
**INVESTIGATION PROGRESS: Reported problem not present (false alarm)**
The evidence showed the reported symptom was not present where and when it was
reported. There is nothing to diagnose or fix, and hypotheses, solutions and
mitigations are not accepted. Two things move the case:
- New evidence of a DIFFERENT problem: send it with revised_problem_statement
  (describing what is observed) for the user to confirm.
- The user disputes the finding with new information: set
  invalidation_withdrawn with withdrawal_basis, and verification starts again.
{closing}
"""
    return ""


def _verifying_rows_missing_emphasis(missing: bool) -> str:
    """One line while the problem is verified but no row is on record as having
    shown it (#1906, ruled 2026-11-03): no post-fix re-check can bind to the
    problem until one is cited. Appended to the focus block on every stage.
    ``missing`` is the record's (``prompt_state.verifying_rows_missing``)."""
    if not missing:
        return ""
    return (
        "\nNo row is on record as what showed the problem: cite the "
        "symptom_evidence rows already on record that showed it in "
        "symptom_verification_evidence_ids. Until then no re-check can confirm "
        "the problem gone.\n"
    )


def _get_diagnosis_focus_emphasis(zone: FocusZone) -> str:
    """The focus emphasis of a diagnosing case's zone (Framework §8.5).

    Returns a contextual status signal rendered as ``{focus_emphasis}`` at the
    top of the prompt's per-turn tail, right after ``CACHE_BOUNDARY`` (#613),
    on DIAGNOSIS turns. Informs the agent where the investigation stands and
    what would advance it, WITHOUT overriding the user's question.

    Called only when no problem hold stands: the two holds (a revision
    awaiting re-confirmation, a false alarm) render from
    ``_problem_hold_emphasis`` on every stage, above this dispatch.

    ``zone`` is the record's (``prompt_state.focus_zone``), from the
    milestones it shows:
    - Zone 1 (VERIFY): symptom_verified not shown — a three-way verdict:
      verified, revised (inaccurate statement) or invalidated (false alarm)
    - Zone 2 (CAUSE): symptom_verified, root_cause_identified not shown — root
      cause analysis; CAUSE_STALE when the symptom's last observation is
      stale (read fresh), which names its observation window. Without it the
      zone says "Symptoms are confirmed" and nothing about WHERE to look, so
      evidence requests drift to the present — the failure mode in which an
      investigation queries the last 30 minutes for a symptom observed two
      hours earlier.
    - Zone 3 (SOLUTION): root_cause_identified, solution_proposed not shown —
      propose fix
    - Zone 3 pending (PENDING): solution_proposed — awaiting execution,
      NON-suppressive hold that yields to root-cause analysis on new
      evidence/dispute (INV-33)
    """
    if zone == FocusZone.VERIFY:
        return """
**INVESTIGATION PROGRESS: Symptom verification pending**
No symptoms have been formally confirmed. Check the data against the confirmed
problem statement and reach one of three verdicts:
- It shows the stated symptom → symptom_verified, with the symptom_evidence,
  citing its ids in symptom_verification_evidence_ids.
- It shows a real problem the statement describes INACCURATELY — a different
  symptom, component, scope or time, such that the statement would misdirect
  the investigation → verification_updates.revised_problem_statement, citing
  the symptom_evidence. Describe what is observed, never its cause. Added
  precision alone is not a revision.
- It shows the reported symptom was NEVER present where and when it was
  reported (a false alarm) → verification_updates.problem_invalidated, citing
  symptom_absence_evidence from that window.
The problem not happening right now is NOT a false alarm, and missing data is
neither a revision nor a false alarm — ask for the data.

Cause work waits for it: hypotheses, causal chains and root-cause conclusions
are not accepted until the symptom is verified. When the data that verifies it
also shows the cause, verify and form the hypotheses in the same response.

Data showing the problem at an earlier time DOES verify it — a problem is worth
investigating while it EXISTS (evidence still collectible, root cause
unidentified, solution unknown), whether or not it is firing right now. Do not
withhold symptom_verified because the evidence is not from this minute.

What the observation time governs is WHERE the investigation looks. State it
explicitly when you verify: it becomes the window every later evidence request
targets, and requests must name absolute timestamps rather than relative ones,
which silently drift to the present.
"""
    elif zone == FocusZone.CAUSE_STALE:
        return """
**INVESTIGATION PROGRESS: Root cause analysis — anchor to the symptom's window**
The symptom was observed some time ago (see the observation time on
symptom_verified below). That period, not the present, is where this
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
    elif zone == FocusZone.CAUSE:
        return """
**INVESTIGATION PROGRESS: Root cause analysis**
Symptoms are confirmed. When evaluating evidence, focus on hypotheses
explaining the root cause. Two independent causal observations grounding a
hypothesis's chain root, and that hypothesis marking the root terminal
(root_is_terminal: the ROOT-CAUSE TEST's why-stopping verdict, citing its
causal_evidence in terminal_basis_evidence_ids), are what let the engine mark
the cause identified.

If new data shows the symptom claim itself was wrong — misread data, the wrong
system, an artefact — do not absorb it as noise. Say so, record it, and set
symptom_verified=False with a justification; if the data shows what the
problem actually is, send the revised_problem_statement for the user to
confirm, or problem_invalidated if it was never present. (The problem merely
not occurring right now is NOT that: an existing problem is investigable
whether or not it is currently firing.)
"""
    elif zone == FocusZone.SOLUTION:
        return """
**INVESTIGATION PROGRESS: Solution needed**
Root cause is identified. A concrete, executable fix with specific commands
advances the investigation to Treatment.
"""
    else:
        return """
**INVESTIGATION PROGRESS: Solution proposal issued — awaiting execution**
A fix has been proposed and is awaiting execution. If the user reports executing
it, set solution_accepted=True with accepted_action_ref naming its ACTION_ID
(<pending_action>), and infer the transition to TREATMENT.
This hold is NOT a freeze. If the user's reply instead brings new evidence,
questions the fix, or points at a different cause, resume root-cause analysis on
that signal — investigate it rather than repeating the standby. A pending
proposal never forecloses a live diagnostic thread.
"""


_FOCUS = "focus emphasis"


def _focus_lines(selection: FocusSelection) -> str:
    """The focus emphasis's text for ``selection``: the hold, else the zone;
    the resolve decline; the verifying rows missing."""
    focus = _problem_hold_emphasis(selection.hold)
    if selection.zone is not None:
        focus = _get_diagnosis_focus_emphasis(selection.zone)
    focus += _resolve_declined_emphasis(selection.resolve_declined)
    return focus + _verifying_rows_missing_emphasis(selection.verifying_rows_missing)


def _focus_emphasis(
    fold: NarrationFold, current_turn: int, *, steered: bool, zoned: bool
) -> str:
    """The focus emphasis, selected from the prompt assembly's record and its
    fresh inputs (``prompt_state.focus_selection``, #2811 S2b-1c-i). If the
    record or a fresh input the selection asks for cannot be read, the whole
    emphasis is the one declared line ``PROGRESS_OMITTED``, in place of the
    hold, the zone, the resolve decline and the verifying-rows lines; the
    failure is logged and counted once per assembly (``NarrationFold``), and
    the prompt is built. Only the selection is guarded: a fault in the text
    raises, as on ``main``."""
    try:
        outcome = fold.outcome(_FOCUS)
        selection = (
            None
            if outcome is None
            else focus_selection(
                outcome, fold.fresh(), current_turn, steered=steered, zoned=zoned
            )
        )
    except Exception:  # noqa: BLE001 — only the emphasis is lost (#2811)
        fold.fail(_FOCUS)
        selection = None
    if selection is None:
        return f"\n{PROGRESS_OMITTED}\n"
    return _focus_lines(selection)


def _select_diagnosis_block(case: Case, given: GivenTools) -> str:
    """The RCA diagnosis block for a diagnosing turn, naming only the tools
    ``given``.

    Diagnosis has no path fork (redesign R5): hypothesis formulation and
    evidence-needs run as a single opportunistic flow. Stage guidance is
    selected from the indicators the case outcome record shows
    (``symptom_verified`` / ``root_cause_identified`` / ``solution_proposed``,
    ``prompt_state.focus_zone``) and rendered by
    ``_get_diagnosis_focus_emphasis`` — not by a prospective path choice.
    ``rca_diagnosis_block`` carries the hypothesis-evidence ordering mandate
    (``_HYPOTHESIS_EVIDENCE_ORDERING_BLOCK``), then the chain-emission block
    teaches lazy backward expansion (the engine ingests the emitted chain).

    The focus emphasis is NOT part of this block (#613). It moves with the
    progress milestones and, in Zone 2, with the wall clock (symptom
    currency), while this block is the last part of the prompt's cached
    prefix. It renders as ``{focus_emphasis}`` at the top of the per-turn
    tail instead (``get_prompt_for_case``).
    """
    return rca_diagnosis_block(given) + _CHAIN_EMISSION_BLOCK


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
    given_tools: GivenTools = NO_TOOLS,
    target_tokens: Optional[int] = None,
    kb_rendered: Optional[List[Dict[str, Any]]] = None,
    terminal_affordances: TerminalAffordances = NO_TERMINAL_AFFORDANCES,
    kb_lookup: Optional[KbLookup] = None,
    prompt_report: Optional[PromptReport] = None,
) -> str:
    """Build the final prompt based on case state and stage.

    ``kb_rendered``, when given, is refilled with the KB entries the returned
    prompt actually carries: the push entries whose header survived the
    section budget — none when the template has no ``{kb_results}`` slot
    (terminal) — then the entries of ``kb_lookup``'s block, which
    every template renders; and nothing at all when the minimal fallback
    replaced the assembled prompt. The engine passes it so a turn's
    ``sources`` name only what the model was shown.

    ``kb_lookup`` is this turn's knowledge-base question lookup (§5.6 of
    ``orchestration-capabilities.md``); ``None`` on a turn that asked none.

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
        given_tools: The tool names this turn gives the model (#1939): the
            names of ``build_da_tool_schemas()`` on a tool-loop turn,
            ``NO_TOOLS`` on a tool-less one. Every tool the prompt names is in
            it, and it gates the directed-analysis evidence index+stub elision
            (the extract is dropped only when ``search_file`` is given). The
            default names no tool: a caller that does not say what the turn
            offers gets a prompt that points at nothing.
        target_tokens: Optional HARD cap on the assembled prompt, in the
            provider's estimated tokens. It lowers both the fill target and the
            overflow ceiling the backstop enforces, so the result fits it or is
            the minimal fallback prompt. The tool loop uses it to re-assemble a
            base that fits what is left of its per-call budget after the system
            instruction and the ``tools=`` payload (#614). ``None`` (every other
            caller) leaves the resolved budget unchanged.
        terminal_affordances: What a terminal Q&A turn offers below its reply
            (#1955), built from the suggestion list the engine attaches to it.
            The default offers nothing, so the prompt points at no suggestion.
        prompt_report: When given, set on every build to whether the minimal
            fallback replaced the assembled prompt, as ``kb_rendered`` is
            refilled: the last build is the prompt the model answered from.

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

    # One narration fold per assembly (#2811 S2b-1a, S2b-1b-ii, S2b-1c-i): the
    # ctx below is built up to three times, and the fallback the backstop may
    # return instead renders several candidates; every block of all of
    # them folds and reads the needs pool once.
    narration_fold = NarrationFold(case)

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
            given_tools=given_tools,
            kb_rendered=kb_rendered,
            kb_lookup=kb_lookup,
            narration_fold=narration_fold,
        )

    rendered: Dict[str, str] = {}

    def _render(ctx: dict) -> str:
        prompt = _render_template(ctx)
        rendered["prompt"] = prompt
        return prompt

    def _render_template(ctx: dict) -> str:
        """Format the full prompt from a prebuilt section ctx dict."""
        # Engine-resolved (not LLM-decidable): which page-capture guidance the
        # follow-up suggestions block renders. See _page_capture_hint.
        ctx["page_capture_hint"] = _page_capture_hint(getattr(case, "source", None))
        # What this turn's tools are, stated once at the top of every template
        # (#1939). Sorted, so one set renders one byte string (#613 prefix).
        ctx["tools_this_turn"] = tools_line(given_tools)
        ctx["tool_results_rule"] = tool_results_note(given_tools)
        # Both templates that render a cut, elided or omitted item (#2401).
        ctx["unseen_text"] = unseen_text_rule(given_tools)

        # #1328: a question about the assistant itself. Rendered as a block
        # in INQUIRY (which has no stage slot) and as the stage instructions
        # in INVESTIGATING; empty everywhere else so the slot costs nothing.
        is_agent_meta = processing_mode == "agent_meta"
        ctx["agent_meta_instructions"] = (
            AGENT_META_INSTRUCTIONS + "\n\n" if is_agent_meta else ""
        )

        if case.state == CaseState.INQUIRY:
            return INQUIRY_TEMPLATE.format(
                knowledge_help=inquiry_knowledge_help(given_tools),
                reading_uploaded_files=inquiry_reading_uploaded_files(given_tools),
                **ctx,
            )

        elif case.state == CaseState.INVESTIGATING:
            stage = case.current_stage or InvestigationStage.DIAGNOSIS

            # knowledge_query and agent_meta dispatch to their own
            # instructions, bypassing stage logic. This prevents EVIDENCE
            # GROUNDING and DIAGNOSTIC REASONING REQUIREMENTS from forcing the
            # LLM to cite case evidence for general knowledge questions, or
            # for questions about FaultMaven itself (#1328).
            # The focus emphasis renders in the per-turn tail, not in the stage
            # instructions: it moves with the milestones and the wall clock,
            # and the stage instructions close the cached prefix (#613). A
            # problem hold renders on every stage (#1889); the zone emphasis
            # only on DIAGNOSIS; a standing resolve decline is appended on
            # every stage (#1895) and every processing mode (#2223). A
            # knowledge_query or agent_meta turn carries nothing else. Its
            # lines are selected from the case outcome record
            # (``_focus_emphasis``, #2811 S2b-1c-i).
            steered = processing_mode != "knowledge_query" and not is_agent_meta
            if processing_mode == "knowledge_query":
                adaptive_instr = knowledge_query_instructions(given_tools)
            elif is_agent_meta:
                adaptive_instr = AGENT_META_INSTRUCTIONS
            # Dispatch to stage instructions (derived display stage)
            elif stage == InvestigationStage.DIAGNOSIS:
                adaptive_instr = _select_diagnosis_block(case, given_tools)
            elif stage == InvestigationStage.MITIGATION:
                adaptive_instr = mitigation_instructions(given_tools)
            elif stage == InvestigationStage.TREATMENT:
                adaptive_instr = treatment_instructions(given_tools)
            else:
                adaptive_instr = rca_diagnosis_block(given_tools)
            focus_emphasis = _focus_emphasis(
                narration_fold,
                case.current_turn,
                steered=steered,
                zoned=steered and stage == InvestigationStage.DIAGNOSIS,
            )

            # Add stage to context for schema reference
            ctx["stage"] = stage.value if stage else "diagnosis"

            # knowledge_query exempts from evidence grounding AND diagnostic
            # reasoning (knowledge_query_instructions waives both — a
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
            # which is what the waiver is for. The working-vocabulary rule
            # stays too: the base still names the ROOT-CAUSE TEST on those
            # turns, and the reply is still the user's to read (#2494).
            evidence_grounding = (
                _OBSERVATION_TIME_BLOCK + "\n" + _WORKING_VOCABULARY_RULE + "\n\n"
                if waive_grounding
                else evidence_grounding_block(given_tools)
            )
            diagnostic_reasoning = (
                "" if waive_grounding else _DIAGNOSTIC_REASONING_BLOCK
            )

            return INVESTIGATION_BASE.format(
                fetch_next_data_point=next_data_point_fetch(given_tools),
                prior_files=prior_files_note(given_tools),
                adaptive_instructions=adaptive_instr,
                focus_emphasis=focus_emphasis,
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
            if kb_rendered is not None:
                kb_rendered[:] = lookup_entries_only(kb_rendered)
            return TERMINAL_TEMPLATE.format(
                state_upper=case.state.value.upper(),
                state_lower=case.state.value,
                summary_kind=summary_kind,
                summary_request_rule=terminal_summary_rule(
                    summary_kind, terminal_affordances
                ),
                runbook_request_rule=terminal_runbook_rule(terminal_affordances),
                **ctx,
            )

    prompt = _budgeted_prompt(
        case,
        user_message,
        _build_ctx,
        _render,
        provider_name,
        model_name,
        given_tools,
        target_tokens=target_tokens,
        kb_lookup=kb_lookup,
        narration_fold=narration_fold,
    )
    # The overflow/starvation backstop returned the minimal fallback in place
    # of the assembled prompt.
    fell_back = prompt != rendered.get("prompt")
    if prompt_report is not None:
        prompt_report.fallback = fell_back
    if kb_rendered is not None and fell_back:
        # It carries no pushed KB context, only the lookup's verdict, which
        # names its candidates by title.
        kb_rendered[:] = lookup_entries_only(kb_rendered)
    return prompt


def _budgeted_prompt(
    case: Case,
    user_message: str,
    build_ctx,
    render,
    provider_name: Optional[str],
    model_name: Optional[str],
    given_tools: GivenTools,
    target_tokens: Optional[int] = None,
    kb_lookup: Optional[KbLookup] = None,
    narration_fold: Optional[NarrationFold] = None,
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
        given_tools,
        kb_lookup=kb_lookup,
        narration_fold=narration_fold,
    )


_RESERVE_KEYS = (
    "identity",
    "core_context",
    "milestones",
    "inquiry_state",
    "pending_action",
    "system_feedback",
    "exclusions",
    "fix_verification",
    "user_message",
    "kb_lookup",
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
    given_tools,
    kb_lookup: Optional[KbLookup] = None,
    narration_fold: Optional[NarrationFold] = None,
) -> str:
    """Allocator assembly: template-aware section budget + backstop (§4/§6/§7).

    A fallback either arm returns is rendered for ``given_tools``, the set the
    assembled prompt was built for: it replaces that prompt on the same call.
    It reads the assembly's record (``narration_fold``), so one assembly
    folds once, and a fold failure is counted once, whichever prompt it
    returns.
    """
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
            case,
            user_message,
            given_tools=given_tools,
            max_tokens=fallback_budget,
            kb_lookup=kb_lookup,
            narration_fold=narration_fold,
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
                case,
                user_message,
                given_tools=given_tools,
                max_tokens=fallback_budget,
                kb_lookup=kb_lookup,
                narration_fold=narration_fold,
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
