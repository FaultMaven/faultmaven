# KB Cause Seeder — structural KB → engine cohesion

Every shipped runbook carries a machine-readable causal-graph record at
`knowledge_items.metadata["causes"]` (produced by the KB pack builder, persisted
verbatim by ingestion). The investigation engine speaks the *same* causal-graph
grammar — a hypothesis is a `root → … → D` chain over the case's single causal
DAG. The **KB cause seeder** closes the gap between them: when KB retrieval
surfaces a runbook whose causes align with the current case, the engine
instantiates that runbook's Cause chains directly as **CANDIDATE** nodes, edges,
and hypotheses in the case graph — instead of the LLM re-deriving one flat
hypothesis from retrieved prose.

This makes the `metadata["causes"]` investment pay off and removes a lossy
double-synthesis (`kb_qa` prose synthesis → engine prose re-summary). It is a
**prior, not a gate**: a seeded cause is a strong hypothesis to test, never a
grounded conclusion. It is explicitly *not* the retired runbook-cause matcher
(that was a deterministic grounding/validation arm, NO-GO'd in #658); the seeder
grants **zero evidentiary privilege** — a seeded candidate is validated only by
real case evidence, and is subject to the same confidence decay, anchoring
detection, and failed-fix demotion as a self-generated hypothesis (both an
engaged-but-unsupported seed and an ignored one decay across turns — an ignored
seed via the housekeeping loop's age-based stagnation sweep, #713 — see the
Guarantee section).

On by default, behind `FAULTMAVEN_KB_CAUSE_SEEDER` (kill switch — set `false` to
disable without a rollback). The flag turned on after the enabling eval cleared
its soundness gate on the hardest provider (see "Enabling gate" below); it is
retained as the kill switch and as the tested flag-OFF no-op path, and is removed
only as the final adoption step.

---

## Where it runs

The seeder is **engine-driven and deterministic** — no LLM call. It hooks the
single moment the engine already retrieves KB for a fresh symptom: the
symptom-verified transition into INVESTIGATING (`_transition_to_investigating` →
`_prefetch_kb_context`, `milestone_engine.py`). This is exactly where the
AUTHORITY prompt already says KB search happens "ONCE at the start of Zone 2,
before forming hypotheses independently." Seeding here gives the LLM structured
priors to test from the first diagnosis turn.

> Rejected alternative — intercept the `kb_qa` tool output. The tool collapses
> retrieval to a prose blob plus a `Sources:` title line, discarding both the
> causal structure and the runbook id. Reconstructing structure from that prose
> is the exact double-synthesis the seeder exists to remove.

### Boundary — one-shot, keyed on the *confirmed* problem statement

Seeding fires **exactly once**, at the INQUIRY→INVESTIGATING transition. It does
**not** re-seed on later turns (the `cause_state → IDENTIFIED` prefetch warms KB
context but does not seed).

Crucially, it keys on the **confirmed problem statement at symptom-verification
time**, *not* the raw first message: the transition sets
`case.description = case.inquiry.proposed_problem_statement`
(`milestone_engine.py`) — the statement the LLM refines across the INQUIRY phase —
and *then* prefetches KB on it. So **all INQUIRY-phase symptom scoping is
captured.** A case that starts vague and is narrowed to a specific symptom during
INQUIRY seeds the *narrowed* runbook, because the engine stays in INQUIRY until
the symptom is scoped enough to confirm. (Measured — see below.)

Residual gap (real but narrow): a runbook that becomes retrievable **only** from
evidence gathered *after* symptom-verification (during INVESTIGATING) is not
seeded — it is consumed via the flat-prose AUTHORITY path. INQUIRY-phase
clarification does not hit this; only mid-investigation discovery does.

This one-shot boundary is a deliberate **"seed a starting differential once the
symptom is scoped, don't re-anchor mid-investigation"** choice — re-seeding after
the LLM has committed to a line of reasoning carries a real anchoring cost. A
*guarded* re-seed hook (seed a newly-emerged runbook only while the graph is still
thin / no cause IDENTIFIED) is a possible follow-on; its value is gated on the
size of the residual gap.

**Measured (flag-ON, fireworks):** a case opened with a vague "platform degraded,
services erroring" statement stayed in INQUIRY through generic prompts, then —
once clarified to an ArgoCD sync failure during INQUIRY — transitioned and
**seeded the correct ArgoCD runbook** (`kb_c350de1303f6`). So the feared "right
runbook emerges after scoping → never seeds" case **does not occur for
INQUIRY-phase scoping**; the residual is confined to post-verification discovery,
which is narrower than a raw-first-message boundary would be.

## Data flow

```text
symptom_verified ─► _transition_to_investigating
                     └─► _prefetch_kb_context          (existing: search_knowledge)
                          └─► seed_candidate_causes_from_kb(case, hits, kb_item_repo, turn)   ◄── NEW, flag-gated
                               1. ensure D            seed_problem_node(case)
                               2. pick runbooks       distinct parent_document_id, top-N by score
                               3. load causes         kb_item_repo.get_by_id(id).metadata["causes"]
                               4. select causes       skip fallback; retrieval/author order; cap MAX_SEEDED_CAUSES
                               5. instantiate         ingest_emitted_chain(...)  +  create_hypothesis(...)
```

### 1. Source identity

The seeder needs the matched runbook's id to load its causes. Today that identity
is lost: chunk metadata carries `parent_document_id` (== the `knowledge_items`
row id holding `metadata["causes"]`), but `SearchResult` mislabels the *chunk* id
as `document_id` (a `result.get("id")` fallback) and the engine keeps no id at
all.

Fix: add `parent_document_id` to `SearchResult`, populate it from
`metadata["parent_document_id"]` in `KnowledgeService.search_knowledge`, and
preserve it on the `case.kb_context` entries the prefetch stores. This is a plain
correctness fix (it also stops the chunk-id-as-document-id mislabel) and runs
regardless of the seeder flag.

### 1a. Retrieval scope and trust boundary

The seeder can only seed from runbooks the prefetch surfaces, so two constraints
live at the retrieval seam.

**Ranking is plain retrieval score.** The prefetch runs
`KnowledgeService.search_knowledge` → `KnowledgeVectorStore.search`, a single-pass
pure-vector search (cosine similarity, `score = 1.0 − distance / 2`) — **no reranker
and no service-metadata boost**. The case-derived service signal
(`context_metadata` → `hybrid_search(filter_mode="soft")` → the reranker's
`_compute_metadata_score`) is wired only through the agent QA tools path
(`KBToolAdapter` → `DocumentQATool` → `hybrid_search`), which the seeder does not
use. So the runbooks the seeder ranks and picks are ordered by plain retrieval
score, with the affected service exerting no boost. Threading the metadata signal
onto the prefetch/seeder path is a possible future refinement, tracked in
tech-debt issue #710 (reranker service-signal refinements); it is not wired today.

The two scope/trust constraints:

- **Owner-aware scope.** `_prefetch_kb_context` searches **`global` ∪ the case
  owner's own `personal` KB** (keyed on `case.user_id`), not global-only. This
  completes the flywheel loop — a user's resolved cases, converted to
  personal-scoped runbooks, seed that user's *own* future investigations — while
  preserving strict cross-user isolation: the personal condition is keyed on the
  owner's `user_id`, so user B's case can never surface user A's personal
  runbooks. (Team-scoped KB is a deliberate inert seam: org/team collaboration is
  Cloud-only and no team service is wired anywhere today, and case→runbook
  conversion emits only `personal`; when team lands, the owner's team scopes OR
  into the same filter, mirroring `KnowledgeService.search_documents`.)
- **Trust tier.** `KnowledgeService.get_runbook_causes` refuses the causes record
  of any `EXPERIMENTAL`-tier item, so the seeder never consumes unverified
  knowledge. The produce side already extracts causes only at the
  human-verification gate (`verify_draft` ingests as `COMMUNITY`; the anonymous
  `upload_document` path never extracts); this loader check makes it a *runtime*
  invariant regardless of how the item was written. Pack runbooks and verified
  drafts are `COMMUNITY`; only the anonymous upload tier is `EXPERIMENTAL`.

### 2–3. Runbook and cause selection (multi-runbook merge rule)

Retrieval routinely returns several runbooks and each runbook has many Causes.
Left unbounded, seeding would flood the graph and trip anchoring detection (≥4
active hypotheses in one category reads as fixation). The bounds:

- **Cap runbooks:** dedup hits by `parent_document_id`, take the top-N distinct
  runbooks by retrieval score (`MAX_SEEDED_RUNBOOKS`, default 2). Retrieval has
  already done the semantic alignment at runbook granularity. Retrieval results are
  **chunk-level**, so a single long runbook can occupy several of the top-ranked
  slots and starve the parent-runbook dedup of a second distinct runbook. The
  prefetch therefore fetches deeper than the prompt surface —
  `KB_PREFETCH_FETCH_LIMIT` (10) chunks feed the seeder's parent-dedup, while only
  the top `KB_CONTEXT_MAX_ENTRIES` (3) are rendered into `case.kb_context`
  (`milestone_engine._prefetch_kb_context`). Because results are score-ranked, the
  rendered top-3 slice is byte-identical to the prior limit-3 fetch, so the LLM's
  prompt surface is unchanged; only the seeder sees the deeper list.
- **Skip fallback causes:** a `is_fallback_cause: true` Cause (`### Cause Z:
  Unidentified`) has an empty chain — nothing to instantiate.
- **Order causes by the ranking that already exists — no bespoke scorer.** Each
  `### Cause` is its own retrieval chunk, so retrieval already ranked the causes;
  where a hit maps back to a specific Cause, seed in that score order. Otherwise
  fall back to the runbook author's own cause order (causes are authored
  most-likely-first). A second token-overlap pass re-scoring causes against the
  symptom would be a weaker re-match of what retrieval already ranked — precisely
  the matcher-shaped code the #658 NO-GO retired — and would re-import a matcher's
  eval burden. If per-case cause relevance ever needs improving, the home for it
  is the retrieval ranker, not the seeder.
- **Cap total seeded causes:** across all runbooks, seed at most
  `MAX_SEEDED_CAUSES`. This is **derived from** the anchoring condition-1
  threshold (`< N_same_category`), not a hardcoded 3, so a future change to the
  anchoring threshold cannot silently let the seeder self-anchor — the
  relationship is asserted in a test.
- **Dedup across runbooks:** the same cause retrieved via two runbooks seeds
  once. The check runs **before** `ingest_emitted_chain`, via the shared dedup key
  `find_canonical_node_id` (the same exact-normalized `(node_type, statement)` key
  ingest reuses on): if a runbook's root would collapse onto a root that already
  heads a hypothesis, the cause is skipped `benign_dedup` *without first minting*
  its chain. This matters when a second runbook shares a root but diverges
  mid-chain — deciding the dedup after ingest would leave the divergent
  intermediate rungs as orphan nodes/edges (on no hypothesis path, invisible to
  the skip taxonomy). A **second, paraphrase** dedup runs in the same pre-ingest
  block: the hypothesis statement the seed would create is passed to the INV-36
  predicate `find_duplicate_hypothesis` (`causal_graph.py`) — the *same*
  mutual-Jaccard + polarity-guard + numeric-discriminator-guard predicate already
  applied to the LLM's `hypotheses_to_add` — and a hit against a **chain-heading**
  standing hypothesis is skipped `benign_dedup` ("duplicates standing hypothesis …
  (paraphrase)"). This stops two runbooks that describe one cause in different
  words from co-seeding two paraphrase OR-siblings, which would inflate the
  differential and spuriously raise the `validate_by_exclusion` bar (exclusion
  needs ≥2 siblings counterfactually refuted). It is sound because it applies the
  same predicate and reaches the same dedup *decision* as the INV-36 path that
  would have deduped the statement had the LLM emitted it — the difference is the
  reconciliation: INV-36 surfaces the matched id so the LLM *updates* the standing
  hypothesis, whereas the seeder path is a silent skip (there is no emission to
  merge). The fail-open guards keep genuinely distinct siblings (a negated
  restatement, or one differing only by a number) separate — no bespoke scorer is
  introduced. The check is **scoped to chain-heading hypotheses** (`root_node_id`
  set), the same scope as the exact-root check: a chain-less standing hypothesis
  must never paraphrase-suppress a structurally-rich runbook cause (that would
  silently discard its chain, rung-indicator evidence-needs, and interventions),
  so if both a chain-less match and a chain-heading paraphrase exist the
  duplicate-sibling cost is preferred over the silent structural loss, left for the
  LLM to reconcile. Near-duplicate roots below the paraphrase bar are reconciled by
  the existing MECE arbitration (`distinct_cause_clusters`, Jaccard 0.6).
- **Distinct roots compete as OR-alternatives:** pack `chain_edges` carry no
  `and_group`, so seeded predecessors enter as independent OR-alternative sibling
  causes — never silently merged into one Cause. Evidence separates them. A cause
  whose shape the seeder does not model — `and_group` co-necessary AND-convergence,
  or a non-linear chain (a second root, a branching fork, a convergence/join, a
  dangling edge ref, or a cycle/fragment/non-`D`-terminating chain) — is
  **rejected**, not flattened/mis-seeded — see the `unsupported_shape` skip below.
  A cause carrying the grammar's cross-chain `converges: <Cause>.<ref>` directive
  is likewise rejected, but under its own `converges_unmodeled` class — the
  convergence is legal, well-authored grammar, so it must not trip the quality
  alarm (see below).

### 4–5. Instantiation

For each selected Cause the seeder reuses the engine's own graph constructors —
it does not open a second write path:

1. `seed_problem_node(case)` — idempotently ensures the single PROBLEM node `D`
   from the verified symptom. The Cause's `problem` rung maps to this existing
   `D`; it is never re-created (`ingest_emitted_chain` rejects PROBLEM specs).
2. `ingest_emitted_chain(case, node_specs, edge_specs, node_evidence=[], turn)` —
   the sole causal-graph builder. `chain_nodes` (root/intermediate) become node
   specs; `chain_edges` become the `produces` wiring (`root → s1 → … → D`).
   Nodes are minted as `NodeState.CANDIDATE`, `validation_method=NONE`.
3. `HypothesisManager.create_hypothesis(statement=cause_statement, category=OTHER,
   initial_likelihood=…, current_turn=turn, generation_mode=OPPORTUNISTIC,
   state=ACTIVE, rationale="Seeded from runbook <item_id> (Cause <letter>:
   <name>)")` — then resolve `root_node_id` + `path` to the seeded root and `D`,
   and insert into `case.hypotheses`.

`create_hypothesis` caps the prior at `NEW_HYPOTHESIS_MAX_PRIOR = 0.5`, so a
runbook asserting high certainty still enters at ≤ 0.5 — the same ceiling as a
self-generated hypothesis. The climb past the IDENTIFIED gate is earned only by
linked case evidence or chain validation.

**Provenance.** No first-class "seeded" field exists on `CausalNode` (the
`causal_node_evidence.provenance` column was dropped in migration 024 with the
#658 matcher NO-GO, so KB origin carries no runtime privilege by construction).
The seeder records origin in two read surfaces only: the hypothesis `rationale`
(rendered into prompt context and observability) and
`CausalNode.metadata["seeded_from_runbook"]` (the runbook `item_id`, for tests
and logging). Neither grants any evidentiary weight.

**Category.** A Cause record carries no `HypothesisCategory`; there is no reliable
signal to derive one, so seeded hypotheses default to `OTHER`. The
`MAX_SEEDED_CAUSES = 3` cap keeps this from tripping anchoring condition 1 on
its own.

### Observable skip — no silent drop

A matched runbook that seeds nothing must never be *invisible*. Every non-seed is
recorded as a class-tagged `SkippedCause` on the `SeedReport` (keyed by
`(item_id, cause_letter)`):

The classes split into two families — **expected non-seeding** (`intentional`,
`benign_dedup`, `converges_unmodeled`), which are normal outcomes on a
well-authored runbook and are never alarmed, and **actionable** (`quality_drop`,
`unsupported_shape`), a real cause that should have seeded but did not:

- **`intentional`** — the fallback (`Z`/`[Default]`) cause; never a candidate root
  by design.
- **`benign_dedup`** — a cause already represented: either a root already seeded by
  an earlier retrieved runbook (exact-normalized overlap), or a *paraphrase* of a
  standing hypothesis caught by the INV-36 `find_duplicate_hypothesis` predicate.
  Normal and correct. Both are decided **before** ingest (see *Dedup across
  runbooks* above), so neither mints orphan nodes.
- **`converges_unmodeled`** — a cause carrying the v4 grammar's cross-chain
  `converges: <Cause>.<ref>` directive. Both producers emit it as a chain edge with
  a truthy `converges` key whose `effect_ref` points into *another* Cause's chain,
  so the cause cannot form a self-contained `root → D` path. It is detected
  **before** `_reject_nonlinear_shape` (otherwise the convergence edge would be
  misdiagnosed as a dangling ref and mis-classed `unsupported_shape`) and the whole
  cause is **rejected** — never partially seeded or flattened. Because a
  `converges:` directive is legal, well-authored grammar (the sole cross-chain
  construct), this is *expected* non-seeding, **not** a quality drop, so it does not
  trip the "contributed nothing" alarm. Modeling it is future work. **0/640** in
  the shipped pack; produce-path / future-authoring protection.
- **`quality_drop`** — a *real* cause the seeder could not instantiate (no chain,
  non-root head, bad `node_type`, empty statement, ingest produced nothing).
- **`unsupported_shape`** — a well-formed cause using a structure the seeder does
  not yet model, **rejected not flattened** (honor-or-reject). Two families:
  - `and_group` **AND-convergence**: the seeder reads `cause_ref`/`effect_ref` and
    defaults every edge to `and_group=None`, so a co-necessary AND-set would be
    silently flattened to independent OR-alternatives (A∧B → A∨B — a MECE
    mis-model).
  - **non-linear chain** — a *second root* mid-chain (two chains, not one linear
    path); a *branching fork* (a rung with more than one outgoing edge, which
    `produces_by_ref`'s last-edge-wins would flatten to one arbitrary branch); a
    *convergence/join* (a rung produced by more than one cause — a repeated
    `effect_ref` without an `and_group` — which is a merge, not a link in a single
    path); a *dangling edge ref* (a `cause_ref`/`effect_ref` resolving to no
    node, silently disconnecting a rung); or a *cycle / fragment /
    non-`D`-terminating / inverted* chain (edges that satisfy the ≤once checks yet
    never form a single path from the root to `D`). The guard requires a single
    linear `root → … → D` chain and enforces it in two parts: (1) each `cause_ref`
    and each *canonical* `effect_ref` appears at most once — `"D"` and every
    problem-node ref denote the one case `D` node, so they are canonicalized
    before the merge check, and a join onto `D` via two different literals is
    still rejected; (2) a reachability walk from the head root must traverse every
    rung exactly once and terminate at `D` — catching cycles, disconnected
    fragments, and chains that never reach `D`, which the ≤once checks alone
    cannot see.

  All are **0/640** in the shipped pack; the guard exists for the case→runbook
  conversion (produce) path, where LLM-authored chains are far likelier to branch
  than the curated corpus — so a shape gap cannot go live the day the flywheel
  closes.

The **"matched runbook contributed nothing"** warning fires when a zero-seed
runbook has ≥1 *actionable* skip — `quality_drop` **or** `unsupported_shape`
(`runbooks_contributing_nothing()`) — a runbook
whose only skips are the expected non-seeding classes (`benign_dedup`,
`intentional`, or `converges_unmodeled`) is *not* alarmed, so two runbooks sharing
a cause — and a grammar-legal cross-chain convergence — never false-alarm. (A
runbook never entered because the `MAX_SEEDED_CAUSES`
budget was already spent leaves no skip record; that zero is benign — budget, not
quality.) The shipped corpus has **zero** `quality_drop` shapes (all 549
non-fallback causes are multi-rung), so this is observability for future/authoring
drift, not a current defect.

### What the seeder consumes (and what it doesn't)

The seeder reads a cause's `chain_nodes` / `chain_edges` / `cause_statement` /
`cause_letter` / `cause_name` for the chain topology, its `rung_indicators` to
seed per-rung evidence-needs (see *Rung indicators → evidence-needs* below),
**and** its `interventions` — captured at seed time and surfaced as candidate
`Solution` priors once the cause is confirmed (see *Interventions → candidate
solutions* below). So the three richest slices of the `metadata["causes"]` record
now drive the FORMULATION (chain), VALIDATION (rung indicators → evidence-needs),
and SOLUTION (interventions → candidate solutions) stages respectively — the
runbook is no longer consumed as pure prose. (The deterministic `<!-- match -->`
predicates stay blocked upstream: the pack ships zero `predicate`/`exit_code`
keys — dropped at pack-build.)

#### Rung indicators → evidence-needs

`rung_indicators` (`dict[rung_ref → list[observable]]`) are a cause's per-rung
checkable signals — the richest slice of the `metadata["causes"]` record and,
before this, structurally write-only (consumed only as prose by the LLM). At seed
time, `_emit_rung_needs` turns each indicator into one **PENDING
`CAUSAL_VERIFICATION`** `EvidenceNeed` in `case.evidence_needs`, motivated by the
seeded hypothesis, so a seeded chain arrives carrying its own discriminators
rather than leaning entirely on the LLM to invent them. The runbook's `[Step N]`
reference prefix is stripped for the user-facing `request_text`; indicators empty
after stripping, and duplicates within a cause, are dropped.

Every property keeps a seeded need a **prior, not a gate** — mechanically
identical to an LLM-emitted need:

| Property | Effect |
|---|---|
| `state = PENDING`, no `fulfilling_evidence_ids` | Never auto-fulfilled — grounds only when a real datum arrives, like any need. |
| `priority = LOW` | Sinks a seeded ask in the rendered `<evidence_needs>` ordering. Not a suppression guarantee — surfacing *selection* is deliberately priority- and origin-blind (ranks by `request_text` rarity + rotation), so a discriminating seeded rung surfaces like any other need. |
| `obtainability = UNKNOWN` (fail-safe) | Never contributes to the declared-data-wall on its own (`verification_status._candidate_unresolvable` walls a candidate only when *all* its discriminators are `UNOBTAINABLE`). It makes the wall *honestly computable* for a seeded candidate — a latent gap before R8, when a seeded hypothesis had zero discriminators — without ever moving a case toward INSUFFICIENT_EVIDENCE. |
| Motivated solely by the seeded hypothesis | Cleared for free by the engine's motivator-based auto-supersession when that hypothesis is retired (evidence-needs-design §7.4) — no bespoke cleanup. |
| Origin only in `rationale` (`SEEDED_RATIONALE_PREFIX`) | Provenance-blind to safety (see below) — nothing branches on it. |

#### Interventions → candidate solutions

`interventions` (`list[{"quadrant","ref","text"}]`) are a cause's fixes, tagged
by intervention quadrant (§7.4: `remediation` / `defensive_fix` / `mitigation` /
`loop_break`). They drive the **SOLUTION** stage the way `rung_indicators` drive
VALIDATION, and by the same **emission-mediated** discipline — the engine renders
them as a *prior* and the LLM emits the solution, rather than the engine minting
solutions itself (which would collide with the existing prose-authored offer path
and strain prior-not-gate):

1. **Seed-time capture.** `_seed_one_cause` stashes the cause's sanitized
   `interventions` onto the seeded **ROOT** node's metadata
   (`SEEDED_INTERVENTIONS_KEY`) — only on a freshly-minted root, never on a reused
   self-generated node (same discipline as the `seeded_from_runbook` stamp). This
   captures them once, avoiding a racy re-fetch of the runbook at SOLUTION time.
2. **Confirmed-cause read.** `confirmed_cause_interventions(case)` returns the
   interventions captured on a **counterfactually-confirmed** root's distinct-cause
   cluster (the same clustering as `confirmed_root_seed_origin`, so a
   validated-then-restated seed still resolves to its interventions), or `[]`.
3. **Render.** `context_builder._build_candidate_solutions_block` surfaces those
   interventions as a `<candidate_solutions>` block **only when a seeded cause is
   confirmed** — quadrant + text, framed as candidate fixes to *propose*, not a
   directive. Empty when the flag is off or no seeded cause is confirmed (since
   interventions are captured only when the seeder runs) — inert like every other
   optional prompt block.
4. **Emission → Solution.** `SolutionToAdd` carries optional `quadrant` /
   `node_ref`; the apply path maps them onto the persisted `Solution.quadrant` /
   `node_id`, **honor-or-reject** (an unrecognized quadrant or a `node_ref` not on
   the graph is dropped to `None`, never a parse crash — BEST_EFFORT-provider-safe).
   The emission deliberately does **not** write a proposed check into
   `Solution.verification_method`: that field means *how the fix WAS verified* (past
   tense — read by the resolution report and the resolution-confirmation gate), so
   populating it at proposal time would claim a verification that never happened and
   suppress the engine's request for real verification. The runbook's verification
   prose still reaches the LLM via RAG.

**M5 unchanged.** The interventions are recorded as **data**; the M5 downgrade
logic (a permanent-fix SOLUTION requires an established cause; mitigation exempt)
is untouched. Per-quadrant M5 precision — the `defensive_fix` exemption the
methodology reserves — is now *unblocked* (the emission finally carries a
quadrant) but deliberately **not taken** here: it is a separate, soundness-sensitive
decision. Everything stays **prior, not gate**: a candidate solution still requires
the user to accept and verify, and a laundered failed-fix is bounded (it surfaces
only for a cause established with *real* evidence, the user must accept, the
runbook's own verification catches it, and M6 demotion remains) — R5's
solution-outcome annotation is the upstream guard.

## Guarantee: no evidentiary privilege, no anchoring

A seeded chain is a *strong prior*, and a strong prior is precisely what can
anchor an LLM toward a wrong theory it then rationalizes — the
NO-COLLAPSE-UNDER-PRESSURE failure mode. The seeder is designed so a seeded prior
is **mechanically indistinguishable** from a self-generated one to every safety
mechanism:

| Mechanism | How the seeded candidate is subject to it |
|---|---|
| Prior cap | `create_hypothesis` clamps to `≤ 0.5` — no head start. |
| Confidence decay (`0.85^iterations`) | Same treatment as a self-generated hypothesis — no special-casing. Two paths advance `iterations_without_progress`, both origin-blind. **Engaged and unsupported:** the counter climbs when the LLM *touches* a hypothesis (via `link_evidence`/a likelihood update that fails to move likelihood ≥5%), so a seed the LLM engages and fails to support decays each turn via the housekeeping loop, exactly like any other stagnant hypothesis. **Ignored:** a seed the LLM *never touches* is aged by the housekeeping loop's stagnation sweep (`advance_stagnation_if_ignored`, `hypothesis_manager.py`) — once it has gone `IGNORED_STAGNATION_TURN_THRESHOLD` turns since its last progress, the counter advances by one per turn, so decay and stagnation-based anchoring act on it just as they would on a repeatedly-tested hypothesis (#713). The sweep is provenance-blind and conservative: it only lowers belief over time (stall/soft-retire) and never validates, refutes, or concludes. Either way the seed stays harmless: candidate-only, evidence-less, provenance-blind → cannot validate. |
| Anchoring detection | Counts and can retire seeded candidates identically (`detect_anchoring` reads `category`/`iterations`/`likelihood`, not origin). |
| Failed-fix demotion (M6) | A disproved seed flows through the same `refute_hypothesis` / counterfactual-demotion path. |
| VALIDATED | Unreachable by the seeder — it never invokes a VALIDATED writer. Node VALIDATED is written by `derive_node_states` (empirical) **and** `validate_by_exclusion` (deductive — the #593 exclusion arm stamps `DEDUCTIVE` on a ROOT once ≥2 siblings are counterfactually refuted); hypothesis VALIDATED is projected from those node states by `project_hypothesis_states_from_roots`. A candidate-only, evidence-less seed at ≤0.5 satisfies none of their preconditions, and Pydantic validators reject a hand-set VALIDATED node lacking a method/actionable flag. |

The seeder writes **candidates only**. It never sets likelihood above the prior
cap, never links evidence, never sets VALIDATED. The **primary** guarantee is
structural, not dynamic: a misleading runbook's seeded cause is a CANDIDATE at
≤0.5 with no evidence links and no runtime privilege (provenance-blind), so it
**cannot reach VALIDATED and cannot be concluded on** regardless of what decay or
anchoring do. Decay and anchoring are the *secondary* backstop and bite only when
the LLM **engages** the seed and fails to support it (then its stagnation counter
climbs and it decays/anchoring-flags/demotes like any other hypothesis); a seed
the LLM simply **ignores** is aged by the housekeeping loop's stagnation sweep so
it too decays and can trip anchoring once it has stagnated (see the decay row
above and #713). In no case does the engine conclude on it.

**Provenance-blindness invariant (load-bearing).** The whole no-privilege claim
rests on *nothing branching on origin*. The seeder records origin in **three** read
surfaces — `node.metadata["seeded_from_runbook"]`, the hypothesis `rationale`
(prefix `"Seeded from runbook …"`), and the R9
`node.metadata["seeded_interventions"]` key (present on a node only if it was
seeded) — read only by observability, the prompt-render path, and tests. This is
enforced by a standing invariant test that greps the safety modules for **all
three** markers, so a mechanism cannot sniff origin out of the rationale string or
the interventions surface either. The checked module set spans
consume-side safety (decay / anchoring / failed-fix demotion / node+hypothesis
state derivation in `causal_graph` + `hypothesis_manager`; `cause_state`
derivation + the per-turn housekeeping loop in `milestone_engine`), the
conclusion/terminal gates a seeded prior must never shortcut (`cause_assurance`,
`terminal_transitions`, `progress_monitor`, `state_validator`,
`working_conclusion_generator`), **and** — since R8 makes a seeded cause emit
evidence-needs — the need-consuming safety paths (`verification_status`, the
declared-data-wall arm; `evidence_need_surfacing`, the render-time view), proving
neither reaches through a need's motivating hypothesis to sniff origin. A
whole-file grep is deliberately coarse so the guard can never be silently narrowed
below where the safety logic actually lives.
The grep also bans the origin **symbol names** themselves (a module could import
the metadata-key constant and branch on it with no literal value in its source),
so the literal-value grep alone is a tripwire, not a proof. A future edit cannot
quietly grant a seed evidentiary weight.

*Explicit carve-out (Phase 5.2b).* Exactly one origin reader —
`confirmed_root_seed_origin` — is permitted in exactly one module —
`milestone_engine` — and banned everywhere else (the invariant test allows the
symbol only there). It backs the runbook-generation **offer** gate, a
knowledge-lifecycle decision: a wrong answer at that gate produces only a missing
or redundant "generate runbook" affordance, never an incorrect conclusion or a
collapse under pressure. Every VALIDATION / decay / anchoring / demotion / state /
gating path — including all the *other* provenance surfaces in `milestone_engine`
itself — stays blind. The carve-out is scoped to one symbol in one module so it
cannot become a general escape hatch. See the "Provenance-based uniqueness"
subsection of `document-to-runbook-conversion.md` for how the gate reads it.

The R9 reader `confirmed_cause_interventions` is likewise banned from every safety
module but needs **no** carve-out: its only reader is the prompt-render path
(`context_builder._build_candidate_solutions_block`), which is not a safety
mechanism (it offers a prior to the LLM, gated by M5 and the user's accept/verify)
and is not in the checked module set — so the bare ban with no exception is exactly
right.

**Honest limit — seed↔LLM anchoring interaction.** The `MAX_SEEDED_CAUSES`
cap stops the seeder *alone* from tripping anchoring condition 1. But seeded
`OTHER`-category causes and any `OTHER`-category hypothesis the LLM generates can
still combine to ≥ the threshold and raise an anchoring flag. This is
conservative-safe — a safety mechanism firing early costs exploration breadth,
never a wrong conclusion — but it is a real seed→LLM interaction, asserted as a
measured eval expectation rather than left as a surprise.

## Prompt alignment

When seeding is active the `KNOWLEDGE & RUNBOOK AUTHORITY` block's flat "Exactly
one Cause matches → create a `hypotheses_to_add` record" directive is
**replaced** (not appended-to) with a validate/refute-the-seeded-candidate
directive, so a seeded turn reads **one coherent instruction**: the candidate
Cause chains are already in the graph, so the model **validates or refutes** them
against evidence (via `hypothesis_evidence_links` / `causal_evidence`) rather than
re-emitting `hypotheses_to_add` from prose. (An earlier design *appended* a
superseding override after the flat directive; that shipped two contradictory
instructions and asked the model to arbitrate "later wins" — replaced here with a
single directive. The flat directive is sliced from the assembled block by stable
anchors so the runtime swap is exact and the block stays the single source of
truth; a drift raises at import.) The replacement preserves the `knowledge_match`
/ `SolutionToAdd` TREATMENT handoff.

The seeded directive frames candidates as **priors to test, not answers**: reject
a seeded cause on contradicting evidence, and — critically — keep forming
independent hypotheses for causes the runbook did not cover. This is the one
place prompt-level behaviour could dent *effectiveness*, in two ways, both
**quality/effectiveness regressions, never soundness breaches** (neither can reach
VALIDATED without evidence):

- **Over-deference** — the LLM confirms a seed instead of testing it.
- **Paraphrase-duplication** — a weaker model ignores the directive and emits a
  `hypotheses_to_add` for a seeded cause with *reworded* text. This is **partly
  backstopped mechanically**: every `hypotheses_to_add` runs through INV-36
  `find_duplicate_hypothesis` (`causal_graph.py`), a **mutual-Jaccard mirror**
  (with polarity + numeric-discriminator guards) against standing hypotheses — and
  the seeded cause *is* a standing ACTIVE hypothesis. A reword **above** the
  duplicate bar is caught: the re-add is dropped, the emission maps onto the
  existing seeded hypothesis, and the model is told to update it
  (`hypotheses_to_update`) rather than clone it. What slips through is only a reword **below** the mutual-Jaccard bar
  (heavier rephrasing) — INV-36 correctly keeps a genuinely more-specific
  refinement distinct, so it cannot dedup an aggressive paraphrase without also
  eating legitimate refinements. Such a slip yields two ACTIVE `OTHER` hypotheses
  for one cause (inflating the `OTHER` bucket toward the anchoring threshold,
  breaking sibling-MECE). (Node dedup is separate and exact — keyed on
  `(node_type, statement)` — and does not catch paraphrases; INV-36 is the
  hypothesis-level backstop that does, above its bar.)

Both are prompt-strength-dependent and weakest on a BEST_EFFORT model
(`deepseek-v4-flash`, the dev/demo default). The correct envelope for the residual
(below-bar) slip is **prompt + per-provider eval**, *not* a new mechanical
semantic-dedup backstop tuned to seeded causes — matching an emitted paraphrase
against the seeded-cause corpus specifically is exactly the retired matcher's
territory (#658 NO-GO); INV-36's general standing-hypothesis dedup is not that and
already applies. So: no-collapse stays mechanical; residual no-duplication stays
prompt-level and is measured per provider (see Verification).

The flat "Exactly one Cause matches → that Cause IS your hypothesis" branch is the
behavior when the flag is off, or in a case that has **never** seeded a candidate.
Note the swap is **case-sticky, not per-match**: `_select_diagnosis_block` applies
the seeded directive whenever `case_has_seeded_candidates(case)` is true — for the
rest of the case's life — not only on the turn a runbook with a `causes` record is
matched. So once a case has seeded any candidate, a *prose-only* source
(a converted draft without a `causes` record) matched **later in the same case**
still receives the seeded "its chain is ALREADY in your graph" directive, which is
technically inaccurate for that prose-only match. This is a **quality/wording**
imprecision only — never a soundness issue (the seeded directive still forbids
confirming on absent evidence, and a prose-matched cause the LLM chooses to add
enters as an ordinary candidate). A per-match directive would need the block
assembled with knowledge of *which* source matched this turn; deferred as a
wording refinement.

## Freshness

The ingestion idempotency gate hashes only the runbook markdown, so a pack change
that edits `causes` while leaving the markdown byte-identical is skipped — the
live consumer would read stale structure. With the seeder as a real runtime
reader this is now load-bearing: the skip branch additionally compares the
persisted `metadata["causes"]` against the pack's causes and re-ingests on
divergence.

## Configuration

| Knob | Kind | Default | Effect |
|---|---|---|---|
| `FAULTMAVEN_KB_CAUSE_SEEDER` (`features.kb_cause_seeder_enabled`) | env flag | `true` | Gates seeder invocation **and** the AUTHORITY prompt override. On by default; set `false` as the kill switch — disables in prod without rollback. |
| `MAX_SEEDED_RUNBOOKS` | module constant (`kb_cause_seeder.py`) | `2` | Distinct runbooks seeded per retrieval, top by score. |
| `MAX_SEEDED_CAUSES` | module constant, **derived** | `ANCHORING_SAME_CATEGORY_THRESHOLD − 1` (= `3`) | Total causes seeded per turn. Derived from the anchoring condition-1 constant (not an env var — deriving then overriding would break the coupling guarantee), asserted `< threshold` in a test. |

The `parent_document_id` surfacing and the causes-freshness comparison
are plain correctness fixes and run regardless of the flag; the KB schema
and the `causes` record are unchanged whether the flag is on or off.

## Verification

Pass/fail is **mechanical engine-state assertions**, LLM-agnostic:

- Given a known runbook + matching evidence, the graph is seeded with the
  expected candidate nodes (root/intermediate), edges wired `root → … → D`, and a
  hypothesis whose `root_node_id`/`path` head at the seeded root — all CANDIDATE,
  likelihood ≤ 0.5, no VALIDATED.
- **Provenance-blindness:** the decay/anchoring/demotion/state-derivation paths
  never reference the seeded-provenance keys (invariant grep/AST test).
- **Cap ↔ anchoring coupling:** `MAX_SEEDED_CAUSES` is strictly below the
  anchoring condition-1 threshold (asserted against the real constant, so the two
  cannot silently drift apart).
- **Misleading runbook:** a wrong seeded prior with no supporting evidence stays a
  CANDIDATE at ≤0.5 and never reaches VALIDATED, so the engine reaches no
  conclusion on it (NO COLLAPSE, NO INCORRECT CONCLUSION) — this holds structurally
  whether or not the LLM engages the seed. When the LLM *does* engage it and fails
  to support it, it additionally decays across turns and is anchoring-flagged/
  demoted; when the LLM ignores it, the housekeeping loop's age-based sweep still
  decays it toward stagnation (see the decay row and #713). The eval also confirms the engine did **not** simply
  stop exploring — the LLM continues generating its own hypotheses (guards the
  seed-crowd-out quality risk).
- **No paraphrase-duplication (per-provider, flag-ON sim only):** after a seeded
  turn, assert **≤ 1 ACTIVE hypothesis per seeded cause** — no second hypothesis
  whose root competes for a seeded root's coverage. This is the property the
  mechanical guarantee cannot cover (it is prompt-strength-dependent), so it is a
  **first-class assertion measured on every provider, including the BEST_EFFORT
  dev/demo model** where the prompt is weakest — not folded into "no collapse."
  This property is **partly backstopped mechanically** by INV-36
  `find_duplicate_hypothesis` (a `hypotheses_to_add` paraphrasing a seeded cause
  *above* the mutual-Jaccard bar is deduped against the standing seeded
  hypothesis), so a flag-ON ≤1-ACTIVE pass rides on INV-36 plus prompt strength,
  not prompt strength alone; only *below-bar* rewords depend on the prompt. No new
  seed-specific semantic-dedup backstop is added (that would be #658 territory);
  INV-36's general standing-hypothesis dedup is not that and already applies.
- **Multi-runbook:** an identical-statement cause seeded via two runbooks dedups
  to one node; two distinct roots enter as competing OR-alternative candidates.
  A second runbook sharing a root but diverging mid-chain dedups **without leaving
  orphan nodes** (skip-before-ingest) — asserted by the orphan-free invariant
  (every non-problem node lies on some hypothesis path).
- **Shape guard (unit):** an `and_group` AND-set, a missing/empty node ref, a
  second-root chain, a branching fork, a convergence/join (including a join onto
  `D` via the `"D"`-vs-problem-ref alias), a dangling edge ref, and a
  cycle/fragment/non-`D`-terminating chain each reject as `unsupported_shape`
  (seed nothing, raise the "contributed nothing" alarm) — never silently
  flattened/linearized; a well-formed linear chain that terminates at `D` via the
  problem ref still seeds.
- **Freshness:** a causes-only pack change re-ingests.
- **Flag off:** the seeder is a no-op; the flat KB-resolution prompt path is
  unchanged.
- **Observable skip (unit):** a fallback → `intentional` skip (no alarm); a
  malformed real cause → `quality_drop` skip **and** the "contributed nothing"
  alarm; a cross-runbook dedup → `benign_dedup` skip (no alarm); a runbook that
  seeded ≥1 cause is not alarmed even with a `quality_drop` sibling.
- **Prior-not-gate (flag-ON sim):** at eval end, ∃ a hypothesis whose
  `root_node_id` ∉ the seeded set with likelihood > the seeded prior, **and** no
  seeded prior is `VALIDATED` — the behavioral proof that a self-generated
  hypothesis can beat a wrong seeded prior (seeding is a prior, not a gate).
- **Exclusion-under-seeding (flag-ON sim):** with a seeded OR-differential
  {A,B,C}, refute all-but-one seeded sibling and pressure the engine toward the
  survivor with no positive evidence and no legitimate exhaustiveness proof — the
  deductive-exclusion arm (`validate_by_exclusion`) must **not** fabricate a
  VALIDATED seeded cause. Any seeded root that reaches VALIDATED must carry a real
  `validation_method`, and if `DEDUCTIVE` its precondition must genuinely hold
  (≥2 sibling roots absolutely excluded — `REFUTED` at belief ≤
  `DEDUCTIVE_EXCLUSION_MAX_BELIEF`). Seeding never lowers the exclusion bar; seeded
  siblings do not bias what exclusion quantifies over (the seeded differential is
  never engine-certified exhaustive).

The sim/eval runs strict-enforcement + averaged + a cheap model, and must include
a misleading-runbook scenario. Model variation never changes these engine rules.

The eval is a committed, re-runnable artifact — harness, scenarios, and recorded
transcripts at [`tests/eval/kb_cause_seeder/`](../../../tests/eval/kb_cause_seeder/)
(modes `smoke` / `mislead` / `exclusion` / `postturn1`).

### Enabling gate — the passes the flag-on decision was made against

The flag is **on by default.** The mechanically-verified code merged first (flag
OFF); the flag was then turned on after the flag-ON sim/eval, on the **hardest
provider (BEST_EFFORT)**, cleared the items below. The bar is the hardest
provider, *not* every provider:
items 1 and 4 are structural (candidate-only, evidence-less, provenance-blind,
prior-capped) and hold LLM-agnostically by construction; the only
prompt-strength-dependent items (2, 3) are *weakest* on a BEST_EFFORT model, so a
pass there is the binding case and a STRICT provider can only do better on them.
Requiring green on every provider adds cost without assurance beyond the
weakest-link pass. STRICT-provider runs stay optional cross-checks (see
[`tests/eval/kb_cause_seeder/README.md`](../../../tests/eval/kb_cause_seeder/README.md)
for the recorded fireworks pass and the external-wall status of the others). The
gate items:

1. **No collapse / no incorrect conclusion** — a wrong seed never reaches
   VALIDATED and the engine does not conclude on it (structural: candidate-only,
   evidence-less, provenance-blind). When the LLM engages the wrong seed it also
   decays + is anchoring-flagged; an ignored seed is decayed toward stagnation by
   the housekeeping age-sweep (see the decay row and #713) — neither path concludes.
2. **No crowd-out** — the LLM keeps generating its own hypotheses.
3. **No paraphrase-duplication** — ≤ 1 ACTIVE hypothesis per seeded cause.
   This is a *quality* property and the only prompt-strength-dependent gate item,
   so it is measured **strict + averaged as a pass-rate**, not a single pass. On
   BEST_EFFORT (`deepseek-v4-flash`) a 2026-07-16 batch measured **7/8** — one run
   hit the documented below-INV-36-bar paraphrase-duplication (soundness held in
   that run too). **Whether the measured rate clears this item is a deliberate
   flag-on judgment**, not a value this eval declares met; the intended envelope
   for the residual is prompt + per-provider eval (see "Prompt alignment"), and a
   prompt change is re-measured with the committed harness. Items 1 and 4 do not
   have this caveat — they hold on every run by construction.
4. **Prior-not-gate — required, and distinct from #1.** No-collapse only
   proves the wrong seed *dies*; this proves the engine still *reaches the right
   answer* via the LLM's own theory rather than stalling: ∃ a hypothesis whose
   `root_node_id` ∉ the seeded set with likelihood > the seeded prior, and no
   seeded prior is `VALIDATED`. This is the positive proof the whole "prior, not
   gate" claim rests on — it is a gate item, not a nice-to-have.

**Decision (flag on).** Items 1 and 4 (soundness) hold on every run by
construction — measured **8/8** on BEST_EFFORT. Item 3's measured pass-rate
(**7/8** on BEST_EFFORT) was accepted as a **known residual**, not a blocker:
pre-production with no users, the criterion for on-vs-off is which state exposes
more of the seeder path to real use and bug-hunting, not rollout safety, and the
single below-INV-36-bar duplication held soundness in that run too. Its envelope
stays prompt + per-provider eval (a prompt change is re-measured with the
committed harness), **not** a new seed-specific dedup backstop (#658 territory).
The flag is retained as the kill switch and the tested flag-OFF no-op path, and is
removed only as the final adoption step.

Plus one **measurement** (not pass/fail — it sizes a follow-on decision):

- **Right runbook emerges only after the initial statement.** A case opened vague,
  then narrowed to a specific symptom. Measures the one-shot boundary (see
  "Boundary"). **Run (fireworks):** narrowing *during INQUIRY* still seeds the
  correct runbook (the confirmed statement carries the scoping), so the residual
  gap is confined to *post-verification* discovery — smaller than feared. A
  guarded re-seed hook for that residual remains a possible follow-on; the
  measurement says it is not urgent.
