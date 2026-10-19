---
description: Run one round of issue processing — settle the last round, propose items and the questions blocking others, build what is approved, report. Opens PRs and files what it finds; never merges.
---

# /process-top-issues

**Purpose:** bring the open-issue count down, keep `main` green, and keep each fix no larger than the defect needs.
The owner merges on your `## Merge-ready` comment without reading the diff, so every PR you hand over must have
been through the full review chain in step 4.

This file is self-contained. Memory notes point here and never restate it; where they disagree, this file wins.
**Changing this file:**
- you propose the edit as a PR, it goes through step 4 at `xhigh` with a design critique, and the owner merges it;
- an edit replaces or tightens an existing rule where it can, and the file stays under 380 lines.

**Roles.** The owner rules on questions, approves rounds, and merges. You (the owning agent) propose, plan, verify
and report. Lanes (subagents) each implement one plan you wrote, in their own worktree; at most three run at once.
Reviewers are independent agents: they never wrote or planned what they review.

`$ARGUMENTS`: empty = continue from wherever the round is; numbers = pin those issues into the next proposal;
`propose` = post a fresh proposal even if one is outstanding.

## State

- **Piles are labels:** `pile:ready` (known work), `pile:blocked` (waits on a ruling, an issue or a condition),
  `pile:yours` (only the owner can run it), `tracking` (in no pile: the Queue, trackers). Move with two edits, add
  first (`--add-label` then `--remove-label`); more than one pile label reads as blocked.
- **Every open issue outside `tracking` carries one priority label**, re-set by these definitions whenever the item
  moves between piles:
  - `P1`: a security, tenancy or data-loss exposure (latent included), or a defect a user or operator hits in
    normal use (a wrong result, a 5xx, lost work);
  - `P2`: any other defect reachable today;
  - `P3`: everything else: latent shapes, cleanup, docs, test infrastructure, enhancements. The owner may raise
    any item, and a ruled feature takes the priority its ruling states (ask if it states none).
  - A `P0` label, if the owner uses one, is read as `P1`, ranked first.
- **An issue closes only for a reason:**
  - it is fixed;
  - it duplicates another issue;
  - you verified its premise is dead;
  - an owner ruling closes it.

  Nothing closes an issue to lower the count: not age, not a timer, not silence.
- **A blocked item's body** has one line in one of these forms:
  - `**Blocked on:** #N …`;
  - `**Blocked on:** an owner ruling on …` (also used for a pull and a PR closed unmerged, with the question that
    settles it);
  - `**Blocked on:** condition — <a check you can run>` (a future event too, e.g. "redis-py ≥ 8 is pinned").
- **Authorship is read from headings**, because you post with the owner's account:
  - Your comments carry exactly one of these headings:
    - on the Queue (issue #1817, pinned, `tracking`): `## Round <N> — proposal`, `## Round <N> — result`,
      `## Note — <what>`;
    - on issues and PRs: `## Plan`, `## Review`, `## Delta`, `## Design critique`, `## Merge-ready`,
      `## Evidence`.
  - Any comment without one is the owner speaking.
  - An answer the owner gives in session is transcribed, quoting them, under `## Owner, in session — <date>`, and is
    read as theirs.

## 0. Locate the round

Find the newest proposal or result on the Queue:

| newest | then |
|---|---|
| none or a result | step 1 |
| a proposal with an owner comment after it | step 3 |
| a proposal, no owner comment, but a lane PR (branch `fix/<n>-<slug>`, *Lane rules*) for one of its items opened after it | approved out of band: post a `## Note`, then step 4 |
| a proposal, no owner comment, no lane PR | stop and say what it waits for; do not re-propose |

## 1. Settle the last round

1. **Each PR the last result named** (the main-red repair too):
   - **Merged:** its final `headRefOid` must be the head its `## Merge-ready` names. If it is not, run the
     vouched-head check (step 4.6) from the vouched head. Code after it gets an independent review now, and what
     that review finds is fixed in a new PR.
   - **Still open:** read its queue state:

     ```bash
     gh api graphql -f query='{repository(owner:"FaultMaven",name:"faultmaven"){pullRequest(number:<n>){
       isInMergeQueue timelineItems(last:5,itemTypes:[REMOVED_FROM_MERGE_QUEUE_EVENT]){
       nodes{... on RemovedFromMergeQueueEvent{createdAt reason}}}}}}'
     ```

     Then:
     - **Refresh** when any of these holds:
       - the vouched-head check finds code after the vouched head;
       - an owner comment follows the merge-ready (it is a fix list);
       - the PR conflicts with `main`;
       - it was removed from the queue (quote the `reason`);
       - a required check is red.

       To refresh: merge `main` in; have any change made (by a lane, or by you for a conflict); get an
       independent delta (step 4.5) when code changed; re-run CI; post merge-ready again.
     - **In the queue at its vouched head:** leave it alone.
     - **Pull it** if it cannot be refreshed (*To pull an item*, step 4, which closes the PR).

   If any is still open after that, queued or not, report it and stop.
2. **Each issue row**, taking the first that matches:
   - **closed:** nothing to do;
   - **open, PR merged:** close it if everything it asked for is delivered; otherwise edit it down to the
     remainder, title and body;
   - **`pulled`, or PR closed unmerged:** `pile:blocked` with `**Blocked on:** an owner ruling on …` naming what
     stopped it — unless an owner ruling recorded since then places it;
   - **`waiting on main` or `waiting on <service>`:** run its check again; if it still waits, report it and stop.

## 2. Propose

1. **Sort.**
   - Give every open issue that lacks a pile label or a priority label the missing one(s). Skip `tracking`.
   - Move to ready any blocked item whose blocking issue has closed.
   - Run each condition you can, and move items whose condition holds.
   - Close a tracker whose tracked work has all closed.
2. **Pick.**
   - From `pile:ready` only: `P1`, then `P2`, then `P3`, oldest first within each. Reserve one slot for the oldest
     `P2` that has waited over 30 days.
   - Items on one seam share a lane and a PR.
   - Size the round by review effort, not item count: at most 5 items and 3 lanes. An item touching a security
     boundary, storage or a migration is a round by itself.
3. **Check premises.** `git fetch origin main`, then confirm each picked item's code points still show the defect.
   An item whose premise looks dead is built as a **verification** item (step 4).
4. **Post the proposal**, then stop. Build nothing that has an open question.

   ```
   ## Round <N> — proposal
   ### Building
   | # | P | root (one line) | done when (observable behaviour) | size (product / test lines, est.) |
   ### Needs your call
   Every item blocked on an owner ruling: question · options · recommendation · what it unblocks.
   Every condition you could not check: what checking it needs; you run it, re-rule, or close the item.
   ### Priorities I set (change any)
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

If you cannot tell which, that is the next question. Unanswered questions stay where they are.

## 4. Build

Dispatch only approved items that the ready query returns with a single pile label. Skip any whose lane branch
already has an open or merged PR. The lane branch is the item's own, or its lead's when its plan says
`Shares lane with #<lead>`:

```bash
gh pr list -R FaultMaven/faultmaven --state all --limit 200 --search "created:>=<this proposal's timestamp>" \
  --json number,headRefName,state --jq '.[] | select(.state != "CLOSED") | select(.headRefName | test("/<n>-"))'
```

A skipped item resumes from the newest `## Merge-ready`, `## Delta` or `## Review` naming its PR's current head,
or from *Verify* when there is none. An item with a `## Plan — round <N>` but no PR resumes from that plan, in its
existing worktree. When the branch exists, add the worktree without `-b`.

1. **Plan** it yourself on `origin/main`, posted on the issue as `## Plan — round <N>`. Items sharing a seam name
   their lead as `Shares lane with #<lead>`. The plan covers:
   - **the root,** with its code points. The root is where the defect starts, however deep. When investigating
     shows the cause sits in a deeper layer than the issue names, that deeper cause is this item's root and this
     PR fixes it. Never fix a symptom and file its root. A root that needs a ruling or will not fit the size budget
     pulls the item with that question, whose options include "mitigate the symptom now, with the root as its own
     P1". Only the owner may choose that option;
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
     with the question;
   - **a design critique,** for a plan that does any of these:
     - changes a contract or a schema;
     - changes what a user sees;
     - makes a design choice a ruling left open;
     - is estimated over 300 product lines;
     - changes this file (always).

     That means one Fable critique before a lane is sent: the Agent tool with `model: "fable"`, read-only,
     briefed to challenge the plan rather than approve it. Post it as `## Design critique`. Record what you
     declined and why. A declined point about a decision reserved for the owner pulls the item with that question.

   For a **verification** item, run the reproduction yourself. If the defect is dead, close the issue, citing what
   fixed it and the evidence. If it is alive, plan it as above.
2. **Lane.** Send a fresh agent on your model. Use `sonnet` only for a docs-only change outside `.claude/` and
   `CLAUDE.md`. Its prompt holds the issue, the ruling, your plan, the done-when, and *Lane rules* below, verbatim.
   Run lanes in the background; plan the next item meanwhile.
3. **Verify.**
   - Bring the head up to `origin/main` first: merge it in, and send a conflict back to the lane. If `main` has
     moved since you planned, re-run the plan's consumer and N searches.
   - Re-run the lane's test command from its worktree, and confirm its head is the PR's head.
   - Read the whole diff against the plan. A hunk the plan does not cover goes back to the lane once.
4. **Adversarial review.** The reviewer is independent: `/code-review`, running in its own context. Brief it to
   break the PR, not to approve it: the invariant, the done-when, the riskiest ways the change could be wrong, and
   what must not change. Your own diff read in *Verify* is quality control, not this review. Start the CI watch in
   the background at the same time.
   - **Runtime PRs: `/code-review xhigh <pr>`.** These are PRs that change what runs in a deployment, a pipeline
     or an agent:
     - `faultmaven/`, `alembic/`, `resources/`, `requirements/`, `pyproject.toml`;
     - `Dockerfile`, `docker-compose*`;
     - `.github/workflows/`, `.github/scripts/`, `.importlinter`, `pytest.ini`, and `scripts/` that CI or CD runs;
     - `.claude/` and any `CLAUDE.md`.
   - **Every other PR: `/code-review high <pr>`.**
   - **A mixed PR** takes the higher level.
   - **A defeat pass** (a review briefed to get past the guard) only for a guard on a security or tenancy boundary
     that has live sites.

   Each finding is exactly one of:
   - **fixed:** it is part of this item. It breaks the invariant; or it is a crash, wrong result or regression the
     diff introduces on a reachable path; or it is a test or guard this PR ships that cannot fail; or it shares the
     item's root;
   - **filed:** it is true, valid and not part of this item (*Filing what you find*, below);
   - **declined:** one line in the PR body saying why. Either it is not true (not reproduced, and not shown at
     `path:line` on `origin/main` or the PR head), or it is not valid (as defined below), or an open issue already
     covers it (add the evidence there).

   Post the verdict on the PR as `## Review — <head>`, with the fix list and the issues filed. A review or delta is
   **clean** when no `fixed` landing is outstanding.
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

   - **A delta with any `fixed` finding** gets one more fix and delta. A second delta with any `fixed` finding pulls
     the item. So does passing twice the size budget.
   - **Any code commit after the reviewed head gets a delta before merge-ready.** That covers fixes, conflict
     resolutions and changes the owner asks for (step 1). A clean merge of `main` needs only CI.
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

   - **A flake:** re-run it once. In-progress checks read `PENDING` (sort by `startedAt`, because a running check's
     `completedAt` is `0001-01-01T00:00:00Z`).
   - **An outside-service outage:** wait it out (check the service's status page). If it outlasts the round,
     report the PR as `waiting on <service>`.
   - **A failure the merge base also has:** it is `main`'s. Repair it first, as its own PR on branch
     `fix/main-red-<short sha>` (worktree `.claude/worktrees/main-red-<short sha>`), built and reviewed like any
     item. The PRs it blocks are reported as `waiting on main`.

   **The vouched-head check.** The head you vouch for must be the last reviewed head (the head named in the last
   `## Review` or `## Delta`), plus only clean merges of `main` and commits that change no file. Only the word
   `clean` passes; anything else is a code change, and gets a delta first:

   ```bash
   git fetch -q origin main "pull/<n>/head"
   git merge-base --is-ancestor <reviewed> <head> || { echo "code: <reviewed> is not an ancestor of <head>"; exit 1; }
   for c in $(git rev-list --first-parent <reviewed>..<head>); do
     set -- $(git rev-list --parents -n1 "$c")
     if [ $# -eq 2 ]; then [ "$(git rev-parse "$c^{tree}")" = "$(git rev-parse "$2^{tree}")" ] || { echo "code: $c"; exit 1; }; continue; fi
     [ $# -eq 3 ] && git merge-base --is-ancestor "$3" origin/main \
       && t=$(git merge-tree --write-tree "$2" "$3") && [ "$(printf '%s\n' "$t" | head -1)" = "$(git rev-parse "$c^{tree}")" ] \
       || { echo "code: $c"; exit 1; }
   done
   echo clean
   ```

   A PR is merge-ready only when all of these hold on its final head:
   - every lane on it has ended, so nothing pushes after you vouch;
   - the adversarial review and every delta are clean, with every finding landed as fixed, filed or declined;
   - the vouched-head check prints `clean`;
   - every required check is green;
   - you have re-run the verification.

   Then comment `## Merge-ready — <head>` with:
   - the base and the plan link;
   - the design critique, if the plan had one, and the points you declined;
   - the review (its level) and each delta (its range and its reviewer);
   - the findings fixed, filed and declined;
   - the commands you re-ran, with their tail output;
   - the checks.

To **pull** an item:
1. stop its lane;
2. close its PR, with a comment saying why. On a PR shared with other items, have the lane take the item's part
   out instead, and remove its `Closes`/`Refs` line from the PR body (`gh api -X PATCH`, read back). Then check
   that nothing else can close it: `git log --format=%B origin/main..<head>` and the PR title must not name
   `#<pulled>`. A commit that names it pulls the whole PR;
3. move it to `pile:blocked` with `**Blocked on:** an owner ruling on …`, stating what stopped it and the options.
   For size or a deep root, the options are: build at the new estimate, mitigate now with the root as its own P1,
   or drop;
4. carry on with the others.

**Filing what you find.** Whether a finding turns up while you investigate, plan, build or review:
- **File it** when it is:
  - **true:** reproduced, or shown at `path:line` on `origin/main`;
  - **valid:** any one of these:
    - it would be `P1` or `P2` by *State*;
    - it is a `P3` with a named trigger, written as a `condition —` line: the dependency bump, the client or the
      configuration that would make it reachable;
    - it hides a failure, such as a test that never runs or a check that cannot fail.

    A shape nothing can reach, which no named event would make reachable, is not valid;
  - **not part of the item:** a different root.

  File it as you find it, with `Found while working on #<n>`, the evidence, a pile and a priority.
- **Search open and closed issues first.** If an open issue already covers it, add the evidence there under
  `## Evidence`. If a closed issue matches, cite it in the new issue. Findings that share one root go in one issue.
- **Never file the item's own root, a symptom of it, or a part of it.** That is fixed in the item's PR (*Plan*).
- **You need not build what you file this round.** A filed issue joins the piles and is taken by priority in a
  later round or session.

## 5. Report

Comment on the Queue:

```
## Round <N> — result
| # | outcome (merge-ready · closed by verification · pulled · waiting on main · waiting on <service>) | PR | product +/− | tests +/− |
Closed <x> · Filed <y> · Net lines: product <p>, tests <t> · Open <before> → <after>
Filed: #<n> (P<k>) — <what, found while working on #<m>> — one line each
Waiting on you: queue each PR at its vouched head: gh pr merge <n> --match-head-commit <sha>
```

Every report states those four numbers and lists every issue filed. A main-red repair gets a row of its own. Then
stop: the round ends when the owner merges.

## Lane rules (sent verbatim)

- **Implement the plan; do not re-plan.** Where the code disagrees with the plan, stop and report what you found.
- **Worktree:** `git fetch origin main`, then
  `git worktree add -b fix/<n>-<slug> /home/swhouse/product/faultmaven/.claude/worktrees/<n> origin/main`. If the
  branch already exists, add the worktree without `-b`.
  Never work in the main checkout. Never delete `./data` there: it is the live stack's database.
- **Before pushing,** run `black --check faultmaven/ tests/`, `ruff check faultmaven/ tests/`, `lint-imports`, and
  the tests that cover the change, plus `python scripts/check_contract_version.py` if `docs/reference/api/`
  moved. Not the whole suite: CI runs it.
- **Tests** reproduce the defect and assert behaviour. No wall-clock thresholds, and no repo-scanning guard unless
  the plan names one. Keep within the plan's size budget, and say so if you cannot.
- **Closing keywords go in the PR body only**, never in the title or a commit message. The body says `Closes #<n>`
  only when the issue as written is delivered. Otherwise it says `Refs #<n>`, and you comment on the issue naming
  what was delivered.
- **Return:** the PR URL, each command with its tail output, `git status --short`, every departure from the
  plan, and anything outside the plan you found, with its evidence. You do not file it; the owning agent does.

## Rules

- **Never merge.** Adding a PR to the merge queue is merging. The owner may authorize merging for the current
  session only, in as many words, and the authorization never carries over.
- **File what is true and valid and not part of the item. Never fix a symptom and file its root.**
- **Issue and PR writes besides filing:** plans, evidence, rulings, labels, `**Blocked on:**` lines, edit-downs, and
  the closes *State* allows (fixed, duplicate, dead premise, owner ruling). Each comment carries its heading.
- **Never ask mid-build:** pull the item instead. Never build an item with an open question.
- **Never stack PRs.** Never hand over a PR that is not merge-ready.
- **Never review your own work:** the review, every delta and every design critique come from an agent that did not
  write or plan what it reads.
- **Never file or relay a finding you have not verified:** reproduced, or shown at `path:line` on `origin/main`.
- **Never poll CI in the foreground,** and never from a lane or a review.
