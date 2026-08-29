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

See `recorded-runs/` for the committed transcripts. The headline is the
substantive-case pass rate of `validate_content` on extractor output, before
versus after.

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
