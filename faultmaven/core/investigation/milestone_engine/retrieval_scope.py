"""A case's knowledge-base retrieval scope: the case's audience, not a user's (#1919).

What the engine retrieves from the knowledge base during a turn reaches the case
transcript. The pre-fetch renders runbook excerpts into the prompt, ``kb_qa``
relays them in its answer, and the runbook dedup names the runbook it found.
Every reader of the case reads that transcript (ADR-013 D4). So the scope a
case retrieves from must be one that every reader of the case may read:

- **Unshared case** (no share rows; its one reader is the creator,
  ``cases.user_id``): global ∪ the creator's personal KB ∪ the runbooks shared
  to the creator's teams.
- **Shared case** (its readers are the creator plus the members of the teams
  it is shared with): global ∪ the runbooks shared to those teams. Nobody's
  personal KB: the creator's would reach the teams, and no one reader's
  personal KB is readable by all the others.

The scope is a function of the case, never of whoever submitted the turn, so
the same case retrieves the same scope whoever is at the keyboard (owner
ruling on #1898, 2026-10-09).

Push, pull and dedup all take their scope from :func:`case_retrieval_scope`.
"""

import logging
from typing import Any, Dict, List, Optional

from faultmaven.config.tenant_context import usable_tenant_id

logger = logging.getLogger(__name__)


async def case_retrieval_scope(
    case: Any,
    *,
    team_service: Optional[Any],
    share_repository: Optional[Any],
    raise_on_failure: bool = False,
) -> Dict[str, Any]:
    """The vector-store ``where`` filter for what ``case``'s audience may read.

    ``team_service`` is ``None`` only in standalone, where team collaboration is
    off (``create_team_service``). Sharing a case needs a team service, so no
    case is shared there and the unshared rule applies, without a team arm.
    ``share_repository`` is created in both modes; when it is absent no share
    row can have been written (``CaseService._share_case_with_team`` no-ops),
    which is again the unshared rule.

    A lookup that fails narrows the scope; it never widens it. The degraded
    scope depends on what is still known:

    - The case's own share lookup fails: whether the case is shared is
      unknown, so the personal arm cannot be shown to be safe. Global only.
    - The case is shared, and resolving its teams' runbooks fails: global only.
    - The case is unshared, and resolving the creator's team runbooks fails:
      global ∪ the creator's personal KB. The creator is the one reader, so the
      personal arm stays within the audience; only the team arm is lost.

    ``raise_on_failure`` re-raises instead of degrading. The runbook dedup sets
    it: a narrowed dedup search would back a "checked, nothing similar" claim
    it did not establish, so its caller takes a caveat branch instead.
    """
    from faultmaven.modules.knowledge.domain.services.knowledge_service import (
        build_kb_scope_filter,
        resolve_shared_kb_ids,
    )

    creator_id = getattr(case, "user_id", None)
    case_id = getattr(case, "case_id", None)
    enterprise_id = getattr(case, "enterprise_id", None)

    if team_service is None or share_repository is None:
        return build_kb_scope_filter(creator_id, [])

    try:
        shares = await share_repository.list_scopes_for_resource("case", case_id)
    except Exception:  # noqa: BLE001
        if raise_on_failure:
            raise
        logger.warning(
            "case_share_lookup_failed: KB retrieval scope narrowed to global",
            extra={"case_id": case_id},
            exc_info=True,
        )
        return build_kb_scope_filter(None, [])

    if not shares:
        try:
            team_ids = (
                await team_service.list_all_user_team_ids(creator_id)
                if creator_id
                else []
            )
            shared_kb_ids = await resolve_shared_kb_ids(
                share_repository, team_ids, enterprise_id
            )
        except Exception:  # noqa: BLE001
            if raise_on_failure:
                raise
            logger.warning(
                "creator_team_kb_resolution_failed: KB retrieval scope narrowed "
                "to global and the creator's personal KB",
                extra={"case_id": case_id},
                exc_info=True,
            )
            shared_kb_ids = []
        return build_kb_scope_filter(creator_id, shared_kb_ids)

    try:
        case_team_ids = await _live_case_team_ids(shares, team_service, enterprise_id)
        shared_kb_ids = await resolve_shared_kb_ids(
            share_repository, case_team_ids, enterprise_id
        )
    except Exception:  # noqa: BLE001
        if raise_on_failure:
            raise
        logger.warning(
            "case_team_kb_resolution_failed: KB retrieval scope narrowed to global",
            extra={"case_id": case_id},
            exc_info=True,
        )
        shared_kb_ids = []
    return build_kb_scope_filter(None, shared_kb_ids)


async def _live_case_team_ids(
    shares: List[Any], team_service: Any, enterprise_id: Optional[str]
) -> List[str]:
    """The live teams, in the case's enterprise, that ``shares`` name.

    A retired team keeps its share rows (``ShareRepository`` docstring), but its
    former members no longer read the case: the case read allowlist reaches
    teams through membership, which excludes ``teams.deleted_at``. Runbooks
    shared only to a retired team are readable by their owner alone, so the
    team arm must not carry them to the case's live teams.
    """
    team_ids = sorted({s.scope_id for s in shares if s.scope_type == "team"})
    tenant_id = usable_tenant_id(enterprise_id)
    if not team_ids or not tenant_id:
        return []
    live = await team_service.name_teams(enterprise_id=tenant_id, team_ids=team_ids)
    return [team_id for team_id in team_ids if team_id in live]
