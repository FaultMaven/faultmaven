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

import pytest

from faultmaven.core.investigation.milestone_engine import (
    KB_QA_ANSWER_TRUNCATED_MARKER,
    KB_QA_RELAY_PREFIX,
    KB_QA_RELAY_SUFFIX,
    MilestoneEngine,
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
