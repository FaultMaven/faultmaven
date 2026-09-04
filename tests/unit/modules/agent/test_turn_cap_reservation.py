"""Where the turn cap is charged, and what has already happened by then.

The cap moved out of a route dependency and into
``InvestigationService.process_turn`` (ADR-016 D5.3, review C1). The position is
the decision, so it is what this module asserts:

* a turn is charged **after** the case is loaded and the access check has
  passed, so a request for a case that does not exist — or one belonging to
  another tenant — costs the caller nothing, and a cross-tenant probe cannot
  make the *prober* pay;
* it is charged **before** attachment preprocessing, so a capped tenant does not
  have its files classified, extracted and written to storage for a turn that
  will not run;
* the invariant then holds for **every** caller of the service rather than for
  every caller that remembered a dependency.

Driven against the real ``TurnCapService`` over the in-memory ledger, with the
engine and the repository faked — the same enforcement the deployment runs,
without a database.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from faultmaven.core.investigation.schemas import TurnPayload
from faultmaven.exceptions import NotFoundError, PermissionDeniedException
from faultmaven.infrastructure.protection.tenant_turn_cap import (
    CapPolicyResolver,
    InMemoryTurnLedger,
    TenantTurnCapExceeded,
    TurnCapService,
    utc_day,
)

pytestmark = pytest.mark.unit

ORG = "org-personal"
OWNER = "user-owner"
STRANGER = "user-stranger"
#: ``case_`` plus 12 hex — the shape the repo's own factories use, and
#: exactly the 17 characters the model pins.
CASE_ID = f"case_{uuid4().hex[:12]}"


class _Orgs:
    def __init__(self, cap=None):
        self.cap = cap

    async def get_organization(self, organization_id):
        return SimpleNamespace(organization_id=organization_id, daily_turn_cap=self.cap)


class _People:
    async def is_personal_organization(self, organization_id):
        return True


def _cap_service(ledger, *, override=None, default=30):
    return TurnCapService(
        CapPolicyResolver(
            _People(),
            _Orgs(override),
            default_limit=lambda: default,
            multi_tenant=lambda: True,
        ),
        ledger,
    )


@pytest.fixture(autouse=True)
def bound_tenant():
    from faultmaven.config.constants import STANDALONE_ORG_ID
    from faultmaven.config.tenant_context import set_current_org_id

    set_current_org_id(ORG)
    yield
    set_current_org_id(STANDALONE_ORG_ID)


def _case(**overrides):
    from faultmaven.modules.case.contracts import CaseState
    from faultmaven.modules.case.domain.models import Case

    defaults = dict(
        case_id=CASE_ID,
        title="Probe",
        description="",
        user_id=OWNER,
        organization_id=ORG,
        state=CaseState.INQUIRY,
        current_turn=0,
    )
    defaults.update(overrides)
    return Case(**defaults)


def _service(ledger, *, case=None, override=None):
    from faultmaven.modules.agent.domain.services.investigation_service import (
        InvestigationService,
    )

    engine = MagicMock()
    engine.llm_provider = MagicMock()
    repository = MagicMock()
    repository.get = AsyncMock(return_value=case)

    preprocessing = MagicMock()
    preprocessing.touched = []

    service = InvestigationService(
        milestone_engine=engine,
        case_repository=repository,
        preprocessing_service=preprocessing,
        file_storage_service=MagicMock(),
        turn_cap=_cap_service(ledger, override=override),
    )
    # Everything past the reservation is out of scope here; the cases below all
    # end at or before it.
    service._preprocess_attachment = AsyncMock(
        side_effect=AssertionError("preprocessing ran for a capped turn")
    )
    return service


async def test_a_missing_case_costs_the_tenant_nothing():
    """404 before the ledger. A route-level guard charged a unit for this."""
    ledger = InMemoryTurnLedger()
    service = _service(ledger, case=None)

    with pytest.raises(NotFoundError):
        await service.process_turn(CASE_ID, OWNER, TurnPayload(query="hello"))

    assert await ledger.usage(ORG, utc_day()) == 0


async def test_a_case_the_caller_may_not_touch_costs_the_prober_nothing():
    """The two-tenant probe's B→A turn attempt must leave no row for B.

    Before the move this was a real defect and not a hypothetical: the guard ran
    as a route dependency, ahead of the access check, so a tenant probing
    another tenant's case id spent its own allowance doing it — and could spend
    somebody else's day by probing on their behalf if the guard had keyed the
    other way.
    """
    ledger = InMemoryTurnLedger()
    service = _service(ledger, case=_case(user_id=OWNER))

    with pytest.raises(PermissionDeniedException):
        await service.process_turn(CASE_ID, STRANGER, TurnPayload(query="hello"))

    assert await ledger.usage(ORG, utc_day()) == 0


async def test_a_capped_tenant_is_refused_before_any_attachment_is_processed():
    """Charged before STEP 1, so a refused turn writes no files and no evidence."""
    ledger = InMemoryTurnLedger()
    service = _service(ledger, case=_case(), override=1)

    await ledger.reserve(ORG, utc_day(), None)  # the day's one turn, already spent

    with pytest.raises(TenantTurnCapExceeded):
        await service.process_turn(CASE_ID, OWNER, TurnPayload(query="hello"))

    # ``_preprocess_attachment`` is rigged to fail loudly if it runs; the
    # refusal above is what proves it did not.
    assert await ledger.usage(ORG, utc_day()) == 1


async def test_the_refusal_carries_the_message_and_the_reset_instant():
    ledger = InMemoryTurnLedger()
    service = _service(ledger, case=_case(), override=2)
    await ledger.reserve(ORG, utc_day(), None)
    await ledger.reserve(ORG, utc_day(), None)

    with pytest.raises(TenantTurnCapExceeded) as raised:
        await service.process_turn(CASE_ID, OWNER, TurnPayload(query="hello"))

    assert raised.value.limit == 2
    assert "2" in raised.value.user_message
    assert "UTC" in raised.value.user_message
    assert raised.value.retry_after_seconds >= 1


def _unconfigured_service():
    from faultmaven.modules.agent.domain.services.investigation_service import (
        InvestigationService,
    )

    engine = MagicMock()
    engine.llm_provider = MagicMock()
    return InvestigationService(milestone_engine=engine, case_repository=MagicMock())


async def test_an_uninjected_cap_is_uncapped_under_single_tenant():
    """Every caller that builds the service directly must keep working.

    A great many unit tests construct ``InvestigationService`` themselves, and
    none of them should have to know the cap exists. Under single-tenant the
    fallback answers "uncapped" from the deployment mode — no port, no database
    — which is exactly what the real policy answers there too.
    """
    from faultmaven.infrastructure.protection.tenant_turn_cap import UnconfiguredTurnCap

    service = _unconfigured_service()
    assert service._turn_cap is None
    fallback = service.turn_cap
    assert isinstance(fallback, UnconfiguredTurnCap)
    assert service.turn_cap is fallback, "the lazy default must be built once"

    reservation = await fallback.reserve(ORG)
    assert reservation.limit is None
    assert reservation.source == "single_tenant"


async def test_an_uninjected_cap_refuses_under_multi_tenant(monkeypatch):
    """The other half: a wiring mistake where a bill exists fails CLOSED.

    The composition root always injects a real service in a multi-tenant
    deployment, so reaching this is a mistake rather than a mode. A silent
    "uncapped" there would be the whole feature quietly absent; a 503 is
    visible on the first turn.
    """
    from faultmaven.infrastructure.protection import tenant_turn_cap as module
    from faultmaven.infrastructure.protection.tenant_turn_cap import (
        TenantTurnCapUnavailable,
        UnconfiguredTurnCap,
    )

    monkeypatch.setattr(module, "_is_multi_tenant", lambda: True)
    with pytest.raises(TenantTurnCapUnavailable):
        await UnconfiguredTurnCap().reserve(ORG)
