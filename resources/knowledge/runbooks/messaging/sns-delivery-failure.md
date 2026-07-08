---
id: "sns-delivery-failure"
title: "AWS SNS Message Delivery Failures: Filtered Drops, Endpoint Errors, and DLQ Redrive"
domain: messaging
service: aws-sns
symptom_class: [data_loss, service_unavailable]
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-06-24"
verified_by: "kb-researcher"
status: draft
tags: [aws-sns, delivery-failure, filter-policy, dead-letter-queue, delivery-status-logging, http-endpoint]
difficulty: intermediate
---

## Symptom Recognition

- CloudWatch alarm on metric `NumberOfNotificationsFailed > 0` for an SNS topic.
- Subscribers report missing messages while `NumberOfMessagesPublished` is non-zero (silent drop).
- CloudWatch metric `NumberOfNotificationsFilteredOut` (or `NumberOfNotificationsFilteredOut-NoMessageAttributes` / `NumberOfNotificationsFilteredOut-InvalidAttributes`) is elevated.
- Delivery-status log entries in CloudWatch Logs show `"status":"FAILURE"` with `"providerResponse"` containing HTTP `4xx`/`5xx` codes.
- Dead-letter queue (DLQ) `ApproximateNumberOfMessagesVisible` is rising on the SQS DLQ attached to a subscription.
- CloudWatch metric `NumberOfNotificationsFailedToRedriveToDlq > 0` (messages could not even reach the DLQ).

## Applicability

- Amazon SNS standard or FIFO topics with HTTP/S, SQS, or Lambda subscriptions.
- AWS CLI v2 configured with credentials that have `sns:GetTopicAttributes`, `sns:GetSubscriptionAttributes`, `sns:SetTopicAttributes`, `sns:SetSubscriptionAttributes`, `cloudwatch:GetMetricStatistics`, and CloudWatch Logs read access.
- For delivery-status logging: an IAM role SNS can assume with `logs:CreateLogGroup`, `logs:CreateLogStream`, and `logs:PutLogEvents`.
- Tools: `aws` CLI, `jq` for inspecting JSON attributes.

## Diagnostic Steps

### Step 1: Quantify failed deliveries via CloudWatch

```bash
aws cloudwatch get-metric-statistics \
  --namespace AWS/SNS \
  --metric-name NumberOfNotificationsFailed \
  --dimensions Name=TopicName,Value=MyTopic \
  --start-time "$(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ)" \
  --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --period 300 --statistics Sum
```

Expected output: `Datapoints` with `Sum` values. A non-zero `Sum` confirms hard delivery failures (distinct from filtering).

### Step 2: Check for messages dropped by filter policies

```bash
aws cloudwatch get-metric-statistics \
  --namespace AWS/SNS \
  --metric-name NumberOfNotificationsFilteredOut \
  --dimensions Name=TopicName,Value=MyTopic \
  --start-time "$(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ)" \
  --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --period 300 --statistics Sum
```

Expected output: `Datapoints` with `Sum`. A non-zero `Sum` means a subscription's filter policy rejected messages (they were intentionally not delivered, no retry).

### Step 3: Inspect the subscription filter policy and DLQ config

```bash
aws sns get-subscription-attributes \
  --subscription-arn arn:aws:sns:us-east-2:123456789012:MyTopic:11111111-2222-3333-4444-555555555555 \
  | jq '.Attributes | {FilterPolicy, FilterPolicyScope, RedrivePolicy}'
```

Expected output: a JSON object showing `FilterPolicy` (the JSON match rules), `FilterPolicyScope` (`MessageAttributes` or `MessageBody`), and `RedrivePolicy` (`{"deadLetterTargetArn":"..."}` if a DLQ is set, else `null`).

### Step 4: Confirm delivery-status logging is enabled on the topic

```bash
aws sns get-topic-attributes \
  --topic-arn arn:aws:sns:us-east-2:123456789012:MyTopic \
  | jq '.Attributes | {HTTPSuccessFeedbackRoleArn, HTTPFailureFeedbackRoleArn, HTTPSuccessFeedbackSampleRate}'
```

Expected output: the feedback role ARNs. If `HTTPFailureFeedbackRoleArn` is `null`/absent, SNS is not logging failure reasons and you cannot see endpoint responses until it is configured.

### Step 5: Read the endpoint response from delivery-status logs

```bash
aws logs filter-log-events \
  --log-group-name "sns/us-east-2/123456789012/MyTopic/Failure" \
  --filter-pattern '{ $.status = "FAILURE" }' \
  --start-time "$(date -u -d '1 hour ago' +%s)000" \
  --limit 20
```

Expected output: log events whose message contains `"status":"FAILURE"` and a `"delivery"` object with `"providerResponse"` and `"statusCode"` (e.g. `500`, `403`, `429`) returned by the endpoint.

## Causes

### Cause A: Subscription filter policy rejects the published messages
**Statement:** A subscription's filter policy does not match the attributes (or body) of the published messages, so SNS silently drops every copy for that subscription without any retry or DLQ entry.
**Chain:**
- root: filter policy match rules diverge from the published message's attributes/body
- s1: SNS evaluates the policy, finds no match, and does not deliver a copy
- s2: `NumberOfNotificationsFilteredOut` increments while delivery counters stay flat
- D: subscriber never receives messages (silent data loss)
**Indicators:**
- s2: [Step 2] non-zero `Sum` on `NumberOfNotificationsFilteredOut` (or the `-NoMessageAttributes` / `-InvalidAttributes` variants)
- root: [Step 3] `FilterPolicy` rules whose keys/values do not match what publishers send, and `FilterPolicyScope` set unexpectedly to `MessageAttributes` while publishers send no attributes
**Interventions:**
- **remediation** (root): correct or remove the filter policy so it matches real traffic. To clear it entirely, set an empty policy.

  ```bash
  aws sns set-subscription-attributes \
    --subscription-arn arn:aws:sns:us-east-2:123456789012:MyTopic:11111111-2222-3333-4444-555555555555 \
    --attribute-name FilterPolicy \
    --attribute-value '{}'
  ```

  **Verification:** re-run Step 2; `NumberOfNotificationsFilteredOut` `Sum` returns to 0 for new messages and the subscriber begins receiving traffic.
- **defensive_fix** (s1): if filtering is intended on attributes but publishers send data in the body, switch scope to `MessageBody` so the policy evaluates the payload instead of dropping for missing attributes.

  ```bash
  aws sns set-subscription-attributes \
    --subscription-arn arn:aws:sns:us-east-2:123456789012:MyTopic:11111111-2222-3333-4444-555555555555 \
    --attribute-name FilterPolicyScope \
    --attribute-value MessageBody
  ```

  **Verification:** re-run Step 3 and confirm `FilterPolicyScope` is `MessageBody`; matching messages now deliver.

### Cause B: HTTP/S endpoint returns permanent (non-retryable) errors
**Statement:** The subscribed HTTP/S endpoint responds with a non-retryable status code (any 4xx other than 429, e.g. 403/404), so SNS treats delivery as a permanent failure and abandons it immediately with no retries.
**Chain:**
- root: endpoint returns a permanent-failure HTTP status (4xx except 429)
- s1: SNS classifies the response as non-retryable and skips the delivery retry policy
- s2: message is discarded (or sent to DLQ if one is attached) and `NumberOfNotificationsFailed` increments
- D: notification is lost / endpoint never processes it
**Indicators:**
- s2: [Step 1] non-zero `Sum` on `NumberOfNotificationsFailed`
- root: [Step 5] failure log event with a `"statusCode"` of 403/404/400 (not 429 or 5xx)
**Interventions:**
- **remediation** (root): fix the endpoint so it returns HTTP 200 and accepts the SNS message format (correct auth, route, and 200 on the confirmation/notification POST). This is an endpoint-side fix; redeploy the receiver, then publish a test message.

  ```bash
  aws sns publish \
    --topic-arn arn:aws:sns:us-east-2:123456789012:MyTopic \
    --message "delivery-test-$(date +%s)"
  ```

  **Verification:** re-run Step 5; the next event shows `"status":"SUCCESS"` and Step 1 `NumberOfNotificationsFailed` `Sum` stays flat.

### Cause C: HTTP/S endpoint returns retryable errors but the retry policy is exhausted
**Statement:** The endpoint returns retryable errors (5xx or 429) for longer than the subscription's delivery retry policy allows, so SNS exhausts its retries (default up to 50 attempts over ~6 hours for HTTP) and gives up.
**Chain:**
- root: endpoint stays unavailable (5xx/429) past the configured retry window
- s1: SNS retries per the delivery policy and each attempt fails
- s2: retry policy is exhausted; message is discarded unless a DLQ is attached, and `NumberOfNotificationsFailed` increments
- D: notification is lost after the retry window
**Indicators:**
- s2: [Step 1] sustained non-zero `NumberOfNotificationsFailed` over consecutive periods
- root: [Step 5] repeated failure events with `"statusCode"` 500/503/429 for the same subscription
**Interventions:**
- **defensive_fix** (root): attach an SQS dead-letter queue so messages survive retry exhaustion and can be redriven later.

  ```bash
  aws sns set-subscription-attributes \
    --subscription-arn arn:aws:sns:us-east-2:123456789012:MyTopic:11111111-2222-3333-4444-555555555555 \
    --attribute-name RedrivePolicy \
    --attribute-value '{"deadLetterTargetArn":"arn:aws:sqs:us-east-2:123456789012:MyDeadLetterQueue"}'
  ```

  **Verification:** re-run Step 3 and confirm `RedrivePolicy` is set; on the next failure, the SQS DLQ `ApproximateNumberOfMessagesVisible` increases instead of the message being lost.
- **mitigation** (s1): widen the HTTP/S delivery retry policy (e.g. more retries / longer backoff) to ride out short endpoint outages.

  ```bash
  aws sns set-subscription-attributes \
    --subscription-arn arn:aws:sns:us-east-2:123456789012:MyTopic:11111111-2222-3333-4444-555555555555 \
    --attribute-name DeliveryPolicy \
    --attribute-value '{"healthyRetryPolicy":{"numRetries":100,"minDelayTarget":5,"maxDelayTarget":120,"numMinDelayRetries":3,"numMaxDelayRetries":3,"backoffFunction":"exponential"}}'
  ```

  **Risk:** longer retries delay message handoff and increase dwell time; downstream may see stale data. **Duration:** keep only until the endpoint is stabilized (hours, not permanent). **Verification:** re-run Step 1; `NumberOfNotificationsFailed` `Sum` trends to 0 as the endpoint recovers.

### Cause D: DLQ is misconfigured so failed messages cannot be redriven
**Statement:** The subscription's RedrivePolicy points at an SQS DLQ in a different account/Region or whose queue policy denies `sqs:SendMessage` from SNS, so SNS cannot move failed messages to the DLQ and they are lost.
**Chain:**
- root: DLQ target is cross-account/cross-Region or its access policy blocks SNS `sqs:SendMessage`
- s1: a delivery fails and SNS attempts to redrive to the DLQ
- s2: the SendMessage to the DLQ is rejected; `NumberOfNotificationsFailedToRedriveToDlq` increments
- D: the failed message is dropped entirely (no DLQ safety net)
**Indicators:**
- s2: [Step 1] / [Step 3] non-zero `NumberOfNotificationsFailedToRedriveToDlq` while Step 3 shows a `RedrivePolicy` is configured
- root: [Step 3] `deadLetterTargetArn` ARN whose account ID or Region differs from the topic/subscription
**Interventions:**
- **remediation** (root): point RedrivePolicy at an SQS queue in the SAME account and Region, and grant the SQS queue policy permission for SNS to send to it.

  ```bash
  aws sqs set-queue-attributes \
    --queue-url https://sqs.us-east-2.amazonaws.com/123456789012/MyDeadLetterQueue \
    --attributes '{"Policy":"{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Service\":\"sns.amazonaws.com\"},\"Action\":\"sqs:SendMessage\",\"Resource\":\"arn:aws:sqs:us-east-2:123456789012:MyDeadLetterQueue\",\"Condition\":{\"ArnEquals\":{\"aws:SourceArn\":\"arn:aws:sns:us-east-2:123456789012:MyTopic\"}}}]}"}'
  ```

  **Verification:** re-run Step 1 metric `NumberOfNotificationsFailedToRedriveToDlq`; `Sum` returns to 0 and the SQS DLQ `ApproximateNumberOfMessagesVisible` rises on the next failure.

### Cause Z: Unidentified
**Statement:** Delivery failures persist but none of the above roots is confirmed by the diagnostics gathered.
**Indicators:**
- [Default]
**Interventions:**
- **mitigation** (D): capture a full diagnostic snapshot and escalate to the messaging/SRE SME.

  ```bash
  aws sns get-topic-attributes --topic-arn arn:aws:sns:us-east-2:123456789012:MyTopic > sns_topic_attrs.json
  aws sns list-subscriptions-by-topic --topic-arn arn:aws:sns:us-east-2:123456789012:MyTopic > sns_subs.json
  aws logs filter-log-events \
    --log-group-name "sns/us-east-2/123456789012/MyTopic/Failure" \
    --start-time "$(date -u -d '6 hours ago' +%s)000" --limit 200 > sns_failure_logs.json
  ```

  **Risk:** snapshot is read-only and safe. **Duration:** N/A. **Verification:** the three JSON files exist and are attached to the escalation ticket for SME review.

## Prevention

- Enable delivery-status logging on every topic so failure reasons are always captured. Create the feedback IAM role (with `logs:CreateLogGroup`, `logs:CreateLogStream`, `logs:PutLogEvents`) then attach it:

  ```bash
  aws sns set-topic-attributes \
    --topic-arn arn:aws:sns:us-east-2:123456789012:MyTopic \
    --attribute-name HTTPFailureFeedbackRoleArn \
    --attribute-value arn:aws:iam::123456789012:role/SNSFeedbackRole
  ```

- Attach a dead-letter queue to every important subscription so retry exhaustion never silently loses data (see Cause C `RedrivePolicy` command).
- Alarm on `NumberOfNotificationsFailed`, `NumberOfNotificationsFilteredOut`, and `NumberOfNotificationsFailedToRedriveToDlq`:

  ```bash
  aws cloudwatch put-metric-alarm \
    --alarm-name SNS-MyTopic-DeliveryFailures \
    --namespace AWS/SNS --metric-name NumberOfNotificationsFailed \
    --dimensions Name=TopicName,Value=MyTopic \
    --statistic Sum --period 300 --evaluation-periods 1 \
    --threshold 0 --comparison-operator GreaterThanThreshold \
    --treat-missing-data notBreaching \
    --alarm-actions arn:aws:sns:us-east-2:123456789012:OpsAlerts
  ```

- Alarm on DLQ depth with `ApproximateNumberOfMessagesVisible` (not `NumberOfMessagesSent`, which does not capture redriven failures).
- Validate filter policies in a staging subscription before applying to production; keep `FilterPolicyScope` aligned with where publishers put data (attributes vs body).

## Sources

- [Sns troubleshooting](https://docs.aws.amazon.com/sns/latest/dg/sns-troubleshooting.html) — primary troubleshooting guide (requested authoritative source).
- [Sns monitoring using cloudwatch](https://docs.aws.amazon.com/sns/latest/dg/sns-monitoring-using-cloudwatch.html) — metric names: `NumberOfNotificationsFailed`, `NumberOfNotificationsFilteredOut(-NoMessageAttributes/-InvalidAttributes)`, `NumberOfNotificationsFailedToRedriveToDlq`.
- [Sns topic attributes](https://docs.aws.amazon.com/sns/latest/dg/sns-topic-attributes.html) — delivery-status logging topic attributes and `set-topic-attributes` feedback-role example.
- [Topics attrib prereq](https://docs.aws.amazon.com/sns/latest/dg/topics-attrib-prereq.html) — IAM permissions (`logs:CreateLogGroup`/`CreateLogStream`/`PutLogEvents`) for delivery-status logging.
- [Sns subscription filter policies](https://docs.aws.amazon.com/sns/latest/dg/sns-subscription-filter-policies.html) — filter-policy match/drop behavior.
- [Sns message filtering scope](https://docs.aws.amazon.com/sns/latest/dg/sns-message-filtering-scope.html) — `FilterPolicyScope` MessageAttributes vs MessageBody.
- [Sns message delivery retries](https://docs.aws.amazon.com/sns/latest/dg/sns-message-delivery-retries.html) — 5xx/429 retryable, other errors permanent; HTTP retry policy up to 50 times over 6 hours.
- [Sns dead letter queues](https://docs.aws.amazon.com/sns/latest/dg/sns-dead-letter-queues.html) — DLQ concept and CloudWatch metrics.
- [Sns configure dead letter queue](https://docs.aws.amazon.com/sns/latest/dg/sns-configure-dead-letter-queue.html) — `RedrivePolicy` with `deadLetterTargetArn`; same-account/Region requirement.
