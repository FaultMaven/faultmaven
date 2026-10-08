"""The turn route's ``Idempotency-Key`` step: claim, look up, replay (#1888).

A turn submitted with an ``Idempotency-Key`` commits a ``TurnReceipt`` in its
one transaction. This module is the route's half: before anything of a keyed
turn runs, it decides whether the request is

- a **retry of a committed turn** — answered from the receipt, 200 with
  ``X-Idempotency-Replayed: true``: no LLM, no turn cap, no auto-title, no
  telemetry turn;
- a **key reused for a different turn** — 409 ``IDEMPOTENCY_KEY_REUSE``;
- a **duplicate of a turn still in flight** — 409 ``TURN_IN_PROGRESS`` with a
  ``Retry-After`` of the seconds left on the first one's claim;
- or **new** — it runs, holding the claim until it is fully answered.

ORDER, and why each step sits where it does (the route calls this right after
its case lookup and before the terminal-case gates and ``prepare_turn``):

1. **After the case lookup**, so a case the caller cannot see is the same 404
   it always was. The step itself discloses nothing: the receipt is keyed on
   the caller, so a principal can only ever read back its own, and a
   teammate's lookup on a shared case simply misses.
2. **Before the terminal-case gates.** A retried closing turn (a status
   transition, or a paste plus close) meets a case its own first attempt made
   terminal; it must replay, not hit "Cannot change status of a closed case".
3. **Before the turn cap's reserve** (inside ``prepare_turn``), so neither a
   replay nor an in-flight refusal charges anything.
4. **Claim FIRST, then look up.** Looked up first, a duplicate could miss the
   receipt (the first turn not yet committed), then win the claim the first
   released after committing, and run the turn a second time.
5. **Released only once the turn is fully answered**: after the settlement
   AND the auto-title, in the route's ``finally``; never once the response is
   built, which happens before the commit. A claim released before the commit
   lets a duplicate in to miss the receipt the same way.

The claim is an optimisation and a truthful answer, never the correctness
mechanism: optimistic concurrency (``cases.version``) and the receipt's unique
key still let exactly one turn commit. What the claim saves is the second LLM
run. Without it (no Redis, logged once; a claim store that failed; or a claim
its turn outlived) a duplicate whose lookup missed runs the turn, and then:

- if it loaded the case BEFORE the first turn committed, OCC refuses its save:
  409 ``CASE_VERSION_CONFLICT``, and the next retry replays;
- if it loaded the case AFTER, OCC passes and the receipt's unique key refuses
  it (``TurnReceiptExistsError``, nothing of it commits). The route answers
  with the committed turn (``replay_committed_turn``): it reads the receipt
  back and replays it, 200 with ``X-Idempotency-Replayed``.

Either way exactly one turn commits and the client is told the truth. The
second LLM run is this degraded mode's residual. Under FakeRedis the claim is
per process, which is exactly the standalone deployment: one process.

Also a residual (window 1, beside ``turn_settlement``'s): a CANCELLED handler
(client disconnect, shutdown) runs the route's ``finally`` and releases the
claim while the shielded settlement may still be committing. A duplicate in
that gap misses the receipt and runs; OCC or the unique key still lets only
one commit, as above.
"""

import hashlib
import json
import logging
import math
import secrets
from dataclasses import dataclass
from typing import Any, Optional, Sequence, Tuple

from fastapi import HTTPException, status
from pydantic import ValidationError

from faultmaven.config.idempotency_key import IDEMPOTENCY_KEY_REUSE
from faultmaven.core.investigation.turn_budget import TURN_COMMIT_RESERVE_SECONDS
from faultmaven.models.api_models import TurnResponse
from faultmaven.models.interfaces_case import ICaseService
from faultmaven.modules.case.api.title_generation import AUTO_TITLE_TIMEOUT_SECONDS
from faultmaven.modules.case.contracts import Case, TurnReceipt, TurnReceiptKey

logger = logging.getLogger(__name__)

#: ``x-error-code`` of the 409 for a duplicate of a keyed turn still running.
TURN_IN_PROGRESS = "TURN_IN_PROGRESS"

#: ``x-error-code`` of the 409 for a committed turn whose stored response no
#: longer validates as a ``TurnResponse`` (a deploy changed the schema between
#: the commit and the retry). Never a 500, and never a new turn: the turn DID
#: commit, so running it again is the one wrong answer.
IDEMPOTENCY_REPLAY_UNAVAILABLE = "IDEMPOTENCY_REPLAY_UNAVAILABLE"

#: Slack on the claim's lifetime beyond the turn's own bound (its ceiling, the
#: commit reserve and the auto-title). Past the TTL the claim expires on its
#: own, so a request that died without its ``finally`` cannot block its key
#: forever; a turn that outlives it can be duplicated, and OCC still lets only
#: one commit.
CLAIM_MARGIN_SECONDS = 30.0

#: Compare-and-delete: release the claim only if this request still holds it.
#: A plain DEL after the TTL expired would delete a LATER holder's claim and
#: let a third duplicate in.
_RELEASE_IF_HELD = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""

#: Logged once per process: an absent claim store is a deployment fact, not a
#: per-request event.
_REPORTED: set = set()


def _report_once(key: str, message: str, *args: Any) -> None:
    if key in _REPORTED:
        return
    _REPORTED.add(key)
    logger.warning(message, *args)


def request_fingerprint(
    *,
    query: Optional[str],
    pasted_content: Optional[str],
    intent_type: Optional[str],
    intent_data: Optional[str],
    input_type: Optional[str],
    source_url: Optional[str],
    observed_at: Optional[str],
    files: Sequence[Tuple[str, bytes]],
) -> str:
    """sha256 over a turn's semantic inputs, as the client sent them.

    The form fields verbatim, and each file by name, size and the sha256 of
    its CONTENT, never by ``UploadFile`` metadata alone: two different files
    of one name and size are two different turns. Canonical JSON (sorted
    keys), so the fingerprint is a function of the inputs and nothing else.
    """
    canonical = json.dumps(
        {
            "query": query,
            "pasted_content": pasted_content,
            "intent_type": intent_type,
            "intent_data": intent_data,
            "input_type": input_type,
            "source_url": source_url,
            "observed_at": observed_at,
            "files": [
                {
                    "name": name,
                    "size": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
                for name, content in files
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def claim_ttl_seconds(agent_timeout: float) -> int:
    """How long a claim may live: the turn's whole bound, plus slack.

    The same three numbers the route's own timing is built from, read from
    where they are defined: the turn's ceiling (``agent_timeout``, resolved by
    the route), the commit's reserve, and the auto-title's bound.
    """
    return math.ceil(
        agent_timeout
        + TURN_COMMIT_RESERVE_SECONDS
        + AUTO_TITLE_TIMEOUT_SECONDS
        + CLAIM_MARGIN_SECONDS
    )


def claim_name(case: Case, author_id: str, idempotency_key: str) -> str:
    """The claim's Redis key: the receipt's own key, prefixed."""
    return (
        f"turn-inflight:{case.enterprise_id}:{case.case_id}:"
        f"{author_id}:{idempotency_key}"
    )


@dataclass
class KeyedTurn:
    """A keyed turn's state between the route's idempotency step and its end."""

    receipt_key: TurnReceiptKey
    """What ``commit_turn`` writes the receipt from."""

    replay: Optional[TurnResponse] = None
    """The committed turn's response, when this request is its retry."""

    _redis: Any = None
    _claim: Optional[str] = None
    _token: Optional[str] = None

    async def release(self) -> None:
        """Give the claim up, if this request still holds it. Safe to call
        more than once, and never raises: a claim that fails to release
        expires at its TTL."""
        if self._claim is None:
            return
        claim, self._claim = self._claim, None
        try:
            await self._redis.eval(_RELEASE_IF_HELD, 1, claim, self._token)
        except Exception as exc:  # noqa: BLE001 - the TTL is the backstop
            logger.warning("Could not release turn claim %s: %r", claim, exc)


async def open_keyed_turn(
    *,
    redis: Any,
    case: Case,
    author_id: str,
    idempotency_key: str,
    fingerprint: str,
    case_service: ICaseService,
    agent_timeout: float,
    correlation_id: str,
) -> KeyedTurn:
    """Claim the key, then look its receipt up (the module docstring's order).

    Returns the ``KeyedTurn`` for a new turn (holding the claim) or for a
    replay (``replay`` set, claim already released). Raises the 409s.
    """
    keyed = KeyedTurn(
        receipt_key=TurnReceiptKey(
            author_id=author_id,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
        )
    )
    await _claim(keyed, redis, case, agent_timeout, correlation_id)
    try:
        receipt = await case_service.get_turn_receipt(
            enterprise_id=case.enterprise_id,
            case_id=case.case_id,
            author_id=author_id,
            idempotency_key=idempotency_key,
        )
    except BaseException:
        await keyed.release()
        raise
    if receipt is None:
        return keyed

    # The turn this key names already committed: nothing of this request will
    # run, so the claim goes now rather than at the route's end.
    await keyed.release()
    keyed.replay = _answer_from_receipt(receipt, fingerprint, case, correlation_id)
    return keyed


async def replay_committed_turn(
    *,
    keyed: KeyedTurn,
    case: Case,
    case_service: ICaseService,
    correlation_id: str,
) -> TurnResponse:
    """Answer a keyed turn whose commit the receipt's unique key refused.

    ``TurnReceiptExistsError``: another request under this key committed while
    this one ran without a claim (the module docstring's degraded mode).
    Nothing of this one committed, so the honest answer is the committed turn:
    read its receipt back and replay it, as a retry would be. A lookup that
    still misses (the receipt's case was deleted in between) is answered as
    in flight, with a short ``Retry-After``: the next retry decides.
    """
    receipt_key = keyed.receipt_key
    receipt = await case_service.get_turn_receipt(
        enterprise_id=case.enterprise_id,
        case_id=case.case_id,
        author_id=receipt_key.author_id,
        idempotency_key=receipt_key.idempotency_key,
    )
    if receipt is None:
        raise _in_progress(correlation_id, REFUSED_REPLAY_RETRY_AFTER_SECONDS)
    return _answer_from_receipt(
        receipt, receipt_key.request_fingerprint, case, correlation_id
    )


#: ``Retry-After`` when a refused commit's receipt cannot be read back: there is
#: no claim whose TTL to report, and the next retry decides.
REFUSED_REPLAY_RETRY_AFTER_SECONDS = 2


def _in_progress(correlation_id: str, retry_after: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=(
            "This turn is still being processed. Retry with the same "
            "Idempotency-Key after Retry-After seconds to receive its result."
        ),
        headers={
            "x-correlation-id": correlation_id,
            "x-error-code": TURN_IN_PROGRESS,
            "Retry-After": str(retry_after),
        },
    )


def _answer_from_receipt(
    receipt: TurnReceipt, fingerprint: str, case: Case, correlation_id: str
) -> TurnResponse:
    """The committed turn's response, or the 409 its receipt calls for."""
    if receipt.request_fingerprint != fingerprint:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This Idempotency-Key was already used for a different turn on "
                "this case. Use a new key for a new turn, or retry the original "
                "turn unchanged."
            ),
            headers={
                "x-correlation-id": correlation_id,
                "x-error-code": IDEMPOTENCY_KEY_REUSE,
            },
        )
    try:
        return TurnResponse.model_validate(receipt.response)
    except ValidationError as invalid:
        logger.error(
            "Turn receipt on case %s (turn %d) no longer validates as a "
            "TurnResponse: %s",
            case.case_id,
            receipt.turn_number,
            invalid,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This turn committed; reload the case.",
            headers={
                "x-correlation-id": correlation_id,
                "x-error-code": IDEMPOTENCY_REPLAY_UNAVAILABLE,
            },
        )


async def _claim(
    keyed: KeyedTurn,
    redis: Any,
    case: Case,
    agent_timeout: float,
    correlation_id: str,
) -> None:
    """Take the in-flight claim, OWNER-TOKENED, or refuse with 409.

    ``SET NX`` with a per-request random value, so only this request's release
    can delete it. No claim store, or one that fails, means no claim: the turn
    proceeds and OCC is its backstop.
    """
    if redis is None:
        _report_once(
            "no-redis",
            "No Redis client on app.state: keyed turns run without an in-flight "
            "claim, so a concurrent duplicate may spend a second LLM run "
            "(optimistic concurrency still lets one commit)",
        )
        return
    name = claim_name(
        case, keyed.receipt_key.author_id, keyed.receipt_key.idempotency_key
    )
    token = secrets.token_hex(16)
    try:
        acquired = await redis.set(
            name, token, nx=True, ex=claim_ttl_seconds(agent_timeout)
        )
    except Exception as exc:  # noqa: BLE001 - OCC is the backstop
        _report_once(
            "claim-failed",
            "Turn in-flight claim unavailable (%r): keyed turns run without it",
            exc,
        )
        return
    if acquired:
        keyed._redis, keyed._claim, keyed._token = redis, name, token
        return

    retry_after = 1
    try:
        remaining_ms = await redis.pttl(name)
        if remaining_ms and remaining_ms > 0:
            retry_after = max(1, math.ceil(remaining_ms / 1000))
    except Exception:  # noqa: BLE001 - 1 s is an honest lower bound
        pass
    raise _in_progress(correlation_id, retry_after)
