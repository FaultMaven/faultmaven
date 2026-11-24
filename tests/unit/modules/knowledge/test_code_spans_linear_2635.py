"""``code_spans`` decides fence membership without scanning every span (#2635).

It checked each inline match against every span found so far, inline ones
included, so a document of dense inline code took quadratic time: 5k, 10k and
20k `` `a` `` spans took 1.6 s, 5.8 s and 20.8 s. Only the fences can hold an
inline match (``finditer`` matches never overlap), and they are sorted and
disjoint, so one bisect on their starts answers it.

``code_spans_oracle_2635.json`` is what ``code_spans`` returned on
``_corpus()`` with the product code of 77f3b9b8b, before the change: written by
``python -m tests.unit.modules.knowledge.test_code_spans_linear_2635``.
"""

from __future__ import annotations

import json
import zlib
from functools import cache
from pathlib import Path

import pytest

from faultmaven.modules.knowledge.domain.services import runbook_grammar as g
from tests.unit.modules.knowledge.test_protected_spans_walk_lines_lazily_2622 import (
    _CORPUS,
)

pytestmark = [pytest.mark.unit, pytest.mark.knowledge_base]

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[3]
ORACLE = HERE / "fixtures" / "code_spans_oracle_2635.json"
_CODE_SPAN_RE = g.CODE_SPAN_RE
FILE_SOURCES = [REPO_ROOT / "resources/knowledge/pack/runbooks", HERE / "fixtures"]


@cache
def _corpus() -> dict[str, str]:
    """Synthetic texts by name, then every shipped runbook and grammar fixture
    by repo path (these may change, and are compared only while unchanged)."""
    texts = {f"2622-corpus-{i}": text for i, text in enumerate(_CORPUS)}
    texts |= {
        "around-fences": "`a` before\n```\n`b` inside\n```\n`c` after `d`",
        "unclosed-fence": "`a`\n```\n`b` never closed `c`\n",
        "tilde-not-closed-by-backticks": "~~~\n```\n`x`\n~~~\n`y`",
        "short-closer-ignored": "````\n```\n`x`\n````\n`y`",
        "long-closer-closes": "```\n`x`\n``````\n`y`",
        "crlf": "`a`\r\n```\r\n`b`\r\n```\r\n`c`\r\n~~~\r\n`d`\r\n~~~\r\n",
        "dense-5k": "`a` " * 5000,
        "dense-between-fences": ("`a` " * 100 + "\n```\n`b` `c`\n```\n") * 50,
    }
    for root in FILE_SOURCES:
        for path in sorted(root.rglob("*.md")):
            texts[path.relative_to(REPO_ROOT).as_posix()] = path.read_text("utf-8")
    return texts


def _crc(text: str) -> int:
    """A number, not a hex string, so the secret scanner reads no key."""
    return zlib.crc32(text.encode("utf-8"))


def _write_oracle() -> None:
    oracle = {
        name: {"crc32": _crc(text), "spans": g.code_spans(text)}
        for name, text in _corpus().items()
    }
    ORACLE.write_text(json.dumps(oracle, separators=(",", ":")) + "\n", "utf-8")


@cache
def _oracle() -> dict:
    return json.loads(ORACLE.read_text("utf-8"))


@pytest.mark.parametrize("name", list(_corpus()))
def test_the_spans_are_mains_spans(name):
    text, recorded = _corpus()[name], _oracle().get(name)
    if "/" in name and (recorded is None or _crc(text) != recorded["crc32"]):
        pytest.skip(f"{name} was added or changed after the oracle was recorded")
    assert _crc(text) == recorded["crc32"], f"synthetic input {name} changed"
    assert g.code_spans(text) == [tuple(span) for span in recorded["spans"]]


def test_every_recorded_text_is_still_compared():
    """A rename in the corpus must not retire part of the oracle quietly."""
    synthetic = {name for name in _oracle() if "/" not in name}
    assert synthetic <= set(_corpus())


def _counting(op: str):
    def compare(self, other):
        _Counted.count += 1
        return getattr(int(self), op)(other)

    return compare


class _Counted(int):
    """A match offset that counts every comparison made against it."""

    count = 0
    __lt__, __le__, __gt__, __ge__ = map(
        _counting, ["__lt__", "__le__", "__gt__", "__ge__"]
    )


class _CountingMatch:
    def __init__(self, match):
        self._match = match

    def start(self):
        return _Counted(self._match.start())

    def end(self):
        return _Counted(self._match.end())

    def span(self):
        return self.start(), self.end()


class _CountingSpanRe:
    def finditer(self, text):
        return map(_CountingMatch, _CODE_SPAN_RE.finditer(text))


def _comparisons(monkeypatch, inline_spans: int) -> int:
    fences = "".join(f"`x` `y`\n```\n`in` {i}\n```\n" for i in range(64))
    text = fences + "`a` " * inline_spans
    with monkeypatch.context() as patch:
        patch.setattr(g, "CODE_SPAN_RE", _CountingSpanRe())
        _Counted.count = 0
        g.code_spans(text)
    return _Counted.count


def test_doubling_the_inline_spans_at_most_doubles_the_fence_checks(monkeypatch):
    once = _comparisons(monkeypatch, 2000)
    twice = _comparisons(monkeypatch, 4000)
    assert once > 2000, "the positive control: every inline span is checked"
    assert twice <= 2 * once, (once, twice)


if __name__ == "__main__":
    _write_oracle()
