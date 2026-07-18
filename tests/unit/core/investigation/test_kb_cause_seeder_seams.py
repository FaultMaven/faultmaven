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
from uuid import uuid4

import pytest

from faultmaven.core.investigation.hypothesis_manager import create_hypothesis_manager
from faultmaven.core.investigation.kb_cause_seeder import (
    MAX_SEEDED_RUNBOOKS,
    SEEDED_FROM_RUNBOOK_KEY,
)
from faultmaven.core.investigation.milestone_engine import MilestoneEngine
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    InquiryData,
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
    """A MilestoneEngine with only the two attributes the seam touches set —
    ``__new__`` skips the heavy constructor."""
    engine = MilestoneEngine.__new__(MilestoneEngine)
    engine.knowledge_service = knowledge_service
    engine.hypothesis_manager = hypothesis_manager or create_hypothesis_manager()
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


# ---------------------------------------------------------------------------
# Scope filter: build_kb_scope_filter — the single source of KB read isolation
# ---------------------------------------------------------------------------


def test_scope_filter_global_only_when_no_owner():
    assert build_kb_scope_filter(None) == {"scope": "global"}
    assert build_kb_scope_filter("") == {"scope": "global"}


def test_scope_filter_global_union_owner_personal():
    assert build_kb_scope_filter("user_a") == {
        "$or": [
            {"scope": "global"},
            {"scope": "personal", "owner_id": "user_a"},
        ]
    }


def test_scope_filter_includes_owner_teams():
    assert build_kb_scope_filter("user_a", ["t1", "t2"]) == {
        "$or": [
            {"scope": "global"},
            {"scope": "personal", "owner_id": "user_a"},
            {"scope": "team", "team_id": "t1"},
            {"scope": "team", "team_id": "t2"},
        ]
    }


def test_scope_filter_personal_condition_keyed_on_owner_only():
    # Isolation invariant: the only personal condition is keyed on the given
    # owner — never another user's id.
    f = build_kb_scope_filter("user_b", ["t1"])
    personal = [c for c in f["$or"] if c.get("scope") == "personal"]
    assert personal == [{"scope": "personal", "owner_id": "user_b"}]


# ---------------------------------------------------------------------------
# Pre-fetch: _prefetch_kb_context — owner-aware scope + cross-user isolation
# ---------------------------------------------------------------------------


class _SearchRecordingStub:
    """Records the ``filters`` passed to ``search_knowledge`` and returns a
    configured result list (so the pre-fetch can build ``case.kb_context``)."""

    def __init__(self, results=None):
        self.results = results or []
        self.filters_seen = []

    async def search_knowledge(self, query, limit=10, filters=None):
        self.filters_seen.append(filters)
        return self.results


def _search_hit(score=0.9, parent_id="rb1"):
    return SimpleNamespace(
        title="t",
        snippet="s",
        score=score,
        document_type="runbook",
        parent_document_id=parent_id,
    )


async def test_prefetch_scope_is_global_union_owner_personal():
    # The pre-fetch must search global PLUS the case owner's own personal KB —
    # otherwise personal (case-generated) runbooks never seed.
    ks = _SearchRecordingStub([_search_hit()])
    engine = _engine(ks)
    case = _case()  # user_id="u"
    await engine._prefetch_kb_context(case, "X fails", "symptom")

    assert ks.filters_seen == [
        {
            "$or": [
                {"scope": "global"},
                {"scope": "personal", "owner_id": "u"},
            ]
        }
    ]


async def test_prefetch_personal_condition_keyed_on_this_case_owner():
    # Cross-user isolation: the personal condition is keyed on THIS case's
    # owner. A case owned by user_b can only ever surface user_b's personal
    # runbooks — never another user's.
    ks = _SearchRecordingStub([_search_hit()])
    engine = _engine(ks)
    case = _case()
    case.user_id = "user_b"
    await engine._prefetch_kb_context(case, "X fails", "symptom")

    scope_filter = ks.filters_seen[0]
    personal = [c for c in scope_filter["$or"] if c.get("scope") == "personal"]
    assert personal == [{"scope": "personal", "owner_id": "user_b"}]
    # No other user's personal scope leaks in.
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


async def test_loader_none_when_no_session_factory():
    svc = KnowledgeService.__new__(KnowledgeService)
    svc._db_session_factory = None
    assert await svc.get_runbook_causes("rb1") is None


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
