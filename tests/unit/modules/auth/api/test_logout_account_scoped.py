"""Deliberate sign-out is account-scoped, not session-scoped.

The dashboard and the browser extension hold INDEPENDENT token chains — the
extension's is minted by FaultMaven's own OAuth server with its own refresh
token. Revoking only the presented ``jti`` therefore signed out one client and
left the other running as the previous user: two identities in one browser, with
the extension still authoring cases as the wrong one.

The rule these pin: deliberate sign-out means everywhere; expiry means here.
Involuntary teardown is not routed through this and stays session-scoped.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import jwt as jwt_lib
import pytest

from faultmaven.modules.auth.api.auth import _revoke_account_wide, logout


@pytest.mark.unit
@pytest.mark.asyncio
async def test_revokes_every_token_for_the_user():
    auth_service = AsyncMock()

    ended = await _revoke_account_wide(auth_service, "user-1")

    assert ended is True
    auth_service.revoke_user_tokens.assert_awaited_once_with("user-1")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reports_failure_instead_of_raising():
    """The presented token is already revoked when this runs.

    Raising would claim the caller is not signed out when they are. Swallowing
    silently would be worse: the user asked to sign out and would be told they
    had, while their other clients kept running. The boolean is the third
    option, and the only honest one.
    """
    auth_service = AsyncMock()
    auth_service.revoke_user_tokens.side_effect = RuntimeError("store down")

    ended = await _revoke_account_wide(auth_service, "user-1")

    assert ended is False


# --------------------------------------------------------------------------- #
# The jti-less token: no revocation HANDLE is not no revocation MECHANISM
# --------------------------------------------------------------------------- #


def _request(auth_service):
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(auth_service=auth_service, sso_login_service=None)
        )
    )


def _token(claims):
    # Never verified — logout decodes with verify_signature=False, because
    # require_authentication already checked it. Length only silences PyJWT's
    # short-key warning.
    return jwt_lib.encode(
        claims, "unit-test-secret-key-please-ignore", algorithm="HS256"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_jti_less_token_still_revokes_the_account():
    """A token with no ``jti`` cannot be revoked by identifier — and the
    account-wide watermark does not need one, because it keys on ``sub``.

    Returning early on a missing ``jti`` declined to use the one mechanism that
    still applied: the caller got a 200 saying they were logged out while the
    token stayed valid until natural expiry and nothing at all was revoked.
    """
    auth_service = AsyncMock()

    result = await logout(
        request=_request(auth_service),
        current_user=SimpleNamespace(user_id="user-1"),
        token=_token({"sub": "user-1", "exp": 9999999999}),
        x_session_id=None,
    )

    auth_service.revoke_user_tokens.assert_awaited_once_with("user-1")
    auth_service.revoke_token.assert_not_awaited()
    assert result.revoked_tokens == 0
    assert result.all_sessions_ended is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_jti_less_token_says_so_when_the_account_revocation_failed():
    """Nothing was revoked and the token remains usable. Reporting a clean
    sign-out here would be the one claim the caller cannot check."""
    auth_service = AsyncMock()
    auth_service.revoke_user_tokens.side_effect = RuntimeError("store down")

    result = await logout(
        request=_request(auth_service),
        current_user=SimpleNamespace(user_id="user-1"),
        token=_token({"sub": "user-1", "exp": 9999999999}),
        x_session_id=None,
    )

    assert result.all_sessions_ended is False
    assert "may remain usable" in result.message


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ordinary_logout_revokes_both_the_jti_and_the_account():
    auth_service = AsyncMock()

    result = await logout(
        request=_request(auth_service),
        current_user=SimpleNamespace(user_id="user-1"),
        token=_token({"sub": "user-1", "jti": "jti-1", "exp": 9999999999}),
        x_session_id=None,
    )

    auth_service.revoke_token.assert_awaited_once_with("jti-1", 9999999999)
    auth_service.revoke_user_tokens.assert_awaited_once_with("user-1")
    assert result.revoked_tokens == 1
    assert result.all_sessions_ended is True
