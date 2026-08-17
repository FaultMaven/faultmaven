---
id: "es-cluster-yellow-red"
title: "Elasticsearch Cluster Yellow or Red Health Status"
domain: database
service: elasticsearch
symptom_class: [service_unavailable, disk_full]
severity: critical
scope: global
version: "2.0.1"
last_updated: "2026-08-17"
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

### Step 2: List unassigned shards and reason codes

```bash
curl -s "http://localhost:9200/_cat/shards?v&h=index,shard,prirep,state,unassigned.reason&s=state"
```

Expected output: Table of shards. Rows with `state=UNASSIGNED` name the problem shards. The `prirep` column shows `p` (primary) or `r` (replica). The `unassigned.reason` column contains reason codes: `NODE_LEFT`, `ALLOCATION_FAILED`, `INDEX_CREATED`, `REROUTE_CANCELLED`, `CLUSTER_RECOVERED`.

### Step 3: Explain allocation for an unassigned shard

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

### Step 5: Check watermark and allocation-enable settings

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

### Step 7: Check for index read-only blocks

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

### Cause A: Disk watermark exceeded

**Statement:** One or more data nodes have exceeded the low (85%) or flood-stage (95%) disk watermark, preventing Elasticsearch from assigning new shards to those nodes.

**Chain:**
- root: a data node's disk usage rises to or above the low watermark (85%)
- s1: `DiskThresholdDecider` denies shard placement on the over-watermark node
- s2: at the flood stage (95%) affected indices are set `read_only_allow_delete: true`, blocking writes
- s3: these read-only blocks persist even after disk pressure is relieved
- D: shards stay unassigned / indices stay write-blocked → cluster yellow or red (Symptom)

**Indicators:**
- root: [Step 4] `disk.percent` >= 85 on one or more nodes
- s1: [Step 3] `node_allocation_decisions` references `DiskThresholdDecider` with verdict `NO`
- s2: [Step 7] `"index.blocks.read_only_allow_delete": "true"` present on one or more indices

**Interventions:**
- **remediation** (root): implement an ILM delete phase to auto-expire old indices and keep disk below the watermark.

  ```bash
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

  **Verification:** re-run Step 4; all nodes below 85%. ILM applies only to indices with matching `index.lifecycle.name`; adding disk capacity is the safest long-term resolution. Cluster status returns `green` within 10 minutes of reallocation completing.
- **mitigation** (root): free disk by deleting old time-based indices to drop usage below the watermark.

  ```bash
  curl -s "http://localhost:9200/_cat/indices?v&h=index,store.size,docs.count,creation.date.string&s=store.size:desc" | head -20
  curl -X DELETE "http://localhost:9200/logs-2026.01.*"
  ```

  **Risk:** deleting indices causes permanent data loss; verify retention policy and stakeholder sign-off before deletion. **Duration:** minutes to free disk. **Verification:** re-run Step 4; over-watermark nodes drop below 85%.
- **defensive_fix** (s3): clear flood-stage read-only blocks once disk drops below flood stage (blocks do not clear themselves).

  ```bash
  curl -X PUT "http://localhost:9200/_all/_settings?pretty" \
    -H 'Content-Type: application/json' \
    -d '{"index.blocks.read_only_allow_delete": null}'
  ```

  **Verification:** re-run Step 7; no `read_only_allow_delete` results, or all null/false.

### Cause B: Node failure or departure

**Statement:** A data node left the cluster unexpectedly, leaving its primary or replica shards with no host until Elasticsearch reallocates them to surviving nodes.

**Chain:**
- root: a data node departs the cluster unexpectedly (crash, network partition, restart)
- s1: its shards are marked `UNASSIGNED` with reason `NODE_LEFT`
- s2: reallocation waits out `index.unassigned.node_left.delayed_timeout` (default 1m), so status is temporarily degraded
- s3: if the node held the only copy of a primary, no surviving node has the data
- D: unassigned shards persist → cluster yellow (replica lost) or red (primary lost) (Symptom)

**Indicators:**
- s1: [Step 2] `unassigned.reason` = `NODE_LEFT` on multiple shards
- root: [Step 1] `number_of_nodes` lower than expected
- root: [Step 6] expected node name absent from output

**Interventions:**
- **remediation** (s3): if the node is permanently gone but replicas exist on surviving nodes, reroute a replica to promote it.

  ```bash
  curl -X POST "http://localhost:9200/_cluster/reroute?pretty" \
    -H 'Content-Type: application/json' \
    -d '{
      "commands": [{
        "allocate_replica": {"index": "<INDEX>", "shard": 0, "node": "<SURVIVING_NODE>"}
      }]
    }'
  ```

  **Verification:** re-run Step 1; `unassigned_shards: 0` and `status: green`; re-run Step 6 to confirm node count restored or acknowledged as gone.
- **mitigation** (s2): extend the delay if the node will return, then retry allocation after it expires.

  ```bash
  curl -X PUT "http://localhost:9200/_all/_settings?pretty" \
    -H 'Content-Type: application/json' \
    -d '{"index.unassigned.node_left.delayed_timeout": "10m"}'
  curl -X POST "http://localhost:9200/_cluster/reroute?retry_failed=true&pretty"
  ```

  **Risk:** if permanently gone, replicas serve reads but writes remain blocked for affected primaries. **Duration:** up to the delayed_timeout window for the node to return, then reroute. **Verification:** re-run Step 1; `unassigned_shards` decreasing toward 0.
- **mitigation** (s3): last resort if the node held the only primary copy and will not return — force a stale primary, accepting data loss.

  ```bash
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

  **Risk:** `allocate_stale_primary` with `accept_data_loss: true` is irreversible — any writes after the shard became unassigned are permanently lost; not recoverable after execution. **Duration:** immediate; use only when the node is confirmed permanently gone. **Verification:** re-run Step 1; `status: green` and `unassigned_shards: 0`.

### Cause C: Insufficient nodes for replica count

**Statement:** The cluster has fewer data nodes than the configured replica count, making it impossible to satisfy the constraint that no two copies of a shard share the same node.

**Chain:**
- root: the cluster has fewer data nodes than `number_of_replicas + 1`
- s1: `SameShardAllocationDecider` forbids placing a replica on the node already holding its primary
- s2: replica shards stay `UNASSIGNED` with reason `INDEX_CREATED` since no eligible node exists
- D: unassignable replicas → permanent cluster yellow status (Symptom)

**Indicators:**
- s2: [Step 2] `unassigned.reason` = `INDEX_CREATED` on replica shards (`prirep=r`) for all indices
- s1: [Step 3] `node_allocation_decisions` references `SameShardAllocationDecider` with verdict `NO`
- root: [Step 6] node count equals 1 or fewer than `number_of_replicas + 1`

**Interventions:**
- **remediation** (root): add a second data node, then restore replicas so the placement constraint can be satisfied.

  ```bash
  curl -X PUT "http://localhost:9200/_all/_settings?pretty" \
    -H 'Content-Type: application/json' \
    -d '{"index": {"number_of_replicas": 1}}'
  ```

  **Verification:** re-run Step 1; `unassigned_shards: 0` and `status: green`. Adding nodes requires provisioning; restoring replicas triggers shard recovery I/O across all indices.
- **mitigation** (s2): reduce replica count to 0 (development/single-node only) so no replica needs placement.

  ```bash
  curl -X PUT "http://localhost:9200/_all/_settings?pretty" \
    -H 'Content-Type: application/json' \
    -d '{"index": {"number_of_replicas": 0}}'
  ```

  **Risk:** `number_of_replicas: 0` eliminates fault tolerance — if the single node fails, data is lost. **Duration:** immediate; restore to 1 once additional nodes are added. **Verification:** re-run Step 1; `status: green` and `unassigned_shards: 0`.

### Cause D: Shard allocation manually disabled

**Statement:** `cluster.routing.allocation.enable` was set to `none` or `primaries` during maintenance and was never re-enabled, leaving new and unassigned shards stranded.

**Chain:**
- root: an operator set `cluster.routing.allocation.enable` to `none` or `primaries` (e.g. before a rolling restart) and never reverted it
- s1: the cluster refuses to place any unassigned shard while allocation is disabled
- D: unassigned shards remain stranded → cluster yellow or red (Symptom)

**Indicators:**
- root: [Step 5] `cluster.routing.allocation.enable` = `none` or `primaries`
- s1: [Step 3] `allocate_explanation` contains `allocation is disabled` with no disk/hardware reason

**Interventions:**
- **remediation** (root): re-enable allocation by clearing the override (returns to default `all`).

  ```bash
  curl -X PUT "http://localhost:9200/_cluster/settings?pretty" \
    -H 'Content-Type: application/json' \
    -d '{"persistent": {"cluster.routing.allocation.enable": null}}'
  ```

  **Risk:** if allocation was disabled due to an active issue, re-enabling may cause unwanted shard movement before the root cause is resolved. **Verification:** re-run Step 5; no `allocation.enable` override value. Monitor `initializing_shards` via Step 1 until it returns to 0.

### Cause E: Allocation filter excludes all nodes

**Statement:** Index-level `index.routing.allocation.require`/`include` settings reference node attributes that no currently-available data node satisfies, causing all allocation decisions to return `NO`.

**Chain:**
- root: an index's routing filter requires a node attribute (e.g. `zone: us-east-1a`) that no available data node carries
- s1: `FilterAllocationDecider` rejects every node when comparing the filter to node attributes
- s2: the shard cannot be placed anywhere and stays unassigned; this silently persists until corrected
- D: unassignable shards → cluster yellow or red (Symptom)

**Indicators:**
- s1: [Step 3] `node_allocation_decisions` references `FilterAllocationDecider` with verdict `NO` on every node
- root: [Step 3] `allocate_explanation` names a node attribute value absent from `_cat/nodes` output

**Interventions:**
- **remediation** (root): correct the filter to match actual node attributes (or remove it entirely).

  ```bash
  curl -s "http://localhost:9200/<INDEX_NAME>/_settings?pretty" | grep -E "routing\.allocation"
  curl -X PUT "http://localhost:9200/<INDEX_NAME>/_settings?pretty" \
    -H 'Content-Type: application/json' \
    -d '{"index.routing.allocation.require.zone": "<CORRECT_ZONE_VALUE>"}'
  ```

  **Risk:** correcting a zone filter may relocate shards across nodes, causing recovery I/O; per-index change with no cluster-wide blast radius. **Verification:** re-run Step 3; `can_allocate` changes from `NO` to `YES`; cluster returns `green` within minutes.
- **mitigation** (s1): remove the filter to immediately unblock allocation if the constraint is not safety-critical.

  ```bash
  curl -X PUT "http://localhost:9200/<INDEX_NAME>/_settings?pretty" \
    -H 'Content-Type: application/json' \
    -d '{"index.routing.allocation.require._name": null}'
  ```

  **Risk:** removing the filter may place shards on unintended nodes if it enforced data-locality (e.g. compliance zone pinning). **Duration:** immediate; restore the corrected filter as soon as a matching node exists. **Verification:** re-run Step 3; shards allocate on the next allocation cycle.

### Cause F: Allocation retry limit exhausted

**Statement:** Elasticsearch attempted to allocate a shard multiple times, exhausted its retry limit (default 5), and marked the shard permanently unallocatable until an operator resets the counter.

**Chain:**
- root: a transient failure (e.g. momentary node overload) makes the first allocation attempts fail
- s1: after 5 failed attempts `MaxRetryAllocationDecider` marks the shard `max_retry` and stops automatic retries
- s2: the retry counter is not reset automatically, so the shard stays unassigned even after the original cause clears
- D: shard remains unassigned → cluster yellow or red (Symptom)

**Indicators:**
- s1: [Step 3] `allocate_explanation` contains `too many allocation attempts` or `MaxRetryAllocationDecider`
- root: [Step 2] `unassigned.reason` = `ALLOCATION_FAILED`

**Interventions:**
- **remediation** (root): investigate the original allocation failure in the logs to prevent recurrence, then reset the retry counter.

  ```bash
  grep "ALLOCATION_FAILED" /var/log/elasticsearch/*.log
  curl -X POST "http://localhost:9200/_cluster/reroute?retry_failed=true&pretty"
  ```

  **Verification:** re-run Step 3; the explanation no longer references `MaxRetryAllocationDecider`; Step 1 shows `initializing_shards > 0` briefly then `unassigned_shards: 0`.
- **mitigation** (s2): reset the retry counter to trigger a fresh allocation attempt without first finding the root cause.

  ```bash
  curl -X POST "http://localhost:9200/_cluster/reroute?retry_failed=true&pretty"
  ```

  **Risk:** if the underlying cause is unresolved, the failure cycle repeats and exhausts retries again. **Duration:** seconds for the call; shard placement takes seconds to minutes. **Verification:** re-run Step 1; `initializing_shards > 0` then `unassigned_shards: 0`.

### Cause G: Max shards per node reached

**Statement:** The cluster-wide `cluster.max_shards_per_node` ceiling (default 1000) has been reached on one or more data nodes, preventing any additional shard assignments.

**Chain:**
- root: many small indices (e.g. daily log indices accumulating over months) push total open shards toward `max_shards_per_node * number_of_data_nodes`
- s1: `ShardsLimitAllocationDecider` denies further shard assignments once the ceiling is hit
- D: new and unassigned shards cannot be placed → cluster yellow or red (Symptom)

**Indicators:**
- root: [Step 8] `cluster.max_shards_per_node` at or near total shard count divided by node count
- s1: [Step 3] `allocate_explanation` contains `too many shards` or `ShardsLimitAllocationDecider`

**Interventions:**
- **remediation** (root): delete old, unneeded time-based indices to reduce total shard count, then reset the limit to default.

  ```bash
  curl -s "http://localhost:9200/_cat/indices?v&h=index,docs.count,store.size&s=index" | grep -E "^logs-202[0-4]"
  curl -X DELETE "http://localhost:9200/logs-2024.*"
  curl -X PUT "http://localhost:9200/_cluster/settings?pretty" \
    -H 'Content-Type: application/json' \
    -d '{"persistent": {"cluster.max_shards_per_node": null}}'
  ```

  **Risk:** deleting indices is permanent; ensure actual shard count is below 1000/node before resetting the limit to null. **Verification:** re-run Step 1; `unassigned_shards: 0`; total shards / data nodes < `cluster.max_shards_per_node`.
- **mitigation** (s1): temporarily raise the ceiling while consolidating indices so shards can allocate now.

  ```bash
  curl -X PUT "http://localhost:9200/_cluster/settings?pretty" \
    -H 'Content-Type: application/json' \
    -d '{"persistent": {"cluster.max_shards_per_node": 2000}}'
  ```

  **Risk:** raising above 1000/node degrades performance — each shard consumes heap, file descriptors, and threads. **Duration:** immediate; revert after shard count is reduced. **Verification:** re-run Step 1; `unassigned_shards` drops toward 0 as shards allocate.

### Cause H: Corrupt or unrecoverable shard data

**Statement:** A data node experienced an unclean shutdown or storage fault that corrupted a shard's segment files, causing every allocation attempt to fail with a shard corruption exception.

**Chain:**
- root: an unclean shutdown or storage fault corrupts a shard's segment files (checksum failure or missing segments)
- s1: Elasticsearch fails segment validation when loading the shard and marks it `ALLOCATION_FAILED`
- s2: `MaxRetryAllocationDecider` quickly exhausts retries on the corrupt copy
- s3: with no healthy replica, the only copies are corrupt
- D: the shard cannot be allocated from any copy → cluster red (Symptom)

**Indicators:**
- s1: [Step 3] `allocate_explanation` contains `shard corruption` or `failed shard on allocating node`
- s3: [Step 2] `unassigned.reason` = `ALLOCATION_FAILED` on a primary shard with no corresponding healthy replica

**Interventions:**
- **remediation** (root): restore the index from a snapshot when data loss from an empty primary is unacceptable.

  ```bash
  curl -X POST "http://localhost:9200/_snapshot/<REPO>/<SNAPSHOT>/_restore?pretty" \
    -H 'Content-Type: application/json' \
    -d '{"indices": "<INDEX_NAME>", "ignore_unavailable": true}'
  ```

  **Risk:** snapshot restore replaces the index entirely; any writes after the snapshot are lost and the restore is one-way (re-index from source if available). **Verification:** re-run Step 2; all shards of the index in `STARTED` state; cluster returns `green`.
- **defensive_fix** (s2): if a healthy replica exists, retry allocation so the shard re-syncs from the replica instead of staying stuck on the corrupt copy.

  ```bash
  curl -s "http://localhost:9200/_cat/shards?v&h=index,shard,prirep,state,node" | grep "<INDEX_NAME>"
  curl -X POST "http://localhost:9200/_cluster/reroute?retry_failed=true&pretty"
  ```

  **Verification:** re-run Step 2; shard reaches `STARTED`; recovery from a healthy replica may take minutes to hours depending on shard size.
- **mitigation** (s3): last resort when no replica exists — allocate an empty primary, losing all data in that shard.

  ```bash
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

  **Risk:** `allocate_empty_primary` creates an empty shard — all data in that shard is permanently lost. **Duration:** immediate; use only after confirming no healthy replica or snapshot exists. **Verification:** re-run Step 2; the shard reaches `STARTED` (empty); cluster returns `green`.

### Cause Z: Unidentified allocation failure

**Statement:** Shards remain unassigned and none of the identified deciders or configuration causes account for the allocation block.

**Indicators:**
- [Default]

**Interventions:**
- **mitigation** (D): capture a full diagnostic snapshot and escalate to the SME / Elastic Support.

  ```bash
  curl -s "http://localhost:9200/_cluster/allocation/explain?pretty" > /tmp/es-alloc-explain.json
  curl -s "http://localhost:9200/_cluster/state?pretty" > /tmp/es-cluster-state.json
  journalctl -u elasticsearch --since "1 hour ago" | grep -iE "error|warn|allocation|shard" \
    > /tmp/es-recent.log
  ```

  **Risk:** low — collecting diagnostics does not change cluster state. **Duration:** collect and escalate to Elastic Support (or GitHub issues for open-source deployments) with the Elasticsearch version (`GET /`). **Verification:** re-run Step 1; cluster returns `green` and `unassigned_shards: 0` after resolution with SME/Elastic Support guidance.

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

- [Elasticsearch Reference — Fix Common Cluster Issues](https://www.elastic.co/docs/troubleshoot/elasticsearch/fix-common-cluster-issues) — priority 1; watermark thresholds, allocation decider names, reroute commands
- [Elasticsearch Reference — Troubleshooting](https://www.elastic.co/docs/troubleshoot/elasticsearch) — priority 1; overview of cluster allocation topics and index-level blocks
- [Elasticsearch Docs — Red or Yellow Cluster Status](https://www.elastic.co/docs/troubleshoot/elasticsearch/red-yellow-cluster-status) — priority 1; diagnostic API commands and resolution patterns
