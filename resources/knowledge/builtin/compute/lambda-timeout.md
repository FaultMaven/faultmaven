---
id: lambda-timeout
title: "AWS Lambda Function Timeout: Diagnosis and Resolution"
domain: compute
service: aws-lambda
symptom_class:
  - timeout
  - latency
severity: high
scope: global
version: "2.1.0"
last_updated: "2026-03-26"
verified_by: kb-researcher
status: draft
tags:
  - aws
  - lambda
  - timeout
  - serverless
  - cold-start
  - latency
  - memory
  - provisioned-concurrency
difficulty: intermediate
---

# AWS Lambda Function Timeout: Diagnosis and Resolution

## Problem Definition

This runbook applies to AWS Lambda functions in any supported runtime and any AWS region. You need the AWS CLI v2 with `lambda:GetFunction`, `lambda:InvokeFunction`, `logs:StartQuery`, and `cloudwatch:GetMetricStatistics` permissions. For VPC-connected functions, you also need `ec2:Describe*` permissions. The function's timeout can be configured from 1 second to 15 minutes (default 3 seconds).

A Lambda function exceeds its configured execution time limit and is forcibly terminated by the Lambda service. The invocation returns `Task timed out after X.XX seconds`. The function produces no return value, any work in progress is lost, and downstream callers (API Gateway, EventBridge, S3 triggers) receive a 5xx error or retry the invocation. The timeout includes both the Init phase (loading code, initializing SDK clients) and the Invoke phase (executing the handler).

The most frequent causes are: cold start overhead consuming most of the timeout budget (especially with large deployment packages or heavy initialization), downstream service latency (external APIs, databases, or AWS services responding slowly or being unreachable), insufficient memory allocation (Lambda allocates CPU proportional to memory, so low memory means slow computation), VPC connectivity issues (missing NAT Gateway or exhausted subnet IPs), synchronous blocking on sequential network calls without parallelism, and recursive invocation loops where the function inadvertently triggers itself.

**Typical error presentation:**

```text
REPORT RequestId: abc-123 Duration: 15000.00 ms Billed Duration: 15000 ms Memory Size: 128 MB Max Memory Used: 95 MB
Task timed out after 15.00 seconds
```

## Diagnostic Steps

### Step 1: Check the Current Timeout and Memory Configuration

**What this checks:** The function's configured limits, which establish the maximum execution time and available CPU.

```bash
aws lambda get-function-configuration --function-name my-function \
  --query '{Timeout:Timeout,MemorySize:MemorySize,Runtime:Runtime,VpcConfig:VpcConfig}'
```

**Expected output:** JSON with `Timeout` (seconds), `MemorySize` (MB), runtime, and VPC configuration.

**What the finding means:** A 128 MB function gets minimal CPU. A 3-second timeout leaves almost no room for cold starts. If the function is in a VPC, ENI attachment adds latency.

### Step 2: Analyze CloudWatch Logs for Duration Breakdown

**What this checks:** How the function spends its execution time, including Init phase duration and application-level timing.

```bash
# Get recent log events from the latest stream
STREAM=$(aws logs describe-log-streams --log-group-name /aws/lambda/my-function \
  --order-by LastEventTime --descending --limit 1 \
  --query 'logStreams[0].logStreamName' --output text)

aws logs get-log-events --log-group-name /aws/lambda/my-function \
  --log-stream-name "$STREAM" --limit 50
```

**Expected output:** Log events including `INIT_START` (cold start indicator), `REPORT` lines with `Duration` and `Init Duration`, and application-level logs before the timeout.

**What the finding means:** If `INIT_START` appears and `Init Duration` is a large fraction of the timeout, cold start overhead is the bottleneck. If application logs show the function reaching a specific downstream call before timing out, that call is the bottleneck.

### Step 3: Check for Cold Start Impact

**What this checks:** How frequently cold starts occur and how much time they add to invocations.

```bash
aws logs start-query --log-group-name /aws/lambda/my-function \
  --start-time $(date -d '1 hour ago' +%s) --end-time $(date +%s) \
  --query-string 'filter @type = "REPORT" | stats count() as invocations, sum(@initDuration > 0) as coldStarts by bin(5m)'
```

**Expected output:** Invocation counts and cold start counts per 5-minute window.

**What the finding means:** If a significant percentage of invocations are cold starts and `@initDuration` approaches the timeout, cold start overhead is the primary issue. Functions with infrequent invocations have the highest cold start rates.

### Step 4: Check Memory Usage vs Limit

**What this checks:** Whether the function is memory-constrained or CPU-constrained due to low memory allocation.

```bash
aws logs start-query --log-group-name /aws/lambda/my-function \
  --start-time $(date -d '1 hour ago' +%s) --end-time $(date +%s) \
  --query-string 'filter @type = "REPORT" | stats max(@maxMemoryUsed / 1000000) as maxMemMB, avg(@duration) as avgDurationMs'
```

**Expected output:** Peak memory usage and average duration.

**What the finding means:** If the function uses well below its memory limit but is slow, it is likely CPU-bound. Increasing memory (which increases CPU) will speed it up. If memory usage is near the limit, the garbage collector may run aggressively, adding latency.

### Step 5: Check for VPC Connectivity Issues

**What this checks:** Whether a VPC-connected function has proper network routing to downstream services.

```bash
# Check if function is in a VPC
aws lambda get-function-configuration --function-name my-function \
  --query 'VpcConfig.{SubnetIds:SubnetIds,SecurityGroupIds:SecurityGroupIds}'

# Check subnet IP availability
for subnet in $(aws lambda get-function-configuration --function-name my-function \
  --query 'VpcConfig.SubnetIds[]' --output text 2>/dev/null); do
  aws ec2 describe-subnets --subnet-ids $subnet \
    --query 'Subnets[0].{SubnetId:SubnetId,AvailableIps:AvailableIpAddressCount,CidrBlock:CidrBlock}'
done
```

**Expected output:** Subnet details with available IP counts.

**What the finding means:** VPC functions need a NAT Gateway to reach public endpoints. Low available IPs in subnets can cause ENI creation failures during scaling. Verify the subnet route table has a `0.0.0.0/0` route to a NAT Gateway.

### Step 6: Check Downstream Service Health

**What this checks:** Whether the services the function depends on are responsive.

```bash
# For DynamoDB
aws dynamodb describe-table --table-name my-table --query 'Table.TableStatus'

# For RDS
aws rds describe-db-instances --db-instance-identifier my-db \
  --query 'DBInstances[0].DBInstanceStatus'
```

**Expected output:** `ACTIVE` or `available` status for downstream services.

**What the finding means:** If a downstream service is degraded, the function blocks waiting for a response until the Lambda timeout expires. Enable X-Ray tracing (Step 7) to identify which specific call is slow.

### Step 7: Enable X-Ray Tracing for Request Breakdown

**What this checks:** A waterfall view of each downstream call with individual durations, identifying the specific bottleneck.

```bash
# Enable active tracing
aws lambda update-function-configuration --function-name my-function \
  --tracing-config Mode=Active

# Invoke the function
aws lambda invoke --function-name my-function --payload '{}' output.json

# View traces
aws xray get-trace-summaries --start-time $(date -d '5 minutes ago' +%s) \
  --end-time $(date +%s)
```

**Expected output:** Trace summaries showing each subsegment (DynamoDB, S3, HTTP calls) with individual durations.

**What the finding means:** The subsegment with the longest duration is the bottleneck. X-Ray shows whether latency comes from connection establishment, data transfer, or response processing.

## Mitigation

### Option 1: Increase the Timeout

The simplest immediate fix when the function's workload is legitimate but the timeout is too short.

- **Risk:** Low. Increases maximum execution time and therefore maximum cost per invocation. Does not fix the root cause if the function is stuck waiting on a dead service.
- **Command:**

  ```bash
  aws lambda update-function-configuration --function-name my-function --timeout 60
  ```

- **Verify:**

  ```bash
  aws lambda invoke --function-name my-function --payload '{}' output.json
  cat output.json
  ```

  The function should complete without timeout errors.
- **Duration:** Immediate. Configuration update takes effect within seconds.

### Option 2: Increase Memory (and CPU)

Use when the function is compute-bound or memory-constrained.

- **Risk:** Low-Medium. Increases cost per invocation proportionally. A function at 256 MB costs twice as much per ms as 128 MB, but may complete in less than half the time, reducing total cost.
- **Command:**

  ```bash
  aws lambda update-function-configuration --function-name my-function --memory-size 256
  ```

- **Verify:**

  ```bash
  aws lambda invoke --function-name my-function --payload '{}' output.json
  ```

  Duration should decrease. Use AWS Lambda Power Tuning to find the optimal memory setting.
- **Duration:** Immediate.

### Option 3: Enable Provisioned Concurrency

Eliminates cold start overhead by keeping pre-initialized execution environments warm.

- **Risk:** Medium. Incurs continuous cost for provisioned environments regardless of invocation volume. Only cost-effective for consistent traffic patterns.
- **Command:**

  ```bash
  # Publish a version (provisioned concurrency works on versions/aliases, not $LATEST)
  VERSION=$(aws lambda publish-version --function-name my-function \
    --query 'Version' --output text)

  # Set provisioned concurrency
  aws lambda put-provisioned-concurrency-config \
    --function-name my-function --qualifier $VERSION \
    --provisioned-concurrent-executions 10
  ```

- **Verify:**

  ```bash
  aws lambda get-provisioned-concurrency-config \
    --function-name my-function --qualifier $VERSION
  ```

  `Status` should be `READY`. Subsequent invocations should show no `INIT_START` in logs.
- **Duration:** Provisioned concurrency takes 1-5 minutes to allocate.

### Option 4: Add Timeouts to Downstream Calls

Prevents the function from waiting indefinitely for a slow downstream service.

- **Risk:** Low. Causes the function to fail fast with a specific error instead of timing out silently. Requires code changes.
- **Command:** Apply in application code:

  ```python
  # Python: set HTTP client timeout
  import requests
  response = requests.get("https://api.example.com/data", timeout=5)

  # AWS SDK (boto3): set client timeout
  import boto3
  from botocore.config import Config
  config = Config(connect_timeout=5, read_timeout=10)
  dynamodb = boto3.client('dynamodb', config=config)
  ```

- **Verify:** Deploy updated code and invoke. The function should return a specific timeout error from the downstream call rather than a Lambda-level timeout.
- **Duration:** Requires code deployment (minutes).

## Root Cause Resolution

**If** Step 3 shows high cold start frequency and `@initDuration` is a significant portion of the timeout **then** reduce initialization time:

```bash
# For Java functions: enable SnapStart to snapshot the initialized state
aws lambda update-function-configuration --function-name my-function \
  --snap-start ApplyOn=PublishedVersions
aws lambda publish-version --function-name my-function
```

Additional cold start reduction strategies: minimize deployment package size (remove dev dependencies, use Lambda Layers for shared code), use lazy initialization for SDK clients, avoid heavy imports at module level in Python, and for Java/JVM consider GraalVM native images.

**If** X-Ray traces (Step 7) show a specific downstream call consuming most of the duration **then** address the bottleneck: add indexes to slow database queries, implement caching (ElastiCache, DAX for DynamoDB), add circuit breakers for external APIs, or offload slow operations to SQS for asynchronous processing.

**If** Step 4 shows the function is not memory-constrained but execution is slow **then** the function is CPU-bound with insufficient CPU. Use the AWS Lambda Power Tuning tool:

```bash
# Deploy the Power Tuning Step Function (one-time setup)
# https://github.com/alexcasalboni/aws-lambda-power-tuning

aws stepfunctions start-execution \
  --state-machine-arn arn:aws:states:us-east-1:123456789012:stateMachine:powerTuningStateMachine \
  --input '{"lambdaARN":"arn:aws:lambda:us-east-1:123456789012:function:my-function","powerValues":[128,256,512,1024,2048],"num":20,"payload":"{}"}'
```

**If** Step 5 shows the function is in a VPC with no NAT Gateway route **then** add VPC endpoints for AWS services (free for gateway endpoints) or a NAT Gateway:

```bash
# Gateway endpoints for DynamoDB and S3 (no cost)
aws ec2 create-vpc-endpoint --vpc-id vpc-123 \
  --service-name com.amazonaws.us-east-1.dynamodb --route-table-ids rtb-123
aws ec2 create-vpc-endpoint --vpc-id vpc-123 \
  --service-name com.amazonaws.us-east-1.s3 --route-table-ids rtb-123
```

**If** CloudWatch shows the function invoking at maximum concurrency continuously **then** the function is triggering itself in a recursive loop. Immediately set concurrency to zero to stop the cascade:

```bash
aws lambda put-function-concurrency --function-name my-function \
  --reserved-concurrent-executions 0

# Fix the trigger configuration (e.g., use separate input/output S3 buckets)
# Then restore concurrency:
aws lambda delete-function-concurrency --function-name my-function
```

## Verification

After applying a fix, confirm the timeout is resolved:

```bash
# Invoke and check for successful completion
aws lambda invoke --function-name my-function --payload '{}' output.json
cat output.json
```

The invocation should return successfully with `StatusCode` 200. No `FunctionError` field should be present.

```bash
# Check duration is well below timeout
aws cloudwatch get-metric-statistics --namespace AWS/Lambda \
  --metric-name Duration --dimensions Name=FunctionName,Value=my-function \
  --start-time $(date -d '10 minutes ago' -u +%Y-%m-%dT%H:%M:%SZ) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%SZ) \
  --period 60 --statistics Average,Maximum
```

Average duration should be well below the configured timeout. Maximum duration should not equal the timeout.

```bash
# Verify error rate is zero
aws cloudwatch get-metric-statistics --namespace AWS/Lambda \
  --metric-name Errors --dimensions Name=FunctionName,Value=my-function \
  --start-time $(date -d '10 minutes ago' -u +%Y-%m-%dT%H:%M:%SZ) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%SZ) \
  --period 60 --statistics Sum
```

`Sum` should be 0 for all recent data points.

## Prevention

### Set Timeouts Based on Measured P99 Duration

Set the function timeout to 2-3x the observed P99 duration, not an arbitrary value:

```bash
aws logs start-query --log-group-name /aws/lambda/my-function \
  --start-time $(date -d '7 days ago' +%s) --end-time $(date +%s) \
  --query-string 'filter @type = "REPORT" | stats pct(@duration, 99) as p99ms'
```

### Add Client-Side Timeouts to All Downstream Calls

Every HTTP request, database query, and AWS SDK call should have an explicit timeout shorter than the function timeout. This ensures the function fails fast with a meaningful error rather than silently timing out.

### Use Power Tuning to Optimize Memory

Run the AWS Lambda Power Tuning tool periodically (especially after code changes) to find the memory setting that minimizes cost or latency. Over-provisioning memory often reduces total cost because the function completes faster.

### Monitor Duration Trends with Alarms

```bash
aws cloudwatch put-metric-alarm --alarm-name "lambda-duration-high-my-function" \
  --metric-name Duration --namespace AWS/Lambda \
  --dimensions Name=FunctionName,Value=my-function \
  --statistic p99 --period 300 --evaluation-periods 3 \
  --threshold 10000 --comparison-operator GreaterThanThreshold \
  --alarm-actions arn:aws:sns:us-east-1:123456789012:ops-alerts
```

### Keep Deployment Packages Small

Remove test dependencies, documentation, and unused libraries. Use Lambda Layers for shared code. Smaller packages reduce cold start time.

### Use Async Patterns for Long Operations

For workloads that may exceed 15 minutes or have unpredictable duration, offload to Step Functions, SQS + worker Lambda, or ECS Fargate tasks instead of synchronous Lambda invocations.

## Sources

- [AWS Lambda: Troubleshooting Invocation Issues](https://docs.aws.amazon.com/lambda/latest/dg/troubleshooting-invocation.html) - Official troubleshooting guide for Lambda timeout, concurrency, VPC, and runtime errors.
- [AWS Lambda: Troubleshooting Cold Starts](https://repost.aws/knowledge-center/lambda-cold-start) - Cold start diagnosis and reduction strategies including provisioned concurrency and SnapStart.
- [AWS Lambda: Configuring Function Timeout](https://docs.aws.amazon.com/lambda/latest/dg/configuration-function-common.html) - Timeout and memory configuration reference.
- [AWS Lambda: Using Lambda with VPC](https://docs.aws.amazon.com/lambda/latest/dg/configuration-vpc.html) - VPC networking for Lambda, including ENI management and NAT Gateway requirements.
- [AWS Lambda Power Tuning](https://github.com/alexcasalboni/aws-lambda-power-tuning) - Open-source tool for optimizing Lambda memory and cost.
