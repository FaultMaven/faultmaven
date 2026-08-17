---
id: "dynamodb-throttling"
title: "AWS DynamoDB throttling: ProvisionedThroughputExceededException and hot partitions"
domain: database
service: aws-dynamodb
symptom_class: [throughput_degradation, latency]
severity: high
scope: global
version: "1.0.1"
last_updated: "2026-08-17"
verified_by: "kb-researcher"
status: draft
tags: [provisioned-throughput-exceeded, hot-partition, gsi-throttling, adaptive-capacity, exponential-backoff]
difficulty: advanced
---

## Symptom Recognition

- Application errors / SDK exceptions:
  - `ProvisionedThroughputExceededException`
  - `ThrottlingException`
  - `RequestLimitExceeded`
- HTTP status `400 Bad Request` returned by the DynamoDB endpoint for the throttling exceptions above.
- Elevated read/write latency and retries exhausting the SDK retry queue ("retry queue is too large to finish").
- CloudWatch `AWS/DynamoDB` metrics breaching zero:
  - `ThrottledRequests` (Sum > 0)
  - `ReadThrottleEvents` (Sum > 0)
  - `WriteThrottleEvents` (Sum > 0) — base table only unless a `GlobalSecondaryIndexName` dimension is supplied
- Throttling on a single table/index while overall consumed capacity is well below provisioned capacity (a hot-partition signature).

## Applicability

- Service: Amazon DynamoDB, both provisioned and on-demand capacity modes.
- Affected operations: `GetItem`, `Query`, `Scan`, `PutItem`, `UpdateItem`, `DeleteItem`, `BatchGetItem`, `BatchWriteItem`, and any GSI updates triggered by a base-table write.
- Required access/permissions: `dynamodb:DescribeTable`, `dynamodb:DescribeContributorInsights`, `dynamodb:UpdateContributorInsights`, `cloudwatch:GetMetricStatistics`, `cloudwatch:PutMetricAlarm`.
- Tools: AWS CLI v2 (`aws dynamodb`, `aws cloudwatch`), CloudWatch Contributor Insights enabled (optional but recommended for hot-key diagnosis).

## Diagnostic Steps

### Step 1: Confirm throttling is occurring at the table/index level

```bash
aws cloudwatch get-metric-statistics \
  --namespace AWS/DynamoDB \
  --metric-name ThrottledRequests \
  --dimensions Name=TableName,Value=ProductCatalog \
  --start-time 2026-06-24T00:00:00Z \
  --end-time 2026-06-24T23:59:59Z \
  --period 300 \
  --statistics Sum
```

Expected output: a `Datapoints` array; any datapoint with `Sum` greater than `0` confirms requests are being throttled in that 5-minute window.

### Step 2: Separate read vs write throttling and isolate a GSI

```bash
aws cloudwatch get-metric-statistics \
  --namespace AWS/DynamoDB \
  --metric-name WriteThrottleEvents \
  --dimensions Name=TableName,Value=ProductCatalog Name=GlobalSecondaryIndexName,Value=GSI_Status \
  --start-time 2026-06-24T00:00:00Z \
  --end-time 2026-06-24T23:59:59Z \
  --period 300 \
  --statistics Sum
```

Expected output: `Datapoints` with `Sum > 0` for the `GSI_Status` dimension means the GSI is throttling and applying back-pressure to base-table writes; run the same call with `ReadThrottleEvents` and with the `TableName`-only dimension to compare.

### Step 3: Compare consumed vs provisioned capacity

```bash
aws cloudwatch get-metric-statistics \
  --namespace AWS/DynamoDB \
  --metric-name ConsumedReadCapacityUnits \
  --dimensions Name=TableName,Value=ProductCatalog \
  --start-time 2026-06-24T00:00:00Z \
  --end-time 2026-06-24T23:59:59Z \
  --period 360 \
  --statistics Average Maximum
```

Expected output: `Average`/`Maximum` consumed capacity datapoints. If consumed capacity sits well below provisioned `ReadCapacityUnits`/`WriteCapacityUnits` yet Step 1 shows throttling, the cause is a hot partition rather than aggregate under-provisioning.

### Step 4: Read the table's provisioned throughput and GSI list

```bash
aws dynamodb describe-table \
  --table-name ProductCatalog \
  --query 'Table.[BillingModeSummary.BillingMode, ProvisionedThroughput, GlobalSecondaryIndexes[].{Index:IndexName,Keys:KeySchema,PT:ProvisionedThroughput}]'
```

Expected output: the billing mode (`PROVISIONED` or `PAY_PER_REQUEST`), the table's provisioned RCU/WCU, and each GSI's key schema and provisioned throughput — used to map a throttling index to its partition-key attribute.

### Step 5: Identify the hottest keys with Contributor Insights

```bash
aws dynamodb update-contributor-insights \
  --table-name ProductCatalog \
  --contributor-insights-action ENABLE

aws dynamodb describe-contributor-insights \
  --table-name ProductCatalog \
  --query '[ContributorInsightsStatus, ContributorInsightsRuleList]'
```

Expected output: `ContributorInsightsStatus` transitions to `ENABLED`; the resulting CloudWatch Contributor Insights "most throttled keys" graph names the specific partition-key (or partition+sort) value receiving disproportionate, throttled traffic.

## Causes

### Cause A: Low-cardinality partition key concentrates traffic on one partition
**Statement:** The table's partition key has too few distinct, evenly-accessed values (e.g. a status or date key), so one partition receives more than the per-partition limit of 3000 read units/sec or 1000 write units/sec.
**Chain:**
- root: low-cardinality / skewed partition key
- s1: a single physical partition absorbs a disproportionate share of traffic (hot partition)
- s2: that partition exceeds its 3000 RCU/sec or 1000 WCU/sec ceiling while total table capacity is unused
- D: requests to those keys fail with ProvisionedThroughputExceededException
**Indicators:**
- root: [Step 5] Contributor Insights "most throttled keys" graph is dominated by one or a few partition-key values
- s1: [Step 3] consumed capacity is far below provisioned capacity while throttling persists
- s2: [Step 1] ThrottledRequests Sum > 0 on the base table
- D: [Symptom] application logs show ProvisionedThroughputExceededException
**Interventions:**
- **remediation** (root): Redesign the partition key for high cardinality and uniform access — switch to a key with many distinct values, or add a calculated write-sharding suffix so writes spread across N logical partitions; backfill into a new table and repoint the application.

  ```bash
  aws dynamodb create-table \
    --table-name ProductCatalog_v2 \
    --attribute-definitions AttributeName=pk_sharded,AttributeType=S AttributeName=sk,AttributeType=S \
    --key-schema AttributeName=pk_sharded,KeyType=HASH AttributeName=sk,KeyType=RANGE \
    --billing-mode PAY_PER_REQUEST
  ```

  **Verification:** re-run Step 5 — Contributor Insights shows throttled traffic distributed across many keys with no single dominant key; re-run Step 1 returns `Sum = 0`.
- **mitigation** (s1): Raise table-wide provisioned capacity for short-term headroom while the partition-key redesign ships.

  ```bash
  aws dynamodb update-table \
    --table-name ProductCatalog \
    --provisioned-throughput ReadCapacityUnits=4000,WriteCapacityUnits=2000
  ```

  **Risk:** raising table-wide capacity costs more and does not fix the skew if a single key still exceeds the per-partition limit. **Duration:** until the partition-key redesign ships. **Verification:** Step 1 `ThrottledRequests` Sum trends toward 0.

### Cause B: Aggregate provisioned capacity is below sustained demand
**Statement:** The table is in `PROVISIONED` mode and consumed read or write capacity has risen to meet or exceed the configured `ReadCapacityUnits`/`WriteCapacityUnits`, so even evenly-distributed traffic is throttled.
**Chain:**
- root: provisioned RCU/WCU set below sustained workload demand
- s1: consumed capacity reaches the provisioned ceiling table-wide
- D: requests are throttled with ProvisionedThroughputExceededException
**Indicators:**
- root: [Step 4] describe-table shows `BillingMode` PROVISIONED with low ProvisionedThroughput relative to load
- s1: [Step 3] Average/Maximum consumed capacity is at or near provisioned units (not a hot-partition pattern)
- D: [Step 1] ThrottledRequests Sum > 0 across many keys
**Interventions:**
- **remediation** (root): Raise provisioned capacity to cover sustained demand, or convert to on-demand (`PAY_PER_REQUEST`) so capacity scales automatically; pre-warm with warm throughput ahead of known spikes.

  ```bash
  aws dynamodb update-table \
    --table-name ProductCatalog \
    --billing-mode PAY_PER_REQUEST
  ```

  **Verification:** re-run Step 3 — consumed capacity no longer pinned at a ceiling; Step 1 `ThrottledRequests` returns `Sum = 0`.
- **defensive_fix** (s1): Enable auto scaling so provisioned capacity tracks utilization automatically and absorbs growth without manual edits.

  ```bash
  aws application-autoscaling register-scalable-target \
    --service-namespace dynamodb \
    --resource-id "table/ProductCatalog" \
    --scalable-dimension "dynamodb:table:WriteCapacityUnits" \
    --min-capacity 1000 --max-capacity 8000
  ```

  **Verification:** consumed capacity stays below the dynamically-scaled provisioned value; Step 1 stays at 0 through the next traffic peak.

### Cause C: GSI back-pressure throttles base-table writes
**Statement:** A global secondary index has insufficient write capacity or a hot GSI partition key (e.g. `status` with few values), so the GSI throttles and propagates back-pressure that throttles the base-table write even when the base table has ample capacity.
**Chain:**
- root: GSI under-provisioned or its partition key is low-cardinality
- s1: the GSI cannot absorb the index-write volume from base-table updates
- s2: GSI back-pressure throttles the originating base-table write
- D: writes fail with ProvisionedThroughputExceededException / ThrottlingException
**Indicators:**
- root: [Step 4] describe-table shows the GSI key schema uses a low-cardinality attribute and/or low ProvisionedThroughput
- s1: [Step 2] WriteThrottleEvents Sum > 0 when the GlobalSecondaryIndexName dimension is supplied
- s2: [Step 1] base-table ThrottledRequests Sum > 0 with no base-table hot key in Step 5
- D: [Symptom] write paths log ThrottlingException / ProvisionedThroughputExceededException
**Interventions:**
- **remediation** (root): Give the GSI a higher-cardinality partition key (or add a sharding suffix), and/or provision the GSI's write capacity to match the base-table write rate.

  ```bash
  aws dynamodb update-table \
    --table-name ProductCatalog \
    --attribute-definitions AttributeName=status,AttributeType=S \
    --global-secondary-index-updates '[{"Update":{"IndexName":"GSI_Status","ProvisionedThroughput":{"ReadCapacityUnits":3000,"WriteCapacityUnits":3000}}}]'
  ```

  **Verification:** re-run Step 2 against the `GSI_Status` dimension — `WriteThrottleEvents` Sum returns to 0; base-table writes succeed.
- **mitigation** (s1): Drop non-essential GSIs or project fewer attributes to reduce per-write index work until the index is redesigned.

  ```bash
  aws dynamodb update-table \
    --table-name ProductCatalog \
    --global-secondary-index-updates '[{"Delete":{"IndexName":"GSI_Status"}}]'
  ```

  **Risk:** deleting a GSI breaks queries that depend on it; verify no production query path uses it. **Duration:** until a high-cardinality replacement GSI is built. **Verification:** Step 2 against the deleted index returns no datapoints; base-table write throttling clears.

### Cause D: No exponential backoff, so transient/adaptive-capacity throttles surface as hard errors
**Statement:** The client does not implement (or has disabled) the SDK's exponential-backoff retry, so brief throttles that DynamoDB adaptive capacity would absorb within seconds bubble up as application-visible ProvisionedThroughputExceededException failures.
**Chain:**
- root: exponential-backoff retry missing or disabled in the client
- s1: short-lived throttles (e.g. before adaptive capacity reallocates) are not retried and ride out
- D: transient throttles become user-facing errors
**Indicators:**
- root: [Symptom] client config shows retries set to 0 or a custom client that bypasses SDK backoff
- s1: [Step 1] ThrottledRequests appears in brief bursts that resolve on their own within minutes
- D: [Symptom] application surfaces ProvisionedThroughputExceededException instead of silently succeeding on retry
**Interventions:**
- **defensive_fix** (s1): Use the AWS SDK default retry/backoff (progressively longer waits — e.g. up to 50 ms, then 100 ms, then 200 ms) and raise max retry attempts; for batch operations, back off the whole batch so the individual requests are far more likely to succeed.

  ```bash
  aws configure set default.retry_mode adaptive
  aws configure set default.max_attempts 10
  ```

  **Verification:** with backoff enabled, brief throttle bursts in Step 1 no longer correlate with application errors; the SDK retry eventually succeeds.

### Cause Z: Unidentified
**Statement:** Throttling persists but none of the above roots match — escalate with a full diagnostic snapshot.
**Indicators:**
- [Default]
**Interventions:**
- **mitigation** (D): Capture a complete diagnostic snapshot (metrics window, table description, Contributor Insights output) and escalate to the DynamoDB SME / AWS Support.

  ```bash
  aws dynamodb describe-table --table-name ProductCatalog > /tmp/ddb-describe.json
  aws dynamodb describe-contributor-insights --table-name ProductCatalog > /tmp/ddb-ci.json
  aws cloudwatch get-metric-statistics --namespace AWS/DynamoDB --metric-name ThrottledRequests \
    --dimensions Name=TableName,Value=ProductCatalog \
    --start-time 2026-06-24T00:00:00Z --end-time 2026-06-24T23:59:59Z \
    --period 300 --statistics Sum > /tmp/ddb-throttle.json
  ```

  **Risk:** snapshot-only, no change applied — throttling continues until the SME acts. **Duration:** until escalation is answered. **Verification:** SME confirms receipt of the three artifacts and assigns a root cause.

## Prevention

- Design partition keys for high cardinality and uniform access from the start; add write-sharding suffixes for known hot entities so no single key exceeds 3000 RCU/sec or 1000 WCU/sec.
- Mirror partition-key cardinality rules onto every GSI; a base table can be balanced while a low-cardinality GSI partition key (e.g. `status`) silently throttles writes.
- Keep CloudWatch Contributor Insights enabled in `THROTTLED_KEYS` mode on busy tables and indexes to surface hot keys before they cause incidents.
- Prefer on-demand (`PAY_PER_REQUEST`), or enable auto scaling and pre-provision warm throughput ahead of expected traffic spikes, so adaptive/burst capacity has headroom.
- Always run the AWS SDK default exponential backoff with adequate `max_attempts`; never disable retries on DynamoDB clients.
- Alarm on throttling so it is caught early:

  ```bash
  aws cloudwatch put-metric-alarm \
    --alarm-name DynamoDBThrottledRequests \
    --alarm-description "Alarm when DynamoDB requests exceed provisioned throughput" \
    --namespace AWS/DynamoDB \
    --metric-name ThrottledRequests \
    --dimensions Name=TableName,Value=ProductCatalog \
    --statistic Sum --threshold 0 --comparison-operator GreaterThanThreshold \
    --period 300 --unit Count --evaluation-periods 1 \
    --treat-missing-data notBreaching \
    --alarm-actions arn:aws:sns:us-east-1:123456789012:dynamodb-throttling
  ```

## Sources

- [Error handling with DynamoDB](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Programming.Errors.html) — exact exception strings (`ProvisionedThroughputExceededException`, `ThrottlingException`, `RequestLimitExceeded`), HTTP 400 classification, SDK auto-retry behavior, and the exponential-backoff progression (50/100/200 ms) including batch-operation backoff guidance.
- [Designing partition keys to distribute your workload](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/bp-partition-key-uniform-load.html) — hot partitions from low-cardinality/skewed partition keys (status codes and rounded dates as the documented "Bad" uniformity examples), design for many distinct, evenly-accessed key values — grounds Cause A's root and the partition-key Prevention guidance.
- [Using write sharding to distribute workloads evenly](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/bp-partition-key-sharding.html) — random and calculated write-sharding suffixes that expand the partition-key space — grounds Cause A's sharding-suffix remediation and the Prevention sharding guidance.
- [Understanding GSI write throttling and back pressure](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/gsi-throttling.html) — GSI back-pressure mechanism and the low-cardinality `status` GSI example used in Cause C.
- [CloudWatch throttling metrics](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/TroubleshootingThrottling-cloudwatch.html) — `ThrottledRequests`, `ReadThrottleEvents`, `WriteThrottleEvents` semantics and the GSI dimension requirement.
- [DynamoDB burst and adaptive capacity](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/burst-adaptive-capacity.html) — adaptive capacity behavior and the 3000 RCU/sec, 1000 WCU/sec per-partition limits.
- [Monitoring metrics in DynamoDB with Amazon CloudWatch](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Monitoring-metrics-with-Amazon-CloudWatch.html) — `get-metric-statistics` command form used in Steps 1-3.
- [Creating CloudWatch alarms to monitor DynamoDB](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/creating-alarms.html) — `put-metric-alarm` form used in Prevention.
- [CloudWatch Contributor Insights for DynamoDB: How it works](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/contributorinsights_HowItWorks.html) — most-throttled-keys graphs and `THROTTLED_KEYS` mode used in Step 5.
- [describe-contributor-insights — AWS CLI Reference](https://docs.aws.amazon.com/cli/latest/reference/dynamodb/describe-contributor-insights.html) — `update-contributor-insights` / `describe-contributor-insights` command forms used in Step 5.
