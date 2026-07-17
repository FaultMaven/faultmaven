"""Drift-guards for the v4 cause authoring-grammar vocabulary.

``cause_grammar`` is the single backend definition of the markdown sub-field
labels / quadrant tags / fallback token. These tests pin it three ways:

  1. frozen literals — so a careless edit fails the build and forces a conscious,
     cross-repo-coordinated change (the kb-toolkit ``config.py`` defaults mirror
     these; see that repo's ``test_cause_grammar_vocab``);
  2. to the ``InterventionQuadrant`` enum — the quadrant VALUES' real owner, so
     the authoring vocab can't drift from the engine's enum;
  3. to the two backend CONSUMERS — the validator (it must still enforce exactly
     this vocabulary) and the authoring prompt (it must instruct exactly this
     vocabulary), so neither silently diverges from the constants.
"""

import pytest

from faultmaven.modules.case.domain.models import InterventionQuadrant
from faultmaven.modules.knowledge.domain.services import conversion_service
from faultmaven.modules.knowledge.domain.services.cause_grammar import (
    FALLBACK_CAUSE_LETTER,
    FALLBACK_INDICATOR_TOKEN,
    INTERVENTION_QUADRANTS,
    LEGACY_V3_CAUSE_SUBFIELDS,
    OPTIONAL_CAUSE_SUBFIELDS,
    QUADRANT_ALTERNATION,
    REQUIRED_CAUSE_SUBFIELDS,
)
from faultmaven.modules.knowledge.domain.services.runbook_validator import (
    RunbookValidator,
)

pytestmark = pytest.mark.unit


class TestVocabularyFrozen:
    def test_required_subfields_frozen(self):
        # Mirrored in kb-toolkit config.required_cause_subfields — change both.
        assert REQUIRED_CAUSE_SUBFIELDS == ("Statement", "Indicators", "Interventions")

    def test_optional_subfields_frozen(self):
        # Mirrored in kb-toolkit config.optional_cause_subfields — change both.
        assert OPTIONAL_CAUSE_SUBFIELDS == ("Chain",)

    def test_quadrants_frozen(self):
        # Mirrored in kb-toolkit config.valid_quadrants — change both.
        assert INTERVENTION_QUADRANTS == (
            "remediation",
            "defensive_fix",
            "mitigation",
            "loop_break",
        )

    def test_quadrant_alternation_matches_tuple(self):
        assert QUADRANT_ALTERNATION == "|".join(INTERVENTION_QUADRANTS)

    def test_fallback_tokens_frozen(self):
        assert FALLBACK_INDICATOR_TOKEN == "[Default]"
        assert FALLBACK_CAUSE_LETTER == "Z"

    def test_legacy_subfields_frozen(self):
        assert LEGACY_V3_CAUSE_SUBFIELDS == ("Mechanism", "Mitigation", "Resolution")


class TestQuadrantsPinnedToEnum:
    def test_quadrants_equal_intervention_quadrant_enum(self):
        # The enum (modules.case) owns the quadrant values; the authoring vocab
        # mirrors them. Pin order-insensitively (the enum has no display order).
        assert set(INTERVENTION_QUADRANTS) == {q.value for q in InterventionQuadrant}


class TestConversionPromptCoversVocabulary:
    """The authoring prompt is prose (deliberately not rewritten to interpolate
    constants — prompt edits change LLM behavior). Instead, pin it here: it must
    instruct every term the validator enforces, so prompt and validator can't
    drift apart."""

    PROMPT = conversion_service.CONVERSION_SYSTEM_PROMPT

    def test_prompt_instructs_every_required_subfield(self):
        for sub in REQUIRED_CAUSE_SUBFIELDS:
            assert f"**{sub}:**" in self.PROMPT, f"prompt omits **{sub}:**"

    def test_prompt_mentions_optional_chain_subfield(self):
        for sub in OPTIONAL_CAUSE_SUBFIELDS:
            assert f"**{sub}:**" in self.PROMPT, f"prompt omits **{sub}:**"

    def test_prompt_lists_every_quadrant(self):
        # Enumeration guarantee: every quadrant the validator accepts must be named
        # in the authoring instructions. A bare substring is the right check here —
        # the prompt legitimately names some quadrants only in the rule-7 prose
        # enumeration (`remediation / defensive_fix / mitigation / loop_break`) and
        # others as bold example bullets (`- **remediation** (root): …`).
        for q in INTERVENTION_QUADRANTS:
            assert q in self.PROMPT, f"prompt omits quadrant {q!r}"

    def test_prompt_documents_fallback(self):
        assert FALLBACK_INDICATOR_TOKEN in self.PROMPT
        assert f"Cause {FALLBACK_CAUSE_LETTER}" in self.PROMPT


class TestValidatorUsesVocabulary:
    """The validator's structural lint must still be driven by the vocabulary:
    a canonical v4 runbook passes clean; dropping a quadrant/sub-field/fallback
    trips exactly the matching warning."""

    def _structure_warnings(self, content: str) -> list[str]:
        v = RunbookValidator()
        errors: list[str] = []
        warnings: list[str] = []
        v._validate_structure(content, errors, warnings)
        return warnings

    _CANONICAL = (
        "## Causes\n\n"
        "### Cause A: Pool exhaustion\n"
        "**Statement:** Idle transactions exhaust the connection pool.\n"
        "**Indicators:**\n- root: [Step 1] pool at 100%\n"
        "**Interventions:**\n- **remediation** (root): close idle transactions.\n\n"
        "### Cause Z: Unidentified\n"
        "**Statement:** None of the documented causes match.\n"
        "**Indicators:**\n- [Default]\n"
        "**Interventions:**\n- **mitigation** (D): consult an SME.\n"
    )

    def test_canonical_runbook_no_vocab_warnings(self):
        warns = self._structure_warnings(self._CANONICAL)
        joined = " ".join(warns)
        assert "sub-field" not in joined
        assert "quadrant" not in joined
        assert "fallback Cause" not in joined

    def test_missing_quadrant_tag_warns(self):
        # Interventions present but no quadrant tag anywhere.
        content = (
            "## Causes\n\n"
            "### Cause A: x\n"
            "**Statement:** s\n**Indicators:**\n- root: [Step 1] y\n"
            "**Interventions:**\n- close the thing.\n"
        )
        assert any("quadrant" in w for w in self._structure_warnings(content))

    def test_missing_fallback_blocks(self):
        # A missing [Default] fallback is now an ERROR (Gate 2c), matching the
        # upstream validator and the conversion prompt contract — not a warning.
        content = (
            "## Causes\n\n"
            "### Cause A: x\n"
            "**Statement:** s\n**Indicators:**\n- root: [Step 1] y\n"
            "**Interventions:**\n- **remediation** (root): fix.\n"
        )
        v = RunbookValidator()
        errors: list[str] = []
        warnings: list[str] = []
        v._validate_cause_graph(content, errors, warnings)
        assert any("fallback Cause" in e for e in errors)

    def test_legacy_v3_subfield_warns(self):
        content = (
            "## Causes\n\n"
            "### Cause A: x\n"
            "**Statement:** s\n**Mechanism:** old v3 field\n"
            "**Indicators:**\n- [Default]\n"
            "**Interventions:**\n- **remediation** (root): fix.\n"
        )
        assert any("v3 Cause sub-field" in w for w in self._structure_warnings(content))
