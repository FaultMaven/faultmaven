"""The engine's KB prefetch (``_prefetch_kb_context``): what it owes.

Hybrid retrieval with the floor at admission (#1272), the floor's parity with
the QA tool's (#1072), owner-keyed scope (global ∪ the case owner's personal KB
∪ the owner's team shares), stale-context clearing, fetch depth versus the
prompt surface, and the remediation-time edge in the turn pipeline. Several of
these invariants were pinned only in the KB cause seeder's seam tests until
fm#1295 removed the seeder; they are live behaviour and moved here.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.milestone_engine import (
    KB_CONTEXT_MAX_ENTRIES,
    KB_PREFETCH_FETCH_LIMIT,
    KB_PREFETCH_RELEVANCE_THRESHOLD,
    MilestoneEngine,
)


def _engine(knowledge_service=None):
    from tests.unit.core.investigation.test_solution_offer_liveness import _make_engine

    engine = _make_engine()
    engine.knowledge_service = knowledge_service
    engine.runbook_kb = None
    return engine


def _case():
    from tests.unit.core.investigation.test_solution_offer_liveness import _make_case

    case = _make_case()  # user_id="u1", enterprise_id="o1"
    case.kb_context = None
    return case


class _SearchRecordingStub:
    """Records every ``search_knowledge`` call and returns a fixed result list."""

    def __init__(self, results):
        self.results = results
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


def _search_hit(score=0.9, parent_id="rb1"):
    return SimpleNamespace(
        title="t",
        snippet="s",
        score=score,
        document_type="runbook",
        parent_document_id=parent_id,
    )


class TestPrefetchUsesHybridRetrieval:
    @pytest.mark.asyncio
    async def test_prefetch_asks_for_hybrid_and_floors_at_admission(self):
        service = MagicMock()
        service.search_knowledge = AsyncMock(return_value=[])
        engine = _engine(service)
        await engine._prefetch_kb_context(_case(), "disk full", "symptom")
        kwargs = service.search_knowledge.call_args.kwargs
        assert kwargs["use_hybrid"] is True, (
            "pure vector search puts the runbook covering #1272's incident at "
            "rank 70 of 91; no floor value can rescue a chunk ranked 369th "
            "because the fetch limit is applied to chunks before any floor"
        )
        assert kwargs["min_score"] == KB_PREFETCH_RELEVANCE_THRESHOLD
        assert kwargs["limit"] == KB_PREFETCH_FETCH_LIMIT


class TestStaleContextIsCleared:
    @pytest.mark.asyncio
    async def test_a_trigger_that_finds_nothing_clears_earlier_context(self):
        """A later trigger's miss must not leave the earlier one's runbooks up.

        The floor moved to admission, which made `relevant` identical to
        `results` and left the clearing branch — written as `elif results:` —
        unreachable. Stale runbooks then stood in the prompt as if they still
        matched.
        """
        service = MagicMock()
        service.search_knowledge = AsyncMock(return_value=[])
        engine = _engine(service)
        case = _case()
        case.kb_context = [{"title": "Something From An Earlier Turn"}]
        await engine._prefetch_kb_context(case, "root cause query", "root_cause")
        assert case.kb_context is None


class TestRemediationPrefetchOnTheIdentifiedEdge:
    """Runbooks reach the model at remediation time, unconditionally.

    fm#1295 removed the KB cause seeder — the engine's unasked assertion of a
    runbook's causes as hypotheses. What must survive it is the runbook
    reaching the model as prose at remediation time — the ``root_cause``
    prefetch the turn pipeline fires on the cause_state→IDENTIFIED edge —
    because that is where the corrective pattern has measured value.

    Driven through ``_apply_investigation_updates`` itself, not by calling
    ``_prefetch_kb_context`` directly: the thing being pinned is the CALL SITE,
    which a removal could delete or gate without any test on
    ``_prefetch_kb_context`` noticing. The rising edge is supplied by patching
    the edge helper (tested on its own in ``test_prompt_engine_realignment``),
    and a no-edge control shows the assertion is about the edge, not about a
    mock that always fires.
    """

    @staticmethod
    def _engine_on_edge(monkeypatch, edge_query):
        from tests.unit.core.investigation.test_solution_offer_liveness import (
            _make_engine,
        )

        monkeypatch.setattr(
            "faultmaven.core.investigation.milestone_engine."
            "_kb_prefetch_query_on_identification",
            lambda *a, **k: edge_query,
        )
        engine = _make_engine()
        engine._prefetch_kb_context = AsyncMock(return_value=[])
        return engine

    @pytest.mark.asyncio
    async def test_the_identified_edge_prefetches_remediation_runbooks(
        self, monkeypatch
    ):
        from tests.unit.core.investigation.test_solution_offer_liveness import (
            _make_case,
            _meta,
            _solution_updates,
        )

        engine = self._engine_on_edge(monkeypatch, "redis maxmemory reached")
        case = _make_case()
        await engine._apply_investigation_updates(case, _solution_updates(), _meta())
        calls = [
            c
            for c in engine._prefetch_kb_context.await_args_list
            if c.args[2:3] == ("root_cause",)
        ]
        assert len(calls) == 1, (
            "the remediation-time KB prefetch on the cause_state→IDENTIFIED edge "
            f"must fire; prefetch calls seen: "
            f"{engine._prefetch_kb_context.await_args_list}"
        )
        assert (
            calls[0].args[0] is case and calls[0].args[1] == "redis maxmemory reached"
        )

    @pytest.mark.asyncio
    async def test_no_edge_no_remediation_prefetch(self, monkeypatch):
        """Control: the assertion above is about the edge, not a mock that
        always fires."""
        from tests.unit.core.investigation.test_solution_offer_liveness import (
            _make_case,
            _meta,
            _solution_updates,
        )

        engine = self._engine_on_edge(monkeypatch, None)
        await engine._apply_investigation_updates(
            _make_case(), _solution_updates(), _meta()
        )
        assert not [
            c
            for c in engine._prefetch_kb_context.await_args_list
            if c.args[2:3] == ("root_cause",)
        ]


class TestPrefetchFloorAndScope:
    """Invariants that lived only in the seeder's seam tests until fm#1295."""

    def test_prefetch_threshold_tracks_the_qa_tool_threshold(self):
        """Both floors read the same score, so they must carry the same number.

        ``_prefetch_kb_context`` filters ``SearchResult.score``, which
        ``KnowledgeService.search_knowledge`` passes through verbatim from
        ``KnowledgeVectorStore.search`` — the identical scale the QA tool's
        ``relevance_threshold`` is calibrated against. They drifted apart in the
        fix for #1072 exactly because nothing tied them together.
        """
        from faultmaven.modules.agent.tools.kb_configs.unified_kb_config import (
            UnifiedKBConfig,
        )

        assert KB_PREFETCH_RELEVANCE_THRESHOLD == UnifiedKBConfig().relevance_threshold

    @pytest.mark.asyncio
    async def test_prefetch_keeps_weakest_on_topic_and_drops_adjacent_off_topic(self):
        """The measured calibration (#1072): 0.591 is the weakest on-topic
        retrieval measured against the shipped KB, 0.477 the strongest off-topic
        one. The floor sits between them, and what clears it is what reaches
        ``case.kb_context``."""
        ks = _SearchRecordingStub(
            [
                _search_hit(score=0.591, parent_id="on-topic"),
                _search_hit(score=0.477, parent_id="off-topic"),
            ]
        )
        engine = _engine(ks)
        case = _case()
        await engine._prefetch_kb_context(case, "X fails", "symptom")
        assert [r["parent_document_id"] for r in case.kb_context] == ["on-topic"]

    @pytest.mark.asyncio
    async def test_prefetch_scope_is_global_union_owner(self):
        # global PLUS the case owner's own KB — otherwise personal
        # (case-generated) runbooks never reach the prompt. The team arm is
        # wired but resolves empty with no team_service/share_repository.
        ks = _SearchRecordingStub([_search_hit()])
        engine = _engine(ks)
        await engine._prefetch_kb_context(_case(), "X fails", "symptom")
        assert ks.filters_seen == [{"$or": [{"scope": "global"}, {"owner_id": "u1"}]}]

    @pytest.mark.asyncio
    async def test_prefetch_team_arm_uses_owner_shared_runbooks(self):
        # With team_service + share_repository attached (Cloud), the OWNER's
        # scope widens with runbooks shared to the OWNER's teams — keyed on
        # case.user_id, NOT the session user, and on the case's tenant (#879).
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
        engine.team_service.list_all_user_team_ids.assert_awaited_once_with("owner_b")
        engine.share_repository.list_resource_ids.assert_awaited_once_with(
            resource_type="knowledge_item",
            scope_type="team",
            scope_ids=["team_1"],
            enterprise_id="o1",
        )

    @pytest.mark.asyncio
    async def test_prefetch_owner_condition_keyed_on_this_case_owner(self):
        ks = _SearchRecordingStub([_search_hit()])
        engine = _engine(ks)
        case = _case()
        case.user_id = "user_b"
        await engine._prefetch_kb_context(case, "X fails", "symptom")
        scope_filter = ks.filters_seen[0]
        assert [c for c in scope_filter["$or"] if "owner_id" in c] == [
            {"owner_id": "user_b"}
        ]

    @pytest.mark.asyncio
    async def test_prefetch_global_only_when_no_owner(self):
        # An owner-less case (user_id cleared after account deletion) falls back
        # to a plain global scope — never an unfiltered cross-tenant read.
        ks = _SearchRecordingStub([_search_hit()])
        engine = _engine(ks)
        case = _case()
        case.user_id = None
        await engine._prefetch_kb_context(case, "X fails", "symptom")
        assert ks.filters_seen == [{"scope": "global"}]

    def test_prefetch_fetches_deeper_than_the_prompt_surface(self):
        # The fetch depth is the reranker's candidate pool, deeper than the
        # prompt surface, so a long runbook's top chunks do not crowd out a
        # second runbook before the top slice is taken.
        assert KB_PREFETCH_FETCH_LIMIT > KB_CONTEXT_MAX_ENTRIES

    @pytest.mark.asyncio
    async def test_prefetch_renders_only_the_top_slice(self):
        hits = [_search_hit(score=0.9 - i * 0.01, parent_id=f"rb{i}") for i in range(6)]
        ks = _SearchRecordingStub(hits)
        engine = _engine(ks)
        case = _case()
        await engine._prefetch_kb_context(case, "X fails", "symptom")
        assert ks.limits_seen == [KB_PREFETCH_FETCH_LIMIT]
        assert [r["parent_document_id"] for r in case.kb_context] == [
            f"rb{i}" for i in range(KB_CONTEXT_MAX_ENTRIES)
        ]
