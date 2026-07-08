"""Self-contained, replaceable KB pack — shipped runbooks + build-time vectors.

A *KB pack* is a portable bundle that the app ingests at startup without loading
or running the embedding model:

    <pack>/
      pack.json                 # metadata + per-chunk text/row (see below)
      vectors.npz               # float32 vectors[total_chunks, dim], key "vectors"
      runbooks/<scope>/**/*.md   # full runbook source (knowledge_items.content)

``pack.json``::

    {
      "pack_format": 1,
      "version": "<informational, e.g. date or git sha>",
      "model": "BAAI/bge-m3",
      "dim": 1024,
      "runbooks": [
        {
          "item_id": "kb_<hex>", "content_hash": "<sha256 of full md>",
          "title": "...", "scope": "global",
          "relpath": "global/database/redis-oom.md",
          "tags": [...], "source_url": null, "owner_id": null, "team_id": null,
          "chunks": [ {"chunk_index": 0, "vector_row": 0, "text": "..."}, ... ]
        },
        ...
      ]
    }

Why self-contained? Each chunk ships its **text and vector** together, so the app
writes exactly what the pack says — it never re-chunks or re-embeds. That removes
the otherwise-fragile requirement that whoever builds the pack (the kb-toolkit)
chunk byte-identically to the app. Per-chunk *metadata* is intentionally NOT in
the pack — the app derives it from the runbook frontmatter at ingest (cheap,
app-owned), keeping the pack contract minimal: runbook content + chunk text +
vectors.

Location is configured by ``KB_PACK_DIR`` (``settings.database.kb_pack_dir``).
Empty → the baseline pack bundled in the image at ``resources/knowledge/pack``.
An override points at an external, replaceable pack so the KB can be updated
offline without rebuilding the app image.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

PACK_FORMAT = 1
BASELINE_SUBDIR = Path("resources") / "knowledge" / "pack"
PACK_JSON = "pack.json"
VECTORS_NPZ = "vectors.npz"
RUNBOOKS_SUBDIR = "runbooks"
EMBEDDING_MODEL = "BAAI/bge-m3"
EMBEDDING_DIM = 1024


@dataclass
class PackChunk:
    chunk_index: int
    text: str
    embedding: List[float]


@dataclass
class PackRunbook:
    item_id: str
    content_hash: str
    title: str
    scope: str
    relpath: str
    content: str  # full markdown (knowledge_items.content)
    tags: List[str]
    source_url: Optional[str]
    owner_id: Optional[str]
    team_id: Optional[str]
    chunks: List[PackChunk]
    # v4 per-Cause graph records (cause_letter/statement, chain_nodes/edges,
    # rung_indicators, interventions, is_fallback_cause), stored verbatim on the
    # knowledge item. Empty for older packs that predate the v4 record.
    causes: List[Dict[str, Any]] = field(default_factory=list)


def baseline_pack_dir(project_root: Path) -> Path:
    return project_root / BASELINE_SUBDIR


def resolve_pack_dir(project_root: Path, configured: str) -> Path:
    """Return the pack directory to load: the configured override, else baseline."""
    configured = (configured or "").strip()
    if configured:
        return Path(configured)
    return baseline_pack_dir(project_root)


class KbPack:
    """Read-only view over a loaded KB pack."""

    def __init__(
        self,
        runbooks: List[PackRunbook],
        model: str,
        dim: int,
        version: str,
        source_dir: Path,
    ) -> None:
        self.runbooks = runbooks
        self.model = model
        self.dim = dim
        self.version = version
        self.source_dir = source_dir

    def __len__(self) -> int:
        return len(self.runbooks)

    @property
    def item_ids(self) -> set:
        return {r.item_id for r in self.runbooks}

    @classmethod
    def load(cls, pack_dir: Path) -> Optional["KbPack"]:
        """Load a pack, or return None if absent/unreadable/incompatible.

        Never raises — a missing or corrupt pack disables shipped-runbook
        ingestion rather than crashing startup; the caller logs and continues.
        """
        pack_json = pack_dir / PACK_JSON
        vectors_npz = pack_dir / VECTORS_NPZ
        if not pack_json.exists() or not vectors_npz.exists():
            logger.info(
                "No KB pack found at %s (pack.json / vectors.npz missing) — "
                "no shipped runbooks will be ingested from a pack.",
                pack_dir,
            )
            return None

        try:
            import numpy as np

            meta = json.loads(pack_json.read_text(encoding="utf-8"))
            fmt = int(meta.get("pack_format", 0))
            if fmt != PACK_FORMAT:
                logger.warning(
                    "KB pack at %s has pack_format=%s (expected %d) — ignoring.",
                    pack_dir,
                    fmt,
                    PACK_FORMAT,
                )
                return None

            model = meta.get("model", EMBEDDING_MODEL)
            dim = int(meta.get("dim", EMBEDDING_DIM))
            version = str(meta.get("version", "unknown"))

            with np.load(vectors_npz) as npz:
                vectors = npz["vectors"]
            if vectors.ndim != 2 or vectors.shape[1] != dim:
                logger.warning(
                    "KB pack vectors shape %s does not match dim %d — ignoring.",
                    getattr(vectors, "shape", None),
                    dim,
                )
                return None

            runbooks_root = pack_dir / RUNBOOKS_SUBDIR
            runbooks: List[PackRunbook] = []
            for rb in meta.get("runbooks", []):
                relpath = rb["relpath"]
                content_path = runbooks_root / relpath
                content = content_path.read_text(encoding="utf-8")
                chunks = [
                    PackChunk(
                        chunk_index=c["chunk_index"],
                        text=c["text"],
                        embedding=vectors[c["vector_row"]].tolist(),
                    )
                    for c in rb.get("chunks", [])
                ]
                runbooks.append(
                    PackRunbook(
                        item_id=rb["item_id"],
                        content_hash=rb["content_hash"],
                        title=rb["title"],
                        scope=rb.get("scope", "global"),
                        relpath=relpath,
                        content=content,
                        tags=list(rb.get("tags") or []),
                        source_url=rb.get("source_url"),
                        owner_id=rb.get("owner_id"),
                        team_id=rb.get("team_id"),
                        chunks=chunks,
                        causes=list(rb.get("causes") or []),
                    )
                )

            logger.info(
                "Loaded KB pack from %s: %d runbooks, %d chunk vectors "
                "(model=%s, dim=%d, version=%s)",
                pack_dir,
                len(runbooks),
                vectors.shape[0],
                model,
                dim,
                version,
            )
            return cls(
                runbooks=runbooks,
                model=model,
                dim=dim,
                version=version,
                source_dir=pack_dir,
            )
        except Exception as exc:
            logger.warning(
                "Failed to load KB pack at %s (%s) — shipped runbooks will not "
                "be ingested from a pack.",
                pack_dir,
                exc,
            )
            return None
