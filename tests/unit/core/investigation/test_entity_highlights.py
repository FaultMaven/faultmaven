"""Phase 4c — ``fetch_entity_highlights`` + context-builder slot.

The helper pre-formats the ``<entity_highlights>`` block from the
Phase 4 ``case_entities`` registry. The context builder carries it
through into the ctx dict so the INVESTIGATING template can drop it
in directly.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.prompts.context_builder import (
    build_investigation_context,
    fetch_entity_highlights,
)
from faultmaven.modules.case.domain.models import (
    Case,
    CaseEntity,
    CaseStatus,
    EntityType,
    InquiryData,
)


def _entity(
    *,
    entity_type: EntityType,
    entity_value: str,
    mention_count: int,
    in_error_context: bool = False,
) -> CaseEntity:
    return CaseEntity(
        case_id="case_abcdef012345",
        entity_type=entity_type,
        entity_value=entity_value,
        evidence_id="ev_aaaaaaaaaaaa",
        mention_count=mention_count,
        in_error_context=in_error_context,
    )


@pytest.fixture
def repo_with_entities():
    """Repo stub whose ``list_top_entities`` returns predictable rows
    per type. The tests inspect the formatted block; what matters is
    that each ``EntityType`` the helper queries gets routed to the
    right bucket."""
    repo = MagicMock()

    async def fake_list(case_id, entity_type, limit=10):
        if entity_type == EntityType.IP:
            return [
                _entity(
                    entity_type=EntityType.IP,
                    entity_value="10.0.0.5",
                    mention_count=12,
                    in_error_context=True,
                ),
                _entity(
                    entity_type=EntityType.IP,
                    entity_value="10.0.0.6",
                    mention_count=3,
                ),
            ]
        if entity_type == EntityType.HOSTNAME:
            return [
                _entity(
                    entity_type=EntityType.HOSTNAME,
                    entity_value="db-master",
                    mention_count=7,
                )
            ]
        return []

    repo.list_top_entities = AsyncMock(side_effect=fake_list)
    return repo


class TestFetchEntityHighlights:
    @pytest.mark.asyncio
    async def test_returns_empty_when_repo_is_none(self):
        result = await fetch_entity_highlights(None, "case_xyz")
        assert result == ""

    @pytest.mark.asyncio
    async def test_returns_empty_when_repo_lacks_method(self):
        class LegacyRepo:
            pass

        result = await fetch_entity_highlights(LegacyRepo(), "case_xyz")
        assert result == ""

    @pytest.mark.asyncio
    async def test_formats_block_with_only_populated_types(self, repo_with_entities):
        result = await fetch_entity_highlights(repo_with_entities, "case_xyz")
        assert result.startswith("<entity_highlights>")
        assert result.endswith("</entity_highlights>")
        assert "ip:" in result
        assert "10.0.0.5 ×12 (error)" in result
        assert "10.0.0.6 ×3" in result
        assert "hostname:" in result
        assert "db-master ×7" in result
        # Types with zero rows (user, service) must NOT appear as
        # empty headings — only populated types surface.
        assert "user:\n" not in result
        assert "service:\n" not in result

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_types_have_rows(self):
        repo = MagicMock()
        repo.list_top_entities = AsyncMock(return_value=[])
        result = await fetch_entity_highlights(repo, "case_xyz")
        assert result == ""

    @pytest.mark.asyncio
    async def test_per_type_query_failure_is_skipped(self):
        """A single type's query raising must not nuke the whole
        block — the helper should skip that type and keep the others."""
        repo = MagicMock()

        async def flaky(case_id, entity_type, limit):
            if entity_type == EntityType.IP:
                raise RuntimeError("ip bucket broken")
            if entity_type == EntityType.HOSTNAME:
                return [
                    _entity(
                        entity_type=EntityType.HOSTNAME,
                        entity_value="db-01",
                        mention_count=2,
                    )
                ]
            return []

        repo.list_top_entities = AsyncMock(side_effect=flaky)
        result = await fetch_entity_highlights(repo, "case_xyz")
        assert "hostname:" in result
        assert "db-01" in result
        assert "ip:" not in result

    @pytest.mark.asyncio
    async def test_respects_per_type_limit_kwarg(self):
        repo = MagicMock()
        repo.list_top_entities = AsyncMock(return_value=[])
        await fetch_entity_highlights(repo, "case_xyz", per_type_limit=3)
        # All calls use the supplied limit.
        for call in repo.list_top_entities.await_args_list:
            assert call.kwargs["limit"] == 3


class TestContextBuilderSlot:
    def _make_investigating_case(self) -> Case:
        now = datetime.now(timezone.utc)
        return Case(
            case_id="case_abcdef012345",
            user_id="u",
            organization_id="o",
            title="t",
            description="d",
            status=CaseStatus.INVESTIGATING,
            inquiry=InquiryData(
                problem_statement_confirmed=True,
                decided_to_investigate=True,
                proposed_problem_statement="test",
            ),
            created_at=now,
            updated_at=now,
            last_activity_at=now,
        )

    def test_entity_highlights_key_is_always_present(self):
        case = self._make_investigating_case()
        ctx = build_investigation_context(case, "hello")
        assert "entity_highlights" in ctx
        assert ctx["entity_highlights"] == ""

    def test_passing_highlights_surfaces_in_ctx(self):
        case = self._make_investigating_case()
        block = "<entity_highlights>\nip:\n  - 10.0.0.5 ×3\n</entity_highlights>"
        ctx = build_investigation_context(case, "hello", entity_highlights=block)
        assert ctx["entity_highlights"] == block
