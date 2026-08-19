"""An over-long KB answer must lose its MIDDLE, not its remediation.

``_format_tool_result`` trims a kb_qa answer that will not fit the relay
budget. It used to keep the head, and for this one payload that direction is
wrong twice over (#1088):

1. The synthesis prompt is written to load the tail. It asks for "full
   diagnostic steps, commands, and resolution procedures" and says to
   "compress only background context, never actionable steps". A procedure's
   tail is its remediation, so a head-keeping cut deletes precisely what the
   prompt was written to protect and keeps the background it was told to
   compress.
2. ``UnifiedKBConfig.format_response`` appends the ``Sources:`` line to the end
   of the answer, and ``KB_QA_RELAY_SUFFIX`` — read by the model immediately
   afterwards — instructs it to cite "the primary source title(s) from the
   content above". A head-keeping cut removes that line before the model reads
   the instruction that depends on it.

Measured over one full simulation run on the instrumentation from #1090:
8 tool results relayed (5 kb_qa, 3 search_file), 3 clipped — all kb_qa, none
search_file — dropping 540, 655 and 1249 characters. So the cap binds this one
tool, and it binds it on 3 answers in 5.
"""

import logging
import re
from unittest.mock import patch

import pytest

from faultmaven.core.investigation import milestone_engine as me
from faultmaven.core.investigation.milestone_engine import (
    FENCE_REPAIR_RESERVE,
    KB_QA_ANSWER_TRUNCATED_MARKER,
    KB_QA_RELAY_PREFIX,
    KB_QA_RELAY_SUFFIX,
    PARAGRAPH_REALIGN_MAX_CHARS,
    MilestoneEngine,
    _balance_code_fences,
    _elide_answer_middle,
)
from faultmaven.models.interfaces import ToolResult

pytestmark = [pytest.mark.unit]

SOURCE_LINE = "Sources: Kubernetes Container OOMKilled, Java JVM OutOfMemoryError"
REMEDIATION_COMMAND = "-XX:MaxRAMPercentage=75.0"


def _answer_budget() -> int:
    return (
        MilestoneEngine.TOOL_RESULT_MAX_CHARS
        - len(KB_QA_RELAY_PREFIX)
        - len(KB_QA_RELAY_SUFFIX)
    )


def _runbook_answer(total_chars: int) -> str:
    """A KB answer shaped like a real one: framing, diagnostics, remediation.

    The remediation section and the source line sit at the END, which is where
    the synthesis prompt and ``format_response`` respectively put them. The
    middle is filler standing in for the background context the prompt says to
    compress first.
    """
    head = (
        "## Diagnostic and remediation procedure\n\n"
        "The container was terminated with exit code 137 (OOMKilled).\n"
    )
    tail = (
        "\n\n## Remediation\n\n"
        "1. Raise the container memory limit to 1.5Gi.\n"
        f"2. Size the heap against the container budget: `{REMEDIATION_COMMAND}`.\n"
        "3. Bound the in-process cache and redeploy.\n"
        "4. Validate: watch restartCount stay at 0 for one peak period.\n\n"
        f"{SOURCE_LINE}"
    )
    filler_len = total_chars - len(head) - len(tail)
    assert filler_len > 0, "test answer is too small to have a middle"
    paragraph = "Background: " + ("cache retention detail " * 12).strip() + "\n\n"
    filler = (paragraph * (filler_len // len(paragraph) + 1))[:filler_len]
    return head + filler + tail


def _split_retained(relayed: str) -> tuple:
    """The surviving head and tail of the answer, markers stripped."""
    body = relayed[len(KB_QA_RELAY_PREFIX) : -len(KB_QA_RELAY_SUFFIX)]
    if body.endswith(KB_QA_ANSWER_TRUNCATED_MARKER):
        body = body[: -len(KB_QA_ANSWER_TRUNCATED_MARKER)]
    head, _, tail = body.partition("[...")
    tail = tail.split("...]", 1)[1] if "...]" in tail else ""
    return head.rstrip(), tail.lstrip()


def _retained_answer_chars(relayed: str, answer: str) -> int:
    """Characters of *answer* still present in *relayed*, markers excluded."""
    body = relayed[len(KB_QA_RELAY_PREFIX) : -len(KB_QA_RELAY_SUFFIX)]
    if body.endswith(KB_QA_ANSWER_TRUNCATED_MARKER):
        body = body[: -len(KB_QA_ANSWER_TRUNCATED_MARKER)]
    head, _, tail = body.partition("[...")
    tail = tail.split("...]", 1)[1] if "...]" in tail else ""
    return len(head.rstrip()) + len(tail.lstrip())


def _relayed(answer: str) -> str:
    return MilestoneEngine._format_tool_result(
        ToolResult(success=True, data=answer), tool_name="kb_qa"
    )


# The largest overflow observed in the measured run was 1249 characters. This
# is deliberately larger, because #1100 gave the synthesizer a retry ceiling of
# twice the base budget, so the answers reaching this cut can only get longer.
OVERSIZED = 9200


def test_trimmed_answer_keeps_its_remediation_steps():
    """The tail of a procedure is the fix. Cutting head-first deleted it."""
    answer = _runbook_answer(OVERSIZED)
    assert len(answer) > _answer_budget(), "test setup must actually overflow"

    relayed = _relayed(answer)

    assert REMEDIATION_COMMAND in relayed, (
        "the remediation step was cut away; the model is left with the "
        "diagnosis and no fix, which is what the synthesis prompt explicitly "
        "asks to preserve"
    )
    assert "4. Validate" in relayed, "the verification step was cut away"


def test_trimmed_answer_keeps_the_source_line_the_relay_suffix_demands():
    """The suffix says to cite "from the content above" — so it must be there.

    Otherwise the instruction the model reads next names content that the trim
    has just removed, and the citation requirement is unsatisfiable.
    """
    relayed = _relayed(_runbook_answer(OVERSIZED))

    assert "SOURCE CITATION" in relayed, "sanity: the suffix instruction is present"
    assert SOURCE_LINE in relayed, (
        "the source line was trimmed away while the relay suffix still "
        "instructs the model to cite the titles from the content above"
    )


def test_the_marker_tells_the_model_the_middle_is_missing():
    """ "Some of the middle is gone" is a different fact from "it stops here".

    The model is asked to relay this answer onward, so which one it believes
    changes what it does with the text.
    """
    relayed = _relayed(_runbook_answer(OVERSIZED))

    assert "elided from the middle" in relayed
    assert "characters elided" in relayed
    # Deliberately hedged: an answer can arrive already cut by the provider
    # (#1094), and ``truncation.TRUNCATION_NOTICE`` then says it "stops
    # mid-answer". A marker asserting this IS the end would contradict that
    # notice inside the same string.
    assert "the END of the answer" not in relayed
    assert "as it was received" in relayed


def test_trimmed_result_still_fits_the_engine_cap():
    """Regression pin: both markers must be inside the budget, not on top of it."""
    relayed = _relayed(_runbook_answer(OVERSIZED))

    assert len(relayed) <= MilestoneEngine.TOOL_RESULT_MAX_CHARS, (
        f"relayed kb_qa result is {len(relayed)} chars, past the "
        f"{MilestoneEngine.TOOL_RESULT_MAX_CHARS} cap"
    )


def test_trimmed_result_still_anchors_the_truncation_metrics():
    """Regression pin for #1090's de-duplication contract.

    The tool loop recognises a formatter-trimmed kb_qa result by the answer
    marker sitting immediately before the relay suffix, and steps aside so the
    result is counted once rather than twice. Moving the cut must not move that
    anchor, or the clip rate stops being a rate.
    """
    relayed = _relayed(_runbook_answer(OVERSIZED))

    assert relayed.endswith(KB_QA_ANSWER_TRUNCATED_MARKER + KB_QA_RELAY_SUFFIX), (
        "the end-anchored answer marker moved; the tool loop would count this "
        "result a second time at its own cut site"
    )


def test_an_answer_that_fits_is_relayed_untouched():
    """The elision must fire only on overflow."""
    answer = _runbook_answer(_answer_budget() - 500)

    relayed = _relayed(answer)

    assert answer in relayed, "a fitting answer was modified"
    assert "elided" not in relayed
    assert KB_QA_ANSWER_TRUNCATED_MARKER not in relayed


def test_the_post_redaction_cut_also_keeps_the_tail():
    """The second cut must not undo the first.

    Redaction runs between the formatter and ``_truncate_tool_result`` and it
    EXPANDS text — every entity becomes a longer ``<TYPE_digest>`` placeholder —
    so an answer the formatter sized exactly to the budget re-crosses the cap.
    That second cut used to be head-first, protecting only the relay suffix,
    which discarded the very remediation and source line the formatter had just
    gone out of its way to preserve, and left the suffix instructing the model
    to cite titles from content no longer present.
    """
    relayed = _relayed(_runbook_answer(_answer_budget() - 400))
    # Stand in for sanitisation: entities replaced by longer placeholders.
    grown = relayed.replace("Background:", "<IP_ADDRESS_0123456789abcdef>" * 20)
    assert (
        len(grown) > MilestoneEngine.TOOL_RESULT_MAX_CHARS
    ), "test setup must actually push the result past the cap"

    out, _ = MilestoneEngine._truncate_tool_result(grown, "kb_qa")

    assert len(out) <= MilestoneEngine.TOOL_RESULT_MAX_CHARS
    assert out.endswith(KB_QA_RELAY_SUFFIX), "the relay instructions were cut"
    assert REMEDIATION_COMMAND in out, (
        "the post-redaction cut discarded the remediation steps the formatter "
        "preserved — the pre-#1088 failure, one step later"
    )
    assert SOURCE_LINE in out, (
        "the post-redaction cut discarded the source line while the suffix "
        "still instructs the model to cite it"
    )


def test_reported_dropped_chars_is_what_was_actually_removed(caplog):
    """The dashboard aggregates this field to size the ceiling.

    Two wrong answers are available and both look plausible.
    ``len(content) - budget`` is the overflow, not the loss -- the elide also
    spends its markers and its realignment. A before/after length difference is
    closer but nets the INSERTED markers off the loss, under-reporting by their
    combined length. The number has to be source characters destroyed.
    """
    answer = _runbook_answer(OVERSIZED)

    with caplog.at_level(logging.WARNING):
        relayed = _relayed(answer)

    records = [r for r in caplog.records if r.msg == "tool_result_truncated"]
    assert records, "the truncation was not reported at all"
    reported = records[-1].dropped_chars

    # Recover the surviving head and tail by splitting on the inline marker.
    body = relayed[len(KB_QA_RELAY_PREFIX) : -len(KB_QA_RELAY_SUFFIX)]
    body = body[: -len(KB_QA_ANSWER_TRUNCATED_MARKER)]
    head, _, tail = body.partition("[...")
    tail = tail.split("...]", 1)[1]
    retained = len(head.rstrip()) + len(tail.lstrip())

    assert reported == len(answer) - retained, (
        f"reported {reported} dropped characters; {len(answer) - retained} "
        f"characters of the answer are absent from the relayed result"
    )
    assert reported > len(answer) - (
        len(relayed) - len(KB_QA_RELAY_PREFIX) - len(KB_QA_RELAY_SUFFIX)
    ), "the count still nets the inserted markers off the loss"


def test_the_inline_marker_states_the_same_number_that_is_logged():
    """The model reads this number and the dashboard aggregates the other.

    Nothing structural keeps them equal, and a wrong one is invisible: the
    marker is prose to every reader downstream.
    """
    answer = _runbook_answer(OVERSIZED)

    relayed = _relayed(answer)

    stated = int(
        re.search(r"\[\.\.\. ([\d,]+) characters elided", relayed)
        .group(1)
        .replace(",", "")
    )
    assert stated > 0, "the marker claims nothing was elided"
    assert stated == len(answer) - _retained_answer_chars(relayed, answer)


def test_the_opening_gets_at_least_as_much_room_as_the_closing():
    """The docstring promises the opening survives; nothing pinned it.

    ``KB_QA_ANSWER_TAIL_SHARE`` reserves the tail, so raising it silently eats
    the head — at 0.9 the framing and the first diagnostic steps are gone while
    every other assertion here still passes. Stated as an ordering rather than
    a number so it constrains the share without restating it.
    """
    answer = _runbook_answer(OVERSIZED)

    relayed = _relayed(answer)
    head, tail = _split_retained(relayed)

    assert len(head) >= len(tail), (
        f"the surviving head is {len(head)} characters against a {len(tail)} "
        f"character tail — the answer's opening is being spent on its closing"
    )
    assert answer[:60] in relayed, "the answer's first line did not survive"


def test_the_reserved_tail_can_hold_a_whole_remediation_section():
    """A tail share can be too small as easily as too large.

    ``KB_QA_ANSWER_TAIL_SHARE`` exists to hold a remediation section AND the
    source line, not just the last paragraph — at 0.05 the ordering assertion
    above still holds while the reserved tail shrinks to a few hundred
    characters, too small for the procedure's conclusion on a real answer.
    """
    relayed = _relayed(_runbook_answer(OVERSIZED))
    _, tail = _split_retained(relayed)

    assert len(tail) >= 1200, (
        f"only {len(tail)} characters of the answer's ending survive — too "
        f"little for a remediation section plus its source line"
    )


def test_a_seam_inside_a_command_list_lands_on_a_line_boundary():
    """Command lists and fenced blocks contain no blank line.

    A realignment that searches only for a paragraph break therefore walks
    straight past the whole block and leaves the seam mid-command — the verb
    kept, its target truncated — which reads as a real, wrong instruction
    rather than as a visible cut.

    Unfenced deliberately: inside a fence the balancing pass appends a closing
    ``\u0060\u0060\u0060``, which would make the last line look clean whatever the seam did.
    """
    step = "1. Run `kubectl set resources deploy/checkout-api --limits=memory=1536Mi`"
    answer = (
        "## Diagnose\n\nThe container was OOMKilled.\n\n"
        + "Background filler paragraph about cache retention.\n\n" * 40
        + (step + "\n") * 100  # the head seam falls inside this list
        + "\nMore background.\n\n" * 40
        + "## Remediation\n\n1. `"
        + REMEDIATION_COMMAND
        + "`\n\n"
        + SOURCE_LINE
    )
    assert len(answer) > _answer_budget(), "test setup must overflow"

    relayed = _relayed(answer)
    head, _ = _split_retained(relayed)
    last_line = head.rstrip().rsplit("\n", 1)[-1]

    assert last_line == step or last_line.endswith("."), (
        f"the head seam landed mid-line on {last_line!r} — a truncated command "
        f"reads as an instruction, not as a cut"
    )


def test_the_tail_seam_also_resumes_on_a_line_boundary():
    """Both seams cut through the answer; both need the same boundary search.

    The tail's is the one that matters more — it resumes immediately before the
    remediation steps, so a mid-line start there hands the model a fragment of
    a command as the first thing it reads after the elision marker.
    """
    step = "3. Apply `kubectl set resources deploy/checkout-api --limits=memory=2Gi`"
    answer = (
        "## Diagnose\n\nThe container was OOMKilled.\n\n"
        + "Background filler paragraph about cache retention.\n\n" * 60
        + (step + "\n") * 60  # the tail seam falls inside this list
        + "\n## Remediation\n\n1. `"
        + REMEDIATION_COMMAND
        + "`\n\n"
        + SOURCE_LINE
    )
    assert len(answer) > _answer_budget(), "test setup must overflow"

    relayed = _relayed(answer)
    _, tail = _split_retained(relayed)
    first_line = tail.lstrip().split("\n", 1)[0]

    assert first_line == step or first_line.startswith(("#", "1.", "Sources:")), (
        f"the tail seam resumed mid-line on {first_line!r} — the first thing "
        f"the model reads after the elision is a command fragment"
    )


def test_realignment_does_not_rewind_through_a_long_unbroken_run():
    """The seam bound is what stops realignment eating answer text.

    Written against an absolute number rather than against
    ``PARAGRAPH_REALIGN_MAX_CHARS``, because a test phrased in terms of the
    constant it guards cannot detect that constant changing — at 4000 the
    original form of this assertion still passed.

    The seams are placed inside long runs with no line breaks at all, which is
    what a realignment search actually walks back through; densely paragraphed
    filler hides the cost because a boundary is always a few characters away.
    """
    budget = _answer_budget()
    run = "unbroken diagnostic prose without any line break at all. " * 60
    answer = (
        "## Diagnose\n\nFirst line of the answer.\n\n"
        + run  # swallows the head seam
        + "\n\n"
        + "Background filler paragraph.\n\n" * 20
        + run  # swallows the tail seam
        + "\n\n## Remediation\n\n1. `"
        + REMEDIATION_COMMAND
        + "`\n\n"
        + SOURCE_LINE
    )
    assert len(answer) > budget, "test setup must overflow"

    relayed = _relayed(answer)

    unused = MilestoneEngine.TOOL_RESULT_MAX_CHARS - len(relayed)
    assert unused <= 800, (
        f"{unused} characters of the cap went unused — realignment rewound "
        f"through a long unbroken run, discarding answer text to tidy a seam"
    )


def test_an_elide_through_a_fenced_block_leaves_the_fences_balanced():
    """An unbalanced fence makes everything after it read, and render, as code.

    The drop zone can hold the close of a block opened in the head, or the open
    of one closed in the tail. Both leave the relayed answer with an odd fence
    count, which the model asked to relay it and the Dashboard rendering the
    transcript both take literally.
    """
    budget = _answer_budget()
    answer = (
        "## Diagnose\n\n```bash\n"
        + "kubectl get pod checkout-api-0 -o yaml\n" * 200  # fence opens, closes late
        + "```\n\n"
        + "Background filler paragraph.\n\n" * 60
        + "```bash\n"
        + "kubectl set resources deploy/checkout-api --limits=memory=1536Mi\n" * 80
        + "```\n\n## Remediation\n\n1. `"
        + REMEDIATION_COMMAND
        + "`\n\n"
        + SOURCE_LINE
    )
    assert len(answer) > budget, "test setup must overflow"

    relayed = _relayed(answer)

    assert relayed.count("```") % 2 == 0, (
        "the elide left an unbalanced code fence; everything after it reads as "
        "code to the model and renders as code in the transcript"
    )


@pytest.mark.parametrize("budget", [0, 1, 12, 19, 20, 200])
def test_a_degenerate_budget_still_produces_something_that_fits(budget):
    """The fallback branch must fall back to something smaller, not larger.

    ``content[: budget - len(marker)]`` is a NEGATIVE slice once the budget is
    under the marker length: it returns the content minus its last few
    characters, thousands over the cap rather than under it. Reachable by the
    wrapper edit the branch exists to survive.
    """
    out, _ = _elide_answer_middle("A" * 9000, budget)

    assert len(out) <= max(
        budget, len(KB_QA_ANSWER_TRUNCATED_MARKER)
    ), f"a budget of {budget} produced {len(out)} characters"


def test_a_twice_cut_answer_carries_one_marker_not_two():
    """The formatter trims, redaction re-inflates, the loop trims again.

    One marker still says the true thing. Two in a row read as noise to the
    model that has to relay the answer onward.
    """
    relayed = _relayed(_runbook_answer(OVERSIZED))
    grown = relayed.replace("Background:", "<IP_ADDRESS_0123456789abcdef>" * 3)
    assert (
        len(grown) > MilestoneEngine.TOOL_RESULT_MAX_CHARS
    ), "setup must re-cross the cap"

    out, _ = MilestoneEngine._truncate_tool_result(grown, "kb_qa")

    assert out.count(KB_QA_ANSWER_TRUNCATED_MARKER) == 1
    assert len(out) <= MilestoneEngine.TOOL_RESULT_MAX_CHARS


def test_fence_repair_cannot_push_the_result_past_the_budget():
    """Repair inserts characters, so its room has to be reserved, not borrowed.

    Found by property testing rather than inspection, and pinned the same way,
    because a single hand-picked answer does not hit it: repair adds at most 8
    characters and paragraph realignment usually leaves more slack than that.
    Only answers whose seams both land inside fenced blocks AND whose
    realignment happens to land tight overflow the cap — 8 sizes in this
    2000-wide sweep, by 1–2 characters each.
    """
    unit = "```bash\nkubectl get pod checkout-api-0 -o yaml\n"
    over = []

    for n in range(7400, 9400, 7):
        answer = ("## Diagnose\n\n" + unit * 300)[:n]
        relayed = _relayed(answer)
        if len(relayed) > MilestoneEngine.TOOL_RESULT_MAX_CHARS:
            over.append((n, len(relayed) - MilestoneEngine.TOOL_RESULT_MAX_CHARS))

    assert not over, (
        f"fence repair pushed {len(over)} of these answers past the cap "
        f"(worst: {max(o[1] for o in over)} characters over) — its room is "
        f"being borrowed from the answer's budget instead of reserved"
    )


def test_the_fence_reserve_costs_nothing_when_repair_cannot_fire():
    """Reserved room is subtracted from the answer whether repair fires or not.

    Most KB answers carry no fenced block, so holding it back unconditionally
    spent up to 8 characters of answer on a repair that was never possible.
    Pinned by comparison against a zeroed reserve rather than an absolute
    length, because the other sources of slack (worst-case marker sizing,
    paragraph realignment) dwarf 8 characters and would hide it.
    """
    # No fences, and no line breaks either: paragraph realignment would
    # otherwise rewind both seams to the same boundary whatever the reserve
    # did, absorbing the 8 characters and hiding the difference.
    answer = "A" * (_answer_budget() + 3000)
    assert "```" not in answer, "this test needs fence-free content"

    with_reserve, dropped_with = _elide_answer_middle(answer, _answer_budget())
    with patch.object(me, "FENCE_REPAIR_RESERVE", 0):
        without_reserve, dropped_without = _elide_answer_middle(
            answer, _answer_budget()
        )

    assert with_reserve == without_reserve, (
        f"the fence reserve shortened an answer that contains no fence, so "
        f"repair could never have fired on it: kept "
        f"{len(with_reserve)} characters against {len(without_reserve)}"
    )
    assert dropped_with == dropped_without


def test_the_fence_reserve_is_no_larger_than_the_repair_it_covers():
    """Reserved room is answer text, so an over-generous reserve is pure loss.

    Repair appends one closing fence to the head and prepends one opening
    fence to the tail — four characters each, and never more, because the
    balance test is a parity check that fires at most once per side. Bounded
    against an independent number rather than against the expression itself,
    which would restate the constant and assert nothing.
    """
    assert FENCE_REPAIR_RESERVE <= 16, (
        f"{FENCE_REPAIR_RESERVE} characters held back for a repair that adds "
        f"at most two four-character fences — the surplus is answer text that "
        f"is discarded on every fenced answer"
    )

    # And it really is enough: repair never adds more than the reservation.
    fenced = "```bash\nkubectl get pod -o yaml\n" * 300
    head_raw = fenced[:3000]
    tail_raw = fenced[-2000:]
    added = len(_balance_code_fences(head_raw)) - len(head_raw)
    if tail_raw.count("```") % 2:
        added += len("```\n")
    assert added <= FENCE_REPAIR_RESERVE


def test_the_fence_reserve_still_applies_when_a_fence_is_present():
    """The other direction: content that can be repaired must keep the room."""
    unit = "```bash\nkubectl get pod checkout-api-0 -o yaml\n"
    over = []

    for n in range(7400, 9400, 7):
        answer = ("## Diagnose\n\n" + unit * 300)[:n]
        relayed = _relayed(answer)
        if len(relayed) > MilestoneEngine.TOOL_RESULT_MAX_CHARS:
            over.append(n)

    assert not over, f"{len(over)} fenced answers exceeded the cap"


def test_the_dropped_count_excludes_characters_repair_inserted():
    """Repair adds text that was never in the answer.

    Counting the kept slices after repair credits those inserted characters as
    retained content, under-reporting the loss — the same netting error the
    returned count exists to avoid, reintroduced by a later step.
    """
    fenced = "```bash\nkubectl get pod checkout-api-0 -o yaml\n" * 400
    answer = "## Diagnose\n\n" + fenced + "\n\n" + SOURCE_LINE

    _, dropped = _elide_answer_middle(answer, _answer_budget())

    assert 0 < dropped <= len(answer)
    assert dropped >= len(answer) - _answer_budget()


@pytest.mark.parametrize("slack", [0, 1, 500])
def test_content_that_already_fits_is_returned_untouched(slack):
    """The helper must be total: both callers gate on overflow, the next may not.

    With a budget above the content the head and tail slices overlap, so the
    result duplicated text and the dropped count went negative.
    """
    content = "A" * 1000

    out, dropped = _elide_answer_middle(content, len(content) + slack)

    assert out == content
    assert dropped == 0
