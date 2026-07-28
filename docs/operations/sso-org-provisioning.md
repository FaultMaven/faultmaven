# Runbook: Provisioning an SSO Organization (Cloud)

**Applies to:** Cloud deployments (`TENANT_PROVIDER=multi`, `AUTH_MODE=oauth`, WorkOS AuthKit)
**Design:** [`docs/architecture/security/sso-org-mapping.md`](../architecture/security/sso-org-mapping.md)
**Refs:** #869, ADR-013, ADR-015

## When to run this

Run it **before** the first user of a new customer signs in.

Under multi-tenant, an SSO login lands in the FaultMaven organization that the
IdP's organization is mapped to. Until that mapping exists the login fails
closed — deliberately: there is no just-in-time tenant creation, because an
organization is a billing and isolation boundary and an IdP claim is not
authority to create one.

Symptom of the missing mapping: the user is bounced back to the dashboard login
page with `?error=sso_org_unmapped`, and the API logs

```text
event=sso_org_resolution_failed reason=org_unmapped provider=workos provider_org_id=org_01H…
```

That `provider_org_id` is the value you need below.

## Step 1 — find the WorkOS organization id

1. Sign in to the WorkOS dashboard and select the FaultMaven **production**
   environment (the environment selector is top-left; ids differ per
   environment, and a staging id will never match in production).
2. Go to **Organizations** and open the customer's organization (create it
   first if it does not exist, and configure their SSO connection or Directory
   Sync there).
3. Copy the **Organization ID** from the organization's detail page. It looks
   like `org_01HQZX9K3P4M5N6R7S8T9V0W1X`.

Sanity check: the users you expect to be able to sign in must be members of
*that* organization in WorkOS. FaultMaven trusts the IdP for organization
affiliation at login time; it does not second-guess it.

## Step 2 — provision the tenant and the mapping

The script creates, idempotently, the enterprise, the organization, its default
team, and the mapping row. Re-running it with the same arguments is a no-op that
prints the current state.

It must run with the **RLS-owning** database role (`faultmaven`), not the
limited application role (`faultmaven_app`): it writes rows for a tenant that
does not exist yet.

In Kubernetes:

```bash
kubectl exec -it deploy/faultmaven-api -n faultmaven -- \
  python scripts/auth/provision_sso_org.py \
    --name "Acme Corp" \
    --slug acme \
    --workos-org-id org_01HQZX9K3P4M5N6R7S8T9V0W1X
```

Locally, against a database URL you supply:

```bash
DATABASE_URL="postgresql+asyncpg://faultmaven:…@host/faultmaven" \
  python scripts/auth/provision_sso_org.py \
    --name "Acme Corp" --slug acme --workos-org-id org_01H…
```

Options:

| flag | meaning |
| --- | --- |
| `--name` | organization display name |
| `--slug` | URL-friendly slug, unique within the enterprise |
| `--workos-org-id` | the `org_…` id from step 1 |
| `--enterprise-id` | put the organization under an **existing** enterprise instead of creating one (use this for a second organization belonging to the same customer) |

Expected output ends with `✅ Tenant ready` and the three ids plus the mapping,
each marked `created` or `already present`.

### If it warns about reusing a tenant

The organization is resolved by `(enterprise, slug)` and the enterprise by slug,
so a `--slug` that collides with an existing customer's resolves onto **their**
tenant. When the script binds a new IdP organization to a tenant it did not
create in this run, it says so before writing:

```text
⚠️  REUSING AN EXISTING TENANT — confirm this is the right one.
    organization 2222…  (Acme Corp / acme) already existed
    workos:org_01J… is being bound to it, so its users will land in
    that tenant and see its cases. …
```

That is correct and expected when you are adding a **second** IdP organization
for a customer you already provisioned. If the name on the existing tenant is
not the customer you are onboarding, **stop** — re-run with a distinct `--slug`
(or an explicit `--enterprise-id`). Binding two customers to one organization
pools their cases.

### If it refuses: organization already claimed

```text
❌ FaultMaven organization 2222… is already claimed by a different workos organization.
   claimed by: org_01H…
   requested:  org_01J…
```

The tenant your `--slug` resolved to is already bound to another IdP
organization, and the mapping is 1:1 per provider. Nothing was written. This is
almost always the slug collision above, caught one step later. Re-provision the
new customer under a distinct slug.

## Step 3 — verify

1. Have the first user click **Sign in with SSO** on the dashboard.
2. They should land in the dashboard, not back on the login page. Any
   `?error=` on the callback URL means it failed — see Troubleshooting.
3. Confirm the session is scoped to the right tenant. Decode the access token
   the dashboard holds (browser devtools → the `Authorization` header on any
   API call) and check the claim:

   ```json
   "organization_id": "<the organization_id the script printed>"
   ```

   An empty `organization_id` means the login did not resolve a tenant and every
   subsequent request will 403 with *"Request is not scoped to an
   organization."*

4. Confirm membership landed:

   ```sql
   SELECT u.username, om.role_id
     FROM organization_members om
     JOIN users u USING (user_id)
    WHERE om.organization_id = '<organization_id>';
   ```

## Step 4 — bind an administrator

There is no allowlist and no login path that grants elevated roles (ADR-015 D5).
The first user signs in as an ordinary member; an operator promotes them
afterwards:

```bash
kubectl exec -it deploy/faultmaven-api -n faultmaven -- \
  python scripts/auth/promote_to_platform_admin.py <username>
```

Note the scope difference: `platform_admin` is the **deployment**-scoped
operator role and grants cross-tenant reach. If you only need a tenant
administrator, change their `organization_members.role_id` to the `admin` system
role instead.

The promotion takes effect on their next token mint — have them sign out and
back in, or wait out the access-token lifetime.

## Troubleshooting

### `?error=sso_org_unmapped`

Either the IdP sent no organization, or the one it sent is not mapped. Check the
logs for the reason slug:

- `reason=no_idp_org` — the WorkOS session is not organization-scoped. The user
  authenticated outside any organization (typically a personal AuthKit account,
  or an SSO connection not attached to an organization). Fix it in WorkOS.
- `reason=org_unmapped` — the `provider_org_id` in the log line has no mapping.
  Run step 2 with that id.

### `?error=sso_failed` with `reason=org_unavailable`

The mapping points at an organization that is missing, soft-deleted, or
deactivated. Check it:

```sql
SELECT organization_id, name, is_active, deleted_at
  FROM organizations
 WHERE organization_id = (
   SELECT organization_id FROM sso_org_mappings
    WHERE provider = 'workos' AND provider_org_id = 'org_01H…'
 );
```

Reactivate the organization (`is_active = true`, `deleted_at = NULL`) or repoint
the mapping (see below).

### `?error=sso_failed` with `reason=enterprise_mismatch`

The account already exists under a **different** enterprise than the mapped
organization's. This is refused on purpose: moving an account between
enterprises changes which customer owns their data, and it is never an implicit
consequence of an IdP claim.

Decide deliberately:

- **The account is in the right place and the mapping is wrong** — fix the
  mapping (below).
- **The account should move** — this is an account migration, not a login fix.
  Move `users.enterprise_id`, remove the stale `organization_members` row, and
  bump the user's revocation watermark so their existing tokens stop working.
  Their case data does **not** follow them; cases belong to the organization.
- **They are a genuinely separate person at a second customer** — they need a
  separate account with a separate IdP identity.

### Everything logs in fine but requests 403

*"Request is not scoped to an organization."* means the token carries an empty
`organization_id`. Under multi-tenant that is the deliberate fail-closed state —
the token was minted without a resolved tenant. Have the user sign in again
after the mapping exists; a token minted before the mapping does not heal on
refresh (by design).

## Operator procedures

### Repointing a mapping to a different organization

The provisioning script **refuses** to do this — it prints both organization ids
and exits non-zero. Remapping changes which tenant existing users land in on
their next login, so it is a deliberate act:

```sql
UPDATE sso_org_mappings
   SET organization_id = '<new_org_id>', updated_at = now()
 WHERE provider = 'workos' AND provider_org_id = 'org_01H…';
```

Then, for every affected user:

1. add them to the new organization (they will be added automatically on next
   login, but existing sessions will not);
2. bump their revocation watermark so outstanding tokens stop working —
   otherwise they keep operating in the old tenant until their tokens expire.

### Retiring a tenant

Deactivate the organization (`is_active = false`). Logins for it then fail with
the generic slug and `reason=org_unavailable`, and the mapping row can stay.
Deleting the organization cascades the mapping away.

### Revoking access for one user

Removing their `organization_members` row stops *future* logins from being
member-scoped, but it does not invalidate an outstanding token by itself.
Membership is verified at login only. Bump the user's per-user revocation
watermark to end the session immediately.
