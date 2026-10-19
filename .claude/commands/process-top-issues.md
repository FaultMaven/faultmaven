---
description: Run one round of issue processing — settle the last round, propose items and the questions blocking others, build what is approved, report. Opens PRs; never merges; files an issue only on the owner's yes.
---

# /process-top-issues

**Purpose:** bring the open-issue count down, keep `main` green, and keep each fix no larger than the defect needs.
This file is self-contained. Only the owner changes it, and a new rule **replaces** an old one, so it does not
grow. Memory notes point here and never restate it; where they disagree, this file wins.

**Roles.** The owner rules on questions, approves rounds, decides what gets filed, and merges. You (the owning
agent) propose, plan, verify, review and report. Lanes (subagents) each implement one plan you wrote, in their own
worktree; at most three run at once.

`$ARGUMENTS`: empty = continue from wherever the round is; numbers = pin those issues into the next proposal;
`propose` / `build` = force that step.

## State

- **Piles are labels:** `pile:ready` (known work), `pile:blocked` (waits on a ruling, an issue or a condition),
  `pile:yours` (only the owner can run it), `tracking` (in no pile: the Queue, trackers). Move with two edits, add
  first (`--add-label` then `--remove-label`); more than one pile label reads as blocked.
- **Every open issue outside `tracking` carries one priority label:**
  - `P1`: a security, tenancy or data-loss exposure (latent included), or a defect a user or operator hits in
    normal use (a wrong result, a 5xx, lost work);
  - `P2`: any other defect reachable today;
  - `P3`: everything else: latent shapes, cleanup, docs, test infrastructure, enhancements. The owner may raise
    any item, and a ruled feature takes the priority its ruling states (ask if it states none).
  - A `P0` label, if the owner uses one, is read as `P1`, ranked first.
- **A `P3` in `pile:ready`** carries `**Expires:** <date, 30 days after it was labelled P3>` in its body. Write that
  line whenever an item becomes a ready `P3`, by whatever step, and when an older ready `P3` has none.
- **A blocked item's body** has one line in one of these forms:
  - `**Blocked on:** #N …`;
  - `**Blocked on:** an owner ruling on …` (also used for a pull, a PR closed unmerged, or an item over its size
    budget, with the question that settles it);
  - `**Blocked on:** condition — <a check you can run>` (a future event too, e.g. "redis-py ≥ 8 is pinned").
- **The Queue** is issue #1817 (pinned, `tracking`). Write your comments there under exactly one of these
  headings: `## Round <N> — proposal`, `## Round <N> — result`, `## Note — <what>`. Any other comment is the owner
  speaking.

## 0. Locate the round

Find the newest proposal or result on the Queue:

| newest | then |
|---|---|
| none or a result | step 1 |
| a proposal with an owner comment after it | step 3 |
| a proposal, no owner comment, but a lane PR for one of its items opened after it | approved out of band: post a `## Note`, then step 4 |
| a proposal, no owner comment, no lane PR | stop and say what it waits for; do not re-propose |

## 1. Settle the last round

1. **The last result's PRs.** For each PR it named that is still open and not in the merge queue:
   - refresh it if it conflicts, was dequeued, or has a red required check: merge `main` in, re-run CI, and delta
     any code change (step 4.5);
   - pull it if it cannot be refreshed (step 4's *pull*, which closes the PR).

   If any is still open after that, report it and stop.
2. **Each issue row**, taking the first that matches:
   - **closed:** nothing to do;
   - **open, PR merged:** close it if everything it asked for is delivered; otherwise edit it down to the
     remainder, title and body;
   - **`pulled`, or PR closed unmerged:** `pile:blocked` with `**Blocked on:** an owner ruling on …` naming what
     stopped it — unless an owner ruling recorded since then places it.

## 2. Propose

1. **Sort.**
   - Give every open issue that lacks a pile label or a priority label the missing one(s). Skip `tracking`.
   - Move to ready any blocked item whose blocking issue has closed.
   - Run each condition you can, and move items whose condition holds; list the ones you cannot run as
     `not checked: needs <what>`.
   - Close a tracker whose tracked work has all closed.
2. **Expire.** Close, as not planned, each ready `P3` past its `**Expires:**` date with no owner comment since it
   was labelled. Comment: `Expired: P3, 30 days (/process-top-issues). Reopen with a reason.`
3. **Pick.**
   - From `pile:ready` only: `P1` before `P2`, oldest first within each. Reserve one slot for the oldest `P2` that
     has waited over 30 days. `P3` only when the owner pins it.
   - Items on one seam share a lane and a PR.
   - Size the round by review effort, not item count: at most 5 items and 3 lanes. An item touching a security
     boundary, storage or a migration is a round by itself.
4. **Check premises.** `git fetch origin main`, then confirm each picked item's code points still show the defect.
   An item whose premise looks dead is built as a **verification** item (step 4).
5. **Post the proposal**, then stop. Build nothing that has an open question.

   ```
   ## Round <N> — proposal
   ### Building
   | # | P | root (one line) | done when (observable behaviour) | size (product / test lines, est.) |
   ### Needs your call
   Every item blocked on an owner ruling, with question · options · recommendation · what it unblocks.
   Every filing candidate not yet answered (step 5): security/tenancy ones first and uncapped.
   ### Priorities I set (change any)
   ### Expired this round · expiring before the next
   ### Yours to run
   ### Settled from last round
   ### Scorecard
   open <n> (P1 <a> · P2 <b> · P3 <c> · blocked <d>) · last round: closed <x>, filed <y>
   ```

## 3. Take the answers

Record each ruling on its own issue. Then place the issue:
- **implies work:** ready, with the ruling as its spec and its priority;
- **defers:** blocked, with a `condition —` line;
- **implies none:** close it, quoting the ruling;
- **the owner's to run:** yours.

For a filing candidate:
- **file:** create it, with a pile and a priority;
- **drop:** remove it from the list.

If you cannot tell which, that is the next question. Unanswered questions stay where they are.

## 4. Build

Per approved item, skipping one whose lane branch already has a PR, open or merged, opened after this round's
proposal. That item goes straight to *Verify* with the PR it has, or is reported as merged. This is what keeps a
re-entry, or an out-of-band approval, from sending a second lane over built work:

```bash
gh pr list -R FaultMaven/faultmaven --state all --search "created:>=<this proposal's timestamp>" \
  --json number,headRefName,state --jq '.[] | select(.state != "CLOSED") | select(.headRefName | test("/<n>-"))'
```

1. **Plan** it yourself on `origin/main`, posted on the issue as `## Plan — round <N>`:
   - **the root,** with its code points. Fix the root. Never fix a symptom and leave the root for later;
   - **the change,** and what is out of scope;
   - **N,** for a duplicated rule, with the scan that counted it. You run the scan for the plan; it is not
     committed as a test (below);
   - **the consumers,** for a value whose meaning moves (the search that found them);
   - **the invariant:** one or two sentences that must hold after the merge;
   - **the tests:** a regression test that reproduces each defect and asserts the behaviour. No wall-clock or
     performance thresholds. A repo-scanning guard, census or lint only when you can name an incident where the
     same class came back after an earlier fix. Such a guard is exercised through the path that runs it, and
     reverting the fix makes it fail;
   - **the size budget:** estimated product and test lines;
   - **a probe,** when the plan chooses a mechanism (a classifier rule, a predicate, a library behaviour): the
     realistic inputs you ran it against, negatives first, and what each returned. Run the probe before sending
     a lane;
   - **the decisions.** Any choice about what a user sees, overriding a documented decision, deleting data,
     changing a contract's major version, or between options of very different size is not yours: pull the item
     with the question.

   For a **verification** item, run the reproduction yourself. If the defect is dead, close the issue, citing what
   fixed it and the evidence. If it is alive, plan it as above.
2. **Lane.** Send a fresh agent on your model, or `sonnet` for a docs-only change. Its prompt holds the issue, the
   ruling, your plan, the done-when, and *Lane rules* below, verbatim. Run lanes in the background; plan the next
   item meanwhile.
3. **Verify.** Re-run the lane's test command from its worktree, and confirm its head is the PR's head. Read the
   whole diff against the plan. A hunk the plan does not cover goes back to the lane once.
4. **Review once.** Brief the review with the invariant and the done-when, and start the CI watch in the
   background at the same time.
   - **Runtime PRs: `/code-review xhigh <pr>`.** These are PRs that change what runs in a deployment or a
     pipeline: `faultmaven/`, `alembic/`, `resources/`, `requirements/`, `pyproject.toml`, `Dockerfile`,
     `docker-compose*`, `.github/workflows/`, `.github/scripts/`, `.importlinter`, `pytest.ini`, or `scripts/` that CI
     or CD runs.
   - **Every other PR: `/code-review high <pr>`.**
   - **A mixed PR** takes the higher level.
   - **A defeat pass** (a review briefed to get past the guard) only for a guard on a security or tenancy boundary
     that has live sites.

   Each finding is either:
   - **fixed:** it breaks the invariant, or is a crash, wrong result or regression the diff introduces on a
     reachable path; or
   - **declined:** one line in the PR body saying why. Out of scope, unreachable, a miss with no live site, or a
     simplification that changes no outcome.

   A finding outside the PR's scope becomes a **filing candidate** (step 5) when it is a security or tenancy
   exposure, or a defect of medium or higher severity that someone can hit. Anything lower is declined. Post the
   verdict on the PR as `## Review — <head>`, with the fix list.
5. **Fix once.** The fix list goes to the same lane (`SendMessage`), as one commit. If the fix redesigns a
   mechanism, probe the new design first, as in the plan. Then review only the fix commits against the fix list
   (the delta):
   - read them yourself when they are small;
   - otherwise send one reviewer, briefed with the commit range, never the PR number.

   If the fix breaks the invariant, or the item passes twice its size budget, pull it.
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

   - Re-run a flake once.
   - Wait out an outside-service outage (check the service's status page).
   - A failure the merge base also has is `main`'s: plan its repair first, on branch `fix/main-red-<short sha>`.
     Nothing merges before it.

   In-progress checks read `PENDING`; sort by `startedAt`, because a running check's `completedAt` is
   `0001-01-01T00:00:00Z`, not empty.

   Then comment `## Merge-ready — <head>`: the base, the plan link, the review (1 round + delta, k declined), the
   commands you re-ran with their tail output, and the checks.

To **pull** an item:
1. stop its lane;
2. close its PR, with a comment saying why. On a PR shared with other items, have the lane take the item's part
   out instead, and remove its `Closes`/`Refs` line from the PR body (`gh api -X PATCH`, read back), so the merge
   cannot close it;
3. move it to `pile:blocked` with `**Blocked on:** an owner ruling on …`, stating what stopped it and the choice
   (for size: the new estimate, build or drop);
4. carry on with the others.

## 5. Report

Comment on the Queue:

```
## Round <N> — result
| # | outcome (merge-ready · closed by verification · pulled) | PR | product +/− | tests +/− |
Closed <x> · Filed <y> (all on the owner's yes) · Net lines: product <p>, tests <t> · Open <before> → <after>
Filing candidates: <PR> — <what, who hits it and how, reproduced or not>
Waiting on you: queue <PRs>; each is merge-ready at the head its comment names.
```

Every report states those four numbers. Filing candidates carry into the next proposal's *Needs your call*:
- security and tenancy candidates are uncapped and listed first, until answered;
- any other candidate counts against a cap of 3 and is dropped after two proposals go unanswered.

Then stop: the round ends when the owner merges.

## Lane rules (sent verbatim)

- **Implement the plan; do not re-plan.** Where the code disagrees with the plan, stop and report what you found.
- **Worktree:** `git fetch origin main`, then
  `git worktree add -b fix/<n>-<slug> /home/swhouse/product/faultmaven/.claude/worktrees/<n> origin/main`.
  Never work in the main checkout. Never delete `./data` there: it is the live stack's database.
- **Before pushing,** run `black --check faultmaven/ tests/`, `ruff check faultmaven/ tests/`, `lint-imports`, and
  the tests that cover the change, plus `python scripts/check_contract_version.py` if `docs/reference/api/`
  moved. Not the whole suite: CI runs it.
- **Tests** reproduce the defect and assert behaviour. No wall-clock thresholds, and no repo-scanning guard unless
  the plan names one. Keep within the plan's size budget, and say so if you cannot.
- **The PR body** says `Closes #<n>` only when the issue as written is delivered. Otherwise it says `Refs #<n>`,
  and you comment on the issue naming what was delivered.
- **Return:** the PR URL, each command with its tail output, `git status --short`, and every departure from the
  plan.

## Rules

- **Never merge.** Adding a PR to the merge queue is merging. The owner may authorize merging for the current
  session only, in as many words, and the authorization never carries over.
- **Never create an issue without the owner's yes** (step 3).
  - Your own issue writes are rulings, labels, `**Blocked on:**` and `**Expires:**` lines, edit-downs, and
    expiry and verification closes.
  - A round that filed without that yes has failed; say so first in its report.
- **Never ask mid-build:** pull the item instead. Never build an item with an open question.
- **Never stack PRs.** Never hand over a PR that is not merge-ready.
- **Never relay a finding you did not reproduce,** except a security or tenancy filing candidate, which is marked
  `not reproduced`.
- **Never poll CI in the foreground,** and never from a lane or a review.
