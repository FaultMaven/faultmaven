---
id: redis-out-of-memory
title: "Redis - Out of Memory"
technology: redis
severity: high
tags:
  - redis
  - memory
  - eviction
  - oom
difficulty: intermediate
version: "1.0.0"
last_updated: "2025-01-15"
verified_by: "FaultMaven Team"
status: verified
---

# Redis - Out of Memory

> **Purpose**: Resolve Redis memory exhaustion and eviction issues

## Quick Reference Card

**🔍 Symptoms:**
- OOM errors in Redis logs
- Keys being evicted
- Write operations failing
- Performance degradation

**⚡ Common Causes:**
1. **Memory limit too low** (40%) - maxmemory set below actual needs
2. **No eviction policy** (30%) - Redis hits limit but can't evict
3. **Memory leak** (20%) - Keys accumulating without expiration
4. **Large keys** (10%) - Individual keys consuming too much memory

**🚀 Quick Fix:**
```bash
# Check current memory usage
redis-cli INFO memory

# Increase maxmemory (temporary)
redis-cli CONFIG SET maxmemory 2gb
```

**⏱️ Estimated Resolution Time:** 10-20 minutes

---

## Diagnostic Steps

### Step 1: Check Memory Usage

```bash
# View memory statistics
redis-cli INFO memory

# Key metrics to check:
# used_memory_human: Actual memory used
# maxmemory_human: Maximum configured
# used_memory_peak_human: Peak usage
# mem_fragmentation_ratio: Fragmentation

# Check specific values
redis-cli INFO memory | grep -E 'used_memory|maxmemory|fragmentation'
```

### Step 2: Identify Large Keys

```bash
# Find largest keys (scan-based, safe)
redis-cli --bigkeys

# Or manually check key sizes
redis-cli DEBUG OBJECT <key-name>

# Check memory usage by key pattern
redis-cli --memkeys --memkeys-samples 1000
```

### Step 3: Check Eviction Policy

```bash
# Current eviction policy
redis-cli CONFIG GET maxmemory-policy

# Check evicted keys count
redis-cli INFO stats | grep evicted_keys
```

---

## Solutions

### Solution 1: Increase Memory Limit

**When to use:** Current limit is insufficient for workload

```bash
# Check current limit
redis-cli CONFIG GET maxmemory

# Increase limit (runtime)
redis-cli CONFIG SET maxmemory 4gb

# Make permanent in redis.conf
sudo nano /etc/redis/redis.conf
# Add/modify:
maxmemory 4gb

# Restart Redis
sudo systemctl restart redis
```

**Time to resolution:** ~5 minutes

### Solution 2: Configure Eviction Policy

**When to use:** No eviction policy set or wrong policy

```bash
# Set appropriate eviction policy
redis-cli CONFIG SET maxmemory-policy allkeys-lru

# Common policies:
# allkeys-lru: Evict least recently used keys
# volatile-lru: Evict LRU keys with TTL
# allkeys-lfu: Evict least frequently used
# volatile-ttl: Evict keys with shortest TTL
# noeviction: Return errors when full (default)

# Make permanent
echo "maxmemory-policy allkeys-lru" | sudo tee -a /etc/redis/redis.conf

# Restart
sudo systemctl restart redis
```

**Time to resolution:** ~5 minutes

### Solution 3: Clean Up Old Keys

**When to use:** Keys accumulating without expiration

```bash
# Find keys without expiration
redis-cli KEYS * | while read key; do
  ttl=$(redis-cli TTL "$key")
  if [ "$ttl" -eq "-1" ]; then
    echo "$key has no expiration"
  fi
done

# Set expiration on keys (example: 7 days)
redis-cli EXPIRE mykey 604800

# Delete old/unused keys
redis-cli DEL old-key-1 old-key-2

# Flush database if appropriate (CAUTION!)
redis-cli FLUSHDB  # Current database
redis-cli FLUSHALL  # All databases
```

**Time to resolution:** ~15 minutes

### Solution 4: Optimize Data Storage

**When to use:** Inefficient data structures or large values

```bash
# Use appropriate data structures
# Instead of many individual keys:
# SET user:1:name "John"
# SET user:1:email "john@example.com"

# Use hashes:
# HSET user:1 name "John" email "john@example.com"

# Compress values (application-level)
# Use MessagePack, Protobuf, or compression

# Set shorter expiration times
redis-cli EXPIRE session:* 3600  # 1 hour
```

**Time to resolution:** Varies (development work)

---

## Prevention

### Immediate Prevention

1. **Monitor memory usage**
   ```bash
   # Alert when memory >80%
   watch redis-cli INFO memory | grep used_memory_human
   ```

2. **Set reasonable maxmemory**
   ```conf
   # 75% of available RAM
   maxmemory 3gb
   maxmemory-policy allkeys-lru
   ```

3. **Use expiration for temp data**
   ```bash
   # Set TTL on all temporary keys
   redis-cli SETEX mykey 3600 "value"
   ```

### Best Practices

- ✅ Set maxmemory to prevent OOM
- ✅ Choose appropriate eviction policy
- ✅ Monitor memory usage continuously
- ✅ Use TTL for temporary data
- ✅ Use appropriate data structures
- ✅ Consider Redis Cluster for scaling
- ❌ Don't use noeviction in production
- ❌ Don't store large values (>1MB)

---

## Related Issues

**Related runbooks:**
- [Redis - Connection Refused](redis-connection-refused.md)

**External resources:**
- [Redis Memory Optimization](https://redis.io/docs/management/optimization/memory-optimization/)
- [Eviction Policies](https://redis.io/docs/reference/eviction/)

---

## Version History

| Version | Date       | Author           | Changes                  |
|---------|------------|------------------|--------------------------|
| 1.0.0   | 2025-01-15 | FaultMaven Team  | Initial verified version |

---

## License & Attribution

Licensed under Apache-2.0 License.
