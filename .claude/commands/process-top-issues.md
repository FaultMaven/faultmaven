---
description: Run one round of issue processing — settle the last round, propose items and the questions blocking others, build what is approved, report. Opens PRs and files what it finds; never merges.
---

# /process-top-issues

**Purpose:** work the backlog down by fixing what matters most at its root, keep `main` green, and keep each fix no
larger than the defect needs. Nothing is hidden or closed to lower the count. The owner merges on your `## Merge-ready`
comment without reading the diff, so every PR you hand over must have been through the full review chain in step 4.

This file is self-contained. Memory notes point here and never restate it; where they disagree, this file wins.
**Changing this file:** you propose the edit as a PR, it goes through step 4 at `xhigh` with a design critique, and the
owner merges it; an edit replaces or tightens an existing rule where it can.

**Roles.** The owner rules on questions, approves rounds, and merges. You (the owning agent) propose, plan, verify
and report. Lanes (subagents) each implement one plan you wrote, in their own worktree; at most three run at once.
Reviewers are independent agents: they never wrote or planned what they review.

`$ARGUMENTS`: empty = continue from wherever the round is; numbers = pin those issues into the next proposal;
`propose` = post a fresh proposal even if one is outstanding.

## State

- **Piles are labels:** `pile:ready` (known work), `pile:blocked` (waits on a ruling, an issue or a condition),
  `pile:yours` (only the owner can run it), `tracking` (in no pile: the Queue, trackers). Move with two edits, add
  first (`--add-label` then `--remove-label`); more than one pile label reads as blocked. **The ready query** (Pick
  and Build use it) returns the open `pile:ready` issues outside `tracking`; an item with a second pile label is
  skipped:

  ```bash
  gh issue list -R FaultMaven/faultmaven --state open --label pile:ready --limit 500 --json number,title,labels,createdAt \
    --jq 'map(select(any(.labels[]; .name == "tracking") | not))'
  ```
- **Every open issue outside `tracking` carries one priority label.** It is re-set by these definitions whenever the
  item moves between piles, unless the owner set it or a ruling states it:
  - `P1`: a security, tenancy or data-loss exposure (latent included), or a defect a user or operator hits in
    normal use (a wrong result, a 5xx, lost work). A `P0` label, if the owner uses one, is read as `P1`, ranked first;
  - `P2`: any other defect reachable today;
  - `P3`: everything else: latent shapes, cleanup, docs, test infrastructure, enhancements. The owner may raise
    any item, and a ruled feature takes the priority its ruling states (ask if it states none).
- **An issue closes only for a reason:** it is fixed; it duplicates another issue; you verified its premise is dead;
  it is a tracker whose tracked work has all closed; or an owner ruling closes it. Nothing closes an issue to lower
  the count: not age, not a timer, not silence.
- **A blocked item's body** has one line: `**Blocked on:** #N …`; `**Blocked on:** an owner ruling on …` (also for a
  pull or a PR closed unmerged, with the question that settles it); or
  `**Blocked on:** condition — <a check you can run>` (a future event too, e.g. "redis-py ≥ 8 is pinned").
- **Authorship is read from headings**, because you post with the owner's account. Your comments carry exactly one:
  on the Queue (the open, pinned `tracking` issue titled `Queue`; step 0 finds it) `## Round <N> — proposal`,
  `## Round <N> — result` or `## Note — <what>`; on issues and PRs `## Plan`, `## Review`, `## Delta`,
  `## Design critique`, `## Merge-ready`, `## Evidence`, `## Ruling`, `## Closed`, `## Pulled` or `## Delivered`. Any
  comment without one is the owner speaking. An answer the owner gives in session is transcribed, quoting them, under
  `## Owner, in session — <date>`, and is read as theirs.

## Where an item stands

Steps 1 and 4 take the first row matching an item's records. **Its PR** is the newest PR on its plan's branch (its
lead's, for a shared lane) whose body names it with a closing keyword or `Refs`, else the newest that names it (the
command lists them, newest last). `R` is the head in its PR's newest `## Review` or `## Delta`; `H`, the newest of
those and `## Merge-ready`. A **pull** is the newest `## Pulled` on the item, or on its PR when that comment closed
it; a **live ruling**, a `## Ruling` on the item after its newest plan and pull, and after its PR merged when it did.

```bash
set -o pipefail; gh pr list -R FaultMaven/faultmaven --state all --search "<n> in:body" --limit 1000 --json number,state,headRefName,body,createdAt \
  | jq --arg n <n> '[.[] | select(.body | test("(?i)\\b(close[sd]?|fix(e[sd])?|resolve[sd]?|refs):?[ \\t]+"
    + "((FaultMaven/faultmaven)?#|https://github\\.com/FaultMaven/faultmaven/issues/)" + $n + "\\b"))] | map(del(.body)) | sort_by(.createdAt)' || echo error
```

| # | the records show | next |
|---|---|---|
| 1 | its PR merged | at step 1: its merged head must be the head its newest `## Merge-ready` names, with no `## Pulled` after it; else file `PR #<p> merged at an unvouched head` (search first), naming `git log --oneline R..<merged head>`, or the whole PR when no `R` exists, in `pile:blocked` on an owner ruling on how to review them. At step 4, an open item whose PR merged since this round's proposal waits for step 1's edit-down and a new proposal; one whose PR merged before it reads on from row 11 |
| 2 | the item, or an item its PR closes, in `pile:blocked` with a `**Blocked on:** an owner ruling on …` line or with no `**Blocked on:**` line; and either that PR open, or a plan of the round being settled with no pull newer than it | that item's pull is unfinished: finish *To pull an item* for it from its first step not done |
| 3 | the item in `pile:blocked` | it waits on its blocker |
| 4 | its PR open, its newest `## Review` or `## Delta` clean or its findings waived by a live ruling, and a required check red because `main` is (a `main red` issue, step 4.6) or a service it needs is down | waiting: leave it until that issue closes or the check passes when re-run (`gh run rerun <run-id> --failed`); then merge `main` in and go on at row 6 or 9 |
| 5 | its PR in the merge queue at a merge-ready's `H`, with none of row 6's triggers (`gh api graphql -f query='{repository(owner:"FaultMaven",name:"faultmaven"){pullRequest(number:<p>){isInMergeQueue timelineItems(last:5,itemTypes:[REMOVED_FROM_MERGE_QUEUE_EVENT]){nodes{... on RemovedFromMergeQueueEvent{createdAt reason}}}}}}'`) | leave it |
| 6 | its PR open after a merge-ready, and code after the merge-ready's head (the vouched-head check), an owner comment after the merge-ready, a conflict with `main`, a queue removal dated after it with the PR not back in the queue (row 5's query; quote its `reason`), or a red required check | refresh: merge `main` in, the change made (by the lane, by you for a conflict), a delta of what changed (step 4.5), CI, merge-ready again; a refresh with a `## Delta` after the merge-ready goes on at row 8 or 9, unless an owner comment, a conflict or a red check is newer than that delta. A `fixed` finding in that delta gets one fix commit and one delta; a `fixed` finding in that one pulls the item. A red check the merge base also has makes it row 4, never a pull |
| 7 | its PR open after a merge-ready at `H` | merge-ready: carried under *Yours to run*; while an item it closes is in `pile:blocked`, it is not merge-ready and waits on that item's blocker |
| 8 | its PR open; its newest `## Review` or `## Delta` has a `fixed` finding neither landed nor waived by a live ruling | a pull trigger met since its newest live ruling, or ever when it has none, pulls it: the cap or size (step 4.5), or a `fixed` finding in a re-entry delta (row 12) or in a refresh's second delta (row 6). Else the fix commit, then a delta of the commits since `R` (row 12's, when its live ruling grants a re-entry) |
| 9 | its PR open; its newest `## Review` or `## Delta` is clean, or its findings are waived by a live ruling | the vouched-head check from `R` (step 4.6); code after `R` gets a delta first; then merge-ready, or row 7's wait while an item it closes is in `pile:blocked` |
| 10 | its PR open, no review | *Verify*, then the review (steps 4.3–4.4) |
| 11 | a `## Plan — round <N>` newer than any pull, live ruling and PR of the item, and no open PR | resume a lane on that plan, in the branch and worktree it names |
| 12 | a pull that closed its PR, which has not merged since, and a live ruling granting one more fix and delta, an exception commit or a merge as is | re-entry: `gh pr reopen` it. A merge as is waives its outstanding findings, each filed as an issue: row 9. Else the lane adds one commit on its branch, and a delta of only that commit, outside the cap, where a `fixed` finding pulls it again |
| 13 | a live ruling implying other work (a re-plan, a build at a new estimate) | rebuild: a plan for this round (step 4.1, with a critique when 4.1 calls for one) on a new branch, with its own review and PR; a closed PR stays closed |
| 14 | none of the above | new: step 4.1 |

## 0. Locate the round

Find the newest proposal or result on the Queue, the issue this prints:
`gh issue list -R FaultMaven/faultmaven --label tracking --search 'Queue in:title' --json number,title --jq '.[] | select(.title == "Queue") | .number'`

| newest | then |
|---|---|
| none or a result | step 1 |
| a proposal with an owner comment after it | step 3 |
| a proposal, no owner comment, but a lane PR (branch `fix/<n>-<slug>`, step 4.1) for one of its items opened after it | approved out of band: post a `## Note`, then step 4 |
| a proposal, no owner comment, no lane PR | stop and say what it waits for; do not re-propose |

## 1. Settle the last round

1. **Each PR the last result named,** and each item it carries: do what its row in *Where an item stands* says.
   Rows 1–7 settle it, as does a closed PR (step 1.2 places its item). On rows 8–10, a PR with a `## Delta` after its
   merge-ready, or reported waiting last round, goes on at its row; else a build was interrupted: report and stop.
2. **Each issue row**, taking the first that matches: **closed**, nothing to do; **open with its PR merged**, close it
   if everything it asked for is delivered, else edit it down to the remainder, title and body; **`pulled`, or its PR
   closed unmerged**, `pile:blocked` with `**Blocked on:** an owner ruling on …` naming what stopped it, unless an
   owner ruling recorded since then places it.

## 2. Propose

1. **Sort.**
   - Give every open issue that lacks a pile label or a priority label the missing one(s). Skip `tracking`.
   - Move to ready any blocked item whose blocking issue has closed or whose condition holds (run each you can).
   - Close a tracker whose tracked work has all closed.
   - Run `.venv/bin/python scripts/security_alert_sweep.py`. Put each `untracked` group's alert URLs on a body line
     starting `**Alerts:**` in the open core issue covering it, or file one per root opening `**Repo: FaultMaven/<r>.**`
     then that line, verified by the alert page and manifest. Anything written about a private repo's alert names only
     its URL, kind and count. Raise secret-scanning alerts with the owner in session, never in writing. Report the
     non-secret `unknown`/`unavailable` lines in the proposal. Dismiss nothing without the owner's ruling on the issue.
2. **Pick.**
   - **The design-flaw pass** gives one row per seam worked since the last proposal, meaning a seam with a merged or
     open PR in that window. Each row holds the **chain** (the defects fixed and still open on it, by number); the
     **mechanism** (the code fact that produced them, at `path:line`) and the **structural fix** (the one change that
     makes that class impossible, and the open issues it would absorb), or "not yet analysed"; its **open count** now,
     and at each earlier proposal that printed one for it; and one **recommendation** no ruling has decided since the
     counts, mechanism and structural fix it reads, the first that applies, for *Needs your call*: a freeze, when the
     seam has no open root issue and neither of its last two printed steps lowered the count (now not below the last
     printed count, nor that one below the one before it); a root analysis, when it has no mechanism; a ruling on the
     structural fix, when it names both.
   - **Rulings carry the state.** A freeze ruling names the seam's root issue (labelled `root-issue`, filed if none is
     open, in `pile:blocked` with `**Blocked on:** an owner ruling on its root analysis and structural fix`) and lists
     exactly the issues it blocks on the root issue's `**Blocks:**` line: each listed issue that no open PR carries
     and that is not already in `pile:blocked` moves there with `**Blocked on:** #<root>`; one an open PR carries
     stays with that PR; one already blocked keeps its line. When a listed issue's own blocker clears while the root
     issue is open, by Sort or by a ruling, its line becomes `**Blocked on:** #<root>`. While the root issue is open,
     no issue on its `**Blocks:**` line gets a new plan beyond the stages a ruling sets, and an open PR on one
     finishes by its row in *Where an item stands*. A ruling on the structural fix names the seam's root issue (filed
     as above if none is open) and lists its stages, each filed as an issue with its budget and the issues it
     delivers; a stage's PR closes only those (another it delivers is a change to the list) and says `Refs #<root>`;
     when the last stage's PR merges, close the root issue under `## Closed`, citing the stages. That list places
     them, as step 3 places any ruled issue. A ruling that declines the structural fix closes the root issue and lists
     where its issues go. Every change to a root issue's `**Blocks:**` line or a stage's list is a ruling, except a
     finding added under *Filing what you find*.
   - From `pile:ready` only. **Design flaws first:** an item on a seam whose structural fix is ruled to be built,
     while its root issue is open, is the next stage that ruling sets, in the order the owner last approved. Then
     `P1`, `P2`, `P3`, oldest first within each. One slot stays reserved for the oldest `P2` waiting over 30 days that
     may be planned, even when design flaws could fill it. Skip an item that an open PR from an earlier round still
     carries (its PR, *Where an item stands*), and any item on that PR's seam until it merges.
   - Items on one seam share a lane and a PR.
   - Size the round by review effort: at most 5 items and 3 lanes, plus up to 3 **Next** items the approval also
     covers, none a migration. An item touching a security boundary or storage takes its own lane and PR; a migration
     is alone in its round, with no Next items.
3. **Check premises.** `git fetch origin main`, then confirm each picked item's code points still show the defect.
   An item whose premise looks dead is built as a **verification** item (step 4).
4. **Post the proposal**, then stop. Build nothing that has an open question.

   ```
   ## Round <N> — proposal
   ### Design-flaw pass (one row per worked seam: chain · mechanism · structural fix · open counts · recommendation)
   ### Building
   | # | P | root (one line) | done when (observable behaviour) | size (product / test lines, est.) |
   ### Next (same columns; each starts when a lane's PR is merge-ready or pulled, or its item closes)
   ### Needs your call
   Every item blocked on an owner ruling: question · options · recommendation · what it unblocks.
   Every condition you could not check: what checking it needs; you run it, re-rule, or close the item.
   ### Priorities I set (change any)
   ### Yours to run
   ### Settled from last round
   ### Scorecard
   open <n> (P1 <a> · P2 <b> · P3 <c> · blocked <d>) · alerts: <the sweep's `queue:` line>, filed <f> · last round: closed <x>, filed <y>
   ```

## 3. Take the answers

Record each ruling on its own issue under `## Ruling`, then place the issue: ready when it implies work (the ruling is
its spec, with its priority), blocked with a `condition —` line when it defers, closed, quoting it, when it implies
none, and yours when only the owner can run it. If you cannot tell where a ruling places the issue, that is the next
question; unanswered questions stay where they are.

## 4. Build

Dispatch only approved items that the ready query returns with a single pile label, and, when a lane's PR is
merge-ready or pulled or its item closes, the first **Next** item not on any open PR's seam (a refresh waits for a
free slot). Each starts at its row in *Where an item stands*; one whose plan (newer than any pull) says
`Shares lane with #<lead>`, at its lead's.

1. **Plan** it yourself on `origin/main`, posted on the issue as `## Plan — round <N>`. Items sharing a seam name
   their lead as `Shares lane with #<lead>`. The plan covers:
   - **the root,** with its code points. The root is where the defect starts, however deep. When investigating shows
     the cause sits in a deeper layer than the issue names, that deeper cause is this item's root and this PR fixes
     it. Never fix a symptom and file its root. A root that needs a ruling, or a budget past the bound below, pulls
     the item with that question, whose options include "mitigate the symptom now, with the root as its own P1".
     Only the owner may choose that option;
   - **the change,** and what is out of scope; **the lane:** branch `fix/<n>-<slug>` in directory `<n>`, both suffixed
     `-r<N>` (this round) after a pull or when either exists, based on `origin/main` unless a ruling names another;
   - **N,** for a duplicated rule, with the scan that counted it (run for the plan, not committed as a test);
   - **the consumers,** for a value whose meaning moves (the search that found them);
   - **the invariant:** one or two sentences that must hold after the merge;
   - **the tests:** a regression test that reproduces each defect and asserts the behaviour, and for each behaviour
     on `main` the change must not alter, the test that pins it (existing or new). No wall-clock or performance
     thresholds. A repo-scanning guard, census or lint only when you can name an incident where the
     same class came back after an earlier fix. Such a guard is exercised through the path that runs it, and
     reverting the fix makes it fail;
   - **the size budget:** estimated product and test lines. A product budget past twice the newest estimate (the
     proposal's, or a later one an owner ruling set) and 50 lines over it pulls the item before a lane, as 4.5 does;
   - **a probe,** when the plan chooses a mechanism (a classifier rule, a predicate, a library behaviour): the
     realistic inputs you ran it against before sending a lane, negatives first, and what each returned;
   - **the decisions.** Any choice about what a user sees, overriding a documented decision, deleting data,
     changing a contract's major version, or between options of very different size is not yours: pull the item
     with the question;
   - **a design critique,** for a plan that changes a contract, a schema or what a user sees, touches a security
     boundary or storage, makes a design choice a ruling left open, is estimated over 300 product lines, or changes
     this file (always). It reviews the plan, not the written change, which the review covers: one Fable critique,
     from an agent that did not write or plan the work, before a lane is sent: the Agent tool with `model: "fable"`,
     read-only, briefed to challenge the plan rather than approve it. Post it as `## Design critique` with what you
     declined and why. A declined point about a decision reserved for the owner pulls the item with that question.

   For a **verification** item, run the reproduction yourself. If the defect is dead, close the issue with a `## Closed`
   comment citing what fixed it and the evidence. If it is alive, plan it as above.
2. **Lane.** Send a fresh agent on your model. Use `sonnet` only for a docs-only change outside `.claude/` and
   `CLAUDE.md`. Its prompt holds the issue, its rulings, your plan, the done-when, the branch, directory and base the
   plan names (new, or on an existing branch: rows 2, 6, 8, 10, 11, 12), and *Lane rules* below, verbatim.
   Run lanes in the background. Meanwhile, plan the next item; and once a round, check the 5 oldest ready `P3`s
   outside the round with no `## Evidence — premise` in 30 days. One is dead only when a reproduction showing its
   defect at the commit it cites no longer shows it on `origin/main` (never a latent one or one without such a
   reproduction; an error or a moved code point is not death): close it under `## Closed` with both runs; on each
   other, post `## Evidence — premise` with what you ran.
3. **Verify.**
   - Bring the head up to `origin/main` first: merge it in, and send a conflict back to the lane. If `main` has
     moved since you planned, re-run the plan's consumer and N searches.
   - Confirm the lane's head is the PR's head, and read the whole diff against the plan: a hunk the plan does not
     cover goes back to the lane once.
4. **Adversarial review.** The reviewer is independent: `/code-review`, running in its own context. Brief it to
   break the PR, not to approve it: the invariant, the done-when, the riskiest ways the change could be wrong, and
   what must not change. Your own diff read in *Verify* is quality control, not this review. Start the CI watch in
   the background at the same time.
   - **Runtime PRs: `/code-review xhigh <pr>`.** These change what runs in a deployment, a pipeline or an agent:
     `faultmaven/`, `alembic/`, `resources/`, `requirements/`, `pyproject.toml`, `Dockerfile`, `docker-compose*`,
     `.github/workflows/`, `.github/scripts/`, `.importlinter`, `pytest.ini`, `scripts/` that CI or CD runs,
     `.claude/` and any `CLAUDE.md`.
   - **Every other PR: `/code-review high <pr>`.** A mixed PR takes the higher level.
   - **A defeat pass** (a review briefed to get past the guard) only for a guard on a security or tenancy boundary
     that has live sites.

   Each finding is exactly one of:
   - **fixed:** it is part of this item. It breaks the invariant; or it is a defect in code this PR adds; or it is a
     crash, wrong result or regression the PR causes anywhere; or it is a test or guard this PR ships that cannot
     fail; or it shares the item's root;
   - **filed:** it is true, valid and not part of this item (*Filing what you find*, below);
   - **declined:** one line in the PR body saying why. Either it is not true (not reproduced, and not shown at
     `path:line` on `origin/main` or the PR head), or it is not valid (as defined below), or an open issue already
     covers it (add the evidence there).

   Post the verdict on the PR as `## Review — <head>`, with the fix list and the issues filed. A review or delta is
   **clean** when no `fixed` landing is outstanding and unwaived.
5. **Fix and delta.** The fix list goes to the same lane (`SendMessage`) as one commit. If the fix redesigns a
   mechanism, probe the new design first, as in the plan.
   - **The delta.** An independent reviewer always runs it; the author of the commits never does. It reviews what
     changed since the reviewed head against the fix list and the invariant. Brief it with that diff, which leaves
     out `main`'s commits, never the PR number, which would re-review the whole PR:

     ```bash
     git fetch -q origin main "pull/<n>/head"
     t=$(git merge-tree --write-tree <reviewed> "$(git merge-base <head> origin/main)" | head -1)
     git diff "$t" <head>
     ```

     Post its verdict on the PR as `## Delta — <head>`, with the reviewed head, the reviewer and each finding's
     landing.

   - **A fix delta with any `fixed` finding** gets one more fix and delta. A second fix delta with any `fixed`
     finding pulls the item, unless each such finding is a comment, docstring, document or missing test whose fix
     touches only comments, docstrings no code reads (not a model's or a route's), `docs/` or `tests/`: that fix,
     if its commit keeps to them, gets one more delta, once, outside the cap, where a `fixed` finding pulls the item.
   - **Size.** Passing twice the plan's product budget (or the newest budget an owner ruling set after it; never one
     you restated) and 50 lines over it pulls the item, as does growth carrying an owner-reserved decision. Test lines
     never count against it (#2198). Any smaller overrun, and any test overrun, is stated, with the new estimate, in
     the delta and the merge-ready.
   - **Never review the same code twice.** Each plan gets at most one design critique, and each PR one adversarial
     review and two fix deltas, plus the text-or-test one above; more fix rounds pull the item to the owner (a
     rebuild, row 13, is a new plan and PR). A re-entry, a refresh, a conflict resolution or a change the owner asks
     for is a delta of only what is new, outside that cap. A whole-PR review is never repeated, nor run as a delta.
   - **No cap narrows a root fix.** A structural fix's budget is the one its ruling sets. Reaching the size bound or
     the fix-round cap pulls it to the owner with the question, and never narrows the fix.
6. **Merge-ready.** Every required context must be `SUCCESS` or `SKIPPED` on the head:

   ```bash
   req=$(gh api repos/FaultMaven/faultmaven/rules/branches/main --jq \
     '[.[] | select(.type=="required_status_checks") | .parameters.required_status_checks[].context] | unique')
   gh pr view <n> --json statusCheckRollup | jq -r --argjson req "$req" '
     [.statusCheckRollup[] | {n: (.name // .context), t: (.startedAt // ""),
       c: (if (.status // "COMPLETED") != "COMPLETED" then "PENDING" else (.conclusion // .state) end)}] as $all
     | $req | map(. as $r | ([$all[] | select(.n == $r)] | sort_by(.t) | last) as $hit
                  | "\($r)=\($hit.c // "MISSING")") | join("  ")'
   ```

   - **A flake:** re-run it once. A running check reads `PENDING` (its `completedAt` is `0001-01-01T00:00:00Z`).
   - **An outside-service outage:** wait it out; if it outlasts the round, report the PR as `waiting on <service>`.
   - **A failure the merge base also has:** it is `main`'s. File it as a ready `P1` issue,
     `main red: <check> at <sha>` (search first), and post a `## Note — main red` on the Queue. The PRs it blocks are
     reported as `waiting on main`. The repair is proposed and approved like any item, unless the owner asks for it in
     session, which approves it as an item of its own: it is built at once, under step 4 in full, in the next free
     lane.

   **The vouched-head check.** The head you vouch for must be the last reviewed head (the head in the heading of the
   last `## Review` or `## Delta`), plus only clean merges of `main` and commits that change no file. Only the word
   `clean` passes; anything else is a code change, and gets a delta first:

   ```bash
   git fetch -q origin main "pull/<n>/head"
   git merge-base --is-ancestor <reviewed> <head> || { echo "code: <reviewed> is not an ancestor of <head>"; exit 1; }
   for c in $(git rev-list --first-parent <reviewed>..<head>); do
     read -r _ p q extra <<<"$(git rev-list --parents -n1 "$c")"
     if [ -z "$q" ]; then [ "$(git rev-parse "$c^{tree}")" = "$(git rev-parse "$p^{tree}")" ] || { echo "code: $c"; exit 1; }; continue; fi
     [ -z "$extra" ] && git merge-base --is-ancestor "$q" origin/main \
       && t=$(git merge-tree --write-tree "$p" "$q") && [ "$(printf '%s\n' "$t" | head -1)" = "$(git rev-parse "$c^{tree}")" ] \
       || { echo "code: $c"; exit 1; }
   done
   echo clean
   ```

   A PR is merge-ready only when, on its final head: every lane on it has ended, so nothing pushes after you vouch; no
   item it closes is in `pile:blocked`; the review and every delta are clean, with every finding landed or waived by a
   live ruling; the vouched-head check prints `clean`; every required check is green; it is not a draft; and on it you
   re-ran each lane's test command for any of its tests CI did not run there (a `skipif` the CI jobs meet, a suite the
   `changes` job skipped; `real_llm` only on the owner's approval). Then comment `## Merge-ready — <head>` with the
   base, the plan link, the design critique and its declined points, the review level, each delta's range and
   reviewer, the findings fixed, filed, declined and waived, the size against the budget, any command you re-ran with
   its tail output, and the checks.

To **pull** an item:
1. stop its lane;
2. move it to `pile:blocked` with `**Blocked on:** an owner ruling on …`, stating what stopped it and the options,
   then post `## Pulled` on it, saying why. For size or a deep root: build at the new estimate, mitigate now with the
   root as its own P1, or drop. For the fix cap: one more fix and delta, merge as is, re-plan, or drop;
3. close its PR with a `## Pulled` comment saying why. On a PR shared with other items, have the lane take the
   item's part out instead, and remove its `Closes`/`Refs` line from the PR body (`gh api -X PATCH`, read back).
   Then check that nothing will close it and its body no longer names it: it must print `none` (`error`: unread):

   ```bash
   p=<pulled>; k='\b(close[sd]?|fix(e[sd])?|resolve[sd]?'
   s="):?[[:blank:]]+((FaultMaven/faultmaven)?#|https://github\.com/FaultMaven/faultmaven/issues/)$p\b"
   grep -iqE "$k$s" <<<"Closes FaultMaven/faultmaven#$p" || { echo error; exit 1; }
   c=$(git log --format=%B origin/main..<head>) && b=$(gh pr view <n> --json title,body --jq '.title, .body') || { echo error; exit 1; }
   l=$(gh api graphql -f query='{repository(owner:"FaultMaven",name:"faultmaven"){pullRequest(number:<n>){closingIssuesReferences(first:50){nodes{number}}}}}' \
     --jq '.data.repository.pullRequest.closingIssuesReferences.nodes[].number') || { echo error; exit 1; }
   { grep -iqE "$k$s" <<<"$c" || grep -iqE "$k|refs$s" <<<"$b" || grep -qx "$p" <<<"$l"; } && echo named || echo none
   ```

   A commit message that names it pulls the whole PR;
4. carry on with the others.

**Filing what you find.** Whether a finding turns up while you investigate, plan, build or review:
- **File it** when it is **true** (reproduced, or shown at `path:line` on `origin/main`), **valid** (a real defect or
  gap, reachable today or latent; a style preference, or a simplification that changes no outcome, is not) and **not
  part of the item** (a different root). A latent one is a ready `P3` (`P1` if *State* makes it one) whose body says
  what would make it reachable, if known. File it as you find it, with `Found while working on #<n>`, the evidence, a
  pile and a priority.
- **Search open and closed issues first.** If an open issue already covers it, add the evidence there under
  `## Evidence`. If a closed issue matches, cite it in the new issue. Findings that share one root go in one issue.
- **A finding that shares an open root issue's root,** and that no open issue other than the root issue covers, is
  filed as its own issue, in `pile:blocked` with `**Blocked on:** an owner ruling on its place in #<root>`, and
  appended to the root issue's `**Blocks:**` line (a body edit), with an `## Evidence` comment there naming it; the
  next ruling on the root issue places it.
- **Fix the item's own root, a symptom of it, or a part of it in its PR;** file it only when a ruling waived it.

## 5. Report on the Queue

```
## Round <N> — result
| # | outcome (merged · merge-ready · carried · closed by verification or sweep · pulled · waiting on main or <service>) | PR | product +/− | tests +/− |
Closed <x> · Filed <y> · Net lines: product <p>, tests <t> · Open <before> → <after>
Filed: #<n> (P<k>) — <what, found while working on #<m>> — one line each
Waiting on you: queue each PR at its vouched head: gh pr merge <n> --match-head-commit <sha>
```

Post it when every dispatched item is merged, merge-ready, closed, pulled or waiting, with a row per item and its PR.
Every report states those four numbers and lists every issue filed. It carries forward a row for every PR still open
from an earlier round, until that PR merges or closes; a carried row adds nothing to the four numbers, which count
each PR in the round that first reports it. Then stop.

## Lane rules (sent verbatim)

- **Implement the plan; do not re-plan.** Where the code disagrees with the plan, stop and report what you found.
- **Worktree:** the branch, directory and base your brief names. A new lane runs, as one command,
  `git fetch origin main && git worktree add -b <branch> /home/swhouse/product/faultmaven/.claude/worktrees/<dir> <base>`,
  and stops and reports if the branch or the directory exists. A lane on an existing branch works in that directory,
  adding it with `git worktree add /home/swhouse/product/faultmaven/.claude/worktrees/<dir> <branch>` if only the
  branch exists, or as a new lane if neither does. Never work in the main checkout, and never delete its `./data`.
- **Before pushing,** run `black --check faultmaven/ tests/`, `ruff check faultmaven/ tests/`, `lint-imports`, and
  the tests that cover the change, plus `python scripts/check_contract_version.py` if `docs/reference/api/`
  moved. Not the whole suite: CI runs it.
- **Tests** reproduce the defect and assert behaviour. No wall-clock thresholds, and no repo-scanning guard unless
  the plan names one. Keep within the plan's size budget, and say so if you cannot.
- **Closing keywords go in the PR body only**, never in the title or a commit message, one issue per line. The body
  says `Closes #<n>` only when the issue as written is delivered. Otherwise it says `Refs #<n>`, and you comment on
  the issue, under `## Delivered`, naming what was delivered.
- **Return:** the PR URL, each command with its tail output, `git status --short`, every departure from the
  plan, and anything outside the plan you found, with its evidence. You do not file it; the owning agent does.

## Rules

- **Never merge.** Adding a PR to the merge queue is merging. The owner may authorize merging for the current
  session only, in as many words, and the authorization never carries over.
- **Write to issues and PRs only what these steps call for.** Every comment carries its heading (*State*). Close an
  issue only for a reason *State* allows.
- **Never ask mid-build:** pull the item instead. Never build an item with an open question.
- **Never stack PRs.** Never hand over a PR that is not merge-ready.
- **Never file or relay a finding you have not verified:** reproduced, or shown at `path:line` on `origin/main`.
- **Never poll CI in the foreground,** and never from a lane or a review.
