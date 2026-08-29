"""The persisted-runbook-id mint is byte-identical to the copies it replaced.

#1213's review recorded that the slug rule lived in three places. Two of them —
``conversion.generate_runbook_id`` and
``ConversionService.create_runbook_from_template`` — mint a value that is
written to ``conversion_drafts.runbook_id`` and into runbook frontmatter, so a
behaviour change there does not just alter future ids, it orphans rows that
already exist: the disk scan reconciles a file against a row by that id, and a
re-minted id makes an existing draft look like a new one.

Consolidation was therefore conditional on proving it changes nothing. Both
originals are frozen verbatim below and compared against
``runbook_id_from_parts`` over a corpus that includes every shape the two
implementations could plausibly disagree on. Freezing the source rather than
citing it is the point: if someone "improves" the shared helper, these fail.

The third place — ``runbook_filename`` / ``safe_path_component`` — is NOT
consolidated onto the same policy, and the last class here pins why by
executing the divergence.
"""

from __future__ import annotations

import hashlib
import random
import re

import pytest

from faultmaven.modules.knowledge.domain.models.conversion import (
    FailureModeAnalysis,
    generate_runbook_id,
)
from faultmaven.utils.runbook_id import (
    runbook_filename,
    runbook_id_from_parts,
    safe_path_component,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# The two implementations as they stood at aab5f34, frozen verbatim
# ---------------------------------------------------------------------------


def _original_generate_runbook_id(service, title):
    """``conversion.generate_runbook_id`` before the consolidation."""
    base = f"{service}-{title}"
    slug = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")
    if len(slug) > 60:
        suffix = hashlib.md5(slug.encode(), usedforsecurity=False).hexdigest()[:4]
        slug = slug[:55] + "-" + suffix
    return slug


def _original_create_runbook(service_name, title):
    """``ConversionService.create_runbook_from_template`` before the
    consolidation. Kept separate even though it is character-for-character the
    same expression — that the two copies agreed was an assumption too, and
    this is where it stops being one."""
    base = f"{service_name}-{title}"
    runbook_id = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")
    if len(runbook_id) > 60:
        suffix = hashlib.md5(runbook_id.encode(), usedforsecurity=False).hexdigest()[:4]
        runbook_id = runbook_id[:55] + "-" + suffix
    return runbook_id


# ---------------------------------------------------------------------------
# The corpus
# ---------------------------------------------------------------------------

EXPLICIT_CASES = [
    # Ordinary
    ("checkout-api", "Database connection pool exhausted"),
    ("auth", "500s on login"),
    # Leading / trailing separators, which ``.strip("-")`` governs
    ("---svc---", "---title---"),
    ("  svc  ", "  title  "),
    ("/svc/", "/title/"),
    # Punctuation runs, which the collapse governs
    ("svc!!!", "a...b,,,c;;;d"),
    ("svc", "-" * 40),
    ("svc", "_" * 40),
    # Empty results — the case #1215's filename fallback had to handle, and
    # the case the id policy deliberately does NOT
    ("", ""),
    ("...", "..."),
    ("!!!", "???"),
    # Unicode: non-latin scripts, combining marks, separator look-alikes,
    # emoji, and the FULLWIDTH SOLIDUS the filename docstring names
    ("сервис", "База данных недоступна"),
    ("サービス", "データベース接続エラー"),
    ("svc", "café" + "́"),
    ("svc", "／／etc／pwned"),
    ("svc", "🔥🚀 outage 💡"),
    ("svc", "ＡＢＣ"),
    # Traversal shapes
    ("../../..", "../../../etc/passwd"),
    ("svc", "..\\..\\windows"),
    ("svc", "a\x00b"),
    # Exactly at, one under, and one over the 60-char boundary
    ("s", "x" * 58),
    ("s", "x" * 59),
    ("s", "x" * 60),
    ("s", "x" * 61),
    ("s", "x" * 500),
    # Two long titles sharing a 60-char prefix — the case the md5
    # disambiguator exists for
    ("service", "the same long prefix repeated over and over aaaa " + "a" * 40),
    ("service", "the same long prefix repeated over and over aaaa " + "b" * 40),
    # None, which the f-string stringifies rather than rejecting
    (None, "title"),
    ("svc", None),
    (None, None),
]


def _random_corpus(n=2000, seed=20260828):
    """Deterministic pseudo-random corpus over the alphabet that matters:
    allowlisted characters, separators, punctuation, whitespace, control
    bytes, and multi-byte scripts."""
    rng = random.Random(seed)
    alphabet = (
        "abcdefghijklmnopqrstuvwxyz"
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "0123456789"
        "-_. /\\:;,!?*'\"()[]{}@#$%^&+=|~`"
        "\t\n\r\x00"
        "éüñçöàЖДйДしテスト中文한글"
        "／⁄∕"
        "🔥🚀"
    )
    cases = []
    for _ in range(n):
        service = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 30)))
        title = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 90)))
        cases.append((service, title))
    return cases


CORPUS = EXPLICIT_CASES + _random_corpus()


# ---------------------------------------------------------------------------
# The differential
# ---------------------------------------------------------------------------


class TestTheSharedMintIsByteIdenticalToBothOriginals:
    def test_every_corpus_input_agrees(self):
        mismatches = []
        for service, title in CORPUS:
            new = runbook_id_from_parts(service, title)
            for name, old in (
                ("generate_runbook_id", _original_generate_runbook_id(service, title)),
                ("create_runbook", _original_create_runbook(service, title)),
            ):
                if new != old:
                    mismatches.append((name, service, title, old, new))
        assert not mismatches, (
            f"{len(mismatches)} of {len(CORPUS)} inputs changed the PERSISTED id. "
            f"First five: {mismatches[:5]}"
        )

    def test_the_public_entry_point_agrees_too(self):
        """Through ``generate_runbook_id`` itself, not just the extracted
        expression — the delegation is part of what is being pinned."""
        for service, title in CORPUS[:200]:
            fm = FailureModeAnalysis(
                id="fm_1",
                title="" if title is None else str(title),
                domain="platform",
                service="" if service is None else str(service),
                symptom_class=["availability"],
                severity="high",
                symptoms_summary="s",
                resolution_summary="r",
            )
            assert generate_runbook_id(fm) == _original_generate_runbook_id(
                fm.service, fm.title
            )

    def test_the_corpus_actually_exercises_the_branches(self):
        """A differential over inputs that all take one branch proves nothing.

        Both the truncation branch and the empty-result branch must be reached,
        and the corpus must contain inputs the transform actually changes."""
        ids = [runbook_id_from_parts(s, t) for s, t in CORPUS]
        assert any(len(i) == 60 for i in ids), "truncation branch never reached"
        assert any(i == "" for i in ids), "empty-result branch never reached"
        assert any(
            runbook_id_from_parts(s, t) != f"{s}-{t}" for s, t in CORPUS
        ), "no input was actually transformed"


class TestTheFilenamePolicyIsDeliberatelyDifferent:
    """Why the third site was NOT folded in.

    ``safe_path_component`` and ``runbook_id_from_parts`` share the character
    rule (``_slug``) and nothing else. Merging the two *policies* is what would
    change persisted ids, so the divergence is pinned here rather than left as
    a comment that a future edit can quietly contradict.
    """

    def test_over_length_diverges(self):
        long_title = "x" * 200
        assert len(runbook_id_from_parts("svc", long_title)) == 60
        # The id keeps a disambiguator so two long titles sharing a prefix stay
        # distinct; the path component simply truncates, because collisions
        # there are resolved by the id suffix ``runbook_filename`` appends.
        a = runbook_id_from_parts("svc", "same prefix " * 6 + "aaa")
        b = runbook_id_from_parts("svc", "same prefix " * 6 + "bbb")
        assert a != b
        assert safe_path_component("same-prefix-" * 20)[:60] == safe_path_component(
            "same-prefix-" * 30
        )

    def test_empty_result_diverges(self):
        # An id with no allowlisted characters is empty. Substituting a
        # placeholder here would re-mint ids that already exist in
        # ``conversion_drafts``.
        assert runbook_id_from_parts("...", "???") == ""
        # An empty PATH component is a write to the parent directory, so it
        # must never be empty.
        assert safe_path_component("...") == "unknown"
        assert runbook_filename("...", "...").endswith(".md")
        assert runbook_filename("...", "...") != ".md"
