"""Phase 4c — ``FindEntityTool``.

Covers input validation (entity_value required, entity_type optional
and constrained to the controlled vocabulary), repo delegation, and
the shape of the returned ``ToolResult``. No real repository involved
— the tool is a thin async wrapper over
``case_repository.find_entity``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.modules.agent.tools.base import ToolContext
from faultmaven.modules.agent.tools.find_entity_tool import FindEntityTool
from faultmaven.modules.case.domain.models import CaseEntity, EntityType


def _ctx() -> ToolContext:
    return ToolContext(
        session_id="sess_1",
        case_id="case_abc",
        enterprise_id="org_1",
        user_id="user_1",
    )


def _entity(
    *,
    entity_type: EntityType = EntityType.IP,
    entity_value: str = "10.0.0.5",
    evidence_id: str = "ev_aaaaaaaaaaaa",
    mention_count: int = 1,
    in_error_context: bool = False,
    first_seen_ts: datetime | None = None,
) -> CaseEntity:
    return CaseEntity(
        case_id="case_abc",
        entity_type=entity_type,
        entity_value=entity_value,
        evidence_id=evidence_id,
        mention_count=mention_count,
        in_error_context=in_error_context,
        first_seen_ts=first_seen_ts,
    )


@pytest.fixture
def repo():
    r = MagicMock()
    r.find_entity = AsyncMock(return_value=[])
    return r


@pytest.fixture
def tool(repo):
    return FindEntityTool(case_repository=repo)


class TestSchema:
    def test_name(self, tool):
        assert tool.name == "find_entity"

    def test_requires_entity_value(self, tool):
        schema = tool.parameters_schema
        assert schema["required"] == ["entity_value"]

    def test_entity_type_is_documented(self, tool):
        schema = tool.parameters_schema
        desc = schema["properties"]["entity_type"]["description"]
        # Every controlled-vocab value is named in the description.
        for t in EntityType:
            assert t.value in desc


class TestExecute:
    @pytest.mark.asyncio
    async def test_returns_error_when_repo_unavailable(self):
        tool = FindEntityTool(case_repository=None)
        result = await tool.execute_with_context({"entity_value": "10.0.0.5"}, _ctx())
        assert result.success is False
        assert "Case repository" in result.error

    @pytest.mark.asyncio
    async def test_missing_value_rejected(self, tool):
        result = await tool.execute_with_context({}, _ctx())
        assert result.success is False
        assert "entity_value" in result.error

    @pytest.mark.asyncio
    async def test_blank_value_rejected(self, tool):
        result = await tool.execute_with_context({"entity_value": "   "}, _ctx())
        assert result.success is False

    @pytest.mark.asyncio
    async def test_invalid_entity_type_rejected(self, tool):
        result = await tool.execute_with_context(
            {"entity_value": "10.0.0.5", "entity_type": "not_a_type"},
            _ctx(),
        )
        assert result.success is False
        assert "Unknown entity_type" in result.error

    @pytest.mark.asyncio
    async def test_delegates_to_repo_with_parsed_type(self, tool, repo):
        repo.find_entity.return_value = [
            _entity(
                entity_type=EntityType.IP,
                entity_value="10.0.0.5",
                mention_count=4,
                in_error_context=True,
                first_seen_ts=datetime(2026, 4, 23, tzinfo=timezone.utc),
            )
        ]
        result = await tool.execute_with_context(
            {"entity_value": "10.0.0.5", "entity_type": "ip"},
            _ctx(),
        )
        assert result.success is True
        repo.find_entity.assert_awaited_once()
        kwargs = repo.find_entity.await_args.kwargs
        assert kwargs["case_id"] == "case_abc"
        assert kwargs["entity_value"] == "10.0.0.5"
        assert kwargs["entity_type"] == EntityType.IP
        assert result.data["count"] == 1
        match = result.data["matches"][0]
        assert match["entity_value"] == "10.0.0.5"
        assert match["entity_type"] == "ip"
        assert match["mention_count"] == 4
        assert match["in_error_context"] is True
        assert match["first_seen_ts"].startswith("2026-04-23")

    @pytest.mark.asyncio
    async def test_no_type_passes_none_to_repo(self, tool, repo):
        await tool.execute_with_context({"entity_value": "alpha"}, _ctx())
        kwargs = repo.find_entity.await_args.kwargs
        assert kwargs["entity_type"] is None

    @pytest.mark.asyncio
    async def test_value_is_trimmed(self, tool, repo):
        await tool.execute_with_context({"entity_value": "  10.0.0.5  "}, _ctx())
        assert repo.find_entity.await_args.kwargs["entity_value"] == "10.0.0.5"

    @pytest.mark.asyncio
    async def test_repo_exception_degrades_to_error_result(self, tool, repo):
        repo.find_entity.side_effect = RuntimeError("boom")
        result = await tool.execute_with_context({"entity_value": "10.0.0.5"}, _ctx())
        assert result.success is False
        assert "boom" in result.error
