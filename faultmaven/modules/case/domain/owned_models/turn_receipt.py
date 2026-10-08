"""The turn receipt: a committed keyed turn's identity and its answer (#1888).

A turn submitted with an ``Idempotency-Key`` commits a receipt in the turn's
ONE transaction (``ICaseRepository.save(case, reports=..., receipt=...)``,
#1882): the request's identity (author, key, a fingerprint of the turn's
inputs) and the ``TurnResponse`` the client was sent, as JSON. A retry with the
same key is answered from the receipt instead of running the turn again, and a
commit whose acknowledgement was lost can be recognised as committed by
reading it back.

Rows of ``turn_receipts``, a table the Case module owns. Lifecycle: deleted
with their case (``ON DELETE CASCADE``) and with their enterprise; no retention
job, because a receipt is one small row per keyed turn and lives exactly as
long as the messages it acknowledges.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict

from pydantic import BaseModel, ConfigDict, Field


@dataclass(frozen=True)
class TurnReceiptKey:
    """What identifies a keyed turn request, known before the turn runs.

    The route builds it from the request; ``InvestigationService.commit_turn``
    turns it into the ``TurnReceipt`` its commit writes, once the response it
    acknowledges exists.
    """

    author_id: str
    """The submitting user. In the key because a shared case has several
    principals and client keys are not UUID-grade: two users' turns must never
    answer each other's retries."""

    idempotency_key: str
    request_fingerprint: str
    """sha256 over the turn's semantic inputs, so a key reused for a different
    turn is refused (409 ``IDEMPOTENCY_KEY_REUSE``) rather than replayed."""


class TurnReceipt(BaseModel):
    """One committed keyed turn: unique per (enterprise, case, author, key)."""

    model_config = ConfigDict(frozen=True)

    case_id: str
    author_id: str
    idempotency_key: str
    request_fingerprint: str
    turn_number: int = Field(ge=0)
    """The message clock the turn committed at (``case.current_turn`` after
    it). What tells THIS turn's receipt from an older one under the same key
    when a commit's outcome is being probed."""

    response: Dict[str, Any]
    """``TurnResponse.model_dump(mode="json")``: exactly what the client was
    sent for this turn."""

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class TurnReceiptExistsError(Exception):
    """A turn tried to commit under a key that already has a receipt.

    The receipt's unique key ``(enterprise_id, case_id, author_id,
    idempotency_key)`` refused the INSERT, so this save committed NOTHING (the
    whole transaction rolled back) and ANOTHER turn under the same key already
    did. Raised by every ``ICaseRepository.save(..., receipt=...)``, unwrapped,
    as ``StaleCaseException`` is.

    Reached only when a duplicate ran without the route's in-flight claim (no
    Redis, a claim store that failed, or a claim outlived by its turn) and
    loaded the case after the first turn committed, so optimistic concurrency
    let it through. The route answers it with the committed turn: it reads the
    receipt back and replays it (#1888).
    """

    def __init__(self, case_id: str, idempotency_key: str) -> None:
        self.case_id = case_id
        self.idempotency_key = idempotency_key
        super().__init__(
            f"A turn under Idempotency-Key {idempotency_key!r} already committed "
            f"on case {case_id}"
        )
