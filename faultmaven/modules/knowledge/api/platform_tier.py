"""Gate for global-tier (platform corpus) authoring under multi-tenancy (#770).

Global-scope knowledge is the org-free platform tier: its rows are readable by
every tenant (RLS read exemption, migration 033) and its chunks are served to
every tenant by the org-free vector path. Under ``TENANT_PROVIDER=multi`` the
``admin`` role carried by a tenant session is an ORG admin, not a platform
operator — letting it publish at global scope would be a cross-tenant content
injection vector. Global content under multi therefore ships exclusively via
the audited platform maintenance path (``jobs/run.py kb_seed
--cross-tenant-maintenance``); the RLS write policies enforce the same
invariant at the database layer (defense-in-depth, migration 033).

Single-tenant deployments are unaffected: there the deployment operator IS the
platform operator, and the existing admin-role checks keep applying.
"""

from fastapi import HTTPException

from faultmaven.providers.tenancy.factory import (
    BUILTIN_MULTI,
    requested_tenant_provider,
)

GLOBAL_AUTHORING_FORBIDDEN_MSG = (
    "Global-scope knowledge is the platform corpus: under multi-tenant "
    "deployment it is seeded exclusively via the audited platform maintenance "
    "path (kb_seed job), not through tenant sessions."
)


def require_global_authoring_allowed() -> None:
    """Refuse (403) any tenant-session attempt to author global-scope KB under multi."""
    if requested_tenant_provider() == BUILTIN_MULTI:
        raise HTTPException(status_code=403, detail=GLOBAL_AUTHORING_FORBIDDEN_MSG)
