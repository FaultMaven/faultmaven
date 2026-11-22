"""``PackRunbook.pack_digest`` identifies a pack version by what was loaded
(#2244): the content, the pack-written fields and each chunk's index, text
and float32 vector. Fails on c21fdc5b1, which has no digest."""

from __future__ import annotations

import dataclasses
import struct
from pathlib import Path

import numpy as np
import pytest

from faultmaven.bootstrap.kb_pack import KbPack, PackChunk
from tests.unit.bootstrap.test_kb_init import RUNBOOK_MD, _write_pack

pytestmark = [pytest.mark.unit]


def _load(pack_dir: Path):
    return KbPack.load(pack_dir).runbooks[0]


def _with_vectors(pack_dir: Path, vectors: np.ndarray) -> Path:
    np.savez(pack_dir / "vectors.npz", vectors=vectors)
    return pack_dir


VECTORS = np.random.default_rng(2244).random((2, 8), dtype=np.float32)


def test_two_loads_of_one_pack_give_one_digest(tmp_path):
    pack_dir = _with_vectors(_write_pack(tmp_path), VECTORS)
    assert _load(pack_dir).pack_digest == _load(pack_dir).pack_digest


def test_the_vector_bytes_are_the_loaded_float32_rows(tmp_path):
    """A5: the bytes hashed equal ``vectors[row].astype("<f4").tobytes()``."""
    runbook = _load(_with_vectors(_write_pack(tmp_path), VECTORS))
    for chunk, row in zip(runbook.chunks, VECTORS):
        packed = struct.pack(f"<{len(chunk.embedding)}f", *chunk.embedding)
        assert packed == row.astype("<f4").tobytes()


def _changed_vector() -> np.ndarray:
    nudged = VECTORS.copy()
    nudged[1, 3] = np.nextafter(nudged[1, 3], np.float32(2))  # one ulp
    return nudged


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"content": RUNBOOK_MD + "\nMore.\n"}, id="content"),
        pytest.param({"title": "Another title"}, id="title"),
        pytest.param({"chunk_texts": ("section one", "section 2")}, id="chunk-text"),
        pytest.param({"chunk_texts": ("section two", "section one")}, id="order"),
        pytest.param({"chunk_texts": ("a", "b", "c")}, id="count"),
        pytest.param({"vectors": _changed_vector()}, id="vector"),
    ],
)
def test_the_digest_moves_with_each_loaded_input(tmp_path, change):
    base = _load(_with_vectors(_write_pack(tmp_path / "base"), VECTORS))
    vectors = change.pop("vectors", VECTORS)
    pack_dir = _write_pack(tmp_path / "changed", **change)
    if len(change.get("chunk_texts", "ab")) == 3:
        vectors = np.vstack([VECTORS, VECTORS[:1]])
    _with_vectors(pack_dir, vectors)
    assert _load(pack_dir).pack_digest != base.pack_digest


@pytest.mark.parametrize(
    "field",
    [
        {"tags": ["postgres"]},
        {"source_url": "https://example.invalid/runbook"},
        {"owner_id": "user-1"},
        {"chunks": [PackChunk(chunk_index=0, text="section one", embedding=[0.0])]},
    ],
    ids=["tags", "source_url", "owner_id", "chunks"],
)
def test_the_digest_moves_with_each_manifest_field(tmp_path, field):
    base = _load(_write_pack(tmp_path))
    assert dataclasses.replace(base, **field).pack_digest != base.pack_digest
    # The manifest's own content_hash is stamped beside it, not in it.
    assert dataclasses.replace(base, content_hash="x").pack_digest == base.pack_digest
