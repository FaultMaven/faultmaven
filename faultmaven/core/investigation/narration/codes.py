"""The decision log's codes (#2636, #2615 stage R).

A decider records what it decided as a code with its params
(``stage_gates.record_decision``), never as text. The narration step renders
the codes after the turn's record is written
(``milestone_engine/turn_narration.py``). Params carry
identities, the model's own emission, and what the engine did this turn;
every verdict a text states is read from the record at render time.

Both enums are APPEND-ONLY and pinned member for member by
``tests/unit/core/investigation/test_narration_golden_2636.py``.
"""

import hashlib
import json
from enum import StrEnum

#: Set by the test suite (``tests/conftest.py``): a decision token left
#: unrendered at the turn record raises there instead of being logged and
#: stripped (``turn_application.record_turn``).
TOKENS_FAIL = False

#: Where a turn's decisions accumulate, beside ``system_notices``.
DECISIONS_KEY = "outcome_decisions"


class NoticeCode(StrEnum):
    """A notice for the model's next turn."""

    # (R) rendered from the record.
    SOLUTION_OFFER_WITHDRAWN = "solution_offer_withdrawn"
    ABSENCE_IN_FAILED_WINDOW = "absence_in_failed_window"
    UNBOUND_RECHECKS = "unbound_rechecks"
    FAILED_FIX_NOT_REREGISTERED = "failed_fix_not_reregistered"
    MITIGATION_VERIFY_PROCEEDED = "mitigation_verify_proceeded"
    MITIGATION_VERIFY_REFUSED = "mitigation_verify_refused"
    MITIGATION_DISPOSITION_REFUSED = "mitigation_disposition_refused"
    PENDING_CHECK = "pending_check"
    DECLINED_RESOLUTION_REASK = "declined_resolution_reask"
    FIX_UNTARGETED = "fix_untargeted"
    SIGNALS_ON_INVALIDATED_PROBLEM = "signals_on_invalidated_problem"
    # (D) a decision on this response's emission.
    DECLINED_CLOSE_REASK = "declined_close_reask"
    ROOT_TERMINAL_NOT_QUALIFYING = "root_terminal_not_qualifying"
    ROOT_TERMINAL_REFUSED = "root_terminal_refused"
    REFUTATION_REFUSED = "refutation_refused"
    FALSE_ALARM_REFUSED = "false_alarm_refused"
    REVISION_REFUSED = "revision_refused"
    FIX_LICENCE_REFUSED = "fix_licence_refused"
    FIX_REPORT_IGNORED = "fix_report_ignored"
    # Context only: read by other renderers, never rendered itself.
    HYPOTHESIS_RETIRED = "hypothesis_retired"
    MITIGATION_DISPOSED = "mitigation_disposed"
    OFFER_LAPSED = "offer_lapsed"
    # (R) the engine's offer standing, read at the end of the cycle.
    ENGINE_OFFER_STANDS = "engine_offer_stands"


class OfferCode(StrEnum):
    """An engine offer's prose, or a request's: its ``summary`` and the
    metadata key the reply composer reads."""

    RCA_INFEASIBLE_CLOSE = "rca_infeasible_close"
    DEFERRED_RESOLVE = "deferred_resolve"
    DEFERRED_CLOSE = "deferred_close"
    BACKSTOP_RESOLVE = "backstop_resolve"
    FALSE_ALARM_CLOSE = "false_alarm_close"
    NEEDS_INFO_UNMET_CLOSE = "needs_info_unmet_close"
    RESOLUTION_NEEDS_INFO = "resolution_needs_info"
    # Summaries composed from the case at the narration step (stage S2 moves
    # them here): the step-0 request turned READY, and the model's proposal.
    REQUEST_READY = "request_ready"
    MODEL_PROPOSAL = "model_proposal"


#: Codes that write no notice: other renderers read them.
CONTEXT_ONLY = frozenset(
    {
        NoticeCode.HYPOTHESIS_RETIRED,
        NoticeCode.MITIGATION_DISPOSED,
        NoticeCode.OFFER_LAPSED,
    }
)

#: Deduplicated by code alone (A9): two recomputes in one turn withdraw into
#: one notice.
AGGREGATED = frozenset({NoticeCode.SOLUTION_OFFER_WITHDRAWN})

#: The metadata key each offer code's prose is published under.
PROSE_KEYS = {
    OfferCode.RCA_INFEASIBLE_CLOSE: "rca_infeasible_closure_message",
    OfferCode.DEFERRED_RESOLVE: "deferred_solution_gate_message",
    OfferCode.DEFERRED_CLOSE: "deferred_solution_gate_message",
    OfferCode.BACKSTOP_RESOLVE: "resolution_ready_gate_message",
    OfferCode.FALSE_ALARM_CLOSE: "false_alarm_closure_message",
    OfferCode.NEEDS_INFO_UNMET_CLOSE: "resolution_readiness_message",
    OfferCode.RESOLUTION_NEEDS_INFO: "resolution_needs_info_message",
}


def decision_key(code: str, params: dict) -> str:
    """What a decision is deduplicated by: its code, and its params unless the
    code is deduplicated by code alone."""
    if code in AGGREGATED:
        return str(code)
    return f"{code}:{json.dumps(params, sort_keys=True, default=str)}"


def decision_token(key: str) -> str:
    """The placeholder a notice decision holds its place with in
    ``system_notices`` until the narration step replaces it (A10). The same
    decision always yields the same token, so a merge deduplicates it."""
    digest = hashlib.sha1(key.encode(), usedforsecurity=False).hexdigest()[:16]
    return f"⁣notice:{digest}⁣"


def is_token(notice: str) -> bool:
    return notice.startswith("⁣notice:")
