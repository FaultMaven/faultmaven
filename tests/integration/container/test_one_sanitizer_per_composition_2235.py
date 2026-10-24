"""A composition builds one ``DataSanitizer`` and probes Presidio once (#2235).

The container built one from its settings, and three more were built beside it
from the process settings: a ``data_sanitizer`` registration nothing read, the
LLM router's and the knowledge ingester's. With PII sanitizing on and service
checks on, each health-checked both Presidio URLs in its constructor, with a
retry, so a startup with Presidio down waited out four retry delays per URL.

These drive ``container.initialize()``. Constructions are counted on the real
class's ``__init__``; the health check itself is replaced by a recorder, so no
probe leaves the process.
"""

import logging

import pytest

from faultmaven.config.settings import reset_settings
from faultmaven.infrastructure.security.redaction import DataSanitizer
from tests.utils import sanitizer_constructions

pytestmark = [pytest.mark.integration, pytest.mark.security]

SELF_BUILT = "redacting with a sanitizer it built itself"


@pytest.fixture
def service_checks_on(monkeypatch):
    """Service checks and PII sanitizing pinned on, whatever the profile; the
    settings singleton rebuilt from the patched environment and again after."""
    monkeypatch.setenv("SKIP_SERVICE_CHECKS", "false")
    monkeypatch.setenv("PROTECTION_ENABLED", "true")
    monkeypatch.setenv("SANITIZE_PII", "true")
    reset_settings()
    yield
    monkeypatch.undo()
    reset_settings()


@pytest.fixture
def presidio_probes(monkeypatch):
    """Each URL a sanitizer health-checked, in order. Presidio answers down."""
    probed = []

    def _test_service_health(self, service_url: str) -> bool:
        probed.append(service_url)
        return False

    monkeypatch.setattr(DataSanitizer, "_test_service_health", _test_service_health)
    return probed


def _self_built_warnings(caplog):
    return [r for r in caplog.records if SELF_BUILT in r.getMessage()]


@pytest.mark.asyncio
async def test_a_composition_builds_one_sanitizer_and_the_router_holds_it(
    fresh_container, monkeypatch, caplog
):
    """Under the suite's own environment."""
    constructions = sanitizer_constructions(monkeypatch)

    with caplog.at_level(logging.WARNING):
        await fresh_container.initialize()
        router = fresh_container.get_llm_provider()
        held_by_router = router.sanitizer

    sanitizer = fresh_container.get_service("sanitizer")
    assert constructions == [sanitizer]
    assert fresh_container.sanitizer is sanitizer
    assert held_by_router is sanitizer
    assert _self_built_warnings(caplog) == []


@pytest.mark.asyncio
async def test_with_service_checks_on_presidio_is_probed_once_per_url(
    service_checks_on, presidio_probes, fresh_container, monkeypatch, caplog
):
    """The acceptance criterion, and the one composition that builds the
    knowledge ingester: it holds the same instance."""
    constructions = sanitizer_constructions(monkeypatch)

    with caplog.at_level(logging.WARNING):
        await fresh_container.initialize()
        held_by_router = fresh_container.get_llm_provider().sanitizer

    protection = fresh_container.settings.protection
    assert presidio_probes == [
        protection.presidio_analyzer_url,
        protection.presidio_anonymizer_url,
    ]
    sanitizer = fresh_container.get_service("sanitizer")
    assert constructions == [sanitizer]
    ingester = fresh_container.get_service("knowledge_ingester")
    assert ingester is not None
    assert ingester.sanitizer is sanitizer
    assert held_by_router is sanitizer
    assert _self_built_warnings(caplog) == []
