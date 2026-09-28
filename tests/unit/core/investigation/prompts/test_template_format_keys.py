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

from faultmaven.core.investigation.prompts.templates import (
    assembly,
    blocks,
    diagnosis,
    fallback,
    inquiry,
    investigation,
    terminal,
    treatment,
)
from faultmaven.core.investigation.prompts.templates.fallback import (
    _FALLBACK_FENCE_RULE_TEMPLATE,
    FALLBACK_INQUIRY_TEMPLATE,
    FALLBACK_INVESTIGATION_TEMPLATE,
    FALLBACK_TERMINAL_TEMPLATE,
)
from faultmaven.core.investigation.prompts.templates.inquiry import INQUIRY_TEMPLATE
from faultmaven.core.investigation.prompts.templates.investigation import (
    INVESTIGATION_BASE,
)
from faultmaven.core.investigation.prompts.templates.terminal import TERMINAL_TEMPLATE

pytestmark = pytest.mark.unit

#: `templates` has no facade any more (fm#1707): each formatted template is
#: read from the submodule that DEFINES it, never through a re-export.
_SUBMODULES = (
    assembly,
    blocks,
    diagnosis,
    fallback,
    inquiry,
    investigation,
    terminal,
    treatment,
)

#: Keys each formatted template is rendered with. Update deliberately, in step
#: with the renderer — a diff here means the prompt's contract with its caller
#: changed. Values are read from the module that defines each name (see the
#: imports above), not through any package-level re-export.
EXPECTED_KEYS: dict[str, set[str]] = {
    "_FALLBACK_FENCE_RULE_TEMPLATE": {
        "blocks",
    },
    "FALLBACK_INQUIRY_TEMPLATE": {
        "current_turn_evidence",
        "fence_preamble",
        "problem_summary",
        "system_feedback",
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
        "system_feedback",
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
        "focus_emphasis",
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

#: The value for each `EXPECTED_KEYS` name, read from its defining module —
#: the one canonical import path (fm#1707: no facade re-export).
_VALUES: dict[str, str] = {
    "_FALLBACK_FENCE_RULE_TEMPLATE": _FALLBACK_FENCE_RULE_TEMPLATE,
    "FALLBACK_INQUIRY_TEMPLATE": FALLBACK_INQUIRY_TEMPLATE,
    "FALLBACK_INVESTIGATION_TEMPLATE": FALLBACK_INVESTIGATION_TEMPLATE,
    "FALLBACK_TERMINAL_TEMPLATE": FALLBACK_TERMINAL_TEMPLATE,
    "INQUIRY_TEMPLATE": INQUIRY_TEMPLATE,
    "TERMINAL_TEMPLATE": TERMINAL_TEMPLATE,
    "INVESTIGATION_BASE": INVESTIGATION_BASE,
}


def _format_keys(text: str) -> set[str]:
    return {f[1] for f in string.Formatter().parse(text) if f[1]}


def _formatted_names() -> set[str]:
    """Names the package actually calls ``.format()`` on, read from the source.

    Read from source rather than listed by hand so a new formatted template
    cannot be introduced without this file noticing. ``templates`` is a
    package with no code of its own (an empty ``__init__.py``): a
    ``.format()`` call lives in one of its submodules (e.g. ``fallback.py``),
    so every ``.py`` file under the package directory is scanned.
    """
    pkg_dir = Path(fallback.__file__).parent
    src = "".join(p.read_text() for p in sorted(pkg_dir.glob("*.py")))
    return set(re.findall(r"\b([A-Z_][A-Z0-9_]*)\.format\(", src))


def _package_state() -> dict[str, object]:
    """Every top-level name bound across the package's submodules.

    Mirrors what a flat facade used to re-export, so a ``{name}`` collision
    with a constant defined ANYWHERE in the package is still caught — not
    only within the formatted string's own defining module. None of these
    submodules binds another submodule as an attribute of itself (each uses
    ``from .blocks import X`` — a name import, not ``from . import blocks``),
    so this merge contains only genuine module-level values, never a
    submodule object; there is nothing to exclude by type.
    """
    merged: dict[str, object] = {}
    for mod in _SUBMODULES:
        merged.update(vars(mod))
    return merged


def test_the_set_of_formatted_templates_is_known():
    """Guard the guard: a new ``.format()`` target must be pinned before it ships."""
    assert _formatted_names() == set(EXPECTED_KEYS), (
        "formatted templates changed — add the new one to EXPECTED_KEYS with the "
        "keys its renderer supplies"
    )


@pytest.mark.parametrize("name", sorted(EXPECTED_KEYS))
def test_template_keys_match_what_the_renderer_supplies(name):
    value = _VALUES[name]
    assert isinstance(
        value, str
    ), f"{name} is not a string"  # never skip: skipping fails open
    assert _format_keys(value) == EXPECTED_KEYS[name]


@pytest.mark.parametrize("name", sorted(EXPECTED_KEYS))
def test_no_key_is_a_python_name_from_this_module(name):
    """The specific slip this guards: bracing a constant defined in the package.

    Checked separately from the pinned set because it names the failure, so a
    reader updating EXPECTED_KEYS sees why a module name must never appear.
    """
    state = _package_state()
    leaked = {k for k in _format_keys(_VALUES[name]) if k.strip() in state}
    assert not leaked, (
        f"{name} braces module-level name(s) {sorted(leaked)} — these are "
        "definition-time values and must be concatenated, not braced"
    )
