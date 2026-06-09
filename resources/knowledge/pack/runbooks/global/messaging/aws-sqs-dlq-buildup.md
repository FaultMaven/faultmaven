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
version: "1.0.0"
last_updated: "2026-05-12"
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

### Cause A: Visibility timeout is shorter than consumer processing time

**Statement:** The source queue's `VisibilityTimeout` is less than the time the consumer needs to process a message, so SQS makes the message visible again mid-processing, causing repeated redelivery until `maxReceiveCount` is exhausted.

**Mechanism:** When a consumer receives a message, SQS hides it from other consumers for the `VisibilityTimeout` duration. If the consumer does not call `DeleteMessage` before that window expires, SQS makes the message visible again and increments `ApproximateReceiveCount`. Each redelivery consumes one unit of `maxReceiveCount`. A consumer processing at p99 duration of 60 s against a 30 s visibility timeout will requeue every slow message; once those messages exceed `maxReceiveCount` they move to the DLQ even though the consumer eventually succeeds at each attempt.

**Indicator:**

- [Step 6] maximum observed Lambda duration exceeds the `VisibilityTimeout` value returned in Step 3
<!-- match: {"step": 6, "predicate": "threshold", "target": "VisibilityTimeout_seconds_vs_max_duration_seconds", "op": "<", "value": 1.0} -->
- [Step 3] `VisibilityTimeout` is 30 (the default) while any processing workload has non-trivial downstream I/O
- [Step 2] DLQ messages show `ApproximateReceiveCount` equal to `maxReceiveCount`; consumer logs show no error for the same message IDs (the consumer succeeded eventually, but too late)

**Mitigation:**

- **Risk:** Raising `VisibilityTimeout` extends the window before a genuinely stuck or crashed consumer's message becomes available for retry by another consumer — balance timeout tolerance against stuck-message detection latency.
- **Command:**

  ```bash
  # Set VisibilityTimeout to at least 6x the Lambda function timeout (or 6x observed p99 processing time)
  # Example: Lambda timeout=60s → VisibilityTimeout=360s
  aws sqs set-queue-attributes \
    --queue-url "https://sqs.us-east-1.amazonaws.com/123456789012/my-queue" \
    --attributes VisibilityTimeout=360
  ```

- **Duration:** Immediate; takes effect on the next message receive cycle.

**Resolution:**

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

### Cause B: Poison pill messages with malformed or incompatible payloads

**Statement:** One or more messages in the source queue have payloads that consistently trigger unhandled exceptions in the consumer, causing every processing attempt to fail until `maxReceiveCount` is exhausted and the messages move to the DLQ.

**Mechanism:** A poison pill is a message whose content — malformed JSON, missing required fields, an oversized body, an unexpected schema version, or an invalid data type — causes the consumer to throw an unhandled exception every time it receives the message. Because the exception prevents `DeleteMessage` from being called, SQS requeues the message after the visibility timeout. After `maxReceiveCount` attempts the message is permanently moved to the DLQ. For FIFO queues a single poison pill blocks all messages in the same `MessageGroupId`, halting that group entirely.

**Indicator:**

- [Step 2] DLQ messages share a common structural pattern in their body previews — missing fields, unusual encoding, consistent size outlier, or identical schema
<!-- match: {"step": 2, "predicate": "contains", "target": "Body preview"} -->
- [Step 4] consumer logs show `JSONDecodeError`, `ValidationError`, `KeyError`, `NullPointerException`, or similar parsing/schema error for the same message IDs
<!-- match: {"step": 4, "predicate": "contains", "target": "JSONDecodeError"} -->
- [Step 2] all DLQ messages have `ApproximateReceiveCount` equal to `maxReceiveCount` (fully exhausted retries)

**Mitigation:**

- **Risk:** Manually deleting messages from the source queue or DLQ is destructive — the underlying producer bug will generate new poison pills until the producer is fixed. Deleting without logging message content discards forensic data.
- **Command:**

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

- **Duration:** Stop-gap until the consumer adds input validation and explicit poison-pill handling. Fix the producer to stop generating invalid payloads.

**Resolution:**

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

### Cause C: `maxReceiveCount` set too low for the expected transient failure rate

**Statement:** The source queue's `maxReceiveCount` is configured at 1 or 2, causing any single transient consumer failure — network blip, brief downstream timeout, cold start — to immediately route the message to the DLQ rather than retrying.

**Mechanism:** `maxReceiveCount` defines how many total receive attempts SQS allows before a message is moved to the DLQ. With `maxReceiveCount=1`, the first consumer failure DLQs the message permanently regardless of whether the failure was transient. Transient failures — Lambda cold starts, brief downstream database connection drops, network packet loss — are expected at low frequency in distributed systems. A `maxReceiveCount` that does not provide sufficient retry headroom converts every transient failure into a DLQ entry, producing steady DLQ growth even when the consumer is fundamentally healthy.

**Indicator:**

- [Step 3] `maxReceiveCount` is 1, 2, or 3 in the redrive policy output
<!-- match: {"step": 3, "predicate": "threshold", "target": "maxReceiveCount", "op": "<", "value": 4} -->
- [Step 2] DLQ messages show mixed `ApproximateReceiveCount` values (some at 1, some at 2) rather than all at the `maxReceiveCount` ceiling — messages fail on the first attempt with no retry opportunity
- [Step 4] consumer logs show transient errors (connection timeouts, rate limit errors, brief exceptions) rather than persistent schema or logic failures

**Mitigation:**

- **Risk:** Raising `maxReceiveCount` allows genuinely broken messages more retry attempts before quarantine, which extends the time poison pills circulate in the source queue consuming consumer capacity. Balance retry tolerance against quarantine speed.
- **Command:**

  ```bash
  # Increase maxReceiveCount to 5-10 to tolerate transient failures
  SOURCE_Q="https://sqs.us-east-1.amazonaws.com/123456789012/my-queue"
  DLQ_ARN="arn:aws:sqs:us-east-1:123456789012:my-queue-dlq"

  aws sqs set-queue-attributes \
    --queue-url "$SOURCE_Q" \
    --attributes "{\"RedrivePolicy\": \"{\\\"deadLetterTargetArn\\\":\\\"${DLQ_ARN}\\\",\\\"maxReceiveCount\\\":\\\"10\\\"}\"}"
  ```

- **Duration:** Immediate; takes effect on the next message receive cycle.

**Resolution:** Same as Mitigation.

**Verification:** Step 3 shows the updated `maxReceiveCount` value; Step 1 shows DLQ growth rate decreasing after the change; Step 4 shows transient errors resolving on subsequent consumer attempts without messages reaching the DLQ.

### Cause D: Downstream dependency failure causes persistent consumer errors

**Statement:** A downstream dependency — database, HTTP API, or internal service — is unavailable or slow, causing every consumer processing attempt to fail and accumulate DLQ entries at the rate of `maxReceiveCount` retries per message.

**Mechanism:** SQS consumers are often thin adapters that receive a message and delegate to a downstream system. If that downstream system fails — a database goes unreachable, an API returns 5xx responses, a network ACL blocks egress — every consumer processing attempt fails. The consumer does not call `DeleteMessage`, so SQS requeues the message. After `maxReceiveCount` retries the message moves to the DLQ. DLQ accumulation in this pattern is proportional to the volume of messages arriving during the downstream outage, and the DLQ depth provides a lower bound on message loss if the `MessageRetentionPeriod` expires before the downstream recovers.

**Indicator:**

- [Step 4] consumer logs show `ConnectionRefused`, `ServiceUnavailable`, `ReadTimeout`, `ConnectionTimeout`, or `HTTPError 5xx` errors for the downstream dependency
<!-- match: {"step": 4, "predicate": "contains", "target": "ConnectionRefused"} -->
- [Step 2] DLQ message receive counts are clustered at `maxReceiveCount` and DLQ depth correlates with the downstream outage window
- [Step 1] DLQ growth rate tracks the source queue's incoming message rate exactly — every message being processed during the outage window ends up in the DLQ

**Mitigation:**

- **Risk:** Increasing `maxReceiveCount` while the downstream is still down only delays the DLQ entry — messages accumulate in the source queue until maxReceiveCount is exhausted. The source queue's retention period bounds how long messages can wait for recovery.
- **Command:**

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

- **Duration:** Hold until the downstream dependency is confirmed healthy; re-enable the event source mapping and monitor DLQ depth.

**Resolution:**

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

### Cause E: Consumer does not call `DeleteMessage` after successful processing

**Statement:** The consumer successfully processes messages but never calls `DeleteMessage`, causing SQS to requeue every processed message at the end of each visibility timeout until `maxReceiveCount` is exhausted and the message moves to the DLQ.

**Mechanism:** SQS uses explicit deletion as the success acknowledgement. A message is only permanently removed when `DeleteMessage` is called with the correct `ReceiptHandle`. If the consumer completes processing but a code path omits the delete call — an exception in the cleanup branch, a missing `finally` block, or a misunderstanding of the Lambda event source mapping contract — SQS treats the message as unprocessed. For Lambda event source mappings, Lambda automatically deletes messages when the function returns without error; if the function throws an exception (even a handled one logged internally), Lambda does not delete the batch. With `ReportBatchItemFailures` disabled, a single item failure in a batch causes the entire batch to be redelivered.

**Indicator:**

- [Step 4] consumer logs show successful processing log lines but no errors — the consumer appears healthy yet DLQ depth grows
<!-- match: {"step": 4, "predicate": "absent", "target": "Exception"} -->
- [Step 1] source queue `NumberOfMessagesNotVisible` is consistently non-zero even when processing throughput appears normal — messages are in flight indefinitely
- [Step 5] `sqs:DeleteMessage` is `allowed` for the consumer role (ruling out an IAM cause)

**Mitigation:**

- **Risk:** Enabling `ReportBatchItemFailures` changes how Lambda handles partial batch failures — previously the entire batch would be retried on any single item failure, so enabling it may expose previously masked individual message failures.
- **Command:**

  ```bash
  # For Lambda: check REPORT lines to see if the function completes without errors
  aws logs filter-log-events \
    --log-group-name "/aws/lambda/my-queue-processor" \
    --filter-pattern "START END REPORT" \
    --start-time "$(date -u -d '30 minutes ago' +%s)000" \
    --limit 20 \
    --output text --query "events[*].message"
  ```

- **Duration:** Diagnostic. If Lambda REPORT lines show `Billed Duration` with no corresponding error, the function is completing but may not be deleting messages correctly when using direct SDK calls rather than event source mapping auto-deletion.

**Resolution:**

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

### Cause Z: Unidentified

**Statement:** DLQ depth is confirmed growing by Step 1 but the indicators for Causes A through E did not match the evidence gathered.

**Mechanism:** DLQ buildup is real (Step 1 shows `ApproximateNumberOfMessagesVisible` increasing on the DLQ) but the per-step decomposition did not isolate the root cause to visibility timeout, poison pill content, low `maxReceiveCount`, downstream failures, or missing deletes. Less common origins include SQS service-side issues, cross-account redrive policy misconfigurations, Lambda concurrency limits causing messages to remain in flight and expire, `OverLimit` in-flight message quota reached (120,000 for Standard queues), VPC endpoint connectivity issues preventing consumers from reaching SQS, or encryption key access failures for SSE-KMS queues.

**Indicator:**

- [Default] Steps 1-5 confirmed DLQ depth is growing and Causes A-E indicators did not match the gathered evidence

**Mitigation:**

- **Risk:** Purging the DLQ permanently deletes all messages and discards forensic data; only purge after confirming messages are not recoverable and no longer needed.
- **Command:**

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

- **Duration:** Minutes. Collect, document findings, then escalate to AWS Support or the queue owner.

**Resolution:** Out of runbook scope. Attach the diagnostic bundle, the DLQ sample messages, the consumer application log excerpts, and the CloudWatch DLQ growth graph to an incident ticket. Escalate with the source queue ARN, DLQ ARN, the time window of onset, and the consumer runtime details (Lambda function name and version, or ECS task definition revision).

**Verification:** Hand-off acknowledged by the receiving engineer and an incident ticket opened with the captured artefacts attached, the DLQ growth timeline, and a follow-up owner assigned.

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
