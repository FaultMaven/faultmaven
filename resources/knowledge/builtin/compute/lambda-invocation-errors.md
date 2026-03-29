---
id: lambda-invocation-errors
title: "AWS Lambda Invocation Errors: Diagnosis and Resolution"
domain: compute
service: aws-lambda
symptom_class:
  - oom
  - auth_failure
  - connection_refused
severity: high
scope: global
version: "2.1.0"
last_updated: "2026-03-26"
verified_by: kb-researcher
status: draft
tags:
  - aws
  - lambda
  - invocation
  - runtime-error
  - out-of-memory
  - handler
  - permissions
  - vpc
difficulty: intermediate
---

# AWS Lambda Invocation Errors: Diagnosis and Resolution

## Problem Definition

This runbook applies to AWS Lambda functions using any supported runtime (Python, Node.js, Java, .NET, Go, Ruby, or custom runtimes via `provided.al2023`) in any AWS region. You need the AWS CLI v2 with `lambda:GetFunction`, `lambda:InvokeFunction`, `logs:StartQuery`, and `iam:SimulatePrincipalPolicy` permissions. For VPC-connected functions, you also need `ec2:Describe*` permissions to inspect subnet and security group configurations.

A Lambda function invocation fails with a runtime error before completing normally. The function crashes, throws an unhandled exception, exhausts its memory, cannot find the handler, lacks permissions to access a downstream AWS resource, or cannot reach a downstream service due to VPC connectivity issues. These errors increment the `Errors` CloudWatch metric and return a `FunctionError` field in synchronous invocation responses.

The most frequent causes are: out of memory (function exceeds configured memory limit, runtime killed with signal 9), handler not found (`Runtime.ImportModuleError` or `Runtime.HandlerNotFound` from mismatched handler configuration), unhandled application exceptions, missing IAM permissions on the execution role (`AccessDeniedException` from downstream AWS services), VPC connectivity failure (missing NAT Gateway or VPC endpoints), concurrency throttling (`TooManyRequestsException`), and dependency initialization failure during the Init phase.

**Typical error presentation:**

```text
REPORT RequestId: abc-123 Duration: 2500.00 ms Billed Duration: 2500 ms Memory Size: 128 MB Max Memory Used: 129 MB
RequestId: abc-123 Error: Runtime exited with error: signal: killed
Runtime.ExitError
```

```text
[ERROR] Runtime.ImportModuleError: Unable to import module 'handler': No module named 'handler'
```

```text
[ERROR] ClientError: An error occurred (AccessDeniedException) when calling the PutItem operation
```

## Diagnostic Steps

### Step 1: Identify the Error Type from CloudWatch Logs

**What this checks:** Which category of error the function is experiencing, directing you to the correct resolution path.

```bash
# Get recent error logs
aws logs start-query --log-group-name /aws/lambda/my-function \
  --start-time $(date -d '1 hour ago' +%s) --end-time $(date +%s) \
  --query-string 'filter @message like /ERROR|Error|Exception|killed/ | sort @timestamp desc | limit 20'
```

Wait a few seconds then fetch results:

```bash
aws logs get-query-results --query-id <query-id-from-above>
```

**Expected output:** Error messages from recent invocations.

**What the finding means:** `Runtime.ExitError` with `signal: killed` indicates OOM. `Runtime.ImportModuleError` or `Runtime.HandlerNotFound` indicates handler misconfiguration. `AccessDeniedException` indicates missing IAM permissions. `ConnectionError` or `EndpointConnectionError` indicates network/VPC issues.

### Step 2: Check for Out of Memory

**What this checks:** Whether the function's peak memory usage reached or exceeded the configured limit.

```bash
aws logs start-query --log-group-name /aws/lambda/my-function \
  --start-time $(date -d '1 hour ago' +%s) --end-time $(date +%s) \
  --query-string 'filter @type = "REPORT" | stats max(@maxMemoryUsed / 1000000) as maxMemMB, max(@memorySize / 1000000) as limitMB'
```

**Expected output:** `maxMemMB` and `limitMB` values.

**What the finding means:** If `maxMemMB` equals or exceeds `limitMB`, the function hit the memory limit. The `signal: killed` message in logs confirms an OOM kill by the Lambda runtime.

### Step 3: Check Handler Configuration

**What this checks:** Whether the handler path in the function configuration matches the actual module and function in the deployment package.

```bash
aws lambda get-function-configuration --function-name my-function \
  --query '{Handler:Handler,Runtime:Runtime,PackageType:PackageType}'
```

**Expected output:** Handler in the format `module.function` (e.g., `handler.lambda_handler` for Python, `index.handler` for Node.js).

**What the finding means:** If the handler does not match the deployment package structure, the runtime cannot find the entry point. Verify by listing files in the deployment package:

```bash
aws lambda get-function --function-name my-function --query 'Code.Location' --output text \
  | xargs curl -s -o /tmp/lambda.zip
unzip -l /tmp/lambda.zip | head -30
```

### Step 4: Check Execution Role Permissions

**What this checks:** Whether the function's IAM execution role has the permissions required to access downstream AWS services.

```bash
# Get the execution role ARN
ROLE_ARN=$(aws lambda get-function-configuration --function-name my-function \
  --query 'Role' --output text)

# List attached policies
ROLE_NAME=$(echo $ROLE_ARN | awk -F/ '{print $NF}')
aws iam list-attached-role-policies --role-name $ROLE_NAME

# Simulate a specific action to test permissions
aws iam simulate-principal-policy --policy-source-arn $ROLE_ARN \
  --action-names dynamodb:PutItem \
  --resource-arns arn:aws:dynamodb:us-east-1:123456789012:table/my-table
```

**Expected output:** The simulation `EvalDecision` should be `allowed`.

**What the finding means:** `implicitDeny` or `explicitDeny` means the role lacks the required permission. The `AccessDeniedException` in logs will specify exactly which action and resource were denied.

### Step 5: Check VPC Configuration and Connectivity

**What this checks:** Whether a VPC-connected function can reach AWS services and the internet.

```bash
# Get VPC config
aws lambda get-function-configuration --function-name my-function \
  --query 'VpcConfig.{SubnetIds:SubnetIds,SecurityGroupIds:SecurityGroupIds}'

# If in a VPC, check that subnets have NAT Gateway routes
for subnet in $(aws lambda get-function-configuration --function-name my-function \
  --query 'VpcConfig.SubnetIds[]' --output text); do
  RTB=$(aws ec2 describe-route-tables \
    --filters "Name=association.subnet-id,Values=$subnet" \
    --query 'RouteTables[0].RouteTableId' --output text)
  echo "Subnet: $subnet  RouteTable: $RTB"
  aws ec2 describe-route-tables --route-table-ids $RTB \
    --query 'RouteTables[0].Routes[?DestinationCidrBlock==`0.0.0.0/0`]'
done
```

**Expected output:** A route to a NAT Gateway (`nat-xxxx`) for each subnet, or VPC endpoints for the required AWS services.

**What the finding means:** VPC-connected functions need either a NAT Gateway route or VPC endpoints to reach AWS services. Without either, SDK calls time out with `EndpointConnectionError` or `ConnectTimeoutError`.

### Step 6: Check Concurrency and Throttling

**What this checks:** Whether the function is being throttled due to concurrency limits.

```bash
# Check reserved concurrency
aws lambda get-function-concurrency --function-name my-function

# Check account-level concurrency limits
aws lambda get-account-settings \
  --query '{ConcurrentExecutions:AccountLimit.ConcurrentExecutions,UnreservedConcurrency:AccountLimit.UnreservedConcurrentExecutions}'

# Check throttle metrics
aws cloudwatch get-metric-statistics --namespace AWS/Lambda \
  --metric-name Throttles --dimensions Name=FunctionName,Value=my-function \
  --start-time $(date -d '1 hour ago' -u +%Y-%m-%dT%H:%M:%SZ) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%SZ) \
  --period 60 --statistics Sum
```

**Expected output:** Concurrency settings and throttle counts.

**What the finding means:** Non-zero `Throttles` means invocations are being rejected. If reserved concurrency is set to 0, the function is effectively disabled. If the account limit is reached, all functions compete for capacity.

### Step 7: Test Invocation with Diagnostic Payload

**What this checks:** The exact error response and log output from a synchronous invocation.

```bash
aws lambda invoke --function-name my-function \
  --payload '{"test": true}' \
  --log-type Tail output.json \
  --query 'LogResult' --output text | base64 -d
```

**Expected output:** The last 4 KB of CloudWatch Logs output from the invocation, including any error details and the REPORT line.

**What the finding means:** The decoded log tail shows the exact error, stack trace, and memory usage. If `FunctionError` is present in the response, the invocation failed at the application level.

## Mitigation

### Option 1: Increase Memory for OOM Errors

- **Risk:** Low. Increases cost per invocation proportionally. Lambda allocates CPU proportional to memory, so this also speeds up compute-bound functions.
- **Command:**

  ```bash
  CURRENT_MEM=$(aws lambda get-function-configuration --function-name my-function \
    --query 'MemorySize' --output text)
  NEW_MEM=$((CURRENT_MEM * 2))
  aws lambda update-function-configuration --function-name my-function --memory-size $NEW_MEM
  ```

- **Verify:**

  ```bash
  aws lambda invoke --function-name my-function --payload '{}' output.json
  cat output.json
  ```

  The invocation should succeed. Check the REPORT log line: `Max Memory Used` should be well below the new `Memory Size`.
- **Duration:** Immediate.

### Option 2: Fix Handler Configuration

- **Risk:** Low. Only updates the function configuration to point to the correct handler.
- **Command:**

  ```bash
  # For Python: module_name.function_name
  aws lambda update-function-configuration --function-name my-function \
    --handler app.lambda_handler

  # For Node.js: file_name.export_name
  aws lambda update-function-configuration --function-name my-function \
    --handler index.handler
  ```

- **Verify:**

  ```bash
  aws lambda invoke --function-name my-function --payload '{}' output.json
  cat output.json
  ```

  The `Runtime.ImportModuleError` or `Runtime.HandlerNotFound` error should no longer appear.
- **Duration:** Immediate.

### Option 3: Add Missing IAM Permissions

- **Risk:** Medium. Granting overly broad permissions creates security risk. Use the least-privilege principle and scope to specific resources.
- **Command:**

  ```bash
  ROLE_NAME=$(aws lambda get-function-configuration --function-name my-function \
    --query 'Role' --output text | awk -F/ '{print $NF}')

  aws iam put-role-policy --role-name $ROLE_NAME \
    --policy-name lambda-resource-access \
    --policy-document '{
      "Version": "2012-10-17",
      "Statement": [{
        "Effect": "Allow",
        "Action": ["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:Query"],
        "Resource": "arn:aws:dynamodb:us-east-1:123456789012:table/my-table"
      }]
    }'
  ```

- **Verify:**

  ```bash
  # Wait ~10 seconds for IAM propagation, then invoke
  aws lambda invoke --function-name my-function --payload '{}' output.json
  cat output.json
  ```

  The `AccessDeniedException` should no longer appear.
- **Duration:** IAM policy changes propagate within 10-60 seconds.

### Option 4: Add VPC Endpoint or NAT Gateway

- **Risk:** Medium. NAT Gateway incurs hourly cost (~$0.045/hr per AZ) plus data transfer charges. Gateway VPC endpoints for S3 and DynamoDB are free.
- **Command:**

  ```bash
  # Add free VPC endpoints for S3 and DynamoDB
  aws ec2 create-vpc-endpoint --vpc-id vpc-123 \
    --service-name com.amazonaws.us-east-1.dynamodb \
    --route-table-ids rtb-123

  aws ec2 create-vpc-endpoint --vpc-id vpc-123 \
    --service-name com.amazonaws.us-east-1.s3 \
    --route-table-ids rtb-123
  ```

- **Verify:**

  ```bash
  aws lambda invoke --function-name my-function --payload '{}' output.json
  cat output.json
  ```

  `EndpointConnectionError` or `ConnectTimeoutError` should no longer appear.
- **Duration:** VPC endpoints activate within 1-2 minutes.

## Root Cause Resolution

**If** Step 2 confirms OOM (max memory equals limit, `signal: killed` in logs) **then** investigate memory usage within the function. Add memory profiling to identify growth:

```python
# Python: add to handler for diagnostics
import tracemalloc
tracemalloc.start()

def lambda_handler(event, context):
    # ... function logic ...
    current, peak = tracemalloc.get_traced_memory()
    print(f"Current memory: {current / 1024 / 1024:.1f} MB, Peak: {peak / 1024 / 1024:.1f} MB")
    tracemalloc.stop()
```

Common fixes: stream large files instead of loading entirely into memory, process records in smaller batches (reduce SQS batch size), reuse SDK clients across invocations (declare outside the handler), and for Node.js check for unresolved promises that accumulate closures.

**If** memory grows with each invocation on the same execution environment **then** there is a memory leak in global/module-scope objects that persist across warm invocations. Identify objects that grow and reset them between invocations.

**If** Step 3 shows a handler mismatch **then** fix the configuration to match the deployment package structure. Common mistakes: Python handler file nested in a subdirectory instead of at ZIP root, Node.js file not exporting the handler function, Java missing fully qualified class name (`com.example.MyHandler::handleRequest`).

**If** Step 4 shows `implicitDeny` for a required action **then** use IAM Access Analyzer to generate a least-privilege policy from actual CloudTrail activity:

```bash
aws accessanalyzer start-policy-generation \
  --policy-generation-details '{
    "principalArn": "arn:aws:iam::123456789012:role/my-lambda-role",
    "cloudTrailDetails": {
      "trailArn": "arn:aws:cloudtrail:us-east-1:123456789012:trail/my-trail",
      "startTime": "2026-03-17T00:00:00Z",
      "endTime": "2026-03-26T00:00:00Z",
      "accessRole": "arn:aws:iam::123456789012:role/AccessAnalyzerRole"
    }
  }'
```

**If** Step 5 shows no NAT Gateway route and the function needs to reach public endpoints **then** evaluate three options: remove VPC configuration if the function does not need VPC resources (`aws lambda update-function-configuration --function-name my-function --vpc-config SubnetIds=[],SecurityGroupIds=[]`), add free gateway VPC endpoints for AWS services, or add a NAT Gateway in a public subnet.

**If** Step 6 shows throttles **then** request a concurrency limit increase or optimize the function to reduce concurrent execution count:

```bash
aws service-quotas request-service-quota-increase \
  --service-code lambda --quota-code L-B99A9384 --desired-value 3000
```

## Verification

After applying fixes, confirm the function invokes successfully:

```bash
# Test invocation
aws lambda invoke --function-name my-function --payload '{"test": true}' output.json
cat output.json
```

The response should contain the expected output without a `FunctionError` field.

```bash
# Check error metrics are zero
aws cloudwatch get-metric-statistics --namespace AWS/Lambda \
  --metric-name Errors --dimensions Name=FunctionName,Value=my-function \
  --start-time $(date -d '10 minutes ago' -u +%Y-%m-%dT%H:%M:%SZ) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%SZ) \
  --period 60 --statistics Sum
```

Error count should be 0 for recent periods.

```bash
# Verify memory headroom
aws logs start-query --log-group-name /aws/lambda/my-function \
  --start-time $(date -d '10 minutes ago' +%s) --end-time $(date +%s) \
  --query-string 'filter @type = "REPORT" | stats max(@maxMemoryUsed / 1000000) as usedMB, max(@memorySize / 1000000) as limitMB'
```

`usedMB` should be at most 80% of `limitMB` for adequate headroom.

## Prevention

### Right-Size Memory Allocation

Use the AWS Lambda Power Tuning tool to find the optimal memory setting. Set memory to at least 1.5x the observed peak usage to accommodate load spikes and garbage collection overhead.

### Use IAM Access Analyzer for Least-Privilege Policies

Generate execution role policies from actual CloudTrail activity rather than guessing required permissions. Review policies quarterly to remove unused permissions.

### Test Locally Before Deploying

Use AWS SAM CLI or the Lambda runtime interface emulator to test functions locally with realistic payloads:

```bash
sam local invoke MyFunction -e events/test-event.json
```

### Monitor Error Rate with Alarms

```bash
aws cloudwatch put-metric-alarm --alarm-name "lambda-errors-my-function" \
  --metric-name Errors --namespace AWS/Lambda \
  --dimensions Name=FunctionName,Value=my-function \
  --statistic Sum --period 300 --evaluation-periods 2 \
  --threshold 5 --comparison-operator GreaterThanThreshold \
  --alarm-actions arn:aws:sns:us-east-1:123456789012:ops-alerts
```

### Validate Handler Configuration in CI/CD

Add a pre-deployment check that verifies the handler path matches a file in the deployment package. For container images, verify the ENTRYPOINT is correct with a local test.

### Use Dead Letter Queues for Async Functions

Configure a DLQ (SQS or SNS) for asynchronous invocations so failed events are preserved for analysis rather than silently dropped after retry exhaustion:

```bash
aws lambda update-function-configuration --function-name my-function \
  --dead-letter-config TargetArn=arn:aws:sqs:us-east-1:123456789012:my-function-dlq
```

## Sources

- [AWS Lambda: Troubleshooting Invocation Issues](https://docs.aws.amazon.com/lambda/latest/dg/troubleshooting-invocation.html) - Official guide covering runtime errors, IAM issues, VPC connectivity, concurrency throttling, and EFS mount failures.
- [AWS Lambda: Troubleshooting Execution Issues](https://docs.aws.amazon.com/lambda/latest/dg/troubleshooting-execution.html) - Runtime exit errors, Node.js async handler issues, and memory exhaustion troubleshooting.
- [AWS Lambda: Execution Role](https://docs.aws.amazon.com/lambda/latest/dg/lambda-intro-execution-role.html) - IAM execution role configuration and required permissions.
- [AWS Lambda: Using Lambda with VPC](https://docs.aws.amazon.com/lambda/latest/dg/configuration-vpc.html) - VPC networking, ENI management, and connectivity requirements.
- [AWS IAM Access Analyzer: Policy Generation](https://docs.aws.amazon.com/IAM/latest/UserGuide/access-analyzer-policy-generation.html) - Generate least-privilege policies from CloudTrail activity.
