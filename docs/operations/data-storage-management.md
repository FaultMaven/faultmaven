# Data and Storage Management

Operations guide for managing FaultMaven's data directory at the OS level.

Covers the physical file layout, what each file stores, and common tasks including managing knowledge base runbooks without the Dashboard UI.

---

## Data Directory Layout

All runtime data lives under `data/` relative to the project root. This directory is gitignored.

```
data/
├── faultmaven.db              # SQLite — all relational data (37 tables; see er-diagram.md)
│
├── chroma-kb/                 # ChromaDB instance — permanent KB vectors
│   ├── chroma.sqlite3         #   Collection metadata, doc IDs, text, full-text index
│   └── <uuid>/                #   HNSW vector index (one folder per collection)
│       ├── data_level0.bin    #   One collection: faultmaven_kb (KB documents
│       ├── header.bin         #   AND runbooks — see "Expected collections")
│       ├── length.bin
│       └── link_lists.bin
│
├── chroma-evidence/           # ChromaDB instance — ephemeral case evidence vectors
│   ├── chroma.sqlite3         #   Same structure as chroma-kb/
│   └── <uuid>/                #   Collections: case_{case_id} (dynamic, per-case)
│       └── ...                #   Created on evidence upload, deleted on case closure
│
├── evidence/                  # Raw uploaded files (not vectors)
│   └── <organization_id>/     #   Organized by organization (tenant isolation)
│       └── <case_id>/         #     then by case
│           └── <YYYY-MM-DD>/  #       then by upload date
│               └── <uuid>_<filename>  # UUID-prefixed to prevent collisions
│
└── knowledge/                 # Standalone scan import directory: hand-placed .md files only.
    │                          #   Nothing writes here and no startup step reads it.
    ├── global/                #   Imported as global-scope drafts (platform operator only)
    ├── team_<team_id>/        #   Imported as personal drafts (no teams on a standalone deployment); the scan's `notes` say so
    └── personal_<user_id>/    #   Imported as personal drafts (`user_<user_id>/` works too)
```

The runbooks themselves are not on disk. The built-ins ship in the KB pack (`resources/knowledge/pack/` or `KB_PACK_DIR`, not under `data/`); authored, uploaded and converted runbooks live in the database, as draft rows (`conversion_drafts.content`) and, once verified, `knowledge_items` rows (#1950).

### What stores what

| Physical Path | Storage Technology | Logical Data | Lifecycle |
| --- | --- | --- | --- |
| `faultmaven.db` | SQLite | Cases, users, evidence metadata, hypotheses, solutions, messages, RBAC, audit logs, conversion jobs/drafts (with the draft text), knowledge items (with the published runbook text) | Application-managed |
| `chroma-kb/` | ChromaDB (permanent) | KB embeddings: one `faultmaven_kb` collection holding KB documents and runbooks alike. Backed up, never wiped. | Permanent |
| `chroma-evidence/` | ChromaDB (ephemeral) | Case evidence embeddings: `case_{case_id}` collections (one per active case). Excluded from backups, safe to wipe. | Per-case lifecycle |
| `evidence/<organization_id>/<case_id>/<YYYY-MM-DD>/` | Filesystem | Raw uploaded files (logs, configs, CSVs, PDFs). Not vectors — original files only. UUID-prefixed filenames prevent collisions. | 90-day retention |
| `resources/knowledge/pack/` | Image / `KB_PACK_DIR` | The **KB pack** — pre-deployed runbooks + build-time vectors. Ingested at startup with no embedding model; built by `faultmaven-kb-toolkit`. See [kb-pack-architecture.md](../architecture/knowledge-and-ai/kb-pack-architecture.md). | Shipped |
| `knowledge/` | Filesystem (standalone only) | The **import directory** for `POST /api/v1/knowledge/scan`: each hand-placed `.md` file becomes a draft whose text is stored in `conversion_drafts.content`. Not runtime storage: nothing writes here, no startup step reads it, and the scan refuses (422) under `TENANT_PROVIDER=multi`. | Manual file drop |

### Why two ChromaDB instances

KB data and case evidence have fundamentally different lifecycles:

- **KB** is permanent, curated, and valuable — it must be backed up and protected from corruption.
- **Case evidence vectors** are ephemeral, created on upload, deleted on case closure, and fully reconstructable from `data/evidence/` raw files.

Separating them into two ChromaDB instances (`chroma-kb/` and `chroma-evidence/`) ensures:
- KB backups exclude disposable evidence data
- Evidence churn (creation/deletion of per-case collections) cannot fragment or corrupt the KB store
- Either instance can be rebuilt independently without affecting the other
- The corruption blast radius of either instance is contained

### Relationship between the KB pack, knowledge/, evidence/, and the two ChromaDB instances

```text
KB pack (resources/knowledge/pack/ or KB_PACK_DIR)  →  startup bootstrap (idempotent)  →  body: knowledge_items; chunk texts + vectors: chroma-kb/ (faultmaven_kb collection)
Dashboard / API conversion  →  draft row  →  verify  →  body: knowledge_items; chunks: chroma-kb/
Dashboard / API upload  →  draft row verified on the spot  →  body: knowledge_items; chunks: chroma-kb/
knowledge/*.md  →  POST /knowledge/scan (standalone, manual)  →  draft row  →  verify  →  body: knowledge_items; chunks: chroma-kb/

evidence/<organization_id>/<case_id>/<date>/<uuid>_<file>  →  background vectorization  →  chroma-evidence/ (case_{id} collection)
```

- `resources/knowledge/pack/` (or `KB_PACK_DIR`) holds the **KB pack** — pre-deployed runbooks + build-time vectors. The startup bootstrap (`faultmaven/bootstrap/kb_init.py`) ingests it with **no embedding model**: it writes each runbook's body to its `knowledge_items` row and the pack's chunk texts + pre-computed vectors to `chroma-kb/`. A chunk is served only while its row is complete and published (#2397). Content-hash idempotency makes restarts free for unchanged runbooks; removed runbooks are pruned. The pack is built/owned by `faultmaven-kb-toolkit` — see [`kb-pack-architecture.md`](../architecture/knowledge-and-ai/kb-pack-architecture.md).
- `knowledge/` is the **standalone import directory** `/knowledge/scan` reads: hand-placed runbook files become drafts holding their text. Nothing writes here; authored and converted drafts live in `conversion_drafts.content` (#1950), and the pre-deployed built-ins come from the pack. See [`document-to-runbook-conversion.md`](../architecture/knowledge-and-ai/document-to-runbook-conversion.md) §9.5.
- `evidence/` holds **raw uploaded files**. After the upload API returns a response, a background task vectorizes the content into a `case_{case_id}` collection in `chroma-evidence/`.
- Each ChromaDB instance is independent — they share no files.

Nothing reads `knowledge/` after a scan, so deleting a file there changes nothing already imported: the draft the scan made from it keeps its row and its text. Use the Dashboard or API to remove a runbook (see "Removing a runbook" below) and `fm-reset-kb` to wipe the KB.

---

## Managing Knowledge Base Runbooks (Without UI)

### Adding / updating pre-deployed runbooks (KB pack)

Pre-deployed runbooks ship in the **KB pack** (pre-chunked + pre-embedded), built
and owned by `faultmaven-kb-toolkit`. The startup bootstrap ingests the pack with
**no embedding model** — it does **not** walk `data/knowledge/{scope}/`. To add or
update a pre-deployed runbook you rebuild the pack and deliver it; whether that
needs a new app image depends on how the pack reaches the process (Step 3). This
bypasses the Dashboard Drafts UI entirely — that UI is reserved for case-generated
and document-converted drafts that need human review.

**Step 1: Author / edit the runbook (public source)**

Add or edit `.md` files under `resources/knowledge/runbooks/<domain>/` in this
repo — the authoritative, public, PR-able source. (The toolkit's authoring/validation
tools — `kb-validate`, `kb-quality`, `kb-init`, `kb-researcher` — operate on these
files; the toolkit's `data/runbooks/` is a symlink to this directory.) Runbooks
require YAML frontmatter — `id` and `title` are mandatory; the deterministic
`knowledge_items.item_id` is derived from `id`.

**Step 2: Build the pack**

```bash
# In faultmaven-kb-toolkit:
kb-build-pack --version 2026-06-09 --tar   # → dist/kb-pack + dist/kb-pack-2026-06-09.tar.gz
```

The pack **replaces** the built-in set, it does not add to it: on every startup the
bootstrap prunes built-in rows (`kb_<12 hex>` ids) that the pack lacks, so build the
pack from the complete source tree. Authored runbooks (UUID or `kb_<16 hex>` ids) are never pruned.

**Step 3: Deliver it and make it live.** A pack goes live only where the process
that ingests it can read it. The stock stack bakes `resources/` into the image
(`Dockerfile`: `COPY resources/ ./resources/`) and `docker-compose.yml` mounts only
`./data` and `.env` into the API, so copying a pack into a checkout and restarting
the stock image changes nothing. Also, `KB_PACK_DIR` wins whenever it is set
(`resolve_pack_dir` has no fallback to the baseline): a deployment that once used a
`KB_PACK_DIR` row below and later moves to an image row keeps ingesting the old
directory until `KB_PACK_DIR` is unset. Pick the row that matches the deployment:

| Deployment | Deliver | Make it live |
| --- | --- | --- |
| Docker Compose, built from source | In `faultmaven-kb-toolkit/` (where Step 2 wrote `dist/`): `cp -r dist/kb-pack/* /path/to/faultmaven/resources/knowledge/pack/` | With `KB_PACK_DIR` unset (re-comment the two compose lines below if you set them): `./faultmaven.sh stop && ./faultmaven.sh start --build` — the build copies the vendored pack into the image. `./faultmaven.sh restart` alone recreates the container from the image already present, which still holds the old pack, and `start --build` on a running stack only reports "already running". **Tag caveat:** the build is tagged `ghcr.io/faultmaven/faultmaven:latest`, and a later plain `start` (or a `restart` issued while the stack is stopped, which runs `start`) pulls `:latest` over it, reverting changed runbooks and pruning added ones. Keep starting with `--build`, or set `FM_IMAGE_TAG=local` and pin `FM_DASHBOARD_IMAGE_TAG` to a published tag in `.env` before building: `start` skips the pull only when neither tag is `latest`. |
| Docker Compose, default GHCR image | In the directory that holds `docker-compose.yml`: `mkdir -p kb-pack && tar xzf /path/to/kb-pack-<v>.tar.gz -C kb-pack --strip-components=1` (the tarball root is `kb-pack/`); in `docker-compose.yml` uncomment `KB_PACK_DIR=/kb-pack` and the `./kb-pack:/kb-pack:ro` mount | `./faultmaven.sh restart` (it recreates the container, so the compose edit applies) |
| Docker Compose, a newer published image | An image that was built with the pack vendored | With `KB_PACK_DIR` unset (re-comment the two compose lines): `./faultmaven.sh stop && ./faultmaven.sh start --pull`, or pin `FM_IMAGE_TAG` to the new tag |
| Kubernetes, `TENANT_PROVIDER=single` | A new API image **with `KB_PACK_DIR` unset** (remove the `kb-pack-from-minio.yaml` patch from the overlay, since it sets `KB_PACK_DIR=/kb-pack`), **or** upload the tarball to MinIO `kb-packs`, set `KB_PACK_VERSION` in the ConfigMap and enable the opt-in `overlays/onprem/kb-pack-from-minio.yaml` patch in `faultmaven-enterprise-infra` | Roll the API pod |
| Kubernetes, `TENANT_PROVIDER=multi` | A new image: the pack must be baked into the image the `kb_seed` Job runs | The API skips its startup bootstrap here, so `KB_PACK_DIR` has no effect, and the MinIO patch only adds a failure: its initContainer exits non-zero while `KB_PACK_VERSION` is empty, so the API pod does not start (with a version set it downloads a pack nothing reads). Run the `kb_seed` Job (`python -m faultmaven.jobs.run kb_seed --cross-tenant-maintenance`) **with the image the API runs** — the manifest's pinned image is stale — and CD does so after each deploy. See `docs/operations/onprem-app-deployment.md` § "Deploying an updated pack" in `faultmaven-enterprise-infra` |

**Confirm it landed.** On Compose and Kubernetes `single`, read the API log (`./faultmaven.sh logs api`): `KB bootstrap: ingesting N runbook(s) from pack <dir> (version=...)`, then the `KB bootstrap: BootstrapResult(ingested=..., skipped_unchanged=..., failed=..., pruned=..., ...)` summary with the counts you expect. A `KB bootstrap: no pack loaded from <dir>` warning means the pack was not loaded (missing, corrupt, or refused by a load guard); for a hand-extracted pack the usual cause is an extract that kept the `kb-pack/` root instead of leaving `pack.json` + `vectors.npz` + `runbooks/` directly in `KB_PACK_DIR`. Under `multi` the API log only says `KB bootstrap skipped under multi-tenancy: seed the platform KB pack via the kb_seed maintenance job (#770).`; the evidence is in the Job's log: `KB seed completed: BootstrapResult(...)` and the `KB bootstrap complete: N ingested, N unchanged, N failed, N pruned, ...` line. `ingested=0` with every runbook unchanged means the Job's image carries the pack the database already holds: check it is the image you built, not a stale tag.

The bootstrap is **idempotent**: it compares the pack's `content_hash` against the
hash stamped on the existing row when the pack last ingested it
(`knowledge_items.metadata.pack_content_hash`), not against the row's current
content, so an operator's edit is never mistaken for a new pack version, and skips
unchanged runbooks whose row is served. A changed runbook, or one no chunk of
whose `vector_generation` exists (#2388), is re-ingested: its vectors first, then
one transaction that replaces the row it observed; no vector is deleted, and the
old generation's chunks are collected by the generation sweep. New runbooks are
added; runbooks removed from the pack have their rows pruned.

The bootstrap ingests **each runbook separately**. `_ingest_pack_runbook` refuses
a runbook with no chunks in the pack before any lookup or write. A failure in
`ingest_runbook` (ChromaDB unreachable, for example) writes no row, or leaves the
row it would have replaced as it was: the vectors it wrote carry a generation no
row holds, so no door serves them, and the next bootstrap's collectors remove
them once older than `KB_INCOMPLETE_INGEST_MAX_AGE_HOURS`
(the failure contract: [`kb-ingestion-architecture.md`](../architecture/knowledge-and-ai/kb-ingestion-architecture.md#failure-contract)). Per-runbook failures don't abort the
rest of the bootstrap; check the API logs for `KB bootstrap failed for ...`
warnings after restart. Full build + delivery detail: [`kb-pack-architecture.md`](../architecture/knowledge-and-ai/kb-pack-architecture.md).

### Reset / hot-rebuild

`fm-reset-kb` wipes the KB state and (optionally) re-runs the bootstrap in-process:

```bash
fm-reset-kb --dry-run             # See counts; no changes
fm-reset-kb --yes                 # Wipe; bootstrap reruns on API restart
fm-reset-kb --yes --rebuild       # Wipe + immediate in-process rebuild
fm-reset-kb --yes --all-drafts    # Also delete every draft (upload, document, case)
fm-reset-kb --yes --keep-chroma   # Wipe SQL only; keep ChromaDB collections
```

Defaults are conservative — `conversion_drafts` is preserved unless `--all-drafts` is passed. A draft that published a deleted runbook is discarded, its text kept, so it leaves the Drafts list (#2369). `--all-drafts` deletes every draft instead (upload, document and case drafts alike; for an authored runbook whose item the reset deletes, its draft holds the last copy of its text). On either path the reset then releases each case key held by a conversion job with no live draft left, so that case can be converted again (#2387). Only the items the reset locked are deleted; one a running API writes meanwhile is kept and reported.

`fm-reset-kb` is a console entrypoint shipped with the installed package (`faultmaven/cli/reset_kb.py`), so it is available both in a local checkout (after `pip install -e .`) and inside the API pod.

> **This resets the KB, not the deployment.** It refuses under `TENANT_PROVIDER=multi`. To return a whole deployment to a clean slate (cases, evidence, users, tenants, object storage, Redis), use `fm-wipe-deployment`: see [Deployment Wipe](./deployment-wipe.md).

#### Which store it wipes, and when it refuses

The ChromaDB wipe is an `rmtree` of a **local** directory, and both of the
questions that implies are answered from the settings the running server reads
(fm#936):

* **Is a local directory the store at all?** `CHROMADB_URL` says no — it is the
  knob `create_kb_chromadb_client` dispatches on, and under it this command
  cannot reach the server's collections, so it **refuses before touching SQL**
  rather than deleting every `knowledge_items` row while the server's vectors
  survive. Pass `--keep-chroma` to wipe only the SQL rows, accepting the
  divergence; to clear a remote store, remove its KB collection on the server
  and restart the API so the bootstrap re-ingests the pack
  (`fm-wipe-deployment` with no flags writes nothing and reports what the
  configured store holds).
* **Which local directory?** Whatever `CHROMADB_KB_PERSIST_DIR` says, read
  exactly as `create_persistent_client` reads it — so a relative value
  (including the shipped default `./data/chroma-kb`) is relative to **the
  working directory the command runs from**. That is the same resolution the
  startup bootstrap uses to *create* it, so the two cannot name different
  trees; there is deliberately no project-root anchoring, because no consumer
  resolves it that way. **Run `fm-reset-kb` from the same working directory the
  server runs from** — `/app` in the image, the checkout for the dev scripts.
* **Does that path exist, and is it store-shaped?** A blank
  `CHROMADB_KB_PERSIST_DIR=` is refused rather than defaulted — an empty value
  resolves to the working directory, and no consumer falls back to the
  documented default either (`create_persistent_client("")` dies in
  `os.makedirs`), so guessing one here would target a tree nothing opens. A
  path that is not there means this process is not looking where the server
  looked. A path that is neither empty nor holding `chroma.sqlite3` is not a
  ChromaDB store. All three refuse before the SQL wipe. `--keep-chroma` is the
  escape hatch when the KB genuinely has no vector store yet.

**Exit codes:** `0` did what it said · `1` refused, nothing written · `2` wiped,
then `--rebuild` ingested with failures (re-runnable) · `3` wiped the SQL rows
but could **not** remove the vector store — the KB is diverged and needs a human
before the API goes back up. A runbook chaining `fm-reset-kb --yes && kubectl …
scale --replicas=1` stops on 3, which is the point of it having a code at all.
`--dry-run` writes nothing and always exits 0, even when it prints the refusal a
real run would give.

Before this, the path was re-derived from the project root while the server
opened `CHROMADB_KB_PERSIST_DIR`, *and* the startup bootstrap created an empty
directory at the project root — so on a deployment backed by external ChromaDB
the command deleted every row, removed the empty look-alike, printed
`Removed …` and exited 0. The "no ChromaDB directory found" warning written to
catch exactly that could not fire, because the bootstrap's decoy made the
directory exist; it is now a refusal that runs first.

#### ⚠️ Stop the API before wiping

The wipe **`rmtree`s a ChromaDB directory that a running server holds open**. A live API keeps file handles and in-memory collection state on the tree being deleted, so with the server up it can keep serving reads from deleted files, recreate a partial directory underneath the one just removed, or fail on its next write. `--dry-run` is always safe; anything with `--yes` is not.

Scale the API down for the wipe, or restart it immediately afterwards so it reopens a clean store:

```bash
# Kubernetes — run the wipe against the same volume with the server down
kubectl -n faultmaven scale deploy/faultmaven-api --replicas=0
kubectl -n faultmaven run fm-reset --rm -it --restart=Never \
  --image=<the API image> --overrides='<PVC mount for data/>' -- fm-reset-kb --yes
kubectl -n faultmaven scale deploy/faultmaven-api --replicas=1
```

If scaling to zero is not an option, run it in the live pod and restart immediately — accepting that reads between the wipe and the restart are undefined:

```bash
kubectl exec -it deploy/faultmaven-api -- fm-reset-kb --dry-run   # always safe
kubectl exec -it deploy/faultmaven-api -- fm-reset-kb --yes
kubectl -n faultmaven rollout restart deploy/faultmaven-api       # do this now
```

The Docker Compose stack has the identical problem — `data/` is bind-mounted into the API container, so a wipe from the host or from inside the container hits a store the running server holds open:

```bash
./faultmaven.sh stop
fm-reset-kb --yes      # in the checkout's venv, against the bind-mounted data/
./faultmaven.sh start
```

The command prints the **resolved, absolute** ChromaDB store it found — the local path, or the remote server it is refusing to touch. Check it against the store the server actually writes to.

It refuses to run under `TENANT_PROVIDER=multi` — a multi-tenant database holds every tenant's KB, and a blanket wipe would bypass the audited maintenance path. Reseed the platform tier with the `kb_seed` job instead (#770).

### Updating built-in global runbooks

The built-in runbooks ship in the **KB pack** (`resources/knowledge/pack/`), built by
the KB Toolkit (the authoritative source). They are **not** copied to
`data/knowledge/`, and the bootstrap reads nothing from there. Updating them is the
three steps above: rebuild the pack, then deliver it with the Step 3 row for your
deployment. Re-vendoring the pack in a checkout and running `./faultmaven.sh restart`
is **not** one of those rows: on the default GHCR image it re-ingests the pack baked
into that image, unchanged.

To update a **running** instance without an image rebuild, deliver the pack via
`KB_PACK_DIR` (local bind-mount) or MinIO (Kubernetes `single`) instead of re-vendoring —
see [`kb-pack-architecture.md`](../architecture/knowledge-and-ai/kb-pack-architecture.md) §Delivery.

### Important: do not ingest runbooks directly into ChromaDB

Add runbooks through the paths that keep the invariant below: a new KB pack for built-ins, or an upload, conversion or (standalone) `/knowledge/scan` import followed by verify for authored ones. The bootstrap ingests only the pack; a file copied into `data/knowledge/` is ingested by nothing until a scan imports it and someone verifies the draft. Do **not** write directly to ChromaDB (e.g., via scripts or the ChromaDB Python client) or insert directly into `knowledge_items`. The bootstrap maintains the invariant that **no row exists in `knowledge_items` without a matching set of ChromaDB chunks for the same `item_id`**, and vice versa. Bypassing the bootstrap breaks that invariant and produces:

- Orphaned vectors with no provenance (no SQL row to identify them).
- Orphaned SQL rows that aren't searchable (no chunks for retrieval).
- No audit trail of who ingested the runbook or when.

### Removing a runbook

Delete it via the API or the Dashboard. Once the delete answers, no knowledge base
search, investigation or runbook dedup serves it: every one reads a chunk only
while its row is complete and published. An authored runbook has no file to
remove: its row is deleted and its vectors are removed after it, best effort. A
built-in is **unpublished**: its row is kept and flagged, and the next bootstrap's
reconcile removes its vectors. To retire a built-in for good, remove it from the
pack (the bootstrap then prunes the row).

For bulk removal, `fm-reset-kb --yes` (without `--rebuild`) wipes the full KB state; the next API restart re-ingests the KB pack and nothing else (no startup step reads `data/knowledge/`). A runbook authored through the Dashboard exists only as database rows, so the wipe removes its `knowledge_items` row and vectors along with the built-ins. It keeps the `conversion_drafts` rows (and so the draft text) unless `--all-drafts` is passed; back up `data/faultmaven.db` first (see [Backup Strategy](#backup-strategy)).

---

## Evidence File Management

### Directory structure

Evidence files are organized by organization, case, and upload date. Filenames are UUID-prefixed to prevent collisions when the same filename is uploaded twice:

```
data/evidence/
└── local-user-org/                          # organization_id
    │                                         #   (default org in Local Deployment;
    │                                         #    real org ID in Cloud Deployment)
    ├── 01dfc7e3c882/                        # case_id (no "case_" prefix)
    │   └── 2026-04-18/                      # YYYY-MM-DD of upload
    │       ├── a1b2c3d4e5f6_system-logs.txt # UUID prefix + sanitized filename
    │       └── ...
    ├── 025d63119af9/
    │   └── 2026-04-17/
    │       └── f0e9d8c7b6a5_metrics-export.csv
    └── ...
```

The layout is **per-organization**, not per-user — this aligns with FaultMaven's tenancy model (all data is scoped to an organization; Local Deployment uses a single default organization `local-user-org` created at startup).

The path format is implemented in `_generate_storage_path` at [file_storage_service.py:475](../../faultmaven/modules/evidence/domain/services/file_storage_service.py#L475).

### Checking disk usage

```bash
# Total evidence storage
du -sh data/evidence/

# Per-case breakdown (Local deployment has a single org, so the glob is simpler)
du -sh data/evidence/*/*/ | sort -rh | head -20

# Find large files
find data/evidence/ -type f -size +10M -exec ls -lh {} \;

# Files uploaded in the last 7 days
find data/evidence/ -type f -mtime -7
```

### Evidence dual storage

Each uploaded file is stored in two places:

1. `data/evidence/<organization_id>/<case_id>/<YYYY-MM-DD>/<uuid>_<filename>` — the raw file (90-day retention)
2. `data/faultmaven.db` — structured metadata in the `evidence` table (evidence ID, category, summary, preprocessing result) and the `uploaded_files` table (file path reference, upload metadata)

After upload, a background task vectorizes the content into `data/chroma-evidence/` — a `case_{case_id}` collection for semantic search during investigation (ephemeral, cleaned up on case closure). The raw file and relational metadata are written synchronously during upload. The vector embedding is a background task that runs after the API response — it may silently fail without affecting the upload.

---

## SQLite Database Management

### Engine configuration

The async SQLAlchemy engine is created in [`faultmaven/infrastructure/persistence/database.py`](../../faultmaven/infrastructure/persistence/database.py). For SQLite URLs the setup uses `NullPool` (SQLite doesn't pool well) and registers a `connect` event listener that sets six PRAGMAs on **every** new connection — PRAGMAs are per-connection state in SQLite, and `NullPool` opens a fresh connection per checkout.

**Correctness PRAGMAs (must succeed for the app to behave):**

| PRAGMA | Value | Why it's set |
| --- | --- | --- |
| `journal_mode` | `WAL` | Readers don't block writers. Without this the default rollback journal takes an exclusive write lock, so any concurrent read+write in the async event loop collides. |
| `busy_timeout` | `5000` ms | Wait up to 5s on contention before failing. Default `0` ms surfaces as `sqlite3.OperationalError: database is locked` on the second concurrent commit. |
| `foreign_keys` | `ON` | SQLite ignores FK constraints by default; setting this enables `ON DELETE CASCADE`. |

**Performance PRAGMAs (safe defaults under WAL):**

| PRAGMA | Value | Why it's set |
| --- | --- | --- |
| `synchronous` | `NORMAL` | Canonical WAL pairing. Default `FULL` fsyncs every commit; `NORMAL` is safe under WAL — loses at most the last few commits on **power loss**, never on app/OS crash — and substantially faster. |
| `temp_store` | `MEMORY` | Temp tables, sort spill, and index build go to RAM instead of `/tmp` files. Temp data is ephemeral, so no durability cost. |
| `cache_size` | `-64000` | ~64 MB page cache (negative = KB absolute). Default 2000 pages ≈ 8 MB is too small for the hot working set (cases + recent messages + evidence rows). |

**Deployment scope:** These PRAGMAs apply only to SQLite (Standalone). Cloud deployments use PostgreSQL, which has its own concurrency model (MVCC), default-on foreign keys, and server-side configuration (`shared_buffers`, `work_mem`, `synchronous_commit`, etc.) outside the application engine layer. The branch in `get_engine()` is gated on `is_sqlite(url)` — the SQLite PRAGMA listener never runs against PostgreSQL.

**Assumptions baked into the SQLite config:**

- **Single-process per DB file.** Local deploys run one uvicorn worker against one `data/faultmaven.db` (per `docker-compose.yml`). WAL works best with single-writer-multi-reader; multiple writer processes would still serialize through `busy_timeout`.
- **Local POSIX filesystem.** WAL requires correct fsync semantics. ext4 / btrfs / APFS / Docker volumes on host filesystems are fine. **NFS / SMB will corrupt the database** — SQLite docs explicitly warn against them.
- **Dev-grade durability.** `synchronous=NORMAL` accepts the tail-of-commit risk on power loss. Acceptable for a self-hosted single-user tool; not acceptable for paid SaaS — but Cloud uses PostgreSQL so this trade-off doesn't apply there.

### Inspecting the database

```bash
# Open interactive SQLite shell
sqlite3 data/faultmaven.db

# List all tables
.tables

# Check table row counts
SELECT name, (SELECT COUNT(*) FROM cases) FROM sqlite_master WHERE name='cases';

# Check database size
.shell du -sh data/faultmaven.db
```

### Useful queries

```sql
-- Case count by state
SELECT state, COUNT(*) FROM cases GROUP BY state;

-- Evidence count by case
SELECT case_id, COUNT(*) FROM evidence GROUP BY case_id ORDER BY COUNT(*) DESC LIMIT 10;

-- Knowledge items count
SELECT scope, COUNT(*) FROM knowledge_items GROUP BY scope;

-- Conversion drafts by status
SELECT status, COUNT(*) FROM conversion_drafts GROUP BY status;
```

### Backup

```bash
# Online backup (safe while app is running — WAL-aware)
sqlite3 data/faultmaven.db ".backup data/faultmaven-backup-$(date +%Y%m%d).db"

# Or simply copy (stop app first for consistency)
cp data/faultmaven.db data/faultmaven-backup-$(date +%Y%m%d).db
```

**WAL caveat:** Because the engine runs in WAL mode (see "Engine configuration" above), the DB file is accompanied by `data/faultmaven.db-wal` and `data/faultmaven.db-shm` sidecar files. The `.backup` command above is WAL-aware and produces a consistent snapshot from the live database. **A plain `cp` of only `faultmaven.db` may miss recent commits still in the WAL.** If you must use a file copy, either stop the app first, or run `sqlite3 data/faultmaven.db "PRAGMA wal_checkpoint(FULL);"` before copying — then include the `-wal` and `-shm` files in the copy as well.

### Database migrations

```bash
# Apply pending migrations
alembic upgrade head

# Check current migration version
alembic current

# View migration history
alembic history
```

---

## ChromaDB Management

### Inspecting collections

```bash
# KB instance (permanent collections)
python3 -c "
import chromadb
client = chromadb.PersistentClient(path='data/chroma-kb')
for col in client.list_collections():
    print(f'{col.name}: {col.count()} documents')
"

# Evidence instance (ephemeral per-case collections)
python3 -c "
import chromadb
client = chromadb.PersistentClient(path='data/chroma-evidence')
for col in client.list_collections():
    print(f'{col.name}: {col.count()} documents')
"
```

### Expected collections

The table below is the operational expected-state checklist. For collection design, scope-filter implementation, and embedding model details see [docs/architecture/data-and-storage/vector-storage.md](../architecture/data-and-storage/vector-storage.md).

| Instance | Collection | Purpose |
| --- | --- | --- |
| `chroma-kb/` | `faultmaven_kb` | All KB documents (global/personal/team, metadata-filtered) **and** runbooks (`document_type == "runbook"`) |
| `chroma-evidence/` | `case_<case_id>` | Per-case evidence vectors (one per active investigation) |

`chroma-kb/` holds exactly one collection. Earlier revisions of this table also
listed `faultmaven_runbooks` and `knowledge_items`; neither has ever existed as
a ChromaDB collection. `faultmaven_runbooks` was only ever a decorative constant
(`RunbookKnowledgeBase` is injected the `faultmaven_kb` store and never selects
a collection; the constant is gone as of fm#1030) — and
`knowledge_items` is a **SQL table**. Runbooks are therefore distinguished from
KB documents only by their `document_type` metadata value, which is why runbook
search ANDs that predicate into every query (fm#1030). An operator finding no
`faultmaven_runbooks` collection is looking at correct state.

### Mapping UUID folders to collections

Each ChromaDB instance stores HNSW indexes as UUID-named folders. To identify which collection a UUID folder belongs to:

```bash
python3 -c "
import sqlite3, sys
db = sys.argv[1] if len(sys.argv) > 1 else 'data/chroma-kb/chroma.sqlite3'
conn = sqlite3.connect(db)
c = conn.cursor()
c.execute('''
    SELECT s.id AS segment_uuid, c.name AS collection_name
    FROM segments s JOIN collections c ON s.collection = c.id
    WHERE s.type LIKE '%hnsw%'
''')
for row in c.fetchall():
    print(f'{row[0]}  ->  {row[1]}')
conn.close()
" data/chroma-kb/chroma.sqlite3
```

### ChromaDB disk usage

```bash
du -sh data/chroma-kb/ data/chroma-evidence/
```

---

## Disk Space and Cleanup

### Quick health check

```bash
du -sh data/faultmaven.db data/chroma-kb/ data/chroma-evidence/ data/evidence/ data/knowledge/
```

### What grows over time

| Path | Growth Driver | Cleanup |
| --- | --- | --- |
| `data/evidence/` | User uploads (largest consumer) | 90-day retention policy; manual cleanup of closed cases |
| `data/chroma-evidence/` | Per-case vector collections | Auto-cleaned on case closure; safe to wipe entirely |
| `data/chroma-kb/` | KB ingestion | Minimal growth; permanent |
| `data/faultmaven.db` | All relational writes | Archive resolved cases older than 90 days |
| `data/knowledge/` | Standalone scan import directory (hand-placed runbook files) | Minimal growth; manual curation |

### Manual cleanup of closed case evidence

```bash
# List evidence directories for cases older than 90 days
# (cross-reference with database to find resolved cases)
# Paths look like: data/evidence/<organization_id>/<case_id>/
sqlite3 data/faultmaven.db "
  SELECT 'data/evidence/' || organization_id || '/' || case_id
  FROM cases
  WHERE state IN ('resolved', 'closed')
  AND resolved_at < datetime('now', '-90 days');
"
```

### Full data reset (development only)

```bash
# Stop the application first
rm -rf data/faultmaven.db data/chroma-kb/ data/chroma-evidence/ data/evidence/
# Restart — auto-initialization recreates directories, runs migrations,
# and creates the default admin user
```

---

## Backup Strategy

### Minimum backup set

| What | How | Frequency |
| --- | --- | --- |
| `data/faultmaven.db` | SQLite `.backup` command (WAL-aware; see "SQLite Database Management → Backup" for the file-copy caveat) | Daily |
| `data/chroma-kb/` | Directory snapshot | Daily |
| `data/knowledge/` | Directory copy (standalone scan import files only; runbook text is already in `faultmaven.db`) | On change |
| `data/evidence/` | Directory copy or rsync | Daily (large) |
| `data/chroma-evidence/` | **Not backed up** — ephemeral, reconstructable from raw evidence files | N/A |

### Example backup script

```bash
#!/bin/bash
BACKUP_DIR="/backup/faultmaven/$(date +%Y%m%d)"
mkdir -p "$BACKUP_DIR"

# SQLite (online backup)
sqlite3 data/faultmaven.db ".backup $BACKUP_DIR/faultmaven.db"

# ChromaDB KB only (permanent — evidence instance is ephemeral, skip it)
cp -r data/chroma-kb/ "$BACKUP_DIR/chroma-kb/"

# Standalone scan import files (runbook text itself is in faultmaven.db)
cp -r data/knowledge/ "$BACKUP_DIR/knowledge/"

# Evidence raw files (rsync for incremental)
rsync -a data/evidence/ "$BACKUP_DIR/evidence/"

echo "Backup complete: $BACKUP_DIR"
du -sh "$BACKUP_DIR"
```

### Restore

```bash
# Stop application
# Replace data directory contents from backup
cp /backup/faultmaven/20260328/faultmaven.db data/faultmaven.db
cp -r /backup/faultmaven/20260328/chroma-kb/ data/chroma-kb/
cp -r /backup/faultmaven/20260328/knowledge/ data/knowledge/
cp -r /backup/faultmaven/20260328/evidence/ data/evidence/
# chroma-evidence/ is not restored — it will be recreated as case files are vectorized
# Start application
```
