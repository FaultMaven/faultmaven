"""Settings describe ONE database, configured by ``DATABASE_URL``.

The retired two-database layout (a separate auth database and cases database,
configured by ``AUTH_DB_*`` / ``CASES_DB_*``) is gone: nothing opens either, so
settings neither carry those fields nor react to those variables.
"""

import pytest

from faultmaven.config.settings import DatabaseSettings

RETIRED_ENV = {
    "AUTH_DB_HOST": "auth-host",
    "AUTH_DB_PORT": "5433",
    "AUTH_DB_NAME": "auth_db",
    "AUTH_DB_USER": "auth_service",
    "AUTH_DB_PASSWORD": "auth-secret",
    "CASES_DB_HOST": "cases-host",
    "CASES_DB_PORT": "5434",
    "CASES_DB_NAME": "cases_db",
    "CASES_DB_USER": "case_service",
    "CASES_DB_PASSWORD": "cases-secret",
}


@pytest.mark.unit
def test_database_settings_declare_no_per_database_fields():
    retired = [
        name
        for name in DatabaseSettings.model_fields
        if name.startswith(("auth_db_", "cases_db_"))
    ]

    assert retired == []


@pytest.mark.unit
@pytest.mark.parametrize("attribute", ["auth_db_url", "cases_db_url"])
def test_database_settings_build_no_per_database_url(attribute):
    assert not hasattr(DatabaseSettings(), attribute)


@pytest.mark.unit
def test_retired_variables_do_not_change_the_database(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://app@db:5432/faultmaven")
    baseline = DatabaseSettings().model_dump()

    for key, value in RETIRED_ENV.items():
        monkeypatch.setenv(key, value)
    configured = DatabaseSettings()

    assert configured.database_url == "postgresql+asyncpg://app@db:5432/faultmaven"
    assert configured.model_dump() == baseline
