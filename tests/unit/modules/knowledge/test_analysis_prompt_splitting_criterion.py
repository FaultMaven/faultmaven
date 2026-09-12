"""The analysis prompt must split on what is OBSERVED, never on the fix.

#1375: `ANALYSIS_SYSTEM_PROMPT` said failure modes are distinct when they have
"different symptoms OR different resolutions". Causes of one failure have
different resolutions by definition, so that `OR` licensed one runbook per
cause — for ANY document, not only for a runbook fed back in. A legitimate
vendor guide covering one symptom (NGINX 502) with five causes analysed into
four failure modes and produced three runbooks.

The criterion lives in a prompt and its output is an LLM's, so no unit test can
prove the model obeys it; the measurement that does lives in
`tests/eval/conversion_splitting/`. What IS testable is that the rule the
measurement validated is still the rule being sent — this is a tripwire against
a silent revert, not a proof of behaviour.
"""

from __future__ import annotations

import pytest

from faultmaven.modules.knowledge.domain.services.conversion_service import (
    ANALYSIS_SYSTEM_PROMPT,
)
from faultmaven.modules.knowledge.domain.services.runbook_validator import (
    VALID_SYMPTOM_CLASSES,
)

pytestmark = [pytest.mark.unit, pytest.mark.knowledge_base]


def test_resolution_alone_does_not_make_a_distinct_failure_mode():
    """The exact clause that caused #1375 must not come back.

    Matched case-insensitively on the disjunction itself rather than on the old
    sentence, so a reworded revert ("differing symptoms or differing fixes")
    is caught too.
    """
    normalised = " ".join(ANALYSIS_SYSTEM_PROMPT.lower().split())

    for revived in (
        "different symptoms or different resolutions",
        "differing symptoms or differing resolutions",
        "different symptoms or different fixes",
    ):
        assert revived not in normalised, (
            f"the prompt distinguishes failure modes by resolution again ({revived!r}); "
            "causes of one failure differ by resolution, so this splits one "
            "runbook into one-per-cause — see tests/eval/conversion_splitting/"
        )


def test_the_prompt_states_the_merge_rule():
    """Several causes of one symptom are ONE failure mode, stated explicitly."""
    normalised = " ".join(ANALYSIS_SYSTEM_PROMPT.lower().split())

    assert (
        "root causes of the same observable symptom are one failure mode" in normalised
    )
    assert "differing only in resolution is not distinct" in normalised


def test_the_prompt_keeps_the_split_rule_too():
    """And genuinely different symptoms still split — the over-correction guard.

    A criterion that merged everything would score well on the one-symptom
    documents and destroy the four-symptom control. The prompt has to say both
    halves, and `tests/eval/conversion_splitting/multi-failure-guide.md` is
    where the behaviour is actually measured.
    """
    normalised = " ".join(ANALYSIS_SYSTEM_PROMPT.lower().split())

    assert "different observable symptoms are different failure modes" in normalised


def test_the_symptom_class_test_is_offered_from_the_real_vocabulary():
    """The merge test names `symptom_class`, and the vocabulary is still bound.

    The prompt tells the model to merge two candidates carrying the same
    `symptom_class` for the same `service` — which is only a usable test if the
    controlled vocabulary is actually interpolated, as it has been since the
    dedup-key fix.
    """
    assert "symptom_class" in ANALYSIS_SYSTEM_PROMPT
    assert "__SYMPTOM_CLASS_VOCAB__" not in ANALYSIS_SYSTEM_PROMPT
    for value in VALID_SYMPTOM_CLASSES:
        assert value in ANALYSIS_SYSTEM_PROMPT


def test_eval_corpus_expectations_are_recorded_next_to_the_documents():
    """The documents the criterion was measured on ship with their answers.

    A prompt change that cannot be re-measured is a prompt change nobody can
    review; this keeps the corpus and its expected counts findable from the
    test that guards the rule.
    """
    from pathlib import Path

    corpus = Path(__file__).resolve().parents[4] / "tests/eval/conversion_splitting"
    readme = (corpus / "README.md").read_text()

    documents = sorted(p.name for p in (corpus / "documents").glob("*.md"))
    assert documents == [
        "multi-failure-guide.md",
        "postmortem-one-incident.md",
        "vendor-guide-one-symptom.md",
    ]
    for name in documents:
        assert name in readme, f"{name} has no recorded expectation"
