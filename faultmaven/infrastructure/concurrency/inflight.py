"""One record of the work in flight: drained per process, read per case (#2490, #2096).

**Per process.** :func:`track` holds every task that outlives the request that
started it (a turn's settlement, its uploads' ``mark_linked``, vectorization, a
runbook conversion) until it ends. The container's last release drains it
(:func:`drain`) before it resets and closes what the composition owns, so work
in flight never meets a closed Redis client, ChromaDB client or engine.
``cancellable`` work is cancelled once the bound has passed; ``must_finish``
work (the settlement: no cancellation may reach its commit, R1) is awaited with
no bound, which the database's command timeout supplies (#2280). A cancel does
not stop a thread, so :func:`draining` holds while a release that is still the
last drains and then closes the stores, and the vector write reads it first.
**Residual:** a write already in its thread at the bound finishes into the
client after it. **Residual (two loops):** a drain superseded by a hold from
another loop leaves the flag down while its own loop's work runs; if that
hold's release closes the stores first, that work's writes after those closes
are not refused (#2532).

**Per case, across replicas.** A case's lease is the Redis sorted set
``case-inflight:{enterprise_id}:{case_id}``: holder tokens scored by expiry on
the Redis clock (``TIME``). A process has ONE member per case, counted here.
Every turn writes it (:func:`hold_case`), joining or not, before it reads the
tombstone; :func:`track` with ``case=`` counts a task synchronously, before its
first step, so no window opens at a turn's release. The member goes with the
last local holder. Renewals and releases are never tracked. A delete
(:class:`CaseDeleteGate`) writes a tombstone and then reads the lease. Each
side writes before it reads, so both cannot pass; both may refuse.

**Residual, ruled (2026-11-04):** a turn whose lease write or tombstone read
fails runs unguarded (logged once, counted) while a delete fails closed (503);
a delete that passes meanwhile can race it. Standalone's Redis is in-process.
"""

import asyncio
import contextlib
import enum
import logging
import math
import secrets
from typing import Any, Callable, Dict, Iterator, Optional, Set, Tuple

from faultmaven.infrastructure.concurrency.owned_claim import (
    OwnedClaim,
    renew_every,
)

logger = logging.getLogger(__name__)

#: How long the drain waits for work in flight before it cancels the rest.
DRAIN_BOUND_SECONDS = 10.0
#: How long a cancelled task is given to end once cancelled.
CANCEL_GRACE_SECONDS = 1.0

#: A case lease's life without a renewal, and the wait between two renewals.
LEASE_TTL_MS = 30_000
LEASE_RENEW_SECONDS = 10.0
#: The key outlives its latest member by this much.
LEASE_KEY_MARGIN_MS = 5_000
#: The delete's tombstone: renewed while the delete runs, so this bounds only
#: one whose process died.
TOMBSTONE_TTL_SECONDS = 30
TOMBSTONE_RENEW_SECONDS = 10.0
#: ``Retry-After`` of a delete refused because the store is unreachable.
DELETE_UNAVAILABLE_RETRY_AFTER_SECONDS = 10

#: ``x-error-code``s. A delete refused while work on the case is in flight, a
#: turn refused while a delete of its case runs (no ``Retry-After``: the client
#: reloads the case), and a delete refused because the store is unreachable.
CASE_WORK_IN_PROGRESS = "CASE_WORK_IN_PROGRESS"
CASE_DELETE_IN_PROGRESS = "CASE_DELETE_IN_PROGRESS"
CASE_DELETE_UNAVAILABLE = "CASE_DELETE_UNAVAILABLE"

#: ``(enterprise_id, case_id)``.
CaseKey = Tuple[str, str]

#: Write (``ARGV[3]`` empty) or renew (``"XX"``) one member, scored by its
#: expiry on the Redis clock, then re-arm the key's TTL to the latest expiry
#: plus a margin (``ZADD`` never touches it). A renewal of a member gone: 0.
TAKE_OR_RENEW = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
if ARGV[3] == 'XX' then
  if not redis.call('ZSCORE', KEYS[1], ARGV[1]) then
    return 0
  end
  redis.call('ZADD', KEYS[1], 'XX', now + tonumber(ARGV[2]), ARGV[1])
else
  redis.call('ZADD', KEYS[1], now + tonumber(ARGV[2]), ARGV[1])
end
local latest = redis.call('ZRANGE', KEYS[1], -1, -1, 'WITHSCORES')[2]
redis.call('PEXPIRE', KEYS[1], tonumber(latest) - now + tonumber(ARGV[4]))
return 1
"""

#: The delete's read, in one script on the Redis clock: drop the lapsed
#: members, then count the live ones. ``{live, ms until the latest lapses}``.
READ_LEASES = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
local live = redis.call('ZCARD', KEYS[1])
if live == 0 then
  return {0, 0}
end
local latest = redis.call('ZRANGE', KEYS[1], -1, -1, 'WITHSCORES')[2]
return {live, tonumber(latest) - now}
"""


class Kind(enum.Enum):
    """What the drain may do to a tracked task once its bound has passed."""

    CANCELLABLE = "cancellable"
    MUST_FINISH = "must_finish"


_TRACKED: Dict["asyncio.Task[Any]", Tuple[str, Kind]] = {}
_RELEASES: Set["asyncio.Task[None]"] = set()
_LEASES: Dict[CaseKey, "_CaseLease"] = {}
_DRAINS: list[Callable[[], bool]] = []  # each running drain's last(); they overlap
_REPORTED: Set[str] = set()


def lease_key(case: CaseKey) -> str:
    return f"case-inflight:{case[0]}:{case[1]}"


def tombstone_key(case: CaseKey) -> str:
    return f"case-deleting:{case[0]}:{case[1]}"


def track(
    task: "asyncio.Task[Any]",
    *,
    what: str,
    case: Optional[CaseKey] = None,
    kind: Kind,
) -> "asyncio.Task[Any]":
    """Hold ``task`` until it ends, and return it.

    Call it on the task as it is created, with no ``await`` between: with
    ``case``, the case's count rises before the task's first step can run, and
    falls when it ends. ``what`` names it in the drain's log.
    """
    _TRACKED[task] = (what, kind)
    lease = _join(case) if case is not None else None

    def _ended(finished: "asyncio.Task[Any]") -> None:
        _TRACKED.pop(finished, None)
        if lease is not None:
            _release_soon(lease)

    task.add_done_callback(_ended)
    return task


def tracked(what_prefix: str = "") -> Set["asyncio.Task[Any]"]:
    """The tracked tasks whose ``what`` starts with ``what_prefix``. For tests."""
    return {
        t for t, (what, _) in list(_TRACKED.items()) if what.startswith(what_prefix)
    }


def draining() -> bool:
    """Whether a drain runs whose release no hold has superseded, or a release
    still the last runs, from its drain until its closes end: a store write
    that has not started must not start. A drain stops counting toward it once
    a hold is taken after its release, and never counts again (a drain no
    longer the last closes nothing); another drain still the last keeps it
    raised. Once a last release's closes begin, the flag stays raised until
    they end, whatever composes or resets meanwhile, so a second composition's
    evidence writes are refused during those closes."""
    return any(last() for last in list(_DRAINS))


@contextlib.contextmanager
def raised(last: Callable[[], bool]) -> Iterator[None]:
    """Count ``last`` toward :func:`draining` while the block runs."""
    drains = _DRAINS  # removed from the list it joined
    drains.append(last)
    try:
        yield
    finally:
        drains.remove(last)


async def drain(
    timeout_s: Optional[float] = None, last: Callable[[], bool] = lambda: True
) -> int:
    """Finish the work in flight on this loop. Returns how many were cancelled.

    Waits for every tracked task, those spawned meanwhile included, up to
    ``timeout_s`` (default ``DRAIN_BOUND_SECONDS``). Then it cancels the
    ``cancellable`` rest, gives them ``CANCEL_GRACE_SECONDS``, and awaits the
    ``must_finish`` ones with no bound. Once no work is left, the case leases
    being released get the same grace, and it returns. Once ``last()`` turns
    false (the release is no longer the last), this drain stops counting
    toward :func:`draining` at once and cancels nothing more: it still waits
    for this loop's work up to its bound, then returns, leaving the rest
    running. Superseded past the bound, during the cancel grace or the wait
    after it, it still ends that grace and waits, with no bound, for the
    ``must_finish`` work it did not cancel.
    Interrupted, it cancels nothing and re-raises: a last release then calls
    :func:`cancel_cancellable`. **Residual:** an interrupted shutdown does not
    wait for a settlement, which can then meet closed stores.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + (DRAIN_BOUND_SECONDS if timeout_s is None else timeout_s)
    abandoned: Set["asyncio.Task[Any]"] = set()
    cancelled = 0
    with raised(last):
        while True:
            pending = [t for t in _pending_on(loop) if t not in abandoned]
            if not pending:
                break
            if loop.time() < deadline:
                await asyncio.wait(pending, timeout=deadline - loop.time())
                continue
            if not last():
                return cancelled
            late = await _cancel_late(loop, abandoned)
            cancelled += len(late)
            finishing = [t for t in pending if t not in late]
            if finishing:
                await asyncio.wait(finishing)
        releases = [t for t in _RELEASES if t.get_loop() is loop]
        if releases:
            await asyncio.wait(releases, timeout=CANCEL_GRACE_SECONDS)
    return cancelled


async def cancel_cancellable() -> int:
    """Cancel this loop's ``cancellable`` work and give it the grace, which a
    second interruption ends. A last release whose drain was interrupted."""
    return len(await _cancel_late(asyncio.get_running_loop(), set()))


async def _cancel_late(loop: asyncio.AbstractEventLoop, abandoned: Set[Any]) -> list:
    """Cancel this loop's ``cancellable`` rest and give it the grace."""
    late = [
        t
        for t in _pending_on(loop)
        if t not in abandoned and _TRACKED[t][1] is Kind.CANCELLABLE
    ]
    for task in late:
        logger.warning("Cancelling %s: still running", _TRACKED[task][0])
        task.cancel()
    if late:
        await asyncio.wait(late, timeout=CANCEL_GRACE_SECONDS)
        abandoned.update(t for t in late if not t.done())
    return late


def _pending_on(loop: asyncio.AbstractEventLoop) -> list:
    """This loop's unfinished tracked tasks. A closed loop's are dropped (the
    cancel guarded); another live loop's are left to its own drain."""
    ours = []
    for task in list(_TRACKED):
        if task.get_loop() is loop:
            if not task.done():
                ours.append(task)
        elif task.get_loop().is_closed():
            _TRACKED.pop(task, None)
            try:
                task.cancel()
            except Exception:  # noqa: BLE001 - cancelling it raises
                pass
    return ours


def let_leases_lapse() -> None:
    """Stop every lease renewal on this loop: a last release whose drain ended.

    A holder left (work the drain abandoned) writes nothing more, and its lease
    lapses on its TTL; a later turn starts a fresh one.
    """
    loop = asyncio.get_running_loop()
    for case, lease in list(_LEASES.items()):
        if lease.loop is loop:
            del _LEASES[case]
            lease.ended, lease.redis = True, None
            if lease.renewal is not None:
                lease.renewal.cancel()


# ---------------------------------------------------------------------------
# The case lease
# ---------------------------------------------------------------------------


class CaseDeleteInProgress(Exception):
    """A delete of the case runs: the turn refuses, and its lease is gone."""


class CaseWorkInProgress(Exception):
    """Work on the case is in flight (or another delete runs): nothing cleared."""

    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after
        super().__init__(f"work on the case is in flight; retry after {retry_after} s")


class CaseDeleteUnavailable(Exception):
    """The lease or tombstone store is unreachable: nothing cleared."""


def _unguarded(reason: str, exc: Optional[BaseException]) -> None:
    """A turn proceeds that a delete cannot see: logged once per reason, counted."""
    if reason not in _REPORTED:
        _REPORTED.add(reason)
        logger.warning(
            "Case in-flight lease unavailable (%s%s): turns run without one, so a "
            "delete may race them (#2096)",
            reason,
            f": {exc!r}" if exc is not None else "",
        )
    try:
        from faultmaven.infrastructure.shims.metrics import case_unleased_turns

        case_unleased_turns.labels(reason=reason).inc()
    except Exception:  # noqa: BLE001 - a metrics failure must not fail a turn
        pass


class _CaseLease:
    """This process's one member of a case's lease, held while ``count`` > 0."""

    def __init__(self, case: CaseKey, redis: Any, loop: Any) -> None:
        self.case, self.redis, self.loop = case, redis, loop
        self.key = lease_key(case)
        self.member = secrets.token_hex(16)
        self.count = 0
        self.ended = False
        self.writes: Set["asyncio.Task[Any]"] = set()
        self.renewal: "Optional[asyncio.Task[None]]" = None
        self.error: Optional[BaseException] = None

    async def write(self) -> bool:
        """Write the member (a lapsed one comes back), and whether it landed."""
        if self.redis is None:
            return False
        write = asyncio.ensure_future(self._script(""))
        self.writes.add(write)
        write.add_done_callback(
            lambda w: self.writes.discard(w) or w.cancelled() or w.exception()
        )
        try:
            await asyncio.shield(write)
        except Exception as exc:  # noqa: BLE001 - the turn runs unleased (ruled)
            self.error = exc
            return False
        if not self.ended and self.renewal is None:
            self.renewal = asyncio.ensure_future(self._renew())
        return True

    def _script(self, mode: str) -> Any:
        ttl, margin = LEASE_TTL_MS, LEASE_KEY_MARGIN_MS
        return self.redis.eval(
            TAKE_OR_RENEW, 1, self.key, self.member, ttl, mode, margin
        )

    async def _renew(self) -> None:
        while True:
            await asyncio.sleep(LEASE_RENEW_SECONDS)
            try:
                if not await self._script("XX"):
                    logger.warning("Lease %s lapsed while held: rewriting", self.key)
                    if not await self.write():
                        logger.warning("Could not rewrite %s: %r", self.key, self.error)
            except Exception as exc:  # noqa: BLE001 - retried next interval
                logger.warning("Could not renew the lease %s: %r", self.key, exc)

    async def give_up(self) -> None:
        """Stop renewing, then remove the member once every write has landed."""
        if self.renewal is not None:
            self.renewal.cancel()
            await asyncio.wait({self.renewal})
        if self.writes:
            await asyncio.wait(set(self.writes))
        if self.redis is None:
            return
        try:
            await self.redis.zrem(self.key, self.member)
        except Exception as exc:  # noqa: BLE001 - the TTL is the backstop
            logger.warning("Could not release the lease %s: %r", self.key, exc)


def _join(case: CaseKey, redis: Any = None) -> _CaseLease:
    """Count one more local holder of ``case``'s lease. Synchronous."""
    loop = asyncio.get_running_loop()
    lease = _LEASES.get(case)
    if lease is None or lease.loop is not loop:
        # A task tracked with no turn holding its case (only a test calls the
        # engine without the route) holds no store until a turn brings one.
        lease = _LEASES[case] = _CaseLease(case, redis, loop)
    elif lease.redis is None:
        lease.redis = redis
    lease.count += 1
    return lease


def _leave(lease: _CaseLease) -> Any:
    """Count one holder fewer; the last one's ``give_up`` to await, or None."""
    lease.count -= 1
    if lease.count > 0:
        return None
    lease.ended = True
    if _LEASES.get(lease.case) is lease:
        del _LEASES[lease.case]
    return lease.give_up()


def _release_soon(lease: _CaseLease) -> None:
    """``_leave`` from a done-callback: the member's removal runs as a task."""
    give_up = _leave(lease)
    if give_up is None:
        return
    try:
        task = lease.loop.create_task(give_up)
    except RuntimeError:  # its loop has closed; the TTL is the backstop
        give_up.close()
        return
    _RELEASES.add(task)
    task.add_done_callback(_RELEASES.discard)


class CaseHold:
    """A turn's hold on its case's lease. ``release`` it once, in a ``finally``."""

    def __init__(self, lease: _CaseLease) -> None:
        self._lease: Optional[_CaseLease] = lease

    async def release(self) -> None:
        """Let the hold go. Safe to call more than once; never raises."""
        lease, self._lease = self._lease, None
        give_up = _leave(lease) if lease is not None else None
        if give_up is not None:
            await give_up


async def hold_case(redis: Any, case: CaseKey) -> CaseHold:
    """Write ``case``'s lease member for a turn, then read its tombstone.

    Raises :class:`CaseDeleteInProgress`, having let the lease go, while a
    delete of the case runs. If either command fails the turn proceeds
    unguarded (the module docstring's residual).
    """
    lease = _join(case, redis)
    hold = CaseHold(lease)
    try:
        landed = await lease.write()
        deleting = await redis.exists(tombstone_key(case)) if redis else False
    except Exception as exc:  # noqa: BLE001 - the turn runs unguarded (ruled)
        landed, deleting, lease.error = False, False, exc
    except BaseException:
        await hold.release()
        raise
    if deleting:
        await hold.release()
        raise CaseDeleteInProgress()
    if not landed:
        _unguarded("no_store" if redis is None else "store_error", lease.error)
    return hold


class CaseDeleteGate:
    """Where a case delete writes its tombstone and reads the case's lease."""

    def __init__(self, redis: Any) -> None:
        self._redis = redis

    async def begin(self, case: CaseKey) -> "CaseTombstone":
        """Write the tombstone, then read the lease; the tombstone, held.

        Raises :class:`CaseWorkInProgress` while a lease stands or another
        delete holds the tombstone, and :class:`CaseDeleteUnavailable` with no
        store or one that fails. Either way the delete clears nothing, and no
        tombstone of this delete is left.
        """
        redis, name = self._redis, tombstone_key(case)
        if redis is None:
            raise CaseDeleteUnavailable()
        token = secrets.token_hex(16)
        try:
            written = await redis.set(name, token, nx=True, ex=TOMBSTONE_TTL_SECONDS)
            left_ms = None if written else await redis.pttl(name)
        except Exception as exc:
            raise CaseDeleteUnavailable() from exc
        if not written:  # another delete runs
            raise CaseWorkInProgress(_seconds(left_ms))
        claim = OwnedClaim(redis, name, token)
        claim._keep(renew_every(TOMBSTONE_RENEW_SECONDS), TOMBSTONE_TTL_SECONDS * 1000)
        tombstone = CaseTombstone(claim)
        try:
            live, left_ms = await redis.eval(READ_LEASES, 1, lease_key(case))
        except BaseException as exc:
            await tombstone.end(deleted=False)
            if isinstance(exc, Exception):
                raise CaseDeleteUnavailable() from exc
            raise
        if live:
            await tombstone.end(deleted=False)
            raise CaseWorkInProgress(_seconds(left_ms))
        return tombstone

    async def deleting(self, case: CaseKey) -> bool:
        """Whether a delete of ``case`` runs: its tombstone stands (#2510).

        Best-effort: ``False`` with no store or a read that fails (logged once
        per reason), as for a tombstone that lapsed under a stalled delete, so
        it means no delete is *known* to run. The case then reads as live, and
        the turn's own read (:func:`hold_case`) still refuses it.
        """
        if self._redis is None:
            return _tombstone_unread("no_store", None)
        try:
            return bool(await self._redis.exists(tombstone_key(case)))
        except Exception as exc:  # noqa: BLE001 - the read is best-effort
            return _tombstone_unread("store_error", exc)


def _tombstone_unread(reason: str, exc: Optional[BaseException]) -> bool:
    """A read that cannot see a delete's tombstone: logged once per reason."""
    if f"tombstone:{reason}" not in _REPORTED:
        _REPORTED.add(f"tombstone:{reason}")
        logger.warning(
            "Case delete tombstone unreadable (%s%s): a case read reports no "
            "delete running (#2510)",
            reason,
            f": {exc!r}" if exc is not None else "",
        )
    return False


def _seconds(ms: Any) -> int:
    return max(1, math.ceil(int(ms or 0) / 1000))


class CaseTombstone:
    """A delete's tombstone, held while it runs."""

    def __init__(self, claim: OwnedClaim) -> None:
        self._claim = claim

    async def end(self, *, deleted: bool) -> None:
        """Delete the tombstone, unless the row is gone: then it lapses alone."""
        if not deleted:
            await self._claim.release()
        elif self._claim.renewal is not None:
            self._claim.renewal.cancel()
