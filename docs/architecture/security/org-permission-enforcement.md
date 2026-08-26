# Wiring org-scoped permissions to endpoint checks

**Status:** Phase 0 landed (#1163) — the machinery exists and **enforces
nothing**. Phases 1–3 are design. Tracked by [#1040](https://github.com/FaultMaven/faultmaven/issues/1040).
**Supersedes the "tracked in #706" pointers** in [rbac.md](rbac.md), [iam-design.md](iam-design.md) and `infrastructure/persistence/user_repository.py`.
**Related:** ADR-012 D9 (operator vs org-admin axes), ADR-006 (`TENANT_PROVIDER=multi` seam), #706 (PR #1039, which settled the role *source* question), #874 / #1042 (membership and role writes must revoke), dash#35 (role-naming reconciliation).

---

## What is actually true today

The `Role` → `Permission` mapping in `faultmaven/models/rbac.py` maps to nothing
at the API surface. Three separate facts make that so, and they compound:

**1. The `permissions` claim is never minted.** `AuthenticatedUser.from_jwt_claims`
reads `claims.get("permissions", [])`, and neither token generator writes that
key — `jwt_token_generator.py` mints `roles` plus a hardcoded `scopes` literal
(`["openid", "profile", "email", "cases:read", "cases:write", "knowledge:read"]`,
identical in the RS256 and HS256 paths, unrelated to the user's roles). So
`AuthenticatedUser.permissions` is **always the empty list**.

The practical consequence is worth stating plainly, because it inverts the
obvious first step: `require_permission("cases:read")` on an endpoint today
would **403 every caller, including a platform admin**. The dependencies are not
merely unused — they are unusable until something populates the field. Any plan
that begins "add `require_permission` to a route and see" begins with an outage.

**2. `require_role` / `require_any_role` have zero production call sites.**
Only docstring examples and the `api/middleware/__init__.py` re-export. They
would work — `roles` *is* minted — but nothing calls them, so `platform_admin`
(via `require_platform_admin` / `is_platform_admin()`) is the only role any
enforcement path consults. Assigning `member` versus `viewer` changes nothing
about what a user can do.

**3. There are already two answers to "what is this user's org role", and they
can disagree.** #706 settled that `users.dev_roles` is the canonical source of
the JWT `roles` claim and that `organization_members.role_id` is affiliation,
not the claim source. But the Cloud org console does not consult the claim at
all: `OrganizationManagementService` gates on
`IOrganizationRepository.user_has_permission`, which joins
`organization_members → role_permissions → permissions` **live, per request**.
So the console's authority model and the token's `roles` claim are two
independent readings of the same question, kept in sync only by convention.

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

## Decision 3 — close the dot-versus-colon trap at the boundary

`IOrganizationRepository.user_has_permission` parses `"resource.action"` (a
**dot**) while the `Permission` enum spells the same permission with a **colon**
(`"org:manage_users"`). Passing the enum's own value silently returns `False` —
a permission check that denies for a reason unrelated to the user's permissions.
`faultmaven_cloud` works around it today by defining dot-form literals
(`PERM_MANAGE_USERS = "org.manage_users"`) beside a comment explaining the trap.

A silent-deny format mismatch in an authorization primitive should not be a
comment in each caller. `user_has_permission` should accept the `Permission`
enum (or its colon form) and normalise internally; the dot form stays supported
for the existing callers. This is small, independently landable, and should
precede any wiring — it is the one failure mode here that fails *closed* and
would otherwise be diagnosed as "the RBAC wiring doesn't work".

---

## Staged rollout

Enforcement changes authorization across the API surface, and fact 1 means the
naive first step is an outage. So it stages:

**Phase 0 — the seam, enforcing nothing. Landed (#1163).**
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

Nothing calls it. `tests/unit/modules/auth/test_permission_enforcement_is_unwired.py`
AST-scans the package and holds `require_permission` and its four siblings to an
empty allowlist of uses (imports: the middleware re-export only), so Phase 0
cannot silently become Phase 2.

**Phase 1 — populate and observe.** `get_current_user` fills
`AuthenticatedUser.permissions` from the resolver. Still no endpoint checks, but
`require_permission` becomes *usable* rather than deny-all. Add shadow logging:
for a candidate set of endpoints, log what *would* have been denied. The
question Phase 1 answers is the one no amount of reading settles — whether the
`ROLE_PERMISSIONS` map matches how the product is actually used.

**Phase 2 — enforce where a wrong answer is obvious.** Destructive operations
first (`cases:delete`, `evidence:delete`), where a `viewer` holding the ability
is plainly wrong and the blast radius of a mistake is a 403 on a rare action
rather than on the main read path.

**Phase 3 — enforce broadly**, informed by Phase 1's shadow data, and delete the
hardcoded `scopes` literal or make it role-derived.

Phases 1–3 are each independently revertible, which is the point of staging
them: the failure mode of RBAC wiring is locking out real users, and it is
discovered in production or not at all.

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

**Item 4 — one role vocabulary.** `modules/auth/domain/models/rbac.py` no longer
defines its own `Role` / `Permission` / `ROLE_PERMISSIONS`; it re-exports
`faultmaven/models/rbac.py` (the copy migration 029 seeds from, and that
`rbac_seed.SYSTEM_ROLE_IDS` maps to stable ids) and adds only what is genuinely
auth-module knowledge: `PLATFORM_ADMIN_ROLE`, `PLATFORM_ADMIN_ROLE_SET`,
`BASE_USER_ROLE`, `OPERATOR_GRANTED_ROLES`.
`test_platform_admin_role_separation.py` now asserts the two modules expose the
**same objects** (identity, not equality — two separately-defined enums with
identical members compare unequal member-by-member and would pass an equality
check right through a re-fork).

## Deliberately not decided here

- **Team-scoped permissions.** Teams narrow KB read scope and carry no token
  claim; whether they should participate in `ROLE_PERMISSIONS` is a separate
  question.
- **Break-glass interaction.** ADR-012 D8/D9 break-glass grants cross-tenant
  content access to an operator. How a grant composes with org permissions (it
  should not need to — the axes are orthogonal) wants its own note once Phase 2
  exists.
- **Per-resource ownership.** `ROLE_PERMISSIONS` is role-uniform: it cannot
  express "a member may edit their own case but not another member's". Every
  such check is currently a service-layer ownership test, and this design leaves
  it there.
