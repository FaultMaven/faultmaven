"""A configured service that cannot be built refuses the start where the
deployment must not degrade, and is never stood in for (#3003).

Drives the real composition. The milestone engine, the investigation service
(its turn cap included), the report-generation service and the user service
each caught their own construction failure and returned ``None``, and
``agent_service`` became ``object()``: a production pod composed, reported
healthy and failed every turn. Now each factory's arm calls
``unbuilt_where_must_not_degrade``; elsewhere it serves without the service,
as before.
"""

import pytest

from faultmaven.container.errors import RequiredServiceConstructionError

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

CONSTRUCTORS = pytest.mark.parametrize(
    "target,name",
    [
        (
            "faultmaven.core.investigation.milestone_engine.engine.MilestoneEngine",
            "MilestoneEngine",
        ),
        (
            "faultmaven.modules.agent.domain.services.investigation_service."
            "service.InvestigationService",
            "InvestigationService",
        ),
        (
            "faultmaven.infrastructure.protection.tenant_turn_cap.TurnCapService",
            "InvestigationService",
        ),
        (
            "faultmaven.modules.report.domain.services.report_generation_service."
            "ReportGenerationService",
            "ReportGenerationService",
        ),
        (
            "faultmaven.modules.auth.domain.services.user_service.UserService",
            "UserService",
        ),
    ],
    ids=["engine", "investigation", "turn_cap", "report", "user"],
)


@CONSTRUCTORS
async def test_a_production_boot_refuses_and_names_the_class(
    target, name, boom, environment, fresh_container
):
    assert environment("production").must_not_degrade
    error = boom(target)

    with pytest.raises(RequiredServiceConstructionError) as refused:
        await fresh_container.initialize()

    assert refused.value.__cause__ is error
    assert str(refused.value) == (
        f"{name} could not be constructed where the deployment must not "
        "degrade: RuntimeError"
    )
    assert not fresh_container.is_initialized


async def test_a_development_boot_serves_without_the_report_service(
    boom, environment, fresh_container
):
    """Standalone is unchanged: one failed service costs that service, not the
    ones wired after it."""
    assert not environment("development").must_not_degrade
    boom(
        "faultmaven.modules.report.domain.services.report_generation_service."
        "ReportGenerationService"
    )

    await fresh_container.initialize()

    assert fresh_container.is_initialized
    assert fresh_container.report_generation_service is None
    assert fresh_container.milestone_engine is not None
    assert fresh_container.investigation_service is not None


async def test_no_stand_in_for_an_investigation_service_it_could_not_build(
    boom, environment, fresh_container
):
    assert not environment("development").must_not_degrade
    boom(
        "faultmaven.modules.agent.domain.services.investigation_service."
        "service.InvestigationService"
    )

    await fresh_container.initialize()

    assert fresh_container.is_initialized
    assert fresh_container.investigation_service is None
    assert not hasattr(fresh_container, "agent_service")
