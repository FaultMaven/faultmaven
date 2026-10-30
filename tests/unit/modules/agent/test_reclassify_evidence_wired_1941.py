"""``reclassify_evidence`` works when its flag is on (#1941).

The tool used to hold the investigation service, which is built after the
milestone engine, which is built from the tool registry: so the tool was
constructed with ``None`` and nothing put it in the DA registry. With
FAULTMAVEN_RECLASSIFY_ENABLED=true the model was not given the tool, and the
evidence-grounding hedge had it tell the user the feature was off.

The container now builds ONE ``EvidenceReclassifier`` from the deps the
infrastructure layer registers before any tool is built, whatever the flag
says (the PATCH route reads it through ``app.state``). The tool holds that same
instance, and ``_create_investigation_tools`` gives the tool to the model when
the flag built it. These tests drive the real builders and the real registry,
then execute the tool the registry hands back against an in-flight case.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.container.providers.services import _create_investigation_tools
from faultmaven.container.providers.tools import (
    create_evidence_reclassifier,
    create_reclassify_evidence_tool,
)
from faultmaven.models.api import DataType
from faultmaven.modules.agent.domain.services.investigation_service.reclassification import (  # noqa: E501
    EvidenceReclassifier,
)
from faultmaven.modules.agent.tools.base import ToolContext
from faultmaven.modules.case.domain.models.evidence import EvidenceSourceType
from tests.model_content import disabled_redaction

from .conftest import (
    MockCaseRepository,
    create_sample_case,
    make_evidence,
    make_preprocessing_result,
    make_uploaded_file,
)

pytestmark = pytest.mark.unit

OWNER = "user_owner"
EVIDENCE_ID = "ev_aaaaaaaaaaaa"


@pytest.fixture
def reclassify_flag(monkeypatch):
    """Set FAULTMAVEN_RECLASSIFY_ENABLED for the builder and the tool alike."""

    def _set(enabled: bool) -> None:
        settings = SimpleNamespace(
            preprocessing=SimpleNamespace(
                reclassify_enabled=enabled, confidence_marker_enabled=False
            )
        )
        for target in (
            "faultmaven.config.settings.get_settings",
            "faultmaven.modules.agent.tools.reclassify_evidence_tool.get_settings",
        ):
            monkeypatch.setattr(target, lambda: settings)

    return _set


class _Container(SimpleNamespace):
    """The two faces of the container the composition root reads: registered
    services by name, and tool attributes."""

    def get_service(self, name):
        return self.services.get(name)


def _deps():
    preprocessing = MagicMock()
    preprocessing.reclassify_evidence = AsyncMock(
        return_value=make_preprocessing_result(new_data_type=DataType.LOGS_AND_ERRORS)
    )
    storage = MagicMock()
    storage.retrieve_file = AsyncMock(return_value=b"line1\nline2 ERROR\n")
    return {
        "case_repository": MockCaseRepository(),
        "file_storage_service": storage,
        "preprocessing_service": preprocessing,
    }


def _composed(deps=None) -> _Container:
    """A container after ``register_tools``: the reclassifier registered (when
    built), and the flag-gated tool attribute whatever its builder returned."""
    container = _Container(services=_deps() if deps is None else deps)
    container.services["evidence_reclassifier"] = create_evidence_reclassifier(
        container
    )
    container.reclassify_evidence_tool = create_reclassify_evidence_tool(container)
    return container


def _in_flight_case():
    case = create_sample_case(user_id=OWNER)
    case.evidence = [make_evidence(evidence_id=EVIDENCE_ID)]
    case.uploaded_files = [make_uploaded_file()]
    return case


def _context(case) -> ToolContext:
    return ToolContext(
        session_id="sess_1",
        case_id=case.case_id,
        enterprise_id=case.enterprise_id,
        user_id=OWNER,
        in_memory_case=case,
        redaction_ctx=disabled_redaction(),
    )


def test_flag_on_builds_the_tool_on_the_registered_deps(reclassify_flag) -> None:
    reclassify_flag(True)
    container = _composed()
    reclassifier = container.reclassify_evidence_tool.reclassifier
    assert isinstance(reclassifier, EvidenceReclassifier)
    # The one instance the PATCH route is published, not a second one.
    assert reclassifier is container.services["evidence_reclassifier"]
    services = container.services
    assert reclassifier.repository is services["case_repository"]
    assert reclassifier.file_storage_service is services["file_storage_service"]
    assert reclassifier.preprocessing_service is services["preprocessing_service"]


@pytest.mark.asyncio
async def test_the_given_tool_reclassifies_the_in_flight_case(reclassify_flag) -> None:
    reclassify_flag(True)
    container = _composed()
    registry = _create_investigation_tools(container, knowledge_service=MagicMock())
    tool = registry.get("reclassify_evidence")
    assert tool is not None, "flag on, yet the model is not given the tool"

    case = _in_flight_case()
    assert case.evidence[0].source_type is not EvidenceSourceType.LOGS
    result = await tool.execute_with_context(
        params={"evidence_id": EVIDENCE_ID, "data_type": "logs_and_errors"},
        context=_context(case),
    )

    assert result.success, result.error
    assert result.data["source_type"] == EvidenceSourceType.LOGS.value
    # Written onto the aggregate the turn holds; the turn's save persists it.
    assert case.evidence[0].source_type is EvidenceSourceType.LOGS
    container.services["case_repository"].save.assert_not_awaited()
    container.services[
        "preprocessing_service"
    ].reclassify_evidence.assert_awaited_once()
    assert (
        container.services[
            "preprocessing_service"
        ].reclassify_evidence.call_args.kwargs["user_override"]
        is DataType.LOGS_AND_ERRORS
    )


def test_flag_off_builds_nothing_and_gives_nothing(reclassify_flag) -> None:
    reclassify_flag(False)
    container = _composed()
    assert container.reclassify_evidence_tool is None
    # The reclassifier is still built: the route answers 404 off its own flag
    # check, never 503 for a missing reclassifier.
    assert isinstance(container.services["evidence_reclassifier"], EvidenceReclassifier)
    registry = _create_investigation_tools(container, knowledge_service=MagicMock())
    assert registry is None or registry.get("reclassify_evidence") is None


@pytest.mark.parametrize(
    "absent", ["case_repository", "file_storage_service", "preprocessing_service"]
)
def test_a_missing_dependency_builds_nothing(reclassify_flag, absent) -> None:
    # Never a tool that answers "unavailable" on every call: no tool at all.
    reclassify_flag(True)
    deps = _deps()
    deps[absent] = None
    container = _composed(deps)
    assert container.services["evidence_reclassifier"] is None
    assert container.reclassify_evidence_tool is None
