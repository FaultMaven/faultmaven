"""``deep_analysis``'s miss answers name every file it can read (#2009).

It kept a private copy of "what can this tool read in this case" that omitted
the uploads no evidence row records, which its own ``file_id`` fallback reads.
All three file tools now phrase their miss answer from
``enumerate_readable_files``. This is #2004's case matrix, run over
``deep_analysis``'s three miss branches (an inference row, a row whose upload
has no stored file, an unknown id).
"""

import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from faultmaven.modules.agent.tools.base import ToolContext
from faultmaven.modules.agent.tools.deep_analysis_tool import DeepAnalysisTool
from faultmaven.modules.agent.tools.search_file_tool import (
    enumerate_readable_files,
    format_searchable_alternatives,
)

pytestmark = pytest.mark.unit

NO_ROW = "file_00000000aaaa"  # stored, no evidence row
RECORDED = "file_00000000bbbb"  # stored, recorded by EV_STORED
UNSTORED = "file_00000000cccc"  # no stored file, recorded by EV_UNSTORED
EV_INFERENCE = "ev_inference"
EV_STORED = "ev_stored"
EV_UNSTORED = "ev_unstored"


def _upload(file_id, name, stored=True):
    return SimpleNamespace(
        file_id=file_id,
        filename=name,
        display_name=name,
        storage_ref=f"org/case/{name}" if stored else None,
        data_type=None,
    )


def _row(evidence_id, source_file_id=None):
    return SimpleNamespace(
        evidence_id=evidence_id, source_file_id=source_file_id, category=None
    )


def _case(uploads, rows):
    by_id = {u.file_id: u for u in uploads}
    return SimpleNamespace(
        case_id="case_aabb11223344",
        evidence=rows,
        uploaded_files=uploads,
        find_uploaded_file=lambda fid: by_id.get(fid),
    )


# case label -> (uploads, rows)
CASES = {
    "uploads_with_no_row_only": (
        [_upload(NO_ROW, "paste.txt")],
        [_row(EV_INFERENCE)],
    ),
    "recorded_row_with_stored_file": (
        [_upload(RECORDED, "app.log")],
        [_row(EV_INFERENCE), _row(EV_STORED, RECORDED)],
    ),
    "recorded_row_without_stored_file": (
        [_upload(UNSTORED, "gone.log", stored=False)],
        [_row(EV_INFERENCE), _row(EV_UNSTORED, UNSTORED)],
    ),
    "nothing_at_all": ([], [_row(EV_INFERENCE)]),
    "all_of_them": (
        [
            _upload(NO_ROW, "paste.txt"),
            _upload(RECORDED, "app.log"),
            _upload(UNSTORED, "gone.log", stored=False),
        ],
        [_row(EV_INFERENCE), _row(EV_STORED, RECORDED), _row(EV_UNSTORED, UNSTORED)],
    ),
}

# case label -> the ids a miss must name, written out so the matrix does not
# take its expectation from the enumerator it tests.
EXPECTED_NAMED = {
    "uploads_with_no_row_only": {NO_ROW},
    "recorded_row_with_stored_file": {EV_STORED},
    "recorded_row_without_stored_file": set(),
    "nothing_at_all": set(),
    "all_of_them": {NO_ROW, EV_STORED},
}

# branch label -> (the id the model passes, extra row to add)
BRANCHES = {
    "inference_row": EV_INFERENCE,
    "row_without_stored_file": EV_UNSTORED,
    "unknown_id": "file_dead00000001",
}


async def _miss(case, evidence_id):
    tool = DeepAnalysisTool(tier2_service=AsyncMock())
    ctx = ToolContext(
        session_id="s",
        case_id=case.case_id,
        enterprise_id="org",
        user_id="u",
        in_memory_case=case,
    )
    result = await tool.execute_with_context(
        {"evidence_id": evidence_id, "query": "q"}, ctx
    )
    assert not result.success
    return result.error


@pytest.mark.parametrize("branch", list(BRANCHES))
@pytest.mark.parametrize("label", list(CASES))
async def test_a_miss_names_exactly_the_files_the_tool_can_read(label, branch):
    uploads, rows = CASES[label]
    evidence_id = BRANCHES[branch]
    if branch == "row_without_stored_file" and not any(
        r.evidence_id == EV_UNSTORED for r in rows
    ):
        # The branch needs its row; add it with an upload nothing can read.
        uploads = [*uploads, _upload(UNSTORED, "gone.log", stored=False)]
        rows = [*rows, _row(EV_UNSTORED, UNSTORED)]
    case = _case(uploads, rows)

    error = await _miss(case, evidence_id)

    for expected in EXPECTED_NAMED[label]:
        assert expected in error, (label, branch, expected)
    readable = enumerate_readable_files(case.evidence, evidence_id, case)
    named_ids = {f.id for f in readable}
    # Every readable file is named, by the id the tool resolves.
    for f in readable:
        assert f"{f.id} ({f.filename})" in error, (label, branch)
    # Nothing unreadable is named.
    for unreadable in (UNSTORED, EV_UNSTORED, EV_INFERENCE):
        if unreadable not in named_ids and unreadable != evidence_id:
            assert unreadable not in error, (label, branch, unreadable)
    # The phrasing matches the kind: unrecorded uploads say how to pass them.
    unrecorded = [f for f in readable if not f.recorded]
    recorded = [f for f in readable if f.recorded]
    assert ("Uploaded files with no evidence row" in error) == bool(unrecorded)
    if unrecorded:
        assert "analyze them by passing the file_id as evidence_id" in error
    assert ("Available file-backed evidence in this case" in error) == bool(recorded)
    # deep_analysis names only itself: never another tool.
    assert "search_file" not in error and "case_evidence_search" not in error


@pytest.mark.parametrize("label", list(CASES))
async def test_the_list_deep_analysis_names_is_the_one_search_file_names(label):
    uploads, rows = CASES[label]
    case = _case(uploads, rows)
    error = await _miss(case, "file_dead00000001")

    search_file_hint = format_searchable_alternatives(
        case.evidence, "file_dead00000001", case
    )
    pairs = re.findall(r"((?:ev|file)_\w+) \(([^)]*)\)", search_file_hint)
    assert bool(pairs) == (
        label != "nothing_at_all" and label != "recorded_row_without_stored_file"
    )
    # Same files, whichever wording: every id search_file's hint names is named here.
    for file_id, filename in pairs:
        assert f"{file_id} ({filename})" in error


async def test_an_upload_with_no_row_is_named_and_then_analyzable():
    """The #2009 case end to end: what the miss names, the tool reads."""
    uploads, rows = CASES["uploads_with_no_row_only"]
    case = _case(uploads, rows)
    tier2 = AsyncMock()
    tier2.analyze.return_value = SimpleNamespace(
        answer="a", excerpts=[], confidence=0.9, backend_used="t"
    )
    miss = await _miss(case, EV_INFERENCE)
    assert f"{NO_ROW} (paste.txt)" in miss

    tool = DeepAnalysisTool(tier2_service=tier2)
    ctx = ToolContext(
        session_id="s",
        case_id=case.case_id,
        enterprise_id="org",
        user_id="u",
        in_memory_case=case,
    )
    result = await tool.execute_with_context({"evidence_id": NO_ROW, "query": "q"}, ctx)
    assert result.success, result.error
