"""What the #1270 fix does to ``turns_without_progress``, over the whole corpus.

``turns_without_progress`` is read by nine modules -- ``progress_monitor``'s
exhaustion detector, ``working_conclusion_generator``'s LOW/BLOCKED momentum
bands, ``verification_status.is_stalled``, ``evidence_need_surfacing``'s page
cursor, ``prompts/context_builder``'s "M turns since last progress" line,
``hypothesis_manager``. Correcting the transition turn RESETS the counter where
it used to climb, so every stall signal fires later or not at all. That is the
correct direction, but it is a behaviour change to a signal the engine acts on,
so it is measured here rather than argued.

Model: the counter is a run-length over the stored per-turn ``progress_made``.
The deterministic arm is modelled explicitly -- see ``run_counter`` -- and the
model is VALIDATED against the persisted ``cases.turns_without_progress`` column
before any after-value is claimed.

Not collected by pytest (no ``test_`` filename). Reads the corpus; writes
nothing. Run from the repo root::

    python tests/eval/progress_score_ordering/corpus_before_after.py

Corpus override: ``FM_CASES_DB``.
"""

import collections
import contextlib
import json
import os
import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DB = os.environ.get("FM_CASES_DB", str(REPO_ROOT / "data" / "faultmaven.db"))

#: See ``replay_transition.SENTINEL``.
SENTINEL = "Confirm problem statement and decide to investigate"

#: The bands the counter drives, read off the source rather than guessed:
#:   ``exhaustion_thresholds.EXHAUSTION_STALL_THRESHOLD = 5``
#:     -> ``progress_monitor._detect_exhaustion``, ``verification_status.is_stalled``
#:   ``working_conclusion_generator``: BLOCKED at >= 5, LOW at >= 3,
#:     ``blocked_reasons`` entry at >= 3
LOW_BAND = 3
BLOCKED_BAND = 5


def is_inquiry(turn: dict) -> bool:
    return (turn.get("next_steps") or []) == [SENTINEL]


def transition_index(turns: list[dict]) -> int | None:
    for j in range(1, len(turns)):
        if is_inquiry(turns[j - 1]) and not is_inquiry(turns[j]):
            return j if all(is_inquiry(t) for t in turns[:j]) else None
    return None


def run_counter(scores: list[bool], deterministic: list[bool]) -> list[int]:
    """Replay Step 5.8 over a case's per-turn progress scores.

    Step 5.8's increment sits inside the generation block, so a DETERMINISTIC
    branch never reaches it: ``_finish_deterministic_turn`` resets on progress
    and otherwise FREEZES the counter (its own docstring says so, measured
    rather than assumed). A stored turn record written by that branch carries no
    ``momentum`` -- it is not passed -- which is the discriminator used here.

    With that arm modelled the replay reproduces the persisted
    ``cases.turns_without_progress`` column exactly on 228/228 local cases; that
    is the positive control for every after-value this script prints.
    """
    twp = 0
    out = []
    for made, det in zip(scores, deterministic):
        if made:
            twp = 0
        elif not det:
            twp += 1
        out.append(twp)
    return out


def main() -> None:
    with contextlib.closing(sqlite3.connect(DB)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "select case_id, turns_without_progress, metadata from cases "
            "where metadata is not null"
        ).fetchall()

    model_ok = model_bad = 0
    rescored = 0
    turns_before: collections.Counter = collections.Counter()
    turns_after: collections.Counter = collections.Counter()
    ever_before: collections.Counter = collections.Counter()
    ever_after: collections.Counter = collections.Counter()
    delay: collections.Counter = collections.Counter()
    final_before: list[int] = []
    final_after: list[int] = []

    for r in rows:
        meta = json.loads(r["metadata"])
        turns = sorted(
            meta.get("turn_history") or [], key=lambda t: t.get("turn_number") or 0
        )
        if not turns:
            continue
        scores = [bool(t.get("progress_made")) for t in turns]
        deterministic = [t.get("momentum") is None for t in turns]
        before = run_counter(scores, deterministic)
        if before[-1] == (r["turns_without_progress"] or 0):
            model_ok += 1
        else:
            model_bad += 1

        j = transition_index(turns)
        after_scores = list(scores)
        if j is not None and not scores[j]:
            after_scores[j] = True
            rescored += 1
        after = run_counter(after_scores, deterministic)

        for value in before:
            if value >= LOW_BAND:
                turns_before["low"] += 1
            if value >= BLOCKED_BAND:
                turns_before["blocked"] += 1
        for value in after:
            if value >= LOW_BAND:
                turns_after["low"] += 1
            if value >= BLOCKED_BAND:
                turns_after["blocked"] += 1

        for band, name in ((LOW_BAND, "low"), (BLOCKED_BAND, "blocked")):
            hit_b = next((i for i, v in enumerate(before) if v >= band), None)
            hit_a = next((i for i, v in enumerate(after) if v >= band), None)
            if hit_b is not None:
                ever_before[name] += 1
            if hit_a is not None:
                ever_after[name] += 1
            # All four transitions, including the FALSIFYING one. The fix can
            # only ever delay a stall signal, so a band crossed in AFTER but not
            # in BEFORE -- or crossed EARLIER -- would contradict the claim this
            # script exists to support. Dropping those cases meant the output
            # could not disagree with its own thesis.
            if hit_b is not None and hit_a is not None:
                if hit_a > hit_b:
                    delay[(name, f"later by {hit_a - hit_b}")] += 1
                elif hit_a < hit_b:
                    delay[(name, f"EARLIER by {hit_b - hit_a}")] += 1
            elif hit_b is not None and hit_a is None:
                delay[(name, "never crossed after")] += 1
            elif hit_b is None and hit_a is not None:
                delay[(name, "NEWLY crossed after")] += 1

        final_before.append(before[-1])
        final_after.append(after[-1])

    # DENOMINATORS FIRST, and a hard stop if either is empty. A before/after
    # over zero cases, or a re-score count of zero, prints a clean-looking
    # result that actually means "I could not ask" -- the detector matched
    # nothing, or the model never reproduced the stored counter it is supposed
    # to be perturbing. Three outcomes, never two.
    print(f"cases with turn history                  : {len(final_before)}")
    print(
        f"counter model reproduces stored column   : {model_ok}/{model_ok + model_bad}"
    )
    print(f"cases whose transition turn is re-scored : {rescored}")
    if not final_before or not rescored or model_bad:
        print(
            "COULD NOT ASK: "
            + (
                "no cases with turn history; "
                if not final_before
                else (
                    "the counter model does not reproduce the stored column, so "
                    "its after-values are not trustworthy; "
                    if model_bad
                    else "the detector matched no transition turn to re-score; "
                )
            )
            + "the comparison below is meaningless."
        )
        raise SystemExit(2)
    print()
    print("turns_without_progress (final, summed over the corpus)")
    print(f"  before {sum(final_before)}   after {sum(final_after)}")
    print(
        f"  cases with a non-zero counter: before "
        f"{sum(1 for v in final_before if v)}   after {sum(1 for v in final_after if v)}"
    )
    print()
    print(f"turn-instances at or above LOW / blocked_reasons (>= {LOW_BAND})")
    print(f"  before {turns_before['low']}   after {turns_after['low']}")
    print(
        f"turn-instances at or above BLOCKED / is_stalled / exhaustion "
        f"(>= {BLOCKED_BAND})"
    )
    print(f"  before {turns_before['blocked']}   after {turns_after['blocked']}")
    print()
    print("cases that EVER reach a band")
    print(f"  >= {LOW_BAND}: before {ever_before['low']}   after {ever_after['low']}")
    print(
        f"  >= {BLOCKED_BAND}: before {ever_before['blocked']}   "
        f"after {ever_after['blocked']}"
    )
    print()
    print("when the first band crossing happens, after vs before")
    if not delay:
        print("  (no case crosses a band on either side)")
    for key in sorted(delay, key=lambda k: (k[0], str(k[1]))):
        print(f"  {key[0]:8s} {key[1]}: {delay[key]} cases")
    wrong_way = {k: v for k, v in delay.items() if "EARLIER" in k[1] or "NEWLY" in k[1]}
    if wrong_way:
        print(
            "  CONTRADICTION: the fix can only delay a stall signal, but these "
            f"cases cross a band sooner or newly: {wrong_way}"
        )


if __name__ == "__main__":
    main()
