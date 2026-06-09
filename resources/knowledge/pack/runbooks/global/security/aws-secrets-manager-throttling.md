---
id: "aws-secrets-manager-throttling"
title: "AWS Secrets Manager RateExceeded Throttling"
domain: security
service: aws-secrets-manager
symptom_class: [timeout, service_unavailable]
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-05-12"
verified_by: "kb-researcher"
status: draft
tags: [aws, secrets-manager, throttling, rate-limit, caching, sdk, lambda-extension]
difficulty: intermediate
---

## Symptom Recognition

Applications receive `ThrottlingException` or `RateExceededException` from the AWS Secrets Manager API:

```text
An error occurred (ThrottlingException) when calling the GetSecretValue operation: Rate exceeded
```

```text
botocore.exceptions.ClientError: An error occurred (ThrottlingException) when calling
the GetSecretValue operation (reached max retries: 4): Rate exceeded
```

CloudTrail shows `errorCode: ThrottlingException` on `secretsmanager.amazonaws.com` events. CloudWatch `APICallCount` for `GetSecretValue` spikes to or above service quota. Application response latency increases as SDK retry loops execute before ultimately failing.

## Applicability

Applies to all AWS accounts using AWS Secrets Manager in any region. Affects all runtimes and AWS SDK versions (boto3, SDK for Java, SDK for JavaScript, AWS CLI v2, etc.). Requires the following permissions for diagnosis: `secretsmanager:GetSecretValue`, `cloudtrail:LookupEvents`, `cloudwatch:GetMetricStatistics`, `logs:FilterLogEvents`. Throttling quotas are per-account per-region and apply to cross-account callers based on the calling account's quota, not the secret owner's account.

## Diagnostic Steps

### Step 1: Search CloudTrail for Secrets Manager ThrottlingException events in the last hour

```bash
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=EventSource,AttributeValue=secretsmanager.amazonaws.com \
  --start-time "$(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ)" \
  --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --query 'Events[?contains(CloudTrailEvent, `ThrottlingException`)].{Time:EventTime,Event:CloudTrailEvent}' \
  --output json
```

Expected output: JSON array of events with `errorCode: ThrottlingException`. The `userIdentity.arn` field identifies which principals are being throttled. Empty array means throttling occurs at SDK retry layer before CloudTrail records the call.

### Step 2: Measure current GetSecretValue API call rate via CloudWatch to compare against quota

```bash
aws cloudwatch get-metric-statistics \
  --namespace AWS/SecretsManager \
  --metric-name APICallCount \
  --start-time "$(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ)" \
  --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --period 60 \
  --statistics Sum \
  --dimensions Name=Operation,Value=GetSecretValue \
  --output table
```

Expected output: `Sum` values per 60-second period. Divide by 60 to get requests per second. Values approaching 10,000 RPS indicate quota pressure; values at or above indicate throttling is expected.

### Step 3: Identify top callers by IAM principal using GetSecretValue call count

```bash
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=EventSource,AttributeValue=secretsmanager.amazonaws.com \
  --start-time "$(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ)" \
  --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --query 'Events[?contains(CloudTrailEvent, `GetSecretValue`)].CloudTrailEvent' \
  --output text | python3 -c "
import sys, json, collections
events = [json.loads(l) for l in sys.stdin if l.strip()]
counts = collections.Counter(e.get('userIdentity',{}).get('arn','unknown') for e in events)
[print(f'{c}\t{a}') for a, c in counts.most_common(10)]
"
```

Expected output: tab-separated count and IAM ARN lines, most frequent first. A single role with disproportionately high call counts indicates a workload missing client-side caching.

### Step 4: Check whether the application uses the Secrets Manager caching library

```bash
pip show aws-secretsmanager-caching 2>/dev/null \
  && echo "Caching library INSTALLED" \
  || echo "Caching library NOT INSTALLED"
```

Expected output: `Caching library INSTALLED` means the library is available but may not be wired in. `NOT INSTALLED` means the application fetches directly on every call.

### Step 5: Inspect the effective AWS SDK retry mode and max-attempts configuration

```bash
env | grep -Ei 'retry|max_attempt|AWS_RETRY'
```

Expected output: `AWS_RETRY_MODE=standard` or `adaptive` is correct. If unset, boto3 defaults to `legacy` mode (5 retries, fixed backoff), which amplifies throttle storms. `AWS_MAX_ATTEMPTS` values above 10 can significantly worsen burst throttling.

### Step 6: Check current GetSecretValue service quota and pending quota increase requests

```bash
aws service-quotas get-service-quota \
  --service-code secretsmanager \
  --quota-code L-2F66A066 \
  --query 'Quota.{QuotaName:QuotaName,Value:Value,Adjustable:Adjustable}' \
  --output table
```

Expected output: shows the current GetSecretValue quota value (default 10,000 RPS) and whether it is adjustable. If the quota was already increased, this shows the adjusted value.

## Causes

### Cause A: No client-side caching — every invocation calls GetSecretValue directly

**Statement:** The application calls `GetSecretValue` on every request or function invocation without any in-process cache, multiplying API call rate by request throughput.

**Mechanism:** Without caching, each HTTP request handled by the application (or each Lambda invocation) issues a separate `GetSecretValue` API call. At modest throughput — 100 RPS across 10 pods — this generates 1,000 Secrets Manager API calls per second. The service quota (typically 10,000 RPS per region) is shared across all secrets and all services in the account, so a single high-throughput service can consume the entire quota and starve other callers.

**Indicator:**

- [Step 4] Output is `Caching library NOT INSTALLED`
- [Step 3] A single IAM role ARN accounts for the majority of GetSecretValue calls
<!-- match: {"step": 4, "predicate": "contains", "target": "NOT INSTALLED"} -->

**Mitigation:**

- **Risk:** Installing the caching library requires a code change and re-deploy; secrets remain stale for up to `secret_refresh_interval` seconds after rotation.
- **Command:**

  ```bash
  pip install aws-secretsmanager-caching
  ```

- **Duration:** Permanent — code must be updated to use the cache client.

**Resolution:**

```python
from aws_secretsmanager_caching import SecretCache, SecretCacheConfig
import boto3

cache_config = SecretCacheConfig(
    max_cache_size=1000,
    secret_refresh_interval=3600  # seconds; align with rotation schedule
)
cache = SecretCache(config=cache_config, client=boto3.client('secretsmanager'))

# Replace client.get_secret_value() calls:
secret_value = cache.get_secret_string('my-secret-name')
```

**Verification:** After re-deploying, re-run Step 2. `APICallCount` for `GetSecretValue` should drop by 90%+ within 5 minutes. Confirm `ThrottlingException` count in CloudWatch reaches zero.

---

### Cause B: Lambda cold-start thundering herd — hundreds of concurrent invocations each fetch secrets on startup

**Statement:** Concurrent Lambda cold starts each call `GetSecretValue` at initialization time, creating a burst that exceeds the account's Secrets Manager quota.

**Mechanism:** Lambda functions that retrieve secrets in the global initialization scope (outside the handler) run that code on every cold start. An autoscaling event or traffic burst that triggers hundreds of concurrent cold starts within seconds generates an equivalent number of simultaneous `GetSecretValue` calls. Each Lambda execution environment has no shared state, so there is no cross-invocation caching without the Lambda extension. The burst may be short-lived but still triggers `ThrottlingException` for all concurrently initializing functions.

**Indicator:**

- [Symptom] `ThrottlingException` errors correlate with Lambda concurrency spikes visible in CloudWatch Lambda metrics
- [Step 3] Multiple distinct Lambda execution role ARNs each contribute GetSecretValue calls at the same timestamp
<!-- match: {"step": 1, "predicate": "contains", "target": "ThrottlingException"} -->

**Mitigation:**

- **Risk:** Adding the Lambda extension layer requires a function update; the extension adds ~10 ms cold-start overhead. Setting `SECRETS_MANAGER_TTL` too low (e.g., 0) disables caching and defeats the purpose.
- **Command:**

  ```bash
  # Add the AWS Parameters and Secrets Lambda Extension layer
  aws lambda update-function-configuration \
    --function-name my-function \
    --layers arn:aws:lambda:us-east-1:177933569100:layer:AWS-Parameters-and-Secrets-Lambda-Extension:11
  ```

- **Duration:** Permanent — extension caches secrets in the execution environment for `SECRETS_MANAGER_TTL` seconds (default: 300 s).

**Resolution:**

```bash
# Set cache TTL; default is 300 s. Align with rotation schedule.
aws lambda update-function-configuration \
  --function-name my-function \
  --environment "Variables={SECRETS_MANAGER_TTL=300}"
```

Replace `GetSecretValue` SDK calls with the extension HTTP endpoint:

```python
import os, json, requests

def get_secret(secret_name):
    url = f"http://localhost:2773/secretsmanager/get?secretId={secret_name}"
    headers = {"X-Aws-Parameters-Secrets-Token": os.environ["AWS_SESSION_TOKEN"]}
    return json.loads(requests.get(url, headers=headers).text)["SecretString"]
```

**Verification:** Deploy updated function, then trigger a concurrency spike (e.g., load test). CloudWatch `APICallCount` for `GetSecretValue` should not increase proportionally to Lambda invocation count.

---

### Cause C: Microservice fleet rolling restart — all pods fetch secrets on startup simultaneously

**Statement:** A Kubernetes rolling deployment or autoscaling event causes many application pods to start simultaneously, each fetching secrets before serving traffic, creating a burst of `GetSecretValue` calls.

**Mechanism:** Each pod fetches secrets during initialization (often before the readiness probe passes), concentrating API calls within a short window. With `maxSurge: 100%` and no delay between pod starts, a 50-pod deployment can generate 50 concurrent `GetSecretValue` calls per secret. If the application fetches multiple secrets and lacks caching, the burst multiplies further. The same pattern occurs during HPA scale-out events.

**Indicator:**

- [Symptom] `ThrottlingException` errors correlate with deployment or HPA scale events visible in Kubernetes events
- [Step 1] CloudTrail ThrottlingException timestamps align with pod startup timestamps
<!-- match: {"step": 1, "predicate": "contains", "target": "ThrottlingException"} -->

**Mitigation:**

- **Risk:** Slower rollout (`maxSurge: 25%`) increases total deployment time; in the interim, the workload may serve reduced capacity.
- **Command:**

  ```bash
  kubectl patch deployment my-app -p \
    '{"spec":{"strategy":{"rollingUpdate":{"maxSurge":"25%","maxUnavailable":"10%"}}}}'
  ```

- **Duration:** Permanent deployment configuration change; also address root cause by adding caching (see Cause A).

**Resolution:**

```bash
# Add a startup delay between pod initializations using minReadySeconds
kubectl patch deployment my-app -p \
  '{"spec":{"minReadySeconds":10}}'
```

**Verification:** Monitor `GetSecretValue` call rate in CloudWatch during the next deployment. The rate should remain below 10% of quota throughout the rollout.

---

### Cause D: Secret rotation storm — multiple secrets rotating in the same window trigger concurrent read and write bursts

**Statement:** Multiple secrets scheduled to rotate at the same time generate concurrent `GetSecretValue`, `PutSecretValue`, and `DescribeSecret` calls from the rotation Lambda functions, consuming burst capacity across multiple API operation types.

**Mechanism:** Each secret rotation invokes a Lambda function that calls `GetSecretValue` (to retrieve the current secret), updates the credential in the target system, then calls `PutSecretValue`. Write APIs (`PutSecretValue`) have a much lower quota (50 RPS) than read APIs. When dozens of secrets rotate simultaneously, even moderate concurrency saturates write quotas. Rotation Lambdas also trigger `DescribeSecret` internally. The burst is self-reinforcing if rotation Lambdas retry aggressively.

**Indicator:**

- [Step 1] CloudTrail shows `ThrottlingException` on `PutSecretValue` or `DescribeSecret` operations (not only `GetSecretValue`)
- [Step 2] Re-run Step 2 with `Value=PutSecretValue` — Sum divided by 60 approaches 50 RPS
<!-- match: {"step": 1, "predicate": "contains", "target": "PutSecretValue"} -->

**Mitigation:**

- **Risk:** Staggering rotation schedules requires updating each secret individually; brief window where some secrets are on old credentials during the transition.
- **Command:**

  ```bash
  # Stagger secrets across different hours/days using cron expressions
  aws secretsmanager rotate-secret \
    --secret-id my-secret-1 \
    --rotation-rules '{"ScheduleExpression":"cron(0 2 1 * ? *)","Duration":"2h"}'

  aws secretsmanager rotate-secret \
    --secret-id my-secret-2 \
    --rotation-rules '{"ScheduleExpression":"cron(0 8 15 * ? *)","Duration":"2h"}'
  ```

- **Duration:** Permanent — rotation schedules remain until changed.

**Resolution:** Same as Mitigation.

**Verification:** After staggering rotations, confirm CloudTrail shows no `ThrottlingException` events on `PutSecretValue` or `DescribeSecret` during the next rotation window.

---

### Cause E: SDK retry amplification — legacy or aggressive retry configuration multiplies requests during throttle events

**Statement:** The application's AWS SDK is configured with `legacy` retry mode or an excessive `max_attempts` value, causing each throttled request to generate multiple retry calls that worsen the throttle cascade.

**Mechanism:** In `legacy` retry mode, boto3 uses a fixed backoff with up to 5 retries per call. With no exponential backoff or jitter, all retrying callers fire at nearly the same time, creating synchronized retry waves. Each wave re-triggers throttling, sustaining the event far longer than necessary. `standard` mode uses truncated binary exponential backoff with jitter (max 20 s backoff) and a retry token bucket that prevents retry storms. `adaptive` mode additionally adds client-side rate limiting using a token bucket, but is not recommended for multi-tenant applications.

**Indicator:**

- [Step 5] `AWS_RETRY_MODE` is unset (defaults to `legacy`) or `AWS_MAX_ATTEMPTS` is set above 10
- [Step 1] CloudTrail shows repeated ThrottlingException entries from the same ARN within seconds of each other
<!-- match: {"step": 5, "predicate": "absent", "target": "AWS_RETRY_MODE"} -->

**Mitigation:**

- **Risk:** `standard` mode increases latency for throttled requests (up to 20 s backoff per retry). This is intentional — it prevents retry storms. `adaptive` mode can delay even the first request when a token bucket is drained; do not use with shared clients across unrelated secrets.
- **Command:**

  ```bash
  export AWS_RETRY_MODE=standard
  export AWS_MAX_ATTEMPTS=5
  ```

- **Duration:** Permanent — set in application environment or SDK config file.

**Resolution:**

```python
import boto3
from botocore.config import Config

config = Config(retries={"mode": "standard", "max_attempts": 5})
client = boto3.client("secretsmanager", config=config)
```

**Verification:** After restarting the application with the new retry config, confirm CloudTrail no longer shows rapid consecutive ThrottlingException entries from the same ARN within sub-second intervals.

---

### Cause Z: Unidentified

**Statement:** [Default] Throttling cause is not identifiable from available diagnostic output.

**Mechanism:** The diagnostic steps above cover the most common causes of Secrets Manager throttling. If all checks are inconclusive, the root cause may involve cross-account quota sharing, VPC endpoint request concentration, or a newly introduced workload not yet visible in historical CloudTrail data.

**Indicator:**

- [Default] None of Causes A–E match the diagnostic findings

**Mitigation:**

- **Risk:** Requesting a quota increase does not address underlying inefficiency; the account will eventually exhaust the higher limit without caching.
- **Command:**

  ```bash
  aws service-quotas request-service-quota-increase \
    --service-code secretsmanager \
    --quota-code L-2F66A066 \
    --desired-value 20000
  ```

- **Duration:** AWS processes quota increase requests in 1–3 business days; approved increases are permanent.

**Resolution:** Escalate to AWS Support with CloudTrail event IDs and CloudWatch metric data. Simultaneously implement client-side caching (Cause A Resolution) as a defensive measure regardless of root cause.

**Verification:** Monitor CloudWatch `APICallCount` after quota increase approval. Confirm `ThrottlingException` events cease.

## Prevention

1. **Always use the Secrets Manager caching library** in production applications. Configure `secret_refresh_interval` to match the rotation schedule (1–24 hours is typical). Available for Python, Java, .NET, Go, and Rust.

2. **Add the AWS Parameters and Secrets Lambda Extension** to all Lambda functions that access Secrets Manager. Default TTL is 300 seconds; set `SECRETS_MANAGER_TTL` to a value lower than your rotation window.

3. **Use `standard` retry mode in all AWS SDKs.** Set `AWS_RETRY_MODE=standard` in the environment or configure via `Config(retries={"mode": "standard"})` in boto3. Never leave `legacy` mode in place for production workloads.

4. **Stagger Kubernetes rollouts** using `maxSurge: 25%` and `minReadySeconds: 10` to prevent simultaneous pod-startup secret fetch bursts.

5. **Stagger secret rotation schedules** using distinct cron expressions so that no more than 5 secrets rotate within the same hour.

6. **Set a CloudWatch alarm on Secrets Manager throttling:**

   ```bash
   aws cloudwatch put-metric-alarm \
     --alarm-name SecretsManagerThrottling \
     --metric-name APICallCount \
     --namespace AWS/SecretsManager \
     --statistic Sum \
     --period 300 \
     --threshold 50 \
     --comparison-operator GreaterThanThreshold \
     --dimensions Name=ErrorCode,Value=ThrottlingException \
     --evaluation-periods 1 \
     --alarm-actions arn:aws:sns:us-east-1:123456789012:ops-alerts
   ```

7. **Monitor quota headroom** using Service Quotas: set a CloudWatch alarm when `GetSecretValue` call rate exceeds 70% of the approved quota to provide lead time for quota increase requests.

## Sources

- [AWS Secrets Manager quotas — AWS Secrets Manager User Guide](https://docs.aws.amazon.com/secretsmanager/latest/userguide/reference_limits.html) — quota values, throttling error types, backoff guidance; priority 1
- [Use caching to retrieve secrets — AWS Secrets Manager best practices](https://docs.aws.amazon.com/secretsmanager/latest/userguide/best-practices.html) — caching library list, rotation best practices; priority 1
- [Use Secrets Manager secrets in Lambda functions — AWS Lambda Developer Guide](https://docs.aws.amazon.com/lambda/latest/dg/with-secrets-manager.html) — Lambda extension setup, environment variables (SECRETS_MANAGER_TTL default 300 s, port 2773, cache size 1000), code examples for Python/Node/Java; priority 1
- [Retry behavior — AWS SDKs and Tools Reference Guide](https://docs.aws.amazon.com/sdkref/latest/guide/feature-retry-behavior.html) — retry modes (legacy/standard/adaptive), max_attempts defaults, exponential backoff with jitter algorithm; priority 1
