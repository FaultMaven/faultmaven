# Wiring org-scoped permissions to endpoint checks

**Status:** **Decided** (owner ruling 2026-09-24 on [#1040](https://github.com/FaultMaven/faultmaven/issues/1040)): the org mapping is pruned to the management axis and **nothing new is wired**. Phase 0 (#1163) stands as the resolver seam; Phases 1–3 are superseded. Decisions 1–3 below are kept as the history that led there.
**Supersedes the "tracked in #706" pointers** in [rbac.md](rbac.md), [iam-design.md](iam-design.md) and `infrastructure/persistence/user_repository.py`.
**Related:** ADR-012 D9 (operator vs org-admin axes), ADR-006 (`TENANT_PROVIDER=multi` seam), #706 (PR #1039, which settled the role *source* question), #874 / #1042 (membership and role writes must revoke), dash#35 (role-naming reconciliation), **ADR-017** (the enterprise isolates, the organization bills).

---

## The decision — prune to the management axis; wire nothing new

ADR-017 raised an open question against this document's staged rollout: Phases
2–3 would have enforced `cases:read` / `cases:write` / `cases:delete` by
**organization** role, turning org membership into a data-access gate, while
ADR-017 D2/D5 says organization roles are the organization's *management*
vocabulary and "no longer gate data visibility, which they never should have" —
isolation is the enterprise's job (RLS on `enterprise_id`), sharing the team's
(consent, `resource_shares`). The owner's ruling of 2026-09-24 answers it:

1. **The mapping is the management axis.** `ROLE_PERMISSIONS` in
   `faultmaven/models/rbac.py` holds exactly `org:manage_users` and
   `org:manage_settings`, both granted to `admin`; `member` and `viewer` grant
   nothing. The `cases:*` and `evidence:*` permissions are deleted from the enum,
   the map and the seed — alembic `020_org_permissions_are_management_only`
   deletes their rows and grants from every database (the `sessions:*` ones went
   in `017`).
2. **Nothing new is wired.** The premise "recorded but not enforced" was wrong
   for the `org:*` pair: they are already enforced, where the organization is
   managed. faultmaven-cloud's organization management authorises every request
   through `OrganizationManagementService._authorized_org`, which asks
   `IOrganizationRepository.user_has_permission` for `org:manage_settings` or
   `org:manage_users` — a live join over `role_permissions`. In core, the
   org-management surface is `/admin/users/{id}/roles`, gated by
   `require_platform_admin`, and `fm-remove-org-member`. Adding a
   `require_permission` to those would be a second mechanism for a check that
   already runs.
3. **The readers shrink with the mapping.** `STANDALONE_PERMISSIONS`
   (`providers/tenancy/permissions.py`) follows `ROLE_PERMISSIONS[Role.ADMIN]`
   and is now the two `org:*` permissions. The five dependency factories nothing
   called — `require_permission`, `require_any_permission`,
   `require_all_permissions`, `require_role`, `require_any_role` — are deleted
   from `api/middleware/auth.py`, with `AuthenticatedUser`'s `has_permission`,
   `has_role`, `has_any_permission` and `has_all_permissions`. The
   `PermissionResolver` seam is kept.

Items 2 and 4 of #1040 close with it: see *Already landed under #1040* below.

The OAuth **scope** strings `cases:read` / `cases:write` / `knowledge:read`,
which tokens carry in `scopes` and the consent page lists, are a separate,
literal vocabulary and are untouched.

---

## What is actually true today

The `Role` → `Permission` mapping is the organization's management axis, and it
is enforced where the organization is managed. Three facts describe the rest:

**1. The `permissions` claim is never minted.** `AuthenticatedUser.from_jwt_claims`
reads `claims.get("permissions", [])`, and neither token generator writes that
key — `jwt_token_generator.py` mints `roles` plus a hardcoded `scopes` literal
(`["openid", "profile", "email", "cases:read", "cases:write", "knowledge:read"]`,
identical in the RS256 and HS256 paths, unrelated to the user's roles). So
`AuthenticatedUser.permissions` is **always the empty list**, and nothing reads
it: the dependencies that would have (and that would have denied every caller,
platform admin included) are deleted.

**2. `platform_admin` is the only role a core dependency consults.**
`require_platform_admin` / `is_platform_admin()` read the deployment axis from
the `roles` claim. There is no org-role dependency in core; the org axis is
read by organization management, per request, from `organization_members`.

**3. There are two answers to "what is this user's org role", and they can
disagree.** #706 settled that `users.dev_roles` is the canonical source of
the JWT `roles` claim and that `organization_members.role_id` is affiliation,
not the claim source. But the Cloud org console does not consult the claim at
all: `OrganizationManagementService` gates on
`IOrganizationRepository.user_has_permission`, which joins
`organization_members → role_permissions → permissions` **live, per request**.
The org role in the `roles` claim authorises nothing, so the disagreement
decides nothing; the console's live answer is the one that is enforced.

---

## The vocabulary problem, and why it blocks the obvious fix

`dev_roles` uses `user` / `admin` / `platform_admin`. The `Role` enum is
`admin` / `member` / `viewer`. `user` and `member` denote the same tier in
different strings, and — because `user` is not a member of `Role` —
`get_permissions_for_roles(["user"])` returns the **empty set**.

So the obvious implementation ("derive permissions from the `roles` claim at
mint time") locks out every ordinary account: a user whose `dev_roles` is
`["user"]` derives no permissions and loses `cases:read`. The vocabulary gap is
not a tidiness item to do afterwards — it is load-bearing on the very first
step, which is why #1040 item 2 and item 1 cannot be sequenced independently.

*Since the 2026-09-24 decision there is no `cases:read` to lose:* `user` and
`member` both grant nothing, so nothing reconciles them, and Decision 2 below
stands as the answer.

---

## Decision 1 — resolve per request, do not mint into the token

Two models were considered.

**Mint at login.** Derive permissions when the token is issued and carry them as
a claim. Cheap on the request path; no database read.

**Resolve per request.** Read the user's org role from `organization_members` on
each request and expand it through `ROLE_PERMISSIONS`. Always current; costs one
indexed lookup.

**Resolve per request wins**, and the reason is the shape of the bugs this
codebase keeps finding rather than a performance argument:

- Membership was verified at login only, so removing a member left their tokens
  working until expiry (#874). The role has the same shape, so demoting an admin
  left elevated claims live (#1042). Both were closed by pairing the write with a
  revocation watermark. **Minting permissions would add a third login-time
  answer to keep paired** — and the next writer who forgets the pairing
  reintroduces the same bug on a claim that now decides authorization directly.
- Staleness in an authorization input is a security property, not a latency
  tradeoff. A demotion that takes effect on the next request is a different
  product from one that takes effect within `JWT_REFRESH_TOKEN_EXPIRY_DAYS`.
- The Cloud console **already** resolves per request. Choosing the same model
  collapses the two disagreeing answers from fact 3 into one; choosing the other
  entrenches the disagreement and makes it enforcing.

The cost is one query per authorizing request, on an indexed
`(user_id, organization_id)` lookup, and it is cacheable per request and per
`(user, org)` with a short TTL. That is a known, bounded cost. Note that the
per-request model **still requires** the #1042 revocation pairing for a different
reason: the `organization_id` claim itself is minted at login, so a token
continues to name an org the user has left. Revocation is what ends that, and
that pairing already exists.

**What this does not change.** `dev_roles` remains the canonical source of the
JWT `roles` claim, exactly as #706 settled. What changes is that the claim stops
being the *authorization* source for the **org** axis — a question #706
explicitly left open. `roles` remains the source for the deployment axis
(`platform_admin`), which has no organization to resolve against.

## Decision 2 — the vocabulary duplication becomes moot, not reconciled

Given Decision 1, each string gets exactly one home:

| Axis | Source of truth | Vocabulary | Read by |
|------|-----------------|------------|---------|
| Deployment | `users.dev_roles` → `roles` claim | `platform_admin` | `require_platform_admin` / `is_platform_admin()` |
| Organization | `organization_members.role_id` (live) | `admin`, `member`, `viewer` | the permission resolver below |
| Base marker | `users.dev_roles` | `user` | nothing — grants no permissions, keeps the role list non-empty |

There is then no `user`-versus-`member` collision to resolve: `user` is a marker
on the deployment axis and `member` is a tier on the org axis, and they never
appear in the same list for the same purpose. This is strictly better than the
alternatives considered — aliasing `user` to `Role.MEMBER` in the permission map
(cheap, but leaves two strings meaning one thing forever) or migrating
`dev_roles` `user` → `member` (a data migration, a dashboard change, and a
compatibility window, to reach the same place).

**Cross-repo:** the dashboard renders these strings. Under this decision the
dashboard's org-role UI reads the org axis and its operator badge reads the
deployment axis — which is what dash#35 is for. No string it currently renders
changes meaning; what changes is which endpoint it should read each from.

## Decision 3 — close the dot-versus-colon trap at the boundary — **landed**

`IOrganizationRepository.user_has_permission` parsed `"resource.action"` (a
**dot**) while the `Permission` enum spells the same permission with a **colon**
(`"org:manage_users"`) — and migration 029 seeds the `permissions` rows by
splitting that colon, so the table agreed with the enum and only the lookup did
not. Passing the enum's own value silently returned `False`: a permission check
that denied for a reason unrelated to the user's permissions.
`faultmaven_cloud` worked around it with dot-form literals
(`PERM_MANAGE_USERS = "org.manage_users"`) beside a comment explaining the trap.

A silent-deny format mismatch in an authorization primitive should not be a
comment in each caller. `user_has_permission` now accepts a `Permission` member
or its colon form and normalises internally. The dot form is **not** kept
alongside it: two spellings for one permission leaves the next caller free to
pick the one nothing else understands. Anything that is not exactly one
`resource:action` pair — a non-string included — returns `False`, because an
authorization primitive fails closed on input it cannot interpret. The Cloud
literals and their comments are gone with it.

This landed first and alone, because it is the one failure mode here that fails
*closed*: during the rollout it would otherwise be diagnosed as "the RBAC wiring
doesn't work" rather than as a string-format bug.

---

## Staged rollout — superseded

Phases 1–3 below are **superseded** by the 2026-09-24 decision and will not
run: the org axis is the management axis, it is already enforced where it is
managed, and nothing gates data by organization role. They are kept as the plan
the ruling replaced.

**Phase 0 — the seam, enforcing nothing. Landed (#1163), and stands.**
`providers/tenancy/permissions.PermissionResolver` sits behind the
`TenantProvider` seam (ADR-006): `MultiTenantPermissionResolver` reads the
caller's `organization_members` role and expands it through `ROLE_PERMISSIONS`
live; `SingleTenantPermissionResolver` returns the fixed set for the standalone
account, which is legitimately both org admin and operator, without reading a
table that deployment does not populate. `create_permission_resolver` takes the
*built* `TenantProvider` rather than re-reading `TENANT_PROVIDER`, so one object
decides the deployment's tenancy. It fails closed throughout: `resolve` returns
a frozenset and never `None`, an unidentified caller is answered without a
query, and a `role_id` outside the seeded system set grants nothing.

Nothing in core calls it. `test_permission_enforcement_is_unwired.py`, which
held `require_permission` and its four siblings to an empty allowlist of uses,
is deleted with them: the surface it held empty is gone.

**Phase 1 — populate and observe** *(superseded)*. `get_current_user` would
have filled `AuthenticatedUser.permissions` from the resolver, with shadow
logging of what candidate endpoints would have denied.

**Phase 2 — enforce where a wrong answer is obvious** *(superseded)*.
Destructive operations first (`cases:delete`, `evidence:delete`).

**Phase 3 — enforce broadly** *(superseded)*, and delete the hardcoded `scopes`
literal or make it role-derived.

---

## Already landed under #1040

**Item 3 — promote/demote asymmetry.** `PLATFORM_ADMIN_ROLE_SET` is
`["user", "admin", "platform_admin"]`, so promotion granted the org-scoped
`admin` too, while `fm-demote-platform-admin` removed only `platform_admin`.
Promote-then-demote therefore left an account holding org authority it never
had. `fm-demote-platform-admin` now removes `OPERATOR_GRANTED_ROLES` — derived
from `PLATFORM_ADMIN_ROLE_SET` rather than restated, since restating it is how
the two came apart — keeping the base `user` marker. `--keep-org-admin` covers
the account that held org `admin` independently, which nothing records and this
command therefore cannot infer.

**Item 4 — one role vocabulary, one door.** `modules/auth/domain/models/rbac.py`
no longer defines its own `Role` / `Permission` / `ROLE_PERMISSIONS`, and no
longer re-exports them either: `faultmaven/models/rbac.py` is the one
definition and the one door (it is what faultmaven-cloud imports and what the
seed is written from), and the auth module's `rbac` holds only what is
genuinely auth-module knowledge: `PLATFORM_ADMIN_ROLE`,
`PLATFORM_ADMIN_ROLE_SET`, `BASE_USER_ROLE`, `OPERATOR_GRANTED_ROLES`,
`ORG_ADMIN_ROLE`, `ROLELESS_ACCOUNT_ROLE` and `effective_roles`. The auth
packages above it (`modules/auth`, `modules/auth/domain/models`) stopped
re-exporting `Role` / `Permission` too. `test_platform_admin_role_separation.py`
asserts that none of the three auth doors offers the vocabulary.

**Item 1 — the mapping.** Pruned to the management axis, as decided above.

**Item 2 — `user` versus `member`.** No code: after the prune both grant
nothing, so there is nothing to reconcile, and Decision 2 stands — `user` is the
deployment-axis marker on `dev_roles`, `member` an org-axis role.

## Deliberately not decided here

- **Team-scoped permissions.** Teams narrow KB read scope and carry no token
  claim; whether they should participate in `ROLE_PERMISSIONS` is a separate
  question.
- **Break-glass interaction.** ADR-012 D8/D9 break-glass grants cross-tenant
  content access to an operator. It does not compose with org permissions: the
  axes are orthogonal, and org permissions gate no content.
- **Per-resource ownership.** `ROLE_PERMISSIONS` is role-uniform: it cannot
  express "a member may edit their own case but not another member's". Every
  such check is currently a service-layer ownership test, and this design leaves
  it there.
