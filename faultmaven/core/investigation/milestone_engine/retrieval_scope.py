"""A case's knowledge-base retrieval scope: the case driver's knowledge (#1919).

Retrieval applies the knowledge of the person driving the investigation:
global ∪ the driver's personal KB ∪ the runbooks shared to the driver's teams
(owner ruling on #1919, 2026-10-10). The model's answer written from that
knowledge is the driver applying it, and is accepted disclosure to everyone who
reads the case. Stored COPIES of runbook text (a turn's ``sources``) are a
different matter: they are access-checked per viewer when they are read back
(``gate_kb_sources`` in ``modules/knowledge/contracts.py``).

The driver is the case's EFFECTIVE driver (ADR-020 D1):
``COALESCE(driver_id, user_id)`` — the creator unless the case was handed to
someone. It is keyed HERE, in this one function: the pre-fetch (push),
``kb_qa`` (pull) and the runbook dedup all take their scope from it, and the
pre-fetch stamps the same key on ``kb_context_origin`` (ADR-020 D9).
"""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def retrieval_principal(case: Any) -> Optional[str]:
    """The account whose knowledge a case retrieves with: its effective driver
    (ADR-020 D1/D9). The scope below keys on it, and the pre-fetch stamps it on
    ``kb_context_origin`` so the stamp and the fetch always name the same
    account."""
    return getattr(case, "effective_driver_id", None) or getattr(case, "user_id", None)


async def case_retrieval_scope(
    case: Any,
    *,
    team_service: Optional[Any],
    share_repository: Optional[Any],
    raise_on_failure: bool = False,
) -> Dict[str, Any]:
    """The vector-store ``where`` filter for the case driver's knowledge.

    The driver is :func:`retrieval_principal`. ``team_service`` is ``None``
    only in standalone, where team collaboration is off
    (``create_team_service``): the scope is then global ∪ the driver's personal
    KB. A case with no driver (no assigned driver, and ``cases.user_id`` set
    NULL when the creator's account was deleted) reads global only.

    The team arm comes from ``list_all_user_team_ids``, which joins through
    ``teams`` and excludes retired ones, so a runbook shared only to a retired
    team is not in it.

    A failed team lookup narrows the scope to global ∪ the driver's personal
    KB; it never widens it. ``raise_on_failure`` re-raises instead. The runbook
    dedup sets it: a narrowed dedup search would back a "checked, nothing
    similar" claim it did not establish, so its caller takes a caveat branch.
    """
    from faultmaven.modules.knowledge.domain.services.knowledge_service import (
        build_kb_scope_filter,
        resolve_shared_kb_ids,
    )

    driver_id = retrieval_principal(case)
    shared_kb_ids: List[str] = []
    if driver_id and team_service is not None and share_repository is not None:
        try:
            team_ids = await team_service.list_all_user_team_ids(driver_id)
            shared_kb_ids = await resolve_shared_kb_ids(
                share_repository, team_ids, getattr(case, "enterprise_id", None)
            )
        except Exception:  # noqa: BLE001
            if raise_on_failure:
                raise
            logger.warning(
                "driver_team_kb_resolution_failed: KB retrieval scope narrowed "
                "to global and the driver's personal KB",
                extra={"case_id": getattr(case, "case_id", None)},
                exc_info=True,
            )
            shared_kb_ids = []
    return build_kb_scope_filter(driver_id, shared_kb_ids)
