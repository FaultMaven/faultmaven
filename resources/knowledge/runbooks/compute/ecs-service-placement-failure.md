---
id: "ecs-service-placement-failure"
title: "AWS ECS service unable to place tasks (insufficient resources, ENI/port/constraint exhaustion)"
domain: compute
service: aws-ecs
symptom_class: [scheduling_failure]
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-06-24"
verified_by: "kb-researcher"
status: draft
tags: [ecs, task-placement, resource-cpu, resource-memory, resource-eni, capacity-provider]
difficulty: intermediate
---

## Symptom Recognition

- Service event (ECS console "Events" tab / `describe-services`):
  `(service my-service) was unable to place a task because no container instance met all of its requirements.`
- `runningCount` stays below `desiredCount`; the service never reaches steady state and does not emit `has reached a steady state.`
- New deployments hang; tasks never leave `PENDING`, or with capacity providers never leave `PROVISIONING`.
- Variant root-cause phrasing in the same event stream:
  - `...has insufficient CPU units available.`
  - `...has insufficient memory available.`
  - `...is already using a port required by your task.`
  - `...has no available network interfaces.` (awsvpc network mode)
  - No instance satisfied a `memberOf` placement constraint expression.

## Applicability

- Amazon ECS on EC2 launch type (capacity owned by you). Fargate-launch services do not hit container-instance resource/ENI/port exhaustion; for Fargate placement failures see `TaskFailedToStart`/subnet-IP exhaustion instead.
- Container agent 1.28.1+ and `ecs-init` 1.28.1-2+ required for ENI trunking (`awsvpcTrunking`).
- Required IAM: `ecs:DescribeServices`, `ecs:ListContainerInstances`, `ecs:DescribeContainerInstances`, `ecs:DescribeTaskDefinition`, `ecs:DescribeCapacityProviders`, `autoscaling:DescribeAutoScalingGroups`.
- Tools: AWS CLI v2 (`aws ecs`, `aws autoscaling`), `jq`.
- Environment: `export CLUSTER=my-cluster SERVICE=my-service` before running the commands below.

## Diagnostic Steps

### Step 1: Read the service events to capture the exact placement-failure reason

```bash
aws ecs describe-services --cluster "$CLUSTER" --services "$SERVICE" \
  --query 'services[0].{desired:desiredCount,running:runningCount,pending:pendingCount,events:events[0:5].message}' \
  --output json
```

Expected output: `desired` > `running`, and an `events` message containing `was unable to place a task because no container instance met all of its requirements`. Note any trailing reason (CPU / memory / port / network interfaces / constraint).

### Step 2: Read the task definition's resource requirements

```bash
TD=$(aws ecs describe-services --cluster "$CLUSTER" --services "$SERVICE" \
  --query 'services[0].taskDefinition' --output text)
aws ecs describe-task-definition --task-definition "$TD" \
  --query 'taskDefinition.{taskCpu:cpu,taskMem:memory,netMode:networkMode,
    containers:containerDefinitions[].{name:name,cpu:cpu,mem:memory,memRes:memoryReservation,
    ports:portMappings},constraints:placementConstraints}' --output json
```

Expected output: per-task and per-container `cpu` (1024 units = 1 vCPU), `memory`/`memoryReservation` (MiB), `networkMode`, host `portMappings`, and any `placementConstraints`.

### Step 3: Inspect remaining resources on every container instance

```bash
aws ecs list-container-instances --cluster "$CLUSTER" --query 'containerInstanceArns' --output text \
| tr '\t' '\n' | xargs -r aws ecs describe-container-instances --cluster "$CLUSTER" --container-instances \
  --query 'containerInstances[].{arn:containerInstanceArn,status:status,agent:agentConnected,
    running:runningTasksCount,
    remCPU:remainingResources[?name==`CPU`].integerValue|[0],
    remMEM:remainingResources[?name==`MEMORY`].integerValue|[0],
    usedPorts:remainingResources[?name==`PORTS`].stringSetValue|[0]}' --output table
```

Expected output: one row per registered container instance. `remCPU` (CPU units) and `remMEM` (MiB) are what is left for new tasks; `usedPorts` lists reserved host ports (defaults 22, 2375, 2376, 51678-51680 plus task host ports).

### Step 4: Check container-instance attributes against the placement constraints

```bash
aws ecs list-container-instances --cluster "$CLUSTER" --query 'containerInstanceArns' --output text \
| tr '\t' '\n' | xargs -r aws ecs describe-container-instances --cluster "$CLUSTER" --container-instances \
  --query 'containerInstances[].{arn:containerInstanceArn,
    attrs:attributes[?starts_with(name,`ecs`)==`false`].{n:name,v:value}}' --output json
```

Expected output: custom attributes per instance. Compare against the `memberOf` expression(s) from Step 2; if no instance matches, the constraint excludes all of them.

### Step 5: Check the capacity provider / Auto Scaling group (if used)

```bash
aws ecs describe-services --cluster "$CLUSTER" --services "$SERVICE" \
  --query 'services[0].capacityProviderStrategy' --output json
ASG=$(aws ecs describe-capacity-providers --capacity-providers \
  $(aws ecs describe-clusters --clusters "$CLUSTER" \
    --query 'clusters[0].defaultCapacityProviderStrategy[0].capacityProvider' --output text) \
  --query 'capacityProviders[0].autoScalingGroupProvider.autoScalingGroupArn' --output text)
aws autoscaling describe-auto-scaling-groups --query \
  "AutoScalingGroups[?contains(AutoScalingGroupARN,'$(echo $ASG | awk -F/ '{print $NF}')')].{min:MinSize,max:MaxSize,desired:DesiredCapacity,inService:Instances[?LifecycleState=='InService']|length(@)}" \
  --output table
```

Expected output: the strategy plus the ASG's `min`/`max`/`desired`/`inService`. `max:0` or `desired==max` with capacity still short means managed scaling cannot add instances.

## Causes

### Cause A: Task CPU/memory request exceeds the largest free slot on any instance
**Statement:** The task definition's reserved CPU units and/or memory (MiB) are larger than the `remainingResources` on every registered container instance, so the scheduler finds no instance with a free slot.
**Chain:**
- root: per-task or per-container CPU/memory reservation exceeds every instance's free capacity
- s1: each instance is filtered out in the resource-matching phase (CPU/GPU/memory/port filter)
- D: service emits the placement-failure event and `runningCount` < `desiredCount`
**Indicators:**
- root: [Step 2] task/container `cpu`/`memory` value is >= any instance's `remCPU`/`remMEM` from Step 3 (e.g. task `cpu:1024` but every `remCPU` < 1024)
- root: [Step 1] event reason includes insufficient CPU/memory
- s1: [Step 3] every row shows `remCPU`/`remMEM` below the Step 2 requirement
**Interventions:**
- **remediation** (root): right-size the task so it fits an instance, or run on larger instances. Lower the reservation in the task definition, register a new revision, and update the service.

  ```bash
  aws ecs register-task-definition --cli-input-json file://taskdef.json   # cpu/memory lowered
  aws ecs update-service --cluster "$CLUSTER" --service "$SERVICE" \
    --task-definition "$(aws ecs describe-task-definition --task-definition "$TD" --query 'taskDefinition.family' --output text)"
  ```

  **Verification:** re-run Step 1; `running` reaches `desired` and a `has reached a steady state.` event appears.
- **mitigation** (s1): add capacity by raising the ASG desired count (or launch an instance) so a fitting slot exists.

  ```bash
  aws autoscaling set-desired-capacity --auto-scaling-group-name "<asg-name>" --desired-capacity <N+1> --honor-cooldown
  ```

  **Risk:** higher EC2 spend; over-provisioning if the real fix is right-sizing. **Duration:** until the task definition is right-sized or load drops. **Verification:** re-run Step 3; at least one instance shows `remCPU`/`remMEM` >= the Step 2 requirement.

### Cause B: Required host port is already reserved on every candidate instance
**Statement:** The task uses `bridge`/`host` network mode with a fixed host `containerPort`/`hostPort`, and that port is already in `remainingResources` PORTS (reserved by another task or a default reserved port) on every instance.
**Chain:**
- root: a fixed host port in the task definition collides with a port already reserved on all instances
- s1: each instance is filtered out in the port-matching phase of placement
- D: service emits the placement-failure event and tasks stay PENDING
**Indicators:**
- root: [Step 2] a `portMappings` entry has a fixed `hostPort` (non-zero) under `bridge`/`host` mode
- s1: [Step 3] that port appears in every instance's `usedPorts` set
**Interventions:**
- **remediation** (root): use dynamic host port mapping (set `hostPort` to `0`, let ECS assign an ephemeral port) and front the service with an ALB target group. Register a new revision and update the service.

  ```bash
  # taskdef.json portMappings: [{ "containerPort": 8080, "hostPort": 0, "protocol": "tcp" }]
  aws ecs register-task-definition --cli-input-json file://taskdef.json
  aws ecs update-service --cluster "$CLUSTER" --service "$SERVICE" --task-definition <family>
  ```

  **Verification:** re-run Step 1; tasks place and reach steady state; Step 3 `usedPorts` now shows ECS-assigned ephemeral ports (32768-65535 range), not a fixed collision.
- **mitigation** (s1): add instances so a host with the port free becomes available.

  ```bash
  aws autoscaling set-desired-capacity --auto-scaling-group-name "<asg-name>" --desired-capacity <N+1> --honor-cooldown
  ```

  **Risk:** only buys one extra task per added instance; does not scale. **Duration:** until dynamic ports are adopted. **Verification:** re-run Step 3; a new instance shows the required port absent from `usedPorts`.

### Cause C: awsvpc tasks exhausted the per-instance ENI limit
**Statement:** Tasks use `awsvpc` network mode (one ENI per task) and every instance has reached its maximum attachable elastic network interfaces, leaving no instance with ENI capacity to attach a new task ENI.
**Chain:**
- root: awsvpc task ENI demand exceeds the per-instance ENI attachment limit on every instance
- s1: each instance reports no available network interface during placement
- D: service emits the placement-failure / no-available-network-interfaces event
**Indicators:**
- root: [Step 2] `netMode` is `awsvpc`
- s1: [Step 1] event reason references no available network interfaces
- s1: [Step 3] `running` task count per instance equals that instance type's ENI ceiling (e.g. 2 awsvpc tasks on a c5.large without trunking)
**Interventions:**
- **remediation** (root): enable ENI trunking (`awsvpcTrunking`) so supported instances get a higher ENI limit (e.g. c5.large rises from 3 to 12 ENIs → ~10 tasks instead of 2). Only instances launched AFTER enabling it get the trunk interface.

  ```bash
  aws ecs put-account-setting --name awsvpcTrunking --value enabled
  # then roll the ASG so new instances launch with trunking
  aws autoscaling start-instance-refresh --auto-scaling-group-name "<asg-name>"
  ```

  **Verification:** on a freshly launched instance, `describe-container-instances` shows the trunk ENI and more awsvpc tasks place; re-run Step 1 for steady state.
- **mitigation** (s1): scale the ASG out so more instances (each with spare ENI slots) are available.

  ```bash
  aws autoscaling set-desired-capacity --auto-scaling-group-name "<asg-name>" --desired-capacity <N+1> --honor-cooldown
  ```

  **Risk:** linear, expensive scaling around the low default ENI cap. **Duration:** until trunking is enabled and instances are refreshed. **Verification:** re-run Step 3; new instances host additional awsvpc tasks without the no-ENI event.

### Cause D: A placement constraint excludes every container instance
**Statement:** A `memberOf` placement constraint expression (or required custom attribute) on the service/task definition matches no registered container instance, so the constraint-matching phase eliminates all candidates.
**Chain:**
- root: the placement constraint expression references an attribute no instance carries
- s1: the constraint-matching phase returns zero eligible instances
- D: service emits the placement-failure event despite free CPU/memory existing
**Indicators:**
- root: [Step 2] a `constraints` entry of type `memberOf` with an `expression` (e.g. `attribute:ecs.instance-type == c5.xlarge`)
- s1: [Step 4] no instance's `attrs` satisfy that expression
- s1: [Step 3] instances have ample `remCPU`/`remMEM` yet tasks still do not place (resources are not the limiter)
**Interventions:**
- **remediation** (root): fix the constraint to match real attributes, or add the attribute to instances. Correct the expression in the task/service definition and redeploy.

  ```bash
  # Option 1: relax/correct the expression in taskdef placementConstraints, then:
  aws ecs register-task-definition --cli-input-json file://taskdef.json
  aws ecs update-service --cluster "$CLUSTER" --service "$SERVICE" --task-definition <family>
  # Option 2: tag instances so they satisfy the existing constraint:
  aws ecs put-attributes --cluster "$CLUSTER" \
    --attributes name=<attr>,value=<val>,targetType=container-instance,targetId=<container-instance-id>
  ```

  **Verification:** re-run Step 4; at least one instance now satisfies the expression; re-run Step 1 for steady state.

### Cause E: Capacity provider / Auto Scaling group cannot add fitting instances
**Statement:** The service runs via a capacity provider whose Auto Scaling group has `MaxSize` of 0 (or is already at max), or the task's resource requirements exceed the group's smallest instance type, so managed scaling never adds a usable instance and tasks stay in PROVISIONING.
**Chain:**
- root: ASG cannot scale out usable capacity (MaxSize=0 / at max, or instance type too small for the task)
- s1: managed scaling adds no instance that can host the task
- s2: tasks remain stuck in PROVISIONING
- D: service never reaches desiredCount and emits the placement-failure event
**Indicators:**
- root: [Step 5] ASG `max` is `0`, or `desired == max` while capacity is still short
- root: [Step 2] task `cpu`/`memory` exceeds the capacity of the ASG's smallest instance type
- s2: [Step 1] tasks observed in PROVISIONING and not transitioning to RUNNING
**Interventions:**
- **remediation** (root): give the ASG headroom and an instance type that fits the task. Raise `MaxSize` (and switch to a larger instance type / a dedicated ASG for large tasks).

  ```bash
  aws autoscaling update-auto-scaling-group --auto-scaling-group-name "<asg-name>" --max-size <N>
  ```

  **Verification:** re-run Step 5; `max` > 0 and `inService` grows; re-run Step 1 until steady state. Do not edit the managed scaling policy directly.
- **defensive_fix** (s1): set capacity-provider managed scaling Target Capacity below 100% (e.g. 80%) so the cluster keeps spare capacity and new tasks place immediately.

  ```bash
  aws ecs update-capacity-provider --name "<capacity-provider>" \
    --auto-scaling-group-provider managedScaling="{status=ENABLED,targetCapacity=80}"
  ```

  **Verification:** re-run Step 3; instances show spare `remCPU`/`remMEM`, and subsequent placements do not wait on a scale-out.

### Cause Z: Unidentified
**Statement:** The placement failure does not match any cause above (e.g. agent disconnected on instances, GPU/Elastic Inference requirement, subnet IP exhaustion for awsvpc, or an undiagnosed scheduler condition).
**Indicators:**
- [Default]
**Interventions:**
- **mitigation** (D): capture a full diagnostic snapshot and escalate to the ECS/platform SME.

  ```bash
  {
    echo "=== service ==="; aws ecs describe-services --cluster "$CLUSTER" --services "$SERVICE";
    echo "=== task def ==="; aws ecs describe-task-definition --task-definition "$TD";
    echo "=== container instances ==="; aws ecs list-container-instances --cluster "$CLUSTER" --query 'containerInstanceArns' --output text \
      | tr '\t' '\n' | xargs -r aws ecs describe-container-instances --cluster "$CLUSTER" --container-instances;
    echo "=== capacity providers ==="; aws ecs describe-clusters --clusters "$CLUSTER" --include ATTACHMENTS SETTINGS;
  } > ecs-placement-snapshot-$(date +%Y%m%dT%H%M%S).txt
  ```

  **Risk:** none (read-only capture). **Duration:** n/a. **Verification:** snapshot file written; attach to the escalation ticket for the SME.

## Prevention

- Right-size task `cpu`/`memory` to leave headroom on the chosen instance type (1024 CPU units = 1 vCPU); avoid requesting a full instance per task.
- Use dynamic host ports (`hostPort: 0`) with an ALB target group instead of fixed host ports to avoid port-collision placement failures.
- Enable `awsvpcTrunking` ahead of scaling awsvpc workloads, and refresh the ASG so instances carry the trunk ENI.
- Keep capacity-provider Target Capacity below 100% (e.g. 80%) so the cluster holds spare capacity for immediate placement.
- Use separate Auto Scaling groups / capacity providers per minimum resource requirement so large tasks always have a fitting instance type; ensure every ASG has `MaxSize` > 0.
- Alert on `DesiredCount > RunningCount` sustained for N minutes (CloudWatch) and on repeated `unable to place a task` service events.

## Sources

- [Service event messages list](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/service-event-messages-list.html) — verbatim "unable to place a task because no container instance met all of its requirements" event and CPU/memory/port/ENI failure reasons.
- [Task placement](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task-placement.html) — placement filter order (CPU/GPU/memory/port, then constraints).
- [Task placement constraints](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task-placement-constraints.html) — `memberOf` placement-constraint behavior and cluster query language.
- [Describe container instances](https://docs.aws.amazon.com/cli/latest/reference/ecs/describe-container-instances.html) and https://docs.aws.amazon.com/AmazonECS/latest/APIReference/API_ContainerInstance.html — `registeredResources` vs `remainingResources` (CPU/MEMORY/PORTS), default reserved ports 22/2375/2376/51678-51680.
- [Container instance eni](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/container-instance-eni.html) — per-instance ENI limits, `awsvpcTrunking` (c5.large 3→12 ENIs), agent 1.28.1+ prerequisite, only new instances get trunking.
- [Asg capacity providers](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/asg-capacity-providers.html) and https://docs.aws.amazon.com/AmazonECS/latest/developerguide/cluster-auto-scaling.html — capacity-provider managed scaling, tasks stuck in PROVISIONING when MaxSize=0 or task exceeds smallest instance type, Target Capacity for spare headroom.
- [Troubleshooting](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/troubleshooting.html) — ECS troubleshooting entry point (resource-shortage placement guidance).
