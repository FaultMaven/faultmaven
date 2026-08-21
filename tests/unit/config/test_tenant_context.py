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
    writable_org_id,
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


# =============================================================================
# writable_org_id — the org a write stamps on its row (#1143)
# =============================================================================


@pytest.mark.unit
def test_writable_org_id_prefers_the_explicit_org():
    """A caller that knows whose row this is beats the ambient context."""
    set_current_org_id("org-ambient")
    try:
        assert writable_org_id("org-explicit") == "org-explicit"
    finally:
        set_current_org_id(STANDALONE_ORG_ID)


@pytest.mark.unit
def test_writable_org_id_falls_back_to_the_bound_tenant():
    """With no explicit org, the stamp is the org the session is bound to.

    Identical to what the RLS ``begin`` listener puts in ``app.current_org_id``,
    which is the whole point: a stamp that disagrees with the binding is a row
    the same transaction is refused permission to write (#1143).
    """
    set_current_org_id("org-bound")
    try:
        assert writable_org_id(None) == "org-bound"
    finally:
        set_current_org_id(STANDALONE_ORG_ID)


@pytest.mark.unit
def test_writable_org_id_is_a_noop_for_standalone():
    """Single-tenant keeps stamping the Standalone org, unchanged.

    The contextvar's own default is that org, so the #1143 fix moved nothing for
    a standalone deployment — pinned so a future change to the fallback cannot
    quietly relocate standalone rows.
    """
    assert writable_org_id(None) == STANDALONE_ORG_ID


@pytest.mark.unit
def test_writable_org_id_refuses_an_unscoped_context():
    """The empty non-org sentinel must raise, not be stamped.

    ``api/middleware/tenant_scope`` binds ``""`` for unauthenticated and
    invalid-token requests. That value passes ``NOT NULL`` and — because
    ``current_setting('app.current_org_id')`` is also ``""`` — passes the RLS
    ``WITH CHECK`` as well, so without this guard the write survives both checks
    and dies on the ``organizations`` FK as an opaque ``IntegrityError`` several
    frames from the cause.
    """
    set_current_org_id("")
    try:
        with pytest.raises(ValueError, match="not scoped to an organization"):
            writable_org_id(None)
    finally:
        set_current_org_id(STANDALONE_ORG_ID)
