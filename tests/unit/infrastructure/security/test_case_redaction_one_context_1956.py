"""#1956: the case redaction registry's shared loader, helpers and save rule.

A ``CaseRedactionContext`` set ``_dirty`` on EVERY sanitize pass, so a context
that had redacted nothing new still wrote its whole registry back on
``save()``. Two contexts loaded on one case key then clobbered each other: the
later save replaced the other's new entries with its own stale snapshot, and a
placeholder the user was about to read could no longer be reversed. ``_dirty``
is now set only when the registry grew; and the turn loads ONE context
(``load_case_redaction``) instead of one per caller.
"""

from __future__ import annotations

import json

import pytest

from faultmaven.config.settings import get_settings
from faultmaven.infrastructure.security.case_redaction import (
    REDACTION_PLACEHOLDER_RE,
    CaseRedactionContext,
    case_redacted,
    case_redacted_messages,
    load_case_redaction,
    should_redact,
)

pytestmark = pytest.mark.unit


class _Sanitizer:
    """Registry-backed detection of two literal values."""

    VALUES = {
        "alice@example.com": ("EMAIL_ADDRESS", "<EMAIL_ADDRESS_aaaaaaaaaaaaaaaa>"),
        "10.0.0.7": ("IP_ADDRESS", "<IP_ADDRESS_bbbbbbbbbbbbbbbb>"),
    }

    def sanitize_text_with_registry(self, text, registry):
        for value, (kind, placeholder) in self.VALUES.items():
            if value in text:
                registry.setdefault(kind, {})[value] = placeholder
                text = text.replace(value, placeholder)
        return text


class _Redis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.sets = 0

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.sets += 1
        self.store[key] = value


def _ctx(redis) -> CaseRedactionContext:
    return CaseRedactionContext(
        case_id="case_1956", sanitizer=_Sanitizer(), redis_client=redis, enabled=True
    )


def _stored(redis) -> dict:
    return json.loads(redis.store["redaction:case_1956"])["reverse"]


class TestTheSaveWritesOnlyWhatGrew:
    async def test_two_contexts_on_one_case_do_not_clobber(self):
        """The regression: B loaded before A saved, redacted only a value it
        already knew, and its save wrote back a snapshot without A's entry."""
        redis = _Redis()
        seed = _ctx(redis)
        await seed.load()
        await seed.asanitize("from 10.0.0.7")
        await seed.save()

        a, b = _ctx(redis), _ctx(redis)
        await a.load()
        await b.load()
        await a.asanitize("user alice@example.com")
        await a.save()
        await b.asanitize("again from 10.0.0.7")  # nothing new
        await b.save()

        assert "<EMAIL_ADDRESS_aaaaaaaaaaaaaaaa>" in _stored(redis)
        assert "<IP_ADDRESS_bbbbbbbbbbbbbbbb>" in _stored(redis)

    async def test_a_pass_that_grew_nothing_does_not_write(self):
        redis = _Redis()
        ctx = _ctx(redis)
        await ctx.load()
        await ctx.asanitize("no sensitive value here")
        ctx.sanitize("still none")
        await ctx.save()
        assert redis.sets == 0

    async def test_a_pass_that_grew_the_registry_writes_once(self):
        redis = _Redis()
        ctx = _ctx(redis)
        await ctx.load()
        await ctx.asanitize("user alice@example.com")
        await ctx.save()
        await ctx.save()
        assert redis.sets == 1
        assert ctx.reverse("<EMAIL_ADDRESS_aaaaaaaaaaaaaaaa>") == "alice@example.com"

    async def test_the_sync_pass_follows_the_same_rule(self):
        redis = _Redis()
        ctx = _ctx(redis)
        await ctx.load()
        ctx.sanitize("user alice@example.com")
        await ctx.save()
        assert redis.sets == 1


class TestTheLoader:
    async def test_it_builds_enabled_by_should_redact_and_loads(self):
        redis = _Redis()
        seed = _ctx(redis)
        await seed.load()
        await seed.asanitize("user alice@example.com")
        await seed.save()
        sanitizer = _Sanitizer()

        ctx = await load_case_redaction("case_1956", sanitizer, redis)

        assert ctx.case_id == "case_1956"
        assert ctx.sanitizer is sanitizer and ctx.redis_client is redis
        assert ctx.enabled is should_redact(sanitizer)
        assert ctx.ttl_seconds == (
            get_settings().protection.redaction_registry_ttl_hours * 3600
        )
        if ctx.enabled:
            assert ctx.entity_count == 1

    async def test_no_sanitizer_is_disabled(self):
        ctx = await load_case_redaction("case_1956", None, _Redis())
        assert ctx.enabled is False


class TestTheHelpers:
    async def test_no_context_passes_through(self):
        assert await case_redacted("alice@example.com", None) == "alice@example.com"
        messages = [{"role": "user", "content": "alice@example.com"}]
        out = await case_redacted_messages(messages, None)
        assert out == messages and out[0] is not messages[0]

    async def test_a_context_redacts_and_records(self):
        ctx = _ctx(_Redis())
        assert await case_redacted("user alice@example.com", ctx) == (
            "user <EMAIL_ADDRESS_aaaaaaaaaaaaaaaa>"
        )
        [message] = await case_redacted_messages(
            [{"role": "user", "content": "from 10.0.0.7"}], ctx
        )
        assert message["content"] == "from <IP_ADDRESS_bbbbbbbbbbbbbbbb>"
        assert ctx.entity_count == 2

    @pytest.mark.parametrize(
        "text, found",
        [
            ("<EMAIL_ADDRESS_0123456789abcdef>", True),
            ("x <US_SSN_0a1b2c3d4e5f> y", True),
            ("<IP_ADDRESS_10>", True),
            ("<b>bold</b>", False),
            ("<Email_Address_abc>", False),
            ("plain text", False),
        ],
    )
    def test_the_placeholder_shape(self, text, found):
        assert bool(REDACTION_PLACEHOLDER_RE.search(text)) is found
