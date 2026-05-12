---
id: "es-search-latency"
title: "Elasticsearch Search Latency Spikes"
domain: database
service: elasticsearch
symptom_class: [latency, oom]
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-05-12"
verified_by: "kb-researcher"
status: draft
tags: [elasticsearch, latency, gc-pressure, circuit-breaker, slow-queries, heap, thread-pool]
difficulty: intermediate
---

## Symptom Recognition

- Search API response times increase from milliseconds to seconds or time out
- Application-side query errors with HTTP `429` and message containing `circuit_breaking_exception`
- `_cat/nodes` shows `heap.percent` above 75% on one or more data nodes
- `_cat/thread_pool/search` shows non-zero and growing `rejected` count
- GC logs contain stop-the-world old-generation pauses exceeding 200 ms
- `_nodes/stats` shows elevated `search.query_time_in_millis` growth rate
- Kibana dashboards load slowly or time out on aggregation-heavy panels
- Queries that were previously fast now slow across all indices simultaneously (systemic resource pressure)

## Applicability

Applies to self-managed Elasticsearch 7.x and 8.x clusters, Elastic Cloud, and cloud-provider managed services (Amazon OpenSearch Service, GCP Elasticsearch). Requires access to the Elasticsearch REST API on port 9200 (or 9243 for TLS), read access to search slow log files on data nodes, and JVM/GC log access. Kibana Search Profiler (6.4+) is optional but aids profile visualization.

## Diagnostic Steps

### Step 1: Check cluster health and per-node resource utilization

```bash
curl -s "http://localhost:9200/_cluster/health?pretty"
curl -s "http://localhost:9200/_cat/nodes?v&h=name,heap.percent,ram.percent,cpu,load_1m,disk.used_percent,node.role"
```

Expected output: cluster health JSON with `status` field; node table with heap, CPU, disk, and load columns per node.

### Step 2: Check search thread pool rejections

```bash
curl -s "http://localhost:9200/_cat/thread_pool/search?v&h=node_name,active,queue,rejected,completed"
```

Expected output: one row per node; `rejected` column shows cumulative rejections since node start.

### Step 3: Check JVM heap usage and GC activity

```bash
curl -s "http://localhost:9200/_nodes/stats/jvm?pretty" | grep -E "heap_used_percent|collection_count|collection_time_in_millis"
```

Expected output: per-node `heap_used_percent` and old-generation GC `collection_count` with cumulative `collection_time_in_millis`.

### Step 4: Check circuit breaker status

```bash
curl -s "http://localhost:9200/_nodes/stats/breaker?pretty"
```

Expected output: per-node breaker objects each with `limit_size`, `estimated_size`, and `tripped` counter for `request`, `fielddata`, `in_flight_requests`, and `parent` breakers.

### Step 5: Enable and review the search slow log

```bash
curl -X PUT "http://localhost:9200/<INDEX_NAME>/_settings?pretty" \
  -H 'Content-Type: application/json' -d '{
    "index.search.slowlog.threshold.query.warn": "5s",
    "index.search.slowlog.threshold.query.info": "2s",
    "index.search.slowlog.threshold.fetch.warn": "1s",
    "index.search.slowlog.threshold.fetch.info": "500ms"
  }'
tail -50 /var/log/elasticsearch/<CLUSTER_NAME>_index_search_slowlog.json
```

Expected output: JSON log entries with `took_millis`, shard ID, index name, and `source` containing the full query body.

### Step 6: Profile a specific slow query

```bash
curl -s -X GET "http://localhost:9200/<INDEX_NAME>/_search?pretty" \
  -H 'Content-Type: application/json' -d '{
    "profile": true,
    "query": { "match": { "<FIELD>": "<VALUE>" } }
  }'
```

Expected output: a `profile` block per shard with `time_in_nanos` and `breakdown` for each query component (`create_weight`, `build_scorer`, `next_doc`, `score`, `advance`, `match`).

### Step 7: Check field data memory usage

```bash
curl -s "http://localhost:9200/_nodes/stats/indices/fielddata?pretty" | grep -E "memory_size|evictions"
curl -s "http://localhost:9200/_cat/fielddata?v&format=json" | head -30
```

Expected output: per-node field data heap consumption broken down by field name; `evictions` counter shows cache pressure.

### Step 8: Check segment counts on top indices

```bash
curl -s "http://localhost:9200/_cat/indices?v&h=index,docs.count,store.size,segments.count&s=segments.count:desc" | head -20
```

Expected output: index table sorted by segment count descending; read-only historical indices with hundreds of segments per shard indicate missing force-merge.

## Causes

### Cause A: Expensive query pattern

**Statement:** A query using leading wildcards, regex, unbounded terms aggregations, or Painless scripts causes full index scans, consuming disproportionate CPU and heap on every execution.

**Mechanism:** Leading-wildcard and regex queries rewrite into a union of every matching term in the inverted index before scoring, scaling with index cardinality rather than result set size. Deep unbounded `terms` aggregations load all bucket values into the request circuit breaker scope. Each execution amplifies resource consumption across all shards touched by the scatter-gather.

**Indicator:**

- [Step 5] slow log entries repeat the same query `source` pattern across multiple shards
- [Step 6] `build_scorer` or `next_doc` `time_in_nanos` dominates the profile breakdown for the slow query

<!-- match: {"step": 5, "predicate": "contains", "target": "wildcard"} -->
<!-- match: {"step": 5, "predicate": "contains", "target": "script"} -->

**Mitigation:**

- **Risk:** Query changes may alter result relevance; test in staging before deploying.
- **Command:**

  ```bash
  # Replace leading-wildcard with match on an analyzed field
  curl -s -X GET "http://localhost:9200/<INDEX>/_search?pretty" \
    -H 'Content-Type: application/json' -d '{
      "query": { "match": { "message": "error" } }
    }'
  # Cap terms aggregation cardinality
  curl -s -X GET "http://localhost:9200/<INDEX>/_search?pretty" \
    -H 'Content-Type: application/json' -d '{
      "size": 0,
      "aggs": { "top_terms": { "terms": { "field": "status.keyword", "size": 100 } } }
    }'
  ```

- **Duration:** Immediate once the new query is deployed.

**Resolution:**

```bash
# Set cluster-wide max_buckets safety net (ES 7.0+)
curl -X PUT "http://localhost:9200/_cluster/settings?pretty" \
  -H 'Content-Type: application/json' -d '{
    "persistent": {
      "search.max_buckets": 10000
    }
  }'
```

- **Impact:** Cluster-wide; any aggregation exceeding 10 000 buckets returns a `too_many_buckets_exception`. No restart required.
- **Rollback:** Set `search.max_buckets` to `null` to restore the default.

**Verification:** Run `curl -s "http://localhost:9200/_nodes/stats/indices/search?pretty" | grep query_time` before and after; confirm `query_time_in_millis` growth rate is lower after query change is deployed.

---

### Cause B: JVM heap pressure and GC pauses

**Statement:** Sustained JVM heap utilization above 75% triggers frequent stop-the-world old-generation garbage collection pauses that directly add to query response time.

**Mechanism:** Elasticsearch holds segment metadata, field data caches, query results, and request buffers in heap. When the old generation fills, the G1GC collector stops all JVM threads (stop-the-world) for tens to hundreds of milliseconds to reclaim space. Queries in flight during a pause accumulate wall-clock latency equal to the GC pause duration. A heap flatlined above 85–90% indicates GC thrashing where the collector runs continuously but cannot free enough memory to reduce pressure.

**Indicator:**

- [Step 3] `heap_used_percent` above 75 on any data node
- [Step 3] old-generation `collection_time_in_millis` increasing rapidly (more than 5 collections per minute)

<!-- match: {"step": 3, "predicate": "threshold", "target": "heap_used_percent", "op": ">", "value": 75} -->

**Mitigation:**

- **Risk:** Heap above 31 GB loses compressed ordinary object pointers (OOPs), reducing effective addressable memory per byte. Never exceed 50% of physical RAM; the other half is required for the OS file system cache used for segment reads.
- **Command:**

  ```bash
  # Edit /etc/elasticsearch/jvm.options — set both to the same value
  # -Xms16g
  # -Xmx16g
  # Rolling restart one node at a time:
  sudo systemctl restart elasticsearch
  ```

- **Duration:** Takes effect after node restart; complete rolling restart before declaring resolved.

**Resolution:**

```bash
# Add coordinating-only nodes to absorb scatter-gather heap cost
# Then rebalance shards to reduce per-node shard density:
curl -s "http://localhost:9200/_cat/allocation?v&h=node,shards,disk.used_percent"
```

- **Impact:** Adding nodes requires cluster topology change. Reducing heap per-node shard count requires shard rebalancing (automatic after node addition).
- **Rollback:** Remove added nodes via shrink API or decommission procedure.

**Verification:** `curl -s "http://localhost:9200/_nodes/stats/jvm?pretty" | grep heap_used_percent` — confirm all nodes below 75% and sustained over 30 minutes of peak traffic.

---

### Cause C: Circuit breaker tripped

**Statement:** A circuit breaker (`request`, `fielddata`, or `parent`) has tripped, rejecting memory-intensive operations to prevent OOM, causing `429` errors and apparent latency.

**Mechanism:** The parent circuit breaker (default 95% of real heap) acts as a combined limit across all child breakers. When the `request` breaker (60% of heap) trips on a large aggregation, or the `fielddata` breaker (40% of heap) trips on field data loading, Elasticsearch rejects those operations with `CircuitBreakingException`. The remaining non-rejected queries complete normally, but applications without proper retry logic surface these as high-latency or timeout errors.

**Indicator:**

- [Step 4] `tripped` counter is non-zero on any breaker for any node
- [Symptom] HTTP 429 responses with body containing `circuit_breaking_exception`

<!-- match: {"step": 4, "predicate": "contains", "target": "\"tripped\" : 1"} -->

**Mitigation:**

- **Risk:** Lowering the request breaker limit causes more queries to be rejected with 429 but protects the remaining queries from OOM. Application must handle `CircuitBreakingException` gracefully with backoff.
- **Command:**

  ```bash
  # Temporarily lower request breaker to fail fast expensive queries
  curl -X PUT "http://localhost:9200/_cluster/settings?pretty" \
    -H 'Content-Type: application/json' -d '{
      "transient": {
        "indices.breaker.request.limit": "40%"
      }
    }'
  # Clear field data cache if fielddata breaker tripped
  curl -X POST "http://localhost:9200/_cache/clear?fielddata=true&pretty"
  ```

- **Duration:** Immediate; transient settings persist until node restart or explicit reset.

**Resolution:**

```bash
# Convert text fields used in aggregations to keyword to eliminate field data loading
curl -X PUT "http://localhost:9200/<INDEX>/_mapping?pretty" \
  -H 'Content-Type: application/json' -d '{
    "properties": {
      "<FIELD>": {
        "type": "text",
        "fields": { "keyword": { "type": "keyword" } }
      }
    }
  }'
```

- **Impact:** Mapping changes do not backfill existing documents; reindex required for existing data. New documents immediately benefit.
- **Rollback:** Reindex to original mapping if keyword sub-field is not desired.

**Verification:** `curl -s "http://localhost:9200/_nodes/stats/breaker?pretty" | grep tripped` — confirm `tripped` counters stop increasing and 429 error rate drops to zero.

---

### Cause D: Search thread pool saturation

**Statement:** The search thread pool is exhausted, causing incoming queries to queue and eventually be rejected, producing rising latency and 429 errors.

**Mechanism:** The search thread pool defaults to `(vCPU * 3 / 2) + 1` threads with a queue of 1000. Each search request touching N primary shards consumes N thread slots on the coordinating node's thread pool during the scatter-gather phase. Indices with excessive primary shards amplify thread consumption per query. When the queue fills, Elasticsearch rejects new search requests immediately with a `rejected` error rather than queuing indefinitely.

**Indicator:**

- [Step 2] `rejected` column non-zero and increasing across polling intervals
- [Step 2] `queue` column consistently above 500 during peak traffic

<!-- match: {"step": 2, "predicate": "threshold", "target": "rejected", "op": ">", "value": 0} -->

**Mitigation:**

- **Risk:** Increasing queue size delays timeout detection; clients wait longer before failing. Coordinate with application teams on timeout settings before increasing.
- **Command:**

  ```bash
  # Increase search queue size (dynamic in ES 7.x+)
  curl -X PUT "http://localhost:9200/_cluster/settings?pretty" \
    -H 'Content-Type: application/json' -d '{
      "transient": {
        "thread_pool.search.queue_size": 2000
      }
    }'
  ```

- **Duration:** Immediate; buys time to investigate root cause without dropping requests.

**Resolution:**

```bash
# Add coordinating-only nodes to handle scatter-gather and free data node threads
# Reduce primary shard count on over-sharded indices using shrink API:
curl -X POST "http://localhost:9200/<INDEX>/_shrink/<SHRUNK_INDEX>?pretty" \
  -H 'Content-Type: application/json' -d '{
    "settings": { "index.number_of_shards": 1 }
  }'
```

- **Impact:** Shrink API requires the index to be read-only and all shards relocated to one node first; plan a maintenance window.
- **Rollback:** Shrink cannot be undone; retain original index until verified.

**Verification:** `curl -s "http://localhost:9200/_cat/thread_pool/search?v&h=node_name,active,queue,rejected"` — confirm `rejected` count stops increasing and `queue` stays below 200 during peak.

---

### Cause E: Excessive segment count

**Statement:** An index accumulates hundreds of segments per shard due to insufficient background merging, forcing queries to union results across all segments and increasing per-query overhead.

**Mechanism:** Elasticsearch writes new data in small Lucene segments. Background merge policies consolidate segments, but high write throughput can outpace merging. Each query must open and search every segment independently then merge results. Segment metadata (bloom filters, field stats) consumes heap proportional to count. Read-only historical indices that were never force-merged retain the high segment count from their active indexing period indefinitely.

**Indicator:**

- [Step 8] `segments.count` above 200 per shard on indices that are no longer receiving writes
- [Step 8] read-only time-based indices (e.g., Logstash `logstash-YYYY.MM.DD`) with high segment counts

<!-- match: {"step": 8, "predicate": "threshold", "target": "segments.count", "op": ">", "value": 200} -->

**Mitigation:**

- **Risk:** Force merge triggers intensive I/O. Run only during off-peak hours. Never force merge actively written indices — it interferes with normal merge policy and can create segments larger than intended.
- **Command:**

  ```bash
  # Force merge a closed/read-only index to 1 segment (off-peak only)
  curl -X POST "http://localhost:9200/<INDEX_NAME>/_forcemerge?max_num_segments=1&pretty"
  ```

- **Duration:** Minutes to hours depending on index size and disk throughput; monitor `_cat/tasks` to track progress.

**Resolution:**

```bash
# Increase refresh interval during active indexing to reduce segment creation rate
curl -X PUT "http://localhost:9200/<INDEX_NAME>/_settings?pretty" \
  -H 'Content-Type: application/json' -d '{
    "index.refresh_interval": "30s"
  }'
# Tune merge policy for actively indexed indices
curl -X PUT "http://localhost:9200/<INDEX_NAME>/_settings?pretty" \
  -H 'Content-Type: application/json' -d '{
    "index.merge.policy.segments_per_tier": 5,
    "index.merge.policy.max_merge_at_once": 5
  }'
```

**Verification:** `curl -s "http://localhost:9200/_cat/indices/<INDEX_NAME>?v&h=index,segments.count"` — confirm `segments.count` at or near 1 after force merge completes.

---

### Cause F: Deep pagination via from+size

**Statement:** Queries using `from` + `size` exceeding 10 000 force Elasticsearch to load and discard large result windows from every shard, causing memory pressure and latency proportional to the pagination depth.

**Mechanism:** Each shard must retrieve and sort all `from + size` documents before the coordinating node can select the final page. For a query with `from=9000, size=100`, every shard sends 9 100 hits to the coordinating node. With many shards, this results in gigabytes of heap allocation per query and triggers the request circuit breaker when `index.max_result_window` is set above the default 10 000.

**Indicator:**

- [Step 5] slow log entries showing `from` values above 5 000 in the query `source`
- [Step 4] `request` breaker `estimated_size` spikes correlated with pagination-heavy traffic

<!-- match: {"step": 5, "predicate": "contains", "target": "\"from\""} -->

**Mitigation:**

- **Risk:** Requires application-side change; interim mitigation is to cap `index.max_result_window` to prevent heap exhaustion.
- **Command:**

  ```bash
  # Enforce hard cap on deep pagination (returns 400 for requests exceeding limit)
  curl -X PUT "http://localhost:9200/<INDEX_NAME>/_settings?pretty" \
    -H 'Content-Type: application/json' -d '{
      "index.max_result_window": 10000
    }'
  ```

- **Duration:** Immediate; blocks new deep-pagination requests.

**Resolution:**

```bash
# Migrate to search_after with Point-in-Time for stateless cursor pagination
# Step 1: Open a PIT
curl -X POST "http://localhost:9200/<INDEX_NAME>/_pit?keep_alive=1m&pretty"
# Step 2: Use search_after with sort and pit.id from Step 1
curl -s -X GET "http://localhost:9200/_search?pretty" \
  -H 'Content-Type: application/json' -d '{
    "size": 100,
    "query": { "match_all": {} },
    "sort": [{ "@timestamp": "desc" }, { "_shard_doc": "desc" }],
    "pit": { "id": "<PIT_ID>", "keep_alive": "1m" }
  }'
```

**Verification:** Confirm no slow log entries contain `"from"` values above 1 000 and that request circuit breaker `estimated_size` stops spiking during pagination-heavy traffic windows.

---

### Cause Z: Unidentified cause [Default]

**Statement:** Search latency is elevated but the diagnostic steps do not clearly point to a single known cause.

**Mechanism:** Latency can arise from combinations of causes (e.g., moderate heap pressure plus moderate thread pool saturation), from transient cluster events (network partition, shard relocation storm), or from application-side issues (connection pool exhaustion upstream of Elasticsearch). The interaction of multiple moderate stressors may not surface as a clear single indicator.

**Indicator:**

- [Default] All Steps 1–8 completed and no single threshold or pattern from Causes A–F matched

**Mitigation:**

- **Risk:** Escalation path; no cluster changes until cause is identified.
- **Command:**

  ```bash
  # Capture a full diagnostic snapshot for escalation
  curl -s "http://localhost:9200/_cluster/stats?pretty" > /tmp/es-cluster-stats.json
  curl -s "http://localhost:9200/_nodes/stats?pretty" > /tmp/es-nodes-stats.json
  curl -s "http://localhost:9200/_tasks?detailed=true&actions=*search*&pretty" > /tmp/es-search-tasks.json
  ```

- **Duration:** Safe indefinitely; no cluster changes made.

**Resolution:** Out of runbook scope — escalate to Elasticsearch support or a platform engineer with the diagnostic snapshot files and GC log excerpts.

**Verification:** Latency returns to baseline within SLA after escalation team applies targeted fix and 24-hour monitoring period elapses.

## Prevention

1. **Profile queries before production.** Use the Profile API and slow log in staging. Set `index.search.slowlog.threshold.query.warn: 5s` via index templates so all new indices capture slow queries automatically.

2. **Size JVM heap correctly.** Allocate 50% of physical RAM to heap, maximum 31 GB to retain compressed OOPs. Set `-Xms` and `-Xmx` to the same value in `jvm.options` to prevent heap resizing pauses.

3. **Monitor GC pauses.** Alert when old-generation GC pause time exceeds 200 ms or `heap_used_percent` is sustained above 75%. Use `elasticsearch_exporter` with Prometheus for continuous GC metrics.

4. **Use keyword fields for aggregations and sorting.** Never aggregate or sort on `text` fields. Map aggregation targets as `keyword` with `doc_values` enabled (the default). Set `indices.fielddata.cache.size: 20%` to cap field data heap consumption.

5. **Keep shards per GB of heap below 20.** Excessive shard count wastes heap on segment metadata and search contexts. Use Index Lifecycle Management (ILM) to roll over, shrink, and force-merge time-series indices automatically.

6. **Alert on circuit breaker trips.** Any `tripped > 0` is an early warning of imminent OOM and correlates directly with latency spikes. Alert immediately and investigate before the breaker trips repeatedly.

7. **Use search_after for pagination.** Prohibit `from + size` above 1 000 in application code. Migrate user-facing pagination to `search_after` with PIT. Use the Scroll API only for batch reindex operations, not real-time access.

8. **Force-merge closed time-series indices.** Apply a force-merge to `max_num_segments=1` as the final ILM step before moving an index to the cold tier. This permanently eliminates per-query segment union overhead for historical data.

9. **Add dedicated coordinating nodes for heavy search workloads.** Coordinating-only nodes (no `data`, `master`, or `ingest` roles) absorb scatter-gather overhead and merge overhead, freeing data node thread pools for shard-level work.

## Sources

- [Elasticsearch Reference — Fix Common Cluster Issues](https://www.elastic.co/guide/en/elasticsearch/reference/current/fix-common-cluster-issues.html) — Priority 1; circuit breaker overview and cluster health triage
- [Elasticsearch Reference — Circuit Breaker Settings](https://www.elastic.co/guide/en/elasticsearch/reference/current/circuit-breaker.html) — Priority 1; all breaker types, default limits (parent 95%, request 60%, fielddata 40%), and configuration API
- [Elasticsearch Reference — Search Slow Log](https://www.elastic.co/guide/en/elasticsearch/reference/current/index-modules-slowlog.html) — Priority 1; threshold configuration for query and fetch phases, log format
- [Elasticsearch Reference — Profile API](https://www.elastic.co/guide/en/elasticsearch/reference/current/search-profile.html) — Priority 1; query timing breakdown phases (create_weight, build_scorer, next_doc, score), aggregation profiling
- [Elasticsearch Reference — Paginate Search Results](https://www.elastic.co/guide/en/elasticsearch/reference/current/paginate-search-results.html) — Priority 1; from+size 10 000 limit, search_after cursor, Point-in-Time API
