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
from typing import Any, Dict, Optional

from .providers import LLMResponse, StopReason


class LLMResponseCache:
    """Exact-key, case-scoped cache of LLM responses."""

    def __init__(self, max_size: int = 1000):
        self.max_size = max_size
        self.cache: Dict[str, Dict[str, Any]] = {}

    def _get_cache_key(
        self, prompt: str, model: str, case_id: Optional[str] = None
    ) -> str:
        """Generate cache key scoped to case, prompt, and model.

        The fields are length-prefixed rather than joined on a delimiter (#940
        review). ``':'`` is legal inside every one of them — Ollama models are
        literally ``llama3:8b`` — so a joined key is ambiguous: shifting the
        boundary between two fields produces the same string, and
        ``(prompt='P', model='llama3:8b')`` would collide with
        ``(prompt='P:llama3', model='8b')``. A collision here serves one call's
        answer for a different call, which is the one thing a cache must never
        do. Length prefixes make the encoding injective, so distinct triples
        always hash apart.

        ``None`` gets its own flag byte instead of folding to ``''``: an
        un-scoped call and a call carrying an empty case id are different
        namespaces, and only one of them may serve the other's entries.
        """
        digest = hashlib.sha256()
        for field in (case_id, prompt, model):
            if field is None:
                digest.update(b"\x00")
                continue
            encoded = field.encode()
            # Feed the parts separately — no full-string concatenation.
            digest.update(b"\x01")
            digest.update(f"{len(encoded)}:".encode())
            digest.update(encoded)
        return digest.hexdigest()

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
            # Rehydrate the stop reason with the rest of the entry. A field
            # stored but not rehydrated is worse than one never stored: every
            # replay would confidently report UNKNOWN, laundering a cut body
            # into a complete-looking one for as long as the entry lives — and
            # there is no TTL and no eviction API here (#1094).
            stop_reason=cache_entry.get("stop_reason", StopReason.UNKNOWN),
        )

    def store(
        self,
        prompt: str,
        model: str,
        response: LLMResponse,
        case_id: Optional[str] = None,
    ):
        """Store response under its exact prompt/model/case key.

        An existing entry under the key is replaced. That happens only on the
        bypass-cache path: a caller that skipped the lookup because it had
        already found the cached answer unusable — a truncated structured-output
        body (#513) — and is now writing the complete one over it. There is no
        eviction API, so this overwrite is the sole way a bad entry stops being
        served.
        """
        cache_key = self._get_cache_key(prompt, model, case_id)

        self.cache[cache_key] = {
            "content": response.content,
            "confidence": response.confidence,
            "provider": response.provider,
            "model": response.model,
            "tokens_used": response.tokens_used,
            "stop_reason": response.stop_reason,
        }

        # Evict the oldest entry when full. Insertion order is age-of-first-write
        # here: an ordinary repeat of a key is answered by ``check`` before
        # ``store`` is reached, so almost every write is an insert, and a dict
        # assignment to an existing key keeps its original position rather than
        # moving it to the end. So the first key the dict yields is the oldest
        # first-written, and eviction is O(1) instead of a scan over every entry.
        # The bypass-cache overwrite therefore refreshes the value without
        # refreshing the age — accepted, because it is the rare retry path and
        # evicting it early costs one cache miss, never a wrong answer.
        if len(self.cache) > self.max_size:
            del self.cache[next(iter(self.cache))]
