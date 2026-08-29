"""The persisted runbook id is non-empty, kebab, and distinct per input.

#1230 and #1243 are one decision — both are about what
``runbook_id_from_parts`` is allowed to emit into
``conversion_drafts.runbook_id`` and into runbook frontmatter — so they are
pinned together.

What each class establishes:

- ``TestTheEmptySlugBranch`` — #1230's reproducing case. A punctuation-only or
  non-latin ``(service, title)`` pair used to mint ``""``; two such runbooks
  persisted two rows sharing that id AND sharing
  ``item_id_from_runbook_id('')``.
- ``TestTheTruncationBranch`` — #1243. Truncation landing on a hyphen emitted a
  double hyphen, on 5 of the 91 shipped runbook titles.
- ``TestTheRealValidatorAcceptsEveryMintedId`` — the bar #1243 actually names:
  ``verify_draft`` rejects a draft on its id alone. Pinned by RUNNING
  ``RunbookValidator``, not by restating its regex, so a change to the
  validator's grammar surfaces here rather than passing a stale copy.
- ``TestWhatTheMintDeliberatelyDoesNotDo`` — the mint is a pure function and
  stays deterministic, so it cannot make two identically-slugging titles
  distinct. That is the database's job (migration 045), and stating it here
  keeps a future reader from "fixing" determinism away.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from faultmaven.modules.knowledge.domain.services.runbook_validator import (
    RunbookValidator,
)
from faultmaven.utils.runbook_id import (
    is_hash_only_runbook_id,
    item_id_from_runbook_id,
    runbook_id_from_parts,
)

pytestmark = pytest.mark.unit

#: Restated only for the corpus sweeps below. Every claim that MATTERS is made
#: against the real validator in ``TestTheRealValidatorAcceptsEveryMintedId``.
KEBAB = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

#: The shipped KB pack — the corpus #1243 measured against. Resolved from this
#: file rather than from the cwd so the assertion is about THIS checkout.
PACK_RUNBOOKS = (
    pathlib.Path(__file__).resolve().parents[3]
    / "resources"
    / "knowledge"
    / "pack"
    / "runbooks"
)

#: The exact five ids #1243 reported, verbatim from the issue body. Kept as
#: literals so the test fails if the *inputs* that produced them stop being in
#: the pack, rather than silently measuring a different corpus.
ISSUE_1243_BROKEN_IDS = [
    "mongodb-mongodb-replica-set-election-storms-disrupting--3cf5",
    "gitlab-ci-gitlab-ci-pipeline-failures-stuck-jobs-rules--c877",
    "golang-go-goroutine-and-memory-leaks-pprof-and-runtime--9b06",
    "kubernetes-kubernetes-statefulset-stuck-during-rolling--cbc4",
    "aws-kinesis-aws-kinesis-data-streams-high-iterator-age--95e7",
]


def _frontmatter(path: pathlib.Path) -> dict[str, str]:
    """The pack's YAML frontmatter, read the crude way on purpose.

    Only ``service`` and ``title`` are needed and both are plain scalars; a
    YAML dependency here would buy nothing and could disagree with what the
    real ingest reads.
    """
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}
    body = text.split("---", 2)[1]
    out: dict[str, str] = {}
    for line in body.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def _pack_pairs() -> list[tuple[str, str, str]]:
    """``(filename, service, title)`` for every shipped runbook."""
    pairs = []
    for path in sorted(PACK_RUNBOOKS.rglob("*.md")):
        meta = _frontmatter(path)
        pairs.append((path.name, meta.get("service", ""), meta.get("title", "")))
    return pairs


# ---------------------------------------------------------------------------
# #1230 — the empty-slug branch
# ---------------------------------------------------------------------------


class TestTheEmptySlugBranch:
    #: Pairs whose ``(service, title)`` contains no allowlisted character.
    DEGENERATE = [
        ("...", "???"),
        ("!!!", "___"),
        ("サービス", "データベース接続エラー"),
        ("сервис", "База данных недоступна"),
        ("🔥🚀", "💡"),
        ("", ""),
        ("／／", "／"),
    ]

    def test_no_degenerate_pair_mints_an_empty_id(self):
        for service, title in self.DEGENERATE:
            minted = runbook_id_from_parts(service, title)
            assert minted, f"({service!r}, {title!r}) still mints an empty id"
            assert KEBAB.match(minted), minted

    def test_degenerate_pairs_mint_DIFFERENT_ids(self):
        """The defect, stated exactly: two of them shared one id.

        Both the ``runbook_id`` and the derived ``item_id`` are checked —
        ``item_id_from_runbook_id('')`` was ``kb_e3b0c44298fc`` for every one of
        them, which is what made verify/approve unable to tell the rows apart.
        """
        ids = {}
        item_ids = {}
        for service, title in self.DEGENERATE:
            minted = runbook_id_from_parts(service, title)
            ids.setdefault(minted, []).append((service, title))
            item_ids.setdefault(item_id_from_runbook_id(minted), []).append(minted)

        collisions = {k: v for k, v in ids.items() if len(v) > 1}
        assert not collisions, f"distinct pairs still share an id: {collisions}"
        assert len(item_ids) == len(self.DEGENERATE), (
            "distinct runbook_ids collapsed to one derived item_id: "
            f"{ {k: v for k, v in item_ids.items() if len(v) > 1} }"
        )

    def test_the_mint_is_deterministic(self):
        """Not incidental — the disk scan reconciles a file to its row BY this
        id, so a random fallback (which is what ``draft_filename`` correctly
        uses for a FILENAME) would manufacture a phantom draft on every scan."""
        for service, title in self.DEGENERATE:
            first = runbook_id_from_parts(service, title)
            assert all(
                runbook_id_from_parts(service, title) == first for _ in range(5)
            ), f"({service!r}, {title!r}) is not deterministic"

    def test_a_readable_title_does_not_take_the_hash_branch(self):
        """Without this the fallback could swallow every id and still pass."""
        minted = runbook_id_from_parts("checkout-api", "Connection pool exhausted")
        assert minted == "checkout-api-connection-pool-exhausted"
        assert not is_hash_only_runbook_id(minted)
        assert is_hash_only_runbook_id(runbook_id_from_parts("...", "???"))


# ---------------------------------------------------------------------------
# #1243 — the truncation branch, measured against the real pack
# ---------------------------------------------------------------------------


class TestTheTruncationBranch:
    def test_the_pack_is_actually_present(self):
        """Guards every assertion below: an empty corpus passes them all."""
        pairs = _pack_pairs()
        assert len(pairs) == 91, f"expected the 91 shipped runbooks, got {len(pairs)}"
        assert all(s and t for _, s, t in pairs), "a shipped runbook lost its metadata"

    def test_every_shipped_title_mints_a_kebab_id(self):
        """#1243's measurement, inverted. Called the way the conversion paths
        call it — service AND title — because that is what produced 5/91."""
        broken = [
            (name, runbook_id_from_parts(service, title))
            for name, service, title in _pack_pairs()
            if not KEBAB.match(runbook_id_from_parts(service, title))
        ]
        assert (
            not broken
        ), f"{len(broken)}/91 shipped titles mint a non-kebab id: {broken}"

    def test_the_five_ids_the_issue_named_are_no_longer_produced(self):
        """Names the regression rather than only counting it.

        Each of the five is checked to be (a) gone, and (b) gone because its
        SOURCE is still in the pack — otherwise a runbook simply being deleted
        would read as a fix."""
        minted = {runbook_id_from_parts(s, t) for _, s, t in _pack_pairs()}
        still_broken = [i for i in ISSUE_1243_BROKEN_IDS if i in minted]
        assert not still_broken, still_broken

        # (b): the repaired id is the old one with the double hyphen collapsed,
        # so each broken id must have a surviving counterpart in the pack.
        for broken in ISSUE_1243_BROKEN_IDS:
            repaired = broken.replace("--", "-")
            assert repaired in minted, (
                f"{broken!r} is absent because its source runbook left the pack, "
                f"not because the mint was fixed ({repaired!r} not minted)"
            )

    def test_truncation_still_disambiguates(self):
        """The ``rstrip`` must not eat the disambiguator. Two long titles
        sharing a 55-character prefix have to stay distinct, or the fix for
        #1243 would have created #1230's collision at a different length."""
        prefix = "the same long prefix repeated over and over aaaa "
        a = runbook_id_from_parts("service", prefix + "a" * 40)
        b = runbook_id_from_parts("service", prefix + "b" * 40)
        assert a != b
        assert KEBAB.match(a) and KEBAB.match(b)

    def test_a_truncated_id_stays_within_the_bound(self):
        assert len(runbook_id_from_parts("svc", "x" * 500)) <= 60


# ---------------------------------------------------------------------------
# The bar the issue actually names: the validator's own verdict
# ---------------------------------------------------------------------------


RUNBOOK_TEMPLATE = """---
id: {runbook_id}
title: "A Title Long Enough To Not Warn"
domain: platform
service: svc
symptom_class: [availability]
scope: global
tags: []
difficulty: medium
severity: high
version: "1.0.0"
last_updated: "2026-08-29"
verified_by: ""
status: draft
---

# Runbook: A Title Long Enough To Not Warn

## Symptom Recognition
x

## Applicability
x

## Diagnostic Steps
x

## Causes
x

## Prevention
x

## Sources
- Manually authored runbook
"""


class TestTheRealValidatorAcceptsEveryMintedId:
    """#1243: "A draft carrying a non-kebab id fails ``verify_draft``'s
    validation on the id alone."

    So the pin runs the validator rather than a copy of its regex. Only id
    errors are inspected — the template is deliberately minimal and may draw
    other complaints; those are not this file's business.
    """

    @staticmethod
    def _id_errors(runbook_id: str) -> list[str]:
        result = RunbookValidator().validate_content(
            RUNBOOK_TEMPLATE.format(runbook_id=runbook_id)
        )
        return [e for e in result.errors if "ID must be" in e or "field: id" in e]

    def test_the_validator_would_have_rejected_the_old_ids(self):
        """Establishes the test can fail. Without it, a validator that accepted
        everything would make every assertion below vacuous."""
        assert self._id_errors("")
        assert self._id_errors(ISSUE_1243_BROKEN_IDS[0])

    def test_every_degenerate_pair_now_passes_the_id_gate(self):
        for service, title in TestTheEmptySlugBranch.DEGENERATE:
            minted = runbook_id_from_parts(service, title)
            assert not self._id_errors(minted), (minted, self._id_errors(minted))

    def test_every_shipped_title_now_passes_the_id_gate(self):
        offenders = []
        for name, service, title in _pack_pairs():
            minted = runbook_id_from_parts(service, title)
            errors = self._id_errors(minted)
            if errors:
                offenders.append((name, minted, errors))
        assert not offenders, offenders


# ---------------------------------------------------------------------------
# What the mint deliberately does NOT do
# ---------------------------------------------------------------------------


class TestWhatTheMintDeliberatelyDoesNotDo:
    def test_a_surviving_service_keeps_the_hash_branch_from_firing(self):
        """The shape that LOOKS like it should have been covered by #1230.

        ``("redis", "!!!")`` and ``("redis", "???")`` both mint ``"redis"``:
        the hash branch keys on the WHOLE pair filtering to nothing, and here
        the service survives. Extending it to "the title contributed nothing"
        would re-mint ids that are valid today, which is the one thing this
        change may not do — so this is contained by the 045 index and its 409
        (``tests/integration/modules/knowledge/test_runbook_id_conflict.py``),
        not by the mint.

        Executed rather than left as a docstring claim, because a future reader
        chasing "make the mint unique" needs to hit the consequence.
        """
        assert (
            runbook_id_from_parts("redis", "!!!")
            == runbook_id_from_parts("redis", "???")
            == "redis"
        )
        # Still valid, still deterministic — degraded, not broken.
        assert KEBAB.match(runbook_id_from_parts("redis", "!!!"))

    def test_a_none_part_is_normalised_to_the_empty_string(self):
        """``service: str | None`` is a promise the mint has to keep.

        The f-string used to stringify ``None`` into the literal ``"none"``, so
        three punctuation-only titles under a ``None`` service all minted
        ``"none"`` — #1230's collision reintroduced through the one input the
        signature admits and the empty-slug branch cannot see. No call site
        passes ``None`` today, so nothing persisted is affected; the type hint
        is what is being repaired.
        """
        ids = [runbook_id_from_parts(None, t) for t in ("!!!", "???", "。。。")]
        assert len(set(ids)) == 3, ids
        assert all(i and KEBAB.match(i) for i in ids), ids
        assert all(is_hash_only_runbook_id(i) for i in ids), ids
        # None and "" are now indistinguishable, which is the whole claim.
        assert runbook_id_from_parts(None, "Redis OOM") == "redis-oom"
        assert runbook_id_from_parts("svc", None) == "svc"
        assert runbook_id_from_parts(None, None) == runbook_id_from_parts("", "")

    def test_two_titles_that_slug_identically_still_share_an_id(self):
        """Not a leftover defect — the documented boundary.

        The mint is a pure function of ``(service, title)`` and MUST stay
        deterministic (the disk scan reconciles a file to its row by this id),
        so it cannot see the other rows and cannot make these distinct.
        Uniqueness is enforced where the other rows ARE visible: the partial
        unique index on ``(organization_id, runbook_id)`` from migration 045,
        exercised in ``tests/integration/test_alembic_migrations.py``.

        If someone later makes the mint globally unique, this test fails and
        they are made to notice that they have also broken scan reconciliation.
        """
        assert runbook_id_from_parts("svc", "Foo Bar") == runbook_id_from_parts(
            "svc", "foo-bar"
        )

    def test_is_hash_only_is_a_signal_not_a_guard(self):
        """Its docstring says a real title can match. Pinned so a caller does
        not start treating it as proof of anything."""
        assert is_hash_only_runbook_id(runbook_id_from_parts("", "Runbook 3fa2b1c0"))
        assert not is_hash_only_runbook_id("")
        assert not is_hash_only_runbook_id(None)
        assert not is_hash_only_runbook_id("runbook-3fa2b1")  # 6 hex, not 8
