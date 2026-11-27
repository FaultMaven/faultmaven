"""Each caller's hold of a worker answer is within its ``*_ANSWER_HOLD`` (#3122).

The container-memory status counts what a serving process holds while it reads
and parses a worker's answer as the job's output limit times the caller's
constant. Per caller, its worst answer is written by the worker's own writer
from steering text (a BMP character, the text, an escaped newline, then an
astral one, so the decode widens twice and a parse's string builder widens at
its over-allocated size) and sized to the output limit; it is read through the
real reader and parsed as the caller parses it, under ``tracemalloc``. The hold
is that peak less the same path's over an empty answer: above the constant
less one, at most the constant plus ``TRACE_SLACK_BYTES``, and more than the
output limit once, which is what the status counted before. Tier 1 and
preparation, which can receive a lone surrogate, are also read with one.
"""

from __future__ import annotations

import asyncio
import json
import tracemalloc
from types import SimpleNamespace
from typing import Any, Callable, List

import pytest

from faultmaven.infrastructure import embedding_guard, worker_process
from faultmaven.modules.agent.tools import search_file_tool, search_file_worker
from faultmaven.modules.knowledge.domain.services import (
    document_preprocessor,
    runbook_gate,
    validation_worker,
)
from faultmaven.modules.preprocessing import extraction_worker, preprocessing_service
from faultmaven.modules.preprocessing.extractors.protocol import ExtractResult

pytestmark = pytest.mark.unit

MIB = 1024 * 1024
BMP, ASTRAL = "€", "\U0001f600"

#: What the read and parse of an empty answer may peak at: the reader's
#: frames and objects, never the answer's.
EMPTY_PEAK_BYTES = 64 * 1024

#: Room above the constant for allocations not the reader's: ``tracemalloc``
#: counts every thread's (an xdist worker's IO thread), and its filters apply
#: to snapshots, never to the peak, so they cannot be filtered out of it.
TRACE_SLACK_BYTES = 64 * 1024


def _frame(frame: dict) -> bytes:
    """A frame as ``worker_process.serve``'s ``send`` writes it."""
    line = json.dumps(frame, ensure_ascii=False) + "\n"
    return line.encode("utf-8", "surrogatepass")


#: What a phased worker sent before its answer, counted against the limit.
_PHASES = sum(len(_frame({"phase": p})) for p in ("ready", "working", "done"))


def _phased(parts: List[str]) -> bytes:
    """A phased answer as ``serve`` writes ``result_parts``' parts."""
    raw = [p.encode("utf-8", "surrogatepass") for p in parts]
    return _frame({"result_bytes": [len(r) for r in raw]}) + b"".join([*raw, b"\n"])


def _peak(stream: bytes, limit: int, read, received: int, parse) -> int:
    """Traced peak while ``read`` takes ``stream`` and ``parse`` its answer."""

    async def go() -> int:
        tracing = tracemalloc.is_tracing()
        if not tracing:
            tracemalloc.start()
        try:
            base = tracemalloc.get_traced_memory()[0]
            tracemalloc.reset_peak()
            reader = asyncio.StreamReader(limit=limit)
            reader.feed_data(stream)
            reader.feed_eof()
            proc = SimpleNamespace(stdout=reader)
            got = await read(proc, {"received": received}, limit)
            parsed = parse(got)
            peak = tracemalloc.get_traced_memory()[1] - base
            del parsed
            return peak
        finally:
            if not tracing:
                tracemalloc.stop()

    return asyncio.run(go())


def _fit(stream: Callable[[int], bytes], room: int) -> bytes:
    """``stream(n)`` at the largest ``n`` within ``room`` bytes."""
    n = 0
    for _ in range(20):
        size = len(stream(n))
        step = (room - size) // (len(stream(n + 1)) - size)
        if step == 0:
            return stream(n)
        n += step
    raise AssertionError("the answer did not fit its limit")


def _steer(n: int) -> str:
    return BMP + "x" * n + "\n" + ASTRAL


def _steer_surrogate(n: int) -> str:
    """As ``_steer``, with a lone surrogate, which ``surrogatepass`` decodes
    by copying the input."""
    return BMP + "\ud800" + "x" * n + "\n" + ASTRAL


SHAPES = {"plain": _steer, "lone surrogate": _steer_surrogate}


def _reader(text: str) -> bytes:
    return _phased([text])


def _search(text: str) -> bytes:
    answer = ([{"line": 1, "content": text}], 1)
    return _phased(search_file_worker.answer_parts(answer))


def _gate(text: str) -> bytes:
    # ``_quality``'s refusal, its error echoing a value the user wrote.
    def run(name: str, part: str) -> Any:
        error = f"ID must be kebab-case (lowercase, hyphen-separated): {part}"
        return {"refused": {"errors": [error], "warnings": []}}

    answer = validation_worker.answers(
        run, [["enforce_runbook_quality", 0]], [text], 60
    )
    return _phased(validation_worker._parts(answer()))


def _tier1(text: str) -> bytes:
    result = ExtractResult(file_extract="f", search_map=text, file_meta={"lines": 1})
    return _phased(extraction_worker._parts(result))


def _preparation(text: str) -> bytes:
    result = {
        "extracted_text": text,
        "source_metadata": {"original_filename": "a.md", "word_count": 1},
        "redaction_report": {"redactions": [], "warning": None, "total_redacted": 0},
        "token_count": 1,
        "warnings": [],
        "triage_sample": "t",
    }
    return _frame({"result": result})


CALLERS = {
    # The token count's answer is two integers: its hold is the reader's.
    "token_count": (
        embedding_guard.TOKEN_COUNT_ANSWER_HOLD,
        embedding_guard.TOKEN_COUNT_OUTPUT_LIMIT_BYTES,
        _reader,
        worker_process._parts,
        _PHASES,
        lambda parts: parts,
    ),
    "search": (
        search_file_tool.SEARCH_ANSWER_HOLD,
        search_file_tool.search_output_limit(MIB, 1),
        _search,
        worker_process._parts,
        _PHASES,
        search_file_worker.read_answer,
    ),
    "gate": (
        runbook_gate.GATE_ANSWER_HOLD,
        runbook_gate.GATE_OUTPUT_LIMIT_BYTES,
        _gate,
        worker_process._parts,
        _PHASES,
        lambda parts: json.loads(parts[0]),
    ),
    "tier1": (
        preprocessing_service.TIER1_ANSWER_HOLD,
        preprocessing_service.tier1_limits(1)[2],
        _tier1,
        worker_process._parts,
        _PHASES,
        lambda parts: ExtractResult.from_json(parts[0]),
    ),
    "preparation": (
        document_preprocessor.PREPARATION_ANSWER_HOLD,
        document_preprocessor.PREPARATION_OUTPUT_LIMIT_BYTES,
        _preparation,
        worker_process._frames,
        0,
        lambda result: result,
    ),
}


@pytest.mark.parametrize(
    "caller, shape",
    [(caller, "plain") for caller in CALLERS]
    + [("tier1", "lone surrogate"), ("preparation", "lone surrogate")],
)
def test_the_worst_answer_at_the_limit_is_held_within_the_callers_constant(
    caller, shape
):
    hold, limit, writer, read, received, parse = CALLERS[caller]
    steer = SHAPES[shape]
    empty = _peak(writer(""), limit, read, received, parse)
    worst = _fit(lambda n: writer(steer(n)), limit - received)

    held = _peak(worst, limit, read, received, parse) - empty

    assert empty < EMPTY_PEAK_BYTES
    assert (hold - 1) * limit < held <= hold * limit + TRACE_SLACK_BYTES
    assert held > limit  # the status's term before #3122: the limit once


def test_the_token_count_and_search_hold_what_the_reader_holds():
    assert worker_process.PHASED_ANSWER_HOLD == 7
    assert embedding_guard.TOKEN_COUNT_ANSWER_HOLD == 7
    assert search_file_tool.SEARCH_ANSWER_HOLD == 7
