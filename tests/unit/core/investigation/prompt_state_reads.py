"""A whole case's prompt-state selections, read as the prompt reads them
(#2811 S2b-1c-i): the record and the fresh inputs of one ``NarrationFold``.
For the tests of a focus-emphasis line, whose function takes only the
selection (``narration.prompt_state``)."""

from faultmaven.core.investigation.narration.prompt_state import (
    PromptStateFresh,
    focus_zone,
)
from faultmaven.core.investigation.prompts.context_builder.narration_fold import (
    NarrationFold,
)
from faultmaven.modules.case.contracts import CaseOutcome


def selections(case) -> tuple[CaseOutcome, PromptStateFresh]:
    """The record ``case`` folds to and its fresh inputs."""
    fold = NarrationFold(case)
    outcome = fold.outcome("test")
    assert outcome is not None, "the fold raised"
    return outcome, fold.fresh()


def zone_emphasis(case) -> str:
    """The DIAGNOSIS zone's emphasis the record selects for ``case``."""
    from faultmaven.core.investigation.prompts.templates.assembly import (
        _get_diagnosis_focus_emphasis,
    )

    return _get_diagnosis_focus_emphasis(focus_zone(*selections(case)))
