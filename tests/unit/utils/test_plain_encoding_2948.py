"""Text spelling a tiktoken special token is encoded as the text it is (#2948):
the chunker, the shared count and the fallback prompt's bound never raise on
it or fall back to the character estimate."""

import base64
import logging
import random
import re
import string

import pytest
import tiktoken

from faultmaven.core.investigation.prompts.templates.fallback import (
    get_fallback_prompt_for_case,
)
from faultmaven.core.preprocessing.vector_storage import chunk_structural_index
from faultmaven.utils.token_estimation import (
    _MAX_ENCODE_RUN,
    _cuts,
    _get_tiktoken_encoder,
    estimate_tokens,
)
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

    assert set(lines) <= {line for c in chunks for line in c.text.split("\n")}


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


def _texts_without_a_long_run() -> list[str]:
    """Varied texts whose longest run of non-whitespace or of whitespace is
    at most the constant."""
    pieces = [
        *SPECIALS,
        "日本語のログ",
        "🙂🚀",
        "café",
        "\r\n",
        "\t",
        " ",
        "=",
        "a",
        "ERROR ",
    ]
    rng = random.Random(2948)
    texts = ["".join(rng.choices(pieces, k=rng.randint(1, 400))) for _ in range(40)]
    texts += [c * _MAX_ENCODE_RUN for c in " a-🙂"]
    texts += ["x" + " " * _MAX_ENCODE_RUN + "y\t" + "-" * _MAX_ENCODE_RUN]
    texts += [base64.b64encode(rng.randbytes(768)).decode() + "\n" + "ab" * 512]
    texts += [
        " ".join("".join(rng.choices(string.ascii_letters, k=1024)) for _ in "xy")
    ]
    longest = max(len(run) for text in texts for run in re.findall(r"\S+|\s+", text))
    assert longest == _MAX_ENCODE_RUN
    return texts


def test_text_without_a_long_run_is_encoded_in_one_call():
    raw = tiktoken.get_encoding("cl100k_base")
    encoder = _get_tiktoken_encoder("gpt-4")

    for text in _texts_without_a_long_run():
        assert encoder.encode(text) == raw.encode(text, disallowed_special=()), text


_RNG = random.Random(1)
_LONG = {
    "spaces": " " * 1_000_000,
    "a": "a" * 1_000_000,
    "dashes": "-" * 1_000_000,
    "ab": "ab" * 500_000,
    "letters": "".join(_RNG.choices(string.ascii_letters, k=1_000_000)),
    "base64": base64.b64encode(_RNG.randbytes(750_000)).decode(),
    "mixed": "log " + "=" * 3000 + " <|endoftext|> 日本",
}


@pytest.mark.parametrize("text", _LONG.values(), ids=_LONG.keys())
def test_a_long_run_is_encoded_in_pieces_that_decode_to_the_text(text):
    encoder = _get_tiktoken_encoder("gpt-4")

    ids = encoder.encode(text)

    assert b"".join(encoder.decode_tokens_bytes(ids)) == text.encode("utf-8")
    assert encoder.decode(ids) == text


def test_a_cut_never_parts_a_surrogate_pair():
    """tiktoken joins a pair it is given whole; parted, each half would be
    replaced by U+FFFD."""
    text = "a" * (_MAX_ENCODE_RUN - 1) + chr(0xD83D) + chr(0xDE00) + "b" * 2000

    ids = _get_tiktoken_encoder("gpt-4").encode(text)

    assert _get_tiktoken_encoder("gpt-4").decode(ids) == text.encode(
        "utf-16", "surrogatepass"
    ).decode("utf-16")


@pytest.mark.parametrize(
    "text",
    [
        "-------\x1c" * 40_000,
        "".join(random.Random(3).choices(string.punctuation + "\x1c\x1f", k=320_000)),
    ],
    ids=["record-separated-dashes", "punctuation-and-separators"],
)
def test_a_record_separator_does_not_end_a_run(text):
    """Python's ``\\s`` takes U+001C-U+001F; tiktoken reads them as
    punctuation, so a run they break for Python is one span for tiktoken."""
    encoder = _get_tiktoken_encoder("gpt-4")
    assert list(_cuts(text)), "no cut: tiktoken would take the text whole"

    ids = encoder.encode(text)

    assert encoder.decode(ids) == text
