# PostgreSQL Runbooks

Troubleshooting guides for common PostgreSQL issues.

## Available Runbooks

- [Connection Pool Exhausted](postgres-connection-pool-exhausted.md) - Too many connections
- [Slow Queries](postgres-slow-queries.md) - Query performance issues

## Common Diagnostic Commands

```bash
# Connect to PostgreSQL
psql -U <username> -d <database>

# Check active connections
SELECT count(*) FROM pg_stat_activity;

# View current queries
SELECT pid, query, state FROM pg_stat_activity WHERE state = 'active';

# Check slow queries
SELECT query, calls, total_time, mean_time
FROM pg_stat_statements
ORDER BY mean_time DESC
LIMIT 10;

# View database size
SELECT pg_database.datname, pg_size_pretty(pg_database_size(pg_database.datname))
FROM pg_database;

# Check table bloat
SELECT schemaname, tablename, pg_size_pretty(pg_total_relation_size(schemaname||'.'||tablename))
FROM pg_tables
ORDER BY pg_total_relation_size(schemaname||'.'||tablename) DESC;
```

## Prerequisites

Most PostgreSQL runbooks assume you have:
- `psql` client installed
- Database credentials
- `pg_stat_statements` extension enabled (for query analysis)

## Related Resources

- [PostgreSQL Official Documentation](https://www.postgresql.org/docs/)
- [PostgreSQL Performance](https://www.postgresql.org/docs/current/performance-tips.html)
- [Query Tuning](https://www.postgresql.org/docs/current/using-explain.html)
