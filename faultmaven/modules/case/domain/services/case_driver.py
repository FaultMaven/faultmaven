"""The case driver (ADR-020): reassignment, candidates and releases.

A case has a CREATOR (``cases.user_id``) and a DRIVER (``cases.driver_id``,
NULL meaning the creator drives). The driver holds the investigation writes;
the creator holds governance. This module holds the rules that MOVE the driver:

- **Reassignment** (D4): the creator or the effective driver picks the new
  driver from the case's candidates, through a versioned write.
- **Candidates** (D4): the creator, whatever its account kind, plus the active
  individual accounts in the case's enterprise that are members of a team the
  case is shared with.
- **Releases** (D3): an operation that would leave the driver unable to read
  the case hands it back to the creator FIRST, because the operation's own
  write commits in a store this module cannot share a transaction with.

It is a mixin of :class:`CaseService` so that every rule here resolves access
through the service's one resolver (``_may_access``) and its share and
membership lookups, rather than a second copy of them.
"""

import logging
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Set

from faultmaven.exceptions import (
    CASE_TERMINAL,
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ValidationException,
)
from faultmaven.modules.case.domain.models.case import Case
from faultmaven.modules.case.domain.models.driver import (
    CaseDriverChange,
    CaseDriverChangeReason,
    DrivenCase,
)

logger = logging.getLogger(__name__)

#: The x-error-code a lost version race carries, the turn route's own code for
#: the same condition.
CASE_VERSION_CONFLICT = "CASE_VERSION_CONFLICT"

#: The account kind a non-creator candidate must be (ADR-017 D6 kinds:
#: ``individual | service``). A service account as driver would leave no human
#: able to write the case until the creator took it back (ADR-020 D4).
_INDIVIDUAL = "individual"

#: How often a reassignment reloads and re-decides after losing the version
#: race to a concurrent writer before answering 409.
_REASSIGN_ATTEMPTS = 3


@dataclass(frozen=True)
class DriverCandidate:
    """One account a case's driver may be handed to (ADR-020 D4)."""

    user_id: str
    display_name: Optional[str]


class CaseDriverMixin:
    """Driver rules for :class:`CaseService` (ADR-020 D3, D4, D5)."""

    # -- lookups ----------------------------------------------------------- #

    async def _team_member_ids(
        self, enterprise_id: str, team_ids: Iterable[str]
    ) -> Set[str]:
        """Every member of ``team_ids`` (live teams of ``enterprise_id``)."""
        team_ids = list(team_ids)
        if not team_ids or not self.team_service:
            return set()
        rosters = await self.team_service.list_member_ids_of_teams(
            enterprise_id=enterprise_id, team_ids=team_ids
        )
        return {uid for members in rosters.values() for uid in members}

    async def _accounts(self, enterprise_id: str, user_ids: Iterable[str]) -> Dict:
        """``user_id`` → account, for the ids anchored to ``enterprise_id``."""
        wanted = sorted({uid for uid in user_ids if uid})
        if not wanted or not self.account_reader:
            return {}
        accounts = await self.account_reader.get_many_in_enterprise(
            enterprise_id, wanted
        )
        return {a.user_id: a for a in accounts}

    async def _driver_candidates(self, case: Case) -> List[DriverCandidate]:
        """The creator, then the active individual members of the case's
        teams, in the case's enterprise (ADR-020 D4). Without an account reader
        nothing can be vouched for beyond the creator, so only the creator is
        offered."""
        member_ids = await self._team_member_ids(
            case.enterprise_id, await self.get_case_team_ids(case.case_id)
        )
        accounts = await self._accounts(case.enterprise_id, member_ids | {case.user_id})
        candidates: List[DriverCandidate] = []
        if case.user_id:
            creator = accounts.get(case.user_id)
            candidates.append(
                DriverCandidate(
                    user_id=case.user_id,
                    display_name=creator.display_name if creator else None,
                )
            )
        for uid in sorted(member_ids - {case.user_id}):
            account = accounts.get(uid)
            if (
                account is not None
                and account.is_active
                and account.account_kind == _INDIVIDUAL
            ):
                candidates.append(
                    DriverCandidate(user_id=uid, display_name=account.display_name)
                )
        return candidates

    async def display_names_for(
        self, enterprise_id: str, user_ids: Iterable[str]
    ) -> Dict[str, str]:
        """``user_id`` → display name, never an email (ADR-020 D5)."""
        accounts = await self._accounts(enterprise_id, user_ids)
        return {uid: a.display_name for uid, a in accounts.items()}

    async def fill_display_names(self, rows: Iterable) -> None:
        """Set ``creator_display_name`` / ``driver_display_name`` on case rows
        (``CaseSummary`` / ``CaseDetail``) in place, one account read per
        enterprise (ADR-020 D5). Best-effort: a failed read leaves the names
        null rather than failing the listing that called it."""
        rows = list(rows)
        by_enterprise: Dict[str, Set[str]] = {}
        for row in rows:
            by_enterprise.setdefault(row.enterprise_id, set()).update(
                uid for uid in (row.user_id, row.driver_id) if uid
            )
        names: Dict[tuple, str] = {}
        for enterprise_id, user_ids in by_enterprise.items():
            try:
                found = await self.display_names_for(enterprise_id, user_ids)
            except Exception as e:  # noqa: BLE001
                logger.warning("Failed to resolve case display names: %s", e)
                continue
            for uid, name in found.items():
                names[(enterprise_id, uid)] = name
        for row in rows:
            row.creator_display_name = names.get((row.enterprise_id, row.user_id))
            row.driver_display_name = names.get((row.enterprise_id, row.driver_id))

    # -- the governance gate ------------------------------------------------ #

    async def _case_for_driver_governance(
        self, case_id: str, actor_user_id: str
    ) -> Case:
        """The case, iff ``actor_user_id`` is its creator or effective driver.

        A caller who cannot READ the case gets 404 (the read gate runs first,
        as on every case route); a reader who is neither gets 403 (ADR-020
        D4).
        """
        case = await self.repository.get(case_id)
        if case is None or not await self._may_access(case, actor_user_id):
            raise NotFoundError("Case", case_id)
        if actor_user_id not in (case.user_id, case.effective_driver_id):
            raise AuthorizationError(
                "Only the case's creator or its current driver may do this"
            )
        return case

    # -- D4: candidates and reassignment ------------------------------------ #

    async def list_driver_candidates(
        self, case_id: str, actor_user_id: str
    ) -> List[DriverCandidate]:
        """Who the driver may be handed to. Creator or driver only."""
        case = await self._case_for_driver_governance(case_id, actor_user_id)
        return await self._driver_candidates(case)

    async def reassign_driver(
        self, case_id: str, actor_user_id: str, target_user_id: str
    ) -> Case:
        """Hand the case to ``target_user_id`` (ADR-020 D4).

        Versioned: the write is a compare-and-swap on ``cases.version`` that
        bumps it, so a turn in flight fails with a version conflict, and a
        concurrent writer makes this reload and re-decide every rule. Naming
        the creator stores NULL. Naming the current driver changes nothing and
        writes no audit row.

        Raises:
            NotFoundError: the case is absent or the caller cannot read it.
            AuthorizationError: the caller reads it but is neither its creator
                nor its effective driver.
            ConflictError: the case is terminal (``CASE_TERMINAL``), or the
                version race was lost repeatedly (``CASE_VERSION_CONFLICT``).
            ValidationException: the target is not a candidate (422).
        """
        for _ in range(_REASSIGN_ATTEMPTS):
            case = await self._case_for_driver_governance(case_id, actor_user_id)
            if case.state.is_terminal:
                raise ConflictError(
                    f"Case {case_id} is {case.state.value}; its driver no "
                    "longer changes",
                    resource_type="Case",
                    resource_id=case_id,
                    conflict_reason="case_terminal",
                    error_code=CASE_TERMINAL,
                )
            candidates = {c.user_id for c in await self._driver_candidates(case)}
            if target_user_id not in candidates:
                raise ValidationException(
                    "driver_id: not a candidate for this case's driver"
                )
            if target_user_id == case.effective_driver_id:
                return case
            stored = None if target_user_id == case.user_id else target_user_id
            version = await self.repository.reassign_driver(
                case_id,
                driver_id=stored,
                expected_version=case.version,
                change=CaseDriverChange(
                    case_id=case_id,
                    enterprise_id=case.enterprise_id,
                    from_driver_id=case.effective_driver_id,
                    to_driver_id=target_user_id,
                    reason=CaseDriverChangeReason.REASSIGNED,
                    actor_user_id=actor_user_id,
                ),
            )
            if version is not None:
                logger.info(
                    "Case %s driver %s -> %s by %s",
                    case_id,
                    case.effective_driver_id,
                    target_user_id,
                    actor_user_id,
                )
                # Answered from a fresh read, not by editing the loaded object:
                # that object may be one a turn in flight is holding, and it
                # must keep the version it was loaded at to conflict as it
                # should.
                return await self.repository.get(case_id) or case
        raise ConflictError(
            f"Case {case_id} changed while reassigning its driver; reload and " "retry",
            resource_type="Case",
            resource_id=case_id,
            conflict_reason="concurrent_update",
            error_code=CASE_VERSION_CONFLICT,
        )

    # -- D3: releases -------------------------------------------------------- #

    async def _release(
        self,
        driven: DrivenCase,
        reason: CaseDriverChangeReason,
        actor_user_id: Optional[str],
    ) -> bool:
        return await self.repository.release_driver(
            driven.case_id,
            driver_id=driven.driver_id,
            change=CaseDriverChange(
                case_id=driven.case_id,
                enterprise_id=driven.enterprise_id,
                from_driver_id=driven.driver_id,
                to_driver_id=driven.creator_id,
                reason=reason,
                actor_user_id=actor_user_id,
            ),
        )

    async def _still_reads_without(
        self,
        *,
        case_id: str,
        driver_id: str,
        lost_team_ids: Set[str],
        driver_team_ids: Set[str],
    ) -> bool:
        """Whether ``driver_id`` keeps a read path to ``case_id`` once
        ``lost_team_ids`` stop counting: another team the case is shared with
        that the driver is in."""
        case_teams = set(await self.get_case_team_ids(case_id))
        return bool((case_teams - lost_team_ids) & (driver_team_ids - lost_team_ids))

    async def release_driver_before_unshare(
        self, case: Case, team_id: str, actor_user_id: str
    ) -> bool:
        """``case`` is about to be unshared from ``team_id``: release its
        driver iff that share is the driver's last read path (ADR-020 D3)."""
        if not case.driver_id or case.driver_id == case.user_id:
            return False
        driver_teams = set(await self._resolve_user_team_ids(case.driver_id))
        if await self._still_reads_without(
            case_id=case.case_id,
            driver_id=case.driver_id,
            lost_team_ids={team_id},
            driver_team_ids=driver_teams,
        ):
            return False
        return await self._release(
            DrivenCase(
                case_id=case.case_id,
                enterprise_id=case.enterprise_id,
                creator_id=case.user_id,
                driver_id=case.driver_id,
            ),
            CaseDriverChangeReason.UNSHARED,
            actor_user_id,
        )

    async def release_driver_before_team_leave(
        self, *, enterprise_id: str, team_id: str, user_id: str
    ) -> int:
        """``user_id`` is about to leave ``team_id`` (``ICaseDriverRelease``)."""
        driver_teams = set(await self._resolve_user_team_ids(user_id))
        released = 0
        for driven in await self.repository.list_cases_driven_by(user_id):
            if driven.enterprise_id != enterprise_id or driven.creator_id == user_id:
                continue
            if team_id not in await self.get_case_team_ids(driven.case_id):
                continue
            if await self._still_reads_without(
                case_id=driven.case_id,
                driver_id=user_id,
                lost_team_ids={team_id},
                driver_team_ids=driver_teams,
            ):
                continue
            if await self._release(driven, CaseDriverChangeReason.LEFT_TEAM, user_id):
                released += 1
        return released

    async def release_driver_before_deactivation(
        self, *, user_id: str, actor_user_id: Optional[str]
    ) -> int:
        """``user_id`` is about to be deactivated (``ICaseDriverRelease``)."""
        released = 0
        for driven in await self.repository.list_cases_driven_by(user_id):
            if await self._release(
                driven, CaseDriverChangeReason.DEACTIVATED, actor_user_id
            ):
                released += 1
        return released
