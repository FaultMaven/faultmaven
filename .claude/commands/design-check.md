---
description: Detect drift between design docs and implementation for a domain, in both directions. Does not pick a side.
---

# /design-check

Detect drift between design documents and implementation within a specific domain, in **both directions**:
- **Design-only:** the design specifies something the code does not implement
- **Code-only:** the code does something no design doc describes

Does NOT decide which side is "correct" or whether a divergence is a positive adaptation vs. a regression — a separate triage pass makes that call. This skill's job is objective, accurate identification of differences. Call out unambiguous bugs, errors, or internal inconsistencies (e.g., a repository targeting a table that does not exist, two enums with the same name and different value spaces) — those are factual findings, not judgments.

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
> - **Present but diverges** → code-only (the code does something the design does not describe; record both what the design said and what the code does)
> - **Absent** → design-only (the design specifies it, code does not implement it)
>
> Then scan the code for behaviors, classes, methods, flags, or flows that are not mentioned anywhere in the design docs. Those are also **code-only**.
>
> **Step 4 — Stay objective; do not judge adaptive vs. regression.** Report divergences in both directions with evidence. A separate triage pass decides whether each item should be resolved by updating the design or by changing the code — that is not your call. However, call out unambiguous bugs, errors, or internal inconsistencies as facts when they are clear from the evidence (e.g., code referencing a schema object that does not exist anywhere, two same-named enums with incompatible value spaces, a storage layout that contradicts itself across files). State them as observations, not recommendations.
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
> ## Note to the agent acting on this report
>
> **Think of this report as a CT scan, not a treatment plan.** It lists objective differences between what the design documents specify and what the code actually does. It does not prescribe fixes.
>
> You are the doctor. Your job is to read each finding and decide the disposition. For every item:
>
> 1. **Ask: is this a positive pivot or a regression?**
>    - **Positive pivot** (deliberate improvement, code is now the better reality) → **update the design document to match the code.** The code becomes the new source of truth.
>    - **Regression / bug / incomplete work** (accidental divergence, unfinished migration, missing implementation) → **bring the code back to the design specification.** The design remains the source of truth.
> 2. **Unambiguous inconsistencies are factual errors, not interpretive calls.** They must be fixed regardless of which side you favor — the system is currently self-contradictory (e.g., code queries a table that does not exist, two same-named enums have incompatible values). Address them directly.
> 3. **Evaluate each finding individually.** A single section may contain a mix of "update docs" and "fix code" items. Do not batch-decide by section heading.
> 4. **When the evidence is ambiguous, consult a human stakeholder before acting.** If you cannot tell from code and docs alone whether a divergence was deliberate, ask.
> 5. **Think systemically and holistically.** For each gap, do not optimize only for the local file or component. Decide which way to resolve it based on overall system robustness, performance, maintainability, and coherence — including effects on neighboring modules, data flows, operational cost, and long-term evolvability. A "locally clean" fix that weakens the system is worse than a messier fix that strengthens it.
>
> The radiologist (this skill) has reported what it sees. You decide which findings are benign variants, which need surgery, and which need medication.
>
> ---
>
> ## Design-only (spec says X, code does not)
> Design specifies X but code does not implement it.
>
> - **[<doc>:<section>]** <specification>
>   → Expected in: <code path>
>   → Observed: not present / partial / diverges in <way>
>
> ## Code-only (code does Y, no spec for Y)
> Code does X but no design doc describes it.
>
> - **[<code path>:<line>]** <behavior>
>   → Not mentioned in: <docs checked>
>
> ## Unambiguous inconsistencies
> (Optional — only include if you found factual errors: references to non-existent schema objects, same-named constructs with incompatible definitions, code paths that contradict each other. State as observations, no recommendations.)
>
> ## Matches
> (Optional — summarize cleanly matched areas in one paragraph so the user can see coverage.)
>
> ## Overall Assessment
> <one paragraph: magnitude of drift and where it concentrates. Do not speculate on whether docs or code is "correct" — the triage pass will decide.>
> ```
>
> **Step 6 — Completion check.** You are done when every design element has been traced to code (or marked as missing) and every significant code behavior has been traced to a design element (or marked as undocumented). Print the report path.

---

### 3. Surface the report

After the subagent finishes, show the user the report path and a one-line summary (e.g., *"6 design-only, 9 code-only, 1 unambiguous inconsistency — see `docs/working/ANALYSIS-design-check-investigation.md`"*).

## Completion Criteria

Done when: (a) the report file exists, and (b) every design element has been traced to code and every significant code behavior has been traced to a design element.

## Rules

- Do not fix code. Do not fix docs. The report is the deliverable.
- Do not pick a side. Drift is reported symmetrically.
- "Significant code behavior" excludes internal plumbing (DI wiring, logging, private helpers). Focus on behaviors a design doc *would* describe.
