"""A composition-root failure must not reach a serving pod (#890, #891).

``DIContainer.initialize()`` is only the first step of the composition root:
bootstrap, the RLS role gate and the whole ``app.state`` wiring cascade run
after it, inside the same startup path. A failure in any of them leaves the
application serving routes whose services are absent — the #885 shape, one
layer out.

Two things are pinned here.

**The gate.** The degrade decision keys on ``settings.must_not_degrade``, not on
``ENVIRONMENT`` alone. The rehearsal overlay runs ``DEPLOYMENT_MODE=cloud`` with
``ENVIRONMENT=staging``, so an ``ENVIRONMENT``-only gate — what the lifespan
used to have — reads "not production" and serves a partial API. The matrix below
sweeps both axes precisely because a cloud+production-only test passes against
that bug.

**The channel.** A ``RuntimeError`` out of the composition root is terminal in
every mode: the container's cloud refusal, the bootstrap failure and the RLS
role gate all raise it, having already decided the boot cannot continue. The
jobs analogue is pinned by
``test_jobs_entrypoints.py::test_container_init_runtime_error_is_terminal``;
this is the web one.

Settings are the REAL ``FaultMavenSettings``: a stand-in mock's
``must_not_degrade`` is truthy in every mode, which would make the
must-not-refuse half of the matrix pass against a dead gate.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI

from faultmaven.config.settings import FaultMavenSettings
from faultmaven.main import compose_application

# The deployments that must refuse a partial start, and those that may serve
# one. Cloud refuses whatever ENVIRONMENT says (the rehearsal ran staging);
# a self-hosted instance may serve partially unless its operator declared
# ENVIRONMENT=production.
GATE_MATRIX = [
    ("cloud", "production", True),
    ("cloud", "staging", True),
    ("cloud", "development", True),
    ("standalone", "production", True),
    ("standalone", "staging", False),
    ("standalone", "development", False),
]

# The shapes an ``app.state`` wiring failure actually takes: a getter for a
# service the container never registered (AttributeError — what the old blanket
# handler swallowed by name), a misconfigured collaborator, a dead dependency.
WIRING_FAILURES = [
    AttributeError("'DIContainer' object has no attribute 'get_case_service'"),
    ValueError("TENANT_PROVIDER=multi requires an organization"),
    ConnectionError("postgres: connection refused"),
]


def _settings_for(
    monkeypatch, deployment_mode: str, environment: str
) -> FaultMavenSettings:
    """Real settings object for a mode/environment (no ``.env`` interference)."""
    monkeypatch.setenv("DEPLOYMENT_MODE", deployment_mode)
    monkeypatch.setenv("ENVIRONMENT", environment)
    return FaultMavenSettings(_env_file=None)


def _failing_wiring(exc: Exception):
    return patch(
        "faultmaven.main._wire_composition_root", new=AsyncMock(side_effect=exc)
    )


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
@pytest.mark.parametrize("deployment_mode,environment,must_refuse", GATE_MATRIX)
async def test_wiring_failure_refuses_exactly_where_it_must(
    monkeypatch, deployment_mode, environment, must_refuse
):
    """Sweep the full {mode} x {environment} matrix against a wiring failure."""
    settings = _settings_for(monkeypatch, deployment_mode, environment)
    assert settings.must_not_degrade is must_refuse

    for boom in WIRING_FAILURES:
        app = FastAPI()
        with _failing_wiring(boom):
            if must_refuse:
                with pytest.raises(RuntimeError) as exc:
                    await compose_application(app, settings)
                # A non-RuntimeError cause, so the refusal is the only thing
                # that could have produced this.
                assert exc.value.__cause__ is boom
            else:
                await compose_application(app, settings)  # must NOT raise


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("deployment_mode,environment,_must_refuse", GATE_MATRIX)
async def test_healthy_composition_starts_in_every_deployment(
    monkeypatch, deployment_mode, environment, _must_refuse
):
    """The gate only decides failures — a clean composition starts everywhere."""
    settings = _settings_for(monkeypatch, deployment_mode, environment)

    app = FastAPI()
    with patch("faultmaven.main._wire_composition_root", new=AsyncMock()) as wired:
        await compose_application(app, settings)

    wired.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
@pytest.mark.parametrize("deployment_mode,environment,_must_refuse", GATE_MATRIX)
async def test_runtime_error_is_terminal_in_every_deployment(
    monkeypatch, deployment_mode, environment, _must_refuse
):
    """A ``RuntimeError`` from the composition root aborts startup everywhere.

    It is the channel the container's cloud refusal (#885/#889) travels on, so
    a mode that tolerated it would put the rehearsal pod back to serving
    partially with every container-side test still green.
    """
    settings = _settings_for(monkeypatch, deployment_mode, environment)

    app = FastAPI()
    boom = RuntimeError("DI Container initialization failed under cloud")
    with _failing_wiring(boom):
        with pytest.raises(RuntimeError) as exc:
            await compose_application(app, settings)

    # Re-raised as itself, not re-wrapped — the callee's message is the one an
    # operator needs to see in the CrashLoop.
    assert exc.value is boom


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_a_missing_service_propagates_out_of_the_wiring(monkeypatch):
    """The wiring cascade itself must not swallow a missing service.

    A getter for a service the container never registered raises
    ``AttributeError`` — which a blanket handler around the cascade catches by
    name, logs as "some optional services not available", and continues from.
    That is the #885 shape one layer out: the route is mounted, the service is
    absent, and the pod reports healthy. The decision belongs to
    ``compose_application``, so the failure has to reach it.
    """
    from faultmaven.main import _wire_composition_root

    settings = _settings_for(monkeypatch, "standalone", "development")

    container = MagicMock()
    container.initialize = AsyncMock()
    boom = AttributeError("'DIContainer' object has no attribute 'case_service'")
    container.get_case_service.side_effect = boom

    with (
        patch("faultmaven.container.container", container),
        patch("faultmaven.bootstrap.startup.bootstrap_application", new=AsyncMock()),
        patch(
            "faultmaven.infrastructure.persistence.rls_role_guard."
            "assert_app_db_role_enforces_rls",
            new=AsyncMock(),
        ),
        patch(
            "faultmaven.providers.tenancy.factory.requested_tenant_provider",
            return_value="single",
        ),
    ):
        with pytest.raises(AttributeError) as exc:
            await _wire_composition_root(FastAPI(), settings)

    assert exc.value is boom


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_container_runtime_error_aborts_the_web_lifespan(monkeypatch):
    """End to end: the raise must escape ``lifespan``, so uvicorn exits.

    #889's guarantee rests on this link, and it holds for the standalone
    development default — the most lenient configuration there is — because
    the lifespan has no handler of its own left to reinterpret it.
    """
    monkeypatch.setenv("DEPLOYMENT_MODE", "standalone")
    monkeypatch.setenv("ENVIRONMENT", "development")

    from faultmaven.main import lifespan

    app = FastAPI()
    boom = RuntimeError("DI Container initialization failed")
    with _failing_wiring(boom):
        with pytest.raises(RuntimeError) as exc:
            async with lifespan(app):
                pytest.fail("lifespan must not yield after a composition failure")

    assert exc.value is boom
