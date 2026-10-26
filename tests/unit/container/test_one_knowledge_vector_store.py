"""A composed container holds one KB vector store, and every consumer gets it.

``register_tools`` used to build a second ``KnowledgeVectorStore`` and
overwrite ``container.knowledge_vector_store`` with it, so ``kb_qa`` and
``KnowledgeService`` shared that second store while the one
``register_infrastructure`` registered as ``knowledge_vector_store`` went
unused. Each store holds its own term-stats index and circuit breaker (the
blocking executor is module-wide), so two would hold two of each; the orphan
had no reader, so nothing read its index.

The three providers run here as composition runs them, in order, against a
private container. Stubs stand in only at the boundaries: the infrastructure
services that own a socket or a model, and the ChromaDB client (a stand-in that
is not an HTTP client, so the store runs no blocking executor and touches
nothing at construction). ``SKIP_SERVICE_CHECKS`` is off, because under it no store and no
Q&A tool is built and every identity below would hold vacuously.
"""

from __future__ import annotations

import logging

import pytest

from faultmaven.config.settings import FaultMavenSettings
from faultmaven.container import DIContainer
from faultmaven.container.providers import tools
from faultmaven.container.providers.infrastructure import (
    register_knowledge_vector_store,
)
from faultmaven.container.providers.services import register_services
from faultmaven.container.registry import DependencyRegistry
from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
    KnowledgeVectorStore,
)
from faultmaven.infrastructure.persistence.case_vector_store import CaseVectorStore

pytestmark = pytest.mark.unit

#: The infrastructure the tools and service layers read out of the container
#: (as in ``test_service_composition.py``).
BOUNDARY_SERVICES = (
    "session_store",
    "vector_store",
    "llm_provider",
    "data_classifier",
    "log_processor",
    "sanitizer",
    "tracer",
    "preprocessing_service",
    "file_storage_service",
)


def _settings(monkeypatch) -> FaultMavenSettings:
    monkeypatch.setenv("DEPLOYMENT_MODE", "standalone")
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("TENANT_PROVIDER", "single")
    settings = FaultMavenSettings(_env_file=None)
    settings.server.skip_service_checks = False
    settings.protection.protection_enabled = False
    settings.protection.sanitize_pii = False
    return settings


def _container(settings: FaultMavenSettings) -> DIContainer:
    """A real container off the singleton, with its boundary services stubbed."""
    container = object.__new__(DIContainer)
    container._initialized = False
    container._initializing = True
    container._registry = DependencyRegistry()
    container._logger = logging.getLogger("test.one_kb_store")
    container.settings = settings
    for name in BOUNDARY_SERVICES:
        container._register_service(name, object())
    container.redis_client = None
    container.case_repository = None
    container.user_store = object()
    return container


def test_every_consumer_gets_the_registered_store(monkeypatch):
    container = _container(_settings(monkeypatch))
    case_store = CaseVectorStore(client=object())
    container._register_service("case_vector_store", case_store)
    container.case_vector_store = case_store

    register_knowledge_vector_store(container, object())
    tools.register_tools(container)
    register_services(container)

    store = container.get_service("knowledge_vector_store")
    assert isinstance(store, KnowledgeVectorStore)
    assert container.knowledge_vector_store is store
    assert container.get_service("knowledge_service")._vector_store is store
    assert container.kb_qa_tool._vector_store is store
    # The case tool is given the case store, not a KB store.
    assert container.case_evidence_qa_tool._vector_store is case_store
