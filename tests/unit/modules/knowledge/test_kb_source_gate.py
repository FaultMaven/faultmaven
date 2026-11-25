"""Stored runbook excerpts are gated per viewer when read back (#1919).

Owner ruling on #1919 (2026-10-10): a turn retrieves with its driver's
knowledge; the excerpts its prompt carried are stored as the turn's
``sources``; and those COPIES are checked against whoever reads them. A viewer
who can open the runbook sees the excerpt; anyone else sees a redacted entry.
``gate_kb_sources`` is that check. These pin its arms with doubles; the
integration file drives it over the real visibility query.
"""

from typing import Any, List, Optional, Set

import pytest

from faultmaven.models.api import Source, SourceType
from faultmaven.modules.knowledge.contracts import (
    RESTRICTED_SOURCE_ACCESS,
    gate_kb_sources,
)

pytestmark = [pytest.mark.unit, pytest.mark.security]

VIEWER_ID = "user-viewer"
READABLE = "rb-readable"
UNREADABLE = "rb-unreadable"
TEAM_RB = "rb-shared-to-viewers-team"


class _Viewer:
    user_id = VIEWER_ID
    enterprise_id = "ent-1"


class _Knowledge:
    """``visible_document_ids`` double: records every call."""

    def __init__(self, readable: Set[str], team_readable: Set[str] = frozenset()):
        self.readable = set(readable)
        self.team_readable = set(team_readable)
        self.calls: List[dict] = []

    async def visible_document_ids(self, document_ids, user=None, team_ids=None):
        self.calls.append(
            {"ids": list(document_ids), "user": user, "team_ids": list(team_ids or [])}
        )
        visible = self.readable | (self.team_readable if team_ids else set())
        return {d for d in document_ids if d in visible}


class _Teams:
    def __init__(self, *, fail: bool = False):
        self.fail = fail

    async def list_all_user_team_ids(self, user_id):
        if self.fail:
            raise RuntimeError("team lookup failed")
        return ["team-of-viewer"]


def _kb(document_id: Optional[str], *, new: Optional[bool] = True) -> Source:
    metadata: dict = {"title": f"Runbook {document_id}", "trigger": "symptom"}
    if document_id is not None:
        metadata["document_id"] = document_id
    return Source(
        type=SourceType.KNOWLEDGE_BASE,
        content=f"excerpt of {document_id}",
        confidence=0.9,
        metadata=metadata,
        new_this_turn=new,
    )


def _redacted(runbook: int) -> dict:
    """A redacted entry's fields: only the access flag and the runbook number
    (#2824) survive in ``metadata``."""
    return {
        "type": SourceType.KNOWLEDGE_BASE,
        "content": "",
        "confidence": None,
        "metadata": {"access": RESTRICTED_SOURCE_ACCESS, "runbook": runbook},
    }


def _assert_redacted(
    source: Source, *, runbook: int = 1, new: Optional[bool] = True
) -> None:
    dumped = source.model_dump()
    for key, value in _redacted(runbook).items():
        assert dumped[key] == value, f"{key}: {dumped[key]!r}"
    assert dumped["new_this_turn"] is new
    assert "excerpt" not in str(dumped) and "Runbook" not in str(dumped)


async def _gate(lists, knowledge: Any = None, teams: Any = None):
    return await gate_kb_sources(
        lists,
        viewer=_Viewer(),
        knowledge_service=knowledge,
        team_service=teams if teams is not None else _Teams(),
    )


async def test_a_readable_source_is_returned_unchanged():
    source = _kb(READABLE)

    ((gated,),) = await _gate([[source]], _Knowledge({READABLE}))

    assert gated == source


async def test_an_unreadable_source_is_redacted():
    (gated,) = await _gate(
        [[_kb(READABLE), _kb(UNREADABLE, new=False)]], _Knowledge({READABLE})
    )

    assert gated[0] == _kb(READABLE)
    _assert_redacted(gated[1], new=False)


async def test_a_runbook_shared_to_the_viewers_team_is_readable():
    knowledge = _Knowledge(set(), team_readable={TEAM_RB})

    ((gated,),) = await _gate([[_kb(TEAM_RB)]], knowledge)

    assert gated == _kb(TEAM_RB)
    assert knowledge.calls[0]["team_ids"] == ["team-of-viewer"]


async def test_a_failed_team_lookup_loses_the_team_arm_not_the_gate():
    ((gated,),) = await _gate(
        [[_kb(TEAM_RB)]], _Knowledge(set(), team_readable={TEAM_RB}), _Teams(fail=True)
    )

    _assert_redacted(gated)


@pytest.mark.parametrize("document_id", [None, ""])
async def test_a_source_with_no_document_id_fails_closed(document_id):
    knowledge = _Knowledge({READABLE})

    ((gated,),) = await _gate([[_kb(document_id)]], knowledge)

    _assert_redacted(gated)
    assert knowledge.calls == [], "nothing checkable: no visibility query"


async def test_no_knowledge_service_redacts_every_runbook_excerpt():
    ((gated,),) = await _gate([[_kb(READABLE)]], None)

    _assert_redacted(gated)


async def test_other_source_types_pass_unchanged():
    other = Source(type=SourceType.LOG_FILE, content="a log line", metadata={})

    ((gated,),) = await _gate([[other]], _Knowledge(set()))

    assert gated == other


async def test_one_visibility_query_covers_every_list():
    knowledge = _Knowledge({READABLE})
    lists = [[_kb(READABLE), _kb(UNREADABLE)], None, [], [_kb(READABLE), _kb(TEAM_RB)]]

    gated = await _gate(lists, knowledge)

    assert len(knowledge.calls) == 1
    assert sorted(knowledge.calls[0]["ids"]) == sorted({READABLE, UNREADABLE, TEAM_RB})
    assert gated[1] is None and gated[2] == []
    assert [s.content for s in gated[3]] == [f"excerpt of {READABLE}", ""]


async def test_no_runbook_sources_means_no_query():
    knowledge = _Knowledge({READABLE})

    assert await _gate([None, []], knowledge) == [None, []]
    assert knowledge.calls == []


async def test_the_knowledge_service_fails_closed_on_a_lookup_error():
    """``visible_document_ids`` answers the empty set when it cannot evaluate
    the rule, so every excerpt is withheld rather than shown."""
    from unittest.mock import MagicMock

    from faultmaven.modules.knowledge.domain.services.knowledge_service import (
        KnowledgeService,
    )

    def _broken_factory():
        raise RuntimeError("database unavailable")

    service = KnowledgeService(
        sanitizer=MagicMock(),
        tracer=MagicMock(),
        vector_store=MagicMock(),
        db_session_factory=_broken_factory,
    )

    assert await service.visible_document_ids([READABLE], user=_Viewer()) == set()
    ((gated,),) = await _gate([[_kb(READABLE)]], service)
    _assert_redacted(gated)


# ----------------------------------------------------------------------------
# The runbook number on a redacted entry (#2824, owner ruling option (a))
# ----------------------------------------------------------------------------

RB_A = "rb-a"
RB_B = "rb-b"


def _numbers(sources: List[Source]) -> List[Any]:
    return [(s.metadata or {}).get("runbook") for s in sources]


async def test_redacted_sections_of_one_runbook_share_a_number():
    """Sections of one runbook share it, two runbooks get 1 and 2 by first
    appearance, an id-less entry gets its own, and a readable entry between
    them takes none and is unchanged."""
    lists = [[_kb(RB_A), _kb(READABLE), _kb(RB_B), _kb(None), _kb(RB_A), _kb(RB_B)]]

    (gated,) = await _gate(lists, _Knowledge({READABLE}))

    assert gated[1] == _kb(READABLE)
    assert _numbers(gated) == [1, None, 2, 3, 1, 2]
    for source, number in zip(gated[:1] + gated[2:], [1, 2, 3, 1, 2]):
        _assert_redacted(source, runbook=number)


async def test_id_less_entries_each_draw_the_next_number_in_order():
    (gated,) = await _gate(
        [[_kb(None), _kb(RB_A), _kb(""), _kb(RB_A), _kb(None)]], _Knowledge(set())
    )

    assert _numbers(gated) == [1, 2, 3, 2, 4]


async def test_numbers_follow_appearance_not_the_ids_sort_order():
    """The visibility query takes the ids sorted; the numbering must not."""
    (gated,) = await _gate([[_kb("z-runbook"), _kb("a-runbook")]], _Knowledge(set()))

    assert _numbers(gated) == [1, 2]


async def test_each_list_in_one_call_is_numbered_on_its_own():
    """A transcript page gates every row in one call; each row starts at 1,
    and a runbook second in one row and first in the next gets 2, then 1."""
    knowledge = _Knowledge(set())

    first, second = await _gate(
        [[_kb(RB_A), _kb(RB_B)], [_kb(RB_B), _kb(None), _kb(RB_A)]], knowledge
    )

    assert len(knowledge.calls) == 1
    assert _numbers(first) == [1, 2]
    assert _numbers(second) == [1, 2, 3]


async def test_a_redacted_entry_carries_only_the_access_flag_and_the_number():
    (gated,) = await _gate([[_kb(RB_A, new=False)]], _Knowledge(set()))

    assert gated[0].metadata == {"access": RESTRICTED_SOURCE_ACCESS, "runbook": 1}
    assert type(gated[0].metadata["runbook"]) is int
    _assert_redacted(gated[0], new=False)


async def test_a_viewer_who_can_read_everything_gets_the_stored_list_unchanged():
    stored = [_kb(RB_A), _kb(RB_B), _kb(RB_A)]

    (gated,) = await _gate([stored], _Knowledge({RB_A, RB_B}))

    assert gated == stored
