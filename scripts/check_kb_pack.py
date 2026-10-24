#!/usr/bin/env python3
"""CI guard: the bundled KB pack is valid AND in sync with the runbook sources.

The authoritative runbook sources live (publicly, contributable) under
``resources/knowledge/runbooks/``; the KB pack under ``resources/knowledge/pack``
is the compiled artifact (built by faultmaven-kb-toolkit's ``kb-build-pack``).
This check verifies three things:

1. **Integrity** — the committed pack loads, every runbook's stored content hash
   matches its bundled markdown, and every chunk has a full-dimension vector.
2. **Freshness** — every source runbook under ``resources/knowledge/runbooks/`` is
   represented in the pack (by content hash). This catches a runbook edited or
   added without rebuilding the pack — i.e. a contributor's PR that changed a
   ``.md`` but not the pack.
3. **Pairing** (#1286) — each chunk's text is the text its vector was embedded
   from: ``text_sha256`` and ``pair_sha256`` (the producer's recipe) match the
   chunk's text and ``vectors[vector_row]``, the rows run ``0..n-1`` in manifest
   order, and ``vectors_sha256`` is the hash of the ``vectors.npz`` bytes.

Stdlib + numpy only (no model). Run: ``python scripts/check_kb_pack.py``.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Optional

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from faultmaven.bootstrap.kb_pack import (  # noqa: E402
    EMBEDDING_DIM,
    PACK_JSON,
    VECTORS_NPZ,
    KbPack,
    baseline_pack_dir,
)

RUNBOOKS_SRC = PROJECT_ROOT / "resources" / "knowledge" / "runbooks"
REBUILD = (
    "rebuild the pack with faultmaven-kb-toolkit `kb-build-pack` and re-vendor "
    "resources/knowledge/pack/ (never edit pack.json or recompute a hash)"
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pairing_problems(pack_dir: Path) -> list[str]:
    """Each chunk's text against the vector it ships beside, read from the raw files.

    Runs only on a pack ``KbPack.load`` accepted: every ``vector_row`` is an
    in-range, distinct int.
    """
    meta = json.loads((pack_dir / PACK_JSON).read_text(encoding="utf-8"))
    raw = (pack_dir / VECTORS_NPZ).read_bytes()
    with np.load(pack_dir / VECTORS_NPZ) as npz:
        vectors = npz["vectors"]
    problems = []
    if meta.get("vectors_sha256") != _sha256(raw):
        problems.append(f"vectors_sha256 != sha256({VECTORS_NPZ})")
    if meta.get("total_chunks") != len(vectors):
        problems.append(f"total_chunks != the {len(vectors)} rows of {VECTORS_NPZ}")
    rows = []
    for rb in meta["runbooks"]:
        for c in rb["chunks"]:
            where, row = f"{rb['relpath']} chunk {c['chunk_index']}", c["vector_row"]
            rows.append(row)
            missing = [k for k in ("text_sha256", "pair_sha256") if not c.get(k)]
            if missing:
                problems.append(
                    f"{where}: no {'/'.join(missing)} — rebuild with kb-toolkit "
                    "at or after a1557dd"
                )
            elif c["text_sha256"] != _sha256(c["text"].encode("utf-8")):
                problems.append(f"{where}: text_sha256 != sha256(text)")
            elif c["pair_sha256"] != _sha256(
                bytes.fromhex(c["text_sha256"])
                + np.ascontiguousarray(vectors[row], dtype="<f4").tobytes()
            ):
                problems.append(f"{where}: pair_sha256 != its text + vectors[{row}]")
    if rows != list(range(len(vectors))):
        problems.append(f"vector_row: the chunks' rows are not 0..{len(vectors) - 1}")
    return problems


def main(pack_dir: Optional[Path] = None, runbooks_src: Optional[Path] = None) -> int:
    pack_dir = pack_dir or baseline_pack_dir(PROJECT_ROOT)
    runbooks_src = runbooks_src or RUNBOOKS_SRC
    pack = KbPack.load(pack_dir)
    if pack is None:
        print(f"ERROR: no valid KB pack at {pack_dir} (missing/corrupt).")
        return 1

    problems: list[str] = []
    if len(pack) == 0:
        problems.append("pack contains 0 runbooks")

    # 1) Integrity
    pack_hashes = set()
    for rb in pack.runbooks:
        pack_hashes.add(rb.content_hash)
        actual = hashlib.sha256(rb.content.encode("utf-8")).hexdigest()
        if actual != rb.content_hash:
            problems.append(f"{rb.relpath}: content_hash != sha256(bundled md)")
        if not rb.chunks:
            problems.append(f"{rb.relpath}: 0 chunks")
        for c in rb.chunks:
            if len(c.embedding) != EMBEDDING_DIM:
                problems.append(
                    f"{rb.relpath} chunk {c.chunk_index}: dim "
                    f"{len(c.embedding)} != {EMBEDDING_DIM}"
                )
                break

    # 2) Freshness — every source runbook is in the pack. Skip non-runbook
    # markdown (README etc.): runbooks start with YAML frontmatter, docs do not.
    stale = []
    if runbooks_src.exists():
        for md in sorted(runbooks_src.rglob("*.md")):
            content = md.read_text(encoding="utf-8")
            if not content.lstrip().startswith("---"):
                continue
            if hashlib.sha256(content.encode("utf-8")).hexdigest() not in pack_hashes:
                stale.append(str(md.relative_to(runbooks_src)))
    for s in stale:
        problems.append(f"source runbook not in pack (rebuild needed): {s}")

    # 3) Pairing — the text each chunk ships is the text its vector came from.
    problems.extend(pairing_problems(pack_dir))

    if problems:
        print(f"ERROR: KB pack at {pack_dir} failed checks:")
        for p in problems[:20]:
            print(f"  - {p}")
        if len(problems) > 20:
            print(f"  … and {len(problems) - 20} more")
        print(f"  → {REBUILD}")
        return 1

    total_chunks = sum(len(r.chunks) for r in pack.runbooks)
    print(
        f"OK: KB pack valid + in sync with sources — {len(pack)} runbooks, "
        f"{total_chunks} chunks, dim {pack.dim} (version={pack.version})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
