"""The prose of the engine's offers and requests (#2636, #2615 stage R).

Composers keyed on what a decider decided, a reason and its params, never on
the case: the readiness message (``terminal_transitions``'
``assess_resolution_readiness``, through ``ResolutionReadiness.message``), the
closure→resolve pivot (``assess_closure_readiness``) and the close summary
(``assess_close_summary``). The deciders derive the params; these compose.
"""

from typing import Optional, Sequence

from faultmaven.modules.case.contracts import ConfirmationGap, ResolutionReason


def mitigation_sentence(line: str) -> str:
    """``mitigation_closure_line``'s line standing as a sentence of its own."""
    return f"{line[:1].upper()}{line[1:]}."


#: The NEEDS_INFO frame's opening line and its data sentence, shared by the
#: documentation-gap ask and the held check (#1906 D5.2).
_NEEDS_INFO_INTRO = (
    "To mark this **resolved** and write up the resolution summary, I just need "
    "a few essentials:\n\n"
)
_SUBMIT_AS_DATA = (
    "Submit check output as data (pasted or attached, not typed into your "
    "message): what you write is your account, and the output is what shows it."
)


def _cause_recheck_ask(cause: Optional[str], fix: Optional[str]) -> str:
    """The readiness ask for the cause leg after a dated fix (#1927, #2557),
    naming the root's statement and the fix when the decider could."""
    if cause and fix:
        return (
            "- **The cause re-checked after the fix** — what you reported is "
            f'shown gone; the cause the fix addressed, "{cause}", needs to be '
            f'seen removed too: the output of a check showing what "{fix}" '
            "changed at it."
        )
    return (
        "- **The cause re-checked after the fix** — what you reported "
        "is shown gone; the cause the fix addressed needs to be seen "
        "removed too: the output of a check showing it (e.g. the setting "
        "now reads the new value, or the corrected process is the one "
        "running)."
    )


def readiness_message(
    reason: str,
    *,
    missing: Sequence[str] = (),
    gaps: Sequence[str] = (),
    request_text: Optional[str] = None,
    off_cause_turn: Optional[int] = None,
    recheck_cause: Optional[str] = None,
    recheck_fix: Optional[str] = None,
) -> str:
    """``ResolutionReadiness.message``, keyed by which branch answered."""
    if reason == ResolutionReason.FALSE_ALARM:
        return (
            "The evidence showed the reported problem was never present, so "
            "there is nothing to resolve. You can **close** the case instead; "
            "it will be recorded as a false alarm."
        )
    if reason == ResolutionReason.PENDING_CHECK:
        return (
            _NEEDS_INFO_INTRO
            + f"- **The check I asked for** — {request_text}"
            + "\n\n"
            + _SUBMIT_AS_DATA
        )
    if reason == ResolutionReason.CONFIRMED:
        return ""
    if reason == ResolutionReason.NOTHING_ON_RECORD:
        return (
            "This case has nothing investigated yet, so there's nothing to "
            "document as a resolution.\n\n"
            "If it's no longer relevant you can **close** it. If it was "
            "actually fixed, tell me what the problem was, what caused it, "
            "and how it was fixed, and submit the output of a check that "
            "shows it gone as data (pasted or attached, not typed into "
            "your message); I'll document the resolution."
        )
    if reason == ResolutionReason.FIX_OFF_CAUSE:
        return (
            f"The last fix (turn {off_cause_turn}) did not act on the root cause "
            "now identified, so no check after it can show that cause gone. "
            "To mark this **resolved** I need a **fix of the root cause** — "
            "tell me what you changed (or plan to change) there, and once "
            "it's applied, submit the output of checks showing the cause "
            "gone and the original problem gone with it, as data (pasted or "
            "attached, not typed into your message).\n\n"
            "If the problem is only quiet for now, or the root's fix is "
            "**deferred**, you can **close** the case instead — that keeps "
            "the investigation on record without claiming the cause was "
            "fixed."
        )
    if reason == ResolutionReason.FIX_FAILED:
        return (
            "The last fix didn't hold: the cause was shown removed, but the "
            "problem was seen again afterwards. To mark this **resolved** I "
            "need a **new fix** — tell me what you changed (or what you plan "
            "to change), and once it's applied, submit the output of checks "
            "showing the cause gone and the original problem gone with it, "
            "as data (pasted or attached, not typed into your message).\n\n"
            "If the problem is only quiet for now (a restart, a failover or "
            "another workaround), or the real fix is **deferred**, you can "
            "**close** the case instead — that keeps the investigation on "
            "record without claiming the fix worked."
        )
    asks = []
    if "root cause" in missing:
        asks.append("- **Root cause** — what caused the problem?")
    if "solution" in missing:
        asks.append("- **What fixed it** — the action that resolved it.")
    if ConfirmationGap.RELIEF_WITHOUT_CAUSE_RECHECK in gaps:
        asks.append(_cause_recheck_ask(recheck_cause, recheck_fix))
    elif ConfirmationGap.PROBLEM_UNRECHECKED in gaps:
        asks.append(
            "- **Confirmation the original problem is gone** — the cause is "
            "shown removed; what you first reported needs to be seen gone "
            "too: the output of the failing check or of the place the error "
            "appeared, now clean."
        )
    else:
        asks.append(
            "- **Confirmation it's resolved** — the output of a check "
            "showing the original problem is now gone (e.g. the latest "
            "output, with the error no longer in it)."
        )
    if ConfirmationGap.NO_SYMPTOM_ROW in gaps:
        asks.append(
            "- **The check that showed the problem** — the original failing "
            "output isn't on record as what showed the problem; submit it as "
            "data (pasted or attached) so the fix can be confirmed against it."
        )
    return (
        _NEEDS_INFO_INTRO
        + "\n".join(asks)
        + "\n\n"
        + _SUBMIT_AS_DATA
        + " If the problem was already fixed outside this "
        "session, the same applies, and I'll document it.\n\n"
        "If it was only **stabilized** (a workaround/failover while the root "
        "cause is still pending) or the real fix is **deferred** (a change "
        "window, hardware RMA, another team), you can **close** the case "
        "instead — that keeps the investigation and the documented (or "
        "deferred) solution on record without claiming it's permanently "
        "fixed."
    )


def suggest_resolve_message(
    root_cause: Optional[str], solution_titles: Sequence[str], record_lines: str
) -> str:
    """The closure→resolve pivot's prompt: the rows it rests on, cited (#1906
    D5.1), and only the root cause and fix the record holds."""
    detail = f"- **Root cause**: {root_cause}\n" if root_cause else ""
    if solution_titles:
        detail += "- **Solution**: " + ", ".join(solution_titles) + "\n"
    kept = "; the confirmed fix stays on record." if solution_titles else "."
    return (
        "This case qualifies for **resolved** status rather than closed. "
        f"Closing would close it without marking it resolved{kept}\n\n"
        "The record shows:\n\n"
        + record_lines
        + "\n"
        + detail
        + "\nWould you like to mark it **resolved** instead?"
    )


def close_summary_message(
    *,
    evidence_count: int,
    hypothesis_count: int,
    milestones_completed: int,
    root_cause: Optional[str],
    solution_titles: Sequence[str],
) -> str:
    """What the investigation accomplished, for a close's confirmation
    prompt; the TRIVIAL prompt when it accomplished nothing."""
    parts = []
    if evidence_count > 0:
        parts.append(
            f"- **Evidence collected**: {evidence_count} item{'s' if evidence_count != 1 else ''}"
        )
    if hypothesis_count > 0:
        parts.append(f"- **Hypotheses explored**: {hypothesis_count}")
    if milestones_completed > 0:
        parts.append(f"- **Milestones completed**: {milestones_completed}")
    if root_cause is not None:
        parts.append(f"- **Root cause identified**: {root_cause}")
    if solution_titles:
        parts.append(f"- **Solutions on record**: {', '.join(solution_titles)}")
    if not parts:
        return (
            "This case has minimal investigation data. "
            "Are you sure you want to close it?"
        )
    return (
        "Here's what was accomplished during this investigation:\n\n"
        + "\n".join(parts)
        + "\n\nAre you sure you want to close this case without resolution?"
    )
