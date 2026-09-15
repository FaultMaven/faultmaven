# Issue processing

How issues are ranked, picked, worked and closed, and the campaign against the
defect class that generates most of them. Written for #1453; the numbers it
rests on are in that issue's evaluation comment and are re-derivable with
`python scripts/backlog_metrics.py`.

## Words used here

Two words do a lot of work below, and one of them means two different
things in this repository.

**Seam.** The set of places in the code where one rule has to hold. Every
place that writes a conversation row is one seam; every place that reads
that table in order is another. A seam is not a file or a module, it is
"all the copies of one rule", which is why a defect on a seam tends to have
siblings. A finding is **on-seam** for a pull request when it lives in code
that pull request is already changing, and **off-seam** when it does not.
That is the whole test for whether it gets fixed there or filed as its own
issue: the pull request already owns the code, so fixing it costs nothing
extra and leaves no second instance behind.

**Head.** The newest commit on a branch. A pull request's head moves every
time a new commit is pushed to it, so "the final head" means the code as it
stands after the last fix, not as it was when the pull request was opened.
The distinction matters because a fix written to answer a review is itself
new code that nobody has reviewed. Note the collision: **alembic head**,
further down, is an unrelated term meaning the newest database migration.

## Why this exists

The open-issue count sat between 50 and 63 for ten weeks while ~25 issues a
week were opened and ~22 closed. Five things the count was hiding, from the
#1453 evaluation over the 320 issues filed since July 2026:

1. **The backlog is two populations.** 56% of closures happen within a day of
   filing: an issue found and fixed inside the same lane. The rest is a
   *residue* that survives its first week, after which only a deliberate pick
   closes it. The residue takes in ~8 issues a week and drains in bursts; the
   flat count is the residue, not churn.
2. **The residue is mostly not defects.** Of the open set, 16 are decisions,
   11 features and 5 investigations against 15 defects. A lane picks defects,
   so the rest sit: a P2 issue's median time to close is 17 days, P3 is
   effectively never, and 12 of 15 P3 issues are still open.
3. **The defects being found are old.** Median latency between the defective
   line's introduction and the issue is 144 days; 62% are over 90 days and
   only ~13% under a week, a share that has been stable month to month while
   the over-90-day share rises. That is the signature of draining a fixed pool
   rather than generating new debt.
4. **Fixes rarely break things; they expose things.** 13 of 193 defects (7%)
   were attributed to a prior change, and blame puts the median age of the
   code that broke at 83 days: the change moved a producer and an old consumer
   did not know. Per lane, follow-ups *caused* by the fix run at 0.06; follow-
   ups merely *found* during it at 0.39. The caused series terminates after
   one step; the found series is bounded by the pool, not by any rate.
5. **The dominant class is "one rule, N implementations, no owner".** 39% of
   defects since July carry it (76 of 193; 47 at high confidence), median
   N = 2, and it is repo-wide: 27–50% of the issues in every code module, not
   concentrated in `modules/case`. The regression class in (4) is the same
   shape seen from the other side: N was never recorded, so a change could not
   enumerate what it touched.

Consequences for the procedure: the residue drains only if decisions get
ruled and features get scheduled or closed, not by fixing defects faster; and
the class is retired by recording N and checking it, not by fixing instances.

## 1. The queue

**Where.** One pinned tracking issue titled `Queue`, refreshed by
`/process-top-issues`. GitHub is where the owner reads; a file in the repo
would need a PR per re-rank. Labels stay as they are (`P0`–`P3`, `bug`,
`tech-debt`); they were a parking lot, not a queue, and this does not try to
make them one.

**Size.** The top **five** are the project. Below them the list is ranked but
not committed to; an item enters the top five only when one leaves.

**What an entry carries** (all required; a lane starting on an entry that
lacks one fills it in before touching code):

| Field | Why |
|---|---|
| Rank and **why** | Otherwise re-litigated every cycle. One sentence. |
| **Kind**: `defect`, `decision`, `investigation`, `feature`, `chore` | They have different exits (below). A lane that treats a decision as a defect stalls or guesses. |
| **Done when** | Observable. For a defect: the failing test that now passes and the guard that keeps it so. For a decision: the ruling, recorded where the code will find it. |
| **N**, for a duplicated-rule item | The count of implementations and *how it was determined* (the grep or AST scan, not a memory). Three lanes this session shipped having found N − 1. |
| **Blocked by** | Issue numbers and shared files. Lanes touching the same seam do not run in parallel; a PR that depends on another unmerged PR is not opened. |

**Kinds and their exits.**

- `defect` — the lane ends with a PR. The PR carries the fix, the test that
  reproduced it, and for a duplicated-rule item the guard that pins N.
- `decision` — the lane ends with a **decision memo** as an issue comment:
  the options, what each one changes, a recommendation, and which
  documented design each would override. The owner rules in a reply; the
  item is then re-filed as a defect or feature with the ruling as its
  spec. The lane writes no code.
- `investigation` — the lane ends with a **measurement**: the question, the
  method, the numbers, and what they change. Numbers are produced by a
  script that is committed, so the measurement can be repeated.
- `feature` — needs a spec before a lane; the queue entry says who writes
  it. The spec is an issue comment, like the memo (`docs/working/` is
  gitignored, and a spec nobody has accepted is not a permanent document
  yet). A feature with no spec and no date is closed with a comment, not
  carried.
- `chore` — a PR, no test required beyond what the change itself needs.

**Ranking.** Highest first: a security defect; a defect on a seam that has
produced three or more issues in the last month (it will produce more until
its rule has an owner); a decision that blocks two or more other items; the
oldest residue item with a stated done-when. Features rank by whether someone
will use the result this quarter. Everything else is by age.

**Refresh** (the first step of every run):

1. For each of the top five: still open? Not closed as a duplicate? Not
   overtaken by a merged PR that touched its seam? Still the kind it was
   filed as? Drop what fails; promote from below.
2. New issues since the last refresh are classified and ranked in.
3. Re-rank if the residue is growing on a seam the top five do not cover.
   The seam comes from step 2's own classification — which paths each new
   issue names — not from `scripts/backlog_metrics.py`, which reports the
   residue by week, age and priority label and has no code dimension.

## 2. The lane procedure

One **owning agent** runs a cycle. It refreshes the queue, dispatches one
**subagent per item** into its own worktree, reviews, and reports. It decides
everything it can decide; the escalation list below is closed, not
illustrative.

**Per lane, in order.** Design → implement in a worktree → push → one
adversarial review → one fix commit → one delta pass → escalate what is left.
The owner merges. A subagent never merges and never stacks a PR on an
unmerged branch.

**Gates the owning agent enforces**, each from a failure that cost time:

- **Worktree per lane, base checked.** A lane branches from `origin/main`
  fetched now, not from a shared checkout. Before believing a failure of
  `test_the_committed_contract_is_consistent_with_its_base`, compare the
  lane's contract version with `origin/main`; a stale base fails it with a
  message that reads like a real defect. Parallel lanes collide on global
  scalars (the API contract version, the alembic head): the owning agent
  arbitrates those, and never dispatches two lanes onto the same seam.
- **N stated before the fix.** A duplicated-rule lane states N and the scan
  that produced it in the PR body, and ships the guard that re-derives N,
  with a positive-control floor so a scan that stops matching fails rather
  than passing on nothing. "Found three" with no scan is not done.
- **Consumers enumerated before a producer changes.** A PR that changes what
  a writer writes, what a field means, or what a placeholder looks like lists
  its readers, with the grep that found them. This is the regression class:
  the code that breaks is old code that read the old meaning.
- **Review is a gate, not a courtesy.** `/code-review` found a defect in every
  lane this session, including in fixes of previously reviewed defects and
  in two changes that mutation testing had certified. A PR is not reported
  as done until a review has run on its final head and every finding is
  fixed, comment-answered as a design call, or filed off-seam.
- **Rework after merge goes to a new PR.** A PR merged at an earlier head
  while corrections were in flight left `main` carrying known defects twice.
  Once merged, nothing more is pushed to that branch.
- **The owning agent verifies, it does not relay.** A subagent's finding is
  acted on only with execution evidence: the command, its output, on a tree
  whose `module.__file__` was checked. A suggested remedy is run before it
  is adopted. After any rework the lane's own mutation matrix is re-run.
- **The whole tree is ported.** Before a lane's PR is reported, `git status`
  in its worktree is clean or every remaining file is named in the report.
- **Root before scope; scope by ticket, never by depth.** A lane investigates
  to the root cause before deciding what it ships, and designs the fix for
  the class the instance belongs to: a defect that is one of N
  implementations is fixed by giving the rule an owner and a guard, not by
  correcting the copy that was reported. When the root's fix is larger than
  one PR, scope is managed by filing a ticket that names the root and the
  design, and the lane's PR states which part of the root it closes. What a
  ticket may never do is stand in for the investigation: "look deeper later"
  is an unfinished lane, not a scope decision. Related work uncovered on the
  way is folded in when it shares the root and filed only when it does not.

**What escalates to a human** (everything else the owning agent decides and
records in the PR or issue):

- A change that would override another layer's *documented* decision.
- A product question about what the user sees.
- Two defensible options that lead to materially different work.
- Anything that deletes data, changes a wire contract's major version, or
  changes what a deployment needs configured.
- Merging. Always.

## 3. The trigger

`/process-top-issues` (`.claude/commands/process-top-issues.md`) runs one
cycle: refresh the queue, take the top five, dispatch, review, report. It
opens PRs and hands back; it does not merge, and it stops to ask only on the
escalation list above. Its end-of-cycle report is a comment on the `Queue`
issue: each item's state (PR opened / memo posted / measurement posted /
blocked, with the link), what was escalated, what the refresh changed, and
the current `backlog_metrics` summary.

## 4. The campaign: a hypothesis and its test

**Read this section as a bet, not a plan.** The evaluation established that
the duplicated-rule shape is the most COMMON defect shape, at 39% of
defects. It did not establish that retiring it lowers the defect rate, and
one measurement points the other way: lanes on duplicated-rule issues
produce fewer follow-ups than other lanes (0.43 against 0.73) and N does
not predict how much a fix will spawn. Prevalence is not leverage.

Nor can the bet be settled from the existing corpus. The guards that would
be the evidence are too new to have a track record:

| guard | landed |
|---|---|
| config purity | 2026-01-09 |
| single JWT mint surface | 2026-08-06 |
| the other four | 2026-09-02 or later |

The one guard with history is actively discouraging, and it is worth being
exact about why. It enforces "only the config package and the composition
root read the environment". It watches `faultmaven/services`, `core` and
`api`. Those three directories contain **zero** environment reads. All
thirteen files that do read the environment sit in `infrastructure`,
`modules`, `jobs` and `bootstrap`, which it never looks at, and they are
reading real deployment config (`JOB_RUNNER_TYPE`, `ENVIRONMENT`,
`ENABLE_TRACING`). The guard has been green for eight months because it
looks only where there is nothing to find.

Issue #1332 is the same failure one step further on: a sibling compliance
gate scans a directory that does not exist, and nothing noticed until an
audit. So a guard's REACH is the whole of its value, and a guard that is
green tells you nothing until you know what it looked at.

So the order below is a sequence to TEST, not a commitment to deliver.
Item 2 is the test: it sits on the seam that produced six issues in three
weeks, so it is the fastest to answer, and a seam that keeps producing
issues after its guard refutes the bet. Items 3 to 6 are contingent on
that answer, and the campaign is abandoned rather than continued if the
seam stays noisy.

### The class

A rule, fact or vocabulary with N implementations and nothing that records
N. It shows up as "two writers disagree", "five implementations, no caller",
"four channels, one hidden by a fixture", and as the regression where a
producer changed and an old reader did not. The cost of a correct fix is
finding all N; miss one and the follow-up issue is guaranteed.

### Making N machine-checkable

The repository already does this, unsystematically: a dozen scan-style
guards under `tests/unit/architecture/` and several dozen test files in all
that walk the package with `ast` or a regex (the exact counts are what the
register below will hold; a count quoted here would drift). They work
(`#1428`'s reader-order scan, `#1397`'s one-kind-field scan, the JWT
single-mint-surface test). What they cost is 120–230 lines each, because
every one re-implements the walk, the floor, and the identity control. The
campaign's first deliverable therefore is not another guard but the thing
that makes a guard cheap:

1. **A shared scan harness** (`tests/unit/architecture/_scan.py`): walk the
   package and `tests/`, apply a regex or AST predicate, and assert a
   **floor** (the scan visited at least this many modules), an **identity
   control** (it visited this one), and — the requirement the config-purity
   guard's eight green months argue for — **coverage**: the scan must
   declare where the rule CAN be violated and fail if it did not look
   there. A guard whose reach excludes every live violation is worse than
   no guard, because it reports safety. A guard becomes the predicate,
   the expected N, and a docstring naming the issue: ~30 lines. It grows out
   of what exists — `tests/import_guard_ast.py` already holds the AST
   predicates for one family, and two tests carry a private `_scan(paths)`
   walker with different contracts (`test_swallowed_first_party_imports`
   tolerates absent paths and reports unreadable files;
   `test_optional_dependency_detection` does neither) that the harness
   unifies. Item 1's lane starts by counting these with a scan, not from
   this paragraph.
2. **An invariant register** (`docs/development/invariants.md`): one row per
   rule the codebase has been bitten by, naming the *owner* (the single
   implementation every call site goes through), N today, the guard that
   pins it, and the issues it retired. A duplicated-rule issue is closed
   only by adding its row. The register is what the refresh step reads to
   see whether a new issue is an instance of a known rule.

### Ordering

Retire a class before fixing an instance. Item 1 is worth doing whatever
the bet turns out to be, because it makes a guard cheap and every guard
here needs the positive controls it carries (#1332 is what an uncontrolled
guard becomes). Item 2 is the experiment. Items 3 onward run only if item
2's seam goes quiet:

| # | Item | Retires | Instances waiting |
|---|---|---|---|
| 1 | Scan harness + register | the cost objection to every guard below | — |
| 2 | One constructor for a `case_messages` row, and a scan that every `INSERT INTO case_messages` goes through it | the seam that produced #1397, #1420, #1428, #1442, #1451, #1452 in three weeks | #1452, #1451, #1442 |
| 3 | A route-auth scan: every route under `faultmaven/api` and every module router declares an auth dependency or is on a named public allowlist | per-route authentication drift across every router, not one file | #1447 |
| 4 | A vocabulary scan: a field that has an enum is written only from that enum | the "no single canonical vocabulary" family | #583, #1040, #1163 |
| 5 | Consumer enumeration as a PR rule (section 2) | the exposed-old-reader regression class | procedural |
| 6 | A single decoder for JSON metadata columns, with a scan on `json.loads(...metadata...)` (12 sites today) | the copied-decoder family (#928, #1107) | — |

Items 2 to 4 each close their waiting instances in the same PR as their
guard; the guard is what makes the instance's fix complete. That is also
what keeps the experiment honest: item 2 pays for itself in closed issues
whether or not the bet holds, so running it costs nothing beyond the
guard.

### Stopping condition

The campaign ends when all four hold for four consecutive weekly cycles.
Each names where it is read from, because only the first is machine-read:

| # | Condition | Read from |
|---|---|---|
| 1 | **Residue net ≤ 0** every week, and the open set's median age falling | `scripts/backlog_metrics.py` — the weekly table and the open-set line |
| 2 | **New defects with the duplicated-rule signature under 15%** of defects filed in the window (from 39%) | the refresh step's classification of each new issue |
| 3 | **Follow-ups caused by a fix: none** in the window | the refresh step, per issue: the script counts follow-ups but cannot read CAUSATION, so a lane marker is read by hand for whether the parent's fix introduced the defect or merely exposed it |
| 4 | **Every closed duplicated-rule issue has a register row** with a guard | `docs/development/invariants.md`, which campaign item 1 creates; until it exists this condition is not yet evaluable and the cycle report says so |

The caused-per-lane rate is already 0.06; condition 3's target is that a
change exposing an old reader is caught by the consumer-enumeration gate
rather than by a follow-up.

**None of the four may be read from a window in which the looking fell
off.** A review does not create a defect, it records one that was already
there, so the number of issues filed is partly a measure of how hard the
period looked. Review is the largest single channel, at just under a third
of everything filed since July. A cycle that skipped reviews would show a
smaller residue inflow with the defect mass untouched, and would satisfy
condition 1 by going blind. So the cycle report states what share of merged
pull requests were reviewed, and a window where that share fell is not a
window the conditions can be read from. The same caution applies to the
open count generally: it moves with attention as well as with health, which
is the whole reason this document measures the residue and the fix latency
instead of the count.

When they hold, the campaign's artefacts stay (the harness, the register,
the metrics script, this procedure) and the queue goes on being processed
as ordinary work.

There are two ways this ends early, and both are acceptable outcomes rather
than failures to argue with. If item 2's seam keeps producing issues after
its guard, the bet is refuted and items 3 onward are not run. If after eight
weeks condition 2 has not moved, the class was misidentified. In either case
this document is revised, not extended.
