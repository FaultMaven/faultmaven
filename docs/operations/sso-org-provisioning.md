# Runbook: Provisioning an SSO Organization (Cloud)

**Applies to:** Cloud deployments (`TENANT_PROVIDER=multi`, `AUTH_MODE=oauth`, WorkOS AuthKit)
**Design:** [`docs/architecture/security/sso-org-mapping.md`](../architecture/security/sso-org-mapping.md)
**Refs:** #869, ADR-017 (the enterprise isolates, the organization bills, the team shares — supersedes ADR-013's framing), ADR-015

> Provisioning the **tenant** is one step of onboarding an account. For the
> end-to-end procedure — WorkOS organization, invitation, first sign-in, role
> grant — see [`account-provisioning.md`](account-provisioning.md).

## When to run this

Run it **before** the first user of a new customer signs in.

Under multi-tenant, an SSO login lands in the FaultMaven **enterprise** that the
IdP's organization is mapped to (ADR-017 D9 — the mapping targets the
enterprise, the isolation boundary, not the organization it also provisions).
Until that mapping exists the login fails closed — deliberately: there is no
just-in-time tenant creation for a mapped IdP organization, because the
enterprise is the isolation boundary and an IdP claim is not authority to
create one.

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

This warning is about the **enterprise**, which is the isolation boundary —
cases belong to it, so reusing one is what pools two customers' data. Creating a
new organization under an existing enterprise is a different, milder situation
(the organization is only a billing target) and gets its own message, below.

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
   API call) and check the isolation claim:

   ```json
   "enterprise_id": "<the enterprise_id the script printed>"
   ```

   An absent `enterprise_id` claim means the login did not resolve a tenant, and
   the request is refused (403, *"Request is not scoped to an enterprise."*)
   rather than issued. `organization_id` will normally be **absent** too — a
   login does not put the user in the organization the script created; see the
   next step.

4. Confirm the anchor landed:

   ```sql
   SELECT username, enterprise_id FROM users WHERE username = '<username>';
   ```

   **The user is not automatically added to the organization.** Sign-in
   anchors the account to the enterprise only (ADR-017 D3) — an organization is
   a billing target created by payment (D5), and a login cannot know of one.
   If this customer needs the account billed to the organization the script
   created, add the membership deliberately (there is no CLI for this yet;
   write the `organization_members` row directly, or use the Cloud org
   console once it exists).

## Step 4 — bind an administrator

There is no allowlist and no login path that grants elevated roles (ADR-015 D5).
The first user signs in as an ordinary member; an operator promotes them
afterwards:

```bash
kubectl exec -it deploy/faultmaven-api -n faultmaven -- \
  fm-promote-platform-admin <username>
```

Note the scope difference: `platform_admin` is the **deployment**-scoped
operator role and grants cross-tenant reach. If you only need an organization
administrator, add (or update) their `organization_members` row with the
`admin` system role instead — this is a **billing-management** role (ADR-017
D5) and, unlike the isolation anchor, is not written automatically by sign-in.

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
  this subject's personal tenant with `--next-login refuse` (the default). The
  account is still anchored to the retired enterprise, which is what the login
  reads. To let them start over, re-run the retirement with
  `--next-login fresh-tenant` (see [Retiring a personal
  tenant](#retiring-a-personal-tenant)).
- `reason=personal_anchor_enterprise_deleted` — the account is anchored to a
  soft-deleted enterprise that carries **no** retirement policy: a company that
  was removed, not a personal tenant this command retired. Decide deliberately;
  do not clear the anchor by hand.
- `reason=personal_anchor_enterprise_missing` — the account's `enterprise_id`
  names a row that does not exist. A data fault; repair the row.

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
| `personal_anchor_enterprise_deleted` | `sso_org_unmapped` | anchored to a soft-deleted enterprise carrying no retirement policy | a removed company, not a retirement — decide deliberately |
| `personal_anchor_enterprise_missing` | `sso_org_unmapped` | `users.enterprise_id` names a row that does not exist | a data fault; repair the row |
| `personal_no_subject` | `sso_failed` | the IdP returned no stable subject | a provider fault; nothing to fix here |
| `signup_user_inactive` | `sso_user_inactive` | a pre-existing account (personal or domain arm) is deactivated or deleted | reactivate, or leave refused |
| `signup_unusable_email` | `sso_failed` | the IdP supplied no usable email address (personal or domain arm) | fix the profile in WorkOS |
| `signup_email_conflict` | `sso_failed` | another, unlinked account owns that email (personal or domain arm) | ADR-015 D4: never linked automatically; resolve the duplicate account |
| `personal_provisioning_ceiling` | `sso_failed` | `SSO_JIT_PERSONAL_TENANT_MAX_PER_HOUR` reached | expected under abuse; raise the ceiling only if the traffic is legitimate |
| `personal_is_sentinel` | `sso_failed` | the resolved enterprise is the Standalone sentinel (fm#850) | a data fault; investigate before clearing |
| `personal_unavailable` | `sso_failed` | the personal enterprise is missing or soft-deleted | reactivate it, or retire the tenant properly — see below. Do NOT delete the subject row by hand: that mints a second tenant while the IdP organization still holds the derived external id |
| `signup_repository_unwired` | `sso_failed` | the switch is on but the enterprise/personal-tenant repositories are not wired | a deployment fault; the login refuses rather than falling through |

`personal_unavailable` is what a **hand-made** retirement produces: soft-delete
the enterprise and every later login for that subject fails there, with no path
back — the mapping and subject rows still point at the dead tenant, and the
WorkOS organization still holds the derived external id, so the subject can
never be given a new tenant either. `fm-personal-tenant retire` is the
supported way to do it; see [Retiring a personal
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

The mapping points at the **Standalone** enterprise
(`00000000-0000-0000-0000-000000000002`). Under multi-tenant that id identifies
the deployment, not a tenant, and binding it would pool logins into the
single-tenant sentinel (fm#850). Repoint the mapping at a real enterprise; do
not "fix" it by relaxing the guard.

This refusal applies to operator-provisioned mappings as well as sign-up
(domain and personal) — all three resolution paths end in the same
bind-and-verify tail, so none can lose a check the others have. The reason
prefix differs by branch (`org_is_sentinel` / `domain_is_sentinel` /
`personal_is_sentinel`); the fix is the same.

### `?error=sso_failed` with `reason=org_unavailable`

The mapping points at an enterprise that is missing or soft-deleted. Check it:

```sql
SELECT enterprise_id, name, deleted_at
  FROM enterprises
 WHERE enterprise_id = (
   SELECT enterprise_id FROM sso_org_mappings
    WHERE provider = 'workos' AND provider_org_id = 'org_01H…'
 );
```

Un-delete the enterprise (`deleted_at = NULL`) or repoint the mapping (see
below). Enterprises carry no `is_active` flag — soft-delete (`deleted_at`) is
the only "off" switch.

### `?error=sso_failed` with `reason=enterprise_mismatch`

The account already exists under a **different** enterprise than the mapped
organization's. This is refused on purpose: moving an account between
enterprises changes which customer owns their data, and it is never an implicit
consequence of an IdP claim.

Decide deliberately:

- **The account is in the right place and the mapping is wrong** — fix the
  mapping (below).
- **The account should move** — this is an account migration, not a login fix.
  Move `users.enterprise_id` (`fm-personal-tenant re-anchor`, or by hand for a
  non-personal account), then drop any stale organization membership with
  `fm-remove-org-member` (see [Revoking access for one
  user](#revoking-access-for-one-user)), which also ends their existing sessions.
  Their case data does **not** follow them; cases belong to the enterprise.
- **They are a genuinely separate person at a second customer** — they need a
  separate account with a separate IdP identity.

### Everything logs in fine but requests 403

*"Request is not scoped to an enterprise."* means the token carries no usable
`enterprise_id` claim. Under multi-tenant that is the deliberate fail-closed
state — the token was minted without a resolved tenant. Have the user sign in
again after the mapping exists; a token minted before the mapping does not heal
on refresh (by design — there is no fallback to reading `users.enterprise_id`
for an old token, ADR-017).

## Operator procedures

### Repointing a mapping to a different enterprise

The provisioning script **refuses** to do this — it prints both enterprise ids
and exits non-zero. Remapping changes which tenant existing users land in on
their next login, so it is a deliberate act:

```sql
UPDATE sso_org_mappings
   SET enterprise_id = '<new_enterprise_id>', updated_at = now()
 WHERE provider = 'workos' AND provider_org_id = 'org_01H…';
```

Then, for every affected user, revoke their sessions so the old tenant's tokens
stop working (a bumped revocation watermark — the only automatic membership
write a login makes is the enterprise anchor, so there is no organization
membership row to drop here unless one was separately added). They are
re-anchored to the new enterprise automatically on their next login.

### Retiring a tenant

Soft-delete the enterprise (`enterprises.deleted_at = now()` — there is no
`is_active` flag on `enterprises`). Logins for it then fail with the generic
slug and `reason=org_unavailable`, and the mapping row can stay (it does
**not** cascade away on its own; `sso_org_mappings.enterprise_id` has
`ON DELETE CASCADE`, but a soft delete is not a `DELETE`).

That is the **company** tenant procedure. A personal tenant — one an org-less SSO
identity provisioned for itself — has a subject binding, a derived slug and a
WorkOS organization behind it, none of which that procedure touches, so it has
its own command.

### Retiring a personal tenant

```bash
kubectl exec -it deploy/faultmaven-api -n faultmaven -- \
  env DATABASE_URL="$OWNER_DSN" \
  fm-personal-tenant retire --subject user_01H...

kubectl exec -it deploy/faultmaven-api -n faultmaven -- \
  env DATABASE_URL="$OWNER_DSN" \
  fm-personal-tenant retire --subject user_01H... --apply
```

**Dry run is the default**: without `--apply` the command reads, prints every
side-effect it would apply, and writes nothing on either side. Pass the owner
DSN, as with `fm-provision-sso-org` and for the same reason — it reads and
writes rows of a tenant across RLS-tenanted tables without binding it. A
preflight refuses before any write if the connected role is scoped.

**Addressing.** A **live** tenant is addressed by `--subject`, through its
binding row. A retirement stamps that binding as retired early, so a tenant
that is already part-retired is addressed by `--enterprise-id` — the id the
command prints on every run, and repeats in the "finish this retirement with"
line if a step fails. Naming both is a cross-check; the command refuses if
they disagree. An enterprise whose slug was not derived from an IdP subject is
refused: it is not a personal tenant.

**`--next-login` decides what that subject's next org-less sign-in gets:**

| Value | What is written | The next org-less login |
| --- | --- | --- |
| `refuse` (default) | the account stays anchored to the retired enterprise | refused, logging `reason=personal_tenant_retired` |
| `fresh-tenant` | nothing on the account — `users.enterprise_id` is NOT NULL (ADR-017 D3) and is never cleared | provisions a brand-new personal tenant on the next org-less sign-in, which re-anchors the account itself |

The whole retirement state is **typed columns**: `deleted_at` on the enterprise,
and `sso_personal_enterprises.retired_at` / `retirement_state` for the
operator's choice. The login's verdict is a column read: an unreadable or
unexpected value can never produce the permissive answer.

Nothing is renamed. The slug uniqueness index is partial on
`deleted_at IS NULL`, so a retired tenant keeps its slug and the subject's next
tenant derives the same one.

**What it does, and in this order** (each step idempotent, so re-running an
interrupted run finishes it):

1. soft-deletes the enterprise — the fence, after which no login can enter the
   tenant;
2. **revokes every outstanding token** of the accounts anchored to the tenant.
   The callback is not the only way in: a live refresh chain keeps minting for a
   tenant whose enterprise row is gone;
3. stamps the `sso_personal_enterprises` row as retired (keeping it, with
   `retired_at` + `retirement_state`) — before the provider calls, because
   while it looks live a login will ask the provider to *finish* a membership
   and re-create the IdP organization step 4 removes;
4. deletes the provider membership and organization, addressed by the
   `provider_org_id` **this tenant's mapping row records** — never by an id
   re-derived from the subject, which a later tenant of the same subject would
   also answer to;
5. deletes the `sso_org_mappings` row — after step 4, so the recorded id is
   still readable, and so the derived external id is free for a later tenant.

**Cases, evidence and knowledge items are NOT deleted**, and neither is the
enterprise row that owns them. What a retired tenant keeps, and for how long,
is a retention question (ADR-014) and deliberately not this command's.

WorkOS must be configured where the command runs, and a usable revocation store
must be wired. It refuses rather than skipping either: a retirement that leaves
the provider organization standing keeps the derived external id claimed, and
one that leaves tokens live has not retired anything.

Read the exit code:

| Code | Meaning |
|------|---------|
| 0 | Done, or a dry run that reported what it would do |
| 1 | Refused — nothing matched, or a guard tripped; nothing was written |
| 2 | A bad flag (argparse usage error) — nothing was written |
| 3 | Nothing to do: already retired with the same `--next-login` policy |
| 4 | Incomplete: some steps landed and a later one failed. Re-run the printed `--enterprise-id` command to finish it |

### Re-anchoring a personal account to a company enterprise

A **mapped** login already does this by itself for an account anchored to
nothing, to a retired personal enterprise, or to its own live personal tenant.
Use the command when no such login is coming:

```bash
kubectl exec -it deploy/faultmaven-api -n faultmaven -- \
  env DATABASE_URL="$OWNER_DSN" \
  fm-personal-tenant re-anchor --subject user_01H... \
    --enterprise-id <company enterprise id> --apply
```

The target must be a **mapped** company enterprise: an unmapped one is refused
(no login could land in it, so the account would be stranded), and so is one
that is itself a personal tenant. A deactivated account is refused — it would be
a member that cannot sign in. Re-anchoring moves only the account's isolation
membership (`users.enterprise_id`); it writes no organization membership — that
remains a billing fact a human grants separately (ADR-017 D5).

The move is persisted *before* the binding is retired; the reverse order strands
an account anchored to a personal enterprise whose subject row no longer names
it. The personal tenant is left standing and dormant, and the command prints the
`retire --enterprise-id …` line to retire it when you are ready.

**No login ever moves an already-anchored account onto a personal enterprise.**
Anchors move toward company enterprises only; setting an absent anchor is not a
move. That direction is refused by the single anchor-mover both the login and
this command use.

### Cleaning up a provider-side organization

Provisioning creates the WorkOS organization *before* the database transaction,
so an attempt that minted one and then failed to commit leaves an organization
no tenant claims. Remove it by its **explicit** id:

```bash
fm-personal-tenant purge-idp-org --provider-org-id org_01H... --apply
```

The id is deliberately not derived from a subject: a derived id also names
whatever tenant that subject holds *now*. The command refuses an id that a live
mapping still points at, and tells you to retire that tenant instead.

### Revoking access for one user

> **This removes billing-roster membership, not enterprise access.** Under
> ADR-017 a login never adds anyone to an organization automatically — that
> stopped being a JIT side-effect of sign-in — so removing the
> `organization_members` row and killing the user's sessions is enough to end
> *this organization's* claim on the account and its access to whatever the
> organization role gated. It does **not** end the account's ability to sign
> back in and land in the **enterprise** with no organization: that is the
> isolation anchor (`users.enterprise_id`), which this command does not touch.
> **If you are offboarding someone entirely, deprovision them at the IdP** (or
> deactivate the FaultMaven account) — that is what ends their ability to
> authenticate at all.
>
> **Do not do this when you are moving someone between enterprises** — that is,
> when you arrived here from the `reason=enterprise_mismatch` account-migration
> case or from [repointing a mapping](#repointing-a-mapping-to-a-different-enterprise).
> Those procedures re-anchor the account on its next mapped login; this command
> only ever touches organization membership and sessions, so it is orthogonal
> to that move — run it if the account also needs dropping from an
> organization it no longer belongs to.

Organization membership is verified at **login** only for the org-console
surfaces it gates, so deleting the `organization_members` row stops *future*
authorization checks that read it while every outstanding token keeps working
until it expires. The two writes have to happen together, so run the command
that does both:

```bash
kubectl exec -it deploy/faultmaven-api -n faultmaven -- \
  fm-remove-org-member --enterprise-id <enterprise_id> --organization-id <organization_id> --user <username> --dry-run
kubectl exec -it deploy/faultmaven-api -n faultmaven -- \
  fm-remove-org-member --enterprise-id <enterprise_id> --organization-id <organization_id> --user <username> --yes
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

### Changing one tenant's daily turn cap

The **billing subject** (ADR-017 D5) — an account's organization when it has
one, the account itself when it does not — is what the cap keys on. An account
in no organization is capped at `TENANT_DAILY_TURN_CAP` (default **30**)
investigation turns per UTC day; an organization is uncapped unless given an
override, and a **single-tenant (self-hosted) deployment is never capped at
all** — that is decided from the deployment mode, so an install that has not
run the migration keeps serving turns. A subject at its cap is refused with a
429 that names the limit and the reset instant — its reads, its sign-in and the
knowledge base are unaffected. The design is in
[SSO Enterprise Mapping → The daily turn cap](../architecture/security/sso-org-mapping.md#the-daily-turn-cap).

Read the current state first — it also prints how much of today the subject has
already used, which is usually the number you actually wanted. `--enterprise-id`
is required (it is what every read and write below is RLS-scoped by); the
subject is named with exactly one of `--organization-id` or `--account-id`
(`--account-id` is read-only — there is no row on an account to write an
override to):

```bash
kubectl exec -it deploy/faultmaven-api -n faultmaven -- \
  fm-set-turn-cap --enterprise-id <enterprise_id> --organization-id <organization_id> --show
```

Then one of three actions. They are three flags because they are three different
things:

```bash
# Cap this organization at 200 turns per UTC day.
fm-set-turn-cap --enterprise-id <enterprise_id> --organization-id <organization_id> --cap 200 --yes

# Take the cap off this organization entirely.
fm-set-turn-cap --enterprise-id <enterprise_id> --organization-id <organization_id> --unlimited --yes

# Remove the override, so the deployment policy applies again:
#   no organization → TENANT_DAILY_TURN_CAP, an organization → uncapped.
fm-set-turn-cap --enterprise-id <enterprise_id> --organization-id <organization_id> --clear --yes
```

`--clear` and `--unlimited` are **not** the same action. On an account carrying
no organization, clearing returns it to the deployment default while
un-limiting takes the cap off. On a company organization they happen to
coincide today — which is exactly why they must not share a spelling.

`--dry-run` previews without writing. A write with neither `--dry-run` nor
`--yes` is refused: this changes what a tenant is allowed to spend.

**It takes effect on that subject's next turn.** The override is read from the
row on every turn, so there is no restart and no redeploy — and no need to wait
for a rollout when somebody is stuck mid-incident. Changing
`TENANT_DAILY_TURN_CAP` itself is an ordinary setting change and *does* need a
redeploy; use the per-organization override when one tenant needs headroom now.

Raising a cap does **not** give back a day already spent: the ledger holds what
was used, and the subject resumes against the new, higher number. Lowering a
cap below a subject's standing count refuses its next turn immediately, and the
log line reports the true count rather than the new limit.

`--show` renders the verdict the enforcement itself resolves (the same
`CapPolicyResolver` object), so what you read is what the subject's next turn
will meet — not a second description of the policy. A soft-deleted organization
does not resolve at all.

| Code | Meaning |
|------|---------|
| 0 | Done, or a dry run, or `--show` |
| 1 | Refused: no such organization, an attempted write against `--account-id`, or the update matched no row — nothing was written |
| 2 | A bad flag (argparse usage error) — nothing was written |

If a turn is refused with **503** and `x-error-code:
TENANT_TURN_CAP_UNAVAILABLE`, the cap could not be applied at all — the ledger
write failed. That is a database problem, not a quota one; the caller's
allowance is untouched, and the API logs carry the underlying error.
