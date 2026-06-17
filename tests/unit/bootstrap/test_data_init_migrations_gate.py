"""Unit tests for the RUN_STARTUP_MIGRATIONS gate in data_init.

The app self-runs `alembic upgrade head` at startup only when it owns its
schema. When an external migration Job owns schema (the K8s deployment), the
app connects as a non-owner, no-DDL role, so a startup migration that needs
DDL would be permission-denied and crash-loop the pod. These tests pin that
`initialize_data_layer` runs migrations iff `RUN_STARTUP_MIGRATIONS` is true.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from faultmaven.bootstrap import data_init


@pytest.mark.unit
@pytest.mark.asyncio
async def test_startup_migrations_run_when_flag_true():
    settings = SimpleNamespace(run_startup_migrations=True)
    with (
        patch.object(data_init, "ensure_data_directories"),
        patch.object(data_init, "run_alembic_migrations") as run_migrations,
        patch(
            "faultmaven.config.settings.get_settings", return_value=settings
        ),
    ):
        await data_init.initialize_data_layer(container=MagicMock())

    run_migrations.assert_called_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_startup_migrations_skipped_when_flag_false():
    """Job-owned schema + non-owner role: the app must NOT self-migrate."""
    settings = SimpleNamespace(run_startup_migrations=False)
    with (
        patch.object(data_init, "ensure_data_directories"),
        patch.object(data_init, "run_alembic_migrations") as run_migrations,
        patch(
            "faultmaven.config.settings.get_settings", return_value=settings
        ),
    ):
        await data_init.initialize_data_layer(container=MagicMock())

    run_migrations.assert_not_called()


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
