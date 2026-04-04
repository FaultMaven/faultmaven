---
id: aws-secrets-manager-throttling
title: "AWS Secrets Manager Throttling"
domain: security
service: aws-secrets-manager
symptom_class:
  - timeout
  - service_unavailable
severity: medium
scope: global
version: "1.0.0"
last_updated: "2026-03-26"
verified_by: kb-researcher
status: draft
tags:
  - aws
  - secrets-manager
  - throttling
  - rate-limit
  - caching
  - sdk
difficulty: intermediate
---

# AWS Secrets Manager Throttling

## Problem Definition

Applies to all AWS accounts using Secrets Manager for secret retrieval. Requires `secretsmanager:GetSecretValue` and `secretsmanager:DescribeSecret` permissions, plus CloudTrail and CloudWatch access for diagnostics. Affects all AWS SDK versions and CLI v2+.

Secrets Manager throttling occurs when API call rates exceed the per-account, per-region service limits. The caller receives a `ThrottlingException` or `RateExceededException`:

```
An error occurred (ThrottlingException) when calling the GetSecretValue operation:
Rate exceeded
```

```
botocore.exceptions.ClientError: An error occurred (ThrottlingException) when calling
the GetSecretValue operation (reached max retries: 4): Rate exceeded
```

Default API rate limits (per account, per region):

- **GetSecretValue**: 10,000 requests per second (sustained), burst to 10,000.
- **DescribeSecret**: 2,000 requests per second.
- **PutSecretValue / CreateSecret / UpdateSecret**: 50 requests per second.
- **RotateSecret**: 50 requests per second.

Throttling typically occurs because:

- **No client-side caching** — every function invocation or container startup calls GetSecretValue directly instead of caching the result.
- **Lambda cold starts at scale** — hundreds of concurrent Lambda invocations each fetch secrets on startup.
- **Microservice fleet restarts** — a rolling deployment or autoscaling event causes many pods/containers to fetch secrets simultaneously.
- **Secret rotation storms** — multiple secrets rotating in the same window trigger bursts of read and write operations.
- **Retry amplification** — SDK retries with insufficient backoff multiply the request rate during an existing throttle event.

## Diagnostic Steps

### Step 1. Confirm throttling in CloudTrail

Searches CloudTrail for Secrets Manager API calls that returned throttling errors, identifying which operations and which principals are being throttled.

```bash
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=EventSource,AttributeValue=secretsmanager.amazonaws.com \
  --start-time "$(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ)" \
  --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --query 'Events[?contains(CloudTrailEvent, `ThrottlingException`)].{Time:EventTime,Event:CloudTrailEvent}' \
  --output json
```

Expected output shows events with `errorCode: ThrottlingException`. The `userIdentity.arn` field identifies which principal is generating the most calls. If no events appear, the throttling may be occurring at the SDK level before reaching CloudTrail.

### Step 2. Check current API call rate via CloudWatch

Measures the actual API call rate to determine how close the account is to the service limit and which operations are hottest.

```bash
aws cloudwatch get-metric-statistics \
  --namespace AWS/SecretsManager \
  --metric-name APICallCount \
  --start-time "$(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ)" \
  --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --period 60 \
  --statistics Sum \
  --dimensions Name=Operation,Value=GetSecretValue
```

Expected output shows the Sum of API calls per minute. Divide by 60 to get requests per second. If the value approaches or exceeds the service limit, throttling is expected.

### Step 3. Identify the top callers

Determines which IAM principals and source IPs are generating the most Secrets Manager API calls, helping pinpoint the service or workload responsible.

```bash
# Use CloudTrail Insights or Athena for detailed breakdown
# SELECT useridentity.arn, COUNT(*) as call_count
# FROM cloudtrail_logs
# WHERE eventsource = 'secretsmanager.amazonaws.com'
#   AND eventname = 'GetSecretValue'
#   AND eventtime > '2026-03-26T00:00:00Z'
# GROUP BY useridentity.arn
# ORDER BY call_count DESC
# LIMIT 20;
```

The output identifies which roles/users are making the most calls. A single role with disproportionately high call counts indicates a caching issue in that workload.

### Step 4. Check SDK retry configuration

Examines whether the application's AWS SDK retry settings are amplifying the throttle. Default retry counts vary by SDK.

```bash
# Python (boto3) — check environment or code for retry config
# boto3 defaults: 5 retries with exponential backoff for standard mode
# Check for BOTO_MAX_RETRIES or AWS_MAX_ATTEMPTS environment variable
env | grep -i 'retry\|max_attempt'
```

If `AWS_MAX_ATTEMPTS` is set very high or retry mode is `legacy` (no exponential backoff), retries amplify the throttle. The `adaptive` retry mode is recommended for throttle-sensitive workloads.

### Step 5. Check for caching in the application

Determines whether the application is using the Secrets Manager caching library or implementing its own cache. Without caching, every invocation makes a network call.

```bash
# For Python applications, check if the caching library is installed
pip show aws-secretsmanager-caching 2>/dev/null || echo "Caching library NOT installed"

# For Java, check Maven dependencies
# grep -r 'aws-secretsmanager-caching' pom.xml build.gradle 2>/dev/null
```

If the caching library is not present and the application code calls `get_secret_value()` directly on every request, this is the root cause.

## Mitigation

### Option 1: Enable SDK adaptive retry mode

**Risk**: Adaptive retry mode adds latency to retried calls (exponential backoff with jitter). This is intentional and preferred over failing fast during transient throttles.

**Command**:

```bash
# Set environment variable for all AWS SDK calls in the process
export AWS_RETRY_MODE=adaptive
export AWS_MAX_ATTEMPTS=5
```

**Verify**: Restart the application and monitor CloudWatch for reduced ThrottlingException counts.

**Duration**: Permanent — adaptive retry mode is the recommended default.

### Option 2: Request a service quota increase

**Risk**: Higher quotas allow more API calls but do not address the underlying inefficiency. The account will eventually hit the new limit if caching is not implemented.

**Command**:

```bash
aws service-quotas request-service-quota-increase \
  --service-code secretsmanager \
  --quota-code L-2F66A066 \
  --desired-value 20000
```

**Verify**:

```bash
aws service-quotas get-requested-service-quota-change \
  --request-id <request-id-from-above> \
  --query 'RequestedQuota.{Status:Status,DesiredValue:DesiredValue}'
```

**Duration**: Quota increases are permanent. AWS may take 1-3 business days to approve.

### Option 3: Stagger application restarts

**Risk**: Slower rollout increases deployment time. Acceptable for non-urgent deployments.

**Command**:

```bash
# For Kubernetes deployments, set maxSurge and maxUnavailable to limit concurrent pod starts
kubectl patch deployment my-app -p '{"spec":{"strategy":{"rollingUpdate":{"maxSurge":"25%","maxUnavailable":"10%"}}}}'
```

**Verify**: Monitor GetSecretValue call rate during deployment and confirm it stays below the service limit.

**Duration**: Permanent deployment configuration change.

## Root Cause Resolution

**If** the application has no client-side caching → implement the AWS Secrets Manager caching library:

Python:

```bash
pip install aws-secretsmanager-caching
```

```python
from aws_secretsmanager_caching import SecretCache, SecretCacheConfig
from botocore.session import Session

cache_config = SecretCacheConfig(
    max_cache_size=1000,
    secret_refresh_interval=3600  # seconds
)
cache = SecretCache(config=cache_config, client=Session().create_client('secretsmanager'))

# Use cache.get_secret_string() instead of client.get_secret_value()
secret_value = cache.get_secret_string('my-secret')
```

**If** Lambda functions fetch secrets on every cold start → use the Lambda Extensions caching layer:

```bash
# Add the AWS Parameters and Secrets Lambda Extension layer
aws lambda update-function-configuration \
  --function-name my-function \
  --layers arn:aws:lambda:us-east-1:177933569100:layer:AWS-Parameters-and-Secrets-Lambda-Extension:11
```

The extension caches secrets locally and serves them from `localhost:2773`, eliminating cold-start API calls to Secrets Manager.

**If** secret rotation storms cause burst throttling → stagger rotation schedules across secrets:

```bash
# Set different rotation schedules to avoid all secrets rotating at the same time
aws secretsmanager rotate-secret \
  --secret-id my-secret-1 \
  --rotation-rules '{"ScheduleExpression":"rate(30 days)","Duration":"2h"}'

aws secretsmanager rotate-secret \
  --secret-id my-secret-2 \
  --rotation-rules '{"ScheduleExpression":"cron(0 12 15 * ? *)","Duration":"2h"}'
```

**If** SDK retry amplification is worsening throttles → configure adaptive retry mode in the SDK configuration:

```python
import boto3
from botocore.config import Config

config = Config(
    retries={'mode': 'adaptive', 'max_attempts': 5}
)
client = boto3.client('secretsmanager', config=config)
```

**If** a single workload dominates the API call rate → consider storing the secret in AWS Systems Manager Parameter Store (SecureString) for high-throughput read scenarios, which has higher default rate limits (10,000 TPS for GetParameter).

## Verification

1. Monitor the ThrottlingException count in CloudWatch after applying the fix:

```bash
aws cloudwatch get-metric-statistics \
  --namespace AWS/SecretsManager \
  --metric-name APICallCount \
  --start-time "$(date -u -d '30 minutes ago' +%Y-%m-%dT%H:%M:%SZ)" \
  --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --period 60 \
  --statistics Sum \
  --dimensions Name=Operation,Value=GetSecretValue
```

The API call rate should drop significantly after caching is implemented. Expect a 90%+ reduction for workloads that previously fetched on every request.

2. Confirm no ThrottlingException errors in application logs:

```bash
# For CloudWatch Logs
aws logs filter-log-events \
  --log-group-name /aws/lambda/my-function \
  --start-time "$(date -u -d '30 minutes ago' +%s)000" \
  --filter-pattern "ThrottlingException"
```

Expected output: no matching events.

3. Verify the application retrieves secrets successfully under normal load by running a health check or smoke test.

## Prevention

1. **Always use the Secrets Manager caching library** in production applications. Set `secret_refresh_interval` to match your rotation schedule (typically 1-24 hours).

2. **Use the Lambda Parameters and Secrets Extension** for all Lambda functions that access Secrets Manager. It provides transparent caching without code changes.

3. **Stagger deployment rollouts** to avoid thundering-herd secret fetches. Configure Kubernetes `maxSurge` or ECS `minimumHealthyPercent` to limit concurrent startups.

4. **Set up CloudWatch alarms on Secrets Manager throttling**:

```bash
aws cloudwatch put-metric-alarm \
  --alarm-name SecretsManagerThrottling \
  --metric-name ThrottleCount \
  --namespace AWS/SecretsManager \
  --statistic Sum \
  --period 300 \
  --threshold 10 \
  --comparison-operator GreaterThanThreshold \
  --evaluation-periods 1 \
  --alarm-actions arn:aws:sns:us-east-1:123456789012:ops-alerts
```

5. **Distribute secrets across regions** if the workload is multi-region, to avoid concentrating API calls in a single region's quota.

6. **Use AWS Config rules** to detect applications deployed without caching libraries in their dependency manifests.

## Sources

- [Quotas for AWS Secrets Manager - AWS Secrets Manager User Guide](https://docs.aws.amazon.com/secretsmanager/latest/userguide/reference_limits.html)
- [Reduce Secrets Manager API calls with client-side caching - AWS Secrets Manager User Guide](https://docs.aws.amazon.com/secretsmanager/latest/userguide/retrieving-secrets_cache-ref-implguide.html)
- [AWS Parameters and Secrets Lambda Extension - AWS Lambda User Guide](https://docs.aws.amazon.com/systems-manager/latest/userguide/ps-integration-lambda-extensions.html)
- [Retry behavior in AWS SDKs - AWS General Reference](https://docs.aws.amazon.com/general/latest/gr/api-retries.html)
- [Secrets Manager best practices - AWS Secrets Manager User Guide](https://docs.aws.amazon.com/secretsmanager/latest/userguide/best-practices.html)
- [Troubleshoot AWS Secrets Manager throttling - AWS re:Post](https://repost.aws/knowledge-center/secrets-manager-throttling)
