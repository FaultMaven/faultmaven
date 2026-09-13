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


def test_the_default_is_offered_by_the_picker():
    """An operator must be able to select what the deployment already runs.

    The two are separate lists and drifted apart: the default was a model the
    picker offered and the API refused.
    """
    assert _default() in PROVIDER_SCHEMA["groq"]["available_models"]


def test_the_registry_and_settings_agree_on_the_default():
    assert PROVIDER_SCHEMA["groq"]["default_model"] == _default()


def test_every_offered_model_is_priced():
    """A model the picker offers but pricing does not know bills as zero.

    `lookup_rates` matches on bare substrings, so this also pins the
    `openai/`-prefix convention the Groq ids use.
    """
    for model in PROVIDER_SCHEMA["groq"]["available_models"]:
        assert lookup_rates("groq", model) is not None, model


def test_the_default_can_carry_the_investigation_role():
    """STRICT, because the engine drives state from schema-constrained output.

    A BEST_EFFORT default silently degrades every investigation on that
    provider — the reason the picker list was widened to include gpt-oss in the
    first place.
    """
    # A minimal config: `get_effective_model` honours a requested model only
    # when it is in `config.models`, which the registry populates from the
    # picker list. Driving the real method rather than asserting on the
    # provider's internal set keeps this a test of behaviour.
    from types import SimpleNamespace

    provider = GroqProvider.__new__(GroqProvider)
    provider.config = SimpleNamespace(
        models=list(PROVIDER_SCHEMA["groq"]["available_models"]),
        default_model=_default(),
    )
    capability = provider.get_structured_output_capability(_default())

    assert capability is StructuredOutputCapability.STRICT


def test_no_shipped_source_still_pins_the_decommissioned_model():
    """All four copies moved together, which is the part that failed before.

    settings, the registry default, the picker list, `.env.example` and
    CLAUDE.md each carried the id independently; three of them are not reached
    by any import, so only a text sweep catches a straggler.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[4]
    for relative in (
        "faultmaven/config/settings.py",
        "faultmaven/infrastructure/llm/providers/registry.py",
        ".env.example",
        "CLAUDE.md",
    ):
        text = (root / relative).read_text()
        # The id may appear in prose explaining WHY it was dropped; what must
        # not survive is an assignment or an offered value.
        for line in text.splitlines():
            stripped = line.strip()
            if "llama-3.3-70b-versatile" not in stripped:
                continue
            assert stripped.startswith("#") or stripped.startswith(
                "|"
            ), f"{relative} still pins the decommissioned model: {stripped}"
