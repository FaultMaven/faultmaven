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
detection, and failed-fix demotion as a self-generated hypothesis (an
engaged-but-unsupported seed decays; an ignored one stays inert at its ≤0.5 prior
— see the Guarantee section).

Shipped dark behind `FAULTMAVEN_KB_CAUSE_SEEDER` (default off).

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

### 2–3. Runbook and cause selection (multi-runbook merge rule)

Retrieval routinely returns several runbooks and each runbook has many Causes.
Left unbounded, seeding would flood the graph and trip anchoring detection (≥4
active hypotheses in one category reads as fixation). The bounds:

- **Cap runbooks:** dedup hits by `parent_document_id`, take the top-N distinct
  runbooks by rerank score (`KB_SEEDER_MAX_RUNBOOKS`, default 2). Retrieval has
  already done the semantic alignment at runbook granularity.
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
  `KB_SEEDER_MAX_CAUSES`. This is **derived from** the anchoring condition-1
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
  the skip taxonomy). Near-duplicate roots are reconciled by the existing MECE
  arbitration (`distinct_cause_clusters`, Jaccard 0.6).
- **Distinct roots compete as OR-alternatives:** pack `chain_edges` carry no
  `and_group`, so seeded predecessors enter as independent OR-alternative sibling
  causes — never silently merged into one Cause. Evidence separates them. A cause
  whose shape the seeder does not model — `and_group` co-necessary AND-convergence,
  or a non-linear chain (a second root, a branching fork, a convergence/join, a
  dangling edge ref, or a cycle/fragment/non-`D`-terminating chain) — is
  **rejected**, not flattened/mis-seeded — see the `unsupported_shape` skip below.

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
`KB_SEEDER_MAX_CAUSES = 3` cap keeps this from tripping anchoring condition 1 on
its own.

### Observable skip — no silent drop

A matched runbook that seeds nothing must never be *invisible*. Every non-seed is
recorded as a class-tagged `SkippedCause` on the `SeedReport` (keyed by
`(item_id, cause_letter)`):

- **`intentional`** — the fallback (`Z`/`[Default]`) cause; never a candidate root
  by design.
- **`benign_dedup`** — a root already seeded by an earlier retrieved runbook
  (overlap); normal and correct. Decided **before** ingest (see *Dedup across
  runbooks* above), so it never mints orphan nodes.
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
whose only skips are dedup/fallback is *not* alarmed, so two runbooks sharing a
cause never false-alarm. (A runbook never entered because the `MAX_SEEDED_CAUSES`
budget was already spent leaves no skip record; that zero is benign — budget, not
quality.) The shipped corpus has **zero** `quality_drop` shapes (all 549
non-fallback causes are multi-rung), so this is observability for future/authoring
drift, not a current defect.

### What the seeder consumes (and what it doesn't)

The seeder reads a cause's `chain_nodes` / `chain_edges` / `cause_statement` /
`cause_letter` / `cause_name`. It does **not** structurally consume
`rung_indicators` or `interventions` — those reach the engine only as *prose* for
the LLM (the AUTHORITY block's per-rung indicator matching, and treatment-stage
`SolutionToAdd`). So a seeded chain is *topology with the per-rung validation
signal detached*: the engine leans on the LLM to map evidence to the right rung.
This is the deepest remaining slice of the write-only-`causes` residue. Making it
load-bearing — seeding `rung_indicators` as evidence-needs / expected
`causal_evidence` per rung — is a **recommended, separately-sized follow-on**, its
go/no-go informed by whether seeded rungs validate well in the eval. (Wiring the
deterministic `<!-- match -->` predicates is blocked upstream: the pack ships zero
`predicate`/`exit_code` keys — they are dropped at pack-build.)

## Guarantee: no evidentiary privilege, no anchoring

A seeded chain is a *strong prior*, and a strong prior is precisely what can
anchor an LLM toward a wrong theory it then rationalizes — the
NO-COLLAPSE-UNDER-PRESSURE failure mode. The seeder is designed so a seeded prior
is **mechanically indistinguishable** from a self-generated one to every safety
mechanism:

| Mechanism | How the seeded candidate is subject to it |
|---|---|
| Prior cap | `create_hypothesis` clamps to `≤ 0.5` — no head start. |
| Confidence decay (`0.85^iterations`) | Same treatment as a self-generated hypothesis — no special-casing. Precisely: `iterations_without_progress` climbs only when the LLM *engages* a hypothesis (touches it via `link_evidence`/likelihood update that fails to move likelihood ≥5%), and `apply_likelihood_decay` no-ops while the counter is 0 (`hypothesis_manager.py`). So a seed the LLM **engages and fails to support** decays each turn via the housekeeping loop, exactly like any other stagnant hypothesis; a seed the LLM **never touches** keeps `iterations_without_progress=0` and stays inert at its ≤0.5 prior (it never decays, and stagnation-based anchoring never fires on it). Either way it is harmless: candidate-only, evidence-less, provenance-blind → cannot validate. (Whether an *untouched* ACTIVE hypothesis should also decay is a global engine question, not seeder-specific — filed as #713.) |
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
the LLM simply **ignores** stays inert at ≤0.5 rather than decaying (see the decay
row above and #713). In no case does the engine conclude on it.

**Provenance-blindness invariant (load-bearing).** The whole no-privilege claim
rests on *nothing branching on origin*. The seeder records origin in two read
surfaces — `node.metadata["seeded_from_runbook"]` and the hypothesis `rationale`
(prefix `"Seeded from runbook …"`) — read only by observability and tests. This
is enforced by a standing invariant test that greps the safety modules for
**both** markers (the metadata key *and* the rationale-prefix literal), so a
mechanism cannot sniff origin out of the rationale string either. The checked
module set spans consume-side safety (decay / anchoring / failed-fix demotion /
node+hypothesis state derivation in `causal_graph` + `hypothesis_manager`;
`cause_state` derivation + the per-turn housekeeping loop in `milestone_engine`)
**and** the conclusion/terminal gates a seeded prior must never shortcut
(`cause_assurance`, `terminal_transitions`, `progress_monitor`, `state_validator`,
`working_conclusion_generator`) — a whole-file grep is deliberately coarse so the
guard can never be silently narrowed below where the safety logic actually lives.
A future edit cannot quietly grant a seed evidentiary weight.

**Honest limit — seed↔LLM anchoring interaction.** The `KB_SEEDER_MAX_CAUSES`
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
| `FAULTMAVEN_KB_CAUSE_SEEDER` (`features.kb_cause_seeder_enabled`) | env flag | `false` | Gates seeder invocation **and** the AUTHORITY prompt override. Kill switch — disables in prod without rollback. |
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
- **Cap ↔ anchoring coupling:** `KB_SEEDER_MAX_CAUSES` is strictly below the
  anchoring condition-1 threshold (asserted against the real constant, so the two
  cannot silently drift apart).
- **Misleading runbook:** a wrong seeded prior with no supporting evidence stays a
  CANDIDATE at ≤0.5 and never reaches VALIDATED, so the engine reaches no
  conclusion on it (NO COLLAPSE, NO INCORRECT CONCLUSION) — this holds structurally
  whether or not the LLM engages the seed. When the LLM *does* engage it and fails
  to support it, it additionally decays across turns and is anchoring-flagged/
  demoted; when the LLM ignores it, it simply sits inert at ≤0.5 (no decay — see
  the decay row and #713). The eval also confirms the engine did **not** simply
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

### Enabling gate — required passes before the flag turns on

The flag ships OFF; the mechanically-verified code merges first. Turning it on
requires the flag-ON sim/eval, on the **hardest provider (BEST_EFFORT)**, to clear
all of the items below. The bar is the hardest provider, *not* every provider:
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
   decays + is anchoring-flagged; an ignored seed stays inert at ≤0.5 rather than
   decaying (see the decay row and #713) — neither path concludes.
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

Plus one **measurement** (not pass/fail — it sizes a follow-on decision):

- **Right runbook emerges only after the initial statement.** A case opened vague,
  then narrowed to a specific symptom. Measures the one-shot boundary (see
  "Boundary"). **Run (fireworks):** narrowing *during INQUIRY* still seeds the
  correct runbook (the confirmed statement carries the scoping), so the residual
  gap is confined to *post-verification* discovery — smaller than feared. A
  guarded re-seed hook for that residual remains a possible follow-on; the
  measurement says it is not urgent.
