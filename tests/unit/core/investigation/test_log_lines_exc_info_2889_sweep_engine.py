"""The engine area's arms that log an unexpected failure before a 5xx carry its
frames (#2889, the ``exc_info`` sweep).

Principles §7, "What a log line may hold", rule 4, as ruled on #2886: "A line
that logs an unexpected failure before the request is answered with a 5xx
carries that exception's frames (``exc_info``)." Stage S3 of #2886.

The three arms precede a refusal that stood (#1770): the turn fails with
``MilestoneEngineError(PROVIDER_REFUSED)``, raised ``from None`` so that no
provider error on its cause chain re-routes the 503. The send's failure is on
no cause chain, so these lines are the only place its frames can be.

Each case drives one arm through ``generate_structured_output`` with the #1770
helpers and a send failure carrying :data:`MARKER`, captures every record at
DEBUG, and renders each through the real chain's formatter
(``build_log_formatter``). The marker is on no rendered line (the single-shot
case leaves out the one line #2889 keeps open, :data:`REMAINDER`), the arm's
line names the class and carries its frames, and the outcome is unchanged: the
refusal, unchained, which the turn route answers 503.
"""

import json

import pytest

from faultmaven.api.exception_handlers import llm_service_error_http_exception
from faultmaven.core.investigation.milestone_engine.errors import MilestoneEngineError
from faultmaven.exceptions import PROVIDER_REFUSED, LLMException, ServiceException
from tests.unit.core.investigation.test_refusal_reduced_retry_1770 import (
    REFUSED,
    SEARCHED,
    _engine,
    _raising_queue,
    _turn,
)
from tests.unit.infrastructure.llm.test_log_lines_name_exceptions_2889 import (
    MARKER,
    _named,
    lines,  # noqa: F401 - fixture
)

pytestmark = [pytest.mark.unit, pytest.mark.security]


def _framed(rendered: list[str], event: str, cls: str) -> None:
    """The arm's line names ``cls`` and carries its frames; no line holds the
    marker."""
    line = _named(rendered, event, cls)
    assert line["frames"].startswith(f"{cls}:"), line


#: The rule-1 remainder #2889 keeps open (``generation.py``'s "failed after
#: retries" line interpolates the original exception's text: recorded at
#: https://github.com/FaultMaven/faultmaven/issues/2889#issuecomment-7093858459).
#: Not this sweep's to change, so the single-shot case leaves that one line out
#: of its marker check; every other line, the arm's own included, is held to it.
REMAINDER = "Structured generation failed after retries"


def _without_the_remainder(rendered: list[str]) -> list[str]:
    return [line for line in rendered if REMAINDER not in json.loads(line)["event"]]


def _failure() -> LLMException:
    return LLMException(f"upstream {MARKER} rejected the call")


async def _refused(engine, *, tools: bool) -> MilestoneEngineError:
    """The turn's refusal, after asserting it is the unchained
    ``PROVIDER_REFUSED`` the route answers 503."""
    with pytest.raises(MilestoneEngineError) as raised:
        await _turn(engine, tools=tools)
    refusal = raised.value
    assert refusal.error_code == PROVIDER_REFUSED
    assert refusal.__cause__ is None
    try:
        raise ServiceException("Turn processing failed") from refusal
    except ServiceException as wrapped:
        assert llm_service_error_http_exception(wrapped).status_code == 503
    return refusal


# ── core/investigation/milestone_engine/generation.py ─────────────────────


async def test_a_failed_tool_loop_reissue_carries_its_frames(lines):
    """The tool loop's call is refused and its re-issue raises: the refusal,
    and the re-issue line with the failure's frames."""
    engine = _engine()
    engine.deps.da_provider._generate, _ = _raising_queue([REFUSED, _failure()])

    await _refused(engine, tools=True)

    _framed(lines(), "Tool loop: the re-issue failed", "LLMException")


async def test_a_failed_structured_output_reissue_carries_its_frames(lines):
    """The single shot is refused and its re-issue through the router raises:
    the refusal, and the re-issue line with the failure's frames."""
    engine = _engine()
    engine.deps.llm_provider.route, _ = _raising_queue([REFUSED, _failure()])

    await _refused(engine, tools=False)

    _framed(
        _without_the_remainder(lines()),
        "Structured output: re-issue failed",
        "LLMException",
    )


async def test_a_failed_generate_after_a_refusal_carries_its_frames(lines):
    """The tool loop's call is refused, its re-issue answers with a tool call,
    and the next generate raises: with a refusal seen there is no no-tools
    fallback, so the turn ends in the refusal, and the generate line carries
    the failure's frames."""
    engine = _engine()
    engine.deps.da_provider._generate, sent = _raising_queue(
        [REFUSED, SEARCHED, _failure()]
    )

    await _refused(engine, tools=True)

    assert len(sent) == 3
    _framed(lines(), "Tool loop: generate failed at iteration", "LLMException")
