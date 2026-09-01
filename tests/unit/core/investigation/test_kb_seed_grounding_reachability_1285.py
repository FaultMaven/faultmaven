"""#1285 — what the seeding gate may ground on, measured on recorded corpus data.

#1272 gave the gate two grounds: the query NAMED the runbook, or a chunk COVERED
the query at ``term_coverage >= 0.90``. The pin that shipped with it constructed
a coverage of ``threshold - 0.01`` and checked the comparison — which proves the
operator works and cannot see whether any real chunk reaches the bar, because the
fixture supplies the very number whose reachability is in question.

This file replaces that shape. Every coverage value here is RECOMPUTED by the
production code from a real chunk of the shipped KB pack and the real document
frequencies of the corpus it was retrieved from; nothing is placed on the
right-hand side of the comparison under test. What it found, and what it pins:

``term_coverage`` is a share OF THE QUERY, so it is maximised by queries that
identify least. "The application is slow." reaches 1.000 against eight runbooks
at once, seven of which it names nothing of — above the 0.926 the best correct
symptom statement reaches against its own runbook. The ordering is wrong, so no threshold repairs it: any
bar at or below 0.926 admits both, and any bar above it admits ONLY the
content-free query. 0.90 sat in the first regime.

Measured over 226 queries / 1296 pairs against the shipped pack, the covers arm
decided 37 of the 722 pairs the names arm left undecided — 1 on-domain and 36
off-domain — and fired 0 times in 1026 pairs over 178 real ``case.description``
narratives, which is the only distribution this gate serves. It was removed.

The fixture is a recording, and is regenerated as described in
``tests/fixtures/kb_grounding_1285/PROVENANCE.md``.
"""

import json
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.kb_grounding import (
    KBSeedGrounding,
    kb_hit_grounding,
)
from faultmaven.core.investigation.milestone_engine import MilestoneEngine
from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
    CorpusTermStats,
    KnowledgeVectorStore,
)
from faultmaven.models.common import SearchResult

pytestmark = pytest.mark.unit

FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "fixtures"
    / "kb_grounding_1285"
    / "grounding_pairs.json"
)

CONTENT_FREE_QUERY = "The application is slow."
DISK_RUNBOOK = "Linux Disk Full"
# The two disk phrasings either side of the shipped 0.90 bar. Both retrieve the
# same, correct runbook; they differ only in wording.
DISK_ABOVE_BAR = (
    "The app server filesystem is at 100% and writes fail with ENOSPC, "
    "no space left on device."
)
DISK_BELOW_BAR = "df shows 100% used on /var and the service cannot write its PID file."


def _fixture():
    return json.loads(FIXTURE.read_text())


def _recorded_stats(n_chunks, df):
    """A real ``CorpusTermStats`` carrying the corpus's recorded frequencies.

    ``idf`` is a pure function of ``(n_chunks, document_frequency)``, so filling
    the postings with the recorded counts reproduces the production weights
    exactly rather than approximating them. Built as the real class so a change
    to the IDF formula reaches these numbers.
    """
    stats = CorpusTermStats(documents=[], signature=None)
    stats.n_chunks = n_chunks
    stats._postings = {term: set(range(n)) for term, n in df.items()}
    return stats


def _measure(pair, n_chunks):
    """Per-chunk coverage and identity terms for one pair, by the shipped code.

    Coverage is folded to the maximum over the runbook's own retrieved chunks,
    which is what the engine does: grounding is a property of the DOCUMENT, and
    judging each chunk separately would make it do corroboration's job twice.
    """
    stats = _recorded_stats(n_chunks, pair["df"])
    query_terms = KnowledgeVectorStore._extract_query_terms(pair["query"])
    per_chunk = [
        KnowledgeVectorStore._compute_term_overlap(
            query_terms, chunk["text"], stats=stats
        )
        for chunk in pair["chunks"]
    ]
    named = KnowledgeVectorStore._identity_terms_in_query(
        {"title": pair["runbook_title"], "service": pair["runbook_service"]},
        pair["query"].lower(),
    )
    return per_chunk, named


@pytest.fixture(scope="module")
def measured():
    """Every fixture pair, with coverage and identity terms recomputed.

    Recomputed rather than read: a fixture that carried the answers would pin
    nothing about the code that produces them.
    """
    data = _fixture()
    n_chunks = data["corpus"]["n_chunks"]
    rows = []
    for pair in data["pairs"]:
        per_chunk, named = _measure(pair, n_chunks)
        rows.append(
            {**pair, "per_chunk": per_chunk, "coverage": max(per_chunk), "named": named}
        )
    return rows


def _hit(row):
    return SimpleNamespace(
        term_coverage=row["coverage"],
        identity_terms_in_query=row["named"],
    )


def _report(rows):
    """The table the acceptance criterion asks for, denominators included."""
    silent = [r for r in rows if not r["named"]]
    lines = [
        f"pairs (denominator)                      : {len(rows)}",
        f"distinct queries                         : {len({r['query'] for r in rows})}",
        f"grounded by the NAMES arm                : {len(rows) - len(silent)}",
        f"names silent (all a 2nd ground could do) : {len(silent)}",
        f"  of those, ON-domain                    : "
        f"{sum(1 for r in silent if r['on_domain'])}",
        f"  of those, OFF-domain                   : "
        f"{sum(1 for r in silent if not r['on_domain'])}",
    ]
    return "\n".join(lines)


class TestTheFixtureIsAFaithfulRecording:
    """The apparatus responds, and it responds with the corpus's own numbers.

    Anchored on the recorded values, which were produced by a separate offline
    run against the live ChromaDB collection — independent of anything this file
    computes. If ``_compute_term_overlap`` or ``_identity_terms_in_query``
    changes what it measures, or is stubbed out, this fails first and every
    number below is known to be untrustworthy.
    """

    def test_every_recomputed_value_matches_what_was_recorded(self, measured):
        mismatches = [
            (r["query"][:40], r["runbook_title"][:30], r["per_chunk"], r["named"])
            for r in measured
            if r["named"] != r["recorded_identity_terms"]
            or abs(r["coverage"] - r["recorded_max_term_coverage"]) > 1e-9
            or any(
                abs(got - chunk["recorded_term_coverage"]) > 1e-9
                for got, chunk in zip(r["per_chunk"], r["chunks"])
            )
        ]
        assert not mismatches, (
            f"{len(mismatches)} of {len(measured)} pairs no longer reproduce the "
            f"recorded corpus measurement: {mismatches[:5]}. Either the lexical "
            f"code changed (the plural fold, the stop list, the 3-char floor, the "
            f"IDF formula — the whole of the gate now) or the fixture is stale. "
            f"See tests/fixtures/kb_grounding_1285/PROVENANCE.md."
        )

    def test_both_classes_span_the_bar_in_the_partition_that_is_swept(self, measured):
        """Guards the partition the sweep RUNS on, not a wider one.

        The first cut of this fixture checked ``measured`` while the sweep ran
        over the names-silent subset, and in that subset every off-domain pair
        sat at coverage exactly 1.000 — so the sweep's comparison was true by
        selection at every bar and could not have failed. Both classes must have
        members on both sides of the bar or the sweep measures the fixture.
        """
        silent = [r for r in measured if not r["named"]]
        on = [r for r in silent if r["on_domain"]]
        off = [r for r in silent if not r["on_domain"]]
        quadrant = {
            ("on", "above"): [r for r in on if r["coverage"] >= 0.90],
            ("on", "below"): [r for r in on if r["coverage"] < 0.90],
            ("off", "above"): [r for r in off if r["coverage"] >= 0.90],
            ("off", "below"): [r for r in off if r["coverage"] < 0.90],
        }
        for key, rows in sorted(quadrant.items()):
            print(f"  {key[0]:<4}{key[1]:<7}{len(rows):>4}")
        empty = [k for k, v in quadrant.items() if not v]
        assert not empty, (
            f"quadrants {empty} are empty: with no off-domain pair below the bar "
            f"(or no on-domain pair above it) the sweep below cannot distinguish "
            f"an inverted distribution from a selected one"
        )
        assert len(quadrant[("off", "below")]) >= 10, (
            f"only {len(quadrant[('off', 'below')])} off-domain pairs below the "
            f"bar — too few for the sweep's rate to move"
        )

    def test_both_labels_are_present_in_quantity(self, measured):
        on = [r for r in measured if r["on_domain"]]
        off = [r for r in measured if not r["on_domain"]]
        assert len(on) >= 15 and len(off) >= 50, (
            f"on-domain {len(on)}, off-domain {len(off)} of {len(measured)} — "
            f"a single-element class makes every claim below a tautology"
        )

    def test_kind_and_on_domain_are_independent(self, measured):
        """``kind`` must not have collapsed into a copy of ``on_domain``.

        In the first cut it had: every off-domain pair came from a labelled
        NEGATIVE query, so the two fields agreed on all 55 rows and the fixture
        held no off-domain pair retrieved by a query that was about something.
        That is the same selection defect the sweep guard above now catches,
        seen from the labelling side.
        """
        combos = {(r["kind"], r["on_domain"]) for r in measured}
        assert (
            "labelled_positive",
            False,
        ) in combos, (
            f"no off-domain pair from a labelled POSITIVE query: {sorted(combos)}"
        )
        assert ("labelled_positive", True) in combos
        assert ("labelled_negative", False) in combos


class TestCoverageOrdersTheAnswersWrongly:
    """Why the covers arm cannot be repaired by moving its threshold.

    These are claims about the recorded DATA, not about the gate's code: they
    hold whatever the gate does with them, which is what makes them a reason.
    """

    def test_a_query_that_identifies_nothing_covers_everything(self, measured):
        rows = [r for r in measured if r["query"] == CONTENT_FREE_QUERY]
        assert len(rows) >= 5, (
            f"expected the content-free statement's retrieved runbooks in the "
            f"fixture, found {len(rows)}"
        )
        assert all(r["coverage"] == pytest.approx(1.0) for r in rows), [
            (r["runbook_title"], r["coverage"]) for r in rows
        ]
        assert not any(r["on_domain"] for r in rows), (
            "this is one of the project's own labelled NEGATIVES: the correct "
            "outcome for it is to seed nothing"
        )
        assert len({r["runbook_title"] for r in rows}) >= 5, (
            "the point is simultaneity — one content-free sentence reaching "
            "maximum coverage against many unrelated runbooks at once"
        )

    def test_the_worst_pair_outranks_the_best_correct_one(self, measured):
        """No threshold separates them, because the ordering itself is inverted."""
        silent = [r for r in measured if not r["named"]]
        on = [r for r in silent if r["on_domain"]]
        off = [r for r in silent if not r["on_domain"]]
        assert on and off, "COULD NOT ASK: one class is empty"
        best_on = max(r["coverage"] for r in on)
        worst_off = max(r["coverage"] for r in off)
        print(_report(measured))
        print(f"best ON-domain coverage {best_on:.3f} vs worst OFF {worst_off:.3f}")
        assert worst_off > best_on, (
            f"the covers arm was removed because coverage ranks an off-domain "
            f"pair ({worst_off:.3f}) above every on-domain one ({best_on:.3f}) "
            f"over {len(silent)} pairs the names arm leaves undecided. If this "
            f"ever stops holding, a second ground becomes arguable again — on "
            f"THIS measurement, not on the threshold's plausibility"
        )

    def test_no_bar_admits_correct_pairs_at_a_better_rate_than_wrong_ones(
        self, measured
    ):
        """Swept as a RATE, because the two classes are different sizes.

        The first cut compared raw counts over a 12-vs-29 split in which every
        off-domain pair sat at coverage exactly 1.000. ``n_off >= n_on`` was
        then true at every bar no matter what the code or the data did — the
        off-domain count was the constant 29 — so the sweep could not have
        detected a distribution that was NOT inverted. Comparing admission
        RATES over classes that both span the range is a claim the data can
        falsify, and it does: at bar 0.30 the on-domain rate is the higher of
        the two, which is exactly the kind of point the old form could not see.

        What survives, and is the reason the arm is gone, is that the inversion
        holds across the whole region where a bar could sensibly be set.
        """
        silent = [r for r in measured if not r["named"]]
        on = [r for r in silent if r["on_domain"]]
        off = [r for r in silent if not r["on_domain"]]
        assert len(on) >= 8 and len(off) >= 20, (
            f"COULD NOT ASK: {len(on)} on-domain and {len(off)} off-domain "
            f"pairs where the names arm is silent"
        )
        rates = []
        for bar in [i / 100 for i in range(20, 101, 5)]:
            r_on = sum(1 for r in on if r["coverage"] >= bar) / len(on)
            r_off = sum(1 for r in off if r["coverage"] >= bar) / len(off)
            rates.append((bar, r_on, r_off))
        print(f"bar    on-rate (n={len(on)})   off-rate (n={len(off)})")
        for bar, r_on, r_off in rates:
            print(f"{bar:.2f}   {r_on:>10.2f}   {r_off:>14.2f}")

        # Non-vacuity: the sweep must MOVE, or "off >= on everywhere" would be
        # a statement about a constant.
        assert len({round(r_off, 3) for _, _, r_off in rates}) >= 4, (
            f"the off-domain rate barely varies across the sweep "
            f"({sorted({round(r, 3) for _, _, r in rates})}) — the fixture, not "
            f"the metric, is deciding this"
        )
        assert len({round(r_on, 3) for _, r_on, _ in rates}) >= 4

        # The claim: over the region where a usable bar could sit — one that
        # admits fewer than everything — coverage prefers the WRONG class.
        usable = [(bar, r_on, r_off) for bar, r_on, r_off in rates if bar >= 0.50]
        assert usable
        assert all(r_off >= r_on for _, r_on, r_off in usable), (
            f"a bar at or above 0.50 admits on-domain pairs at a better rate "
            f"than off-domain ones, which would make a second ground arguable "
            f"again: {[(b, round(o, 2), round(f, 2)) for b, o, f in usable]}"
        )
        at_shipped = next(t for t in rates if t[0] == 0.90)
        assert at_shipped[2] >= 5 * at_shipped[1], (
            f"at the bar #1272 shipped, off-domain admission rate "
            f"{at_shipped[2]:.2f} vs on-domain {at_shipped[1]:.2f} — the "
            f"inversion that removed the arm has narrowed; re-measure before "
            f"trusting either number"
        )

    def test_the_same_correct_runbook_lands_on_both_sides_of_the_old_bar(
        self, measured
    ):
        """The user-visible harm: phrasing, not correctness, decided admission."""
        by_query = {
            r["query"]: r
            for r in measured
            if r["runbook_title"] == DISK_RUNBOOK
            and r["query"] in (DISK_ABOVE_BAR, DISK_BELOW_BAR)
        }
        assert set(by_query) == {DISK_ABOVE_BAR, DISK_BELOW_BAR}, sorted(by_query)
        above = by_query[DISK_ABOVE_BAR]
        below = by_query[DISK_BELOW_BAR]
        assert above["on_domain"] and below["on_domain"]
        assert not above["named"] and not below["named"]
        print(f"same runbook: {above['coverage']:.3f} vs {below['coverage']:.3f}")
        assert above["coverage"] >= 0.90 > below["coverage"], (
            f"two phrasings of one correct disk-full incident measured "
            f"{above['coverage']:.3f} and {below['coverage']:.3f} — the old bar "
            f"ran between them, so it was deciding wording"
        )


class TestTheGateGroundsOnNamesAlone:
    def test_the_names_arm_decides_and_the_rate_is_reported(self, measured):
        by_verdict = defaultdict(list)
        for row in measured:
            by_verdict[kb_hit_grounding(_hit(row))].append(row)
        print(_report(measured))
        for verdict, rows in sorted(by_verdict.items(), key=lambda kv: kv[0].value):
            print(f"{verdict.value:<12}{len(rows):>5} / {len(measured)}")
        named = by_verdict[KBSeedGrounding.NAMED]
        ungrounded = by_verdict[KBSeedGrounding.UNGROUNDED]
        # The positive half of the control: the gate is not simply refusing
        # everything. Without this, the refusals below prove nothing.
        assert len(named) >= 10, (
            f"only {len(named)} of {len(measured)} pairs ground — a gate that "
            f"admits nothing would satisfy every refusal assertion in this file"
        )
        assert sum(1 for r in named if r["on_domain"]) >= 5, (
            "the pairs it admits must include on-domain ones, or 'it admits "
            "something' is not evidence that it admits the right things"
        )
        assert all(r["named"] for r in named)
        assert not any(r["named"] for r in ungrounded)
        assert not by_verdict[KBSeedGrounding.UNMEASURED], (
            "every fixture pair carries a measured coverage; an UNMEASURED "
            "verdict here would mean the apparatus stopped measuring"
        )

    def test_maximum_coverage_no_longer_grounds_anything(self, measured):
        rows = [r for r in measured if r["query"] == CONTENT_FREE_QUERY]
        assert rows
        assert all(r["coverage"] == pytest.approx(1.0) for r in rows)
        unnamed = [r for r in rows if not r["named"]]
        assert len(unnamed) >= 5, (
            f"only {len(unnamed)} of {len(rows)} pairs are unnamed — this claim "
            f"is about what COVERAGE grounds, so it needs pairs the names arm "
            f"does not decide"
        )
        assert all(
            kb_hit_grounding(_hit(r)) is KBSeedGrounding.UNGROUNDED for r in unnamed
        ), (
            "coverage 1.000 against unrelated runbooks must not ground a seed; "
            "this is the arm #1285 removed"
        )

    def test_the_surviving_arms_wrong_admissions_are_bounded(self, measured):
        """The metric #1285 condemned the coverage arm with, applied to the arm
        it kept.

        A labelled NEGATIVE is a statement carrying no concrete failure
        signature; the project's own expectation for one is to seed nothing. So
        every pair the gate admits from such a query is a wrong admission, with
        no labelling judgement required. The coverage arm was removed at 1
        on-domain to 36 off-domain. This is the same measurement for what
        remains, and until #1285 nothing asserted it — the file reported the
        arm's MISSES (the 12 on-domain pairs it does not admit) and never its
        wrong admissions, so a change doubling them would have passed.

        This is a BOUND, not an endorsement. It is expected to be lowered.
        """
        neg = [r for r in measured if r["kind"] == "labelled_negative"]
        admitted = [
            r for r in neg if kb_hit_grounding(_hit(r)) is KBSeedGrounding.NAMED
        ]
        queries = {r["query"] for r in neg}
        hit_queries = {r["query"] for r in admitted}
        print(
            f"labelled-negative pairs (correct outcome: seed nothing): {len(neg)}\n"
            f"  admitted by the names arm                            : "
            f"{len(admitted)}\n"
            f"  distinct such queries                                : "
            f"{len(queries)}\n"
            f"  distinct such queries with >=1 admission             : "
            f"{len(hit_queries)}"
        )
        for r in sorted(admitted, key=lambda r: (r["query"], r["runbook_title"])):
            print(
                f"    {r['query'][:38]!r:<42} -> {r['runbook_title'][:40]:<42}"
                f"via {r['named']}"
            )
        assert len(neg) >= 40, f"COULD NOT ASK: only {len(neg)} negative pairs"
        assert len(admitted) <= 16, (
            f"the surviving arm admits {len(admitted)} of {len(neg)} pairs from "
            f"queries that identify nothing, up from 16. Every one is a runbook "
            f"asserted as a candidate root cause for a statement with no failure "
            f"signature in it"
        )
        assert len(hit_queries) <= 8, (
            f"{len(hit_queries)} of {len(queries)} content-free queries now seed "
            f"through the names arm, up from 8"
        )
        # The other half of the bound: it must still be a real residue, or the
        # ceiling above is being met by a gate that stopped admitting anything.
        assert len(admitted) >= 10, (
            f"only {len(admitted)} wrong admissions — if the arm genuinely "
            f"improved, lower the ceiling deliberately rather than leaving a "
            f"bound that no longer bounds anything"
        )

    def test_the_remaining_arm_has_a_residue_and_here_it_is(self, measured):
        """One pair of the eight IS named, and it is worth knowing which.

        "The application is slow." names *MongoDB Lock Contention and Slow
        Operations* through the word `slow` in its title. #1272 measured the
        rules that close this — requiring two matched terms, one above an IDF
        floor, or a share of the document's identity mass — and every one cost
        between four and nine correct seeds, so the residue was accepted. It is
        pinned here rather than left implicit because removing the coverage arm
        makes title tokenisation the whole of the gate: this is the shape of
        what it still lets through, and the fixture for anyone who takes it on.
        """
        rows = [r for r in measured if r["query"] == CONTENT_FREE_QUERY]
        named = [r for r in rows if r["named"]]
        assert [(r["runbook_title"], r["named"]) for r in named] == [
            ("MongoDB Lock Contention and Slow Operations", ["slow"])
        ], (
            f"the known residue of the names arm on this query changed: {named}. "
            f"That is not necessarily wrong — but it is a change to the only "
            f"ground the seeding gate has left, so it must be looked at."
        )
        assert kb_hit_grounding(_hit(named[0])) is KBSeedGrounding.NAMED

    def test_the_verdict_responds_to_the_field_it_claims_to_read(self, measured):
        """Discriminating control: same chunk, same runbook, only naming changes.

        A refusal test that only ever shows non-firing cannot distinguish a
        working gate from a dead one. This flips one real pair from UNGROUNDED
        to NAMED by changing nothing but the query, with the identity terms
        computed by the same production method rather than asserted by hand.
        """
        row = next(
            r
            for r in measured
            if r["query"] == CONTENT_FREE_QUERY and "Kafka" in r["runbook_title"]
        )
        assert kb_hit_grounding(_hit(row)) is KBSeedGrounding.UNGROUNDED
        naming_query = "kafka consumer lag keeps growing on one partition"
        named = KnowledgeVectorStore._identity_terms_in_query(
            {"title": row["runbook_title"], "service": row["runbook_service"]},
            naming_query,
        )
        assert named, (
            f"the control itself is broken: {naming_query!r} names no term of "
            f"{row['runbook_title']!r}"
        )
        flipped = SimpleNamespace(
            term_coverage=row["coverage"], identity_terms_in_query=named
        )
        assert kb_hit_grounding(flipped) is KBSeedGrounding.NAMED


class TestUnmeasuredIsNotUngrounded:
    """The third branch: no lexical evidence at all is not evidence against."""

    def test_a_hit_with_no_evidence_is_unmeasured(self):
        assert (
            kb_hit_grounding(
                SimpleNamespace(term_coverage=None, identity_terms_in_query=[])
            )
            is KBSeedGrounding.UNMEASURED
        )

    def test_a_measured_zero_is_ungrounded_not_unmeasured(self):
        """Emptiness and absence are different facts and must not collapse.

        ``identity_terms_in_query`` is ``[]`` in both, so the verdict turns on
        ``term_coverage``: a measured 0.0 is a finding, ``None`` is a silence.
        """
        assert (
            kb_hit_grounding(
                SimpleNamespace(term_coverage=0.0, identity_terms_in_query=[])
            )
            is KBSeedGrounding.UNGROUNDED
        )

    def test_an_object_missing_the_fields_entirely_is_unmeasured(self):
        """A hit-shaped object from a path that computes neither field."""
        assert kb_hit_grounding(SimpleNamespace()) is KBSeedGrounding.UNMEASURED

    def test_naming_decides_even_with_no_coverage_measurement(self):
        assert (
            kb_hit_grounding(
                SimpleNamespace(term_coverage=None, identity_terms_in_query=["nginx"])
            )
            is KBSeedGrounding.NAMED
        )


class TestATermIndexOutageCannotSwitchTheGateOff:
    """The coupling: ``term_coverage`` is a float in BOTH term-index states.

    Without a corpus term index ``_compute_term_overlap`` degrades to an
    unweighted binary fraction — a different quantity on the same [0, 1] scale —
    rather than returning None. So an index outage cannot reach the UNMEASURED
    branch and cannot silently stop the gate applying. The gate reads ``None``
    as "no reranker ran" and nothing else; this is what makes that reading true.
    """

    @staticmethod
    def _candidate():
        return {
            "id": "c1",
            "content": "Disk usage reached 100% and writes fail with ENOSPC.",
            "metadata": {"title": "Linux Disk Full", "scope": "global"},
            "score": 0.7,
        }

    def test_rerank_writes_a_float_with_no_corpus_statistics(self):
        query = "writes fail with ENOSPC on a full volume"
        out = KnowledgeVectorStore._rerank(
            candidates=[self._candidate()],
            query_terms=KnowledgeVectorStore._extract_query_terms(query),
            context_metadata={},
            query=query,
            stats=None,
        )
        assert isinstance(out[0]["term_coverage"], float)
        assert (
            kb_hit_grounding(
                SimpleNamespace(
                    term_coverage=out[0]["term_coverage"],
                    identity_terms_in_query=[],
                )
            )
            is KBSeedGrounding.UNGROUNDED
        ), "an index outage must not turn a measured hit into an unjudged one"

    def test_the_two_states_are_different_quantities(self):
        """Control: if both states produced the same number, the test above
        would be uninformative about the coupling it claims to pin."""
        query = "writes fail with ENOSPC on a full volume"
        terms = KnowledgeVectorStore._extract_query_terms(query)
        stats = _recorded_stats(1297, {t: 5 if t == "enospc" else 400 for t in terms})
        with_index = KnowledgeVectorStore._rerank(
            candidates=[self._candidate()],
            query_terms=terms,
            context_metadata={},
            query=query,
            stats=stats,
        )[0]["term_coverage"]
        without = KnowledgeVectorStore._rerank(
            candidates=[self._candidate()],
            query_terms=terms,
            context_metadata={},
            query=query,
            stats=None,
        )[0]["term_coverage"]
        print(f"with index {with_index:.4f}  without {without:.4f}")
        assert with_index != pytest.approx(without), (
            "the IDF weighting must actually change the value, or 'a float in "
            "both states' says nothing about which quantity was measured"
        )

    def test_the_verdict_itself_is_the_same_in_both_states(self, measured):
        """And this is the point of the two above: the coupling is gone.

        The gate now reads ``identity_terms_in_query``, which the term index
        never touched, and ``term_coverage`` only for ``is None``, which the
        index cannot change. So the SAME hit gets the SAME verdict either way.
        It is worth asserting rather than reasoning to, because it is the
        property that made the shipped gate's threshold ill-defined: 0.90 was
        dead against one quantity and live at the boundary of the other.
        """
        query = "writes fail with ENOSPC on a full volume"
        terms = KnowledgeVectorStore._extract_query_terms(query)
        stats = _recorded_stats(1297, {t: 5 if t == "enospc" else 400 for t in terms})
        verdicts = set()
        for state in (stats, None):
            out = KnowledgeVectorStore._rerank(
                candidates=[self._candidate()],
                query_terms=terms,
                context_metadata={},
                query=query,
                stats=state,
            )[0]
            verdicts.add(
                kb_hit_grounding(
                    SimpleNamespace(
                        term_coverage=out["term_coverage"],
                        identity_terms_in_query=out["identity_terms_in_query"],
                    )
                )
            )
        assert len(verdicts) == 1, (
            f"the grounding verdict must not depend on whether a corpus term "
            f"index existed, and it does: {verdicts}"
        )
        # ... and the verdict is a real one, not a shared UNMEASURED shrug.
        assert verdicts == {KBSeedGrounding.NAMED}, verdicts


class TestTheSeederRefusesTheContentFreeQuery:
    """End to end: the same recorded pairs, through the real seeding path."""

    @staticmethod
    def _engine():
        engine = MilestoneEngine.__new__(MilestoneEngine)
        service = MagicMock()
        service.get_runbook_causes = AsyncMock(
            side_effect=lambda p: [{"cause_letter": "A", "statement": "s"}]
        )
        engine.knowledge_service = service
        engine.hypothesis_manager = MagicMock()
        return engine

    @staticmethod
    def _case(description):
        return SimpleNamespace(
            case_id="case_test",
            user_id="u1",
            organization_id=None,
            kb_context=None,
            current_turn=1,
            description=description,
        )

    async def _seed_hits(self, hits, monkeypatch):
        """Drive the real seeding path with ready-made hits."""
        seen = {}

        def _capture(case, runbooks, *a, **k):
            seen["runbooks"] = runbooks
            return SimpleNamespace(seeded_anything=True)

        monkeypatch.setattr(
            "faultmaven.core.investigation.kb_cause_seeder.seed_candidate_causes",
            _capture,
        )
        monkeypatch.setattr(
            "faultmaven.config.settings.get_settings",
            lambda: SimpleNamespace(
                features=SimpleNamespace(kb_cause_seeder_enabled=True)
            ),
        )
        engine = self._engine()
        await engine._seed_candidate_causes_from_kb(self._case("q"), hits)
        return seen.get("runbooks", [])

    async def _seed(self, rows, monkeypatch):
        seen = {}

        def _capture(case, runbooks, *a, **k):
            seen["runbooks"] = runbooks
            return SimpleNamespace(seeded_anything=True)

        monkeypatch.setattr(
            "faultmaven.core.investigation.kb_cause_seeder.seed_candidate_causes",
            _capture,
        )
        monkeypatch.setattr(
            "faultmaven.config.settings.get_settings",
            lambda: SimpleNamespace(
                features=SimpleNamespace(kb_cause_seeder_enabled=True)
            ),
        )
        hits = [
            SearchResult(
                document_id=chunk["chunk_id"],
                title=row["runbook_title"],
                document_type="runbook",
                tags=[],
                score=0.7,
                snippet="...",
                parent_document_id=row["runbook_title"],
                total_chunks=8,
                matched_cause_letters=["A"],
                term_coverage=coverage,
                identity_terms_in_query=row["named"],
            )
            for row in rows
            for chunk, coverage in zip(row["chunks"], row["per_chunk"])
        ]
        engine = self._engine()
        await engine._seed_candidate_causes_from_kb(self._case(rows[0]["query"]), hits)
        return seen.get("runbooks", [])

    @pytest.mark.asyncio
    async def test_nothing_seeds_for_a_statement_that_identifies_nothing(
        self, measured, monkeypatch
    ):
        rows = [r for r in measured if r["query"] == CONTENT_FREE_QUERY]
        assert len(rows) >= 5
        # Every pair for this query goes in. The first cut filtered to the
        # unnamed ones, which quietly restricted the claim to pairs the REMOVED
        # arm handled and dropped the single pair that shows what the surviving
        # arm does with the same statement.
        unnamed = [r for r in rows if not r["named"]]
        named = [r for r in rows if r["named"]]
        assert len(unnamed) >= 5 and len(named) >= 1, (
            f"expected both kinds for this query: {len(unnamed)} unnamed, "
            f"{len(named)} named"
        )
        assert all(r["coverage"] == pytest.approx(1.0) for r in unnamed), (
            "these are the maximum-coverage pairs; if they no longer are, this "
            "test is no longer about the arm that was removed"
        )
        seeded = await self._seed(rows, monkeypatch)
        assert seeded == [], (
            f"{len(rows)} runbooks retrieved for a statement that identifies "
            f"nothing, {len(unnamed)} of them at coverage 1.000, must not seed "
            f"candidate root causes. NOTE what is doing the work here: the "
            f"{len(named)} NAMED pair is refused downstream by #1144's "
            f"corroboration guard (retrieval returned one chunk of it), not by "
            f"grounding. Grounding admits it — see "
            f"test_the_surviving_arms_wrong_admissions_are_bounded"
        )

    @pytest.mark.asyncio
    async def test_a_parentless_hit_does_not_admit_the_ungrounded_ones(
        self, monkeypatch
    ):
        """A hit with no ``parent_document_id`` must not open the gate.

        Grounding is folded per runbook through a set of admitted parent ids.
        A parentless hit that passes the gate used to put a literal ``None`` in
        that set, and the membership test then admitted every OTHER parentless
        hit — UNGROUNDED ones included. Reachable: the parent id is derived
        from chunk metadata falling back to the chunk id, and both can be
        absent. It seeds nothing either way, so the only visible effect is that
        ``kb_cause_seed_ungrounded_total`` stops counting what the gate turned
        away — the number the gate is re-sized on.
        """
        ungrounded = MagicMock()
        monkeypatch.setattr(
            "faultmaven.core.investigation.milestone_engine."
            "kb_cause_seed_ungrounded_total",
            ungrounded,
        )

        def _mk(parent, named, doc_id):
            return SearchResult(
                document_id=doc_id,
                title="t",
                document_type="runbook",
                tags=[],
                score=0.7,
                snippet="...",
                parent_document_id=parent,
                total_chunks=8,
                matched_cause_letters=["A"],
                term_coverage=0.5,
                identity_terms_in_query=named,
            )

        # One parentless hit the gate ADMITS, beside a parentless one it refuses.
        hits = [_mk(None, ["named"], "c1"), _mk(None, [], "c2")]
        seeded = await self._seed_hits(hits, monkeypatch)
        assert seeded == [], "neither hit belongs to a runbook, so nothing seeds"
        assert ungrounded.inc.called, (
            "the refused parentless hit was excluded, so the exclusion must be "
            "counted; if it is not, a None in the admitted-parent set has waved "
            "it through"
        )

    @pytest.mark.asyncio
    async def test_the_gate_not_applying_is_counted_not_silent(
        self, measured, monkeypatch
    ):
        """A gate that has stopped applying looks exactly like one finding nothing.

        Hits with no lexical evidence pass through — an absent measurement must
        not authorise what the gate withholds, but neither may it disable
        seeding wholesale. That pass-through is the one state in which nothing
        is checked, so it is counted. The counter is substituted rather than
        read: ``prometheus_client`` is optional here and its absence turns every
        metric into a no-op that would make a read-based assertion vacuous.
        """
        counter = MagicMock()
        monkeypatch.setattr(
            "faultmaven.core.investigation.milestone_engine."
            "kb_cause_seed_grounding_unmeasured_total",
            counter,
        )
        row = next(r for r in measured if r["named"] and len(r["chunks"]) >= 2)
        unmeasured = {**row, "named": [], "per_chunk": [None] * len(row["chunks"])}
        seeded = await self._seed([unmeasured], monkeypatch)

        assert counter.inc.call_count == 1, (
            "a seeding attempt carrying unmeasured hits must be counted, or "
            "'the gate turned nothing away' and 'the gate did not run' are the "
            "same observation"
        )
        assert [r.item_id for r in seeded] == [
            row["runbook_title"]
        ], "unmeasured is not ungrounded: the hits still pass through"

        # The discriminating half: the counter must not fire when grounding WAS
        # measured, or 'it fired' says nothing about which state produced it.
        await self._seed([row], monkeypatch)
        assert counter.inc.call_count == 1

    @pytest.mark.asyncio
    async def test_a_named_runbook_still_seeds(self, measured, monkeypatch):
        """The other half of the control, through the same path.

        Without it, the refusal above is satisfied by a seeder that never seeds.
        Uses a pair with two recorded chunks, because #1144's corroboration
        guard sits downstream of grounding and would otherwise decline it for a
        reason that has nothing to do with this file.
        """
        candidates = [
            r
            for r in measured
            if r["named"] and r["on_domain"] and len(r["chunks"]) >= 2
        ]
        assert (
            candidates
        ), "COULD NOT ASK: no named on-domain pair with two recorded chunks"
        row = candidates[0]
        seeded = await self._seed([row], monkeypatch)
        assert [r.item_id for r in seeded] == [row["runbook_title"]], (
            f"the query {row['query'][:50]!r} names {row['runbook_title']!r} "
            f"and must still seed it"
        )


GRAFANA_RUNBOOK = "Grafana Dashboard Loading Slowly"


class TestRarityOfTheMatchedTermDoesNotOrderTheAdmissions:
    """fm#1293 — the repair the issue points at, measured before it was built.

    The names arm admits on ONE title word, so the natural repair is to ask
    whether that word is IDENTIFYING rather than whether it matched — weight
    it by corpus rarity, the discrimination #1282 applied to ranking. The
    statistics to do it with are reachable where the arm runs: ``_rerank``
    holds ``CorpusTermStats`` at the moment it computes
    ``identity_terms_in_query``. So the only open question was whether rarity
    ORDERS the admissions, and on the recorded corpus it does not, in both
    directions:

    - the wrong admissions that go on to SEED (the labelled negatives seeding
      *Grafana Dashboard Loading Slowly*) ride on ``dashboard`` at 20 of 1297
      chunks — under ``IDENTIFIER_DF_RATIO``, an identifier by the corpus's
      own test;
    - correct seeds ride on nothing rare: ``disk`` (99), ``connection`` (188),
      ``403``/``denied`` (69/59) — and the disk query carries no
      identifier-class term anywhere in it, so a query-level precondition
      (the issue's own declined alternative) refuses it too.

    A rarity floor at the declared ratio keeps 3 of the 7 correct admissions
    and 3 of the 16 wrong ones, and the 3 wrong ones it keeps are the only
    ones that seed. Title-level rarity (how many runbooks share the word),
    naming the service, requiring two terms and requiring the query's rarest
    term were measured beside it and each drops 3 to 7 of the 7. The residue
    is semantic — "the dashboard shows…" names the instrument, not the
    subject — and no per-term quantity the reranker holds can see that.
    Pinned so the next reading of the issue does not build it.
    """

    @pytest.fixture(scope="class")
    def n_chunks(self):
        return _fixture()["corpus"]["n_chunks"]

    @staticmethod
    def _fully_recorded(row):
        # ``df`` is keyed by the QUERY's tokens. A title term matched under the
        # plural fold (``pod`` by "pods") has no recorded frequency of its own,
        # so it cannot be judged here; such pairs are left out and counted.
        return all(t in row["df"] for t in row["named"])

    @staticmethod
    def _rarity_admits(row, n_chunks):
        stats = _recorded_stats(n_chunks, row["df"])
        return any(stats.is_identifier(t) for t in row["named"])

    @staticmethod
    def _query_identifiers(row, n_chunks):
        stats = _recorded_stats(n_chunks, row["df"])
        terms = KnowledgeVectorStore._extract_query_terms(row["query"])
        missing = [t for t in terms if t not in row["df"]]
        assert not missing, f"COULD NOT ASK: query terms with no recorded df: {missing}"
        return [t for t in terms if stats.is_identifier(t)]

    def _grafana_negatives(self, measured):
        return [
            r
            for r in measured
            if r["kind"] == "labelled_negative"
            and r["named"]
            and r["runbook_title"] == GRAFANA_RUNBOOK
        ]

    def test_the_seeding_residue_rides_on_identifier_class_terms(
        self, measured, n_chunks
    ):
        rows = self._grafana_negatives(measured)
        assert len(rows) >= 3, (
            f"COULD NOT ASK: {len(rows)} labelled-negative admissions of "
            f"{GRAFANA_RUNBOOK!r} in the fixture"
        )
        for r in rows:
            assert self._fully_recorded(r), r["named"]
            assert kb_hit_grounding(_hit(r)) is KBSeedGrounding.NAMED
            assert self._rarity_admits(r, n_chunks), (
                f"{r['query'][:40]!r} names {GRAFANA_RUNBOOK!r} via {r['named']} "
                f"at df {[r['df'][t] for t in r['named']]} — a rarity floor at "
                f"IDENTIFIER_DF_RATIO would now REFUSE this pair, which is the "
                f"one thing this file says it cannot do. Re-derive, don't adjust."
            )

    @pytest.mark.asyncio
    async def test_and_that_residue_seeds_through_the_real_path(
        self, measured, monkeypatch
    ):
        """Residue means SEEDED, not merely admitted.

        Grounding is one of three guards; the corroboration guard downstream
        could in principle be what stops these. It is not: every recorded
        Grafana pair with two chunks clears it and seeds, which is what the
        offline e2e driver also shows on the live corpus (10, 5 and 4 chunks).
        """
        rows = [r for r in self._grafana_negatives(measured) if len(r["chunks"]) >= 2]
        assert rows, "COULD NOT ASK: no Grafana negative carries two recorded chunks"
        driver = TestTheSeederRefusesTheContentFreeQuery()
        for r in rows:
            seeded = await driver._seed([r], monkeypatch)
            assert [x.item_id for x in seeded] == [GRAFANA_RUNBOOK], (
                f"{r['query'][:40]!r} no longer seeds {GRAFANA_RUNBOOK!r} — if a "
                f"guard now catches it, say which and re-derive this file"
            )

    def test_correct_seeds_ride_on_common_terms(self, measured, n_chunks):
        rows = [
            r
            for r in measured
            if r["on_domain"] and r["named"] and self._fully_recorded(r)
        ]
        assert (
            len(rows) >= 5
        ), f"COULD NOT ASK: {len(rows)} judgeable correct admissions"
        common = [r for r in rows if not self._rarity_admits(r, n_chunks)]
        titles = {r["runbook_title"] for r in common}
        for fragment in (
            "Linux Disk Full",
            "AWS RDS Connection Exhaustion",
            "AWS S3 403 Access Denied",
        ):
            assert any(fragment in t for t in titles), (
                f"{fragment!r} is a correct seed named on a common word "
                f"(df well above the identifier ratio); if it now carries a "
                f"rare term the corpus changed and this must be re-derived"
            )
        assert len(common) >= 3

    def test_a_rarity_floor_keeps_the_residue_and_drops_the_correct_seeds(
        self, measured, n_chunks
    ):
        judged = [r for r in measured if r["named"] and self._fully_recorded(r)]
        skipped = sum(1 for r in measured if r["named"] and not self._fully_recorded(r))
        on = [r for r in judged if r["on_domain"]]
        neg = [r for r in judged if r["kind"] == "labelled_negative"]
        on_kept = [r for r in on if self._rarity_admits(r, n_chunks)]
        neg_kept = [r for r in neg if self._rarity_admits(r, n_chunks)]
        print(
            f"named pairs judged (every matched term has a recorded df): "
            f"{len(judged)} (skipped {skipped} folded-plural pairs)\n"
            f"  rarity floor at IDENTIFIER_DF_RATIO keeps, ON-domain : "
            f"{len(on_kept)}/{len(on)}\n"
            f"  rarity floor keeps, labelled-NEGATIVE                 : "
            f"{len(neg_kept)}/{len(neg)}"
        )
        for r in neg_kept:
            print(f"    keeps {r['query'][:38]!r:<42} -> {r['runbook_title'][:40]}")
        assert len(on) >= 6 and len(neg) >= 12, "COULD NOT ASK: too few judged pairs"
        # The cost: more than half of the correct admissions.
        assert 2 * len(on_kept) <= len(on), (
            f"a rarity floor now keeps {len(on_kept)} of {len(on)} correct "
            f"admissions — if that is real, the measurement in this file's "
            f"docstring is stale and fm#1293's disposition should be re-opened"
        )
        # And the wrong admissions it keeps include every one that seeds.
        assert {r["runbook_title"] for r in neg_kept} >= {GRAFANA_RUNBOOK}

    def test_the_query_level_precondition_refuses_the_disk_query(
        self, measured, n_chunks
    ):
        """fm#1293's own declined alternative, re-measured.

        Refuse to ground when the query carries no identifier-class term. On
        the 24 labelled statements it costs the disk seed and saves nothing
        that seeds: every query behind the Grafana residue carries such a term
        (``dashboard`` itself, among others).
        """
        by_query = {}
        for r in measured:
            by_query.setdefault(r["query"], r)
        disk = next(
            r for q, r in by_query.items() if q.startswith("Disk on the app server")
        )
        assert self._query_identifiers(disk, n_chunks) == [], (
            "the disk query now carries an identifier-class term; the "
            "precondition's cost has changed and must be re-measured"
        )
        grafana_queries = {r["query"] for r in self._grafana_negatives(measured)}
        assert len(grafana_queries) >= 3
        for q in grafana_queries:
            assert self._query_identifiers(by_query[q], n_chunks), (
                f"{q[:40]!r} carries no identifier-class term — the precondition "
                f"would now refuse a Grafana residue query; re-measure"
            )
