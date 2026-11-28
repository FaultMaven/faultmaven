"""The Tier 0 classifier's patterns run in linear time inside its sample (#3116 B2).

Each rewritten pattern alone, and ``classify`` through its entry point, grow
linearly up to their limits; ``main``'s form of each pattern does not. The
classifier's verdicts are as on ``main``. The patterns and why they changed:
``classifier_rewrites_3116.py``; their answers compared exhaustively:
``test_classifier_pattern_equivalence_3116.py``.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

from faultmaven.models.api import DataType, SourceMetadata
from faultmaven.modules.preprocessing import classifier
from faultmaven.modules.preprocessing.classifier import DataClassifier
from tests.unit.modules.preprocessing.classifier_rewrites_3116 import (
    NL,
    REWRITES,
    SAMPLE,
    answer,
    payload,
)
from tests.wallclock import assert_linear_growth

pytestmark = [pytest.mark.unit]

STEP = 8

_GROWTH = [
    pytest.param(name, shape, id=f"{name}-{shape!r}")
    for name, rewrite in REWRITES.items()
    for shape in rewrite.shapes
]


# --- Each rewritten pattern, alone ------------------------------------------


@pytest.mark.parametrize("name, shape", _GROWTH)
def test_each_rewrite_is_linear_up_to_its_limit(name, shape):
    """Under its site's flags, from a window whose top is at its limit."""
    rewrite = REWRITES[name]
    assert_linear_growth(
        answer(rewrite, rewrite.now),
        lambda n: payload(shape, n),
        small=rewrite.limit // STEP**2,
        max_escalations=0,
        undecided_repeats=2,
        label=f"{name} over {shape!r}",
    )


@pytest.mark.parametrize("name", REWRITES)
def test_mains_form_is_super_linear_on_the_first_shape(name):
    """What "fails on ``main``" means for the pattern alone. The window tops
    out at a sixteenth of the limit, where the quadratic already decides (and
    iostat's quartic costs seconds above it); one step up if it does not."""
    rewrite = REWRITES[name]
    with pytest.raises(AssertionError, match="is the usual cause"):
        assert_linear_growth(
            answer(rewrite, rewrite.main),
            lambda n: payload(rewrite.shapes[0], n),
            small=max(rewrite.limit // (16 * STEP**2), 8),
            max_escalations=1,
            undecided_repeats=2,
            label=f"main's {name}",
        )


# --- Through the classifier -------------------------------------------------

_UPLOAD = SourceMetadata(source_type="file_upload")
_PASTE = SourceMetadata(source_type="text_paste")
#: The shapes ``main``'s classify was quadratic on through the entry point.
_STRONG = (("", "a"), ("", "1"), ("", "a."), ("", "a_"), ("", "a;"), NL)
_ENTRY = [
    pytest.param(filename, source, shape, id=f"{source.source_type}-{shape!r}")
    for filename, source in (("x.log", _UPLOAD), ("pasted-content-1.txt", _PASTE))
    for shape in _STRONG
]


@pytest.mark.performance
@pytest.mark.parametrize("filename, source, shape", _ENTRY)
def test_classify_is_linear_up_to_its_sample(filename, source, shape):
    """Each read super-linear through ``main``'s classify (0.3-1.6 s at 4,992).

    ``performance``: under coverage the tracer raises ``classify``'s fixed
    cost past what the window's top size can show, and the call cannot decide.
    ``benchmarks.yml`` runs it with ``--no-cov``; the per-pattern tests above
    stay in the required jobs.
    """
    dc = DataClassifier()
    assert_linear_growth(
        lambda text: dc.classify(filename, text, source_metadata=source),
        lambda n: payload(shape, n),
        small=SAMPLE // STEP**2,
        max_escalations=0,
        undecided_repeats=2,
        label=f"classify({filename!r}) over {shape!r}",
    )


# --- The classifier's verdicts ----------------------------------------------


class _Patterns:
    """``re`` as the classifier module sees it: records each pattern it runs
    with its flags and, for ``main``, runs each rewrite's ``main`` form."""

    def __init__(self, as_main: bool):
        self.seen: set[tuple[str, int]] = set()
        self._swap = {r.now: r.main for r in REWRITES.values()} if as_main else {}

    def __getattr__(self, name):
        return getattr(re, name)

    def _run(self, fn, pattern, string, flags=0):
        self.seen.add((pattern, int(flags)))
        return fn(self._swap.get(pattern, pattern), string, flags)

    def search(self, pattern, string, flags=0):
        return self._run(re.search, pattern, string, flags)

    def findall(self, pattern, string, flags=0):
        return self._run(re.findall, pattern, string, flags)

    def finditer(self, pattern, string, flags=0):
        return self._run(re.finditer, pattern, string, flags)


_TESTS = pathlib.Path(__file__).resolve().parents[3]


def _classifier_tests() -> list[pathlib.Path]:
    """Every test module that uses ``DataClassifier``: its literals are inputs."""
    return [
        path
        for path in sorted(_TESTS.rglob("*.py"))
        if "DataClassifier" in path.read_text(encoding="utf-8")
    ]


def _fixture_texts() -> list[str]:
    modules = _classifier_tests()
    assert len(modules) > 15, "the classifier's test modules were not found"
    texts = {
        node.value
        for path in modules
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    texts |= {payload(shape, 400) for shape in _STRONG}
    return sorted(texts)


def _verdicts(monkeypatch, as_main: bool, texts: list[str]):
    patterns = _Patterns(as_main)
    monkeypatch.setattr(classifier, "re", patterns)
    dc = DataClassifier()
    verdicts = []
    for text in texts:
        for filename in ("x.log", "x.txt", "x.csv", "x"):
            for source in (_UPLOAD, _PASTE):
                verdicts.append(dc.classify(filename, text, source_metadata=source))
        verdicts.append(
            dc.classify("x.dat", text, agent_hint=DataType.STRUCTURED_CONFIG)
        )
    return verdicts, patterns.seen


def test_every_classifier_fixture_classifies_as_on_main(monkeypatch):
    texts = _fixture_texts()
    assert len(texts) > 200

    now, seen = _verdicts(monkeypatch, False, texts)
    on_main, _ = _verdicts(monkeypatch, True, texts)

    assert now == on_main
    # The forms held as ``now`` are the ones the classifier runs, under these flags.
    assert {(r.now, r.flags) for r in REWRITES.values()} <= seen
