---
description: Run one cycle of the issue-processing procedure — refresh the queue, work the top five in isolated lanes, review, report. Opens PRs; never merges.
allow_all_tools: true
---

# /process-top-issues

One cycle of `docs/development/issue-processing.md`. Read that document
first; this file is the procedure's runnable form and does not restate its
reasoning. You are the **owning agent**: you refresh, dispatch, verify and
report. Subagents work individual items. Nobody merges.

## Argument

`$ARGUMENTS` — optional. `refresh-only` stops after step 2 and posts the
refreshed queue. An issue number list (`1452 1447`) overrides the top five for
this run. Empty runs the full cycle on the current top five.

## Procedure

### 1. Measure

```bash
python scripts/backlog_metrics.py --weeks 8
```

Keep the output; it goes in the report. Note which seams the residue is
growing on.

### 2. Refresh the queue

Find the pinned issue titled `Queue` (`gh issue list --label tracking
--search "Queue in:title"`). If none exists, create it with an empty table
and say so in the report.

For each entry in the top five, check in this order and drop on the first
failure: still open; not closed as a duplicate; no PR merged since the last
refresh touched its seam (`gh pr list --state merged --search <seam
keyword>`); still the kind it was filed as. Promote from below the line.

Classify every issue opened since the last refresh: kind (`defect`,
`decision`, `investigation`, `feature`, `chore`); whether it is an instance
of a rule already in `docs/development/invariants.md`; N and the scan that
produced it if it is a duplicated-rule item. Rank it in by the rule in the
procedure's §1.

Rewrite the `Queue` body. Every top-five entry carries rank-and-why, kind,
done-when, N (if applicable) and blocked-by. Write the body with
`gh api -X PATCH` and read it back — `gh issue edit` can fail silently.

If `$ARGUMENTS` is `refresh-only`, post the report (step 6) and stop.

### 3. Check the lanes can run in parallel

Two top-five items that touch the same files, the API contract version or
the alembic head do not run together. Sequence them and say so in the report.

### 4. Dispatch one subagent per item

Each subagent gets a self-contained prompt (it inherits nothing from this
conversation) that contains: the issue number and its full text; the queue
entry (kind, done-when, N, blocked-by); the lane procedure from
`docs/development/issue-processing.md` §2 verbatim; and the exit for its
kind:

- **defect** → a PR from a fresh worktree on `origin/main` fetched now
  (`git worktree add -b fix/<n>-<slug> .claude/worktrees/<n> origin/main`),
  containing the fix, the reproducing test, and for a duplicated-rule item
  the guard with a positive-control floor. PR body states N and the scan
  that found it, and lists the consumers of anything whose meaning changed.
  Runs `black`, `ruff`, `lint-imports`, `pytest tests/` (not unit-only), and
  `python scripts/check_contract_version.py` if `docs/reference/api/` moved.
  `Closes #<n>` only if the issue as written is delivered.
- **decision** → a decision memo as a comment on the issue: options, what
  each changes, which documented design each overrides, a recommendation.
  No code.
- **investigation** → a measurement as a comment on the issue, produced by
  a committed script under `scripts/` with a unit test, opened as a PR.
- **feature** → a spec under `docs/working/DRAFT-<slug>.md` opened as a PR,
  or a recommendation to close with reasons. No implementation.
- **chore** → a PR.

The subagent reports back with: the PR or comment URL, the exact commands
it ran with their tail output, `git status --short` of its worktree, and
anything it could not resolve.

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

### Escalations
- <what, which item, the two options>
```

Then stop. Merging, and every item on the procedure's escalation list, is
the owner's.

## Rules

- **Never merge.** Not with green CI, not with an addressed review. Only an
  explicit per-PR instruction from the owner delegates a merge, and it
  covers that PR alone.
- **Never stack.** A lane whose PR depends on another unmerged PR states
  the merge order in its body and waits.
- **Never relay an unverified finding** as a defect.
- **Never edit a queue entry's rank without recording why** in the entry.
