# Progress-score ordering (#1270)

Two corpus harnesses backing the #1270 fix — the generation path scored
`progress_made` **before** `_check_automatic_transitions` wrote the
`status_transitioned` arm, so an automatic INQUIRY→INVESTIGATING transition
never counted as progress.

Neither is collected by pytest (no `test_*` filename). Both read a local dev
SQLite corpus and write nothing. Point them at one with `FM_CASES_DB`; the
default is `data/faultmaven.db` under the repo root.

```bash
python tests/eval/progress_score_ordering/replay_transition.py
python tests/eval/progress_score_ordering/corpus_before_after.py
```

## `replay_transition.py` — does the engine score the transition turn?

Replays every stored case that has an observable INQUIRY→INVESTIGATING
transition through the **real** `MilestoneEngine`, seeded from that case's own
title, description, `problem_confirmation`, `preliminary_urgency`, proposed
problem statement and its two real user messages. Only the LLM is stubbed, and
with the fields the case actually recorded — so the engine runs the same
ordering it runs in production.

Measured on the 253-case dev corpus (170 replayable transitions):

| | before #1270 | after |
|---|---|---|
| transition turn `progress_made=True` | 0 | 170 |
| `turns_without_progress` after the turn | 2 | 0 |

The replay reads 170/170 where the *stored* corpus reads 158/170 mis-scored:
the 12 stored exceptions each carried an upload on the transition turn, so
`novel_files_uploaded` — written at Step 0, before the score — fired for them
independently. The replay attaches no file, so it removes that confound.

## `corpus_before_after.py` — what the fix does to the stall signals

`turns_without_progress` is read by nine modules. Correcting the transition turn
resets the counter where it used to climb, so **every stall signal fires later
or not at all**. This quantifies that over the stored corpus rather than over a
fixture.

The counter is replayed as a run-length over each case's stored per-turn
`progress_made`, with the deterministic arm modelled: Step 5.8's increment sits
inside the generation block, so a deterministic branch resets on progress and
otherwise FREEZES the counter (`_finish_deterministic_turn` says so). A stored
turn record from that branch carries no `momentum`, which is the discriminator.

**Positive control:** with that arm modelled the replay reproduces the persisted
`cases.turns_without_progress` column exactly on **228/228** cases. Without it,
167/228 — and the 61 misses are all one-directional overstatements, which is the
frozen-counter signature. No after-value is claimed until the before-value
reproduces.
