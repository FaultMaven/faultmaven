# kb_grounding_1285 fixture — provenance

`recorded-hybrid-retrievals.json` is a **recording**, not a construction. Every chunk in it
is real text from the shipped KB pack, retrieved by the real hybrid pipeline for
the query it is paired with, and every `df` entry is that term's real document
frequency in the corpus it was retrieved from. Nothing in it was authored to
make a comparison come out a particular way — which is the whole point, because
the pin it replaced supplied the very number whose reachability was in question
(`KB_SEED_MIN_TERM_COVERAGE - 0.01`).

Read by `the seeder's reachability test (deleted with the KB cause seeder, fm#1295)`,
which **recomputes** coverage and identity terms from this text through the
production code and asserts the results still match what was recorded. That
assertion is the fixture's tripwire: with the coverage arm gone, title/service
tokenisation — the plural fold, the stop list, the three-character floor — is
the entire seeding gate, and a change to any of it lands here first. It checks
`recorded_term_coverage` per chunk, `recorded_max_term_coverage` (the value the
SELECTION below was made on), and `recorded_identity_terms`.

## Selection, and the way it went wrong the first time

The first cut kept "every pair the covers arm admitted (≥ 0.90)" plus "the
highest-coverage off-domain pairs". Both rules select on coverage, so **every
off-domain pair in the names-silent partition landed at coverage exactly
1.000** — and the threshold sweep that ran over that partition compared a
constant against a variable. `n_off >= n_on` was true at every bar by
construction, and a distribution that was *not* inverted could not have failed
it. Two tests now guard against a repeat: one asserts every quadrant of
(on/off × above/below the bar) is populated in the swept partition, and one
asserts `kind` has not collapsed into a copy of `on_domain`, which is the same
defect seen from the labelling side.

The current rules:

| selection | why it is present |
|---|---|
| every pair the removed covers arm admitted (`≥ 0.90`, names silent) | what the arm actually did, including "The application is slow." reaching 1.000 |
| every on-domain pair the names arm misses | the case a second ground would exist for — 12 of them, the honest residue |
| off-domain names-silent pairs **stratified** into coverage bands, ≤6 per band | so the sweep measures the metric rather than the selection |
| every off-domain pair the names arm ADMITS | the surviving arm's own cost, which nothing bounded before #1285 |
| on-domain pairs the names arm carries, two chunks each | the positive control: the gate admits, and the seeding path runs end to end past #1144's corroboration guard |

Only **labelled** records are eligible. A `real_case` record carries no
expected-runbook fragments, so `on_domain` is False for all of its pairs — which
means *unlabelled*, not *off-domain*. An earlier cut of the stratified generator
folded them in and manufactured 518 "wrong admissions" out of pairs nobody had
adjudicated; the real-case corpus is still what the *rate* measurements in
`milestone_engine` are drawn from, but it cannot label anything.

A pair carries up to **two** of the runbook's own retrieved chunks because the
engine folds hits per runbook (grounding is a property of the document) and
because corroboration downstream counts distinct chunks.

`provenance` on each pair says where its query came from:

- `labelled_24` — the project's own labelled statements,
  `labelled-statements.json`.
- `peer_paraphrase` — disk-full rephrasings written by a second agent measuring
  the same gate from the index side, independently of this fixture's author.
- `authored_paraphrase` — symptom-phrased queries written for #1285 so the
  covers arm's intended population (a correct runbook the query never names) was
  represented at all. Authored **after** the mechanism under evaluation was
  conceived, so they are the weakest evidence here and are labelled as such; the
  conclusions in `milestone_engine` are stated against the independent subsets
  as well.

## Regenerating

Needs an ingested KB (`data/chroma-kb` + `data/faultmaven.db`) and the BGE-M3
embedder — the same inputs as `the seeder's eval driver (deleted with the KB cause seeder, fm#1295)`,
whose `retrieve_hybrid` produces the hits this was recorded from. Re-record when
the shipped pack changes, or when a deliberate change to the lexical code makes
the faithfulness assertion fail; do **not** hand-edit values to make a test pass.
After re-recording, re-derive the counts asserted in the test — they are exact
because the fixture is frozen, and they must be re-derived, not adjusted until
green. The bound in `test_the_surviving_arms_wrong_admissions_are_bounded` is a
ceiling that is expected to be *lowered* by a future change, never raised to fit.
