"""A container that cannot compose must name itself under pytest (#823).

The lenient path returns normally with ``_initialized`` still False and the
real error only in captured logs. In a test process that is worse than a
crash: the failure re-surfaces as ``assert False is True`` in whichever test
reads container state next — for #823 that was 15 tests in
``test_container_foundation.py``, hundreds of lines from a ChromaDB client
conflict.

So under pytest the default is to raise, and a test that genuinely wants the
degraded container asks for it by name. The opt-in is scoped to that leniency:
it can never excuse a deployment that ``must_not_degrade``.

Settings here are the REAL ``FaultMavenSettings`` — a stand-in mock's
``must_not_degrade`` is truthy in every mode, which would make the standalone
half of these pairs pass against a dead gate.
"""

from unittest.mock import MagicMock, patch

import pytest

from faultmaven.config.settings import FaultMavenSettings

# Via the package, not `faultmaven._container_impl` directly: the two import
# each other, and entering through the impl module first breaks collection.
from faultmaven.container import DIContainer


def _settings_for(
    monkeypatch, deployment_mode: str, environment: str = "development"
) -> FaultMavenSettings:
    """Real settings object for a mode/environment (no ``.env`` interference)."""
    monkeypatch.setenv("DEPLOYMENT_MODE", deployment_mode)
    monkeypatch.setenv("ENVIRONMENT", environment)
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
    """Patch context: infrastructure + tools succeed, services raise ``exc``."""
    reg_infra = MagicMock(return_value=_awaitable_none())
    return (
        patch("faultmaven._container_impl.get_settings", return_value=settings),
        patch("faultmaven._container_impl.register_infrastructure", new=reg_infra),
        patch("faultmaven._container_impl.register_tools"),
        patch("faultmaven._container_impl.register_services", side_effect=exc),
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_composition_failure_under_pytest_raises_by_default(monkeypatch):
    """Standalone + development — the lenient combination — still raises here."""
    settings = _settings_for(monkeypatch, "standalone")
    assert settings.must_not_degrade is False

    container = _fresh_container()
    boom = ValueError("An instance of Chroma already exists with different settings")

    patches = _compose_failing_at_services(settings, boom)
    with patches[0], patches[1], patches[2], patches[3]:
        with pytest.raises(RuntimeError) as exc:
            await container.initialize()

    # The cause travels with it, so the next reader sees the real error rather
    # than an assertion about `_initialized`.
    assert exc.value.__cause__ is boom
    assert container._initialized is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_composition_failure_names_itself_for_any_error_shape(monkeypatch):
    """Any initialization failure surfaces as itself, not one lucky shape."""
    settings = _settings_for(monkeypatch, "standalone")

    for boom in (
        ImportError("No module named 'workos'"),
        ValueError("chroma client conflict"),
        ConnectionError("redis: connection refused"),
        AttributeError("module has no attribute 'SSOProvider'"),
    ):
        container = _fresh_container()
        patches = _compose_failing_at_services(settings, boom)
        with patches[0], patches[1], patches[2], patches[3]:
            with pytest.raises(RuntimeError) as exc:
                await container.initialize()
        assert exc.value.__cause__ is boom, f"cause lost for {boom!r}"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_allow_degraded_opts_back_into_the_lenient_path(monkeypatch):
    """The opt-in restores the standalone posture #889 preserved."""
    settings = _settings_for(monkeypatch, "standalone")

    container = _fresh_container()
    patches = _compose_failing_at_services(settings, ImportError("no workos"))
    with patches[0], patches[1], patches[2], patches[3]:
        await container.initialize(allow_degraded=True)  # must NOT raise

    assert container._initialized is False
    assert container._initializing is False


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_allow_degraded_does_not_excuse_cloud(monkeypatch):
    """The opt-in is scoped to pytest leniency, never to the mode gate.

    Swept over the deployments that must not degrade, so the scoping is a
    property of ``must_not_degrade`` rather than of one mode.
    """
    for mode, environment in (
        ("cloud", "development"),
        ("cloud", "staging"),
        ("cloud", "production"),
        ("standalone", "production"),
    ):
        settings = _settings_for(monkeypatch, mode, environment)
        assert settings.must_not_degrade is True

        container = _fresh_container()
        # A non-RuntimeError cause, so the RuntimeError can only have come from
        # the refusal and not from the original error passing through.
        patches = _compose_failing_at_services(settings, ImportError("no workos"))
        with patches[0], patches[1], patches[2], patches[3]:
            with pytest.raises(RuntimeError) as exc:
                await container.initialize(allow_degraded=True)

        assert f"DEPLOYMENT_MODE={mode}" in str(exc.value)
        assert container._initialized is False
