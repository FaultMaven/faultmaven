# Owner decision brief — 2026-09-03 — web-first surface programme (ADR-016)

**Owner approved this brief in full on 2026-09-03 with no additional input: every recommendation below is now the decision.** Resulting choices: D1 A · D2 A (default 30 turns per personal tenant per UTC day, tune after a measured sim run) · D3 A · D4 A · D5 B · D6 B now, A later · D7 A · D8 A now + C as follow-up · D9 owner runs the two WorkOS checks · copilot audit fail-closed scheduled after the shared-UI sequence.

Decisions that are **mine to raise and yours to make**, with the facts needed
to decide. Status key: ⬜ open · ✅ decided · ⚫ closed by events

Design of record: `faultmaven-doc-internal` ADR-016 (branch
`docs/adr-016-web-first-surface`, PR to follow). Work packages and their locks:
dashboard#119, copilot#229, copilot#230, dashboard#120, fm#1317, fm#1045,
website#42, fm#1252 (owner-run).

---

## D1 ✅ fm#1252 — the live two-tenant assertion is yours to run, and it gates opening sign-up

**Facts.** Every link of the tenant chain is probed in CI (RLS as a non-owner
role, the request binder, forged and injected org claims, the KB filter). What
is NOT recorded anywhere is that tenant A cannot observe tenant B on the *live*
deployment. fm#1252 lists the surfaces and says why an agent cannot do it: the
permission classifier correctly blocks binding a tenant. Four orgs and five
users exist there, so the material for the test is in place. A correction to my
own earlier framing: "isolation was never adversarially tested" was stale; it is
tested at every link, just not end to end on the live box.

**What I cannot determine.** Whether the live RLS role is the limited
`faultmaven_app` on the current image; only a `printenv`/`\du` on the pod shows
it.

**Options.**
- A. You run the fm#1252 walk (seed identifiable data as B, probe as A, record
  both) before fm#1045 opens sign-up. ~1 hour with the CI probe from fm#1317 as
  the script.
- B. Open sign-up on the strength of the CI probes alone.

**Recommendation: A.** Self-service makes strangers tenants; the CI lane proves
the code, not the deployment. Row 8 in ADR-016 does not merge before this is
recorded.

---

## D2 ✅ Per-tenant LLM usage cap — the number and the behaviour at the cap

**Facts.** `per_session_hourly` rate limiting protects LLM compute per
*session*; nothing caps spend per *tenant*. A self-serve visitor with a fresh
tenant is bounded only by how many sessions they open. Cloud CHAT runs on
`gpt-5.6-luna`; a full investigation turn is on the order of tens of thousands
of prompt tokens (the prompt is ~3000 lines plus schema).

**What I cannot determine.** Your acceptable free-tier spend per stranger per
day. I have no per-turn cost figure for the current model on the current
prompt; the sim harness can measure one on request.

**Options.**
- A. Daily cap per personal tenant on investigation turns (a count, not
  tokens), refused with a clear message and a "come back tomorrow"; operator
  override per org. Simple, legible, measurable.
- B. Token/cost budget per tenant. Accurate, but needs per-provider cost
  tables and a metering write per call.
- C. No cap; rely on the session limiter.

**Recommendation: A**, with the count set after one measured sim run gives a
per-turn cost. Behaviour at the cap: refuse the *turn*, never the sign-in or
the read paths, so an over-cap user can still see their cases.

---

## D3 ✅ Sign-up policy — who may create a personal tenant

**Facts.** WorkOS AuthKit handles email verification on the hosted sign-up
form. ADR-016 D5 opens the no-org branch only; company orgs stay operator-
provisioned. Personal → business is not a migration (accepted, fm#1045).

**Options.**
- A. Any verified email. Disposable-domain blocking left to WorkOS settings.
- B. Any verified email, plus a platform-side blocklist of disposable domains.
- C. Allowlist of domains (defeats the purpose).

**Recommendation: A** for beta, revisit with data. The cap in D2 is the real
abuse control; a blocklist is a maintenance burden with little bite.

---

## D4 ✅ What the personal organization is called where the UI shows it

**Facts.** The account menu in both frontends shows the organization name from
`/auth/me`. A JIT personal org needs a name and a slug that cannot collide.
The KB already uses `personal` as a scope name, and the brand lexicon avoids
tier words.

**Options.**
- A. Name "Personal" (constant), slug derived from the user id. The UI shows
  "Personal" in the account menu.
- B. Name derived from the email local part ("alice's workspace").

**Recommendation: A.** Constant name, id-derived slug; no PII in the slug, no
collision logic beyond the id.

---

## D5 ✅ Where the shared Copilot UI lives — spike landed, my recommendation is in

**Facts (copilot PR #232, verified 2026-09-03).** The UI's real boundary is
~27 direct browser-API references plus a transitive closure of ~106 more in
the extension's library modules, including a second auth stack and API client.
The proof renders the unmodified chat UI in a plain Vite page with a stub
session, no sign-in anywhere, capture button present and explaining itself;
the extension artifact is byte-identical. Seven concrete risks found, among
them two Tailwind configs that already drifted silently and a zero-importer
stale type copy in the Dashboard.

**Options.**
- A. Package published from the copilot repo (needs a registry and a version
  bump per change).
- B. Git dependency pinned by SHA to `packages/copilot-ui`, plus a REQUIRED
  staleness check on the Dashboard's main. Same pin-and-gate idiom both repos
  already run for the API contract.
- C. Dashboard moves into the copilot repo. Divergence becomes impossible, at
  the cost of merging two release trains and the store artifact guard.

**Recommendation: B**, approved by me on the PR as the working decision. It
is yours to veto because it (1) makes the Dashboard build depend on a pinned
git SHA of another repo, including on Vercel, and (2) requires adding a
required status check to the Dashboard's main, which has none today. If you
prefer C, say so before PR 7 of the sequence (the relocation); everything
before it is identical under B and C.

Eight serial PRs; one Chrome Web Store submission at the end. PR 1 is #232,
ready for merge.

---

---

## D6 ✅ fm#1318 — operator user-admin routes reach across tenants with no audit (found by the fm#1317 probe)

**Facts.** A `platform_admin` bound to org A can list, read, deactivate, re-role,
revoke tokens for, and delete org B's users through `/admin/users*` and the
two `/auth/users*` operator routes; `GET /auth/users` reports a deployment-wide
total. No tenant predicate, no audit row. `get_user_details`' docstring claims
a confinement that does not exist. An **organization** admin is refused all of
it, so no tenant can reach another tenant this way. The case surface next door
requires a break-glass grant plus an audit row for the same operator.
Reachable only by the operator role, i.e. by you.

**Is it a gate for self-service sign-up?** No: strangers cannot hold
`platform_admin`. It is a least-privilege and audit-consistency gap in the
operator model (ADR-012 D9), pinned by two executable descriptions in the
probe that go red when it is fixed.

**Options.**
- A. Bring user administration under the break-glass model: operator reads and
  mutations of another tenant's users need a grant and write an audit row,
  like case content. Consistent; the larger change.
- B. Add the tenant predicate only (operator sees and mutates users in the
  bound org; cross-tenant needs a grant), audit later.
- C. Accept as operator privilege, delete the false docstring, keep the pins.

**Recommendation: B now, A when the operator console is next touched.** The
false docstring goes in either case.

---

## D7 ✅ `/debug/cases/{id}/causal-graph` has no authentication and no case-access check

**Facts.** It calls the repository directly; RLS is its only guard. Not
registered under `ENVIRONMENT=production`, so Cloud is unaffected. Under
multi-tenant with RLS an unauthenticated caller binds the non-org sentinel and
reads nothing (measured). On a standalone SQLite deployment it serves any case
to any caller, but standalone is single-user by design.

**Options.** A. Require authentication and the normal case-access check (a
few lines; the probe's control leg already exercises the route). B. Leave it,
documented as dev-only.

**Recommendation: A**, folded into the next core PR that touches the case
routes (fm#1045 is next), not a PR of its own.

---

## D8 ✅ Personal-tenant lifecycle: no retire path, and what "switching to a company" leaves behind

**Facts (fm#1320 review, 2026-09-03).** Personal tenants are the first tenants a
*user* creates rather than an operator, and nothing retires one: organization
delete is soft, the subject row then makes every login for that subject
`personal_org_unavailable` with no operator recovery short of hand-deleting
rows (and the enterprise row does not cascade). The PR now adopts my working
rule for switching: a mapped company login re-anchors a personal-enterprise
user to the company; the personal tenant goes **dormant** (cases stay, user
cannot enter it). That matches "no data migration" as you accepted it, but it
is a product choice.

**Options.**
- A. Dormant (current): the personal org's cases become inaccessible to the
  user after switching; an operator could later re-anchor by hand.
- B. Accessible: the user keeps entering the personal org via unscoped logins
  and the company org via scoped ones. Contradicts ADR-013's one-enterprise
  anchoring and needs a design pass on `_ensure_org_affiliation`.
- C. Dormant now, plus a small operator CLI to retire or re-anchor a personal
  tenant (the "retire tenant" primitive the review asked for). One more lane.

**Recommendation: A for this PR, C as the follow-up before the switch is
flipped**, because "user cannot recover, operator has no tool" is the state an
open funnel will reach within days.

---

## D9 ✅ WorkOS facts the code assumes and nobody has measured against the live API

- `external_id` is unique per environment and a duplicate create returns a
  **409** (the code now also tolerates 422 after the fix round).
- `list_organization_memberships` returns only `active` memberships unless
  `statuses` is passed.
- `delete_organization` frees the `external_id` for reuse (the retire tool's
  `--next-login fresh-tenant` depends on it; `refuse` mode does not).

**Ask:** one manual run against the real WorkOS environment before the switch
is flipped: create an org with a fixed `external_id` twice and record the
status code; create a membership twice and record the response. Ten minutes;
it decides whether the retry paths are real.

---

## D10 ⬜ Dashboard built-in panel (dashboard#120) — three things only you can do before it merges

**State (2026-09-04).** Branch `feat/built-in-copilot-panel-120` @ `84c85b631`
is built and proven in jsdom against the relocated package (copilot PR 7,
unopened): web host adapter over the existing AuthManager, panel only inside
the authenticated shell (dynamic import — unreached on `/login`), case-detail
transcript replaced (the break-glass operator view keeps the read-only
renderer, correctly), first-run landing, page-side advertisement, pin/
staleness/contract gates proven red-then-green. Two small package gaps go
into copilot PR 7b (`initialCase` prop; session-key export).

1. **Required checks on the Dashboard's main** (contexts UPDATED 2026-09-04: the
   bundle gates now run inside `Lint & Type Check`). No required-status-checks
   rule exists; the org ruleset covers all repos and a PUT replaces
   `bypass_actors`, so the safe move is a NEW repo-scoped ruleset. Run, once
   PR 8 is open and its two jobs have reported at least once:
   ```
   gh api --method POST /repos/FaultMaven/faultmaven-dashboard/rulesets --input - <<'JSON'
   { "name": "required-checks-copilot-ui", "target": "branch", "enforcement": "active",
     "conditions": { "ref_name": { "include": ["~DEFAULT_BRANCH"], "exclude": [] } },
     "rules": [ { "type": "required_status_checks", "parameters": {
         "strict_required_status_checks_policy": false, "do_not_enforce_on_create": false,
         "required_status_checks": [ { "context": "Copilot UI Pin Check" }, { "context": "Lint & Type Check" } ] } } ] }
   JSON
   ```
2. **A browser smoke before merge.** Nobody has yet rendered the panel in a
   real browser against a live API through this adapter. I will run one on a
   standalone stack (passwordless dev-login) with headless Chromium once
   copilot PR 7 is on main and PR 8 is open; you should also click through
   it on your own stack (layout, transcript framing, input bar, a turn).
**Smoke run 1 (2026-09-04, standalone API :8091 + production Dashboard build,
Playwright; screenshots under the session scratchpad `smoke/`).** Sign-in page
clean (no panel markup, no panel chunk fetched) · advertisement attribute and
message present · layout, tokens and icons fine · **BLOCKING: the panel
crashed on every mount — the package needs a TanStack `QueryClientProvider`
and the Dashboard installs none** (the jsdom tests mocked the package, so they
could not see it; with a provider added as a probe the panel rendered fully,
case A/B transcripts correct, capture explains itself, zero console errors) ·
sign-in hard-codes a redirect to `/kb`, bypassing the first-run landing ·
cross-tab sign-out reaches the panel but not the app shell · the panel's own
sidebar/account row duplicate the Dashboard's inside its page. Fixes
dispatched: package side (7b amended: self-contained provider, `chrome:
'embedded'`, placeholder copy) and Dashboard side (real-package render test,
login redirect, shell cross-tab sign-out). Smoke run 2 follows before merge.

**Package amendment (copilot 7b @ e9baf67) after smoke 1:** the panel now owns
its TanStack query client — which also closed a live defect in the shipped
extension (two parts of the panel reached DIFFERENT query caches when the host
mounted its own client, so the case header went stale after a turn with
nothing thrown); an `embedded` layout for the Dashboard (no second sidebar or
account row); and the draft-case composer placeholder, previously "Select a
case to start chatting…" while a new case was open, now reads **"Describe
what's wrong, or paste data to start..."** — the implementer's proposed wording,
flagged for you; overrule by reply.

**Smoke run 2 (dashboard @ 52d7a01, package e9baf67): ALL PASS** — sign-in
page clean · zero-case user lands on the panel with a new investigation ·
advertisement present · case detail is the interactive panel with input bar
and self-explaining capture button · A→B navigation correct (reload and
client-side) · layout/tokens/icons fine at 1440×900 and 1024×768 · embedded
chrome: one account identity, one navigation, no panel sidebar · placeholder
copy and enabled composer · cross-tab sign-out takes the panel AND the shell
to `/login` · one LLM turn NOT POSSIBLE here (no key; the send path reached
the provider and the failed turn was correctly not persisted). **Zero console
errors, zero failed requests.** Screenshots and captures copied to
`docs/working/smoke-120/{smoke,smoke2}/`. One finding being fixed now: on the
case-detail page the panel's composer sits below the fold at both sizes (the
panel starts under the case card and is 70vh tall); `/investigate` fills the
viewport correctly.

**Smoke run 3 (dashboard @ 4ef3984, case-detail layout): ALL PASS** — composer
on screen without scrolling at 1440×900 (94 px clearance) and 1024×768; page
never scrolls; header stays; the one scroller is inside the panel; other tabs
scroll within their area; `/investigate` unchanged; zero console errors, zero
failed requests. Evidence in `docs/working/smoke-120/smoke3/`. **One product
judgement for you (not a defect):** at 1024×768 the transcript gets 148 px
(≈5–6 lines) because everything above the panel is fixed height — the 155 px
case card is the cost. Options: leave it (usable, tight); or collapse the case
card on the Transcript tab; or let the card scroll away. See
`41-caseC-longtranscript-1024x768.png`.

**PR 7 (copilot #245) review found a defect that would have shipped an
unstyled panel:** Tailwind does not merge the `content` key across presets,
so after the relocation the extension's stylesheet omitted the package's
classes; the digest guard tracks change, not correctness, and the token test
had built its own config. Fixed (the extension config spreads the preset's
`content`; the test now runs the real config; 366 classes restored). Approved
and green; merge #245 next.

**dashboard#124 review (2026-09-04): HOLD the ruleset command and do not merge
yet.** Eight passes found eight must-fix items, the worst being that the
Kubernetes Dashboard ships `VITE_API_URL=""` (same-origin) and the package's
transport needs an absolute origin, so in the real cloud deployment the panel
would render an EMPTY transcript with no error (every smoke used an absolute
URL). Also: sign-out leaves the previous user's panel state for the next user
on the same browser; a cross-tab account switch is ignored; a successful token
refresh still shows "Session expired"; teammates get a live composer on shared
cases; the package stylesheet restyles the whole app; the zero-case redirect
makes `/cases` unreachable; `h-screen` removes page scroll on short viewports.
Two of my decisions reversed: staleness is advisory, not required (required =
pin shape + contract parity + ancestry), and the Dashboard keeps its unused-
code strictness (the package fixes its own). Fix round in flight across a
package follow-up (copilot 7c) and one Dashboard commit; the ruleset job
names may change — I will send the final command when the fix lands, then
smoke run 5 on the deployment's same-origin shape.

**Red baseline taken (same-origin harness mirroring the Ingress):** all three
reproduced on the current #124 head — empty transcript with NO failed request
and no 4xx (the transport throws before anything leaves the browser), nine
`fm.copilot.*` keys surviving sign-out, and the account-switch mismatch (tab 1
shows the old identity while storage holds the new token; the safe ordering is
sign-out-then-sign-in, the failing one is a direct sign-in from `/login` while
authenticated). Evidence in `docs/working/smoke-120/smoke5-red/`. One more
decision taken: the capabilities endpoint lives only at `/v1/meta/capabilities`,
outside the `/api` prefix the Ingress forwards; a core PR adds
`/api/v1/meta/capabilities` as canonical with the old path kept as a deprecated
alias, and the package switches to it. No infra change.

**2026-09-05 — dashboard#124 head `769e445`:** every review item landed and
verified (my delta mutations red where they should be), all pin legs green
on the merged package. Smoke run 5 then found two blockers (below), fixed in
the final head `ac62ca6`.

**Smoke run 5 (same-origin, `769e445` + core main):** same-origin transcript
FIXED (message renders, capabilities JSON via `/api/v1`, zero errors) ·
account switch FIXED (tab 1 goes to the login page) · refresh-on-401 FIXED
(no "Session expired", one refresh, request succeeds) · style scoping holds ·
entry boundary holds · non-owner read-only NOT TESTABLE on standalone (no
team sharing; needs the cloud stack — you will see it in your click-through).
**Two new blockers, hold the merge:** (a) the Dashboard lost Tailwind's
preflight when the package correctly stopped shipping a global reset — every
page now scrolls 16 px, links are underlined, and 1024 px overflows
horizontally; one-line fix plus a gate; (b) the sign-out purge throws because
the host clears its store while the purge is still running — 8 of 12 panel
keys survive, including cached conversations; fixed by page-lifetime store
singletons on the host and a purge that captures its store in the package.
Plus the Dashboard's own capabilities call still uses the un-routed path.
Fixes landed in `ac62ca6`; smoke 6 below. Evidence: `docs/working/smoke-120/smoke5/`.

**Smoke run 6 (same-origin, dashboard `ac62ca6` + copilot `ef324edd` + core
main `bf41e0f`): ALL PASS — GO.** Preflight restored (three routes, both
viewports: no page scroll, no horizontal overflow, links not underlined, the
1024 px `main` fits) · sign-out leaves 0 of 12 panel keys under both the
cross-tab and the local path, panel unmounted, no `No HostStore installed`
throws · the Dashboard's own capabilities call is the only capabilities
request and goes to `/api/v1/meta/capabilities` (200, JSON) · every earlier
observation and the case-detail layout re-verified at both sizes · whole run:
zero console errors, zero failed requests; the only two console lines are the
expected "Auth state cleared by the host" notices. Repo gates green
(preflight + panel-scoped rules + shared classes; entry boundary; pin legs).
Still untestable on standalone, for your cloud click-through: non-owner
read-only (no team sharing here) and one real LLM turn. Evidence:
`docs/working/smoke-120/smoke6/`. **Do now: run the ruleset command in
item 1 (contexts `Copilot UI Pin Check` + `Lint & Type Check`), then merge
dashboard#124 at `ac62ca6`.** It stays pinned to copilot `ef324edd`; #250
(already approved, merge it) is adopted by a later one-line pin bump.

3. **Visual review.** Adopting the package's global stylesheet adds an
   app-wide `a:hover` opacity and a `button` transition (one design system,
   deliberate); and copilot PR 7's preset test fixed two live theme defects
   (a hover token that never existed; an evidence-bar rung with a stray-zero
   class, set to the solid accent — overrule if the translucent wash was
   intended). Eyes on both.

## Notes, no decision needed

- ✅ **2026-09-05 — ruleset `required-checks-copilot-ui` (id 22325006) LIVE**
  on dashboard main; branch rules now require `Copilot UI Pin Check` and
  `Lint & Type Check`. **Every code-side item of the ADR-016 programme is
  merged, deployed, and gated.** What remains is the pre-flip list in item 2
  (fm#1252 live assertion, D9 WorkOS checks, cap number after a measured run,
  cloud click-through incl. non-owner read-only, website#42 + sign-in copy at
  flip time), and D8's dormant switch stays off until you flip it.

- ✅ **2026-09-05 07:15Z — LIVE VERIFIED after your promotion (infra#304).**
  api.faultmaven.ai runs `sha-bf41e0f`; CD run: migration Job SUCCESS →
  rollout → /health smoke SUCCESS, no rollback; `/api/v1/meta/capabilities`
  → 200 JSON with `managementConsole: true`, `teamSharing: true`; both API
  pods carry the narrowed `OAUTH_REDIRECT_URI_PATTERNS` (infra#303 merged,
  fm#1169 closed); no "applying the default cap" line in the API logs since
  the rollout (turn-cap resolver is reading migrated tables). dashboard#126
  merged (#78 closed); cloud#34 merged. The capabilities regression is
  CLOSED. Smoke worktrees removed.
  ‼ **STILL OWED, the last code-side item: the item-1 ruleset POST.** I tried
  to run it for you and the session's permission classifier refused the
  call, so it must be you. Verified after every merge today: dashboard main
  has ZERO required status checks.

- **2026-09-05 issue sweep (you asked to resolve as many related issues as
  possible before closing the programme).** CLOSED: copilot#230 (the
  programme issue; every deliverable merged, summary comment lists the PRs)
  and dashboard#45 (management console — built in #54, both boxes done in
  code; stale). COMMENTED fm#1329: the quota charging an off-topic turn is BY
  DESIGN (the cap bounds compute; an exemption is a free channel) — keep; the
  state-machine/case-record half is a real engine defect that belongs with
  fm#1328, not here. dashboard#78 fix = **dashboard#126 (`6b9d0ef`), 14/14 green, APPROVED,
  you merge** (backend premise verified in code: role assignment rebuilds
  only the org axis, `platform_admin` is not even in the writable enum;
  operator badge now sits beside a live select; an explicit self-row lock
  was ADDED because the old operator lock had covered the caller's own row
  only by accident; tests mutation-checked). LEFT, not
  related: dashboard#35/#67 (pre-project features), dashboard#51 (still
  backend-blocked: `GET /cases` binds no `created_after`), fm#1206/#791/#640.
  fm#1252 stays yours (item 2).
- **D11 (yours): fm#1169 — narrow `OAUTH_REDIRECT_URI_PATTERNS` to the store
  id on the public host.** Its stated blocker was "retire sideloaded
  distribution"; checked today: the README, the website, and the store badge
  all point at the Chrome Web Store listing (live since 2026-08-24), and the
  only remaining unpacked instructions are the developer section of the
  copilot README. The GitHub release still attaches zips. I am preparing the
  infra PR (onprem overlay only, as Deployment env so it rolls on apply;
  gate extended so a family pattern or an access list that does not admit
  the first-party redirect fails CI). **Merging it locks out every sideloaded
  copilot pointed at api.faultmaven.ai; store installs are unaffected.** My
  recommendation: merge it — it is the only change that closes copilot
  impersonation, and staging stays open for unpacked builds. Your call, since
  you know whether anyone still runs a sideloaded build against the public
  host. **PR: enterprise-infra#303 (`2d31022`), CI all green, APPROVED** —
  my own mutations on the rendered overlay: family pattern → gate FAILS;
  access value naming a different id → gate FAILS (first-party redirect not
  admitted); real value → PASS. Merging deploys via CD (Deployment env ⇒
  pods roll; the image pin is unchanged by this PR). Close fm#1169 by hand
  after the merge.

- ‼ **2026-09-05 LIVE ORDERING DEFECT (mine): the Dashboard auto-deploys to
  app.faultmaven.ai on merge, but the API behind api.faultmaven.ai is pinned
  by image tag and did not move.** Verified: the live Dashboard bundle carries
  the panel and calls `/api/v1/meta/capabilities`; api.faultmaven.ai answers
  404 there (200 on the bare path). Effect until the API deploys: the
  Dashboard's capabilities fetch errors, so the management console nav item
  and route are hidden for cloud admins, and the embedded panel runs on its
  fabricated self-hosted fallback (team sharing off, dashboard URL localhost).
  Sign-in, cases, and turns are unaffected. My earlier note only ordered core
  #1327 before the CWS upload; it also had to precede the Dashboard merge.
  **Where the live API actually comes from** (checked on the cluster):
  namespace `faultmaven` runs the CORE image `ghcr.io/faultmaven/faultmaven:
  sha-080b358`, pinned by `newTag` in enterprise-infra
  `kubernetes/apps/faultmaven/overlays/onprem/kustomization.yaml`. The
  faultmaven-cloud image is wired but unused, so cloud PR #34 (pin bump to
  bf41e0fda, green) is routine pin currency, NOT the fix.
  **THE FIX — yours, it is a production deploy:** the queue already names the
  image (`available-images/staging/faultmaven.yaml` → `sha-bf41e0f`,
  commit_sha matches core main; the tag exists in GHCR). Run
  `gh workflow run promote-images.yaml -R FaultMaven/faultmaven-enterprise-infra
  -f services=faultmaven`, merge the PR it opens, and let the CD pipeline run
  (migration Job FIRST, then rollout, /health smoke, auto-rollback). ⛔ Do
  NOT dispatch CD with `skip_migration`: the new turn-cap resolver FAILS
  CLOSED — without migrations 051/053 every tenant is capped at 30
  turns/day. What sha-bf41e0f arms beyond 080b358 (9 commits): migrations
  050–053 (050 rewrites `knowledge_items` metadata, KB-proportional, in one
  transaction; 052 builds partial unique indexes without CONCURRENTLY on the
  small `enterprises`/`organizations` tables; nothing touches `cases`),
  personal tenants dormant (`SSO_JIT_PERSONAL_TENANT_ENABLED` default off),
  company tenants uncapped with no override, operator user-admin confined to
  the caller's tenant (#1322: an org-less platform admin now gets 403 on
  `/admin/users*`), the refresh grant refuses a soft-deleted org, and the
  capabilities alias. Verify after: `curl -s -o /dev/null -w '%{http_code}'
  https://api.faultmaven.ai/api/v1/meta/capabilities` → 200, then the
  management console is back on app.faultmaven.ai.
- **2026-09-05: dashboard#124 MERGED (`1b6bcaa`) and copilot#250 MERGED
  (`599362f`). The built-in panel programme is code complete.** A one-line
  Dashboard pin bump to `599362f` follows (adopts #250's purge fix as defence
  in depth); it changes no contract. What is left is yours: the pre-flip list
  in item 2, the cloud click-through, and deploying core #1327 before the next
  store upload. ‼ **The item-1 ruleset POST has NOT been run** (verified
  2026-09-05 via `gh api /repos/.../rules/branches/main`: no
  `required_status_checks` rule from any ruleset, classic protection empty).
  #124 merged with the pin gate green but unenforced. Run it before merging
  the pin-bump PR so that PR proves the gate is live.
- **dashboard#125 OPEN (`e78ebfd`): the pin bump to copilot `599362f`.**
  Verified against main: one `package.json` line and five lockfile SHA lines,
  nothing else; pin gate, lint, typecheck, 734 tests, build, style and
  boundary gates all green; contract pin untouched (2.5.0). APPROVED.
  **MERGED 2026-09-05 (`8b2c304`).** Every PR in the programme is now on
  main. ‼ Re-checked after this merge: the item-1 ruleset is STILL not
  installed — main has no required status checks. It is the one open
  programme item on the code side; run the POST.

- **2026-09-05: copilot#247/#248/#249 and fm#1327 all MERGED.** New: copilot#250
  (purge survives a late host-store clear; approved, green) — merge it.
  Remaining:
  the Dashboard's final commit (pin → #249's merge) → smoke 5 → I send the
  final ruleset command → you merge dashboard#124. Then: deploy core (the
  capabilities alias) BEFORE the next store upload. fm#1327 (`/api/v1/meta/capabilities` canonical + deprecated alias;
  contract 2.6.0; approved, green) — merge and DEPLOY before the next store upload.
- Incidental (fm#1327 found, not fixed): the first request through a cold
  rate limiter publishes no `x-ratelimit-*` headers, indistinguishable from a
  failed-open check. Small; fold into the next core PR touching the limiter.
- ⚠ **Store release ordering:** the next Chrome Web Store upload (the one
  carrying the package relocation and #249) must follow the core deploy of
  the capabilities alias; the deprecated old path keeps installed extensions
  working until then.
- **Test hygiene owed #2 (found by fm#1325's CI failure):** three integration
  fixtures assign `os.environ["DATABASE_URL"]` to an in-memory SQLite URL
  without `monkeypatch`, permanently repointing the process; any later test
  that rebuilds the settings singleton boots against an empty database. Small
  own-branch fix after fm#1325 merges.
- **fm#1325 (config status + CLI test isolation) — contract change, premise
  CORRECTED.** The frontends adopt the API contract by PIN (`api-contract.pin.json`),
  not by watching core main, so merging #1325 reddens nothing and there is no
  merge-ordering constraint. Two adoption PRs (`chore: adopt API contract
  2.5.0`, 2.0.0 → 2.5.0, additive) open after #1325 merges, pinned to its
  merge commit; merge them whenever convenient.
- **2026-09-04 merge queue:** ✅ fm#1324 merged · copilot#235 (shared UI PR 3; approved, green) · fm#1325 (config status, approved, green); the two contract-adoption PRs follow at leisure (no ordering constraint); copilot PR 4 opens after #235 merges. Prepared behind them: copilot
  PRs 4 and 5, PR 6 in preparation.
- **Test hygiene owed (pre-existing on main since #1323):** #1323's CLI unit
  tests are order-dependent when run in one process with `-m postgres` modules
  and `DATABASE_URL` set (18 spurious failures; pass in isolation; no CI lane
  hits it). Fold a fixture-isolation fix into the next core PR.
- **Core merge order now (2026-09-04):** fm#1323 (retire tool, migration 052)
  → then I rebase fm#1324 (cap → migration 053) → you merge fm#1324. Both are
  reviewed and approved; both had one fix round and one delta pass.
- **Cap PR fm#1324 review round (2026-09-04):** eight passes; two defects were
  my own dispatched invariants. The rate-limiter refund I required is deleted
  (it lifted the per-session bound on a capped client and filled the shared
  per-IP window); the reservation moves inside the service after validation
  so refusals charge nothing; single-tenant never touches the ledger; policy
  through the auth/organization ports; `x-error-code` exposed cross-origin for
  the built-in panel. Fix round in flight.
- **D8 design change I made (FYI, 2026-09-04):** the retire tool's first
  cut worked around `users.enterprise_id NOT NULL` with a JSON marker in
  `enterprises.settings`, a slug rename and a second anchor-mover in the login
  path. The review found four serious defects rooted in that (an unscoped
  login could move a company-anchored account back to personal; a re-run
  could delete the live successor's WorkOS org; ambiguous lookup once a
  subject has two retired tenants; a live refresh chain keeps minting for a
  retired tenant). Decision: make `users.enterprise_id` nullable by migration
  (reverse of 006) and express retirement in typed columns; one anchor-mover;
  IdP teardown by recorded id; retire revokes tokens and `/refresh` refuses
  for a soft-deleted org. Fix round in flight on fm#1323.
- **D6 consequence to decide later:** under multi-tenant, a user with no
  organization membership row (org-less identities predating #1045, or one
  whose membership was removed) is now administerable by nobody through the
  operator routes, and no CLI deletes a single user. Refused rather than
  invented; needs an operator path when the account tooling is next touched.
- **Duplicate D7 commits:** both fm#1322 and the cap branch fix the debug
  route; D7 lands in fm#1322, the cap branch drops its copy at rebase.
- **Cap PR deploy-ordering check (done, no action):** the cap's ledger table is
  on the hot path and fails closed, so an image ahead of its migration would
  refuse every turn. Verified: the CD pipeline runs the migration Job with the
  same image tag and `kubectl wait`s for completion (300s, deploy fails
  otherwise) BEFORE `kubectl apply -k` and the rollout; standalone runs
  `alembic upgrade head` itself at startup. Covered in both models.
- **Cap PR flips one #1319 probe assertion** (debug causal-graph route now 404
  to a stranger instead of 200). Whichever merges second updates it; I handle
  it at open time.
- Scratch Postgres for the core lanes is still up (`docker rm -f fm-pg-1317`
  when the cap PR is done; it reuses it).
- **Merge queue for you (all reviewed, all green), in this order:**
  dashboard#122 → dashboard#121 (re-run after) · copilot#233 · website#43 ·
  copilot#231 (inert until dashboard#120 deploys) · copilot#232 (PR 1 of 8) ·
  fm#1319 (two-tenant surface probe, 51 cases) · fm#1320 (JIT personal tenant,
  switch OFF by default; ALL lanes green on `173a1c98a`) · fm#1322 (operator
  user-admin confinement, D6+D7; merge AFTER #1319, I rebase it) ·
  doc-internal#43 (ADR-016).
- **Before the JIT switch is ever flipped:** D1 live assertion · D2 cap PR ·
  D9 WorkOS checks · D8 retire tool · surface both settings in
  `GET /admin/config/status`.
- **Dependency advisories (found by the programme's first CI runs).** Two
  browserslist highs published 2026-09-01 red-line the dashboard audit;
  fixes: dashboard#122 (merge BEFORE dashboard#121), copilot#233 (artifact
  digest unchanged, no store upload), website#43 (also clearing the
  `@humanfs/node` moderate from 2026-09-02, which its stricter gate catches).
- ⚠ The copilot repo's audit job is FAIL-OPEN (`continue-on-error: true` and
  `|| echo`): 5 high advisories remain there and nothing turns red. Making it
  fail-closed is a one-line workflow change that would go red today; your
  call whether to take that on now or after the eight-PR sequence.
- Wave 1 dispatched 2026-09-03: dashboard#119 (store URL), copilot#229 (yield),
  fm#1317 (surface probe), copilot#230 (spike). Each lands as a PR for one
  review round, one fix commit, then your merge.
- The primary checkouts of `faultmaven-dashboard` and `faultmaven-doc-internal`
  are on other branches with local changes; all programme work runs in
  `*-wt-<issue>` worktrees and does not touch them.
