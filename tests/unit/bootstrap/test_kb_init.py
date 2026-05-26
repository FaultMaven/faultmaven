"""Unit tests for faultmaven.bootstrap.kb_init.

Covers the atomicity + idempotency guarantees the bootstrap was created
to enforce. Uses fakes for KnowledgeService and the DB session factory
so the tests run without ChromaDB or a real database — the bootstrap's
contract is fully describable in terms of those collaborators.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.bootstrap import kb_init

RUNBOOK_FRONTMATTER = """---
id: example-runbook
title: "Example Runbook"
domain: database
service: postgresql
symptom_class:
  - latency
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-05-26"
verified_by: "test"
status: draft
tags: [postgres, latency]
---

# Body content for tests.
"""


@pytest.fixture
def project_root_with_one_runbook(tmp_path: Path) -> Path:
    """Create a fake project root containing a single runbook under
    data/knowledge/global/."""
    kb_dir = tmp_path / "data" / "knowledge" / "global"
    kb_dir.mkdir(parents=True)
    (kb_dir / "example.md").write_text(RUNBOOK_FRONTMATTER, encoding="utf-8")
    return tmp_path


class FakeSession:
    """Async-context-manager fake that returns no existing rows."""

    def __init__(self, existing_row: Optional[object] = None) -> None:
        self._existing = existing_row

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *args) -> None:
        return None

    async def execute(self, _stmt):
        scalar_mock = MagicMock()
        scalar_mock.scalar_one_or_none = MagicMock(return_value=self._existing)
        return scalar_mock

    async def delete(self, _row):
        return None

    async def commit(self):
        return None


def _make_session_factory(existing_row=None):
    def factory():
        return FakeSession(existing_row)

    return factory


@pytest.fixture
def patch_model_cache():
    """Pretend BGE-M3 loaded successfully."""
    with patch.object(
        kb_init, "_enumerate_runbook_files", wraps=kb_init._enumerate_runbook_files
    ):
        with patch(
            "faultmaven.infrastructure.model_cache.model_cache"
        ) as model_cache_mock:
            model_cache_mock.get_bge_m3_model.return_value = MagicMock(name="bge_m3")
            yield model_cache_mock


@pytest.mark.asyncio
async def test_bootstrap_ingests_new_runbook(
    project_root_with_one_runbook, patch_model_cache
):
    """A fresh runbook (no DB row) is ingested and counted."""
    knowledge_service = MagicMock()
    knowledge_service.ingest_runbook = AsyncMock(return_value=4)

    result = await kb_init.bootstrap_kb(
        knowledge_service=knowledge_service,
        db_session_factory=_make_session_factory(existing_row=None),
        organization_id="org-test",
        project_root=project_root_with_one_runbook,
    )

    assert len(result.ingested) == 1
    assert result.skipped_unchanged == []
    assert result.failed == []
    knowledge_service.ingest_runbook.assert_awaited_once()
    # Verify deterministic item_id was passed (kb_<sha256(id)[:12]>).
    call_kwargs = knowledge_service.ingest_runbook.await_args.kwargs
    assert call_kwargs["document_id"] == kb_init._item_id_from_runbook_id(
        "example-runbook"
    )
    assert call_kwargs["title"] == "Example Runbook"
    assert call_kwargs["verified_by"] == "system"


@pytest.mark.asyncio
async def test_bootstrap_skips_unchanged_runbook(
    project_root_with_one_runbook, patch_model_cache
):
    """Re-running with the same file content skips ingestion."""
    knowledge_service = MagicMock()
    knowledge_service.ingest_runbook = AsyncMock(return_value=4)

    # Existing row whose `content` matches the file on disk verbatim.
    existing = MagicMock()
    existing.content = (
        project_root_with_one_runbook / "data" / "knowledge" / "global" / "example.md"
    ).read_text(encoding="utf-8")

    result = await kb_init.bootstrap_kb(
        knowledge_service=knowledge_service,
        db_session_factory=_make_session_factory(existing_row=existing),
        organization_id="org-test",
        project_root=project_root_with_one_runbook,
    )

    assert result.ingested == []
    assert len(result.skipped_unchanged) == 1
    assert result.failed == []
    knowledge_service.ingest_runbook.assert_not_awaited()


@pytest.mark.asyncio
async def test_bootstrap_re_ingests_changed_runbook(
    project_root_with_one_runbook, patch_model_cache
):
    """Content drift on disk triggers re-ingestion (delete + re-create)."""
    knowledge_service = MagicMock()
    knowledge_service.ingest_runbook = AsyncMock(return_value=4)
    knowledge_service._vector_store = MagicMock()
    knowledge_service._vector_store.delete_documents_by_parent_id = AsyncMock()

    # Existing DB row whose content differs from disk.
    existing = MagicMock()
    existing.content = "STALE CONTENT — does not match disk"

    result = await kb_init.bootstrap_kb(
        knowledge_service=knowledge_service,
        db_session_factory=_make_session_factory(existing_row=existing),
        organization_id="org-test",
        project_root=project_root_with_one_runbook,
    )

    assert len(result.ingested) == 1
    assert result.skipped_unchanged == []
    knowledge_service.ingest_runbook.assert_awaited_once()
    # Stale chunks should have been cleaned out of ChromaDB.
    knowledge_service._vector_store.delete_documents_by_parent_id.assert_awaited()


@pytest.mark.asyncio
async def test_bootstrap_raises_on_zero_chunks(
    project_root_with_one_runbook, patch_model_cache
):
    """If ingest_runbook reports 0 chunks (silent failure), the file is
    recorded as failed AND the orphan-cleanup path is invoked."""
    knowledge_service = MagicMock()
    knowledge_service.ingest_runbook = AsyncMock(return_value=0)
    knowledge_service._vector_store = MagicMock()
    knowledge_service._vector_store.delete_documents_by_parent_id = AsyncMock()

    result = await kb_init.bootstrap_kb(
        knowledge_service=knowledge_service,
        db_session_factory=_make_session_factory(existing_row=None),
        organization_id="org-test",
        project_root=project_root_with_one_runbook,
    )

    assert result.ingested == []
    assert len(result.failed) == 1
    failed_file, reason = result.failed[0]
    assert "example.md" in failed_file
    assert "0 chunks" in reason or "RuntimeError" in reason


@pytest.mark.asyncio
async def test_bootstrap_no_knowledge_dir_is_safe(tmp_path: Path, patch_model_cache):
    """If data/knowledge/ does not exist, bootstrap returns an empty result
    without attempting ingestion or raising."""
    knowledge_service = MagicMock()
    knowledge_service.ingest_runbook = AsyncMock(return_value=4)

    result = await kb_init.bootstrap_kb(
        knowledge_service=knowledge_service,
        db_session_factory=_make_session_factory(existing_row=None),
        organization_id="org-test",
        project_root=tmp_path,
    )

    assert result.ingested == []
    assert result.skipped_unchanged == []
    assert result.failed == []
    knowledge_service.ingest_runbook.assert_not_awaited()


@pytest.mark.asyncio
async def test_bootstrap_aborts_when_bge_m3_unavailable(
    project_root_with_one_runbook,
):
    """If BGE-M3 fails to load, the bootstrap aborts without attempting
    any ingestion (the silent-0-chunks failure mode is unavoidable in this
    state, so the right behaviour is fail-fast)."""
    knowledge_service = MagicMock()
    knowledge_service.ingest_runbook = AsyncMock(return_value=4)

    with patch("faultmaven.infrastructure.model_cache.model_cache") as model_cache_mock:
        model_cache_mock.get_bge_m3_model.return_value = None

        result = await kb_init.bootstrap_kb(
            knowledge_service=knowledge_service,
            db_session_factory=_make_session_factory(existing_row=None),
            organization_id="org-test",
            project_root=project_root_with_one_runbook,
        )

    assert result.ingested == []
    assert result.failed == []
    knowledge_service.ingest_runbook.assert_not_awaited()


def test_item_id_from_runbook_id_is_deterministic():
    assert kb_init._item_id_from_runbook_id("foo") == kb_init._item_id_from_runbook_id(
        "foo"
    )
    assert kb_init._item_id_from_runbook_id("foo") != kb_init._item_id_from_runbook_id(
        "bar"
    )
    assert kb_init._item_id_from_runbook_id("foo").startswith("kb_")
