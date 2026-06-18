"""Structural invariants for the LLM provider registry schema.

The cloud admin dashboard renders a per-provider model drop-down whose options
come from ``available_models`` and whose pre-selected value is ``default_model``
(see ``admin_config.get_llm_config`` -> ``registry.get_available_models_for``).

If ``default_model`` is not one of ``available_models``, the dashboard shows a
selected model that is absent from its own option list — and, worse, an
``available_models`` entry that the provider has retired will let an admin pick a
model that 404s at request time (the exact failure mode of the original gemini
outage). These tests pin both properties at the schema level so a future edit
cannot silently reintroduce either.
"""

import pytest

from faultmaven.infrastructure.llm.providers.registry import PROVIDER_SCHEMA

# Providers whose catalog is fetched dynamically rather than enumerated here;
# their ``available_models`` is intentionally empty and the invariant is N/A.
_DYNAMIC_CATALOG_PROVIDERS = {"local", "openrouter"}


@pytest.mark.unit
@pytest.mark.parametrize(
    "provider",
    [p for p in PROVIDER_SCHEMA if p not in _DYNAMIC_CATALOG_PROVIDERS],
)
def test_default_model_is_offered(provider: str) -> None:
    """default_model must appear in available_models (the drop-down's options)."""
    schema = PROVIDER_SCHEMA[provider]
    available = schema["available_models"]

    assert available, (
        f"{provider!r} enumerates a static catalog but available_models is empty; "
        "either populate it or add the provider to _DYNAMIC_CATALOG_PROVIDERS."
    )
    assert schema["default_model"] in available, (
        f"{provider!r} default_model {schema['default_model']!r} is not in "
        f"available_models {available!r} — the dashboard would pre-select a model "
        "that is not one of its own drop-down options."
    )


@pytest.mark.unit
def test_dynamic_catalog_providers_have_empty_available_models() -> None:
    """Dynamic-catalog providers must NOT enumerate a stale static list.

    Keeps the _DYNAMIC_CATALOG_PROVIDERS exemption honest: if someone adds a
    hardcoded list to one of these, the invariant test above stops covering it,
    so fail loudly instead.
    """
    for provider in _DYNAMIC_CATALOG_PROVIDERS:
        if provider not in PROVIDER_SCHEMA:
            continue
        assert PROVIDER_SCHEMA[provider]["available_models"] == [], (
            f"{provider!r} is marked dynamic-catalog but enumerates "
            f"available_models {PROVIDER_SCHEMA[provider]['available_models']!r}; "
            "remove the static list or drop it from _DYNAMIC_CATALOG_PROVIDERS."
        )
