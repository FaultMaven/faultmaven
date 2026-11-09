"""``fm-backfill-case-outcome`` gives each stored case its first record (#2636).

Ruled 2026-11-09: computed once from the case as stored, a fold over no prior,
through an ``fm-*`` entrypoint, with no legacy read path. Driven here against
a real SQLite case repository, so a write is a real save with its version
bump. The console-entrypoint and database-gate pins cover the command with
every other ``fm-*`` command (``test_console_entrypoints.py``,
``test_database_gate.py``, ``test_commands_shut_the_container_down.py``).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.cli.backfill_case_outcome import backfill_case_outcome
from faultmaven.core.investigation import outcome as outcome_module
from faultmaven.core.investigation.outcome import compute_case_outcome
from faultmaven.infrastructure.persistence.models import Base
from tests.case_builders import inquiry_case
from tests.unit.core.investigation.test_m6_cause_gone_problem_persists_1927 import (
    _identified_case,
)

pytestmark = pytest.mark.unit

_PROVIDER = "faultmaven.providers.tenancy.factory.requested_tenant_provider"


@pytest.fixture
async def repository():
    from faultmaven.modules.case.infrastructure.sqlite_case_repository.repository import (
        SQLiteCaseRepository,
    )

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield SQLiteCaseRepository(session)
    await engine.dispose()


@pytest.fixture
async def stored(repository):
    """Two stored cases without a record: an identified cause, and an inquiry."""
    identified, _root, _hyp = _identified_case()
    inquiry = inquiry_case(user_id="u", enterprise_id="o", statement="it is slow")
    for case in (identified, inquiry):
        await repository.save(case)
    return {c.case_id: (await repository.get(c.case_id)) for c in (identified, inquiry)}


async def _run(repository, *, dry_run=False, enterprise_id=None, provider="single"):
    with patch(_PROVIDER, return_value=provider):
        return await backfill_case_outcome(
            enterprise_id=enterprise_id, dry_run=dry_run, repository=repository
        )


async def test_a_dry_run_writes_nothing(repository, stored, capsys):
    assert await _run(repository, dry_run=True) == 0
    for case_id, before in stored.items():
        after = await repository.get(case_id)
        assert after.progress.outcome is None
        assert after.version == before.version
    out = capsys.readouterr().out
    assert "Records would be written: 2" in out
    assert "Dry run — nothing was written" in out


async def test_yes_writes_each_record_from_the_case_as_stored(repository, stored):
    assert await _run(repository) == 0
    for case_id, before in stored.items():
        after = await repository.get(case_id)
        assert after.progress.outcome == compute_case_outcome(None, before)
        assert after.version == before.version + 1


async def test_a_second_run_writes_none(repository, stored, capsys):
    await _run(repository)
    versions = {i: (await repository.get(i)).version for i in stored}
    capsys.readouterr()
    assert await _run(repository) == 0
    assert {i: (await repository.get(i)).version for i in stored} == versions
    out = capsys.readouterr().out
    assert "Have one:     2 (left alone)" in out
    assert "Records written: 0" in out


async def test_multi_tenant_requires_an_enterprise(repository, stored, capsys):
    assert await _run(repository, provider="multi") == 1
    assert "--enterprise-id is required" in capsys.readouterr().out
    for case_id in stored:
        assert (await repository.get(case_id)).progress.outcome is None


async def test_multi_tenant_binds_the_enterprise_it_names(repository, stored):
    bind = AsyncMock(return_value=SimpleNamespace(name="Acme"))
    with patch("faultmaven.cli._tenant.bind_and_load_enterprise", bind):
        assert await _run(repository, provider="multi", enterprise_id="ent_1") == 0
    bind.assert_awaited_once_with("ent_1")
    for case_id in stored:
        assert (await repository.get(case_id)).progress.outcome is not None


async def test_a_case_that_raises_is_listed_and_the_run_goes_on(
    repository, stored, monkeypatch, capsys
):
    broken, healthy = list(stored)
    real = outcome_module.compute_case_outcome

    def _compute(prior, case, **kwargs):
        if case.case_id == broken:
            raise KeyError("a derivation that cannot read this case")
        return real(prior, case, **kwargs)

    monkeypatch.setattr(outcome_module, "compute_case_outcome", _compute)
    assert await _run(repository) == 1
    assert (await repository.get(broken)).progress.outcome is None
    assert (await repository.get(healthy)).progress.outcome is not None
    out = capsys.readouterr().out
    assert "Failed:       1" in out
    assert f"  - {broken}: KeyError" in out
