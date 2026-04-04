---
id: aws-alb-504-timeout
title: "AWS Application Load Balancer 504 Gateway Timeout"
domain: networking
service: aws-alb
symptom_class:
  - timeout
severity: high
scope: global
version: "2.1.0"
last_updated: "2026-03-26"
verified_by: "kb-researcher"
status: draft
tags:
  - aws
  - alb
  - "504"
  - gateway-timeout
  - load-balancer
  - idle-timeout
  - target-group
difficulty: intermediate
---

## Problem Definition

This runbook covers AWS Application Load Balancer (ALB) 504 Gateway Timeout errors. It applies to all ALB deployments across AWS regions, including targets backed by EC2 instances, ECS tasks, Lambda functions, and IP-based targets. Diagnosis requires access to the AWS Console or CLI with `elasticloadbalancing:Describe*` and `cloudwatch:GetMetricStatistics` permissions, plus read access to the ALB access log S3 bucket if logging is enabled.

An HTTP 504 from an ALB indicates the load balancer could not obtain a response from the target within the allowed time. The ALB generates the 504 itself when the target fails to respond — it is not forwarded from the target. The error appears in CloudWatch as the `HTTPCode_ELB_504_Count` metric and in ALB access logs with `elb_status_code=504`. When `target_status_code=-` accompanies the 504, the target never responded at all.

Common causes include:

- **Target application too slow** — the target accepts the connection but does not return response headers before the ALB idle timeout expires (default 60 seconds). The ALB closes the connection and returns 504 to the client.
- **Target connection failure** — the ALB cannot establish a TCP connection to the target within the 10-second connection timeout. The target process is not listening, has exhausted its connection backlog, or a security group/NACL blocks the port.
- **Idle timeout mismatch** — the target's HTTP keep-alive timeout is shorter than the ALB idle timeout. The target closes an idle connection just as the ALB reuses it for a new request, resulting in a connection reset reported as 504.
- **Network ACL blocking ephemeral ports** — the subnet NACL does not allow return traffic from targets to ALB nodes on ephemeral ports (1024-65535), preventing responses from reaching the ALB.
- **All targets unhealthy** — every registered target is failing health checks, leaving no healthy target for the ALB to forward to.
- **Incomplete Content-Length** — the target sends a `Content-Length` header declaring more bytes than it actually sends. The ALB waits for the remaining bytes until the idle timeout expires.
- **Lambda target timeout** — the target is a Lambda function that does not respond before the Lambda service timeout or the ALB connection timeout (10 seconds for Lambda invoke).
- **SSL handshake timeout** — when the ALB connects to targets over HTTPS, the TLS handshake must complete within 10 seconds. Slow certificate validation or resource-constrained targets can exceed this limit.
- **Cross-AZ target unreachable** — if cross-zone load balancing is disabled, an AZ with no healthy targets returns 504 for requests routed to that AZ.

## Diagnostic Steps

### Step 1: Confirm the 504 is ALB-generated

Checks whether the ALB or the target generated the 504 by inspecting ALB access log fields. Field 9 is `elb_status_code` and field 10 is `target_status_code`.

```bash
# Download and inspect recent access logs from S3
aws s3 ls s3://<logging-bucket>/<prefix>/AWSLogs/<account-id>/elasticloadbalancing/<region>/$(date +%Y/%m/%d)/ --recursive | tail -5

# Download a recent log file
aws s3 cp s3://<logging-bucket>/<path-to-log>.log.gz /tmp/alb.log.gz
gunzip /tmp/alb.log.gz

# Find 504 entries
awk '$9 == 504' /tmp/alb.log | head -20
```

Expected output fields for each scenario:

| Field | 504 from ALB (target too slow) | 504 from target |
|---|---|---|
| `elb_status_code` (field 9) | `504` | `504` |
| `target_status_code` (field 10) | `-` (no response) | `504` |
| `target_processing_time` (field 7) | `-1` (timed out) | Positive value |

If `target_processing_time = -1` and `target_status_code = -`, the ALB never received a response — the timeout is ALB-side. If `target_status_code = 504`, the target itself generated the error and the investigation shifts to the application layer.

### Step 2: Check ALB idle timeout setting

Checks the configured idle timeout to determine the maximum time the ALB waits for a target response.

```bash
aws elbv2 describe-load-balancer-attributes \
  --load-balancer-arn <alb-arn> \
  --query "Attributes[?Key=='idle_timeout.timeout_seconds'].Value" \
  --output text
```

Expected output is a number (default `60`). If the application has endpoints that legitimately take longer than this value to respond (report generation, batch processing), the ALB will return 504 before the target finishes. A value much higher than 60 suggests someone already increased it as a workaround, and the real fix is backend optimization.

### Step 3: Check target health

Checks whether any targets are healthy and available to receive traffic.

```bash
# List target health for each target group
aws elbv2 describe-target-health \
  --target-group-arn <target-group-arn>

# Check CloudWatch for unhealthy host count
aws cloudwatch get-metric-statistics \
  --namespace AWS/ApplicationELB \
  --metric-name UnHealthyHostCount \
  --dimensions Name=TargetGroup,Value=<target-group-value> Name=LoadBalancer,Value=<alb-value> \
  --statistics Maximum \
  --period 300 \
  --start-time $(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%S) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%S)
```

Expected output: at least one target with `State: healthy`. If all targets show `unhealthy`, no requests can be served and the ALB returns 504 for every request. The `Reason` field in the health check output (e.g., `Elb.InitialHealthChecking`, `Target.Timeout`, `Target.FailedHealthChecks`) narrows the cause.

### Step 4: Check target response time metrics

Checks whether targets are responding slowly enough to trigger the idle timeout.

```bash
aws cloudwatch get-metric-statistics \
  --namespace AWS/ApplicationELB \
  --metric-name TargetResponseTime \
  --dimensions Name=LoadBalancer,Value=<alb-value> \
  --statistics Average \
  --extended-statistics p99 \
  --period 300 \
  --start-time $(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%S) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%S)
```

Expected output: p99 `TargetResponseTime` well below the idle timeout. If p99 approaches or exceeds the idle timeout value, slow targets are the direct cause of 504s. Compare against the idle timeout from Step 2.

### Step 5: Check target connection errors

Checks whether the ALB can establish TCP connections to targets.

```bash
aws cloudwatch get-metric-statistics \
  --namespace AWS/ApplicationELB \
  --metric-name TargetConnectionErrorCount \
  --dimensions Name=LoadBalancer,Value=<alb-value> \
  --statistics Sum \
  --period 300 \
  --start-time $(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%S) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%S)
```

Expected output: `Sum: 0.0`. A non-zero `TargetConnectionErrorCount` means the ALB cannot establish TCP connections to some targets. This indicates the target process is not listening, the connection backlog is full, or a network rule blocks the port.

### Step 6: Verify security groups and NACLs

Checks whether network rules permit traffic between the ALB and its targets.

```bash
# Check the target's security group allows inbound from ALB
aws ec2 describe-security-groups --group-ids <target-sg-id> \
  --query "SecurityGroups[].IpPermissions[]"

# Check subnet NACLs allow ephemeral port return traffic
aws ec2 describe-network-acls --filters "Name=association.subnet-id,Values=<target-subnet-id>" \
  --query "NetworkAcls[].Entries[]"
```

Expected output: the target security group has an inbound rule allowing traffic from the ALB security group on the target port. The subnet NACLs must allow both inbound and outbound traffic on ephemeral ports (1024-65535). Missing NACL rules for ephemeral ports silently block responses from targets to the ALB.

### Step 7: Test target directly (bypass ALB)

Checks whether the target responds when accessed directly, isolating the ALB and network path from the equation.

```bash
# From within the VPC (e.g., a bastion host or SSM session)
time curl -v http://<target-private-ip>:<port>/health

# For a slow endpoint, measure the full response time
time curl -o /dev/null -s -w "connect: %{time_connect}s\nttfb: %{time_starttransfer}s\ntotal: %{time_total}s\n" http://<target-private-ip>:<port>/slow-endpoint
```

Expected output: a successful response within the ALB idle timeout. If the target responds slowly or not at all when accessed directly, the problem is the application itself, not the ALB. If the target responds quickly directly but times out through the ALB, the problem is in the network path or ALB configuration.

### Step 8: Check for keep-alive timeout mismatch

Checks whether intermittent 504s correlate with low-traffic periods, which indicates the target is closing idle connections before the ALB expects.

```bash
# Check your application's keep-alive configuration
# For Apache:
grep -i "KeepAliveTimeout" /etc/httpd/conf/httpd.conf

# For Nginx:
grep "keepalive_timeout" /etc/nginx/nginx.conf

# For Node.js:
grep -r "server.keepAliveTimeout" .
```

Expected output: the target's keep-alive timeout must be **greater** than the ALB idle timeout. If the target's timeout is equal to or less than the ALB idle timeout, the target closes connections that the ALB considers active, producing intermittent 504s during connection reuse.

## Mitigation

### Option 1: Increase ALB idle timeout

- **Risk**: Low. Allows more time for slow targets to respond. May increase resource consumption on the ALB if many connections are held open longer.
- **Command**:

```bash
aws elbv2 modify-load-balancer-attributes \
  --load-balancer-arn <alb-arn> \
  --attributes Key=idle_timeout.timeout_seconds,Value=120
```

- **Verify**:

```bash
aws elbv2 describe-load-balancer-attributes \
  --load-balancer-arn <alb-arn> \
  --query "Attributes[?Key=='idle_timeout.timeout_seconds'].Value" \
  --output text
```

- **Duration**: Immediate. Applies to new connections. Revert after addressing the slow target.

### Option 2: Scale up or out the target group

- **Risk**: Low. Adds capacity to handle requests faster or distribute load across more targets.
- **Command**:

```bash
# For Auto Scaling group targets — increase desired capacity
aws autoscaling set-desired-capacity \
  --auto-scaling-group-name <asg-name> \
  --desired-capacity <new-count>

# For ECS service targets
aws ecs update-service --cluster <cluster> --service <service> --desired-count <new-count>
```

- **Verify**:

```bash
aws elbv2 describe-target-health --target-group-arn <target-group-arn>
```

- **Duration**: 1-5 minutes for new targets to register and pass health checks.

### Option 3: Fix keep-alive timeout mismatch

- **Risk**: Low. Adjusts the target's keep-alive timeout to exceed the ALB idle timeout, preventing premature connection closure.
- **Command**:

```bash
# For Nginx — set keepalive_timeout higher than ALB idle timeout (e.g., 65s for 60s ALB timeout)
# In nginx.conf:
#   keepalive_timeout 65;
nginx -t && nginx -s reload

# For Apache:
# In httpd.conf:
#   KeepAliveTimeout 65
systemctl reload httpd

# For Node.js:
# server.keepAliveTimeout = 65000;  // milliseconds
```

- **Verify**:

```bash
# Monitor 504 count in CloudWatch over the next 15 minutes
aws cloudwatch get-metric-statistics \
  --namespace AWS/ApplicationELB \
  --metric-name HTTPCode_ELB_504_Count \
  --dimensions Name=LoadBalancer,Value=<alb-value> \
  --statistics Sum --period 60 \
  --start-time $(date -u -d '15 minutes ago' +%Y-%m-%dT%H:%M:%S) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%S)
```

- **Duration**: Immediate after application reload/restart.

### Option 4: Enable cross-zone load balancing

- **Risk**: Low. Distributes traffic across all registered targets in all AZs, preventing 504s when one AZ has no healthy targets. Slight increase in inter-AZ data transfer costs.
- **Command**:

```bash
aws elbv2 modify-load-balancer-attributes \
  --load-balancer-arn <alb-arn> \
  --attributes Key=load_balancing.cross_zone.enabled,Value=true
```

- **Verify**:

```bash
aws elbv2 describe-load-balancer-attributes \
  --load-balancer-arn <alb-arn> \
  --query "Attributes[?Key=='load_balancing.cross_zone.enabled'].Value" \
  --output text
```

- **Duration**: Immediate. Takes effect for new connections.

## Root Cause Resolution

**If** `target_processing_time = -1` in access logs and the target application is slow → profile the target application to identify slow endpoints. Common causes include slow database queries, external API calls without timeouts, large payload serialization, and garbage collection pauses. Optimize the endpoint or offload long-running work to an asynchronous queue.

```bash
# Identify the slowest endpoints from ALB access logs
# Field 7 = target_processing_time, field 13 = request URL
awk '$9 != 504 && $7 > 5 {print $7, $13}' /tmp/alb.log | sort -rn | head -20
```

**If** `TargetConnectionErrorCount` is non-zero and targets are not reachable → verify the target process is running and listening on the configured port. Check security groups, NACLs, and that the target group port matches the application port. For containerized targets, ensure the container port mapping is correct.

```bash
aws elbv2 describe-target-groups --target-group-arns <target-group-arn> \
  --query "TargetGroups[].{Port:Port,Protocol:Protocol,HealthCheckPath:HealthCheckPath}"
```

**If** all targets are unhealthy and returning health check failures → fix the health check endpoint. Common issues: the health check path returns a non-200 status, the health check port differs from the traffic port, or the health check timeout is too short for the application's startup time.

```bash
# Check and adjust health check configuration
aws elbv2 describe-target-groups --target-group-arns <target-group-arn> \
  --query "TargetGroups[].{HealthCheckPath:HealthCheckPath,HealthCheckPort:HealthCheckPort,HealthCheckTimeoutSeconds:HealthCheckTimeoutSeconds,HealthyThresholdCount:HealthyThresholdCount,UnhealthyThresholdCount:UnhealthyThresholdCount}"

# Increase health check timeout and interval if targets are slow to start
aws elbv2 modify-target-group \
  --target-group-arn <target-group-arn> \
  --health-check-timeout-seconds 10 \
  --health-check-interval-seconds 30 \
  --healthy-threshold-count 2 \
  --unhealthy-threshold-count 3
```

**If** 504s are intermittent and correlate with idle periods (keep-alive mismatch) → set the target's HTTP keep-alive timeout to at least 5 seconds greater than the ALB idle timeout. For example, if the ALB idle timeout is 60 seconds, set the target keep-alive to at least 65 seconds. This ensures the target never closes a connection that the ALB considers active.

**If** NACLs block ephemeral port traffic → update the subnet NACL to allow inbound and outbound traffic on ephemeral ports (1024-65535) between the ALB subnets and target subnets.

```bash
aws ec2 create-network-acl-entry \
  --network-acl-id <nacl-id> \
  --rule-number 200 \
  --protocol tcp \
  --port-range From=1024,To=65535 \
  --cidr-block <alb-subnet-cidr> \
  --rule-action allow \
  --ingress
```

**If** the target is a Lambda function timing out → increase the Lambda function timeout (up to 15 minutes), but note the ALB connection timeout for Lambda is 10 seconds for the initial invoke. For long-running functions, consider using an asynchronous invocation pattern instead.

**If** the Content-Length mismatch is causing incomplete responses → fix the target application to send accurate `Content-Length` headers that match the actual response body size, or use chunked transfer encoding (`Transfer-Encoding: chunked`) instead of declaring a content length.

## Verification

After applying a fix, verify the 504 errors have stopped:

1. Check CloudWatch for 504 error rate trending to zero:

```bash
aws cloudwatch get-metric-statistics \
  --namespace AWS/ApplicationELB \
  --metric-name HTTPCode_ELB_504_Count \
  --dimensions Name=LoadBalancer,Value=<alb-value> \
  --statistics Sum --period 60 \
  --start-time $(date -u -d '30 minutes ago' +%Y-%m-%dT%H:%M:%S) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%S)
```

2. Confirm target response time is within acceptable bounds:

```bash
aws cloudwatch get-metric-statistics \
  --namespace AWS/ApplicationELB \
  --metric-name TargetResponseTime \
  --dimensions Name=LoadBalancer,Value=<alb-value> \
  --extended-statistics p99 \
  --period 60 \
  --start-time $(date -u -d '30 minutes ago' +%Y-%m-%dT%H:%M:%S) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%S)
```

3. Verify all targets are healthy:

```bash
aws elbv2 describe-target-health --target-group-arn <target-group-arn>
```

4. Send test requests through the ALB and confirm successful responses:

```bash
for i in $(seq 1 20); do
    curl -o /dev/null -s -w "%{http_code}\n" https://<alb-dns-name>/health
done | sort | uniq -c
```

5. Check access logs for any remaining 504 entries in the most recent log files:

```bash
aws s3 ls s3://<logging-bucket>/<prefix>/AWSLogs/<account-id>/elasticloadbalancing/<region>/$(date +%Y/%m/%d)/ --recursive | tail -3
```

## Prevention

1. **Set keep-alive timeout higher than ALB idle timeout** — Configure the target application's HTTP keep-alive timeout to at least 5 seconds greater than the ALB idle timeout. This prevents the race condition where the target closes a connection the ALB is about to reuse. Document this requirement as a deployment standard.

2. **Enable ALB access logs** — Access logs are disabled by default. Enable them to an S3 bucket so that when 504s occur, you have the `target_processing_time`, `target_status_code`, and request details needed for root cause analysis.

3. **Set CloudWatch alarms on HTTPCode_ELB_504_Count** — Create alarms that trigger when the 504 rate exceeds a threshold (e.g., more than 10 in a 5-minute period). Route to your incident response channel.

4. **Monitor TargetResponseTime p99** — Alert when p99 response time exceeds 80% of the ALB idle timeout. This gives early warning before timeouts start occurring.

5. **Configure health checks with appropriate thresholds** — Set health check intervals, timeouts, and thresholds so that unhealthy targets are removed from the pool before they cause 504s. Use a dedicated lightweight health check endpoint.

6. **Enable cross-zone load balancing** — Ensure traffic is distributed across all healthy targets in all AZs. Without this, an AZ with no healthy targets returns 504 for all requests routed to that AZ.

7. **Implement application-level timeouts** — Ensure the target application has explicit timeouts for all downstream calls (database queries, external APIs, file I/O) that are shorter than the ALB idle timeout. This prevents a single slow dependency from consuming the full timeout budget.

8. **Use connection draining (deregistration delay)** — Set an appropriate deregistration delay so that in-flight requests to targets being removed complete before the target is deregistered. The default is 300 seconds; tune based on your application's longest request.

9. **Right-size target group capacity** — Use Auto Scaling with target tracking on `TargetResponseTime` or `RequestCountPerTarget` to scale targets before they become overloaded. Overloaded targets respond slowly and trigger 504s.

10. **Use AWS Athena for access log analysis** — Set up an Athena table over your ALB access log S3 bucket for ad hoc SQL queries during incidents. Pre-built queries for 504 analysis significantly reduce mean-time-to-diagnose.

## Sources

- [AWS: Troubleshoot your Application Load Balancers](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/load-balancer-troubleshooting.html) — Official AWS documentation covering HTTP 504 causes including connection timeout, idle timeout, NACL issues, Content-Length mismatch, Lambda timeouts, and SSL handshake failures.
- [AWS: CloudWatch Metrics for Application Load Balancers](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/load-balancer-cloudwatch-metrics.html) — Reference for ALB metrics including HTTPCode_ELB_504_Count, TargetResponseTime, UnHealthyHostCount, and TargetConnectionErrorCount with their dimensions and statistics.
- [AWS: Access Logs for Application Load Balancers](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/load-balancer-access-logs.html) — Detailed access log format documentation including target_processing_time, elb_status_code, and target_status_code fields used to diagnose 504 timeouts.
- [AWS: Monitor your Application Load Balancers](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/load-balancer-monitoring.html) — Overview of ALB monitoring capabilities including CloudWatch metrics, access logs, connection logs, and health check logs.
