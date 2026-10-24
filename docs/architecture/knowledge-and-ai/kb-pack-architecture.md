# KB Pack Architecture

**Document Type:** Component Specification
**Version:** 1.0
**Last Updated:** 2026-10-14

---

## Purpose

This document specifies the **KB pack**: the self-contained, replaceable artifact
that ships the built-in runbooks **and** their pre-computed embeddings, so the
FaultMaven API can populate its Knowledge Base at startup in seconds without
running an embedding model.

It covers the pack format, the `KB_PACK_DIR` configuration contract, how the pack
is built (by `faultmaven-kb-toolkit`), and how it is delivered to each deployment
mode. For how the pack is *ingested* into `knowledge_items` + ChromaDB, see
[`kb-ingestion-architecture.md`](./kb-ingestion-architecture.md). For the chunking
strategy and retrieval mechanics, see
[`vector-retrieval-architecture.md`](./vector-retrieval-architecture.md).

---

## Why a pack? (the problem it solves)

The KB contains 91 built-in runbooks → ~1319 structure-aware chunks. Embedding
those with BGE-M3 on a CPU-limited pod takes **~50–85 minutes** — and it is
genuinely CPU-FLOPs-bound, so no thread/batch tuning gets it under a startup
window. The original design ran that embedding **inside the FastAPI lifespan,
before the port served**, so the pod never became Ready and the rollout rolled
back (the KB stayed nearly empty).

Embedding is deterministic, so the fix is to do it **once at build time** and
ship the vectors. First-boot ingestion then writes pre-computed vectors straight
to ChromaDB — no chunking, no model — and completes in seconds. (The BGE-M3 model
itself still loads once at boot, in ~5s, from the cache **baked into the image** —
CPU-only torch, `HF_HUB_OFFLINE`, so no HuggingFace fetch — for query-time search;
*ingestion* just doesn't use it.) (Investigation trail:
`docs/working/ANALYSIS-kb-ingestion-perf.md`.)

---

## Pack format

A pack is a directory (transported as `kb-pack-<version>.tar.gz`):

```text
<pack>/
  pack.json                   # manifest: metadata + per-chunk text/vector-row
  vectors.npz                 # float32 array vectors[total_chunks, 1024], key "vectors"
  runbooks/<scope>/**/*.md     # full runbook source (knowledge_items.content)
```

`pack.json`:

```json
{
  "pack_format": 1,
  "version": "<informational, e.g. a date or git sha>",
  "model": "BAAI/bge-m3",
  "dim": 1024,
  "total_chunks": 1319,
  "vectors_sha256": "<sha256 of the vectors.npz bytes>",
  "runbooks": [
    {
      "item_id": "kb_<12 hex>",
      "content_hash": "<sha256 of the full markdown>",
      "title": "...", "scope": "global",
      "relpath": "global/networking/k8s-coredns-failures.md",
      "tags": [...], "source_url": null, "owner_id": null, "team_id": null,
      "chunks": [ {"chunk_index": 0, "vector_row": 0, "text": "...",
                   "text_sha256": "<hex>", "pair_sha256": "<hex>"}, ... ],
      "causes": [ {"cause_letter": "A", "cause_name": "...", "cause_statement": "...",
                   "chain_nodes": [...], "chain_edges": [...], "rung_indicators": {...},
                   "interventions": [...], "is_fallback_cause": false}, ... ]
    }
  ]
}
```

### Why self-contained (text **and** vector per chunk)

Each chunk ships its **text and its vector together**. The app writes exactly what
the pack says — it never re-chunks and never re-embeds shipped runbooks. This
removes what would otherwise be a fragile cross-repo coupling: the pack builder
(in the toolkit) would have to chunk byte-identically to the app forever. Because
the chunk text is in the pack, the builder's chunker only has to be *reasonable*,
not *identical*. (In practice the toolkit uses the same structure-aware strategy,
so chunks match the app exactly — but nothing breaks if they diverge.)

Per-chunk **metadata** is intentionally *not* in the pack — the app derives it
from the runbook frontmatter at ingest (cheap, app-owned), keeping the pack
contract: runbook content + chunk text + vectors (the per-Cause `causes` record
the toolkit also ships is ignored by the app since fm#1295 — below).

### Integrity guards (load- and ingest-time)

Because the pack ships pre-computed vectors and explicit chunk indices, four
silent-corruption failure modes are guarded rather than left to chance:

- **Embedding-model identity** (`KbPack.load`). The pack ships build-time
  vectors, but queries are embedded at runtime by the app's `BAAI/bge-m3`
  (`model_cache.BGE_M3_MODEL_ID` / `get_bge_m3_model`). A pack built with a
  *different* embedder has vectors in a different space, so every similarity is
  garbage — and the `dim` check alone does not catch it (a different model can
  share the 1024-d shape). The loader compares the pack's declared `model`
  against `EMBEDDING_MODEL` (case-insensitively — HF ids resolve so) and, on a
  mismatch, logs a loud `ERROR` and **refuses the pack** (returns `None`, same as
  a `dim`/format mismatch) rather than filling the store with vectors that can
  never be correctly retrieved. Refusal is *before* `_prune_orphan_builtins` /
  `_reconcile_vectors`, so an **already-populated KB keeps its last-good
  content** — only a fresh install is left empty. A `model`-less pack is assumed
  compatible (no signal to check — fail open); an empty-string `model` is a
  malformed declaration and is refused. `kb_pack.EMBEDDING_MODEL` keeps its own
  copy of the id (so the loader stays model-free — importing `model_cache` would
  drag in sentence-transformers) and is pinned to `BGE_M3_MODEL_ID` by a
  drift-guard test.

- **Chunk integrity** (`_ingest_pack_runbook`). Each chunk carries an explicit
  `chunk_index`, but the ingest path re-derives the chunk index and chunk id by
  list position (`enumerate`). The ingest step, *before* any lookup or re-ingest
  delete, rejects a runbook with (a) **no chunks** (never retrievable) or (b) a
  `chunk_index` list that is not canonical `0..n-1` in list order (out of order /
  gaps / duplicates / non-zero start would silently misalign ids from the
  manifest). Validating before the delete is deliberate: a malformed pack
  *update* must not delete a previously-good row and leave nothing behind. The
  per-runbook loop isolates the failure to that one runbook (recorded in
  `BootstrapResult.failed`) and the rest of the pack still ingests. (Per-chunk
  `vector_row` is honoured directly at load — bounds-checked below — so
  text↔vector pairing is unaffected by list order; only the derived index/id is
  guarded here.)

- **`vector_row` bounds** (`KbPack.load`). Each chunk names the `vector_row` that
  pairs its text with an embedding in the shared `vectors` matrix. A **negative**
  row is the silent hazard: numpy indexing *wraps* it (`-1` → the last vector), so
  the chunk's text would be stored against the **wrong** embedding — undetectably,
  because the lookup succeeds and the dimension still matches (Phase-6's exact
  target class). The loader requires every `vector_row` to be a plain int in
  `[0, n_vectors)` (`bool` rejected — it is an `int` subclass, so `True` would
  index row 1); any violation logs a loud `ERROR` and **refuses the pack whole**
  (returns `None`, same fail-safe as the `dim`/model guards), so the misaligned
  pair can never reach the store and an already-populated KB keeps its last-good
  content. (An out-of-range *positive* row would at worst raise deep in load and
  refuse the pack anyway; the explicit guard turns both into one targeted refusal.)

- **`vector_row` uniqueness** (`KbPack.load`). The bounds check above accepts
  each row on its own; it cannot see that **two** chunks name the *same* valid
  row. The builder assigns rows as a single running counter across all runbooks
  (`pack_builder`: `vector_row = total_chunks + i`), so the shipped invariant is
  a global 1:1 chunk↔vector pairing. If two chunks share one row they receive
  the **same** embedding, so at least one is paired with the **wrong** vector —
  undetectably (the lookup succeeds and the dimension matches). The loader tracks
  claimed rows *pack-wide* (across runbooks — the `vectors` matrix is shared) and
  on the first collision logs a loud `ERROR` and **refuses the pack whole**
  (returns `None`, same fail-safe as the bounds/`dim`/model guards). This catches
  a **shared** row; it does not catch a *swapped pair* (two chunks exchanging two
  still-distinct rows keeps every row unique and is unverifiable at load). In the
  vendored pack (`resources/knowledge/pack/`), the swapped pair, a stale vector, a
  dropped chunk and a substituted `vectors.npz` are caught before they ship, by the
  hashes and row order the CI gate checks (below). The gate checks only that pack:
  a pack read from `KB_PACK_DIR` is not checked at load.

### Per-Cause `causes` record (toolkit-emitted, ignored by the app)

Each runbook entry also ships a **`causes`** array — one record per `### Cause`
in the runbook's `## Causes` section, produced by the toolkit's
`pack_builder._extract_causes`. Its only runtime reader was the KB cause seeder,
removed in fm#1295; since then the app's pack loader (`bootstrap/kb_pack.py`)
does not read the field and nothing is persisted from it. The toolkit keeps
emitting it (its own contract test pins the shape); whether to stop is a
toolkit decision. Retrieval serves the runbook *text* as RAG context — the
`## Causes` section chunks one Cause per chunk, so each retrieved Cause is a
self-contained cause→fix unit. See
[runbook-content-architecture.md](./runbook-content-architecture.md) for the
authoring grammar.

**The cause authoring grammar is a cross-repo contract.** The toolkit grammar
source (`kb_toolkit/core/runbook_grammar.py` + `config.py`) and the backend
mirror (`modules/knowledge/domain/services/cause_grammar.py`, referenced by
`runbook_validator.py`) live in different repos and share no importable module,
so the vocabulary is a **manual mirror** kept honest by each repo's
frozen-literal drift-guard test plus the kb-toolkit `golden-cross-repo` CI job
(`scripts/check_vocab_cross_repo.py`), which mechanically asserts the two
repos' vocabularies are equal.

### Identity & idempotency keys

- `item_id` = `kb_<sha256(frontmatter id)[:12]>` — must match
  `faultmaven.utils.runbook_id.item_id_from_runbook_id` so the app recognises
  these as built-ins (for the orphan-prune and delete semantics).
- `content_hash` = SHA-256 of the full markdown — the app's idempotency key.
  Ingest stamps it on the row (`metadata.pack_content_hash`), and a later
  bootstrap skips the runbook while the pack's hash still equals the stamp, so
  an operator's edit of the row is never read as a new pack version (#1966).

---

## Configuration: `KB_PACK_DIR`

The app reads the pack from a single configurable filesystem path
(`settings.database.kb_pack_dir`, env `KB_PACK_DIR`):

| `KB_PACK_DIR` | Behaviour |
|---------------|-----------|
| **unset (default)** | Use the **baseline pack bundled in the image** at `resources/knowledge/pack`. Zero-config; works on every deployment. |
| **set** | Load the pack from that directory instead — an external, **replaceable** pack. |

A set `KB_PACK_DIR` always wins and there is no fallback to the baseline
(`resolve_pack_dir`): a directory that is empty, missing or invalid ingests nothing
(a `KB bootstrap: no pack loaded` warning), and a deployment that once used it keeps
reading that directory after moving to an image-borne pack until it is unset.

This one abstraction is the whole "same core, different configuration" story: the
app only ever reads a path; *how the pack arrives there* is a per-deployment
concern (see [Delivery](#delivery)). Loader: `faultmaven/bootstrap/kb_pack.py`
(`KbPack.load`); it fails soft (logs + returns `None`) on a missing/corrupt pack
rather than crashing startup.

---

## Source of truth & ownership

The **authoritative runbook sources live in this (public) repo** at
`resources/knowledge/runbooks/<domain>/*.md` — on purpose, for transparency and
community contribution (anyone can read them and open PRs; see that directory's
`README.md`). The **build** is owned by `faultmaven-kb-toolkit` (`kb-build-pack`),
which reads those public sources and produces the pack. (The toolkit's
`data/runbooks/` is a symlink to the public sources, so there is a single physical
copy and no drift.) The app *consumes* a vendored baseline snapshot of the pack.

- Producer: `kb-build-pack` (toolkit) → `kb_toolkit/core/pack_builder.py`.
- Chunking: **structure-aware**, matching the documented KB design (split on
  `##`/`###` headers + horizontal rules, 100–3000 char variable chunks — see
  [`vector-retrieval-architecture.md`](./vector-retrieval-architecture.md)
  §Chunking Strategy). The toolkit ports the app's `ContentChunker` for this
  (`kb_toolkit/core/structure_chunker.py`); its general-purpose fixed-size
  `DocumentChunker` is **not** used for the pack.
- Freshness gate: `scripts/check_kb_pack.py` (this repo, pre-commit + CI) verifies
  the committed pack is structurally valid **and in sync with the sources** under
  `resources/knowledge/runbooks/` — so a PR that edits a runbook without rebuilding
  the pack fails CI. (`kb-build-pack --check` does the same on the toolkit side.)
  It also checks that each chunk's text is the text its vector was embedded from
  (#1286): per chunk, `text_sha256` (of the text) and `pair_sha256` (of that hash
  plus the `vectors[vector_row]` bytes, little-endian float32); in the manifest,
  `vectors_sha256` (of the `vectors.npz` bytes); and rows running `0..n-1` in
  manifest order. A failure names the chunk or the field; the remedy is a rebuild,
  never an edited `pack.json` or a recomputed hash.
- Thread cap: the build bounds embedding threads (`--threads`, default 8) so a
  single bandwidth-bound build can't oversubscribe a shared host.

Full producer-side docs:
`faultmaven-kb-toolkit/docs/BUILDING-THE-KB-PACK.md`.

### Vendoring the baseline

`resources/knowledge/pack/` in this repo is a committed snapshot built from the
public sources in `resources/knowledge/runbooks/`. It ships in the image
(`Dockerfile` `COPY resources/`) as the zero-config default. To refresh it after a
runbook change: `kb-build-pack` in the toolkit (it reads the public sources) and
copy the result into `resources/knowledge/pack/`, then commit. CI's
`check_kb_pack.py` fails if the two ever drift.

A vendored baseline is **baked into the image**, so it is live only in an image
that contains it: the default stack runs the published GHCR image and mounts no
`resources/` directory, so merging a pack change or editing the checkout does not
change a running deployment (see [Delivery](#delivery) for what does).

---

## Delivery

Local and single-tenant Kubernetes default to the bundled baseline and make the
external override **opt-in**, so nothing changes until an operator asks for it.
Multi-tenant Kubernetes has no override: the pack the `kb_seed` Job ingests is the
one baked into its image.

### Local / self-hosted (`docker-compose.yml`)

Three ways to get a new pack live, all ending in the startup bootstrap ingesting it:

- **A pack in an image, from source:** vendor the pack into `resources/knowledge/pack/`
  of the checkout, then, with `KB_PACK_DIR` unset (re-comment the two compose lines
  if you set them), `./faultmaven.sh stop && ./faultmaven.sh start --build`.
  `./faultmaven.sh restart` alone recreates the container from the image already
  present, which still holds the old pack. The build is tagged
  `ghcr.io/faultmaven/faultmaven:latest`, so a later plain `start` (or a `restart`
  issued while the stack is stopped) pulls `:latest` over it and reverts to the
  published pack; keep starting with `--build`, or set `FM_IMAGE_TAG=local` and pin
  `FM_DASHBOARD_IMAGE_TAG` in `.env` (`start` pulls only while one of the two tags
  is `latest`).
- **A newer published image:** with `KB_PACK_DIR` unset,
  `./faultmaven.sh stop && ./faultmaven.sh start --pull` (or pin `FM_IMAGE_TAG`).
- **No rebuild (`KB_PACK_DIR`):** a commented `KB_PACK_DIR=/kb-pack` env and a
  `./kb-pack:/kb-pack:ro` bind-mount on the `api` service. In the directory that
  holds `docker-compose.yml`, extract the tarball so `pack.json` + `vectors.npz` +
  `runbooks/` sit directly in `./kb-pack`
  (`mkdir -p kb-pack && tar xzf /path/to/kb-pack-<version>.tar.gz -C kb-pack --strip-components=1`
  — the tarball root is `kb-pack/`), uncomment both lines, and run
  `./faultmaven.sh restart` (it recreates the container, so the compose edit applies).
  Unset → bundled baseline.

### Cloud / Kubernetes, `TENANT_PROVIDER=single` (`faultmaven-enterprise-infra`)

An opt-in strategic-merge patch
(`kubernetes/apps/faultmaven/overlays/onprem/kb-pack-from-minio.yaml`) adds an
`initContainer` (`minio/mc`) that downloads
`kb-packs/kb-pack-${KB_PACK_VERSION}.tar.gz` from MinIO into an `emptyDir` mounted
at `KB_PACK_DIR=/kb-pack`. `KB_PACK_VERSION` is a (default-empty) ConfigMap key.
Operator workflow + verification: `docs/operations/onprem-app-deployment.md` in
that repo, § "Deploying an updated pack". A new API image carrying a vendored
baseline is the other route, but only with the patch removed from the overlay: the
patch sets `KB_PACK_DIR=/kb-pack`, which keeps winning over the image's pack.

### Cloud / Kubernetes, `TENANT_PROVIDER=multi`

The API **skips its startup KB bootstrap** (the tenant-bound app role cannot write
the org-free global rows; it logs `KB bootstrap skipped under multi-tenancy`), so
`KB_PACK_DIR` has no effect, and the MinIO patch only adds a failure: its
initContainer exits non-zero while `KB_PACK_VERSION` is empty, so the API pod does
not start. The pack is ingested by the `kb_seed` Job
(`python -m faultmaven.jobs.run kb_seed --cross-tenant-maintenance`; manifest
`kubernetes/apps/faultmaven/jobs/kb-seed.yaml` in `faultmaven-enterprise-infra`,
also run by CD after each deploy), which runs the same `bootstrap_kb` against the
pack baked into **its own image**; run it with the image the API runs, because the
manifest's pinned image is stale. Its log carries `KB seed completed:
BootstrapResult(...)`. A new pack therefore reaches a multi deployment only in a
new image.

### Update workflow

1. Edit runbooks in `resources/knowledge/runbooks/` → in the toolkit,
   `kb-build-pack --version <v> --tar`.
2. **Local, no image rebuild:** extract the pack into the `KB_PACK_DIR` directory
   (`--strip-components=1`), `./faultmaven.sh restart`.
   **Kubernetes `single`, no image rebuild:** upload the tarball to MinIO
   `kb-packs`, set `KB_PACK_VERSION=<v>`, roll out.
   **Anywhere the pack is vendored** (and always under `multi`): build and deploy a
   new image, with `KB_PACK_DIR` unset (and the MinIO patch removed) so the
   image's pack is the one read.
3. The bootstrap's idempotent pack-hash ingest + orphan prune add new
   runbooks, re-ingest the ones whose pack version changed (republishing one an
   operator had unpublished — a shipped update is a new document), and remove
   deleted ones. The
   prune is keyed on the built-in `kb_<12 hex>` id: a pack replaces the built-in
   set rather than adding to it, and authored runbooks are never touched.

---

## Key files

| Concern | Location |
|---------|----------|
| **Runbook sources (authoritative, public, PR-able)** | `faultmaven/resources/knowledge/runbooks/<domain>/*.md` |
| Pack format + loader | `faultmaven/bootstrap/kb_pack.py` |
| Bootstrap ingestion from pack | `faultmaven/bootstrap/kb_init.py` |
| `KB_PACK_DIR` setting | `faultmaven/config/settings.py` (`DatabaseSettings.kb_pack_dir`) |
| App-side check (integrity + source↔pack freshness + text↔vector pairing) | `scripts/check_kb_pack.py` |
| Bundled baseline pack (generated artifact) | `faultmaven/resources/knowledge/pack/` |
| Local delivery | `faultmaven/docker-compose.yml` |
| Pack builder + CLI | `faultmaven-kb-toolkit` → `kb_toolkit/core/pack_builder.py`, `cli/kb_build_pack.py` |
| Structure-aware chunker | `faultmaven-kb-toolkit/kb_toolkit/core/structure_chunker.py` |
| Cloud delivery, `single` (opt-in) | `faultmaven-enterprise-infra` → `kubernetes/apps/faultmaven/overlays/onprem/kb-pack-from-minio.yaml`; operator workflow in `docs/operations/onprem-app-deployment.md` |
| Cloud seeding, `multi` | `faultmaven/jobs/kb_seed.py` + `faultmaven-enterprise-infra` → `kubernetes/apps/faultmaven/jobs/kb-seed.yaml` |
