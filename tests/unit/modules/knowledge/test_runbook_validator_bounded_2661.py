"""The runbook validator's near-duplicate check and per-item refusals are bounded
(#2661 sub-stage 3b0, #2690).

- The near-duplicate check finds exactly the all-pairs loop's pairs, in its order:
  a differential against that loop, copied from ``e1451bc85``. The first 20 pairs
  keep their warning; one more warning counts the rest.
- A frontmatter refusal names each distinct invalid value once, at most ten, each
  clipped to 80 characters, then counts the rest. Its size no longer grows with
  the item count or with a value a YAML alias repeats.

The same cases are in ``faultmaven-kb-toolkit``'s suite
(``tests/unit/test_validator_bounded_2661.py``), whose near-duplicate block is a
byte copy of this one.
"""

import random
import re
from typing import Dict, List

import pytest

from faultmaven.modules.knowledge.domain.services.runbook_validator import (
    RunbookValidator,
    check_cause_statement_invariants,
    validate_and_score,
)
from faultmaven.modules.knowledge.taxonomy import SymptomClass, render_vocabulary
from tests.runbook_samples import valid_runbook

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# The reference: main's all-pairs loop, copied from e1451bc85
# ---------------------------------------------------------------------------


def _ref_norm(s: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", s.lower()))


def _ref_jaccard(a: str, b: str) -> float:
    ta = set(re.findall(r"[a-z0-9]+", a.lower()))
    tb = set(re.findall(r"[a-z0-9]+", b.lower()))
    return len(ta & tb) / len(ta | tb) if (ta | tb) else 0.0


def _reference(statements: List[tuple]) -> tuple[List[str], List[str]]:
    """``check_cause_statement_invariants`` at ``e1451bc85``: every pair named."""
    errors: List[str] = []
    warnings: List[str] = []
    for letter, stmt in statements:
        if re.search(r"\[step\s*\d+\]", stmt or "", re.IGNORECASE):
            errors.append(
                f"Cause {letter}: Statement contains an operator-step marker "
                f"([Step N]). The Statement is the symptom-level match surface — "
                f"describe the observable condition; keep step references in Indicators."
            )
    norms = [(letter, _ref_norm(stmt)) for letter, stmt in statements]
    groups: Dict[str, List[str]] = {}
    for letter, norm in norms:
        if norm:
            groups.setdefault(norm, []).append(letter)
    for letters in groups.values():
        if len(letters) > 1:
            errors.append(
                f"Causes {', '.join(letters)} have identical Statements; sibling "
                f"causes must be mutually discriminative (MECE) or the matcher abstains."
            )
    for i in range(len(statements)):
        li, si = statements[i]
        if not norms[i][1]:
            continue
        for j in range(i + 1, len(statements)):
            lj, sj = statements[j]
            if not norms[j][1] or norms[i][1] == norms[j][1]:
                continue
            if _ref_jaccard(si, sj) >= 0.6:
                warnings.append(
                    f"Causes {li} and {lj} have near-duplicate Statements (lexical "
                    "overlap >= 60%); make them "
                    "discriminative or holistic matching may return 'multiple'."
                )
    return errors, warnings


def _capped(warnings: List[str]) -> List[str]:
    """The reference's warnings as the cap shows them."""
    more = len(warnings) - 20
    if more <= 0:
        return warnings
    noun = "pair" if more == 1 else "pairs"
    return warnings[:20] + [
        f"And {more} more {noun} of causes have near-duplicate Statements."
    ]


_POOL = ["disk", "full", "pool", "idle", "lock", "İo", "straße", "x9", "a", "b"]
_PUNCT = ["", ".", ",", "!", " ...", "?"]


def _statement(rng: random.Random, words: List[str]) -> str:
    roll = rng.random()
    if roll < 0.05:
        return ""
    if roll < 0.08:
        return rng.choice(["...", "???", " - "])
    picked = [rng.choice(words) for _ in range(rng.randint(1, 6))]  # repeats too
    picked = [w.upper() if rng.random() < 0.2 else w for w in picked]
    return " ".join(picked) + rng.choice(_PUNCT)


def _lists(seed: int, count: int) -> List[List[tuple]]:
    rng = random.Random(seed)
    out = []
    for _ in range(count):
        words = rng.sample(_POOL, rng.randint(2, 8))
        stmts = [_statement(rng, words) for _ in range(rng.randint(0, 24))]
        if stmts and rng.random() < 0.2:
            stmts.append(rng.choice(stmts))  # an exact duplicate
        out.append([(f"C{n}", s) for n, s in enumerate(stmts)])
    return out


# ---------------------------------------------------------------------------
# The near-duplicate check
# ---------------------------------------------------------------------------


class TestNearDuplicateDifferential:
    def test_two_thousand_seeded_lists_match_the_all_pairs_loop(self):
        """Mutation: slice each posting list past ``i + 1`` (dropping the pair
        with the next Statement), or compare ``>`` for ``>=``, and this fails."""
        over_cap = with_pairs = 0
        for statements in _lists(seed=2661, count=2000):
            ref_errors, ref_warnings = _reference(statements)
            errors, warnings = check_cause_statement_invariants(statements)
            assert errors == ref_errors, statements
            assert warnings == _capped(ref_warnings), statements
            with_pairs += bool(ref_warnings)
            over_cap += len(ref_warnings) > 20
        # Positive controls: the corpus reaches the threshold and the cap.
        assert with_pairs > 200 and over_cap > 20

    def test_seeded_documents_match_through_the_validator(self):
        """The same check as the validator runs it over a document's Causes."""
        with_pairs = 0
        for statements in _lists(seed=2690, count=200):
            statements = [
                (chr(ord("A") + n), s.strip())
                for n, (_, s) in enumerate(statements[:25])
            ]
            content = "## Causes\n\n" + "".join(
                f"### Cause {letter}: name\n**Statement:** {stmt}\n\n"
                for letter, stmt in statements
            )
            errors: List[str] = []
            warnings: List[str] = []
            RunbookValidator()._validate_cause_statements(content, errors, warnings)
            ref_errors, ref_warnings = _reference([s for s in statements if s[1]])
            assert errors == ref_errors
            assert warnings == _capped(ref_warnings)
            with_pairs += bool(ref_warnings)
        assert with_pairs > 20


def _groups(*sizes: int) -> List[tuple]:
    """Groups of Statements ``<g> <g> <g> <n>``: every pair inside a group is near
    (3/5), no pair across groups is."""
    statements, n = [], 0
    for g, size in enumerate(sizes):
        for _ in range(size):
            statements.append((f"C{n}", f"g{g} h{g} k{g} n{n}"))
            n += 1
    return statements


class TestNearDuplicateCap:
    @pytest.mark.parametrize(
        "sizes,pairs,last",
        [
            ((6, 3, 2, 2), 20, None),
            ((7,), 21, "And 1 more pair of causes have near-duplicate Statements."),
            (
                (7, 3, 2),
                25,
                "And 5 more pairs of causes have near-duplicate Statements.",
            ),
        ],
    )
    def test_twenty_pairs_are_named_and_the_rest_counted(self, sizes, pairs, last):
        statements = _groups(*sizes)
        _, ref_warnings = _reference(statements)
        assert len(ref_warnings) == pairs
        _, warnings = check_cause_statement_invariants(statements)
        assert warnings[:20] == ref_warnings[:20]
        assert warnings[20:] == ([last] if last else [])

    def test_the_answer_stops_growing_with_the_pairs(self):
        _, warnings = check_cause_statement_invariants(_groups(300))
        assert len(warnings) == 21
        assert warnings[-1] == (
            "And 44830 more pairs of causes have near-duplicate Statements."
        )


# ---------------------------------------------------------------------------
# Frontmatter refusals (#2690)
# ---------------------------------------------------------------------------

_OFF_VOCAB = (
    "Invalid symptom_class '{}'. Must be one of the controlled vocabulary "
    f"({render_vocabulary(SymptomClass)}), or move it to `tags` if it is a "
    "long-tail symptom."
)


def _with(fields: Dict[str, str], prefix: str = "") -> str:
    """The sample runbook with frontmatter lines replaced, ``prefix`` lines first."""
    head, body = valid_runbook().split("\n---\n", 1)
    lines = [ln for ln in head.splitlines() if ln.split(":")[0] not in fields]
    lines[1:1] = prefix.splitlines()
    lines += [f"{field}: {value}" for field, value in fields.items()]
    return "\n".join(lines) + "\n---\n" + body


def _errors(content: str, needle: str) -> List[str]:
    return [
        e for e in RunbookValidator().validate_content(content).errors if needle in e
    ]


def _flow(values: List[str]) -> str:
    return "[" + ", ".join(values) + "]"


class TestSymptomClassRefusals:
    @pytest.mark.parametrize(
        "value,sentence",
        [
            ("bogus", _OFF_VOCAB.format("bogus")),
            (
                "Bogus",
                "symptom_class must be lowercase with hyphens/underscores: Bogus",
            ),
            ("1", "symptom_class items must be strings: 1"),
        ],
        ids=["off-vocabulary", "not-lowercase", "not-a-string"],
    )
    def test_ten_copies_and_ten_thousand_give_one_error(self, value, sentence):
        """Mutation: append one error per item, as ``e1451bc85`` did, and the
        10,000-copy answer is 10,000 errors."""
        for copies in (1, 10, 10_000):
            content = _with({"symptom_class": _flow([value] * copies)})
            assert _errors(content, "symptom_class") == [sentence], copies

    def test_one_off_vocabulary_value_keeps_its_sentence(self):
        assert _errors(_with({"symptom_class": "[split_brain]"}), "symptom_class") == [
            _OFF_VOCAB.format("split_brain")
        ]

    def test_values_keep_their_branch_and_item_order(self):
        content = _with({"symptom_class": "[latency, 7, Bad, bogus, 7, Bad, bogus]"})
        assert _errors(content, "symptom_class") == [
            "symptom_class items must be strings: 7",
            "symptom_class must be lowercase with hyphens/underscores: Bad",
            _OFF_VOCAB.format("bogus"),
        ]

    def test_eleven_distinct_values_name_ten_and_count_one(self):
        values = [f"bogus{n}" for n in range(11)]
        errors = _errors(_with({"symptom_class": _flow(values)}), "symptom_class")
        assert errors == [_OFF_VOCAB.format(v) for v in values[:10]] + [
            "And 1 more invalid symptom_class values."
        ]

    def test_a_long_value_keeps_its_sentence_clipped(self):
        long = "x" * 200
        assert _errors(_with({"symptom_class": f"[{long}]"}), "symptom_class") == [
            _OFF_VOCAB.format("x" * 77 + "...")
        ]

    def test_unhashable_items_are_refused_without_raising(self):
        errors = _errors(
            _with({"symptom_class": "[[a], {b: c}, [a]]"}), "symptom_class"
        )
        assert errors == [
            "symptom_class items must be strings: ['a']",
            "symptom_class items must be strings: {'b': 'c'}",
        ]

    def test_an_alias_to_a_huge_list_gives_a_bounded_refusal(self):
        """``e1451bc85`` rendered the aliased list whole once per item: 300
        aliases to 20,000 items answered about 30 MB."""
        prefix = "x-big: &big " + _flow(["aa"] * 20_000)
        content = _with({"symptom_class": _flow(["*big"] * 300)}, prefix)
        (error,) = _errors(content, "symptom_class")
        assert error == (
            "symptom_class items must be strings: "
            "['aa', 'aa', 'aa', 'aa', 'aa', 'aa', ...]"
        )


class TestSiblingRefusals:
    def test_an_aliased_scalar_vocabulary_value_is_clipped(self):
        prefix = "x-big: &big " + _flow(["aa"] * 20_000)
        (error,) = _errors(_with({"severity": "*big"}, prefix), "Invalid severity")
        assert error.startswith(
            "Invalid severity '['aa', 'aa', 'aa', 'aa', 'aa', 'aa', ...]'. "
        )

    def test_a_long_scalar_vocabulary_value_is_clipped(self):
        (error,) = _errors(_with({"severity": "y" * 200}), "Invalid severity")
        assert error.startswith(f"Invalid severity '{'y' * 77}...'. Must be one of:")

    def test_a_short_scalar_value_renders_as_before(self):
        # A YAML date renders by ``str``, as on ``e1451bc85``, not by ``repr``.
        (error,) = _errors(_with({"severity": "2026-01-01"}), "Invalid severity")
        assert error.startswith("Invalid severity '2026-01-01'. Must be one of:")

    def test_tags_name_ten_distinct_values_and_count_the_rest(self):
        """Mutation: append one error per tag and 20,000 aliased copies answer
        20,000 errors."""
        values = [f"Bad{n}" for n in range(12)]
        prefix = "x-big: &big " + _flow(values * 2_000)
        errors = RunbookValidator().validate_content(_with({"tags": "*big"}, prefix))
        refused = [e for e in errors.errors if "tags" in e or e.startswith("Tag ")]
        assert refused == [
            f"Tag must be lowercase alphanumeric with hyphens: {v}" for v in values[:10]
        ] + ["And 2 more invalid tags."]


class TestScore:
    """The score subtracts 10 per error, so it is ``e1451bc85``'s unless values
    repeat. The pinned figures were captured from ``e1451bc85``."""

    def test_distinct_invalid_values_score_as_before(self):
        validation, score = validate_and_score(_with({"symptom_class": "[aa, bb, cc]"}))
        assert len(validation.errors) == 3
        assert (score.overall, score.grade) == (70.2, "C")

    def test_a_repeated_value_scores_higher_than_before(self):
        # e1451bc85 gave [aa, aa, aa] three errors and 70.2/C; one error now.
        validation, score = validate_and_score(_with({"symptom_class": "[aa, aa, aa]"}))
        assert len(validation.errors) == 1
        assert (score.overall, score.grade) == (76.2, "C")
