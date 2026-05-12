---
id: "lambda-invocation-errors"
title: "AWS Lambda Runtime Invocation Errors"
domain: compute
service: aws-lambda
symptom_class: [oom, auth_failure, connection_refused]
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-05-12"
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

**Statement:** The function exhausts its configured memory allocation and the Lambda runtime sends SIGKILL (signal 9), terminating the execution environment before the handler returns.
**Mechanism:** Lambda enforces a hard memory ceiling on each execution environment. When RSS memory reaches the limit the runtime kills the process immediately without invoking any cleanup handlers, producing `Runtime.ExitError` with `signal: killed`. Because CPU allocation scales proportionally with memory, memory-constrained functions are also CPU-constrained, which can increase execution time and worsen the OOM rate under load.
**Indicator:**

- [Step 2] `maxMemMB` equals `limitMB` in the REPORT query output
- [Step 1] log contains `signal: killed` in `Runtime.ExitError` line

<!-- match: {"step": 1, "predicate": "contains", "target": "signal: killed"} -->
<!-- match: {"step": 2, "predicate": "threshold", "target": "maxMemMB_pct_of_limitMB", "op": ">=", "value": 0.98} -->

**Mitigation:**

- **Risk:** Doubling memory doubles cost-per-invocation proportionally; also increases CPU, which may reduce duration and partly offset the cost increase.
- **Command:**

  ```bash
  CURRENT=$(aws lambda get-function-configuration --function-name my-function \
    --query 'MemorySize' --output text)
  NEW=$((CURRENT * 2))
  aws lambda update-function-configuration --function-name my-function --memory-size $NEW
  ```

- **Duration:** Immediate; applies to the next cold start.

**Resolution:**

```bash
# Profile peak usage over 24 hours, then set memory to 1.5× peak
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

**Verification:** Re-run Step 2 after 15 minutes of production traffic. `maxMemMB` should be ≤ 75% of new `limitMB`.

---

### Cause B: Handler not found — module path or export name mismatch

**Statement:** The Lambda runtime cannot locate the handler entry point because the `Handler` configuration field does not match the actual module file path or exported function name in the deployment package.
**Mechanism:** On cold start, the runtime loads the module named before the dot separator in `Handler` (e.g., `handler` in `handler.lambda_handler`) and then resolves the attribute after the dot. If the ZIP does not contain that module at its root, or the module does not export that attribute, the runtime emits `Runtime.ImportModuleError` or `Runtime.HandlerNotFound` before the Invoke phase begins, causing every invocation to fail.
**Indicator:**

- [Step 1] log contains `Runtime.ImportModuleError` or `Runtime.HandlerNotFound`
- [Step 3] `Handler` value does not correspond to a file visible in Step 4's ZIP listing

<!-- match: {"step": 1, "predicate": "contains", "target": "Runtime.ImportModuleError"} -->
<!-- match: {"step": 1, "predicate": "contains", "target": "Runtime.HandlerNotFound"} -->

**Mitigation:**

- **Risk:** Low — only changes the handler configuration field; no code is modified.
- **Command:**

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

- **Duration:** Immediate.

**Resolution:** Rebuild the deployment package ensuring the handler module is at the ZIP root (not inside a subdirectory), redeploy, and correct the `Handler` field to match.

```bash
# Verify and redeploy (Python example)
cd /path/to/project
zip -r function.zip handler.py requirements/ site-packages/
aws lambda update-function-code --function-name my-function --zip-file fileb://function.zip
aws lambda update-function-configuration --function-name my-function --handler handler.lambda_handler
```

**Verification:** Run Step 8. The response must not contain `FunctionError` and the log tail must not contain `ImportModuleError` or `HandlerNotFound`.

---

### Cause C: Missing bootstrap — invalid entrypoint for custom runtime

**Statement:** A `provided.al2023` custom-runtime function fails to start because the `bootstrap` executable is absent from or not at the root of the deployment package ZIP.
**Mechanism:** Lambda's custom runtime contract requires an executable file named `bootstrap` at `/var/task/bootstrap` (the ZIP root). If the file is missing, inside a subdirectory, or is a symlink rather than a real binary, the runtime emits `Runtime.InvalidEntrypoint` before any handler code can run, and every invocation fails immediately.
**Indicator:**

- [Step 1] log contains `Runtime.InvalidEntrypoint` or `Couldn't find valid bootstrap`
- [Step 4] `bootstrap` is absent from the root of the ZIP listing or listed under a subdirectory path

<!-- match: {"step": 1, "predicate": "contains", "target": "Runtime.InvalidEntrypoint"} -->
<!-- match: {"step": 1, "predicate": "contains", "target": "Couldn't find valid bootstrap"} -->

**Mitigation:**

- **Risk:** Low — redeploy only; no configuration changes.
- **Command:**

  ```bash
  # Verify bootstrap is executable and at ZIP root
  unzip -l /tmp/lambda.zip | grep bootstrap
  # Must show: <size> bootstrap  (no leading path)

  # Rebuild: bootstrap must be at root, mode 755
  chmod 755 bootstrap
  zip -j function.zip bootstrap   # -j strips directory paths
  aws lambda update-function-code --function-name my-function --zip-file fileb://function.zip
  ```

- **Duration:** Immediate after redeployment.

**Resolution:** Same as Mitigation.

**Verification:** Run Step 8. The log tail must not contain `InvalidEntrypoint` or `Couldn't find valid bootstrap`.

---

### Cause D: Missing IAM permission — execution role denied access to downstream AWS service

**Statement:** The function's IAM execution role lacks a required permission for a downstream AWS service, causing the SDK call to fail with `AccessDeniedException`.
**Mechanism:** Lambda executes function code under the identity of the execution role. When the function calls an AWS service (DynamoDB, S3, SQS, etc.) without the required IAM action on that resource, the service returns an `AccessDeniedException`. The function receives this as an exception from the SDK and typically propagates it as an unhandled error, incrementing the `Errors` metric. IAM policy changes propagate within 10–60 seconds; calls made immediately after an update may still fail.
**Indicator:**

- [Step 1] log contains `AccessDeniedException` with the denied action name
- [Step 5] `EvalDecision` is `implicitDeny` or `explicitDeny` for that action

<!-- match: {"step": 1, "predicate": "contains", "target": "AccessDeniedException"} -->
<!-- match: {"step": 5, "predicate": "contains", "target": "implicitDeny"} -->

**Mitigation:**

- **Risk:** Medium — adding overly broad permissions widens the blast radius; scope to the specific resource ARN and action.
- **Command:**

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

- **Duration:** Allow 60 seconds for IAM propagation, then re-test.

**Resolution:** Use IAM Access Analyzer to generate a least-privilege policy from CloudTrail activity, then replace the inline policy with the generated managed policy.

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

- **Impact:** Policy change is role-wide; all functions sharing the execution role are affected.
- **Rollback:** `aws iam delete-role-policy --role-name $ROLE_NAME --policy-name lambda-resource-access`

**Verification:**

Run Step 5 again. `EvalDecision` must be `allowed`. Then run Step 8 — `AccessDeniedException` must not appear in log tail.

---

### Cause E: VPC-connected function lacks NAT Gateway or VPC endpoint for AWS services

**Statement:** A Lambda function attached to a VPC subnet with no NAT Gateway route and no VPC endpoint cannot reach public AWS service endpoints, causing all outbound SDK calls to time out.
**Mechanism:** When a function is attached to a VPC, Lambda routes all outbound traffic through the VPC's Hyperplane ENI. Public subnets do not grant internet access to Lambda (Lambda instances do not receive public IPs). Without a NAT Gateway on the subnet's route table, packets destined for `dynamodb.us-east-1.amazonaws.com` (or any other public AWS endpoint) have no valid route, so the SDK connection times out producing `EndpointConnectionError` or `ConnectTimeoutError` after the SDK's default timeout.
**Indicator:**

- [Step 1] log contains `EndpointConnectionError` or `ConnectTimeoutError` or `Could not connect to the endpoint URL`
- [Step 6] subnet route table has no `0.0.0.0/0` route targeting a NAT Gateway

<!-- match: {"step": 1, "predicate": "contains", "target": "EndpointConnectionError"} -->
<!-- match: {"step": 6, "predicate": "absent", "target": "nat-"} -->

**Mitigation:**

- **Risk:** Low for free Gateway VPC endpoints (S3/DynamoDB); NAT Gateway incurs ~$0.045/hr per AZ plus data transfer charges.
- **Command:**

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

- **Duration:** VPC endpoints become active within 1–2 minutes.

**Resolution:** For services requiring internet access (third-party APIs, other AWS services without Gateway endpoints), add a NAT Gateway in a public subnet and add a route in the Lambda subnets' route table pointing `0.0.0.0/0` to the NAT Gateway. If the function does not need VPC resources at all, remove the VPC configuration:

```bash
aws lambda update-function-configuration --function-name my-function \
  --vpc-config SubnetIds=[],SecurityGroupIds=[]
```

- **Impact:** VPC endpoint addition is VPC-wide for all resources using that route table. NAT Gateway addition requires a public subnet and Elastic IP.
- **Rollback:** `aws ec2 delete-vpc-endpoints --vpc-endpoint-ids <endpoint-id>`

**Verification:**

Run Step 8. `EndpointConnectionError` or `ConnectTimeoutError` must not appear. Confirm via Step 6 that the route table now contains a `0.0.0.0/0` route or a service-specific prefix-list entry.

---

### Cause F: Concurrency throttling — reserved concurrency set to zero or account limit reached

**Statement:** Lambda rejects invocations with `TooManyRequestsException` because either the function's reserved concurrency is set to 0 (function disabled) or the account-level concurrent execution limit is exhausted.
**Mechanism:** Lambda enforces two concurrency ceilings: a per-function reserved concurrency cap and an account-level unreserved concurrency pool (default 1000 per region). When reserved concurrency is 0, every invocation is throttled immediately. When account-level unreserved concurrency is exhausted by other functions, additional invocations to non-reserved functions are throttled. Throttled invocations return HTTP 429 and are retried by asynchronous callers (up to 2 times), which can cause DLQ backlog or event loss.
**Indicator:**

- [Step 7] `ReservedConcurrentExecutions` is 0, or CloudWatch `Throttles` metric sum is non-zero
- [Step 8] `StatusCode` is 429 in the invoke response

<!-- match: {"step": 7, "predicate": "contains", "target": "TooManyRequestsException"} -->
<!-- match: {"step": 8, "predicate": "contains", "target": "TooManyRequestsException"} -->

**Mitigation:**

- **Risk:** Removing the reserved concurrency cap may allow the function to consume all account concurrency, starving other functions.
- **Command:**

  ```bash
  # If reserved concurrency is 0, raise it to a safe value
  aws lambda put-function-concurrency --function-name my-function \
    --reserved-concurrent-executions 50

  # Or delete the reserved concurrency (use account pool)
  aws lambda delete-function-concurrency --function-name my-function
  ```

- **Duration:** Immediate.

**Resolution:** Request a concurrency quota increase if the account limit is the constraint:

```bash
aws service-quotas request-service-quota-increase \
  --service-code lambda \
  --quota-code L-B99A9384 \
  --desired-value 3000
```

- **Impact:** Concurrency changes are function-scoped for reserved concurrency; account-wide for quota increases.
- **Rollback:** `aws lambda put-function-concurrency --function-name my-function --reserved-concurrent-executions <original-value>`

**Verification:**

Run Step 7. `Throttles` metric sum must drop to 0. Re-run Step 8 — `StatusCode` must be 200.

---

### Cause Z: Unidentified Lambda invocation failure

**Statement:** The invocation error does not match any of the identified patterns and requires deeper investigation or escalation. [Default]
**Mechanism:** Lambda invocation failures that do not produce one of the recognisable error strings (signal: killed, ImportModuleError, HandlerNotFound, InvalidEntrypoint, AccessDeniedException, EndpointConnectionError, TooManyRequestsException) may be caused by unhandled application exceptions, Init-phase timeouts (Sandbox.Timedout), KMS key issues, EFS/S3 mount failures, unexpected Node.js process exit (Runtime.NodejsExit), or transient AWS service disruptions.
**Indicator:**

- [Default] None of Causes A–F match the error strings observed in Steps 1 and 8

**Mitigation:**

- **Risk:** None — diagnostic only.
- **Command:**

  ```bash
  # Check function state and last update status
  aws lambda get-function-configuration --function-name my-function \
    --query '{State:State,StateReason:StateReason,LastUpdateStatus:LastUpdateStatus,LastUpdateStatusReason:LastUpdateStatusReason}'

  # Look for X-Ray traces for deeper error context
  aws xray get-service-graph \
    --start-time $(date -d '1 hour ago' -u +%s) \
    --end-time $(date -u +%s)

  # Check for Init-phase timeouts (Sandbox.Timedout)
  QUERY_ID=$(aws logs start-query \
    --log-group-name /aws/lambda/my-function \
    --start-time $(date -d '1 hour ago' +%s) \
    --end-time $(date +%s) \
    --query-string 'filter @message like /Sandbox|KMS|EFS|NodejsExit|Init/ | sort @timestamp desc | limit 20' \
    --query 'queryId' --output text)
  sleep 5
  aws logs get-query-results --query-id "$QUERY_ID"
  ```

- **Duration:** Diagnostic only; no change applied.

**Resolution:** Out of runbook scope. Escalate to AWS Support with the function ARN, request IDs from failing invocations, and the full CloudWatch Logs output from Steps 1 and 8. Enable AWS X-Ray active tracing on the function to capture per-segment timing.

```bash
aws lambda update-function-configuration --function-name my-function \
  --tracing-config Mode=Active
```

**Verification:** Resolution requires identifying the root cause through escalation. Confirm the error rate returns to zero via the CloudWatch `Errors` metric after the fix is applied.

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
