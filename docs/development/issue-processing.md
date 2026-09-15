# Issue processing

How issues are ranked, picked, worked and closed, and the campaign against the
defect class that generates most of them. Written for #1453; the numbers it
rests on are in that issue's evaluation comment and are re-derivable with
`python scripts/backlog_metrics.py`.

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

## 4. The campaign

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
   package and `tests/`, apply a regex or AST predicate, and assert both a
   **floor** (the scan visited at least this many modules) and an
   **identity control** (it visited this one). A guard becomes the predicate,
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

Retire a class before fixing an instance. In order:

| # | Item | Retires | Instances waiting |
|---|---|---|---|
| 1 | Scan harness + register | the cost objection to every guard below | — |
| 2 | One constructor for a `case_messages` row, and a scan that every `INSERT INTO case_messages` goes through it | the seam that produced #1397, #1420, #1428, #1442, #1451, #1452 in three weeks | #1452, #1451, #1442 |
| 3 | A route-auth scan: every route under `faultmaven/api` and every module router declares an auth dependency or is on a named public allowlist | per-route authentication drift across every router, not one file | #1447 |
| 4 | A vocabulary scan: a field that has an enum is written only from that enum | the "no single canonical vocabulary" family | #583, #1040, #1163 |
| 5 | Consumer enumeration as a PR rule (section 2) | the exposed-old-reader regression class | procedural |
| 6 | A single decoder for JSON metadata columns, with a scan on `json.loads(...metadata...)` (12 sites today) | the copied-decoder family (#928, #1107) | — |

Items 2–4 each close their waiting instances in the same PR as their guard;
the guard is what makes the instance's fix complete.

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

When they hold, the campaign's artefacts stay (the harness, the register,
the metrics script, this procedure) and the queue goes on being processed
as ordinary work. If after eight weeks condition 2 has not moved, the class
was misidentified and this document is revised, not extended.
