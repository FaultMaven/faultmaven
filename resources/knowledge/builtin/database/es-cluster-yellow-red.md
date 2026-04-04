---
id: es-cluster-yellow-red
title: "Elasticsearch Cluster Yellow or Red Status: Unassigned Shards"
domain: database
service: elasticsearch
symptom_class:
  - service_unavailable
  - disk_full
severity: critical
scope: global
version: "2.1.0"
last_updated: "2026-03-26"
verified_by: kb-researcher
status: draft
tags:
  - elasticsearch
  - cluster-health
  - shards
  - disk-watermark
  - allocation
difficulty: intermediate
---

# Elasticsearch Cluster Yellow or Red Status: Unassigned Shards

## Problem Definition

This runbook covers Elasticsearch clusters (versions 7.x and 8.x) reporting yellow or red health status due to unassigned shards. It applies to self-managed deployments, Elastic Cloud, and cloud-provider managed services (Amazon OpenSearch, GCP Elasticsearch). You need access to the Elasticsearch REST API (port 9200 by default), cluster node logs, and JVM/GC logs on each data node. Kibana access is helpful but not required.

Elasticsearch cluster health degrades from green to yellow or red when shards cannot be allocated to nodes. A **yellow** status means all primary shards are assigned but one or more replica shards are not — the cluster is functional but has reduced fault tolerance. A **red** status means one or more primary shards are unassigned, causing data loss risk and incomplete search results.

**Common symptoms:**

- `GET _cluster/health` returns `status: yellow` or `status: red`
- Indexing requests return `ClusterBlockException` or `UnavailableShardsException`
- Search results are incomplete or queries time out (red status — missing primary shards)
- Monitoring dashboards show unassigned shard counts increasing over time
- Kibana displays a "Cluster Health: Red" banner
- Application logs show `503 Service Unavailable` from the Elasticsearch client
- Write operations fail with `index read-only / allow delete (api)` when flood stage watermark is breached

**Common root causes:**

- Disk watermark thresholds exceeded (low: 85%, high: 90%, flood stage: 95%)
- Node failure or network partition leaving shards orphaned
- Insufficient nodes to satisfy the configured replica count (e.g., 1 replica on a single-node cluster)
- Shard allocation filtering or awareness rules preventing placement
- Cluster-level `read_only_allow_delete` block triggered by flood stage watermark
- Corrupt shard data after unclean node shutdown
- Maximum shards per node limit reached (`cluster.max_shards_per_node`, default 1000)
- Index-level `index.routing.allocation` settings restricting placement
- Allocation disabled manually via `cluster.routing.allocation.enable: none`

## Diagnostic Steps

### Step 1: Check cluster health

**What this checks:** The overall cluster state, node count, and shard allocation summary.

```bash
curl -s "localhost:9200/_cluster/health?pretty"
```

**Expected output:** A JSON object with `status`, `number_of_nodes`, `active_primary_shards`, `active_shards`, `unassigned_shards`, `relocating_shards`, and `initializing_shards` fields.

**What the finding means:** If `status` is `yellow`, all primaries are assigned but some replicas are not — the cluster is functional but cannot survive another node failure. If `status` is `red`, at least one primary shard is unassigned and the cluster has data loss risk. If `number_of_nodes` is lower than expected, a node has left the cluster. If `initializing_shards` is non-zero, recovery is in progress.

### Step 2: Identify unassigned shards and their reasons

**What this checks:** Which specific indices and shards are unassigned, and the reason code for each.

```bash
curl -s "localhost:9200/_cat/shards?v&h=index,shard,prirep,state,unassigned.reason&s=state"
```

**Expected output:** A table of shards. Rows with `state=UNASSIGNED` identify the problem shards. The `prirep` column shows `p` (primary) or `r` (replica). The `unassigned.reason` column shows reason codes.

**What the finding means:** `NODE_LEFT` means a node departed the cluster and its shards are orphaned. `ALLOCATION_FAILED` means the allocator attempted placement but every node rejected it (disk, decider rules, or corruption). `INDEX_CREATED` means the shard was never allocated after index creation, typically because there are insufficient nodes. `REROUTE_CANCELLED` means a manual or automatic reroute was cancelled. `CLUSTER_RECOVERED` means the shard is waiting for allocation after a full cluster restart.

### Step 3: Get detailed allocation explanation for a specific shard

**What this checks:** The exact reason Elasticsearch cannot allocate a specific shard, including which allocation deciders rejected it on each node.

```bash
curl -s -X GET "localhost:9200/_cluster/allocation/explain?pretty" -H 'Content-Type: application/json' -d '{
  "index": "<INDEX_NAME>",
  "shard": 0,
  "primary": true
}'
```

**Expected output:** A detailed JSON response with `current_state`, `can_allocate`, `allocate_explanation`, and a `node_allocation_decisions` array listing each node and the decider verdicts (YES, NO, or THROTTLE).

**What the finding means:** The `allocate_explanation` field names the specific decider that blocked allocation. Common blockers: `DiskThresholdDecider` (disk full), `SameShardAllocationDecider` (cannot place replica on same node as primary), `FilterAllocationDecider` (index-level routing rules exclude all available nodes), `AwarenessAllocationDecider` (zone awareness prevents placement), `MaxRetryAllocationDecider` (too many failed attempts — use `POST _cluster/reroute?retry_failed=true` to reset).

### Step 4: Check disk usage on all nodes

**What this checks:** Per-node disk usage to determine if any node exceeds the watermark thresholds.

```bash
curl -s "localhost:9200/_cat/allocation?v&h=node,disk.used,disk.avail,disk.total,disk.percent,shards"
```

**Expected output:** A table showing each node's disk usage percentage, available space, and shard count.

**What the finding means:** If `disk.percent` exceeds 85% (low watermark), Elasticsearch will not allocate new shards to that node. Above 90% (high watermark), Elasticsearch actively relocates shards away. Above 95% (flood stage), all indices with shards on that node are set to read-only (`index.blocks.read_only_allow_delete: true`), blocking all writes.

### Step 5: Check cluster settings for watermarks and allocation blocks

**What this checks:** The active disk watermark thresholds, allocation enable state, and whether any read-only blocks are in effect.

```bash
curl -s "localhost:9200/_cluster/settings?include_defaults=true&flat_settings=true&pretty" | grep -E "watermark|read_only|allocation.enable"
```

**Expected output:** Lines showing `cluster.routing.allocation.disk.watermark.low`, `.high`, `.flood_stage` values, `cluster.routing.allocation.enable` state, and any `read_only_allow_delete` blocks.

**What the finding means:** If the flood stage watermark was breached, indices are automatically set to read-only. If `cluster.routing.allocation.enable` is `none` or `primaries`, someone manually disabled allocation (common during maintenance). Both conditions persist after the original cause is resolved and must be manually cleared.

### Step 6: Check node status and resource usage

**What this checks:** Whether all expected nodes are present and their resource health (heap, CPU, RAM).

```bash
curl -s "localhost:9200/_cat/nodes?v&h=name,heap.percent,ram.percent,cpu,load_1m,disk.used_percent,node.role"
```

**Expected output:** One row per node with resource metrics.

**What the finding means:** Missing nodes indicate a failure or network partition. Nodes with `heap.percent` above 85% are at risk of long GC pauses, which can cause the master to remove them from the cluster. The `node.role` column shows which nodes are data-eligible (`d`), master-eligible (`m`), or coordinating-only (`-`). If all data nodes are missing or overloaded, no shard allocation is possible.

### Step 7: Check for index-level read-only blocks

**What this checks:** Whether any indices have been set to read-only by the flood stage watermark or by manual action.

```bash
curl -s "localhost:9200/_all/_settings?pretty" | grep -E "read_only|blocks"
```

**Expected output:** Any index with `index.blocks.read_only_allow_delete: true` is blocked from accepting writes.

**What the finding means:** This block is automatically applied when the flood stage watermark (95%) is breached and must be manually cleared even after disk pressure is resolved. Indices with `index.blocks.read_only: true` were set manually and indicate intentional write protection.

### Step 8: Check maximum shards per node limit

**What this checks:** Whether the cluster-wide shard limit per node has been reached.

```bash
curl -s "localhost:9200/_cluster/settings?include_defaults=true&flat_settings=true&pretty" | grep "max_shards_per_node"
```

**Expected output:** The value of `cluster.max_shards_per_node` (default 1000).

**What the finding means:** If a node already holds this many shards, no additional shards can be allocated to it. This commonly occurs in clusters with many small indices. Compare with the actual shard count per node from Step 6.

## Mitigation

### Option 1: Clear read-only block on indices (flood stage recovery)

Use when the flood stage watermark was triggered and indices are now read-only, and disk space has already been freed below the flood stage threshold.

- **Risk:** Low, provided disk space is genuinely below the flood stage threshold. If disk is still above 95%, the block will be re-applied immediately.
- **Command:**

```bash
curl -X PUT "localhost:9200/_all/_settings?pretty" -H 'Content-Type: application/json' -d '{
  "index.blocks.read_only_allow_delete": null
}'
```

- **Verify:**

```bash
curl -s "localhost:9200/_all/_settings?pretty" | grep "read_only"
# Expected: no read_only_allow_delete entries
```

- **Duration:** Immediate.

### Option 2: Free disk space by deleting old indices

Use when disk watermarks are exceeded and the cluster cannot allocate shards.

- **Risk:** High — permanent data loss for deleted indices. Verify retention policies and confirm with stakeholders before deleting.
- **Command:**

```bash
# List indices sorted by size (largest first)
curl -s "localhost:9200/_cat/indices?v&h=index,store.size,docs.count,creation.date.string&s=store.size:desc" | head -20

# Delete old indices (example: logs older than 30 days)
curl -X DELETE "localhost:9200/logs-2026.02.*"
```

- **Verify:**

```bash
curl -s "localhost:9200/_cat/allocation?v&h=node,disk.percent"
# Expected: disk.percent below 85% on all nodes
```

- **Duration:** Minutes. Disk space is freed as Elasticsearch removes segment files.

### Option 3: Re-enable shard allocation

Use when allocation was manually disabled during maintenance and not re-enabled afterward.

- **Risk:** Low. This restores normal cluster behavior. If allocation was disabled intentionally due to an active issue, re-enabling may cause unwanted shard movement.
- **Command:**

```bash
curl -X PUT "localhost:9200/_cluster/settings?pretty" -H 'Content-Type: application/json' -d '{
  "persistent": {
    "cluster.routing.allocation.enable": null
  }
}'
```

- **Verify:**

```bash
curl -s "localhost:9200/_cluster/health?pretty" | grep -E "status|initializing_shards"
# Expected: initializing_shards > 0 as allocation resumes, then status improves
```

- **Duration:** Seconds for the setting change. Shard recovery time depends on shard size and cluster load.

### Option 4: Reroute unassigned shards manually

Use when automatic allocation fails due to transient issues or when `MaxRetryAllocationDecider` has exhausted retries.

- **Risk:** Moderate for replica shards (safe). High for stale primaries — forcing allocation with `accept_data_loss: true` discards any writes that occurred after the shard became unassigned.
- **Command:**

For retrying failed allocations (safe first step):

```bash
curl -X POST "localhost:9200/_cluster/reroute?retry_failed=true&pretty"
```

For replica shards (safe):

```bash
curl -X POST "localhost:9200/_cluster/reroute?pretty" -H 'Content-Type: application/json' -d '{
  "commands": [{
    "allocate_replica": {
      "index": "<INDEX_NAME>",
      "shard": 0,
      "node": "<NODE_NAME>"
    }
  }]
}'
```

For stale primary shards (data loss risk — last resort):

```bash
curl -X POST "localhost:9200/_cluster/reroute?pretty" -H 'Content-Type: application/json' -d '{
  "commands": [{
    "allocate_stale_primary": {
      "index": "<INDEX_NAME>",
      "shard": 0,
      "node": "<NODE_NAME>",
      "accept_data_loss": true
    }
  }]
}'
```

- **Verify:**

```bash
curl -s "localhost:9200/_cluster/health?pretty" | grep status
# Expected: yellow or green (improved from red)
```

- **Duration:** Seconds to minutes per shard, depending on shard size.

### Option 5: Reduce replica count

Use when the cluster has fewer data nodes than the configured replica count (e.g., single-node cluster with `number_of_replicas: 1`).

- **Risk:** Lower fault tolerance — if a node fails, there is no replica to promote. Only appropriate for development environments or while scaling out.
- **Command:**

```bash
curl -X PUT "localhost:9200/<INDEX_NAME>/_settings?pretty" -H 'Content-Type: application/json' -d '{
  "index": {
    "number_of_replicas": 0
  }
}'
```

- **Verify:**

```bash
curl -s "localhost:9200/_cluster/health?pretty" | grep -E "status|unassigned_shards"
# Expected: status green, unassigned_shards 0
```

- **Duration:** Immediate.

### Option 6: Temporarily raise disk watermark thresholds

Use as a short-term measure to allow shard allocation while disk capacity expansion is in progress.

- **Risk:** High — nodes may run critically low on disk, potentially causing OS-level issues or data corruption. Only use while actively adding disk capacity.
- **Command:**

```bash
curl -X PUT "localhost:9200/_cluster/settings?pretty" -H 'Content-Type: application/json' -d '{
  "transient": {
    "cluster.routing.allocation.disk.watermark.low": "90%",
    "cluster.routing.allocation.disk.watermark.high": "95%",
    "cluster.routing.allocation.disk.watermark.flood_stage": "97%"
  }
}'
```

- **Verify:**

```bash
curl -s "localhost:9200/_cluster/health?pretty" | grep status
# Expected: status improves as shards begin allocating
```

- **Duration:** Immediate. Revert to defaults once capacity is added.

## Root Cause Resolution

**If** disk watermarks are exceeded → add data nodes or expand disk volumes on existing nodes. Implement Index Lifecycle Management (ILM) policies to automatically roll over, shrink, and delete old indices based on age or size.

**If** a node is permanently down and will not return → remove the node from the cluster with `POST /_cluster/voting_config_exclusions?node_names=<NODE>` (if master-eligible). Elasticsearch will reallocate its shards to remaining nodes if sufficient capacity exists. If the node held the only copy of a primary shard, that data is lost.

**If** a node is temporarily down but will return → wait for the node to rejoin. Increase `index.unassigned.node_left.delayed_timeout` to avoid unnecessary shard reallocation for brief outages:

```bash
curl -X PUT "localhost:9200/_all/_settings?pretty" -H 'Content-Type: application/json' -d '{
  "index.unassigned.node_left.delayed_timeout": "10m"
}'
```

**If** `cluster.max_shards_per_node` limit is reached → increase the limit or reduce total shard count by merging small indices using the Reindex API, or reduce the number of primary shards on new indices. Target 10-50 GB per shard.

**If** shard allocation awareness settings prevent placement → verify that `cluster.routing.allocation.awareness.attributes` and zone/rack labels are correctly configured on all data nodes. Ensure each awareness zone has sufficient data node capacity.

**If** corrupt shard data prevents allocation → check the allocation explain API for corruption errors. If the shard has replicas on other nodes, delete the corrupt copy and let it re-sync from a healthy replica. If it is the only copy, use `allocate_stale_primary` with `accept_data_loss: true` or `allocate_empty_primary` if no copy exists.

**If** allocation was disabled during maintenance → re-enable with `cluster.routing.allocation.enable: null` (see Mitigation Option 3).

**If** the cluster has thousands of small indices → consolidate using the Reindex API or adopt a time-based index pattern with ILM rollover to produce fewer, larger indices. Consider using data streams for time-series data.

## Verification

After applying fixes, confirm cluster health is restored:

```bash
# 1. Cluster health is green
curl -s "localhost:9200/_cluster/health?pretty" | grep -E "status|unassigned_shards|number_of_nodes"
# Expected: status green, unassigned_shards 0

# 2. All shards are assigned
curl -s "localhost:9200/_cat/shards?v&h=index,shard,prirep,state" | grep -c UNASSIGNED
# Expected: 0

# 3. Disk usage is within watermarks
curl -s "localhost:9200/_cat/allocation?v&h=node,disk.percent"
# Expected: all nodes below 85%

# 4. No read-only blocks on indices
curl -s "localhost:9200/_all/_settings?pretty" | grep "read_only_allow_delete"
# Expected: no results or all set to null/false

# 5. Allocation is enabled
curl -s "localhost:9200/_cluster/settings?flat_settings=true&pretty" | grep "allocation.enable"
# Expected: no override (default is "all") or explicitly "all"

# 6. Indexing and searching work
curl -X POST "localhost:9200/test-health-check/_doc" -H 'Content-Type: application/json' -d '{"test": "ok"}'
curl -s "localhost:9200/test-health-check/_search?pretty"
curl -X DELETE "localhost:9200/test-health-check"
```

Monitor cluster health for at least 1 hour to confirm stability and that all shard relocations complete. Watch `initializing_shards` and `relocating_shards` return to zero.

## Prevention

1. **Implement Index Lifecycle Management (ILM)** — Configure automatic rollover, shrink, and delete policies to manage disk usage proactively. Set retention periods appropriate for each index pattern. Use data streams for time-series data.

2. **Monitor disk usage with alerts** — Alert at 75% disk usage, well before the 85% low watermark. Use Prometheus with `elasticsearch_exporter` or the Elasticsearch built-in monitoring stack.

3. **Size clusters for N-1 redundancy** — Ensure the cluster can tolerate losing one data node without exceeding disk watermarks on the remaining nodes. Plan for at least 33% disk headroom.

4. **Use dedicated master nodes** — In production clusters with 3+ data nodes, run 3 dedicated master-eligible nodes to prevent split-brain and ensure cluster stability during data node outages.

5. **Set appropriate replica counts** — Use `number_of_replicas: 1` for production data (requires at least 2 data nodes). Use `0` only for ephemeral or easily re-indexable data.

6. **Configure shard allocation awareness** — Use allocation awareness attributes to distribute primary and replica shards across availability zones or racks to survive zone-level failures.

7. **Right-size shards** — Target 10-50 GB per shard. Too many small shards waste heap and file descriptors; too few large shards impair recovery time and rebalancing.

8. **Monitor unassigned shard count** — Alert immediately when `unassigned_shards > 0` persists for more than 5 minutes. This is the earliest signal of cluster degradation.

9. **Test node failure recovery** — Periodically simulate node failures in staging to verify that shard reallocation works correctly and the cluster recovers to green within your SLA.

10. **Keep Elasticsearch versions current** — Newer versions include shard allocation improvements, better disk watermark handling, and more informative allocation explain output.

## Sources

- [Elasticsearch — Red or Yellow Cluster Status](https://www.elastic.co/docs/troubleshoot/elasticsearch/red-yellow-cluster-status)
- [Elasticsearch — Diagnose Unassigned Shards](https://www.elastic.co/docs/troubleshoot/elasticsearch/diagnose-unassigned-shards)
- [Elasticsearch Reference — Fix Common Cluster Issues](https://www.elastic.co/guide/en/elasticsearch/reference/current/fix-common-cluster-issues.html)
- [Elasticsearch Reference — Cluster Health API](https://www.elastic.co/guide/en/elasticsearch/reference/current/cluster-health.html)
- [Elasticsearch Reference — Cluster Allocation Explain API](https://www.elastic.co/guide/en/elasticsearch/reference/current/cluster-allocation-explain.html)
- [Elasticsearch Reference — Disk-based Shard Allocation](https://www.elastic.co/guide/en/elasticsearch/reference/current/modules-cluster.html#disk-based-shard-allocation)
- [Elasticsearch Reference — Index Lifecycle Management](https://www.elastic.co/guide/en/elasticsearch/reference/current/index-lifecycle-management.html)
