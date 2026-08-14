"""Unit tests for the SSO identity-provider seam (ADR-015, PR 1).

Covers three things and nothing that needs a live WorkOS SDK:

* the WorkOS adapter's URL construction, identity mapping, and error mapping,
  exercised through an injected fake client (the adapter is import-safe without
  the ``workos`` package);
* the ``AuthSettings.sso_configured`` gate across the local / unconfigured /
  configured matrix;
* the DI factory, which must return None unless oauth-mode SSO is configured and
  otherwise build the provider from the (secret-unwrapped) settings.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from faultmaven.config.settings import AuthMode, AuthSettings
from faultmaven.container.providers.services import create_sso_identity_provider
from faultmaven.modules.auth.contracts import SSOIdentity
from faultmaven.modules.auth.exceptions import SSOAuthenticationError
from faultmaven.modules.auth.infrastructure.sso.workos_provider import (
    WorkOSIdentityProvider,
)


class _FakeUserManagement:
    """Records get_authorization_url kwargs; serves a scripted authenticate."""

    def __init__(
        self, *, authorize_url="https://idp.example/authkit", response=None, raises=None
    ):
        self._authorize_url = authorize_url
        self._response = response
        self._raises = raises
        self.authorize_calls: list[dict] = []
        self.authenticate_calls: list[dict] = []

    def get_authorization_url(self, **kwargs):
        self.authorize_calls.append(kwargs)
        return self._authorize_url

    def authenticate_with_code(self, **kwargs):
        self.authenticate_calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return self._response


class _FakeClient:
    def __init__(self, user_management):
        self.user_management = user_management


def _workos_response(**user_overrides):
    fields = {
        "id": "user_01ABC",
        "email": "dev@example.com",
        "email_verified": True,
        "first_name": "Dev",
        "last_name": "Eloper",
        "name": None,
    }
    fields.update(user_overrides)
    return SimpleNamespace(user=SimpleNamespace(**fields), organization_id="org_01XYZ")


# --------------------------------------------------------------------------- #
# Adapter: authorization URL
# --------------------------------------------------------------------------- #


def test_build_authorization_url_uses_authkit_and_configured_redirect():
    um = _FakeUserManagement(authorize_url="https://idp.example/login")
    provider = WorkOSIdentityProvider(client=_FakeClient(um), redirect_uri="https://cb")

    url = provider.build_authorization_url(state="state-123")

    assert url == "https://idp.example/login"
    assert um.authorize_calls == [
        {"provider": "authkit", "redirect_uri": "https://cb", "state": "state-123"}
    ]


def test_provider_name_is_workos():
    provider = WorkOSIdentityProvider(
        client=_FakeClient(_FakeUserManagement()), redirect_uri="https://cb"
    )
    assert provider.provider_name == "workos"


# --------------------------------------------------------------------------- #
# Adapter: code exchange → normalized identity
# --------------------------------------------------------------------------- #


def test_exchange_code_maps_workos_user_to_identity():
    um = _FakeUserManagement(response=_workos_response())
    provider = WorkOSIdentityProvider(client=_FakeClient(um), redirect_uri="https://cb")

    identity = provider.exchange_code("auth-code")

    assert um.authenticate_calls == [{"code": "auth-code"}]
    assert identity == SSOIdentity(
        provider="workos",
        provider_user_id="user_01ABC",
        email="dev@example.com",
        email_verified=True,
        display_name="Dev Eloper",
        organization_id="org_01XYZ",
    )


def test_exchange_code_prefers_workos_name_over_first_last():
    um = _FakeUserManagement(response=_workos_response(name="Ada Lovelace"))
    provider = WorkOSIdentityProvider(client=_FakeClient(um), redirect_uri="https://cb")

    identity = provider.exchange_code("auth-code")

    assert identity.display_name == "Ada Lovelace"


def test_exchange_code_display_name_none_when_no_name_parts():
    um = _FakeUserManagement(
        response=_workos_response(name=None, first_name=None, last_name=None)
    )
    provider = WorkOSIdentityProvider(client=_FakeClient(um), redirect_uri="https://cb")

    identity = provider.exchange_code("auth-code")

    assert identity.display_name is None


def test_exchange_code_preserves_unverified_email_flag():
    um = _FakeUserManagement(response=_workos_response(email_verified=False))
    provider = WorkOSIdentityProvider(client=_FakeClient(um), redirect_uri="https://cb")

    assert provider.exchange_code("auth-code").email_verified is False


def test_exchange_code_wraps_sdk_failure_as_sso_error():
    um = _FakeUserManagement(raises=RuntimeError("workos: invalid_grant"))
    provider = WorkOSIdentityProvider(client=_FakeClient(um), redirect_uri="https://cb")

    with pytest.raises(SSOAuthenticationError) as exc_info:
        provider.exchange_code("bad-code")

    # Uniform message — no provider detail leaks to the caller.
    assert "invalid_grant" not in str(exc_info.value)


def test_exchange_code_maps_malformed_response_to_sso_error():
    # A response missing the expected shape must not leak a raw AttributeError;
    # it is an auth failure like any other (identity mapping is inside the guard).
    um = _FakeUserManagement(response=SimpleNamespace())  # no .user attribute
    provider = WorkOSIdentityProvider(client=_FakeClient(um), redirect_uri="https://cb")

    with pytest.raises(SSOAuthenticationError):
        provider.exchange_code("auth-code")


# --------------------------------------------------------------------------- #
# Settings gate: AuthSettings.sso_configured
# --------------------------------------------------------------------------- #


def _oauth_env(monkeypatch, **overrides):
    monkeypatch.setenv("AUTH_MODE", "oauth")
    monkeypatch.setenv("OAUTH_ENABLED", "true")
    for key in ("WORKOS_API_KEY", "WORKOS_CLIENT_ID", "WORKOS_REDIRECT_URI"):
        monkeypatch.delenv(key, raising=False)
    for key, value in overrides.items():
        monkeypatch.setenv(key, value)


def test_sso_configured_true_when_oauth_and_all_workos_present(monkeypatch):
    _oauth_env(
        monkeypatch,
        WORKOS_API_KEY="sk_test_123",
        WORKOS_CLIENT_ID="client_123",
        WORKOS_REDIRECT_URI="https://api.example/api/v1/auth/sso/callback",
    )
    assert AuthSettings().sso_configured is True


def test_sso_configured_false_when_any_workos_field_missing(monkeypatch):
    _oauth_env(
        monkeypatch,
        WORKOS_API_KEY="sk_test_123",
        WORKOS_CLIENT_ID="client_123",
        # redirect uri intentionally absent
    )
    assert AuthSettings().sso_configured is False


def test_sso_configured_false_when_api_key_is_empty_string(monkeypatch):
    # A blank/un-injected secret must read as "not configured" so SSO stays
    # cleanly off, rather than building a client with an empty key.
    _oauth_env(
        monkeypatch,
        WORKOS_API_KEY="",
        WORKOS_CLIENT_ID="client_123",
        WORKOS_REDIRECT_URI="https://api.example/cb",
    )
    assert AuthSettings().sso_configured is False


def test_sso_configured_false_in_local_mode_even_if_workos_set(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "local")
    monkeypatch.setenv("OAUTH_ENABLED", "false")
    monkeypatch.setenv("WORKOS_API_KEY", "sk_test_123")
    monkeypatch.setenv("WORKOS_CLIENT_ID", "client_123")
    monkeypatch.setenv("WORKOS_REDIRECT_URI", "https://api.example/cb")

    settings = AuthSettings()
    assert settings.auth_mode == AuthMode.LOCAL
    assert settings.sso_configured is False


def test_workos_api_key_is_secret(monkeypatch):
    _oauth_env(
        monkeypatch,
        WORKOS_API_KEY="sk_test_secret",
        WORKOS_CLIENT_ID="client_123",
        WORKOS_REDIRECT_URI="https://api.example/cb",
    )
    settings = AuthSettings()
    # Secret is not exposed in repr; only get_secret_value() reveals it.
    assert "sk_test_secret" not in repr(settings.workos_api_key)
    assert settings.workos_api_key.get_secret_value() == "sk_test_secret"


# --------------------------------------------------------------------------- #
# DI factory
# --------------------------------------------------------------------------- #


def _settings_with_auth(auth):
    return SimpleNamespace(auth=auth)


def test_factory_returns_none_when_not_configured():
    auth = SimpleNamespace(sso_configured=False)
    assert create_sso_identity_provider(_settings_with_auth(auth)) is None


def test_factory_builds_provider_from_config_when_configured(monkeypatch):
    captured = {}
    sentinel = object()

    def fake_from_config(**kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(WorkOSIdentityProvider, "from_config", fake_from_config)

    auth = SimpleNamespace(
        sso_configured=True,
        workos_api_key=SimpleNamespace(get_secret_value=lambda: "sk_secret"),
        workos_client_id="client_123",
        workos_redirect_uri="https://api.example/cb",
    )

    result = create_sso_identity_provider(_settings_with_auth(auth))

    assert result is sentinel
    assert captured == {
        "api_key": "sk_secret",
        "client_id": "client_123",
        "redirect_uri": "https://api.example/cb",
    }


# --------------------------------------------------------------------------- #
# Adapter: single-logout (ending the IdP's own session)
# --------------------------------------------------------------------------- #
#
# Clearing FaultMaven's session does not end the IdP's. Without these, "log out"
# leaves the AuthKit session alive: the next authorization request is answered
# without a prompt, so the account cannot be switched and the next person at a
# shared browser is one click from being signed in.


def _access_token(claims: dict) -> str:
    """A WorkOS-shaped access token. The signature is irrelevant by design —
    the adapter reads ``sid`` without verifying (see ``_session_id_of``)."""
    import jwt as jwt_lib

    return jwt_lib.encode(
        claims, "irrelevant-secret-padded-to-32-bytes-min", algorithm="HS256"
    )


def test_exchange_extracts_the_session_id_from_the_access_token():
    """The positive case: WorkOS returns no session_id field, only the claim."""
    response = _workos_response()
    response.access_token = _access_token(
        {"sid": "session_01HSID", "sub": "user_01ABC"}
    )
    provider = WorkOSIdentityProvider(
        client=_FakeClient(_FakeUserManagement(response=response)),
        redirect_uri="https://cb",
    )

    identity = provider.exchange_code("code-1")

    assert identity.provider_session_id == "session_01HSID"


@pytest.mark.parametrize(
    ("access_token", "why"),
    [
        (None, "provider returned no token at all"),
        ("", "empty token"),
        ("not-a-jwt", "undecodable"),
        (_access_token({"sub": "user_01ABC"}), "token carries no sid claim"),
        (_access_token({"sid": ""}), "sid present but empty"),
        (_access_token({"sid": 12345}), "sid present but not a string"),
    ],
)
def test_exchange_degrades_to_no_session_id_rather_than_failing(access_token, why):
    """A missing session id costs single-logout. It must never cost the login.

    Every one of these would otherwise raise inside ``_to_identity`` and be
    caught by ``exchange_code``'s blanket handler as ``SSOAuthenticationError``
    — turning an unreadable *logout* handle into a failed *sign-in*.
    """
    response = _workos_response()
    response.access_token = access_token
    provider = WorkOSIdentityProvider(
        client=_FakeClient(_FakeUserManagement(response=response)),
        redirect_uri="https://cb",
    )

    identity = provider.exchange_code("code-1")

    assert identity.provider_session_id is None, why
    # The login itself is unharmed — the rest of the identity still maps.
    assert identity.provider_user_id == "user_01ABC"
    assert identity.email == "dev@example.com"


def test_build_logout_url_asks_the_sdk_for_the_session_it_was_given():
    um = _FakeUserManagement()
    um.get_logout_url = lambda **kw: (  # type: ignore[method-assign]
        um.logout_calls.append(kw) or "https://idp.example/logout?session=abc"
    )
    um.logout_calls = []  # type: ignore[attr-defined]
    provider = WorkOSIdentityProvider(client=_FakeClient(um), redirect_uri="https://cb")

    url = provider.build_logout_url(provider_session_id="session_01HSID")

    assert url == "https://idp.example/logout?session=abc"
    assert um.logout_calls == [{"session_id": "session_01HSID"}]


def test_build_logout_url_returns_none_when_the_sdk_raises():
    """Logout runs after local teardown. Raising here would report a failed
    logout for a sign-out that already did the part that matters."""

    def _boom(**_kwargs):
        raise RuntimeError("workos is down")

    um = _FakeUserManagement()
    um.get_logout_url = _boom  # type: ignore[method-assign]
    provider = WorkOSIdentityProvider(client=_FakeClient(um), redirect_uri="https://cb")

    assert provider.build_logout_url(provider_session_id="session_01HSID") is None


def test_build_logout_url_returns_none_for_an_empty_session_id():
    """Never call the SDK with an empty handle — an empty ``session_id`` is a
    request to log out *something*, and what it would end is unspecified."""

    def _must_not_be_called(**_kwargs):  # pragma: no cover - asserted by failure
        raise AssertionError("SDK called with an empty session id")

    um = _FakeUserManagement()
    um.get_logout_url = _must_not_be_called  # type: ignore[method-assign]
    provider = WorkOSIdentityProvider(client=_FakeClient(um), redirect_uri="https://cb")

    assert provider.build_logout_url(provider_session_id="") is None


def test_default_port_implementation_offers_no_single_logout():
    """The port's default answers None so a provider without single-logout is a
    supported implementation, not a broken one."""
    from faultmaven.modules.auth.contracts import ISSOIdentityProvider

    class _Minimal(ISSOIdentityProvider):
        @property
        def provider_name(self):
            return "minimal"

        def build_authorization_url(self, *, state):
            return "https://idp/authorize"

        def exchange_code(self, code):
            return SSOIdentity(
                provider="minimal",
                provider_user_id="u",
                email="u@example.com",
                email_verified=True,
            )

    assert _Minimal().build_logout_url(provider_session_id="whatever") is None
