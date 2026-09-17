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
owner's part is short: answer the questions, merge, and run the few things
no agent can. The first two are the round's two moments; the third stands
outside it and holds nothing up. Everything between is autonomous and asks
nothing.

## What the owner tracks

Nothing. Three actions are yours, and none of them needs you to know where
the work has got to:

- **Answer questions.** A proposal comment lists them, each with options and
  a recommendation, so answering is a word. Answer any, ignore any: an
  unanswered question returns in the next proposal and nothing stalls
  waiting on it.
- **Merge pull requests.** A result comment lists them with their CI state.
- **Run what only you can run, and close it.** The *yours* pile is work no
  agent can do — a live-deployment check, a console or credential an agent
  lacks — and a ruling can route work into it. It is listed in every
  proposal because nothing else will remind anyone it exists; closing an
  item is what takes it off the list, and that close is the one exit here
  the agent can neither perform nor see. Nothing is ranked or held up while
  it sits there, which is why this is the action that can wait.

The first two arrive as ordinary GitHub notifications. The third does not
arrive at all — it is listed, every round, until you close it.

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

### 0. Settle the last round

No unmerged pull request from the previous round. If there is one, this
round does not start: report what is outstanding and stop. This is what
bounds work in progress, and it is why nothing here tracks pull requests
ageing in the background.

**Not open is not the same as settled.** *Close out* reports before the
merges, so this is the only moment an agent sees what the owner did, and
every issue the last round's pull requests named has to end up somewhere
definite.

**The check is per pull request; the settlement is per issue.** One pull
request routinely carries several — a lane given three issues on one seam
delivers them together — so a rule keyed on the pull request has no answer
for a merge that closed two of the three and left the other open. The pairs
come from the round's own **result table**, which lists every item beside
the pull request that carried it, and not from what the pull requests cited:
a lane that cited nothing would contribute no issue and never be settled,
which is the hazard the first case below exists for. Settle each issue on
its own state:

- **Closed** — whatever closed it, nothing to do. The state is read off the
  issue rather than off the pull request's body, because a lane that cited
  nothing at all leaves the same open issue as one that wrote `Refs #<n>` —
  the citation for delivering part of an issue, which closes nothing — and
  only the issue says so.
- **Open, and its pull request merged** — the lane delivered part of it and
  split the rest, so the parent is still open, still ready, at the rank that
  approved a premise now partly dead. The owning agent closes it when every
  remaining part is delivered or re-filed, citing the pull request and the
  re-filings; when a part still stands and was not re-filed, the agent edits
  the parent down to that part — title and body — and returns it to the
  ready pile as an arrival, ranked against the current candidates, because
  the old rank was earned by the larger claim. There is no third outcome:
  the parent either closes or gets smaller. Round 1 merged two of these,
  #1467 and #1468, and both parents — #1447 and #918 — had to be noticed
  and closed by hand.
- **Open, and its pull request closed unmerged** — the owner abandoned it.
  Why is theirs to say, so the agent does not guess: it records that the
  work was built and the pull request closed unmerged, links it, and returns
  the item to the **blocked** pile, where the next proposal puts the
  question back — build it another way, or close it? Left in ready it sits
  at the rank that selected it with nothing to move it, so the next round
  dispatches a lane to build the same fix again.

**Every one of those ends by writing a note on the issue naming the pull
request it settled, and a note naming that pull request ends the matter.**
Settling mutates issues, and until the note exists the only record that it
ran is the proposal, which is written later — so an invocation that settles
and then stops re-applies the whole thing next time, editing the same parent
down a second time and re-ranking it again. **The note has to name the pull
request, not merely exist:** an issue is settled once per pull request that
carries it, and a parent edited down is built again later under another one.
A guard that fires on any note at all would skip that second settlement, and
every settlement after it, leaving the issue open at a stale rank forever —
which is the `Refs`-merged parent again, re-created permanently by the
mechanism meant to prevent re-application. The note is also what tells a
pull request the **agent** closed, in a pull, from one the owner abandoned:
a pull writes the same note whenever it closes a pull request, so the
abandonment case never fires on it, and a pull mid-build has no pull request
to close and never reaches here at all. Reading the round's result prose for
that instead would rest on a format this pass introduced, which round 1's
result, written before it, does not carry.

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

**A yours item leaves by the ordinary door: the owner closes it once they
have run it.** Nothing reports back and nothing needs to — each proposal
rebuilds the pile from open issues, so a closed one drops out by itself and
an open one is listed again. If running it turns up a defect, that is a new
issue like any other. It is written down because it is the one exit here
that the agent neither performs nor sees.

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
the evidence or fixes what survives.

**Who closes a verified-fixed issue:** the owning agent, after re-running
that evidence rather than relaying it, naming the pull request that actually
fixed it — resolved with `gh pr view`, never inferred from a narration.
Residue is filed separately rather than held against the issue. A result
that is ambiguous — it reproduces only under one configuration, or the guard
does not bite and making it bite is a design call — is not a closure: it
goes to the blocked pile as a question, like any other pull.

Rank the ready pile (see *Picking*), then post **one** comment, the round
proposal:

1. **Building** — the ready items this round builds, each with one line on
   why it is ranked there and what "done" means.
2. **Needs your call** — every blocked item, the whole standing pile rather
   than only the new ones. Each gets the question in one sentence, the
   options, a recommendation, and what it unblocks. Answering should take
   one word. An item already ruled on and **deferred** is listed here too,
   carrying its ruling and the condition it waits on instead of a question:
   it is shown, never re-asked, and dropping it from the list would take it
   out of every pile.
3. **Yours to run** — the third pile, listed so it is visible, never
   ranked.
4. **Settled from last round** — what *Settle the last round* did with each
   issue the previous round's pull requests named, one line each. It is
   where a settlement becomes visible to anyone but the agent.
5. **Measurement** — the output of `python scripts/backlog_metrics.py`.

### 2. Owner answers

The owner approves or edits the round and answers whichever questions they
choose to. An unanswered question is not a failure: that item stays blocked
and appears again next round. An answered one moves its issue to the ready
pile with the ruling recorded on it as its spec — if that is where the
ruling sends it. A ruling lands its issue in one of four places, and three
of them are not ready: see *What escalates*.

### 3. Build

One lane per approved item, in its own worktree, autonomous. The gates under
*Building* are not optional.

**No question is asked while building.** If a lane cannot deliver its item,
the item is **pulled**: the lane stops, what stopped it is recorded on the
issue — the question if there is one, otherwise the fact — the item returns
to the blocked pile, and it appears in the next proposal. The other lanes
carry on. Half-built work is not left behind and the round is not held up.

**"Cannot deliver", not "needs a ruling".** Needing a ruling is the common
case and not the only one: the work turns out to be several rounds of it, or
it cannot be done from where the lane stands. Neither trips any of the four
escalation triggers, so gating the exit on a ruling leaves them with no exit
at all — the same shape the *Review on the final head* gate had to close one
level down, which is why that gate's condition is "cannot clear" rather than
"needs a ruling". Failing to clear a blocking finding is one way of failing
to deliver; this is the general case.

### 4. Land

Report each pull request with its CI state. The owner merges. A round is not
over until every one is merged or explicitly abandoned — abandoned meaning
the owner closed it unmerged, which *Settle the last round* turns back into
a blocked item. A pull request the agent closed itself is a pull, settled
where it happened, and not this.

### 5. Close out

Post the result on the proposal comment: what merged, what was pulled and
what stopped it, what was filed along the way. Then the next round can start
— and it is that round's *Settle the last round* that does it, because this
report is written before the owner has merged any of them.

## Picking

Rank the ready pile by the first rule that applies:

1. It unblocks two or more other items.
2. It is a security or correctness defect reachable in a shipped
   configuration.
3. It sits on a seam that produced three or more issues in the last month,
   because that seam will keep producing them until its rule has one owner.
4. Otherwise, oldest first.

**Rule 4 is a tiebreak over everything left, not a fourth property.** Read
as "it is the oldest ready item" it is true of exactly one issue, and every
ready item holding none of 1-3 and not being that one has no rank at all —
no position in the order, so nothing for a round to reach. That is most of
what review produces: five of the thirteen issues round 1 filed hold none of
rules 1-3.

The ranking fills whatever capacity the pinned items leave. It decides
*order*, not size; size is the judgement above.

**The first thing that capacity buys is the oldest item in the rule-4
tier**, ahead of rules 1-3, unless the pinned items left no capacity at all.
Rules 1-3 outrank the tier every time, so without a reserved place the drain
rate is exactly zero and every item in it is back where the missing rank
left it. The slot is also what reaches the already-fixed issues the premise
check cannot: that check runs on the items about to be built, so an issue
whose fix landed long ago and is ranked nowhere is never checked against
`main` at all, and nothing else in the round would look at it. No round has
produced an instance yet — both dead premises round 1 found were P1 defects,
ranked far above the tier and caught by the check itself — so this one
stands on the mechanism rather than on a case.

**One slot makes the drain non-zero. It does not bound the wait, and this
document's own figures say so:** round 1 filed thirteen issues and five of
them hold none of rules 1-3, so on those numbers the tier gained four in a
round that a slot drains one from. No rule written here can serve a queue
faster than it arrives, so the claim is the modest one — an item in the tier
has a rank and a non-zero rate, which is a queue, where before it had no
position at all, which was a leak. Whether the queue is fast enough is a
measurement rather than a rule: the proposal reports the tier's size beside
the residue, a tier growing across four rounds says so, and the lever in the
meantime is the one that already exists — the owner pins.

**Size is bounded by review capacity, not by lane capacity.** Lanes are
cheap and parallel; review is neither, because a fix written to answer a
review is new code that has to be reviewed again. Round 1's five items became
three pull requests: one needed no fix commit at all, the other two needed
four and six, and each round found a defect in the fix before it — the last
of them a boot gate the round before had added. Count the *independent
seams* a batch touches rather than the items: three items on three seams is
a bigger round than five on one. When a single item is large enough that its
own review will run several rounds — a security boundary, a storage change,
anything shipping a new guard — it is a round by itself.

**Ranking is incremental.** A new issue is compared against the current
candidates when it arrives, and that is the only comparison it gets. Nothing
re-sorts the whole backlog each round, which would be work proportional to
the backlog for comparisons already made. Losing that comparison is not a
state: the item keeps the place the comparison gave it, and the pile drains
past it. That only holds if the place survives the round, so the **ranked
head** — the items holding rules 1-3, in order — is written into the `Queue`
body and carried forward. The rule-4 tier is not, and needs not be: "oldest
first" is recoverable from the issues themselves at any moment, which is why
the tier is the part of the pile that costs no bookkeeping. Three things
move an item up out of its turn — a new priority label, another issue on the
same seam, a citation from a new issue — and an item that gets none of them
still arrives, because rule 4 orders its tier and every round takes the
oldest of it. An earlier draft listed a fourth trigger, "an age threshold
recorded in the pile", which nothing ever recorded a value for. A trigger
with no value is not an exit.

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
  narrow on purpose: everything non-blocking is filed, and the round says
  how many findings it filed rather than fixed. **And it is bounded by the
  pull rule, which reaches an open pull request as well as a lane
  mid-build:** a blocking finding **the lane cannot clear** is pulled — the
  owning agent closes the pull request, records on the issue either the
  question, if it needs a ruling, or simply that the lane could not clear
  it, adds the settlement note naming that pull request so the next round
  reads the close as a pull rather than as the owner's abandonment, returns
  the item to the blocked pile, and the result reports it as not delivered.
  The condition is "cannot clear", not "needs a ruling": a
  finding that is merely too hard trips none of the four escalation
  triggers, so gating the exit on a ruling would leave that case with no
  exit at all — the same shape as the leak this rule exists to close. "The
  lane could not clear it" is itself a call for the owner: ship the bug, or
  take it on themselves. Closing a pull request is the one action on one the
  owning agent may take; merging is never one. Without that exit this
  exception would be the only state here the *round itself* cannot leave —
  step 5 could never run, so no other lane's work would reach the owner
  either, and holding the round up is precisely what the pull rule exists to
  prevent.
- **Verify, do not relay.** Act on a subagent's finding only with execution
  evidence, and run a suggested remedy before adopting it. A remedy is
  checked *before* it is asked for: in round 1 the suggested fix for a
  session disclosure would have turned it into a 500 on the degraded path
  that made the bug reachable, and the lane caught it by running it.
- **Verify the fix, not only the finding.** The attention goes where the
  disagreement is, so a finding a lane pushes back on gets checked and a
  finding it accepts does not. Round 1 reported an engine leak as fixed while
  half of it stood — the sibling fixture was fixed, the one named in the
  review was not — and it was caught a round later by re-running the
  measurement rather than re-reading the report. Re-run the thing that
  failed, not the summary of it.
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

A round asks the owner for exactly two things: the answers in *Owner
answers* and the merges in *Land*. The third owner action, the *yours* pile,
is not a round's to ask for — it is standing, and holds nothing up. Between
those two, decide and record rather than ask.

A question belongs in the blocked pile, and therefore in a proposal, when any
of these holds. Everything else an agent decides and records.

1. It would override a documented design decision.
2. It is about what a user sees or experiences.
3. Two defensible options mean materially different amounts of work.
4. It deletes data, changes a wire contract's major version, or changes what
   a deployment must configure.

**A pull enters the blocked pile by its own door, and these four do not
govern it.** They say when an agent must *ask* rather than decide; a pull is
not an agent asking, it is a lane stopping. Most pulls do trip one of them,
because most are a lane meeting a question — but a lane that simply could
not deliver, whether what stopped it was a blocking review finding or the
size of the work, trips none, and it still belongs in the pile, because the
alternative is a round that cannot reach step 5. What the issue records in
that case is not a question but the fact: the lane could not deliver it. The
owner's call is then whether to ship the bug, take it on themselves, or drop
it.

Recording a decision is not the same as closing the issue. A ruling puts its
issue in exactly one of the four places an issue can be, and what the agent
records says which:

- **It implies work** — re-filed as the defect or feature that work is, with
  the ruling as its spec, into **ready**. The pull request that delivers it
  closes it: by `Closes`, or at the next round's *Settle the last round* if
  that pull request could only carry `Refs`.
- **It defers** — "not yet" rather than "never". The issue stays
  **blocked**, with the ruling and the condition that would revisit it both
  recorded, and the proposal lists it as answered-and-waiting rather than as
  a question, so leaving it there costs the owner nothing. The owning agent
  re-reads that condition each round as it sorts — being listed is what
  gives the check somewhere to happen — and moves the issue to ready the
  round the condition holds. A deferral naming no condition is not a
  complete ruling, and asking for one is the next question.
- **It implies none** — the behaviour is right as it stands, so the owning
  agent **closes** the issue as it records the ruling, quoting it. It never
  reaches ready: ready means a lane can be dispatched against it, and there
  is nothing here to dispatch.
- **It makes the work the owner's own** — into **yours**, listed every
  proposal and never ranked.

**Read the ruling before choosing among the last three, because on the
standing pile a pure "implies none" is rare.** #1168's recommendation is
"(b) **for now**" and its own row says it pairs with #1167, whose
recommendation is "(1) now, (3) **at a second tenant**". Both are deferrals.
Closing either on a "leave it" reading would shut a live tenant-isolation
gap that is explicitly expected to be revisited, and break the pairing with
nothing left to re-file against.

## How this is judged

Each round reports `scripts/backlog_metrics.py`. The number that matters is
the **residue**, meaning issues still open a week after filing. The raw open
count moves with how hard the period looked rather than with how healthy the
code is, and review alone accounts for about a third of everything filed.

Three signals that this document is wrong rather than the work:

- Residue not falling across four rounds spanning at least four weeks.
- Items pulled in *Build* more often than they are built, which would mean
  *Propose* is not finding the questions before the work starts.
- The rule-4 tier growing across four rounds, which means the reserved slot
  is drawing from it slower than review is filling it.

**A round raising the open count is not one of them.** Round 1 closed five
issues and filed thirteen, so the open set rose over the round, and nine of
the thirteen came out of review. That is the process working: a review
finding becomes an issue precisely so it is not silently carried, and the
residue — issues surviving a week — is what says whether they drain. Judge a
round by what it *closed and filed*, and by whether the filed ones close
later; an agent that keeps the count flat by not writing findings down is
failing, not succeeding.

**Every fifth round, read this document as a state machine rather than as
prose.** For each state an issue can be in, name what moves it out and who
does it. A state with no exit is a leak, and it is invisible when the same
text is read as description: four survived three review rounds of an earlier
draft and were found only this way, and two more, just as old, were found
only on the second such read. **Then read the result the same way before
shipping it.** A pass that closes leaks writes new states: the pass that
added the blocking-finding exception under *Building* created the first
state here that the round itself could not leave, and its own first fix for
that left the merely-too-hard case with no exit either. The read that counts
is of the text after the edits, not of the edits.

## Words used here

**Seam.** All the places one rule has to hold. Every place that writes a
conversation row is one seam; every place that reads that table in order is
another. Not a file or a folder, which is why a defect on a seam usually has
siblings.

**Head.** The newest commit on a branch. It moves every time something is
pushed, so "the final head" means the code after the last fix rather than the
code when the pull request was opened. Unrelated to the *alembic head*, which
is the newest database migration.
