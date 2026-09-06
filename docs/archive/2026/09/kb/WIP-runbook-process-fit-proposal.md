# WIP — Seeder observable-skip + rung-indicator audit (was: "runbook↔process fit")

**Status:** Draft for review & approval · **Date:** 2026-07-15 · **Parent:** [`WIP-kb-remediation-plan.md`](WIP-kb-remediation-plan.md) (Phase 4, at task 4.6 — eval) · **Seeder:** `feat/kb-cause-seeder`

**Decision requested:** approve a **~half-day** insertion into Phase 4.6 (§4), NOT the original 5-task plan.

> **History (read once, then ignore):** the first draft of this doc proposed a five-task "runbook↔process fit" contract. Verification against the shipped seeder found **two tasks already done, one targeting a shape with zero corpus instances, and the framing over-built.** This version is the trimmed, evidence-corrected scope. The named "fit contract" is dropped as ceremony; what survives is one acceptance criterion, one eval, one narrow audit.

---

## 1. Context — where this came from

Scoping issue **#698** led into the KB remediation plan, and reviewing how the engine consumes runbooks surfaced a question worth making explicit: **what does the Phase 4 seeder do when a matched runbook is not the multi-rung happy path** — a fallback cause, a (hypothetically) chainless cause, a malformed record? If the answer is "silently nothing," a perfectly-matched runbook can contribute zero to an investigation, invisibly.

That question is legitimate. The original proposal over-answered it. The corrected, verified picture is below.

---

## 2. What we verified against the shipped seeder (this changed the scope)

All checks are against `feat/kb-cause-seeder` (`faultmaven/core/investigation/kb_cause_seeder.py`, `prompts/templates.py`) and the shipped pack.

| Claim | Verdict | Evidence |
|---|---|---|
| Fallback (`Z`) cause is excluded from seeding | **Already done** | `kb_cause_seeder.py:188` `if cause.get("is_fallback_cause"): return None, []` + `test_fallback_cause_is_skipped` |
| Prompt licenses the LLM to form hypotheses outside the seeded set | **Already done** | `templates.py:2731` "KEEP forming your own hypotheses for any cause the runbook did NOT cover — the seeds are a starting differential, not a ceiling"; also "a prior to TEST, not an answer" and "an unsupported seed decays on its own" |
| Corpus has degenerate/chainless *non-fallback* causes (the shape task 1 normalized) | **Zero instances** | 91 runbooks / 640 causes = 91 fallback + 549 non-fallback, all chained; 0 chainless non-fallback |
| Seeder consumes per-rung `indicators` / `interventions` | **No — dropped** | `grep -c "indicator\|intervention"` in seeder = 0; it reads `chain_nodes`/`chain_edges`/`cause_statement`/`cause_letter`/`cause_name` only |
| A matched runbook that yields zero seeds is observable | **No — silent** | `SeedReport` has no `skipped` field; ~6 silent `return None, []` sites; the summary log fires only `if report.seeded_anything` |

**Consequences:** the seeder already fits **100% of the actual corpus**. It is not under-designed. Two of the original tasks are shipped; one targeted a shape that doesn't exist (and building for it would violate the plan's own no-speculative-shapes rule). The real gaps are two, and both are small.

---

## 3. The two genuine gaps

### 3.1 The silent skip (the real fix)

Non-seedable causes (fallback, malformed, no-root, empty-statement) are dropped with **no record and no log**. A matched runbook contributing zero seeds is invisible — which is both an observability hole and the thing that would hide any *future* fit problem. This needs **no "normalize vs prose" decision**: with zero real degenerate instances, "not seeded; prose serves it" *is* the defined behavior — we are only making it **visible**.

### 3.2 Rung indicators/interventions are not structurally consumed — AUDIT VERDICT

**Audit run 2026-07-15 (verdict: confirmed write-only).** The full cause-record key set is exactly `{cause_letter, cause_name, cause_statement, chain_edges, chain_nodes, interventions, is_fallback_cause, rung_indicators}`, so `rung_indicators` + `interventions` are *provably the complete set* of unconsumed structured fields. Evidence:

- **`rung_indicators`** ships on all 640 causes (rung ref → prose indicator strings, e.g. `"[Step 3] kubectl describe job shows BackoffLimitExceeded"`; 1,475 prose values, 0 structured). Its **only** reference in the app is a "stored verbatim" comment (`bootstrap/kb_pack.py:84`) — **zero runtime consumers.** The seeder attaches no evidence-need / expected `causal_evidence` to seeded rungs.
- **`interventions`** are structured (quadrant/ref/text) but likewise consumed only as prose via the treatment-stage prompt, not structurally.
- **The deterministic `<!-- match -->` predicates are gone before runtime:** the pack has 0 `"predicate"` keys, 0 `exit_code`, 0 structured `threshold`. They are dropped at pack-build (kb-toolkit), so option (c) is **blocked** on a produce-side extraction change.

So per-rung diagnostic signal reaches the engine **only as prose for the LLM to read** — the seeded chain is topology with the validation signal detached. This is the deepest instance of the write-only-`causes` residue the whole plan targets. Three outcomes, **sized separately, not part of this half-day:**

- **(a) accept LLM/prose-mediated rung validation** — *the de facto current state*; seeded chain still gives dedup/decay/anchoring, but no machine-readable per-rung signal. Zero cost.
- **(b) seed `rung_indicators` as evidence-needs / expected `causal_evidence`** — **feasible now** (field ships structured-by-rung); a consume-side seeder change. The real "make it load-bearing" enhancement, and where a seeded runbook most helps (tells the engine what evidence to collect per rung).
- **(c) wire the deterministic `<!-- match -->` predicates** — **blocked**: needs a kb-toolkit `_extract_causes` change to emit predicates first, then a runtime evaluator. Cross-repo, largest.

**Recommendation:** ship Phase 4 on **(a)**; record **(b)** as the recommended separately-sized follow-on; **(c)** stays parked behind (b). Let the (a)/(b) call be *informed by 4.6b* (see task 3 sequencing) rather than decided in a vacuum.

---

## 4. Action plan (insert into Phase 4.6 — ~half a day)

| # | Task | Type | Notes |
|---|---|---|---|
| 1 | **Observable skip** (the real fix). `SeedReport.skipped` keyed on `(item_id, cause_letter)` — there is no `cause_id` field. See **impl note A** for the required skip-class taxonomy and the class-aware alarm. | Code + test | few lines |
| 2 | **Eval 3b → fold into 4.6b.** A non-seeded hypothesis can win over a wrong seeded prior. See **impl note B** for the mechanical end-state. | Test | the one additive assertion |
| 3 | **Rung-indicator audit — verdict recorded (§3.2).** Ship on (a); (b) is the recommended separately-sized follow-on; (c) blocked. See **sequencing note.** | Audit | done; no code this half-day |

Eval assertions are mechanical engine-state checks (LLM-agnostic), per the plan's Rule 4.

**Impl note A — skip-class taxonomy + class-aware alarm (task 1).** The ~6 `return None, []` sites are not homogeneous; tag each skip with its class, keyed on `(item_id, cause_letter)`:

- **intentional** — `:189` fallback (`is_fallback_cause`; never a candidate by design).
- **benign dedup** — `:252` `root_id in existing_roots` (a second retrieved runbook overlapping on a cause already seeded by the first; normal and correct).
- **quality drop** — `:198` no chain / `:203` non-root head / `:205`,`:225` bad `node_type` / `:221` empty statement / `:243` ingest produced nothing.

Fire the runbook-level **"matched runbook contributed nothing"** signal **only when a zero-seed runbook's zero is not fully explained by dedup/fallback** — i.e. ≥1 cause hit a *quality-drop* class. Otherwise two retrieved runbooks sharing a cause raise a false alarm every time (runbook B legitimately seeds nothing because A already did). That distinction is the difference between an actionable signal and noise.

**Impl note B — pin 3b's mechanical end-state (Rule-4, not a soft judge).** At eval end assert a concrete engine-state condition, e.g.: *∃ a hypothesis whose `root_node_id` is **not** in the seeded set, with likelihood **>** the seeded prior's, **and** the seeded prior is **not** `VALIDATED`.* Spell it out in the assertion so it is provider-agnostic.

**Sequencing note (task 3).** Let the (a)/(b)/(c) verdict be *informed by task 2's harness*, not decided in a vacuum: the seeder plants chain topology with no structured validation hook per rung, so the engine leans entirely on the LLM to map evidence to the right rung. If 4.6b shows seeded rungs validate poorly (evidence lands but the correct rung doesn't move), that is the concrete signal for outcome **(b)** — seed indicators as evidence-needs.

---

## 5. Cut from the original proposal (with reason)

| Original task | Disposition |
|---|---|
| Task 1 — empty-chain normalize to `root→D` or route-to-prose | **Cut.** Zero corpus instances; §4 forbids building for non-existent shapes. The observable-skip (task 1 here) makes any future instance visible, and "prose serves it" is the defined behavior — no normalization needed. |
| Task 1 — `Z`/`[Default]` never a candidate | **Cut — already shipped** (`kb_cause_seeder.py:188`). |
| Task 4 — reword prompt to license non-seeded hypotheses | **Cut — already shipped** (`templates.py:2731`). |
| Tasks 3a / crowd-out evals | **Already planned in 4.6b**; don't duplicate. |
| 3c (misleading → decay) | **Already done.** |
| The named "runbook↔process fit contract" + shape taxonomy | **Cut as ceremony.** Survives as one acceptance criterion (below), not a standing framework. |

---

## 6. Acceptance criterion (the one that survives)

> **The seeder has a defined, tested, observable outcome for every corpus-real cause shape — no silent drops** — plus eval 3b (a non-seeded hypothesis can beat a wrong seeded prior), plus a recorded verdict on rung-indicator consumption.

No guarantee regression: NO INCORRECT CONCLUSION, NO COLLAPSE UNDER PRESSURE.

---

## 7. Decision requested

Approve §4 (tasks 1–2, ~half a day) into the running 4.6, with impl notes A/B folded into the task descriptions handed to the agent. Task 1 is the real fix; task 2 is the additive eval. Task 3's audit is **done** (§3.2 verdict): ship Phase 4 on (a); outcome **(b)** — seed `rung_indicators` as evidence-needs — is the recommended separately-sized follow-on, its go/no-go informed by 4.6b's seeded-rung-validation behavior; (c) parked behind (b).

**Honest bottom line:** the seeder was **not** under-designed — it already fits the actual corpus. What's missing is thin: the skip is silent rather than observable, one eval assertion, and an honest look at whether the rung indicators are consumed.
