---
description: Run one round of issue processing — settle what the last round left, propose a round of items plus the questions blocking others, build what is approved, report. Opens PRs; never merges.
allow_all_tools: true
---

# /process-top-issues

One round of `docs/development/issue-processing.md`. Read that document
first; this file is its runnable form and does not restate its reasoning.

You are the **owning agent**. You propose, dispatch, verify and report.
Subagents work single items. Nobody merges.

The round's shape, and the reason for it: **every question is asked before
the work starts, never during it.** A round touches the owner twice —
answers, then merges — beside one standing action outside it, the *yours*
pile. The rest is autonomous.

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

## The Queue

Everything below reads or writes one pinned tracking issue titled exactly
`Queue`. Its **body** holds the three piles, the ranked head in order and the
round timestamp; its **last comment** is where the round has got to.

```bash
gh issue list --state open --label tracking --search "Queue in:title" \
  --json number,title --jq 'map(select(.title == "Queue"))'
```

Create it if absent (`gh issue create --title Queue --label tracking`, then
`gh issue pin`) and say so in the report.

**Writing the body.** Three steps write it — the settlement (1), the proposal
(2) and a pull (4) — and each writes the *whole* body, because a PATCH on
`body` replaces it. So:

- **Emit all three parts every time**: the piles, the ranked head in order,
  and the timestamp. A write that emits the piles alone deletes the head, and
  with it every rules-1-3 comparison ever made — which is what "losing a
  comparison does not have to be undone" rests on. The rule-4 tier is not
  written down; oldest-first is recoverable from the issues.
- **The timestamp belongs to step 2.** Steps 1 and 4 carry forward the value
  they read. Stamping it in step 1 would make step 2's own "what arrived
  since the last round" read, seconds later, find nothing, and every issue
  filed during the last round would be sorted into no pile at all.
- **`gh api -X PATCH`, then read it back.** `gh issue edit` can fail
  silently. At the pull site a write lost that way re-creates the leak the
  write exists to close, and nothing else would ever say so.
- **The owning agent makes every one of these writes.** A subagent never
  touches the `Queue`. There is no compare-and-set here, so two lanes writing
  at once would each compute from a read taken before the other's, and the
  first pull's move to **blocked** would vanish — the same leak by a third
  road.
- **The body names only open issues.** Step 2 enforces that when it rebuilds;
  steps 1 and 4 carry forward what they read, so the body can briefly name an
  issue closed since. Nothing acts on those numbers in between, and step 2's
  rebuild drops them.

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

## 1. Settle the last round

The previous round's **result comment names the pull requests it opened**.
Those are the ones that must be merged before another round starts:

```bash
gh pr view <n> --json state,mergedAt      # for each PR that round named
```

Any of them still open means this round does not start: report what is
outstanding and stop. An open pull request from anything else — a change to
this procedure, another person's branch — is not this check's business. Do
not run a bare `gh pr list` and refuse on whatever it finds.

Then settle. That check is per pull request; **the settlement is per
issue** — one pull request may carry several. Take the issue↔pull-request
pairs from **the result table itself**, which lists every item beside the
pull request that carried it; do not derive them from what the pull requests
cited, or a lane that cited nothing contributes no issue and is never
settled. Read each issue's own state, not the pull request's body.

```bash
gh issue view <n> --json state,title,body,comments   # each cited issue
```

Walk that table **one row at a time**. A row is an issue, the pull request
that carried it and an outcome, so what you do is decided by the row in front
of you and that issue's own state — never by another row, and never by the
issue alone. Take the **first** row here that matches; the order is what
lands on the fact rather than on the report of it, so a pull request the
owner reopened and merged is settled as merged even where the round reported
that item `pulled`:

| the row | what you do |
|---|---|
| the issue is closed | nothing |
| the issue is open, its pull request merged | close #<n> if every part it named is now delivered or re-filed, citing this pull request and the re-filings; otherwise edit #<n> down to the part that still stands — title and body — and put it back in **ready** as a fresh arrival, ranked against the current candidates and never left at its old rank. There is no third outcome |
| its outcome is `pulled` | nothing. The pull recorded it on the issue and put it in **blocked** where it happened. The row may name no pull request at all — a lane pulled mid-build never opened one |
| the issue is open, its pull request closed unmerged | the owner abandoned it — comment that the work was built and the pull request closed unmerged, link it, and move #<n> to **blocked**; the next proposal asks whether to build it another way or close it. Do not guess why |

A row matching none of them — open, naming no pull request, and not `pulled` —
is a malformed result. Say so under *Settled from last round* rather than
guessing; the item stays wherever the last body put it. If several pull
requests named one issue, the merged one decides.

**Every action above is safe to run twice**, so a stop between here and the
proposal costs nothing — but safe is not silent. Read the issue before you
write: a close on a closed issue, an edit down to a remainder the body
already carries, and a re-rank are all no-ops. The abandonment comment is the
one that is not, because it is free-form prose a re-run cannot reliably
recognise as its own. That duplicate is the whole price of having no marker,
and it was priced knowingly. Do not add a marker back — one used to be here,
and it turned that stop into the one state this procedure could not leave.

Then **write the `Queue` body**, per *The Queue*, with the piles as the
settlement leaves them. That write *is* the settlement: the piles are in that
body and nowhere else, and step 2's rebuild reads open issues and the body,
neither of which says an item was returned to blocked.

Report what you settled under *Settled from last round* in the proposal.

Before the first round there is no result comment, so there is nothing to
check and the round starts.

## 2. Propose

Read what arrived since the round timestamp in the `Queue` body:

```bash
gh issue list --state open --limit 500 --json number,title,labels,createdAt,body
```

Sort each new issue into **ready**, **blocked** or **yours** using *What
escalates* in the procedure. The third pile is work no agent can do — a live
deployment check, a console or credential an agent lacks. List it, never
rank it into a round. Compare each new issue against the current candidates
and place it; do not re-sort the backlog. Losing does not have to be undone
— the item keeps its place and the pile drains past it. Move one up only if
a trigger fired: a new priority label, another issue on the same seam, a
citation.

**Rebuild the piles from open issues.** Never carry a number forward from
the last `Queue` body without checking it is still open: a *yours* item the
owner has run and closed, and a blocked item closed by a "leave it" ruling,
are closed on GitHub and nowhere else.

**The first thing the round's capacity buys is the oldest rule-4 item** —
the oldest ready issue holding none of picking rules 1-3 — ahead of rules
1-3, unless the tier is empty or the pinned items left no capacity at all.
Rules 1-3 outrank that tier every time, so without the reserved place its
drain rate is zero. It does not bound the wait: report the tier's size in
*Measurement* so a tier growing across rounds is visible.

**Then check the premise of the items you are about to list under
*Building* — whatever their age, and before you write the comment.** `git
fetch origin main` now and confirm each one's named code points still say
what the issue says. This runs on the selected candidates, not on this week's
arrivals: round 1's two dead items were 49 and 58 days old, so checking only
new issues would have missed both. An item whose premise looks dead still
goes into the round — as a **verification** lane rather than a build lane.

Post one comment on `Queue`:

```
## Round <N> — proposal

### Building
| # | kind | why in this round | done when |

One line on why the round is this size, in terms of **independent seams and
the review rounds they will cost**, not item count: lanes are parallel and
review is not, so three items on three seams is a bigger round than five on
one, and an item whose own review will run several rounds — a security
boundary, a storage change, anything shipping a new guard — is a round by
itself.

### Needs your call (every blocked item, not only the new ones)
| # | the question | options | my recommendation | unblocks |

Deferred items belong in this table too, with the ruling and the condition
they wait on in place of the question and options. Listed, never re-asked —
and never omitted, or they leave every pile.

### Yours to run (never ranked into a round)
| # | what only you can do |

### Settled from last round
| PR | issue | what it did |

### Measurement
<python scripts/backlog_metrics.py --weeks 8>
Rule-4 tier: <n> ready items holding none of rules 1-3 (last round: <n>)
```

Write the `Queue` body per *The Queue*: the three piles, **the ranked head in
order**, and this round's timestamp, which is set here and nowhere else.

Then **stop and wait**. Do not build anything that has an open question
against it.

## 3. Take the answers

Read the owner's reply. For each answered question, record the ruling as a
comment on its own issue, then:

- **implies work** — re-file the issue as the defect or feature that work
  is, with the ruling as its spec, into **ready**.
- **defers** ("(b) **for now**", "(3) **at a second tenant**") — leave it
  **blocked** and record the condition that would revisit it. List it in the
  next proposal as answered-and-waiting, not as a question, and re-read the
  condition when you sort: it moves to ready the round it holds. Do not
  close it: a deferral is "not yet", not "never".
- **implies none** (the behaviour is right as it stands) — **close** the
  issue there, quoting the ruling. Do not move it to ready.
- **makes the work the owner's** — move it to **yours**.

A ruling lands in one of those four. If you cannot tell which, that is the
next question, not a guess.

Unanswered questions stay blocked and go in the next proposal unchanged.

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
  delivered; otherwise `Refs #<n>`, plus a comment on #<n> naming what the
  pull request delivered and which issues carry the rest. The next round's
  *Settle the last round* reads it to decide whether #<n> closes or is
  edited down.
- A feature lane produces a spec as an issue comment, not a file under
  `docs/working/`, which is gitignored.
- An investigation lane commits its measurement script with a unit test.

The subagent returns: the pull request or comment URL, the exact commands it
ran with their tail output, `git status --short` of its worktree, and
anything unresolved.

**If a lane cannot deliver its item, pull it** — because it needs a ruling,
because the work turns out to be several rounds of it, or because it cannot
be done from where the lane stands. Stop that lane, record on the issue the
question if there is one and otherwise what stopped it, return the item to
the blocked pile — **write the `Queue` body now**, per *The Queue*, because
step 2 has already run and nothing else will write it — and carry on with the
others. A re-entry into this step does not re-dispatch that item, whatever
the proposal still lists: the body places it in **blocked** and the issue
says what stopped it. Do not ask the owner mid-round and do not guess.

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
   goes back for as many rounds as the lane can clear it in, because
   escalating it would hand the owner a pull request you know is broken. **If
   the lane cannot clear it — for any reason, not only a ruling — pull it**:
   close the pull request, record on the issue either the question or that
   the lane could not clear it, return the item to the blocked pile (write
   the `Queue` body, as in the pull above), and give it a result row with
   outcome `pulled` — that row is what stops the next round's settlement
   reading your close as the owner's abandonment. That is the loop's only
   other exit, and without it the round cannot reach step 5 at all.
   Blocking means the change is worse than the bug it fixes for someone who
   has not hit it. Say in the result how many findings you filed rather
   than fixed.
4. **Never relay a finding you could not reproduce by running it.**

## 5. Report and hand back

Comment on the round's proposal:

```
## Round <N> — result

| # | outcome | link | CI | review rounds |

One row per **issue**, even where one lane delivered several under one pull
request: the next round's *Settle the last round* reads this table for its
issue-to-pull-request pairs, and an issue missing from it is never settled.
A pulled issue gets a row too. `outcome` is free text with **one reserved
value** — `pulled`, lower case, which nothing but a pull may carry and which
is the only value the next settlement reads. A mid-build pull's row names no
pull request, because none was opened. The line below carries what stopped
it.

Pulled: #N — <the question, or what stopped the lane>
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
  a leak and prose hides it. **If you edit the procedure, read your own
  result the same way before opening the pull request** — the pass that last
  edited it created two new leaks while closing others, and both were caught
  only by re-reading the result.
