---
id: aws-sqs-dlq-buildup
title: "AWS SQS Dead Letter Queue Buildup: Visibility Timeout, Max Receive Count, and Poison Pills"
domain: messaging
service: aws-sqs
symptom_class:
  - data-loss
  - throughput-degradation
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-03-26"
verified_by: kb-researcher
status: draft
tags:
  - aws
  - sqs
  - dlq
  - dead-letter
  - visibility-timeout
  - poison-pill
difficulty: intermediate
---

# AWS SQS Dead Letter Queue Buildup

## Problem Definition

Applies to AWS SQS Standard and FIFO queues in any region. Requires `sqs:*` and `cloudwatch:GetMetricStatistics` IAM permissions. AWS CLI v2 and Python 3.8+ required for diagnostic commands.

An AWS SQS Dead Letter Queue (DLQ) buildup occurs when messages are repeatedly received from the source queue but not successfully deleted by consumers. After exceeding the `maxReceiveCount` configured in the source queue's redrive policy, SQS automatically moves messages to the designated DLQ. A growing DLQ indicates that consumers are consistently failing to process certain messages.

**Symptoms and errors:**

- `ApproximateNumberOfMessagesVisible` on the DLQ increases steadily in CloudWatch
- Source queue `NumberOfMessagesReceived` is high but `NumberOfMessagesDeleted` lags behind
- Consumer logs show repeated exceptions or timeouts for the same message IDs
- CloudWatch alarm on `ApproximateNumberOfMessagesVisible > 0` fires for the DLQ
- For Lambda triggers: `IteratorAge` increases or invocation errors spike
- Messages silently expire from the DLQ when `MessageRetentionPeriod` is reached (default 4 days)
- For FIFO queues: a single poison pill blocks the entire message group

**Common causes:**

- Consumer application bugs causing unhandled exceptions on specific message formats
- Poison pill messages with malformed, oversized, or incompatible payloads that consistently crash the consumer
- Visibility timeout shorter than consumer processing time, causing repeated redelivery and eventual DLQ routing
- Downstream dependency failures (database unavailable, API timeout) causing consumer failures
- `maxReceiveCount` set too low (e.g., 1) so that a single transient failure DLQs the message immediately
- Consumer not calling `DeleteMessage` after successful processing
- IAM permission issues preventing the consumer from deleting messages

## Diagnostic Steps

### Step 1: Check DLQ Message Count and Growth Rate

Determines the current DLQ depth and whether it is actively growing. A non-zero and increasing count confirms the buildup.

```bash
# Get current approximate message count in the DLQ
aws sqs get-queue-attributes \
  --queue-url https://sqs.us-east-1.amazonaws.com/123456789012/my-queue-dlq \
  --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible

# Monitor growth rate over the past hour via CloudWatch
aws cloudwatch get-metric-statistics \
  --namespace AWS/SQS \
  --metric-name ApproximateNumberOfMessagesVisible \
  --dimensions Name=QueueName,Value=my-queue-dlq \
  --start-time $(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%S) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%S) \
  --period 300 \
  --statistics Sum
```

**Expected output:** `ApproximateNumberOfMessages` should be 0 during normal operation. A rising trend across 5-minute intervals confirms active DLQ buildup rather than a one-time batch of failures.

**What this means:** If the count is stable (not growing), the root cause may already be resolved and only historical messages remain. If actively growing, the consumer is currently failing.

### Step 2: Inspect DLQ Messages for Patterns

Peeks at sample messages to identify whether specific payload formats, attributes, or senders correlate with failures.

```bash
# Receive and inspect sample messages (non-destructive peek)
aws sqs receive-message \
  --queue-url https://sqs.us-east-1.amazonaws.com/123456789012/my-queue-dlq \
  --max-number-of-messages 5 \
  --attribute-names All \
  --message-attribute-names All | python3 -m json.tool

# Check receive counts to distinguish transient vs persistent failures
aws sqs receive-message \
  --queue-url https://sqs.us-east-1.amazonaws.com/123456789012/my-queue-dlq \
  --attribute-names ApproximateReceiveCount \
  --max-number-of-messages 10 | python3 -c "
import sys, json
data = json.load(sys.stdin)
for msg in data.get('Messages', []):
    count = msg.get('Attributes', {}).get('ApproximateReceiveCount', 'N/A')
    body = msg.get('Body', '')[:100]
    print(f'ReceiveCount: {count}, Body preview: {body}')
"
```

**Expected output:** Messages with `ApproximateReceiveCount` equal to the `maxReceiveCount` threshold. Look for common patterns in message bodies (e.g., all contain a specific field format, are from a particular producer, or exceed expected size).

**What this means:** If all DLQ messages share a structural pattern (same schema violation, same producer), it is a poison pill issue. If receive counts vary widely, the problem is likely transient downstream failures rather than message content.

### Step 3: Check Source Queue Redrive Policy and Visibility Timeout

Retrieves the `maxReceiveCount` and `VisibilityTimeout` configuration that governs when messages move to the DLQ.

```bash
# Get the redrive policy from the source queue
aws sqs get-queue-attributes \
  --queue-url https://sqs.us-east-1.amazonaws.com/123456789012/my-queue \
  --attribute-names RedrivePolicy VisibilityTimeout
```

**Expected output:** `RedrivePolicy` shows `maxReceiveCount` (recommended 5-10) and `deadLetterTargetArn` pointing to the DLQ. `VisibilityTimeout` should be at least 6x the average consumer processing time.

**What this means:** `maxReceiveCount=1` means any single transient failure sends the message to the DLQ immediately. A `VisibilityTimeout` of 30 seconds with a consumer that takes 60 seconds to process causes every message to be received twice and eventually DLQ'd.

### Step 4: Check Consumer Application Logs

Identifies the specific errors consumers encounter when processing messages that end up in the DLQ.

```bash
# Search for processing errors in Lambda consumer logs
aws logs filter-log-events \
  --log-group-name /aws/lambda/my-queue-processor \
  --filter-pattern "?ERROR ?Exception ?Timeout ?failed" \
  --start-time $(date -u -d '1 hour ago' +%s000) \
  --limit 20

# For ECS/EC2-based consumers
aws logs filter-log-events \
  --log-group-name /ecs/my-consumer-service \
  --filter-pattern "?Exception ?error ?failed ?timeout" \
  --start-time $(date -u -d '1 hour ago' +%s000) \
  --limit 20
```

**Expected output:** Error messages indicating the failure reason (e.g., `ValidationError`, `ConnectionRefused`, `TimeoutError`, `JSONDecodeError`).

**What this means:** `JSONDecodeError` or `ValidationError` points to poison pill messages. `ConnectionRefused` or `TimeoutError` points to downstream dependency failures. No errors at all suggests the consumer is not deleting messages after successful processing.

### Step 5: Verify Consumer IAM Permissions

Checks whether the consumer has the required permissions to receive, process, and delete messages.

```bash
# Simulate consumer IAM permissions
aws iam simulate-principal-policy \
  --policy-source-arn arn:aws:iam::123456789012:role/my-consumer-role \
  --action-names sqs:ReceiveMessage sqs:DeleteMessage sqs:ChangeMessageVisibility \
  --resource-arns arn:aws:sqs:us-east-1:123456789012:my-queue

# Verify the role is attached to the compute resource
aws lambda get-function-configuration --function-name my-queue-processor \
  --query "Role" --output text
```

**Expected output:** All three actions should show `EvalDecision: allowed`. The role ARN should match the consumer's execution role.

**What this means:** If `sqs:DeleteMessage` is denied, the consumer receives and processes messages but cannot delete them, causing indefinite redelivery until `maxReceiveCount` is hit.

### Step 6: Compare Visibility Timeout to Processing Duration

Determines whether the visibility timeout gives the consumer enough time to finish processing before the message becomes visible again.

```bash
# Check Lambda duration metrics (p99 is critical)
aws cloudwatch get-metric-statistics \
  --namespace AWS/Lambda \
  --metric-name Duration \
  --dimensions Name=FunctionName,Value=my-queue-processor \
  --start-time $(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%S) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%S) \
  --period 300 \
  --statistics Average Maximum p99
```

**Expected output:** The `VisibilityTimeout` (from Step 3) must exceed the p99 processing duration. AWS recommends the visibility timeout be at least 6x the Lambda function timeout for Lambda-triggered queues.

**What this means:** If p99 duration is 45 seconds and visibility timeout is 30 seconds, the slowest 1% of messages will always be redelivered and eventually DLQ'd. This creates a steady trickle into the DLQ that correlates with message complexity or size.

## Mitigation

### Option 1: Redrive Messages from DLQ Back to Source Queue

**Risk:** Medium. Messages that previously failed will fail again if the root cause is not fixed. Only redrive after the consumer fix is deployed. Messages are moved, not copied.

**Command:**

```bash
# Start a DLQ redrive task
aws sqs start-message-move-task \
  --source-arn arn:aws:sqs:us-east-1:123456789012:my-queue-dlq

# Check redrive task status
aws sqs list-message-move-tasks \
  --source-arn arn:aws:sqs:us-east-1:123456789012:my-queue-dlq
```

**Verify:**

```bash
# DLQ count should decrease, source queue count should increase
aws sqs get-queue-attributes \
  --queue-url https://sqs.us-east-1.amazonaws.com/123456789012/my-queue-dlq \
  --attribute-names ApproximateNumberOfMessages
aws sqs get-queue-attributes \
  --queue-url https://sqs.us-east-1.amazonaws.com/123456789012/my-queue \
  --attribute-names ApproximateNumberOfMessages
```

**Duration:** Minutes to hours depending on DLQ depth. SQS redrives at approximately 500 messages/second.

### Option 2: Increase Visibility Timeout

**Risk:** Low. Gives consumers more time to process messages before redelivery. Only downside is increased latency for message retry if a consumer crashes mid-processing.

**Command:**

```bash
# Increase visibility timeout on the source queue
aws sqs set-queue-attributes \
  --queue-url https://sqs.us-east-1.amazonaws.com/123456789012/my-queue \
  --attributes VisibilityTimeout=300

# For Lambda triggers, also increase the Lambda timeout
aws lambda update-function-configuration \
  --function-name my-queue-processor \
  --timeout 300
```

**Verify:**

```bash
aws sqs get-queue-attributes \
  --queue-url https://sqs.us-east-1.amazonaws.com/123456789012/my-queue \
  --attribute-names VisibilityTimeout
# Monitor that DLQ growth stops within 15-30 minutes
```

**Duration:** Immediate. Effect seen within one visibility timeout cycle.

### Option 3: Increase maxReceiveCount

**Risk:** Low. Allows more retry attempts before DLQ routing. Useful for transient failures but does not fix persistent ones.

**Command:**

```bash
aws sqs set-queue-attributes \
  --queue-url https://sqs.us-east-1.amazonaws.com/123456789012/my-queue \
  --attributes '{
    "RedrivePolicy": "{\"deadLetterTargetArn\":\"arn:aws:sqs:us-east-1:123456789012:my-queue-dlq\",\"maxReceiveCount\":\"10\"}"
  }'
```

**Verify:**

```bash
aws sqs get-queue-attributes \
  --queue-url https://sqs.us-east-1.amazonaws.com/123456789012/my-queue \
  --attribute-names RedrivePolicy
```

**Duration:** Immediate.

### Option 4: Purge DLQ After Investigation

**Risk:** High. Permanently deletes all messages in the DLQ. Only use after confirming messages are not recoverable or no longer needed.

**Command:**

```bash
# CAUTION: Permanently deletes all messages
aws sqs purge-queue \
  --queue-url https://sqs.us-east-1.amazonaws.com/123456789012/my-queue-dlq
# PurgeQueue can only be called once every 60 seconds
```

**Verify:**

```bash
aws sqs get-queue-attributes \
  --queue-url https://sqs.us-east-1.amazonaws.com/123456789012/my-queue-dlq \
  --attribute-names ApproximateNumberOfMessages
# May take up to 60 seconds for count to reflect
```

**Duration:** Immediate.

## Root Cause Resolution

**If** visibility timeout is shorter than processing time → Set `VisibilityTimeout` to at least 6x the average processing duration. For Lambda-triggered queues, the visibility timeout must be at least 6x the Lambda function timeout. Implement `ChangeMessageVisibility` in long-running consumers to extend the timeout dynamically during processing.

**If** poison pill messages consistently crash the consumer → Add input validation and error handling in the consumer to catch malformed messages. Log the message body and attributes, then explicitly delete the message to prevent retry. Route unprocessable messages to a separate investigation queue rather than the DLQ.

**If** downstream dependency failures cause consumer errors → Implement circuit breakers with exponential backoff in the consumer for downstream calls. Increase `maxReceiveCount` to 5-10 to tolerate transient failures. Consider SQS delay queues or message timers for backoff between retries.

**If** `maxReceiveCount` is too low → Increase to 5-10 for most workloads. A `maxReceiveCount=1` means any transient failure immediately DLQs the message. Balance retry tolerance against how quickly truly unprocessable messages should be quarantined.

**If** the consumer is not deleting messages after processing → Verify the consumer code explicitly calls `DeleteMessage` after successful processing. For Lambda triggers, ensure the function returns successfully (does not throw) because SQS Lambda integration automatically deletes messages on successful invocation. For batch processing with Lambda, enable `ReportBatchItemFailures` to delete successful items individually.

**If** IAM permissions prevent message deletion → Add `sqs:DeleteMessage` and `sqs:ChangeMessageVisibility` permissions to the consumer's IAM role. For Lambda triggers, the execution role needs `sqs:ReceiveMessage`, `sqs:DeleteMessage`, and `sqs:GetQueueAttributes` on the source queue ARN.

## Verification

After applying fixes, confirm the system is healthy:

```bash
# 1. DLQ message count is stable or decreasing (no new messages entering)
aws sqs get-queue-attributes \
  --queue-url https://sqs.us-east-1.amazonaws.com/123456789012/my-queue-dlq \
  --attribute-names ApproximateNumberOfMessages

# 2. Source queue is being processed normally (messages flowing through)
aws sqs get-queue-attributes \
  --queue-url https://sqs.us-east-1.amazonaws.com/123456789012/my-queue \
  --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible

# 3. Consumer logs show no errors in the last 15 minutes
aws logs filter-log-events \
  --log-group-name /aws/lambda/my-queue-processor \
  --filter-pattern "ERROR" \
  --start-time $(date -u -d '15 minutes ago' +%s000) \
  --limit 5

# 4. CloudWatch DLQ alarm is not in ALARM state
aws cloudwatch describe-alarms \
  --alarm-names my-queue-dlq-alarm \
  --state-value ALARM

# 5. Send a test message and verify end-to-end processing
aws sqs send-message \
  --queue-url https://sqs.us-east-1.amazonaws.com/123456789012/my-queue \
  --message-body '{"test": true, "timestamp": "'$(date -u +%Y-%m-%dT%H:%M:%SZ)'"}'
# Wait 30 seconds, then confirm the message was processed (not in source queue or DLQ)
```

## Prevention

- **Set `maxReceiveCount` to 5-10** to provide tolerance for transient failures while quarantining persistent ones
- **Set `VisibilityTimeout` to 6x average processing time** to prevent premature redelivery
- **Create a CloudWatch alarm on DLQ `ApproximateNumberOfMessagesVisible > 0`** because any message in the DLQ warrants investigation
- **Set DLQ retention period to 14 days (maximum)** to provide adequate time for investigation and redrive before silent data loss
- **Enable `ReportBatchItemFailures`** for Lambda SQS triggers to avoid reprocessing successful items in a batch
- **Add input validation** in consumers with explicit error handling for malformed messages — delete poison pills after logging
- **Use `ChangeMessageVisibility`** in long-running consumers to extend timeout dynamically rather than setting a very high static timeout
- **Monitor `NumberOfMessagesSent` on the DLQ** in CloudWatch — a sustained non-zero rate indicates ongoing consumer failures
- **Monitor `ApproximateAgeOfOldestMessage` on the source queue** — growing age indicates processing delays that may precede DLQ buildup
- **Implement structured error logging** in consumers that includes the SQS message ID, receipt handle, and failure reason for correlation during incidents
- **For FIFO queues**, be aware that a single poison pill blocks the entire message group — implement per-message error handling to avoid group-level stalls
- **Automate DLQ redrive** with a scheduled Lambda function that checks DLQ depth and redrives when the consumer is healthy

## Sources

- [AWS SQS Developer Guide — Dead-Letter Queues](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-dead-letter-queues.html)
- [AWS SQS Developer Guide — Visibility Timeout](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-visibility-timeout.html)
- [AWS SQS Developer Guide — Message Move Tasks (DLQ Redrive)](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-configure-dead-letter-queue-redrive.html)
- [AWS Lambda Developer Guide — Using Lambda with SQS](https://docs.aws.amazon.com/lambda/latest/dg/with-sqs.html)
- [AWS SQS API Reference — SetQueueAttributes](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/APIReference/API_SetQueueAttributes.html)
