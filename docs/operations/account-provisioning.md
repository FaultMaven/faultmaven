# Runbook: Creating an Account (Cloud)

**Applies to:** Cloud deployments (`AUTH_MODE=oauth`, `TENANT_PROVIDER=multi`, WorkOS AuthKit)
**Related:** [`sso-org-provisioning.md`](sso-org-provisioning.md) (the *tenant*),
[`security/service-account-credentials.md`](security/service-account-credentials.md) (the *credential*)
**Refs:** ADR-012, ADR-015, #869

This runbook is about the **account**. The two documents above cover the tenant
it lives in and the credential a non-human account authenticates with; this one
is the procedure that strings them together, and it is the one to follow when
somebody asks "how do I get X an account?".

## The thing to understand first

**In cloud mode FaultMaven cannot create a human account.** There is no
registration endpoint, no admin endpoint, and no script that does it:

- `POST /api/v1/auth/register` (and `/dev-register`) carry
  `Depends(require_local_mode)` and return **404 `endpoint_not_available`**
  whenever `AUTH_MODE != local`.
- `GET /api/v1/auth/config` advertises the truth: `"register_endpoint": null`,
  `"supports_registration": false`.
- **`POST /admin/users` does not exist.** The admin API can list, activate,
  deactivate and assign roles on accounts that already exist — it cannot mint
  one. It is also **confined to the operator's own organization** (#1318): under
  `TENANT_PROVIDER=multi` a platform admin bound to one tenant administers that
  tenant's users only, and another tenant's account answers exactly what an
  absent id answers.
- `scripts/auth/create_user.py` and `./faultmaven.sh create-user` are
  local/self-hosted development conveniences. They are not in the wheel, not in
  the container image, and not a deployment procedure.

Human accounts are created **in WorkOS**. FaultMaven creates its own `users` row
just-in-time on the first successful SSO callback. So "create an account" really
means: *make sure the identity exists in the IdP, and that a tenant is waiting
for it.*

Non-human accounts are the exact opposite: they are created **by FaultMaven**,
and WorkOS is never in their path.

## Which procedure do I want?

| The account is for | Use | WorkOS involved? |
|---|---|---|
| A person who signs in through a browser | [Human account](#human-account) | Yes — identity lives there |
| A simulator, agent, CI job, or any headless caller | [Service account](#service-account) | No |

Pick deliberately. A human account authenticates with an access token that
expires in **15 minutes** by default, which makes it a poor fit for anything
long-running or unattended; a service account holds a rotating refresh token and
needs no browser.

---

## Human account

### Order is not negotiable

Invite **and** provision the tenant **before** the person first signs in. There
is no just-in-time tenant creation — an organization is a billing and isolation
boundary, and an IdP claim is not authority to create one. A first sign-in with
no mapping fails closed with `?error=sso_org_unmapped`.

> ⚠️ Today that error renders in the dashboard as *"Sign-in failed. Please try
> again."* — advice that can never work (dashboard **#79**, open). Until it is
> fixed, a misordered onboarding looks to the user like a generic outage. Do not
> hand out a sign-in link before completing step 3.

### Step 1 — decide the tenant

Settled beta policy is **one WorkOS Organization per participant**, mapping 1:1
to a FaultMaven organization. Do not put unrelated participants in a shared
tenant: the FaultMaven organization *is* the RLS boundary, so co-locating two
parties pools their incident data, and a shared tenant means the deployment
never exercises the multi-tenant path at all.

Put an account in an **existing** tenant only when the person genuinely belongs
to that customer.

### Step 2 — create the WorkOS organization

In the WorkOS dashboard, in the **production** environment (ids differ per
environment and a staging id will never match), create the Organization and copy
its `org_…` id.

> ⚠️ **Verified domains grant automatic membership.** If a domain is verified on
> an organization, users signing up with that email domain can be joined to it
> automatically. Before issuing an address on a domain that is already attached
> to another organization, confirm which organization the account will actually
> land in — otherwise a new account silently joins an existing tenant instead of
> its own.
>
> Users on **any** domain (including personal addresses) can be invited as
> guests to an organization. A matching domain is a convenience, never a
> requirement — and using an unattached address is the simplest way to avoid the
> ambiguity above. For accounts handed to an outside party, that is the
> recommended default: see
> [Vendor, reviewer and demo accounts](#vendor-reviewer-and-demo-accounts).

### Step 3 — provision the FaultMaven tenant

Follow [`sso-org-provisioning.md`](sso-org-provisioning.md). In short — note
that the API pod's own `DATABASE_URL` is the limited application role and will
be refused, so the owner DSN is passed explicitly:

```bash
OWNER_DSN=$(kubectl -n faultmaven get secret faultmaven-db-privileged \
  -o jsonpath='{.data.MIGRATION_DATABASE_URL}' | base64 -d)

kubectl exec -it deploy/faultmaven-api -n faultmaven -- \
  env DATABASE_URL="$OWNER_DSN" \
  fm-provision-sso-org \
    --name "Acme Corp" --slug acme \
    --workos-org-id org_01HQZX9K3P4M5N6R7S8T9V0W1X
```

**Record the FaultMaven `organization_id` it prints.** It is a UUID, it is not
the `org_…` IdP id, and later steps need it.

Read that runbook's warnings about slug collisions before choosing `--slug`: a
slug that resolves onto an existing tenant binds the new IdP organization to
*that* tenant and pools its cases.

### Step 4 — invite the person

Enable an authentication method for them first. Beta uses AuthKit's own
**Email + Password** (owner decision — no paid enterprise-SAML connections
during beta). An organization with a member and a registered redirect URI still
cannot sign anyone in if no method is enabled.

Send the invitation from the WorkOS dashboard, or via the API:

```bash
curl -s -X POST https://api.workos.com/user_management/invitations \
  -H "Authorization: Bearer $WORKOS_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"email":"person@example.com","organization_id":"org_01H…"}'
```

The invitee opens the emailed link, accepts, and sets a password. **Sending is
not onboarding** — an unaccepted invitation leaves `accepted_at: null` and
`email_verified: false`, and the account cannot authenticate:

```bash
curl -s "https://api.workos.com/user_management/invitations?email=$EMAIL" \
  -H "Authorization: Bearer $WORKOS_API_KEY" \
  | jq '.data[] | {state, accepted_at, expires_at}'
# PASS: state "accepted", accepted_at non-null.
```

> ⛔ **The `Initiate login URI` trap.** With that field set, AuthKit's `/invite`
> link redirects through `user_management/initiate_login` to
> `/api/v1/auth/sso/login`. That is fine on a deployment where the SSO router is
> mounted, and a dead end (`{"detail":"Not Found"}`) on one where it is not —
> breaking the very invitation acceptance this step depends on. The router
> mounts only when `AUTH_MODE=oauth` *and* all three `WORKOS_*` values are
> non-empty.

Verify the membership — and query it with `statuses` **explicit**, because this
is the call that lies:

```bash
# The false all-clear: pending memberships are HIDDEN by default.
curl -s "https://api.workos.com/user_management/organization_memberships?organization_id=$ORG_ID" \
  -H "Authorization: Bearer $WORKOS_API_KEY" | jq '.data | length'

# The truth:
curl -s "https://api.workos.com/user_management/organization_memberships?organization_id=$ORG_ID&statuses=active,inactive,pending" \
  -H "Authorization: Bearer $WORKOS_API_KEY" | jq '.data[] | {user_id, status}'
# PASS: a row with status "active". "pending" means the invitation was never accepted.
```

Running both forms *is* the control: if they disagree, the default filter is
hiding something and the first number was never an answer.

### Step 5 — first sign-in creates the FaultMaven account

Have them sign in at the dashboard. On the first successful callback FaultMaven
JIT-provisions the `users` row and writes an `account_created` entry to
`user_audit_log`. What it creates is fixed by ADR-015 and is not configurable:

- **Username** = the sanitized email local-part, **suffixed on collision**
  (`-2`, `-3`, …). `alice@acme.com` normally becomes `alice`, but becomes
  `alice-2` if `alice` is taken. **Verify the derived username before using it**
  in any later command — promoting the wrong `alice` is a real outcome.
- **No password** (NULL) — WorkOS is the only way in.
- **Roles = `["user"]`**, never admin. There is no allowlist and no login path
  that grants elevated roles.

Matching is strict, on the IdP **subject** only. There is deliberately **no
email-based linking**: an existing unlinked account that already owns the email
is a hard conflict, not a link target.

> ‼️ A consequence worth telling participants up front: **changing their email
> is not a migration.** A different email is a different WorkOS user, hence a
> different subject, hence a **new** FaultMaven account — and their cases stay
> in the old organization behind a NOT NULL RLS key. Neither an identity-link
> nor an org-move tool exists.

### Step 6 — verify

1. They land in the dashboard, not back on the login page. Any `?error=` on the
   callback URL means it failed.
2. The session is scoped to the right tenant. Decode the access token (devtools
   → the `Authorization` header on any API call) and check:

   ```json
   "organization_id": "<the UUID step 3 printed>"
   ```

   An **empty** `organization_id` means no tenant resolved, and every subsequent
   request 403s with *"Request is not scoped to an organization."*
3. Confirm the account exists and is in the right shape:

   ```bash
   kubectl exec -it deploy/faultmaven-api -n faultmaven -- \
     python -c "import urllib.request,json; print(json.load(urllib.request.urlopen('http://localhost:8000/api/v1/auth/config')))"
   ```

   …and, as a platform admin, `GET /api/v1/admin/users` — which lists the users
   of **your own** organization, so run it as an operator bound to the tenant
   you just provisioned into.

### Step 7 — grant elevated roles (only if needed)

```bash
kubectl exec -it deploy/faultmaven-api -n faultmaven -- \
  fm-promote-platform-admin <derived-username>
```

`fm-promote-platform-admin` grants the operator set (`user` + `admin` +
`platform_admin`); `fm-demote-platform-admin` revokes `platform_admin` and
deliberately leaves the org-scoped `admin` alone. `platform_admin` is
deployment-scoped and cross-tenant — the user-management API cannot mint it.

> **Role changes are not retroactive.** They take effect at the **next token
> mint**. The person keeps their old roles until they sign out and back in, or
> until the access token expires. A promotion that "did not work" is usually
> this.

---

## Vendor, reviewer and demo accounts

Accounts handed to an outside party — a Chrome Web Store reviewer, an app-store
submission, a prospect demo — have a requirement ordinary onboarding does not:
**you** must control the mailbox, because you accept the invitation and set the
password, while *they* only ever receive the finished credentials. They must
never need inbox access to sign in.

**Convention: one controlled inbox, plus-addressed per purpose.**

```text
faultmavenuser@gmail.com           ← the single inbox you control
  faultmavenuser+cws@gmail.com     → Chrome Web Store reviewers
  faultmavenuser+apple@gmail.com   → Apple submission
  faultmavenuser+demo1@gmail.com   → a specific client demo
```

The base address deliberately carries **no dot** and does not say "test". The
alias is visible to whoever receives it, and the same inbox serves prospect
demos as well as store submissions — `+demo1` on a "test" address reads as
throwaway to someone you are selling to.

Why an outside address rather than a company one: **a verified domain grants
automatic membership**. An address on a domain already attached to an existing
organization can be auto-joined to *that* tenant instead of its own — which for
a vendor reviewer means landing inside your production tenant. An address on an
unattached domain removes the ambiguity rather than working around it.

This works because both sides key on the **exact** address:

- **WorkOS** uses the full email as the unique identifier and treats each alias
  as a separate user. Identity linking deduplicates different *credentials* for
  the same address; it never merges different addresses.
- **FaultMaven** strips characters outside `[a-z0-9._-]` when deriving the
  username, so the `+` is dropped rather than truncated at — the aliases stay
  distinct *and* readable:

  | Email | Derived username |
  |---|---|
  | `faultmavenuser+cws@gmail.com` | `faultmavenusercws` |
  | `faultmavenuser+apple@gmail.com` | `faultmavenuserapple` |
  | `faultmavenuser+demo1@gmail.com` | `faultmavenuserdemo1` |

  Had derivation truncated at the `+`, all three would collapse to
  `faultmavenuser` and be separated only by `-2`/`-3` suffixes. It does not —
  but this is exactly why step 5 says to verify the derived username.

### ‼️ Separate users are not separate tenants

Plus-addressing gives you distinct **users**, and nothing more. Under
`TENANT_PROVIDER=multi` each one still needs its **own WorkOS Organization and
its own `fm-provision-sso-org` mapping** (steps 2–3), or its first sign-in fails
closed with `sso_org_unmapped`.

Skipping that and pooling the aliases into one organization puts them in one RLS
tenant — so a client-demo account would sit in the same tenant as an app-store
reviewer, seeing the same organization's data. Budget roughly two minutes of
provisioning per alias; the addresses are free, the tenants are not.

### Before handing credentials to a reviewer

- **Confirm the sign-in needs no inbox access.** If AuthKit issues an email
  verification code on an unfamiliar device or geography — and an external
  reviewer is both — they stall behind a code only you can see, at a time you
  cannot predict. A store review that fails on a login wall is an expensive way
  to discover this. Test the flow from a different network first.
- **Gmail also ignores dots.** `faultmaven.user@gmail.com` reaches the same
  inbox as `faultmavenuser@gmail.com`, but FaultMaven derives a *different*
  username from it (`faultmaven.user`), so it becomes a second account rather
  than an error. Keeping the base address dot-free removes the ambiguity;
  write it down once and reuse it verbatim.
- **The inbox becomes the root of all vendor access.** Its own 2FA and recovery
  matter more than a throwaway account's usually would.

---

## Service account

For a simulator, agent, CI job, or any headless caller. WorkOS is never
involved: FaultMaven mints and owns these tokens. There is no
client-credentials grant and none is needed — the existing `refresh_token` grant
is already headless.

The account still needs a tenant, so complete steps 1–3 above (or reuse an
existing tenant deliberately). Then:

```bash
kubectl exec -it deploy/faultmaven-api -n faultmaven -- \
  fm-provision-service-account \
    --username sim-runner \
    --account-kind individual \
    --organization-id <faultmaven-organization-uuid> \
    --token-only
```

- `--organization-id` is **required** under `TENANT_PROVIDER=multi`, and takes
  the FaultMaven organization **UUID** — not the IdP's `org_…` id. Without it
  the credential would mint an empty organization claim and every request it
  makes would be refused at `bind_request_org_context`: dead on arrival, and
  only visible as the caller's first API call failing.
- The Standalone sentinel (`00000000-0000-0000-0000-000000000001`) is refused —
  it identifies the single-tenant *deployment*, not a tenant.
- `--account-kind` is `individual` or `slack` (ADR-012). Use `individual` for
  anything that is not the Slack agent.
- `--token-only` puts the token alone on stdout and progress on stderr, so it
  can be redirected straight into a secret without touching the terminal.

**The printed refresh token is shown once and is not recoverable.** Nothing
stores it server-side — refresh tokens are stateless JWTs, and only *revocations*
are stored (in Redis). Capture it directly into its destination.

The consumer exchanges it for access tokens at `POST /api/v1/auth/oauth/token`
with `grant_type=refresh_token`, receiving a rotated refresh token each time.
The organization claim rides that rotation. The endpoint takes RFC 6749 §3.2
form encoding or JSON, and answers refusals as RFC 6749 §5.2 objects — see
[service-account-credentials.md](security/service-account-credentials.md#how-the-credential-renews)
for the exact call.

> A consumer that expects a **static bearer token** (for example the simulator's
> `FM_SIM_AUTH_TOKEN`) needs an access token, not this refresh token — and
> access tokens expire in 15 minutes. Either teach it the refresh exchange, or
> mint a fresh access token at the start of each run.

Re-running the command on an existing account reuses it, keeping its `user_id`
so historical cases stay attached, and corrects `account_kind` if it is wrong.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `?error=sso_org_unmapped` | No `sso_org_mappings` row for the IdP org. Step 3 was skipped or used the wrong `org_…`. Currently renders as a generic "please try again" (dash **#79**) |
| `?error=sso_failed` with `reason=email_conflict` in logs | An existing unlinked account already owns that email. Deliberate: no email linking, and the browser gets a generic slug so it is not an account oracle |
| Login fails, `reason=enterprise_mismatch` | The account sits under the wrong enterprise. This is an **account migration**, not a configuration change — see the parent runbook |
| 403 *"Request is not scoped to an organization"* | Empty `organization_id` claim — the login resolved no tenant |
| Promotion appears to do nothing | Roles apply at next token mint. Sign out and back in |
| `fm-provision-sso-org` refuses, naming `faultmaven_app` | The `env DATABASE_URL=` override did not take effect |
| Membership query returns 0 for a user who was invited | The default filter hides `pending`. Re-query with `statuses=active,inactive,pending` |

## Removing access

```bash
# Report what would change, without writing:
kubectl exec -it deploy/faultmaven-api -n faultmaven -- \
  fm-remove-org-member --organization-id <uuid> --user <username> --dry-run

kubectl exec -it deploy/faultmaven-api -n faultmaven -- \
  fm-remove-org-member --organization-id <uuid> --user <username> --yes
```

`--user` accepts a username, an email address, or a user id. `--dry-run` and
`--yes` are mutually exclusive — passing both is a usage error, not a
preference.

This removes the membership **and** revokes that user's tokens as one operation
(#874) — important, because revoking membership alone leaves live access tokens
working until they expire. If a run removes the membership but fails to revoke,
recover it with `--finish-interrupted` rather than re-running.

Removing the WorkOS user or their organization membership stops future sign-ins,
but does **not** revoke tokens FaultMaven has already minted. Do both.
