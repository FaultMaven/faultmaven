"""The structured generation's terminal log lines name an exception by its class
and code, never its text (#2889, the rule-1 remainder).

Principles §7, "What a log line may hold", rule 1: "A first-party exception
contributes its class and its ``error_code`` ..., not its message." Stage S3 of
#2886. Both lines read their exception through ``ErrorResult``'s
``original_exception``, the terminal one two assignments from it, a shape the
census did not read until ``EXCEPTION_ATTRS`` and its fixed-point name tracking
(``test_log_message_channel_census_2889.py``).

Each case drives ``generate_structured_output`` through the real
``LLMErrorHandler`` with :data:`MARKER` in the starved exception's text and, for
the terminal line, in a model-not-found failure's text, which
``ErrorResult.message`` also embeds. Every record is rendered through the real
chain's formatter; the marker is on no line, and the site's line names the
class chain and the code.
"""

import pytest

from faultmaven.core.investigation.milestone_engine.errors import MilestoneEngineError
from faultmaven.exceptions import LLMException, LLMOutputFloorError
from tests.unit.core.investigation.test_starved_inference_reissue_2802 import (
    ANSWER,
    INFERENCE,
    _engine,
    _queue,
    _single_shot,
)
from tests.unit.infrastructure.llm.test_log_lines_name_exceptions_2889 import (
    MARKER,
    _named,
    lines,  # noqa: F401 - fixture
)

pytestmark = [pytest.mark.unit, pytest.mark.security]


def _starved() -> LLMOutputFloorError:
    return LLMOutputFloorError(f"starved below the floor: {MARKER}")


def _model_not_found() -> LLMException:
    """``MODEL_NOT_FOUND``, whose ``ErrorResult.message`` embeds ``str(error)``
    (``llm_error_handler.py``)."""
    return LLMException(f"model {MARKER} not found", status_code=404)


async def test_the_starved_reissue_line_names_the_class(lines):
    """``generation.py``'s "reasoning starved the answer" line: the starved
    attempt is re-issued and answers."""
    engine = _engine()
    engine.deps.llm_provider.route, _ = _queue([_starved(), ANSWER])

    await _single_shot(engine, **INFERENCE)

    _named(lines(), "reasoning starved the answer", "(LLMOutputFloorError)")


async def test_the_failed_after_retries_line_names_classes_and_the_code(lines):
    """``generation.py``'s "failed after retries" line: the starved attempt's
    re-issue fails with a missing model. The line names both classes and the
    ``ErrorResult``'s code, and holds neither exception's text nor
    ``ErrorResult.message``; the raised error is unchanged."""
    engine = _engine()
    engine.deps.llm_provider.route, _ = _queue([_starved(), _model_not_found()])

    with pytest.raises(MilestoneEngineError) as raised:
        await _single_shot(engine, **INFERENCE)

    assert raised.value.error_code == "MODEL_NOT_FOUND"
    assert MARKER in str(raised.value)  # the raised message keeps ``detail``
    line = _named(lines(), "Structured generation failed after retries", "LLMException")
    assert line["event"] == (
        "Structured generation failed after retries: LLMException "
        "(error_code=MODEL_NOT_FOUND); after the INFERENCE attempt starved "
        "(LLMOutputFloorError)"
    )
