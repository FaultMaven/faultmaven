"""``_protected_spans`` walks a document's lines without holding them (#2622).

It read ``text.splitlines(keepends=True)`` and a list of every line's offset:
on a document of two-character lines that held about 64 bytes for each
character, and the code-span scan runs on every upload whose frontmatter looks
like a runbook's (Stage 1b) and every Markdown file with ``<!--``. So a plain
text file could exhaust the preparation worker's memory limit, where the ruling
says only a PDF, DOCX or HTML file can. It now walks the line boundaries
``str.splitlines`` uses, one at a time, and holds only the spans.

``_reference`` is the algorithm it replaced, kept here to compare outputs:
every input must give the same spans.
"""

from __future__ import annotations

import random
import tracemalloc

import pytest

from faultmaven.modules.knowledge.domain.services import runbook_grammar as g

pytestmark = [pytest.mark.unit, pytest.mark.knowledge_base]


def _reference(text: str) -> list[tuple[int, int]]:
    """``_protected_spans`` as it was at 037c7f90c."""
    spans: list[tuple[int, int]] = []
    lines = text.splitlines(keepends=True)
    offsets: list[int] = []
    running = 0
    for line in lines:
        offsets.append(running)
        running += len(line)

    i = 0
    while i < len(lines):
        opener = g.CODE_FENCE_LINE_RE.match(lines[i])
        if not opener:
            i += 1
            continue
        marker, width = opener.group(1)[0], len(opener.group(1))
        j = i + 1
        while j < len(lines):
            closer = g.CODE_FENCE_LINE_RE.match(lines[j])
            if (
                closer
                and closer.group(1)[0] == marker
                and len(closer.group(1)) >= width
            ):
                break
            j += 1
        end = offsets[j] + len(lines[j]) if j < len(lines) else len(text)
        spans.append((offsets[i], end))
        i = j + 1

    def in_fence(pos: int) -> bool:
        return any(lo <= pos < hi for lo, hi in spans)

    for m in g.CODE_SPAN_RE.finditer(text):
        if not in_fence(m.start()):
            spans.append(m.span())
    return spans


#: Every line boundary ``str.splitlines`` knows.
_BOUNDARIES = [
    "\n",
    "\r\n",
    "\r",
    "\v",
    "\f",
    "\x1c",
    "\x1d",
    "\x1e",
    "\x85",
    " ",
    " ",
]

_CORPUS = [
    "",
    "no code at all",
    "```\nfenced\n```\nafter",
    "~~~\ntilde fenced\n~~~\n",
    "````\nouter\n```\nnot a closer: too short\n````\nafter `inline`",
    "```\nan unclosed fence runs to the end\n<!-- not a comment -->",
    "~~~\na tilde fence ignores a backtick closer\n```\n~~~",
    "   ```\nindented three\n   ```",
    "    ```\nindented four is not a fence\n    ```",
    "```py\nwith an info string\n```",
    "text `a` and ``b`c`` and `unclosed",
    "<!-- a comment --> `<!--` quoted --> after",
    "## Causes\n<!--\n```\n-->\n```\n",
    "---\nid: a\ndomain: b\nservice: c\nsymptom_class: d\n---\n\xe9\n\xe9\n",
    *(f"x{b}```{b}code `y`{b}```{b}z{b}`w`" for b in _BOUNDARIES),
    "a\r\n```\r\nwindows\r\n```\r\n",
    "a\r```\rold mac\r```\r",
    "```\x85fence after NEL\x85```\x85",
    "``` line separator ```",
    "a\x1c```\x1cfile separator\x1c```",
    "\r\r\n\n\x85``` `a` ```",
]


@pytest.mark.parametrize("text", _CORPUS)
def test_the_spans_are_the_reference_spans(text):
    assert g._protected_spans(text) == _reference(text)


def test_the_spans_are_the_reference_spans_on_random_documents():
    pieces = [
        "```",
        "````",
        "~~~",
        "~~~~",
        "`",
        "``",
        " ",
        "   ",
        "    ",
        "\t",
        "<!--",
        "-->",
        "a",
        "\xe9",
        "```sh",
        "x`y`z",
        *_BOUNDARIES,
    ]
    rng = random.Random(2622)
    for _ in range(5000):
        text = "".join(rng.choice(pieces) for _ in range(rng.randint(0, 60)))
        assert g._protected_spans(text) == _reference(text), repr(text)


def test_no_line_is_held():
    """2 MB of two-character lines, the shape that cost the most: the walk
    allocates next to nothing, where the line list held about 128 MB."""
    text = "\xe9\n" * 1_000_000
    tracemalloc.start()
    try:
        spans = g._protected_spans(text)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert spans == []
    assert peak < 1024 * 1024, f"{peak:,} bytes held while walking the lines"
