from faultmaven.core.investigation.cause_assurance import runbook_conversion_ready
from faultmaven.modules.case.contracts import CaseState

from .cause_state import (
    _get_root_cause_summary,
    _get_solution_summary,
)


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

    Used where the engine controls the reply text directly: the explicit
    confirm-button path. The dropdown-resolution path was the other caller
    until RESOLVED left the menu. Prepends a deterministic status line (e.g. "Case closed.") and
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
    by the removed KB cause seeder (legacy rows only — see
    ``seeded_provenance``): generating one would only duplicate the runbook the
    case was resolved by applying. The async similarity dedup at action time
    (a ≥70% match by title and score for the user to judge) is the tier that
    applies to every case.
    """
    if not runbook_conversion_ready(case):
        return None
    from faultmaven.core.investigation.seeded_provenance import (
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
