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

import ast
import pathlib
import re
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
#: A bare `[`, repeated. A THIRD hostile shape: it carries no fences, no
#: newlines and no whitespace, so neither payload above reaches the link
#: regex's backtracking at all.
#:
#: A BARE bracket, not `"[aaaa](http://x"`. The first version of this guard
#: used the link-shaped payload and the mutation control showed why that was
#: the wrong choice: on the unbounded pattern the link-shaped body costs 1.5s
#: at 192 KB while bare brackets cost 257s, so the cheap shape was being used
#: to certify the expensive one. Same error as testing well-formed fences for
#: a bug that only bites on bare ones.
LINK_UNIT = "["

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


def _fastest_elapsed(fn, payload: str) -> float:
    """Best wall-clock over ``_REPS`` runs, after a warm-up call.

    MINIMUM, not mean or median. Every source of noise here is additive -- a
    scheduling slice, a GC pause, another test on a neighbouring core -- so the
    fastest sample is the one least contaminated by things that are not the
    code under test, and it is the standard estimator for exactly that reason.

    A median was tried first and still flaked: the ratio test passed 6/6 alone
    and failed when run alongside a 117-test module, which is the condition CI
    actually runs under. A median of three is only as good as its middle
    sample, and under load two of three can be contended.
    """
    fn(payload[:64])
    return min(_elapsed(fn, payload) for _ in range(_REPS))


def _elapsed(fn, payload: str) -> float:
    start = time.perf_counter()
    fn(payload)
    return time.perf_counter() - start


def _growth_ratio(fn, small: str, large: str) -> float:
    """Time ratio for a 2x input. ~2 is linear; ~4 is the quadratic signature.

    ``small`` is measured FIRST and bound to a name. Written as a single
    expression -- ``_median(large) / _median(small)`` -- Python evaluates the
    numerator first, so ``large`` is timed on the colder caches and its cost is
    overstated; that ordering alone put this ratio at 3.6 on correct code.
    """
    small_seconds = max(_fastest_elapsed(fn, small), 1e-6)
    large_seconds = _fastest_elapsed(fn, large)
    return large_seconds / small_seconds


def _scorer():
    from faultmaven.modules.knowledge.domain.services.runbook_validator import (
        QualityScorer,
    )

    return QualityScorer()


def _time_scoring(payload: str) -> float:
    """Seconds to score ``payload`` — a duration, not a quality score."""
    return _fastest_elapsed(_scorer().score_content, payload)


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


def test_validation_of_bracket_heavy_content_costs_a_time_a_request_can_afford():
    """The external-link matcher, on the shape that costs the most.

    ``\\[([^\\]]+)\\]\\(https?://[^\\)]+\\)`` is unbounded on both sides, so every
    ``[`` starts a scan to the end of the input looking for a closing paren
    that never arrives, and the next ``[`` does it again. On main, 192 KB of
    bare brackets costs **257s**; ``MAX_UPLOAD_SIZE_MB`` defaults to 10.

    The fix is POSSESSIVE quantifiers, not a narrower character class and not a
    plain cap. See ``EXTERNAL_LINK_RE``'s own comment for why both of those
    were tried and rejected.
    """
    from faultmaven.modules.knowledge.domain.services.runbook_validator import (
        RunbookValidator,
    )

    seconds = _fastest_elapsed(RunbookValidator().validate_content, _kb(LINK_UNIT, 192))

    assert seconds < _BUDGET_SECONDS, (
        f"{seconds:.3f}s to validate 192 KB of brackets — the unbounded "
        "pattern took 257s on the same input"
    )


def test_the_link_pattern_matches_exactly_what_the_unbounded_one_did():
    """Equivalence against the ORIGINAL pattern, over the shipped corpus.

    The previous version of this guard counted links in a 22-link synthetic
    fixture. That could not see a narrowing, because the fixture only contained
    the forms the narrowed pattern still matched — and a narrowed pattern did
    ship and did stop matching titled links. Comparing against the unbounded
    original over real runbooks is the check that would have caught it.
    """
    from faultmaven.modules.knowledge.domain.services.runbook_validator import (
        EXTERNAL_LINK_RE,
    )

    unbounded = re.compile(r"\[([^\]]+)\]\(https?://[^\)]+\)")

    disagreements = []
    for path in _runbook_corpus():
        body = path.read_text(encoding="utf-8")
        got, want = len(EXTERNAL_LINK_RE.findall(body)), len(unbounded.findall(body))
        if got != want:
            disagreements.append(f"{path.name}: bounded={got} unbounded={want}")

    assert (
        disagreements == []
    ), f"the link pattern stopped matching what it used to: {disagreements}"


@pytest.mark.parametrize(
    "markdown",
    [
        "[the docs](https://example.com/a/b?c=d)",
        # CommonMark titled link. The narrowed pattern that shipped in the
        # first version of this fix returned ZERO for this, because the title
        # sits inside the parens behind a space and the class excluded \s.
        '[the docs](https://example.com/a "The Docs")',
        # Link text may span lines in CommonMark; excluding \n dropped these.
        "[the\ndocs](https://example.com/a)",
        "[x](https://example.org/page?a=1&b=2#frag)",
    ],
    ids=["plain", "titled", "multiline-text", "query-and-fragment"],
)
def test_real_markdown_link_forms_are_still_counted(markdown):
    """A pattern that stops matching a real form fails silently.

    The only consumer is a ``len(links) == 0`` warning, so nothing breaks — the
    runbook just stops being credited with its references.
    """
    from faultmaven.modules.knowledge.domain.services.runbook_validator import (
        EXTERNAL_LINK_RE,
    )

    assert EXTERNAL_LINK_RE.findall(markdown), f"stopped matching: {markdown!r}"


def test_the_link_caps_are_where_the_constants_say_they_are():
    """Pins the caps at their actual values, which the old fixture did not.

    Its longest link text was 8 characters and its longest post-scheme URL 21,
    so caps of ``{1,8}`` and ``{1,21}`` — 1.6% and 1.0% of the shipped values —
    still returned the expected count. A tightening to ``{1,64}``, which would
    drop any GitHub permalink, would have left it green.
    """
    from faultmaven.modules.knowledge.domain.services.runbook_validator import (
        EXTERNAL_LINK_RE,
        MAX_LINK_TEXT_CHARS,
        MAX_LINK_URL_AFTER_SCHEME_CHARS,
    )

    def link(text_len: int, after_scheme_len: int) -> str:
        return f"[{'t' * text_len}](https://{'u' * after_scheme_len})"

    # At the cap: matched. One over: not. The URL cap bounds what follows the
    # scheme, not the whole URL -- `2048 - len("https://")` reads like the edge
    # and sits 8 characters inside it, which is how the first version of this
    # test passed against a boundary it never reached.
    assert EXTERNAL_LINK_RE.findall(link(MAX_LINK_TEXT_CHARS, 8))
    assert not EXTERNAL_LINK_RE.findall(link(MAX_LINK_TEXT_CHARS + 1, 8))

    assert EXTERNAL_LINK_RE.findall(link(8, MAX_LINK_URL_AFTER_SCHEME_CHARS))
    assert not EXTERNAL_LINK_RE.findall(link(8, MAX_LINK_URL_AFTER_SCHEME_CHARS + 1))


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


def _reference_command_explanation_count(content: str) -> int:
    """Count fence-then-prose pairs WITHOUT a regex, by index scanning.

    An independent second opinion for the tempered pattern. Pinning absolute
    score sums was tried first and is the wrong instrument: the vendored KB
    pack is rebuilt periodically by ``kb-build-pack``, so editing a single
    runbook would fail this test with a message blaming the fence regex on a
    PR that never touched one. This compares the regex against a reference on
    whatever the corpus happens to contain, so it pins the invariant that
    matters and is indifferent to pack content.
    """
    count, i = 0, 0
    while True:
        open_at = content.find("```", i)
        if open_at == -1:
            return count
        close_at = content.find("```", open_at + 3)
        if close_at == -1:
            return count
        j = close_at + 3
        while j < len(content) and content[j] in " \t":
            j += 1
        if j < len(content) and content[j] == "\r":
            j += 1
        if j < len(content) and content[j] == "\n":
            k = j + 1
            while k < len(content) and content[k].isspace():
                k += 1
            if k < len(content) and content[k].isupper():
                count += 1
        i = close_at + 3


def test_the_fence_pattern_agrees_with_a_regex_free_reference():
    """The guard the previous version of this file could not be.

    ``RunbookValidator`` never calls ``QualityScorer`` -- the validator class
    ends well above where the scorer begins -- so a test driving
    ``validate_content`` cannot observe the fence regex at all. That is what
    made the earlier corpus test vacuous about the very change it named.

    This one runs the shipped pattern against every runbook and checks it
    against a reference that uses no regex, so a change to what counts as a
    command explanation shows up as a disagreement rather than as a score
    nobody notices.
    """
    from faultmaven.modules.knowledge.domain.services import runbook_validator

    pattern = re.compile(r"```(?:[^`]|`(?!``))*```[ \t]*\r?\n\s*[A-Z]")
    source = pathlib.Path(runbook_validator.__file__).read_text(encoding="utf-8")
    assert pattern.pattern in source, (
        "the fence pattern moved; this guard is comparing a stale copy and "
        "would pass while the shipped one changed"
    )

    disagreements = []
    for p in _runbook_corpus():
        body = p.read_text(encoding="utf-8")
        got, want = len(pattern.findall(body)), _reference_command_explanation_count(
            body
        )
        if got != want:
            disagreements.append(f"{p.name}: regex={got} reference={want}")

    assert disagreements == [], (
        "the fence pattern changed what counts as a command explanation: "
        f"{disagreements}"
    )


def test_the_shipped_corpus_is_still_scored_without_error():
    """Every shipped runbook still scores, and nothing lands at grade F.

    A floor rather than a pinned sum, for the same pack-rebuild reason as
    above: it catches a change that breaks scoring outright without failing on
    a runbook edit.
    """
    scorer = _scorer()
    scores = {
        p.name: scorer.score_content(p.read_text(encoding="utf-8"))
        for p in _runbook_corpus()
    }

    failing = {n: s.overall for n, s in scores.items() if s.grade == "F"}
    assert failing == {}, f"shipped runbooks scoring F: {failing}"


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
# The chunker's other splitter
# --------------------------------------------------------------------------


def test_chunking_a_body_with_no_headers_costs_a_time_a_request_can_afford():
    """The horizontal-rule splitter carried the same defect as the frontmatter one.

    ``\\n\\s*(?:---+|\\*\\*\\*+|___+)\\s*\\n`` -- ``\\s`` matches ``\\n``, so a body of
    alternating newline and space admits many ways to reach the same rule and
    is rescanned. It ran on every document with no markdown headers (the header
    split returns one section, and this is what runs next), synchronously
    inside ``async def _index_document_in_vector_store``, on content from
    ``POST /knowledge/documents``. Measured before the fix: 0.31s at 8 KB,
    17.9s at 64 KB -- and MAX_UPLOAD_SIZE_MB defaults to 10, so 64 KB is 0.6%
    of the ceiling.

    It lived here and in ``ingestion`` verbatim, which is the same
    two-copies-drift story as the frontmatter grammar, so it is now one
    definition both import.
    """
    from faultmaven.modules.knowledge.domain.services.content_chunker import (
        ContentChunker,
    )

    body = "x" + _kb(NO_FENCE_UNIT, 64) + "x"

    assert _once(ContentChunker().split, body) < _BUDGET_SECONDS


# --------------------------------------------------------------------------
# Frontmatter that is valid YAML but not a mapping
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    ["domain of the service", "this is a valid identifier line", "- a\n- b", "42"],
    ids=["scalar-with-a-field-name", "scalar-containing-id", "sequence", "int"],
)
def test_non_mapping_frontmatter_does_not_reach_a_subscript(body):
    """``yaml.safe_load`` returns whatever the YAML says, not always a dict.

    ``---\\ndomain of the service\\n---`` parses to a plain string. Every
    consumer then subscripts it: ``if key in fm`` is a SUBSTRING test on a str
    and ``fm[key]`` raises ``TypeError``, reaching an unhandled 500 from
    ``validate_content``, ``score_content`` and ``extract_frontmatter_metadata``
    on caller-supplied content. ``or {}`` does not catch it -- a non-empty
    string is truthy.
    """
    from faultmaven.modules.knowledge.domain.services.runbook_validator import (
        RunbookValidator,
    )
    from faultmaven.utils.frontmatter import (
        extract_frontmatter_metadata,
        parse_frontmatter,
    )

    doc = f"---\n{body}\n---\n# Title\n\nSome prose.\n"

    assert parse_frontmatter(doc) == {}
    assert extract_frontmatter_metadata(doc) == {}
    RunbookValidator().validate_content(doc)  # must not raise
    _scorer().score_content(doc)  # must not raise


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
    from faultmaven.utils.frontmatter import match_frontmatter, parse_frontmatter

    lf = _runbook_corpus()[0].read_text(encoding="utf-8")
    crlf = lf.replace("\n", "\r\n")

    # Driven at `match_frontmatter`/`parse_frontmatter` ONLY, and that is now the
    # whole point of this test. It used to also assert on
    # `validate_content(crlf).errors` -- that "No YAML frontmatter found" was
    # absent and no `Missing required metadata field` fired. Those three
    # assertions went VACUOUS when #1403 made `validate_content` normalise line
    # endings at its first line: they cannot fail for a line-ending reason any
    # more, whatever `utils.frontmatter` does, so keeping them here would read as
    # coverage while guarding nothing. Mutation-checked -- narrowing `_DELIMITER`
    # back to `---[ \t]*\n` fails only the two calls below.
    #
    # Whole-document CRLF/LF equality, including the validator and the scorer, is
    # asserted across the corpus in `test_crlf_line_endings_1403.py`. This stays
    # as the frontmatter-grammar half: the one that fails if `\r?\n` is narrowed
    # again.
    assert match_frontmatter(crlf) is not None, "CRLF frontmatter stopped parsing"
    assert parse_frontmatter(crlf) == parse_frontmatter(lf)


# --------------------------------------------------------------------------
# The grammar has exactly one definition
# --------------------------------------------------------------------------


#: Three dashes NOT followed by `+`. The trailing `+` makes it a "one or more
#: dashes" quantifier, which is the HORIZONTAL RULE pattern
#: (`---+|\*\*\*+|___+`) -- a different grammar that legitimately contains three
#: dashes and has its own single definition in
#: `content_chunker.HR_SPLIT_BOUNDARY_RE`. Without the distinction this guard
#: fires on that pattern and gets suppressed by the next person to hit it,
#: which is how a guard stops guarding.
_DELIMITER_LITERAL = re.compile(r"-{3}(?!\+)")


def _is_delimiter_literal(literal: str) -> bool:
    return bool(_DELIMITER_LITERAL.search(literal))


def _inline_grammar_sites(path: pathlib.Path) -> list[str]:
    """Every ``re.*`` call in ``path`` whose pattern literal contains ``---``.

    Matched on the AST, not on source text. A first draft of this guard was a
    source regex anchored on ``r"^---`` and had a blind spot the exact shape of
    the bug it exists to prevent: it did not see
    ``_re.match(r"^(---\\s*\\n)(.*?)(\\n---\\s*\\n)", ...)``, a tenth copy that
    sat in the tree while the guard reported it clean. Nor would it have seen
    ``r'^---'`` in single quotes, an ``rf"..."`` prefix, or a pattern with no
    caret at all -- and ``re.match`` is already anchored, so omitting the caret
    is the natural thing for the next author to do.

    Looking at what is PASSED TO ``re`` rather than at how it is spelled
    removes all of those at once.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    sites = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        base = node.func.value
        if not isinstance(base, ast.Name) or base.id not in {"re", "_re"}:
            continue
        for arg in node.args[:1]:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                if _is_delimiter_literal(arg.value):
                    sites.append(f"{path.name}:{node.lineno}")
            elif isinstance(arg, ast.JoinedStr):  # an f-string pattern
                literal = "".join(
                    v.value
                    for v in arg.values
                    if isinstance(v, ast.Constant) and isinstance(v.value, str)
                )
                if _is_delimiter_literal(literal):
                    sites.append(f"{path.name}:{node.lineno}")
    return sites


def test_the_frontmatter_grammar_is_defined_in_exactly_one_place():
    """Nine copies of this regex drifted apart; that is the root defect.

    Four were fixed and six left, so a document's YAML was stripped by the
    chunker but counted as body by the validator, and the two disagreed about
    where the document started. A guard on the count is the only thing that
    stops the eleventh copy being written -- the fix itself does not.

    Scope is ``faultmaven/`` AND ``tests/``: a test helper carried a copy of
    this grammar and had already drifted from production, which is how a
    fixture came to assert on a document shape the code under test could no
    longer parse.
    """
    repo = pathlib.Path(__file__).resolve().parents[4]
    canonical = repo / "faultmaven" / "utils" / "frontmatter.py"
    assert canonical.exists(), f"canonical grammar missing at {canonical}"

    offenders = sorted(
        f"{p.relative_to(repo)}:{site.split(':')[1]}"
        for root in ("faultmaven", "tests", "scripts")
        for p in (repo / root).rglob("*.py")
        if p != canonical
        for site in _inline_grammar_sites(p)
    )

    assert offenders == [], (
        "frontmatter delimiter regex written inline instead of imported from "
        f"faultmaven.utils.frontmatter: {offenders}"
    )
