"""The KB synthesis budget must agree with the ceiling that actually binds.

``SYNTHESIS_MAX_TOKENS`` is not the real limit on a KB answer. The engine
truncates every tool result to ``MilestoneEngine.TOOL_RESULT_MAX_CHARS`` before
it re-enters the model's context, and the kb_qa result carries a fixed relay
wrapper on top of the answer. Whatever the model writes beyond that allowance is
generated, paid for, and then discarded.

Two constants in different modules therefore have to stay in step, and nothing
structural keeps them there: raising the token budget alone buys only wasted
generation, and raising the character cap alone leaves the model unable to fill
it. This pins the agreement in BOTH directions so either change fails loudly
here rather than silently degrading answers.

The chars/token figures are measured on real synthesis calls, not assumed:
7285/1774, 5261/1346 and 7729/1970 give 3.91-4.11 characters per token for this
prompt's mix of prose, shell commands and markdown.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.milestone_engine import (
    KB_QA_RELAY_SUFFIX,
    MilestoneEngine,
)
from faultmaven.models.interfaces import ToolResult
from faultmaven.modules.agent.tools.document_qa_tool import (
    KB_ANSWER_RELAY_CHARS,
    SYNTHESIS_MAX_TOKENS,
    DocumentQATool,
)
from faultmaven.modules.agent.tools.kb_configs.case_evidence_config import (
    CaseEvidenceConfig,
)
from faultmaven.modules.agent.tools.kb_configs.unified_kb_config import UnifiedKBConfig

pytestmark = [pytest.mark.unit]

# Densest measured encoding. The DENSEST value is the load-bearing one: fewer
# characters per token means more tokens are needed to fill the same space, so
# sizing against it is what guarantees the budget can reach the cap.
MIN_CHARS_PER_TOKEN = 3.909

# Sparsest measured encoding, used to size the LARGEST answer the budget can
# produce: more characters per token means a longer string for the same tokens.
MAX_CHARS_PER_TOKEN = 4.107

# How far past the usable allowance the budget may sit before it is buying
# generation the engine throws away. Some slack is right -- the encoding varies
# per answer -- but a budget that could write half again as much as can ever be
# accepted is not sized to this pipeline.
MAX_OVERSHOOT = 1.5


def _wrapper_overhead_chars() -> int:
    """Characters the kb_qa relay wrapper adds around the answer.

    Measured by formatting a known payload and subtracting it, rather than
    hardcoded, so editing the wrapper text keeps this test honest instead of
    stale. The sentinel is non-empty deliberately: the wrapper branch is guarded
    by ``result.data`` being truthy, so an empty payload would skip it and
    silently measure the wrong thing.
    """
    sentinel = "X"
    wrapped = MilestoneEngine._format_tool_result(
        ToolResult(success=True, data=sentinel), tool_name="kb_qa"
    )
    assert sentinel in wrapped, "sentinel did not reach the kb_qa wrapper branch"
    return len(wrapped) - len(sentinel)


def test_synthesis_budget_can_fill_the_engine_tool_result_cap():
    """Under-sizing clips the answer mid-procedure, and clips it silently."""
    usable = MilestoneEngine.TOOL_RESULT_MAX_CHARS - _wrapper_overhead_chars()
    reachable = SYNTHESIS_MAX_TOKENS * MIN_CHARS_PER_TOKEN

    assert reachable >= usable, (
        f"synthesis budget {SYNTHESIS_MAX_TOKENS} tokens reaches only "
        f"{reachable:.0f} characters, short of the {usable} the engine will "
        f"accept -- answers would be cut off before the cap is even reached"
    )


def test_synthesis_budget_is_not_sized_past_what_the_engine_accepts():
    """Over-sizing is not free: those tokens are generated, billed, then dropped.

    This is the direction that looks harmless and is not. Raising the budget to
    'give the answer room' does nothing on its own, because the extra text never
    survives ``TOOL_RESULT_MAX_CHARS`` -- it only adds latency to every KB turn.
    """
    usable = MilestoneEngine.TOOL_RESULT_MAX_CHARS - _wrapper_overhead_chars()
    reachable = SYNTHESIS_MAX_TOKENS * MIN_CHARS_PER_TOKEN

    assert reachable <= usable * MAX_OVERSHOOT, (
        f"synthesis budget {SYNTHESIS_MAX_TOKENS} tokens can write "
        f"{reachable:.0f} characters but the engine accepts at most {usable}; "
        f"the excess is generated and discarded. Raise "
        f"TOOL_RESULT_MAX_CHARS together with it, or leave the budget alone"
    )


def test_relay_instructions_survive_the_largest_answer_the_budget_allows():
    """The wrapper's tail is instructions, not prose — it must not be truncated.

    ``_format_tool_result`` places the citation format and "return the
    structured response by calling the response schema tool. Do not reply with
    plain text." AFTER the answer, and the engine truncates by keeping the HEAD.
    An answer large enough to overflow therefore used to strip the instructions
    that tell the model how to answer at all — the opposite of the intended
    failure, and silent. The wrapper reserves its own space instead, trimming
    the answer rather than itself.
    """
    biggest_answer = "A" * int(SYNTHESIS_MAX_TOKENS * MAX_CHARS_PER_TOKEN)
    wrapped = MilestoneEngine._format_tool_result(
        ToolResult(success=True, data=biggest_answer), tool_name="kb_qa"
    )

    assert len(wrapped) <= MilestoneEngine.TOOL_RESULT_MAX_CHARS, (
        f"wrapped kb_qa result is {len(wrapped)} chars, past the "
        f"{MilestoneEngine.TOOL_RESULT_MAX_CHARS} cap — the engine would cut the tail"
    )
    assert "Do not reply with plain text." in wrapped, (
        "the schema-tool instruction was truncated away; the model is no longer "
        "told how to return its response"
    )
    assert "SOURCE CITATION" in wrapped, "the citation guidance was truncated away"


def test_relay_tail_survives_redaction_growth_past_the_cap():
    """PII redaction runs BEFORE truncation, and it EXPANDS text.

    Each entity becomes a ``<TYPE_digest>`` placeholder — an IPv4 address grows
    from 8 characters to 29 — so an answer wrapped to exactly the budget
    re-crosses the cap once its entities are replaced. A reservation made while
    formatting cannot hold across that step, which is why the tail protection
    lives at the truncation site. Without it the generic head-first cut would
    remove the schema-tool instruction again, on any deployment with redaction
    enabled and entities in the answer.
    """
    wrapped = MilestoneEngine._format_tool_result(
        ToolResult(success=True, data="A" * 7400), tool_name="kb_qa"
    )
    # Stand in for sanitisation: entities replaced by longer placeholders.
    grown = wrapped.replace("A" * 50, "<IP_ADDRESS_0123456789abcdef>" * 30, 1)
    assert (
        len(grown) > MilestoneEngine.TOOL_RESULT_MAX_CHARS
    ), "test setup must actually push the result past the cap"

    out = MilestoneEngine._truncate_tool_result(grown, "kb_qa")

    assert len(out) <= MilestoneEngine.TOOL_RESULT_MAX_CHARS
    assert out.endswith(
        KB_QA_RELAY_SUFFIX
    ), "the relay instructions were cut by the post-redaction truncation"
    assert "Do not reply with plain text." in out


def test_other_tools_keep_plain_head_truncation():
    """The tail protection is scoped to kb_qa and must not change other tools.

    search_file and deep_analysis results are data, not instructions, and their
    tools already budget themselves against this cap; silently relocating their
    cut would change what the model sees for reasons unrelated to this fix.
    """
    out = MilestoneEngine._truncate_tool_result("X" * 9000, "search_file")

    assert out.startswith("X" * 100)
    assert out.endswith("[truncated]")
    assert KB_QA_RELAY_SUFFIX not in out


# ---------------------------------------------------------------------------
# The allowance the synthesizer is TOLD it has (#1088)
# ---------------------------------------------------------------------------
#
# The two tests above keep the TOKEN budget in step with the character cap.
# They do not make the model aware of either: it was instructed to preserve
# full procedural detail with no length target at all, so it wrote to whatever
# the material wanted and the engine removed the surplus. Measured over one
# simulation run, 3 KB answers in 5 overflowed, by 540-1249 characters.
#
# ``KB_ANSWER_RELAY_CHARS`` is that missing target. It lives in the tool rather
# than being imported from the engine because ``milestone_engine`` imports this
# package's tools -- the dependency runs one way only -- so it needs the same
# cross-module pin the token budget already has.


def test_stated_allowance_fits_inside_what_the_engine_will_relay():
    """Telling the model a number larger than the cap teaches it to overflow."""
    usable = MilestoneEngine.TOOL_RESULT_MAX_CHARS - _wrapper_overhead_chars()

    assert KB_ANSWER_RELAY_CHARS <= usable, (
        f"the synthesizer is told it has {KB_ANSWER_RELAY_CHARS} characters but "
        f"the engine relays at most {usable}; the instruction would license "
        f"exactly the overflow it exists to prevent"
    )


def test_stated_allowance_leaves_room_for_the_source_line():
    """``format_response`` appends "Sources: ..." to this answer before wrapping.

    That line is part of what has to fit, and it is not free — five runbook
    titles run comfortably past 100 characters. An allowance equal to the full
    relay budget would be overflowed by the source line alone.
    """
    usable = MilestoneEngine.TOOL_RESULT_MAX_CHARS - _wrapper_overhead_chars()

    assert usable - KB_ANSWER_RELAY_CHARS >= 250, (
        f"only {usable - KB_ANSWER_RELAY_CHARS} characters of headroom between "
        f"the stated allowance and the relay budget — not enough for the "
        f"appended source line"
    )


def test_stated_allowance_is_not_so_small_it_wastes_the_budget():
    """The other direction: an allowance far under the cap throws away room.

    Sized against the token budget, which is itself sized to the cap: an
    allowance the budget cannot even reach would be telling the model to stop
    well short of what the pipeline will happily carry.
    """
    reachable = SYNTHESIS_MAX_TOKENS * MIN_CHARS_PER_TOKEN

    assert KB_ANSWER_RELAY_CHARS >= reachable * 0.8, (
        f"the synthesizer is told it has {KB_ANSWER_RELAY_CHARS} characters "
        f"while its token budget reaches {reachable:.0f} and the engine would "
        f"accept them; the instruction is leaving answer room unused"
    )


@pytest.mark.asyncio
async def test_synthesis_prompt_tells_the_model_its_allowance():
    """The number has to reach the model, not just live in a constant.

    Without it the prompt asks for maximum procedural detail and says nothing
    about length, so "compress only background context, never actionable steps"
    has no threshold to act on and the model has no reason to apply it.
    """
    chunks = [
        {
            "content": "chunk",
            "metadata": {"title": "Kubernetes Container OOMKilled"},
            "score": 0.71,
        }
    ]
    vector_store = MagicMock()
    vector_store.hybrid_search = AsyncMock(return_value=chunks)
    vector_store.search = AsyncMock(return_value=chunks)

    router = MagicMock()
    router.route = AsyncMock(
        return_value=SimpleNamespace(content="answer", is_truncated=False)
    )

    tool = DocumentQATool(vector_store, router, UnifiedKBConfig())
    await tool.answer_question(question="Why was it OOMKilled?", scope_id=None, k=5)

    router.route.assert_awaited()
    prompt = router.route.await_args.kwargs["messages"][-1]["content"]

    assert f"{KB_ANSWER_RELAY_CHARS:,}" in prompt, (
        "the synthesis prompt does not state the character allowance; the "
        "model cannot prioritise remediation over background against a limit "
        "it has not been told"
    )
    assert "never diagnostic commands or remediation steps" in prompt, (
        "the allowance was stated without saying what to drop first, which is "
        "the half that protects the procedure"
    )


@pytest.mark.asyncio
async def test_case_evidence_answers_are_not_told_the_kb_relay_allowance():
    """The allowance is a fact about ONE KB's downstream channel.

    ``AnswerFromCaseEvidence`` subclasses ``DocumentQATool`` and shares this
    synthesis prompt, but its results are not relayed through the engine's
    ``kb_qa`` branch — ``_format_tool_result`` special-cases that name only. So
    an evidence answer gets no relay wrapper, no middle-elide, and a plain
    head-first cut at the full cap. Stating the kb_qa number there would shorten
    those answers against a ceiling that is not theirs, and describe an eliding
    behaviour they do not have.
    """
    chunks = [{"content": "chunk", "metadata": {"filename": "app.log"}, "score": 0.71}]
    vector_store = MagicMock()
    vector_store.hybrid_search = AsyncMock(return_value=chunks)
    vector_store.search = AsyncMock(return_value=chunks)

    router = MagicMock()
    router.route = AsyncMock(
        return_value=SimpleNamespace(content="answer", is_truncated=False)
    )

    tool = DocumentQATool(vector_store, router, CaseEvidenceConfig())
    await tool.answer_question(question="What errors?", scope_id="case_1", k=5)

    prompt = router.route.await_args.kwargs["messages"][-1]["content"]

    assert CaseEvidenceConfig().answer_char_allowance is None
    assert f"{KB_ANSWER_RELAY_CHARS:,}" not in prompt
    assert "Fit the answer within" not in prompt, (
        "the case-evidence answer was given a length allowance derived from a "
        "relay wrapper its results never pass through"
    )
