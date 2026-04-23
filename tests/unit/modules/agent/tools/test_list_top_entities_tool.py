"""Phase 4c — ``ListTopEntitiesTool``.

Covers entity_type required + constrained, limit bounds clamping,
repo delegation, and result shape.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.modules.agent.tools.base import ToolContext
from faultmaven.modules.agent.tools.list_top_entities_tool import (
    _MAX_LIMIT,
    ListTopEntitiesTool,
)
from faultmaven.modules.case.domain.models import CaseEntity, EntityType


def _ctx() -> ToolContext:
    return ToolContext(
        session_id="sess_1",
        case_id="case_abc",
        organization_id="org_1",
        user_id="user_1",
    )


def _entity(
    value: str, count: int, entity_type: EntityType = EntityType.IP
) -> CaseEntity:
    return CaseEntity(
        case_id="case_abc",
        entity_type=entity_type,
        entity_value=value,
        evidence_id="ev_aaaaaaaaaaaa",
        mention_count=count,
    )


@pytest.fixture
def repo():
    r = MagicMock()
    r.list_top_entities = AsyncMock(return_value=[])
    return r


@pytest.fixture
def tool(repo):
    return ListTopEntitiesTool(case_repository=repo)


class TestSchema:
    def test_name(self, tool):
        assert tool.name == "list_top_entities"

    def test_requires_entity_type(self, tool):
        assert tool.parameters_schema["required"] == ["entity_type"]


class TestExecute:
    @pytest.mark.asyncio
    async def test_returns_error_when_repo_unavailable(self):
        tool = ListTopEntitiesTool(case_repository=None)
        result = await tool.execute_with_context({"entity_type": "ip"}, _ctx())
        assert result.success is False
        assert "Case repository" in result.error

    @pytest.mark.asyncio
    async def test_missing_type_rejected(self, tool):
        result = await tool.execute_with_context({}, _ctx())
        assert result.success is False
        assert "entity_type" in result.error

    @pytest.mark.asyncio
    async def test_unknown_type_rejected(self, tool):
        result = await tool.execute_with_context({"entity_type": "not_a_type"}, _ctx())
        assert result.success is False

    @pytest.mark.asyncio
    async def test_delegates_to_repo_with_limit(self, tool, repo):
        repo.list_top_entities.return_value = [
            _entity("10.0.0.5", 5),
            _entity("10.0.0.6", 2),
        ]
        result = await tool.execute_with_context(
            {"entity_type": "ip", "limit": 2}, _ctx()
        )
        assert result.success is True
        repo.list_top_entities.assert_awaited_once()
        kwargs = repo.list_top_entities.await_args.kwargs
        assert kwargs["case_id"] == "case_abc"
        assert kwargs["entity_type"] == EntityType.IP
        assert kwargs["limit"] == 2
        assert result.data["count"] == 2
        assert result.data["entities"][0]["entity_value"] == "10.0.0.5"
        assert result.data["entities"][0]["mention_count"] == 5

    @pytest.mark.asyncio
    async def test_limit_clamped_to_max(self, tool, repo):
        await tool.execute_with_context({"entity_type": "ip", "limit": 10_000}, _ctx())
        assert repo.list_top_entities.await_args.kwargs["limit"] == _MAX_LIMIT

    @pytest.mark.asyncio
    async def test_limit_clamped_to_min(self, tool, repo):
        await tool.execute_with_context({"entity_type": "ip", "limit": 0}, _ctx())
        assert repo.list_top_entities.await_args.kwargs["limit"] == 1

    @pytest.mark.asyncio
    async def test_non_integer_limit_rejected(self, tool):
        result = await tool.execute_with_context(
            {"entity_type": "ip", "limit": "lots"}, _ctx()
        )
        assert result.success is False
        assert "limit" in result.error

    @pytest.mark.asyncio
    async def test_default_limit_applies(self, tool, repo):
        await tool.execute_with_context({"entity_type": "ip"}, _ctx())
        assert repo.list_top_entities.await_args.kwargs["limit"] == 10

    @pytest.mark.asyncio
    async def test_repo_exception_degrades_to_error_result(self, tool, repo):
        repo.list_top_entities.side_effect = RuntimeError("boom")
        result = await tool.execute_with_context({"entity_type": "ip"}, _ctx())
        assert result.success is False
        assert "boom" in result.error
