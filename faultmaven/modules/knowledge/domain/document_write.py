"""Scope-aware write policy for published KB documents (#834).

The document write routes (``PUT``/``DELETE /knowledge/documents/{id}`` and
their bulk twins ``POST /knowledge/documents/bulk-update``/``bulk-delete``)
were unconditionally operator-only, so the owner of a personal-scope runbook
could not edit or delete it. This policy makes them ownership-aware:

* **global** — platform-corpus authoring: delegate to the global-tier policy
  (:mod:`faultmaven.modules.knowledge.domain.global_authoring` — refused from
  any tenant session under multi, ``platform_admin`` single-tenant).
* **personal / team** — the document's owner may write it. A non-owner may
  only when they are the single-tenant platform operator (the deployment
  admin), expressed as :func:`is_global_authoring_allowed` — under multi there
  is deliberately NO operator override here (operator access to tenant content
  is the break-glass path, ADR-012, never a standing bypass). Team-scope
  shares grant read visibility (ADR-013 §D4), not write.

Every actor-facing write route applies it, single and bulk alike (#866): the
bulk routes run it once per target and pass only the permitted ids on, so the
two surfaces cannot drift.

Rejected alternative: enforcing inside ``KnowledgeService`` write methods —
those remain the trusted path for internal ingestion and maintenance jobs,
which have no actor, so the actor-facing gate lives at the route boundary
where the actor is known.
"""

from typing import Optional

from faultmaven.exceptions import AuthorizationError
from faultmaven.modules.knowledge.domain.global_authoring import (
    ensure_global_authoring_allowed,
    is_global_authoring_allowed,
)

DOCUMENT_WRITE_FORBIDDEN_MSG = (
    "Only the document's owner (or the platform operator) may modify or delete it."
)


def ensure_document_write_allowed(
    *,
    scope: str,
    owner_id: Optional[str],
    actor_user_id: str,
    is_platform_admin: bool,
) -> None:
    """Enforce the document write policy, raising on refusal.

    Raises:
        AuthorizationError: the actor may not write this document. The global
            exception handler maps it to HTTP 403.
    """
    if scope == "global":
        ensure_global_authoring_allowed(is_platform_admin)
        return
    if owner_id and actor_user_id and owner_id == actor_user_id:
        return
    if is_global_authoring_allowed(is_platform_admin):
        return
    raise AuthorizationError(DOCUMENT_WRITE_FORBIDDEN_MSG)
