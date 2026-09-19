"""Every component probe must be able to go red.

#1515: seven of the eight checks were `await asyncio.sleep(...)` followed by a
hardcoded HEALTHY, so their `except` arms were unreachable and `/health` could
not report a dependency outage. The guard against that regressing is not "the
check calls something" — it is that breaking the dependency turns the check
red. Each probe below therefore gets a matched pair: a double that works, and
a double that fails the way the real client fails.

The second half of the file pins the fatal/degraded split, because that is
what decides whether a failure reaches a Kubernetes probe as a non-200.
"""

from __future__ import annotations

from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.infrastructure.health.component_monitor import (
    ComponentHealthMonitor,
    HealthStatus,
)

# asyncio_mode = auto, so async tests need no per-test marker.
pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------
# Doubles
# --------------------------------------------------------------------------


class _FakeRegistry:
    """The DI container's service registry, as the probes read it."""

    def __init__(self, failed: Optional[list[tuple[str, str]]] = None):
        self._failed = failed or []

    def get_failed_services(self) -> list[tuple[str, str]]:
        return list(self._failed)


class _FakeContainer:
    """A wired DI container carrying exactly the services a test names."""

    def __init__(self, *, initialized: bool = True, failed=None, **services: Any):
        self._initialized = initialized
        self._registry = _FakeRegistry(failed)
        for name, instance in services.items():
            setattr(self, name, instance)


def _use_container(monkeypatch, container: Optional[Any]) -> None:
    monkeypatch.setattr(
        ComponentHealthMonitor, "_live_container", staticmethod(lambda: container)
    )


def _redis(*, ping_raises: bool = False, dbsize: int = 7) -> Any:
    client = MagicMock()
    client.ping = AsyncMock(
        side_effect=(
            ConnectionError("Error 111 connecting to redis:6379")
            if ping_raises
            else None
        )
    )
    client.dbsize = AsyncMock(return_value=dbsize)
    return client


def _session_store(*, exists_raises: bool = False) -> Any:
    store = MagicMock()
    store.redis_client = _redis()
    if exists_raises:
        store.exists = AsyncMock(side_effect=ConnectionError("Redis operation failed"))
    else:
        store.exists = AsyncMock(return_value=False)
    return store


def _vector_store(*, count_raises: bool = False, count: int = 1297) -> Any:
    store = MagicMock()
    store.collection_name = "faultmaven_kb"
    store.collection = MagicMock()
    store.collection.count = MagicMock(
        side_effect=RuntimeError("Collection does not exist") if count_raises else None,
        return_value=count,
    )
    return store


def _sanitizer(*, probed: bool, analyzer: bool, redacts: bool = True) -> Any:
    sanitizer = MagicMock()
    sanitizer.presidio_probed = probed
    sanitizer.analyzer_available = analyzer
    sanitizer.anonymizer_available = analyzer
    sanitizer.pattern_replacements = [("a", "b")] * 14
    sanitizer.sanitize = MagicMock(
        side_effect=lambda text: "health probe from <IP>" if redacts else text
    )
    return sanitizer


def _router(
    *, providers: list[str], unhealthy: list[str] = (), breaker="closed"
) -> Any:
    router = MagicMock()
    router.registry.get_available_providers = MagicMock(return_value=list(providers))
    router.registry.get_provider_health_summary = MagicMock(
        return_value={
            name: {"health": "unhealthy" if name in unhealthy else "healthy"}
            for name in providers
        }
    )
    router.circuit_breaker.state = breaker
    router.connection_metrics = {
        "total_calls": 12,
        "successful_calls": 11,
        "failed_calls": 1,
    }
    return router


# --------------------------------------------------------------------------
# Redis — the ping either answers or it does not
# --------------------------------------------------------------------------


async def test_redis_healthy_reports_which_backend_answered(monkeypatch):
    _use_container(monkeypatch, _FakeContainer(redis_client=_redis(dbsize=3)))
    out = await ComponentHealthMonitor()._check_redis_health()
    assert out["status"] is HealthStatus.HEALTHY
    assert out["metadata"]["keys"] == 3
    # A silent FakeRedis substitution is the thing worth seeing here.
    assert out["metadata"]["backend"] in {"redis", "fakeredis", "unknown"}


async def test_redis_unhealthy_when_ping_fails(monkeypatch):
    _use_container(monkeypatch, _FakeContainer(redis_client=_redis(ping_raises=True)))
    out = await ComponentHealthMonitor()._check_redis_health()
    assert out["status"] is HealthStatus.UNHEALTHY
    assert "connecting to redis" in out["error"]


async def test_redis_healthy_even_when_dbsize_is_unsupported(monkeypatch):
    """DBSIZE is a nicety; a PING that answered is the health signal."""
    client = _redis()
    client.dbsize = AsyncMock(side_effect=RuntimeError("unsupported"))
    _use_container(monkeypatch, _FakeContainer(redis_client=client))
    out = await ComponentHealthMonitor()._check_redis_health()
    assert out["status"] is HealthStatus.HEALTHY
    assert "keys" not in out["metadata"]


# --------------------------------------------------------------------------
# Session store — one layer above the socket
# --------------------------------------------------------------------------


async def test_session_store_healthy(monkeypatch):
    _use_container(monkeypatch, _FakeContainer(session_store=_session_store()))
    out = await ComponentHealthMonitor()._check_session_store_health()
    assert out["status"] is HealthStatus.HEALTHY


async def test_session_store_unhealthy_when_read_fails(monkeypatch):
    _use_container(
        monkeypatch, _FakeContainer(session_store=_session_store(exists_raises=True))
    )
    out = await ComponentHealthMonitor()._check_session_store_health()
    assert out["status"] is HealthStatus.UNHEALTHY
    assert "Redis operation failed" in out["error"]


async def test_session_store_probe_never_writes(monkeypatch):
    """The probe must be a pure read — a health check that writes is a bug."""
    store = _session_store()
    _use_container(monkeypatch, _FakeContainer(session_store=store))
    await ComponentHealthMonitor()._check_session_store_health()
    store.exists.assert_awaited_once()
    store.set.assert_not_called()
    store.delete.assert_not_called()


# --------------------------------------------------------------------------
# Vector store
# --------------------------------------------------------------------------


async def test_vector_store_healthy_reports_the_real_count(monkeypatch):
    _use_container(monkeypatch, _FakeContainer(vector_store=_vector_store(count=1297)))
    out = await ComponentHealthMonitor()._check_vector_store_health()
    assert out["status"] is HealthStatus.HEALTHY
    assert out["metadata"]["vectors"] == 1297
    assert out["metadata"]["collection"] == "faultmaven_kb"


async def test_vector_store_unhealthy_when_collection_is_gone(monkeypatch):
    _use_container(
        monkeypatch, _FakeContainer(vector_store=_vector_store(count_raises=True))
    )
    out = await ComponentHealthMonitor()._check_vector_store_health()
    assert out["status"] is HealthStatus.UNHEALTHY
    assert "Collection does not exist" in out["error"]


async def test_vector_store_avoids_the_production_circuit_breaker(monkeypatch):
    """A 10s probe must not keep resetting the breaker's failure count.

    `ChromaDBVectorStore.count()` records success/failure on the breaker that
    real traffic shares; the probe therefore reaches the collection directly.
    """
    store = _vector_store()
    store.count = AsyncMock(return_value=1)
    _use_container(monkeypatch, _FakeContainer(vector_store=store))
    await ComponentHealthMonitor()._check_vector_store_health()
    store.count.assert_not_awaited()
    store.collection.count.assert_called_once()


# --------------------------------------------------------------------------
# Sanitizer — a local functional assertion, not an HTTP probe
# --------------------------------------------------------------------------


async def test_sanitizer_unhealthy_when_nothing_is_redacted(monkeypatch):
    _use_container(
        monkeypatch,
        _FakeContainer(sanitizer=_sanitizer(probed=True, analyzer=True, redacts=False)),
    )
    out = await ComponentHealthMonitor()._check_sanitizer_health()
    assert out["status"] is HealthStatus.UNHEALTHY
    assert out["metadata"]["redaction_verified"] is False


async def test_sanitizer_unhealthy_when_redaction_raises(monkeypatch):
    sanitizer = _sanitizer(probed=True, analyzer=True)
    sanitizer.sanitize = MagicMock(side_effect=RuntimeError("pseudonym key missing"))
    _use_container(monkeypatch, _FakeContainer(sanitizer=sanitizer))
    out = await ComponentHealthMonitor()._check_sanitizer_health()
    assert out["status"] is HealthStatus.UNHEALTHY
    assert "pseudonym key missing" in out["error"]


async def test_sanitizer_degraded_only_when_presidio_was_reached_for(monkeypatch):
    _use_container(
        monkeypatch,
        _FakeContainer(sanitizer=_sanitizer(probed=True, analyzer=False)),
    )
    out = await ComponentHealthMonitor()._check_sanitizer_health()
    assert out["status"] is HealthStatus.DEGRADED


async def test_sanitizer_healthy_when_presidio_is_not_configured(monkeypatch):
    """Standalone never runs Presidio. Reporting it degraded forever is noise.

    This is the cry-wolf guard: a signal that is always amber is a signal
    nobody reads, and #1515 is about signals nobody can read.
    """
    _use_container(
        monkeypatch,
        _FakeContainer(sanitizer=_sanitizer(probed=False, analyzer=False)),
    )
    out = await ComponentHealthMonitor()._check_sanitizer_health()
    assert out["status"] is HealthStatus.HEALTHY
    assert out["metadata"]["presidio_configured"] is False
    assert "presidio_analyzer" not in out["metadata"]


# --------------------------------------------------------------------------
# LLM provider — local state only; no probe may spend a completion
# --------------------------------------------------------------------------


async def test_llm_provider_healthy_lists_the_real_providers(monkeypatch):
    _use_container(
        monkeypatch,
        _FakeContainer(llm_provider=_router(providers=["gemini", "openai"])),
    )
    out = await ComponentHealthMonitor()._check_llm_provider_health()
    assert out["status"] is HealthStatus.HEALTHY
    assert out["metadata"]["available_providers"] == ["gemini", "openai"]
    assert out["metadata"]["total_calls"] == 12


async def test_llm_provider_unhealthy_when_nothing_is_configured(monkeypatch):
    _use_container(monkeypatch, _FakeContainer(llm_provider=_router(providers=[])))
    out = await ComponentHealthMonitor()._check_llm_provider_health()
    assert out["status"] is HealthStatus.UNHEALTHY
    assert "no LLM provider" in out["error"]


async def test_llm_provider_degraded_when_the_breaker_is_open(monkeypatch):
    _use_container(
        monkeypatch,
        _FakeContainer(llm_provider=_router(providers=["gemini"], breaker="open")),
    )
    out = await ComponentHealthMonitor()._check_llm_provider_health()
    assert out["status"] is HealthStatus.DEGRADED


async def test_llm_provider_degraded_when_a_provider_is_marked_unhealthy(monkeypatch):
    _use_container(
        monkeypatch,
        _FakeContainer(
            llm_provider=_router(providers=["gemini", "openai"], unhealthy=["openai"])
        ),
    )
    out = await ComponentHealthMonitor()._check_llm_provider_health()
    assert out["status"] is HealthStatus.DEGRADED
    assert out["metadata"]["unhealthy_providers"] == ["openai"]


async def test_llm_provider_probe_never_generates(monkeypatch):
    """The only real connectivity test in this codebase is a billed call."""
    router = _router(providers=["gemini"])
    _use_container(monkeypatch, _FakeContainer(llm_provider=router))
    await ComponentHealthMonitor()._check_llm_provider_health()
    router.generate.assert_not_called()
    router.route.assert_not_called()


# --------------------------------------------------------------------------
# Knowledge base + tracer
# --------------------------------------------------------------------------


class _Session:
    def __init__(self, dialect: str, count: int = 91, raises: bool = False):
        self._dialect = dialect
        self._count = count
        self._raises = raises
        self.scalar_calls = 0
        self.execute_calls = 0

    async def scalar(self, *_a, **_k):
        self.scalar_calls += 1
        if self._raises:
            raise RuntimeError("no such table: knowledge_items")
        return self._count

    async def execute(self, *_a, **_k):
        self.execute_calls += 1
        if self._raises:
            raise RuntimeError("no such table: knowledge_items")
        return MagicMock()

    def get_bind(self):
        bind = MagicMock()
        bind.dialect.name = self._dialect
        return bind


class _ACM:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *_a):
        return False


def _patch_session(monkeypatch, session) -> None:
    monkeypatch.setattr(
        "faultmaven.infrastructure.persistence.database.get_db_session",
        lambda *a, **k: _ACM(session),
    )


async def test_knowledge_base_reports_the_count_where_it_is_the_whole_truth(
    monkeypatch,
):
    _patch_session(monkeypatch, _Session("sqlite", count=91))
    out = await ComponentHealthMonitor()._check_knowledge_base_health()
    assert out["status"] is HealthStatus.HEALTHY
    assert out["metadata"]["knowledge_items"] == 91


async def test_knowledge_base_omits_an_rls_scoped_count(monkeypatch):
    """An unscoped PostgreSQL session sees an empty tenant.

    Publishing that zero would be a new fabrication in place of the old one —
    and paying for the COUNT(*) only to discard it would be worse than both,
    at six probes a minute per pod.
    """
    session = _Session("postgresql", count=0)
    _patch_session(monkeypatch, session)
    out = await ComponentHealthMonitor()._check_knowledge_base_health()
    assert out["status"] is HealthStatus.HEALTHY
    assert "knowledge_items" not in out["metadata"]
    assert session.scalar_calls == 0, "must not pay for a count it cannot report"
    assert session.execute_calls == 1, "but must still prove the table is reachable"


async def test_knowledge_base_unhealthy_when_the_table_is_unreachable(monkeypatch):
    _patch_session(monkeypatch, _Session("sqlite", raises=True))
    out = await ComponentHealthMonitor()._check_knowledge_base_health()
    assert out["status"] is HealthStatus.UNHEALTHY
    assert "knowledge_items" in out["error"]


async def test_tracer_degraded_when_enabled_but_recording_nothing(monkeypatch):
    monkeypatch.setattr(
        "faultmaven.infrastructure.observability.tracing.tracing_is_effective",
        lambda: False,
    )
    settings = MagicMock()
    settings.observability.opik_enabled = True
    monkeypatch.setattr("faultmaven.config.settings.get_settings", lambda: settings)
    _use_container(monkeypatch, None)
    out = await ComponentHealthMonitor()._check_tracer_health()
    assert out["status"] is HealthStatus.DEGRADED


async def test_tracer_healthy_when_tracing_is_deliberately_off(monkeypatch):
    monkeypatch.setattr(
        "faultmaven.infrastructure.observability.tracing.tracing_is_effective",
        lambda: False,
    )
    settings = MagicMock()
    settings.observability.opik_enabled = False
    monkeypatch.setattr("faultmaven.config.settings.get_settings", lambda: settings)
    _use_container(monkeypatch, None)
    out = await ComponentHealthMonitor()._check_tracer_health()
    assert out["status"] is HealthStatus.HEALTHY


# --------------------------------------------------------------------------
# No probe may report HEALTHY without evidence
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "check",
    [
        "_check_llm_provider_health",
        "_check_session_store_health",
        "_check_vector_store_health",
        "_check_redis_health",
        "_check_sanitizer_health",
    ],
)
async def test_container_backed_probes_report_unknown_before_wiring(monkeypatch, check):
    """ "Nothing is wired yet" is UNKNOWN, never HEALTHY."""
    _use_container(monkeypatch, None)
    out = await getattr(ComponentHealthMonitor(), check)()
    assert out["status"] is HealthStatus.UNKNOWN


async def test_an_unregistered_component_is_unknown_not_healthy():
    """The old generic check returned HEALTHY for any name at all."""
    out = await ComponentHealthMonitor()._generic_health_check("nonexistent")
    assert out["status"] is HealthStatus.UNKNOWN


async def test_a_failed_service_is_unhealthy_and_a_disabled_one_is_degraded(
    monkeypatch,
):
    _use_container(
        monkeypatch,
        _FakeContainer(vector_store=None, failed=[("vector_store", "boom")]),
    )
    out = await ComponentHealthMonitor()._check_vector_store_health()
    assert out["status"] is HealthStatus.UNHEALTHY
    assert out["error"] == "boom"

    _use_container(monkeypatch, _FakeContainer(vector_store=None))
    out = await ComponentHealthMonitor()._check_vector_store_health()
    assert out["status"] is HealthStatus.DEGRADED


# --------------------------------------------------------------------------
# Fatal vs degraded — what actually reaches a Kubernetes probe
# --------------------------------------------------------------------------


def test_only_the_database_is_fatal_to_serving():
    """Widening this set changes when production pods leave their Service.

    It is not a detail of this module: a component added here stops traffic
    to a pod that might still have served the request. Change it knowingly.
    """
    assert ComponentHealthMonitor().fatal_components == {"database"}


def test_a_degraded_non_fatal_component_does_not_make_the_system_unhealthy():
    monitor = ComponentHealthMonitor()
    monitor.component_health["tracer"].status = HealthStatus.DEGRADED
    for name, health in monitor.component_health.items():
        if name != "tracer":
            health.status = HealthStatus.HEALTHY

    status, summary = monitor.get_overall_health_status()
    assert status is HealthStatus.DEGRADED
    assert summary["fatal_unhealthy"] == []


def test_an_unhealthy_non_fatal_component_is_degraded_not_unhealthy():
    """A dead vector store degrades answers; it does not stop the service."""
    monitor = ComponentHealthMonitor()
    for health in monitor.component_health.values():
        health.status = HealthStatus.HEALTHY
    monitor.component_health["vector_store"].status = HealthStatus.UNHEALTHY

    status, summary = monitor.get_overall_health_status()
    assert status is HealthStatus.DEGRADED
    assert summary["fatal_unhealthy"] == []


def test_an_unhealthy_fatal_component_is_unhealthy():
    monitor = ComponentHealthMonitor()
    for health in monitor.component_health.values():
        health.status = HealthStatus.HEALTHY
    monitor.component_health["database"].status = HealthStatus.UNHEALTHY

    status, summary = monitor.get_overall_health_status()
    assert status is HealthStatus.UNHEALTHY
    assert summary["fatal_unhealthy"] == ["database"]


def test_a_degraded_fatal_component_still_serves():
    """The database reports DEGRADED for an RLS-bypassing role.

    That is a tenant-isolation finding, not an inability to answer requests,
    and it must not pull the pod.
    """
    monitor = ComponentHealthMonitor()
    for health in monitor.component_health.values():
        health.status = HealthStatus.HEALTHY
    monitor.component_health["database"].status = HealthStatus.DEGRADED

    status, summary = monitor.get_overall_health_status()
    assert status is HealthStatus.DEGRADED
    assert summary["fatal_unhealthy"] == []


def test_an_undeterminable_component_is_never_fatal():
    monitor = ComponentHealthMonitor()
    for health in monitor.component_health.values():
        health.status = HealthStatus.HEALTHY
    monitor.component_health["database"].status = HealthStatus.UNKNOWN

    status, summary = monitor.get_overall_health_status()
    assert status is HealthStatus.DEGRADED
    assert summary["fatal_unhealthy"] == []


async def test_readiness_only_probes_the_fatal_set(monkeypatch):
    """Readiness must not pay for, or be gated by, a degrading dependency."""
    monitor = ComponentHealthMonitor()
    probed: list[str] = []

    async def _record(name: str):
        probed.append(name)
        health = monitor.component_health[name]
        health.status = HealthStatus.HEALTHY
        return health

    monkeypatch.setattr(monitor, "check_component_health", _record)
    ready, detail = await monitor.check_serving_readiness()
    assert ready is True
    assert probed == ["database"]
    assert detail["blocking"] == []


async def test_readiness_blocks_on_an_unhealthy_fatal_component(monkeypatch):
    monitor = ComponentHealthMonitor()

    async def _unhealthy(name: str):
        health = monitor.component_health[name]
        health.status = HealthStatus.UNHEALTHY
        return health

    monkeypatch.setattr(monitor, "check_component_health", _unhealthy)
    ready, detail = await monitor.check_serving_readiness()
    assert ready is False
    assert detail["blocking"] == ["database"]


# --------------------------------------------------------------------------
# The 24h figures measure the window, not the lifetime
# --------------------------------------------------------------------------


async def test_probe_counts_expire_with_the_history_window(monkeypatch):
    """They used to be monotonic counters labelled "24h"."""
    from datetime import datetime, timedelta, timezone

    monitor = ComponentHealthMonitor()
    monkeypatch.setattr(
        monitor,
        "_perform_health_check",
        AsyncMock(return_value={"status": HealthStatus.HEALTHY, "metadata": {}}),
    )

    stale = datetime.now(timezone.utc) - timedelta(hours=48)
    monitor.health_history["database"] = [
        (stale, HealthStatus.UNHEALTHY, 1.0) for _ in range(50)
    ]

    health = await monitor.check_component_health("database")
    assert health.probe_failures_24h == 0
    assert health.probe_successes_24h == 1
    assert health.probe_availability_24h == 100.0


async def test_a_degraded_probe_counts_as_available(monkeypatch):
    """Availability is "did it answer"; degraded-ness is a separate axis."""
    monitor = ComponentHealthMonitor()
    monkeypatch.setattr(
        monitor,
        "_perform_health_check",
        AsyncMock(return_value={"status": HealthStatus.DEGRADED, "metadata": {}}),
    )
    health = await monitor.check_component_health("tracer")
    assert health.probe_availability_24h == 100.0
    assert health.probe_failures_24h == 0


async def test_a_raising_probe_is_recorded_as_a_failure(monkeypatch):
    monitor = ComponentHealthMonitor()
    monkeypatch.setattr(
        monitor, "_perform_health_check", AsyncMock(side_effect=RuntimeError("down"))
    )
    health = await monitor.check_component_health("redis")
    assert health.status is HealthStatus.UNHEALTHY
    assert health.probe_failures_24h == 1
    assert health.probe_availability_24h == 0.0


async def test_metadata_does_not_outlive_the_probe_that_reported_it(monkeypatch):
    """A stale key served as current is the same class of bug as a constant."""
    monitor = ComponentHealthMonitor()
    monkeypatch.setattr(
        monitor,
        "_perform_health_check",
        AsyncMock(
            return_value={"status": HealthStatus.HEALTHY, "metadata": {"vectors": 5}}
        ),
    )
    await monitor.check_component_health("vector_store")
    assert monitor.component_health["vector_store"].metadata == {"vectors": 5}

    monkeypatch.setattr(
        monitor,
        "_perform_health_check",
        AsyncMock(return_value={"status": HealthStatus.UNHEALTHY, "metadata": {}}),
    )
    await monitor.check_component_health("vector_store")
    assert monitor.component_health["vector_store"].metadata == {}
