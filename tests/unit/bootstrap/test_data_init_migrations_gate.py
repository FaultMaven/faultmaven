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
_KNOWN = frozenset({"base_rev", "older_rev", "head_rev"})


def _scripts(heads=_HEAD, known=_KNOWN, override=""):
    """Patch what ``_stamped_remedy`` reads: ALEMBIC_CONFIG and the scripts."""

    def reader(value):
        if isinstance(value, Exception):
            return MagicMock(side_effect=value)
        return MagicMock(return_value=value)

    return (
        patch.object(data_init, "_alembic_config_override", return_value=override),
        patch.object(data_init, "alembic_script_heads", reader(heads)),
        patch.object(data_init, "alembic_script_revisions", reader(known)),
    )


@pytest.mark.unit
def test_a_stamp_at_head_is_not_told_to_rerun_the_migration():
    """At head the Job applies nothing and reports success: the advice names the
    repair and the re-provision instead."""
    override, heads, known = _scripts()
    with override, heads, known:
        remedy = data_init._stamped_remedy(_HEAD)
    assert "changes nothing" in remedy
    assert "database-migrations.md" in remedy and "deployment-wipe.md" in remedy
    assert "run the migration Job" not in remedy


@pytest.mark.unit
def test_a_stamp_behind_head_is_told_to_run_the_migration_and_what_if_that_fails():
    """Behind head the pending run is the remedy; the at-head remedy is named for
    a name no pending revision creates (an amended or hand-dropped one)."""
    override, heads, known = _scripts()
    with override, heads, known:
        remedy = data_init._stamped_remedy(frozenset({"older_rev"}))
    assert remedy.startswith(
        "The migration scripts' head is head_rev: run the migration Job "
        "(`alembic upgrade head`). If the database still lacks them once at head, "
        "no migration run will create them."
    )
    assert "database-migrations.md" in remedy and "deployment-wipe.md" in remedy


@pytest.mark.unit
@pytest.mark.parametrize(
    "stamped",
    [frozenset({"newer_rev"}), frozenset({"head_rev", "newer_rev"})],
    ids=["newer stamp", "one of two stamps unknown"],
)
def test_a_stamp_the_scripts_do_not_contain_is_not_told_to_migrate(stamped):
    """An image rolled back across a migration: alembic cannot locate the stamp,
    so a migration run from this image fails and is not the advice."""
    override, heads, known = _scripts()
    with override, heads, known:
        remedy = data_init._stamped_remedy(stamped)
    assert remedy == (
        "This image's migration scripts do not contain newer_rev: the database "
        "was migrated by a newer image, or by other scripts, so no migration run "
        "from this image can proceed. Run an image whose migrations include it."
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "override, heads, known",
    [
        ("", OSError("alembic.ini not found"), _KNOWN),
        ("", _HEAD, OSError("revision file unreadable")),
        ("/elsewhere/alembic.ini", _HEAD, _KNOWN),
    ],
    ids=["heads unreadable", "revisions unreadable", "ALEMBIC_CONFIG set"],
)
def test_unknown_scripts_claim_neither_remedy(override, heads, known):
    """When the scripts the migration run reads cannot be known, the run and the
    at-head remedy are both named and neither is asserted."""
    o, h, k = _scripts(heads=heads, known=known, override=override)
    with o, h as heads_reader, k as revisions_reader:
        remedy = data_init._stamped_remedy(frozenset({"newer_rev"}))
    assert remedy.startswith(
        "If that is not the migration scripts' head, run the migration Job"
    )
    assert "if it is, no migration run creates them" in remedy
    assert "changes nothing" not in remedy and "do not contain" not in remedy
    if override:
        heads_reader.assert_not_called()
        revisions_reader.assert_not_called()
