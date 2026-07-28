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


@pytest.fixture
def fresh_container():
    """The DI container singleton, reset around the test.

    The integration lane does not inherit the root conftest's fixtures (its
    own conftest imports the root module only for its import-time mocks), so
    the reset is spelled out here. Resetting afterwards matters: this test
    fully initializes the singleton every other test in the session shares.
    """
    from faultmaven.container import container

    container.reset()
    yield container
    container.reset()


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

    # Assert the real class first: a partially composed container substitutes
    # MinimalKnowledgeService (#899), which has no session factory at all. Say
    # so plainly rather than reporting it as the #894 wiring regression.
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
