"""Phase 3b — ListEvidenceByTimeTool

Covers input parsing (ISO-8601 bound validation), service delegation,
and the shape of the returned ToolResult data.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.modules.agent.tools.base import ToolContext
from faultmaven.modules.agent.tools.list_evidence_by_time_tool import (
    ListEvidenceByTimeTool,
)


def _ctx() -> ToolContext:
    return ToolContext(
        session_id="sess_1",
        case_id="case_123",
        organization_id="org_1",
        user_id="user_1",
    )


def _evidence_stub(evidence_id: str, start: datetime, end: datetime):
    ev = MagicMock()
    ev.evidence_id = evidence_id
    ev.original_filename = "server.log"
    ev.data_type = "logs"
    ev.coverage_start_ts = start
    ev.coverage_end_ts = end
    ev.summary = "stub"
    return ev


@pytest.fixture
def repo():
    r = MagicMock()
    r.list_evidence_by_time_window = AsyncMock(return_value=[])
    return r


@pytest.fixture
def tool(repo):
    return ListEvidenceByTimeTool(case_repository=repo)


class TestSchema:
    def test_name(self, tool):
        assert tool.name == "list_evidence_by_time"

    def test_properties_optional(self, tool):
        schema = tool.parameters_schema
        assert schema["required"] == []
        props = schema["properties"]
        assert "start_ts" in props
        assert "end_ts" in props


class TestInputValidation:
    @pytest.mark.asyncio
    async def test_bad_start_ts_format_surfaces_error(self, tool):
        result = await tool.execute_with_context(
            params={"start_ts": "not-a-date"}, context=_ctx()
        )
        assert result.success is False
        assert "start_ts" in result.error
        assert "not-a-date" in result.error

    @pytest.mark.asyncio
    async def test_bad_end_ts_format_surfaces_error(self, tool):
        result = await tool.execute_with_context(
            params={"end_ts": "14:30"}, context=_ctx()
        )
        assert result.success is False
        assert "end_ts" in result.error

    @pytest.mark.asyncio
    async def test_missing_repository_returns_error(self):
        tool = ListEvidenceByTimeTool(case_repository=None)
        result = await tool.execute_with_context(params={}, context=_ctx())
        assert result.success is False
        assert "not wired" in result.error.lower() or "unavail" in result.error.lower()


class TestDelegation:
    @pytest.mark.asyncio
    async def test_no_bounds_calls_repo_with_nones(self, tool, repo):
        await tool.execute_with_context(params={}, context=_ctx())
        kwargs = repo.list_evidence_by_time_window.call_args.kwargs
        assert kwargs["case_id"] == "case_123"
        assert kwargs["start"] is None
        assert kwargs["end"] is None

    @pytest.mark.asyncio
    async def test_bounds_parsed_and_passed_through(self, tool, repo):
        await tool.execute_with_context(
            params={
                "start_ts": "2026-04-23T14:00:00",
                "end_ts": "2026-04-23T15:00:00",
            },
            context=_ctx(),
        )
        kwargs = repo.list_evidence_by_time_window.call_args.kwargs
        assert kwargs["start"] == datetime(2026, 4, 23, 14, 0, 0)
        assert kwargs["end"] == datetime(2026, 4, 23, 15, 0, 0)

    @pytest.mark.asyncio
    async def test_response_shape_contains_expected_fields(self, tool, repo):
        repo.list_evidence_by_time_window.return_value = [
            _evidence_stub(
                "ev_test",
                datetime(2026, 4, 23, 14, 0, tzinfo=timezone.utc),
                datetime(2026, 4, 23, 14, 30, tzinfo=timezone.utc),
            ),
        ]
        result = await tool.execute_with_context(params={}, context=_ctx())
        assert result.success is True
        assert result.data["count"] == 1
        assert result.data["case_id"] == "case_123"
        ev = result.data["evidence"][0]
        assert ev["evidence_id"] == "ev_test"
        assert ev["coverage_start_ts"] == "2026-04-23T14:00:00+00:00"
        assert ev["coverage_end_ts"] == "2026-04-23T14:30:00+00:00"

    @pytest.mark.asyncio
    async def test_repo_exception_becomes_tool_error(self, tool, repo):
        repo.list_evidence_by_time_window.side_effect = RuntimeError(
            "DB connection lost"
        )
        result = await tool.execute_with_context(params={}, context=_ctx())
        assert result.success is False
        assert "DB connection lost" in result.error


# ---------------------------------------------------------------------------
# #1271 item 4 / #1274 — un-promoted files, and the provenance of every span
# ---------------------------------------------------------------------------


def _uploaded_stub(file_id, start, end, source="caller_declared"):
    uf = MagicMock()
    uf.file_id = file_id
    uf.display_name = "pasted text (turn 1)"
    uf.data_type = "alert"
    uf.coverage_start_ts = start
    uf.coverage_end_ts = end
    uf.coverage_source = source
    uf.summary = "forwarded alert"
    return uf


def _case_with(files, evidence=()):
    case = MagicMock()
    case.uploaded_files = list(files)
    case.evidence = list(evidence)
    return case


class TestUnpromotedFilesAreAnswersToo:
    @pytest.mark.asyncio
    async def test_a_dated_file_with_no_evidence_row_is_returned(self, tool):
        """INV-07 keeps a forwarded alert un-promoted through INQUIRY. Answering
        "what was observed when" with silence, while the prompt shows that same
        file carrying observed_through, teaches the model to distrust the
        attribute."""

        instant = datetime(2026, 8, 30, 11, 38, 37, tzinfo=timezone.utc)
        ctx = _ctx()
        ctx.in_memory_case = _case_with([_uploaded_stub("file_abc", instant, instant)])

        result = await tool.execute_with_context({}, ctx)

        assert result.success
        rows = result.data["evidence"]
        assert [r["file_id"] for r in rows] == ["file_abc"]
        assert rows[0]["coverage_source"] == "caller_declared"

    @pytest.mark.asyncio
    async def test_a_promoted_file_is_not_listed_twice(self, tool):
        """Once an Evidence row cites the file, the repository query returns it;
        emitting the file as well would double-count one observation."""

        instant = datetime(2026, 8, 30, 11, 38, 37, tzinfo=timezone.utc)
        ev = MagicMock()
        ev.source_file_id = "file_abc"
        ctx = _ctx()
        ctx.in_memory_case = _case_with(
            [_uploaded_stub("file_abc", instant, instant)], evidence=[ev]
        )

        result = await tool.execute_with_context({}, ctx)
        assert result.data["evidence"] == []

    @pytest.mark.asyncio
    async def test_a_timeless_file_stays_excluded(self, tool):
        """Same exclusion the repository query applies to evidence."""

        ctx = _ctx()
        ctx.in_memory_case = _case_with([_uploaded_stub("file_abc", None, None)])
        result = await tool.execute_with_context({}, ctx)
        assert result.data["evidence"] == []

    @pytest.mark.asyncio
    async def test_a_file_outside_the_window_is_excluded(self, tool):
        instant = datetime(2026, 8, 30, 11, 38, 37, tzinfo=timezone.utc)
        ctx = _ctx()
        ctx.in_memory_case = _case_with([_uploaded_stub("file_abc", instant, instant)])

        result = await tool.execute_with_context(
            {"start_ts": "2026-08-30T20:00:00+00:00"}, ctx
        )
        assert result.data["evidence"] == []

    @pytest.mark.asyncio
    async def test_the_unvouched_source_is_reported_not_hidden(self, tool):
        """The prompt withholds an epoch_s span; the tool must not silently
        present it as fact instead — it reports the span WITH its provenance so
        both surfaces say the same thing about how much it is worth."""

        instant = datetime(2026, 8, 30, 11, 38, 37, tzinfo=timezone.utc)
        ctx = _ctx()
        ctx.in_memory_case = _case_with(
            [_uploaded_stub("file_abc", instant, instant, source="epoch_s")]
        )

        rows = (await tool.execute_with_context({}, ctx)).data["evidence"]
        assert rows[0]["coverage_source"] == "epoch_s"
