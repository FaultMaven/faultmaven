# Redis Runbooks

Troubleshooting guides for common Redis issues.

## Available Runbooks

- [Connection Refused](redis-connection-refused.md) - Cannot connect to Redis
- [Out of Memory](redis-out-of-memory.md) - Redis memory exhaustion

## Common Diagnostic Commands

```bash
# Check Redis status
redis-cli ping

# View Redis info
redis-cli info

# Check memory usage
redis-cli info memory

# Check connected clients
redis-cli client list

# Monitor Redis commands in real-time
redis-cli monitor

# Check maxmemory configuration
redis-cli config get maxmemory
```

## Prerequisites

Most Redis runbooks assume you have:
- `redis-cli` installed
- Network access to Redis server
- Appropriate authentication credentials

## Related Resources

- [Redis Official Documentation](https://redis.io/documentation)
- [Redis Administration](https://redis.io/topics/admin)
- [Redis Memory Optimization](https://redis.io/topics/memory-optimization)
