import logging
from typing import Optional

from faultmaven.core.investigation.cause_assurance import runbook_conversion_ready
from faultmaven.core.investigation.cause_identification import cause_identification_leg
from faultmaven.core.investigation.lifecycle_metrics import (
    case_outcome_write_failures_total,
)
from faultmaven.core.investigation.narration.offers import confirmation_message
from faultmaven.core.investigation.narration.replies import (
    _CLOSED_REPLIES,
    ResolutionContent,
    confirmation_params,
    confirmation_params_of,
    terminal_status_line,
)
from faultmaven.core.investigation.outcome import narration_outcome
from faultmaven.core.investigation.prompts.templates.terminal import (
    TerminalAffordances,
)
from faultmaven.core.investigation.terminal_transitions import resolution_record_lines
from faultmaven.modules.case.contracts import CaseOutcome, CaseState, WritePoint

from .cause_state import (
    _get_root_cause_summary,
    _get_solution_summary,
)

logger = logging.getLogger(__name__)


def resolution_content(case) -> ResolutionContent:
    """The case content the resolve confirmation names (ruling of
    2026-11-21): built here, not in ``narration``, whose import contract
    ``cause_state`` would break (it imports ``cause_assurance``)."""
    return ResolutionContent(
        root_cause=_get_root_cause_summary(case),
        solution=_get_solution_summary(case),
        has_evidence=bool(case.evidence),
        has_commands=any(
            getattr(s, "commands", None) or getattr(s, "implementation_steps", None)
            for s in case.solutions or ()
        ),
    )


def _reply_outcome(case) -> Optional[CaseOutcome]:
    """The record folded for the moment the reply narrates
    (``narration_outcome``), stored nowhere; None when the fold raised, which
    is logged with its traceback and counted on
    ``case_outcome_write_failures_total{at="narration"}``, once per call."""
    try:
        return narration_outcome(case)
    except Exception:  # noqa: BLE001 — the reply renders from its fallback
        logger.exception(
            "Case %s: the record could not be read for the reply (#2811)",
            getattr(case, "case_id", None),
        )
        try:
            case_outcome_write_failures_total.labels(at=WritePoint.NARRATION).inc()
        except Exception:  # noqa: BLE001 — a metrics error never fails a reply
            logger.exception("case_outcome_write_failures_total not counted")
        return None


def _confirmation_outcome_fresh(case) -> tuple[Optional[str], str]:
    """The confirmation's two record facts, derived from the case, when the
    fold raised: the decider's decide-time read (A4), removed at stage S3.
    The fold derives them through these same two functions, so a failure
    inside one of them raises again here, as the decider's read did on
    ``main``; only a fold failure outside them renders from this fallback."""
    return cause_identification_leg(case), resolution_record_lines(case)


def resolution_confirmation_params(case) -> dict:
    """The facts the resolve confirmation states (``narration.offers.
    resolution_confirmation``), read where the confirmation is decided (#2636,
    A4): whether a cause is identified and the rows that confirm the
    resolution from the record folded at that moment (#2811 S2b-2b), and the
    root cause, the solution and which optional details would improve the
    documentation from the case's content (``ResolutionContent``). A fold
    failure outside ``cause_identification_leg`` and
    ``resolution_record_lines`` renders from the decider's read
    (``_confirmation_outcome_fresh``); a failure inside them raises, as on
    ``main``.
    """
    outcome = _reply_outcome(case)
    if outcome is None:
        leg, record_lines = _confirmation_outcome_fresh(case)
        return confirmation_params_of(leg, record_lines, resolution_content(case))
    return confirmation_params(outcome, resolution_content(case))


def _build_resolution_confirmation(case) -> str:
    """The resolution confirmation prompt, with its optional enrichment
    hints, from the case as it stands."""
    return confirmation_message(resolution_confirmation_params(case))


def _record_free_reply(case) -> str:
    """The closed reply when no record can be read (the transition's write
    failed and the fold raised): the reason's line alone, with no mitigation
    sentence."""
    return _CLOSED_REPLIES[case.closure_reason]


def _terminal_confirmation_response(case) -> str:
    """Deterministic status line after a transition is confirmed
    (``narration.replies.terminal_status_line``).

    The closure's reason and mitigation line are the record's (#2811
    S2b-2b): the closure the terminal transition wrote, and a re-fold only
    when that write failed (the stored record is not terminal). The terminal
    reply is composed by ``_compose_terminal_reply`` which appends the
    auto-generated summary content (when produced).
    """
    if case.state == CaseState.RESOLVED:
        return terminal_status_line(case.state, None, None)
    stored = case.progress.outcome
    if stored is None or not stored.folded_under.is_terminal:
        stored = _reply_outcome(case)
        if stored is None:
            return _record_free_reply(case)
    return terminal_status_line(
        case.state, stored.closure.reason, stored.closure.statement
    )


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
    """
    if not runbook_conversion_ready(case):
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


def terminal_affordances(
    suggestions: list, *, runbook_draft_exists: bool
) -> TerminalAffordances:
    """What the terminal prompt may point at, read off ``suggestions`` (#1955).

    ``suggestions`` is the list the terminal Q&A reply is given, so this is
    the ONE predicate for "is the chip on screen": the prompt says "use the
    suggestion below" exactly when the suggestion is below. Nothing here
    re-derives the offer gates (``runbook_conversion_ready``, the regen cap,
    the draft lookup) — they already decided the list.
    """
    payloads = {s.get("payload") for s in suggestions}
    return TerminalAffordances(
        summary_regeneration=bool(
            payloads
            & {
                REGENERATE_RESOLUTION_SUMMARY_PAYLOAD,
                REGENERATE_CLOSURE_SUMMARY_PAYLOAD,
            }
        ),
        runbook_generation=GENERATE_RUNBOOK_PAYLOAD in payloads,
        runbook_draft_exists=runbook_draft_exists,
    )
