"""Canonical single-tenant ("standalone") identity constants.

The standalone (self-hosted) deployment is single-tenant: every case, user and
artifact belongs to one implicit **enterprise** — the isolation boundary
(ADR-017 D8). These UUIDs are that implicit identity, stamped on write so the
NOT NULL ``enterprise_id`` substrate column is always populated without the
deployment ever having to create or choose a tenant.

There is deliberately **no standalone organization**. Under ADR-017 the
organization is a billing target, and a deployment nobody pays for has none;
``organization_id`` on a data row is nullable attribution and stays ``NULL``
here. The default team row exists so the sharing substrate has a scope to point
at (team-scoped sharing itself stays inert in standalone — there is no
membership-population path).

Multi-tenant isolation is NOT enforced via these constants. In standalone there
is nothing to isolate (one enterprise). In cloud, isolation is enforced by
PostgreSQL Row-Level Security keyed on ``enterprise_id`` — NOT by per-query
``WHERE enterprise_id`` filters in the standalone repositories. The column is
the schema substrate both deployments share; only the isolation *mechanism*
differs.

This module is intentionally dependency-free so any layer (config, models,
providers, repositories) can import it without creating an import cycle or
tripping an import-linter contract.
"""

# Implicit single-tenant enterprise — the isolation boundary (ADR-017 D8).
STANDALONE_ENTERPRISE_ID = "00000000-0000-0000-0000-000000000002"
STANDALONE_ENTERPRISE_SLUG = "default"
STANDALONE_ENTERPRISE_NAME = "Default Enterprise"

# Implicit single-tenant team (the default sharing unit inside the enterprise).
# Team-scoped sharing stays inert in standalone (no membership-population path —
# that is the Cloud management module); the row exists so the schema's sharing
# substrate is complete.
STANDALONE_TEAM_ID = "00000000-0000-0000-0000-000000000003"
STANDALONE_TEAM_NAME = "Default Team"
