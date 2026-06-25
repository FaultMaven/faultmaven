---
id: "cloudfront-5xx-errors"
title: "AWS CloudFront 502/504 errors: origin connection, SSL/cipher, timeout, and cache-behavior misrouting"
domain: networking
service: aws-cloudfront
symptom_class: [service_unavailable, timeout]
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-06-24"
verified_by: "kb-researcher"
status: draft
tags: [cloudfront, http-502, http-504, ssl-handshake, origin-timeout, cache-behavior]
difficulty: advanced
---

## Symptom Recognition

- Viewer receives `502 ERROR` page: `The request could not be satisfied.` / `CloudFront wasn't able to connect to the origin.`
- Viewer receives `504 ERROR` page: `The request could not be satisfied.` / `CloudFront attempted to establish a connection with the origin, but either the attempt failed or the origin closed the connection.`
- Response header `X-Cache: Error from cloudfront` on the failing requests.
- CloudWatch metric `5xxErrorRate` (namespace `AWS/CloudFront`, dimension `DistributionId`, Region `Global`/`us-east-1`) rises above baseline; per-code `502ErrorRate` / `504ErrorRate` spike when additional metrics are enabled.
- Origin access logs show TLS handshake aborts, or show NO matching request at all (CloudFront never reached the origin).

## Applicability

- Amazon CloudFront distributions with a custom origin (EC2, ALB, NLB, S3 website endpoint, or non-AWS HTTP origin). S3 REST origins (OAC/OAI) excluded.
- Requires: IAM permissions `cloudfront:GetDistributionConfig`, `cloudfront:GetDistribution`, `cloudwatch:GetMetricStatistics`; network reachability to the origin for connectivity tests.
- Tools: AWS CLI v2 (`aws cloudfront`, `aws cloudwatch`), `openssl`, `curl`, `dig`/`nslookup`, `nc`/`telnet`.

## Diagnostic Steps

### Step 1: Confirm the error code and rate from CloudWatch
```bash
aws cloudwatch get-metric-statistics \
  --namespace AWS/CloudFront \
  --metric-name 5xxErrorRate \
  --dimensions Name=DistributionId,Value=E123EXAMPLE Name=Region,Value=Global \
  --start-time "$(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ)" \
  --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --period 300 --statistics Average --region us-east-1
```
Expected output: `Datapoints` array; a non-zero `Average` (percentage) confirms 5xx errors are originating from CloudFront, not the viewer's network.

### Step 2: Dump the distribution origin and cache-behavior config
```bash
aws cloudfront get-distribution-config --id E123EXAMPLE --output json \
  | jq '.DistributionConfig | {Origins: .Origins.Items, DefaultCacheBehavior: .DefaultCacheBehavior, CacheBehaviors: .CacheBehaviors.Items}'
```
Expected output: each origin's `DomainName`, `CustomOriginConfig.OriginProtocolPolicy`, `OriginSslProtocols`, `ConnectionTimeout`, `OriginReadTimeout` (origin response timeout); each cache behavior's `PathPattern` and `TargetOriginId`.

### Step 3: Test the origin TLS handshake and certificate chain
```bash
openssl s_client -connect origin.example.com:443 -servername origin.example.com -showcerts </dev/null 2>&1 \
  | egrep -i 'verify return code|subject=|issuer=|Cipher is|Protocol'
```
Expected output: `Verify return code: 0 (ok)`, a non-empty issuer chain (leaf + intermediates), and a `Protocol`/`Cipher` line for the negotiated session.

### Step 4: Test origin reachability and response latency end to end
```bash
curl -sv -o /dev/null --max-time 65 \
  -w 'dns=%{time_namelookup} connect=%{time_connect} tls=%{time_appconnect} ttfb=%{time_starttransfer} total=%{time_total} code=%{http_code}\n' \
  https://origin.example.com/healthz
```
Expected output: `code=200` with `total` well under 30s. A hung connect (`connect`/`tls` never completing) or `total` near/over 30s points at firewall/security-group blocking or a slow origin.

## Causes

### Cause A: Origin TLS protocol/cipher mismatch
**Statement:** The custom origin offers no SSL/TLS protocol or cipher suite in common with the set CloudFront supports, so the TLS handshake between CloudFront and the origin fails and CloudFront returns 502.
**Chain:**
- root: origin's enabled protocols/ciphers do not intersect CloudFront's supported set
- s1: TLS ClientHello/ServerHello negotiation aborts with no agreed cipher
- s2: CloudFront cannot establish the back-end TLS session and drops the connection
- D: 502 Bad Gateway returned to the viewer (Symptom Recognition)
**Indicators:**
- root: [Step 3] `openssl s_client` negotiates only protocols/ciphers outside CloudFront's supported list (e.g. only TLS 1.0 or a non-supported cipher), or handshake fails outright
  <!-- match: {"step": 3, "predicate": "absent", "target": "Verify return code: 0 (ok)"} -->
- s1: [Step 2] `OriginSslProtocols` on the origin config omits the protocol the origin actually requires
- D: [Symptom] response header `X-Cache: Error from cloudfront` with a 502 page
**Interventions:**
- **remediation** (root): Enable a CloudFront-supported protocol and cipher on the origin (TLS 1.2 with a modern cipher), and set the distribution's `OriginSslProtocols` to match.

  ```bash
  aws cloudfront get-distribution-config --id E123EXAMPLE --output json > dist.json
  # In dist.json set Origins.Items[].CustomOriginConfig.OriginSslProtocols.Items to ["TLSv1.2"]
  ETAG=$(jq -r '.ETag' dist.json)
  jq '.DistributionConfig' dist.json > dist-config.json
  aws cloudfront update-distribution --id E123EXAMPLE \
    --distribution-config file://dist-config.json --if-match "$ETAG"
  ```
  **Verification:** Re-run Step 3; expect `Verify return code: 0 (ok)` and a TLS 1.2 `Protocol` line, then re-run Step 1 and confirm `5xxErrorRate` returns to baseline.

### Cause B: Origin certificate invalid, self-signed, or incomplete chain
**Statement:** The origin's certificate is expired, self-signed, or served without the full intermediate chain (or its SAN/CN does not match the Origin Domain Name), so CloudFront cannot validate it and returns 502.
**Chain:**
- root: origin certificate is untrusted (expired/self-signed/missing intermediates/name mismatch)
- s1: CloudFront's certificate validation of the origin fails during the handshake
- s2: CloudFront drops the TCP connection to the origin
- D: 502 Bad Gateway returned to the viewer (Symptom Recognition)
**Indicators:**
- root: [Step 3] `openssl s_client` shows `Verify return code` other than 0, a self-signed leaf, missing intermediate `issuer=` entries, or a `subject=` CN/SAN that does not match the Origin Domain Name
  <!-- match: {"step": 3, "predicate": "contains", "target": "self signed certificate"} -->
- s1: [Step 2] `OriginProtocolPolicy` is `https-only` or `match-viewer` (TLS to origin is in force, so cert validation applies)
- D: [Symptom] 502 page with `CloudFront wasn't able to connect to the origin`
**Interventions:**
- **remediation** (root): Install a publicly trusted certificate (e.g. ACM-issued on the ALB, or a CA cert) that includes the full intermediate chain in correct order and whose SAN matches the Origin Domain Name; CloudFront does not support self-signed origin certs.

  ```bash
  # Verify the chain order/completeness after install:
  openssl s_client -connect origin.example.com:443 -servername origin.example.com -showcerts </dev/null 2>/dev/null \
    | openssl verify -untrusted /dev/stdin
  ```
  **Verification:** Re-run Step 3 and confirm `Verify return code: 0 (ok)` with leaf + intermediate(s) present; reload the distribution URL and confirm a 200 instead of 502.

### Cause C: Origin unreachable from CloudFront (firewall / security group / private origin)
**Statement:** A security group, firewall, or network ACL blocks inbound traffic from CloudFront edge IPs to the origin (or the origin is on a private network with no public route), so CloudFront's connection attempt never completes and it returns 504.
**Chain:**
- root: inbound path from CloudFront edge ranges to the origin port is blocked or the origin has no public route
- s1: CloudFront's TCP connect attempts to the origin time out (3 attempts, 10s each = 30s default)
- s2: no origin response is received before the request expires
- D: 504 Gateway Timeout returned to the viewer (Symptom Recognition)
**Indicators:**
- root: [Step 4] `curl` to the origin hangs at `connect`/`tls` and aborts at `--max-time`, OR origin access logs show no CloudFront requests at all
  <!-- match: {"step": 4, "predicate": "absent", "target": "code=200"} -->
- s1: [Step 2] origin `DomainName` resolves to a private IP or a security group lacking the CloudFront managed prefix list
- D: [Symptom] 504 page `either the attempt failed or the origin closed the connection`
**Interventions:**
- **remediation** (root): Allow inbound traffic from CloudFront by attaching the CloudFront managed prefix list (`com.amazonaws.global.cloudfront.origin-facing`) to the origin's security group, and ensure the origin is publicly routable.

  ```bash
  PLID=$(aws ec2 describe-managed-prefix-lists \
    --filters Name=prefix-list-name,Values=com.amazonaws.global.cloudfront.origin-facing \
    --query 'PrefixLists[0].PrefixListId' --output text)
  aws ec2 authorize-security-group-ingress \
    --group-id sg-0abc123 --ip-permissions \
    "IpProtocol=tcp,FromPort=443,ToPort=443,PrefixListIds=[{PrefixListId=$PLID}]"
  ```
  **Verification:** Re-run Step 4 from outside the VPC and expect `code=200`; re-run Step 1 and confirm 504s clear.
- **mitigation** (s1): Temporarily widen the origin's allow-list to `0.0.0.0/0` on the origin port to confirm the block is network-layer, then immediately scope it back to the prefix list.

  ```bash
  aws ec2 authorize-security-group-ingress \
    --group-id sg-0abc123 --protocol tcp --port 443 --cidr 0.0.0.0/0
  ```
  **Risk:** Exposes the origin directly to the internet, bypassing CloudFront/WAF. **Duration:** Minutes — only long enough to confirm reachability, then revoke. **Verification:** Step 4 returns `code=200` while open; revoke with `revoke-security-group-ingress` and confirm the prefix-list rule still serves traffic.

### Cause D: Origin response slower than the origin response timeout
**Statement:** The origin takes longer to return the complete response than the distribution's origin response timeout (`OriginReadTimeout`, default 30s, max 60s), so CloudFront ends the connection and returns 504.
**Chain:**
- root: origin processing time for the request exceeds the configured `OriginReadTimeout`
- s1: CloudFront's read on the established origin connection times out
- s2: CloudFront terminates the in-flight request before a body is returned
- D: 504 Gateway Timeout returned to the viewer (Symptom Recognition)
**Indicators:**
- root: [Step 4] `curl` `ttfb`/`total` for the slow path approaches or exceeds the configured `OriginReadTimeout`
  <!-- match: {"step": 4, "predicate": "threshold", "metric": "total", "op": ">=", "value": 30} -->
- s1: [Step 2] `Origins.Items[].CustomOriginConfig.OriginReadTimeout` is at default 30 while the workload's p99 exceeds it
- D: [Symptom] 504 page returned only on the slow/expensive endpoints
**Interventions:**
- **defensive_fix** (s1): Raise `OriginReadTimeout` toward the 60s maximum to absorb legitimately slow responses while the origin is optimized.

  ```bash
  aws cloudfront get-distribution-config --id E123EXAMPLE --output json > dist.json
  # Set Origins.Items[].CustomOriginConfig.OriginReadTimeout to 60
  ETAG=$(jq -r '.ETag' dist.json); jq '.DistributionConfig' dist.json > dist-config.json
  aws cloudfront update-distribution --id E123EXAMPLE \
    --distribution-config file://dist-config.json --if-match "$ETAG"
  ```
  **Verification:** Re-run Step 4 against the slow endpoint and confirm `code=200` within the new window; confirm `504ErrorRate` falls in Step 1.
- **remediation** (root): Reduce origin processing time below the timeout (optimize the slow query/handler or offload long jobs to async) so responses complete well within `OriginReadTimeout`.

  ```bash
  curl -sv -o /dev/null --max-time 65 \
    -w 'ttfb=%{time_starttransfer} total=%{time_total} code=%{http_code}\n' \
    https://origin.example.com/slow-endpoint
  ```
  **Verification:** `ttfb`/`total` drop comfortably below `OriginReadTimeout` and 504s disappear from Step 1.

### Cause E: Cache-behavior path-pattern misrouting to the wrong origin
**Statement:** A cache behavior's path pattern routes matching requests to a `TargetOriginId` that cannot serve them (a misconfigured, decommissioned, or wrong origin), because CloudFront uses the first matching behavior in list order, so those requests fail with 502/504.
**Chain:**
- root: a cache behavior's `PathPattern`/order sends a request path to the wrong `TargetOriginId`
- s1: CloudFront forwards the request to an origin that is unconfigured or down for that path
- s2: that origin fails to connect or to respond
- D: 502/504 returned to the viewer for the affected paths only (Symptom Recognition)
**Indicators:**
- root: [Step 2] the `PathPattern` of an earlier-listed cache behavior matches the failing path and its `TargetOriginId` points at the wrong/dead origin
  <!-- match: {"step": 2, "predicate": "contains", "target": "TargetOriginId"} -->
- s1: [Step 1] 5xx errors are scoped to specific URL paths rather than the whole distribution
- D: [Symptom] only requests under one path prefix return 502/504 while others return 200
**Interventions:**
- **remediation** (root): Correct the offending cache behavior's `TargetOriginId` (or path-pattern ordering) so the path routes to a healthy origin.

  ```bash
  aws cloudfront get-distribution-config --id E123EXAMPLE --output json > dist.json
  # Fix the failing CacheBehaviors.Items[].TargetOriginId / reorder PathPatterns
  ETAG=$(jq -r '.ETag' dist.json); jq '.DistributionConfig' dist.json > dist-config.json
  aws cloudfront update-distribution --id E123EXAMPLE \
    --distribution-config file://dist-config.json --if-match "$ETAG"
  ```
  **Verification:** Re-run Step 2 and confirm the path maps to the intended healthy `TargetOriginId`; request the affected path and confirm 200, then confirm 5xx clears in Step 1.

### Cause Z: Unidentified
**Statement:** The 502/504 errors persist after ruling out TLS/cipher mismatch, certificate validity, origin reachability, response timeout, and cache-behavior routing; the root cause is not yet identified from available evidence.
**Indicators:**
- [Default]
**Interventions:**
- **mitigation** (D): Capture a full diagnostic snapshot and escalate to the networking/CloudFront SME.

  ```bash
  TS=$(date -u +%Y%m%dT%H%M%SZ); OUT="cloudfront-${TS}"
  aws cloudfront get-distribution-config --id E123EXAMPLE --output json > "${OUT}-dist.json"
  aws cloudwatch get-metric-statistics --namespace AWS/CloudFront --metric-name 5xxErrorRate \
    --dimensions Name=DistributionId,Value=E123EXAMPLE Name=Region,Value=Global \
    --start-time "$(date -u -d '3 hours ago' +%Y-%m-%dT%H:%M:%SZ)" \
    --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --period 300 --statistics Average \
    --region us-east-1 > "${OUT}-5xx.json"
  openssl s_client -connect origin.example.com:443 -servername origin.example.com -showcerts </dev/null 2>&1 > "${OUT}-tls.txt"
  curl -sv -o /dev/null --max-time 65 \
    -w 'dns=%{time_namelookup} connect=%{time_connect} tls=%{time_appconnect} ttfb=%{time_starttransfer} total=%{time_total} code=%{http_code}\n' \
    https://origin.example.com/ 2> "${OUT}-curl.txt"
  tar czf "${OUT}.tgz" "${OUT}"-*
  ```
  **Risk:** None (read-only capture). **Duration:** N/A. **Verification:** `${OUT}.tgz` contains the distribution config, 5xx metric window, TLS handshake, and curl timing; attach to the escalation ticket.

## Prevention

- Enable CloudFront additional metrics so `502ErrorRate`/`504ErrorRate` are queryable, and create a CloudWatch alarm on `5xxErrorRate` (statistic Average, namespace `AWS/CloudFront`) above a low threshold.
- Pin `OriginSslProtocols` to `TLSv1.2` and use ACM-managed certificates with auto-renewal on the origin (ALB) to prevent expiry and chain-order regressions.
- Attach the `com.amazonaws.global.cloudfront.origin-facing` managed prefix list to every origin security group so edge-IP range changes do not break connectivity.
- Set `OriginReadTimeout` based on the workload's measured p99 (up to 60s) and alert on origin TTFB approaching it; move long-running work to async endpoints.
- Review cache-behavior `PathPattern` ordering and `TargetOriginId` mappings on every distribution change; validate with `aws cloudfront get-distribution-config` in CI before deploying.

## Sources

- [HTTP 502 status code (Bad Gateway) — Amazon CloudFront](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/http-502-bad-gateway.html) — exact 502 error text, TLS/cipher negotiation failure, self-signed/expired cert and chain-order causes, SAN/CN domain-name match requirement, `X-Cache: Error from cloudfront`.
- [HTTP 504 status code (Gateway Timeout) — Amazon CloudFront](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/http-504-gateway-timeout.html) — 504 error text, firewall/security-group blocking, private/non-public origin, 30s connect default and origin response timeout, curl timing test.
- [Resolve "CloudFront wasn't able to connect to the origin" error — AWS re:Post](https://repost.aws/knowledge-center/resolve-cloudfront-connection-error) — origin connectivity and certificate-chain validation guidance.
- [Troubleshoot 504 errors in CloudFront — AWS re:Post](https://repost.aws/knowledge-center/cloudfront-troubleshoot-504-errors) — CloudFront managed prefix list for origin security groups, connectivity test commands.
- [Types of metrics for CloudFront — Amazon CloudFront](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/programming-cloudwatch-metrics.html) — `5xxErrorRate` definition, namespace `AWS/CloudFront`, `DistributionId` dimension, Region `Global`/us-east-1, per-code metrics.
- [Cache behavior settings — Amazon CloudFront](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/DownloadDistValuesCacheBehavior.html) — path-pattern first-match ordering and `TargetOriginId` routing behavior.
- [Origin settings — Amazon CloudFront](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/DownloadDistValuesOrigin.html) — `OriginProtocolPolicy`, `OriginSslProtocols`, connection/response timeout settings.
