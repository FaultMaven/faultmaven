# Owner decision brief — 2026-09-01

Decisions that are **mine to raise and yours to make**, with the facts needed
to decide. Status key: ⬜ open · ✅ decided · ⚫ closed by events

---

## D1 ⬜ fm#1293 — the grounding gate cannot be fixed with corpus rarity; what disposition?

**Your precondition, answered first.** Corpus document frequencies ARE
reachable at runtime. `KnowledgeVectorStore._rerank` holds a `CorpusTermStats`
(built once per collection over the global tier, `document_frequency` /
`idf` / `is_identifier`) at the exact point it computes
`identity_terms_in_query`. It is `None` only on a term-index outage, an empty
global tier, or a collection over `MAX_TERM_INDEX_CHUNKS`. So the carrier
exists — the fix was buildable.

**It was measured before being built, and the premise fails.** Over the 113-pair
fixture (live stats from the same 1297-chunk corpus; every value recomputed by
production code):

| rule on the matched title word | correct admissions kept | labelled-negative admissions kept |
|---|---|---|
| any term matches (shipped) | 7/7 | 16/16 |
| term is corpus-rare (`df ≤ 2%`, the runtime `is_identifier`) | **3/7** | 3/16 |
| term unique to this runbook's title | 3/7 | 5/16 |
| term in ≤2 runbook titles | 4/7 | 7/16 |
| query names the runbook's `service` | 4/7 | 1/16 |
| ≥2 matched terms | 4/7 | 1/16 |
| term is the query's rarest word | 0/7 | 3/16 |
| query carries ≥1 corpus-rare word (the issue's declined precondition) | 6/7 | 8/16 |

The inversion is not marginal. The wrong admissions that actually **seed** — all
three are *Grafana Dashboard Loading Slowly* — ride on `dashboard` at df 20 of
1297, an identifier by the corpus's own test. The correct seeds ride on `disk`
(99), `connection` (188), `403`/`denied` (69/59). Every rarity carrier keeps
the residue and drops the correct seeds.

**Live seeding outcome (repaired e2e, 24 statements, guard on):** 16/16
on-domain seed; 3/8 content-free statements seed junk, and all 3 are the one
Grafana runbook on the one word. The issue's declined precondition, re-run on
this: loses the disk seed, saves **none** of the three (every Grafana query
carries rare words, `dashboard` among them). Pure cost.

**Population check:** 0 of the 214 real `case.description` rows in the local
DB mention a dashboard. At seeding time `case.description` is already the
LLM's confirmed problem statement, not the raw user text. Live exposure of the
Grafana residue is therefore *unmeasured*, not demonstrated. Local-box data
only — not the cloud population.

**What I cannot determine:** whether the cloud population ever phrases a
confirmed problem statement around the monitoring instrument. That needs the
cloud `cases` table, which I did not query.

**Options**

- **(A) Close fm#1293 as measured: no lexical carrier separates; residue is
  semantic and bounded.** Keep the gate as is. The bound is now pinned in
  `test_kb_seed_grounding_reachability_1285.py` (new class
  `TestRarityOfTheMatchedTermDoesNotOrderTheAdmissions`) and in the
  `_identity_terms_in_query` docstring, so it is not re-proposed. Proceed to
  fm#1295's counter reading with the under-report quantified: on the fixture
  the names arm admits 16/51 labelled-negative pairs (8 of 12 queries); on the
  24 statements, 3/8 content-free ones seed, one runbook.
- **(B) Ship the query-level precondition anyway.** Measured as pure cost on
  the live seeding outcome (see above). Not recommended.
- **(C) Leave open awaiting a purpose-built labelled set** (the issue's own
  "what would settle it"). Defensible, but the set would be built to
  adjudicate a residue that on the current data is one runbook on one word.

**Recommendation: (A).** The issue was right that the arm admits on one word
and right that it needed measuring; the direction it and I both assumed
(rarity) is refuted by the data at admission AND at seeding. Closing it with the
measurement on record shrinks the backlog honestly; keeping it open buys a
labelled set for a bounded residue.

**Shipped alongside (PR on branch
`fix/1293-grounding-rarity-refuted-and-e2e-repair`):** the eval's `e2e` mode
has been silently broken since #1282 — its knowledge-service stub did not accept
`use_hybrid`/`min_score`, every prefetch raised, the wrapper swallowed it, and
the mode printed `0/16 on-domain, 0/8 junk` on BOTH arms — a false clean. Fixed
to honour hybrid retrieval and carry the grounding fields.

---

## D2 ⬜ fm#1295 — shape of the work (needs your "go" on the sequence, not on code yet)

**What the seeder touches, read from code (2026-09-02).** It is ONE writer into
the engine's causal graph, not the graph. `seed_candidate_causes` writes
candidate nodes, hypotheses (prior 0.3) and rung evidence-needs through the same
duck-typed writers the LLM path uses. The graph, chain validation, cause
assurance, `RootCauseConclusion`, milestones and case→runbook conversion have no
dependency on it. Reverse reads that DO depend on seeded provenance:

| reader | where | what is lost without seeds |
|---|---|---|
| `confirmed_cause_interventions` | `context_builder.py:2874` | runbook interventions pushed into remediation as `<candidate_solutions>` when the confirmed root was a seed |
| `confirmed_root_seed_origin` | `milestone_engine.py:3543`, `:4531` | the cheap "you applied runbook X, don't generate a duplicate" short-circuit; the ≥70% embedding dedup below it remains |
| `case_has_seeded_candidates` | `templates.py:3362` | the seeded variant of the KB-matched-cause prompt block |
| `and_group` seeder branch | `causal_graph.py:2168` | a coercion path only seeder specs reach |
| `metadata["causes"]` extraction + `cause_letters` stamps | ingestion, `knowledge_service`, `runbook_cause_extractor` | no other runtime reader — becomes dead |

**Not restructuring.** Runbook `## Causes` content, ingestion chunking per Cause,
the engine's causal chain model and the conversion output are all unchanged.
The one thing that needs REPLACING, not deleting, is the remediation channel in
row 1: corrective patterns must still reach the model after the root is
established, via the existing `root_cause` prefetch (prose) and the `kb_qa`
tools. That has to be verified to fire, not assumed.

**Proposed sequence**

1. Merge PR #1296 (reviewer fixes pending on 9 approved points).
2. **Measure before removing.** The seeder has a kill switch
   (`FAULTMAVEN_KB_CAUSE_SEEDER`). Run the sim on the same case set with it on
   and off: correct-root rate, turns to conclusion, anti-anchoring retirements,
   evidence-need provenance. Plus the cloud yield from
   `kb_cause_seed_attempt_total{outcome}` with the #1293 under-report caveat.
   ⚠ needs your choice of sim account before any run.
3. Decision gate on 2: off ≥ on ⇒ proceed; otherwise #1295 rescopes.
4. Removal, in two PRs: (a) default off + remediation-channel verification;
   (b) delete the seeder, its 3 provenance readers, prompt variant, R8 mint,
   6 metrics, settings flag, `and_group` branch, cause-record extraction and
   stamps; archive `kb-cause-seeder.md`; rewrite conversion §6.6 flywheel claim
   and the R8 section of `evidence-needs-design.md`. Doc + code same PR.
5. Then fm#1116 (reasoning power) against the post-removal baseline.

**Recommendation:** approve the sequence; step 2 is the real decision input and
is cheap because the flag exists.

---

## D3 ⬜ fm#1295 step 2 — the A/B run: venue, account, budget (2026-09-02)

**Yield, read from cluster Prometheus (60-day window, interim-prod):**

| counter | 60d | 7d |
|---|---|---|
| `kb_cause_seed_attempt_total{outcome="seeded"}` | 8 | 1 |
| `kb_cause_seed_ungrounded_total` | 3 | — |
| `kb_cause_seed_uncorroborated_total` | 12 | — |
| `kb_cause_seed_grounding_unmeasured_total` | 0 | — |

The push path seeds roughly once a week in production. I could not read the
case-count denominator: the API pod's DB role is behind row-level security
(`count(*)` returns 0) and the statistics-table read was refused by the
classifier. Seeder flag in the pod: **unset ⇒ default ON**. Deployed image
`sha-18d85e4` = current main.

**Venue.** Two choices:

- **(a) Local API on this box, both arms** (recommended). `scripts/faultmaven-dev.sh`
  with `CHAT_PROVIDER=openai` / `OPENAI_MODEL=gpt-5.6-luna` to match the pod
  (local `.env` currently says gemini), the local KB is the shipped 1297-chunk
  pack, `AUTH_MODE=local` (sim logs in as local user `admin` via
  `FM_SIM_USERNAME`, no refresh-token chain, no human account touched). Off-arm
  = `FAULTMAVEN_KB_CAUSE_SEEDER=false` on the local process only. No prod config
  changes. Cases land in the local SQLite DB, nowhere visible.
- **(b) Interim-prod via a guest/cws refresh token.** The off-arm requires
  flipping the seeder flag on the production deployment between arms — a live
  config change on the box serving `api.faultmaven.ai`, and cases land in a
  human account.

**Scenario set.** The 60 grounded scenarios were authored from the pack, so the
seeder fires on most of them; the A/B is meaningful across the board. Proposed:
12 scenarios spanning the 8 domains, 2 reps each (UNRESOLVED@15 is known
variance), 2 arms ⇒ **48 runs, ~8-10 min each ⇒ ~7 h sequential**, LLM spend on
both the engine (gpt-5.6-luna) and the sim persona/judge (Fireworks DeepSeek).
A cheaper first cut: 6 scenarios × 1 rep × 2 arms = 12 runs, ~2 h, then extend
only if the arms differ.

**Instrument per run:** sim `--findings --ground-truth` (GT score, coverage,
RC turn, RESOLVED@N); engine side from the local DB: `causal_nodes` metadata
`seeded_from_runbook` (did the seed fire, was it the root), hypotheses retired
by anti-anchoring, `case_messages.turn_number` accounting. Compare with
`fm-sre-sim --compare`.

**Needs you (both binding, neither assumable):**
1. Venue (a) or (b); if (b), WHICH account.
2. Budget: 12-run first cut, or the full 48.

---

## D4 ⬜ fm#1295 step 3 — the decision gate, with the 12-run A/B in hand (2026-09-02 08:18 UTC)

Local API on this box, main's engine code, gpt-5.6-luna, shipped KB pack, sim
persona/judge = Fireworks DeepSeek (judge unchanged across arms). 6 scenarios × 2
arms, 1 repetition; 5 sim-side persona timeouts were rerun at a 300 s ceiling.
Run logs + findings: `fm-sre-simulator/output/ab-{on,off}-<scenario>-{0324,0656,0712}*`.

| scenario | ON (seeder) | OFF | seeded runbook (ON) | anti-anchor retirements ON / OFF |
|---|---|---|---|---|
| aws-iam-role-assumption | RESOLVED@16, GT .90 | **RESOLVED@12**, GT .90 | EKS Cluster Auth (off-domain) | 2 / 0 |
| redis-oom | UNRES@20, GT .60, cov 0 | **RESOLVED@13**, GT .80 | Redis OOM (correct; root stayed `candidate`) | 5 / 1 |
| linux-disk-full | UNRES@20, GT .90, rc@8 | **RESOLVED@11**, GT .95, rc@7 | *nothing seeded* (gate silent) | 2 / 0 |
| nginx-502 | UNRES@20, GT .60 | UNRES@20, GT .80, rc@20 | NGINX 502 (correct; root `inconclusive`) | 2 / 2 |
| kafka-consumer-lag | UNRES@20, **GT .95**, RCC written | UNRES@20, GT .40 | Kafka Consumer Lag (correct; seeded root `validated`) | 3 / 3 |
| grafana-dashboard-slow | **RESOLVED@13**, GT .95, cov .75 | UNRES@20, GT .95, rc@5 | Grafana Dashboard (correct; root `inconclusive`) | 1 / 0 |
| **totals** | resolved 2/6 · rc identified 3/6 · mean GT .82 · mean turns 18.2 | resolved 3/6 · rc identified 5/6 · mean GT .80 · mean turns 16.0 | 5/6 cases seeded | 15 / 7 |

**Reading.**
- The pre-registered gate ("off ≥ on ⇒ proceed") is **met**: OFF resolves more
  (3 vs 2), identifies the root more often (5 vs 3), in fewer turns (16 vs 18),
  at the same judge score (.80 vs .82).
- Mechanism matches the earlier evidence: in 3 of the 5 seeded cases the seeded
  root never left `candidate`/`inconclusive`, and the ON arm carried 26
  hypotheses with 15 anti-anchoring retirements against 16 / 7 OFF. Redis is the
  clearest harm case: the CORRECT runbook was seeded and the case still ran 20
  turns unresolved through 5 retirements, while OFF resolved in 13.
- Kafka is the one pair favouring the seeder (.95 vs .40) — the seeded root was
  the one case where a seed became `validated`.
- ⚠ **Variance floor.** The disk pair had NO seed on either side (the names gate
  stayed silent on the sim's phrasing), so it is a same-config repeat: UNRES@20 vs
  RESOLVED@11. That spread is as large as any ON-vs-OFF difference above. One
  repetition cannot separate seeder effect from run-to-run noise at the pair level;
  only the aggregate direction is informative, and it points to "not worse off".
- Gap: the local `/metrics` route 404s, so the per-arm counter snapshot is empty;
  seeding was verified from the API log and the DB (`seeded_from_runbook`).

**Options**
- **(A) Proceed to step 4a now**: flip `FAULTMAVEN_KB_CAUSE_SEEDER` default to
  off + verify the remediation channel, on the strength of the gate being met and
  the mechanism evidence. Removal PR (4b) after a soak.
- **(B) One more repetition first** (6 scenarios × 2 arms, ~2.5 h with the 300 s
  persona ceiling) to get 2 reps/pair before touching a production default.
- **(C) Full 48-run design.**

**Recommendation: (B), then (A).** The gate is met, but the disk pair shows
same-config variance spans the whole observed range, and flipping a production
default on n=6 with 1 rep would be a decision under noise. A second rep costs a
few hours and no code; if it agrees, (A) follows with a measured claim.

### D4 addendum — turn-by-turn mechanism (2026-09-02 09:0x UTC), supersedes the recommendation above

Reconstructed from the engine's own rows (`case_messages`, `hypotheses`,
`causal_nodes`, `evidence_needs`) for the redis, kafka and grafana pairs, plus
the seeded-need and validated-root audit for every ON case.

| pair | where the correct cause came from | turn found ON / OFF | what the seeds did | why the outcomes differed |
|---|---|---|---|---|
| redis-oom | the pasted key-size rollup (`rec:features:user:*` 9.59 GB) | **5 / 7** | 3 seeded chains (maxmemory unset, client buffers, fragmentation) retired at t9 "never linked to evidence"; 0 of 9 seeded rung needs ever surfaced | persona: ON executed a rollback at t7 and the case spent t8–t20 in treatment waiting for a post-fix reading the persona cannot produce; OFF never executed and closed at t13 |
| grafana | the pasted Prometheus journal (OOM-killed, restart counter 7) | **4 / 4** | correct runbook seeded; 3 chains never validated; 1 rung need surfaced | persona: ON asked to escalate and closed at t13; OFF looped on an unrecoverable verification window |
| kafka | **neither arm reached it** (scenario root = dropped `idx_orders_customer_id`); both found the mechanical cause (poll-interval evictions) at t4 | 4 / 4 (mechanical) | seeded root *"M members < N partitions"* was **validated at t6** on a literally-true indicator (3 members, 6 partitions) that the seeded rung need surfaced at t4–5, and **became the recorded `root_cause_conclusion`**; ON never explored upstream. OFF found the dropped index at t16 (judge .40, closer to truth than ON's .95) | the "seeder win" is a judge artefact: the judge scored the mechanical cause, its own aggregate text says ON "failed to investigate the upstream root cause" |
| iam | pasted OIDC provider listing | 6 / 5 | seeded (off-domain EKS runbook) root text matched the real answer and validated; its rung need surfaced and was fulfilled | resolved @16 vs @12 — no speed gain from a correct seed |

**What this establishes.**
1. In every examined pair the diagnosis came from evidence the persona pasted,
   at the same turn in both arms (or one turn earlier ON). Seeds did not shorten
   the path to the cause in any case.
2. Seeds are not inert: rung needs surfaced to the persona in 4 of 5 ON cases,
   and in kafka a seeded chain validated on a literally-true, non-causal
   indicator and became the engine's recorded conclusion while the true root
   went unexplored. That is the fm#1144 failure mode, reproduced with the
   CORRECT runbook seeded.
3. The ON-vs-OFF totals are dominated by the treatment-phase verification loop
   (persona cannot produce post-fix data under `--no-intervention`), which is
   orthogonal to seeding and is the fm#1139 / #1122 territory.

**Revised recommendation: (A) proceed to step 4a now.** Under a symmetric
choice the gate is met on the totals, and the mechanism evidence says the
seeder's only observable effect on diagnosis was one wrong recorded conclusion.
A second repetition would re-measure the treatment-loop variance, not the
seeder.

**D4 status 2026-09-02:** owner chose (A). Step 4a shipped as PR #1302 from an isolated worktree; awaiting merge. Next: step 4b removal PR.

**D2 status 2026-09-02:** step 4b split in two. Part 1 (engine seeding path) = PR #1307, open. Part 2 (knowledge-side cause-record pipeline) follows; it changes pack-ingest idempotency (`kb_init` compares the persisted causes and chunk-stamp identity), so the global tier re-ingests on the first boot after it — stated up front rather than folded in.
