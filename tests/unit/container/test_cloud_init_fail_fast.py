"""A container that cannot be composed must not serve under cloud (#885).

``DIContainer.initialize()`` composes in order — infrastructure, tools, then
services — so an exception part-way through leaves every service registered
after the failing line absent. The blanket handler used to swallow that into
``_initialized = False`` and return normally, and the pod kept serving: the #629
flip rehearsal (missing workos SDK, #884) had readiness green and ``/health``
reporting ``healthy`` with the whole service layer gone.

Under ``DEPLOYMENT_MODE=cloud`` the failure must propagate instead, so the
lifespan aborts, uvicorn exits and the pod CrashLoops into a rollout rollback.
Standalone keeps the lenient posture — dev and test ergonomics depend on it.

Settings here are the REAL ``FaultMavenSettings``: a stand-in mock's ``is_cloud``
is truthy in both modes, which would make the standalone half of this pair pass
against a dead gate.
"""

from unittest.mock import MagicMock, patch

import pytest

from faultmaven.config.settings import FaultMavenSettings

# Via the package, not `faultmaven._container_impl` directly: the two import
# each other, and entering through the impl module first breaks collection.
from faultmaven.container import DIContainer


def _settings_for(monkeypatch, deployment_mode: str) -> FaultMavenSettings:
    """Real settings object for a deployment mode (no ``.env`` interference)."""
    monkeypatch.setenv("DEPLOYMENT_MODE", deployment_mode)
    return FaultMavenSettings(_env_file=None)


def _awaitable_none():
    async def _noop(*_args, **_kwargs):
        return None

    return _noop()


def _fresh_container() -> DIContainer:
    container = DIContainer()
    container._initialized = False
    container._initializing = False
    return container


def _compose_failing_at_services(settings: FaultMavenSettings, exc: Exception):
    """Patch context: infrastructure + tools succeed, services raise ``exc``.

    Mirrors the rehearsal shape — ``register_services`` blew up on the SSO
    provider line, after ``register_infrastructure`` had already registered the
    auth stores that kept ``/health`` looking healthy.
    """
    reg_infra = MagicMock(return_value=_awaitable_none())
    return (
        patch("faultmaven._container_impl.get_settings", return_value=settings),
        patch("faultmaven._container_impl.register_infrastructure", new=reg_infra),
        patch("faultmaven._container_impl.register_tools"),
        patch("faultmaven._container_impl.register_services", side_effect=exc),
    )


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_cloud_container_composition_failure_propagates(monkeypatch):
    """Cloud: the composition failure must reach the caller (the lifespan)."""
    settings = _settings_for(monkeypatch, "cloud")
    assert settings.is_cloud is True

    container = _fresh_container()
    boom = ImportError("No module named 'workos'")

    patches = _compose_failing_at_services(settings, boom)
    with patches[0], patches[1], patches[2], patches[3]:
        with pytest.raises(RuntimeError) as exc:
            await container.initialize()

    # RuntimeError is the container's fail-fast channel: the web lifespan
    # re-raises it (startup aborts) and the jobs runner treats it as terminal.
    assert "DEPLOYMENT_MODE=cloud" in str(exc.value)
    assert exc.value.__cause__ is boom
    # Never leaves a half-composed container marked usable.
    assert container._initialized is False
    assert container._initializing is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cloud_fail_fast_holds_for_any_composition_exception(monkeypatch):
    """The guarantee is about composition failing, not about which error it was.

    Sweeps the shapes a broken image actually produces — a missing SDK, a bad
    credential/config, a dead dependency — so the gate cannot be satisfied by
    one instance.
    """
    settings = _settings_for(monkeypatch, "cloud")

    for boom in (
        ImportError("No module named 'workos'"),
        ValueError("WORKOS_API_KEY is empty"),
        ConnectionError("redis: connection refused"),
        AttributeError("module has no attribute 'SSOProvider'"),
    ):
        container = _fresh_container()
        patches = _compose_failing_at_services(settings, boom)
        with patches[0], patches[1], patches[2], patches[3]:
            with pytest.raises(RuntimeError):
                await container.initialize()
        assert container._initialized is False, f"half-composed after {boom!r}"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cloud_fail_fast_not_defeated_by_test_or_skip_escapes(monkeypatch):
    """``SKIP_SERVICE_CHECKS`` / non-production ``ENVIRONMENT`` / running under
    pytest must not buy a cloud pod its way out of the refusal.

    These escapes exist for the standalone degraded path; if they applied to
    cloud the guarantee would be off exactly where it has to hold (this test is
    itself running under pytest, so the ``is_test`` escape is live).
    """
    settings = _settings_for(monkeypatch, "cloud")
    monkeypatch.setenv("SKIP_SERVICE_CHECKS", "true")
    monkeypatch.setenv("ENVIRONMENT", "development")

    container = _fresh_container()
    # A non-RuntimeError cause, so the RuntimeError below can only have come
    # from the cloud refusal and not from the original error passing through.
    patches = _compose_failing_at_services(settings, ImportError("no workos"))
    with patches[0], patches[1], patches[2], patches[3]:
        with pytest.raises(RuntimeError) as exc:
            await container.initialize()

    assert "DEPLOYMENT_MODE=cloud" in str(exc.value)
    assert container._initialized is False


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_cloud_fail_fast_survives_an_unimportable_tenancy_module(monkeypatch):
    """The handler's own imports must not be able to bypass the refusal.

    ``initialize()``'s exception handler imports ``TenancyConfigurationError``
    at runtime, and ``register_services`` imports that same factory inside the
    function — so a tenancy module missing from the image (the #885 trigger
    shape) lands in this handler and would make the handler itself raise
    ``ImportError``, sailing straight past the cloud guard and back into the
    lifespan's degrade path.
    """
    import sys

    settings = _settings_for(monkeypatch, "cloud")
    container = _fresh_container()

    # sys.modules[name] = None makes `from name import X` raise ImportError.
    monkeypatch.setitem(sys.modules, "faultmaven.providers.tenancy.factory", None)

    patches = _compose_failing_at_services(settings, ImportError("no tenancy factory"))
    with patches[0], patches[1], patches[2], patches[3]:
        with pytest.raises(RuntimeError) as exc:
            await container.initialize()

    assert "DEPLOYMENT_MODE=cloud" in str(exc.value)
    assert container._initialized is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_standalone_keeps_lenient_degraded_posture(monkeypatch):
    """Standalone: unchanged. No raise; the container returns marked
    uninitialized so dev/test callers get ``None`` services rather than an
    exploding startup."""
    settings = _settings_for(monkeypatch, "standalone")
    assert settings.is_cloud is False

    container = _fresh_container()
    patches = _compose_failing_at_services(
        settings, ImportError("No module named 'workos'")
    )
    with patches[0], patches[1], patches[2], patches[3]:
        await container.initialize()  # must NOT raise

    assert container._initialized is False
    assert container._initializing is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_standalone_lenient_for_the_same_error_shapes(monkeypatch):
    """The standalone half is pinned across the same sweep, so the pair proves a
    mode split rather than one lucky exception type."""
    settings = _settings_for(monkeypatch, "standalone")

    for boom in (
        ImportError("No module named 'workos'"),
        ValueError("WORKOS_API_KEY is empty"),
        ConnectionError("redis: connection refused"),
        AttributeError("module has no attribute 'SSOProvider'"),
    ):
        container = _fresh_container()
        patches = _compose_failing_at_services(settings, boom)
        with patches[0], patches[1], patches[2], patches[3]:
            await container.initialize()  # must NOT raise
        assert container._initialized is False
