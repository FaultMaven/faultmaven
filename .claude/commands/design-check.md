---
description: Detect drift between design docs and implementation for a domain, in both directions. Does not pick a side.
---

# /design-check

Detect drift between design documents and implementation within a specific domain, in **both directions**:
- **Aspirational drift:** what the design specifies that the code does not implement
- **Undocumented evolution:** what the code does that the design does not describe

Does NOT decide which side is "correct." Reports both; user decides.

## Argument

`$ARGUMENTS` — the domain name. Valid domains: `investigation`, `knowledge`, `data-processing`, `storage`, `auth`, `case`, `core-architecture`.

If missing or invalid, reject with the list of valid domains.

## Procedure (main conversation)

### 1. Look up the domain's scope

Read `.claude/manifest.json`. Find `domains.<name>`:
- `docs` — list of doc paths (files or directories)
- `code` — list of code paths (directories or files)

If the domain is not in the manifest, stop and tell the user.

### 2. Spawn the subagent

Spawn a `general-purpose` subagent via the Task tool with a fully self-contained prompt. This check reads both docs and code for the full domain and would pollute the main conversation context.

---

**Subagent prompt template:**

> You are performing a design-vs-code drift check for FaultMaven. You will read both design documents and code. You will not fix anything. You will produce a drift report.
>
> **Scope:**
> - Design docs: `<DOC_PATHS>`
> - Code: `<CODE_PATHS>`
>
> **Step 1 — Enumerate.** List every `.md` file in the doc scope and every `.py` file in the code scope. Confirm the list before reading.
>
> **Step 2 — Read the design docs in full.** Extract every specified behavior, rule, data structure, flow, constraint, enum value, and invariant into an internal checklist. Be granular: each numbered rule, each named data field, each described transition is a separate item.
>
> **Step 3 — Read the code.** For each item on the design checklist, locate the implementation in the code:
> - **Present and matching** → OK
> - **Present but diverges** → undocumented evolution (the code does something the design does not describe)
> - **Absent** → aspirational drift (the design specifies it, code does not implement it)
>
> Then scan the code for behaviors, classes, methods, flags, or flows that are not mentioned anywhere in the design docs. Those are also **undocumented evolution**.
>
> **Step 4 — Do NOT judge which side is correct.** Your job is to report the drift in both directions, with evidence, and let the user decide. A design element that appears "outdated" and a code behavior that appears "experimental" are treated the same way — both are reported.
>
> **Step 5 — Write the report** to `docs/working/ANALYSIS-design-check-<DOMAIN>.md` with this structure:
>
> ```
> # Design Check: <domain>
>
> **Docs in scope:** <list>
> **Code in scope:** <list>
> **Design elements traced:** <count>
> **Code behaviors traced:** <count>
>
> ## Aspirational Drift (design → code)
> Design specifies X but code does not implement it.
>
> - **[<doc>:<section>]** <specification>
>   → Expected in: <code path>
>   → Observed: not present / partial / diverges in <way>
>
> ## Undocumented Evolution (code → design)
> Code does X but no design doc describes it.
>
> - **[<code path>:<line>]** <behavior>
>   → Not mentioned in: <docs checked>
>
> ## Matches
> (Optional — summarize cleanly matched areas in one paragraph so the user can see coverage.)
>
> ## Overall Assessment
> <one paragraph: magnitude of drift, whether it's concentrated in one area, whether docs or code is evolving faster>
> ```
>
> **Step 6 — Completion check.** You are done when every design element has been traced to code (or marked as missing) and every significant code behavior has been traced to a design element (or marked as undocumented). Print the report path.

---

### 3. Surface the report

After the subagent finishes, show the user the report path and a one-line summary (e.g., *"6 aspirational drifts, 9 undocumented behaviors — see `docs/working/ANALYSIS-design-check-investigation.md`"*).

## Completion Criteria

Done when: (a) the report file exists, and (b) every design element has been traced to code and every significant code behavior has been traced to a design element.

## Rules

- Do not fix code. Do not fix docs. The report is the deliverable.
- Do not pick a side. Drift is reported symmetrically.
- "Significant code behavior" excludes internal plumbing (DI wiring, logging, private helpers). Focus on behaviors a design doc *would* describe.
