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

**First locate the round, and locate it by kind rather than by position.**
Where a round has got to is read off the `Queue`'s comments, and for nine
rounds that meant "read the last one". The last one is not a state. When
round 10 started, the newest comment was a handover note, which no row
covered: read strictly it fell through to no state at all, read loosely to
*waiting on the owner*, and it would have stopped a round that was fully
merged. The board is the natural place for such a note — five of the first
ten rounds posted one — so this recurs.

Nor can the reader fall back on who wrote it. **The agent posts with the
owner's credential**, so proposal, result, note and answer are all authored
by the same account and authorship separates nothing. What the agent can do
is label its own writes, so it does: a proposal, a result and a withdrawal
each carry a heading naming which they are, anything else the agent posts to
the board is headed `## Note — …`, and **a comment carrying none of those
four headings is the owner speaking**. Locating is then a scan back to the
newest comment of a recognised kind, which makes a note free.

That is the case where the narration is merely cluttered. The case where it
is **absent** needs the facts instead: round 3 was approved in a working
session rather than as a reply, and its result was never posted, so two of
the three rows were false at once and a fully built round read as unstarted.
So a proposal with no owner comment after it is not yet *waiting* — the step
also asks whether the round's lane branches carry pull requests, because an
approval nobody wrote down is still visible in what it authorised. Where
they do, the round was approved out of band: the agent transcribes the
approval onto the board as a note, so the next reader need not re-derive it,
and continues from the work rather than from the prose. Writing that note
twice is the whole cost of a stop in the middle of it: a note is not a
recognised kind, so a re-run reads the same proposal, finds the same lane
branches and continues the same way — free-form prose a re-run cannot
recognise as its own, priced here exactly as the abandonment comment is.

What this cannot tell apart is an **owner** comment that happens to open
with one of the four headings, which would read as the agent's and leave the
round waiting. The headings are the agent's marks and the owner has no
reason to type one; the failure is the safe direction — the round stops and
says what it is waiting for — and the fact check catches it wherever lanes
have already run. `.claude/commands/process-top-issues.md` carries the
table.

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
  the parent down to that part — title and body — and then **places the
  remainder by *What escalates***, like any other item whose contents have
  just been re-read: ready when nothing left in it needs a ruling, blocked
  when something does, yours when the work itself is the owner's. Either way
  it leaves the ranked head, which only the *Propose* that follows writes,
  so that pass compares it against the current candidates like any other
  arrival instead of leaving it at a rank the larger claim earned. The
  parent still either closes or gets smaller; what changed is where the
  smaller one goes. Round 1 merged two of these, #1467 and #1468, and both
  parents — #1447 and #918 — had to be noticed and closed by hand.

  This row used to write `pile:ready` unconditionally, and the reason was
  sound but narrower than the rule it produced: writing *a* pile is what
  repairs a label a **pull** failed to write, and a partly-delivered issue
  was never pulled. Round 10 hit the gap. #1563 delivered item 15 of #985
  and left three owner rulings behind; the letter would have moved all three
  into ready, where *Needs your call* — which lists **blocked** items only —
  would never have re-asked them, and three questions would have gone quiet
  with nothing recording that they had. Placing the remainder keeps the
  repair, because a pile is still written whichever way it goes. A remainder
  that lands in blocked carries its question in the `**Blocked on:**` form
  below, or the next proposal has nothing to phrase.
- **Open, and its pull request closed unmerged** — the owner abandoned it.
  Why is theirs to say, so the agent does not guess: it records that the
  work was built and the pull request closed unmerged, links it, and returns
  the item to the **blocked** pile, where the next proposal puts the
  question back — build it another way, or close it? Left in ready it sits
  at the rank that selected it with nothing to move it, so the next round
  dispatches a lane to build the same fix again.

**Every one of those is safe to repeat**, and it is the medium rather than
the prose that makes it so. A pile is a **label on the issue**, so a move is
`--add-label` then `--remove-label` on that one issue: adding a label already
present is a no-op, removing an absent one is a no-op, and neither reads
anything another step might be writing. A re-run of the settlement re-derives
the same labels and changes nothing. Safe is not the same as silent — the
abandonment comment is free-form prose a re-run cannot reliably recognise as
its own, so it may be posted twice — but one duplicate comment is the entire
price.

**Three consecutive attempts tried to buy that with prose, and each bought a
leak.** The piles used to be lists inside one shared blob — the `Queue` issue
body, which has no compare-and-set, no transaction and no schema. Every fix
added a rule about *when* to write the blob, and each rule was another
unsynchronised writer. The sharpest of them wrote a `Settled by #<pr>` marker
on each issue and skipped any already carrying one, which made the stop it was
built for **unrecoverable**: the marker was on the issue while the piles were
in the body, so a re-run read the marker and did nothing while *Propose*
rebuilt the item straight back into ready at its old rank. Its replacement
moved the blob write earlier and left a pull's move unwritten; the fix for
*that* left the item named in the ranked head, which is what a round
dispatched from at the time. Three roads to one leak, on two files, by three
careful lanes. The wall was the blob. **A label has the properties the prose
kept failing to state**, and it has them by construction: one issue, one
atomic edit, no shared object, and a pile that is a query rather than a parse.

**What survives in the `Queue` body is what a label cannot hold** — the ranked
head, in order, and the round timestamp. *Propose* is its only writer, so it
has no concurrency left to get wrong, and the head is an ordering over
membership rather than membership itself: **a name the ready query no longer
returns is skipped wherever the head is read.** *Build* states that predicate
in full and it is the only one. That is why a pull writes one label and
nothing else, and why a head naming a blocked, pulled or closed item is stale
rather than wrong.

**A pull request the agent closed is told from one the owner abandoned by
the result table**, which reports that issue's outcome as `pulled`. It
decides whether the settlement posts an abandonment comment, and nothing
else: the pile move runs either way, so the row that reads it is not a skip,
and losing the report costs one comment too many rather than an item in the
wrong pile. An earlier draft objected to reading the round's result at all,
on the ground that it rested on a format that pass had just introduced. That
objection is spent — the table is already where the settlement gets its
pairs — and it would not matter here in any case, because no state rides on
the answer.

### 1. Propose

Sort every issue filed since the last round into three piles, by labelling
it — the label **is** the pile, and an issue carrying none is one nobody has
sorted yet:

- **`pile:ready`** — the answer is known; it needs work, not a call.
- **`pile:blocked`** — it needs a ruling before anyone can build it.
- **`pile:yours`** — the work itself is the owner's and no agent can do it: a
  live-deployment check, or anything needing a credential or console an
  agent does not have.

Each pile is then a query over open issues, which is why nothing here has a
rule about carrying a closed number forward: a closed issue is not in the
answer.

Three rules keep `pile:blocked` from silting up. The first and the last were
learned the expensive way — the pile held at 12 for six rounds, and when it
was finally read in one pass, **ten of the twelve moved**: one closed, eight
ruled, one split. The middle one is the exit that a closed blocker leaves,
which nothing else was watching.

- **`pile:blocked` records what it is blocked ON, in the body, in one form.**
  "Blocked on a ruling from the owner" and "blocked on issue #N landing" are
  different states that look identical in a label, and the second is not
  owner latency. #1513 spent six rounds counted against the owner while
  waiting on #1294. Say which at the moment the label goes on, as a line
  reading `**Blocked on:** #N …` or `**Blocked on:** <the ruling> …` — the
  issue reference first, so that a ruling which merely cites an issue for
  context is not read as a dependency on it. **In the body, not in a
  comment:** the statement is a current value, re-read every round by
  whoever sorts and by *Picking*'s promotion rule, and a comment thread
  holds a history instead — scanning one for the newest statement is the
  same "read the narration" that labels replaced everywhere else here. It is
  also what makes the rule computable: `scripts/backlog_metrics.py` reads
  that line and nothing else, and reports how many blocked items state
  nothing, so the gap is counted rather than assumed away — and reports a
  statement it can see but cannot read as **neither**, rather than filing it
  as owner latency: a measurement that misses can be fixed, one that answers
  the wrong bucket recreates the miscount this line exists to end. **An item
  already in the pile without a readable one gets it the next time you
  sort** — that pass has to phrase its question for *Needs your call*
  anyway, so the statement is the phrasing written down.
- **A blocked item whose named issue has closed moves to ready as you
  sort.** Its condition is met and nothing else will notice — the label is
  the pile, and closing #N writes no label on anything waiting for it. The
  metrics name these, so the check costs a read.
- **Split a mixed issue when you label it, not later.** An issue is blocked
  if *any* part of it needs a ruling, so one design question freezes
  everything beside it. #985 carried thirteen items, of which its own text
  marked **1-7 as "mechanical riders (no design needed)"** — seven items
  needing no thought sat since round 1 behind six decisions they did not
  depend on. Splitting is cheap while the analysis is still loaded and
  expensive afterwards.

The direction of that error is worth keeping in mind: the blocked pile was
not a queue of hard decisions. It was mostly a queue of unexamined labels.
Nothing re-asks *why* an item is blocked once the label is on, so blocked-ness
becomes a fact rather than a claim — which is the same failure this campaign
keeps finding in code.

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

**Once per round, though — this step is re-entered.** Step 0 routes an
answered proposal back here whatever has already run, and an item its lane has
built is still open and still ready, so the piles cannot tell "approved and not
yet built" from "approved and built". The pull request can, and it costs no new
state: *Settle the last round* will not start a round while an earlier one's is
still open, so an open pull request on an approved item's lane branch is this
round's work. It is a condition on the same dispatch predicate — which is what
re-entry reads, rather than a rule of its own — and the item it skips is not
dropped: it goes on to verification and review with the pull request it already
has, which is also what makes the skip self-checking, since a branch someone
else named alike is read there rather than quietly standing in for a lane.

**No question is asked while building.** If a lane cannot deliver its item,
the item is **pulled**: the lane stops, what stopped it is recorded on the
issue — the question if there is one, otherwise the fact — the item returns
to the blocked pile — one label on one issue, which collides with nothing
another lane is doing — and it appears in the next proposal. The other lanes
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

Report each pull request with its CI state. **The owner merges**, unless the
owner delegates it for this round in as many words — approving a *round* is
not approving its merges, and an agent that widens one into the other is
deciding something that was not given to it. Delegated or not, a merge needs
all four: review clean on the final head, every required context green on
that head, the merge base green by commit, and the head unchanged since the
review. A round is not
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

**A blocker is ranked for what it releases, not only for itself.** Rule 1
needs two or more dependents, so an issue that exactly one blocked item
waits on holds no rule for that at all: it falls to the rule-4 tier, where
the reserved slot reaches one item a round, oldest first. #1513 has waited
six rounds on #1294 for precisely that reason and **no rule was broken at
any step** — the blocked item's only exit depended on an issue the ranking
had no reason to reach. So before the four rules are applied, every
`pile:blocked` item whose `**Blocked on:**` line names an issue **lends that
issue its own claim**: the blocker is ranked by the best rule holding for it
*or* for anything waiting on it, and inside the rule-4 tier it takes the
earlier of the two filing dates.

Inheritance, rather than counting a blocker-of-one under rule 1, because
rule 1 is the top rank and the threshold of two is what earns it that place:
a blocker of one would otherwise outrank a live security defect on strictly
less leverage than rule 1 was written for. Inheritance gives it exactly the
priority of the thing it is holding up and no more. If the blocked item
would have been a rule-2 defect, its blocker ranks there; if the blocked
item would itself have sat in the tier, the pair sits in the tier at the
older of the two dates, which is the half #1513 was missing — a blocker is
usually the *newer* of the pair, so its own filing date is the worst
possible key for an item whose job is to release an old one. A blocker of
two or more still holds rule 1 on its own, unchanged — which is also why
the lend is **not** gated on how many wait: gating it would write rule 1's
threshold a second time, and the two would disagree the first time the
threshold moved. Lending always, from every dependent, costs nothing where
rule 1 already holds. The relation is a query over the blocked pile's
bodies, so it needs no bookkeeping and disappears by itself the round the
blocked item moves.

**The lend is one hop, and that is enough.** Where A waits on B and B waits
on C, only B's own claim reaches C, because the lend is applied to the ready
pile and B is not in it. The chain still terminates: C is built, B moves to
ready the round its named issue closes, B is built, A moves. One link a
round, which is a queue rather than a stall — and a transitive lend would
rank C for a claim two removes away that may be settled by the time it
arrives.

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
measurement rather than a rule: `scripts/backlog_metrics.py` computes the
tier and the proposal reports its size beside the residue, a tier growing
across four rounds says so, and the lever in the meantime is the one that
already exists — the owner pins.

**That measurement did not exist for the first nine rounds, so the slot's
own defence had never been tested.** The proxy to hand was "ready items
carrying no priority label", which #1511 measured at 59 of 62 and called
plainly wrong, because rule 2 is a property of the defect and not of a
label. What is computed now is **rule 1 only**, from the blocked pile's
`**Blocked on:**` lines, and **the figure is published as an upper bound**,
because everything holding rule 2 or rule 3 is still counted inside it.
Upward is the only direction it may err in: an item wrongly left in the tier
is one the slot may reach early, while an item wrongly taken out is one
nothing reaches at all, which is the leak the slot exists to close. The
approximation fails that way by construction — a dependency stated anywhere
but the `**Blocked on:**` line contributes no edge, and each such miss
leaves its item *in*. The same section names the tier's oldest members,
which is the slot's candidate list — a list to read rather than a verdict,
since the oldest may be exactly the rule-2 item the computation cannot see.

**Rule 3 is reported and not applied, and the first cut of this got that
wrong.** It excluded an item whenever the item cited a path three or more
recent issues also cited, which conflates a citation with a production. Of
the ten items it unranked, three were wrong and all three by one mechanism:
#1462 (chromadb credentials) and #1463 (filter-shaped routes) were taken out
of the tier on `docs/development/issue-processing.md`, which they cite only
because this campaign's issues quote its gates — and which leads the hot
list at 7 for exactly that reason. **The campaign's own procedure file had
become a seam that silently unranked unrelated work**, which is the failure
the upper bound exists to forbid. The second objection is fatal on its own:
what that file scores is a function of how many issues happened to quote a
gate this month, so the tier would move by three for reasons with nothing to
do with the backlog — and being comparable round over round is one of the
three things this figure is for. So the hot seams are listed for whoever
ranks to apply rule 3 **by reading**, which is what "it sits on a seam"
always required, being a judgement about an issue's subject rather than
about its text. The cost is stated rather than hidden: no false positives by
construction, and a false negative for every genuine rule-3 item — on the
corpus that is the seven of those ten that were right, plus the whole
health-signal seam (#1515, #1516, #1547, #1565, #1568 — five issues in a
month, not one of which names a file).

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
re-sorts the whole backlog each round, which would be work proportional to the
backlog for comparisons already made. Losing that comparison is not a state:
the item keeps the place the comparison gave it, and the pile drains past it.
That only holds if the place survives the round, so the **ranked head** — the
items holding rules 1-3, in order — is written into the `Queue` body and
carried forward. An order is the one thing a label cannot hold, and it is safe
there because *Propose* is that body's only writer and because the head
decides nothing on its own: membership is the label, so a name the dispatch
predicate no longer admits — blocked, mid-move, pulled or closed — is skipped
rather than obeyed. *Build* states it; note that the mid-move case is the one
the ready query alone does not catch, which is why that predicate is more than
the ready query. The rule-4 tier is not written down, and needs not be:
"oldest first" is recoverable from the issues themselves at any moment, so the
tier is the part of the pile that costs no bookkeeping. Three things move an
item up out of its turn — a new priority label, another issue on the same
seam, a citation from a new issue — and an item that gets none of them still
arrives, because rule 4 orders its tier and every round takes the oldest of
it. An earlier draft listed a fourth trigger, "an age threshold recorded in
the pile", which nothing ever recorded a value for. A trigger with no value is
not an exit.

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
- **A pull request that ships a guard gets a pass briefed to DEFEAT the
  guard.** Not to review the code — to answer one question: *what can be
  re-introduced without this noticing?* Four rounds running the defect has
  been in the guard the pull request installed (#1516, #1544, #1553, #1555),
  and in round 8 it was in **both** pull requests that shipped one. Each time
  the owning agent had already verified the pull request and passed it.

  The reason that verification keeps failing is worth stating, because it
  looks like diligence. It checks the guard's **answer on the current tree** —
  the census counted 13 then 2, the scale floored at 1.0 across NaN, inf, zero
  and denormal. Both true, and neither says anything about reach, which is the
  whole of what a guard is for. Two measured examples:

  ```
  census:    log_once(..., user_id=uid)          CAUGHT
             log_once(..., **{"user_id": uid})   MISSED    <- 20 live sites,
                                                              2 in the guarded file
  workflow:  assert "schedule" in condition      passes on `!= 'schedule'`
  ```

  So the brief is: re-introduce the defect in every shape the codebase
  actually uses, and report which the guard misses. Count the live sites of
  each shape — a miss on the house idiom is a different finding from a miss on
  a shape nobody writes.

- **Measure an over-approximation's cost; never tune a guard until it is
  quiet.** Widening a guard invites narrowing it again when it lights up, and
  narrowing-until-silent is how the original blind spot got there. State the
  false-positive count and read each one before deciding. On #1553 the
  widening cost **zero** across 27 previously-invisible sites, and the single
  new finding was correct and allowlisted with a reason — which is only
  knowable because it was counted rather than assumed.

- **Review what feeds a detector as hard as the detector.** When a pull
  request's centre of gravity is a clever algorithm, that is where reviewers
  look and where the author has already been careful. A round-6 review swept
  all 226 read-sites of an AST walk — f-strings, concatenation, path division,
  globs — found nothing, then looked one line earlier at how the walk's input
  was produced and found the defect: `git diff --name-only` prints only a
  rename's *destination*, so moving a file into an inert directory arrived as
  one harmless path with the source invisible. That repository's normal
  archival move (321 of them in `main`'s history) defeated the whole design.
  Budget explicit attention for a detector's inputs and outputs; the plumbing
  is under-looked precisely because the algorithm is interesting.

- **State N before fixing a duplicated rule.** Say how many implementations
  exist and show the scan that found the number, then ship that scan as a
  test. A guard must also declare where its rule *can* be violated and fail
  if it did not look there: the repository's oldest guard has been green for
  eight months while watching three directories that contain none of the
  violations it exists to catch.
- **Read CI for the regression check; never recompute it.** The question is
  "did this branch break something that worked", and CI answers it on both
  sides for nothing. It runs `pytest tests/` twice on every push — standalone
  and cloud — and it runs on every merge to `main`, so the merge base already
  carries a verdict. Ask for it **by commit**, never by listing runs and
  grepping: `gh run list --branch main --limit 60` and `--limit 5` return
  different pages, so the grep form reports "no run" for commits that have one.

  ```bash
  ci_verdict() {   # green on BOTH sides = no regression. Anything else is not a pass.
    gh api "repos/FaultMaven/faultmaven/commits/$(git rev-parse "$1")/check-runs" --paginate --jq '
      [.check_runs[] | select(.name | test("^Test (Standalone|Cloud)$"))]
      | group_by(.name) | map(max_by(.started_at))
      | if length == 0 then "NO RUN - not a pass"
        else map("\(.name)=\(.conclusion // .status)") | join("  ") end'
  }
  ci_verdict <merge-base>;  ci_verdict <pull-request head>
  ```

  `max_by(.started_at)` is load-bearing: a commit can carry several runs — a
  re-push or a second branch at the same commit re-triggers it — and the newest
  is the one that describes the current state. **Green base plus green head is
  the whole check.** A red head against a green base is this branch's regression, with
  no comparison needed. Only when the base is *also* red does a comparison
  arise, and then it is a diff of the two CI runs' failure lists.

  **Never use a local run as the baseline.** Locally `main` fails around six
  tests on the pinned dependencies while CI is green on that same commit, so
  a local baseline measures the box. Round 3 spent **15.3 hours of wall clock
  on 32 whole-suite runs — 64% of a 24-hour session** — building failure-set
  comparisons to work around that local-only problem, and re-ran the base side
  separately for three pull requests that shared one merge base. The base side
  of a comparison is a constant: it belongs to the merge base, not to the pull
  request and not to the review round.

- **The CI verdict is the owning agent's, and nobody else's.** Three review
  lanes in one round each stalled polling `Test Standalone` / `Test Cloud`.
  The rule above says how to get the answer and never says whose answer it
  is, and that gap is what they fell into. A reviewer's output is findings;
  whether CI is green on the final head is a *merge* criterion, read once by
  whoever merges. A review that catches itself waiting on a check run should
  report what it has and label the rest unreached — a partial review with an
  honest gap beats a complete one that arrives after the decision it was for.

- **Blame a finding before acting on it.** `git blame` the line to the commit
  that introduced it. A finding in code the branch added only to answer an
  earlier review round is not on the issue's seam however true it is, and the
  test is: *would this line exist if review had never run?* If not, file it.
  On fm#1498 every one of round 4's fourteen findings blamed to a
  review-response commit and none to the fix — the fix had drawn no finding in
  four rounds, its responses ran to twice its size, and it should have shipped
  after the first round. A review loop with no scope gate generates the
  defects it then finds.

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
  it, returns the item to the blocked pile — one label on that issue — and
  reports it in the result as
  `pulled` rather than delivered, which is also what tells the next round's
  settlement that this close was not the owner's. The condition is "cannot
  clear", not "needs a ruling": a finding that is merely too hard trips none
  of the four escalation triggers, so gating the exit on a ruling would
  leave that case with no exit at all — the same shape as the leak this rule
  exists to close. "The lane could not clear it" is itself a call for the
  owner: ship the bug, or take it on themselves. Closing a pull request is
  the one action on one the owning agent may take; merging is never one.
  Without that exit this exception would be the only state here the *round
  itself* cannot leave — step 5 could never run, so no other lane's work
  would reach the owner either, and holding the round up is precisely what
  the pull rule exists to prevent.
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
  is drawing from it slower than review is filling it. The tier is what
  `scripts/backlog_metrics.py` prints under *Rule-4 tier*, an upper bound by
  construction; compare it against that same figure from an earlier round
  and never against a proxy, because the two move for different reasons. The
  first round to compute it has nothing to compare against and says so.

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
draft and were found only this way, two more — just as old — were found only
on the second such read, and the third read found two more again, both of
them live at the time: a partly-delivered remainder with nowhere to go, and
a blocker of exactly one with no promotion path. Three reads, eight leaks,
and not one of them found by reading the prose. **Then read the result the
same way before shipping it.** A pass that closes leaks writes new states:
the pass that added the blocking-finding exception under *Building* created
the first state here that the round itself could not leave, and its own
first fix for that left the merely-too-hard case with no exit either. The read that counts
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
