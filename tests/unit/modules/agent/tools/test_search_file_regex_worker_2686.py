"""``search_file``'s regex runs in a Principle 14 worker per search (#2686, #3116 A).

The model writes the pattern, so it never runs in the API process. Pin 1: a
catastrophic pattern over a hostile line does not hold the event loop; the
search stops at its declared work bound and a heartbeat shows no gap past it.
The answers on a normal file are ``main``'s, captured at ``bc2beae55`` through
``execute_with_context`` (``search_file_oracle_bc2beae55.json``), never typed.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.milestone_engine.vectorization import (
    EvidenceVectorizer,
)
from faultmaven.infrastructure import worker_process
from faultmaven.modules.agent.tools import search_file_tool
from faultmaven.modules.agent.tools.base import ToolContext
from faultmaven.modules.agent.tools.search_file_tool import SearchFileTool
from faultmaven.modules.agent.tools.search_file_worker import regex_excerpts
from faultmaven.modules.case.contracts import UploadedFile
from tests.model_content import disabled_redaction

pytestmark = pytest.mark.unit

_ORACLE = json.loads(
    (Path(__file__).parent / "search_file_oracle_bc2beae55.json").read_text()
)

#: The issue's pattern: nested quantifiers, exponential over ``a…a!``.
_CATASTROPHIC = "(a+)+$"

#: A bound the test can wait for; the production bound is 10 s.
_TEST_WORK_SECONDS = 1.0


def _context(user_id: str = "user_1") -> ToolContext:
    ev = MagicMock()
    ev.evidence_id = "ev_abc"
    ev.source_file_id = "file_0123456789ab"
    upload = UploadedFile(
        file_id="file_0123456789ab",
        filename="app.log",
        size_bytes=1,
        uploaded_at_turn=1,
        storage_ref="evidence/case_123/app.log",
        upload_source="file_upload",
    )
    case = MagicMock()
    case.case_id = "case_123"
    case.evidence = [ev]
    case.uploaded_files = [upload]
    case.find_uploaded_file.side_effect = lambda fid: (
        upload if fid == upload.file_id else None
    )
    return ToolContext(
        session_id="sess_1",
        case_id="case_123",
        enterprise_id="org_1",
        user_id=user_id,
        in_memory_case=case,
        redaction_ctx=disabled_redaction(),
    )


def _tool(content: str) -> SearchFileTool:
    storage = AsyncMock()
    storage.retrieve_file = AsyncMock(return_value=content.encode())
    return SearchFileTool(storage_service=storage)


def _hostile_file(n: int) -> str:
    return "INFO started\n" + "a" * n + "!\nINFO done\n"


@pytest.fixture
def test_sized_bound(monkeypatch):
    phases = search_file_tool.SEARCH_PHASES
    monkeypatch.setattr(
        search_file_tool,
        "SEARCH_PHASES",
        worker_process.Phases(
            start=phases.start,
            request=phases.request,
            work=_TEST_WORK_SECONDS,
            result=phases.result,
        ),
    )


async def _all_slots_free() -> bool:
    """Every slot can be taken at once, each by a principal of its own."""
    slots = search_file_tool.worker_process.loop_slots(
        search_file_tool._search_slots_state, search_file_tool.SEARCH_SLOTS
    )[1]
    taken = []
    try:
        for n in range(search_file_tool.SEARCH_SLOTS):
            async with asyncio.timeout(0.5):
                await slots.acquire(f"probe-{n}")
            taken.append(f"probe-{n}")
        return True
    except TimeoutError:
        return False
    finally:
        for key in taken:
            slots.release(key)


# -- the answers on a normal file are main's ----------------------------------


@pytest.mark.parametrize(
    "case", _ORACLE["cases"], ids=lambda c: json.dumps(c["params"], sort_keys=True)
)
async def test_a_normal_file_answers_as_main_did(case):
    result = await _tool(_ORACLE["content"]).execute_with_context(
        params=case["params"], context=_context()
    )

    assert {
        "success": result.success,
        "data": result.data,
        "error": result.error,
        "timed_out": result.timed_out,
    } == case["expected"]


def test_the_oracle_covers_every_shape_it_pins():
    params = [c["params"] for c in _ORACLE["cases"]]
    shapes = {
        (p.get("search_type", "keyword"), p.get("output_format", "excerpts"))
        for p in params
    }
    assert shapes == {
        ("keyword", "excerpts"),
        ("keyword", "count"),
        ("regex", "excerpts"),
        ("regex", "count"),
    }
    expected = [c["expected"]["data"] for c in _ORACLE["cases"]]
    assert any(
        d.get("truncated") for d in expected if d.get("output_format") == "count"
    )
    assert any(
        d.get("truncated") for d in expected if d.get("output_format") == "excerpts"
    )
    assert any("Invalid regex" in json.dumps(d) for d in expected)
    assert _ORACLE["captured_at"].startswith("bc2beae55")


# -- pin 1: the loop is never held, and the search stops at its bound ---------


async def _ticks_during(call) -> tuple[int, object]:
    """How many times the event loop ran while ``call()`` ran, and its result.

    A count, as ``test_gate_stays_off_the_event_loop.py`` takes it: a call
    that holds the loop gives it no turn at all, however fast the machine;
    one that waits on a worker gives it turns, fewer on a loaded box but never
    none."""
    ticks: list[float] = []
    stop = asyncio.Event()

    async def heartbeat() -> None:
        # Tick after the stop is set too, so a gap spanning the call is seen.
        while True:
            ticks.append(time.perf_counter())
            if stop.is_set():
                return
            await asyncio.sleep(0.005)

    beat = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.02)
    started = time.perf_counter()
    try:
        result = await call()
    finally:
        finished = time.perf_counter()
        stop.set()
        await beat
    return sum(1 for t in ticks if started < t < finished), result


@pytest.mark.parametrize("output_format", ["excerpts", "count"])
async def test_a_catastrophic_pattern_stops_at_its_bound_off_the_loop(
    test_sized_bound, output_format
):
    """The inline search is the positive control: ``main``'s search on the
    loop, at a size it finishes (each two more ``a`` cost it about 4x). The
    worker gets 40, which no search finishes, and the test-sized work bound:
    the loop runs throughout, and the search ends at the bound."""

    async def inline():
        return regex_excerpts(_hostile_file(22), _CATASTROPHIC, 3)

    async def in_worker():
        return await _tool(_hostile_file(40)).execute_with_context(
            params={
                "evidence_id": "ev_abc",
                "query": _CATASTROPHIC,
                "search_type": "regex",
                "output_format": output_format,
            },
            context=_context(),
        )

    inline_ticks, _ = await _ticks_during(inline)
    worker_ticks, result = await _ticks_during(in_worker)

    assert inline_ticks == 0, "positive control: the loop ran during the inline search"
    assert worker_ticks >= 3, "the loop did not run while the worker searched"
    assert result.success is False
    assert result.error == (
        "The pattern did not finish within 1 seconds over this file; use a "
        "simpler pattern (no nested quantifiers) or a keyword search."
    )
    assert await _all_slots_free()


async def test_the_bounded_time_error_is_no_timeout_so_nothing_is_vectorized(
    test_sized_bound,
):
    context = _context()
    result = await _tool(_hostile_file(40)).execute_with_context(
        params={
            "evidence_id": "ev_abc",
            "query": _CATASTROPHIC,
            "search_type": "regex",
        },
        context=context,
    )
    assert result.success is False and result.timed_out is False

    vectorizer = EvidenceVectorizer(deps=MagicMock())
    vectorizer._reactive_vectorize = AsyncMock(return_value="vectorized")
    vectorizer._evidence_is_vectorized = MagicMock(return_value=False)
    await vectorizer.track_da_result(
        func_name="search_file",
        evidence_id="ev_abc",
        tool_result=result,
        result_text="before",
        case=context.in_memory_case,
        tool_context=context,
        da_empty_search_counts={},
        proactive_tasks={},
        given_tools=frozenset(),
    )

    vectorizer._reactive_vectorize.assert_not_awaited()


async def test_no_pattern_runs_in_this_process(monkeypatch):
    """The pattern reaches ``re`` only in the worker: here, ``re.compile`` of
    it raises, and the search still answers."""
    real_compile = re.compile
    pattern = r"db-pool\s+timeout"

    def refuse(p, *args, **kwargs):
        assert p != pattern, "the model's pattern was compiled in the API process"
        return real_compile(p, *args, **kwargs)

    monkeypatch.setattr(re, "compile", refuse)
    result = await _tool(_ORACLE["content"]).execute_with_context(
        params={"evidence_id": "ev_abc", "query": pattern, "search_type": "regex"},
        context=_context(),
    )

    assert result.success is True and result.data["total_matches"] == 8


# -- every other end is an error the model reads, and frees the slot ----------


def _regex_call(content: str = "INFO a\nERROR b\n", query: str = "ERROR"):
    return _tool(content).execute_with_context(
        params={"evidence_id": "ev_abc", "query": query, "search_type": "regex"},
        context=_context(),
    )


async def test_a_file_too_dense_for_its_memory_limit_is_too_large():
    # 1 MiB of one-letter lines, every line matched: measured at 263 MiB, past
    # the 112 MiB this file's limit gives.
    result = await _regex_call("a\n" * (512 * 1024), "a")

    assert result.success is False and result.timed_out is False
    assert result.error == (
        "The pattern matched more of this file than one search can hold; use a "
        "narrower pattern or a keyword search."
    )
    assert await _all_slots_free()


async def test_an_answer_past_the_output_limit_is_too_large(monkeypatch):
    monkeypatch.setattr(search_file_tool, "search_output_limit", lambda c, n: 64)

    result = await _regex_call()

    assert result.success is False and "than one search can hold" in result.error
    assert await _all_slots_free()


async def test_a_worker_that_does_not_start_is_unavailable(monkeypatch):
    monkeypatch.setattr(search_file_tool, "_SEARCH_WORKER", "faultmaven.no_such_worker")

    result = await _regex_call()

    assert result.success is False and result.timed_out is False
    assert (
        result.error == "Regex search is unavailable right now; use a keyword search."
    )
    assert await _all_slots_free()


async def test_no_free_slot_within_the_wait_is_busy(monkeypatch):
    monkeypatch.setattr(search_file_tool, "SEARCH_SLOT_WAIT_SECONDS", 0.05)
    slots = worker_process.loop_slots(
        search_file_tool._search_slots_state, search_file_tool.SEARCH_SLOTS
    )
    monkeypatch.setattr(search_file_tool, "_search_slots_state", slots)
    held = [f"other-{n}" for n in range(search_file_tool.SEARCH_SLOTS)]
    for key in held:
        await slots[1].acquire(key)
    try:
        result = await _regex_call()
    finally:
        for key in held:
            slots[1].release(key)

    assert result.success is False and result.timed_out is False
    assert result.error == (
        "Every regex search slot is busy; retry the search shortly, or use a "
        "keyword search."
    )
    assert await _all_slots_free()


async def test_a_cancelled_search_frees_its_slot_and_its_worker(test_sized_bound):
    search = asyncio.create_task(
        _tool(_hostile_file(40)).execute_with_context(
            params={
                "evidence_id": "ev_abc",
                "query": _CATASTROPHIC,
                "search_type": "regex",
            },
            context=_context(),
        )
    )
    await asyncio.sleep(0.3)
    search.cancel()
    with pytest.raises(asyncio.CancelledError):
        await search

    assert await _all_slots_free()


async def test_a_search_is_admitted_as_the_user_who_asked(monkeypatch):
    """The slot is keyed by ``context.user_id`` (#2645): with one of two slots
    held by ``user_1``, ``user_1`` waits for two free and is told busy, while
    ``user_2`` takes the free one."""
    monkeypatch.setattr(search_file_tool, "SEARCH_SLOT_WAIT_SECONDS", 0.05)
    slots = worker_process.loop_slots(None, 2)
    monkeypatch.setattr(search_file_tool, "_search_slots_state", slots)
    monkeypatch.setattr(search_file_tool, "SEARCH_SLOTS", 2)
    await slots[1].acquire("user_1")
    try:
        tool = _tool("INFO a\nERROR b\n")
        params = {"evidence_id": "ev_abc", "query": "ERROR", "search_type": "regex"}
        same = await tool.execute_with_context(params=params, context=_context())
        other = await tool.execute_with_context(
            params=params, context=_context("user_2")
        )
    finally:
        slots[1].release("user_1")

    assert same.success is False and "busy" in same.error
    assert other.success is True and other.data["total_matches"] == 1
