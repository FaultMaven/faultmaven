# Issue processing

How issues get picked, worked and closed. One round at a time, with every
question asked before the work starts rather than during it.

## The idea

Issues here come in two kinds, and only two matter for scheduling: the ones
where **the answer is known** and someone just has to do the work, and the
ones where **a call has to be made first**. Mixing them in one queue is what
jams it. A lane can pick up a defect and finish it. It cannot pick up "which
of these two layers owns this policy" and finish it, so that issue sits, and
everything ranked below it sits too.

Measured on 2026-09-15: of 63 open issues the largest single group was
decisions, and 9 of the 16 open ones genuinely needed the owner rather than
an agent. Meanwhile the defects being fixed had a median age of 144 days, so
the pool is old and being drained rather than freshly generated. Almost
nothing here is urgent. Almost everything is blocked in the sense that
nobody has said which way.

So the round below puts the questions **first**, in one batch, each with
options and a recommendation, and then builds only what has an answer. The
owner's part is short and happens at two moments: answer the questions, and
merge. Everything between is autonomous and asks nothing.

## What the owner tracks

Nothing. Two actions arrive as ordinary GitHub notifications, and neither
needs you to know where the work has got to:

- **Answer questions.** A proposal comment lists them, each with options and
  a recommendation, so answering is a word. Answer any, ignore any: an
  unanswered question returns in the next proposal and nothing stalls
  waiting on it.
- **Merge pull requests.** A result comment lists them with their CI state.

Everything else — which issues are ranked where, what is built, what is
being reviewed, what is waiting — is the agent's bookkeeping. The command
locates itself, so `/process-top-issues` with no arguments is always the
right thing to type, whatever happened last.

"Round" below is the unit of that bookkeeping: one batch of work from
proposal to merge. It is a word for reading this document, not something to
hold in your head.

## The round

A **round** is a batch of issues worked together. One round runs at a time.

**Five is a working size, not a rule.** It exists to keep one round's
resource draw reasonable — a batch job, not a sprint that consumes
everything at once. Judge by the work rather than the count: three large
interdependent items can be a whole round, seven small independent ones can
be another. The proposal says why the round is the size it is.

**The owner can pin items.** Any issue named by the owner goes into the round
whatever its rank, and the remaining capacity is filled by the ranking. A
pinned item that turns out to need a ruling is reported as such rather than
built on a guess.

### 0. Check the last round ended

No unmerged pull request from the previous round. If there is one, this
round does not start: report what is outstanding and stop. This is what
bounds work in progress, and it is why nothing here tracks pull requests
ageing in the background.

### 1. Propose

Sort every issue filed since the last round into three piles:

- **ready** — the answer is known; it needs work, not a call.
- **blocked** — it needs a ruling before anyone can build it.
- **yours** — the work itself is the owner's and no agent can do it: a
  live-deployment check, or anything needing a credential or console an
  agent does not have.

The third pile exists because running this procedure on 2026-09-16 found two
beta gates that were neither ready nor blocked on a ruling. Calling them
ready would have had them proposed every round and never built. They are
listed in the proposal and never ranked into a round.

**An open issue is not evidence the defect is live.** Before an item is
ranked into a round, check its named code points against `origin/main`
fetched now. Round 1 approved five P1s and two of them — #907 and #752 — had
already been fixed, each by a pull request that solved the problem from
another angle and never cited the issue it closed. They had been open 49 and
58 days; the fixes were 23 days and **one** day old. So age measures linking
hygiene as much as code health, and `fetched now` is not a figure of speech —
a base pulled the day before would still have ranked #752. The check is
cheap: reading one narration file and one `git grep` caught both before any
lane was dispatched. An item whose premise looks dead is still worth a round
— as a **verification** lane, which proves by execution whether it
reproduces, makes the covering guard bite under mutation, and either posts
the evidence or fixes what survives. **Who closes
it:** the owning agent, after re-running that evidence rather than relaying
it, naming the pull request that actually fixed it — resolved with `gh pr
view`, never inferred from a narration. Residue is filed separately rather
than held against the issue. A result that is ambiguous — it reproduces only
under one configuration, or the guard does not bite and making it bite is a
design call — is not a closure: it goes to the blocked pile as a question,
like any other pull.

Rank the ready pile (see *Picking*), then post **one** comment, the round
proposal, with three parts:

1. **Building** — the ready items this round builds, each with one line on
   why it is ranked there and what "done" means.
2. **Needs your call** — every blocked item, the whole standing pile rather
   than only the new ones. Each gets the question in one sentence, the
   options, a recommendation, and what it unblocks. Answering should take
   one word.
3. **Yours to run** — the third pile, listed so it is visible, never
   ranked.
4. **Measurement** — the output of `python scripts/backlog_metrics.py`.

### 2. Owner answers

The owner approves or edits the five and answers whichever questions they
choose to. An unanswered question is not a failure: that item stays blocked
and appears again next round. An answered one moves its issue to the ready
pile with the ruling recorded on it as its spec.

### 3. Build

One lane per approved item, in its own worktree, autonomous. The gates under
*Building* are not optional.

**No question is asked while building.** If a lane discovers its item
actually needs a ruling, the item is **pulled**: the lane stops, the question
is recorded on the issue, the item returns to the blocked pile, and it
appears in the next proposal. The other lanes carry on. Half-built work is
not left behind and the round is not held up.

### 4. Land

Report each pull request with its CI state. The owner merges. A round is not
over until every one is merged or explicitly abandoned.

### 5. Close out

Post the result on the proposal comment: what merged, what was pulled and the
question that pulled it, what was filed along the way. Then the next round
can start.

## Picking

Rank the ready pile by the first rule that applies:

1. It unblocks two or more other items.
2. It is a security or correctness defect reachable in a shipped
   configuration.
3. It sits on a seam that produced three or more issues in the last month,
   because that seam will keep producing them until its rule has one owner.
4. It is the oldest ready item.

The ranking fills whatever capacity the pinned items leave. It decides
*order*, not size; size is the judgement above.

**Size is bounded by review capacity, not by lane capacity.** Lanes are
cheap and parallel; review is neither, because a fix written to answer a
review is new code that has to be reviewed again. Round 1's five items became
three pull requests: one needed no fix commit at all, the other two needed
four and six, and each round found a defect in the fix before it — the last
of them a boot gate the round before had added. Count the *independent
seams* a batch touches rather than the items: three items on three seams is
a bigger round than five on one. When a single item is large enough that its own review will
run several rounds — a security boundary, a storage change, anything shipping
a new guard — it is a round by itself.

**Ranking is incremental.** A new issue is compared against the current
candidates when it arrives, and that is the only comparison it gets. Nothing
re-sorts the whole backlog each round, which would be work proportional to
the backlog for comparisons already made. An item that lost comes back when
something would change its rank: a new priority label, another issue on the
same seam, a citation from a new issue, or an age threshold recorded in the
pile.

## Building

Gates for a lane, each from a failure that cost real time:

- **Root before scope.** Trace to the root cause before deciding what to
  ship, and design the fix for the class rather than the reported copy. When
  the root's fix is larger than one pull request, scope is managed by filing
  a ticket that names the root and the design. A ticket may split the fix; it
  may never stand in for the investigation.
- **Worktree per lane, base fetched now.** Never work in a shared checkout.
  Lanes collide on global values such as the API contract version and the
  alembic head, so two lanes on the same seam are sequenced rather than run
  together.
- **State N before fixing a duplicated rule.** Say how many implementations
  exist and show the scan that found the number, then ship that scan as a
  test. A guard must also declare where its rule *can* be violated and fail
  if it did not look there: the repository's oldest guard has been green for
  eight months while watching three directories that contain none of the
  violations it exists to catch.
- **Enumerate the consumers before changing a producer.** When a field's
  meaning, a written value or a placeholder changes, list what reads it and
  show the search that found them. This is the regression class here — the
  code that breaks is old code that read the old meaning.
- **Review on the final head.** A fix written to answer a review is new code
  nobody has reviewed, so run the review again after it. A finding that
  survives two rounds is escalated rather than iterated — **unless it blocks
  the merge**, because escalation hands the owner a pull request, and a pull
  request that refuses a fresh install or answers 500 where it promises 401
  is not something to hand anyone. Blocking means the change is worse than
  the bug it fixes for someone who has not hit the bug. That exception is
  narrow on purpose: everything non-blocking is filed, and the round says how
  many findings it filed rather than fixed. **And it is bounded by the pull
  rule, which reaches an open pull request as well as a lane mid-build:** a
  blocking finding the lane cannot clear without a ruling is pulled — the
  owning agent closes the pull request, records the question on the issue,
  returns the item to the blocked pile, and the result reports it as not
  delivered. Closing a pull request is the one action on one the owning agent
  may take; merging is never one. Without that exit this exception would be
  the only state here the *round itself* cannot leave — step 5 could never
  run, so no other lane's work would reach the owner either, and holding the
  round up is precisely what the pull rule exists to prevent.
- **Verify, do not relay.** Act on a subagent's finding only with execution
  evidence, and run a suggested remedy before adopting it. A remedy is
  checked *before* it is asked for: in round 1 the suggested fix for a
  session disclosure would have turned it into a 500 on the degraded path
  that made the bug reachable, and the lane caught it by running it.
- **Verify the fix, not only the finding.** The attention goes where the
  disagreement is, so a finding a lane pushes back on gets checked and a
  finding it accepts does not. Round 1 reported an engine leak as fixed while
  half of it stood — the sibling fixture was fixed, the one named in the
  review was not — and it was caught a round later by re-running the measurement
  rather than re-reading the report. Re-run the thing that failed, not the
  summary of it.
- **Exercise a guard through the path that runs it.** A direct call proves
  the guard's logic and nothing about where it is called from. Round 1
  shipped a boot gate that refused a database with no table, tested by
  calling it, and it ran *before* the migration that creates the table — so
  it refused every first-ever install. Both the lane's test and the owning
  agent's verification called it directly, which is why neither saw it. A
  guard that runs at startup needs one test that drives startup.
- **Port the whole tree.** Before reporting, the worktree is clean or every
  remaining file is named. Clean is not the same as shipped: a lane in round
  1 finished with 203 lines unstaged and the pull request head unmoved, so
  the work existed only on disk.

## What escalates

The round asks the owner for exactly two things: the answers in step 2 and
the merges in step 4. Between those, decide and record rather than ask.

A question belongs in the blocked pile, and therefore in a proposal, when any
of these holds. Everything else an agent decides and records.

1. It would override a documented design decision.
2. It is about what a user sees or experiences.
3. Two defensible options mean materially different amounts of work.
4. It deletes data, changes a wire contract's major version, or changes what
   a deployment must configure.

Recording a decision is not the same as closing the issue. When a ruling
implies work, the issue is re-filed as the defect or feature that work is,
with the ruling as its spec, and it closes when the work lands.

## How this is judged

Each round reports `scripts/backlog_metrics.py`. The number that matters is
the **residue**, meaning issues still open a week after filing. The raw open
count moves with how hard the period looked rather than with how healthy the
code is, and review alone accounts for about a third of everything filed.

Two signals that this document is wrong rather than the work:

- Residue not falling across four rounds spanning at least four weeks.
- Items pulled in step 3 more often than they are built, which would mean
  step 1 is not finding the questions before the work starts.

**A round raising the open count is not one of them.** Round 1 closed two
issues and filed eleven, taking the open set from 63 to 72, and all but two
of the eleven came out of review. That is the process working: a review
finding becomes an issue precisely so it is not silently carried, and the
residue — issues surviving a week — is what says whether they drain. Judge a
round by what it *closed and filed*, and by whether the filed ones close
later; an agent that keeps the count flat by not writing findings down is
failing, not succeeding.

**Every fifth round, read this document as a state machine rather than as
prose.** For each state an issue can be in, name what moves it out and who
does it. A state with no exit is a leak, and it is invisible when the same
text is read as description: four such leaks survived three review rounds of
an earlier draft and were found only this way.

## Words used here

**Seam.** All the places one rule has to hold. Every place that writes a
conversation row is one seam; every place that reads that table in order is
another. Not a file or a folder, which is why a defect on a seam usually has
siblings.

**Head.** The newest commit on a branch. It moves every time something is
pushed, so "the final head" means the code after the last fix rather than the
code when the pull request was opened. Unrelated to the *alembic head*, which
is the newest database migration.
