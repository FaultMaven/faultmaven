# Runbook: Provisioning an SSO Organization (Cloud)

**Applies to:** Cloud deployments (`TENANT_PROVIDER=multi`, `AUTH_MODE=oauth`, WorkOS AuthKit)
**Design:** [`docs/architecture/security/sso-org-mapping.md`](../architecture/security/sso-org-mapping.md)
**Refs:** #869, ADR-013, ADR-015

> Provisioning the **tenant** is one step of onboarding an account. For the
> end-to-end procedure — WorkOS organization, invitation, first sign-in, role
> grant — see [`account-provisioning.md`](account-provisioning.md).

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

`fm-provision-sso-org` creates, idempotently, the enterprise, the organization,
its default team, and the mapping row. Re-running it with the same arguments is
a no-op that prints the current state.

It is a console entrypoint shipped with the installed package
(`faultmaven/cli/provision_sso_org.py`), so it is on `PATH` in the API pod and
in any environment where `pip install -e .` has been run — no repository
checkout required.

It must run with the **RLS-owning** database role (`faultmaven`), not the
limited application role (`faultmaven_app`): it writes rows for a tenant that
does not exist yet, so row-level security has no policy that admits them.

**The API pod's own `DATABASE_URL` is the wrong role**, and deliberately so —
startup asserts that the application connects as a role RLS *applies* to
(`assert_app_db_role_enforces_rls`; a role exempt from RLS would silently defeat
tenant isolation). A bare `kubectl exec` inherits that environment, so the owner
DSN has to be passed explicitly. The command refuses before writing anything if
it isn't.

In Kubernetes — fetch the owner DSN from the privileged secret, then override
`DATABASE_URL` for this one invocation:

```bash
OWNER_DSN=$(kubectl -n faultmaven get secret faultmaven-db-privileged \
  -o jsonpath='{.data.MIGRATION_DATABASE_URL}' | base64 -d)

kubectl exec -it deploy/faultmaven-api -n faultmaven -- \
  env DATABASE_URL="$OWNER_DSN" \
  fm-provision-sso-org \
    --name "Acme Corp" \
    --slug acme \
    --workos-org-id org_01HQZX9K3P4M5N6R7S8T9V0W1X
```

On success the command echoes the role it verified
(`Database role: faultmaven (RLS-exempt — provisioning allowed)`). If you see a
refusal naming `faultmaven_app`, the `env` override did not take effect.

Locally, against a database URL you supply:

```bash
DATABASE_URL="postgresql+asyncpg://faultmaven:…@host/faultmaven" \
  fm-provision-sso-org \
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
    enterprise   3333…  (Acme / acme) already existed
    organization 2222…  (Acme Corp / acme) already existed
    workos:org_01J… is being bound to it, so its users will land in
    that tenant and see its cases. If this is a different customer, stop and
    re-provision under a distinct --slug.
```

That is correct and expected when you are adding a **second** IdP organization
for a customer you already provisioned. If the name on the existing tenant is
not the customer you are onboarding, **stop** — re-run with a distinct `--slug`
(or an explicit `--enterprise-id`). Binding two customers to one organization
pools their cases.

This warning is about the **organization**, which is the isolation boundary —
cases belong to it, so reusing one is what pools two customers' data. Creating a
new organization under an existing enterprise is a different, milder situation
and gets its own message, below.

### If it warns about the enterprise parent

With no `--enterprise-id`, the enterprise is resolved by `--slug` too. A slug
that matches an existing *enterprise* therefore parents the new organization
under it:

```text
⚠️  NEW ORGANIZATION UNDER AN EXISTING ENTERPRISE.
    enterprise   3333…  (Acme / acme) already existed and was matched
                 by --slug, not named with --enterprise-id.
    If this customer does not belong to that enterprise, stop and re-run
    with a distinct --slug (or an explicit --enterprise-id). …
```

Nothing is pooled — the organization is new and its cases are its own — so this
is not a data-isolation incident. It is flagged because it is expensive to
correct later: a user account created under the wrong enterprise fails login
closed with `reason=enterprise_mismatch`, and moving it is an account migration
(see that section below), not a configuration change.

It is silent when you pass `--enterprise-id` explicitly, because naming the
parent *is* the confirmation this message asks for.

An `--enterprise-id` that is present but empty — an unset shell variable in the
`kubectl exec` recipe above — is refused outright rather than treated as absent.
The two readings ("use the enterprise I named" and "resolve one from `--slug`")
lead to different tenants, so the script will not guess between them.

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

## Register the logout redirect (once per environment)

Signing out of FaultMaven does not end the IdP's session on its own — WorkOS
holds its own cookie on its own domain, and until it is ended the next sign-in
is answered without a prompt: the account cannot be switched, and the next
person at a shared browser is one click from being signed back in.

FaultMaven therefore hands the client a logout URL and asks WorkOS to return the
browser to the dashboard afterwards. **WorkOS refuses a `return_to` it does not
recognise**, so the dashboard origin has to be registered first:

1. WorkOS dashboard → **Redirects** → **Logout redirects**.
2. Add the dashboard origin — `https://app.faultmaven.ai` in production. It must
   match what the API has as `dashboard_url`; that is the value FaultMaven
   sends.
3. Set it as the default Logout URI too, so a logout that arrives without a
   `return_to` still lands somewhere sensible.

This is environment configuration, not per-customer: do it once, not per
organization. Skipping it does not break sign-in — only sign-out, and only at
the last hop, which is an easy failure to misread as "logout is broken".

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
  fm-promote-platform-admin <username>
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
  **This slug cannot occur while `SSO_JIT_PERSONAL_TENANT_ENABLED` is on** — that
  is the branch the switch redirects into personal-tenant provisioning. If you
  see it, the switch is off.
- `reason=org_unmapped` — the `provider_org_id` in the log line has no mapping.
  Run step 2 with that id. Unaffected by the switch: a company is onboarded
  deliberately, never by whoever signs in first.
- `reason=personal_account_already_anchored` — the switch is on, and an account
  that already belongs to an enterprise arrived with **no** IdP organization.
  Provisioning a personal tenant would lock them out of their company, so the
  login is refused. Fix it in WorkOS by scoping the session to their
  organization; do not touch the database.
- `reason=personal_tenant_retired` — the switch is on, and an operator retired
  this subject's personal tenant with `--next-login refuse` (the default). This
  is the refusal that choice asks for. To let them start over, re-run the
  retirement with `--next-login fresh-tenant` (see [Retiring a personal
  tenant](#retiring-a-personal-tenant)).

### Personal-tenant reason slugs (switch on only)

`SSO_JIT_PERSONAL_TENANT_ENABLED` adds one more way to reach the two existing
error slugs. The **slugs are unchanged** — a browser-visible vocabulary is a
cross-repo contract and this feature does not extend it — so the `reason=` in
the log is the only thing that tells these apart. Every personal-path refusal
logs one:

| logged `reason` | slug shown | what it means | operator action |
| --- | --- | --- | --- |
| `personal_account_already_anchored` | `sso_org_unmapped` | an account already in an enterprise arrived unscoped | scope the session in WorkOS |
| `personal_tenant_retired` | `sso_org_unmapped` | an operator retired this subject's tenant with `--next-login refuse` | intended; re-retire with `--next-login fresh-tenant` to let them start over |
| `personal_no_subject` | `sso_failed` | the IdP returned no stable subject | a provider fault; nothing to fix here |
| `personal_user_inactive` | `sso_user_inactive` | the account is deactivated or deleted | reactivate, or leave refused |
| `personal_unusable_email` | `sso_failed` | the IdP supplied no usable email address | fix the profile in WorkOS |
| `personal_email_conflict` | `sso_failed` | another, unlinked account owns that email | ADR-015 D4: never linked automatically; resolve the duplicate account |
| `personal_provisioning_ceiling` | `sso_failed` | `SSO_JIT_PERSONAL_TENANT_MAX_PER_HOUR` reached | expected under abuse; raise the ceiling only if the traffic is legitimate |
| `personal_org_is_sentinel` | `sso_failed` | a subject row points at the Standalone org (fm#850) | a data fault; investigate before clearing |
| `personal_org_unavailable` | `sso_failed` | the personal org is missing, soft-deleted or deactivated | reactivate it, or retire the tenant properly — see below. Do NOT delete the subject row by hand: that mints a second tenant while the IdP organization still holds the derived external id |
| `personal_org_repository_unwired` | `sso_failed` | the switch is on but the personal-tenant repository is not wired | a deployment fault; the login refuses rather than falling through |

`personal_org_unavailable` is what a **hand-made** retirement produces: soft-delete
the organization and every later login for that subject fails there, with no path
back — the enterprise does not cascade, the mapping and subject rows still point
at the dead tenant, and the WorkOS organization still holds the derived external
id, so the subject can never be given a new tenant either. `fm-personal-tenant
retire` is the supported way to do it; see [Retiring a personal
tenant](#retiring-a-personal-tenant).

A collision is logged as its own **event** rather than a `reason=`:
`sso_personal_tenant_collision`, carrying `colliding_key` and `colliding_value`.
It fires when a key this subject derives is already held by something that is
not a concurrent attempt by the same subject — start at the named key. Its
sibling `sso_personal_tenant_race_adopted` is the benign case (a concurrent
first login won; this one adopted its tenant) and needs no action.

Two more are informational rather than refusals: `sso_personal_membership_resuming`
(a previous attempt committed the tenant but not the IdP membership; this login
finished it) and `sso_personal_tenant_reanchored` (a mapped login moved an
account off its personal enterprise onto the company one, retiring the personal
binding — ADR-016 D5 as amended).

### `?error=sso_failed` with `reason=org_is_sentinel`

The mapping points at the **Standalone** organization
(`00000000-0000-0000-0000-000000000001`). Under multi-tenant that id identifies
the deployment, not a tenant, and binding it would pool logins into the
single-tenant sentinel (fm#850). Repoint the mapping at a real organization; do
not "fix" it by relaxing the guard.

This refusal applies to operator-provisioned mappings as well as personal
tenants — both resolution paths end in the same bind-and-verify tail, so neither
can lose a check the other has.

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
  Move `users.enterprise_id`, then drop the stale membership with
  `fm-remove-org-member` (see [Revoking access for one
  user](#revoking-access-for-one-user)), which also ends their existing sessions.
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

Then, for every affected user, remove them from the **old** organization with
`fm-remove-org-member` (see [Revoking access for one
user](#revoking-access-for-one-user)). That drops the stale membership and bumps
their revocation watermark in one step — without the watermark they keep
operating in the old tenant until their tokens expire. They are added to the new
organization automatically on their next login.

### Retiring a tenant

Deactivate the organization (`is_active = false`). Logins for it then fail with
the generic slug and `reason=org_unavailable`, and the mapping row can stay.
Deleting the organization cascades the mapping away.

That is the **company** tenant procedure. A personal tenant — one an org-less SSO
identity provisioned for itself — has a subject binding, a derived slug and a
WorkOS organization behind it, none of which that procedure touches, so it has
its own command.

### Retiring a personal tenant

```bash
kubectl exec -it deploy/faultmaven-api -n faultmaven -- \
  env DATABASE_URL="$OWNER_DSN" \
  fm-personal-tenant retire --subject user_01H…

kubectl exec -it deploy/faultmaven-api -n faultmaven -- \
  env DATABASE_URL="$OWNER_DSN" \
  fm-personal-tenant retire --subject user_01H… --apply
```

**Dry run is the default**: without `--apply` the command reads, prints every
side-effect it would apply, and writes nothing on either side. Pass the owner
DSN, as with `fm-provision-sso-org` and for the same reason — it resolves the
organization by a *derived slug*, which the RLS-scoped application role cannot
read. A preflight refuses before any write if the connected role is scoped.

Address the tenant by `--subject` (the IdP's opaque handle, `user_01H…`), by
`--organization-id`, or by both — naming both is a cross-check, and the command
refuses if they disagree. An organization whose slug was not derived from a
subject is refused outright: it is not a personal tenant.

**`--next-login` decides what that subject's next org-less sign-in gets:**

| Value | The next org-less login |
| --- | --- |
| `refuse` (default) | is refused, logging `reason=personal_tenant_retired` |
| `fresh-tenant` | provisions a brand-new personal tenant |

The account stays anchored to the retired enterprise either way —
`users.enterprise_id` is NOT NULL, so a retirement cannot clear it — so the
choice is *recorded* on the retired enterprise (in `enterprises.settings`) and
the login reads it there. The record is bound to the subject by the same derived
key the provisioning path uses, and a login honours it only after re-deriving
that key from its own identity: a marker on the wrong enterprise releases
nobody.

**What it does, and in this order** (each step idempotent, so re-running an
interrupted run finishes it rather than starting a different one):

1. soft-deletes the organization — the fence, after which no login can enter the
   tenant;
2. deletes the `sso_org_mappings` row;
3. deletes the `sso_personal_orgs` subject binding — before the WorkOS calls,
   because while it exists a login would ask WorkOS to *finish* the membership
   and re-create the organization step 4 removes;
4. deletes the WorkOS membership, then the WorkOS organization, freeing the
   derived `external_id`;
5. renames the organization's slug, freeing the derived slug;
6. soft-deletes and renames the enterprise and records the `--next-login`
   decision — last, because that record is what releases the anchor, and
   releasing it earlier would send the next login into a collision with the
   tenant being retired.

**Cases, evidence and knowledge items are NOT deleted**, and neither is the
organization row that owns them: it is soft-deleted and renamed. What a retired
tenant keeps, and for how long, is a retention question (ADR-014) and
deliberately not this command's.

WorkOS must be configured where the command runs. It refuses rather than
skipping the IdP half: leaving the WorkOS organization standing would keep the
derived external id claimed, so the subject could never be given a second
tenant — and the command would have reported a retirement it did not complete.

Read the exit code:

| Code | Meaning |
|------|---------|
| 0 | Done, or a dry run that reported what it would do |
| 1 | Refused — nothing was written |
| 2 | A bad flag (argparse usage error) — nothing was written |
| 3 | Nothing to do: already retired with the same `--next-login` policy |
| 4 | Incomplete: some steps landed and a later one failed. Re-run the same command to finish it |

### Re-anchoring a personal account to a company organization

A **mapped** login already does this by itself: an account anchored to its own
personal enterprise that signs in through a mapped company organization is
re-anchored, granted the `member` role, and has its personal binding retired.
Use the command when no such login is coming — the account has to be moved
before WorkOS knows about it, or its personal binding is already gone:

```bash
kubectl exec -it deploy/faultmaven-api -n faultmaven -- \
  env DATABASE_URL="$OWNER_DSN" \
  fm-personal-tenant re-anchor --subject user_01H… \
    --organization-id <company organization id> --apply
```

The target must be a **mapped** company organization: an unmapped one is refused
(no login could land in it, so the account would be stranded), and so is one that
is itself a personal tenant. The account must be anchored to its **own** personal
tenant's enterprise; moving one between company enterprises is a manual
migration, not this command.

The move is persisted *before* the binding is retired — the reverse order would
strand an account anchored to a personal enterprise whose subject row no longer
names it, which no login could repair. The personal tenant is left standing and
dormant: its cases stay where they are. The command prints the exact
`fm-personal-tenant retire --organization-id …` line to retire it when you are
ready. Same exit codes as above.

### Revoking access for one user

> **If you are offboarding someone, deprovision them at the IdP first.** Under
> SSO this procedure ends *sessions*; it does not on its own end *access*. The
> login path adds membership just-in-time, so a user who can still authenticate
> at WorkOS is silently re-added to the organization on their next login and
> issued fresh tokens. Remove them from the IdP organization (or disable the
> account) **before** running the command below, or you have logged them out
> rather than offboarded them.
>
> **Do not do this when you are moving someone** — that is, when you arrived
> here from the `reason=enterprise_mismatch` account-migration case or from
> [repointing a mapping](#repointing-a-mapping-to-a-different-organization).
> Those procedures *depend* on the next IdP login re-adding the user, to the new
> organization. Disabling their IdP account would block the very migration you
> are performing. There, run the command alone: it drops the stale membership
> and ends the sessions that still carry the old tenant.

Membership is verified at **login** only, so deleting the
`organization_members` row stops *future* logins from being member-scoped while
every outstanding token keeps working until it expires. The two writes have to
happen together, so run the command that does both:

```bash
kubectl exec -it deploy/faultmaven-api -n faultmaven -- \
  fm-remove-org-member --organization-id <organization_id> --user <username> --dry-run
kubectl exec -it deploy/faultmaven-api -n faultmaven -- \
  fm-remove-org-member --organization-id <organization_id> --user <username> --yes
```

`--user` takes a username, an email address, or a user id. The command removes
the membership and bumps the user's revocation watermark in one operation, then
prints the instant before which their tokens are now invalid.

A user who is not a member of that organization is **refused**, because that is
what a mistyped `--organization-id` looks like — `users` is not tenant-scoped, so
revoking anyway would end every session of an unrelated tenant's user while
removing nothing.

Read the exit code:

| Code | Meaning |
|------|---------|
| 0 | Done: membership removed, tokens revoked |
| 1 | Refused — nothing was written |
| 2 | A bad flag (argparse usage error) — nothing was written |
| 3 | Membership removed, revocation did **not** land: they are out of the org with live tokens. Re-run with `--finish-interrupted` |
| 4 | Revocation landed, membership row **not** deleted: sessions are dead but the row may survive, and with it access on the next login. Verify the row before treating them as removed |

Do **not** do this with two SQL statements. The second one is the one that
actually ends the session, and it is the one that gets forgotten.
