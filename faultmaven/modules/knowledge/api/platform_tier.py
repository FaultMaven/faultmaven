"""Route-layer gate for global-tier (platform corpus) authoring under multi (#770).

This is the thin API-layer adapter over the shared policy in
:mod:`faultmaven.modules.knowledge.domain.global_authoring`. It is used by the
routes whose scope is a request field known at the API layer (``convert`` /
``runbooks/create`` / ``documents`` upload / ``suggestions/{id}/approve``): those
routes call :func:`require_global_authoring_allowed` (the multi arm) alongside
their own admin-role check (the single-tenant arm).

The publish/mint points whose scope is only known after a DB/disk lookup
(``verify_draft`` / ``verify_batch`` / ``scan``) enforce the same policy from the
service layer via
:func:`faultmaven.modules.knowledge.domain.global_authoring.ensure_global_authoring_allowed`
— the knowledge-module layer contract forbids the domain importing this API
module, so the multi arm is re-checked there rather than reused by import (the
canonical refusal message :data:`GLOBAL_AUTHORING_FORBIDDEN_MSG` IS shared, from
the domain policy).
"""

from fastapi import HTTPException

from faultmaven.modules.knowledge.domain.global_authoring import (
    GLOBAL_AUTHORING_MULTI_MSG,
)
from faultmaven.providers.tenancy.factory import (
    BUILTIN_MULTI,
    requested_tenant_provider,
)

# Backward-compatible public name; the canonical string lives in the domain
# policy so the service enforcement and this route gate share one message.
GLOBAL_AUTHORING_FORBIDDEN_MSG = GLOBAL_AUTHORING_MULTI_MSG


def require_global_authoring_allowed() -> None:
    """Refuse (403) any tenant-session attempt to author global-scope KB under multi."""
    if requested_tenant_provider() == BUILTIN_MULTI:
        raise HTTPException(status_code=403, detail=GLOBAL_AUTHORING_FORBIDDEN_MSG)
