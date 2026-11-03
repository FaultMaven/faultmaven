"""A real composition's ChromaDB stores are released when the container shuts down.

chromadb keeps a local client's System (SQLite pool, HNSW segment files, Rust
runtime) in a process-wide cache until the client is closed. Before shutdown
closed the container's clients, every composition over a store left it open for
the rest of the process; each test here that composed over its own ``tmp_path``
left two. ``close()`` releases the segment files at once, and the SQLite handles
when the closed client is garbage-collected, so the check runs after a
collection.
"""

from __future__ import annotations

import gc
import os
import sys

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc/self/fd"),
]


def _held_under(root: str) -> list[str]:
    held = []
    for fd in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            continue
        if target.startswith(root):
            held.append(target)
    return held


async def test_a_composition_holds_no_store_file_once_shut_down(
    service_checks_on, fresh_container, tmp_path
):
    await fresh_container.initialize()
    assert fresh_container.kb_chromadb_client is not None
    assert fresh_container.evidence_chromadb_client is not None
    # Positive control: both stores are open under this test's tmp_path.
    assert _held_under(str(tmp_path / "chroma-kb"))
    assert _held_under(str(tmp_path / "chroma-evidence"))

    await fresh_container.shutdown()
    gc.collect()

    assert _held_under(str(tmp_path / "chroma-kb")) == []
    assert _held_under(str(tmp_path / "chroma-evidence")) == []


async def test_the_kb_store_is_released_when_the_evidence_client_cannot_be_built(
    service_checks_on, fresh_container, tmp_path, monkeypatch
):
    """The KB client is built first. Were it set on the container only after the
    evidence client, a failure building that one would leave the KB client
    where shutdown never reaches it."""
    from faultmaven.container.providers import infrastructure

    def no_evidence_store(settings):
        raise OSError("the evidence store cannot be opened")

    monkeypatch.setattr(
        infrastructure, "create_evidence_chromadb_client", no_evidence_store
    )

    await fresh_container.initialize(allow_degraded=True)
    assert _held_under(str(tmp_path / "chroma-kb"))  # positive control

    await fresh_container.shutdown()
    gc.collect()

    assert _held_under(str(tmp_path / "chroma-kb")) == []
