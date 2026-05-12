---
id: k8s-pending-pods
title: "Kubernetes Pods Stuck in Pending State"
domain: compute
service: kubernetes
symptom_class:
  - scheduling_failure
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-05-12"
verified_by: kb-researcher
status: draft
tags:
  - kubernetes
  - scheduler
  - pending
  - scheduling
  - taints
  - affinity
  - pvc
  - quota
difficulty: intermediate
---

# Kubernetes Pods Stuck in Pending State

## Symptom Recognition

- `kubectl get pods` reports `STATUS: Pending` and `READY: 0/N` for the affected pod, with `AGE` increasing while the pod never transitions to `ContainerCreating` or `Running` (e.g. `my-app-abc123-xyz   0/1   Pending   0   15m`).
- `kubectl describe pod` shows a `FailedScheduling` event from `default-scheduler` (or the named scheduler in use) under the `Events` table, with a `Message` beginning `0/N nodes are available:` followed by a comma-separated list of predicate failures.
- Common scheduler message tails include `Insufficient cpu`, `Insufficient memory`, `Insufficient ephemeral-storage`, `node(s) didn't match Pod's node affinity/selector`, `node(s) didn't match Pod's nodeSelector`, `node(s) had untolerated taint`, `node(s) had taints that the pod didn't tolerate`, `pod has unbound immediate PersistentVolumeClaims`, `node(s) exceed max volume count`, `node(s) didn't satisfy existing pods anti-affinity rules`, `node(s) didn't match pod topology spread constraints`, and `node(s) didn't have free ports for the requested pod ports`.
- For ResourceQuota rejection the pod is never created in the first place: the controller (Deployment/ReplicaSet/StatefulSet) records a `FailedCreate` event with message `pods "<name>" is forbidden: exceeded quota: <quota-name>, requested: ..., used: ..., limited: ...`, and `kubectl get pods -n <namespace>` shows fewer replicas than desired with no Pending pod at all.
- For scheduling gates the pod status is the literal string `SchedulingGated`, not `Pending`, and the scheduler emits no `FailedScheduling` event because the pod has not yet entered the scheduling queue.
- The pod's `.status.conditions[?(@.type=="PodScheduled")].status` is `False` with `reason: Unschedulable` for genuine scheduler failures, or `reason: SchedulingGated` for gated pods.

## Applicability

- Kubernetes 1.24 or newer on any distribution (vanilla, EKS, GKE, AKS, OpenShift, k3s). Scheduling-gate diagnostics require 1.26+ (the feature is GA from 1.30).
- Requires `kubectl` access with `get`, `list`, `describe` verbs on `pods`, `nodes`, `persistentvolumeclaims`, `persistentvolumes`, `storageclasses`, `resourcequotas`, `limitranges`, and `events` in the target namespace, plus cluster-wide `get` on `nodes` and `storageclasses`.
- `kubectl top nodes` and `kubectl top pods` require the `metrics-server` add-on to be installed and healthy in `kube-system`.
- Inspection of the scheduler itself (`kubectl logs -n kube-system -l component=kube-scheduler`) requires access to the `kube-system` namespace and assumes a self-hosted scheduler; on managed services (EKS, GKE, AKS) the scheduler is hidden and only its events are visible.
- Modifying node labels, taints, or cordoning requires `update` on `nodes` (typically a cluster-admin or platform-operator role).

## Diagnostic Steps

### Step 1: Confirm pod is Pending and capture age

```bash
kubectl get pod <pod-name> -n <namespace> -o wide
```

Expected output: a row showing `STATUS: Pending` (or `SchedulingGated`) with `READY: 0/N`, no `NODE` assigned (`<none>`), and an `AGE` value. A pod Pending for more than 1-2 minutes is not transient — it has a structural scheduling constraint.

### Step 2: Read FailedScheduling event message verbatim

```bash
kubectl describe pod <pod-name> -n <namespace>
```

Expected output: an `Events` table at the bottom of the description containing one or more `Warning  FailedScheduling  default-scheduler  ...` lines. The Message field is the scheduler's verdict and is the single most important diagnostic: it begins `0/N nodes are available:` and lists every failing predicate (e.g. `0/5 nodes are available: 2 Insufficient cpu, 3 node(s) had taints that the pod didn't tolerate`). Record the full message string.

### Step 3: Capture the scheduling condition in structured form

```bash
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{range .status.conditions[*]}{.type}{"  "}{.status}{"  "}{.reason}{"  "}{.message}{"\n"}{end}'
```

Expected output: one line per condition. The `PodScheduled  False  Unschedulable  <message>` line confirms the scheduler rejected the pod and repeats the predicate-failure list machine-readably. `PodScheduled  False  SchedulingGated` indicates `.spec.schedulingGates` is non-empty.

### Step 4: List node allocatable vs requested resources

```bash
kubectl describe nodes | grep -E "^Name:|Allocated resources|cpu  |memory  " -A 1
kubectl top nodes
```

Expected output: per-node `Allocated resources` block showing `Requests` and `Limits` totals as both absolute values and percentages of allocatable. `kubectl top nodes` shows actual live usage. A node with `cpu  Requests: 95%` or `memory  Requests: 100%` cannot fit additional CPU/memory requests; live usage from `kubectl top` is informational only — the scheduler decides on requests, not usage.

### Step 5: List node taints

```bash
kubectl get nodes -o custom-columns=NAME:.metadata.name,TAINTS:.spec.taints,READY:.status.conditions[?\(@.type==\"Ready\"\)].status
```

Expected output: each node's name, its taints array (key/value/effect tuples or `<none>`), and Ready status. Well-known taints to recognize: `node.kubernetes.io/not-ready:NoSchedule`, `node.kubernetes.io/unschedulable:NoSchedule` (cordoned), `node.kubernetes.io/disk-pressure:NoSchedule`, `node.kubernetes.io/memory-pressure:NoSchedule`, `node.kubernetes.io/pid-pressure:NoSchedule`, `node-role.kubernetes.io/control-plane:NoSchedule`.

### Step 6: List node labels and the pod's affinity/nodeSelector requirements

```bash
kubectl get nodes --show-labels
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{"nodeSelector="}{.spec.nodeSelector}{"\nnodeAffinity="}{.spec.affinity.nodeAffinity}{"\ntopologySpreadConstraints="}{.spec.topologySpreadConstraints}{"\n"}'
```

Expected output: per-node label map, followed by the pod's `nodeSelector`, `nodeAffinity` (with `requiredDuringSchedulingIgnoredDuringExecution` and `preferredDuringSchedulingIgnoredDuringExecution` arrays), and `topologySpreadConstraints`. Cross-check: does at least one node satisfy every key/operator/value in `requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms`?

### Step 7: List PVCs referenced by the pod and their bind status

```bash
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{range .spec.volumes[?(@.persistentVolumeClaim)]}{.persistentVolumeClaim.claimName}{"\n"}{end}'
kubectl get pvc -n <namespace>
kubectl describe pvc <pvc-name> -n <namespace>
```

Expected output: list of PVC names referenced by the pod, then the PVC table showing `STATUS` (`Bound`, `Pending`, or `Lost`) and `STORAGECLASS`, then a detailed description with `Events` showing `ProvisioningFailed`, `WaitForFirstConsumer`, `FailedBinding`, or `Successfully provisioned volume`.

### Step 8: Check ResourceQuota and LimitRange in the namespace

```bash
kubectl describe resourcequota -n <namespace>
kubectl describe limitrange -n <namespace>
kubectl get events -n <namespace> --field-selector reason=FailedCreate --sort-by='.lastTimestamp' | tail -20
```

Expected output: per-quota `Used` vs `Hard` columns for `requests.cpu`, `requests.memory`, `limits.cpu`, `limits.memory`, `pods`, `count/*`, etc. Any row where `Used` equals or would exceed `Hard` blocks new pods. The `FailedCreate` events from controllers carry the exact admission error string `pods "<name>" is forbidden: exceeded quota: <quota>, requested: ..., used: ..., limited: ...`.

### Step 9: Verify the kube-scheduler is running and processing the pod

```bash
kubectl get pods -n kube-system -l component=kube-scheduler
kubectl logs -n kube-system -l component=kube-scheduler --tail=100 | grep -i "<pod-name>\|FailedScheduling\|error"
kubectl get events --all-namespaces --field-selector source=default-scheduler --sort-by='.lastTimestamp' | tail -20
```

Expected output: the scheduler pod row should show `Running` with `READY: 1/1` (or for HA `READY: 1/1` on the leader and replicas Ready). The logs should contain a recent line referencing the pending pod. If no scheduler events exist cluster-wide for any recent pod, the scheduler itself is the failure. On managed services (EKS, GKE, AKS) the scheduler pod is not visible; rely on the events query instead.

### Step 10: Check hostPort conflicts and volume-attachment limits

```bash
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{range .spec.containers[*].ports[*]}{.hostPort}{"  "}{.protocol}{"\n"}{end}'
kubectl get pods --all-namespaces -o jsonpath='{range .items[*].spec.containers[*].ports[?(@.hostPort)]}{.hostPort}{"  "}{end}' | tr ' ' '\n' | sort | uniq -c | sort -rn | head -20
```

Expected output: the pod's declared `hostPort` values (if any), then a frequency table of hostPort usage across the cluster. A hostPort claimed on every eligible node will surface as `node(s) didn't have free ports for the requested pod ports` in the Step 2 scheduler message.

## Causes

### Cause A: Cluster has no node with enough allocatable CPU or memory to fit the pod's requests

**Statement:** The pod's aggregate `resources.requests.cpu` or `resources.requests.memory` exceeds the remaining unallocated capacity on every node, so the scheduler's `PodFitsResources` filter eliminates all candidates.

**Mechanism:** The scheduler computes each node's allocatable capacity (node capacity minus the sum of requests of pods already bound to it). For each candidate node it runs the `NodeResourcesFit` predicate, which rejects nodes whose remaining headroom is smaller than the pod's request. When every node fails the same way, the scheduler emits `FailedScheduling` with `0/N nodes are available: N Insufficient cpu` or `0/N nodes are available: N Insufficient memory` and the pod stays Pending until either the request shrinks, a pod is removed, or a new node joins the cluster. The decision uses requests (not live usage), so a cluster can look idle in `kubectl top` and still refuse to schedule.

**Indicator:**

- [Step 2] scheduler event message contains `Insufficient cpu` or `Insufficient memory`
<!-- match: {"step": 2, "predicate": "contains", "target": "Insufficient cpu"} -->
- [Step 2] scheduler event message contains `Insufficient memory`
<!-- match: {"step": 2, "predicate": "contains", "target": "Insufficient memory"} -->
- [Step 4] every node's `Allocated resources` block shows `cpu  Requests` or `memory  Requests` at or near 100% of allocatable

**Mitigation:**

- **Risk:** Lowering `resources.requests` allows scheduling but risks CPU throttling or OOM kills if actual usage exceeds the reduced request; only safe when observed peak usage is well below the new request.
- **Command:**

  ```bash
  kubectl set resources deployment/<deployment-name> -n <namespace> \
    --requests=cpu=<lower-cpu>,memory=<lower-mem>
  ```

- **Duration:** Permanent if sized from observed p99 usage with 25-30% headroom; revisit if traffic patterns change.

**Resolution:**

```bash
# Option 1: add a node (manual or via Cluster Autoscaler scale-up trigger)
kubectl get pods -n kube-system -l app.kubernetes.io/name=cluster-autoscaler
kubectl logs -n kube-system -l app.kubernetes.io/name=cluster-autoscaler --tail=50 | grep -i "scale up\|unschedulable"
# Option 2: right-size requests from observed usage (Prometheus / kubectl top history)
kubectl set resources deployment/<deployment-name> -n <namespace> \
  --requests=cpu=<right-sized-cpu>,memory=<right-sized-mem>
kubectl rollout status deployment/<deployment-name> -n <namespace>
```

**Impact:** Cluster-wide capacity change (new node) or per-deployment sizing change; the scheduler reschedules pods with the new requests, which can disturb bin-packing on existing nodes.
**Rollback:** `kubectl set resources deployment/<deployment-name> -n <namespace> --requests=cpu=<previous-cpu>,memory=<previous-mem>` restores the prior sizing; for autoscaler-driven node adds, the autoscaler scales down idle nodes automatically.

**Verification:** After the change, `kubectl get pod <pod-name> -n <namespace>` transitions to `Running` within 30 seconds and `kubectl describe node <assigned-node>` shows the pod under `Non-terminated Pods` with the new requests counted toward allocated.

### Cause B: No node carries labels that satisfy the pod's nodeSelector or required node affinity

**Statement:** The pod's `spec.nodeSelector` or `nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution` specifies label keys/values that no node in the cluster has, so the scheduler filters every node out before scoring.

**Mechanism:** Required node affinity is a hard predicate: a node is feasible only if it carries every key/value matching the pod's `matchExpressions` (within a term, all expressions ANDed; across terms, ORed). When zero nodes pass, the scheduler emits `0/N nodes are available: N node(s) didn't match Pod's node affinity/selector` or `N node(s) didn't match Pod's nodeSelector` and the pod stays Pending. Common shapes: a workload pinned to `nodepool=gpu` on a cluster with no GPU node pool, a pod targeting `topology.kubernetes.io/zone=us-east-1a` after that zone's nodes were drained, or a typo in the label key (`kubernetes.io/arch` vs `beta.kubernetes.io/arch`).

**Indicator:**

- [Step 2] scheduler event message contains `node(s) didn't match Pod's node affinity/selector`
<!-- match: {"step": 2, "predicate": "contains", "target": "didn't match Pod's node affinity"} -->
- [Step 2] scheduler event message contains `node(s) didn't match Pod's nodeSelector`
<!-- match: {"step": 2, "predicate": "contains", "target": "didn't match Pod's nodeSelector"} -->
- [Step 6] no node in `kubectl get nodes --show-labels` satisfies every key/value pair listed in the pod's `nodeSelector` or `requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms`

**Mitigation:**

- **Risk:** Adding a label to a node that does not actually have the property (e.g. labeling a non-GPU node `nodepool=gpu`) will let scheduling succeed but the workload will fail at runtime; only label nodes that genuinely match the property.
- **Command:**

  ```bash
  kubectl label node <node-name> <key>=<value>
  ```

- **Duration:** Permanent until the node-pool composition changes; safe if the label reflects reality.

**Resolution:**

```bash
# Option 1: provision a matching node pool (cluster-autoscaler / managed-service console) and let it auto-label
# Option 2: relax the affinity from required to preferred so scheduling falls back to any node
kubectl get deployment <deployment-name> -n <namespace> -o yaml > /tmp/<name>.yaml
# Edit nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution -> preferredDuringSchedulingIgnoredDuringExecution with weight: 100
kubectl apply -f /tmp/<name>.yaml
kubectl rollout status deployment/<deployment-name> -n <namespace>
```

**Impact:** Workload-wide; switching required to preferred allows fallback scheduling, but the pod may land on a node that does not match the original intent (e.g. a CPU-only node when GPU was preferred). Provisioning a new node pool is cluster-wide.
**Rollback:** Restore the original deployment manifest with `kubectl apply -f <previous-manifest>.yaml` or `kubectl rollout undo deployment/<deployment-name> -n <namespace>`.

**Verification:** `kubectl get pod <pod-name> -n <namespace> -o wide` shows an assigned `NODE` and `kubectl describe node <assigned-node>` confirms the node carries the required labels.

### Cause C: Every candidate node has a NoSchedule taint the pod does not tolerate

**Statement:** All nodes that would otherwise be feasible carry a `NoSchedule` (or `NoExecute`) taint, and the pod's `tolerations` array does not match that taint key/value/effect.

**Mechanism:** The `TaintToleration` predicate filters out any node whose un-tolerated taints contain at least one `NoSchedule` or `NoExecute` entry. The scheduler emits `0/N nodes are available: N node(s) had taints that the pod didn't tolerate` (or `N node(s) had untolerated taint {<key>: <value>}`). Common shapes: dedicated node pools without matching toleration on the workload, control-plane-only nodes (`node-role.kubernetes.io/control-plane:NoSchedule`) when no worker pool exists, nodes auto-tainted by the lifecycle controller during resource pressure (`memory-pressure`, `disk-pressure`, `pid-pressure`), or a cordoned node carrying `node.kubernetes.io/unschedulable:NoSchedule`.

**Indicator:**

- [Step 2] scheduler event message contains `node(s) had taints that the pod didn't tolerate`
<!-- match: {"step": 2, "predicate": "contains", "target": "had taints that the pod didn't tolerate"} -->
- [Step 2] scheduler event message contains `untolerated taint`
<!-- match: {"step": 2, "predicate": "contains", "target": "untolerated taint"} -->
- [Step 5] every node listed shows at least one `NoSchedule` or `NoExecute` taint whose key is absent from the pod's `tolerations`

**Mitigation:**

- **Risk:** Removing a taint from a node opens it to every pod that previously avoided it; only safe if the taint was applied as a stopgap, not as part of a dedicated-pool design.
- **Command:**

  ```bash
  kubectl taint nodes <node-name> <key>=<value>:NoSchedule-
  ```

- **Duration:** Minutes-to-hours while triaging the workload's intended placement.

**Resolution:**

```bash
# Add the matching toleration to the deployment so the pod can land on the tainted nodes
kubectl patch deployment <deployment-name> -n <namespace> --type=json \
  -p='[{"op":"add","path":"/spec/template/spec/tolerations","value":[{"key":"<taint-key>","operator":"Equal","value":"<taint-value>","effect":"NoSchedule"}]}]'
kubectl rollout status deployment/<deployment-name> -n <namespace>
```

**Impact:** Workload-wide; rolling update cycles all replicas with the new toleration. The pod becomes schedulable on previously-blocked nodes but pressure-related taints (`memory-pressure`, `disk-pressure`) should rarely be tolerated — they indicate the node is unhealthy.
**Rollback:** `kubectl rollout undo deployment/<deployment-name> -n <namespace>` restores the prior tolerations array.

**Verification:** `kubectl get pod <pod-name> -n <namespace> -o wide` shows `NODE` populated within 30 seconds and `kubectl describe node <assigned-node>` confirms the node still carries the taint while the pod tolerates it.

### Cause D: PersistentVolumeClaim referenced by the pod is unbound under Immediate binding mode

**Statement:** The pod mounts a PVC that is stuck `Pending` (no matching PV, provisioner failure, or no StorageClass) and the PVC's `volumeBindingMode` is `Immediate`, so the scheduler refuses to place the pod.

**Mechanism:** When `volumeBindingMode: Immediate` (the default), the PVC binding controller attempts to bind the PVC as soon as it is created — independently of any pod. If binding fails (no matching PV, no StorageClass, provisioner returns `ProvisioningFailed`, requested capacity exceeds StorageClass quota), the PVC stays `Pending`. The scheduler's `VolumeBinding` predicate then fails any pod that references that PVC with `pod has unbound immediate PersistentVolumeClaims`. The dual case is a zone-topology mismatch: an EBS/Persistent-Disk PV exists in `us-east-1a` but the only nodes with capacity are in `us-east-1b`, and Immediate binding pre-committed the volume before the scheduler could pick a compatible node.

**Indicator:**

- [Step 2] scheduler event message contains `pod has unbound immediate PersistentVolumeClaims`
<!-- match: {"step": 2, "predicate": "contains", "target": "unbound immediate PersistentVolumeClaims"} -->
- [Step 7] one or more PVCs referenced by the pod show `STATUS: Pending` in `kubectl get pvc`
- [Step 7] PVC events contain `ProvisioningFailed`, `no persistent volumes available for this claim and no storage class is set`, or `waiting for a volume to be created`

**Mitigation:**

- **Risk:** Manually creating a static PV that satisfies the PVC bypasses dynamic provisioning checks; the PV must match `storageClassName`, `accessModes`, and capacity exactly or it will not bind.
- **Command:**

  ```bash
  kubectl describe pvc <pvc-name> -n <namespace>
  kubectl get storageclass
  # Verify provisioner pod health (e.g. ebs-csi-controller, cinder-csi)
  kubectl get pods -A | grep -E 'csi|provisioner'
  ```

- **Duration:** Diagnostic; fix the provisioner or StorageClass before relying on dynamic provisioning again.

**Resolution:**

```bash
# Option 1: switch the StorageClass to WaitForFirstConsumer so binding defers until pod scheduling picks a node
kubectl get storageclass <sc-name> -o yaml > /tmp/<sc>.yaml
# Edit volumeBindingMode: WaitForFirstConsumer, then recreate (StorageClass volumeBindingMode is immutable)
kubectl delete storageclass <sc-name> && kubectl apply -f /tmp/<sc>.yaml
# Option 2: fix the underlying provisioner (CSI driver pod not Ready, IAM permission missing, cloud-provider quota hit)
kubectl logs -n kube-system <csi-controller-pod> -c <csi-provisioner> --tail=100
```

**Impact:** StorageClass change is cluster-wide and affects all future PVCs using that class; existing bound PVs are unaffected. Provisioner fixes are infra-team scope (IAM, cloud quotas).
**Rollback:** Recreate the original StorageClass YAML with the previous `volumeBindingMode`; existing PVs continue to function regardless.

**Verification:** `kubectl get pvc -n <namespace>` shows the PVC transitions to `Bound` and `kubectl get pod <pod-name> -n <namespace>` transitions out of Pending within 60 seconds.

### Cause E: Namespace ResourceQuota blocks the controller from creating the pod

**Statement:** The namespace's ResourceQuota is exhausted, so the API server's quota admission plugin returns 403 on pod creation and the controlling ReplicaSet or StatefulSet cannot materialize the pod at all.

**Mechanism:** ResourceQuota admission runs synchronously on every pod-create request, summing existing pods' requests and rejecting any new pod that would push the total over `hard`. The rejection is at admission, not scheduling, so the pod never appears in `kubectl get pods` — the controlling ReplicaSet records a `FailedCreate` event carrying the admission error `pods "<name>" is forbidden: exceeded quota: <quota>, requested: ..., used: ..., limited: ...`. When the quota covers `cpu`/`memory` but the pod omits explicit requests, admission also rejects with `must specify cpu,memory` unless a LimitRange supplies defaults.

**Indicator:**

- [Step 8] `kubectl get events --field-selector reason=FailedCreate` shows messages containing `exceeded quota`
<!-- match: {"step": 8, "predicate": "contains", "target": "exceeded quota"} -->
- [Step 8] `kubectl get events --field-selector reason=FailedCreate` shows messages containing `is forbidden`
<!-- match: {"step": 8, "predicate": "contains", "target": "is forbidden"} -->
- [Step 8] `kubectl describe resourcequota` shows at least one row where `Used` equals `Hard`, and `kubectl get pods -n <namespace>` shows fewer replicas than the Deployment's `spec.replicas`

**Mitigation:**

- **Risk:** Deleting completed/failed pods to free up `count/pods` quota is safe; deleting active pods to free `requests.cpu`/`requests.memory` interrupts those services.
- **Command:**

  ```bash
  kubectl delete pods -n <namespace> --field-selector=status.phase=Succeeded
  kubectl delete pods -n <namespace> --field-selector=status.phase=Failed
  ```

- **Duration:** Immediate; revisit if the namespace fills again.

**Resolution:**

```bash
# Option 1: raise the quota if justified (capacity review with the namespace owner)
kubectl edit resourcequota <quota-name> -n <namespace>
# Option 2: reduce per-pod requests so the workload fits within the existing quota
kubectl set resources deployment/<deployment-name> -n <namespace> \
  --requests=cpu=<lower>,memory=<lower>
# Option 3: add a LimitRange providing defaults so pods missing explicit requests are not rejected
kubectl apply -f - <<EOF
apiVersion: v1
kind: LimitRange
metadata:
  name: defaults
  namespace: <namespace>
spec:
  limits:
  - type: Container
    defaultRequest:
      cpu: 100m
      memory: 128Mi
    default:
      cpu: 500m
      memory: 512Mi
EOF
```

**Impact:** Namespace-scoped. Raising quota consumes shared cluster capacity; adding a LimitRange retroactively forces defaults on new pods only (existing pods unchanged).
**Rollback:** `kubectl edit resourcequota <quota-name> -n <namespace>` to restore previous `hard` values; `kubectl delete limitrange defaults -n <namespace>` to remove defaults.

**Verification:** `kubectl describe resourcequota -n <namespace>` shows `Used` below `Hard` for the previously-saturated row, and `kubectl get deployment <deployment-name> -n <namespace>` reaches its desired replica count.

### Cause F: Pod requests a hostPort already taken on every eligible node

**Statement:** The pod declares a `hostPort` and every node in the cluster already has another pod bound to the same `(hostPort, protocol, hostIP)` tuple, so the `NodePorts` predicate eliminates all candidates.

**Mechanism:** `hostPort` reserves a port on the host network namespace; only one pod per node may claim a given port/protocol/hostIP combination. The scheduler's `NodePorts` predicate filters out any node where the tuple is already in use. When a DaemonSet uses a hostPort (one replica per node by design), any additional pod requesting the same hostPort has zero feasible nodes and surfaces as `0/N nodes are available: N node(s) didn't have free ports for the requested pod ports`. Common shape: scaling a Deployment that uses hostPort beyond one replica per node, or two unrelated workloads contending on a metrics-exporter port (e.g. node-exporter and a custom Prometheus exporter both wanting `:9100`).

**Indicator:**

- [Step 2] scheduler event message contains `node(s) didn't have free ports for the requested pod ports`
<!-- match: {"step": 2, "predicate": "contains", "target": "didn't have free ports for the requested pod ports"} -->
- [Step 10] the pod declares one or more `hostPort` values in `.spec.containers[*].ports[*].hostPort`
- [Step 10] the same hostPort is already claimed on every node (frequency table shows the count equals or exceeds total node count)

**Mitigation:**

- **Risk:** Removing the hostPort changes the workload's external connectivity model; if downstream clients depend on reaching the pod via the node's IP+port, they will break until a Service replaces the hostPort.
- **Command:**

  ```bash
  kubectl patch deployment <deployment-name> -n <namespace> --type=json \
    -p='[{"op":"remove","path":"/spec/template/spec/containers/0/ports/0/hostPort"}]'
  ```

- **Duration:** Permanent if a Service replaces the role of hostPort.

**Resolution:**

```bash
# Replace hostPort with a Service (NodePort or ClusterIP) so the pod is reachable without consuming a host port
kubectl expose deployment/<deployment-name> -n <namespace> --port=<pod-port> --target-port=<pod-port> --type=NodePort
# OR if a hostPort is genuinely required (e.g. ingress controller), constrain replicas to one per node via a DaemonSet
```

**Impact:** Workload-wide. Switching from hostPort to a Service alters how clients reach the pod; coordinate with downstream owners.
**Rollback:** Restore the original deployment manifest with `kubectl apply -f <previous-manifest>.yaml` and `kubectl delete service <deployment-name> -n <namespace>`.

**Verification:** `kubectl get pod -l <selector> -n <namespace> -o wide` shows all replicas scheduled and `kubectl get service <deployment-name> -n <namespace>` reports an assigned `CLUSTER-IP` (and `NodePort` if applicable).

### Cause G: Pod topology spread constraint with DoNotSchedule cannot be satisfied

**Statement:** The pod's `topologySpreadConstraints` with `whenUnsatisfiable: DoNotSchedule` requires a maximum skew of pods across topology domains (zones, nodes) that no remaining node can satisfy without exceeding the skew.

**Mechanism:** Topology spread enforces balanced distribution: with `maxSkew: 1` across `topology.kubernetes.io/zone`, no zone may contain more than 1 + min(matching pods elsewhere) replicas. The `PodTopologySpread` predicate rejects any node whose addition would breach the skew, so when a zone is depleted the workload accumulates as `node(s) didn't match pod topology spread constraints`. The dual case is required pod-anti-affinity with `topologyKey: kubernetes.io/hostname`, which enforces one-replica-per-node and surfaces as `node(s) didn't satisfy existing pods anti-affinity rules` once every node hosts a replica.

**Indicator:**

- [Step 2] scheduler event message contains `node(s) didn't match pod topology spread constraints`
<!-- match: {"step": 2, "predicate": "contains", "target": "didn't match pod topology spread constraints"} -->
- [Step 2] scheduler event message contains `node(s) didn't satisfy existing pods anti-affinity rules`
<!-- match: {"step": 2, "predicate": "contains", "target": "didn't satisfy existing pods anti-affinity rules"} -->
- [Step 6] the pod spec contains `topologySpreadConstraints` with `whenUnsatisfiable: DoNotSchedule` or `affinity.podAntiAffinity.requiredDuringSchedulingIgnoredDuringExecution`

**Mitigation:**

- **Risk:** Relaxing `whenUnsatisfiable` from `DoNotSchedule` to `ScheduleAnyway` allows scheduling but lets the workload concentrate in one zone — a single-zone outage will then take all replicas down.
- **Command:**

  ```bash
  kubectl patch deployment <deployment-name> -n <namespace> --type=json \
    -p='[{"op":"replace","path":"/spec/template/spec/topologySpreadConstraints/0/whenUnsatisfiable","value":"ScheduleAnyway"}]'
  ```

- **Duration:** Hours-to-days; revert once the depleted zone has capacity again.

**Resolution:**

```bash
# Option 1: add capacity in the depleted topology domain (new node in the zone, or restore drained nodes)
kubectl get nodes -L topology.kubernetes.io/zone
kubectl uncordon <drained-node>
# Option 2: increase maxSkew if the workload can tolerate looser balance
kubectl patch deployment <deployment-name> -n <namespace> --type=json \
  -p='[{"op":"replace","path":"/spec/template/spec/topologySpreadConstraints/0/maxSkew","value":3}]'
```

**Impact:** Workload-wide. Higher `maxSkew` or `ScheduleAnyway` reduces resilience to single-zone failures; restoring capacity in the depleted zone is the durable fix.
**Rollback:** `kubectl rollout undo deployment/<deployment-name> -n <namespace>` restores the prior topology constraints.

**Verification:** `kubectl get pod -l <selector> -n <namespace> -o wide` shows pods distributed across zones (use `-o custom-columns=NAME:.metadata.name,ZONE:.spec.nodeName` plus the node-to-zone map), and `RESTARTS=0` is not relevant for Pending — confirm all replicas have `STATUS: Running`.

### Cause H: Pod has scheduling gates that have not been cleared

**Statement:** The pod's `.spec.schedulingGates` array is non-empty, so the scheduler does not even queue the pod for evaluation and the pod stays in `STATUS: SchedulingGated` indefinitely until a controller (or an operator) removes the gates.

**Mechanism:** Scheduling gates (GA in Kubernetes 1.30) let admission controllers, custom controllers, or quota-budget systems hold a pod out of the scheduling queue by setting one or more named gates at creation time. The scheduler skips any pod with non-empty `schedulingGates` and emits no `FailedScheduling` event — there is no scheduling decision to report. The `PodScheduled` condition reads `status: False, reason: SchedulingGated`. The pod becomes schedulable only after every gate is removed via a PATCH to `.spec.schedulingGates`. Gates can be removed but never added after creation, so a hung gate-controller blocks the pod permanently.

**Indicator:**

- [Step 1] pod `STATUS` is the literal string `SchedulingGated`
<!-- match: {"step": 1, "predicate": "contains", "target": "SchedulingGated"} -->
- [Step 3] `PodScheduled  False  SchedulingGated` appears in the conditions output
<!-- match: {"step": 3, "predicate": "contains", "target": "SchedulingGated"} -->
- [Step 2] the Events table contains NO `FailedScheduling` event (the scheduler never considered the pod)

**Mitigation:**

- **Risk:** Manually clearing a scheduling gate bypasses the controller that owns it; if the controller was holding the pod for a quota or admission check, the pod may run despite a policy violation. Confirm with the gate-owner before clearing.
- **Command:**

  ```bash
  kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.spec.schedulingGates}'
  kubectl patch pod <pod-name> -n <namespace> --type=json \
    -p='[{"op":"replace","path":"/spec/schedulingGates","value":[]}]'
  ```

- **Duration:** Permanent for the affected pod; new pods will be created with the gate again until the controller is fixed.

**Resolution:**

```bash
# Identify the controller that owns the gate (gate name often namespaced, e.g. example.com/quota-check)
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.spec.schedulingGates[*].name}'
# Inspect the controller's logs to find out why it has not cleared the gate
kubectl get pods --all-namespaces -l <controller-selector>
kubectl logs -n <controller-ns> <controller-pod> --tail=200
# Fix the controller, or remove the gate from the pod template if the controller is no longer needed
kubectl edit deployment <deployment-name> -n <namespace>   # remove spec.template.spec.schedulingGates
```

**Verification:** `kubectl get pod <pod-name> -n <namespace>` transitions from `SchedulingGated` to `Pending` and then `Running`, and `kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.spec.schedulingGates}'` returns an empty array or null.

### Cause I: kube-scheduler is down or partitioned from the API server

**Statement:** No active kube-scheduler instance is processing pods cluster-wide, so every newly-created pod accumulates in Pending without any `FailedScheduling` event because no scheduler is generating events.

**Mechanism:** Kubernetes runs the scheduler as one or more leader-elected pods in `kube-system`. If the leader pod crashes, loses its lease, or is partitioned from the API server, no scheduling decisions occur until a follower wins re-election. During the gap, every new pod sits in Pending with `PodScheduled  False  Unschedulable  no nodes available to schedule pods` or — more commonly — with no `Events` entries at all because the source of those events is offline. The symptom is cluster-wide: not just one pod but every recently-created pod is Pending. On managed services (EKS, GKE, AKS) the scheduler is operated by the vendor; this cause manifests as a control-plane health-check failure in the provider's console.

**Indicator:**

- [Step 9] `kubectl get pods -n kube-system -l component=kube-scheduler` shows no Running pod, or all replicas in `CrashLoopBackOff`, `Pending`, or `Error`
- [Step 9] `kubectl get events --field-selector source=default-scheduler` returns no events in the last several minutes despite multiple recently-created pods
- [Symptom] every recently-created pod across the cluster is Pending, not just the one being investigated

**Mitigation:**

- **Risk:** Restarting the scheduler pod is safe in HA setups (a follower takes over); in single-replica clusters there is a 10-30 second scheduling outage during restart.
- **Command:**

  ```bash
  kubectl delete pod -n kube-system -l component=kube-scheduler
  ```

- **Duration:** Single restart cycle (seconds).

**Resolution:**

```bash
# Self-hosted clusters: inspect scheduler logs and config
kubectl logs -n kube-system -l component=kube-scheduler --tail=200 --previous
kubectl get configmap -n kube-system kube-scheduler-config -o yaml
# Managed services (EKS/GKE/AKS): the scheduler is vendor-operated — open a support ticket with the cluster ID, region, and approximate time of the outage.
```

**Impact:** Cluster-wide. Until the scheduler is healthy no new pods anywhere will be placed, including system-critical workloads (CoreDNS, ingress controllers if they roll).
**Rollback:** Restore the previous scheduler ConfigMap with `kubectl apply -f <previous-scheduler-config>.yaml` if a configuration change preceded the outage.

**Verification:** `kubectl get pods -n kube-system -l component=kube-scheduler` shows `Running 1/1` (or the expected HA replica count) and a new test pod (`kubectl run test --image=busybox --restart=Never -- sleep 60`) reaches `Running` within 30 seconds.

### Cause Z: Unidentified

**Statement:** The pod is stuck in Pending (or `SchedulingGated`) but no indicator from Causes A through I matches the gathered evidence.

**Mechanism:** The scheduler is failing to place the pod or the pod has not entered the scheduling queue, but the collected event message, node ledger, taints, labels, PVC state, quota, hostPort, topology, gates, and scheduler health do not point to any standard failure mode. Less common causes include a custom `schedulerName` whose scheduler is the failing component, an admission webhook silently dropping bind events, preemption blocked by PriorityClass victim selection, Cluster Autoscaler with no node-pool template matching the pod's profile, or a custom PreFilter plugin rejecting the pod without surfacing a reason.

**Indicator:**

- [Default] Pod confirmed `Pending` or `SchedulingGated` (Step 1, Step 3) but Causes A-I indicators do not match the gathered evidence

**Mitigation:**

- **Risk:** Recreating the pod from a simplified manifest (strip nodeSelector, tolerations, affinity, topology constraints, hostPort) tests whether the scheduling rejection is from the pod's constraints or from infrastructure; the simplified pod may bypass intended placement rules and should be deleted after triage.
- **Command:**

  ```bash
  kubectl run probe --image=registry.k8s.io/pause:3.9 --restart=Never -n <namespace>
  kubectl get pod probe -n <namespace> -o wide
  kubectl delete pod probe -n <namespace>
  ```

- **Duration:** Single probe cycle (under 1 minute).

**Resolution:** Out of runbook scope. Capture the artefacts from Steps 1-10 (pod description, scheduler event verbatim, structured conditions, node resource ledger, node taints/labels, pod affinity/topology spec, PVC inventory, quota state, scheduler health, hostPort frequency table) plus the pod's `.spec.schedulerName` and any custom scheduler logs, and escalate to the platform on-call or the cluster operator with the failure-mode summary.

**Verification:** Hand-off acknowledged by the receiving engineer; an incident ticket is opened with the captured artefacts attached and a follow-up owner assigned.

## Prevention

- Set both `requests` and `limits` for CPU and memory on every container, and enforce defaults cluster-wide via a `LimitRange` per namespace so workloads cannot be deployed without sizing.
- Run Cluster Autoscaler (or its managed equivalent: Karpenter on EKS, GKE node auto-provisioning, AKS cluster autoscaler) with node-pool templates matching every workload profile in the cluster (GPU, large-memory, zone-pinned).
- Alert on `kube_pod_status_phase{phase="Pending"}` lasting more than 10 minutes with a Prometheus rule so Pending pods surface before users notice. A complementary alert on `scheduler_pending_pods{queue="gated"} > 0` catches stuck scheduling gates.
- Use `WaitForFirstConsumer` as the StorageClass `volumeBindingMode` for any topology-aware storage (EBS, Persistent Disk, Azure Disk) so PVC binding follows pod scheduling instead of pre-committing to a zone.
- Audit ResourceQuota headroom monthly: page when `Used / Hard` exceeds 80% on any quota row so capacity can be adjusted before pods fail to create.
- Avoid `hostPort` for workloads with more than one replica per node — use a `Service` (ClusterIP or NodePort) instead, or pin the workload to a `DaemonSet` if one-per-node is genuinely required.
- Treat node taints as policy artefacts: tag every custom taint with an owner and a sunset date, and remove cordons / maintenance taints promptly once the operation completes.
- Pin scheduler configuration in version control: a misconfigured scheduler profile (custom PreFilter plugin, wrong leader-election lease) is hard to spot without a known-good baseline to diff against.
- Validate workload constraints in CI: kustomize/helm rendering should fail the pipeline when a manifest references a `nodeSelector` key with no matching node in the target cluster's known label set.

## Sources

- [Kubernetes - Debug Pods](https://kubernetes.io/docs/tasks/debug/debug-application/debug-pods/) - Priority 1. Pending-pod diagnostic flow, the role of `kubectl describe pod`, common scheduling-failure causes (insufficient resources, hostPort).
- [Kubernetes - kube-scheduler](https://kubernetes.io/docs/concepts/scheduling-eviction/kube-scheduler/) - Priority 1. Scheduling lifecycle (filtering then scoring), `FailedScheduling` event semantics, predicate and scoring plugin model.
- [Kubernetes - Taints and Tolerations](https://kubernetes.io/docs/concepts/scheduling-eviction/taint-and-toleration/) - Priority 1. Well-known taints (`node-role.kubernetes.io/control-plane`, `node.kubernetes.io/disk-pressure`, `memory-pressure`, `pid-pressure`, `unschedulable`, `not-ready`), `NoSchedule` vs `NoExecute` vs `PreferNoSchedule`, toleration operator semantics.
- [Kubernetes - Assigning Pods to Nodes](https://kubernetes.io/docs/concepts/scheduling-eviction/assign-pod-node/) - Priority 1. nodeSelector vs node affinity, `requiredDuringSchedulingIgnoredDuringExecution` vs `preferredDuringSchedulingIgnoredDuringExecution`, pod topology spread constraints (`maxSkew`, `whenUnsatisfiable`).
- [Kubernetes - Persistent Volumes](https://kubernetes.io/docs/concepts/storage/persistent-volumes/) - Priority 1. PVC binding lifecycle, `volumeBindingMode: Immediate` vs `WaitForFirstConsumer`, `ProvisioningFailed`, the `pod has unbound immediate PersistentVolumeClaims` scheduler message.
- [Kubernetes - Resource Quotas](https://kubernetes.io/docs/concepts/policy/resource-quotas/) - Priority 1. Quota admission semantics, the `exceeded quota` / `is forbidden` rejection string, LimitRange interaction for default requests, why Deployment creation succeeds while pod creation fails.
- [Kubernetes - Pod Scheduling Readiness](https://kubernetes.io/docs/concepts/scheduling-eviction/pod-scheduling-readiness/) - Priority 1. Scheduling gates, the `SchedulingGated` pod status, `scheduler_pending_pods{queue="gated"}` metric, gate creation/removal rules.
