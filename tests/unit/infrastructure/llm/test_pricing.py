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

    @pytest.mark.parametrize(
        "model", ["gpt-5.6-luna", "gpt-5.4-mini", "gpt-4.1-mini", "gpt-4o"]
    )
    def test_openai_family_cache_read_cheaper_than_input(self, model):
        """The property, asserted per model rather than as arithmetic on one
        hardcoded rate — this test previously encoded gpt-5.4-mini's input as
        0.15, which was gpt-4o-mini's number, and kept passing while the rate
        it asserted was wrong."""
        rates = lookup_rates("openai", model)
        assert rates is not None
        assert 0 < rates.cache_read < rates.input

    def test_cost_sums_disjoint_buckets(self):
        """The arithmetic the parametrized test above no longer pins: buckets
        are disjoint and simply sum."""
        cost, priced = estimate_cost_usd(
            "openai",
            "gpt-5.6-luna",
            input_tokens=1_000_000,
            cache_read_tokens=1_000_000,
        )
        assert priced is True
        assert cost == pytest.approx(0.20 + 0.02)

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

    def test_gemini_3_7_flash_priced_at_introductory_rate(self):
        # Google's introductory rate ($0.75 in / $3.75 out / $0.075 cache read
        # per 1M) runs through 2026-12-31; the table entry documents the
        # 2027-01-01 standard rate it must be bumped to.
        cost, priced = estimate_cost_usd(
            "gemini",
            "gemini-3.7-flash",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            cache_read_tokens=1_000_000,
        )
        assert priced is True
        assert cost == pytest.approx(0.75 + 3.75 + 0.075)

    def test_gemini_3_5_flash_lite_specific_rate_wins_over_3_5_flash(self):
        # "gemini-3.5-flash" is a substring of "gemini-3.5-flash-lite" — the
        # substring scan is longest-match-wins, so the specific -lite key
        # beats the flash key it contains for dated variants too, or the
        # classifier/synthesis model prices at the 5x-higher flash rate.
        rates = lookup_rates("gemini", "gemini-3.5-flash-lite")
        assert rates is not None
        assert rates.input == 0.30 and rates.output == 2.50
        dated = lookup_rates("gemini", "gemini-3.5-flash-lite-001")
        assert dated is not None and dated.input == 0.30

    def test_longest_match_wins_for_operator_override_variants(self, monkeypatch):
        # LLM_PRICING_OVERRIDES merges via dict.update, appending operator
        # keys AFTER every built-in — no ordering discipline can make a more
        # specific operator key precede a built-in in the scan. Longest-match
        # is what makes the override's dated variant price at the override
        # rate instead of substring-matching the built-in gemini-3.7-flash.
        monkeypatch.setenv(
            pricing._OVERRIDES_ENV,
            '{"gemini": {"gemini-3.7-flash-lite": {"input": 0.1, "output": 0.4}}}',
        )
        reload_rates()
        try:
            dated = lookup_rates("gemini", "gemini-3.7-flash-lite-preview-01")
            assert dated is not None
            assert dated.input == 0.1 and dated.output == 0.4
        finally:
            monkeypatch.delenv(pricing._OVERRIDES_ENV)
            reload_rates()

    def test_gemini_3_7_flash_standard_rate_applies_from_2027(self):
        # The introductory→standard switch is mechanized: _build_rates
        # resolves _SCHEDULED_RATE_CHANGES against today's date, so a process
        # started in January 2027 prices at $1.50/$7.50 with no code edit —
        # and stays priced=True either way (the unpriced counter can't catch
        # a stale-but-present entry, which is why this is date-driven).
        from datetime import date

        before = pricing._build_rates(date(2026, 12, 31))
        assert before["gemini"]["gemini-3.7-flash"].input == 0.75
        after = pricing._build_rates(date(2027, 1, 1))
        entry = after["gemini"]["gemini-3.7-flash"]
        assert entry.input == 1.50 and entry.output == 7.50
        assert entry.cache_read == 0.15

    def test_operator_override_beats_scheduled_rate_change(self, monkeypatch):
        # An operator pin wins before AND after the scheduled date — the
        # schedule applies over defaults, overrides apply over both.
        from datetime import date

        monkeypatch.setenv(
            pricing._OVERRIDES_ENV,
            '{"gemini": {"gemini-3.7-flash": {"input": 9.9, "output": 9.9}}}',
        )
        try:
            after = pricing._build_rates(date(2027, 6, 1))
            assert after["gemini"]["gemini-3.7-flash"].input == 9.9
        finally:
            monkeypatch.delenv(pricing._OVERRIDES_ENV)
        reload_rates()

    def test_gemini_3_5_flash_official_rates(self):
        # Corrected 2026-08-26 from a stale 0.15/0.60 to Google's published
        # $1.50 in / $9.00 out / $0.15 cache read per 1M.
        rates = lookup_rates("gemini", "gemini-3.5-flash")
        assert rates is not None
        assert rates.input == 1.50 and rates.output == 9.0

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

    def test_default_openai_model_gpt_5_6_luna_is_priced(self):
        """gpt-5.6-luna is the shipped OpenAI default; an unpriced default is
        the worst case for the module's guarantee (every call on it would
        report $0 with priced=True nowhere to be seen). Short-context rates
        per developers.openai.com/api/docs/pricing."""
        rates = lookup_rates("openai", "gpt-5.6-luna")
        assert rates is not None
        assert rates.input == 0.20 and rates.output == 1.20
        assert rates.cache_read == 0.02 and rates.cache_write == 0.25
        cost, priced = estimate_cost_usd(
            "openai",
            "gpt-5.6-luna",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            cache_read_tokens=1_000_000,
            cache_write_tokens=1_000_000,
        )
        assert priced is True
        assert cost == pytest.approx(0.20 + 1.20 + 0.02 + 0.25)

    def test_gpt_5_4_mini_uses_its_own_rate_not_gpt_4o_minis(self):
        """Regression: the gpt-5.4-mini row carried gpt-4o-mini's numbers
        (0.15/0.60), under-reporting it 5x on input and 7.5x on output.
        The two models must not share a rate."""
        five = lookup_rates("openai", "gpt-5.4-mini")
        four = lookup_rates("openai", "gpt-4o-mini")
        assert five is not None and four is not None
        assert five.input == 0.75 and five.output == 4.50
        assert (five.input, five.output) != (four.input, four.output)

    def test_supported_openai_model_gpt_5_4_mini_is_priced(self):
        # gpt-5.4-mini remains a supported (non-default) OpenAI model and must
        # stay priced so a deployment that pins it doesn't report ~$0.
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
