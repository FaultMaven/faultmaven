"""``search_file``'s regex search, a phased job of ``worker_process`` run as
``python -m`` by ``SearchFileTool`` (Principle 14, #2686).

The model writes the pattern, so it never runs in the API process: each search
runs here, under a CPU-time deadline (``ITIMER_PROF``, whose ``SIGPROF`` ends
the process, inside C code too). The request names the pattern, the format,
the context lines and the result cap (at least 1); the file is its one raw
part. The search reads the file a line at a time and keeps only the windows it
returns, so its memory is the file's text and those windows. The answer is a
JSON head (the total, and each result with its text field ``None``) and each
result's text raw, so the output is bounded by the file's text and its
line-number prefixes. It imports ``io``, ``json``, ``re``, ``signal`` and
``worker_process`` only.
"""

import io
import json
import re
import signal
import sys
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from faultmaven.infrastructure import worker_process

#: A result's text field, sent raw: an excerpt, or a matched line.
_TEXT_FIELDS = ("excerpt", "content")

#: What ``re.compile`` raises for a pattern it cannot build: the model's own
#: pattern at fault, answered as invalid (a group nested past the parser's
#: recursion, a repeat count past its range).
PATTERN_ERRORS = (re.error, OverflowError, RecursionError)


def excerpt(lines: List[str], line_start: int, line_end: int) -> str:
    """Lines ``line_start``..``line_end`` (1-based, inclusive), numbered."""
    return "\n".join(f"{j}: {lines[j - 1]}" for j in range(line_start, line_end + 1))


def merge_overlapping(
    matches: List[Dict[str, Any]], lines: List[str]
) -> List[Dict[str, Any]]:
    """Merge overlapping excerpt windows; a merged window's excerpt is its own
    ``line_start``..``line_end``. The regex search merges by the same rule."""
    if not matches:
        return []

    sorted_matches = sorted(matches, key=lambda m: m.get("line_start", 0))
    merged: List[Dict[str, Any]] = [sorted_matches[0]]

    for current in sorted_matches[1:]:
        prev = merged[-1]
        if current.get("line_start", 0) <= prev.get("line_end", 0):
            # Overlapping — extend
            if current.get("line_end", 0) > prev.get("line_end", 0):
                prev["line_end"] = current["line_end"]
                prev["excerpt"] = excerpt(lines, prev["line_start"], prev["line_end"])
                prev["relevance"] = max(
                    prev.get("relevance", 0), current.get("relevance", 0)
                )
        else:
            merged.append(current)

    return merged


def _lines(content: str) -> Iterator[str]:
    """``content.split("\\n")``, one line at a time."""
    start = 0
    while (end := content.find("\n", start)) >= 0:
        yield content[start:end]
        start = end + 1
    yield content[start:]


def _compile(pattern: str) -> "re.Pattern[str] | str":
    try:
        return re.compile(pattern, re.IGNORECASE | re.MULTILINE)
    except PATTERN_ERRORS as e:
        # The text describes the model's own pattern, not our system.
        return str(e)


def regex_excerpts(
    content: str, pattern: str, context_lines: int, cap: int
) -> Tuple[List[Dict[str, Any]], int]:
    """The first ``cap`` merged windows around the lines the pattern matches,
    with context, and how many there are."""
    regex = _compile(pattern)
    if isinstance(regex, str):
        return [{"error": f"Invalid regex pattern: {regex}"}], 1

    context_lines = min(context_lines, 10)  # Smaller context for regex
    count = content.count("\n") + 1
    kept: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    total = 0
    for i, line in enumerate(_lines(content)):
        if not regex.search(line):
            continue
        start, end = max(0, i - context_lines), min(count, i + context_lines + 1)
        if current is not None and start + 1 <= current["line_end"]:
            if end > current["line_end"]:
                current["line_end"] = end
                current["relevance"] = 0  # as the dict merge marks an extension
            continue
        total += 1
        current = {
            "excerpt": None,
            "line_start": start + 1,
            "line_end": end,
            "match_line": i + 1,
        }
        if len(kept) < cap:
            kept.append(current)
    _fill_excerpts(content, kept)
    return kept, total


def _fill_excerpts(content: str, windows: List[Dict[str, Any]]) -> None:
    """Each window's excerpt, written in one more pass over the lines into a
    buffer that holds only its text."""
    at, text = 0, io.StringIO()
    for i, line in enumerate(_lines(content), start=1):
        if at == len(windows):
            return
        if i >= windows[at]["line_start"]:
            text.write(f"{i}: {line}\n")
        if i == windows[at]["line_end"]:
            windows[at]["excerpt"] = text.getvalue()[:-1]
            at, text = at + 1, io.StringIO()


def regex_lines(
    content: str, pattern: str, cap: int
) -> Tuple[List[Dict[str, Any]], int]:
    """The first ``cap`` matched lines, as ``{"line": N, "content": "..."}``,
    and how many lines match."""
    regex = _compile(pattern)
    if isinstance(regex, str):
        return [{"line": 0, "content": f"Invalid regex: {regex}"}], 1
    kept: List[Dict[str, Any]] = []
    total = 0
    for i, line in enumerate(_lines(content)):
        if regex.search(line):
            total += 1
            if len(kept) < cap:
                kept.append({"line": i + 1, "content": line})
    return kept, total


def answer_parts(answer: Tuple[List[Dict[str, Any]], int]) -> List[str]:
    """The capped results and the total, as the head and each text raw."""
    results, total = answer
    head, texts = [], []
    for result in results:
        result = dict(result)
        for field in _TEXT_FIELDS:
            if field in result:
                texts.append(result[field])
                result[field] = None
        head.append(result)
    return [json.dumps({"total": total, "results": head}), *texts]


def read_answer(parts: List[str]) -> Tuple[List[Dict[str, Any]], int]:
    """``answer_parts``' inverse, each text back in its field's place."""
    head = json.loads(parts[0])
    texts = iter(parts[1:])
    results = head["results"]
    for result in results:
        for field in _TEXT_FIELDS:
            if field in result:
                result[field] = next(texts)
    return results, head["total"]


def _search(request: Dict[str, Any], send) -> Callable[[], Any]:
    content: str = request["parts"][0]
    pattern, cap = request["pattern"], request["max_results"]

    def work() -> Tuple[List[Dict[str, Any]], int]:
        signal.setitimer(signal.ITIMER_PROF, request["work_cpu_seconds"])
        try:
            if request["format"] == "count":
                return regex_lines(content, pattern, cap)
            return regex_excerpts(content, pattern, request["context_lines"], cap)
        finally:
            signal.setitimer(signal.ITIMER_PROF, 0)

    return work


if __name__ == "__main__":
    sys.exit(worker_process.serve(_search, "regex search", result_parts=answer_parts))
