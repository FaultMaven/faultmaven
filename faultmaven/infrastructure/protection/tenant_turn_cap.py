"""The per-tenant investigation-turn cap (ADR-016 D5.3).

Self-service sign-up hands anyone who can authenticate an organization of their
own, and an investigation turn is the one operation in the product that spends
LLM compute without a further gate. ADR-016 D5 therefore sequences a per-tenant
usage cap ahead of opening sign-up: *"Open sign-up plus uncapped compute is an
open bill."* This module is that cap's mechanism; the reservation is taken from
``InvestigationService.process_turn``, after the case is loaded, the access
check has passed and the payload is known to be a turn.

What it counts, and what it does not
------------------------------------
**Turns per organization per UTC calendar day, as a count.** Not tokens: the
owner tunes the number against measured usage, and a count is the only unit a
refusal can state honestly to the person it refuses ("30 turns today; resets at
00:00 UTC"). Not a rolling window, for the same reason — a sliding 24 h window
cannot promise a reset instant, so the message would have to lie or say nothing.

It is not the session rate limiter and does not interact with it. That limiter
meters request *rate* per session to protect the service from a burst; this
meters *volume* per tenant per day to bound a bill. A capped tenant keeps
reading: since fm#994 reads sit on their own per-session windows, so a refused
turn cannot exhaust the quota a caller needs to look at its own cases.

Which tenants are capped
------------------------
======================  ==================================================
tenant                  cap
======================  ==================================================
single-tenant           **uncapped, decided without a query** — see below
personal, no override   the deployment default (``TENANT_DAILY_TURN_CAP``)
company, no override    **uncapped** — the cap bounds self-service, not
                        customers
either, override ``0``  explicitly uncapped
either, override ``N``  N turns per UTC day
======================  ==================================================

**Single-tenant is answered before any port is touched.** A self-hosted install
has one organization, pays for its own compute, and is not what D5.3 exists to
bound — and deciding it from the deployment mode rather than from a table means
an install that has not run migration 053 (``RUN_STARTUP_MIGRATIONS=false``, a
supported posture) keeps working instead of losing every turn to a
usage-allowance message it could never have earned.

"Personal" is not a flag on the organization: it is the existence of a row in
``sso_personal_orgs`` naming it (#1045), asked through
``ISSOPersonalOrgRepository.is_personal_organization``. That table is untenanted,
so the question is answerable whatever tenant is bound, and there is exactly one
writer of it — the just-in-time provisioning path.

Failure direction, stated once
------------------------------
**Ambiguity caps.** Whichever of the two questions cannot be answered — the
tenant's kind, or its override — the tenant is treated as capped at
``TENANT_DAILY_TURN_CAP``. An unreadable *override* is emphatically not "no
override": a company tenant carrying an override of 50 would otherwise degrade
to **uncapped** on a failed read, which is the exact inversion D5.3 exists to
prevent, and it is reachable wherever the failure does not abort the surrounding
transaction.

**A ledger failure refuses the turn.** If the reservation cannot be written the
cap cannot be honoured, so the turn is refused rather than served uncounted.
This costs nothing in availability terms *for the deployments it applies to*:
the ledger lives in the same database a multi-tenant turn must read its case
from, so a database that cannot take this write cannot serve the turn either.

Reservation, not billing
------------------------
The unit is consumed when the turn is accepted — after the case load, the access
check and the route's validation, and before the model runs — and is not
refunded if the turn later fails. Refunding would be a free-retry channel for
anyone who can make a turn fail, and the compute a failed turn spent is real. A
turn refused *by this cap* consumes nothing: that is the atomic statement's own
property, not a second check.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Dict, Optional, Protocol, Tuple

from faultmaven.infrastructure.protection.window_math import retry_after_seconds

logger = logging.getLogger(__name__)

#: The override value meaning "explicitly uncapped, whatever kind of tenant this
#: is". Zero rather than a negative number or NULL: NULL already means "no
#: override" (fall back to the kind-dependent policy), so unlimited needs a
#: distinct, non-negative spelling — and the column's CHECK forbids negatives so
#: it cannot acquire a second one.
UNLIMITED_OVERRIDE = 0


class TenantTurnCapError(Exception):
    """Base for the two ways this module refuses a turn."""


class TenantTurnCapExceeded(TenantTurnCapError):
    """The tenant has used its allowance for this UTC day.

    Carries everything the refusal has to state — the limit, what has been used,
    and the instant the allowance returns — so the message is rendered once,
    here, rather than assembled again by every caller.
    """

    def __init__(
        self,
        *,
        organization_id: str,
        limit: int,
        used: int,
        reset_at: datetime,
        source: str = "unknown",
    ):
        self.organization_id = organization_id
        self.limit = limit
        self.used = used
        self.reset_at = reset_at
        #: Where the limit came from — an override, the default, or the
        #: fail-closed indeterminate branch. Carried so the refusal log answers
        #: the operator's next question ("is this tenant on a custom cap?")
        #: without a query.
        self.source = source
        super().__init__(
            f"organization {organization_id} reached its daily turn cap "
            f"({used}/{limit}); resets at {reset_at.isoformat()}"
        )

    @property
    def retry_after_seconds(self) -> int:
        """Whole seconds until the allowance returns, never below one.

        Through ``window_math.retry_after_seconds`` — the same rounding the rate
        limiter's waits go through — rather than a second arithmetic of its own.
        Two ways to compute "how long until this instant" in one codebase is how
        one of them starts answering zero.
        """
        return retry_after_seconds(
            self.reset_at.timestamp(), datetime.now(timezone.utc).timestamp()
        )

    @property
    def user_message(self) -> str:
        """What the person who submitted the turn is told.

        States the limit, that it is a daily one, and when it comes back — in
        UTC, because that is the boundary the ledger actually uses, and a local
        rendering would be a guess about a client this layer cannot see.
        """
        return (
            f"You have used all {self.limit} investigation turns available to "
            f"this workspace today. Your allowance resets at "
            f"{self.reset_at.strftime('%H:%M UTC on %d %b %Y')}. "
            "Reading cases, transcripts and the knowledge base is unaffected."
        )


class TenantTurnCapUnavailable(TenantTurnCapError):
    """The cap could not be honoured, so the turn is refused.

    Distinct from :class:`TenantTurnCapExceeded` because it is not the user's
    doing and the wait is not until midnight — telling somebody their daily
    allowance is spent when the ledger merely failed to write would be a false
    statement about their own account.
    """


@dataclass(frozen=True)
class CapPolicy:
    """The cap in force for one organization, and where it came from.

    ``limit is None`` means uncapped. ``source`` is carried for the log line, so
    an operator reading a refusal can tell an override from the default without
    querying anything.
    """

    limit: Optional[int]
    source: str


@dataclass(frozen=True)
class Reservation:
    """A turn admitted against the cap."""

    organization_id: str
    used: int
    limit: Optional[int]
    source: str


def utc_day(now: Optional[datetime] = None) -> date:
    """The UTC calendar day a turn is charged to."""
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date()


def next_utc_midnight(now: Optional[datetime] = None) -> datetime:
    """The instant the current UTC day's allowance returns.

    Midnight *after* the moment given, always in the future: a turn refused at
    23:59:59.9 is told to wait a fraction of a second, which is the truth, and a
    turn refused at 00:00:00 is told to wait a full day, which is also the truth.
    """
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return datetime.combine(
        moment.date() + timedelta(days=1), time.min, tzinfo=timezone.utc
    )


# =============================================================================
# Ports
# =============================================================================


class PersonalOrgLookup(Protocol):
    """The one question this module asks of the auth module's SSO port."""

    async def is_personal_organization(self, organization_id: str) -> bool: ...


class OrganizationLookup(Protocol):
    """The one question this module asks of the organization repository."""

    async def get_organization(self, organization_id: str): ...


class ITurnLedger(ABC):
    """Where accepted turns are counted, per organization per UTC day.

    A port rather than a module-level function so the reservation can be driven
    in a test without a database — and so the *enforcement* is what those tests
    exercise, rather than being blanked out because it happens to need one.
    """

    @abstractmethod
    async def reserve(
        self, organization_id: str, day: date, limit: Optional[int]
    ) -> Optional[int]:
        """Charge one turn, refusing at ``limit``.

        Returns the new count, or ``None`` when the day already stands at or
        above ``limit`` — in which case **nothing is written**. A ``limit`` of
        ``None`` records usage without a ceiling and therefore always returns a
        count. Implementations must make the check and the increment one
        indivisible step: two turns arriving together at the boundary must not
        both be admitted.
        """

    @abstractmethod
    async def usage(self, organization_id: str, day: date) -> int:
        """What the ledger holds for this organization on ``day``."""


class InMemoryTurnLedger(ITurnLedger):
    """A ledger in a dict, for tests and for nothing else.

    Same contract as the SQL one, including the refusal-writes-nothing property,
    so a test that drives the real ``CapPolicyResolver`` and the real service
    against this is exercising the enforcement rather than a stand-in for it.
    Not concurrency-safe across processes and not meant to be: the property that
    needs a real database — two concurrent reservations at the boundary — is
    asserted against PostgreSQL in the integration module.
    """

    def __init__(self) -> None:
        self._counts: Dict[Tuple[str, date], int] = {}

    async def reserve(
        self, organization_id: str, day: date, limit: Optional[int]
    ) -> Optional[int]:
        standing = self._counts.get((organization_id, day), 0)
        if limit is not None and standing >= limit:
            return None
        self._counts[(organization_id, day)] = standing + 1
        return standing + 1

    async def usage(self, organization_id: str, day: date) -> int:
        return self._counts.get((organization_id, day), 0)


class SqlTurnLedger(ITurnLedger):
    """The shipped ledger: one row per (organization, UTC day) in Postgres.

    The reservation is a single statement — ``INSERT … ON CONFLICT … DO UPDATE
    SET turn_count = turn_count + 1 WHERE turn_count < :cap RETURNING
    turn_count`` — so the check and the increment cannot interleave and an empty
    ``RETURNING`` *is* the refusal.

    A table rather than a Redis counter because ADR-016 D5.3 requires the cap to
    fail **closed**, and a counter whose store can be unavailable fails open: a
    Redis blip would silently un-cap every tenant until it healed.
    """

    async def reserve(
        self, organization_id: str, day: date, limit: Optional[int]
    ) -> Optional[int]:
        from faultmaven.infrastructure.persistence.database import get_db_session
        from faultmaven.infrastructure.persistence.db_compat import dialect_insert
        from faultmaven.infrastructure.persistence.models import (
            OrganizationTurnUsageModel,
        )

        table = OrganizationTurnUsageModel
        async with get_db_session() as session:
            statement = dialect_insert(session, table).values(
                organization_id=organization_id, usage_date=day, turn_count=1
            )
            conflict = {
                "index_elements": ["organization_id", "usage_date"],
                "set_": {"turn_count": table.turn_count + 1},
            }
            if limit is not None:
                conflict["where"] = table.turn_count < limit
            statement = statement.on_conflict_do_update(**conflict).returning(
                table.turn_count
            )
            value = (await session.execute(statement)).scalar_one_or_none()
        return None if value is None else int(value)

    async def usage(self, organization_id: str, day: date) -> int:
        from sqlalchemy import select

        from faultmaven.infrastructure.persistence.database import get_db_session
        from faultmaven.infrastructure.persistence.models import (
            OrganizationTurnUsageModel,
        )

        async with get_db_session() as session:
            value = (
                await session.execute(
                    select(OrganizationTurnUsageModel.turn_count).where(
                        OrganizationTurnUsageModel.organization_id == organization_id,
                        OrganizationTurnUsageModel.usage_date == day,
                    )
                )
            ).scalar_one_or_none()
        return int(value or 0)


# =============================================================================
# Policy
# =============================================================================


def _default_limit() -> int:
    """The deployment default, read at the point of use.

    Through ``get_settings()`` rather than captured at import, so the value the
    cap enforces is the one the process is configured with rather than whatever
    was in the environment when this module first loaded. That is not a
    live-reload claim — ``get_settings()`` is a process singleton — it is what
    keeps the setting from becoming a documented knob nothing reads.
    """
    from faultmaven.config.settings import get_settings

    return get_settings().agent.tenant_daily_turn_cap


def _is_multi_tenant() -> bool:
    """Whether this deployment has more than one tenant to bound.

    Read through the same factory predicate every other tenancy decision uses,
    so "is this multi-tenant?" has one answer in the process rather than a copy
    here that could disagree with the RLS binder.
    """
    from faultmaven.providers.tenancy.factory import (
        BUILTIN_MULTI,
        requested_tenant_provider,
    )

    return requested_tenant_provider() == BUILTIN_MULTI


class CapPolicyResolver:
    """Decides the cap in force for one organization, through the ports.

    Shared by the enforcement path and by ``fm-set-turn-cap --show``, which is
    the point: an operator reading a tenant's cap sees the rule the next turn
    will actually meet, not a second description of it that can drift. It also
    means the CLI inherits the repository's ``deleted_at`` filter and stops
    resolving soft-deleted organizations.

    Neither port is reached under single-tenant.
    """

    def __init__(
        self,
        personal_orgs: PersonalOrgLookup,
        organizations: OrganizationLookup,
        *,
        default_limit=_default_limit,
        multi_tenant=_is_multi_tenant,
    ) -> None:
        self._personal_orgs = personal_orgs
        self._organizations = organizations
        self._default_limit = default_limit
        self._multi_tenant = multi_tenant

    async def resolve(self, organization_id: str) -> CapPolicy:
        """The cap in force. Never raises; ambiguity resolves to the default."""
        if not self._multi_tenant():
            return CapPolicy(limit=None, source="single_tenant")

        if not organization_id:
            # Multi-tenant with no bound tenant. Unreachable through the front
            # door — ``bind_request_org_context`` refuses an unscoped request
            # before any route runs — and guarded anyway, because this must not
            # be the place that decides an unscoped request is free.
            logger.warning("turn cap: no tenant bound; applying the default cap")
            return CapPolicy(limit=self._default_limit(), source="indeterminate")

        try:
            organization = await self._organizations.get_organization(organization_id)
        except Exception as exc:
            # Fail CLOSED, and this is the branch the review corrected. Reading
            # an unreadable override as "no override" would drop a company
            # tenant carrying an explicit cap of 50 all the way to *uncapped* —
            # the exact inversion this cap exists to prevent, on the path where
            # the failure does not abort the surrounding transaction.
            logger.warning(
                "turn cap: override lookup failed for organization %s (%s); "
                "applying the default cap",
                organization_id,
                type(exc).__name__,
            )
            return CapPolicy(limit=self._default_limit(), source="indeterminate")

        override = (
            getattr(organization, "daily_turn_cap", None) if organization else None
        )
        if override is not None:
            if override == UNLIMITED_OVERRIDE:
                return CapPolicy(limit=None, source="override_unlimited")
            return CapPolicy(limit=int(override), source="override")

        try:
            is_personal = await self._personal_orgs.is_personal_organization(
                organization_id
            )
        except Exception as exc:
            # The same direction, for the same reason: an un-migrated or
            # partially-migrated deployment is the live shape of this branch,
            # and reading it as "company" would hand every tenant an uncapped
            # day the moment the table became unreadable.
            logger.warning(
                "turn cap: cannot determine whether organization %s is a "
                "personal tenant (%s); applying the default cap",
                organization_id,
                type(exc).__name__,
            )
            return CapPolicy(limit=self._default_limit(), source="indeterminate")

        if is_personal:
            return CapPolicy(limit=self._default_limit(), source="default_personal")
        return CapPolicy(limit=None, source="company_uncapped")


class TurnCapService:
    """Reserves one turn against a tenant's daily allowance, or refuses."""

    def __init__(self, resolver: CapPolicyResolver, ledger: ITurnLedger) -> None:
        self._resolver = resolver
        self._ledger = ledger

    async def reserve(
        self, organization_id: str, *, now: Optional[datetime] = None
    ) -> Reservation:
        """Admit one turn for ``organization_id``, or refuse it.

        ``now`` is sampled **once** and both the charged day and the reset
        instant are derived from it. Two samples would let a turn refused at
        23:59:59.98 be charged to day D and told to come back at the midnight
        after D+1 — a whole extra day's wait, produced by nothing but the clock
        moving between two calls.

        Raises:
            TenantTurnCapExceeded: the day's allowance is spent. Nothing was
                written, so the refusal itself does not consume a turn.
            TenantTurnCapUnavailable: the cap could not be applied at all.
        """
        moment = now or datetime.now(timezone.utc)
        day = utc_day(moment)

        policy = await self._resolver.resolve(organization_id)
        if policy.limit is None and policy.source == "single_tenant":
            # Decided without touching a port, so a single-tenant deployment
            # that has never run migration 053 keeps serving turns.
            return Reservation(
                organization_id, used=0, limit=None, source=policy.source
            )

        try:
            used = await self._ledger.reserve(organization_id, day, policy.limit)
            standing = (
                await self._ledger.usage(organization_id, day) if used is None else 0
            )
        except Exception as exc:
            logger.error(
                "turn cap: could not reserve a turn for organization %s (%s: %s); "
                "refusing the turn rather than serving it uncounted",
                organization_id,
                type(exc).__name__,
                exc,
            )
            raise TenantTurnCapUnavailable(str(exc)) from exc

        if used is None:
            # ``policy.limit`` cannot be None here: the ledger port's contract
            # is that a ``None`` limit always returns a count, and both
            # implementations honour it. Stated rather than branched on — a
            # guard for a state the contract forbids is a branch no test can
            # reach and no reader can trust.
            #
            # The refusal, logged where both callers reach it. INFO rather
            # than WARNING: a tenant meeting a cap that exists to be met is the
            # mechanism working, and a WARNING would train operators to ignore
            # the channel. The organization and the count are both here because
            # the operator's next action — raise this tenant's cap, or leave it
            # — needs both.
            logger.info(
                "turn cap: refused a turn for organization %s (%s/%s used today, "
                "policy=%s); resets at %s",
                organization_id,
                standing,
                policy.limit,
                policy.source,
                next_utc_midnight(moment).isoformat(),
            )
            raise TenantTurnCapExceeded(
                organization_id=organization_id,
                limit=policy.limit,
                # What the ledger actually holds, not the limit: a cap lowered
                # mid-day leaves a count ABOVE it, and reporting the limit would
                # quietly rename that state.
                used=standing,
                reset_at=next_utc_midnight(moment),
                source=policy.source,
            )

        return Reservation(
            organization_id=organization_id,
            used=used,
            limit=policy.limit,
            source=policy.source,
        )


class UnconfiguredTurnCap:
    """The default when nobody injected a cap: safe under single-tenant, closed elsewhere.

    ``InvestigationService`` is constructed directly by a great many tests and
    by any caller that does not go through the composition root. Those callers
    must not have to know the cap exists — and must not silently escape it
    either. So the fallback is deployment-aware in exactly the way the real
    policy is:

    * **single-tenant** — uncapped, decided from the deployment mode. A
      self-hosted install is not what D5.3 bounds, so a service built without a
      cap behaves identically to one built with the real thing.
    * **multi-tenant** — refuses, with a message naming the cause. This is the
      only shape where a bill exists, and the composition root always injects a
      real service there, so reaching this is a wiring mistake. Failing closed
      turns that mistake into a visible 503 rather than an invisible hole.

    It composes nothing, which is the other half of its job: the shared
    mechanism module must not reach into the auth module's *infrastructure* to
    build an adapter (import-linter contract 10). Where the concrete ports are
    chosen is the composition root's business.
    """

    async def reserve(
        self, organization_id: str, *, now: Optional[datetime] = None
    ) -> Reservation:
        if not _is_multi_tenant():
            return Reservation(
                organization_id, used=0, limit=None, source="single_tenant"
            )
        logger.error(
            "turn cap: no cap service was wired into this InvestigationService, "
            "and this is a multi-tenant deployment; refusing the turn"
        )
        raise TenantTurnCapUnavailable(
            "no per-tenant turn cap is configured for this multi-tenant deployment"
        )
