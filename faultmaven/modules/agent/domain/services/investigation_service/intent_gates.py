"""The INV-26 guard for resolver-minted intents: whether adopting a minted confirmation/status-transition intent would let inferred typed text commit a gate a DECIDE click would otherwise commit deterministically (#721, fm#918)."""

from faultmaven.core.investigation.milestone_engine.affordances import (
    gate1_statement_is_confirmable,
)
from faultmaven.core.investigation.milestone_engine.statement_revision import (
    revision_pending,
)
from faultmaven.core.investigation.milestone_engine.transition_consent import (
    gate1_bare_consent,
)
from faultmaven.models.api_models import (
    IntentType,
    QueryIntent,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
)


def _minted_intent_swallows_gate_consent(
    case: "Case", minted: QueryIntent, user_message: str
) -> bool:
    """INV-26 guard for resolver-minted intents (#721, widened by fm#918).

    True when adopting ``minted`` would let typed text that is not consent
    COMMIT A GATE: a SUBSTANTIVE message for the pending terminal transition,
    and, for Gate 1, any message that is not one bare consent token
    (``gate1_bare_consent``, #1794 ruling (a)). The IntentResolver's classifier tier semantically
    matches typed text against the previous turn's DECIDE suggestions and
    can mint ``confirmation``/``status_transition`` intents — but the
    engine treats those intents as deterministic consent (the DECIDE-click
    path) and consults them BEFORE its INV-26 bare-token guards. A click
    IS deterministic consent; an inference from typed text is not. So a
    minted intent that would commit a gate must pass the same substance
    test the typed-confirmation matcher applies (``is_substantive_reply``
    — shared single source of truth): "yes but what about the replication
    lag?" is substantive input, never consent.

    **Two gates, not one.** Until fm#918 this returned False whenever the
    case had no ``pending_transition``, justified as "mints with no
    pending transition (e.g. Gate 1 problem-statement confirmation) …
    adopt as before — none of them can execute a terminal transition".
    The premise holds; the conclusion did not follow. A minted
    ``confirmation`` with no pending transition reaches the engine's
    section 0c, which on an INQUIRY case carrying a proposed problem
    statement commits **Gate 1** (``problem_statement_confirmed``), and
    ``_check_automatic_transitions`` then
    fires INQUIRY → INVESTIGATING. Measured: "correct — is the problem
    statement about the replica or the primary?" started the
    investigation off a statement the user was in the middle of
    questioning. Gate 1 is reversible where RESOLVED is not, which is why
    it is a P1 and not a P0 — but INV-26 is a rule about what an
    INFERENCE may answer, not about which gate it lands on.

    What is deliberately NOT guarded stays unguarded, because neither
    commits anything: a **decline**, and a **contradicting** status
    transition. Both only cancel a standing proposal, and the message is
    processed as a normal turn either way.

    The Gate-1 arm reads ``confirmation_value`` as of #1464, and the
    decline it no longer guards is the change #1464's own note predicted.
    Until then the engine's 0c branch was value-blind, so a minted
    ``confirmation_value=False`` committed Gate 1 exactly as True did and
    guarding both arms was what "would commit a gate" MEANT. 0c now
    commits on an explicit True alone
    (``tests/unit/core/investigation/test_gate_one_decline_1464.py``
    drives that through the real path), so a declining mint commits
    nothing and the broad arm was over-broad by exactly the one case that
    used to justify it.

    Narrowed rather than left broad, for two reasons beyond the name
    being true again. The broad arm caught a decline only on the Gate-1
    SHAPE — INQUIRY, statement proposed — and nowhere else: a declining
    mint on an INVESTIGATING case with a pending transition was never
    guarded, so "an inferred no is not trusted" was never the rule this
    predicate held, only an accident of where the commit happened to be.
    And keeping it would now cost the user's own answer: an adopted
    decline reaches 0b/0c as a decline, where a rejected mint leaves the
    outcome to the typed-decline pattern matcher instead.

    One structural consequence, so nobody re-derives it as a hole: with
    the Gate-1 arm affirmative-only, any mint it matches while a
    ``pending_transition`` exists is matched by ``confirms_pending_-
    transition`` too (that arm accepts CONFIRMATION+True for any pending
    row). The fm#918 if/else hole therefore cannot reopen through a
    pending case — but the arm is still written independently of
    ``pending``, because 0c is reached with one or without one and the
    no-pending shape is the arm's own.
    """
    from faultmaven.core.investigation.terminal_transitions import (
        is_substantive_reply,
    )

    # OR, not if/else. The two gates are not alternatives — a case can
    # carry a pending transition AND be an INQUIRY case with a proposed
    # problem statement, and writing the Gate-1 arm as the ``else`` of
    # ``if pending`` made it unreachable exactly there. Measured on that
    # shape with a substantive DECLINE
    # ("no - but is the problem statement about the replica or the
    # primary?"): the pending arm only matches ``confirmation_value is
    # True``, so the mint was adopted, 0b cancelled the pending and fell
    # through, and 0c committed Gate 1 — the exposure the arm exists to
    # close, reached through the one door the if/else left open. A
    # ``needs_info`` pending is worse still: 0b is skipped wholesale
    # (``elif not case.pending_transition.get("needs_info")``) and the
    # mint lands in 0c directly.
    pending = getattr(case, "pending_transition", None)

    # The pending TERMINAL gate. Requires a pending row by definition.
    confirms_pending_transition = bool(pending) and (
        (minted.type == IntentType.CONFIRMATION and minted.confirmation_value is True)
        or (
            minted.type == IntentType.STATUS_TRANSITION
            and minted.to_state is not None
            and minted.to_state.value == pending.get("to_state")
        )
    )

    # Gate 1. The same conditions the engine's 0c branch checks before it
    # commits, read in the same order — a fourth condition added there
    # without one here would make this guard silently miss the commit it
    # exists to intercept. ``confirmation_value is True`` is 0c's newest
    # one (#1464): a declining mint commits nothing there, so it commits
    # nothing to guard here. Deliberately says NOTHING about ``pending``:
    # 0c is reached with one or without one.
    # The SAME predicate the engine's two consent sites use. Both arguments
    # are the standing statement because this site runs before any of this
    # turn's updates are applied — nothing can have revised it yet, so the
    # rule degrades to "a statement stands, and it is not just whitespace".
    # That is deliberately NOT a claim to detect the revise-and-confirm
    # shape here: no turn-start snapshot exists at this site to compare
    # against. What routing through the shared predicate buys is that the
    # three sites cannot come to disagree about what counts as a statement.
    _standing_statement = getattr(
        getattr(case, "inquiry", None), "proposed_problem_statement", None
    )
    commits_gate_one = (
        minted.type == IntentType.CONFIRMATION
        and minted.confirmation_value is True
        and case.state == CaseState.INQUIRY
        and gate1_statement_is_confirmable(_standing_statement)
    )

    # Each gate by its own screen. The terminal arm keeps the substance test:
    # the engine's pending gate already executes a typed consent only when it
    # is bare, and re-asks anything else it adopts. The Gate-1 arm applies the
    # bare test here (#1794, ruling (a)), and section 0c is its second reader:
    # it screens any minted Gate-1 confirmation that reaches it the same way.
    # So a mint on "ok, don't start yet" or "yes please" is dropped here, and
    # the text is processed as a normal turn, where the LLM's flag meets the
    # same test.
    # The statement-revision handshake: Gate 1 again, inside INVESTIGATING.
    # Engine section 0b' reads a minted confirmation with the disposition
    # gate's grammar, which commits only a bare consent token; the same screen
    # here drops a mint on anything else, so the text is processed as an
    # ordinary turn with the revision still standing.
    commits_revision = (
        minted.type == IntentType.CONFIRMATION
        and minted.confirmation_value is True
        and revision_pending(case)
    )

    return (
        (confirms_pending_transition and is_substantive_reply(user_message))
        or (commits_gate_one and not gate1_bare_consent(user_message))
        or (commits_revision and not gate1_bare_consent(user_message))
    )
