# SSO Organization Mapping

**Status:** Implemented
**Scope:** Cloud (`TENANT_PROVIDER=multi`). Standalone / single-tenant is unaffected.
**Refs:** ADR-010 (tenancy in core), ADR-013 (Enterprise > Organization > Team), ADR-015 (hosted SSO login), ADR-016 D5 (self-service personal tenants), #869, #629, #850, #1045

## What this is

Under multi-tenant, an SSO login has to answer a question single-tenant never
asks: **which tenant does this person belong to?** FaultMaven answers it from
the identity provider's own organization, resolved through an
operator-provisioned mapping table.

A successful login therefore does four things a single-tenant login does not:

1. resolves the IdP's organization to a FaultMaven organization;
2. binds that organization as the request's tenant for the rest of the callback,
   so everything below it runs inside the right Row-Level Security scope;
3. ensures the user is a member of it;
4. carries it into the minted access and refresh tokens, so the session stays
   scoped to that tenant across rotation.

Anything that cannot be resolved fails the login closed. An IdP organization
this deployment has no mapping for is refused: an organization is a billing and
isolation boundary, and an IdP claim is not authority to create one. A company
is onboarded deliberately, never by whoever signs in first.

An identity that carries **no** IdP organization at all is a separate question
with a separate answer, gated on its own switch — see
[Personal tenants](#personal-tenants) below. Nothing in this section changes in
either switch state.

## The mapping table

`sso_org_mappings` (migration 038):

| column | meaning |
| --- | --- |
| `provider` | SSO provider key, `workos` today |
| `provider_org_id` | the IdP's own organization identifier (`org_01H…`) |
| `organization_id` | FK → `organizations`, `ON DELETE CASCADE` |
| `created_at` / `updated_at` | server-defaulted, timezone-aware |

`(provider, provider_org_id)` is the primary key — one IdP organization resolves
to at most one tenant. `(provider, organization_id)` is unique — a tenant is
claimed by at most one IdP organization per provider. The relation is 1:1 per
provider.

### Why it is not RLS-tenanted

Every other organization-keyed table is enrolled in the migration-018
tenant-isolation policy, `organizations` and `organization_members` included.
This one deliberately is not.

The SSO callback that reads it is **unauthenticated**. No tenant is bound at
that moment — binding the tenant is precisely what this lookup decides. Under
the migration-018 policy every tenanted table is invisible there, so a mapping
that lived on a tenanted table could not be read at the only moment it matters.

Leaving it outside RLS discloses nothing: a row holds an identifier
equivalence and no tenant data, and it exists only because an operator created
it. The FK still ties it to a real organization, and `ON DELETE CASCADE` means
deleting a tenant retires its mapping with it.

## The login flow

`SSOLoginService._complete_callback`, after the IdP code exchange succeeds and
**before** any user lookup:

1. **The IdP must name an organization.** `SSOIdentity.organization_id` is
   populated by the WorkOS adapter. Absent → `sso_org_unmapped`, unless
   `SSO_JIT_PERSONAL_TENANT_ENABLED` is on, in which case this is the one
   branch that takes the [personal-tenant path](#personal-tenants) instead.
2. **The mapping must exist.** Looked up through `ISSOOrgMappingRepository`.
   Absent → `sso_org_unmapped`.
3. **The tenant is bound.** `set_current_org_id(mapped_org_id)` scopes every
   database transaction for the remainder of the callback.
4. **The organization must be usable.** Read back through
   `IOrganizationRepository.get_organization` — an organization that is missing,
   soft-deleted, or deactivated fails the login with the generic `sso_failed`.

Logging carries reason slugs (`no_idp_org`, `org_unmapped`, `org_unavailable`,
`enterprise_mismatch`) plus the provider and, where useful, the IdP organization
id — never the subject or the email. The IdP organization id is not a secret and
is exactly what an operator needs in order to provision the missing mapping.

`sso_org_unmapped` is the one failure an operator can act on, which is why it is
distinct from the generic slug. It still leaks nothing: it says only that this
deployment does not know that IdP organization.

Single-tenant runs none of the above. The mapping repository is never consulted
and the behaviour is byte-for-byte what it was before.

## Membership

One code path serves first-time and returning users, so the two cannot drift:

- **Enterprise guard.** If the account already belongs to an enterprise and it
  is not the mapped organization's, the login fails closed. Moving an account
  between enterprises is a deliberate operator act, never an implicit
  consequence of an IdP claim.
- **Idempotent ensure.** If the user is already a member, nothing is written —
  an existing role (including `admin`) is left exactly as it is. Otherwise a
  membership row is added with the `member` role from `SYSTEM_ROLE_IDS`.
- **Additive only.** Memberships in *other* organizations are never enumerated
  or removed. Under RLS they are invisible from inside a bound tenant, by
  design. The IdP is authoritative for the organization the user is logging into,
  not for the set of organizations they belong to.
- **Fail closed.** A membership write that fails takes the login down with it. A
  just-in-time account may survive that failure, but no org-less session does:
  the next attempt heals it, because the ensure is idempotent.

Just-in-time provisioning additionally anchors the new account to the mapped
organization's `enterprise_id`, instead of the standalone default the repository
would otherwise supply.

Binding the tenant before provisioning also fixes the ADR-015 PR-7 known
precondition for free: the JIT audit write stamps `get_current_org_id()`, which
under multi-tenant previously had no tenant to stamp — so the audit row either
failed its FK or landed unreadable.

## The organization rides the token chain

The `users` table has no organization column, and it should not have one:
affiliation is a row in `organization_members`, and RLS is the authority on it.
That leaves a gap — a user *loaded from the database* carries no organization,
so any code path that re-mints tokens from a loaded user would mint an org-less
pair.

The organization therefore travels with the credentials:

| hop | carrier |
| --- | --- |
| callback → dashboard | `organization_id` on the single-use completion-code payload |
| exchange → tokens | attached to the user as `User.organization_id`, a runtime-only field the persistence layer never writes |
| access token | `organization_id` claim (already present) |
| refresh token | `organization_id` claim (added for both RS256 and HS256) |
| `/auth/refresh` | the validated refresh claim is re-attached to the reloaded user before the new pair is minted |
| oauth refresh grant (`POST /auth/oauth/token`, `grant_type=refresh_token`) | same re-attachment, in `OAuthService.refresh_access_token` |
| service-account mint | `provision_service_account_credential` stamps the operator-supplied organization on the account before signing, which is where a non-human actor's chain begins |

Both refresh paths carry the same contract, deliberately: a credential that
rotates through only one of them would otherwise lose its tenant on first
rotation. `/auth/refresh` is the dashboard's path; the oauth refresh grant is
Copilot's and the D10 service account's.

A service account has no login to derive a tenant from, so the operator supplies
it at provisioning time (`--organization-id`). Provisioning refuses at mint
whatever `bind_request_org_context` would refuse at bind — the sentinel org in
any mode, and a missing organization under multi-tenant — so the misconfiguration
surfaces to the operator minting the credential rather than as a rejected API
call later. Operator procedure:
`docs/operations/security/service-account-credentials.md`.

`resolve_organization_claim` remains the single place that decides what goes in
the claim, and the #850 guards remain live backstops on both ends: under
multi-tenant a user with no organization mints an **empty** claim (never the
Standalone sentinel), and `bind_request_org_context` refuses a verified request
whose claim is empty or sentinel-valued. A refresh token minted before this
feature existed carries no claim at all and therefore refreshes into an empty
claim — fail-closed, not silently healed.

## What is re-verified, and when

- **Membership is verified at login only.** An access token is valid until it
  expires; a refresh token keeps rotating the same organization.
- **An organization move requires re-login.** Changing a user's tenant is an
  operator action; their existing tokens keep the old claim until they expire or
  are revoked.
- **Revocation is the per-user watermark.** Removing a membership does not
  invalidate an outstanding token by itself — bump the user's revocation
  watermark to end the session immediately.

## Provisioning

`fm-provision-sso-org` idempotently creates the enterprise, the
organization, its default team, and the mapping row. It runs with the RLS-owning
database role because it writes rows for a tenant that does not exist yet, and it
refuses to remap an IdP organization that already points at a different
FaultMaven organization — remapping is a deliberate operator act, not a script
default.

Admin binding is manual and post-hoc (ADR-015 D5): no login path grants elevated
roles, so the first user signs in via SSO and an operator promotes them with the
existing role scripts. There is no allowlist. This holds for personal tenants
too — their single member holds the `member` role, and the platform tier stays
platform-only.

Operator procedure: `docs/operations/sso-org-provisioning.md`.

## Personal tenants

**Refs:** ADR-016 D5 (amends ADR-015), #1045, migration 051.

Any visitor can sign in with a personal email and use FaultMaven as an
individual. Because `organization_id` *is* the RLS key and is NOT NULL on the
tenanted tables, "individual with no organization" has no data model to live in
— so it necessarily means an **auto-provisioned personal organization**:
invisible in the UI, real in the database.

### The switch

| setting | `SSO_JIT_PERSONAL_TENANT_ENABLED` (`AuthSettings.sso_jit_personal_tenant_enabled`) |
| --- | --- |
| default | `false` |
| scope | multi-tenant (Cloud) only; single-tenant never decides a tenant |
| read | live from settings on every callback, not captured at composition time |

Off, an identity with no IdP organization is refused exactly as it was before
this feature existed. The switch is read on each callback so an operator flip
takes effect without a redeploy, and so it cannot become a documented knob that
nothing consults.

Sign-up does **not** open when this ships. ADR-016 D5 sequences three hard
preconditions ahead of it: the two-tenant surface probe green on the Postgres
lane (#1317), the live two-tenant assertion recorded by the owner (#1252), and a
per-tenant LLM usage cap that fails closed at the limit. The cap is a separate
change; open sign-up plus uncapped compute is an open bill.

### What first sign-in creates

The rows the `fm-provision-sso-org` CLI creates, in the same order and in one
transaction, triggered by a login rather than an operator:

1. an **enterprise**, slug `personal-<key>`;
2. the **organization** — a real, distinct row named `Personal`, never the
   Standalone sentinel (#850);
3. its **default team** (ADR-013);
4. the **`sso_org_mappings`** row binding the IdP organization to it;
5. the **`sso_personal_orgs`** row binding the subject to it.

On the IdP side, a WorkOS organization holding that one member.

**Membership is not written here.** The user row does not exist yet — tenant
resolution runs before the user lookup, so the RLS scope is right for everything
after it. `_ensure_org_affiliation` writes it afterwards, with the `member` role,
using the same code that serves a mapped tenant. That is what makes "a personal
org's single member holds the member role" (ADR-015 D5) true by construction
rather than by a second copy of the rule.

### Why the login path does not need the owner role

`fm-provision-sso-org` demands the RLS-exempt owner DSN because it resolves an
organization by `(enterprise_id, slug)` — an id-blind lookup the `organizations`
policy cannot satisfy. The login path has no such lookup: it *generates* the
organization id and binds it as the tenant context **before** the transaction
opens, so the engine's `begin` listener writes it into `app.current_org_id` and
migration 018's policy (no `FOR` clause, so `USING` doubles as `WITH CHECK`)
accepts every row. The subject-keyed table is what stands in for the CLI's slug
lookup, and it is untenanted, so "which organization is this?" is answered
before RLS is in the way.

### `sso_personal_orgs` (migration 051)

| column | meaning |
| --- | --- |
| `provider` | SSO provider key, `workos` today |
| `provider_user_id` | the IdP's stable **subject** (`user_01H…`), never an email |
| `organization_id` | FK → `organizations`, `ON DELETE CASCADE` |
| `provider_org_id` | the IdP organization minted to hold that one member |
| `created_at` / `updated_at` | server-defaulted, timezone-aware |

`(provider, provider_user_id)` is the primary key; `(provider, organization_id)`
is unique.

**Why membership cannot be the lookup.** A returning individual's callback may
report no organization at all — AuthKit populates one only when the sign-in was
organization-scoped. The obvious alternative, reading `organization_members`, is
unavailable: it is RLS-tenanted (migration 018) and no tenant is bound at
callback time, because binding the tenant is what this lookup decides. The
subject is the one identifier every login carries.

**Why not reuse `sso_org_mappings`.** It is keyed on the IdP's *organization*
id, which the callback need not carry, and it is 1:1 per organization — a
personal tenant's row there is already spent on the IdP organization that holds
the member. So the subject binding needs its own table, untenanted for exactly
the same reason as its sibling.

Both shapes of a returning login therefore agree: with no organization echoed,
the subject row resolves the tenant; with one echoed, the ordinary mapped path
resolves it through the `sso_org_mappings` row first sign-in wrote — to the same
organization.

### Naming

The organization is called `Personal` — one constant,
`modules/auth/domain/personal_tenant.PERSONAL_ORG_NAME`. The slug and the IdP
`external_id` are two renderings of one derived key: a 128-bit BLAKE2b digest of
the length-prefixed `(provider, subject)` pair, rendered `personal-<32 hex>`.

The key carries no PII (the input is the IdP's opaque subject handle, and the
digest is one-way), cannot collide across users, and is **deterministic** — which
is what makes a retry safe: an attempt that minted the IdP organization and then
failed to commit re-derives the same `external_id` and finds it rather than
minting a second. Fields are length-prefixed rather than separator-joined
because any separator can appear inside a value, and an ambiguous encoding lets
a crafted subject land on another pair's tenant.

### Failure direction

The IdP organization is created **before** the database transaction, and the
database rows are one transaction. Both orderings follow from the same rule: a
failure must leave the login refused and a retry able to finish, never a
half-provisioned tenant a later login adopts as if it were complete.

- **IdP call fails** → nothing written on either side; login refused.
- **Database transaction fails** → it rolls back whole, so no enterprise,
  organization or team survives. The residue is an IdP organization with no
  tenant, which is invisible to everyone and is found again by the next attempt.
- **Two concurrent first logins** → both derive the same slug and the same IdP
  organization, so the loser trips one of three constraints
  (`enterprises.slug`, `sso_org_mappings`'s primary key, `sso_personal_orgs`'s),
  rolls back whole, re-reads the subject row and adopts the winner's tenant.
  Exactly one organization results.
- **A subject row pointing at the Standalone sentinel** → refused. Under
  multi-tenant the sentinel identifies the deployment, not a tenant (#850).
- **Membership write fails** → the login is refused and the tenant is left in
  exactly the state the operator path leaves a freshly provisioned one in. The
  next attempt heals it, because the ensure is idempotent.

### What WorkOS guarantees, and what it does not

`external_id` is unique per WorkOS environment, so `get_organization_by_external_id`
is what makes the IdP half idempotent and a duplicate create is refused rather
than silently accepted. That refusal is resolved by re-reading, never by minting
a second organization. Membership creation is likewise get-or-confirm: a refused
create is **confirmed by listing the memberships**, not inferred from the
exception type, and a refusal that cannot be confirmed fails the login closed.

There is no cross-service transaction. If the IdP accepts the create and the
response is lost, the organization exists and the next attempt adopts it — that
is the direction the ordering was chosen for. Nothing here reconciles an IdP
organization whose tenant was never created; `sso_personal_orgs.provider_org_id`
exists so an operator can.

### Non-goals (owner-accepted, #1045)

Personal → business is **not a migration**. SSO matches on subject, so a
different email is a different WorkOS user and therefore a new FaultMaven
account; JIT deliberately refuses to link by email (ADR-015 D4); and the
individual's cases live in the personal organization behind a NOT NULL RLS key,
so moving them is an organization move, not an owner change. Neither an
SSO-identity link nor an org move exists.

## Rejected alternatives

- **Mapping columns on `organizations`** — unreadable pre-bind under RLS, and
  reading them would drag an RLS-bypassing owner-role query into the
  unauthenticated auth path.
- **Deriving the claim from membership at refresh time** — the same RLS problem
  (no tenant is bound when the refresh token is validated), and it would put an
  owner-role read on the hot token path.
- **Email-domain-based mapping** — domains are unverifiable and non-unique
  (contractors, acquisitions, shared consumer domains); the IdP's organization
  id is the stable key the IdP already asserts.
- **Storing a subject's personal organization in `sso_org_mappings`** — the
  table is keyed on the IdP *organization* id and is 1:1 per organization, so a
  personal tenant's one row there is already spent on the IdP organization
  holding the member. A subject row would have to displace it.
- **A `personal_organization_id` column on `users`** — affiliation is a row in
  `organization_members` and RLS is the authority on it; the `users` table
  deliberately has no organization column, and adding one for this case would
  make it two authorities.
- **Creating the FaultMaven tenant before the IdP organization** — the cheaper
  ordering, and the wrong one: it leaves a tenant with no IdP organization,
  which the next login adopts as if it were complete. The chosen order leaves
  only residue that is invisible and self-healing.
- **A shared enterprise for all individuals** — it would make the
  `enterprise_mismatch` guard vacuous across every personal tenant, and the
  enterprise is what the operator path creates per tenant.
