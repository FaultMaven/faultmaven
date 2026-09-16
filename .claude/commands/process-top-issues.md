---
description: Run one cycle of the issue-processing procedure — refresh the queue, work the top five in isolated lanes, review, report. Opens PRs; never merges.
allow_all_tools: true
---

# /process-top-issues

One cycle of `docs/development/issue-processing.md`. Read that document
first; this file is the procedure's runnable form and does not restate its
reasoning. It also defines the two words used throughout both files: a
**seam** is all the places one rule has to hold, and a **head** is the
newest commit on a branch (distinct from the alembic head, which is the
newest database migration). You are the **owning agent**: you refresh, dispatch, verify and
report. Subagents work individual items. Nobody merges.

## Argument

`$ARGUMENTS` — optional. `refresh-only` stops after step 2 and posts the
refreshed queue. An issue number list (`1452 1447`) overrides **which items
are dispatched** this run. Empty runs the full cycle on the current top five.

**Step 2 always runs, including under an override.** The override chooses
what to work on; it does not skip setting states. An overridden item that is
`awaiting merge`, `awaiting ruling` or `blocked` is refused and said so in
the report, exactly as a promoted one would be. Otherwise the override is a
side door back into dispatching work that is already done.

## Procedure

### 1. Measure

```bash
python scripts/backlog_metrics.py --weeks 8
```

Keep the output; it goes in the report. It tells you whether the residue is
growing, by week and by age; WHICH seam it is growing on comes from step 2's
classification, because the script has no code dimension.

### 2. Refresh the queue

Find the pinned issue titled `Queue`:

```bash
gh issue list --state open --label tracking --search "Queue in:title" \
  --json number,title --jq 'map(select(.title == "Queue"))'
```

The exact-title filter matters: `in:title` is a token match, and a later
tracking issue whose title merely contains "Queue" would otherwise be the
one whose body gets rewritten.

If none exists, create it so the next cycle finds it by the same predicate,
and say so in the report:

```bash
gh issue create --title "Queue" --label tracking --body "<empty table>"
gh issue pin <number>
```

**Read every open issue, not only the top five** (procedure §1: the ranking
is recomputed each cycle, so nothing sits below a line where it can rot):

```bash
gh issue list --state open --limit 500 --json number,title,labels,updatedAt
gh pr list --state open --json number,title,body,headRefName   # for state
```

Set each issue's state. The three waiting states are read from GitHub, not
remembered:

- **`awaiting merge`** — an open pull request names it (`Closes #N`, or a
  `fix/N-` branch). Do not dispatch it; the work is done on our side.
- **`awaiting ruling`** — the last cycle posted a memo or an escalation on
  it and the owner has not replied since. Do not dispatch it. Carry its age
  in cycles.
- **`blocked`** — its queue entry names a dependency that has not resolved.
- **`queued`** — everything else, and the only dispatchable state.

Classify every issue not classified before: kind (`defect`, `decision`,
`investigation`, `feature`, `chore`); N and the scan that produced it if it
is a duplicated-rule item; and, once campaign item 1 has landed
`docs/development/invariants.md`, whether it is an instance of a rule
already in the register (until then, say in the report that the register
does not exist yet and classify from the issue text alone).

Rank the whole `queued` set by the rule in the procedure's §1 and take the
top five. A previous top-five item now in a waiting state leaves the five
and the next `queued` item is promoted.

Rewrite the `Queue` body: the five in flight, every waiting item with its
state and age, and the timestamp of this refresh (the next cycle reads it
to know what "since the last refresh" means). Every top-five entry carries
rank-and-why, kind, done-when, N (if applicable) and blocked-by. Write the
body with `gh api -X PATCH` and read it back — `gh issue edit` can fail
silently.

If `$ARGUMENTS` is `refresh-only`, post the report (step 6) and stop.

### 3. Check the lanes can run in parallel

Two top-five items that touch the same files, the API contract version or
the alembic head do not run together. Sequence them and say so in the report.

### 4. Dispatch one subagent per item

**Before dispatching any item, re-check its state.** Do not dispatch one
that is `awaiting merge`, `awaiting ruling` or `blocked`. Without this the
same defect gets a second worktree and a second pull request, because the
issue is still open and looks dispatchable.

Each subagent gets a self-contained prompt (it inherits nothing from this
conversation) that contains: the issue number and its full text; the queue
entry (kind, done-when, N, blocked-by); and `docs/development/issue-processing.md`
§1 "Kinds and their exits" and §2 "The lane procedure" **verbatim** — the
exits are defined there once and are not restated here. The mechanics the
prompt adds:

- Every lane traces to the root cause before deciding what it ships, and
  designs the fix for the class, not the reported copy (procedure §2, "Root
  before scope"). A lane that reports a symptom fixed with the root
  ticketed as "investigate later" is sent back; a lane that reports the root
  found, one part of its fix shipped, and the rest ticketed with the design
  is complete.
- A defect or chore lane works in a fresh worktree on `origin/main` fetched
  now (`git worktree add -b fix/<n>-<slug> .claude/worktrees/<n>
  origin/main`), and before pushing runs `black`, `ruff`, `lint-imports`,
  `pytest tests/` (not unit-only), and `python scripts/check_contract_version.py`
  if `docs/reference/api/` moved. `Closes #<n>` only if the issue as written
  is delivered.
- A decision or feature lane writes no code: its memo or spec is a comment
  on the issue (`gh issue comment <n> --body-file …`). `docs/working/` is
  gitignored and is not a place a PR can carry a spec.
- A decision the escalation list leaves to you is **decided, recorded and
  closed** in the same cycle, not queued as a memo. A decision an agent may
  make and does not make is a decision that will be made again next cycle.
- An investigation lane commits its measurement script under `scripts/`
  with a unit test and opens a PR for that; the numbers go in an issue
  comment.

The subagent reports back with: the PR or comment URL, the exact commands
it ran with their tail output, `git status --short` of its worktree, and
anything it could not resolve.

**A lane that has not returned when the cycle ends is not lost.** Report it
as `in flight`, leave its item in that state, and do not dispatch it again
next cycle. A lane still `in flight` two cycles later is escalated with
whatever it last reported: a lane that cannot finish is a defect in the
queue entry or in the lane procedure, and a third attempt will not discover
which.

### 5. Verify, review, rework

For each returned lane, in order:

1. **Verify the report.** Open the PR; confirm `git status` was clean or
   every leftover file is named. Re-run the lane's test command yourself
   from the worktree and confirm the output matches what was reported.
2. **Review.** Run `/code-review` on the PR's final head. Classify every
   finding: on-seam defect → send the subagent back with the finding for
   one fix commit; design call → PR comment for the owner; off-seam defect
   → new issue with a `Found while working on #<n>` line.
3. **Delta pass.** After the fix commit, re-run `/code-review` on the new
   head and the lane's own tests. A finding that survives two rounds is
   escalated, not iterated.
4. **Substantiate before relaying.** A finding you cannot reproduce by
   running it is reported as unverified, never as a defect.

Do not push to a PR that has been merged. If the owner merged mid-cycle,
remaining corrections open a new PR.

### 6. Report

Post one comment on the `Queue` issue:

```
## Cycle <date>

### Metrics
<backlog_metrics output, weeks table + residue line>

### Items
| # | kind | outcome | link | review | escalated |
|---|---|---|---|---|---|

### Refresh
Dropped: … (why). Promoted: … . New issues classified: N (dup-rule: M).
Merged PRs reviewed this window: N of M. (The stopping condition cannot be
read from a window where this share fell: fewer reviews means fewer
filings without meaning fewer defects.)

### Awaiting you (every cycle, until answered)
| # | waiting since | what is being asked | the options |
|---|---|---|---|

### Escalations new this cycle
- <what, which item, the two options>
```

The **Awaiting you** table is not optional and is not trimmed. It repeats
every outstanding ruling, with its age in cycles, on every cycle until the
owner answers. It is the only thing in this procedure that makes the
owner's own queue visible, and the #1453 triage found nine items in that
state before the first cycle had run.

Then stop. Merging, and every item on the procedure's escalation list, is
the owner's.

### 7. Check this procedure for leaks

The cycle is the thing that runs again, so it is the thing whose faults
compound. Two checks, both cheap:

- **If a defect reached `main` that one of §2's gates should have caught,
  file an issue against the procedure** and name the gate that missed it.
  The gates are only worth what they catch, and nothing else in this cycle
  notices when one is inert.
- **Every fifth cycle, read `docs/development/issue-processing.md` as a
  state machine, not as prose.** For each state, name what moves an item
  out of it and who does that. A state with no exit is a leak, and it is
  invisible when the document is read as description. The four leaks fixed
  in this file's history — a ranking that maintained five items of
  sixty-three, a waiting item re-dispatched into a second pull request, an
  escalation with no return path, and an agent-decided decision with no
  terminal state — were all found that way and by nothing else.

## Rules

- **Never merge.** Not with green CI, not with an addressed review. Only an
  explicit per-PR instruction from the owner delegates a merge, and it
  covers that PR alone.
- **Never stack.** A PR that would depend on another unmerged PR is not
  opened; the lane waits and says so in its report.
- **Never relay an unverified finding** as a defect.
- **Never edit a queue entry's rank without recording why** in the entry.
- **Never dispatch a waiting item.** An open pull request or an unanswered
  memo means the work is with someone else.
- **Never let a ruling age out of sight.** Every unanswered escalation is
  repeated in every report until it is answered.
- **Never leave an agent-decidable decision undecided.** Decide it, record
  why on the issue, close it.
