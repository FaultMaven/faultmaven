"""#2499: a notice the engine wrote for the model reaches the next prompt
whole, or not at all.

The record stores every notice of a turn as a unit (``system_notices``), and
the prompt drops whole notices from the tail of that list when its budget is
short, prepended notices last, saying how many it dropped. Before, the turn
record cut the "\\n"-joined text at 980 characters, so the newest notices went
mid-sentence: the issue's shapes are real turns of the #1906 harness whose
notices exceed that cut. No live LLM.
"""

import pytest

from faultmaven.core.investigation.milestone_engine.stage_gates import (
    _add_system_feedback,
    add_evidence_validation_feedback,
    merge_system_notices,
)
from faultmaven.core.investigation.milestone_engine.turn_records import (
    _create_turn_record,
)
from faultmaven.core.investigation.prompts.context_builder import (
    assembly as context_assembly,
)
from faultmaven.core.investigation.prompts.context_builder.assembly import (
    system_feedback_block,
)
from faultmaven.core.investigation.prompts.templates.assembly import (
    get_prompt_for_case,
)
from faultmaven.core.investigation.terminal_transitions import (
    PENDING_CHECK_DECLINE_CLAUSE,
)
from faultmaven.modules.case.contracts import (
    MAX_SYSTEM_NOTICE_CHARS,
    MAX_SYSTEM_NOTICES,
    NOTICE_CUT_MARKER,
    TurnOutcome,
)
from tests.case_builders import with_declined_offer
from tests.unit.core.investigation.given_tools import DEFAULT_GIVEN
from tests.unit.core.investigation.test_pending_check_holds_the_offer_1906 import (
    _asked_at_7,
    _engine,
    _mark,
    _turn,
)
from tests.unit.core.investigation.test_system_feedback_delivery_1688 import (
    _investigating_case,
    _record,
)

pytestmark = pytest.mark.unit

_SUPERSEDE = "SUPERSEDE the need with the reason."
_OLD_CUT = 1000


def _ask(chars: int) -> str:
    """A ``request_text`` of exactly ``chars`` characters."""
    text = "Prometheus targets page showing job=billing UP on every scrape "
    return (text * (chars // len(text) + 1))[:chars]


def _record_of(*notices: str, turn: int = 6):
    return _create_turn_record(
        turn_number=turn,
        milestones_completed=[],
        evidence_added=[],
        hypotheses_generated=[],
        hypotheses_validated=[],
        solutions_proposed=[],
        progress_made=False,
        outcome=TurnOutcome.CONVERSATION,
        user_message="please continue",
        agent_response="working on it",
        system_notices=list(notices),
    )


async def test_1_a_held_check_under_a_decline_keeps_both_notices_whole():
    """The issue's shape: a held symptom check under a standing resolve
    decline, the model proposes ``closed``, the need's ``request_text`` 118
    characters (swapped onto the need the real turn 7 created). The record
    holds the declined-reask notice and the held notice whole, and the next
    prompt carries the held notice's second exit, SUPERSEDE."""
    engine = _engine()
    case, _, need, _ = await _asked_at_7(engine)
    need.request_text = _ask(118)
    with_declined_offer(case, kind="resolve", basis="backstop")
    await _turn(engine, case, 8, "close it then", propose="closed")
    notices = case.turn_history[-1].system_notices
    assert sum(len(n) for n in notices) > _OLD_CUT, "premise: over the old cut"
    held = [n for n in notices if n.startswith("RESOLUTION HELD")]
    assert len(held) == 1 and held[0].endswith(PENDING_CHECK_DECLINE_CLAUSE)
    assert _SUPERSEDE in held[0]
    assert len(notices) >= 2 and notices[-1] == held[0], "the held notice is last"
    prompt = get_prompt_for_case(case, "what now?", given_tools=DEFAULT_GIVEN)
    assert _SUPERSEDE in prompt
    assert PENDING_CHECK_DECLINE_CLAUSE in prompt


async def test_2_a_fulfilled_need_at_the_field_maximum_is_held_whole():
    """The second shape: a FULFILLED need whose ``request_text`` is 500
    characters (the field's maximum); the held notice alone is over the old
    cut, and arrives whole."""
    engine = _engine()
    case, _, need, verifying = await _asked_at_7(engine)
    need.request_text = _ask(500)
    with_declined_offer(case, kind="resolve", basis="backstop")
    await _turn(
        engine,
        case,
        8,
        "checking the targets page",
        needs=[_mark(need, "fulfilled", verifying)],
    )
    held = [n for n in case.turn_history[-1].system_notices if "marked FULFILLED" in n]
    assert len(held) == 1 and len(held[0]) > _OLD_CUT, "premise: over the old cut"
    assert held[0].endswith(PENDING_CHECK_DECLINE_CLAUSE)


def test_3_many_backstops_are_all_stored_in_order_prepended_first():
    """Five notices totalling over the old cut: all five stored whole, the
    prepended one first, the rest chronological. Replaces the pin of the old
    ``[truncated]`` cut."""
    metadata: dict = {}
    notices = [f"BACKSTOP {i}: " + "refused, and here is why. " * 10 for i in range(5)]
    for i, notice in enumerate(notices):
        _add_system_feedback(metadata, notice, prepend=(i == 2))
    assert sum(len(n) for n in notices) > _OLD_CUT, "premise"
    assert "system_feedback" not in metadata, "the joined string is gone"
    record = _record_of(*metadata["system_notices"])
    assert record.system_notices == [notices[2], *notices[:2], *notices[3:]]


def test_4_the_prompt_drops_whole_notices_from_the_tail_and_says_so():
    """A budget that fits three of five: three whole notices and the line
    naming the two omitted. The prepended notice heads the list, so it is
    never the one dropped while a later one stands; the wiring passes the
    settings cap, so the full prompt renders the same line."""
    notices = [f"NOTICE {i}: " + "do this instead. " * 6 for i in range(5)]
    metadata: dict = {}
    for i, notice in enumerate(notices):
        _add_system_feedback(metadata, notice, prepend=(i == 4))
    ordered = metadata["system_notices"]
    case = _investigating_case()
    case.turn_history = [
        _record(2),
        _record(3).model_copy(update={"system_notices": ordered}),
    ]
    case.current_turn = 3
    fits_three = len("\n".join(ordered[:3])) + 120  # the heading and the line
    block = system_feedback_block(case, max_tokens=fits_three, count=len)
    for notice in ordered[:3]:
        assert notice in block
    for notice in ordered[3:]:
        assert notice not in block
    assert notices[4] in block, "the prepended notice stands"
    assert "[2 more notices from turn 3 omitted]" in block
    assert "truncated" not in block
    assert system_feedback_block(case).count("NOTICE ") == 5, "no budget: all"
    prompt = get_prompt_for_case(case, "what now?", given_tools=DEFAULT_GIVEN)
    assert "more notices from turn 3 omitted]" not in prompt, "the cap fits all"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(context_assembly, "_system_feedback_cap", lambda: 90)
        prompt = get_prompt_for_case(case, "what now?", given_tools=DEFAULT_GIVEN)
    assert "more notices from turn 3 omitted]" in prompt
    assert notices[4] in prompt, "the prepended notice stands"
    assert 1 <= prompt.count("NOTICE ") <= 4


def test_5_only_a_notice_over_the_per_notice_bound_is_cut_at_its_end():
    """A single notice over MAX_SYSTEM_NOTICE_CHARS is the one remaining cut:
    at its end, with the marker; its neighbours stay whole."""
    long = "L" * (MAX_SYSTEM_NOTICE_CHARS + 1)
    record = _record_of("first, whole", long, "last, whole")
    assert record.system_notices[0] == "first, whole"
    assert record.system_notices[2] == "last, whole"
    cut = record.system_notices[1]
    assert len(cut) == MAX_SYSTEM_NOTICE_CHARS and cut.endswith(NOTICE_CUT_MARKER)
    assert cut.startswith("L" * 100)


def test_6_a_merge_keeps_every_prepend_at_the_head():
    """The early steps' notices and the response's are two accumulators
    (``turn_application``, and a replayed bundle's in ``statement_revision``);
    their merge keeps prepended notices at the head, the later writer's
    first, then the chronological rest, earlier writer first. A notice already
    present is not repeated."""
    early: dict = {}
    _add_system_feedback(early, "early chronological")
    _add_system_feedback(early, "early prepended", prepend=True)
    live: dict = {}
    _add_system_feedback(live, "live chronological")
    _add_system_feedback(live, "early chronological")
    _add_system_feedback(live, "live prepended", prepend=True)
    merge_system_notices(early, live)
    assert early["system_notices"] == [
        "live prepended",
        "early prepended",
        "early chronological",
        "live chronological",
    ]
    assert early["system_notices_head"] == 2


def test_6_a_replayed_validation_line_merges_into_the_live_one():
    """A replayed bundle's EVIDENCE VALIDATION line and the live turn's
    become ONE line at the head naming both, not two with the replayed one
    in the droppable tail."""
    bundle: dict = {}
    _add_system_feedback(bundle, "replayed refusal")
    add_evidence_validation_feedback(
        bundle,
        not_recorded=["solution_accepted"],
        errors=["Milestones ['solution_accepted'] completed without justification."],
    )
    live: dict = {}
    add_evidence_validation_feedback(
        live,
        not_recorded=["symptom_verified"],
        errors=["Milestones ['symptom_verified'] completed without justification."],
    )
    merge_system_notices(live, bundle)
    notices = live["system_notices"]
    assert sum(n.startswith("EVIDENCE VALIDATION") for n in notices) == 1
    assert notices[0].startswith(
        "EVIDENCE VALIDATION: Milestones ['solution_accepted', 'symptom_verified'] "
        "were NOT recorded this turn."
    )
    assert "['symptom_verified', 'solution_accepted'] completed without" in notices[0]
    assert notices[1:] == ["replayed refusal"]


def test_7_a_first_notice_over_the_budget_is_head_cut_never_omitted():
    """When even the first notice does not fit, it is head-cut to the room
    with the record's marker and the rest are counted, singular when one:
    the block never omits everything. A whole notice that fits is never cut."""
    case = _investigating_case()
    long = "FIRST: " + "a long instruction. " * 40
    case.turn_history = [
        _record(3).model_copy(update={"system_notices": [long, "SECOND: short."]})
    ]
    case.current_turn = 3
    block = system_feedback_block(case, max_tokens=300, count=len)
    assert block.count(NOTICE_CUT_MARKER) == 1 and "FIRST: a long" in block
    assert "[1 more notice from turn 3 omitted]" in block
    assert "SECOND" not in block and len(block) <= 300 + 2
    alone = _record(3).model_copy(update={"system_notices": [long]})
    case.turn_history = [alone]
    assert "omitted]" not in system_feedback_block(case, max_tokens=300, count=len)
    assert NOTICE_CUT_MARKER in system_feedback_block(case, max_tokens=300, count=len)


def test_8_the_record_keeps_nineteen_and_says_how_many_it_dropped():
    """Past MAX_SYSTEM_NOTICES the record keeps the first nineteen and a
    twentieth saying how many more the turn wrote: nothing goes in silence."""
    notices = [f"NOTICE {i}" for i in range(MAX_SYSTEM_NOTICES + 1)]
    record = _record_of(*notices)
    assert len(record.system_notices) == MAX_SYSTEM_NOTICES
    assert record.system_notices[:-1] == notices[: MAX_SYSTEM_NOTICES - 1]
    assert record.system_notices[-1] == (
        "[2 more notices from this turn dropped at the record]"
    )
    assert _record_of(*notices[:MAX_SYSTEM_NOTICES]).system_notices == (
        notices[:MAX_SYSTEM_NOTICES]
    )
