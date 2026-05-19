---
description: Audit documentation within a domain for redundancy, contradictions, dead refs, and internal inconsistency. Docs-vs-docs only. Verifiable findings only. Each finding is freshness-gated against the scan-time commit so a stale report cannot drive bad action.
---

# /doc-audit

Detect documentation entropy (redundancy, contradictions, dead references, internal inconsistency) within a specific domain. **Docs-vs-docs only** — does NOT read code. Drift between docs and code is `/design-check`'s job.

## Argument

`$ARGUMENTS` — the domain name. Valid domains: `investigation`, `knowledge`, `data-processing`, `storage`, `auth`, `case`, `core-architecture`.

If missing or invalid, reject with the list of valid domains.

## Anti-fabrication contract

This skill compares text across documents. Every finding is a claim that two passages relate in a specific way (overlap, contradict, link-to-missing-thing, self-contradict). **A confabulated quote is the easiest way to fabricate a finding** — the LLM "remembers" what one doc says and pattern-matches it against another, but the remembered phrasing doesn't actually exist. To prevent that:

1. **Every claim about a document's content** must be a *verbatim* extract with `<file>:<line>`. Paraphrase is forbidden in finding text.
2. **Every cited quote must be re-grep-able.** If `grep -nF "<phrase>" <file>` doesn't return the cited line, the finding is invalid.
3. **Dead-reference claims** require *two* verifications: (a) the source-side quote (the link or mention, with file:line), and (b) a documented negative search showing the target doesn't exist in scope (e.g., `find <scope> -name '<target>' | wc -l` → 0, or `grep -rnE '^#+\s*<anchor>' <scope>` → 0 hits).
4. **Redundancy and contradiction** findings require quotes from *both* docs, each with file:line. A claim that "Doc A and Doc B cover the same topic" without two quotes is fabrication.
5. **Internal-inconsistency** findings require *two* verbatim quotes from the same doc with their respective line numbers.
6. **If you cannot verify a finding, omit it.** Do not soften, do not caveat — omit.

A short verified report beats a long unverifiable one.

## Anti-staleness contract

A correct-at-scan-time audit becomes harmful the moment the doc set moves past it. An LLM consumer following stale findings will revert valid recent edits, reintroduce already-resolved contradictions, or delete content that was just consolidated. **Worse than no audit.** This contract prevents that.

Hard rules every report must satisfy:

A. **Record the scan commit.** First line of the report (after the title) is `**Scanned at:** <commit-sha> <iso-timestamp> (working tree: clean | dirty)`. Captured by running `git rev-parse HEAD` and `git status --porcelain` at scan start.
B. **Capture expected output for every verification command.** Each finding's `Verify with:` block contains the command AND the exact output it produced at scan time. This makes staleness mechanically detectable: a current run that produces different output means the finding is stale.
C. **Emit a freshness gate at the top of the report.** A self-contained shell snippet that re-runs every verification command, diffs against captured output, and prints a per-finding `FRESH | STALE` verdict. Exits non-zero if any finding is STALE.
D. **Explicit consumer instruction.** The "Note to the agent acting on this report" section MUST instruct the consumer to run the freshness gate first and to skip any finding that comes up STALE. Stale findings are not "soft" — they must not be acted on.
E. **Per-finding granularity.** A partially stale report is fine: the consumer acts on FRESH findings and re-runs the audit for the rest. Don't invalidate the entire report on a single stale finding.

## Procedure (main conversation)

### 1. Look up the domain's doc scope

Read `.claude/manifest.json`. Find `domains.<name>.docs` — this is the list of paths (files or directories) in scope.

If the domain is not in the manifest, stop and tell the user.

### 2. Capture the scan anchor

Before spawning the subagent, record:

- `git rev-parse HEAD` → `SCAN_COMMIT`
- `git status --porcelain | wc -l` → `WORKING_TREE_DIRT` (0 = clean)
- `date -u +%Y-%m-%dT%H:%M:%SZ` → `SCAN_TIMESTAMP`

Pass all three to the subagent. The subagent will record them in the report header. If `WORKING_TREE_DIRT` > 0, warn the user — a scan against a dirty tree will produce findings whose freshness can't be reliably checked later.

### 3. Spawn the subagent

Spawn a `general-purpose` subagent via the Task tool with a fully self-contained prompt that incorporates both the anti-fabrication and anti-staleness contracts. This audit is read-heavy and would pollute the main conversation context.

---

**Subagent prompt template:**

> You are auditing FaultMaven documentation for internal consistency. You will not read any code. You will not fix anything. You will produce a freshness-anchored audit report.
>
> **Scan anchor** (pre-recorded by the caller — include verbatim in the report header):
>
> - `SCAN_COMMIT`: `<sha>`
> - `WORKING_TREE_DIRT`: `<N>` (0 means clean)
> - `SCAN_TIMESTAMP`: `<iso8601>`
>
> **Hard constraint 1 — anti-fabrication.** This audit compares text across documents. Confabulated quotes are the easiest way to fabricate a finding — the LLM "remembers" what one doc says and pattern-matches it against another, but the remembered phrasing doesn't actually exist in the doc. To prevent that, every claim about a document's content must be a *verbatim* extract with `<file>:<line>`, re-grep-able with `grep -nF`. **A short verified report beats a long unverifiable one.**
>
> Six rules:
>
> 1. **Every claim about a document's content** must be a *verbatim* extract with `<file>:<line>`. Paraphrase is forbidden.
> 2. **Every cited quote must be re-grep-able.** If `grep -nF "<phrase>" <file>` doesn't return the cited line, the finding is invalid.
> 3. **Dead-reference claims** require (a) the source-side quote (with file:line) and (b) a documented negative search showing the target doesn't exist in scope.
> 4. **Redundancy and contradiction** findings require quotes from *both* docs (with file:line each).
> 5. **Internal-inconsistency** findings require *two* verbatim quotes from the same doc.
> 6. **If you cannot verify, omit.** Do not soften, do not caveat — omit.
>
> **Hard constraint 2 — anti-staleness.** A correct-at-scan-time finding becomes harmful the moment the docs move past it. Every finding must carry enough captured state for a future consumer to mechanically detect staleness.
>
> 1. **Capture verification command output.** For every `Verify with:` command in a finding, run it once and record both the command AND its exact output (truncated to the most-discriminating ~80 chars). Embed both in the finding so a future re-run can be diff'd.
> 2. **Use stable matchers.** Prefer `grep -nF '<verbatim string>'` over line-number-only references — line numbers drift; verbatim content moves more slowly and is detectable when it changes.
> 3. **Emit a freshness gate** in the report (template below) that re-runs every captured verification command and prints `FRESH` or `STALE` per finding.
> 4. **Distinguish positive from absence assertions.** Each gate `check()` line has a mode: `contains` (default) means "FRESH iff the pattern is in the command output"; `absent` means "FRESH iff the pattern is NOT in the command output." Dead-reference target checks (the typical shape of "this file/anchor must remain missing") MUST use `absent` mode with the target name as the pattern. Never ship a check with `expected=""` and the default `contains` — that always passes and disables staleness detection for the finding. If you find yourself writing an empty pattern, that's the signal to convert to `absent` mode with a non-empty pattern.
>
> **Scope — documents to read:** `<DOC_PATHS>` (files and all `.md` files recursively within the listed directories, plus any `README.md`).
>
> **Step 1 — Enumerate the document set.** List every `.md` file within scope. Print the list before reading.
>
> **Step 2 — Read every document in scope in full.** As you read, capture verbatim extracts of any passage that *might* matter for a finding (claims about counts, rule names, status enums, named components, cross-references to other docs, repeated topics across files). Record each as `<file>:<line> | "<verbatim quote>"` in your scratch space. Do not paraphrase at this stage; only collect.
>
> **Step 3 — Analyze. Look for:**
>
> - **Redundancy:** Multiple documents covering the same topic with overlapping content. Cite verbatim excerpts from *each* overlapping doc. Note which document should be the single source of truth (usually the one declared canonical in `docs/architecture/README.md`, or the one the domain's README points to first).
> - **Contradictions:** Documents that state conflicting facts or conventions (e.g., two docs describing the same enum with different values, two docs describing the same flow with different steps). Cite the verbatim conflicting passages from both sides.
> - **Dead references:** Markdown links `[text](path)`, section anchors, or document titles that no longer exist within the documentation set. For each, record the source-side quote AND verify the target's non-existence (`find <scope> -name '<filename>'`, or `grep -rn '^#+\s*<anchor>' <scope>`). Do NOT verify code file paths against the codebase — that is out of scope.
> - **Internal inconsistency:** A single document whose content contradicts itself, or uses inconsistent terminology for the same concept. Cite two verbatim quotes from the same doc.
>
> You must NOT read code to detect staleness. Scope is strictly documentation cross-checking. If you notice a claim in a doc that "smells stale," note it only if another doc *also* in scope disagrees. Otherwise it's out of scope for this audit — use `/design-check <domain>` for doc-vs-code checks.
>
> **Step 4 — Self-verification pass (MANDATORY).** Before writing the report, take every candidate finding from Step 3 and run a second-pass verification AND capture the output:
>
> - For every quoted phrase: run `grep -nF "<verbatim phrase>" <file>`, **capture the output**. If grep returns nothing or different content, **drop the finding**.
> - For every dead-reference target: re-run the find / grep that proves non-existence and **capture the output** (should be 0). If hits are returned, the target exists — **drop the finding**.
> - For every contradiction or redundancy: verify both quotes independently and capture both outputs. If only one verifies, **drop the finding**.
>
> Maintain a verification log — one line per candidate finding — listing the verification commands run and their outcomes. This log goes into the report verbatim.
>
> **Step 5 — Write the report** to `docs/working/ANALYSIS-doc-audit-<DOMAIN>.md` with this structure:
>
> ````
> # Doc Audit: <domain>
>
> **Scanned at:** `<SCAN_COMMIT>` <SCAN_TIMESTAMP> (working tree: clean | dirty)
> **Documents assessed:** <count>
> **Scope:** <doc paths>
> **Findings included:** <count of verified findings>
> **Findings dropped during self-verification:** <count> (see Verification Log)
>
> Documents in scope:
> - <doc1>
> - <doc2>
> - ...
>
> ---
>
> ## ⚠️ Freshness gate — RUN THIS BEFORE ACTING ON ANY FINDING
>
> The audit was taken at commit `<SCAN_COMMIT>`. The doc set may have moved
> since then — most often because parallel PRs consolidated some of the
> entropy this report describes. **A finding that has been silently closed
> by a later PR must not be acted on; doing so reverts valid edits and can
> reintroduce already-resolved contradictions.**
>
> Copy the block below into a shell at the repo root. It re-runs every
> finding's verification command, diffs against the captured scan-time
> output, and prints a per-finding `FRESH | STALE` verdict.
>
> ```bash
> #!/bin/bash
> # Freshness gate for ANALYSIS-doc-audit-<DOMAIN>.md
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
> # Use `absent` for dead-reference target checks ("file/anchor must remain
> # missing") and for any other "X is not in Y" assertion. The default
> # `contains` mode silently passes on empty $expected — any absence
> # assertion MUST use `absent` mode to be staleness-detecting.
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
> # Multi-part findings (redundancy / contradiction) use suffixed IDs (-a, -b)
> # so a partial closure on one side correctly invalidates the finding.
> # Positive assertions — "this exact source-side quote still exists":
> check "R-1-a" '<command-from-R-1-source-A>' '<expected-substring-from-source-A>'
> check "R-1-b" '<command-from-R-1-source-B>' '<expected-substring-from-source-B>'
> # Dead-reference source quote — positive (the link still exists in the source doc):
> check "D-1-src" '<command-from-D-1-source-quote>' '<expected-substring>'
> # Dead-reference target — negative (the target filename must remain missing):
> check "D-1-tgt" '<find-or-grep-command-against-target>' '<target-filename-or-anchor>' absent
> # ... one check() line per verification per finding ...
>
> echo
> echo "Fresh: $fresh  Stale: $stale"
> if [[ $stale -gt 0 ]]; then
>   echo "STALE checks: ${failures[*]}"
>   echo "A finding is stale if ANY of its checks is stale (e.g., R-1 is stale if either R-1-a or R-1-b is stale)."
>   echo "Re-run /doc-audit <DOMAIN> for a current report. Do NOT act on stale findings."
>   exit 1
> fi
> ```
>
> If the gate exits non-zero, **stop**. Re-run `/doc-audit <DOMAIN>` to
> regenerate. Fresh findings are still actionable; stale findings are not.
>
> ---
>
> ## Note to the agent acting on this report
>
> **Before doing anything:** run the Freshness gate above. If a finding
> comes up STALE, the entropy it describes was already resolved by a later
> PR — acting on it will revert valid edits or reintroduce
> already-resolved contradictions. Stale ≠ "needs softer judgment"; stale
> = "do not touch."
>
> For each FRESH finding:
>
> - **Redundancy** — usually means migrating prose into the canonical doc and replacing the redundant passages with a one-line cross-reference. Don't delete content; consolidate it.
> - **Contradictions** — choose one source of truth (typically the one declared canonical in the domain README) and align the other.
> - **Dead references** — either restore the target or remove the reference; don't paper over with a placeholder.
> - **Internal inconsistency** — pick a single terminology / fact and apply it throughout the doc.
> - **When evidence is ambiguous, consult a human.**
>
> ---
>
> ## Redundancy
>
> ### [R-1] <short title>
>
> - **Overlap topic** — <one-line summary>
> - **Source A** — `<docA>:<lineA>` "<verbatim quote A>"
> - **Source B** — `<docB>:<lineB>` "<verbatim quote B>"
> - **Recommended canonical** — <which doc, and why>
> - **Verify with:**
>   ```bash
>   grep -nF "<quote A>" <docA>
>   grep -nF "<quote B>" <docB>
>   ```
>   **Expected output (captured at scan):**
>   ```
>   <lineA>:<content of line A>
>   <lineB>:<content of line B>
>   ```
>
> ## Contradictions
>
> ### [C-1] <short title>
>
> - **Claim conflict** — <one-line summary>
> - **Source A** — `<docA>:<lineA>` "<verbatim quote A>"
> - **Source B** — `<docB>:<lineB>` "<verbatim quote B>"
> - **Verify with:**
>   ```bash
>   grep -nF "<quote A>" <docA>
>   grep -nF "<quote B>" <docB>
>   ```
>   **Expected output (captured at scan):**
>   ```
>   <lineA>:<content of line A>
>   <lineB>:<content of line B>
>   ```
>
> ## Dead References
>
> ### [D-1] <short title>
>
> - **Source-side reference** — `<docA>:<lineA>` "<verbatim quote including the link or mention>"
> - **Target** — `<path or anchor>`
> - **Non-existence proof at scan time** — `<find or grep command>` → 0 hits
> - **Verify with:**
>   ```bash
>   grep -nF "<source quote>" <docA>
>   <find-or-grep-non-existence-command>
>   ```
>   **Expected output (captured at scan):**
>   ```
>   <lineA>:<content of line A>
>   0
>   ```
>
> ## Internal Inconsistency
>
> ### [II-1] <short title>
>
> - **Doc** — `<docA>`
> - **Quote 1** — `<docA>:<line1>` "<verbatim>"
> - **Quote 2** — `<docA>:<line2>` "<verbatim>"
> - **Why these conflict** — <one sentence>
> - **Verify with:**
>   ```bash
>   grep -nF "<quote 1>" <docA>
>   grep -nF "<quote 2>" <docA>
>   ```
>   **Expected output (captured at scan):**
>   ```
>   <line1>:<content of line 1>
>   <line2>:<content of line 2>
>   ```
>
> ## Verification Log
>
> | ID | Re-verification command | Outcome (captured) |
> |----|-------------------------|---------|
> | R-1 | `grep -nF "..." <fileA>` | confirmed at scan: returns line A |
> | R-1 | `grep -nF "..." <fileB>` | confirmed at scan: returns line B |
> | ... | ... | ... |
>
> **Dropped during self-verification:**
>
> - <candidate finding text> — reason: <e.g., "quote not found by grep", "target file exists at <path>">
>
> ## Overall Assessment
>
> <one paragraph: magnitude of *verified* entropy at the scan commit and where it concentrates. Do not extrapolate from dropped findings. State explicitly that this assessment is anchored to `<SCAN_COMMIT>`.>
> ````
>
> Leave any section that has no findings with the text "None." beneath the section heading. (The freshness gate stays — it's a no-op when there are no findings.)
>
> **Step 6 — Final integrity check (MANDATORY).** Before printing the report path, pick 3 random findings from the file you just wrote and:
>
> 1. Re-run their `Verify with:` commands and confirm the captured "Expected output" still matches.
> 2. Confirm the freshness gate's `check` lines for those findings reference the exact captured expected substrings (no paraphrase, no edits).
>
> If any spot-check fails, fix the report (drop the finding, log the drop, update the freshness gate) and re-run the integrity check. Only print the report path when all 3 spot-checks pass.

---

### 4. Surface the report

After the subagent finishes, show the user:

1. The report path.
2. A one-line summary (e.g., *"4 redundancies, 1 contradiction, 0 dead refs, 2 internal inconsistencies, 3 dropped — see `docs/working/ANALYSIS-doc-audit-investigation.md`"*).
3. The scan commit SHA.
4. **An explicit reminder to run the freshness gate before acting** — paste the gate's bash block into a shell at the repo root; STALE findings must not be acted on.

## Completion criteria

Done when:

a. The report file exists.
b. Every finding has a re-runnable `Verify with:` command AND a captured `Expected output` block.
c. The report header records `Scanned at: <commit-sha> <timestamp> (working tree: ...)`.
d. The Freshness gate section at the top of the report contains one `check` line per verification (with suffixed IDs for multi-part findings like `R-1-a` / `R-1-b`), referencing the exact captured expected substring.
e. The "Note to the agent" section instructs the consumer to run the gate before acting and to skip stale findings.
f. The Verification Log lists outcomes for each finding plus any drops.

## Rules

- Docs-vs-docs only. Do not read code.
- Do not auto-fix, auto-delete, or auto-edit any documents.
- Use `/design-check <domain>` for docs-vs-code drift.
- **Verifiability over coverage.** Omit anything you cannot ground in a verbatim quote and a re-runnable command.
- **Freshness over completeness.** A 3-finding report that mechanically self-verifies is worth more than a 15-finding report whose consumer has no way to detect staleness.
- The freshness gate is non-negotiable. If you cannot generate captured expected output for a finding, drop the finding rather than ship it without a gate entry.
