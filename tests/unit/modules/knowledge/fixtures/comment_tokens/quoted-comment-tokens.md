---
id: "html-comment-tokens-in-content"
title: "Static site renders raw HTML comment markers"
domain: application
service: static-site
symptom_class: [service_unavailable]
severity: medium
scope: global
version: "1.0.0"
last_updated: "2026-08-29"
verified_by: "lane-1241"
status: draft
tags: [markdown, templating, html]
difficulty: intermediate
---

# Runbook: Static site renders raw HTML comment markers

<!-- Authoring note: this runbook exists to keep the cause grammar honest. It
     quotes HTML comment markers as CONTENT, in code spans and in a fence, so a
     parser that strips them naively corrupts real text. Do not "tidy" it. -->

## Symptom Recognition
- Pages show literal comment markers in the rendered body instead of hiding them.
- The templating log reports an unbalanced marker on the offending template.

## Applicability
Any static-site build that passes author markdown through an HTML templating
step, with read access to the template sources and the build log.

## Diagnostic Steps

### Step 1: Find templates with unbalanced markers
```bash
# A template opening a comment it never closes leaves `<!--` in the output,
# and one closing a comment it never opened leaves a bare `-->` behind.
grep -rn -e '<!--' -e '-->' templates/ | sort
```
Count the openers and closers per file; they must balance.

## Causes

### Cause A: Unclosed comment opener in a template
**Statement:** A template contains a stray `<!--` opener with no matching `-->`, so every following block is swallowed into the comment and never rendered.
**Chain:**
- root: An editor leaves `<!--` behind after deleting a closing marker
- s1: The templating step treats the remainder of the file as comment text
- D: The page renders with its trailing content missing
**Indicators:**
- root: [Step 1] the opener count exceeds the closer count for one template
- D: [Symptom] the page body ends early with no error
**Interventions:**
- **remediation** (root): Close the comment, or delete the stray opener.
  **Risk:** None. **Duration:** 1m. **Verification:** openers and closers balance.

### Cause B: Orphaned closer emitted as body text
**Statement:** A partial rendered into the page ends with a bare `-->`, which the browser prints verbatim because no comment was open.
**Indicators:**
- root: [Step 1] a template has a closer with no preceding opener
- D: [Symptom] readers see the marker in the page body
**Interventions:**
- **defensive_fix** (root): Lint templates for marker balance in CI.
  **Risk:** None. **Duration:** 15m. **Verification:** CI fails on an unbalanced file.

### Cause Z: Unidentified
**Statement:** None of the documented causes match the observed evidence.
**Indicators:**
- [Default]
**Interventions:**
- **mitigation** (D): Capture the rendered output and the template source, then
  consult an SME. **Risk:** Diagnostic only. **Duration:** Until SME review.
  **Verification:** N/A.

## Prevention
- Add a marker-balance check to the template lint step.

## Sources
- Internal incident review, 2026-08.
