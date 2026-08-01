"""Tests for VectorizeFileTool — on-demand vectorization for semantic search.

Storage redesign 2026-04 phase 2: VectorizeFileTool reads evidence from
`case.evidence` (via `context.in_memory_case` / `context.case_repository`)
instead of the deleted standalone evidence service.
"""

from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.core.preprocessing.vector_storage import VectorIndexOutcome
from faultmaven.modules.agent.tools.base import ToolContext
from faultmaven.modules.agent.tools.vectorize_file_tool import (
    VECTORIZATION_MAX_SIZE_BYTES,
    VECTORIZED_SYSTEM_MESSAGE,
    VectorizeFileTool,
    append_vectorization_advisory,
)


def _make_evidence(
    evidence_id: str = "ev_abc",
    case_id: str = "case_123",
    size_bytes: int = 100_000,
    extract: Optional[str] = "Structural index content here...",
    source_type_value: str = "logs",
    source_file_id: str = "file_aaaa11112222",
):
    ev = MagicMock()
    ev.evidence_id = evidence_id
    ev.case_id = case_id
    ev.extract = extract
    ev.source_type.value = source_type_value
    ev.source_file_id = source_file_id
    # Stash size_bytes for the helper that builds the case so we can wire up
    # the matching UploadedFile (production reads size from the file).
    ev._test_size_bytes = size_bytes
    return ev


def _make_context(case_id: str = "case_123", evidence_items=None):
    case = MagicMock()
    case.case_id = case_id
    case.evidence = evidence_items if evidence_items is not None else [_make_evidence()]

    # Build a uploaded_files list and wire find_uploaded_file to return matches.
    # Post-010: preprocessing artifacts (structural_index, data_type) live
    # on the UploadedFile, not on Evidence. The vectorize_file tool reads
    # from the file row, so the test fixture must populate those fields.
    uploaded_files = []
    for ev in case.evidence:
        f = MagicMock()
        f.file_id = ev.source_file_id
        f.size_bytes = getattr(ev, "_test_size_bytes", 0)
        f.filename = "test.log"
        f.upload_source = "file_upload"
        # Mirror the evidence's extract into the file's structural_index —
        # this is what production now reads (batch 4).
        f.structural_index = ev.extract
        f.data_type = ev.source_type.value
        uploaded_files.append(f)
    case.uploaded_files = uploaded_files

    def _find_uploaded_file(file_id):
        if not file_id:
            return None
        return next((f for f in uploaded_files if f.file_id == file_id), None)

    case.find_uploaded_file.side_effect = _find_uploaded_file

    return ToolContext(
        session_id="sess_1",
        case_id=case_id,
        organization_id="org_1",
        user_id="user_1",
        in_memory_case=case,
    )


@pytest.fixture
def mock_settings():
    """Patch get_settings to return controllable vectorization threshold."""
    settings = MagicMock()
    settings.agent.vectorization_min_size_bytes = 50_000  # 50KB default
    with patch(
        "faultmaven.modules.agent.tools.vectorize_file_tool.get_settings",
        return_value=settings,
    ):
        yield settings


@pytest.fixture
def tool():
    return VectorizeFileTool(
        case_vector_store=MagicMock(),
        storage_service=MagicMock(),
    )


@pytest.fixture
def context():
    return _make_context()


class TestSizeGates:
    @pytest.mark.asyncio
    async def test_rejects_file_below_minimum(self, tool, mock_settings):
        ev = _make_evidence(size_bytes=10_000)
        context = _make_context(evidence_items=[ev])

        result = await tool.execute_with_context(
            params={"evidence_id": "ev_abc"},
            context=context,
        )

        assert result.success is False
        assert "too small" in result.error
        assert str(mock_settings.agent.vectorization_min_size_bytes) in result.error

    @pytest.mark.asyncio
    async def test_rejects_file_above_maximum(self, tool, mock_settings):
        ev = _make_evidence(size_bytes=60_000_000)
        context = _make_context(evidence_items=[ev])

        result = await tool.execute_with_context(
            params={"evidence_id": "ev_abc"},
            context=context,
        )

        assert result.success is False
        assert "too large" in result.error
        assert str(VECTORIZATION_MAX_SIZE_BYTES) in result.error

    @pytest.mark.asyncio
    async def test_accepts_file_within_range(self, tool, context, mock_settings):
        # Must return a real VectorIndexOutcome: the tool now branches on it to
        # decide whether it may claim the file is searchable (#941), so a bare
        # AsyncMock returning a MagicMock is a double that no longer stands in
        # for production.
        with patch(
            "faultmaven.core.preprocessing.vector_storage.store_in_vector_db_background",
            new_callable=AsyncMock,
            return_value=VectorIndexOutcome.INDEXED,
        ) as mock_store:
            result = await tool.execute_with_context(
                params={"evidence_id": "ev_abc"},
                context=context,
            )

        assert result.success is True
        assert "vectorized" in result.data["message"]
        mock_store.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_custom_min_size_threshold(self, tool, mock_settings):
        mock_settings.agent.vectorization_min_size_bytes = 200_000
        ev = _make_evidence(size_bytes=100_000)
        context = _make_context(evidence_items=[ev])

        result = await tool.execute_with_context(
            params={"evidence_id": "ev_abc"},
            context=context,
        )

        assert result.success is False
        assert "too small" in result.error
        assert "200000" in result.error


class TestVectorization:
    @pytest.mark.asyncio
    async def test_calls_store_function(self, tool, context, mock_settings):
        with patch(
            "faultmaven.core.preprocessing.vector_storage.store_in_vector_db_background",
            new_callable=AsyncMock,
            return_value=VectorIndexOutcome.INDEXED,
        ) as mock_store:
            await tool.execute_with_context(
                params={"evidence_id": "ev_abc"},
                context=context,
            )

        mock_store.assert_awaited_once()
        call_kwargs = mock_store.call_args[1]
        assert call_kwargs["case_id"] == "case_123"
        assert call_kwargs["evidence_id"] == "ev_abc"
        assert "Structural index" in call_kwargs["structural_index"]

    @pytest.mark.asyncio
    async def test_no_preprocessed_content(self, tool, mock_settings):
        # Post-010: structural_index lives on uploaded_files; the
        # _make_context helper mirrors evidence.extract → file.structural_index,
        # so extract=None yields a file with no structural_index.
        ev = _make_evidence(extract=None)
        context = _make_context(evidence_items=[ev])

        result = await tool.execute_with_context(
            params={"evidence_id": "ev_abc"},
            context=context,
        )

        assert result.success is False
        assert "no preprocessed structural_index" in result.error


class TestOutcomeIsReportedNotAssumed:
    """Only an index that exists may be announced (#941).

    ``store_in_vector_db_background`` returned ``None`` whether it wrote chunks,
    skipped for an unavailable embedder, found nothing to chunk, or failed — so
    this tool said "vectorized and is now searchable via case_evidence_search"
    for all four. The model then searched, got nothing, and read it as "this file
    does not contain that": an index that was never written laundered into a
    finding about the evidence.

    These drive ``execute_with_context`` rather than the renderer it delegates
    to. The renderer being right is not the property — the tool *reaching* it is,
    and asserting on the helper's return value would pass against a tool that
    never calls it.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "outcome,expect_success,expect_searchable",
        [
            (VectorIndexOutcome.INDEXED, True, True),
            (VectorIndexOutcome.EMBEDDER_UNAVAILABLE, False, False),
            (VectorIndexOutcome.FAILED, False, False),
            (VectorIndexOutcome.NOTHING_TO_INDEX, True, False),
        ],
        ids=["indexed", "embedder_unavailable", "failed", "nothing_to_index"],
    )
    async def test_searchability_is_claimed_only_when_it_is_true(
        self, tool, context, mock_settings, outcome, expect_success, expect_searchable
    ):
        with patch(
            "faultmaven.core.preprocessing.vector_storage.store_in_vector_db_background",
            new_callable=AsyncMock,
            return_value=outcome,
        ):
            result = await tool.execute_with_context(
                params={"evidence_id": "ev_abc"},
                context=context,
            )

        assert result.success is expect_success
        rendered = f"{result.error or ''} {(result.data or {}).get('message', '')}"
        assert ("is now searchable" in rendered) is expect_searchable, (
            f"{outcome.value} rendered as searchable="
            f"{'is now searchable' in rendered}: {rendered!r}"
        )

        # `indexed` is the machine-readable half of the same statement, and the
        # ONLY half the engines read — they never render `message`. Both success
        # arms must state it: the gate upstream is `data.get("indexed") is
        # False`, which a missing key passes, so an unstated key silently means
        # "searchable" (#941).
        if result.success:
            assert result.data["indexed"] is expect_searchable

    @pytest.mark.asyncio
    async def test_an_unwritten_index_tells_the_model_to_conclude_nothing(
        self, tool, context, mock_settings
    ):
        """Same rule the KB adapters follow: a layer that could not do its job
        establishes nothing, and has to say so — otherwise the empty
        ``case_evidence_search`` that follows is read as a finding."""
        with patch(
            "faultmaven.core.preprocessing.vector_storage.store_in_vector_db_background",
            new_callable=AsyncMock,
            return_value=VectorIndexOutcome.EMBEDDER_UNAVAILABLE,
        ):
            result = await tool.execute_with_context(
                params={"evidence_id": "ev_abc"},
                context=context,
            )

        assert result.success is False
        assert result.data is None
        assert "says nothing about its contents" in result.error.lower()


class TestValidation:
    @pytest.mark.asyncio
    async def test_missing_evidence_id(self, tool, context):
        result = await tool.execute_with_context(
            params={},
            context=context,
        )
        assert result.success is False
        assert "evidence_id" in result.error

    @pytest.mark.asyncio
    async def test_no_vector_store(self, context):
        tool = VectorizeFileTool(case_vector_store=None)
        result = await tool.execute_with_context(
            params={"evidence_id": "ev_abc"},
            context=context,
        )
        assert result.success is False
        assert "not available" in result.error

    @pytest.mark.asyncio
    async def test_evidence_not_found(self, tool, mock_settings):
        # No evidence on the case at all.
        context = _make_context(evidence_items=[])
        result = await tool.execute_with_context(
            params={"evidence_id": "ev_missing"},
            context=context,
        )
        assert result.success is False
        assert "not found" in result.error


class TestToolProperties:
    def test_name(self, tool):
        assert tool.name == "vectorize_file"

    def test_schema(self, tool):
        schema = tool.parameters_schema
        assert "evidence_id" in schema["properties"]
        assert schema["required"] == ["evidence_id"]


class TestAdvisoryRuleIsStatedOnce:
    """``append_vectorization_advisory`` holds the rule the four emission sites
    used to restate — two in ``MilestoneEngine``, two in
    ``AgentOrchestrationService``, with the message text copied inline.

    The advisory is not cosmetic: it sends the model to
    ``case_evidence_search``. Claiming it for a file that was never written
    guarantees an empty search the model then reads as a statement about the
    file's contents (#941).
    """

    @pytest.mark.parametrize(
        "indexed,expect_appended",
        [(True, True), (False, False)],
        ids=["indexed", "indexed_nothing"],
    )
    def test_appended_only_for_a_file_that_is_really_indexed(
        self, indexed, expect_appended
    ):
        out = append_vectorization_advisory("before", indexed)

        assert (out != "before") is expect_appended
        assert ("is now searchable" in out) is False  # tool wording, not this one
        assert (VECTORIZED_SYSTEM_MESSAGE in out) is expect_appended

    def test_not_duplicated_when_already_present(self):
        """Two emission sites can fire for one evidence in a turn."""
        once = append_vectorization_advisory("before", True)

        assert append_vectorization_advisory(once, True) == once

    @pytest.mark.parametrize("falsy", [False, None, 0, ""])
    def test_anything_that_is_not_a_confirmed_index_is_refused(self, falsy):
        """Fail-closed. The callers derive this from a tool payload, so the
        rule has to hold for whatever "we were not told it is indexed" looks
        like at the call site, not just for literal False."""
        assert append_vectorization_advisory("before", falsy) == "before"
