---
id: "vault-sealed"
title: "HashiCorp Vault Sealed or Unavailable: Auto-Unseal, Raft Quorum, Token/Lease, and Policy Failures"
domain: security
service: hashicorp-vault
symptom_class: [service_unavailable, auth_failure]
severity: critical
scope: global
version: "1.0.0"
last_updated: "2026-06-24"
verified_by: "kb-researcher"
status: draft
tags: [vault-sealed, auto-unseal, raft-quorum, permission-denied, lease-expiry, kms]
difficulty: advanced
---

## Symptom Recognition

- `vault status` reports `Sealed   true`; `vault status` exit code is `2`.
- API/CLI requests fail with HTTP 503 and: `Vault is sealed`.
- Auto-unseal node fails to start; server log shows: `failed to unseal core` and `failed to decrypt encrypted root key`.
- Raft node returns: `local node not active but active cluster node not found`.
- Reads/writes return HTTP 403 with: `1 error occurred: * permission denied`.
- Renewals fail: `permission denied` on `auth/token/renew-self`; expired-token requests return `invalid token` / `permission denied`.
- `vault status` shows `HA Mode   standby` on every node (no active leader).

## Applicability

- Vault 1.3.0+ (Integrated Storage / Raft), CLI 1.8+ for full `vault status` Raft fields.
- Storage: Integrated Storage (Raft) clusters; auto-unseal via AWS KMS, Azure Key Vault, GCP Cloud KMS, or Transit.
- Access required: a valid root or admin token for `vault token capabilities` / `vault operator raft`; unseal/recovery key shares for Shamir; OS-level access to the Vault host and server config (`seal "..."` stanza).
- Tools: `vault` CLI, shell access to the Vault host, `VAULT_ADDR` set (e.g. `export VAULT_ADDR=https://127.0.0.1:8200`).

## Diagnostic Steps

### Step 1: Check seal and HA state

```bash
vault status
```

Expected output:

```
Key                     Value
---                     -----
Seal Type               shamir
Initialized             true
Sealed                  false
Total Shares            5
Threshold               3
Storage Type            raft
HA Enabled              true
HA Mode                 active
Raft Committed Index    52
Raft Applied Index      52
```

Note `Sealed`, `Seal Type`, `HA Mode`, and `Unseal Progress`. Exit code `0` = unsealed, `2` = sealed, `1` = error.

### Step 2: Inspect server startup logs for seal errors

```bash
journalctl -u vault --no-pager -n 200 | grep -iE "unseal|seal|root key|kms"
```

Expected output: no `failed to decrypt encrypted root key` / `failed to unseal core` lines on a healthy node; presence indicates an auto-unseal mechanism failure.

### Step 3: List Raft peers and identify the leader

```bash
vault operator raft list-peers
```

Expected output:

```
Node       Address            State     Voter
----       -------            -----     -----
node1      10.0.0.10:8201     leader    true
node2      10.0.0.11:8201     follower  true
node3      10.0.0.12:8201     follower  true
```

A healthy cluster shows exactly one `leader` and a voter majority alive.

### Step 4: Test token validity and effective capabilities

```bash
vault token lookup
vault token capabilities "$(vault print token)" secret/data/myapp
```

Expected output: `vault token lookup` returns a non-expired `ttl`/`expire_time`; `vault token capabilities` lists `read` (or required verbs) for the path, not `deny`.

## Causes

### Cause A: Auto-unseal mechanism (KMS/Transit) unreachable or misconfigured
**Statement:** The configured auto-unseal provider (AWS KMS, Azure/GCP KMS, or Transit) is unreachable, has an invalid `kms_key_id`/key reference, or the node lacks IAM/credentials to call it, so Vault cannot decrypt the root key at startup and stays sealed.
**Chain:**
- root: auto-unseal provider call fails (bad key id, missing IAM creds, or no network path to KMS)
- s1: Vault cannot decrypt the encrypted root key during core unseal
- s2: node remains sealed and refuses requests
- D: Vault sealed / unavailable (Symptom Recognition)
**Indicators:**
- root: [Step 2] log contains `failed to decrypt encrypted root key` (KMS/credential/network fault)
- s1: [Step 2] log contains `failed to unseal core`
- s2: [Step 1] `vault status` shows `Sealed   true` with `Seal Type` of `awskms`/`azurekeyvault`/`gcpckms`/`transit`
**Interventions:**
- **remediation** (root): Restore the seal provider — fix the `seal "awskms"` stanza (`kms_key_id`, `region`), restore IAM permissions/credentials, and confirm network reachability; then restart Vault to retry auto-unseal.

  ```bash
  vault operator unseal -status
  systemctl restart vault
  journalctl -u vault -f | grep -iE "unseal|root key"
  ```

  **Verification:** Re-run Step 1; `vault status` shows `Sealed   false` and exit code `0`.
- **mitigation** (s1): If the KMS outage is temporary and recovery keys exist, migrate to Shamir to unseal manually using the `-migrate` flag.

  ```bash
  vault operator unseal -migrate
  ```

  **Risk:** Recovery keys alone cannot decrypt the root key if the seal is permanently deleted; migration changes the cluster seal type and must be done on all nodes. **Duration:** Until the KMS/Transit dependency is restored; revert with another `-migrate`. **Verification:** Re-run Step 1; `Sealed   false`.

### Cause B: Raft quorum lost (no voter majority / no leader)
**Statement:** A majority of Raft voter peers are down or unreachable, so Integrated Storage cannot elect a leader and surviving nodes cannot transition to active, leaving the cluster unavailable.
**Chain:**
- root: voter-majority of Raft peers offline/unreachable (e.g. 2 of 3 voters down)
- s1: no Raft leader can be elected
- s2: surviving node stays standby and cannot serve writes
- D: Vault unavailable (Symptom Recognition)
**Indicators:**
- root: [Step 3] `vault operator raft list-peers` lists fewer than a voter majority reachable, or errors out
- s1: [Step 1] every node reports `HA Mode   standby` (no `active`)
- s2: [Step 2] log contains `local node not active but active cluster node not found`
**Interventions:**
- **remediation** (root): Bring failed voter nodes back online and rejoin so a majority of voters is restored.

  ```bash
  vault operator raft join "https://active-node:8200"
  vault operator raft list-peers
  ```

  **Verification:** Re-run Step 3; output shows one `leader` and a voter majority `true`; Step 1 shows one node `HA Mode   active`.
- **mitigation** (s1): If quorum is permanently lost (majority unrecoverable), force a single-node peers set via `raft/peers.json` on the surviving node to recover the cluster.

  ```bash
  cat > /opt/vault/data/raft/peers.json <<'EOF'
  [{"id":"node1","address":"10.0.0.10:8201","non_voter":false}]
  EOF
  systemctl restart vault
  ```

  **Risk:** Forcing peers discards the votes of excluded nodes and risks data loss if a stale node was actually ahead; take a snapshot first. **Duration:** Emergency recovery only; rebuild the cluster afterward. **Verification:** Re-run Step 3; surviving node becomes `leader`.

### Cause C: Token expired or lacks policy capabilities (permission denied)
**Statement:** The client token has expired (or its policy does not grant the required capability on the path, including `auth/token/renew-self`), so Vault denies the request even though the cluster is unsealed and healthy.
**Chain:**
- root: token TTL expired or its policy omits the capability for the requested path
- s1: Vault authorization check denies the operation
- s2: client request returns 403 permission denied
- D: auth_failure for the caller (Symptom Recognition)
**Indicators:**
- root: [Step 4] `vault token lookup` shows an expired/near-zero TTL, or `vault token capabilities` returns `deny` for the path
- s2: [Symptom] request returns `1 error occurred: * permission denied`
**Interventions:**
- **remediation** (root): Re-authenticate to obtain a fresh token and attach a policy that grants the needed capability (and `update` on `auth/token/renew-self` for renewable tokens).

  ```bash
  vault login -method=userpass username=appuser
  vault policy write app-policy - <<'EOF'
  path "secret/data/myapp" { capabilities = ["read"] }
  path "auth/token/renew-self" { capabilities = ["update"] }
  EOF
  ```

  **Verification:** Re-run Step 4; `vault token capabilities` lists `read` (not `deny`) and `vault token lookup` shows a positive TTL.
- **defensive_fix** (s1): Enable token auto-renewal so leases do not silently expire under the consuming app.

  ```bash
  vault token renew -increment=24h
  ```

  **Verification:** `vault token lookup` shows the extended `expire_time`; the request that returned 403 now succeeds.

### Cause D: Vault sealed by restart with Shamir keys not yet supplied
**Statement:** A Shamir-sealed node restarted and no operator has submitted the threshold of unseal key shares, so Vault is initialized but sealed and serving no requests.
**Chain:**
- root: node restarted with Shamir seal and unseal threshold not yet met
- s1: root key never reconstructed in memory
- s2: node stays sealed
- D: Vault sealed / unavailable (Symptom Recognition)
**Indicators:**
- root: [Step 1] `vault status` shows `Seal Type   shamir`, `Initialized   true`, `Sealed   true`, and `Unseal Progress` below `Threshold`
- s2: [Symptom] API requests return `Vault is sealed`
**Interventions:**
- **remediation** (root): Submit unseal key shares until the threshold is reached (each operator runs the command with their share, in any order).

  ```bash
  vault operator unseal
  vault operator unseal
  vault operator unseal
  ```

  **Verification:** Re-run Step 1; `Sealed   false`, `Unseal Progress 0/3`, exit code `0`.
- **defensive_fix** (root): Migrate this cluster to auto-unseal so restarts no longer require manual share entry.

  ```bash
  vault operator unseal -migrate
  ```

  **Verification:** Restart the node and confirm Step 1 shows `Sealed   false` without operator interaction.

### Cause Z: Unidentified
**Statement:** Vault is sealed or unavailable but none of the above root causes is confirmed by the diagnostics; an unmodeled condition (corrupted storage, partial network partition, version-specific bug) is in play.
**Indicators:**
- [Default]
**Interventions:**
- **mitigation** (D): Capture a full diagnostic snapshot and escalate to the Vault SME / platform on-call.

  ```bash
  vault status > /tmp/vault-status.txt 2>&1
  vault operator raft list-peers > /tmp/vault-peers.txt 2>&1
  vault debug -interval=1m -duration=10m -output=/tmp/vault-debug.tar.gz
  journalctl -u vault --no-pager -n 1000 > /tmp/vault-journal.txt
  ```

  **Risk:** `vault debug` collects metrics/config/profiling — review before sharing externally. **Duration:** Run once, then escalate immediately while the cluster is impaired. **Verification:** Snapshot files exist and are attached to the incident; SME has acknowledged.

## Prevention

- Auto-unseal: alert on the Vault `vault.core.unseal` and seal-provider error logs; monitor IAM/KMS key validity and KMS endpoint reachability from every node.
- Raft quorum: run an odd number of voters (3 or 5), spread across failure domains; alert on `vault operator raft autopilot state` `Healthy false` and on any node where `HA Mode` is `standby` cluster-wide for >60s.
- Snapshots: schedule `vault operator raft snapshot save` regularly so quorum loss is recoverable without `peers.json` surgery.
- Tokens/leases: grant `auth/token/renew-self` to renewable tokens and run Vault Agent auto-auth so app tokens renew before TTL expiry; alert on rising 403 rates in the audit log.
- Seal safety: never delete the KMS key while a cluster depends on it; keep recovery keys offline and test a seal migration in staging.

## Sources

- [Troubleshooting vault](https://developer.hashicorp.com/vault/tutorials/monitoring/troubleshooting-vault) — exact CLI commands (`vault status`, `vault operator unseal`, `vault operator raft list-peers`, `vault token capabilities`), permission-denied and missing-token error strings, `vault debug` package.
- [Seal](https://developer.hashicorp.com/vault/docs/concepts/seal) — Shamir vs auto-unseal, recovery vs unseal keys, `-migrate` flag, why a node is sealed after restart, KMS dependency warning.
- [Status](https://developer.hashicorp.com/vault/docs/commands/status) — exact `vault status` field layout (Raft Committed/Applied Index, HA Mode) and exit codes (0 unsealed, 1 error, 2 sealed).
- [Awskms](https://developer.hashicorp.com/vault/docs/configuration/seal/awskms) — AWS KMS seal stanza fields (`kms_key_id`, `region`) and failure modes for `failed to decrypt encrypted root key`.
- [Raft](https://developer.hashicorp.com/vault/docs/commands/operator/raft) — `vault operator raft list-peers` / `join` output (Node, Address, State leader/follower, Voter) and quorum semantics.
- [22575](https://github.com/hashicorp/vault/issues/22575) — `auth/token/renew-self` missing capability producing `1 error occurred: * permission denied` (403).
