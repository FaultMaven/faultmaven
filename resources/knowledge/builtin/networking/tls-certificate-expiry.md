---
id: tls-certificate-expiry
title: "TLS Certificate Expiry — Diagnosis, Emergency Renewal, and Prevention"
domain: networking
service: general
symptom_class:
  - auth_failure
  - service_unavailable
severity: critical
scope: global
version: "1.0.0"
last_updated: "2026-03-26"
verified_by: "kb-researcher"
status: draft
tags:
  - tls
  - ssl
  - certificates
  - x509
  - expiry
  - https
  - openssl
  - certbot
  - cert-manager
difficulty: intermediate
---

# TLS Certificate Expiry

## Problem Definition

TLS certificate expiry causes immediate service outages when X.509 certificates pass their `notAfter` validity date. This runbook applies to any system serving or validating TLS certificates, including web servers (NGINX, Apache, HAProxy), Kubernetes Ingress controllers, AWS ALB/NLB, mutual TLS (mTLS) service meshes, and API gateways. Diagnosis requires `openssl` CLI access, shell access to the certificate-serving host or Kubernetes cluster, and (for renewal) credentials for the certificate authority or ACME client. Certificates from any CA are covered: Let's Encrypt (90-day validity), commercial CAs (1-year validity), and private/internal CAs.

Expired certificates cause clients to reject TLS connections outright. Common symptoms include:

- `curl: (60) SSL certificate problem: certificate has expired` when connecting to an expired endpoint
- Browser errors: `NET::ERR_CERT_DATE_INVALID`, `SEC_ERROR_EXPIRED_CERTIFICATE`, `SSL_ERROR_EXPIRED_CERT_ALERT`
- Application and service mesh logs: `x509: certificate has expired or is not yet valid`, `TLS handshake error: remote error: tls: expired certificate`
- Java applications: `javax.net.ssl.SSLHandshakeException: PKIX path validation failed` when the trust chain includes an expired certificate
- Kubernetes Ingress returning 502 or refusing connections after a cert-manager `Certificate` resource shows `Ready=False`
- mTLS failures between services when either the client or server certificate in a service mesh has expired
- Automated renewal succeeded (new certificate exists on disk) but the web server was never reloaded, so it continues serving the old expired certificate

## Diagnostic Steps

### Step 1. Check Certificate Expiry Date on the Live Endpoint

Connect to the remote endpoint and extract the certificate validity dates. This determines whether the certificate is actually expired and how long ago it expired.

```bash
openssl s_client -connect <host>:443 -servername <host> </dev/null 2>/dev/null | \
  openssl x509 -noout -dates
```

Expected output for a valid certificate: `notBefore` in the past and `notAfter` in the future. If `notAfter` is in the past, the certificate is expired. To check whether a certificate expires within a specific window:

```bash
# Exit code 1 = expired or expires within 24 hours
openssl x509 -in /path/to/cert.pem -noout -checkend 86400
echo "Exit code: $?"
```

Exit code 0 means the certificate is valid for at least the specified seconds. Exit code 1 confirms the certificate is expired or about to expire.

### Step 2. Inspect the Full Certificate Chain

An expired intermediate or cross-signed root certificate causes chain validation failure even when the leaf certificate itself is valid. Inspect every certificate in the chain served by the endpoint.

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

Each certificate in the output shows its subject, issuer, and validity dates. If any intermediate certificate has an expired `notAfter`, that is the root cause -- not the leaf certificate. Common example: the DST Root CA X3 cross-sign expired October 2021, breaking chains on older clients.

### Step 3. Verify the Certificate Covers the Requested Hostname

A certificate may be valid but not cover the hostname being requested, causing a different class of TLS error that can be confused with expiry during incident triage.

```bash
openssl s_client -connect <host>:443 -servername <host> </dev/null 2>/dev/null | \
  openssl x509 -noout -subject -ext subjectAltName
```

Confirm the hostname appears in the Subject Alternative Name (SAN) list. Wildcard certificates (`*.example.com`) do not cover the bare domain (`example.com`) or multi-level subdomains (`a.b.example.com`). If the hostname is not in the SAN list, the issue is certificate scope rather than expiry.

### Step 4. Check System Clock for Skew

A server or client with an incorrect system clock will reject valid certificates as expired (or not-yet-valid). This is especially common on VMs after snapshot restore or on embedded systems without NTP.

```bash
date -u
timedatectl status
chronyc tracking 2>/dev/null || ntpq -p 2>/dev/null
```

If the system time differs from UTC by more than a few minutes, or if `timedatectl` shows `NTP synchronized: no`, clock skew is the likely cause. Valid certificates will appear expired on a system whose clock is set to a future date, and not-yet-valid on a system whose clock is in the past.

### Step 5. Check Automated Renewal Status

Determine whether the renewal mechanism (certbot, cert-manager, ACM) attempted renewal, succeeded, or failed silently.

For certbot:

```bash
systemctl status certbot.timer
certbot certificates
certbot renew --dry-run
```

If `certbot.timer` is inactive or missing, renewals are not running. If `certbot certificates` shows a valid certificate but the live endpoint serves an expired one, the renewal succeeded but the web server was not reloaded (proceed to Step 6). If `--dry-run` fails, the ACME challenge configuration is broken.

For cert-manager (Kubernetes):

```bash
kubectl get certificates -A -o wide
kubectl describe certificate <cert-name> -n <namespace>
kubectl get orders,challenges -A
kubectl logs -n cert-manager deploy/cert-manager --tail=100 | grep -i -E "error|fail|renew"
```

If the Certificate resource shows `Ready=False`, the `describe` output shows the reason (challenge failure, issuer misconfiguration, rate limiting). If `orders` or `challenges` are stuck, the ACME flow is blocked.

For AWS Certificate Manager (ACM):

```bash
aws acm list-certificates --query 'CertificateSummaryList[*].[DomainName,Status,NotAfter]' --output table
aws acm describe-certificate --certificate-arn <arn> --query 'Certificate.[Status,NotAfter,RenewalSummary]'
```

If `RenewalSummary.RenewalStatus` is `FAILED`, ACM could not auto-renew (common cause: DNS validation records were removed).

### Step 6. Compare On-Disk Certificate Against Live Endpoint

Determine whether a renewed certificate exists on disk but the web server is still serving the old certificate.

```bash
echo "=== On disk ==="
openssl x509 -in /etc/letsencrypt/live/<domain>/fullchain.pem -noout -serial -dates 2>/dev/null

echo "=== Live endpoint ==="
openssl s_client -connect <host>:443 -servername <host> </dev/null 2>/dev/null | \
  openssl x509 -noout -serial -dates
```

If the serial numbers differ and the on-disk certificate has a valid `notAfter` while the live endpoint serves an expired one, the web server was not reloaded after renewal. For Kubernetes, compare the secret content against the Ingress endpoint:

```bash
kubectl get secret <tls-secret> -n <namespace> -o jsonpath='{.data.tls\.crt}' | \
  base64 -d | openssl x509 -noout -serial -dates
```

### Step 7. Verify Firewall and Network Access for ACME Challenges

If automated renewal is failing due to challenge validation errors, confirm that the ACME validation path is not blocked.

```bash
# HTTP-01: port 80 must be reachable from the internet
curl -sv http://<host>/.well-known/acme-challenge/test 2>&1 | head -20

# Firewall rules
iptables -L INPUT -n | grep -E '(80|443)'
ss -tlnp | grep -E ':(80|443)\s'

# DNS-01: TXT record must propagate
dig TXT _acme-challenge.<domain> @8.8.8.8
```

If port 80 is blocked (firewall, security group, or no listener), HTTP-01 challenges will fail. If the DNS TXT record does not propagate, DNS-01 challenges will fail. The specific challenge type depends on the certbot or cert-manager issuer configuration.

## Mitigation

### Option 1. Emergency Renewal with certbot

Force an immediate certificate renewal when the existing certificate is expired and certbot is installed.

- **Risk**: Low. Certbot reuses the existing ACME account and domain configuration. If deploy hooks are configured, the web server reloads automatically.
- **Command**:
  ```bash
  certbot renew --force-renewal
  # Or renew a specific domain
  certbot certonly --force-renewal -d <domain> -d <additional-domain>
  # Reload the web server
  systemctl reload nginx
  ```
- **Verify**:
  ```bash
  openssl s_client -connect <host>:443 -servername <host> </dev/null 2>/dev/null | \
    openssl x509 -noout -dates
  ```
  The `notAfter` date should be approximately 90 days in the future for Let's Encrypt.
- **Duration**: 1-5 minutes. ACME validation typically completes in under 60 seconds.

### Option 2. Reload Web Server to Pick Up Already-Renewed Certificate

If the certificate on disk is valid but the live endpoint serves the expired one (confirmed in Step 6), reload the web server.

- **Risk**: Low. A reload does not drop existing connections (NGINX and HAProxy support graceful reload). If the on-disk certificate is malformed, the reload will fail safely with a configuration test.
- **Command**:
  ```bash
  # NGINX
  nginx -t && systemctl reload nginx

  # HAProxy
  haproxy -c -f /etc/haproxy/haproxy.cfg && systemctl reload haproxy

  # Apache
  apachectl configtest && systemctl reload apache2
  ```
- **Verify**:
  ```bash
  openssl s_client -connect <host>:443 -servername <host> </dev/null 2>/dev/null | \
    openssl x509 -noout -serial -dates
  ```
  The serial number should match the on-disk certificate.
- **Duration**: Under 30 seconds.

### Option 3. Replace TLS Secret in Kubernetes

If cert-manager failed to renew or the Ingress is serving an expired certificate from a Kubernetes secret, replace the secret manually or force cert-manager to re-issue.

- **Risk**: Medium. Deleting the TLS secret triggers an Ingress controller reload. If the replacement certificate or key is malformed, HTTPS breaks entirely until corrected.
- **Command**:
  ```bash
  # Option A: Force cert-manager re-issuance by deleting the secret
  kubectl delete secret <tls-secret-name> -n <namespace>
  # cert-manager detects the missing secret and triggers a new Certificate request

  # Option B: Manual secret replacement
  kubectl create secret tls <tls-secret-name> -n <namespace> \
    --cert=/path/to/fullchain.pem \
    --key=/path/to/privkey.pem \
    --dry-run=client -o yaml | kubectl apply -f -
  ```
- **Verify**:
  ```bash
  kubectl get secret <tls-secret-name> -n <namespace> -o jsonpath='{.data.tls\.crt}' | \
    base64 -d | openssl x509 -noout -dates
  openssl s_client -connect <host>:443 -servername <host> </dev/null 2>/dev/null | \
    openssl x509 -noout -dates
  ```
  Both the secret and the live endpoint should show the new expiry date. Ingress controllers typically detect secret changes within 30-60 seconds.
- **Duration**: 1-3 minutes.

### Option 4. Update AWS ALB/NLB Certificate

If the certificate is managed by ACM and auto-renewal failed, request a new certificate or re-validate the existing one.

- **Risk**: Low. ACM certificate association is an in-place update with no downtime on the load balancer.
- **Command**:
  ```bash
  # Request a new certificate if needed
  aws acm request-certificate --domain-name <domain> \
    --validation-method DNS --subject-alternative-names <san1> <san2>

  # Associate new certificate with the listener
  aws elbv2 modify-listener --listener-arn <listener-arn> \
    --certificates CertificateArn=<new-cert-arn>
  ```
- **Verify**:
  ```bash
  openssl s_client -connect <alb-dns>:443 -servername <domain> </dev/null 2>/dev/null | \
    openssl x509 -noout -dates
  ```
  The `notAfter` date should reflect the new certificate.
- **Duration**: 2-30 minutes. DNS validation can take time to propagate.

### Option 5. Temporarily Bypass TLS Verification (Last Resort, Internal Services Only)

If the certificate cannot be renewed immediately and the affected service is internal and non-sensitive, temporarily disable TLS verification on the client side.

- **Risk**: High. Disabling TLS verification removes man-in-the-middle attack protection. Use only for internal services during an active incident, and revert immediately after renewal.
- **Command**:
  ```bash
  # curl
  curl -k https://<host>/health

  # Node.js
  NODE_TLS_REJECT_UNAUTHORIZED=0 node app.js

  # Python
  PYTHONHTTPSVERIFY=0 python script.py
  ```
- **Verify**:
  ```bash
  curl -k -o /dev/null -w "%{http_code}" https://<host>/health
  ```
  Should return HTTP 200. This is a temporary workaround only -- revert as soon as the certificate is renewed.
- **Duration**: Until the certificate is renewed. Must be reverted immediately.

## Root Cause Resolution

**If** `systemctl status certbot.timer` shows the timer is inactive or missing, the automated renewal was never configured or was disabled. Enable it and add a deploy hook to reload the web server after renewal:

```bash
systemctl enable --now certbot.timer
systemctl list-timers | grep certbot

# Add a deploy hook so the web server reloads automatically
cat > /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh << 'HOOK'
#!/bin/bash
nginx -t && systemctl reload nginx
HOOK
chmod +x /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh

# Verify the full renewal path works
certbot renew --dry-run
```

**If** `certbot renew --dry-run` fails with HTTP-01 challenge errors, port 80 is blocked or the web server is not configured to serve `/.well-known/acme-challenge/`. Confirm the firewall allows inbound port 80 and the web server has a location block serving the challenge directory. For DNS-01 challenges, verify the DNS provider API credentials are valid and the zone delegation is correct.

**If** Let's Encrypt rate limits are hit (error message contains `rateLimited`), wait for the rate limit window to reset: 1 hour for failed validations, 1 week for duplicate certificate requests. Use the staging environment (`certbot certonly --staging -d <domain> --dry-run`) for testing without consuming production rate limits.

**If** `kubectl describe certificate <name>` shows `Ready=False` due to Order or Challenge failures, check the cert-manager issuer configuration and the Challenge status:

```bash
kubectl describe clusterissuer <issuer-name>
kubectl get challenges -A
kubectl describe challenge <challenge-name> -n <namespace>
```

Common causes include expired ACME account credentials, Issuer referencing a deleted secret, and DNS solver permissions. Fix the issuer configuration and delete the stale CertificateRequest to trigger a fresh issuance.

**If** the leaf certificate is valid but chain verification fails with an expired intermediate, rebuild the certificate chain with the correct intermediate:

```bash
curl -o intermediate.pem https://letsencrypt.org/certs/lets-encrypt-r3.pem
cat /path/to/cert.pem intermediate.pem > /path/to/fullchain.pem
nginx -t && systemctl reload nginx
```

For the DST Root CA X3 cross-sign expiry issue, ensure the server sends the ISRG Root X1 chain instead of the cross-signed chain.

**If** the certificate on disk is valid but the live endpoint serves the expired one (serial numbers differ in Step 6), the web server was not reloaded after renewal. Reload immediately and configure a deploy hook to prevent recurrence (see the certbot timer fix above).

**If** `timedatectl` shows NTP is not synchronized or the system time is significantly off, fix time synchronization:

```bash
timedatectl set-ntp true
chronyc makestep    # Force immediate sync (chrony)
# or
systemctl restart systemd-timesyncd  # Force sync (systemd-timesyncd)
```

## Verification

After applying a fix, confirm the certificate is valid and the full TLS chain passes verification.

1. Confirm certificate validity on the live endpoint:

```bash
openssl s_client -connect <host>:443 -servername <host> </dev/null 2>/dev/null | \
  openssl x509 -noout -subject -dates -issuer
```

The `notAfter` date should be in the future (approximately 90 days for Let's Encrypt, 1 year for commercial CAs).

2. Verify the full certificate chain passes validation:

```bash
openssl s_client -connect <host>:443 -servername <host> -verify_return_error </dev/null 2>&1 | \
  grep "Verify return code"
```

Expected: `Verify return code: 0 (ok)`. Any other return code indicates a chain problem.

3. Test client connectivity with strict TLS validation (no `-k` flag):

```bash
curl -sv https://<host>/health 2>&1 | grep -E "(SSL certificate|HTTP/|subject)"
```

The request should succeed without certificate errors and return the expected HTTP status.

4. Confirm automated renewal is operational:

```bash
# certbot
certbot renew --dry-run

# cert-manager
kubectl get certificates -A -o wide
```

For certbot, `--dry-run` should succeed. For cert-manager, all Certificate resources should show `Ready=True`.

5. Verify in Certificate Transparency logs that the new certificate was issued:

```bash
curl -s "https://crt.sh/?q=<domain>&output=json" | python3 -c "import sys,json; certs=json.load(sys.stdin); print(certs[0]['not_after'] if certs else 'No certs found')"
```

The most recent entry should show the new certificate's expiry date.

## Prevention

- **Automate certificate renewal with deploy hooks.** Never rely on manual renewal. Configure certbot with a systemd timer and a deploy hook that reloads the web server. For Kubernetes, use cert-manager with `renewBefore: 360h` (15 days) to renew well before expiry:

```yaml
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: example-cert
spec:
  secretName: example-tls
  issuerRef:
    name: letsencrypt-prod
    kind: ClusterIssuer
  dnsNames:
    - example.com
    - "*.example.com"
  renewBefore: 360h
```

- **Monitor certificate expiry with Prometheus alerts.** Use blackbox_exporter to probe TLS endpoints and alert at 30, 14, and 7 days before expiry:

```yaml
- alert: TLSCertificateExpiringSoon
  expr: probe_ssl_earliest_cert_expiry - time() < 86400 * 30
  for: 1h
  labels:
    severity: warning
  annotations:
    summary: "TLS certificate for {{ $labels.instance }} expires in < 30 days"

- alert: TLSCertificateExpiringCritical
  expr: probe_ssl_earliest_cert_expiry - time() < 86400 * 7
  for: 1h
  labels:
    severity: critical
  annotations:
    summary: "TLS certificate for {{ $labels.instance }} expires in < 7 days"
```

For cert-manager, monitor `certmanager_certificate_expiration_timestamp_seconds` for the same thresholds.

- **Maintain a certificate inventory.** Track all certificates, their expiry dates, renewal methods, and responsible owners. Run a scheduled scan across all endpoints:

```bash
#!/bin/bash
DOMAINS="api.example.com app.example.com admin.example.com"
WARN_DAYS=30
for domain in $DOMAINS; do
  expiry=$(openssl s_client -connect "$domain:443" -servername "$domain" </dev/null 2>/dev/null | \
    openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2)
  expiry_epoch=$(date -d "$expiry" +%s 2>/dev/null)
  days_left=$(( (expiry_epoch - $(date +%s)) / 86400 ))
  if [ "$days_left" -lt "$WARN_DAYS" ]; then
    echo "WARNING: $domain expires in $days_left days ($expiry)"
  else
    echo "OK: $domain expires in $days_left days ($expiry)"
  fi
done
```

- **Prefer short-lived certificates.** Use Let's Encrypt (90-day validity) over long-lived commercial certificates. Short-lived certificates force early automation, reduce exposure from compromised private keys, and ensure the renewal pipeline is tested regularly.

- **Configure renewal buffer time.** Renew certificates well before expiry. Certbot renews at 30 days remaining by default; cert-manager uses `renewBefore` (set to at least 15 days). For commercial CAs, set calendar reminders at 60, 30, and 14 days before expiry.

- **Test renewal in staging before production.** Validate the full renewal flow in a staging environment using Let's Encrypt staging (`certbot certonly --staging -d <domain>`) to catch firewall, DNS, and permission issues without consuming production rate limits.

- **Ensure deploy hooks reload all dependent services.** Certificate renewal without service reload is a silent failure. Configure post-renewal hooks for every service that consumes the certificate (NGINX, HAProxy, application servers, sidecar proxies).

## Sources

- [OpenSSL s_client Manual](https://www.openssl.org/docs/man3.0/man1/openssl-s_client.html) -- Official reference for TLS connection testing, certificate chain inspection, and verification flags.
- [Let's Encrypt Documentation](https://letsencrypt.org/docs/) -- ACME protocol, challenge types, rate limits, chain of trust, and certificate lifecycle.
- [Certbot User Guide](https://eff-certbot.readthedocs.io/en/stable/using.html) -- Certificate issuance, renewal, deploy hooks, and troubleshooting.
- [cert-manager Troubleshooting](https://cert-manager.io/docs/troubleshooting/) -- Kubernetes certificate lifecycle debugging, Certificate and CertificateRequest status.
- [cert-manager ACME Troubleshooting](https://cert-manager.io/docs/troubleshooting/acme/) -- Order and Challenge debugging for ACME-based issuers, DNS and HTTP solver diagnostics.
- [Prometheus blackbox_exporter](https://github.com/prometheus/blackbox_exporter) -- TLS certificate expiry monitoring via `probe_ssl_earliest_cert_expiry` metric.
- [Mozilla SSL Configuration Generator](https://ssl-config.mozilla.org/) -- Recommended TLS configurations for NGINX, Apache, HAProxy, and other servers.
