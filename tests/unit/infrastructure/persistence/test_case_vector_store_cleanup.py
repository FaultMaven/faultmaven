"""Unit tests for CaseVectorStore.cleanup_orphaned_collections.

The orphaned-collection sweep went live for the first time when case_cleanup
was rewired onto the case repository (it had been silently 'skipped' since
inception), so its diff/prefix/re-check logic gets direct coverage here: the
sweep must delete ONLY ``case_``-prefixed collections with no case row, never
a live case's collection and never a non-case collection.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from faultmaven.infrastructure.persistence.case_vector_store import CaseVectorStore


def _collections(*names):
    return [SimpleNamespace(name=n) for n in names]


@pytest.fixture
def store():
    client = MagicMock()
    return CaseVectorStore(client=client), client


@pytest.mark.asyncio
async def test_deletes_only_orphaned_case_collections(store):
    vs, client = store
    client.list_collections.return_value = _collections(
        "case_live1",  # in the reference set -> kept
        "case_orphan1",  # not in the set -> deleted
        "case_orphan2",  # not in the set -> deleted
        "faultmaven_kb",  # no case_ prefix -> never touched
    )

    deleted = await vs.cleanup_orphaned_collections(["live1"])

    assert deleted == 2
    deleted_names = {
        c.kwargs.get("name") or c.args[0]
        for c in client.delete_collection.call_args_list
    }
    assert deleted_names == {"case_orphan1", "case_orphan2"}


@pytest.mark.asyncio
async def test_empty_reference_set_spares_non_case_collections(store):
    # Even a pathological empty id set only ever touches case_ collections.
    vs, client = store
    client.list_collections.return_value = _collections("faultmaven_kb", "case_x")

    deleted = await vs.cleanup_orphaned_collections([])

    assert deleted == 1
    client.delete_collection.assert_called_once_with(name="case_x")


@pytest.mark.asyncio
async def test_recheck_spares_case_created_after_snapshot(store):
    # The snapshot race: 'case_new' was created after the caller built the
    # reference set. The per-candidate re-check must spare it.
    vs, client = store
    client.list_collections.return_value = _collections("case_new", "case_gone")

    async def case_exists(case_id):
        return case_id == "new"

    deleted = await vs.cleanup_orphaned_collections([], case_exists=case_exists)

    assert deleted == 1
    client.delete_collection.assert_called_once_with(name="case_gone")


@pytest.mark.asyncio
async def test_recheck_failure_fails_safe(store):
    # If the DB re-check errors, do NOT delete on a stale snapshot alone.
    vs, client = store
    client.list_collections.return_value = _collections("case_maybe")

    async def case_exists(case_id):
        raise RuntimeError("db unavailable")

    deleted = await vs.cleanup_orphaned_collections([], case_exists=case_exists)

    assert deleted == 0
    client.delete_collection.assert_not_called()
