---
id: "aws-secrets-manager-throttling"
title: "AWS Secrets Manager RateExceeded Throttling"
domain: security
service: aws-secrets-manager
symptom_class: [timeout, service_unavailable]
severity: high
scope: global
version: "2.0.0"
last_updated: "2026-06-25"
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

### Step 1: Search CloudTrail for Secrets Manager ThrottlingException events

```bash
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=EventSource,AttributeValue=secretsmanager.amazonaws.com \
  --start-time "$(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ)" \
  --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --query 'Events[?contains(CloudTrailEvent, `ThrottlingException`)].{Time:EventTime,Event:CloudTrailEvent}' \
  --output json
```

Expected output: JSON array of events with `errorCode: ThrottlingException`. The `userIdentity.arn` field identifies which principals are being throttled. Empty array means throttling occurs at SDK retry layer before CloudTrail records the call.

### Step 2: Measure GetSecretValue API call rate against quota

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

### Step 3: Identify top callers by IAM principal

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

### Step 4: Check whether the application uses the caching library

```bash
pip show aws-secretsmanager-caching 2>/dev/null \
  && echo "Caching library INSTALLED" \
  || echo "Caching library NOT INSTALLED"
```

Expected output: `Caching library INSTALLED` means the library is available but may not be wired in. `NOT INSTALLED` means the application fetches directly on every call.

### Step 5: Inspect the effective SDK retry mode and max-attempts

```bash
env | grep -Ei 'retry|max_attempt|AWS_RETRY'
```

Expected output: `AWS_RETRY_MODE=standard` or `adaptive` is correct. If unset, boto3 defaults to `legacy` mode (5 retries, fixed backoff), which amplifies throttle storms. `AWS_MAX_ATTEMPTS` values above 10 can significantly worsen burst throttling.

### Step 6: Check the GetSecretValue service quota

```bash
aws service-quotas get-service-quota \
  --service-code secretsmanager \
  --quota-code L-2F66A066 \
  --query 'Quota.{QuotaName:QuotaName,Value:Value,Adjustable:Adjustable}' \
  --output table
```

Expected output: shows the current GetSecretValue quota value (default 10,000 RPS) and whether it is adjustable. If the quota was already increased, this shows the adjusted value.

## Causes

### Cause A: No client-side caching on a high-throughput workload

**Statement:** The application calls `GetSecretValue` on every request or function invocation without any in-process cache, multiplying API call rate by request throughput until the account quota is exhausted.

**Chain:**
- root: the application has no in-process secret cache and re-fetches on every request/invocation
- s1: per-request fetching multiplies API call rate by request throughput (e.g. 100 RPS x 10 pods = 1,000 GetSecretValue calls/sec)
- s2: sustained call rate consumes the shared per-region quota (default 10,000 RPS across all secrets and callers)
- D: GetSecretValue calls are throttled — see Symptom Recognition

**Indicators:**
- root: [Step 4] Output is `Caching library NOT INSTALLED`
- s1: [Step 3] A single IAM role ARN accounts for the majority of GetSecretValue calls
- s2: [Step 2] Per-60s `Sum` divided by 60 approaches or exceeds 10,000 RPS

**Interventions:**
- **remediation** (root): wire in the Secrets Manager caching library so each process serves secrets from an in-memory cache.

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

  **Verification:** After re-deploying, re-run Step 2. `APICallCount` for `GetSecretValue` should drop by 90%+ within 5 minutes; confirm `ThrottlingException` count in CloudWatch reaches zero.
- **mitigation** (root): install the caching library so it is available to wire into the application.

  ```bash
  pip install aws-secretsmanager-caching
  ```

  **Risk:** Installing the library still requires a code change and re-deploy to take effect; once wired, secrets remain stale for up to `secret_refresh_interval` seconds after rotation. **Duration:** until the application is updated to use the cache client. **Verification:** re-run Step 4 — output is `Caching library INSTALLED`.

---

### Cause B: Lambda cold-start thundering herd fetching secrets at init

**Statement:** Concurrent Lambda cold starts each call `GetSecretValue` in the initialization scope, creating a simultaneous burst that exceeds the account's Secrets Manager quota.

**Chain:**
- root: the function retrieves secrets in global init scope (outside the handler), so the fetch runs on every cold start
- s1: a traffic burst or autoscaling event triggers hundreds of concurrent cold starts within seconds
- s2: each execution environment has no shared state, so every cold start issues its own simultaneous GetSecretValue call
- D: the concurrent fetch burst is throttled — see Symptom Recognition

**Indicators:**
- root: [Step 3] Multiple distinct Lambda execution role ARNs each contribute GetSecretValue calls at the same timestamp
- s1: [Symptom] `ThrottlingException` errors correlate with Lambda concurrency spikes visible in CloudWatch Lambda metrics
- s2: [Step 1] CloudTrail shows `ThrottlingException` clustered at cold-start timestamps

**Interventions:**
- **defensive_fix** (s2): add the AWS Parameters and Secrets Lambda Extension so secrets are cached in the execution environment, then read from the extension endpoint instead of the SDK.

  ```bash
  # Add the AWS Parameters and Secrets Lambda Extension layer
  aws lambda update-function-configuration \
    --function-name my-function \
    --layers arn:aws:lambda:us-east-1:177933569100:layer:AWS-Parameters-and-Secrets-Lambda-Extension:11
  ```

  ```bash
  # Set cache TTL; default is 300 s. Align with rotation schedule.
  aws lambda update-function-configuration \
    --function-name my-function \
    --environment "Variables={SECRETS_MANAGER_TTL=300}"
  ```

  ```python
  import os, json, requests

  def get_secret(secret_name):
      url = f"http://localhost:2773/secretsmanager/get?secretId={secret_name}"
      headers = {"X-Aws-Parameters-Secrets-Token": os.environ["AWS_SESSION_TOKEN"]}
      return json.loads(requests.get(url, headers=headers).text)["SecretString"]
  ```

  **Verification:** Deploy updated function, then trigger a concurrency spike (e.g., load test). CloudWatch `APICallCount` for `GetSecretValue` should not increase proportionally to Lambda invocation count.

---

### Cause C: Microservice fleet rolling restart fetching secrets simultaneously

**Statement:** A Kubernetes rolling deployment or autoscaling event starts many application pods at once, each fetching secrets before serving traffic, concentrating `GetSecretValue` calls into a short burst.

**Chain:**
- root: a rolling deployment or HPA scale-out starts many pods within a short window (e.g. `maxSurge: 100%`, no inter-pod delay)
- s1: each pod fetches its secrets during initialization, before the readiness probe passes
- s2: with no caching, the concurrent fetches concentrate (e.g. 50 pods x N secrets) into a few seconds
- D: the concentrated fetch burst is throttled — see Symptom Recognition

**Indicators:**
- root: [Symptom] `ThrottlingException` errors correlate with deployment or HPA scale events visible in Kubernetes events
- s1: [Step 1] CloudTrail ThrottlingException timestamps align with pod startup timestamps

**Interventions:**
- **defensive_fix** (s1): stagger pod initialization with `minReadySeconds` so secret fetches spread out across the rollout.

  ```bash
  # Add a startup delay between pod initializations using minReadySeconds
  kubectl patch deployment my-app -p \
    '{"spec":{"minReadySeconds":10}}'
  ```

  **Verification:** Monitor `GetSecretValue` call rate in CloudWatch during the next deployment. The rate should remain below 10% of quota throughout the rollout.
- **mitigation** (root): slow the rollout surge so fewer pods initialize concurrently.

  ```bash
  kubectl patch deployment my-app -p \
    '{"spec":{"strategy":{"rollingUpdate":{"maxSurge":"25%","maxUnavailable":"10%"}}}}'
  ```

  **Risk:** Slower rollout increases total deployment time; in the interim the workload may serve reduced capacity. **Duration:** permanent deployment config change; also address the root cause by adding caching (see Cause A). **Verification:** the next rollout completes without `ThrottlingException` in CloudTrail.

---

### Cause D: Secret rotation storm saturating read and write quotas

**Statement:** Multiple secrets scheduled to rotate in the same window run their rotation Lambdas concurrently, generating simultaneous `GetSecretValue`, `PutSecretValue`, and `DescribeSecret` calls that saturate burst capacity across multiple API operation types.

**Chain:**
- root: many secrets are scheduled to rotate in the same window, so their rotation Lambdas run concurrently
- s1: each rotation Lambda calls GetSecretValue, then PutSecretValue, then DescribeSecret against the same operation quotas
- s2: write APIs (PutSecretValue, 50 RPS) have a far lower quota than reads, so even moderate concurrency saturates them
- s3: aggressive rotation-Lambda retries re-fire the same calls, making the burst self-reinforcing
- D: rotation read/write calls are throttled — see Symptom Recognition

**Indicators:**
- root: [Step 1] CloudTrail shows `ThrottlingException` on `PutSecretValue` or `DescribeSecret` operations (not only `GetSecretValue`)
- s2: [Step 2] Re-run Step 2 with `Value=PutSecretValue` — Sum divided by 60 approaches 50 RPS

**Interventions:**
- **remediation** (root): stagger rotation schedules across distinct hours/days so no window runs many rotations at once.

  ```bash
  # Stagger secrets across different hours/days using cron expressions
  aws secretsmanager rotate-secret \
    --secret-id my-secret-1 \
    --rotation-rules '{"ScheduleExpression":"cron(0 2 1 * ? *)","Duration":"2h"}'

  aws secretsmanager rotate-secret \
    --secret-id my-secret-2 \
    --rotation-rules '{"ScheduleExpression":"cron(0 8 15 * ? *)","Duration":"2h"}'
  ```

  **Verification:** After staggering rotations, confirm CloudTrail shows no `ThrottlingException` events on `PutSecretValue` or `DescribeSecret` during the next rotation window.

---

### Cause E: SDK retry amplification sustaining the throttle cascade

**Statement:** The application's AWS SDK is configured with `legacy` retry mode or an excessive `max_attempts` value, so each throttled request spawns multiple synchronized retries that re-trigger throttling and prolong the event.

**Chain:**
- root: the SDK uses `legacy` retry mode (or `AWS_MAX_ATTEMPTS` > 10), giving fixed backoff with no jitter
- s1: every throttled call retries up to 5 times with fixed timing, so all callers re-fire at nearly the same instant
- s2: the synchronized retry wave re-triggers throttling, sustaining the event far longer than necessary
- D: ThrottlingException persists in rapid bursts from the same ARN — see Symptom Recognition

**Indicators:**
- root: [Step 5] `AWS_RETRY_MODE` is unset (defaults to `legacy`) or `AWS_MAX_ATTEMPTS` is set above 10
- s2: [Step 1] CloudTrail shows repeated ThrottlingException entries from the same ARN within seconds of each other

**Interventions:**
- **remediation** (root): configure `standard` retry mode (truncated exponential backoff with jitter and a retry token bucket) in the SDK client.

  ```python
  import boto3
  from botocore.config import Config

  config = Config(retries={"mode": "standard", "max_attempts": 5})
  client = boto3.client("secretsmanager", config=config)
  ```

  **Verification:** After restarting the application with the new retry config, confirm CloudTrail no longer shows rapid consecutive ThrottlingException entries from the same ARN within sub-second intervals.
- **mitigation** (root): set `standard` retry mode via environment variables without a code change.

  ```bash
  export AWS_RETRY_MODE=standard
  export AWS_MAX_ATTEMPTS=5
  ```

  **Risk:** `standard` mode increases latency for throttled requests (up to 20 s backoff per retry) — intentional, to prevent retry storms; do not switch to `adaptive` for clients shared across unrelated secrets, as a drained token bucket can delay even the first request. **Duration:** permanent — set in the application environment or SDK config file. **Verification:** re-run Step 5 — `AWS_RETRY_MODE=standard` is present.

---

### Cause Z: Unidentified

**Statement:** [Default] Throttling cause is not identifiable from available diagnostic output (e.g. cross-account quota sharing, VPC endpoint request concentration, or a new workload not yet visible in historical CloudTrail).

**Indicators:**
- [Default]

**Interventions:**
- **mitigation** (D): capture a full diagnostic snapshot (CloudTrail event IDs, CloudWatch `APICallCount`/`ThrottlingException` metrics, Steps 1–6 output) and escalate to AWS Support / the SME; optionally request a quota increase as a stopgap.

  ```bash
  aws service-quotas request-service-quota-increase \
    --service-code secretsmanager \
    --quota-code L-2F66A066 \
    --desired-value 20000
  ```

  **Risk:** A quota increase does not address underlying inefficiency — without caching the account will eventually exhaust the higher limit; AWS processes increases in 1–3 business days. **Duration:** until the SME identifies the root cause; approved increases are permanent. **Verification:** monitor CloudWatch `APICallCount` after approval and confirm `ThrottlingException` events cease.

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
