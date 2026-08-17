---
id: "es-search-latency"
title: "Elasticsearch Search Latency Spikes"
domain: database
service: elasticsearch
symptom_class: [latency, oom]
severity: high
scope: global
version: "2.0.0"
last_updated: "2026-06-25"
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

**Statement:** A query using leading wildcards, regex, unbounded terms aggregations, or Painless scripts forces full index scans, consuming disproportionate CPU and heap on every execution.

**Chain:**
- root: A query uses a leading wildcard, regex, unbounded `terms` aggregation, or Painless script.
- s1: The query rewrites into a union of every matching term, scaling with index cardinality rather than result-set size.
- s2: Each scatter-gather execution amplifies CPU and heap consumption across all shards touched.
- D: Search response times rise to seconds or time out (points at Symptom Recognition).

**Indicators:**
- root: [Step 5] slow log entries repeat the same query `source` pattern across multiple shards
- s2: [Step 6] `build_scorer` or `next_doc` `time_in_nanos` dominates the profile breakdown for the slow query

**Interventions:**
- **remediation** (root): Set a cluster-wide `max_buckets` safety net (ES 7.0+) so any aggregation exceeding 10 000 buckets returns `too_many_buckets_exception`. No restart required. Rollback: set `search.max_buckets` to `null` to restore the default.

  ```bash
  curl -X PUT "http://localhost:9200/_cluster/settings?pretty" \
    -H 'Content-Type: application/json' -d '{
      "persistent": {
        "search.max_buckets": 10000
      }
    }'
  ```

  **Verification:** Run `curl -s "http://localhost:9200/_nodes/stats/indices/search?pretty" | grep query_time` before and after; confirm `query_time_in_millis` growth rate is lower after the query change is deployed.
- **defensive_fix** (s1): Rewrite the leading-wildcard query as a `match` on an analyzed field and cap `terms` aggregation cardinality. Test in staging first — query changes may alter result relevance. Effective immediately once the new query is deployed.

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

  **Verification:** Re-run Step 6; confirm `build_scorer`/`next_doc` no longer dominate the profile breakdown.

---

### Cause B: JVM heap pressure and GC pauses

**Statement:** Sustained JVM heap utilization above 75% triggers frequent stop-the-world old-generation garbage collection pauses that directly add to query response time.

**Chain:**
- root: JVM heap utilization is sustained above 75% on a data node.
- s1: Segment metadata, field data caches, query results, and request buffers fill the old generation.
- s2: The G1GC collector stops all JVM threads for tens to hundreds of milliseconds to reclaim space; above 85–90% it thrashes continuously without freeing enough memory.
- s3: Queries in flight during a pause accumulate wall-clock latency equal to the GC pause duration.
- D: Search response times rise to seconds or time out (points at Symptom Recognition).

**Indicators:**
- root: [Step 3] `heap_used_percent` above 75 on any data node
- s2: [Step 3] old-generation `collection_time_in_millis` increasing rapidly (more than 5 collections per minute)

**Interventions:**
- **remediation** (root): Add coordinating-only nodes to absorb scatter-gather heap cost, then rebalance shards (automatic after node addition) to reduce per-node shard density. Adding nodes is a topology change. Rollback: remove added nodes via shrink API or decommission procedure.

  ```bash
  curl -s "http://localhost:9200/_cat/allocation?v&h=node,shards,disk.used_percent"
  ```

  **Verification:** `curl -s "http://localhost:9200/_nodes/stats/jvm?pretty" | grep heap_used_percent` — confirm all nodes below 75% and sustained over 30 minutes of peak traffic.
- **mitigation** (root): Raise the JVM heap by setting `-Xms`/`-Xmx` to the same value in `jvm.options`, then rolling-restart one node at a time.

  ```bash
  # Edit /etc/elasticsearch/jvm.options — set both to the same value
  # -Xms16g
  # -Xmx16g
  # Rolling restart one node at a time:
  sudo systemctl restart elasticsearch
  ```

  **Risk:** Heap above 31 GB loses compressed ordinary object pointers (OOPs), reducing effective addressable memory per byte. Never exceed 50% of physical RAM; the other half is required for the OS file system cache used for segment reads. **Duration:** Takes effect after node restart; complete the rolling restart before declaring resolved. **Verification:** re-run Step 3; confirm `heap_used_percent` drops below 75 on the restarted nodes.

---

### Cause C: Circuit breaker tripped

**Statement:** A circuit breaker (`request`, `fielddata`, or `parent`) has tripped, rejecting memory-intensive operations to prevent OOM, causing `429` errors and apparent latency.

**Chain:**
- root: A large aggregation or field data load exceeds a breaker limit (request 60%, fielddata 40%, or parent 95% of heap).
- s1: Elasticsearch rejects the offending operation with `CircuitBreakingException` to prevent OOM.
- s2: Applications without proper retry/backoff logic surface the rejection as high-latency or timeout errors.
- D: Application sees HTTP `429` with `circuit_breaking_exception` and apparent latency (points at Symptom Recognition).

**Indicators:**
- root: [Step 4] `tripped` counter is non-zero on any breaker for any node
- s2: [Symptom] HTTP 429 responses with body containing `circuit_breaking_exception`

**Interventions:**
- **remediation** (root): Convert text fields used in aggregations to `keyword` to eliminate field data loading. Mapping changes do not backfill existing documents (reindex required); new documents benefit immediately. Rollback: reindex to original mapping if the keyword sub-field is not desired.

  ```bash
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

  **Verification:** `curl -s "http://localhost:9200/_nodes/stats/breaker?pretty" | grep tripped` — confirm `tripped` counters stop increasing and the 429 error rate drops to zero.
- **mitigation** (s1): Temporarily lower the request breaker to fail expensive queries fast, and clear the field data cache if the fielddata breaker tripped.

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

  **Risk:** Lowering the request breaker limit rejects more queries with 429 but protects the rest from OOM; the application must handle `CircuitBreakingException` gracefully with backoff. **Duration:** Immediate; transient settings persist until node restart or explicit reset. **Verification:** re-run Step 4; confirm the offending breaker stops tripping.

---

### Cause D: Search thread pool saturation

**Statement:** The search thread pool is exhausted, causing incoming queries to queue and eventually be rejected, producing rising latency and 429 errors.

**Chain:**
- root: Indices with excessive primary shards (or high concurrency) consume search thread slots faster than they free (default `(vCPU * 3 / 2) + 1` threads, queue 1000).
- s1: Each request touching N primary shards consumes N coordinating-node thread slots during scatter-gather, and the queue fills.
- s2: Elasticsearch rejects new search requests immediately rather than queuing indefinitely.
- D: Rising latency and HTTP `429` errors as requests are rejected (points at Symptom Recognition).

**Indicators:**
- root: [Step 2] `rejected` column non-zero and increasing across polling intervals
- s1: [Step 2] `queue` column consistently above 500 during peak traffic

**Interventions:**
- **remediation** (root): Add coordinating-only nodes to handle scatter-gather and free data-node threads, and reduce primary shard count on over-sharded indices via the shrink API. Shrink requires the index read-only with all shards relocated to one node first — plan a maintenance window. Rollback: shrink cannot be undone; retain the original index until verified.

  ```bash
  curl -X POST "http://localhost:9200/<INDEX>/_shrink/<SHRUNK_INDEX>?pretty" \
    -H 'Content-Type: application/json' -d '{
      "settings": { "index.number_of_shards": 1 }
    }'
  ```

  **Verification:** `curl -s "http://localhost:9200/_cat/thread_pool/search?v&h=node_name,active,queue,rejected"` — confirm `rejected` stops increasing and `queue` stays below 200 during peak.
- **mitigation** (s1): Increase the search queue size (dynamic in ES 7.x+) to buy time without dropping requests.

  ```bash
  curl -X PUT "http://localhost:9200/_cluster/settings?pretty" \
    -H 'Content-Type: application/json' -d '{
      "transient": {
        "thread_pool.search.queue_size": 2000
      }
    }'
  ```

  **Risk:** A larger queue delays timeout detection; clients wait longer before failing. Coordinate with application teams on timeout settings before increasing. **Duration:** Immediate; buys time to investigate the root cause without dropping requests. **Verification:** re-run Step 2; confirm `rejected` stops increasing while the root cause is addressed.

---

### Cause E: Excessive segment count

**Statement:** An index accumulates hundreds of segments per shard due to insufficient background merging, forcing queries to union results across all segments and increasing per-query overhead.

**Chain:**
- root: High write throughput outpaces background merge policy, so an index accumulates hundreds of small Lucene segments per shard (never force-merged once read-only).
- s1: Each query must open and search every segment independently, then merge results.
- s2: Segment metadata (bloom filters, field stats) consumes heap proportional to segment count, amplifying per-query overhead.
- D: Per-query overhead rises and search latency increases (points at Symptom Recognition).

**Indicators:**
- root: [Step 8] `segments.count` above 200 per shard on indices that are no longer receiving writes
- root: [Step 8] read-only time-based indices (e.g., Logstash `logstash-YYYY.MM.DD`) with high segment counts

**Interventions:**
- **remediation** (root): Force-merge the closed/read-only index to 1 segment (off-peak only).

  ```bash
  curl -X POST "http://localhost:9200/<INDEX_NAME>/_forcemerge?max_num_segments=1&pretty"
  ```

  **Verification:** `curl -s "http://localhost:9200/_cat/indices/<INDEX_NAME>?v&h=index,segments.count"` — confirm `segments.count` at or near 1 after force merge completes.
- **defensive_fix** (root): Increase the refresh interval during active indexing and tune the merge policy to reduce segment creation rate on actively indexed indices.

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

  **Verification:** Re-run Step 8; confirm new segment accumulation slows on the actively indexed index.

---

### Cause F: Deep pagination via from+size

**Statement:** Queries using `from` + `size` exceeding 10 000 force Elasticsearch to load and discard large result windows from every shard, causing memory pressure and latency proportional to the pagination depth.

**Chain:**
- root: A query uses `from` + `size` exceeding 10 000 (with `index.max_result_window` raised above the default).
- s1: Each shard must retrieve and sort all `from + size` documents before the coordinating node selects the final page (e.g. `from=9000, size=100` sends 9 100 hits per shard).
- s2: With many shards this allocates gigabytes of heap per query and can trip the request circuit breaker.
- D: Memory pressure and latency proportional to pagination depth (points at Symptom Recognition).

**Indicators:**
- root: [Step 5] slow log entries showing `from` values above 5 000 in the query `source`
- s2: [Step 4] `request` breaker `estimated_size` spikes correlated with pagination-heavy traffic

**Interventions:**
- **remediation** (root): Migrate to `search_after` with a Point-in-Time (PIT) for stateless cursor pagination instead of `from`+`size`.

  ```bash
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
- **mitigation** (root): Enforce a hard cap on deep pagination via `index.max_result_window` (returns 400 for requests exceeding the limit) as an interim measure pending the application-side change.

  ```bash
  curl -X PUT "http://localhost:9200/<INDEX_NAME>/_settings?pretty" \
    -H 'Content-Type: application/json' -d '{
      "index.max_result_window": 10000
    }'
  ```

  **Risk:** Requires an eventual application-side change; the cap rejects deep-pagination requests with 400 in the meantime. **Duration:** Immediate; blocks new deep-pagination requests. **Verification:** re-run Step 4; confirm `request` breaker `estimated_size` stops spiking during pagination-heavy windows.

---

### Cause Z: Unidentified

**Statement:** Search latency is elevated but the diagnostic steps do not clearly point to a single known cause.

**Chain:**
- root: No single threshold or pattern from Causes A–F matched after all diagnostic steps.
- s1: Latency arises from a combination of moderate stressors, a transient cluster event (network partition, shard relocation storm), or an application-side issue upstream of Elasticsearch.
- D: Search latency is elevated with no clear single indicator (points at Symptom Recognition).

**Indicators:**
- root: [Default] All Steps 1–8 completed and no single threshold or pattern from Causes A–F matched

**Interventions:**
- **mitigation** (D): Capture a full diagnostic snapshot for escalation, then escalate to Elasticsearch support or a platform engineer with the snapshot files and GC log excerpts.

  ```bash
  curl -s "http://localhost:9200/_cluster/stats?pretty" > /tmp/es-cluster-stats.json
  curl -s "http://localhost:9200/_nodes/stats?pretty" > /tmp/es-nodes-stats.json
  curl -s "http://localhost:9200/_tasks?detailed=true&actions=*search*&pretty" > /tmp/es-search-tasks.json
  ```

  **Risk:** Escalation path; no cluster changes are made until the cause is identified. **Duration:** Safe indefinitely; no cluster changes made. **Verification:** Latency returns to baseline within SLA after the escalation team applies a targeted fix and a 24-hour monitoring period elapses.

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

- [Elasticsearch Reference — Fix Common Cluster Issues](https://www.elastic.co/docs/troubleshoot/elasticsearch/fix-common-cluster-issues) — Priority 1; circuit breaker overview and cluster health triage
- [Elasticsearch Reference — Circuit Breaker Settings](https://www.elastic.co/guide/en/elasticsearch/reference/current/circuit-breaker.html) — Priority 1; all breaker types, default limits (parent 95%, request 60%, fielddata 40%), and configuration API
- [Elasticsearch Reference — Search Slow Log](https://www.elastic.co/guide/en/elasticsearch/reference/current/index-modules-slowlog.html) — Priority 1; threshold configuration for query and fetch phases, log format
- [Elasticsearch Reference — Profile API](https://www.elastic.co/guide/en/elasticsearch/reference/current/search-profile.html) — Priority 1; query timing breakdown phases (create_weight, build_scorer, next_doc, score), aggregation profiling
- [Elasticsearch Reference — Paginate Search Results](https://www.elastic.co/guide/en/elasticsearch/reference/current/paginate-search-results.html) — Priority 1; from+size 10 000 limit, search_after cursor, Point-in-Time API
