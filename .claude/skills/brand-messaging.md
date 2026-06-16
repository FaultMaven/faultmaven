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

That's the complete bare statement. The rule for audience qualifiers is: do not append qualifiers that *narrow* the audience to a single role — "for SRE teams", "for DevOps engineers", "for platform teams". Inclusive framings that describe a broad audience or use case are fine: "for Engineers, SREs, and DevOps professionals", "for modern engineering teams", "for technical troubleshooting". The rule is against shrinking the perceived audience, not against naming it.

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

### Deployment terminology (per ADR-004)

| Use | Not |
|---|---|
| standalone *(deployment architecture)* | local deployment, on-prem edition, self-managed edition |
| cloud *(deployment architecture)* | SaaS-only / hosted *as an architecture name* |
| self-hosted ↔ FaultMaven-hosted *(operator)* | — |
| modular monolith | microservices, microservice backend |
| open core *(applies to both deployments)* | "Open Source" as a tier name implying Cloud isn't open |
| source-available / fair-source *(the backend license: FSL-1.1-ALv2)* | "open source" / "open-source" as a descriptor of the backend or the product |
| Standalone / Cloud *(the two deployments)* | Community Edition / Enterprise Edition (CE/EE) — retired tier names |

- `standalone`/`cloud` name the **deployment architecture**; `self-hosted`/`FaultMaven-hosted` name the **operator** (who runs it). These are independent axes.
- **"cloud" describes the architecture (cloud-native), not the location** — a cloud deployment can run in public cloud *or* on-prem as a private cloud. `cloud` and `on-prem` are not opposites.
- `local` is reserved for `AUTH_MODE=local` / `CHAT_PROVIDER=local` — **never** a deployment term.
- The Core is **open core in both deployments.** Do not label a tier "Open Source" as if Cloud were proprietary; name a tier by its architecture (Standalone) or operator (Self-Hosted).
- The backend/engine is **fair-source — FSL-1.1-ALv2 (source-available, converts to Apache-2.0 two years after each release)**, *not* OSI "open source." Describe the **backend or the product** as source-available or fair-source, never "open source." The **frontends stay permissively licensed (MIT/Apache)** and may be called open-source individually; third-party "open-source models" (e.g. via HuggingFace) keep the term too. The word is only wrong when it describes FaultMaven's backend/product/licensing.
- There is **one unified codebase** — no Community/Enterprise edition split and no core/cloud fork. Standalone and Cloud are the **same core**, differing only by configuration and composed modules; multi-tenancy lives in the core schema, and proprietary cloud-only concerns (billing, usage metering, hosted IAM/admin) are **composed modules outside the open core**, never feature flags in the open tree.

### Capitalization

The rule of thumb: lowercase as a common noun, Title Case only as a proper product/component name or a literal UI label.

| Form | Use when |
|---|---|
| `FaultMaven` (Title Case) | Always — the product name in prose, copy, headlines, UI |
| `faultmaven` (lowercase) | Repository names, directory names, package names, URL paths, code identifiers |
| `copilot` (lowercase) | Product-class noun in prose — "FaultMaven is a troubleshooting copilot" |
| `Copilot` (Title Case) | Proper product-component name — "FaultMaven Copilot", "the Copilot extension" |
| `runbook` (lowercase) | The concept — "the runbook for disk-full incidents", "ingest your runbooks" |
| `Runbooks` (Title Case) | Literal UI label / navigation tab in the Dashboard |
| `case` (lowercase) | The concept — "FaultMaven creates a case for each investigation" |
| `Cases` (Title Case) | Literal UI label / navigation tab in the Dashboard |
| `evidence` (lowercase) | Always — uncountable noun, no Title Case form |
| `hypothesis` (lowercase) | The concept |
| `Hypotheses` (Title Case) | Literal UI label / tab in the case detail view |

**Exception — H1 / bold marketing headlines.** Title Case for the canonical product-class noun is acceptable in H1 / hero / bold-tagline contexts (e.g., a README's bold subtitle, a landing-page hero `<h1>`). "The AI-Powered Troubleshooting Copilot" as a hero headline is fine. This is a near-universal convention in landing/README copy and matches the existing `faultmaven` README. The lowercase rule still applies in body prose.

This rule governs how to capitalize *the canonical terms listed in this skill*. It does not claim authority over UI copy more broadly (see Scope Boundaries).

---

## 4. Audience Framing

FaultMaven is positioned as a **capability**, not a role-specific tool. The rule is against *narrowing* the perceived audience to a single role — not against naming a broad audience.

- ✅ "FaultMaven helps engineers resolve incidents faster by…"
- ✅ "AI-powered troubleshooting copilot for Engineers, SREs, and DevOps professionals" (inclusive enumeration — names everyone who'd use it)
- ✅ "AI-powered troubleshooting copilot for modern engineering" (broad scope, not role-specific)
- ❌ "FaultMaven is designed for SRE teams who…" (narrows to one role)
- ❌ "Built for DevOps professionals dealing with…" (narrows to one role)

When a specific audience IS relevant (e.g., a blog post targeting one role), lead with the capability and qualify later: *"FaultMaven is an AI-powered troubleshooting copilot. For on-call engineers, that means…"*

---

## 5. Tone

- **Practitioner-to-practitioner**, not vendor-to-buyer.
- **Show, don't tell.** "Correlates telemetry with runbooks" > "Revolutionary AI-powered solution."
- **No superlatives** without evidence. Avoid "best-in-class", "industry-leading", "cutting-edge".
- **Concrete nouns over abstract ones.** "logs" and "metrics" over "telemetry signals" in user-facing copy (use "telemetry" sparingly, only when the collective noun is needed).
- **Precise verbs over corporate jargon.** Prefer plain mechanical verbs that name what the system actually does: `uses`, `reads`, `writes`, `queries`, `correlates`, `ingests`, `matches`, `links`, `retrieves`. Avoid `leverages`, `utilizes`, `empowers`, `harnesses`, `unlocks`, `drives` — they obscure rather than describe. If "use" feels too plain, that's usually a sign the sentence needs a more specific verb (e.g., "queries the KB"), not a fancier one.

---

## 6. Canonical References

These secondary documents reinforce the above but do NOT override this skill:

- **`CLAUDE.md` §Project Overview** — Product description and key value propositions. Mirrors this skill; if they diverge, this skill is canonical and CLAUDE.md should be updated.
- **`README.md`** (repo root) — Must conform to this skill for any positioning language.

---

## 7. Enforcement (Future)

The terminology, capitalization, audience, and verb rules above are currently enforced manually via `/sync-brand`. The grep-style search cues throughout this skill (and in the `/sync-brand` propagation checklist) are written so they can graduate into automated checks:

- **Pre-commit hook** — a regex scan over staged `.md`, `.html`, `.tsx`, `package.json`, and `manifest.json` files that fails the commit on a violation. Two pattern classes, matching "Authority by rule type":
  - **Universal (terminology — runs on ALL brand-facing files, including marketing/website):** `troubleshooting assistant`, `microservices backend`, `Local Deployment`, `Deploy locally` (deployment context), `Enterprise SaaS` (as the Cloud tier name), `Community Edition` / `Enterprise Edition` / `CE/EE` (retired tier names — there is one unified codebase, use Standalone/Cloud; *grep wiring in `brand_lint.py` deferred until the `[enterprise]`→`[cloud]` extras rename clears the remaining `pyproject.toml` references*), `faultmaven-deploy` / `fm-*-service` (obsolete repos).
  - **Core surfaces only (positioning/audience/tone — README + product descriptions, NOT marketing copy):** `for SRE teams`, `designed for DevOps`, `leverages`, `utilizes`, etc.
  - **NOT grep-enforced (human / `/sync-brand` judgment only):** category terms like *AIOps platform*, *observability platform*, and *playbook*; **and `open source` / `open-source` describing the backend or product** (now fair-source/FSL — see §3). FaultMaven legitimately references the category terms by **contrast** ("not the predictive AIOps platform"), and "open source" legitimately appears for third-party models ("HuggingFace open-source models") and the permissively-licensed frontends — so a substring grep would false-positive. The §3 rules still apply — don't *label FaultMaven* a category platform, and don't call the *backend/product* "open source" — but enforce both by review, not regex.
- **CI lint job** — same two pattern classes, run against brand-facing files in PRs (a `.github/workflows/brand-lint.yml`-style workflow that greps and posts a comment with offending lines). The universal class runs on marketing files too; the core-surfaces class does not.
- **Search-cue maintenance** — when a violation pattern is added or retired in this skill, update the corresponding hook/CI rule in the same PR. The skill remains the source of truth; the automation is a downstream check, exactly as `/sync-brand` treats this skill as canonical and downstream-repo copies as derivatives.

Goal: prevent a contributor from ever successfully committing `<meta name="description" content="AIOps platform">` in the first place, rather than catching it in a quarterly audit.

---

## Scope Boundaries

### Authority by rule type

The skill's authority is **two-tiered**, because the surfaces differ:

- **Terminology (§3, including the deployment lexicon) is universal.** Canonical terms and the deployment lexicon apply to **every** brand-facing surface — README, product descriptions, **and** marketing copy, website pages, SEO meta. A factual term cannot differ across surfaces (you cannot say "microservices" on the website and "modular monolith" in the README). Retired terms are retired everywhere.
- **Positioning, value props, audience framing, tone, and specific claims (§1, §2, §4, §5) stop at the marketing boundary.** The website and other marketing surfaces own their own pitch, headlines, and claims; the skill does not dictate them. It requires only that they **not contradict** the canonical positioning.

**This skill governs:**
- Product positioning language (one-liners, elevator pitches, README opening)
- Canonical terminology for product concepts (universal — see above)
- Value proposition framing
- Audience framing (capability-first, not role-first)

**This skill does NOT govern:**
- UI copy, button labels, tooltip text — product design concern, not messaging
- Marketing **positioning, claims, ads, and SEO pitch** — the specific marketing message is the website's to own (but its **terminology** must still conform to §3, per "Authority by rule type")
- Visual design, logos, color
- Technical documentation (architecture, API reference, runbooks)
- Code comments, log messages, error messages shown to developers
- Internal design docs, ADRs, specs

---

## Cross-Repo Canonicality

This file in the `faultmaven` API repo is **canonical**. Copies in downstream repos (`faultmaven-copilot`, `faultmaven-dashboard`, `faultmaven-website`) must be treated as downstream. Use `/sync-brand` to diff this canonical version against those copies and produce a propagation checklist.

### Messages are not duplicated — they are consistent, aligned, and corroborative

Public surfaces do **not** repeat the same copy. Each says what fits its job — the API README is setup-and-architecture, the Copilot README is the in-browser extension, the Dashboard README is KB/case management, the website is the marketing pitch. The cross-surface requirement is:

- **Consistent** — no surface contradicts another. Same facts everywhere: modular monolith, standalone/cloud, open core in **both** deployments.
- **Aligned** — same canonical terminology (§3) and the same underlying positioning (§1), expressed in each surface's own voice.
- **Corroborative** — surfaces reinforce and cross-reference each other (the website links to the repo Quick Start; the Copilot README points to the API for the backend), so a reader moving between them gets **one coherent story**, not three overlapping ones.

`/sync-brand` propagates **terminology and positioning**, never copy. It must never make two repos say the same sentence.
