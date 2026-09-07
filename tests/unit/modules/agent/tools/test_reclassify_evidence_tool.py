"""Phase 1.5 — ReclassifyEvidenceTool

Covers the agent-tool wrapper around
``InvestigationService.reclassify_evidence``. The tool is lean by
design — validation + delegation — so tests focus on the validation
surface and the exception-to-ToolResult mapping.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.exceptions import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
)
from faultmaven.models.api import DataType
from faultmaven.modules.agent.tools.base import ToolContext
from faultmaven.modules.agent.tools.reclassify_evidence_tool import (
    ReclassifyEvidenceTool,
)


def _enable_flag(value: bool):
    class _Prep:
        reclassify_enabled = value
        confidence_marker_enabled = False

    class _Settings:
        preprocessing = _Prep()

    return patch(
        "faultmaven.modules.agent.tools.reclassify_evidence_tool.get_settings",
        return_value=_Settings(),
    )


def _ctx():
    return ToolContext(
        session_id="sess_1",
        case_id="case_xyz",
        enterprise_id="org_1",
        user_id="user_abc",
    )


@pytest.fixture
def service():
    svc = MagicMock()
    svc.reclassify_evidence = AsyncMock()
    return svc


@pytest.fixture
def tool(service):
    return ReclassifyEvidenceTool(investigation_service=service)


class TestSchemaAndDescription:
    def test_name_is_reclassify_evidence(self, tool):
        assert tool.name == "reclassify_evidence"

    def test_schema_enum_matches_data_type_enum(self, tool):
        schema_enum = set(tool.parameters_schema["properties"]["data_type"]["enum"])
        expected = {t.value for t in DataType}
        assert schema_enum == expected

    def test_required_params(self, tool):
        assert set(tool.parameters_schema["required"]) == {"evidence_id", "data_type"}


class TestFeatureFlagGating:
    @pytest.mark.asyncio
    async def test_flag_off_returns_error(self, tool):
        with _enable_flag(False):
            result = await tool.execute_with_context(
                params={"evidence_id": "ev_abc", "data_type": "logs_and_errors"},
                context=_ctx(),
            )
        assert result.success is False
        assert "not enabled" in result.error.lower()


class TestInputValidation:
    @pytest.mark.asyncio
    async def test_missing_evidence_id(self, tool):
        with _enable_flag(True):
            result = await tool.execute_with_context(
                params={"data_type": "logs_and_errors"},
                context=_ctx(),
            )
        assert result.success is False
        assert "evidence_id" in result.error

    @pytest.mark.asyncio
    async def test_missing_data_type(self, tool):
        with _enable_flag(True):
            result = await tool.execute_with_context(
                params={"evidence_id": "ev_abc"},
                context=_ctx(),
            )
        assert result.success is False
        assert "data_type" in result.error

    @pytest.mark.asyncio
    async def test_invalid_data_type(self, tool):
        with _enable_flag(True):
            result = await tool.execute_with_context(
                params={"evidence_id": "ev_abc", "data_type": "totally_fake"},
                context=_ctx(),
            )
        assert result.success is False
        assert "totally_fake" in result.error


class TestServiceDelegation:
    @pytest.mark.asyncio
    async def test_success_path_returns_updated_data(self, tool, service):
        updated = MagicMock()
        updated.evidence_id = "ev_abc"
        updated.source_type.value = "logs_and_errors"
        updated.summary = "Re-extracted"
        service.reclassify_evidence.return_value = updated

        with _enable_flag(True):
            result = await tool.execute_with_context(
                params={"evidence_id": "ev_abc", "data_type": "logs_and_errors"},
                context=_ctx(),
            )

        assert result.success is True
        assert result.data["evidence_id"] == "ev_abc"
        assert result.data["data_type"] == "logs_and_errors"
        # Tool must pass trigger="agent_tool" for observability labelling.
        kwargs = service.reclassify_evidence.call_args.kwargs
        assert kwargs["trigger"] == "agent_tool"
        assert kwargs["case_id"] == "case_xyz"
        assert kwargs["user_id"] == "user_abc"
        assert kwargs["data_type"] == DataType.LOGS_AND_ERRORS

    @pytest.mark.asyncio
    async def test_not_found_maps_to_error(self, tool, service):
        service.reclassify_evidence.side_effect = NotFoundError("Evidence", "ev_abc")
        with _enable_flag(True):
            result = await tool.execute_with_context(
                params={"evidence_id": "ev_abc", "data_type": "logs_and_errors"},
                context=_ctx(),
            )
        assert result.success is False
        assert "ev_abc" in result.error

    @pytest.mark.asyncio
    async def test_authorization_error_maps_to_error(self, tool, service):
        """AuthorizationError surfaces as a failed ToolResult."""
        service.reclassify_evidence.side_effect = AuthorizationError("nope")
        with _enable_flag(True):
            result = await tool.execute_with_context(
                params={"evidence_id": "ev_abc", "data_type": "logs_and_errors"},
                context=_ctx(),
            )
        assert result.success is False

    @pytest.mark.asyncio
    async def test_no_backing_file_maps_to_error(self, tool, service):
        """Evidence with no stored raw file → ConflictError → failed
        ToolResult.
        """
        service.reclassify_evidence.side_effect = ConflictError(
            "Evidence ev_abc has no stored raw file",
            resource_type="evidence",
            resource_id="ev_abc",
            conflict_reason="no_backing_file",
        )
        with _enable_flag(True):
            result = await tool.execute_with_context(
                params={"evidence_id": "ev_abc", "data_type": "logs_and_errors"},
                context=_ctx(),
            )
        assert result.success is False
        assert "no stored raw file" in result.error
