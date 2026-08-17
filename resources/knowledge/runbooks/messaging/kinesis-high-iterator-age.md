---
id: "kinesis-high-iterator-age"
title: "AWS Kinesis Data Streams High Iterator Age (Consumers Falling Behind)"
domain: messaging
service: aws-kinesis
symptom_class: [latency, throughput_degradation]
severity: high
scope: global
version: "1.0.1"
last_updated: "2026-08-17"
verified_by: "kb-researcher"
status: draft
tags: [iterator-age, getrecords, provisionedthroughputexceeded, hot-shard, resharding]
difficulty: advanced
---

## Symptom Recognition

- CloudWatch metric `GetRecords.IteratorAgeMilliseconds` is rising or sustained high (consumer reading old records, not the tip of the stream).
- KCL/Enhanced Fan-Out consumers report rising `MillisBehindLatest`.
- Iterator age approaching 50% of the stream retention period (default 24h / 86400000 ms) — imminent data loss as records expire before they are read.
- `ReadProvisionedThroughputExceeded` > 0 (per-shard read limit hit) and/or `ProvisionedThroughputExceededException` (HTTP 400) returned from `GetRecords`.
- `WriteProvisionedThroughputExceeded` > 0 on specific shards while others are idle (partition-key hot-spotting).
- Consumer `processRecords` latency elevated; throughput per `GetRecords` call below the 2 MB/s per-shard ceiling.

## Applicability

- Service: Amazon Kinesis Data Streams (provisioned mode; on-demand auto-scales but can still hot-spot per shard).
- Tools: AWS CLI v2 (`aws kinesis`, `aws cloudwatch`), credentials with `kinesis:DescribeStreamSummary`, `kinesis:ListShards`, `kinesis:GetShardIterator`, `kinesis:GetRecords`, `kinesis:UpdateShardCount`, `kinesis:SplitShard`, `kinesis:MergeShards`, `kinesis:EnableEnhancedMonitoring`, `cloudwatch:GetMetricStatistics`, `cloudwatch:PutMetricAlarm`.
- Per-shard hard limits: writes 1 MB/s and 1,000 records/s; classic reads 2 MB/s and 5 `GetRecords` transactions/s.
- Replace `STREAM` with your stream name and `REGION` with your AWS region throughout.

## Diagnostic Steps

### Step 1: Confirm iterator age and read position across the stream

```bash
aws cloudwatch get-metric-statistics \
  --namespace AWS/Kinesis \
  --metric-name GetRecords.IteratorAgeMilliseconds \
  --dimensions Name=StreamName,Value=STREAM \
  --start-time "$(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%S)" \
  --end-time "$(date -u +%Y-%m-%dT%H:%M:%S)" \
  --period 60 --statistics Maximum Average --region REGION
```

Expected output: `Datapoints` with `Maximum`/`Average` in milliseconds. A healthy consumer is near 0; rising values or a Maximum approaching 43200000 (half of 24h retention) indicate falling behind with data-loss risk.

### Step 2: Read stream summary, shard count, and retention

```bash
aws kinesis describe-stream-summary --stream-name STREAM --region REGION \
  --query 'StreamDescriptionSummary.{Status:StreamStatus,OpenShards:OpenShardCount,RetentionHours:RetentionPeriodHours,Mode:StreamModeDetails.StreamMode}'
```

Expected output: `Status: ACTIVE`, the current `OpenShards` count, `RetentionHours` (24 by default), and stream `Mode`. Use shard count as the denominator for throughput-per-shard math in later steps.

### Step 3: Enable shard-level enhanced monitoring to localize the bottleneck

```bash
aws kinesis enable-enhanced-monitoring --stream-name STREAM --region REGION \
  --shard-level-metrics IncomingBytes IncomingRecords OutgoingBytes \
                        WriteProvisionedThroughputExceeded \
                        ReadProvisionedThroughputExceeded IteratorAgeMilliseconds
```

Expected output: `CurrentShardLevelMetrics` (empty/prior) and `DesiredShardLevelMetrics` listing the six metrics. Shard-level metrics publish to CloudWatch every minute (note: enhanced metrics incur an additional charge).

### Step 4: Check per-shard throttling (read and write throughput exceeded)

```bash
for M in WriteProvisionedThroughputExceeded ReadProvisionedThroughputExceeded; do
  echo "== $M =="
  aws cloudwatch get-metric-statistics --namespace AWS/Kinesis --metric-name "$M" \
    --dimensions Name=StreamName,Value=STREAM \
    --start-time "$(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%S)" \
    --end-time "$(date -u +%Y-%m-%dT%H:%M:%S)" \
    --period 60 --statistics Sum --region REGION \
    --query 'Datapoints[?Sum>`0`].[Timestamp,Sum]' --output text
done
```

Expected output: empty output means no throttling. Non-zero `Sum` rows on `ReadProvisionedThroughputExceeded` mean the consumer side is throttled; non-zero on `WriteProvisionedThroughputExceeded` means producers are saturating shard write capacity.

### Step 5: Detect shard hot-spotting (uneven per-shard ingest)

```bash
aws kinesis list-shards --stream-name STREAM --region REGION \
  --query 'Shards[?!not_null(SequenceNumberRange.EndingSequenceNumber)].ShardId' --output text | tr '\t' '\n' | while read S; do
  BYTES=$(aws cloudwatch get-metric-statistics --namespace AWS/Kinesis \
    --metric-name IncomingBytes \
    --dimensions Name=StreamName,Value=STREAM Name=ShardId,Value="$S" \
    --start-time "$(date -u -d '10 min ago' +%Y-%m-%dT%H:%M:%S)" \
    --end-time "$(date -u +%Y-%m-%dT%H:%M:%S)" \
    --period 60 --statistics Sum --region REGION \
    --query 'sum(Datapoints[].Sum)' --output text)
  echo "$S incoming_bytes_10m=$BYTES"
done
```

Expected output: one line per open shard. Roughly equal `incoming_bytes_10m` is healthy; one or a few shards carrying most of the volume (near 1 MB/s = ~600 MB/10 min) while others are near 0 confirms partition-key hot-spotting.

### Step 6: Isolate slow consumer processing (drain rate vs. ceiling)

```bash
SHARD=$(aws kinesis list-shards --stream-name STREAM --region REGION \
  --query 'Shards[0].ShardId' --output text)
ITER=$(aws kinesis get-shard-iterator --stream-name STREAM --shard-id "$SHARD" \
  --shard-iterator-type LATEST --region REGION --query 'ShardIterator' --output text)
aws kinesis get-records --shard-iterator "$ITER" --limit 1000 --region REGION \
  --query '{Returned:length(Records),MillisBehindLatest:MillisBehindLatest}'
```

Expected output: `Returned` record count and `MillisBehindLatest`. A high `MillisBehindLatest` here (raw read keeps up) while your application lags points to slow `processRecords` logic, not Kinesis throughput. Compare against an empty/no-op record processor to confirm.

## Causes

### Cause A: Aggregate consumer read demand exceeds per-shard read limits
**Statement:** Total reads across all consumer applications on the stream exceed the per-shard ceiling of 2 MB/s and 5 GetRecords transactions/s, so reads are throttled and the consumer cannot drain shards fast enough to keep iterator age low.
**Chain:**
- root: combined consumer read rate exceeds 2 MB/s / 5 tps per open shard
- s1: GetRecords calls throttled with ProvisionedThroughputExceededException
- s2: effective drain rate falls below the incoming record rate, backlog accumulates
- D: GetRecords.IteratorAgeMilliseconds rises (consumers fall behind)
**Indicators:**
- root: [Step 4] `ReadProvisionedThroughputExceeded` has non-zero `Sum` datapoints
- s1: [Step 6] `MillisBehindLatest` non-zero / throttling errors on raw `GetRecords`
- D: [Symptom] `GetRecords.IteratorAgeMilliseconds` trending upward in Step 1
**Interventions:**
- **remediation** (root): increase shard count so total read demand fits within per-shard limits.

  ```bash
  aws kinesis update-shard-count --stream-name STREAM \
    --target-shard-count 8 --scaling-type UNIFORM_SCALING --region REGION
  ```
  **Verification:** re-run Step 4 (no `ReadProvisionedThroughputExceeded` datapoints) and Step 1 (`GetRecords.IteratorAgeMilliseconds` declining toward 0).
- **mitigation** (s1): increase retention so the backlog is not lost while you scale.

  ```bash
  aws kinesis increase-stream-retention-period \
    --stream-name STREAM --retention-period-hours 168 --region REGION
  ```
  **Risk:** higher storage cost; does not improve drain rate, only buys time. **Duration:** until iterator age stops rising and falls back below 50% of retention. **Verification:** Step 2 shows `RetentionHours: 168`; Step 1 Maximum stays well below 50% of the new retention.

### Cause B: Producer partition keys hot-spot a subset of shards
**Statement:** Producers use partition keys with skewed distribution so records concentrate on a few shards, saturating those shards' 1 MB/s / 1,000 records/s write limit while others sit idle, and consumers of the hot shards fall behind.
**Chain:**
- root: skewed partition-key distribution concentrates writes on a few shards
- s1: hot shards hit the 1 MB/s / 1,000 rec/s write ceiling (WriteProvisionedThroughputExceeded)
- s2: backlog builds only on the hot shards; consumers of those shards cannot keep up
- D: GetRecords.IteratorAgeMilliseconds rises (driven by the hot shards)
**Indicators:**
- root: [Step 5] one or few shards carry most `IncomingBytes` while others are near 0
- s1: [Step 4] `WriteProvisionedThroughputExceeded` has non-zero `Sum` datapoints
- D: [Symptom] iterator age elevated on the hot shards
**Interventions:**
- **remediation** (root): change the producer to use a higher-cardinality, evenly distributed partition key (e.g. a random/UUID or hashed key) so writes spread across shards. (Producer code change — set `PartitionKey` per `PutRecord`/`PutRecords` to a high-cardinality value.)

  ```bash
  # Verify even spread after the producer change ships:
  aws kinesis put-record --stream-name STREAM \
    --partition-key "$(uuidgen)" --data "$(echo -n 'probe' | base64)" --region REGION
  ```
  **Verification:** re-run Step 5; `incoming_bytes_10m` becomes roughly uniform across shards and Step 4 shows no `WriteProvisionedThroughputExceeded`.
- **mitigation** (s1): split the hottest shard to immediately halve its key-space load while the producer fix ships.

  ```bash
  HOT=$(aws kinesis list-shards --stream-name STREAM --region REGION \
    --query 'Shards[0].ShardId' --output text)
  MID=$(aws kinesis describe-stream --stream-name STREAM --region REGION \
    --query "StreamDescription.Shards[?ShardId=='$HOT'].HashKeyRange.StartingHashKey | [0]" --output text)
  aws kinesis split-shard --stream-name STREAM --shard-to-split "$HOT" \
    --new-starting-hash-key "$MID" --region REGION
  ```
  **Risk:** splitting on a static key range may still leave the hot key on one child shard; raises shard count and cost. **Duration:** until the high-cardinality partition-key fix is deployed. **Verification:** Step 2 `OpenShardCount` increased; Step 5 shows reduced load on the former hot shard.

### Cause C: Slow record-processing logic in the consumer
**Statement:** The consumer's `processRecords` logic is slower than the inbound rate (CPU-intensive, I/O-blocking, or bottlenecked on synchronization), so the application drains each shard below the available 2 MB/s and the read position falls behind even though Kinesis is not throttling.
**Chain:**
- root: processRecords per-batch latency exceeds the inter-arrival time of records
- s1: application consumes records slower than they arrive (no read throttling present)
- s2: unread backlog grows on each shard
- D: GetRecords.IteratorAgeMilliseconds / MillisBehindLatest rises
**Indicators:**
- root: [Step 6] raw `GetRecords` keeps up (low `MillisBehindLatest` on direct read) yet the app lags
- s1: [Step 4] no `ReadProvisionedThroughputExceeded` datapoints (not throttling)
- D: [Symptom] `GetRecords.IteratorAgeMilliseconds` elevated despite headroom
**Interventions:**
- **defensive_fix** (s1): raise consumer parallelism/throughput by removing blocking work from `processRecords` (offload I/O to async workers) and keep KCL `maxRecords` at the system default rather than a low value.

  ```bash
  # Reset to default behavior: remove any low maxRecords override in KCL config, then redeploy.
  grep -rn "maxRecords" ./kcl-config/ || echo "no override — using KCL defaults"
  ```
  **Verification:** re-run Step 6 with the no-op processor baseline; deployed app's `MillisBehindLatest` returns to near baseline and Step 1 iterator age declines.
- **remediation** (root): add shards and run more KCL workers/leases so each worker processes a smaller key range, reducing per-worker `processRecords` pressure.

  ```bash
  aws kinesis update-shard-count --stream-name STREAM \
    --target-shard-count 6 --scaling-type UNIFORM_SCALING --region REGION
  ```
  **Verification:** Step 2 shows higher `OpenShardCount`; Step 1 `GetRecords.IteratorAgeMilliseconds` trends to ~0 as parallelism increases.

### Cause D: maxRecords throttle / low GetRecords batch size caps drain rate
**Statement:** The consumer is configured with a low `maxRecords` per GetRecords call (or an undersized batch limit), so each poll returns too few records to keep up with the inbound rate even though shard read capacity is unused.
**Chain:**
- root: GetRecords batch size (KCL maxRecords) set too low
- s1: each poll returns far fewer than the 10,000-record / 10 MB GetRecords ceiling
- s2: cumulative drain rate stays below the inbound record rate
- D: GetRecords.IteratorAgeMilliseconds rises
**Indicators:**
- root: [Step 6] `Returned` record count is small while `MillisBehindLatest` is high
- s1: [Step 4] no read throttling (capacity is available, just under-used)
- D: [Symptom] iterator age elevated
**Interventions:**
- **defensive_fix** (root): restore KCL `maxRecords` to the system default (AWS recommends defaults) so each poll fetches a full batch.

  ```bash
  grep -rn "maxRecords" ./kcl-config/ \
    && echo "remove the low maxRecords override and redeploy the consumer"
  ```
  **Verification:** re-run Step 6; `Returned` approaches the requested `--limit` (up to 10,000) and Step 1 iterator age declines.

### Cause Z: Unidentified
**Statement:** None of the known roots (read-limit saturation, partition-key hot-spotting, slow processRecords, or maxRecords throttle) match the collected evidence; the elevated iterator age has an undetermined root cause.
**Indicators:**
- [Default]
**Interventions:**
- **mitigation** (D): capture a full diagnostic snapshot and escalate to the Kinesis/data-platform SME.

  ```bash
  TS=$(date -u +%Y%m%dT%H%M%SZ)
  { aws kinesis describe-stream-summary --stream-name STREAM --region REGION;
    aws kinesis list-shards --stream-name STREAM --region REGION;
    for M in GetRecords.IteratorAgeMilliseconds ReadProvisionedThroughputExceeded \
             WriteProvisionedThroughputExceeded IncomingBytes OutgoingBytes; do
      echo "== $M =="; aws cloudwatch get-metric-statistics --namespace AWS/Kinesis \
        --metric-name "$M" --dimensions Name=StreamName,Value=STREAM \
        --start-time "$(date -u -d '3 hours ago' +%Y-%m-%dT%H:%M:%S)" \
        --end-time "$(date -u +%Y-%m-%dT%H:%M:%S)" --period 60 \
        --statistics Sum Maximum Average --region REGION; done; } \
    > "kinesis-${TS}.diag" 2>&1
  echo "Snapshot written to kinesis-${TS}.diag — attach to the escalation ticket."
  ```
  **Risk:** snapshot only; does not change drain rate, so iterator age may keep rising toward data loss. **Duration:** until the SME responds; if iterator age nears 50% of retention, also run Cause A's retention mitigation. **Verification:** `kinesis-${TS}.diag` exists and contains stream summary plus the five metric series.

## Prevention

- Alarm on rising iterator age before data loss (half of a 24h/86400000 ms retention ≈ 43200000 ms; alert earlier):

  ```bash
  aws cloudwatch put-metric-alarm --alarm-name "STREAM-iterator-age-high" \
    --namespace AWS/Kinesis --metric-name GetRecords.IteratorAgeMilliseconds \
    --dimensions Name=StreamName,Value=STREAM --statistic Maximum \
    --period 60 --evaluation-periods 5 --threshold 60000 \
    --comparison-operator GreaterThanThreshold --treat-missing-data notBreaching \
    --region REGION
  ```

- Keep shard-level enhanced monitoring enabled (Step 3) so hot-spotting and per-shard throttling are visible.
- Alarm on `ReadProvisionedThroughputExceeded` and `WriteProvisionedThroughputExceeded` `> 0` to catch saturation early.
- Use high-cardinality, evenly distributed partition keys at the producer to prevent hot shards.
- Provision shards (or use on-demand mode) with headroom over peak `IncomingBytes`/`IncomingRecords`; revisit capacity when throughput trends up.
- Keep KCL `maxRecords` and worker settings at AWS-recommended defaults; size the consumer fleet so each worker leases a manageable number of shards.
- Always have a consumer reading the stream and watch `GetRecords.IteratorAgeMilliseconds` continuously.

## Sources

- [Troubleshooting consumers](https://docs.aws.amazon.com/streams/latest/dev/troubleshooting-consumers.html) — iterator age / MillisBehindLatest, the three documented causes of consumers falling behind (reads exceeding per-shard limits, low maxRecords, slow processRecords), and 50%-of-retention data-loss risk / increase-retention stopgap.
- [Monitoring with cloudwatch](https://docs.aws.amazon.com/streams/latest/dev/monitoring-with-cloudwatch.html) — exact CloudWatch metric names: GetRecords.IteratorAgeMilliseconds, IncomingBytes/Records, OutgoingBytes, Read/WriteProvisionedThroughputExceeded; enhanced (shard-level) monitoring behavior and 1-minute publish cadence.
- [Enable enhanced monitoring](https://docs.aws.amazon.com/cli/latest/reference/kinesis/enable-enhanced-monitoring.html) — `aws kinesis enable-enhanced-monitoring` syntax and valid shard-level metric names.
- [Update shard count](https://docs.aws.amazon.com/cli/latest/reference/kinesis/update-shard-count.html) — `aws kinesis update-shard-count` with `--scaling-type UNIFORM_SCALING --target-shard-count`.
- [Describe stream summary](https://docs.aws.amazon.com/cli/latest/reference/kinesis/describe-stream-summary.html) — `describe-stream-summary` open shard count / retention output.
- [Kinesis using sdk java resharding](https://docs.aws.amazon.com/streams/latest/dev/kinesis-using-sdk-java-resharding.html) — resharding model: split-shard vs merge-shards behavior.
- [Kinesis fis provisioned throughput](https://docs.aws.amazon.com/streams/latest/dev/kinesis-fis-provisioned-throughput.html) — ProvisionedThroughputExceededException (HTTP 400) causes: spikes, insufficient shard capacity, uneven partition-key distribution; per-shard write (1 MB/s, 1000 rec/s) and read (2 MB/s, 5 tps) limits.
