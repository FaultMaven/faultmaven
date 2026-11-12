"""The prose of the engine's offers and requests (#2636, #2615 stage R).

Composers keyed on what a decider decided, a reason and its params, never on
the case: the readiness message (``terminal_transitions``'
``assess_resolution_readiness``, through ``ResolutionReadiness.message``), the
closure→resolve pivot (``assess_closure_readiness``), the close summary
(``assess_close_summary``) and the resolve confirmation
(``terminal_replies._build_resolution_confirmation``). The deciders derive the
params; these compose.

``render_offer`` renders the prose of an offer (or a request) a decision cycle
opened, from its ``OfferCode`` and the params its decider read (decide-time,
A4), after the cycle's record is written (#2636 B1d).
"""

from typing import Optional, Sequence

from faultmaven.modules.case.contracts import (
    ClosureEligibility,
    ConfirmationGap,
    ResolutionReason,
)

from .codes import OfferCode


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
    naming the root's statement and the fix when the decider named them. The
    choice rests on whether they were named, not on their text: a name can
    clip to nothing and is still named."""
    if cause is not None and fix is not None:
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


def _required(reason: str, **params) -> None:
    for name, value in params.items():
        if value is None:
            raise ValueError(f"readiness reason {reason!r} needs {name!r}")


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
    """``ResolutionReadiness.message``, keyed by which branch answered. An
    unknown reason, or one without the param its text names, raises."""
    if reason == ResolutionReason.FALSE_ALARM:
        return (
            "The evidence showed the reported problem was never present, so "
            "there is nothing to resolve. You can **close** the case instead; "
            "it will be recorded as a false alarm."
        )
    if reason == ResolutionReason.PENDING_CHECK:
        _required(reason, request_text=request_text)
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
        _required(reason, off_cause_turn=off_cause_turn)
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
    if reason != ResolutionReason.DOCUMENTATION_GAPS:
        raise ValueError(f"no readiness message for reason {reason!r}")
    asks = []
    if "root cause" in missing:
        asks.append("- **Root cause** — what caused the problem?")
    if "solution" in missing:
        asks.append("- **What fixed it** — the action that resolved it.")
    if "confirmation the problem is now resolved" in missing:
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
    trivial: bool,
    evidence_count: int,
    hypothesis_count: int,
    milestones_completed: int,
    root_cause: Optional[str],
    solution_titles: Sequence[str],
) -> str:
    """What the investigation accomplished, for a close's confirmation
    prompt; the TRIVIAL prompt when the decider found it accomplished
    nothing (``trivial``)."""
    if trivial:
        return (
            "This case has minimal investigation data. "
            "Are you sure you want to close it?"
        )
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
    return (
        "Here's what was accomplished during this investigation:\n\n"
        + "\n".join(parts)
        + "\n\nAre you sure you want to close this case without resolution?"
    )


#: ``close_summary_message``'s params, as ``ClosureReadiness.offer_params``
#: carries them.
_SUMMARY_PARAMS = (
    "trivial",
    "evidence_count",
    "hypothesis_count",
    "milestones_completed",
    "root_cause",
    "solution_titles",
)


def closure_message(params: dict) -> str:
    """``ClosureReadiness.message``, from its ``offer_params``: the pivot on
    SUGGEST_RESOLVE, else the close summary."""
    if params["verdict"] == ClosureEligibility.SUGGEST_RESOLVE:
        return suggest_resolve_message(
            params["root_cause"], params["solution_titles"], params["record_lines"]
        )
    return close_summary_message(**{k: params[k] for k in _SUMMARY_PARAMS})


def resolution_confirmation(
    *,
    root: str,
    solution: str,
    record_lines: str,
    no_evidence: bool,
    no_commands: bool,
) -> str:
    """The resolve confirmation (``terminal_replies``): what is on record, and
    the optional details that would improve the documentation (#2428)."""
    parts = [
        "Here's what I have on record:\n",
        f"- **Root cause**: {root}",
        f"- **Solution**: {solution}",
    ]
    if record_lines:
        parts.append(record_lines)
    hints = []
    if no_evidence:
        hints.append("diagnostic evidence (logs, metrics, error messages)")
    if no_commands:
        hints.append("specific commands or steps you used")
    if hints:
        parts.append(
            "\nThis is enough to resolve. If you'd like to improve the documentation "
            "(and any runbook generated from it), you can also share:"
        )
        parts.extend(f"- {hint}" for hint in hints)
        parts.append("\nConfirm to resolve now, or share more details first.")
    else:
        parts.append(
            "\nIs this correct? Once you confirm, I'll mark the case as resolved."
        )
    return "\n".join(parts)


_CONFIRMATION_PARAMS = (
    "root",
    "solution",
    "record_lines",
    "no_evidence",
    "no_commands",
)


def confirmation_message(params: dict) -> str:
    """The resolve confirmation, from ``terminal_replies.
    resolution_confirmation_params``."""
    return resolution_confirmation(**{k: params[k] for k in _CONFIRMATION_PARAMS})


#: The step-0 close when a remembered resolution request is still unmet.
_REQUEST_UNMET_CLOSE = (
    "I understand. Without confirmation that the root cause "
    "was **eliminated** (e.g. the original error is now "
    "absent after the fix), I can't mark this as "
    "**resolved** — a restored-but-stabilized or "
    "deferred-fix case isn't a resolution.\n\n"
    "You can **close** the case instead — this preserves "
    "the root cause analysis and the documented (or "
    "deferred) solution."
)

_DEFERRED_CLOSE = (
    "The root cause and fix are documented, but the fix can't be applied "
    "or verified during this session — it needs out-of-band implementation "
    "(a change request, maintenance window, or another team). Shall I close "
    "this case with the solution documented for your team to apply?"
)

_READINESS_PARAMS = ("request_text", "off_cause_turn", "recheck_cause", "recheck_fix")


def _readiness(params: dict) -> str:
    """The readiness message for the reason its decider decided, from
    ``ResolutionReadiness.offer_params``."""
    return readiness_message(
        params["reason"],
        missing=params["missing"],
        gaps=params["gaps"],
        **{k: params.get(k) for k in _READINESS_PARAMS},
    )


def render_offer(code: str, params: dict) -> str:
    """The prose of an offer or request a decision cycle opened, rendered after
    its record from the facts its decider read (A4): every text is the one the
    decider composed before B1d. None reads the record yet (stage S3 switches
    the deciders to it)."""
    if code == OfferCode.RCA_INFEASIBLE_CLOSE:
        # The mitigation's record (#2100, C1), never a verdict it does not hold.
        lead = mitigation_sentence(params["closure_line"] or "a mitigation was applied")
        return f"{lead} Since {params['rationale']}, shall we close this case?"
    if code == OfferCode.DEFERRED_RESOLVE:
        return (
            "The record shows:\n\n"
            + params["record_lines"]
            + "\n\nThe implementation work you flagged as out-of-band (a change "
            "request, maintenance window, or another team) is follow-up: it "
            "stays documented on the resolved case and does not need the "
            "incident held open. Shall I mark this case resolved?"
        )
    if code == OfferCode.DEFERRED_CLOSE:
        return _DEFERRED_CLOSE
    if code == OfferCode.BACKSTOP_RESOLVE:
        # READY is a confirmed ELIMINATION, not a confirmed CAUSE: the
        # attribution is named only with a cause on record (#1906 D5.1).
        if params["identification_leg"] is not None:
            still_open = (
                "The case is still open until you confirm: marking it resolved "
                "records the attribution and writes up the resolution summary."
            )
        else:
            still_open = (
                "The case is still open until you confirm: marking it resolved "
                "writes up what happened."
            )
        return (
            f"The record shows:\n\n{params['record_lines']}\n\n{still_open} "
            "Shall I mark this case resolved?"
        )
    if code == OfferCode.FALSE_ALARM_CLOSE:
        return (
            "The evidence shows the reported problem was not present where and "
            f"when it was reported: {params['rationale']} There is nothing to "
            "fix, so this case cannot be resolved. Shall we **close** it as a "
            "false alarm? If you have information showing the problem is real, "
            "say so and the investigation continues."
        )
    if code == OfferCode.NEEDS_INFO_UNMET_CLOSE:
        return _readiness(params) if params["suggest_close"] else _REQUEST_UNMET_CLOSE
    if code == OfferCode.RESOLUTION_NEEDS_INFO or (
        code == OfferCode.MODEL_PROPOSAL and params["composer"] == "readiness"
    ):
        return _readiness(params)
    if code == OfferCode.MODEL_PROPOSAL and params["composer"] == "closure":
        return closure_message(params)
    if code == OfferCode.REQUEST_READY or (
        code == OfferCode.MODEL_PROPOSAL and params["composer"] == "confirmation"
    ):
        return confirmation_message(params)
    raise KeyError(code)
