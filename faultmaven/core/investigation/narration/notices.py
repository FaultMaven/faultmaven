"""The notices and prompt lines a decider's outcome is stated in (#2636, #2615
stage R).

Composers keyed on what a decider decided, never on the case: the rule that
binds the model while a resolve decline stands and the actions it points at,
the feedback while the model's own check holds the resolution, and the
``solution_accepted`` refusals. The deciders derive the params; these compose.
"""

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


def license_notice(subject: str) -> str:
    """The M5 refusal: a permanent fix registers only on an established cause."""
    return (
        f"SYSTEM: 'solution_accepted' was not registered: {subject} targets a "
        "root cause that is no longer established, and a permanent fix needs "
        "one (M5). Re-ground the root cause with evidence first, or propose a "
        "temporary mitigation (WORKAROUND)."
    )
