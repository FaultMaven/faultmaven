"""Unit tests for the KB cause-seeder I/O seams (LLM-free, stubbed dependencies).

The pure seeder (`seed_candidate_causes`) is covered in `test_kb_cause_seeder.py`.
This file pins the two seams around it — where retrieval crosses into the pure
module — which had **zero** coverage and are exactly where a real incident
originates:

- ``MilestoneEngine._seed_candidate_causes_from_kb`` — the flag gate, the
  dedup-to-distinct-runbook / best-score-wins / rank ordering, the
  ``get_runbook_causes`` fan-out and its None/[]/mixed filtering, and the
  crash-isolation contract (a seeder bug must never break the transition).
- ``KnowledgeService.get_runbook_causes`` — the loader that reads
  ``knowledge_items.metadata["causes"]`` and must return ``None`` (never raise)
  on a missing id, a row with no causes record, a non-list causes value, or a
  lookup error.

Both seams are exercised with stubbed collaborators (no live server, no LLM, no
DB) so the tests stay fast and deterministic.
"""

from types import SimpleNamespace
from unittest.mock import DEFAULT, AsyncMock, patch
from uuid import uuid4

import pytest

from faultmaven.core.investigation.hypothesis_manager import create_hypothesis_manager
from faultmaven.core.investigation.kb_cause_seeder import (
    MAX_SEEDED_RUNBOOKS,
    SEEDED_FROM_RUNBOOK_KEY,
)
from faultmaven.core.investigation.milestone_engine import (
    KB_CONTEXT_MAX_ENTRIES,
    KB_PREFETCH_FETCH_LIMIT,
    KB_PREFETCH_RELEVANCE_THRESHOLD,
    KB_SEED_MIN_CORROBORATING_CHUNKS,
    MilestoneEngine,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    InquiryData,
    NeedPriority,
    NeedPurpose,
    NeedState,
    ProblemVerification,
)
from faultmaven.modules.knowledge.domain.models.knowledge_item import (
    VerificationLevel,
)
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
    build_kb_scope_filter,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Shared fixtures / stubs
# ---------------------------------------------------------------------------


def _case() -> Case:
    """A symptom-verified case (a problem node can be seeded, so a valid cause
    chain can anchor to D)."""
    return Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="u",
        organization_id="o",
        title="t",
        description="X fails",
        state=CaseState.INVESTIGATING,
        inquiry=InquiryData(
            proposed_problem_statement="X fails",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
        problem_verification=ProblemVerification(
            symptom_statement="X fails", severity=CaseSeverity.HIGH
        ),
        current_turn=1,
    )


def _hit(
    parent_id,
    score,
    letters=("A",),
    total_chunks=None,
    chunk_id=None,
    named=("stub",),
    term_coverage=0.5,
):
    """A retrieval hit as the wrapper reads it.

    ``.matched_cause_letters`` is the #1092 join key: which of the parent
    runbook's causes THIS chunk carries. Defaults to Cause A because most tests
    here stub a single-cause runbook; a hit carrying no letters is a non-cause
    chunk and seeds nothing.

    **It carries the #1272 grounding fields, and that is load-bearing.** They
    were absent until #1285, and ``kb_hit_grounding`` reads both through
    ``getattr(..., None)`` — so every hit in this file was UNMEASURED and all
    71 tests below exercised the seeder with the grounding gate NOT APPLYING.
    A hit shape that drifts away from the production one silently switches the
    gate off rather than failing, so the default here is a hit the gate judges
    and admits (``named``), and ``test_the_hit_shape_keeps_the_gate_active``
    pins that.
    """
    return SimpleNamespace(
        parent_document_id=parent_id,
        score=score,
        matched_cause_letters=list(letters),
        total_chunks=total_chunks,
        document_id=chunk_id,
        identity_terms_in_query=list(named),
        term_coverage=term_coverage,
    )


def test_the_hit_shape_keeps_the_gate_active():
    """Positive control on the helper above, not on the seeder.

    Without this, a hit shape that stopped carrying the grounding fields would
    make every seeding test in this file pass for the wrong reason: the gate
    would read the absence as "unmeasured, therefore not judged" and admit
    everything, which is exactly the state these tests are meant to run
    downstream of.
    """
    from faultmaven.core.investigation.kb_grounding import (
        KBSeedGrounding,
        kb_hit_grounding,
    )

    assert kb_hit_grounding(_hit("kb_x", 0.7)) is KBSeedGrounding.NAMED
    assert (
        kb_hit_grounding(_hit("kb_x", 0.7, named=())) is KBSeedGrounding.UNGROUNDED
    ), "a hit with no identity terms must be judged, not waved through"


def _corroborator(parent_id, score=0.5):
    """A second, letter-less chunk of the same runbook.

    The #1144 corroboration guard seeds a runbook only when the turn's retrieval
    surfaced at least ``KB_SEED_MIN_CORROBORATING_CHUNKS`` of its chunks — one
    lone chunk is the signature of an off-domain coincidence, not of a runbook
    that covers the case. Real retrieval supplies the second chunk for free
    (every shipped runbook has >=9 chunks, >=4 of them cause-bearing), so a
    stub handing the wrapper ONE hit per runbook describes a shape production
    does not produce.

    Tests below that are about something else — rank order, cause ordering,
    telemetry labels, loader fan-out — pair each seedable runbook with this so
    the guard is satisfied and the contract under test is what a failure points
    at. The guard itself is pinned in its own section, on hits without it.
    """
    return _hit(parent_id, score, ())


def _good_cause(letter="A", root_stmt="root A: the underlying fault") -> dict:
    """A well-formed pack-shape cause (root → s1 → D) that seeds one hypothesis."""
    return {
        "cause_letter": letter,
        "cause_name": f"Cause {letter}",
        "cause_statement": f"cause {letter} symptom-level statement",
        "chain_nodes": [
            {"ref": "root", "node_type": "root", "statement": root_stmt},
            {"ref": "s1", "node_type": "intermediate", "statement": f"s1 {letter}"},
            {"ref": "D", "node_type": "problem", "statement": "X is failing"},
        ],
        "chain_edges": [
            {"cause_ref": "root", "effect_ref": "s1"},
            {"cause_ref": "s1", "effect_ref": "D"},
        ],
        "rung_indicators": {"root": [f"[Step 1] observable for {letter}"]},
        "is_fallback_cause": False,
    }


class _KnowledgeStub:
    """Records ``get_runbook_causes`` calls and returns a configured value per id.

    ``causes_by_id`` maps item_id -> return value; an id whose value is an
    ``Exception`` instance is raised (to exercise the wrapper's crash isolation)."""

    def __init__(self, causes_by_id=None):
        self.causes_by_id = causes_by_id or {}
        self.calls = []

    async def get_runbook_causes(self, item_id):
        self.calls.append(item_id)
        val = self.causes_by_id.get(item_id)
        if isinstance(val, Exception):
            raise val
        return val


def _engine(knowledge_service, hypothesis_manager=None) -> MilestoneEngine:
    """A MilestoneEngine with only the attributes the seam touches set —
    ``__new__`` skips the heavy constructor."""
    engine = MilestoneEngine.__new__(MilestoneEngine)
    engine.knowledge_service = knowledge_service
    engine.hypothesis_manager = hypothesis_manager or create_hypothesis_manager()
    engine.runbook_kb = None  # dedup honestly skipped in these seams (fm#1030)
    return engine


@pytest.fixture
def enable_seeder(monkeypatch):
    """Turn the flag ON for the wrapper (it is default-OFF in real settings)."""
    monkeypatch.setattr(
        "faultmaven.config.settings.get_settings",
        lambda: SimpleNamespace(features=SimpleNamespace(kb_cause_seeder_enabled=True)),
    )


@pytest.fixture
def seed_spy(monkeypatch):
    """Replace the pure seeder with a spy so the wrapper's *hand-off* (which
    runbooks, in what order) is asserted independently of the seeder internals."""
    calls = []

    def _spy(case, runbooks, current_turn, **kwargs):
        calls.append(
            SimpleNamespace(
                case=case,
                runbooks=runbooks,
                current_turn=current_turn,
                kwargs=kwargs,
            )
        )
        # Mirror the real SeedReport surface the wrapper reads — including
        # ``seeded_anything``, which the outcome counter branches on. A spy that
        # omitted it would raise into the crash handler and quietly turn every
        # spy test into a "crashed" path.
        return SimpleNamespace(seeded_hypothesis_ids=[], seeded_anything=False)

    monkeypatch.setattr(
        "faultmaven.core.investigation.kb_cause_seeder.seed_candidate_causes", _spy
    )
    return calls


# ---------------------------------------------------------------------------
# Wrapper: _seed_candidate_causes_from_kb — flag gate
# ---------------------------------------------------------------------------


async def test_wrapper_flag_off_is_a_noop(monkeypatch, seed_spy):
    # Flag OFF: the wrapper returns before touching the knowledge service or the
    # seeder — the whole feature is dark.
    monkeypatch.setattr(
        "faultmaven.config.settings.get_settings",
        lambda: SimpleNamespace(
            features=SimpleNamespace(kb_cause_seeder_enabled=False)
        ),
    )
    ks = _KnowledgeStub()
    engine = _engine(ks)
    await engine._seed_candidate_causes_from_kb(
        _case(), [_hit("rb1", 0.9), _corroborator("rb1")]
    )
    assert ks.calls == []
    assert seed_spy == []


async def test_wrapper_no_knowledge_service_is_a_noop(enable_seeder, seed_spy):
    engine = _engine(knowledge_service=None)
    # Must not raise despite kb_hits present.
    await engine._seed_candidate_causes_from_kb(
        _case(), [_hit("rb1", 0.9), _corroborator("rb1")]
    )
    assert seed_spy == []


async def test_wrapper_empty_hits_is_a_noop(enable_seeder, seed_spy):
    ks = _KnowledgeStub()
    engine = _engine(ks)
    await engine._seed_candidate_causes_from_kb(_case(), [])
    assert ks.calls == []
    assert seed_spy == []


# ---------------------------------------------------------------------------
# Wrapper: dedup to distinct runbooks, best-score-wins, rank order, cap
# ---------------------------------------------------------------------------


async def test_wrapper_dedups_to_distinct_runbooks_best_score_wins(
    enable_seeder, seed_spy
):
    # Two hits share rb1 (0.4, 0.9); rb2 is 0.7. Distinct runbooks, best score per
    # runbook, ranked by score → rb1(0.9) before rb2(0.7).
    ks = _KnowledgeStub({"rb1": [_good_cause("A")], "rb2": [_good_cause("B")]})
    engine = _engine(ks)
    await engine._seed_candidate_causes_from_kb(
        _case(),
        [
            _hit("rb1", 0.4),
            _hit("rb2", 0.7, ("B",)),
            _corroborator("rb2"),
            _hit("rb1", 0.9),
        ],
    )
    # Consulted in rank order, once per distinct runbook.
    assert ks.calls == ["rb1", "rb2"]
    assert len(seed_spy) == 1
    passed = seed_spy[0].runbooks
    assert [rb.item_id for rb in passed] == ["rb1", "rb2"]
    # best-score-wins: rb1 carries 0.9 (not the 0.4 hit).
    assert passed[0].score == 0.9
    assert passed[1].score == 0.7


async def test_wrapper_skips_hits_without_parent_document_id(enable_seeder, seed_spy):
    # A hit with no parent runbook id (None, or the attribute absent) is not a
    # seedable runbook and is dropped from the dedup.
    ks = _KnowledgeStub({"rb1": [_good_cause("A")]})
    engine = _engine(ks)
    await engine._seed_candidate_causes_from_kb(
        _case(),
        [
            SimpleNamespace(parent_document_id=None, score=0.9),
            SimpleNamespace(score=0.8),  # attribute entirely absent
            _hit("rb1", 0.7),
            _corroborator("rb1"),
        ],
    )
    assert ks.calls == ["rb1"]
    assert [rb.item_id for rb in seed_spy[0].runbooks] == ["rb1"]


async def test_wrapper_caps_runbooks_consulted(enable_seeder, seed_spy):
    # More distinct runbooks than the cap: only the top MAX_SEEDED_RUNBOOKS by
    # score are loaded — the rest are never consulted.
    assert MAX_SEEDED_RUNBOOKS == 2
    ks = _KnowledgeStub(
        {
            "rb1": [_good_cause("A")],
            "rb2": [_good_cause("B")],
            "rb3": [_good_cause("C")],
        }
    )
    engine = _engine(ks)
    await engine._seed_candidate_causes_from_kb(
        _case(),
        [
            _hit("rb1", 0.9),
            _corroborator("rb1"),
            _hit("rb2", 0.8, ("B",)),
            _corroborator("rb2"),
            _hit("rb3", 0.7, ("C",)),
            _corroborator("rb3"),
        ],
    )
    assert ks.calls == ["rb1", "rb2"]
    assert "rb3" not in ks.calls
    assert [rb.item_id for rb in seed_spy[0].runbooks] == ["rb1", "rb2"]


# ---------------------------------------------------------------------------
# Wrapper: seed the causes RETRIEVAL matched, not the runbook's first N (#1092)
# ---------------------------------------------------------------------------


async def test_wrapper_seeds_only_the_causes_retrieval_matched(enable_seeder, seed_spy):
    """The #1092 regression, in its exact shape.

    A runbook whose author order is A, B, C, D, where the chunk that matched is
    D's. The wrapper used to discard which chunk matched, re-fetch the whole
    causes record and hand the seeder A, B, C — three causes the query never
    surfaced — while D, the one that did, fell past MAX_SEEDED_CAUSES. This is
    the live incident: a k8s OOMKilled/exit-137 case seeded the GKE runbook's
    three *unschedulable* causes and never reached its OOMKilled cause.
    """
    causes = [_good_cause(x) for x in ("A", "B", "C", "D")]
    ks = _KnowledgeStub({"rb1": causes})
    engine = _engine(ks)
    await engine._seed_candidate_causes_from_kb(
        _case(), [_hit("rb1", 0.9, ("D",)), _corroborator("rb1")]
    )

    passed = seed_spy[0].runbooks
    assert [c["cause_letter"] for c in passed[0].causes] == ["D"]


async def test_wrapper_orders_matched_causes_by_retrieval_score(
    enable_seeder, seed_spy
):
    # Two chunks of one runbook matched: C scored above A. The seeder consumes
    # causes in the order handed to it, so retrieval order must survive the
    # hand-off — author order (A before C) must NOT win.
    ks = _KnowledgeStub({"rb1": [_good_cause(x) for x in ("A", "B", "C")]})
    engine = _engine(ks)
    await engine._seed_candidate_causes_from_kb(
        _case(), [_hit("rb1", 0.6, ("A",)), _hit("rb1", 0.9, ("C",))]
    )

    assert [c["cause_letter"] for c in seed_spy[0].runbooks[0].causes] == ["C", "A"]


async def test_wrapper_ties_keep_author_order(enable_seeder, seed_spy):
    # One chunk carrying two headings gives both causes the same score. The sort
    # is stable, so the author's own most-likely-first order breaks the tie
    # rather than an arbitrary one.
    ks = _KnowledgeStub({"rb1": [_good_cause(x) for x in ("A", "B", "C")]})
    engine = _engine(ks)
    await engine._seed_candidate_causes_from_kb(
        _case(), [_hit("rb1", 0.9, ("C", "A")), _corroborator("rb1")]
    )

    assert [c["cause_letter"] for c in seed_spy[0].runbooks[0].causes] == ["A", "C"]


async def test_wrapper_non_cause_chunk_hit_seeds_nothing(enable_seeder, seed_spy):
    # A hit on a runbook's Symptom Recognition / Diagnostic Steps / Prevention
    # chunk carries no cause letter. It shows the runbook is topically relevant,
    # never that any particular cause of it applies — so nothing seeds, and the
    # causes record is not even loaded.
    ks = _KnowledgeStub({"rb1": [_good_cause("A")]})
    engine = _engine(ks)
    await engine._seed_candidate_causes_from_kb(_case(), [_hit("rb1", 0.95, ())])

    assert ks.calls == []
    assert seed_spy == []


async def test_wrapper_ranks_runbooks_by_best_matched_cause(enable_seeder, seed_spy):
    # rb2's top chunk (0.95) is a non-cause chunk; its matched CAUSE scored 0.4.
    # rb1's matched cause scored 0.7. Ranking on best-any-chunk would put rb2
    # first; ranking on best matched cause — the only score that speaks to a
    # cause — puts rb1 first.
    ks = _KnowledgeStub({"rb1": [_good_cause("A")], "rb2": [_good_cause("B")]})
    engine = _engine(ks)
    await engine._seed_candidate_causes_from_kb(
        _case(),
        [
            _hit("rb2", 0.95, ()),
            _hit("rb1", 0.70, ("A",)),
            _corroborator("rb1"),
            _hit("rb2", 0.40, ("B",)),
        ],
    )

    assert ks.calls == ["rb1", "rb2"]
    assert [rb.item_id for rb in seed_spy[0].runbooks] == ["rb1", "rb2"]
    assert seed_spy[0].runbooks[0].score == 0.70


async def test_wrapper_letter_naming_no_cause_in_record_is_dropped(
    enable_seeder, seed_spy
):
    # A chunk heading names Cause E but the causes record holds only A — a
    # produce-side inconsistency. Drop that runbook rather than fall back to
    # seeding whatever the record happens to hold.
    ks = _KnowledgeStub({"rb1": [_good_cause("A")]})
    engine = _engine(ks)
    await engine._seed_candidate_causes_from_kb(
        _case(), [_hit("rb1", 0.9, ("E",)), _corroborator("rb1")]
    )

    assert ks.calls == ["rb1"]
    assert seed_spy == []


# ---------------------------------------------------------------------------
# Wrapper: corroboration guard (#1144) — a runbook must be matched BROADLY
# before any of its causes may be asserted as a candidate root cause
# ---------------------------------------------------------------------------


async def test_lone_cause_chunk_does_not_seed(enable_seeder, seed_spy):
    """The #1144 regression, in its exact shape.

    A wrong-domain runbook surfaces on ONE chunk that happens to carry a cause
    heading, and rank alone promoted it to a candidate root cause: a live k8s
    OOMKilled case was seeded an NGINX-502 chain and a MongoDB WiredTiger chain,
    and wore the NGINX text in its case header as the working conclusion.

    One chunk is not enough to assert a cause. The runbook's prose still reaches
    the LLM through kb_context — what is withheld is the ASSERTION.
    """
    ks = _KnowledgeStub({"rb_offdomain": [_good_cause("A")]})
    engine = _engine(ks)
    await engine._seed_candidate_causes_from_kb(_case(), [_hit("rb_offdomain", 0.9)])

    assert ks.calls == []  # not even looked up — declined before the load
    assert seed_spy == []


async def test_second_chunk_of_the_same_runbook_unlocks_seeding(
    enable_seeder, seed_spy
):
    """The other half of the guard: breadth of match is what admits a runbook.

    Same cause chunk, same score, same everything — plus one more chunk of the
    SAME runbook. That is the whole difference between a lexical coincidence in
    one paragraph and a document that is actually about this failure, and it is
    what separated the two populations when the alternatives were measured (a
    score floor could not: on-domain seeds scored 0.603-0.731 and off-domain
    ones 0.519-0.715).
    """
    ks = _KnowledgeStub({"rb1": [_good_cause("A")]})
    engine = _engine(ks)
    await engine._seed_candidate_causes_from_kb(
        _case(), [_hit("rb1", 0.9), _corroborator("rb1")]
    )

    assert [rb.item_id for rb in seed_spy[0].runbooks] == ["rb1"]


async def test_corroborating_chunk_need_not_carry_a_cause(enable_seeder, seed_spy):
    """Corroboration counts CHUNKS of the runbook, not cause chunks.

    A runbook that covers the case matches across its sections — Symptom
    Recognition, Diagnostic Steps, Sources — and those carry no cause letter.
    Requiring the second chunk to be a cause chunk too would measure how a
    runbook is chunked rather than how well it matches.
    """
    ks = _KnowledgeStub({"rb1": [_good_cause("A")]})
    engine = _engine(ks)
    await engine._seed_candidate_causes_from_kb(
        _case(), [_hit("rb1", 0.9, ("A",)), _hit("rb1", 0.4, ())]
    )

    assert [rb.item_id for rb in seed_spy[0].runbooks] == ["rb1"]


async def test_one_chunk_returned_twice_does_not_corroborate_itself(
    enable_seeder, seed_spy
):
    """Corroboration counts DISTINCT chunks, not hits.

    The two are identical today — one vector search returns each chunk at most
    once — so nothing exercises the difference in production yet. It is pinned
    because the day they diverge is the day the guard fails OPEN: a hybrid/BM25
    merge returning the same chunk from both arms would let one chunk corroborate
    itself and silently restore the #1144 behaviour with the guard still
    apparently in place.
    """
    ks = _KnowledgeStub({"rb1": [_good_cause("A")]})
    engine = _engine(ks)
    await engine._seed_candidate_causes_from_kb(
        _case(),
        [
            _hit("rb1", 0.9, chunk_id="rb1_chunk_4", total_chunks=14),
            # Same chunk, second retrieval arm, different score.
            _hit("rb1", 0.7, chunk_id="rb1_chunk_4", total_chunks=14),
        ],
    )

    assert ks.calls == []
    assert seed_spy == []


async def test_two_genuinely_distinct_chunks_still_corroborate(enable_seeder, seed_spy):
    """The other side of the same rule: distinct ids corroborate as before, so
    the dedup cannot be mistaken for a tightening of the threshold."""
    ks = _KnowledgeStub({"rb1": [_good_cause("A")]})
    engine = _engine(ks)
    await engine._seed_candidate_causes_from_kb(
        _case(),
        [
            _hit("rb1", 0.9, chunk_id="rb1_chunk_4", total_chunks=14),
            _hit("rb1", 0.7, (), chunk_id="rb1_chunk_9", total_chunks=14),
        ],
    )

    assert [rb.item_id for rb in seed_spy[0].runbooks] == ["rb1"]


async def test_hits_without_a_chunk_id_each_count_as_their_own_chunk(
    enable_seeder, seed_spy
):
    """A MISSING id is not the failure mode being closed; a REPEATED one is.

    Collapsing anonymous hits together would tighten the guard against a source
    that never duplicated anything, so they stay distinct — which is also what
    keeps every other test in this file describing the behaviour it means to.
    """
    ks = _KnowledgeStub({"rb1": [_good_cause("A")]})
    engine = _engine(ks)
    await engine._seed_candidate_causes_from_kb(
        _case(), [_hit("rb1", 0.9, chunk_id=None), _hit("rb1", 0.7, (), chunk_id=None)]
    )

    assert [rb.item_id for rb in seed_spy[0].runbooks] == ["rb1"]


async def test_guard_is_per_runbook_not_per_result_set(enable_seeder, seed_spy):
    """A corroborated runbook is not vouched for by its neighbours.

    rb1 arrives on two chunks, rb2 on one. The result set is 'broad' overall,
    but breadth only means anything WITHIN one document — so rb1 seeds and rb2
    does not.
    """
    ks = _KnowledgeStub({"rb1": [_good_cause("A")], "rb2": [_good_cause("B")]})
    engine = _engine(ks)
    await engine._seed_candidate_causes_from_kb(
        _case(),
        [_hit("rb1", 0.9), _corroborator("rb1"), _hit("rb2", 0.95, ("B",))],
    )

    assert ks.calls == ["rb1"]
    assert [rb.item_id for rb in seed_spy[0].runbooks] == ["rb1"]


async def test_guard_declines_regardless_of_score(enable_seeder, seed_spy):
    """The guard is not a score floor wearing a different name.

    A lone chunk at the top of the ranking is still a lone chunk. This is the
    measured point of the whole change: score does not separate on-domain from
    off-domain seeds, so nothing here may quietly key on it.
    """
    ks = _KnowledgeStub({"rb1": [_good_cause("A")]})
    engine = _engine(ks)
    await engine._seed_candidate_causes_from_kb(_case(), [_hit("rb1", 0.99)])

    assert seed_spy == []


async def test_a_one_chunk_runbook_corroborates_itself(enable_seeder, seed_spy):
    """A document that IS one chunk matches COMPLETELY when that chunk matches.

    Corroboration asks whether a runbook matched broadly, and breadth only means
    anything against the document's own length. A flat threshold read a whole
    one-chunk document as a marginal match and made it permanently unseedable —
    and compact documents are the flywheel's own output: a runbook authored
    through ``POST /knowledge/runbooks/create``, or converted from a resolved
    case, chunks whole under the chunker's section budget. The owner-aware
    prefetch scope exists to seed exactly those.
    """
    ks = _KnowledgeStub({"rb_personal": [_good_cause("A")]})
    engine = _engine(ks)
    await engine._seed_candidate_causes_from_kb(
        _case(), [_hit("rb_personal", 0.9, total_chunks=1)]
    )

    assert [rb.item_id for rb in seed_spy[0].runbooks] == ["rb_personal"]


async def test_a_long_runbook_on_one_chunk_still_declines(enable_seeder, seed_spy):
    """The length exemption is not a hole in the guard.

    Same single hit, same score — the only difference is that this document has
    fourteen chunks and surfaced one of them. That is the #1144 shape and it
    stays declined.
    """
    ks = _KnowledgeStub({"rb_long": [_good_cause("A")]})
    engine = _engine(ks)
    await engine._seed_candidate_causes_from_kb(
        _case(), [_hit("rb_long", 0.9, total_chunks=14)]
    )

    assert seed_spy == []


async def test_absent_length_stamp_reads_as_unknown_not_small(enable_seeder, seed_spy):
    """Pre-stamp content must not be waved through by MISSING metadata.

    An absent ``total_chunks`` is 'unknown', so the full threshold applies — the
    behaviour such content had before the exemption existed. Reading absence as
    'small' would let any unstamped document seed on one lone chunk, which is the
    defect wearing a disguise.
    """
    ks = _KnowledgeStub({"rb1": [_good_cause("A")]})
    engine = _engine(ks)
    await engine._seed_candidate_causes_from_kb(
        _case(), [_hit("rb1", 0.9, total_chunks=None)]
    )

    assert seed_spy == []


async def test_uncorroborated_counter_ignores_runbooks_never_consultable(
    enable_seeder,
):
    """The guard's COST is what it turned away, not everything it looked at.

    Two corroborated runbooks fill both MAX_SEEDED_RUNBOOKS slots and seed
    normally; three lone-chunk runbooks rank below them and could not have been
    consulted even with the guard off. Nothing was lost, so nothing is counted —
    otherwise the number the threshold gets re-sized from reports a price the
    guard never charged.
    """
    ks = _KnowledgeStub({"rb1": [_good_cause("A")], "rb2": [_good_cause("B")]})
    engine = _engine(ks)
    with patch(
        "faultmaven.core.investigation.milestone_engine."
        "kb_cause_seed_uncorroborated_total"
    ) as c:
        await engine._seed_candidate_causes_from_kb(
            _case(),
            [
                _hit("rb1", 0.90),
                _corroborator("rb1"),
                _hit("rb2", 0.85, ("B",)),
                _corroborator("rb2"),
                _hit("rb_low1", 0.60, ("A",)),
                _hit("rb_low2", 0.55, ("A",)),
                _hit("rb_low3", 0.50, ("A",)),
            ],
        )
    c.inc.assert_not_called()


async def test_uncorroborated_counter_counts_a_decline_that_cost_a_slot(
    enable_seeder,
):
    """The other half: a lone-chunk runbook that OUTRANKED the corroborated one
    would have been consulted, so declining it genuinely cost a seed and is
    counted."""
    ks = _KnowledgeStub({"rb1": [_good_cause("A")]})
    engine = _engine(ks)
    with patch(
        "faultmaven.core.investigation.milestone_engine."
        "kb_cause_seed_uncorroborated_total"
    ) as c:
        await engine._seed_candidate_causes_from_kb(
            _case(),
            [
                _hit("rb_top", 0.95, ("A",), total_chunks=14),
                _hit("rb1", 0.70),
                _corroborator("rb1"),
            ],
        )
    c.inc.assert_called_once_with(1)


def test_corroboration_threshold_is_reachable_within_the_fetch_depth():
    """The threshold means nothing except relative to how deep retrieval fetches.

    Halve KB_PREFETCH_FETCH_LIMIT and the same threshold silently becomes a much
    harder bar; raise the threshold past the depth and NOTHING can ever seed.
    Pinned together so a change to either has to come here and say so.
    """
    assert KB_SEED_MIN_CORROBORATING_CHUNKS >= 2, "1 chunk is the defect (#1144)"
    assert KB_SEED_MIN_CORROBORATING_CHUNKS < KB_PREFETCH_FETCH_LIMIT
    # Room for at least MAX_SEEDED_RUNBOOKS runbooks to corroborate at once,
    # or the guard would cap fan-out below the seeder's own cap by accident.
    assert KB_SEED_MIN_CORROBORATING_CHUNKS * MAX_SEEDED_RUNBOOKS <= (
        KB_PREFETCH_FETCH_LIMIT
    )


# ---------------------------------------------------------------------------
# Wrapper: outcome telemetry (#1092) — the labels are exclusive and sum to
# attempts, so `seeded`/total is a yield and its complement the zero-seed rate
# ---------------------------------------------------------------------------


def _counters():
    return patch.multiple(
        "faultmaven.core.investigation.milestone_engine",
        kb_cause_seed_attempt_total=DEFAULT,
        kb_cause_seed_letter_mismatch_total=DEFAULT,
    )


def _outcomes(m):
    """The ``outcome=`` labels recorded on the attempt counter, in call order."""
    return [
        c.kwargs["outcome"]
        for c in m["kb_cause_seed_attempt_total"].labels.call_args_list
    ]


async def test_outcome_counter_records_seeded(enable_seeder):
    ks = _KnowledgeStub({"rb1": [_good_cause("A")]})
    engine = _engine(ks)
    with _counters() as m:
        await engine._seed_candidate_causes_from_kb(
            _case(), [_hit("rb1", 0.9), _corroborator("rb1")]
        )
    assert _outcomes(m) == ["seeded"]


async def test_outcome_counter_records_the_recall_trade(enable_seeder):
    """A hit on a non-cause chunk is the ONE outcome the author-order fallback
    used to cover. It is labeled distinctly so the recall side of removing that
    fallback is measurable in telemetry rather than assumed."""
    ks = _KnowledgeStub({"rb1": [_good_cause("A")]})
    engine = _engine(ks)
    with _counters() as m:
        await engine._seed_candidate_causes_from_kb(_case(), [_hit("rb1", 0.9, ())])
    assert _outcomes(m) == ["no_cause_chunk_matched"]


async def test_outcome_counter_records_no_seedable_cause(enable_seeder):
    ks = _KnowledgeStub({"rb1": None})
    engine = _engine(ks)
    with _counters() as m:
        await engine._seed_candidate_causes_from_kb(
            _case(), [_hit("rb1", 0.9), _corroborator("rb1")]
        )
    assert _outcomes(m) == ["no_seedable_cause"]


async def test_outcome_counter_records_all_causes_skipped(enable_seeder):
    # The fallback cause is an INTENTIONAL skip, so the seeder is handed a cause
    # and creates nothing — a zero-seed that is NOT a retrieval problem.
    fallback = _good_cause("Z")
    fallback["is_fallback_cause"] = True
    ks = _KnowledgeStub({"rb1": [fallback]})
    engine = _engine(ks)
    with _counters() as m:
        await engine._seed_candidate_causes_from_kb(
            _case(), [_hit("rb1", 0.9, ("Z",)), _corroborator("rb1")]
        )
    assert _outcomes(m) == ["all_causes_skipped"]


async def test_outcome_counter_records_crash_and_only_crash(enable_seeder, monkeypatch):
    """A seeder crash must land on ``crashed`` ALONE. Counting the success label
    before the call would double-count the attempt and inflate the yield."""

    def _boom(*a, **k):
        raise RuntimeError("seeder bug")

    monkeypatch.setattr(
        "faultmaven.core.investigation.kb_cause_seeder.seed_candidate_causes", _boom
    )
    ks = _KnowledgeStub({"rb1": [_good_cause("A")]})
    engine = _engine(ks)
    with _counters() as m:
        await engine._seed_candidate_causes_from_kb(
            _case(), [_hit("rb1", 0.9), _corroborator("rb1")]
        )
    assert _outcomes(m) == ["crashed"]


async def test_outcome_counter_separates_the_guard_from_a_retrieval_miss(
    enable_seeder,
):
    """A cause DID match; the #1144 guard declined it. That is the guard's cost,
    not retrieval landing on prose, and folding it into ``no_cause_chunk_matched``
    would hide it inside a counter that already means something else."""
    ks = _KnowledgeStub({"rb1": [_good_cause("A")]})
    engine = _engine(ks)
    with _counters() as m:
        await engine._seed_candidate_causes_from_kb(_case(), [_hit("rb1", 0.9)])
    assert _outcomes(m) == ["no_corroborated_runbook"]


async def test_uncorroborated_counter_sizes_the_guard_per_runbook(enable_seeder):
    """The sizing surface the threshold gets re-tuned from: one increment per
    DECLINED RUNBOOK, not per chunk and not per cause."""
    ks = _KnowledgeStub({})
    engine = _engine(ks)
    with patch(
        "faultmaven.core.investigation.milestone_engine."
        "kb_cause_seed_uncorroborated_total"
    ) as c:
        await engine._seed_candidate_causes_from_kb(
            _case(),
            # rb1 declined on two lone cause letters (still ONE runbook);
            # rb2 declined on one; rb3 corroborated, so never counted.
            [
                _hit("rb1", 0.9, ("A", "B")),
                _hit("rb2", 0.8, ("A",)),
                _hit("rb3", 0.7, ("A",)),
                _corroborator("rb3"),
            ],
        )
    c.inc.assert_called_once_with(2)


async def test_no_attempt_counted_when_retrieval_returned_nothing(enable_seeder):
    """The attempt counter measures what SEEDING did with hits. A turn with no
    hits at all is a KB miss, not a seeding outcome — counting it would dilute
    the zero-seed rate with retrieval misses."""
    engine = _engine(_KnowledgeStub())
    with _counters() as m:
        await engine._seed_candidate_causes_from_kb(_case(), [])
    assert _outcomes(m) == []


async def test_letter_mismatch_is_counted_not_just_logged(enable_seeder):
    """The produce-side integrity alarm. The shipped pack is pinned by a corpus
    test; generated/uploaded runbooks are not, so this counter is the only
    sighting of that drift in production."""
    ks = _KnowledgeStub({"rb1": [_good_cause("A")]})
    engine = _engine(ks)
    with _counters() as m:
        await engine._seed_candidate_causes_from_kb(
            _case(), [_hit("rb1", 0.9, ("E",)), _corroborator("rb1")]
        )
    assert m["kb_cause_seed_letter_mismatch_total"].inc.call_count == 1
    assert _outcomes(m) == ["no_seedable_cause"]


# ---------------------------------------------------------------------------
# Wrapper: get_runbook_causes None / [] / mixed → only causes-bearing runbooks seed
# ---------------------------------------------------------------------------


async def test_wrapper_all_runbooks_without_causes_seeds_nothing(
    enable_seeder, seed_spy
):
    # Matched runbooks that carry no causes record (None or []) → the flat-prose
    # path serves them; the seeder is never invoked (a legitimate zero-seed).
    ks = _KnowledgeStub({"rb1": None, "rb2": []})
    engine = _engine(ks)
    await engine._seed_candidate_causes_from_kb(
        _case(),
        [
            _hit("rb1", 0.9),
            _corroborator("rb1"),
            _hit("rb2", 0.8),
            _corroborator("rb2"),
        ],
    )
    assert ks.calls == ["rb1", "rb2"]  # both were looked up
    assert seed_spy == []  # but none carried causes → seeder not called


async def test_wrapper_mixed_causes_seeds_only_causes_bearing_runbooks(
    enable_seeder, seed_spy
):
    # One runbook carries causes, one does not → only the causes-bearing one is
    # handed to the seeder.
    ks = _KnowledgeStub({"rb1": [_good_cause("A")], "rb2": None})
    engine = _engine(ks)
    await engine._seed_candidate_causes_from_kb(
        _case(),
        [
            _hit("rb1", 0.9),
            _corroborator("rb1"),
            _hit("rb2", 0.8),
            _corroborator("rb2"),
        ],
    )
    assert ks.calls == ["rb1", "rb2"]
    assert [rb.item_id for rb in seed_spy[0].runbooks] == ["rb1"]


# ---------------------------------------------------------------------------
# Wrapper: crash isolation — a seeder bug never breaks the transition
# ---------------------------------------------------------------------------


async def test_wrapper_swallows_loader_error(enable_seeder, seed_spy):
    # get_runbook_causes raising (its own contract is None-on-error, but defend in
    # depth) must not propagate out of the transition.
    ks = _KnowledgeStub({"rb1": RuntimeError("boom")})
    engine = _engine(ks)
    await engine._seed_candidate_causes_from_kb(
        _case(), [_hit("rb1", 0.9), _corroborator("rb1")]
    )
    # Did not raise; and because the load blew up, the seeder never ran.
    assert seed_spy == []


async def test_wrapper_swallows_seeder_crash(enable_seeder, monkeypatch):
    # A crash inside the pure seeder is a seeder bug, not a no-match — it must be
    # swallowed (logged elsewhere) so the INQUIRY→INVESTIGATING transition still
    # completes.
    def _boom(*a, **k):
        raise RuntimeError("seeder bug")

    monkeypatch.setattr(
        "faultmaven.core.investigation.kb_cause_seeder.seed_candidate_causes", _boom
    )
    ks = _KnowledgeStub({"rb1": [_good_cause("A")]})
    engine = _engine(ks)
    # Must complete without raising.
    await engine._seed_candidate_causes_from_kb(
        _case(), [_hit("rb1", 0.9), _corroborator("rb1")]
    )


# ---------------------------------------------------------------------------
# Wrapper: happy path end-to-end through the REAL seeder (no spy)
# ---------------------------------------------------------------------------


async def test_wrapper_happy_path_seeds_through_real_seeder(enable_seeder):
    # No spy: prove the wrapper builds SeededRunbooks the real seeder can consume,
    # producing candidate nodes/hypotheses on the case.
    ks = _KnowledgeStub({"rb1": [_good_cause("A")]})
    case = _case()
    engine = _engine(ks)
    await engine._seed_candidate_causes_from_kb(
        case, [_hit("rb1", 0.9), _corroborator("rb1")]
    )

    assert len(case.hypotheses) == 1
    h = next(iter(case.hypotheses.values()))
    assert h.rationale and "rb1" in h.rationale
    seeded_nodes = [
        n
        for n in case.causal_nodes.values()
        if (n.metadata or {}).get(SEEDED_FROM_RUNBOOK_KEY) == "rb1"
    ]
    assert seeded_nodes  # provenance stamped on the newly-minted nodes


@pytest.mark.asyncio
async def test_wrapper_seeds_rung_evidence_needs(enable_seeder):
    # End-to-end through the flag-ON engine wrapper: a seeded cause's rung
    # indicators land as PENDING causal evidence-needs on the case (R8). The
    # flag-OFF counterpart is test_wrapper_flag_off_is_a_noop — no seeding, so no
    # needs.
    ks = _KnowledgeStub({"rb1": [_good_cause("A")]})
    case = _case()
    engine = _engine(ks)
    await engine._seed_candidate_causes_from_kb(
        case, [_hit("rb1", 0.9), _corroborator("rb1")]
    )

    hyp_id = next(iter(case.hypotheses))
    needs = [n for n in case.evidence_needs if hyp_id in n.motivating_hypothesis_ids]
    assert needs  # the seeded hypothesis arrived carrying its discriminators
    for n in needs:
        assert n.purpose == NeedPurpose.CAUSAL_VERIFICATION
        assert n.state == NeedState.PENDING
        assert n.priority == NeedPriority.LOW
        assert n.request_text == "observable for A"  # [Step N] prefix stripped


# ---------------------------------------------------------------------------
# Scope filter: build_kb_scope_filter — the single source of KB read isolation
# ---------------------------------------------------------------------------


def test_scope_filter_global_only_when_no_owner():
    assert build_kb_scope_filter(None) == {"scope": "global"}
    assert build_kb_scope_filter("") == {"scope": "global"}


def test_scope_filter_global_union_owner():
    # global ∪ owned (any scope). The owner arm is scope-agnostic — an author
    # always sees their own items (ADR-013 §D4 / ADR-011 D3).
    assert build_kb_scope_filter("user_a") == {
        "$or": [
            {"scope": "global"},
            {"owner_id": "user_a"},
        ]
    }


def test_scope_filter_includes_shared_id_allowlist():
    # The team arm is now a pre-resolved id allowlist (from the share table),
    # matched against the chunk's parent_document_id — never scope/team_id
    # metadata (which would orphan a chunk on unshare).
    assert build_kb_scope_filter("user_a", ["kb_1", "kb_2"]) == {
        "$or": [
            {"scope": "global"},
            {"owner_id": "user_a"},
            {"parent_document_id": {"$in": ["kb_1", "kb_2"]}},
        ]
    }


def test_scope_filter_owner_condition_keyed_on_owner_only():
    # Isolation invariant: the only owner condition is keyed on the given
    # owner — never another user's id.
    f = build_kb_scope_filter("user_b", ["kb_1"])
    owner = [c for c in f["$or"] if "owner_id" in c]
    assert owner == [{"owner_id": "user_b"}]


# ---------------------------------------------------------------------------
# Pre-fetch: _prefetch_kb_context — owner-aware scope + cross-user isolation
# ---------------------------------------------------------------------------


class _SearchRecordingStub:
    """Records the ``filters`` + ``limit`` passed to ``search_knowledge`` and
    returns a configured result list (so the pre-fetch can build
    ``case.kb_context``).

    The retrieval-mode arguments are recorded too rather than absorbed by a
    bare ``**kwargs``: the pre-fetch's choice of hybrid retrieval and its
    admission floor are part of the seam this class exists to pin (#1272), and
    a stub that silently swallowed them would keep passing if the engine
    stopped asking for either.
    """

    def __init__(self, results=None):
        self.results = results or []
        self.filters_seen = []
        self.limits_seen = []
        self.hybrid_seen = []
        self.min_score_seen = []

    async def search_knowledge(
        self, query, limit=10, filters=None, use_hybrid=False, min_score=None
    ):
        self.filters_seen.append(filters)
        self.limits_seen.append(limit)
        self.hybrid_seen.append(use_hybrid)
        self.min_score_seen.append(min_score)
        return self.results


def _search_hit(score=0.9, parent_id="rb1", letters=("A",)):
    return SimpleNamespace(
        title="t",
        snippet="s",
        score=score,
        document_type="runbook",
        parent_document_id=parent_id,
        matched_cause_letters=list(letters),
    )


def test_prefetch_threshold_tracks_the_qa_tool_threshold():
    """Both floors read the same score, so they must carry the same number.

    ``_prefetch_kb_context`` filters ``SearchResult.score``, which
    ``KnowledgeService.search_knowledge`` passes through verbatim from
    ``KnowledgeVectorStore.search`` — the identical scale the QA tool's
    ``relevance_threshold`` is calibrated against. They drifted apart in the
    fix for #1072 exactly because nothing tied them together: the QA-tool floor
    was the visible symptom and got the attention, while this one filters
    silently and would have kept dropping on-topic runbooks.
    """
    from faultmaven.modules.agent.tools.kb_configs.unified_kb_config import (
        UnifiedKBConfig,
    )

    assert KB_PREFETCH_RELEVANCE_THRESHOLD == UnifiedKBConfig().relevance_threshold


async def test_prefetch_keeps_weakest_on_topic_and_drops_adjacent_off_topic():
    """The measured calibration, applied on this path (#1072).

    0.591 is the weakest on-topic retrieval measured against the shipped KB
    (a query whose correct runbook names its distinguishing keyword verbatim);
    0.477 is the strongest off-topic one (ZooKeeper -> Kafka via shared
    vocabulary). The floor must sit between them here, as it does for the QA
    tool.
    """
    ks = _SearchRecordingStub(
        [
            _search_hit(score=0.591, parent_id="on-topic"),
            _search_hit(score=0.477, parent_id="off-topic"),
        ]
    )
    engine = _engine(ks)
    case = _case()

    relevant = await engine._prefetch_kb_context(case, "X fails", "symptom")

    assert [r.parent_document_id for r in relevant] == ["on-topic"]


async def test_prefetch_scope_is_global_union_owner():
    # The pre-fetch must search global PLUS the case owner's own KB — otherwise
    # personal (case-generated) runbooks never seed. The team arm is wired but
    # resolves empty when no team_service/share_repository is attached (standalone,
    # or a bare __new__ engine), so the scope collapses to global ∪ owner.
    ks = _SearchRecordingStub([_search_hit()])
    engine = _engine(ks)
    case = _case()  # user_id="u"
    await engine._prefetch_kb_context(case, "X fails", "symptom")

    assert ks.filters_seen == [
        {
            "$or": [
                {"scope": "global"},
                {"owner_id": "u"},
            ]
        }
    ]


async def test_prefetch_team_arm_uses_owner_shared_runbooks():
    # When team_service + share_repository are attached (Cloud), the seeder
    # widens the OWNER's scope with the runbooks shared to the OWNER's teams —
    # keyed on case.user_id, NOT the session user. Inert until conversion emits
    # team-shared runbooks, but the plumbing must resolve through the share table.
    ks = _SearchRecordingStub([_search_hit()])
    engine = _engine(ks)
    engine.team_service = SimpleNamespace(
        list_all_user_team_ids=AsyncMock(return_value=["team_1"])
    )
    engine.share_repository = SimpleNamespace(
        list_resource_ids=AsyncMock(return_value=["rb_team_a"])
    )
    case = _case()
    case.user_id = "owner_b"
    await engine._prefetch_kb_context(case, "X fails", "symptom")

    scope_filter = ks.filters_seen[0]
    assert {"parent_document_id": {"$in": ["rb_team_a"]}} in scope_filter["$or"]
    assert {"owner_id": "owner_b"} in scope_filter["$or"]
    # Teams resolved for the CASE OWNER, not any session user.
    engine.team_service.list_all_user_team_ids.assert_awaited_once_with("owner_b")
    engine.share_repository.list_resource_ids.assert_awaited_once_with(
        resource_type="knowledge_item",
        scope_type="team",
        scope_ids=["team_1"],
        # The share row must belong to the CASE's tenant (#879).
        organization_id="o",
    )


async def test_prefetch_owner_condition_keyed_on_this_case_owner():
    # Cross-user isolation: the owner condition is keyed on THIS case's owner.
    # A case owned by user_b can only ever surface user_b's own runbooks —
    # never another user's.
    ks = _SearchRecordingStub([_search_hit()])
    engine = _engine(ks)
    case = _case()
    case.user_id = "user_b"
    await engine._prefetch_kb_context(case, "X fails", "symptom")

    scope_filter = ks.filters_seen[0]
    owner = [c for c in scope_filter["$or"] if "owner_id" in c]
    assert owner == [{"owner_id": "user_b"}]
    # No other user's scope leaks in.
    assert all(c.get("owner_id") in (None, "user_b") for c in scope_filter["$or"])


async def test_prefetch_global_only_when_no_owner():
    # An owner-less case (user_id cleared after account deletion) falls back to
    # a plain global scope — never an unfiltered cross-tenant read.
    ks = _SearchRecordingStub([_search_hit()])
    engine = _engine(ks)
    case = _case()
    case.user_id = None
    await engine._prefetch_kb_context(case, "X fails", "symptom")

    assert ks.filters_seen == [{"scope": "global"}]


# ---------------------------------------------------------------------------
# Pre-fetch: fetch depth vs. prompt-surface cap — the seeder's parent-runbook
# dedup needs chunk diversity that a limit-3 fetch starves
# ---------------------------------------------------------------------------


async def test_prefetch_fetches_deeper_than_the_prompt_surface():
    # The fetch depth is the deeper constant (so a long runbook's top chunks don't
    # crowd out a second runbook), NOT the prompt-surface cap.
    assert KB_PREFETCH_FETCH_LIMIT > KB_CONTEXT_MAX_ENTRIES
    ks = _SearchRecordingStub([_search_hit()])
    engine = _engine(ks)
    await engine._prefetch_kb_context(_case(), "X fails", "symptom")
    assert ks.limits_seen == [KB_PREFETCH_FETCH_LIMIT]


async def test_prefetch_depth_lets_second_runbook_reach_the_seeder():
    # Starvation regression: three chunks of runbook A rank above one chunk of
    # runbook B (all >= 0.3). A limit-3 fetch would return only A's chunks and the
    # parent-dedup would collapse to ONE runbook. The deeper fetch returns all
    # four, so the seeder's parent-dedup sees BOTH parents, in ranked order.
    results = [
        _search_hit(score=0.90, parent_id="rb_a"),
        _search_hit(score=0.85, parent_id="rb_a"),
        _search_hit(score=0.80, parent_id="rb_a"),
        _search_hit(score=0.75, parent_id="rb_b", letters=("B",)),
    ]
    ks = _SearchRecordingStub(results)
    engine = _engine(ks)
    case = _case()
    relevant = await engine._prefetch_kb_context(case, "X fails", "symptom")

    # The full ranked list is returned — this is exactly what the seeder's
    # parent-dedup consumes.
    assert len(relevant) == 4
    # Distinct parents, in ranked order: BOTH A and B reach the seeder.
    seen = []
    for r in relevant:
        pid = r.parent_document_id
        if pid not in seen:
            seen.append(pid)
    assert seen == ["rb_a", "rb_b"]

    # The prompt surface stays capped at the top KB_CONTEXT_MAX_ENTRIES, and is
    # byte-identical to the top-N slice of the ranked results.
    assert len(case.kb_context) == KB_CONTEXT_MAX_ENTRIES
    assert [c["parent_document_id"] for c in case.kb_context] == [
        r.parent_document_id for r in relevant[:KB_CONTEXT_MAX_ENTRIES]
    ]
    assert [c["score"] for c in case.kb_context] == [
        r.score for r in relevant[:KB_CONTEXT_MAX_ENTRIES]
    ]


class _PrefetchAndCausesStub:
    """A knowledge_service exposing BOTH seams the engine path touches: the
    prefetch's ``search_knowledge`` and the seeder's ``get_runbook_causes`` — so a
    single test can drive prefetch → seed end-to-end."""

    def __init__(self, search_results, causes_by_id):
        self.search_results = search_results
        self.causes_by_id = causes_by_id
        self.limits_seen = []
        self.hybrid_seen = []

    async def search_knowledge(
        self, query, limit=10, filters=None, use_hybrid=False, min_score=None
    ):
        self.limits_seen.append(limit)
        self.hybrid_seen.append(use_hybrid)
        return self.search_results

    async def get_runbook_causes(self, item_id):
        return self.causes_by_id.get(item_id)


async def test_prefetch_then_seed_end_to_end_seeds_both_runbooks(enable_seeder):
    # End-to-end starvation regression: three chunks of runbook A rank above one
    # chunk of runbook B (all >= 0.3). The old limit-3 prefetch would return only
    # A's chunks and starve B; the deeper fetch lets the seeder's parent-dedup
    # reach BOTH parents, so a candidate hypothesis seeds from A AND from B.
    search_results = [
        _search_hit(score=0.90, parent_id="rb_a"),
        _search_hit(score=0.85, parent_id="rb_a"),
        _search_hit(score=0.80, parent_id="rb_a"),
        _search_hit(score=0.75, parent_id="rb_b", letters=("B",)),
        _search_hit(score=0.70, parent_id="rb_b", letters=()),
    ]
    # Distinct roots AND statements so neither the exact-root nor the paraphrase
    # dedup collapses them — both are genuinely distinct causes.
    ca = _good_cause("A", root_stmt="alpha root distinct fault")
    ca["cause_statement"] = "alpha distinct cause statement"
    cb = _good_cause("B", root_stmt="beta root distinct fault")
    cb["cause_statement"] = "beta distinct cause statement"
    ks = _PrefetchAndCausesStub(search_results, {"rb_a": [ca], "rb_b": [cb]})
    engine = _engine(ks)
    case = _case()

    relevant = await engine._prefetch_kb_context(case, "X fails", "symptom")
    await engine._seed_candidate_causes_from_kb(case, relevant)

    # Both runbooks contributed a seeded candidate (provenance stamped per parent).
    origins = {
        (n.metadata or {}).get(SEEDED_FROM_RUNBOOK_KEY)
        for n in case.causal_nodes.values()
    }
    assert "rb_a" in origins and "rb_b" in origins
    assert len(case.hypotheses) == 2


# ---------------------------------------------------------------------------
# Loader: get_runbook_causes — stubbed session factory + repository
# ---------------------------------------------------------------------------


class _FakeSessionCM:
    async def __aenter__(self):
        return "session"

    async def __aexit__(self, *a):
        return False


def _service_with_repo(
    monkeypatch, *, item="__unset__", raises=None
) -> KnowledgeService:
    """A KnowledgeService whose repository is stubbed to return ``item`` (or raise).

    ``item`` is what ``repo.get_by_id`` returns; ``raises`` (if set) is raised from
    it instead. The async session factory is faked (the fake repo ignores the
    session)."""

    class _FakeRepo:
        def __init__(self, session):
            pass

        async def get_by_id(self, item_id):
            if raises is not None:
                raise raises
            return None if item == "__unset__" else item

    monkeypatch.setattr(
        "faultmaven.modules.knowledge.infrastructure.persistence."
        "knowledge_item_repository.DatabaseKnowledgeItemRepository",
        _FakeRepo,
    )
    svc = KnowledgeService.__new__(KnowledgeService)
    svc._db_session_factory = lambda: _FakeSessionCM()
    return svc


async def test_loader_none_when_item_id_falsy(monkeypatch):
    # Empty id short-circuits before the repo is ever consulted.
    svc = _service_with_repo(monkeypatch)
    assert await svc.get_runbook_causes("") is None


# A "no session factory" case used to live here. It is gone with the degraded
# read path: since #899 the constructor requires the factory, so a KnowledgeService
# that would return None for that reason cannot exist. The construction contract
# is pinned in tests/integration/modules/knowledge/test_ingest_runbook.py.


async def test_loader_none_when_item_missing(monkeypatch):
    svc = _service_with_repo(monkeypatch, item=None)
    assert await svc.get_runbook_causes("rb1") is None


async def test_loader_none_when_no_metadata(monkeypatch):
    svc = _service_with_repo(monkeypatch, item=SimpleNamespace(metadata=None))
    assert await svc.get_runbook_causes("rb1") is None


async def test_loader_none_when_no_causes_key(monkeypatch):
    svc = _service_with_repo(monkeypatch, item=SimpleNamespace(metadata={"other": 1}))
    assert await svc.get_runbook_causes("rb1") is None


async def test_loader_none_when_causes_not_a_list(monkeypatch):
    # A non-list causes value is malformed → None (never a partial/garbage load).
    svc = _service_with_repo(
        monkeypatch, item=SimpleNamespace(metadata={"causes": {"A": {}}})
    )
    assert await svc.get_runbook_causes("rb1") is None


async def test_loader_returns_the_causes_list(monkeypatch):
    causes = [{"cause_letter": "A"}, {"cause_letter": "B"}]
    svc = _service_with_repo(
        monkeypatch,
        item=SimpleNamespace(
            metadata={"causes": causes},
            verification_level=VerificationLevel.COMMUNITY,
        ),
    )
    assert await svc.get_runbook_causes("rb1") == causes


async def test_loader_seeds_admin_verified(monkeypatch):
    # ADMIN_VERIFIED (gold-standard) is trusted → causes returned.
    causes = [{"cause_letter": "A"}]
    svc = _service_with_repo(
        monkeypatch,
        item=SimpleNamespace(
            metadata={"causes": causes},
            verification_level=VerificationLevel.ADMIN_VERIFIED,
        ),
    )
    assert await svc.get_runbook_causes("rb1") == causes


async def test_loader_refuses_experimental_item(monkeypatch):
    # Runtime trust invariant (R2): an EXPERIMENTAL item — AI-generated /
    # unreviewed / anonymous-upload — must never seed, even when it carries a
    # well-formed causes record.
    causes = [{"cause_letter": "A"}]
    svc = _service_with_repo(
        monkeypatch,
        item=SimpleNamespace(
            metadata={"causes": causes},
            verification_level=VerificationLevel.EXPERIMENTAL,
        ),
    )
    assert await svc.get_runbook_causes("rb1") is None


async def test_loader_refuses_experimental_raw_int_level(monkeypatch):
    # verification_level is persisted as an int (IntEnum); the refusal must fire
    # on the raw 0 the repository actually hydrates, not only the enum member.
    svc = _service_with_repo(
        monkeypatch,
        item=SimpleNamespace(
            metadata={"causes": [{"cause_letter": "A"}]},
            verification_level=0,
        ),
    )
    assert await svc.get_runbook_causes("rb1") is None


async def test_loader_none_on_lookup_error(monkeypatch):
    # A repository/DB error is swallowed to None — the seeder treats None as
    # "prose-only source, nothing to seed", never a crash.
    svc = _service_with_repo(monkeypatch, raises=RuntimeError("db down"))
    assert await svc.get_runbook_causes("rb1") is None


# ---------------------------------------------------------------------------
# Action tier: _handle_runbook_creation provenance short-circuit (Phase 5.2b)
# ---------------------------------------------------------------------------


class _TitleKnowledgeStub:
    """A knowledge service exposing ``get_runbook_title`` (and ``runbook_kb``)."""

    runbook_kb = None

    def __init__(self, titles=None):
        self.titles = titles or {}
        self.title_calls = []

    async def get_runbook_title(self, item_id):
        self.title_calls.append(item_id)
        return self.titles.get(item_id)


async def test_action_short_circuits_when_confirmed_cause_seeded(monkeypatch):
    """The cheap SYNC provenance tier fires ABOVE the async EXISTING_COVERS
    similarity backstop: a case whose confirmed cause was seeded from runbook X
    returns the NAMED 'already covered' message without creating a draft."""
    monkeypatch.setattr(
        "faultmaven.core.investigation.kb_cause_seeder.confirmed_root_seed_origin",
        lambda case: "rb_cover",
    )
    ks = _TitleKnowledgeStub({"rb_cover": "ArgoCD sync failure"})
    engine = _engine(ks)
    engine.conversion_service = None  # must not be reached — no draft created

    result = await engine._handle_runbook_creation(_case(), {})

    assert ks.title_calls == ["rb_cover"]
    assert "ArgoCD sync failure" in result["agent_response"]
    assert result["suggested_follow_ups"] == []


async def test_action_message_degrades_when_title_unavailable(monkeypatch):
    # Title lookup returning None still yields a coherent (runbook-unnamed) message.
    monkeypatch.setattr(
        "faultmaven.core.investigation.kb_cause_seeder.confirmed_root_seed_origin",
        lambda case: "rb_cover",
    )
    ks = _TitleKnowledgeStub({})
    engine = _engine(ks)
    engine.conversion_service = None

    result = await engine._handle_runbook_creation(_case(), {})
    assert "an existing runbook" in result["agent_response"]


async def test_action_proceeds_when_cause_self_discovered(monkeypatch):
    """No seed origin → the provenance tier is skipped and the normal
    readiness/dedup path runs (stubbed here to NOT_READY), and the title lookup
    is never made."""
    monkeypatch.setattr(
        "faultmaven.core.investigation.kb_cause_seeder.confirmed_root_seed_origin",
        lambda case: None,
    )
    from faultmaven.core.investigation import terminal_transitions

    async def _not_ready(case, runbook_kb=None, scope_resolver=None):
        return terminal_transitions.RunbookSuggestion(
            terminal_transitions.RunbookSuggestion.NOT_READY, "not ready"
        )

    monkeypatch.setattr(terminal_transitions, "evaluate_runbook_suggestion", _not_ready)
    ks = _TitleKnowledgeStub({"rb_cover": "should not be used"})
    engine = _engine(ks)
    engine.conversion_service = None

    result = await engine._handle_runbook_creation(_case(), {})
    assert result["agent_response"] == "not ready"
    assert ks.title_calls == []


# ---------------------------------------------------------------------------
# Loader: get_runbook_title — names the covering runbook for the offer message
# ---------------------------------------------------------------------------


async def test_get_runbook_title_returns_title(monkeypatch):
    svc = _service_with_repo(
        monkeypatch, item=SimpleNamespace(title="ArgoCD sync failure")
    )
    assert await svc.get_runbook_title("rb1") == "ArgoCD sync failure"


async def test_get_runbook_title_none_when_falsy_id(monkeypatch):
    svc = _service_with_repo(monkeypatch, item=SimpleNamespace(title="x"))
    assert await svc.get_runbook_title("") is None


async def test_get_runbook_title_none_when_item_missing(monkeypatch):
    svc = _service_with_repo(monkeypatch, item=None)
    assert await svc.get_runbook_title("rb1") is None


async def test_get_runbook_title_none_on_lookup_error(monkeypatch):
    svc = _service_with_repo(monkeypatch, raises=RuntimeError("db down"))
    assert await svc.get_runbook_title("rb1") is None
