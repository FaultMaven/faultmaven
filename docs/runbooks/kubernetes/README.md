# Kubernetes Runbooks

Troubleshooting guides for common Kubernetes issues.

## Available Runbooks

### Pod Issues
- [CrashLoopBackOff](k8s-pod-crashloopbackoff.md) - Pod continuously restarting
- [OOMKilled](k8s-pod-oomkilled.md) - Pod killed due to memory exhaustion
- [ImagePullBackOff](k8s-pod-imagepullbackoff.md) - Cannot pull container image

### Infrastructure Issues
- [Node Not Ready](k8s-node-not-ready.md) - Node in NotReady state

## Common Diagnostic Commands

```bash
# Check pod status
kubectl get pods -A

# Describe pod for events
kubectl describe pod <pod-name> -n <namespace>

# View pod logs
kubectl logs <pod-name> -n <namespace>

# View previous container logs (if crashed)
kubectl logs <pod-name> -n <namespace> --previous

# Check resource usage
kubectl top pods -n <namespace>
kubectl top nodes

# Check node status
kubectl get nodes
kubectl describe node <node-name>
```

## Prerequisites

Most Kubernetes runbooks assume you have:
- `kubectl` installed and configured
- Appropriate RBAC permissions
- Access to cluster monitoring (optional but helpful)

## Related Resources

- [Kubernetes Official Documentation](https://kubernetes.io/docs/)
- [Debugging Pods](https://kubernetes.io/docs/tasks/debug/debug-application/debug-pods/)
- [Troubleshooting Applications](https://kubernetes.io/docs/tasks/debug/debug-application/)
