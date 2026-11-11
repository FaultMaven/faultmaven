---
description: Run one round of issue processing — settle the last round, propose items and the questions blocking others, build what is approved, report. Opens PRs and files what it finds; never merges.
---

# /process-top-issues

**Purpose:** make the system healthier and more robust round after round by resolving what matters most at its root,
not by treating its symptoms alone (only an owner ruling leaves a root unfixed), so that the backlog shrinks over
rounds instead of growing; keep `main` green, and keep each fix no larger than the defect needs. Nothing is hidden or
closed to lower the count. The owner merges on your `## Merge-ready` comment without reading the diff, so every PR you
hand over must have been through the full review chain in step 4. When a rule here works against that purpose, say so
under *Needs your call*; a change the owner rules goes through *Changing this file*.

This file defines the rules for processing backlog issues, and is self-contained. Memory notes point here and never
restate it; where they disagree, this file wins.
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
    normal use (a wrong result, a 5xx, lost work), or an issue filed as a `main red` (by 4.6 or step 2's alert sweep).
    A `P0` label, if the owner uses one, is read as `P1`, ranked first;
  - `P2`: any other defect reachable today;
  - `P3`: everything else: latent shapes, cleanup, docs, test infrastructure, enhancements. The owner may raise
    any item, and a ruled feature takes the priority its ruling states (ask if it states none).
- **Every open issue outside `tracking`, `root-issue` and `root-candidate` carries one class label,** the only writer
  of its class. Design statements are `docs/architecture/**`, ADRs and `INV-*` rows; `CLAUDE.md`, `.claude/rules`,
  docstrings, comments and tests are not. Recurrence only says where to look:
  - `class:defect`, the default: the code breaks what the design states, or fails in normal use where no design
    intends it to. A defect filed now cites, at its `path:line`, a statement that determines the behaviour there (a Key
    Rules row, an `INV-*` row or a section-level sentence, never a principle's headline alone), or the observable
    failure (a crash, a 5xx, a wrong result), or, for a doc stale against correct code, that code;
  - `class:chore`: hygiene, test infrastructure, docs drift or an enhancement;
  - `class:design`, only by an owner ruling on a root: the issues on its `**Blocks:**` line and its stages.
- **Roots.** A `root-candidate` is your question that a seam has a design flaw: in `pile:blocked` with
  `**Blocked on:** an owner ruling on its design change`, it carries the seam's `## Evidence — design check` (*Pick*).
  Only a `## Ruling` makes it a `root-issue` (relabel, add first) and sets its `**Blocks:**` line; roots merge or
  split only by ruling. A stage carries `design-stage`, `class:design`, its root's priority and the body line
  `**Stage of:** #<root>`.
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

A PR in another FaultMaven repo names the item qualified; these are its candidates too:

```bash
gh search prs --owner FaultMaven --match body "FaultMaven/faultmaven#<n>" --limit 100 --json url,body | jq -r --arg n <n> '.[]
  | select(.body | test("(?i)\\b(close[sd]?|fix(e[sd])?|resolve[sd]?|refs):?[ \\t]+(FaultMaven/faultmaven#|https://github\\.com/FaultMaven/faultmaven/issues/)"
    + $n + "\\b")) | .url' | while read -r u; do gh pr view "$u" --json url,state,headRefName,createdAt; done
```

| # | the records show | next |
|---|---|---|
| 1 | its PR merged | at step 1: its merged head must be the head its newest `## Merge-ready` names, with no `## Pulled` after it; else file `PR #<p> merged at an unvouched head` (search first), naming `git log --oneline R..<merged head>`, or the whole PR when no `R` exists, in `pile:blocked` on an owner ruling on how to review them. At step 4, an open item whose PR merged since this round's proposal waits for step 1's edit-down and a new proposal; one whose PR merged before it reads on from row 11 |
| 2 | the item, or an item its PR closes, in `pile:blocked` with a `**Blocked on:** an owner ruling on …` line or with no `**Blocked on:**` line; and either that PR open, or a plan of the round being settled with no pull newer than it | that item's pull is unfinished: finish *To pull an item* for it from its first step not done |
| 3 | the item in `pile:blocked` | it waits on its blocker |
| 4 | its PR open, its newest `## Review` or `## Delta` clean or its findings waived by a live ruling, and a required check red because `main` is (a `main red` issue, step 4.6) or a service it needs is down; or a stage's PR with an `evidence` finding questioning the stage's own design and no live ruling since | waiting: leave it until that issue closes or the check passes when re-run (`gh run rerun <run-id> --failed`); then merge `main` in and go on at row 6 or 9. On the design finding: waiting on an owner ruling on its design, asked under *Needs your call*; never merge-ready until a live ruling sends it on (row 8 or 9) or rebuilds it (row 13) |
| 5 | its PR in the merge queue at a merge-ready's `H`, with none of row 6's triggers (`gh api graphql -f query='{repository(owner:"FaultMaven",name:"faultmaven"){pullRequest(number:<p>){isInMergeQueue timelineItems(last:5,itemTypes:[REMOVED_FROM_MERGE_QUEUE_EVENT]){nodes{... on RemovedFromMergeQueueEvent{createdAt reason}}}}}}'`) | leave it |
| 6 | its PR open after a merge-ready, and code after the merge-ready's head (the vouched-head check), an owner comment after the merge-ready, a conflict with `main`, a queue removal dated after it with the PR not back in the queue (row 5's query; quote its `reason`), or a red required check | refresh: merge `main` in, the change made (by the lane, by you for a conflict), a delta of what changed (step 4.5), CI, merge-ready again; a refresh with a `## Delta` after the merge-ready goes on at row 8 or 9, unless an owner comment, a conflict or a red check is newer than that delta. A `fixed` finding in that delta gets one fix commit and one delta; a `fixed` finding in that one pulls the item. A red check the merge base also has makes it row 4, never a pull. A queue removal is not a code change when its `reason` is `failed_checks`; every failed test in the merge-group run is the test an open flake issue names, with the same assertion; that issue records an occurrence on a run without this PR's commits; and a re-run of that required check on `H` after the removal is green. Re-queuing past it is re-running past the flake, so 4.6's flake rule applies. When the flake's code is on this PR's seam, its fix in this PR is this row's refresh, not a re-queue. Otherwise, met while settling (step 1), the flake's fix is the first row of the next proposal's *Building*, outside its items. Then post merge-ready again at the same head, quoting the run and the `reason`; the owner re-queues, or you do under a merge authority transcribed that session (*Rules*). Once per PR: a second such removal waits until the flake fix's PR merges. Anything else is this row's refresh |
| 7 | its PR open after a merge-ready at `H` | merge-ready: carried under *Yours to run*; while an item it closes is in `pile:blocked` (other than one a stage's PR closes whose line is `**Blocked on:** #<root>` for that stage's root), it is not merge-ready and waits on that item's blocker |
| 8 | its PR open; its newest `## Review` or `## Delta` has a `fixed` finding neither landed nor waived by a live ruling | a pull trigger met since its newest live ruling, or ever when it has none, pulls it: the cap or size (step 4.5), or a `fixed` finding in a re-entry delta (row 12) or in a refresh's second delta (row 6), or a stage's PR not converging (step 4). Else the fix commit, then a delta of the commits since `R` (row 12's, when its live ruling grants a re-entry) |
| 9 | its PR open; its newest `## Review` or `## Delta` is clean, or its findings are waived by a live ruling | the vouched-head check from `R` (step 4.6); code after `R` gets a delta first; then merge-ready, or row 7's wait while an item it closes is in `pile:blocked` |
| 10 | its PR open, no review | *Verify*, then the review (steps 4.3–4.4) |
| 11 | a `## Plan — round <N>` newer than any pull, live ruling and PR of the item, and no open PR | resume a lane on that plan, in the branch and worktree it names |
| 12 | a pull that closed its PR, which has not merged since, and a live ruling granting one more fix and delta, an exception commit or a merge as is | re-entry: `gh pr reopen` it. A merge as is waives its outstanding findings, each filed as an issue: row 9. Else the lane adds one commit on its branch, and a delta of only that commit, outside the cap, where a `fixed` finding pulls it again |
| 13 | a live ruling implying other work (a re-plan, a build at a new estimate) | rebuild: a plan for this round (step 4.1, with a critique when 4.1 calls for one) on a new branch, with its own review and PR; a closed PR stays closed |
| 14 | none of the above | new: step 4.1 |

## 0. Locate the round

Find the newest proposal or result across the two newest `Queue` issues, which this prints oldest first, so a rotation
loses no round; the last it prints, the open one, is the Queue you post to:
`gh issue list -R FaultMaven/faultmaven --label tracking --state all --search 'Queue in:title' --limit 100 --json number,title --jq '[.[] | select(.title == "Queue") | .number] | sort | .[-2:][]'`

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

**The health block** runs first, and again before posting with `lanes` set; the proposal opens with its output,
verbatim. A stop, fixed before posting: `unclassified` not below `was` (until it is 0), or any `multi`; a design check
due while fewer than two ran since the last proposal; when the round dispatches, fewer design lanes picked than offered
(less any stage on an open PR's seam, or in a migration round), or two from one root; a finding its root neither lists
nor names; `slot: round unknown`. While `in flight` is past either limit (five open PRs by this account in `faultmaven`,
or naming a core issue; three pulled items awaiting a ruling), nothing new but a flake's fix (4.6) is dispatched: the
round finishes, settles or asks instead. Labels not yet in use read as empty. The output names a PR only in a public
repo:

```bash
R=FaultMaven/faultmaven; lanes="<the lead issue of each lane you pick>"; now=$(date -u +%s)
ago() { date -u -d "@$((now - $1 * 86400))" +%FT%TZ; }; lines() { gh api "repos/$R/issues/$1/comments" --paginate --jq '.[] | "\(.created_at) \(.body | split("\n")[0])"'; }
# the newest two Queues, oldest first, so a rotated Queue still reads the round, the last proposal and the trend
q=$(gh issue list -R $R --label tracking --state all --search 'Queue in:title' --limit 100 --json number,title \
  --jq '[.[] | select(.title == "Queue") | .number] | sort | .[-2:][]')
qc=$(for n in $q; do gh api "repos/$R/issues/$n/comments" --paginate --jq '[.[] | select(.body | test("^## Round [0-9]+ — (proposal|result)"))
  | {t: .created_at, p: (.body | test("^## Round [0-9]+ — proposal")), b: .body}]' || echo false; done | jq -s add)
since=$(jq -r 'map(select(.p)) | last.t' <<<"$qc"); r2=$(jq -r 'map(select(.p | not)) | .[-2].t // "none"' <<<"$qc")
open=$(gh issue list -R $R --state open --limit 1000 --json number,labels,body,createdAt)
pub=$(gh repo list FaultMaven --visibility public --limit 200 --json name --jq 'map(.name)')
cls='def has($l): any(.labels[]; .name | IN($l[])); def free: has(["tracking", "root-issue", "root-candidate"]) | not;
  def cls: [.labels[].name | select(startswith("class:"))] | if length > 1 then "multi" else first // "none" end;
  def ready: [.labels[].name | select(startswith("pile:"))] == ["pile:ready"]; def kw: "(?i)\\b(close[sd]?|fix(e[sd])?|resolve[sd]?|refs):?[ \\t]+";
  def by($u): map(cls | ltrimstr("class:") | sub("^none$"; $u)) | group_by(.) | map("\(.[0]) \(length)") | join(" · ");
  def core($c): "(" + (if $c then "(FaultMaven/faultmaven)?#" else "FaultMaven/faultmaven#" end) + "|https://github\\.com/FaultMaven/faultmaven/issues/)";
  def refs($r): any($prs[]; . as $x | .body | test(kw + core($x.c) + "\($r)\\b"));
  def stage: (.body | capture("\\*\\*Stage of:\\*\\* (FaultMaven/faultmaven)?#(?<r>[0-9]+)").r) // "?";'
# open PRs by this account in faultmaven, or naming a core issue (the PRs this procedure opened)
prs=$(gh search prs --owner FaultMaven --state open --author @me --limit 200 --json repository,number,createdAt,body \
  | jq "$cls"'[.[] | . + {c: (.repository.name == "faultmaven")} | select(.c or (.body | test(kw + core(false) + "[0-9]")))]')
was=$(jq -r 'map(select(.p) | .b | [scan("(?m)^open by class: [^\n]*")] | last | select(.) | ([scan("unclassified ([0-9]+)")[0]] | first // "0")) | last // "none"' <<<"$qc")
jq -r --arg was "$was" "$cls"'[.[] | select(free)] | "open by class: \(by("unclassified")) (unclassified was \($was))"' <<<"$open"
due=; ran=0; for n in $(jq -r "$cls"'.[] | select(has(["root-candidate"])) | .number' <<<"$open"); do
  c=$(lines "$n" | grep ' ## Evidence — design check') || { due+="#$n "; continue; }
  [ "$(cut -d' ' -f1 <<<"$c" | sort | tail -1)" \> "$since" ] && ran=$((ran + 1)); done
echo "design checks due, not run: ${due:-none} · run since the last proposal: $ran"
u=$(for n in $(jq -r "$cls"'.[] | select(has(["pile:blocked"]) and (.body | test("\\*\\*Blocked on:\\*\\* an owner ruling"))) | .number' <<<"$open"); do
  l=$(lines "$n"); grep -q ' ## Pulled' <<<"$l" && printf '#%s ' "$n"; done); np=$(wc -w <<<"$u"); na=$(jq length <<<"$prs")
jq -r --argjson prs "$prs" --arg lanes "$lanes" --argjson lim "$([ "$na" -gt 5 ] || [ "$np" -gt 3 ] && echo true || echo false)" "$cls"'
  [.[] | select(has(["design-stage"]) and ready) | {n: .number, r: stage} | select((refs(.n) or (.r != "?" and refs(.r))) | not)] as $a
  | [$a[] | select(.n | tostring | IN($lanes | scan("[0-9]+")))] as $p | (if $lim then 0 else [($a | map(.r) | unique | length), 2] | min end) as $k
  | "design lanes: picked \($p | length) of \($k) offered, one per root: \(if ($p | map(.r) | unique | length) == ($p | length) then "yes" else "no" end)"
  + " (actionable: \($a | map("#\(.n) of #\(.r)") | join(", ") | if . == "" then "none" end))"' <<<"$open"
# the round this proposal opens: one past the newest result, else unknown (a stop); every third is a chore batch (Pick)
rn=$(jq '[.[]? | select(.p | not) | .b | capture("^## Round (?<n>[0-9]+)").n | tonumber] | max // empty | . + 1' <<<"$qc" 2>/dev/null)
if [ -z "$rn" ]; then echo "slot: round unknown"; else b=; [ $((rn % 3)) -eq 0 ] && b="chore batch (round $rn); ordinary: "
jq -r --arg b "$b" --argjson prs "$prs" --arg cut "$(ago 30)" "$cls"'[.[] | select(free and ready and (refs(.number) | not)
  and (has(["class:chore", "class:design", "design-stage"]) | not))] | [.[] | select(has(["P0", "P1"]))] as $p1
  | ($p1 | sort_by((has(["P0"]) | not), .createdAt)) + ([.[] | select(has(["P2"]) and .createdAt < $cut)] | sort_by(.createdAt))
  | map("#\(.number)") | "slot: \($b)\(first // "none, the ordinary order")\(if length > 1 then " (then \(.[1:3] | join(" ")))" else "" end)"' <<<"$open"; fi
roots=$(jq "$cls"'[.[] | select(has(["root-issue", "root-candidate"])) | {n: .number, b: [.body | scan("(?m)^\\*\\*Blocks:\\*\\*.*") | gsub("\\([^)]*\\)"; "") | scan("#([0-9]+)")[0] | tonumber]}]' <<<"$open")
off=$(gh issue list -R $R --state all --search "created:>=$since \"Found while working on\" in:body" --limit 300 --json number,body \
  --jq '.[] | "\(.number) \(.body | [scan("Found while working on #([0-9]+)")[0]] | first // "")"' | while read -r f m; do
  [ -n "$m" ] || continue; up=$(gh api "repos/$R/issues/$m" --jq .body | grep -oP '\*\*(Blocked on|Stage of):\*\* (FaultMaven/faultmaven)?#\K[0-9]+' | paste -sd,)
  for r in $(jq -r --argjson f "$f" --argjson m "$m" --argjson up "[$up]" '.[] | select(.n == $m or (.b | index($m)) or (.n | IN($up[])))
    | select(.b | index($f) | not) | .n' <<<"$roots"); do
    t=$(jq -r --argjson r "$r" '.[] | select(.number == $r) | .body' <<<"$open"; gh api "repos/$R/issues/$r/comments" --paginate --jq '.[].body')
    grep -qi "filed as #$f\b" <<<"$t" || printf '#%s (root #%s) ' "$f" "$r"
  done; done); echo "findings on an open root's seam it neither lists nor names: ${off:-none}"
o=$(gh issue list -R $R --state all --search "created:>=$(ago 14 | cut -c1-10)" --limit 1000 --json labels)
c=$(gh issue list -R $R --state closed --search "closed:>=$(ago 14 | cut -c1-10)" --limit 1000 --json labels)
jq -rn --argjson o "$o" --argjson c "$c" "$cls"'"14 days: opened \($o | length) (\($o | by("unlabelled"))) · closed \($c | length) (\($c | by("unlabelled")))
  · ratio \(if ($c | length) > 0 then ($o | length) / ($c | length) * 100 | round / 100 else "n/a" end)"' | tr -s '\n ' ' '; echo
jq -r 'map(select(.p) | .b) | "ratio at the last proposal: \([last | scan("ratio ([0-9.]+|n/a)")[0]] | last // "none") · open at the last four: \(
  map([scan("(?m)^open ([0-9]+) \\(")[0]] | last) | map(select(.)) | .[-4:] | join(" → "))"' <<<"$qc"
jq -r "$cls"'def med: sort | if length == 0 then "none" else .[length / 2 | floor] end; [.[] | select(free)
  | {p: ([.labels[].name | select(IN("P0", "P1", "P2"))] | first // "" | sub("P0"; "P1")), d: ((now - (.createdAt | fromdate)) / 86400 | floor)}]
  | "median age in days: P1 \(map(select(.p == "P1") | .d) | med) · P2 \(map(select(.p == "P2") | .d) | med)"' <<<"$open"
jq -r --argjson prs "$prs" --arg due "$due" "$cls"'"roots: candidates \(map(select(has(["root-candidate"]))) | length) (check due \($due
  | [scan("[0-9]+")] | length)) · ruled \([.[] | select(has(["root-issue"])) | .number] | "\(length) (stage PR open \(map(select(refs(.))) | length))")"' <<<"$open"
echo "in flight: open PRs by this account in faultmaven, or naming a core issue, $na of 5 · pulled, awaiting a ruling $np of 3 (${u:-none})"
jq -r --arg t "$r2" --argjson pub "$pub" '[.[] | select($t != "none" and .createdAt < $t)] | (map(select(.repository.name | IN($pub[])) | "\(.repository.name)#\(.number)"))
  as $n | "open across two results: \($n | join(" ") | if . == "" then "none" end)\(if length > ($n | length) then " + \(length - ($n | length)) private" else "" end)"' <<<"$prs"
mp=$(gh pr list -R $R --state merged --search "merged:>=$since" --limit 300 --json body)
jq -r --argjson mp "$mp" "$cls"'[$mp[].body | scan("(?i)\\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?):?[ \\t]+#([0-9]+)")[0] | tonumber] as $m
  | [.[] | select(free and (.number | IN($m[]))) | "#\(.number)"] | "closable, closed by a PR merged since the last proposal: \(join(" ") | if . == "" then "none" end)"' <<<"$open"
echo "since the last proposal: merged $(jq length <<<"$mp") · PRs opened $(gh pr list -R $R --state all --search "created:>=$since" --limit 300 --json number --jq length)"
```

1. **Sort.**
   - Give every open issue that lacks a pile, priority or class label the missing one(s) (*State*), oldest first, so
     the `unclassified` count falls each round. Skip `tracking`, `root-issue` and `root-candidate` for the class. The
     default is a label, not a verdict: a chore is relabelled when the item is next read.
   - **The transition,** in the first round under this text: create the labels `class:defect`, `class:chore`,
     `class:design`, `root-candidate` and `design-stage`; give each ruled root's `**Blocks:**` issues `class:design`,
     and each of its open stages `design-stage`, `class:design`, the root's priority and a `**Stage of:** #<root>`
     line; and give every other open issue outside `tracking`, `root-issue` and `root-candidate` its class
     (`class:defect` where nothing says chore), so `unclassified` is 0 from the second round. A ruled root's existing
     code stages are exempt from the docs stage coming first, and take their pins at their next ruling.
   - Move to ready any blocked item whose blocking issue has closed or whose condition holds (run each you can).
   - **The closable sweep:** close each issue the `closable` line lists whose PR delivered it all (else edit it down
     to the remainder), each duplicate, each dead premise (reproduced as in step 4.2), and each tracker whose tracked
     work has all closed; never by age. The count goes on the scorecard.
   - Run `.venv/bin/python scripts/security_alert_sweep.py`. Put each `untracked` group's alert URLs on a body line
     starting `**Alerts:**` in the open core issue covering it, or file one per root opening `**Repo: FaultMaven/<r>.**`
     then that line, verified by the alert page and manifest. Anything written about a private repo's alert names only
     its URL, kind and count. Raise secret-scanning alerts with the owner in session, never in writing. Report the
     non-secret `unknown`/`unavailable` lines in the proposal. A failing `scheduled audit:` line is placed as well:
     read its newest scheduled run. An advisory against `main` is that repo's `main red`: added to the `**Alerts:**`
     line of the open issue covering it, else filed as 4.6 files one, opening `**Repo: FaultMaven/<r>.**` outside
     core. Any other failing line, and a stopped or missing schedule, is reported in the proposal with what it needs.
     Dismiss nothing without the owner's ruling on the issue.
2. **Pick.**
   - **The design pass** gives one row per open `root-issue` and `root-candidate`: its design statement, level,
     principle, design change, open count (its `**Blocks:**` issues and stages) and state (check due, ruling due,
     pins due, building, waiting). Plus a **look row** per seam worked since the last proposal (a merged or open PR in
     that window) whose review filed a finding or whose open count rose: its **chain** (the defects fixed and still
     open on it, by number) and open count say only where to look, and its only output is a design check, never a
     per-defect pick.
   - **A design check** quotes the design statement (*State*) that is silent or wrong on a decision the system needs
     (ownership, source of truth, data lifecycle, turn order, execution bounds, failure semantics, a trust boundary),
     so that code built exactly to it would fail observably for a user or operator; a detail any reasonable
     implementation of the stated design satisfies is not a gap. It names the **level** (the system: a rule every part
     must keep; or one part's design), the **principle** in `architectural-design-principles.md` it falls under or the
     one that is missing (two parts' gaps under one missing rule are one system-level flaw), every part of the system
     the concern touches, and the **design change**: the issues it absorbs, and its proposed stages, each with its
     budget, the issues it delivers and its **pins** (what its PR is verified against), so a ruling can adopt them in
     one word. The change is critiqued first under `## Design critique`. Run at most two a round, one per seam, those
     the owner ordered first, before the proposal. A flaw is posted as `## Evidence — design check` on the seam's
     `root-candidate` (filed, search first, if none is open), a *Needs your call* question; a candidate under a
     principle another root covers is proposed for merge with it. "None" is posted on an open candidate as
     `## Evidence — design check: none`, recommending a ruling of no design flaw, else in the look row; either way
     citing the statement each defect breaks.
   - **Rulings carry the state.** A ruling that orders a design check files its seam's candidate, the check due. A
     ruling that freezes a seam, or rules its design change, makes its candidate a `root-issue` and lists exactly the
     issues it blocks on the `**Blocks:**` line; they take `class:design` and get no per-defect fixes, because its
     stages deliver them. Each listed issue that no open PR carries and that is not already in `pile:blocked` moves
     there with `**Blocked on:** #<root>`; one an open PR carries stays with that PR; one already blocked keeps its
     line. When a listed issue's own blocker clears while the root issue is open, by Sort or by a ruling, its line
     becomes `**Blocked on:** #<root>`. While the root issue is open, no issue on its `**Blocks:**` line gets a new plan
     beyond the stages a ruling sets, and an open PR on one finishes by its row in *Where an item stands*. A ruling on
     the design change lists its stages, each filed as a `design-stage` issue with its budget, the issues it delivers
     and its pins. The first stage is the `docs/architecture/` amendment (the principle text and its enforcement row),
     merged before any code stage. A stage ruled without pins is filed in `pile:blocked` with `**Blocked on:** an
     owner ruling on its pins`. The root issue then stays in `pile:blocked` with `**Blocked on:** condition — its last
     stage's PR has merged`. A stage's PR closes only the issues it delivers (another it delivers is a change to the
     list) and says `Refs #<root>`; when the last stage's PR merges, close the root issue under `## Closed`, citing
     the stages and filing each `## Evidence` finding no stage delivered as an issue, or declining it with the
     reason. That list places them, as step 3 places any ruled issue. A ruling of no design flaw, or one that
     declines the design change, closes the root or candidate and lists where its issues go; its `## Closed` comment
     files each `## Evidence` finding on it as an issue, or declines it with the reason. Every change to a root issue's
     `**Blocks:**` line or a stage's list is a ruling, except a finding added under *Filing what you find*.
   - From `pile:ready` only, in three lanes (the health block's `design lanes` and `slot` lines):
     - **two design lanes:** the next stage, in the order the owner last approved, of each root with no stage PR open,
       at most one per root; a design lane no stage can take goes to the ordinary order;
     - **the third lane, the slot:** the oldest `P1` (*State*: a wrong result or a 5xx in normal use included), else
       the oldest `P2` waiting over 30 days, else the ordinary order; never a chore or a batch (*Filing what you
       find*), except in a round whose number is divisible by 3: the slot then takes one chore batch, the seam with
       the most ready `class:chore` issues, oldest on a tie, within the round's size limits below, planned and
       reviewed as one item and counted as one. Its seam is the component the chores' fixes touch, named in the plan.
       In the batch's round the slot does not take an open `P1`, and only the slot waits: the **Next** items follow
       the ordinary order, as a design lane no stage can take does. In a migration round, or when no chore seam is
       free (none ready, or each on an open PR's seam), the slot is the ordinary one, and the proposal says so in one
       line under *Health*. The `slot` line lists the next ones for when the first sits on an open PR's seam; in a
       batch round it reads `chore batch (round <N>); ordinary:` before them, for the fallback;
     - **the ordinary order:** `P1`, `P2`, `P3`, oldest first within each, a chore after the defects of its priority.

     Skip an item that an open PR from an earlier round still carries (its PR, *Where an item stands*), and any item
     on that PR's seam until it merges.
   - Items on one seam share a lane and a PR.
   - Size the round by review effort: at most 5 items and 3 lanes, plus up to 3 **Next** items the approval also covers,
     none a migration; a flake's fix (4.6) takes a lane but is not an item. An item touching a security boundary or
     storage takes its own lane and PR; a migration is alone in its round, with no Next items. The proposal states the
     round's expected agent count (lanes, reviews, deltas, critiques), at most about 12; at most three reviews or
     critiques run at once. A usage-limit stop is reported in the result, and the next round is sized down.
3. **Check premises.** `git fetch origin main`, then confirm each picked item's code points still show the defect.
   An item whose premise looks dead is built as a **verification** item (step 4).
4. **Post the proposal**, then stop. Build nothing that has an open question.

   ```
   ## Round <N> — proposal
   ### Health (the health block's output, verbatim)
   ### Design pass (per root: statement · level · principle · design change · open count · state; look rows: chain · counts · check)
   ### Building
   | # | P | class | root (one line) | done when (observable behaviour) | size (product / test lines, est.) |
   ### Next (same columns; each starts when a lane's PR is merge-ready or pulled, or its item closes, after any flake's fix)
   ### Needs your call
   At most five, ranked: question · options · recommendation · what it unblocks. A backlog-health question (its
   diagnosis, what to batch or close by ruling, what to defer) is first when this and the last proposal's ratio are
   both above 1.2; then any unanswered for two rounds; then, while the `pulled` count is past its limit, the oldest
   pulls, as many as bring it down to the limit; then design rulings due. Other items awaiting a ruling: numbers.
   The diagnosis, read from the look rows' chains and the `14 days` line, names the source of the rise: in-scope
   findings filed instead of fixed in the item's PR (fix them with their parent); same-root findings filed apart
   (consolidate them, as evidence or one issue); a component the findings cluster in (a systemic fix); new exposure
   the reviews found (they stay filed). A true, distinct finding is never left unfiled to lower the count.
   Every condition you could not check: what checking it needs; you run it, re-rule, or close the item.
   ### Open across two results (each PR with its blocker; a private repo's by count only)
   ### Priorities I set (change any)
   ### Yours to run
   ### Settled from last round
   ### Scorecard
   open <n> (P1 <a> · P2 <b> · P3 <c> · blocked <d>) · alerts: <the sweep's `queue:` line>, filed <f> · last round: merged <m> per <l> lanes, closed <x> (sweep <k>), filed <y> (design evidence <g>, defects <h>, chores <j>)
   ```

   The health lines carry the rest of the scorecard. The 14-day ratio and the scorecard's `open` count are indicators
   of the purpose, read over rounds, never a round's target. They trigger only the backlog-health question above, the
   batching in *Filing what you find*, and the result's diagnosis when Open rose (step 5).

## 3. Take the answers

Record each ruling on its own issue under `## Ruling`, then place the issue: ready when it implies work (the ruling is
its spec, with its priority), blocked with a `condition —` line when it defers, closed, quoting it, when it implies
none, and yours when only the owner can run it. If you cannot tell where a ruling places the issue, that is the next
question; unanswered questions stay where they are.

## 4. Build

Dispatch only approved items that the ready query returns with a single pile label, and, when a lane's PR is merge-ready
or pulled or its item closes, a flake's fix (4.6) first, then the first **Next** item not on any open PR's seam (a
refresh waits for a free slot), and nothing new but a flake's fix while the health block's `in flight` line is past a
limit. Each starts at its row in
*Where an item stands*; one whose plan (newer than any pull) says `Shares lane with #<lead>`, at its lead's. Each lane
aims to end the round merged or merge-ready. A design stage's PR open at two consecutive results, neither merged nor
merge-ready at either and not waiting at row 4 at the second, has not converged: it is pulled with a re-plan into
smaller sub-stages (4.1) recommended, never given more fix rounds.

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
   - **the tests:** a regression test that reproduces each defect and asserts the behaviour, and for each behaviour on
     `main` the change must not alter, the test that pins it (existing or new). For a change that must not alter
     behaviour, the oracle is captured by running `main`, at the commit the plan names, on the same inputs, through
     `main`'s entry points (its deciders) when the new units do not exist there; never typed by hand. No wall-clock or
     performance thresholds. A repo-scanning guard, census or lint only when you can name an incident where the same
     class came back after an earlier fix. Such a guard is exercised through the path that runs it, and reverting the
     fix makes it fail;
   - **the size budget:** estimated product and test lines. A product budget past twice the newest estimate (the
     proposal's, or a later one an owner ruling set) and 50 lines over it pulls the item before a lane, as 4.5 does.
     A stage over about 600 product lines is sub-staged here: the plan lists sub-stages that together deliver the
     ruled change and builds the first, its PR saying `Refs #<stage>`, and step 1 edits the stage down. No sub-stage
     leaves `main` with a known defect that a later sub-stage or stage must repair; where avoiding one needs a change
     to the ruled stage order, pull the item with that question, as the owner-reserved decisions below do;
   - **the pins,** for a stage: its ruling's, quoted;
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
   `CLAUDE.md`. Its prompt is literal text, with no shell expansion. The plan and rule files it names in the scratchpad
   are absolute paths; a repo path is relative to the lane's worktree, never the main checkout. It holds the issue, its
   rulings, your plan, the done-when, the branch, directory and base the plan names (new, or on an existing branch:
   rows 2, 6, 8, 10, 11, 12), and *Lane rules* below, pasted in verbatim.
   Run lanes in the background. Meanwhile, plan the next item; and once a round, check the 5 oldest ready `P3`s
   outside the round with no `## Evidence — premise` in 30 days. One is dead only when a reproduction showing its
   defect at the commit it cites no longer shows it on `origin/main` (never a latent one or one without such a
   reproduction; an error or a moved code point is not death): close it under `## Closed` with both runs; on each
   other, post `## Evidence — premise` with what you ran.
3. **Verify.**
   - Bring the head up to `origin/main` first: merge it in, and send a conflict back to the lane. If `main` has
     moved since you planned, re-run the plan's consumer and N searches, and re-capture an oracle (4.1) whose
     behaviour the merged `main` changed.
   - Confirm the lane's head is the PR's head, and read the whole diff against the plan: a hunk the plan does not
     cover goes back to the lane once.
4. **Adversarial review.** The reviewer is independent: `/code-review`, running in its own context. Brief it to
   break the PR, not to approve it: the invariant, the pins (a stage's ruling's), the done-when, the defects the
   change could introduce, and what must not change. Your own diff read in *Verify* is quality control, not this
   review. Start the CI watch in the background at the same time.
   - **Runtime PRs: `/code-review xhigh <pr>`.** These change what runs in a deployment, a pipeline or an agent:
     `faultmaven/`, `alembic/`, `resources/`, `requirements/`, `pyproject.toml`, `Dockerfile`, `docker-compose*`,
     `.github/workflows/`, `.github/scripts/`, `.importlinter`, `pytest.ini`, `scripts/` that CI or CD runs,
     `.claude/` and any `CLAUDE.md`.
   - **Every other PR: `/code-review high <pr>`.** A mixed PR takes the higher level.
   - **A defeat pass** (a review briefed to get past the guard) only for a guard on a security or tenancy boundary
     that has live sites.

   Each finding is exactly one of:
   - **fixed:** it is part of this item. It breaks the invariant; or it is a defect in code this PR adds; or it is a
     crash, wrong result or regression the PR causes anywhere, on any seam, a root's or candidate's included; or it is
     a test or guard this PR ships that cannot fail; or it shares the item's root. A stage's PR is verified against
     its pins;
   - **evidence:** it questions the design itself, with no defect in this PR's code: an `## Evidence` comment on the
     root (*Filing what you find*), outside the cap. One that questions a stage's own design holds its PR at row 4
     until the owner rules;
   - **filed:** it is true, valid and not part of this item (*Filing what you find*, below);
   - **declined:** one line in the PR body saying why. Either it is not true (not reproduced, and not shown at
     `path:line` on `origin/main` or the PR head), or it is not valid (as defined below), or an open issue already
     covers it (add the evidence there), or, on an exception delta (4.5), it is a mutation-survival or adversarial-edit
     finding against a test already shown to fail on `main`.

   Post the verdict on the PR as `## Review — <head>`, with the fix list, the `evidence` findings and the issues
   filed. A review or delta is
   **clean** when no `fixed` landing is outstanding and unwaived.
5. **Fix and delta.** The fix list goes to the same lane (`SendMessage`) as one commit. It tells the lane to find,
   in the files the PR touches, the other statements of each fact a fix changes, fix them in the same commit, and
   list them in its return. If the fix redesigns a mechanism,
   probe the new design first, as in the plan.
   - **The delta.** An independent reviewer always runs it; the author of the commits never does. It reviews what
     changed since the reviewed head against the fix list and the invariant, and also checks those other statements.
     Brief it with that diff and the lane's list of them. The diff leaves out `main`'s commits; never brief the PR
     number, which would re-review the whole PR:

     ```bash
     git fetch -q origin main "pull/<n>/head"
     t=$(git merge-tree --write-tree <reviewed> "$(git merge-base <head> origin/main)" | head -1)
     git diff "$t" <head>
     ```

     Post its verdict on the PR as `## Delta — <head>`, with the reviewed head, the reviewer and each finding's
     landing.

   - **A fix delta with any `fixed` finding** gets one more fix and delta. A second fix delta with any `fixed`
     finding pulls the item, unless each such finding is either a comment, docstring, document or missing test whose
     fix touches only comments, docstrings no code reads (not a model's or a route's), `docs/` or `tests/`, or local:
     its fix is in one file, at most 10 lines, with no new mechanism. That fix, if its commit keeps to those bounds,
     gets one exception commit and delta, once, outside the cap, where a `fixed` finding pulls the item.
   - **Size.** Passing twice the plan's product budget (or the newest budget an owner ruling set after it; never one
     you restated) and 50 lines over it pulls the item, as does growth carrying an owner-reserved decision. Test lines
     never count against it (#2198). Any smaller overrun, and any test overrun, is stated, with the new estimate, in
     the delta and the merge-ready.
   - **Never review the same code twice.** Each plan gets at most one design critique, and each PR one adversarial
     review and two fix deltas, plus the exception one above; more fix rounds pull the item to the owner (a
     rebuild, row 13, is a new plan and PR). A re-entry, a refresh, a conflict resolution or a change the owner asks
     for is a delta of only what is new, outside that cap. A whole-PR review is never repeated, nor run as a delta.
   - **No cap narrows a design change.** A stage's budget is the one its ruling sets, split by sub-staging (4.1)
     when it is over about 600 product lines; the sub-stages together still deliver the ruled change. Reaching the
     size bound or the fix-round cap pulls it to the owner with the question, and never narrows the change.
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

   - **A flake:** re-run it once, and add the run to its issue under `## Evidence`, filed if none is open (search
     first): the issue records the flake, and its root fix still follows. A running check reads `PENDING` (its
     `completedAt` is `0001-01-01T00:00:00Z`). Having re-run past it, its root fix is this round's work: in the item's
     PR when the flake's code is on the item's seam, else in its own small PR, under step 4 in full, as approved work,
     like a `main red` repair asked for in session. That PR takes the next free lane ahead of any **Next** item,
     counts toward the three lanes but not the round's five items, is not held back by the `in flight` limit, and is
     reported in the result. A re-run plus an issue is never the fix (4.1, *Filing what you find*).
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
   item it closes is in `pile:blocked` (row 7's exception aside); it is not waiting on a ruling on its design (row 4);
   the review and every delta are clean, with every finding landed or waived by a live ruling; the vouched-head check
   prints `clean`; every required check is green; it is not a draft; and on it you re-ran each lane's test command for
   any of its tests CI did not run there (a `skipif` the CI jobs meet, a suite the `changes` job skipped; `real_llm`
   only on the owner's approval). Then comment `## Merge-ready — <head>` with the base, the plan link, the design
   critique and its declined points, the review level, each delta's range and reviewer, the findings fixed, evidence,
   filed, declined and waived, the size against the budget, any command you re-ran with its tail output, and the
   checks.

To **pull** an item:
1. stop its lane;
2. move it to `pile:blocked` with `**Blocked on:** an owner ruling on …`, stating what stopped it and the options,
   then post `## Pulled` on it, saying why, the narrowest option that closes the item first (the exception commit,
   4.5, where it applies). For size or a deep root: build at the new estimate, mitigate now with the root as its own
   P1, or drop. For the fix cap: one more fix and delta of the named fix first, then merge as is, re-plan, or drop;
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
  pile, a priority and a class.
- **Search open and closed issues first.** If an open issue already covers it, add the evidence there under
  `## Evidence`. If a closed issue matches, cite it in the new issue. Findings that share one root go in one issue.
- **Classify it first** (*State*). A finding that shares a `root-issue`'s root is an `## Evidence` comment on it, not
  a new issue. Only one a stage must deliver is also filed, in `pile:blocked` with `**Blocked on:** an owner ruling
  on its place in #<root>`, and appended to the root issue's `**Blocks:**` line (a body edit); the next ruling on the
  root places it. One on a `root-candidate`'s seam is evidence there, and is also filed as ordinary work, keeping its
  place, when it is reachable or `P1`/`P2` (the evidence says `filed as #<f>`). A design flaw on a seam with no open
  root is a design check (*Pick*). Any other finding made while working on a root's issue is named in an
  `## Evidence` comment there: `different root, filed as #<f>`.
- **While the 14-day opened count exceeds the closed** (the health block), a PR's out-of-scope chore findings go into
  one `class:chore` issue per PR, and its latent `P3` defect findings into one `class:defect` issue per PR, each a
  checklist with `Found while working on #<n>`, closed when its list is done, and planned one item per seam, never in
  the slot, except the `class:chore` one in a chore-batch round (*Pick*). Reachable and `P1`/`P2` defects are still
  filed one each.
- **Fix the item's own root, a symptom of it, or a part of it in its PR;** file it only when a ruling waived it.

## 5. Report on the Queue

```
## Round <N> — result
| # | outcome (merged · merge-ready · carried · closed by verification or sweep · pulled · waiting on main or <service>) | PR | product +/− | tests +/− |
Closed <x> · Filed <y> · Net lines: product <p>, tests <t> · Open <before> → <after>
Trend: 14-day ratio <r> · open at the last four <a> → <b> → <c> → <d> · when Open rose, its diagnosis (step 2's diagnosis rules)
Filed: #<n> (P<k>) — <what, found while working on #<m>> — one line each
Waiting on you: queue each PR at its vouched head: gh pr merge <n> -R FaultMaven/<repo> --match-head-commit <sha> (plus --squash outside core)
```

Post it when every dispatched item and flake's fix is merged, merge-ready, closed, pulled or waiting, with a row per
item and its PR, and one per flake's fix (4.6); the PR column reads `private` for a private repo's PR (*Rules*).
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
- **Before pushing,** run `black --check faultmaven/ tests/`, `ruff check faultmaven/ tests/`, `lint-imports`, the
  tests that cover the change or pin text it edits, and the repo-wide guard suites (`tests/unit/ci`,
  `tests/unit/architecture`) in CI's environment, at most four workers, plus
  `python scripts/check_contract_version.py` if `docs/reference/api/` moved. Not the rest of the suite: CI runs it.
- **Tests** reproduce the defect and assert behaviour. No wall-clock thresholds, and no repo-scanning guard unless
  the plan names one. Keep within the plan's size budget, and say so if you cannot.
- **Closing keywords go in the PR body only**, never in the title or a commit message, one issue per line. The body
  says `Closes #<n>` only when the issue as written is delivered. Otherwise it says `Refs #<n>`, and you comment on
  the issue, under `## Delivered`, naming what was delivered.
- **Return:** the PR URL, each command with its tail output, `git status --short`, every departure from the
  plan, and anything outside the plan you found, with its evidence. You do not file it; the owning agent does. For a
  fix commit, also each other statement of a changed fact you fixed (line, old, new).

## Rules

- **Never merge.** Adding a PR to the merge queue is merging. The owner may authorize merging for the current
  session only, in as many words, and the authorization never carries over.
- **Write to issues and PRs only what these steps call for.** Every comment carries its heading (*State*). Close an
  issue only for a reason *State* allows.
- **Nothing written to the Queue names a private repo's PR or its merge command;** the owner gets them in session.
- **Never ask mid-build:** pull the item instead. Never build an item with an open question.
- **Never stack PRs.** Never hand over a PR that is not merge-ready.
- **Never quote a closing keyword followed by an issue number** in a PR body or comment, not even in a code span:
  the queries here read code spans.
- **Never file or relay a finding you have not verified:** reproduced, or shown at `path:line` on `origin/main`.
- **Never poll CI in the foreground,** and never from a lane or a review.
