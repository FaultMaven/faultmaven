---
name: brand-messaging
description: Triggers when modifying README positioning language, API description strings, product descriptions in config/settings, or any copy that describes FaultMaven as a product. Do NOT trigger on technical documentation, code comments, internal architecture docs, test fixtures, or runtime log messages.
---

# Skill: brand-messaging

**What this skill does:** Ensures consistent product positioning, terminology, value propositions, and audience framing when modifying brand-facing content. Also serves as the canonical source for cross-repo messaging sync (used by `/sync-brand`).

**Why this skill is different:** Unlike the other skills, this one *contains* the content rather than pointing to an external design doc. No upstream design doc governs brand messaging — the General Skill Requirement R-SK-05 explicitly permits this when a skill is itself the source of truth. Edits to this skill are the canonical way to change FaultMaven's product messaging.

---

## 1. Product Positioning

### Canonical one-line description
> **FaultMaven is an AI-powered troubleshooting copilot.**

That's the complete statement. Do not append "for SRE teams", "for DevOps engineers", "for platform teams", or any other audience qualifier. Tagline describes capability, not audience or deployment mode.

### Extended description (when more context is needed)
> FaultMaven is an AI-powered troubleshooting copilot. It correlates live telemetry with runbooks, documentation, and past fixes to deliver contextual AI-driven incident investigation.

---

## 2. Value Propositions

The four canonical value props, in priority order:

1. **Evidence-centric investigation** — logs, metrics, configs, past solutions; grounded in data the user provides or the system retrieves.
2. **Knowledge flywheel** — learns from resolved incidents; turns every resolution into reusable troubleshooting knowledge.
3. **Multi-LLM support** — 9 providers (Anthropic, OpenAI, Gemini, Fireworks, Groq, HuggingFace, Cohere, OpenRouter, Local Ollama/vLLM). Do not drop or reorder providers casually.
4. **Zero context-switching** — browser extension integrates into existing tools (the copilot, not a separate app).

---

## 3. Canonical Terminology

Use these exact terms. Do not substitute synonyms.

| Use | Not |
|---|---|
| troubleshooting copilot | AIOps tool, incident assistant, observability platform |
| investigation | debugging session, ticket, triage call |
| case | incident, ticket, issue |
| evidence | inputs, artifacts, data blobs |
| runbook | playbook, doc, KB article |
| knowledge base (KB) | vector store, document store |
| hypothesis | theory, guess, hunch |
| milestone | phase, step, stage *(reserved term — "stage" is the within-INVESTIGATING sub-level)* |
| status: **INQUIRY → INVESTIGATING → RESOLVED/CLOSED** | in-progress, open, done |
| FaultMaven API / Dashboard / Copilot / Website | the backend, the frontend, the plugin |

---

## 4. Audience Framing

FaultMaven is positioned as a **capability**, not a role-specific tool.

- ✅ "FaultMaven helps engineers resolve incidents faster by…"
- ❌ "FaultMaven is designed for SRE teams who…"
- ❌ "Built for DevOps professionals dealing with…"

When a specific audience IS relevant (e.g., a blog post), lead with the capability and qualify later: *"FaultMaven is an AI-powered troubleshooting copilot. For on-call engineers, that means…"*

---

## 5. Tone

- **Practitioner-to-practitioner**, not vendor-to-buyer.
- **Show, don't tell.** "Correlates telemetry with runbooks" > "Revolutionary AI-powered solution."
- **No superlatives** without evidence. Avoid "best-in-class", "industry-leading", "cutting-edge".
- **Concrete nouns over abstract ones.** "logs" and "metrics" over "telemetry signals" in user-facing copy (use "telemetry" sparingly, only when the collective noun is needed).

---

## 6. Canonical References

These secondary documents reinforce the above but do NOT override this skill:

- **`CLAUDE.md` §Project Overview** — Product description and key value propositions. Mirrors this skill; if they diverge, this skill is canonical and CLAUDE.md should be updated.
- **`README.md`** (repo root) — Must conform to this skill for any positioning language.

---

## Scope Boundaries

**This skill governs:**
- Product positioning language (one-liners, elevator pitches, README opening)
- Canonical terminology for product concepts
- Value proposition framing
- Audience framing (capability-first, not role-first)

**This skill does NOT govern:**
- UI copy, button labels, tooltip text — product design concern, not messaging
- Marketing page copy, ads, SEO meta descriptions — specific marketing statements are out of scope
- Visual design, logos, color
- Technical documentation (architecture, API reference, runbooks)
- Code comments, log messages, error messages shown to developers
- Internal design docs, ADRs, specs

---

## Cross-Repo Canonicality

This file in the `faultmaven` API repo is **canonical**. Copies in downstream repos (`faultmaven-copilot`, `faultmaven-dashboard`, `faultmaven-website`) must be treated as downstream. Use `/sync-brand` to diff this canonical version against those copies and produce a propagation checklist.
