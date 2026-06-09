---
id: "lambda-timeout"
title: "AWS Lambda Function Timeout"
domain: compute
service: aws-lambda
symptom_class: [timeout, latency]
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-05-12"
verified_by: "kb-researcher"
status: draft
tags: [aws, lambda, serverless, cold-start, vpc, provisioned-concurrency]
difficulty: intermediate
---

## Symptom Recognition

- CloudWatch Logs show `Task timed out after X.XX seconds` in the function's log group
- REPORT line shows `Duration` equal to the configured `Timeout` value (e.g., `Duration: 15000.00 ms` when timeout is 15 s)
- Lambda `Errors` CloudWatch metric spikes; corresponding `Throttles` metric may also increase if retries pile up
- API Gateway, ALB, or EventBridge callers receive 5xx errors at the same rate as timeouts
- `INIT_START` present in logs before `Task timed out` — indicates cold-start overhead consumed the timeout budget
- X-Ray traces show a subsegment (HTTP call, DB query, SDK call) that does not return before the trace terminates

## Applicability

- Applies to all Lambda runtimes (Python, Node.js, Java, Go, .NET, Ruby, custom runtimes) in any AWS region
- Required IAM permissions: `lambda:GetFunctionConfiguration`, `lambda:UpdateFunctionConfiguration`, `lambda:InvokeFunction`, `logs:StartQuery`, `logs:GetLogEvents`, `logs:DescribeLogStreams`, `cloudwatch:GetMetricStatistics`
- For VPC-connected functions: add `ec2:DescribeSubnets`, `ec2:DescribeRouteTables`, `ec2:DescribeNatGateways`
- AWS CLI v2 required for the CloudWatch Logs Insights queries in Steps 3–4
- Configurable timeout range: 1 second to 900 seconds (15 minutes); default is 3 seconds

## Diagnostic Steps

### Step 1: Retrieve current timeout and memory configuration

```bash
aws lambda get-function-configuration \
  --function-name my-function \
  --query '{Timeout:Timeout,MemorySize:MemorySize,Runtime:Runtime,VpcConfig:VpcConfig}'
```

Expected output: JSON with `Timeout` (seconds), `MemorySize` (MB), runtime identifier, and `VpcConfig` (empty object if not VPC-attached).

### Step 2: Fetch the most recent log stream and inspect REPORT lines

```bash
STREAM=$(aws logs describe-log-streams \
  --log-group-name /aws/lambda/my-function \
  --order-by LastEventTime --descending --limit 1 \
  --query 'logStreams[0].logStreamName' --output text)

aws logs get-log-events \
  --log-group-name /aws/lambda/my-function \
  --log-stream-name "$STREAM" \
  --limit 100 \
  --query 'events[*].message' --output text
```

Expected output: Log lines including `INIT_START`, `START`, `REPORT`, and any `Task timed out after` message. The `REPORT` line shows `Duration`, `Billed Duration`, `Memory Size`, `Max Memory Used`, and optionally `Init Duration`.

### Step 3: Quantify cold-start frequency and init duration over the last hour

```bash
aws logs start-query \
  --log-group-name /aws/lambda/my-function \
  --start-time $(date -d '1 hour ago' +%s) \
  --end-time $(date +%s) \
  --query-string 'filter @type = "REPORT" | stats count() as invocations, count(@initDuration) as coldStarts, max(@initDuration) as maxInitMs, avg(@duration) as avgDurationMs by bin(5m)'
```

Expected output: Per-5-minute rows with `invocations`, `coldStarts`, `maxInitMs`, and `avgDurationMs`. A non-zero `coldStarts` column confirms cold-start overhead is present.

### Step 4: Check memory usage versus allocation

```bash
aws logs start-query \
  --log-group-name /aws/lambda/my-function \
  --start-time $(date -d '1 hour ago' +%s) \
  --end-time $(date +%s) \
  --query-string 'filter @type = "REPORT" | stats max(@maxMemoryUsed) as peakMemBytes, avg(@duration) as avgMs, max(@duration) as maxMs'
```

Expected output: `peakMemBytes` (raw bytes), `avgMs`, `maxMs`. Divide `peakMemBytes` by 1048576 to convert to MB and compare to the `MemorySize` from Step 1.

### Step 5: Check VPC subnet IP availability (VPC-attached functions only)

```bash
SUBNET_IDS=$(aws lambda get-function-configuration \
  --function-name my-function \
  --query 'VpcConfig.SubnetIds[]' \
  --output text)

for subnet in $SUBNET_IDS; do
  aws ec2 describe-subnets --subnet-ids "$subnet" \
    --query 'Subnets[0].{SubnetId:SubnetId,AvailableIPs:AvailableIpAddressCount,CidrBlock:CidrBlock}'
done
```

Expected output: One JSON object per subnet showing `AvailableIPs`. Values near 0 indicate IP exhaustion that prevents Hyperplane ENI creation.

### Step 6: Verify internet routing for VPC-attached functions

```bash
SUBNET_ID=$(aws lambda get-function-configuration \
  --function-name my-function \
  --query 'VpcConfig.SubnetIds[0]' --output text)

RTB_ID=$(aws ec2 describe-route-tables \
  --filters "Name=association.subnet-id,Values=$SUBNET_ID" \
  --query 'RouteTables[0].RouteTableId' --output text)

aws ec2 describe-route-tables --route-table-ids "$RTB_ID" \
  --query 'RouteTables[0].Routes[*].{Dest:DestinationCidrBlock,GatewayId:GatewayId,NatGatewayId:NatGatewayId}'
```

Expected output: Route table entries. A `0.0.0.0/0` route with a `NatGatewayId` confirms internet access for VPC functions. Absence of this route means the function cannot reach public endpoints.

### Step 7: Enable X-Ray active tracing and inspect subsegment durations

```bash
aws lambda update-function-configuration \
  --function-name my-function \
  --tracing-config Mode=Active

aws lambda invoke \
  --function-name my-function \
  --payload '{}' /tmp/lambda-out.json

aws xray get-trace-summaries \
  --start-time $(date -d '5 minutes ago' +%s) \
  --end-time $(date +%s) \
  --query 'TraceSummaries[*].{Id:Id,Duration:Duration,HasError:HasError}'
```

Expected output: Trace summaries. Retrieve the longest trace ID, then run `aws xray batch-get-traces --trace-ids <id>` to see per-subsegment durations identifying the specific bottleneck call.

### Step 8: Check for recursive invocation loops

```bash
aws cloudwatch get-metric-statistics \
  --namespace AWS/Lambda \
  --metric-name ConcurrentExecutions \
  --dimensions Name=FunctionName,Value=my-function \
  --start-time $(date -d '30 minutes ago' -u +%Y-%m-%dT%H:%M:%SZ) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%SZ) \
  --period 60 --statistics Maximum
```

Expected output: `Maximum` concurrency per minute. Sustained high concurrency approaching the account limit while the function is not handling external traffic indicates a recursive loop.

## Causes

### Cause A: Cold start exhausts the timeout budget

**Statement:** The Init phase (loading code and initializing global state) consumes a large fraction of the configured timeout, leaving insufficient time for the Invoke phase to complete.

**Mechanism:** Lambda creates a new execution environment when no warm environment is available. The Init phase runs the full module/package import, SDK client construction, and any global initialization code before the handler is called. For short timeouts (≤3 s), a slow Init phase causes `Sandbox.Timedout`, and subsequent suppressed-init attempts also fail because the suppressed init must complete within the same timeout budget.

**Indicator:**

- [Step 2] `INIT_START` appears in the log stream and `Task timed out after` follows in the same invocation
- [Step 3] `coldStarts` count is high relative to `invocations` (e.g., >20% cold-start rate)
- [Step 3] `maxInitMs` is close to or exceeds the configured timeout in milliseconds

<!-- match: {"step": 3, "predicate": "threshold", "target": "maxInitMs", "op": ">", "value": 2500} -->

**Mitigation:**

- **Risk:** Increasing timeout raises maximum cost per invocation; does not fix the root cause.
- **Command:**

  ```bash
  aws lambda update-function-configuration \
    --function-name my-function \
    --timeout 30
  ```

- **Duration:** Immediate; buys time to optimize initialization code.

**Resolution:**

```bash
# Enable SnapStart (Java only) to snapshot initialized state
aws lambda update-function-configuration \
  --function-name my-function \
  --snap-start ApplyOn=PublishedVersions
aws lambda publish-version --function-name my-function

# For all runtimes: enable provisioned concurrency on a published version
VERSION=$(aws lambda publish-version --function-name my-function \
  --query 'Version' --output text)
aws lambda put-provisioned-concurrency-config \
  --function-name my-function \
  --qualifier "$VERSION" \
  --provisioned-concurrent-executions 5
```

- **Impact:** Provisioned concurrency incurs continuous cost regardless of invocation volume; right-size to observed peak concurrency.
- **Rollback:** `aws lambda delete-provisioned-concurrency-config --function-name my-function --qualifier "$VERSION"`

**Verification:** Invoke 10 times and confirm `INIT_START` is absent from logs and `Duration` is consistently below 80% of the configured timeout.

---

### Cause B: Downstream service latency blocks the handler

**Statement:** A downstream dependency (database, external API, or AWS service) responds slowly or not at all, causing the handler to block until the Lambda timeout fires.

**Mechanism:** Lambda functions frequently make synchronous network calls to databases, caches, or third-party APIs. When the downstream service is degraded — due to high load, cold connection pools, or misconfigured DNS — the SDK or HTTP client waits indefinitely (or up to its own timeout, which may exceed the Lambda timeout). The function continues consuming execution time while blocked, then terminates with `Task timed out after X seconds` without producing output.

**Indicator:**

- [Step 7] X-Ray subsegment for a specific downstream call (DynamoDB, RDS, HTTP) shows duration equal to total trace duration
- [Step 2] Application logs show the function reaching the downstream call but no response log after it
- [Symptom] Duration in REPORT line equals the configured timeout consistently across invocations

<!-- match: {"step": 7, "predicate": "contains", "target": "HasError"} -->

**Mitigation:**

- **Risk:** Adding client-side timeouts causes the function to fail fast with a specific error instead of a silent Lambda timeout; callers may need to handle a new error type.
- **Command:**

  ```bash
  # No CLI command — add SDK timeout in application code:
  # Python boto3 example:
  # from botocore.config import Config
  # config = Config(connect_timeout=3, read_timeout=8, retries={'max_attempts': 2})
  # client = boto3.client('dynamodb', config=config)
  echo "Deploy updated function code with explicit client timeouts"
  ```

- **Duration:** Requires a code deployment (minutes); effective immediately after deploy.

**Resolution:**

```bash
# After identifying the bottleneck via X-Ray, address root cause per service:
# DynamoDB — add a GSI or enable DAX caching
# RDS — add a read replica or connection pool (RDS Proxy)
# External API — add circuit breaker or async offload via SQS

# Example: enable RDS Proxy for a MySQL database
aws rds create-db-proxy \
  --db-proxy-name my-proxy \
  --engine-family MYSQL \
  --auth '[{"AuthScheme":"SECRETS","SecretArn":"arn:aws:secretsmanager:...","IAMAuth":"DISABLED"}]' \
  --role-arn arn:aws:iam::123456789012:role/rds-proxy-role \
  --vpc-subnet-ids subnet-abc subnet-def
```

**Verification:** After deployment, run `aws xray get-trace-summaries` following 5–10 invocations and confirm no subsegment duration exceeds 50% of the Lambda timeout.

---

### Cause C: Insufficient memory causing CPU starvation

**Statement:** The function is allocated insufficient memory, resulting in proportionally low CPU that makes computation-heavy operations too slow to complete within the timeout.

**Mechanism:** Lambda allocates CPU power linearly with memory: a 128 MB function receives approximately 1/16th of a vCPU, while a 1769 MB function receives one full vCPU. Functions performing JSON parsing, compression, image processing, or cryptographic operations on 128–256 MB are CPU-starved and may take 10–100x longer than on 1024 MB+. Additionally, garbage collectors in Python, Java, and Node.js run more frequently under memory pressure, adding latency spikes.

**Indicator:**

- [Step 4] `peakMemBytes` is well below the `MemorySize` allocation (function is not memory-bound), yet `avgMs` is high
- [Step 1] `MemorySize` is 128 or 256 MB
- [Step 2] No downstream service call in logs immediately before timeout — function appears to time out inside CPU-bound code

<!-- match: {"step": 1, "predicate": "threshold", "target": "MemorySize", "op": "<", "value": 512} -->

**Mitigation:**

- **Risk:** Increasing memory increases per-ms cost but typically reduces total invocation duration; net cost often decreases.
- **Command:**

  ```bash
  aws lambda update-function-configuration \
    --function-name my-function \
    --memory-size 1024
  ```

- **Duration:** Immediate.

**Resolution:**

```bash
# Use AWS Lambda Power Tuning to find the optimal memory/cost/performance trade-off
aws stepfunctions start-execution \
  --state-machine-arn arn:aws:states:us-east-1:123456789012:stateMachine:powerTuningStateMachine \
  --input '{
    "lambdaARN": "arn:aws:lambda:us-east-1:123456789012:function:my-function",
    "powerValues": [128, 256, 512, 1024, 1769, 3008],
    "num": 20,
    "payload": "{}"
  }'
```

- **Impact:** Memory change applies to all future invocations; no restart needed.
- **Rollback:** `aws lambda update-function-configuration --function-name my-function --memory-size 128`

**Verification:** After the memory increase, confirm `avgDurationMs` from Step 4 drops by at least 40% and no longer approaches the timeout.

---

### Cause D: VPC subnet IP exhaustion or missing NAT Gateway

**Statement:** A VPC-attached Lambda function cannot establish a Hyperplane ENI due to subnet IP exhaustion, or it cannot reach public endpoints because no NAT Gateway route exists in the subnet route table.

**Mechanism:** When a Lambda function is attached to a VPC, the Lambda service creates Hyperplane ENIs in the specified subnets. If a subnet has fewer available IPs than needed to create ENIs at the required concurrency, new execution environments cannot be created and invocations time out. Additionally, Lambda functions in a VPC do not have internet access by default — a NAT Gateway (for private subnets) or VPC endpoint (for AWS services) is required. Without these, SDK calls to AWS services or external APIs block until the Lambda timeout fires.

**Indicator:**

- [Step 5] `AvailableIPs` for one or more subnets is below 10
- [Step 6] No route with `NatGatewayId` exists for `0.0.0.0/0` in the subnet route table
- [Step 1] `VpcConfig.SubnetIds` is non-empty (function is VPC-attached)

<!-- match: {"step": 5, "predicate": "threshold", "target": "AvailableIPs", "op": "<", "value": 10} -->
<!-- match: {"step": 6, "predicate": "absent", "target": "NatGatewayId"} -->

**Mitigation:**

- **Risk:** Adding VPC endpoints or a NAT Gateway incurs hourly charges; verify the function genuinely needs internet access before adding a NAT Gateway.
- **Command:**

  ```bash
  # Add gateway endpoints for DynamoDB and S3 (no hourly charge)
  VPC_ID=$(aws lambda get-function-configuration \
    --function-name my-function \
    --query 'VpcConfig.VpcId' --output text)

  RTB_ID=$(aws ec2 describe-route-tables \
    --filters "Name=vpc-id,Values=$VPC_ID" \
    --query 'RouteTables[0].RouteTableId' --output text)

  aws ec2 create-vpc-endpoint \
    --vpc-id "$VPC_ID" \
    --service-name com.amazonaws.us-east-1.dynamodb \
    --route-table-ids "$RTB_ID"

  aws ec2 create-vpc-endpoint \
    --vpc-id "$VPC_ID" \
    --service-name com.amazonaws.us-east-1.s3 \
    --route-table-ids "$RTB_ID"
  ```

- **Duration:** VPC endpoints become active within 2 minutes.

**Resolution:**

```bash
# If function needs general internet access, attach a NAT Gateway
# (replace with your actual subnet/EIP values)
EIP=$(aws ec2 allocate-address --domain vpc --query 'AllocationId' --output text)
NAT_GW=$(aws ec2 create-nat-gateway \
  --subnet-id subnet-public-123 \
  --allocation-id "$EIP" \
  --query 'NatGateway.NatGatewayId' --output text)

# Wait for NAT Gateway to become available (~1 min), then add route
aws ec2 create-route \
  --route-table-id "$RTB_ID" \
  --destination-cidr-block 0.0.0.0/0 \
  --nat-gateway-id "$NAT_GW"
```

**Verification:** Invoke the function and confirm it completes without timeout. Check Step 6 again and verify the `0.0.0.0/0` route now shows a `NatGatewayId`.

---

### Cause E: Recursive invocation loop

**Statement:** The function triggers itself recursively (e.g., by writing to the same S3 bucket or SQS queue that invokes it), causing concurrency to spike to the account limit and all invocations to time out.

**Mechanism:** Lambda automatically detects recursive loops for some services, but not all trigger patterns. When a function writes an object to the S3 bucket configured as its own trigger, or sends a message to the SQS queue that invokes it, each invocation spawns new invocations exponentially. Concurrency reaches the account limit within seconds. Each execution waits for downstream invocations that are themselves throttled, producing cascading timeouts. Cost accumulates rapidly.

**Indicator:**

- [Step 8] `Maximum` concurrent executions is at or near the account concurrency limit (default 1000)
- [Symptom] Lambda `Throttles` metric is also elevated simultaneously with `Errors`
- [Step 2] Function logs show the same event payload pattern repeating across multiple log streams

<!-- match: {"step": 8, "predicate": "threshold", "target": "Maximum", "op": ">", "value": 800} -->

**Mitigation:**

- **Risk:** Setting concurrency to 0 stops all invocations including legitimate traffic; apply only when a recursive loop is confirmed.
- **Command:**

  ```bash
  # Stop the cascade immediately
  aws lambda put-function-concurrency \
    --function-name my-function \
    --reserved-concurrent-executions 0
  ```

- **Duration:** Apply until the root trigger configuration is corrected; restore within 30 minutes.

**Resolution:**

```bash
# Fix the trigger (example: separate S3 input and output buckets)
# Remove the self-referential trigger
aws lambda remove-permission \
  --function-name my-function \
  --statement-id s3-trigger-self

# Restore concurrency after fixing the trigger
aws lambda delete-function-concurrency --function-name my-function
```

**Verification:** After restoring concurrency, confirm `ConcurrentExecutions` stays at expected levels and no recursive invocations appear in CloudWatch Logs Insights.

---

### Cause Z: Unidentified timeout cause

**Statement:** The timeout root cause cannot be determined from available logs, metrics, or traces.

**Mechanism:** The timeout may result from an uncommon trigger (EFS mount latency, infrequent SDK credential refresh, or a runtime-specific bug) not covered by the above causes. Additional instrumentation is required.

**Indicator:**

- [Default] Steps 1–8 did not conclusively match any cause above

**Mitigation:**

- **Risk:** Adding X-Ray and structured logging has minimal performance overhead (<1 ms per invocation) but may expose sensitive data in traces.
- **Command:**

  ```bash
  # Enable active tracing and add structured timing logs to the function
  aws lambda update-function-configuration \
    --function-name my-function \
    --tracing-config Mode=Active \
    --environment Variables={LOG_LEVEL=DEBUG}
  ```

- **Duration:** Collect data for at least 1 hour before escalating.

**Resolution:** Out of runbook scope — escalate to AWS Support with the X-Ray trace ID, CloudWatch Logs Insights query results, and function configuration export (`aws lambda get-function --function-name my-function`).

**Verification:** Timeout is no longer occurring after AWS Support resolution.

## Prevention

Set the function timeout to 2–3x the observed P99 duration, not an arbitrary value:

```bash
aws logs start-query \
  --log-group-name /aws/lambda/my-function \
  --start-time $(date -d '7 days ago' +%s) \
  --end-time $(date +%s) \
  --query-string 'filter @type = "REPORT" | stats pct(@duration, 99) as p99Ms, pct(@duration, 999) as p999Ms'
```

Add explicit client-side timeouts to every SDK and HTTP call shorter than the function timeout. This ensures the function fails fast with a meaningful error rather than silently timing out.

Set a CloudWatch alarm on p99 Duration to catch latency regression before it causes timeouts:

```bash
aws cloudwatch put-metric-alarm \
  --alarm-name "lambda-duration-p99-high-my-function" \
  --metric-name Duration \
  --namespace AWS/Lambda \
  --dimensions Name=FunctionName,Value=my-function \
  --extended-statistic p99 \
  --period 300 \
  --evaluation-periods 3 \
  --threshold 10000 \
  --comparison-operator GreaterThanThreshold \
  --alarm-actions arn:aws:sns:us-east-1:123456789012:ops-alerts
```

Keep deployment packages small (remove dev dependencies, use Lambda Layers for shared libraries) to reduce Init phase duration for cold starts.

Use provisioned concurrency for latency-sensitive functions with predictable traffic patterns to eliminate cold start overhead entirely.

For workloads that may exceed 15 minutes, redesign to use AWS Step Functions, SQS + worker Lambda, or ECS Fargate instead of increasing the Lambda timeout.

## Sources

- [AWS Lambda: Troubleshoot invocation issues](https://docs.aws.amazon.com/lambda/latest/dg/troubleshooting-invocation.html) — Priority 1. Official troubleshooting guide covering `Sandbox.Timedout`, recursive loop detection, provisioned concurrency spillover, and VPC Pending states.
- [AWS Lambda: Configuring functions](https://docs.aws.amazon.com/lambda/latest/dg/configuration-function-common.html) — Priority 1. Timeout configuration reference including 1–900 s range, memory-to-CPU allocation, and runtime options.
- [AWS Lambda: VPC access configuration](https://docs.aws.amazon.com/lambda/latest/dg/configuration-vpc.html) — Priority 1. Hyperplane ENI lifecycle, subnet IP requirements, internet access via NAT Gateway, and VPC endpoint configuration for AWS services.
