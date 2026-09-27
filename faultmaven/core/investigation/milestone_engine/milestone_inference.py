import logging
from datetime import datetime
from typing import Any

from faultmaven.core.investigation.schemas import (
    BaseInteractionResponse,
    InquiryResponse,
    TerminalResponse,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
    EvidenceCategory,
    EvidenceSourceType,
    InvestigationProgress,
)

from .stage_gates import CATEGORY_MILESTONE_MAP

logger = logging.getLogger(__name__)


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


def _milestone_already_recorded(
    progress: InvestigationProgress, milestone: str
) -> bool:
    """Whether the case already records ``milestone`` as reached.

    Covers the boolean fields of ``MilestoneUpdates``. A name it does not know
    reads as not recorded, so a milestone added to the schema later is still
    validated rather than waved through.
    """
    mitigation = progress.mitigation
    recorded = {
        "symptom_verified": progress.symptom_verified,
        "solution_accepted": progress.solution_accepted,
        "mitigation_accepted": mitigation is not None and mitigation.accepted,
        "mitigation_verified": mitigation is not None and mitigation.verified,
    }
    return bool(recorded.get(milestone, False))


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
        A milestone the case already records is not a completion, so it is
        never offending.

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

    # Get list of milestone fields being completed (set to True). A milestone
    # the case already records is a restatement, not a completion: the prompt
    # and ``MilestoneJustifications`` ask for a justification only for a
    # milestone the model CHANGES, and models restate standing booleans.
    # Rejecting the restatement stripped a no-op and would feed back that an
    # achieved milestone was rejected (fm#1677). The apply path and the stage
    # gate absorb a restatement themselves.
    completed_milestones = []
    milestone_dict = milestones.model_dump(exclude_none=True)
    for milestone_name, value in milestone_dict.items():
        if (
            isinstance(value, bool)
            and value is True
            and not _milestone_already_recorded(case.progress, milestone_name)
        ):
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
    #
    # One message for all of them, not one each: the errors are delivered to
    # the next turn through ``system_feedback``, which the turn record caps at
    # 1000 characters, so their size must not grow with the milestone count.
    justifications = internal_reasoning.milestone_justifications.as_dict()
    unjustified = [m for m in completed_milestones if m not in justifications]
    if unjustified:
        offending.update(unjustified)
        errors.append(
            f"Milestones {unjustified} completed without justification. To "
            "claim one, set it True again and set "
            "internal_reasoning.milestone_justifications.<milestone> to the "
            "evidence that shows it, citing evidence IDs; null or blank is "
            "no justification."
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
        f"Post-processing LLM response: evidence_to_add_count={len(evidence_to_add)}"
    )
    return updates
