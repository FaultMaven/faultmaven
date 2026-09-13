"""The shipped Groq model must be one Groq actually serves (#1381).

`llama-3.3-70b-versatile` shipped as the default in `settings.py`, the registry,
`.env.example` and CLAUDE.md long after Groq decommissioned it — every call
404'd `model_not_found`, so `CHAT_PROVIDER=groq` could not answer a turn and the
documented cost optimisation (`CLASSIFIER_PROVIDER=groq`) 404'd on every
classification.

No unit test can know Groq's live catalogue. What IS testable is the internal
coherence that was broken: the default has to be a model this codebase itself
says is fit for the role, offered by the picker, and priced.
"""

from __future__ import annotations

import pytest

from faultmaven.config.settings import LLMSettings
from faultmaven.infrastructure.llm.pricing import lookup_rates
from faultmaven.infrastructure.llm.providers.groq_provider import GroqProvider
from faultmaven.infrastructure.llm.providers.registry import PROVIDER_SCHEMA
from faultmaven.infrastructure.llm.structured_output_capability import (
    StructuredOutputCapability,
)

pytestmark = [pytest.mark.unit, pytest.mark.llm]


def _default() -> str:
    return LLMSettings.model_fields["groq_model"].default


def test_the_default_itself_is_strict_not_merely_one_of_the_options():
    """The gap the neighbouring suite leaves.

    `test_groq_picker_offers_a_strict_model` asserts that AT LEAST ONE offered
    model enforces a schema natively — it takes the list, filters for STRICT and
    asserts the filter is non-empty. A BEST_EFFORT **default** sitting beside a
    STRICT option passes it, and the default is what a deployment actually runs
    unless an operator intervenes. The engine drives state from
    schema-constrained responses, so that is the configuration that matters.
    """
    from faultmaven.infrastructure.llm.providers.base import ProviderConfig

    # A REAL ProviderConfig, as the neighbouring suite builds one. A namespace
    # carrying only `models`/`default_model` works until `get_effective_model`
    # takes its warning branch, which touches `self.logger` and
    # `self.provider_name` — then this raises AttributeError instead of
    # reporting the capability it names.
    provider = GroqProvider(
        ProviderConfig(
            name="groq",
            api_key="test-key",
            base_url=PROVIDER_SCHEMA["groq"]["default_base_url"],
            models=list(PROVIDER_SCHEMA["groq"]["available_models"]),
            default_model=_default(),
        )
    )

    assert (
        provider.get_structured_output_capability(_default())
        is StructuredOutputCapability.STRICT
    )


def test_every_shipped_source_names_the_current_default():
    """All the places the id is written by hand agree with `settings.py`.

    Stated POSITIVELY, and that is the point. The first version asserted the
    decommissioned id was ABSENT and exempted lines starting with `#` or `|` so
    prose could mention it — which is exactly how `.env.example` and CLAUDE.md
    write the value, so the guard passed with `llama-3.3-70b-versatile`
    restored in both. Mutation-verified at the time; the exemption swallowed
    the only files the sweep existed for.

    A positive claim has no exemption to slip through: whatever else a file
    says, it must name the model actually shipped.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[4]
    default = _default()

    env_example = (root / ".env.example").read_text()
    assert f"GROQ_MODEL={default}" in env_example

    claude_md = (root / "CLAUDE.md").read_text()
    groq_rows = [
        line
        for line in claude_md.splitlines()
        if line.startswith("|") and "`GROQ_API_KEY`" in line
    ]
    assert groq_rows, "CLAUDE.md no longer documents Groq in its provider table"
    for row in groq_rows:
        assert default in row, f"provider table names a different model: {row}"


def test_the_getting_started_guide_lists_only_servable_models():
    """Onboarding is a way to re-introduce the defect, not just document it.

    `_create_provider_config` folds a configured model into `config.models`, so
    `get_effective_model` honours whatever a user sets — a guide listing a
    decommissioned id sends them straight to `model_not_found` (#1381).
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[4]
    guide = (root / "docs/getting-started/user-guide.md").read_text()

    groq_lines = [ln for ln in guide.splitlines() if "**Groq**" in ln]
    assert groq_lines, "the guide no longer documents Groq models"
    for line in groq_lines:
        assert "llama-3.3-70b-versatile" not in line
        assert "llama-3.1-8b-instant" not in line
        assert _default() in line
