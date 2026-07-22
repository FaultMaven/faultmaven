---
description: Detect drift between design docs and implementation for a domain, in both directions. Does not pick a side. Verifiable findings only. Each finding is freshness-gated against the scan-time commit so a stale report cannot drive bad action.
---

# /design-check

Detect drift between design documents and implementation within a specific domain, in **both directions**:

- **Design-only:** the design specifies something the code does not implement
- **Code-only:** the code does something no design doc describes

Does NOT decide which side is "correct" or whether a divergence is a positive adaptation vs. a regression — a separate triage pass makes that call. This skill's job is **objective, verifiable, freshness-anchored identification of differences**. Call out unambiguous bugs, errors, or internal inconsistencies (e.g., a repository targeting a table that does not exist, two enums with the same name and different value spaces) — those are factual findings, not judgments.

## Working directory

All paths in this command are relative to the faultmaven repository root (the directory containing `.claude/manifest.json`). Run every command from there, and instruct the subagent to do the same.

## Argument

`$ARGUMENTS` — the domain name, trimmed and lowercased. The set of valid domains is the set of keys under `domains` in `.claude/manifest.json` (the single source of truth; at the time of writing: `investigation`, `knowledge`, `data-processing`, `storage`, `auth`, `case`, `core-architecture`).

If the argument is missing or is not a manifest key, stop and reply with the list of keys read from the manifest. Do not guess a close match.

## Anti-fabrication contract

This skill has previously produced reports with confabulated quotes, fabricated line numbers, and invented threshold values. The procedure below makes that impossible. **A 5-finding verified report is far more valuable than a 30-finding unverifiable one.** Coverage is not a goal; verifiability is.

Hard rules every finding must satisfy:

1. **Every claimed doc quote** must be a *verbatim* extract with `<file>:<line>` and must be re-grep-able. Paraphrase is forbidden in finding text. If you cannot grep the exact phrase from the cited file, the finding is invalid.
2. **Every claimed code behavior** must include `<file>:<line>` pointing to the exact line that demonstrates the behavior. If the cited line does not say what the finding claims, the finding is invalid.
3. **Every "not mentioned in X" claim** requires a documented negative grep: list the search keywords used and the number of hits returned (must be 0 for the claim to stand).
4. **Every enum-value, threshold, or count claim** must be obtained by grep against the file containing the literal, or by executing code (`python3 -c "..."` from the repo root) — never by paraphrase or recall. If the python import fails (missing deps/env), fall back to grep on the defining file; do not substitute a remembered value.
5. **If you cannot verify a finding, omit it.** Do not "soften" it, do not include it with a caveat, do not include it at all.

## Anti-staleness contract

A correct-at-scan-time report becomes harmful the moment the codebase moves past it. An LLM consumer following stale findings will revert valid recent work and create regressions. **Worse than no report.** This contract prevents that.

Hard rules every report must satisfy:

A. **Record the scan commit.** First line of the report (after the title) is `**Scanned at:** <commit-sha> <iso-timestamp> (working tree: clean | dirty)`. Captured by running `git rev-parse HEAD` and `git status --porcelain` at scan start.
B. **Capture expected output for every verification command.** Each finding's `Verify with:` block contains the command AND the exact output it produced at scan time. This makes staleness mechanically detectable: a current run that produces different output means the finding is stale.
C. **Emit a freshness gate at the top of the report.** A self-contained shell snippet that re-runs every verification command, diffs against captured output, and prints a per-finding `FRESH | STALE` verdict. Exits non-zero if any finding is STALE.
D. **Explicit consumer instruction.** The "Note to the agent acting on this report" section MUST instruct the consumer to run the freshness gate first and to skip any finding that comes up STALE. Stale findings are not "soft" — they must not be acted on.
E. **Per-finding granularity.** A partially stale report is fine: the consumer acts on FRESH findings and re-runs the scan for the rest. Don't invalidate the entire report on a single stale finding.

## Procedure (main conversation)

### 1. Look up the domain's scope

Read `.claude/manifest.json`. Find `domains.<name>`:

- `docs` — list of doc paths (files or directories)
- `code` — list of code paths (directories or files)

If the domain is not in the manifest, stop and tell the user.

### 2. Capture the scan anchor

Before spawning the subagent, record:

- `git rev-parse HEAD` → `SCAN_COMMIT`
- `git status --porcelain | wc -l` → `WORKING_TREE_DIRT` (0 = clean)
- `date -u +%Y-%m-%dT%H:%M:%SZ` → `SCAN_TIMESTAMP`

Pass all three to the subagent. The subagent will record them in the report header (`WORKING_TREE_DIRT` = 0 renders as `working tree: clean`, otherwise `working tree: dirty (<N> files)`). If `WORKING_TREE_DIRT` > 0, warn the user — a scan against a dirty tree will produce findings whose freshness can't be reliably checked later — but proceed.

### 3. Spawn the subagent

Spawn a `general-purpose` subagent (via the Task tool, or whatever the subagent-spawning tool is named in this harness) with the prompt template below. This check reads both docs and code for the full domain and would pollute the main conversation context.

Before sending, substitute **every** `<...>` placeholder: `<sha>` / `<N>` / `<iso8601>` from Step 2, `<DOC_PATHS>` / `<CODE_PATHS>` copied verbatim from the manifest entry, and `<DOMAIN>` = the validated argument. The prompt must be fully self-contained — the subagent cannot see this file or the conversation.

---

**Subagent prompt template:**

> You are performing a design-vs-code drift check for FaultMaven. You will read both design documents and code. You will not fix anything. You will produce a freshness-anchored drift report.
>
> **Scan anchor** (pre-recorded by the caller — include verbatim in the report header):
>
> - `SCAN_COMMIT`: `<sha>`
> - `WORKING_TREE_DIRT`: `<N>` (0 means clean)
> - `SCAN_TIMESTAMP`: `<iso8601>`
>
> **Hard constraint 1 — anti-fabrication.** Previous reports have confabulated quotes, fabricated line numbers, and invented thresholds. Every claim must be re-grep-able from the source. **A short verified report beats a long unverifiable one.** Five rules:
>
> 1. **Every claimed doc quote** must be a *verbatim* extract with `<file>:<line>`. Paraphrase is forbidden. If you cannot grep the exact phrase, the finding is invalid.
> 2. **Every claimed code behavior** must include `<file>:<line>` pointing to the exact line that demonstrates it. If the line doesn't say what the finding claims, the finding is invalid.
> 3. **Every "not mentioned in X" claim** requires a documented negative grep: keywords used + 0-hit confirmation.
> 4. **Every enum-value, threshold, or count claim** must be obtained by grep on the file containing the literal, or by executing code (`python3 -c "..."` from the repo root) — never by paraphrase or recall. If the python import fails (missing deps/env), fall back to grep on the defining file.
> 5. **If you cannot verify a finding, omit it.** No softening, no caveats.
>
> **Hard constraint 2 — anti-staleness.** A correct-at-scan-time finding becomes harmful the moment the code moves past it. Every finding must carry enough captured state for a future consumer to mechanically detect staleness.
>
> 1. **Capture verification command output.** For every `Verify with:` command in a finding, run it once and record both the command AND its exact output. The finding's `Expected output` block shows the captured line(s) in full; the gate's `expected` argument is a *discriminating substring* of that output (single line, ≤80 chars) — the gate passes iff that substring appears in a fresh run of the command.
> 2. **Use stable matchers.** Prefer `grep -nF '<verbatim string>'` over line-number-only references (`sed -n '<N>p'` is acceptable when paired with a verbatim grep on the same content — the grep is the primary check, the sed is the locator). Line numbers drift; verbatim content moves more slowly and is detectable when it changes. For grep patterns, quotes must come from a single line — `grep -F` cannot match across lines.
> 3. **Emit a freshness gate** in the report (template below) that re-runs every captured verification command and prints `FRESH` or `STALE` per finding.
> 4. **Distinguish positive from absence assertions.** Each gate `check()` line has a mode: `contains` (default) means "FRESH iff the pattern is in the command output"; `absent` means "FRESH iff the pattern is NOT in the command output." Negative-grep findings (the typical shape of design-only "code does not implement X" claims) MUST use `absent` mode with the pattern that should stay missing. Never ship a check with `expected=""` and the default `contains` — that always passes and disables staleness detection for the finding. If you find yourself writing an empty pattern, that's the signal to convert to `absent` mode with a non-empty pattern.
> 5. **Keep gate arguments shell-safe.** The gate `eval`s each command and substring-matches `expected`, both passed as single-quoted shell strings. Every command and expected pattern must therefore be a single line containing no single quotes, backticks, `$`, or backslashes. If the natural verbatim quote contains one of those characters, pick a different discriminating substring from the same line that doesn't — the full verbatim line still lives in the finding's `Expected output` block.
>
> **Scope:**
>
> - Design docs: `<DOC_PATHS>`
> - Code: `<CODE_PATHS>`
>
> **Step 1 — Enumerate.** List every `.md` file in the doc scope and every `.py` file in the code scope (exclude `__pycache__` and empty `__init__.py`). Print the lists.
>
> **Step 2 — Read the design docs.** Read each doc file in full. As you read, capture *direct quotes* of specific claims (numbered rules, threshold values, enum-value lists, named transitions, data-structure declarations). Record each as `<file>:<line> | "<verbatim quote>"`. Do not paraphrase. If a doc is long, read it in sections — but never quote from memory; always re-read before quoting.
>
> **Step 3 — Read the code.** For each doc-extract from Step 2, attempt to locate the corresponding implementation:
>
> - **Present and matching** → no finding.
> - **Present but diverges** → candidate code-divergence finding. Record the code `<file>:<line>` showing the divergence and the actual code text.
> - **Absent (grep-confirmed)** → candidate design-only finding. Record the keywords you grep'd for and confirm 0 hits.
>
> Then read the code for *significant* behaviors not covered in Step 2 (state transitions, gates, milestones, enums, prompt structures, tools, hypotheses, scoring, recovery flows, journaling, intent/indicator resolution — NOT internal plumbing like DI wiring, logging, private helpers). For each, grep the design docs for keywords related to that behavior:
>
> - **Found in docs** → no finding.
> - **Not found (grep-confirmed)** → candidate code-only finding. Record grep keywords + 0-hit confirmation.
>
> **Step 4 — Self-verification pass (MANDATORY).** Before writing the report, take every candidate finding from Step 3 and run a second-pass verification AND capture the output:
>
> - For every quoted doc phrase: run `grep -nF "<verbatim phrase>" <doc file>`, **capture the output**. If grep returns nothing or different content, **drop the finding**.
> - For every cited code line: re-Read the file with offset matching the cited line and confirm the line content matches the finding's claim. If it doesn't, **drop the finding**. Where possible also `grep -nF` on a stable verbatim substring of the line and capture that output too.
> - For every "not in X" claim: re-run the grep with recorded keywords and confirm 0 hits. If hits are returned, **drop the finding**.
> - For every threshold/count/enum-value: re-execute the `python3 -c "..."` or grep that produced the value and **capture the output**. If the value differs, **drop the finding**.
>
> Maintain a verification log in your scratch space — one line per finding — listing the commands run and their outcomes. This log goes into the report.
>
> **Step 5 — Stay objective; do not judge adaptive vs. regression.** Report divergences in both directions with evidence. A separate triage pass decides whether each item should be resolved by updating the design or by changing the code — that is not your call. However, call out unambiguous bugs, errors, or internal inconsistencies as facts when they are clear from the evidence. State them as observations, not recommendations.
>
> **Step 6 — Write the report** to `docs/working/ANALYSIS-design-check-<DOMAIN>.md` (overwrite any previous report at that path) with this structure:
>
> ````markdown
> # Design Check: <domain>
>
> **Scanned at:** `<SCAN_COMMIT>` <SCAN_TIMESTAMP> (working tree: clean | dirty)
> **Docs in scope:** <list of .md files actually read>
> **Code in scope:** <list of .py files actually read or grep-scanned>
> **Findings included:** <count of verified findings>
> **Findings dropped during self-verification:** <count> (see Verification Log)
>
> ---
>
> ## ⚠️ Freshness gate — RUN THIS BEFORE ACTING ON ANY FINDING
>
> The scan was taken at commit `<SCAN_COMMIT>`. The codebase may have moved
> since then — most often because parallel PRs closed some of the drift this
> report describes. **A finding that has been silently closed by a later PR
> must not be acted on; doing so reverts valid work and creates regressions.**
>
> Copy the block below into a shell at the repo root. It re-runs every
> finding's verification command, diffs against the captured scan-time
> output, and prints a per-finding `FRESH | STALE` verdict.
>
> ```bash
> #!/bin/bash
> # Freshness gate for ANALYSIS-design-check-<DOMAIN>.md
> # Scanned at: <SCAN_COMMIT>
> # Re-run before acting on any finding.
>
> set -u
> fresh=0; stale=0; failures=()
>
> # Modes:
> #   contains (default) — FRESH iff $expected is a substring of command output
> #   absent             — FRESH iff $expected is NOT in command output
> #
> # Use `absent` for negative-grep / "X is not mentioned in Y" findings.
> # The default `contains` mode silently passes on empty $expected, so any
> # absence assertion MUST use the `absent` mode to be staleness-detecting.
> check() {
>   local id="$1" cmd="$2" expected="$3" mode="${4:-contains}"
>   local actual; actual=$(eval "$cmd" 2>&1 || true)
>   local pass=false
>   if [[ "$mode" == "absent" ]]; then
>     [[ "$actual" != *"$expected"* ]] && pass=true
>   else
>     [[ "$actual" == *"$expected"* ]] && pass=true
>   fi
>   if $pass; then
>     echo "[FRESH] $id"; fresh=$((fresh+1))
>   else
>     echo "[STALE] $id  mode=$mode  pattern: $expected  got: ${actual:0:120}"
>     stale=$((stale+1)); failures+=("$id")
>   fi
> }
>
> # Positive assertion — "this exact line still exists":
> check "DO-1" '<command-from-finding-DO-1>' '<expected-substring-from-finding-DO-1>'
> # Negative assertion — "this symbol must remain absent":
> # check "DO-2b" '<negative-grep-from-finding-DO-2>' '<pattern-that-must-stay-missing>' absent
> # ... one check() line per finding ...
>
> echo
> echo "Fresh: $fresh  Stale: $stale"
> if [[ $stale -gt 0 ]]; then
>   echo "STALE findings: ${failures[*]}"
>   echo "Re-run /design-check <DOMAIN> for a current report. Do NOT act on stale findings."
>   exit 1
> fi
> ```
>
> If the gate exits non-zero, **stop**. Re-run `/design-check <DOMAIN>` to
> regenerate. Fresh findings are still actionable; stale findings are not.
>
> ---
>
> ## Note to the agent acting on this report
>
> **Think of this report as a CT scan, not a treatment plan.** It lists
> objective differences between what the design documents specify and what
> the code actually does at the scan commit. It does not prescribe fixes.
>
> **Before doing anything:** run the Freshness gate above. If a finding
> comes up STALE, the drift it describes was already closed by a later PR
> — acting on it will revert valid work. Stale ≠ "needs softer judgment";
> stale = "do not touch."
>
> For each FRESH finding, decide the disposition:
>
> 1. **Positive pivot** (deliberate improvement, code is now the better reality) → update the design document to match the code.
> 2. **Regression / bug / incomplete work** → bring the code back to the design specification.
> 3. **Unambiguous inconsistencies are factual errors, not interpretive calls.** Fix regardless of which side you favor.
> 4. **Evaluate each finding individually** — a section may mix "update docs" and "fix code" items.
> 5. **When evidence is ambiguous, consult a human.**
> 6. **Think systemically.** A locally clean fix that weakens the system is worse than a messier fix that strengthens it.
>
> ---
>
> ## Design-only (spec says X, code does not)
>
> ### [DO-1] <short title>
>
> - **Doc claim** — `<doc-file>:<line>` "<verbatim quote>"
> - **Expected in code** — `<code-path-searched>`
> - **Negative grep** — `grep -rn "<keywords>" <code-path>` → 0 hits at scan time
> - **Verify with:**
>   ```bash
>   grep -nF "<verbatim quote>" <doc-file>
>   ```
>   **Expected output (captured at scan):**
>   ```
>   <line>:<verbatim line content as captured>
>   ```
>
> ## Code-only (code does Y, no spec for Y)
>
> ### [CO-1] <short title>
>
> - **Code behavior** — `<code-file>:<line>` (excerpt: `<one-line code extract>`)
> - **Docs checked** — `<doc-paths-searched>`
> - **Negative grep** — `grep -rn "<keywords>" <doc-path>` → 0 hits at scan time
> - **Verify with:**
>   ```bash
>   grep -nF "<one-line code extract>" <code-file>
>   ```
>   **Expected output (captured at scan):**
>   ```
>   <line>:<code line content>
>   ```
>
> ## Unambiguous inconsistencies
> (Only include if you found factual errors with re-runnable verification. State as observations, no recommendations.)
>
> ### [UI-1] <short title>
>
> - **Observation** — <one-sentence factual statement>
> - **Evidence** — `<file1>:<line1>` "<verbatim>" vs `<file2>:<line2>` "<verbatim>"
> - **Verify with:**
>   ```bash
>   <commands that demonstrate the contradiction>
>   ```
>   **Expected output (captured at scan):**
>   ```
>   <captured output that shows the contradiction>
>   ```
>
> ## Matches
> (Optional — one paragraph summarizing what was verified as cleanly aligned at the scan commit.)
>
> ## Verification Log
>
> Each finding above was re-verified after the initial draft. Findings that failed re-verification were dropped.
>
> | ID | Re-verification command | Outcome (captured) |
> |----|------------------------|---------|
> | DO-1 | `grep -nF "..." <file>` | confirmed at scan: returns line N |
> | CO-1 | `sed -n 'Np' <file>` | confirmed at scan: matches |
> | ... | ... | ... |
>
> **Dropped during self-verification:**
>
> - <candidate finding text> — reason: <e.g., "doc quote not found by grep">
>
> ## Overall Assessment
>
> <one paragraph: magnitude of *verified* drift at the scan commit and where it concentrates. Do not extrapolate from dropped findings. State explicitly that this assessment is anchored to `<SCAN_COMMIT>`.>
> ````
>
> Leave any section that has no findings with the text "None." beneath the section heading. (The freshness gate stays — with zero findings it simply prints `Fresh: 0  Stale: 0`.)
>
> **Step 7 — Final integrity check.** Before printing the report path, pick 3 findings at random from the file you just wrote (all of them if the report has 3 or fewer) and:
>
> 1. Re-run their `Verify with:` commands and confirm the captured "Expected output" still matches.
> 2. Confirm the freshness gate's `check` lines for those findings reference the exact captured expected substrings (no paraphrase, no edits).
>
> If any spot-check fails, fix the report (drop the finding, log the drop, update the freshness gate) and re-run the integrity check. Only print the report path when all 3 spot-checks pass.

---

### 4. Verify, then surface the report

After the subagent finishes, verify mechanically before telling the user anything:

1. Confirm the report file exists at `docs/working/ANALYSIS-design-check-<DOMAIN>.md` and its header records the scan commit from Step 2.
2. Extract the freshness-gate bash block from the report and run it once from the repo root. Immediately after a scan, **every check must print FRESH** — a STALE-at-birth check means its captured expected output is wrong (a capture error or fabrication), not that the code moved. If any check is STALE, send the subagent back to fix or drop that finding (via SendMessage/resume if available, otherwise a follow-up subagent with the report and the failing check IDs), then re-run the gate.

Then show the user:

1. The report path.
2. A one-line summary (e.g., *"4 design-only, 2 code-only, 1 unambiguous inconsistency, 11 candidates dropped — see `docs/working/ANALYSIS-design-check-investigation.md`"*).
3. The scan commit SHA, and confirmation that the gate ran clean (all FRESH) at scan time.
4. **An explicit reminder to run the freshness gate before acting later** — paste the gate's bash block into a shell at the repo root; STALE findings must not be acted on.

## Completion criteria

Done when:

a. The report file exists.
b. Every finding has a re-runnable `Verify with:` command AND a captured `Expected output` block.
c. The report header records `Scanned at: <commit-sha> <timestamp> (working tree: ...)`.
d. The Freshness gate section at the top of the report contains one `check` line per finding, referencing the exact captured expected output.
e. The "Note to the agent" section instructs the consumer to run the gate before acting and to skip stale findings.
f. The Verification Log lists outcomes for each finding plus any drops.
g. The caller ran the freshness gate once immediately after the scan and every check printed FRESH.

## Rules

- Do not fix code. Do not fix docs. The report is the deliverable.
- Do not pick a side. Drift is reported symmetrically.
- "Significant code behavior" excludes internal plumbing (DI wiring, logging, private helpers). Focus on behaviors a design doc *would* describe.
- **Verifiability over coverage.** Omit anything you cannot ground in a verbatim quote and a re-runnable command.
- **Freshness over completeness.** A 3-finding report that mechanically self-verifies is worth more than a 15-finding report whose consumer has no way to detect staleness.
- The freshness gate is non-negotiable. If you cannot generate captured expected output for a finding, drop the finding rather than ship it without a gate entry.
