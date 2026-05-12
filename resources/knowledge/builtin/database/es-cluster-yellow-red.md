---
id: "es-cluster-yellow-red"
title: "Elasticsearch Cluster Yellow or Red Health Status"
domain: database
service: elasticsearch
symptom_class: [service_unavailable, disk_full]
severity: critical
scope: global
version: "1.0.0"
last_updated: "2026-05-12"
verified_by: "kb-researcher"
status: draft
tags: [elasticsearch, cluster-health, shards, disk-watermark, allocation]
difficulty: intermediate
---

## Symptom Recognition

- `GET _cluster/health` returns `"status": "yellow"` or `"status": "red"` with `unassigned_shards > 0`.
- Yellow status: all primary shards assigned, one or more replica shards unassigned — cluster functional but has no fault-tolerance margin.
- Red status: one or more primary shards unassigned — search results incomplete, indexing fails with `UnavailableShardsException`.
- Indexing returns `ClusterBlockException[blocked by: [FORBIDDEN/12/index read-only / allow delete (api)]]`.
- `_cat/shards` shows rows with `state=UNASSIGNED`; `unassigned.reason` values include `NODE_LEFT`, `ALLOCATION_FAILED`, `INDEX_CREATED`.
- Application logs show `503 Service Unavailable` or `NoNodeAvailableException` from Elasticsearch client.
- Kibana displays a "Cluster Health: Red" banner.
- Monitoring shows `elasticsearch.cluster.shards.unassigned` metric increasing over time.

## Applicability

Applies to self-managed Elasticsearch 7.x and 8.x, Elastic Cloud, and cloud-provider managed services (Amazon OpenSearch Service, GCP Elastic on Marketplace). Requires network access to the Elasticsearch REST API (default port 9200). Administrative cluster privileges are required to call `_cluster/settings`, `_cluster/reroute`, and `_cluster/allocation/explain`. Kibana access is helpful but not required. Commands use `curl`; substitute `GET`/`PUT`/`POST` in Kibana Dev Tools as needed.

## Diagnostic Steps

### Step 1: Check cluster health summary

```bash
curl -s "http://localhost:9200/_cluster/health?pretty"
```

Expected output: JSON with `status` (`green`/`yellow`/`red`), `number_of_nodes`, `active_primary_shards`, `active_shards`, `unassigned_shards`, `initializing_shards`, `relocating_shards`. Note the exact `status` value and `unassigned_shards` count.

### Step 2: List all unassigned shards and their reason codes

```bash
curl -s "http://localhost:9200/_cat/shards?v&h=index,shard,prirep,state,unassigned.reason&s=state"
```

Expected output: Table of shards. Rows with `state=UNASSIGNED` name the problem shards. The `prirep` column shows `p` (primary) or `r` (replica). The `unassigned.reason` column contains reason codes: `NODE_LEFT`, `ALLOCATION_FAILED`, `INDEX_CREATED`, `REROUTE_CANCELLED`, `CLUSTER_RECOVERED`.

### Step 3: Get detailed allocation explanation for one unassigned shard

```bash
curl -s -X GET "http://localhost:9200/_cluster/allocation/explain?pretty" \
  -H 'Content-Type: application/json' \
  -d '{"index": "<INDEX_NAME>", "shard": 0, "primary": true}'
```

Expected output: JSON with `current_state`, `can_allocate`, `allocate_explanation`, and `node_allocation_decisions` array. Each node entry lists decider verdicts (`YES`/`NO`/`THROTTLE`). The `explanation` field on each `NO` decision names the blocking decider: `DiskThresholdDecider`, `SameShardAllocationDecider`, `FilterAllocationDecider`, `AwarenessAllocationDecider`, `MaxRetryAllocationDecider`.

### Step 4: Check per-node disk usage

```bash
curl -s "http://localhost:9200/_cat/allocation?v&h=node,disk.used,disk.avail,disk.total,disk.percent,shards"
```

Expected output: Table with one row per node. Note `disk.percent` values. Thresholds: `>= 85%` blocks new shard allocation (low watermark), `>= 90%` triggers relocation away from node (high watermark), `>= 95%` sets indices read-only (flood stage).

### Step 5: Check cluster-level settings for watermarks and allocation enable state

```bash
curl -s "http://localhost:9200/_cluster/settings?include_defaults=true&flat_settings=true&pretty" \
  | grep -E "watermark|read_only|allocation\.enable"
```

Expected output: Lines for `cluster.routing.allocation.disk.watermark.low`, `.high`, `.flood_stage`, `cluster.routing.allocation.enable`, and any `read_only_allow_delete` values. `allocation.enable` of `none` or `primaries` means allocation was disabled manually.

### Step 6: Check node health and resource usage

```bash
curl -s "http://localhost:9200/_cat/nodes?v&h=name,heap.percent,ram.percent,cpu,load_1m,disk.used_percent,node.role"
```

Expected output: One row per node with live resource metrics. Missing nodes indicate failure or network partition. `heap.percent >= 85` means GC pressure that can cause master to evict node from cluster. `node.role` shows `d` (data), `m` (master-eligible), `-` (coordinating only).

### Step 7: Check for index-level read-only blocks

```bash
curl -s "http://localhost:9200/_all/_settings?pretty" | grep -E "read_only|blocks"
```

Expected output: Any index with `"index.blocks.read_only_allow_delete": "true"` is flood-stage blocked. Indices with `"index.blocks.read_only": "true"` were blocked manually.

### Step 8: Check max shards per node limit

```bash
curl -s "http://localhost:9200/_cluster/settings?include_defaults=true&flat_settings=true&pretty" \
  | grep "max_shards_per_node"
```

Expected output: The value of `cluster.max_shards_per_node` (default `1000`). Compare against the shard count per node from Step 6 to detect if the ceiling is reached.

## Causes

### Cause A: Disk watermark exceeded — allocation blocked on high-disk nodes

**Statement:** One or more data nodes have exceeded the low (85%) or flood-stage (95%) disk watermark, preventing Elasticsearch from assigning new shards to those nodes.

**Mechanism:** Elasticsearch's `DiskThresholdDecider` denies shard placement on any node at or above the low watermark (default 85%). When a node reaches the flood stage (95%), all indices with shards on that node are set to `index.blocks.read_only_allow_delete: true`, blocking writes cluster-wide for those indices. The blocks persist after disk pressure is relieved and must be cleared manually.

**Indicator:**

- [Step 4] `disk.percent` >= 85 on one or more nodes
- [Step 3] `node_allocation_decisions` contains `explanation` referencing `DiskThresholdDecider` with verdict `NO`
- [Step 7] `"index.blocks.read_only_allow_delete": "true"` present on one or more indices

<!-- match: {"step": 4, "predicate": "threshold", "target": "disk.percent", "op": ">=", "value": 85} -->
<!-- match: {"step": 3, "predicate": "contains", "target": "DiskThresholdDecider"} -->
<!-- match: {"step": 7, "predicate": "contains", "target": "read_only_allow_delete"} -->

**Mitigation:**

- **Risk:** Deleting indices causes permanent data loss. Verify retention policy and stakeholder sign-off before deletion.
- **Command:**

  ```bash
  # List largest indices to find candidates for deletion
  curl -s "http://localhost:9200/_cat/indices?v&h=index,store.size,docs.count,creation.date.string&s=store.size:desc" | head -20
  # Delete old time-based indices (example pattern)
  curl -X DELETE "http://localhost:9200/logs-2026.01.*"
  # After freeing space, clear flood-stage read-only blocks
  curl -X PUT "http://localhost:9200/_all/_settings?pretty" \
    -H 'Content-Type: application/json' \
    -d '{"index.blocks.read_only_allow_delete": null}'
  ```

- **Duration:** Minutes to free disk. Clear read-only blocks immediately after disk drops below flood stage.

**Resolution:**

```bash
# Durable fix: implement ILM delete phase to auto-expire old indices
curl -X PUT "http://localhost:9200/_ilm/policy/auto-delete-30d?pretty" \
  -H 'Content-Type: application/json' \
  -d '{
    "policy": {
      "phases": {
        "delete": {"min_age": "30d", "actions": {"delete": {}}}
      }
    }
  }'
```

- **Impact:** ILM policy applies to indices with matching `index.lifecycle.name` setting — no immediate impact on existing indices without the setting. Adding disk capacity is the safest long-term resolution.
- **Rollback:** `DELETE _ilm/policy/auto-delete-30d` removes the policy without affecting existing indices.

**Verification:** `curl -s "http://localhost:9200/_cat/allocation?v&h=node,disk.percent"` — all nodes below 85%. `curl -s "http://localhost:9200/_all/_settings?pretty" | grep read_only` — no results or all null/false. Cluster status returns `green` within 10 minutes of shard reallocation completing.

### Cause B: Node failure or departure — shards orphaned

**Statement:** A data node left the cluster unexpectedly, leaving its primary or replica shards with no host until Elasticsearch reallocates them to surviving nodes.

**Mechanism:** When a node departs, Elasticsearch marks its shards `UNASSIGNED` with reason `NODE_LEFT`. Reallocation starts after `index.unassigned.node_left.delayed_timeout` (default 1 minute), which causes temporary yellow/red during that window. If the departed node held the only copy of a primary shard, the cluster enters red status until the node returns or an operator forces allocation with data loss.

**Indicator:**

- [Step 1] `number_of_nodes` lower than expected
- [Step 2] `unassigned.reason` = `NODE_LEFT` on multiple shards
- [Step 6] Expected node name absent from output

<!-- match: {"step": 2, "predicate": "contains", "target": "NODE_LEFT"} -->
<!-- match: {"step": 1, "predicate": "contains", "target": "number_of_nodes"} -->

**Mitigation:**

- **Risk:** If node is temporarily down (restart/patch), waiting avoids unnecessary shard recovery I/O. If permanently gone, replicas on surviving nodes serve reads but writes remain blocked for affected primaries.
- **Command:**

  ```bash
  # Extend delay to avoid thrashing if node will return
  curl -X PUT "http://localhost:9200/_all/_settings?pretty" \
    -H 'Content-Type: application/json' \
    -d '{"index.unassigned.node_left.delayed_timeout": "10m"}'
  # Retry failed allocations after delay expires
  curl -X POST "http://localhost:9200/_cluster/reroute?retry_failed=true&pretty"
  ```

- **Duration:** Wait up to the delayed_timeout window for the node to return; then reroute.

**Resolution:**

```bash
# If node is permanently gone and replicas exist on surviving nodes, reroute replicas:
curl -X POST "http://localhost:9200/_cluster/reroute?pretty" \
  -H 'Content-Type: application/json' \
  -d '{
    "commands": [{
      "allocate_replica": {"index": "<INDEX>", "shard": 0, "node": "<SURVIVING_NODE>"}
    }]
  }'
# If node held only copy of primary and will not return (last resort, data loss):
curl -X POST "http://localhost:9200/_cluster/reroute?pretty" \
  -H 'Content-Type: application/json' \
  -d '{
    "commands": [{
      "allocate_stale_primary": {
        "index": "<INDEX>", "shard": 0, "node": "<NODE>", "accept_data_loss": true
      }
    }]
  }'
```

- **Impact:** `allocate_stale_primary` with `accept_data_loss: true` is irreversible — any writes that occurred after the shard became unassigned are permanently lost.
- **Rollback:** Not applicable for `allocate_stale_primary`; data is unrecoverable after execution.

**Verification:** `curl -s "http://localhost:9200/_cluster/health?pretty" | grep -E "status|unassigned_shards"` — `unassigned_shards: 0` and `status: green`. `curl -s "http://localhost:9200/_cat/nodes?v"` — expected node count restored or acknowledged as gone.

### Cause C: Insufficient nodes for configured replica count

**Statement:** The cluster has fewer data nodes than the number of replicas configured, making it impossible to satisfy the replica placement constraint that no two copies of a shard share the same node.

**Mechanism:** Elasticsearch's `SameShardAllocationDecider` enforces that no node can hold both a primary shard and its replica, nor two replicas of the same shard. A single-node cluster with `number_of_replicas: 1` produces permanent yellow status because the replica can never be placed. This is the default state for development environments.

**Indicator:**

- [Step 2] `unassigned.reason` = `INDEX_CREATED` on all replica shards (`prirep=r`) for all indices
- [Step 3] `node_allocation_decisions` contains `explanation` referencing `SameShardAllocationDecider` with verdict `NO`
- [Step 6] Node count equals 1 or fewer than `number_of_replicas + 1`

<!-- match: {"step": 2, "predicate": "contains", "target": "INDEX_CREATED"} -->
<!-- match: {"step": 3, "predicate": "contains", "target": "SameShardAllocationDecider"} -->

**Mitigation:**

- **Risk:** Setting `number_of_replicas: 0` eliminates fault tolerance — if the single node fails, data is lost.
- **Command:**

  ```bash
  # Reduce replica count to 0 for all indices (development/single-node only)
  curl -X PUT "http://localhost:9200/_all/_settings?pretty" \
    -H 'Content-Type: application/json' \
    -d '{"index": {"number_of_replicas": 0}}'
  ```

- **Duration:** Immediate. Restore to 1 once additional nodes are added.

**Resolution:**

```bash
# Add a second data node and restore replicas
curl -X PUT "http://localhost:9200/_all/_settings?pretty" \
  -H 'Content-Type: application/json' \
  -d '{"index": {"number_of_replicas": 1}}'
```

- **Impact:** Adding nodes requires infrastructure provisioning; restoring replicas triggers shard recovery I/O across all indices.
- **Rollback:** `PUT _all/_settings {"index": {"number_of_replicas": 0}}` returns to no-replica state.

**Verification:** `curl -s "http://localhost:9200/_cluster/health?pretty" | grep -E "status|unassigned_shards"` — `unassigned_shards: 0` and `status: green`.

### Cause D: Shard allocation manually disabled

**Statement:** `cluster.routing.allocation.enable` was set to `none` or `primaries` during maintenance and was never re-enabled, leaving new and unassigned shards stranded.

**Mechanism:** Operators commonly disable shard allocation before rolling restarts to prevent unnecessary shard movement. If re-enable is skipped (crash, incomplete runbook), the cluster remains in a state where it cannot place any unassigned shards. The cause is not a hardware or capacity failure but a configuration state.

**Indicator:**

- [Step 5] `cluster.routing.allocation.enable` = `none` or `primaries`
- [Step 3] `allocate_explanation` contains `allocation is disabled` or `NO decisions` with no disk/hardware reason

<!-- match: {"step": 5, "predicate": "contains", "target": "allocation.enable"} -->
<!-- match: {"step": 3, "predicate": "contains", "target": "allocation is disabled"} -->

**Mitigation:**

- **Risk:** Low. Re-enabling allocation restores normal cluster behavior. If allocation was disabled due to an active issue, re-enabling may cause unwanted shard movement before the root cause is resolved.
- **Command:**

  ```bash
  curl -X PUT "http://localhost:9200/_cluster/settings?pretty" \
    -H 'Content-Type: application/json' \
    -d '{"persistent": {"cluster.routing.allocation.enable": null}}'
  ```

- **Duration:** Seconds for the setting change. Shard recovery proceeds asynchronously.

**Resolution:** **Same as Mitigation.**

**Verification:** `curl -s "http://localhost:9200/_cluster/settings?flat_settings=true&pretty" | grep allocation.enable` — no override value (default `all`). Monitor `initializing_shards` counter via `_cluster/health` until it returns to 0.

### Cause E: Allocation filter rules exclude all available nodes

**Statement:** Index-level `index.routing.allocation.require` or `index.routing.allocation.include` settings reference node attributes that no currently-available data node satisfies, causing all allocation decisions to return `NO`.

**Mechanism:** `FilterAllocationDecider` compares the index's routing filter settings against each node's attributes (e.g., `node.attr.zone`, `node.attr.rack`). If the index requires `zone: us-east-1a` but no data node carries that attribute label, every node is rejected. This silently persists until the filter is corrected or a matching node is added.

**Indicator:**

- [Step 3] `node_allocation_decisions` contains `explanation` referencing `FilterAllocationDecider` with verdict `NO` on every node
- [Step 3] `allocate_explanation` names a specific node attribute value that is absent from `_cat/nodes` output

<!-- match: {"step": 3, "predicate": "contains", "target": "FilterAllocationDecider"} -->

**Mitigation:**

- **Risk:** Removing the filter may place shards on unintended nodes if the filter was enforcing a data-locality requirement (e.g., compliance zone pinning).
- **Command:**

  ```bash
  # Inspect the problematic filter on the index
  curl -s "http://localhost:9200/<INDEX_NAME>/_settings?pretty" | grep -E "routing\.allocation"
  # Remove the filter
  curl -X PUT "http://localhost:9200/<INDEX_NAME>/_settings?pretty" \
    -H 'Content-Type: application/json' \
    -d '{"index.routing.allocation.require._name": null}'
  ```

- **Duration:** Immediate. Shards allocate on the next allocation cycle.

**Resolution:**

```bash
# Correct the filter to match actual node attributes, or remove it entirely
curl -X PUT "http://localhost:9200/<INDEX_NAME>/_settings?pretty" \
  -H 'Content-Type: application/json' \
  -d '{"index.routing.allocation.require.zone": "<CORRECT_ZONE_VALUE>"}'
```

- **Impact:** Per-index change with no cluster-wide blast radius. Correcting a zone filter may relocate shards across nodes, causing recovery I/O.
- **Rollback:** `PUT <INDEX>/_settings {"index.routing.allocation.require.zone": "<ORIGINAL>"}` restores the original filter.

**Verification:** `curl -s "http://localhost:9200/_cluster/allocation/explain?pretty" -d '{"index": "<INDEX>", "shard": 0, "primary": true}'` — `can_allocate` changes from `NO` to `YES`. `_cluster/health` returns `green` within minutes.

### Cause F: MaxRetryAllocationDecider — allocation exhausted retry limit

**Statement:** Elasticsearch has attempted to allocate a shard multiple times, exhausted its retry limit (default 5), and marked the shard as permanently unallocatable until an operator resets the counter.

**Mechanism:** After 5 failed allocation attempts, `MaxRetryAllocationDecider` marks the shard with `max_retry` and stops automatic retries. The root failure that caused the initial attempts may have been transient (e.g., a momentary node overload), but the retry counter is not reset automatically. The shard remains unassigned even after the original cause is resolved.

**Indicator:**

- [Step 3] `allocate_explanation` contains `too many allocation attempts` or `MaxRetryAllocationDecider`
- [Step 2] `unassigned.reason` = `ALLOCATION_FAILED`

<!-- match: {"step": 3, "predicate": "contains", "target": "MaxRetryAllocationDecider"} -->
<!-- match: {"step": 2, "predicate": "contains", "target": "ALLOCATION_FAILED"} -->

**Mitigation:**

- **Risk:** Low. `retry_failed=true` resets the retry counter and triggers a fresh allocation attempt. If the underlying cause is unresolved, the cycle will repeat.
- **Command:**

  ```bash
  curl -X POST "http://localhost:9200/_cluster/reroute?retry_failed=true&pretty"
  ```

- **Duration:** Seconds for the API call. Shard placement takes additional seconds to minutes.

**Resolution:** **Same as Mitigation.** Investigate the original allocation failure in Elasticsearch logs to prevent recurrence (`grep "ALLOCATION_FAILED" /var/log/elasticsearch/*.log`).

**Verification:** `_cluster/health` shows `initializing_shards > 0` briefly, then `unassigned_shards: 0`. `_cluster/allocation/explain` no longer references `MaxRetryAllocationDecider`.

### Cause G: Cluster max-shards-per-node limit reached

**Statement:** The cluster-wide `cluster.max_shards_per_node` ceiling (default 1000) has been reached on one or more data nodes, preventing any additional shard assignments.

**Mechanism:** The `ShardsLimitAllocationDecider` enforces a hard ceiling of `cluster.max_shards_per_node * number_of_data_nodes` total open shards. Clusters with many small indices (e.g., daily log indices that accumulate over months) routinely hit this limit. The limit was introduced in 7.x to prevent uncontrolled shard proliferation from degrading cluster performance.

**Indicator:**

- [Step 8] `cluster.max_shards_per_node` is at or near the total shard count divided by node count
- [Step 3] `allocate_explanation` contains `too many shards` or `ShardsLimitAllocationDecider`

<!-- match: {"step": 3, "predicate": "contains", "target": "too many shards"} -->
<!-- match: {"step": 3, "predicate": "contains", "target": "ShardsLimitAllocationDecider"} -->

**Mitigation:**

- **Risk:** Raising the limit above 1000 per node can degrade cluster performance — each shard consumes heap, file descriptors, and threads. Raise only while consolidating indices.
- **Command:**

  ```bash
  # Temporary: raise the limit while consolidating
  curl -X PUT "http://localhost:9200/_cluster/settings?pretty" \
    -H 'Content-Type: application/json' \
    -d '{"persistent": {"cluster.max_shards_per_node": 2000}}'
  ```

- **Duration:** Immediate. Revert after shard count is reduced.

**Resolution:**

```bash
# Delete old, unneeded time-based indices to reduce total shard count
curl -s "http://localhost:9200/_cat/indices?v&h=index,docs.count,store.size&s=index" | grep -E "^logs-202[0-4]"
curl -X DELETE "http://localhost:9200/logs-2024.*"
# Reset limit to default once shard count is under control
curl -X PUT "http://localhost:9200/_cluster/settings?pretty" \
  -H 'Content-Type: application/json' \
  -d '{"persistent": {"cluster.max_shards_per_node": null}}'
```

- **Impact:** Deleting indices is permanent. Resetting `max_shards_per_node` to null restores the default 1000 — ensure actual shard count is below this before resetting.
- **Rollback:** `PUT _cluster/settings {"persistent": {"cluster.max_shards_per_node": 2000}}` restores the raised limit.

**Verification:** `curl -s "http://localhost:9200/_cluster/health?pretty" | grep unassigned_shards` returns `0`. Total shards / data nodes < `cluster.max_shards_per_node`.

### Cause H: Corrupt or unrecoverable shard data

**Statement:** A data node experienced an unclean shutdown or storage fault that corrupted a shard's segment files, causing every allocation attempt to fail with a shard corruption exception.

**Mechanism:** Elasticsearch validates shard integrity when loading segments from disk. If checksums fail or segment files are missing, the shard is marked `ALLOCATION_FAILED` and the `MaxRetryAllocationDecider` quickly exhausts retries. If a healthy replica exists on another node, deleting the corrupt copy allows Elasticsearch to recover from the replica. If no healthy copy exists, data recovery requires `allocate_empty_primary` (data loss) or restoring from a snapshot.

**Indicator:**

- [Step 3] `allocate_explanation` contains `shard corruption` or `failed to open shard on node` or `failed shard on allocating node`
- [Step 2] `unassigned.reason` = `ALLOCATION_FAILED` on a primary shard with no corresponding healthy replica

<!-- match: {"step": 3, "predicate": "contains", "target": "shard corruption"} -->
<!-- match: {"step": 3, "predicate": "contains", "target": "failed shard on allocating node"} -->

**Mitigation:**

- **Risk:** If a healthy replica exists, deleting the corrupt copy is safe — it re-syncs from the replica. If no replica exists, `allocate_empty_primary` creates an empty shard (all data lost).
- **Command:**

  ```bash
  # Check if a healthy replica exists before proceeding
  curl -s "http://localhost:9200/_cat/shards?v&h=index,shard,prirep,state,node" | grep "<INDEX_NAME>"
  # Retry allocation first (may succeed if transient):
  curl -X POST "http://localhost:9200/_cluster/reroute?retry_failed=true&pretty"
  # Last resort if no replica — allocate empty primary (ALL DATA IN THIS SHARD LOST):
  curl -X POST "http://localhost:9200/_cluster/reroute?pretty" \
    -H 'Content-Type: application/json' \
    -d '{
      "commands": [{
        "allocate_empty_primary": {
          "index": "<INDEX_NAME>", "shard": 0,
          "node": "<TARGET_NODE>", "accept_data_loss": true
        }
      }]
    }'
  ```

- **Duration:** Shard recovery from a healthy replica may take minutes to hours depending on shard size. `allocate_empty_primary` is immediate.

**Resolution:**

```bash
# Restore from snapshot if data loss from allocate_empty_primary is unacceptable
curl -X POST "http://localhost:9200/_snapshot/<REPO>/<SNAPSHOT>/_restore?pretty" \
  -H 'Content-Type: application/json' \
  -d '{"indices": "<INDEX_NAME>", "ignore_unavailable": true}'
```

- **Impact:** Snapshot restore replaces the index entirely. Any writes made after the snapshot was taken are lost.
- **Rollback:** Not applicable — snapshot restore is one-way. Re-index from source if available.

**Verification:** `curl -s "http://localhost:9200/_cat/shards?v" | grep "<INDEX_NAME>"` — all shards in `STARTED` state. `_cluster/health` returns `green`.

### Cause Z: Unidentified allocation failure

**Statement:** Shards remain unassigned and none of the identified deciders or configuration causes account for the allocation block. [Default]

**Mechanism:** Allocation failures may arise from unusual combinations of allocation awareness settings, zone awareness constraints, cross-cluster replication blocks, security plugin permission errors, or internal Elasticsearch bugs not covered by the diagnostic steps above.

**Indicator:**

- [Default] All preceding Causes have been evaluated and do not match
- [Step 3] `allocate_explanation` contains an unrecognized decider or explanation string

**Mitigation:**

- **Risk:** Low. Collecting diagnostics does not change cluster state.
- **Command:**

  ```bash
  # Capture full allocation explain output for all unassigned shards
  curl -s "http://localhost:9200/_cluster/allocation/explain?pretty" > /tmp/es-alloc-explain.json
  # Capture cluster state
  curl -s "http://localhost:9200/_cluster/state?pretty" > /tmp/es-cluster-state.json
  # Capture recent Elasticsearch log lines
  journalctl -u elasticsearch --since "1 hour ago" | grep -iE "error|warn|allocation|shard" \
    > /tmp/es-recent.log
  ```

- **Duration:** Collect and escalate to Elastic Support or GitHub issues for open-source deployments.

**Resolution:** Out of runbook scope. Provide `/tmp/es-alloc-explain.json`, `/tmp/es-cluster-state.json`, and `/tmp/es-recent.log` to Elastic Support along with the Elasticsearch version (`GET /`).

**Verification:** Cluster health returns `green` and `unassigned_shards: 0` after resolution with Elastic Support guidance.

## Prevention

1. Implement Index Lifecycle Management (ILM): configure rollover, shrink, and delete phases to control index and shard count proactively. Target 10–50 GB per shard.

2. Alert at 75% disk usage on all data nodes — well before the 85% low watermark fires. Use `elasticsearch_exporter` for Prometheus or Elastic's built-in monitoring stack.

3. Alert immediately when `unassigned_shards > 0` persists for more than 5 minutes. This is the earliest signal of cluster degradation.

4. Size clusters for N-1 redundancy: the remaining nodes after losing one data node must not exceed 85% disk usage.

5. Use dedicated master-eligible nodes (3 nodes) in production clusters with 3+ data nodes to prevent split-brain and ensure stability during data node failures.

6. Set `number_of_replicas: 1` for all production indices. Use `number_of_replicas: 0` only for ephemeral or easily re-indexable data.

7. Configure allocation awareness (`cluster.routing.allocation.awareness.attributes`) to distribute shards across availability zones, ensuring zone-level failures produce yellow (not red) status.

8. Use data streams for time-series data — they enforce rollover, prevent unbounded shard growth, and integrate with ILM out of the box.

9. Periodically test node failure recovery in staging: stop one data node and verify the cluster returns to green within your SLA.

10. Keep `cluster.max_shards_per_node` at the default 1000 and reduce shard count with ILM rather than raising the limit.

## Sources

- [Elasticsearch Reference — Fix Common Cluster Issues](https://www.elastic.co/guide/en/elasticsearch/reference/current/fix-common-cluster-issues.html) — priority 1; watermark thresholds, allocation decider names, reroute commands
- [Elasticsearch Reference — Troubleshooting](https://www.elastic.co/guide/en/elasticsearch/reference/current/troubleshooting.html) — priority 1; overview of cluster allocation topics and index-level blocks
- [Elasticsearch Docs — Red or Yellow Cluster Status](https://www.elastic.co/docs/troubleshoot/elasticsearch/red-yellow-cluster-status) — priority 1; diagnostic API commands and resolution patterns
