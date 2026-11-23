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
from faultmaven.container.providers.tools import create_document_qa_tools

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
            lambda s: create_milestone_engine(
                MagicMock(), MagicMock(), None, settings=s
            ),
        ),
        (
            "faultmaven.modules.agent.domain.services.investigation_service."
            "service.InvestigationService",
            "InvestigationService",
            lambda s: create_investigation_service(
                MagicMock(), MagicMock(), settings=s
            ),
        ),
        (
            "faultmaven.infrastructure.protection.tenant_turn_cap.TurnCapService",
            "InvestigationService",
            lambda s: create_investigation_service(
                MagicMock(), MagicMock(), settings=s
            ),
        ),
        (
            "faultmaven.modules.report.domain.services.report_generation_service."
            "ReportGenerationService",
            "ReportGenerationService",
            lambda s: create_report_generation_service(None, None, None, settings=s),
        ),
        (
            "faultmaven.modules.auth.domain.services.user_service.UserService",
            "UserService",
            lambda s: create_user_service(MagicMock(), MagicMock(), None, s),
        ),
    ],
    ids=["engine", "investigation", "turn_cap", "report", "user"],
)


@FACTORIES
def test_refused_where_the_deployment_must_not_degrade(
    target, name, build, environment
):
    settings = environment("production")
    assert settings.must_not_degrade
    error = ImportError("marker")

    with patch(f"{target}.__init__", side_effect=error):
        with pytest.raises(RequiredServiceConstructionError) as refused:
            build(settings)

    assert refused.value.__cause__ is error
    assert str(refused.value) == (
        f"{name} could not be constructed where the deployment must not "
        "degrade: ImportError"
    )


@FACTORIES
def test_served_without_it_elsewhere(target, name, build, environment):
    settings = environment("development")
    assert not settings.must_not_degrade

    with patch(f"{target}.__init__", side_effect=ImportError("marker")):
        assert build(settings) is None


def _kb_qa_failing(environment, env):
    """``create_document_qa_tools``' arm, executed: ``kb_qa`` failing to build."""
    settings = environment(env)
    settings.server.skip_service_checks = False
    return settings, patch(
        "faultmaven.modules.agent.tools.kb_qa.AnswerFromKB.__init__",
        side_effect=ImportError("marker"),
    )


def test_a_tool_arm_serves_without_the_tool_elsewhere(environment):
    settings, failing = _kb_qa_failing(environment, "development")
    with failing:
        tools = create_document_qa_tools(None, MagicMock(), MagicMock(), settings)
    assert tools["kb_qa_tool"] is None


def test_a_tool_arm_refuses_where_it_must_not_degrade(environment):
    settings, failing = _kb_qa_failing(environment, "production")
    with failing, pytest.raises(RequiredServiceConstructionError) as refused:
        create_document_qa_tools(None, MagicMock(), MagicMock(), settings)
    assert isinstance(refused.value.__cause__, ImportError)
    assert str(refused.value) == (
        "The document Q&A tools could not be constructed where the deployment "
        "must not degrade: ImportError"
    )


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
