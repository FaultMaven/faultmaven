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
import re
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

#: A placeholder as the sanitizer writes it: ``<TYPE_digest>``
#: (``DataSanitizer._hashed_placeholder``). The digest width is not pinned: a
#: registry persisted before #971 holds narrower ones, and a loaded registry
#: keeps handing them out.
REDACTION_PLACEHOLDER_RE = re.compile(r"<[A-Z][A-Z0-9_]*_[0-9a-f]+>")


def _as_text(raw: Any) -> str:
    """A Redis reply as text, whether the client decodes responses or not."""
    return raw.decode("utf-8") if isinstance(raw, bytes) else raw


def should_redact(sanitizer) -> bool:
    """Whether what is sent to a model is redacted at the engine layer.

    The one decision every model-calling path makes — an investigation turn,
    the terminal Q&A turn, case and document conversion: a sanitizer is configured (the DI container hands one out; a
    ``None`` means redaction is disabled at DI level) AND ``SANITIZE_PII`` is
    on. One copy, so no path can drift from the others (#1901).
    """
    if not sanitizer:
        return False

    from faultmaven.config.settings import get_settings

    return get_settings().protection.sanitize_pii


#: The key a model-boundary context is built under. A persisted registry is
#: keyed by its case; this context never loads, saves or cleans one up, so
#: nothing ever reads its key, and no caller is asked for one.
MODEL_BOUNDARY_SCOPE = "model-boundary"


def model_boundary_redaction(sanitizer) -> "CaseRedactionContext":
    """The redaction a knowledge-authoring path applies to what it sends a model.

    The investigation path's mechanism, not a second one: the same class, over
    the same injected sanitizer instance, enabled by :func:`should_redact`. It
    matters independently of the router — ``LLMRouter`` runs its own sanitizer
    pass under the same flag, but that is a property of the default router (a
    deployment may substitute its own via ``LLM_ROUTER_CLASS``), and it is the
    engine's layer, not the router's, that the investigation path relies on.

    No Redis registry is loaded or saved and nothing is ever reversed: the
    investigation path persists its registry so it can put real values back
    into the reply it shows the user, but a runbook is meant to be
    de-identified, so the placeholders the model writes are what is persisted.
    Placeholders are a keyed function of the value (#971), so they match the
    investigation's without the registry — and so the context takes no case or
    conversion id: with no registry, an id would key nothing
    (:data:`MODEL_BOUNDARY_SCOPE`).
    """
    return CaseRedactionContext(
        case_id=MODEL_BOUNDARY_SCOPE,
        sanitizer=sanitizer,
        enabled=should_redact(sanitizer),
    )


class CaseRedactionContext:
    """Case-scoped bidirectional PII redaction registry backed by Redis.

    Ensures the same PII value always gets the same placeholder across
    all evidence files, tool results, and conversation turns within a case.
    """

    #: One Redis HASH per case: field ``json.dumps([entity_type, value])``,
    #: value the placeholder. The ``redaction:`` namespace is the one the
    #: deployment wipe clears.
    REDIS_KEY_PREFIX = "redaction:registry"
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
        # The (type, value) entries known to be in the stored registry: read
        # by load()/save() or written by save(). save() writes the rest.
        self._persisted: Set[Tuple[str, str]] = set()
        self._loaded = False

    async def _read_stored(self) -> Dict[Tuple[str, str], str]:
        """The stored registry, as ``{(entity_type, value): placeholder}``."""
        raw = await self.redis_client.hgetall(self._redis_key)
        stored: Dict[Tuple[str, str], str] = {}
        for field, placeholder in (raw or {}).items():
            entity_type, value = json.loads(_as_text(field))
            stored[(entity_type, value)] = _as_text(placeholder)
        return stored

    def _absorb(self, stored: Dict[Tuple[str, str], str]) -> None:
        """Union the stored registry into this one, and record it as what is
        stored. An entry this context already holds is kept: a placeholder is
        a keyed function of the value (#971), so the two agree."""
        for (entity_type, value), placeholder in stored.items():
            self._forward.setdefault(entity_type, {}).setdefault(value, placeholder)
        self._persisted = set(stored)
        self._rebuild_reverse()

    async def load(self) -> None:
        """Load existing registry from Redis.

        If Redis is unavailable or the key doesn't exist, starts with
        an empty registry (in-memory only for this turn).
        """
        if not self.enabled:
            self._loaded = True
            return

        try:
            self._absorb(await self._read_stored())
            logger.debug(
                f"Loaded redaction registry for case {self.case_id}: "
                f"{self.entity_count} entities"
            )
        except Exception as e:
            logger.warning(
                f"Failed to load redaction registry from Redis for case "
                f"{self.case_id}: {e}. Using in-memory only."
            )

        self._loaded = True

    async def save(self) -> None:
        """Merge this context's entries into the stored registry, and refresh
        its TTL.

        A MERGE, never an overwrite (#1956). The stored registry is read
        first; each entry this context holds that is not stored is written
        with ``HSETNX`` into the case's hash, so the stored registry only
        grows and an entry already there is never replaced. Two contexts on
        one case — two concurrent turns, a turn and the case title, or a
        context whose ``load()`` failed — therefore cannot drop each other's
        entries, whatever order they save in, with no lock and no retry: each
        field is written atomically by Redis. Reading first also restores
        entries the key lost when it expired under a live context.

        The writes and the ``EXPIRE`` go in one MULTI/EXEC transaction, so a
        dropped connection cannot leave a registry with no TTL. The TTL is
        refreshed on EVERY save of a context that holds any entry, new or not:
        an active case's registry lives ``redaction_registry_ttl_hours`` past
        its last turn, not past its last new value. The stored entries are
        read into this context as well, so it can reverse what others added.
        """
        if not self.enabled or not self.entity_count:
            return

        try:
            self._absorb(await self._read_stored())
            unsaved = [
                (entity_type, value, placeholder)
                for entity_type, type_map in self._forward.items()
                for value, placeholder in type_map.items()
                if (entity_type, value) not in self._persisted
            ]
            pipe = self.redis_client.pipeline(transaction=True)
            for entity_type, value, placeholder in unsaved:
                pipe.hsetnx(
                    self._redis_key, json.dumps([entity_type, value]), placeholder
                )
            pipe.expire(self._redis_key, self.ttl_seconds)
            await pipe.execute()
            self._persisted.update((t, v) for t, v, _ in unsaved)
            logger.debug(
                f"Saved redaction registry for case {self.case_id}: "
                f"{len(unsaved)} new of {self.entity_count} entities"
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
        self._rebuild_reverse()
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


class CaseRedactionRegistryEraser:
    """The case delete's eraser for the registry (#1016): the registry holds
    the case's ORIGINAL PII values, so it goes with the case. A Redis error
    raises, which keeps the case row for a retry."""

    store_name = "pii_redaction_registry"

    def __init__(self, redis_client: Any):
        self.redis_client = redis_client

    async def erase_case(self, case_id: str, enterprise_id: str) -> int:
        key = f"{CaseRedactionContext.REDIS_KEY_PREFIX}:{case_id}"
        return int(await self.redis_client.delete(key))


async def load_case_redaction(
    case_id: str, sanitizer: Any, redis_client: Any
) -> CaseRedactionContext:
    """The case's redaction context for one turn, loaded from its registry.

    The one constructor every caller of a case-scoped redaction uses (#1956):
    the investigation service builds the turn's context here once and hands
    the same instance to every model call it makes on the turn's behalf (the
    intent classifier, the out-of-band triage and answer, the engine and its
    tools), so the turn has one load, one save and one reverse. Two contexts
    loaded on one case key would each hold a snapshot, and whichever saved
    last would drop the other's new entries. The case title builds its own,
    after the turn's save. Enabled by :func:`should_redact`; the registry TTL
    comes from ``protection.redaction_registry_ttl_hours``.
    """
    from faultmaven.config.settings import get_settings

    ctx = CaseRedactionContext(
        case_id=case_id,
        sanitizer=sanitizer,
        redis_client=redis_client,
        enabled=should_redact(sanitizer),
        ttl_hours=get_settings().protection.redaction_registry_ttl_hours,
    )
    await ctx.load()
    return ctx
