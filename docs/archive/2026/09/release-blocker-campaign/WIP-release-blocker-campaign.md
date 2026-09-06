# Release-blocker campaign — full board (CHECKPOINT 2026-08-29, batch 2 MERGED, batch 3 ready)

**BATCH 2 IS MERGED.** #1236/#1237/#1238/#1239/#1240 are on `main` (`3b262d889`).
Reconciled after merge: the merge window closed **exactly** those five lane issues
(#1222, #1226, #1228, #1229, #1235) and nothing collateral; both held-backs stayed open.
Combined-HEAD verified beyond per-PR CI (each PR was green against the OLD base, none
against the others): full suite **11475 passed / 1 failed**, that one failure reproduced
at the batch base `d8b8378a3` so it is pre-existing; all five lanes' suites together 568
passed; adversarial prompt-fence corpus 32/32 inert on merged main.

**Three classes closed by batch 2:** path containment (every subsystem), upload novelty
(every path), prompt fence (caller-controlled blocks — MAIN PROMPT ONLY; the fallback is
open and tracked as #1242).

## ⬅ BATCH 3 (2026-08-29) — #1249 MERGED, #1248 READY

| PR | Issue(s) | State |
|----|----------|-------|
| #1249 | fm#1227 | **MERGED** `b5163d0bb` 10:57Z — #1227 closed |
| #1248 | fm#1230 + fm#1243 | **READY** — synced head `404af6ccc`, **12/12 green**, mergeable=true, `mergeable_state=blocked` is the REVIEW gate only (no branch protection, zero reviews) |

`main` also took #1247 (`441dacaee`) and #1250 (`859a8d47c`, a regression fix for #1247) from another workstream. Current main: `859a8d47c`.

**The #1248 red was never a defect: the branch was un-synced and someone re-ran the
workflow instead.** With `mergeable=false` GitHub cannot build `refs/pull/N/merge`, so
CI tested the head in isolation — a tree without 045 — and reproduced
`KeyError: 'e9f0a1b2c3d4'` forever. **A sync fixed it on the first run, no re-run.**
Two conflicts, both predicted: `HEAD_REVISION` (ours, 046 is chain head) and the
`suggestion_service.py` import block (union; both symbols verified USED, not assumed).

### Decisions that outlive batch 3
- **fm#1227 eviction REMOVED.** `knowledge_items` has no back-pointer, so an approved
  suggestion's `knowledge_item_id` is the only case→runbook link. Cap now counts
  UNREVIEWED rows per org and refuses. **Consequence: the table grows monotonically.**
- **fm#1227 concurrency:** `version` column + conditional `UPDATE ... WHERE version = :loaded`.
  Making the store shared CREATED the class — #1214's in-process guard was sound only
  while one worker owned the dict. Publish-then-claim chosen deliberately.
- **fm#1230 constraint:** UNIQUE `(organization_id, runbook_id)` WHERE `status <> 'discarded'`.
  Tenant-qualified partly for CONFIDENTIALITY — a unique index is enforced BELOW RLS, so a
  key omitting the tenant is a cross-tenant existence oracle over titles. Not scope/owner
  qualified because neither column is on the draft row.
- **Verified safety property:** 0 of 91 shipped pack ids match `runbook_id_from_parts`
  (on-disk ids are hand-curated short slugs), so re-minting cannot touch the shipped KB.
- **Migration verification standard raised:** a round trip must assert a REJECTED row plus
  an accepted control, not count columns. Column counts pass while constraints are silently
  dropped.

## ⬅ BATCH 4 — IN FLIGHT

| Lane | Issue | Notes |
|------|-------|-------|
| dispatched | **#1241** | `parse_cause_subfields` reads labels inside HTML comments → an empty required field passes the quality gate #1214 made load-bearing. GATE BYPASS. Must audit the 91-runbook pack before landing (something may rely on the bug). |
| dispatched | **#1242** | Fallback templates unfenced, no fence rule; attacker-planted `FENCE:` line can be the ONLY one. Reachable via budget pressure. Finishes the class #1240 closed on the main prompt only. |

Both off `859a8d47c`, disjoint trees (`modules/knowledge/` vs `core/investigation/prompts/`).

### BETA GATES — now anchored (were not)
- **#1251** prove end-to-end sign-in through the PUBLISHED store build. #1066 closed having
  left this unrun: "only a sign-in proves the skip fires". Owner-run. Run BEFORE #1169, which
  changes what the test exercises.
- **#1252** assert two-tenant isolation on the live deployment. 4 orgs exist; nothing records
  A cannot see B. Owner-run (an agent binding a tenant is blocked by the permission
  classifier). Seed identifiable data in B first, or a negative result proves nothing.
  ChromaDB is a separate surface — see #1168.

### OWNER DECISION RECORDED 2026-08-29: infra#266 is NOT a Beta blocker
Stated by the owner. The campaign board previously made #266's severity conditional
on the Slack scope call; that condition is now resolved — it is hygiene, not a gate.
Volume encryption on the Postgres primary remains an open SECURITY decision
(an un-escrowed key converts exposure into permanent data loss) but it does not gate Beta.
infra PR #269 merged earlier in the campaign; by design it reduces exposure by zero
(it pins today's wide export so a *widening* fails).

### The Beta gates, after that descope — five, in three different shapes
**Decisions (no engineering exists until made):**
- `slack-agent#25` — Slack scope for Beta: ship per-workspace service accounts, or exclude Slack.
- `fm#1016` — data-deletion scope. The promise is already published on www.faultmaven.ai/privacy/slack
  and Beta takes real incident data.
- `fm#1169` — narrow `OAUTH_REDIRECT_URI_PATTERNS`. A scope question (is sideloaded
  distribution still supported?), one line of code once answered. Run AFTER #1251.

**Verifications only the owner can run (live box; an agent binding a tenant is blocked
by the permission classifier):**
- `fm#1251` — sign-in through the PUBLISHED store build.
- `fm#1252` — two-tenant isolation, A cannot see B.

**Operational, needs root, blocks nothing:**
- `sudo rm -rf /home/swhouse/pgdata-1227` (owner `pcp:swhouse`, mode 700).

### ⛔ UNFINISHED ROOT WORK — these are NOT optional follow-ups

**The rule (owner, 2026-08-29): fix the issue at its ROOT, however many rounds it
takes. File only what is genuinely UNRELATED to the target.** The PM lane broke this
repeatedly — it treated each lane's issue statement as the scope boundary and
ticketed the remainder. Two of those tickets (#1241, #1242) came back and were fixed
anyway, so the filing bought nothing but a round trip. Do not repeat it.

Group these by ROOT, not by issue number. Three lanes:

**Root A — the #1222 clarification-recovery path**
- `#1245` choices are lost when the user replies with something else (same recovery
  path, different trigger). Needs a BOUNDED EXPIRY: without one, unresolved
  questions accumulate into the resolver's choice list and worsen the tier-2
  wrong-file exposure #1236 assessed and accepted.
- `#1244` the design doc describing that same emitter, which #1236 made staler.
  Part of the fix, not a follow-up.

**Root B — how caller text is protected in prompts (#1228/#1242's root)**
- `#1256` `sanitize_user_input` ESCAPES `<`/`>` on the main path while #1254 now
  FENCES the same channel on the fallback — two paths disagreeing about one channel,
  and escaping corrupts what the user wrote (`a < b` → `a &lt; b`).
  **Ordering is load-bearing:** fence `<conversation_history>` FIRST, then drop the
  escape. The escape is currently the only guard there — #1228 scoped that block out
  *because* the escape existed.

**Root C — runbook_id collision handling (#1230's root)**
- `#1258` two drafts in ONE job minting the same id raise a bare `IntegrityError`.
  Measured: `('redis','Redis OOM')` and `('redis','redis oom')` both mint
  `redis-redis-oom`. The cross-job case is closed; this one is not. #1230's own
  guard comment defers it ("belongs where the duplicate is produced").

### Genuinely unrelated — correctly filed, ordinary backlog
`#1246` CLAUDE.md counts + migration narration drift ·
`#1251` / `#1252` Beta gates (owner-run) ·
`FaultMaven/faultmaven-kb-toolkit#29` cross-repo grammar mirror — **load-bearing**:
it is the tracked terminator for the portability constraint #1255 shipped, and the
`_REGEX_SYMBOLS` allowlist is the lever that makes upstream CI refuse to pass until
the mirror lands.

### BOX STATE — needs the owner
- `/home/swhouse/pgdata-1227` **still present** (owner `pcp:swhouse`, mode 700; the lane
  reported it removed — only the container was). Needs `sudo rm -rf`.
- `asyncpg==0.31.0` deliberately left in the venv — declared in `requirements/cloud.txt` but
  was ABSENT, which is why PG-marked suites had never been runnable here.
- ~30 stale lane worktrees in `git worktree list`.

## What changed since the 2026-08-17 checkpoint

**31 faultmaven issues closed.** The whole resolution-summary and KB-cause-seeder
arc (#1091/#1096/#1097/#1098/#1103/#1107/#1108/#1136/#1137/#1143/#1144), the
auth/identity cluster (#1042/#1043/#1120/#1127/#1128/#1129/#1161), #1048, #1066,
#1079, #1150, and #1173 (ChromaDB auth fail-open, both halves).

### Four premises on the last board were wrong. All four were measured, not argued.

1. **`fm#1170` is not an engine regression.** All 13 historical
   `jwt-token-decode` "requests" fired on the phrase `projected token` — which
   *this scenario's own evidence files* hand the model. The bisect closed to two
   commits, both exonerated by direct measurement. Corrected coverage gives
   **identical distributions across the two eras**; the regression disappears.
   Now `fm-sre-simulator#47`, fixed in sim PR #48.
2. **`fm#1122` was three defects wearing one label.** Run 2 is a real engine
   defect (§7.1 restatement guard, below). Run 1 is `fm-sre-simulator#45` — the
   persona structurally cannot take the exit the server offers, which makes
   `UNRESOLVED@15` uninformative on 14 of 75 scenarios. A third defect nobody had
   filed became `#1195`.
3. **`infra#266`'s central claim is false.** `credentials.db` is a single-row
   table holding the agent's *own* FaultMaven refresh credential. The Slack
   workspace bot tokens are in **Postgres** — whose volume is in the `postgresql`
   backup group, 6-hourly, retain 28, same unencrypted NFS store, since long
   before #263. The exposure is real and **wider** than filed; the named file is
   the wrong one.
4. **The "1 organization" tenancy measurement was RLS-scoped.** `sso_org_mappings`
   (not RLS-tenanted) holds **4 distinct `organization_id`s provisioned 08-13/08-14**
   — before that checkpoint — and 5 users. The org/member/case counts read 0
   because the app role has no `bypassrls` and no tenant was bound. Re-measure
   before quoting.

### Engine quality: one confirmed defect, not five runs' worth

`#1122` run 2 mechanism, verified by A/B replay on current main: the root had 3
independent qualifying causal supports against a bar of 2 and `would_validate`
was **True**; it is pinned to `INCONCLUSIVE` by `root_restates_case_frame`
scoring it **1/9 = 0.111 novel** against `ROOT_NOVELTY_MIN_FRACTION = 0.3`. The
2026-08-24 "clean" run is **un-triggered, not fixed** — #1140 descoped the
auto-release explicitly. **No threshold fix exists**: no value releases the
incident while keeping #656 blocked (0.12 holds, 0.11 releases both). Do not
re-attempt one.

---

## ⬅ CURRENT PHASE — opening Beta

Beta is **invite-only, one WorkOS Organization per participant**. Onboarding is
manual by design; there is no self-service path.

### Conditions to clear before handing anyone a sign-in link

1. **⛔ Slack scope — `slack-agent#25`, unbuilt.** One global service account
   bound to one organization, 6 workspaces installed. Either exclude Slack from
   Beta or ship #25. Scope call first, engineering second. **This decision also
   sets whether `infra#266` is a blocker or hygiene.**
2. **⛔ End-to-end sign-in through the published store build — no issue anchors
   this.** `#1066` closed 2026-08-24 having verified the pin is live in the
   ConfigMap and explicitly leaving this unrun: *"`/admin/config/status` proves
   the value is pinned; only a sign-in proves the skip fires."* Install from the
   store, sign in. If a consent prompt appears, reopen #1066.
3. **⛔ Two-tenant isolation on the live box — no issue anchors this.** Four orgs
   exist (above), but nothing records the actual assertion that tenant A cannot
   see tenant B, and the environment that once proved RLS bite
   (`faultmaven-rehearsal`) was destroyed. Owner-run; a session's attempt to bind
   a tenant for this is correctly blocked by the permission classifier.
4. **`fm#1016` — the data-deletion scope decision is live, not hypothetical.**
   `www.faultmaven.ai/privacy/slack` already publishes the promise and Beta takes
   real incident data.
5. **`infra#266` — Slack workspace OAuth tokens in retained unencrypted backups.**
   PR #269 is open and **reduces exposure by zero** by design: it pins today's
   wide export as expected so a *widening* fails. The control failure — a
   secret-bearing store offered to the whole `192.168.0.0/24`, with
   `no_root_squash` making the `0700 root:root` mitigation inert — needs the
   operator step. Volume encryption on the **Postgres primary** is the owner call;
   an un-escrowed key converts exposure into permanent data loss.
6. **`fm#1169` — cheap to close now.** A hostile extension can still present
   `client_id=faultmaven-copilot` with its own redirect; the consent screen renders
   the client *name*, so it reads correctly for the impostor. The extension is now
   published with a known id. If sideloaded distribution no longer needs
   supporting, narrowing `OAUTH_REDIRECT_URI_PATTERNS` closes it in one line.

### Conditional, not blocking

`fm#1040` (org-scoped Permission mapping unwired) bites only when **an org holds
two users** — latent under one-org-per-participant, live the moment a participant
invites a colleague. Phase 0 is landing (#1189/#1190/#1191 merged); `#1163`
carries the rest. Its sibling `#1042` closed 08-23. `fm#1045` (individual signup)
stays deliberately out of scope for invite-only Beta.

---

## Campaign lanes — 2026-08-26/27 session

All QC'd by execution, not by report. `/code-review` at high effort on every code
PR, findings adjudicated against the real head before relaying.

| Lane | PR | State |
|---|---|---|
| `#1156` credential leak | #1196 + **#1205** | **MERGED** `13b04812e` |
| `#1074` runbook citations | **#1203** | **MERGED** `0bc5d0f2a` |
| `#1166` KB write scope | **#1197** | **MERGED** `ec7bb3bad` — bumps contract 2.0.1 → 2.1.0 |
| `#666` filename leak | **#1198** | **MERGED** `d424f046e` |
| `#1195` handoff contradiction | **#1199** | **MERGED** `68e6d2dbd` |
| `infra#266` | **infra#269** | **MERGED**; reduces exposure by zero *by design* |
| `sim#47` metric | **sim#48** | ⬅ **STILL OPEN** — CI green, mergeable, blocked only on the 1 required approving review. `gh` is the author and cannot self-approve. **Owner action.** |
| `#1122` / `#1170` | diagnosis only | Reported; #1122 release decision is the owner's |

### Post-merge verification (done, by content — squash merges, never by ancestry)

All five faultmaven PRs verified present on `main` by claim, not just by blob:
contract `2.1.0` in `contract_version.py`/`openapi.json`/`README`;
`is_minted_filename` and its call sites; `closed_restatement_held` +
`RESTATEMENT_HELD`; the #1156 guard. PR head SHAs all match `refs/pull/N/head`
— **no stranded pushes**.

⚠️ **`main` HEAD never got a CI test run.** `13b04812e` (#1205's merge) has only
the chained `Publish Docker Image`; the last `CI/CD Pipeline` ran on
`0bc5d0f2a` (main~1), which excludes #1205. Five merges inside 132 s. `CI/CD
Pipeline` has no `workflow_dispatch`, so only a push can re-trigger it.
**Run locally instead and verified green:** the CI standalone invocation
(`pytest tests/ -m "not cloud and not benchmark"` with the job's env) →
**11519 passed, 84 skipped, 6 failed, 1 error**, and all 7 are the documented
local-only `test_opik_fail_closed` set (`import opik` fails in this venv;
#1205 touched only `exception_handlers.py` + its test + a doc).

### ⚠️ Three trackers were wrong, in both directions — corrected this session

GitHub parsed **prose** in #1198's body as closing keywords. Two issues were
auto-closed by `d424f046e` that the PR did not fix, and both were **verified
still live on `main`** before reopening:

- **`#1207`** — the body reads *"**Fix #1207** via Option 1 (skip the append)"*,
  an instruction to a future fixer. `Fix #1207` is a closing keyword. The
  unconditional append is still at `milestone_engine.py:9303` and the dependency
  test still carries its `xfail(strict=True)`. **Reopened.**
- **`#694`** — the body reads *"Whoever **resolves #694** should know this rests
  on it"*. No `len(files)` guard exists anywhere in the package. **Reopened.**

And the reverse: **`#1195` stayed OPEN** though #1199 fixed it — that merge
commit carried no closing keyword at all. Fix verified on `main`; left for the
owner to close.

**Lesson for this campaign's PR bodies:** never write `Fix #N` / `resolves #N`
about an issue the PR does *not* close. Use "see #N" or "#N must be fixed
first". The dependency banner convention this campaign uses is exactly the shape
that trips it.

### ✅ The downstream drift premise was also wrong — the gates are GREEN

The last board said dashboard and copilot `api-types-drift` "go red when #1197
lands". **They do not.** Both jobs fetch a **pinned** ref from
`api-contract.pin.json`, not `main`, and the workflow comment says why:

> *The PINNED contract, not `main`. Fetching `main` meant the API repository
> publishing a change reached this client the moment it merged — turning this
> repository red with no commit here recording that anyone accepted it. Adoption
> is moving `ref` in api-contract.pin.json, and that PR is where this side says
> yes.*

Both repos pin `contractVersion 2.0.0` at ref `a879206c` (adopted 2026-08-22),
which that ref does serve. `main` now serves **2.1.0**.

So there is no red signal, and **no signal at all**: the clients are silently two
contract versions behind (2.0.0 → 2.0.1 → 2.1.0). Adoption is a deliberate PR in
each repo that moves `ref` + `contractVersion`. Nothing will remind anyone.

Note `dash#115` is **not** part of that: `closure_reason` is typed
`string | null`, not an enum, so #1199 changes no generated types. It is a
display gap in both frontends and has to be done on purpose.

### Filed from review fallout

- **`#1195`** — restatement-held roots reach the insufficient-evidence handoff:
  the engine asks the user for data it has already told the model cannot help.
- **`#1200`** — `approve_suggestion` passes `metadata=` to `upload_document`,
  which has no such parameter; `TypeError` into a broad `except`, so the approve
  route **silently creates nothing**. Pre-existing, live route.
- **`#1201`** — page captures reach the engine tagged `source_type="file_upload"`;
  provenance derived from filename shape while the same file's `_is_paste_target`
  checks the provenance tag first.
- **`#1204`** — `aws-iam-role-assumption-failure` fails `kb-validate --strict`:
  Cause G is **3360 chars against a 3000 ceiling** and line-splits mid-causal-chain,
  with 6 of 8 indicator entries missing their token. Cause G is the IRSA scenario's
  actual root cause. **Possibly the upstream cause `#1079` was closed without
  identifying.**

---

### Merge-phase lanes opened this session (all QC'd by execution)

| Issue | PR | State |
|---|---|---|
| `fm#1207` duplicate UploadedFile | **fm#1209** | Fix + round-2 review corrections pushed. `/code-review high` run and adjudicated |
| `fm#1200` approve_suggestion | **fm#1211** | Fix + round-2 review corrections pushed. ⚠️ see below |
| `copilot#224` chip origin | **copilot#225** | ✅ **MERGED** `6c6f7db60`, verified on main, CI green |
| `sim#47` metric | **sim#48** | ✅ **MERGED** `825b1d456` (true merge, 25 files), CI green |
| `fm#1213` path traversal | **fm#1215** | Fix + round-2 review corrections pushed. ⚠️ see below |
| `fm#1208` prompt injection | **fm#1216** | Fix + round-2 rework pushed. ⚠️ see below |
| `fm#1201` capture provenance | **fm#1218** | Fix pushed, CI green on the branch |
| `fm#1204` Cause G ceiling | **fm#1219** | Cause G split into G + new Cause I; all 91 validate; pack rebuilt and blast radius verified |
| dashboard OpenSSL CVE | **dash#117** | ✅ **MERGED** `9fac8487d`; unblocked #116 after a branch sync (a re-run was NOT enough — see hygiene) |
| `dash#115` closure label | **dash#116** + **copilot#227** | Paired, identical copy; both suites green |

**`fm#1210` filed** (from the #1209 review, measured): `novel_files_uploaded` is
**never populated**. `investigation_service._preprocess_attachment` appends the
row to the same `case` object *before* `process_turn`, so `known_ids` always
holds the id and the novelty condition is always False. #1136's stall-net arm is
dead for uploads: a turn where the user supplies a genuinely new file is scored
no-progress. **Pre-existing** — measured identically on `main` and on #1209's
branch — so do not read it as fallout from that PR.

#### ⚠️ `fm#1211` — the fix is right, the path is dead, and it exposed three more

`/code-review 1211 high` found round 1's central claim **false**: `description=`
is referenced **zero times** in `upload_document`'s body (AST-verified), so the
lineage it claimed to carry recorded nothing. Attribution now rides `owner_id`,
which reaches four real columns. Two round-1 tests were also not measuring what
they claimed.

Three defects that fixing #1200 made *reachable*, now closed in the same PR:
re-approval published duplicates into the **global** corpus (measured: 3
approvals → 3 items, first two orphaned), approval could claim a null
`knowledge_item_id`, and the published OpenAPI description advertised
`verification_level=2 (admin verified)` for content that ships EXPERIMENTAL.

Two filed rather than fixed:

- **`fm#1213` (security)** — `upload_document` mints the on-disk filename from an
  unsanitized title; `'../../../etc/pwned'` resolves outside `data/knowledge`.
  **Pre-existing and live today** via `POST /knowledge/documents` (`title` is a
  form field, platform-admin gated). #1211 adds a second, LLM-influenced source.
- **`fm#1214`** — `app.state.suggestion_service` is **never set**. Every request
  builds a fresh empty service, so extract → approve cannot complete: the approve
  route 404s before reaching the 400 #1200 describes. Also covers the fake-id
  `else` branch that 100% of production traffic would take, the bypassed runbook
  quality gate, and the missing compensating delete.

**Merge judgement for the owner:** #1211's path is currently unreachable, so
landing it does not make the knowledge flywheel work and must not be read as
doing so. Recommend landing anyway — its guards are what make #1214's wiring
safe — but holding it behind #1214 is a defensible alternative.

#### ⚠️ `fm#1215` — round 1's guard was vacuous, and the PR body said otherwise

`/code-review 1215 high` found the containment assertion anchored on
`target_dir` — the directory the caller-influenced component is IN — so an
escaped directory trivially contained its own child and the check passed.
Measured on that branch:

```
upload_document(title='pwned', scope='personal', owner_id='../../../../escaped')
  files OUTSIDE : ['escaped/pwned-kb-f14d64c17d8d4c97.md']
  dirs created  : ['data/knowledge/user_..', 'escaped']
```

Also: `mkdir(parents=True)` ran *before* the check, and the only write-site test
used `scope='global'` where the branch could never evaluate false — so the guard
shipped with a false anchor AND zero executing coverage. Now anchored on
`data_dir`, validated before `mkdir`, with `team_id`/`owner_id` sanitised so an
escape is unconstructible. `ConversionService._scope_dir` had the identical
shape and is fixed too.

Helper defects, all measured: `runbook_filename('...', '___')` returned `'.md'`
(hidden dotfile, all such docs collide), the id suffix was unbounded (412 chars,
past ext4 NAME_MAX and `filename String(255)`), and a `None` id raised.

**Severity stays low** — the live route is platform-admin and the directory
components come from auth context, not request bodies. The reason to land it is
that the hardening it advertised did not work.

#### ⚠️ `fm#1216` — round 1 used the wrong mechanism AND left the bigger hole

`/code-review 1216 high` found two things worth carrying forward.

**Entity-escaping was wrong here.** Round 1 escaped `&`/`<`/`>`/`"`, so
`R&D-config.yaml` rendered as `label="R&amp;D-config.yaml"`. Nothing decodes
entities on this path — the prompt is READ by the model — while `search_file`
reports the raw name and the engine says to cite the label verbatim. The model
would echo a filename the user never had: #666's failure mode through the back
door. Now SANITISED (`"`→`'`, `<`/`>` dropped, whitespace runs incl. newlines
collapsed, `&` untouched). `causal_map._sanitize_label` escapes and is right to
— mermaid genuinely decodes. Opposite context, opposite answer.

**The body channels are the larger hole, and are NOT fixed.** Verified: a file's
own CONTENT forges a complete `<uploaded_file>` element
(`rendered.count("<uploaded_file")` 1 → 2), likewise via `ev.extract` and
`ev.summary`. That is strictly worse than the filename vector — file content IS
the incident data, pasted from systems the submitter does not control. Neither
sanitising (corrupts evidence) nor escaping (breaks verbatim citation) works
there; it needs a fencing scheme, which is a design call. **Filed as `fm#1217`**,
with an `xfail(strict=True)` in the suite so that file cannot be read as proof
the class is closed — which is exactly how round 1 read.

**#1209 also fixes the persisted half of `#1201`**, unplanned — and **#1218**
now fixes its derivation, so the two halves compose. The engine's
duplicate carried the `upload_source` value `investigation_service` fabricates
from the filename prefix (`file_upload` for a page capture), and the
non-COALESCE'd upsert made it win over the genuine `page_capture` tag. Measured
`[page_capture, file_upload] → file_upload` before, `page_capture` after. The
derivation at source is still wrong and stays with #1201 — and the `#1201`
premise written into `context_builder.py:1633-1637` is now stale for new rows.

### `fm#1204` — the issue overstated one half, and the pack is the real work

`kb-validate --strict` on `main` reports **two** errors, not the six-indicator
shape the issue describes:

1. Cause G is **3358 chars** against the 3000 chunk ceiling (as filed).
2. **ONE** entry in the Indicators section carries no `[Step N]`/`[Symptom]`/
   `[Default]` token — and it is not an indicator at all, but an explanatory
   paragraph about IRSA audience partition-invariance that was placed inside
   that section. The issue's "6 of 8 indicator entries" does not reproduce; the
   actual Indicators list has 2 entries and both are tokenised.

Fixed by SPLITTING rather than trimming, which addresses both errors at once:
the audience material becomes **Cause I** (its own statement/chain/indicators/
interventions, mapping onto the existing Step 8, which already returns
`Audiences`), and Cause G keeps the provider-registration failure mode. G is now
2944 chars, I is 2341, and **all 91 runbooks validate**.

Two measurement corrections worth recording:

- The issue's suggestion to audit the other #1082 runbooks — **done, they are
  clean**. 1 of 91 failed, and it was this one.
- I initially measured Cause Z at 4411 chars and nearly "fixed" it. That was a
  regex artifact — as the last cause, the split captured `## Prevention` and
  `## Sources` too. Bounded correctly it is **987**. The validator was right and
  my measurement was wrong; check the tool before trusting a hand count.

The **KB pack had to be rebuilt** — it vendors a COPY of each runbook plus
pre-embedded BGE-M3 vectors, so a source edit stales it. `kb-build-pack --check`
is the gate that says so, and it failed on the edit. The rebuild needs the cached
BGE-M3 (4.3G, present on this box) and took ~60 min for 1297 chunks on CPU.

**The rebuild is deterministic**, which is worth knowing before anyone fears a
pack rebuild: 1 of 91 runbook entries changed, 90/90 unchanged entries kept an
identical `content_hash`, and **2 of 1297 vector rows** differ — the two cause
chunks. `total_chunks` is unchanged at 1297, because the oversize block was
ALREADY being split in two; the fix replaces a bad boundary with a good one.

What that bad boundary was, measured against the shipped pack: a 3005-char chunk
ending mid-code-fence plus an **orphaned 352-char fragment** beginning
`**Risk:** None` — no heading, no statement, no chain, retrievable on its own.
Cause G is the IRSA scenario's actual root cause, so the block carrying the
correct answer was the one arriving incoherent. That is the shape #1204 suspected
might sit upstream of #1079.

**Not done:** re-deriving the IRSA engine conclusion. It needs the committed
replay harness over a corpus, not one transcript — and now that both this and
`sim#47` are fixed, such a re-derivation is finally meaningful. Schedule it as
its own piece.

## ✅ CLOSED by hardening batch 1 (2026-08-29) — verified merged by content

| Issue | PR | Merge commit |
|---|---|---|
| **`fm#694`** one-file contract | #1220 | `8f84f15fe` |
| **`fm#1214`** suggestion wiring + gate | #1221 | `e4855b133` |
| **`fm#1217`** prompt body-channel fence | #1223 | `6117232b4` |
| **`fm#1210`** upload novelty (tri-state) | #1224 | `e745cfad0` |
| **#1213 follow-ups** containment helper | #1225 | `58c5e9dd1` |

## ⬅ OPEN backlog — the seven follow-ups filed by batch 1

Grouped by root-cause **class** (the convergence unit; close a class, not an
instance). Batch-2 lanes and the two held-for-decision items are specified in
the new-session opening statement — not repeated here.

| Issue | Class | One-line |
|---|---|---|
| **`fm#1228`** | prompt trust-boundary | fence `problem_context` + `entity_highlights` — the complete unfenced remainder (all 10 blocks classified in `prompts/fence.py`) |
| **`fm#1229`** | silent-degradation seam | novelty counted only on the generation path; thread it through gate branches + service-routed intents |
| **`fm#1226`** | flywheel completeness | `EXTRACTION_PROMPT` can't emit a gate-passing runbook, so approval always refuses an unedited draft |
| **`fm#1227`** | flywheel completeness | suggestion store is in-memory (non-durable / multi-worker-invisible / unbounded) — needs the DB store. **Held: heavier.** |
| **`fm#1222`** | contract explicitness | clarification emitter handles only `failed[0]` — paste+file second attachment unrecoverable |
| **`fm#1235`** | path containment | `FilesystemStorageBackend` uses a denylist, not the root-anchored allowlist. Low sev (system-generated keys) |
| **`fm#1230`** | identifier uniqueness | `conversion_drafts.runbook_id` collides for empty slugs, no unique constraint. **Held: data decision.** |

### Are these Beta blockers? Measured: NO — and the reason matters

Re-judged on 2026-08-28 rather than inherited, since three of the four did not
exist when the "none is a hard gate" call was made.

- **`fm#1214` — not a blocker. Nothing calls it.** The only references to
  `/knowledge/suggestions/*/approve` and `/cases/*/extract-knowledge` in either
  frontend are in the GENERATED types; no hand-written UI invokes them. So no
  participant and no admin can reach it through a UI. It is a dead backend route
  with a published API surface — worth fixing because the contract advertises
  it, not because anyone hits it.
- **`fm#1217` — the strongest of the four, and still not a gate.** The forged
  element lands in the SAME case's prompt, from content that case's own user
  pasted. It is self-inflicted within one tenant, not a cross-tenant leak — which
  is what separates it from gate 3 (two-tenant isolation). Schedule it early;
  do not hold Beta on it.
- **`fm#1210` — narrower than it first reads.** `_check_if_progress_made` has
  four arms; only `novel_files_uploaded` is dead. A turn still counts as progress
  via novel evidence, a new outstanding `EvidenceNeed`, or a hypothesis test. It
  bites on the specific shape "user uploads a genuinely new file AND the LLM
  emits nothing novel" — real, and a mis-score toward *stalled*, but the net is
  not off.
- **`fm#694` — not a blocker.** Known clients all self-discipline today.

**So the Beta bottleneck is still the six owner DECISIONS, not code.** Do not let
this session's findings delay the gate list; they are quality and hardening work
that can run in parallel with, or after, those calls.

Plus two follow-ups recorded as a comment on the now-closed **`fm#1213`**: the
slug rule exists in three places (the other two mint PERSISTED ids, so
consolidating is a data decision), and `conversion_service`'s `update_draft` /
`verify_draft` re-open a `file_path` read back from the DB with no containment
re-check.

**Deliberately not attempted:** re-deriving the IRSA engine conclusion. #1204 and
`sim#47` were both unreliable and are both now fixed, so a re-derivation is
finally meaningful — but it needs the committed replay harness over a corpus, not
one transcript. Schedule it as its own piece.

## P1 — during / immediately after Beta opens

- **`fm#1122` release decision (owner).** (b) attack the trigger — make the #1091
  refusal *act* rather than advise, auto-retiring a refused claimant that
  duplicate-matches the owner; keeps the guard, removes the pollutant at source.
  Recommended. (c) refusal-scoped non-lexical exclusion — untried, highest risk,
  sits on the #656 no-incorrect-conclusion boundary.
- **`fm#1204`** — then re-derive any IRSA engine conclusion, since both the
  runbook chunking *and* the measurement were unreliable.
- **`fm#355`** — route the schema tool through `response_schema`. Biggest engine
  payoff still on the board.
- **`fm#1016`** — see gate 4.
- **`fm#836`** storage-ref coherence — answer the cloud-volume question first: if
  `data/knowledge` is not shared under multi-replica this is data loss ⇒ P0.
  `fm#835` rides along.
- **KB tenancy trio `#1166`/`#1167`/`#1168`** — all three came out of probe
  `#1162`, which hunted adversarially for a leak and **found none**. Hardening,
  not blockers.
- **`dash#78`** — role control disabled on a premise that no longer holds.
- **Downstream contract adoption** — ⚠️ **corrected**: the gates do NOT go red
  (see above). Both clients pin `2.0.0` at ref `a879206c`; `main` serves `2.1.0`.
  There IS a version pin to bump in each client — `api-contract.pin.json`
  (`ref` + `contractVersion`) — contrary to the last board's note. Adoption is a
  PR of its own in each repo that moves the pin and regenerates
  `src/types/api.generated.ts`. **Nothing will turn red to prompt it.**

## P2 — after Beta stabilizes, by keystone

Fix the keystone and the cluster collapses; do not schedule members individually.

- **`fm#509` — LLM error classification (keystone).** Collapses `#824` (inert
  COMPRESS_MEMORY), `#510`, half of `#548`; `#552` is the HTTP-plumbing tail.
- **`fm#828` — revocation durability in standalone.** ⚠ Explicitly **not** a Beta
  gate (standalone-only, multi-user self-hosted). A design decision that must
  argue against #767's deliberate deletion of the Postgres store.
- **One backend change, two issues: `fm#752` + `dash#51`** — bind the ignored
  `GET /cases` filters, regen dashboard types, restore the date filter. `fm#741`
  folds into the same contract-touching PR.
- **Knowledge/KB leftovers:** `fm#907` (CHROMADB_URL branch sends no auth token —
  real on cloud) → `fm#936` → `fm#955` → `fm#878`.
- **Test-infra session:** `fm#947` + `fm#942` + `fm#908`.
- **`fm#982`** — shadow-stack remainder; re-verify scope before claiming.
- **Flip-coupled UI, now unblocked:** `dash#45` (org/team management console),
  `dash#35` (three visibility tiers + role-naming), `dash#67` (grants review
  console — backend already built).
- **Small/independent:** `fm#923`, `fm#918`, `fm#694`, `fm#520`, `fm#512`,
  `fm#583`, `fm#522`, `fm#1048` (422-with-bytes crashes the validation handler ⇒
  opaque 500 on any endpoint given a non-JSON body).

## P3 — enhancements and deliberate deferrals

`fm#640` (LLM usage view), `fm#791` (terminal case-analytics metrics),
`fm#613`/`fm#614`/`fm#611`/`fm#610` (prompt-sizing/caching residue).

## Parked — trigger-gated, do not schedule

- `fm#673` — gated on chain-grounding reliability; ratified as the design's own
  endpoint. Do not "work" it.
- `fm#710`, `fm#723`, `fm#857` — each records its own reopen trigger.
- `fm#904` (protobuf/litellm watch), `fm#980` (cryptography exception watch —
  workos pin), `cloud#15` (unpinned core dep breaks cloud silently).
- `fm#985` — items 10/15/17 only, all **owner decisions**, not code sessions.

## Owner decision queue (consolidate into ONE ask, not five sessions)

- **Beta scope: Slack in or out** — gate 1. Also decides whether `infra#266` is a
  blocker or hygiene.
- **`#1016` scope** — how much of the data-deletion promise gates Beta now that
  real user data is in play.
- **`#1122` release decision** — (b) or (c). The engine defect is confirmed and
  the threshold route is proven closed; what remains is a product judgement about
  releasing a root the guard is deliberately holding.
- **`infra#266`** — volume encryption on the Postgres primary (key escrow is the
  risk), and whether to rotate Slack workspace bot tokens. Rotation forces every
  workspace to reinstall the app — an incident decision on evidence of
  compromise, not hygiene.
- **`#1169`** — does sideloaded extension distribution still need supporting? If
  not, one config line closes the impersonation hazard.
- **`#819` / `#629` closure** — the work is done; the trackers are not. Still the
  two refs most likely to mislead the next reader.
- **Merge policy during an active campaign** — see hygiene below. #1196 was merged
  at round 1 while rounds 2 and 3 were in flight; either hold PRs until handed
  over QC-passed, or accept residue PRs as the default.
- **Unanchored infra risks** — data-tier SPOFs, the Longhorn NFS backup target
  sharing a failure domain with the host, near-full host disks. Still no tracking
  anchor. `infra#266` is now one instance of this class with an issue; the rest
  are not.
- **`#985` items 10/15/17.**

## Session hygiene (keep these rules)

- **Claim by issue assignment before starting.**
- **Source-mutating sessions get their own worktree and a feature branch**; never
  `git add -A`. Check which branch a shared checkout is on before branching.
- **A squash-merged PR's branch commit is NOT an ancestor of main.** Verify a PR
  landed by content, never by commit ancestry. Corollary learned the hard way:
  **a merged PR stops tracking its branch**, so its head SHA and its CI results
  freeze at the merge point while the branch moves on. If a PR's head disagrees
  with `git ls-remote`, the PR is closed and later pushes are stranded.
- **Merging mid-rework strands the rework.** #1196 merged at round 1; rounds 2
  and 3 lived only on the branch, so every defect the review found stayed live on
  main while the PR read as done. Recover with a fresh branch off current main,
  transplant by content, and verify with blob hashes plus a re-run of the
  mutation harness — a rebase can silently drop a hunk.
- **`/code-review` may target a stale head.** Re-running it right after a rework
  can review the previous head and re-report fixed findings. Check the reviewed
  head against `git rev-parse origin/<branch>` and adjudicate each finding against
  the real head before relaying — telling an author their correct fix is broken
  invites them to re-fix working code.
- **The session scratchpad is shared by every lane.** Generic filenames
  (`probe.py`, `mutate.py`, `commit.txt`) collide silently. Two lanes were bitten
  in one session — one committed another lane's commit message, one had its
  mutation harness replaced by a harness targeting different files. A replaced
  harness still runs; it just tests something else and looks like a pass. Give
  each lane `scratchpad/lane<issue>/` at dispatch.
- **A review finding calling something "live-wired" may mean only "wired in DI".**
  Trace call sites to a route before accepting a finding as a live leak.
- **`pyproject.toml` exempts `test_*.py` from `F401`/`F811`**, so unused-import
  residue in *any* test file is invisible to CI. Do not read a clean lint run as
  proof a test file has no dead imports, and do not report such residue as
  pre-existing noise without checking whether the PR introduced it.
- **`opik` is not installed on this box**, so
  `tests/unit/infrastructure/observability/test_opik_fail_closed.py` fails 6/10
  locally while CI is green. Not a regression; do not chase it.
- **`api-types-drift` in the frontends goes red when faultmaven's spec changes.**
  That is the gate working. Regenerate in a PR of its own.
- **Never write `Fix #N` / `resolves #N` in a PR body about an issue the PR does
  NOT close.** GitHub parses prose. #1198's dependency banner ("Fix #1207 via
  Option 1") and a forward-reference ("Whoever resolves #694 should know…")
  auto-closed two live defects on merge. Use "see #N" / "#N must be fixed first".
  Conversely, a PR that DOES fix an issue needs the keyword — #1199 fixed #1195
  and left it open.
- **A `Mock`/`AsyncMock` with `spec=<function>` does NOT enforce the call
  signature; only `create_autospec` does.** Measured this session. It matters
  whenever the defect under test IS a signature mismatch (#1200): the first draft
  of that lane's fixture used `AsyncMock(spec=...)` and 2 of 12 tests passed when
  they should have failed. Same family as the autospec note already in memory —
  and note that setting `.return_value` on an autospecced attribute preserves
  enforcement, while replacing the attribute destroys it.
- **A test can pin a state production never reaches.** #1209's first draft seeded
  an aggregate without a row that `investigation_service` always appends, making
  a dead branch look reachable; the whole block could have been deleted with
  every test green. Drive the real call ordering, and when a review says a branch
  is unreachable, **measure the same thing on `main` before accepting that your
  change caused it** — on #1209 the claimed regression was pre-existing.
- **Run the probe with the tree forced.** `PYTHONPATH=<worktree>` and assert
  `module.__file__`; the editable install silently wins otherwise. It did once
  this session and reported the branch's numbers as identical to main's.
- **A route test whose fixture is malformed passes its error-code assertions
  vacuously.** A `DevUser` missing two required fields made every request 500 in
  the #1211 lane, so two `assert status_code == 500` pins passed while proving
  nothing. Assert a SUCCESS case in the same file — it fails loudly when the
  scaffolding is broken — and mutate the route to confirm the error pins bite.
- **Register the real exception handlers in route tests.** A bare
  `FastAPI(); include_router(...)` app maps `ConflictError` to a raw 500, not the
  documented 409: the mapping lives in `get_exception_handlers()`, not the route.
- **Anchor a containment check on the ROOT, never on the directory the
  untrusted component is in.** That is circular: an escaped directory contains
  its own child. #1215 shipped exactly that mistake, with a PR body asserting it
  was covered. Validate BEFORE `mkdir(parents=True)` too, or an escaping path is
  materialised whatever the write then does.
- **A guard that cannot fire through the public surface needs a test that
  defeats a sanitiser to reach it.** Otherwise it has zero executing coverage
  and a wrong one looks identical to a right one.
- **Escaping and sanitising are not interchangeable — ask whether anything
  DECODES.** Mermaid does, so `causal_map` escapes. The LLM prompt does not, so
  entity-escaping there just shows the model `&amp;` and breaks the
  cite-verbatim contract. #1216 shipped the wrong one first.
- **A "safe values" test class must use values the transform would change.**
  #1216's guard used names containing no character any escaper touches, so it
  passed under every scheme including one that mangled real filenames. Two
  reverts are the tell: one proves the fix is safe, the second proves it is not
  merely safe-but-wrong.
- **Re-running a `pull_request` workflow REPLAYS its original merge commit; it
  does not recompute it against the updated base.** So a PR blocked by something
  that has since been fixed on main does NOT go green on a re-run — the replay
  builds the same stale merge ref. The run metadata is the tell: `created_at`
  before the fix, `run_started_at` after, `run_attempt > 1`. The fix is a NEW
  run: merge the base into the branch and push.

  This bit **every open Dashboard PR at once** when `dash#117` fixed a Trivy CVE
  gate on main — `#116` (attempt 2) and `#118` (attempt **4**, so it had been
  re-run three times) both kept failing on `libcrypto3 3.5.7-r0` for hours
  afterwards. When one base-image/security gate is fixed on main, EVERY open PR
  in that repo needs a branch sync, not a re-run. Syncing a peer lane's branch is
  safe when the file sets are disjoint — verify that, verify their files are
  byte-identical after the merge, run their suite, and say so on the PR.
- Decision-queue items go to the owner as one consolidated ask.
