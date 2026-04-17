---
description: Audit brand-facing content in this repo against the canonical brand-messaging skill and produce a propagation checklist for downstream repos.
---

# /sync-brand

Audit brand-facing content in the `faultmaven` API repo against the canonical brand/messaging skill, and produce a propagation checklist for downstream repos (`faultmaven-copilot`, `faultmaven-dashboard`, `faultmaven-website`).

**Scope:** product messaging and positioning only. Not UI copy, not marketing ads, not visual design.

## Argument

None required. Optional: `$ARGUMENTS` can be a specific repo name to include in the propagation checklist if you want to scope the downstream list narrower (e.g., `website` to only list that repo).

## Procedure

### 1. Read the canonical skill

Read `.claude/skills/brand-messaging.md` in full. This is the canonical source for product messaging in FaultMaven. Claude Code operates in one repo at a time, so the copy in this repo is canonical; copies in downstream repos are downstream.

### 2. Collect brand-facing content in this repo

Review these locations for language that describes FaultMaven as a product:

- `README.md` (repo root) — especially opening paragraphs, feature bullets, value props
- `CLAUDE.md` §Project Overview — should mirror the skill
- `faultmaven/main.py` or wherever the FastAPI app is instantiated — `title=`, `description=`, `version=` strings on `FastAPI(...)`
- `faultmaven/config/settings.py` — any product-description fields
- `pyproject.toml` — `description` field
- Top-level docs that open with product positioning: `docs/README.md`, `docs/getting-started/` intro pages

Skip: architecture docs, API reference, test files, log messages, code comments, migration docstrings. These are technical, not brand-facing.

### 3. Compare against the canonical skill

For each piece of brand-facing content found, check it against the skill's §1 Positioning, §2 Value Propositions, §3 Terminology, and §4 Audience Framing.

List inconsistencies in one of these categories:
- **Drift from canonical tagline** (e.g., an audience qualifier added)
- **Terminology substitution** (e.g., "incident" used where the skill says "case")
- **Value prop reordering or omission**
- **Audience framing violation** (role-first instead of capability-first)

### 4. Produce the propagation checklist

Identify what has changed in the canonical skill (git log on `.claude/skills/brand-messaging.md` for recent commits, plus anything the user mentions as recently edited). For each substantive change, describe it **in messaging terms**, not file-level diff terms.

Good:
- *"Value proposition #1 was rephrased from 'evidence-first' to 'evidence-centric'. Downstream repos should search for 'evidence-first' and update."*
- *"Terminology: 'playbook' is no longer accepted; use 'runbook' everywhere."*

Bad:
- *"Line 37 changed from X to Y."* (File-level — not portable across repos.)

Then list the downstream repos and which of them are likely to carry the changed messaging (README, product description, landing page copy, extension popup copy, etc.). Downstream repos implement the changes their own way — this command does not modify them.

### 5. Output

Write the report to `docs/working/SYNC-BRAND-<YYYY-MM-DD>.md` with this structure:

```
# Brand Sync — <date>

## Canonical Skill
`.claude/skills/brand-messaging.md` (commit: <short-sha>)

## Inconsistencies in This Repo
- [<file>]: <what> — canonical says <X>, file says <Y>
- (or "None.")

## Changes to Propagate
### <change 1, phrased in messaging terms>
- Rationale: <if known>
- Downstream search cue: <string to grep for in downstream repos>

### <change 2>
...

## Downstream Repos — Propagation Checklist
- [ ] `faultmaven-copilot` — likely touches: <e.g., extension popup positioning, README>
- [ ] `faultmaven-dashboard` — likely touches: <e.g., landing page, empty states with product pitch>
- [ ] `faultmaven-website` — likely touches: <e.g., homepage, about page, meta tags>

Each downstream repo implements the changes in its own way. To sync, open that repo and replay this checklist.
```

Print the report path and a one-line summary.

## Completion Criteria

Done when: (a) the report file exists, (b) any inconsistencies in this repo are listed, and (c) the propagation checklist is written in messaging terms (not file diffs).

## Rules

- Never modify files in downstream repos. Claude Code operates in one repo at a time.
- Never invent messaging changes the user did not request — report only what the canonical skill currently says versus what the repo's content says.
- If you find inconsistencies in *this* repo that the user wants fixed, that is a follow-up edit, not part of `/sync-brand`'s output. Offer to do it, but do not do it silently.
