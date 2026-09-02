"""#1272 — the seeding path: hybrid retrieval, the grounding gate, and the
identity a retrieved runbook arrives with.

The failure this closes is the CONFIDENT WRONG ANSWER. On the query the issue
was opened on, eight runbooks cleared the similarity floor, three of them on
several chunks each, and every one was about a different platform — because the
only word identifying the system (``qemu``) appears in none of the 91 shipped
runbooks, so every candidate matched on "failed", "start" and "file" alone.
Neither the floor nor #1144's corroboration guard can see that, because both
ask about a runbook's match and neither asks whether the query was about it.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.milestone_engine import (
    KB_PREFETCH_FETCH_LIMIT,
    KB_PREFETCH_RELEVANCE_THRESHOLD,
    MilestoneEngine,
)
from faultmaven.models.common import SearchResult


def _result(
    doc_id,
    parent,
    score=0.7,
    coverage=None,
    named=None,
    letters=("A",),
    total_chunks=8,
    title="Some Runbook",
):
    return SearchResult(
        document_id=doc_id,
        title=title,
        document_type="runbook",
        tags=[],
        score=score,
        snippet="...",
        parent_document_id=parent,
        total_chunks=total_chunks,
        matched_cause_letters=list(letters),
        term_coverage=coverage,
        identity_terms_in_query=list(named or []),
    )


def _engine(knowledge_service=None):
    engine = MilestoneEngine.__new__(MilestoneEngine)
    engine.knowledge_service = knowledge_service
    engine.hypothesis_manager = MagicMock()
    return engine


def _case():
    return SimpleNamespace(
        case_id="case_test",
        user_id="u1",
        organization_id=None,
        kb_context=None,
        current_turn=1,
        description="Failed to start QEMU binary cannot create PID file",
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


class TestRemediationPrefetchIsNotGatedBySeeding:
    """Turning the seeder off must not turn runbooks off.

    fm#1295 flipped ``FAULTMAVEN_KB_CAUSE_SEEDER`` to off by default. What that
    withholds is the engine's unasked assertion of a runbook's causes as
    hypotheses. What it must NOT withhold is the runbook reaching the model as
    prose at remediation time — the ``root_cause`` prefetch the turn pipeline
    fires on the cause_state→IDENTIFIED edge — because that is where the
    corrective pattern has measured value.

    Driven through ``_apply_investigation_updates`` itself, not by calling
    ``_prefetch_kb_context`` directly: the thing being pinned is the CALL SITE,
    which the fm#1295 removal work could delete or gate without any test on
    ``_prefetch_kb_context`` noticing. The rising edge is supplied by patching
    the edge helper (tested on its own in ``test_prompt_engine_realignment``),
    and a no-edge control shows the assertion is about the edge, not about a
    mock that always fires.
    """

    @staticmethod
    def _engine_with_seeder_off(monkeypatch, edge_query):
        from tests.unit.core.investigation.test_solution_offer_liveness import (
            _make_engine,
        )

        monkeypatch.setattr(
            "faultmaven.config.settings.get_settings",
            lambda: SimpleNamespace(
                features=SimpleNamespace(kb_cause_seeder_enabled=False)
            ),
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
    async def test_the_identified_edge_still_prefetches_with_the_seeder_off(
        self, monkeypatch
    ):
        from tests.unit.core.investigation.test_solution_offer_liveness import (
            _make_case,
            _meta,
            _solution_updates,
        )

        engine = self._engine_with_seeder_off(monkeypatch, "redis maxmemory reached")
        case = _make_case()
        await engine._apply_investigation_updates(case, _solution_updates(), _meta())
        calls = [
            c
            for c in engine._prefetch_kb_context.await_args_list
            if c.args[2:3] == ("root_cause",)
        ]
        assert len(calls) == 1, (
            "the remediation-time KB prefetch on the cause_state→IDENTIFIED edge "
            f"must fire with the seeder off; prefetch calls seen: "
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

        engine = self._engine_with_seeder_off(monkeypatch, None)
        await engine._apply_investigation_updates(
            _make_case(), _solution_updates(), _meta()
        )
        assert not [
            c
            for c in engine._prefetch_kb_context.await_args_list
            if c.args[2:3] == ("root_cause",)
        ]


class TestSeedingGroundingGate:
    """A runbook may seed only if the query NAMED it.

    #1272 also admitted a runbook that COVERED the query at
    ``term_coverage >= 0.90``. That arm was removed in #1285: measured over the
    shipped corpus it decided 37 chunks, 36 of them off-domain, because
    ``term_coverage`` is a share of the QUERY and so peaks on queries that
    identify nothing. The measurement and its pins live in
    ``test_kb_seed_grounding_reachability_1285.py``; what remains here is
    #1272's own behaviour, which the removal did not change.
    """

    async def _seed(self, hits, monkeypatch):
        seen = {}
        service = MagicMock()
        service.get_runbook_causes = AsyncMock(
            side_effect=lambda p: [{"cause_letter": "A", "statement": "s"}]
        )
        engine = _engine(service)

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
        await engine._seed_candidate_causes_from_kb(_case(), hits)
        return seen.get("runbooks", [])

    @pytest.mark.asyncio
    async def test_ungrounded_runbook_does_not_seed(self, monkeypatch):
        # #1272 exactly: two chunks of a Kubernetes runbook, well above the
        # similarity floor, corroborating each other — and the query is about
        # QEMU.
        hits = [
            _result("k8s_chunk_1", "kb_k8s", coverage=0.53, named=[]),
            _result("k8s_chunk_2", "kb_k8s", coverage=0.49, named=[]),
        ]
        assert await self._seed(hits, monkeypatch) == []

    @pytest.mark.asyncio
    async def test_a_named_runbook_seeds(self, monkeypatch):
        hits = [
            _result("nginx_1", "kb_nginx", coverage=0.21, named=["nginx", "502"]),
            _result("nginx_2", "kb_nginx", coverage=0.19, named=["nginx"]),
        ]
        seeded = await self._seed(hits, monkeypatch)
        assert [r.item_id for r in seeded] == ["kb_nginx"]

    @pytest.mark.asyncio
    async def test_an_unnamed_runbook_does_not_seed_at_any_coverage(self, monkeypatch):
        """Coverage no longer admits anything, including at its maximum.

        This was ``test_a_covering_runbook_seeds_even_when_unnamed``, sized on
        a query copied out of a runbook. On real queries the value it asserted
        is reached by statements that identify nothing at all — see #1285's
        measurement.
        """
        hits = [
            _result("ldf_1", "kb_ldf", coverage=1.0, named=[]),
            _result("ldf_2", "kb_ldf", coverage=0.4, named=[]),
        ]
        assert await self._seed(hits, monkeypatch) == []

    @pytest.mark.asyncio
    async def test_grounding_is_per_runbook_not_per_chunk(self, monkeypatch):
        """One naming chunk admits the runbook; its siblings then corroborate.

        Judged per chunk, this runbook is admitted on the chunk whose metadata
        carried the identity terms and then declined by #1144 for want of a
        second — grounding would be doing corroboration's job a second time.
        """
        hits = [
            _result("ldf_1", "kb_ldf", coverage=0.4, named=["disk"], letters=("A",)),
            _result("ldf_2", "kb_ldf", coverage=0.3, named=[], letters=("B",)),
        ]
        seeded = await self._seed(hits, monkeypatch)
        assert [r.item_id for r in seeded] == ["kb_ldf"]

    @pytest.mark.asyncio
    async def test_unmeasured_hits_are_not_judged(self, monkeypatch):
        """No term index / pure-vector path: absent evidence must not gate.

        An absent measurement must not authorise what the gate withholds, but
        it must not silently disable seeding either.
        """
        hits = [
            _result("y_1", "kb_y", coverage=None, named=[]),
            _result("y_2", "kb_y", coverage=None, named=[]),
        ]
        seeded = await self._seed(hits, monkeypatch)
        assert [r.item_id for r in seeded] == ["kb_y"]

    @pytest.mark.asyncio
    async def test_a_grounded_runbook_survives_beside_an_ungrounded_one(
        self, monkeypatch
    ):
        hits = [
            _result("k8s_1", "kb_k8s", score=0.80, coverage=0.5, named=[]),
            _result("k8s_2", "kb_k8s", score=0.79, coverage=0.5, named=[]),
            _result("ldf_1", "kb_ldf", score=0.60, coverage=0.4, named=["disk"]),
            _result("ldf_2", "kb_ldf", score=0.59, coverage=0.2, named=[]),
        ]
        seeded = await self._seed(hits, monkeypatch)
        assert [r.item_id for r in seeded] == ["kb_ldf"], (
            "the gate is per runbook, so a higher-ranked ungrounded runbook "
            "must not take the grounded one's slot"
        )
