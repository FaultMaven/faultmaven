"""
Exact-key response cache for LLM responses.

Caches a provider response under the exact ``(case, prompt, model)`` triple, so
a repeated identical call inside one case is served without a second API call.

This used to also do semantic matching: embed the prompt with BGE-M3, and serve
any cached entry within cosine 0.85 of it. That branch was removed (#940) for
two independent reasons.

*It could not run on the request path safely.* The embed was a bare synchronous
``encoder.encode([text])[0]`` called from sync ``check``/``store``, which the
async ``LLMRouter.route`` awaits — CPU-bound work directly on the event loop,
the same shape that blocks ``/health`` long enough for the liveness probe to
SIGKILL the pod and return 502s. The cache path only arms when ``route`` gets a
bare ``prompt`` *and* an explicit ``model``, which today means the
``STRUCTURED_OUTPUT_PROVIDER`` override — putting the encode on the per-turn
structured-output path, the largest prompts in the system.

*And it was unsound for this product.* Consecutive investigation turns in one
case share most of their context, so two prompts one turn apart sit well above
0.85 while asking different questions. Serving turn N's structured output for
turn N+1 hands the milestone engine a stale conclusion presented as fresh.
FaultMaven's guarantee is NO INCORRECT CONCLUSION; a cache must never be the
thing that manufactures one.

What remains is exact-key only: identical prompt, identical model, identical
case ⇒ identical response. ``check``/``store`` are pure dict operations plus one
sha256, so they are safe to call from async code without a thread hop.
"""

import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .providers import LLMResponse


class LLMResponseCache:
    """Exact-key, case-scoped cache of LLM responses."""

    def __init__(self, max_size: int = 1000):
        self.max_size = max_size
        self.cache: Dict[str, Dict[str, Any]] = {}

    def _get_cache_key(
        self, prompt: str, model: str, case_id: Optional[str] = None
    ) -> str:
        """Generate cache key scoped to case, prompt, and model"""
        content = f"{case_id or ''}:{prompt}:{model}"
        return hashlib.sha256(content.encode()).hexdigest()

    def check(
        self, prompt: str, model: str, case_id: Optional[str] = None
    ) -> Optional[LLMResponse]:
        """Return the response cached for this exact prompt/model/case, if any.

        Exact match only — a prompt that differs by one character is a miss.
        The key carries ``case_id``, so a response is never served across cases.

        Synchronous by design and safe to call from the event loop: no embedding,
        no I/O, no similarity scan (#940).
        """
        cache_key = self._get_cache_key(prompt, model, case_id)
        cache_entry = self.cache.get(cache_key)
        if cache_entry is None:
            return None

        return LLMResponse(
            content=cache_entry["content"],
            confidence=cache_entry["confidence"],
            provider=cache_entry["provider"],
            model=cache_entry["model"],
            tokens_used=cache_entry["tokens_used"],
            response_time_ms=0,  # Cached response
            cached=True,
        )

    def store(
        self,
        prompt: str,
        model: str,
        response: LLMResponse,
        case_id: Optional[str] = None,
    ):
        """Store response under its exact prompt/model/case key."""
        cache_key = self._get_cache_key(prompt, model, case_id)

        self.cache[cache_key] = {
            "content": response.content,
            "confidence": response.confidence,
            "provider": response.provider,
            "model": response.model,
            "tokens_used": response.tokens_used,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # Evict oldest entries if cache is full. Ties in the ISO timestamp
        # resolve to the earliest-inserted key, since dicts iterate in insertion
        # order and ``min`` keeps the first minimum it sees.
        if len(self.cache) > self.max_size:
            oldest_key = min(
                self.cache.keys(), key=lambda k: self.cache[k]["timestamp"]
            )
            del self.cache[oldest_key]
