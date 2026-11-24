"""Code-span membership is linear in the document's code (#2635).

``_protected_spans`` checked each inline match against every span found so far,
and ``comment_spans`` and Stage 1b's body check scanned every code span for each
offset they asked about, so dense code took quadratic time: 5k, 10k and 20k
`` `a` `` spans took 1.6 s, 5.8 s and 20.8 s. All three now ask
``offset_in_spans``, which walks sorted runs with a forward pointer.

``code_spans_oracle_2635.json`` is what ``code_spans``, ``comment_spans``,
``mask_html_comments`` and ``_has_runbook_body`` returned on ``_corpus()`` and
``_random_texts()`` with the product code of 77f3b9b8b, before the change:
written by ``python -m tests.unit.modules.knowledge.test_code_spans_linear_2635``.
"""

from __future__ import annotations

import json
import random
import zlib
from functools import cache
from pathlib import Path

import pytest

from faultmaven.modules.knowledge.domain.services import preparation as p
from faultmaven.modules.knowledge.domain.services import runbook_grammar as g
from tests.unit.modules.knowledge.test_protected_spans_walk_lines_lazily_2622 import (
    _BOUNDARIES,
    _CORPUS,
)

pytestmark = [pytest.mark.unit, pytest.mark.knowledge_base]

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[3]
ORACLE = HERE / "fixtures" / "code_spans_oracle_2635.json"
PACK = REPO_ROOT / "resources/knowledge/pack/runbooks"
_CODE_SPAN_RE = g.CODE_SPAN_RE


@cache
def _corpus() -> dict[str, str]:
    """Synthetic texts by name, then the grammar fixtures (two of them frozen
    pack runbooks) and every shipped runbook by repo path."""
    texts = {f"2622-corpus-{i}": text for i, text in enumerate(_CORPUS)}
    texts |= {
        "around-fences": "`a` before\n```\n`b` inside\n```\n`c` after `d`",
        "back-to-back-fences": "```\na\n```\n~~~\n`b`\n~~~\n```\n```\n`c`\n```",
        "inline-into-fence": "`a\n```\nb` <!-- c -->\n```\n`d` -->",
        "unclosed-fence": "`a`\n```\n`b` never closed `c`\n",
        "tilde-not-closed-by-backticks": "~~~\n```\n`x`\n~~~\n`y`",
        "short-closer-ignored": "````\n```\n`x`\n````\n`y`",
        "long-closer-closes": "```\n`x`\n``````\n`y`",
        "crlf": "`a`\r\n```\r\n`b`\r\n```\r\n`c`\r\n~~~\r\n`d`\r\n~~~\r\n",
        "dense-5k": "`a` " * 5000,
        "dense-between-fences": ("`a` " * 100 + "\n```\n`b` `c`\n```\n") * 50,
        "quoted-openers": "`<!--` <!-- real `-->` --> " * 500,
    }
    for root in (HERE / "fixtures", PACK):
        for path in sorted(root.rglob("*.md")):
            texts[path.relative_to(REPO_ROOT).as_posix()] = path.read_text("utf-8")
    return texts


@cache
def _random_texts() -> list[str]:
    pieces = ["```", "````", "~~~", "`", "``", " ", "   ", "<!--", "-->", "a"]
    pieces += ["\n## Causes\n", "\n## Symptom Recognition\n", *_BOUNDARIES]
    rng = random.Random(2635)
    return [
        "".join(rng.choice(pieces) for _ in range(rng.randint(0, 60)))
        for _ in range(3000)
    ]


def _crc(text: str) -> int:
    """A number, not a hex string, so the secret scanner reads no key."""
    return zlib.crc32(text.encode("utf-8"))


def _record(text: str) -> dict:
    return {
        "crc32": _crc(text),
        "code": [list(span) for span in g.code_spans(text)],
        "comments": [list(span) for span in g.comment_spans(text)],
        "mask_crc32": _crc(g.mask_html_comments(text)),
        "body": p._has_runbook_body(text),
    }


@cache
def _oracle() -> dict:
    return json.loads(ORACLE.read_text("utf-8"))


@pytest.mark.parametrize("name", list(_corpus()))
def test_the_outputs_are_mains(name):
    text, recorded = _corpus()[name], _oracle()["texts"].get(name)
    if name.startswith("resources/") and (
        recorded is None or _crc(text) != recorded["crc32"]
    ):
        pytest.skip(f"{name} was added or changed after the oracle was recorded")
    assert _record(text) == recorded


def test_the_outputs_on_random_texts_are_mains():
    for i, text in enumerate(_random_texts()):
        assert _record(text) == _oracle()["random"][i], repr(text)


def test_every_recorded_text_is_still_compared():
    """A rename must not retire part of the oracle quietly. Only the pack may
    move on; the frozen copies keep two of its runbooks compared for good."""
    kept = {name for name in _oracle()["texts"] if not name.startswith("resources/")}
    assert kept <= set(_corpus())
    assert len(_oracle()["random"]) == len(_random_texts())


def test_offset_in_spans_answers_any_span_list_in_any_order():
    """The helper is exact for overlapping, unsorted and empty spans, and for
    offsets asked backwards (Stage 1b restarts per heading pattern)."""
    rng = random.Random(2635)
    for _ in range(2000):
        spans = [(lo, lo + rng.randint(0, 6)) for lo in rng.choices(range(30), k=5)]
        inside = g.offset_in_spans(spans)
        for pos in rng.choices(range(40), k=12):
            assert inside(pos) == any(lo <= pos < hi for lo, hi in spans), spans


def _counting(op: str):
    def compare(self, other):
        _Counted.count += 1
        return getattr(int(self), op)(other)

    return compare


class _Counted(int):
    """An offset that counts every comparison made against it."""

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


def _counted_spans(spans):
    return lambda text: [(_Counted(lo), _Counted(hi)) for lo, hi in spans(text)]


def _comparisons(monkeypatch, call, n: int) -> int:
    with monkeypatch.context() as patch:
        patch.setattr(g, "CODE_SPAN_RE", _CountingSpanRe())
        patch.setattr(g, "_protected_spans", _counted_spans(g._protected_spans))
        patch.setattr(p, "code_spans", _counted_spans(g.code_spans))
        _Counted.count = 0
        call(n)
    return _Counted.count


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(
            lambda n: g.code_spans("`x` `y`\n```\n`in`\n```\n" * n), id="code_spans"
        ),
        pytest.param(
            lambda n: g.comment_spans("`<!--` x\n```\n<!--\n```\n" * n + "<!---->"),
            id="comment_spans",
        ),
        pytest.param(
            lambda n: p._has_runbook_body("```\n## Symptom Recognition\n```\n" * n),
            id="stage_1b_body",
        ),
    ],
)
def test_doubling_the_code_at_most_doubles_the_comparisons(monkeypatch, call):
    """Fences and inline spans grow together, so neither a per-span scan of the
    fences nor one of the inline spans can hide behind a fixed count. Compared
    as increments, so a constant at either end of the walk does not count."""
    c1, c2, c4 = (_comparisons(monkeypatch, call, n) for n in (500, 1000, 2000))
    assert c1 >= 500, "the positive control: every offset is checked"
    assert c4 - c2 <= 2 * (c2 - c1), (c1, c2, c4)


if __name__ == "__main__":
    ORACLE.write_text(
        json.dumps(
            {
                "texts": {name: _record(text) for name, text in _corpus().items()},
                "random": [_record(text) for text in _random_texts()],
            },
            separators=(",", ":"),
        )
        + "\n",
        "utf-8",
    )
