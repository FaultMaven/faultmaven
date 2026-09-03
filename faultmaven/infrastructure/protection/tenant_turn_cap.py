"""The per-tenant investigation-turn cap (ADR-016 D5.3).

Self-service sign-up hands anyone who can authenticate an organization of their
own, and an investigation turn is the one operation in the product that spends
LLM compute without a further gate. ADR-016 D5 therefore sequences a per-tenant
usage cap ahead of opening sign-up: *"Open sign-up plus uncapped compute is an
open bill."* This module is that cap.

What it counts, and what it does not
------------------------------------
**Turns per organization per UTC calendar day, as a count.** Not tokens: the
owner tunes the number against measured usage, and a count is the only unit a
refusal can state honestly to the person it refuses ("30 turns today; resets at
00:00 UTC"). Not a rolling window, for the same reason — a sliding 24 h window
cannot promise a reset instant, so the message would have to lie or say nothing.

It is emphatically **not** the session rate limiter. That limiter meters request
*rate* per session to protect the service from a burst; this meters *volume* per
tenant per day to bound a bill. They fail in different directions and answer to
different owners, which is why a refusal here is released from the limiter's
LLM-compute bucket rather than counted twice (see
``api/middleware/rate_limiting``).

Which tenants are capped
------------------------
======================  ==================================================
tenant                  cap
======================  ==================================================
personal, no override   the deployment default (``TENANT_DAILY_TURN_CAP``)
company, no override    **uncapped** — the cap bounds self-service, not
                        customers
either, override ``0``  explicitly uncapped
either, override ``N``  N turns per UTC day
======================  ==================================================

"Personal" is not a flag on the organization: it is the existence of a row in
``sso_personal_orgs`` naming it (#1045). That table is untenanted, so the
question is answerable whatever tenant is bound, and there is exactly one writer
of it — the just-in-time provisioning path. A company organization provisioned
by ``fm-provision-sso-org`` has no such row, and neither does the Standalone
sentinel of a self-hosted deployment, so both read as company and are uncapped.

Failure direction, stated once
------------------------------
**Ambiguity caps at the default.** If the personal/company question cannot be
answered — the lookup raises, the table is missing on a half-migrated
deployment — the tenant is treated as *personal* and capped at
``TENANT_DAILY_TURN_CAP``. The alternative direction (unknown ⇒ uncapped) is
precisely the shape ADR-016 D5.3 exists to prevent, and it is the one a
half-deployed release would take by accident.

**A ledger failure refuses the turn.** If the reservation itself cannot be
written, the cap cannot be honoured, so the turn is refused rather than served
uncounted. This costs nothing in availability terms: the ledger lives in the
same database the turn must read the case from, so a database that cannot take
this write cannot serve the turn either.

Reservation, not billing
------------------------
The unit is consumed when the turn is **accepted**, before the LLM runs, and is
not refunded if the turn later fails. Refunding would be a free-retry channel
for anyone who can make a turn fail, and the compute a failed turn spent is real.
A turn refused *by this cap* consumes nothing — that is the atomic statement's
whole point, not a second check.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

from sqlalchemy import select

logger = logging.getLogger(__name__)

#: The override value meaning "explicitly uncapped, whatever kind of tenant this
#: is". Zero rather than a negative number or a NULL: NULL already means "no
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

        Derived from the same instant the message names, so a client that
        honours the header and a person who reads the sentence wait for the same
        thing.
        """
        remaining = (self.reset_at - datetime.now(timezone.utc)).total_seconds()
        return max(1, int(remaining) + 1)

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

    Midnight *after* the current day, always in the future: a turn refused at
    23:59:59.9 is told to wait a fraction of a second, which is the truth, and a
    turn refused at 00:00:00 is told to wait a full day, which is also the truth.
    """
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return datetime.combine(
        moment.date() + timedelta(days=1), time.min, tzinfo=timezone.utc
    )


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


async def resolve_policy(session, organization_id: str) -> CapPolicy:
    """Decide the cap in force for ``organization_id``.

    Two independent questions, deliberately not folded into one join: *is this a
    personal tenant?* is answered from the untenanted ``sso_personal_orgs``, and
    *does it carry an override?* from its own ``organizations`` row, which RLS
    scopes to the bound tenant. Joining them would make the first question
    unanswerable whenever the second row is invisible.

    Raises nothing: an unanswerable personal/company question is resolved as
    *personal* (the fail-closed direction), with the reason in the log.
    """
    from faultmaven.infrastructure.persistence.models import (
        OrganizationModel,
        SSOPersonalOrgModel,
    )

    override: Optional[int]
    try:
        override = (
            await session.execute(
                select(OrganizationModel.daily_turn_cap).where(
                    OrganizationModel.organization_id == organization_id
                )
            )
        ).scalar_one_or_none()
    except Exception as exc:
        # Not directly exercised: the fail-closed test revokes access to
        # ``sso_personal_orgs``, not to ``organizations``, and on PostgreSQL a
        # failure here aborts the transaction so the lookup below fails too and
        # the *indeterminate* branch decides. It is written out anyway because
        # the two questions must fail in the same direction, and a bare
        # propagate here would make an override outage read as "the cap could
        # not be applied" rather than "no override".
        logger.warning(
            "turn cap: override lookup failed for organization %s (%s); "
            "falling back to the default policy",
            organization_id,
            type(exc).__name__,
        )
        override = None

    if override is not None:
        if override == UNLIMITED_OVERRIDE:
            return CapPolicy(limit=None, source="override_unlimited")
        return CapPolicy(limit=int(override), source="override")

    try:
        is_personal = (
            await session.execute(
                select(SSOPersonalOrgModel.organization_id)
                .where(SSOPersonalOrgModel.organization_id == organization_id)
                .limit(1)
            )
        ).scalar_one_or_none() is not None
    except Exception as exc:
        # Fail closed, and say so. An un-migrated or partially-migrated
        # deployment is the live shape of this branch, and the other direction
        # would hand every tenant an uncapped day the moment this table became
        # unreadable.
        logger.warning(
            "turn cap: cannot determine whether organization %s is a personal "
            "tenant (%s); applying the default cap",
            organization_id,
            type(exc).__name__,
        )
        return CapPolicy(limit=_default_limit(), source="indeterminate")

    if is_personal:
        return CapPolicy(limit=_default_limit(), source="default_personal")
    return CapPolicy(limit=None, source="company_uncapped")


async def _increment(session, organization_id: str, day: date, limit: Optional[int]):
    """Charge one turn to the ledger, refusing at ``limit``.

    One statement, both branches. ``limit is None`` records usage without a
    ceiling — a company tenant is never refused here, but its counts are what
    the default is tuned against. Otherwise the ``WHERE`` on the conflict clause
    is the enforcement: no row comes back when the day is already at the limit,
    and nothing is written.
    """
    from faultmaven.infrastructure.persistence.db_compat import dialect_insert
    from faultmaven.infrastructure.persistence.models import OrganizationTurnUsageModel

    table = OrganizationTurnUsageModel
    statement = dialect_insert(session, table).values(
        organization_id=organization_id, usage_date=day, turn_count=1
    )
    conflict = {
        "index_elements": ["organization_id", "usage_date"],
        "set_": {"turn_count": table.turn_count + 1},
    }
    if limit is not None:
        conflict["where"] = table.turn_count < limit
    statement = statement.on_conflict_do_update(**conflict).returning(table.turn_count)

    return (await session.execute(statement)).scalar_one_or_none()


async def _standing_count(session, organization_id: str, day: date) -> int:
    """What the ledger holds for this organization today.

    Only read on the refusal path, where the atomic statement deliberately
    returns nothing, so it costs a query on refusals and none on served turns.
    """
    from faultmaven.infrastructure.persistence.models import OrganizationTurnUsageModel

    value = (
        await session.execute(
            select(OrganizationTurnUsageModel.turn_count).where(
                OrganizationTurnUsageModel.organization_id == organization_id,
                OrganizationTurnUsageModel.usage_date == day,
            )
        )
    ).scalar_one_or_none()
    return int(value or 0)


async def reserve_turn(
    organization_id: str, *, now: Optional[datetime] = None
) -> Reservation:
    """Admit one turn for ``organization_id``, or refuse it.

    Raises:
        TenantTurnCapExceeded: the day's allowance is spent. Nothing was
            written, so the refusal itself does not consume a turn.
        TenantTurnCapUnavailable: the cap could not be applied at all.
    """
    from faultmaven.infrastructure.persistence.database import get_db_session

    day = utc_day(now)

    try:
        async with get_db_session() as session:
            policy = await resolve_policy(session, organization_id)
            used = await _increment(session, organization_id, day, policy.limit)
            if used is None:
                # The conflict clause refused. Read what the ledger actually
                # holds rather than reporting the limit back: a cap lowered
                # mid-day leaves a count ABOVE it, and a refusal log that
                # reported the limit would quietly rename that state.
                standing = await _standing_count(session, organization_id, day)
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
        # ``policy.limit`` cannot be None on this branch — the unlimited call
        # carries no WHERE and always returns a row — but a real check rather
        # than an ``assert``, which vanishes under ``-O`` exactly where a
        # production deployment would want it most.
        if policy.limit is None:  # pragma: no cover - unreachable by construction
            raise TenantTurnCapUnavailable("the uncapped ledger write returned no row")
        raise TenantTurnCapExceeded(
            organization_id=organization_id,
            limit=policy.limit,
            used=standing,
            reset_at=next_utc_midnight(now),
            source=policy.source,
        )

    return Reservation(
        organization_id=organization_id,
        used=int(used),
        limit=policy.limit,
        source=policy.source,
    )
