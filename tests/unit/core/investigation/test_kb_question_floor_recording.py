"""The question lookup's floor against a recording of the real search (F2 of
the #2797 review; §5.6 of ``orchestration-capabilities.md``).

``tests/eval/kb_retrieval/question-lookups.json`` holds what the real hybrid
pipeline returned on the shipped pack for 16 topics the pack covers and 6 it
does not, searched as the lookup searches (the bare topic, its fetch limit,
admitted at its floor). Recorded offline by ``record_question_lookups.py``
beside it; re-record when the pack, the retrieval or the floor changes.

What the recording shows, and these tests hold:

* every covered topic comes back as candidates with its runbook among them —
  shown, or named among the further titles — so the floor never answers "not
  found" to a topic the knowledge base covers;
* every off-topic topic comes back as candidates too (its closest chunk scored
  0.49–0.54 on cosine), so on the shipped pack it is the reply's relevance
  judgement, not the floor, that says "not found". The block's rule for that
  is therefore load-bearing.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.kb_question import (
    KB_QUESTION_CANDIDATE_FLOOR,
    KB_QUESTION_FETCH_LIMIT,
    KbLookupStatus,
    render_kb_lookup_block,
)
from faultmaven.core.investigation.milestone_engine.kb_question_lookup import (
    look_up_kb_question,
)
from faultmaven.core.investigation.prompts.fence import PromptFence, mint_token
from faultmaven.models.common import SearchResult

pytestmark = pytest.mark.unit

RECORDING = json.loads(
    (
        Path(__file__).resolve().parents[3]
        / "eval"
        / "kb_retrieval"
        / "question-lookups.json"
    ).read_text()
)
COVERED = [r for r in RECORDING["rows"] if r["covered_by"]]
OFF_TOPIC = [r for r in RECORDING["rows"] if not r["covered_by"]]


def test_the_recording_was_made_the_way_the_lookup_searches():
    """A recording admitted at a higher floor, or cut at a smaller limit,
    cannot say what the lookup now returns: re-record."""
    assert RECORDING["fetch_limit"] == KB_QUESTION_FETCH_LIMIT
    assert RECORDING["admission_floor"] <= KB_QUESTION_CANDIDATE_FLOOR
    assert (len(COVERED), len(OFF_TOPIC)) == (16, 6)


async def _replay(row):
    results = [
        SearchResult(
            document_id=f"{r['parent_document_id']}_chunk_{n}",
            title=r["title"],
            document_type="runbook",
            tags=[],
            score=r["score"],
            snippet=r["head"][:200] + "...",
            content=r["head"],
            parent_document_id=r["parent_document_id"],
        )
        for n, r in enumerate(row["results"])
    ]
    service = MagicMock()
    service.search_knowledge = AsyncMock(return_value=results)
    deps = SimpleNamespace(
        knowledge_service=service, team_service=None, share_repository=None
    )
    case = SimpleNamespace(
        case_id="case_aabbccddeeff",
        description="",
        effective_driver_id="user_driver",
        user_id="user_driver",
        enterprise_id="ent_1",
    )
    return await look_up_kb_question(
        deps, case, {"kind": "lookup", "topic": row["topic"], "from_context": False}
    )


def _names(row, titles) -> bool:
    return any(f.lower() in t.lower() for t in titles for f in row["covered_by"])


@pytest.mark.parametrize("row", COVERED, ids=lambda r: r["topic"])
async def test_a_covered_topic_reaches_its_runbook(row):
    lookup = await _replay(row)
    assert lookup.status is KbLookupStatus.CANDIDATES
    shown = [e["title"] for e in lookup.entries]
    assert _names(row, shown + list(lookup.other_titles))
    if not _names(row, shown):
        # Reached only through the further titles ("disk full on a linux
        # server", whose runbook ranked sixth): the reply is told it may name
        # one of those.
        block = render_kb_lookup_block(lookup, PromptFence(mint_token()))
        assert "a further runbook's title plainly is" in block


@pytest.mark.parametrize("row", OFF_TOPIC, ids=lambda r: r["topic"])
async def test_an_off_topic_question_is_left_to_the_reply_to_judge(row):
    lookup = await _replay(row)
    assert lookup.status is KbLookupStatus.CANDIDATES
    block = render_kb_lookup_block(lookup, PromptFence(mint_token()))
    assert "only if its title or" in block
    assert "If no candidate is about it, say first" in block
