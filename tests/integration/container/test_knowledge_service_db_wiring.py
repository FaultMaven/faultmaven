"""A really-initialized container yields a DB-capable KnowledgeService.

The provider-level guard lives in
``tests/unit/container/test_knowledge_service_db_wiring.py``. This is the
boot-time counterpart: it drives a full ``container.initialize()``, which is
what the jobs process does, so it costs real ChromaDB clients and a Redis
attempt — hence integration, not unit.
"""

import pytest

from faultmaven.infrastructure.persistence.database import get_db_session
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_container_built_knowledge_service_can_reach_the_database(
    fresh_container,
):
    """A container initialized the way the JOBS process initializes it yields a
    knowledge_service that can persist.

    ``faultmaven/jobs/run.py`` does exactly this and nothing else — imports the
    container singleton and awaits ``initialize()`` — so this *is* the jobs
    process's knowledge_service, not an approximation of it.
    """
    await fresh_container.initialize()

    knowledge_service = fresh_container.get_knowledge_service()

    # Assert the real class first: a partially composed container yields None
    # (before #899 it substituted an in-memory stub with no session factory at
    # all). Say so plainly rather than reporting it as the #894 wiring
    # regression.
    assert isinstance(knowledge_service, KnowledgeService), (
        "The container did not compose a real KnowledgeService (got "
        f"{type(knowledge_service).__name__}). Fix composition first — the "
        "session-factory assertion below is meaningless for a stub."
    )
    # Identity, not just truthiness: the production session factory is what
    # binds the RLS tenant scope per transaction, and every KB persistence path
    # gates on this attribute — ingest_runbook refuses outright without it, so
    # `kb_seed` fails for every pack runbook (#894).
    assert knowledge_service._db_session_factory is get_db_session


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_composed_knowledge_service_writes_only_through_the_guarded_store(
    service_checks_on,
    fresh_container,
):
    """The KB writer is ``KnowledgeVectorStore`` — the store whose
    ``add_documents`` refuses a KB chunk with no tenant stamp (#1168). The
    container used to wire ``knowledge_vector_store or vector_store``; that
    fallback is gone, and the dedup reader is bound to the same writer's
    collection.

    Runs under either ambient ``SKIP_SERVICE_CHECKS`` value (see
    ``service_checks_on``): it never skips, because a skipped pin would let the
    service's store change with CI still green.
    """
    from faultmaven.config.settings import get_settings
    from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
        KB_COLLECTION,
        KnowledgeVectorStore,
    )
    from faultmaven.infrastructure.persistence.chromadb_store import (
        ChromaDBVectorStore,
    )

    assert get_settings().server.skip_service_checks is False

    await fresh_container.initialize()
    knowledge_service = fresh_container.get_knowledge_service()
    plain_store = fresh_container.get_service("vector_store")

    # The service holds the KB store, not the plain one the container also
    # registers.
    assert isinstance(plain_store, ChromaDBVectorStore)
    assert isinstance(knowledge_service._vector_store, KnowledgeVectorStore)
    assert knowledge_service._vector_store is fresh_container.knowledge_vector_store
    assert knowledge_service._vector_store is not plain_store
    # One KB store per process: kb_qa and the registered service hold the
    # service's instance, not a second one (#2284 follow-up).
    assert knowledge_service._vector_store is fresh_container.get_service(
        "knowledge_vector_store"
    )
    assert fresh_container.kb_qa_tool._vector_store is knowledge_service._vector_store
    assert fresh_container.runbook_kb.vector_store.collection_name == KB_COLLECTION
