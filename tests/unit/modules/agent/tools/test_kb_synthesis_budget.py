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

import pytest

from faultmaven.core.investigation.milestone_engine import MilestoneEngine
from faultmaven.models.interfaces import ToolResult
from faultmaven.modules.agent.tools.document_qa_tool import SYNTHESIS_MAX_TOKENS

pytestmark = [pytest.mark.unit]

# Densest measured encoding. The DENSEST value is the load-bearing one: fewer
# characters per token means more tokens are needed to fill the same space, so
# sizing against it is what guarantees the budget can reach the cap.
MIN_CHARS_PER_TOKEN = 3.909

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
