#!/usr/bin/env python3
"""CI guard: the bundled KB pack is valid AND in sync with the runbook sources.

The authoritative runbook sources live (publicly, contributable) under
``resources/knowledge/runbooks/``; the KB pack under ``resources/knowledge/pack``
is the compiled artifact (built by faultmaven-kb-toolkit's ``kb-build-pack``).
This check verifies two things:

1. **Integrity** — the committed pack loads, every runbook's stored content hash
   matches its bundled markdown, and every chunk has a full-dimension vector.
2. **Freshness** — every source runbook under ``resources/knowledge/runbooks/`` is
   represented in the pack (by content hash). This catches a runbook edited or
   added without rebuilding the pack — i.e. a contributor's PR that changed a
   ``.md`` but not the pack.

Stdlib + numpy only (no model). Run: ``python scripts/check_kb_pack.py``.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from faultmaven.bootstrap.kb_pack import (  # noqa: E402
    EMBEDDING_DIM,
    KbPack,
    baseline_pack_dir,
)

RUNBOOKS_SRC = PROJECT_ROOT / "resources" / "knowledge" / "runbooks"


def main() -> int:
    pack_dir = baseline_pack_dir(PROJECT_ROOT)
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
    if RUNBOOKS_SRC.exists():
        for md in sorted(RUNBOOKS_SRC.rglob("*.md")):
            content = md.read_text(encoding="utf-8")
            if not content.lstrip().startswith("---"):
                continue
            if hashlib.sha256(content.encode("utf-8")).hexdigest() not in pack_hashes:
                stale.append(str(md.relative_to(RUNBOOKS_SRC)))
    for s in stale:
        problems.append(f"source runbook not in pack (rebuild needed): {s}")

    if problems:
        print(f"ERROR: KB pack at {pack_dir} failed checks:")
        for p in problems[:20]:
            print(f"  - {p}")
        if stale:
            print(
                "  → rebuild the pack: in faultmaven-kb-toolkit run "
                "`kb-build-pack` and re-vendor resources/knowledge/pack/"
            )
        return 1

    total_chunks = sum(len(r.chunks) for r in pack.runbooks)
    print(
        f"OK: KB pack valid + in sync with sources — {len(pack)} runbooks, "
        f"{total_chunks} chunks, dim {pack.dim} (version={pack.version})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
