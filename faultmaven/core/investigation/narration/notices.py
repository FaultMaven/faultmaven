"""The notices a decision cycle's codes render to (#2636, #2615 stage R).

Each renderer reads the code's params and the case's record
(``CaseOutcome``), never the case: a verdict a notice states is the record's,
read after the cycle's last decider ran. ``render_notice`` returns the notices
a code renders to, in order; none when the record no longer bears it.
"""

from typing import Any, Callable, Optional

from faultmaven.modules.case.contracts import (
    CaseOutcome,
    FixWindowRecord,
    MitigationRefusalReason,
    ProblemStatus,
    UnboundRecheck,
)

from .codes import NoticeCode

#: What binds the model while a resolve decline stands (#1895): the ONE rule the
#: step-2 refusal's feedback and the prompt line both state, whenever the
#: decline stands. The model never proposes on a request. A new solution lapses
#: the decline too (``offers.decline_stands``, ruling 3 of #2379), but it earns
#: READY only with a verification of its own, so the rule names the
#: verification alone.
RESOLVE_DECLINED_RULE = (
    "Do not propose resolved on the evidence already on record. If the user "
    "reports a NEW verification that the fix held, record it (the cause gone "
    "as causal_absence_evidence, the problem gone as symptom_absence_evidence) "
    "and propose resolved in the same turn. A request is not a verification: "
    "never record a confirmation row from a request."
)

#: Where the rule points a user who changes their mind or wants to close: the
#: engine's two chips (#2472). Stated only when the reply carries them
#: (``offer_projection.project_offers(case).reopen_chip``): a hold or an open
#: offer leaves both off.
RESOLVE_DECLINED_REOPEN_ACTION = (
    "A user who changes their mind without a new verification uses the "
    "engine's 'Mark it resolved' action: point them to it."
)
RESOLVE_DECLINED_CLOSE_ACTION = (
    "A user who wants to close the case instead uses the engine's 'Close "
    "without resolving' action: point them to it."
)
RESOLVE_DECLINED_ACTIONS = (
    f"{RESOLVE_DECLINED_REOPEN_ACTION} {RESOLVE_DECLINED_CLOSE_ACTION}"
)


def resolve_declined_actions(pending_check_holds: bool) -> str:
    """``RESOLVE_DECLINED_ACTIONS``, for a caller that already knows the chips
    are built: the close action alone while a check the model asked for after
    the fix holds the case (``pending_symptom_check``, #1906 D5.2), since "Mark
    it resolved" then answers with that check, not an offer."""
    if pending_check_holds:
        return RESOLVE_DECLINED_CLOSE_ACTION
    return RESOLVE_DECLINED_ACTIONS


#: The feedback when step 2 refuses a resolution the user declined (#1895),
#: the part that holds on every case: ``RESOLVE_DECLINED_RULE``.
DECLINED_RESOLVE_FEEDBACK = (
    "TRANSITION NOT PROPOSED: the user declined marking this case resolved, "
    "and no new confirmation that the fix held has been recorded since. "
    + RESOLVE_DECLINED_RULE
)

#: Added to it only when the reply carries the engine's two chips (#2472).
_DECLINED_RESOLVE_CHIPS_FEEDBACK = (
    "The engine has attached its 'Mark it resolved' and 'Close without "
    "resolving' actions to this reply."
)

#: How a refused re-proposal tells the model what binds it. Conditional, never
#: a flat ban: a model that obeys a ban never proposes, so a user who later
#: types "ok, close it" (on Slack, with no status menu) gets no refusal and no
#: card, and no way to close at all.
_DIRECTED_ONLY = (
    "Propose {what} only if the user directs it; the engine then attaches the "
    "Close action to your reply. Do not propose it unprompted."
)

#: What follows an answer showing the problem gone while the user's decline of
#: the resolution stands: it lapses only when the confirmation rows gain one
#: (``offers.decline_stands``), so a need marked FULFILLED on any other row
#: leaves it binding. Scoped to that answer: the clause promises nothing on an
#: answer showing the problem present (a problem-bound one fails the fix's
#: window, leaving nothing to re-open).
PENDING_CHECK_DECLINE_CLAUSE = (
    "The user declined the resolution. Once an answer showing the problem gone "
    "is on record, a new problem-bound row showing it lapses that decline; if "
    "that answer is on any other row, the decline still binds, and the user "
    "re-opens the resolution with 'Mark it resolved'."
)

NO_PENDING_SOLUTION_NOTICE = (
    "SYSTEM: 'solution_accepted' was not registered — no "
    "SOLUTION proposal is currently pending (it may have been "
    "downgraded, withdrawn or superseded since it was made). If "
    "the user has already carried out the fix and the root cause "
    "stands established, register it in ONE response: re-propose "
    "it as a SolutionToAdd and set solution_accepted with "
    "accepted_action_ref new_index_N naming it, justified by "
    "their report; do not ask them to accept it again. If the root "
    "cause is not established, re-ground it with evidence first. If "
    "the problem is already resolved, record the confirming "
    "evidence instead: causal_absence for the cause gone and "
    "symptom_absence for the reported problem gone."
)


def pending_check_feedback(
    need_id: str,
    request_text: str,
    *,
    fulfilled: bool,
    proposed: bool,
    declined_stands: bool,
) -> str:
    """What the model is told while its own check holds the resolution
    (#1906 D5.2), naming both ways out: the answer on record, or the need
    superseded. ``proposed``: its ``resolved`` was refused (ruling 3); else
    the backstop held the offer. A held FULFILLED need cites only rows from
    turns before the ask (``pending_symptom_check``), and the feedback says
    so. While a resolve decline stands, the answer does not by itself bring
    the resolution back, so the feedback promises no offer or proposal and
    says what does (``PENDING_CHECK_DECLINE_CLAUSE``)."""
    named = f"[{need_id}] {request_text}"
    if fulfilled:
        status = (
            f"the check you asked for ({named}) is marked FULFILLED, but the "
            "rows you cited predate the ask"
        )
    else:
        status = f"the check you asked for is still pending ({named})"
    lead = "RESOLVED NOT PROPOSED" if proposed else "RESOLUTION HELD"
    exits = (
        "mark the need FULFILLED citing it. If the user cannot run the check, "
        "SUPERSEDE the need with the reason."
    )
    if declined_stands:
        return (
            f"{lead}: {status}. Record its answer as a row and {exits} "
            f"{PENDING_CHECK_DECLINE_CLAUSE}"
        )
    # Scoped like the clause: it promises nothing on an answer showing the
    # problem still present (a problem-bound one fails the fix's window).
    then = (
        "Propose resolved once an answer showing the problem gone is on record"
        if proposed
        else "The resolution is offered once an answer showing the problem gone "
        "is on record"
    )
    return f"{lead}: {status}. {then}: record the row and {exits}"


def license_notice(subject: str) -> str:
    """The M5 refusal: a permanent fix registers only on an established cause."""
    return (
        f"SYSTEM: 'solution_accepted' was not registered: {subject} targets a "
        "root cause that is no longer established, and a permanent fix needs "
        "one (M5). Re-ground the root cause with evidence first, or propose a "
        "temporary mitigation (WORKAROUND)."
    )


# ---------------------------------------------------------------------------
# The renderers, one per code
# ---------------------------------------------------------------------------

Renderer = Callable[[dict, Optional[CaseOutcome], list], list[str]]
_RENDERERS: dict[str, Renderer] = {}


def _renders(code: NoticeCode):
    def register(fn: Renderer) -> Renderer:
        _RENDERERS[code] = fn
        return fn

    return register


def render_notice(
    code: str, params: dict, outcome: Optional[CaseOutcome], log: list
) -> list[str]:
    """The notices ``code`` renders to; ``log`` is the turn's decision log."""
    return _RENDERERS[code](params, outcome, log)


def has_renderer(code: str) -> bool:
    return code in _RENDERERS


@_renders(NoticeCode.SOLUTION_OFFER_WITHDRAWN)
def _withdrawn(params: dict, outcome: Optional[CaseOutcome], log: list) -> list:
    # #2036: the cause holds but its why is open, so the fix acts on a state
    # something re-creates.
    if outcome is not None and outcome.cause.identification_held_open:
        return [
            "SYSTEM: Your pending SOLUTION proposal was withdrawn: the cause "
            "it targets validated, but its why is open (ROOT-CAUSE TEST, "
            "clause b), so the fix would only intercept a state that comes "
            "back. Ask for what produced that state; then re-root onto that "
            "cause or mark the root terminal (root_is_terminal true, "
            "citing the causal_evidence row in "
            "terminal_basis_evidence_ids) and re-propose. To restore "
            "service now, propose it as a temporary mitigation "
            "(WORKAROUND)."
        ]
    return [
        "SYSTEM: Your pending SOLUTION proposal was withdrawn because "
        "the root cause it targeted is no longer established. Re-ground "
        "the root cause with evidence before re-proposing a permanent "
        "fix, or propose a temporary mitigation (WORKAROUND) instead."
    ]


def _window(outcome: Optional[CaseOutcome], fix_turn) -> Optional[FixWindowRecord]:
    for attempt in outcome.attempts if outcome is not None else []:
        if attempt.window is not None and attempt.window.fix_turn == fix_turn:
            return attempt.window
    return None


@_renders(NoticeCode.ABSENCE_IN_FAILED_WINDOW)
def _absence_in_failed(params: dict, outcome: Optional[CaseOutcome], log) -> list:
    window = _window(outcome, params["fix_turn"])
    if window is None or window.state != "failed":
        return []
    if window.disconfirmation is not None:
        shape = "the cause was shown removed, then the problem was seen again"
        cause = (
            " — re-assert the cause with a root_cause_conclusion on a new "
            "hypothesis if the same cause was addressed again — then"
        )
    else:
        shape = (
            f"the problem was seen gone at turn {window.relief_turn}, then seen "
            "again"
        )
        if window.fix_turn is None:
            shape += f" at turn {window.failure_turn}"
        cause = (
            " — a problem that went away after the fix and came back does not "
            "by itself disprove the cause the fix addressed: if that cause still "
            "stands, no new hypothesis is needed; if it no longer stands, "
            "re-assert it with a root_cause_conclusion on a new hypothesis — then"
        )
    # Window 0 (#2501): a fix done outside the case carries no execution turn.
    which = (
        "The fix done outside the case"
        if window.fix_turn is None
        else f"The fix executed at turn {window.fix_turn}"
    )
    return [
        f"SYSTEM: {which} did not hold: {shape}. Absence evidence recorded "
        "after that cannot confirm a resolution. If the user has applied (or "
        "will apply) a new fix, record it: propose it as a SolutionToAdd and, "
        "once the user says it ran, set solution_accepted with "
        "accepted_action_ref naming it (new_index_N, or its ACTION_ID once "
        f"proposed){cause} record the "
        "cause gone and the problem gone after it. If the problem is only "
        "quiet after a workaround, offer to close instead."
    ]


def unnarrated_rechecks(outcome: Optional[CaseOutcome]) -> list[UnboundRecheck]:
    """The unbound re-checks the record has not narrated, on a verified
    problem (#2555), whatever turn recorded them or their data."""
    if outcome is None or outcome.problem.status != ProblemStatus.VERIFIED:
        return []
    return [u for u in outcome.unbound_rechecks if u.narrated_turn is None]


def _named(found: list[UnboundRecheck]) -> tuple[list[str], list[str]]:
    """The ids the unbound notice names: five of each line, so a row it does
    not name stays unnarrated for a later turn."""
    unnamed = [u.evidence_id for u in found if not u.uncited][:5]
    uncited = [u.evidence_id for u in found if u.uncited][:5]
    return unnamed, uncited


def named_rechecks(outcome: Optional[CaseOutcome]) -> list[UnboundRecheck]:
    """The unnarrated re-checks the unbound notice names."""
    found = unnarrated_rechecks(outcome)
    unnamed, uncited = _named(found)
    return [u for u in found if u.evidence_id in {*unnamed, *uncited}]


@_renders(NoticeCode.UNBOUND_RECHECKS)
def _unbound(params: dict, outcome: Optional[CaseOutcome], log: list) -> list:
    """One line for the rows its window binds but the verdict does not cite
    (``uncited_recheck_why``), and one for the rows that name nothing that
    binds, with the ids that WOULD bind (``SubjectIndex.bindable_rows``)."""
    found = unnarrated_rechecks(outcome)
    if not found or outcome.bindable is None:
        return []
    bindable = outcome.bindable
    by_mitigation = {u.evidence_id for u in found if u.after_mitigation}
    unnamed, uncited = _named(found)
    problem_ids = ", ".join(bindable.problem_row_ids[:5])
    cause_ids = ", ".join(bindable.cause_row_ids[:5])
    why = bindable.why
    if cause_ids:
        cause_text = f"one of {cause_ids} (the cited basis of the root cause)."
    elif outcome.cause.confirmation_root_ids:
        cause_text = (
            f"{why}; re-send root_is_terminal true citing the causal_evidence "
            "rows that show why it arose (RE-ROOT if a deeper cause produced "
            "it), then re-check one of them."
        )
    else:
        cause_text = "none supports the root cause alone yet."
    lines = []
    if uncited:
        lines.append(
            f"SYSTEM: {'Post-fix row(s)' if params['fix_dated'] else 'Row(s)'} "
            f"{', '.join(uncited)} confirm nothing: "
            + (f"{why}." if cause_ids else cause_text)
        )
    if unnamed:
        after = "Post-mitigation" if by_mitigation.issuperset(unnamed) else "Post-fix"
        lines.append(
            f"SYSTEM: {after} row(s) {', '.join(unnamed)} name nothing they "
            "re-check that binds, so they confirm and disconfirm nothing. Set "
            "rechecks — symptom rows: "
            + (
                f"one of {problem_ids}"
                if problem_ids
                else "none is on record as having shown the problem; cite those "
                "rows in symptom_verification_evidence_ids"
            )
            + "; causal_absence rows: "
            + cause_text
        )
    return lines


@_renders(NoticeCode.FAILED_FIX_NOT_REREGISTERED)
def _failed_fix(params: dict, outcome, log) -> list:
    return [
        f"SYSTEM: 'solution_accepted' was not registered again: SOLUTION "
        f"{params['action_id']} is registered, and its fix window failed or a "
        "later fix followed it. If the user re-applied it, re-propose it as a "
        "SolutionToAdd and set accepted_action_ref new_index_N naming it."
    ]


_MITIGATION_PROCEEDED = (
    "SYSTEM: mitigation_verified was not recorded — the mitigation "
    "is recorded as proceed_unmitigated, which is written once."
)


@_renders(NoticeCode.MITIGATION_VERIFY_PROCEEDED)
def _mitigation_proceeded(params: dict, outcome, log) -> list:
    return [_MITIGATION_PROCEEDED]


@_renders(NoticeCode.MITIGATION_VERIFY_REFUSED)
def _mitigation_refused(params: dict, outcome: Optional[CaseOutcome], log) -> list:
    """Why ``mitigation_verified`` does not register, from the record's
    ``refusal_reason``. A disposition recorded as ``proceed_unmitigated`` later
    in the cycle (#2603) blocks every verify, so the line says that instead of
    asking for a re-check that can no longer count. The way to settle it is
    named only while the record carries no disposition."""
    mitigation = outcome.mitigation if outcome is not None else None
    disposition = mitigation.disposition if mitigation is not None else None
    if outcome is None:
        # No record: the disposition this cycle recorded is the log's.
        disposition = next(
            (
                e["params"]["value"]
                for e in log
                if e["code"] == NoticeCode.MITIGATION_DISPOSED
            ),
            None,
        )
    if disposition == "proceed_unmitigated":
        return [_MITIGATION_PROCEEDED]
    reason = (mitigation.refusal_reason if mitigation is not None else None) or (
        params["reason"]
    )
    head = "SYSTEM: mitigation_verified was not recorded — "
    declare = (
        "; or, if the user says it holds, set mitigation_disposition declared_stable"
    )
    if reason == MitigationRefusalReason.ATTEMPT_UNDATED:
        line = (
            head + "the mitigation's execution is not dated, so no re-check can "
            "verify it"
        )
        settle = (
            "; settle it with mitigation_disposition: declared_stable if the "
            "user says it holds, proceed_unmitigated to go on without one"
        )
    elif reason == MitigationRefusalReason.REVISION_PENDING:
        line = (
            head + "the revised problem statement awaits the user's "
            "confirmation: re-check the problem once it is confirmed"
        )
        settle = declare
    elif reason == MitigationRefusalReason.PROBLEM_UNVERIFIED:
        line = (
            head + "the problem is not verified: verify the problem first, "
            "citing symptom_verification_evidence_ids, then re-check it"
        )
        settle = declare
    else:
        present = mitigation.present_turn if mitigation is not None else None
        turn = mitigation.attempt_turn if mitigation is not None else None
        ids = ", ".join(params["problem_row_ids"][:5])
        seen = (
            f" (it was seen present at turn {present})" if present is not None else ""
        )
        when = f" (turn {turn})" if turn is not None else ""
        line = (
            head + "no re-check bound to the problem shows it relieved after the "
            f"mitigation{when}{seen}: record the post-mitigation "
            "symptom_absence_evidence with rechecks "
            + (
                f"naming one of {ids}"
                if ids
                else "naming a row that showed the problem (none is on record: "
                "cite them in symptom_verification_evidence_ids)"
            )
        )
        settle = declare
    return [line + (settle if disposition is None else "") + "."]


@_renders(NoticeCode.MITIGATION_DISPOSITION_REFUSED)
def _disposition_refused(params: dict, outcome: Optional[CaseOutcome], log) -> list:
    why = params["why"]
    if why == "not_accepted":
        refused = "no mitigation is accepted"
    elif why == "verified":
        refused = "a problem-bound re-check already showed the mitigation holding"
    else:
        mitigation = outcome.mitigation if outcome is not None else None
        carried = (mitigation.disposition if mitigation else None) or params["carried"]
        refused = f"the mitigation already carries {carried}, written once"
    return [
        f"SYSTEM: mitigation_disposition {params['value']} was not recorded "
        f"— {refused}."
    ]


@_renders(NoticeCode.PENDING_CHECK)
def _pending_check(params: dict, outcome: Optional[CaseOutcome], log) -> list:
    return [
        pending_check_feedback(
            params["need_id"],
            params["request_text"],
            fulfilled=params["fulfilled"],
            proposed=params["proposed"],
            declined_stands=(
                outcome.resolution.declined_stands
                if outcome is not None
                else params["declined_stands"]
            ),
        )
    ]


@_renders(NoticeCode.DECLINED_RESOLUTION_REASK)
def _declined_resolution(params: dict, outcome, log) -> list:
    if not params["chips"]:
        return [DECLINED_RESOLVE_FEEDBACK]
    return [
        f"{DECLINED_RESOLVE_FEEDBACK} {_DECLINED_RESOLVE_CHIPS_FEEDBACK} "
        f"{resolve_declined_actions(params['pending_check_holds'])}"
    ]


@_renders(NoticeCode.FIX_UNTARGETED)
def _untargeted(params: dict, outcome, log) -> list:
    return [
        f"SYSTEM: fix {params['action_id']} is recorded without a target: the "
        "root-cause conclusion names its cause in words only. Set "
        "names_root_node_id on root_cause_conclusion to the cause's "
        "cn_... node, and node_ref on each fix to the node it changes."
    ]


@_renders(NoticeCode.SIGNALS_ON_INVALIDATED_PROBLEM)
def _signals_on_invalidated(params: dict, outcome, log) -> list:
    return [
        f"{', '.join(params['signals'])} NOT ACCEPTED: the evidence "
        "showed the reported symptom was never present, so there is "
        "nothing to mitigate or fix. If the user disputes the "
        "finding, record it (invalidation_withdrawn)."
    ]


@_renders(NoticeCode.DECLINED_CLOSE_REASK)
def _declined_close(params: dict, outcome, log) -> list:
    if params["side"] == "false_alarm":
        return [
            "TRANSITION NOT PROPOSED: resolution is not eligible on a false "
            "alarm, and the user declined closing on this finding at turn "
            f"{params['declined_at']}. " + _DIRECTED_ONLY.format(what="a close")
        ]
    return [
        "TRANSITION NOT PROPOSED: the user declined closing this case with "
        "the solution documented (the deferred-implementation close), and "
        "no new solution or confirmation has been recorded since. "
        + _DIRECTED_ONLY.format(what="that close")
    ]


@_renders(NoticeCode.ROOT_TERMINAL_NOT_QUALIFYING)
def _root_not_qualifying(params: dict, outcome, log) -> list:
    return [
        f"Root {params['node_id']}: not qualifying: {'; '.join(params['refused'])} "
        "(the root's verdict stands on its cited rows)."
    ]


@_renders(NoticeCode.ROOT_TERMINAL_REFUSED)
def _root_refused(params: dict, outcome, log) -> list:
    refused = params["refused"]
    return [
        f"Root {params['node_id']} was NOT marked terminal: root_is_terminal true "
        "must cite, in terminal_basis_evidence_ids, a causal_evidence row "
        f"recorded since this root was posited (turn "
        f"{params['posited_turn']}) showing that nothing controllable "
        "produces its state (how it was issued or set, its lifetime, that "
        "nothing renews it). "
        + (f"Not qualifying: {'; '.join(refused)}. " if refused else "None cited. ")
        + "Its why stays open."
    ]


@_renders(NoticeCode.REFUTATION_REFUSED)
def _refutation_refused(params: dict, outcome, log) -> list:
    """What the link pass did, as decided. "It stays open" is not said of a
    hypothesis a later step of the cycle retired (A19)."""
    h_id = params["hypothesis_id"]
    hedged = (
        " Its REFUTES links are self-hedged (stance_confidence below 0.6), "
        "which is not a disproof."
        if params["hedged"]
        else ""
    )
    retired_later = any(
        e["code"] == NoticeCode.HYPOTHESIS_RETIRED
        and e["params"]["hypothesis_id"] == h_id
        for e in log
    )
    if params["retired"]:
        where = (
            " Its links lowered its likelihood enough to set it aside (RETIRED), "
            "which is not a disproof."
        )
    else:
        where = "" if retired_later else " It stays open."
    return [
        f"Hypothesis {h_id} was NOT refuted: nothing on record refutes it."
        f"{hedged} A refutation stands only on an evidence row from submitted "
        f"data linked with stance REFUTES to the hypothesis "
        f"(hypothesis_evidence_links) or to a node of its chain "
        f"(node_evidence_links), this turn or earlier, or on a recorded "
        f"failed fix of it.{where} Do not tell the user it is ruled out: "
        f"ask for the check that would settle it, or set state=RETIRED if "
        f"you are setting it aside without a disproof."
    ]


@_renders(NoticeCode.FALSE_ALARM_REFUSED)
def _false_alarm_refused(params: dict, outcome, log) -> list:
    return [f"FALSE-ALARM FINDING NOT ACCEPTED: {params['reason']}."]


@_renders(NoticeCode.REVISION_REFUSED)
def _revision_refused(params: dict, outcome, log) -> list:
    return [f"STATEMENT REVISION NOT ACCEPTED: {params['reason']}."]


@_renders(NoticeCode.FIX_LICENCE_REFUSED)
def _licence_refused(params: dict, outcome, log) -> list:
    subject = params["subject"]
    if params["variant"] == "withdrawn":
        return [
            f"SYSTEM: 'solution_accepted' was not registered: {subject} was "
            "withdrawn while its root cause was not established, and a withdrawn "
            "fix does not register by its ACTION_ID. If the user carried it out, "
            "re-propose it as a SolutionToAdd and set solution_accepted with "
            "accepted_action_ref new_index_N naming it."
        ]
    if params["variant"] == "downgraded":
        return [
            f"SYSTEM: 'solution_accepted' was not registered: {subject} was "
            "downgraded to a DIAGNOSTIC when it was proposed, because its root "
            "cause was not established then. The cause is established now: if the "
            "user carried out the fix, re-propose it as a SolutionToAdd and set "
            "solution_accepted with accepted_action_ref new_index_N naming it."
        ]
    return [license_notice(subject)]


def fix_report_reason(params: dict) -> str:
    """Why ``accepted_fix_reported_in`` was ignored, by its decider's code."""
    turn, previous = params.get("turn"), params.get("previous")
    dated_here = f"the fix is dated to turn {turn}"
    return {
        "unaccepted": lambda: "it is read only with solution_accepted",
        "carried": lambda: (
            "a fix carried through a revision hold is dated to its hold "
            f"turn, {turn}"
        ),
        "no_previous": lambda: f"there is no message before turn {turn}; {dated_here}",
        "window_not_failed": lambda: (
            f"the fix executed at turn {params['fix_turn']} was not seen failing "
            f"before turn {previous}, so turn {previous}'s observations stay "
            f"with it; {dated_here}"
        ),
        "decides_disconfirmation": lambda: (
            f"turn {previous}'s observations decide whether the fix executed at "
            f"turn {params['fix_turn']} was disconfirmed, so they stay with it; "
            f"{dated_here}"
        ),
        "previous_held": lambda: (
            f"turn {previous} was held for the revised statement confirmed now, "
            f"so its record lacks the cause work replayed since; {dated_here}"
        ),
    }[params["reason"]]()


@_renders(NoticeCode.FIX_REPORT_IGNORED)
def _fix_report_ignored(params: dict, outcome, log) -> list:
    return [
        "SYSTEM: accepted_fix_reported_in previous_message was ignored: "
        f"{fix_report_reason(params)}."
    ]
