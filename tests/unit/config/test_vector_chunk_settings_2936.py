"""``VECTOR_CHUNK_SIZE_TOKENS`` is at least 16 and ``VECTOR_CHUNK_OVERLAP_TOKENS``
between 0 and half of it, or startup fails: an over-long line is cut into
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


@pytest.mark.parametrize(
    ("size", "overlap"), [("500", "0"), ("16", "8"), ("17", "8"), ("500", "250")]
)
def test_inside_the_bounds_is_accepted(size, overlap):
    assert _chunking(
        VECTOR_CHUNK_SIZE_TOKENS=size, VECTOR_CHUNK_OVERLAP_TOKENS=overlap
    ) == (int(size), int(overlap))


@pytest.mark.parametrize(
    ("size", "overlap", "names"),
    [
        ("15", "0", "VECTOR_CHUNK_SIZE_TOKENS"),
        ("500", "-1", "VECTOR_CHUNK_OVERLAP_TOKENS"),
        ("500", "251", "VECTOR_CHUNK_OVERLAP_TOKENS"),
        ("17", "9", "VECTOR_CHUNK_OVERLAP_TOKENS"),
        ("500", "499", "VECTOR_CHUNK_OVERLAP_TOKENS"),
    ],
)
def test_anything_else_fails_startup(size, overlap, names):
    with pytest.raises(ValidationError, match=names):
        _chunking(VECTOR_CHUNK_SIZE_TOKENS=size, VECTOR_CHUNK_OVERLAP_TOKENS=overlap)
