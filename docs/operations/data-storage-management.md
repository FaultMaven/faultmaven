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
└── knowledge/                 # Runbook source files (markdown, pre-ingestion)
    ├── global/                #   System-wide runbooks (admin-curated)
    │   ├── k8s-crashloopbackoff.md
    │   └── pg-slow-queries.md
    ├── team_<team_id>/        #   Team-scoped runbooks
    └── personal_<user_id>/    #   Personal runbooks (from case conversions)
```

### What stores what

| Physical Path | Storage Technology | Logical Data | Lifecycle |
| --- | --- | --- | --- |
| `faultmaven.db` | SQLite | Cases, users, evidence metadata, hypotheses, solutions, messages, RBAC, audit logs, conversion jobs/drafts, knowledge items | Application-managed |
| `chroma-kb/` | ChromaDB (permanent) | KB embeddings: one `faultmaven_kb` collection holding KB documents and runbooks alike. Backed up, never wiped. | Permanent |
| `chroma-evidence/` | ChromaDB (ephemeral) | Case evidence embeddings: `case_{case_id}` collections (one per active case). Excluded from backups, safe to wipe. | Per-case lifecycle |
| `evidence/<organization_id>/<case_id>/<YYYY-MM-DD>/` | Filesystem | Raw uploaded files (logs, configs, CSVs, PDFs). Not vectors — original files only. UUID-prefixed filenames prevent collisions. | 90-day retention |
| `resources/knowledge/pack/` | Image / `KB_PACK_DIR` | The **KB pack** — pre-deployed runbooks + build-time vectors. Ingested at startup with no embedding model; built by `faultmaven-kb-toolkit`. See [kb-pack-architecture.md](../architecture/knowledge-and-ai/kb-pack-architecture.md). | Shipped |
| `knowledge/global/` | Filesystem | Authored/converted global runbook markdown (draft → verify flow). **No longer seeded from built-ins** — those ship in the pack. | Permanent |
| `knowledge/personal_*/` | Filesystem | Runbook markdown from case-to-runbook conversion | User-controlled |
| `knowledge/team_*/` | Filesystem | Team-scoped runbook files | Team-controlled |

### Why two ChromaDB instances

KB data and case evidence have fundamentally different lifecycles:

- **KB** is permanent, curated, and valuable — it must be backed up and protected from corruption.
- **Case evidence vectors** are ephemeral, created on upload, deleted on case closure, and fully reconstructable from `data/evidence/` raw files.

Separating them into two ChromaDB instances (`chroma-kb/` and `chroma-evidence/`) ensures:
- KB backups exclude disposable evidence data
- Evidence churn (creation/deletion of per-case collections) cannot fragment or corrupt the KB store
- Either instance can be rebuilt independently without affecting the other
- The corruption blast radius of either instance is contained

### Relationship between knowledge/, evidence/, and the two ChromaDB instances

```text
knowledge/*.md  →  startup bootstrap (atomic, idempotent)  →  chroma-kb/ (faultmaven_kb collection)

evidence/<organization_id>/<case_id>/<date>/<uuid>_<file>  →  background vectorization  →  chroma-evidence/ (case_{id} collection)
```

- `resources/knowledge/pack/` (or `KB_PACK_DIR`) holds the **KB pack** — pre-deployed runbooks + build-time vectors. The startup bootstrap (`faultmaven/bootstrap/kb_init.py`) ingests it with **no embedding model**: it writes the pack's chunk texts + pre-computed vectors atomically into `knowledge_items` + `chroma-kb/`. Content-hash idempotency makes restarts free for unchanged runbooks; removed runbooks are pruned. The pack is built/owned by `faultmaven-kb-toolkit` — see [`kb-pack-architecture.md`](../architecture/knowledge-and-ai/kb-pack-architecture.md).
- `knowledge/` holds **authored/converted source markdown** (the draft → verify flow and `/knowledge/scan`) — not the pre-deployed built-ins. See [`kb-ingestion-architecture.md`](../architecture/knowledge-and-ai/kb-ingestion-architecture.md).
- `evidence/` holds **raw uploaded files**. After the upload API returns a response, a background task vectorizes the content into a `case_{case_id}` collection in `chroma-evidence/`.
- Each ChromaDB instance is independent — they share no files.

Deleting a file from `knowledge/` does not remove its embeddings from ChromaDB. The bootstrap intentionally does not garbage-collect deleted files (that's a separate operator concern); use the Dashboard or `fm-reset-kb` to remove KB entries.

---

## Managing Knowledge Base Runbooks (Without UI)

### Adding / updating pre-deployed runbooks (KB pack)

Pre-deployed runbooks ship in the **KB pack** (pre-chunked + pre-embedded), built
and owned by `faultmaven-kb-toolkit`. The startup bootstrap ingests the pack with
**no embedding model** — it does **not** walk `data/knowledge/{scope}/`. To add or
update a pre-deployed runbook you rebuild the pack and deliver it (no app-image
rebuild). This bypasses the Dashboard Drafts UI entirely — that UI is reserved for
case-generated and document-converted drafts that need human review.

**Step 1: Author / edit the runbook (public source)**

Add or edit `.md` files under `faultmaven/resources/knowledge/runbooks/<domain>/`
— the authoritative, public, PR-able source. (The toolkit's authoring/validation
tools — `kb-validate`, `kb-quality`, `kb-init`, `kb-researcher` — operate on these
files; the toolkit's `data/runbooks/` is a symlink to this directory.) Runbooks
require YAML frontmatter — `id` and `title` are mandatory; the deterministic
`knowledge_items.item_id` is derived from `id`.

**Step 2: Build the pack**

```bash
# In faultmaven-kb-toolkit:
kb-build-pack --version 2026-06-09 --tar   # → dist/kb-pack + dist/kb-pack-2026-06-09.tar.gz
```

**Step 3: Deliver it** (pick one) and restart:

```bash
# A) Vendor as the committed baseline (rebuilds the image's default pack)
cp -r dist/kb-pack/* faultmaven/resources/knowledge/pack/

# B) Local self-hosted, no rebuild: extract into the deployment's KB_PACK_DIR dir
#    (see docker-compose.yml), then restart.

# C) Cloud, no rebuild: upload kb-pack-<version>.tar.gz to MinIO `kb-packs`,
#    set KB_PACK_VERSION, roll out (opt-in init container).

./faultmaven.sh restart
```

The bootstrap is **idempotent**: it compares the pack's `content_hash` against the
existing `knowledge_items.content` row and skips unchanged runbooks. Changed
runbooks trigger an atomic delete-then-reingest; new runbooks are added; runbooks
removed from the pack are pruned.

The bootstrap is **atomic per runbook**: a failure (ChromaDB unreachable, 0 chunks
in the pack) cleans up any partial SQL row before raising — no half-state remains
in either store. Per-runbook failures don't abort the rest of the bootstrap; check
the API logs for `KB bootstrap failed for ...` warnings after restart. Full
build + delivery detail: [`kb-pack-architecture.md`](../architecture/knowledge-and-ai/kb-pack-architecture.md).

### Reset / hot-rebuild

`fm-reset-kb` wipes the KB state and (optionally) re-runs the bootstrap in-process:

```bash
fm-reset-kb --dry-run             # See counts; no changes
fm-reset-kb --yes                 # Wipe; bootstrap reruns on API restart
fm-reset-kb --yes --rebuild       # Wipe + immediate in-process rebuild
fm-reset-kb --yes --all-drafts    # Also delete case-generated drafts
fm-reset-kb --yes --keep-chroma   # Wipe SQL only; keep ChromaDB collections
```

Defaults are conservative — `conversion_drafts` (case-generated work in progress) is preserved unless `--all-drafts` is passed.

`fm-reset-kb` is a console entrypoint shipped with the installed package (`faultmaven/cli/reset_kb.py`), so it is available both in a local checkout (after `pip install -e .`) and inside the API pod.

> **This resets the KB, not the deployment.** It refuses under `TENANT_PROVIDER=multi`, and its ChromaDB wipe `rmtree`s a *local* directory — with an external `CHROMADB_URL` the vectors survive it. To return a whole deployment to a clean slate (cases, evidence, users, tenants, object storage, Redis), use `fm-wipe-deployment`: see [Deployment Wipe](./deployment-wipe.md).

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

The command prints the **resolved** ChromaDB path it found. Check it against the store the server actually writes to — if it reports that no directory was found, the SQL rows are gone and the vector store was left untouched, which means the two halves of the KB have diverged.

It refuses to run under `TENANT_PROVIDER=multi` — a multi-tenant database holds every tenant's KB, and a blanket wipe would bypass the audited maintenance path. Reseed the platform tier with the `kb_seed` job instead (#770).

### Updating built-in global runbooks

The 59 built-in runbooks ship in the **KB pack** (`resources/knowledge/pack/`),
built by the KB Toolkit (the authoritative source). They are **no longer** copied
to `data/knowledge/global/`. To update them, rebuild the pack and re-vendor the
baseline:

```bash
# In faultmaven-kb-toolkit (owns data/runbooks):
kb-build-pack --version 2026-06-09

# Re-vendor the committed baseline in the app repo:
cp -r dist/kb-pack/* /path/to/faultmaven/resources/knowledge/pack/

# Restart the API — the bootstrap detects content-hash changes and re-ingests
# (and prunes any runbooks removed from the pack).
./faultmaven.sh restart
```

To update a **running** instance without rebuilding the image, deliver the pack
via `KB_PACK_DIR` (local bind-mount) or MinIO (cloud) instead of re-vendoring —
see [`kb-pack-architecture.md`](../architecture/knowledge-and-ai/kb-pack-architecture.md) §Delivery.

### Important: do not ingest runbooks directly into ChromaDB

Copying runbook files into `data/knowledge/` and letting the bootstrap ingest them is the correct way to add runbooks. Do **not** write directly to ChromaDB (e.g., via scripts or the ChromaDB Python client) or insert directly into `knowledge_items`. The bootstrap maintains the invariant that **no row exists in `knowledge_items` without a matching set of ChromaDB chunks for the same `item_id`**, and vice versa. Bypassing the bootstrap breaks that invariant and produces:

- Orphaned vectors with no provenance (no SQL row to identify them).
- Orphaned SQL rows that aren't searchable (no chunks for retrieval).
- No audit trail of who ingested the runbook or when.

### Removing a runbook

Removing a runbook requires two steps — deleting the source file and removing the KB entry:

```bash
# 1. Remove the source file (so it doesn't get re-ingested on next restart)
rm data/knowledge/global/my-runbook.md

# 2. Delete via the API or Dashboard so both SQL and ChromaDB are updated
#    The bootstrap intentionally does NOT garbage-collect files deleted from
#    disk — explicit deletion is a separate operator concern.
```

For bulk removal, `fm-reset-kb --yes` (without `--rebuild`) wipes the full KB state; the next API restart will re-ingest from whatever remains in `data/knowledge/`.

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

Each uploaded evidence file is stored in two places:

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
| `data/knowledge/` | Runbook source files | Minimal growth; manual curation |

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
| `data/knowledge/` | Directory copy | On change |
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

# Knowledge source files
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
# chroma-evidence/ is not restored — it will be recreated as cases upload evidence
# Start application
```
