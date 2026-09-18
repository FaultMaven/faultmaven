"""Every ``.format()`` key in a prompt template must be one the renderer supplies.

These templates are plain strings rendered later with ``.format(**ctx)``, so a
``{name}`` inside one is a RUNTIME context key, not a value. Writing
``{_SOME_CONSTANT}`` beside the f-string blocks in the same module reads as
interpolation and is not: the constant is never substituted and ``.format``
raises ``KeyError`` on the turn that renders it. A definition-time value is
CONCATENATED, never braced.

The trap is not only underscores. ``{PUBLIC_NAME}``, ``{ _x }`` with spaces,
``{obj.attr}`` and positional ``{2}`` all evade a "starts with an underscore"
proxy and all raise at render — ``_RCA_DIAGNOSIS_BLOCK`` really does parse to
the key ``'2'``, out of the regex literal ``[45][0-9]{2}``, and is safe only
because it is injected as a VALUE rather than formatted. So the invariant is
pinned directly: for each string the module actually formats, its key set must
be exactly the set the renderer passes.

Deriving the target list from the source rather than pinning it by hand is what
stops a NEW ``.format()`` target being added with no coverage — the failure this
file exists to prevent, one level up.
"""

from __future__ import annotations

import re
import string
from pathlib import Path

import pytest

from faultmaven.core.investigation.prompts import templates as t

pytestmark = pytest.mark.unit

#: Keys each formatted template is rendered with. Update deliberately, in step
#: with the renderer — a diff here means the prompt's contract with its caller
#: changed.
EXPECTED_KEYS: dict[str, set[str]] = {
    "_FALLBACK_FENCE_RULE_TEMPLATE": {
        "blocks",
    },
    "FALLBACK_INQUIRY_TEMPLATE": {
        "current_turn_evidence",
        "fence_preamble",
        "problem_summary",
        "user_message",
    },
    "FALLBACK_INVESTIGATION_TEMPLATE": {
        "current_turn_evidence",
        "fence_preamble",
        "hypotheses_summary",
        "journal_digest",
        "milestones_summary",
        "problem_summary",
        "stage",
        "user_message",
    },
    "FALLBACK_TERMINAL_TEMPLATE": {
        "fence_preamble",
        "problem_summary",
        "resolution_summary",
        "state",
        "user_message",
    },
    "INQUIRY_TEMPLATE": {
        "agent_meta_instructions",
        "conversation_history",
        "core_context",
        "evidence",
        "identity",
        "inquiry_state",
        "page_capture_hint",
        "system_feedback",
        "user_message",
    },
    "TERMINAL_TEMPLATE": {
        "conversation_history",
        "core_context",
        "identity",
        "state_lower",
        "state_upper",
        "summary_kind",
        "user_message",
    },
    "INVESTIGATION_BASE": {
        "adaptive_instructions",
        "candidate_solutions",
        "conversation_history",
        "core_context",
        "diagnostic_reasoning",
        "entity_highlights",
        "evidence",
        "evidence_grounding",
        "evidence_needs",
        "hypotheses",
        "identity",
        "investigation_journal",
        "kb_results",
        "milestones",
        "page_capture_hint",
        "pending_action",
        "system_feedback",
        "user_message",
        "working_conclusion",
    },
}


def _format_keys(text: str) -> set[str]:
    return {f[1] for f in string.Formatter().parse(text) if f[1]}


def _formatted_names() -> set[str]:
    """Names the module actually calls ``.format()`` on, read from the source.

    Read from source rather than listed by hand so a new formatted template
    cannot be introduced without this file noticing.
    """
    src = Path(t.__file__).read_text()
    return set(re.findall(r"\b([A-Z_][A-Z0-9_]*)\.format\(", src))


def test_the_set_of_formatted_templates_is_known():
    """Guard the guard: a new ``.format()`` target must be pinned before it ships."""
    assert _formatted_names() == set(EXPECTED_KEYS), (
        "formatted templates changed — add the new one to EXPECTED_KEYS with the "
        "keys its renderer supplies"
    )


@pytest.mark.parametrize("name", sorted(EXPECTED_KEYS))
def test_template_keys_match_what_the_renderer_supplies(name):
    value = getattr(t, name)
    assert isinstance(
        value, str
    ), f"{name} is not a string"  # never skip: skipping fails open
    assert _format_keys(value) == EXPECTED_KEYS[name]


@pytest.mark.parametrize("name", sorted(EXPECTED_KEYS))
def test_no_key_is_a_python_name_from_this_module(name):
    """The specific slip this guards: bracing a constant defined in the module.

    Checked separately from the pinned set because it names the failure, so a
    reader updating EXPECTED_KEYS sees why a module name must never appear.
    """
    leaked = {k for k in _format_keys(getattr(t, name)) if hasattr(t, k.strip())}
    assert not leaked, (
        f"{name} braces module-level name(s) {sorted(leaked)} — these are "
        "definition-time values and must be concatenated, not braced"
    )
