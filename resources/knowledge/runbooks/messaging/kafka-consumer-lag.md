---
id: kafka-consumer-lag
title: "Kafka Consumer Lag"
domain: messaging
service: kafka
symptom_class:
  - latency
  - throughput-degradation
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-05-12"
verified_by: kb-researcher
status: draft
tags:
  - kafka
  - consumer
  - lag
  - consumer-group
  - rebalance
  - cooperative-sticky
  - static-membership
  - max-poll-interval
difficulty: intermediate
---

# Kafka Consumer Lag

## Symptom Recognition

- `kafka-consumer-groups.sh --describe --group <g>` shows the `LAG` column rising over time on one or more partitions — `LAG = LOG-END-OFFSET - CURRENT-OFFSET` and a healthy consumer holds it in single digits.
- The consumer JMX metric `kafka.consumer:type=consumer-fetch-manager-metrics,client-id=*` `records-lag-max` is non-zero and trending up; `records-consumed-rate` flat or down while broker `kafka.server:type=BrokerTopicMetrics,name=MessagesInPerSec,topic=*` is steady or up.
- Consumer group state reported by `kafka-consumer-groups.sh ... --state` is repeatedly `PreparingRebalance` or `CompletingRebalance` rather than `Stable`; `kafka.consumer:type=consumer-coordinator-metrics` `rebalance-rate-per-hour` is elevated.
- Consumer logs contain `Member ... sending LeaveGroup request`, `Attempt to heartbeat failed since group is rebalancing`, `consumer poll timeout has expired. This means the time between subsequent calls to poll() was longer than the configured max.poll.interval.ms`, or `CommitFailedException: Commit cannot be completed since the group has already rebalanced`.
- Downstream SLOs reported on producer-to-consumer end-to-end latency degrade; clients of the consuming service report stale data.
- `LAG` is uneven across partitions in the same consumer group — some partitions sit at zero while others grow into the thousands or millions, indicating skewed assignment or hot keys rather than uniform under-capacity.
- The `CURRENT-OFFSET` column in `--describe` shows `-` for one or more partitions, meaning the partition has no active consumer in the group (more partitions than members, or a member crashed mid-rebalance).

## Applicability

- Apache Kafka 2.4+ brokers and Java client 2.4+ (or librdkafka 1.6+) where the cooperative rebalance protocol and static membership are available. The configuration defaults referenced below match Apache Kafka 3.x / Confluent Platform 7.x.
- Shell access to a host with `kafka-consumer-groups.sh`, `kafka-topics.sh`, `kafka-run-class.sh`, and `kafka-console-consumer.sh` on the `PATH` (any Kafka client distribution).
- Network reachability from the diagnostic host to at least one broker's listener (typically `9092` PLAINTEXT or `9093` TLS); credentials for SASL/SSL listeners if enabled.
- Read access to the consumer application's logs (typically `/var/log/<app>/consumer.log` or equivalent container `stdout`) and to its `consumer.properties` / programmatic `ConsumerConfig`.
- Either JMX exposure on the consumer JVM (port and `-Dcom.sun.management.jmxremote*` flags) or a Prometheus JMX exporter scrape so consumer-side metrics are observable.
- Permission to scale the consumer deployment (`kubectl scale`, ASG desired count, etc.) and to alter topic partition counts (`kafka-topics.sh --alter`) — the latter is irreversible.
- Identity of the affected consumer group (`group.id`), the topic(s) it subscribes to, and the `bootstrap.servers` list.

## Diagnostic Steps

### Step 1: Capture per-partition lag and confirm the group is the bottleneck

```bash
BS=kafka1:9092
GROUP=my-consumer-group
kafka-consumer-groups.sh --bootstrap-server $BS --describe --group $GROUP
kafka-consumer-groups.sh --bootstrap-server $BS --describe --group $GROUP \
  | awk 'NR>1 && $1!="" {sum+=$6; if($6>max){max=$6}; n++}
         END {printf "total_lag=%d max_partition_lag=%d partitions=%d\n", sum+0, max+0, n+0}'
```

Expected output: header row followed by one line per partition with columns `TOPIC PARTITION CURRENT-OFFSET LOG-END-OFFSET LAG CONSUMER-ID HOST CLIENT-ID`. The `awk` summary reports `total_lag`, `max_partition_lag`, and `partitions`. Any partition with `LAG` consistently >1000 (or growing faster than the consume rate) is lagging. A `CURRENT-OFFSET` of `-` means no active member owns that partition.

### Step 2: Check group state and rebalance frequency

```bash
kafka-consumer-groups.sh --bootstrap-server $BS --describe --group $GROUP --state
kafka-consumer-groups.sh --bootstrap-server $BS --describe --group $GROUP --members --verbose
grep -E "Rebalance|JoinGroup|SyncGroup|LeaveGroup|max\.poll\.interval|poll timeout|CommitFailed" \
  /var/log/<app>/consumer.log 2>/dev/null | tail -50
```

Expected output: from `--state`, a line ending in `STATE: Stable` plus `#MEMBERS: <N>` and `ASSIGNMENT-STRATEGY`. From `--members --verbose`, each member with its `CLIENT-ID`, `HOST`, `#PARTITIONS`, and the per-partition assignment list. A state other than `Stable` (`PreparingRebalance`, `CompletingRebalance`, `Dead`, `Empty`) confirms the group is unhealthy. Log lines showing repeated `LeaveGroup` / `JoinGroup` within minutes, or `consumer poll timeout has expired`, prove rebalance churn rather than steady-state under-capacity.

### Step 3: Measure consumer-side fetch and processing balance via JMX

```bash
# Replace with your JMX exporter scrape, jconsole, or jcmd output
JMX_HOST=consumer-host
JMX_PORT=9999
jcmd $(pgrep -of java) PerfCounter.print 2>/dev/null | head
# Prometheus example
curl -s http://$JMX_HOST:9404/metrics \
  | grep -E '^kafka_consumer_(fetch_manager|coordinator)_(records_lag_max|records_consumed_rate|fetch_rate|poll_idle_ratio_avg|rebalance_rate_per_hour|last_rebalance_seconds_ago)' \
  | head -30
```

Expected output: numeric values for `records-lag-max`, `records-consumed-rate`, `fetch-rate`, `poll-idle-ratio-avg`, `rebalance-rate-per-hour`, `last-rebalance-seconds-ago`. `poll-idle-ratio-avg` near 1.0 means the consumer is idle waiting for messages (broker/fetch is the bottleneck); near 0.0 means the application is CPU/IO-bound between `poll()` calls (processing is the bottleneck). `rebalance-rate-per-hour > 1` is abnormal for a stable group.

### Step 4: Compare consumer count to partition count and verify balanced assignment

```bash
TOPIC=my-topic
kafka-topics.sh --bootstrap-server $BS --describe --topic $TOPIC \
  | awk 'NR==1 {for(i=1;i<=NF;i++) if($i=="PartitionCount:") print "partitions="$(i+1)}'
kafka-consumer-groups.sh --bootstrap-server $BS --describe --group $GROUP --members \
  | awk 'NR>1 && $1!="" {n++; p+=$3} END {printf "members=%d total_partitions_assigned=%d\n", n+0, p+0}'
kafka-consumer-groups.sh --bootstrap-server $BS --describe --group $GROUP --members \
  | awk 'NR>1 && $1!="" {print $3}' | sort | uniq -c | sort -rn
```

Expected output: a `partitions=<N>` line, a `members=<M> total_partitions_assigned=<P>` line, and a histogram of partition counts per member. Healthy: `M <= N`, every member has `floor(N/M)` or `ceil(N/M)` partitions, and `P == N`. Skew (e.g., one member with 6 partitions, others with 2) or `M > N` (excess idle members) localises the imbalance.

### Step 5: Measure incoming production rate to rule out a producer spike

```bash
TOPIC=my-topic
WINDOW=60
kafka-run-class.sh kafka.tools.GetOffsetShell --bootstrap-server $BS --topic $TOPIC --time -1 \
  | awk -F: '{s+=$3} END {print "log_end_total_t0=" s+0}' > /tmp/k0
sleep $WINDOW
kafka-run-class.sh kafka.tools.GetOffsetShell --bootstrap-server $BS --topic $TOPIC --time -1 \
  | awk -F: '{s+=$3} END {print "log_end_total_t1=" s+0}' > /tmp/k1
paste /tmp/k0 /tmp/k1 \
  | awk -v w=$WINDOW '{split($1,a,"="); split($2,b,"="); printf "messages_in_per_sec=%.1f\n", (b[2]-a[2])/w}'
```

Expected output: `messages_in_per_sec=<rate>`. Compare against the baseline (`kafka.server:type=BrokerTopicMetrics,name=MessagesInPerSec,topic=<topic>`) recorded before the incident. A current rate within ±20% of baseline rules out producer spike; a rate >2x baseline points to traffic burst rather than consumer regression.

### Step 6: Review consumer configuration for the rebalance-prone defaults

```bash
# From a running JVM
jcmd $(pgrep -of <app>) VM.system_properties 2>/dev/null \
  | grep -E "max\.poll\.records|max\.poll\.interval\.ms|session\.timeout\.ms|heartbeat\.interval\.ms|partition\.assignment\.strategy|group\.instance\.id|fetch\.min\.bytes|fetch\.max\.wait\.ms|max\.partition\.fetch\.bytes"
# Or from a static config file
grep -E "max\.poll\.records|max\.poll\.interval\.ms|session\.timeout\.ms|heartbeat\.interval\.ms|partition\.assignment\.strategy|group\.instance\.id|fetch\.min\.bytes|fetch\.max\.wait\.ms|max\.partition\.fetch\.bytes" \
  /etc/<app>/consumer.properties 2>/dev/null
```

Expected output: each configured key with its value, or no output if the key is left at the client default. Reference defaults (Apache Kafka 3.x / Confluent 7.x clients): `max.poll.records=500`, `max.poll.interval.ms=300000`, `session.timeout.ms=45000`, `heartbeat.interval.ms=3000`, `fetch.min.bytes=1`, `fetch.max.wait.ms=500`, `max.partition.fetch.bytes=1048576`, `partition.assignment.strategy=[RangeAssignor, CooperativeStickyAssignor]`, `group.instance.id=null`.

### Step 7: Capture the GC and processing profile of one slow consumer

```bash
PID=$(pgrep -of <app>)
jstat -gcutil $PID 1s 10
jstack $PID > /tmp/consumer-stack.$$
grep -E "consumer|kafka|poll" /tmp/consumer-stack.$$ | head -40
# If async-profiler / perf is available
# async-profiler.sh -d 30 -f /tmp/consumer-cpu.html $PID
```

Expected output: from `jstat`, ten lines of `S0 S1 E O M CCS YGC YGCT FGC FGCT GCT`. Sustained `O > 90` (old gen >90%) and frequent `FGC` increments mean GC is stealing wall-clock from the poll loop. From `jstack`, the consumer's poll thread should be in `Consumer.poll()` waiting for fetch, not blocked inside the application handler. Stacks dominated by `RUNNABLE` frames inside the application handler (DB driver, HTTP client) confirm processing is the bottleneck.

## Causes

### Cause A: Per-batch processing exceeds `max.poll.interval.ms` and triggers repeated rebalances

**Statement:** The consumer's processing loop occasionally takes longer than `max.poll.interval.ms` to finish a batch and call `poll()` again, so the group coordinator removes it from the group, causing a rebalance and zero throughput on its partitions until it rejoins.

**Mechanism:** Each `poll()` returns up to `max.poll.records` records (default 500). The application must complete the batch and call `poll()` again within `max.poll.interval.ms` (default 300000 ms). If a slow downstream call, GC pause, or a single oversized batch pushes that time over the limit, the coordinator marks the member dead, reassigns its partitions, and the next `commitSync`/`commitAsync` from the evicted member fails with `CommitFailedException`. While the rebalance runs, no member processes — lag grows on every partition.

**Indicator:**

- [Step 2] consumer log contains `consumer poll timeout has expired. This means the time between subsequent calls to poll() was longer than the configured max.poll.interval.ms`
<!-- match: {"step": 2, "predicate": "contains", "target": "consumer poll timeout has expired"} -->
- [Step 2] consumer log contains `CommitFailedException`
<!-- match: {"step": 2, "predicate": "contains", "target": "CommitFailedException"} -->
- [Step 3] `kafka_consumer_coordinator_metrics_rebalance_rate_per_hour > 1` over the affected window
<!-- match: {"step": 3, "predicate": "threshold", "target": "rebalance_rate_per_hour", "op": ">", "value": 1} -->

**Mitigation:**

- **Risk:** Lowering `max.poll.records` reduces the work per `poll()` but increases the number of `poll()` round-trips per second; verify broker fetch capacity. Raising `max.poll.interval.ms` extends the time before a slow consumer is detected as dead — if processing is genuinely stuck, lag will grow longer before recovery.
- **Command:**

  ```bash
  # Edit application config — example for a Spring/Java client
  # max.poll.records=100
  # max.poll.interval.ms=600000
  kubectl set env deployment/<app> \
    KAFKA_CONSUMER_MAX_POLL_RECORDS=100 \
    KAFKA_CONSUMER_MAX_POLL_INTERVAL_MS=600000
  kubectl rollout restart deployment/<app>
  ```

- **Duration:** Until the durable fix in Resolution lands; this is a config-only stop-gap, not a fix for the underlying slow processing path.

**Resolution:**

```bash
# Code-side: hand each batch from poll() to a bounded worker pool and
# call poll() again immediately. The poll thread keeps heartbeats flowing
# and respects max.poll.interval.ms; the workers do the slow I/O.
#
# Java sketch (illustrative — adapt to your client library):
#   while (running) {
#     ConsumerRecords<K,V> recs = consumer.poll(Duration.ofMillis(500));
#     workerPool.submit(() -> processBatch(recs));
#     consumer.commitAsync(offsetsAfterEnqueue(recs), this::handleCommit);
#   }
#
# Pair the code change with right-sized config:
cat > /etc/<app>/consumer.properties.d/poll-tuning.properties <<'EOF'
max.poll.records=200
max.poll.interval.ms=600000
EOF
kubectl rollout restart deployment/<app>
```

**Impact:** Cluster-wide for the consumer deployment. Decoupling poll from processing changes the at-least-once delivery boundary — commit only after the worker has durably handled the records, or use idempotent downstream writes. Restart is rolling; rebalances will occur during rollout.

**Rollback:** Remove the `poll-tuning.properties` drop-in (or revert the env vars) and redeploy the previous container image: `kubectl rollout undo deployment/<app>`.

**Verification:** After 30 minutes at production load, `kafka_consumer_coordinator_metrics_rebalance_rate_per_hour` returns to near zero, `consumer poll timeout has expired` no longer appears in logs, and Step 1's `total_lag` is decreasing.

### Cause B: Consumer count is less than partition count — sustained under-capacity

**Statement:** The consumer group has fewer members than the topic has partitions, so the per-member partition load exceeds what a single instance can process at the incoming message rate, and lag grows on every member's partitions.

**Mechanism:** A single partition is consumed by at most one member of a group; with `M` members and `N` partitions where `M < N`, each member owns `ceil(N/M)` partitions and must process the combined production rate of all of them. If per-member throughput (records/sec) is less than the per-member incoming rate (partition_count_per_member * messages_in_per_sec / N), lag grows linearly. Adding members up to `N` linearly improves group throughput; adding members beyond `N` leaves the extras idle.

**Indicator:**

- [Step 4] `members < partitions` reported by the `members=` / `partitions=` lines from `kafka-topics.sh --describe` and `kafka-consumer-groups.sh --members`
- [Step 4] every consumer in the histogram has `>= ceil(partitions/members)` partitions assigned and lag is rising on the majority of them
- [Step 5] `messages_in_per_sec` is within ±20% of historical baseline (the producer is not spiking)

**Mitigation:**

- **Risk:** Scaling consumers triggers a rebalance; with `RangeAssignor` (eager protocol) this is stop-the-world for seconds to minutes. Use `CooperativeStickyAssignor` (see Cause D) to make the rebalance incremental.
- **Command:**

  ```bash
  # Scale up to but not above partition count
  PARTITIONS=$(kafka-topics.sh --bootstrap-server $BS --describe --topic $TOPIC \
    | awk 'NR==1 {for(i=1;i<=NF;i++) if($i=="PartitionCount:") print $(i+1)}')
  kubectl scale deployment/<app> --replicas=$PARTITIONS
  kubectl rollout status deployment/<app>
  ```

- **Duration:** Permanent. Re-scale down only after sustained `total_lag = 0` for an hour and headroom on a single member.

**Resolution:** Same as Mitigation.

**Impact:** Cluster-wide for the consumer deployment. Increases cost linearly with replica count. One rebalance occurs at scale-up — graceful with `CooperativeStickyAssignor`, stop-the-world with `RangeAssignor`.

**Rollback:** `kubectl scale deployment/<app> --replicas=<previous>` — this also triggers one rebalance.

**Verification:** Step 4 reports `members == partitions` (or chosen target), Step 1 shows `total_lag` falling within the next consume-rate * time window, and `kafka_consumer_fetch_manager_metrics_records_lag_max` trends toward 0.

### Cause C: Partition count is the ceiling — group is fully consumed but topic is under-partitioned

**Statement:** The consumer group already has one member per partition, so adding more consumers cannot help; the topic itself does not provide enough parallelism for the sustained production rate.

**Mechanism:** Kafka assigns at most one consumer per partition within a group. When `members == partitions` and lag still grows, the per-partition throughput ceiling has been hit — either the production rate exceeds what a single consumer can drain from one partition, or partition keys concentrate traffic on a subset of partitions ("hot partitions"). Adding members beyond `partitions` produces idle members. The only way to add parallelism is to increase the topic's partition count, which is an irreversible operation and breaks per-key ordering for clients that rely on it.

**Indicator:**

- [Step 4] `members == partitions` (or `members > partitions` with excess idle members reported as `#PARTITIONS=0`)
- [Step 4] partition-count histogram is flat at 1 per member yet [Step 1] `total_lag` is still growing
- [Step 5] `messages_in_per_sec` is at or above the historical baseline and per-member CPU is not saturated (room to consume faster if partitions allowed)

**Mitigation:**

- **Risk:** `kafka-topics.sh --alter --partitions` cannot be reversed; adding partitions changes the partition assignment of new messages by key (`hash(key) % partitions`) and breaks ordering guarantees for any consumer that relies on key-based ordering. Document the change and confirm downstream consumers tolerate the re-keying.
- **Command:**

  ```bash
  # Dry-run: confirm current partition count before altering
  kafka-topics.sh --bootstrap-server $BS --describe --topic $TOPIC | head -1
  ```

- **Duration:** Diagnostic only; the durable change is in Resolution.

**Resolution:**

```bash
# Double the partition count; pick a multiple that distributes hot keys evenly.
NEW=24
kafka-topics.sh --bootstrap-server $BS --alter --topic $TOPIC --partitions $NEW
# Scale consumers to match
kubectl scale deployment/<app> --replicas=$NEW
```

**Impact:** Cluster-wide and permanent. New partition assignment for keyed records means consumers that group state by key may briefly see records arrive on a different partition than historical state suggests; plan a state migration or accept the discontinuity. Each broker now hosts more partition replicas — verify broker disk and file-descriptor headroom.

**Rollback:** Partition count cannot be decreased. To revert, create a new topic with the old partition count, mirror existing data into it (e.g., MirrorMaker 2), repoint producers and consumers, and decommission the over-partitioned topic.

**Verification:** Step 1's `total_lag` falls within minutes after the scale-up; Step 4's `partitions=` and `members=` both equal `NEW`; `kafka-topics.sh --describe --topic $TOPIC` lists `NEW` partitions with leaders and ISRs healthy.

### Cause D: Eager rebalance protocol causes stop-the-world pauses on every membership change

**Statement:** The consumer group is configured with `RangeAssignor` (or another eager-protocol assignor), so every membership change revokes all partitions from all members before reassignment, halting consumption for the duration of the rebalance.

**Mechanism:** Under the eager rebalance protocol every member must revoke its full assignment, send a JoinGroup, wait for the leader's SyncGroup, and only then resume. While the rebalance runs no member is consuming any partition — lag grows uniformly on every partition for the duration (typically seconds, up to minutes under load). KIP-429's `CooperativeStickyAssignor` switches to incremental rebalancing: members keep partitions they will continue to own and only revoke the ones being moved, so steady-state throughput is preserved during scale events.

**Indicator:**

- [Step 2] `kafka-consumer-groups.sh ... --state` reports `ASSIGNMENT-STRATEGY: range` (or `roundrobin`, `sticky`) rather than `cooperative-sticky`
<!-- match: {"step": 2, "predicate": "contains", "target": "range"} -->
- [Step 6] `partition.assignment.strategy` does not contain `org.apache.kafka.clients.consumer.CooperativeStickyAssignor`
<!-- match: {"step": 6, "predicate": "absent", "target": "CooperativeStickyAssignor"} -->
- [Step 2] consumer logs show `Revoking previously assigned partitions` followed by `(Re-)joining group` on every membership change

**Mitigation:**

- **Risk:** Switching assignors requires a two-bounce rolling upgrade. A single-bounce swap from eager to cooperative is rejected by the broker and the group will be stuck in `PreparingRebalance`. Follow the documented two-step procedure exactly.
- **Command:**

  ```bash
  # First bounce: ADD CooperativeStickyAssignor alongside the existing one.
  # partition.assignment.strategy=org.apache.kafka.clients.consumer.CooperativeStickyAssignor,\
  #                              org.apache.kafka.clients.consumer.RangeAssignor
  kubectl set env deployment/<app> \
    KAFKA_CONSUMER_PARTITION_ASSIGNMENT_STRATEGY="org.apache.kafka.clients.consumer.CooperativeStickyAssignor,org.apache.kafka.clients.consumer.RangeAssignor"
  kubectl rollout restart deployment/<app>
  ```

- **Duration:** Hours. Hold here until every replica has restarted and the group state is `Stable`, then proceed to the second bounce.

**Resolution:**

```bash
# Second bounce: REMOVE the eager assignor. Only run after every replica has
# completed the first bounce and the group is Stable.
kubectl set env deployment/<app> \
  KAFKA_CONSUMER_PARTITION_ASSIGNMENT_STRATEGY="org.apache.kafka.clients.consumer.CooperativeStickyAssignor"
kubectl rollout restart deployment/<app>
# Verify the switchover
kafka-consumer-groups.sh --bootstrap-server $BS --describe --group $GROUP --state
```

**Impact:** Cluster-wide for the consumer deployment. The first bounce produces one eager rebalance per replica; the second produces a single protocol switch from EAGER to COOPERATIVE that is itself implemented as a final eager rebalance. After the switch, all future membership changes are incremental.

**Rollback:** Reverse the env var on the affected revision (`kubectl rollout undo deployment/<app>` twice) — same two-bounce constraint applies in reverse.

**Verification:** `kafka-consumer-groups.sh ... --state` reports `ASSIGNMENT-STRATEGY: cooperative-sticky`; subsequent scale events show `Revoking previously assigned partitions` only for the partitions actually being moved, and Step 1's `total_lag` does not spike during a scale-up.

### Cause E: Transient consumer restarts trigger rebalances; static membership is not configured

**Statement:** Each consumer restart — pod rescheduling, rolling deploy, brief network blip — is treated as a permanent departure because `group.instance.id` is unset, so the coordinator runs a full rebalance instead of waiting for the member to return.

**Mechanism:** Without `group.instance.id` every consumer is a dynamic member, identified only by an ephemeral `member.id` issued at JoinGroup. A `LeaveGroup` (sent on graceful shutdown) or a `session.timeout.ms` expiry triggers a rebalance immediately. With KIP-345 static membership, setting `group.instance.id` to a stable per-replica string makes the broker key on that ID; transient absences within `session.timeout.ms` do not trigger a rebalance, and the returning member resumes its previous assignment.

**Indicator:**

- [Step 2] consumer logs show `LeaveGroup` followed by `JoinGroup` correlated with pod restarts, deployment rollouts, or node maintenance
- [Step 6] `group.instance.id` is null (no output) for every consumer
<!-- match: {"step": 6, "predicate": "absent", "target": "group.instance.id"} -->
- [Step 3] `kafka_consumer_coordinator_metrics_last_rebalance_seconds_ago` is small (recent) and `rebalance-rate-per-hour > 1`, with timestamps that correlate with deployment activity rather than load changes

**Mitigation:**

- **Risk:** Static membership defers rebalance for up to `session.timeout.ms` (default 45000 ms; broker max raised to 1800000 ms under KIP-345). If a static member is permanently gone, its partitions remain unassigned for that window — set `session.timeout.ms` to balance restart tolerance against detection latency.
- **Command:**

  ```bash
  # Set a stable instance id from the pod's ordinal index (StatefulSet) or pod name (Deployment)
  kubectl patch deployment/<app> --type=strategic -p '
  spec:
    template:
      spec:
        containers:
        - name: <app>
          env:
          - name: KAFKA_CONSUMER_GROUP_INSTANCE_ID
            valueFrom:
              fieldRef:
                fieldPath: metadata.name
          - name: KAFKA_CONSUMER_SESSION_TIMEOUT_MS
            value: "60000"
  '
  ```

- **Duration:** Permanent. Static membership is the documented production pattern for long-lived stateful consumers.

**Resolution:** Same as Mitigation.

**Impact:** Cluster-wide for the consumer deployment. Each replica now has a stable identity; restarts within `session.timeout.ms` (60 s above) cause zero rebalances. Replacing a replica with a new pod name (e.g., re-creating the StatefulSet) does count as a new identity and triggers one rebalance.

**Rollback:** Remove the `KAFKA_CONSUMER_GROUP_INSTANCE_ID` env var via `kubectl patch` and `kubectl rollout restart`; revert to dynamic membership.

**Verification:** After a rolling restart of the consumer deployment, Step 3's `rebalance-rate-per-hour` stays at 0 over the rollout window (instead of spiking once per replica), and consumer logs show no `JoinGroup`/`SyncGroup` for transient restarts within `session.timeout.ms`.

### Cause F: Partition assignment is skewed — hot keys concentrate traffic on a subset of partitions

**Statement:** Production is partitioned by a key that does not distribute evenly across partitions, so a small number of partitions carry most of the traffic, and the consumers owning those partitions lag while others run idle.

**Mechanism:** Kafka's default partitioner maps `partition = hash(key) % partition_count`. If the key space is skewed (e.g., a small set of high-traffic tenant IDs, geographic clustering, or accidental near-constant keys), the resulting hash distribution concentrates records on a few partitions. The consumer assigned to a hot partition processes the combined hot-key rate; its peers see the LAG column flat at 0. Adding consumers or partitions does not redistribute existing hot keys — the hash still places them on the same partition modulo the new count.

**Indicator:**

- [Step 1] `max_partition_lag` is orders of magnitude larger than the median partition lag; the lag is concentrated on a handful of partitions
- [Step 4] partition-count histogram is balanced (each member has the same number of partitions) yet [Step 1] LAG is uneven
- [Symptom] business metrics show a small number of keys (tenant IDs, hash buckets) dominating production rate

**Mitigation:**

- **Risk:** Re-keying production data changes downstream ordering guarantees per original key; consumers that group state by key must tolerate the migration window or accept the new key. A custom partitioner that adds randomness preserves throughput but eliminates ordering by the original key.
- **Command:**

  ```bash
  # Quantify the skew before changing the producer
  TOPIC=my-topic
  for P in $(seq 0 $(( $(kafka-topics.sh --bootstrap-server $BS --describe --topic $TOPIC \
      | awk 'NR==1 {for(i=1;i<=NF;i++) if($i=="PartitionCount:") print $(i+1)}') - 1 ))); do
    OFF=$(kafka-run-class.sh kafka.tools.GetOffsetShell --bootstrap-server $BS \
      --topic $TOPIC --partition $P --time -1 | awk -F: '{print $3}')
    echo "partition=$P log_end=$OFF"
  done | sort -t= -k3 -n
  ```

- **Duration:** Diagnostic only; the durable change is in Resolution.

**Resolution:**

```bash
# Producer-side: switch to a partitioning strategy that spreads hot keys.
# Option A — append a random suffix to hot keys before producing:
#   record_key = original_key + "#" + random(0..N-1)
#   (consumer must merge by original_key downstream)
# Option B — use the built-in UniformStickyPartitioner for keyless / low-cardinality keys:
#   producer.properties: partitioner.class=org.apache.kafka.clients.producer.RoundRobinPartitioner
# Option C — increase partition count and use a custom partitioner that buckets hot keys
#   across multiple partitions while keeping cold keys stable.
#
# After deploying the producer change, drain the existing hot partitions:
kafka-consumer-groups.sh --bootstrap-server $BS --describe --group $GROUP
# Wait until lag on the hot partitions returns to 0 — no consumer-side change required.
```

**Impact:** Producer-side change; affects every producer of the topic. Ordering by original key is no longer guaranteed across partitions — downstream stateful processing (joins, aggregations keyed on the original key) must be reviewed before deploying.

**Rollback:** Revert the producer's `partitioner.class` (or key-suffixing logic) and redeploy the producer.

**Verification:** Step 1's per-partition lag distribution after a full retention window shows a flat profile (max within 2x of median); Step 5's production rate is unchanged.

### Cause G: Producer throughput spike — burst exceeds steady-state consumer capacity

**Statement:** Producer message rate has spiked well above the historical baseline, and the consumer group — sized for steady-state — cannot keep up with the burst even though it is healthy.

**Mechanism:** Consumer capacity is provisioned against a steady-state ingestion rate. A traffic burst (marketing event, retry storm from a failing downstream system, batch backfill, customer migration) pushes `MessagesInPerSec` above the consumer group's sustained drain rate. Lag accumulates at the difference for the duration of the burst, then drains once production returns to baseline. The consumer-side rebalance/processing/configuration signals all look healthy — the problem is upstream.

**Indicator:**

- [Step 5] `messages_in_per_sec` is materially higher (e.g., >2x) than the recorded baseline
<!-- match: {"step": 5, "predicate": "threshold", "target": "messages_in_per_sec_ratio_vs_baseline", "op": ">", "value": 2.0} -->
- [Step 2] group state is `Stable` and rebalance rate is near zero
- [Step 3] `poll_idle_ratio_avg` is near 0 (consumers are saturated processing, not idle waiting)

**Mitigation:**

- **Risk:** Temporarily over-scaling consumers spends budget on capacity that is unneeded once the burst subsides; pair with a cool-down period before scaling back down.
- **Command:**

  ```bash
  # Burst-scale up to the partition count (more replicas than partitions is wasted)
  PARTITIONS=$(kafka-topics.sh --bootstrap-server $BS --describe --topic $TOPIC \
    | awk 'NR==1 {for(i=1;i<=NF;i++) if($i=="PartitionCount:") print $(i+1)}')
  kubectl scale deployment/<app> --replicas=$PARTITIONS
  ```

- **Duration:** Until burst subsides plus a 30-minute cool-down to confirm lag has drained.

**Resolution:**

```bash
# Producer-side rate-limit (controllable bursts):
#   producer.properties: linger.ms=20  batch.size=131072  compression.type=lz4
#
# Or use a Kafka quota on the producer client.id:
kafka-configs.sh --bootstrap-server $BS --alter \
  --add-config 'producer_byte_rate=10485760' \
  --entity-type clients --entity-name <producer-client-id>
#
# Consumer-side autoscaling (KEDA Kafka scaler on lag):
#   trigger: type=kafka  lagThreshold=10000  consumerGroup=<group>  topic=<topic>
```

**Impact:** Producer quotas throttle producers cluster-wide for the matched `client-id`; the producer will see `ProduceResponse` throttle-time and may apply back-pressure. KEDA-based autoscaling on lag is the production pattern for handling expected bursts without manual intervention.

**Rollback:** Remove the quota (`kafka-configs.sh ... --delete-config 'producer_byte_rate'`) or scale consumers back to baseline.

**Verification:** Step 1's `total_lag` falls to baseline within the expected drain time (`total_lag / (consume_rate - new_steady_rate)`); subsequent bursts of similar size do not produce lag because autoscaling provisioned capacity in advance.

### Cause H: Fetch sizing starves the consumer of records per round-trip

**Statement:** `fetch.min.bytes` is set too high or `max.partition.fetch.bytes` is set too low, so each `poll()` round-trip returns far fewer records than the consumer could process, and per-record overhead dominates throughput.

**Mechanism:** A high `fetch.min.bytes` makes the broker wait until that many bytes are available (or `fetch.max.wait.ms` elapses) before responding, inflating idle time on every fetch when the topic does not produce that much data per `fetch.max.wait.ms` window. A low `max.partition.fetch.bytes` caps per-partition response payload — if individual records are large, the broker returns only one or two records per fetch even when more are available. Either way the consumer spends most of its wall clock on the fetch round-trip rather than processing, and steady-state throughput sits below what the application can sustain.

**Indicator:**

- [Step 3] `kafka_consumer_fetch_manager_metrics_fetch_rate` is unusually high relative to `records_consumed_rate` (low records-per-fetch ratio)
- [Step 3] `kafka_consumer_fetch_manager_metrics_poll_idle_ratio_avg` is high (>0.5) yet lag is growing — consumer waits in `poll()` for records that the broker is withholding
- [Step 6] `fetch.min.bytes` > 1, or `max.partition.fetch.bytes` < `1048576` (the documented default)

**Mitigation:**

- **Risk:** Raising `max.partition.fetch.bytes` increases consumer-side memory footprint per partition and per fetch; multiply by partition count to size heap headroom. Lowering `fetch.min.bytes` reduces broker-side batching and slightly increases broker CPU per fetch.
- **Command:**

  ```bash
  # Restore defaults and verify
  kubectl set env deployment/<app> \
    KAFKA_CONSUMER_FETCH_MIN_BYTES=1 \
    KAFKA_CONSUMER_FETCH_MAX_WAIT_MS=500 \
    KAFKA_CONSUMER_MAX_PARTITION_FETCH_BYTES=1048576
  kubectl rollout restart deployment/<app>
  ```

- **Duration:** Permanent unless the workload is known to be latency-insensitive and benefits from larger broker-side batches.

**Resolution:** Same as Mitigation.

**Impact:** Cluster-wide for the consumer deployment. Default fetch sizing is the documented baseline; deviations should be backed by a measured workload reason.

**Rollback:** Revert the env vars to the previous values and `kubectl rollout restart deployment/<app>`.

**Verification:** Step 3 shows `records_consumed_rate` rising materially per consumer at the same `fetch_rate`; Step 1's `total_lag` decreases at the new throughput.

### Cause I: GC pauses on the consumer JVM exceed `max.poll.interval.ms`

**Statement:** Long stop-the-world garbage-collection pauses on the consumer JVM block the poll thread for longer than `max.poll.interval.ms`, causing the coordinator to evict the member and triggering rebalances.

**Mechanism:** A full GC (`FGC` in `jstat`) freezes every Java thread including the Kafka client's poll thread. If the pause is long enough, the consumer fails to call `poll()` (and to send heartbeats while inside `poll()`) before `max.poll.interval.ms` and `session.timeout.ms` expire. The coordinator removes the member, partitions are reassigned, and on the next poll the evicted member sees `CommitFailedException`. Heap pressure typically comes from buffering large batches in memory (`max.poll.records` * record size) or from application-side caches that grow without bound.

**Indicator:**

- [Step 7] `jstat -gcutil` shows `O > 90` (old generation >90% full) and `FGC` increments during the affected window
- [Step 7] `jstack` shows the consumer poll thread parked or `RUNNABLE` inside `Consumer.poll()`/`Fetcher.fetchedRecords()` while application threads pile up waiting for memory
- [Step 2] consumer log entries `consumer poll timeout has expired` are preceded by GC log lines `Pause Full` lasting hundreds of ms to seconds

**Mitigation:**

- **Risk:** Reducing `max.poll.records` lowers the per-batch heap footprint but raises the fetch round-trip rate. Switching collectors (G1 → ZGC / Shenandoah) changes pause characteristics but requires JDK 17+ and a tuning pass.
- **Command:**

  ```bash
  # Immediate: take a heap snapshot for offline analysis, then halve the batch size
  jmap -dump:live,format=b,file=/tmp/consumer.hprof $(pgrep -of <app>)
  kubectl set env deployment/<app> KAFKA_CONSUMER_MAX_POLL_RECORDS=100
  kubectl rollout restart deployment/<app>
  ```

- **Duration:** Until the durable GC fix lands; this caps heap growth per poll() but does not address the root cause.

**Resolution:**

```bash
# JVM-side: switch to a low-pause collector and right-size the heap.
# Example for JDK 17+ with ZGC:
JAVA_TOOL_OPTIONS="-Xms4g -Xmx4g -XX:+UseZGC -XX:+ZGenerational \
  -Xlog:gc*,gc+age=trace,safepoint:file=/var/log/<app>/gc.log:time,uptime,level,tags"
kubectl set env deployment/<app> JAVA_TOOL_OPTIONS="$JAVA_TOOL_OPTIONS"
kubectl rollout restart deployment/<app>
# Verify
kubectl exec -it deployment/<app> -- jcmd 1 GC.heap_info
kubectl exec -it deployment/<app> -- grep -E "Pause" /var/log/<app>/gc.log | tail -20
```

**Impact:** Cluster-wide for the consumer deployment. ZGC has higher CPU and memory overhead than G1 but pause times are sub-millisecond regardless of heap size. Validate the JDK version supports the chosen collector before rollout.

**Rollback:** Restore the previous `JAVA_TOOL_OPTIONS` (or remove the env var entirely to use the JDK default collector) and `kubectl rollout restart deployment/<app>`.

**Verification:** Step 7's `jstat -gcutil` shows `FGC` count stable over a 30-minute window; consumer logs contain no `consumer poll timeout has expired` for the same window; Step 1 shows lag draining.

### Cause Z: Unidentified

**Statement:** Step 1 confirmed lag is growing for the affected consumer group but the indicators for Causes A through I did not match the gathered evidence.

**Mechanism:** Lag is real (Step 1's `total_lag` and `max_partition_lag` are non-zero and trending up) but the per-step decomposition did not localise the bottleneck to poll-interval timeouts, capacity, partition assignment, rebalance protocol, static membership, hot keys, producer burst, fetch sizing, or GC. Less common origins include broker-side issues (under-replicated partitions, ISR shrinkage, broker GC) that affect fetch latency, network packet loss between consumer and broker, SASL/SSL handshake regressions, kernel-level scheduling on the consumer host, or a bug in a specific client version.

**Indicator:**

- [Default] Steps 1-7 confirmed lag exists and the group is the bottleneck, but Causes A-I indicators did not match the gathered evidence

**Mitigation:**

- **Risk:** Resetting consumer offsets to skip the backlog (`--reset-offsets --to-latest`) is destructive — affected messages are not consumed and any business logic that depended on them is silently bypassed. Only acceptable when the data is reproducible upstream or explicitly disposable.
- **Command:**

  ```bash
  # Capture a snapshot before escalation
  kafka-consumer-groups.sh --bootstrap-server $BS --describe --group $GROUP --verbose > /tmp/lag-$(date +%s).describe
  kafka-consumer-groups.sh --bootstrap-server $BS --describe --group $GROUP --state > /tmp/lag-$(date +%s).state
  kafka-consumer-groups.sh --bootstrap-server $BS --describe --group $GROUP --members --verbose > /tmp/lag-$(date +%s).members
  kafka-topics.sh --bootstrap-server $BS --describe --topic $TOPIC > /tmp/lag-$(date +%s).topic
  curl -s http://<jmx-exporter>:9404/metrics | grep '^kafka_consumer_' > /tmp/lag-$(date +%s).consumer-metrics
  curl -s http://<broker-jmx-exporter>:9404/metrics | grep -E 'kafka_server_replicamanager_(under_replicated|under_min_isr)|kafka_controller' > /tmp/lag-$(date +%s).broker-metrics
  tar czf /tmp/kafka-lag-bundle-$(date +%s).tgz /tmp/lag-*.{describe,state,members,topic,consumer-metrics,broker-metrics}
  ```

- **Duration:** Minutes. Collect, hand off, then escalate.

**Resolution:** Out of runbook scope. Attach the `kafka-lag-bundle-*.tgz` collected above, the consumer's recent application log, GC log, and the broker controller log to an incident ticket; escalate to the Kafka platform on-call with the affected `group.id`, `topic`, lag trajectory, and the time window of the regression.

**Verification:** Hand-off acknowledged by the receiving engineer; an incident ticket is opened with the captured artefacts attached and a follow-up owner assigned.

## Prevention

- Alert on `kafka_consumer_fetch_manager_metrics_records_lag_max` exceeding a per-topic budget (records, or wall-clock if produce rate is known) sustained for 5 minutes; page on growth, warn on absolute value.
- Track and alert on `kafka_consumer_coordinator_metrics_rebalance_rate_per_hour > 1` — a stable consumer group should not rebalance once per hour under normal operation.
- Set `partition.assignment.strategy=org.apache.kafka.clients.consumer.CooperativeStickyAssignor` on every new consumer at first deployment to avoid the two-bounce migration later.
- Set `group.instance.id` to a stable per-replica string (StatefulSet ordinal, Deployment pod name with `fieldRef`) and tune `session.timeout.ms` to tolerate the longest expected restart window without overshooting failure-detection latency (defaults: `session.timeout.ms=45000`, broker max `1800000`).
- Decouple `poll()` from message processing using a bounded worker pool; the poll thread should only enqueue and commit, never block on application I/O.
- Capacity-plan partition count and consumer replicas together. Always provision partitions ≥ peak parallelism target; remember that partition count cannot be decreased once raised.
- Page on `kafka.server:type=BrokerTopicMetrics,name=MessagesInPerSec,topic=*` exceeding 2x baseline for 5 minutes — burst detection lets autoscalers respond before lag accumulates.
- Configure KEDA (or equivalent) consumer autoscaling on consumer-group lag for production deployments; pin minimum replicas at baseline + 20% headroom, maximum at partition count.
- Leave `fetch.min.bytes=1`, `fetch.max.wait.ms=500`, and `max.partition.fetch.bytes=1048576` at defaults unless a measured workload reason justifies a change.
- Run consumers on a low-pause GC (G1 with `MaxGCPauseMillis=200`, or ZGC on JDK 17+) and ship GC logs to a centralised aggregator so pause regressions are visible alongside lag.
- Track partition-level lag distribution (not just `records-lag-max`) — uneven distribution surfaces hot-key skew before it becomes an incident.

## Sources

- [Confluent Platform — Consumer Configuration Reference](https://docs.confluent.io/platform/current/installation/configuration/consumer-configs.html) — Priority 1. Authoritative defaults for `max.poll.records=500`, `max.poll.interval.ms=300000`, `session.timeout.ms=45000`, `heartbeat.interval.ms=3000`, `fetch.min.bytes=1`, `max.partition.fetch.bytes=1048576`, `request.timeout.ms=30000`, `connections.max.idle.ms=540000`, `partition.assignment.strategy=[RangeAssignor, CooperativeStickyAssignor]`, `group.instance.id=null`, `enable.auto.commit=true`, `auto.offset.reset=latest`.
- [Confluent Platform — Kafka Consumer Client Guide](https://docs.confluent.io/platform/current/clients/consumer.html) — Priority 1. Poll-loop semantics, behaviour on `max.poll.interval.ms` exceedance, group coordinator removal mechanics, eager vs cooperative rebalance protocols, `records-lag-max` / `records-consumed-rate` / `poll-idle-ratio-avg` / `fetch-rate` metric definitions, `kafka-consumer-groups` CLI examples (`--list`, `--describe`, `--reset-offsets --shift-by`).
- [Confluent Platform — Scaling Kafka Consumer Groups](https://docs.confluent.io/platform/current/clients/consumer.html#scaling-consumer-groups) — Priority 1. Partition-to-consumer one-to-one rule within a group, idle-member behaviour when consumers exceed partitions, `CooperativeStickyAssignor` recommendation for minimising disruption, partition-ordering implications when altering partition count.
- [Apache Kafka — KIP-429: Consumer Incremental Rebalance Protocol](https://cwiki.apache.org/confluence/display/KAFKA/KIP-429%3A+Kafka+Consumer+Incremental+Rebalance+Protocol) — Priority 1. The eager-protocol "stop-the-world" problem, `CooperativeStickyAssignor` design (`ownedPartitions()` augmentation), two-bounce rolling-upgrade migration path from eager to cooperative assignors.
- [Apache Kafka — KIP-345: Static Membership Protocol](https://cwiki.apache.org/confluence/display/KAFKA/KIP-345%3A+Introduce+static+membership+protocol+to+reduce+consumer+rebalances) — Priority 1. `group.instance.id` semantics for static membership, interaction with `session.timeout.ms` as the sole liveness signal under static membership, broker-side `session.timeout.ms` cap raised to 1800000 (30 minutes), rationale for using static membership with stateful applications and MirrorMaker.
