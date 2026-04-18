# Data and Storage Management

Operations guide for managing FaultMaven's data directory at the OS level.

Covers the physical file layout, what each file stores, and common tasks including managing knowledge base runbooks without the Dashboard UI.

---

## Data Directory Layout

All runtime data lives under `data/` relative to the project root. This directory is gitignored.

```
data/
├── faultmaven.db              # SQLite — all relational data (33 tables; see er-diagram.md)
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
│   └── <user_id>/             #   Organized by uploading user
│       └── case_<case_id>/    #     then by case
│           └── <filename>     #       Original file as uploaded
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
| `evidence/<user_id>/case_*` | Filesystem | Raw uploaded files (logs, configs, CSVs, PDFs). Not vectors — original files only. | 90-day retention |
| `knowledge/global/` | Filesystem | Runbook markdown source files (global scope). Seeded from `faultmaven/knowledge/builtin/` on first startup (59 built-in runbooks). | Permanent |
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

evidence/<user_id>/case_*/file  →  background vectorization  →  chroma-evidence/ (case_{id} collection)
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

Evidence files are organized by user ID and case ID:

```
data/evidence/
└── 00000000-0000-0000-0000-000000000001/    # user_id
    ├── case_01dfc7e3c882/
    │   └── system-logs.txt                   # original uploaded file
    ├── case_025d63119af9/
    │   └── metrics-export.csv
    └── ...
```

### Checking disk usage

```bash
# Total evidence storage
du -sh data/evidence/

# Per-case breakdown
du -sh data/evidence/*/case_* | sort -rh | head -20

# Find large files
find data/evidence/ -type f -size +10M -exec ls -lh {} \;
```

### Evidence triple storage

Each uploaded evidence file is stored in three places:

1. `data/evidence/<user_id>/case_<case_id>/` — the raw file (90-day retention)
2. `data/faultmaven.db` — structured metadata in `evidence`, `evidence_artifacts`, and `uploaded_files` tables (evidence ID, category, summary, preprocessing result, file path reference)
3. `data/chroma-evidence/` — vectorized chunks in a `case_{case_id}` collection for semantic search during investigation (ephemeral, cleaned up on case closure)

The raw file and relational metadata are written synchronously during upload. The vector embedding is a background task that runs after the API response — it may silently fail without affecting the upload.

---

## SQLite Database Management

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
# Online backup (safe while app is running)
sqlite3 data/faultmaven.db ".backup data/faultmaven-backup-$(date +%Y%m%d).db"

# Or simply copy (stop app first for consistency)
cp data/faultmaven.db data/faultmaven-backup-$(date +%Y%m%d).db
```

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
sqlite3 data/faultmaven.db "
  SELECT 'data/evidence/%/case_' || REPLACE(case_id, '-', '')
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
| `data/faultmaven.db` | SQLite `.backup` command | Daily |
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
