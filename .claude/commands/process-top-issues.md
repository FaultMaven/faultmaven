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
| `pile:blocked` | needs an owner ruling, waits on an issue or a condition, or a lane stopped on it |
| `pile:yours` | only the owner can run it — live deployment, credentials |
| `tracking` | **in no pile** — the `Queue`, a campaign tracker, a document refined each round |

Create any that is missing before you use one; `gh issue edit` fails the
**whole** edit on a label the repository does not have.

**`tracking` takes an issue out of every pile, whatever `pile:` label it also
carries.** A tracker is not work a lane can be dispatched against, so in ready
it would be counted in the rule-4 tier and named as the reserved slot's buy;
and bare it would read as an unsorted arrival, which step 2 sorts straight
back into ready (#819 carried `tracking` and `pile:ready` both; #1499 sat in
ready untagged). So the ready query below excludes it, step 2 does not sort
it, and a `pile:` label on it is a leftover to remove — the metrics name any
under *Tracking, but carrying a `pile:` label*. Moving an issue **into**
tracking is the same two edits as any move, add first: `--add-label
tracking`, then `--remove-label pile:<whichever>`; the intermediate is a
tracker with a leftover, which nothing dispatches. **Out of it**: a tracker
closes when what it tracks is done — whoever closes the last of it closes the
tracker too — and one that turns out to be work gets a pile and loses the
label, again add first. The metrics list every open tracker each run, so the
label cannot quietly take real work out of every pile.

Three rules when you apply `pile:blocked`, the first and last from a pile
that held at 12 for six rounds and then lost ten of twelve in one reading:

- **Say what it is blocked ON, in the issue BODY, in one form**, as you
  label it. "Needs an owner ruling", "needs #N to land" and "waits until
  something is observed" look identical in a label and only the first is
  owner latency (#1513 was miscounted for six rounds; #673 was counted as a
  ruling nobody owed). One line, the reference — or the word `condition` —
  first:

  ```
  **Blocked on:** #1294 — the arithmetic moves when that ladder splits.
  **Blocked on:** an owner ruling on which axis owns the degrade policy.
  **Blocked on:** condition — `llm_stop_reasons_total{stop_reason="max_tokens"}` above 1% of calls over a week.
  **Blocked on:** condition — unobservable today: nothing records <what>; observing it needs <the instrument>.
  ```

  The third form is a **deferral on something to be observed** — neither a
  question nor a dependency — and the script counts it in its own bucket,
  *waiting on a condition*, never as a ruling. State the condition as
  something step 2 can **check** — a query, a metric, a log or trace count,
  and where to look — because step 2 checks it every round and a condition
  it cannot run is one nothing ever checks. If nothing can observe it today,
  write `condition — unobservable today:` and what would have to exist; the
  metrics name those under *Nothing can check*, because their only exit is
  the owner. `condition` must open the statement, as a reference must: `an
  owner ruling on the condition for …` stays a ruling.

  The body rather than a comment, because this is a current value that
  *Picking*'s promotion rule and `scripts/backlog_metrics.py` both read
  every round, and a thread holds a history. The reference **first**: a
  reference anywhere else is read as neither a dependency nor a ruling, so
  `**Blocked on:** an owner ruling on #1294's shape` comes back as *stated
  but unreadable* and is reported for you to reword. A line naming an issue
  **and** something it cannot resolve — `**Blocked on:** #1294 and #9999` —
  is reported as both, so the half it could not read is never dropped. The
  label with nothing after it, or with `TBD` — or `condition` with no
  condition after it — counts as **stating nothing** rather than as a
  ruling: *Needs your call* lists the ruling bucket, and an entry there with
  no question in it is a round spent waiting on an answer nobody was asked
  for. The metrics count all of these, so no gap is invisible.
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
**No** `pile:` label means an unsorted arrival, which step 2 sorts — unless
the issue carries `tracking`, which is in no pile and is never sorted — and
nothing bare is ever dispatched either, because step 4 dispatches from the
ready query, which a bare issue is not in.

**The ready query** — the one step 4 dispatches from, and the one every
"ready" in this file means:

```bash
gh issue list --state open --label pile:ready --limit 500 \
  --json number,title,labels,createdAt,body \
  --jq 'map(select(any(.labels[]; .name == "tracking") | not))'
```

The tracker is dropped in `--jq` rather than with `--search "-label:tracking"`
on purpose: `--search` switches `gh` to the search index, which lags a label
edit, so an item pulled a moment ago could still come back from it.

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

**Write `##`; match `#` or `##`.** One hash or two, on all four — the rest
of the heading is exact. This is not politeness to the owner: it is the
producer half of this rule, which lives in prose and in another file while
the consumer here is a grammar. Every result the board carries — rounds 7,
8, 9 and 10 — was posted as `# Round <N> — result`, matched nothing, and so
read as *the owner answering the proposal above it*. On a finished and fully
merged round that sends the next reader to step 3, hunting rulings in its
own result comment. Both directions of this rule are stated below; that one
is the unsafe one and the one that has happened, because it needs only the
agent to forget a character, while the safe one needs the owner to type a
heading they have no reason to type. No other comment kind opens with
`Round <N> — <state>`, so the tolerance introduces no ambiguity, and it
makes ten rounds of history readable (#1585).

Now scan back to the **newest comment of a recognised kind** — past notes,
past owner comments, however many — and read what follows it. Take the first
row that matches:

| the newest recognised comment, and what follows it | state | do |
|---|---|---|
| none | before the first round | step 1 |
| a **result** or a **withdrawal** | between rounds | step 1 |
| a **proposal**, with an owner comment after it | answered | step 3, take the answers |
| a **proposal**, no owner comment after it, but **any** of its *Building* items has a lane pull request opened **after that proposal**, open or merged | approved out of band, and some of its work is built | post the approval as a `## Note —` so the next reader need not re-derive it, then step 4 — whose dispatch predicate skips a lane branch that already has such a pull request, which is what stops a fresh lane being sent over work already on `main` — and step 5 |
| a **proposal**, no owner comment after it, and no lane pull request opened after it | waiting on the owner | report what it is waiting for, and stop. Do not re-propose |

```bash
# One query, bounded by THIS PROPOSAL'S timestamp rather than by a page
# size, then filtered on the <prefix>/<n>-<slug> branch tail.
gh pr list --state all --limit 100 --search "created:>=<this proposal's timestamp>" \
  --json number,headRefName,state \
  --jq '.[] | select(.state != "CLOSED")
            | select(.headRefName | test("/(1450|1511|1512)-"))'  # the round's numbers
```

**Both halves of that query are load-bearing, and each replaces something
that was measurably wrong.**

- **Bounded by the proposal, not by a count.** A repo-wide `--limit` page is
  what the historical case this row exists for rolls off. Worse, without the
  date bound the row fires on a pull request from a *previous* round: an item
  delivered under `Refs #<n>` stays open, is re-ranked into the next round,
  and its old merged pull request then matches — so the row would transcribe
  an approval nobody gave and step 4 would dispatch every other item in that
  round, unapproved. That breaks this file's own *Never build an item with an
  unanswered question*. A round opens a handful of pull requests in a few
  hours, so the window is small and nothing can roll off it.
- **Matched on the branch TAIL, not on a prefix.** Over the last 200 pull
  requests the numbered lane branches run `fix/` 110, `feat/` 7, `docs/` 6,
  `demo/` 5, `chore/` 1 — so a `head:fix/<n>-` search misses 19 of 129 and
  answers 0 for #1554, whose branch was `chore/1554-remove-dead-session-deps`.
  For those items step 4 would dispatch over merged work while this row read
  the round as unanswered: both halves of the failure, on 15% of branches.

**Any**, not all: one such pull request is the evidence that the round was
approved. Step 4 then dispatches whatever has none, which is the part that
was approved and never built.

**This row does not skip settlement; it defers it to the step that does it.**
Step 5 posts the result these pull requests were missing, and the *next*
invocation reads that result and settles every issue it names — including
the one whose merged pull request said `Refs #<n>` and left it open, which
is the case step 1 exists for and the case that left #1447 and #918 to be
closed by hand. Settling here instead is not possible: step 1 takes its
issue↔pull-request pairs from a result table that, by the definition of this
row, has not been written yet.

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
abandonment comment carries.

**The one case this still cannot see** is an **owner** comment opening with
one of the four headings, which reads as yours and leaves the round waiting.
That is the safe direction and it is reported as such: the round stops
naming what it waits for, and the owner says in their next comment that the
one above was theirs — the exit is a human word, and there is no mechanical
one, because nothing distinguishes the two authors. Widening the match to
`#` or `##` widens this case by exactly the owner comments headed with a
single hash; the trade is deliberate, because the direction it closes ends
in a round being built on rulings nobody wrote and this one ends in a round
asking.

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
| the round reported it `pulled` | **place it by *What escalates***, in the same two label edits and for the reason the row above gives. With nothing recorded on the issue since the pull that is `pile:blocked` — a pull enters that pile by its own door — and doing it again is a no-op. With a ruling recorded it is wherever that ruling sent it: `pile:ready`, `pile:blocked` again for a deferral, `pile:yours`; a ruling implying no work closed the issue, which matched the **first** row and never reached this one. **An answer arriving before this step is the normal path, not an edge case** — the proposal carrying a pulled item's question is the very next one, so by the time the settlement walks the row the owner has usually ruled and step 3 has placed it, and forcing `pile:blocked` moves it back to where *Needs your call* re-asks a question that has been answered (#512, round 11 → 12). Say nothing more — the pull already recorded what stopped it, which is the only thing this row does differently from the one below. The row may name no pull request at all, because a lane pulled mid-build opened none |
| the issue is open, its pull request closed unmerged | the owner abandoned it — comment that the work was built and the pull request closed unmerged, link it, then **place it by *What escalates*** the same way; with nothing recorded that is `pile:blocked`, and the next proposal asks whether to build it another way or close it. Do not guess why — placing on a ruling the owner *did* record is the opposite of guessing, and the two rows have to compute the same pile or the reserved `pulled` value starts carrying state |

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
**Except anything labelled `tracking`, which is in no pile** — the `Queue`
first among them: it is the board, not an item on it, and a `pile:ready` on it
would put it in the rule-4 tier and in reach of step 4. An arrival that is
itself a tracker gets `tracking` rather than a pile. `pile:yours` is work no
agent can do: a live deployment check, a console or
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
- an item carrying **no** `**Blocked on:**` line, one whose line is empty or
  `TBD` (the script counts those two together as *stating nothing*), or one
  the metrics report as *stated but unreadable*, gets a readable one now:
  you are about to phrase its question for the proposal, and that line is
  the phrasing written down. The script counts them, so the gap is visible
  rather than merely present;
- an item **waiting on a condition** — the metrics list each under *Waiting
  on a condition* with the condition quoted — is **checked**: run what the
  condition names, and move the item to ready the round it holds. One you
  cannot run from where you stand — a deployment's Prometheus you have no
  access to — is not skipped: its row in *Needs your call* says `not
  checked: needs <what>`, which hands the check to the owner that round
  rather than letting it lapse in silence. A ruled
  deferral whose line is still a ruling-shaped sentence gets rewritten into
  `**Blocked on:** condition — …` now, or the script keeps counting it as a
  question. One the metrics name under *Nothing can check* goes into *Needs
  your call* with its condition marked unobservable and the owner's three
  exits as the options — build the measurement (a ready item), re-rule, or
  close — because nothing else will ever move it.

**Then read the ready pile's question list, which the ready query cannot
settle either.** An item that entered ready carrying a question, a
dependency or a deferral — or grew one in its thread since — has no other
reader: this step sorts only unlabelled issues, and ready's only other exits
are the rule-4 slot and being ranked into a round. Round 15 found **5 of 81**
ready items that were not ready work (#835, #723, #1114, #1040, #1463). The
metrics list the candidates under *Ready items that read like a question*:
ready items whose body carries decision, gating or trigger language and no
recorded ruling (a `**Ruled` body line, or a `Ruling` / `Ruled` / `Owner
ruling(s)` / `Decision record` heading in the body or a comment). **Read
each one; the list is never a label move** — the language is a symptom, and
most hits are prose about the code. For each, one of:

- it is ready work — leave it. Nothing is written, so the list names it
  again next round; that re-read is the price of never moving an item on
  the heuristic alone, and it is small because the list is;
- it needs a ruling, waits on an issue, or waits on a condition — move it to
  `pile:blocked` (the two edits) with its `**Blocked on:**` line in the form
  that fits;
- it is mixed — split it, as for any label;
- it is a tracker — `tracking`, per *The piles are labels*;
- its question was answered in a form the list does not recognise — record
  the ruling in the body as a `**Ruled <date>.**` line, so the next round
  does not read it again. Before moving an item to blocked, **read its
  comments for a ruling under any heading**: round 15 moved #1451 and #1502
  to blocked as open questions when both had been ruled under `## Ruling
  recorded`, which the detector of the day did not read — asking the owner a
  question they have answered is the expensive direction here.

This is reading, not re-sorting: nothing is re-ranked, and an item that stays
keeps the place its first comparison gave it.

**Last, the trackers** — the metrics list every open one under *Tracking, in
no pile*. A tracker's children are closed by lanes that never look at it, so
nothing else notices when its work is done: close any whose tracked work has
all closed, citing what closed it. The board and a document refined each
round never qualify, and that is the whole of the check. A `pile:` label the
metrics name on one (*Tracking, but carrying a `pile:` label*) is removed now.

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
what it is holding up and no more. Lend from every dependent, not only
where there is one: a count here would restate rule 1's threshold, and the
two would disagree the first time it moved.

**The first thing the round's capacity buys is the oldest rule-4 item** —
the oldest ready issue holding none of picking rules 1-3 — ahead of rules
1-3, unless the tier is empty or the pinned items left no capacity at all.
Rules 1-3 outrank that tier every time, so without the reserved place its
drain rate is zero. It does not bound the wait, so the size is reported:
`scripts/backlog_metrics.py` computes the tier under *Rule-4 tier* and names
its oldest members, which is this slot's candidate list. **Read the
candidate before ranking it.** The figure is an upper bound — only rule 1 is
computed, and an item holding rule 2 or rule 3 is still counted in the tier,
so the oldest member may be one. The same section lists the **hot seams**
for you to apply rule 3 by reading; it does not apply them itself, because
the path an issue cites is not the seam that produced it.

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

Deferred items belong in this table too — every item the metrics list under
*Waiting on a condition* — with the ruling and the condition they wait on in
place of the question and options, and what this round's check of it found.
Listed, never re-asked — and never omitted, or they leave every pile. The
exception is one under *Nothing can check*: that is a question, because its
only exit is the owner — build the measurement, re-rule, or close.

### Yours to run (never ranked into a round)
| # | what only you can do |

### Settled from last round
| PR | issue | what it did |

### Measurement
<python scripts/backlog_metrics.py --weeks 8>
Piles: ready <n> · blocked <n> · yours <n>
Rule-4 tier: <the script's figure> (last round: <n, or "n/a — first
  computed figure">) — an upper bound
Carrying more than one pile label: <the script's line, or none>
Read like a question: <the script's count> — <what reading each one did>
```

The rule-4 line is **quoted from the script's own *Rule-4 tier* section**,
never estimated and never derived from labels: the proxy that was used for
nine rounds read 59 of 62 and was measuring the wrong thing. Compare it only
against the same script's figure from an earlier round — and where there is
no earlier one, write `n/a — first computed figure` rather than reaching for
the retired proxy, which the sentence above forbids comparing against.

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
  `pile:blocked` where it is and rewrite its `**Blocked on:**` line into the
  condition form — `**Blocked on:** condition — <what step 2 will check>`,
  or `condition — unobservable today: …` — so the metrics stop counting it
  as a question. List it in the next proposal as answered-and-waiting, not
  as a question, and check the condition when you sort: it moves to ready
  the round it holds. Do not close it: a deferral is "not yet", not "never".
  A deferral whose trigger is another issue landing is a dependency, and
  takes the `#N` form instead.
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
being the one under *The piles are labels* (open, `pile:ready`, and not
`tracking`), and "already built"
being a pull request on this item's lane branch that is **open or merged**
and was opened **after this round's proposal** — step 0's query, unchanged:

```bash
gh pr list --state all --limit 100 --search "created:>=<this round's proposal>" \
  --json number,url,headRefName,state \
  --jq '.[] | select(.state != "CLOSED")
            | select(.headRefName | test("/<n>-"))'   # the <prefix>/<n>-<slug> tail
```

**Merged, not only open.** Step 1 will not start a round while a previous
round's pull request is open, so within a normal round "already built" and
"open" coincide — but step 0's out-of-band row is reached *precisely* when
they are merged, and an `--state open` test finds nothing there and
dispatches a fresh lane over work already on `main`. Closed-unmerged is
excluded because the owner abandoned it, which step 1 turns into a blocked
item; it would not be in the ready query either. **After the proposal**,
because an item delivered under `Refs #<n>` is still open and can be ranked
again: its previous round's merged pull request would otherwise make this
round skip it as built. Matched on the branch tail rather than a prefix,
because `fix/` is the convention and not the whole of it — `chore/`,
`feat/`, `docs/` and `demo/` carry 19 of the last 129 lane branches.

Three conditions, because the query expresses only the first — and its
`tracking` filter is part of that first: a tracker carrying a leftover
`pile:ready` is singly pile-labelled, so without it nothing here would skip
the tracker. An item mid-move
carries both labels and `--label pile:ready` still returns it, and an item
whose lane has delivered stays open and singly-labelled until the owner merges.

That is one predicate and it is the only one — a re-entry into this step reads
it rather than a rule of its own. It is a query rather than a test on a name,
which is what lets it cover the three ways an approved item stops being
dispatchable: a pull earlier in this step or in a previous invocation of it
(the item gained `pile:blocked`, and between the two label commands it carries
**both** — still `pile:ready`, and still not dispatchable), an item delivered
and closed by a previous round (labels survive closing, so only `--state open`
excludes it), and an item a lane has already built — which its lane pull
request identifies whether that pull request is still open or has since been
merged. **Skipped for that third reason is not dropped**: the item joins the
verify-and-review pass below carrying the pull request it has, exactly as a
returned lane would — except that a pull request the owner has already
merged is *reported* as merged rather than re-reviewed, because a review
after the merge changes nothing and the worktree it was built in may be
gone. It still gets its row, which is what the next settlement reads. (A
feature lane opens none, so this does not reach one — its spec is a comment,
which a re-run can no more recognise as its own than the abandonment
comment, and one duplicate is the price.)

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
settlement posts an abandonment comment, and nothing else: the pile is
placed by *What escalates* either way. A mid-build pull's row names no pull request, because none
was opened. The line below carries what stopped it.

Pulled: #N — <the question, or what stopped the lane>
Filed on the way: …
Waiting on you: merge the pull requests above.
```

The `## Round <N> — result` heading is load-bearing, not decoration: it is
what step 0 scans back to, and a result posted under any other heading reads
as the owner answering the proposal above it. **Two hashes, and the rest of
the string exactly as written above** — step 0 matches one hash or two and
nothing else, and the four results already on the board were each posted
with one, which is the drift this literal exists to stop. Copy it; do not
retype it from memory.

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
- **Never post to the `Queue` without one of step 0's four headings**, and
  copy the heading rather than retyping it. A comment with any other heading
  is read as the owner speaking, so an unlabelled note of your own answers
  your own proposal — and so does a result whose heading is a character out.
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
