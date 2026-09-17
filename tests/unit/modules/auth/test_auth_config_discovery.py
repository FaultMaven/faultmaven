"""Unit tests for auth configuration discovery (`GET /auth/config`).

The dashboard decides its sign-in flow from this endpoint. In cloud/oauth
deployments the human sign-in target is `oauth.hosted_login_url` — the hosted
SSO login entry point (ADR-015 D3) — which must be advertised only when SSO is
actually configured (the same `sso_configured` gate that mounts the SSO
router), and must never fall back to `authorize_url` (the copilot OAuth-PKCE
machine flow).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from faultmaven.config.settings import AuthMode
from faultmaven.modules.auth.api import auth as auth_routes

pytestmark = pytest.mark.asyncio


def _settings_patch(auth_mode: AuthMode, sso_configured: bool = False):
    settings_stub = SimpleNamespace(
        auth=SimpleNamespace(auth_mode=auth_mode, sso_configured=sso_configured)
    )
    return patch.object(auth_routes, "get_settings", return_value=settings_stub)


async def test_local_mode_reports_local_flow_without_oauth():
    with _settings_patch(AuthMode.LOCAL):
        config = await auth_routes.get_auth_config()

    assert config.auth_mode == "local"
    assert config.login_endpoint == "/api/v1/auth/login"
    assert config.supports_registration is True
    assert config.oauth is None


async def test_oauth_mode_without_sso_advertises_no_hosted_login():
    with _settings_patch(AuthMode.OAUTH, sso_configured=False):
        config = await auth_routes.get_auth_config()

    assert config.auth_mode == "oauth"
    assert config.oauth is not None
    # Honest "not configured" state: the dashboard must not get a login URL
    # that would redirect users into a 404 (the SSO router is unmounted).
    assert config.oauth.hosted_login_url is None
    # The machine-flow endpoint is still advertised for the copilot.
    assert config.oauth.authorize_url == "/auth/oauth/authorize"


async def test_oauth_mode_with_sso_advertises_hosted_login_url():
    with _settings_patch(AuthMode.OAUTH, sso_configured=True):
        config = await auth_routes.get_auth_config()

    assert config.auth_mode == "oauth"
    assert config.oauth is not None
    # Relative path — the dashboard resolves it against its API origin. Must
    # match the mounted SSO router path (main.py mounts modules/auth/api/sso.py
    # under /api/v1).
    assert config.oauth.hosted_login_url == "/api/v1/auth/sso/login"
    # Distinct from the copilot PKCE authorize endpoint.
    assert config.oauth.hosted_login_url != config.oauth.authorize_url


async def test_oauth_mode_with_sso_advertises_the_screen_hint_capability():
    """A client gates its sign-up control on this, not on a version.

    Without it the Dashboard shipped a "Create an account" button against an
    API that accepted `screen_hint`, dropped it, and served the sign-in
    screen — two buttons doing the same thing, with nothing able to notice.
    """
    with _settings_patch(AuthMode.OAUTH, sso_configured=True):
        config = await auth_routes.get_auth_config()

    assert config.oauth.supports_screen_hint is True


async def test_oauth_mode_without_sso_advertises_no_screen_hint_capability():
    """Nothing to hint at when there is no hosted login to hint to."""
    with _settings_patch(AuthMode.OAUTH, sso_configured=False):
        config = await auth_routes.get_auth_config()

    assert config.oauth.hosted_login_url is None
    assert config.oauth.supports_screen_hint is False


async def test_screen_hint_capability_defaults_to_false_when_absent():
    """An older API sends no such field, and that must read as "no".

    Declared with a `False` default rather than `Optional[bool]` for exactly
    this: a client treating a missing value as unknown-therefore-fine would
    show the dead control again. Asserted on the model so the default cannot
    be loosened to None without this failing.
    """
    from faultmaven.modules.auth.api.auth import OAuthConfigResponse

    parsed = OAuthConfigResponse.model_validate(
        {
            "authorize_url": "/auth/oauth/authorize",
            "token_url": "/auth/oauth/token",
            "client_id": "faultmaven-copilot",
            "scopes": [],
            "hosted_login_url": "/api/v1/auth/sso/login",
        }
    )

    assert parsed.supports_screen_hint is False
