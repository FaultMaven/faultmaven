"""The narration renderers reproduce today's text, byte for byte (#2636).

One fixture per code (amendment 15): the code's params and the record's fields
it reads, and the text ``origin/main``'s composer wrote for the same inputs at
the decision point (``58386f934``), transcribed from that composer. The
renderer must reproduce it exactly; the plan's expected differences are
fixtures of their own, named for their issue. The rendered paths are also
pinned end to end by the ~43 text-pinning tests that run through the deciders.

Pin 6: every ``NoticeCode`` and ``OfferCode`` member has a renderer and a
fixture, and the members are pinned (the enums are append-only).
"""

import pytest

from faultmaven.core.investigation.narration.codes import (
    CONTEXT_ONLY,
    NoticeCode,
    OfferCode,
)
from faultmaven.core.investigation.narration.notices import (
    DECLINED_RESOLVE_FEEDBACK,
    RESOLVE_DECLINED_ACTIONS,
    RESOLVE_DECLINED_CLOSE_ACTION,
    has_renderer,
    render_notice,
)
from faultmaven.core.investigation.narration.offers import render_offer
from faultmaven.modules.case.contracts import (
    AttemptOutcome,
    BindableRows,
    CaseOutcome,
    CaseState,
    CauseOutcome,
    ClosureEligibility,
    ClosureOutcome,
    ConfirmationGap,
    DisconfirmationPair,
    FixWindowRecord,
    FixWindowState,
    MitigationOutcome,
    MitigationRefusalReason,
    PoolEmptyReason,
    ProblemOutcome,
    ProblemStatus,
    ResolutionOutcome,
    ResolutionReason,
    ResolutionVerdict,
    UnboundRecheck,
)

pytestmark = pytest.mark.unit

_M5 = (
    "SYSTEM: 'solution_accepted' was not registered: {} targets a root cause "
    "that is no longer established, and a permanent fix needs one (M5). "
    "Re-ground the root cause with evidence first, or propose a temporary "
    "mitigation (WORKAROUND)."
)
_REFUTED_TAIL = (
    " A refutation stands only on an evidence row from submitted data linked "
    "with stance REFUTES to the hypothesis (hypothesis_evidence_links) or to a "
    "node of its chain (node_evidence_links), this turn or earlier, or on a "
    "recorded failed fix of it.{} Do not tell the user it is ruled out: ask "
    "for the check that would settle it, or set state=RETIRED if you are "
    "setting it aside without a disproof."
)
_ROWS = "Record its answer as a row and mark the need FULFILLED citing it. If "
_SUPERSEDE = "the user cannot run the check, SUPERSEDE the need with the reason."


def outcome(**update) -> CaseOutcome:
    """A record with the sections a fixture sets; the rest neutral."""
    base = CaseOutcome(
        folded_under=CaseState.INVESTIGATING,
        problem=ProblemOutcome(status=ProblemStatus.VERIFIED),
        cause=CauseOutcome(pool_empty_reason=PoolEmptyReason.NO_CAUSE),
        resolution=ResolutionOutcome(
            verdict=ResolutionVerdict.NEEDS_INFO,
            reason=ResolutionReason.DOCUMENTATION_GAPS,
        ),
        closure=ClosureOutcome(eligibility=ClosureEligibility.HAS_SUBSTANCE),
    )
    return base.model_copy(update=update)


def _failed_window(fix_turn, *, disconfirmed: bool) -> list:
    return [
        AttemptOutcome(
            action_id="act_000000000001" if fix_turn else None,
            window=FixWindowRecord(
                fix_turn=fix_turn,
                state=FixWindowState.FAILED,
                relief_turn=7,
                failure_turn=9,
                disconfirmation=(
                    DisconfirmationPair(cause_turn=8, problem_turn=9)
                    if disconfirmed
                    else None
                ),
            ),
        )
    ]


_HELD_OPEN = CauseOutcome(
    held_open_root_ids=["cn_000000000001"],
    pool_empty_reason=PoolEmptyReason.HELD_OPEN,
)
_UNBOUND = {
    "unbound_rechecks": [
        UnboundRecheck(evidence_id="ev_a", observed_turn=8, uncited=False),
        UnboundRecheck(evidence_id="ev_b", observed_turn=8, uncited=True),
    ],
    "bindable": BindableRows(
        problem_row_ids=["ev_p"], cause_row_ids=["ev_c"], why="why"
    ),
}
_MITIGATION = MitigationOutcome(
    attempt_turn=6,
    present_turn=8,
    refusal_reason=MitigationRefusalReason.NO_BOUND_RELIEF,
)

#: (id, code, params, record, expected notices).
NOTICES = [
    (
        "withdrawn-fallen",
        NoticeCode.SOLUTION_OFFER_WITHDRAWN,
        {"count": 1},
        outcome(),
        [
            "SYSTEM: Your pending SOLUTION proposal was withdrawn because the "
            "root cause it targeted is no longer established. Re-ground the root "
            "cause with evidence before re-proposing a permanent fix, or propose "
            "a temporary mitigation (WORKAROUND) instead."
        ],
    ),
    (
        "withdrawn-held-open",
        NoticeCode.SOLUTION_OFFER_WITHDRAWN,
        {"count": 2},
        outcome(cause=_HELD_OPEN),
        [
            "SYSTEM: Your pending SOLUTION proposal was withdrawn: the cause it "
            "targets validated, but its why is open (ROOT-CAUSE TEST, clause b), "
            "so the fix would only intercept a state that comes back. Ask for "
            "what produced that state; then re-root onto that cause or mark the "
            "root terminal (root_is_terminal true, citing the causal_evidence row "
            "in terminal_basis_evidence_ids) and re-propose. To restore service "
            "now, propose it as a temporary mitigation (WORKAROUND)."
        ],
    ),
    (
        "absence-disconfirmed",
        NoticeCode.ABSENCE_IN_FAILED_WINDOW,
        {"fix_turn": 6, "rows": ["ev_x"]},
        outcome(attempts=_failed_window(6, disconfirmed=True)),
        [
            "SYSTEM: The fix executed at turn 6 did not hold: the cause was shown "
            "removed, then the problem was seen again. Absence evidence recorded "
            "after that cannot confirm a resolution. If the user has applied (or "
            "will apply) a new fix, record it: propose it as a SolutionToAdd and, "
            "once the user says it ran, set solution_accepted with "
            "accepted_action_ref naming it (new_index_N, or its ACTION_ID once "
            "proposed) — re-assert the cause with a root_cause_conclusion on a "
            "new hypothesis if the same cause was addressed again — then record "
            "the cause gone and the problem gone after it. If the problem is only "
            "quiet after a workaround, offer to close instead."
        ],
    ),
    (
        "absence-window-zero-relapse",
        NoticeCode.ABSENCE_IN_FAILED_WINDOW,
        {"fix_turn": None, "rows": ["ev_x"]},
        outcome(attempts=_failed_window(None, disconfirmed=False)),
        [
            "SYSTEM: The fix done outside the case did not hold: the problem was "
            "seen gone at turn 7, then seen again at turn 9. Absence evidence "
            "recorded after that cannot confirm a resolution. If the user has "
            "applied (or will apply) a new fix, record it: propose it as a "
            "SolutionToAdd and, once the user says it ran, set solution_accepted "
            "with accepted_action_ref naming it (new_index_N, or its ACTION_ID "
            "once proposed) — a problem that went away after the fix and came "
            "back does not by itself disprove the cause the fix addressed: if "
            "that cause still stands, no new hypothesis is needed; if it no "
            "longer stands, re-assert it with a root_cause_conclusion on a new "
            "hypothesis — then record the cause gone and the problem gone after "
            "it. If the problem is only quiet after a workaround, offer to close "
            "instead."
        ],
    ),
    (
        "unbound-both-lines",
        NoticeCode.UNBOUND_RECHECKS,
        {"fix_dated": True},
        outcome(**_UNBOUND),
        [
            "SYSTEM: Post-fix row(s) ev_b confirm nothing: why.",
            "SYSTEM: Post-fix row(s) ev_a name nothing they re-check that binds, "
            "so they confirm and disconfirm nothing. Set rechecks — symptom "
            "rows: one of ev_p; causal_absence rows: one of ev_c (the cited "
            "basis of the root cause).",
        ],
    ),
    (
        "failed-fix",
        NoticeCode.FAILED_FIX_NOT_REREGISTERED,
        {"action_id": "act_1"},
        outcome(),
        [
            "SYSTEM: 'solution_accepted' was not registered again: SOLUTION "
            "act_1 is registered, and its fix window failed or a later fix "
            "followed it. If the user re-applied it, re-propose it as a "
            "SolutionToAdd and set accepted_action_ref new_index_N naming it."
        ],
    ),
    (
        "mitigation-proceeded",
        NoticeCode.MITIGATION_VERIFY_PROCEEDED,
        {},
        outcome(),
        [
            "SYSTEM: mitigation_verified was not recorded — the mitigation is "
            "recorded as proceed_unmitigated, which is written once."
        ],
    ),
    (
        "mitigation-unbound",
        NoticeCode.MITIGATION_VERIFY_REFUSED,
        {"reason": "no_bound_relief", "problem_row_ids": ["ev_p"]},
        outcome(mitigation=_MITIGATION),
        [
            "SYSTEM: mitigation_verified was not recorded — no re-check bound to "
            "the problem shows it relieved after the mitigation (turn 6) (it was "
            "seen present at turn 8): record the post-mitigation "
            "symptom_absence_evidence with rechecks naming one of ev_p; or, if "
            "the user says it holds, set mitigation_disposition declared_stable."
        ],
    ),
    (
        "mitigation-undated",
        NoticeCode.MITIGATION_VERIFY_REFUSED,
        {"reason": "attempt_undated", "problem_row_ids": []},
        outcome(
            mitigation=MitigationOutcome(
                refusal_reason=MitigationRefusalReason.ATTEMPT_UNDATED
            )
        ),
        [
            "SYSTEM: mitigation_verified was not recorded — the mitigation's "
            "execution is not dated, so no re-check can verify it; settle it "
            "with mitigation_disposition: declared_stable if the user says it "
            "holds, proceed_unmitigated to go on without one."
        ],
    ),
    (
        "disposition-carries",
        NoticeCode.MITIGATION_DISPOSITION_REFUSED,
        {"value": "proceed_unmitigated", "why": "carries", "carried": None},
        outcome(mitigation=MitigationOutcome(disposition="declared_stable")),
        [
            "SYSTEM: mitigation_disposition proceed_unmitigated was not recorded "
            "— the mitigation already carries declared_stable, written once."
        ],
    ),
    (
        "pending-check-declined",
        NoticeCode.PENDING_CHECK,
        {
            "need_id": "eneed_1",
            "request_text": "re-run the check",
            "fulfilled": False,
            "proposed": False,
            "declined_stands": False,
        },
        outcome(
            resolution=ResolutionOutcome(
                verdict=ResolutionVerdict.NEEDS_INFO,
                reason=ResolutionReason.PENDING_CHECK,
                declined_stands=True,
            )
        ),
        [
            "RESOLUTION HELD: the check you asked for is still pending ([eneed_1] "
            "re-run the check). " + _ROWS + _SUPERSEDE + " The user declined the "
            "resolution. Once an answer showing the problem gone is on record, a "
            "new problem-bound row showing it lapses that decline; if that answer "
            "is on any other row, the decline still binds, and the user re-opens "
            "the resolution with 'Mark it resolved'."
        ],
    ),
    (
        "pending-check-proposed",
        NoticeCode.PENDING_CHECK,
        {
            "need_id": "eneed_1",
            "request_text": "re-run the check",
            "fulfilled": True,
            "proposed": True,
            "declined_stands": False,
        },
        outcome(),
        [
            "RESOLVED NOT PROPOSED: the check you asked for ([eneed_1] re-run the "
            "check) is marked FULFILLED, but the rows you cited predate the ask. "
            "Propose resolved once an answer showing the problem gone is on "
            "record: record the row and mark the need FULFILLED citing it. If "
            + _SUPERSEDE
        ],
    ),
    (
        "declined-resolution-chips-held",
        NoticeCode.DECLINED_RESOLUTION_REASK,
        {"chips": True, "pending_check_holds": True},
        outcome(),
        [
            f"{DECLINED_RESOLVE_FEEDBACK} The engine has attached its 'Mark it "
            "resolved' and 'Close without resolving' actions to this reply. "
            + RESOLVE_DECLINED_CLOSE_ACTION
        ],
    ),
    (
        "untargeted",
        NoticeCode.FIX_UNTARGETED,
        {"action_id": "act_1"},
        outcome(),
        [
            "SYSTEM: fix act_1 is recorded without a target: the root-cause "
            "conclusion names its cause in words only. Set names_root_node_id on "
            "root_cause_conclusion to the cause's cn_... node, and node_ref on "
            "each fix to the node it changes."
        ],
    ),
    (
        "signals-invalidated",
        NoticeCode.SIGNALS_ON_INVALIDATED_PROBLEM,
        {"signals": ["mitigation_accepted", "solution_accepted"]},
        outcome(),
        [
            "mitigation_accepted, solution_accepted NOT ACCEPTED: the evidence "
            "showed the reported symptom was never present, so there is nothing "
            "to mitigate or fix. If the user disputes the finding, record it "
            "(invalidation_withdrawn)."
        ],
    ),
    (
        "declined-close-false-alarm",
        NoticeCode.DECLINED_CLOSE_REASK,
        {"side": "false_alarm", "declined_at": 4},
        outcome(),
        [
            "TRANSITION NOT PROPOSED: resolution is not eligible on a false "
            "alarm, and the user declined closing on this finding at turn 4. "
            "Propose a close only if the user directs it; the engine then "
            "attaches the Close action to your reply. Do not propose it "
            "unprompted."
        ],
    ),
    (
        "root-not-qualifying",
        NoticeCode.ROOT_TERMINAL_NOT_QUALIFYING,
        {"node_id": "cn_1", "refused": ["ev_1: not causal"]},
        outcome(),
        [
            "Root cn_1: not qualifying: ev_1: not causal (the root's verdict "
            "stands on its cited rows)."
        ],
    ),
    (
        "root-refused",
        NoticeCode.ROOT_TERMINAL_REFUSED,
        {"node_id": "cn_1", "posited_turn": 3, "refused": []},
        outcome(),
        [
            "Root cn_1 was NOT marked terminal: root_is_terminal true must cite, "
            "in terminal_basis_evidence_ids, a causal_evidence row recorded since "
            "this root was posited (turn 3) showing that nothing controllable "
            "produces its state (how it was issued or set, its lifetime, that "
            "nothing renews it). None cited. Its why stays open."
        ],
    ),
    (
        "refutation-open",
        NoticeCode.REFUTATION_REFUSED,
        {"hypothesis_id": "hyp_1", "hedged": True, "retired": False},
        outcome(),
        [
            "Hypothesis hyp_1 was NOT refuted: nothing on record refutes it. Its "
            "REFUTES links are self-hedged (stance_confidence below 0.6), which "
            "is not a disproof." + _REFUTED_TAIL.format(" It stays open.")
        ],
    ),
    (
        "false-alarm-refused",
        NoticeCode.FALSE_ALARM_REFUSED,
        {"reason": "the same response verified the symptom"},
        outcome(),
        [
            "FALSE-ALARM FINDING NOT ACCEPTED: the same response verified the "
            "symptom."
        ],
    ),
    (
        "revision-refused",
        NoticeCode.REVISION_REFUSED,
        {"reason": "the revised statement is empty"},
        outcome(),
        ["STATEMENT REVISION NOT ACCEPTED: the revised statement is empty."],
    ),
    (
        "licence-carried",
        NoticeCode.FIX_LICENCE_REFUSED,
        {
            "variant": "licence",
            "subject": "the fix the user reported running at turn 4",
        },
        outcome(),
        [_M5.format("the fix the user reported running at turn 4")],
    ),
    (
        "report-ignored-not-failed",
        NoticeCode.FIX_REPORT_IGNORED,
        {"reason": "window_not_failed", "fix_turn": 6, "previous": 9, "turn": 10},
        outcome(),
        [
            "SYSTEM: accepted_fix_reported_in previous_message was ignored: the "
            "fix executed at turn 6 was not seen failing before turn 9, so turn "
            "9's observations stay with it; the fix is dated to turn 10."
        ],
    ),
]

#: The plan's expected differences, each a fixture of its own.
DIFFERENCES = [
    (
        "2603-proceeded-asks-no-recheck",
        NoticeCode.MITIGATION_VERIFY_REFUSED,
        {"reason": "no_bound_relief", "problem_row_ids": ["ev_p"]},
        outcome(
            mitigation=_MITIGATION.model_copy(
                update={"disposition": "proceed_unmitigated"}
            )
        ),
        [
            "SYSTEM: mitigation_verified was not recorded — the mitigation is "
            "recorded as proceed_unmitigated, which is written once."
        ],
    ),
    (
        "2555-unbound-before-the-watermark-is-narrated",
        NoticeCode.UNBOUND_RECHECKS,
        {"fix_dated": True},
        outcome(**_UNBOUND, narration_since=9),
        [],
    ),
]


@pytest.mark.parametrize(
    "code,params,record,expected",
    [pytest.param(*f[1:], id=f[0]) for f in NOTICES + DIFFERENCES],
)
def test_each_notice_renders_mains_text(code, params, record, expected):
    assert render_notice(code, params, record, []) == expected


def test_a19_a_refutation_refused_then_retired_is_not_said_to_stay_open():
    """An expected difference (A19): a later step of the cycle retired the
    hypothesis the refusal named, so "It stays open" is not rendered."""
    retired = [
        {"code": NoticeCode.HYPOTHESIS_RETIRED, "params": {"hypothesis_id": "hyp_1"}}
    ]
    params = {"hypothesis_id": "hyp_1", "hedged": False, "retired": False}
    assert render_notice(NoticeCode.REFUTATION_REFUSED, params, outcome(), retired) == [
        "Hypothesis hyp_1 was NOT refuted: nothing on record refutes it."
        + _REFUTED_TAIL.format("")
    ]


_RECORD_LINES = "- **The cause** — c: row (turn 7)"

#: (id, code, params, record, expected).
OFFERS = [
    (
        "rca-infeasible",
        OfferCode.RCA_INFEASIBLE_CLOSE,
        {"rationale": "third-party API outage"},
        outcome(
            closure=ClosureOutcome(
                eligibility=ClosureEligibility.HAS_SUBSTANCE,
                statement="a mitigation was applied; its attempt is undated, so "
                "no re-check can be tied to it",
            )
        ),
        "A mitigation was applied; its attempt is undated, so no re-check can be "
        "tied to it. Since third-party API outage, shall we close this case?",
    ),
    (
        "deferred-resolve",
        OfferCode.DEFERRED_RESOLVE,
        {},
        outcome(
            resolution=ResolutionOutcome(
                verdict=ResolutionVerdict.READY,
                reason=ResolutionReason.CONFIRMED,
                statement=_RECORD_LINES,
            )
        ),
        "The record shows:\n\n" + _RECORD_LINES + "\n\nThe implementation work "
        "you flagged as out-of-band (a change request, maintenance window, or "
        "another team) is follow-up: it stays documented on the resolved case "
        "and does not need the incident held open. Shall I mark this case "
        "resolved?",
    ),
    (
        "deferred-close",
        OfferCode.DEFERRED_CLOSE,
        {},
        outcome(),
        "The root cause and fix are documented, but the fix can't be applied or "
        "verified during this session — it needs out-of-band implementation (a "
        "change request, maintenance window, or another team). Shall I close "
        "this case with the solution documented for your team to apply?",
    ),
    (
        "backstop",
        OfferCode.BACKSTOP_RESOLVE,
        {"cause_on_record": False},
        outcome(
            resolution=ResolutionOutcome(
                verdict=ResolutionVerdict.READY,
                reason=ResolutionReason.CONFIRMED,
                statement=_RECORD_LINES,
            )
        ),
        "The record shows:\n\n" + _RECORD_LINES + "\n\nThe case is still open "
        "until you confirm: marking it resolved writes up what happened. Shall I "
        "mark this case resolved?",
    ),
    (
        "false-alarm",
        OfferCode.FALSE_ALARM_CLOSE,
        {"rationale": "the 503s were a synthetic probe."},
        outcome(),
        "The evidence shows the reported problem was not present where and when "
        "it was reported: the 503s were a synthetic probe. There is nothing to "
        "fix, so this case cannot be resolved. Shall we **close** it as a false "
        "alarm? If you have information showing the problem is real, say so and "
        "the investigation continues.",
    ),
    (
        "unmet-request-close",
        OfferCode.NEEDS_INFO_UNMET_CLOSE,
        {"suggest_close": False, "reason": "documentation_gaps"},
        outcome(),
        "I understand. Without confirmation that the root cause was "
        "**eliminated** (e.g. the original error is now absent after the fix), "
        "I can't mark this as **resolved** — a restored-but-stabilized or "
        "deferred-fix case isn't a resolution.\n\nYou can **close** the case "
        "instead — this preserves the root cause analysis and the documented "
        "(or deferred) solution.",
    ),
    (
        "needs-info-gaps",
        OfferCode.RESOLUTION_NEEDS_INFO,
        {"reason": "documentation_gaps", "missing": [], "gaps": []},
        outcome(
            resolution=ResolutionOutcome(
                verdict=ResolutionVerdict.NEEDS_INFO,
                reason=ResolutionReason.DOCUMENTATION_GAPS,
                missing=["confirmation the problem is now resolved", "solution"],
                confirmation_gaps=[ConfirmationGap.PROBLEM_UNRECHECKED],
            )
        ),
        "To mark this **resolved** and write up the resolution summary, I just "
        "need a few essentials:\n\n- **What fixed it** — the action that "
        "resolved it.\n- **Confirmation the original problem is gone** — the "
        "cause is shown removed; what you first reported needs to be seen gone "
        "too: the output of the failing check or of the place the error "
        "appeared, now clean.\n\nSubmit check output as data (pasted or "
        "attached, not typed into your message): what you write is your "
        "account, and the output is what shows it. If the problem was already "
        "fixed outside this session, the same applies, and I'll document it."
        "\n\nIf it was only **stabilized** (a workaround/failover while the "
        "root cause is still pending) or the real fix is **deferred** (a change "
        "window, hardware RMA, another team), you can **close** the case "
        "instead — that keeps the investigation and the documented (or "
        "deferred) solution on record without claiming it's permanently fixed.",
    ),
]


@pytest.mark.parametrize(
    "code,params,record,expected", [pytest.param(*f[1:], id=f[0]) for f in OFFERS]
)
def test_each_offer_renders_mains_text(code, params, record, expected):
    assert render_offer(code, params, record) == expected


#: Composed from the case at the narration step until S2 moves them here.
_CASE_COMPOSED = {OfferCode.REQUEST_READY, OfferCode.MODEL_PROPOSAL}


def test_pin_6_every_code_has_a_renderer_and_a_fixture():
    rendered = {f[1] for f in NOTICES} | {f[1] for f in OFFERS}
    for code in NoticeCode:
        if code in CONTEXT_ONLY:
            continue
        assert has_renderer(code), code
        assert code in rendered, f"{code} has no golden fixture"
    assert set(OfferCode) - rendered == _CASE_COMPOSED


def test_the_codes_are_append_only():
    """A member renamed or removed would make the census and the fixtures
    lie; members are only added, here and in the census together."""
    assert [m.value for m in NoticeCode] == [
        "solution_offer_withdrawn",
        "absence_in_failed_window",
        "unbound_rechecks",
        "failed_fix_not_reregistered",
        "mitigation_verify_proceeded",
        "mitigation_verify_refused",
        "mitigation_disposition_refused",
        "pending_check",
        "declined_resolution_reask",
        "fix_untargeted",
        "signals_on_invalidated_problem",
        "declined_close_reask",
        "root_terminal_not_qualifying",
        "root_terminal_refused",
        "refutation_refused",
        "false_alarm_refused",
        "revision_refused",
        "fix_licence_refused",
        "fix_report_ignored",
        "hypothesis_retired",
    ]
    assert [m.value for m in OfferCode] == [
        "rca_infeasible_close",
        "deferred_resolve",
        "deferred_close",
        "backstop_resolve",
        "false_alarm_close",
        "needs_info_unmet_close",
        "resolution_needs_info",
        "request_ready",
        "model_proposal",
    ]


def test_the_declined_actions_name_both_chips_unless_a_check_holds():
    from faultmaven.core.investigation.narration.notices import (
        resolve_declined_actions,
    )

    assert resolve_declined_actions(False) == RESOLVE_DECLINED_ACTIONS
    assert resolve_declined_actions(True) == RESOLVE_DECLINED_CLOSE_ACTION
