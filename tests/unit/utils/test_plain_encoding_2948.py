"""Text spelling a tiktoken special token is encoded as the text it is (#2948):
the chunker, the shared count and the fallback prompt's bound never raise on
it or fall back to the character estimate."""

import logging

import pytest
import tiktoken

from faultmaven.core.investigation.prompts.templates.fallback import (
    get_fallback_prompt_for_case,
)
from faultmaven.core.preprocessing.vector_storage import chunk_structural_index
from faultmaven.utils.token_estimation import _get_tiktoken_encoder, estimate_tokens
from tests.case_builders import investigating_case

pytestmark = pytest.mark.unit

#: Every special token cl100k_base defines (the first test holds it whole).
SPECIALS = (
    "<|endoftext|>",
    "<|endofprompt|>",
    "<|fim_prefix|>",
    "<|fim_middle|>",
    "<|fim_suffix|>",
)


def test_the_loader_s_encoder_offers_only_plain_encoding():
    assert set(SPECIALS) == tiktoken.get_encoding("cl100k_base").special_tokens_set
    encoder = _get_tiktoken_encoder("gpt-4")

    public = {name for name in dir(encoder) if not name.startswith("_")}

    assert public == {"encode", "decode", "decode_tokens_bytes"}


@pytest.mark.parametrize("special", SPECIALS)
@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_the_shared_count_is_tiktoken_s(special, provider, caplog):
    text = f"hi {special} there"
    plain = tiktoken.get_encoding("cl100k_base").encode_ordinary(text)

    with caplog.at_level(logging.WARNING, logger="faultmaven.utils.token_estimation"):
        count = estimate_tokens(text, provider)

    assert count == len(plain)
    assert caplog.records == [], "no fallback warning"


@pytest.mark.parametrize("special", SPECIALS)
def test_the_chunker_chunks_a_log_quoting_a_special_token(special):
    lines = [f"vllm generated: hi{special} req={i}" for i in range(200)]

    chunks = chunk_structural_index("=== Log ===\n" + "\n".join(lines), 500, 50)

    assert all(any(line in c.text for c in chunks) for line in lines)


@pytest.mark.parametrize("special", SPECIALS)
def test_the_fallback_prompt_carries_a_special_token_in_the_message(special):
    case = investigating_case(
        case_id="case_aabb11223344",
        user_id="user_123",
        enterprise_id="org_123",
        statement="vLLM replies stop early",
    )
    message = f"vllm out: hi{special} req=1"

    assert message in get_fallback_prompt_for_case(case, message)
