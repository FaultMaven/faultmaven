---
id: "gcs-access-denied"
title: "GCS 403 access denied — IAM, legacy ACL, signed-URL, CORS, and VPC-SC causes"
domain: storage
service: gcs
symptom_class: [auth_failure]
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-06-24"
verified_by: "kb-researcher"
status: draft
tags: [gcs-403, access-denied, iam, uniform-bucket-level-access, signed-url, vpc-service-controls]
difficulty: intermediate
---

## Symptom Recognition

- API/CLI error: `<identity> does not have storage.objects.get access to the Google Cloud Storage object` (HTTP 403).
- API/CLI error: `<identity> does not have storage.objects.list access to the Google Cloud Storage bucket` (e.g. on bulk delete / list).
- HTTP responses: `403 Forbidden`, `403 Permission Denied`, JSON `reason: "forbidden"`.
- Anonymous web access: `Anonymous caller does not have storage.objects.get access ...` / `AccessDenied`.
- Signed-URL request returns 403 with XML `<Code>SignatureDoesNotMatch</Code>` or `<Code>AccessDenied</Code>` (often after `X-Goog-Expires` elapsed).
- Browser console: `Access to fetch at 'https://storage.googleapis.com/...' from origin '...' has been blocked by CORS policy` (preflight `OPTIONS` fails / no `Access-Control-Allow-Origin`).
- gcloud-only error string: `Request is prohibited by organization's policy. vpcServiceControlsUniqueIdentifier: <UID>` (HTTP 403, `RESOURCE_NOT_FOUND`/`SERVICE_PERIMETER` violation).

## Applicability

- Service: Google Cloud Storage (JSON/XML API, `gcloud storage`, `gsutil`, client libraries, browser/signed-URL access).
- Access: `roles/storage.objectViewer`/`objectAdmin` for object ops; `roles/storage.admin` or `resourcemanager.*` to read IAM policy; `roles/accesscontextmanager.policyReader` to inspect VPC-SC perimeters.
- Tools: `gcloud` CLI (authenticated via `gcloud auth login` / ADC), `gsutil`, `curl`, a browser dev console for CORS.
- The Cloud Storage JSON API (`storage.googleapis.com`) must be enabled in the project.

## Diagnostic Steps

### Step 1: Reproduce the access and capture the exact identity + permission in the error

```bash
gcloud storage objects describe gs://BUCKET_NAME/OBJECT_PATH --verbosity=debug
```

Expected output: on failure, a 403 whose message names the acting identity and the missing permission, e.g. `caller@example.com does not have storage.objects.get access to the Google Cloud Storage object`. Note the email — it may differ from the identity you expected (`gcloud auth list` shows the active account).

### Step 2: Check the caller's IAM bindings on the bucket

```bash
gcloud storage buckets get-iam-policy gs://BUCKET_NAME \
  --format="json(bindings)"
# Project-level bindings (inherited) for the same identity:
gcloud projects get-iam-policy PROJECT_ID \
  --flatten="bindings[].members" \
  --filter="bindings.members:CALLER_EMAIL" \
  --format="value(bindings.role)"
```

Expected output: the roles granting the missing permission (e.g. `roles/storage.objectViewer` grants `storage.objects.get`) appear for `CALLER_EMAIL`. An empty result means no grant at bucket or project level.

### Step 3: Check whether uniform bucket-level access (UBLA) is enabled

```bash
gcloud storage buckets describe gs://BUCKET_NAME \
  --format="default(uniform_bucket_level_access, public_access_prevention)"
```

Expected output: e.g. `uniform_bucket_level_access: true`. When `true`, object/bucket ACLs are disabled and only IAM grants access; `public_access_prevention: enforced` blocks `allUsers`/`allAuthenticatedUsers`.

### Step 4: Inspect legacy ACLs on the object (only meaningful when UBLA is false)

```bash
gcloud storage objects describe gs://BUCKET_NAME/OBJECT_PATH \
  --format="default(acl)"
```

Expected output: with UBLA disabled, the per-object ACL entity/role list. With UBLA enabled this list is empty and ACL get/set calls return `400 Bad Request` — IAM is authoritative.

### Step 5: Validate a signed URL or CORS preflight against the bucket

```bash
# Signed URL: -v shows the HTTP status and any XML error body
curl -v "https://storage.googleapis.com/BUCKET_NAME/OBJECT_PATH?X-Goog-Signature=...&X-Goog-Expires=...&X-Goog-Date=..."
# CORS preflight as a browser would send it:
curl -v -X OPTIONS "https://storage.googleapis.com/BUCKET_NAME/OBJECT_PATH" \
  -H "Origin: https://app.example.com" \
  -H "Access-Control-Request-Method: GET"
# Current CORS config on the bucket:
gcloud storage buckets describe gs://BUCKET_NAME --format="default(cors_config)"
```

Expected output: a valid signed URL returns `200`; an expired/tampered one returns `403` with `<Code>SignatureDoesNotMatch</Code>` or `<Code>AccessDenied</Code>`. A matching CORS rule returns the `Access-Control-Allow-Origin` header; otherwise the preflight 403s and `cors_config` lacks an entry covering the Origin+method.

## Causes

### Cause A: Caller is missing the required IAM permission
**Statement:** The acting identity (user or service account) has no IAM binding — at bucket or any inherited level — granting the specific `storage.*` permission named in the 403, so Cloud Storage rejects the request.
**Chain:**
- root: identity lacks an IAM role granting the named `storage.objects.*` permission
- s1: the authorization check finds no allow binding for the resource
- D: 403 `does not have <permission> access` (Symptom Recognition)
**Indicators:**
- root: [Step 2] `CALLER_EMAIL` has no role (e.g. `roles/storage.objectViewer`) granting the permission at bucket or project level.
  <!-- match: {"step": 2, "predicate": "absent", "target": "roles/storage"} -->
- s1: [Step 1] the 403 message names this permission and the acting identity.
  <!-- match: {"step": 1, "predicate": "contains", "target": "does not have storage"} -->
**Interventions:**
- **remediation** (root): grant the least-privilege role that includes the missing permission to the caller on the bucket.

  ```bash
  gcloud storage buckets add-iam-policy-binding gs://BUCKET_NAME \
    --member="user:CALLER_EMAIL" \
    --role="roles/storage.objectViewer"
  ```

  **Verification:** re-run Step 2 (role now listed for `CALLER_EMAIL`), then re-run Step 1 and confirm the object describe returns metadata, not 403.

### Cause B: Uniform bucket-level access makes a relied-upon ACL grant ineffective
**Statement:** UBLA is enabled on the bucket, which disables all object/bucket ACLs, so an identity that previously had access only through a legacy ACL entry (and has no equivalent IAM binding) is now denied.
**Chain:**
- root: UBLA enabled, disabling ACLs while the identity's access came only from an ACL grant
- s1: the legacy ACL entry is ignored and no IAM binding covers the identity
- s2: the authorization check resolves to deny
- D: 403 access denied for an identity that "used to work" (Symptom Recognition)
**Indicators:**
- root: [Step 3] `uniform_bucket_level_access: true` on the bucket.
  <!-- match: {"step": 3, "predicate": "contains", "target": "uniform_bucket_level_access: true"} -->
- s1: [Step 4] the object ACL list is empty / ACL get returns `400 Bad Request`, and Step 2 shows no IAM role for the identity.
  <!-- match: {"step": 2, "predicate": "absent", "target": "roles/storage"} -->
**Interventions:**
- **remediation** (root): replace the lost ACL grant with an equivalent IAM binding (the durable model under UBLA).

  ```bash
  gcloud storage buckets add-iam-policy-binding gs://BUCKET_NAME \
    --member="serviceAccount:SA_EMAIL" \
    --role="roles/storage.objectViewer"
  ```

  **Verification:** re-run Step 2 (binding present) and Step 1 (access succeeds) while leaving UBLA enabled.
- **mitigation** (s1): temporarily disable UBLA to restore the legacy ACL path while IAM bindings are migrated.

  ```bash
  gcloud storage buckets update gs://BUCKET_NAME --no-uniform-bucket-level-access
  ```

  **Risk:** re-enables fine-grained ACLs org-wide on the bucket, widening the access surface and diverging from policy. **Duration:** only until equivalent IAM bindings are in place (hours, not permanent). **Verification:** Step 3 shows `uniform_bucket_level_access: false` and the ACL-based caller succeeds; revert with `--uniform-bucket-level-access` after migration.

### Cause C: Public access prevention blocks an allUsers/allAuthenticatedUsers grant
**Statement:** Public access prevention is enforced on the bucket (directly or via an inherited org policy), so any attempt to grant or use anonymous (`allUsers` / `allAuthenticatedUsers`) access is rejected and unauthenticated callers get 403.
**Chain:**
- root: `public_access_prevention: enforced` on the bucket or its org policy
- s1: public/anonymous principals are stripped or never honored
- D: `Anonymous caller does not have storage.objects.get access` / 403 (Symptom Recognition)
**Indicators:**
- root: [Step 3] `public_access_prevention: enforced` in the bucket describe output.
  <!-- match: {"step": 3, "predicate": "contains", "target": "public_access_prevention: enforced"} -->
- s1: [Step 1] the 403 names the principal as `Anonymous caller`.
  <!-- match: {"step": 1, "predicate": "contains", "target": "Anonymous caller"} -->
**Interventions:**
- **remediation** (root): if public access is genuinely required and permitted by org policy, set public access prevention to inherited and grant the public role; otherwise serve via signed URLs instead.

  ```bash
  gcloud storage buckets update gs://BUCKET_NAME --public-access-prevention=inherited
  gcloud storage buckets add-iam-policy-binding gs://BUCKET_NAME \
    --member="allUsers" --role="roles/storage.objectViewer"
  ```

  **Verification:** re-run Step 3 (`public_access_prevention: inherited`), then fetch the object anonymously and confirm `200`. If the org policy still enforces it, the update fails — keep prevention on and use a signed URL (Cause D path) instead.

### Cause D: Signed URL is expired or its signature does not match
**Statement:** The signed URL presented by the client has passed its `X-Goog-Expires` window or was built from a canonical request that does not match what Cloud Storage recomputes (wrong headers, host, or clock skew), so the signature check fails.
**Chain:**
- root: signed URL expired OR its canonical request/signature does not match the actual request
- s1: Cloud Storage's recomputed signature differs from the presented one
- D: 403 `SignatureDoesNotMatch` / `AccessDenied` on the signed URL (Symptom Recognition)
**Indicators:**
- root: [Step 5] the `X-Goog-Date` + `X-Goog-Expires` window has elapsed, or signed headers differ from those actually sent.
- s1: [Step 5] the 403 body contains `<Code>SignatureDoesNotMatch</Code>` (or `AccessDenied` for expiry).
  <!-- match: {"step": 5, "predicate": "contains", "target": "SignatureDoesNotMatch"} -->
**Interventions:**
- **remediation** (root): regenerate the signed URL with a fresh, sufficient expiry and signing identity, ensuring all custom headers included in the signature are sent verbatim (no extra spaces after colons).

  ```bash
  gcloud storage sign-url gs://BUCKET_NAME/OBJECT_PATH \
    --impersonate-service-account=SIGNER_SA_EMAIL \
    --http-verb=GET --duration=15m
  ```

  **Verification:** re-run Step 5 against the new URL and confirm `200` with the object body; no `SignatureDoesNotMatch` in the response.
- **defensive_fix** (s1): ensure the signing service account holds `iam.serviceAccountTokenCreator` so URL generation does not silently fall back to a stale local key, and keep generated durations short but adequate.

  ```bash
  gcloud iam service-accounts add-iam-policy-binding SIGNER_SA_EMAIL \
    --member="serviceAccount:CALLER_SA_EMAIL" \
    --role="roles/iam.serviceAccountTokenCreator"
  ```

  **Verification:** `gcloud storage sign-url` succeeds without credential errors and the resulting URL returns `200` within its window.

### Cause E: VPC Service Controls perimeter blocks the request
**Statement:** The Cloud Storage request crosses a VPC Service Controls perimeter boundary (caller or resource project is inside a perimeter the other side isn't authorized to reach), so the perimeter denies it regardless of valid IAM.
**Chain:**
- root: request violates a VPC-SC perimeter (caller/source not in an allowed access level or ingress rule)
- s1: the perimeter blocks the API call before IAM would otherwise allow it
- D: 403 `Request is prohibited by organization's policy` with a `vpcServiceControlsUniqueIdentifier` (Symptom Recognition)
**Indicators:**
- root: [Step 1] the gcloud 403 message includes `vpcServiceControlsUniqueIdentifier: <UID>` even though Step 2 shows the identity HAS the required IAM role.
  <!-- match: {"step": 1, "predicate": "contains", "target": "vpcServiceControlsUniqueIdentifier"} -->
- s1: [Step 2] IAM bindings are present and correct, yet access still fails — pointing past IAM to the perimeter.
  <!-- match: {"step": 2, "predicate": "contains", "target": "roles/storage"} -->
**Interventions:**
- **remediation** (root): take the unique ID to the security admin to locate the denial in audit logs and add the source (project/IP/identity) to an access level or ingress rule on the perimeter.

  ```bash
  gcloud logging read \
    'protoPayload.metadata."@type"="type.googleapis.com/google.cloud.audit.VpcServiceControlAuditMetadata"' \
    --project=PERIMETER_PROJECT_ID --limit=20 \
    --format="value(protoPayload.metadata.violations)"
  ```

  **Verification:** after the access level / ingress policy update propagates, re-run Step 1 and confirm the object describe returns metadata with no `vpcServiceControlsUniqueIdentifier` in the response.

### Cause Z: Unidentified
**Statement:** The 403 does not match Causes A–E after the diagnostic steps; the root cause is not yet identified.
**Indicators:**
- [Default]
**Interventions:**
- **mitigation** (D): capture a full diagnostic snapshot and escalate to the storage/security SME.

  ```bash
  {
    gcloud auth list
    gcloud storage buckets describe gs://BUCKET_NAME \
      --format="default(iam_config, uniform_bucket_level_access, public_access_prevention, cors_config)"
    gcloud storage buckets get-iam-policy gs://BUCKET_NAME --format=json
    gcloud storage objects describe gs://BUCKET_NAME/OBJECT_PATH --verbosity=debug 2>&1
  } > gcs_403_snapshot.txt
  ```

  **Risk:** none (read-only capture). **Duration:** n/a. **Verification:** `gcs_403_snapshot.txt` contains the active identity, bucket access config, IAM policy, and the verbose 403 trace; attach it to the escalation.

## Prevention

- Manage access exclusively through IAM with uniform bucket-level access enabled; treat legacy ACLs as deprecated and migrate any ACL-only grants to IAM bindings.
- Grant least-privilege predefined roles (`roles/storage.objectViewer` / `objectAdmin`) to workload service accounts, not broad `roles/storage.admin` or project-wide owner.
- Keep `public_access_prevention` enforced and serve external/anonymous access via short-lived signed URLs rather than `allUsers` grants.
- Generate signed URLs with the minimum adequate `--duration`, sign via `iam.serviceAccountTokenCreator` impersonation (no exported keys), and include only headers the client will actually send.
- Define the bucket CORS config in Terraform so Origin/method/headers used by browser clients always have a matching entry; clear browser preflight cache when testing changes.
- For VPC Service Controls, codify access levels and ingress/egress rules in Terraform, enable VPC-SC audit logging, and route the 403 unique identifier to security for fast perimeter diagnosis.

## Sources

- [Troubleshooting](https://docs.cloud.google.com/storage/docs/troubleshooting) — primary 403 troubleshooting page: exact error strings (`does not have storage.objects.get access`, `Anonymous caller`, `403 Forbidden`), IAM permission/identity checks, IAM deny policies, public access prevention, and CORS preflight/cache guidance.
- [Uniform bucket level access](https://docs.cloud.google.com/storage/docs/uniform-bucket-level-access) — UBLA disables ACLs; ACL get/set returns 400 and only IAM grants access when enabled.
- [Using uniform bucket level access](https://docs.cloud.google.com/storage/docs/using-uniform-bucket-level-access) — exact `gcloud storage buckets describe --format="default(uniform_bucket_level_access)"` check and enable/disable update commands.
- [Lists](https://cloud.google.com/storage/docs/access-control/lists) — legacy ACL model and its interaction with IAM/UBLA.
- [Signed urls](https://cloud.google.com/storage/docs/access-control/signed-urls) — signed URL structure, `X-Goog-Expires`, and `SignatureDoesNotMatch` from mismatched canonical requests (e.g. spaces after header colons).
- [Cross origin](https://cloud.google.com/storage/docs/cross-origin) — CORS preflight (`OPTIONS`) triggers and matching Origin/method/header rules; failed preflight blocks the primary request.
- [Troubleshooting](https://docs.cloud.google.com/vpc-service-controls/docs/troubleshooting) — 403 `Request is prohibited by organization's policy` with `vpcServiceControlsUniqueIdentifier`; using the UID to find audit-log violations.
- [Retrieve troubleshoot errors](https://cloud.google.com/vpc-service-controls/docs/retrieve-troubleshoot-errors) — retrieving VPC-SC violation details (`VpcServiceControlAuditMetadata`) from Cloud Audit Logs.
