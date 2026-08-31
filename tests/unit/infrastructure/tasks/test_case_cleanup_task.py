"""The in-process case-cleanup task's reference-set guard (issue #1232).

This is the SECOND copy of the case-cleanup sweep — the CLI job in
``faultmaven/jobs/case_cleanup.py`` is the other — and it ran the same
unguarded diff: `list_all_case_ids()` returning `[]` makes
`expected_collections` empty inside `cleanup_orphaned_collections`, so every
case collection is classified orphaned and deleted. The per-candidate
`case_exists` re-check reads the same repository and answers "no" too, so it
rescues nothing.

Fixing only the CLI job would have left this path open, and this one runs
unattended inside the API process on a 6-hour timer.

Run with:
    pytest tests/unit/infrastructure/tasks/test_case_cleanup_task.py -v
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from faultmaven.infrastructure.tasks.case_cleanup import (
    cleanup_orphaned_collections_task,
)


def _store(collections):
    store = AsyncMock()
    store.list_case_collection_ids = AsyncMock(return_value=collections)
    store.cleanup_orphaned_collections = AsyncMock(return_value=0)
    return store


def _repo(case_ids):
    repo = AsyncMock()
    repo.list_all_case_ids = AsyncMock(return_value=case_ids)
    repo.get = AsyncMock(return_value=None)
    return repo


class TestTheTaskRefusesADisjointReferenceSet:
    @pytest.mark.asyncio
    async def test_empty_case_id_set_does_not_delete(self):
        """The RLS / broken-authority shape."""
        store = _store(["case_a", "case_b"])

        await cleanup_orphaned_collections_task(store, _repo([]))

        store.cleanup_orphaned_collections.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_empty_but_disjoint_does_not_delete(self):
        """The half an emptiness check misses."""
        store = _store(["case_a", "case_b"])

        await cleanup_orphaned_collections_task(store, _repo(["case_zzz"]))

        store.cleanup_orphaned_collections.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_one_overlapping_id_permits_the_sweep(self):
        """Positive control — a task that never swept would pass both tests
        above while doing nothing useful."""
        store = _store(["case_a", "case_orphan"])

        await cleanup_orphaned_collections_task(store, _repo(["case_a"]))

        store.cleanup_orphaned_collections.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_collections_is_not_a_refusal(self):
        """Nothing enumerated means no decision to make."""
        store = _store([])

        await cleanup_orphaned_collections_task(store, _repo([]))

        store.cleanup_orphaned_collections.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_failing_candidate_listing_does_not_delete(self):
        """Unable to enumerate is 'cannot decide', not 'nothing is there'."""
        store = AsyncMock()
        store.list_case_collection_ids = AsyncMock(
            side_effect=RuntimeError("chromadb unreachable")
        )
        store.cleanup_orphaned_collections = AsyncMock(return_value=0)

        await cleanup_orphaned_collections_task(store, _repo(["case_a"]))

        store.cleanup_orphaned_collections.assert_not_awaited()
