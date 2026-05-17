---
description: Audit documentation within a domain for redundancy, contradictions, dead refs, and internal inconsistency. Docs-vs-docs only. Verifiable findings only.
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

## Procedure (main conversation)

### 1. Look up the domain's doc scope

Read `.claude/manifest.json`. Find `domains.<name>.docs` — this is the list of paths (files or directories) in scope.

If the domain is not in the manifest, stop and tell the user.

### 2. Spawn the subagent

Spawn a `general-purpose` subagent via the Task tool with a fully self-contained prompt that incorporates the anti-fabrication contract above. This audit is read-heavy and would pollute the main conversation context.

---

**Subagent prompt template:**

> You are auditing FaultMaven documentation for internal consistency. You will not read any code. You will not fix anything. You will produce an audit report.
>
> **Hard constraint — every finding must be re-runnable.** This audit compares text across documents. Confabulated quotes are the easiest way to fabricate a finding — the LLM "remembers" what one doc says and pattern-matches it against another, but the remembered phrasing doesn't actually exist in the doc. To prevent that, every claim about a document's content must be a *verbatim* extract with `<file>:<line>`, re-grep-able with `grep -nF`. A short verified report beats a long unverifiable one.
>
> The six hard rules:
>
> 1. **Every claim about a document's content** must be a *verbatim* extract with `<file>:<line>`. Paraphrase is forbidden.
> 2. **Every cited quote must be re-grep-able.** If `grep -nF "<phrase>" <file>` doesn't return the cited line, the finding is invalid.
> 3. **Dead-reference claims** require (a) the source-side quote (with file:line) and (b) a documented negative search showing the target doesn't exist in scope.
> 4. **Redundancy and contradiction** findings require quotes from *both* docs (with file:line each).
> 5. **Internal-inconsistency** findings require *two* verbatim quotes from the same doc.
> 6. **If you cannot verify, omit.** Do not soften, do not caveat — omit.
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
> **Step 4 — Self-verification pass (MANDATORY).** Before writing the report, take every candidate finding from Step 3 and run a second-pass verification:
>
> - For every quoted phrase: `grep -nF "<verbatim phrase>" <file>` — confirm it returns the cited line. If it returns nothing or a different line, **drop the finding**.
> - For every dead-reference target: re-run the find / grep that proves non-existence. If hits are returned, the target exists — **drop the finding**.
> - For every contradiction or redundancy: verify both quotes independently. If only one verifies, **drop the finding**.
>
> Maintain a verification log — one line per candidate finding — listing the verification commands run and their outcomes. This log goes into the report verbatim.
>
> **Step 5 — Write the report** to `docs/working/ANALYSIS-doc-audit-<DOMAIN>.md` with this structure:
>
> ```
> # Doc Audit: <domain>
>
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
> ## Redundancy
>
> ### [R-1] <short title>
> - **Overlap topic** — <one-line summary>
> - **Source A** — `<docA>:<lineA>` "<verbatim quote A>"
> - **Source B** — `<docB>:<lineB>` "<verbatim quote B>"
> - **Recommended canonical** — <which doc, and why (e.g., "declared canonical in README" or "domain READMEs points here first")>
> - **Verify with:** `grep -nF "<quote A>" <docA>` (returns line A) and `grep -nF "<quote B>" <docB>` (returns line B)
>
> ## Contradictions
>
> ### [C-1] <short title>
> - **Claim conflict** — <one-line summary>
> - **Source A** — `<docA>:<lineA>` "<verbatim quote A>"
> - **Source B** — `<docB>:<lineB>` "<verbatim quote B>"
> - **Verify with:** two grep commands above
>
> ## Dead References
>
> ### [D-1] <short title>
> - **Source-side reference** — `<docA>:<lineA>` "<verbatim quote including the link or mention>"
> - **Target** — `<path or anchor>`
> - **Non-existence proof** — `<find or grep command>` → 0 hits
> - **Verify with:** `grep -nF "<source quote>" <docA>` + the non-existence command
>
> ## Internal Inconsistency
>
> ### [II-1] <short title>
> - **Doc** — `<docA>`
> - **Quote 1** — `<docA>:<line1>` "<verbatim>"
> - **Quote 2** — `<docA>:<line2>` "<verbatim>"
> - **Why these conflict** — <one sentence>
> - **Verify with:** two grep commands above
>
> ## Verification Log
>
> | ID | Re-verification command | Outcome |
> | ---- | ---- | ---- |
> | R-1 | `grep -nF "..." <fileA>` | confirmed: returns line A |
> | R-1 | `grep -nF "..." <fileB>` | confirmed: returns line B |
> | ... | ... | ... |
>
> **Dropped during self-verification:**
> - <candidate finding text> — reason: <e.g., "quote not found by grep", "target file exists at <path>">
>
> ## Overall Assessment
>
> <one paragraph: magnitude of *verified* entropy and where it concentrates. Do not extrapolate from dropped findings.>
> ```
>
> Leave any section that has no findings with the text "None." beneath the section heading.
>
> **Step 6 — Final integrity check (MANDATORY).** Before printing the report path, pick 3 random findings from the file you just wrote and re-run their `Verify with:` commands one more time. Show the output. If any fails, fix the report (drop the finding, log the drop) and re-run the check. Only print the report path when all 3 spot-checks pass.

---

### 3. Surface the report

After the subagent finishes, show the user the report path and a one-line summary (e.g., *"4 redundancies, 1 contradiction, 0 dead refs, 2 internal inconsistencies, 3 dropped — see `docs/working/ANALYSIS-doc-audit-investigation.md`"*).

## Completion Criteria

Done when: (a) the report file exists, (b) every finding has a re-runnable verification command in its body, and (c) the Verification Log section lists outcomes for each finding plus any drops.

## Rules

- Docs-vs-docs only. Do not read code.
- Do not auto-fix, auto-delete, or auto-edit any documents.
- Use `/design-check <domain>` for docs-vs-code drift.
- **Verifiability over coverage.** Omit anything you cannot ground in a verbatim quote and a re-runnable command.
