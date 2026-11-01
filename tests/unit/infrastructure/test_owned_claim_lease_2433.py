"""The owned claim as a lease, and the shared request fingerprint (#2433).

The KB upload and conversion routes take the turn route's owner-tokened claim
(#1888), moved to ``infrastructure/concurrency/owned_claim``, as a lease: a
conversion has no bound of its own, so the claim is renewed while its holder
runs. Against FakeRedis, with no wall clock: the lease's interval is the
test's to step.

The turn's fingerprint moved to ``infrastructure/concurrency/request_fingerprint``
and must stay byte-identical, or a turn receipt stored before the deploy stops
matching its retry.
"""

import asyncio
import contextlib

import fakeredis.aioredis as fakeredis_aio
import pytest

from faultmaven.infrastructure.concurrency.owned_claim import ClaimHeld, take_claim
from faultmaven.infrastructure.concurrency.request_fingerprint import (
    digest_file,
    request_fingerprint,
)

pytestmark = pytest.mark.unit

CLAIM = "kb-inflight:e:u:convert:key-2433"


class _Interval:
    """The lease's interval, stepped by the test."""

    def __init__(self) -> None:
        self._go, self._parked = asyncio.Event(), asyncio.Event()

    async def __call__(self) -> None:
        self._parked.set()
        await self._go.wait()
        self._go.clear()

    async def elapse(self, renewal: asyncio.Task) -> None:
        """One interval passes: the renewal runs once, and this returns once
        it waits for the next interval or has ended."""
        await asyncio.wait_for(self._parked.wait(), 5)
        self._parked.clear()
        self._go.set()
        parked = asyncio.ensure_future(self._parked.wait())
        await asyncio.wait(
            {parked, renewal}, timeout=5, return_when=asyncio.FIRST_COMPLETED
        )
        parked.cancel()


async def _lease(redis, interval):
    return await take_claim(
        redis,
        CLAIM,
        ttl_seconds=60,
        purpose="keyed test requests",
        backstop="a receipt",
        renew=interval,
    )


class TestTheLease:
    async def test_renewal_rearms_the_ttl_while_the_holder_runs(self):
        redis = fakeredis_aio.FakeRedis(decode_responses=True)
        interval = _Interval()
        claim = await _lease(redis, interval)
        await redis.pexpire(CLAIM, 5_000)  # most of the lease has elapsed

        await interval.elapse(claim.renewal)

        assert await redis.pttl(CLAIM) > 55_000, "the lease was not re-armed"
        assert not claim.renewal.done()
        await interval.elapse(claim.renewal)
        assert not claim.renewal.done(), "it renews for as long as the holder runs"
        await claim.release()

    async def test_a_replaced_holder_neither_renews_nor_releases_the_new_claim(
        self,
    ):
        """A's lease lapsed (a stalled renewal) and B claimed the key. A's
        renewal and A's release must leave B's claim as it is."""
        redis = fakeredis_aio.FakeRedis(decode_responses=True)
        interval = _Interval()
        a = await _lease(redis, interval)
        await redis.delete(CLAIM)
        b = await take_claim(
            redis, CLAIM, ttl_seconds=60, purpose="keyed test requests", backstop="b"
        )
        await redis.pexpire(CLAIM, 5_000)
        held_by_b = await redis.get(CLAIM)

        await interval.elapse(a.renewal)
        await a.release()

        assert a.renewal.done(), "a renewal that lost its token stops"
        assert await redis.get(CLAIM) == held_by_b
        assert await redis.pttl(CLAIM) <= 5_000, "A re-armed B's claim"
        with pytest.raises(ClaimHeld):
            await _lease(redis, _Interval())
        await b.release()
        assert await redis.exists(CLAIM) == 0

    async def test_release_stops_the_renewal_and_frees_the_key(self):
        redis = fakeredis_aio.FakeRedis(decode_responses=True)
        claim = await _lease(redis, _Interval())

        await claim.release()
        await asyncio.wait({claim.renewal}, timeout=5)

        assert claim.renewal.cancelled()
        assert await redis.exists(CLAIM) == 0

    async def test_the_holders_cancellation_stops_the_renewal(self):
        """A holder cancelled without reaching its release (an anyio scope
        cancels the ``finally``'s await too): the renewal stops at its next
        interval, and the claim lapses at its TTL instead of being kept
        forever by a task nobody runs."""
        redis = fakeredis_aio.FakeRedis(decode_responses=True)
        interval = _Interval()
        taken, claims = asyncio.Event(), []

        async def holder():
            claims.append(await _lease(redis, interval))
            taken.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(holder())
        await asyncio.wait_for(taken.wait(), 5)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        (claim,) = claims
        await redis.pexpire(CLAIM, 5_000)

        await interval.elapse(claim.renewal)

        assert claim.renewal.done()
        assert await redis.pttl(CLAIM) <= 5_000, "a dead holder's lease renewed"


#: A turn's inputs, and the fingerprint the turn route's own function gave them
#: on ``2ada6d455``, before it moved.
TURN_FIELDS = {
    "query": "checkout 503s",
    "pasted_content": None,
    "intent_type": "confirmation",
    "intent_data": '{"proposal_id": "0123456789abcdef"}',
    "input_type": None,
    "source_url": "https://status.example.com/incident/42",
    "observed_at": "2026-11-01T03:00:00Z",
}
TURN_FILES = [("app.log", b"ERROR pool exhausted\n"), ("pool.cfg", b"max=10\n")]
TURN_FINGERPRINT = "b7dad4cf66e3e6900819729c5171a65ca1ec00de7f1fc723d8d3ed63431432d0"
EMPTY_TURN_FINGERPRINT = (
    "45de4a3dba72f3d148722593a51fe70fc71959c221df321b6f8f3e40e05a098e"
)


class TestTheSharedFingerprint:
    def test_a_turns_fingerprint_is_what_it_was_before_the_move(self):
        assert request_fingerprint(TURN_FIELDS, TURN_FILES) == TURN_FINGERPRINT
        assert (
            request_fingerprint(dict.fromkeys(TURN_FIELDS), [])
            == EMPTY_TURN_FINGERPRINT
        )

    def test_a_file_on_disk_fingerprints_as_its_bytes(self, tmp_path):
        path = tmp_path / "source.pdf"
        path.write_bytes(b"%PDF-1.7 a source document")
        fields = {"scope": "personal", "team_id": None}

        on_disk = request_fingerprint(fields, [digest_file("source.pdf", path)])

        assert on_disk == request_fingerprint(
            fields, [("source.pdf", b"%PDF-1.7 a source document")]
        )
        assert on_disk != request_fingerprint(
            fields, [("source.pdf", b"%PDF-1.7 another document!!")]
        ), "the content, not the name and size, identifies the file"
