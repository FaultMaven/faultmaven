"""Tests that the prompt templates teach only enum values the schema accepts.

When the prompt enumerates valid values for a field (``outcome`` is the
canonical example), each value listed in the prompt must also be a member
of the corresponding Python enum. Drift here causes Pydantic ValidationErrors
at structured-output parse time: the LLM emits exactly what the prompt
taught and the schema rejects it.

Prior to 2026-05-20, ``SCHEMA_INSTRUCTIONS`` taught the outcome values
``milestone_completed | data_requested | hypothesis_validated | conversation
| blocked``. Of those:
  - ``hypothesis_validated`` was not in ``TurnOutcome`` (the enum has
    ``hypothesis_tested``)
  - ``blocked`` was not in ``TurnOutcome`` at all
  - Four legitimate enum values (``data_provided``, ``data_not_provided``,
    ``case_resolved``, ``other``) were missing from the prompt

Result: the LLM regularly emitted ``hypothesis_validated`` per the prompt,
the schema rejected it, the self-correction retry cascade fired, and on
context-exhaustion the original (uncorrected) response shipped. See server
logs for ValidationError occurrences and the broader follow-ups handoff for
the self-correction cascade design discussion.

As of 2026-05-21 (Item 7 refactor), the outcome block in
``SCHEMA_INSTRUCTIONS`` is auto-generated from ``TurnOutcome.description``
at module load — there is no second source of truth to drift against.
These tests stay as regression checks: they catch a future hand-edit that
hardcodes the prompt block back, and they pin the auto-gen invariant
(every enum member contributes a line; no extra lines exist).
"""

from __future__ import annotations

import re

import pytest

from faultmaven.core.investigation.prompts.templates import SCHEMA_INSTRUCTIONS
from faultmaven.modules.case.domain.models import TurnOutcome


def _extract_outcome_values_from_prompt() -> set[str]:
    """Parse the ``outcome:`` block in SCHEMA_INSTRUCTIONS and return the
    backtick-quoted enum-name candidates. The prompt format uses inline
    backticks (```value```) for the enum tokens, so a simple regex
    captures them without needing to track formatting changes.
    """
    match = re.search(
        r"outcome:.*?(?=\n  -[a-zA-Z_]|\n\*\*|\n[A-Z]|\Z)",
        SCHEMA_INSTRUCTIONS,
        re.DOTALL,
    )
    assert (
        match is not None
    ), "SCHEMA_INSTRUCTIONS no longer contains an outcome enumeration"
    return set(re.findall(r"``([a-z_]+)``", match.group(0)))


# Parametrized exhaustiveness: same pattern as the IntentType dispatch-table
# exhaustiveness check. Each TurnOutcome value gets its own test case so a
# failure points at the exact missing value, not a batched set diff.


@pytest.mark.parametrize("outcome", list(TurnOutcome), ids=lambda v: v.value)
def test_every_enum_value_appears_in_prompt(outcome: TurnOutcome):
    """Every ``TurnOutcome`` enum value must appear in SCHEMA_INSTRUCTIONS so
    the LLM can discover and emit it. If the enum grows but the prompt is
    not updated, the LLM cannot reach the new value — silent drift.

    Pattern matches the IntentType dispatch-exhaustiveness check in
    ``test_investigation_service.py::test_every_intent_type_is_routed``.
    """
    prompt_values = _extract_outcome_values_from_prompt()
    assert outcome.value in prompt_values, (
        f"TurnOutcome.{outcome.name} ({outcome.value!r}) is not enumerated "
        f"in SCHEMA_INSTRUCTIONS. The LLM cannot emit this value. "
        f"Add it to the outcome block in templates.py:SCHEMA_INSTRUCTIONS."
    )


def test_prompt_contains_no_values_outside_enum():
    """The other direction: every value the prompt teaches must be a real
    enum value. Catches drift like the 2026-05-20 ``hypothesis_validated`` /
    ``blocked`` bug where the LLM emitted exactly what the prompt taught
    and Pydantic rejected it.

    Kept as a single test rather than parametrized because the dynamic
    "what's currently in the prompt" set isn't a stable parameter — the
    test catches additions to the prompt that aren't backed by the enum.
    """
    prompt_values = _extract_outcome_values_from_prompt()
    enum_values = {v.value for v in TurnOutcome}
    drifted = prompt_values - enum_values
    assert not drifted, (
        f"SCHEMA_INSTRUCTIONS teaches outcome values that are not in "
        f"TurnOutcome: {sorted(drifted)}. Update the prompt to use the real "
        f"enum names: {sorted(enum_values)}"
    )


@pytest.mark.parametrize("outcome", list(TurnOutcome), ids=lambda v: v.value)
def test_prompt_carries_runtime_description_for_each_value(outcome: TurnOutcome):
    """The auto-gen invariant: the description string stored on each
    enum member must appear in ``SCHEMA_INSTRUCTIONS``. Catches a future
    hand-edit that hardcodes the prompt block back (and thereby
    re-introduces the drift class).

    This is near-tautological today — the block IS generated from
    ``.description`` — but the test pins the contract so a refactor
    that switches to manual prompt text fails loudly.
    """
    assert outcome.description in SCHEMA_INSTRUCTIONS, (
        f"TurnOutcome.{outcome.name}.description "
        f"({outcome.description!r}) is not present in SCHEMA_INSTRUCTIONS. "
        f"Either the description was changed without regenerating the "
        f"prompt, or someone replaced the auto-generated block with a "
        f"hand-written list. Re-source the block from "
        f"templates._OUTCOME_PROMPT_BLOCK."
    )


def test_turn_outcome_descriptions_are_non_empty_and_distinct():
    """Each ``TurnOutcome`` value must have a non-empty, distinct
    description. Empty descriptions degrade the auto-generated prompt;
    duplicate descriptions make the values ambiguous to the LLM."""
    descriptions = [o.description for o in TurnOutcome]
    for o in TurnOutcome:
        assert o.description, f"TurnOutcome.{o.name} has empty description"
    assert len(set(descriptions)) == len(descriptions), (
        f"Duplicate descriptions across TurnOutcome members: "
        f"{descriptions}. Each value must be distinguishable to the LLM."
    )
