# Brand Sync — 2026-07-23

## Canonical Skill
`.claude/skills/brand-messaging.md` (commit: `4ce6e187` — "docs: adopt seasoned-engineer positioning (four traits) in brand messaging (#664)")

Repo audited: `faultmaven` (API repo). This repo holds the canonical skill; downstream copies are derivatives.

## Inconsistencies in This Repo

**None.**

Brand-facing surfaces checked, all conformant:

- **`README.md`** — Hero subtitle "AI Troubleshooting Copilot for Modern Engineering." uses Title Case in an H1/bold-hero context (allowed per §3 hero exception) and the broad "for Modern Engineering" scope (allowed per §4, not role-narrowing). Body prose immediately restates the canonical lowercase "AI-powered troubleshooting copilot." The four traits appear with correct names and order (Goal-driven → Methodical → Evidence-based → Self-learning). Tagline "Built to solve, not to chat" is an approved §1 tagline. The "predictive AIOps platforms act like actuaries" line references the category by **contrast** — a legitimate §7 usage, not a self-label. License badge + prose say FSL-1.1-ALv2 / fair-source, never "open source." No banned deployment terms (no Community/Enterprise Edition, no microservices, no "local deployment").
- **`CLAUDE.md` §Project Overview** — Mirrors the skill: canonical one-liner + seasoned-engineer hook line, and the four value props in §2 priority order (Evidence-centric investigation → Knowledge flywheel → Multi-LLM support with the full 9-provider list → Zero context-switching). Component table uses the qualified proper nouns (FaultMaven API / Dashboard / Copilot).
- **`faultmaven/main.py`** — `FastAPI(title="FaultMaven API", description="AI-powered troubleshooting copilot for Engineers, SREs, and DevOps professionals", version="1.0.0")`. The description is the exact inclusive-enumeration form §4 approves (names everyone, narrows to no one); title uses the canonical component name. (The `FastAPI(title='FaultMaven API')` at line ~15 is illustrative code inside the module docstring, not a live app instance — still consistent.)
- **`pyproject.toml`** — `description = "AI-powered troubleshooting copilot for incident investigation"`. Canonical lowercase product-class noun; "for incident investigation" is a use-case qualifier, not a role narrowing.
- **`faultmaven/config/settings.py`** — No product-positioning fields; every `description=` is a technical config-field doc (out of scope).

## Changes to Propagate

These are the substantive messaging changes in the canonical skill's recent history, phrased in messaging terms. This repo (README/CLAUDE/main.py/pyproject) has already absorbed all four; the checklist below is for the downstream repos.

### 1. Seasoned-engineer positioning — the four product-character traits (newest, #664)
Canonical §1 now carries a product-character hook — **"FaultMaven works a problem the way a seasoned engineer does — and never forgets what it learns"** — expanded into four ordered traits: **Goal-driven, Methodical, Evidence-based, Self-learning**. Use wherever a surface explains *how* FaultMaven behaves (hero, About blurbs, README opening). Keep trait names and order; each surface phrases the glosses in its own voice. Traits complement (do not replace) the §2 value props. Three approved taglines added: *"Built to solve, not to chat."* / *"Methodical. Evidence-based. Better every time."* / *"Troubleshoots like an engineer. Learns like a team."*
- Rationale: differentiate from "chatbot" framing; give every surface one coherent character story.
- Downstream search cue: grep for `seasoned engineer`, `Goal-driven`, `Built to solve`; also look for hero/About copy that still describes behavior without the trait vocabulary.
- §7 grep loop: **no new banned term** — this change *adds* positioning, it does not retire a term. No §7 pattern addition required.

### 2. Naming-architecture scope discipline (#473)
"FaultMaven Copilot" names the **browser extension only** — never the whole product (the product is bare "FaultMaven"; use apposition "FaultMaven — your AI troubleshooting copilot" for copilot framing on the product). Qualify the backend as **"FaultMaven API"**, not bare "FaultMaven," when distinguishing the server. Lowercase `copilot` = the category/identity; Title-Case `Copilot` = the extension component.
- Downstream search cue: grep for `FaultMaven Copilot` used to mean the whole system; bare "FaultMaven" where the API server is meant.
- §7 grep loop: intentionally **not grep-enforceable** ("FaultMaven Copilot" is a valid string for the extension) — review/`/sync-brand` judgment only. No §7 change.

### 3. Fair-source / FSL licensing + unified-codebase tier names (#464)
The backend/product is **fair-source — FSL-1.1-ALv2 (source-available, converts to Apache-2.0 two years after each release)** — never "open source" as a descriptor of the backend or product. (Third-party "open-source models" via HuggingFace, and the permissively-licensed frontends, may still use the term.) The two deployments are **Standalone / Cloud**; **Community Edition / Enterprise Edition (CE/EE) are retired**. One unified codebase, open core in **both** deployments — never label a tier "Open Source" as if Cloud were proprietary.
- Downstream search cue: grep for `open source` / `open-source` describing the backend or product; `Community Edition`, `Enterprise Edition`, `CE/EE`.
- §7 grep loop: **already covered** — §7's universal class lists `Community Edition` / `Enterprise Edition` / `CE/EE` and `Enterprise SaaS`; "open source" describing the backend/product is deliberately **human-judgment only** (substring would false-positive on legit uses). No §7 change needed.

### 4. ADR-004 deployment lexicon (#457)
`standalone` / `cloud` name the **deployment architecture**; `self-hosted` / `FaultMaven-hosted` name the **operator**. Architecture is **modular monolith**, never "microservices." "cloud" describes cloud-native architecture, not location (a cloud deployment can run on-prem as a private cloud — `cloud` and `on-prem` are not opposites). `local` is reserved for `AUTH_MODE=local` / `CHAT_PROVIDER=local`, never a deployment term.
- Downstream search cue: grep for `microservices`, `on-prem edition`, `local deployment`, `Deploy locally` (as a deployment-mode label), `self-managed edition`.
- §7 grep loop: **already covered** — §7's universal class lists `microservices backend`, `Local Deployment`, `Deploy locally`. No §7 change needed.

## Downstream Repos — Propagation Checklist

Each downstream repo implements these in its own voice — propagate the terminology and positioning, never the copy. To sync, open that repo and replay this checklist against its brand-facing surfaces. Terminology (§3, incl. deployment lexicon) is universal and applies to marketing surfaces too; positioning/value/audience/tone (§1/§2/§4/§5) are checked on marketing surfaces only for outright contradiction.

- [ ] `faultmaven-copilot` — likely touches: extension popup / onboarding positioning copy, `README.md` opening, store listing / `manifest.json` description. Verify: four-traits hook echoed in its own voice (#1); "FaultMaven Copilot" used only for the extension itself and product framing via apposition (#2); fair-source/FSL wording, no "open source" for the backend (#3); no microservices/deployment-term drift (#4).
- [ ] `faultmaven-dashboard` — likely touches: `README.md`, any landing/marketing blurb, empty-state or About copy carrying a product pitch, `package.json` description. Verify: trait vocabulary in About/empty-state pitch (#1); "FaultMaven Dashboard" as the qualified component name, "FaultMaven" (not "FaultMaven Copilot") for the whole product (#2); tier names Standalone/Cloud, fair-source licensing (#3); deployment lexicon (#4).
- [ ] `faultmaven-website` — likely touches: homepage/hero, About page, `<meta name="description">` and other SEO meta, pricing/editions page. **Marketing surface**: enforce §3 terminology universally (fair-source not "open source" for the product; Standalone/Cloud not CE/EE; modular monolith not microservices; correct copilot/Copilot casing) and check §1/§2/§4 only for contradiction — the site owns its pitch, but the four-traits character and canonical positioning should read as one coherent story with the repos.

Each downstream repo implements the changes in its own way. This command does not modify them.
