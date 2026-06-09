---
id: cert-manager-issuance-failure
title: "cert-manager Certificate Issuance Failure"
domain: networking
service: cert-manager
symptom_class:
  - auth_failure
  - timeout
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-05-12"
verified_by: kb-researcher
status: draft
tags:
  - cert-manager
  - acme
  - lets-encrypt
  - tls
  - http-01
  - dns-01
  - rate-limit
  - clusterissuer
  - challenge
difficulty: intermediate
---

# cert-manager Certificate Issuance Failure

## Symptom Recognition

- `kubectl get certificate -n <ns>` shows `READY=False` for one or more Certificate resources; `kubectl describe certificate <name>` lists a Condition with `Status: False` and a Message such as `Issuing certificate as Secret does not exist`, `Waiting on certificate issuance from order <name>: "pending"`, or `The certificate request has failed to complete and will be retried`.
- The dependent workload serves a stale TLS secret, a self-signed kubelet certificate, or terminates handshakes with `tls: internal error` — the application has not been changed, only the certificate failed to renew.
- A child `CertificateRequest`, `Order`, or `Challenge` resource exists with `status.state` of `pending`, `errored`, `invalid`, or `expired`; `kubectl describe challenge <name>` carries a `Reason` such as `Waiting for HTTP-01 challenge propagation: failed to perform self check GET request`, `Waiting for DNS-01 challenge propagation: NS <ns> returned REFUSED for _acme-challenge.<domain>`, or an ACME error URN.
- cert-manager controller log (`kubectl logs -n cert-manager deploy/cert-manager`) emits ACME-protocol error strings: `acme: error: 429 :: urn:ietf:params:acme:error:rateLimited :: Error creating new order :: too many certificates already issued for "<domain>"`, `urn:ietf:params:acme:error:rateLimited :: too many failed authorizations recently`, `acme: authorization error for <domain>: 400 urn:ietf:params:acme:error:unauthorized :: Invalid response from http://<domain>/.well-known/acme-challenge/<token>`, `context deadline exceeded`, `propagation check failed: NS <ns> returned REFUSED`, or `Failed to update ACME account: 400 urn:ietf:params:acme:error:invalidEmail`.
- `kubectl get clusterissuer` (or `kubectl get issuer -n <ns>`) shows `READY=False`; describe output reports `Failed to register ACME account`, `Failed to verify ACME account`, or `acmeAccountVerificationFailed`.
- `kubectl get pods -n cert-manager` shows the `cert-manager` controller, `cert-manager-webhook`, or `cert-manager-cainjector` pod in `CrashLoopBackOff`, `OOMKilled`, or `0/1 Running` with not-ready status; `kubectl apply` of a Certificate manifest fails with `failed calling webhook "webhook.cert-manager.io": ... connection refused` or `... context deadline exceeded`.
- ACME validation HTTP responses include `503 Service Unavailable` with a `Retry-After` header (Let's Encrypt rate-limit signal) or a 4xx body containing a `urn:ietf:params:acme:error:*` URN.

## Applicability

- cert-manager v1.13+ installed in the `cert-manager` namespace on Kubernetes 1.25+, using ACME `Issuer` or `ClusterIssuer` resources backed by Let's Encrypt (production `https://acme-v02.api.letsencrypt.org/directory` or staging `https://acme-staging-v02.api.letsencrypt.org/directory`).
- `kubectl` configured with read access to `Certificate`, `CertificateRequest`, `Order`, `Challenge`, `Issuer`, `ClusterIssuer`, `Secret`, and `Pod` resources cluster-wide; write access in the affected workload namespace and in `cert-manager`.
- `cmctl` (cert-manager CLI) on the operator's host, matching the controller minor version.
- For HTTP-01 diagnosis: external reachability of the cluster on TCP/80 from the public internet, and `dig`, `curl`, or equivalent.
- For DNS-01 diagnosis: read access to the DNS-provider secret (`Secret` referenced by `solvers[].dns01.<provider>.<credentialField>`), credentials or console access to the DNS provider for record verification, and `dig +short TXT _acme-challenge.<domain> @<resolver>`.
- For webhook diagnosis: ability to `kubectl exec` into the `cert-manager-webhook` pod and (in private clusters) inspect node-to-API-server firewall rules on the webhook port.

## Diagnostic Steps

### Step 1: Determine which Certificate is failing and which Condition message is set

```bash
kubectl get certificate -A -o wide
kubectl describe certificate -n <ns> <cert-name>
cmctl status certificate -n <ns> <cert-name>
```

Expected output: `READY=True` with a non-empty `Not After` date for a healthy certificate. A failing Certificate prints `Ready: False` with a Condition `Reason` (`Issuing`, `Failed`, or `DoesNotExist`) and a Message that names the next resource in the chain. `cmctl status certificate` prints the full chain (Certificate → CertificateRequest → Order → Challenge) in one view; record the exact Message string verbatim — it selects the Cause below.

### Step 2: Walk the issuance chain to the failing child resource

```bash
kubectl get certificaterequest -n <ns> --sort-by=.metadata.creationTimestamp
kubectl describe certificaterequest -n <ns> <cr-name>
kubectl get order -n <ns>
kubectl describe order -n <ns> <order-name>
kubectl get challenge -n <ns>
kubectl describe challenge -n <ns> <challenge-name>
```

Expected output: each parent resource references its child by name. The terminal child carries the operative error in its `status.reason` / `status.state` (Challenge: `pending`, `valid`, `invalid`, `expired`; Order: `pending`, `ready`, `processing`, `valid`, `invalid`, `errored`; CertificateRequest: a Condition with `Reason: Failed` and `Message` containing the ACME error URN). When no Challenge exists, the failure is at the Order layer (rate-limit, invalid email, account registration); when a Challenge exists in `invalid` state, the failure is at the validation layer (HTTP-01 self-check, DNS-01 propagation).

### Step 3: Grep cert-manager controller logs for the ACME error string

```bash
kubectl logs -n cert-manager -l app.kubernetes.io/name=cert-manager --tail=400 \
  | grep -iE "error|rateLimited|urn:ietf:params:acme|propagation|self check|failed|invalidEmail|unauthorized"
kubectl logs -n cert-manager -l app.kubernetes.io/name=cert-manager --since=1h \
  | grep -E "<cert-name>|<order-name>|<challenge-name>"
```

Expected output: steady-state logs print `Found relevant CertificateRequest resource for Certificate` and `Issuing certificate as Secret does not exist` (one-time, during initial issuance) without `error`. A failing reconcile prints one of the canonical ACME strings — `acme: error: 429 :: urn:ietf:params:acme:error:rateLimited`, `urn:ietf:params:acme:error:invalidEmail`, `urn:ietf:params:acme:error:unauthorized`, `urn:ietf:params:acme:error:dns`, `failed to perform self check GET request`, or `context deadline exceeded` — each maps directly to one Cause below.

### Step 4: Verify Issuer / ClusterIssuer is Ready and the ACME account is registered

```bash
kubectl get clusterissuer -o wide
kubectl get issuer -A -o wide
kubectl describe clusterissuer <issuer-name>
kubectl get clusterissuer <issuer-name> -o jsonpath='{.spec.acme.server}{"\n"}{.status.acme.uri}{"\n"}'
kubectl get secret -n cert-manager <privateKeySecretRef-name> -o jsonpath='{.data.tls\.key}' | head -c 40
```

Expected output: `READY=True`; `status.acme.uri` is a non-empty URL such as `https://acme-v02.api.letsencrypt.org/acme/acct/<id>`; the private-key Secret exists in the `cert-manager` namespace (for `ClusterIssuer`) or in the issuer's namespace (for `Issuer`). A Ready=False issuer with Message `Failed to register ACME account: 400 urn:ietf:params:acme:error:invalidEmail` or `Failed to verify ACME account` blocks every Certificate that references it.

### Step 5: Diagnose HTTP-01 self-check failure

```bash
# Solver resources cert-manager creates during HTTP-01 validation:
kubectl get pods,svc,ingress -n <ns> -l acme.cert-manager.io/http01-solver=true
kubectl get ingress -n <ns> -l acme.cert-manager.io/http01-solver=true \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.ingressClassName}{"\t"}{.status.loadBalancer.ingress[*].ip}{"\n"}{end}'

# Solver Ingress class vs cluster ingress controller:
kubectl get ingressclass
kubectl get clusterissuer <issuer-name> -o jsonpath='{.spec.acme.solvers}' | jq .

# Reach the challenge URL exactly as the ACME server would, from the public internet:
TOKEN=$(kubectl get challenge -n <ns> <challenge-name> -o jsonpath='{.spec.token}')
curl -v -H "Host: <domain>" "http://<domain>/.well-known/acme-challenge/${TOKEN}"
curl -sI "http://<domain>/.well-known/acme-challenge/test" | head -3
dig +short A <domain>
```

Expected output: solver Pod is `Running`, solver Service has endpoints, solver Ingress is admitted with an `ADDRESS` and an `ingressClassName` that matches one of the cluster's `IngressClass` resources. The `curl` GET returns `HTTP/1.1 200 OK` and a body equal to the challenge key authorization. A `connection refused`, `connection timed out`, `404 Not Found` body, redirect to HTTPS, or a different IP from `dig` than the cluster's external load balancer points at one of Causes B, C, or D.

### Step 6: Diagnose DNS-01 challenge propagation and provider credentials

```bash
# What TXT does cert-manager think it published?
kubectl get challenge -n <ns> <challenge-name> -o jsonpath='{.spec.token}{"\n"}{.status.presented}{"\n"}{.status.reason}{"\n"}'

# What does authoritative DNS actually answer right now?
dig +short TXT _acme-challenge.<domain>
dig +short TXT _acme-challenge.<domain> @8.8.8.8
dig +short TXT _acme-challenge.<domain> @1.1.1.1
dig +trace TXT _acme-challenge.<domain> | tail -20

# DNS-provider credential and zone configuration:
kubectl get clusterissuer <issuer-name> -o jsonpath='{.spec.acme.solvers}' | jq '.[] | select(.dns01)'
kubectl get secret -n cert-manager <dns-provider-secret> -o jsonpath='{.data}' | jq 'keys'

# Provider-specific zone sanity (Route53 example):
aws route53 list-hosted-zones --query "HostedZones[?Name=='<domain>.']"
aws route53 list-resource-record-sets --hosted-zone-id <zone-id> \
  --query "ResourceRecordSets[?Name=='_acme-challenge.<domain>.']"
```

Expected output: `status.presented=true` indicates cert-manager successfully called the DNS provider API; `dig` returns the same TXT value from multiple public resolvers within the propagation-check timeout. An empty `dig` answer with `status.reason: NS <ns> returned REFUSED` or `propagation check failed` indicates the record was never created or the cluster's recursive nameservers cannot see it. A `403 AccessDenied`, `401 Unauthorized`, or provider-specific permission error in controller logs indicates wrong credentials or missing IAM permissions.

### Step 7: Check Let's Encrypt rate-limit headroom

```bash
kubectl logs -n cert-manager -l app.kubernetes.io/name=cert-manager --since=24h \
  | grep -iE "rateLimited|too many certificates|too many failed authorizations|503 Service Unavailable|Retry-After"
kubectl get order -A -o jsonpath='{range .items[*]}{.metadata.namespace}{"\t"}{.metadata.name}{"\t"}{.status.state}{"\t"}{.status.reason}{"\n"}{end}' \
  | grep -E "errored|invalid"

# Independent ledger of recent issuance for this domain (Certificate Transparency):
curl -s "https://crt.sh/?q=<domain>&output=json" | jq '[.[] | select(.entry_timestamp > "'$(date -d '7 days ago' -Iseconds)'")] | length'
```

Expected output: no `rateLimited` lines in the last 24 hours; CT-log query returns a number well below 50 (the per-registered-domain weekly cap) and below 5 for any duplicate identifier set. A controller log containing `urn:ietf:params:acme:error:rateLimited :: Error creating new order :: too many certificates already issued for "<registered-domain>": see https://letsencrypt.org/docs/rate-limits/` indicates Cause F; `urn:ietf:params:acme:error:rateLimited :: too many failed authorizations recently` indicates Cause G.

### Step 8: Inspect cert-manager controller, webhook, and cainjector health

```bash
kubectl get pods -n cert-manager -o wide
kubectl get deploy -n cert-manager
kubectl logs -n cert-manager -l app.kubernetes.io/component=controller --tail=100
kubectl logs -n cert-manager -l app.kubernetes.io/component=webhook --tail=100
kubectl logs -n cert-manager -l app.kubernetes.io/component=cainjector --tail=100
kubectl get endpoints -n cert-manager cert-manager-webhook
kubectl describe deploy -n cert-manager cert-manager | sed -n '/Containers:/,/Conditions:/p'
```

Expected output: all three Deployments report `READY n/n` matching desired; pods have status `Running` with `RESTARTS=0` (or stable, non-growing); `kubectl get endpoints cert-manager-webhook` lists at least one `<ip>:<port>` pair. A CrashLoopBackOff, `OOMKilled`, missing endpoints, or `failed calling webhook "webhook.cert-manager.io": ... connection refused` / `... i/o timeout` / `x509: certificate signed by unknown authority` during `kubectl apply` indicates Cause H.

### Step 9: Verify the produced TLS Secret and certificate chain

```bash
SECRET_NAME=$(kubectl get certificate -n <ns> <cert-name> -o jsonpath='{.spec.secretName}')
kubectl get secret -n <ns> "$SECRET_NAME"
kubectl get secret -n <ns> "$SECRET_NAME" -o jsonpath='{.data.tls\.crt}' \
  | base64 -d | openssl x509 -noout -subject -issuer -dates -ext subjectAltName
echo | openssl s_client -connect <domain>:443 -servername <domain> 2>/dev/null \
  | openssl x509 -noout -subject -issuer -dates
```

Expected output: the Secret exists with non-empty `tls.crt` / `tls.key`; `openssl x509` prints the expected `subject`, an issuer of `O = Let's Encrypt, CN = R3` (or current intermediate), a `notAfter` ~90 days out, and a `subjectAltName` that contains every `spec.dnsNames` entry from the Certificate. A missing Secret, an issuer of `(STAGING) Let's Encrypt` when production was intended, or an SAN list that doesn't include the requested name confirms the issuance never completed against the intended ACME server.

## Causes

### Cause A: ACME account registration failed (invalid email or unaccepted ToS)

**Statement:** The `(Cluster)Issuer` cannot register an ACME account with Let's Encrypt because the configured email is invalid, blocked, or the Terms of Service were not accepted on the configured server, so every downstream Certificate stalls before an Order is ever created.

**Mechanism:** cert-manager registers an ACME account on first reconcile by POSTing to `<server>/acme/new-account` with the configured `spec.acme.email`. Let's Encrypt rejects malformed emails (RFC-5321), addresses on its blocklist (e.g., `example.com`), and any registration where the URL in `acme.server` does not point at a v02 directory. The Issuer never reaches `Ready=True` and `status.acme.uri` stays empty; cert-manager logs `Failed to register ACME account: 400 urn:ietf:params:acme:error:invalidEmail` (or `:badPublicKey`, `:agreementRequired`). Without a registered account, no Order can be POSTed, so every Certificate that references the issuer sits with `Reason: Waiting for issuance`.

**Indicator:**

- [Step 4] `kubectl describe clusterissuer` shows `Ready: False` and Message contains `urn:ietf:params:acme:error:invalidEmail` (or `:badPublicKey`, `:agreementRequired`)
<!-- match: {"step": 4, "predicate": "contains", "target": "urn:ietf:params:acme:error:invalidEmail"} -->
- [Step 3] controller logs contain `Failed to register ACME account`
<!-- match: {"step": 3, "predicate": "contains", "target": "Failed to register ACME account"} -->
- [Step 4] `status.acme.uri` is empty on the (Cluster)Issuer

**Mitigation:**

- **Risk:** Switching the issuer to the staging ACME server lets registration succeed for diagnosis but produces untrusted certificates; clients will hit `x509: certificate signed by unknown authority` until the issuer is pointed back at production.
- **Command:**

  ```bash
  kubectl patch clusterissuer <issuer-name> --type merge -p \
    '{"spec":{"acme":{"server":"https://acme-staging-v02.api.letsencrypt.org/directory","email":"<valid-team-address>@<your-domain>"}}}'
  ```

- **Duration:** Minutes — flip back to production after correcting the email.

**Resolution:**

```bash
# Replace the email with a valid, monitored mailbox on a real domain.
kubectl patch clusterissuer <issuer-name> --type merge -p \
  '{"spec":{"acme":{"email":"<sre-alerts>@<your-domain>"}}}'

# Force a fresh account-key Secret (cert-manager will re-register on next reconcile).
kubectl delete secret -n cert-manager <privateKeySecretRef-name>
kubectl annotate clusterissuer <issuer-name> cert-manager.io/force-reconcile="$(date +%s)" --overwrite
```

**Impact:** Cluster-wide for `ClusterIssuer`; namespace-scoped for `Issuer`. Every Certificate that references the issuer will re-issue on the next reconcile — under heavy load this can fan out hundreds of Orders simultaneously, so verify rate-limit headroom (Cause F) before forcing.

**Rollback:** `kubectl apply` the prior (Cluster)Issuer manifest from git, including the original email and `privateKeySecretRef`.

**Verification:** `kubectl get clusterissuer <issuer-name>` reports `READY=True`; `kubectl get clusterissuer <issuer-name> -o jsonpath='{.status.acme.uri}'` prints a non-empty URL; a test Certificate progresses through CertificateRequest → Order → Challenge within 5 minutes.

### Cause B: HTTP-01 self-check fails — solver endpoint unreachable from the public internet

**Statement:** Let's Encrypt's validation servers cannot reach `http://<domain>/.well-known/acme-challenge/<token>` because port 80 is blocked, the public DNS A record does not point at the cluster's external load balancer, or no ingress listens on the solver path.

**Mechanism:** cert-manager spawns a solver Pod, Service, and Ingress matching `host: <domain>` and `path: /.well-known/acme-challenge/<token>`, then runs an in-cluster self check GET against the same URL; on failure the Challenge stays `pending` with `Waiting for HTTP-01 challenge propagation: failed to perform self check GET request`. Even when the self check passes, Let's Encrypt's external validation fails when TCP/80 is blocked at ISP/firewall/security-group, the public A record points at an old environment, or a CDN/WAF strips `/.well-known/`. HTTP-01 follows up to 10 redirects but only to ports 80 and 443.

**Indicator:**

- [Step 3] controller log contains `failed to perform self check GET request`
<!-- match: {"step": 3, "predicate": "contains", "target": "failed to perform self check GET request"} -->
- [Step 2] Challenge `status.reason` contains `Waiting for HTTP-01 challenge propagation`
<!-- match: {"step": 2, "predicate": "contains", "target": "Waiting for HTTP-01 challenge propagation"} -->
- [Step 5] `curl -v http://<domain>/.well-known/acme-challenge/<token>` returns `connection refused`, a TCP timeout, a 404 body, or redirects to HTTPS before serving the token
- [Step 5] `dig +short A <domain>` returns an address that is not the cluster's ingress external IP

**Mitigation:**

- **Risk:** Switching the affected Certificate to the staging issuer (or to a DNS-01 solver) bypasses HTTP-01 entirely; clients of the workload may temporarily see a self-signed or staging chain until the production issuance lands.
- **Command:**

  ```bash
  kubectl annotate certificate -n <ns> <cert-name> cert-manager.io/issue-temporary-certificate="true" --overwrite
  ```

- **Duration:** Minutes-to-hours — keep the temporary certificate in place only until the public-side reachability is restored.

**Resolution:**

```bash
# 1) Confirm public DNS points at the cluster ingress external IP/hostname.
INGRESS_IP=$(kubectl get svc -n ingress-nginx ingress-nginx-controller \
  -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
dig +short A <domain>
# If the A record is wrong, update it at the DNS provider; do not proceed until dig matches $INGRESS_IP.

# 2) Open TCP/80 to the cluster ingress on every layer in front of it (cloud LB, security group, network ACL, on-prem firewall).
# Cloud-specific examples:
aws ec2 describe-security-groups --group-ids <ingress-sg> --query "SecurityGroups[].IpPermissions[?FromPort==`80`]"
gcloud compute firewall-rules list --filter="targetTags~ingress AND allowed.ports:80"

# 3) Make sure the solver Ingress class matches the cluster's ingress controller.
kubectl get ingressclass
kubectl patch clusterissuer <issuer-name> --type=json \
  -p='[{"op":"replace","path":"/spec/acme/solvers/0/http01/ingress/ingressClassName","value":"nginx"}]'

# 4) Force re-issuance once reachability is fixed.
cmctl renew -n <ns> <cert-name>
```

**Impact:** A DNS-record change is global (TTL-bounded); firewall and ingress-class changes are cluster-wide. Opening TCP/80 to a load balancer that previously only allowed 443 increases the exposed surface — pair with an ingress-level redirect to HTTPS for non-ACME paths.

**Rollback:** Revert the DNS A record or firewall rule to its prior value; `kubectl apply` the prior (Cluster)Issuer manifest to restore the original `ingressClassName`.

**Verification:** `curl -v http://<domain>/.well-known/acme-challenge/test` reaches the cluster ingress (200 or 404 with `server: nginx` header — not connection-refused/timeout); a freshly created Challenge transitions from `pending` to `valid` within 2 minutes; controller logs no longer contain `failed to perform self check GET request` for the affected name.

### Cause C: HTTP-01 solver Ingress uses the wrong ingress class

**Statement:** The HTTP-01 solver Ingress is admitted with an `ingressClassName` that no controller in the cluster claims, so the temporary `/.well-known/acme-challenge/<token>` path never gets served and the self check (and Let's Encrypt) receive a `404 Not Found`.

**Mechanism:** When the `Issuer` / `ClusterIssuer` HTTP-01 solver omits `ingressClassName` (or specifies a class that doesn't exist), the Ingress object is created but no ingress-controller pod picks it up. The cluster's primary ingress controller continues to serve other Hosts on the same load-balancer IP, and any request to `/.well-known/acme-challenge/<token>` falls through to the default backend, which returns 404. cert-manager logs `failed to perform self check GET request ...: unexpected HTTP status: 404` and the Challenge stays `pending` until it times out. Distinct from Cause B: TCP connectivity works (the request reaches the cluster), but no ingress rule for the solver path is honored.

**Indicator:**

- [Step 5] solver Ingress is present (`acme.cert-manager.io/http01-solver=true`) but its `ingressClassName` is empty or names an IngressClass that does not exist in `kubectl get ingressclass`
- [Step 5] `curl -v http://<domain>/.well-known/acme-challenge/<token>` returns `HTTP/1.1 404 Not Found` from the cluster ingress controller
<!-- match: {"step": 5, "predicate": "contains", "target": "404 Not Found"} -->
- [Step 3] controller log contains `unexpected HTTP status: 404`
<!-- match: {"step": 3, "predicate": "contains", "target": "unexpected HTTP status: 404"} -->

**Mitigation:**

- **Risk:** Editing the live (Cluster)Issuer triggers re-evaluation of every Challenge that references it; in-flight Challenges may move from `pending` to `errored` and have to be retried.
- **Command:**

  ```bash
  # Discover the actual ingress class:
  kubectl get ingressclass -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.controller}{"\n"}{end}'
  ```

- **Duration:** Minutes — diagnostic only.

**Resolution:**

```bash
kubectl patch clusterissuer <issuer-name> --type=json \
  -p='[{"op":"replace","path":"/spec/acme/solvers/0/http01/ingress/ingressClassName","value":"nginx"}]'

# Optionally, for ingress controllers that handle multiple resources on one host,
# let cert-manager edit the existing user Ingress instead of creating a new one:
kubectl annotate ingress -n <ns> <app-ingress> \
  acme.cert-manager.io/http01-edit-in-place="true" --overwrite

# Trigger a fresh attempt:
cmctl renew -n <ns> <cert-name>
```

**Impact:** Cluster-wide for `ClusterIssuer` — every HTTP-01-using Certificate now creates solver Ingresses under the new class. Confirm the named class is actually the in-cluster default before editing.

**Rollback:** `kubectl apply` the prior (Cluster)Issuer manifest from git, or `kubectl patch` back to the previous `ingressClassName`.

**Verification:** Newly created solver Ingresses show `kubectl get ingress -n <ns> -l acme.cert-manager.io/http01-solver=true -o wide` with a non-empty `ADDRESS` and the corrected class; the curl from Step 5 returns 200; the Challenge moves to `valid`.

### Cause D: DNS A/CNAME records point away from the cluster (CDN, proxy, or stale record)

**Statement:** The domain's public A or CNAME records resolve to an address that is not the cluster's ingress load balancer — typically a CDN edge or a previous environment — so HTTP-01 validation reaches that intermediate instead of the cluster and never sees the challenge token.

**Mechanism:** Let's Encrypt resolves the identifier on the public internet and connects to whichever address authoritative DNS returns; when a CDN fronts the domain in proxy mode, the CDN terminates the connection and serves its own 404/503, and when the A record points at a pre-migration IP, the request reaches an unrelated server. cert-manager's in-cluster self check often still passes because in-cluster DNS resolves to the Service IP, masking the public-side failure. The Challenge ends up `invalid` with `acme: authorization error for <domain>: 403 urn:ietf:params:acme:error:unauthorized :: Invalid response from http://<domain>/.well-known/acme-challenge/<token>`.

**Indicator:**

- [Step 3] controller log contains `urn:ietf:params:acme:error:unauthorized :: Invalid response from http://`
<!-- match: {"step": 3, "predicate": "contains", "target": "urn:ietf:params:acme:error:unauthorized"} -->
- [Step 5] `dig +short A <domain>` returns an address that is not the cluster ingress external IP
- [Step 5] `curl -sI http://<domain>/.well-known/acme-challenge/test` returns a `server:` header that does not match the cluster's ingress controller (e.g., `server: cloudflare`, `server: AmazonS3`)

**Mitigation:**

- **Risk:** Switching to the DNS-01 solver removes the dependency on public HTTP reachability but requires the DNS provider to support cert-manager (and credentials with permission to write `_acme-challenge` TXT records); wildcard certificates require DNS-01 anyway.
- **Command:**

  ```bash
  kubectl patch certificate -n <ns> <cert-name> --type merge -p \
    '{"spec":{"issuerRef":{"name":"<dns01-clusterissuer>","kind":"ClusterIssuer"}}}'
  ```

- **Duration:** Until the DNS routing is corrected; consider keeping DNS-01 as the long-term solver.

**Resolution:**

```bash
# 1) Point the domain at the cluster ingress (or, on CDN platforms, disable proxy / set the edge to DNS-only mode for the apex/sub-hostname).
# For Cloudflare: set the orange cloud to grey (DNS-only) on the relevant record, or move to DNS-01.
# For Route 53 alias records: set Alias Target to the cluster's NLB/ALB hostname.

# 2) Confirm propagation, then renew.
dig +short A <domain> @1.1.1.1
dig +short A <domain> @8.8.8.8
cmctl renew -n <ns> <cert-name>
```

**Impact:** Disabling CDN proxy mode exposes the origin to direct internet traffic; pair with origin-only access controls (mTLS at the edge, IP allowlists) before the change. The DNS record update is global once propagated.

**Rollback:** Re-enable the CDN proxy / restore the prior A record. Switch the Certificate back to the HTTP-01 issuer (`kubectl apply` the prior manifest).

**Verification:** `dig +short A <domain> @8.8.8.8` returns the cluster ingress external IP; `curl -sI http://<domain>/.well-known/acme-challenge/test` returns the cluster's ingress controller `server:` header; a fresh Challenge reaches `valid` within 2 minutes.

### Cause E: DNS-01 challenge fails — provider credentials, zone, or propagation

**Statement:** cert-manager either could not create the `_acme-challenge.<domain>` TXT record (DNS-provider authentication or zone-id failure) or the record was created but is not visible to Let's Encrypt's recursive resolvers within the propagation-check timeout.

**Mechanism:** Failure splits into three layers: credential failure where the provider API returns `403 AccessDenied`/`InvalidSignatureException` and cert-manager logs `failed to create TXT record`; wrong-zone failure where credentials are valid but `hostedZoneID` (Route53) or SOA-based zone discovery routes the record to a zone that doesn't serve the domain; propagation failure where the record is published but the configured recursive resolvers still return NXDOMAIN/REFUSED past the propagation-check timeout. Stale NS records or split-horizon DNS commonly trigger the propagation variant.

**Indicator:**

- [Step 6] `status.presented` is `false` or `status.reason` contains `Failed to create TXT record` / `Access Denied` / `Unauthorized`
- [Step 3] controller log contains `propagation check failed` or `NS <ns> returned REFUSED`
<!-- match: {"step": 3, "predicate": "contains", "target": "propagation check failed"} -->
- [Step 6] `dig +short TXT _acme-challenge.<domain> @8.8.8.8` returns empty when `dig +short TXT _acme-challenge.<domain>` (authoritative) returns the published value
- [Step 6] `dig +short TXT _acme-challenge.<domain>` is empty against every public resolver and `status.presented` claims `true`

**Mitigation:**

- **Risk:** Forcing cert-manager to use specific recursive nameservers (`--dns01-recursive-nameservers-only --dns01-recursive-nameservers=8.8.8.8:53,1.1.1.1:53`) makes propagation checks consistent but bypasses any internal/split-horizon DNS the cluster relies on; if your hosted zone is private, this will hide the published record from the controller.
- **Command:**

  ```bash
  kubectl patch deployment cert-manager -n cert-manager --type=json -p='[
    {"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--dns01-recursive-nameservers=8.8.8.8:53,1.1.1.1:53"},
    {"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--dns01-recursive-nameservers-only"}
  ]'
  kubectl rollout status -n cert-manager deploy/cert-manager
  ```

- **Duration:** Permanent unless the cluster requires split-horizon DNS — keep these flags.

**Resolution:**

```bash
# 1) Fix DNS-provider credentials.
# Route53 example: the IAM policy must include route53:GetChange, route53:ChangeResourceRecordSets, route53:ListHostedZonesByName.
aws iam get-role-policy --role-name <cert-manager-role> --policy-name cert-manager-dns01
kubectl create secret generic <dns-provider-secret> -n cert-manager \
  --from-literal=secret-access-key='<new-secret>' --dry-run=client -o yaml | kubectl apply -f -

# 2) Pin the zone-id in the Issuer to avoid SOA discovery surprises.
kubectl patch clusterissuer <issuer-name> --type=json -p='[
  {"op":"replace","path":"/spec/acme/solvers/0/dns01/route53/hostedZoneID","value":"<Z123EXAMPLE>"}
]'

# 3) Verify the credential and zone are correct end-to-end, then renew.
cmctl renew -n <ns> <cert-name>
```

**Impact:** A bad IAM-policy change can lock cert-manager out of writing any TXT record cluster-wide — every DNS-01 Certificate will start to fail on its next renewal. Stage the policy change against a single staging Certificate before applying to production issuers.

**Rollback:** Revert the credential Secret to the prior value (`kubectl apply -f <previous-secret>.yaml`); `kubectl apply` the prior (Cluster)Issuer manifest to restore the previous `hostedZoneID`.

**Verification:** `dig +short TXT _acme-challenge.<domain> @8.8.8.8` returns the value cert-manager published; the Challenge transitions to `valid` within the configured propagation-check window; controller logs no longer contain `propagation check failed` or provider auth errors over a 10-minute window.

### Cause F: Let's Encrypt rate limit — Certificates per Registered Domain or Duplicate Certificate

**Statement:** Let's Encrypt rejects the new-order request with HTTP 503 and `urn:ietf:params:acme:error:rateLimited` because the registered domain has issued more than 50 certificates in the trailing 7 days, or because the same exact set of identifiers has been issued more than 5 times in 7 days.

**Mechanism:** Let's Encrypt enforces two domain-keyed weekly limits at the new-order endpoint — 50 certificates per Registered Domain (eTLD+1) per 7 days, and 5 Duplicate Certificates for the exact same identifier set per 7 days. Over-limit requests return HTTP 503 with `Retry-After` and an ACME problem document carrying `urn:ietf:params:acme:error:rateLimited :: Error creating new order :: too many certificates already issued`. cert-manager backs off and retries, but the underlying Order stays `errored` until the trailing 7-day window clears; the dominant trigger is a CI loop that recreates the Certificate on every deploy.

**Indicator:**

- [Step 3] controller log contains `urn:ietf:params:acme:error:rateLimited`
<!-- match: {"step": 3, "predicate": "contains", "target": "urn:ietf:params:acme:error:rateLimited"} -->
- [Step 3] controller log contains `too many certificates already issued`
<!-- match: {"step": 3, "predicate": "contains", "target": "too many certificates already issued"} -->
- [Step 7] CT-log query against `https://crt.sh/?q=<registered-domain>` returns more than 50 certs in the last 7 days (per-RegisteredDomain limit) or more than 5 for an identical SAN set (duplicate limit)

**Mitigation:**

- **Risk:** Switching the Certificate to the staging issuer produces an untrusted chain; any client without staging-CA trust will fail TLS verification. Use only for the duration of the rate-limit window and only on non-production hostnames.
- **Command:**

  ```bash
  kubectl patch certificate -n <ns> <cert-name> --type merge -p \
    '{"spec":{"issuerRef":{"name":"<letsencrypt-staging-issuer>","kind":"ClusterIssuer"}}}'
  ```

- **Duration:** Up to 7 days, until the production rate-limit window clears.

**Resolution:**

```bash
# 1) Stop the loop that's burning the budget — identify what's recreating the Certificate.
kubectl get events -A --field-selector involvedObject.kind=Certificate \
  --sort-by=.lastTimestamp | tail -20
# Common cause: a Helm chart or Kustomize overlay that deletes-and-recreates Certificates per deploy. Pin Certificates outside the per-deploy bundle.

# 2) Wait for the window to clear, then re-issue from production. The exact reset time
# appears in Retry-After; conservatively wait 7 days from the earliest of the over-budget issuances.
date -d '7 days ago' -Iseconds  # window cutoff
curl -s "https://crt.sh/?q=<registered-domain>&output=json" \
  | jq '[.[] | select(.entry_timestamp > "'$(date -d '7 days ago' -Iseconds)'")] | length'

# 3) For sustained high volume, request a rate-limit override at https://isrg.formstack.com/forms/rate_limit_adjustment_request.
```

**Resolution:** Same as Mitigation.

**Impact:** Pausing issuance is namespace-scoped; switching to staging affects every TLS client of the workload. Rate-limit override requests take weeks and are evaluated case-by-case — do not rely on them for incident response.

**Rollback:** `kubectl patch certificate -n <ns> <cert-name> --type merge -p '{"spec":{"issuerRef":{"name":"<letsencrypt-prod-issuer>","kind":"ClusterIssuer"}}}'` once the window clears.

**Verification:** A test Certificate against the production issuer transitions to `Ready=True` without `rateLimited` in the controller log; the rolling 7-day CT-log count drops below 50 for the registered domain.

### Cause G: Let's Encrypt rate limit — too many failed authorizations recently

**Statement:** Let's Encrypt is rejecting new authorization attempts because the account has exceeded 5 failed validation attempts per identifier per hour, so even a fixed configuration cannot proceed until the hourly window clears.

**Mechanism:** Let's Encrypt tracks the failed-validation counter per `(account, identifier)` tuple. Each `invalid` Challenge increments it; the limit is 5 per hour with refill of 1 per 12 minutes. Once tripped, every new Order for that identifier on the same account is rejected at the new-authorization step with `urn:ietf:params:acme:error:rateLimited :: Error finalizing order :: too many failed authorizations recently: see https://letsencrypt.org/docs/rate-limits/`. The trap is that fixing the underlying issue (Causes B-E) does not immediately help — the account is locked out of that identifier for up to an hour. A separate, harsher limit caps consecutive authorization failures per identifier at 1,152 across all accounts.

**Indicator:**

- [Step 3] controller log contains `too many failed authorizations recently`
<!-- match: {"step": 3, "predicate": "contains", "target": "too many failed authorizations recently"} -->
- [Step 3] controller log contains `urn:ietf:params:acme:error:rateLimited` together with at least 5 `invalid` Challenges for the same `dnsNames` in the last hour
- [Step 7] `kubectl get challenges -A` shows 5+ `invalid` entries for the same identifier within a 60-minute window

**Mitigation:**

- **Risk:** Switching to the staging server lets the underlying fix (sidecar injection, DNS credentials, ingress class) be validated against a fresh authorization counter — but staging certs are untrusted. Keep the swap until 1 hour after the last failed validation, then flip back.
- **Command:**

  ```bash
  kubectl patch certificate -n <ns> <cert-name> --type merge -p \
    '{"spec":{"issuerRef":{"name":"<letsencrypt-staging-issuer>","kind":"ClusterIssuer"}}}'
  ```

- **Duration:** 1-2 hours until the failed-authorization counter refills.

**Resolution:**

```bash
# 1) Fix the underlying validation failure (Cause B, C, D, or E) before retrying production.
# 2) Pause the Certificate so cert-manager stops retrying and burning the counter further.
kubectl annotate certificate -n <ns> <cert-name> \
  cert-manager.io/issue-temporary-certificate="true" --overwrite

# 3) Wait at least 60 minutes from the last invalid Challenge, then renew once.
date
kubectl get challenge -A --sort-by=.metadata.creationTimestamp \
  | grep invalid | tail -5
sleep 3600
cmctl renew -n <ns> <cert-name>
```

**Impact:** The pause/temporary-certificate annotation is per-Certificate; other Certificates in the namespace continue to renew. Repeatedly retrying without fixing the root cause re-trips the limit and extends the lockout window.

**Rollback:** `kubectl annotate certificate -n <ns> <cert-name> cert-manager.io/issue-temporary-certificate-` to remove the annotation and resume normal issuance.

**Verification:** After the hourly window passes and the underlying cause is fixed, the next Order's Challenge transitions to `valid`; the controller log no longer contains `too many failed authorizations recently`; CertificateRequest `READY=True`.

### Cause H: cert-manager controller, webhook, or cainjector pod is unhealthy

**Statement:** A cert-manager control-plane component (`cert-manager`, `cert-manager-webhook`, or `cert-manager-cainjector`) is unhealthy, halting reconciliation cluster-wide or rejecting every `kubectl apply` of a cert-manager CRD with a webhook error.

**Mechanism:** `cert-manager` reconciles issuance, `cert-manager-webhook` admits CRD writes via Validating/Mutating webhooks, and `cert-manager-cainjector` injects CA bundles into webhook configs and CRDs. A down controller silently halts reconciliation while applies still succeed; a down webhook fails every apply with `failed calling webhook "webhook.cert-manager.io": ... connection refused` / `i/o timeout` / `x509: certificate signed by unknown authority`. Common triggers: webhook Pod evicted/OOMKilled, private-cluster firewall blocks the webhook port, or cainjector lost leader election during upgrade and stopped re-injecting the CA bundle.

**Indicator:**

- [Step 8] `kubectl get pods -n cert-manager` shows one of the cert-manager Pods in `CrashLoopBackOff`, `Pending`, `OOMKilled`, or not Ready
- [Step 8] `kubectl get endpoints -n cert-manager cert-manager-webhook` returns no `<ip>:<port>` pair
<!-- match: {"step": 8, "predicate": "contains", "target": "<none>"} -->
- [Step 8] `kubectl apply -f <cert-manifest>.yaml` returns `failed calling webhook "webhook.cert-manager.io"`
<!-- match: {"step": 8, "predicate": "contains", "target": "failed calling webhook \"webhook.cert-manager.io\""} -->
- [Step 8] `kubectl apply` returns `x509: certificate signed by unknown authority`
<!-- match: {"step": 8, "predicate": "contains", "target": "x509: certificate signed by unknown authority"} -->

**Mitigation:**

- **Risk:** Scaling up `cert-manager-webhook` and `cert-manager` to multiple replicas is generally safe (both support leader election), but on resource-tight clusters the new pods may sit Pending and the issue persists.
- **Command:**

  ```bash
  kubectl scale deploy -n cert-manager cert-manager-webhook --replicas=2
  kubectl scale deploy -n cert-manager cert-manager --replicas=2
  kubectl rollout status -n cert-manager deploy/cert-manager-webhook
  ```

- **Duration:** Hours, until the persistent fix lands.

**Resolution:**

```bash
# 1) If OOMKilled, raise memory limits proportional to the Certificate count in the cluster.
kubectl set resources -n cert-manager deploy/cert-manager \
  --limits=cpu=500m,memory=512Mi --requests=cpu=100m,memory=256Mi
kubectl set resources -n cert-manager deploy/cert-manager-webhook \
  --limits=cpu=200m,memory=256Mi --requests=cpu=50m,memory=64Mi

# 2) If the webhook is unreachable in a private cluster, open the webhook port from the API server's pod-range.
# GKE private cluster: master-authorized-networks and firewall rule for TCP/10250 (or webhook.securePort).
# EKS with custom CNI: set hostNetwork=true and pick a free securePort.
helm upgrade cert-manager jetstack/cert-manager -n cert-manager \
  --reuse-values --set webhook.hostNetwork=true --set webhook.securePort=10260

# 3) If x509 chain is broken, force cainjector to re-inject.
kubectl rollout restart -n cert-manager deploy/cert-manager-cainjector
kubectl rollout restart -n cert-manager deploy/cert-manager-webhook
```

**Impact:** Cluster-wide for the cert-manager control plane. Restarting the webhook briefly returns `failed calling webhook` for any Certificate apply during the rollout; existing certificates continue to serve from their stored Secrets, so workloads are unaffected for the rollout duration.

**Rollback:** `kubectl rollout undo deploy/cert-manager`, `kubectl rollout undo deploy/cert-manager-webhook`, `kubectl rollout undo deploy/cert-manager-cainjector` (in that order). For Helm changes, `helm rollback cert-manager <prev-revision>`.

**Verification:** `kubectl get pods -n cert-manager` shows all three Deployments at the desired replica count with `RESTARTS=0` for at least 10 minutes; `kubectl get endpoints -n cert-manager cert-manager-webhook` lists ready endpoints; `kubectl apply` of a test Certificate succeeds without webhook errors and the Certificate progresses to `Ready=True`.

### Cause I: ClusterIssuer/Issuer points at the wrong ACME server or solver scope is too narrow

**Statement:** The issuer references the staging ACME server while production trust is required (or vice versa), or its `solvers[].selector` excludes the dnsName/namespace of the failing Certificate, so cert-manager has no solver to use and stalls before creating an Order.

**Mechanism:** `spec.acme.solvers` is an ordered list of `{selector, http01|dns01}` entries; cert-manager picks the first solver whose selector matches the Certificate's namespace labels, dnsNames, or matchLabels. When no selector matches, the Certificate stays `Ready: False` with `Reason: ConfigError` and Message `no configured challenge solvers can be used for this challenge`. When the server URL is wrong (a `ClusterIssuer` named `letsencrypt-prod` that actually points at `acme-staging-v02`), issuance succeeds but the returned chain is untrusted and Ingress controllers/clients hit `x509: certificate signed by unknown authority`. Both manifest as Certificates that look "fine" at the resource level but are operationally broken.

**Indicator:**

- [Step 4] `spec.acme.server` does not match the intended environment (staging URL in production issuer or vice versa)
- [Step 2] CertificateRequest / Order condition contains `no configured challenge solvers can be used`
<!-- match: {"step": 2, "predicate": "contains", "target": "no configured challenge solvers can be used"} -->
- [Step 9] `openssl x509 ... -issuer` prints `O = (STAGING) Internet Security Research Group, CN = (STAGING) Pretend Pear X1` when production was intended
<!-- match: {"step": 9, "predicate": "contains", "target": "(STAGING) Pretend Pear X1"} -->
- [Step 2] Certificate Condition Message contains `no configured challenge solvers can be used for this challenge`

**Mitigation:**

- **Risk:** Patching the issuer server URL re-registers the ACME account against the new endpoint on next reconcile; this consumes one of the per-IP account-registration quotas (10 per 3 hours), so don't flip back and forth.
- **Command:**

  ```bash
  kubectl get clusterissuer -o custom-columns=NAME:.metadata.name,SERVER:.spec.acme.server,READY:.status.conditions[0].status
  ```

- **Duration:** Diagnostic only.

**Resolution:**

```bash
# 1) Point the issuer at the right ACME server and a fresh account-key Secret.
kubectl patch clusterissuer <issuer-name> --type merge -p \
  '{"spec":{"acme":{"server":"https://acme-v02.api.letsencrypt.org/directory","privateKeySecretRef":{"name":"letsencrypt-prod-account-key"}}}}'

# 2) Widen the solver selector so it actually matches the failing Certificate.
kubectl apply -f - <<'EOF'
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: <issuer-name>
spec:
  acme:
    server: https://acme-v02.api.letsencrypt.org/directory
    email: <sre-alerts>@<your-domain>
    privateKeySecretRef:
      name: letsencrypt-prod-account-key
    solvers:
      - http01:
          ingress:
            ingressClassName: nginx
      - selector:
          dnsZones:
            - <your-domain>
        dns01:
          route53:
            region: us-east-1
            hostedZoneID: <Z123EXAMPLE>
EOF

# 3) Re-issue affected Certificates.
cmctl renew -n <ns> <cert-name>
```

**Impact:** Cluster-wide for `ClusterIssuer`. Re-registering the ACME account creates a new account at the production endpoint — the prior staging account becomes orphaned (no data exposure, but counts against the per-IP new-registration cap).

**Rollback:** `kubectl apply` the prior `ClusterIssuer` manifest from git, restoring both the server URL and the original `privateKeySecretRef`.

**Verification:** `kubectl get clusterissuer <issuer-name>` shows `READY=True` and `spec.acme.server` matches the intended environment; a renewed Certificate's `openssl x509 ... -issuer` prints `O = Let's Encrypt, CN = R10` (or current production intermediate); the Ingress controller no longer returns `x509: certificate signed by unknown authority`.

### Cause Z: Unidentified

**Statement:** The Certificate is not Ready but the gathered evidence does not match the Indicators for Causes A through I.

**Mechanism:** A cert-manager issuance failure was confirmed (Certificate `Ready=False` or stale Secret) but the failing layer — Certificate, CertificateRequest, Order, Challenge, Issuer, or control plane — does not surface a known ACME error URN, rate-limit string, propagation message, or webhook failure. The cause may be an upstream provider outage (Let's Encrypt incident at https://letsencrypt.status.io/), a CRD schema mismatch after a partial upgrade, a CNI/DNS regression that intermittently breaks self-checks, or a multi-tenant network policy that blocks the solver Pod from the Service Mesh.

**Indicator:**

- [Default] Certificate is `Ready=False` (Step 1) but Causes A through I Indicators do not match the gathered evidence

**Mitigation:**

- **Risk:** Bumping cert-manager log verbosity to `--v=6` is read-only but generates substantial output; on a busy cluster (>500 Certificates) it can fill node stdout buffers within minutes — leave on only long enough to capture one full reconcile cycle.
- **Command:**

  ```bash
  kubectl patch deployment cert-manager -n cert-manager --type=json -p='[
    {"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--v=6"}
  ]'
  kubectl rollout status -n cert-manager deploy/cert-manager
  # Capture artefacts:
  kubectl logs -n cert-manager -l app.kubernetes.io/name=cert-manager --tail=2000 > /tmp/cert-manager.log
  cmctl status certificate -n <ns> <cert-name> > /tmp/cmctl-status.txt
  kubectl get certificate,certificaterequest,order,challenge -n <ns> -o yaml > /tmp/cert-manager-chain.yaml
  kubectl get clusterissuer,issuer -A -o yaml > /tmp/cert-manager-issuers.yaml
  curl -s https://letsencrypt.status.io/api/v2/summary.json > /tmp/letsencrypt-status.json
  ```

- **Duration:** Minutes. Remove `--v=6` once the capture is complete.

**Resolution:** Out of runbook scope. Package the captured artefacts, the affected Certificate name, the operator-observed timestamps, and the Let's Encrypt status snapshot; escalate to the platform/security on-call or open a cert-manager issue at https://github.com/cert-manager/cert-manager/issues.

**Verification:** Hand-off acknowledged; an incident ticket is opened with the captured artefacts attached and a follow-up owner assigned.

## Prevention

- Use `ClusterIssuer` (not per-namespace `Issuer`) for shared Let's Encrypt configuration so the ACME server URL, account email, and solver definitions live in one auditable place; pin the server URL and `privateKeySecretRef` in git.
- Always validate new issuer or solver configuration against `https://acme-staging-v02.api.letsencrypt.org/directory` first; only promote to production once a staging Certificate reaches `Ready=True` end-to-end. Staging has 30,000-cert weekly headroom versus 50 in production.
- Keep Certificate resources out of per-deploy Helm releases or Kustomize overlays — wrap them in a long-lived bundle so CI does not delete-and-recreate the resource on every push (the dominant trigger for the Duplicate-Certificate rate-limit).
- For DNS-01, set the controller flags `--dns01-recursive-nameservers-only --dns01-recursive-nameservers=8.8.8.8:53,1.1.1.1:53` so propagation checks see the same view Let's Encrypt does (skip on clusters that rely on split-horizon DNS for `_acme-challenge.*`).
- Scope DNS-provider IAM to the specific hosted zone; for Route53 use a permissions boundary that allows `route53:GetChange`, `route53:ChangeResourceRecordSets`, and `route53:ListHostedZonesByName` only against the target zone-id.
- Run cert-manager with at least two `cert-manager-webhook` replicas (`replicaCount: 2` in the Helm values) on different nodes so a single eviction does not cause `failed calling webhook` errors during issuance.
- Alert on `certmanager_certificate_ready_status{condition="True"} == 0` per Certificate, sustained for 15 minutes; alert on `certmanager_certificate_expiration_timestamp_seconds - time() < 1209600` (14 days) for renewal-stall detection. Both metrics are exposed on the controller pod's `:9402/metrics`.
- Alert on `rate(certmanager_acme_client_request_count{status=~"4..|5.."}[5m]) > 0` to catch ACME-error bursts before they exhaust the failed-authorization budget.
- Pre-create production Certificates well before go-live; per-Registered-Domain rate limits are first-come-first-served, and reserving issuance early avoids competing with a launch-day burst.
- Subscribe an on-call rotation to https://letsencrypt.status.io/ and route the alerts to the same channel as cert-manager renewal failures, so provider-side incidents are immediately disambiguated from cluster-side regressions.
- Run `cmctl check api` and `cmctl status certificate` in a scheduled CI job (e.g., daily) and surface failures as a non-page warning; the cmctl output catches webhook x509 chain drift and stalled Certificates before they cause a service outage.

## Sources

- [cert-manager Troubleshooting](https://cert-manager.io/docs/troubleshooting/) — Priority 1. Diagnostic resource chain (Certificate → CertificateRequest → Order → Challenge), `kubectl describe` over controller logs, common Condition Messages including `Issuing certificate as Secret does not exist` and `Waiting on certificate issuance from order ... pending`.
- [cert-manager ACME Troubleshooting](https://cert-manager.io/docs/troubleshooting/acme/) — Priority 1. HTTP-01 self-check failures (`failed to perform self check GET request`), DNS-01 propagation/SOA issues, `cert-manager.io/issue-temporary-certificate` and `acme.cert-manager.io/http01-edit-in-place` annotations, exact ACME error URNs including `urn:ietf:params:acme:error:invalidEmail`.
- [cert-manager Webhook Troubleshooting](https://cert-manager.io/docs/troubleshooting/webhook/) — Priority 1. Webhook failure modes (`failed calling webhook ... connection refused`, `i/o timeout`, `x509: certificate signed by unknown authority`, `no endpoints available for service`), GKE/EKS private-cluster firewall requirements, cainjector failure surface.
- [cert-manager ACME Configuration](https://cert-manager.io/docs/configuration/acme/) — Priority 1. Issuer fields (`email`, `server`, `privateKeySecretRef`, `solvers`), HTTP-01 (`ingressClassName`) and DNS-01 (Route53/Cloudflare/Google/Akamai/RFC2136/Webhook) solver shapes, External Account Binding, profile selection.
- [Let's Encrypt Rate Limits](https://letsencrypt.org/docs/rate-limits/) — Priority 1. Exact thresholds: 50 certs per Registered Domain per 7 days, 5 Duplicate Certificates per 7 days, 5 Failed Validations per identifier per hour, 300 New Orders per 3 hours, 10 new accounts per IP per 3 hours; HTTP 503 + `Retry-After` response signal; staging environment as remediation.
- [Let's Encrypt Challenge Types](https://letsencrypt.org/docs/challenge-types/) — Priority 1. HTTP-01 mechanics (port 80, `/.well-known/acme-challenge/<TOKEN>`, 10-redirect follow), DNS-01 mechanics (`_acme-challenge.<domain>` TXT, ~1h propagation), TLS-ALPN-01 surface, wildcard restriction to DNS-01.
