# Database Schema

This directory contains PostgreSQL schema definition scripts for the FaultMaven database.

## Schema Structure

**Purpose**: These SQL scripts create the complete database schema from scratch for fresh deployments.

**Target State**: Enterprise-ready normalized PostgreSQL schema with multi-tenancy and sharing

---

## Schema Files

### 001_initial_hybrid_schema.sql (19KB)

**Status**: ✅ Production-ready

**Description**: Creates the foundational hybrid normalized schema with:
- 10 normalized tables (cases, evidence, hypotheses, solutions, case_messages, uploaded_files, case_status_transitions, case_tags, agent_tool_calls)
- JSONB columns for flexible low-cardinality data
- Full-text search indexes (GIN)
- Foreign key constraints with CASCADE deletes
- Auto-update triggers for timestamps
- Utility views (case_overview, active_hypotheses, recent_evidence)

**Reference**: `docs/architecture/case-storage-design.md`

**When to use**: Base schema required for all deployments

### 002_add_case_sharing.sql (12KB)

**Status**: ✅ Production-ready (Implemented 2025-01-14)

**Description**: Adds case sharing and collaboration features:
- `case_participants` table for individual user sharing
- Participant roles: owner, collaborator, viewer
- SQL functions: `upsert_case_participant()`, `remove_case_participant()`, `get_user_case_role()`
- Audit trail for sharing actions (`case_sharing_audit` table)

**Reference**: `docs/architecture/data-storage-design.md` (Section 3.3 - Case Sharing)

**When to use**: After 001, enables Feature 1 (share cases with specific users)

### 003_enterprise_user_schema.sql (24KB)

**Status**: ✅ Production-ready (Implemented 2025-01-14)

**Description**: Implements enterprise SaaS multi-tenancy with teams and RBAC:
- 8 tables: `organizations`, `organization_members`, `teams`, `team_members`, `roles`, `permissions`, `role_permissions`, `user_audit_log`
- 7 system roles: owner, admin, member, viewer, team lead
- 19 permissions across 5 resources (cases, knowledge_base, organization, users, teams)
- Row-Level Security (RLS) policies for multi-tenant isolation
- SQL functions: `user_has_org_permission()`, `user_is_team_member()`, `get_user_teams()`
- Adds `org_id` and `team_id` columns to `cases` table for team-based sharing

**Reference**: `docs/architecture/user-storage-design.md`

**When to use**: After 002, enables Features 2-4 prerequisites (organizations, teams, RBAC)

### 004_kb_sharing_infrastructure.sql (23KB)

**Status**: ✅ Production-ready (Implemented 2025-01-14)

**Description**: Adds knowledge base document sharing capabilities:
- 5 tables: `kb_documents`, `kb_document_shares`, `kb_document_team_shares`, `kb_document_org_shares`, `kb_sharing_audit`
- Visibility levels: private, shared, team, organization
- Share permissions: read, write
- SQL functions: `share_kb_document_with_user()`, `share_kb_document_with_team()`, `user_can_access_kb_document()`
- ChromaDB integration metadata (collection references)

**Reference**: `docs/architecture/data-storage-design.md` (Section 5.5 - KB Sharing)

**When to use**: After 003, enables Features 3-4 (share KB documents with users/teams)

---

## How to Apply Schema

### Application Order

**IMPORTANT**: Apply schema files in sequential order:
1. `001_initial_hybrid_schema.sql` - Base schema (required)
2. `002_add_case_sharing.sql` - Case sharing (depends on 001)
3. `003_enterprise_user_schema.sql` - Organizations & teams (depends on 001, 002)
4. `004_kb_sharing_infrastructure.sql` - KB sharing (depends on 003)

### Option 1: Manual Application (PostgreSQL CLI)

```bash
# Connect to PostgreSQL database
psql -h localhost -U faultmaven -d faultmaven_cases

# Apply migrations in order
\i docs/database/docs/schema/001_initial_hybrid_schema.sql
\i docs/database/docs/schema/002_add_case_sharing.sql
\i docs/database/docs/schema/003_enterprise_user_schema.sql
\i docs/database/docs/schema/004_kb_sharing_infrastructure.sql

# Verify tables created
\dt

# Verify views created
\dv

# Check indexes
\di
```

### Option 2: Using Docker (Development)

```bash
# Start PostgreSQL container
docker run -d \
  --name faultmaven-postgres \
  -e POSTGRES_USER=faultmaven \
  -e POSTGRES_PASSWORD=devpassword \
  -e POSTGRES_DB=faultmaven_cases \
  -p 5432:5432 \
  postgres:15

# Wait for PostgreSQL to be ready
sleep 5

# Apply migrations in order
docker exec -i faultmaven-postgres psql -U faultmaven -d faultmaven_cases < docs/database/docs/schema/001_initial_hybrid_schema.sql
docker exec -i faultmaven-postgres psql -U faultmaven -d faultmaven_cases < docs/database/docs/schema/002_add_case_sharing.sql
docker exec -i faultmaven-postgres psql -U faultmaven -d faultmaven_cases < docs/database/docs/schema/003_enterprise_user_schema.sql
docker exec -i faultmaven-postgres psql -U faultmaven -d faultmaven_cases < docs/database/docs/schema/004_kb_sharing_infrastructure.sql

# Verify
docker exec -it faultmaven-postgres psql -U faultmaven -d faultmaven_cases -c "\dt"
```

### Option 3: Kubernetes Deployment (Production)

```bash
# Create ConfigMap from migration files
kubectl create configmap case-db-migrations \
  --from-file=docs/database/docs/schema/ \
  -n faultmaven

# Create migration Job
kubectl apply -f - <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: case-db-migration-001
  namespace: faultmaven
spec:
  template:
    spec:
      containers:
      - name: migration
        image: postgres:15
        command:
        - psql
        - -h
        - postgresql.faultmaven.svc.cluster.local
        - -U
        - faultmaven
        - -d
        - faultmaven_cases
        - -f
        - /docs/schema/001_initial_hybrid_schema.sql
        volumeMounts:
        - name: migration
          mountPath: /migrations
        env:
        - name: PGPASSWORD
          valueFrom:
            secretKeyRef:
              name: postgresql-credentials
              key: password
      volumes:
      - name: migration
        configMap:
          name: case-db-migration
      restartPolicy: OnFailure
EOF

# Wait for completion
kubectl wait --for=condition=complete job/case-db-migration-001 -n faultmaven --timeout=120s

# Check logs
kubectl logs job/case-db-migration-001 -n faultmaven

# Verify tables created
kubectl exec -it postgresql-0 -n faultmaven -- psql -U faultmaven -d faultmaven_cases -c "\dt"
```

---

## Configuration

After applying migrations, update `.env` to use the hybrid schema:

```bash
# Case Storage Configuration
CASE_STORAGE_TYPE=postgres_hybrid  # Use hybrid normalized schema

# PostgreSQL Connection
CASES_DB_HOST=localhost            # or postgresql.faultmaven.svc.cluster.local in K8s
CASES_DB_PORT=5432
CASES_DB_NAME=faultmaven_cases
CASES_DB_USER=faultmaven
CASES_DB_PASSWORD=your_password_here

# Connection Pool
CASES_DB_POOL_SIZE=10
CASES_DB_MAX_OVERFLOW=20
```

**Configuration Options**:
- `CASE_STORAGE_TYPE=inmemory` - InMemory storage (development, data lost on restart)
- `CASE_STORAGE_TYPE=postgres` - Legacy single-table JSONB (deprecated)
- `CASE_STORAGE_TYPE=postgres_hybrid` - **Production-ready 10-table hybrid schema (recommended)**

---

## Migration Verification

After applying migration and configuring `.env`, verify the system works:

### 1. Start FaultMaven API

```bash
cd /home/swhouse/projects/FaultMaven
source .venv/bin/activate
python -m faultmaven.main
```

**Expected log output**:
```
✅ Case repository: PostgreSQL Hybrid (10-table schema) @ localhost:5432/faultmaven_cases
```

### 2. Create Test Case

```bash
curl -X POST http://localhost:8000/api/v1/cases \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Test migration case",
    "description": "Verifying hybrid schema works"
  }'
```

**Expected response**:
```json
{
  "case_id": "case_abc123xyz...",
  "title": "Test migration case",
  "status": "consulting",
  "created_at": "2025-01-09T12:00:00Z"
}
```

### 3. Upload Evidence

```bash
curl -X POST http://localhost:8000/api/v1/cases/case_abc123xyz.../data \
  -F "file=@test.log" \
  -F "description=Test evidence upload"
```

### 4. Verify Database Records

```sql
-- Connect to PostgreSQL
psql -U faultmaven -d faultmaven_cases

-- Check case created
SELECT case_id, title, status, created_at FROM cases;

-- Check evidence created (normalized table)
SELECT evidence_id, case_id, category, summary FROM evidence;

-- Check using utility view
SELECT * FROM case_overview;
```

**Expected**:
- 1 row in `cases` table
- 1 row in `evidence` table
- 1 row in `case_overview` view with evidence_count=1

---

## Rollback Strategy

### Development: Drop and Recreate

```sql
-- Drop all tables (cascades to dependent tables)
DROP TABLE IF EXISTS cases CASCADE;
DROP TABLE IF EXISTS evidence CASCADE;
DROP TABLE IF EXISTS hypotheses CASCADE;
DROP TABLE IF EXISTS solutions CASCADE;
DROP TABLE IF EXISTS case_messages CASCADE;
DROP TABLE IF EXISTS uploaded_files CASCADE;
DROP TABLE IF EXISTS case_status_transitions CASCADE;
DROP TABLE IF EXISTS case_tags CASCADE;
DROP TABLE IF EXISTS agent_tool_calls CASCADE;

-- Drop views
DROP VIEW IF EXISTS case_overview CASCADE;
DROP VIEW IF EXISTS active_hypotheses CASCADE;
DROP VIEW IF EXISTS recent_evidence CASCADE;

-- Drop types
DROP TYPE IF EXISTS case_status CASCADE;
DROP TYPE IF EXISTS evidence_category CASCADE;
DROP TYPE IF EXISTS hypothesis_status CASCADE;
DROP TYPE IF EXISTS solution_status CASCADE;
DROP TYPE IF EXISTS message_role CASCADE;
DROP TYPE IF EXISTS file_processing_status CASCADE;

-- Reapply migration
\i docs/schema/001_initial_hybrid_schema.sql
```

### Production: Export and Reimport

```bash
# 1. Export existing data
pg_dump -U faultmaven -d faultmaven_cases --data-only --table=cases > backup.sql

# 2. Drop schema
psql -U faultmaven -d faultmaven_cases < docs/schema/rollback_001.sql

# 3. Revert to legacy single-table schema
psql -U faultmaven -d faultmaven_cases < docs/schema/legacy_single_table.sql

# 4. Reimport data
psql -U faultmaven -d faultmaven_cases < backup.sql

# 5. Update .env
sed -i 's/CASE_STORAGE_TYPE=postgres_hybrid/CASE_STORAGE_TYPE=postgres/' .env
```

---

## Performance Testing

After migration, verify performance meets expectations:

### Case Load Performance (Target: <10ms)

```bash
# Load test: Create 100 cases
for i in {1..100}; do
  curl -X POST http://localhost:8000/api/v1/cases \
    -H "Content-Type: application/json" \
    -d "{\"title\": \"Load test case $i\", \"description\": \"Testing hybrid schema performance\"}"
done

# Measure retrieval time
time curl http://localhost:8000/api/v1/cases/{case_id}
```

### Evidence Filtering Performance (Target: <5ms)

```sql
-- Measure evidence query time
EXPLAIN ANALYZE
SELECT * FROM evidence
WHERE case_id = 'case_abc123'
AND category = 'LOGS_AND_ERRORS'
ORDER BY upload_timestamp DESC
LIMIT 10;
```

**Expected**:
- Execution time: < 5ms
- Index Scan on `idx_evidence_case_id` and `idx_evidence_category`

### Full-Text Search Performance (Target: <15ms)

```sql
-- Measure search query time
EXPLAIN ANALYZE
SELECT c.case_id, c.title,
  ts_rank(to_tsvector('english', c.title), plainto_tsquery('english', 'error')) as rank
FROM cases c
WHERE to_tsvector('english', c.title) @@ plainto_tsquery('english', 'error')
ORDER BY rank DESC
LIMIT 20;
```

**Expected**:
- Execution time: < 15ms
- Bitmap Index Scan on GIN index

---

## Troubleshooting

### Issue: Migration fails with "relation already exists"

**Cause**: Tables already exist from previous migration attempt

**Fix**:
```sql
-- Drop all tables first
DROP SCHEMA public CASCADE;
CREATE SCHEMA public;

-- Reapply migration
\i docs/schema/001_initial_hybrid_schema.sql
```

### Issue: FaultMaven fails to connect with "connection refused"

**Cause**: PostgreSQL not running or wrong connection string

**Fix**:
```bash
# Check PostgreSQL is running
docker ps | grep postgres
# or
pg_isready -h localhost -p 5432

# Verify connection string in .env
echo $CASES_DB_URL
# Should be: postgresql+asyncpg://faultmaven:password@localhost:5432/faultmaven_cases
```

### Issue: "asyncpg.exceptions.UndefinedTableError: relation 'cases' does not exist"

**Cause**: Migration not applied

**Fix**:
```bash
# Apply migration
psql -U faultmaven -d faultmaven_cases < docs/schema/001_initial_hybrid_schema.sql

# Verify tables exist
psql -U faultmaven -d faultmaven_cases -c "\dt"
```

### Issue: Slow queries after migration

**Cause**: Missing VACUUM or ANALYZE

**Fix**:
```sql
-- Analyze all tables to update statistics
ANALYZE cases;
ANALYZE evidence;
ANALYZE hypotheses;
ANALYZE solutions;

-- Vacuum to reclaim space
VACUUM ANALYZE;

-- Check index usage
SELECT schemaname, tablename, indexname, idx_scan
FROM pg_stat_user_indexes
ORDER BY idx_scan ASC;
```

---

## Schema Evolution

### Naming Convention

```
{number}_{description}.sql
```

Examples:
- `002_add_case_tags_index.sql`
- `003_add_evidence_s3_ref.sql`
- `004_add_agent_tool_calls_performance_indexes.sql`

### Schema Extension Template

```sql
-- Schema Extension: {number} - {description}
-- Date: {YYYY-MM-DD}
-- Description: {detailed description}

BEGIN;

-- DDL changes here

COMMIT;

-- Verification queries
SELECT COUNT(*) FROM {new_table};
```

---

## References

- **Design Document**: [case-storage-design.md](../docs/architecture/case-storage-design.md)
- **DB Abstraction Layer**: [db-abstraction-layer-specification.md](../docs/architecture/db-abstraction-layer-specification.md)
- **Implementation**: [postgresql_hybrid_case_repository.py](../faultmaven/infrastructure/persistence/postgresql_hybrid_case_repository.py)
