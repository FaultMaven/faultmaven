---
description: Architectural review of recent changes in a fresh subagent context, against authoritative design docs.
---

# /review

Architectural review of recent changes against the authoritative design docs, run in a fresh subagent context free from implementation bias. Reports violations and concerns. Does NOT auto-fix.

Architectural review here means **conformance to design docs and module conventions**, not code quality (ruff/mypy handle that) and not tests (the `test-engineer` agent handles that).

## Argument

`$ARGUMENTS` — optional. If empty, review unstaged + staged changes in the current branch. If `staged`, review only staged changes. If a branch or commit reference (e.g., `main..HEAD`), review that diff range.

## Procedure (main conversation)

### 1. Capture the diff

Run `git diff <range>` (default: `HEAD`) and collect the list of changed files.

If no changes are detected, report that and stop.

### 2. Determine which skills apply

Based on the changed file paths, decide which of these skills the subagent must load, **in addition to `architecture`** which is always loaded:

- Any file under `faultmaven/modules/agent/` or `faultmaven/core/investigation/` → load `investigation-framework`
- Any file under `faultmaven/modules/knowledge/` or `faultmaven/infrastructure/knowledge/` → load `rag-architecture`
- Any file under `faultmaven/modules/preprocessing/` or `faultmaven/core/preprocessing/` → load `ingestion-pipeline`
- README, API description strings, or product-positioning copy changed → load `brand-messaging`

### 3. Spawn the subagent

Spawn a `general-purpose` subagent via the Task tool. Pass a fully self-contained prompt (the subagent inherits none of this conversation). The prompt below is the template — substitute the actual diff range and skill list.

---

**Subagent prompt template:**

> You are performing an architectural review of a FaultMaven code diff. You will not fix anything — your job is to produce a review report.
>
> **Step 1 — Load skills.** Read each of these files in order and follow their procedures:
> - `.claude/skills/architecture.md`
> - *(plus any additional skill files listed below)*
>
> Additional skills for this review: `<SKILL_LIST>`
>
> Read the authoritative design documents each skill points to. Do not skip this step.
>
> **Step 2 — Read the diff.** Run `git diff <DIFF_RANGE>` and read every changed file in full (not just the diff hunks — context matters).
>
> **Step 3 — Evaluate each changed file** against the conventions and design specified by the loaded skills and design docs. For each file, decide:
> - Violations: clear contradictions of a documented design or convention
> - Concerns: things that are not strictly violations but worth flagging (drift risk, unclear ownership, mixing read/write paths, etc.)
> - Clean: nothing notable
>
> **Step 4 — Write the report** to `docs/working/REVIEW-<short-topic>.md` with this structure:
>
> ```
> # Review: <short topic>
>
> **Diff range:** <range>
> **Skills loaded:** <list>
> **Files assessed:** <count>
>
> ## Violations
> - [path:line] <what doc/rule is violated> — <specifics>
>
> ## Concerns
> - [path:line] <what concern> — <rationale>
>
> ## Overall Assessment
> <one paragraph: ship / revise / block>
> ```
>
> Leave sections empty with "None." if nothing to report there.
>
> **Step 5 — Completion check.** You are done when every changed file has been assessed against every applicable skill. Print the report path.

---

### 4. Surface the report

After the subagent finishes, show the user the report path and a one-line summary (e.g., *"3 violations, 2 concerns — see `docs/working/REVIEW-auth-refactor.md`"*).

## Completion Criteria

Done when: (a) the report file exists, and (b) every changed file has been assessed against the relevant skill(s).

## Rules

- The review runs in the subagent, not the main conversation — no exceptions.
- Do not auto-fix. The report is the deliverable.
- If the subagent finds that the design docs themselves are unclear or contradictory, it should flag that in "Concerns" rather than picking a side.
