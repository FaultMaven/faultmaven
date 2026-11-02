"""The INV-26 guard for resolver-minted intents: whether adopting a minted Gate 1 confirmation would let typed text that is not one bare consent token commit the problem statement a click would otherwise commit deterministically (#721, fm#918, #1794)."""

from faultmaven.core.investigation.milestone_engine.affordances import (
    gate1_statement_is_confirmable,
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
    """INV-26 guard for resolver-minted intents (#721, fm#918, #1794).

    True when adopting ``minted`` would commit Gate 1 (the problem-statement
    confirmation) on typed text that is not one bare consent token
    (``gate1_bare_consent``, #1794 ruling (a)). The resolver's classifier
    tier matches typed text against Gate 1's confirmation pair, and the
    engine's section 0c commits a confirmation on an INQUIRY case carrying a
    proposed statement. A click IS consent; an inference from typed text is
    not, so "correct — is the problem statement about the replica or the
    primary?" must not start the investigation off a statement the user is
    questioning. The engine's 0c screens a minted Gate 1 confirmation the same
    way; this guard also tells the out-of-band lane the message answered a
    gate (``gate_reply_refused``).

    Gate 1 is the only gate a mint can reach. Typed text never mints the chip
    of an offer on the record — the resolve, close and revision pairs, "Mark
    it resolved", the close cards (``intent_resolver._typed_text_may_answer``,
    #2222) — and the engine's 0b and 0b' read a minted intent as no answer
    anyway. A declining mint commits nothing (0c commits on an explicit True
    alone, #1464), so it is not guarded.
    """
    # The same conditions the engine's 0c branch checks before it commits,
    # read in the same order — a condition added there without one here would
    # make this guard silently miss the commit it exists to intercept. Both
    # statement arguments are the standing statement: this site runs before
    # any of this turn's updates are applied, so the shared predicate degrades
    # to "a statement stands, and it is not just whitespace".
    _standing_statement = getattr(
        getattr(case, "inquiry", None), "proposed_problem_statement", None
    )
    commits_gate_one = (
        minted.type == IntentType.CONFIRMATION
        and minted.confirmation_value is True
        and case.state == CaseState.INQUIRY
        and gate1_statement_is_confirmable(_standing_statement)
    )
    return commits_gate_one and not gate1_bare_consent(user_message)
