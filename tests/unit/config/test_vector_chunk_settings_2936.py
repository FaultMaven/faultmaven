"""``VECTOR_CHUNK_SIZE_TOKENS`` is at least 1 and ``VECTOR_CHUNK_OVERLAP_TOKENS``
at least 0 and below it, or startup fails: an over-long line is cut into
pieces of size minus overlap (#2936)."""

import os
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from faultmaven.config.settings import DatabaseSettings

pytestmark = [pytest.mark.unit]

_KEYS = ("VECTOR_CHUNK_SIZE_TOKENS", "VECTOR_CHUNK_OVERLAP_TOKENS")


def _chunking(**values: str) -> tuple[int, int]:
    env = {k: v for k, v in os.environ.items() if k not in _KEYS}
    env.update(values)
    with patch.dict(os.environ, env, clear=True):
        settings = DatabaseSettings()
    return settings.vector_chunk_size_tokens, settings.vector_chunk_overlap_tokens


def test_the_defaults_are_accepted():
    assert _chunking() == (500, 50)


def test_no_overlap_is_accepted():
    assert _chunking(VECTOR_CHUNK_OVERLAP_TOKENS="0") == (500, 0)


@pytest.mark.parametrize(
    ("size", "overlap", "names"),
    [
        ("0", "0", "VECTOR_CHUNK_SIZE_TOKENS"),
        ("500", "-1", "VECTOR_CHUNK_OVERLAP_TOKENS"),
        ("500", "500", "VECTOR_CHUNK_OVERLAP_TOKENS"),
        ("100", "200", "VECTOR_CHUNK_OVERLAP_TOKENS"),
    ],
)
def test_anything_else_fails_startup(size, overlap, names):
    with pytest.raises(ValidationError, match=names):
        _chunking(VECTOR_CHUNK_SIZE_TOKENS=size, VECTOR_CHUNK_OVERLAP_TOKENS=overlap)
