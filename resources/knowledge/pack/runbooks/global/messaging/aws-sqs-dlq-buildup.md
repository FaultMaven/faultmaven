---
id: aws-sqs-dlq-buildup
title: "AWS SQS Dead Letter Queue Buildup"
domain: messaging
service: aws-sqs
symptom_class:
  - data_loss
  - throughput_degradation
severity: high
scope: global
version: "2.0.0"
last_updated: "2026-06-25"
verified_by: kb-researcher
status: draft
tags:
  - aws
  - sqs
  - dlq
  - dead-letter
  - visibility-timeout
  - poison-pill
  - redrive
difficulty: intermediate
---

## Symptom Recognition

- `ApproximateNumberOfMessagesVisible` on the DLQ queue rises steadily in CloudWatch; any non-zero value on a DLQ warrants immediate investigation.
- Source queue `NumberOfMessagesReceived` metric is elevated but `NumberOfMessagesDeleted` lags behind — messages are received but not deleted.
- Consumer logs show repeated exceptions or unhandled errors for the same message IDs across multiple processing attempts.
- CloudWatch alarm on `ApproximateNumberOfMessagesVisible > 0` for the DLQ enters ALARM state.
- For Lambda event source mappings: `IteratorAge` grows or function invocation error count spikes.
- Messages silently disappear from the DLQ when `MessageRetentionPeriod` is reached, causing silent data loss — standard queues use original enqueue timestamp, so DLQ retention must exceed source queue retention.
- For FIFO queues: a single failing message blocks all subsequent messages in the same message group ID from being delivered.

## Applicability

- AWS SQS Standard and FIFO queues in any AWS Region. Requires IAM permissions: `sqs:GetQueueAttributes`, `sqs:ReceiveMessage`, `sqs:DeleteMessage`, `sqs:ChangeMessageVisibility`, `sqs:SetQueueAttributes`, `cloudwatch:GetMetricStatistics`, `logs:FilterLogEvents`, and `iam:SimulatePrincipalPolicy`.
- AWS CLI v2 installed and configured with credentials for the target account. Python 3.8+ available for inline scripts.
- Access to the consumer application's CloudWatch log group. For Lambda-triggered queues: Lambda function name and IAM execution role ARN. For ECS/EC2-based consumers: the application's log group name.
- The source queue URL, DLQ URL, and their respective ARNs. The `maxReceiveCount` value configured in the source queue's redrive policy.

## Diagnostic Steps

### Step 1: Measure DLQ depth and confirm active growth

```bash
SOURCE_Q="https://sqs.us-east-1.amazonaws.com/123456789012/my-queue"
DLQ_Q="https://sqs.us-east-1.amazonaws.com/123456789012/my-queue-dlq"

# Current approximate message count in the DLQ
aws sqs get-queue-attributes \
  --queue-url "$DLQ_Q" \
  --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible \
  --output json

# Growth rate over the past hour via CloudWatch (5-minute granularity)
aws cloudwatch get-metric-statistics \
  --namespace AWS/SQS \
  --metric-name ApproximateNumberOfMessagesVisible \
  --dimensions Name=QueueName,Value=my-queue-dlq \
  --start-time "$(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ)" \
  --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --period 300 \
  --statistics Maximum \
  --output json | python3 -c "
import sys, json
data = json.load(sys.stdin)
pts = sorted(data['Datapoints'], key=lambda x: x['Timestamp'])
for p in pts:
    print(f\"{p['Timestamp']}: {p['Maximum']:.0f} messages\")
"
```

Expected output: `ApproximateNumberOfMessages` > 0 confirms buildup. The CloudWatch time series shows whether the count is growing (active consumer failures), stable (historical failures, root cause may be resolved), or decreasing (redrive in progress). A rising trend across multiple 5-minute intervals confirms the consumer is currently failing.

### Step 2: Sample DLQ messages and inspect receive counts

```bash
DLQ_Q="https://sqs.us-east-1.amazonaws.com/123456789012/my-queue-dlq"

# Receive and inspect up to 10 sample messages (non-destructive peek)
aws sqs receive-message \
  --queue-url "$DLQ_Q" \
  --max-number-of-messages 10 \
  --attribute-names All \
  --message-attribute-names All \
  --output json | python3 -c "
import sys, json
data = json.load(sys.stdin)
msgs = data.get('Messages', [])
print(f'Sampled {len(msgs)} messages')
for i, msg in enumerate(msgs):
    attrs = msg.get('Attributes', {})
    body_preview = msg.get('Body', '')[:120]
    print(f'--- Message {i+1} ---')
    print(f'  MessageId: {msg.get(\"MessageId\")}')
    print(f'  ApproximateReceiveCount: {attrs.get(\"ApproximateReceiveCount\", \"N/A\")}')
    print(f'  SentTimestamp: {attrs.get(\"SentTimestamp\", \"N/A\")}')
    print(f'  Body preview: {body_preview}')
"
```

Expected output: each message shows `ApproximateReceiveCount` equal to or near the `maxReceiveCount` threshold. Look for structural patterns in message bodies: shared schema violations, messages from a specific producer, consistently oversized payloads, or missing required fields. If receive counts vary widely (some at 1, some at 10), the root cause is likely transient downstream failures. If all messages share a structural pattern, it points to a poison pill condition.

### Step 3: Retrieve the source queue redrive policy and visibility timeout

```bash
SOURCE_Q="https://sqs.us-east-1.amazonaws.com/123456789012/my-queue"

aws sqs get-queue-attributes \
  --queue-url "$SOURCE_Q" \
  --attribute-names RedrivePolicy VisibilityTimeout MessageRetentionPeriod \
  --output json | python3 -c "
import sys, json
data = json.load(sys.stdin)
attrs = data.get('Attributes', {})
print(f'VisibilityTimeout: {attrs.get(\"VisibilityTimeout\", \"N/A\")} seconds')
print(f'MessageRetentionPeriod: {attrs.get(\"MessageRetentionPeriod\", \"N/A\")} seconds')
rp = attrs.get('RedrivePolicy', '{}')
rp_parsed = json.loads(rp)
print(f'maxReceiveCount: {rp_parsed.get(\"maxReceiveCount\", \"N/A\")}')
print(f'deadLetterTargetArn: {rp_parsed.get(\"deadLetterTargetArn\", \"N/A\")}')
"
```

Expected output: `maxReceiveCount` should be between 5 and 10 for most workloads — a value of 1 means any single transient failure immediately DLQs the message. `VisibilityTimeout` should be at least 6x the average consumer processing time; for Lambda-triggered queues, it must be at least 6x the Lambda function timeout. Values of `VisibilityTimeout=30` (default) combined with processing times over 5 seconds indicate a timeout misconfiguration.

### Step 4: Search consumer logs for the specific failure reason

```bash
# For Lambda-triggered consumers
aws logs filter-log-events \
  --log-group-name "/aws/lambda/my-queue-processor" \
  --filter-pattern "?ERROR ?Exception ?Timeout ?failed ?error" \
  --start-time "$(date -u -d '1 hour ago' +%s)000" \
  --end-time "$(date -u +%s)000" \
  --limit 30 \
  --output json | python3 -c "
import sys, json
data = json.load(sys.stdin)
for e in data.get('events', []):
    print(e.get('message', '').rstrip())
"

# For ECS or EC2-based consumers
aws logs filter-log-events \
  --log-group-name "/ecs/my-consumer-service" \
  --filter-pattern "?Exception ?error ?failed ?timeout" \
  --start-time "$(date -u -d '1 hour ago' +%s)000" \
  --end-time "$(date -u +%s)000" \
  --limit 30 \
  --output json | python3 -c "
import sys, json
data = json.load(sys.stdin)
for e in data.get('events', []):
    print(e.get('message', '').rstrip())
"
```

Expected output: specific exception types and messages indicating the failure reason. `JSONDecodeError` or `ValidationError` points to malformed message payloads (poison pill). `ConnectionRefused`, `TimeoutError`, or `ServiceUnavailable` points to downstream dependency failures. `AccessDenied` or `403` errors point to IAM permission issues. No error output at all when the consumer appears to process messages may indicate the consumer is not calling `DeleteMessage` after successful processing.

### Step 5: Verify consumer IAM permissions for SQS actions

```bash
CONSUMER_ROLE_ARN="arn:aws:iam::123456789012:role/my-consumer-role"
SOURCE_QUEUE_ARN="arn:aws:sqs:us-east-1:123456789012:my-queue"

aws iam simulate-principal-policy \
  --policy-source-arn "$CONSUMER_ROLE_ARN" \
  --action-names sqs:ReceiveMessage sqs:DeleteMessage sqs:ChangeMessageVisibility sqs:GetQueueAttributes \
  --resource-arns "$SOURCE_QUEUE_ARN" \
  --output json | python3 -c "
import sys, json
data = json.load(sys.stdin)
for r in data.get('EvaluationResults', []):
    action = r.get('EvalActionName')
    decision = r.get('EvalDecision')
    print(f'{action}: {decision}')
"
```

Expected output: all four actions should print `allowed`. If `sqs:DeleteMessage` shows `implicitDeny` or `explicitDeny`, the consumer receives and processes messages but cannot delete them — every processed message is redelivered indefinitely until `maxReceiveCount` is exhausted and the message is DLQ'd.

### Step 6: Compare visibility timeout to observed consumer processing duration

```bash
FUNCTION_NAME="my-queue-processor"

# Check Lambda function timeout configuration
aws lambda get-function-configuration \
  --function-name "$FUNCTION_NAME" \
  --query "{Timeout: Timeout, MemorySize: MemorySize, Role: Role}" \
  --output json

# Check p99 processing duration over the past hour
aws cloudwatch get-metric-statistics \
  --namespace AWS/Lambda \
  --metric-name Duration \
  --dimensions Name=FunctionName,Value="$FUNCTION_NAME" \
  --start-time "$(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ)" \
  --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --period 3600 \
  --statistics Average Maximum \
  --output json | python3 -c "
import sys, json
data = json.load(sys.stdin)
pts = data.get('Datapoints', [{}])
if pts:
    p = pts[0]
    print(f'Average duration: {p.get(\"Average\", 0):.0f} ms')
    print(f'Maximum duration: {p.get(\"Maximum\", 0):.0f} ms')
    print(f'Recommended min VisibilityTimeout: {int(p.get(\"Maximum\", 0) / 1000 * 6 + 60)} seconds')
"
```

Expected output: the `VisibilityTimeout` from Step 3 must exceed the maximum observed duration. AWS recommends that for Lambda event source mappings, the queue visibility timeout is at least 6 times the Lambda function timeout setting. If maximum observed duration is 45,000 ms (45 s) and visibility timeout is 30 s, every slow message will be redelivered and eventually DLQ'd.

## Causes

### Cause A: Visibility timeout shorter than consumer processing time

**Statement:** The source queue's `VisibilityTimeout` is shorter than the consumer's processing time, so SQS makes the message visible again mid-processing, causing repeated redelivery until `maxReceiveCount` is exhausted.

**Chain:**
- root: `VisibilityTimeout` is set shorter than the consumer's actual processing time.
- s1: SQS makes the message visible again before the consumer calls `DeleteMessage`, incrementing `ApproximateReceiveCount`.
- s2: each slow message is redelivered every visibility window, consuming one `maxReceiveCount` unit per cycle even though it eventually succeeds.
- D: once redeliveries exceed `maxReceiveCount`, messages move to the DLQ and DLQ depth rises (Symptom).

**Indicators:**
- root: [Step 3] `VisibilityTimeout` is 30 (the default) while the workload has non-trivial downstream I/O.
- s1: [Step 6] maximum observed Lambda duration exceeds the `VisibilityTimeout` from Step 3.
- s2: [Step 2] DLQ messages show `ApproximateReceiveCount` equal to `maxReceiveCount`; consumer logs show no error for those IDs (it succeeded, but too late).

**Interventions:**
- **remediation** (root): set `VisibilityTimeout` to at least 6x the Lambda timeout (or 6x observed p99) and align the Lambda timeout; for long-running consumers add a `ChangeMessageVisibility` heartbeat rather than a high static timeout.

  ```bash
  # Long-running consumers: heartbeat via ChangeMessageVisibility (e.g. every 30s)
  aws sqs change-message-visibility \
    --queue-url "https://sqs.us-east-1.amazonaws.com/123456789012/my-queue" \
    --receipt-handle "<receipt-handle>" \
    --visibility-timeout 120

  # Lambda: set function timeout and queue VisibilityTimeout together
  aws lambda update-function-configuration \
    --function-name my-queue-processor --timeout 120
  aws sqs set-queue-attributes \
    --queue-url "https://sqs.us-east-1.amazonaws.com/123456789012/my-queue" \
    --attributes VisibilityTimeout=720
  ```

  **Verification:** Step 6 duration stays below the new `VisibilityTimeout`; Step 1 DLQ stable or decreasing; Step 3 confirms the updated value.
- **mitigation** (s1): raise `VisibilityTimeout` to at least 6x the Lambda timeout (or 6x observed p99) to stop mid-processing redelivery immediately.

  ```bash
  # Set VisibilityTimeout to 6x the Lambda timeout / p99 (e.g. 60s → 360s)
  aws sqs set-queue-attributes \
    --queue-url "https://sqs.us-east-1.amazonaws.com/123456789012/my-queue" \
    --attributes VisibilityTimeout=360
  ```

  **Risk:** A higher `VisibilityTimeout` delays retry of a crashed consumer's message by another consumer. **Duration:** Immediate. **Verification:** Step 6 duration sits below the new `VisibilityTimeout`; Step 1 DLQ no longer growing.

### Cause B: Poison pill messages with malformed or incompatible payloads

**Statement:** One or more messages carry payloads that consistently trigger unhandled consumer exceptions, so every attempt fails until `maxReceiveCount` is exhausted and they move to the DLQ.

**Chain:**
- root: a message carries content the consumer cannot handle — malformed JSON, missing fields, oversized body, unexpected schema version, or invalid data type.
- s1: the consumer throws an unhandled exception on every receive of that message, so `DeleteMessage` is never called.
- s2: SQS requeues the message each visibility timeout, exhausting `maxReceiveCount`; on FIFO this blocks every message in the same `MessageGroupId`.
- D: after `maxReceiveCount` attempts the poison pill moves to the DLQ, growing DLQ depth (Symptom).

**Indicators:**
- root: [Step 2] DLQ messages share a structural pattern in their body previews — missing fields, unusual encoding, size outlier, or identical schema.
- s1: [Step 4] consumer logs show `JSONDecodeError`, `ValidationError`, `KeyError`, `NullPointerException`, or similar parsing/schema error for the same message IDs.
- s2: [Step 2] all DLQ messages have `ApproximateReceiveCount` equal to `maxReceiveCount` (fully exhausted retries).

**Interventions:**
- **remediation** (root): add schema validation before business logic so invalid payloads are logged and deleted rather than retried; for Lambda enable `ReportBatchItemFailures`; fix the producer emitting them.

  ```bash
  # Enable partial-batch reporting; the consumer must validate and delete bad payloads
  aws lambda update-event-source-mapping \
    --uuid "<event-source-mapping-uuid>" \
    --function-response-types ReportBatchItemFailures
  ```

  **Verification:** Step 4 logs show `Poison pill` entries rather than unhandled exceptions; Step 1 DLQ no longer growing; a test message with the failing schema is deleted (not DLQ'd).
- **mitigation** (s2): log and delete the poison pill from the DLQ to clear the buildup while the fix is deployed.

  ```bash
  DLQ="https://sqs.us-east-1.amazonaws.com/123456789012/my-queue-dlq"
  MSG=$(aws sqs receive-message --queue-url "$DLQ" --max-number-of-messages 1 \
    --attribute-names All --output json | tee /tmp/poison-pill-$(date +%s).json)
  RECEIPT=$(echo "$MSG" | python3 -c "import sys,json;print(json.load(sys.stdin)['Messages'][0]['ReceiptHandle'])")
  aws sqs delete-message --queue-url "$DLQ" --receipt-handle "$RECEIPT"
  ```

  **Risk:** Destructive — the producer regenerates poison pills until fixed; deleting without logging discards forensics. **Duration:** Stop-gap until the consumer validates input and the producer is fixed. **Verification:** body in `/tmp/poison-pill-*.json`; Step 1 DLQ drops by the deleted message and does not regrow.

### Cause C: maxReceiveCount set too low for the transient failure rate

**Statement:** The source queue's `maxReceiveCount` is configured at 1 or 2, causing any single transient consumer failure — network blip, brief downstream timeout, cold start — to immediately route the message to the DLQ rather than retrying.

**Chain:**
- root: `maxReceiveCount` is set at 1, 2, or 3, leaving no retry headroom for expected transient failures.
- s1: a normal transient failure (Lambda cold start, brief connection drop, packet loss) trips the low ceiling on the first or second attempt.
- s2: the message is DLQ'd immediately with no further retry, even though the consumer is fundamentally healthy.
- D: steady DLQ growth accumulates from routine transient failures (Symptom).

**Indicators:**
- root: [Step 3] `maxReceiveCount` is 1, 2, or 3 in the redrive policy output.
- s1: [Step 4] consumer logs show transient errors (connection timeouts, rate limit errors, brief exceptions) rather than persistent schema or logic failures.
- s2: [Step 2] DLQ messages show mixed `ApproximateReceiveCount` values (some at 1, some at 2) rather than all at the `maxReceiveCount` ceiling — messages fail on the first attempt with no retry opportunity.

**Interventions:**
- **remediation** (root): increase `maxReceiveCount` to 5-10 in the redrive policy so transient failures are retried instead of immediately quarantined.

  ```bash
  # Increase maxReceiveCount to 5-10 to tolerate transient failures
  SOURCE_Q="https://sqs.us-east-1.amazonaws.com/123456789012/my-queue"
  DLQ_ARN="arn:aws:sqs:us-east-1:123456789012:my-queue-dlq"

  aws sqs set-queue-attributes \
    --queue-url "$SOURCE_Q" \
    --attributes "{\"RedrivePolicy\": \"{\\\"deadLetterTargetArn\\\":\\\"${DLQ_ARN}\\\",\\\"maxReceiveCount\\\":\\\"10\\\"}\"}"
  ```

  **Verification:** Step 3 shows the updated `maxReceiveCount` value; Step 1 shows DLQ growth rate decreasing after the change; Step 4 shows transient errors resolving on subsequent consumer attempts without messages reaching the DLQ.

### Cause D: Downstream dependency failure causes persistent consumer errors

**Statement:** A downstream dependency — database, HTTP API, or internal service — is unavailable or slow, so every consumer attempt fails and messages accumulate in the DLQ after `maxReceiveCount` retries.

**Chain:**
- root: a downstream dependency the consumer delegates to becomes unreachable or slow (DB down, API 5xx, network ACL blocking egress).
- s1: every consumer processing attempt fails, so `DeleteMessage` is never called and SQS requeues each message.
- s2: each message exhausts its `maxReceiveCount` retries during the outage window and is moved to the DLQ.
- D: DLQ depth grows with message volume arriving during the outage, bounding message loss if retention expires (Symptom).

**Indicators:**
- root: [Step 4] consumer logs show `ConnectionRefused`, `ServiceUnavailable`, `ReadTimeout`, `ConnectionTimeout`, or `HTTPError 5xx` for the downstream dependency.
- s1: [Step 1] DLQ growth rate tracks the source queue's incoming message rate — every message processed during the outage ends up in the DLQ.
- s2: [Step 2] DLQ message receive counts cluster at `maxReceiveCount` and DLQ depth correlates with the downstream outage window.

**Interventions:**
- **remediation** (root): once the downstream is healthy, re-enable the event source mapping and redrive the DLQ messages back to the source queue.

  ```bash
  DLQ_ARN="arn:aws:sqs:us-east-1:123456789012:my-queue-dlq"
  # Re-enable the consumer
  aws lambda update-event-source-mapping \
    --uuid "<event-source-mapping-uuid>" --enabled true
  # Redrive DLQ to source, then monitor progress
  aws sqs start-message-move-task --source-arn "$DLQ_ARN"
  aws sqs list-message-move-tasks --source-arn "$DLQ_ARN"
  ```

  **Verification:** Step 4 logs no longer show downstream errors; Step 1 DLQ stable or decreasing after re-enable; source queue `ApproximateNumberOfMessages` drains to zero.
- **mitigation** (s1): suspend the Lambda event source mapping while the downstream is restored, so messages wait in the source queue instead of exhausting retries.

  ```bash
  # Suspend the Lambda event source mapping (restore with --enabled true)
  ESM_UUID=$(aws lambda list-event-source-mappings \
    --function-name my-queue-processor \
    --query "EventSourceMappings[?contains(EventSourceArn,'my-queue')].UUID" \
    --output text)
  aws lambda update-event-source-mapping --uuid "$ESM_UUID" --enabled false
  ```

  **Risk:** Raising `maxReceiveCount` while the downstream is down only delays DLQ entry; queue retention bounds the wait. **Duration:** Hold until the downstream is healthy, then re-enable. **Verification:** Step 1 shows the DLQ stops growing while suspended and the source queue holds the backlog; re-enabling drains it.

### Cause E: Consumer does not call DeleteMessage after successful processing

**Statement:** The consumer processes messages successfully but never calls `DeleteMessage`, so SQS requeues each processed message at the end of every visibility timeout until `maxReceiveCount` is exhausted and it moves to the DLQ.

**Chain:**
- root: the consumer completes processing but omits the `DeleteMessage` call (missing `finally`, cleanup-branch exception, or misread Lambda ESM contract).
- s1: SQS never gets the success ack, so it treats the message as unprocessed and requeues it each visibility timeout.
- s2: the message is redelivered until `maxReceiveCount` is exhausted; with `ReportBatchItemFailures` disabled, one failed item redelivers the entire Lambda batch.
- D: each phantom-redelivered message eventually moves to the DLQ, growing depth despite healthy processing (Symptom).

**Indicators:**
- root: [Step 5] `sqs:DeleteMessage` is `allowed` for the consumer role (ruling out an IAM cause).
- s1: [Step 1] source queue `NumberOfMessagesNotVisible` is consistently non-zero even when throughput appears normal — messages are in flight indefinitely.
- s2: [Step 4] consumer logs show successful processing but no errors — the consumer appears healthy yet DLQ depth grows.

**Interventions:**
- **remediation** (root): ensure `DeleteMessage` is called (or the Lambda returns successfully) for every message, and enable `ReportBatchItemFailures` so partial failures don't redeliver the batch.

  ```bash
  # Prevent whole-batch redelivery on partial failure
  aws lambda update-event-source-mapping \
    --uuid "<event-source-mapping-uuid>" \
    --function-response-types ReportBatchItemFailures
  # Non-Lambda: call sqs.delete_message only on success, else re-raise to retry.
  ```

  **Verification:** Step 1 `ApproximateNumberOfMessagesNotVisible` returns to near zero after each batch; DLQ stops growing; Step 4 shows processing without phantom redeliveries.
- **mitigation** (s1): inspect Lambda REPORT lines to confirm the function completes without error (missing delete, not thrown exception) before deploying the fix.

  ```bash
  # Check Lambda REPORT lines for clean completion
  aws logs filter-log-events \
    --log-group-name "/aws/lambda/my-queue-processor" \
    --filter-pattern "START END REPORT" \
    --start-time "$(date -u -d '30 minutes ago' +%s)000" \
    --limit 20 --output text --query "events[*].message"
  ```

  **Risk:** Enabling `ReportBatchItemFailures` may expose message failures previously masked by whole-batch retry. **Duration:** Diagnostic; a `Billed Duration` REPORT line with no error means the function completes but isn't deleting via direct SDK calls. **Verification:** REPORT lines confirm clean completion, pointing to a missing `DeleteMessage`.

### Cause Z: Unidentified

**Statement:** DLQ depth is confirmed growing but the Cause A–E indicators did not match; the driver is a less common origin (SQS service-side issue, cross-account redrive misconfig, Lambda concurrency or in-flight `OverLimit` quota, VPC endpoint connectivity, or SSE-KMS key access failure).

**Indicators:**
- [Default]

**Interventions:**
- **mitigation** (D): collect a full diagnostic snapshot (source and DLQ attributes, a DLQ message sample, and the DLQ send-rate metric), then escalate to AWS Support or the queue owner with the artefacts attached.

  ```bash
  # Collect a diagnostic bundle before escalation
  DLQ_Q="https://sqs.us-east-1.amazonaws.com/123456789012/my-queue-dlq"
  SOURCE_Q="https://sqs.us-east-1.amazonaws.com/123456789012/my-queue"
  TS=$(date +%s)

  aws sqs get-queue-attributes \
    --queue-url "$SOURCE_Q" \
    --attribute-names All > /tmp/sqs-source-attrs-$TS.json

  aws sqs get-queue-attributes \
    --queue-url "$DLQ_Q" \
    --attribute-names All > /tmp/sqs-dlq-attrs-$TS.json

  aws sqs receive-message \
    --queue-url "$DLQ_Q" \
    --max-number-of-messages 10 \
    --attribute-names All \
    --output json > /tmp/sqs-dlq-sample-$TS.json

  aws cloudwatch get-metric-statistics \
    --namespace AWS/SQS \
    --metric-name NumberOfMessagesSent \
    --dimensions Name=QueueName,Value=my-queue-dlq \
    --start-time "$(date -u -d '6 hours ago' +%Y-%m-%dT%H:%M:%SZ)" \
    --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --period 300 --statistics Sum > /tmp/sqs-dlq-sent-$TS.json

  tar czf /tmp/sqs-dlq-bundle-$TS.tgz /tmp/sqs-*-$TS.json
  echo "Diagnostic bundle: /tmp/sqs-dlq-bundle-$TS.tgz"
  ```

  **Risk:** Purging the DLQ permanently deletes all messages and discards forensic data; only purge after confirming messages are not recoverable and no longer needed. **Duration:** Minutes; collect, document findings, then escalate to AWS Support or the queue owner with the source queue ARN, DLQ ARN, onset time window, and consumer runtime details. **Verification:** Hand-off acknowledged by the receiving engineer and an incident ticket opened with the captured artefacts, the DLQ growth timeline, and a follow-up owner assigned.

## Prevention

- Set `maxReceiveCount` to 5-10 for most workloads to tolerate transient failures while still quarantining persistent ones within a reasonable time window.
- Set `VisibilityTimeout` to at least 6x the average consumer processing duration; for Lambda event source mappings, set it to at least 6x the Lambda function timeout.
- Create a CloudWatch alarm on DLQ `ApproximateNumberOfMessagesVisible > 0` — any message in the DLQ warrants investigation because it represents a processing failure.
- Set the DLQ `MessageRetentionPeriod` to 14 days (maximum) and verify it exceeds the source queue retention period; for Standard queues the DLQ uses the original enqueue timestamp, so a message that spent 3 days in the source queue has only 1 day of DLQ retention remaining with the default 4-day DLQ retention.
- Enable `ReportBatchItemFailures` on all Lambda SQS event source mappings to prevent re-processing successfully handled items in a batch when one item fails.
- Add input schema validation in the consumer before business logic, and explicitly call `DeleteMessage` (or return successfully from Lambda) for messages that are unprocessable to prevent them from exhausting `maxReceiveCount` retries.
- Use `ChangeMessageVisibility` in long-running consumers to extend the visibility timeout programmatically as a heartbeat rather than setting an excessively high static timeout; the maximum visibility timeout is 12 hours from first receive.
- Alert on `NumberOfMessagesSent` to the DLQ being non-zero for more than 5 consecutive minutes — a sustained send rate indicates an ongoing consumer failure rather than a one-time event.
- Alert on `ApproximateAgeOfOldestMessage` growing on the source queue — this metric precedes DLQ buildup and indicates processing is falling behind.
- For FIFO queues, implement per-message-group error isolation in the consumer so a single poison pill does not block the entire message group; track `MessageGroupId` in error logs to identify which groups are stalled.
- Implement exponential backoff in consumers for downstream dependency calls to reduce the rate at which transient failures consume `maxReceiveCount` retries.
- Automate DLQ monitoring with a scheduled Lambda function that checks DLQ depth and alerts when depth exceeds zero; optionally integrate with PagerDuty or Slack for immediate notification.

## Sources

- [AWS SQS Developer Guide — Dead-Letter Queues](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-dead-letter-queues.html) — Priority 1. `maxReceiveCount` semantics, `redrive policy` and `redrive allow policy` configuration, message retention period behaviour difference between Standard and FIFO DLQs (Standard: original enqueue timestamp unchanged; FIFO: reset on DLQ move), recommendation to set DLQ retention longer than source queue retention.
- [AWS SQS Developer Guide — Visibility Timeout](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-visibility-timeout.html) — Priority 1. Visibility timeout mechanics (message hidden on receive, re-queued on expiry), `ChangeMessageVisibility` API for programmatic extension, 12-hour maximum from first receive, in-flight message limits (120,000 for Standard queues, `OverLimit` error), heartbeat pattern recommendation, FIFO group-level visibility semantics.
- [AWS Lambda Developer Guide — Using Lambda with Amazon SQS](https://docs.aws.amazon.com/lambda/latest/dg/with-sqs.html) — Priority 1. Lambda polling and batch deletion behaviour (deletes on success, requeues entire batch on any exception unless `ReportBatchItemFailures` is enabled), idempotency requirement, `ReportBatchItemFailures` configuration for partial batch response, 6x Lambda timeout recommendation for queue `VisibilityTimeout`.
