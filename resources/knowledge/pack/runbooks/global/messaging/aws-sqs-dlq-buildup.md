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

**Statement:** The source queue's `VisibilityTimeout` is less than the time the consumer needs to process a message, so SQS makes the message visible again mid-processing, causing repeated redelivery until `maxReceiveCount` is exhausted.

**Chain:**
- root: `VisibilityTimeout` is set shorter than the consumer's actual message processing time.
- s1: SQS makes the message visible again before the consumer calls `DeleteMessage`, incrementing `ApproximateReceiveCount`.
- s2: each slow message is redelivered every visibility window, consuming one `maxReceiveCount` unit per cycle even though the consumer eventually succeeds.
- D: once redeliveries exceed `maxReceiveCount`, messages move to the DLQ and DLQ depth rises (Symptom).

**Indicators:**
- root: [Step 3] `VisibilityTimeout` is 30 (the default) while any processing workload has non-trivial downstream I/O.
- s1: [Step 6] maximum observed Lambda duration exceeds the `VisibilityTimeout` value returned in Step 3.
  <!-- match: {"step": 6, "predicate": "threshold", "target": "VisibilityTimeout_seconds_vs_max_duration_seconds", "op": "<", "value": 1.0} -->
- s2: [Step 2] DLQ messages show `ApproximateReceiveCount` equal to `maxReceiveCount`; consumer logs show no error for the same message IDs (the consumer succeeded eventually, but too late).

**Interventions:**
- **remediation** (root): set `VisibilityTimeout` to at least 6x the Lambda function timeout (or 6x observed p99), and align the Lambda timeout itself; for long-running consumers add a `ChangeMessageVisibility` heartbeat rather than relying solely on a high static timeout.

  ```bash
  # For long-running consumers, implement ChangeMessageVisibility as a heartbeat
  # rather than relying solely on a high static timeout.
  # Example AWS CLI extension during processing (run every 30s in a background thread):
  aws sqs change-message-visibility \
    --queue-url "https://sqs.us-east-1.amazonaws.com/123456789012/my-queue" \
    --receipt-handle "<receipt-handle>" \
    --visibility-timeout 120

  # For Lambda: set function timeout and queue VisibilityTimeout together
  aws lambda update-function-configuration \
    --function-name my-queue-processor \
    --timeout 120

  aws sqs set-queue-attributes \
    --queue-url "https://sqs.us-east-1.amazonaws.com/123456789012/my-queue" \
    --attributes VisibilityTimeout=720
  ```

  **Verification:** After the change, Step 6's maximum duration metric stays below the new `VisibilityTimeout`; Step 1 shows DLQ depth stable or decreasing (no new messages entering); Step 3 confirms the updated `VisibilityTimeout` value on the queue.
- **mitigation** (s1): raise `VisibilityTimeout` to at least 6x the Lambda function timeout (or 6x observed p99 processing time) to stop mid-processing redelivery immediately.

  ```bash
  # Set VisibilityTimeout to at least 6x the Lambda function timeout (or 6x observed p99 processing time)
  # Example: Lambda timeout=60s → VisibilityTimeout=360s
  aws sqs set-queue-attributes \
    --queue-url "https://sqs.us-east-1.amazonaws.com/123456789012/my-queue" \
    --attributes VisibilityTimeout=360
  ```

  **Risk:** Raising `VisibilityTimeout` extends the window before a genuinely stuck or crashed consumer's message becomes available for retry by another consumer — balance timeout tolerance against stuck-message detection latency. **Duration:** Immediate; takes effect on the next message receive cycle. **Verification:** Step 6 maximum duration now sits below the configured `VisibilityTimeout`; Step 1 shows DLQ depth no longer growing.

### Cause B: Poison pill messages with malformed or incompatible payloads

**Statement:** One or more messages in the source queue have payloads that consistently trigger unhandled exceptions in the consumer, causing every processing attempt to fail until `maxReceiveCount` is exhausted and the messages move to the DLQ.

**Chain:**
- root: a message carries content the consumer cannot handle — malformed JSON, missing required fields, oversized body, unexpected schema version, or invalid data type.
- s1: the consumer throws an unhandled exception on every receive of that message, so `DeleteMessage` is never called.
- s2: SQS requeues the message after each visibility timeout, exhausting `maxReceiveCount` attempts; on FIFO queues this also blocks every message in the same `MessageGroupId`.
- D: after `maxReceiveCount` attempts the poison pill is permanently moved to the DLQ, growing DLQ depth (Symptom).

**Indicators:**
- root: [Step 2] DLQ messages share a common structural pattern in their body previews — missing fields, unusual encoding, consistent size outlier, or identical schema.
  <!-- match: {"step": 2, "predicate": "contains", "target": "Body preview"} -->
- s1: [Step 4] consumer logs show `JSONDecodeError`, `ValidationError`, `KeyError`, `NullPointerException`, or similar parsing/schema error for the same message IDs.
  <!-- match: {"step": 4, "predicate": "contains", "target": "JSONDecodeError"} -->
- s2: [Step 2] all DLQ messages have `ApproximateReceiveCount` equal to `maxReceiveCount` (fully exhausted retries).

**Interventions:**
- **remediation** (root): add schema validation in the consumer before business logic so invalid payloads are logged and explicitly deleted rather than retried; for Lambda event source mappings also enable `ReportBatchItemFailures`. Fix the producer so it stops generating invalid payloads.

  ```bash
  # Add schema validation in the consumer before business logic processing.
  # Python example (illustrative):
  #   def process_message(body: str) -> None:
  #       try:
  #           msg = MessageSchema().loads(body)  # raises ValidationError on bad input
  #       except (json.JSONDecodeError, ValidationError) as exc:
  #           logger.error("Poison pill", extra={"body": body[:500], "error": str(exc)})
  #           # Explicitly delete so the message does not exhaust maxReceiveCount
  #           sqs.delete_message(QueueUrl=SOURCE_Q, ReceiptHandle=receipt_handle)
  #           return
  #       _handle_valid_message(msg)
  #
  # For Lambda SQS event source mappings, also enable ReportBatchItemFailures:
  aws lambda update-event-source-mapping \
    --uuid "<event-source-mapping-uuid>" \
    --function-response-types ReportBatchItemFailures
  ```

  **Verification:** After deploying the consumer fix, Step 4 logs show `Poison pill` log entries with the offending message body rather than unhandled exceptions; Step 1 shows DLQ depth is no longer growing; and a test message with the previously-failing schema structure is explicitly deleted (not DLQ'd) by the updated consumer.
- **mitigation** (s2): log and delete the offending poison pill from the DLQ to clear the immediate buildup while the consumer fix is deployed.

  ```bash
  # Log the poison pill message body before deleting from DLQ
  aws sqs receive-message \
    --queue-url "https://sqs.us-east-1.amazonaws.com/123456789012/my-queue-dlq" \
    --max-number-of-messages 1 \
    --attribute-names All \
    --output json | tee /tmp/poison-pill-$(date +%s).json

  # Extract receipt handle and delete the message from DLQ
  RECEIPT=$(aws sqs receive-message \
    --queue-url "https://sqs.us-east-1.amazonaws.com/123456789012/my-queue-dlq" \
    --max-number-of-messages 1 \
    --query "Messages[0].ReceiptHandle" \
    --output text)
  aws sqs delete-message \
    --queue-url "https://sqs.us-east-1.amazonaws.com/123456789012/my-queue-dlq" \
    --receipt-handle "$RECEIPT"
  ```

  **Risk:** Manually deleting messages from the source queue or DLQ is destructive — the underlying producer bug will generate new poison pills until the producer is fixed, and deleting without logging message content discards forensic data. **Duration:** Stop-gap until the consumer adds input validation and explicit poison-pill handling, and the producer stops generating invalid payloads. **Verification:** the logged body is captured in `/tmp/poison-pill-*.json`; Step 1 shows the DLQ depth dropped by the deleted message and is not regrowing once the producer is fixed.

### Cause C: maxReceiveCount set too low for the transient failure rate

**Statement:** The source queue's `maxReceiveCount` is configured at 1 or 2, causing any single transient consumer failure — network blip, brief downstream timeout, cold start — to immediately route the message to the DLQ rather than retrying.

**Chain:**
- root: `maxReceiveCount` is set at 1, 2, or 3, leaving no retry headroom for expected transient failures.
- s1: a normal transient failure (Lambda cold start, brief connection drop, packet loss) trips the low ceiling on the first or second attempt.
- s2: the message is DLQ'd immediately with no further retry, even though the consumer is fundamentally healthy.
- D: steady DLQ growth accumulates from routine transient failures (Symptom).

**Indicators:**
- root: [Step 3] `maxReceiveCount` is 1, 2, or 3 in the redrive policy output.
  <!-- match: {"step": 3, "predicate": "threshold", "target": "maxReceiveCount", "op": "<", "value": 4} -->
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

**Statement:** A downstream dependency — database, HTTP API, or internal service — is unavailable or slow, causing every consumer processing attempt to fail and accumulate DLQ entries at the rate of `maxReceiveCount` retries per message.

**Chain:**
- root: a downstream dependency the consumer delegates to becomes unreachable or slow (DB down, API 5xx, network ACL blocking egress).
- s1: every consumer processing attempt fails, so `DeleteMessage` is never called and SQS requeues each message.
- s2: each message exhausts its `maxReceiveCount` retries during the outage window and is moved to the DLQ.
- D: DLQ depth grows in proportion to message volume arriving during the outage, bounding potential message loss if retention expires (Symptom).

**Indicators:**
- root: [Step 4] consumer logs show `ConnectionRefused`, `ServiceUnavailable`, `ReadTimeout`, `ConnectionTimeout`, or `HTTPError 5xx` errors for the downstream dependency.
  <!-- match: {"step": 4, "predicate": "contains", "target": "ConnectionRefused"} -->
- s1: [Step 1] DLQ growth rate tracks the source queue's incoming message rate exactly — every message being processed during the outage window ends up in the DLQ.
- s2: [Step 2] DLQ message receive counts are clustered at `maxReceiveCount` and DLQ depth correlates with the downstream outage window.

**Interventions:**
- **remediation** (root): once the downstream is confirmed healthy, re-enable the event source mapping and redrive the DLQ messages back to the source queue for reprocessing.

  ```bash
  # Re-enable the event source mapping after the downstream is healthy
  aws lambda update-event-source-mapping \
    --uuid "<event-source-mapping-uuid>" \
    --enabled true

  # Redrive any messages that already reached the DLQ back to the source queue
  aws sqs start-message-move-task \
    --source-arn "arn:aws:sqs:us-east-1:123456789012:my-queue-dlq"

  # Monitor redrive progress
  aws sqs list-message-move-tasks \
    --source-arn "arn:aws:sqs:us-east-1:123456789012:my-queue-dlq"
  ```

  **Verification:** Step 4 logs no longer show downstream errors; Step 1 DLQ depth is stable or decreasing after the event source mapping is re-enabled; `ApproximateNumberOfMessages` on the source queue drains to zero within the expected processing window.
- **mitigation** (s1): suspend consumer processing by disabling the Lambda event source mapping while the downstream is restored, so messages wait in the source queue instead of exhausting retries into the DLQ.

  ```bash
  # Pause consumer processing by temporarily suspending the Lambda event source mapping
  # while the downstream dependency is restored
  ESM_UUID=$(aws lambda list-event-source-mappings \
    --function-name my-queue-processor \
    --query "EventSourceMappings[?contains(EventSourceArn,'my-queue')].UUID" \
    --output text)

  aws lambda update-event-source-mapping \
    --uuid "$ESM_UUID" \
    --enabled false

  echo "Event source mapping suspended. Restore with: aws lambda update-event-source-mapping --uuid $ESM_UUID --enabled true"
  ```

  **Risk:** Increasing `maxReceiveCount` while the downstream is still down only delays the DLQ entry — messages accumulate in the source queue until `maxReceiveCount` is exhausted; the source queue's retention period bounds how long messages can wait for recovery. **Duration:** Hold until the downstream dependency is confirmed healthy; re-enable the event source mapping and monitor DLQ depth. **Verification:** Step 1 shows the DLQ stops growing while suspended and the source queue holds the backlog; re-enabling drains it cleanly.

### Cause E: Consumer does not call DeleteMessage after successful processing

**Statement:** The consumer successfully processes messages but never calls `DeleteMessage`, causing SQS to requeue every processed message at the end of each visibility timeout until `maxReceiveCount` is exhausted and the message moves to the DLQ.

**Chain:**
- root: the consumer completes processing but omits the `DeleteMessage` call (missing `finally`, cleanup-branch exception, or a misread Lambda event-source-mapping contract).
- s1: SQS never receives the success acknowledgement, so it treats the message as unprocessed and requeues it at the end of each visibility timeout.
- s2: the message is redelivered repeatedly until `maxReceiveCount` is exhausted; with `ReportBatchItemFailures` disabled, one failed item redelivers the entire Lambda batch.
- D: each phantom-redelivered message eventually moves to the DLQ, growing DLQ depth despite healthy processing (Symptom).

**Indicators:**
- root: [Step 5] `sqs:DeleteMessage` is `allowed` for the consumer role (ruling out an IAM cause).
- s1: [Step 1] source queue `NumberOfMessagesNotVisible` is consistently non-zero even when processing throughput appears normal — messages are in flight indefinitely.
- s2: [Step 4] consumer logs show successful processing log lines but no errors — the consumer appears healthy yet DLQ depth grows.
  <!-- match: {"step": 4, "predicate": "absent", "target": "Exception"} -->

**Interventions:**
- **remediation** (root): ensure `DeleteMessage` is called (or the Lambda returns successfully) for every processed message, and enable `ReportBatchItemFailures` so partial batch failures do not redeliver the whole batch.

  ```bash
  # Enable ReportBatchItemFailures to prevent the entire batch from being redelivered
  # when only a subset of items fail
  aws lambda update-event-source-mapping \
    --uuid "<event-source-mapping-uuid>" \
    --function-response-types ReportBatchItemFailures

  # For non-Lambda consumers: ensure DeleteMessage is called in a finally block
  # Python example (illustrative):
  #   try:
  #       process(message)
  #   except Exception as exc:
  #       logger.error("Processing failed", exc_info=exc)
  #       raise  # Re-raise so the message is not deleted and retries occur
  #   else:
  #       sqs.delete_message(QueueUrl=SOURCE_Q, ReceiptHandle=receipt_handle)
  ```

  **Verification:** Step 1's source queue `ApproximateNumberOfMessagesNotVisible` returns to near zero after processing each batch; DLQ depth stops growing; Step 4 logs show consistent successful processing without phantom redeliveries.
- **mitigation** (s1): inspect Lambda REPORT lines to confirm the function completes without error (so the issue is a missing delete, not a thrown exception) before deploying the code fix.

  ```bash
  # For Lambda: check REPORT lines to see if the function completes without errors
  aws logs filter-log-events \
    --log-group-name "/aws/lambda/my-queue-processor" \
    --filter-pattern "START END REPORT" \
    --start-time "$(date -u -d '30 minutes ago' +%s)000" \
    --limit 20 \
    --output text --query "events[*].message"
  ```

  **Risk:** Enabling `ReportBatchItemFailures` changes how Lambda handles partial batch failures — previously the entire batch would be retried on any single item failure, so enabling it may expose previously masked individual message failures. **Duration:** Diagnostic; if Lambda REPORT lines show `Billed Duration` with no corresponding error, the function is completing but may not be deleting messages correctly when using direct SDK calls rather than event source mapping auto-deletion. **Verification:** REPORT lines confirm clean completion, pointing to a missing `DeleteMessage` rather than a thrown exception.

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
