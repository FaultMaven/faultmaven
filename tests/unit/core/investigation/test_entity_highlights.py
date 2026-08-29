"""Phase 4c — ``fetch_entity_highlights`` + context-builder slot.

The helper reads highlight ROWS out of the Phase 4 ``case_entities``
registry; the context builder formats them into ``<entity_highlights>``
inside the prompt's shared fence and carries the result through into
the ctx dict for the INVESTIGATING template.

The fetch/format split is #1228: the values are extracted from uploaded
file content, so the block has to be fenced, and ``render_fenced``'s
safety property is that it can RE-RENDER on a token collision — which it
cannot do around an awaited database query. So the fetch returns rows and
the formatting is pure.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.prompts.context_builder import (
    _ENTITY_HIGHLIGHTS_PREAMBLE,
    EntityHighlightGroup,
    EntityHighlightRow,
    _render_entity_highlights,
    build_investigation_context,
    fetch_entity_highlights,
)
from faultmaven.core.investigation.prompts.fence import render_fenced
from faultmaven.modules.case.domain.models import (
    Case,
    CaseEntity,
    CaseState,
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
    per type. The tests inspect the rows and the block rendered from
    them; what matters is that each ``EntityType`` the helper queries
    gets routed to the right bucket."""
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


def _render(groups) -> str:
    """The block the assembly would emit for ``groups`` (own fence token)."""
    return render_fenced(lambda f: _render_entity_highlights(groups, f))


class TestFetchEntityHighlights:
    @pytest.mark.asyncio
    async def test_returns_empty_when_repo_is_none(self):
        result = await fetch_entity_highlights(None, "case_xyz")
        assert result == []
        assert _render(result) == ""

    @pytest.mark.asyncio
    async def test_returns_empty_when_repo_lacks_method(self):
        class LegacyRepo:
            pass

        result = await fetch_entity_highlights(LegacyRepo(), "case_xyz")
        assert result == []
        assert _render(result) == ""

    @pytest.mark.asyncio
    async def test_formats_block_with_only_populated_types(self, repo_with_entities):
        groups = await fetch_entity_highlights(repo_with_entities, "case_xyz")
        assert [g.entity_type for g in groups] == ["ip", "hostname"]
        assert groups[0].rows == (
            EntityHighlightRow("10.0.0.5", 12, True),
            EntityHighlightRow("10.0.0.6", 3, False),
        )

        result = _render(groups)
        # The standing instruction is renderer-owned and sits ABOVE the opening
        # delimiter (#1228) — the rule demotes unfenced text INSIDE a fenced
        # block to quoted case content, and an instruction there goes with it.
        assert result.startswith(_ENTITY_HIGHLIGHTS_PREAMBLE)
        assert f'<entity_highlights fence="{_token(result)}">' in result
        assert result.endswith('</entity_highlights fence="%s">' % _token(result))
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
        assert result == []
        assert _render(result) == ""

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
        groups = await fetch_entity_highlights(repo, "case_xyz")
        assert [g.entity_type for g in groups] == ["hostname"]
        result = _render(groups)
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
            state=CaseState.INVESTIGATING,
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
        groups = [
            EntityHighlightGroup(
                entity_type="ip", rows=(EntityHighlightRow("10.0.0.5", 3),)
            )
        ]
        ctx = build_investigation_context(case, "hello", entity_highlight_groups=groups)
        block = ctx["entity_highlights"]
        assert "ip:" in block
        assert "10.0.0.5 ×3" in block
        # Rendered on the PROMPT's fence, not one of its own (#1228).
        assert _token(block) == _token(ctx["core_context"])


def _token(rendered: str) -> str:
    """The fence token on the first fenced delimiter in ``rendered``."""
    import re

    m = re.search(r'fence="([0-9a-f]+)"', rendered)
    assert m, rendered
    return m.group(1)
