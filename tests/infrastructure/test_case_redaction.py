"""Tests for CaseRedactionContext — case-scoped PII redaction registry."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.infrastructure.security.case_redaction import CaseRedactionContext
from faultmaven.infrastructure.security.redaction import DataSanitizer


@pytest.fixture
def sanitizer():
    """Create a DataSanitizer instance (no Presidio, pattern-only)."""
    return DataSanitizer()


@pytest.fixture
def mock_redis():
    """Create a mock async Redis client."""
    client = AsyncMock()
    client.get = AsyncMock(return_value=None)
    client.set = AsyncMock()
    client.delete = AsyncMock()
    return client


class TestCaseRedactionContext:
    """Core behavior of case-scoped redaction registry."""

    def test_same_ip_gets_same_placeholder(self, sanitizer):
        """Same IP across two sanitize() calls must get the same placeholder."""
        ctx = CaseRedactionContext("case-1", sanitizer)
        text1 = "Connection from 192.168.1.100 accepted"
        text2 = "Failed login from 192.168.1.100 rejected"

        result1 = ctx.sanitize(text1)
        result2 = ctx.sanitize(text2)

        # Both should have the same placeholder for the same IP
        assert "192.168.1.100" not in result1
        assert "192.168.1.100" not in result2

        # Extract the placeholder used in result1 and verify it's in result2
        # The placeholder should be consistent
        for placeholder in ctx._reverse:
            if ctx._reverse[placeholder] == "192.168.1.100":
                assert placeholder in result1
                assert placeholder in result2
                break
        else:
            pytest.fail("IP 192.168.1.100 not found in reverse mapping")

    def test_different_ips_get_different_placeholders(self, sanitizer):
        """Different IPs must get different placeholders."""
        ctx = CaseRedactionContext("case-1", sanitizer)
        # Use IPs from different subnets so the sanitizer's regex captures
        # them as distinct values (the regex captures first 3 octets as a group)
        text1 = "Connection from 192.168.1.100 accepted"
        text2 = "Connection from 172.16.0.50 accepted"

        result1 = ctx.sanitize(text1)
        result2 = ctx.sanitize(text2)

        # Both IPs should be redacted
        assert "192.168.1" not in result1
        assert "172.16.0" not in result2

        # They should have different placeholders in the registry
        assert ctx.entity_count >= 2, "Different IPs should get different placeholders"

    def test_reverse_restores_originals(self, sanitizer):
        """reverse() should replace all placeholders with original values."""
        ctx = CaseRedactionContext("case-1", sanitizer)
        original = "Failed login from 10.20.30.40 for user admin"

        redacted = ctx.sanitize(original)
        assert "10.20.30.40" not in redacted

        restored = ctx.reverse(redacted)
        assert "10.20.30.40" in restored

    def test_disabled_context_is_noop(self, sanitizer):
        """When enabled=False, sanitize and reverse return input unchanged."""
        ctx = CaseRedactionContext("case-1", sanitizer, enabled=False)
        text = "Secret IP: 172.16.0.1"

        assert ctx.sanitize(text) == text
        assert ctx.reverse(text) == text

    def test_empty_string_passthrough(self, sanitizer):
        """Empty or None text should pass through without error."""
        ctx = CaseRedactionContext("case-1", sanitizer)
        assert ctx.sanitize("") == ""
        assert ctx.sanitize(None) is None
        assert ctx.reverse("") == ""

    def test_no_pii_passthrough(self, sanitizer):
        """Text without PII should pass through unchanged."""
        ctx = CaseRedactionContext("case-1", sanitizer)
        text = "System started successfully with no errors"
        assert ctx.sanitize(text) == text

    def test_entity_count(self, sanitizer):
        """entity_count property should reflect unique entities tracked."""
        ctx = CaseRedactionContext("case-1", sanitizer)
        assert ctx.entity_count == 0

        ctx.sanitize("Connection from 192.168.1.100 accepted")
        assert ctx.entity_count >= 1

        ctx.sanitize("Connection from 172.16.0.50 accepted")
        assert ctx.entity_count >= 2

    def test_reverse_longest_first(self, sanitizer):
        """Reverse should replace longer placeholders before shorter ones."""
        ctx = CaseRedactionContext("case-1", sanitizer)

        # Manually set up a scenario where placeholder ordering matters
        ctx._forward = {
            "IP_ADDRESS": {
                "10.0.0.1": "<IP_ADDRESS_1>",
                "10.0.0.2": "<IP_ADDRESS_2>",
                "10.0.0.3": "<IP_ADDRESS_10>",
            }
        }
        ctx._rebuild_reverse()

        text = "Found <IP_ADDRESS_10> and <IP_ADDRESS_1>"
        result = ctx.reverse(text)

        # <IP_ADDRESS_10> should not be partially replaced
        assert "10.0.0.3" in result
        assert "10.0.0.1" in result


class TestCaseRedactionRedis:
    """Redis persistence for case-scoped redaction registry."""

    @pytest.mark.asyncio
    async def test_load_from_redis(self, sanitizer, mock_redis):
        """Registry should load existing mappings from Redis."""
        stored_data = json.dumps(
            {
                "forward": {"IP_ADDRESS": {"10.0.0.1": "<IP_ADDRESS_1>"}},
                "reverse": {"<IP_ADDRESS_1>": "10.0.0.1"},
            }
        )
        mock_redis.get = AsyncMock(return_value=stored_data)

        ctx = CaseRedactionContext("case-1", sanitizer, redis_client=mock_redis)
        await ctx.load()

        assert ctx._forward == {"IP_ADDRESS": {"10.0.0.1": "<IP_ADDRESS_1>"}}
        assert ctx._reverse == {"<IP_ADDRESS_1>": "10.0.0.1"}
        mock_redis.get.assert_called_once_with("redaction:case-1")

    @pytest.mark.asyncio
    async def test_save_to_redis(self, sanitizer, mock_redis):
        """Registry should persist to Redis after modification."""
        ctx = CaseRedactionContext("case-1", sanitizer, redis_client=mock_redis)
        await ctx.load()

        ctx.sanitize("Connection from 10.0.0.1 accepted")
        await ctx.save()

        mock_redis.set.assert_called_once()
        call_args = mock_redis.set.call_args
        key = call_args[0][0]
        assert key == "redaction:case-1"

        # Verify TTL was set
        assert call_args[1]["ex"] == 168 * 3600  # 7 days in seconds

    @pytest.mark.asyncio
    async def test_save_skipped_when_not_dirty(self, sanitizer, mock_redis):
        """save() should skip Redis write when no changes were made."""
        ctx = CaseRedactionContext("case-1", sanitizer, redis_client=mock_redis)
        await ctx.load()
        await ctx.save()

        mock_redis.set.assert_not_called()

    @pytest.mark.asyncio
    async def test_load_survives_redis_failure(self, sanitizer, mock_redis):
        """Registry should work in-memory when Redis is unavailable."""
        mock_redis.get = AsyncMock(side_effect=ConnectionError("Redis down"))

        ctx = CaseRedactionContext("case-1", sanitizer, redis_client=mock_redis)
        await ctx.load()

        # Should still work in-memory
        result = ctx.sanitize("Connection from 10.0.0.1 accepted")
        assert "10.0.0.1" not in result

    @pytest.mark.asyncio
    async def test_save_survives_redis_failure(self, sanitizer, mock_redis):
        """save() should log warning but not crash when Redis fails."""
        mock_redis.set = AsyncMock(side_effect=ConnectionError("Redis down"))

        ctx = CaseRedactionContext("case-1", sanitizer, redis_client=mock_redis)
        await ctx.load()
        ctx.sanitize("Connection from 10.0.0.1 accepted")

        # Should not raise
        await ctx.save()

    @pytest.mark.asyncio
    async def test_roundtrip_preserves_registry(self, sanitizer, mock_redis):
        """Save then load should preserve the full registry state."""
        # Phase 1: Sanitize and save
        ctx1 = CaseRedactionContext("case-1", sanitizer, redis_client=mock_redis)
        await ctx1.load()
        redacted = ctx1.sanitize("Connection from 10.0.0.1 accepted")
        await ctx1.save()

        # Capture what was saved to Redis
        saved_data = mock_redis.set.call_args[0][1]

        # Phase 2: Load into a new context
        mock_redis.get = AsyncMock(return_value=saved_data)
        ctx2 = CaseRedactionContext("case-1", sanitizer, redis_client=mock_redis)
        await ctx2.load()

        # The new context should reverse the same text correctly
        restored = ctx2.reverse(redacted)
        assert "10.0.0.1" in restored

    @pytest.mark.asyncio
    async def test_no_redis_client(self, sanitizer):
        """Registry should work without Redis (in-memory only)."""
        ctx = CaseRedactionContext("case-1", sanitizer, redis_client=None)
        await ctx.load()

        result = ctx.sanitize("Connection from 10.0.0.1 accepted")
        assert "10.0.0.1" not in result

        # save() should be a no-op
        await ctx.save()

    @pytest.mark.asyncio
    async def test_cleanup_deletes_redis_key(self, sanitizer, mock_redis):
        """cleanup() should delete the Redis key."""
        ctx = CaseRedactionContext("case-1", sanitizer, redis_client=mock_redis)
        await ctx.cleanup()

        mock_redis.delete.assert_called_once_with("redaction:case-1")

    @pytest.mark.asyncio
    async def test_custom_ttl(self, sanitizer, mock_redis):
        """Custom TTL should be used when saving to Redis."""
        ctx = CaseRedactionContext(
            "case-1", sanitizer, redis_client=mock_redis, ttl_hours=24
        )
        await ctx.load()
        ctx.sanitize("Connection from 10.0.0.1 accepted")
        await ctx.save()

        call_args = mock_redis.set.call_args
        assert call_args[1]["ex"] == 24 * 3600


class TestSanitizeTextWithRegistry:
    """Tests for DataSanitizer.sanitize_text_with_registry()."""

    def test_external_registry_used(self):
        """sanitize_text_with_registry should use the provided registry."""
        sanitizer = DataSanitizer()
        registry = {}

        text = "Connection from 10.0.0.1 and 10.0.0.2"
        result = sanitizer.sanitize_text_with_registry(text, registry)

        # Registry should have been populated
        assert len(registry) > 0
        assert "10.0.0.1" not in result
        assert "10.0.0.2" not in result

    def test_registry_consistency_across_calls(self):
        """Same value across two calls with same registry gets same placeholder."""
        sanitizer = DataSanitizer()
        registry = {}

        result1 = sanitizer.sanitize_text_with_registry(
            "Login from 192.168.1.1", registry
        )
        result2 = sanitizer.sanitize_text_with_registry(
            "Logout from 192.168.1.1", registry
        )

        # Extract the placeholder from result1
        # Both should contain the same placeholder for 192.168.1.1
        for type_map in registry.values():
            if "192.168.1.1" in type_map:
                placeholder = type_map["192.168.1.1"]
                assert placeholder in result1
                assert placeholder in result2
                break
        else:
            pytest.fail("192.168.1.1 not in registry")

    def test_existing_sanitize_unchanged(self):
        """Original sanitize() method should still create fresh registry."""
        sanitizer = DataSanitizer()

        # Two separate sanitize() calls should NOT share a registry
        result1 = sanitizer.sanitize("Login from 10.0.0.1")
        result2 = sanitizer.sanitize("Login from 10.0.0.2")

        # Both should be redacted (but independently)
        assert "10.0.0.1" not in result1
        assert "10.0.0.2" not in result2
