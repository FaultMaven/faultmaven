"""An owner-tokened in-flight claim in Redis, optionally held as a lease.

The claim a keyed request takes before it looks its receipt up, so that a
duplicate arriving while the first still runs is refused instead of running
again (the turn route, #1888; the KB upload and conversion routes, #2433).

- **Taken** with ``SET NX`` under a random per-request token and a TTL, so
  only this request's release can delete it, and a request that died without
  its ``finally`` cannot block the key past the TTL.
- **Refused** with :class:`ClaimHeld`, whose ``retry_after`` is the holder's
  remaining TTL (``PTTL``) in whole seconds, at least 1.
- **Released** by a compare-and-delete, so a release after the TTL ran out
  cannot delete a later holder's claim and let a third duplicate in.
- **Absent** when there is no Redis client or the claim store fails: the
  caller runs without a claim, and its own backstop (a receipt's unique key,
  optimistic concurrency) still lets one request commit. Reported once per
  process and purpose: an absent claim store is a deployment fact.

A **lease** (``renew``) keeps the claim for exactly as long as its holder runs,
for a request with no bound of its own: while the holder runs, a task re-arms
the TTL with a compare-and-``PEXPIRE``. It stops when the claim is released,
when the holder's task ends (cancelled, or dead without its ``finally``), or
when the token is no longer held. A claim without one keeps its fixed TTL.
"""

import asyncio
import functools
import logging
import math
import secrets
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

#: Compare-and-delete: release the claim only if this holder still has it.
RELEASE_IF_HELD = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""

#: Compare-and-``PEXPIRE``: re-arm the TTL only while this holder has the
#: claim. 0 when another holder has it or the key is gone; never creates one.
RENEW_IF_HELD = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""

_REPORTED: set = set()


def _report_once(key: str, message: str, *args: Any) -> None:
    if key in _REPORTED:
        return
    _REPORTED.add(key)
    logger.warning(message, *args)


def renew_every(seconds: float) -> Callable[[], Awaitable[None]]:
    """A lease's interval: the wait between two renewals."""
    return functools.partial(asyncio.sleep, seconds)


class ClaimHeld(Exception):
    """Another request holds the claim."""

    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after
        super().__init__(f"claim held; retry after {retry_after} s")


class OwnedClaim:
    """A claim this request holds. Released only by its own token."""

    def __init__(self, redis: Any, name: str, token: str) -> None:
        self._redis, self.name, self._token = redis, name, token
        self._released = False
        self.renewal: "Optional[asyncio.Task[None]]" = None

    def _keep(self, wait: Callable[[], Awaitable[Any]], ttl_ms: int) -> None:
        holder = asyncio.current_task()

        async def keep() -> None:
            while True:
                await wait()
                if holder is not None and holder.done():
                    return
                try:
                    held = await self._redis.eval(
                        RENEW_IF_HELD, 1, self.name, self._token, ttl_ms
                    )
                except Exception as exc:  # noqa: BLE001 - retried next interval
                    logger.warning("Could not renew claim %s: %r", self.name, exc)
                    continue
                if not held:
                    return

        self.renewal = asyncio.ensure_future(keep())

    async def release(self) -> None:
        """Stop renewing, and give the claim up if this holder still has it.
        Safe to call more than once, and never raises: a claim that fails to
        release expires at its TTL."""
        if self._released:
            return
        self._released = True
        if self.renewal is not None:
            self.renewal.cancel()
        try:
            await self._redis.eval(RELEASE_IF_HELD, 1, self.name, self._token)
        except Exception as exc:  # noqa: BLE001 - the TTL is the backstop
            logger.warning("Could not release claim %s: %r", self.name, exc)


async def take_claim(
    redis: Any,
    name: str,
    *,
    ttl_seconds: int,
    purpose: str,
    backstop: str,
    renew: Optional[Callable[[], Awaitable[Any]]] = None,
) -> Optional[OwnedClaim]:
    """Take the claim ``name`` for ``ttl_seconds``, or raise :class:`ClaimHeld`.

    ``None`` when there is no claim store (``redis`` is None, or it failed):
    the caller runs without a claim. ``purpose`` names the callers in that
    report (``"keyed turns"``) and ``backstop`` what still holds without it.
    ``renew`` makes the claim a lease, renewed after each wait it returns.
    """
    if redis is None:
        _report_once(
            f"{purpose}:no-redis",
            "No Redis client on app.state: %s run without an in-flight claim, "
            "so a concurrent duplicate may run too (%s)",
            purpose,
            backstop,
        )
        return None
    token = secrets.token_hex(16)
    try:
        acquired = await redis.set(name, token, nx=True, ex=ttl_seconds)
    except Exception as exc:  # noqa: BLE001 - the caller's backstop holds
        _report_once(
            f"{purpose}:claim-failed",
            "In-flight claim unavailable (%r): %s run without it",
            exc,
            purpose,
        )
        return None
    if acquired:
        claim = OwnedClaim(redis, name, token)
        if renew is not None:
            claim._keep(renew, ttl_seconds * 1000)
        return claim

    retry_after = 1
    try:
        remaining_ms = await redis.pttl(name)
        if remaining_ms and remaining_ms > 0:
            retry_after = max(1, math.ceil(remaining_ms / 1000))
    except Exception:  # noqa: BLE001 - 1 s is an honest lower bound
        pass
    raise ClaimHeld(retry_after)
