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


@pytest.mark.unit
@pytest.mark.parametrize(
    "provider",
    [p for p in PROVIDER_SCHEMA if p not in _DYNAMIC_CATALOG_PROVIDERS],
)
def test_every_offered_model_is_priced(provider: str) -> None:
    """Every drop-down option must have a rate in the pricing table.

    An admin picking a model from the UI must not silently produce $0 cost
    reporting: ``estimate_cost_usd`` returns ``priced=False`` for an unknown
    model, which the metering layer counts on a separate unpriced counter
    rather than surfacing as spend. That honest-undercount design is the right
    behaviour for a model an operator pinned by hand — it is the wrong
    behaviour for one the product itself offered.

    Audited 2026-08-26: nine offered models across five providers were unpriced
    (fireworks llama-v3p1-8b/70b + qwen2p5-coder, openai gpt-4-turbo + o3-mini,
    anthropic claude-3-5-sonnet-20241022, groq Llama-4-Scout, cohere
    command-light). Adding a model to a picker now means pricing it, or the
    build fails here.
    """
    from faultmaven.infrastructure.llm.pricing import lookup_rates

    # NOTE the coverage limit: lookup_rates short-circuits _ZERO_COST_PROVIDERS
    # ("local", "huggingface") to a zero rate before consulting the table, so
    # for those two this assertion is trivially true and proves nothing about
    # their model lists. That is a property of the pricing module's
    # self-hosted-is-free assumption, not of this test; it is called out here
    # so the green tick is not read as coverage it does not have.
    unpriced = [
        model
        for model in PROVIDER_SCHEMA[provider]["available_models"]
        if lookup_rates(provider, model) is None
    ]
    assert not unpriced, (
        f"{provider!r} offers {unpriced!r} in the dashboard picker, but "
        "infrastructure/llm/pricing.py has no rate for them — calls would "
        "report $0 spend. Add a rate row, or drop the model from "
        "available_models."
    )


@pytest.mark.unit
@pytest.mark.parametrize("provider", list(PROVIDER_SCHEMA))
def test_default_model_is_priced(provider: str) -> None:
    """The default is what runs when nobody chooses — it must be priced.

    Covers the dynamic-catalog providers too (whose default is not in any
    list), because an unpriced DEFAULT means a deployment that changed nothing
    reports zero spend.
    """
    from faultmaven.infrastructure.llm.pricing import lookup_rates

    default = PROVIDER_SCHEMA[provider]["default_model"]
    assert lookup_rates(provider, default) is not None, (
        f"{provider!r} default_model {default!r} has no rate in "
        "infrastructure/llm/pricing.py — a deployment that changed nothing "
        "would report $0 spend."
    )
