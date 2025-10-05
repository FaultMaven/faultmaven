---
id: postgres-slow-queries
title: "PostgreSQL - Slow Queries"
technology: postgresql
severity: medium
tags:
  - postgresql
  - performance
  - queries
  - indexing
difficulty: advanced
version: "1.0.0"
last_updated: "2025-01-15"
verified_by: "FaultMaven Team"
status: verified
---

# PostgreSQL - Slow Queries

> **Purpose**: Diagnose and optimize slow PostgreSQL queries

## Quick Reference Card

**🔍 Symptoms:**
- Query timeouts
- High database CPU usage
- Application slowness
- Long transaction times

**⚡ Common Causes:**
1. **Missing indexes** (50%) - Queries doing full table scans
2. **Inefficient queries** (30%) - N+1 queries, unnecessary JOINs
3. **Table bloat** (15%) - Dead tuples not vacuumed
4. **Lock contention** (5%) - Queries waiting on locks

**🚀 Quick Fix:**
```sql
-- Find slow queries
SELECT query, mean_exec_time, calls
FROM pg_stat_statements
ORDER BY mean_exec_time DESC
LIMIT 10;
```

**⏱️ Estimated Resolution Time:** 30-60 minutes

---

## Diagnostic Steps

### Step 1: Enable Query Logging

```sql
-- Enable pg_stat_statements
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- Check slow query log
SHOW log_min_duration_statement;

-- Set threshold (log queries >1s)
ALTER SYSTEM SET log_min_duration_statement = 1000;
SELECT pg_reload_conf();
```

### Step 2: Identify Slow Queries

```sql
-- Top 10 slowest queries
SELECT 
  query,
  calls,
  total_exec_time,
  mean_exec_time,
  max_exec_time
FROM pg_stat_statements
ORDER BY mean_exec_time DESC
LIMIT 10;

-- Currently running queries
SELECT pid, query, state, query_start, now() - query_start as duration
FROM pg_stat_activity
WHERE state = 'active'
ORDER BY duration DESC;
```

### Step 3: Analyze Query Plan

```sql
-- Explain query execution plan
EXPLAIN ANALYZE 
SELECT * FROM users WHERE email = 'user@example.com';

-- Look for:
-- - Seq Scan (full table scan - bad for large tables)
-- - Index Scan (good)
-- - High cost values
-- - High actual time values
```

---

## Solutions

### Solution 1: Add Missing Indexes

**When to use:** EXPLAIN shows sequential scans on large tables

```sql
-- Create index on frequently queried columns
CREATE INDEX idx_users_email ON users(email);

-- Composite index for multi-column queries
CREATE INDEX idx_orders_user_status ON orders(user_id, status);

-- Partial index for filtered queries
CREATE INDEX idx_active_users ON users(email) WHERE active = true;

-- Check index usage
SELECT schemaname, tablename, indexname, idx_scan
FROM pg_stat_user_indexes
ORDER BY idx_scan;
```

**Time to resolution:** ~10-20 minutes

### Solution 2: Optimize Query Structure

**When to use:** Query logic is inefficient

```sql
-- Bad: N+1 query problem
SELECT * FROM orders;
-- Then for each order:
SELECT * FROM users WHERE id = order.user_id;

-- Good: Single query with JOIN
SELECT o.*, u.*
FROM orders o
JOIN users u ON u.id = o.user_id;

-- Bad: SELECT *
SELECT * FROM large_table;

-- Good: Select only needed columns
SELECT id, name, email FROM large_table;

-- Use LIMIT for pagination
SELECT * FROM users ORDER BY created_at LIMIT 100 OFFSET 0;
```

**Time to resolution:** Varies (code changes)

### Solution 3: Vacuum and Analyze

**When to use:** Table bloat or outdated statistics

```sql
-- Analyze table to update statistics
ANALYZE users;

-- Vacuum to reclaim space
VACUUM users;

-- Full vacuum (requires exclusive lock)
VACUUM FULL users;

-- Auto-vacuum settings
ALTER TABLE users SET (autovacuum_vacuum_scale_factor = 0.1);

-- Check table bloat
SELECT schemaname, tablename, pg_size_pretty(pg_total_relation_size(schemaname||'.'||tablename))
FROM pg_tables
ORDER BY pg_total_relation_size(schemaname||'.'||tablename) DESC
LIMIT 10;
```

**Time to resolution:** ~20-40 minutes

### Solution 4: Query Optimization Techniques

```sql
-- Use covering indexes
CREATE INDEX idx_users_covering ON users(email) INCLUDE (name, created_at);

-- Use CTEs for complex queries
WITH recent_orders AS (
  SELECT * FROM orders WHERE created_at > now() - interval '7 days'
)
SELECT * FROM recent_orders WHERE status = 'completed';

-- Use EXISTS instead of IN for large subqueries
-- Bad:
SELECT * FROM users WHERE id IN (SELECT user_id FROM orders);

-- Good:
SELECT * FROM users u WHERE EXISTS (SELECT 1 FROM orders o WHERE o.user_id = u.id);
```

---

## Prevention

### Immediate Prevention

1. **Monitor slow queries**
   ```sql
   SELECT query, mean_exec_time
   FROM pg_stat_statements
   WHERE mean_exec_time > 1000
   ORDER BY mean_exec_time DESC;
   ```

2. **Set query timeout**
   ```sql
   ALTER DATABASE mydb SET statement_timeout = '30s';
   ```

### Best Practices

- ✅ Create indexes on WHERE/JOIN columns
- ✅ Use EXPLAIN ANALYZE before deploying
- ✅ Monitor pg_stat_statements
- ✅ Regular VACUUM ANALYZE
- ✅ Use connection pooling
- ✅ Optimize N+1 queries
- ❌ Don't over-index (index maintenance cost)
- ❌ Don't use SELECT * in production

---

## Related Issues

**Related runbooks:**
- [PostgreSQL - Connection Pool Exhausted](postgres-connection-pool-exhausted.md)

**External resources:**
- [PostgreSQL Performance Tips](https://wiki.postgresql.org/wiki/Performance_Optimization)
- [EXPLAIN Tutorial](https://www.postgresql.org/docs/current/using-explain.html)

---

## Version History

| Version | Date       | Author           | Changes                  |
|---------|------------|------------------|--------------------------|
| 1.0.0   | 2025-01-15 | FaultMaven Team  | Initial verified version |

---

## License & Attribution

Licensed under Apache-2.0 License.
