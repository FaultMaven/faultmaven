"""Unit tests for the RUN_STARTUP_MIGRATIONS gate in data_init.

The app self-runs `alembic upgrade head` at startup only when it owns its
schema. When an external migration Job owns schema (the K8s deployment), the
app connects as a non-owner, no-DDL role, so a startup migration that needs
DDL would be permission-denied and crash-loop the pod. These tests pin that
`initialize_data_layer` runs migrations iff `RUN_STARTUP_MIGRATIONS` is true,
and that the schema check (#1514) runs on both branches: the Job-owned
deployment is the one that cannot migrate itself.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.bootstrap import data_init


@pytest.mark.unit
@pytest.mark.asyncio
async def test_startup_migrations_run_when_flag_true():
    settings = SimpleNamespace(run_startup_migrations=True)
    with (
        patch.object(data_init, "ensure_data_directories"),
        patch.object(data_init, "_schema_already_at_head", return_value=False),
        patch.object(data_init, "run_alembic_migrations") as run_migrations,
        patch.object(data_init, "verify_schema_tables", AsyncMock()) as schema_check,
        patch("faultmaven.config.settings.get_settings", return_value=settings),
    ):
        await data_init.initialize_data_layer(container=MagicMock())

    run_migrations.assert_called_once()
    schema_check.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_startup_migrations_skipped_when_flag_false():
    """Job-owned schema + non-owner role: the app must NOT self-migrate."""
    settings = SimpleNamespace(run_startup_migrations=False)
    with (
        patch.object(data_init, "ensure_data_directories"),
        patch.object(data_init, "_schema_already_at_head") as at_head_check,
        patch.object(data_init, "run_alembic_migrations") as run_migrations,
        patch.object(data_init, "verify_schema_tables", AsyncMock()) as schema_check,
        patch("faultmaven.config.settings.get_settings", return_value=settings),
    ):
        await data_init.initialize_data_layer(container=MagicMock())

    # Neither the at-head check (#2212) nor the migration runs; the schema
    # check does, since the Job may not have created every table (#1514).
    at_head_check.assert_not_called()
    run_migrations.assert_not_called()
    schema_check.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_schema_read_past_the_bound_refuses_the_boot(monkeypatch):
    """Unlike the at-head check, a timeout here is a refusal, not a skip (#1514)."""
    import asyncio

    async def slow_read(url, declared):
        await asyncio.sleep(2)  # finite, so a missing bound fails rather than hangs

    settings = SimpleNamespace(
        database=SimpleNamespace(database_url="sqlite+aiosqlite:///./schema-check.db")
    )
    monkeypatch.setattr(data_init, "AT_HEAD_CHECK_TIMEOUT_SECONDS", 0.05)
    with (
        patch.object(data_init, "_live_schema", slow_read),
        patch("faultmaven.config.settings.get_settings", return_value=settings),
        pytest.raises(TimeoutError, match="schema check's database read"),
    ):
        await data_init.verify_schema_tables()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_schema_behind_the_models_refuses_the_boot():
    """The check's refusal is the boot's: nothing downstream swallows it (#1514)."""
    settings = SimpleNamespace(run_startup_migrations=False)
    refusal = RuntimeError("Database schema is behind the models: missing tables x")
    with (
        patch.object(data_init, "ensure_data_directories"),
        patch.object(data_init, "verify_schema_tables", AsyncMock(side_effect=refusal)),
        patch("faultmaven.config.settings.get_settings", return_value=settings),
        pytest.raises(RuntimeError, match="behind the models"),
    ):
        await data_init.initialize_data_layer(container=MagicMock())


@pytest.mark.unit
def test_run_startup_migrations_defaults_true():
    """Default preserves self-contained docker/local behaviour."""
    from faultmaven.config.settings import FaultMavenSettings

    assert FaultMavenSettings().run_startup_migrations is True


@pytest.mark.unit
def test_run_startup_migrations_reads_env_alias(monkeypatch):
    """Infra sets RUN_STARTUP_MIGRATIONS=false in the K8s ConfigMap."""
    from faultmaven.config.settings import FaultMavenSettings

    monkeypatch.setenv("RUN_STARTUP_MIGRATIONS", "false")
    assert FaultMavenSettings().run_startup_migrations is False


_HEAD = frozenset({"head_rev"})


@pytest.mark.unit
def test_a_stamp_at_head_is_not_told_to_rerun_the_migration():
    """At head the Job applies nothing and reports success: the advice names the
    repair and the re-provision instead."""
    with (
        patch.object(data_init, "_alembic_config_override", return_value=""),
        patch.object(data_init, "alembic_script_heads", return_value=_HEAD),
    ):
        remedy = data_init._stamped_remedy(_HEAD)
    assert "changes nothing" in remedy
    assert "database-migrations.md" in remedy and "deployment-wipe.md" in remedy
    assert "run the migration Job" not in remedy


@pytest.mark.unit
def test_a_stamp_behind_head_is_told_to_run_the_migration():
    with (
        patch.object(data_init, "_alembic_config_override", return_value=""),
        patch.object(data_init, "alembic_script_heads", return_value=_HEAD),
    ):
        remedy = data_init._stamped_remedy(frozenset({"older_rev"}))
    assert remedy == (
        "The migration scripts' head is head_rev: run the migration Job "
        "(`alembic upgrade head`)."
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "override, heads",
    [
        ("", OSError("alembic.ini not found")),
        ("/elsewhere/alembic.ini", _HEAD),
    ],
    ids=["heads unreadable", "ALEMBIC_CONFIG set"],
)
def test_an_unknown_head_claims_neither_remedy(override, heads):
    """When the scripts' head cannot be known for the run that migrates this
    database, both cases are named and neither is asserted."""
    reader = (
        MagicMock(side_effect=heads)
        if isinstance(heads, Exception)
        else MagicMock(return_value=heads)
    )
    with (
        patch.object(data_init, "_alembic_config_override", return_value=override),
        patch.object(data_init, "alembic_script_heads", reader),
    ):
        remedy = data_init._stamped_remedy(_HEAD)
    assert remedy.startswith(
        "If that is not the migration scripts' head, run the migration Job"
    )
    assert "if it is, no migration run creates them" in remedy
    assert "changes nothing" not in remedy
    if override:
        reader.assert_not_called()
