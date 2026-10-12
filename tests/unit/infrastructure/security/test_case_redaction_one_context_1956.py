"""#1956: the case redaction registry's shared loader, helpers and save rule.

``save()`` used to write the context's whole registry over the stored one.
Two contexts loaded on one case key — two concurrent turns, a turn and the
case title, or a context whose ``load()`` failed — then clobbered each other:
the later save replaced the other's new entries with its own snapshot, and a
placeholder the user was about to read could no longer be reversed. The
registry is now one Redis hash per case and ``save()`` MERGES: each new entry
is written with ``HSETNX``, so the stored registry only grows, and the stored
registry is read back into the context.
"""

from __future__ import annotations

import fakeredis.aioredis
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

EMAIL, EMAIL_PH = "alice@example.com", "<EMAIL_ADDRESS_aaaaaaaaaaaaaaaa>"
IP, IP_PH = "10.0.0.7", "<IP_ADDRESS_bbbbbbbbbbbbbbbb>"
HOST, HOST_PH = "db-01.corp", "<HOSTNAME_cccccccccccccccc>"
KEY = "redaction:registry:case_1956"


class _Sanitizer:
    """Registry-backed detection of three literal values."""

    VALUES = {
        EMAIL: ("EMAIL_ADDRESS", EMAIL_PH),
        IP: ("IP_ADDRESS", IP_PH),
        HOST: ("HOSTNAME", HOST_PH),
    }

    def sanitize_text_with_registry(self, text, registry):
        for value, (kind, placeholder) in self.VALUES.items():
            if value in text:
                registry.setdefault(kind, {}).setdefault(value, placeholder)
                text = text.replace(value, placeholder)
        return text


class _CountingRedis(fakeredis.aioredis.FakeRedis):
    """FakeRedis that counts pipelines executed (the save's one write)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.writes = 0

    def pipeline(self, *args, **kwargs):
        self.writes += 1
        return super().pipeline(*args, **kwargs)


class _FailingLoadRedis(fakeredis.aioredis.FakeRedis):
    """Its first read fails, as a Redis blip would."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.reads_to_fail = 1

    async def hgetall(self, name):
        if self.reads_to_fail:
            self.reads_to_fail -= 1
            raise ConnectionError("blip")
        return await super().hgetall(name)


def _ctx(redis) -> CaseRedactionContext:
    return CaseRedactionContext(
        case_id="case_1956", sanitizer=_Sanitizer(), redis_client=redis, enabled=True
    )


async def _stored_placeholders(redis) -> set[str]:
    return {
        p.decode() if isinstance(p, bytes) else p
        for p in (await redis.hgetall(KEY)).values()
    }


class TestTheSaveMerges:
    async def test_two_contexts_that_both_grow_keep_both(self):
        """Two turns loaded before either saved; each redacts a new value."""
        redis = fakeredis.aioredis.FakeRedis()
        a, b = _ctx(redis), _ctx(redis)
        await a.load()
        await b.load()
        await a.asanitize(f"user {EMAIL}")
        await b.asanitize(f"from {IP}")
        await a.save()
        await b.save()

        assert await _stored_placeholders(redis) == {EMAIL_PH, IP_PH}
        # The later saver read the other's entry back, so it can reverse it.
        assert b.reverse(EMAIL_PH) == EMAIL

    async def test_a_context_that_grew_nothing_new_does_not_drop_a_peers_entry(self):
        redis = fakeredis.aioredis.FakeRedis()
        seed = _ctx(redis)
        await seed.load()
        await seed.asanitize(f"from {IP}")
        await seed.save()

        a, b = _ctx(redis), _ctx(redis)
        await a.load()
        await b.load()
        await a.asanitize(f"user {EMAIL}")
        await a.save()
        await b.asanitize(f"again from {IP}")  # nothing new
        await b.save()

        assert await _stored_placeholders(redis) == {EMAIL_PH, IP_PH}

    async def test_a_context_whose_load_failed_does_not_overwrite_the_registry(self):
        redis = _FailingLoadRedis()
        redis.reads_to_fail = 0
        seed = _ctx(redis)
        await seed.load()
        await seed.asanitize(f"user {EMAIL} from {IP}")
        await seed.save()

        redis.reads_to_fail = 1
        blind = _ctx(redis)
        await blind.load()  # fails: starts empty
        assert blind.entity_count == 0
        await blind.asanitize(f"host {HOST}")
        await blind.save()

        assert await _stored_placeholders(redis) == {EMAIL_PH, IP_PH, HOST_PH}

    async def test_a_save_with_nothing_new_writes_nothing(self):
        redis = _CountingRedis()
        ctx = _ctx(redis)
        await ctx.load()
        await ctx.asanitize("no sensitive value here")
        ctx.sanitize("still none")
        await ctx.save()
        assert redis.writes == 0

    async def test_a_save_writes_each_new_entry_once(self):
        redis = _CountingRedis()
        ctx = _ctx(redis)
        await ctx.load()
        await ctx.asanitize(f"user {EMAIL}")
        await ctx.save()
        await ctx.save()
        assert redis.writes == 1
        assert ctx.reverse(EMAIL_PH) == EMAIL

    async def test_the_sync_pass_is_saved_too(self):
        redis = fakeredis.aioredis.FakeRedis()
        ctx = _ctx(redis)
        await ctx.load()
        ctx.sanitize(f"user {EMAIL}")
        await ctx.save()
        assert await _stored_placeholders(redis) == {EMAIL_PH}

    async def test_the_ttl_is_kept(self):
        redis = fakeredis.aioredis.FakeRedis()
        ctx = CaseRedactionContext(
            "case_1956", _Sanitizer(), redis, enabled=True, ttl_hours=2
        )
        await ctx.load()
        await ctx.asanitize(f"user {EMAIL}")
        await ctx.save()
        assert 3600 < await redis.ttl(KEY) <= 7200


class TestTheLoader:
    async def test_it_builds_enabled_by_should_redact_and_loads(self):
        redis = fakeredis.aioredis.FakeRedis()
        seed = _ctx(redis)
        await seed.load()
        await seed.asanitize(f"user {EMAIL}")
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
        ctx = await load_case_redaction(
            "case_1956", None, fakeredis.aioredis.FakeRedis()
        )
        assert ctx.enabled is False


class TestTheHelpers:
    async def test_no_context_passes_through(self):
        assert await case_redacted("alice@example.com", None) == "alice@example.com"
        messages = [{"role": "user", "content": "alice@example.com"}]
        out = await case_redacted_messages(messages, None)
        assert out == messages and out[0] is not messages[0]

    async def test_a_context_redacts_and_records(self):
        ctx = _ctx(fakeredis.aioredis.FakeRedis())
        assert await case_redacted(f"user {EMAIL}", ctx) == f"user {EMAIL_PH}"
        [message] = await case_redacted_messages(
            [{"role": "user", "content": f"from {IP}"}], ctx
        )
        assert message["content"] == f"from {IP_PH}"
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
