---
id: cert-manager-issuance-failure
title: "cert-manager Certificate Issuance Failure — ACME and Let's Encrypt Diagnosis"
domain: networking
service: cert-manager
symptom_class:
  - auth_failure
  - timeout
severity: high
scope: global
version: "2.1.0"
last_updated: "2026-03-26"
verified_by: "kb-researcher"
status: draft
tags:
  - cert-manager
  - tls
  - certificate
  - lets-encrypt
  - acme
  - dns-challenge
  - http-challenge
difficulty: intermediate
---

## Problem Definition

This runbook covers cert-manager certificate issuance failures in Kubernetes clusters. It applies to cert-manager v1.x deployments using ACME-based Issuers or ClusterIssuers (typically Let's Encrypt). Diagnosis requires `kubectl` access with permissions to read Certificate, CertificateRequest, Order, and Challenge resources, plus access to cert-manager controller logs in the `cert-manager` namespace. For DNS-01 challenges, access to the DNS provider console or API is needed to verify record creation.

A cert-manager issuance failure occurs when cert-manager cannot obtain or renew a TLS certificate from an ACME provider. The Certificate resource remains in a non-Ready state, the associated CertificateRequest shows errors, and the Order or Challenge resources reveal the specific failure point. Services depending on the certificate may serve expired or self-signed certificates, causing TLS errors for clients. The failure is silent from the application perspective — the certificate simply does not appear or renew, and the TLS secret remains stale.

Certificate issuance follows a pipeline: Certificate → CertificateRequest → Order → Challenge. Failures can occur at any stage. Common causes include:

- **HTTP-01 challenge failure** — Let's Encrypt cannot reach the `/.well-known/acme-challenge/` endpoint on the domain. Causes include ingress misconfiguration, firewall rules blocking port 80, or DNS not pointing to the cluster's external IP.
- **DNS-01 challenge failure** — cert-manager cannot create the `_acme-challenge` TXT record in the DNS provider. Causes include incorrect DNS provider credentials, IAM permission errors, wrong hosted zone ID, or DNS propagation delays.
- **Let's Encrypt rate limits** — exceeded the rate limit for certificates per registered domain (50 per week), duplicate certificates (5 per week), or failed validations (5 per hour per account per hostname).
- **Issuer or ClusterIssuer misconfiguration** — wrong ACME server URL, invalid or expired account key, missing or incorrect solver configuration.
- **DNS propagation delay** — the TXT record is created but not yet visible to Let's Encrypt's validation servers due to TTL or propagation lag.
- **Ingress class mismatch** — the HTTP-01 solver creates a temporary Ingress but uses a different ingress class than the cluster's ingress controller, so traffic never reaches the solver pod.
- **Private or unreachable domain** — the domain resolves to a private IP or is behind a CDN/proxy that strips the challenge path.
- **Namespace or secret permission error** — cert-manager cannot read the DNS provider secret or write the resulting TLS secret to the target namespace.
- **cert-manager controller not running** — the cert-manager pod itself is crashed, OOMKilled, or not scheduling.

Typical error indicators: Certificate `Ready=False`, CertificateRequest with `Denied` or error conditions, Challenge stuck in `pending` or showing `invalid` state, and cert-manager controller logs containing `acme: error` or `challenge validation failed`.

## Diagnostic Steps

### Step 1: Check Certificate resource status

Checks whether the Certificate is in a Ready state and what condition message explains any failure.

```bash
kubectl get certificate -n <namespace>
kubectl describe certificate <cert-name> -n <namespace>
```

Expected output for a healthy certificate: `READY=True` with a valid `Not After` date. If `READY=False`, the `status.conditions` message describes the failure (e.g., `Issuing certificate as Secret does not exist`, `The certificate request has failed to complete`). The `status.lastFailureTime` field shows when the last issuance attempt failed.

### Step 2: Trace the issuance pipeline

Follows the cert-manager resource chain (Certificate → CertificateRequest → Order → Challenge) to find the exact failure point. The Challenge resource contains the most specific error.

```bash
# Check CertificateRequest status
kubectl get certificaterequest -n <namespace>
kubectl describe certificaterequest <cr-name> -n <namespace>

# Check Order status (created by CertificateRequest)
kubectl get order -n <namespace>
kubectl describe order <order-name> -n <namespace>

# Check Challenge status (created by Order, one per domain)
kubectl get challenge -n <namespace>
kubectl describe challenge <challenge-name> -n <namespace>
```

Expected output: the Challenge resource shows `status.state` as `pending`, `valid`, `invalid`, or `expired`. An `invalid` state with `status.reason` containing the ACME server error message (e.g., `Incorrect TXT record`, `Connection refused`) identifies the specific failure. If no Challenge exists, the failure is at the Order or CertificateRequest level.

### Step 3: Check cert-manager controller logs

Checks for errors in the cert-manager controller, webhook, and cainjector components that may prevent issuance.

```bash
# Find cert-manager controller pod
kubectl get pods -n cert-manager

# Check controller logs for errors
kubectl logs -l app.kubernetes.io/name=cert-manager -n cert-manager --tail=100 | grep -i "error\|warn\|fail"

# Check the webhook logs (handles validation)
kubectl logs -l app.kubernetes.io/name=webhook -n cert-manager --tail=50

# Check cainjector logs (handles CA bundle injection)
kubectl logs -l app.kubernetes.io/name=cainjector -n cert-manager --tail=50
```

Expected output: no error lines in steady state. Common error patterns include `acme: error: 429` (rate limited), `context deadline exceeded` (DNS propagation timeout), `failed to create TXT record` (DNS provider credentials), and `secret not found` (missing credential secret). A CrashLoopBackOff on the controller pod itself prevents all issuance.

### Step 4: Diagnose HTTP-01 challenge failures

Checks whether the HTTP-01 challenge solver is reachable from the internet, which is required for Let's Encrypt validation.

```bash
# Check if the challenge solver pod and service are running
kubectl get pods -n <namespace> -l "acme.cert-manager.io/http01-solver=true"
kubectl get svc -n <namespace> -l "acme.cert-manager.io/http01-solver=true"
kubectl get ingress -n <namespace> -l "acme.cert-manager.io/http01-solver=true"

# Test if the challenge endpoint is reachable from outside the cluster
curl -v http://<domain>/.well-known/acme-challenge/<token>

# Check if port 80 is open from the internet
nslookup <domain>
curl -sI http://<domain>/ | head -5

# Check if the ingress controller is handling the solver ingress
kubectl get ingress -n <namespace> -o yaml | grep "ingressClassName\|kubernetes.io/ingress.class"
```

Expected output: the solver pod is Running, the solver Ingress has an address assigned, and the curl command returns a 200 with the challenge token. If the curl returns connection refused, a timeout, or a 404, the challenge path is not being served. A mismatch between the solver Ingress class and the cluster's ingress controller class is the most common cause.

### Step 5: Diagnose DNS-01 challenge failures

Checks whether cert-manager can create the required TXT record and whether Let's Encrypt can see it.

```bash
# Check if the TXT record was created
dig +short TXT _acme-challenge.<domain>
# Use a public DNS resolver to check propagation
dig +short TXT _acme-challenge.<domain> @8.8.8.8

# Check the DNS provider credentials secret
kubectl get secret <dns-provider-secret> -n cert-manager -o yaml

# For AWS Route53: check IAM permissions
# The role needs: route53:GetChange, route53:ChangeResourceRecordSets, route53:ListHostedZonesByName
aws route53 list-hosted-zones --query "HostedZones[?Name=='<domain>.']"

# For Cloudflare: check API token scope (Zone:DNS:Edit)
```

Expected output: `dig` returns the challenge TXT record value. If the record does not exist, cert-manager failed to create it — check the controller logs for DNS provider errors. If the record exists via direct query but not via `@8.8.8.8`, DNS has not propagated yet. An incorrect hosted zone ID causes records to be created in the wrong zone.

### Step 6: Check for Let's Encrypt rate limits

Checks whether issuance is blocked by ACME rate limits rather than a configuration error.

```bash
# Check cert-manager logs for rate limit errors
kubectl logs -l app.kubernetes.io/name=cert-manager -n cert-manager --tail=200 | grep -i "rate\|limit\|too many"

# Check the Order status message for rate limit details
kubectl get order -n <namespace> -o jsonpath='{range .items[*]}{.metadata.name}: {.status.reason}{"\n"}{end}'
```

Expected output: no rate limit messages. If present, the message specifies which limit was hit. Rate limit reference for Let's Encrypt: 50 certificates per registered domain per week, 5 duplicate certificates per week, 5 failed validation attempts per account per hostname per hour, 300 new orders per account per 3 hours. The Certificate Transparency log at `https://crt.sh/?q=<domain>` shows how many certificates were recently issued.

### Step 7: Verify Issuer/ClusterIssuer configuration

Checks whether the Issuer or ClusterIssuer is correctly configured and has a registered ACME account.

```bash
# Check Issuer or ClusterIssuer status
kubectl get issuer -n <namespace>
kubectl get clusterissuer
kubectl describe issuer <issuer-name> -n <namespace>
kubectl describe clusterissuer <clusterissuer-name>

# Verify the ACME server URL
# Production: https://acme-v02.api.letsencrypt.org/directory
# Staging: https://acme-staging-v02.api.letsencrypt.org/directory

# Check if the Issuer has a registered ACME account
kubectl get issuer <issuer-name> -n <namespace> -o jsonpath='{.status.acme.uri}'
```

Expected output: the Issuer shows `Ready=True` and `status.acme.uri` contains a registered account URL. If the Issuer is not Ready, the ACME server URL may be wrong, the account key may be invalid, or the solver configuration is missing.

## Mitigation

### Option 1: Switch to Let's Encrypt staging for debugging

- **Risk**: Low. Staging certificates are not trusted by browsers but have much higher rate limits (30,000 certificates per registered domain per week). Use for debugging the issuance pipeline without hitting production rate limits.
- **Command**:

```bash
# Update the Issuer to use staging ACME server
kubectl edit issuer <issuer-name> -n <namespace>
# Change: server: https://acme-v02.api.letsencrypt.org/directory
# To:     server: https://acme-staging-v02.api.letsencrypt.org/directory

# Delete the failed Certificate to trigger re-issuance
kubectl delete certificate <cert-name> -n <namespace>
# Re-apply the Certificate resource
kubectl apply -f <certificate-manifest>.yaml
```

- **Verify**:

```bash
kubectl get certificate <cert-name> -n <namespace> -w
# Wait for Ready=True (staging cert)
```

- **Duration**: 1-5 minutes for HTTP-01, 2-10 minutes for DNS-01 (depends on propagation).

### Option 2: Manually create a temporary TLS secret

- **Risk**: Medium. Bypasses cert-manager entirely. The manually-created certificate will not auto-renew. Set a calendar reminder to replace it.
- **Command**:

```bash
# Generate a self-signed certificate for immediate service restoration
openssl req -x509 -nodes -days 7 -newkey rsa:2048 \
    -keyout /tmp/tls.key -out /tmp/tls.crt \
    -subj "/CN=<domain>"

# Create the TLS secret (overwrite if exists)
kubectl create secret tls <secret-name> \
    --cert=/tmp/tls.crt --key=/tmp/tls.key \
    -n <namespace> --dry-run=client -o yaml | kubectl apply -f -

# Clean up local files
rm /tmp/tls.key /tmp/tls.crt
```

- **Verify**:

```bash
kubectl get secret <secret-name> -n <namespace>
curl -vk https://<domain>/ 2>&1 | grep "subject:\|issuer:\|expire"
```

- **Duration**: Immediate. Replace with a proper cert-manager certificate within 7 days.

### Option 3: Force certificate re-issuance

- **Risk**: Low. Deletes the existing Certificate resource and re-creates it, triggering a fresh issuance attempt from scratch.
- **Command**:

```bash
# Delete the stuck certificate and all associated resources
kubectl delete certificate <cert-name> -n <namespace>

# Wait for cert-manager to clean up Orders and Challenges
sleep 10

# Re-apply the Certificate manifest
kubectl apply -f <certificate-manifest>.yaml

# Watch the issuance progress
kubectl get certificate <cert-name> -n <namespace> -w
```

- **Verify**:

```bash
kubectl get certificate <cert-name> -n <namespace>
kubectl get certificaterequest -n <namespace> --sort-by=.metadata.creationTimestamp | tail -3
```

- **Duration**: 1-10 minutes depending on challenge type and DNS propagation.

### Option 4: Fix ingress class for HTTP-01 solver

- **Risk**: Low. Corrects the solver configuration so the ACME challenge endpoint becomes reachable.
- **Command**:

```bash
# Check which ingress class the cluster uses
kubectl get ingressclass

# Update the Issuer solver to use the correct ingress class
kubectl edit issuer <issuer-name> -n <namespace>
# Ensure the http01 solver specifies the correct ingress class:
# solvers:
#   - http01:
#       ingress:
#         class: nginx  # or your ingress controller's class
```

- **Verify**:

```bash
kubectl delete certificate <cert-name> -n <namespace>
kubectl apply -f <certificate-manifest>.yaml
kubectl get challenge -n <namespace> -w
```

- **Duration**: 1-5 minutes after fix.

## Root Cause Resolution

**If** HTTP-01 challenge fails with "connection refused" or "no route to host" → port 80 is not reachable from the internet. Ensure the ingress controller serves port 80, firewall/security groups allow inbound port 80, and the domain's DNS A record points to the cluster's external load balancer IP.

```bash
dig +short A <domain>
nc -zv <external-ip> 80 -w 5
```

**If** HTTP-01 challenge fails with "invalid response" or 404 → the solver's temporary Ingress is not being served by the ingress controller. Fix the ingress class in the Issuer solver configuration to match the cluster's ingress controller class.

**If** DNS-01 challenge fails with "unauthorized" or "access denied" → the DNS provider credentials are wrong or the IAM role lacks permissions. Update the Secret with correct credentials and verify the IAM policy includes the required DNS record management permissions (Route53: `route53:GetChange`, `route53:ChangeResourceRecordSets`, `route53:ListHostedZonesByName`, `route53:ListResourceRecordSets`).

**If** DNS-01 challenge fails with "NXDOMAIN" for `_acme-challenge.<domain>` → cert-manager created the TXT record in the wrong hosted zone, or the zone ID in the Issuer does not match the domain. Verify the `hostedZoneID` (Route53) or `project`/`zone` (Cloud DNS) in the Issuer configuration.

**If** the Order shows "too many certificates already issued" → the Let's Encrypt rate limit has been hit. Wait for the rate limit window to expire (1 week for most limits). Use the staging ACME server for testing or request a rate limit override from Let's Encrypt for high-volume domains.

**If** DNS-01 challenge is stuck in "pending" with no TXT record appearing → DNS propagation is too slow for the default check timeout. Add `dns01RecursiveNameservers` to the cert-manager deployment to use specific resolvers and increase the propagation check timeout.

```bash
# Add to cert-manager Deployment args:
# --dns01-recursive-nameservers-only
# --dns01-recursive-nameservers=8.8.8.8:53,1.1.1.1:53
kubectl edit deployment cert-manager -n cert-manager
```

**If** cert-manager controller is CrashLoopBackOff or OOMKilled → increase resource limits for the cert-manager deployment. Check for excessive Certificate resources causing high memory usage during reconciliation.

```bash
kubectl set resources deployment/cert-manager -n cert-manager --limits=cpu=500m,memory=512Mi --requests=cpu=100m,memory=256Mi
```

## Verification

After applying a fix, verify the certificate is issued:

1. Confirm the Certificate resource reaches Ready state:

```bash
kubectl get certificate <cert-name> -n <namespace>
# Should show READY=True and an expiry date
```

2. Verify the TLS secret was created with the correct certificate:

```bash
kubectl get secret <secret-name> -n <namespace> -o jsonpath='{.data.tls\.crt}' | base64 -d | openssl x509 -noout -subject -issuer -dates
```

3. Test the TLS connection end-to-end:

```bash
curl -vI https://<domain>/ 2>&1 | grep "subject:\|issuer:\|expire\|SSL certificate verify"

echo | openssl s_client -connect <domain>:443 -servername <domain> 2>/dev/null | openssl x509 -noout -subject -issuer -dates
```

4. Confirm the issuance pipeline is clean (no stuck resources):

```bash
kubectl get certificaterequest,order,challenge -n <namespace>
# There should be no resources in pending or errored state
```

5. Verify auto-renewal will work by checking the certificate's renewal time:

```bash
kubectl get certificate <cert-name> -n <namespace> -o jsonpath='{.status.renewalTime}'
# Should be set to approximately 30 days before expiry
```

## Prevention

1. **Use the staging ACME server for testing** — Always test new certificate configurations against `https://acme-staging-v02.api.letsencrypt.org/directory` first. Switch to production only after confirming the pipeline works end-to-end. This avoids burning rate limits.

2. **Monitor Certificate resource readiness** — Set up alerts for Certificate resources with `Ready=False` that persist for more than 15 minutes. Use `kubectl get certificate` in a monitoring check or integrate with Prometheus via the cert-manager metrics endpoint (`certmanager_certificate_ready_status` on port 9402).

3. **Configure DNS-01 propagation checks** — For DNS-01 challenges, set `--dns01-recursive-nameservers` to public resolvers (8.8.8.8, 1.1.1.1) so cert-manager validates propagation from the same perspective as Let's Encrypt.

4. **Set up certificate expiry alerts** — Monitor `certmanager_certificate_expiration_timestamp_seconds` and alert when a certificate is within 14 days of expiry without renewal.

5. **Document and test DNS provider credentials rotation** — When rotating DNS API tokens or IAM credentials, update the cert-manager Secret and verify issuance still works. Automate credential rotation to prevent silent expiry.

6. **Use ClusterIssuer for multi-namespace consistency** — Use ClusterIssuer instead of per-namespace Issuers to centralize ACME configuration and reduce misconfiguration risk.

7. **Keep cert-manager updated** — cert-manager releases include fixes for ACME protocol changes, DNS provider bugs, and security issues. Pin to a supported minor version and apply patch updates promptly.

8. **Pre-create certificates before go-live** — Issue certificates well before a domain goes live to avoid scrambling during launch. Let's Encrypt rate limits are per-registered-domain, so early issuance reserves your quota.

9. **Implement fallback certificate sources** — For critical services, consider a secondary Issuer (such as a private CA or a different ACME provider) that can issue certificates if the primary provider is unavailable.

10. **Separate certificate lifecycle from application deploys** — Do not include Certificate resources in application Helm charts or manifests that are frequently deleted and re-created. Certificate resources should be long-lived to maintain renewal history.

## Sources

- [cert-manager Troubleshooting Guide](https://cert-manager.io/docs/troubleshooting/) — Official troubleshooting guide covering Certificate, CertificateRequest, Order, and Challenge resource debugging, including common error messages and resolution steps.
- [cert-manager ACME Troubleshooting](https://cert-manager.io/docs/troubleshooting/acme/) — Official guide specifically for ACME (Let's Encrypt) issuance failures, covering HTTP-01 and DNS-01 challenge debugging, rate limits, and solver configuration.
- [Let's Encrypt Rate Limits](https://letsencrypt.org/docs/rate-limits/) — Authoritative reference for Let's Encrypt rate limit thresholds, including certificates per registered domain, duplicate certificates, failed validations, and new order limits.
- [cert-manager Configuration: Issuers](https://cert-manager.io/docs/configuration/acme/) — Reference for ACME Issuer and ClusterIssuer configuration, solver types, DNS provider setup, and ingress class settings.
