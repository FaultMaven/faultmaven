"""Case-Scoped PII Redaction Context

Maintains consistent placeholder mappings across all evidence files
and tool results within a single investigation case. Backed by Redis
for persistence across requests within the same case.

The key insight: each `DataSanitizer.sanitize()` call creates a fresh entity
registry, so nothing in a bare sanitize call remembers what an earlier one
assigned. Placeholder *agreement* across calls comes from the pseudonym being
a keyed function of the value (#971), but agreement is not enough on its own —
reversing a placeholder needs the mapping, and a throwaway registry discards
it. This class provides a case-scoped registry that persists across calls, so
the case keeps the only mapping back to the original values.

Usage:
    ctx = CaseRedactionContext(case_id, sanitizer, redis_client)
    await ctx.load()               # Load existing registry from Redis
    redacted = ctx.sanitize(text)   # Redact with case-scoped registry
    original = ctx.reverse(text)    # Reverse placeholders → originals
    await ctx.save()                # Persist updated registry to Redis
"""

import asyncio
import json
import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def should_redact(sanitizer) -> bool:
    """Whether what is sent to a model is redacted at the engine layer.

    The one decision every model-calling path makes — an investigation turn,
    the terminal Q&A turn, case→runbook extraction and conversion, document
    conversion: a sanitizer is configured (the DI container hands one out; a
    ``None`` means redaction is disabled at DI level) AND ``SANITIZE_PII`` is
    on. One copy, so no path can drift from the others (#1901).
    """
    if not sanitizer:
        return False

    from faultmaven.config.settings import get_settings

    return get_settings().protection.sanitize_pii


def model_boundary_redaction(scope_id: str, sanitizer) -> "CaseRedactionContext":
    """The redaction a knowledge-authoring path applies to what it sends a model.

    The investigation path's mechanism, not a second one: the same class, over
    the same injected sanitizer instance, enabled by :func:`should_redact`. It
    matters independently of the router — ``LLMRouter`` runs its own sanitizer
    pass under the same flag, but that is a property of the default router (a
    deployment may substitute its own via ``LLM_ROUTER_CLASS``), and it is the
    engine's layer, not the router's, that the investigation path relies on.

    ``scope_id`` keys the context: the case id when the text is a case's, a
    per-conversion id when it is an uploaded document's. No Redis registry is
    loaded or saved and nothing is ever reversed: the investigation path
    persists its registry so it can put real values back into the reply it
    shows the user, but a runbook is meant to be de-identified, so the
    placeholders the model writes are what is persisted. Placeholders are a
    keyed function of the value (#971), so they match the investigation's
    without the registry.
    """
    return CaseRedactionContext(
        case_id=scope_id,
        sanitizer=sanitizer,
        enabled=should_redact(sanitizer),
    )


class CaseRedactionContext:
    """Case-scoped bidirectional PII redaction registry backed by Redis.

    Ensures the same PII value always gets the same placeholder across
    all evidence files, tool results, and conversation turns within a case.
    """

    REDIS_KEY_PREFIX = "redaction"
    DEFAULT_TTL_HOURS = 168  # 7 days

    def __init__(
        self,
        case_id: str,
        sanitizer: Any,
        redis_client: Any = None,
        enabled: bool = True,
        ttl_hours: int = DEFAULT_TTL_HOURS,
    ):
        """Initialize case redaction context.

        Args:
            case_id: Case identifier for scoping the registry.
            sanitizer: DataSanitizer instance for PII detection logic.
            redis_client: Async Redis client for persistence. If None,
                registry is in-memory only (consistent within turn).
            enabled: If False, sanitize() and reverse() are no-ops.
            ttl_hours: Redis key TTL in hours.
        """
        self.case_id = case_id
        self.sanitizer = sanitizer
        self.redis_client = redis_client
        self.enabled = enabled
        self.ttl_seconds = ttl_hours * 3600
        self._redis_key = f"{self.REDIS_KEY_PREFIX}:{case_id}"

        # Bidirectional mapping state
        self._forward: Dict[str, Dict[str, str]] = {}  # type → {value → placeholder}
        self._reverse: Dict[str, str] = {}  # placeholder → original value
        self._dirty = False  # Track whether save is needed
        self._loaded = False

    async def load(self) -> None:
        """Load existing registry from Redis.

        If Redis is unavailable or the key doesn't exist, starts with
        an empty registry (in-memory only for this turn).
        """
        if not self.enabled:
            self._loaded = True
            return

        try:
            raw = await self.redis_client.get(self._redis_key)
            if raw:
                data = json.loads(raw)
                self._forward = data.get("forward", {})
                self._reverse = data.get("reverse", {})
                logger.debug(
                    f"Loaded redaction registry for case {self.case_id}: "
                    f"{sum(len(v) for v in self._forward.values())} entities"
                )
        except Exception as e:
            logger.warning(
                f"Failed to load redaction registry from Redis for case "
                f"{self.case_id}: {e}. Using in-memory only."
            )

        self._loaded = True

    async def save(self) -> None:
        """Persist current registry to Redis with TTL.

        Only writes if the registry was modified since last load/save.
        """
        if not self.enabled or not self._dirty:
            return

        try:
            data = json.dumps({"forward": self._forward, "reverse": self._reverse})
            await self.redis_client.set(self._redis_key, data, ex=self.ttl_seconds)
            self._dirty = False
            logger.debug(
                f"Saved redaction registry for case {self.case_id}: "
                f"{sum(len(v) for v in self._forward.values())} entities"
            )
        except Exception as e:
            logger.warning(
                f"Failed to save redaction registry to Redis for case "
                f"{self.case_id}: {e}. Changes are in-memory only."
            )

    def sanitize(self, text: str) -> str:
        """Redact PII in text using case-scoped registry.

        Delegates PII detection to the DataSanitizer's pattern matching
        and Presidio integration, but uses the case-scoped registry for
        consistent placeholder assignment across files.

        Args:
            text: Input text to redact.

        Returns:
            Text with PII replaced by ``<TYPE_digest>`` placeholders.
        """
        if not self.enabled or not text or not isinstance(text, str):
            return text

        result = self.sanitizer.sanitize_text_with_registry(text, self._forward)

        # Sync reverse mapping from forward registry
        self._rebuild_reverse()
        self._dirty = True

        return result

    async def asanitize(self, text: str) -> str:
        """Async boundary for :meth:`sanitize`.

        Offloads the sanitizer's CPU-bound regex passes and blocking Presidio
        round-trip to a worker thread so the event loop stays responsive. Any
        ``async`` caller MUST use this instead of ``sanitize()`` (#654). The
        registry mutation and reverse-map rebuild run back on the loop thread
        after the offloaded work completes, so no locking is needed for the
        sequential per-turn access pattern.
        """
        if not self.enabled or not text or not isinstance(text, str):
            return text

        result = await asyncio.to_thread(
            self.sanitizer.sanitize_text_with_registry, text, self._forward
        )

        self._rebuild_reverse()
        self._dirty = True

        return result

    async def asanitize_messages(
        self, messages: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """:meth:`asanitize` over each message's ``content``, in order.

        For a chat-shaped call (``router.route(messages=...)``): every message
        is redacted, the system prompt included. Returns new dicts; the input
        list is not mutated, and keys other than ``content`` ride through.

        Text content only. ``asanitize`` passes anything that is not a ``str``
        through unchanged, so a message carrying structured content (a list of
        parts, a dict) would leave in clear — where the router's own pass,
        ``DataSanitizer.asanitize``, walks lists and dicts. Rather than send it,
        an enabled context refuses: ``TypeError`` names the shape. No caller
        sends structured content today; one that starts to must redact it
        first. A disabled context redacts nothing, so it checks nothing.

        Raises:
            TypeError: enabled, and a message's ``content`` is not a ``str``.
        """
        if not self.enabled:
            return [dict(message) for message in messages]
        for index, message in enumerate(messages):
            content = message.get("content")
            if not isinstance(content, str):
                raise TypeError(
                    f"cannot redact message {index} "
                    f"(role={message.get('role')!r}): content is "
                    f"{type(content).__name__}, not str"
                )
        return [
            {**message, "content": await self.asanitize(message["content"])}
            for message in messages
        ]

    def reverse(self, text: str) -> str:
        """Replace all placeholders in text with original values.

        Sorts by placeholder length descending to avoid partial
        replacement issues (e.g., `<IP_ADDRESS_10>` before `<IP_ADDRESS_1>`).

        Args:
            text: Text containing placeholders.

        Returns:
            Text with placeholders replaced by original values.
        """
        if not self.enabled or not text or not self._reverse:
            return text

        # Sort by length descending to replace longer placeholders first
        sorted_placeholders = sorted(
            self._reverse.items(), key=lambda x: len(x[0]), reverse=True
        )

        result = text
        for placeholder, original in sorted_placeholders:
            result = result.replace(placeholder, original)

        return result

    def _rebuild_reverse(self) -> None:
        """Rebuild reverse mapping from forward registry.

        Called after sanitize() to ensure reverse mapping stays in sync
        with any new entries added by the sanitizer.
        """
        self._reverse.clear()
        for type_map in self._forward.values():
            for original, placeholder in type_map.items():
                self._reverse[placeholder] = original

    async def cleanup(self) -> None:
        """Delete the Redis key for this case.

        Call when a case is closed/resolved to free Redis memory.
        """
        try:
            await self.redis_client.delete(self._redis_key)
            logger.debug(f"Cleaned up redaction registry for case {self.case_id}")
        except Exception as e:
            logger.warning(
                f"Failed to cleanup redaction registry for case {self.case_id}: {e}"
            )

    @property
    def entity_count(self) -> int:
        """Total number of unique PII entities tracked."""
        return sum(len(v) for v in self._forward.values())
