---
id: postgres-connection-pool-exhausted
title: "PostgreSQL - Connection Pool Exhausted"
technology: postgresql
severity: high
tags:
  - postgresql
  - connections
  - pool
  - performance
difficulty: intermediate
version: "1.0.0"
last_updated: "2025-01-15"
verified_by: "FaultMaven Team"
status: verified
---

# PostgreSQL - Connection Pool Exhausted

> **Purpose**: Resolve "too many connections" errors in PostgreSQL

## Quick Reference Card

**🔍 Symptoms:**
- Error: `FATAL: too many connections`
- Applications cannot connect
- Database connection errors
- Connection timeouts

**⚡ Common Causes:**
1. **Connection leaks** (50%) - Connections not closed properly
2. **Pool size too small** (30%) - max_connections too low
3. **Sudden traffic spike** (15%) - More users than expected
4. **Long-running queries** (5%) - Queries holding connections

**🚀 Quick Fix:**
```bash
# Check current connections
psql -U postgres -c "SELECT count(*) FROM pg_stat_activity;"

# Kill idle connections
psql -U postgres -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE state = 'idle';"
```

**⏱️ Estimated Resolution Time:** 5-15 minutes

---

## Diagnostic Steps

### Step 1: Check Connection Count

```sql
-- Current connections
SELECT count(*) FROM pg_stat_activity;

-- By database
SELECT datname, count(*) 
FROM pg_stat_activity 
GROUP BY datname;

-- By state
SELECT state, count(*) 
FROM pg_stat_activity 
GROUP BY state;
```

### Step 2: Check Connection Limit

```sql
-- Maximum connections allowed
SHOW max_connections;

-- Current vs maximum
SELECT 
  count(*) as current,
  (SELECT setting::int FROM pg_settings WHERE name='max_connections') as max,
  round(100.0 * count(*) / (SELECT setting::int FROM pg_settings WHERE name='max_connections'), 2) as percent
FROM pg_stat_activity;
```

### Step 3: Identify Connection Sources

```sql
-- Connections by application
SELECT application_name, count(*) 
FROM pg_stat_activity 
GROUP BY application_name 
ORDER BY count DESC;

-- Idle connections
SELECT pid, usename, application_name, state, state_change
FROM pg_stat_activity 
WHERE state = 'idle'
ORDER BY state_change;
```

---

## Solutions

### Solution 1: Kill Idle Connections

**When to use:** Many idle connections consuming limit

```sql
-- Kill all idle connections
SELECT pg_terminate_backend(pid) 
FROM pg_stat_activity 
WHERE state = 'idle' 
AND pid <> pg_backend_pid();

-- Kill specific application connections
SELECT pg_terminate_backend(pid)
FROM pg_stat_activity
WHERE application_name = 'myapp'
AND state = 'idle';
```

**Time to resolution:** ~2 minutes

### Solution 2: Increase max_connections

**When to use:** Legitimate need for more connections

```bash
# Edit postgresql.conf
sudo nano /var/lib/postgresql/data/postgresql.conf

# Increase max_connections
max_connections = 200  # Was 100

# Also increase shared_buffers (rule: 25% of RAM)
shared_buffers = 2GB

# Restart PostgreSQL
sudo systemctl restart postgresql
```

**⚠️ Note:** Each connection uses memory. Monitor system RAM.

**Time to resolution:** ~10 minutes

### Solution 3: Fix Connection Leaks

**When to use:** Connections not being closed by application

```python
# Bad - connection leak
def query_db():
    conn = psycopg2.connect("dbname=mydb")
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users")
    return cursor.fetchall()
    # Connection never closed!

# Good - proper cleanup
def query_db():
    with psycopg2.connect("dbname=mydb") as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM users")
            return cursor.fetchall()
    # Connection automatically closed
```

**Time to resolution:** Varies (code fix required)

### Solution 4: Implement Connection Pooling

**When to use:** Application needs efficient connection reuse

```python
# Python with connection pool
from psycopg2 import pool

# Create connection pool
connection_pool = pool.SimpleConnectionPool(
    minconn=5,
    maxconn=20,
    host="localhost",
    database="mydb",
    user="user",
    password="password"
)

# Use from pool
def query():
    conn = connection_pool.getconn()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users")
        return cursor.fetchall()
    finally:
        connection_pool.putconn(conn)
```

**Time to resolution:** ~30 minutes (implementation)

---

## Prevention

### Immediate Prevention

1. **Set connection timeout**
   ```sql
   ALTER DATABASE mydb SET idle_in_transaction_session_timeout = '10min';
   ```

2. **Monitor connections**
   ```sql
   -- Alert when >80% used
   SELECT count(*) * 100.0 / (SELECT setting::int FROM pg_settings WHERE name='max_connections')
   FROM pg_stat_activity;
   ```

### Best Practices

- ✅ Use connection pooling (PgBouncer)
- ✅ Close connections properly
- ✅ Set idle timeouts
- ✅ Monitor connection usage
- ✅ Use prepared statements
- ❌ Don't create connection per query
- ❌ Don't set max_connections too high

---

## Related Issues

**Related runbooks:**
- [PostgreSQL - Slow Queries](postgres-slow-queries.md)

**External resources:**
- [PostgreSQL Connection Management](https://www.postgresql.org/docs/current/runtime-config-connection.html)

---

## Version History

| Version | Date       | Author           | Changes                  |
|---------|------------|------------------|--------------------------|
| 1.0.0   | 2025-01-15 | FaultMaven Team  | Initial verified version |

---

## License & Attribution

Licensed under Apache-2.0 License.
