---
id: "tls-certificate-expiry"
title: "TLS Certificate Expiry Causing Service Outage"
domain: networking
service: general
symptom_class: [auth_failure, service_unavailable]
severity: critical
scope: global
version: "2.0.0"
last_updated: "2026-06-25"
verified_by: "kb-researcher"
status: draft
tags: [tls, ssl, certificates, x509, expiry, openssl, certbot, cert-manager, acme, lets-encrypt]
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

### Step 2: Inspect the full chain for expired intermediates

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

### Step 3: Confirm the certificate covers the hostname

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

### Step 6: Compare on-disk certificate against live endpoint

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

### Cause A: Leaf certificate passed notAfter and was not renewed

**Statement:** The X.509 leaf certificate served by the endpoint has passed its `notAfter` validity date and was never renewed, so all TLS clients reject the handshake.

**Chain:**
- root: the served leaf certificate's `notAfter` is in the past (renewal never ran)
- s1: every TLS client compares `notAfter` against the current time during the handshake and finds it expired
- s2: the client terminates the connection before any application data is exchanged; no TLS session is established
- D: the service is completely unreachable over HTTPS (points at Symptom Recognition)

**Indicators:**
- root: [Step 1] `notAfter` date is in the past
- s2: [Symptom] `certificate has expired` in client or server logs

**Interventions:**
- **remediation** (root): enable the certbot timer and a deploy hook so renewals run automatically at the 30-day threshold.

  ```bash
  systemctl enable --now certbot.timer
  cat > /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh << 'HOOK'
  #!/bin/bash
  nginx -t && systemctl reload nginx
  HOOK
  chmod +x /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
  certbot renew --dry-run
  ```

  **Verification:** re-run Step 1; `notAfter` is ~90 days in the future and `certbot renew --dry-run` succeeds without errors.
- **mitigation** (root): force an immediate renewal to restore service now.

  ```bash
  certbot renew --force-renewal
  # Or a specific domain
  certbot certonly --force-renewal -d <domain>
  systemctl reload nginx
  ```

  **Risk:** Certbot reuses the existing ACME account and domain config; rate-limited if multiple forced renewals were recently attempted. **Duration:** 1-5 minutes; ACME validation typically completes in under 60 seconds. **Verification:** re-run Step 1; `notAfter` is in the future.

---

### Cause B: Renewed certificate on disk but web server never reloaded

**Statement:** Automated renewal wrote a valid certificate to disk, but the web server was never sent a reload signal and keeps serving the old expired certificate from memory.

**Chain:**
- root: renewal wrote a new certificate to disk but no `SIGHUP`/`systemctl reload` was sent to the web server
- s1: the running web server still holds the old certificate cached in memory from its last start/reload
- s2: clients are served the cached expired certificate even though a valid replacement exists on disk
- D: TLS clients reject the expired served certificate and the service is unreachable (points at Symptom Recognition)

**Indicators:**
- s2: [Step 6] serial numbers differ — on-disk cert has a future `notAfter`, live endpoint serves a past `notAfter`

**Interventions:**
- **remediation** (root): add a deploy hook so future renewals automatically reload the web server.

  ```bash
  cat > /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh << 'HOOK'
  #!/bin/bash
  nginx -t && systemctl reload nginx
  HOOK
  chmod +x /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
  ```

  **Verification:** re-run Step 6; on-disk and live endpoint show the same serial and a future `notAfter`.
- **mitigation** (s1): reload the running web server to pick up the on-disk certificate now.

  ```bash
  # NGINX
  nginx -t && systemctl reload nginx

  # HAProxy
  haproxy -c -f /etc/haproxy/haproxy.cfg && systemctl reload haproxy

  # Apache
  apachectl configtest && systemctl reload apache2
  ```

  **Risk:** Low. `nginx -t` validates config before reload; a graceful reload does not drop existing connections. **Duration:** Under 30 seconds. **Verification:** re-run Step 6; serials and `notAfter` match.

---

### Cause C: cert-manager Certificate stuck on a failing ACME challenge

**Statement:** The cert-manager Certificate shows `Ready=False` because its ACME HTTP-01 or DNS-01 challenge is blocked by a firewall, Ingress misconfiguration, or DNS propagation failure.

**Chain:**
- root: the ACME challenge is unreachable — port 80 blocked / Ingress not routing `.well-known` / DNS-01 TXT not propagated or wrong API credentials
- s1: the ACME CA cannot validate the Order's Challenge, so issuance never completes
- s2: the Certificate stays `Ready=False` and the existing certificate is left to expire unrenewed
- D: the endpoint serves an expired certificate (or 502s) and clients reject it (points at Symptom Recognition)

**Indicators:**
- s2: [Step 5] `kubectl get certificates -A` shows `READY=False`
- s2: [Step 5] `kubectl describe certificate` shows a challenge failure reason or `Waiting for DNS-01 challenge propagation`
- root: [Step 7] port 80 connection refused or DNS TXT record absent

**Interventions:**
- **remediation** (root): fix challenge connectivity — allow `.well-known/acme-challenge/` through the Ingress and confirm RBAC, or refresh the DNS-01 provider secret.

  ```bash
  # For HTTP-01 — ensure the Ingress allows /.well-known/acme-challenge/
  kubectl describe clusterissuer <issuer-name>
  kubectl auth can-i create ingress --as=system:serviceaccount:cert-manager:cert-manager -n <namespace>

  # For DNS-01 — check the DNS provider secret is current
  kubectl get secret <dns-provider-secret> -n cert-manager -o yaml
  ```

  **Verification:** re-run Step 5; `kubectl get certificates -A -o wide` shows `READY=True` and `kubectl get orders,challenges -A` returns no pending items.
- **mitigation** (s1): force re-issuance by deleting the stale CertificateRequest after fixing the challenge.

  ```bash
  kubectl get challenges -A
  kubectl describe challenge <challenge-name> -n <namespace>

  # cert-manager recreates the CertificateRequest automatically
  kubectl delete certificaterequest -n <namespace> --all
  ```

  **Risk:** Low. Deleting stale CertificateRequest/Challenge resources causes cert-manager to re-attempt issuance. **Duration:** 2-10 minutes for ACME validation after the challenge is fixed. **Verification:** re-run Step 5; the Certificate goes `READY=True`.

---

### Cause D: Expired intermediate certificate in the trust chain

**Statement:** The leaf certificate is valid but an intermediate CA certificate in the served chain has passed its `notAfter` date, so chain validation fails on clients doing full-chain verification.

**Chain:**
- root: an intermediate CA certificate in the served chain has passed its `notAfter` (e.g. DST Root CA X3, expired Oct 2021)
- s1: clients walk the chain leaf-to-root and hit the expired intermediate during path validation
- s2: path validation terminates with `certificate has expired` / `PKIX path validation failed`, even though the leaf is still valid
- D: clients performing full-chain verification reject the connection (points at Symptom Recognition)

**Indicators:**
- s1: [Step 1] `notAfter` on the leaf cert is in the future
- root: [Step 2] one of the intermediate certs shows a past `notAfter`

**Interventions:**
- **remediation** (root): replace the expired intermediate with the CA's current one and reload; ensure certbot/cert-manager always bundle the full chain (default for both).

  ```bash
  curl -o intermediate.pem https://letsencrypt.org/certs/lets-encrypt-r3.pem
  cat /path/to/cert.pem intermediate.pem > /path/to/fullchain.pem
  nginx -t && systemctl reload nginx
  ```

  **Verification:** re-run Step 2; all intermediates show future `notAfter`. Run `openssl s_client -connect <host>:443 -servername <host> -verify_return_error </dev/null 2>&1 | grep "Verify return code"` and confirm `0 (ok)`.

---

### Cause E: System clock skew making valid certificates appear expired

**Statement:** The local system clock is significantly ahead of real UTC, so in-date certificates fail `notAfter` validation because the skewed clock places the current time past their expiry.

**Chain:**
- root: the host lost NTP synchronization (snapshot restore, live migration, embedded device) and its clock drifted ahead of real UTC
- s1: TLS validity checks compare `notAfter` against the skewed local clock, not a network time source
- s2: an in-date certificate is judged expired locally because the clock places "now" past its `notAfter`
- D: client- or server-side validation rejects the handshake as expired (points at Symptom Recognition)

**Indicators:**
- s2: [Step 1] `notAfter` appears in the past from the affected host, but a remote check confirms the certificate is valid
- root: [Step 4] `timedatectl status` shows `NTP synchronized: no` or a significant offset

**Interventions:**
- **remediation** (root): permanently enable NTP and confirm chrony is synchronized.

  ```bash
  timedatectl set-ntp true
  chronyc tracking
  ```

  **Verification:** re-run Step 4; `timedatectl status` shows `NTP synchronized: yes`. Re-run Step 1 from the affected host; `notAfter` is in the future.
- **mitigation** (root): force an immediate one-time hard time sync to clear the skew now.

  ```bash
  timedatectl set-ntp true
  chronyc makestep       # Force immediate sync (chrony)
  # or
  systemctl restart systemd-timesyncd   # Force sync (systemd-timesyncd)
  timedatectl status
  ```

  **Risk:** Correcting time can briefly confuse time-dependent processes (cron jobs, scheduled tasks); verify before and after. **Duration:** Immediate; `chronyc makestep` forces a one-time hard sync within seconds. **Verification:** re-run Step 4; offset is near zero and `NTP synchronized: yes`.

---

### Cause F: AWS ACM auto-renewal failed because the DNS validation record was removed

**Statement:** AWS Certificate Manager could not auto-renew a certificate because the CNAME DNS validation record it reuses for renewals was deleted from the hosted zone.

**Chain:**
- root: the `_<hash>.<domain>` CNAME validation record ACM reuses for renewals was removed (DNS migration, zone recreation, manual cleanup)
- s1: ACM cannot prove domain ownership, so auto-renewal fails silently with no default CloudWatch alarm
- s2: the certificate is left unrenewed and reverts to `PENDING_VALIDATION` / `RenewalStatus=FAILED` until it expires
- D: the ALB/NLB serves an expired certificate and clients reject it (points at Symptom Recognition)

**Indicators:**
- s2: [Step 5] `RenewalStatus=FAILED` in ACM describe output
- s2: [Step 5] ACM console or CLI shows `PENDING_VALIDATION` on a certificate that was previously `ISSUED`

**Interventions:**
- **remediation** (root): re-add the required CNAME validation record so ACM can validate and auto-renew.

  ```bash
  aws acm describe-certificate --certificate-arn <arn> \
    --query 'Certificate.DomainValidationOptions[*].ResourceRecord'

  aws route53 change-resource-record-sets --hosted-zone-id <zone-id> \
    --change-batch '{"Changes":[{"Action":"UPSERT","ResourceRecordSet":{"Name":"<cname-name>","Type":"CNAME","TTL":300,"ResourceRecords":[{"Value":"<cname-value>"}]}}]}'
  ```

  **Verification:** re-run Step 5; `aws acm describe-certificate ... --query 'Certificate.[Status,RenewalSummary]'` shows `Status=ISSUED` and `RenewalStatus=SUCCESS`.
- **mitigation** (s2): if the cert cannot be renewed in time, request a fresh one and swap the listener certificate.

  ```bash
  aws acm request-certificate --domain-name <domain> \
    --validation-method DNS --subject-alternative-names <san1> <san2>

  # Once validated, swap the listener certificate
  aws elbv2 modify-listener --listener-arn <listener-arn> \
    --certificates CertificateArn=<new-cert-arn>
  ```

  **Risk:** Low. Updating the listener certificate takes effect within seconds with zero downtime; roll back with `aws elbv2 modify-listener --listener-arn <arn> --certificates CertificateArn=<old-arn>`. **Duration:** 5-30 minutes for DNS propagation and ACM re-validation. **Verification:** `openssl s_client -connect <alb-dns>:443 -servername <domain> </dev/null 2>/dev/null | openssl x509 -noout -dates` confirms a future `notAfter`.

---

### Cause G: Kubernetes TLS Secret updated but Ingress controller did not reload

**Statement:** cert-manager renewed the Certificate and updated the TLS Secret, but the Ingress controller's broken Secret watch left it serving the old expired certificate from its in-memory cache.

**Chain:**
- root: the Ingress controller's Secret watch is broken (stale informer cache, missing RBAC to watch Secrets, or a controller bug)
- s1: cert-manager wrote the renewed certificate to the TLS Secret but the controller never detected the update
- s2: the controller keeps serving the old expired certificate cached in memory despite a valid Secret
- D: clients are served the expired certificate and reject the connection (points at Symptom Recognition)

**Indicators:**
- s1: [Step 5] `kubectl get certificates -A` shows `READY=True` with a recent `notAfter`
- s2: [Step 6] the Secret contains a valid cert but the live endpoint serves an expired cert (differing serials)

**Interventions:**
- **remediation** (root): force cert-manager to rewrite a fresh Secret so the controller's watch fires.

  ```bash
  kubectl delete secret <tls-secret-name> -n <namespace>
  kubectl get certificate <cert-name> -n <namespace> -w
  ```

  **Verification:** re-run Step 6; the Secret and live endpoint serials match with a future `notAfter`.
- **mitigation** (s2): restart the Ingress controller to force a full Secret re-read now.

  ```bash
  kubectl rollout restart deployment/ingress-nginx-controller -n ingress-nginx
  kubectl rollout status deployment/ingress-nginx-controller -n ingress-nginx
  ```

  **Risk:** Low. The restart causes a brief (< 5 s) reload pause during which new connections may fail; existing connections are not dropped. Roll back with `kubectl rollout undo deployment/ingress-nginx-controller -n ingress-nginx`. **Duration:** Under 2 minutes for the rollout to complete. **Verification:** re-run Step 6; live endpoint serial matches the Secret.

---

### Cause Z: Unidentified

**Statement:** The certificate expiry symptom could not be attributed to any diagnosed cause after completing all diagnostic steps.

**Indicators:**
- [Default]

**Interventions:**
- **mitigation** (D): capture a full certificate-chain diagnostic snapshot and escalate to the platform or security SME.

  ```bash
  openssl s_client -connect <host>:443 -servername <host> -showcerts </dev/null 2>/dev/null > /tmp/cert-chain.txt
  openssl s_client -connect <host>:443 -servername <host> -verify_return_error </dev/null 2>&1 >> /tmp/cert-chain.txt
  cat /tmp/cert-chain.txt
  ```

  **Risk:** Low; a temporary TLS bypass or traffic rerouting minimises impact while escalating — never leave TLS verification bypassed in production. **Duration:** Escalate immediately. **Verification:** confirm with the escalation team that a valid certificate is serving (`openssl s_client` verify return code `0 (ok)`).

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
