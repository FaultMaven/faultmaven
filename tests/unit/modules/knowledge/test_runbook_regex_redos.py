"""Runbook scoring and validation stay linear in caller-supplied content size.

Both run SYNCHRONOUSLY inside async request handlers, over runbook bodies a
caller supplies, so a quadratic regex here stalls the event loop for the whole
process, not just one worker (CodeQL ``py/polynomial-redos``).

**Drive the right entry point.** The expensive patterns live in
``QualityScorer``, NOT ``RunbookValidator``, and the two are reached by
different routes:

* ``QualityScorer.score_content`` — ``PUT .../drafts/{id}`` and
  ``POST /knowledge/runbooks/create``, both open to any authenticated user.
* ``RunbookValidator.validate_content`` — the upload path. It returns on
  missing frontmatter long before scoring, so a bare-fence payload never
  reaches the costly code at all.

That distinction has now produced a vacuous test twice, which is why it is
stated first and why every guard below carries its own reason for biting:

* the first version of this file drove ``validate_content`` with bare fences,
  so restoring the quadratic regex left every test passing;
* the second version's corpus test claimed to pin the *fence* change while
  driving ``validate_content``, which never calls ``QualityScorer`` at all —
  the class it was pinning was not on the path it exercised.

**Two hostile shapes, not one.** The reported reproduction was fence-heavy, and
a fix and tests built around that shape missed a second quadratic regex
(``has_fix``) sixteen lines away in the same function. A body of nothing but
alternating newline and space — no fences whatsoever — cost 28s at 100 KB.
Both shapes are asserted here; neither alone is sufficient.

**Absolute budgets first, growth ratios second.** A ratio on a sub-10ms
baseline is noise-dominated on a shared runner. The payloads here are large
enough that the measurement dominates scheduling jitter, every timing is a
median of several runs, and the load-bearing assertion is an absolute bound
with an order of magnitude of headroom — a quadratic regression takes seconds,
not percent.
"""

from __future__ import annotations

import pathlib
import re
import statistics
import time

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.knowledge_base]

FENCE = "`" * 3

#: A fence-heavy body: the originally reported shape. BARE fences, deliberately
#: -- a WELL-FORMED fence (```` ```\ncmd\n```\nThen check it. ````) does not
#: trigger the lazy `.*?` at all, because it finds its closer immediately. A
#: draft of this file used the well-formed shape and the mutation control caught
#: it: restoring the quadratic regex left the growth test green, because the
#: payload never exercised the path. Measured with the quadratic pattern:
#: bare fences 40.9s at 48 KB, well-formed fences 0.0007s.
FENCE_UNIT = FENCE
#: A body with NO fences at all: the shape `has_fix` was quadratic on, and the
#: shape the first fix and its tests were both blind to.
NO_FENCE_UNIT = "\n "

_REPS = 3


def _kb(unit: str, kilobytes: int) -> str:
    return unit * max(1, (kilobytes * 1024) // len(unit))


def _once(fn, payload: str) -> float:
    """One measurement, after a warm-up. For budget assertions only.

    A budget with orders of magnitude of headroom does not need a median, and
    repeating it multiplies how long a REGRESSION takes to report: the whole
    point of the payload sizes below is that a reintroduced quadratic fails in
    seconds rather than making CI sit through it five times.
    """
    fn(payload[:64])
    start = time.perf_counter()
    fn(payload)
    return time.perf_counter() - start


def _median_elapsed(fn, payload: str) -> float:
    """Median wall-clock over ``_REPS`` runs, after a warm-up call.

    Median rather than a single sample because one scheduling slice or GC pause
    landing in the only measurement is enough to move a ratio past its bound
    with the code entirely correct.
    """
    fn(payload[:64])
    samples = []
    for _ in range(_REPS):
        start = time.perf_counter()
        fn(payload)
        samples.append(time.perf_counter() - start)
    return statistics.median(samples)


def _growth_ratio(fn, small: str, large: str) -> float:
    """Time ratio for a 2x input. ~2 is linear; ~4 is the quadratic signature.

    ``small`` is measured FIRST and bound to a name. Written as a single
    expression -- ``_median(large) / _median(small)`` -- Python evaluates the
    numerator first, so ``large`` is timed on the colder caches and its cost is
    overstated; that ordering alone put this ratio at 3.6 on correct code.
    """
    small_seconds = max(_median_elapsed(fn, small), 1e-6)
    large_seconds = _median_elapsed(fn, large)
    return large_seconds / small_seconds


def _scorer():
    from faultmaven.modules.knowledge.domain.services.runbook_validator import (
        QualityScorer,
    )

    return QualityScorer()


def _time_scoring(payload: str) -> float:
    """Seconds to score ``payload`` — a duration, not a quality score."""
    return _median_elapsed(_scorer().score_content, payload)


def _runbook_corpus() -> list[pathlib.Path]:
    root = (
        pathlib.Path(__file__).resolve().parents[4]
        / "resources/knowledge/pack/runbooks"
    )
    books = sorted(root.rglob("*.md"))
    assert (
        len(books) > 50
    ), f"corpus missing at {root} — every guard below would be vacuous"
    return books


# --------------------------------------------------------------------------
# Cost
# --------------------------------------------------------------------------


#: Sizes are chosen so a reintroduced quadratic fails FAST, not just fails.
#: Measured with the quadratic patterns restored: bare fences 12.2s at 24 KB,
#: no-fence 5.9s at 48 KB. Measured with them fixed: 0.005s and 0.035s. Every
#: budget below therefore has at least a 28x margin over correct behaviour and
#: reports a regression within seconds.
_BUDGET_SECONDS = 1.0


def test_scoring_a_fence_heavy_body_costs_a_time_a_request_can_afford():
    """The originally reported shape. 12.2s at 24 KB with the lazy `.*?`."""
    assert _once(_scorer().score_content, _kb(FENCE_UNIT, 24)) < _BUDGET_SECONDS


def test_scoring_a_body_with_no_fences_costs_a_time_a_request_can_afford():
    """The shape the first fix missed entirely.

    ``has_fix`` used ``^\\s*`` under MULTILINE -- ``\\s`` matches ``\\n``, so it
    ran past the line it anchored to and rescanned from every line start. With
    the fence regex already fixed, this body still cost 5.9s at 48 KB and 28s
    at 100 KB. ``MAX_UPLOAD_SIZE_MB`` defaults to 10, so 100 KB is 1% of what a
    caller may send.
    """
    assert _once(_scorer().score_content, _kb(NO_FENCE_UNIT, 48)) < _BUDGET_SECONDS


def test_validation_on_adversarial_frontmatter_costs_a_time_a_request_can_afford():
    """An unterminated frontmatter block is the worst case for the delimiter.

    This one IS in ``RunbookValidator``, so it is reached by the upload path
    rather than only by the draft-edit and manual-create routes.
    """
    from faultmaven.modules.knowledge.domain.services.runbook_validator import (
        RunbookValidator,
    )

    payload = "---\n" + _kb(NO_FENCE_UNIT, 48)

    assert _once(RunbookValidator().validate_content, payload) < _BUDGET_SECONDS


def test_scoring_grows_linearly_with_body_size():
    """Growth SHAPE, as a backstop to the budgets above.

    A budget catches a quadratic that is already expensive at the sizes tested;
    a ratio also catches a milder one that is still on the wrong curve. Run on
    the no-fence shape because its linear cost (18 ms at 24 KB) sits far enough
    above scheduling noise for the ratio to mean something -- the fence shape's
    linear cost is 5 ms, where a GC pause alone moves the ratio.
    """
    ratio = _growth_ratio(
        _scorer().score_content, _kb(NO_FENCE_UNIT, 24), _kb(NO_FENCE_UNIT, 48)
    )

    assert ratio < 3.0, (
        f"doubling the body multiplied scoring work by {ratio:.1f}x -- "
        "the quadratic signature of a backtracking regex on caller content"
    )


# --------------------------------------------------------------------------
# Verdicts and scores are unchanged
# --------------------------------------------------------------------------

#: Pinned on the pre-fix code. Both sums are sensitive to the fence regex —
#: `actionability` carries its +10 bonus — so a change that alters what counts
#: as a command explanation moves these, which is exactly what the previous
#: version of this test claimed to check and structurally could not.
_CORPUS_ACTIONABILITY_SUM = 8655.0
_CORPUS_OVERALL_SUM = 8236.2


def test_the_shipped_corpus_still_validates_identically():
    """A performance fix that changes verdicts is not a performance fix."""
    from faultmaven.modules.knowledge.domain.services.runbook_validator import (
        RunbookValidator,
    )

    validator = RunbookValidator()
    failures = [
        p.name
        for p in _runbook_corpus()
        if not validator.validate_content(p.read_text(encoding="utf-8")).passed
    ]

    assert failures == [], f"the ReDoS fix changed validation verdicts: {failures}"


def test_the_shipped_corpus_still_scores_identically():
    """The guard the previous version of this file could not be.

    ``RunbookValidator`` never calls ``QualityScorer`` — the validator class
    ends well above where the scorer begins — so a test driving
    ``validate_content`` cannot observe the fence regex at all. This one scores
    every runbook and pins two aggregates the fence bonus feeds.
    """
    scorer = _scorer()
    scores = [
        scorer.score_content(p.read_text(encoding="utf-8")) for p in _runbook_corpus()
    ]

    assert sum(s.actionability for s in scores) == pytest.approx(
        _CORPUS_ACTIONABILITY_SUM
    ), "the fence pattern changed what counts as a command explanation"
    assert sum(s.overall for s in scores) == pytest.approx(_CORPUS_OVERALL_SUM)


def test_a_bash_fence_containing_inline_backticks_still_counts():
    """``[^`]*`` excluded EVERY backtick, not just a fence-closing triple.

    Shell command substitution inside a bash fence is idiomatic runbook
    content. Under the over-narrow pattern this body counted zero command
    explanations instead of four, dropping actionability 65 -> 55 and moving
    content across ``QUALITY_WARNING_THRESHOLD``.
    """
    body = (
        FENCE + "bash\necho `date`\nkubectl get pods\n" + FENCE + "\nThen inspect.\n"
    ) * 4

    assert _scorer().score_content(body).actionability == 65.0


# --------------------------------------------------------------------------
# CRLF
# --------------------------------------------------------------------------


def test_crlf_content_is_scored_and_validated_like_its_lf_twin():
    """Narrowing ``\\s*`` to ``[ \\t]*`` silently dropped CRLF support.

    ``\\s`` matched the ``\\r`` of a CRLF line ending by accident. Once it was
    gone, ``^---[ \\t]*\\n`` no longer matched ``---\\r\\n``: a Windows-authored
    runbook reported "No YAML frontmatter found", every ``REQUIRED_METADATA``
    check fired on content that had the metadata, and the score fell 18 points
    into a different grade. The shipped corpus is LF-only, so a corpus
    comparison cannot see this — it needs its own fixture.
    """
    from faultmaven.modules.knowledge.domain.services.runbook_validator import (
        RunbookValidator,
    )
    from faultmaven.utils.frontmatter import match_frontmatter, parse_frontmatter

    lf = _runbook_corpus()[0].read_text(encoding="utf-8")
    crlf = lf.replace("\n", "\r\n")

    assert match_frontmatter(crlf) is not None, "CRLF frontmatter stopped parsing"
    assert parse_frontmatter(crlf) == parse_frontmatter(lf)

    validator = RunbookValidator()
    crlf_errors = list(validator.validate_content(crlf).errors or [])
    assert "No YAML frontmatter found" not in crlf_errors, (
        "frontmatter stopped parsing on CRLF, so every REQUIRED_METADATA check "
        f"fires on content that carries the metadata: {crlf_errors}"
    )
    assert not any(e.startswith("Missing required metadata field") for e in crlf_errors)

    # The scorer reads the same block, and its result is persisted to
    # `conversion_drafts.quality_score` and drives the warning threshold.
    scorer = _scorer()
    assert scorer._extract_metadata(crlf) == scorer._extract_metadata(lf)

    # Scoped deliberately. CRLF *section* matching is a separate, PRE-EXISTING
    # gap -- `_validate_sections` is `\n`-specific on main too, so a CRLF
    # runbook reports missing sections there with or without this change. This
    # guard pins the regression this change caused and does not claim the rest.


# --------------------------------------------------------------------------
# The grammar has exactly one definition
# --------------------------------------------------------------------------

#: Matches a hand-rolled frontmatter-delimiter regex in a source line.
_INLINE_GRAMMAR = re.compile(r'r"\^-{3}')


def test_the_frontmatter_grammar_is_defined_in_exactly_one_place():
    """Nine copies of this regex drifted apart; that is the root defect.

    Four were fixed and five left, so a document's YAML was stripped by the
    chunker but counted as body by the validator, and the two disagreed about
    where the document started. A guard on the count is the only thing that
    stops the tenth copy being written — the fix itself does not.
    """
    root = pathlib.Path(__file__).resolve().parents[4] / "faultmaven"
    canonical = root / "utils" / "frontmatter.py"
    assert canonical.exists(), f"canonical grammar missing at {canonical}"

    offenders = sorted(
        f"{p.relative_to(root.parent)}:{n}"
        for p in root.rglob("*.py")
        if p != canonical
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if _INLINE_GRAMMAR.search(line)
    )

    assert offenders == [], (
        "frontmatter delimiter regex written inline instead of imported from "
        f"faultmaven.utils.frontmatter: {offenders}"
    )
