---
description: Run one project of the issue-processing procedure — check the last one finished, pick up to five issues, work them in isolated lanes, review, report. Opens PRs; never merges.
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

A **project** is up to five issues worked together. One runs at a time, and
it ends when its pull requests are merged, so this command starts by
checking that the last one did.

## Argument

`$ARGUMENTS` — optional. `refresh-only` stops after step 2 and posts the
refreshed queue. An issue number list (`1452 1447`) overrides **which items
are dispatched** this run. Empty picks the project in step 2.

**Step 2 always runs, including under an override.** The override chooses
this project's items; it does not skip the check that the previous project
finished. An override while a project is still running is refused and said
so in the report. Otherwise the override is a side door into starting a
second project on top of an unfinished one.

## Procedure

### 1. Measure

```bash
python scripts/backlog_metrics.py --weeks 8
```

Keep the output; it goes in the report. It tells you whether the residue is
growing, by week and by age; WHICH seam it is growing on comes from step 2's
classification, because the script has no code dimension.

### 2. Check the last project finished, then pick this one

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

**First confirm the previous project finished** (procedure §1: one project
at a time, and it ends when its pull requests are merged):

```bash
gh pr list --state open --json number,title,headRefName   # must be empty
```

An unmerged pull request from the last project, or a ruling it asked for and
never got, means this cycle reports what is outstanding and stops. The one
exception is an item deliberately returned to the pool because its ruling
never came; §1's valve.

Then read what arrived since the last refresh, not the whole backlog:

```bash
gh issue list --state open --limit 500 --json number,title,labels,createdAt
```

Classify every issue not classified before: kind (`defect`, `decision`,
`investigation`, `feature`, `chore`); N and the scan that produced it if it
is a duplicated-rule item; and, once campaign item 1 has landed
`docs/development/invariants.md`, whether it is an instance of a rule
already in the register (until then, say in the report that the register
does not exist yet and classify from the issue text alone).

Evaluate each new issue against the current candidates and place it, and
re-rank a previous loser only if its trigger fired (§1: a new priority
label, another issue on the same seam, a citation, an age threshold). Then
pick this project's items from the pool — up to five, fewer if fewer are
worth doing.

Rewrite the `Queue` body: this project's items, the candidate pool in rank
order, and the timestamp of this refresh (the next cycle reads it to know
what "since the last refresh" means). Every top-five entry carries
rank-and-why, kind, done-when, N (if applicable) and blocked-by. Write the
body with `gh api -X PATCH` and read it back — `gh issue edit` can fail
silently.

If `$ARGUMENTS` is `refresh-only`, post the report (step 6) and stop.

### 3. Check the lanes can run in parallel

Two top-five items that touch the same files, the API contract version or
the alembic head do not run together. Sequence them and say so in the report.

### 4. Dispatch one subagent per item

A project is dispatched once, so no item is worked twice. The guard that
makes that true is step 2's: a new project does not start while the
previous one has an unmerged pull request.

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
- A decision the escalation list leaves to you is **decided and recorded**
  in the same cycle rather than queued as a memo for the owner. Recording
  it takes the same exit an owner ruling takes: the issue is re-filed as a
  defect or feature with the ruling as its spec, and it closes when that
  work lands. Deciding is not the same as finishing, so a decision that
  implies code does not close on being decided.
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

### Selection
Picked: … (why each). New issues classified: N (dup-rule: M). Losers
re-ranked because a trigger fired: … . Returned to the pool for want of a
ruling: … .
Merged PRs reviewed this window: N of M. (The stopping condition cannot be
read from a window where this share fell: fewer reviews means fewer
filings without meaning fewer defects.)

### Waiting on you (what is stopping this project finishing)
| # | what is being asked | the options | asked on |
|---|---|---|---|
```

This table is what the project is blocked on, so it is short by
construction and empty when the project is done. There is no separate
register of things ageing in the background, because a project that has not
finished has not been replaced by another one.

Then stop. Merging, and every item on the procedure's escalation list, is
the owner's.

### 7. Check this procedure for leaks

The cycle is the thing that runs again, so it is the thing whose faults
compound. Two checks, both cheap:

- **If a defect reached `main` that one of §2's gates should have caught,
  file an issue against the procedure** and name the gate that missed it.
  The gates are only worth what they catch, and nothing else in this cycle
  notices when one is inert.
- **Every fifth project, read `docs/development/issue-processing.md` as a
  state machine, not as prose.** For each state, name what moves an item
  out of it and who does that. A state with no exit is a leak, and it is
  invisible when the document is read as description. The four leaks fixed
  in this file's history — a ranking that maintained five items of
  sixty-three, an item re-dispatched into a second pull request, an
  escalation with no return path, and an agent-decided decision with no
  exit — were all found that way and by nothing else. Two of the four
  turned out on review to be over-stated, and the fix for one of them was
  itself wrong, which is the other half of the argument for the check:
  read the machine, then read the fix as a machine too.

## Rules

- **Never merge.** Not with green CI, not with an addressed review. Only an
  explicit per-PR instruction from the owner delegates a merge, and it
  covers that PR alone.
- **Never stack.** A PR that would depend on another unmerged PR is not
  opened; the lane waits and says so in its report.
- **Never relay an unverified finding** as a defect.
- **Never edit a queue entry's rank without recording why** in the entry.
- **Never start a project over an unfinished one.** An unmerged pull
  request means the last project has not ended.
- **Never leave an agent-decidable decision undecided.** Decide it, record
  why on the issue, and re-file it as the work the ruling implies. Do not
  close it unless deciding was the whole of it.
