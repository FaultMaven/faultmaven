"""The image carries every tiktoken encoding the code loads.

Every token count goes through tiktoken, which downloads an encoding's BPE file
on first use. The Dockerfile bakes the file into the image, so a pod with no
network counts real tokens instead of four characters a token. That holds only
while the baked set covers what the code asks for: code that starts loading a
second encoding needs a second prefetch, and nothing else would notice.

The CI image build proves the prefetch works, because its second load runs
through an unreachable proxy. These tests pin what that build cannot see: which
encodings the code loads, and that the image keeps baking them and keeps
checking the cache offline.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCKERFILE = REPO_ROOT / "Dockerfile"
SOURCE = REPO_ROOT / "faultmaven"

#: ``tiktoken.get_encoding("x")``, and ``encoding_name = "x"`` for the loader
#: that picks a name first and passes it on.
_LOADED = re.compile(r"""(?:get_encoding\(|encoding_name\s*=\s*)["']([a-z0-9_]+)["']""")
_BAKED = re.compile(r"""get_encoding\('([a-z0-9_]+)'\)""")


def _sources() -> list[tuple[Path, str]]:
    return [(p, p.read_text(encoding="utf-8")) for p in SOURCE.rglob("*.py")]


def test_the_code_names_its_encodings():
    """``encoding_for_model`` picks an encoding from a model name at runtime,
    which no build step can know in advance."""
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path, text in _sources()
        if "encoding_for_model(" in text
    ]
    assert offenders == []


def test_every_encoding_the_code_loads_is_baked_into_the_image():
    loaded = {name for _, text in _sources() for name in _LOADED.findall(text)}
    # The scan has to see the loaders that exist today, or the check below
    # passes on an empty set.
    assert "cl100k_base" in loaded
    baked = set(_BAKED.findall(DOCKERFILE.read_text(encoding="utf-8")))
    assert loaded <= baked, f"not baked into the image: {sorted(loaded - baked)}"


def test_the_cache_reaches_runtime_and_is_checked_offline():
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    assert re.search(r"^ENV TIKTOKEN_CACHE_DIR=\S+", dockerfile, re.MULTILINE)
    # The build's own proof: a load through an unreachable proxy.
    assert re.search(
        r"https_proxy=http://127\.0\.0\.1:9 .*\n?.*python -c \"import tiktoken; "
        r"tiktoken\.get_encoding\('cl100k_base'\)",
        dockerfile,
    )
