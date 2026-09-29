"""The dedup reader is bound to the KB WRITER's collection, by construction.

``KnowledgeService`` writes through ``knowledge_vector_store`` and nothing else
(#1168 removed the ``or vector_store`` fallback, which bypassed the tenant-stamp
guard and was never selectable). That writer is ``KnowledgeVectorStore``, whose
``add_documents`` targets the hardcoded ``KB_COLLECTION`` — while a bare
``ChromaDBVectorStore`` (``container.vector_store``) binds the settings-derived
collection name. A dedup reader built over the settings store diverges from the
writer the moment ``CHROMADB_COLLECTION`` is overridden, and a reader/writer
collection split silently reinstates the empty-result dedup fm#1030 removed.
These tests pin ``create_runbook_dedup_kb``'s binding rule for every writer
configuration (fm#1030 review, CORE 1).
"""

import inspect
from unittest.mock import MagicMock

import pytest

from faultmaven.container.providers.services import create_runbook_dedup_kb
from faultmaven.infrastructure.knowledge.knowledge_vector_store import KB_COLLECTION
from faultmaven.infrastructure.knowledge.runbook_kb import RunbookKnowledgeBase

pytestmark = [pytest.mark.unit]


def test_with_the_production_writer_the_reader_binds_kb_collection():
    """Writer = KnowledgeVectorStore (hardcoded KB_COLLECTION) → the reader is
    built over the same KB client, bound to the same constant — never to a
    settings-derived store."""
    kb = create_runbook_dedup_kb(
        knowledge_vector_store=MagicMock(),
        kb_chromadb_client=MagicMock(),
    )

    assert isinstance(kb, RunbookKnowledgeBase)
    assert kb.vector_store.collection_name == KB_COLLECTION


def test_no_kb_writer_means_no_dedup_reader_even_with_a_settings_store():
    """No ``knowledge_vector_store`` → no reader, whatever else the container
    holds. The settings-bound ``ChromaDBVectorStore`` used to be the reader
    here, paired with the service's ``or vector_store`` writer fallback. The
    writer half is gone (#1168), so a reader over that store would search a
    collection nothing writes — and the factory no longer even accepts one."""
    assert "vector_store" not in inspect.signature(create_runbook_dedup_kb).parameters

    kb = create_runbook_dedup_kb(
        knowledge_vector_store=None,
        kb_chromadb_client=MagicMock(),
    )

    assert kb is None


def test_a_writer_without_its_client_disables_dedup_rather_than_misbinding():
    """KnowledgeVectorStore present but no KB client to rebind: return None
    (honest "dedup did not run") instead of falling back to the settings
    store, whose collection the writer may never write."""
    kb = create_runbook_dedup_kb(
        knowledge_vector_store=MagicMock(),
        kb_chromadb_client=None,
    )

    assert kb is None


def test_no_stores_at_all_means_no_dedup_kb():
    assert (
        create_runbook_dedup_kb(knowledge_vector_store=None, kb_chromadb_client=None)
        is None
    )
