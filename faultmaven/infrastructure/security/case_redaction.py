"""Case-Scoped PII Redaction Context

Maintains consistent placeholder mappings across all evidence files
and tool results within a single investigation case. Backed by Redis
for persistence across requests within the same case.

The key insight: each `DataSanitizer.sanitize()` call creates a fresh
entity registry, so different files in the same case get colliding
placeholders (e.g., two different IPs both become `<IP_ADDRESS_1>`).
This class provides a case-scoped registry that persists across calls,
ensuring cross-file consistency.

Usage:
    ctx = CaseRedactionContext(case_id, sanitizer, redis_client)
    await ctx.load()               # Load existing registry from Redis
    redacted = ctx.sanitize(text)   # Redact with case-scoped registry
    original = ctx.reverse(text)    # Reverse placeholders → originals
    await ctx.save()                # Persist updated registry to Redis
"""

import json
import logging
import re
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


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
        if not self.redis_client or not self.enabled:
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
        if not self.redis_client or not self.enabled or not self._dirty:
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
            Text with PII replaced by indexed placeholders.
        """
        if not self.enabled or not text or not isinstance(text, str):
            return text

        result = self.sanitizer.sanitize_text_with_registry(text, self._forward)

        # Sync reverse mapping from forward registry
        self._rebuild_reverse()
        self._dirty = True

        return result

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
        if not self.redis_client:
            return

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
