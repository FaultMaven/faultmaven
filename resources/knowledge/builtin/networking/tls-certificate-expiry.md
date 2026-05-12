---
id: "tls-certificate-expiry"
title: "TLS Certificate Expiry Causing Service Outage"
domain: networking
service: general
symptom_class: [auth_failure, service_unavailable]
severity: critical
scope: global
version: "1.0.0"
last_updated: "2026-05-12"
verified_by: "kb-researcher"
status: draft
tags: [tls, ssl, certificates, x509, expiry, https, openssl, certbot, cert-manager, acme, lets-encrypt]
difficulty: intermediate
---

## Symptom Recognition

- `curl: (60) SSL certificate problem: certificate has expired` on any HTTPS endpoint
- Browser errors: `NET::ERR_CERT_DATE_INVALID`, `SEC_ERROR_EXPIRED_CERTIFICATE`, `SSL_ERROR_EXPIRED_CERT_ALERT`
- Application logs: `x509: certificate has expired or is not yet valid`, `TLS handshake error: remote error: tls: expired certificate`
- Java applications: `javax.net.ssl.SSLHandshakeException: PKIX path validation failed: CERT_NOT_YET_VALID` or `CERT_EXPIRED`
- Kubernetes Ingress returning 502 or refusing TLS connections; `kubectl get certificates -A` shows `Ready=False`
- `openssl s_client` verify return code is non-zero (e.g., `Verify return code: 10 (certificate has expired)`)
- Service-mesh mTLS failures logged as `certificate verify failed` when either the client or server leaf cert has passed `notAfter`
- Monitoring alerts: `TLSCertificateExpiringSoon` or `probe_ssl_earliest_cert_expiry` metric below threshold

## Applicability

Applies to any service serving or validating X.509 TLS certificates: NGINX, Apache, HAProxy, Kubernetes Ingress controllers, AWS ALB/NLB, Istio/Envoy mTLS, and API gateways. Covers certificates from any CA: Let's Encrypt (90-day validity), commercial CAs (1-year validity), and private/internal CAs. Requires `openssl` CLI access and shell or `kubectl` access to the certificate-serving host or cluster. Emergency renewal requires credentials for the certificate authority or ACME client (certbot, cert-manager, ACM).

## Diagnostic Steps

### Step 1: Check certificate expiry on the live endpoint

```bash
openssl s_client -connect <host>:443 -servername <host> </dev/null 2>/dev/null | \
  openssl x509 -noout -dates
```

Expected output: `notBefore` in the past and `notAfter` in the future. If `notAfter` is in the past, the certificate is expired. To quantify:

```bash
openssl x509 -in /path/to/cert.pem -noout -checkend 86400
echo "Exit code: $?"
```

Exit code 0 = valid for at least 24 h. Exit code 1 = expired or expiring within 24 h.

### Step 2: Inspect the full certificate chain for expired intermediates

```bash
openssl s_client -connect <host>:443 -servername <host> -showcerts </dev/null 2>/dev/null | \
  awk '/BEGIN CERT/,/END CERT/{print}' | \
  csplit -z -f cert- - '/BEGIN CERT/' '{*}' 2>/dev/null
for f in cert-*; do
  echo "=== $f ==="
  openssl x509 -in "$f" -noout -subject -issuer -dates
done
rm -f cert-*
```

Expected output: each intermediate shows `notAfter` in the future. An expired intermediate (e.g., `DST Root CA X3`) will show a past `notAfter` and is the chain root cause.

### Step 3: Confirm the certificate covers the requested hostname

```bash
openssl s_client -connect <host>:443 -servername <host> </dev/null 2>/dev/null | \
  openssl x509 -noout -subject -ext subjectAltName
```

Expected output: the target hostname appears in the `subjectAltName` list. Absence here indicates a hostname-scope mismatch rather than expiry.

### Step 4: Check system clock for NTP skew

```bash
date -u
timedatectl status
chronyc tracking 2>/dev/null || ntpq -p 2>/dev/null
```

Expected output: `timedatectl` shows `NTP synchronized: yes` and system time matches UTC within a few minutes. `System time offset` reported by `chronyc tracking` should be under 1 second.

### Step 5: Check automated renewal status

For certbot:

```bash
systemctl status certbot.timer
certbot certificates
certbot renew --dry-run
```

Expected output: `certbot.timer` is `active (waiting)` and `certbot certificates` shows a non-expired `VALID` date. If `--dry-run` fails, the ACME challenge path is broken.

For cert-manager:

```bash
kubectl get certificates -A -o wide
kubectl describe certificate <cert-name> -n <namespace>
kubectl get orders,challenges -A
kubectl logs -n cert-manager deploy/cert-manager --tail=100 | grep -iE "error|fail|renew"
```

Expected output: `READY=True` for all Certificate resources. Any `READY=False` row identifies the failing certificate; `describe` shows the reason.

For AWS ACM:

```bash
aws acm list-certificates \
  --query 'CertificateSummaryList[*].[DomainName,Status,NotAfter]' \
  --output table
aws acm describe-certificate --certificate-arn <arn> \
  --query 'Certificate.[Status,NotAfter,RenewalSummary]'
```

Expected output: `Status=ISSUED` and `RenewalStatus=SUCCESS`. If `RenewalStatus=FAILED`, DNS validation records were removed.

### Step 6: Compare on-disk certificate against the live endpoint

```bash
echo "=== On disk ==="
openssl x509 -in /etc/letsencrypt/live/<domain>/fullchain.pem -noout -serial -dates 2>/dev/null

echo "=== Live endpoint ==="
openssl s_client -connect <host>:443 -servername <host> </dev/null 2>/dev/null | \
  openssl x509 -noout -serial -dates
```

Expected output: serial numbers match and both show the same future `notAfter`. Differing serials with the on-disk cert valid and the live cert expired means the web server was never reloaded after renewal.

For Kubernetes:

```bash
kubectl get secret <tls-secret> -n <namespace> \
  -o jsonpath='{.data.tls\.crt}' | base64 -d | openssl x509 -noout -serial -dates
```

### Step 7: Verify ACME challenge connectivity

```bash
# HTTP-01: port 80 must be reachable from the internet
curl -sv http://<host>/.well-known/acme-challenge/test 2>&1 | head -20

# Firewall rules
iptables -L INPUT -n | grep -E '(80|443)'
ss -tlnp | grep -E ':(80|443)\s'

# DNS-01: TXT record propagation
dig TXT _acme-challenge.<domain> @8.8.8.8
```

Expected output: HTTP-01 path returns any response (even 404) without connection refusal. DNS-01 shows the TXT record in the `ANSWER SECTION`. Any `Connection refused` on port 80 or missing TXT record blocks ACME validation.

## Causes

### Cause A: Leaf certificate passed its notAfter date and was not renewed

**Statement:** The X.509 leaf certificate served by the endpoint has passed its `notAfter` validity date, causing all TLS clients to reject the connection.

**Mechanism:** Every TLS client validates `notAfter` against the current time during the handshake. When `notAfter` is in the past the client terminates the connection immediately, before any application data is exchanged. No TLS session is established, so the service is completely unreachable over HTTPS.

**Indicator:**

- [Step 1] `notAfter` date is in the past
- [Symptom] `certificate has expired` in client or server logs
<!-- match: {"step": 1, "predicate": "contains", "target": "notAfter"} -->

**Mitigation:**

- **Risk:** Certbot reuses the existing ACME account and domain config; if deploy hooks are configured the web server reloads automatically. Rate-limited if multiple forced renewals were recently attempted.
- **Command:**

  ```bash
  certbot renew --force-renewal
  # Or a specific domain
  certbot certonly --force-renewal -d <domain>
  systemctl reload nginx
  ```

- **Duration:** 1–5 minutes. ACME validation typically completes in under 60 seconds.

**Resolution:**

```bash
# Enable the certbot timer so renewals run automatically at 30-day threshold
systemctl enable --now certbot.timer
# Add a deploy hook to reload the web server after every future renewal
cat > /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh << 'HOOK'
#!/bin/bash
nginx -t && systemctl reload nginx
HOOK
chmod +x /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
certbot renew --dry-run
```

- **Impact:** Cluster-wide / host-wide. `systemctl enable certbot.timer` affects the system timer for all configured domains on this host.
- **Rollback:** `systemctl disable --now certbot.timer` to revert; remove the deploy hook script to undo the hook.

**Verification:** Run `openssl s_client -connect <host>:443 -servername <host> </dev/null 2>/dev/null | openssl x509 -noout -dates` and confirm `notAfter` is approximately 90 days in the future. `certbot renew --dry-run` should succeed without errors.

---

### Cause B: Renewed certificate exists on disk but web server was never reloaded

**Statement:** The automated renewal succeeded and wrote a new certificate to disk, but the web server process was not sent a reload signal and continues serving the old expired certificate from memory.

**Mechanism:** Web servers (NGINX, HAProxy, Apache) cache the TLS certificate in memory at startup or reload time. When certbot writes a new certificate to `/etc/letsencrypt/live/`, the running process is unaware until it re-reads the file. Without a deploy hook that sends `SIGHUP` or calls `systemctl reload`, the expired certificate remains active indefinitely even though a valid replacement is on disk.

**Indicator:**

- [Step 6] serial numbers differ: on-disk cert has a future `notAfter`, live endpoint serves a past `notAfter`
<!-- match: {"step": 6, "predicate": "contains", "target": "Serial Number"} -->

**Mitigation:**

- **Risk:** Low. `nginx -t` validates config before reload. A graceful reload does not drop existing connections.
- **Command:**

  ```bash
  # NGINX
  nginx -t && systemctl reload nginx

  # HAProxy
  haproxy -c -f /etc/haproxy/haproxy.cfg && systemctl reload haproxy

  # Apache
  apachectl configtest && systemctl reload apache2
  ```

- **Duration:** Under 30 seconds.

**Resolution:**

```bash
# Add deploy hook so future renewals automatically reload the web server
cat > /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh << 'HOOK'
#!/bin/bash
nginx -t && systemctl reload nginx
HOOK
chmod +x /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
```

- **Impact:** Per-host. Only affects deployments on this host that consume Let's Encrypt certs.
- **Rollback:** Remove the deploy hook file; NGINX reload is non-destructive and self-contained.

**Verification:** Recheck serial numbers with Step 6 commands. Both on-disk and live endpoint should show the same serial and a future `notAfter`.

---

### Cause C: cert-manager Certificate resource stuck — ACME challenge failing

**Statement:** The cert-manager Certificate resource shows `Ready=False` because the ACME HTTP-01 or DNS-01 challenge is blocked by a firewall, Ingress misconfiguration, or DNS propagation failure.

**Mechanism:** cert-manager issues an Order to the ACME CA, which creates a Challenge that must be validated externally. For HTTP-01, the CA makes an HTTP request to `http://<domain>/.well-known/acme-challenge/<token>`; if port 80 is blocked or the Ingress does not route `.well-known` traffic to the cert-manager solver pod, validation fails. For DNS-01, a TXT record must propagate to the authoritative nameservers before the CA queries it; if the DNS provider API credentials are wrong or the TTL is high, validation times out.

**Indicator:**

- [Step 5] `kubectl get certificates -A` shows `READY=False`
- [Step 5] `kubectl describe certificate` shows challenge failure reason or `Waiting for DNS-01 challenge propagation`
- [Step 7] port 80 connection refused or DNS TXT record absent
<!-- match: {"step": 5, "predicate": "contains", "target": "Ready=False"} -->

**Mitigation:**

- **Risk:** Low. Deleting stale CertificateRequest and Challenge resources causes cert-manager to re-attempt issuance.
- **Command:**

  ```bash
  # Describe the failing challenge to get the error
  kubectl get challenges -A
  kubectl describe challenge <challenge-name> -n <namespace>

  # Force re-issuance by deleting the stale CertificateRequest
  kubectl delete certificaterequest -n <namespace> --all
  # cert-manager will create a new CertificateRequest automatically
  ```

- **Duration:** 2–10 minutes for ACME validation to complete after challenge is fixed.

**Resolution:**

```bash
# For HTTP-01 — ensure the Ingress allows /.well-known/acme-challenge/
kubectl describe clusterissuer <issuer-name>
# Confirm the cert-manager controller has access to create/update Ingress resources
kubectl auth can-i create ingress --as=system:serviceaccount:cert-manager:cert-manager -n <namespace>

# For DNS-01 — check the DNS provider secret is current
kubectl get secret <dns-provider-secret> -n cert-manager -o yaml
```

- **Impact:** Namespace-scoped for HTTP-01 (Ingress annotation); cluster-wide for ClusterIssuer DNS credentials.
- **Rollback:** Revert Ingress annotations or restore the previous DNS provider secret.

**Verification:** `kubectl get certificates -A -o wide` shows `READY=True` for all Certificate resources. `kubectl get orders,challenges -A` returns no pending items.

---

### Cause D: Expired intermediate certificate in the trust chain

**Statement:** The leaf certificate is valid but the intermediate CA certificate in the chain served by the endpoint has passed its `notAfter` date, causing chain validation to fail on clients that perform full-chain verification.

**Mechanism:** TLS clients walk the certificate chain from leaf to a trusted root. If any intermediate in the chain is expired, path validation terminates with a `certificate has expired` or `PKIX path validation failed` error even though the leaf certificate itself remains valid. This is distinct from leaf-cert expiry: the endpoint continues serving TLS handshakes, but all clients reject the expired intermediate. A real-world example is the DST Root CA X3 cross-signed chain that expired October 2021, breaking older client stacks.

**Indicator:**

- [Step 1] `notAfter` on leaf cert is in the future
- [Step 2] one of the intermediate certs shows a past `notAfter`
<!-- match: {"step": 2, "predicate": "contains", "target": "notAfter"} -->

**Mitigation:**

- **Risk:** Low. Updating `fullchain.pem` and reloading NGINX is non-destructive if the new intermediate is correct.
- **Command:**

  ```bash
  # Download the current intermediate from the CA
  curl -o intermediate.pem https://letsencrypt.org/certs/lets-encrypt-r3.pem
  cat /path/to/cert.pem intermediate.pem > /path/to/fullchain.pem
  nginx -t && systemctl reload nginx
  ```

- **Duration:** Under 5 minutes.

**Resolution:** **Same as Mitigation.** Ensure certbot or cert-manager is configured to always include the full chain (default behaviour for both). For cert-manager, `spec.privateKey.algorithm` does not affect chain bundling — the Certificate resource automatically fetches the chain from the ACME issuer.

**Verification:** Re-run Step 2 — all intermediates in the chain should show future `notAfter` dates. Run `openssl s_client -connect <host>:443 -servername <host> -verify_return_error </dev/null 2>&1 | grep "Verify return code"` and confirm `0 (ok)`.

---

### Cause E: System clock skew causing valid certificates to appear expired

**Statement:** The server or client system clock is significantly ahead of real UTC time, causing in-date certificates to fail `notAfter` validation because the local clock places the current time past the certificate's expiry.

**Mechanism:** TLS validity checks compare `notAfter` against the local system clock, not a network time source. If a VM or container has lost NTP synchronization — common after snapshot restore, live migration, or on embedded systems — the clock can drift hours or days ahead. A certificate valid until next year appears expired to a client running with a clock set two years in the future. Both client-side and server-side clock skew can manifest as certificate errors.

**Indicator:**

- [Step 1] `notAfter` date appears in the past when queried from the affected host, but a remote check confirms the certificate is valid
- [Step 4] `timedatectl status` shows `NTP synchronized: no` or significant offset
<!-- match: {"step": 4, "predicate": "contains", "target": "NTP synchronized: no"} -->

**Mitigation:**

- **Risk:** Correcting time can briefly confuse time-dependent processes (cron jobs, scheduled tasks). Verify before and after.
- **Command:**

  ```bash
  timedatectl set-ntp true
  chronyc makestep       # Force immediate sync (chrony)
  # or
  systemctl restart systemd-timesyncd   # Force sync (systemd-timesyncd)
  timedatectl status
  ```

- **Duration:** Immediate; `chronyc makestep` forces a one-time hard sync within seconds.

**Resolution:**

```bash
# Ensure NTP is permanently enabled and the correct NTP server is configured
timedatectl set-ntp true
# Verify chrony is running and synchronized
chronyc tracking
```

- **Impact:** Host-wide. Time correction affects all processes on the host simultaneously.
- **Rollback:** Not applicable — correct time is always the right state.

**Verification:** `timedatectl status` shows `NTP synchronized: yes`. Re-run `openssl s_client -connect <host>:443 -servername <host> </dev/null 2>/dev/null | openssl x509 -noout -dates` from the affected host and confirm `notAfter` is in the future.

---

### Cause F: AWS ACM auto-renewal failed because DNS validation record was removed

**Statement:** AWS Certificate Manager could not auto-renew a certificate because the CNAME DNS validation record required for renewal was deleted from the hosted zone.

**Mechanism:** ACM uses DNS validation to prove domain ownership before issuing or renewing certificates. During the initial issuance a `_<hash>.<domain>` CNAME record is created; ACM re-uses this same record for all future renewals. If the record is removed (e.g., during a DNS migration, zone recreation, or manual cleanup), ACM cannot validate the domain and the renewal fails silently until the certificate expires. ACM sends expiry notification emails at 45, 30, and 15 days before expiry, but the renewal failure itself does not generate a CloudWatch alarm by default.

**Indicator:**

- [Step 5] `RenewalStatus=FAILED` in ACM describe output
- [Step 5] ACM console or CLI shows `PENDING_VALIDATION` on a certificate that was previously `ISSUED`
<!-- match: {"step": 5, "predicate": "contains", "target": "FAILED"} -->

**Mitigation:**

- **Risk:** Low. Re-adding the DNS CNAME record does not affect live traffic. ACM auto-renewal retries within hours once the record propagates.
- **Command:**

  ```bash
  # Get the required CNAME record details from ACM
  aws acm describe-certificate --certificate-arn <arn> \
    --query 'Certificate.DomainValidationOptions[*].ResourceRecord'

  # Add the CNAME record to Route 53 (or your DNS provider)
  aws route53 change-resource-record-sets --hosted-zone-id <zone-id> \
    --change-batch '{"Changes":[{"Action":"UPSERT","ResourceRecordSet":{"Name":"<cname-name>","Type":"CNAME","TTL":300,"ResourceRecords":[{"Value":"<cname-value>"}]}}]}'
  ```

- **Duration:** 5–30 minutes for DNS propagation and ACM re-validation.

**Resolution:**

```bash
# If the certificate cannot be renewed in time, request a new one
aws acm request-certificate --domain-name <domain> \
  --validation-method DNS --subject-alternative-names <san1> <san2>

# Once validated, swap the listener certificate
aws elbv2 modify-listener --listener-arn <listener-arn> \
  --certificates CertificateArn=<new-cert-arn>
```

- **Impact:** ALB/NLB listener-wide. Updating the listener certificate takes effect within seconds with zero downtime.
- **Rollback:** `aws elbv2 modify-listener --listener-arn <arn> --certificates CertificateArn=<old-arn>` to revert the listener.

**Verification:** `aws acm describe-certificate --certificate-arn <arn> --query 'Certificate.[Status,RenewalSummary]'` shows `Status=ISSUED` and `RenewalStatus=SUCCESS`. `openssl s_client -connect <alb-dns>:443 -servername <domain> </dev/null 2>/dev/null | openssl x509 -noout -dates` confirms a future `notAfter`.

---

### Cause G: Kubernetes TLS Secret not updated after cert-manager renewal

**Statement:** cert-manager renewed the Certificate but the Ingress controller continues to serve the old expired certificate because it caches TLS secrets from Kubernetes and did not detect the secret update.

**Mechanism:** cert-manager writes renewed certificates to a Kubernetes TLS Secret. Ingress controllers (NGINX Ingress, Traefik, Contour) watch Secret resources and reload TLS configuration when a secret changes. If the controller's watch is broken — due to informer cache staleness, missing RBAC permissions to watch Secrets, or a bug in the controller version — it will not pick up the updated secret. The running controller continues to serve the expired certificate from its in-memory cache.

**Indicator:**

- [Step 5] `kubectl get certificates -A` shows `READY=True` with a recent `notAfter`
- [Step 6] Secret contains a valid cert but the live endpoint serves an expired cert (differing serials)
<!-- match: {"step": 6, "predicate": "contains", "target": "Serial Number"} -->

**Mitigation:**

- **Risk:** Low. Restarting the Ingress controller pod causes a brief (< 5 s) reload pause during which new connections may fail; existing connections are not dropped.
- **Command:**

  ```bash
  # Restart the Ingress controller to force a full secret re-read
  kubectl rollout restart deployment/ingress-nginx-controller -n ingress-nginx

  # Verify the rollout completes
  kubectl rollout status deployment/ingress-nginx-controller -n ingress-nginx
  ```

- **Duration:** Under 2 minutes for the rollout to complete.

**Resolution:**

```bash
# If the issue recurs, force cert-manager to write a fresh secret
# by deleting the secret — cert-manager will recreate it immediately
kubectl delete secret <tls-secret-name> -n <namespace>
kubectl get certificate <cert-name> -n <namespace> -w
```

- **Impact:** Namespace-scoped to the Ingress controller pod set. Rollout restart affects all Ingress routes on that controller during the brief reload window.
- **Rollback:** `kubectl rollout undo deployment/ingress-nginx-controller -n ingress-nginx` to revert the controller to the previous version if the restart introduced regressions.

**Verification:** `kubectl get secret <tls-secret> -n <namespace> -o jsonpath='{.data.tls\.crt}' | base64 -d | openssl x509 -noout -serial -dates` and compare serial against `openssl s_client -connect <host>:443` output — both should match.

---

### Cause Z: Unidentified certificate expiry cause

**Statement:** The certificate expiry symptom could not be attributed to any of the diagnosed causes after completing all diagnostic steps.

**Mechanism:** Certificate expiry incidents can have platform-specific root causes not covered by the preceding causes — for example, HSM/vault certificate rotation failures, mutual TLS client-cert expiry in a service mesh, private CA root rollover, or certificate pinning conflicts.

**Indicator:**

- [Default] None of Causes A–G match; Step 1 confirms expiry but Steps 2–7 reveal no clear mechanism

**Mitigation:**

- **Risk:** Low. A temporary TLS bypass or traffic rerouting minimises impact while escalating.
- **Command:**

  ```bash
  # Collect full certificate chain details for escalation
  openssl s_client -connect <host>:443 -servername <host> -showcerts </dev/null 2>/dev/null > /tmp/cert-chain.txt
  openssl s_client -connect <host>:443 -servername <host> -verify_return_error </dev/null 2>&1 >> /tmp/cert-chain.txt
  cat /tmp/cert-chain.txt
  ```

- **Duration:** Escalate immediately; do not leave TLS verification bypassed in production.

**Resolution:** Out of runbook scope. Escalate to the platform or security team with the certificate chain dump and the output of all diagnostic steps.

**Verification:** Confirm with the escalation team that a valid certificate is serving on the endpoint (`openssl s_client` verify return code `0 (ok)`).

## Prevention

- **Automate renewal with deploy hooks.** Configure certbot with `systemctl enable --now certbot.timer` and a deploy hook that reloads the web server. For cert-manager, set `renewBefore: 360h` (15 days) on every Certificate resource:

  ```yaml
  apiVersion: cert-manager.io/v1
  kind: Certificate
  spec:
    renewBefore: 360h
  ```

- **Alert at 30, 14, and 7 days before expiry.** Use Prometheus `blackbox_exporter` with the `probe_ssl_earliest_cert_expiry` metric:

  ```yaml
  - alert: TLSCertificateExpiringSoon
    expr: probe_ssl_earliest_cert_expiry - time() < 86400 * 30
    for: 1h
    labels:
      severity: warning
  - alert: TLSCertificateExpiringCritical
    expr: probe_ssl_earliest_cert_expiry - time() < 86400 * 7
    for: 1h
    labels:
      severity: critical
  ```

  For cert-manager, monitor `certmanager_certificate_expiration_timestamp_seconds` for the same thresholds.

- **Maintain a certificate inventory.** Run a nightly scan across all endpoints and alert on certificates expiring within 30 days:

  ```bash
  for domain in api.example.com app.example.com; do
    expiry=$(openssl s_client -connect "$domain:443" -servername "$domain" </dev/null 2>/dev/null | \
      openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2)
    days_left=$(( ($(date -d "$expiry" +%s) - $(date +%s)) / 86400 ))
    [ "$days_left" -lt 30 ] && echo "WARNING: $domain expires in $days_left days"
  done
  ```

- **Prefer short-lived certificates (Let's Encrypt, 90-day).** Short validity forces regular automation testing and limits exposure from compromised private keys. Commercial 1-year certificates mask renewal pipeline failures for months before expiry.

- **Verify renewal pipelines in staging.** Test the full renewal path using Let's Encrypt staging (`certbot certonly --staging -d <domain> --dry-run`) to catch firewall, DNS, and permission issues without consuming production rate limits.

- **Ensure deploy hooks cover all certificate consumers.** If multiple services (NGINX, HAProxy, a sidecar proxy) share a certificate directory, each must have its own reload hook. Missing one service silently leaves it serving the expired certificate.

- **Protect DNS validation records.** Tag or comment ACM CNAME validation records in Route 53 so DNS engineers know not to delete them. For cert-manager, use a dedicated subdelegated DNS zone for `_acme-challenge.*` records.

- **Monitor NTP synchronization.** Alert on `timedatectl status` showing `NTP synchronized: no` for more than 5 minutes. Clock skew causes spurious TLS failures that are difficult to distinguish from genuine expiry incidents.

## Sources

- [cert-manager Troubleshooting](https://cert-manager.io/docs/troubleshooting/) — Certificate and CertificateRequest status debugging, controller log analysis. Priority 2.
- [cert-manager ACME Troubleshooting](https://cert-manager.io/docs/troubleshooting/acme/) — Order and Challenge debugging for ACME-based issuers, DNS and HTTP solver diagnostics. Priority 2.
- [Let's Encrypt Documentation](https://letsencrypt.org/docs/) — ACME protocol, challenge types, rate limits, chain of trust, and certificate lifecycle. Priority 1.
- [Certbot User Guide](https://eff-certbot.readthedocs.io/en/stable/using.html) — Certificate issuance, renewal, deploy hooks, and troubleshooting. Priority 1.
- [OpenSSL s_client Manual](https://www.openssl.org/docs/man3.0/man1/openssl-s_client.html) — Official reference for TLS connection testing, certificate chain inspection, and verification flags. Priority 1.
- [Prometheus blackbox_exporter](https://github.com/prometheus/blackbox_exporter) — TLS certificate expiry monitoring via `probe_ssl_earliest_cert_expiry` metric. Priority 2.
