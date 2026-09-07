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


# ---------------------------------------------------------------------------
# Configuration-shaped keys (#1358)
# ---------------------------------------------------------------------------

# Keys whose VALUE is an environment variable name. Nothing resolves
# configuration *through* these keys — ``_create_provider_config`` reads each
# provider's own settings field directly — so a wrong value cannot surface as a
# bug. It just misinforms the next reader, which is what ``base_url_var`` did
# for four years: unread everywhere, and for ``local`` it advertised
# ``LOCAL_LLM_BASE_URL`` while the code read ``LOCAL_LLM_URL``.
_ENV_VAR_KEY_SUFFIX = "_var"


def _settings_snapshot() -> dict:
    """Every LLMSettings field value, with SecretStr unwrapped for comparison."""
    from faultmaven.config.settings import LLMSettings

    settings = LLMSettings()
    snapshot = {}
    for field in type(settings).model_fields:
        value = getattr(settings, field, None)
        if hasattr(value, "get_secret_value"):
            value = value.get_secret_value()
        snapshot[field] = value
    return snapshot


@pytest.mark.unit
def test_every_env_var_key_names_a_variable_settings_consumes(monkeypatch) -> None:
    """A ``*_var`` value must name an env var the settings layer actually reads.

    This is the invariant #1358 states: a configuration-shaped key names the
    environment variable actually consumed for that provider, or it does not
    exist. It is asserted BEHAVIOURALLY rather than by grepping settings.py,
    because a field reaches its env var two different ways — an explicit
    ``validation_alias`` (``local_url`` <- ``LOCAL_LLM_URL``) or the pydantic
    default of the field's own name (``openai_model`` <- ``OPENAI_MODEL``) —
    and only one of those is greppable.

    Method: snapshot every LLMSettings field, set the advertised variable to a
    sentinel, rebuild, and require that some field moved. Comparing against a
    baseline rather than a fixed expectation keeps the test honest when a
    developer's .env already sets the variable: os.environ outranks .env, so
    the sentinel still wins, and any value the .env contributed is present in
    BOTH snapshots and cancels out.
    """
    env_var_keys = sorted(
        {
            key
            for schema in PROVIDER_SCHEMA.values()
            for key in schema
            if key.endswith(_ENV_VAR_KEY_SUFFIX)
        }
    )
    assert env_var_keys, "PROVIDER_SCHEMA advertises no env vars — test is inert"

    unread = []
    for provider, schema in sorted(PROVIDER_SCHEMA.items()):
        for key in env_var_keys:
            env_var = schema.get(key)
            if env_var is None:
                continue  # e.g. local has api_key_var=None — no key needed
            baseline = _settings_snapshot()
            monkeypatch.setenv(env_var, "fm-sentinel-value")
            try:
                moved = [
                    field
                    for field, value in _settings_snapshot().items()
                    if value != baseline[field]
                ]
            finally:
                monkeypatch.delenv(env_var, raising=False)
            if not moved:
                unread.append(f"{provider}.{key} = {env_var!r}")

    assert not unread, (
        "PROVIDER_SCHEMA advertises environment variables that no LLMSettings "
        f"field consumes: {unread}. Setting one changes nothing, so the key is "
        "documentation that lies. Fix the value to the variable the code "
        "actually reads, or delete the key (what #1358 did for base_url_var)."
    )


@pytest.mark.unit
def test_base_url_var_is_not_reintroduced() -> None:
    """Regression pin for #1358.

    ``base_url_var`` had no reader anywhere in the repo and, for ``local``,
    disagreed with the code outright. ``default_base_url`` — which IS read, by
    ``_create_provider_config`` — stays. Re-adding a base-URL env var name
    here means re-adding a key nothing consults, so it must come with a reader
    that makes it authoritative.
    """
    offenders = [p for p, schema in PROVIDER_SCHEMA.items() if "base_url_var" in schema]
    assert not offenders, (
        f"{offenders} re-declare 'base_url_var'. It has no reader — the "
        "construction path reads llm_settings.<provider>_base_url directly. "
        "Give it a reader or leave it deleted (#1358)."
    )


@pytest.mark.unit
def test_groq_picker_offers_a_strict_model() -> None:
    """Groq's picker must offer at least one STRICT structured-output model.

    The investigation engine drives state from schema-constrained responses.
    A BEST_EFFORT model only gets the schema asked for in-prompt, so it can
    omit required fields, the engine drops the state_updates, and the
    investigation degrades. Groq's ONLY STRICT models are openai/gpt-oss-20b
    and openai/gpt-oss-120b (GroqProvider.get_structured_output_capability);
    until #1359 the picker listed the two Llama models and nothing else, so
    every Groq configuration an operator could pick from the UI degraded
    primary CHAT, and the one usable pair could only be reached by setting
    GROQ_MODEL by hand.

    Asserted against the provider's own capability method rather than a
    hardcoded model list, so it tracks the provider if the STRICT set moves.
    """
    from faultmaven.infrastructure.llm.providers.base import ProviderConfig
    from faultmaven.infrastructure.llm.providers.groq_provider import GroqProvider
    from faultmaven.infrastructure.llm.structured_output_capability import (
        StructuredOutputCapability,
    )

    offered = PROVIDER_SCHEMA["groq"]["available_models"]
    provider = GroqProvider(
        ProviderConfig(
            name="groq",
            api_key="test-key",
            base_url=PROVIDER_SCHEMA["groq"]["default_base_url"],
            models=list(offered),
            default_model=PROVIDER_SCHEMA["groq"]["default_model"],
        )
    )

    strict = [
        model
        for model in offered
        if provider.get_structured_output_capability(model)
        is StructuredOutputCapability.STRICT
    ]
    assert strict, (
        f"Groq's picker offers {offered!r}, none of which enforce a schema "
        "natively. Every option degrades primary CHAT, and the models that "
        "would not are unreachable from the UI (#1359)."
    )
