---
id: "route53-resolution-failure"
title: "AWS Route 53 DNS resolution failures: resolver rules, alias records, failover, and PHZ association"
domain: networking
service: aws-route53
symptom_class: [connection_refused, timeout]
severity: high
scope: global
version: "1.0.1"
last_updated: "2026-08-17"
verified_by: "kb-researcher"
status: draft
tags: [route53, dns-resolver, private-hosted-zone, servfail, nxdomain, dns-failover]
difficulty: advanced
---

## Symptom Recognition

- Application errors: `Name or service not known`, `Temporary failure in name resolution`, `getaddrinfo ENOTFOUND <name>`.
- `dig <name>` returns `status: NXDOMAIN` (name does not exist) or `status: SERVFAIL` (resolver could not complete the query — usually a forwarding/timeout/path problem).
- `dig <name>` returns `;; connection timed out; no servers could be reached` against `169.254.169.253` (VPC+2) or a Resolver inbound endpoint IP.
- On-prem queries for a private domain hosted in a VPC time out, while in-VPC queries succeed.
- Failover record keeps serving the primary endpoint even though the endpoint is down; or returns no answer for a name that should fail over.
- CloudWatch `AWS/Route53Resolver` `EndpointHealthyENICount` drops below the number of configured IP addresses for an endpoint.

## Applicability

- AWS Route 53 (public + private hosted zones), Route 53 Resolver inbound/outbound endpoints and forwarding rules, Route 53 health checks / failover routing.
- Required IAM permissions (read-path): `route53:ListHostedZones`, `route53:ListResourceRecordSets`, `route53:GetHostedZone`, `route53:TestDNSAnswer`, `route53:GetHealthCheckStatus`, `route53resolver:ListResolverEndpoints`, `route53resolver:ListResolverRules`, `route53resolver:ListResolverRuleAssociations`, `ec2:DescribeVpcAttribute`, `ec2:DescribeSecurityGroups`, `ec2:DescribeNetworkAcls`. Write path additionally needs `route53:ChangeResourceRecordSets`, `route53:AssociateVPCWithHostedZone`, `route53resolver:AssociateResolverRule`, `ec2:ModifyVpcAttribute`, `ec2:AuthorizeSecurityGroupEgress`.
- Tools: AWS CLI v2 (`aws route53`, `aws route53resolver`, `aws ec2`, `aws cloudwatch`), `dig` (bind-utils / dnsutils) or `nslookup`, a shell on an EC2 instance inside the target VPC.

## Diagnostic Steps

### Step 1: Resolve the name directly against the VPC resolver and capture the status code

Run from an EC2 instance in the affected VPC. `169.254.169.253` is the VPC+2 Amazon-provided DNS (the `.2` address of the VPC CIDR also works).

```bash
dig +nocmd <name> any +noall +comments @169.254.169.253
dig <name> @169.254.169.253
nslookup <name> 169.254.169.253
```

Expected output: an `ANSWER SECTION` with the record and `status: NOERROR`. Note the exact `status:` value (`NOERROR` / `NXDOMAIN` / `SERVFAIL`) and whether the query times out.

### Step 2: Confirm VPC DNS attributes are enabled

```bash
aws ec2 describe-vpc-attribute --vpc-id <vpc-id> --attribute enableDnsSupport
aws ec2 describe-vpc-attribute --vpc-id <vpc-id> --attribute enableDnsHostnames
```

Expected output: both `EnableDnsSupport.Value` and `EnableDnsHostnames.Value` are `true`.

### Step 3: Verify the private hosted zone exists and is associated with the querying VPC

```bash
aws route53 list-hosted-zones-by-vpc --vpc-id <vpc-id> --vpc-region <region>
aws route53 list-resource-record-sets --hosted-zone-id <zone-id> \
  --query "ResourceRecordSets[?Name=='<name>.']"
```

Expected output: the PHZ for the domain appears in `HostedZoneSummaries`, and the queried record name/type is present in `ResourceRecordSets`.

### Step 4: Inspect Resolver endpoints, forwarding rules, and their VPC associations

```bash
aws route53resolver list-resolver-endpoints \
  --query "ResolverEndpoints[].{Id:Id,Dir:Direction,Status:Status,IpCount:IpAddressCount}"
aws route53resolver list-resolver-rules \
  --query "ResolverRules[].{Id:Id,Domain:DomainName,Type:RuleType,Target:TargetIps[].Ip,Ep:ResolverEndpointId}"
aws route53resolver list-resolver-rule-associations \
  --filters Name=VPCId,Values=<vpc-id>
```

Expected output: the inbound/outbound endpoint `Status` is `OPERATIONAL`; a `FORWARD`/`SYSTEM` rule for the domain exists with correct `TargetIps`; and that rule ID appears in the associations for `<vpc-id>`.

### Step 5: Check the outbound-endpoint security group and NACL egress for DNS

```bash
aws ec2 describe-security-groups --group-ids <resolver-ep-sg-id> \
  --query "SecurityGroups[].IpPermissionsEgress"
aws ec2 describe-network-acls --filters Name=association.subnet-id,Values=<ep-subnet-id> \
  --query "NetworkAcls[].Entries"
```

Expected output: the SG egress allows UDP and TCP port 53 to the target DNS server IPs; the NACL allows outbound UDP/TCP 53 to the DNS server and inbound UDP/TCP on the ephemeral range `1024-65535` from it.

### Step 6: Check failover record configuration and health-check status

```bash
aws route53 list-resource-record-sets --hosted-zone-id <zone-id> \
  --query "ResourceRecordSets[?Failover!=null].{Name:Name,FO:Failover,HC:HealthCheckId,Alias:AliasTarget.EvaluateTargetHealth}"
aws route53 get-health-check-status --health-check-id <health-check-id>
aws route53 test-dns-answer --hosted-zone-id <zone-id> \
  --record-name <name> --record-type A
```

Expected output: a `PRIMARY` and `SECONDARY` record exist; the primary has a `HealthCheckId` (or `EvaluateTargetHealth=true` for an ELB alias); `get-health-check-status` shows recent `Success`; `test-dns-answer` returns the value Route 53 would actually serve right now.

## Causes

### Cause A: VPC DNS attributes disabled
**Statement:** The querying VPC has `enableDnsSupport` and/or `enableDnsHostnames` set to false, so the Amazon-provided resolver at VPC+2 does not answer queries and private hosted zone records cannot resolve.
**Chain:**
- root: VPC `enableDnsSupport`/`enableDnsHostnames` is false
- s1: the VPC+2 Amazon-provided DNS resolver is unreachable / does not serve PHZ records
- D: name resolution fails in the application
**Indicators:**
- root: [Step 2] `EnableDnsSupport.Value` or `EnableDnsHostnames.Value` is `false`.
- s1: [Step 1] query to `169.254.169.253` times out or returns `SERVFAIL`.
**Interventions:**
- **remediation** (root): enable both VPC DNS attributes.

  ```bash
  aws ec2 modify-vpc-attribute --vpc-id <vpc-id> --enable-dns-support "{\"Value\":true}"
  aws ec2 modify-vpc-attribute --vpc-id <vpc-id> --enable-dns-hostnames "{\"Value\":true}"
  ```

  **Verification:** re-run Step 2 — both values are `true`; re-run Step 1 — `dig` returns `status: NOERROR` with an answer.

### Cause B: Private hosted zone not associated with the querying VPC
**Statement:** The private hosted zone for the domain is not associated with the VPC the client queries from (wrong VPC ID or never associated), so the resolver has no authoritative source and returns NXDOMAIN for in-zone names.
**Chain:**
- root: PHZ is missing the association for the querying VPC
- s1: the VPC resolver has no authoritative data for the domain
- s2: resolver answers from public DNS or not at all
- D: name resolution fails (NXDOMAIN) for the private name
**Indicators:**
- root: [Step 3] the domain's PHZ is absent from `list-hosted-zones-by-vpc` output for `<vpc-id>`.
- s2: [Step 1] `dig @169.254.169.253` returns `status: NXDOMAIN` for the private name.
**Interventions:**
- **remediation** (root): associate the VPC with the private hosted zone.

  ```bash
  aws route53 associate-vpc-with-hosted-zone --hosted-zone-id <zone-id> \
    --vpc VPCRegion=<region>,VPCId=<vpc-id>
  ```

  **Verification:** re-run Step 3 — the PHZ now appears for `<vpc-id>`; re-run Step 1 — the private record resolves with `status: NOERROR`.

### Cause C: Resolver forwarding rule misconfigured or not associated with the VPC
**Statement:** The conditional-forwarding rule for the domain is missing, points at the wrong target DNS IPs, references a non-operational outbound endpoint, or is not associated with the querying VPC, so queries for the forwarded domain are never delivered to the authoritative on-prem/peer DNS server.
**Chain:**
- root: forwarding rule is misconfigured or unassociated for the domain in `<vpc-id>`
- s1: queries for the domain are not forwarded to the correct target DNS servers
- s2: resolver returns SERVFAIL or times out
- D: name resolution fails for the forwarded domain
**Indicators:**
- root: [Step 4] no `FORWARD` rule for the domain is associated with `<vpc-id>`, or its `TargetIps`/`ResolverEndpointId` is wrong / not `OPERATIONAL`.
- s2: [Step 1] `dig` for the forwarded domain returns `status: SERVFAIL` or times out.
**Interventions:**
- **remediation** (root): point the rule at the correct target IPs and associate it with the VPC.

  ```bash
  aws route53resolver associate-resolver-rule \
    --resolver-rule-id <rule-id> --vpc-id <vpc-id> --name <assoc-name>
  ```

  **Verification:** re-run Step 4 — the rule shows correct `TargetIps` and appears in associations for `<vpc-id>`; re-run Step 1 — the forwarded name resolves with `status: NOERROR`.
- **defensive_fix** (s2): note that a Resolver forwarding rule takes precedence over a private hosted zone for the same domain; if both exist, remove the overlapping rule or scope it more specifically so the PHZ is not shadowed.

  ```bash
  aws route53resolver disassociate-resolver-rule --resolver-rule-id <overlapping-rule-id> --vpc-id <vpc-id>
  ```

  **Verification:** re-run Step 4 — no conflicting `FORWARD` rule shadows the PHZ domain; re-run Step 1 — the PHZ name resolves locally.

### Cause D: Outbound-endpoint security group or NACL blocks port 53
**Statement:** The security group on the outbound Resolver endpoint (or the subnet NACL) does not permit UDP/TCP port 53 egress to the target DNS servers and inbound ephemeral-port replies, so forwarded DNS queries are dropped on the path to the authoritative server.
**Chain:**
- root: SG egress or NACL blocks UDP/TCP 53 to the target DNS server (and ephemeral replies)
- s1: forwarded DNS packets are dropped on the network path
- s2: resolver times out waiting for the upstream answer (SERVFAIL)
- D: name resolution fails for the forwarded domain
**Indicators:**
- root: [Step 5] SG `IpPermissionsEgress` has no rule allowing UDP/TCP port 53 to the target DNS IPs, or NACL lacks the ephemeral inbound range.
- s2: [Step 1] `dig` for the forwarded domain returns `status: SERVFAIL` after a delay.
**Interventions:**
- **remediation** (root): allow UDP and TCP port 53 egress to the target DNS servers.

  ```bash
  aws ec2 authorize-security-group-egress --group-id <resolver-ep-sg-id> \
    --ip-permissions IpProtocol=udp,FromPort=53,ToPort=53,IpRanges='[{CidrIp=<dns-server-cidr>}]'
  aws ec2 authorize-security-group-egress --group-id <resolver-ep-sg-id> \
    --ip-permissions IpProtocol=tcp,FromPort=53,ToPort=53,IpRanges='[{CidrIp=<dns-server-cidr>}]'
  ```

  **Verification:** re-run Step 5 — egress shows UDP and TCP `ToPort: 53` to the DNS CIDR; re-run Step 1 — the forwarded name resolves with `status: NOERROR`.

### Cause E: Failover record served stale because Evaluate Target Health / health check is misconfigured
**Statement:** The primary failover record has no associated health check, or its ELB alias has `EvaluateTargetHealth=false`, so Route 53 cannot detect the primary as unhealthy and keeps returning it (or, when every record is unhealthy, reverts to serving all records as a last resort).
**Chain:**
- root: primary failover record lacks a working health check / `EvaluateTargetHealth` is false
- s1: Route 53 cannot determine the primary endpoint is unhealthy
- s2: queries continue to be answered with the dead primary (no failover to secondary)
- D: clients connect to a dead endpoint (connection refused / timeout)
**Indicators:**
- root: [Step 6] the `PRIMARY` record has no `HealthCheckId` and its alias `EvaluateTargetHealth` is `false`.
- s2: [Step 6] `test-dns-answer` still returns the primary endpoint while its health check shifts to `Failure`.
**Interventions:**
- **remediation** (root): attach a health check to the primary and enable `EvaluateTargetHealth` on the alias; set `EvaluateTargetHealth=false` on the secondary. Apply with a `change-resource-record-sets` batch.

  ```bash
  aws route53 change-resource-record-sets --hosted-zone-id <zone-id> \
    --change-batch file://failover-records.json
  ```

  **Verification:** re-run Step 6 — primary shows a `HealthCheckId`/`EvaluateTargetHealth=true`; force the primary unhealthy and confirm `test-dns-answer` returns the secondary.
- **mitigation** (s2): temporarily delete or disable the primary record so all traffic goes to the known-good secondary while the health check is fixed.

  ```bash
  aws route53 change-resource-record-sets --hosted-zone-id <zone-id> \
    --change-batch '{"Changes":[{"Action":"DELETE","ResourceRecordSet":{...primary...}}]}'
  ```

  **Risk:** removes all failover protection and is a manual cutover; a typo in the DELETE batch (values must match exactly) can fail or remove the wrong record. **Duration:** until the health check + `EvaluateTargetHealth` fix lands (hours). **Verification:** re-run Step 1 — `dig` returns only the secondary endpoint.

### Cause Z: Unidentified
**Statement:** None of the known causes match; the resolution failure stems from an unmodeled factor (DNSSEC validation failure, `.local`/link-local TLD handled specially, Resolver query throttling, DNS Firewall rule block, propagation delay, or an upstream authoritative-server fault).
**Indicators:**
- [Default]
**Interventions:**
- **mitigation** (D): capture a full diagnostic snapshot and escalate to the networking/DNS SME.

  ```bash
  TS=$(date +%Y%m%d-%H%M%S)
  { dig +trace <name>; dig <name> @169.254.169.253;
    aws route53resolver list-resolver-endpoints;
    aws route53resolver list-resolver-rules;
    aws route53resolver list-resolver-rule-associations --filters Name=VPCId,Values=<vpc-id>;
    aws route53 list-resource-record-sets --hosted-zone-id <zone-id>;
    aws cloudwatch get-metric-statistics --namespace AWS/Route53Resolver \
      --metric-name EndpointHealthyENICount --start-time "$(date -u -d '-1 hour' +%FT%TZ)" \
      --end-time "$(date -u +%FT%TZ)" --period 300 --statistics Minimum \
      --dimensions Name=EndpointId,Value=<endpoint-id>;
  } > route53-diag-$TS.txt 2>&1
  ```

  **Risk:** read-only snapshot, no production impact. **Duration:** N/A. **Verification:** `route53-diag-$TS.txt` is non-empty and attached to the escalation ticket.

## Prevention

- Enforce `enableDnsSupport=true` and `enableDnsHostnames=true` as part of VPC provisioning (Terraform/CloudFormation guardrails); audit with AWS Config rule `vpc-dns-resolution-enabled`.
- Manage PHZ-to-VPC associations and Resolver rule associations as code so a new VPC is never left unassociated; review overlapping FORWARD-rule vs PHZ domains (the rule wins) before deploy.
- Pin outbound-endpoint SG and subnet NACL rules (UDP/TCP 53 egress + ephemeral inbound) in IaC and protect them from drift.
- Enable Resolver query logging (`aws route53resolver create-resolver-query-log-config`) to a CloudWatch Logs group so SERVFAIL/NXDOMAIN patterns are queryable.
- Alarm on `AWS/Route53Resolver` `EndpointHealthyENICount < IpAddressCount` and on Route 53 health-check `HealthCheckStatus` flapping; alarm on `InboundQueryVolume`/`OutboundQueryVolume` anomalies.
- For failover records: always pair PRIMARY (`EvaluateTargetHealth=true` or a dedicated health check) with SECONDARY (`EvaluateTargetHealth=false`); never leave empty target groups behind an ELB alias (an empty target group is treated as unhealthy).

## Sources

- [Route 53 fix dns resolution private zone](https://www.repost.aws/knowledge-center/route-53-fix-dns-resolution-private-zone) — PHZ resolution: VPC DNS attributes, `list-hosted-zones-by-vpc`, VPC+2 / base-plus-two resolver, NXDOMAIN vs SERVFAIL, dig/nslookup QUESTION-SECTION check, rule-over-PHZ precedence, `.local` SERVFAIL.
- [Route 53 fix dns resolution resolver](https://repost.aws/knowledge-center/route-53-fix-dns-resolution-resolver) — Resolver endpoint troubleshooting: SG egress UDP/TCP 53, NACL outbound 53 + inbound ephemeral 1024-65535, inbound/outbound endpoint roles.
- [Route 53 resolver rules vpc](https://repost.aws/knowledge-center/route-53-resolver-rules-vpc) — Resolver rule association issues, most-specific-match rule selection, VPC+2 forwarding caveat.
- [List resolver rules](https://docs.aws.amazon.com/cli/latest/reference/route53resolver/list-resolver-rules.html) — `aws route53resolver list-resolver-rules` filters (HostVPCId, SecurityGroupIds) and output fields.
- [Dns failover determining health of endpoints](https://docs.aws.amazon.com/Route53/latest/DeveloperGuide/dns-failover-determining-health-of-endpoints.html) — health check determination; ELB Evaluate Target Health behavior, empty target group = unhealthy.
- [Dns failover problems](https://docs.aws.amazon.com/Route53/latest/DeveloperGuide/dns-failover-problems.html) — "all records unhealthy" last-resort behavior (Route 53 reverts to treating all as healthy).
- [Resource record sets values failover alias](https://docs.aws.amazon.com/Route53/latest/DeveloperGuide/resource-record-sets-values-failover-alias.html) — failover alias values; primary EvaluateTargetHealth=Yes, secondary No.
- [Monitoring resolver with cloudwatch](https://docs.aws.amazon.com/Route53/latest/DeveloperGuide/monitoring-resolver-with-cloudwatch.html) — `AWS/Route53Resolver` metrics (healthy/OPERATIONAL ENIs, inbound/outbound query volume).
- [List resource record sets](https://docs.aws.amazon.com/cli/latest/reference/route53/list-resource-record-sets.html) — `aws route53 list-resource-record-sets`, `test-dns-answer`, alias-record health semantics.
- [Resolver query logging](https://docs.aws.amazon.com/Route53/latest/DeveloperGuide/resolver-query-logs.html) — Resolver query logging destinations (CloudWatch Logs log group, S3, Firehose) and logged values including the DNS response code (`NoError`/`ServFail`) — grounds the Prevention step enabling query logging to CloudWatch Logs so response-code patterns are queryable.
