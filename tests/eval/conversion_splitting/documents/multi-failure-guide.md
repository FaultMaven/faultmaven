# Kubernetes Pod Troubleshooting Reference

This reference covers the distinct ways a pod fails to reach Running state.
Each section is an independent failure with its own symptom.

## Pods killed for exceeding memory

The pod terminates with exit code 137 and the pod status shows `OOMKilled`.
`kubectl describe pod` reports `Reason: OOMKilled`. The container exceeded its
`resources.limits.memory`. Raise the limit, or fix the leak in the application.

```bash
kubectl describe pod <name> | grep -A5 'Last State'
```

## Image cannot be pulled

The pod sits in `ImagePullBackOff`. Events show
`Failed to pull image ... unauthorized` or `manifest unknown`. The tag does not
exist, or the registry credentials are missing. Verify the tag and create an
`imagePullSecret`.

```bash
kubectl get events --field-selector involvedObject.name=<pod>
```

## Pod cannot be scheduled

The pod stays `Pending` and events show
`0/5 nodes are available: insufficient cpu`. No node satisfies the resource
request, or a taint excludes every candidate. Lower the request or add capacity.

```bash
kubectl describe pod <name> | grep -A10 Events
```

## Container starts then exits repeatedly

The pod reports `CrashLoopBackOff` with an increasing restart count. The process
exits non-zero on startup — usually a missing env var or an unreachable
dependency. Read the previous container's logs.

```bash
kubectl logs <pod> --previous
```
