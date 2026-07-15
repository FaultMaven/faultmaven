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
real case evidence, decays when unsupported, and is anchoring-flagged and demoted
exactly like a self-generated hypothesis.

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

## Data flow

```text
symptom_verified ─► _transition_to_investigating
                     └─► _prefetch_kb_context          (existing: search_knowledge)
                          └─► seed_candidate_causes_from_kb(case, hits, kb_item_repo, turn)   ◄── NEW, flag-gated
                               1. ensure D            seed_problem_node(case)
                               2. pick runbooks       distinct parent_document_id, top-N by score
                               3. load causes         kb_item_repo.get_by_id(id).metadata["causes"]
                               4. select causes       skip fallback; rank by evidence alignment; cap MAX_SEEDED_CAUSES
                               5. instantiate         ingest_emitted_chain(...)  +  create_hypothesis(...)
```

### 1. Source identity (4.2)

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

### 2–3. Runbook and cause selection (multi-runbook merge rule — 4.3)

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
  once — handled for free by `ingest_emitted_chain`, which reuses a node on
  exact-normalized `(node_type, statement)`. Near-duplicate roots are reconciled
  by the existing MECE arbitration (`distinct_cause_clusters`, Jaccard 0.6).
- **Distinct roots compete as OR-alternatives:** pack `chain_edges` carry no
  `and_group`, so seeded predecessors enter as independent OR-alternative sibling
  causes — never silently merged into one Cause. Evidence separates them. A cause
  that *does* carry `and_group` (co-necessary AND-convergence) is **rejected**, not
  flattened — see the `unsupported_shape` skip below.

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
  (overlap); normal and correct.
- **`quality_drop`** — a *real* cause the seeder could not instantiate (no chain,
  non-root head, bad `node_type`, empty statement, ingest produced nothing).
- **`unsupported_shape`** — a well-formed cause using a structure the seeder does
  not yet model. Today that is only `and_group` **AND-convergence**: the seeder
  reads `cause_ref`/`effect_ref` and defaults every edge to `and_group=None`, so a
  co-necessary AND-set would be silently flattened to independent OR-alternatives
  (A∧B → A∨B — a MECE mis-model). It is **rejected, not flattened** (honor-or-reject,
  plan 4.3) until AND-seeding is built. Zero instances in the pack today; the guard
  exists for the Phase 5 produce path (converted runbooks generate v4 structure).

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

## Guarantee: no evidentiary privilege, no anchoring (4.1)

A seeded chain is a *strong prior*, and a strong prior is precisely what can
anchor an LLM toward a wrong theory it then rationalizes — the
NO-COLLAPSE-UNDER-PRESSURE failure mode. The seeder is designed so a seeded prior
is **mechanically indistinguishable** from a self-generated one to every safety
mechanism:

| Mechanism | How the seeded candidate is subject to it |
|---|---|
| Prior cap | `create_hypothesis` clamps to `≤ 0.5` — no head start. |
| Confidence decay (`0.85^iterations`) | Seeded hypotheses are ACTIVE with `iterations_without_progress=0` and empty `evidence_links`; an unsupported seed's counter climbs and it decays each turn via the existing housekeeping loop — no special-casing. |
| Anchoring detection | Counts and can retire seeded candidates identically (`detect_anchoring` reads `category`/`iterations`/`likelihood`, not origin). |
| Failed-fix demotion (M6) | A disproved seed flows through the same `refute_hypothesis` / counterfactual-demotion path. |
| VALIDATED | Unreachable by the seeder. `derive_node_states` (nodes) and `project_hypothesis_states_from_roots` (hypotheses) are the sole VALIDATED writers, and Pydantic validators reject a hand-set VALIDATED node lacking a method/actionable flag. |

The seeder writes **candidates only**. It never sets likelihood above the prior
cap, never links evidence, never sets VALIDATED. Anchoring and decay are the
backstop: a misleading runbook's seeded cause receives no supporting evidence,
decays, is anchoring-flagged, and is demoted — the engine does not conclude.

**Provenance-blindness invariant (load-bearing).** The whole no-privilege claim
rests on *nothing branching on origin*. The seeder's provenance markers
(`node.metadata["seeded_from_runbook"]`, the hypothesis `rationale`) are read
only by observability and tests. No safety mechanism — confidence decay,
anchoring detection, failed-fix demotion, node/hypothesis state derivation —
reads them. This is enforced by a standing invariant test (a grep/AST assertion
that the decay/anchoring/demotion/derivation code paths never reference the
provenance keys), so a future edit cannot quietly grant a seed evidentiary
weight.

**Honest limit — seed↔LLM anchoring interaction.** The `KB_SEEDER_MAX_CAUSES`
cap stops the seeder *alone* from tripping anchoring condition 1. But seeded
`OTHER`-category causes and any `OTHER`-category hypothesis the LLM generates can
still combine to ≥ the threshold and raise an anchoring flag. This is
conservative-safe — a safety mechanism firing early costs exploration breadth,
never a wrong conclusion — but it is a real seed→LLM interaction, asserted as a
measured eval expectation rather than left as a surprise.

## Prompt alignment (4.4)

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
  `hypotheses_to_add` for a seeded cause with *reworded* text. Node dedup keys on
  exact-normalized `(node_type, statement)`, so a paraphrase is **not** caught,
  yielding two ACTIVE `OTHER` hypotheses for one cause (inflating the `OTHER`
  bucket toward the anchoring threshold, breaking sibling-MECE).

Both are prompt-strength-dependent and weakest on a BEST_EFFORT model
(`deepseek-v4-flash`, the dev/demo default). The correct envelope is **prompt +
per-provider eval**, *not* a mechanical semantic-dedup backstop — matching an
emitted paraphrase against seeded causes is exactly the retired matcher's
territory (#658 NO-GO). So: no-collapse stays mechanical; no-duplication stays
prompt-level and is measured per provider (see Verification).

The flat "Exactly one Cause matches → that Cause IS your hypothesis" branch
remains the behavior when the flag is off, and remains the fallback for
**prose-only** sources (converted drafts without a `causes` record) even when the
flag is on.

## Freshness (4.5)

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

The `parent_document_id` surfacing (4.2) and the causes-freshness comparison
(4.5) are plain correctness fixes and run regardless of the flag; the KB schema
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
- **Misleading runbook:** a wrong seeded prior with no supporting evidence decays
  across turns and is anchoring-flagged/demoted; the engine reaches no conclusion
  (NO COLLAPSE, NO INCORRECT CONCLUSION). The eval also confirms the engine did
  **not** simply stop exploring — the LLM continues generating its own
  hypotheses (guards the seed-crowd-out quality risk).
- **No paraphrase-duplication (per-provider, flag-ON sim only):** after a seeded
  turn, assert **≤ 1 ACTIVE hypothesis per seeded cause** — no second hypothesis
  whose root competes for a seeded root's coverage. This is the property the
  mechanical guarantee cannot cover (it is prompt-strength-dependent), so it is a
  **first-class assertion measured on every provider, including the BEST_EFFORT
  dev/demo model** where the prompt is weakest — not folded into "no collapse."
  Deliberately **not** backstopped by mechanical semantic dedup (#658 territory).
- **Multi-runbook:** an identical-statement cause seeded via two runbooks dedups
  to one node; two distinct roots enter as competing OR-alternative candidates.
- **Freshness:** a causes-only pack change re-ingests.
- **Flag off:** the seeder is a no-op; the flat KB-resolution prompt path is
  unchanged.
- **Observable skip (unit):** a fallback → `intentional` skip (no alarm); a
  malformed real cause → `quality_drop` skip **and** the "contributed nothing"
  alarm; a cross-runbook dedup → `benign_dedup` skip (no alarm); a runbook that
  seeded ≥1 cause is not alarmed even with a `quality_drop` sibling.
- **Prior-not-gate (3b, flag-ON sim):** at eval end, ∃ a hypothesis whose
  `root_node_id` ∉ the seeded set with likelihood > the seeded prior, **and** no
  seeded prior is `VALIDATED` — the behavioral proof that a self-generated
  hypothesis can beat a wrong seeded prior (seeding is a prior, not a gate).

The sim/eval runs strict-enforcement + averaged + cheap model
(`claude-haiku-4-5`), and must include a misleading-runbook scenario. Model
variation never changes these engine rules.
