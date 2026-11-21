"""The resolve confirmation's facts and the closed reply's status line,
rendered from the record (#2811 S2b-2b).

Whether a cause is identified, the resolution's record lines and the
closure's reason and mitigation line are the record's
(``cause.identification_leg``, ``resolution.statement``, ``closure``). The
texts the confirmation names besides are the case's own content, read through
one typed accessor, ``ResolutionContent`` (rulings of 2026-11-20 and
2026-11-21): the conclusion's root-cause text, the most recent solution's text
and whether the case holds evidence or commands. Pin 2 exempts it
permanently, with its field set asserted; it holds no outcome fact and is not
a census row. The case-level adapters stay in
``milestone_engine.terminal_replies``.
"""

from dataclasses import dataclass
from typing import Mapping, Optional

from faultmaven.modules.case.contracts import (
    CaseOutcome,
    CaseState,
    ClosureReason,
    closure_reason_table,
)

from .offers import mitigation_sentence

#: The root line of a confirmation with no cause-identification leg (#1906
#: D5.1): no conclusion is named as the root cause.
NO_ROOT_ON_RECORD = "no root cause on record"


@dataclass(frozen=True)
class ResolutionContent:
    """The case content the resolve confirmation names, never an outcome fact
    (ruling of 2026-11-21, S2b-2b (a)): the root-cause text
    (``_get_root_cause_summary``), the most recent solution's text
    (``_get_solution_summary``, ``solutions[-1]`` until S2c's confirming
    solution), and whether the case holds evidence, and commands or
    implementation steps on a solution."""

    root_cause: str
    solution: str
    has_evidence: bool
    has_commands: bool


def confirmation_params(outcome: CaseOutcome, content: ResolutionContent) -> dict:
    """The facts the resolve confirmation states (``offers.
    confirmation_message``), from the record: whether a cause is identified
    (``cause.identification_leg``) and the record lines
    (``resolution.statement``)."""
    return confirmation_params_of(
        outcome.cause.identification_leg, outcome.resolution.statement, content
    )


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
        ClosureReason.SOLUTION_DEFERRED: (
            "Case closed — cause identified and fix documented; implementation "
            "is deferred out-of-band."
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


def terminal_status_line(
    state: CaseState, reason: Optional[ClosureReason], line: Optional[str]
) -> str:
    """The status line after a transition is confirmed: "Case resolved.", or
    the closure reason's line (``_CLOSED_REPLIES``; a CLOSED case always
    carries a reason). What it says about a mitigation is the record's
    ``line`` (``closure.statement``, #2100): it replaces the
    ``mitigation_sufficient`` line and follows the reasons in
    ``_MITIGATION_LINE_FOLLOWS``."""
    if state == CaseState.RESOLVED:
        return "Case resolved."
    if line is None or not (
        reason == ClosureReason.MITIGATION_SUFFICIENT
        or reason in _MITIGATION_LINE_FOLLOWS
    ):
        return _CLOSED_REPLIES[reason]
    if reason == ClosureReason.MITIGATION_SUFFICIENT:
        return f"Case closed — {line}; root-cause analysis was deferred."
    return f"{_CLOSED_REPLIES[reason]} {mitigation_sentence(line)}"
