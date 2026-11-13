"""Principle 14's admission rule for its workers' slots (#2645, ruled on #2661).

A principal holding a slot takes another only while two are free, so one is
left for a newcomer; a freed slot goes to the waiter holding the fewest. Each
worker (document preparation, Evidence Tier 1) waits under it with the user
who asked as its principal. The jobs here are plain coroutines.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from faultmaven.infrastructure.worker_process import FairSlots
from faultmaven.models.api import DataType

pytestmark = [pytest.mark.unit]


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def test_alone_a_principal_uses_all_but_one_slot():
    slots, running, most = FairSlots(3), 0, 0
    release = asyncio.Event()

    async def job() -> None:
        nonlocal running, most
        await slots.acquire("a")
        running += 1
        most = max(most, running)
        await release.wait()
        running -= 1
        slots.release("a")

    jobs = [asyncio.create_task(job()) for _ in range(5)]
    await _settle()
    assert (running, slots.locked()) == (2, True)
    release.set()
    await asyncio.gather(*jobs)
    assert (most, slots.locked()) == (2, False)


async def test_a_newcomer_starts_beside_a_principal_holding_a_slot():
    """#2645's shape on two slots: A's second job waits, B starts at once."""
    slots = FairSlots(2)
    await slots.acquire("a")
    second = asyncio.create_task(slots.acquire("a"))
    await _settle()
    assert not second.done()

    await asyncio.wait_for(slots.acquire("b"), 1)

    slots.release("a")
    await asyncio.wait_for(second, 1)  # a holds none, so it is admitted


async def test_a_freed_slot_goes_to_the_waiter_holding_fewest():
    slots, order = FairSlots(3), []
    for p in ("a", "a", "c"):
        await slots.acquire(p)

    async def wait(p: str) -> None:
        await slots.acquire(p)
        order.append(p)

    waiters = [asyncio.create_task(wait(p)) for p in ("a", "b")]
    await _settle()
    slots.release("c")  # one free: a (holding two) is not admitted, b is
    await _settle()
    assert order == ["b"]
    slots.release("a")
    slots.release("a")  # two free: a, now holding none, is admitted
    await asyncio.gather(*waiters)
    assert order == ["b", "a"]


async def test_among_equals_the_earliest_waiter_goes_first():
    slots, order = FairSlots(1), []
    await slots.acquire("a")

    async def wait(p: str) -> None:
        await slots.acquire(p)
        order.append(p)
        slots.release(p)

    waiters = []
    for p in ("c", "b", "d"):  # none holds a slot: arrival decides
        waiters.append(asyncio.create_task(wait(p)))
        await _settle()
    slots.release("a")
    await asyncio.gather(*waiters)
    assert order == ["c", "b", "d"]


async def test_a_cancelled_waiter_leaves_the_queue():
    slots = FairSlots(1)
    await slots.acquire("a")
    waiter = asyncio.create_task(slots.acquire("b"))
    await _settle()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    slots.release("a")
    assert not slots.locked()
    await asyncio.wait_for(slots.acquire("c"), 1)


async def test_a_waiter_granted_then_cancelled_releases_its_grant():
    slots = FairSlots(1)
    await slots.acquire("a")
    waiter = asyncio.create_task(slots.acquire("b"))
    await _settle()
    slots.release("a")  # granted to b, which has not resumed yet
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert not slots.locked()
    await asyncio.wait_for(slots.acquire("c"), 1)


async def test_locked_while_none_is_free_or_someone_waits():
    slots = FairSlots(2)
    assert not slots.locked()
    await slots.acquire("a")
    waiter = asyncio.create_task(slots.acquire("a"))  # a holds one: waits
    await _settle()
    assert slots.locked(), "a slot is free, but a principal waits"
    slots.release("a")
    await waiter
    assert not slots.locked()
    await slots.acquire("b")
    assert slots.locked(), "no slot is free"


# ---------------------------------------------------------------------------
# Each worker waits with its principal
# ---------------------------------------------------------------------------


class _Asked(Exception):
    pass


@pytest.fixture
def asked(monkeypatch):
    """The principal each ``acquire`` is asked with; the job stops there."""
    seen: list[str] = []

    async def acquire(self, principal: str) -> None:
        seen.append(principal)
        raise _Asked

    monkeypatch.setattr(FairSlots, "acquire", acquire)
    return seen


async def test_preparation_waits_with_its_principal(asked, tmp_path):
    from faultmaven.modules.knowledge.domain.services.document_preprocessor import (
        DocumentPreprocessor,
    )

    with pytest.raises(_Asked):
        await DocumentPreprocessor(llm_router=None, sanitizer=None).preprocess(
            tmp_path / "a.md", "text/markdown", conversion_id="c", principal="u-prep"
        )
    assert asked == ["u-prep"]


def _tier1_service():
    from faultmaven.modules.preprocessing.preprocessing_service import (
        PreprocessingService,
    )
    from tests.unit.modules.preprocessing.extraction_stubs import CrimeScene

    classifier = MagicMock()
    classifier.classify.return_value = MagicMock(
        data_type=DataType.LOGS_AND_ERRORS,
        confidence=0.9,
        source="rule_based",
        source_type=None,
        classification_failed=False,
        suggested_types=None,
    )
    return PreprocessingService(classifier=classifier, logs_extractor=CrimeScene())


_LOGS = "\n".join(f"2026-11-13 ERROR line {i}" for i in range(250))


async def test_tier1_upload_waits_with_its_principal(asked):
    with pytest.raises(_Asked):
        await _tier1_service().classify_and_extract(
            content=_LOGS, filename="x.log", principal="u-upload"
        )
    assert asked == ["u-upload"]


async def test_tier1_reclassification_waits_with_its_principal(asked):
    with pytest.raises(_Asked):
        await _tier1_service().reclassify_evidence(
            content=_LOGS,
            filename="x.log",
            user_override=DataType.LOGS_AND_ERRORS,
            principal="u-reclassify",
        )
    assert asked == ["u-reclassify"]
