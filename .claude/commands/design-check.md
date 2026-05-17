---
description: Detect drift between design docs and implementation for a domain, in both directions. Does not pick a side. Verifiable findings only.
---

# /design-check

Detect drift between design documents and implementation within a specific domain, in **both directions**:
- **Design-only:** the design specifies something the code does not implement
- **Code-only:** the code does something no design doc describes

Does NOT decide which side is "correct" or whether a divergence is a positive adaptation vs. a regression — a separate triage pass makes that call. This skill's job is **objective, verifiable identification of differences**. Call out unambiguous bugs, errors, or internal inconsistencies (e.g., a repository targeting a table that does not exist, two enums with the same name and different value spaces) — those are factual findings, not judgments.

## Argument

`$ARGUMENTS` — the domain name. Valid domains: `investigation`, `knowledge`, `data-processing`, `storage`, `auth`, `case`, `core-architecture`.

If missing or invalid, reject with the list of valid domains.

## Anti-fabrication contract

This skill has previously produced reports with confabulated quotes, fabricated line numbers, and invented threshold values. The procedure below is designed to make that impossible. **A 5-finding verified report is far more valuable than a 30-finding unverifiable one.** Coverage is not a goal; verifiability is.

The hard rules every finding must satisfy:

1. **Every claimed doc quote** must be a *verbatim* extract with `<file>:<line>` and must be re-grep-able. Paraphrase is forbidden in finding text. If you cannot grep the exact phrase from the cited file, the finding is invalid.
2. **Every claimed code behavior** must include `<file>:<line>` pointing to the exact line that demonstrates the behavior. If the cited line does not say what the finding claims, the finding is invalid.
3. **Every "not mentioned in X" claim** requires a documented negative grep: list the search keywords used and the number of hits returned (must be 0 for the claim to stand).
4. **Every enum-value, threshold, or count claim** must be obtained by executing code (`python3 -c "..."`) or by grep against the file containing the literal — never by paraphrase or recall.
5. **If you cannot verify a finding, omit it.** Do not "soften" it, do not include it with a caveat, do not include it at all.

## Procedure (main conversation)

### 1. Look up the domain's scope

Read `.claude/manifest.json`. Find `domains.<name>`:
- `docs` — list of doc paths (files or directories)
- `code` — list of code paths (directories or files)

If the domain is not in the manifest, stop and tell the user.

### 2. Spawn the subagent

Spawn a `general-purpose` subagent via the Task tool with a fully self-contained prompt that incorporates the anti-fabrication contract above. This check reads both docs and code for the full domain and would pollute the main conversation context.

---

**Subagent prompt template:**

> You are performing a design-vs-code drift check for FaultMaven. You will read both design documents and code. You will not fix anything. You will produce a drift report.
>
> **Hard constraint — every finding must be re-runnable.** This skill previously produced reports with confabulated quotes, fabricated line numbers, and invented threshold values. To prevent that, **every claim you make must be re-grep-able from the source.** A short verified report beats a long unverifiable one. Coverage is not a goal; verifiability is.
>
> The five hard rules:
>
> 1. **Every claimed doc quote** must be a *verbatim* extract with `<file>:<line>`. Paraphrase is forbidden. If you cannot grep the exact phrase from the cited file, the finding is invalid.
> 2. **Every claimed code behavior** must include `<file>:<line>` pointing to the exact line that demonstrates the behavior. If the cited line does not say what the finding claims, the finding is invalid.
> 3. **Every "not mentioned in X" claim** requires a documented negative grep: list the search keywords and confirm 0 hits in the cited file/directory.
> 4. **Every enum-value, threshold, or count claim** must be obtained by executing code (`python3 -c "..."`) or by grep against the file containing the literal — never by paraphrase or recall.
> 5. **If you cannot verify a finding, omit it.** Do not soften, do not caveat — omit.
>
> **Scope:**
> - Design docs: `<DOC_PATHS>`
> - Code: `<CODE_PATHS>`
>
> **Step 1 — Enumerate.** List every `.md` file in the doc scope and every `.py` file in the code scope (exclude `__pycache__` and empty `__init__.py`). Print the lists.
>
> **Step 2 — Read the design docs.** Read each doc file in full. As you read, capture *direct quotes* of specific claims (numbered rules, threshold values, enum-value lists, named transitions, data-structure declarations). Record each as `<file>:<line> | "<verbatim quote>"`. Do not paraphrase at this stage; only collect extracts. If a doc is long, read it in sections — but never quote from memory; always re-read before quoting.
>
> **Step 3 — Read the code.** For each doc-extract from Step 2, attempt to locate the corresponding implementation:
>
> - **Present and matching** → no finding.
> - **Present but diverges** → candidate code-divergence finding. Record the code `<file>:<line>` showing the divergence and the actual code text.
> - **Absent (grep-confirmed)** → candidate design-only finding. Record the keywords you grep'd for and confirm 0 hits.
>
> Then read the code for behaviors not covered in Step 2. For each one that is *significant* (state transitions, gates, milestones, enums, prompt structures, tools, hypotheses, scoring, recovery flows, journaling, intent/indicator resolution — NOT internal plumbing like DI wiring, logging, private helpers), grep the design docs for keywords related to that behavior:
>
> - **Found in docs** → no finding.
> - **Not found (grep-confirmed)** → candidate code-only finding. Record the grep keywords + 0-hit confirmation.
>
> **Step 4 — Self-verification pass (MANDATORY).** Before writing the report, take every candidate finding from Step 3 and run a second-pass verification:
>
> - For every quoted doc phrase: `grep -nF "<verbatim phrase>" <doc file>` — confirm it returns the cited line. If grep returns nothing or a different line, **drop the finding**.
> - For every cited code line: re-Read the file with offset matching the cited line and confirm the line content matches the finding's claim. If it doesn't, **drop the finding**.
> - For every "not in X" claim: re-run the grep with the recorded keywords against the cited file/directory and confirm 0 hits. If hits are returned, **drop the finding**.
> - For every threshold/count/enum-value: re-execute the `python3 -c "..."` or grep that produced the value. If the value differs, **drop the finding**.
>
> Maintain a verification log in your scratch space — one line per finding — listing the verification commands run and their outcomes. This log goes into the report.
>
> **Step 5 — Stay objective; do not judge adaptive vs. regression.** Report divergences in both directions with evidence. A separate triage pass decides whether each item should be resolved by updating the design or by changing the code — that is not your call. However, call out unambiguous bugs, errors, or internal inconsistencies as facts when they are clear from the evidence (e.g., code referencing a schema object that does not exist anywhere, two same-named enums with incompatible value spaces, a storage layout that contradicts itself across files). State them as observations, not recommendations.
>
> **Step 6 — Write the report** to `docs/working/ANALYSIS-design-check-<DOMAIN>.md` with this structure:
>
> ```
> # Design Check: <domain>
>
> **Docs in scope:** <list of .md files actually read in full>
> **Code in scope:** <list of .py files actually read or grep-scanned>
> **Findings included:** <count of verified findings>
> **Findings dropped during self-verification:** <count> (see Verification Log)
>
> ## Note to the agent acting on this report
>
> **Think of this report as a CT scan, not a treatment plan.** It lists objective differences between what the design documents specify and what the code actually does. It does not prescribe fixes.
>
> You are the doctor. Your job is to read each finding and decide the disposition:
>
> 1. **Positive pivot** (deliberate improvement, code is now the better reality) → update the design document to match the code.
> 2. **Regression / bug / incomplete work** → bring the code back to the design specification.
> 3. **Unambiguous inconsistencies are factual errors, not interpretive calls.** Fix regardless of which side you favor.
> 4. **Evaluate each finding individually** — a section may mix "update docs" and "fix code" items.
> 5. **When evidence is ambiguous, consult a human.**
> 6. **Think systemically.** A locally clean fix that weakens the system is worse than a messier fix that strengthens it.
>
> Every finding below includes a re-runnable verification command. Run it if you doubt the claim.
>
> ---
>
> ## Design-only (spec says X, code does not)
>
> ### [finding-id-1] <short title>
> - **Doc claim** — `<doc-file>:<line>` "<verbatim quote>"
> - **Expected in code** — `<code-path-searched>`
> - **Negative grep** — `grep -rn "<keywords>" <code-path>` → 0 hits
> - **Verify with:** `grep -nF "<verbatim quote>" <doc-file>` (should return the cited line)
>
> ## Code-only (code does Y, no spec for Y)
>
> ### [finding-id-2] <short title>
> - **Code behavior** — `<code-file>:<line>` (excerpt: `<one-line code extract>`)
> - **Docs checked** — `<doc-paths-searched>`
> - **Negative grep** — `grep -rn "<keywords>" <doc-path>` → 0 hits
> - **Verify with:** `sed -n '<line>p' <code-file>` (should match the excerpt)
>
> ## Unambiguous inconsistencies
> (Only include if you found factual errors with re-runnable verification: references to non-existent schema objects, same-named constructs with incompatible definitions, code paths that contradict each other. State as observations, no recommendations.)
>
> ### [finding-id-3] <short title>
> - **Observation** — <one-sentence factual statement>
> - **Evidence** — `<file1>:<line1>` "<verbatim>" vs `<file2>:<line2>` "<verbatim>"
> - **Verify with:** <commands that demonstrate the contradiction>
>
> ## Matches
> (Optional — one paragraph summarizing what was verified as cleanly aligned.)
>
> ## Verification Log
>
> Each finding above was re-verified after the initial draft. Findings that failed re-verification were dropped.
>
> | ID | Re-verification command | Outcome |
> |----|------------------------|---------|
> | finding-id-1 | `grep -nF "..." <file>` | confirmed: returns line N |
> | finding-id-2 | `sed -n 'Np' <file>` | confirmed: matches |
> | ... | ... | ... |
>
> **Dropped during self-verification:**
> - <candidate finding text> — reason: <e.g., "doc quote not found by grep">
>
> ## Overall Assessment
> <one paragraph: magnitude of *verified* drift and where it concentrates. Do not extrapolate from dropped findings.>
> ```
>
> **Step 7 — Final integrity check.** Before printing the report path, pick 3 random findings from the file you just wrote and re-run their `Verify with:` commands one more time. If any fails, fix the report (drop the finding, log the drop) and re-run the check. Only print the report path when all 3 spot-checks pass.

---

### 3. Surface the report

After the subagent finishes, show the user the report path and a one-line summary (e.g., *"4 design-only, 2 code-only, 1 unambiguous inconsistency, 11 candidates dropped during self-verification — see `docs/working/ANALYSIS-design-check-investigation.md`"*).

## Completion Criteria

Done when: (a) the report file exists, (b) every finding has a re-runnable verification command in its body, and (c) the Verification Log section lists outcomes for each finding plus any drops.

## Rules

- Do not fix code. Do not fix docs. The report is the deliverable.
- Do not pick a side. Drift is reported symmetrically.
- "Significant code behavior" excludes internal plumbing (DI wiring, logging, private helpers). Focus on behaviors a design doc *would* describe.
- **Verifiability over coverage.** Omit anything you cannot ground in a verbatim quote and a re-runnable command.
