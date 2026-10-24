"""The knowledge ingester redacts with the composition's sanitizer (#2235).

``KnowledgeIngester.__init__`` used to build a ``DataSanitizer()`` of its own,
from the process settings rather than the ones it was given, and health-check
Presidio for it. Its factory now hands it the container's instance.

The factory returns ``None`` under ``SKIP_SERVICE_CHECKS`` before building
anything, so these run with checks off and standalone ChromaDB settings (a
``PersistentClient`` under ``tmp_path``), as ``test_no_eager_embedding_load``
does.
"""

import logging

import pytest

from faultmaven.container import DIContainer
from faultmaven.container.errors import ServiceUnavailableError
from faultmaven.container.providers import tools
from faultmaven.container.providers.tools import (
    create_knowledge_ingester,
    register_tools,
)
from faultmaven.container.registry import DependencyRegistry
from faultmaven.modules.knowledge.domain.services.ingestion import KnowledgeIngester
from tests.unit.container.conftest import make_chroma_settings
from tests.utils import sanitizer_constructions

SELF_BUILT = "redacting with a sanitizer it built itself"

pytestmark = [pytest.mark.unit, pytest.mark.security]


def _standalone_settings(tmp_path):
    settings = make_chroma_settings(skip_service_checks=False)
    settings.database.chromadb_kb_persist_dir = str(tmp_path / "chroma-kb")
    return settings


def test_the_factory_hands_the_ingester_the_composition_sanitizer(
    monkeypatch, tmp_path
):
    constructions = sanitizer_constructions(monkeypatch)
    composed = object()

    ingester = create_knowledge_ingester(_standalone_settings(tmp_path), composed)

    assert ingester is not None
    assert ingester.sanitizer is composed
    assert constructions == [], "the ingester built a sanitizer of its own"


def test_an_ingester_given_none_builds_one_from_its_own_settings(
    monkeypatch, tmp_path, caplog
):
    """Built directly (a script, a test), it still redacts, configured by the
    settings it was handed rather than the process singleton, and the second
    construction is not silent."""
    constructions = sanitizer_constructions(monkeypatch)
    settings = _standalone_settings(tmp_path)

    with caplog.at_level(logging.WARNING):
        ingester = KnowledgeIngester(settings=settings)

    assert constructions == [ingester.sanitizer]
    assert ingester.sanitizer._settings is settings
    assert [r.levelno for r in caplog.records if SELF_BUILT in r.getMessage()] == [
        logging.WARNING
    ]


class _FactoryReached(Exception):
    pass


def test_register_tools_refuses_a_container_with_no_sanitizer(monkeypatch, tmp_path):
    """A miss raises, rather than handing the factory ``None``, from which the
    ingester would build a sanitizer of its own."""

    def _factory(settings, sanitizer):
        raise _FactoryReached(f"the factory was handed {sanitizer!r}")

    monkeypatch.setattr(tools, "create_knowledge_ingester", _factory)
    container = object.__new__(DIContainer)  # private: not the singleton
    container._registry = DependencyRegistry()
    container._logger = logging.getLogger("test.2235")
    container.settings = _standalone_settings(tmp_path)

    with pytest.raises(ServiceUnavailableError, match="'sanitizer'"):
        register_tools(container)
