"""The prose of the engine's offers and requests (#2636, #2615 stage R).

Composers keyed on what a decider decided, a reason and its params, never on
the case: the readiness message (``terminal_transitions``'
``assess_resolution_readiness``, through ``ResolutionReadiness.message``), the
closure→resolve pivot (``assess_closure_readiness``), the close summary
(``assess_close_summary``) and the resolve confirmation
(``terminal_replies._build_resolution_confirmation``). The deciders derive the
params; these compose. The resolve confirmation's record facts (whether a
cause is identified, the record lines) come from the record
(``narration.replies``), the texts it names from ``ResolutionContent``, read
at render.

``render_offer`` renders the prose of an offer (or a request) a decision cycle
opened, from its ``OfferCode`` and its params, after the cycle's record is
written (#2636 B1d). Every offer is in ``RECORD_PROSE``: the narration step
and the reply take the outcome facts its params state from the cycle's record
(``record_params``, #2811 S2b-2c, #3053 S3d-3), per decision, and the content
its prose names from ``ResolutionContent`` read at render. The decision's own
params stand (``narrated_params``) in the four cases ruled on 2026-11-26
(``RULED_CASES``), where the template cannot render the disagreeing record,
and with no readable record (Q5).

The "Root cause" ask carries why the cause cannot be confirmed yet
(``pool_empty_user_line``, #2811 S2c-1b) only where the record is read: on
the needs-info request (``RESOLUTION_NEEDS_INFO``) and on the reopen chip's
refusal (``transition_turns._chip_refusal``: the needs-info request's ask
from the fold the chip decides on, #3053 S3d-2), exactly when
``notices.empty_pool_text`` gives a line for the record's cause. The
decide-time message (``ResolutionReadiness.message``) keeps the ask without
it.
"""

from typing import TYPE_CHECKING, Callable, Optional, Sequence

from faultmaven.modules.case.contracts import (
    CaseOutcome,
    CauseOutcome,
    ClosureEligibility,
    ConfirmationGap,
    PoolEmptyReason,
    ResolutionReason,
)

from .codes import DECIDED_FROM, OfferCode
from .notices import empty_pool_text

if TYPE_CHECKING:
    from .fix_verification import Content
    from .replies import ResolutionContent


def mitigation_sentence(line: str) -> str:
    """``mitigation_closure_line``'s line standing as a sentence of its own."""
    return f"{line[:1].upper()}{line[1:]}."


#: The closure line's clauses for the user's declaration and the stall's
#: proceed (#2100), which the outcome statement states too, each followed by
#: ``at_turn`` of its disposition turn (#3053 S3d-4, ruling Q8).
MITIGATION_DECLARED = "you reported the mitigation stable"
MITIGATION_PROCEEDED = "the investigation went on before the mitigation was verified"


def at_turn(turn: Optional[int]) -> str:
    """`` (turn t)``, or nothing for no turn: never "(turn None)"."""
    return f" (turn {turn})" if turn is not None else ""


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


#: Why the cause cannot be confirmed yet, said to the user beside the "Root
#: cause" ask, by the record's ``pool_empty_reason`` (approved copy item 5,
#: #2811 S2c).
_POOL_EMPTY_USER_LINES = {
    PoolEmptyReason.HELD_OPEN: (
        "A cause is on record, but what produced it is still open."
    ),
    PoolEmptyReason.IDENTIFICATION_CONTESTED: (
        "More than one cause still fits the evidence, so none is identified yet."
    ),
    PoolEmptyReason.FALLEN_WITH_OTHER_STANDING: (
        "The cause identified earlier no longer stands, and another candidate "
        "is still open."
    ),
    PoolEmptyReason.FIX_TARGETS_OTHER_CAUSE: (
        "The cause identified earlier no longer stands, and the latest fix "
        "acted on a different cause."
    ),
    PoolEmptyReason.ROOTLESS_VALIDATED_ONLY: (
        "A supported explanation is on record, but it is not tied to a root "
        "cause yet."
    ),
    PoolEmptyReason.NO_CAUSE: (
        "No root cause is on record that a check can confirm yet."
    ),
}


def pool_empty_user_line(cause: CauseOutcome) -> Optional[str]:
    """The user's half of why the confirmation pool is empty (#2811 S2c-1b):
    the line for the record's ``pool_empty_reason``, whenever
    ``notices.empty_pool_text`` (the one guard) gives the model's half for
    the record's cause, else None."""
    if empty_pool_text(cause) is None:
        return None
    return _POOL_EMPTY_USER_LINES[cause.pool_empty_reason]


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
    cause: Optional[CauseOutcome] = None,
) -> str:
    """``ResolutionReadiness.message``, keyed by which branch answered. An
    unknown reason, or one without the param its text names, raises.
    ``cause``: the record's, passed only where the record is read
    (``record_params``, which the reopen chip's refusal renders through
    too); the "Root cause" ask then carries its ``pool_empty_user_line``
    when there is one. The decide-time message passes none, so its ask is
    the bare one."""
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
        line = pool_empty_user_line(cause) if cause is not None else None
        ask = "- **Root cause** — what caused the problem?"
        asks.append(ask if line is None else f"{ask} {line}")
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

_READINESS_PARAMS = (
    "request_text",
    "off_cause_turn",
    "recheck_cause",
    "recheck_fix",
    "cause",
)


def _readiness(params: dict) -> str:
    """The readiness message for the reason in ``params``: from
    ``ResolutionReadiness.offer_params``, the record's facts filled in by
    ``record_params`` unless the decision stands (``narrated_params``)."""
    return readiness_message(
        params["reason"],
        missing=params["missing"],
        gaps=params["gaps"],
        **{k: params.get(k) for k in _READINESS_PARAMS},
    )


#: The offers whose prose states the cycle's record (#2811 S2b-2c, ruling (a)
#: of 2026-11-22; #3053 S3d-3, ruling Q5 of 2026-11-25), by ``(code,
#: composer)``, the composer a model proposal's alone: every offer.
RECORD_PROSE = frozenset(
    {
        (OfferCode.RCA_INFEASIBLE_CLOSE, None),
        (OfferCode.DEFERRED_RESOLVE, None),
        (OfferCode.DEFERRED_CLOSE, None),
        (OfferCode.BACKSTOP_RESOLVE, None),
        (OfferCode.FALSE_ALARM_CLOSE, None),
        (OfferCode.NEEDS_INFO_UNMET_CLOSE, None),
        (OfferCode.RESOLUTION_NEEDS_INFO, None),
        (OfferCode.REQUEST_READY, None),
        (OfferCode.MODEL_PROPOSAL, "readiness"),
        (OfferCode.MODEL_PROPOSAL, "confirmation"),
        (OfferCode.MODEL_PROPOSAL, "closure"),
    }
)

#: The offers whose prose names the case's content (``ResolutionContent``),
#: read at render (#3053 S3d-3): the confirmation's root cause, solution and
#: hints, the closure proposal's root cause.
TAKES_CONTENT = frozenset(
    {
        (OfferCode.REQUEST_READY, None),
        (OfferCode.MODEL_PROPOSAL, "confirmation"),
        (OfferCode.MODEL_PROPOSAL, "closure"),
    }
)


def _asks_readiness(code: str, params: dict) -> bool:
    """Whether the offer's prose is a readiness ask (``_readiness``)."""
    if code == OfferCode.NEEDS_INFO_UNMET_CLOSE:
        return bool(params["suggest_close"])
    return code == OfferCode.RESOLUTION_NEEDS_INFO or (
        code == OfferCode.MODEL_PROPOSAL and params.get("composer") == "readiness"
    )


def ask_over_confirmed(
    code: str, params: dict, outcome: CaseOutcome, request_text: Optional[str]
) -> bool:
    """Ruled case 1 (2026-11-26): a readiness ask (the needs-info request, the
    unmet request's close on SUGGEST_CLOSE, the model proposal's) over a
    record whose reason is CONFIRMED, whose readiness message is empty."""
    return (
        _asks_readiness(code, params)
        and outcome.resolution.reason == ResolutionReason.CONFIRMED
    )


def check_without_text(
    code: str, params: dict, outcome: CaseOutcome, request_text: Optional[str]
) -> bool:
    """Ruled case 2 (2026-11-26): a readiness ask over a record whose reason
    is PENDING_CHECK, with no request text in the record or the decision
    (``readiness_message`` would raise)."""
    return (
        _asks_readiness(code, params)
        and outcome.resolution.reason == ResolutionReason.PENDING_CHECK
        and request_text is None
        and params.get("request_text") is None
    )


#: The resolve offers decided on the resolution's reason.
_RESOLVE_ON_REASON = frozenset(
    {
        (OfferCode.BACKSTOP_RESOLVE, None),
        (OfferCode.REQUEST_READY, None),
        (OfferCode.MODEL_PROPOSAL, "confirmation"),
    }
)


def resolve_unconfirmed(
    code: str, params: dict, outcome: CaseOutcome, request_text: Optional[str]
) -> bool:
    """Ruled case 3 (2026-11-26, by each offer's own fields): the backstop,
    the readiness request and the confirmation over a record whose reason is
    not CONFIRMED, whose ``resolution.statement`` is empty. The deferred
    resolve, decided on the closure eligibility, is ``verdict_flipped``'s."""
    return (code, params.get("composer")) in _RESOLVE_ON_REASON and (
        outcome.resolution.reason != ResolutionReason.CONFIRMED
    )


def verdict_flipped(
    code: str, params: dict, outcome: CaseOutcome, request_text: Optional[str]
) -> bool:
    """Ruled case 4 (2026-11-26), and case 3 for the deferred resolve: an offer
    decided on the closure eligibility whose record eligibility differs from
    its decision; its kind was fixed when it opened."""
    fields = DECIDED_FROM[(code, params.get("composer"))]
    return "closure.eligibility" in fields and (
        outcome.closure.eligibility != params["verdict"]
    )


#: The cases where an offer renders its decision's params over a record it
#: disagrees with, counted like every disagreement (owner, 2026-11-26).
RULED_CASES = (
    ask_over_confirmed,
    check_without_text,
    resolve_unconfirmed,
    verdict_flipped,
)


def _confirmation(
    params: dict,
    outcome: Optional[CaseOutcome],
    content: "ResolutionContent",
    text: "Callable[[str], Optional[Content]]",
) -> dict:
    """The confirmation's params: from ``outcome`` (``replies.
    confirmation_params``), else from the decision's leg and record lines,
    with the content read at render."""
    from .replies import confirmation_params, confirmation_params_of

    if outcome is None:
        stated = confirmation_params_of(
            params["identification_leg"], params["record_lines"], content
        )
        return {**params, **stated}
    stated = confirmation_params(outcome, content, text)
    return {**params, **stated, "reason": outcome.resolution.reason}


def record_params(
    code: str,
    params: dict,
    outcome: CaseOutcome,
    request_text: Optional[str],
    content: "Optional[ResolutionContent]" = None,
    text: "Optional[Callable[[str], Optional[Content]]]" = None,
) -> dict:
    """``params`` with the outcome facts the offer states taken from
    ``outcome``, and the content it names from ``content`` and ``text`` (the
    narrators' text accessor), both read at render. ``request_text``: the
    request text of the need the record holds the resolution for
    (``resolution.pending_check_need_id``), None when it holds none, the
    decision's then. Every other param (the rationale, the proposal id, the
    composer, the texts a readiness ask names, the closure summary's counts
    and titles) stays the decider's. ``DEFERRED_CLOSE`` states no fact the
    record holds. A readiness ask takes the record's cause too, whose
    pool-empty line the "Root cause" ask carries; only the needs-info request
    (and the reopen chip's refusal, which renders as it, #3053 S3d-2) reaches
    that ask, as the other two compose readiness on SUGGEST_CLOSE alone.
    Where the record's write failed, ``outcome`` is the narration's unstored
    one."""
    resolution = outcome.resolution
    composer = params.get("composer")
    if code == OfferCode.RCA_INFEASIBLE_CLOSE:
        return {**params, "closure_line": outcome.closure.statement}
    if code in (OfferCode.BACKSTOP_RESOLVE, OfferCode.DEFERRED_RESOLVE):
        return {
            **params,
            "record_lines": resolution.statement,
            "identification_leg": outcome.cause.identification_leg,
            **({"reason": resolution.reason} if "reason" in params else {}),
        }
    if code == OfferCode.FALSE_ALARM_CLOSE:
        return {**params, "problem_status": outcome.problem.status}
    if code == OfferCode.DEFERRED_CLOSE:
        return params
    if code == OfferCode.REQUEST_READY or composer == "confirmation":
        return _confirmation(params, outcome, content, text)
    if composer == "closure":
        leg = outcome.cause.identification_leg
        root = content.conclusion if leg is not None else None
        if params["verdict"] == ClosureEligibility.SUGGEST_RESOLVE:
            stated = {"root_cause": root, "record_lines": resolution.statement}
        else:
            stated = {
                "root_cause": None if leg is None else root or _IDENTIFIED_ROOT,
                "milestones_completed": len(outcome.milestones_shown),
            }
        return {**params, **stated, "identification_leg": leg}
    # The readiness asks: the unmet request's close, the needs-info request
    # and the model proposal's.
    return {
        **params,
        "reason": resolution.reason,
        "missing": list(resolution.missing),
        "gaps": list(resolution.confirmation_gaps),
        "request_text": (
            request_text if request_text is not None else params.get("request_text")
        ),
        "off_cause_turn": resolution.off_cause_turn,
        "cause": outcome.cause,
    }


#: The close summary's root line with a cause identified and no conclusion
#: text (``terminal_transitions.assess_close_summary``).
_IDENTIFIED_ROOT = "the identified root cause"


def narrated_params(
    code: str,
    params: dict,
    outcome: Optional[CaseOutcome],
    request_text: Optional[str],
    content: "Optional[ResolutionContent]" = None,
    text: "Optional[Callable[[str], Optional[Content]]]" = None,
) -> dict:
    """What the offer renders from (Q5, #3053 S3d-3): the record's facts
    (``record_params``), per decision; the decision's own params with no
    readable record (``outcome`` None) or in a ruled case (``RULED_CASES``),
    a disagreement the caller counts. The confirmation's decision names the
    content read at render beside its leg and record lines."""
    if outcome is not None and not any(
        ruled(code, params, outcome, request_text) for ruled in RULED_CASES
    ):
        return record_params(code, params, outcome, request_text, content, text)
    if code == OfferCode.REQUEST_READY or params.get("composer") == "confirmation":
        return _confirmation(params, None, content, text)
    return params


def render_offer(code: str, params: dict) -> str:
    """The prose of an offer or request a decision cycle opened, rendered after
    its record from ``params``: what ``narrated_params`` gives, the facts the
    record states or, in a ruled case or with no record, the facts its
    decider read (A4)."""
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
