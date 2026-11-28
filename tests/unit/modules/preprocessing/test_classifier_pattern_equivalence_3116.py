"""Each rewritten classifier pattern answers as ``main``'s did (#3116 B2).

The yes/no, or for the prose pattern the ``findall`` count, compared with
``main``'s form under the site's flags: over every string of each alphabet's
symbols up to its length, then over a seeded sample of longer strings that
holds the strings the design critique named, and over the first 5,000
characters of each shipped KB pack chunk, as written and lowercased (the
classifier lowercases its sample). The patterns: ``classifier_rewrites_3116.py``.
"""

from __future__ import annotations

import itertools
import json
import pathlib
import random

import pytest

from tests.unit.modules.preprocessing.classifier_rewrites_3116 import (
    NAMED,
    REWRITES,
    SAMPLE,
    answer,
)

pytestmark = [pytest.mark.unit, pytest.mark.slow]

REPO = pathlib.Path(__file__).resolve().parents[4]

_ENUMERATIONS = [
    pytest.param(name, index, id=f"{name}-{index}")
    for name, rewrite in REWRITES.items()
    for index in range(len(rewrite.alphabets))
]


@pytest.mark.parametrize("name, index", _ENUMERATIONS)
def test_each_rewrite_answers_as_main_over_every_short_string(name, index):
    rewrite = REWRITES[name]
    alphabet, longest = rewrite.alphabets[index]
    before, after = answer(rewrite, rewrite.main), answer(rewrite, rewrite.now)
    differ, matched, texts = [], 0, 0
    for length in range(longest + 1):
        for text in map("".join, itertools.product(alphabet, repeat=length)):
            texts += 1
            reading = before(text)
            matched += bool(reading)
            if reading != after(text) and len(differ) < 5:
                differ.append(text)
    assert differ == []
    # Not vacuous: the alphabet reaches matches and misses alike.
    assert 0 < matched < texts


def _sampled(seed: int) -> list[str]:
    """Longer strings over every rewrite's symbols and the named strings."""
    rng = random.Random(seed)
    symbols = sorted({s for r in REWRITES.values() for a, _ in r.alphabets for s in a})
    symbols += NAMED + ["calls in 1.5 seconds", "a() = 0", "}", "%", "+", "-"]
    texts = list(NAMED)
    for _ in range(4_000):
        texts.append("".join(rng.choices(symbols, k=rng.randint(2, 24))))
    return texts


def _pack_chunks() -> list[str]:
    pack = json.loads((REPO / "resources/knowledge/pack/pack.json").read_text())
    chunks = [c["text"][:SAMPLE] for book in pack["runbooks"] for c in book["chunks"]]
    assert len(chunks) > 1_000, "the KB pack corpus is missing"
    return chunks


@pytest.mark.parametrize("name", REWRITES)
def test_each_rewrite_answers_as_main_over_a_sample_and_the_kb_pack(name):
    rewrite = REWRITES[name]
    before, after = answer(rewrite, rewrite.main), answer(rewrite, rewrite.now)
    corpus = _sampled(3116) + _pack_chunks()
    corpus += [text.lower() for text in corpus]
    assert [text for text in corpus if before(text) != after(text)][:5] == []
