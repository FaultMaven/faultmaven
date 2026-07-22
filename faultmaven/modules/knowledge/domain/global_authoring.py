"""Global-tier (platform corpus) KB authoring policy (#770, R4).

Global scope is the org-free platform tier: its rows are readable by every
tenant (RLS read exemption, migration 033) and its chunks are served to every
tenant by the org-free vector path, and — once verified — they re-enter
diagnosis for everyone as seeder candidates (the Phase-4 KB cause seeder
consumes ``metadata["causes"]``). Authoring global content is therefore a
platform-operator action, not a tenant one:

* **Multi-tenant** (``TENANT_PROVIDER=multi``): NO tenant session may author it —
  the ``admin`` role a tenant session carries is an ORG admin, not a platform
  operator, so publishing at global scope would be a cross-tenant content
  injection vector. Global content ships exclusively via the audited platform
  maintenance path (``jobs/run.py kb_seed --cross-tenant-maintenance``). The RLS
  write policies enforce the same invariant at the database layer
  (defense-in-depth, migration 033).
* **Single-tenant** (standalone / today's cloud-single): the deployment operator
  IS the platform operator, so the ``admin`` role is required.

This is the single source of truth for that policy. It is enforced at EVERY
point global content is authored:

* the ``convert`` / ``runbooks/create`` / ``documents`` (upload) /
  ``suggestions/{id}/approve`` routes, where the scope is a request field known
  at the API layer (via :mod:`faultmaven.modules.knowledge.api.platform_tier`,
  which reuses :data:`GLOBAL_AUTHORING_MULTI_MSG` from here); and
* the ``verify_draft`` / ``verify_batch`` / ``scan`` service methods, where a
  draft's scope is only known once the conversion-job row is loaded (or the
  on-disk file's directory is inspected) — so the gate lives at the service
  publish/mint point rather than the route.

The service enforcement raises the domain :class:`AuthorizationError`, which the
global exception handler (:mod:`faultmaven.api.exception_handlers`) translates to
HTTP 403. Keeping the enforcement in the domain (rather than importing the
API-layer ``platform_tier`` helper) respects the knowledge-module layer contract
(``api`` may import ``domain``, not the reverse).
"""

from faultmaven.exceptions import AuthorizationError
from faultmaven.providers.tenancy.factory import (
    BUILTIN_MULTI,
    requested_tenant_provider,
)

GLOBAL_AUTHORING_MULTI_MSG = (
    "Global-scope knowledge is the platform corpus: under multi-tenant "
    "deployment it is seeded exclusively via the audited platform maintenance "
    "path (kb_seed job), not through tenant sessions."
)

GLOBAL_AUTHORING_ADMIN_MSG = (
    "Global-scope knowledge is the platform corpus: authoring, verifying, or "
    "scanning it into the knowledge base requires the platform admin role."
)


def ensure_global_authoring_allowed(is_admin: bool) -> None:
    """Enforce the global-tier authoring policy, raising on refusal.

    Args:
        is_admin: Whether the caller carries the ``admin`` role. Ignored under
            multi-tenant deployment (no tenant session may author global scope).

    Raises:
        AuthorizationError: multi-tenant (any role) or single-tenant non-admin.
            The global exception handler maps it to HTTP 403.
    """
    if requested_tenant_provider() == BUILTIN_MULTI:
        raise AuthorizationError(GLOBAL_AUTHORING_MULTI_MSG)
    if not is_admin:
        raise AuthorizationError(GLOBAL_AUTHORING_ADMIN_MSG)


def is_global_authoring_allowed(is_admin: bool) -> bool:
    """Non-raising form of :func:`ensure_global_authoring_allowed`.

    Used where a mixed-scope operation (e.g. a disk scan discovering runbooks of
    every scope) must SKIP the global items a caller may not author while still
    processing the personal/team ones, rather than refusing the whole request.
    """
    try:
        ensure_global_authoring_allowed(is_admin)
        return True
    except AuthorizationError:
        return False
