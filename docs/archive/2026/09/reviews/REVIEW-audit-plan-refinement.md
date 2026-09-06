# Refinement of the three-campaign audit plan — v2, post-Fable

Reviewed against `origin/main`. v1 of this document made three confident claims
that Fable refuted and I then re-verified as wrong; they are corrected below and
the errors kept visible rather than quietly dropped, because the *reason* for them
is itself a finding (see §0).

Baseline: `origin/main` was `620698b3` at v1; it has since advanced by
`c3516705` (#822, D10 service-account refresh work), which is material to §2.

---

## §0 Why v1 was wrong — the process finding

v1 critiqued the plan's **one-line summaries** without reading the **underlying
GitHub issues**. Issue #769's own text already contained everything v1 presented
as a correction *and* the part v1 got wrong. So v1's headline recommendation
("rescope #769 before starting") was aimed at a compressed summary, not at the
tracked scope — and v1's rescope was **less accurate than the issue it was
correcting**.

**Rule for this plan's execution: read the issue, not the plan row, before
sizing any item.** The plan is an index; the issues are the scope.

## §1 — #662 is DONE (verified), but it is still OPEN on GitHub

Merged `620698b3` (PR #820) + `c2dc788b`, `08c1f73b`, `269e2907`. Fable
re-verified every sub-claim:

- **No trimming or compression was built.** Zero hits for trim/compress/
  summarize-history on the overflow path. What shipped: the pre-existing
  minimal-prompt degrade (`milestone_engine.py:6769–6813`) made *reachable* by
  re-keying `_is_context_length_error` on the shared `TOKEN_LIMIT` code
  (`:885–925`), plus surfacing (`lifecycle_metrics.py:483`,
  `templates.py:2509`).
- **`ErrorAction.COMPRESS_MEMORY` is a dead action** — produced at
  `llm_error_handler.py:308`, and `with_retry`'s only action branch is `RETRY`
  (`:379`). Nothing consumes it.
- **500→503 was #717** (`cd8e603f`; `exception_handlers.py:182`), not #820.

So the plan's "trim/compress … map to 502/503" language describes work that does
not exist. Two consequences the plan should carry:

1. **`COMPRESS_MEMORY` needs a tracked disposition** — consume it or delete it —
   under the no-dead-code norm. This is independent of whether compression is ever
   built, and neither document had it.
2. **#662 must be closed with evidence** (PR #820 / `620698b3`). It is *still
   open*: verified `gh api …/issues/662` → `state: open`. It falls between "drop
   it from the queue" and the M0 close list, and belongs in the latter.

## §2 — #769: the plan and the issue were right; v1 was not

**REFUTED (v1's core claim).** v1 said the plan's diagnosis was "materially
wrong" and framed two parallel revocation mechanisms. What is actually true:

- `RedisTokenManager` mints **opaque UUID tokens, SHA-256 hashed, keyed off
  `DevUser`** — a pre-JWT legacy abstraction. **JWTs are never written into it.**
  It is not a second revocation mechanism for request-path tokens; it is an
  orphaned legacy subsystem.
- Therefore v1's "open decision — whether `RedisTokenManager` becomes the
  per-user index" **is not a real option**: it holds no JTIs. The disposition is
  deletion.
- **v1's severity escalation collapses.** I claimed the admin endpoint returns a
  false non-zero count implying containment. It cannot: the per-user set
  (`auth:user_tokens:{user_id}`) is written **only** by `create_token`
  (`token_manager.py:67`), which I verified has **zero production callers** — the
  definition is the only hit outside tests. So `revoke_user_tokens` iterates an
  empty set and always returns 0, responding `"Revoked all 0 tokens for user"` at
  HTTP 200. Visible no-op, not false success.
- **Severity Med stands.** With `c3516705`'s refresh-path liveness check
  (`oauth_service.py:467–473`, `user.is_active` rejected on refresh) plus <30-min
  access expiry, "bounded to one <30-min token" is an accurate description. v1
  also cited that check as being on the main it claimed to review — it was not
  yet; I read it from the local D10 branch.
- **Do not reopen #767.** Its "one revocation store" holds for the artifacts the
  request path validates.

**Still correct from v1 (Fable verified exact to the line):** the request path
consults only the unified jti store (`auth_service.py:455/516/535`,
`jwt_token_generator.py:315/395/736/803`; enforced at
`api/v1/auth_dependencies.py:214`); that store is jti-only
(`add_revoked_token(jti, ttl)` / `is_revoked(jti)`); `auth_service.revoke_user_tokens`
is an honest no-op used by `user_service.py:446/514/620/868` **and `:932`**.

**Execution per the issue, not per v1:** per-user JTI index at mint (or per-user
`not_valid_before`) fanned into the unified store; **delete** `RedisTokenManager`
as test-backed dead-code removal (empty store, no production `create_token`
caller, `/me` `token_count` always 0). Add a regression test on the **endpoint
response**, not only the store — both the plan ("revoked N") and v1 ("non-zero
count") described an observable that does not exist.

## §3 — #689: right gap, wrong seam in v1, and bigger than either document said

**v1 cited the wrong seam.** #689 is about evidence blobs through
`FileStorageService` (hardcoded local aiofiles I/O). v1's evidence —
`SimpleStorageBackend` at `container/providers/services.py:229` — is a
*different, nearly-dead* seam: `CaseDataIngestionService` holds it as
`self._storage` and uses it **only for a health-check status**, with no
store/retrieve call. Correct target confirmed.

**Verified and unchanged:** `get_storage_backend` (`storage/factory.py:25`,
honors `settings.providers.storage_backend`) has **no production caller** —
docstrings and `tests/infrastructure/test_storage_backends.py` only.

**New — the fix surface is four sites, not two.** Fable named the two composition
roots; I verified two more, inside *feature* code, each constructing storage
inline with a hardcoded local default:

| site | kind |
|---|---|
| `container/providers/infrastructure.py:603` | composition root |
| `services/service_factory.py:147` | composition root (duplicate construction) |
| `modules/agent/tools/read_file_tool.py:186` | **inline in feature code**, `storage_root=…"./data/evidence"` |
| `modules/agent/domain/services/agent_orchestration_service.py:1718` | **inline in feature code**, same default |

Routing only the composition roots through the factory would leave the two agent
sites reading local disk under `STORAGE_BACKEND=s3` — a *partial* fix that looks
complete. M3 must be sized for all four.

## §4 — the guard: right instinct, v1's design naive

v1 proposed one "container resolves the configured implementation" test. Fable's
corrections, which I accept:

- It would **not** have caught #769 — no setting selects between revocation
  stores; that defect is an orphaned subsystem, not config-ignored resolution.
- Standing up the real composition root pulls in FakeRedis + DB bootstrap. The
  in-repo precedent, `tests/unit/container/test_file_storage_service_wiring.py`
  (born from a 2026-04 silent-wiring incident), deliberately went **source-level**
  for exactly this reason.
- Any guard must cover **both composition roots** — and, per §3, the inline
  feature-code constructions too, which is how a "fixed" wiring silently regresses.

**Revised guard, two parts:**

1. **Config-resolution:** unit-test the provider factory functions directly
   against a settings stub (they take explicit args), covering both roots; plus a
   lint/source-level assertion that `FileStorageService` is not constructed
   outside the sanctioned factory.
2. **Behavioral revocation-liveness:** revoke via *every* surface that claims to
   revoke → assert the request path rejects. This is the shape that catches
   #769-class bugs, which no config test can.

**The structural root cause neither document named:** there are **two composition
roots** (`container/providers/` and `services/service_factory.py`) plus direct
instantiation in feature code. That duplication is *why* inert-machinery bugs
recur here, and it is a candidate item in its own right.

## §5 — numbering collides three ways (verified real)

`two-dimensional-hypothesis-methodology.md:120–125` defines **M1–M7** as
load-bearing, schema-and-engine-enforced invariants (M1 terminal+actionable root,
M4 empirical|deductive-only, M6 deterministic failed-fix demotion). The collision
has **already occurred in merged history**: PR #820's own title opens "M1 /
NO-COLLAPSE residual" meaning the *methodology* M1, while this plan's "M1" means
the milestone. Bucket B adds a third "M1" (WorkOS org-mapping).

Key milestones by issue number, or use a distinct prefix (`W0…W4`); rename Bucket
B's internal "M1" → "B1".

## §6 — M0 gate and dependencies (verified as judgment calls)

- **Evidence bar instead of a Fable gate on the no-code M0.** #662 being
  merged-but-open is the live example of the failure mode: closure must cite a
  merged PR/commit *and* a verification command whose output was seen.
- **Split tracker hygiene from memory hygiene** — different artifact classes,
  and memory isn't PR-able.
- **M2 → Bucket B:** state it as a must-fix-before-cutover policy rather than a
  hard blocker. With the refresh-liveness check now on main, #769 is bounded; the
  genuinely hard Bucket B blocker remains the unbuilt WorkOS org-mapping.

---

## Revised queue

| Order | Item | Change from the original plan |
|---|---|---|
| — | #662 | **DONE** (`620698b3`); drop the "trim/compress" language; **close the issue** in M0(a); file `COMPRESS_MEMORY` disposition |
| 1 | M0 hygiene | First. Split (a) issue closures — each with cited evidence, **now including #662** — from (b) memory corrections. No Fable gate |
| 2 | #769 | **Execute per the issue text** (per-user JTI index → unified store; delete `RedisTokenManager` as dead code). Keep **Med**. No rescope, no #767 reopen. Add an endpoint-response regression test |
| 3 | #689 | Correct seam = `FileStorageService`, **four** construction sites incl. two inline in agent code. Larger than the plan implies. Carry the §4 guard (part 1) alongside |
| 4 | #587/#608/#620/#623 | Unchanged — **not verified by me** |
| — | Bucket B | Unchanged; rename internal "M1" → "B1"; #769 as pre-cutover policy |
| — | candidate | Dual composition roots + inline construction (§4) as its own item |

## Not verified by me

Not audited by me: issues #587, #608, #620 and #623; the Bucket B
WorkOS-mapping claim; and the seven stale-issue closures individually (item 1's
own job). Bucket B ordering is taken on the audit's word.
