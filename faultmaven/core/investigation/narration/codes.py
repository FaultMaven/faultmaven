"""The decision log's codes (#2636, #2615 stage R).

A decider records what it decided as a code with its params
(``stage_gates.record_decision``), never as text. The narration step renders
the codes after the decision cycle's record is written
(``milestone_engine/turn_narration.py``). Params carry identities, the model's
own emission, what the engine did this turn, and the facts a renderer falls
back on when no record can be read; every verdict a text states is read from
the record at render time.

The log is turn-local metadata and never stored (a turn record holds rendered
text), so member order has no runtime meaning. ``NoticeCode`` is APPEND-ONLY
from here, and pinned member for member by
``tests/unit/core/investigation/test_turn_reorder_2636.py``.
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

    # Rendered from the record.
    SOLUTION_OFFER_WITHDRAWN = "solution_offer_withdrawn"
    MITIGATION_VERIFY_PROCEEDED = "mitigation_verify_proceeded"
    MITIGATION_VERIFY_REFUSED = "mitigation_verify_refused"
    MITIGATION_DISPOSITION_REFUSED = "mitigation_disposition_refused"
    # A decision on this response's emission.
    REFUTATION_REFUSED = "refutation_refused"
    # Context only: read by other renderers, never rendered itself.
    # A hypothesis the cycle ends with no longer open (retired, refuted or
    # validated) after a refusal of its refutation.
    HYPOTHESIS_RETIRED = "hypothesis_retired"
    MITIGATION_DISPOSED = "mitigation_disposed"


#: Codes that write no notice: other renderers read them.
CONTEXT_ONLY = frozenset(
    {NoticeCode.HYPOTHESIS_RETIRED, NoticeCode.MITIGATION_DISPOSED}
)

#: Deduplicated by code alone (A9): two withdrawals in one turn render one
#: notice, the later decision's params replacing the earlier's.
AGGREGATED = frozenset({NoticeCode.SOLUTION_OFFER_WITHDRAWN})


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
    return f"\u2063notice:{digest}\u2063"


def is_token(notice: str) -> bool:
    return notice.startswith("\u2063notice:")
