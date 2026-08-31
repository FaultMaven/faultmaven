# kb_grounding_1285 fixture — provenance

`grounding_pairs.json` is a **recording**, not a construction. Every chunk in it
is real text from the shipped KB pack, retrieved by the real hybrid pipeline for
the query it is paired with, and every `df` entry is that term's real document
frequency in the corpus it was retrieved from. Nothing in it was authored to
make a comparison come out a particular way — which is the whole point, because
the pin it replaced supplied the very number whose reachability was in question
(`KB_SEED_MIN_TERM_COVERAGE - 0.01`).

Read by `tests/unit/core/investigation/test_kb_seed_grounding_reachability_1285.py`,
which **recomputes** coverage and identity terms from this text through the
production code and asserts the results still match what was recorded. That
assertion is the fixture's tripwire: with the coverage arm gone, title/service
tokenisation — the plural fold, the stop list, the three-character floor — is
the entire seeding gate, and a change to any of it lands here first.

## What is in it, and why

| selection | why it is present |
|---|---|
| every pair the removed covers arm admitted (`term_coverage >= 0.90`) | what the arm actually did, including "The application is slow." reaching 1.000 against eight unrelated runbooks |
| every on-domain pair the names arm misses | the case a second ground would exist for — 12 of them, the honest residue |
| the highest-coverage off-domain pairs the names arm misses | what any bar on coverage drags in alongside them |
| on-domain pairs the names arm carries, with two chunks each | the positive control: the gate admits, and the seeding path runs end to end past #1144's corroboration guard |

A pair carries up to **two** of the runbook's own retrieved chunks because the
engine folds hits per runbook (grounding is a property of the document) and
because corroboration downstream counts distinct chunks.

`provenance` on each pair says where its query came from:

- `labelled_24` — the project's own labelled statements,
  `tests/eval/kb_cause_seeder/corroboration-statements.json`.
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
embedder — the same inputs as `tests/eval/kb_cause_seeder/run_corroboration_eval.py`,
whose `retrieve_hybrid` produces the hits this was recorded from. Re-record when
the shipped pack changes, or when a deliberate change to the lexical code makes
the faithfulness assertion fail; do **not** hand-edit values to make a test pass.
After re-recording, re-read the counts asserted in the test — they are exact
because the fixture is frozen, and they must be re-derived, not adjusted until
green.
