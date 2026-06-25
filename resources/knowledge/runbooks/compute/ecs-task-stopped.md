---
id: "ecs-task-stopped"
title: "AWS ECS tasks repeatedly stop and won't stay running"
domain: compute
service: aws-ecs
symptom_class: [crash_loop, service_unavailable]
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-06-24"
verified_by: "kb-researcher"
status: draft
tags: [cannot-pull-container-error, resource-initialization-error, out-of-memory-error, exit-code-137, elb-health-checks, fargate]
difficulty: intermediate
---

## Symptom Recognition

- ECS service event log shows tasks repeatedly transitioning to `STOPPED` and the service never reaches steady state: `service <name> has reached a steady state` never appears; instead repeated `service <name> has started 1 tasks` / `has stopped 1 running tasks`.
- `aws ecs describe-tasks` returns `lastStatus: STOPPED` with a populated `stoppedReason` / `stopCode` and a non-zero container `exitCode`.
- Common `stoppedReason` strings seen verbatim:
  - `CannotPullContainerError: ...` (image cannot be retrieved)
  - `ResourceInitializationError: unable to pull secrets or registry auth: ...`
  - `OutOfMemoryError: Container killed due to memory usage`
  - `Essential container in task exited`
  - `Task failed ELB health checks in (target-group arn:aws:elasticloadbalancing:...)`
- Running task count oscillates near zero; downstream `503 Service Unavailable` from the load balancer because no healthy targets are registered.

## Applicability

- Amazon ECS on EC2 launch type or Fargate launch type (platform versions in current support).
- Required IAM access: `ecs:ListTasks`, `ecs:DescribeTasks`, `ecs:DescribeServices`, `ecs:DescribeTaskDefinition`, `elasticloadbalancing:DescribeTargetHealth`, `logs:GetLogEvents`/`logs:FilterLogEvents`, and read on `ecr:GetAuthorizationToken` for image-pull diagnosis.
- Tools: AWS CLI v2 (`aws ecs`, `aws elbv2`, `aws logs`), configured credentials/region.
- Assumes the task definition's `awslogs` (or `awsfirelens`) log driver is configured so container stdout/stderr is queryable in CloudWatch Logs.

## Diagnostic Steps

### Step 1: List the most recently stopped tasks for the service

```bash
aws ecs list-tasks \
  --cluster "$CLUSTER" \
  --service-name "$SERVICE" \
  --desired-status STOPPED \
  --region "$REGION"
```

Expected output: a `taskArns` array of recently stopped task ARNs. An empty array means no stopped tasks are retained (ECS keeps stopped tasks for a limited window) — re-run shortly after a failure.

### Step 2: Inspect the stopped task's stopCode, stoppedReason, and container exitCode

```bash
aws ecs describe-tasks \
  --cluster "$CLUSTER" \
  --tasks "$TASK_ARN" \
  --region "$REGION" \
  --query 'tasks[0].{stopCode:stopCode,stoppedReason:stoppedReason,containers:containers[].{name:name,exitCode:exitCode,reason:reason}}'
```

Expected output: a JSON object with the task-level `stopCode` and `stoppedReason`, plus each container's `exitCode` and `reason`. This is the primary signal that selects which Cause below applies.

### Step 3: Read the container application logs around the stop time

```bash
aws logs get-log-events \
  --log-group-name "$LOG_GROUP" \
  --log-stream-name "$LOG_STREAM" \
  --region "$REGION" \
  --limit 100
```

Expected output: the container's stdout/stderr. Look for application stack traces, `OutOfMemoryError`, missing-file/`exec` errors, or "permission denied" messages emitted just before exit. (Derive `$LOG_GROUP`/`$LOG_STREAM` from the task definition's `logConfiguration` and the task ID.)

### Step 4: Check service health-check configuration and target health

```bash
aws ecs describe-services \
  --cluster "$CLUSTER" \
  --services "$SERVICE" \
  --region "$REGION" \
  --query 'services[0].{grace:healthCheckGracePeriodSeconds,lb:loadBalancers}'

aws elbv2 describe-target-health \
  --target-group-arn "$TARGET_GROUP_ARN" \
  --region "$REGION" \
  --query 'TargetHealthDescriptions[].{target:Target.Id,state:TargetHealth.State,reason:TargetHealth.Reason,desc:TargetHealth.Description}'
```

Expected output: the configured `healthCheckGracePeriodSeconds` and load balancer block, plus per-target health state/reason (e.g. `unhealthy` / `Target.ResponseCodeMismatch` / `Request timed out`).

## Causes

### Cause A: Container image cannot be pulled
**Statement:** ECS cannot retrieve the task's container image because the image reference is wrong or the pull path is broken (missing/incorrect tag, registry-auth/IAM failure, no network route to the registry, or DockerHub rate limit), so no container ever starts.
**Chain:**
- root: image pull request fails at task launch
- s1: container never enters RUNNING; task is stopped before app code executes
- D: task repeatedly stops / service never reaches steady state
**Indicators:**
- root: [Step 2] `stoppedReason` contains `CannotPullContainerError`
  <!-- match: {"step": 2, "predicate": "contains", "target": "CannotPullContainerError"} -->
- root: [Step 2] for Fargate registry-auth/network failures, `stopCode` is `ResourceInitializationError` and `stoppedReason` contains `unable to pull secrets or registry auth`
  <!-- match: {"step": 2, "predicate": "contains", "target": "ResourceInitializationError"} -->
- s1: [Step 3] no application log lines exist for the container (stream empty or absent)
  <!-- match: {"step": 3, "predicate": "absent", "target": "application startup log line"} -->
**Interventions:**
- **remediation** (root): Fix the image reference and pull path — correct the `image` name/tag in the task definition, attach an `executionRoleArn` with `AmazonECSTaskExecutionRolePolicy` (ECR pull + CloudWatch), and confirm the task's subnet has a route to the registry (NAT gateway or ECR VPC endpoints). Then register the revision and force a new deployment.

  ```bash
  aws ecs register-task-definition --cli-input-json file://taskdef.json --region "$REGION"
  aws ecs update-service --cluster "$CLUSTER" --service "$SERVICE" \
    --task-definition "$FAMILY" --force-new-deployment --region "$REGION"
  ```

  **Verification:** Re-run Step 2 on the next launched task; `stoppedReason` no longer contains `CannotPullContainerError` and the task reaches `RUNNING`.
- **defensive_fix** (s1): For DockerHub rate-limit pulls, authenticate the pull or mirror the image into private ECR to remove the anonymous quota.

  ```bash
  aws ecr create-repository --repository-name "$REPO" --region "$REGION"
  docker tag "$SRC_IMAGE" "$ACCOUNT.dkr.ecr.$REGION.amazonaws.com/$REPO:$TAG"
  aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$ACCOUNT.dkr.ecr.$REGION.amazonaws.com"
  docker push "$ACCOUNT.dkr.ecr.$REGION.amazonaws.com/$REPO:$TAG"
  ```

  **Verification:** New task pulls from ECR; Step 2 shows no `CannotPullContainerError` and Step 3 shows app startup logs.

### Cause B: Container exhausts its memory allocation and is OOM-killed
**Statement:** The container's working-set memory exceeds the memory allocated in the task definition (task/container `memory` or `memoryReservation`), so the kernel OOM-killer terminates it.
**Chain:**
- root: process memory usage exceeds the task definition's memory limit
- s1: kernel OOM-killer terminates the container process
- s2: essential container exits with a memory-related exit code; ECS stops the task
- D: task repeatedly stops / service never reaches steady state
**Indicators:**
- root: [Step 2] `stoppedReason` contains `OutOfMemoryError: Container killed due to memory usage`
  <!-- match: {"step": 2, "predicate": "contains", "target": "OutOfMemoryError: Container killed due to memory usage"} -->
- s2: [Step 2] container `exitCode` is `137` (SIGKILL) or `139` (segfault on an unavailable memory region)
  <!-- match: {"step": 2, "predicate": "exit_code", "target": 137} -->
- s1: [Step 3] logs show a `java.lang.OutOfMemoryError` / allocation failure / abrupt truncation just before exit
  <!-- match: {"step": 3, "predicate": "contains", "target": "OutOfMemoryError"} -->
**Interventions:**
- **remediation** (root): Raise the memory allocation to fit observed usage (task-level `memory` and/or container `memory`), register the revision, and redeploy.

  ```bash
  aws ecs register-task-definition --cli-input-json file://taskdef.json --region "$REGION"
  aws ecs update-service --cluster "$CLUSTER" --service "$SERVICE" \
    --task-definition "$FAMILY" --force-new-deployment --region "$REGION"
  ```

  **Verification:** After redeploy, watch the task's `MemoryUtilization` and confirm it stays below 100%; re-run Step 2 — no `OutOfMemoryError`, task stays `RUNNING`.

  ```bash
  aws cloudwatch get-metric-statistics --namespace AWS/ECS --metric-name MemoryUtilization \
    --dimensions Name=ClusterName,Value="$CLUSTER" Name=ServiceName,Value="$SERVICE" \
    --start-time "$START" --end-time "$END" --period 60 --statistics Maximum --region "$REGION"
  ```

### Cause C: Application/entrypoint error causes the essential container to exit
**Statement:** The essential container's own process fails — a bad `ENTRYPOINT`/`CMD`, a missing binary, a config/permission error, or an application crash — so it exits non-zero and ECS stops the whole task.
**Chain:**
- root: essential container process fails on startup or crashes at runtime
- s1: essential container exits non-zero; ECS stops the task because an essential container exited
- D: task repeatedly stops / service never reaches steady state
**Indicators:**
- s1: [Step 2] `stoppedReason` contains `Essential container in task exited`
  <!-- match: {"step": 2, "predicate": "contains", "target": "Essential container in task exited"} -->
- root: [Step 2] container `exitCode` is `255` (the `ENTRYPOINT`/`CMD` failed because of an error) or `1` (application error)
  <!-- match: {"step": 2, "predicate": "exit_code", "target": 255} -->
- root: [Step 3] logs show the application stack trace, `exec ... no such file or directory`, or `permission denied` immediately before exit
  <!-- match: {"step": 3, "predicate": "contains", "target": "no such file or directory"} -->
**Interventions:**
- **remediation** (root): Fix the container command/application — correct the `entryPoint`/`command`/`image` so the binary exists and is executable, or fix the application bug surfaced in Step 3 logs. Register the revision and redeploy.

  ```bash
  aws ecs register-task-definition --cli-input-json file://taskdef.json --region "$REGION"
  aws ecs update-service --cluster "$CLUSTER" --service "$SERVICE" \
    --task-definition "$FAMILY" --force-new-deployment --region "$REGION"
  ```

  **Verification:** Re-run Step 2 on the new task — container `exitCode` is `0`/absent and `lastStatus` is `RUNNING`; Step 3 shows the app reaching its normal "listening"/ready log line.

### Cause D: Tasks fail load balancer health checks before becoming ready
**Statement:** The task starts but does not pass the target group health check within the service's `healthCheckGracePeriodSeconds` — because the app is still bootstrapping, or the health check port/path is misconfigured — so ECS deregisters and stops the task and starts a new one in a loop.
**Chain:**
- root: task cannot pass the ELB health check within the configured grace period (slow startup or wrong port/path)
- s1: target group marks the task `unhealthy`
- s2: ECS scheduler deregisters and stops the task, then launches a replacement
- D: task repeatedly stops / service never reaches steady state
**Indicators:**
- s2: [Step 2] `stoppedReason` contains `Task failed ELB health checks in (target-group`
  <!-- match: {"step": 2, "predicate": "contains", "target": "Task failed ELB health checks in (target-group"} -->
- s1: [Step 4] `describe-target-health` shows the target `state` `unhealthy` with reason such as `Target.ResponseCodeMismatch` or `Request timed out`
  <!-- match: {"step": 4, "predicate": "contains", "target": "unhealthy"} -->
- root: [Step 4] `healthCheckGracePeriodSeconds` is shorter than the app's observed cold-start time (e.g. grace period `0`/unset while app needs tens of seconds)
  <!-- match: {"step": 4, "predicate": "threshold", "field": "healthCheckGracePeriodSeconds", "op": "lt", "value": 60} -->
**Interventions:**
- **remediation** (root): Correct the target group health check `port`/`path`/expected matcher to hit a real ready endpoint the container serves (addresses the wrong-port/path branch of the root).

  ```bash
  aws elbv2 modify-target-group --target-group-arn "$TARGET_GROUP_ARN" \
    --health-check-path "/healthz" --health-check-port traffic-port \
    --matcher HttpCode=200 --region "$REGION"
  ```

  **Verification:** Re-run Step 4 — targets transition to `healthy`; service event log shows it reached a steady state.
- **defensive_fix** (s1): Increase the service health check grace period so the scheduler ignores health checks while the app cold-starts (addresses the slow-startup branch).

  ```bash
  aws ecs update-service --cluster "$CLUSTER" --service "$SERVICE" \
    --health-check-grace-period-seconds 120 --region "$REGION"
  ```

  **Verification:** Re-run Step 4 — `healthCheckGracePeriodSeconds` reflects the new value and targets reach `healthy`; Step 1 stops returning fresh stopped tasks.

### Cause Z: Unidentified
**Statement:** None of the known stop signatures above match — the `stoppedReason`/`exitCode` and logs do not correspond to a recognized image-pull, OOM, application-exit, or health-check failure.
**Indicators:**
- [Default]
**Interventions:**
- **mitigation** (D): Capture a full diagnostic snapshot of the stopped task, service state, and logs, then escalate to the ECS SME with the artifacts attached.

  ```bash
  aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK_ARN" --region "$REGION" > task.json
  aws ecs describe-services --cluster "$CLUSTER" --services "$SERVICE" --region "$REGION" > service.json
  aws ecs describe-task-definition --task-definition "$FAMILY" --region "$REGION" > taskdef.json
  aws logs get-log-events --log-group-name "$LOG_GROUP" --log-stream-name "$LOG_STREAM" --region "$REGION" > container.log
  ```

  **Risk:** Diagnostic-only; collecting logs/describe output makes no change to the running service. **Duration:** Indefinite — escalate promptly so the crash loop is investigated. **Verification:** `task.json`, `service.json`, `taskdef.json`, and `container.log` are non-empty and attached to the escalation.

## Prevention

- Pin images by immutable digest or controlled tag, mirror third-party images into private ECR, and validate the `executionRoleArn` (ECR pull + CloudWatch Logs) and registry network path (NAT gateway or ECR VPC endpoints) before deploys.
- Size task memory from observed `MemoryUtilization`; alarm on `AWS/ECS` `MemoryUtilization` ≥ 85% so OOM kills are caught before they crash-loop.
- Validate `entryPoint`/`command` and run the image locally before registering; gate deploys on a smoke test of the container's start path.
- Set `healthCheckGracePeriodSeconds` to comfortably exceed measured cold-start time, and point the target group health check at a lightweight readiness endpoint with the correct port/path/matcher.
- Add a CloudWatch alarm on the service event "failed to reach steady state" / a low `RunningTaskCount`, and subscribe to ECS Task State Change events (EventBridge → SNS) to alert on repeated `STOPPED` transitions.

## Sources

- [Ecs task stopped](https://repost.aws/knowledge-center/ecs-task-stopped) — top-level stopped-task troubleshooting workflow: `aws ecs list-tasks --desired-status STOPPED`, `aws ecs describe-tasks`, `stopCode`/`stoppedReason`/`reason` fields, exit codes (0/1/137) and their meanings.
- [Stopped task error codes](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/stopped-task-error-codes.html) — exact `stoppedReason` strings (`CannotPullContainerError`, `ResourceInitializationError`, `OutOfMemoryError`, `Essential container in task exited`) and exit-code semantics (137 SIGKILL, 139 memory region unavailable, 255 ENTRYPOINT/CMD error).
- [Task cannot pull image](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task_cannot_pull_image.html) — `CannotPullContainerError` root causes (network, IAM/execution role, DockerHub rate quota, bad tag, cross-account image, full filesystem).
- [Resource initialization error](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/resource-initialization-error.html) — `ResourceInitializationError` (Fargate cannot pull registry auth / connectivity to ECR).
- [Out of memory](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/out-of-memory.html) — `OutOfMemoryError: Container killed due to memory usage` message and memory-limit cause.
- [Troubleshoot unhealthy checks ecs](https://repost.aws/knowledge-center/troubleshoot-unhealthy-checks-ecs) — `Task failed ELB health checks in (target-group ...)`, grace period, target group port/path, `aws elbv2 describe-target-health`, `health-check-grace-period-seconds` on `update-service`.
- [Troubleshoot service load balancers](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/troubleshoot-service-load-balancers.html) — service load-balancer / health-check-grace-period guidance for services that never reach steady state.
