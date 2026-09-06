# Brand Sync — 2026-08-21

## Canonical Skill

`.claude/skills/brand-messaging.md` (last substantive commit before this sync: `1a019d84f`)

Scope per `/sync-brand`: terminology (§3, incl. the deployment lexicon) is
**universal** — it binds marketing surfaces too; positioning, value props,
audience framing and tone (§1/§2/§4/§5) stop at the marketing boundary and are
checked there only for *contradiction*.

## Inconsistencies in This Repo

- [`scripts/brand_lint.py`]: the module docstring called this repo
  "faultmaven CE" — **CE is a retired tier name** (§3 deployment lexicon: one
  unified codebase, Standalone/Cloud). Fixed to "the FaultMaven API".
- [`.claude/skills/brand-messaging.md` §7]: titled **"Enforcement (Future)"**
  and written in the conditional ("can graduate into automated checks",
  "a `.github/workflows/brand-lint.yml`-style workflow"). The automation
  shipped — `scripts/brand_lint.py` + a `brand-lint` workflow here, and
  `scripts/brand-lint.mjs` + a workflow in each of the three frontend repos.
  Rewritten in the present tense to describe what runs.
- [`.claude/skills/brand-messaging.md` §7]: the universal pattern list did
  **not** carry `live telemetry`, even though #821 retired the claim from
  canonical positioning. §7's own rule ("when a violation pattern is added or
  retired, update the hook/CI rule in the same PR") was not applied, so the
  retirement never reached any automated check and drifted downstream for a
  month. Pattern added here and in all four lint scripts.

No positioning, value-prop, audience or terminology drift found in
`README.md`, `CLAUDE.md`, `pyproject.toml`, `main.py`'s `FastAPI(...)`
description, or `docs/README.md`.

## Changes to Propagate

### 1. "live telemetry" is a **retired overclaim**, not a style preference

#821 removed it from the canonical extended description because FaultMaven has
no reach into production — no agents, no credentials — and the claim
contradicted the security posture stated everywhere else. It works from what
you paste, upload, or capture. Canonical replacement: *"the logs, metrics, and
configs you share"*.

- Rationale: an inaccurate capability claim on a public surface, and the
  strongest one a Chrome Web Store or prospective-customer reader would test.
- Downstream search cue: `live telemetry`, and any bare `telemetry` listed
  as a *grounding source* alongside runbooks and past fixes.
- Now grep-enforced: `\blive telemetry\b`, **universal** class (it is a
  factual claim, so it binds marketing surfaces too).

### 2. Retired tier names were missing from every downstream lint copy

`Community Edition` / `Enterprise Edition` are in the canonical `brand_lint.py`
but were absent from all three `brand-lint.mjs` copies, so a retired tier name
would have passed CI on the website, the Dashboard and the Copilot. The
`fm-*-service` pattern had also drifted (missing the `(?!-)` lookahead that
keeps `fm-provision-service-account` from matching).

- Downstream search cue: diff your `UNIVERSAL` array against
  `faultmaven/scripts/brand_lint.py`.

### 3. The store-facing product description was outside every lint's reach

The Copilot's `package.json` `description` and `public/_locales/en/messages.json`
carry the text Chrome and Firefox publish as the extension's description; both
lints scanned `README.md` only. Scope widened.

### 4. Terminology: the product class is **copilot**, never **assistant**

`troubleshooting assistant` is already a universal grep pattern, but it only
runs where a lint is wired. It survived on the website's published extension
privacy policy ("Render the assistant in the browser side panel") because that
file is `.tsx` under `src/` — in scope for the website lint, but the phrasing
was bare "assistant", which no pattern matches.

- Downstream search cue: `\bassistant\b` referring to FaultMaven itself.
  Slack's own API surface names (`assistant:write`, "Assistant container")
  are legitimate and must not be rewritten.

### 5. "Deep Linking … references your Knowledge Base articles" does not ship

The Copilot README promised inline KB citations. `item.sources` is read in
`ChatWindow.tsx` but never assigned anywhere in the extension, and the backend
turn response carries no sources field — so `SourceCitation` and
`injectSourceCitations` are unreachable code. What does ship is a case-header
badge that deep-links to the matching report/runbook in the Dashboard. README
reworded to that.

- Rationale: §5 "show, don't tell" — but chiefly, it is not true.
- Downstream search cue: any promise of citations, source chips, or
  "referenced" documents in the chat.

## Downstream Repos — Propagation Checklist

- [x] `faultmaven-copilot` — README About paragraph (live telemetry),
  Capabilities "Deep Linking" bullet, `package.json` description
  ("(WXT Version)" leaked into a published description), lint pattern +
  scope sync. Branch `docs/brand-sync-2026-08-21`.
- [x] `faultmaven-dashboard` — README About paragraph (live telemetry),
  lint pattern + scope sync. Branch `docs/brand-sync-2026-08-21`.
- [x] `faultmaven-website` — lint pattern sync; extension privacy policy
  ("assistant" → "copilot", and the permission table corrected against the
  shipped manifest — see below). Branch `docs/brand-sync-2026-08-21`.
- [x] `faultmaven-slack-agent` — README opening (telemetry as a grounding
  source; "engine" → "copilot"). Branch `docs/brand-sync-2026-08-21`.
  **Still has no brand-lint check** — the only public repo without one.

## Owner Decisions — Not Actioned Here

These are public brand surfaces that no repo's lint can reach, and that only
the owner can change.

1. **The GitHub org page contradicts §3.** Nine `fm-*-service` repos and
   `fm-api-gateway`, `fm-core-lib`, `fm-job-worker` are still **public**, with
   descriptions like *"FaultMaven Case Management Microservice"*. The skill's
   own §7 lists `fm-*-service` as obsolete repos not to reference — yet a
   visitor to github.com/FaultMaven sees a dozen of them. Archive or make
   private.
2. **`faultmaven-deploy` is public and describes an "open-source" stack.**
   Both halves violate §3: it is a retired repo, and the backend is
   fair-source (FSL-1.1-ALv2), never "open source".
3. **`faultmaven-modular` (public)** — "The official home of the FaultMaven
   platform" competes with `faultmaven` for the same claim.
4. **Two private repo descriptions carry retired names**:
   `faultmaven-enterprise-infra` ("FaultMaven Enterprise SaaS" — `Enterprise
   SaaS` is a grep-enforced retired pattern) and `faultmaven-cloud` ("Cloud
   edition … built on top of the open-source core" — "edition" and "open
   source" both retired).
5. **The published extension privacy policy is being corrected**
   (`faultmaven-website` branch above) because its permission table did not
   match the shipped manifest. Merging that is a publication decision, not a
   docs cleanup — see the CWS notes for the same defect.
