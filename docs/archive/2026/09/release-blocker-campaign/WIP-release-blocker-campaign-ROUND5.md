# Release-blocker campaign — full board (CHECKPOINT 2026-08-29 R5, three ROOT lanes in flight)

> Every status line below was re-verified against `gh api` and the live tree at the
> time of writing. Treat it as stale on your next read anyway.

## Base and merge state — VERIFIED

`main` is **`e1cf27371`** (#1259, "one answer to is-this-optional-dependency-usable"),
NOT `c7204f5a3` as the round brief stated — #1259 landed ~1h after #1253. **The three
lanes are based on `e1cf27371`, not on `c7204f5a3`.** Rationale: GitHub builds
`refs/pull/N/merge` against main's tip, so branching off a stale tip is what produced
last round's un-mergeable PR and burned a full cycle. #1259 touches optional-dependency
flag detection — disjoint from all three lane trees.

### Merge-window reconciliation — CLEAN
15 issues closed since 2026-08-28, every one attributable to a merged PR, **nothing
collateral**:

| closed | issues | by |
|---|---|---|
| 01:45–01:48Z | #694, #1214, #1217, #1210 | batch 1 |
| 07:31–07:33Z | #1222, #1235, #1226, #1229, #1228 | batch 2 |
| 10:57Z | #1227 | #1249 |
| 19:15Z | #1230, #1243 | #1248 |
| 19:37–19:38Z | #1242, #1241 | #1254, #1255 |
| 22:17Z | #1233 | #1253 |

No repeat of the "PR-body prose auto-closed a live defect" failure this window.

### The known-failing test — path in the round brief was WRONG
It is **`tests/integration/test_main_app.py`**, not `tests/unit/api/test_main_app.py`.
The wrong path makes pytest answer *"file or directory not found"*, collect 0 items and
**exit without a failure** — a failed probe reading as a pass, the exact trap the round
brief warns about. Corrected to all three lanes mid-flight.

Reproduced on current `main` `e1cf27371`:
```
E   AssertionError: assert 'degraded' == 'healthy'
FAILED tests/integration/test_main_app.py::test_application_uses_configuration_defaults
1 failed, 2 warnings in 49.14s
```
It patches `os.environ` with `clear=True` down to `{"CHAT_PROVIDER":"openai",
"REDIS_HOST":"localhost"}`, so no API key resolves, the registry falls through to
`local` only, and `/health` reports `degraded`. Environment-shaped; survived both #1253
and #1259. **Pre-existing. Not a regression. Do not "fix" it.**

Consequence worth carrying: it is an **integration** test. CI runs `pytest tests/`, not
`pytest tests/unit/`. A unit-only local run cannot see an integration break you caused.

## ⚠ CORRECTION: `faultmaven-kb-toolkit#29` has NO forcing function today

The previous board asserted that the `_REGEX_SYMBOLS` allowlist "is the lever that makes
upstream CI refuse to pass until the mirror lands." **That is false as stated.** The lever
is real but **has not been pulled**. Measured, not argued:

1. `faultmaven` post-#1255 carries the comment countermeasure —
   `CODE_FENCE_LINE_RE:104`, `CODE_SPAN_RE:108`, `mask_html_comments:217`,
   `iter_cause_blocks:266` in `modules/knowledge/domain/services/runbook_grammar.py`.
2. `faultmaven-kb-toolkit` @ `371a517` carries **none** of them — no `mask_html_comments`,
   no `CODE_FENCE_LINE_RE`, no `CODE_SPAN_RE`. The kb-toolkit#29 defect is live in the
   pack builder.
3. Upstream `scripts/check_grammar_cross_repo.py` `_REGEX_SYMBOLS` lists **7** symbols
   (`CAUSE_HEADING_RE`, `STEP_HEADING_RE`, `INDICATOR_TOKEN_RE`, `STEP_REF_RE`,
   `HTML_COMMENT_RE`, `INTERVENTION_RE`, `CHAIN_RUNG_RE`) plus `CONVERGES_REF`.
   Occurrences of the two countermeasure symbols in that file: **0**.
4. #1255 changed **none** of the 7 allowlisted assignments, so the gate sees no drift.
5. The gate therefore **passes** across the real trees:
   ```
   $ python scripts/check_grammar_cross_repo.py <faultmaven> <kb-toolkit>
   v4 parse grammar is in sync across both repos.
   EXIT=0
   ```
6. **The gate is not inert** — mutation-proved. Injecting `MUTATED` into the
   allowlisted `HTML_COMMENT_RE` on a scratch copy:
   ```
   ::error::v4 parse grammar 'HTML_COMMENT_RE' differs across repos:
       kb-toolkit runbook_grammar.py : '<!--.*?-->'  flags=DOTALL
       faultmaven runbook_grammar.py : 'MUTATED<!--.*?-->'  flags=DOTALL
   EXIT=1
   ```
   So EXIT=0 on the real trees is a genuine pass, not a dead script.

This is precisely the "fail upstream CI rather than pass quietly" outcome that
`runbook_grammar.py`'s own MIRROR NOTE says the allowlist addition is needed to prevent —
and the addition has not been made. Arming it is a **one-line change in a different repo**
(add `CODE_FENCE_LINE_RE` and `CODE_SPAN_RE` to `_REGEX_SYMBOLS`); both are named publicly
on the faultmaven side specifically so that stays one line.

**OWNER DECISION, deliberately not taken by the PM lane:** arming the lever turns
kb-toolkit CI **red until #29 is fixed**, blocking unrelated kb-toolkit merges. That
sequencing is a call about someone else's repo, which is the narrow definition of
"genuinely unrelated" under the fix-at-root rule. Options: arm now and fix #29 under a red
gate; fix #29 first and arm in the same PR (keeps CI green, loses the forcing function in
the interim); or arm on a schedule.

## ⬅ ROUND 5 — MERGED. Three ROOT lanes, two review rounds each.

| PR | Root | Closes | Base | Round-1 CI |
|----|------|--------|------|-----------|
| **#1261** | C — runbook_id collision | `#1258` | `e1cf27371` | 12/12 green |
| **#1262** | B — caller text in prompts | `#1256` | `e1cf27371` | 12/12 green |
| **#1263** | A — clarification recovery | `#1245`, `#1244` | `e1cf27371` | green bar 3 tests |

**All three lanes extended past their issue statement rather than ticketing.** That is
the rule working. Each also came back with review findings serious enough to require a
second round — CI green is not the bar; the reviews found what CI cannot.

### Root B's discovery invalidated the premise #1228 shipped on
`fence.py` claimed `<conversation_history>` "passes through `sanitize_user_input` on its
own path". **It does not.** `sanitize_user_input` runs once on *this turn's* argument;
the transcript is replayed from `case.messages`, which `InvestigationService.process_turn`
appends verbatim. Dev DB: **2150 user messages, 42 carry a raw `<`**, including a real
#666 instance — *"Consumer lag has increased from `<1000` to `>250000`"*. So it was the
one **unprotected** caller-controlled channel, not a differently-protected one. #1228's
scope-out was reasoned from a false premise.

### Confirmed by PM execution — regressions the PRs introduce

**#1262 re-commits #1256's own defect in a new form.** `fence.element()` applies
`PromptFence.terminator`, and `_ends_inside_tag` is biased to false positives:
```
POSITIVE CONTROL  'plain text with no angle brackets' -> (False, '')
                  'a complete <tag>here</tag> closed' -> (False, '')
                  'is p99 latency <500ms expected?'   -> (True,  '')
                  'consumer lag is now <1000'         -> (True,  '')
                  'a < b'                             -> (True,  '')
```
`a < b` is **issue #1256's own second example**. The PR removes `a < b → a &lt; b` and
replaces it with `a < b>[fence: …110 chars…]`.

**#1262 silently drops the conversation slot under budget pressure.** `reseal` on a tail
shorter than the ~40-char closing delimiter returns `""`:
```
POSITIVE CONTROL reseal(untruncated) EMPTY? False
  keep=40 -> len=80 EMPTY=False
  keep=35 -> len= 0 EMPTY=True     <- whole slot dropped
```
`main` renders a fragment carrying the latest turn; the branch renders nothing. Violates
`_truncate_to`'s INV-4. The branch's own sweeps `continue` on an empty block, so the
suite cannot catch it — the failed-probe trap in test form.

**#1263 ships the bug it was written to prevent.** The lane qualifies `label` (587, 602)
but not `payload` (589, 604), and `IntentResolver._exact_match` tests **`payload` first**.
`_clarification_subject` returns the same `submission_phrase` for any two pastes, so
payloads are byte-identical and a typed answer reclassifies the **wrong file** — the exact
exposure the lane was chartered to bound. Its labels-unique probe read `True` while the
first-checked channel collided underneath.

**#1261 asserts an invariant it violates.** Migration 047's widened CHECK admits
`'cancelled'`; `ConversionStatus` has no `CANCELLED` member, so such a row 500s
`GET /knowledge/conversions/{id}` — beside a new comment reading *"Keep this list and
`ConversionStatus` in step"*. This is the **tracked** class **#520**, whose text names this
exact pair; Root C fixed one half unknowingly.

### Filed this round — #1264 (the only genuinely-unrelated discovery)
Persisted turn counter does not advance on SERVICE-dispatched turns: `turn_history` is
appended **only** in `milestone_engine.py:5993` / `:12477`, both repositories persist
`effective_current_turn`, so in-flight turn numbers **repeat** across a reload. Verified
before filing. Blast radius beyond suggestions: `case_messages.turn_number`,
`uploaded_files.uploaded_at_turn`, hypothesis confidence decay, evidence-need windows.
Error direction is consistent — ages **under**-counted, so stale things stay alive.
Fix is a design call between two non-equivalent options; owner's.

### Owner decisions outstanding from this round
- **`faultmaven-kb-toolkit#29` lever** — arm it (kb-toolkit CI red until #29 lands) or fix
  #29 first and arm in the same PR. See the correction section above.
- **`CaseService.add_case_query` is dead code** (no caller). It is the helper #1228's
  reasoning assumed was the transcript writer. Delete or keep.
- **#520** — its `ConversionStatus` arm is being closed in #1261; `DraftStatus` and
  `CaseReport` arms remain open.

---

## ROUND 5 OUTCOME — all three merged, combined HEAD verified

`main` = **`cead40fd5`**.

| PR | Merge commit | Closes |
|----|--------------|--------|
| #1261 Root C | `79196b827` | #1258 |
| #1262 Root B | `07917055d` | #1256 |
| #1263 Root A | `cead40fd5` | #1245, #1244 |

### Reconciliation — CLEAN
Exactly **four** issues closed, one set per PR, nothing collateral. Everything held as a
non-closing reference stayed open: **#918, #1264, #520, #1246, #1251, #1252,
kb-toolkit#29**. No repeat of the prose-auto-close failure.

**#1244 was a doc issue, so status was not accepted as evidence.** Its three claims were
re-measured on merged main: pre-#1198 payload wording **0** hits (was present),
`file_reclassification` contract **3** (was 0), one-attachment premise **0** (was
present). Substantively fixed.

### Combined-HEAD full suite — CLEAN
```
12759 passed, 122 skipped, 1 deselected, 1 xfailed, 16 warnings in 1833.34s (0:30:33)
```
Zero `FAILED`/`ERROR` lines. Each PR had been green only against its OWN base and none
against the other two, so this was the first run of the three together.

### ⚠ CORRECTION: the "1 pre-existing failure" is ORDER-DEPENDENT, not simply failing
Previous boards recorded `tests/integration/test_main_app.py::
test_application_uses_configuration_defaults` as a standing failure. Measured both ways
on `cead40fd5`:

- **inside the full suite → PASSES** (it is in the log, and the suite reports 0 failed);
- **standalone → FAILS** with `assert 'degraded' == 'healthy'`.

It patches `os.environ` with `clear=True`, so alone there is no API key and the provider
chain falls to `local`; in a full run an earlier test has already warmed the registry.
**Consequence: CI (`pytest tests/`) is green.** Anyone running that one test alone sees a
red that is not a regression. Do not "fix" it, and do not quote it as a standing failure.

### Composition check on merged main
```
#1256  'a < b && c > d'            -> VERBATIM      (escape gone)
#1262  'a < b'                      ends_inside_tag=(False,'')   no false terminator
       'cut through <uploaded_file' -> (True,'')    security intact
#1263  shared payload               -> None          ambiguity refused
```

### Trap caught this round
The first combined-suite launch used `--timeout=600`; pytest-timeout is **not installed**,
so pytest died instantly with `unrecognized arguments` **while the `nohup &` launcher
returned exit 0**. A collection positive control (12,882 collected) was run before the
real suite. This is the fourth instance of the failed-probe-reads-as-a-pass class.

## Follow-on opened: PR #1266 (#1246)
Removes the drifting counts from `faultmaven/CLAUDE.md` rather than correcting them, and
pins the head with `tests/unit/architecture/test_claude_md_pins_no_migration_head.py`
(root revision exempt, exemption DERIVED via `down_revision is None`, three positive
controls). The head had drifted **twice** — stale when #1246 was filed, stale again by
047/048 during it.

## ⛔ OWNER ITEMS STILL OPEN
1. **`/home/swhouse/product/.claude/CLAUDE.md` line 26** says "12 import-linter
   contracts"; lines 17 and 138 of the same file say 13. **13 is correct** — the file
   contradicts itself. NOT fixed here: separate git repo, no remote, uncommitted local
   work in it (`CLAUDE.md`, `agents/README.md`, `commands/README.md`, `settings.json`,
   `settings.local.json` all modified).
2. **kb-toolkit#29 lever unarmed** — gate passes `EXIT=0`; mutation-proved live
   (`EXIT=1`). Arming = add `CODE_FENCE_LINE_RE`, `CODE_SPAN_RE` to `_REGEX_SYMBOLS`
   upstream; reddens kb-toolkit CI until #29 lands.
3. **`CaseService.add_case_query` dead code** — delete or keep.
4. **#1264** turn-counter design call. Comment on the issue records that
   `MockCaseRepository` cannot express the defect, so 5 test files' worth of doubles
   cannot catch it.
5. **#520** — `ConversionStatus` arm closed by #1261; `DraftStatus`/`CaseReport` remain.
6. `sudo rm -rf /home/swhouse/pgdata-1227` (owner `pcp:swhouse`, mode 700).

---

## SESSION CLOSED — all follow-ons merged

| PR | Merge | Closed |
|----|-------|--------|
| #1266 | `8593c9508` | #1246 |
| #1268 | `9709e2228` | (no issue — dead-code removal) |
| #1269 | `99849cce3` | #520 |
| kb-toolkit#30 | `4db14b849` | kb-toolkit#29 |

### Reconciliation — CLEAN
Closed as intended: **#1246, #520, kb-toolkit#29**. Stayed open as intended:
**#918, #1257, #1251, #1252**.

**#1264 closed, and it was NOT one of ours.** Verified: closed by PR #1267
(`fix(turn-accounting): record a turn for every route that consumes one`) from
another workstream — the legitimate fix. None of the three merge commits above
reference #1264. No stray auto-close in this window either.

### Owner items — DONE
- `/home/swhouse/pgdata-1227` **removed** (confirmed absent).
- Workspace `/home/swhouse/product/.claude/CLAUDE.md`: the three import-linter
  counts are **deleted**, not corrected — same principle as #1246. It had drifted
  to `12` inside an otherwise-good accuracy pass while `lint-imports` reports 13.
  **Left UNCOMMITTED** on purpose: that repo has no remote and carried a peer's
  in-progress edits (LangGraph→milestone engine, `preprocessing` added, Dashboard
  tabs, local-dev description) — all preserved.

### What this round established that outlives it
1. **Fix at the root, and the root is often wider than the issue.** All three
   lanes extended past their issue statement; #1262 found #1228's scope-out rested
   on a FALSE premise (`<conversation_history>` was never covered by
   `sanitize_user_input` — 42/2150 dev-DB messages carry a raw `<`).
2. **CI green is not the bar.** All three lanes were 12/12 green in round 1 and
   all three still shipped a defect a review found — twice, the very bug the PR
   was written to prevent (#1263's `payload` channel; #1262's `a < b` re-mangling).
3. **A guard must be shown to bite.** kb-toolkit#29's "forcing function" did not
   exist; the gate passed `EXIT=0` over a genuinely divergent grammar. It now
   fails on the pre-fix tree.
4. **Measure the blast radius, don't relay it.** kb-toolkit#30's impact on shipped
   runbooks was measured directly: 91 runbooks / 640 causes / byte-identical
   (1,404,499 bytes), 0 changed — with a positive control (a comment planted
   INSIDE a cause body) proving the harness detects change. A first control
   planted in the section preamble found nothing and would have looked like proof.
