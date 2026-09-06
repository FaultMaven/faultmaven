# Next-phase plan — campaigns to conclusion (2026-07-26; ground truth refreshed 2026-07-29)

Supersedes `REVIEW-audit-plan-refinement.md` (its queue — M0 → #769 → #689 →
causal debt — is fully executed). Status anchors below are issue/PR numbers:
**query `gh` for truth; never trust a status line older than the moment you act.**

## ⬅ START HERE: what do I work on next?

**The ordered work queue is the gap-ledger comment on fm#629.** The next issue to
resolve is the **first unchecked box** in it, top to bottom:

```bash
gh issue view 629 --repo FaultMaven/faultmaven --comments   # the ledger is the last comment
```

This plan describes *phases*; the ledger enumerates *gaps* and orders them. When
they disagree, the ledger wins — it is refreshed on every finding.

**Exit criterion (owner, 2026-07-29):** close all gaps required to complete the
three projects — causal chain, multi-tenancy, runbook remediation. Not a reduced
"minimum set that permits a flip"; the objective is completing the projects.

**Where the three projects actually stand** (cross-referenced 2026-07-29 against
the 55 open issues):

| Project | Open gaps |
|---|---|
| Runbook lifecycle hardening | **0** — complete |
| Causal chain | **1** — #673 only, ratified by the dual-authoring design review as the design's own recorded endpoint, deliberately gated on chain-grounding; not a defect |
| KB remediation | **2** — #710, #723 |
| Multi-tenancy (#629) | the only live front — enumerated in the ledger |

51 of the 55 open issues belong to **none** of the three projects; they are the
Phase C tech-debt backlog and are not campaign scope.

**Standing rule (owner, 2026-07-29):** every gap found is either resolved or
added to the ledger *in the same action that files the issue*. A finding that is
filed but not queued is how a bounded campaign starts looking infinite.

## Ground truth this plan is built on (verified 2026-07-28, all via `gh`)

- **Lane C is CONCLUDED as a lane** (see below): soundness cluster, KB
  permission holes, and activation prep all closed, with exactly two residuals —
  #673 (gated on data, not workable; see Lane C item 1) and #857 (small index).
- **Causal-chain campaign**: audit gaps closed (#838 merged `cdc1043f`).
- **Runbook-remediation**: pipeline complete; seeder shipped (#712), corpus
  guarded (#810/#811/#817).
- **Multi-tenancy mechanics COMPLETE** per #629's own checklist: RLS (25+ tables),
  request org-binding, jobs/CLI tenant scopes + audited maintenance role,
  global-tier KB (#770), coherence gates. D9 operator access DONE: #812/#813/
  #814/#815 all merged, epic #816 closed. Team layer U1–U16 built across 6 repos
  (latest: #850 sentinel refusal, #851 per-turn authorship).
- **Identity lane shipped**: SSO AuthKit + JIT + audit trail (PRs #765/#773/#774),
  D10 interim credential (#822 + slack-agent#35).
- **The one hard blocker is CLOSED — MERGED 2026-07-28**: WorkOS Org→FM-org
  mapping = #869 → **PR #870 merged to main `f52490de`** (#869 closed; full
  dispatch chain: Opus impl → Fable review, 2 findings fixed → adversarial
  sign-off → owner merge; CI green on all 13 checks). Mechanics: non-RLS
  `sso_org_mappings` (migration 038, head `b0c1d2e3f4a5`), fail-closed
  `sso_org_unmapped`, JIT lands in the mapped org (member role + org's
  enterprise), org claim rides access+refresh tokens (fixes the #850
  refresh-trap), provisioning via `scripts/auth/provision_sso_org.py` +
  runbook `docs/operations/sso-org-provisioning.md` + design doc
  `docs/architecture/security/sso-org-mapping.md`. Follow-ups: #872 (copilot
  OAuth-PKCE org propagation — copilot fails closed under multi until done),
  #873 (Slack D10 credential org claim — **prerequisite for the rehearsal's
  staged D10 swap leg**), #874 (membership-removal→revocation wiring),
  doc-internal#34 (ADR-015 amendment note). **Phase B is UNGATED — step 1
  (#629 flip rehearsal on staging) is THE next critical-path item.**
- **Infra tier hardened for activation** (2026-07-28): infra#131 CLOSED — all
  app CronJobs run clean (4 layers: #134 OOM, #137 envFrom, #138 dead
  knowledge-indexing retired + ChromaDB netpol → case-cleanup, #139 self-clearing
  CronJob alerts; stock KubeJobFailed was firing-but-wallpaper, now null-routed
  for the namespace). Storage-cleanup canary: 48h clean-scheduled-run window ends
  ~2026-07-30 03:00 UTC → then infra#132 (enable real orphan deletion).
  Gotchas that will bite Phase B if forgotten: cd-pipeline has NO --prune and
  never applies kubernetes/platform/; out-of-process kb_seed matches neither
  ChromaDB netpol label under multi (noted on infra#138).

## Phase A — parallel now

### Lane I (identity agent) — the flip's critical path ⬅ THE open front
1. ~~**WorkOS Org→FM-org SSO mapping**~~ **MERGED 2026-07-28** (#869/PR #870,
   main `f52490de`). See the ground-truth bullet for mechanics + follow-ups
   (#872/#873/#874/doc-internal#34).
2. The auth leftovers, worst first: **#830** (unauthenticated revoke writes
   attacker-TTL keys), #828/#829/#831/#832, #679, #706. (#519 closed.)
   ⬅ Lane-I queue head; can run parallel to Phase B in a separate session.

### Lane C (campaign agent) — CONCLUDED 2026-07-28; two residuals
1. ~~Causal-chain soundness cluster~~ DONE: #722/#721/#787 (PR #860),
   #843/#840 (PR #859), #514/#521 (PR #862) — all closed.
   - **#673 residual — parked on a dated gate, do not "work" it**: Stage A
     (chain-authored conclusion precedence) merged #861 + deployed. Stage B
     (retire dual-authoring) gated on INV-41: backstop reliance <2% over a
     rolling 30d at the provider floor. Reading 2026-07-27: ~83% reliance,
     ZERO chain-licensed resolutions ever, and a nonzero `none` leg (possible
     readiness bypass — investigate that independently). Earliest evaluation
     ~2026-08-26; if `chain` stays at zero the work is chain-building
     elicitation, not retirement mechanics.
2. ~~KB authoring permission holes~~ DONE: #834/#785/#854 closed (PR #863).
3. ~~Activation prep independent of SSO~~ DONE: infra#127 closed (WARN→FATAL
   live, #865), dashboard#58 closed, #855 closed (PR #864).
   - **#857 residual**: `case_messages.author_id` index — small; do before any
     per-author query ships (fits either lane as a rider).

## Phase B — activation (gate LIFTED 2026-07-28 — #870 merged) ⬅ NEXT: step 2 prep, gated on the ledger

**Step 1 (flip rehearsal) is COMPLETE as of 2026-07-29.** All eight matrix items
pass on `sha-52c2cc5`; results on fm#629 (`issuecomment-5111695463`,
`issuecomment-5111920937`). The rehearsal was a discovery instrument and has
finished discovering — it produced fm#901 and infra#149/#150/#151, all queued in
the ledger. Items 3 and 4 need one re-run once the ledger's sections A and E
close; no further rehearsal rounds are planned.

**Slack-continuity invariant (owner-confirmed 2026-07-26; SIMPLIFIED
2026-08-12).** The live agent authenticates via dev-login, which
`require_local_mode` 404s the moment `AUTH_MODE != local` — and `/auth/refresh`
is mode-aware (HS256 local / RS256 oauth), so its replacement credential only
works once the server is in oauth mode. That coupling is unchanged and is
precisely why the cutover is ONE event.

⚠️ **What changed 2026-08-12 (owner):** the requirement to keep the live agent
on dev-login is **withdrawn**, so the staged interim and #819's five-criterion
hard stop are retired. The auth cutover is no longer "deliberately delayed"
behind the tenancy work — it is an independently schedulable event, gated only
on WorkOS provisioning and the admin pre-link. Full derivation: fm#819 body +
`issuecomment-5263664301`.

Tenancy remains separate: #850 refuses the sentinel org under multi, so the
org-less `slack-agent` account fails closed the moment `TENANT_PROVIDER=multi`
even with auth untouched — that is a prerequisite of step 4, not of step 3.
The steps below are each a planned, verified event:

1. ~~Org-mapping merged~~ ✓ → **#629 flip rehearsal on staging** — **IN PROGRESS
   2026-07-28** (fm#629 + fm#873 claimed; detailed plan:
   `WIP-629-flip-rehearsal-plan.md`): `multi` + oauth
   on a staging namespace; verify RLS bite, SSO login into a mapped org,
   global-KB read, jobs refusal matrix — and a staged D10 credential swap
   end-to-end. Progress 2026-07-28: **#873 → PR #875** (both legs: mint-time
   stamp + oauth-refresh-grant re-attachment, which was silently dropping the
   claim) CI-green + signed off; **infra PR #140** = the full rehearsal buildout
   (disposable `faultmaven-rehearsal` ns overlay + runbook
   `docs/operations/flip-rehearsal.md`; owner approved the shape; NOTE
   `overlays/staging` is reserved for future Cloud — untouched) reviewed, two
   findings fixed (`app.current_org_id` GUC; fail-fast migration Job), signed
   off. Blocking: owner merges #875 + infra#140; re-issued WorkOS Staging API
   key (first paste truncated; only matrix item 3 needs it). Then: pin
   post-#875 sha and execute the runbook, negative test first. Session prep
   notes (2026-07-28):
   - **#873 first (small, rehearsal-blocking):** `provision_service_account.py`
     mints the D10 credential with no org claim; after #869, an org-less refresh
     credential fails closed under multi — the staged-swap leg needs the script
     to stamp `--organization-id`.
   - Infra side: staging namespace needs `TENANT_PROVIDER=multi` +
     `AUTH_MODE=oauth` + all three `WORKOS_*` set ATOMICALLY (coherence gate
     hard-fails); WorkOS org ids are PER-ENVIRONMENT (staging id ≠ prod id —
     runbook step 1); then run `scripts/auth/provision_sso_org.py` (owner DSN).
   - Known gotchas in scope: out-of-process `kb_seed` matches neither ChromaDB
     netpol label under multi (noted on infra#138) — the global-KB-read check
     will surface this; copilot fails closed under multi until #872 (out of
     rehearsal scope, don't chase); cloud admin image is registered-not-deployed
     (membership mgmt UI not needed for the rehearsal — JIT + script suffice).
2. **Slack org-binding + org-move migration** (#819/slack#25): per the
   W-phase plan posted on slack#25 (2026-07-26) — the migration CONSUMES the
   W1 per-workspace provisioning primitive (W1/W2 are Phase-A-buildable now;
   slack#25's "blocked on P2" status is stale — `set_current_org_id` is live).
   Historical-case attribution comes from the agent's thread→case store
   `team_id` mapping; unattributable cases go to a holding org, never deleted
   (`user_id` is `ON DELETE SET NULL`; #850 fail-closes sentinel accounts
   under multi).
3. **Coordinated live WorkOS auth cutover + D10 switch** — one event, and no
   longer gated behind step 2. ✅ hard stop lifted 2026-08-12. One atomic
   commit sets `AUTH_MODE=oauth` + `OAUTH_ENABLED=true` +
   `DEPLOYMENT_MODE=cloud` + the three `WORKOS_*` + a real RS256 key mount;
   then mint the agent credential in-pod (`fm-provision-service-account -u
   slack-agent`, **no** `--organization-id` under single-tenant) and redeploy
   the agent. ⚠️ The credential **cannot be pre-minted** (the CLI refuses
   unless already in oauth mode), so Slack is down from flip to redeploy.
   ⚠️ `DEPLOYMENT_MODE=cloud` is load-bearing: under `standalone` the
   coherence gate only warns, missing RSA keys are auto-generated per restart,
   and a missing WorkOS config leaves the box healthy with no login path.
   ⚠️ The `admin` account must be **pre-linked** to its WorkOS subject first —
   SSO JIT refuses to link by email and fails the login instead. Rollback:
   revert the commit **and delete `credentials.db`** from the agent PVC.
   Full derivation: fm#819 + `issuecomment-5263664301`.
4. **The flip itself** (#629 close): production `TENANT_PROVIDER=multi`.
   ⚠️ **2026-08-12: steps 3 and 4 are candidates to MERGE into one event.**
   Owner confirmed the existing data is test data to be wiped (no backcompat,
   no preservation), which deletes step 4's largest risk — the org-move
   migration. Two findings then argue for bundling: the default-admin
   bootstrap is gated **only on tenancy**, so cloud+oauth left at
   single-tenant recreates an undeletable, unusable `platform_admin` row every
   boot; and `fm-provision-service-account` **refuses** `--organization-id`
   under single while **requiring** it under multi, so single-tenant
   provisioning is guaranteed rework. Ledger support: #706/#959/#958/#831 are
   closed and #874's PR #1041 is green, so #819 is the last open ledger item.
   ⚠️ The wipe must be **drop + recreate**, never `DELETE`/`TRUNCATE` —
   migration 029's RBAC seed will not re-run and every SSO login then fails
   closed. Decision pending on the campaign board's owner queue; full
   derivation on fm#819 `issuecomment-5263853329`.
5. Post-flip: dashboard#45 (U13d console), dashboard#67/#68 (break-glass
   follow-ups), slack#25 per-workspace fan-out.

## Phase C — after campaigns conclude

Tech-debt queue (~45 issues), triaged in clusters: LLM-error plumbing
(#509/#510/#548/#552), context/token allocator (#610/#611/#613/#614), test
infra (#823/#856/#511), storage residue (#835/#836), misc. Plus the two
deliberately-parked design follow-ons (rung-indicators-as-needs; #349 Rule-2
eval signal).

## Standing rules
- Campaign gaps before tech debt (owner directive, 2026-07-25).
- Claim work by **assigning the GitHub issue** before starting; memory carries
  ownership + rationale only, never status.
- New issues from reviews keep arriving (#845–#857 this week): classify each as
  campaign-essential vs deferred at filing time, don't re-litigate the queue.
