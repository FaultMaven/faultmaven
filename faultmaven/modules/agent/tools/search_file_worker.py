"""``search_file``'s regex search, a phased job of ``worker_process`` run as
``python -m`` by ``SearchFileTool`` (Principle 14, #2686).

The model writes the pattern, so it never runs in the API process: each search
runs here, killed at its bounds. The request names the pattern, the format,
the context lines and the result cap; the file is its one raw part. The answer
is a JSON head (the total, and each result with its text field ``None``) and
each result's text raw, so the output is bounded by the file's text and its
line-number prefixes. It imports ``re``, ``json`` and ``worker_process`` only.
"""

import json
import re
import sys
from typing import Any, Callable, Dict, List, Tuple

from faultmaven.infrastructure import worker_process

#: A result's text field, sent raw: an excerpt, or a matched line.
_TEXT_FIELDS = ("excerpt", "content")


def merge_overlapping(matches: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Merge overlapping excerpt windows."""
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
                prev["excerpt"] = current["excerpt"]  # Use the wider window
                prev["relevance"] = max(
                    prev.get("relevance", 0), current.get("relevance", 0)
                )
        else:
            merged.append(current)

    return merged


def regex_excerpts(
    content: str, pattern: str, context_lines: int
) -> List[Dict[str, Any]]:
    """Regex search: compile pattern, find all matches, return with context."""
    try:
        regex = re.compile(pattern, re.IGNORECASE | re.MULTILINE)
    except re.error as e:
        # The text describes the model's own pattern, not our system.
        return [{"error": f"Invalid regex pattern: {e}"}]

    lines = content.split("\n")
    context_lines = min(context_lines, 10)  # Smaller context for regex
    matches: List[Dict[str, Any]] = []

    for i, line in enumerate(lines):
        if regex.search(line):
            start = max(0, i - context_lines)
            end = min(len(lines), i + context_lines + 1)
            matches.append(
                {
                    "excerpt": "\n".join(
                        f"{j + 1}: {lines[j]}" for j in range(start, end)
                    ),
                    "line_start": start + 1,
                    "line_end": end,
                    "match_line": i + 1,
                }
            )

    return merge_overlapping(matches)


def regex_lines(content: str, pattern: str) -> List[Dict[str, Any]]:
    """Compact search: the matched lines, as ``{"line": N, "content": "..."}``."""
    try:
        regex = re.compile(pattern, re.IGNORECASE | re.MULTILINE)
    except re.error as e:
        # The text describes the model's own pattern, not our system.
        return [{"line": 0, "content": f"Invalid regex: {e}"}]
    return [
        {"line": i + 1, "content": line}
        for i, line in enumerate(content.split("\n"))
        if regex.search(line)
    ]


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
        if request["format"] == "count":
            found = regex_lines(content, pattern)
        else:
            found = regex_excerpts(content, pattern, request["context_lines"])
        return found[:cap], len(found)

    return work


if __name__ == "__main__":
    sys.exit(worker_process.serve(_search, "regex search", result_parts=answer_parts))
