"""The KB upload and conversion routes' ``Idempotency-Key`` step (#2433).

``POST /knowledge/documents`` and ``POST /knowledge/convert`` mint their ids on
the server, so a retry of either used to publish again. A keyed request now
commits a receipt (``kb_request_receipts``) in the transaction that publishes
it (``modules/knowledge/domain/request_receipts.py``). This is the routes'
half, the counterpart of the turn's (``modules/case/api/turn_idempotency.py``).
Before anything of a keyed request runs, it decides whether the request is

- a **retry of a published request**: answered from the receipt with its
  status, its body and ``X-Idempotency-Replayed: true``; nothing publishes;
- a **key reused for a different request** (another file, or any other form
  field): 409 ``IDEMPOTENCY_KEY_REUSE``;
- a **duplicate of a request still running**: 409 ``KB_REQUEST_IN_PROGRESS``
  with ``Retry-After``;
- or **new**: it runs, holding the claim until it is fully answered.

The key is optional: an unkeyed request always runs. Both routes are declared
``never_replayed`` (``main.py``), so ``IdempotencyMiddleware`` never answers
them differently.

**Claim first, then look up.** Looked up first, a duplicate could miss the
receipt of a first request not yet committed, then win the claim that request
released after committing, and publish again. The claim is released in the
route's ``finally``, once the response is built and everything has committed.
It is a lease (``infrastructure/concurrency/owned_claim``): a conversion has no
bound of its own, so no fixed TTL could outlast it. The lease is renewed while
the request runs and frees the key within ``CLAIM_TTL_SECONDS`` of a process
death, so ``Retry-After`` is when the lease would lapse, and the first request
may still be running then.

The claim saves the second run; the receipt's key is what lets one publish.
Without a claim (no Redis, a failed claim store, a lapsed lease) a duplicate
whose lookup missed runs, and its receipt INSERT is refused in its own
transaction, so nothing of it publishes (``KbReceiptExistsError``). The route
answers it as a retry: the published request's receipt, read back.
"""

from dataclasses import dataclass
from typing import Annotated, Any, Awaitable, Callable, Dict, Optional, Sequence

from fastapi import Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

from faultmaven.config.idempotency_key import (
    IDEMPOTENCY_KEY_MAX_LENGTH,
    IDEMPOTENCY_KEY_MIN_LENGTH,
    IDEMPOTENCY_KEY_PATTERN,
    IDEMPOTENCY_KEY_REUSE,
    IDEMPOTENCY_REPLAYED_HEADER,
)
from faultmaven.infrastructure.concurrency.owned_claim import (
    ClaimHeld,
    OwnedClaim,
    renew_every,
    take_claim,
)
from faultmaven.modules.knowledge.domain.request_receipts import (
    KbReceiptKey,
    KbRequestReceipt,
)

#: ``x-error-code`` of the 409 for a duplicate of a keyed request still running.
KB_REQUEST_IN_PROGRESS = "KB_REQUEST_IN_PROGRESS"

#: The lease's TTL. Renewed every third of it while the request runs.
CLAIM_TTL_SECONDS = 60

#: ``Retry-After`` when a refused publication's receipt cannot be read back:
#: there is no claim whose TTL to report, and the next retry decides.
REFUSED_REPLAY_RETRY_AFTER_SECONDS = 2

#: The routes' ``Idempotency-Key`` header, in the grammar every layer reads, so
#: a key outside it is the published 422.
IdempotencyKey = Annotated[
    Optional[str],
    Header(
        alias="Idempotency-Key",
        min_length=IDEMPOTENCY_KEY_MIN_LENGTH,
        max_length=IDEMPOTENCY_KEY_MAX_LENGTH,
        pattern=IDEMPOTENCY_KEY_PATTERN,
        description=(
            "Optional. Identifies this submission across retries: a retry "
            "under the same key, with the same file and fields, returns the "
            "published result instead of publishing again. One key per "
            "submission; a new submission, or any change to its file or "
            "fields, takes a new key."
        ),
    ),
]


def keyed_responses(
    *, created: str, location: bool, conflicts: str = "", codes: Sequence[str] = ()
) -> Dict:
    """The route's documented 201 and 409 (``responses=``)."""
    headers: Dict[str, Any] = {
        IDEMPOTENCY_REPLAYED_HEADER: {
            "description": (
                "`true` when this response replays a request that already "
                "published under this `Idempotency-Key`; absent otherwise."
            ),
            "schema": {"type": "string", "enum": ["true"]},
        }
    }
    if location:
        headers["Location"] = {
            "description": "The published runbook, on the first answer and its replay.",
            "schema": {"type": "string"},
        }
    return {
        201: {
            "description": (
                f"{created} A retry under the same `Idempotency-Key` is "
                "answered with the same body, and carries "
                f"`{IDEMPOTENCY_REPLAYED_HEADER}: true`."
            ),
            "headers": headers,
        },
        409: {
            "description": (
                "Conflict. Told apart by `x-error-code`: "
                f"`{KB_REQUEST_IN_PROGRESS}` (a request with this "
                "`Idempotency-Key` is still running: retry with the same key "
                "after `Retry-After` seconds; it may still be running then); "
                f"`{IDEMPOTENCY_KEY_REUSE}` (the key was used for a different "
                f"file or different fields).{conflicts}"
            ),
            "headers": {
                "x-error-code": {
                    "description": "Which conflict.",
                    "schema": {
                        "type": "string",
                        "enum": [KB_REQUEST_IN_PROGRESS, IDEMPOTENCY_KEY_REUSE, *codes],
                    },
                },
                "Retry-After": {
                    "description": (
                        f"Seconds, on `{KB_REQUEST_IN_PROGRESS}` only: when the "
                        "running request's claim lapses unless it is renewed."
                    ),
                    "schema": {"type": "integer"},
                },
            },
        },
    }


def redis_of(request: Optional[Request]) -> Any:
    """The claim store on ``app.state``, or ``None``."""
    app = getattr(request, "app", None)
    return getattr(getattr(app, "state", None), "redis_client", None)


def claim_name(key: KbReceiptKey) -> str:
    """The claim's Redis key: the receipt's own key, prefixed."""
    return (
        f"kb-inflight:{key.enterprise_id}:{key.author_id}:{key.route_name}:"
        f"{key.idempotency_key}"
    )


Lookup = Callable[[KbReceiptKey], Awaitable[Optional[KbRequestReceipt]]]


@dataclass
class KeyedRequest:
    """A keyed request's state between the idempotency step and its end."""

    receipt_key: KbReceiptKey
    """What the publishing transaction writes the receipt from."""

    replay: Optional[KbRequestReceipt] = None
    """The published request's receipt, when this request is its retry."""

    _claim: Optional[OwnedClaim] = None

    async def release(self) -> None:
        """Give the claim up, if this request still holds it. Safe to call
        more than once, and never raises."""
        if self._claim is not None:
            claim, self._claim = self._claim, None
            await claim.release()


async def open_keyed_request(
    *, redis: Any, receipt_key: KbReceiptKey, lookup: Lookup
) -> KeyedRequest:
    """Claim the key, then look its receipt up (the module docstring's order).

    Returns the ``KeyedRequest`` for a new request (holding the claim) or for
    a replay (``replay`` set, claim already released). Raises the 409s.
    """
    keyed = KeyedRequest(receipt_key=receipt_key)
    try:
        keyed._claim = await take_claim(
            redis,
            claim_name(receipt_key),
            ttl_seconds=CLAIM_TTL_SECONDS,
            purpose="keyed KB uploads and conversions",
            backstop="the receipt's key still lets one publish",
            renew=renew_every(CLAIM_TTL_SECONDS / 3),
        )
    except ClaimHeld as held:
        raise _in_progress(held.retry_after) from None
    try:
        receipt = await lookup(receipt_key)
    except BaseException:
        await keyed.release()
        raise
    if receipt is not None:
        # Nothing of this request will run: the claim goes now.
        await keyed.release()
        keyed.replay = _answer(receipt, receipt_key)
    return keyed


async def replay_committed_request(
    *, keyed: KeyedRequest, lookup: Lookup
) -> KbRequestReceipt:
    """Answer a keyed request whose receipt INSERT was refused
    (``KbReceiptExistsError``): another request under the key published while
    this one ran without a claim, and nothing of this one did. A read-back that
    misses (the subject was deleted in between) is answered as in progress,
    with a short ``Retry-After``: the next retry decides."""
    receipt = await lookup(keyed.receipt_key)
    if receipt is None:
        raise _in_progress(REFUSED_REPLAY_RETRY_AFTER_SECONDS)
    return _answer(receipt, keyed.receipt_key)


def replayed(
    receipt: KbRequestReceipt, *, headers: Optional[Dict[str, str]] = None
) -> JSONResponse:
    """The published request's answer, as it was sent, marked a replay."""
    return JSONResponse(
        status_code=receipt.status_code,
        content=receipt.response,
        headers={IDEMPOTENCY_REPLAYED_HEADER: "true", **(headers or {})},
    )


def _in_progress(retry_after: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=(
            "A request with this Idempotency-Key is still being processed. "
            "Retry with the same key after Retry-After seconds to receive its "
            "result; it may still be running then."
        ),
        headers={
            "x-error-code": KB_REQUEST_IN_PROGRESS,
            "Retry-After": str(retry_after),
        },
    )


def _answer(receipt: KbRequestReceipt, key: KbReceiptKey) -> KbRequestReceipt:
    """The receipt, or the 409 a different request under its key calls for."""
    if receipt.request_fingerprint != key.request_fingerprint:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This Idempotency-Key was already used for a different "
                f"{key.route_name}. Use a new key for a new submission, or retry the "
                "original one unchanged."
            ),
            headers={
                "x-error-code": IDEMPOTENCY_KEY_REUSE,
            },
        )
    return receipt
