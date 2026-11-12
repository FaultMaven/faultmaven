"""The notices and prompt lines a decider's outcome is stated in (#2636, #2615
stage R).

Composers keyed on what a decider decided, never on the case: the rule that
binds the model while a resolve decline stands and the actions it points at,
the feedback while the model's own check holds the resolution, and the
``solution_accepted`` refusals (no pending SOLUTION, the M5 licence). The
deciders derive the params; these compose.

Below them, the renderers of the decision log's notice codes (#2636 B1b): each
reads the code's params and the cycle's record (``CaseOutcome``), never the
case, after the cycle's last decider ran (``render_notice``). With no outcome
at all (``None``), a renderer falls back on the facts its decider recorded.
B1c's renderers read no record: their params carry the facts their deciders
read (decide-time, A4), so each renders the text its decider composed.
"""

from typing import Callable, Optional

from faultmaven.modules.case.contracts import CaseOutcome, MitigationRefusalReason

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
    so, with a decline or without. While a resolve decline stands
    (``declined_stands``), the answer does not by itself bring the resolution
    back, so the feedback promises no offer or proposal and says what does
    (``PENDING_CHECK_DECLINE_CLAUSE``). What it promises is scoped to an
    answer showing the problem gone."""
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


#: How a refused re-proposal tells the model what binds it. Conditional, never
#: a flat ban: a model that obeys a ban never proposes, so a user who later
#: types "ok, close it" (on Slack, with no status menu) gets no refusal and no
#: card, and no way to close at all.
_DIRECTED_ONLY = (
    "Propose {what} only if the user directs it; the engine then attaches the "
    "Close action to your reply. Do not propose it unprompted."
)

#: The feedback when step 2 refuses a resolution the user declined (#1895),
#: the part that holds on every case: ``RESOLVE_DECLINED_RULE``, the binding
#: half the prompt line states too.
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


def license_notice(subject: str) -> str:
    """The M5 refusal: a permanent fix registers only on an established cause."""
    return (
        f"SYSTEM: 'solution_accepted' was not registered: {subject} targets a "
        "root cause that is no longer established, and a permanent fix needs "
        "one (M5). Re-ground the root cause with evidence first, or propose a "
        "temporary mitigation (WORKAROUND)."
    )


# ---------------------------------------------------------------------------
# The renderers of the decision log's notice codes (#2636 B1b)
# ---------------------------------------------------------------------------

Renderer = Callable[[dict, Optional[CaseOutcome], list], list[str]]
_RENDERERS: dict[str, Renderer] = {}

#: The codes whose renderers read the cycle's record (B1b's). Every other
#: renderer reads only its params and the log, so a cycle whose log holds none
#: of these is narrated with no outcome computed (#2636).
READS_THE_RECORD = frozenset(
    {
        NoticeCode.SOLUTION_OFFER_WITHDRAWN,
        NoticeCode.MITIGATION_VERIFY_REFUSED,
        NoticeCode.MITIGATION_DISPOSITION_REFUSED,
    }
)


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


@_renders(NoticeCode.SOLUTION_OFFER_WITHDRAWN)
def _withdrawn(params: dict, outcome: Optional[CaseOutcome], log: list) -> list:
    """The variant is the record's; with no outcome, the one decided."""
    held_open = (
        outcome.cause.identification_held_open
        if outcome is not None
        else params["held_open"]
    )
    if held_open:
        # #2036: the cause holds but its why is open, so the fix acts on a
        # state something re-creates.
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


_MITIGATION_PROCEEDED = (
    "SYSTEM: mitigation_verified was not recorded — the mitigation "
    "is recorded as proceed_unmitigated, which is written once."
)


@_renders(NoticeCode.MITIGATION_VERIFY_PROCEEDED)
def _mitigation_proceeded(params: dict, outcome, log) -> list:
    return [_MITIGATION_PROCEEDED]


@_renders(NoticeCode.MITIGATION_VERIFY_REFUSED)
def _mitigation_refused(params: dict, outcome: Optional[CaseOutcome], log) -> list:
    """Why ``mitigation_verified`` did not register: the branch decided
    (``params["reason"]``, A4), with the disposition, the attempt turn and the
    turn the problem was seen present read from the record. A disposition
    recorded as ``proceed_unmitigated`` later in the cycle (#2603) blocks
    every verify, so the line says that instead of asking for a re-check that
    can no longer count. The way to settle it is named only while no
    disposition is recorded. With no outcome, the disposition is the log's,
    else the one stored before the cycle (``stored_disposition``), and the
    turns are the decider's."""
    if outcome is None:
        disposition = next(
            (
                e["params"]["value"]
                for e in log
                if e["code"] == NoticeCode.MITIGATION_DISPOSED
            ),
            params.get("stored_disposition"),
        )
        turn, present = params["attempt_turn"], params["present_turn"]
    else:
        mitigation = outcome.mitigation
        disposition = mitigation.disposition if mitigation is not None else None
        turn = mitigation.attempt_turn if mitigation is not None else None
        present = mitigation.present_turn if mitigation is not None else None
    if disposition == "proceed_unmitigated":
        return [_MITIGATION_PROCEEDED]
    reason = params["reason"]
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
        ids = ", ".join(params["problem_row_ids"][:5])
        when = f" (turn {turn})" if turn is not None else ""
        seen = (
            f" (it was seen present at turn {present})" if present is not None else ""
        )
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


@_renders(NoticeCode.REFUTATION_REFUSED)
def _refutation_refused(params: dict, outcome, log) -> list:
    """What the link pass did, as decided. "It stays open." is not said of a
    hypothesis the cycle ends with no longer open (``HYPOTHESIS_RETIRED``,
    A19)."""
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


# ---------------------------------------------------------------------------
# B1c: rendered from the facts their deciders read (decide-time, A4)
# ---------------------------------------------------------------------------


@_renders(NoticeCode.ABSENCE_IN_FAILED_WINDOW)
def _absence_in_failed(params: dict, outcome, log) -> list:
    """The FAILED window as ``latest_fix_window`` read it at the decision:
    whether it disconfirmed the cause, and its turns. The record's window can
    differ within the cycle (a FAILED window is carried whole), so it is not
    read here."""
    if params["disconfirmed"]:
        shape = "the cause was shown removed, then the problem was seen again"
        cause = (
            " — re-assert the cause with a root_cause_conclusion on a new "
            "hypothesis if the same cause was addressed again — then"
        )
    else:
        shape = (
            f"the problem was seen gone at turn {params['relief_turn']}, then "
            "seen again"
        )
        if params["fix_turn"] is None:
            shape += f" at turn {params['failure_turn']}"
        cause = (
            " — a problem that went away after the fix and came back does not "
            "by itself disprove the cause the fix addressed: if that cause still "
            "stands, no new hypothesis is needed; if it no longer stands, "
            "re-assert it with a root_cause_conclusion on a new hypothesis — then"
        )
    # Window 0 (#2501): a fix done outside the case carries no execution turn.
    which = (
        "The fix done outside the case"
        if params["fix_turn"] is None
        else f"The fix executed at turn {params['fix_turn']}"
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


@_renders(NoticeCode.UNBOUND_RECHECKS)
def _unbound(params: dict, outcome, log) -> list:
    """``main``'s row selection, until B2: the rows this turn recorded that
    bind to nothing, as ``_flag_unbound_post_fix_rows`` selected them, and the
    ids that WOULD bind (``SubjectIndex.bindable_rows``), its ``why`` and
    whether the confirmation pool has roots, as it read them. One line for
    the rows its window binds but the verdict does not cite, then one for the
    rows that name nothing that binds; five ids each."""
    uncited, unnamed = params["uncited"], params["unnamed"]
    problem_ids = ", ".join(params["problem_row_ids"][:5])
    cause_ids = ", ".join(params["cause_row_ids"][:5])
    why = params["why"]
    if cause_ids:
        cause_text = f"one of {cause_ids} (the cited basis of the root cause)."
    elif params["pool_roots"]:
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
            f"{', '.join(uncited[:5])} confirm nothing: "
            + (f"{why}." if cause_ids else cause_text)
        )
    if unnamed:
        by_mitigation = set(params["after_mitigation"])
        after = (
            "Post-mitigation" if by_mitigation.issuperset(unnamed[:5]) else "Post-fix"
        )
        lines.append(
            f"SYSTEM: {after} row(s) {', '.join(unnamed[:5])} name nothing they "
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


@_renders(NoticeCode.PENDING_CHECK)
def _pending_check(params: dict, outcome, log) -> list:
    """Whether a resolve decline stands is the decider's (A4)."""
    return [
        pending_check_feedback(
            params["need_id"],
            params["request_text"],
            fulfilled=params["fulfilled"],
            proposed=params["proposed"],
            declined_stands=params["declined_stands"],
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


@_renders(NoticeCode.FALSE_ALARM_REFUSED)
def _false_alarm_refused(params: dict, outcome, log) -> list:
    return [f"FALSE-ALARM FINDING NOT ACCEPTED: {params['reason']}."]


@_renders(NoticeCode.REVISION_REFUSED)
def _revision_refused(params: dict, outcome, log) -> list:
    return [f"STATEMENT REVISION NOT ACCEPTED: {params['reason']}."]


@_renders(NoticeCode.FIX_LICENCE_REFUSED)
def _licence_refused(params: dict, outcome, log) -> list:
    """A fix not registered for its licence: withdrawn ``license_lost`` once
    the license holds again (the name does not register it, a re-proposal
    does), its only bearer an M5-downgraded copy once it holds (the copy
    cannot register, a re-proposal can), or the license down."""
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
