"""The factories that caught their own construction failure (#3003).

Each refuses where the deployment must not degrade, naming the error's class,
never its text, and serves without the service elsewhere. The database user
store never falls through to Redis while ``UserService`` reads the database:
it refuses in every mode.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from faultmaven.container.errors import RequiredServiceConstructionError
from faultmaven.container.providers.infrastructure import create_user_store
from faultmaven.container.providers.services import (
    create_investigation_service,
    create_milestone_engine,
    create_report_generation_service,
    create_user_service,
)

pytestmark = pytest.mark.unit

_DSN = SimpleNamespace(
    database=SimpleNamespace(database_url="sqlite+aiosqlite:///./data/x.db")
)

FACTORIES = pytest.mark.parametrize(
    "target,name,build",
    [
        (
            "faultmaven.core.investigation.milestone_engine.engine.MilestoneEngine",
            "MilestoneEngine",
            lambda: create_milestone_engine(MagicMock(), MagicMock(), None),
        ),
        (
            "faultmaven.modules.agent.domain.services.investigation_service."
            "service.InvestigationService",
            "InvestigationService",
            lambda: create_investigation_service(MagicMock(), MagicMock()),
        ),
        (
            "faultmaven.infrastructure.protection.tenant_turn_cap.TurnCapService",
            "InvestigationService",
            lambda: create_investigation_service(MagicMock(), MagicMock()),
        ),
        (
            "faultmaven.modules.report.domain.services.report_generation_service."
            "ReportGenerationService",
            "ReportGenerationService",
            lambda: create_report_generation_service(None, None, None),
        ),
        (
            "faultmaven.modules.auth.domain.services.user_service.UserService",
            "UserService",
            lambda: create_user_service(MagicMock(), MagicMock(), None, _DSN),
        ),
    ],
    ids=["engine", "investigation", "turn_cap", "report", "user"],
)


@FACTORIES
def test_refused_where_the_deployment_must_not_degrade(
    target, name, build, environment
):
    assert environment("production").must_not_degrade
    error = ImportError("marker")

    with patch(f"{target}.__init__", side_effect=error):
        with pytest.raises(RequiredServiceConstructionError) as refused:
            build()

    assert refused.value.__cause__ is error
    assert str(refused.value) == (
        f"{name} could not be constructed where the deployment must not "
        "degrade: ImportError"
    )


@FACTORIES
def test_served_without_it_elsewhere(target, name, build, environment):
    assert not environment("development").must_not_degrade

    with patch(f"{target}.__init__", side_effect=ImportError("marker")):
        assert build() is None


def test_the_database_user_store_never_falls_through_to_redis(environment):
    """Refused in every mode (#2103's invariant): ``UserService`` reads the
    database, so a Redis store would take logins it never sees."""
    assert not environment("development").must_not_degrade
    error = ImportError("marker")

    with patch(
        "faultmaven.infrastructure.auth.database_user_store.DatabaseUserStore"
        ".__init__",
        side_effect=error,
    ):
        with pytest.raises(RequiredServiceConstructionError) as refused:
            create_user_store(SimpleNamespace(), _DSN)

    assert refused.value.__cause__ is error
    assert str(refused.value) == (
        "Database user store could not be constructed: ImportError"
    )
