"""The resolve confirmation's facts and the closed reply's status line,
rendered from the record (#2811 S2b-2b).

Whether a cause is identified, the resolution's record lines and the
closure's reason and mitigation line are the record's
(``cause.identification_leg``, ``resolution.statement``, ``closure``), and so
are the attempts the closed reply and the confirming solution name
(``attempts``, #2811 S2c-1). The texts the confirmation names besides are the
case's own content, read through one typed accessor, ``ResolutionContent``
(rulings of 2026-11-20 and 2026-11-21): the conclusion's root-cause text, the
most recent solution's text and whether the case holds evidence or commands.
Pin 2 exempts it permanently, with its field set asserted; it holds no
outcome fact and is not a census row. The confirming attempt's description is
read by its id (``content_by_id``). The case-level adapters stay in
``milestone_engine.terminal_replies``.
"""

from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Sequence

from faultmaven.modules.case.contracts import (
    AttemptOutcome,
    CaseOutcome,
    CaseState,
    ClosureReason,
    FixWindowState,
    closure_reason_table,
)

from .fix_verification import Content
from .offers import mitigation_sentence

#: The root line of a confirmation with no cause-identification leg (#1906
#: D5.1): no conclusion is named as the root cause.
NO_ROOT_ON_RECORD = "no root cause on record"


@dataclass(frozen=True)
class ResolutionContent:
    """The case content the resolve confirmation names, never an outcome fact
    (ruling of 2026-11-21, S2b-2b (a)): the root-cause text
    (``_get_root_cause_summary``), the most recent solution's text
    (``_get_solution_summary``, ``solutions[-1]``; the confirmation names
    the confirming attempt instead when the record holds one), and whether
    the case holds evidence, and commands or implementation steps on a
    solution; and the root-cause conclusion's own text, unclipped, which the
    model's closure proposal names (``offers.record_params``, #3053 S3d-3).
    """

    root_cause: str
    solution: str
    has_evidence: bool
    has_commands: bool
    conclusion: Optional[str] = None


def confirmation_params(
    outcome: CaseOutcome,
    content: ResolutionContent,
    text: Callable[[str], Optional[Content]],
) -> dict:
    """The facts the resolve confirmation states (``offers.
    confirmation_message``), from the record: whether a cause is identified
    (``cause.identification_leg``), the record lines
    (``resolution.statement``) and, when the record holds a confirming
    attempt, that attempt as the solution (``confirming_solution``)."""
    params = confirmation_params_of(
        outcome.cause.identification_leg, outcome.resolution.statement, content
    )
    solution = confirming_solution(outcome.attempts, text)
    if solution is not None:
        params["solution"] = solution
    return params


def confirming_attempt(
    attempts: Sequence[AttemptOutcome],
) -> Optional[AttemptOutcome]:
    """The attempt whose window confirms: the observed window with the
    greatest ``fix_turn``, SUCCEEDED, and the one attempt at that turn. None
    with no dated observed window (window 0 has no attempt to name), when
    that window is not SUCCEEDED, or when several attempts share it."""
    dated = [
        a
        for a in attempts
        if a.window is not None and a.window.observed and a.window.fix_turn is not None
    ]
    if not dated:
        return None
    latest = max(a.window.fix_turn for a in dated)
    at = [a for a in dated if a.window.fix_turn == latest]
    if len(at) != 1 or at[0].window.state != FixWindowState.SUCCEEDED:
        return None
    return at[0]


def confirming_solution(
    attempts: Sequence[AttemptOutcome], text: Callable[[str], Optional[Content]]
) -> Optional[str]:
    """The confirming attempt (``confirming_attempt``) as the confirmation
    names it: its description, read by its id and clipped at 200 characters,
    and the turn it was carried out when the record holds it. None with no
    confirming attempt or no description."""
    attempt = confirming_attempt(attempts)
    content = text(attempt.action_id) if attempt is not None else None
    if content is None or not content.text:
        return None
    description = content.text
    if len(description) > 200:
        description = description[:200] + "..."
    if attempt.accepted_in_turn is None:
        return description
    return f"{description} (carried out turn {attempt.accepted_in_turn})"


def confirmation_params_of(
    leg: Optional[str], record_lines: str, content: ResolutionContent
) -> dict:
    """``confirmation_params`` from the two facts themselves: with no
    cause-identification leg, no conclusion is named as the root cause. It
    never asks how the fix was verified (#2428)."""
    return {
        "root": content.root_cause if leg is not None else NO_ROOT_ON_RECORD,
        "solution": content.solution,
        "record_lines": record_lines,
        # Enrichment that improves the docs but does not block resolution.
        "no_evidence": not content.has_evidence,
        "no_commands": not content.has_commands,
        # The identification decided on, for the narration's conflict count.
        "identification_leg": leg,
    }


#: The status line a confirmed close opens its reply with, one per reason, so
#: the user can tell at a glance what was preserved.
_CLOSED_REPLIES: Mapping[ClosureReason, str] = closure_reason_table(
    {
        ClosureReason.INQUIRY_ONLY: "Case closed without investigation.",
        ClosureReason.CLOSED_FALSE_ALARM: (
            "Case closed as a false alarm — the reported problem was not "
            "present. The evidence is preserved in the closure summary."
        ),
        ClosureReason.CLOSED_INSUFFICIENT_EVIDENCE: (
            "Case closed — insufficient evidence to ground a cause. "
            "Residual candidates and the missing data are preserved in the "
            "closure summary."
        ),
        ClosureReason.CLOSED_RESTATEMENT_HELD: (
            "Case closed — a cause was supported by the evidence but never "
            "stated distinctly from the problem. The candidates and what the "
            "cause still needs are preserved in the closure summary."
        ),
        ClosureReason.RESOLUTION_DECLINED: (
            "Case closed — the fix was confirmed to work; you chose not to "
            "mark it resolved."
        ),
        # The record-free fallback, and the line when a fix carried out was
        # seen failing: else the record says whether the fix was carried out
        # (``closed_line``).
        ClosureReason.SOLUTION_DEFERRED: (
            "Case closed — cause identified and a fix is on record."
        ),
        ClosureReason.CLOSED_RCA_INFEASIBLE: (
            "Case closed — the root cause is not reachable for this problem."
        ),
        # The record-free fallback: a mitigation_sufficient case states its
        # record instead (``terminal_status_line``).
        ClosureReason.MITIGATION_SUFFICIENT: (
            "Case closed — a mitigation was applied; root-cause analysis was "
            "deferred."
        ),
    }
)

#: The reasons whose reply is followed by the mitigation's record when the
#: case has one (#2100, C5). ``mitigation_sufficient`` states it in place of
#: its own line; the others omit it: no record can exist before the
#: investigation, a false alarm's problem was not present, and a declined
#: resolution's fix was confirmed.
_MITIGATION_LINE_FOLLOWS = frozenset(
    {
        ClosureReason.CLOSED_RCA_INFEASIBLE,
        ClosureReason.SOLUTION_DEFERRED,
        ClosureReason.CLOSED_RESTATEMENT_HELD,
        ClosureReason.CLOSED_INSUFFICIENT_EVIDENCE,
    }
)


def _turn_clause(attempts: Sequence[AttemptOutcome]) -> str:
    """ " (turn {t})" for the greatest turn the attempts were accepted in, or
    nothing when the record holds none."""
    turns = [a.accepted_in_turn for a in attempts if a.accepted_in_turn is not None]
    return f" (turn {max(turns)})" if turns else ""


def _reported_or(reported: Sequence[int], otherwise: str) -> str:
    """What the user reported carrying out that registered as no fix
    (INV-49), else ``otherwise``."""
    if not reported:
        return otherwise
    return (
        f"The user reported carrying out a proposed action (turn "
        f"{max(reported)}); it was not registered as a fix of the identified "
        "cause."
    )


def closed_line(
    reason: ClosureReason,
    leg: Optional[str],
    attempts: Sequence[AttemptOutcome],
    reported: Sequence[int] = (),
) -> str:
    """The closure reason's line, composed from the record where the reason
    alone says more than it holds (#2419, #2421). An accepted attempt has an
    ``action_id``; a carried-out fix is one whose kind is not an
    interception (an unkinded attempt reads as a remediation). A
    ``closed_insufficient_evidence`` close with an identification leg and no
    SUCCEEDED window says the cause was identified and what was carried out;
    ``solution_deferred`` says whether its fix was carried out, or only its
    first sentence when a carried-out fix's window FAILED: the record holds
    accepted attempts alone, so it cannot tell a failed fix from the deferred
    one. Every other shape is the reason's line (``_CLOSED_REPLIES``).

    ``reported``: the turns of changes the user reported carrying out that
    registered as no fix (``ProposedAction.reported_done_in_turn``, INV-49).
    Where the line would say nothing was carried out, it says that instead
    (#3039): the record holds the report, so "not carried out" is false."""
    accepted = [a for a in attempts if a.action_id is not None]
    fixes = [a for a in accepted if a.kind != "interception"]
    if reason == ClosureReason.SOLUTION_DEFERRED:
        if any(
            f.window is not None and f.window.state == FixWindowState.FAILED
            for f in fixes
        ):
            return _CLOSED_REPLIES[reason]
        done = (
            f"It was carried out{_turn_clause(fixes)}."
            if fixes
            else _reported_or(reported, "It was not carried out in the case.")
        )
        return f"{_CLOSED_REPLIES[reason]} {done}"
    if (
        reason == ClosureReason.CLOSED_INSUFFICIENT_EVIDENCE
        and leg is not None
        and not any(
            a.window is not None and a.window.state == FixWindowState.SUCCEEDED
            for a in attempts
        )
    ):
        if not accepted:
            done = _reported_or(reported, "No fix was carried out in the case.")
        elif not fixes:
            done = f"Only a workaround was carried out{_turn_clause(accepted)}."
        else:
            done = f"A fix was carried out{_turn_clause(fixes)}; it was not confirmed."
        return (
            "Case closed — a root cause was identified; no fix of it was "
            f"confirmed. {done}"
        )
    return _CLOSED_REPLIES[reason]


def terminal_status_line(
    state: CaseState,
    reason: Optional[ClosureReason],
    line: Optional[str],
    *,
    leg: Optional[str],
    attempts: Sequence[AttemptOutcome],
    reported: Sequence[int] = (),
) -> str:
    """The status line after a transition is confirmed: "Case resolved.", or
    the closure reason's line (``closed_line``, from the record's
    identification leg and attempts; a CLOSED case always carries a reason).
    What it says about a mitigation is the record's ``line``
    (``closure.statement``, #2100): it replaces the ``mitigation_sufficient``
    line and follows the reasons in ``_MITIGATION_LINE_FOLLOWS``."""
    if state == CaseState.RESOLVED:
        return "Case resolved."
    if line is None or not (
        reason == ClosureReason.MITIGATION_SUFFICIENT
        or reason in _MITIGATION_LINE_FOLLOWS
    ):
        return closed_line(reason, leg, attempts, reported)
    if reason == ClosureReason.MITIGATION_SUFFICIENT:
        return f"Case closed — {line}; root-cause analysis was deferred."
    return f"{closed_line(reason, leg, attempts, reported)} {mitigation_sentence(line)}"
