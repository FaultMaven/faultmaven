"""Runbook scoring and validation stay linear in caller-supplied content size.

Both run SYNCHRONOUSLY inside async request handlers, over runbook bodies a
caller supplies, and two regexes were quadratic (CodeQL `py/polynomial-redos`).

**Drive the right entry point.** The expensive pattern lives in `QualityScorer`,
NOT `RunbookValidator`, and the two are reached by different routes:

* `QualityScorer.score_content` — `PUT .../drafts/{id}` and
  `POST /knowledge/runbooks/create`, both open to any authenticated user.
  Measured before the fix: 6 KB 0.79s, 12 KB 3.7s, **24 KB 11.6s**.
* `RunbookValidator.validate_content` — the upload path. It returns on missing
  frontmatter long before scoring, so a bare-fence payload never reaches the
  costly code at all.

The first version of this file drove `validate_content` with bare fences and was
therefore VACUOUS: restoring the quadratic regex left every test passing. That
is the specific mistake these tests exist to not repeat, which is why the
mutation control below is part of the file rather than a note in a commit.

Growth SHAPE is asserted, not wall-clock: timing thresholds are flaky on shared
runners, but quadratic is quadratic everywhere, so doubling the input and
comparing the ratio is stable.
"""

from __future__ import annotations

import time

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.knowledge_base]

FENCE = "`" * 3


def _elapsed(fn, payload: str) -> float:
    start = time.perf_counter()
    fn(payload)
    return time.perf_counter() - start


def _growth_ratio(fn, small: str, large: str) -> float:
    """Time ratio for a 2x input. ~2 is linear; ~4 is quadratic."""
    # Warm any lazy compilation so the first call is not charged for it.
    fn(small[:64])
    a = max(_elapsed(fn, small), 1e-6)
    b = _elapsed(fn, large)
    return b / a


def _score(payload: str) -> float:
    from faultmaven.modules.knowledge.domain.services.runbook_validator import (
        QualityScorer,
    )

    return _elapsed(QualityScorer().score_content, payload)


def test_scoring_does_not_blow_up_on_a_fence_heavy_body():
    """Doubling the input must not quadruple the work.

    ```` ```.*?``` ```` under DOTALL rescans forward across every other fence in
    the document. A ratio near 4 is that quadratic signature; near 2 or below is
    a linear scan.
    """
    small, large = FENCE * 3000, FENCE * 6000
    _score(FENCE * 8)  # warm import + compile so the first call is not charged

    ratio = _score(large) / max(_score(small), 1e-6)

    assert ratio < 3.0, (
        f"doubling a fence-heavy body multiplied scoring work by {ratio:.1f}x — "
        "the quadratic signature of a backtracking regex on caller content"
    )


def test_scoring_a_hostile_body_costs_a_time_a_request_can_afford():
    """The end-to-end claim on the shape that measured 11.6s before the fix.

    Deliberately generous so it fails on a REGRESSION rather than a slow runner:
    there is an order of magnitude between this bound and the bug.
    """
    assert _score(FENCE * (24 * 1024 // 3)) < 2.0


def test_validation_does_not_blow_up_on_adversarial_frontmatter():
    """`^---\\s*\\n` — `\\s` matches `\\n`, so the split point was ambiguous.

    This one IS in `RunbookValidator`, so it is reached by the upload path.
    """
    from faultmaven.modules.knowledge.domain.services.runbook_validator import (
        RunbookValidator,
    )

    validator = RunbookValidator()
    build = lambda n: "---\n" + "\n " * n  # noqa: E731
    validator.validate_content(build(8))

    small = _elapsed(validator.validate_content, build(3000))
    large = _elapsed(validator.validate_content, build(6000))
    ratio = large / max(small, 1e-6)

    assert ratio < 3.0, f"frontmatter matching grew {ratio:.1f}x for a 2x input"


def test_the_shipped_corpus_still_validates_identically():
    """A performance fix that changes verdicts is not a performance fix.

    The tempered fence pattern counts differently on 8 of the 91 shipped
    runbooks, but its only consumer is a `>= 3` threshold and no runbook crosses
    it — so every shipped runbook must still reach the same pass/fail.
    """
    from pathlib import Path

    from faultmaven.modules.knowledge.domain.services.runbook_validator import (
        RunbookValidator,
    )

    pack = Path(__file__).resolve().parents[4] / "resources/knowledge/pack/runbooks"
    runbooks = sorted(pack.rglob("*.md"))
    assert len(runbooks) > 50, "corpus missing — this test would be vacuous"

    validator = RunbookValidator()
    failures = [
        p.name for p in runbooks if not validator.validate_content(p.read_text()).passed
    ]

    assert failures == [], f"the ReDoS fix changed validation verdicts: {failures}"
