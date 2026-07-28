"""The container-built KnowledgeService must be able to reach the database.

``KnowledgeService.ingest_runbook`` refuses outright when
``_db_session_factory`` is unset ("vector-only ingestion is no longer
supported"), and every other KB persistence path degrades to a warning and an
empty result. The web process used to hide that by patching the attribute onto
the instance from the lifespan, so only the *web* process had a DB-capable
service. The jobs process (``python -m faultmaven.jobs.run``) initializes the
same container and never runs a lifespan, so ``kb_seed`` failed on every pack
runbook — and under ``TENANT_PROVIDER=multi`` that job is the only seeding
path, leaving a fresh deployment with an empty global KB (#894).

The fix is at the composition root: the container hands KnowledgeService the
session factory at construction, so every process gets the same capability.
These tests exercise the container registration path rather than a hand-built
KnowledgeService, because the defect lived in the wiring, not the service.
"""

import pytest

from faultmaven.config.settings import FaultMavenSettings
from faultmaven.container.providers.services import create_knowledge_service
from faultmaven.infrastructure.persistence.database import get_db_session


@pytest.mark.unit
@pytest.mark.asyncio
async def test_container_built_knowledge_service_can_reach_the_database(
    reset_container,
):
    """A container initialized the way the JOBS process initializes it yields a
    knowledge_service that can persist.

    ``faultmaven/jobs/run.py`` does exactly this and nothing else — imports the
    container singleton and awaits ``initialize()`` — so this *is* the jobs
    process's knowledge_service, not an approximation of it.
    """
    await reset_container.initialize()

    knowledge_service = reset_container.get_knowledge_service()

    assert knowledge_service is not None
    # The gate ``ingest_runbook`` evaluates is literally
    # ``self._db_session_factory is None``, so a non-None factory means the
    # refusal branch is unreachable for this instance.
    assert getattr(knowledge_service, "_db_session_factory", None) is not None, (
        "The container built KnowledgeService without a db_session_factory. "
        "ingest_runbook refuses in that state, so `kb_seed` fails for every "
        "pack runbook (#894)."
    )
    # Identity, not just truthiness: the production session factory is what
    # binds the RLS tenant scope per transaction.
    assert knowledge_service._db_session_factory is get_db_session


@pytest.mark.unit
@pytest.mark.asyncio
async def test_container_knowledge_service_persistence_paths_are_all_enabled(
    reset_container,
):
    """The factory gates more than ingest.

    ``list_documents``, ``get_document``, delete and the suggestion paths each
    guard on the same attribute and silently degrade (warning + empty result)
    when it is missing. Asserting the single attribute the whole family reads
    keeps this about the property rather than one method.
    """
    await reset_container.initialize()
    service = reset_container.get_knowledge_service()

    factory = getattr(service, "_db_session_factory", None)
    assert factory is not None
    assert callable(factory)


@pytest.mark.unit
def test_knowledge_service_provider_always_wires_the_session_factory():
    """The provider factory itself carries the DB capability.

    The composition root previously relied on an optional parameter it never
    passed, which is how the bug shipped: the default silently produced a
    DB-less service. Real ``FaultMavenSettings`` here — the constructor reads
    them, and a stand-in would let this pass against a dead gate.
    """
    settings = FaultMavenSettings(_env_file=None)

    service = create_knowledge_service(
        vector_store=None,
        knowledge_ingester=None,
        sanitizer=None,
        tracer=None,
        llm_provider=None,
        redis_client=None,
        settings=settings,
    )

    assert service._db_session_factory is get_db_session


@pytest.mark.unit
def test_no_post_construction_session_factory_patch_in_the_composition_root():
    """No process may re-acquire the capability by monkey-patching it on.

    Re-introducing ``knowledge_service._db_session_factory = ...`` in the
    lifespan would once again make the capability web-only and hide a
    regression in the container wiring from every non-web process.
    """
    from pathlib import Path

    main_source = (
        Path(__file__).resolve().parents[3] / "faultmaven" / "main.py"
    ).read_text()

    assert "_db_session_factory =" not in main_source, (
        "main.py assigns _db_session_factory onto a service after "
        "construction. Wire it at the composition root instead, so every "
        "process (web AND jobs) gets a DB-capable service."
    )
