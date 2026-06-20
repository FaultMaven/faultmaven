"""Canonical single-tenant ("standalone") identity constants.

The standalone (self-hosted) deployment is single-tenant: every case,
user, and artifact belongs to one implicit organization inside one implicit
enterprise. These UUIDs are that implicit identity — stamped on write so the
NOT NULL ``organization_id`` / ``enterprise_id`` substrate columns are always
populated, without the deployment ever having to create or choose a tenant.

Multi-tenant isolation is NOT enforced via these constants. In standalone there
is nothing to isolate (one tenant). In cloud, tenant isolation is enforced by
PostgreSQL Row-Level Security — NOT by per-query ``WHERE organization_id``
filters in the standalone repositories. The ``organization_id`` column remains the schema substrate both
deployments share; only the isolation *mechanism* differs.

This module is intentionally dependency-free so any layer (config, models,
providers, repositories) can import it without creating an import cycle or
tripping an import-linter contract.
"""

# Implicit single-tenant organization (standalone / self-hosted).
STANDALONE_ORG_ID = "00000000-0000-0000-0000-000000000001"
STANDALONE_ORG_SLUG = "default"
STANDALONE_ORG_NAME = "Default Organization"

# Implicit single-tenant enterprise (the top-tier container for the org).
STANDALONE_ENTERPRISE_ID = "00000000-0000-0000-0000-000000000002"
STANDALONE_ENTERPRISE_SLUG = "default"
STANDALONE_ENTERPRISE_NAME = "Default Enterprise"
