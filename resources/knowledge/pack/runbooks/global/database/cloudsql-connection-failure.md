---
id: "cloudsql-connection-failure"
title: "GCP Cloud SQL connection refused or timed out from clients and Auth Proxy"
domain: database
service: gcp-cloud-sql
symptom_class: [connection_refused, timeout]
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-06-24"
verified_by: "kb-researcher"
status: draft
tags: [cloud-sql, auth-proxy, authorized-networks, private-ip, max-connections, connection-refused]
difficulty: intermediate
---

## Symptom Recognition

- Client error: `could not connect to server: Connection refused` / `Is the server running on host ... and accepting TCP/IP connections on port 5432?`
- Client error: `psql: error: connection to server ... failed: Connection timed out`
- Cloud SQL Auth Proxy stderr: `failed to connect to instance: Dial error: failed to dial ... dial tcp 10.x.x.x:3307: i/o timeout`
- Cloud SQL Auth Proxy stderr: `The proxy has encountered a terminal error: unable to start: ... TLS handshake`
- PostgreSQL log: `FATAL: remaining connection slots are reserved for non-replication superuser connections`
- PostgreSQL log: `FATAL: sorry, too many clients already`
- MySQL client: `ERROR 1040 (HY000): Too many connections`

## Applicability

- Service: Cloud SQL for PostgreSQL or MySQL (public IP, private IP, or Private Service Connect).
- Access: `roles/cloudsql.client` IAM role (grants `cloudsql.instances.connect`); `roles/cloudsql.admin` or `roles/cloudsql.viewer` to read instance config.
- Tools: `gcloud` CLI (authenticated), `cloud-sql-proxy` v2.x, `psql` or `mysql` client, `telnet`/`nc`.
- Cloud SQL Admin API (`sqladmin.googleapis.com`) must be enabled in the project.

## Diagnostic Steps

### Step 1: Confirm the instance is RUNNABLE and read its IP config

```bash
gcloud sql instances describe INSTANCE_ID \
  --format="value(state, ipAddresses[].ipAddress, settings.ipConfiguration.ipv4Enabled, settings.ipConfiguration.authorizedNetworks[].value)"
```

Expected output: `RUNNABLE` plus the public/private IPs and the list of authorized CIDR ranges.

### Step 2: Test raw TCP reachability to the database port

```bash
# Public IP clients use 5432 (PG) / 3306 (MySQL); the Auth Proxy dials the instance on 3307
telnet PUBLIC_OR_PRIVATE_IP 5432
```

Expected output: `Connected to ...`. `ping`/`traceroute` (ICMP) do NOT work with Cloud SQL and must not be used.

### Step 3: Run the Cloud SQL Auth Proxy in the foreground and read its stderr

```bash
./cloud-sql-proxy --port 5432 PROJECT_ID:REGION:INSTANCE_ID
```

Expected output: `Listening on 127.0.0.1:5432` and `The proxy has started successfully and is ready for new connections!`

### Step 4: Inspect active connection count against the configured limit

```bash
# PostgreSQL
gcloud sql connect INSTANCE_ID --user=postgres --quiet <<'SQL'
SELECT count(*) AS active, current_setting('max_connections')::int AS max_conn FROM pg_stat_activity;
SQL
```

Expected output: an `active` count and the `max_conn` limit; `active` near `max_conn` indicates exhaustion.

### Step 5: Verify the caller has the Cloud SQL client IAM role

```bash
gcloud projects get-iam-policy PROJECT_ID \
  --flatten="bindings[].members" \
  --filter="bindings.role=roles/cloudsql.client" \
  --format="value(bindings.members)"
```

Expected output: the service account / user email appears in the list of members bound to `roles/cloudsql.client`.

## Causes

### Cause A: Client IP is not in the instance authorized networks
**Statement:** The connecting client's public IP is outside every CIDR range in the instance's `authorizedNetworks` list, so Cloud SQL's edge drops the inbound TCP SYN for the public IP path.
**Chain:**
- root: client public IP absent from `authorizedNetworks` CIDR list
- s1: Cloud SQL edge silently drops inbound packets from the unlisted source
- s2: client TCP connect never completes the handshake
- D: connection refused / timed out (Symptom Recognition)
**Indicators:**
- root: [Step 1] the `authorizedNetworks` values do not contain the client's egress IP/CIDR.
- s2: [Step 2] `telnet PUBLIC_IP 5432` hangs then times out rather than connecting.
  <!-- match: {"step": 2, "predicate": "absent", "target": "Connected to"} -->
**Interventions:**
- **remediation** (root): add the client's egress CIDR to the authorized networks. Use valid CIDR notation; existing RFC 1918 ranges (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16) are implicit. NOTE: `--authorized-networks` REPLACES the full list, so include all current entries.

  ```bash
  gcloud sql instances patch INSTANCE_ID \
    --authorized-networks=EXISTING_CIDR_1,EXISTING_CIDR_2,CLIENT_IP/32
  ```

  **Verification:** re-run Step 1 and confirm `CLIENT_IP/32` appears, then re-run Step 2 and confirm `Connected to`.
- **mitigation** (s1): connect through the Cloud SQL Auth Proxy instead, which does not require authorized networks (it tunnels over the Admin API to port 3307).

  ```bash
  ./cloud-sql-proxy --port 5432 PROJECT_ID:REGION:INSTANCE_ID
  ```

  **Risk:** masks the missing firewall rule; other direct clients still fail. **Duration:** until the authorized-network entry is added. **Verification:** Step 3 prints `ready for new connections` and `psql -h 127.0.0.1 -p 5432` succeeds.

### Cause B: Auth Proxy lacks IAM permission or the Admin API is disabled
**Statement:** The identity running the Cloud SQL Auth Proxy is missing `roles/cloudsql.client` (or `sqladmin.googleapis.com` is disabled), so the proxy cannot fetch instance metadata or open the port-3307 tunnel.
**Chain:**
- root: caller missing `roles/cloudsql.client` or Admin API disabled
- s1: proxy's Admin API metadata/ephemeral-cert call is denied
- s2: proxy fails to establish the TLS tunnel to instance port 3307
- D: proxy exits / client connection refused (Symptom Recognition)
**Indicators:**
- root: [Step 5] the caller's email is absent from the `roles/cloudsql.client` members list.
  <!-- match: {"step": 5, "predicate": "absent", "target": "@"} -->
- s2: [Step 3] proxy stderr shows `failed to dial` / `dial tcp ...:3307` or `terminal error: unable to start`.
  <!-- match: {"step": 3, "predicate": "contains", "target": "terminal error"} -->
**Interventions:**
- **remediation** (root): enable the Admin API and grant the client role to the proxy's service account.

  ```bash
  gcloud services enable sqladmin.googleapis.com
  gcloud projects add-iam-policy-binding PROJECT_ID \
    --member=serviceAccount:SERVICE_ACCOUNT_EMAIL \
    --role=roles/cloudsql.client
  ```

  **Verification:** re-run Step 5 (email now listed) and Step 3 (proxy prints `ready for new connections`).
- **defensive_fix** (s2): pin an explicit credentials file so the proxy never silently falls back to the wrong ADC identity.

  ```bash
  ./cloud-sql-proxy --credentials-file PATH_TO_KEY_FILE --port 5432 PROJECT_ID:REGION:INSTANCE_ID &
  ```

  **Verification:** Step 3 succeeds with the intended service account and no `Dial error` lines appear.

### Cause C: Private IP path broken — outbound firewall or VPC peering routes missing
**Statement:** The client's VPC cannot reach the instance's private IP because either an egress firewall blocks TCP 3307 or the `cloudsql-postgres-googleapis-com` peering does not export the required routes.
**Chain:**
- root: egress firewall blocks 3307 OR Service Networking peering missing route export
- s1: packets to the instance private IP are dropped in the VPC
- s2: TCP handshake to the private IP never completes
- D: connection timed out (Symptom Recognition)
**Indicators:**
- root: [Step 1] `ipv4Enabled` is false / instance only exposes a private IP, yet the client is on a peered VPC.
- s2: [Step 2] `telnet PRIVATE_IP 5432` (or the proxy dialing 3307) times out with no response.
  <!-- match: {"step": 2, "predicate": "contains", "target": "timed out"} -->
**Interventions:**
- **remediation** (root): re-export the subnet routes (including public-IP routes) over the Service Networking peering so the private path resolves.

  ```bash
  gcloud compute networks peerings update cloudsql-postgres-googleapis-com \
    --network=NETWORK \
    --export-subnet-routes-with-public-ip \
    --project=PROJECT_ID
  ```

  **Verification:** re-run Step 2 against the private IP and confirm `Connected to`; the proxy (Step 3) reaches 3307.
- **defensive_fix** (s1): add an explicit egress allow rule for the Cloud SQL admin port so future firewall changes don't silently break the path.

  ```bash
  gcloud compute firewall-rules create allow-cloudsql-proxy-egress \
    --direction=EGRESS --action=ALLOW --rules=tcp:3307 \
    --destination-ranges=PRIVATE_SERVICES_CIDR --network=NETWORK
  ```

  **Verification:** Step 2 / Step 3 succeed; `gcloud compute firewall-rules describe allow-cloudsql-proxy-egress` shows the rule active.

### Cause D: Connection slots exhausted — max_connections limit reached
**Statement:** The number of established sessions has reached the instance `max_connections` flag, so the database refuses every new login until existing connections are released.
**Chain:**
- root: open connections reached the `max_connections` limit
- s1: server rejects new logins, reserving the last slots for superuser
- D: new client connections refused (Symptom Recognition)
**Indicators:**
- root: [Step 4] `active` count is at or near `max_conn`.
  <!-- match: {"step": 4, "predicate": "threshold", "field": "active", "op": ">=", "value": 100} -->
- s1: [Symptom] logs show `FATAL: remaining connection slots are reserved` (PG) or `ERROR 1040 (HY000): Too many connections` (MySQL).
  <!-- match: {"step": 4, "predicate": "contains", "target": "too many"} -->
**Interventions:**
- **remediation** (root): raise the `max_connections` flag (sized to instance memory) and front the DB with a pooler to cap real connections.

  ```bash
  gcloud sql instances patch INSTANCE_ID --database-flags=max_connections=200
  ```

  **Verification:** after the instance restart, re-run Step 4 and confirm `max_conn` is the new value and `active` is below it; new clients connect.
- **mitigation** (s1): free slots immediately by terminating idle sessions older than 10 minutes.

  ```sql
  SELECT pg_terminate_backend(pid)
  FROM pg_stat_activity
  WHERE state = 'idle' AND (now() - state_change) > interval '10 minutes'
    AND backend_type = 'client backend';
  ```

  **Risk:** kills in-flight idle transactions; clients must reconnect. **Duration:** minutes — load returns until pooling/limit is fixed. **Verification:** re-run Step 4; `active` drops and new connections succeed.

### Cause Z: Unidentified
**Statement:** The connection failure does not match Causes A–D after the diagnostic steps; root cause is not yet identified.
**Indicators:**
- [Default]
**Interventions:**
- **mitigation** (D): capture a full diagnostic snapshot and escalate to the database SME.

  ```bash
  gcloud sql instances describe INSTANCE_ID > cloudsql_snapshot.txt
  gcloud sql operations list --instance=INSTANCE_ID --limit=10 >> cloudsql_snapshot.txt
  gcloud logging read "projects/PROJECT_ID/logs/cloudsql.googleapis.com%2Fpostgres.log" --limit=50 >> cloudsql_snapshot.txt
  ./cloud-sql-proxy --port 5432 PROJECT_ID:REGION:INSTANCE_ID 2>> cloudsql_snapshot.txt
  ```

  **Risk:** none (read-only capture). **Duration:** n/a. **Verification:** `cloudsql_snapshot.txt` contains instance state, recent operations, server logs, and proxy stderr; attach it to the escalation.

## Prevention

- Always connect via the Cloud SQL Auth Proxy or a language Connector so credentials and TLS are managed and you avoid authorized-network churn.
- Front the database with a connection pooler (PgBouncer / Cloud SQL Managed Connection Pooling) and size `max_connections` to instance memory; alert when `active/max_connections` exceeds 0.8.
- Grant `roles/cloudsql.client` to the workload service account (not broad admin) and keep `sqladmin.googleapis.com` enabled in CI/Terraform.
- For private IP, codify the Service Networking peering and an explicit `tcp:3307` egress allow rule in Terraform so route exports survive network changes.
- Implement client retry with exponential backoff and jitter; set TCP keepalive (`net.ipv4.tcp_keepalive_time = 60`) to avoid `Aborted connection` drops.

## Sources

- [Diagnose issues](https://docs.cloud.google.com/sql/docs/postgres/diagnose-issues) — entry point confirming connection issues route to the debugging/troubleshooting guides.
- [Diagnose issues](https://docs.cloud.google.com/sql/docs/mysql/diagnose-issues) — MySQL diagnose index; authorized networks, private IP, Auth Proxy, pooling references.
- [Debugging connectivity](https://docs.cloud.google.com/sql/docs/postgres/debugging-connectivity) — telnet 3307/5432 checks, ICMP unsupported, pg_stat_activity, peering route export, keepalive sysctl.
- [Troubleshooting](https://docs.cloud.google.com/sql/docs/postgres/troubleshooting) — connectivity error strings (Connection refused, Aborted connection, cert errors), max_connections on replicas, authorized-network notes.
- [Authorize networks](https://docs.cloud.google.com/sql/docs/postgres/authorize-networks) — `gcloud sql instances patch --authorized-networks` syntax, CIDR rules, implicit RFC 1918 ranges.
- [Connect auth proxy](https://docs.cloud.google.com/sql/docs/postgres/connect-auth-proxy) — cloud-sql-proxy v2 install/invocation, `--port`/`--credentials-file`, `roles/cloudsql.client`, `gcloud services enable sqladmin.googleapis.com`, IAM binding command.
- [Debugging connectivity](https://docs.cloud.google.com/sql/docs/debugging-connectivity) — outbound firewall must allow port 3307 to the instance.
- [1840](https://github.com/GoogleCloudPlatform/cloud-sql-proxy/issues/1840) — verbatim proxy `failed to connect to instance: Dial error: failed to dial` / port 3307 timeout error string.
