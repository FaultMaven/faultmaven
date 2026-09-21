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

## The piles are labels

An issue's pile is a **label on that issue**, not a list in a shared blob:

| label | pile |
|---|---|
| `pile:ready` | the answer is known; it needs work, not a call |
| `pile:blocked` | needs an owner ruling, or a lane stopped on it |
| `pile:yours` | only the owner can run it — live deployment, credentials |

Create any that is missing before you use one; `gh issue edit` fails the
**whole** edit on a label the repository does not have.

Three rules when you apply `pile:blocked`, the first and last from a pile
that held at 12 for six rounds and then lost ten of twelve in one reading:

- **Say what it is blocked ON, in the issue BODY, in one form**, as you
  label it. "Needs an owner ruling" and "needs #N to land" look identical in
  a label and only the first is owner latency (#1513 was miscounted for six
  rounds). One line, the reference first:

  ```
  **Blocked on:** #1294 — the arithmetic moves when that ladder splits.
  **Blocked on:** an owner ruling on which axis owns the degrade policy.
  ```

  The body rather than a comment, because this is a current value that
  *Picking*'s promotion rule and `scripts/backlog_metrics.py` both read
  every round, and a thread holds a history. The reference first, because
  `**Blocked on:** an owner ruling on #1294's shape` is owner latency and
  must not read as a dependency. The metrics report how many blocked items
  state nothing, so a missing line is counted rather than invisible.
- **A blocked item whose named issue has closed goes to ready as you sort** —
  `--add-label pile:ready`, then `--remove-label pile:blocked`. Closing #N
  writes no label on anything waiting for it, so nothing else will notice.
  The metrics name these under *Rule-4 tier*.
- **Split a mixed issue as you label it.** Any part needing a ruling blocks
  the whole issue, so one design question freezes the mechanical work beside
  it — #985 held seven items its own text called "no design needed" since
  round 1. Cheap now, expensive later.

**Read a pile as a query, never as a parse:**

```bash
gh issue list --state open --label pile:blocked --limit 500 \
  --json number,title,labels,createdAt,body
```

`--state open` is why nothing needs a rule about carrying closed numbers
forward: a closed issue is not in the answer.

**Move an item with two separate edits, the add first:**

```bash
gh issue edit <n> --add-label pile:blocked      # then, as its own command:
gh issue edit <n> --remove-label pile:ready
```

Each single-direction edit is one mutation. `--add-label X --remove-label Y`
in one command is **not** one: `gh` fires two mutations concurrently, so a
stop between them can leave either — and "neither label" reads as an unsorted
arrival, which *Propose* would sort straight back into ready. Add first and
the only intermediate is "both", which the reading rule below settles.

**Reading, when the labels disagree:** more than one `pile:` label means
**blocked**, or **yours** if blocked is not among them. The restrictive
answer is deliberate and it is not symmetric: on a half-finished move *into*
blocked it is already right, and on one *out of* blocked it costs a re-ask
next proposal — which is the cheap direction to be wrong in, because the
expensive one would be dispatching a lane at an item that was pulled. Say so
in *Measurement* so a half-finished move is visible rather than merely safe.
**No** `pile:` label means an unsorted arrival, which step 2 sorts — and
nothing bare is ever dispatched either, because step 4 dispatches from the
ready query, which a bare issue is not in.

Adding a label already present is a no-op and so is removing one that is
absent, both exit 0, which is what makes every move below safe to repeat.
That is obtained from the medium rather than argued for in prose, and it is
why nothing here writes a marker recording that it has been.

The migration off the old enumerated lists has been **run** (2026-09-17):
`pile:ready` 64, `pile:blocked` 12, `pile:yours` 2, balancing against 79 open
issues with only the `Queue` itself unlabelled. It is not repeated here,
because the commands were keyed to the lists as they stood that day and a
re-run would re-add `pile:blocked` to anything a ruling has since moved to
ready — a second writer of pile membership, derived from a stale list, which
is the shape this whole scheme exists to remove. The run and its
reconciliation are recorded on the `Queue`.

## The Queue

One pinned tracking issue titled exactly `Queue`:

```bash
gh issue list --state open --label tracking --search "Queue in:title" \
  --json number,title --jq 'map(select(.title == "Queue"))'
```

Create it if absent (`gh issue create --title Queue --label tracking`, then
`gh issue pin`) and say so in the report. Its **newest comment of a
recognised kind** is where the round has got to — step 0 says which kinds
those are, and the last comment is not one of them by virtue of being last.
Its **body** holds what a label cannot: the **ranked head** in order, the
**round timestamp**, and a stamped snapshot of the three counts.

**Step 2 is the only writer of that body.** Membership is labels, so the
settlement and a pull change piles without touching it — which is what lets
the head live in a blob at all. Write it with `gh api -X PATCH` and read it
back; `gh issue edit` can fail silently.

- **The ranked head orders; the labels decide.** A name in the head that the
  ready query does not return is skipped wherever the head is read — step 4
  states that predicate, and it is the only one. So a pull does not have to
  edit the head, and a head naming a blocked, pulled or closed item is stale
  rather than wrong.
- **The counts are a snapshot, never read back.** Step 2 stamps them beside
  the timestamp because the body is also what a person reads; every decision
  takes its count from the query.
- **The timestamp is step 2's.** It is what "what arrived since the last
  round" is measured from.
- **The rule-4 tier is not written down** — oldest-first is recoverable from
  `createdAt`, and `scripts/backlog_metrics.py` recomputes the tier and names
  its oldest members on every run.

## 0. Locate the round

**Every comment on the `Queue` is authored by the same account** — the agent
posts with the owner's credential — so a state cannot be read off who wrote
one. It is read off the heading, and the agent gives its own writes exactly
four:

| heading the agent writes | what it is |
|---|---|
| `## Round <N> — proposal` | a recognised kind: a round proposed |
| `## Round <N> — result` | a recognised kind: a round reported |
| `## Round <N> — withdrawn` | a recognised kind: a round ended with nothing to report |
| `## Note — <what>` | never a state: a migration record, a handover, a checkpoint |

Anything else is **the owner speaking**. Write every board comment under one
of those four headings, or the next reader cannot tell your note from an
answer; five of the first ten rounds posted a note, and the one that closed
round 9 is what made round 10's step 0 guess.

Now scan back to the **newest comment of a recognised kind** — past notes,
past owner comments, however many — and read what follows it. Take the first
row that matches:

| the newest recognised comment, and what follows it | state | do |
|---|---|---|
| none | before the first round | step 1 |
| a **result** or a **withdrawal** | between rounds | step 1 |
| a **proposal**, with an owner comment after it | answered | step 3, take the answers |
| a **proposal**, no owner comment after it, but its *Building* items have lane pull requests | approved out of band and already built | post the approval as a `## Note —` so the next reader need not re-derive it, then step 4 (whose dispatch predicate skips what is built) and step 5 |
| a **proposal**, no owner comment after it, and no lane pull requests | waiting on the owner | report what it is waiting for, and stop. Do not re-propose |

```bash
# "its Building items have lane pull requests": the step-4 branch convention,
# over the proposal's own numbers, state ALL — an unposted result is exactly
# the case where those pull requests are already merged.
gh pr list --state all --limit 200 --json number,url,headRefName,state \
  --jq '.[] | select(.headRefName | test("/(1450|1511|1512)-"))'   # the round's numbers
```

The last two rows are one question — *was this round approved?* — asked of
the narration first and of the facts second. Round 3 was approved in a
working session rather than as a reply **and** its result was never posted,
so the narration said "unanswered" about a round whose work was merged. An
approval nobody wrote down is still visible in what it authorised.

`withdrawn` is written out of band, when a round is abandoned before it is
built. The heading exists so the next reader lands between rounds instead of
on a proposal that no longer stands — round 4 was withdrawn exactly once and
had no heading to say so.

The transcription note is **safe to write twice**: a note is not a
recognised kind, so a re-run reads the same proposal, finds the same lane
branches and takes the same row. One duplicate comment, the same price the
abandonment comment carries. The one case this cannot see is an **owner**
comment opening with one of the four headings, which would read as yours and
leave the round waiting — the safe direction, and reported as such.

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
issue alone. Take the **first** row here that matches; the order lands on the
fact rather than on the report of it, so a pull request the owner reopened
and merged is settled as merged even where the round reported that item
`pulled`:

| the row | what you do |
|---|---|
| the issue is closed | nothing |
| the issue is open, its pull request merged | close #<n> if every part it named is now delivered or re-filed, citing this pull request and the re-filings; otherwise edit #<n> down to the part that still stands — title and body — and **place the remainder by *What escalates***: `pile:ready` when nothing left in it needs a ruling, `pile:blocked` when something does (record the question in the `**Blocked on:**` form, or the next *Needs your call* has nothing to phrase), `pile:yours` when the work itself is the owner's. Write it with the two label edits in whichever direction it goes; writing *a* pile is what repairs a label a `pulled` round already moved, and that property is not particular to `pile:ready`. An item edited down also **leaves the ranked head**: step 2 writes that body and does not carry the name into it, so the proposal compares it against the current candidates like any other arrival. The parent still either closes or gets smaller — what is chosen here is only where the smaller one goes |
| the round reported it `pulled` | `--add-label pile:blocked`, then `--remove-label pile:ready`. The pull did this when it happened; doing it again is a no-op, and doing it *now* is what repairs a label the pull failed to write. Say nothing more — the pull already recorded what stopped it. The row may name no pull request at all, because a lane pulled mid-build opened none |
| the issue is open, its pull request closed unmerged | the owner abandoned it — comment that the work was built and the pull request closed unmerged, link it, then the same two label edits; the next proposal asks whether to build it another way or close it. Do not guess why |

A row matching none of them — open, naming no pull request, and not `pulled` —
is a malformed result. Say so under *Settled from last round* rather than
guessing; the item keeps the labels it has. If several pull requests named one
issue, the merged one decides.

**Nothing above is a skip.** The `pulled` row does the full pile move and
differs from the one below it only in staying quiet, so the round's report
suppresses a comment and never carries state: lose the report and you get one
comment too many, never an item in the wrong pile. That is the whole reason a
label replaced the note this step used to write.

**So the settlement is safe to run twice** — but safe is not silent. Every
label edit is a no-op the second time, a close on a closed issue is a no-op,
and an edit down to a remainder the body already carries changes nothing. The
abandonment comment is the one that is not, because it is free-form prose a
re-run cannot reliably recognise as its own. That duplicate is the whole
price, and it was priced knowingly.

This step writes no `Queue` body. Report what you settled under *Settled from
last round* in the proposal.

Before the first round there is no result comment, so there is nothing to
check and the round starts.

## 2. Propose

Read what arrived since the round timestamp in the `Queue` body:

```bash
gh issue list --state open --limit 500 --json number,title,labels,createdAt,body
```

Label each issue carrying no `pile:` label — every new arrival, and anything
a half-finished move left bare — using *What escalates* in the procedure.
**Except the `Queue` issue itself, which is in no pile**: it is the board, not
an item on it, and a `pile:ready` on it would be open, singly-labelled and
returned by the ready query, so step 4 would dispatch a lane at the board.
`pile:yours` is work no agent can do: a live deployment check, a console or
credential an agent lacks. List it, never rank it into a round. Compare each
new issue against the current candidates and place it; do not re-sort the
backlog. Losing does not have to be undone — the item keeps its place and the
pile drains past it. Move one up only if a trigger fired: a new priority
label, another issue on the same seam, a citation.

**Then re-read the blocked pile, which is the one pile a query cannot
settle.** Three checks, all cheap, all on items the next *Needs your call*
has to list anyway:

- an item whose `**Blocked on:**` line names an issue that has **closed**
  moves to ready (`--add-label pile:ready`, then `--remove-label
  pile:blocked`) — nothing else notices, because closing #N writes no label
  on anything waiting for it;
- an item carrying **no** `**Blocked on:**` line gets one now: you are about
  to phrase its question for the proposal, and that line is the phrasing
  written down. `scripts/backlog_metrics.py` counts these, so the gap is
  visible rather than merely present;
- an item **ruled and deferred** is re-read against its condition and moves
  to ready the round the condition holds.

The piles need no rebuilding: each is a query over open issues, so an item
the owner closed — a *yours* item they ran, a blocked item closed by a "leave
it" ruling — leaves by itself, and no number is ever carried forward.

**Rank a blocker for what it releases.** Before the four rules are applied,
every `pile:blocked` item whose `**Blocked on:**` line names an issue lends
that issue its claim: the blocker is ranked by the best rule holding for it
*or* for anything waiting on it, and in the rule-4 tier it takes the earlier
of the two filing dates. Rule 1 needs two dependents, so without this a
blocker of exactly one holds no rule at all and the blocked item's only exit
is unreachable — #1513 waited six rounds on #1294 with no rule broken. Not
counted under rule 1, because rule 1 outranks a live security defect and a
blocker of one has not earned that; inheritance gives it the priority of
what it is holding up and no more.

**The first thing the round's capacity buys is the oldest rule-4 item** —
the oldest ready issue holding none of picking rules 1-3 — ahead of rules
1-3, unless the tier is empty or the pinned items left no capacity at all.
Rules 1-3 outrank that tier every time, so without the reserved place its
drain rate is zero. It does not bound the wait, so the size is reported:
`scripts/backlog_metrics.py` computes the tier under *Rule-4 tier* and names
its oldest members, which is this slot's candidate list. **Read the
candidate before ranking it.** The figure is an upper bound — rule 2 is a
property of the defect and is not computed, so an item holding it is still
counted in the tier, and the oldest member may be one.

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
Piles: ready <n> · blocked <n> · yours <n>
Rule-4 tier: <the script's figure> (last round: <n>) — an upper bound
Carrying more than one pile label: <none, or the numbers>
```

The rule-4 line is **quoted from the script's own *Rule-4 tier* section**,
never estimated and never derived from labels: the proxy that was used for
nine rounds read 59 of 62 and was measuring the wrong thing. Compare it only
against the same script's figure from an earlier round.

Write the `Queue` body per *The Queue*: **the ranked head in order**, this
round's timestamp, and the three counts as a snapshot. This is the only step
that writes it.

Then **stop and wait**. Do not build anything that has an open question
against it.

## 3. Take the answers

Read the owner's reply. For each answered question, record the ruling as a
comment on its own issue, then:

- **implies work** — re-file the issue as the defect or feature that work
  is, with the ruling as its spec: `--add-label pile:ready`, then
  `--remove-label pile:blocked`.
- **defers** ("(b) **for now**", "(3) **at a second tenant**") — leave
  `pile:blocked` where it is and record the condition that would revisit it.
  List it in the next proposal as answered-and-waiting, not as a question,
  and re-read the condition when you sort: it moves to ready the round it
  holds. Do not close it: a deferral is "not yet", not "never".
- **implies none** (the behaviour is right as it stands) — **close** the
  issue there, quoting the ruling. Its labels stop mattering the moment it
  closes, so leave them.
- **makes the work the owner's** — `--add-label pile:yours`, then
  `--remove-label pile:blocked`.

A ruling lands in one of those four. If you cannot tell which, that is the
next question, not a guess.

Unanswered questions stay blocked and go in the next proposal unchanged.

## 4. Build

**The approved items** are the ones the owner approved from this round's
proposal — the ranked head's names that made it into *Building*, plus any the
owner pinned and the rule-4 slot, neither of which is in the head. The head is
their order, not their membership.

Dispatch them in the order the ranked head gives,
**skipping any the ready query does not return, any it returns that carries a
second `pile:` label, and any this step has already built** — the ready query
being `gh issue list --state open --label pile:ready`, and "already built"
being an open pull request on this item's lane branch:

```bash
# the <prefix>/<n>-<slug> branch convention this step mandates, below
gh pr list --state open --json number,url,headRefName \
  --jq '.[] | select(.headRefName | test("/<n>-"))'
```

Three conditions, because the query expresses only the first: an item mid-move
carries both labels and `--label pile:ready` still returns it, and an item
whose lane has delivered stays open and singly-labelled until the owner merges.

That is one predicate and it is the only one — a re-entry into this step reads
it rather than a rule of its own. It is a query rather than a test on a name,
which is what lets it cover the three ways an approved item stops being
dispatchable: a pull earlier in this step or in a previous invocation of it
(the item gained `pile:blocked`, and between the two label commands it carries
**both** — still `pile:ready`, and still not dispatchable), an item delivered
and closed by a previous round (labels survive closing, so only `--state open`
excludes it), and an item a lane has already built this round — which the open
pull request identifies because step 1 does not let a round start while a
previous round's is still open. **Skipped for that third reason is not
dropped**: the item joins the verify-and-review pass below carrying the pull
request it has, exactly as a returned lane would. (A feature lane opens none,
so this does not reach one — its spec is a comment, which a re-run can no more
recognise as its own than the abandonment comment, and one duplicate is the
price.)

One subagent per approved item, each with a self-contained prompt carrying:
the issue and its full text, the ruling if it had one, what "done" means, and
the *Building* section of `docs/development/issue-processing.md` verbatim.

Mechanics the prompt adds:

- Defect, chore and investigation lanes work in a fresh worktree on
  `origin/main` fetched now:
  `git worktree add -b fix/<n>-<slug> .claude/worktrees/<n> origin/main`.
  Before pushing: `black`, `ruff`, `lint-imports`, the tests that cover the
  change, and `python scripts/check_contract_version.py` if
  `docs/reference/api/` moved. **Not the whole suite** — see *Read CI for the
  regression check* below. A docs-only diff runs no tests at all. `Closes #<n>` only if the issue as written is
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
the blocked pile — `--add-label pile:blocked` then `--remove-label
pile:ready`, per *The piles are labels* — and carry on with the others. That
is one issue's own labels, so it needs nothing from step 2 and collides with
no other lane. A re-entry into this step will not re-dispatch it, whatever
the proposal and the head still say, because the dispatch rule above reads
the label. Do not ask the owner mid-round and do not guess.

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

   **If the pull request ships a guard, the brief is to defeat the guard** —
   what can be re-introduced without it noticing, in the shapes this codebase
   actually uses, with the live site count for each miss. Verifying the
   guard's answer on the current tree is a different activity and does not
   substitute: four rounds running the defect was in the guard the pull
   request installed, and every time it had already passed that check.
3. **Delta.** Re-review the new head. A finding surviving two rounds is
   escalated, not iterated — unless it **blocks the merge**, in which case it
   goes back for as many rounds as the lane can clear it in, because
   escalating it would hand the owner a pull request you know is broken. **If
   the lane cannot clear it — for any reason, not only a ruling — pull it**:
   close the pull request, record on the issue either the question or that
   the lane could not clear it, return the item to the blocked pile (the two
   label edits, as in the pull above), and give it a result row with outcome
   `pulled`, which is what keeps the next round's settlement from reading
   your close as the owner's abandonment. That is the loop's only other
   exit, and without it the round cannot reach step 5 at all.
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
is the only value the next settlement reads. It decides whether that
settlement posts an abandonment comment, and nothing else: the pile is the
label either way. A mid-build pull's row names no pull request, because none
was opened. The line below carries what stopped it.

Pulled: #N — <the question, or what stopped the lane>
Filed on the way: …
Waiting on you: merge the pull requests above.
```

The `## Round <N> — result` heading is load-bearing, not decoration: it is
what step 0 scans back to, and a result posted under any other heading reads
as the owner answering the proposal above it.

Then stop. The round ends when the owner merges.

## Rules

- **Never merge.** Not on green CI, not on a clean review. Only an explicit
  instruction from the owner delegates it, and it covers exactly what it
  names — one pull request, or a named set. Approving a *round* is not
  approving its merges; if the delegation is needed, ask for it once, naming
  the pull requests, rather than reading it into a round approval. A
  delegated merge still needs all four: review clean on the final head,
  every required context green on that head, the merge base green by commit,
  and the head unchanged since the review.
- **Never poll CI from a review lane.** The CI verdict belongs to whoever
  merges. A reviewer reports findings and says which of its checks it did
  not reach; it does not wait on `Test Standalone` / `Test Cloud`. Three
  review lanes in one round stalled on exactly this.
- **Never post to the `Queue` without one of step 0's four headings.** A
  comment with any other heading is read as the owner speaking, so an
  unlabelled note of your own answers your own proposal.
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
