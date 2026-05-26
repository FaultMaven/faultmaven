# Data and Storage Management

Operations guide for managing FaultMaven's data directory at the OS level.

Covers the physical file layout, what each file stores, and common tasks including managing knowledge base runbooks without the Dashboard UI.

---

## Data Directory Layout

All runtime data lives under `data/` relative to the project root. This directory is gitignored.

```
data/
├── faultmaven.db              # SQLite — all relational data (29 tables; see er-diagram.md)
│
├── chroma-kb/                 # ChromaDB instance — permanent KB vectors
│   ├── chroma.sqlite3         #   Collection metadata, doc IDs, text, full-text index
│   └── <uuid>/                #   HNSW vector index (one folder per collection)
│       ├── data_level0.bin    #   Collections: faultmaven_kb, faultmaven_runbooks,
│       ├── header.bin         #               knowledge_items
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
| `chroma-kb/` | ChromaDB (permanent) | KB embeddings: `faultmaven_kb`, `faultmaven_runbooks`, `knowledge_items` collections. Backed up, never wiped. | Permanent |
| `chroma-evidence/` | ChromaDB (ephemeral) | Case evidence embeddings: `case_{case_id}` collections (one per active case). Excluded from backups, safe to wipe. | Per-case lifecycle |
| `evidence/<organization_id>/<case_id>/<YYYY-MM-DD>/` | Filesystem | Raw uploaded files (logs, configs, CSVs, PDFs). Not vectors — original files only. UUID-prefixed filenames prevent collisions. | 90-day retention |
| `knowledge/global/` | Filesystem | Runbook markdown source files (global scope). Seeded from `resources/knowledge/builtin/` on first startup (59 built-in runbooks). | Permanent |
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

```
knowledge/*.md  →  Dashboard scan → activate  →  chroma-kb/ (faultmaven_kb collection)

evidence/<organization_id>/<case_id>/<date>/<uuid>_<file>  →  background vectorization  →  chroma-evidence/ (case_{id} collection)
```

- `knowledge/` holds **source markdown files**. The canonical ingestion path is: copy files here, open the Dashboard KB page (triggers automatic scan), then activate drafts. Activation triggers chunking, BGE-M3 embedding generation, and storage into the `faultmaven_kb` collection in `chroma-kb/`.
- `evidence/` holds **raw uploaded files**. After the upload API returns a response, a background task vectorizes the content into a `case_{case_id}` collection in `chroma-evidence/`.
- Each ChromaDB instance is independent — they share no files.

Deleting a file from `knowledge/` does not remove its embeddings from ChromaDB. You must delete the document via the Dashboard or API for the vector store to reflect the change.

---

## Managing Knowledge Base Runbooks (Without UI)

### Adding runbooks via filesystem + Dashboard scan

This is the recommended workflow for bulk-loading runbooks without using the API upload endpoint.

**Step 1: Place runbook files on disk**

Copy `.md` files into the appropriate scope directory:

```bash
# Global runbooks (visible to all users)
cp my-runbook.md data/knowledge/global/

# Team-scoped runbooks
cp team-runbook.md data/knowledge/team_<team_id>/

# Personal runbooks
cp personal-runbook.md data/knowledge/personal_<user_id>/
```

Runbooks should use YAML frontmatter for metadata. Minimal example:

```yaml
---
id: my-runbook-id
title: "PostgreSQL - Slow Query Diagnosis"
technology: postgresql
severity: medium
tags:
  - postgresql
  - performance
status: verified
---

# PostgreSQL - Slow Query Diagnosis

(runbook content here)
```

See `docs/operations/runbooks/template.md` for the full template with all supported frontmatter fields.

**Step 2: Scan from the Dashboard**

Open the Dashboard KB page (http://localhost:3333), go to the **Drafts** tab, and click **"Scan for runbooks"**. This calls `POST /api/v1/knowledge/scan` which:

1. Walks `data/knowledge/` recursively for `.md` files
2. Skips files already tracked in the database
3. Extracts title and metadata from YAML frontmatter
4. Infers scope from the directory name (`global/`, `team_*`, `personal_*`)
5. Creates draft records so they appear in the Drafts tab

From the Drafts tab you can then review, edit, and activate each draft into the vector database.

**Step 2 (alternative): Scan via API**

```bash
curl -X POST http://localhost:8090/api/v1/knowledge/scan \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json"
```

Response:

```json
{
  "discovered": 3,
  "skipped": 12,
  "errors": [],
  "drafts": [...]
}
```

### Updating built-in global runbooks

The 59 built-in runbooks ship in `resources/knowledge/builtin/` and are copied to `data/knowledge/global/` on first startup. To update them from the KB Toolkit (the authoritative source), sync the two directories:

```bash
rsync -av --delete \
  /path/to/faultmaven-kb-toolkit/data/runbooks/ \
  /path/to/faultmaven/resources/knowledge/builtin/
```

This skips unchanged files (compares size + modification time), copies new or modified runbooks, and removes any that were deleted from the toolkit. The `--delete` flag is safe here because the toolkit is the source of truth for built-in runbooks.

After syncing, the updated files are in the repo (`resources/`) but not yet in the runtime directory (`data/knowledge/global/`). To propagate changes to a running instance:

```bash
# Copy updated files to runtime directory
rsync -av --delete \
  resources/knowledge/builtin/ \
  data/knowledge/global/

# Then open the Dashboard KB page and activate the updated runbooks
```

Note: the bootstrap only copies built-in runbooks if `data/knowledge/` has no `.md` files anywhere (including personal/team scopes). On subsequent startups, it does not overwrite user modifications. The rsync above is a manual step for when you want to pull in updated runbooks from a new release.

### Important: do not ingest runbooks directly into ChromaDB

Copying runbook files into `data/knowledge/` is the correct way to add runbooks at the OS level. However, do **not** write directly to ChromaDB (e.g., via scripts or the ChromaDB Python client). The `conversion_drafts` table in `faultmaven.db` is the single source of truth for ingestion state. Writing directly to ChromaDB bypasses this table, which causes:

- The Dashboard "Scan for runbooks" to re-discover the file as a new draft
- Verifying that draft to ingest the same content a second time — duplicate embeddings
- No audit trail of who ingested the runbook or when

Always use the Dashboard scan → activate workflow to move runbooks from `data/knowledge/` into the vector database.

### Removing a runbook

Removing a runbook requires two steps — deleting the source file and removing the vector entry:

```bash
# 1. Remove the source file
rm data/knowledge/global/my-runbook.md

# 2. Remove from ChromaDB via API (if ingested)
#    Use the document management endpoints or re-ingest with --force
```

If the runbook was added via the scan workflow, delete the draft from the Dashboard Drafts tab. If it was already verified and ingested, use the KB management API to delete the document.

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

**Deployment scope:** These PRAGMAs apply only to SQLite (Local / Community Edition). Cloud / Enterprise deployments use PostgreSQL, which has its own concurrency model (MVCC), default-on foreign keys, and server-side configuration (`shared_buffers`, `work_mem`, `synchronous_commit`, etc.) outside the application engine layer. The branch in `get_engine()` is gated on `is_sqlite(url)` — the SQLite PRAGMA listener never runs against PostgreSQL.

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
-- Case count by status
SELECT status, COUNT(*) FROM cases GROUP BY status;

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
| `chroma-kb/` | `faultmaven_kb` | All KB documents (global/personal/team, metadata-filtered) |
| `chroma-kb/` | `faultmaven_runbooks` | Runbook similarity matching for report recommendations |
| `chroma-kb/` | `knowledge_items` | Knowledge module search service |
| `chroma-evidence/` | `case_<case_id>` | Per-case evidence vectors (one per active investigation) |

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
  WHERE status IN ('resolved', 'closed')
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
