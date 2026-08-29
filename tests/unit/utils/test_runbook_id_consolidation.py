"""The persisted-runbook-id mint changed EXACTLY where it had to, nowhere else.

#1213's review recorded that the slug rule lived in three places. Two of them —
``conversion.generate_runbook_id`` and
``ConversionService.create_runbook_from_template`` — mint a value that is
written to ``conversion_drafts.runbook_id`` and into runbook frontmatter, so a
behaviour change there does not just alter future ids, it orphans rows that
already exist: the disk scan reconciles a file against a row by that id, and a
re-minted id makes an existing draft look like a new one.

Consolidation was therefore conditional on proving it changed nothing, and this
file proved it: both originals frozen verbatim, compared against
``runbook_id_from_parts`` over a corpus covering every shape the two
implementations could plausibly disagree on.

#1230/#1243 then changed the mint on purpose — its empty and double-hyphen
branches emitted ids the runbook validator rejects. That does NOT retire the
differential, it **narrows** it, and narrowing is where its value now is: the
frozen originals stay, and the claim becomes

    for every input, either the new id is byte-identical to what the originals
    produced, or the originals produced an id that was ALREADY INVALID and the
    new one is valid.

An unintended drift still fails, because it would change an id that was fine.
A vacuous version of that claim would also pass, so
``test_both_deliberate_divergences_are_actually_exercised`` requires the
divergence set to be non-empty and to contain both classes.

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
    is_hash_only_runbook_id,
    runbook_filename,
    runbook_id_from_parts,
    safe_path_component,
)

pytestmark = pytest.mark.unit

#: The grammar ``RunbookValidator`` enforces on frontmatter ``id``. Restated
#: here only to CLASSIFY the frozen originals' output; that the LIVE mint
#: satisfies the REAL validator is pinned behaviourally, by running it, in
#: ``tests/unit/utils/test_runbook_id_uniqueness.py``.
_KEBAB = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def _was_already_invalid(old_id: str) -> bool:
    return not _KEBAB.match(old_id)


def _carries_a_none_part(service, title) -> bool:
    """The SECOND deliberate divergence class, and the only other one.

    The originals let the f-string stringify ``None`` into the literal
    ``"none"``, so ``(None, "!!!")``, ``(None, "???")`` and ``(None, "。。。")``
    all minted ``"none"`` — #1230's collision, reachable through the one input
    the signature admits but the empty-slug branch cannot see (the slug is not
    empty, it is ``"none"``). ``None`` is now normalised to ``""``.

    Those ids are re-minted even though some were valid kebab
    (``"none-title"`` → ``"title"``). That is affordable for a reason that must
    be RE-CHECKED, not assumed, if a call site is ever added: no caller can
    pass ``None``. All four supply a ``str`` — ``conversion_service.py``'s
    ``service_name: str`` / ``title: str``, ``conversion.py``'s
    ``FailureModeAnalysis.service: str = "unknown"`` / ``title: str``,
    ``suggestion_service.py``'s literal ``"case"``, and its
    ``service if isinstance(service, str) else ""`` coercion — so no persisted
    id can have been minted this way.
    """
    return service is None or title is None


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


class TestTheSharedMintOnlyChangedIdsThatWereAlreadyInvalid:
    def test_no_corpus_input_re_mints_a_valid_id(self):
        """The load-bearing claim of #1230/#1243: nothing usable was re-minted.

        A drift that alters an id the originals produced VALIDLY lands here,
        whatever its motivation — those are the rows in ``conversion_drafts``
        that would be orphaned."""
        regressions = []
        for service, title in CORPUS:
            new = runbook_id_from_parts(service, title)
            for name, old in (
                ("generate_runbook_id", _original_generate_runbook_id(service, title)),
                ("create_runbook", _original_create_runbook(service, title)),
            ):
                if new == old:
                    continue
                if _was_already_invalid(old) or _carries_a_none_part(service, title):
                    continue
                regressions.append((name, service, title, old, new))
        assert not regressions, (
            f"{len(regressions)} of {len(CORPUS)} inputs re-minted an ALREADY "
            f"VALID id. First five: {regressions[:5]}"
        )

    def test_the_none_exemption_is_exactly_three_inputs_wide(self):
        """The exemption in ``_carries_a_none_part`` must not be a blank cheque.

        It is claimed on the grounds that no call site can pass ``None``, so it
        has to stay confined to inputs that actually carry one — and it has to
        be REACHED, or the exemption is dead code hiding nothing."""
        exempted = [
            (s, t)
            for s, t in CORPUS
            if runbook_id_from_parts(s, t) != _original_generate_runbook_id(s, t)
            and not _was_already_invalid(_original_generate_runbook_id(s, t))
        ]
        assert exempted, "the None divergence was never reached"
        assert all(_carries_a_none_part(s, t) for s, t in exempted), exempted
        assert set(exempted) == {(None, "title"), ("svc", None), (None, None)}, exempted

    def test_a_none_part_now_behaves_exactly_like_an_empty_string(self):
        """What the normalisation is FOR — and the collision it removes."""
        assert runbook_id_from_parts(None, "Redis OOM") == runbook_id_from_parts(
            "", "Redis OOM"
        )
        assert runbook_id_from_parts("svc", None) == runbook_id_from_parts("svc", "")
        ids = [runbook_id_from_parts(None, t) for t in ("!!!", "???", "。。。")]
        assert len(set(ids)) == 3, ids
        assert all(_KEBAB.match(i) for i in ids), ids
        # The originals collapsed all three onto the literal "none".
        assert {
            _original_generate_runbook_id(None, t) for t in ("!!!", "???", "。。。")
        } == {"none"}

    def test_every_divergence_replaces_an_invalid_id_with_a_valid_one(self):
        """The other direction: where it did change, it changed for the better.

        A "fix" that swapped one invalid id for another invalid one would pass
        the test above and fail this one."""
        bad = []
        for service, title in CORPUS:
            new = runbook_id_from_parts(service, title)
            old = _original_generate_runbook_id(service, title)
            if new != old and not _KEBAB.match(new):
                bad.append((service, title, old, new))
        # Not only the divergences: EVERY id the mint produces is kebab now.
        bad += [
            (s, t, None, runbook_id_from_parts(s, t))
            for s, t in CORPUS
            if not _KEBAB.match(runbook_id_from_parts(s, t))
        ]
        assert not bad, f"divergences that are still not kebab: {bad[:5]}"

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
            old = _original_generate_runbook_id(fm.service, fm.title)
            new = generate_runbook_id(fm)
            # ``FailureModeAnalysis`` coerces both parts to ``str``, so the
            # None exemption cannot apply here and is deliberately not offered.
            assert new == old or _was_already_invalid(old), (fm.service, fm.title, old)
            assert _KEBAB.match(new), new

    def test_both_deliberate_divergences_are_actually_exercised(self):
        """Without this, the two tests above hold vacuously.

        The corpus must reach the truncation branch, the empty-slug branch, AND
        the sub-case of truncation that lands on a hyphen — the one #1243
        measured. A corpus that stopped reaching them would silently turn this
        file into a differential over nothing."""
        pairs = [
            (s, t, runbook_id_from_parts(s, t), _original_generate_runbook_id(s, t))
            for s, t in CORPUS
        ]
        assert any(
            len(old) == 60 for _, _, _, old in pairs
        ), "truncation branch never reached"
        assert any(old == "" for _, _, _, old in pairs), "empty branch never reached"
        assert any(
            is_hash_only_runbook_id(new) for _, _, new, _ in pairs
        ), "the hash-only replacement for an empty slug was never minted"
        assert any(
            "--" in old and new != old for _, _, new, old in pairs
        ), "the double-hyphen truncation #1243 measured was never reached"
        assert any(
            new != old for _, _, new, old in pairs
        ), "no input diverged at all — the mint fix is not in this tree"


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
        # Neither is empty any more (#1230), but the substitutes differ in the
        # property that matters. An ID must be DISTINCT per input, because two
        # drafts sharing one are indistinguishable to verify/approve — so its
        # substitute is derived from the input.
        a = runbook_id_from_parts("...", "???")
        b = runbook_id_from_parts("!!!", "___")
        assert a and b and a != b, (a, b)
        # A PATH component only has to be A safe segment; a collision there is
        # resolved by the id suffix ``runbook_filename`` appends, so a fixed
        # SHARED literal is correct and more readable.
        assert safe_path_component("...") == safe_path_component("???") == "unknown"
        assert runbook_filename("...", "...").endswith(".md")
        assert runbook_filename("...", "...") != ".md"
