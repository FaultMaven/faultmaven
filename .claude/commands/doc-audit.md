---
description: Audit documentation within a domain for redundancy, contradictions, dead refs, and internal inconsistency. Docs-vs-docs only.
---

# /doc-audit

Detect documentation entropy (redundancy, contradictions, dead references, internal inconsistency) within a specific domain. **Docs-vs-docs only** — does NOT read code. Drift between docs and code is `/design-check`'s job.

## Argument

`$ARGUMENTS` — the domain name. Valid domains: `investigation`, `knowledge`, `data-processing`, `storage`, `auth`, `case`, `core-architecture`.

If missing or invalid, reject with the list of valid domains.

## Procedure (main conversation)

### 1. Look up the domain's doc scope

Read `.claude/manifest.json`. Find `domains.<name>.docs` — this is the list of paths (files or directories) in scope.

If the domain is not in the manifest, stop and tell the user.

### 2. Spawn the subagent

Spawn a `general-purpose` subagent via the Task tool with a fully self-contained prompt. This audit is read-heavy and would pollute the main conversation context.

---

**Subagent prompt template:**

> You are auditing FaultMaven documentation for internal consistency. You will not read any code. You will not fix anything. You will produce an audit report.
>
> **Scope — documents to read:** `<DOC_PATHS>` (files and all `.md` files recursively within the listed directories, plus any `README.md`).
>
> **Step 1 — Enumerate the document set.** List every `.md` file within scope. Confirm the list before reading.
>
> **Step 2 — Read every document in scope in full.** Do not skim.
>
> **Step 3 — Analyze. Look for:**
>
> - **Redundancy:** Multiple documents covering the same topic, with overlapping content. Note which document should be the single source of truth (usually the one declared canonical in `docs/architecture/README.md`, or the one the domain's README points to first).
> - **Contradictions:** Documents that state conflicting facts or conventions with each other (e.g., two docs describing the same enum with different values, two docs describing the same flow with different steps).
> - **Dead references:** Links to other docs, section anchors, or document titles that no longer exist within the documentation set. Check markdown links `[text](path)` and mentions of document titles. Do NOT verify code file paths against the codebase — that is out of scope.
> - **Internal inconsistency:** A single document whose content contradicts itself, or uses inconsistent terminology (e.g., calls the same concept by two different names without noting they are synonyms).
>
> You must NOT read code to detect staleness. Scope is strictly documentation cross-checking. If you notice a claim in a doc that "smells stale," note it only if another doc *also* in scope disagrees. Otherwise it's out of scope for this audit — use `/design-check <domain>` for doc-vs-code checks.
>
> **Step 4 — Write the report** to `docs/working/ANALYSIS-doc-audit-<DOMAIN>.md` with this structure:
>
> ```
> # Doc Audit: <domain>
>
> **Documents assessed:** <count>
> **Scope:** <doc paths>
>
> ## Redundancy
> - <topic>: <docA>, <docB> both cover this. Recommended canonical: <which>. Specifics: <what overlaps>.
>
> ## Contradictions
> - <topic>: <docA> says X; <docB> says Y. Specifics: <citations with headers or line refs>.
>
> ## Dead References
> - [<docA>] references `<target>` which does not exist in scope.
>
> ## Internal Inconsistency
> - [<docA>]: <what is inconsistent>.
>
> ## Overall Assessment
> <one paragraph>
> ```
>
> Leave sections empty with "None." if nothing to report there.
>
> **Step 5 — Completion check.** You are done when every document in scope has been read and assessed. Print the report path.

---

### 3. Surface the report

After the subagent finishes, show the user the report path and a one-line summary (e.g., *"4 redundancies, 1 contradiction — see `docs/working/ANALYSIS-doc-audit-investigation.md`"*).

## Completion Criteria

Done when: (a) the report file exists, and (b) every document in the domain scope has been read and assessed.

## Rules

- Docs-vs-docs only. Do not read code.
- Do not auto-fix, auto-delete, or auto-edit any documents.
- Use `/design-check <domain>` for docs-vs-code drift.
