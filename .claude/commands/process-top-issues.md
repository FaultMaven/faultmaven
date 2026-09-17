---
description: Run one round of issue processing — check the last round landed, propose five items plus the questions blocking others, build what is approved, report. Opens PRs; never merges.
allow_all_tools: true
---

# /process-top-issues

One round of `docs/development/issue-processing.md`. Read that document
first; this file is its runnable form and does not restate its reasoning.

You are the **owning agent**. You propose, dispatch, verify and report.
Subagents work single items. Nobody merges.

The round's shape, and the reason for it: **every question is asked before
the work starts, never during it.** The owner is touched twice, briefly, and
the rest is autonomous.

## Argument

`$ARGUMENTS` — optional, and usually unnecessary. **Bare invocation is
always correct**: step 0 reads where the round has got to and continues from
there, so the operator never has to know.

- empty — continue from wherever the round is.
- a list of numbers (`1452 1447`) — **pin** these into the next round
  whatever their rank; the ranking fills the rest of the capacity. A pinned
  item that needs a ruling is reported as blocked rather than built.
- `propose` — force a fresh proposal even if one is outstanding.
- `build` — force the building step.

## 0. Locate the round

Read the last comment on the `Queue` issue and continue from what it is:

| last comment | state | do |
|---|---|---|
| none, or a round **result** | between rounds | step 1 |
| a **proposal**, no owner reply after it | waiting on the owner | report what it is waiting for, and stop. Do not re-propose |
| a **proposal** with an owner reply after it | answered | step 3, take the answers |

Never re-post a proposal that is merely unanswered. An unanswered question
is not a failure and repeating it is noise; it already appears in the next
proposal by construction.

## 1. Check the last round landed

The previous round's **result comment names the pull requests it opened**.
Those are the ones that must be merged before another round starts:

```bash
gh pr view <n> --json state,mergedAt      # for each PR that round named
```

Anything still open there means this round does not start: report what is
outstanding and stop. An open pull request from anything else — a change to
this procedure, another person's branch — is not this check's business. Do
not run a bare `gh pr list` and refuse on whatever it finds.

Before the first round there is no result comment, so there is nothing to
check and the round starts.

## 2. Propose

Find the pinned tracking issue whose title is exactly `Queue`:

```bash
gh issue list --state open --label tracking --search "Queue in:title" \
  --json number,title --jq 'map(select(.title == "Queue"))'
```

Create it if absent (`gh issue create --title Queue --label tracking`, then
`gh issue pin`) and say so in the report. Its body holds the two piles; the
last comment holds the last round.

Read what arrived since the last round's timestamp:

```bash
gh issue list --state open --limit 500 --json number,title,labels,createdAt,body
```

**Check the premise of anything you are about to rank.** `git fetch origin
main` now, then confirm the issue's named code points still say what it says.
Two of round 1's five approved P1s were already fixed by pull requests that
never cited them. An item whose premise looks dead goes into the round as a
**verification** lane, not a build lane.

Sort each new issue into **ready**, **blocked** or **yours** using *What
escalates* in the procedure. The third pile is work no agent can do — a live
deployment check, a console or credential an agent lacks. List it, never
rank it into a round. Compare it against the current candidates and place it; do not
re-sort the backlog. Re-rank an old loser only if a trigger fired: a new
priority label, another issue on the same seam, a citation, an age
threshold.

Post one comment on `Queue`:

```
## Round <N> — proposal

### Building
| # | kind | why in this round | done when |

One line on why the round is this size: five is a working batch size, not a
rule, and complexity and dependency move it either way.

### Needs your call (every blocked item, not only the new ones)
| # | the question | options | my recommendation | unblocks |

### Yours to run (never ranked into a round)
| # | what only you can do |

### Measurement
<python scripts/backlog_metrics.py --weeks 8>
```

Rewrite the `Queue` body with both piles and this round's timestamp. Use
`gh api -X PATCH` and read it back; `gh issue edit` can fail silently.

Then **stop and wait**. Do not build anything that has an open question
against it.

## 3. Take the answers

Read the owner's reply. For each answered question, record the ruling as a
comment on its own issue and move that issue to the ready pile; if the ruling
implies work, re-file the issue as the defect or feature that work is, with
the ruling as its spec. Unanswered questions stay blocked and go in the next
proposal unchanged.

## 4. Build

One subagent per approved item, each with a self-contained prompt carrying:
the issue and its full text, the ruling if it had one, what "done" means, and
the *Building* section of `docs/development/issue-processing.md` verbatim.

Mechanics the prompt adds:

- Defect, chore and investigation lanes work in a fresh worktree on
  `origin/main` fetched now:
  `git worktree add -b fix/<n>-<slug> .claude/worktrees/<n> origin/main`.
  Before pushing: `black`, `ruff`, `lint-imports`, `pytest tests/` (not
  unit-only), and `python scripts/check_contract_version.py` if
  `docs/reference/api/` moved. `Closes #<n>` only if the issue as written is
  delivered.
- A feature lane produces a spec as an issue comment, not a file under
  `docs/working/`, which is gitignored.
- An investigation lane commits its measurement script with a unit test.

The subagent returns: the pull request or comment URL, the exact commands it
ran with their tail output, `git status --short` of its worktree, and
anything unresolved.

**If a lane finds its item needs a ruling, pull it.** Stop that lane, record
the question on the issue, return the item to the blocked pile, and carry on
with the others. Do not ask the owner mid-round and do not guess.

Then per returned lane, in order:

1. **Verify.** Re-run the lane's test command yourself from its worktree and
   confirm the output matches what was reported. Confirm `git status` was
   clean or every leftover file is named — and that the work is actually on
   the pull request, not only on disk:

   ```bash
   git -C <worktree> rev-parse --short HEAD @{u}      # must agree
   gh pr view <n> --json commits --jq '.commits[-1].oid[0:9]'
   ```

   Verify the *fixes* as well as the findings. A finding the lane pushed back
   on gets your attention by default; one it accepted does not, and that is
   where a half-done fix survives. Re-run the measurement that failed, not
   the report of it.
2. **Review.** `/code-review` on the pull request's final head. On-seam
   defect goes back to the lane for one fix commit; a design call becomes a
   question for the next proposal, not a mid-round interruption; an off-seam
   defect becomes a new issue carrying `Found while working on #<n>`.
3. **Delta.** Re-review the new head. A finding surviving two rounds is
   escalated, not iterated — unless it **blocks the merge**, in which case it
   goes back however many rounds it takes, because escalating it would hand
   the owner a pull request you know is broken. Blocking means the change is
   worse than the bug it fixes for someone who has not hit the bug. Say in
   the result how many findings you filed rather than fixed.
4. **Never relay a finding you could not reproduce by running it.**

## 5. Report and hand back

Comment on the round's proposal:

```
## Round <N> — result

| # | outcome | link | CI | review rounds |

Pulled: #N — <the question that pulled it>
Filed on the way: …
Waiting on you: merge the pull requests above.
```

Then stop. The round ends when the owner merges.

## Rules

- **Never merge.** Not on green CI, not on a clean review. Only an explicit
  per-pull-request instruction from the owner delegates one, and it covers
  that one.
- **Never start a round over an unfinished one.**
- **Never ask a question mid-build.** Pull the item instead.
- **Never build an item with an unanswered question.**
- **Never stack.** A pull request that would depend on another unmerged one
  is not opened; say the ordering in the report instead.
- **Never relay an unverified finding as a defect.**
- **Every fifth round**, read the procedure as a state machine: name what
  moves an issue out of each state and who does it. A state with no exit is
  a leak and prose hides it.
