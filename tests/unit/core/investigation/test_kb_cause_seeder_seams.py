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
from unittest.mock import AsyncMock
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


def _hit(parent_id, score):
    """A retrieval hit as the wrapper reads it: ``.parent_document_id`` + ``.score``."""
    return SimpleNamespace(parent_document_id=parent_id, score=score)


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
        return SimpleNamespace(seeded_hypothesis_ids=[])

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
    await engine._seed_candidate_causes_from_kb(_case(), [_hit("rb1", 0.9)])
    assert ks.calls == []
    assert seed_spy == []


async def test_wrapper_no_knowledge_service_is_a_noop(enable_seeder, seed_spy):
    engine = _engine(knowledge_service=None)
    # Must not raise despite kb_hits present.
    await engine._seed_candidate_causes_from_kb(_case(), [_hit("rb1", 0.9)])
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
        [_hit("rb1", 0.4), _hit("rb2", 0.7), _hit("rb1", 0.9)],
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
        [_hit("rb1", 0.9), _hit("rb2", 0.8), _hit("rb3", 0.7)],
    )
    assert ks.calls == ["rb1", "rb2"]
    assert "rb3" not in ks.calls
    assert [rb.item_id for rb in seed_spy[0].runbooks] == ["rb1", "rb2"]


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
        _case(), [_hit("rb1", 0.9), _hit("rb2", 0.8)]
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
        _case(), [_hit("rb1", 0.9), _hit("rb2", 0.8)]
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
    await engine._seed_candidate_causes_from_kb(_case(), [_hit("rb1", 0.9)])
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
    await engine._seed_candidate_causes_from_kb(_case(), [_hit("rb1", 0.9)])


# ---------------------------------------------------------------------------
# Wrapper: happy path end-to-end through the REAL seeder (no spy)
# ---------------------------------------------------------------------------


async def test_wrapper_happy_path_seeds_through_real_seeder(enable_seeder):
    # No spy: prove the wrapper builds SeededRunbooks the real seeder can consume,
    # producing candidate nodes/hypotheses on the case.
    ks = _KnowledgeStub({"rb1": [_good_cause("A")]})
    case = _case()
    engine = _engine(ks)
    await engine._seed_candidate_causes_from_kb(case, [_hit("rb1", 0.9)])

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
    await engine._seed_candidate_causes_from_kb(case, [_hit("rb1", 0.9)])

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
    ``case.kb_context``)."""

    def __init__(self, results=None):
        self.results = results or []
        self.filters_seen = []
        self.limits_seen = []

    async def search_knowledge(self, query, limit=10, filters=None):
        self.filters_seen.append(filters)
        self.limits_seen.append(limit)
        return self.results


def _search_hit(score=0.9, parent_id="rb1"):
    return SimpleNamespace(
        title="t",
        snippet="s",
        score=score,
        document_type="runbook",
        parent_document_id=parent_id,
    )


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
        _search_hit(score=0.75, parent_id="rb_b"),
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

    async def search_knowledge(self, query, limit=10, filters=None):
        self.limits_seen.append(limit)
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
        _search_hit(score=0.75, parent_id="rb_b"),
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
