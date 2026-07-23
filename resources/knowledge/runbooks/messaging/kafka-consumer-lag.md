---
id: kafka-consumer-lag
title: "Kafka Consumer Lag"
domain: messaging
service: kafka
symptom_class:
  - latency
  - throughput_degradation
severity: high
scope: global
version: "2.0.0"
last_updated: "2026-06-25"
verified_by: "kb-researcher"
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

### Cause A: Per-batch processing exceeds `max.poll.interval.ms`

**Statement:** The consumer's processing loop occasionally takes longer than `max.poll.interval.ms` to finish a batch and call `poll()` again, so the coordinator removes it from the group and triggers a rebalance.

**Chain:**
- root: a slow downstream call, GC pause, or oversized batch pushes one batch's processing time past `max.poll.interval.ms` (default 300000 ms) before `poll()` is called again.
- s1: the group coordinator marks the member dead and reassigns its partitions; the evicted member's next `commitSync`/`commitAsync` fails with `CommitFailedException`.
- s2: while the rebalance runs no member is processing, so lag grows on every partition until the evicted member rejoins.
- D: LAG rises across the group and the consumer logs poll-timeout / rebalance churn (Symptom Recognition).

**Indicators:**
- root: [Step 2] consumer log contains the poll-timeout message.
- s1: [Step 2] consumer log contains `CommitFailedException`.
- s2: [Step 3] `kafka_consumer_coordinator_metrics_rebalance_rate_per_hour > 1` over the affected window.

**Interventions:**
- **mitigation** (root): lower `max.poll.records` and raise `max.poll.interval.ms` so a batch fits inside the limit.

  ```bash
  # Edit application config — example for a Spring/Java client
  # max.poll.records=100
  # max.poll.interval.ms=600000
  kubectl set env deployment/<app> \
    KAFKA_CONSUMER_MAX_POLL_RECORDS=100 \
    KAFKA_CONSUMER_MAX_POLL_INTERVAL_MS=600000
  kubectl rollout restart deployment/<app>
  ```

  **Risk:** Lowering `max.poll.records` increases the number of `poll()` round-trips per second — verify broker fetch capacity. Raising `max.poll.interval.ms` extends the time before a genuinely stuck consumer is detected as dead, so lag grows longer before recovery. **Duration:** Config-only stop-gap until the durable fix lands. **Verification:** poll-timeout log lines stop and Step 3's `rebalance-rate-per-hour` falls.
- **remediation** (root): hand each `poll()` batch to a bounded worker pool and poll again immediately, so the poll thread keeps heartbeats flowing and respects `max.poll.interval.ms` while workers do the slow I/O.

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

  **Verification:** After 30 minutes at production load, `kafka_consumer_coordinator_metrics_rebalance_rate_per_hour` returns to near zero, `consumer poll timeout has expired` no longer appears in logs, and Step 1's `total_lag` is decreasing. Decoupling poll from processing changes the at-least-once delivery boundary — commit only after the worker has durably handled the records, or use idempotent downstream writes. Rollback: remove the `poll-tuning.properties` drop-in (or revert the env vars) and `kubectl rollout undo deployment/<app>`.

### Cause B: Consumer count is less than partition count — sustained under-capacity

**Statement:** The consumer group has fewer members than the topic has partitions, so the per-member partition load exceeds what a single instance can process at the incoming message rate, and lag grows on every member's partitions.

**Chain:**
- root: with `M` members and `N` partitions where `M < N`, each member owns `ceil(N/M)` partitions (a partition is consumed by at most one member of a group).
- s1: per-member throughput (records/sec) is below the per-member incoming rate, so lag grows linearly on every member's partitions.
- D: LAG rises uniformly across the group while the group state stays healthy (Symptom Recognition).

**Indicators:**
- root: [Step 4] `members < partitions` from the `members=` / `partitions=` lines.
- s1: [Step 4] every consumer in the histogram has `>= ceil(partitions/members)` partitions and lag is rising on the majority; [Step 5] `messages_in_per_sec` is within ±20% of baseline (producer not spiking).

**Interventions:**
- **remediation** (root): scale consumers up to (not above) the partition count so each member owns fewer partitions; adding members beyond `N` leaves the extras idle.

  ```bash
  # Scale up to but not above partition count
  PARTITIONS=$(kafka-topics.sh --bootstrap-server $BS --describe --topic $TOPIC \
    | awk 'NR==1 {for(i=1;i<=NF;i++) if($i=="PartitionCount:") print $(i+1)}')
  kubectl scale deployment/<app> --replicas=$PARTITIONS
  kubectl rollout status deployment/<app>
  ```

  **Verification:** Step 4 reports `members == partitions` (or chosen target), Step 1 shows `total_lag` falling within the next consume-rate * time window, and `kafka_consumer_fetch_manager_metrics_records_lag_max` trends toward 0. One rebalance occurs at scale-up — graceful with `CooperativeStickyAssignor` (see Cause D), stop-the-world with `RangeAssignor`; cost increases linearly with replica count. Rollback: `kubectl scale deployment/<app> --replicas=<previous>` (also triggers one rebalance). Re-scale down only after sustained `total_lag = 0` for an hour.

### Cause C: Partition count is the ceiling — topic is under-partitioned

**Statement:** The consumer group already has one member per partition, so adding more consumers cannot help; the topic itself does not provide enough parallelism for the sustained production rate.

**Chain:**
- root: `members == partitions` and Kafka assigns at most one consumer per partition, so the per-partition throughput ceiling has been hit (production rate exceeds what one consumer drains from one partition, or keys concentrate on a subset of partitions).
- s1: adding members beyond `partitions` produces only idle members; the only way to add parallelism is to raise the topic's partition count (irreversible, breaks per-key ordering).
- D: LAG keeps growing despite a balanced, fully-staffed group (Symptom Recognition).

**Indicators:**
- root: [Step 4] `members == partitions` (or `members > partitions` with idle members reporting `#PARTITIONS=0`) and the histogram is flat at 1 per member, yet [Step 1] `total_lag` is still growing.
- s1: [Step 5] `messages_in_per_sec` is at or above baseline and per-member CPU is not saturated (room to consume faster if partitions allowed).

**Interventions:**
- **mitigation** (root): confirm the current partition count before any irreversible alter.

  ```bash
  # Dry-run: confirm current partition count before altering
  kafka-topics.sh --bootstrap-server $BS --describe --topic $TOPIC | head -1
  ```

  **Risk:** Diagnostic only — no change yet; reads the live topic description. **Duration:** Diagnostic only; the durable change is the remediation below. **Verification:** the reported `PartitionCount:` matches Step 4's `partitions=`.
- **remediation** (root): increase the topic partition count and scale consumers to match.

  ```bash
  # Double the partition count; pick a multiple that distributes hot keys evenly.
  NEW=24
  kafka-topics.sh --bootstrap-server $BS --alter --topic $TOPIC --partitions $NEW
  # Scale consumers to match
  kubectl scale deployment/<app> --replicas=$NEW
  ```

  **Verification:** Step 1's `total_lag` falls within minutes after the scale-up; Step 4's `partitions=` and `members=` both equal `NEW`; `kafka-topics.sh --describe --topic $TOPIC` lists `NEW` partitions with leaders and ISRs healthy. Adding partitions changes `hash(key) % partitions` and breaks key-based ordering — confirm downstream consumers tolerate the re-keying and verify broker disk and file-descriptor headroom. Rollback: partition count cannot be decreased; create a new topic with the old count, mirror data in (e.g. MirrorMaker 2), repoint clients, and decommission the over-partitioned topic.

### Cause D: Eager rebalance protocol causes stop-the-world pauses

**Statement:** The consumer group is configured with `RangeAssignor` (or another eager-protocol assignor), so every membership change revokes all partitions from all members before reassignment, halting consumption for the duration of the rebalance.

**Chain:**
- root: the group's `partition.assignment.strategy` is an eager-protocol assignor (`RangeAssignor`/`roundrobin`/`sticky`) rather than `CooperativeStickyAssignor`.
- s1: on every membership change each member must revoke its full assignment, send a JoinGroup, and wait for the leader's SyncGroup before resuming.
- s2: while that rebalance runs no member consumes any partition, so lag grows uniformly for its duration (seconds, up to minutes under load).
- D: LAG spikes across all partitions on every scale event (Symptom Recognition).

**Indicators:**
- root: [Step 2] `--state` reports `ASSIGNMENT-STRATEGY: range` (or `roundrobin`, `sticky`) rather than `cooperative-sticky`.
- root: [Step 6] `partition.assignment.strategy` does not contain `CooperativeStickyAssignor`.
- s1: [Step 2] consumer logs show `Revoking previously assigned partitions` followed by `(Re-)joining group` on every membership change.

**Interventions:**
- **mitigation** (root): first bounce — ADD `CooperativeStickyAssignor` alongside the existing eager assignor (a single-bounce swap is rejected by the broker and leaves the group stuck in `PreparingRebalance`).

  ```bash
  # First bounce: ADD CooperativeStickyAssignor alongside the existing one.
  # partition.assignment.strategy=org.apache.kafka.clients.consumer.CooperativeStickyAssignor,\
  #                              org.apache.kafka.clients.consumer.RangeAssignor
  kubectl set env deployment/<app> \
    KAFKA_CONSUMER_PARTITION_ASSIGNMENT_STRATEGY="org.apache.kafka.clients.consumer.CooperativeStickyAssignor,org.apache.kafka.clients.consumer.RangeAssignor"
  kubectl rollout restart deployment/<app>
  ```

  **Risk:** A single-bounce swap from eager to cooperative is rejected by the broker and the group will be stuck in `PreparingRebalance`; follow the documented two-step procedure exactly. **Duration:** Hold here until every replica has restarted and the group state is `Stable`, then proceed to the second bounce. **Verification:** every replica has restarted and `--state` reports `Stable`.
- **remediation** (root): second bounce — REMOVE the eager assignor so the group runs incremental cooperative rebalancing on all future membership changes.

  ```bash
  # Second bounce: REMOVE the eager assignor. Only run after every replica has
  # completed the first bounce and the group is Stable.
  kubectl set env deployment/<app> \
    KAFKA_CONSUMER_PARTITION_ASSIGNMENT_STRATEGY="org.apache.kafka.clients.consumer.CooperativeStickyAssignor"
  kubectl rollout restart deployment/<app>
  # Verify the switchover
  kafka-consumer-groups.sh --bootstrap-server $BS --describe --group $GROUP --state
  ```

  **Verification:** `kafka-consumer-groups.sh ... --state` reports `ASSIGNMENT-STRATEGY: cooperative-sticky`; subsequent scale events show `Revoking previously assigned partitions` only for the partitions actually being moved, and Step 1's `total_lag` does not spike during a scale-up. Rollback: reverse the env var on the affected revision (`kubectl rollout undo deployment/<app>` twice) — the same two-bounce constraint applies in reverse.

### Cause E: Transient consumer restarts trigger rebalances — static membership not configured

**Statement:** Each consumer restart — pod rescheduling, rolling deploy, brief network blip — is treated as a permanent departure because `group.instance.id` is unset, so the coordinator runs a full rebalance instead of waiting for the member to return.

**Chain:**
- root: `group.instance.id` is unset, so every consumer is a dynamic member identified only by an ephemeral `member.id` issued at JoinGroup.
- s1: a `LeaveGroup` (graceful shutdown) or `session.timeout.ms` expiry on any restart triggers an immediate full rebalance.
- s2: each rebalance halts consumption while partitions are reassigned, so lag spikes once per restart correlated with deploy/maintenance activity.
- D: LAG and `rebalance-rate-per-hour` spike on a cadence that tracks deployments rather than load (Symptom Recognition).

**Indicators:**
- root: [Step 6] `group.instance.id` is null (no output) for every consumer.
- s1: [Step 2] consumer logs show `LeaveGroup` followed by `JoinGroup` correlated with pod restarts, rollouts, or node maintenance.
- s2: [Step 3] `kafka_consumer_coordinator_metrics_last_rebalance_seconds_ago` is small (recent) with `rebalance-rate-per-hour > 1`, timed to deployment activity rather than load changes.

**Interventions:**
- **remediation** (root): set a stable `group.instance.id` per replica (KIP-345 static membership) and tune `session.timeout.ms` so transient absences within the window do not rebalance.

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

  **Verification:** After a rolling restart of the consumer deployment, Step 3's `rebalance-rate-per-hour` stays at 0 over the rollout window (instead of spiking once per replica), and consumer logs show no `JoinGroup`/`SyncGroup` for transient restarts within `session.timeout.ms`. Static membership defers rebalance for up to `session.timeout.ms` (default 45000 ms; broker max raised to 1800000 ms under KIP-345) — a permanently-gone member leaves its partitions unassigned for that window, so size the timeout to balance restart tolerance against detection latency. Replacing a replica with a new pod name still counts as a new identity and triggers one rebalance. Rollback: remove the `KAFKA_CONSUMER_GROUP_INSTANCE_ID` env var via `kubectl patch` + `kubectl rollout restart` to revert to dynamic membership.

### Cause F: Partition assignment is skewed — hot keys concentrate traffic

**Statement:** Production is partitioned by a key that does not distribute evenly across partitions, so a small number of partitions carry most of the traffic, and the consumers owning those partitions lag while others run idle.

**Chain:**
- root: the producer keys records on a skewed key space (high-traffic tenant IDs, geographic clustering, or near-constant keys), and `partition = hash(key) % partition_count` concentrates records on a few partitions.
- s1: the consumer assigned to a hot partition processes the combined hot-key rate while its peers see LAG flat at 0; adding consumers or partitions does not redistribute existing hot keys (the hash still maps them to the same partition).
- D: LAG is highly uneven — `max_partition_lag` dwarfs the median while other partitions sit at zero (Symptom Recognition).

**Indicators:**
- root: [Symptom] business metrics show a small number of keys (tenant IDs, hash buckets) dominating production rate.
- s1: [Step 1] `max_partition_lag` is orders of magnitude larger than the median; [Step 4] the partition-count histogram is balanced yet [Step 1] LAG is uneven.

**Interventions:**
- **mitigation** (root): quantify the skew across partitions before changing the producer.

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

  **Risk:** Read-only measurement; no production change. **Duration:** Diagnostic only; the durable change is the remediation below. **Verification:** the per-partition `log_end` listing confirms a small set of partitions carries most offsets.
- **remediation** (root): change the producer partitioning so hot keys spread, then drain the hot partitions.

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

  **Verification:** Step 1's per-partition lag distribution after a full retention window shows a flat profile (max within 2x of median); Step 5's production rate is unchanged. This affects every producer of the topic; ordering by original key is no longer guaranteed across partitions — review downstream stateful processing (joins, aggregations keyed on the original key) before deploying. Rollback: revert the producer's `partitioner.class` (or key-suffixing logic) and redeploy the producer.

### Cause G: Producer throughput spike — burst exceeds steady-state consumer capacity

**Statement:** Producer message rate has spiked well above the historical baseline, and the consumer group — sized for steady-state — cannot keep up with the burst even though it is healthy.

**Chain:**
- root: a traffic burst (marketing event, retry storm from a failing downstream, batch backfill, customer migration) pushes `MessagesInPerSec` above the consumer group's sustained drain rate.
- s1: lag accumulates at the difference between produce and drain rate for the duration of the burst, then drains once production returns to baseline.
- D: LAG grows while rebalance/processing/config signals all look healthy — the problem is upstream (Symptom Recognition).

**Indicators:**
- root: [Step 5] `messages_in_per_sec` is materially higher (e.g. >2x) than the recorded baseline.
- s1: [Step 2] group state is `Stable` and rebalance rate is near zero; [Step 3] `poll_idle_ratio_avg` is near 0 (consumers saturated processing, not idle waiting).

**Interventions:**
- **mitigation** (root): burst-scale consumers up to the partition count for the duration of the burst.

  ```bash
  # Burst-scale up to the partition count (more replicas than partitions is wasted)
  PARTITIONS=$(kafka-topics.sh --bootstrap-server $BS --describe --topic $TOPIC \
    | awk 'NR==1 {for(i=1;i<=NF;i++) if($i=="PartitionCount:") print $(i+1)}')
  kubectl scale deployment/<app> --replicas=$PARTITIONS
  ```

  **Risk:** Temporarily over-scaling consumers spends budget on capacity that is unneeded once the burst subsides; pair with a cool-down before scaling back. **Duration:** Until the burst subsides plus a 30-minute cool-down to confirm lag has drained. **Verification:** Step 1's `total_lag` drains toward baseline as replicas come up.
- **remediation** (root): rate-limit the producer (or apply a broker quota) and autoscale consumers on lag so future bursts are absorbed automatically.

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

  **Verification:** Step 1's `total_lag` falls to baseline within the expected drain time (`total_lag / (consume_rate - new_steady_rate)`); subsequent bursts of similar size do not produce lag because autoscaling provisioned capacity in advance. Producer quotas throttle producers cluster-wide for the matched `client-id` (they will see `ProduceResponse` throttle-time and may back-pressure). Rollback: remove the quota (`kafka-configs.sh ... --delete-config 'producer_byte_rate'`) or scale consumers back to baseline.

### Cause H: Fetch sizing starves the consumer of records per round-trip

**Statement:** `fetch.min.bytes` is set too high or `max.partition.fetch.bytes` is set too low, so each `poll()` round-trip returns far fewer records than the consumer could process, and per-record overhead dominates throughput.

**Chain:**
- root: `fetch.min.bytes` is above 1 (broker waits for that many bytes or `fetch.max.wait.ms`), or `max.partition.fetch.bytes` is below the 1048576 default (per-partition payload capped), so each fetch returns too few records.
- s1: the consumer spends most of its wall clock on the fetch round-trip rather than processing, and steady-state throughput sits below what the application can sustain.
- D: LAG grows even though the consumer waits idle in `poll()` for records the broker is withholding (Symptom Recognition).

**Indicators:**
- root: [Step 6] `fetch.min.bytes` > 1, or `max.partition.fetch.bytes` < `1048576` (the documented default).
- s1: [Step 3] `fetch_rate` is unusually high relative to `records_consumed_rate` (low records-per-fetch), and `poll_idle_ratio_avg` is high (>0.5) yet lag is growing.

**Interventions:**
- **remediation** (root): restore the default fetch sizing so each fetch returns a full batch.

  ```bash
  # Restore defaults and verify
  kubectl set env deployment/<app> \
    KAFKA_CONSUMER_FETCH_MIN_BYTES=1 \
    KAFKA_CONSUMER_FETCH_MAX_WAIT_MS=500 \
    KAFKA_CONSUMER_MAX_PARTITION_FETCH_BYTES=1048576
  kubectl rollout restart deployment/<app>
  ```

  **Verification:** Step 3 shows `records_consumed_rate` rising materially per consumer at the same `fetch_rate`; Step 1's `total_lag` decreases at the new throughput. Raising `max.partition.fetch.bytes` increases consumer-side memory per partition per fetch (multiply by partition count to size heap headroom); lowering `fetch.min.bytes` slightly increases broker CPU per fetch. Default fetch sizing is the documented baseline — deviations should be backed by a measured workload reason. Rollback: revert the env vars to the previous values and `kubectl rollout restart deployment/<app>`.

### Cause I: GC pauses on the consumer JVM exceed `max.poll.interval.ms`

**Statement:** Long stop-the-world garbage-collection pauses on the consumer JVM block the poll thread for longer than `max.poll.interval.ms`, causing the coordinator to evict the member and triggering rebalances.

**Chain:**
- root: heap pressure (buffering large batches, `max.poll.records` * record size, or unbounded application caches) drives a full GC (`FGC`) that freezes every Java thread, including the Kafka poll thread.
- s1: the frozen poll thread fails to call `poll()` (and to heartbeat) before `max.poll.interval.ms` / `session.timeout.ms` expire, so the coordinator evicts the member and reassigns its partitions; the next poll sees `CommitFailedException`.
- s2: the rebalance halts consumption while partitions move, so lag grows on every partition until the member rejoins.
- D: LAG rises with poll-timeout / commit-failure log lines preceded by long GC pauses (Symptom Recognition).

**Indicators:**
- root: [Step 7] `jstat -gcutil` shows `O > 90` (old generation >90% full) and `FGC` increments during the affected window.
- s1: [Step 2] `consumer poll timeout has expired` entries are preceded by GC log lines `Pause Full` lasting hundreds of ms to seconds; [Step 7] `jstack` shows the poll thread inside `Consumer.poll()`/`Fetcher.fetchedRecords()` while application threads pile up waiting for memory.

**Interventions:**
- **mitigation** (root): take a heap snapshot for offline analysis and halve the batch size to cap heap growth per `poll()`.

  ```bash
  # Immediate: take a heap snapshot for offline analysis, then halve the batch size
  jmap -dump:live,format=b,file=/tmp/consumer.hprof $(pgrep -of <app>)
  kubectl set env deployment/<app> KAFKA_CONSUMER_MAX_POLL_RECORDS=100
  kubectl rollout restart deployment/<app>
  ```

  **Risk:** Reducing `max.poll.records` lowers the per-batch heap footprint but raises the fetch round-trip rate. **Duration:** Until the durable GC fix lands; this caps heap growth per `poll()` but does not address the root cause. **Verification:** Step 7's `FGC` rate drops and poll-timeout log lines stop after restart.
- **remediation** (root): switch to a low-pause collector and right-size the heap.

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

  **Verification:** Step 7's `jstat -gcutil` shows `FGC` count stable over a 30-minute window; consumer logs contain no `consumer poll timeout has expired` for the same window; Step 1 shows lag draining. ZGC has higher CPU and memory overhead than G1 but pause times are sub-millisecond regardless of heap size — validate the JDK version supports the chosen collector (JDK 17+) before rollout. Rollback: restore the previous `JAVA_TOOL_OPTIONS` (or remove the env var to use the JDK default collector) and `kubectl rollout restart deployment/<app>`.

### Cause Z: Unidentified

**Statement:** Lag is confirmed real and the group is the bottleneck, but the indicators for Causes A through I did not match the gathered evidence (possible broker-side issues, network loss, SASL/SSL handshake regressions, host scheduling, or a client-version bug).

**Indicators:**
- [Default]

**Interventions:**
- **mitigation** (D): capture a full diagnostic snapshot and escalate to the Kafka platform SME.

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

  **Risk:** Resetting consumer offsets to skip the backlog (`--reset-offsets --to-latest`) is destructive — affected messages are not consumed and any business logic depending on them is silently bypassed; only acceptable when the data is reproducible upstream or explicitly disposable. **Duration:** Minutes — collect, hand off, then escalate. **Verification:** an incident ticket is opened with the `kafka-lag-bundle-*.tgz`, consumer log, GC log, and broker controller log attached, and a follow-up owner is assigned by the Kafka platform on-call (given the affected `group.id`, `topic`, lag trajectory, and regression window).

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
