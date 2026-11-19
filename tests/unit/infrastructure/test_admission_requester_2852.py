"""The admission key a bound requester gives (#2852, Principle 14).

``admission_principal`` turns the bound account into ``<account>:<requester>``
and leaves every other principal, and every principal outside a binding, as it
is. Each of the four ``FairSlots`` users acquires and releases that one key.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from faultmaven.config.tenant_context import bind_admission_requester
from faultmaven.infrastructure import embedding_guard, worker_process
from faultmaven.infrastructure.worker_process import FairSlots, admission_principal
from faultmaven.modules.knowledge.domain.services import (
    document_preprocessor,
    runbook_gate,
)
from tests.unit.infrastructure.test_fair_slots_2645 import _LOGS, _tier1_service

pytestmark = [pytest.mark.unit]

ACCOUNT, REQUESTER = "svc-slack", "r" * 32
COMPOUND = f"{ACCOUNT}:{REQUESTER}"


@pytest.fixture(autouse=True)
def _unbound():
    bind_admission_requester(None)
    yield
    bind_admission_requester(None)


def test_the_key_is_the_principal_unless_its_account_is_bound():
    assert admission_principal(ACCOUNT) == ACCOUNT
    bind_admission_requester(ACCOUNT, REQUESTER)
    assert admission_principal(ACCOUNT) == COMPOUND
    assert admission_principal("someone-else") == "someone-else"
    bind_admission_requester(None)
    assert admission_principal(ACCOUNT) == ACCOUNT


@pytest.fixture
def keys(monkeypatch):
    """Each site's acquired and released keys, through the real slots."""
    seen = {"acquired": [], "released": []}
    acquire, release = FairSlots.acquire, FairSlots.release

    async def acquiring(self, principal):
        seen["acquired"].append(principal)
        await acquire(self, principal)

    def releasing(self, principal):
        seen["released"].append(principal)
        release(self, principal)

    async def failing_run(*_args, **_kwargs):
        raise worker_process.WorkerFailed("no worker in this test")

    monkeypatch.setattr(FairSlots, "acquire", acquiring)
    monkeypatch.setattr(FairSlots, "release", releasing)
    monkeypatch.setattr(worker_process, "run", failing_run)
    return seen


async def _tier1():
    await _tier1_service().classify_and_extract(
        content=_LOGS, filename="x.log", principal=ACCOUNT
    )


async def _preparation(monkeypatch, tmp_path):
    async def prepared(*_args):
        return "prepared"

    monkeypatch.setattr(document_preprocessor, "_run_worker", prepared)
    await document_preprocessor._prepare(tmp_path / "a.md", "text/markdown", ACCOUNT)


async def _gate():
    with pytest.raises(runbook_gate.GateUnavailable):
        await runbook_gate.avalidate_and_score("# Runbook", principal=ACCOUNT)


async def _token_count():
    assert await embedding_guard.count_index_tokens(["a"], principal=ACCOUNT) is None


SITES = {
    "tier1": lambda mp, tmp: _tier1(),
    "preparation": _preparation,
    "gate": lambda mp, tmp: _gate(),
    "token_count": lambda mp, tmp: _token_count(),
}


@pytest.mark.parametrize("bound", [False, True], ids=["unbound", "bound"])
@pytest.mark.parametrize("site", sorted(SITES))
async def test_each_site_releases_the_key_it_acquired(
    site, bound, keys, monkeypatch, tmp_path
):
    if bound:
        bind_admission_requester(ACCOUNT, REQUESTER)
    await SITES[site](monkeypatch, tmp_path)
    expected = COMPOUND if bound else ACCOUNT
    assert keys == {"acquired": [expected], "released": [expected]}


async def test_work_the_request_starts_keeps_its_key(keys, monkeypatch):
    """The runbook conversion a turn spawns (``runbook_creation``, a task
    created in the request's context) is admitted under the compound key after
    the request has unbound: that person asked for it (#2852)."""
    from faultmaven.core.investigation.milestone_engine.runbook_creation import (
        RunbookCreator,
    )

    async def convert_from_case(*, request, user_id, enterprise_id):
        await runbook_gate.avalidate_and_score("# Runbook", principal=user_id)

    repository = SimpleNamespace(get=lambda _case_id: asyncio.sleep(0))
    creator = RunbookCreator(
        case_locks={"case-1": asyncio.Lock()},
        deps=SimpleNamespace(repository=repository),
    )
    committed = asyncio.get_running_loop().create_future()
    bind_admission_requester(ACCOUNT, REQUESTER)
    task = asyncio.create_task(
        creator._run_runbook_conversion(
            SimpleNamespace(convert_from_case=convert_from_case),
            SimpleNamespace(case_id="case-1"),
            ACCOUNT,
            "ent-1",
            committed=committed,
        )
    )
    bind_admission_requester(None)  # the next request on this task
    committed.set_result(None)
    await task
    assert keys == {"acquired": [COMPOUND], "released": [COMPOUND]}
