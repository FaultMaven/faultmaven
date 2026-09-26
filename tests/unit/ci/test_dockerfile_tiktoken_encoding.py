"""The image carries every tiktoken encoding the code loads.

tiktoken downloads an encoding's BPE file on first use. The Dockerfile bakes the
file into the image, so a pod with no network still counts real tokens, and the
knowledge base's document preprocessor, which has no fallback, can still count
at all. That holds only while the baked set covers what the code asks for: code
that starts loading a second encoding needs a second prefetch, and nothing else
would notice.

The CI image build proves the prefetch works, because its second load runs
through an unreachable proxy. These tests pin what that build cannot see: which
encodings the code loads, that each one is in that offline check, and that the
cache directory is set before the prefetch writes to it.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import tiktoken

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCKERFILE = REPO_ROOT / "Dockerfile"
SOURCE = REPO_ROOT / "faultmaven"

#: tiktoken calls that pick an encoding from a model name at runtime.
_BY_MODEL = {"encoding_for_model", "encoding_name_for_model"}


def _trees() -> list[tuple[Path, ast.AST]]:
    return [
        (path, ast.parse(path.read_text(encoding="utf-8")))
        for path in SOURCE.rglob("*.py")
    ]


def _instructions() -> list[str]:
    """The Dockerfile's instructions, continuation lines joined, comments out."""
    text = re.sub(r"\\\n", " ", DOCKERFILE.read_text(encoding="utf-8"))
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _offline_check() -> tuple[int, str]:
    """The prefetch RUN, and the part of it that loads through the dead proxy."""
    runs = [
        (i, step)
        for i, step in enumerate(_instructions())
        if step.startswith("RUN ") and "tiktoken" in step and "127.0.0.1:9" in step
    ]
    assert len(runs) == 1, runs
    index, step = runs[0]
    return index, step.split("127.0.0.1:9", 1)[1]


def test_the_code_names_its_encodings():
    """An encoding picked from a model name at runtime is one no build step can
    know in advance."""
    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{node.lineno}"
        for path, tree in _trees()
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (getattr(node.func, "attr", None) or getattr(node.func, "id", None))
        in _BY_MODEL
    ]
    assert offenders == []


def test_every_encoding_the_code_names_is_checked_offline_in_the_image():
    """Any string literal in the code that is a tiktoken encoding name counts,
    however the call that uses it is written or wrapped."""
    # ``gpt2`` is left out: it is also a model id (the HuggingFace provider
    # scores it), so the literal alone does not mean an encoding is loaded.
    known = set(tiktoken.list_encoding_names()) - {"gpt2"}
    named = {
        node.value
        for _, tree in _trees()
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and node.value in known
    }
    # The scan has to see the loaders that exist today, or the check below
    # passes on an empty set.
    assert "cl100k_base" in named
    _, offline = _offline_check()
    checked = set(re.findall(r"get_encoding\('([a-z0-9_]+)'\)", offline))
    assert named <= checked, f"not checked offline: {sorted(named - checked)}"


def test_the_cache_directory_is_set_before_the_prefetch():
    """Set after it, the prefetch and its offline check would both use the
    system temp directory while runtime looked in the configured one."""
    instructions = _instructions()
    settings = [
        i for i, step in enumerate(instructions) if "TIKTOKEN_CACHE_DIR" in step
    ]
    prefetch, _ = _offline_check()
    env = [i for i in settings if instructions[i].startswith("ENV TIKTOKEN_CACHE_DIR=")]
    assert len(env) == 1, [instructions[i] for i in settings]
    assert env[0] < prefetch
    assert settings == env, "nothing else may set the cache directory"
