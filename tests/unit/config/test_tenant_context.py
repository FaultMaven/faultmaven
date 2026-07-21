"""Unit tests for the tenant-context contextvar (ADR-010 P2).

Pins the contract the PostgreSQL RLS ``begin`` listener depends on
(``infrastructure/persistence/database.py``) and that the multi-tenant request
middleware sets per request: it lives at the neutral ``config`` leaf (so the
api layer may import it), defaults to the Standalone org, and round-trips.
"""

import pytest

from faultmaven.config.constants import STANDALONE_ORG_ID
from faultmaven.config.tenant_context import (
    get_current_org_id,
    set_current_org_id,
)


@pytest.mark.unit
def test_defaults_to_standalone_org():
    """A context that never sets the org resolves to the Standalone org.

    This is what keeps single-tenant deployments scoped without any per-request
    wiring — the RLS listener always has a valid org to apply.
    """
    assert get_current_org_id() == STANDALONE_ORG_ID


@pytest.mark.unit
def test_set_then_get_round_trips():
    """Setting the org is visible to a subsequent get in the same context."""
    set_current_org_id("org-1234")
    try:
        assert get_current_org_id() == "org-1234"
    finally:
        # Restore the default so contextvar state does not leak across tests.
        set_current_org_id(STANDALONE_ORG_ID)


@pytest.mark.unit
def test_importable_from_config_leaf():
    """The contextvar module resolves under ``config`` (neutral leaf), not
    under ``infrastructure`` — so the api-layer request middleware (P2b) can
    import it without violating the api→infrastructure boundary."""
    import faultmaven.config.tenant_context as mod

    assert mod.__name__ == "faultmaven.config.tenant_context"
