"""Tests for the structured runbook-Cause matcher entry point
(``AnswerFromKB.aget_cause_matches``) — increment 3 of the matcher wiring.

Covers retrieve → rank distinct runbooks → resolve causes → build CauseRecords →
run the rung-level evaluator. Uses a mock vector store and the REAL
``IndicatorEvaluator`` so the path is exercised end-to-end.
"""

from typing import Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.indicator_evaluator import IndicatorEvaluator
from faultmaven.modules.agent.tools.kb_qa import AnswerFromKB

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _chunk(item_id: str, idx: int = 0, score: float = 0.9, *, with_parent=True):
    meta = {"title": f"Doc {item_id}"}
    if with_parent:
        meta["parent_document_id"] = item_id
    return {
        "id": f"{item_id}_chunk_{idx}",
        "content": f"chunk {idx} of {item_id}",
        "metadata": meta,
        "score": score,
    }


def _cause_dict(
    letter: str,
    name: str,
    rung_indicators: Dict[str, List[str]],
    predicates: Optional[List[dict]] = None,
    *,
    fallback: bool = False,
):
    refs = list(rung_indicators.keys()) or ["root", "D"]
    return {
        "cause_letter": letter,
        "cause_name": name,
        "cause_statement": "stmt",
        "chain_nodes": [{"ref": r, "node_type": "rung", "statement": r} for r in refs],
        "rung_indicators": rung_indicators,
        "match_predicates": predicates or [],
        "is_fallback_cause": fallback,
    }


def _make_tool(chunks):
    vector_store = MagicMock()
    vector_store.hybrid_search = AsyncMock(return_value=chunks)
    vector_store.search = AsyncMock(return_value=chunks)
    llm_router = MagicMock()
    return AnswerFromKB(vector_store=vector_store, llm_router=llm_router)


def _evaluator(*step_pairs):
    table = dict(step_pairs)
    return IndicatorEvaluator(step_output_resolver=table.get)


def _matching_cause():
    return _cause_dict(
        "A",
        "real",
        {"root": ["[Step 1] cond"]},
        [{"step": 1, "predicate": "contains", "target": "x"}],
    )


def _fallback_cause():
    return _cause_dict("Z", "fallback", {"D": ["[Default]"]}, fallback=True)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestEndToEnd:
    @pytest.mark.asyncio
    async def test_single_runbook_single_match(self):
        tool = _make_tool([_chunk("kb_rb1", 0), _chunk("kb_rb1", 1, 0.8)])
        causes = {"kb_rb1": [_matching_cause(), _fallback_cause()]}

        async def resolve(item_id):
            return causes.get(item_id)

        results = await tool.aget_cause_matches(
            "why is it failing?",
            user_id="u1",
            resolve_causes=resolve,
            evaluator=_evaluator((1, "value x present")),
        )

        assert len(results) == 1
        assert results[0].runbook_id == "kb_rb1"
        assert results[0].verdict == "single"
        assert results[0].selected_cause.cause_name == "real"

    @pytest.mark.asyncio
    async def test_ranks_distinct_runbooks_and_caps(self):
        # 3 runbooks across 4 chunks; cap at 2 distinct runbooks, best-first.
        chunks = [
            _chunk("kb_a", 0, 0.95),
            _chunk("kb_a", 1, 0.90),
            _chunk("kb_b", 0, 0.85),
            _chunk("kb_c", 0, 0.80),
        ]
        tool = _make_tool(chunks)
        seen = []

        async def resolve(item_id):
            seen.append(item_id)
            return [_matching_cause(), _fallback_cause()]

        results = await tool.aget_cause_matches(
            "q",
            user_id="u1",
            resolve_causes=resolve,
            evaluator=_evaluator((1, "value x present")),
            max_runbooks=2,
        )

        assert [r.runbook_id for r in results] == ["kb_a", "kb_b"]
        assert seen == ["kb_a", "kb_b"]  # kb_c never resolved (capped)

    @pytest.mark.asyncio
    async def test_item_id_falls_back_to_chunk_id_suffix(self):
        # No parent_document_id metadata → derive item_id from the chunk id.
        tool = _make_tool([_chunk("kb_rb9", 0, with_parent=False)])

        async def resolve(item_id):
            assert item_id == "kb_rb9"
            return [_matching_cause(), _fallback_cause()]

        results = await tool.aget_cause_matches(
            "q",
            user_id="u1",
            resolve_causes=resolve,
            evaluator=_evaluator((1, "value x present")),
        )
        assert results[0].runbook_id == "kb_rb9"


# ---------------------------------------------------------------------------
# Skips, tolerance, and degradation (matcher is a prior, not a gate)
# ---------------------------------------------------------------------------


class TestDegradation:
    @pytest.mark.asyncio
    async def test_runbook_without_causes_is_skipped(self):
        tool = _make_tool([_chunk("kb_v3", 0), _chunk("kb_v4", 0, 0.8)])

        async def resolve(item_id):
            return (
                None if item_id == "kb_v3" else [_matching_cause(), _fallback_cause()]
            )

        results = await tool.aget_cause_matches(
            "q",
            user_id="u1",
            resolve_causes=resolve,
            evaluator=_evaluator((1, "value x present")),
        )
        assert [r.runbook_id for r in results] == ["kb_v4"]

    @pytest.mark.asyncio
    async def test_empty_causes_list_is_skipped(self):
        tool = _make_tool([_chunk("kb_rb1", 0)])

        async def resolve(item_id):
            return []

        results = await tool.aget_cause_matches(
            "q",
            user_id="u1",
            resolve_causes=resolve,
            evaluator=_evaluator((1, "value x present")),
        )
        assert results == []

    @pytest.mark.asyncio
    async def test_malformed_cause_entry_skipped_runbook_still_matches(self):
        tool = _make_tool([_chunk("kb_rb1", 0)])

        async def resolve(item_id):
            # One junk entry (no cause_letter → CauseRecord rejects it), one good.
            return [{"oops": True}, "not-a-dict", _matching_cause(), _fallback_cause()]

        results = await tool.aget_cause_matches(
            "q",
            user_id="u1",
            resolve_causes=resolve,
            evaluator=_evaluator((1, "value x present")),
        )
        assert len(results) == 1
        assert results[0].verdict == "single"

    @pytest.mark.asyncio
    async def test_all_causes_malformed_skips_runbook(self):
        tool = _make_tool([_chunk("kb_rb1", 0)])

        async def resolve(item_id):
            return [{"oops": True}]  # no valid CauseRecord

        results = await tool.aget_cause_matches(
            "q",
            user_id="u1",
            resolve_causes=resolve,
            evaluator=_evaluator((1, "value x present")),
        )
        assert results == []

    @pytest.mark.asyncio
    async def test_resolve_causes_error_skips_that_runbook(self):
        tool = _make_tool([_chunk("kb_bad", 0, 0.9), _chunk("kb_ok", 0, 0.8)])

        async def resolve(item_id):
            if item_id == "kb_bad":
                raise RuntimeError("db blip")
            return [_matching_cause(), _fallback_cause()]

        results = await tool.aget_cause_matches(
            "q",
            user_id="u1",
            resolve_causes=resolve,
            evaluator=_evaluator((1, "value x present")),
        )
        assert [r.runbook_id for r in results] == ["kb_ok"]

    @pytest.mark.asyncio
    async def test_retrieval_failure_returns_empty(self):
        tool = _make_tool([])
        tool._vector_store.hybrid_search = AsyncMock(side_effect=RuntimeError("boom"))
        tool._vector_store.search = AsyncMock(side_effect=RuntimeError("boom"))

        called = False

        async def resolve(item_id):
            nonlocal called
            called = True
            return [_matching_cause()]

        results = await tool.aget_cause_matches(
            "q",
            user_id="u1",
            resolve_causes=resolve,
            evaluator=_evaluator((1, "value x present")),
        )
        assert results == []
        assert called is False  # no runbooks ranked → resolver never invoked

    @pytest.mark.asyncio
    async def test_no_chunks_returns_empty(self):
        tool = _make_tool([])

        async def resolve(item_id):
            return [_matching_cause()]

        results = await tool.aget_cause_matches(
            "q",
            user_id="u1",
            resolve_causes=resolve,
            evaluator=_evaluator((1, "value x present")),
        )
        assert results == []
