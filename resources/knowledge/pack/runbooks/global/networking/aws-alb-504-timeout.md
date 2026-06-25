---
id: "aws-alb-504-timeout"
title: "AWS ALB 504 Gateway Timeout"
domain: networking
service: aws-alb
symptom_class: [timeout]
severity: high
scope: global
version: "2.0.0"
last_updated: "2026-06-25"
verified_by: "kb-researcher"
status: draft
tags: [aws, alb, "504", gateway-timeout, load-balancer, idle-timeout, target-group]
difficulty: intermediate
---

## Symptom Recognition

- HTTP 504 responses received by clients behind an AWS Application Load Balancer
- CloudWatch metric `HTTPCode_ELB_504_Count` shows non-zero `Sum` in `AWS/ApplicationELB` namespace
- ALB access logs contain entries where `elb_status_code` (field 9) is `504`
- `target_status_code` (field 10) is `-` and `target_processing_time` (field 7) is `-1` when the ALB timed out waiting for the target
- `target_status_code` is `504` when the target itself generated the error (shifts investigation to the application layer)
- CloudWatch `TargetResponseTime` p99 approaching or exceeding the configured idle timeout value
- `TargetConnectionErrorCount` metric showing non-zero `Sum` when the ALB cannot establish TCP connections to targets
- `UnHealthyHostCount` showing all targets unhealthy in the target group

## Applicability

- All AWS ALB deployments in any region; covers EC2, ECS, Lambda, and IP-based targets
- Required permissions: `elasticloadbalancing:Describe*`, `cloudwatch:GetMetricStatistics`, `ec2:DescribeSecurityGroups`, `ec2:DescribeNetworkAcls`
- Read access to the ALB access log S3 bucket if logging is enabled
- AWS CLI v2 with credentials configured, or AWS Console access
- Targets accessible from a bastion host or SSM Session Manager for direct connectivity tests

## Diagnostic Steps

### Step 1: Confirm the 504 origin using access logs

```bash
# List recent log files in the configured S3 bucket
aws s3 ls s3://<logging-bucket>/<prefix>/AWSLogs/<account-id>/elasticloadbalancing/<region>/$(date +%Y/%m/%d)/ --recursive | tail -5

# Download and decompress the most recent log
aws s3 cp s3://<logging-bucket>/<path>.log.gz /tmp/alb.log.gz && gunzip /tmp/alb.log.gz

# Extract 504 entries: field 9=elb_status_code, field 10=target_status_code, field 7=target_processing_time
awk '$9 == 504 {print $7, $9, $10, $13}' /tmp/alb.log | head -20
```

Expected output: When `target_processing_time` (field 7) is `-1` and `target_status_code` (field 10) is `-`, the ALB never received a response from the target — the timeout is ALB-side. When `target_status_code` is `504`, the target itself returned the error and the problem is in the application layer.

### Step 2: Check the ALB idle timeout value

```bash
aws elbv2 describe-load-balancer-attributes \
  --load-balancer-arn <alb-arn> \
  --query "Attributes[?Key=='idle_timeout.timeout_seconds'].Value" \
  --output text
```

Expected output: a number — default is `60`. If the application has endpoints that legitimately take longer than this value (report generation, batch processing), the ALB returns 504 before the target finishes. A value already much higher than 60 suggests a previous workaround was applied.

### Step 3: Check target health status

```bash
# Per-target health with failure reason codes
aws elbv2 describe-target-health \
  --target-group-arn <target-group-arn>

# CloudWatch unhealthy host count over the past hour
aws cloudwatch get-metric-statistics \
  --namespace AWS/ApplicationELB \
  --metric-name UnHealthyHostCount \
  --dimensions Name=TargetGroup,Value=<tg-suffix> Name=LoadBalancer,Value=<alb-suffix> \
  --statistics Maximum \
  --period 300 \
  --start-time $(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%S) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%S)
```

Expected output: at least one target with `State: healthy`. If all targets show `unhealthy`, no requests can be served. The `Reason` field (e.g., `Target.Timeout`, `Target.FailedHealthChecks`) narrows the sub-cause.

### Step 4: Measure target response time

```bash
aws cloudwatch get-metric-statistics \
  --namespace AWS/ApplicationELB \
  --metric-name TargetResponseTime \
  --dimensions Name=LoadBalancer,Value=<alb-suffix> \
  --statistics Average \
  --extended-statistics p99 \
  --period 300 \
  --start-time $(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%S) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%S)
```

Expected output: p99 `TargetResponseTime` well below the idle timeout from Step 2. If p99 approaches or exceeds the idle timeout, slow targets are the direct cause of the 504s.

### Step 5: Check target TCP connection errors

```bash
aws cloudwatch get-metric-statistics \
  --namespace AWS/ApplicationELB \
  --metric-name TargetConnectionErrorCount \
  --dimensions Name=LoadBalancer,Value=<alb-suffix> \
  --statistics Sum \
  --period 300 \
  --start-time $(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%S) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%S)
```

Expected output: `Sum: 0.0`. A non-zero value means the ALB cannot establish TCP connections to targets — the target process is not listening, the connection backlog is full, or a network rule is blocking the port.

### Step 6: Verify security groups and subnet NACLs

```bash
# Check target security group allows inbound from ALB security group on target port
aws ec2 describe-security-groups --group-ids <target-sg-id> \
  --query "SecurityGroups[].IpPermissions[]"

# Check subnet NACLs permit inbound ephemeral port return traffic (1024-65535)
aws ec2 describe-network-acls \
  --filters "Name=association.subnet-id,Values=<target-subnet-id>" \
  --query "NetworkAcls[].Entries[]"
```

Expected output: target security group has an inbound rule permitting traffic from the ALB security group on the listener port. Subnet NACLs must allow both inbound on the target port and outbound on ephemeral ports (1024-65535). A missing NACL outbound rule for ephemeral ports silently drops responses from targets back to the ALB.

### Step 7: Test target directly (bypass ALB)

```bash
# From within the VPC via bastion or SSM session
time curl -v http://<target-private-ip>:<port>/health

# Measure connection and TTFB for slow endpoints
time curl -o /dev/null -s \
  -w "connect: %{time_connect}s\nttfb: %{time_starttransfer}s\ntotal: %{time_total}s\n" \
  http://<target-private-ip>:<port>/slow-endpoint
```

Expected output: successful response with total time well below the idle timeout. If the target responds slowly or fails when accessed directly, the problem is the application. If the target responds quickly directly but times out through the ALB, the problem is in the network path or ALB configuration.

### Step 8: Check target keep-alive timeout configuration

```bash
# Apache
grep -i "KeepAliveTimeout" /etc/httpd/conf/httpd.conf /etc/httpd/conf.d/*.conf 2>/dev/null

# Nginx
grep -r "keepalive_timeout" /etc/nginx/

# Node.js (search application source)
grep -r "keepAliveTimeout\|headersTimeout" . --include="*.js" --include="*.ts" | head -10
```

Expected output: the target's keep-alive timeout must be strictly greater than the ALB idle timeout. If the target closes an idle connection at the same moment the ALB reuses it for a new request, the result is an intermittent 504. For a 60-second ALB idle timeout, the target keep-alive must be at least 65 seconds.

## Causes

### Cause A: Target response exceeds ALB idle timeout

**Statement:** The target application does not return response headers before the ALB idle timeout expires, causing the ALB to close the connection and return 504.

**Chain:**
- root: a target endpoint is slow (long DB queries, unguarded downstream calls, large serialization, or GC pauses) and does not emit any response byte quickly
- s1: the ALB idle timeout (default 60s) elapses while the ALB waits for the first response byte from the target
- s2: the ALB closes the target connection without having received a response
- D: the ALB returns HTTP 504 to the client (points at Symptom Recognition)

**Indicators:**
- root: [Step 4] p99 `TargetResponseTime` approaches or exceeds the idle timeout value from Step 2
  <!-- match: {"step": 4, "predicate": "threshold", "target": "p99_TargetResponseTime", "op": ">=", "value": 55} -->
- s2: [Step 1] `target_processing_time` is `-1` and `target_status_code` is `-` in access logs
  <!-- match: {"step": 1, "predicate": "contains", "target": "-1"} -->

**Interventions:**
- **remediation** (root): Identify and optimize the slowest endpoints; apply connection-level timeouts in the application for all downstream calls.

  ```bash
  # Identify the slowest endpoints from ALB access logs
  # field 7 = target_processing_time, field 13 = request URL
  awk '$7 > 5 && $9 != 504 {print $7, $13}' /tmp/alb.log | sort -rn | head -20
  ```

  Profile and optimize the slow endpoints (query optimization, async offloading, caching). Endpoint optimization is application-scoped; no ALB restart required.

  **Verification:** Monitor `HTTPCode_ELB_504_Count` Sum and `TargetResponseTime` p99 in CloudWatch for 15 minutes after optimization. Both metrics should trend to zero/below threshold.
- **mitigation** (s1): Widen the ALB idle timeout so slow targets are not cut off while the application is optimized.

  ```bash
  aws elbv2 modify-load-balancer-attributes \
    --load-balancer-arn <alb-arn> \
    --attributes Key=idle_timeout.timeout_seconds,Value=120
  ```

  **Risk:** Increasing the idle timeout is a temporary measure; it does not fix slow targets and may increase resource consumption on long-held connections. **Duration:** Immediate effect on new connections; revert after optimizing the target application (`Value=60`). **Verification:** `HTTPCode_ELB_504_Count` Sum drops while optimization proceeds.

### Cause B: All targets unhealthy — no target available to serve requests

**Statement:** Every registered target in the target group is failing health checks, leaving the ALB with no healthy target to forward requests to.

**Chain:**
- root: targets are failing the configured health check (application crashes, port mismatch, health check path returning non-2XX, or overload)
- s1: the ALB marks every registered target `unhealthy` and removes them from the routing pool
- s2: the ALB has no valid destination for forwarded requests
- D: the ALB returns HTTP 504 for all forwarded requests (points at Symptom Recognition)

**Indicators:**
- s1: [Step 3] `describe-target-health` shows all targets with `State: unhealthy`
  <!-- match: {"step": 3, "predicate": "contains", "target": "unhealthy"} -->
- s1: [Step 3] `UnHealthyHostCount` Maximum equals the total number of registered targets

**Interventions:**
- **remediation** (root): Fix the underlying target failure — restart crashed processes, correct the health check path to return HTTP 200, match the health check port to the listening port, and resolve overload.

  ```bash
  # Inspect the health check configuration
  aws elbv2 describe-target-groups --target-group-arns <target-group-arn> \
    --query "TargetGroups[].{Path:HealthCheckPath,Port:HealthCheckPort,Timeout:HealthCheckTimeoutSeconds,Interval:HealthCheckIntervalSeconds}"
  ```

  **Verification:** Run `aws elbv2 describe-target-health --target-group-arn <target-group-arn>` and confirm at least one target shows `State: healthy`. Watch `HealthyHostCount` metric rise above zero.
- **mitigation** (s1): Loosen health check thresholds so recovering targets are not evicted prematurely.

  ```bash
  aws elbv2 modify-target-group \
    --target-group-arn <target-group-arn> \
    --health-check-timeout-seconds 10 \
    --health-check-interval-seconds 30 \
    --healthy-threshold-count 2 \
    --unhealthy-threshold-count 5
  ```

  **Risk:** Low — adjusting health check thresholds gives sick targets more recovery time without removing them from the pool prematurely. **Duration:** 2-3 minutes for targets to accumulate passing health checks and re-enter the healthy pool. **Verification:** `HealthyHostCount` rises above zero.

### Cause C: ALB cannot establish TCP connection to target

**Statement:** The ALB cannot complete a TCP handshake to the target within the 10-second connection timeout because the target process is not listening, the connection backlog is full, or a security group blocks the port.

**Chain:**
- root: the target is unreachable on the registered port (process not listening, wrong port, OS connection backlog exhausted under load, or the target security group blocks inbound from the ALB)
- s1: the ALB's TCP handshake to the target fails and `TargetConnectionErrorCount` increments
- s2: the ALB cannot forward the HTTP request to any target
- D: the ALB returns HTTP 504 (points at Symptom Recognition)

**Indicators:**
- s1: [Step 5] `TargetConnectionErrorCount` Sum is non-zero
  <!-- match: {"step": 5, "predicate": "threshold", "target": "TargetConnectionErrorCount_Sum", "op": ">", "value": 0} -->
- root: [Step 7] direct `curl` to the target IP and port times out or is refused

**Interventions:**
- **remediation** (root): Ensure the application process is running and bound to the correct port; fix a misconfigured target group port; scale out if the backlog is exhausted.

  ```bash
  # Confirm the application process is listening on the expected port (run on target via SSM)
  ss -tlnp | grep <port>
  ```

  **Verification:** `TargetConnectionErrorCount` Sum drops to 0 in CloudWatch; `describe-target-health` shows targets as healthy.
- **mitigation** (root): Verify the registered port and add the missing ALB-to-target security group rule.

  ```bash
  # Verify registered target port matches what the application listens on
  aws elbv2 describe-target-groups --target-group-arns <target-group-arn> \
    --query "TargetGroups[].{Port:Port,Protocol:Protocol}"

  # Add inbound rule from ALB security group if missing
  aws ec2 authorize-security-group-ingress \
    --group-id <target-sg-id> \
    --protocol tcp \
    --port <target-port> \
    --source-group <alb-sg-id>
  ```

  **Risk:** Low — verify and correct the target port and security group rule without service disruption. **Duration:** Security group rules take effect within seconds. **Verification:** `TargetConnectionErrorCount` Sum drops to 0.

### Cause D: Subnet NACL blocks ephemeral port return traffic

**Statement:** The subnet Network ACL does not allow outbound traffic on ephemeral ports (1024–65535) from target subnets back to ALB nodes, silently dropping TCP responses.

**Chain:**
- root: the target subnet NACL is missing or denying an outbound ALLOW rule for TCP ephemeral ports (1024–65535) toward the ALB subnets
- s1: because NACLs are stateless, target HTTP responses (destined to an ALB-chosen ephemeral port) are silently dropped at the subnet boundary
- s2: the ALB never receives the target response and its wait times out
- D: the ALB returns HTTP 504 (points at Symptom Recognition)

**Indicators:**
- root: [Step 6] NACL entries show no outbound `ALLOW` rule covering TCP ports 1024–65535 toward ALB subnets
  <!-- match: {"step": 6, "predicate": "absent", "target": "1024-65535"} -->
- s1: [Step 7] direct curl to the target succeeds but traffic through the ALB times out
- s1: [Step 5] `TargetConnectionErrorCount` may be zero (TCP SYN reaches target) while 504s persist

**Interventions:**
- **remediation** (root): Add the missing NACL outbound rule for ephemeral return traffic; also verify the ALB subnet NACL allows inbound on ephemeral ports from the target subnet.

  ```bash
  aws ec2 create-network-acl-entry \
    --network-acl-id <target-subnet-nacl-id> \
    --rule-number 200 \
    --protocol tcp \
    --port-range From=1024,To=65535 \
    --cidr-block <alb-subnet-cidr> \
    --rule-action allow \
    --egress
  ```

  **Verification:** 504 rate drops to zero in CloudWatch `HTTPCode_ELB_504_Count` within 2-3 minutes. Confirm with a `curl` burst through the ALB DNS name.

### Cause E: Keep-alive timeout mismatch

**Statement:** The target's HTTP keep-alive timeout is equal to or shorter than the ALB idle timeout, causing the target to close a connection at the moment the ALB attempts to reuse it for a new request.

**Chain:**
- root: the target's keep-alive timeout is less than or equal to the ALB idle timeout (e.g., Nginx default 60s against a 60s ALB timeout)
- s1: the target sends a TCP FIN to close an idle pooled connection just as the ALB selects that same connection for a new request
- s2: the ALB receives a connection reset mid-request, producing intermittent 504s during low-traffic periods when connections sit idle longest
- D: the ALB returns HTTP 504 (points at Symptom Recognition)

**Indicators:**
- root: [Step 8] target keep-alive timeout is less than or equal to the ALB idle timeout from Step 2
  <!-- match: {"step": 8, "predicate": "threshold", "target": "target_keepalive_seconds", "op": "<=", "value": 60} -->
- s2: [Step 1] 504s are intermittent, with `target_processing_time = -1` but no corresponding spike in `TargetConnectionErrorCount`

**Interventions:**
- **remediation** (root): Set the target keep-alive timeout at least 5 seconds greater than the ALB idle timeout on all target application servers.

  ```bash
  # Nginx — set to ALB idle timeout + 5 seconds (e.g., 65 s for 60 s ALB timeout)
  # Edit /etc/nginx/nginx.conf: keepalive_timeout 65;
  nginx -t && nginx -s reload

  # Apache — edit /etc/httpd/conf/httpd.conf: KeepAliveTimeout 65
  systemctl reload httpd

  # Node.js — in application code:
  # server.keepAliveTimeout = 65000;   // ms
  # server.headersTimeout = 66000;     // ms — must exceed keepAliveTimeout
  ```

  **Verification:** Monitor `HTTPCode_ELB_504_Count` Sum over 30 minutes of normal traffic. Intermittent 504s during idle periods should cease.

### Cause F: Lambda target does not respond before connection timeout

**Statement:** The Lambda function registered as an ALB target does not respond within the ALB's fixed 10-second connection timeout for Lambda invocations.

**Chain:**
- root: the Lambda invocation does not complete within 10 seconds (cold start over 10s, Lambda function timeout ≤ 10s, or Lambda service throttling)
- s1: the ALB's synchronous wait of up to 10 seconds for the Lambda service response expires (this limit is fixed and not governed by the ALB idle timeout)
- s2: the ALB cannot obtain a response from the Lambda target
- D: the ALB returns HTTP 504 (points at Symptom Recognition)

**Indicators:**
- s2: [Step 1] `target:port` (field 5) is `-` (Lambda target) and `target_status_code` is `-`
  <!-- match: {"step": 1, "predicate": "contains", "target": "lambda"} -->
- s1: [Step 3] target group is of type `lambda`; CloudWatch `LambdaInternalError` or `LambdaUserError` metrics are non-zero

**Interventions:**
- **remediation** (root): Restructure long-running operations to an async pattern and provision concurrency to eliminate cold starts.

  ```bash
  # For long-running operations, restructure to async pattern:
  # 1. Lambda returns immediately with 202 Accepted
  # 2. Processing happens in a background Lambda triggered by SQS/SNS
  # Provision concurrency to eliminate cold starts for latency-sensitive functions:
  aws lambda put-provisioned-concurrency-config \
    --function-name <function-name> \
    --qualifier <alias-or-version> \
    --provisioned-concurrent-executions 5
  ```

  **Verification:** `HTTPCode_ELB_504_Count` drops to zero; `LambdaInternalError` Sum returns to zero in CloudWatch.
- **mitigation** (root): Increase the Lambda function timeout and check for throttling.

  ```bash
  # Increase Lambda function timeout (max 15 minutes)
  aws lambda update-function-configuration \
    --function-name <function-name> \
    --timeout 30

  # Check for throttling
  aws cloudwatch get-metric-statistics \
    --namespace AWS/Lambda \
    --metric-name Throttles \
    --dimensions Name=FunctionName,Value=<function-name> \
    --statistics Sum --period 300 \
    --start-time $(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%S) \
    --end-time $(date -u +%Y-%m-%dT%H:%M:%S)
  ```

  **Risk:** Medium — increasing Lambda function timeout allows longer execution but does not fix the 10-second ALB connection timeout for the initial invoke. **Duration:** Lambda configuration update takes effect within seconds. **Verification:** `LambdaUserError`/`LambdaInternalError` Sum trends to zero.

### Cause Z: Unidentified

**Statement:** The 504 timeout source cannot be determined from available metrics and logs.

**Indicators:**
- [Default]

**Interventions:**
- **mitigation** (D): Capture a full diagnostic snapshot (enable ALB access logs, collect CloudWatch metric screenshots for the incident window, target group ARN, and access log fields 6–10, 13, 25) and escalate to the SME / AWS Support.

  ```bash
  # Enable ALB access logs if not already enabled
  aws elbv2 modify-load-balancer-attributes \
    --load-balancer-arn <alb-arn> \
    --attributes Key=access_logs.s3.enabled,Value=true \
                 Key=access_logs.s3.bucket,Value=<bucket-name> \
                 Key=access_logs.s3.prefix,Value=alb-logs
  ```

  **Risk:** Low — enabling access logs and increasing CloudWatch resolution costs money but does not affect production traffic. **Duration:** Logs available within 5 minutes of enabling. **Verification:** Escalation path opened; access logs enabled for future diagnosis.

## Prevention

1. **Set target keep-alive timeout above ALB idle timeout** — Configure the target application's HTTP keep-alive timeout to at least 5 seconds greater than the ALB idle timeout. For a 60-second ALB timeout, set target keep-alive to 65 seconds or more. Document this as a deployment requirement for all services behind ALBs.

2. **Enable ALB access logs** — Access logs are disabled by default. Enable them to an S3 bucket immediately after provisioning the ALB. The `target_processing_time` and `target_status_code` fields are essential for 504 root cause analysis and are unavailable without them.

3. **Alert on HTTPCode_ELB_504_Count** — Create a CloudWatch alarm on `HTTPCode_ELB_504_Count` Sum > 10 in a 5-minute window. Route to your incident channel. Use a short evaluation period (1–2 data points) to catch bursts early.

4. **Alert on TargetResponseTime p99** — Set a CloudWatch alarm when p99 `TargetResponseTime` exceeds 80% of the ALB idle timeout. This provides early warning before timeouts begin.

5. **Monitor UnHealthyHostCount with Minimum statistic** — Alarm on `UnHealthyHostCount` Minimum > 0 for more than one data point. Using Minimum detects when every ALB node and AZ considers a target unhealthy, which is the condition that causes 504s.

6. **Apply explicit downstream timeouts in the target application** — All downstream calls (database, external APIs, file I/O) must have timeouts shorter than the ALB idle timeout. A single unguarded slow dependency consumes the full idle timeout budget.

7. **Use Auto Scaling with response time targets** — Configure Auto Scaling using a target tracking policy on `RequestCountPerTarget` or `TargetResponseTime` so that capacity scales out before targets become overloaded and slow.

8. **Enable cross-zone load balancing** — Enable `load_balancing.cross_zone.enabled` on the ALB so traffic distributes across all healthy targets in all AZs. Without this, an AZ with no healthy targets returns 504 for requests routed to that AZ.

9. **Tune deregistration delay for in-flight requests** — Set the target group `deregistration_delay.timeout_seconds` to at least the application's maximum request duration so in-flight requests complete before a target is removed from the pool during deployments or scale-in.

10. **Set up Athena over access logs for incident analysis** — Create an Athena table over the ALB access log S3 bucket so that during incidents, SQL queries can aggregate 504s by URL, target IP, and time window without downloading individual log files.

## Sources

- [AWS: Troubleshoot your Application Load Balancers](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/load-balancer-troubleshooting.html) — Priority 1. Official AWS documentation covering all HTTP 504 causes: connection timeout, idle timeout, NACL blocking ephemeral ports, Content-Length mismatch, Lambda connection timeout, and SSL handshake timeout.
- [AWS: CloudWatch Metrics for Application Load Balancers](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/load-balancer-cloudwatch-metrics.html) — Priority 1. Reference for metric names, namespaces, dimensions, and recommended statistics for HTTPCode_ELB_504_Count, TargetResponseTime, UnHealthyHostCount, and TargetConnectionErrorCount.
- [AWS: Access Logs for Application Load Balancers](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/load-balancer-access-logs.html) — Priority 1. Defines all access log fields by position including elb_status_code (9), target_status_code (10), target_processing_time (7), and the semantics of `-1` and `-` values used for 504 diagnosis.
