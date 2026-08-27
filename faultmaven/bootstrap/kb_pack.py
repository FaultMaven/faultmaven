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
# The embedding model the shipped pack's vectors were produced with. Must equal
# the runtime query embedder (``model_cache.BGE_M3_MODEL_ID``); a drift-guard test
# pins the two. Kept as a local literal (not imported from model_cache) so this
# loader stays model-free — importing model_cache would pull in sentence-transformers.
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

            # Embedding-model identity guard. The pack ships build-time vectors,
            # but queries are embedded at runtime by EMBEDDING_MODEL
            # (model_cache.BGE_M3_MODEL_ID / get_bge_m3_model). If the pack was
            # built with a different embedder, its stored vectors and the runtime
            # query vectors live in different spaces and every similarity is
            # garbage — silently, because the dimension can still match. Refuse
            # such a pack (ignore it; the KB stays empty until it is rebuilt)
            # rather than fill the store with vectors that can never be correctly
            # retrieved. Only fires when the pack DECLARES a model; a model-less
            # pack (``None``) is assumed compatible — no signal to check, fail
            # open. An empty-string ``model`` is a malformed declaration and is
            # (loudly) refused, not treated as absent. Compared case-insensitively
            # so a same-model alias that only differs in case (HF ids resolve
            # case-insensitively) is not refused as if it were a different model.
            declared_model = meta.get("model")
            if (
                declared_model is not None
                and str(declared_model).strip().lower() != EMBEDDING_MODEL.lower()
            ):
                logger.error(
                    "KB pack at %s was built with embedding model %r but this app "
                    "embeds queries with %r — the vectors are incompatible (garbage "
                    "similarity). Ignoring the pack; no runbooks are ingested from "
                    "it (an already-populated KB keeps its last-good content). "
                    "Rebuild the pack with %r.",
                    pack_dir,
                    declared_model,
                    EMBEDDING_MODEL,
                    EMBEDDING_MODEL,
                )
                return None

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
            n_vectors = int(vectors.shape[0])
            # Every chunk in the pack must claim a DISTINCT vector_row. The
            # builder assigns rows as one running counter across all runbooks
            # (kb-toolkit pack_builder: ``vector_row = total_chunks + i``), so
            # the shipped invariant is a global 1:1 chunk<->vector pairing. If
            # two chunks share one row they get the SAME embedding, so at least
            # one is paired with the WRONG vector — undetectably, since the
            # lookup still succeeds and the dimension matches. The per-row
            # bounds check below cannot catch this (both rows are individually
            # in range). Track claimed rows pack-wide (across runbooks — the
            # matrix is shared) and refuse the whole pack on the first
            # collision. NOTE: this catches a SHARED row, not a swapped pair (A
            # and B exchanging two still-distinct rows keeps every row unique
            # and is unverifiable at load).
            seen_rows: Dict[int, str] = {}
            runbooks: List[PackRunbook] = []
            for rb in meta.get("runbooks", []):
                relpath = rb["relpath"]
                content_path = runbooks_root / relpath
                content = content_path.read_text(encoding="utf-8")
                chunks: List[PackChunk] = []
                for c in rb.get("chunks", []):
                    # ``vector_row`` indexes the shared vectors matrix to pair a
                    # chunk's text with its embedding. It must be a plain in-range
                    # int. A NEGATIVE row would silently WRAP under numpy indexing
                    # (e.g. -1 -> the last vector) and pair the text with the WRONG
                    # embedding — undetectably, since the lookup succeeds and the
                    # dimension still matches; an out-of-range positive row would
                    # (at best) raise deep in load and refuse the pack anyway. Guard
                    # both explicitly so the misalignment can never reach the store,
                    # and refuse the pack WHOLE (like the dim/model guards) rather
                    # than ingest silently-misaligned text<->vector pairs. ``bool``
                    # is rejected as a malformed value (it is an int subclass, so
                    # ``True`` would otherwise index row 1).
                    row = c["vector_row"]
                    if (
                        not isinstance(row, int)
                        or isinstance(row, bool)
                        or not (0 <= row < n_vectors)
                    ):
                        logger.error(
                            "KB pack at %s: runbook %r chunk_index %s has "
                            "vector_row=%r, not a valid row in the pack's %d-vector "
                            "matrix (must be an int in [0, %d)). A negative row would "
                            "silently pair the chunk with the wrong vector. Ignoring "
                            "the pack; no runbooks are ingested from it (an already-"
                            "populated KB keeps its last-good content). Rebuild the "
                            "pack.",
                            pack_dir,
                            rb.get("item_id"),
                            c.get("chunk_index"),
                            row,
                            n_vectors,
                            n_vectors,
                        )
                        return None
                    prior = seen_rows.get(row)
                    if prior is not None:
                        logger.error(
                            "KB pack at %s: vector_row=%d is claimed by two "
                            "chunks (%s, and runbook %r chunk_index %s) — two "
                            "chunks sharing one vector means at least one is "
                            "paired with the wrong embedding. Ignoring the "
                            "pack; no runbooks are ingested from it (an already-"
                            "populated KB keeps its last-good content). Rebuild "
                            "the pack.",
                            pack_dir,
                            row,
                            prior,
                            rb.get("item_id"),
                            c.get("chunk_index"),
                        )
                        return None
                    seen_rows[row] = (
                        f"runbook {rb.get('item_id')!r} "
                        f"chunk_index {c.get('chunk_index')}"
                    )
                    chunks.append(
                        PackChunk(
                            chunk_index=c["chunk_index"],
                            text=c["text"],
                            embedding=vectors[row].tolist(),
                        )
                    )
                # The tier is REQUIRED in the manifest (#1166). It used to be
                # ``rb.get("scope", "global")``, so a pack entry that simply
                # omitted the key was published to the platform corpus — the
                # tier every tenant reads — by exactly the omission this issue
                # is about. The pack is built in a different repository
                # (faultmaven-kb-toolkit), which makes the omission a
                # cross-repo one nobody reviews in the same diff. Refusing the
                # whole pack, rather than defaulting the entry, matches how
                # every other malformed-pack case here is handled: an
                # already-populated KB keeps its last-good content instead of
                # gaining a mis-tiered row.
                if not rb.get("scope"):
                    logger.error(
                        "KB pack at %s: runbook %r names no scope. The tier is "
                        "not defaulted — an omitted one used to mean 'global', "
                        "the platform corpus readable by every tenant. Ignoring "
                        "the pack; no runbooks are ingested from it (an already-"
                        "populated KB keeps its last-good content). Rebuild the "
                        "pack with an explicit scope on every runbook.",
                        pack_dir,
                        rb.get("item_id"),
                    )
                    return None
                runbooks.append(
                    PackRunbook(
                        item_id=rb["item_id"],
                        content_hash=rb["content_hash"],
                        title=rb["title"],
                        scope=rb["scope"],
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
