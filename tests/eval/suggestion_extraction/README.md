# Suggestion extraction — does an approved suggestion actually publish?

The re-runnable artifact behind #1226. Since #1214 the runbook quality gate
(`RunbookValidator`) runs inside `KnowledgeService.upload_document`, before its
first side effect — so a suggestion approved **without a human edit** publishes
only if the extractor's output clears that gate.

It did not. `EXTRACTION_PROMPT` asked for
`## Problem / ## Root Cause / ## Solution / ## Prevention`, which the validator
refuses with six errors:

```
No YAML frontmatter found
Missing required section: Symptom Recognition
Missing required section: Applicability
Missing required section: Diagnostic Steps
Missing required section: Causes
Missing required section: Sources
```

So every one-click approval was a 422, and the extract → review → approve loop
completed only through the review edit (`PUT`), where a reviewer reshaped the
draft into a v4 runbook by hand. The gate holding was correct and is unchanged
— **the extractor moved to meet it.** This driver is how that claim was
measured rather than asserted.

## Running it

```bash
python tests/eval/suggestion_extraction/run_extraction_eval.py before
python tests/eval/suggestion_extraction/run_extraction_eval.py after
python tests/eval/suggestion_extraction/run_extraction_eval.py both \
    --provider anthropic --json recorded-runs/<date>-<provider>.json
```

It needs a **live provider key**, and that is the point: a fixture-only run
proves the plumbing, not the prompt. `--provider` sets `CHAT_PROVIDER` before
settings are read, because the shipped default (`gemini`) answered `503 "This
model is currently experiencing high demand"` for the whole of the recorded
window — measuring a saturated endpoint would have reported the fallback
skeleton as the prompt's output.

`replay` re-scores a recorded run offline, no key and no network:

```bash
python tests/eval/suggestion_extraction/run_extraction_eval.py replay \
    --from recorded-runs/<file>.json
```

Recorded runs keep the full generated runbooks, so a change to the validator can
be re-scored against the same drafts instead of re-billing the model.

## What the two arms are

| Arm | What runs |
|---|---|
| `before` | The pre-#1226 path replayed verbatim — the old prompt (copied from `d8b8378a` into the driver), the old 2000-token cap, one attempt, no repair, no frontmatter-id forcing. |
| `after` | The **real** `SuggestionService.extract_knowledge_from_case`, over a stub case repository. Nothing about the prompt, the retry or the id minting is re-implemented in the driver — a driver that re-implements the path it measures measures itself. |

Both arms score with the same `RunbookValidator().validate_content` the approval
path applies, so the number is literally "would approval have published this".

## The corpus

`cases.json` — eight cases, written the way an engineer actually types
(partial sentences, pasted log lines, a wrong turn before the right one) rather
than as a clean statement of the answer. A prompt that only works on tidy input
has not been tested.

Three of them (`case_ev7_pii_noisy` above all) deliberately carry
incident-specific detail — hostnames, an email address, absolute timestamps, a
ticket id, a customer name — so the de-identification instruction has something
to remove.

One is **thin on purpose**: `case_ev8_thin_case` is "login page slow", no
evidence, no resolution. There is no failure mode there to write a runbook
about, so a failing draft is the honest outcome. The summary reports it apart
from the rest rather than folding it into the headline, which would either
flatter or punish the prompt for the wrong reason.

## What it found

`recorded-runs/2026-08-29-anthropic-claude-sonnet-4-5.json` — the committed
transcript at the shipped head, full runbooks included.

| Arm | Substantive cases passing the gate | Thin case | LLM calls |
|---|---|---|---|
| `before` | **0 / 7 (0%)** | 0 / 1 | 8 |
| `after` | **7 / 7 (100%)** | 1 / 1 | 11 |

Every `before` draft failed with the identical six errors — no frontmatter and
five of the six required sections — which is the point: the old prompt was not
*nearly* a runbook, it was a different document.

**Passes by attempt: 5 on the first draft, 3 after one repair turn.** That
split is what sized `MAX_EXTRACTION_ATTEMPTS`, and it did not start there — see
below.

### The first-draft profile is the thing to read next

`recorded-runs/2026-08-29-first-draft-profile-before-domain-fix.json`, produced
with `--attempts 1`, is the diagnostic that turned a working-but-expensive
result into a cheaper one. At that point **every** case needed the repair turn,
so an extraction cost two full generations. The profile said why:

```
PASS case_ev1_pg_pool
FAIL case_ev2_k8s_oom   Invalid domain 'kubernetes'…; Cause A: Indicator entry has no [Step N] token: 's1: [Step 2, Step 3] …'
FAIL case_ev3_tls_expiry  ID must be kebab-case: tls-outbound-tls-failure-due-to-expired-ca-certificate--7217
FAIL case_ev4_redis_evict Invalid domain 'cache'…
FAIL case_ev5_dns_ndots   Invalid domain 'kubernetes'…
PASS case_ev6_disk_full_wal
PASS case_ev7_pii_noisy
FAIL case_ev8_thin_case   Invalid domain 'web'…
```

Four of five failures were one systematic defect: the prompt enumerated the
`symptom_class` vocabulary but never `VALID_DOMAINS`. The *document* path is
handed a `domain` by its analysis pass and told not to change it; a case
supplies none, so the model free-picked the technology (`kubernetes`, `cache`,
`web`) where the schema wants a coarse system layer. Naming the vocabulary took
first-draft passes from 0/8 to 5/8 and the run from 16 LLM calls to 11.

**Run `--attempts 1` before touching the prompt.** The pass rate alone cannot
distinguish "the prompt is good" from "the repair turn is covering for it".

### Two things the pass rate does not say

- **The runbook `id` leaked incident detail.** The deliberately-noisy fixture
  produced a body the model had de-identified perfectly beside a frontmatter
  line reading `id: case-inc-48213-prod-web-07-returning-502-for-customer-c-…`.
  The id was minted from the raw case title by the extractor itself, and it
  lives *inside* the content, so it is chunked, embedded and retrieved. Fixed —
  minted from the draft's own de-identified `service` + `title`, which also
  fixed the same leak in `suggested_title` (the published item's name *and* its
  filename). This is the clearest argument for running the eval on noisy input
  rather than clean input.
- **A thin case yields a structurally valid but speculative runbook.**
  `case_ev8_thin_case` ("login page slow", no evidence, no resolution) passes:
  the model marks `## Applicability` with the rule-8 `[INSUFFICIENT SOURCE
  DATA]` escape but invents diagnostic steps and a cause with nothing in the
  case to support them. The gate is **structural** — it cannot tell a grounded
  runbook from a plausible one — so the human review step stays load-bearing.
  Not something the extractor can fix from its side.

These are facts about one model on one fixture set on one day, not invariants.
The invariants they motivated are pinned in CI, at
`tests/unit/modules/knowledge/test_extraction_emits_v4_schema_1226.py` — the
prompt carries the v4 template, the retry fires and feeds the structured errors
back, a still-failing draft reaches the reviewer with its reasons attached, and
the gate still refuses genuinely invalid content.

## Re-run it before changing the retry budget

`SuggestionService.MAX_EXTRACTION_ATTEMPTS` is 2 (first try plus one repair
turn) because of the attempt histogram this driver prints, not because 2 is a
nice number. Each extra attempt is a full runbook generation inside a
synchronous HTTP request, so the budget is bought with reviewer latency. Move it
only against a fresh run.
