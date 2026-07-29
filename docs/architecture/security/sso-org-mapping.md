# SSO Organization Mapping

**Status:** Implemented
**Scope:** Cloud (`TENANT_PROVIDER=multi`). Standalone / single-tenant is unaffected.
**Refs:** ADR-010 (tenancy in core), ADR-013 (Enterprise > Organization > Team), ADR-015 (hosted SSO login), #869, #629, #850

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

Anything that cannot be resolved fails the login closed. There is no
just-in-time tenant creation: an organization is a billing and isolation
boundary, and an IdP claim is not authority to create one.

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
   populated by the WorkOS adapter. Absent → `sso_org_unmapped`.
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
existing role scripts. There is no allowlist.

Operator procedure: `docs/operations/sso-org-provisioning.md`.

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
