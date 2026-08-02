"""The LLM response cache serves exact keys and nothing else (#940).

The cache used to fall back to semantic matching: embed the prompt with BGE-M3
and serve any entry within cosine 0.85. That branch ran a synchronous encode on
the event loop, and — worse — could serve one investigation turn's answer for
the next, since consecutive turns in a case share most of their context. It was
deleted; ``check`` now answers only for a byte-identical prompt under the same
model and the same case.

Most of what follows is negative control. "Serves the right thing" is easy to
keep passing by accident; "serves nothing else" is the property that stops a
cache from manufacturing a wrong conclusion, so it is tested as a swept space
rather than a single example.
"""

import pytest

from faultmaven.infrastructure.llm.cache import LLMResponseCache
from faultmaven.infrastructure.llm.providers import LLMResponse

pytestmark = [pytest.mark.unit, pytest.mark.llm]

PROMPT = "Node node-3 went NotReady at 14:02. What should I check first?"


def _response(content: str = "check kubelet logs", **overrides) -> LLMResponse:
    fields = {
        "content": content,
        "confidence": 0.91,
        "provider": "openai",
        "model": "gpt-5.4-mini",
        "tokens_used": 1234,
        "response_time_ms": 8710,
    }
    fields.update(overrides)
    return LLMResponse(**fields)


def _seeded(prompt: str = PROMPT, model: str = "gpt-5.4-mini", case_id: str = "case-1"):
    cache = LLMResponseCache()
    cache.store(prompt, model, _response(), case_id=case_id)
    return cache


# --------------------------------------------------------------------------- #
# The one thing it does
# --------------------------------------------------------------------------- #


def test_exact_key_hit_returns_the_stored_response():
    """A repeat of the identical call is served from memory, flagged as cached
    and reporting zero latency — the metrics path keys `status="cached"` off
    exactly this, and a cached hit that reported the original latency would
    poison the LLM latency histogram."""
    cache = _seeded()

    hit = cache.check(PROMPT, "gpt-5.4-mini", case_id="case-1")

    assert hit is not None
    assert hit.content == "check kubelet logs"
    assert hit.confidence == 0.91
    assert hit.provider == "openai"
    assert hit.model == "gpt-5.4-mini"
    assert hit.tokens_used == 1234
    assert hit.cached is True
    assert hit.response_time_ms == 0


def test_a_case_id_of_none_is_a_key_like_any_other():
    """Un-scoped entries (no case) must round-trip too — the router passes
    ``case_id=None`` for every call made outside a case."""
    cache = LLMResponseCache()
    cache.store(PROMPT, "gpt-5.4-mini", _response(), case_id=None)

    assert cache.check(PROMPT, "gpt-5.4-mini", case_id=None) is not None


# --------------------------------------------------------------------------- #
# ...and the things it must never do
# --------------------------------------------------------------------------- #


NEAR_MISSES = {
    "trailing_character": PROMPT + "?",
    "dropped_character": PROMPT[:-1],
    "substituted_character": PROMPT.replace("node-3", "node-4"),
    "transposed_characters": PROMPT.replace("NotReady", "NotRaedy"),
    "changed_case": PROMPT.lower(),
    "extra_whitespace": PROMPT.replace(" ", "  ", 1),
    "trailing_newline": PROMPT + "\n",
    # The shape that made semantic matching unsound here: turn N+1 of the same
    # investigation, which shares nearly all of turn N's context but asks a
    # different question. Under cosine ≥0.85 this scored as a hit.
    "next_investigation_turn": (
        PROMPT + "\n\nI checked the kubelet; it is running. What now?"
    ),
}


@pytest.mark.parametrize("label", sorted(NEAR_MISSES))
def test_near_miss_prompts_are_misses(label):
    """The mutation-relevant proof that semantic serving is gone. Every one of
    these is within trivial edit distance (and far above cosine 0.85) of the
    stored prompt; every one of them must miss."""
    cache = _seeded()

    assert cache.check(NEAR_MISSES[label], "gpt-5.4-mini", case_id="case-1") is None


@pytest.mark.parametrize("other_case", ["case-2", None, "", "case-1 "])
def test_a_response_is_never_served_across_cases(other_case):
    """Case isolation is a tenancy boundary, not an optimisation: one case's
    answer surfacing in another is a data leak as well as a wrong answer."""
    cache = _seeded(case_id="case-1")

    assert cache.check(PROMPT, "gpt-5.4-mini", case_id=other_case) is None


@pytest.mark.parametrize(
    "other_model", ["gpt-5.4", "claude-sonnet-4-6", "gpt-5.4-mini-2025", ""]
)
def test_a_response_is_never_served_across_models(other_model):
    """The model is part of the key: a cheap classifier's answer must not stand
    in for the primary chat model's, and vice versa."""
    cache = _seeded(model="gpt-5.4-mini")

    assert cache.check(PROMPT, other_model, case_id="case-1") is None


def test_a_populated_cache_still_misses_on_a_near_key():
    """A miss must stay a miss no matter how much neighbouring material is
    resident — the deleted branch scanned every same-case, same-model entry
    looking for something close enough."""
    cache = LLMResponseCache()
    for turn in range(25):
        cache.store(
            f"{PROMPT} (turn {turn})",
            "gpt-5.4-mini",
            _response(content=f"answer {turn}"),
            case_id="case-1",
        )

    assert cache.check(f"{PROMPT} (turn 25)", "gpt-5.4-mini", case_id="case-1") is None
    assert cache.check(PROMPT, "gpt-5.4-mini", case_id="case-1") is None


# --------------------------------------------------------------------------- #
# Bounded memory
# --------------------------------------------------------------------------- #


def test_eviction_drops_the_oldest_entry_first():
    """The cache lives for the process lifetime, so an unbounded one is a slow
    leak. Overflow evicts the oldest entry and keeps the rest servable."""
    cache = LLMResponseCache(max_size=2)
    for index in range(3):
        cache.store(
            f"prompt-{index}",
            "gpt-5.4-mini",
            _response(content=f"answer-{index}"),
            case_id="case-1",
        )

    assert len(cache.cache) == 2
    assert cache.check("prompt-0", "gpt-5.4-mini", case_id="case-1") is None
    for index in (1, 2):
        hit = cache.check(f"prompt-{index}", "gpt-5.4-mini", case_id="case-1")
        assert hit is not None and hit.content == f"answer-{index}"


def test_the_cache_never_exceeds_max_size():
    """Swept rather than pinned at the boundary: whatever the fill order, the
    bound holds after every store."""
    cache = LLMResponseCache(max_size=5)
    for index in range(50):
        cache.store(
            f"prompt-{index}",
            "gpt-5.4-mini",
            _response(content=f"answer-{index}"),
            case_id="case-1",
        )
        assert len(cache.cache) <= 5
