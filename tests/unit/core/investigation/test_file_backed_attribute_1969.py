"""#1969 — the row attribute marks provenance (``file_backed``), not capability.

``<evidence>`` and ``<uploaded_file>`` rows used to carry ``searchable="true"``
to mark a row whose raw file the case holds, as opposed to an engine inference.
Since #1939 a tool-less turn gives the model no search tool, so the name claimed
a capability the turn may lack. The marker is ``file_backed="true"``, rendered on
exactly the rows that carried the old one, whatever the turn's tools:

* ``<evidence>`` (Tier A and Tier B): the row's file is held —
  ``source_file_id`` is set AND the case resolves it AND it is stored
  (``storage_ref``, #2375);
* ``<evidence>`` Tier C (an engine inference, no file): never;
* ``<uploaded_file>``: when its file is stored (#2375), in the full,
  summary-only and fallback-stub renders, with or without ``search_file``
  given.

A file with no ``storage_ref`` is one no tool can read, so the prompt does not
call it file-backed (#2375).

Every instruction that keyed on the old attribute keys on the new one, and no
prompt text or tool error string calls a row "searchable".
"""

from __future__ import annotations

import re
from unittest.mock import AsyncMock

import pytest

from faultmaven.core.investigation.milestone_engine.tool_messages import (
    _build_da_system_instruction,
)
from faultmaven.core.investigation.prompts.context_builder.budget import (
    EVIDENCE_CONTEXT_MAX_CHARS_PER_ITEM,
)
from faultmaven.core.investigation.prompts.context_builder.evidence import (
    _build_evidence_context,
    _render_orphan_file_block,
)
from faultmaven.core.investigation.prompts.fence import PromptFence, mint_token
from faultmaven.core.investigation.prompts.templates.assembly import (
    get_prompt_for_case,
)
from faultmaven.core.investigation.prompts.templates.fallback import (
    get_fallback_prompt_for_case,
)
from faultmaven.core.investigation.prompts.templates.tool_text import NO_TOOLS
from faultmaven.modules.agent.tools.base import ToolContext
from faultmaven.modules.agent.tools.deep_analysis_tool import DeepAnalysisTool
from faultmaven.modules.agent.tools.search_file_tool import SearchFileTool
from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    InquiryData,
    UploadedFile,
)
from tests.case_builders import investigating_case
from tests.model_content import disabled_redaction
from tests.unit.core.investigation.given_tools import DEFAULT_GIVEN

pytestmark = pytest.mark.unit

FILE_BACKED = 'file_backed="true"'
#: The attribute this one replaced, as it was written on a row.
OLD_ATTRIBUTE = 'searchable="'

_MSG = "what does the log show?"


def _fid(i: int) -> str:
    return f"file_{i:012x}"


def _eid(i: int) -> str:
    return f"ev_{i:012x}"


def _file(i: int, turn: int = 1, stored: bool = True) -> UploadedFile:
    return UploadedFile(
        file_id=_fid(i),
        filename=f"app{i}.log",
        size_bytes=100,
        content_type="text/plain",
        uploaded_at_turn=turn,
        upload_source="file_upload",
        storage_ref=f"evidence/case_1969/{i}.txt" if stored else None,
        data_type="logs",
        summary="App log.",
        structural_index=f"2026-07-09 10:55:31 ERROR STRUCT-{i}\nline two\n",
    )


def _evidence(
    i: int,
    file_id: str | None,
    turn: int = 1,
    source_type: EvidenceSourceType = EvidenceSourceType.LOGS,
) -> Evidence:
    return Evidence(
        evidence_id=_eid(i),
        source_file_id=file_id,
        summary=f"summary {i}",
        extract=None,
        category=EvidenceCategory.SYMPTOM_EVIDENCE,
        source_type=source_type,
        primary_purpose="Test",
        collected_by="user_1969",
        collected_at_turn=turn,
    )


def _case(files, evidence, turn: int = 8) -> Case:
    return investigating_case(
        case_id="case_1969aaaaaaaa",
        title="API timeouts",
        description="Upstream timeouts since 10:00",
        user_id="user_1969",
        enterprise_id="org_1969",
        statement="API timeouts",
        uploaded_files=list(files),
        evidence=list(evidence),
        current_turn=turn,
    )


def _open_tag(rendered: str, element: str, **attrs: str) -> str:
    """The one opening tag of ``element`` carrying every ``attr="value"``."""
    found = [
        tag
        for tag in re.findall(rf"<{element}\s[^>]*>", rendered)
        if all(f'{k}="{v}"' in tag for k, v in attrs.items())
    ]
    assert len(found) == 1, (element, attrs, found, rendered)
    return found[0]


#: Both given sets a render can be built for: no tools at all, and the default
#: six. The attribute is provenance, so it must not depend on the choice.
GIVEN_SETS = {"toolless": NO_TOOLS, "default_six": DEFAULT_GIVEN}


@pytest.mark.parametrize("given", GIVEN_SETS.values(), ids=GIVEN_SETS.keys())
class TestEvidenceRows:
    def test_tier_a_and_tier_b_rows_with_a_held_file_are_file_backed(self, given):
        # Five rows over five held files: the three newest render the file
        # extract (Tier A), the two oldest only their summary (Tier B).
        files = [_file(i, turn=i + 1) for i in range(1, 6)]
        rows = [_evidence(i, _fid(i), turn=i + 1) for i in range(1, 6)]

        rendered = _build_evidence_context(_case(files, rows), given_tools=given)

        for i in range(1, 6):
            tag = _open_tag(rendered, "evidence", id=_eid(i))
            assert FILE_BACKED in tag, tag
        # Non-vacuity: the rows really span both tiers.
        for tier_a in (3, 4, 5):
            assert f"STRUCT-{tier_a}" in rendered
        for tier_b in (1, 2):
            assert f"STRUCT-{tier_b}" not in rendered
            assert f"summary {tier_b}" in rendered
        assert OLD_ATTRIBUTE not in rendered

    @pytest.mark.parametrize("with_older_rows", [False, True], ids=["tier_a", "tier_b"])
    def test_a_row_whose_file_is_not_held_is_not_file_backed(
        self, given, with_older_rows
    ):
        # ``source_file_id`` is set but the case does not resolve it: no
        # ``file_id`` attribute and no marker. Alone it is the newest row
        # (Tier A); behind three held rows it falls to Tier B.
        missing = _evidence(9, _fid(0xDEAD), turn=1)
        held = [_evidence(i, _fid(i), turn=i + 1) for i in range(1, 4)]
        files = [_file(i, turn=i + 1) for i in range(1, 4)]
        case = _case(
            files if with_older_rows else [],
            [missing, *held] if with_older_rows else [missing],
        )

        rendered = _build_evidence_context(case, given_tools=given)

        tag = _open_tag(rendered, "evidence", id=_eid(9))
        assert FILE_BACKED not in tag, tag
        assert "file_id=" not in tag, tag
        if with_older_rows:
            for i in range(1, 4):
                assert FILE_BACKED in _open_tag(rendered, "evidence", id=_eid(i))

    @pytest.mark.parametrize("with_older_rows", [False, True], ids=["tier_a", "tier_b"])
    def test_a_row_whose_file_is_not_stored_is_not_file_backed(
        self, given, with_older_rows
    ):
        # #2375: the case resolves the file but holds no stored copy of it, so
        # no tool can read it. The ``file_id`` still renders; the marker not.
        unstored = _evidence(9, _fid(9), turn=1)
        held = [_evidence(i, _fid(i), turn=i + 1) for i in range(1, 4)]
        files = [_file(i, turn=i + 1) for i in range(1, 4)]
        case = _case(
            [_file(9, turn=1, stored=False), *(files if with_older_rows else [])],
            [unstored, *held] if with_older_rows else [unstored],
        )

        rendered = _build_evidence_context(case, given_tools=given)

        tag = _open_tag(rendered, "evidence", id=_eid(9))
        assert FILE_BACKED not in tag, tag
        assert f'file_id="{_fid(9)}"' in tag, tag
        assert ("STRUCT-9" in rendered) is not with_older_rows  # the tier is real
        if with_older_rows:
            for i in range(1, 4):
                assert FILE_BACKED in _open_tag(rendered, "evidence", id=_eid(i))

    def test_an_engine_inference_is_never_file_backed(self, given):
        inference = _evidence(
            10, None, turn=2, source_type=EvidenceSourceType.ENGINE_INFERENCE
        )
        held = _evidence(1, _fid(1), turn=1)

        rendered = _build_evidence_context(
            _case([_file(1)], [held, inference]), given_tools=given
        )

        assert FILE_BACKED in _open_tag(rendered, "evidence", id=_eid(1))
        assert FILE_BACKED not in _open_tag(rendered, "evidence", id=_eid(10))

    @pytest.mark.parametrize("stored", [True, False], ids=["stored", "unstored"])
    def test_an_uploaded_file_is_file_backed_when_stored(self, given, stored):
        # No Evidence row yet (INQUIRY, or an un-promoted upload).
        rendered = _build_evidence_context(
            _case([_file(1, turn=8, stored=stored)], []), given_tools=given
        )

        tag = _open_tag(rendered, "uploaded_file", file_id=_fid(1))
        assert (FILE_BACKED in tag) is stored, tag
        assert OLD_ATTRIBUTE not in rendered

    @pytest.mark.parametrize("stored", [True, False], ids=["stored", "unstored"])
    @pytest.mark.parametrize(
        "summary_only", [False, True], ids=["full", "summary_only"]
    )
    def test_both_orphan_renders_carry_it(self, given, summary_only, stored):
        block = _render_orphan_file_block(
            _file(1, stored=stored),
            {},
            1,
            summary_only=summary_only,
            fence=PromptFence(mint_token()),
            given=given,
        )

        tag = _open_tag(block, "uploaded_file", file_id=_fid(1))
        assert (FILE_BACKED in tag) is stored, tag
        assert OLD_ATTRIBUTE not in block


@pytest.mark.parametrize("stored", [True, False], ids=["stored", "unstored"])
@pytest.mark.parametrize("given", GIVEN_SETS.values(), ids=GIVEN_SETS.keys())
def test_the_fallback_stub_is_file_backed_whatever_the_tools(given, stored):
    """The fallback stub used to add ``searchable`` only when ``search_file`` was
    given. Provenance does not depend on the turn's tools: it renders with none.
    It depends on the stored file (#2375)."""
    case = _case([_file(1, turn=8, stored=stored)], [])

    prompt = get_fallback_prompt_for_case(case, _MSG, given_tools=given)

    tag = _open_tag(prompt, "uploaded_file", file_id=_fid(1))
    assert (FILE_BACKED in tag) is stored, tag
    assert OLD_ATTRIBUTE not in prompt
    # What the turn CAN do with the file is the heading's job, and it still
    # tells the truth for every column: search_file is pointed only at a
    # stored file (#2375).
    if "search_file" in given and stored:
        assert "CURRENT-TURN UPLOAD (use search_file with the file_id):" in prompt
    elif "search_file" in given:
        assert "CURRENT-TURN UPLOAD (no stored file to search):" in prompt
        assert "use search_file" not in prompt
    else:
        assert "CURRENT-TURN UPLOAD (not searchable this turn):" in prompt


def test_the_fallback_heading_names_the_stored_uploads_when_some_are_not():
    """#2375: with a stored and an unstored upload, the heading sends
    search_file only to the file-backed one."""
    case = _case([_file(1, turn=8), _file(2, turn=8, stored=False)], [])

    prompt = get_fallback_prompt_for_case(case, _MSG, given_tools=DEFAULT_GIVEN)

    assert (
        f"CURRENT-TURN UPLOAD (use search_file with the file_id of a {FILE_BACKED} "
        "one):" in prompt
    )


@pytest.mark.parametrize("stored", [True, False], ids=["stored", "unstored"])
class TestSearchFileIsPointedOnlyAtAStoredFile:
    """#2375 review: a text that sends the model to ``search_file`` for a file
    with nothing stored is a dead end. An unstored file keeps its extract,
    and no text points ``search_file`` at it."""

    def test_directed_analysis_elides_only_a_stored_files_extract(self, stored):
        # A Tier A row and a historical (Tier D) orphan, in index-only mode.
        case = _case(
            [_file(1, stored=stored), _file(2, turn=1, stored=stored)],
            [_evidence(1, _fid(1))],
        )

        rendered = _build_evidence_context(
            case, processing_mode="directed_analysis", given_tools=DEFAULT_GIVEN
        )

        for i in (1, 2):
            assert (f"STRUCT-{i}" in rendered) is not stored, rendered
        assert ('elided="directed_analysis"' in rendered) is stored

    def test_a_truncated_extract_points_at_search_file_only_when_stored(self, stored):
        big = "ERROR STRUCT\n" * (EVIDENCE_CONTEXT_MAX_CHARS_PER_ITEM // 10)
        row_file = _file(1, stored=stored).model_copy(update={"structural_index": big})
        rendered = _build_evidence_context(
            _case([row_file], [_evidence(1, _fid(1))]), given_tools=DEFAULT_GIVEN
        )
        orphan = [
            _render_orphan_file_block(
                _file(2, stored=stored).model_copy(update={"structural_index": big}),
                {},
                1,
                summary_only=summary_only,
                fence=PromptFence(mint_token()),
                given=DEFAULT_GIVEN,
            )
            for summary_only in (False, True)
        ]

        for text in (rendered, *orphan):
            assert "[TRUNCATED:" in text or "omitted to fit budget" in text
            assert ("search_file" in text) is stored, text


class TestInstructionsKeyOnTheNewAttribute:
    """Every sentence that told the model what to do with the old attribute."""

    def test_the_inquiry_prompt_names_it_when_search_file_is_given(self):
        case = _case([_file(1, turn=1)], [], turn=1).model_copy(
            update={"state": CaseState.INQUIRY, "inquiry": InquiryData()}
        )

        with_search = get_prompt_for_case(case, _MSG, given_tools=DEFAULT_GIVEN)
        without = get_prompt_for_case(case, _MSG, given_tools=NO_TOOLS)

        assert f'<evidence id="ev_..." {FILE_BACKED}>' in with_search
        assert f'<evidence id="ev_..." {FILE_BACKED}>' not in without
        # An upload is searched by its file_id only when file-backed (#2375).
        assert f'<uploaded_file file_id="file_..." {FILE_BACKED}>' in with_search
        assert "Files are fully searchable" not in with_search
        assert OLD_ATTRIBUTE not in with_search + without

    def test_the_evidence_grounding_block_names_it_when_the_readers_are_given(self):
        case = _case([_file(1)], [_evidence(1, _fid(1))])

        with_readers = get_prompt_for_case(case, _MSG, given_tools=DEFAULT_GIVEN)
        without = get_prompt_for_case(case, _MSG, given_tools=NO_TOOLS)

        assert f"`{FILE_BACKED}` in the <evidence> blocks below" in with_readers
        assert "is NOT\nfile-backed" in with_readers
        assert "in the <evidence> blocks below" not in without
        assert OLD_ATTRIBUTE not in with_readers + without

    def test_the_directed_analysis_instruction_keys_on_it(self):
        text = _build_da_system_instruction(["search_file"], "Schema")

        assert f"rows marked {FILE_BACKED}" in text
        assert "SEARCHABLE EVIDENCE" not in text
        assert OLD_ATTRIBUTE not in text
        # Gated on the tool, as before.
        assert "file_backed" not in _build_da_system_instruction([], "Schema")


class TestToolErrorsKeyOnTheNewAttribute:
    """A tool refusing an engine inference points at the marker the prompt
    renders, so the two cannot name different things."""

    @staticmethod
    def _inference_case() -> Case:
        return _case(
            [_file(1)],
            [
                _evidence(1, _fid(1)),
                _evidence(
                    10, None, turn=2, source_type=EvidenceSourceType.ENGINE_INFERENCE
                ),
            ],
        )

    @staticmethod
    def _ctx(case: Case) -> ToolContext:
        return ToolContext(
            session_id="sess_1969",
            case_id=case.case_id,
            enterprise_id="org_1969",
            user_id="user_1969",
            in_memory_case=case,
            redaction_ctx=disabled_redaction(),
        )

    @pytest.mark.asyncio
    async def test_search_file(self):
        case = self._inference_case()
        tool = SearchFileTool(storage_service=None, context_lines=5, max_results=3)

        result = await tool.execute_with_context(
            {"evidence_id": _eid(10), "query": "ERROR"}, self._ctx(case)
        )

        assert not result.success
        assert FILE_BACKED in result.error, result.error
        assert "searchable" not in result.error.lower(), result.error
        assert f"{_eid(1)} (app1.log)" in result.error

    @pytest.mark.asyncio
    async def test_deep_analysis(self):
        case = self._inference_case()
        tool = DeepAnalysisTool(tier2_service=AsyncMock())

        result = await tool.execute_with_context(
            {"evidence_id": _eid(10), "query": "ERROR"}, self._ctx(case)
        )

        assert not result.success
        assert FILE_BACKED in result.error, result.error
        assert "searchable" not in result.error.lower(), result.error
        assert f"{_eid(1)} (app1.log)" in result.error
