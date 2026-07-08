---
id: "lambda-invocation-errors"
title: "AWS Lambda Runtime Invocation Errors"
domain: compute
service: aws-lambda
symptom_class: [oom, auth_failure, connection_refused]
severity: high
scope: global
version: "2.0.0"
last_updated: "2026-06-25"
verified_by: "kb-researcher"
status: draft
tags: [aws, lambda, serverless, runtime-error, oom, vpc, iam, handler, concurrency, throttling]
difficulty: intermediate
---

## Symptom Recognition

Lambda function invocations return a non-200 HTTP status code, or a 200 response with a `FunctionError` header and JSON error body. CloudWatch `Errors` metric is non-zero. Common presentations:

```text
REPORT RequestId: abc-123 Duration: 3000.00 ms Billed Duration: 3000 ms Memory Size: 128 MB Max Memory Used: 129 MB
RequestId: abc-123 Error: Runtime exited with error: signal: killed
Runtime.ExitError
```

```text
[ERROR] Runtime.ImportModuleError: Unable to import module 'handler': No module named 'handler'
```

```text
[ERROR] Runtime.HandlerNotFound: handler is undefined or not exported
```

```text
[ERROR] ClientError: An error occurred (AccessDeniedException) when calling the PutItem operation: User: arn:aws:iam::123456789012:role/my-function-role is not authorized to perform: dynamodb:PutItem
```

```text
[ERROR] EndpointConnectionError: Could not connect to the endpoint URL: "https://dynamodb.us-east-1.amazonaws.com/"
```

```text
TooManyRequestsException: Rate exceeded
```

```text
Couldn't find valid bootstrap(s): [/var/task/bootstrap /opt/bootstrap]
Runtime.InvalidEntrypoint
```

## Applicability

Applies to all AWS Lambda runtimes (Python 3.x, Node.js 18+, Java 11/17/21, .NET 8, Go 1.x, Ruby 3.x, custom `provided.al2023`) deployed as .zip archives or container images in any AWS region. Requires AWS CLI v2 with permissions: `lambda:GetFunction`, `lambda:GetFunctionConfiguration`, `lambda:InvokeFunction`, `logs:StartQuery`, `logs:GetQueryResults`, `iam:SimulatePrincipalPolicy`, and `cloudwatch:GetMetricStatistics`. For VPC-connected functions also requires `ec2:DescribeSubnets`, `ec2:DescribeRouteTables`, `ec2:DescribeVpcs`.

## Diagnostic Steps

### Step 1: Retrieve the most recent error logs

```bash
QUERY_ID=$(aws logs start-query \
  --log-group-name /aws/lambda/my-function \
  --start-time $(date -d '1 hour ago' +%s) \
  --end-time $(date +%s) \
  --query-string 'filter @message like /ERROR|Error|Exception|killed|ImportModule|HandlerNotFound|InvalidEntrypoint/ | sort @timestamp desc | limit 30' \
  --query 'queryId' --output text)
sleep 5
aws logs get-query-results --query-id "$QUERY_ID" \
  --query 'results[*][?field==`@message`].value' --output text
```

Expected output: Error strings identifying the failure class — `Runtime.ImportModuleError`, `Runtime.HandlerNotFound`, `Runtime.ExitError signal: killed`, `AccessDeniedException`, `EndpointConnectionError`, `TooManyRequestsException`, or `Runtime.InvalidEntrypoint`.

### Step 2: Check memory utilisation against the configured limit

```bash
QUERY_ID=$(aws logs start-query \
  --log-group-name /aws/lambda/my-function \
  --start-time $(date -d '1 hour ago' +%s) \
  --end-time $(date +%s) \
  --query-string 'filter @type="REPORT" | stats max(@maxMemoryUsed/1000000) as maxMemMB, max(@memorySize/1000000) as limitMB by bin(5m)' \
  --query 'queryId' --output text)
sleep 5
aws logs get-query-results --query-id "$QUERY_ID"
```

Expected output: `maxMemMB` and `limitMB` columns per 5-minute bucket. If `maxMemMB` equals `limitMB`, the function is OOM-killing.

### Step 3: Inspect the function handler configuration

```bash
aws lambda get-function-configuration --function-name my-function \
  --query '{Handler:Handler,Runtime:Runtime,PackageType:PackageType,MemorySize:MemorySize}'
```

Expected output: `Handler` in the form `module.function` (Python: `handler.lambda_handler`, Node.js: `index.handler`, Java: `com.example.MyHandler::handleRequest`). For `provided.al2023` custom runtimes the handler field is used by the bootstrap; verify the bootstrap file exists at the ZIP root.

### Step 4: Verify the deployment package structure

```bash
LOC=$(aws lambda get-function --function-name my-function \
  --query 'Code.Location' --output text)
curl -sL "$LOC" -o /tmp/lambda.zip
unzip -l /tmp/lambda.zip | head -40
```

Expected output: The file named in the `Handler` field (e.g., `handler.py` for `handler.lambda_handler`, or `bootstrap` for `provided.al2023`) must appear at the ZIP root — not inside a subdirectory.

### Step 5: Simulate the execution role permissions

```bash
ROLE_ARN=$(aws lambda get-function-configuration --function-name my-function \
  --query 'Role' --output text)

# Replace action/resource with what the AccessDeniedException cited
aws iam simulate-principal-policy \
  --policy-source-arn "$ROLE_ARN" \
  --action-names dynamodb:PutItem \
  --resource-arns arn:aws:dynamodb:us-east-1:123456789012:table/my-table \
  --query 'EvaluationResults[*].{Action:EvalActionName,Decision:EvalDecision}'
```

Expected output: `EvalDecision` is `allowed`. Any `implicitDeny` or `explicitDeny` confirms the execution role lacks the required permission.

### Step 6: Inspect VPC routing for NAT Gateway or VPC endpoint

```bash
aws lambda get-function-configuration --function-name my-function \
  --query 'VpcConfig.{SubnetIds:SubnetIds,SecurityGroupIds:SecurityGroupIds}'

for SUBNET in $(aws lambda get-function-configuration --function-name my-function \
    --query 'VpcConfig.SubnetIds[]' --output text); do
  RTB=$(aws ec2 describe-route-tables \
    --filters "Name=association.subnet-id,Values=$SUBNET" \
    --query 'RouteTables[0].RouteTableId' --output text)
  echo "Subnet: $SUBNET  RouteTable: $RTB"
  aws ec2 describe-route-tables --route-table-ids "$RTB" \
    --query 'RouteTables[0].Routes[?DestinationCidrBlock==`0.0.0.0/0`]'
done
```

Expected output: Each subnet's default route (`0.0.0.0/0`) targets a NAT Gateway ID (`nat-xxxx`). If `VpcConfig.SubnetIds` is empty the function is not VPC-attached. If no NAT Gateway route exists, the function cannot reach public AWS endpoints.

### Step 7: Check concurrency limits and throttle count

```bash
aws lambda get-function-concurrency --function-name my-function

aws lambda get-account-settings \
  --query '{TotalConcurrency:AccountLimit.ConcurrentExecutions,Unreserved:AccountLimit.UnreservedConcurrentExecutions}'

aws cloudwatch get-metric-statistics --namespace AWS/Lambda \
  --metric-name Throttles \
  --dimensions Name=FunctionName,Value=my-function \
  --start-time $(date -d '1 hour ago' -u +%Y-%m-%dT%H:%M:%SZ) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%SZ) \
  --period 300 --statistics Sum
```

Expected output: `ReservedConcurrentExecutions` shows 0 only if the function is disabled. Non-zero `Throttles` sum confirms the function is being throttled.

### Step 8: Run a synchronous test invocation

```bash
aws lambda invoke \
  --function-name my-function \
  --payload '{"_test":true}' \
  --log-type Tail \
  --query 'LogResult' --output text output.json | base64 -d
cat output.json
```

Expected output: Decoded log tail shows the last 4 KB of execution output including REPORT line and any error. `FunctionError` key present in `output.json` confirms an execution-level failure. `StatusCode` 429 confirms throttling.

## Causes

### Cause A: Out of memory — function exceeds configured memory limit

**Statement:** The function exhausts its configured memory allocation, causing the Lambda runtime to send SIGKILL and terminate the execution environment before the handler returns.
**Chain:**
- root: function RSS memory reaches its configured hard ceiling during execution
- s1: runtime kills the process immediately with SIGKILL (signal 9), skipping cleanup handlers
- s2: invocation aborts and emits `Runtime.ExitError` with `signal: killed`
- D: invocation returns a non-200/FunctionError result (Symptom Recognition)
**Indicators:**
- root: [Step 2] `maxMemMB` equals `limitMB` in the REPORT query output
- s2: [Step 1] log contains `signal: killed` in the `Runtime.ExitError` line
**Interventions:**
- **remediation** (root): profile 24h peak usage and set memory to ~1.5× peak so RSS never reaches the ceiling.

  ```bash
  # Profile peak usage over 24 hours, then set memory to 1.5x peak
  QUERY_ID=$(aws logs start-query \
    --log-group-name /aws/lambda/my-function \
    --start-time $(date -d '24 hours ago' +%s) \
    --end-time $(date +%s) \
    --query-string 'filter @type="REPORT" | stats max(@maxMemoryUsed/1000000) as peakMB' \
    --query 'queryId' --output text)
  sleep 5
  aws logs get-query-results --query-id "$QUERY_ID"
  # Then set memory to ceil(peakMB * 1.5), rounded to nearest 64
  aws lambda update-function-configuration --function-name my-function --memory-size <calculated-value>
  ```

  **Verification:** Re-run Step 2 after 15 minutes of production traffic; `maxMemMB` should be ≤ 75% of new `limitMB`.
- **mitigation** (root): immediately double the memory size to lift the ceiling above current peak.

  ```bash
  CURRENT=$(aws lambda get-function-configuration --function-name my-function \
    --query 'MemorySize' --output text)
  NEW=$((CURRENT * 2))
  aws lambda update-function-configuration --function-name my-function --memory-size $NEW
  ```

  **Risk:** Doubling memory doubles cost-per-invocation proportionally; it also increases CPU, which may reduce duration and partly offset the cost. **Duration:** Immediate; applies to the next cold start. **Verification:** Re-run Step 2; `maxMemMB` no longer equals `limitMB`.

---

### Cause B: Handler not found — module path or export name mismatch

**Statement:** The `Handler` configuration field does not match the actual module file path or exported function name in the deployment package, so the runtime cannot locate the handler entry point.
**Chain:**
- root: `Handler` config (e.g. `handler.lambda_handler`) does not match a module/export present at the ZIP root
- s1: on cold start the runtime fails to import the module or resolve the exported attribute
- s2: runtime emits `Runtime.ImportModuleError` or `Runtime.HandlerNotFound` before the Invoke phase begins
- D: every invocation fails (Symptom Recognition)
**Indicators:**
- s2: [Step 1] log contains `Runtime.ImportModuleError` or `Runtime.HandlerNotFound`
- root: [Step 3] `Handler` value does not correspond to a file visible in Step 4's ZIP listing
**Interventions:**
- **remediation** (root): rebuild the package with the handler module at the ZIP root, redeploy, and set `Handler` to match.

  ```bash
  # Verify and redeploy (Python example)
  cd /path/to/project
  zip -r function.zip handler.py requirements/ site-packages/
  aws lambda update-function-code --function-name my-function --zip-file fileb://function.zip
  aws lambda update-function-configuration --function-name my-function --handler handler.lambda_handler
  ```

  **Verification:** Run Step 8; the response must not contain `FunctionError` and the log tail must not contain `ImportModuleError` or `HandlerNotFound`.
- **mitigation** (root): if the package is correct but the field is wrong, correct only the `Handler` configuration field.

  ```bash
  # Python example: file handler.py at ZIP root, function named lambda_handler
  aws lambda update-function-configuration --function-name my-function \
    --handler handler.lambda_handler

  # Node.js example: file index.js at ZIP root, export named handler
  aws lambda update-function-configuration --function-name my-function \
    --handler index.handler

  # Java example: fully qualified class with method
  aws lambda update-function-configuration --function-name my-function \
    --handler com.example.MyHandler::handleRequest
  ```

  **Risk:** Low — only changes the handler configuration field; no code is modified. **Duration:** Immediate. **Verification:** Run Step 8; log tail must not contain `ImportModuleError` or `HandlerNotFound`.

---

### Cause C: Missing bootstrap — invalid entrypoint for custom runtime

**Statement:** A `provided.al2023` custom-runtime function fails to start because the `bootstrap` executable is absent from or not at the root of the deployment package ZIP.
**Chain:**
- root: `bootstrap` executable is missing, in a subdirectory, or a symlink rather than a real binary at the ZIP root
- s1: Lambda's custom-runtime contract cannot find an executable at `/var/task/bootstrap`
- s2: runtime emits `Runtime.InvalidEntrypoint` / `Couldn't find valid bootstrap` before any handler code runs
- D: every invocation fails immediately (Symptom Recognition)
**Indicators:**
- s2: [Step 1] log contains `Runtime.InvalidEntrypoint` or `Couldn't find valid bootstrap`
- root: [Step 4] `bootstrap` is absent from the root of the ZIP listing or listed under a subdirectory path
**Interventions:**
- **remediation** (root): rebuild with an executable `bootstrap` (mode 755) at the ZIP root and redeploy.

  ```bash
  # Verify bootstrap is executable and at ZIP root
  unzip -l /tmp/lambda.zip | grep bootstrap
  # Must show: <size> bootstrap  (no leading path)

  # Rebuild: bootstrap must be at root, mode 755
  chmod 755 bootstrap
  zip -j function.zip bootstrap   # -j strips directory paths
  aws lambda update-function-code --function-name my-function --zip-file fileb://function.zip
  ```

  **Verification:** Run Step 8; the log tail must not contain `InvalidEntrypoint` or `Couldn't find valid bootstrap`.

---

### Cause D: Missing IAM permission — execution role denied downstream access

**Statement:** The function's IAM execution role lacks a required permission for a downstream AWS service, causing the SDK call to fail with `AccessDeniedException`.
**Chain:**
- root: execution role policy omits the required IAM action on the downstream resource
- s1: the function's SDK call to that service (DynamoDB, S3, SQS, etc.) is rejected with `AccessDeniedException`
- s2: the SDK exception propagates as an unhandled error, incrementing the `Errors` metric
- D: invocation returns a FunctionError result (Symptom Recognition)
**Indicators:**
- s1: [Step 1] log contains `AccessDeniedException` with the denied action name
- root: [Step 5] `EvalDecision` is `implicitDeny` or `explicitDeny` for that action
**Interventions:**
- **remediation** (root): generate a least-privilege policy from CloudTrail activity via IAM Access Analyzer and replace the inline policy.

  ```bash
  aws accessanalyzer start-policy-generation \
    --policy-generation-details '{
      "principalArn": "arn:aws:iam::123456789012:role/my-function-role",
      "cloudTrailDetails": {
        "trailArn": "arn:aws:cloudtrail:us-east-1:123456789012:trail/my-trail",
        "startTime": "2026-05-05T00:00:00Z",
        "endTime": "2026-05-12T00:00:00Z",
        "accessRole": "arn:aws:iam::123456789012:role/AccessAnalyzerRole"
      }
    }'
  ```

  **Verification:** Run Step 5; `EvalDecision` must be `allowed`. Then run Step 8 — `AccessDeniedException` must not appear in the log tail.
- **mitigation** (root): attach a narrowly scoped inline policy granting the specific action on the specific resource ARN.

  ```bash
  ROLE_NAME=$(aws lambda get-function-configuration --function-name my-function \
    --query 'Role' --output text | awk -F/ '{print $NF}')

  # Substitute the actual service, actions, and resource ARN
  aws iam put-role-policy --role-name "$ROLE_NAME" \
    --policy-name lambda-resource-access \
    --policy-document '{
      "Version": "2012-10-17",
      "Statement": [{
        "Effect": "Allow",
        "Action": ["dynamodb:PutItem","dynamodb:GetItem","dynamodb:Query"],
        "Resource": "arn:aws:dynamodb:us-east-1:123456789012:table/my-table"
      }]
    }'
  ```

  **Risk:** Medium — overly broad permissions widen the blast radius (role-wide; all functions sharing the role are affected); scope to the specific resource ARN and action. Rollback: `aws iam delete-role-policy --role-name $ROLE_NAME --policy-name lambda-resource-access`. **Duration:** Allow 60 seconds for IAM propagation, then re-test. **Verification:** Re-run Step 5; `EvalDecision` is `allowed`.

---

### Cause E: VPC-connected function lacks NAT Gateway or VPC endpoint for AWS services

**Statement:** A Lambda function attached to a VPC subnet that has no NAT Gateway route and no matching VPC endpoint cannot reach public AWS service endpoints, causing all outbound SDK calls to time out.
**Chain:**
- root: the function's VPC subnet route table has no `0.0.0.0/0` NAT Gateway route and no VPC endpoint for the target service
- s1: outbound packets to public AWS endpoints (e.g. `dynamodb.us-east-1.amazonaws.com`) have no valid route via the Hyperplane ENI (Lambda gets no public IP)
- s2: the SDK connection times out, emitting `EndpointConnectionError` / `ConnectTimeoutError`
- D: the invocation fails on the timed-out downstream call (Symptom Recognition)
**Indicators:**
- s2: [Step 1] log contains `EndpointConnectionError`, `ConnectTimeoutError`, or `Could not connect to the endpoint URL`
- root: [Step 6] subnet route table has no `0.0.0.0/0` route targeting a NAT Gateway
**Interventions:**
- **remediation** (root): add a NAT Gateway (public subnet) with a `0.0.0.0/0` route on the Lambda subnets, or remove the VPC config if no VPC resources are needed.

  ```bash
  # If the function does not need VPC resources at all, detach VPC config
  aws lambda update-function-configuration --function-name my-function \
    --vpc-config SubnetIds=[],SecurityGroupIds=[]
  ```

  **Verification:** Run Step 8; `EndpointConnectionError`/`ConnectTimeoutError` must not appear. Confirm via Step 6 the route table now has a `0.0.0.0/0` route or a service prefix-list entry.
- **mitigation** (root): add free Gateway VPC endpoints for DynamoDB/S3 to give those services a valid route without a NAT Gateway.

  ```bash
  VPC_ID=$(aws lambda get-function-configuration --function-name my-function \
    --query 'VpcConfig.VpcId' --output text)
  RTB_ID=rtb-xxxxxxxx   # Route table associated with the Lambda subnets

  # Free Gateway endpoints for DynamoDB and S3
  aws ec2 create-vpc-endpoint --vpc-id "$VPC_ID" \
    --service-name com.amazonaws.us-east-1.dynamodb \
    --route-table-ids "$RTB_ID"

  aws ec2 create-vpc-endpoint --vpc-id "$VPC_ID" \
    --service-name com.amazonaws.us-east-1.s3 \
    --route-table-ids "$RTB_ID"
  ```

  **Risk:** Low for free Gateway endpoints (S3/DynamoDB); the addition is VPC-wide for all resources using that route table. Rollback: `aws ec2 delete-vpc-endpoints --vpc-endpoint-ids <endpoint-id>`. **Duration:** VPC endpoints become active within 1–2 minutes. **Verification:** Run Step 8; the endpoint error must not appear for the covered service.

---

### Cause F: Concurrency throttling — reserved concurrency zero or account limit reached

**Statement:** Lambda rejects invocations with `TooManyRequestsException` because the function's reserved concurrency is set to 0 or the account-level concurrent execution limit is exhausted.
**Chain:**
- root: function reserved concurrency is 0, or the account unreserved concurrency pool (default 1000/region) is exhausted by other functions
- s1: Lambda throttles the invocation against the concurrency ceiling, returning HTTP 429 / `TooManyRequestsException`
- s2: async callers retry up to 2× and may overflow to a DLQ backlog or lose events
- D: invocation is rejected (Symptom Recognition)
**Indicators:**
- root: [Step 7] `ReservedConcurrentExecutions` is 0, or the CloudWatch `Throttles` metric sum is non-zero
- s1: [Step 8] `StatusCode` is 429 / `TooManyRequestsException` in the invoke response
**Interventions:**
- **remediation** (root): if the account limit is the constraint, request a concurrency quota increase.

  ```bash
  aws service-quotas request-service-quota-increase \
    --service-code lambda \
    --quota-code L-B99A9384 \
    --desired-value 3000
  ```

  **Verification:** Run Step 7; `Throttles` sum drops to 0. Re-run Step 8 — `StatusCode` must be 200.
- **mitigation** (root): raise the function's reserved concurrency from 0 to a safe value, or delete it to use the account pool.

  ```bash
  # If reserved concurrency is 0, raise it to a safe value
  aws lambda put-function-concurrency --function-name my-function \
    --reserved-concurrent-executions 50

  # Or delete the reserved concurrency (use account pool)
  aws lambda delete-function-concurrency --function-name my-function
  ```

  **Risk:** Removing the reserved cap may let the function consume all account concurrency, starving other functions. Rollback: `aws lambda put-function-concurrency --function-name my-function --reserved-concurrent-executions <original-value>`. **Duration:** Immediate. **Verification:** Re-run Step 7; `Throttles` sum is 0.

---

### Cause Z: Unidentified Lambda invocation failure

**Statement:** The invocation error does not match any identified pattern and requires deeper investigation or escalation.
**Chain:**
- root: an unrecognised failure mode (unhandled application exception, Init-phase timeout `Sandbox.Timedout`, KMS key issue, EFS/S3 mount failure, `Runtime.NodejsExit`, or transient AWS disruption)
- D: invocation fails without matching any known error string (Symptom Recognition)
**Indicators:**
- root: [Default] none of Causes A–F match the error strings observed in Steps 1 and 8
**Interventions:**
- **mitigation** (root): capture a full diagnostic snapshot (function state, X-Ray, Init-phase log scan) and escalate to AWS Support / the SME.

  ```bash
  # Check function state and last update status
  aws lambda get-function-configuration --function-name my-function \
    --query '{State:State,StateReason:StateReason,LastUpdateStatus:LastUpdateStatus,LastUpdateStatusReason:LastUpdateStatusReason}'

  # Enable X-Ray active tracing for deeper per-segment context
  aws lambda update-function-configuration --function-name my-function \
    --tracing-config Mode=Active

  # Check for Init-phase timeouts (Sandbox.Timedout) and other rare faults
  QUERY_ID=$(aws logs start-query \
    --log-group-name /aws/lambda/my-function \
    --start-time $(date -d '1 hour ago' +%s) \
    --end-time $(date +%s) \
    --query-string 'filter @message like /Sandbox|KMS|EFS|NodejsExit|Init/ | sort @timestamp desc | limit 20' \
    --query 'queryId' --output text)
  sleep 5
  aws logs get-query-results --query-id "$QUERY_ID"
  ```

  **Risk:** None — diagnostic only; no production change beyond enabling tracing. **Duration:** Diagnostic only; escalate with the function ARN, failing request IDs, and the CloudWatch output from Steps 1 and 8. **Verification:** Confirm the `Errors` metric returns to zero after the SME-identified fix is applied.

## Prevention

Configure a CloudWatch alarm on the `Errors` metric to detect failures within 5 minutes:

```bash
aws cloudwatch put-metric-alarm \
  --alarm-name "lambda-errors-my-function" \
  --metric-name Errors --namespace AWS/Lambda \
  --dimensions Name=FunctionName,Value=my-function \
  --statistic Sum --period 300 --evaluation-periods 1 \
  --threshold 1 --comparison-operator GreaterThanOrEqualToThreshold \
  --alarm-actions arn:aws:sns:us-east-1:123456789012:ops-alerts
```

Set memory to at least 1.5× the observed 24-hour peak from Step 2's REPORT query. Use the AWS Lambda Power Tuning tool (`github.com/alexcasalboni/aws-lambda-power-tuning`) to find the cost-optimal memory configuration.

Validate handler path in CI/CD before every deployment:

```bash
# Python: assert the handler module exists at ZIP root
unzip -l function.zip | grep -q "^.*handler\.py$" || { echo "handler.py missing at ZIP root"; exit 1; }
```

Generate execution role policies from real CloudTrail activity using IAM Access Analyzer rather than hand-crafting them. Review policies quarterly to remove unused permissions.

Configure a Dead Letter Queue (SQS) for async-invoked functions so failures are preserved for analysis:

```bash
aws lambda update-function-configuration --function-name my-function \
  --dead-letter-config TargetArn=arn:aws:sqs:us-east-1:123456789012:my-function-dlq
```

Enable AWS X-Ray active tracing to capture per-segment timing for Init and Invoke phases:

```bash
aws lambda update-function-configuration --function-name my-function \
  --tracing-config Mode=Active
```

For VPC-connected functions, prefer private subnets with a NAT Gateway over public subnets. Use free Gateway VPC endpoints for DynamoDB and S3 to eliminate NAT data-transfer costs on those services.

## Sources

- [AWS Lambda — Troubleshoot invocation issues](https://docs.aws.amazon.com/lambda/latest/dg/troubleshooting-invocation.html) (Priority 1) — Runtime error type catalog: `Runtime.ExitError`, `Runtime.InvalidEntrypoint`, `Sandbox.Timedout`, `Runtime.NodejsExit`, `ResourceConflictException`, EFS/S3 mount errors, throttle handling, invocation loop detection.
- [AWS Lambda — Troubleshoot execution issues](https://docs.aws.amazon.com/lambda/latest/dg/troubleshooting-execution.html) (Priority 1) — Memory and CPU constraints, downstream service unavailability, async handler pitfalls, JSON payload errors, X-Ray trace gaps.
- [AWS Lambda — Giving Lambda functions access to resources in an Amazon VPC](https://docs.aws.amazon.com/lambda/latest/dg/configuration-vpc.html) (Priority 1) — Hyperplane ENI lifecycle, NAT Gateway requirement, VPC endpoint options, required IAM permissions (`ec2:CreateNetworkInterface`, `AWSLambdaVPCAccessExecutionRole`), internet access routing rules.
