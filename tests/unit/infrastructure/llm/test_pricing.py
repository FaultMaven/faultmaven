"""Unit tests for LLM cost estimation (faultmaven.infrastructure.llm.pricing).

Cost is the unit the token-spend incident was noticed in, so the price math,
the disjoint-bucket contract, and the honest unknown-model handling are all
load-bearing. This module is dependency-free, so these tests run anywhere.
"""

import pytest

from faultmaven.infrastructure.llm import pricing
from faultmaven.infrastructure.llm.pricing import (
    estimate_cost_usd,
    lookup_rates,
    reload_rates,
)


@pytest.mark.unit
class TestEstimateCostUsd:
    def test_known_model_prices_each_bucket(self):
        # anthropic claude-sonnet-4-6: input 3 / output 15 / read 0.30 / write 3.75
        # per 1M. Feed 1M of each bucket so the cost equals the summed rates.
        cost, priced = estimate_cost_usd(
            "anthropic",
            "claude-sonnet-4-6",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            cache_read_tokens=1_000_000,
            cache_write_tokens=1_000_000,
        )
        assert priced is True
        assert cost == pytest.approx(3.0 + 15.0 + 0.30 + 3.75)

    def test_input_only(self):
        cost, priced = estimate_cost_usd(
            "anthropic", "claude-sonnet-4-6", input_tokens=500_000
        )
        assert priced is True
        assert cost == pytest.approx(1.5)  # 0.5M * $3/1M

    def test_unknown_model_is_unpriced_not_free(self):
        # An unknown model must be reported unpriced so cost under-counting is
        # visible — never silently treated as $0.
        cost, priced = estimate_cost_usd(
            "anthropic", "claude-from-the-future", input_tokens=1_000_000
        )
        assert cost == 0.0
        assert priced is False

    def test_unknown_provider_is_unpriced(self):
        cost, priced = estimate_cost_usd("acme-llm", "whatever", input_tokens=100)
        assert (cost, priced) == (0.0, False)

    def test_zero_cost_provider_is_priced_free(self):
        # Self-hosted providers are KNOWN to be free: priced=True, cost=0. This
        # keeps them out of the unpriced-calls counter.
        for provider in ("local", "huggingface"):
            cost, priced = estimate_cost_usd(
                provider, "llama3.2", input_tokens=1_000_000
            )
            assert cost == 0.0
            assert priced is True

    def test_substring_match_handles_prefixed_model_id(self):
        # OpenRouter / dated-snapshot ids still resolve via substring match.
        cost, priced = estimate_cost_usd(
            "anthropic", "anthropic/claude-sonnet-4-6-20260101", input_tokens=1_000_000
        )
        assert priced is True
        assert cost == pytest.approx(3.0)

    def test_low_confidence_provider_suffix_is_normalized(self):
        # The registry renames fallbacks to e.g. "anthropic (low-confidence)";
        # pricing must still resolve them.
        rates = lookup_rates("anthropic (low-confidence)", "claude-sonnet-4-6")
        assert rates is not None
        assert rates.input == 3.0

    def test_openai_family_cache_read_cheaper_than_input(self):
        cost, priced = estimate_cost_usd(
            "openai",
            "gpt-5.4-mini",
            input_tokens=1_000_000,
            cache_read_tokens=1_000_000,
        )
        assert priced is True
        # 0.15 (input) + 0.075 (cache_read)
        assert cost == pytest.approx(0.225)

    def test_gemini_3_1_flash_lite_is_priced(self):
        cost, priced = estimate_cost_usd(
            "gemini",
            "gemini-3.1-flash-lite",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            cache_read_tokens=1_000_000,
        )
        assert priced is True
        # 0.25 (input) + 1.50 (output) + 0.025 (cache_read)
        assert cost == pytest.approx(1.775)

    def test_deepseek_v4_flash_full_path_uses_specific_rate_not_generic(self):
        # The served id is a full path; the specific deepseek-v4-flash rate must
        # win over the generic "deepseek" ($0.90) substring fallback.
        rates = lookup_rates("fireworks", "accounts/fireworks/models/deepseek-v4-flash")
        assert rates is not None
        assert rates.input == 0.14 and rates.output == 0.28
        cost, priced = estimate_cost_usd(
            "fireworks",
            "accounts/fireworks/models/deepseek-v4-flash",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            cache_read_tokens=1_000_000,
        )
        assert priced is True
        # 0.14 + 0.28 + 0.03 — NOT the generic 0.90/0.90
        assert cost == pytest.approx(0.45)

    def test_generic_deepseek_still_falls_back(self):
        # An unknown deepseek variant still resolves to the generic rate.
        rates = lookup_rates("fireworks", "accounts/fireworks/models/deepseek-v9")
        assert rates is not None and rates.input == 0.90

    def test_default_anthropic_model_claude_sonnet_4_5_is_priced(self):
        # claude-sonnet-4-5 is the default Anthropic model (CHAT_PROVIDER=anthropic);
        # it must be priced so real runs don't report 100% unpriced calls. Feed 1M
        # of each bucket so cost equals the summed Sonnet-tier rates, and confirm
        # Anthropic cache WRITES (nonzero on Anthropic explicit caching) are billed.
        cost, priced = estimate_cost_usd(
            "anthropic",
            "claude-sonnet-4-5",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            cache_read_tokens=1_000_000,
            cache_write_tokens=1_000_000,
        )
        assert priced is True
        assert cost == pytest.approx(3.0 + 15.0 + 0.30 + 3.75)

    def test_default_openai_model_gpt_5_4_mini_is_priced(self):
        # gpt-5.4-mini is the default OpenAI model; it must be priced so real
        # runs don't report 100% unpriced calls (cost silently ~$0).
        _, priced = estimate_cost_usd(
            "openai",
            "gpt-5.4-mini",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
        )
        assert priced is True

    def test_supported_openai_model_gpt_4_1_mini_is_priced(self):
        # gpt-4.1-mini remains a supported (non-default) OpenAI model and must
        # stay priced so runs that select it don't report ~$0 cost.
        cost, priced = estimate_cost_usd(
            "openai",
            "gpt-4.1-mini",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            cache_read_tokens=1_000_000,
        )
        assert priced is True
        # 0.40 (input) + 1.60 (output) + 0.10 (cache_read)
        assert cost == pytest.approx(2.10)

    def test_dated_gpt_4_1_mini_snapshot_matches(self):
        # Dated snapshot ids (e.g. gpt-4.1-mini-2025-04-14) must substring-match.
        rates = lookup_rates("openai", "gpt-4.1-mini-2025-04-14")
        assert rates is not None
        assert rates.input == 0.40


@pytest.mark.unit
class TestPricingOverrides:
    def test_env_override_merges_over_defaults(self, monkeypatch):
        monkeypatch.setenv(
            "LLM_PRICING_OVERRIDES",
            '{"acme": {"turbo": {"input": 1.0, "output": 2.0}}}',
        )
        reload_rates()
        try:
            cost, priced = estimate_cost_usd(
                "acme", "turbo", input_tokens=1_000_000, output_tokens=1_000_000
            )
            assert priced is True
            assert cost == pytest.approx(3.0)
        finally:
            monkeypatch.delenv("LLM_PRICING_OVERRIDES", raising=False)
            reload_rates()

    def test_invalid_override_json_is_ignored(self, monkeypatch):
        monkeypatch.setenv("LLM_PRICING_OVERRIDES", "not-json{")
        reload_rates()
        try:
            # Defaults must still work; the bad env var is ignored, not fatal.
            _, priced = estimate_cost_usd(
                "anthropic", "claude-sonnet-4-6", input_tokens=1
            )
            assert priced is True
        finally:
            monkeypatch.delenv("LLM_PRICING_OVERRIDES", raising=False)
            reload_rates()

    def test_default_rates_are_pure_literals(self):
        # Guard against a rate accidentally becoming negative or non-numeric.
        for provider, table in pricing.DEFAULT_RATES.items():
            for model, rates in table.items():
                for bucket in ("input", "output", "cache_read", "cache_write"):
                    value = getattr(rates, bucket)
                    assert isinstance(value, int | float), (provider, model, bucket)
                    assert value >= 0.0, (provider, model, bucket)
