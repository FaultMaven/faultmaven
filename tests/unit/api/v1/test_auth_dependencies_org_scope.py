"""Unit tests for organization scoping of the JWT-derived ``DevUser`` (ADR-010).

``get_current_user_optional`` (``api/v1/auth_dependencies``) builds a ``DevUser``
from a validated JWT. It must source ``organization_id`` from the request-scoped
tenant contextvar (``config.tenant_context``) — the same value PostgreSQL RLS
enforces for the request — and NOT from the raw ``organization_id`` JWT claim.

Sourcing the raw claim would (a) silently mask a missing claim to the Standalone
org via ``DevUser.__post_init__`` and (b) let a forged claim diverge from the
RLS-scoped org. The global ``bind_request_org_context`` dependency (P2b) has
already resolved the contextvar before this dependency runs: Standalone under
single-tenant (re-leak guard), the verified claim under multi-tenant (failing
closed on a missing org). These tests pin that this dependency reflects the
resolved contextvar rather than re-deriving org from the token.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.api.v1.auth_dependencies import get_current_user_optional
from faultmaven.config.constants import STANDALONE_ORG_ID
from faultmaven.config.tenant_context import set_current_org_id

OTHER_ORG = "22222222-2222-2222-2222-222222222222"
FORGED_ORG = "99999999-9999-9999-9999-999999999999"


@pytest.fixture(autouse=True)
def _reset_org_context():
    """Keep contextvar state from leaking across tests."""
    set_current_org_id(STANDALONE_ORG_ID)
    yield
    set_current_org_id(STANDALONE_ORG_ID)


async def _resolve_user(claims: dict):
    """Invoke get_current_user_optional with a verified-claims stub.

    Verification + revocation are AuthService's job (pinned by
    test_auth_dependencies_revocation.py); these tests stub the verified
    claims to isolate the org-sourcing behavior.
    """
    auth_service = MagicMock()
    auth_service.verify_token_with_revocation_check = AsyncMock(return_value=claims)
    return await get_current_user_optional(
        request=MagicMock(), token="a.valid.token", auth_service=auth_service
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_org_sourced_from_contextvar_not_raw_claim():
    """Multi-tenant: the org comes from the request-bound contextvar (what tenant_scope
    verified), not the raw claim — so a forged claim can never re-scope the user."""
    set_current_org_id(OTHER_ORG)  # tenant_scope bound the verified org

    user = await _resolve_user(
        {"sub": "user-1", "organization_id": FORGED_ORG, "auth_mode": "oauth"}
    )

    assert user is not None
    assert user.organization_id == OTHER_ORG  # contextvar wins, forged claim ignored


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_org_claim_does_not_mask_to_standalone():
    """A missing ``organization_id`` claim must reflect the bound context, not the
    ``__post_init__`` Standalone mask — the exact silent-masking trap this closes."""
    set_current_org_id(OTHER_ORG)

    user = await _resolve_user({"sub": "user-1", "auth_mode": "oauth"})

    assert user is not None
    assert user.organization_id == OTHER_ORG  # NOT masked to STANDALONE_ORG_ID


@pytest.mark.unit
@pytest.mark.asyncio
async def test_single_tenant_default_context_yields_standalone():
    """Single-tenant: tenant_scope leaves/forces the contextvar at the Standalone org,
    so the DevUser is scoped to Standalone regardless of any injected claim.

    Non-regression guard: this passes on both the old (omit org -> ``__post_init__``
    mask) and new (contextvar) code; it pins that single-tenant behavior is unchanged,
    not the fix itself (tests 1-2 discriminate the fix)."""
    # Contextvar at its Standalone default (autouse fixture), as under single-tenant.
    user = await _resolve_user(
        {"sub": "user-1", "organization_id": FORGED_ORG, "auth_mode": "local"}
    )

    assert user is not None
    assert user.organization_id == STANDALONE_ORG_ID
