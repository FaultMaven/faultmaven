---
id: "gke-workload-unavailable"
title: "GKE Workloads Not Running: Pods Unschedulable or Crashing"
domain: compute
service: gcp-gke
symptom_class: [scheduling_failure, service_unavailable]
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-06-24"
verified_by: "kb-researcher"
status: draft
tags: [failed-scheduling, pod-unschedulable, crash-loop-back-off, no-scale-up, autopilot, insufficient-cpu]
difficulty: intermediate
---

## Symptom Recognition

- Pods stuck in `Pending`, `kubectl get pods` STATUS column shows `Pending` or reason `PodUnschedulable`.
- Pod event (reason `FailedScheduling`): `0/N nodes are available: Insufficient cpu` (or `Insufficient memory`).
- Pod event: `No nodes are available that match all of the predicates: Insufficient cpu (2)`.
- Pod event: `0/1 nodes are available: 1 node(s) didn't have free ports for the requested pod ports`.
- Pods stuck in `CrashLoopBackOff` or `ImagePullBackOff`/`ErrImagePull`; container last state `Terminated` with exit code `137` (OOMKilled).
- Cluster-autoscaler visibility log message id `no.scale.up.nap.pod.zonal.resources.exceeded` or `scale.up.error.quota.exceeded`.
- Autopilot admission webhook event: `Total ephemeral-storage requested by containers for workload '' is higher than the Autopilot maximum of '10Gi'.`
- Workload event: `Does not have minimum availability`.

## Applicability

- GKE Standard and Autopilot clusters (Kubernetes 1.27+); GKE control-plane and node-pool autoscaling.
- Required access: `roles/container.viewer` (read) and `roles/container.developer` (mutate workloads/node pools); `kubectl` configured against the cluster.
- Tools: `gcloud` CLI (with the GKE auth plugin `gke-gcloud-auth-plugin`), `kubectl`, project access to Cloud Logging.
- Obtain credentials first:

```bash
gcloud container clusters get-credentials CLUSTER_NAME --location LOCATION --project PROJECT_ID
```

## Diagnostic Steps

### Step 1: List workload pods and their status

```bash
kubectl get pods -n NAMESPACE -o wide
```

Expected output: a table with STATUS and NODE columns. `Pending` with no NODE indicates a scheduling failure; `CrashLoopBackOff`/`ImagePullBackOff` indicates a runtime failure.

### Step 2: Inspect the failing pod's events and container state

```bash
kubectl describe pod POD_NAME -n NAMESPACE
```

Expected output: the `Events:` section lists scheduler/kubelet messages (reason `FailedScheduling`, `BackOff`, `Failed`); the `Last State:` field under each container shows `Terminated` with an exit `Reason` and `Exit Code`.

### Step 3: Read crashing container logs (current and previous attempt)

```bash
kubectl logs POD_NAME -n NAMESPACE --previous
```

Expected output: stdout/stderr of the last terminated container. An application stack trace or `Get "https://example.com/healthy": EOF` (failed probe) points to a runtime cause; empty output with exit code `137` points to OOM.

### Step 4: Check node resource availability and taints

```bash
kubectl describe nodes | grep -A6 "Allocated resources"
```

Expected output: per-node `Requests` for cpu/memory. If allocatable cpu/memory is near 100% requested across all nodes, the cluster lacks capacity for new pods.

### Step 5: Inspect cluster autoscaler status and node-pool limits

```bash
kubectl describe configmap cluster-autoscaler-status -n kube-system
```

Expected output: per-node-pool `Health` and `ScaleUp` sections showing min/max node counts and backoff state. Then confirm the node pool's max size:

```bash
gcloud container node-pools describe NODE_POOL --cluster CLUSTER_NAME --location LOCATION --format="value(autoscaling.maxNodeCount)"
```

Expected output: the configured maximum node count for the pool.

### Step 6: Query cluster-autoscaler scale-up decisions in Cloud Logging

```bash
gcloud logging read 'resource.type="k8s_cluster" AND log_id("container.googleapis.com/cluster-autoscaler-visibility") AND jsonPayload.noDecisionStatus.noScaleUp:*' --project PROJECT_ID --limit 20 --format="value(jsonPayload.noDecisionStatus.noScaleUp.reason.messageId)"
```

Expected output: scale-up rejection reason ids such as `no.scale.up.nap.pod.zonal.resources.exceeded`, `no.scale.up.mig.failing.predicate`, or scale-up error ids such as `scale.up.error.quota.exceeded`. No rows means autoscaler is not the cause.

## Causes

### Cause A: Insufficient cluster capacity with autoscaling at its ceiling
**Statement:** Existing nodes lack free cpu/memory for the pod's requests, and the responsible node pool is already at its `maxNodeCount`, so the cluster autoscaler cannot add a node and the pod stays unschedulable.
**Chain:**
- root: node pool is at its autoscaling maximum while all nodes are fully requested
- s1: cluster autoscaler declines to add a node for the unschedulable pod
- s2: scheduler finds no node with sufficient cpu/memory
- D: pod stays Pending / PodUnschedulable
**Indicators:**
- root: [Step 5] `maxNodeCount` equals the current node count and autoscaler status shows the pool at its ceiling
- s1: [Step 6] scale-up log shows a noScaleUp reason for the pool
  <!-- match: {"step": 6, "predicate": "contains", "target": "no.scale.up"} -->
- s2: [Step 2] FailedScheduling event reports `Insufficient cpu` or `Insufficient memory`
  <!-- match: {"step": 2, "predicate": "contains", "target": "Insufficient cpu"} -->
- D: [Symptom] pod STATUS is `Pending` with reason `PodUnschedulable`
**Interventions:**
- **remediation** (root): raise the node pool's autoscaling maximum so the autoscaler can provision capacity.

  ```bash
  gcloud container clusters update CLUSTER_NAME --location LOCATION \
    --node-pool NODE_POOL --enable-autoscaling --min-nodes 1 --max-nodes 10
  ```

  **Verification:** re-run Step 1; the pod transitions from `Pending` to `Running` once a new node joins, and Step 5 shows current nodes below the new max.
- **mitigation** (s2): manually scale the node pool up to relieve the immediate shortage.

  ```bash
  gcloud container clusters resize CLUSTER_NAME --location LOCATION \
    --node-pool NODE_POOL --num-nodes 5
  ```

  **Risk:** added nodes incur cost and may be removed again by the autoscaler if requests drop. **Duration:** until the workload's steady-state demand is sized correctly. **Verification:** `kubectl get nodes` shows additional Ready nodes and the pod schedules.

### Cause B: Pod resource requests exceed any single eligible node
**Statement:** The pod requests more cpu, memory, or ephemeral storage than the largest machine type in any eligible node pool can provide, so no node fits it and GKE does not trigger a scale-up.
**Chain:**
- root: pod resource requests exceed the allocatable capacity of the largest eligible node
- s1: scheduler predicate fails on every node and autoscaler will not add a node that still would not fit
- D: pod stays Pending / PodUnschedulable
**Indicators:**
- root: [Step 4] every node's allocatable cpu/memory is smaller than the pod's request shown in Step 2
- s1: [Step 6] scale-up log shows `no.scale.up.nap.pod.zonal.resources.exceeded` or a failing-predicate reason
  <!-- match: {"step": 6, "predicate": "contains", "target": "no.scale.up.nap.pod.zonal.resources.exceeded"} -->
- D: [Step 2] FailedScheduling event: `No nodes are available that match all of the predicates: Insufficient cpu`
  <!-- match: {"step": 2, "predicate": "contains", "target": "No nodes are available that match all of the predicates"} -->
**Interventions:**
- **remediation** (root): right-size the container's resource requests below the node's allocatable capacity.

  ```bash
  kubectl set resources deployment DEPLOYMENT_NAME -n NAMESPACE \
    --requests=cpu=500m,memory=512Mi
  ```

  **Verification:** re-run Step 2; the FailedScheduling event clears and the pod schedules onto a node within one scheduler cycle.
- **defensive_fix** (s1): create or enable a larger node pool (or node auto-provisioning) so a machine type large enough exists.

  ```bash
  gcloud container clusters update CLUSTER_NAME --location LOCATION \
    --enable-autoprovisioning --min-cpu 1 --max-cpu 64 --min-memory 1 --max-memory 256
  ```

  **Verification:** Step 6 no longer reports `zonal.resources.exceeded`; the autoscaler provisions a node sized for the pod.

### Cause C: Pod node affinity/selector or taint tolerations exclude all nodes
**Statement:** The pod's `nodeSelector`/`nodeAffinity` matches no node labels, or every candidate node carries a `NoSchedule` taint the pod does not tolerate, so the scheduler rejects all nodes.
**Chain:**
- root: pod selector/affinity or missing toleration excludes every node in the cluster
- s1: scheduler predicate `MatchNodeSelector` / `PodToleratesNodeTaints` fails on all nodes
- D: pod stays Pending / PodUnschedulable
**Indicators:**
- root: [Step 4] no node carries the label the pod selects, or candidate nodes show a `NoSchedule` taint in `kubectl describe nodes`
- s1: [Step 2] FailedScheduling event references `MatchNodeSelector` or `node(s) had untolerated taint`
  <!-- match: {"step": 2, "predicate": "contains", "target": "MatchNodeSelector"} -->
- D: [Symptom] pod STATUS is `Pending` with reason `PodUnschedulable`
**Interventions:**
- **remediation** (root): label the intended nodes to satisfy the pod's `nodeSelector`.

  ```bash
  kubectl label nodes NODE_NAME LABEL_KEY=LABEL_VALUE
  ```

  **Verification:** re-run Step 2; the `MatchNodeSelector` predicate no longer fails and the pod schedules.
- **defensive_fix** (s1): remove the blocking taint from the target nodes (or add a matching toleration to the pod spec).

  ```bash
  kubectl taint nodes NODE_NAME key:NoSchedule-
  ```

  **Verification:** `kubectl describe nodes NODE_NAME` no longer lists the taint and the pod binds to that node.

### Cause D: Container OOMKilled because memory limit is below working-set demand
**Statement:** The container's memory limit is set below its actual working-set usage, so the kernel OOM-kills it on every start, producing exit code `137` and a CrashLoopBackOff.
**Chain:**
- root: container memory limit is lower than the application's real memory demand
- s1: kernel OOM-kills the container shortly after start (exit code 137)
- s2: kubelet restarts the container repeatedly with exponential backoff
- D: pod reports CrashLoopBackOff and is not serving traffic
**Indicators:**
- root: [Step 2] container `Last State: Terminated`, `Reason: OOMKilled`, `Exit Code: 137`
  <!-- match: {"step": 2, "predicate": "contains", "target": "OOMKilled"} -->
- s1: [Step 2] exit code is `137`
  <!-- match: {"step": 2, "predicate": "contains", "target": "137"} -->
- s2: [Step 1] pod STATUS is `CrashLoopBackOff` with a rising restart count
  <!-- match: {"step": 1, "predicate": "contains", "target": "CrashLoopBackOff"} -->
- D: [Symptom] workload event `Does not have minimum availability`
**Interventions:**
- **remediation** (root): raise the container's memory request and limit above its measured working set.

  ```bash
  kubectl set resources deployment DEPLOYMENT_NAME -n NAMESPACE \
    --requests=memory=512Mi --limits=memory=1Gi
  ```

  **Verification:** re-run Step 1; restart count stops increasing and the pod reaches `Running`/`Ready`; Step 2 shows no new `OOMKilled` events.
- **mitigation** (s2): temporarily scale the deployment to add replicas so at least some pods stay up while the limit is tuned.

  ```bash
  kubectl scale deployment DEPLOYMENT_NAME -n NAMESPACE --replicas=4
  ```

  **Risk:** extra replicas consume more cluster capacity and each may still OOM until limits are fixed. **Duration:** until the memory limit is corrected. **Verification:** `kubectl get deployment DEPLOYMENT_NAME` shows at least one Available replica.

### Cause E: Autopilot rejects the workload for violating a resource constraint
**Statement:** On an Autopilot cluster the workload violates an Autopilot resource constraint (for example ephemeral-storage above the 10Gi maximum), so the admission webhook blocks the pod and the controller cannot create a running replica.
**Chain:**
- root: workload spec violates an Autopilot resource constraint
- s1: Autopilot admission webhook rejects the pod template
- s2: the workload controller cannot create a schedulable pod
- D: workload has no running pods / shows minimum-availability failure
**Indicators:**
- root: [Step 2] controller `Events:` show the Autopilot maximum message, e.g. `is higher than the Autopilot maximum of '10Gi'`
  <!-- match: {"step": 2, "predicate": "contains", "target": "Autopilot maximum"} -->
- s2: [Step 1] the Deployment/Job has zero pods listed in the namespace
- D: [Symptom] workload event `Does not have minimum availability`
**Interventions:**
- **remediation** (root): bring the request within the Autopilot limit (here, ephemeral-storage at or below 10Gi).

  ```bash
  kubectl set resources deployment DEPLOYMENT_NAME -n NAMESPACE \
    --requests=ephemeral-storage=10Gi --limits=ephemeral-storage=10Gi
  ```

  **Verification:** re-run Step 2; the admission webhook event no longer appears and the controller creates a pod that schedules.
- **defensive_fix** (s1): explicitly set requests for every container so Autopilot does not apply defaults that breach a constraint, and validate against documented Autopilot minimums/maximums before deploy.

  ```bash
  kubectl apply --dry-run=server -f workload.yaml
  ```

  **Verification:** the server-side dry run returns no admission error for the workload.

### Cause Z: Unidentified
**Statement:** The available diagnostics do not match any known cause above; the unavailability mechanism is not yet identified.
**Indicators:**
- [Default]
**Interventions:**
- **mitigation** (D): capture a full diagnostic snapshot and escalate to the SME.

  ```bash
  kubectl get pods -n NAMESPACE -o wide > snapshot.txt
  kubectl describe pod POD_NAME -n NAMESPACE >> snapshot.txt
  kubectl logs POD_NAME -n NAMESPACE --previous >> snapshot.txt 2>&1
  kubectl describe configmap cluster-autoscaler-status -n kube-system >> snapshot.txt
  kubectl get events -n NAMESPACE --sort-by=.lastTimestamp >> snapshot.txt
  ```

  **Risk:** snapshot may contain environment-specific data; review before sharing. **Duration:** until the SME responds. **Verification:** snapshot.txt is non-empty and attached to the escalation ticket.

## Prevention

- Set explicit, right-sized `requests` and `limits` on every container; reserve headroom so a single pod never needs more than a node's allocatable capacity.
- Configure node-pool autoscaling with realistic `--max-nodes`, or enable node auto-provisioning (`--enable-autoprovisioning`) so capacity scales before pods queue.
- Add a Cloud Monitoring alert on the `kubernetes.io/container/restart_count` metric and on the count of `Pending`/unschedulable pods.
- Alert on cluster-autoscaler visibility logs filtered to `no.scale.up.*` and `scale.up.error.*` message ids so scale-up failures surface proactively.
- For Autopilot, validate workloads against documented resource minimums/maximums and run `kubectl apply --dry-run=server` in CI before deploy.
- Keep node-pool taints and pod tolerations/selectors documented together so affinity drift does not silently strand pods.

## Sources

- [Troubleshooting](https://docs.cloud.google.com/kubernetes-engine/docs/troubleshooting) — top-level GKE troubleshooting index; confirmed symptom taxonomy (PodUnschedulable, CrashLoopBackOff, ImagePullBackOff, OOM).
- [Deployed workloads](https://docs.cloud.google.com/kubernetes-engine/docs/troubleshooting/deployed-workloads) — verbatim kubectl commands and event strings (`Insufficient cpu (2)`, `MatchNodeSelector`, `PodToleratesNodeTaints`, `Does not have minimum availability`, exit code `137`, taint/label/uncordon commands).
- [Cluster autoscaler scale up](https://docs.cloud.google.com/kubernetes-engine/docs/troubleshooting/cluster-autoscaler-scale-up) — `no.scale.up.*` and `scale.up.error.*` reason ids, `kubectl describe configmap cluster-autoscaler-status`, Cloud Logging visibility query, single-node/max-size scale-up behavior.
- [Autopilot clusters](https://docs.cloud.google.com/kubernetes-engine/docs/troubleshooting/autopilot-clusters) — Autopilot admission messages (`is higher than the Autopilot maximum of '10Gi'`) and resource-constraint rejection behavior.
- [Crashloopbackoff events](https://docs.cloud.google.com/kubernetes-engine/docs/troubleshooting/crashloopbackoff-events) — CrashLoopBackOff exit-code interpretation (0 vs non-zero) and `kubectl logs --previous` / `kubectl describe pod` workflow.
