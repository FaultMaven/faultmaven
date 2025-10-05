---
id: k8s-pod-imagepullbackoff
title: "Kubernetes - Pod ImagePullBackOff"
technology: kubernetes
severity: high
tags:
  - kubernetes
  - pod
  - image
  - registry
  - authentication
difficulty: beginner
version: "1.0.0"
last_updated: "2025-01-15"
verified_by: "FaultMaven Team"
status: verified
---

# Kubernetes - Pod ImagePullBackOff

> **Purpose**: Resolve issues where Kubernetes cannot pull container images from registries

## Quick Reference Card

**🔍 Symptoms:**
- Pod status shows `ImagePullBackOff` or `ErrImagePull`
- Pod remains in `Pending` state
- Events show image pull failures
- New deployments fail to start

**⚡ Common Causes:**
1. **Authentication failure** (40% of cases) - Missing or incorrect registry credentials
2. **Image does not exist** (30% of cases) - Wrong image name, tag, or registry URL
3. **Network connectivity** (20% of cases) - Cannot reach registry from cluster
4. **Rate limiting** (10% of cases) - Too many pulls from public registry (Docker Hub)

**🚀 Quick Fix:**
```bash
# Check exact error message
kubectl describe pod <pod-name> -n <namespace> | grep -A 5 "Failed to pull image"

# Verify image exists and is accessible
docker pull <image-name>:<tag>
```

**⏱️ Estimated Resolution Time:** 10-20 minutes

---

## Diagnostic Steps

### Step 1: Verify ImagePullBackOff Status

**What to check:** Confirm the pod cannot pull the image

**How to check:**
```bash
# Check pod status
kubectl get pods -n <namespace>

# Get detailed events
kubectl describe pod <pod-name> -n <namespace>

# Check image pull status
kubectl get pods -n <namespace> -o jsonpath='{.items[*].status.containerStatuses[*].state}'
```

**Expected output:**
```
NAME                    READY   STATUS             RESTARTS   AGE
myapp-7d9f8c6b5-4xkz2  0/1     ImagePullBackOff   0          5m

Events:
  Warning  Failed     3m    kubelet  Failed to pull image "myregistry.com/myapp:v1.2.3": rpc error: code = Unknown desc = Error response from daemon: pull access denied for myregistry.com/myapp, repository does not exist or may require 'docker login'
  Warning  Failed     3m    kubelet  Error: ErrImagePull
  Normal   BackOff    2m    kubelet  Back-off pulling image "myregistry.com/myapp:v1.2.3"
  Warning  Failed     2m    kubelet  Error: ImagePullBackOff
```

---

### Step 2: Verify Image Name and Tag

**What to check:** Ensure image name, tag, and registry URL are correct

**How to check:**
```bash
# Get image specification from deployment
kubectl get deployment <deployment-name> -n <namespace> -o jsonpath='{.spec.template.spec.containers[*].image}'

# Check all image pull secrets referenced
kubectl get deployment <deployment-name> -n <namespace> -o jsonpath='{.spec.template.spec.imagePullSecrets}'
```

**Common image naming errors:**
```bash
# Wrong registry
myregistry.com/myapp:latest  # Should be: registry.example.com/myapp:latest

# Wrong tag
myapp:lastest  # Typo! Should be: myapp:latest

# Missing tag (defaults to :latest which might not exist)
myapp  # Should be: myapp:v1.2.3

# Wrong image name
my-app:v1  # Should be: myapp:v1
```

**Verify image exists:**
```bash
# Try pulling manually (from machine with Docker)
docker pull <image-name>:<tag>

# Check registry for available tags (if public)
curl https://registry.hub.docker.com/v2/repositories/<org>/<image>/tags
```

---

### Step 3: Check Registry Authentication

**What to check:** Verify credentials for accessing private registry

**How to check:**
```bash
# List secrets in namespace
kubectl get secrets -n <namespace>

# Check if imagePullSecret exists
kubectl get secret <secret-name> -n <namespace>

# View secret details (not the actual credentials)
kubectl describe secret <secret-name> -n <namespace>

# Check what imagePullSecrets are configured in deployment
kubectl get deployment <deployment-name> -n <namespace> -o yaml | grep -A 3 imagePullSecrets
```

**Verify secret content:**
```bash
# Decode secret to verify it's correct format
kubectl get secret <secret-name> -n <namespace> -o jsonpath='{.data.\.dockerconfigjson}' | base64 -d | jq
```

**Expected output:**
```json
{
  "auths": {
    "myregistry.com": {
      "username": "myuser",
      "password": "...",
      "email": "user@example.com",
      "auth": "..."
    }
  }
}
```

---

### Step 4: Test Network Connectivity

**What to check:** Verify cluster can reach the registry

**How to check:**
```bash
# Create debug pod to test connectivity
kubectl run -it --rm debug --image=nicolaka/netshoot --restart=Never -n <namespace> -- bash

# Inside debug pod:
# Test DNS resolution
nslookup myregistry.com

# Test connectivity
curl -v https://myregistry.com/v2/

# Test with authentication
curl -u username:password https://myregistry.com/v2/
```

**Common connectivity issues:**
- Registry URL is internal/private and not accessible from cluster
- Firewall blocking outbound connections
- DNS cannot resolve registry hostname
- Wrong protocol (http vs https)

---

## Solutions

### Solution 1: Create or Update Image Pull Secret

**When to use:**
- Private registry requires authentication
- Secret is missing or incorrect
- Credentials have expired or changed

**Implementation Steps:**

1. **Create docker-registry secret**
   ```bash
   kubectl create secret docker-registry <secret-name> \
     --docker-server=<registry-url> \
     --docker-username=<username> \
     --docker-password=<password> \
     --docker-email=<email> \
     -n <namespace>
   ```

   **Examples:**
   ```bash
   # Docker Hub
   kubectl create secret docker-registry dockerhub-secret \
     --docker-server=docker.io \
     --docker-username=myusername \
     --docker-password=mypassword \
     --docker-email=me@example.com \
     -n production

   # Private registry
   kubectl create secret docker-registry private-registry-secret \
     --docker-server=registry.example.com \
     --docker-username=serviceaccount \
     --docker-password=token123 \
     -n production

   # Google Container Registry (GCR)
   kubectl create secret docker-registry gcr-secret \
     --docker-server=gcr.io \
     --docker-username=_json_key \
     --docker-password="$(cat keyfile.json)" \
     -n production
   ```

2. **Add secret to deployment**
   ```bash
   # Method 1: Patch deployment
   kubectl patch deployment <deployment-name> -n <namespace> -p '{"spec":{"template":{"spec":{"imagePullSecrets":[{"name":"<secret-name>"}]}}}}'

   # Method 2: Edit deployment
   kubectl edit deployment <deployment-name> -n <namespace>
   ```

   Add imagePullSecrets:
   ```yaml
   spec:
     template:
       spec:
         imagePullSecrets:
         - name: private-registry-secret
         containers:
         - name: app
           image: registry.example.com/myapp:v1.2.3
   ```

3. **Alternative: Add secret to service account**
   ```bash
   # This applies to all pods using this service account
   kubectl patch serviceaccount default -n <namespace> -p '{"imagePullSecrets":[{"name":"<secret-name>"}]}'
   ```

4. **Verify fix**
   ```bash
   # Watch pods start successfully
   kubectl get pods -n <namespace> -w

   # Check events for successful pull
   kubectl describe pod <pod-name> -n <namespace>
   ```

**Expected outcome:** Pods successfully pull image and start

**Time to resolution:** ~5 minutes

---

### Solution 2: Fix Image Name or Tag

**When to use:**
- Image name is misspelled
- Tag doesn't exist
- Registry URL is wrong

**Implementation Steps:**

1. **Identify correct image**
   ```bash
   # Verify image exists in registry
   docker pull <correct-image>:<tag>

   # Or check registry UI/API for available tags
   ```

2. **Update deployment with correct image**
   ```bash
   # Quick update
   kubectl set image deployment/<deployment-name> <container-name>=<correct-image>:<tag> -n <namespace>

   # Example
   kubectl set image deployment/myapp myapp=registry.example.com/myapp:v1.2.3 -n production
   ```

3. **Monitor rollout**
   ```bash
   kubectl rollout status deployment/<deployment-name> -n <namespace>
   kubectl get pods -n <namespace> -w
   ```

**Expected outcome:** Pods pull correct image and start successfully

**Time to resolution:** ~2-3 minutes

---

### Solution 3: Fix Network/Firewall Issues

**When to use:**
- Registry is not reachable from cluster
- Firewall blocking connections
- DNS resolution fails

**Implementation Steps:**

1. **Verify network connectivity from nodes**
   ```bash
   # SSH to node (if possible)
   # Test registry connectivity
   curl -v https://registry.example.com/v2/

   # Check DNS
   nslookup registry.example.com
   ```

2. **Update firewall rules to allow registry access**
   ```bash
   # Allow outbound HTTPS to registry
   # Specific steps depend on cloud provider/firewall solution

   # AWS Security Group example:
   # Add outbound rule: HTTPS (443) to registry.example.com

   # GCP Firewall example:
   gcloud compute firewall-rules create allow-registry \
     --allow tcp:443 \
     --destination-ranges=<registry-ip>/32
   ```

3. **If using HTTP registry (not recommended)**
   ```bash
   # Add insecure registry to Docker daemon on nodes
   # Edit /etc/docker/daemon.json on each node:
   {
     "insecure-registries": ["registry.example.com:5000"]
   }

   # Restart Docker daemon
   sudo systemctl restart docker
   ```

4. **Verify connectivity**
   ```bash
   # Test from debug pod
   kubectl run -it --rm debug --image=nicolaka/netshoot --restart=Never -- curl -v https://registry.example.com/v2/
   ```

**Expected outcome:** Registry is accessible from cluster nodes

**Time to resolution:** ~15-30 minutes (depends on firewall process)

---

### Solution 4: Handle Docker Hub Rate Limiting

**When to use:**
- Pulling from Docker Hub (docker.io)
- Getting "toomanyrequests" error
- Anonymous pulls exceeding limits

**Rate limits:**
- Anonymous: 100 pulls per 6 hours per IP
- Authenticated free: 200 pulls per 6 hours
- Pro accounts: Higher limits

**Implementation Steps:**

1. **Create Docker Hub account and get credentials**
   - Sign up at hub.docker.com
   - Note username and password/access token

2. **Create authenticated pull secret**
   ```bash
   kubectl create secret docker-registry dockerhub-auth \
     --docker-server=docker.io \
     --docker-username=<your-username> \
     --docker-password=<your-password-or-token> \
     -n <namespace>
   ```

3. **Add to deployments**
   ```bash
   kubectl patch deployment <deployment-name> -n <namespace> \
     -p '{"spec":{"template":{"spec":{"imagePullSecrets":[{"name":"dockerhub-auth"}]}}}}'
   ```

4. **Alternative: Use image mirror**
   ```bash
   # Use Quay.io, GitHub Container Registry, or private mirror
   kubectl set image deployment/<name> <container>=quay.io/<org>/<image>:<tag> -n <namespace>
   ```

**Expected outcome:** Authenticated pulls avoid rate limits

**Time to resolution:** ~5 minutes

---

## Root Cause Analysis

**Why ImagePullBackOff happens:**

Image pull failure occurs when Kubernetes cannot retrieve the container image:

1. **Kubelet tries to pull image** from specified registry
2. **Pull fails** due to auth, network, or image availability
3. **Kubernetes retries** with exponential backoff
4. **Status changes** from `ErrImagePull` → `ImagePullBackOff`

**Common scenarios:**

1. **Private Registry Without Auth** (40%)
   - Deployment specifies private registry image
   - No imagePullSecret configured
   - Kubelet cannot authenticate

2. **Typos in Image Specification** (30%)
   - Wrong tag (lastest vs latest)
   - Misspelled image name
   - Wrong registry URL

3. **Network Issues** (20%)
   - Registry not accessible from cluster
   - Firewall blocking connections
   - DNS cannot resolve registry

4. **Rate Limiting** (10%)
   - Too many pulls from Docker Hub
   - Anonymous access exceeded
   - Need authenticated pulls

---

## Prevention

### Immediate Prevention

1. **Always specify image tags explicitly**
   ```yaml
   # Bad
   image: myapp  # Defaults to :latest

   # Good
   image: myapp:v1.2.3  # Explicit version
   ```

2. **Validate images before deployment**
   ```bash
   # Pull image locally first
   docker pull myapp:v1.2.3

   # Verify it exists
   docker images | grep myapp
   ```

3. **Use private registry with authentication**
   ```bash
   # Always create imagePullSecret for private registries
   kubectl create secret docker-registry my-registry \
     --docker-server=registry.example.com \
     --docker-username=$USER \
     --docker-password=$PASSWORD \
     -n production
   ```

### Long-Term Prevention

1. **Use container registry within cluster VPC**
   - AWS ECR, Google GCR, Azure ACR
   - Faster pulls
   - Better security
   - Lower costs

2. **Implement image scanning and policies**
   ```yaml
   # Use admission controller (OPA, Kyverno) to enforce policies
   # Example Kyverno policy:
   apiVersion: kyverno.io/v1
   kind: ClusterPolicy
   metadata:
     name: require-image-tag
   spec:
     validationFailureAction: enforce
     rules:
     - name: require-tag
       match:
         resources:
           kinds:
           - Pod
       validate:
         message: "Image tag is required"
         pattern:
           spec:
             containers:
             - image: "*:*"  # Must have tag
   ```

3. **Use image pull secret automatically**
   ```bash
   # Add to default service account
   kubectl patch serviceaccount default -n production \
     -p '{"imagePullSecrets":[{"name":"registry-secret"}]}'
   ```

4. **Monitor registry availability**
   ```yaml
   # Prometheus alert
   - alert: RegistryDown
     expr: probe_success{job="registry-probe"} == 0
     for: 5m
     labels:
       severity: critical
   ```

### Best Practices

- ✅ Always use specific image tags (never :latest in production)
- ✅ Create imagePullSecrets for private registries
- ✅ Validate images before deployment
- ✅ Use authenticated Docker Hub pulls
- ✅ Monitor registry availability
- ✅ Use registry in same region/VPC as cluster
- ✅ Implement image scanning in CI/CD
- ✅ Document image tagging strategy
- ❌ Don't use :latest tag in production
- ❌ Don't deploy without testing image pull
- ❌ Don't rely on anonymous Docker Hub pulls
- ❌ Don't use public registries for sensitive images

---

## Related Issues

**Related runbooks:**
- [Kubernetes - Pod CrashLoopBackOff](k8s-pod-crashloopbackoff.md) - Pod crashes after successful pull
- [Kubernetes - Node Not Ready](k8s-node-not-ready.md) - Node-level image storage issues

**External resources:**
- [Pull an Image from a Private Registry](https://kubernetes.io/docs/tasks/configure-pod-container/pull-image-private-registry/)
- [Images](https://kubernetes.io/docs/concepts/containers/images/)
- [Understanding Docker Hub Rate Limiting](https://docs.docker.com/docker-hub/download-rate-limit/)

---

## Version History

| Version | Date       | Author           | Changes                  |
|---------|------------|------------------|--------------------------|
| 1.0.0   | 2025-01-15 | FaultMaven Team  | Initial verified version |

---

## License & Attribution

This runbook is part of the FaultMaven Knowledge Base.

Licensed under Apache-2.0 License. Contributions welcome via Pull Request.

See [CONTRIBUTING.md](../CONTRIBUTING.md) for contribution guidelines.
