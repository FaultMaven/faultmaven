# SSO Enterprise Mapping

**Status:** Implemented
**Scope:** Cloud (`TENANT_PROVIDER=multi`). Standalone / single-tenant is unaffected.
**Refs:** ADR-010 (tenancy in core), ADR-017 (the enterprise isolates, the organization bills, the team shares — supersedes ADR-013's "Enterprise > Organization > Team" framing), ADR-015 (hosted SSO login), ADR-016 D5 (self-service personal tenants, amended by ADR-017), #869, #629, #850, #1045

## What this is

Under multi-tenant, an SSO login has to answer a question single-tenant never
asks: **which tenant does this person belong to?** Under ADR-017 that tenant is
the **enterprise** — the isolation boundary — not the organization. FaultMaven
answers it from the identity provider's own organization, resolved through an
operator-provisioned mapping table, or — for an identity carrying no IdP
organization at all — from the verified email domain (ADR-017 D3, see
[Personal tenants](#personal-tenants) below).

A successful login therefore does four things a single-tenant login does not:

1. resolves the IdP's organization to a FaultMaven **enterprise** (or derives one
   from the email domain);
2. binds that enterprise as the request's tenant for the rest of the callback, so
   everything below it runs inside the right Row-Level Security scope;
3. anchors the user to it (`users.enterprise_id`) — the **only** membership a
   login establishes; no organization and no team;
4. mints the enterprise into the access and refresh tokens' `enterprise_id`
   claim, so the session stays scoped to that tenant across rotation.

Anything that cannot be resolved fails the login closed. An IdP organization
this deployment has no mapping for is refused: the enterprise is the isolation
boundary, and an IdP claim is not authority to create one. A company is
onboarded deliberately, never by whoever signs in first.

An identity that carries **no** IdP organization at all is a separate question
with a separate answer, gated on its own switch — see
[Personal tenants](#personal-tenants) below. Nothing in this section changes in
either switch state.

**No organization or team is ever created by a login.** An organization is a
billing target created by payment (ADR-017 D5) and a team is formed by consent
(D4); sign-in knows neither and invents neither. What it establishes is the
account's one isolation membership, `users.enterprise_id`, and nothing else.

## The mapping table

`sso_org_mappings` (ADR-017 D9 — targets the enterprise, not the organization; the
table keeps its historical name):

| column | meaning |
| --- | --- |
| `provider` | SSO provider key, `workos` today |
| `provider_org_id` | the IdP's own organization identifier (`org_01H…`) |
| `enterprise_id` | FK → `enterprises`, `ON DELETE CASCADE` |
| `created_at` / `updated_at` | server-defaulted, timezone-aware |

`(provider, provider_org_id)` is the primary key — one IdP organization resolves
to at most one tenant. `(provider, enterprise_id)` is unique — a tenant is
claimed by at most one IdP organization per provider. The relation is 1:1 per
provider. A company that already has an IdP organization maps it to its
enterprise; its members land in that enterprise on sign-in, in **no**
organization until an enterprise or organization admin assigns them one (D7,
post-beta).

### Why it is not RLS-tenanted

Every other enterprise-keyed table is enrolled in the RLS tenant-isolation
policy, `organizations` and `organization_members` included. This one
deliberately is not.

The SSO callback that reads it is **unauthenticated**. No tenant is bound at
that moment — binding the tenant is precisely what this lookup decides. Under
the tenant-isolation policy every tenanted table is invisible there, so a
mapping that lived on a tenanted table could not be read at the only moment it
matters.

Leaving it outside RLS discloses nothing: a row holds an identifier
equivalence and no tenant data, and it exists only because an operator created
it. The FK still ties it to a real enterprise, and `ON DELETE CASCADE` means
deleting a tenant retires its mapping with it.

## The login flow

`SSOLoginService._complete_callback`, after the IdP code exchange succeeds and
**before** any user lookup:

1. **The IdP must name an organization, or sign-up must be enabled.**
   `SSOIdentity.organization_id` is populated by the WorkOS adapter. Absent →
   `sso_org_unmapped`, unless `SSO_JIT_PERSONAL_TENANT_ENABLED` is on, in which
   case this is the one branch that takes the
   [sign-up path](#personal-tenants) instead — deriving the enterprise from the
   verified email domain rather than from an IdP organization.
2. **The mapping must exist.** Looked up through `ISSOOrgMappingRepository`
   (`get_enterprise_id`). Absent → `sso_org_unmapped`.
3. **The tenant is bound.** `set_current_enterprise_id(enterprise_id)` scopes
   every database transaction for the remainder of the callback.
4. **The enterprise must be usable.** Read back through
   `IEnterpriseRepository.get_enterprise` and checked with `enterprise_is_usable`
   — an enterprise that is missing, soft-deleted, or inactive fails the login
   with the generic `sso_failed`.

Steps 3 and 4, plus a refusal of the Standalone sentinel *before* the bind, are
one shared tail (`_bind_and_verify_enterprise`). All three resolution branches
— mapped, domain sign-up, and personal sign-up — end in it, so none can acquire
a check the others lack.

Logging carries reason slugs (`no_idp_org`, `org_unmapped`, `org_unavailable`,
`enterprise_mismatch`) plus the provider and, where useful, the IdP organization
id — never the subject or the email. The IdP organization id is not a secret and
is exactly what an operator needs in order to provision the missing mapping.

`sso_org_unmapped` is the one failure an operator can act on, which is why it is
distinct from the generic slug. It still leaks nothing: it says only that this
deployment does not know that IdP organization.

Single-tenant runs none of the above. The mapping repository is never consulted
and the behaviour is byte-for-byte what it was before.

## Enterprise anchor (the only membership a login writes)

One code path serves first-time and returning users, so the two cannot drift:

- **`users.enterprise_id` is the only membership a login establishes**
  (ADR-017 D3). One column, no roster table — and it is what every later read is
  scoped by. **No `organization_members` row is written by sign-in, ever**: an
  organization is a billing target created by payment (D5), so a sign-in cannot
  know of one and must not invent one. An account signs in, lands in its
  enterprise, and is in no organization until somebody pays for it.
- **Enterprise guard.** If the account already belongs to an enterprise and it
  is not the one this login resolved, the login fails closed with
  `enterprise_mismatch` — with exactly one narrow exception: an account anchored
  to **its own live personal enterprise** moving onto an operator-mapped company
  enterprise (see [Switching to a company](#switching-to-a-company-enterprise-adr-016-d5-as-amended)
  below). Moving an account any other way between enterprises is a deliberate
  operator act, never an implicit consequence of an IdP claim.
- **Idempotent.** An account already anchored to the resolved enterprise is a
  no-op; nothing is written.
- **Fail closed.** An anchor write that fails takes the login down with it. A
  just-in-time account may survive that failure, but no org-less session does:
  the next attempt heals it, because the anchor step is idempotent.

Just-in-time provisioning anchors the new account directly to the resolved
enterprise at creation — never the Standalone sentinel under multi-tenant.

Binding the tenant before provisioning also fixes the ADR-015 PR-7 known
precondition for free: the JIT audit write stamps `get_current_enterprise_id()`,
which under multi-tenant previously had no tenant to stamp — so the audit row
either failed its FK or landed unreadable.

## The enterprise rides the token chain; the organization (if any) is attribution

The `users` table carries `enterprise_id` directly (NOT NULL) — unlike the
organization, which has no column on `users` at all: affiliation, when it
exists, is a row in `organization_members`, and it is never what a login binds.

The isolation claim therefore needs nothing carried between legs: it is minted
straight from `users.enterprise_id` at token-mint time
(`resolve_enterprise_claim`), which the callback's anchor step has already
written. The billing claim (`organization_id`) is genuinely optional and, for a
login, is normally **absent** — a sign-up resolves no organization (D5), so the
completion-code payload carries none and the minted token names no organization
at all. That is the correct statement about an account nobody is paying for.

| hop | carrier |
| --- | --- |
| anchor write (callback) | `users.enterprise_id` — the isolation input for every hop after this one |
| exchange → tokens | `resolve_enterprise_claim(user)` mints `enterprise_id` from the (now-anchored) user row; `resolve_billing_organization(user)` mints `organization_id` only when the user carries one |
| access token | `enterprise_id` claim (always present), `organization_id` claim (present only when the account is in an organization) |
| refresh token | same two claims, same rule |
| `/auth/refresh` | re-derived from the reloaded user's `enterprise_id`; no fallback to a stale claim |
| oauth refresh grant (`POST /auth/oauth/token`, `grant_type=refresh_token`) | same re-derivation, in `OAuthService.refresh_access_token` |
| service-account mint | `provision_service_account_credential` stamps the operator-supplied enterprise on the account before signing, which is where a non-human actor's chain begins |

A service account has no login to derive a tenant from, so the operator supplies
the enterprise at provisioning time (`fm-provision-service-account --enterprise-id`).
Provisioning refuses at mint whatever `bind_request_enterprise_context` would
refuse at bind — the sentinel enterprise in any mode, and a missing enterprise
under multi-tenant — so the misconfiguration surfaces to the operator minting
the credential rather than as a rejected API call later. Operator procedure:
`docs/operations/security/service-account-credentials.md`.

`resolve_enterprise_claim` remains the single place that decides what goes in
the isolation claim, and the #850 guards remain live backstops on both ends:
there is deliberately **no fallback to the user row for a token minted before
this cutover** — the claim is the only isolation input from day one, and a token
without it is refused rather than honoured — and `bind_request_enterprise_context`
refuses a verified request whose claim is empty or sentinel-valued under
multi-tenant.

## What is re-verified, and when

- **The anchor is verified at login only.** An access token is valid until it
  expires; a refresh token keeps rotating the same enterprise.
- **An enterprise move requires re-login** (outside the one narrow
  personal-to-company exception below). Changing a user's tenant is an operator
  action; their existing tokens keep the old claim until they expire or are
  revoked.
- **Revocation is the per-user watermark.** Re-anchoring an account does not
  invalidate an outstanding token by itself — bump the user's revocation
  watermark to end the session immediately.

## Provisioning

`fm-provision-sso-org` idempotently creates the enterprise, an organization
inside it, the organization's default team, and the mapping row. It runs with
the RLS-owning database role because it writes rows for a tenant that does not
exist yet, and it refuses to remap an IdP organization that already points at a
different FaultMaven enterprise — remapping is a deliberate operator act, not a
script default. Reusing an existing enterprise (matched by `--slug`, not named
with `--enterprise-id`) prints a loud reuse warning, because under ADR-017 the
enterprise is the isolation boundary and a slug collision there lands a new
customer's users inside somebody else's wall.

Admin binding is manual and post-hoc (ADR-015 D5): no login path grants elevated
roles, so the first user signs in via SSO and an operator promotes them with the
existing role scripts. There is no allowlist. This holds for personal tenants
too — a personal enterprise's single member holds no elevated role, and the
platform tier stays platform-only.

Operator procedure: `docs/operations/sso-org-provisioning.md`.

## Personal tenants

**Refs:** ADR-016 D5 (amends ADR-015), ADR-017 D3 (re-aims D5 from a personal
*organization* to a personal *enterprise*), #1045.

Any visitor can sign in with a personal email and use FaultMaven as an
individual. Because `enterprise_id` *is* the RLS key and is NOT NULL on the
tenanted tables and on `users`, "individual with no company" has no data model
to live in — so it necessarily means an **auto-provisioned personal
enterprise**: invisible in the UI, real in the database. It creates no
organization and no team.

### The switch

| setting | meaning | default |
| --- | --- | --- |
| `SSO_JIT_PERSONAL_TENANT_ENABLED` (`AuthSettings.sso_jit_personal_tenant_enabled`) | whether an org-less identity may sign up at all (personal domain → private enterprise; any other domain → that domain's shared enterprise) | `false` |
| `SSO_JIT_PERSONAL_TENANT_MAX_PER_HOUR` (`AuthSettings.sso_jit_personal_tenant_max_per_hour`) | ceiling on NEW personal enterprises per rolling hour, deployment-wide (the domain arm is not bounded by it — see [A ceiling on provisioning](#a-ceiling-on-provisioning)) | `20` |

Both are multi-tenant (Cloud) only; single-tenant never decides a tenant.

Off, an identity with no IdP organization is refused exactly as it was before
this feature existed.

Both are read through `get_settings()` **at the point of use** rather than
captured at composition time, so neither can become a documented knob that
nothing consults. That is deliberately *not* a live-reload claim:
`get_settings()` is a process singleton, so changing either variable takes
effect on the next process — a restart or a redeploy, like every other setting
in this repo.

Sign-up does **not** open when this ships. ADR-016 D5 sequences three hard
preconditions ahead of it: the two-tenant surface probe green on the Postgres
lane (#1317), the live two-tenant assertion recorded by the owner (#1252), and a
per-tenant LLM usage cap that fails closed at the limit. The cap is
["The daily turn cap"](#the-daily-turn-cap) below; open sign-up plus uncapped
compute is an open bill.

### What sign-up derives, and what it creates

Reached only when the identity carries no IdP organization
(`_resolve_signup_enterprise`). Exactly one fact is derived — the domain of the
verified email — and it splits two ways:

- **A personal domain** (`PERSONAL_EMAIL_DOMAINS`) → a **private enterprise per
  account** (below).
- **Every other domain** → the **enterprise for that domain**
  (`enterprises.get_or_create_for_domain`), created by the first sign-up from it
  and joined by every later one. **No row of its own** is needed for this arm:
  the domain is re-derived from the verified email on every login, so there is
  nothing to fall out of step with. **No IdP organization is created either** —
  this deployment has no mapping for the domain and does not invent one; a
  company that brings its own IdP organization is onboarded deliberately
  through `sso_org_mappings`, and a mapping minted here on behalf of whoever
  signed in first would be that decision made by an accident of ordering.

Both arms end in the same `_bind_and_verify_enterprise` tail as the mapped
branch, so none of the three can acquire a check the others lack.

**A refused login writes nothing.** Every refusal this callback can still make
is evaluated by `_signup_preflight_refusal` (and, for the personal arm,
`_personal_anchor_refusal`) *before* any write, on either side: an existing
account that is deactivated or deleted; for a subject with no account, an
unusable email and an email a different account already owns; and, for the
personal arm only, an account already anchored to something that is neither
absent nor a retired personal tenant nor its own live one.

### The personal arm: what first sign-in creates

The rows `_provision_personal_tenant` creates, in the same order and mostly in
one transaction, triggered by a login rather than an operator:

1. an **IdP organization** holding that one member (network call, ahead of the
   database transaction);
2. the FaultMaven **enterprise**, slug `personal-<key>`, name `Personal`;
3. the **`sso_org_mappings`** row binding the IdP organization to it;
4. the **`sso_personal_enterprises`** row binding the subject to it.

**Three rows in the database transaction, not five: a sign-up creates no
organization and no team** (ADR-017 D5/D4) — the two-organization,
two-mapping-table, plus-team shape the pre-ADR-017 design used is gone.

On the IdP side, a WorkOS organization holding that one member — its membership
is confirmed **last**, after the database commit (see
[Order of writes](#order-of-writes-and-why-every-partial-state-recovers)).

### Why the login path does not need the owner role

`fm-provision-sso-org` demands the RLS-exempt owner DSN because it resolves an
enterprise by `(enterprise_id or slug)` — an id-blind lookup the `organizations`
policy cannot satisfy pre-bind. The login path has no such lookup: it *derives*
the enterprise's slug from the subject and binds it as the tenant context
**before** the transaction opens, so the engine's `begin` listener writes it
into `app.current_enterprise_id` and the RLS policy (no `FOR` clause, so
`USING` doubles as `WITH CHECK`) accepts every row. The subject-keyed
`sso_personal_enterprises` table is what stands in for the CLI's slug lookup,
and it is untenanted, so "which enterprise is this?" is answered before RLS is
in the way.

### `sso_personal_enterprises`

| column | meaning |
| --- | --- |
| `subject` | PK — the IdP's stable subject (`user_01H…`), never an email |
| `provider` | SSO provider key, `workos` today |
| `enterprise_id` | FK → `enterprises`, `ON DELETE CASCADE`, `UNIQUE` |
| `provider_org_id` | the IdP organization minted to hold that one member |
| `membership_confirmed` | whether the IdP-side membership was established |
| `retired_at` / `retirement_state` | set by `fm-personal-tenant retire`; `NULL` while live |
| `created_at` / `updated_at` | server-defaulted, timezone-aware |

`subject` (not `enterprise_id`) is the primary key — a subject owns at most one
**live** personal enterprise, which is what makes first-login provisioning
idempotent and arbitrates a race between two concurrent first logins (the
loser's INSERT violates this key and rolls back). `enterprise_id` is unique in
the other direction — a personal enterprise belongs to exactly one subject.

**Why membership cannot be the lookup.** A returning individual's callback may
report no organization at all — AuthKit populates one only when the sign-in was
organization-scoped. The obvious alternative, reading `organization_members` (or
even `users.enterprise_id` directly), is unavailable: both are RLS-tenanted and
no tenant is bound at callback time, because binding the tenant is what this
lookup decides. The subject is the one identifier every login carries.

**Why not reuse `sso_org_mappings`.** It is keyed on the IdP's *organization*
id, which the callback need not carry, and it is 1:1 per provider-organization —
a personal tenant's one row there is already spent on the IdP organization that
holds the member. So the subject binding needs its own table, untenanted for
exactly the same reason as its sibling.

Both shapes of a returning login therefore agree: with no organization echoed,
the subject row resolves the tenant; with one echoed, the ordinary mapped path
resolves it through the `sso_org_mappings` row first sign-in wrote — to the same
enterprise.

### Naming

The enterprise is called `Personal` — one constant,
`modules/auth/domain/personal_tenant.PERSONAL_ENTERPRISE_NAME`. The slug and the
IdP `external_id` are two renderings of one derived key: a 128-bit BLAKE2b
digest of the length-prefixed `(provider, subject)` pair, rendered
`personal-<32 hex>` (`personal_enterprise_slug`). A **domain** enterprise's slug
is a separate, domain-separated digest (`domain_enterprise_slug`, prefix
`domain-`), so `personal_key_of_slug` — the test an operator command uses to
confirm it is looking at somebody's private tenant — can never mistake one for
the other.

The key carries no PII (the input is the IdP's opaque subject handle, and the
digest is one-way), cannot collide across users, and is **deterministic** — which
is what makes a retry safe: an attempt that minted the IdP organization and then
failed to commit re-derives the same `external_id` and finds it rather than
minting a second. Fields are length-prefixed rather than separator-joined
because any separator can appear inside a value, and an ambiguous encoding lets
a crafted subject land on another pair's tenant.

### Order of writes, and why every partial state recovers

Three steps, in this order: **IdP organization → database commit → IdP
membership**. Each is placed so that stopping after it leaves a state the next
sign-in repairs by itself.

| stopped after | what exists | what the next callback does |
| --- | --- | --- |
| the IdP organization | an organization nobody is a member of | the IdP still reports no organization, so the same branch runs and finds it again by its derived `external_id` — no duplicate |
| the database commit | the enterprise, with `membership_confirmed` false | resolves from the subject row and finishes the membership; no second tenant |
| the membership | everything, flag unset | the IdP may now echo the organization, sending the login down the **mapped** branch — which resolves, because the mapping committed one step earlier |

**The membership is last on purpose.** It is the IdP-visible change: a membership
is what makes AuthKit start echoing the organization. Creating it before the
commit is the cheaper order and the unrecoverable one — an echoed organization
whose mapping never committed sends every later login to the mapped branch,
which finds no mapping and refuses `sso_org_unmapped` **permanently**, with no
path back. `membership_confirmed` exists so the repair costs a provider
round-trip only on the logins that actually need it, not on every returning
sign-in.

### Collisions are not races

A constraint violation is ambiguous: it is either a concurrent attempt by the
same subject (adopt its tenant) or a key somebody else holds (refuse, loudly).
Only the untenanted subject row distinguishes them, which is why the shared
writer refuses unconditionally and the login path interprets. When the subject
row does not explain the violation, the repository asks the database *which key
collided* and logs `colliding_key` / `colliding_value` — the previous message
named an enterprise id this attempt invented and never committed, which an
operator could not look up anywhere.

### A ceiling on provisioning

The switch bounds nothing about volume: every consumer-mail subject the IdP
vouches for would mint an IdP organization and its own enterprise, so a scripted
sign-up loop exhausts the provider's organization quota.
`SSO_JIT_PERSONAL_TENANT_MAX_PER_HOUR` (default 20) bounds **new personal
enterprises per rolling hour, deployment-wide**, and is checked before the IdP
call. It bounds provisioning only — an existing tenant resolves from its subject
row without ever consulting it, so tripping the ceiling cannot lock out people
already using the product. Global rather than per-subject because the abuse
shape is many subjects, not one retrying.

**The domain arm is deliberately not bounded by it** and needs no ceiling of its
own: it mints one row per *domain*, ever, and reaching a new domain means
controlling one and having the IdP verify an address at it — which is the bar
this ceiling exists because consumer mail does not clear.

This is **not** the per-tenant LLM usage cap. That one bounds what a tenant may
*spend* rather than how many tenants exist, and is
["The daily turn cap"](#the-daily-turn-cap) below.

### Other refusals

- **A subject row pointing at the Standalone sentinel** → refused. Under
  multi-tenant the sentinel identifies the deployment, not a tenant (#850).
- **An account already anchored to something that is not released for
  re-provisioning** → refused with a reason keyed on the anchor's kind
  (`personal_tenant_retired`, `personal_anchor_enterprise_deleted`,
  `personal_anchor_enterprise_missing`, or the generic
  `personal_account_already_anchored`) — an employee arriving unscoped must not
  be handed a personal enterprise of their own.
- **Membership write fails** → the login is refused and the tenant is left in
  exactly the state the operator path leaves a freshly provisioned one in. The
  next attempt heals it, because the ensure is idempotent.

### What WorkOS guarantees, and what it does not

`external_id` is unique per WorkOS environment, so `get_organization_by_external_id`
is what makes the IdP half idempotent and a duplicate create is refused rather
than silently accepted. That refusal is resolved by re-reading, never by minting
a second organization.

**Which status a duplicate produces is not verified against the live API.** A
duplicate unique field is a 409 in some WorkOS surfaces and a 422 in others, so
both `ConflictError` and `UnprocessableEntityError` are treated as conflicts and
resolved the same way. Catching only one would turn the common retry into a
permanent refusal; catching both costs nothing, because the recovery is a
re-read that either finds the winner or re-raises.

Membership creation is likewise get-or-confirm: a refused create is **confirmed
by listing the memberships**, not inferred from the exception type, and a
refusal that cannot be confirmed fails the login closed. The listing passes
**every status the SDK's enum defines**, derived from the enum rather than
spelled — the SDK's default lists `active` only, so a `pending` or `inactive`
membership left by an earlier attempt would read as "not a member", and since
the create that follows a refusal is the same create that was refused, every
retry would refuse again, permanently.

There is no cross-service transaction. If the IdP accepts the create and the
response is lost, the organization exists and the next attempt adopts it — that
is the direction the ordering was chosen for. Nothing here reconciles an IdP
organization whose tenant was never created; `sso_personal_enterprises.provider_org_id`
exists so an operator can (`fm-personal-tenant purge-idp-org`).

### The daily turn cap

**Refs:** ADR-016 D5.3, re-keyed to a billing subject by ADR-017 D5, owner
decision 2026-09-03.

The third precondition. Self-service sign-up hands anyone who can authenticate
an enterprise of their own, and an investigation turn is the one operation in
the product that spends LLM compute without a further gate.

**Turns per billing subject per UTC calendar day, as a count.** Not tokens: the
number is one the owner tunes against measured usage, and a count is the only
unit a refusal can state honestly to the person it refuses. Not a rolling
window either — a sliding 24 h window cannot promise a reset instant, so the
message would have to lie or say nothing.

**The billing subject** (ADR-017 D5) is the account's **organization** when it
has one, and the **account itself** when it does not — "personal" is no longer a
flag or a lookup against a personal-tenant table; it is simply the state of
having no organization, which needs no query of its own to detect:

| subject | override | cap |
| --- | --- | --- |
| **single-tenant deployment** | any | **uncapped, decided without a query** |
| account, no organization | none | `TENANT_DAILY_TURN_CAP` (default **30**) |
| organization, no override | none | **uncapped** |
| either | `0` | uncapped, explicitly |
| either | `N > 0` | N turns per UTC day |

Single-tenant is answered from the deployment mode before either port is
touched. A self-hosted install pays for its own compute and is not what D5.3
exists to bound — and deciding it this way means an install running with
`RUN_STARTUP_MIGRATIONS=false` keeps working instead of losing every turn to a
usage-allowance message it could never have earned.

Company organizations are uncapped by default because the cap bounds what
*self-service* can spend, not customers.

**Where it is charged.** Inside `InvestigationService.process_turn`, after the
case is loaded, after the access check, and before attachment preprocessing.
The position is the decision:

- everything that can refuse the request for a reason other than "you have spent
  your day" has already run — the route's validation (400/413/422), its case
  lookup (404) and the closed-case check (409) — so a malformed turn, a turn to
  a case that does not exist, and a **probe at another tenant's case id** all
  cost nothing;
- it is ahead of classification and extraction, so a capped tenant does not have
  its files written to storage for a turn that will not run;
- and the invariant holds for **every** caller of the service by construction,
  rather than for every caller that remembered a route dependency.

`tests/integration/api/test_turn_cap_surface_inventory.py` remains as the
secondary check: it reads the live OpenAPI document *and* the live route objects
on every run and fails when an operation that can reach the investigation
service appears without a recorded verdict.

**The ledger.** `turn_usage` — RLS-tenanted on `enterprise_id`, keyed on
`(billing_subject_kind, billing_subject_id, usage_date)`. There is no
`created_at`/`updated_at`: every write after the day's first arrives through
`ON CONFLICT DO UPDATE`, which does not fire SQLAlchemy's `onupdate`, so a
timestamp would freeze at the first turn while looking like it tracked the
last. The reservation is a single statement — `INSERT … ON CONFLICT … DO UPDATE
SET turn_count = turn_count + 1 WHERE turn_count < :cap RETURNING turn_count` —
so the check and the increment cannot interleave, and an empty `RETURNING` *is*
the refusal. Consequently **a refused turn increments nothing**. A table rather
than a Redis counter because D5.3 requires the cap to fail closed: a counter
whose store can be unavailable fails open, and a Redis blip would silently
un-cap every tenant until it healed.

The ledger is a **port** (`ITurnLedger`) with a SQL implementation and an
in-memory one. The in-memory one is not a convenience: it is what lets the
enforcement be exercised in unit tests instead of blanked out because it happens
to need a database.

Usage is recorded for every billing subject a cap decision reaches, capped or
not — an organization is never refused, but its counts are what the default is
tuned against.

**At the cap** the turn is refused with **429**, `x-error-code:
TENANT_TURN_CAP_EXCEEDED`, a `Retry-After` naming the next UTC midnight
(rounded by the same `window_math` helper the rate limiter uses), and a message
that states the limit, that it is daily, when it returns, and that reading cases
and the knowledge base is unaffected. `x-error-code` is in
`cors_expose_headers`, because the built-in Dashboard panel is a cross-origin
caller and this header is the only thing distinguishing "come back tomorrow"
from the rate limiter's "slow down".

Nothing else is refused: sign-in, every read, case listing and the knowledge
base never reach `process_turn`. A capped tenant also keeps its reading
throughput — since fm#994 reads sit on their own per-session rate-limit windows,
so a refused turn cannot exhaust the quota a caller needs to look at its own
cases.

**Failure direction.** Whichever question cannot be answered — the subject's
kind or its override — the subject is capped at `TENANT_DAILY_TURN_CAP`. An
unreadable *override* is emphatically not "no override": an organization
carrying an override of 50 would otherwise degrade to uncapped on a failed
read, the exact inversion D5.3 exists to prevent. If the reservation itself
cannot be written the turn is refused with **503** and `x-error-code:
TENANT_TURN_CAP_UNAVAILABLE` — a distinct answer from the 429, because telling
somebody their daily allowance is spent when the ledger merely failed to write
is a false statement about their own account.

**The clock is sampled once** per reservation and both the charged day and the
reset instant derive from it, so a turn refused at 23:59:59.98 cannot be charged
to day D and told to return at the midnight after D+1.

**The unit is consumed when the turn is accepted**, before the model runs, and
is not refunded if the turn later fails. Refunding would be a free-retry channel
for anyone who can make a turn fail.

**The operator control** is `fm-set-turn-cap --enterprise-id <id> --organization-id <id>`
(or `--account-id <id>`, read-only), which writes `organizations.daily_turn_cap`
through the organization repository and renders its `--show` output from the
same `CapPolicyResolver` the enforcement uses — so an operator reads the verdict
the next turn will meet rather than a second description of the policy. Only an
organization is writable: an account in no organization has no row to carry an
override, and the command refuses a write against `--account-id` rather than
dropping it silently. The value is read on every turn, so raising or clearing
one subject's cap takes effect on its **next turn**, with no restart and no
redeploy. `TENANT_DAILY_TURN_CAP` itself is an ordinary setting and moves only
with a redeploy. See
[the operator runbook](../../operations/sso-org-provisioning.md#changing-one-tenants-daily-turn-cap).

### Switching to a company enterprise (ADR-016 D5 as amended)

A personal-tenant user later placed in a mapped company enterprise **is
re-anchored**, not refused. Without this they meet `enterprise_mismatch` forever,
because their account is anchored to the enterprise their own personal tenant
owns — which contradicts the owner's stated intent in #1045 that switching to a
company later works.

The exception is deliberately narrow, and each narrowing does a job:

- It fires only on the **mapped** branch, so the company enterprise was
  operator-provisioned. Nobody re-anchors themselves.
- It fires only when the account's current enterprise is the one *this subject's
  own personal tenant* owns, established from the untenanted
  `sso_personal_enterprises` row — never from an enterprise's name or slug. A
  company-to-company move is still refused.
- Isolation does not weaken: the account still lands with no organization and
  no team of its own; those remain billing/consent facts a human grants
  separately.
- The move is persisted **before** the personal binding is retired. The reverse
  order would leave an account anchored to a personal enterprise whose subject
  row no longer names it, and no login could repair that.

The personal tenant is **not migrated** — the cases stay where they are. What is
removed is the *binding*, so a later unscoped login cannot resolve the user back
into a tenant they can no longer enter; it meets the "already anchored" refusal
instead.

The inverse — an employee whose first login is unscoped — is refused with
`reason=personal_account_already_anchored` and **never provisioned**. Anchoring
them to a personal enterprise would lock them out of their company.

### Non-goals (owner-accepted, #1045)

Personal → business is **not a data migration**. SSO matches on subject, so a
different email is a different WorkOS user and therefore a new FaultMaven
account; JIT deliberately refuses to link by email (ADR-015 D4); and the
individual's cases live in the personal enterprise behind a NOT NULL RLS key, so
moving them is an enterprise move, not an owner change. Neither an SSO-identity
link nor an enterprise move for the data exists. Re-anchoring (above) moves the
*account*, not its data.

There is also **no lifecycle for a dormant personal tenant beyond retirement**.
`fm-personal-tenant retire` takes a personal enterprise out of service
(soft-delete, revoke tokens, decide the subject's next org-less login), but a
re-anchored user's *old* personal enterprise keeps existing, with its cases,
unreachable by that user, until an operator retires it explicitly.

## Rejected alternatives

- **Mapping columns on `organizations` (or, under ADR-017, on `enterprises`
  directly rather than a separate table)** — unreadable pre-bind under RLS, and
  reading them would drag an RLS-bypassing owner-role query into the
  unauthenticated auth path.
- **Deriving the claim from membership at refresh time** — the same RLS problem
  (no tenant is bound when the refresh token is validated), and it would put an
  owner-role read on the hot token path.
- **Email-domain-based mapping for the *IdP-organization* case** — domains are
  unverifiable and non-unique (contractors, acquisitions, shared consumer
  domains); the IdP's organization id is the stable key the IdP already
  asserts. (The email domain *is* used, deliberately, for the no-IdP-organization
  sign-up case, where verifying a domain claim is not what D3 asks for — see
  [What sign-up derives](#what-sign-up-derives-and-what-it-creates).)
- **Storing a subject's personal tenant in `sso_org_mappings`** — the table is
  keyed on the IdP *organization* id and is 1:1 per provider-organization, so a
  personal tenant's one row there is already spent on the IdP organization
  holding the member. A subject row would have to displace it.
- **A `personal_enterprise_id` column on `users`** — the account's enterprise
  anchor already lives on `users.enterprise_id` and RLS is the authority on it;
  adding a second column for "which enterprise is my personal one" would make
  it two authorities for accounts that have re-anchored.
- **Creating the FaultMaven tenant before the IdP organization** — the cheaper
  ordering, and the wrong one: it leaves a tenant with no IdP organization,
  which the next login adopts as if it were complete. The chosen order leaves
  only residue that is invisible and self-healing.
- **A shared enterprise for all individuals** — it would make the
  `enterprise_mismatch` guard vacuous across every personal tenant, and the
  enterprise is what makes "a personal account can never share" true by
  construction rather than by rule.
