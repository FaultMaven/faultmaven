# Owner decision brief — 2026-07-29

Decisions that are **mine to raise and yours to make**. Each one below is
stated with the facts you need, the options, and my recommendation. Nothing
here is blocked on further investigation unless it says so explicitly.

Companion documents: the full prioritized board is
`WIP-release-blocker-campaign.md` (74 issues, P0→P3). The next session's
execution brief is in memory (`project_next_session_p0_batch2`). This file
holds **only things awaiting your call.**

Status key: ⬜ open · ✅ decided · ⚫ closed by events

> **Owner accepted all recommendations, 2026-07-29.** D2, D3 and D4 are
> executed and closed out below. **D1 is measured and now needs only a "go"** —
> the measurement changed the answer materially (see D1).

---

## D1 ✅→⬜ MEASURED — needs only a "go"; the window is ~seconds, not a window

**Measured on the live primary 2026-07-29 (read-only):**

| metric | value |
|---|---|
| `faultmaven` database size | **18 MB** |
| index count | 386 |
| **total index bytes** | **6656 kB (6.6 MB)** |
| largest single index | 56 kB (`knowledge_items`) |
| largest table | `case_messages`, 298 live rows |
| active connections | 7 |
| `datcollversion` | `faultmaven` **2.36** ⚠️ — all others 2.43 |

**This changes the decision.** `REINDEX DATABASE faultmaven` has 6.6 MB of
indexes to rebuild, the largest being 56 kB. That completes in **seconds** —
faster than an API pod restart. Option (c) (`REINDEX ... CONCURRENTLY` with a
per-index driver) is **unnecessary**; the added complexity buys nothing at this
scale. There is no meaningful maintenance window to schedule — just a brief
moment where writes block.

**Recommended action: run it at the next convenient moment, in this order.**

```sql
REINDEX DATABASE faultmaven;
ALTER DATABASE faultmaven REFRESH COLLATION VERSION;
```

I have not run it: it takes write locks and alters database metadata on the
live primary, so it needs an explicit go. **Say the word and I will execute and
verify the stamp flips to 2.43.**

*(Original analysis retained below for the reasoning.)*

## D1 (original) — Schedule the maintenance window for infra#142

**The single highest-severity item on the board.** It is the only open issue
that threatens data you already hold.

**Facts** (from infra#142, found during the #629 rehearsal on 2026-07-28):

| database | `datcollversion` |
|---|---|
| `faultmaven` (LIVE) | **2.36 ⚠️** |
| `postgres` | 2.43 |
| `faultmaven_slack` | 2.43 |
| `template1` | 2.36 → already refreshed to 2.43 during the rehearsal |

The container image's glibc moved 2.36 → 2.43 at some past image bump. Every
text-collation index in the live `faultmaven` database was therefore built
under the *old* ordering. glibc collation changes can silently corrupt index
ordering — wrong lookups and unique-constraint bypass. The version stamp exists
precisely to flag this, and it is currently flagging it.

**The fix, in this order and no other:**

```sql
REINDEX DATABASE faultmaven;
ALTER DATABASE faultmaven REFRESH COLLATION VERSION;
```

Refreshing first would silence the warning while leaving every index built
under the old ordering — the corruption stays and the signal is gone.

**What I cannot tell you from here:** how long `REINDEX DATABASE` will take or
how much it locks, because that depends on the live database's size and index
count, which I have not measured — I don't poke production without being asked.
`REINDEX DATABASE` takes locks that block writes to each table as it goes.

**Options**
- **(a) Measure first, then schedule** — I get table/index sizes off the live
  DB and come back with an expected duration, so the window is sized from data
  rather than guessed. *(Recommended.)*
- **(b) Schedule a generous window now** and accept an unmeasured duration.
- **(c) Use `REINDEX ... CONCURRENTLY` per index** — far less disruptive, no
  full-database lock, but slower overall, and needs a per-index driver script.
  Worth it only if the measurement in (a) shows an unacceptable window.

**My recommendation: (a), starting now.** The measurement is read-only and
cheap; the corruption risk is already accruing, and everything else about this
decision hinges on a number neither of us has yet.

---

## D2 ⬜ fm#565 — split it, then act on the half that is actionable

> **⚠ SUPERSEDED 2026-08-03 by PR #965 (fm#903/#902)** — the authoritative
> record is now `docs/operations/security/vulnerability-exceptions.md`.
> Two premises below are stale: item 3 (protobuf) no longer "needs a
> fireworks-ai bump" — the SDK was a dead dependency (REST provider) and was
> **removed**, protobuf floor raised to `>=5.29.6`; item 4's blocker is not
> opik but **our own `pydantic>=2.6,<2.10` ceiling** (every patched litellm
> needs pydantic ≥2.10 or ==2.12.5), and litellm *does* ship in Docker-based
> Standalone (the image installs requirements/cloud.txt) — the proxy-server
> unreachability rationale is what carries the acceptance, in both modes.

fm#565 is currently one issue containing four unrelated dispositions. It should
be **re-triaged, not implemented**. The four items:

1. **ChromaDB pre-auth code injection, `GHSA-f4j7-r4q5-qw2c` (CRITICAL) — no
   upstream fix exists.** Vulnerable range is `>=1.0.0, <=1.5.9` and PyPI's
   latest *is* 1.5.9, so there is nothing to bump to. Exploitability is
   contained: Standalone runs ChromaDB in-process (`PersistentClient`), so the
   vulnerable HTTP server never starts; Cloud is ClusterIP-only with token auth
   and a NetworkPolicy scoped to two pods (infra#86). An attacker must already
   be executing inside one of those pods.
   → **Decision needed: formally accept this risk with the compensating
   controls documented, and set a watch for a release >1.5.9.**
2. **Transitive HIGHs — fixes exist, just deferred for batching:** `urllib3`
   2.6.3→2.7.0 (decompression-bomb bypass, cross-origin header leak),
   `msgpack` 1.1.2→1.2.1 (OOB read), `starlette` 1.0.0→1.3.1 (form() limits
   ignored, StaticFiles UNC SSRF — gated by `fastapi==0.136.0` compatibility).
   → **These block a production image and are a normal batched bump.**
3. **protobuf DoS** — blocked by an exact pin from `fireworks-ai==0.19.20`.
   Needs a `fireworks-ai` bump or an explicit override plus a test run.
4. **litellm criticals** — transitive via `opik`, never imported, and both CVEs
   are LiteLLM *proxy-server* bugs we don't run. Bumping `opik` actually
   *downgrades* litellm. Not reachable; re-check on a future `opik` release.

**Also inside fm#565, and separable:** pyjwt now emits
`InsecureKeyLengthWarning` for HMAC secrets under 32 bytes. **Confirm the
production `JWT_SECRET_KEY` is ≥32 bytes.** This is a one-line check with real
consequences and does not belong buried in a dependency triage.

**My recommendation:** close fm#565 as a tracking issue and split it into
(i) accept-and-document the ChromaDB risk, (ii) the transitive-HIGH batch —
schedule as P1, (iii) a watch item covering protobuf + litellm, and
(iv) verify the production JWT secret length **now**.

---

## D3 ⬜ Access-token lifetime: 60 minutes vs the documented `<30 min` posture

Your local `.env` sets `JWT_ACCESS_TOKEN_EXPIRY_MINUTES=60`. The code documents
this field as "short-lived per security posture (<30 min)"
(`config/settings.py:1161`), and `CLAUDE.md` says the same. It is not a
regression — it was 60 before the #832 rename too, under the old alias — but it
is a live deviation from a stated security property.

Note this is the **local/HS256** knob only; cloud lifetimes come from the other
settings half (see #888).

**Options**
- **(a) Lower `.env` to the 15-minute default.** *(Recommended.)*
- (b) Amend the posture text to permit 60.
- (c) Keep 60 with an explanatory comment in `.env` recording why.

**My recommendation: (a).** The asymmetry decides it: amending the posture
weakens a documented security property of the *product* to match one dev box's
convenience, and that statement is load-bearing — the revocation design leans
on short-lived access credentials, especially since the request-path revocation
check fails *open*. Lowering costs you almost nothing because refresh rotation
makes re-auth invisible. If the 60 was deliberate (long debugging sessions),
take (c), not (b).

---

## D4 ⬜ Close four issues as won't-do?

All four are explicitly speculative and carry "open this when evidence exists"
triggers in their own bodies. None of those triggers has fired.

- `fm#349` — Rule-2 compliance signal for eval/CI (self-described as not urgent)
- `fm#357` — UX auto-upgrade of close intent when readiness is READY
- `fm#511` — consolidate `_node`/`_hyp`/`_case` test factories (trigger: "act
  on the 4th copy or the next contract change"; there are 3)
- `copilot#71` — self-host custom-domain / TLS polish (deferred, not a bug)

**My recommendation: close all four**, referencing their own triggers. They can
be reopened the moment the trigger fires, and an open issue that nobody will
act on makes the board less honest about what is actually left.

---

## Closed by events

- ⚫ **`.env.example` sync failures — GONE.** I reported three failures
  (`OPIK_TRACK_DISABLE` drift, `OPIK_TRACK_OPERATIONS` and
  `UPLOAD_TIMEOUT_SECONDS` not being settings fields). Re-run on current main
  2026-07-29: **`0 problems`, exit 0.** Either you fixed it or #900's settings
  refactor resolved it. No decision needed. *(Lesson: that reading was stale
  within a day — decisions belong here, dated, not in chat.)*
- ⚫ **Plan doc as a PR — not applicable.** `docs/working/` is gitignored by
  design ("local working files, not tracked"), so the board correctly stays
  local. My earlier offer to open a PR for it was wrong.

---

## D5 ⬜ (added 2026-08-04, from #959 claim-time verification) — the `permissions` claim vocabulary

**Fact (verified on main `9f218daa`):** `AuthenticatedUser.from_jwt_claims`
reads a `permissions` claim; every live mint emits `scopes` and `roles`, never
`permissions`. So `.permissions` is **always `[]` in production**. Nothing
gates on it today — `require_permission`/`has_any`/`has_all` are defined and
exported from `api/middleware/auth.py` but wired to **zero routes** (swept
2026-08-04). The landmine is already signposted in
`test_token_forger_shape_parity.py` and `tests/utils.py`: the first route to
adopt `require_permission` will pass the integration suite (test forgers emit
`permissions`) and 403 every production caller.

**Options:**
1. Map fine-grained scopes (`cases:read`-shaped) into `permissions` in
   `from_jwt_claims`, keeping OIDC scopes (`openid profile email`) out.
2. Mint an explicit `permissions` claim at the generator (token schema change —
   touches #938/#880 contract territory).
3. Delete the `require_permission` surface until RBAC vocabulary is unified
   (it fails closed — 403 — but its presence invites exactly the trap above).

**Recommendation:** (1), as a small PR with forger parity updated in the same
change — it makes the existing exported surface truthful without a token
schema change. Not folded into #959 (mint-side unification) because it is a
product-semantics call: are token scopes and RBAC permissions one vocabulary?

