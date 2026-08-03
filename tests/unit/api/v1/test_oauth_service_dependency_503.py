"""OAuth enabled but unwired must answer 503, not silently resolve to None.

Root cause guarded here: ``get_oauth_service`` documents

    Raises:
        HTTPException: If OAuth is enabled but service unavailable

and raises ``HTTPException(503, "OAuth authentication not configured")`` to
honour it — but the raise sits inside a ``try`` whose only handler is
``except Exception: pass``. That handler exists to tolerate ``get_settings()``
being unavailable; it also swallowed the dependency's own refusal, so the
function fell through to ``return oauth_service`` and handed callers ``None``.

The contract in the docstring was therefore never enforced. Instead of a clean
503 naming the misconfiguration, every OAuth route resolved its service to
``None`` and failed later on attribute access — a 500 pointing at the route
rather than at the missing wiring.

Reachability is a real deployment state, not a contrivance:
``validate_oauth_consistency`` forces ``oauth_enabled=True`` whenever
``auth_mode=oauth``, while the container only assigns ``container.oauth_service``
``if user_store:`` — otherwise it logs "OAuth service skipped (no user_store
available)" and ``app.state.oauth_service`` stays ``None``. That is exactly
``oauth_enabled and oauth_service is None``.

Driven through a real request over ``httpx.ASGITransport`` so the dependency is
resolved by FastAPI the way a route resolves it.
"""

from types import SimpleNamespace

import httpx
import pytest
from fastapi import Depends, FastAPI

import faultmaven.config.settings as settings_module
from faultmaven.api.v1.dependencies import get_oauth_service


def _build_app(oauth_service):
    app = FastAPI()
    app.state.oauth_service = oauth_service

    @app.get("/probe")
    async def probe(service=Depends(get_oauth_service)):
        return {"resolved": service is not None}

    return app


def _patch_settings(monkeypatch, *, oauth_enabled):
    """Point the dependency's late-imported get_settings at a stub.

    The import happens inside the function body, so patching the attribute on
    the settings module is what the call actually resolves. The module object
    itself is left alone — replacing it would create a second settings
    singleton.
    """
    stub = SimpleNamespace(auth=SimpleNamespace(oauth_enabled=oauth_enabled))
    monkeypatch.setattr(settings_module, "get_settings", lambda: stub)


async def _probe(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get("/probe")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_oauth_enabled_without_service_surfaces_as_503(monkeypatch):
    """The dependency's own 503 must reach the client, not be swallowed."""
    _patch_settings(monkeypatch, oauth_enabled=True)

    response = await _probe(_build_app(oauth_service=None))

    # Exact status. Before the fix this was 200 {"resolved": false} — the
    # refusal vanished and the route ran with a None service.
    assert response.status_code == 503, response.text
    assert response.json()["detail"] == "OAuth authentication not configured"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_oauth_enabled_with_service_resolves(monkeypatch):
    """Vacuity control: a wired service resolves over the same app/path."""
    _patch_settings(monkeypatch, oauth_enabled=True)

    response = await _probe(_build_app(oauth_service=SimpleNamespace(name="oauth")))

    assert response.status_code == 200, response.text
    assert response.json() == {"resolved": True}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_oauth_disabled_without_service_is_not_an_error(monkeypatch):
    """The re-raise must not turn the supported local-mode state into a 503.

    With OAuth disabled, an absent service is normal (self-hosted / dev-login),
    so the dependency must still resolve to None rather than refusing.
    """
    _patch_settings(monkeypatch, oauth_enabled=False)

    response = await _probe(_build_app(oauth_service=None))

    assert response.status_code == 200, response.text
    assert response.json() == {"resolved": False}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unavailable_settings_still_degrade_to_none(monkeypatch):
    """The tolerated failure the bare handler was written for must survive.

    ``except Exception: pass`` exists so a settings lookup that blows up
    degrades to None instead of failing the request. Adding the HTTPException
    re-raise must not narrow that.
    """

    def _boom():
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(settings_module, "get_settings", _boom)

    response = await _probe(_build_app(oauth_service=None))

    assert response.status_code == 200, response.text
    assert response.json() == {"resolved": False}
