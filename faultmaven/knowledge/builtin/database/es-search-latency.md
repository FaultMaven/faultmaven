---
id: es-search-latency
title: "Elasticsearch Search Latency Spikes — Slow Queries, Heap Pressure, and Circuit Breakers"
domain: database
service: elasticsearch
symptom_class:
  - latency
  - oom
severity: high
scope: global
version: "2.1.0"
last_updated: "2026-03-26"
verified_by: kb-researcher
status: draft
tags:
  - elasticsearch
  - latency
  - gc-pressure
  - circuit-breaker
  - slow-queries
difficulty: intermediate
---

# Elasticsearch Search Latency Spikes — Slow Queries, Heap Pressure, and Circuit Breakers

## Problem Definition

This runbook covers Elasticsearch clusters (versions 7.x and 8.x) experiencing elevated search latency. It applies to self-managed deployments, Elastic Cloud, and cloud-provider managed services (Amazon OpenSearch, GCP Elasticsearch). You need access to the Elasticsearch REST API (port 9200), the search slow log files on each data node, the JVM/GC logs, and optionally the application-side query logs. Kibana access is helpful for visualizing profiling output but not required.

Elasticsearch search latency spikes occur when query response times increase significantly from their baseline, impacting application performance and user experience. Latency can originate from query complexity, resource pressure (heap, CPU, I/O), or cluster-level issues such as degraded health or thread pool exhaustion.

**Common symptoms:**

- Search API response times increase from milliseconds to seconds or longer
- Application timeouts on Elasticsearch queries
- `_nodes/stats` shows elevated `search.query_time_in_millis`
- Kibana dashboards and visualizations load slowly or time out
- Circuit breaker exceptions in logs: `CircuitBreakingException` with a `429` response code
- Frequent garbage collection (GC) pauses visible in GC logs (pauses > 200ms)
- Thread pool rejections in `search` and `get` pools
- Queries that were previously fast now run slow across all indices (ripple-effect slowness from systemic resource pressure)

**Common root causes:**

- Expensive queries: deep aggregations, leading wildcards, scripts, regex on large text fields
- JVM heap pressure triggering long GC pauses (stop-the-world old generation collections)
- Circuit breaker trips preventing memory-intensive operations from completing
- Too many concurrent searches exhausting the search thread pool
- Large result sets or deep pagination (`from` + `size` > 10000)
- Segment merging consuming I/O bandwidth during heavy indexing
- Field data or global ordinals loading on high-cardinality fields
- Excessive shards per node (above 20 non-frozen shards per GB of heap)
- Cold or frozen tier nodes serving queries with high I/O latency
- Cross-cluster search adding network round-trip latency
- Cluster yellow/red status causing queries to hit fewer shards

## Diagnostic Steps

### Step 1: Check cluster health and node resource metrics

**What this checks:** The overall cluster state and per-node resource metrics to rule out cluster-level issues as the latency cause.

```bash
curl -s "localhost:9200/_cluster/health?pretty"
curl -s "localhost:9200/_cat/nodes?v&h=name,heap.percent,ram.percent,cpu,load_1m,disk.used_percent,node.role"
```

**Expected output:** Cluster health JSON showing `status`, and a node table with resource utilization per node.

**What the finding means:** A yellow or red cluster reduces the number of shards available for queries, directly increasing latency on remaining shards. Nodes with `heap.percent` above 75% are under GC pressure. Nodes with `cpu` above 90% are compute-bound. If `number_of_nodes` is lower than expected, a node has departed.

### Step 2: Identify slow queries with the slow log

**What this checks:** Which specific queries are exceeding latency thresholds, including the full query body and per-shard timing breakdown.

```bash
# Enable slow log on target index (adjust thresholds to match your SLA)
curl -X PUT "localhost:9200/<INDEX_NAME>/_settings?pretty" -H 'Content-Type: application/json' -d '{
  "index.search.slowlog.threshold.query.warn": "5s",
  "index.search.slowlog.threshold.query.info": "2s",
  "index.search.slowlog.threshold.fetch.warn": "1s",
  "index.search.slowlog.threshold.fetch.info": "500ms"
}'
```

Then review the slow log:

```bash
tail -100 /var/log/elasticsearch/<CLUSTER_NAME>_index_search_slowlog.json
```

**Expected output:** JSON log entries containing the slow query body, index name, shard ID, `took_millis`, total shards searched, and source query text.

**What the finding means:** Repeated entries for the same query pattern indicate an application-level issue (bad query design). If many basic search queries appear in the slow log simultaneously, this indicates systemic resource contention rather than a query-specific problem -- all queries slow down when the cluster is under pressure.

### Step 3: Check JVM heap and GC activity

**What this checks:** Whether the JVM heap is under pressure and whether GC pauses are contributing to query latency.

```bash
curl -s "localhost:9200/_nodes/stats/jvm?pretty" | grep -E "heap_used_percent|heap_max|collection_count|collection_time"
```

**Expected output:** `heap_used_percent` for each node, and GC collection counts and cumulative time for young and old generation collectors.

**What the finding means:** If `heap_used_percent` is consistently above 75%, the JVM is under memory pressure. Old generation GC collection times exceeding 200ms are stop-the-world pauses that directly add to query latency. A heap flatlined near 85-95% indicates GC thrashing -- the collector runs continuously but cannot reclaim enough memory.

Check GC logs directly for individual pause details:

```bash
tail -50 /var/log/elasticsearch/gc.log
```

### Step 4: Check circuit breaker status

**What this checks:** Whether memory-protection circuit breakers have tripped, rejecting queries to prevent out-of-memory crashes.

```bash
curl -s "localhost:9200/_nodes/stats/breaker?pretty"
```

**Expected output:** Per-node breaker stats showing `limit_size`, `estimated_size`, and `tripped` count for each breaker type.

**What the finding means:** Key breakers and their default limits: `request` (60% of heap -- covers aggregations, sorting, and in-memory data structures), `fielddata` (40% of heap -- covers field data cache), `in_flight_requests` (100% of heap -- covers all in-flight request payloads), `parent` (95% of real heap -- combined limit, enabled by default in 7.0+). If `tripped > 0`, the breaker has rejected requests to prevent OOM. In ES 7.0+, the parent breaker uses real heap measurement (`indices.breaker.total.use_real_memory: true`), improving accuracy.

### Step 5: Check search thread pool rejections

**What this checks:** Whether the search thread pool is saturated and rejecting incoming queries.

```bash
curl -s "localhost:9200/_cat/thread_pool/search?v&h=node_name,active,queue,rejected,completed"
```

**Expected output:** A table with one row per node showing active threads, queued requests, and cumulative rejected count.

**What the finding means:** If `rejected` is non-zero and increasing, the node cannot keep up with search demand. The search thread pool size defaults to `(number_of_CPUs * 3 / 2) + 1` with a queue of 1000. Rising rejections indicate queries exceed available compute capacity. Each search hitting N shards consumes N thread pool slots, so indices with many primary shards amplify contention.

### Step 6: Profile a slow query

**What this checks:** The internal time breakdown of a specific query to identify which phase (rewrite, scoring, collection) is the bottleneck.

```bash
curl -X GET "localhost:9200/<INDEX_NAME>/_search?pretty" -H 'Content-Type: application/json' -d '{
  "profile": true,
  "query": {
    "match": { "field": "value" }
  }
}'
```

**Expected output:** A `profile` section in the response with per-shard timing for each query phase: `rewrite`, `build_scorer`, `next_doc`, `score`, `advance`, `match`.

**What the finding means:** A high `next_doc` time indicates the query is scanning many documents (missing index or overly broad filter). A high `score` time indicates expensive scoring (scripts, `function_score`). A high `build_scorer` time can indicate complex boolean queries or high segment counts. Use the Kibana Search Profiler (v6.4+) to visualize these results.

### Step 7: Check segment counts and merge activity

**What this checks:** Whether excessive segment counts on indices are degrading search performance.

```bash
curl -s "localhost:9200/_cat/indices?v&h=index,docs.count,store.size,seg.count&s=seg.count:desc" | head -20
```

**Expected output:** A table of indices sorted by segment count, highest first.

**What the finding means:** High segment counts (hundreds per shard) indicate insufficient merging. Each segment requires its own file handles and search context, and queries must union results across all segments. Actively indexed data naturally has more segments; read-only indices should be force-merged to 1 segment.

### Step 8: Check field data and global ordinals memory

**What this checks:** Whether field data loading or global ordinals construction is consuming excessive heap and adding latency.

```bash
curl -s "localhost:9200/_cat/fielddata?v&format=json" | head -50
curl -s "localhost:9200/_nodes/stats/indices/fielddata?pretty"
```

**Expected output:** Per-node field data memory usage broken down by field name.

**What the finding means:** High field data memory on `text` or high-cardinality `keyword` fields causes heap pressure and slow queries. Field data for text fields is loaded on-demand and can consume gigabytes of heap. Global ordinals for keyword fields are rebuilt on each segment refresh. High-cardinality fields (IDs, emails, usernames) used in aggregations are a common source of heap exhaustion.

## Mitigation

### Option 1: Optimize expensive queries

Use when the slow log identifies specific query patterns causing latency.

- **Risk:** Low. Query changes may affect result relevance; test in staging before deploying to production.
- **Command:**

```bash
# Example: Replace leading-wildcard queries with more efficient alternatives
# Before (expensive — scans every term in the inverted index):
# { "query": { "wildcard": { "message": "*error*" } } }

# After (efficient — uses the analyzer to match tokens):
# { "query": { "match": { "message": "error" } } }

# Limit aggregation cardinality with explicit size
curl -X GET "localhost:9200/<INDEX>/_search?pretty" -H 'Content-Type: application/json' -d '{
  "size": 0,
  "aggs": {
    "top_terms": {
      "terms": { "field": "status.keyword", "size": 100 }
    }
  }
}'
```

- **Verify:**

```bash
curl -s "localhost:9200/_nodes/stats/indices/search?pretty" | grep "query_time"
# Expected: query_time_in_millis growth rate decreases
```

- **Duration:** Immediate after query change is deployed.

### Option 2: Increase JVM heap (if under-allocated)

Use when `heap_used_percent` is consistently above 75% and the node has available physical RAM.

- **Risk:** Moderate. Heap above 31 GB loses compressed ordinary object pointers (OOPs), which can reduce effective memory. Never exceed 50% of physical RAM -- the other half is needed for the OS file system cache which Elasticsearch relies on for segment reads. Requires a node restart.
- **Command:**

```bash
# Edit jvm.options (e.g., /etc/elasticsearch/jvm.options)
# Set both to the same value:
# -Xms16g
# -Xmx16g
# Then restart the node:
sudo systemctl restart elasticsearch
```

- **Verify:**

```bash
curl -s "localhost:9200/_nodes/stats/jvm?pretty" | grep "heap_max_in_bytes"
# Expected: reflects the new heap size
```

- **Duration:** Minutes (one node restart per rolling upgrade cycle).

### Option 3: Clear field data cache

Use when field data memory is consuming excessive heap.

- **Risk:** Low. Queries will temporarily slow down while field data is reloaded on demand for subsequent queries.
- **Command:**

```bash
curl -X POST "localhost:9200/_cache/clear?fielddata=true&pretty"
```

- **Verify:**

```bash
curl -s "localhost:9200/_nodes/stats/indices/fielddata?pretty" | grep "memory_size"
# Expected: memory_size_in_bytes significantly reduced
```

- **Duration:** Immediate.

### Option 4: Reduce concurrent search load

Use when search thread pool rejections are high and queries are being dropped.

- **Risk:** Moderate. Increasing the queue size delays timeout detection; queries wait longer before failing. Coordinate with application teams on timeout settings.
- **Command:**

```bash
# Increase search queue size temporarily (requires ES restart on some versions)
# For dynamic update (ES 7.x+):
curl -X PUT "localhost:9200/_cluster/settings?pretty" -H 'Content-Type: application/json' -d '{
  "transient": {
    "thread_pool.search.queue_size": 2000
  }
}'
```

- **Verify:**

```bash
curl -s "localhost:9200/_cat/thread_pool/search?v&h=node_name,active,queue,rejected"
# Expected: rejected count stops increasing
```

- **Duration:** Immediate.

### Option 5: Force merge segments (off-peak only)

Use when segment counts are very high on indices that are no longer being written to (e.g., time-based indices from previous periods).

- **Risk:** High I/O impact. Run during off-peak hours only. Do not force merge indices that are actively receiving writes, as it interferes with the normal merge process and can cause larger segments than intended.
- **Command:**

```bash
curl -X POST "localhost:9200/<INDEX_NAME>/_forcemerge?max_num_segments=1&pretty"
```

- **Verify:**

```bash
curl -s "localhost:9200/_cat/indices/<INDEX_NAME>?v&h=index,seg.count"
# Expected: seg.count reduced to 1
```

- **Duration:** Minutes to hours depending on index size and I/O throughput.

### Option 6: Lower circuit breaker limits to fail fast

Use when expensive queries consume heap and cause cascading slowdowns for all other queries on the node.

- **Risk:** Moderate. More queries will be rejected with `429` errors, but the remaining queries will complete faster. Application must handle `CircuitBreakingException` gracefully.
- **Command:**

```bash
curl -X PUT "localhost:9200/_cluster/settings?pretty" -H 'Content-Type: application/json' -d '{
  "transient": {
    "indices.breaker.request.limit": "40%",
    "indices.breaker.request.overhead": 2
  }
}'
```

- **Verify:**

```bash
curl -s "localhost:9200/_nodes/stats/breaker?pretty" | grep -E "request.*tripped|request.*limit"
# Expected: expensive queries fail fast; overall latency for other queries improves
```

- **Duration:** Immediate.

## Root Cause Resolution

**If** slow queries involve leading-wildcard or regex patterns on large text fields --> replace with `match` queries on analyzed fields, or use `keyword` sub-fields with `term` queries. For substring search, consider using an `ngram` tokenizer at index time.

**If** deep aggregations on high-cardinality fields cause heap pressure --> pre-compute aggregations using transform jobs, or use composite aggregations with pagination instead of single unbounded terms aggregations. Set `search.max_buckets` (default 10,000 in 7.0+) to prevent runaway aggregations.

**If** JVM heap is consistently above 75% --> scale out by adding data nodes to distribute the shard load, or increase heap on existing nodes (up to 31 GB to retain compressed OOPs). Maintain fewer than 20 non-frozen shards per GB of configured heap.

**If** circuit breaker trips on `fielddata` --> convert text fields used for sorting/aggregation to `keyword` type. Use `doc_values` (enabled by default for keyword fields) instead of field data. Set `indices.fielddata.cache.size` to limit heap consumption.

**If** GC pauses exceed 200ms --> tune GC settings. For Elasticsearch 7.x+, G1GC is the default; ensure `-XX:MaxGCPauseMillis=200` is set. Reduce heap pressure by limiting field data, reducing concurrent operations, and scaling out. Switch from CMS to G1GC if still on older JVM settings.

**If** search thread pool is saturated --> add coordinating-only nodes to handle search scatter-gather overhead, freeing data nodes for shard-level operations. Reduce the number of primary shards per index to limit per-query thread consumption. Implement request queuing and timeout limits at the application layer.

**If** deep pagination (`from` + `size` > 10000) --> replace with `search_after` for efficient cursor-based pagination, or use the Point-in-Time (PIT) API with `search_after` for consistent pagination across refreshes.

**If** segment counts are high on actively indexed indices --> tune the merge policy via `index.merge.policy.segments_per_tier` and `index.merge.policy.max_merge_at_once`. Ensure sufficient I/O bandwidth for background merging. Increase `index.refresh_interval` to 30s during heavy indexing to reduce segment creation rate.

## Verification

After applying fixes, confirm latency has improved:

```bash
# 1. Check search latency metrics
curl -s "localhost:9200/_nodes/stats/indices/search?pretty" | grep -E "query_total|query_time"
# Expected: query_time_in_millis growth rate is lower than before

# 2. Verify heap is healthy
curl -s "localhost:9200/_nodes/stats/jvm?pretty" | grep "heap_used_percent"
# Expected: below 75%

# 3. Check circuit breakers have not tripped recently
curl -s "localhost:9200/_nodes/stats/breaker?pretty" | grep "tripped"
# Expected: 0 or not increasing

# 4. Verify no thread pool rejections
curl -s "localhost:9200/_cat/thread_pool/search?v&h=node_name,rejected"
# Expected: rejected count stable (not increasing)

# 5. Run a representative search query and measure timing
time curl -s "localhost:9200/<INDEX>/_search?pretty" -H 'Content-Type: application/json' -d '{
  "query": { "match_all": {} },
  "size": 10
}'
# Expected: response within expected SLA (typically < 200ms for simple queries)

# 6. Check slow log for new entries
tail -10 /var/log/elasticsearch/<CLUSTER_NAME>_index_search_slowlog.json
# Expected: no new entries above warning threshold
```

Monitor for at least 24 hours across peak traffic periods to confirm the improvement is sustained.

## Prevention

1. **Profile queries before production** -- Use the Profile API and slow log in staging to identify expensive queries before they impact production workloads.

2. **Set search slow log thresholds on all indices** -- Configure slow log thresholds via index templates to automatically apply to all new indices. Start with `query.warn: 5s` and `query.info: 2s`, then tighten as baselines are established.

3. **Size JVM heap correctly** -- Allocate 50% of physical RAM to heap (max 31 GB for compressed OOPs), leave the other 50% for the OS file system cache which Elasticsearch relies on heavily for segment reads.

4. **Monitor GC metrics** -- Alert when GC pause time exceeds 200ms or old generation collections exceed 5 per minute. Use Prometheus with `elasticsearch_exporter` for continuous monitoring.

5. **Use keyword fields for aggregations and sorting** -- Never aggregate or sort on `text` fields. Use `keyword` sub-fields with `doc_values` enabled (the default for keyword mappings).

6. **Implement pagination with search_after** -- Avoid deep pagination. Use `search_after` with Point-in-Time for user-facing pagination and the Scroll API for batch export processing.

7. **Scale with coordinating nodes** -- Add dedicated coordinating nodes to handle search scatter-gather overhead, freeing data nodes for shard-level search and indexing.

8. **Implement circuit breaker alerts** -- Alert when any circuit breaker trips. This is an early warning of imminent OOM risk and correlates with latency spikes.

9. **Use Index Lifecycle Management** -- Automatically move old indices to warm/cold tiers with fewer replicas and force-merged segments to reduce active resource consumption.

10. **Avoid unbounded aggregations** -- Always set explicit `size` limits on terms aggregations. Use composite aggregations for paginated results over high-cardinality fields. Configure `search.max_buckets` as a safety net.

11. **Right-size shards per node** -- Maintain fewer than 20 non-frozen shards per GB of configured heap. Excessive shards waste heap on segment metadata and search contexts.

## Sources

- [Elastic Blog — Advanced Tuning: Finding and Fixing Slow Elasticsearch Queries](https://www.elastic.co/blog/advanced-tuning-finding-and-fixing-slow-elasticsearch-queries)
- [Elastic Blog — A Heap of Trouble: Managing Elasticsearch's Managed Heap](https://www.elastic.co/blog/a-heap-of-trouble)
- [Elasticsearch Reference — Fix Common Cluster Issues](https://www.elastic.co/guide/en/elasticsearch/reference/current/fix-common-cluster-issues.html)
- [Elasticsearch Reference — Search Slow Log](https://www.elastic.co/guide/en/elasticsearch/reference/current/index-modules-slowlog.html)
- [Elasticsearch Reference — Circuit Breaker Settings](https://www.elastic.co/guide/en/elasticsearch/reference/current/circuit-breaker.html)
- [Elasticsearch Reference — Profile API](https://www.elastic.co/guide/en/elasticsearch/reference/current/search-profile.html)
- [Elasticsearch Reference — Paginate Search Results](https://www.elastic.co/guide/en/elasticsearch/reference/current/paginate-search-results.html)
- [Opster — Elasticsearch Search Latency Guide](https://opster.com/guides/elasticsearch/how-tos/search-latency-guide/)
