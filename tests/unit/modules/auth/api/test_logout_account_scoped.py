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

from unittest.mock import AsyncMock

import pytest

from faultmaven.modules.auth.api.auth import _revoke_account_wide


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
