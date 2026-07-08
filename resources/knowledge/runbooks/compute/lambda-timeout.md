---
id: "lambda-timeout"
title: "AWS Lambda Function Timeout"
domain: compute
service: aws-lambda
symptom_class: [timeout, latency]
severity: high
scope: global
version: "2.0.0"
last_updated: "2026-06-25"
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

### Step 1: Retrieve timeout and memory configuration

```bash
aws lambda get-function-configuration \
  --function-name my-function \
  --query '{Timeout:Timeout,MemorySize:MemorySize,Runtime:Runtime,VpcConfig:VpcConfig}'
```

Expected output: JSON with `Timeout` (seconds), `MemorySize` (MB), runtime identifier, and `VpcConfig` (empty object if not VPC-attached).

### Step 2: Inspect recent log stream REPORT lines

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

### Step 3: Quantify cold-start frequency and init duration

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

### Step 5: Check VPC subnet IP availability

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

### Step 6: Verify internet routing for VPC functions

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

### Step 7: Inspect X-Ray subsegment durations

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

**Chain:**
- root: No warm execution environment is available, so Lambda must run a full Init phase (module imports, SDK client construction, global init).
- s1: The Init phase consumes a large share of the configured timeout budget, especially on short timeouts (≤3 s).
- s2: The handler (Invoke phase) starts with too little remaining time and cannot finish before the timeout fires.
- D: The invocation terminates with `Task timed out after X seconds` (see Symptom Recognition).

**Indicators:**
- root: [Step 2] `INIT_START` appears in the log stream and `Task timed out after` follows in the same invocation
- s1: [Step 3] `maxInitMs` is close to or exceeds the configured timeout in milliseconds
- s2: [Step 3] `coldStarts` count is high relative to `invocations` (e.g., >20% cold-start rate)

**Interventions:**
- **mitigation** (s1): Raise the timeout to buy time while initialization is optimized.

  ```bash
  aws lambda update-function-configuration \
    --function-name my-function \
    --timeout 30
  ```

  **Risk:** Increasing timeout raises maximum cost per invocation; does not fix the root cause. **Duration:** Immediate; buys time to optimize initialization code. **Verification:** Re-run Step 3; `maxInitMs` no longer approaches the timeout in milliseconds.
- **remediation** (root): Eliminate cold-start init via SnapStart (Java) and/or provisioned concurrency.

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

  Impact: Provisioned concurrency incurs continuous cost regardless of invocation volume; right-size to observed peak concurrency. Rollback: `aws lambda delete-provisioned-concurrency-config --function-name my-function --qualifier "$VERSION"`.

  **Verification:** Invoke 10 times and confirm `INIT_START` is absent from logs and `Duration` is consistently below 80% of the configured timeout.

---

### Cause B: Downstream service latency blocks the handler

**Statement:** A downstream dependency (database, external API, or AWS service) responds slowly or not at all, causing the handler to block until the Lambda timeout fires.

**Chain:**
- root: A downstream dependency (DB, external API, or AWS service) is degraded due to high load, cold connection pools, or misconfigured DNS.
- s1: The handler makes a synchronous network call whose client timeout exceeds the Lambda timeout (or has none), so it waits indefinitely.
- s2: The function consumes execution time while blocked on the call and never reaches its response logic.
- D: The function terminates with `Task timed out after X seconds` without producing output (see Symptom Recognition).

**Indicators:**
- root: [Step 7] X-Ray subsegment for a specific downstream call (DynamoDB, RDS, HTTP) shows duration equal to total trace duration
- s1: [Step 2] Application logs show the function reaching the downstream call but no response log after it
- s2: [Symptom] Duration in REPORT line equals the configured timeout consistently across invocations

**Interventions:**
- **defensive_fix** (s1): Add explicit client-side timeouts so the call fails fast instead of consuming the whole budget.

  ```bash
  # No CLI command — add SDK timeout in application code:
  # Python boto3 example:
  # from botocore.config import Config
  # config = Config(connect_timeout=3, read_timeout=8, retries={'max_attempts': 2})
  # client = boto3.client('dynamodb', config=config)
  echo "Deploy updated function code with explicit client timeouts"
  ```

  **Verification:** After deploy, confirm timeouts surface as a specific client error rather than a silent Lambda timeout, and `Duration` drops below the configured timeout.
- **remediation** (root): Fix the degraded dependency identified via X-Ray.

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

**Chain:**
- root: The function is allocated low memory (128–256 MB); Lambda allocates CPU linearly with memory, so it receives only a fraction of a vCPU.
- s1: CPU-bound work (JSON parsing, compression, image processing, crypto) runs 10–100x slower than at 1024 MB+, and GC runs more often under memory pressure.
- s2: The slow computation cannot complete within the configured timeout.
- D: The invocation terminates with `Task timed out after X seconds` (see Symptom Recognition).

**Indicators:**
- root: [Step 1] `MemorySize` is 128 or 256 MB
- s1: [Step 4] `peakMemBytes` is well below the `MemorySize` allocation (not memory-bound), yet `avgMs` is high
- s2: [Step 2] No downstream service call in logs immediately before timeout — function appears to time out inside CPU-bound code

**Interventions:**
- **mitigation** (s1): Raise memory to grant proportionally more CPU.

  ```bash
  aws lambda update-function-configuration \
    --function-name my-function \
    --memory-size 1024
  ```

  **Risk:** Increasing memory increases per-ms cost but typically reduces total invocation duration; net cost often decreases. **Duration:** Immediate. **Verification:** Re-run Step 4; `avgMs` drops and no longer approaches the timeout.
- **remediation** (root): Right-size memory with Lambda Power Tuning for the optimal cost/performance point.

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

  Impact: Memory change applies to all future invocations; no restart needed. Rollback: `aws lambda update-function-configuration --function-name my-function --memory-size 128`.

  **Verification:** After the memory increase, confirm `avgDurationMs` from Step 4 drops by at least 40% and no longer approaches the timeout.

---

### Cause D: VPC subnet IP exhaustion or missing NAT Gateway

**Statement:** A VPC-attached Lambda function either cannot create a Hyperplane ENI because its subnet is out of IPs, or cannot reach public endpoints because no NAT Gateway route exists in the subnet route table.

**Chain:**
- root: The function is VPC-attached, so Lambda must place its workload in the configured subnets and depend on their networking.
- s1: Either the subnet lacks free IPs for a Hyperplane ENI, OR the route table has no `0.0.0.0/0` route to a NAT Gateway / VPC endpoint.
- s2: New execution environments cannot be created, or outbound SDK/HTTP calls block with no path to their endpoint.
- D: The invocation terminates with `Task timed out after X seconds` (see Symptom Recognition).

**Indicators:**
- root: [Step 1] `VpcConfig.SubnetIds` is non-empty (function is VPC-attached)
- s1: [Step 5] `AvailableIPs` for one or more subnets is below 10
- s1: [Step 6] No route with `NatGatewayId` exists for `0.0.0.0/0` in the subnet route table

**Interventions:**
- **mitigation** (s1): Add gateway VPC endpoints so AWS-service calls bypass the missing NAT path at no hourly charge.

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

  **Risk:** Adding VPC endpoints incurs no hourly charge, but only covers the AWS services with endpoints — general internet calls still fail. **Duration:** VPC endpoints become active within 2 minutes. **Verification:** Invoke and confirm AWS-service calls complete; re-check Step 5/6.
- **remediation** (root): Provision a NAT Gateway and add the `0.0.0.0/0` route for general internet access.

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

**Chain:**
- root: The function writes to the same resource (S3 bucket / SQS queue) that is configured as its own trigger, creating a self-referential loop.
- s1: Each invocation spawns new invocations exponentially; concurrency reaches the account limit (default 1000) within seconds.
- s2: Executions wait on downstream invocations that are themselves throttled, producing cascading blockage.
- D: Invocations terminate with `Task timed out after X seconds`; `Throttles` rise alongside `Errors` (see Symptom Recognition).

**Indicators:**
- root: [Step 2] Function logs show the same event payload pattern repeating across multiple log streams
- s1: [Step 8] `Maximum` concurrent executions is at or near the account concurrency limit (default 1000)
- s2: [Symptom] Lambda `Throttles` metric is also elevated simultaneously with `Errors`

**Interventions:**
- **loop_break** (s1): Set reserved concurrency to 0 to stop the cascade immediately.

  ```bash
  # Stop the cascade immediately
  aws lambda put-function-concurrency \
    --function-name my-function \
    --reserved-concurrent-executions 0
  ```

  **Risk:** Setting concurrency to 0 stops all invocations including legitimate traffic; apply only when a recursive loop is confirmed. **Duration:** Apply until the root trigger configuration is corrected; restore within 30 minutes. **Verification:** Re-run Step 8; `Maximum` concurrency drops to 0.
- **remediation** (root): Remove the self-referential trigger and restore concurrency.

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

### Cause Z: Unidentified

**Statement:** The timeout root cause cannot be determined from available logs, metrics, or traces (e.g., EFS mount latency, infrequent SDK credential refresh, or a runtime-specific bug).

**Chain:**
- root: An uncommon trigger not covered by Causes A–E is producing the timeout, and current instrumentation does not reveal it.
- D: The invocation terminates with `Task timed out after X seconds` (see Symptom Recognition).

**Indicators:**
- root: [Default] Steps 1–8 did not conclusively match any cause above

**Interventions:**
- **mitigation** (D): Enable active tracing and verbose logging, capture a full diagnostic snapshot, then escalate to AWS Support / the SME.

  ```bash
  # Enable active tracing and add structured timing logs to the function
  aws lambda update-function-configuration \
    --function-name my-function \
    --tracing-config Mode=Active \
    --environment Variables={LOG_LEVEL=DEBUG}

  # Capture full configuration for escalation
  aws lambda get-function --function-name my-function > lambda-snapshot.json
  ```

  Escalate to AWS Support with the X-Ray trace ID, CloudWatch Logs Insights query results, and the function configuration export.

  **Risk:** Adding X-Ray and structured logging has minimal performance overhead (<1 ms per invocation) but may expose sensitive data in traces. **Duration:** Collect data for at least 1 hour before escalating. **Verification:** Timeout is no longer occurring after AWS Support resolution.

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
